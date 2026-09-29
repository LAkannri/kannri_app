"""
🚚 引越マルシェへのトス（エントリー業務自動化のホームの「🚚 引越マルシェ」から開く）。

【これまでの手作業】
  SFのレポートを「ライフアップレポート貼付用」のいちばん下に貼る
  → 「連携シート」の数式が同じ行を拾って、名前・住所などが出る（引越マルシェが見るシート）
  → 連携シートの「トス日」と「備考」（顧客対応備考に書いてあるマルシェの希望時間）を手で打つ。

【ここでやること】
  ① SFコネクタで「ライフアップレポート更新自動化」を更新
  ② 貼付用にまだ無い案件（案件 ID で見る）だけを選ぶ
  ③ 貼付用のいちばん下に足す → 連携シートの同じ行に「トス日」と「備考」を入れる
  ④ 足せた案件だけ、Salesforce の「引越マルシェ登録日」に今日を入れる（Data Loader の代わり）

⭐ **連携シートの中身はスプシの数式が作る**（名前の並べ方・住所のつなぎ方）。アプリに写さない。
⚠️ 連携シートの数式は「貼付用の n 行目」を**列の文字で**見ている。レポートの列の並びが変わると
   別の項目が出るので、**更新したシートと貼付用の見出しが違えば足さずに止める**。
⚠️ 二重に足さない：貼付用の「案件 ID」に既にあるものは足さない（書く直前にも読み直す）。
⚠️ 連携シートのトス日・備考は、**空のときだけ**入れる（人が書いたものを上書きしない）。
"""
import datetime as _dt
import re
import time
import unicodedata

SETTINGS_ID = "__marche__"
SETTINGS_NAME = "（引越マルシェの設定）"
WORK_ROOT = "引越マルシェ"
REPORT_TAB = "ライフアップレポート更新自動化"   # SFコネクタで更新するシート
PASTE_TAB = "ライフアップレポート貼付用"        # 連携シートの数式が見ているシート
LINK_TAB = "連携シート"                        # 引越マルシェが見るシート
ID_COL = "案件 ID"
NOTE_COL = "顧客対応備考"
CASE_NO_COL = "案件番号"
LINK_HEADER_ROW = 2                            # 連携シートの見出し（1行目は「最終行」のリンク）
LINK_DATE_COL = "トス日"
LINK_NOTE_COL = "備考"                         # ⚠️ 2つある。左の方（トス時の備考）。右はマルシェ側の架電結果
# 「引越マルシェ」「引越しマルシェ」「引っ越しマルシェ」「マルシェ」
_MARCHE_RE = re.compile(r"(?:引っ?越し?)?マルシェ")
# 備考の区切り（ここから先は別の書き込み）
_CUT_RE = re.compile(r"[★\n\r【〈]|https?://")
NOTE_MAX = 80
SF_OBJECT = "Opportunity"
SF_DATE_FIELD = "hikkoshimarushetourokudate__c"   # 引越マルシェ登録日（日付）


def _norm(s) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def marche_note(text) -> str:
    """顧客対応備考から、マルシェについて書いてあるところだけ抜き出す（無ければ空）。

    例：「4/18 1750 ★引越マルシェ 4/19 18時以降希望 村田」→「4/19 18時以降希望 村田」
    ⚠️ 備考は自由に書かれているので、区切り（★・改行・URL）か80字で切る。
       どこまでが希望時間かを機械に決めさせると取りこぼすので、**多めに残す**（読むのは人）。
    """
    s = str(text or "")
    out = []
    for m in _MARCHE_RE.finditer(s):
        rest = s[m.end():]
        cut = _CUT_RE.search(rest)
        seg = rest[:cut.start()] if cut else rest
        seg = seg.strip(" 　:：、,・/／")
        if len(seg) > NOTE_MAX:
            seg = seg[:NOTE_MAX].rstrip() + "…"
        if seg and seg not in out:
            out.append(seg)
    return " ／ ".join(out)


def _open(gc, url):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _link_rows(ws) -> dict:
    """連携シートの B 列の数式から「貼付用の何行目 → 連携シートの何行目」を作る。"""
    f = ws.get(f"B{LINK_HEADER_ROW + 1}:B{ws.row_count}", value_render_option="FORMULA")
    out = {}
    pat = re.compile(re.escape(PASTE_TAB) + r"'?!\$?A\$?(\d+)")
    for i, r in enumerate(f):
        m = pat.search(str(r[0])) if r else None
        if m:
            out.setdefault(int(m.group(1)), LINK_HEADER_ROW + 1 + i)
    return out


def plan(gc, url: str) -> dict:
    """更新したシートを読み、足す案件を選ぶ（書かない）。

    → {"new": [{"id","row"(書く値),"view"(見える値),"note"}], "dup": [id…], "noid": 件数,
       "headers": [...], "error": ""}
    """
    sh = _open(gc, url)
    src = sh.worksheet(REPORT_TAB)
    view = src.get_values()
    raw = src.get_values(value_render_option="UNFORMATTED_VALUE")
    paste = sh.worksheet(PASTE_TAB)
    p_head = paste.row_values(1)
    out = {"new": [], "dup": [], "noid": 0, "headers": [], "error": ""}
    if not view:
        out["error"] = f"「{REPORT_TAB}」が空です（見出しもありません）"
        return out
    head = [str(h).strip() for h in view[0]]
    while head and not head[-1]:
        head.pop()
    out["headers"] = head
    # ⚠️ 連携シートの数式は列の文字で見ているので、並びが1つでも違えば足さない
    diffs = [f"{_col_letter(i + 1)}列：更新したシート「{h}」／貼付用「{p_head[i] if i < len(p_head) else '（無し）'}」"
             for i, h in enumerate(head) if i >= len(p_head) or str(p_head[i]).strip() != h]
    if diffs:
        out["error"] = ("レポートの列の並びが貼付用と違います（連携シートに別の項目が出てしまうので足しません）："
                        + "／".join(diffs[:5]))
        return out
    if ID_COL not in head:
        out["error"] = f"「{ID_COL}」の列がありません"
        return out
    ic, nc = head.index(ID_COL), (head.index(NOTE_COL) if NOTE_COL in head else -1)
    have = {_norm(x) for x in paste.col_values(p_head.index(ID_COL) + 1)[1:] if _norm(x)}
    seen = set()
    for i, v in enumerate(view[1:], start=1):
        v = (list(v) + [""] * len(head))[:len(head)]
        if not any(str(c).strip() for c in v):
            continue
        cid = _norm(v[ic])
        if not cid:
            out["noid"] += 1
            continue
        if cid in have or cid in seen:
            out["dup"].append(v[ic])
            continue
        seen.add(cid)
        r = list(raw[i]) if i < len(raw) else []
        r = (r + [""] * len(head))[:len(head)]
        out["new"].append({"id": v[ic], "row": r, "view": v,
                           "note": marche_note(v[nc]) if nc >= 0 else ""})
    return out


def append(gc, url: str, items, today: str = "") -> dict:
    """貼付用のいちばん下に足し、連携シートの同じ行にトス日と備考を入れる。

    items＝plan の new（note は画面で直したもの）。→ {"added": [...], "skipped": [...], "rows": (始め, 終わり)}
    ⚠️ 連携シートに数式の行が無いときは、**1件も足さない**（足しても連携シートに出ないため）。
    """
    today = today or _dt.date.today().strftime("%Y/%m/%d")
    sh = _open(gc, url)
    paste = sh.worksheet(PASTE_TAB)
    p_head = paste.row_values(1)
    idc = p_head.index(ID_COL) + 1
    # ⚠️ 書く直前に読み直す（画面を開いたあとに、別のPC・時間指定が足した分を二重にしない）
    ids = paste.col_values(idc)
    have = {_norm(x) for x in ids[1:] if _norm(x)}
    todo = [it for it in items if _norm(it["id"]) not in have]
    skipped = [it["id"] for it in items if _norm(it["id"]) in have]
    if not todo:
        return {"added": [], "skipped": skipped, "rows": None}
    start = len(ids) + 1
    end = start + len(todo) - 1
    link = sh.worksheet(LINK_TAB)
    lrows = _link_rows(link)
    missing = [n for n in range(start, end + 1) if n not in lrows]
    if missing:
        raise RuntimeError(f"「{LINK_TAB}」に、貼付用の {missing[0]} 行目を見る数式の行がありません"
                           "（連携シートの数式を下へ延ばしてから、もう一度押してください）。何も足していません。")
    lhead = link.row_values(LINK_HEADER_ROW)
    if LINK_DATE_COL not in lhead or LINK_NOTE_COL not in lhead:
        raise RuntimeError(f"「{LINK_TAB}」の{LINK_HEADER_ROW}行目に「{LINK_DATE_COL}」「{LINK_NOTE_COL}」の見出しがありません。何も足していません。")
    dcol, ncol = lhead.index(LINK_DATE_COL) + 1, lhead.index(LINK_NOTE_COL) + 1
    width = max(len(it["row"]) for it in todo)
    rows = [(list(it["row"]) + [""] * width)[:width] for it in todo]
    # ⚠️ RAW で書く（番地「3-12」が日付に、電話番号の頭の0が消える、を防ぐ。日付は数値のまま渡る）
    paste.update(values=rows, range_name=f"A{start}:{_col_letter(width)}{end}",
                 value_input_option="RAW")
    # 連携シート：空のときだけ入れる（人が書いたものを上書きしない）
    first, last = lrows[start], lrows[end]
    cur = link.get(f"{_col_letter(dcol)}{first}:{_col_letter(max(dcol, ncol))}{last}")
    date_up, note_up = [], []
    for k, it in enumerate(todo):
        lr = lrows[start + k]
        c = (list(cur[lr - first]) if lr - first < len(cur) else []) + [""] * (max(dcol, ncol) - dcol + 1)
        if not str(c[0]).strip():
            date_up.append({"range": f"{_col_letter(dcol)}{lr}", "values": [[today]]})
        if it.get("note") and not str(c[ncol - dcol]).strip():
            note_up.append({"range": f"{_col_letter(ncol)}{lr}", "values": [[it["note"]]]})
    if date_up:
        link.batch_update(date_up, value_input_option="USER_ENTERED")   # トス日は日付として入れる
    if note_up:
        link.batch_update(note_up, value_input_option="RAW")
    return {"added": [it["id"] for it in todo], "skipped": skipped, "rows": (start, end),
            "link_rows": (first, last), "notes": len(note_up)}


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


def run(supabase, gc, cfg: dict, do_refresh: bool = True, do_append=None) -> dict:
    """①更新 → ②足す案件を選ぶ → ③足す。画面の「▶ ぜんぶ実行」と時間指定が通る。

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
    if not p["new"]:
        steps.add("② 足す案件", "⏹", "新しい案件は0件でした" + extra)
        if cfg.get("sf_pending"):
            steps.add("④ Salesforceの登録日（前回の残り）", *sf_step(supabase, cfg, []))
        return steps.result()
    names = "、".join(case_label(it, p["headers"]) for it in p["new"][:20])
    steps.add("② 足す案件", "✅", f"{len(p['new'])}件：{names}" + extra)
    go = bool(cfg.get("auto_append")) if do_append is None else bool(do_append)
    if not go:
        steps.add("③ 連携シートへ追記", "⏸",
                  f"{len(p['new'])}件 あります。「エントリー業務自動化 → 🚚 引越マルシェ」で中身を見て追記してください"
                  "（「時間指定で追記まで自動」がOFF）")
        return steps.result()
    try:
        r = append(gc, url, p["new"])
    except Exception as e:
        steps.add("③ 連携シートへ追記", "🛑", str(e)[:400])
        return steps.result()
    body = (f"{len(r['added'])}件を足しました（備考あり {r.get('notes', 0)}件）"
            + (f"／直前に別で足されていた {len(r['skipped'])}件は飛ばしました" if r["skipped"] else ""))
    steps.add("③ 連携シートへ追記", "✅" if r["added"] else "⏹", body)
    try:
        save(supabase, {"last_run": {"at": time.strftime("%Y/%m/%d %H:%M"), "added": r["added"]}})
    except Exception:
        pass
    if r["added"] or cfg.get("sf_pending"):
        # ⭐ 書き写せた案件だけ（連携シートに出ていない案件に登録日を入れない）
        steps.add("④ Salesforceの登録日", *sf_step(supabase, cfg, r["added"]))
    return steps.result()


# ==========================================
# ☁️ Salesforce の「引越マルシェ登録日」
# ==========================================
def mark_registered(ids, day: str = "") -> dict:
    """足した案件の「引越マルシェ登録日」を入れる（Data Loader の代わり）。

    ⚠️ 送るのはこの1項目だけ（ほかは触らない）。
    ⚠️ すでに**違う日付**が入っている案件は上書きしない（held に出す）。同じ日付なら済みとみなす。
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
            chunk = ids[i:i + 200]
            q = ", ".join("'" + x.replace("'", "") + "'" for x in chunk)
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
        # ⚠️ まとめて失敗したとき（「A 〜 B」）や、件数だけ分かって中身が50件で切れたときは、全部を失敗にする
        whole = any("〜" in k for k in bad) or (res.get("ng", 0) > len(errs))
        for x in send:
            if whole or x in bad:
                out["ng"].append((x, bad.get(x, "投入に失敗しました")[:200]))
            else:
                out["ok"].append(x)
    return out


def sf_step(supabase, cfg: dict, ids, day: str = ""):
    """登録日を入れ、入らなかった分は設定の `sf_pending` に覚える（次の実行・画面のボタンで入れ直す）。

    → (印, 中身)。⚠️ 連携シートへの追記は取り消せないので、ここで失敗しても追記はやり直させない。
    """
    day = day or _dt.date.today().isoformat()
    pending = dict(load(supabase).get("sf_pending") or {})
    todo = {str(i): day for i in ids}
    todo.update(pending)                    # 前に入らなかった分も一緒に
    by_day = {}
    for i, d in todo.items():
        by_day.setdefault(d, []).append(i)
    ok, held, ng = [], [], []
    for d, xs in by_day.items():
        r = mark_registered(xs, d)
        ok += r["ok"]; held += r["held"]; ng += r["ng"]
    left = {i: todo[i] for i, _ in ng}
    try:
        save(supabase, {"sf_pending": left})
    except Exception:
        pass
    body = f"引越マルシェ登録日を {len(ok)}件 入れました"
    if held:
        body += "／🛡 すでに違う日付が入っていたので上書きしていません：" + "、".join(f"{i}（{v}）" for i, v in held[:10])
    if ng:
        body += "／⚠️ 入らなかった（次の実行で入れ直します）：" + "、".join(f"{i}（{m}）" for i, m in ng[:10])
    return ("🛑" if ng else ("🛡" if held else "✅")), body
