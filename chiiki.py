"""
📦 地域手配（水道・ガス・電気の地域手配）の中身。画面（pages/13_📦_地域手配.py）と
時間指定の自動実行（auto_jobs.run_chiiki）の両方から使う。Streamlit は import しない。

【流れ】
  ① SFコネクタで「地域水道手配」「地域ガス手配」「地域電気手配」を更新
  ② 振り分けとチェック（`check`）：1件ずつ、行き先（FAX／WEB／電話）と、
     **FAXに正しく載ったか**を確かめる。⚠️ が1件でも残っていたら先へ進ませない
  ③ 送った記録をDriveに保存（スプシの保存ボタンのGAS。任意）
  ⏸ FAXは人が送る（印刷 → FAX機。送り先を間違えると取り消せないため）
  ④ 電話・WEBで手配する分は、人が1件ずつ「対応した」にチェック
  ⑤ 全部そろったら、3つのDLシートの手配日をSalesforceへ入れる（Data Loaderの代わり）

⭐ **FAXの中身は、スプシの数式が作る**（東京都水道局FAX など）。ここではその**できあがりを読んで**、
   元データの n件目が FAX の n枠目に載っているかを電話番号で照らし合わせる。
   ⚠️ 判定を数式と二重に書かない。見るのは「載ったか」だけ。
   2026-09-26 に、東京都水道局FAXの3件目から氏名が1人ずつずれていた（数式のコピーずれ）。
   目で見ても気づけなかったので、**枠ごとに照らし合わせる**。
"""
import re
import time
import unicodedata

SETTINGS_ID = "__chiiki__"
WORK_ROOT = "地域手配"

# ── 元データ（SFコネクタで更新するシート）──
SRC = {
    "水道": {"tab": "地域水道手配", "kind": "地域水道種別", "dest": "地域水道局先",
             "start": "水道開始日", "dl": "地域水道手配DL", "dl_col": "地域水道手配日",
             "sf_field": "chiikiS__c"},
    "ガス": {"tab": "地域ガス手配", "kind": "地域G種別", "dest": "地域G先(LP含)",
             "start": "地域G開始日", "dl": "地域ガス手配DL", "dl_col": "地域G手配日",
             "sf_field": "chiikiG__c"},
    "電気": {"tab": "地域電気手配", "kind": "地域E種別", "dest": "地域E先",
             "start": "地域E開始日", "dl": "地域電気手配DL", "dl_col": "地域E手配日",
             "sf_field": "chiikiE__c"},
}
REFRESH_TABS = [SRC[k]["tab"] for k in ("水道", "ガス", "電気")]
ID_COL = "案件 ID"
PHONE_COL = "登録用"
REMARK_COLS = ["顧客対応備考", "営業前備考（営業後は使わない）", "ガス備考", "電力備考"]

# ── 行き先 ──
# FAXの枠：電話番号の欄・氏名の欄（列）と、枠の行。⚠️ スプシの様式を変えたらここも直す。
FAX = {
    "東京都水道局FAX": {"phone": "AJ", "name": "Z", "rows": [13 + 7 * k for k in range(10)]},
    "千葉県水道局FAX": {"phone": "L", "name": "B", "rows": list(range(15, 35))},
    "川崎市水道局FAX": {"phone": "L", "name": "B", "rows": list(range(15, 35))},
    "横浜市水道局FAX": {"phone": "L", "name": "B", "rows": list(range(15, 35))},
    "東京ガスFAX": {"phone": "L", "name": "B", "rows": list(range(15, 25))},
}
WEB = {"大阪ガスWEB": {"phone": "W", "name": "G", "name2": "H", "first": 2}}

# 種別 → 行き先。⚠️ 条件はFAXの数式と同じにする（順番がそろわないと枠の照らし合わせがずれる）。
#   ("fax", シート) / ("web", シート) / ("phone", 説明)
def route(kind_name: str, value: str):
    v = str(value or "").strip()
    if kind_name == "水道":
        m = {"23区水道局": ("fax", "東京都水道局FAX"), "千葉県水道局": ("fax", "千葉県水道局FAX"),
             "川崎市水道局": ("fax", "川崎市水道局FAX"), "横浜市水道局": ("fax", "横浜市水道局FAX"),
             "その他水道局": ("phone", "電話")}
        return m.get(v)
    if kind_name == "ガス":
        if v == "東京ガス":
            return ("fax", "東京ガスFAX")
        if v.startswith("大阪ガス"):            # 古い値「大阪ガス(3営業日以上)」も（大阪ガスWEBの数式と同じ）
            return ("web", "大阪ガスWEB")
        if v in ("東京ガス（INE）", "その他ガス(管理共有で相談)"):
            return ("phone", "電話")
        return None
    if kind_name == "電気":
        # 東京電力も電話で手配している（担当者 2026-09-26。いずれWEBにしたい）
        if v in ("東京電力", "その他電力"):
            return ("phone", "電話")
        return None
    return None


def _nfkc(v) -> str:
    return unicodedata.normalize("NFKC", str(v or "")).strip()


def digits(v) -> str:
    return re.sub(r"[^0-9]", "", _nfkc(v))


def _blank(v) -> bool:
    return re.fullmatch(r"[\s\-－ー―]*", str(v or "")) is not None


def _open(gc, url: str):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def _table(vals):
    if not vals:
        return [], []
    head = [str(h).strip() for h in vals[0]]
    rows = [dict(zip(head, (r + [""] * len(head))[:len(head)])) for r in vals[1:]
            if any(str(x).strip() for x in r)]
    return head, rows


def _nospace(v) -> str:
    return re.sub(r"\s", "", _nfkc(v))


def _fax_name(r: dict) -> str:
    """FAXに載るはずの契約名義（名義人同意が 3点／LL なら名義人。FAXの数式と同じ条件）。"""
    if str(r.get("名義人同意", "")).strip() in ("3点", "LL"):
        return f"{r.get('名義人：名前（姓）', '')} {r.get('名義人：名前（名）', '')}".strip()
    return _name("", r)


def _date(v):
    import datetime as _dt
    s = _nfkc(v)
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _name(kind_name: str, r: dict) -> str:
    # ⚠️ SFレポートの列は変わる（地域電気手配は「名前」1列 → 姓・名の2列に変わった・2026-09-26）
    full = f"{r.get('名前（姓）', '')} {r.get('名前（名）', '')}".strip()
    return full or str(r.get("名前", "")).strip()


def key_of(kind_name: str, case_id: str) -> str:
    return f"{kind_name}:{case_id}"


def check(gc, url: str) -> dict:
    """元データとFAX・WEBのできあがりを読んで、1件ずつ行き先と状態を出す。

    戻り値：{"rows": [...], "fax": {シート: 件数}, "block": ⚠️の件数}
    rows の1件：{"key","商材","案件ID","名前","種別","行き先","区分"(fax/web/phone/?),"状態","理由","注意"}
    """
    sh = _open(gc, url)
    ranges = [f"'{SRC[k]['tab']}'" for k in SRC]
    for key in ("phone", "name"):
        ranges += [f"'{t}'!{s[key]}1:{s[key]}{max(s['rows'])}" for t, s in FAX.items()]
    for key in ("phone", "name", "name2"):
        ranges += [f"'{t}'!{s[key]}{s['first']}:{s[key]}" for t, s in WEB.items()]
    got = sh.values_batch_get(ranges).get("valueRanges", [])
    vals = [g.get("values", []) for g in got]
    src_vals = dict(zip(SRC, vals[:3]))
    nf, nw = len(FAX), len(WEB)
    fax_phone, fax_nm = {}, {}
    for i, (t, s) in enumerate(FAX.items()):
        pc = [(x[0] if x else "") for x in vals[3 + i]]
        nc = [(x[0] if x else "") for x in vals[3 + nf + i]]
        fax_phone[t] = [digits(pc[r - 1]) if r - 1 < len(pc) else "" for r in s["rows"]]
        fax_nm[t] = [_nospace(nc[r - 1]) if r - 1 < len(nc) else "" for r in s["rows"]]
    base = 3 + 2 * nf
    for i, (t, s) in enumerate(WEB.items()):
        pc, n1, n2 = vals[base + i], vals[base + nw + i], vals[base + 2 * nw + i]
        cell = lambda col, j: (col[j][0] if j < len(col) and col[j] else "")
        fax_phone[t] = [digits(cell(pc, j)) for j in range(len(pc))]
        fax_nm[t] = [_nospace(cell(n1, j) + cell(n2, j)) for j in range(len(pc))]
    import datetime as _dt
    today_d = _dt.date.today()

    out, seen = [], {t: 0 for t in list(FAX) + list(WEB)}
    for kind_name, spec in SRC.items():
        head, rows = _table(src_vals.get(kind_name))
        if not head:
            out.append({"key": key_of(kind_name, "（シート）"), "商材": kind_name, "案件ID": "",
                        "名前": "", "種別": "", "行き先": "", "区分": "?",
                        "状態": "⚠️", "理由": f"シート「{spec['tab']}」が読めないか空です", "注意": ""})
            continue
        for r in rows:
            cid = str(r.get(ID_COL, "")).strip()
            if not cid:
                continue
            kv = str(r.get(spec["kind"], "")).strip()
            rt = route(kind_name, kv)
            why, note = [], []
            name = _name(kind_name, r)
            dest_txt = ""
            _sd = _date(r.get(spec["start"], ""))
            if not kv:
                why.append("種別が空です")
            elif rt is None:
                why.append(f"種別「{kv}」は今の選択肢にありません（どこに手配するか決められません）")
            else:
                kind, where = rt
                if kind in ("fax", "web"):
                    # FAX・WEBに書く中身が欠けていないか
                    lack = [lbl for lbl, col in (("名前", None), ("電話番号", PHONE_COL),
                                                 ("市区郡", "市区郡"), ("町名", "町名"),
                                                 ("開始日", spec["start"]))
                            if (_blank(name) if col is None else _blank(r.get(col, "")))]
                    if lack:
                        why.append("空の欄があります：" + "・".join(lack))
                    # 形がおかしいもの（中身の誤字そのものは、正解が手元に無いので見分けられない）
                    ph = digits(r.get(PHONE_COL, ""))
                    if ph and not re.fullmatch(r"0\d{9,10}", ph):
                        why.append(f"電話番号の形がおかしいです（{r.get(PHONE_COL, '')}）")
                    zp = digits(r.get("*郵便番号", ""))
                    if zp and len(zp) != 7:
                        why.append(f"郵便番号の形がおかしいです（{r.get('*郵便番号', '')}）")
                    sd = _date(r.get(spec["start"], ""))
                    if not _blank(r.get(spec["start"], "")) and sd is None:
                        why.append(f"開始日が日付として読めません（{r.get(spec['start'], '')}）")
                    elif sd and sd < today_d:
                        why.append(f"開始日がもう過ぎています（{sd:%Y/%m/%d}）")
                    elif sd and (sd - today_d).days < 3:
                        note.append(f"開始日まであと{(sd - today_d).days}日です（{sd:%m/%d}）")
                    slots = FAX.get(where, {}).get("rows")
                    n = seen[where]
                    seen[where] += 1
                    if slots is not None and n >= len(slots):
                        why.append(f"{where}の枠（{len(slots)}件）が足りません")
                        dest_txt = f"📠 {where}（枠の外）"
                    else:
                        shown = fax_phone.get(where, [])
                        got_d = shown[n] if n < len(shown) else ""
                        want = digits(r.get(PHONE_COL, ""))
                        dest_txt = (f"📠 {where} {n + 1}枠目" if kind == "fax"
                                    else f"🌐 {where} {n + 2}行目")
                        if not got_d:
                            why.append(f"{where}の{n + 1}件目が空です（数式が出していません）")
                        elif want and got_d != want:
                            why.append(f"{where}の{n + 1}件目に、別の電話番号（下4桁 {got_d[-4:]}）が載っています")
                        # ⭐ 氏名も照らし合わせる（2026-09-26：氏名だけが1人ずつずれていた）
                        nms = fax_nm.get(where, [])
                        got_n = nms[n] if n < len(nms) else ""
                        want_n = _nospace(_fax_name(r))
                        if got_n and want_n and got_n != want_n:
                            why.append(f"{where}の{n + 1}件目に、別の氏名（{got_n}）が載っています"
                                       f"（この案件は {want_n}）")
                else:
                    dest_txt = f"📞 電話（{str(r.get(spec['dest'], '')).strip() or kv}）"
            rem = " ".join(str(r.get(c, "")) for c in REMARK_COLS)
            if "キャンセル" in rem:
                note.append("備考に「キャンセル」とあります")
            if rt and rt[0] == "phone" and _sd and (_sd - today_d).days < 3:
                note.append(f"開始日まであと{(_sd - today_d).days}日です（{_sd:%m/%d}）")
            out.append({"key": key_of(kind_name, cid), "商材": kind_name, "案件ID": cid,
                        "名前": name, "種別": kv, "行き先": dest_txt,
                        "開始": _sd.isoformat() if _sd else "",
                        "区分": (rt[0] if rt else "?"),
                        "状態": "⚠️" if why else "✅", "理由": "／".join(why),
                        "注意": "／".join(note)})
    fax_count = {t: n for t, n in seen.items() if n}
    return {"rows": out, "fax": fax_count,
            "block": sum(1 for x in out if x["状態"] == "⚠️"),
            "checked_at": time.strftime("%Y/%m/%d %H:%M")}


# ── その日の進み具合（画面で押したもの）──
def today() -> str:
    return time.strftime("%Y-%m-%d")


def day_state(cfg: dict) -> dict:
    """その日の進み具合。⭐ Supabase に置くので、**どのPCで押しても同じ状態**になる。
    {"day", "decide": {key: "manual"|"skip"}, "done": [key…], "fax_keys": [FAXで送った案件…],
     "fax_done": [送ったFAXシート…], "pushed_keys": [手配日を入れた案件…],
     "memo": {key: 備考に足す文}, "remarked": [備考に書き足した案件…]}

    ⚠️ 日が変わっても、**済んだのに手配日をまだ入れていない案件**は持ち越す（捨てると手配日の入れ忘れになる）。
    """
    st = dict(cfg.get("state") or {})
    for k in ("done", "fax_keys", "fax_done", "pushed_keys", "remarked"):
        st[k] = list(st.get(k) or [])
    st["decide"] = dict(st.get("decide") or {})
    st["memo"] = dict(st.get("memo") or {})
    if st.get("day") != today():
        carry = to_push(st)
        st = {"day": today(), "decide": {k: v for k, v in st["decide"].items() if k in carry},
              "done": [k for k in st["done"] if k in carry],
              "fax_keys": [k for k in st["fax_keys"] if k in carry],
              "fax_done": [], "pushed_keys": [],
              "memo": {k: v for k, v in st["memo"].items() if k in carry},
              "remarked": [k for k in st["remarked"] if k in carry]}
    return st


def to_push(st: dict) -> list:
    """済んだのに、まだ手配日を入れていない案件の key（行が無くても state だけで分かる）。"""
    pushed = set(st.get("pushed_keys") or [])
    skip = {k for k, v in (st.get("decide") or {}).items() if v == "skip"}
    return sorted((set(st.get("done") or []) | set(st.get("fax_keys") or [])) - pushed - skip)


# 📝 電話手配のときに書き足す備考（Salesforce の案件＝案件IDでそのまま引ける）。
#    ⚠️ 上書きはしない（sf_ui.append_remark＝うしろに1行足す・同じ文言は二度書かない）。
#    水道は、いつも顧客対応備考に書いている（担当者 2026-09-26）。
REMARK_FIELD = {"電気": "PowerRemarks__c", "ガス": "GasRemarks__c", "水道": "FormanagementRemarks__c"}
REMARK_LABEL = {"電気": "電力備考", "ガス": "ガス備考", "水道": "顧客対応備考"}


def remark_text(memo: str) -> str:
    return f"{time.strftime('%Y/%m/%d')} {str(memo or '').strip()}"


def fax_sheet(r: dict) -> str:
    return str(r.get("行き先", "")).replace("📠", "").strip().split(" ")[0]


def open_items(rows, st: dict):
    """⚠️ のうち、まだ人が決めていないもの（手配日を入れた案件は除く）。"""
    pushed = set(st.get("pushed_keys") or [])
    return [r for r in rows if r["状態"] == "⚠️" and not st["decide"].get(r["key"]) and r["key"] not in pushed]


def manual_items(rows, st: dict):
    """人が手配する分（電話・WEB・⚠️から「手で手配する」に回したもの）。"""
    pushed = set(st.get("pushed_keys") or [])
    return [r for r in rows
            if st["decide"].get(r["key"]) != "skip" and r["key"] not in pushed
            and (r["区分"] in ("phone", "web") or st["decide"].get(r["key"]) == "manual")]


def _fax_candidates(rows, st: dict):
    return [r for r in rows if r["区分"] == "fax" and r["状態"] == "✅" and not st["decide"].get(r["key"])]


def blocked_sheets(rows, st: dict) -> set:
    """もう送った案件（FAX済み・手配日済み）が載っているFAXシート。
    ⚠️ 送り直すと前のお客様に二重に届くので、このシートはアプリから送らない。"""
    old = set(st.get("fax_keys") or []) | set(st.get("pushed_keys") or [])
    return {fax_sheet(r) for r in _fax_candidates(rows, st) if r["key"] in old}


def fax_items(rows, st: dict):
    """FAXで送る分（まだ送っていない・送り直しにならないもの）。"""
    old = set(st.get("fax_keys") or []) | set(st.get("pushed_keys") or [])
    bl = blocked_sheets(rows, st)
    return [r for r in _fax_candidates(rows, st) if r["key"] not in old and fax_sheet(r) not in bl]


def late_fax_items(rows, st: dict):
    """FAXの行き先なのに、そのシートに前に送った案件が残っていて送れないもの（人が扱いを決める）。"""
    old = set(st.get("fax_keys") or []) | set(st.get("pushed_keys") or [])
    bl = blocked_sheets(rows, st)
    return [r for r in _fax_candidates(rows, st) if r["key"] not in old and fax_sheet(r) in bl]


def handled(r: dict, st: dict) -> bool:
    """手配が済んだか（手配日を入れてよいか・入れたか）。"""
    if r["key"] in (st.get("pushed_keys") or []):
        return True
    if st["decide"].get(r["key"]) == "skip":
        return False
    if r["区分"] == "fax" and r["状態"] == "✅" and not st["decide"].get(r["key"]):
        return r["key"] in (st.get("fax_keys") or [])
    return r["key"] in (st.get("done") or [])


def left_items(rows, st: dict):
    """まだ済んでいない案件（「手配しない」にしたものは除く）。⭐ 利用開始が近い順。忘れ防止の知らせに使う。"""
    left = [r for r in rows if st["decide"].get(r["key"]) != "skip" and not handled(r, st)]
    return sorted(left, key=lambda r: r.get("開始") or "9999")


def urgent(r: dict) -> bool:
    """🚨 利用開始が今日・明日（またはもう過ぎた）。手配できていないとまずい。"""
    import datetime as _dt
    s = r.get("開始") or ""
    if not s:
        return False
    try:
        return _dt.date.fromisoformat(s) <= _dt.date.today() + _dt.timedelta(days=1)
    except ValueError:
        return False


def left_line(r: dict) -> str:
    s = r.get("開始") or ""
    when = f"　開始 {s[5:].replace('-', '/')}" if s else ""
    return (f"{'🚨 ' if urgent(r) else ''}{r['商材']} `{r['案件ID']}` {r['名前']}"
            f"（{r['行き先'] or r['種別']}）{when}")


def ready_to_push(rows, st: dict):
    """(投入してよいか, 理由)。⭐ **済んだ案件だけ**入れるので、残りがあっても済んだ分は入れてよい
    （夕方の更新の前に入れておく＝担当者 2026-09-26）。"""
    if not to_push(st):
        return False, "手配日を入れる案件がありません（済んだ案件はもう入れたか、まだありません）"
    return True, ""


def push_all(gc, url: str, rows, st: dict) -> list:
    """3つのDLシートから、手配日だけを Salesforce に入れる（中身は sf_ui.push_sheet）。
    入れられた案件は st["pushed_keys"] に足す（保存は呼び出し側）。

    ⚠️ マッピングは「案件 ID → Id」「手配日」の2つだけ（ほかの列は送らない）。
    ⭐ **済んでいて、まだ入れていない案件だけ**（`to_push`）。DLシートのそれ以外の行は全部外す。
    ⚠️ 済んだのにDLシートに無い案件は、入れられないので名指しして失敗にする（黙って落とさない）。
    """
    import sf_ui
    want = set(to_push(st))
    out = []
    sh = _open(gc, url)
    for kind_name, spec in SRC.items():
        ok_ids = {k.split(":", 1)[1] for k in want if k.startswith(kind_name + ":")}
        try:
            _h, drows = _table(sh.worksheet(spec["dl"]).get_all_values())
        except Exception as e:
            out.append({"シート": spec["dl"], "結果": f"❌ シートを読めません: {str(e)[:100]}", "ok": 0, "ng": 1})
            continue
        dl_ids = {str(d.get(ID_COL, "")).strip() for d in drows} - {""}
        missing = sorted(ok_ids - dl_ids)
        if not ok_ids & dl_ids:
            r = {"結果": "📭 入れる案件なし", "投入なし": True, "ok": 0, "ng": 0}
        else:
            r = sf_ui.push_sheet(gc, url, spec["dl"], "Opportunity", "Id",
                                 {ID_COL: "Id", spec["dl_col"]: spec["sf_field"]},
                                 skip_col=ID_COL, skip_values=sorted(dl_ids - ok_ids))
            if sf_ui.push_ok(r):
                st["pushed_keys"] = sorted(set(st.get("pushed_keys") or [])
                                           | {key_of(kind_name, i) for i in ok_ids & dl_ids})
        if missing:
            r = {**r, "投入なし": False, "ng": (r.get("ng") or 0) + len(missing),
                 "結果": (str(r.get("結果", "")) + f"／🛑 DLシートに無いので入れられません：{'、'.join(missing)}")}
            r["missing"] = [key_of(kind_name, i) for i in missing]
        out.append({"シート": spec["dl"], **r})
    return out


def summary_lines(res: dict) -> list:
    """Slack・画面の要約。"""
    rows = res.get("rows") or []
    fax = "、".join(f"{t} {n}件" for t, n in (res.get("fax") or {}).items()) or "なし"
    phone = sum(1 for r in rows if r["区分"] == "phone")
    web = sum(1 for r in rows if r["区分"] == "web")
    lines = [f"FAX：{fax}", f"電話 {phone}件／WEB {web}件"]
    for r in [x for x in rows if x["状態"] == "⚠️"][:20]:
        lines.append(f"⚠️ {r['商材']} `{r['案件ID']}`　{r['理由']}")
    return lines
