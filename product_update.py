"""
📄 商品情報の更新（キャリアからの変更依頼 → 商品詳細スプシ・トークスクリプトを直す）

  ① 変更依頼（PDF・Word・画像・メール・貼り付けた文）を入れる
  → ② Gemini が変更点を読み、商品詳細（スプシ）とトークスクリプト（スライド）の「直す場所」を案にする
  → ③ 人が見比べて、直すものだけ選ぶ → ④ すぐ直す／効く日に自動で直す（時間指定の業務 `product_update`）

⭐ AIは案を出すだけ。書き込むのは人が選んだものだけ。
⭐ 直す前の文字が、いまの中身に**ちょうど1か所**あるときだけ直す（AIが場所を取り違えても、別のセルを書き換えない）。
   予約の日に直すときも、その場で読み直して同じ確認をする（その間に人が直していたら、触らずに名指しする）。
⚠️ 数式のセルは直さない（数式が値で消える）。
⚠️ スライドは文の置き換えだけ。表や図の作り直し・スライドの追加は「手で直すこと」として出す。
直した前の文字は記録に残し、「↩ 元に戻す」で戻せる。

設定は Supabase の予約行 `__product_update__`：
  files＝[{name, kind: sheet|slides, url}]（⚠️ URLはコードに書かない＝公開リポジトリ）
  changes＝[{id, created_at, by, source, summary, apply_on, state, edits, manual, results}]
"""
import copy
import datetime
import email
import email.policy
import io
import json
import os
import re
import time
import unicodedata
import uuid
import zipfile

ROW = "__product_update__"
ROW_NAME = "（商品情報の更新の設定）"
KEEP_CHANGES = 200               # 記録はこれだけ残す（古い済んだものから捨てる）
NL = "⏎"                         # AIに渡すとき、セルの中の改行をこの字にする（行の区切りと見分けるため）

STATES = {
    "scheduled": "📅 予約",
    "applied": "✅ 直した",
    "partial": "⚠️ 一部だけ直した",
    "failed": "🛑 直せなかった",
    "undone": "↩ 元に戻した",
    "canceled": "🗑 取り消した",
}

INLINE_TYPES = {                 # Gemini にそのまま渡すもの
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
UPLOAD_TYPES = ["pdf", "png", "jpg", "jpeg", "webp", "gif", "docx", "xlsx", "pptx", "txt", "csv", "md", "eml"]


# ==========================================
# ⚙️ 設定
# ==========================================
def load_cfg(supabase) -> dict:
    try:
        res = supabase.table("merchants").select("config_json").eq("id", ROW).execute()
        cfg = (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception:
        cfg = {}
    cfg.setdefault("files", [])
    cfg.setdefault("changes", [])
    return cfg


def save_cfg(supabase, mutate) -> dict:
    """⚠️ 書く直前に読み直して、mutate(latest) で触ったところだけ変える（別のPCの変更を消さないため）。"""
    latest = load_cfg(supabase)
    mutate(latest)
    ch = latest.get("changes") or []
    if len(ch) > KEEP_CHANGES:
        live = [c for c in ch if c.get("state") == "scheduled"]
        rest = [c for c in ch if c.get("state") != "scheduled"]
        latest["changes"] = (rest[-(KEEP_CHANGES - len(live)):] if KEEP_CHANGES > len(live) else []) + live
        latest["changes"].sort(key=lambda c: c.get("created_at", ""))
    supabase.table("merchants").upsert({
        "id": ROW, "name": ROW_NAME, "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return latest


def update_change(supabase, cid: str, part: dict) -> dict:
    def _m(latest):
        for c in latest.get("changes") or []:
            if c.get("id") == cid:
                c.update(part)
    return save_cfg(supabase, _m)


def add_change(supabase, change: dict) -> dict:
    return save_cfg(supabase, lambda latest: latest.setdefault("changes", []).append(change))


def pc_name() -> str:
    return os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "?"


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


# ==========================================
# 📥 変更依頼を読む（ファイル → Gemini に渡せる形）
# ==========================================
def _xml_text(xml: str, para_tag: str) -> str:
    xml = re.sub(rf"</{para_tag}>", "\n", xml)
    xml = re.sub(r"<w:tab/>|<a:tab/>", "\t", xml)
    xml = re.sub(r"<w:br/>|<a:br/>", "\n", xml)
    txt = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(txt)


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return _xml_text(z.read("word/document.xml").decode("utf-8", "replace"), "w:p")


def _pptx_text(data: bytes) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, n in enumerate(names, 1):
            out.append(f"--- スライド{i} ---\n" + _xml_text(z.read(n).decode("utf-8", "replace"), "a:p"))
    return "\n".join(out)


def _xlsx_text(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"--- シート「{ws.title}」 ---")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(cells):
                out.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(out)


def _eml_text(data: bytes):
    """→ (本文, 添付[(名前, bytes)])"""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    head = f"件名：{msg.get('subject', '')}\n差出人：{msg.get('from', '')}\n日付：{msg.get('date', '')}\n"
    body, atts = "", []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            atts.append((fn, part.get_payload(decode=True) or b""))
        elif part.get_content_type() == "text/plain" and not body:
            body = part.get_content()
        elif part.get_content_type() == "text/html" and not body:
            body = re.sub(r"<[^>]+>", " ", part.get_content())
    return head + "\n" + body, atts


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return data.decode(enc)
        except Exception:
            pass
    return data.decode("utf-8", "replace")


def notice_parts(files, text: str = "") -> tuple:
    """files＝[(名前, bytes)]。→ (Gemini に渡す parts, 読めなかったファイルの名前)"""
    parts, ng = [], []
    if str(text or "").strip():
        parts.append("【貼り付けられた文】\n" + str(text).strip())
    queue = list(files or [])
    while queue:
        name, data = queue.pop(0)
        ext = os.path.splitext(name)[1].lower()
        try:
            if ext in INLINE_TYPES:
                parts.append(f"【添付：{name}】")
                parts.append({"mime_type": INLINE_TYPES[ext], "data": data})
            elif ext == ".docx":
                parts.append(f"【添付：{name}】\n" + _docx_text(data))
            elif ext == ".pptx":
                parts.append(f"【添付：{name}】\n" + _pptx_text(data))
            elif ext == ".xlsx":
                parts.append(f"【添付：{name}】\n" + _xlsx_text(data))
            elif ext == ".eml":
                body, atts = _eml_text(data)
                parts.append(f"【メール：{name}】\n" + body)
                queue.extend(atts)
            else:
                parts.append(f"【添付：{name}】\n" + _decode(data))
        except Exception as e:
            ng.append(f"{name}（{str(e)[:80]}）")
    return parts, ng


# ==========================================
# 🌐 URLを貼られたとき：アプリがページを取ってきて、中身をAIに渡す
#    （APIのGeminiはURLを渡しても見に行かない。ログインが要るページは読めない＝名指しして止める）
# ==========================================
URL_MAX_BYTES = 15 * 1024 * 1024
URL_MIN_TEXT = 80                 # これより短いページは「中身が無い」とみなす（画面をあとから組み立てるページなど）
_CT_EXT = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
           "image/gif": ".gif",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
           "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx"}
_LOGIN_WORDS = ("ログイン", "サインイン", "Sign in", "Log in", "login")


def split_urls(text: str) -> list:
    return list(dict.fromkeys(re.findall(r"https?://[^\s<>\"'、。）)]+", str(text or ""))))


def _safe_host(url: str):
    """社内の機械（localhost・社内のIP）には取りに行かない。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("http/https のURLではありません")
    try:
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("社内の機械のアドレスなので読みません")
    except socket.gaierror:
        raise ValueError("そのアドレスが見つかりません（URLの書き間違い？）")


def _html_text(html: str) -> tuple:
    """→ (題名, 本文)。script/style は捨て、段落や表の区切りは改行にする。"""
    import html as _h
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = _h.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
    s = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|section|article|dd|dt)>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", " | ", s)
    s = _h.unescape(re.sub(r"<[^>]+>", " ", s))
    lines = [re.sub(r"[ \t　]+", " ", x).strip() for x in s.splitlines()]
    return title, "\n".join(x for x in lines if x)


def fetch_url(url: str) -> tuple:
    """→ (名前, bytes, 種類, 文字コード)。読めなければ ValueError（日本語の理由）。"""
    import urllib.request
    import urllib.error
    _safe_host(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            _safe_host(r.geturl())                       # 転送された先も確かめる
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = r.read(URL_MAX_BYTES + 1)
            charset = r.headers.get_content_charset()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError(f"見る権利がありません（HTTP {e.code}・ログインが要るページの可能性）")
        if e.code == 404:
            raise ValueError("ページがありません（HTTP 404）")
        raise ValueError(f"ページを開けませんでした（HTTP {e.code}）")
    except urllib.error.URLError as e:
        raise ValueError(f"ページを開けませんでした（{str(e.reason)[:60]}）")
    if len(data) > URL_MAX_BYTES:
        raise ValueError("大きすぎます（15MBまで）")
    name = re.sub(r"[?#].*$", "", url.rstrip("/").rsplit("/", 1)[-1]) or "page"
    return name, data, ct, charset


def url_parts(urls: list) -> tuple:
    """URLのページを取ってきて、Geminiに渡す形にする。→ (parts, 読めなかった[URL（理由）], 読めた説明)"""
    parts, ng, ok = [], [], []
    for url in urls:
        try:
            name, data, ct, charset = fetch_url(url)
            ext = _CT_EXT.get(ct) or (os.path.splitext(name)[1].lower() if not ct.startswith("text/html") else "")
            if ext and ext != ".html" and ext != ".htm":
                p, bad = notice_parts([(name if name.lower().endswith(ext) else name + ext, data)])
                if bad:
                    raise ValueError(bad[0])
                parts.append(f"【ページ：{url}】")
                parts.extend(p)
                ok.append(f"{url}（{ext[1:].upper()}）")
                continue
            html = data.decode(charset or "utf-8", "replace") if charset else _decode(data)
            title, text = _html_text(html)
            if re.search(r"(?i)<input[^>]+type=[\"']?password", html) or \
                    (any(w.lower() in title.lower() for w in _LOGIN_WORDS) and len(text) < 2000):
                raise ValueError("ログインの画面でした（ログインが要るページは読めません。PDFや画面の写しを入れてください）")
            if len(text) < URL_MIN_TEXT:
                raise ValueError("中身がほとんどありませんでした（あとから画面を組み立てるページの可能性。画面の写しを入れてください）")
            parts.append(f"【ページ：{url}】\n題名：{title}\n{text[:60000]}")
            ok.append(f"{title or url}（{len(text):,}字）")
        except ValueError as e:
            ng.append(f"{url}（{e}）")
        except Exception as e:
            ng.append(f"{url}（{str(e)[:80]}）")
    return parts, ng, ok


# ==========================================
# 📚 直す先（スプシ・スライド）を読む
# ==========================================
def slides_id(url: str) -> str:
    m = re.search(r"/presentation/d/([A-Za-z0-9_-]+)", str(url or ""))
    return m.group(1) if m else str(url or "").strip()


def slides_service(sa_json: str):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    cr = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/presentations"])
    return build("slides", "v1", credentials=cr, cache_discovery=False)


def _open(gc, url):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def read_sheet_book(gc, url: str) -> dict:
    """→ {"title", "tabs": {タブ名: {"values": [[…]], "formulas": [[…]]}}}"""
    sh = _open(gc, url)
    wss = sh.worksheets()
    titles = [w.title for w in wss]
    ranges = ["'%s'" % t.replace("'", "''") for t in titles]
    vals = sh.values_batch_get(ranges).get("valueRanges", [])
    fms = sh.values_batch_get(ranges, params={"valueRenderOption": "FORMULA"}).get("valueRanges", [])
    tabs = {}
    for t, v, f in zip(titles, vals, fms):
        tabs[t] = {"values": v.get("values", []), "formulas": f.get("values", [])}
    return {"title": sh.title, "tabs": tabs, "gids": {w.title: w.id for w in wss}}


def _shape_items(el) -> list:
    """スライドの1つの部品から、文のかたまりを取り出す（表はセルごと）。
    → [{"obj": 部品のID, "cell": 表のセルの位置 or None, "text": 文}]（書き換えるときに場所が要るため）"""
    out = []
    run = lambda te: "".join((x.get("textRun") or {}).get("content", "") for x in te or [])
    if "shape" in el:
        s = run((el["shape"].get("text") or {}).get("textElements"))
        if s.strip():
            out.append({"obj": el.get("objectId"), "cell": None, "text": s})
    elif "table" in el:
        for r, row in enumerate(el["table"].get("tableRows") or []):
            for c, cell in enumerate(row.get("tableCells") or []):
                s = run((cell.get("text") or {}).get("textElements"))
                if s.strip():
                    out.append({"obj": el.get("objectId"), "cell": {"rowIndex": r, "columnIndex": c}, "text": s})
    elif "elementGroup" in el:
        for ch in el["elementGroup"].get("children") or []:
            out.extend(_shape_items(ch))
    return out


def read_slides(svc, url: str) -> dict:
    """→ {"title", "slides": [{"id", "no", "items": [{obj, cell, text}], "texts": [文…]}]}"""
    p = svc.presentations().get(presentationId=slides_id(url)).execute()
    slides = []
    for i, s in enumerate(p.get("slides") or [], 1):
        items = []
        for el in s.get("pageElements") or []:
            items.extend(_shape_items(el))
        slides.append({"id": s["objectId"], "no": i, "items": items, "texts": [x["text"] for x in items]})
    return {"title": p.get("title", ""), "slides": slides}


def read_docs(gc, sa_json: str, files: list) -> tuple:
    """登録したファイルを全部読む。→ (docs{名前: …}, 読めなかった[(名前, 理由)])"""
    docs, ng = {}, []
    svc = None
    for f in files or []:
        name, kind, url = f.get("name", ""), f.get("kind", "sheet"), f.get("url", "")
        if not (name and url):
            continue
        try:
            if kind == "slides":
                svc = svc or slides_service(sa_json)
                docs[name] = {"kind": "slides", "url": url, **read_slides(svc, url)}
            else:
                docs[name] = {"kind": "sheet", "url": url, **read_sheet_book(gc, url)}
        except Exception as e:
            ng.append((name, explain_error(e, kind)))
    return docs, ng


def file_id(url: str) -> str:
    m = re.search(r"/d/([A-Za-z0-9_-]+)", str(url or ""))
    return m.group(1) if m else str(url or "").strip()


def can_edit(sa_json: str, url: str):
    """サービスアカウントがそのファイルを書き換えられるか。→ True / False / None（確かめられない）"""
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        cr = Credentials.from_service_account_info(
            json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"])
        d = build("drive", "v3", credentials=cr, cache_discovery=False)
        f = d.files().get(fileId=file_id(url), fields="capabilities(canEdit)", supportsAllDrives=True).execute()
        return bool((f.get("capabilities") or {}).get("canEdit"))
    except Exception:
        return None


def explain_error(e, kind: str = "") -> str:
    s = str(e)
    if "has not been used" in s or "is disabled" in s or "SERVICE_DISABLED" in s:
        return ("Google Cloud で「Google Slides API」が有効になっていません。"
                "サービスアカウントのプロジェクトで有効にしてください。")
    if "403" in s or "PERMISSION_DENIED" in s or "does not have permission" in s:
        return "サービスアカウントに共有されていません（このファイルを、サービスアカウントに**編集者**として共有してください）。"
    if "404" in s:
        return "ファイルが見つかりません（URLを確かめてください）。"
    return s[:200]


# ==========================================
# 🤖 Gemini に変更点を読ませる
# ==========================================
def _cell(grid, r, c) -> str:
    try:
        v = grid[r][c]
    except IndexError:
        return ""
    return "" if v is None else str(v)


def dump_docs(docs: dict) -> str:
    """AIに渡す形。セルは A1 の番地つき、スライドはページIDつき。"""
    from gspread.utils import rowcol_to_a1
    out = []
    for name, d in docs.items():
        if d["kind"] == "sheet":
            out.append(f"### ファイル「{name}」（スプレッドシート）")
            for tab, t in d["tabs"].items():
                rows = []
                for r, row in enumerate(t["values"]):
                    cells = []
                    for c, v in enumerate(row):
                        v = str(v)
                        if v.strip():
                            fm = _cell(t["formulas"], r, c)
                            mark = "（数式）" if fm.startswith("=") else ""
                            cells.append(f"{rowcol_to_a1(r + 1, c + 1)}={v.replace(chr(13), '').replace(chr(10), NL)}{mark}")
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    out.append(f"#### シート「{tab}」")
                    out.extend(rows)
        else:
            out.append(f"### ファイル「{name}」（スライド）")
            for s in d["slides"]:
                out.append(f"#### スライド{s['no']}（ページID={s['id']}）")
                for t in s["texts"]:
                    # スライドの改行は「\n」（段落）と「\v」（段落の中の改行）の2種類ある
                    out.append("・" + t.rstrip("\n").replace("\v", NL).replace("\n", NL))
    return "\n".join(out)


PROMPT = """あなたは、通信・電気・ガスの取次をしている会社の事務担当です。
キャリア（提供会社）から届いた「変更のお知らせ・変更依頼」を読み、社内の資料（商品詳細のスプレッドシートと、
トークスクリプトのスライド）のどこを、どう直せばよいかの案を出してください。

きょうの日付：{today}

# 決まり
- 直すのは、お知らせに**はっきり書いてある変更**だけ。推測で変えない。関係のない箇所は触らない。
- 1つの直し＝1か所。スプレッドシートは「ファイル・シート・セル番地」、スライドは「ファイル・ページID」で場所を示す。
- "old" は、その場所の**いまの文字の一部をそのまま写したもの**（1文字も変えない）。その場所の中で1回だけ出てくる長さにする。
  セルの中の改行は「{nl}」で表す（資料の表示と同じ）。スライドの "old" には「{nl}」を含めない（1段落の中で収める）。
- "new" は "old" を置き換える文字。前後の書き方（全角半角・単位・区切り）は、まわりに合わせる。
- 「（数式）」と付いたセルは直さない（manual に入れる）。
- 表や図の作り直し、行・スライドの追加や削除、どこを直すか決めきれないものは、edits に入れず manual に書く。
- 同じ内容が複数の場所（シート・スライド）に書いてあれば、全部を edits に入れる。
- ⚠️ **名前の似た別の商品・プランを取り違えない。** 例：「ニチガス単体」と「ニチガス単体（空室）」、「SBAir」と「SBAir 5」は別の商品。
  お知らせ（と担当者の指示）が指している商品・プランの行・シート・スライドだけを直す。同じキャリアのほかのプランは、
  お知らせにそのプランも変わるとはっきり書いていない限り触らない。どのプランのことか決めきれなければ edits に入れず manual に書く。
- 「担当者からの指示」があれば、それがいちばん優先。指示で「直さない」と言われた商品・場所は edits に入れない。
- 各 edit の "target" に、その場所が**どの商品・プランの記述か**を書く（例：「ニチガス単体（空室）」）。
- 変更の効く日（適用日・開始日）が書いてあれば effective_date に YYYY-MM-DD で入れる（年が無ければきょうから見て次に来る日）。無ければ空。

# 出力（JSONだけ）
{{
  "summary": "どのキャリアの、何が、いつから、どう変わるか（2〜4行）",
  "carrier": "キャリア名",
  "effective_date": "YYYY-MM-DD または空",
  "edits": [
    {{"file": "ファイル名", "tab": "シート名（スライドなら空）", "cell": "A1（スライドなら空）",
      "slide": "ページID（スプレッドシートなら空）", "target": "どの商品・プランの記述か",
      "old": "いまの文字", "new": "新しい文字", "reason": "お知らせのどこに基づくか"}}
  ],
  "manual": [ {{"file": "ファイル名", "where": "場所", "what": "手で直してほしいこと"}} ]
}}

# 社内の資料（いまの中身）
{docs}
"""


KEYWORD_PROMPT = """キャリアからの変更のお知らせ（と担当者の指示）を読み、社内資料の中から関係する場所を探すための
「手がかりの言葉」を出してください。キャリア名・会社名・ブランド名・商品名・プラン名（略し方の違いも）と、
お知らせに書いてある**変わる前の値**（料金・電話番号・日数など）を、資料に書いてありそうな形で入れます。
JSONだけ：{"keywords": ["…", "…"]}"""


def _norm_kw(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).lower().replace(" ", "").replace("　", "")


def keywords(model, parts: list, instruction: str = "") -> list:
    """① 小さな問い合わせで手がかりの言葉だけ出させる（資料は渡さない）。"""
    tail = ["# 担当者からの指示\n" + instruction.strip()] if str(instruction or "").strip() else []
    res = model.generate_content([KEYWORD_PROMPT, "# お知らせ", *parts, *tail],
                                 generation_config={"response_mime_type": "application/json", "temperature": 0})
    txt = re.sub(r"^```(?:json)?|```$", "", (res.text or "").strip()).strip()
    kws = [str(k).strip() for k in (json.loads(txt).get("keywords") or [])]
    return [k for k in dict.fromkeys(kws) if len(_norm_kw(k)) >= 2]


def narrow_docs(docs: dict, kws: list) -> tuple:
    """② 手がかりの言葉が出てくるシート・スライドだけ残す（AIの無料枠＝1分25万トークンに収めるため。
    LLのトークスクリプトだけで約19万字ある）。→ (絞った docs, 絞った内容の説明)"""
    keys = [_norm_kw(k) for k in kws]
    hit = lambda text: any(k in _norm_kw(text) for k in keys)
    out, note = {}, []
    for name, d in docs.items():
        if d["kind"] == "sheet":
            tabs = {t: v for t, v in d["tabs"].items()
                    if hit(t) or any(hit(c) for row in v["values"] for c in row if str(c).strip())}
            if tabs:
                out[name] = {**d, "tabs": tabs}
            note.append(f"{name}：シート {len(tabs)}／{len(d['tabs'])}枚")
        else:
            slides = [s for s in d["slides"] if any(hit(t) for t in s["texts"])]
            if slides:
                out[name] = {**d, "slides": slides}
            note.append(f"{name}：スライド {len(slides)}／{len(d['slides'])}枚")
    return out, note


def _one_paragraph(e: dict):
    """スライドで、段落をまたいだ「前の文字」を出されたとき、変わる1段落だけに絞る（置き換えは1段落の中でしかできないため）。"""
    old, new = _real(e["old"]).strip("\n"), _real(e["new"]).strip("\n")
    if "\n" not in old:
        return
    a, b = old.split("\n"), new.split("\n")
    if len(a) != len(b):
        return
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    if len(diff) == 1 and diff[0][0].strip():
        e["old"], e["new"] = diff[0]


PAGE_RULE = (
    "「【ページ：…】」で始まるものはWebページをまるごと文字にしたもの。メニュー・会社案内・お問い合わせ先・"
    "ほかのお知らせの一覧など、今回の変更のお知らせではない部分は使わない。"
    "「いつから・何が・どう変わる」として書かれていることだけを変更として扱う。"
)


def analyze(api_key: str, parts: list, docs: dict, today: datetime.date = None, instruction: str = "") -> dict:
    """instruction＝担当者からの指示（「空室プランだけ」など）。お知らせより優先させる。
    ⭐ 決まりでAIを縛るより、人が選んだものだけ直す（画面は案を全部チェックなしで出す＝担当者 2026-10-06）。

    ① 手がかりの言葉を出させる → ② その言葉が出てくるシート・スライドだけに絞る → ③ 直す場所の案を出させる。
    """
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    today = today or datetime.date.today()
    kws = keywords(model, parts, instruction)
    small, note = narrow_docs(docs, kws)
    if not small:
        small = {n: d for n, d in docs.items() if d["kind"] == "sheet"}
        note.append("手がかりの言葉がどこにも無かったので、スプレッドシートだけを見ました")
    prompt = PROMPT.format(today=today.isoformat(), nl=NL, docs=dump_docs(small))
    prompt = prompt + "\n# ページの読み方\n- " + PAGE_RULE
    tail = []
    if str(instruction or "").strip():
        tail = ["# 担当者からの指示（いちばん優先。どの商品・プランを直すか／直さないか）\n" + str(instruction).strip()]
    res = model.generate_content(
        [prompt, "# キャリアからのお知らせ（ここから）", *parts, "# お知らせ（ここまで）", *tail],
        generation_config={"response_mime_type": "application/json", "temperature": 0.1})
    txt = (res.text or "").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt).strip()
    out = json.loads(txt)
    out.setdefault("edits", [])
    out.setdefault("manual", [])
    out["keywords"], out["looked"] = kws, note
    for e in out["edits"]:
        e["id"] = uuid.uuid4().hex[:8]
        for k in ("file", "tab", "cell", "slide", "target", "old", "new", "reason"):
            e[k] = str(e.get(k) or "")
        if e["slide"]:
            _one_paragraph(e)
    settle(docs, out["edits"])
    return out


def explain_ai_error(e) -> str:
    s = str(e)
    if "429" in s or "quota" in s.lower() or "ResourceExhausted" in s:
        return ("AI（Gemini）の無料枠の上限に当たりました。1分ほど待ってから、もう一度押してください"
                "（1日の回数を使い切ったときは、翌日まで待ちます）。")
    return "AIの読み取りに失敗しました：" + s[:300]


# ==========================================
# 🔎 確かめる（いまの中身に、old がちょうど1か所あるか）
# ==========================================
def _real(s: str) -> str:
    return str(s or "").replace(NL, "\n")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).replace("\r", "")


_DASHES = dict.fromkeys(map(ord, "－‐‑−–—"), "-")
_SPACES = set(" \t　\n\v\r")


def _norm_map(s: str):
    """ゆるく比べるための形（全角半角・空白・改行・ハイフンの種類を無視）と、元の位置の対応。"""
    out, pos = [], []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch).translate(_DASHES).replace("〜", "~"):
            if c in _SPACES:
                continue
            out.append(c)
            pos.append(i)
    return "".join(out), pos


def _find(cur: str, old: str) -> list:
    """cur の中で old が出てくる場所 [(始め, 終わり)]。まずそのまま探し、無ければゆるく探す。
    ⚠️ ゆるく探すのは「AIの写し間違い（全角半角・改行・空白）」を吸収するためで、違う言葉に当てるためではない。"""
    if not old:
        return []
    spans, i = [], cur.find(old)
    while i >= 0:
        spans.append((i, i + len(old)))
        i = cur.find(old, i + 1)
    if spans:
        return spans
    nc, pos = _norm_map(cur)
    no, _ = _norm_map(old)
    if not no:
        return []
    i = nc.find(no)
    while i >= 0:
        spans.append((pos[i], pos[i + len(no) - 1] + 1))
        i = nc.find(no, i + 1)
    return spans


def _trim(o: str, new: str) -> tuple:
    """前とあとで同じ頭と尻尾の長さ。そこは元の文字のまま残す（元の改行・書式を、AIの写し方で崩さないため）。
    同じとみなすのは「まったく同じ文字」か「どちらも空白・改行」だけ（全角→半角のような直しは、まとめて置き換える）。"""
    same = lambda x, y: x == y or (x in _SPACES and y in _SPACES)
    p = 0
    while p < min(len(o), len(new)) and same(o[p], new[p]):
        p += 1
    s = 0
    while s < min(len(o), len(new)) - p and same(o[-1 - s], new[-1 - s]):
        s += 1
    return p, s


def _replace(text: str, a: int, b: int, new: str) -> tuple:
    """text[a:b] を new にした結果と、実際に入れ替える範囲・文字。→ (結果, 始め, 終わり, 入れる文字)"""
    p, s = _trim(text[a:b], new)
    a2, b2, rep = a + p, b - s, new[p:len(new) - s]
    return text[:a2] + rep + text[b2:], a2, b2, rep


def _sheet_cells(t: dict):
    """シートの、数式でない文字のあるセル → [(行, 列, 中身)]（0始まり）"""
    for r, row in enumerate(t["values"]):
        for c, v in enumerate(row):
            if str(v).strip() and not _cell(t["formulas"], r, c).startswith("="):
                yield r, c, str(v).replace("\r", "")


def locate(docs: dict, e: dict) -> dict:
    """→ {"ok": bool, "why": 理由, "before": いまの中身, "after": 直したあとの中身, "done": すでに直っている,
          "old": 実際の前の文字（AIの写し間違いを直したもの）, "cell": 場所を直したときの番地, "note": 直したことの説明}"""
    d = docs.get(e.get("file", ""))
    old, new = _real(e.get("old")), _real(e.get("new"))
    if not d:
        return {"ok": False, "why": f"ファイル「{e.get('file')}」が見つかりません"}
    if old == new:
        return {"ok": False, "why": "前とあとが同じです"}
    if d["kind"] == "sheet":
        from gspread.utils import a1_to_rowcol, rowcol_to_a1
        t = d["tabs"].get(e.get("tab", ""))
        if t is None:
            return {"ok": False, "why": f"シート「{e.get('tab')}」がありません"}
        try:
            r, c = a1_to_rowcol(e.get("cell", "").strip().upper())
        except Exception:
            return {"ok": False, "why": f"セル番地「{e.get('cell')}」が読めません"}
        cur = _cell(t["values"], r - 1, c - 1).replace("\r", "")
        if _cell(t["formulas"], r - 1, c - 1).startswith("="):
            return {"ok": False, "why": "数式のセルです（手で直してください）", "before": cur}
        if not old.strip():
            # 空のセルに書き足す案。セルが本当に空のときだけ書く（入っている文字を黙って消さない）
            if not cur.strip():
                return {"ok": True, "why": "", "before": cur, "after": new, "old": ""}
            return {"ok": False, "before": cur,
                    "why": f"AIは空のセルのつもりでしたが「{cur[:20]}」が入っています（「前」の欄に今の文字を入れれば直せます）"}
        spans = _find(cur, old)
        note, cell = "", None
        if not spans:
            if new and _find(cur, new):
                return {"ok": False, "done": True, "why": "すでに直っています", "before": cur}
            # AIがセル番地だけ取り違えたとき：同じシートの中で、その文字があるセルが1つだけならそこにする
            hits = [(rr, cc, v, sp) for rr, cc, v in _sheet_cells(t) for sp in [_find(v, old)] if len(sp) == 1]
            if len(hits) == 1:
                rr, cc, cur, spans = hits[0][0], hits[0][1], hits[0][2], hits[0][3]
                cell = rowcol_to_a1(rr + 1, cc + 1)
                note = f"AIの指したセル（{e.get('cell')}）には無かったので、同じシートで見つかった {cell} にしました"
            else:
                why = "直す前の文字が、このセルにもシートの中にもありません（すでに直された・AIの読み違い）" if not hits else \
                      f"直す前の文字が、このセルに無く、シートの中に{len(hits)}か所あります（どれか決められません）"
                return {"ok": False, "why": why, "before": cur}
        if len(spans) > 1:
            return {"ok": False, "why": f"直す前の文字が、このセルに{len(spans)}か所あります（どれか決められません）", "before": cur}
        a, b = spans[0]
        after = _replace(cur, a, b, new)[0]
        out = {"ok": True, "why": "", "before": cur, "after": after, "old": cur[a:b], "note": note}
        if cell:
            out["cell"] = cell
        return out
    # スライド
    s = next((x for x in d["slides"] if x["id"] == e.get("slide")), None)
    if not s:
        return {"ok": False, "why": f"ページ「{e.get('slide')}」が見つかりません"}
    if not old.strip():
        return {"ok": False, "why": "スライドは、どこに書き足すか決められません（手で直してください）"}
    items = s.get("items") or [{"obj": None, "cell": None, "text": t} for t in s["texts"]]
    hits = [(k, sp) for k, it in enumerate(items) for sp in _find(it["text"], old)]
    if not hits:
        if new and any(_find(it["text"], new) for it in items):
            return {"ok": False, "done": True, "why": "すでに直っています"}
        return {"ok": False, "why": "直す前の文字が、このスライドにありません（すでに直された・AIの読み違い）"}
    if len(hits) > 1:
        return {"ok": False, "why": f"直す前の文字が、このスライドに{len(hits)}か所あります（どれか決められません）"}
    k, (a, b) = hits[0]
    t = items[k]["text"]
    after, a2, b2, rep = _replace(t, a, b, new)
    return {"ok": True, "why": "", "before": t.rstrip("\n"), "after": after.rstrip("\n"),
            "old": t[a:b], "item": k, "span": (a2, b2), "rep": rep}


def settle(docs: dict, edits: list):
    """AIの案を、実際の中身に合わせて直す（写し間違いの「前」・取り違えたセル番地）。人が見る前に1回だけ。"""
    for e in edits:
        lc = locate(docs, e)
        if lc.get("ok"):
            if lc.get("old") is not None:
                e["old"] = lc["old"].replace("\v", NL).replace("\n", NL)
            if lc.get("cell"):
                e["cell"] = lc["cell"]
            if lc.get("note"):
                e["note"] = lc["note"]


def row_label(docs: dict, e: dict) -> str:
    """その場所が何の行か（人が取り違えに気づくため）。スプシはその行のA列（空なら最初の文字）、スライドは1つ目の文。"""
    d = docs.get(e.get("file", "")) or {}
    if d.get("kind") == "slides":
        s = next((x for x in d.get("slides", []) if x["id"] == e.get("slide")), None)
        # 1つ目の文は「目次に」のようなボタンのことがあるので、見出しらしい文を選ぶ
        t = [x.strip().split("\n")[0] for x in (s or {}).get("texts") or [] if x.strip()]
        t = [x for x in t if "目次" not in x and len(x) >= 3] or t
        return t[0][:30] if t else ""
    from gspread.utils import a1_to_rowcol
    t = (d.get("tabs") or {}).get(e.get("tab", ""))
    try:
        r, _ = a1_to_rowcol(str(e.get("cell", "")).strip().upper())
        row = t["values"][r - 1]
    except Exception:
        return ""
    first = next((str(v) for v in row if str(v).strip()), "")
    return (str(row[0]) if row and str(row[0]).strip() else first).replace("\n", " ")[:30]


def where_label(docs: dict, e: dict) -> str:
    d = docs.get(e.get("file", "")) or {}
    if d.get("kind") == "slides" or e.get("slide"):
        s = next((x for x in d.get("slides", []) if x["id"] == e.get("slide")), None)
        return f"スライド{s['no']}" if s else f"ページ {e.get('slide')}"
    return f"{e.get('tab')}!{e.get('cell')}"


def place_url(docs: dict, e: dict) -> str:
    """その場所を開くリンク（スプシはそのセル、スライドはその1枚）。直す前に人が実物を見るため。"""
    d = docs.get(e.get("file", "")) or {}
    fid = file_id(d.get("url", ""))
    if not fid:
        return ""
    if d.get("kind") == "slides":
        return f"https://docs.google.com/presentation/d/{fid}/edit#slide=id.{e.get('slide', '')}"
    gid = (d.get("gids") or {}).get(e.get("tab", ""))
    if gid is None:
        return f"https://docs.google.com/spreadsheets/d/{fid}/edit"
    cell = str(e.get("cell", "")).strip().upper()
    return f"https://docs.google.com/spreadsheets/d/{fid}/edit#gid={gid}" + (f"&range={cell}" if cell else "")


# ==========================================
# ✏️ 書き込む／元に戻す
# ==========================================
def _sheet_value(before: str, after: str):
    """チェックボックス（TRUE/FALSE）・数字は、その型のまま書く（文字にすると壊れる）。"""
    b, a = before.strip().upper(), after.strip().upper()
    if b in ("TRUE", "FALSE") and a in ("TRUE", "FALSE"):
        return a == "TRUE"
    if re.fullmatch(r"-?\d+(\.\d+)?", before.strip()) and re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?", after.strip()):
        return float(after) if "." in after else int(after)
    return after


def _remember(docs: dict, e: dict, lc: dict):
    d = docs[e["file"]]
    if d["kind"] == "sheet":
        from gspread.utils import a1_to_rowcol
        r, c = a1_to_rowcol(_cell_of(e, lc))
        grid = d["tabs"][e["tab"]]["values"]
        while len(grid) < r:
            grid.append([])
        while len(grid[r - 1]) < c:
            grid[r - 1].append("")
        grid[r - 1][c - 1] = lc["after"]
    else:
        s = next(x for x in d["slides"] if x["id"] == e["slide"])
        it = s["items"][lc["item"]]
        a, b = lc["span"]
        it["text"] = it["text"][:a] + lc["rep"] + it["text"][b:]
        s["texts"] = [x["text"] for x in s["items"]]


def _cell_of(e: dict, lc: dict) -> str:
    return str(lc.get("cell") or e["cell"]).strip().upper()


def _u16(s: str) -> int:
    """スライドの文字位置は UTF-16 で数える（絵文字などで Python の数え方とずれるため）。"""
    return len(s.encode("utf-16-le")) // 2


def _slide_requests(d: dict, e: dict, lc: dict) -> list:
    """1か所ぶんの書き換え。1段落の中なら replaceAllText（書式がそのまま残る）、
    段落をまたぐときは、その場所の文字を消して入れ直す。"""
    s = next(x for x in d["slides"] if x["id"] == e["slide"])
    it = s["items"][lc["item"]]
    a, b = lc["span"]                   # 変わるところだけ（_replace で頭と尻尾の同じ部分を除いた範囲）
    old, new = it["text"][a:b], lc["rep"]
    if old and "\n" not in old and "\v" not in old and "".join(x["text"] for x in s["items"]).count(old) == 1:
        return [{"replaceAllText": {"containsText": {"text": old, "matchCase": True},
                                    "replaceText": new, "pageObjectIds": [e["slide"]]}}]
    start, end = _u16(it["text"][:a]), _u16(it["text"][:b])
    where = {"objectId": it["obj"]}
    if it.get("cell"):
        where["cellLocation"] = it["cell"]
    reqs = []
    if end > start:                     # 足すだけのとき（消す文字が無い）は消さない
        reqs.append({"deleteText": {**where, "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end}}})
    if new:
        reqs.append({"insertText": {**where, "insertionIndex": start, "text": new}})
    return reqs


def apply_edits(gc, sa_json: str, files: list, edits: list) -> list:
    """いまの中身を読み直して、確かめられたものだけ書く。→ 1件ずつの結果
    結果＝{"id", "mark": ✅/⏭/⚠️/🛑, "why", "before", "after", "at"}"""
    names = {e["file"] for e in edits}
    docs, ng = read_docs(gc, sa_json, [f for f in files if f.get("name") in names])
    ngd = dict(ng)
    out = []
    by_file = {}
    for e in edits:
        if e["file"] in ngd:
            out.append({"id": e["id"], "mark": "🛑", "why": f"読めませんでした：{ngd[e['file']]}"})
            continue
        lc = locate(docs, e)
        if lc.get("done"):
            out.append({"id": e["id"], "mark": "⏭", "why": "すでに直っていました"})
        elif not lc["ok"]:
            out.append({"id": e["id"], "mark": "⚠️", "why": lc["why"] + "（触っていません）"})
        else:
            reqs = _slide_requests(docs[e["file"]], e, lc) if docs[e["file"]]["kind"] == "slides" else None
            by_file.setdefault(e["file"], []).append((e, lc, reqs))
            _remember(docs, e, lc)       # 同じセル・スライドに2つ目の直しがあれば、直したあとの中身で確かめる
    svc = None
    for fname, items in by_file.items():
        d = docs[fname]
        try:
            if d["kind"] == "sheet":
                sh = _open(gc, d["url"])
                by_tab = {}
                for e, lc, _ in items:
                    by_tab.setdefault(e["tab"], []).append((e, lc))
                for tab, its in by_tab.items():
                    ws = sh.worksheet(tab)
                    ws.batch_update([{"range": _cell_of(e, lc),
                                      "values": [[_sheet_value(lc["before"], lc["after"])]]} for e, lc in its],
                                    value_input_option="RAW")
                    for e, lc in its:
                        out.append({"id": e["id"], "mark": "✅", "why": "", "before": lc["before"],
                                    "after": lc["after"], "at": now_str(), "cell": _cell_of(e, lc)})
            else:
                svc = svc or slides_service(sa_json)
                reqs = [r for _, _, rs in items for r in rs]
                svc.presentations().batchUpdate(presentationId=slides_id(d["url"]),
                                                body={"requests": reqs}).execute()
                for e, lc, _ in items:
                    out.append({"id": e["id"], "mark": "✅", "why": "", "before": lc["before"],
                                "after": lc["after"], "at": now_str()})
        except Exception as ex:
            for e, lc, _ in items:
                if not any(o["id"] == e["id"] for o in out):
                    out.append({"id": e["id"], "mark": "🛑", "why": explain_error(ex)})
    order = {e["id"]: i for i, e in enumerate(edits)}
    return sorted(out, key=lambda o: order.get(o["id"], 0))


def undo_edits(gc, sa_json: str, files: list, edits: list, results: list) -> list:
    """✅ で直したものを、前の文字に戻す（いまの中身が「直したあと」のままのときだけ）。"""
    done = {r["id"] for r in results if r.get("mark") == "✅"}
    back = [{**e, "old": e["new"], "new": e["old"]} for e in edits if e["id"] in done]
    return apply_edits(gc, sa_json, files, back) if back else []


def overall(results: list) -> str:
    marks = [r.get("mark") for r in results]
    if marks and all(m in ("✅", "⏭") for m in marks):
        return "applied"
    if "✅" in marks:
        return "partial"
    return "failed"


def result_lines(change: dict) -> list:
    eds = {e["id"]: e for e in change.get("edits") or []}
    out = []
    for r in change.get("results") or []:
        e = eds.get(r["id"], {})
        where = f"{e.get('file', '')}／{e.get('tab') or 'スライド'}{('!' + e['cell']) if e.get('cell') else ''}"
        out.append(f"{r['mark']} {where}：{_real(e.get('old'))} → {_real(e.get('new'))}"
                   + (f"（{r['why']}）" if r.get("why") else ""))
    return out


def apply_change(supabase, gc, sa_json: str, cfg: dict, change: dict) -> dict:
    """1件の変更（人が選んだ直し）を書き込み、記録する。→ 書いたあとの change"""
    res = apply_edits(gc, sa_json, cfg.get("files") or [], change.get("edits") or [])
    part = {"results": res, "state": overall(res), "applied_at": now_str(), "applied_by": pc_name()}
    update_change(supabase, change["id"], part)
    return {**change, **part}


# ==========================================
# ⏰ 時間指定（業務の種類 product_update）：効く日になった予約を直す
# ==========================================
def due(cfg: dict, today: datetime.date = None) -> list:
    today = (today or datetime.date.today()).isoformat()
    return [c for c in cfg.get("changes") or []
            if c.get("state") == "scheduled" and str(c.get("apply_on") or "") <= today]


def run(supabase, gc, sa_json: str, cfg: dict = None, today: datetime.date = None) -> dict:
    import auto_jobs
    cfg = cfg if cfg is not None else load_cfg(supabase)
    steps = auto_jobs._Steps()
    todo = due(cfg, today)
    if not todo:
        steps.add("商品情報の更新", "⏹", "きょう直す予約はありません")
        return steps.result()
    if not gc:
        steps.add("商品情報の更新", "🛑", "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です")
        return steps.result()
    for c in todo:
        done = apply_change(supabase, gc, sa_json, cfg, c)
        mark = {"applied": "✅", "partial": "⏸", "failed": "🛑"}[done["state"]]
        head = f"{c.get('carrier') or ''} {c.get('source') or ''}".strip() or "変更"
        steps.add(f"商品情報の更新（{head}）", mark,
                  (c.get("summary") or "") + "\n" + "\n".join(result_lines(done)))
    return steps.result()


def new_change(proposal: dict, edits: list, apply_on: str, source: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:10],
        "created_at": now_str(),
        "by": pc_name(),
        "source": source,
        "carrier": proposal.get("carrier", ""),
        "summary": proposal.get("summary", ""),
        "apply_on": apply_on,
        "state": "scheduled",
        "edits": copy.deepcopy(edits),
        "manual": copy.deepcopy(proposal.get("manual") or []),
        "results": [],
    }
