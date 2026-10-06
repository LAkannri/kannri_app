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
     ⭐ コネクタも表示の文字を打ち込んでいるだけなので、日付・数字・チェックがコネクタと同じ値になる
       （2026-10-06、付箋の総務用3シートで全セルの値を照らし合わせて違い0件）。
  ④ 前の行・列の残りを消す（メモは消えない）。

⚠️ Streamlit を import しない（画面以外からも使えるように）。
"""
import re
import time

NOTE_RE = re.compile(r"Report:\s*(.+)")
_DIGITS_ZERO = re.compile(r"^0\d+$")
_NUMERIC_TYPES = {"double", "int", "currency", "percent"}


def report_name(note: str) -> str:
    """A1のメモ → レポート名（無ければ空）。"""
    m = NOTE_RE.search(str(note or ""))
    return m.group(1).strip() if m else ""


def find_report_id(sf, name: str) -> str:
    """名前 → レポートID。⚠️ 0件・2件以上は止める（違うレポートを書き写さないため）。"""
    q = "SELECT Id, Name FROM Report WHERE Name = '%s'" % name.replace("\\", "\\\\").replace("'", "\\'")
    recs = sf.query_all(q).get("records", [])
    if not recs:
        raise RuntimeError(f"Salesforceにレポート「{name}」が見つかりません（名前が変わったかもしれません）")
    if len(recs) > 1:
        raise RuntimeError(f"Salesforceに「{name}」という名前のレポートが{len(recs)}つあります。"
                           f"どれか決められないので止めました")
    return recs[0]["Id"]


def _cell(text, dtype: str) -> str:
    """APIの表示の文字 → シートに打ち込む文字。"""
    v = "" if text in (None, "-") else str(text)
    if not v:
        return ""
    # ⚠️ 「=」で始まる文は数式になってしまうので、文字として入れる
    if v.startswith("="):
        return "'" + v
    # ⚠️ 0で始まる数字だけの並び（ハイフンの無い電話番号など）は、数字にすると頭の0が落ちる
    if dtype not in _NUMERIC_TYPES and _DIGITS_ZERO.match(v):
        return "'" + v
    return v


def read_report(sf, report_id: str) -> tuple:
    """(見出し, 行) を、レポートの画面に出ている文字のまま読む。

    ⚠️ 表形式のレポートだけ（まとめ・グループのあるレポートは行の並びが違う）。
    ⚠️ レポートAPIは2000行まで。超えたら、途中で切れたまま書かないよう止める。
    """
    r = sf.restful(f"analytics/reports/{report_id}", params={"includeDetails": "true"})
    meta = r.get("reportMetadata") or {}
    fmt = str(meta.get("reportFormat", ""))
    if fmt != "TABULAR":
        raise RuntimeError(f"表形式ではないレポート（{fmt}）は書き写せません")
    if not r.get("allData", True):
        raise RuntimeError("レポートの行が2000行を超えていて、全部を読めませんでした")
    info = r["reportExtendedMetadata"]["detailColumnInfo"]
    cols = meta.get("detailColumns") or []
    head = [info[c]["label"] for c in cols]
    types = [info[c].get("dataType", "") for c in cols]
    body = []
    for row in (r.get("factMap", {}).get("T!T", {}) or {}).get("rows", []):
        body.append([_cell(cell.get("label"), t) for cell, t in zip(row.get("dataCells", []), types)])
    return head, body


def _col(n: int) -> str:
    """1 → A, 27 → AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_tab(ws, head: list, body: list):
    """シートに書く → 前の残り（下の行・右の列）を消す。メモ（コネクタの設定）は残る。"""
    rows, cols = len(body) + 1, max(len(head), 1)
    if ws.row_count < rows:
        ws.add_rows(rows - ws.row_count)
    if ws.col_count < cols:
        ws.add_cols(cols - ws.col_count)
    ws.update([head] + body, "A1", value_input_option="USER_ENTERED")
    last_col, last_row = _col(max(ws.col_count, cols)), max(ws.row_count, rows)
    clear = []
    if last_row > rows:
        clear.append(f"A{rows + 1}:{last_col}{last_row}")
    if ws.col_count > cols:
        clear.append(f"{_col(cols + 1)}1:{last_col}{rows}")
    if clear:
        ws.batch_clear(clear)


def refresh_tabs(gc, sheet_url: str, tabs, sf=None) -> tuple:
    """シートたちを順に書き写す → (全部できたか, ログ)。1枚つまずいても残りは続ける。"""
    import salesforce_loader as sfl
    lines, ok = [], True
    tabs = [t for t in (tabs or []) if t]
    if not tabs:
        return False, "更新するシートがありません"
    try:
        sh = gc.open_by_url(sheet_url) if str(sheet_url).startswith("http") else gc.open_by_key(sheet_url)
        sf = sf or sfl.connect()
    except Exception as e:
        return False, f"❌ つなげませんでした：{str(e)[:300]}"
    for i, t in enumerate(tabs, 1):
        t0 = time.time()
        try:
            ws = sh.worksheet(t)
            name = report_name(ws.get_note("A1"))
            if not name:
                raise RuntimeError("A1のメモにレポート名がありません（一度SFコネクタでこのシートにレポートを入れてください）")
            head, body = read_report(sf, find_report_id(sf, name))
            write_tab(ws, head, body)
            lines.append(f"✅ {i}/{len(tabs)}：{t} ← {name}　{len(body)}件（{time.time() - t0:.1f}秒）")
        except Exception as e:
            ok = False
            lines.append(f"❌ {i}/{len(tabs)}：{t}　{str(e)[:300]}")
    return ok, "\n".join(lines)
