"""
🔄 SFコネクタを使わずに、Salesforceのレポートをスプシのシートへ書き写す

【困っていたこと】
  SFコネクタの更新はロボットがChromeでスプシを開いて押していた。Googleにログインした
  そのPCのChromeが要るので、クラウドの画面からは動かせず、重い・メニューが閉じる・ログイン切れ、で止まっていた。

【このしくみ】
  ① どのレポートかは、シートのA1のメモ（SFコネクタが書く「Report: <名前>」）から読む
     ＝人が設定しなくてよい。コネクタの設定はそのまま残るので、**コネクタでの更新も予備として使える**。
  ② レポートAPIで、**いまのレポートの形のまま**（見出し・列の順番）読む。列を足し引きしても、そのまま付いてくる。
  ③ 見出しを1行目に、中身を2行目から **USER_ENTERED（人が打ち込んだのと同じ扱い）** で書く。
     ⭐ コネクタも表示の文字を打ち込んでいるだけなので、日付・数字・チェックがコネクタと同じ値になる。
  ④ 前の行・列の残りを消す。⚠️ **前にコネクタが書いていた範囲だけ**（右に人が置いた数式の列は触らない）。
  ⚠️ 列の顔ぶれが同じなら、**シートの今の並び**で書く（コネクタは見出しを自分で追いかけないので、
     レポートの並びとシートの並びがずれていることがある。並びを変えると、列の文字で見ている数式が崩れる）。
  ⚠️ 頭の0が落ちる番号もコネクタと同じにする（値を変えないことを優先。担当者 2026-10-06）。
  まとめ（SUMMARY）のレポートは、まとめの順に詳細の行だけを並べる（コネクタと同じ。まとめの列は出さない）。

【切り替えは、確かめたシートだけ】（担当者 2026-10-06：値が変わるところがあったら怖い）
  `verify_tab` が、試しのシートにAPIの中身を書いて、いまのシートと1セルずつ見比べる。
  書き方が違う（日付が文字になる・頭の0が落ちる…）シートは切り替えない。
  結果は予約行 `__sf_api__` の `tabs`（キー＝スプシのID|シート名）。`run_sheet_refresh` は
  ✅ のシートだけAPIで書き、ほかはこれまでどおりロボット（SFコネクタ）で更新する。

⚠️ Streamlit を import しない（画面以外からも使えるように）。
"""
import datetime
import json
import re
import time
import unicodedata

CONFIG_ID = "__sf_api__"
TEMP_PREFIX = "【API試し】"
NOTE_RE = re.compile(r"Report:\s*(.+)")
KEY_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_NUMERIC_TYPES = {"double", "int", "currency", "percent"}

# 確かめた結果
OK, CHECK, NG, ERROR = "ok", "check", "ng", "error"
LABEL = {OK: "✅ 切り替え済み", CHECK: "🔎 人が見て決める", NG: "⚠️ 書き方が違う（切り替えない）",
         ERROR: "❌ 確かめられない"}


def now_stamp() -> str:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M")


def key_of(url: str) -> str:
    m = KEY_RE.search(str(url or ""))
    return m.group(1) if m else ""


def tab_id(key: str, tab: str) -> str:
    return f"{key}|{tab}"


# ==========================================
# 💾 設定（どのシートを切り替えたか）
# ==========================================
def load_cfg(supabase) -> dict:
    res = supabase.table("merchants").select("config_json").eq("id", CONFIG_ID).execute()
    cfg = (res.data[0].get("config_json") or {}) if res.data else {}
    cfg.setdefault("on", True)
    cfg.setdefault("tabs", {})
    return cfg


def save_cfg(supabase, part: dict = None, tabs: dict = None):
    """⚠️ 書く直前に読み直して、渡したところだけ変える（ほかのPCの確かめた結果を消さない）。"""
    latest = load_cfg(supabase)
    latest.update(part or {})
    for k, v in (tabs or {}).items():
        if v is None:
            latest["tabs"].pop(k, None)
        else:
            latest["tabs"][k] = dict(latest["tabs"].get(k) or {}, **v)
    supabase.table("merchants").upsert({
        "id": CONFIG_ID, "name": "（SFコネクタを使わない更新）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return latest


def use_api(cfg: dict, key: str, tab: str) -> bool:
    """このシートをAPIで書いてよいか（全体がONで、確かめて✅か、人がOKしたもの）。"""
    if not cfg.get("on", True):
        return False
    t = (cfg.get("tabs") or {}).get(tab_id(key, tab)) or {}
    return t.get("result") == OK or (t.get("result") == CHECK and t.get("approved"))


# ==========================================
# ☁️ Salesforceのレポートを読む
# ==========================================
def report_name(note: str) -> str:
    """A1のメモ → レポート名（無ければ空）。"""
    m = NOTE_RE.search(str(note or ""))
    return m.group(1).strip() if m else ""


def find_report_id(sf, name: str, head=None, cur=None) -> str:
    """名前 → レポートID。⚠️ 0件は止める（違うレポートを書き写さないため）。

    同じ名前が2つ以上あるときは、**シートの見出しと列の顔ぶれが同じもの**が1つだけなら、それを使う。
    それでも決まらなければ、**いまのシートの行といちばん多く重なるもの**（1つだけのとき）を使う。
    """
    q = "SELECT Id, Name FROM Report WHERE Name = '%s'" % name.replace("\\", "\\\\").replace("'", "\\'")
    recs = sf.query_all(q).get("records", [])
    if not recs:
        raise RuntimeError(f"Salesforceにレポート「{name}」が見つかりません（名前が変わったかもしれません）")
    if len(recs) == 1:
        return recs[0]["Id"]
    sheet = [str(h).strip() for h in (head or [])]
    hits = []
    for rec in recs:
        try:
            d = sf.restful(f"analytics/reports/{rec['Id']}/describe")
            info = d["reportExtendedMetadata"]["detailColumnInfo"]
            labels = [info[c]["label"] for c in d["reportMetadata"].get("detailColumns") or []]
            # シートの左から、そのレポートの列がそろって並んでいるか（右に人の列があってもよい）
            if labels and sorted(sheet[:len(labels)]) == sorted(str(x).strip() for x in labels):
                hits.append(rec["Id"])
        except Exception:
            pass
    if len(hits) == 1:
        return hits[0]
    # 見出しがそっくり同じレポートが2つ以上あるときだけ、行の重なりで決める（見出しが合わないものは選ばない）
    if len(hits) > 1 and cur and len(cur) > 1:
        cells = {str(c) for r in cur[1:] for c in r if str(c).strip()}
        score = []
        for rid in hits:
            try:
                _h, body, _t = read_report(sf, rid)
                score.append((len(cells & {str(c) for r in body for c in r if str(c).strip()}), rid))
            except Exception:
                pass
        score.sort(reverse=True)
        if score and score[0][0] > 0 and (len(score) == 1 or score[0][0] > score[1][0]):
            return score[0][1]
    raise RuntimeError(f"Salesforceに「{name}」という名前のレポートが{len(recs)}つあり、"
                       f"シートの見出しからも決められないので止めました")


def _cell(text, dtype: str) -> str:
    """APIの表示の文字 → シートに打ち込む文字。"""
    v = "" if text in (None, "-") else str(text)
    if not v:
        return ""
    # ⚠️ 「=」で始まる文は数式になってしまうので、文字として入れる
    if v.startswith("="):
        return "'" + v
    return v


def read_report(sf, report_id: str) -> tuple:
    """(見出し, 行, 列の型) を、レポートの画面に出ている文字のまま読む。

    ⚠️ 表形式のレポートだけ（まとめ・グループのあるレポートは行の並びが違う）。
    ⚠️ レポートAPIは2000行まで。超えたら、途中で切れたまま書かないよう止める。
    """
    r = sf.restful(f"analytics/reports/{report_id}", params={"includeDetails": "true"})
    meta = r.get("reportMetadata") or {}
    fmt = str(meta.get("reportFormat", ""))
    if fmt not in ("TABULAR", "SUMMARY"):
        raise RuntimeError(f"この形のレポート（{fmt}）は書き写せません")
    if not r.get("allData", True):
        raise RuntimeError("レポートの行が2000行を超えていて、全部を読めませんでした")
    info = r["reportExtendedMetadata"]["detailColumnInfo"]
    cols = meta.get("detailColumns") or []
    head = [info[c]["label"] for c in cols]
    types = [info[c].get("dataType", "") for c in cols]
    fm = r.get("factMap", {}) or {}
    # まとめ（SUMMARY）のレポートは、まとめの順に詳細の行を並べる（コネクタと同じ。まとめの列は出さない）
    keys = ["T!T"] if fmt == "TABULAR" else [f"{k}!T" for k in _leaf_keys(
        (r.get("groupingsDown") or {}).get("groupings") or [])]
    body = []
    for k in keys:
        for row in (fm.get(k) or {}).get("rows", []):
            body.append([_cell(cell.get("label"), t) for cell, t in zip(row.get("dataCells", []), types)])
    return head, body, types


def _leaf_keys(groupings):
    for g in groupings:
        if g.get("groupings"):
            yield from _leaf_keys(g["groupings"])
        else:
            yield g.get("key")


def in_sheet_order(head, body, types, sheet_head) -> tuple:
    """列の顔ぶれがシートの見出しと同じなら、シートの並びに並べ替える。違えばレポートのまま。"""
    sh = [str(h).strip() for h in sheet_head or []]
    while sh and not sh[-1]:
        sh.pop()
    if sh == list(head) or sorted(sh) != sorted(head) or len(set(head)) != len(head):
        return head, body, types
    idx = [head.index(h) for h in sh]
    return sh, [[r[i] for i in idx] for r in body], [types[i] for i in idx]


def _col(n: int) -> str:
    """1 → A, 27 → AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _old_extent(values, report_head=None) -> tuple:
    """いまコネクタのデータが入っている範囲 → (列の数, 行の数)。

    列：1行目の見出しが左から続いているところまで。行：その列のどこかに何か入っている最後の行。
    ⚠️ レポートには見出しが空の列があることがある（そこで止まると、残りの列を見落とす）。
       report_head を渡したら、シートの見出しがレポートの見出しと先頭から同じところまでは、空でも範囲に入れる。
    """
    head = list(values[0]) if values else []
    width = 0
    for h in head:
        if str(h).strip() == "":
            break
        width += 1
    if report_head:
        p = 0
        while p < len(head) and p < len(report_head) and str(head[p]).strip() == str(report_head[p]).strip():
            p += 1
        width = max(width, p)
    last = 0
    for i, r in enumerate(values):
        if any(str(c).strip() != "" for c in list(r)[:width]):
            last = i + 1
    return width, last


def write_tab(ws, head: list, body: list, old: tuple = None):
    """シートに書く → 前の行の残り（レポートの列の幅の中だけ）を消す。

    ⚠️ **レポートの列より右は、いっさい触らない**。TS付箋のように、データのすぐ右に人が入力する列
       （架電対応者・チェック）や数式の列があり、見出しが続いているので、コネクタの列と見分けられない。
    old … (前の列の数, 前の行の数)。無ければ今のシートから読む。
    """
    if old is None:
        old = _old_extent(ws.get_values("A1:ZZ"), head)
    _old_w, old_rows = old
    rows, cols = len(body) + 1, max(len(head), 1)
    if ws.row_count < rows:
        ws.add_rows(rows - ws.row_count)
    if ws.col_count < cols:
        ws.add_cols(cols - ws.col_count)
    ws.update([head] + body, "A1", value_input_option="USER_ENTERED")
    if old_rows > rows:
        ws.batch_clear([f"A{rows + 1}:{_col(cols)}{old_rows}"])


def _open(gc, key_or_url: str):
    k = key_of(key_or_url) or key_or_url
    return gc.open_by_key(k)


def write_report(ws, sf, cur=None) -> tuple:
    """そのシートのレポートを読み、シートに書く → (レポート名, 件数, 見出しの変化の一言, 列の型)。

    cur … いまのシートの値（無ければ読む）。確かめるとき（写しのシート）と本番で同じ関数を通す。
    """
    name = report_name(ws.get_note("A1"))
    if not name:
        raise RuntimeError("A1のメモにレポート名がありません（SFコネクタでレポートを入れたシートではありません）")
    if cur is None:
        cur = ws.get_values("A1:ZZ")
    sheet_head = list(cur[0]) if cur else []
    head, body, types = read_report(sf, find_report_id(sf, name, sheet_head, cur))
    head, body, types = in_sheet_order(head, body, types, sheet_head)
    old = _old_extent(cur, head)
    changed = ""
    if [str(h) for h in sheet_head[:old[0]]] != list(head):
        changed = "（レポートの列が変わっていたので、レポートどおりの見出しで書きました）"
    write_tab(ws, head, body, old)
    return name, len(body), changed, types


def refresh_one(sh, sf, tab: str) -> str:
    """1枚を書き写す → ログの1行。失敗は例外。"""
    t0 = time.time()
    name, n, changed, _ = write_report(sh.worksheet(tab), sf)
    return f"☁️ Salesforceから直接書き写しました：レポート「{name}」{n}件（{time.time() - t0:.1f}秒）{changed}"


# ==========================================
# 🔍 確かめる（値が変わらないか）
# ==========================================
def _kind(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    return "str"


def _norm(v) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(v)))


def _key_col(cur, new, width):
    """Idの列が無いレポートで、行を突き合わせる列を選ぶ（両方で値が重ならず、いちばん多く一致する列）。

    ⚠️ 並び順で突き合わせると、1行増えただけで以降が全部ずれ、別のお客様どうしを比べてしまう。
    """
    best, best_n = None, 0
    for j in range(width):
        a = [str(r[j]) for r in cur[1:] if j < len(r) and str(r[j]).strip()]
        b = [str(r[j]) for r in new[1:] if j < len(r) and str(r[j]).strip()]
        if not a or not b or len(set(a)) != len(a) or len(set(b)) != len(b):
            continue
        n = len(set(a) & set(b))
        if n > best_n and n >= 0.5 * min(len(a), len(b)):
            best, best_n = j, n
    return best


def compare(cur: list, new: list, types: list) -> dict:
    """いまのシート（cur）と、APIで書いたシート（new）を見比べる。どちらも UNFORMATTED の値。

    ・見出しが違う／型が違う（日付と文字・数字と文字・真偽と文字）／空白や全角半角だけ違う → ⚠️ 書き方が違う
    ・同じ型で値だけ違う → 前の更新からSalesforceが変わった分（ふつう）。ただし文字の列で8割以上違えば 🔎
    行は、案件IDなどIdの列があればそれで突き合わせる（無ければ並び順）。
    """
    cur = [list(r) for r in cur or []]
    new = [list(r) for r in new or []]
    nh = [str(x) for x in (new[0] if new else [])]
    old_w, _ = _old_extent(cur, nh)
    ch = [str(x) for x in (cur[0] if cur else [])][:old_w]
    out = {"result": OK, "problems": [], "notes": [], "examples": [],
           "rows_cur": max(0, len(cur) - 1), "rows_new": max(0, len(new) - 1)}
    # レポートの列より右にある列（人が入力する列・数式の列）は書き写しで触らないので、比べない
    while len(ch) > len(nh) and ch[len(nh):] and all(h not in nh for h in ch[len(nh):]):
        out["notes"].append(f"右の列（{'、'.join(h for h in ch[len(nh):] if h)}）は触りません")
        ch = ch[:len(nh)]
        break
    if ch != nh:
        # 列が足された・消えた（コネクタは見出しを追いかけないので、レポートの変更が溜まっている）。
        # レポートどおりに書くと列の位置が動くので、人が決める。共通の列は名前で見比べる。
        out["result"] = CHECK
        added = [h for h in nh if h not in ch]
        gone = [h for h in ch if h not in nh]
        out["problems"].append("レポートの列が、いまのシートと違います"
                               + (f"（足される列：{'、'.join(added)}）" if added else "")
                               + (f"（なくなる列：{'、'.join(gone)}）" if gone else "")
                               + ("（並びが違います）" if not added and not gone else "")
                               + "。列の文字で見ている数式があれば崩れます")
    common = [h for h in nh if h in ch]
    ci = [ch.index(h) for h in common]
    ni = [nh.index(h) for h in common]
    cur = [[(r + [""] * len(ch))[i] for i in ci] for r in cur]
    new = [[(r + [""] * len(nh))[i] for i in ni] for r in new]
    types = [types[i] if i < len(types) else "" for i in ni]
    nh = common
    width = len(common)
    id_i = next((i for i, t in enumerate(types) if t == "id"), None)
    if id_i is None:
        id_i = _key_col(cur, new, width)

    def pad(r):
        return (r + [""] * width)[:width]

    if id_i is not None:
        a = {str(r[id_i]): pad(r) for r in cur[1:] if len(r) > id_i and str(r[id_i]).strip()}
        b = {str(r[id_i]): pad(r) for r in new[1:] if len(r) > id_i and str(r[id_i]).strip()}
        pairs = [(k, a[k], b[k]) for k in b if k in a]
        only_new, only_cur = len(set(b) - set(a)), len(set(a) - set(b))
    else:
        n = min(len(cur), len(new)) - 1
        pairs = [(str(i + 2), pad(cur[i + 1]), pad(new[i + 1])) for i in range(max(0, n))]
        only_new, only_cur = max(0, len(new) - len(cur)), max(0, len(cur) - len(new))
    fmt_bad, val_diff, filled = {}, {}, {}
    for key, ra, rb in pairs:
        for j in range(width):
            x, y = ra[j], rb[j]
            if x == "" and y == "":
                continue
            filled[j] = filled.get(j, 0) + 1
            if x == y or (_kind(x) == "num" and _kind(y) == "num" and float(x) == float(y)):
                continue
            if x == "" or y == "":
                val_diff[j] = val_diff.get(j, 0) + 1          # 空になった／入った＝中身の変化
                continue
            if _kind(x) != _kind(y) or (_kind(x) == "str" and _norm(x) == _norm(y)):
                fmt_bad.setdefault(j, []).append((key, x, y))
            else:
                val_diff[j] = val_diff.get(j, 0) + 1
    for j, hits in fmt_bad.items():
        out["result"] = NG
        out["problems"].append(f"「{nh[j]}」の書き方が違います（{len(hits)}件）")
        for key, x, y in hits[:3]:
            out["examples"].append({"列": nh[j], "行": key, "いま": f"{x}（{_kind(x)}）",
                                    "APIで書くと": f"{y}（{_kind(y)}）"})
    for j, n in val_diff.items():
        if n >= 3 and n >= 0.8 * filled.get(j, 0) and types[j] not in _NUMERIC_TYPES | {"date", "datetime"}:
            if out["result"] == OK:
                out["result"] = CHECK
            out["problems"].append(f"「{nh[j]}」がほぼ全部の行で違います（{n}件・前の更新からの変化か、書き方の違いか見てください）")
        else:
            out["notes"].append(f"「{nh[j]}」{n}件")
    if only_new or only_cur:
        out["notes"].append(f"行の出入り：増える{only_new}件・なくなる{only_cur}件")
    if id_i is not None:
        after = [k for k in (str(r[id_i]) for r in new[1:] if len(r) > id_i) if k in a]
        before = [k for k in (str(r[id_i]) for r in cur[1:] if len(r) > id_i) if k in b]
        if after != before:
            if out["result"] == OK:
                out["result"] = CHECK
            out["problems"].append("行の並び順が、いまのシートと違います（並びで見ている数式があれば崩れます）")
    if not pairs:
        if out["result"] == OK:
            out["result"] = CHECK
        out["problems"].append("いまのシートとAPIで同じ行が1つもないので、見比べられません（どちらかが0件など）")
    return out


def _retry(fn, tries: int = 4):
    """Googleの読み取り回数の上限（429）に当たったら、1分待ってやり直す。"""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if "429" not in str(e) or i == tries - 1:
                raise
            time.sleep(65)


def verify_tab(sh, sf, tab: str) -> dict:
    """いまのシートを**書式ごと写した**試しのシートに、本番と同じ書き方で書いて、いまのシートと見比べる。

    ⚠️ 写しにするのは、セルの書式（書式なしテキストなど）で入り方が変わるため
       （地域ガス手配は日付の列が文字の書式で、新しいシートに書くと日付になって違って見えた）。
    いまのシートは触らない。試しのシートは必ず消す。
    """
    res = {"sheet_name": sh.title, "tab": tab, "checked_at": now_stamp()}
    try:
        ws = _retry(lambda: sh.worksheet(tab))
        name = report_name(_retry(lambda: ws.get_note("A1")))
        res["report"] = name
        if not name:
            raise RuntimeError("A1のメモにレポート名がありません")
        cur = _retry(lambda: ws.get_values("A1:ZZ", value_render_option="UNFORMATTED_VALUE"))
        formulas = _retry(lambda: ws.get_values("A1:ZZ", value_render_option="FORMULA"))
        width, _ = _old_extent(cur)
        inside = [f"{_col(j + 1)}{i + 1}" for i, r in enumerate(formulas) for j, v in enumerate(list(r)[:width])
                  if str(v).startswith("=")]
        tmp = _retry(lambda: sh.duplicate_sheet(ws.id, new_sheet_name=TEMP_PREFIX + tab[:80]))
        try:
            shown = _retry(lambda: tmp.get_values("A1:ZZ"))
            types = _retry(lambda: write_report(tmp, sf, shown))[3]
            new = _retry(lambda: tmp.get_values("A1:ZZ", value_render_option="UNFORMATTED_VALUE"))
        finally:
            _retry(lambda: sh.del_worksheet(tmp))
        res.update(compare(cur, new, types))
        if inside:
            res["result"] = NG
            res["problems"].insert(0, f"データの範囲に数式があります（{', '.join(inside[:3])}…）。書き写すと消えるので切り替えません")
        if any(str(v).startswith("=") for r in formulas for v in list(r)[width:]):
            res["notes"].append("右に数式の列があります（触りません）")
    except Exception as e:
        res.update({"result": ERROR, "problems": [str(e)[:300]], "notes": [], "examples": []})
    return res


def scan_targets(supabase, gc) -> list:
    """設定に出てくるスプシを全部開き、A1にSFコネクタのメモがあるシートを集める。

    → [{"key","sheet_name","tab","report","used_by"}]（読めないスプシは {"key","error"}）
    """
    rows = supabase.table("merchants").select("id,config_json").execute().data or []
    keys = {}
    for r in rows:
        if str(r.get("id", "")).startswith(CONFIG_ID):
            continue
        for k in KEY_RE.findall(json.dumps(r.get("config_json") or {})):
            keys.setdefault(k, set()).add(r["id"])
    out = []
    for k, ids in keys.items():
        try:
            sh = _retry(lambda: gc.open_by_key(k))
            titles = [w.title for w in _retry(lambda: sh.worksheets())]
            meta = _retry(lambda: sh.fetch_sheet_metadata(params={
                "includeGridData": "true", "ranges": [f"'{t}'!A1" for t in titles],
                "fields": "sheets(properties(title),data(rowData(values(note))))"}))
            for s in meta.get("sheets", []):
                note = ""
                for d in s.get("data", []) or []:
                    for rd in d.get("rowData", []) or []:
                        for v in rd.get("values", []) or []:
                            note = v.get("note", "") or note
                name = report_name(note)
                if name and not s["properties"]["title"].startswith(TEMP_PREFIX):
                    out.append({"key": k, "sheet_name": sh.title, "tab": s["properties"]["title"],
                                "report": name, "used_by": sorted(ids)})
        except Exception as e:
            out.append({"key": k, "used_by": sorted(ids), "error": str(e)[:200]})
    return out


def verify_all(supabase, gc, sf=None, only=None, on_progress=None) -> list:
    """全部（または only＝[タブID…]）を確かめて、結果を `__sf_api__` に残す → 結果の並び。

    ⚠️ 人がOKした（approved）ものは、確かめ直しても中身が同じ判定なら残す。
    ⚠️ お客様の中身（examples）は Supabase に入れない（画面に出すだけ）。
    """
    import salesforce_loader as sfl
    sf = sf or sfl.connect()
    targets = [t for t in scan_targets(supabase, gc) if "error" not in t]
    if only:
        targets = [t for t in targets if tab_id(t["key"], t["tab"]) in set(only)]
    prev = load_cfg(supabase).get("tabs") or {}
    results, sheets = [], {}
    for i, t in enumerate(targets, 1):
        if on_progress:
            on_progress(i, len(targets), t)
        sh = sheets.get(t["key"]) or _retry(lambda: gc.open_by_key(t["key"]))
        sheets[t["key"]] = sh
        r = verify_tab(sh, sf, t["tab"])
        time.sleep(4)            # Googleの読み取り回数の上限（1分あたり）に当たらないように
        r.update({"key": t["key"], "used_by": t["used_by"]})
        tid = tab_id(t["key"], t["tab"])
        keep = (prev.get(tid) or {})
        approved = bool(keep.get("approved")) and r["result"] == CHECK
        save_cfg(supabase, tabs={tid: {
            "result": r["result"], "approved": approved, "report": r.get("report", ""),
            "sheet_name": r["sheet_name"], "tab": t["tab"], "checked_at": r["checked_at"],
            "problems": r.get("problems", [])[:6], "notes": r.get("notes", [])[:6],
            "used_by": t["used_by"]}})
        results.append(r)
    return results


def verify_after_connector(items, budget_sec: int = 180):
    """ロボット（SFコネクタ）で更新した**直後**に、まだ切り替えていないシートを確かめる。

    ⭐ コネクタで更新した直後なら、いまのシートとSalesforceの中身が同じ時点のものになる
       ＝ ずれ（前の更新からの変化）が無く、書き方の違いだけを正しく見分けられる。
       0件で見比べられなかったシートも、行のある日に確かめ直せる（少しずつ自動で切り替わる）。
    ⚠️ 1日1回まで・時間の上限つき。つまずいても更新の結果には影響させない。
    """
    try:
        sb, gc, sf = clients()
        cfg = load_cfg(sb)
        if not cfg.get("on", True):
            return
        t0, today = time.time(), now_stamp()[:10]
        for it in items or []:
            if time.time() - t0 > budget_sec:
                break
            k, tab = it.get("key"), it.get("tab")
            if not (k and tab):
                continue
            prev = (cfg.get("tabs") or {}).get(tab_id(k, tab)) or {}
            if use_api(cfg, k, tab) or str(prev.get("checked_at", "")).startswith(today):
                continue
            sh = gc.open_by_key(k)
            r = verify_tab(sh, sf, tab)
            save_cfg(sb, tabs={tab_id(k, tab): {
                "result": r["result"], "approved": bool(prev.get("approved")) and r["result"] == CHECK,
                "report": r.get("report", ""), "sheet_name": r["sheet_name"], "tab": tab,
                "checked_at": r["checked_at"], "problems": r.get("problems", [])[:6],
                "notes": (r.get("notes", []) + ["SFコネクタで更新した直後に確かめました"])[:6],
                "used_by": prev.get("used_by", [])}})
    except Exception:
        pass


# ==========================================
# 🔁 更新の本体（sms_runner.run_sheet_refresh から呼ぶ）
# ==========================================
def clients():
    """(supabase, gspread, salesforce)。どれかが無ければ例外。"""
    import auto_jobs
    import salesforce_loader as sfl
    s = auto_jobs.load_secrets()
    sb = auto_jobs.supabase_client(s)
    gc = auto_jobs.gspread_client(s.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    if gc is None:
        raise RuntimeError("サービスアカウント（GOOGLE_SERVICE_ACCOUNT_JSON）がありません")
    return sb, gc, sfl.connect()


def plan(tabs, tab_urls, url, supabase=None, gc=None) -> list:
    """更新するシートごとに、APIで書くか → [{"tab","key","url","api"}]。

    tabs が空（URLの #gid= でシートを指す呼び方）のときは、gid からシート名を引く。
    """
    import auto_jobs
    if supabase is None:
        supabase = auto_jobs.supabase_client(auto_jobs.load_secrets())
    cfg = load_cfg(supabase)
    items = []
    tabs = [t for t in (tabs or []) if str(t).strip()]
    if tabs:
        urls = list(tab_urls or [])
        for i, t in enumerate(tabs):
            u = urls[i] if i < len(urls) and urls[i] else url
            k = key_of(u) or key_of(url)
            items.append({"tab": t, "key": k, "url": u, "api": bool(k) and use_api(cfg, k, t)})
        return items
    k = key_of(url)
    m = re.search(r"gid=(\d+)", str(url or ""))
    if not (k and m and cfg.get("on", True)):
        return [{"tab": "", "key": k, "url": url, "api": False}]
    if gc is None:
        gc = auto_jobs.gspread_client(auto_jobs.load_secrets().get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    title = next((w.title for w in gc.open_by_key(k).worksheets() if str(w.id) == m.group(1)), "")
    return [{"tab": title, "key": k, "url": url, "api": bool(title) and use_api(cfg, k, title)}]
