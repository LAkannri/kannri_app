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
JOIN_LINE = "改行"           # 段落の「つなぎ」：前の段落のすぐ下に続ける（既定は空行をあける）
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
        "name_tpl": "{名前（姓）}　{名前（名）}",
        "staff_cols": ["電力契約担当者", "ガス契約担当者"],
        "staff_default": "担当者",
        "cb_cols": [],
        # ⭐ 電気・ガス・水道の連絡先を引く「地域マスタ」（別のスプシ）。空なら連絡先は引かない
        "region_master_url": "",
        # 前に GAS で作った分。LLの記録の2列目は電力会社なので、案件番号だけで見る（tpl_col=0）
        "legacy": {"tabs": ["確認用", "DC用", "「送信履歴」"], "case_col": 6, "tpl_col": 0},
        "routes": [],
        "templates": {},
    },
}

# 🗺 LL：地域マスタから引いて足す値（文面の {…} と条件で使える）
LL_VALUES = ["電力区分", "ガス区分", "電力開始日", "ガス開始日", "ガス立会時間",
             "地域電力名", "地域電力連絡先", "地域電力検索URL",
             "地域ガス名", "地域ガス連絡先", "地域ガス検索URL",
             "水道局名", "水道局連絡先", "水道局受付時間", "水道局備考行", "水道局検索URL",
             "水道区分", "書面誘導の対象", "書面誘導の文"]
# ⭐ 契約外の案内が「不動産会社でのご契約書にて…」のお客様は、うちで契約していない電気・水道・ガスを
#    地域の連絡先ではなく「契約書もしくは重要事項説明書に記載が…」でまとめて案内する（担当者 2026-10-04）
SHOMEN_WORD = "不動産会社でのご契約書"


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


EMPTY_WORD = "（空）"   # 「＝」「≠」の値に書くと「空のとき」を表す（例：（空）,地域電力）


def _vals(v) -> list:
    out = []
    for x in norm(v).split(","):
        x = x.strip()
        if x == norm(EMPTY_WORD):
            out.append("")
        elif x:
            out.append(x)
    return out


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
        return any(v in cell for v in vals if v)
    if op == "含まない":
        return not any(v in cell for v in vals if v)
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
        vv = "・".join(x or EMPTY_WORD for x in _vals(v))
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
MARK_HELP = ("{太字}太字{/太字} ／ {色:#ff0000}色つき{/色} ／ {目立つ}大きな黄色の帯{/目立つ} ／ "
             "{黄}黄色の見出し{/黄} ／ [見える文字](https://…) ／ [見える文字]({列名}) ／ "
             "{画像:DriveのファイルID} ／ {列名}＝お客様の値")
_HILITE = '<span style="font-size: 1.2em; background-color: yellow; font-weight: bold;">'
_YELLOW = '<span style="background-color: yellow; font-weight: bold; padding: 2px;">'
_MARK_WORDS = {"目立つ", "/目立つ", "/色", "太字", "/太字", "黄", "/黄"}


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
        if k == "黄":
            return _keep(_YELLOW)
        if k in ("/目立つ", "/色", "/黄"):
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

    # リンク先に {列名} を書ける（検索のURLなど、お客様ごとに変わるもの）
    def _ph_link(m):
        key = _html.unescape(m.group(2))
        url = str(values.get(key, "") or "")
        if key not in values and missing is not None:
            missing.append(key)
        if not url.startswith("http"):
            return m.group(1)
        return _keep(f'<a href="{_html.escape(url, quote=True)}">') + m.group(1) + _keep("</a>")

    s = re.sub(r"\[([^\]\n]+)\]\(\{([^{}\n]+)\}\)", _ph_link, s)
    s = re.sub(r"\{([^{}\n]+)\}", _ph, s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
               lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = s.replace("\r\n", "\n").replace("\n", "<br>")
    for k, v in tokens.items():
        s = s.replace(k, v)
    return s


def to_plain(text: str, values: dict) -> str:
    """プレビュー・AIに渡す用。マークを外した文。"""
    s = re.sub(r"\{(色:[^}]*|/色|目立つ|/目立つ|太字|/太字|黄|/黄|画像:[^}]*)\}", "", str(text or ""))
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1（\2）", s)
    return re.sub(r"\{([^{}\n]+)\}",
                  lambda m: str(values.get(m.group(1), m.group(0))), s)


# ==========================================
# 📄 BOX（お客様の一覧）を読む
# ==========================================
def _open(gc, url: str):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def col_letter(i: int) -> str:
    """0 → A、29 → AD。"""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def unique_head(head) -> list:
    """同じ見出しが2つあれば2つ目に（2）を付ける（「名義人同意」が2列ある）。

    ⚠️ 見出しが空の列も使うことがある（LLのBOXの会員.COMの印はAD列で見出しが無い）ので、
       空なら「（AD列）」のように列の記号で呼ぶ。
    """
    out, seen = [], {}
    for i, h in enumerate(head):
        h = str(h or "").strip() or f"（{col_letter(i)}列）"
        n = seen.get(h, 0)
        seen[h] = n + 1
        out.append(h if n == 0 else f"{h}（{n + 1}）")
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


def enrich(row: dict, set_cfg: dict, masters: dict = None) -> dict:
    """BOXの1行に、文面で使う値（お客様名・担当者・CB金額・LLなら地域の連絡先）を足す。"""
    r = dict(row)
    tpl = str(set_cfg.get("name_tpl", "") or "{名前（姓）} {名前（名）}")
    r.setdefault("お客様名", re.sub(r"\{([^{}]+)\}", lambda m: str(row.get(m.group(1), "")).strip(), tpl).strip())
    r.setdefault("担当者", staff_of(row, set_cfg))
    # ⚠️ BOXにも「CB金額」の列があるので、名前を分ける（同じ名前だと列の値がそのまま入る）
    r[CB_KEY] = cb_amount(row, set_cfg.get("cb_cols"))
    if set_cfg.get("region_master_url") or masters:
        r.update(ll_values(row, masters or {}))
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
    ci = int(lg.get("case_col", 5) or 5) - 1
    ti = int(lg.get("tpl_col", 2) if lg.get("tpl_col", 2) is not None else 2) - 1
    for vr in vrs[1:]:
        for r in vr.get("values", []) or []:
            if len(r) > max(ci, ti):
                c = str(r[ci]).strip()
                # tpl_col=0：文面の名前を記録していない（LL）＝案件番号だけで「作成済み」とみなす
                t = str(r[ti]).strip() if ti >= 0 else "*"
                if re.fullmatch(r"[A-Z]{2}\d{8}", c):
                    legacy.add(f"{c}|{t}")
                    cases.setdefault(c, []).append(t)
    masters = read_masters(gc, set_cfg.get("region_master_url")) if set_cfg.get("region_master_url") else {}
    return {"head": head, "rows": rows, "legacy": legacy, "legacy_cases": cases,
            "masters": masters, "missing_tabs": [t for t in ltabs if t not in have]}


def legacy_made(box: dict, case_no: str, tpl: str) -> bool:
    lg = box.get("legacy") or set()
    return f"{case_no}|{tpl}" in lg or f"{case_no}|*" in lg


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


def compose(set_cfg: dict, row: dict, tpl_name: str = None, masters: dict = None) -> dict:
    """1人ぶんのメールを作る。

    戻り値：{"template", "subject", "html", "plain", "images", "blocks"(出した段落の番号), "missing", "error"}
    ⚠️ 埋まらない {…} があれば error にする（文字のまま送らない）。
    ⚠️ 段落に「グループ」があれば、そのグループのどれか1つは必ず条件に合わないといけない
       （LLの電力・ガス：契約先の段落がまだ無い商品＝作らずに名指しする。前は「地域電力：●●」のまま送っていた）。
    段落の「つなぎ」：既定は空行をあける（<br><br>）。「改行」は前の段落のすぐ下に続ける。
    """
    r = enrich(row, set_cfg, masters)
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
    images, missing = [], []
    html, plain, markup = "", "", ""
    groups, group_cols = {}, {}
    sections = [(HEAD, tpls.get(HEAD) or {}), (name, tpl), (FOOT, tpls.get(FOOT) or {})]
    for sec, t in sections:
        for i, b in enumerate(t.get("blocks") or []):
            g = str(b.get("group", "") or "").strip()
            hit = match(b.get("when"), r)
            if g:
                groups[g] = groups.get(g, False) or hit
                group_cols.setdefault(g, []).extend(c.get("列", "") for c in b.get("when") or [])
            text = str(b.get("text", "") or "")
            if not text.strip() or not hit:
                continue
            h = to_html(text, r, images, missing)
            p = to_plain(text, r)
            mk = fill_markup(text, r)
            if html:
                line = b.get("join") == JOIN_LINE
                html += "<br>" if line else "<br><br>"
                plain += "\n" if line else "\n\n"
                markup += "\n" if line else "\n\n"
            html += h
            plain += p
            markup += mk
            if sec == name:
                out["blocks"].append(i + 1)
    subject = to_plain(str(tpl.get("subject", "") or ""), r)
    subj_missing = [k for k in placeholders(tpl.get("subject", "")) if k not in r]
    out.update(subject=subject, html=html, plain=plain, images=images, markup=markup)
    missing = sorted(set(missing + subj_missing))
    out["missing"] = missing
    nogroup = [g for g, ok in groups.items() if not ok]
    if nogroup:
        msgs = []
        for g in nogroup:
            cols = [c for c in dict.fromkeys(group_cols.get(g) or []) if c and c not in LL_VALUES]
            vals = "・".join(f"{c}「{r.get(c, '')}」" for c in cols[:3])
            msgs.append(f"「{g}」の段落がどれも合いません（{vals}）")
        out["error"] = "、".join(msgs) + "。✏️ 文面と条件で、この契約先の段落を足してください"
    elif missing:
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
            for kk, vv in v.items():
                if vv is None:
                    cur.pop(kk, None)
                else:
                    cur[kk] = vv
            if cur.get("done"):
                # 済んだものは、直すための中身を残さない（記録の行を大きくしない）
                for kk in ("markup", "subject", "images", "claim"):
                    cur.pop(kk, None)
            items[k] = cur
    cut = (datetime.date.fromisoformat(today()) - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    items = {k: v for k, v in items.items()
             if str(v.get("made", "9999"))[:10] >= cut or not v.get("done")}
    supabase.table("merchants").upsert({
        "id": rid, "name": f"（39メールの記録：{set_name}）", "is_active": False,
        "connector_type": "settings", "config_json": {"set": set_name, "items": items}}).execute()
    return items


# ==========================================
# 🔎 情報漏れ（あれば自動では送らず、DCへ回す）
# ==========================================
#   ⭐ 担当者 2026-10-04：「情報漏れすらなければ自動送信でもいい。漏れのあるものだけDC」。
#      機械で見つけられる漏れだけを見る（特記事項の読み落としのような中身の判断はできない＝画面にそう書く）。
DEFAULT_HOLD_WORDS = ["（未定）", "(日付未定)", "(未定)", "●●", "〇〇〇〇", "○○○○", "ここをクリックして"]
AUTO_DC = "自動送信"


def leaks(set_cfg: dict, row: dict, mail: dict, masters: dict = None) -> list:
    """そのお客様のメールの情報漏れ → 理由の並び（空なら漏れなし）。"""
    out = []
    r = enrich(row, set_cfg, masters)
    to = str(row.get(set_cfg.get("email_col", "メールアドレス"), "") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
        out.append("メールアドレスの形がおかしい")
    if not str(r.get("お客様名", "")).strip():
        out.append("お客様名が空")
    if str(r.get("担当者", "")).strip() in ("", str(set_cfg.get("staff_default", "") or "担当者")):
        out.append("担当者が空")
    plain = str(mail.get("plain", "") or "")
    for w in set_cfg.get("hold_words") or DEFAULT_HOLD_WORDS:
        if w and w in plain:
            out.append("「" + w + "」が残っている" + ("（地域マスタに無い手配先）" if w == "ここをクリックして" else ""))
    if r.get("ガス区分") == "LP" and not str(row.get("LPガス情報", "") or "").strip():
        out.append("LPガスなのにLPガス情報が空")
    return out


def make_drafts(supabase, set_name: str, set_cfg: dict, rows: list, log=print, masters: dict = None,
                send_clean: bool = False) -> dict:
    """選んだお客様の下書きを作る → {"ok": [...], "ng": [(案件番号, 理由)], "sent": [記録のキー…], "held": [(案件番号, 漏れ)]}。

    ⭐ 1件作るたびに記録する（途中で止まっても、作った分は二重に作らない）。
    ⭐ send_clean：情報漏れの無いものは、作ってすぐ送信して DC完了（DC担当者＝自動送信）。
       漏れのあるものは下書きのまま「📨 確認して送る」へ回る。
    """
    svc, creds = gmail(supabase)
    from_addr = str(set_cfg.get("from_addr", "") or "").strip()
    email_col = set_cfg.get("email_col", "メールアドレス")
    case_col = set_cfg.get("case_col", "案件番号")
    ok, ng, sent, held = [], [], [], []
    for row in rows:
        case_no = str(row.get(case_col, "") or "").strip()
        to = str(row.get(email_col, "") or "").strip()
        if "@" not in to:
            ng.append((case_no, "メールアドレスがありません"))
            continue
        m = compose(set_cfg, row, masters=masters)
        if m["error"]:
            ng.append((case_no, m["error"]))
            continue
        try:
            did = create_draft(svc, creds, to, m["subject"], m["html"], from_addr, m["images"])
        except Exception as e:
            ng.append((case_no, f"下書きを作れませんでした：{str(e)[:150]}"))
            continue
        r = enrich(row, set_cfg, masters)
        made = now_stamp()
        update_log(supabase, set_name, {log_key(case_no, m["template"]): {
            "case": case_no, "email": to, "name": r.get("お客様名", ""), "tpl": m["template"],
            "staff": r.get("担当者", ""), "made": made, "draft": did,
            "hist": history_row(set_cfg, row, m["template"], made, masters),
            # ✉️ 送る前に直せるように（送ったら消す＝記録の行を大きくしない）
            "to": to, "subject": m["subject"], "markup": m["markup"], "images": m["images"],
            "check": check_fields(set_cfg, row, m["template"], made, masters),
            "dc": "", "done": False}})
        ok.append(case_no)
        key = log_key(case_no, m["template"])
        lk = leaks(set_cfg, row, m, masters)
        if lk:
            held.append((case_no, lk))
            update_log(supabase, set_name, {key: {"leaks": lk}})
        if send_clean and not lk:
            try:
                sent_msg = svc.users().drafts().send(userId="me", body={"id": did}).execute()
                now = now_stamp()
                update_log(supabase, set_name, {key: {
                    "done": True, "dc": AUTO_DC, "done_at": now, "sent_at": now,
                    "sent_id": sent_msg.get("id", "")}})
                sent.append(key)
                log(f"📨 {case_no}：{m['template']} を送りました（情報漏れなし）")
            except Exception as e:
                ng.append((case_no, f"下書きは作りましたが、送れませんでした（📨 確認して送る に残っています）：{str(e)[:150]}"))
        else:
            log(f"✉️ {case_no}：{m['template']} の下書きを作りました" + (f"（⚠️ {'・'.join(lk)}）" if lk else ""))
    return {"ok": ok, "ng": ng, "sent": sent, "held": held}


# ==========================================
# 📨 1件ずつ確かめて送る（送った瞬間に DC 完了）
# ==========================================
#   ⭐ 担当者 2026-10-04：下書きを作ったら、確認用に出ていた項目を見ながら1件ずつ直し、アプリから送信する。
#      送った瞬間に DC 完了（DC担当者＝送った人）にして、送信履歴にも足す＝ほかの人と対応がダブらない。
#   ⭐ 開いた人を「確認中」として記録する（CLAIM_MIN 分）。ほかの人はその間、送れない（引き継ぐボタンで取れる）。
#   ⚠️ 送る直前に記録を読み直し、もう完了・ほかの人が確認中なら送らない。
#      Gmail の下書きは送ると消えるので、同じ下書きを2回送ろうとしても2回目は Gmail が断る（二重送信の最後の歯止め）。
CLAIM_MIN = 30


def fill_markup(text: str, values: dict) -> str:
    """マークはそのまま、{列名} だけお客様の値で埋めた文（送る前に人が直す用）。

    to_html(fill_markup(t, v), {}) は to_html(t, v) と同じ見た目になる。
    """
    def _link(m):
        url = str(values.get(m.group(2), "") or "")
        return f"[{m.group(1)}]({url})" if url.startswith("http") else m.group(1)

    t = re.sub(r"\[([^\]\n]+)\]\(\{([^{}\n]+)\}\)", _link, str(text or ""))

    def _ph(m):
        k = m.group(1)
        if k in _MARK_WORDS or k.startswith("色:") or k.startswith("画像:"):
            return m.group(0)
        return str(values[k]) if k in values and values[k] is not None else m.group(0)

    return re.sub(r"\{([^{}\n]+)\}", _ph, t)


def check_fields(set_cfg: dict, row: dict, tpl: str, made: str, masters: dict = None) -> list:
    """送る前に見る項目（これまで「確認用」シートに出ていたもの）→ [[見出し, 値], …]。"""
    h = history_row(set_cfg, row, tpl, made, masters)
    r = enrich(row, set_cfg, masters)
    if set_cfg.get("region_master_url") or masters:
        labels = ["アドレス", "電力キャリア", "ガスキャリア", "担当者", "作成時間", "案件番号",
                  "契約外の案内", "都道府県", "町名", "ガス案内不要理由"]
        out = [[a, b] for a, b in zip(labels, h)]
        out.insert(6, ["特記事項", str(row.get("特記事項", "") or "")])
        out.insert(1, ["お客様名", r.get("お客様名", "")])
        out.insert(8, ["契約Pack", str(row.get("契約Pack内容", "") or "")])
        return out
    labels = ["アドレス", "Nキャリア（文面）", "担当者", "作成時間", "案件番号"]
    out = [[a, b] for a, b in zip(labels, h)]
    out.insert(1, ["お客様名", r.get("お客様名", "")])
    for col in ("*商品", "*選択プラン", "選択プランCP", "携帯キャリア", CB_KEY):
        v = r.get(col, "")
        if str(v).strip():
            out.append([col.lstrip("*"), str(v)])
    return out


def _fresh_claim(e: dict, me: str) -> str:
    """ほかの人が確認中なら、その人の名前。"""
    c = e.get("claim") or {}
    who = str(c.get("by", "") or "")
    if not who or who == me:
        return ""
    try:
        at = datetime.datetime.strptime(str(c.get("at", "")), "%Y-%m-%d %H:%M:%S")
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime.now(jst).replace(tzinfo=None)
        if (now - at).total_seconds() > CLAIM_MIN * 60:
            return ""
    except Exception:
        return ""
    return who


def claim(supabase, set_name: str, key: str, me: str, force: bool = False) -> str:
    """その下書きを「確認中」にする → ほかの人が確認中ならその名前（取らない）。force で引き継ぐ。"""
    e = load_log(supabase, set_name).get(key) or {}
    other = _fresh_claim(e, me)
    if other and not force:
        return other
    update_log(supabase, set_name, {key: {"claim": {"by": me, "at": now_stamp()}}})
    return ""


def release(supabase, set_name: str, key: str, me: str):
    e = load_log(supabase, set_name).get(key) or {}
    if (e.get("claim") or {}).get("by") == me:
        update_log(supabase, set_name, {key: {"claim": {}}})


class DraftGone(Exception):
    """Gmail に下書きが無い（Gmail から送った・消した）。"""


def _draft_raw(e: dict, to: str, subject: str, markup: str, from_addr: str, creds) -> str:
    imgs = []
    html = to_html(markup, {}, images=imgs)
    msg = build_mime(to, subject, html, from_addr, imgs, creds)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def save_draft(supabase, set_name: str, set_cfg: dict, key: str, to: str, subject: str, markup: str):
    """直した中身を Gmail の下書きにも書き戻す（送らない）。"""
    e = load_log(supabase, set_name).get(key) or {}
    svc, creds = gmail(supabase)
    raw = _draft_raw(e, to, subject, markup, set_cfg.get("from_addr", ""), creds)
    try:
        svc.users().drafts().update(userId="me", id=e.get("draft", ""),
                                    body={"id": e.get("draft", ""), "message": {"raw": raw}}).execute()
    except Exception as ex:
        if "404" in str(ex) or "not found" in str(ex).lower():
            raise DraftGone() from ex
        raise
    update_log(supabase, set_name, {key: {"to": to, "subject": subject, "markup": markup}})


def send_now(supabase, set_name: str, set_cfg: dict, key: str, me: str,
             to: str, subject: str, markup: str) -> dict:
    """直した中身で送信し、DC完了にする → 記録の1件（送信履歴に書くのは呼ぶ側）。

    ⚠️ 送る直前に読み直す：もう完了／ほかの人が確認中なら送らない（ValueError）。
    """
    if "@" not in str(to):
        raise ValueError("宛先のメールアドレスが正しくありません")
    if re.search(r"\{[^{}\n]+\}", re.sub(r"\{(色:[^}]*|/色|目立つ|/目立つ|太字|/太字|黄|/黄|画像:[^}]*)\}", "", markup)):
        raise ValueError("本文に埋まっていない {…} が残っています")
    e = load_log(supabase, set_name).get(key) or {}
    if e.get("done"):
        raise ValueError(f"もう完了になっています（{e.get('dc', '')}）")
    other = _fresh_claim(e, me)
    if other:
        raise ValueError(f"{other} さんが確認中です（引き継いでから送ってください）")
    svc, creds = gmail(supabase)
    raw = _draft_raw(e, to, subject, markup, set_cfg.get("from_addr", ""), creds)
    try:
        sent = svc.users().drafts().send(userId="me", body={"id": e.get("draft", ""),
                                                             "message": {"raw": raw}}).execute()
    except Exception as ex:
        if "404" in str(ex) or "not found" in str(ex).lower():
            raise DraftGone() from ex
        raise
    now = now_stamp()
    items = update_log(supabase, set_name, {key: {
        "done": True, "dc": me, "done_at": now, "sent_at": now, "sent_id": sent.get("id", ""),
        "email": to, "claim": {}, "markup": None, "subject": None, "images": None}})
    # 送ったものは中身を残さない（記録の行を大きくしない）。宛先を直していれば送信履歴にも直した宛先を書く
    e2 = items.get(key) or {}
    for k in ("markup", "subject", "images"):
        e2.pop(k, None)
    if e2.get("hist") and e2["hist"][0] != to:
        e2["hist"] = [to] + list(e2["hist"][1:])
        update_log(supabase, set_name, {key: {"hist": e2["hist"]}})
    return e2


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


def auto_label(text: str, n: int = 18) -> str:
    """呼び名が無い段落に、文の書き出しで呼び名を付ける（「3行目」では何の段落か分からないため）。"""
    t = to_plain(text, {})
    first = next((x.strip(" 　・▼★※") for x in t.splitlines() if x.strip(" 　・▼★※")), "")
    first = re.sub(r"https?://\S+|（https?://[^）]*）", "", first).strip()
    return "本文：" + (first[:n] + ("…" if len(first) > n else "") if first else "（空）")


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
            blocks.append(_blk(label or auto_label(text), text, copy.deepcopy(rules.get(rn) or [])))
        tpls[name] = {"subject": g["subject"], "blocks": blocks}
    return {"routes": copy.deepcopy(NET_LEGACY_ROUTES), "templates": tpls,
            "missing": [t for t in tabs if t not in got]}


# ==========================================
# ⚡ LL：地域マスタ（別のスプシ）から電気・ガス・水道の連絡先を引く
# ==========================================
#   ⭐ 引き方はスプシのGAS（generateLifelineDrafts）と同じ。文の書き方は段落（画面で直せる）に持ち、
#      ここでは「値」だけを作る（{地域電力名} {水道局連絡先} など）。
#   ⚠️ 都道府県・市区郡が空のときは引かない（GASは空のとき1行目（北海道電力など）を拾っていた）。
MASTER_TABS = {"denki": "電力", "gas_area": "ガスエリアデータ", "gas_contact": "ガス連絡先",
               "water": "水道局マスタ"}
ALL_ELECTRIC = "ループ電気(オール電化)"


def _zip_key(v) -> str:
    s = re.sub(r"\.0$", "", str(v or ""))
    return re.sub(r"[ー－ｰ\-\s]", "", s).strip()


def read_masters(gc, url: str) -> dict:
    """地域マスタを1回の問い合わせで読む → JSONにできる形（画面のキャッシュに載せるため）。"""
    sh = _open(gc, url)
    names = list(MASTER_TABS.values())
    res = sh.values_batch_get([f"'{t}'!A1:I" for t in names],
                              params={"valueRenderOption": "FORMATTED_VALUE"})
    got = {}
    for key, vr in zip(MASTER_TABS.keys(), res.get("valueRanges", [])):
        rows = [list(r) + [""] * 9 for r in (vr.get("values", []) or [])[1:]]
        # ⚠️ 確認の列が「要確認」の行は使わない（地域手配SMSと同じきまり。水道＝G列・ガス連絡先＝C列）
        ng = {"water": 6, "gas_contact": 2}.get(key)
        if ng is not None:
            rows = [r for r in rows if str(r[ng]).strip() != MASTER_CHECK_NG]
        got[key] = rows
    gas_area = {}
    for r in got.get("gas_area", []):
        k = _zip_key(r[0])
        if k:
            gas_area[k] = str(r[1]).strip()
    water = {}
    for r in got.get("water", []):
        k = re.sub(r"\s+", "", str(r[0]) + str(r[1]))
        if k:
            water[k] = [str(r[2]), str(r[3]), str(r[4]), str(r[5])]
    return {"denki": [[str(r[0]), str(r[1]), str(r[2])] for r in got.get("denki", [])],
            "gas_area": gas_area,
            "gas_contact": [[str(r[0]), str(r[1])] for r in got.get("gas_contact", [])],
            "water": water}


def _ymd(v, empty: str) -> str:
    s = str(v or "").strip()
    if not s:
        return empty
    m = re.fullmatch(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", unicodedata.normalize("NFKC", s))
    return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else s


def _search(q: str) -> str:
    from urllib.parse import quote
    return "https://www.google.com/search?q=" + quote(q, safe="-_.!~*'()")


def ll_values(row: dict, masters: dict) -> dict:
    """LLの1行から、地域の連絡先と、電力・ガスの区分を作る。

    電力区分：地域（電力キャリアが空・地域電力・案内NG）／契約
    ガス区分：オール電化（電力がループ電気(オール電化)）／LP（ガスNG理由がLPガス利用・ガス備考にLPガス・
              ガスキャリアがLPガス）／地域（ガスキャリアが空・地域ガス）／契約
    """
    g = lambda k: str(row.get(k, "") or "").strip()
    pref, city = g("都道府県"), re.sub(r"\s+", "", g("市区郡"))
    ele, gas = g("電力キャリア"), g("ガスキャリア")
    out = {k: "" for k in LL_VALUES}
    out["電力区分"] = "地域" if ele in ("", "地域電力", "案内NG") else "契約"
    lp = g("ガスNG･案内不要理由") == "LPガス利用" or "LPガス" in g("ガス備考")
    if ele == ALL_ELECTRIC:
        out["ガス区分"] = "オール電化"
    elif lp or gas == "LPガス":
        out["ガス区分"] = "LP"
    elif gas in ("", "地域ガス"):
        out["ガス区分"] = "地域"
    else:
        out["ガス区分"] = "契約"
    # 📄 契約書面に案内する（うちで契約していないものだけ。電気→水道→ガスの順）
    shomen = SHOMEN_WORD in norm(row.get("契約外公共料金の案内の有無", ""))
    targets = []
    if shomen and out["電力区分"] == "地域":
        out["電力区分"] = "書面"
        targets.append("電気")
    out["水道区分"] = "書面" if shomen else "地域"
    if shomen:
        targets.append("水道")
    if shomen and out["ガス区分"] == "地域":
        out["ガス区分"] = "書面"
        targets.append("ガス")
    out["書面誘導の対象"] = "・".join(targets)
    out["書面誘導の文"] = (targets[0] if len(targets) == 1 else
                       "と".join(targets) if len(targets) == 2 else
                       "、".join(targets[:-1]) + "と" + targets[-1]) if targets else ""
    out["電力開始日"] = _ymd(row.get("電力利用開始日"), "（未定）")
    out["ガス開始日"] = _ymd(row.get("ガス立合希望日"), "(日付未定)")
    out["ガス立会時間"] = g("ガス立合希望時間") or "(未定)"
    # 電力
    if pref:
        for name, prefs, phone in masters.get("denki") or []:
            if pref in prefs:
                out["地域電力名"], out["地域電力連絡先"] = name, phone
                break
    out["地域電力連絡先"] = out["地域電力連絡先"] or "ー"
    out["地域電力検索URL"] = _search(pref + " 地域電力 電話番号 開始")
    # ガス
    area = (masters.get("gas_area") or {}).get(_zip_key(row.get("*郵便番号")), "")
    if area:
        for name, phone in masters.get("gas_contact") or []:
            if area in name:
                out["地域ガス名"], out["地域ガス連絡先"] = name, phone
                break
        out["地域ガス名"] = out["地域ガス名"] or area
    out["地域ガス連絡先"] = out["地域ガス連絡先"] or "（地域ガス会社へお問合せください）"
    out["地域ガス検索URL"] = _search(pref + city + " ガス会社 電話番号 開栓")
    # 水道
    water = masters.get("water") or {}
    key = pref + city
    # ⚠️ マスタには局名が空の行もある（GASはそれを拾って「地域水道局：」を空のまま出していた）。飛ばして次を探す
    hit = water.get(key) if pref else None
    if hit and not hit[0].strip():
        hit = None
    if not hit and pref and city:
        hit = next((v for k, v in water.items() if (key in k or k in key) and v[0].strip()), None)
    if hit:
        out["水道局名"], out["水道局連絡先"], out["水道局受付時間"] = hit[0], hit[1], hit[2]
        out["水道局備考行"] = f"\n備考：{hit[3]}" if hit[3] else ""
    out["水道局検索URL"] = _search(pref + city + " 水道局 電話番号 開栓")
    return out


# ==========================================
# 📥 LLの文面を取り込む（はじめの1回だけ）
# ==========================================
def _grid(gc, url: str, rng: str) -> list:
    sh = _open(gc, url)
    meta = sh.fetch_sheet_metadata(params={
        "ranges": [rng], "includeGridData": "true",
        "fields": "sheets(data(rowData(values(formattedValue,textFormatRuns,userEnteredFormat.textFormat))))"})
    out = []
    for s in meta.get("sheets", []):
        for d in s.get("data", []):
            for rd in d.get("rowData", []) or []:
                out.append(rd.get("values", []) or [])
    return out


def _is_gas(carrier: str) -> bool:
    c = carrier.strip()
    if c.endswith("(E)"):
        return False
    return c.endswith("(G)") or "ガス" in c or c in ("TOKAI",)


def _yellow_title(markup: str) -> str:
    first, _, rest = str(markup or "").partition("\n")
    return "{黄}" + first + "{/黄}" + ("\n" + rest if rest else "")


LL_HEAD = ("{お客様名} 様\n\n"
           "お世話になっております。\n"
           "株式会社ライフアップ、ライフラインサポートの{担当者}と申します。\n"
           "この度はライフラインのご案内をさせていただき誠にありがとうございます。\n"
           "こちらのメールにてご連絡先等のお伝えをさせていただきますので、今一度ご確認をお願いいたします。\n\n"
           "開始日変更、お支払方法のご登録についてなどご質問がある場合は、\n"
           "下記メールアドレスまでお問い合わせくださいませ。\n"
           "【info@lifeap.co.jp】")
LL_STARS = "＊" * 26
LL_FOOT = ("ご案内させていただいた内容につきましてご不明点、ご質問等ございましたら、お気軽に弊社窓口へご連絡くださいませ。\n"
           "メールにてお問合せの場合は【info@lifeap.co.jp】までご連絡をお願いいたします。\n"
           + LL_STARS + "\n\n"
           "{目立つ}お問い合わせフリーダイヤル　0800-300-7310{/目立つ}\n"
           "営業時間11：00～20：00\n"
           "株式会社ライフアップ\n"
           "〒107-0052 東京都港区赤坂3-17-3　H1O赤坂　1108号\n"
           "ライフラインサポート窓口\n"
           "担当：{担当者}\n" + LL_STARS)
LL_KAIIN_IMAGES = ["1HVjgBaD8Snwu51lc_NGpUb0uJ1mYYeuI", "1RXFcKHzn5GZZhNJayguWDHdy4Gbo0-58"]
LL_TEMPLATE = "ライフライン"


SHOMEN_TEXT = ("＜{書面誘導の対象}＞\n"
               "{書面誘導の文}に関しましては契約書もしくは重要事項説明書に記載が\n"
               "ございますのでご確認の上、お引越しまでにお手配をお願いいたします。")


def upgrade_ll_shomen(tpl: dict) -> dict:
    """LLの文面に「契約書面へのご案内」を足す（取り込み済みの文面にも1回だけ当てる。何度当てても同じ）。

    ・電力・ガスのグループに「書面」の空の段落（どれか1つは出す、を満たすため）
    ・水道の2つの段落に「水道区分＝地域」
    ・「水道・停止の注意」の前に「契約書面へのご案内」
    """
    blocks = tpl.setdefault("blocks", [])
    labels = [b.get("label", "") for b in blocks]
    if "契約書面へのご案内" in labels:
        return tpl
    for b in blocks:
        if b.get("label") in ("水道（マスタにある）", "水道（マスタに無い）"):
            if not any(c.get("列") == "水道区分" for c in b.get("when") or []):
                b.setdefault("when", []).insert(0, _c("水道区分", "＝", "地域"))

    def _pos(label, default):
        return next((i for i, b in enumerate(blocks) if b.get("label") == label), default)

    i = _pos("電力の見出し（弊社で手配）", 0)
    blocks.insert(i, dict(_blk("電力（契約書面に案内する）", "", [_c("電力区分", "＝", "書面")]), group="電力"))
    i = _pos("ガスの見出し（お客様で手配）", len(blocks))
    blocks.insert(i, dict(_blk("ガス（契約書面に案内する）", "", [_c("ガス区分", "＝", "書面")]), group="ガス"))
    i = _pos("水道・停止の注意", len(blocks))
    blocks.insert(i, _blk("契約書面へのご案内", SHOMEN_TEXT, [_c("書面誘導の対象", "空でない")]))
    return tpl


def import_ll(gc, url: str) -> dict:
    """LLの文面（「LL」シート＝契約先ごとの案内、「付帯」シート）を読み、アプリの形にする。保存はしない。

    「LL」シートのA列（開栓区分_商材名）を分けて、電力キャリア／ガスキャリアの条件にする。
    電力かガスかは商材名で決める（_(E)＝電力、_(G)・ガスを含む・TOKAI＝ガス）。
    """
    def blk(label, text, when=None, group="", join=""):
        b = _blk(label, text, when)
        if group:
            b["group"] = group
        if join:
            b["join"] = join
        return b

    ele_head = "{太字}＜電力＞　※{電力開始日}利用開始にてお手配"
    gas_head = "{太字}＜ガス＞　※{ガス開始日}利用開始にてお手配"
    gas_time = "\n　　　　　ガスお立会時間　{ガス立会時間}{/太字}"
    E_LOCAL, E_CONTRACT = _c("電力区分", "＝", "地域"), _c("電力区分", "＝", "契約")
    ele_blocks = [
        blk("電力（地域・連絡先あり）", ele_head + "ください{/太字}\n地域電気：{地域電力名}\n連絡先：{地域電力連絡先}",
            [E_LOCAL, _c("地域電力名", "空でない")], "電力"),
        blk("電力（地域・マスタに無い）", ele_head + "ください{/太字}\n地域電気：[【ここをクリックして電力を検索】]({地域電力検索URL})\n連絡先：{地域電力連絡先}",
            [E_LOCAL, _c("地域電力名", "空")], "電力"),
        blk("電力の見出し（弊社で手配）", ele_head + "いたします{/太字}", [E_CONTRACT]),
    ]
    gas_blocks = [
        blk("ガス（オール電化なので書かない）", "", [_c("ガス区分", "＝", "オール電化")], "ガス"),
        blk("ガスの見出し（お客様で手配）", gas_head + "ください" + gas_time, [_c("ガス区分", "＝", "LP,地域")]),
        blk("ガス（LPガス）", "{LPガス情報}", [_c("ガス区分", "＝", "LP")], "ガス", JOIN_LINE),
        blk("ガス（地域・連絡先あり）", "地域ガス：{地域ガス名}\n連絡先：{地域ガス連絡先}",
            [_c("ガス区分", "＝", "地域"), _c("地域ガス名", "空でない")], "ガス", JOIN_LINE),
        blk("ガス（地域・マスタに無い）", "地域ガス：[【ここをクリックしてガス会社を検索】]({地域ガス検索URL})\n連絡先：{地域ガス連絡先}",
            [_c("ガス区分", "＝", "地域"), _c("地域ガス名", "空")], "ガス", JOIN_LINE),
        blk("ガスの見出し（弊社で手配）", gas_head + "いたします" + gas_time, [_c("ガス区分", "＝", "契約")]),
    ]
    found = []
    for row in _grid(gc, url, "'LL'!A1:D80"):
        key = str((row[0] if row else {}).get("formattedValue", "") or "").strip()
        if "_" not in key:
            continue
        kubun, carrier = key.split("_", 1)
        text = cell_markup(row[3] if len(row) > 3 else {})
        if not text.strip():
            continue
        found.append(key)
        if _is_gas(carrier):
            gas_blocks.append(blk(f"ガス：{key}", text,
                                  [_c("ガス区分", "＝", "契約"), _c("ガス開栓区分", "＝", kubun),
                                   _c("ガスキャリア", "＝", carrier)], "ガス", JOIN_LINE))
        else:
            ele_blocks.append(blk(f"電力：{key}", text,
                                  [_c("電力開栓区分", "＝", kubun), _c("電力キャリア", "＝", carrier)],
                                  "電力", JOIN_LINE))
    water_blocks = [
        blk("水道（マスタにある）", "{太字}＜水道＞{/太字}\n地域水道局：{水道局名}\n連絡先：{水道局連絡先}\n"
            "受付時間：{水道局受付時間}{水道局備考行}", [_c("水道局名", "空でない")]),
        blk("水道（マスタに無い）", "{太字}＜水道＞{/太字}\n地域水道局：[【ここをクリックして水道局を検索】]({水道局検索URL})",
            [_c("水道局名", "空")]),
        blk("水道・停止の注意", "※お手数ではございますが水道につきましてはお引越しまでに開栓のご連絡をお願いいたします。\n"
            "※お引越し前のお建物の電力・ガス・水道の停止につきましてはご自身で申請が必要となりますので"
            "お忘れにならないようにお気をつけくださいませ。"),
    ]
    futai = {}
    for row in _grid(gc, url, "'付帯'!A1:B30"):
        k = str((row[0] if row else {}).get("formattedValue", "") or "").strip().upper()
        if k:
            futai[k] = cell_markup(row[1] if len(row) > 1 else {})
    opt_blocks = []
    if futai.get("引越しマルシェ"):
        opt_blocks.append(blk("付帯：引越しマルシェ", _yellow_title(futai["引越しマルシェ"]),
                              [_c("引越マルシェ", "＝", "○,〇")]))
    if futai.get("FP"):
        opt_blocks.append(blk("付帯：FP", _yellow_title(futai["FP"]), [_c("FP付帯", "＝", "○,〇")]))
    if futai.get("会員.COM"):
        imgs = "".join("\n\n{画像:" + i + "}" for i in LL_KAIIN_IMAGES)
        opt_blocks.append(blk("付帯：会員.COM", _yellow_title(futai["会員.COM"]) + imgs,
                              [_c("（AD列）", "＝", "1")]))
    tpls = {
        HEAD: {"subject": "", "blocks": [_blk("あいさつ", LL_HEAD)]},
        LL_TEMPLATE: upgrade_ll_shomen({"subject": "お引越し先のライフラインについて",
                                        "blocks": ele_blocks + gas_blocks + water_blocks + opt_blocks}),
        FOOT: {"subject": "", "blocks": [_blk("署名", LL_FOOT)]},
    }
    return {"routes": [{"uid": new_uid(), "when": [], "template": LL_TEMPLATE}], "templates": tpls,
            "found": found, "missing": [k for k in ("引越しマルシェ", "FP", "会員.COM") if not futai.get(k)]}


# ==========================================
# 🗺 地域マスタに無かった手配先（画面で足す）
# ==========================================
#   ⭐ 地域手配SMS（引越し前SMS・地域手配SMS希望）と同じマスタ・同じきまり（2026-09-22 担当者と決めた）：
#      ・確認の列が「要確認」の行は使わない（安全弁）。それ以外（空・自動・済・手入力）は使う
#      ・足すときは 確認・出典・追記日 を必ず残す（あとで裏取りできるように）
#      ・電話番号は形を確かめてから入れる（先頭0・10〜11桁）
MASTER_CHECK_NG = "要確認"


def phone_ok(v: str) -> bool:
    d = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", str(v or "")))
    return bool(re.fullmatch(r"0\d{9,10}", d))


def ll_missing(row: dict, masters: dict) -> list:
    """そのお客様で、地域マスタから引けなかった手配先 → [{"種類", "都道府県", "市区郡", "郵便番号", "エリア"}]。

    電力・ガスは「地域」のときだけ（契約先の案件はマスタを使わない）。水道はいつも。
    """
    v = ll_values(row, masters)
    g = lambda k: str(row.get(k, "") or "").strip()
    base = {"都道府県": g("都道府県"), "市区郡": re.sub(r"\s+", "", g("市区郡")),
            "郵便番号": g("*郵便番号"), "エリア": ""}
    out = []
    if v["電力区分"] == "地域" and not v["地域電力名"] and base["都道府県"]:
        out.append(dict(base, 種類="電力"))
    if v["ガス区分"] == "地域" and v["地域ガス連絡先"] == "（地域ガス会社へお問合せください）":
        area = (masters.get("gas_area") or {}).get(_zip_key(row.get("*郵便番号")), "")
        out.append(dict(base, 種類="ガス", エリア=area))
    if v["水道区分"] == "地域" and not v["水道局名"] and base["都道府県"] and base["市区郡"]:
        out.append(dict(base, 種類="水道"))
    return out


def add_to_master(gc, url: str, kind: str, item: dict, name: str, phone: str,
                  source: str = "", hours: str = "", days: str = "", mark: str = "手入力") -> str:
    """地域マスタに1行足す → 足した先の説明（失敗は例外）。

    水道：水道局マスタ［都道府県・市区町村・局名・電話番号・営業時間・営業日・確認・出典・追記日］
    ガス：（郵便番号がエリア未登録なら）ガスエリアデータ［郵便番号・エリア］＋ ガス連絡先［エリア・電話番号・確認・出典・追記日］
    電力：電力［電力会社名・対応都道府県・電話番号］
    """
    name, phone = str(name or "").strip(), str(phone or "").strip()
    if not name:
        raise ValueError("名前（局名・会社名）が空です")
    if not phone_ok(phone):
        raise ValueError(f"電話番号の形ではありません：{phone}（0で始まる10〜11桁）")
    sh = _open(gc, url)
    day = today()
    if kind == "水道":
        sh.worksheet(MASTER_TABS["water"]).append_row(
            [item.get("都道府県", ""), item.get("市区郡", ""), name, phone, hours, days, mark, source, day],
            value_input_option="RAW", table_range="A1")
        return f"水道局マスタに「{item.get('都道府県')}{item.get('市区郡')} → {name}」を足しました"
    if kind == "ガス":
        msg = []
        if not item.get("エリア") and item.get("郵便番号"):
            sh.worksheet(MASTER_TABS["gas_area"]).append_row(
                [item.get("郵便番号", ""), name], value_input_option="RAW", table_range="A1")
            msg.append(f"ガスエリアデータに「{item.get('郵便番号')} → {name}」")
        sh.worksheet(MASTER_TABS["gas_contact"]).append_row(
            [name, phone, mark, source, day], value_input_option="RAW", table_range="A1")
        msg.append(f"ガス連絡先に「{name}」")
        return "、".join(msg) + "を足しました"
    if kind == "電力":
        sh.worksheet(MASTER_TABS["denki"]).append_row(
            [name, item.get("都道府県", ""), phone], value_input_option="RAW", table_range="A1")
        return f"電力に「{name}（{item.get('都道府県')}）」を足しました"
    raise ValueError(f"分からない種類：{kind}")


# ==========================================
# 📜 送信履歴（DCが済んだらスプシに1行足す）
# ==========================================
#   ⭐ これまで人が「DC用」から「「送信履歴」」へ写していた行と同じ並びで足す。
#      ネット：アドレス／文面（Nキャリア）／担当者／作成時間／案件番号／（空）／DC担当者／完了
#      LL  ：アドレス／電力キャリア／ガスキャリア／担当者／作成時間／案件番号／契約外の案内／都道府県／町名／ガス案内不要理由／DC担当名／完了
#   電力・ガスの欄は、GASと同じく「地域」のときは地域の会社名（引けなければ「地域電力(未特定)」）を書く。
HISTORY_TAB = "「送信履歴」"


def history_row(set_cfg: dict, row: dict, tpl: str, made: str, masters: dict = None) -> list:
    """下書きを作ったときに、送信履歴に書く行（DC担当者・完了の手前まで）を作っておく。"""
    r = enrich(row, set_cfg, masters)
    email = str(row.get(set_cfg.get("email_col", "メールアドレス"), "") or "").strip()
    case_no = str(row.get(set_cfg.get("case_col", "案件番号"), "") or "").strip()
    when = made.replace("-", "/")
    if not (set_cfg.get("region_master_url") or masters):
        return [email, tpl, r.get("担当者", ""), when, case_no, ""]
    g = lambda k: str(row.get(k, "") or "").strip()
    if r.get("電力区分") in ("地域", "書面"):
        ele = r.get("地域電力名") or "地域電力(未特定)"
    else:
        ele = g("電力キャリア")
    gk = r.get("ガス区分")
    gas = {"オール電化": "オール電化", "LP": "LPガス"}.get(gk) or (
        (r.get("地域ガス名") or "地域ガス(未特定)") if gk in ("地域", "書面") else g("ガスキャリア"))
    return [email, ele, gas, r.get("担当者", ""), when, case_no, g("契約外公共料金の案内の有無"),
            g("都道府県"), re.sub(r"\s+", "", g("市区郡")), g("ガスNG･案内不要理由")]


def write_history(gc, set_cfg: dict, entries: list) -> int:
    """完了した下書きを送信履歴に足す（まとめて1回）→ 足した件数。"""
    rows = []
    for e in entries:
        h = list(e.get("hist") or [e.get("email", ""), e.get("tpl", ""), e.get("staff", ""),
                                   str(e.get("made", "")).replace("-", "/"), e.get("case", ""), ""])
        rows.append(h + [e.get("dc", ""), True])
    if not rows:
        return 0
    sh = _open(gc, set_cfg.get("sheet_url"))
    sh.worksheet(set_cfg.get("history_tab") or HISTORY_TAB).append_rows(
        rows, value_input_option="USER_ENTERED", table_range="A1")
    return len(rows)


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
