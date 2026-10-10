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
🆕 新しい商品は、商品詳細（スプシ）に**お手本の行を写して1行足す**（`analyze_new` → `apply_rows`）。
   足すのは人がチェックした行だけ。元に戻すと、その行の中身が足したときのままなら消す。
直した前の文字は記録に残し、「↩ 元に戻す」で戻せる。

設定は Supabase の予約行 `__product_update__`：
  files＝[{name, kind: sheet|slides, url}]（⚠️ URLはコードに書かない＝公開リポジトリ）
  changes＝[{id, created_at, by, source, summary, apply_on, state, edits, rows, manual, results}]
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
    import gemini_key
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(gemini_key.MODEL)
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


# ==========================================
# 🆕 新しい商品を足す（お手本の行を写して、すぐ下に1行足す）
# ==========================================
NEW_PROMPT = """あなたは、通信・電気・ガスの取次をしている会社の事務担当です。
キャリア（提供会社）から届いた「新しい商品の概要」を読み、社内の商品詳細（スプレッドシート）に
**その商品の行を足す**案を出してください。

きょうの日付：{today}

# 決まり
- 商品詳細は、1つの商品＝1行で、同じ種類の商品（電気・ガス・セットなど）がまとまって並んでいます。
  新しい商品を載せるべきシートごとに、rows に1件ずつ入れます。
- "template_row"：その新しい商品にいちばん近い既存の商品の行番号（お手本。一次店・エリア・種類が近いもの）。
- "after_row"：新しい行をどの行のすぐ下に足すか（ふつうは同じ種類のまとまりの、いちばん下の商品の行）。
- "cells"：新しい行に入れる値。キーは列の文字（"A","B",…）。A列（または商品名の列）には商品名を入れる。
  - 書くのは、お知らせに**はっきり書いてあること**だけ。書いていない項目は入れない（空のまま。推測で埋めない）。
  - 書き方（〇×△、TRUE/FALSE、「〜営業日」、改行）は、お手本の行とその列の見出しに合わせる。セルの中の改行は「{nl}」。
  - 見出しの意味が合う列だけに入れる。合う列が無い大事なことは、その表の「備考」の列にまとめる（備考の列が無ければ manual）。
- その商品の名前の行が、もうシートにあって中身が空なら、行を足さずに edits で空のセルを埋める（"old" は空）。
- 「（数式）」と付いたセルの列には値を入れない。
- スライド（トークスクリプト）の追加や、どこに載せるか決めきれないものは manual に書く。
- 「担当者からの指示」があれば、それがいちばん優先。

# 出力（JSONだけ）
{{
  "summary": "どんな商品か（2〜4行）",
  "carrier": "商品名",
  "effective_date": "取り扱いを始める日（YYYY-MM-DD）。無ければ空",
  "rows": [
    {{"tab": "シート名", "template_row": 17, "after_row": 24, "target": "どの種類のまとまりに足すか",
      "cells": {{"A": "商品名", "B": "…"}}, "reason": "お知らせのどこに基づくか"}}
  ],
  "edits": [
    {{"file": "ファイル名", "tab": "シート名", "cell": "B20", "slide": "", "target": "商品名",
      "old": "", "new": "入れる値", "reason": "…"}}
  ],
  "manual": [ {{"file": "ファイル名", "where": "場所", "what": "手で直してほしいこと"}} ]
}}

# 商品詳細（いまの中身）
{docs}
"""


def _col(c: int) -> str:
    """1始まりの列番号 → 列の文字"""
    from gspread.utils import rowcol_to_a1
    return re.sub(r"\d", "", rowcol_to_a1(1, c))


def _colno(letter: str) -> int:
    from gspread.utils import a1_to_rowcol
    return a1_to_rowcol(str(letter).strip().upper() + "1")[1]


def _name_key(s: str) -> str:
    return _norm_kw(s).replace("\n", "")


def col_heads(t: dict, n: int) -> list:
    """列ごとの見出し（いちばん上の3行のうち、2つ以上のセルに字がある行を見出しとみなす）。"""
    heads = [""] * n
    for row in t["values"][:3]:
        if sum(1 for v in row if str(v).strip()) < 2:
            continue
        for c in range(min(n, len(row))):
            v = str(row[c]).replace("\n", "").strip()
            if v:
                heads[c] = (heads[c] + "／" + v) if heads[c] else v
    return heads


def _row_name(t: dict, r: int) -> str:
    """r 行目（1始まり）の A列"""
    return _cell(t["values"], r - 1, 0).replace("\n", " ").strip()


def _find_row(t: dict, r: int, name: str):
    """r 行目の A列が name のままならそこ、ずれていれば A列が name の行が1つだけのときその行。→ (行, 説明)"""
    if name and _name_key(_row_name(t, r)) == _name_key(name):
        return r, ""
    hits = [i + 1 for i in range(len(t["values"])) if name and _name_key(_row_name(t, i + 1)) == _name_key(name)]
    if len(hits) == 1:
        return hits[0], f"「{name}」が {r}行目から {hits[0]}行目に動いていたので、そこにしました"
    if not hits:
        return None, f"「{name}」の行が見つかりません"
    return None, f"「{name}」の行が{len(hits)}つあります（{r}行目から動いていて、どれか決められません）"


def _indirect_risk(d: dict, tab: str) -> str:
    """行を足すと、行番号を文字で持つ数式（INDIRECT）はずれる。そのシートを指す INDIRECT があれば名指しする。"""
    for tname, t in d["tabs"].items():
        for row in t["formulas"]:
            for f in row:
                f = str(f)
                if f.startswith("=") and "INDIRECT" in f.upper() and (tab in f or tname == tab):
                    return f"シート「{tname}」に、行番号を文字で持つ数式（INDIRECT）があります（行を足すとずれるおそれ）"
    return ""


def check_row(docs: dict, r: dict) -> dict:
    """新しい行の案を、いまの中身で確かめる。
    → {"ok", "why", "done", "after_row", "template_row", "note", "values": [A列からの値], "skipped": [数式の列]}"""
    d = docs.get(r.get("file", "")) or {}
    if d.get("kind") != "sheet":
        return {"ok": False, "why": f"ファイル「{r.get('file')}」（スプレッドシート）が見つかりません"}
    t = d["tabs"].get(r.get("tab", ""))
    if t is None:
        return {"ok": False, "why": f"シート「{r.get('tab')}」がありません"}
    name = str((r.get("cells") or {}).get("A") or "").strip()
    if not name:
        return {"ok": False, "why": "A列（商品名）が空です"}
    if any(_name_key(_row_name(t, i + 1)) == _name_key(name) for i in range(len(t["values"]))):
        return {"ok": False, "done": True, "why": f"「{name}」の行がもうあります（行を足さずに、空のセルを埋めてください）"}
    tr, n1 = _find_row(t, int(r.get("template_row") or 0), r.get("template_name", ""))
    ar, n2 = _find_row(t, int(r.get("after_row") or 0), r.get("after_name", ""))
    if tr is None:
        return {"ok": False, "why": "お手本の行：" + n1}
    if ar is None:
        return {"ok": False, "why": "足す場所：" + n2}
    risk = _indirect_risk(d, r["tab"])
    if risk:
        return {"ok": False, "why": risk + "。手で足してください"}
    cells = r.get("cells") or {}
    try:
        width = max([len(t["values"][tr - 1])] + [_colno(k) for k in cells])
    except Exception:
        return {"ok": False, "why": "列の文字が読めません：" + "、".join(cells)}
    fm = t["formulas"][tr - 1] if tr - 1 < len(t["formulas"]) else []
    skipped = [_col(c + 1) for c in range(len(fm)) if str(fm[c]).startswith("=")]
    bad = [k for k in cells if k.upper() in skipped and str(cells[k]).strip()]
    if bad:
        return {"ok": False, "why": "お手本の行で数式になっている列に値があります：" + "、".join(bad)}
    vals = [_real(cells.get(_col(c + 1), "")) for c in range(width)]
    return {"ok": True, "why": "", "after_row": ar, "template_row": tr,
            "note": "／".join(x for x in (n1, n2) if x), "values": vals, "skipped": skipped}


def analyze_new(api_key: str, parts: list, docs: dict, file_name: str,
                today: datetime.date = None, instruction: str = "") -> dict:
    """新しい商品の概要 → 商品詳細に足す行の案（rows）と、空のセルを埋める案（edits）。"""
    import google.generativeai as genai
    import gemini_key
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(gemini_key.MODEL)
    today = today or datetime.date.today()
    small = {file_name: docs[file_name]}
    prompt = NEW_PROMPT.format(today=today.isoformat(), nl=NL, docs=dump_docs(small))
    tail = ["# 担当者からの指示（いちばん優先）\n" + str(instruction).strip()] if str(instruction or "").strip() else []
    res = model.generate_content(
        [prompt, "# 新しい商品の概要（ここから）", *parts, "# 概要（ここまで）", *tail],
        generation_config={"response_mime_type": "application/json", "temperature": 0.1})
    txt = re.sub(r"^```(?:json)?|```$", "", (res.text or "").strip()).strip()
    out = json.loads(txt)
    for k in ("rows", "edits", "manual"):
        out.setdefault(k, [])
    d = docs[file_name]
    rows = []
    for r in out["rows"]:
        t = d["tabs"].get(str(r.get("tab") or ""))
        try:
            tr, ar = int(r.get("template_row") or 0), int(r.get("after_row") or 0)
        except Exception:
            tr = ar = 0
        cells = {str(k).strip().upper(): str(v or "") for k, v in (r.get("cells") or {}).items()
                 if re.fullmatch(r"[A-Za-z]{1,3}", str(k).strip())}
        rows.append({"id": uuid.uuid4().hex[:8], "file": file_name, "tab": str(r.get("tab") or ""),
                     "template_row": tr, "after_row": ar,
                     "template_name": _row_name(t, tr) if t and tr else "",
                     "after_name": _row_name(t, ar) if t and ar else "",
                     "target": str(r.get("target") or ""), "reason": str(r.get("reason") or ""),
                     "cells": cells})
    out["rows"] = rows
    for e in out["edits"]:
        e["id"] = uuid.uuid4().hex[:8]
        for k in ("file", "tab", "cell", "slide", "target", "old", "new", "reason"):
            e[k] = str(e.get(k) or "")
        e["file"] = e["file"] or file_name
    settle(docs, out["edits"])
    out["keywords"], out["looked"] = [], [f"{file_name}：シート {len(d['tabs'])}枚（全部）"]
    return out


def row_url(docs: dict, r: dict, row: int) -> str:
    return place_url(docs, {"file": r.get("file"), "tab": r.get("tab"), "cell": f"A{row}"}) if row else ""


def apply_rows(gc, sa_json: str, files: list, rows: list) -> list:
    """新しい行を足す。読み直して check_row が通ったものだけ。同じシートは下の行から足す（上の行番号をずらさないため）。
    お手本の行の書式・入力規則を写してから、値を RAW で書く（電話番号の0を落とさない）。"""
    if not rows:
        return []
    names = {r["file"] for r in rows}
    docs, ng = read_docs(gc, sa_json, [f for f in files if f.get("name") in names])
    ngd = dict(ng)
    out, todo = [], []
    for r in rows:
        if r["file"] in ngd:
            out.append({"id": r["id"], "mark": "🛑", "why": f"読めませんでした：{ngd[r['file']]}"})
            continue
        ck = check_row(docs, r)
        if ck.get("done"):
            out.append({"id": r["id"], "mark": "⏭", "why": ck["why"]})
        elif not ck["ok"]:
            out.append({"id": r["id"], "mark": "⚠️", "why": ck["why"] + "（触っていません）"})
        else:
            todo.append((r, ck))
    todo.sort(key=lambda x: (x[0]["file"], x[0]["tab"], -x[1]["after_row"]))
    for r, ck in todo:
        d = docs[r["file"]]
        try:
            sh = _open(gc, d["url"])
            gid = d["gids"][r["tab"]]
            at = ck["after_row"]                  # 0始まりで at の位置＝after_row のすぐ下
            src = ck["template_row"] - 1 + (1 if ck["template_row"] > at else 0)
            width = len(ck["values"])
            reqs = [{"insertDimension": {"range": {"sheetId": gid, "dimension": "ROWS",
                                                   "startIndex": at, "endIndex": at + 1},
                                         "inheritFromBefore": True}}]
            for pt in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
                reqs.append({"copyPaste": {
                    "source": {"sheetId": gid, "startRowIndex": src, "endRowIndex": src + 1},
                    "destination": {"sheetId": gid, "startRowIndex": at, "endRowIndex": at + 1},
                    "pasteType": pt}})
            sh.batch_update({"requests": reqs})
            row = at + 1
            sh.worksheet(r["tab"]).update(f"A{row}:{_col(width)}{row}", [ck["values"]], value_input_option="RAW")
            out.append({"id": r["id"], "mark": "✅", "why": ck.get("note", ""), "row": row,
                        "values": ck["values"], "at": now_str()})
        except Exception as ex:
            out.append({"id": r["id"], "mark": "🛑", "why": explain_error(ex)})
    order = {r["id"]: i for i, r in enumerate(rows)}
    return sorted(out, key=lambda o: order.get(o["id"], 0))


def undo_rows(gc, sa_json: str, files: list, rows: list, results: list) -> list:
    """足した行を消す。⚠️ その行の中身が、足したときのままのときだけ（人が書き足していたら消さない）。"""
    res = {x["id"]: x for x in results if x.get("mark") == "✅" and x.get("values")}
    rows = [r for r in rows if r["id"] in res]
    if not rows:
        return []
    docs, ng = read_docs(gc, sa_json, [f for f in files if f.get("name") in {r["file"] for r in rows}])
    ngd = dict(ng)
    out, todo = [], []
    same = lambda a, b: [_norm(x).strip() for x in a] + [""] * max(0, len(b) - len(a)) == \
        [_norm(x).strip() for x in b] + [""] * max(0, len(a) - len(b))
    for r in rows:
        if r["file"] in ngd:
            out.append({"id": r["id"], "mark": "🛑", "why": f"読めませんでした：{ngd[r['file']]}"})
            continue
        t = docs[r["file"]]["tabs"].get(r["tab"])
        vals = res[r["id"]]["values"]
        hits = [i + 1 for i, row in enumerate((t or {}).get("values") or []) if same([str(v) for v in row], vals)]
        if len(hits) == 1:
            todo.append((r, hits[0]))
        else:
            why = "足した行が見つかりません（消された・書き足された）" if not hits else f"同じ中身の行が{len(hits)}つあります"
            out.append({"id": r["id"], "mark": "⚠️", "why": why + "（触っていません）"})
    todo.sort(key=lambda x: (x[0]["file"], x[0]["tab"], -x[1]))
    for r, row in todo:
        d = docs[r["file"]]
        try:
            _open(gc, d["url"]).batch_update({"requests": [{"deleteDimension": {"range": {
                "sheetId": d["gids"][r["tab"]], "dimension": "ROWS", "startIndex": row - 1, "endIndex": row}}}]})
            out.append({"id": r["id"], "mark": "✅", "why": f"{row}行目を消しました"})
        except Exception as ex:
            out.append({"id": r["id"], "mark": "🛑", "why": explain_error(ex)})
    return out


# ==========================================
# 🎤 トークスクリプトに新しい商品のスライドを足す（お手本の商品のスライドを写して、文を書き換える）
# ==========================================
SLIDE_PROMPT = """あなたは、電気・ガスの取次をしている会社の事務担当です。
営業のトークスクリプト（スライド）に、新しい商品「{name}」のページを作ります。
「お手本の商品」のスライドをそのまま写し、その中の文を「{name}」向けに書き換える案を出してください。

# 決まり
- 書き換えるのは、新しい商品の資料に**はっきり書いてあること**だけ（会社名・商品名・プラン名・エリア・支払い方法・
  手続きの流れ・注意事項など）。資料に無いこと（料金の説明・切り返しなど）は書き換えず、notes に「確かめてほしいこと」として書く。
- お手本の商品に特有で、新しい商品には当てはまらない文（お手本の商品だけのキャンペーンなど）は、消す（"new" を空）か notes に書く。
- 1つの書き換え＝1件。"slide" はスライドID、"old" はそのスライドの**今の文の一部をそのまま写したもの**（1文字も変えない）。
- 会社名・商品名のように、同じ言葉がスライドに何回も出てきて全部変えるときは "all": true にする（そのときの "old" は改行を含めない）。
  それ以外は "all": false で、"old" はそのスライドの中で1回だけ出てくる長さにする（改行は「{nl}」）。
- 前後の書き方（敬語・全角半角・記号）はお手本に合わせる。
- 「言い回しの参考」があれば、その商品の似た説明の言い方をまねてよい。
- 「担当者からの指示」があれば、それがいちばん優先。

# 出力（JSONだけ）
{{
  "edits": [ {{"slide": "スライドID", "old": "今の文", "new": "新しい文", "all": false, "reason": "資料のどこに基づくか"}} ],
  "notes": [ "人が確かめてほしいこと（お手本の文のまま残っている説明など）" ]
}}

# お手本の商品のスライド（これを写します）
{block}
{hint}
"""


def _slide_dump(slides: list) -> str:
    out = []
    for s in slides:
        out.append(f"### スライド{s['no']}（スライドID={s['id']}）")
        for k, t in enumerate(s["texts"], 1):
            out.append(f"[{k}] " + t.rstrip("\n").replace("\v", NL).replace("\n", NL))
    return "\n".join(out)


def find_blocks(slides: list, name: str) -> list:
    """name が出てくる、続いたスライドのまとまり → [(始めの番号, 終わりの番号)]（長い順）"""
    key = _norm_kw(name)
    if not key:
        return []
    hit = [s["no"] for s in slides if key in _norm_kw(" ".join(s["texts"]))]
    runs = []
    for n in hit:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return sorted([tuple(r) for r in runs], key=lambda r: -(r[1] - r[0]))


def _one(s: dict) -> dict:
    """1枚のスライドだけの docs（locate を使い回すため）"""
    return {"f": {"kind": "slides", "slides": [copy.deepcopy(s)]}}


def preview_slide(s: dict, edits: list) -> tuple:
    """お手本のスライドに、選んだ書き換えを当てた結果。→ (書き換えたあとの文のリスト, {編集ID: (OK, 理由, 何か所)})
    ⭐ 足すときと同じ順（1か所ずつの書き換え → まとめて置き換え）で当てる。"""
    d = _one(s)
    chk = {}
    for e in [x for x in edits if not x.get("all")]:
        e2 = {**e, "file": "f", "slide": s["id"]}
        lc = locate(d, e2)
        chk[e["id"]] = (bool(lc.get("ok")), lc.get("why", ""), 1 if lc.get("ok") else 0)
        if lc.get("ok"):
            _remember(d, e2, lc)
    sl = d["f"]["slides"][0]
    for e in [x for x in edits if x.get("all")]:
        old, new = _real(e.get("old")), _real(e.get("new"))
        if not old or "\n" in old:
            chk[e["id"]] = (False, "まとめて置き換えるときは、前の文に改行を入れられません", 0)
            continue
        n = sum(it["text"].count(old) for it in sl["items"])
        chk[e["id"]] = (n > 0, "" if n else "この文がスライドにありません", n)
        for it in sl["items"]:
            it["text"] = it["text"].replace(old, new)
    sl["texts"] = [x["text"] for x in sl["items"]]
    return sl["texts"], chk


def analyze_slides(api_key: str, parts: list, block: list, name: str,
                   hint: list = None, instruction: str = "") -> dict:
    """お手本のスライド（block）を、新しい商品 name 向けに書き換える案。→ {"edits": [...], "notes": [...]}"""
    import google.generativeai as genai
    import gemini_key
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(gemini_key.MODEL)
    h = ("\n# 言い回しの参考（写しません。言い方だけまねてよい）\n" + _slide_dump(hint)) if hint else ""
    prompt = SLIDE_PROMPT.format(name=name, nl=NL, block=_slide_dump(block), hint=h)
    tail = ["# 担当者からの指示（いちばん優先）\n" + str(instruction).strip()] if str(instruction or "").strip() else []
    res = model.generate_content(
        [prompt, "# 新しい商品の資料（ここから）", *parts, "# 資料（ここまで）", *tail],
        generation_config={"response_mime_type": "application/json", "temperature": 0.1})
    out = json.loads(re.sub(r"^```(?:json)?|```$", "", (res.text or "").strip()).strip())
    ids = {s["id"] for s in block}
    edits = []
    for e in out.get("edits") or []:
        e = {k: str(e.get(k) or "") for k in ("slide", "old", "new", "reason")} | {"all": bool(e.get("all"))}
        if e["slide"] not in ids or not e["old"]:
            continue
        e["id"] = uuid.uuid4().hex[:8]
        if not e["all"]:
            _one_paragraph(e)
            d = _one(next(s for s in block if s["id"] == e["slide"]))
            lc = locate(d, {**e, "file": "f"})
            if lc.get("ok") and lc.get("old") is not None:
                e["old"] = lc["old"].replace("\v", NL).replace("\n", NL)
        edits.append(e)
    return {"edits": edits, "notes": [str(x) for x in out.get("notes") or []]}


def new_slide_block(file: str, block: list, edits: list, name: str) -> dict:
    return {"id": uuid.uuid4().hex[:8], "file": file, "name": name,
            "src": [s["id"] for s in block], "src_nos": [s["no"] for s in block],
            "src_texts": {s["id"]: s["texts"] for s in block}, "edits": copy.deepcopy(edits)}


def _same_texts(a: list, b: list) -> bool:
    return [_norm(x).strip() for x in a] == [_norm(x).strip() for x in b]


def apply_slide_blocks(sa_json: str, files: list, blocks: list) -> list:
    """お手本のスライドを複製して、まとまりのすぐ後ろに並べ、書き換えを当てる。
    ⚠️ お手本のスライドが、案を作ったときと変わっていたら（その間に人が直した）足さない。"""
    out = []
    if not blocks:
        return out
    svc = slides_service(sa_json)
    urls = {f["name"]: f["url"] for f in files}
    for b in blocks:
        url = urls.get(b["file"])
        if not url:
            out.append({"id": b["id"], "mark": "🛑", "why": f"ファイル「{b['file']}」が設定にありません"})
            continue
        made = []
        try:
            deck = read_slides(svc, url)
            pos = {s["id"]: i for i, s in enumerate(deck["slides"])}
            if any(x not in pos for x in b["src"]):
                out.append({"id": b["id"], "mark": "⚠️", "why": "お手本のスライドが見つかりません（消された）（触っていません）"})
                continue
            idx = [pos[x] for x in b["src"]]
            if idx != list(range(idx[0], idx[0] + len(idx))):
                out.append({"id": b["id"], "mark": "⚠️", "why": "お手本のスライドが続いて並んでいません（並べ替えられた）（触っていません）"})
                continue
            src = [deck["slides"][i] for i in idx]
            changed = [s["no"] for s in src if not _same_texts(s["texts"], b["src_texts"].get(s["id"]) or [])]
            if changed:
                out.append({"id": b["id"], "mark": "⚠️", "why": f"お手本のスライド（{changed}枚目）が、案を作ったあとに直されています。案を作り直してください（触っていません）"})
                continue
            ids = {s["id"]: "enk_" + uuid.uuid4().hex[:16] for s in src}
            reqs = [{"duplicateObject": {"objectId": s["id"], "objectIds": {s["id"]: ids[s["id"]]}}} for s in src]
            # 複製は元のすぐ後ろにできる（元1・写し1・元2・写し2…）ので、写しをまとまりの後ろへまとめて移す
            reqs.append({"updateSlidesPosition": {"slideObjectIds": [ids[s["id"]] for s in src],
                                                  "insertionIndex": idx[0] + 2 * len(src)}})
            svc.presentations().batchUpdate(presentationId=slides_id(url), body={"requests": reqs}).execute()
            made = [ids[s["id"]] for s in src]
            deck = read_slides(svc, url)
            byid = {s["id"]: s for s in deck["slides"]}
            ng = []
            for s in src:
                new = byid[ids[s["id"]]]
                d = {"f": {"kind": "slides", "slides": [new]}}
                eds = [e for e in b["edits"] if e["slide"] == s["id"]]
                reqs = []
                for e in [x for x in eds if not x.get("all")]:
                    e2 = {**e, "file": "f", "slide": new["id"]}
                    lc = locate(d, e2)
                    if not lc.get("ok"):
                        ng.append(f"スライド{new['no']}：{_real(e['old'])[:20]}…（{lc.get('why')}）")
                        continue
                    reqs += _slide_requests(d["f"], e2, lc)
                    _remember(d, e2, lc)
                if reqs:
                    svc.presentations().batchUpdate(presentationId=slides_id(url), body={"requests": reqs}).execute()
                reqs = [{"replaceAllText": {"containsText": {"text": _real(e["old"]), "matchCase": True},
                                            "replaceText": _real(e["new"]), "pageObjectIds": [new["id"]]}}
                        for e in eds if e.get("all") and _real(e["old"])]
                if reqs:
                    svc.presentations().batchUpdate(presentationId=slides_id(url), body={"requests": reqs}).execute()
            deck = read_slides(svc, url)
            byid = {s["id"]: s for s in deck["slides"]}
            nos = [byid[x]["no"] for x in made if x in byid]
            out.append({"id": b["id"], "mark": "⚠️" if ng else "✅", "slides": made, "at": now_str(),
                        "texts": {x: byid[x]["texts"] for x in made if x in byid},
                        "why": (f"{nos[0]}〜{nos[-1]}枚目に足しました" if nos else "")
                               + ("。書き換えられなかった所：" + "／".join(ng) if ng else "")})
        except Exception as ex:
            out.append({"id": b["id"], "mark": "🛑", "slides": made,
                        "why": ("スライドは足しましたが、書き換えの途中で止まりました：" if made else "") + explain_error(ex, "slides")})
    return out


def undo_slide_blocks(sa_json: str, files: list, blocks: list, results: list) -> list:
    """足したスライドを消す。⚠️ 足したときの文のままのスライドだけ（人が書き足していたら消さない）。"""
    res = {r["id"]: r for r in results if r.get("slides")}
    urls = {f["name"]: f["url"] for f in files}
    out = []
    svc = None
    for b in blocks:
        r = res.get(b["id"])
        if not r:
            continue
        try:
            svc = svc or slides_service(sa_json)
            url = urls[b["file"]]
            byid = {s["id"]: s for s in read_slides(svc, url)["slides"]}
            gone, keep, dels = [], [], []
            for x in r["slides"]:
                if x not in byid:
                    gone.append(x)
                elif r.get("texts") and not _same_texts(byid[x]["texts"], r["texts"].get(x) or []):
                    keep.append(byid[x]["no"])
                else:
                    dels.append({"deleteObject": {"objectId": x}})
            if dels:
                svc.presentations().batchUpdate(presentationId=slides_id(url), body={"requests": dels}).execute()
            why = f"スライドを{len(dels)}枚消しました" + (f"（{keep}枚目は、あとから直されていたので残しました）" if keep else "")
            out.append({"id": b["id"], "mark": "⚠️" if keep else "✅", "why": why})
        except Exception as ex:
            out.append({"id": b["id"], "mark": "🛑", "why": explain_error(ex, "slides")})
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


def undo_change(gc, sa_json: str, files: list, change: dict) -> list:
    """元に戻す：足した行を先に消し（行番号をもとに戻す）、そのあとセルの直しを戻す。"""
    res = change.get("results") or []
    return (undo_slide_blocks(sa_json, files, change.get("slides") or [], res)
            + undo_rows(gc, sa_json, files, change.get("rows") or [], res)
            + undo_edits(gc, sa_json, files, change.get("edits") or [], res))


def result_lines(change: dict) -> list:
    eds = {e["id"]: e for e in change.get("edits") or []}
    rws = {r["id"]: r for r in change.get("rows") or []}
    sls = {b["id"]: b for b in change.get("slides") or []}
    out = []
    for r in change.get("results") or []:
        if r["id"] in sls:
            b = sls[r["id"]]
            out.append(f"{r['mark']} {b['file']}：「{b.get('name', '')}」のスライドを足す（お手本 {b['src_nos'][0]}〜{b['src_nos'][-1]}枚目）"
                       + (f"（{r['why']}）" if r.get("why") else ""))
            continue
        if r["id"] in rws:
            w = rws[r["id"]]
            at = f"（{r['row']}行目）" if r.get("row") else ""
            out.append(f"{r['mark']} {w['file']}／{w['tab']}：「{(w.get('cells') or {}).get('A', '')}」の行を足す{at}"
                       + (f"（{r['why']}）" if r.get("why") else ""))
            continue
        e = eds.get(r["id"], {})
        where = f"{e.get('file', '')}／{e.get('tab') or 'スライド'}{('!' + e['cell']) if e.get('cell') else ''}"
        out.append(f"{r['mark']} {where}：{_real(e.get('old'))} → {_real(e.get('new'))}"
                   + (f"（{r['why']}）" if r.get("why") else ""))
    return out


def apply_change(supabase, gc, sa_json: str, cfg: dict, change: dict) -> dict:
    """1件の変更（人が選んだ直し）を書き込み、記録する。→ 書いたあとの change"""
    # ⭐ セルの直しを先に（行を足すと、その下のセル番地がずれるため）
    res = (apply_edits(gc, sa_json, cfg.get("files") or [], change.get("edits") or [])
           + apply_rows(gc, sa_json, cfg.get("files") or [], change.get("rows") or [])
           + apply_slide_blocks(sa_json, cfg.get("files") or [], change.get("slides") or []))
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


def new_change(proposal: dict, edits: list, apply_on: str, source: str, rows: list = None, slides: list = None) -> dict:
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
        "rows": copy.deepcopy(rows or []),
        "slides": copy.deepcopy(slides or []),
        "manual": copy.deepcopy(proposal.get("manual") or []),
        "results": [],
    }
