"""
✉️ 39メール（契約のあとに送るご案内メール）の中身（画面は pages/14_✉️_39メール.py）

【困っていたこと】
  これまではスプシのGASが下書きを作っていた。文面はシートに書いてあったが、
  **どの段落を出すかは GAS のコードに「SB光の9行目はファミリーのときだけ」と行番号で書いてあった**。
  そのため、商品や料金が変わって文面シートに1行足すと番号がずれ、コードを直せる人しか変えられなかった。

【この作りの考え方】
  ⭐ **文面・出す条件・どの文面を使うかは、全部アプリ（Supabase）に持つ**（担当者 2026-10-04：「アプリに移す」）。
     段落ごとに「出す条件」を書く＝行番号に頼らない。商品が増えても、画面で文面を1つ足すだけ。
  ⭐ 条件は **BOXの見出しの名前** で書く（列の位置に頼らない＝レポートの列が増えてもずれない）。
  ⭐ 下書きは **Gmail API** で直接入れる（GASを通さない。小窓 `Browser.msgBox` でアプリから呼べなかったため）。
     許可は通録のDriveと同じ鍵（`GOOGLE_OAUTH_CLIENT_JSON`）で、**別の許可**を1回だけもらう。
  ⭐ 作った下書きの記録と、DC担当者・完了のチェックは Supabase（`__mail39_log__:<セット>`）。
     スプシの「確認用 → DC用」に手で写していた作業の代わり。
  ⚠️ 前に GAS で作った分（確認用／DC用／「送信履歴」）も読んで、同じ案件・同じ文面は作らない（切り替えの日に二重に作らない）。

⚠️ Streamlit を import しない（画面以外からも使えるように）。
"""
import base64
import copy
import datetime
import html as _html
import json
import os
import re
import time
import unicodedata
import uuid

ROW = "__mail39__"
LOG_PREFIX = "__mail39_log__:"
KEEP_DAYS = 120          # 記録を残す日数

# 🔑 Gmailに下書きを入れる・差出人を確かめる・画像（Drive）を読む
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.settings.basic",
                "https://www.googleapis.com/auth/drive.readonly"]

HEAD = "（冒頭）"
FOOT = "（末尾）"
TPL_COL = "文面"            # 条件で使える「選ばれた文面の名前」
CB_KEY = "キャッシュバック額"  # RCB→CB の順で拾い、3桁区切りにした金額
OPS = ["含む", "含まない", "＝", "≠", "空", "空でない"]
NO_VALUE_OPS = {"空", "空でない"}

DEFAULT_SETS = {
    "ネット": {
        "sheet_url": "",
        "box_tab": "BOX",
        "refresh_robot": "共通_SFコネクタ更新",
        "refresh_tabs": ["BOX"],
        "from_addr": "info_cc@lifeap.co.jp",
        "email_col": "メールアドレス",
        "case_col": "案件番号",
        "name_tpl": "{名前（姓）} {名前（名）}",
        "staff_cols": ["ネット契約担当者"],
        "staff_default": "担当者",
        "cb_cols": ["RCB金額", "CB金額"],
        # 前に GAS で作った分（列は1から数える）
        "legacy": {"tabs": ["確認用", "DC用", "「送信履歴」"], "case_col": 5, "tpl_col": 2},
        "routes": [],
        "templates": {},
    },
    "LL": {
        "sheet_url": "",
        "box_tab": "BOX",
        "refresh_robot": "共通_SFコネクタ更新",
        "refresh_tabs": ["BOX"],
        "from_addr": "info_cc@lifeap.co.jp",
        "email_col": "メールアドレス",
        "case_col": "案件番号",
        "routes": [],
        "templates": {},
    },
}


def today() -> str:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d")


def now_stamp() -> str:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")


def new_uid() -> str:
    return uuid.uuid4().hex[:10]


# ==========================================
# 💾 設定
# ==========================================
def load_cfg(supabase) -> dict:
    try:
        res = supabase.table("merchants").select("config_json").eq("id", ROW).execute()
        cfg = (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception:
        cfg = {}
    sets = cfg.get("sets") or {}
    for name, base in DEFAULT_SETS.items():
        merged = copy.deepcopy(base)
        merged.update(sets.get(name) or {})
        sets[name] = merged
    cfg["sets"] = sets
    return cfg


def save_cfg(supabase, part: dict, set_name: str = None):
    """⚠️ 書く直前に読み直して、渡したところだけ変える（別のPCの変更を消さないため）。

    set_name を渡すと、part はそのセットの中身（触ったキーだけ）。None を渡したキーは消す。
    """
    res = supabase.table("merchants").select("config_json").eq("id", ROW).execute()
    latest = (res.data[0].get("config_json") or {}) if res.data else {}
    if set_name:
        sets = latest.get("sets") or {}
        cur = sets.get(set_name) or {}
        for k, v in part.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        sets[set_name] = cur
        latest["sets"] = sets
    else:
        for k, v in part.items():
            if v is None:
                latest.pop(k, None)
            else:
                latest[k] = v
    supabase.table("merchants").upsert({
        "id": ROW, "name": "（39メールの設定）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return latest


def save_template(supabase, set_name: str, name: str, tpl: dict, old_name: str = None):
    """文面を1つだけ差し替える（ほかの文面は、読み直した最新のまま）。

    old_name を渡すと名前の付け替え（振り分けの行の名前も付け替える）。tpl=None で消す。
    """
    res = supabase.table("merchants").select("config_json").eq("id", ROW).execute()
    latest = (res.data[0].get("config_json") or {}) if res.data else {}
    sets = latest.get("sets") or {}
    cur = sets.get(set_name) or copy.deepcopy(DEFAULT_SETS.get(set_name, {}))
    tpls = dict(cur.get("templates") or {})
    if old_name and old_name != name:
        tpls.pop(old_name, None)
        for r in cur.get("routes") or []:
            if r.get("template") == old_name:
                r["template"] = name
    if tpl is None:
        tpls.pop(name, None)
    else:
        tpls[name] = tpl
    cur["templates"] = tpls
    sets[set_name] = cur
    latest["sets"] = sets
    supabase.table("merchants").upsert({
        "id": ROW, "name": "（39メールの設定）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()


# ==========================================
# 🔎 条件
# ==========================================
def norm(v) -> str:
    return unicodedata.normalize("NFKC", str(v if v is not None else "")).strip()


def _vals(v) -> list:
    return [x.strip() for x in norm(v).split(",") if x.strip()]


def check(cond: dict, row: dict) -> bool:
    """1つの条件。値は NFKC で比べる（全角半角のゆれを吸収）。＝ は大文字小文字を問わない。"""
    col = str(cond.get("列", "") or "").strip()
    op = str(cond.get("op", "") or "").strip()
    cell = norm(row.get(col, ""))
    vals = _vals(cond.get("値", ""))
    if op == "空":
        return cell == ""
    if op == "空でない":
        return cell != ""
    if op == "含む":
        return any(v in cell for v in vals)
    if op == "含まない":
        return not any(v in cell for v in vals)
    if op == "＝":
        return any(cell.casefold() == v.casefold() for v in vals)
    if op == "≠":
        return not any(cell.casefold() == v.casefold() for v in vals)
    return False      # ⚠️ 分からない条件は「合わない」（黙って出さないほうが安全）


def match(conds, row: dict) -> bool:
    """全部の条件に合えば True（条件が無ければ常に出す）。"""
    return all(check(c, row) for c in (conds or []) if str(c.get("列", "") or "").strip())


def describe(conds) -> str:
    """条件を日本語の1行にする（画面の見出し用）。"""
    parts = []
    for c in conds or []:
        col, op, v = str(c.get("列", "")).strip(), str(c.get("op", "")).strip(), str(c.get("値", "")).strip()
        if not col:
            continue
        vv = "・".join(_vals(v))
        parts.append({
            "含む": f"{col}に「{vv}」を含む",
            "含まない": f"{col}に「{vv}」を含まない",
            "＝": f"{col}が「{vv}」",
            "≠": f"{col}が「{vv}」ではない",
            "空": f"{col}が空",
            "空でない": f"{col}が入っている",
        }.get(op, f"{col} {op} {vv}"))
    return "　かつ　".join(parts) if parts else "いつも出す"


# ==========================================
# ✍️ 文面の書き方（マーク）→ HTML
# ==========================================
#   {太字}太字{/太字}   {色:#ff0000}赤い文字{/色}   {目立つ}黄色の帯{/目立つ}
#   [見える文字](https://…)                         {画像:DriveのファイルID}
#   {列名}  → お客様の値（BOXの見出しの名前。ほかに {お客様名} {担当者} {キャッシュバック額} {文面}）
MARK_HELP = ("{太字}太字{/太字} ／ {色:#ff0000}色つき{/色} ／ {目立つ}黄色の帯{/目立つ} ／ "
             "[見える文字](https://…) ／ {画像:DriveのファイルID} ／ {列名}＝お客様の値")
_HILITE = '<span style="font-size: 1.2em; background-color: yellow; font-weight: bold;">'
_MARK_WORDS = {"目立つ", "/目立つ", "/色", "太字", "/太字"}


def placeholders(text: str) -> list:
    out = []
    for m in re.finditer(r"\{([^{}\n]+)\}", str(text or "")):
        k = m.group(1)
        if k in _MARK_WORDS or k.startswith("色:") or k.startswith("画像:"):
            continue
        out.append(k)
    return out


def to_html(text: str, values: dict, images: list = None, missing: list = None) -> str:
    """マークつきの文を HTML にする。{列名} はお客様の値で埋める（値はエスケープする）。

    images に画像のIDを足していく（下書きに埋め込むため）。埋まらない {…} は missing に足す。
    """
    s = _html.escape(str(text or ""), quote=False)
    tokens = {}

    def _keep(h: str) -> str:
        k = f"\x00{len(tokens)}\x00"
        tokens[k] = h
        return k

    def _ph(m):
        k = m.group(1)
        if k == "目立つ":
            return _keep(_HILITE)
        if k == "太字":
            return _keep("<b>")
        if k == "/太字":
            return _keep("</b>")
        if k in ("/目立つ", "/色"):
            return _keep("</span>")
        if k.startswith("色:"):
            return _keep(f'<span style="color:{_html.escape(k[2:].strip(), quote=True)};">')
        if k.startswith("画像:"):
            fid = k[3:].strip()
            if images is not None:
                images.append(fid)
                return _keep(f'<img src="cid:img{len(images)}" width="550">')
            return ""
        key = _html.unescape(k)
        if key in values:
            return _keep(_html.escape(str(values.get(key) if values.get(key) is not None else ""),
                                      quote=False).replace("\n", "<br>"))
        if missing is not None:
            missing.append(key)
        return m.group(0)

    s = re.sub(r"\{([^{}\n]+)\}", _ph, s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
               lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = s.replace("\r\n", "\n").replace("\n", "<br>")
    for k, v in tokens.items():
        s = s.replace(k, v)
    return s


def to_plain(text: str, values: dict) -> str:
    """プレビュー・AIに渡す用。マークを外した文。"""
    s = re.sub(r"\{(色:[^}]*|/色|目立つ|/目立つ|太字|/太字|画像:[^}]*)\}", "", str(text or ""))
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1（\2）", s)
    return re.sub(r"\{([^{}\n]+)\}",
                  lambda m: str(values.get(m.group(1), m.group(0))), s)


# ==========================================
# 📄 BOX（お客様の一覧）を読む
# ==========================================
def _open(gc, url: str):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def unique_head(head) -> list:
    """同じ見出しが2つあれば2つ目に（2）を付ける（「名義人同意」が2列ある）。"""
    out, seen = [], {}
    for h in head:
        h = str(h or "").strip()
        n = seen.get(h, 0)
        seen[h] = n + 1
        out.append(h if n == 0 or not h else f"{h}（{n + 1}）")
    return out


def cb_amount(row: dict, cols) -> str:
    """キャッシュバックの金額：先に書いた列（RCB）が入っていればそれ、無ければ次の列。数字だけ取り出して3桁区切り。"""
    raw = ""
    for c in cols or []:
        v = str(row.get(c, "") or "").strip()
        if v:
            raw = v
            break
    s = unicodedata.normalize("NFKC", raw)
    digits = re.sub(r"[^0-9]", "", s)
    if not digits:
        return ""
    n = int(digits) * (10000 if "万" in s else 1)
    return f"{n:,}"


def staff_of(row: dict, set_cfg: dict) -> str:
    for c in set_cfg.get("staff_cols") or []:
        v = str(row.get(c, "") or "").strip()
        if v and v != "新規":
            return v
    return str(set_cfg.get("staff_default", "") or "担当者")


def enrich(row: dict, set_cfg: dict) -> dict:
    """BOXの1行に、文面で使う値（お客様名・担当者・CB金額）を足す。"""
    r = dict(row)
    tpl = str(set_cfg.get("name_tpl", "") or "{名前（姓）} {名前（名）}")
    r.setdefault("お客様名", re.sub(r"\{([^{}]+)\}", lambda m: str(row.get(m.group(1), "")), tpl).strip())
    r.setdefault("担当者", staff_of(row, set_cfg))
    # ⚠️ BOXにも「CB金額」の列があるので、名前を分ける（同じ名前だと列の値がそのまま入る）
    r[CB_KEY] = cb_amount(row, set_cfg.get("cb_cols"))
    return r


def read_box(gc, set_cfg: dict) -> dict:
    """BOX と、前に GAS で作った記録を1回の問い合わせで読む。

    戻り値：{"head": [...], "rows": [{...}], "legacy": {"案件番号|文面", …}, "legacy_cases": {案件番号: [文面…]}}
    """
    url = str(set_cfg.get("sheet_url", "") or "").strip()
    box = str(set_cfg.get("box_tab", "") or "BOX")
    lg = set_cfg.get("legacy") or {}
    ltabs = [t for t in (lg.get("tabs") or []) if t]
    sh = _open(gc, url)
    have = {ws.title for ws in sh.worksheets()}
    ranges = [f"'{box}'!A1:ZZ"] + [f"'{t}'!A1:Z" for t in ltabs if t in have]
    res = sh.values_batch_get(ranges, params={"valueRenderOption": "FORMATTED_VALUE"})
    vrs = res.get("valueRanges", [])
    vals = (vrs[0].get("values", []) if vrs else []) or []
    head = unique_head(vals[0]) if vals else []
    rows = []
    for r in vals[1:]:
        r = list(r) + [""] * (len(head) - len(r))
        d = {h: str(r[i]) for i, h in enumerate(head) if h}
        if any(str(v).strip() for v in d.values()):
            rows.append(d)
    legacy, cases = set(), {}
    ci, ti = int(lg.get("case_col", 5) or 5) - 1, int(lg.get("tpl_col", 2) or 2) - 1
    for vr in vrs[1:]:
        for r in vr.get("values", []) or []:
            if len(r) > max(ci, ti):
                c, t = str(r[ci]).strip(), str(r[ti]).strip()
                if re.fullmatch(r"[A-Z]{2}\d{8}", c):
                    legacy.add(f"{c}|{t}")
                    cases.setdefault(c, []).append(t)
    return {"head": head, "rows": rows, "legacy": legacy, "legacy_cases": cases,
            "missing_tabs": [t for t in ltabs if t not in have]}


# ==========================================
# 🧩 文面を組み立てる
# ==========================================
def route(set_cfg: dict, row: dict) -> str:
    """どの文面を使うか（上から順に、最初に条件が合ったもの）。合わなければ空。"""
    for r in set_cfg.get("routes") or []:
        name = str(r.get("template", "") or "").strip()
        if name and match(r.get("when"), row):
            return name
    return ""


def compose(set_cfg: dict, row: dict, tpl_name: str = None) -> dict:
    """1人ぶんのメールを作る。

    戻り値：{"template", "subject", "html", "plain", "images", "blocks"(出した段落の番号), "missing", "error"}
    ⚠️ 埋まらない {…} があれば error にする（文字のまま送らない）。
    """
    r = enrich(row, set_cfg)
    name = tpl_name or route(set_cfg, r)
    out = {"template": name, "subject": "", "html": "", "plain": "", "images": [],
           "blocks": [], "missing": [], "error": ""}
    if not name:
        out["error"] = "どの文面を使うか決まりません（振り分けの条件に合いません）"
        return out
    tpls = set_cfg.get("templates") or {}
    tpl = tpls.get(name)
    if not tpl:
        out["error"] = f"文面「{name}」がありません"
        return out
    r[TPL_COL] = name
    images, missing, parts, plains = [], [], [], []
    sections = [(HEAD, tpls.get(HEAD) or {}), (name, tpl), (FOOT, tpls.get(FOOT) or {})]
    for sec, t in sections:
        for i, b in enumerate(t.get("blocks") or []):
            text = str(b.get("text", "") or "")
            if not text.strip() or not match(b.get("when"), r):
                continue
            parts.append(to_html(text, r, images, missing))
            plains.append(to_plain(text, r))
            if sec == name:
                out["blocks"].append(i + 1)
    subj_missing = []
    subject = to_plain(str(tpl.get("subject", "") or ""), r)
    subj_missing = [k for k in placeholders(tpl.get("subject", "")) if k not in r]
    out.update(subject=subject, html="<br><br>".join(parts), plain="\n\n".join(plains),
               images=images)
    missing = sorted(set(missing + subj_missing))
    out["missing"] = missing
    if missing:
        out["error"] = "埋まらない項目があります：" + "、".join("{" + m + "}" for m in missing)
    elif not subject.strip():
        out["error"] = f"文面「{name}」に件名がありません"
    return out


# ==========================================
# 📮 Gmail（下書きを入れる）
# ==========================================
def _token_path() -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EnkanAI")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "gmail_oauth_token.json")


def _save_token(supabase, text: str):
    import gas_deploy
    with open(_token_path(), "w", encoding="utf-8") as f:
        f.write(text)
    fn = gas_deploy._fernet()
    if fn and supabase is not None:
        try:
            save_cfg(supabase, {"token_enc": fn.encrypt(text.encode()).decode(),
                                "token_saved_at": now_stamp()})
        except Exception:
            pass


def gmail_creds(supabase):
    """保存してある許可（期限切れなら更新）。無ければ None。

    ⭐ Supabase にも暗号化して残す＝どのPCからでも同じGmailに下書きを入れられる。
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import gas_deploy
    raw = ""
    if supabase is not None:
        enc = load_cfg(supabase).get("token_enc", "")
        fn = gas_deploy._fernet()
        if enc and fn:
            try:
                raw = fn.decrypt(str(enc).encode()).decode()
            except Exception:
                raw = ""
    if not raw and os.path.isfile(_token_path()):
        try:
            raw = open(_token_path(), encoding="utf-8").read()
        except Exception:
            raw = ""
    if not raw:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), GMAIL_SCOPES)
    except Exception:
        return None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(supabase, creds.to_json())
        except Exception:
            return None
    return creds if creds.valid else None


def authorize_local(supabase, timeout_sec: int = 180):
    """このPCのブラウザを開いて、下書きを入れるGmailのアカウントで1回だけ許可をもらう。"""
    from google_auth_oauthlib.flow import InstalledAppFlow
    import gas_deploy
    cfg = gas_deploy.client_config()
    if not cfg:
        raise RuntimeError("つなぐための鍵（GOOGLE_OAUTH_CLIENT_JSON）がありません。"
                           "「GASをアプリが書き込む」と同じ鍵を使います")
    flow = InstalledAppFlow.from_client_config(cfg, GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  timeout_seconds=timeout_sec, authorization_prompt_message="",
                                  success_message="許可できました。この画面は閉じてください。")
    _save_token(supabase, creds.to_json())
    return creds


def forget(supabase):
    try:
        os.remove(_token_path())
    except Exception:
        pass
    save_cfg(supabase, {"token_enc": None, "token_saved_at": None})


def gmail(supabase):
    from googleapiclient.discovery import build
    creds = gmail_creds(supabase)
    if not creds:
        raise RuntimeError("Gmailの許可がありません（⚙️ 設定の「🔑 Gmailの許可を出す」）")
    return build("gmail", "v1", credentials=creds, cache_discovery=False), creds


def gmail_account(supabase) -> dict:
    """許可したアカウントと、差出人に使えるアドレス → {"email", "send_as": [...], "error"}。"""
    try:
        svc, _ = gmail(supabase)
        prof = svc.users().getProfile(userId="me").execute()
        try:
            sa = svc.users().settings().sendAs().list(userId="me").execute()
            send_as = [x.get("sendAsEmail", "") for x in sa.get("sendAs", [])
                       if x.get("verificationStatus", "accepted") == "accepted" or x.get("isPrimary")]
        except Exception:
            send_as = []
        return {"email": prof.get("emailAddress", ""), "send_as": send_as, "error": ""}
    except Exception as e:
        return {"email": "", "send_as": [], "error": str(e)[:200]}


def _image_bytes(creds, fid: str) -> tuple:
    from googleapiclient.discovery import build
    dv = build("drive", "v3", credentials=creds, cache_discovery=False)
    meta = dv.files().get(fileId=fid, fields="mimeType,name", supportsAllDrives=True).execute()
    data = dv.files().get_media(fileId=fid, supportsAllDrives=True).execute()
    return data, meta.get("mimeType", "image/png")


def build_mime(to: str, subject: str, html: str, from_addr: str = "", images=None, creds=None):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage
    from email.header import Header
    images = images or []
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    if images:
        msg = MIMEMultipart("related")
        msg.attach(alt)
        for i, fid in enumerate(images, 1):
            data, mime = _image_bytes(creds, fid)
            img = MIMEImage(data, _subtype=(mime.split("/")[-1] or "png"))
            img.add_header("Content-ID", f"<img{i}>")
            img.add_header("Content-Disposition", "inline", filename=f"img{i}")
            msg.attach(img)
    else:
        msg = alt
    msg["To"] = to
    if from_addr:
        msg["From"] = from_addr
    msg["Subject"] = Header(subject, "utf-8")
    return msg


def create_draft(svc, creds, to: str, subject: str, html: str, from_addr: str = "",
                 images=None) -> str:
    msg = build_mime(to, subject, html, from_addr, images, creds)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    d = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return d.get("id", "")


# ==========================================
# 📒 作った記録・DC（セットごとに1行）
# ==========================================
def log_key(case_no: str, tpl: str) -> str:
    return f"{case_no}|{tpl}"


def load_log(supabase, set_name: str) -> dict:
    res = supabase.table("merchants").select("config_json").eq("id", LOG_PREFIX + set_name).execute()
    cj = (res.data[0].get("config_json") or {}) if res.data else {}
    return cj.get("items") or {}


def update_log(supabase, set_name: str, changes: dict) -> dict:
    """⚠️ 読み直して、渡した案件だけ書き換える（同時に2人がDCを付けても消し合わない）。"""
    rid = LOG_PREFIX + set_name
    res = supabase.table("merchants").select("config_json").eq("id", rid).execute()
    cj = (res.data[0].get("config_json") or {}) if res.data else {}
    items = cj.get("items") or {}
    for k, v in changes.items():
        if v is None:
            items.pop(k, None)
        else:
            cur = items.get(k) or {}
            cur.update(v)
            items[k] = cur
    cut = (datetime.date.fromisoformat(today()) - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    items = {k: v for k, v in items.items()
             if str(v.get("made", "9999"))[:10] >= cut or not v.get("done")}
    supabase.table("merchants").upsert({
        "id": rid, "name": f"（39メールの記録：{set_name}）", "is_active": False,
        "connector_type": "settings", "config_json": {"set": set_name, "items": items}}).execute()
    return items


def make_drafts(supabase, set_name: str, set_cfg: dict, rows: list, log=print) -> dict:
    """選んだお客様の下書きを作る → {"ok": [...], "ng": [(案件番号, 理由)]}。

    ⭐ 1件作るたびに記録する（途中で止まっても、作った分は二重に作らない）。
    """
    svc, creds = gmail(supabase)
    from_addr = str(set_cfg.get("from_addr", "") or "").strip()
    email_col = set_cfg.get("email_col", "メールアドレス")
    case_col = set_cfg.get("case_col", "案件番号")
    ok, ng = [], []
    for row in rows:
        case_no = str(row.get(case_col, "") or "").strip()
        to = str(row.get(email_col, "") or "").strip()
        if "@" not in to:
            ng.append((case_no, "メールアドレスがありません"))
            continue
        m = compose(set_cfg, row)
        if m["error"]:
            ng.append((case_no, m["error"]))
            continue
        try:
            did = create_draft(svc, creds, to, m["subject"], m["html"], from_addr, m["images"])
        except Exception as e:
            ng.append((case_no, f"下書きを作れませんでした：{str(e)[:150]}"))
            continue
        r = enrich(row, set_cfg)
        update_log(supabase, set_name, {log_key(case_no, m["template"]): {
            "case": case_no, "email": to, "name": r.get("お客様名", ""), "tpl": m["template"],
            "staff": r.get("担当者", ""), "made": now_stamp(), "draft": did,
            "dc": "", "done": False}})
        ok.append(case_no)
        log(f"✉️ {case_no}：{m['template']} の下書きを作りました")
    return {"ok": ok, "ng": ng}


# ==========================================
# 📥 スプシの文面を取り込む（はじめの1回だけ）
# ==========================================
def _hex(c: dict) -> str:
    if not c:
        return ""
    rgb = c.get("rgbColor", c) if isinstance(c, dict) else {}
    r, g, b = (int(round(float(rgb.get(k, 0) or 0) * 255)) for k in ("red", "green", "blue"))
    h = f"#{r:02x}{g:02x}{b:02x}"
    return "" if h == "#000000" else h


def _fmt(base: dict, over: dict) -> dict:
    f = dict(base or {})
    f.update(over or {})
    col = _hex(f.get("foregroundColorStyle") or {}) or _hex(f.get("foregroundColor") or {})
    return {"b": bool(f.get("bold")), "c": col, "u": ((f.get("link") or {}).get("uri") or "")}


def cell_markup(cell: dict) -> str:
    """セルの文字と書式（太字・色・リンク）を、マークつきの文にする。"""
    text = str(cell.get("formattedValue", "") or "")
    if not text:
        return ""
    base = ((cell.get("userEnteredFormat") or {}).get("textFormat") or {})
    runs = cell.get("textFormatRuns") or [{"startIndex": 0, "format": {}}]
    if runs[0].get("startIndex", 0) != 0:
        runs = [{"startIndex": 0, "format": {}}] + runs
    pieces = []
    for i, rn in enumerate(runs):
        s = rn.get("startIndex", 0)
        e = runs[i + 1].get("startIndex", len(text)) if i + 1 < len(runs) else len(text)
        f = _fmt(base, rn.get("format") or {})
        t = text[s:e]
        if pieces and pieces[-1][1] == f:
            pieces[-1][0] += t
        else:
            pieces.append([t, f])
    out = ""
    for t, f in pieces:
        if not t:
            continue
        # ⚠️ 前後の改行・空白はマークの外に出す（リンクの文字に改行が入ると [..](..) が崩れる）
        m = re.match(r"^(\s*)(.*?)(\s*)$", t, re.S)
        pre, x, post = m.group(1), m.group(2), m.group(3)
        if not x:
            out += t
            continue
        if f["c"]:
            x = f"{{色:{f['c']}}}{x}{{/色}}"
        if f["b"] and x.strip():
            x = "{太字}" + x + "{/太字}"
        if f["u"]:
            x = f"[{x}]({f['u']})" if "\n" not in x else x
        out += pre + x + post
    return out


def read_template_tabs(gc, url: str, tabs) -> dict:
    """文面のシート（A列＝呼び名・B列＝文。2行目＝件名）を書式ごと読む → {シート: {"subject", "rows": [(行, 呼び名, 文)]}}。"""
    sh = _open(gc, url)
    have = {ws.title for ws in sh.worksheets()}
    tabs = [t for t in tabs if t in have]
    meta = sh.fetch_sheet_metadata(params={
        "ranges": [f"'{t}'!A1:B60" for t in tabs],
        "includeGridData": "true",
        "fields": "sheets(properties.title,data(startRow,rowData(values("
                  "formattedValue,textFormatRuns,userEnteredFormat.textFormat))))"})
    out = {}
    for s in meta.get("sheets", []):
        title = s["properties"]["title"]
        rows = []
        subject = ""
        for d in s.get("data", []):
            for i, rd in enumerate(d.get("rowData", []) or []):
                rn = d.get("startRow", 0) + i + 1
                vals = rd.get("values", []) or []
                a = str((vals[0] if vals else {}).get("formattedValue", "") or "").strip()
                bcell = vals[1] if len(vals) > 1 else {}
                if rn == 2:
                    subject = str(bcell.get("formattedValue", "") or "").strip()
                elif rn >= 3:
                    rows.append((rn, a, cell_markup(bcell)))
        out[title] = {"subject": subject, "rows": rows}
    return out


def _c(col, op, v=""):
    return {"列": col, "op": op, "値": v}


# ネットのGAS（2026-10-04 時点の generateNetDrafts）に行番号で書いてあった条件を、見出しの名前で書き直したもの。
# ⚠️ 取り込みのときに1回だけ使う。取り込んだあとは画面で直す（ここを直しても、取り込み済みの文面は変わらない）。
_FAM = _c("*選択プラン", "含む", "ファミリー")
_MAN = _c("*選択プラン", "含む", "マンション")
_HAJ = _c("選択プランCP", "含む", "はじめて割")
_SHIN = _c("選択プランCP", "含む", "新生活CP")
_NORI = _c("選択プランCP", "含む", "乗換CP")
_SBYM = _c("携帯キャリア", "含む", "ソフトバンク,Y-モバイル,Yモバイル")
_NSBYM = _c("携帯キャリア", "含まない", "ソフトバンク,Y-モバイル,Yモバイル")
_OUCHI = _c("おうち割", "＝", "○")
_NOUCHI = _c("おうち割", "≠", "○")
_CB = _c(CB_KEY, "空でない")
_YBB = _c("YBB基本サービス", "＝", "○,〇")
NET_LEGACY_RULES = {
    "WiMAX": {4: [_c("モバイル端末色", "≠", "置き型ルーター(ホワイト)")],
              5: [_c("モバイル端末色", "＝", "置き型ルーター(ホワイト)")], 7: [_CB]},
    "NURO": {3: [_c("*選択プラン", "含む", "G2V_マンションミニ")],
             4: [_c("*選択プラン", "含む", "G2V_ファミリー")], 6: [_CB]},
    "KDDI": {3: [_c("*選択プラン", "含む", "マンションタイプA")],
             4: [_c("*選択プラン", "含む", "ファミリータイプB")], 6: [_CB]},
    "SBAir": {4: [_c("*選択プラン", "含む", "Air通常CP")], 5: [_c("*選択プラン", "含む", "U-25/O-60")],
              6: [_c("NET後確先", "＝", "INE")], 7: [_c("NET後確先", "＝", "LINES")],
              8: [_c("SBAir新フロー", "＝", "false")], 9: [_c("SBAir新フロー", "＝", "true")],
              11: [_NORI], 12: [_YBB], 13: [_OUCHI], 14: [_CB]},
    "ドコモ光": {4: [_FAM], 6: [_FAM], 8: [_c("ネットキャンペーン選択欄", "含む", "ドコモ_乗り換え特典")], 9: [_CB]},
    "ドコモ光10G": {4: [_FAM], 6: [_FAM], 8: [_c("ネットキャンペーン選択欄", "含む", "ドコモ_乗り換え特典")], 9: [_CB]},
    "SB光半年無料": {4: [_HAJ], 5: [_SHIN], 6: [_NORI], 8: [_MAN], 9: [_FAM], 11: [_FAM],
                  13: [_SBYM], 14: [_SBYM, _SHIN], 15: [_SBYM, _NORI], 16: [_NSBYM],
                  17: [_NSBYM, _SHIN], 18: [_NSBYM, _NORI], 19: [_OUCHI], 20: [_NOUCHI], 21: [_CB]},
    "SB光": {4: [_HAJ], 5: [_SHIN], 6: [_NORI], 8: [_MAN], 9: [_FAM],
            10: [_c("NET後確先", "＝", "INE")], 11: [_c("NET後確先", "＝", "LINES")], 12: [_FAM],
            14: [_SBYM], 15: [_SBYM, _SHIN], 16: [_SBYM, _NORI], 17: [_NSBYM],
            18: [_NSBYM, _SHIN], 19: [_NSBYM, _NORI], 20: [_OUCHI], 21: [_NOUCHI], 22: [_CB]},
    "SB光10G法人": {4: [_YBB], 6: [_FAM], 8: [_FAM], 11: [_OUCHI], 12: [_NOUCHI], 13: [_CB]},
    "SB光10G": {4: [_FAM], 6: [_FAM], 8: [_SBYM], 9: [_NSBYM], 10: [_OUCHI], 11: [_NOUCHI], 12: [_CB]},
    "SB光解約新規": {4: [_MAN], 5: [_FAM], 7: [_FAM], 9: [_SBYM], 10: [_NSBYM],
                 11: [_OUCHI], 12: [_NOUCHI], 13: [_CB]},
    "SB光法人": {4: [_YBB], 6: [_HAJ], 7: [_HAJ, _MAN], 8: [_HAJ, _FAM], 9: [_FAM], 11: [_FAM],
              14: [_OUCHI], 15: [_NOUCHI], 16: [_CB]},
    "JCOM": {},
    "BIGLOBE": {4: [_c("スマートバリュー", "＝", "○")], 5: [_FAM], 7: [_FAM], 9: [_CB]},
    "BIGLOBE10G": {4: [_c("スマートバリュー", "＝", "○")], 5: [_FAM], 7: [_FAM], 9: [_CB]},
}

_TEN = _c("10GBplan", "＝", "true")
_HOJIN = _c("性別", "＝", "法人")
NET_LEGACY_ROUTES = [
    {"when": [_c("*商品", "＝", "BIGLOBE"), _TEN], "template": "BIGLOBE10G"},
    {"when": [_c("*商品", "＝", "BIGLOBE")], "template": "BIGLOBE"},
    {"when": [_c("*商品", "＝", "SB光"), _TEN, _HOJIN], "template": "SB光10G法人"},
    {"when": [_c("*商品", "＝", "SB光"), _TEN], "template": "SB光10G"},
    {"when": [_c("*商品", "＝", "SB光"), _c("現状利用有無", "＝", "半年無料")], "template": "SB光半年無料"},
    {"when": [_c("*商品", "＝", "SB光"), _HOJIN], "template": "SB光法人"},
    {"when": [_c("*商品", "＝", "SB光"), _c("現状利用有無", "＝", "解約新規")], "template": "SB光解約新規"},
    {"when": [_c("*商品", "＝", "SB光")], "template": "SB光"},
    {"when": [_c("*商品", "＝", "ドコモ光"), _TEN], "template": "ドコモ光10G"},
    {"when": [_c("*商品", "＝", "ドコモ光")], "template": "ドコモ光"},
    {"when": [_c("*商品", "＝", "SBAir")], "template": "SBAir"},
    {"when": [_c("*商品", "＝", "KDDI")], "template": "KDDI"},
    {"when": [_c("*商品", "＝", "NURO")], "template": "NURO"},
    {"when": [_c("*商品", "＝", "WiMAX")], "template": "WiMAX"},
    {"when": [_c("*商品", "含む", "JCOM,J:COM")], "template": "JCOM"},
]

NET_HEAD = ("{お客様名} 様\n\n"
            "お世話になっております。\n"
            "株式会社ライフアップ、ライフラインサポート窓口の{担当者}と申します。\n\n"
            "この度はインターネットのご案内をさせていただいて誠にありがとうございます。\n")
NET_FOOT = ("ご案内させて頂いた内容につきましてご不明点、ご質問等ございましたら、お気軽に弊社窓口へご連絡くださいませ。\n"
            "弊社フリーダイヤルは下記に記載させて頂きますので宜しくお願いいたします。\n"
            "【11:00～20:00 通話料無料】\n\n"
            "＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊\n"
            "{目立つ}お問い合わせフリーダイヤル 0800-300-7310{/目立つ}\n"
            "営業時間 11:00～20:00\n"
            "株式会社ライフアップ\n"
            "〒107-0052 東京都港区赤坂3-17-3 H1O赤坂 1108号\n"
            "ライフラインサポート窓口\n"
            "担当：{担当者}\n"
            "＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊＊")


def _blk(label, text, when=None):
    return {"uid": new_uid(), "label": label, "text": text, "when": when or []}


def import_net(gc, url: str) -> dict:
    """ネットの文面シートを読んで、アプリの形（振り分け・冒頭・末尾・文面）にする。保存はしない。

    キャッシュバックの金額の書き場所（〇〇〇〇／ドコモ光は○○）は {CB金額} に置き換える
    （GASは最初の1か所だけ置き換えていたので、それに合わせる）。
    """
    tabs = list(NET_LEGACY_RULES.keys())
    got = read_template_tabs(gc, url, tabs)
    tpls = {
        HEAD: {"subject": "", "blocks": [
            _blk("あいさつ", NET_HEAD + "先ほどお話しさせていただきました手続きの流れやキャンペーン等をまとめたものを"
                 "添付させていただきますのでご確認お願いいたします。",
                 [_c(TPL_COL, "≠", "JCOM")]),
            _blk("あいさつ（JCOM）", NET_HEAD + "先ほどお話しさせていただきました手続きの流れをまとめたものを"
                 "添付させていただきますのでご確認お願いいたします。",
                 [_c(TPL_COL, "＝", "JCOM")])]},
        FOOT: {"subject": "", "blocks": [_blk("署名", NET_FOOT)]},
    }
    for name in tabs:
        g = got.get(name)
        if not g:
            continue
        rules = NET_LEGACY_RULES.get(name) or {}
        blocks = []
        for rn, label, text in g["rows"]:
            if not text.strip():
                continue
            if rn in rules and rules[rn] and rules[rn][-1] is _CB:
                mark = "○○" if name.startswith("ドコモ光") else "〇〇〇〇"
                text = text.replace(mark, "{" + CB_KEY + "}", 1)
            blocks.append(_blk(label or f"{rn}行目", text, copy.deepcopy(rules.get(rn) or [])))
        tpls[name] = {"subject": g["subject"], "blocks": blocks}
    return {"routes": copy.deepcopy(NET_LEGACY_ROUTES), "templates": tpls,
            "missing": [t for t in tabs if t not in got]}


# ==========================================
# 🔄 BOXを更新（SFコネクタ・共通ロボット1台）
# ==========================================
def run_refresh(gc, set_cfg: dict, set_name: str) -> tuple:
    """SFコネクタで BOX などを更新する → (成功したか, ログ)。"""
    import auto_jobs
    import sms_runner
    url = str(set_cfg.get("sheet_url", "")).strip()
    tabs = list(set_cfg.get("refresh_tabs") or [set_cfg.get("box_tab") or "BOX"])
    urls = sms_runner.tab_urls_for(url, tabs, auto_jobs.tab_gids(gc, url))
    return sms_runner.run_sheet_refresh(set_cfg.get("refresh_robot"),
                                        sms_runner.work_dir("39メール", set_name),
                                        tabs=tabs, tab_urls=urls, url=url)
