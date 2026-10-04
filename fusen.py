"""
📞 付箋架電の中身（画面は pages/13_📞_付箋架電.py）

【困っていたこと】
  スプシの「ネット付箋」などは、F列から右が FILTER の数式で【SF】のシートから自動で出て、
  A〜D列（対応者／対応中／不出／完了）は人が手で入れている。
  ⚠️ SFコネクタで更新すると数式側の並びが変わるのに、手で入れたチェックは同じ行に残る
     ＝ **チェックが別の案件に付く**（1件消えると、下が全部1行ずつずれる）。

【この画面の考え方】
  ⭐ **チェックは行の番号ではなく、案件IDに結びつけて持つ**（更新しても、ずれない）。
  ⭐ **スプシの中身は Supabase に入れない**。画面に出すためにその場で読むだけ。
     Supabase に置くのは「案件ID・状態・メモ・だれが・いつ」だけ。
  ⭐ **ほかのPCの操作が数秒で見える**：画面の表だけを数秒おきに書き直し、
     そのとき読むのは Supabase の小さな行だけ（スプシは読まない）。
     ⚠️ スプシを数秒おきに読むと、Google の読み取り回数の上限（全PC・全ロボットで共有）に当たり、
        ほかの自動実行まで止める。スプシは「更新したとき」と「🔄 を押したとき」だけ読む。
  ⭐ **書く行は人ごとに分ける**（`__fusen__:<セット>:<名前>`）。2人が同時に押しても、
     同じ行を取り合わないので消し合わない。読むときに、案件ごとに**いちばん新しい操作**を採る。
  ⭐ **状態はその日だけ**（架電は日ごとにやり直すため）。前の日の状態は「前回」として見せる。

⚠️ Streamlit を import しない（画面以外からも使えるように）。
"""
import datetime

SETTINGS_ID = "__fusen__"
STATE_PREFIX = "__fusen__:"
ID_COL = "案件 ID"
KEEP_DAYS = 7          # 自分の行に残す日数（それより古い操作は書くときに捨てる）

# スプシ側で人が手で入れていた列。アプリが代わりに持つので、画面には出さない。
MANUAL_COLS = {"対応者", "対応中", "不出", "完了", "対応済", "担当者", "チェック"}

DEFAULT_SETS = {
    "TS用": {
        "sheet_url": "",
        "refresh_tabs": [],
        "tabs": [],
    },
    "総務用": {
        "sheet_url": "",
        "refresh_tabs": ["【SF】ネット付箋", "【SF】LL付箋", "【SF】海外案件"],
        "tabs": [
            {"name": "ネット付箋", "status": ["対応中", "不出", "完了"]},
            {"name": "LL付箋", "status": ["対応中", "不出", "完了"]},
            {"name": "海外案件付箋", "status": ["対応中", "対応済"]},
        ],
    },
}


def today() -> str:
    """日本時間の今日（PCの時計がずれていても日付だけはそろえる）。"""
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d")


def now_stamp() -> str:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")


# ==========================================
# 💾 設定
# ==========================================
def load_cfg(supabase) -> dict:
    res = supabase.table("merchants").select("config_json").eq("id", SETTINGS_ID).execute()
    cfg = (res.data[0].get("config_json") or {}) if res.data else {}
    sets = cfg.get("sets") or {}
    for name, base in DEFAULT_SETS.items():
        merged = dict(base)
        merged.update(sets.get(name) or {})
        sets[name] = merged
    cfg["sets"] = sets
    return cfg


def save_cfg(supabase, part: dict):
    """⚠️ 書く直前に読み直して、渡したところだけ変える（別のPCの変更を消さないため）。"""
    res = supabase.table("merchants").select("config_json").eq("id", SETTINGS_ID).execute()
    latest = (res.data[0].get("config_json") or {}) if res.data else {}
    if "sets" in part:
        sets = latest.get("sets") or {}
        sets.update(part["sets"])
        part = dict(part, sets=sets)
    latest.update(part)
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（付箋架電の設定）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return latest


# ==========================================
# 📄 スプシ（画面に出すために読むだけ。保存しない）
# ==========================================
def _open(gc, url: str):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def read_tabs(gc, url: str, tabs) -> dict:
    """シートたちを **1回の問い合わせで** 読む → {シート名: {"head": [...], "rows": [{...}], "error": ""}}。

    見出しの行は「案件 ID」がある行（LL付箋は2行目にある）。
    人が手で入れていた列（対応者・完了など）は外す。案件IDの無い行も外す。
    """
    out = {}
    tabs = [t for t in tabs if t]
    if not tabs:
        return out
    sh = _open(gc, url)
    res = sh.values_batch_get([f"'{t}'!A1:ZZ" for t in tabs])
    for t, vr in zip(tabs, res.get("valueRanges", [])):
        vals = vr.get("values", []) or []
        hi = next((i for i, r in enumerate(vals[:5]) if ID_COL in [str(c).strip() for c in r]), None)
        if hi is None:
            out[t] = {"head": [], "rows": [], "error": f"見出しに「{ID_COL}」が見つかりません"}
            continue
        head = [str(c).strip() for c in vals[hi]]
        keep = [i for i, h in enumerate(head) if h and h not in MANUAL_COLS]
        id_i = head.index(ID_COL)
        rows, seen = [], set()
        for r in vals[hi + 1:]:
            r = list(r) + [""] * (len(head) - len(r))
            cid = str(r[id_i]).strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            rows.append({head[i]: r[i] for i in keep})
        out[t] = {"head": [head[i] for i in keep], "rows": rows, "error": ""}
    return out


# ==========================================
# 🔁 状態（Supabase。人ごとに1行）
# ==========================================
def _state_id(set_name: str, user: str) -> str:
    return f"{STATE_PREFIX}{set_name}:{user}"


def load_states(supabase, set_name: str) -> dict:
    """そのセットの全員の行を1回で読む → {名前: {案件ID: 操作}}。"""
    pre = f"{STATE_PREFIX}{set_name}:"
    res = supabase.table("merchants").select("id, config_json").like("id", f"{pre}%").execute()
    out = {}
    for r in res.data or []:
        rid = str(r.get("id") or "")
        if not rid.startswith(pre):
            continue
        cj = r.get("config_json") or {}
        out[cj.get("user") or rid[len(pre):]] = cj.get("items") or {}
    return out


def merge(states: dict, day: str = None) -> tuple:
    """案件ごとに、いちばん新しい操作を採る。

    戻り値：(きょうの状態 {案件ID: {...}}, 前回の状態 {案件ID: {...}})
    状態を空に戻した操作（s=""）も「新しい操作」として効く（＝外せる）。
    """
    day = day or today()
    cur, prev = {}, {}
    for user, items in (states or {}).items():
        for cid, it in (items or {}).items():
            e = dict(it, user=user)
            box = cur if it.get("d") == day else prev
            if cid not in box or str(e.get("t", "")) > str(box[cid].get("t", "")):
                box[cid] = e
    prev = {k: v for k, v in prev.items() if v.get("s")}
    return cur, prev


def write_state(supabase, set_name: str, user: str, cid: str, status=None, memo=None):
    """自分の行の、その案件だけを書き換える（ほかの人の行は触らない）。"""
    rid = _state_id(set_name, user)
    res = supabase.table("merchants").select("config_json").eq("id", rid).execute()
    cj = (res.data[0].get("config_json") or {}) if res.data else {}
    items = cj.get("items") or {}
    day = today()
    old = items.get(cid) or {}
    if old.get("d") != day:
        old = {}
    it = {"s": old.get("s", ""), "m": old.get("m", ""), "d": day, "t": now_stamp()}
    if status is not None:
        it["s"] = status
    if memo is not None:
        it["m"] = memo
    items[cid] = it
    cut = (datetime.date.fromisoformat(day) - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    items = {k: v for k, v in items.items() if str(v.get("d", "")) >= cut}
    supabase.table("merchants").upsert({
        "id": rid, "name": f"（付箋架電の記録：{set_name}／{user}）", "is_active": False,
        "connector_type": "settings",
        "config_json": {"user": user, "set": set_name, "items": items}}).execute()


def refresh_folder(set_name: str) -> str:
    import sms_runner
    return sms_runner.work_dir("付箋架電", set_name)


def run_refresh(gc, one_set: dict, set_name: str, robot: str) -> tuple:
    """SFコネクタで更新するシートを、ブラウザ1回で順に更新する → (成功したか, ログ)。"""
    import auto_jobs
    import sms_runner
    url = str(one_set.get("sheet_url", "")).strip()
    tabs = list(one_set.get("refresh_tabs") or [])
    urls = sms_runner.tab_urls_for(url, tabs, auto_jobs.tab_gids(gc, url))
    return sms_runner.run_sheet_refresh(robot, refresh_folder(set_name), tabs=tabs,
                                        tab_urls=urls, url=url)


def stamp_short(t: str) -> str:
    """'2026-10-04 13:05:22' → '13:05'（きょうの分は時刻だけで足りる）。"""
    t = str(t or "")
    return t[11:16] if len(t) >= 16 else t

