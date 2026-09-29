"""
💼 FP連携（一声干渉）（エントリー業務自動化のホームの「💼 FP連携」から開く）。

スプシ「ライフアップ様 リスト」。

【これまでの手作業】
  SFのレポートを「LA貼り付け用」のいちばん下に貼る
  → 「連携分(一声干渉)」の次の行に投入日を打ち、B〜G列の数式（貼り付け用の n 行目を見る）を延ばす
  → 顧客対応備考にFPの希望時間・希望日があれば、I列（電話に出やすい時間）・J列（アポ候補日）に打つ
  → Data Loader で Salesforce の「FP登録日」を入れる。

【ここでやること】
  ① SFコネクタで「LA自動更新」を更新
  ② 貼り付け用にまだ無い案件（A列の案件 ID で見る）だけ選ぶ
  ③ 貼り付け用のいちばん下に足す → 連携分の次の行に A＝投入日（今日）、B〜G＝数式
     （すぐ上の行の数式の行番号だけ差し替える）、I・J＝FPの希望時間・希望日
  ④ 連携分に正しく出た案件だけ、Salesforce の「FP登録日」（`FPentry__c`）に今日を入れる

⭐ **連携分の中身（フリガナ・電話番号…）はスプシの数式が作る**。アプリは数式を延ばすだけで、中身を写さない。
⚠️ 連携分は、途中の行に手で打った値や、先に延ばしてあった数式（別の行番号を見ている）が混ざっている。
   だから「数式が見ている行」を探すのではなく、**A列（投入日）が埋まっている最後の行の次から**書き、
   その行の A〜J に何か出ていたら**1件も書かずに止める**（人が書いたものを上書きしない）。
⚠️ 書いたあとで G列（ユニーク番号＝案件番号）を読み直し、**合っていた案件だけ**登録日を入れる。
⚠️ 数式は列の文字で見ているので、更新したシートと貼り付け用の見出しが違えば足さずに止める。
"""
import datetime as _dt
import re
import time
import unicodedata

SETTINGS_ID = "__fp_toss__"
SETTINGS_NAME = "（FP連携（一声干渉）の設定）"
WORK_ROOT = "FP連携"
REPORT_TAB = "LA自動更新"           # SFコネクタで更新するシート
PASTE_TAB = "LA貼り付け用"          # 連携分の数式が見ているシート（A列＝案件 ID）
LINK_TAB = "連携分(一声干渉)"        # FP側が見るシート
CASE_NO_COL = "案件番号"
FP_DATE_COL = "FP登録日"            # レポートに入っていれば、もう登録済みの案件を飛ばす
NOTE_COL = "顧客対応備考"           # レポートに無ければ Salesforce から読む
LINK_HEADER_ROW = 1
FORMULA_COLS = ("B", "G")           # 連携分で数式を延ばす列
TIME_COL, DAY_COL = "I", "J"        # 電話に出やすい時間／アポ候補日
CHECK_COL = "G"                     # ユニーク番号（LA)＝案件番号。書いたあと読み直す
SF_OBJECT = "Opportunity"
SF_DATE_FIELD = "FPentry__c"        # FP登録日（日付）
SF_NOTE_FIELD = "FormanagementRemarks__c"   # 顧客対応備考（長文なのでIdで引いて読む）

# FPのあと、ここから先は別の話
_CUT_RE = re.compile(r"[★\n\r【〈。]|https?://|マルシェ")
_DAY_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})(?:\s*[(（]?\s*[月火水木金土日祝]\s*[)）]?)?(?:\s*の)?")
# 希望時間らしい手がかり（無ければFPの話ではない＝「FP付帯」など）
_TIME_HINT = re.compile(r"\d|いつでも|午前|午後|以降|以前|まで|頃|朝|昼|夕|夜|平日|土日")
# 末尾の担当者名（数字も手がかりも無い、短い漢字・かな）
_TAIL_NAME = re.compile(r"\s+[一-龥ぁ-んァ-ヶー]{1,4}$")
NOTE_MAX = 60
# 貼り付け用の見出しが古い呼び名のままの列（中身は同じ項目）。連携分の数式はこの列を見ていない
HEAD_ALIASES = {"営業後備考（営業後の対応はこっち）": "顧客対応備考"}


def _norm(s) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_num(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + ord(ch) - 64
    return n


def _open(gc, url):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def fp_wish(text):
    """顧客対応備考から、FPの希望時間と希望日を抜き出す。→ (時間, 日)。無ければ ("", "")。

    例：「5/16 1115 FP 5/17 1300 マルシェ 5/17 黒部」→ ("1300", "5/17")
        「FPは8/2の1200以降希望。林」→ ("1200以降希望", "8/2")
        「★FP 12-13時の間に架電希望」→ ("12-13時の間に架電希望", "")
    ⚠️ FPより**前**の日時は書き込んだ時刻なので見ない。いちばん最初の、手がかりのある FP の話だけ使う。
    ⚠️ 読むのは人。機械に決めさせすぎず、画面で直してから書く。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    for m in re.finditer(r"FP", s):
        # 「FP・マルシェ 6/5午前中」のように、両方の希望をまとめて書くことがある
        rest = re.sub(r"^[\s・/、,]*(?:引っ?越し?)?マルシェ", "", s[m.end():])
        cut = _CUT_RE.search(rest)
        seg = (rest[:cut.start()] if cut else rest)[:NOTE_MAX]
        seg = re.sub(r"^[\s・:、,/はの]+", "", seg).strip()
        if not seg or seg.startswith("付帯") or not _TIME_HINT.search(seg):
            continue
        seg = _TAIL_NAME.sub("", seg).strip()
        days = [f"{int(a)}/{int(b)}" for a, b in _DAY_RE.findall(seg)]
        rest_t = _DAY_RE.sub(" ", seg)
        rest_t = re.sub(r"\s+", " ", rest_t).strip(" 、,・は")
        if not rest_t and not days:
            continue
        return rest_t, "、".join(dict.fromkeys(days))
    return "", ""


def _sf_notes(ids) -> dict:
    """案件 ID → 顧客対応備考（Salesforce から読む）。読めなければ例外。"""
    import salesforce_loader as sfl
    sf = sfl.connect()
    out = {}
    ids = [str(i).strip() for i in ids if str(i).strip()]
    for i in range(0, len(ids), 200):
        q = ", ".join("'" + x.replace("'", "") + "'" for x in ids[i:i + 200])
        for r in sf.query_all(f"SELECT Id, {SF_NOTE_FIELD} FROM {SF_OBJECT} WHERE Id IN ({q})")["records"]:
            out[r["Id"][:15]] = r.get(SF_NOTE_FIELD) or ""
    return out


def plan(gc, url: str) -> dict:
    """更新したシートを読み、足す案件を選ぶ（書かない）。

    → {"new": [{"id","row","view","time","day","note"}], "dup": [id…], "registered": [id…],
       "noid": 件数, "headers": [...], "error": "", "note_error": ""}
    """
    sh = _open(gc, url)
    src = sh.worksheet(REPORT_TAB)
    view = src.get_values()
    raw = src.get_values(value_render_option="UNFORMATTED_VALUE")
    paste = sh.worksheet(PASTE_TAB)
    p_head = [str(h).strip() for h in paste.row_values(1)]
    out = {"new": [], "dup": [], "registered": [], "noid": 0, "headers": [], "error": "", "note_error": ""}
    if not view or not any(str(c).strip() for c in view[0]):
        out["error"] = f"「{REPORT_TAB}」が空です（見出しもありません）。SFコネクタの設定を確かめてください"
        return out
    head = [str(h).strip() for h in view[0]]
    while head and not head[-1]:
        head.pop()
    out["headers"] = head
    # ⚠️ 連携分の数式は列の文字で見ているので、貼り付け用の見出しと1つでも違えば足さない。
    #    A列の見出しは貼り付け用が「最終行」のリンクなので比べない。レポートの右に余分な列があるのはよい。
    diffs = [f"{_col_letter(i + 1)}列：更新したシート「{head[i] if i < len(head) else '（無し）'}」／貼り付け用「{p_head[i]}」"
             for i in range(1, len(p_head))
             if (head[i] if i < len(head) else "") not in (p_head[i], HEAD_ALIASES.get(p_head[i]))]
    if diffs:
        out["error"] = ("レポートの列の並びが貼り付け用と違います（連携分に別の項目が出てしまうので足しません）："
                        + "／".join(diffs[:5]))
        return out
    have = {_norm(x) for x in paste.col_values(1)[1:] if _norm(x)}
    fpc = head.index(FP_DATE_COL) if FP_DATE_COL in head else -1
    nc = head.index(NOTE_COL) if NOTE_COL in head else -1
    seen = set()
    width = len(p_head)
    for i, v in enumerate(view[1:], start=1):
        v = (list(v) + [""] * len(head))[:len(head)]
        if not any(str(c).strip() for c in v):
            continue
        cid = _norm(v[0])
        if not cid:
            out["noid"] += 1
            continue
        if cid in have or cid in seen:
            out["dup"].append(v[0])
            continue
        if fpc >= 0 and str(v[fpc]).strip():
            out["registered"].append(v[0])     # もうFP登録日が入っている＝連携済み
            continue
        seen.add(cid)
        r = list(raw[i]) if i < len(raw) else []
        r = (r + [""] * width)[:width]          # 貼り付け用の幅まで（右の余分な列は書かない）
        out["new"].append({"id": str(v[0]).strip(), "row": r, "view": v,
                           "note": str(v[nc]) if nc >= 0 else None})
    # 顧客対応備考：レポートに無ければ Salesforce から読む
    if out["new"] and nc < 0:
        try:
            notes = _sf_notes([it["id"] for it in out["new"]])
            for it in out["new"]:
                it["note"] = notes.get(it["id"][:15], "")
        except Exception as e:
            out["note_error"] = f"顧客対応備考を Salesforce から読めませんでした（希望時間は空になります）：{str(e)[:150]}"
    for it in out["new"]:
        it["time"], it["day"] = fp_wish(it.get("note") or "")
    return out


def _link_start(link) -> int:
    """連携分で次に書く行（A列＝投入日が埋まっている最後の行の次）。"""
    return len(link.col_values(1)) + 1


def _template(link, before: int):
    """書く行より上で、B〜Gが「貼り付け用の同じ1行」を見ている数式の行を探す。→ (数式の並び, 行番号)"""
    lo = max(LINK_HEADER_ROW + 1, before - 60)
    rng = f"{FORMULA_COLS[0]}{lo}:{FORMULA_COLS[1]}{before - 1}"
    rows = link.get(rng, value_render_option="FORMULA")
    pat = re.compile(r"'?" + re.escape(PASTE_TAB) + r"'?!\$?[A-Z]{1,3}\$?(\d+)")
    for k in range(len(rows) - 1, -1, -1):
        fs = [str(x) for x in rows[k]]
        if len(fs) < _col_num(FORMULA_COLS[1]) - _col_num(FORMULA_COLS[0]) + 1:
            continue
        nums = {n for f in fs for n in pat.findall(f)}
        if len(nums) == 1 and all(f.startswith("=") and pat.search(f) for f in fs):
            return fs, int(nums.pop())
    return None, 0


def _fill(fs, old: int, new: int):
    pat = re.compile(r"('?" + re.escape(PASTE_TAB) + r"'?!\$?[A-Z]{1,3}\$?)" + str(old) + r"(?!\d)")
    return [pat.sub(lambda m: m.group(1) + str(new), f) for f in fs]


def append(gc, url: str, items, today: str = "") -> dict:
    """貼り付け用のいちばん下に足し、連携分の次の行に投入日・数式・希望時間/日を入れる。

    items＝plan の new（time/day は画面で直したもの）。
    → {"added": [...], "ok": [id…（連携分に正しく出た）], "bad": [(id, 理由)…], "skipped": [...], "rows": ...}
    ⚠️ 連携分の書く行に何か出ていたら、**1件も足さない**。
    """
    today = today or _dt.date.today().strftime("%Y/%m/%d")
    sh = _open(gc, url)
    paste = sh.worksheet(PASTE_TAB)
    # ⚠️ 書く直前に読み直す（別のPC・時間指定が足した分を二重にしない）
    ids = paste.col_values(1)
    have = {_norm(x) for x in ids[1:] if _norm(x)}
    todo = [it for it in items if _norm(it["id"]) not in have]
    skipped = [it["id"] for it in items if _norm(it["id"]) in have]
    if not todo:
        return {"added": [], "ok": [], "bad": [], "skipped": skipped, "rows": None}
    p_start = len(ids) + 1
    p_end = p_start + len(todo) - 1

    link = sh.worksheet(LINK_TAB)
    l_start = _link_start(link)
    l_end = l_start + len(todo) - 1
    cur = link.get(f"A{l_start}:{DAY_COL}{l_end}")
    busy = [l_start + k for k, r in enumerate(cur) if any(str(c).strip() for c in r)]
    if busy:
        raise RuntimeError(f"「{LINK_TAB}」の {busy[0]} 行目（書こうとした行）に、もう何か出ています。"
                           "上書きしないよう、何も足していません（連携分の中身を確かめてください）。")
    tpl, tpl_n = _template(link, l_start)
    if not tpl:
        raise RuntimeError(f"「{LINK_TAB}」の {l_start} 行目より上に、延ばせる数式の行（B〜G列が貼り付け用の1行を見ている）が"
                           "見つかりません。何も足していません。")
    if l_end > link.row_count:
        link.add_rows(l_end - link.row_count)

    width = max(len(it["row"]) for it in todo)
    rows = [(list(it["row"]) + [""] * width)[:width] for it in todo]
    # ⚠️ RAW で書く（番地「3-12」が日付に、電話番号の頭の0が消える、を防ぐ。日付は数値のまま渡る）
    paste.update(values=rows, range_name=f"A{p_start}:{_col_letter(width)}{p_end}",
                 value_input_option="RAW")

    ups, raws = [], []
    for k, it in enumerate(todo):
        lr = l_start + k
        ups.append({"range": f"A{lr}", "values": [[today]]})
        ups.append({"range": f"{FORMULA_COLS[0]}{lr}:{FORMULA_COLS[1]}{lr}",
                    "values": [_fill(tpl, tpl_n, p_start + k)]})
        if str(it.get("time") or "").strip():
            raws.append({"range": f"{TIME_COL}{lr}", "values": [[str(it["time"]).strip()]]})
        if str(it.get("day") or "").strip():
            raws.append({"range": f"{DAY_COL}{lr}", "values": [[str(it["day"]).strip()]]})
    link.batch_update(ups, value_input_option="USER_ENTERED")      # 投入日は日付、B〜Gは数式として
    if raws:
        link.batch_update(raws, value_input_option="RAW")           # 希望時間・日は書いたとおりに

    # ⭐ 書いたあと、G列（案件番号）が合っているか読み直す。合っていた案件だけ登録日を入れる
    got = link.get(f"{CHECK_COL}{l_start}:{CHECK_COL}{l_end}")
    p_head = [str(h).strip() for h in paste.row_values(1)]
    cno = p_head.index(CASE_NO_COL) if CASE_NO_COL in p_head else -1
    ok, bad = [], []
    for k, it in enumerate(todo):
        want = _norm(it["row"][cno]) if cno >= 0 else ""
        seen = _norm(got[k][0]) if k < len(got) and got[k] else ""
        if want and seen == want:
            ok.append(it["id"])
        else:
            bad.append((it["id"], f"連携分 {l_start + k} 行目の{CHECK_COL}列が「{seen or '空'}」（案件番号は「{want or '空'}」）"))
    return {"added": [it["id"] for it in todo], "ok": ok, "bad": bad, "skipped": skipped,
            "rows": (l_start, l_end), "paste_rows": (p_start, p_end),
            "wishes": sum(1 for it in todo if it.get("time") or it.get("day"))}


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


def refresh(gc, cfg: dict):
    """① SFコネクタで更新する。→ (ok, 理由)"""
    import auto_jobs
    import sms_runner
    url = str(cfg.get("sheet_url", "") or "").strip()
    robot = str(cfg.get("refresh_robot", "") or auto_jobs.DEFAULT_REFRESH_ROBOT).strip()
    urls = sms_runner.tab_urls_for(url, [REPORT_TAB], auto_jobs.tab_gids(gc, url))
    folder = sms_runner.work_dir(WORK_ROOT, "更新")
    ok, log = sms_runner.run_sheet_refresh(robot, folder, tabs=[REPORT_TAB], tab_urls=urls, url=url)
    return ok, ("" if ok else (sms_runner.stop_reason(log) or log[-300:]))


def case_label(it: dict, head) -> str:
    """Slack・画面に出す呼び名。⚠️ 個人名は出さない（案件番号、無ければ案件 ID）。"""
    if CASE_NO_COL in head:
        v = it["view"][head.index(CASE_NO_COL)]
        if str(v).strip():
            return str(v).strip()
    return str(it["id"])


# ==========================================
# ☁️ Salesforce の「FP登録日」
# ==========================================
def mark_registered(ids, day: str = "") -> dict:
    """「FP登録日」を入れる（Data Loader の代わり）。

    ⚠️ 送るのはこの1項目だけ。すでに**違う日付**が入っている案件は上書きしない（held）。
    → {"ok": [id…], "held": [(id, 今の値)…], "ng": [(id, 理由)…]}
    """
    import salesforce_loader as sfl
    day = day or _dt.date.today().isoformat()
    ids = [str(i).strip() for i in ids if str(i).strip()]
    out = {"ok": [], "held": [], "ng": []}
    if not ids:
        return out
    try:
        sf = sfl.connect()
        cur = {}
        for i in range(0, len(ids), 200):
            q = ", ".join("'" + x.replace("'", "") + "'" for x in ids[i:i + 200])
            for r in sf.query_all(f"SELECT Id, {SF_DATE_FIELD} FROM {SF_OBJECT} WHERE Id IN ({q})")["records"]:
                cur[r["Id"][:15]] = r.get(SF_DATE_FIELD) or ""
    except Exception as e:
        out["ng"] = [(x, f"Salesforceを読めませんでした：{str(e)[:150]}") for x in ids]
        return out
    send = []
    for x in ids:
        if x[:15] not in cur:
            out["ng"].append((x, "Salesforceに案件が見つかりません"))
        elif not cur[x[:15]]:
            send.append(x)
        elif cur[x[:15]] == day:
            out["ok"].append(x)
        else:
            out["held"].append((x, cur[x[:15]]))
    if send:
        res = sfl.upsert(sf, SF_OBJECT, "Id", [{"Id": x, SF_DATE_FIELD: day} for x in send])
        errs = res.get("errors") or []
        bad = {str(e.get("Id", "")): str(e.get("原因", "") or "投入に失敗しました") for e in errs}
        whole = any("〜" in k for k in bad) or (res.get("ng", 0) > len(errs))
        for x in send:
            if whole or x in bad:
                out["ng"].append((x, bad.get(x, "投入に失敗しました")[:200]))
            else:
                out["ok"].append(x)
    return out


def sf_step(supabase, cfg: dict, ids, day: str = ""):
    """登録日を入れ、入らなかった分は設定の `sf_pending` に覚える（次の実行・画面のボタンで入れ直す）。

    → (印, 中身)。⚠️ 連携分への追記は取り消せないので、ここで失敗しても追記はやり直させない。
    """
    day = day or _dt.date.today().isoformat()
    todo = {str(i): day for i in ids}
    todo.update(dict(load(supabase).get("sf_pending") or {}))
    by_day = {}
    for i, d in todo.items():
        by_day.setdefault(d, []).append(i)
    ok, held, ng = [], [], []
    for d, xs in by_day.items():
        r = mark_registered(xs, d)
        ok += r["ok"]; held += r["held"]; ng += r["ng"]
    try:
        save(supabase, {"sf_pending": {i: todo[i] for i, _ in ng}})
    except Exception:
        pass
    body = f"FP登録日を {len(ok)}件 入れました"
    if held:
        body += "／🛡 すでに違う日付が入っていたので上書きしていません：" + "、".join(f"{i}（{v}）" for i, v in held[:10])
    if ng:
        body += "／⚠️ 入らなかった（次の実行で入れ直します）：" + "、".join(f"{i}（{m}）" for i, m in ng[:10])
    return ("🛑" if ng else ("🛡" if held else "✅")), body


def after_append(supabase, cfg: dict, r: dict, steps):
    """追記の結果を工程に足し、正しく出た案件だけ登録日を入れる（画面と時間指定で共用）。"""
    body = (f"{len(r['added'])}件を足しました（連携分 {r['rows'][0]}〜{r['rows'][1]} 行目・希望時間/日あり {r.get('wishes', 0)}件）"
            if r["added"] else "足す案件はありませんでした")
    if r["skipped"]:
        body += f"／直前に別で足されていた {len(r['skipped'])}件は飛ばしました"
    if r.get("bad"):
        body += "／⚠️ 連携分に正しく出なかった（登録日は入れません）：" + "、".join(f"{i}（{m}）" for i, m in r["bad"][:10])
    steps.add("③ 連携分へ追記", "🛑" if r.get("bad") else ("✅" if r["added"] else "⏹"), body)
    try:
        save(supabase, {"last_run": {"at": time.strftime("%Y/%m/%d %H:%M"), "added": r["added"]}})
    except Exception:
        pass
    if r.get("ok") or cfg.get("sf_pending"):
        steps.add("④ Salesforceの FP登録日", *sf_step(supabase, cfg, r.get("ok") or []))


def run(supabase, gc, cfg: dict, do_refresh: bool = True, do_append=None) -> dict:
    """①更新 → ②足す案件を選ぶ → ③足す → ④登録日。画面の「▶ ぜんぶ実行」と時間指定が通る。

    do_append＝None なら設定の `auto_append` に従う（OFFなら ⏸ で止めて知らせる）。
    ⚠️ 更新に失敗したら足さない（古いレポートのまま足すと、取りこぼしに気づけない）。
    """
    import auto_jobs
    steps = auto_jobs._Steps()
    url = str(cfg.get("sheet_url", "") or "").strip()
    if not (url and gc):
        steps.add("準備", "🛑", "スプレッドシートのURL、または接続キーが未設定です")
        return steps.result()
    if do_refresh:
        ok, why = refresh(gc, cfg)
        if steps.add("① レポートの更新", "✅" if ok else "🛑",
                     f"「{REPORT_TAB}」を更新しました" if ok else why) == "🛑":
            return steps.result()
    try:
        p = plan(gc, url)
    except Exception as e:
        steps.add("② 足す案件", "🛑", f"シートを読めませんでした：{str(e)[:200]}")
        return steps.result()
    if p["error"]:
        steps.add("② 足す案件", "🛑", p["error"])
        return steps.result()
    extra = f"（すでに足してある {len(p['dup'])}件は飛ばしました）" if p["dup"] else ""
    if p["registered"]:
        extra += f"（FP登録日が入っている {len(p['registered'])}件は飛ばしました）"
    if not p["new"]:
        steps.add("② 足す案件", "⏹", "新しい案件は0件でした" + extra)
        if cfg.get("sf_pending"):
            steps.add("④ Salesforceの FP登録日（前回の残り）", *sf_step(supabase, cfg, []))
        return steps.result()
    names = "、".join(case_label(it, p["headers"]) for it in p["new"][:20])
    steps.add("② 足す案件", "✅", f"{len(p['new'])}件：{names}" + extra
              + (f"\n⚠️ {p['note_error']}" if p["note_error"] else ""))
    go = bool(cfg.get("auto_append")) if do_append is None else bool(do_append)
    if not go:
        steps.add("③ 連携分へ追記", "⏸",
                  f"{len(p['new'])}件 あります。「エントリー業務自動化 → 💼 FP連携」で中身を見て追記してください"
                  "（「時間指定で追記まで自動」がOFF）")
        return steps.result()
    try:
        r = append(gc, url, p["new"])
    except Exception as e:
        steps.add("③ 連携分へ追記", "🛑", str(e)[:400])
        return steps.result()
    after_append(supabase, cfg, r, steps)
    return steps.result()
