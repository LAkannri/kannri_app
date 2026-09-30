"""
🌐 RENXA（多言語窓口）への連携（エントリー業務自動化のホームの「🌐 RENXA」から開く）。

日本語で案内できないお客様は、Renxa株式会社（多言語窓口）に案内をお願いしている。

【これまでの手作業】
  SFコネクタで「BOX」を更新 → 出てきた案件を1件ずつ、トヨクモ（kintone連携サービス）の
  「不動産個人情報取得フォーム」に入力して回答 → Data Loader で「多言語窓口連携状況」を「連携済み」に。

【ここでやること】
  ① SFコネクタで「BOX」を更新（どの案件を送るかは、このシートが決める）
  ② 案件の中身は **Salesforce から読む**（BOXには名前のフリガナ・電話・生年月日などが無いため）
     → フォームの選び方（電気・ガス・水道・ネットの種別など）を決める。決められない案件は名指しして外す
  ③ ロボット「RENXA」で、**ブラウザ1回・ログイン1回のまま**全件をフォームに入れて回答
  ④ 回答まで行った案件だけ、Salesforce の「多言語窓口連携状況」（`Field127__c`）を「連携済み」にする

⭐ **二重に送らない**：Salesforce が「連携済み」の案件は送らない。回答ボタンを押す**前に**案件IDを
   控え（`sent`）に入れ、回答まで行かずに止まった案件だけ控えから外す。回答まで行ったのに
   連携済みにできなかった／回答したか分からない案件は、控えが残っているあいだは送らない（人が確かめて消す）。
⭐ **種別の決め方は、SFのフラグに合わせる**（エントリーのときに案内不要・不要理由が入っている）。
   案内する状況（新規・検討中…）なら 電気＝個別契約／ガス＝都市ガス／ネット＝個別契約。
   ガスが LPガス利用 → プロパンガス（LPガス情報の会社名・連絡先を入れる。**連絡先が無ければ止める**＝不動産に確認してから）。
   ネットが 無料ネット導入物件 → 無料ネット（ネット案内不要）。オール電化 → オール電化。それ以外 → 案内不要。
   専用電力（一括供給）は不動産から案内するので **案内不要**（担当者 2026-09-30）。
   水道はSFにフラグが無いので、契約Packで決める（設定 `water_rule`）。
⚠️ スプシ・フォームのURLはコードに書かない（公開リポジトリ）。設定（Supabase の予約行 `__renxa__`）に持たせる。
"""
import datetime as _dt
import os
import re
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

SETTINGS_ID = "__renxa__"
SETTINGS_NAME = "（RENXA連携の設定）"
WORK_ROOT = "RENXA"
REPORT_TAB = "BOX"                  # SFコネクタで更新するシート（案件 ID の並び）
ID_COL = "案件 ID"
DEFAULT_ROBOT = "RENXA"
DEFAULT_STAFF = "理田"              # 時間指定のときの店舗担当者名
DEFAULT_LOGIN_MAIL = "info@lifeap.co.jp"
DEFAULT_COMPANY_CODE = "7760"       # フォームの検索で選ぶ会社（株式会社LIFEAP）
TOKYO_COMFORT = "東京コンフォート"   # 店舗名にこれがあれば「その他申し送り事項」に書く

SF_OBJECT = "Opportunity"
SF_LINK_FIELD = "Field127__c"       # 多言語窓口連携状況（未連携／連携済み／対象外）
LINKED = "連携済み"
NOT_TARGET = "対象外"

# 案内する（＝RENXAに個別契約として案内してもらう）状況。これ以外（案内不要・NG・確定案件・空）は案内しない
GUIDE_NET = ("新規", "検討中", "検討中・日付指定", "トスアップ")
GUIDE_LL = ("新規", "検討中", "検討中・日付指定", "地点番号確認中", "トスアップ")

ELEC_OPTS = ("個別契約", "オール電化", "専用電力", "案内不要")
GAS_OPTS = ("都市ガス", "プロパンガス", "オール電化", "案内不要")
WATER_OPTS = ("個別契約", "案内不要")
NET_OPTS = ("個別契約", "無料ネット（ネット案内希望）", "無料ネット（ネット案内不要）", "案内不要")

# 水道の決め方（SFに水道のフラグが無いので、契約Packで決める）
WATER_RULES = {
    "ll": "契約Packに電気かガスがあれば個別契約（無ければ案内不要）",
    "always": "いつも個別契約",
    "never": "いつも案内不要",
}
# フリガナが空のとき（担当者 2026-09-30：いつも姓・名をそのまま入れている）
KANA_RULES = {
    "same": "姓・名と同じ文字を入れる",
    "stop": "止める（人が画面で入れる）",
}

# 申込種別（フォームのプルダウン）。RENXAに回すのは外国語のお客様なので、検討理由で2つに分ける
#   検討理由が「メール案件外国語」→ メール・SNS希望／それ以外（外国語）→ 電話希望（担当者 2026-09-30）
APPLY_PHONE = "外国語対応のお客様（電話希望）"
APPLY_MAIL = "外国語対応のお客様（メール・SNS希望）"
APPLY_OPTS = ("日本語対応のお客様", APPLY_PHONE, APPLY_MAIL, "店舗・法人", "オンライン接客でのご案内")
MAIL_REASON = "メール案件外国語"
# 外国語を選ぶと出る「グローバル」の欄。国籍は分からないので「不明」、言語はSFの「言語_RENXA連携」、空なら英語
NATIONALITY = "不明"
FORM_LANGS = ("英語", "中国語", "広東語", "韓国語", "ベトナム語", "インド語", "ネパール語",
              "ミャンマー語", "タガログ語", "インドネシア語", "スペイン語", "ポルトガル語", "タイ語", "シンハラ語")
DEFAULT_LANG = "英語"
# SFの言語 → フォームのチェック（名前が違うものだけ）
LANG_MAP = {"北京語": "中国語", "ヒンディー語": "インド語"}

# ロボットに渡す値（手順書の {…}）。どの周も必ず全部そろえる（無いと robot.py が止まる）
VAR_KEYS = ("店舗担当者名", "郵便番号", "都道府県", "市区町村", "町域", "番地", "物件名", "部屋番号",
            "電気の種別", "ガスの種別", "水道の種別", "インターネットの種別",
            "プロパンガス会社名", "プロパンガス連絡先", "申込種別", "国籍", "言語",
            "姓", "名", "セイ", "メイ", "電話番号1", "電話番号2", "電話番号3",
            "生年月日", "入居日", "メールアドレス", "ご連絡日時", "その他申し送り事項")
# 画面で直せる値
EDITABLE = ("姓", "名", "セイ", "メイ", "電気の種別", "ガスの種別", "水道の種別", "インターネットの種別",
            "プロパンガス会社名", "プロパンガス連絡先", "申込種別", "言語", "ご連絡日時", "その他申し送り事項")

_OPP_FIELDS = ("Id", "ProposalNumber__c", "StageName", "NGcategory__c",
               "Powersituation__c", "PowerNGNokomireason__c", "Gassituation__c", "GasNGNokomireason__c",
               "Field116__c", SF_LINK_FIELD, "RENXA__c",
               "Studycategory__c", "Powerstudyreasons__c", "Gasstudyreasons__c",
               "LLsaikoru__c", "LLretime__c", "saicoolday__c", "Field89__c",
               "Account.Name", "Account.Pack__c")
_ACC_FIELDS = ("LastName", "FirstName", "Phoneticlastname3__c", "Phoneticname3__c", "Phonetic3__c",
               "Phone", "Field7__c", "mailaddress__c", "Field172__c", "BillingPostalCode", "BillingState",
               "BillingCity", "BillingStreet", "Field11__c", "Buildingname3__c", "Roomnumberhalfwidth3__c", "LP__c")


def _norm(s) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def _open(gc, url):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def _clean(v) -> str:
    """ロボットの呪文（Pythonの文字列）に差し込むので、壊す文字を置き換える。"""
    s = str(v if v is not None else "").strip()
    if s.lower() in ("none", "nan"):
        return ""
    return (s.replace("\\", "＼").replace('"', "”").replace("'", "’")
            .replace("\r", "").replace("\n", " ").replace("{", "｛").replace("}", "｝"))


# ==========================================
# 設定（Supabase の予約行）
# ==========================================
def load(supabase) -> dict:
    import auto_jobs
    return auto_jobs.load_row(supabase, SETTINGS_ID)


def save(supabase, part: dict) -> dict:
    """⚠️ 書く直前に読み直して、触ったところだけ変える。"""
    row = load(supabase)
    row.update(part)
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": SETTINGS_NAME, "is_active": False,
        "connector_type": "settings", "config_json": row}).execute()
    return row


def robot_name(cfg: dict) -> str:
    return str(cfg.get("robot", "") or DEFAULT_ROBOT).strip()


def account_url(login_url: str) -> str:
    """ログインのURL（…/login?backUrl=…）から、ログインしたあとに開く画面（ユーザーページ）を取り出す。"""
    try:
        q = parse_qs(urlparse(str(login_url or "")).query)
        return unquote((q.get("backUrl") or [""])[0])
    except Exception:
        return ""


# ==========================================
# ① 更新・② 読む
# ==========================================
def refresh(gc, cfg: dict):
    """① SFコネクタで BOX を更新する。→ (ok, 理由)"""
    import auto_jobs
    import sms_runner
    url = str(cfg.get("sheet_url", "") or "").strip()
    robot = str(cfg.get("refresh_robot", "") or auto_jobs.DEFAULT_REFRESH_ROBOT).strip()
    urls = sms_runner.tab_urls_for(url, [REPORT_TAB], auto_jobs.tab_gids(gc, url))
    folder = sms_runner.work_dir(WORK_ROOT, "更新")
    ok, log = sms_runner.run_sheet_refresh(robot, folder, tabs=[REPORT_TAB], tab_urls=urls, url=url)
    return ok, ("" if ok else (sms_runner.stop_reason(log) or log[-300:]))


def box_ids(gc, url: str):
    """BOX の案件 ID の並び。→ (ids, 案件IDが空の行の数, エラー)"""
    ws = _open(gc, url).worksheet(REPORT_TAB)
    view = ws.get_values()
    if not view or not any(str(c).strip() for c in view[0]):
        return [], 0, f"「{REPORT_TAB}」が空です（見出しもありません）。SFコネクタの設定を確かめてください"
    head = [str(h).strip() for h in view[0]]
    if ID_COL not in head:
        return [], 0, f"「{REPORT_TAB}」に「{ID_COL}」の列がありません（見出し：{'、'.join(head[:8])}…）"
    c = head.index(ID_COL)
    ids, noid, seen = [], 0, set()
    for r in view[1:]:
        if not any(str(x).strip() for x in r):
            continue
        v = str(r[c] if c < len(r) else "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?", v):
            noid += 1
            continue
        if v[:15] in seen:
            continue
        seen.add(v[:15])
        ids.append(v)
    return ids, noid, ""


def sf_cases(ids) -> dict:
    """案件 ID（15桁）→ Salesforce の案件の中身。読めなければ例外。"""
    import salesforce_loader as sfl
    sf = sfl.connect()
    fields = ", ".join(_OPP_FIELDS + tuple(f"Account__r.{f}" for f in _ACC_FIELDS))
    out = {}
    ids = [str(i).strip() for i in ids if str(i).strip()]
    for i in range(0, len(ids), 150):
        q = ", ".join("'" + x.replace("'", "") + "'" for x in ids[i:i + 150])
        for r in sf.query_all(f"SELECT {fields} FROM {SF_OBJECT} WHERE Id IN ({q})")["records"]:
            out[r["Id"][:15]] = r
    return out


# ==========================================
# フォームの値を決める（ルール）
# ==========================================
def _split2(text):
    """「A B」→ ("A", "B")。区切りが無ければ (全体, "")。"""
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or "")).strip())
    if " " not in s:
        return s, ""
    a, b = s.split(" ", 1)
    return a, b


def _phone3(text):
    s = unicodedata.normalize("NFKC", str(text or "")).strip()
    parts = [p for p in re.split(r"[-ー－‐ｰ\s()（）]+", s) if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return parts
    d = re.sub(r"\D", "", s)
    if len(d) == 11 and d[:3] in ("070", "080", "090", "050"):
        return [d[:3], d[3:7], d[7:]]
    return None


def _ymd(v) -> str:
    s = str(v or "").strip()
    m = re.match(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", s)
    return f"{int(m.group(1))}/{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else ""


def lp_info(text, acc_lp=""):
    """LPガス情報（「LPガス会社名：…／連絡先：…」の形）から (会社名, 連絡先)。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    company, tel = "", ""
    for line in re.split(r"[\r\n]+", s):
        m = re.match(r"\s*([^:：]*?)\s*[:：]\s*(.*)$", line)
        if not m:
            continue
        label, val = m.group(1), m.group(2).strip()
        if "連絡" in label or "電話" in label or "TEL" in label.upper():
            tel = tel or val
        elif "会社" in label or "ガス" in label:
            company = company or val
    if not company:
        company = str(acc_lp or "").strip()
    return company, tel


def decide(sfr: dict, cfg: dict) -> dict:
    """SFのフラグから 電気・ガス・水道・ネットの種別を決める。→ {種別…, "理由": [...]}"""
    why = []
    acc = sfr.get("Account__r") or {}
    pack = str((sfr.get("Account") or {}).get("Pack__c") or "")

    # ⚡ 電気
    ps, pr = str(sfr.get("Powersituation__c") or ""), str(sfr.get("PowerNGNokomireason__c") or "")
    if ps in GUIDE_LL:
        elec = "個別契約"
    elif "オール電化" in pr:
        elec = "オール電化"
    else:
        elec = "案内不要"
        why.append(f"電気：{ps or '空'}{'（' + pr + '）' if pr else ''}→案内不要")

    # 🔥 ガス
    gs, gr = str(sfr.get("Gassituation__c") or ""), str(sfr.get("GasNGNokomireason__c") or "")
    lp_c, lp_t = lp_info(sfr.get("Field116__c"), acc.get("LP__c"))
    if gs in GUIDE_LL:
        gas = "都市ガス"
    elif "LP" in gr.upper() or (gs == "案内不要" and lp_c):
        gas = "プロパンガス"
        why.append(f"ガス：{gs}（{gr or 'LPガス情報あり'}）→プロパンガス")
    elif "オール電化" in gr:
        gas = "オール電化"
    else:
        gas = "案内不要"
        why.append(f"ガス：{gs or '空'}{'（' + gr + '）' if gr else ''}→案内不要")

    # 🌐 ネット
    ns, nr = str(sfr.get("StageName") or ""), str(sfr.get("NGcategory__c") or "")
    if ns in GUIDE_NET:
        net = "個別契約"
    elif "無料ネット" in nr:
        net = "無料ネット（ネット案内不要）"
    else:
        net = "案内不要"
        why.append(f"ネット：{ns or '空'}{'（' + nr + '）' if nr else ''}→案内不要")

    # 💧 水道（SFにフラグが無いので契約Packで）
    rule = str(cfg.get("water_rule") or "ll")
    if rule == "always":
        water = "個別契約"
    elif rule == "never":
        water = "案内不要"
    else:
        water = "個別契約" if (not pack or "電気" in pack or "ガス" in pack) else "案内不要"
    if water == "案内不要":
        why.append(f"水道：契約Pack「{pack or '未設定'}」→案内不要")
    return {"電気の種別": elec, "ガスの種別": gas, "水道の種別": water,
            "インターネットの種別": net,
            "プロパンガス会社名": lp_c if gas == "プロパンガス" else "",
            "プロパンガス連絡先": lp_t if gas == "プロパンガス" else "",
            "理由": why}


def build(sfr: dict, cfg: dict, staff: str) -> dict:
    """1案件ぶんのフォームの値。→ {"vars": {...}, "why": [...]}（足りないものは validate で名指し）"""
    acc = sfr.get("Account__r") or {}
    v = {k: "" for k in VAR_KEYS}
    v["店舗担当者名"] = staff
    v["郵便番号"] = re.sub(r"\D", "", unicodedata.normalize("NFKC", str(acc.get("BillingPostalCode") or "")))
    v["都道府県"] = acc.get("BillingState")
    v["市区町村"] = acc.get("BillingCity")
    v["町域"] = acc.get("BillingStreet")
    v["番地"] = acc.get("Field11__c")
    v["物件名"] = acc.get("Buildingname3__c")
    v["部屋番号"] = acc.get("Roomnumberhalfwidth3__c")
    d = decide(sfr, cfg)
    for k in ("電気の種別", "ガスの種別", "水道の種別", "インターネットの種別", "プロパンガス会社名", "プロパンガス連絡先"):
        v[k] = d[k]
    reasons = " ".join(str(sfr.get(k) or "") for k in ("Studycategory__c", "Powerstudyreasons__c", "Gasstudyreasons__c"))
    v["申込種別"] = APPLY_MAIL if MAIL_REASON in reasons else APPLY_PHONE
    v["国籍"] = NATIONALITY
    v["言語"], lang_why = form_lang(sfr.get("RENXA__c"))
    # 名前：姓・名が分かれていなければ、最初の空白で分ける（外国籍の方はローマ字1欄のことが多い）
    last, first = str(acc.get("LastName") or "").strip(), str(acc.get("FirstName") or "").strip()
    if not first:
        last, first = _split2(last)
    v["姓"], v["名"] = last, first
    k1, k2 = str(acc.get("Phoneticlastname3__c") or "").strip(), str(acc.get("Phoneticname3__c") or "").strip()
    if not k2:
        k1, k2 = _split2(k1 or acc.get("Phonetic3__c"))
    # 2つに分けられないフリガナ（1語だけ・空）は、姓・名をそのまま入れる（担当者のいつものやり方）
    if not (k1 and k2) and str(cfg.get("kana_rule") or "same") == "same":
        k1, k2 = v["姓"], v["名"]
    v["セイ"], v["メイ"] = k1, k2
    ph = _phone3(acc.get("Phone"))
    if ph:
        v["電話番号1"], v["電話番号2"], v["電話番号3"] = ph
    v["生年月日"] = _ymd(acc.get("Field7__c"))
    v["入居日"] = _ymd(acc.get("Field172__c"))
    v["メールアドレス"] = acc.get("mailaddress__c")
    # ご連絡日時：LL再コール → 再コール の順に見る
    for dk, tk in (("LLsaikoru__c", "LLretime__c"), ("saicoolday__c", "Field89__c")):
        if sfr.get(dk):
            t = str(sfr.get(tk) or "")[:5]
            v["ご連絡日時"] = f"{_ymd(sfr.get(dk))} {t}".strip() + " 以降にご連絡をお願いします" if t else \
                f"{_ymd(sfr.get(dk))} にご連絡をお願いします"
            break
    shop = _norm((sfr.get("Account") or {}).get("Name"))
    if _norm(TOKYO_COMFORT) in shop:
        v["その他申し送り事項"] = TOKYO_COMFORT
    return {"vars": {k: _clean(x) for k, x in v.items()}, "why": d["理由"] + ([lang_why] if lang_why else [])}


def form_lang(sf_lang):
    """SFの「言語_RENXA連携」→ フォームのグローバルでチェックする言語。→ (言語, 理由)。空・フォームに無い言語は英語。"""
    s = unicodedata.normalize("NFKC", str(sf_lang or "")).strip("、, ")
    if not s:
        return DEFAULT_LANG, "言語：SFが空→英語"
    s = LANG_MAP.get(s, s)
    if s in FORM_LANGS:
        return s, ""
    return DEFAULT_LANG, f"言語：{s}はフォームに無い→英語"


def validate(v: dict) -> list:
    """フォームに入れられない理由（空なら送れる）。"""
    bad = []
    if not str(v.get("店舗担当者名", "")).strip():
        bad.append("店舗担当者名が空")
    if v.get("申込種別") not in APPLY_OPTS:
        bad.append(f"申込種別「{v.get('申込種別') or '空'}」はフォームの選択肢にありません")
    if str(v.get("申込種別", "")).startswith("外国語"):
        if v.get("言語") not in FORM_LANGS:
            bad.append(f"言語「{v.get('言語') or '空'}」はフォームのグローバルにありません")
        if not str(v.get("国籍", "")).strip():
            bad.append("国籍が空")
    if not re.fullmatch(r"\d{7}", str(v.get("郵便番号", ""))):
        bad.append(f"郵便番号が7桁ではありません（{v.get('郵便番号') or '空'}）")
    if not str(v.get("都道府県", "")).strip():
        bad.append("都道府県が空")
    for k in ("姓", "名"):
        if not str(v.get(k, "")).strip():
            bad.append(f"{k}が空（姓と名に分けられませんでした）")
    for k in ("セイ", "メイ"):
        if not str(v.get(k, "")).strip():
            bad.append(f"{k}（フリガナ）が空")
    if not all(str(v.get(f"電話番号{i}", "")).strip() for i in (1, 2, 3)):
        bad.append("電話番号を3つに分けられません")
    for k, opts in (("電気の種別", ELEC_OPTS), ("ガスの種別", GAS_OPTS),
                    ("水道の種別", WATER_OPTS), ("インターネットの種別", NET_OPTS)):
        if v.get(k) not in opts:
            bad.append(f"{k}「{v.get(k) or '空'}」はフォームの選択肢にありません")
    if v.get("ガスの種別") == "プロパンガス":
        if not str(v.get("プロパンガス会社名", "")).strip():
            bad.append("プロパンガス会社名が空")
        if not re.search(r"\d", str(v.get("プロパンガス連絡先", ""))):
            bad.append("プロパンガスの連絡先が空（不動産に確認してから連携）")
    return bad


def case_label(it: dict) -> str:
    """Slack・画面に出す呼び名。⚠️ 個人名は出さない（案件番号、無ければ案件 ID）。"""
    return str(it.get("no") or it.get("id"))


def plan(gc, cfg: dict, staff: str) -> dict:
    """BOX と Salesforce を読み、送る案件を決める（送らない）。

    → {"items": [{"id","no","vars","why","problems"}], "linked": [..], "not_target": [..],
       "held": [..（控えが残っている）], "missing": [..（SFに無い）], "noid": 件数, "error": ""}
    """
    out = {"items": [], "linked": [], "not_target": [], "held": [], "missing": [], "noid": 0, "error": ""}
    url = str(cfg.get("sheet_url", "") or "").strip()
    ids, out["noid"], err = box_ids(gc, url)
    if err:
        out["error"] = err
        return out
    if not ids:
        return out
    try:
        cases = sf_cases(ids)
    except Exception as e:
        out["error"] = f"Salesforce から案件を読めませんでした：{str(e)[:200]}"
        return out
    sent = dict(cfg.get("sent") or {})
    for x in ids:
        r = cases.get(x[:15])
        if not r:
            out["missing"].append(x)
            continue
        no = str(r.get("ProposalNumber__c") or "")
        link = str(r.get(SF_LINK_FIELD) or "")
        if link == LINKED:
            out["linked"].append(no or x)
            continue
        if link == NOT_TARGET:
            out["not_target"].append(no or x)
            continue
        if x[:15] in {k[:15] for k in sent}:
            out["held"].append(no or x)
            continue
        b = build(r, cfg, staff)
        out["items"].append({"id": r["Id"], "no": no, "vars": b["vars"], "why": b["why"],
                             "problems": validate(b["vars"])})
    return out


# ==========================================
# 🤖 ロボットの手順書（フォームの入力）
# ==========================================
def _after(label: str, tag: str = "input") -> str:
    """見出しの文字のすぐあとにある入力欄（xpath）。"""
    return f"xpath=//*[normalize-space(text())='{label}']/following::{tag}[1]"


def _fill_code(label, var, tag="input"):
    return f'page.locator("{_after(label, tag)}").first.fill("{{{var}}}")'


def _radio_code(group, var):
    # ⭐ 見出し（電気の種別 など）より**あと**にある、その文字の選択肢を押す。
    #    「個別契約」「案内不要」は電気・水道・ネットの全部にあるので、文字だけで押すと別の欄を選ぶ。
    return (f'_g = page.locator("xpath=//*[normalize-space(text())=\'{group}\']'
            f'/following::*[normalize-space(.)=\'{{{var}}}\'][1]").first\n'
            f'_g.click()\n'
            f'page.wait_for_timeout(300)')


def _check_code(label):
    # チェックボックス：見出し（英語 など）のあとの最初のチェック。見た目だけの部品で本体が隠れていても入るように
    return (f'_c = page.locator("xpath=//*[normalize-space(text())=\'{label}\']'
            f'/following::input[@type=\'checkbox\'][1]").first\n'
            'if not _c.is_checked():\n'
            '    try:\n'
            '        _c.check(force=True)\n'
            '    except Exception:\n'
            '        pass\n'
            'if not _c.is_checked():\n'
            '    _c.locator("xpath=ancestor::label[1]").click()\n'
            'if not _c.is_checked():\n'
            f'    raise Exception("「{label}」にチェックを入れられませんでした")')


def _dropdown_code(label, value):
    # プルダウン：ふつうの <select> ならそのまま、作り物（クリックで一覧が出る）なら一覧から選ぶ
    return (f'_s = page.locator("{_after(label, "select")}")\n'
            f'_i = page.locator("{_after(label)}").first\n'
            f'if _s.count() and _s.first.is_visible():\n'
            f'    _s.first.select_option(label="{value}")\n'
            f'else:\n'
            f'    _i.click()\n'
            f'    page.wait_for_timeout(600)\n'
            f'    _o = page.locator("xpath=//li[normalize-space(.)=\'{value}\'] | '
            f'//*[@role=\'option\'][normalize-space(.)=\'{value}\']").locator("visible=true").first\n'
            f'    _o.click()\n'
            f'    page.wait_for_timeout(300)\n'
            f'    if "{value}" not in (_i.input_value() or ""):\n'
            f'        raise Exception("{label}で「{value}」を選べませんでした")')


def build_steps(cfg: dict):
    """RENXA のフォームに入れる手順書と分岐ルール。→ (steps, conditions)

    ⭐ 押す場所は、見出しの文字から探す（録画の場所に頼らない）。呪文が空振りしたときに、
       対象の文字（【電気の種別】個別契約 など）で別の欄を押さないよう、対象は画面に無い書き方にしてある。
    """
    login_mail = str(cfg.get("login_mail") or DEFAULT_LOGIN_MAIL).strip()
    code = str(cfg.get("company_code") or DEFAULT_COMPANY_CODE).strip()
    form_url = str(cfg.get("form_url") or "").strip()
    acc_url = account_url(cfg.get("login_url"))
    S = []

    def add(op, tgt, val="", ai="", when="常に", empty="", marker=""):
        S.append({"順番": len(S) + 1, "いつ": when, "操作": op, "対象": tgt, "値": val,
                  "変換": "", "ai_code": ai, "空のとき": empty, "目印": marker})

    # 🔐 ログイン（メールのリンク）。ログイン済みの日は丸ごと飛ばす（robot.py の _login_needed）
    add("クリック", "メールアドレスでログイン",
        ai='page.get_by_text("メールアドレスでログイン").first.click()', marker="メールアドレスでログイン")
    add("文字を入力", "メールアドレス", val=login_mail,
        ai=f'page.locator("input[type=email], input[type=text]").first.fill("{login_mail}")')
    # ⚠️ 対象に「送信」と書くと、お試しの見張りが送信の手順とみなして止める（ログインのメールを出すだけ）
    add("クリック", "ログイン用のメールを出すボタン",
        ai='page.get_by_role("button", name="送信").first.click()')
    add("メールのリンクを開く", "ログイン用のメール", val=robot_name(cfg))

    # 🧭 フォームへ（周ごとに開き直す＝2件目からも同じ道）
    if form_url:
        add("ページを開く", "不動産個人情報取得フォーム", val=form_url)
    else:
        if acc_url:
            add("ページを開く", "ユーザーページ", val=acc_url)
        add("クリック", "Renxa株式会社", ai='page.get_by_text("Renxa株式会社").first.click()')
        add("クリック", "お客様対応状況一覧", ai='page.get_by_text("お客様対応状況一覧").first.click()')
        add("クリック", "不動産個人情報取得フォーム", ai='page.get_by_text("不動産個人情報取得フォーム").first.click()')

    # 1枚目：店舗
    add("クリック", "【フォーム入力種別】情報連携", ai=_dropdown_code("フォーム入力種別", "情報連携"))
    add("文字を入力", "店舗担当者名", val="{店舗担当者名}", ai=_fill_code("店舗担当者名", "店舗担当者名"), empty="止める")
    add("クリック", "【検索】虫眼鏡のボタン",
        ai='page.locator("xpath=//*[contains(normalize-space(text()),\'検索\') and contains(text(),\'▽\')]'
           '/following::button[1]").first.click()\npage.wait_for_timeout(1000)')
    add("クリック", f"【会社コード {code}】選択",
        ai=f'page.locator("tr", has_text="{code}").get_by_text("選択", exact=True).first.click()\n'
           'page.wait_for_timeout(800)')
    add("クリック", "【1枚目】次へ", ai='page.get_by_role("button", name="次へ").first.click()\npage.wait_for_timeout(1500)')

    # 2枚目：物件・設備
    add("クリック", "【管理】他社管理物件",
        ai='page.locator("xpath=//*[normalize-space(text())=\'管理\']/following::*[normalize-space(.)=\'他社管理物件\'][1]").first.click()')
    add("文字を入力", "郵便番号", val="{郵便番号}",
        ai='page.locator("xpath=//*[contains(normalize-space(text()),\'郵便番号（半角数字7桁）\')]/following::input[1]").first.fill("{郵便番号}")',
        empty="止める")
    for lb, var, em in (("都道府県", "都道府県", "止める"), ("市区町村", "市区町村", "飛ばす"), ("町域", "町域", "飛ばす"),
                        ("番地", "番地", "飛ばす"), ("物件名", "物件名", "飛ばす"), ("部屋番号", "部屋番号", "飛ばす")):
        add("文字を入力", lb, val="{" + var + "}", ai=_fill_code(lb, var), empty=em)
    add("クリック", "【電気の種別】{電気の種別}", ai=_radio_code("電気の種別", "電気の種別"))
    add("クリック", "【ガスの種別】{ガスの種別}", ai=_radio_code("ガスの種別", "ガスの種別"))
    add("文字を入力", "プロパンガス会社名", val="{プロパンガス会社名}", when="プロパンガスのとき",
        ai=_fill_code("プロパンガス会社名", "プロパンガス会社名"), empty="止める")
    # ⚠️ フォームの見出しは「プロパンガス連携先」（連絡先の書き違い）。「プロパンガス連」で探す
    add("文字を入力", "プロパンガス連絡先", val="{プロパンガス連絡先}", when="プロパンガスのとき",
        ai='page.locator("xpath=//*[starts-with(normalize-space(text()),\'プロパンガス連\')]/following::input[1]").first.fill("{プロパンガス連絡先}")',
        empty="止める")
    add("クリック", "【水道の種別】{水道の種別}", ai=_radio_code("水道の種別", "水道の種別"))
    add("クリック", "【インターネットの種別】{インターネットの種別}", ai=_radio_code("インターネットの種別", "インターネットの種別"))
    add("クリック", "【2枚目】次へ", ai='page.get_by_role("button", name="次へ").first.click()\npage.wait_for_timeout(1500)')

    # 3枚目：連絡先
    add("クリック", "【申込種別】{申込種別}", ai=_dropdown_code("申込種別", "{申込種別}"))
    for lb in ("姓", "名", "セイ", "メイ"):
        add("文字を入力", lb, val="{" + lb + "}", ai=_fill_code(lb, lb), empty="止める")
    for i in (1, 2, 3):
        add("文字を入力", f"電話番号{i}", val=f"{{電話番号{i}}}", ai=_fill_code(f"電話番号{i}", f"電話番号{i}"), empty="止める")
    add("日付を入れる", "生年月日", val="{生年月日}", ai=f'page.locator("{_after("生年月日")}").first.click()', empty="飛ばす")
    add("日付を入れる", "入居日/入居予定日", val="{入居日}",
        ai=f'page.locator("{_after("入居日/入居予定日")}").first.click()', empty="飛ばす")
    add("文字を入力", "メールアドレス（お客様）", val="{メールアドレス}",
        ai='page.locator("xpath=(//*[normalize-space(text())=\'メールアドレス\'])[last()]/following::input[1]").first.fill("{メールアドレス}")',
        empty="飛ばす")
    # 🌐 グローバル（申込種別が外国語のときだけ出る欄）：国籍＝不明、言語にチェック
    add("文字を入力", "国籍", val="{国籍}", when="外国語のとき", ai=_fill_code("国籍", "国籍"), empty="止める")
    add("クリック", "【グローバル】{言語}", when="外国語のとき", ai=_check_code("{言語}"))
    add("文字を入力", "ご連絡日時に関する申し送り事項", val="{ご連絡日時}",
        ai=_fill_code("ご連絡日時に関する申し送り事項", "ご連絡日時", "textarea"), empty="飛ばす")
    add("文字を入力", "その他申し送り事項", val="{その他申し送り事項}",
        ai=_fill_code("その他申し送り事項", "その他申し送り事項", "textarea"), empty="飛ばす")
    add("クリック", "【同意】チェック", ai='page.get_by_text("同意", exact=True).last.click()')
    add("クリック", "確認", ai='page.get_by_role("button", name="確認").first.click()\npage.wait_for_timeout(1500)')
    # 🚀 最後の一押し。お試しでは押さない
    add("クリック", "回答", ai='page.get_by_role("button", name="回答").first.click()', when="送信（本番のみ）")
    add("待つ", "", val="3", when="送信のあと")

    conditions = [{"name": "プロパンガスのとき", "logic": "AND",
                   "rules": [{"col": "ガスの種別", "op": "eq", "value": "プロパンガス"}]},
                  {"name": "外国語のとき", "logic": "AND",
                   "rules": [{"col": "申込種別", "op": "contains", "value": "外国語"}]}]
    return S, conditions


def install_robot(supabase, cfg: dict) -> str:
    """ロボット「RENXA」を作る／手順書を作り直す。ほかの設定（メールの認証・ブラウザ）は残す。→ 結果の文"""
    name = robot_name(cfg)
    login_url = str(cfg.get("login_url") or "").strip()
    if not login_url.startswith("http"):
        raise RuntimeError("⚙️ 設定に、ログインのURL（account.kintoneapp.com/login?backUrl=…）を入れてください")
    steps, conds = build_steps(cfg)
    res = supabase.table("merchants").select("*").eq("id", name).execute().data
    old = (res[0].get("config_json") or {}) if res else {}
    rc = dict(old.get("robot_config") or {})
    rc.update({"target_url": login_url, "steps": steps})
    rc.setdefault("mail_wait_sec", 300)
    new = {**old, "product_type": "その他", "robot_config": rc, "conditions": conds,
           "entry_group": "other", "renxa": True}
    new.setdefault("spreadsheet", {})
    new.setdefault("notifications", {})
    supabase.table("merchants").upsert({
        "id": name, "name": name, "is_active": False, "connector_type": "playwright",
        "config_json": new}).execute()
    return f"ロボット「{name}」の手順書を{'作り直し' if res else '作り'}ました（{len(steps)}手順）"


# ==========================================
# ③ フォームに入れる ・ ④ 連携済みにする
# ==========================================
def send(supabase, cfg: dict, items, submit: bool):
    """ロボットで、ブラウザ1回・ログイン1回のまま全件を入れる。→ [{"id","no","ok","submitted","reason","log"}]

    ⚠️ 本番（submit）は、動かす**前に**案件IDを控え（sent）に入れる（押したあとで落ちても二重に送らない）。
    """
    import sms_runner
    if not items:
        return []
    rounds = [{"label": f"{case_label(it)}", "vars": {k: it["vars"].get(k, "") for k in VAR_KEYS}} for it in items]
    if submit:
        _mark_sent(supabase, [it["id"] for it in items], "送信前")
    folder = sms_runner.work_dir(WORK_ROOT, "フォーム")
    try:
        _ok, _tail, res = sms_runner._run_rounds(robot_name(cfg), folder, rounds, submit,
                                                 int(cfg.get("sec_per_case") or 600), "RENXAに入れる.json", "renxa.log")
    except Exception as e:
        res = [{"ok": False, "submitted": False, "reason": f"ロボットを動かせませんでした：{str(e)[:200]}",
                "log": "", "done": False} for _ in items]
    out = []
    for it, r in zip(items, res):
        out.append({"id": it["id"], "no": it.get("no", ""), "ok": bool(r.get("ok")),
                    "submitted": bool(r.get("submitted")), "reason": str(r.get("reason") or ""),
                    "log": str(r.get("log") or "")})
    if submit:
        # 回答まで行かずに止まった案件は、何も送っていないので控えから外す。
        # 回答を押したのに止まった案件は「要確認」で残す（人が確かめる）
        _unmark([x["id"] for x in out if not x["submitted"]], supabase)
        _mark_sent(supabase, [x["id"] for x in out if x["submitted"] and not x["ok"]], "要確認")
        _mark_sent(supabase, [x["id"] for x in out if x["submitted"] and x["ok"]], "回答済み")
    return out


def _mark_sent(supabase, ids, state):
    if not ids:
        return
    sent = dict(load(supabase).get("sent") or {})
    now = time.strftime("%Y/%m/%d %H:%M")
    for i in ids:
        sent[str(i)] = {"at": now, "state": state}
    save(supabase, {"sent": sent})


def _unmark(ids, supabase):
    if not ids:
        return
    sent = dict(load(supabase).get("sent") or {})
    for i in ids:
        sent.pop(str(i), None)
    save(supabase, {"sent": sent})


def forget(supabase, ids):
    """人が確かめて「送られていなかった」とした案件の控えを消す（次の実行で送り直す）。"""
    _unmark(ids, supabase)


def mark_linked(ids) -> dict:
    """「多言語窓口連携状況」を「連携済み」にする（Data Loader の代わり）。

    ⚠️ 送るのはこの1項目だけ。「対象外」が入っている案件は上書きしない（held）。
    → {"ok": [id…], "held": [(id, 今の値)…], "ng": [(id, 理由)…]}
    """
    import salesforce_loader as sfl
    ids = [str(i).strip() for i in ids if str(i).strip()]
    out = {"ok": [], "held": [], "ng": []}
    if not ids:
        return out
    try:
        sf = sfl.connect()
        cur = {}
        for i in range(0, len(ids), 200):
            q = ", ".join("'" + x.replace("'", "") + "'" for x in ids[i:i + 200])
            for r in sf.query_all(f"SELECT Id, {SF_LINK_FIELD} FROM {SF_OBJECT} WHERE Id IN ({q})")["records"]:
                cur[r["Id"][:15]] = r.get(SF_LINK_FIELD) or ""
    except Exception as e:
        out["ng"] = [(x, f"Salesforceを読めませんでした：{str(e)[:150]}") for x in ids]
        return out
    todo = []
    for x in ids:
        if x[:15] not in cur:
            out["ng"].append((x, "Salesforceに案件が見つかりません"))
        elif cur[x[:15]] == LINKED:
            out["ok"].append(x)
        elif cur[x[:15]] == NOT_TARGET:
            out["held"].append((x, cur[x[:15]]))
        else:
            todo.append(x)
    if todo:
        res = sfl.upsert(sf, SF_OBJECT, "Id", [{"Id": x, SF_LINK_FIELD: LINKED} for x in todo])
        errs = res.get("errors") or []
        bad = {str(e.get("Id", "")): str(e.get("原因", "") or "投入に失敗しました") for e in errs}
        whole = any("〜" in k for k in bad) or (res.get("ng", 0) > len(errs))
        for x in todo:
            if whole or x in bad:
                out["ng"].append((x, bad.get(x, "投入に失敗しました")[:200]))
            else:
                out["ok"].append(x)
    return out


def sf_step(supabase, ids=None):
    """回答済みの案件を「連携済み」にする。控えの「回答済み」も一緒に入れ直す。→ (印, 中身)

    ⭐ 連携済みにできた案件は控えから消す（以後は Salesforce の「連携済み」が二重送信を防ぐ）。
    """
    sent = dict(load(supabase).get("sent") or {})
    todo = list(dict.fromkeys([str(i) for i in (ids or [])]
                              + [k for k, v in sent.items() if (v or {}).get("state") == "回答済み"]))
    if not todo:
        return "⏹", "連携済みにする案件はありません"
    r = mark_linked(todo)
    _unmark(r["ok"], supabase)
    body = f"多言語窓口連携状況を「{LINKED}」にしました：{len(r['ok'])}件"
    if r["held"]:
        body += "／🛡 「対象外」が入っていたので上書きしていません：" + "、".join(i for i, _ in r["held"][:10])
    if r["ng"]:
        body += "／⚠️ 入らなかった（次の実行で入れ直します）：" + "、".join(f"{i}（{m}）" for i, m in r["ng"][:10])
    return ("🛑" if r["ng"] else ("🛡" if r["held"] else "✅")), body


def after_send(supabase, results, steps, submit: bool):
    """ロボットの結果を工程に足し、回答まで行った案件だけ連携済みにする（画面と時間指定で共用）。"""
    ok = [x for x in results if x["ok"]]
    ng = [x for x in results if not x["ok"]]
    what = "回答しました" if submit else "回答の手前まで入れました（お試し）"
    body = f"{len(ok)}件 {what}" + (("：" + "、".join(case_label(x) for x in ok[:20])) if ok else "")
    if ng:
        body += "／⚠️ 止まった：" + "、".join(
            f"{case_label(x)}（{x['reason'][:80] or '止まりました'}{'・回答は押しました＝要確認' if x['submitted'] else ''}）"
            for x in ng[:10])
    steps.add("③ RENXAのフォームに入れる", "🛑" if ng else "✅", body)
    try:
        save(supabase, {"last_run": {"at": time.strftime("%Y/%m/%d %H:%M"), "submit": submit,
                                     "ok": [case_label(x) for x in ok], "ng": [case_label(x) for x in ng]}})
    except Exception:
        pass
    if submit:
        steps.add("④ Salesforceの多言語窓口連携状況", *sf_step(supabase, [x["id"] for x in results
                                                                  if x["ok"] and x["submitted"]]))


def run(supabase, gc, cfg: dict, do_refresh: bool = True, do_send=None, staff: str = "") -> dict:
    """①更新 → ②送る案件を決める → ③フォームに入れる → ④連携済み。画面の「▶ ぜんぶ実行」と時間指定が通る。

    do_send＝None なら設定の `auto_send` に従う（OFFなら ⏸ で止めて知らせる）。
    ⚠️ 更新に失敗したら送らない（古いBOXのまま送ると、取りこぼしや対象外の送信に気づけない）。
    """
    import auto_jobs
    steps = auto_jobs._Steps()
    url = str(cfg.get("sheet_url", "") or "").strip()
    if not (url and gc):
        steps.add("準備", "🛑", "スプレッドシートのURL、または接続キーが未設定です")
        return steps.result()
    staff = str(staff or cfg.get("auto_staff") or DEFAULT_STAFF).strip()
    if do_refresh:
        ok, why = refresh(gc, cfg)
        if steps.add("① BOXの更新", "✅" if ok else "🛑",
                     f"「{REPORT_TAB}」を更新しました" if ok else why) == "🛑":
            return steps.result()
    try:
        p = plan(gc, cfg, staff)
    except Exception as e:
        steps.add("② 送る案件", "🛑", f"シートを読めませんでした：{str(e)[:200]}")
        return steps.result()
    if p["error"]:
        steps.add("② 送る案件", "🛑", p["error"])
        return steps.result()
    extra = ""
    if p["linked"]:
        extra += f"（連携済みの {len(p['linked'])}件は飛ばしました）"
    if p["not_target"]:
        extra += f"（対象外の {len(p['not_target'])}件は飛ばしました）"
    if p["held"]:
        extra += f"\n⚠️ 前に送った控えが残っている {len(p['held'])}件は送っていません（画面で確かめてください）：" + "、".join(p["held"][:10])
    if p["missing"]:
        extra += f"\n⚠️ Salesforceに見つからない案件ID：" + "、".join(p["missing"][:10])
    good = [it for it in p["items"] if not it["problems"]]
    bad = [it for it in p["items"] if it["problems"]]
    if bad:
        extra += "\n⚠️ 入れられない（画面で直してください）：" + "／".join(
            f"{case_label(it)}：{'・'.join(it['problems'])}" for it in bad[:10])
    if not good:
        steps.add("② 送る案件", "⏸" if bad or p["held"] else "⏹",
                  "送れる案件は0件でした" + extra)
        if dict(load(supabase).get("sent") or {}):
            steps.add("④ Salesforceの多言語窓口連携状況（前回の残り）", *sf_step(supabase, []))
        return steps.result()
    steps.add("② 送る案件", "✅", f"{len(good)}件：" + "、".join(case_label(it) for it in good[:20]) + extra)
    go = bool(cfg.get("auto_send")) if do_send is None else bool(do_send)
    if not go:
        steps.add("③ RENXAのフォームに入れる", "⏸",
                  f"{len(good)}件 あります。「エントリー業務自動化 → 🌐 RENXA」で中身を見て入れてください"
                  "（「時間指定で回答まで自動」がOFF）")
        return steps.result()
    after_send(supabase, send(supabase, cfg, good, submit=True), steps, submit=True)
    if bad:
        steps.add("② 入れられなかった案件", "⏸", "画面で直してから入れてください：" + "、".join(case_label(it) for it in bad[:20]))
    return steps.result()
