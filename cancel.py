"""
🛑 変更・キャンセル（LL／N）の中身。画面（`pages/5_…`）から使う。

【いままでの流れ（LL・スプシのボタン）】
  ① SFコネクタで「BOX」を更新（レポート「LL変更・キャンセル」）
  ② BOX から、キャリアごとのシート（ニチガス／JAPAN電力／東京ガスセット…）が**数式で**できる
  ③ 「すべてのメール下書きを作成」＝GASが Gmail に下書きを作る（ライフイン24はExcelをDriveにも保存）
  ④ 「BOXシートからDLシートへ読込」→ DLで付箋チェックを「完了」に → CSVにしてData Loader

【この画面の考え方】
  ⭐ **振り分け（数式）とメールの中身（GAS）は、スプシのものをそのまま使う**。
     アプリに同じことを書くと、片方だけ直して食い違う（SMS送信・データローダーと同じ方針）。
     GASは `エンカンAI_連携WebAPI` の `build` で、**下書きを作る関数だけ**を呼ぶ。
     ⚠️ `createAllDrafts` は `ui.alert`（確認の小窓）を使うので、人のいない呼び出しでは落ちる。
        3営業日以内の確認は、アプリの画面で行う（`urgent_rows`）。
  ⭐ ④は DL→CSV→Data Loader をやめ、**選んだ案件の付箋チェックだけ**を「完了」にする。
     備考は DL のように丸ごと送らず、**うしろに1行足す**（`sf_ui.append_remark`）。
     丸ごと送ると、BOXを更新したあとに誰かが書いた備考を消してしまうため。
  ⭐ LL と N は**同じ作り**で、違うのは設定だけ（`sets` の1件＝1つのスプシ）。

設定は Supabase の予約行 `__cancel__` の `config_json.sets`（{"LL": {...}, "N": {...}}）。
⚠️ スプシのURLはコードに書かない（公開リポジトリ）。
"""
import datetime as _dt
import re
import time
import unicodedata

SETTINGS_ID = "__cancel__"
SET_NAMES = ["LL", "N"]

# 付箋の「内容」のうち、キャリアへ依頼して備考に書き残すもの（スプシの onEdit と同じ）
REMARK_WORDS = {
    "キャンセル処理": "キャンセル依頼済",
    "情報修正(詳細は備考)": "情報修正依頼済",
    "情報修正": "情報修正依頼済",
}

# 📋 LL の既定（2026-09-29 に「LL変更キャンセル 【アプリ】」のスプシとGASを読んで決めた）
DEFAULTS = {
    "LL": {
        "box_tab": "BOX",
        "sender_cell": "ボタン!B17",          # GASが「株式会社ライフアップの◯◯」に使う担当者名
        "check_field": "Lc__c",               # L-付箋：チェック
        "remark_fields": {"ガス": "GasRemarks__c", "電気＆ガス": "GasRemarks__c"},
        "cols": {"id": "案件 ID", "no": "案件番号", "name": "名前", "kind": "L-付箋商材種別",
                 "content": "L-付箋：内容", "detail": "L-付箋：内容詳細（必要時）",
                 "to": "L-付箋対応先", "check": "L-付箋：チェック",
                 "power": "電力キャリア", "gas": "ガスキャリア"},
        "date_cols": ["電力利用開始日", "ガス立合希望日"],
        # ✉️ GASが下書きを作るもの（関数はスプシのGASにあるものをそのまま呼ぶ）
        "drafts": [
            {"名前": "ライフイン24（ニチガス・東邦ガス・オクトパス）",
             "関数": "createEmailDraftWithExcel", "シート": "ニチガス,東邦ガス,オクトパスエナジー"},
            {"名前": "INE（JAPAN電力）", "関数": "createJapanDenkiEmailDraft", "シート": "JAPAN電力"},
            {"名前": "東京ガスセット", "関数": "createTokyoGasEmailDraft", "シート": "東京ガスセット"},
            {"名前": "ファインガス（1人1通）", "関数": "createFineGasEmailDraft", "シート": "ファインガス"},
        ],
        # ✋ GASがメールを作らない＝人が中身を見て連携するシート
        "manual_sheets": ["取り直し", "東急でんきガス", "HTBエナジー", "楽々でんき",
                          "ニチガス（空室)", "ミツウロコ"],
    },
    "N": {
        "box_tab": "BOX",
        "sender_cell": "",
        "check_field": "Stickycheck__c",      # N-付箋：チェック
        "remark_fields": {},
        "cols": {"id": "案件 ID", "no": "案件番号", "name": "名前", "kind": "N-付箋商材種別",
                 "content": "N-付箋：内容", "detail": "N-付箋：内容詳細（必要時）",
                 "to": "N-付箋対応先", "check": "N-付箋：チェック"},
        "date_cols": [],
        "drafts": [],
        "manual_sheets": [],
    },
}


def merged(name: str, cfg: dict) -> dict:
    """既定の上に、保存した設定を重ねる（列名などは足りない分だけ既定で埋める）。"""
    base = dict(DEFAULTS.get(name) or DEFAULTS["LL"])
    out = dict(base)
    for k, v in (cfg or {}).items():
        if k == "cols":
            out["cols"] = {**base.get("cols", {}), **(v or {})}
        elif v not in (None, ""):
            out[k] = v
    return out


def norm(s) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


# ==========================================
# 📄 読む
# ==========================================
def _open(gc, url: str):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


def read_tab(gc, url: str, tab: str):
    """(見出し, 行のdictの並び)。空の行は落とす。"""
    vals = _open(gc, url).worksheet(tab).get_all_values()
    if not vals:
        return [], []
    head = [str(h).strip() for h in vals[0]]
    rows = []
    for r in vals[1:]:
        if any(str(x).strip() for x in r):
            rows.append(dict(zip(head, (list(r) + [""] * len(head))[:len(head)])))
    return head, rows


def valid_rows(head, rows):
    """キャリアのシートの「中身のある行」。GASと同じ見分け方（A・B列が空／該当／#エラー は外す）。"""
    out = []
    for r in rows:
        a = str(r.get(head[0], "") if head else "").strip()
        b = str(r.get(head[1], "") if len(head) > 1 else "").strip()
        if not a or "該当" in a or a.startswith("#"):
            continue
        if "該当" in b or b.startswith("#"):
            continue
        out.append(r)
    return out


def dropped_rows(head, rows):
    """中身はあるのに、GASが外してしまう行（A列が空・#エラー）。「該当データなし」はふつうなので数えない。

    ⚠️ 2026-09-29 に実際にあった：ニチガスのシートで、顧客番号（A列）を引く `ニチガスID検知` が `#REF!` になり、
       キャンセルの行があるのにA列が空＝ライフイン24のExcelに入らない（GASは黙って飛ばす）。
    """
    ok = {id(r) for r in valid_rows(head, rows)}
    out = []
    for r in rows:
        if id(r) in ok:
            continue
        vals = [str(v).strip() for v in r.values()]
        if any("該当" in v for v in vals[:2]):
            continue
        if sum(1 for v in vals if v) >= 2:
            out.append(r)
    return out


def _to_rows(vals):
    if not vals:
        return [], []
    head = [str(h).strip() for h in vals[0]]
    rows = [dict(zip(head, (list(r) + [""] * len(head))[:len(head)]))
            for r in vals[1:] if any(str(x).strip() for x in r)]
    return head, rows


def read_all(gc, url: str, box_tab: str, names):
    """BOXとキャリアのシートを**1回の問い合わせで**読む（1枚ずつだと十数枚で30秒近くかかった）。
    戻り値：(BOXの見出し, BOXの行, read_sheets と同じ形)
    """
    names = [n for n in dict.fromkeys(str(x).strip() for x in (names or [])) if n]
    sh = _open(gc, url)
    have = {w.title for w in sh.worksheets()}
    if box_tab not in have:
        raise RuntimeError(f"シート「{box_tab}」がありません")
    ok = [n for n in names if n in have]
    q = [box_tab] + ok
    res = sh.values_batch_get(["'" + n.replace("'", "''") + "'" for n in q])
    got = {n: (vr.get("values") or []) for n, vr in zip(q, res.get("valueRanges") or [])}
    head, box = _to_rows(got.get(box_tab))
    out = {}
    for n in names:
        if n not in have:
            out[n] = {"head": [], "rows": [], "dropped": [], "error": "シートがありません"}
            continue
        h, rows = _to_rows(got.get(n))
        out[n] = {"head": h, "rows": valid_rows(h, rows), "dropped": dropped_rows(h, rows), "error": ""}
    return head, box, out


def read_sheets(gc, url: str, names) -> dict:
    """{シート名: {"head", "rows"（中身のある行）, "dropped"（GASが外す行）, "error"}}"""
    out = {}
    for n in names or []:
        n = str(n).strip()
        if not n or n in out:
            continue
        try:
            head, rows = read_tab(gc, url, n)
            out[n] = {"head": head, "rows": valid_rows(head, rows),
                      "dropped": dropped_rows(head, rows), "error": ""}
        except Exception as e:
            out[n] = {"head": [], "rows": [], "dropped": [], "error": str(e)[:200]}
    return out


def split_names(v) -> list:
    if isinstance(v, (list, tuple)):
        items = v
    else:
        items = re.split(r"[,、\n]", str(v or ""))
    return [str(x).strip() for x in items if str(x).strip()]


# ==========================================
# 🔎 どこにも載っていない案件（振り分け漏れ）
# ==========================================
def where_listed(box_rows, sheets: dict, cols: dict) -> dict:
    """案件ID → 載っているシート名の並び。

    ⭐ 案件ID・案件番号・名前（空白抜き）のどれかが、そのシートのどこかのセルに入っていれば「載っている」。
       シートごとに列の並びが違う（ニチガスは需要者氏名、楽々でんきは名義＋電話番号…）ので、列を決め打ちしない。
    """
    cells = {n: [norm(v) for r in d["rows"] for v in r.values() if norm(v)]
             for n, d in sheets.items()}
    out = {}
    for r in box_rows:
        keys = [norm(r.get(cols.get("id"), "")), norm(r.get(cols.get("no"), "")),
                norm(r.get(cols.get("name"), ""))]
        keys = [k for k in keys if len(k) >= 2]
        hit = [n for n, cs in cells.items() if any(k in c for k in keys for c in cs)]
        out[str(r.get(cols.get("id"), "")).strip()] = hit
    return out


def needs_carrier(r, cols: dict) -> bool:
    """キャリアへの連携が要る案件か（対応先が「キャリア」／空）。お客様・不動産あては人が別に動く。"""
    to = str(r.get(cols.get("to"), "") or "").strip()
    return to in ("", "キャリア")


# ==========================================
# 🚨 利用開始が近い案件（GAS の checkUrgentDates と同じ考え：今日〜3営業日後）
# ==========================================
def _is_holiday(d: _dt.date) -> bool:
    if d.weekday() >= 5:
        return True
    try:
        import jpholiday
        return bool(jpholiday.is_holiday(d))
    except Exception:
        return False


def cutoff(today: _dt.date = None, days: int = 3) -> _dt.date:
    d = today or _dt.date.today()
    n = 0
    while n < days:
        d += _dt.timedelta(days=1)
        if not _is_holiday(d):
            n += 1
    return d


def parse_date(v):
    s = unicodedata.normalize("NFKC", str(v or "")).strip()
    m = re.search(r"(\d{4})[/\-年.](\d{1,2})[/\-月.](\d{1,2})", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.fullmatch(r"\d{5}", s):          # シリアル値のまま読めたとき
        return _dt.date(1899, 12, 30) + _dt.timedelta(days=int(s))
    return None


def urgent_rows(box_rows, date_cols, today: _dt.date = None):
    """[(行, 見出し, 日付)]。⚠️ GASは米国時間で「今日」を出していた（スクリプトの時刻がNY）。こちらは日本時間。"""
    today = today or _dt.date.today()
    last = cutoff(today)
    out = []
    for r in box_rows:
        for c in date_cols or []:
            d = parse_date(r.get(c, ""))
            if d and today <= d <= last:
                out.append((r, c, d))
                break
    return out


# ==========================================
# ✉️ 下書き（GAS）
# ==========================================
def _cell(a1: str):
    """'ボタン!B17' → ('ボタン', 17, 2)。読めなければ None。"""
    m = re.fullmatch(r"\s*'?(.+?)'?!\$?([A-Za-z]+)\$?(\d+)\s*", str(a1 or ""))
    if not m:
        return None
    col = 0
    for ch in m.group(2).upper():
        col = col * 26 + (ord(ch) - 64)
    return m.group(1), int(m.group(3)), col


def read_sender(gc, url: str, a1: str) -> str:
    c = _cell(a1)
    if not c:
        return ""
    try:
        return str(_open(gc, url).worksheet(c[0]).cell(c[1], c[2]).value or "").strip()
    except Exception:
        return ""


def write_sender(gc, url: str, a1: str, name: str):
    """担当者名をスプシのセルに書く（GASはそこを読んで、メールの名乗りに使う）。RAWで書く。"""
    import sms_runner
    c = _cell(a1)
    if not c:
        raise RuntimeError(f"担当者名のセル「{a1}」が読めません（例：ボタン!B17）")
    sms_runner.write_cells(gc, url, c[0], [(c[1], c[2], str(name or "").strip())])


def make_drafts(set_cfg: dict, funcs) -> tuple:
    """GASの下書き関数を順に走らせる。戻り値：(うまくいったか, 返事 or エラーの文)"""
    import sms_runner
    funcs = [f for f in (funcs or []) if str(f).strip()]
    if not funcs:
        return False, "作る下書きが選ばれていません"
    url = str(set_cfg.get("gas_url", "") or "").strip()
    if not url:
        return False, "GASがまだ入っていません（⚙️ 設定 → 🤖 GAS）"
    return sms_runner.run_gas_action(url, str(set_cfg.get("gas_token", "") or ""),
                                     "build", build=",".join(funcs), timeout=600)


# ==========================================
# ✅ Salesforceの付箋を「完了」にする
# ==========================================
def remark_text(r, cols: dict, who: str, today: _dt.date = None):
    """備考に足す1行（スプシの onEdit と同じ形：`9/29　キャンセル依頼済　理田`）。足さないときは ""。"""
    word = REMARK_WORDS.get(str(r.get(cols.get("content"), "") or "").strip())
    if not word:
        return ""
    d = today or _dt.date.today()
    return f"{d.month}/{d.day}　{word}　{str(who or '').strip()}".rstrip()


def remark_field(r, cols: dict, fields: dict) -> str:
    """その案件の備考の項目。商材種別で決める（LL：ガス・電気＆ガス → ガス備考。スプシの onEdit と同じ）。"""
    kind = norm(r.get(cols.get("kind"), "")).replace("&", "＆")
    return next((f for k, f in (fields or {}).items() if norm(k).replace("&", "＆") == kind), "")


def mark_done(set_cfg: dict, rows, who: str) -> list:
    """選んだ案件の付箋チェックを「完了」にし、備考にうしろから1行足す。

    ⚠️ 送るのは付箋チェックの1項目だけ（DLのように備考を丸ごと送らない＝あとから書かれた備考を消さない）。
    ⚠️ 備考は、完了にできた案件だけに足す。
    戻り値：[{"案件ID", "名前", "完了", "備考"}]
    """
    import salesforce_loader as sfl
    import sf_ui
    cols = set_cfg.get("cols") or {}
    field = str(set_cfg.get("check_field", "") or "").strip()
    if not field:
        raise RuntimeError("付箋チェックの項目（API名）が設定されていません")
    rows = [r for r in rows if str(r.get(cols.get("id"), "") or "").strip()]
    sf = sfl.connect()
    recs = [{"Id": str(r[cols["id"]]).strip(), field: "完了"} for r in rows]
    res = sfl.upsert(sf, "Opportunity", "Id", recs)
    bad, all_bad = {}, ""
    for e in res.get("errors") or []:
        k = str(e.get("Id") or "").strip()
        if "〜" in k:                      # まとめて送れなかった（どれも入っていない）
            all_bad = str(e.get("原因", "") or "失敗")[:150]
        bad[k] = str(e.get("原因", "") or "失敗")[:150]
    if res.get("ng") and not all_bad and len(bad) < int(res.get("ng") or 0):
        all_bad = "送れませんでした（どの案件か分かりません。Salesforceで確かめてください）"
    fields = set_cfg.get("remark_fields") or {}
    out = []
    for r in rows:
        rid = str(r[cols["id"]]).strip()
        row = {"案件ID": rid, "名前": r.get(cols.get("name"), ""), "完了": "", "備考": ""}
        if rid in bad or all_bad:
            row["完了"] = "❌ " + (bad.get(rid) or all_bad)
            out.append(row)
            continue
        row["完了"] = "✅"
        text = remark_text(r, cols, who)
        rf = remark_field(r, cols, fields)
        if text and rf:
            why = sf_ui.append_remark("Opportunity", rid, rf, text)
            if not why:
                row["備考"] = "✅ " + text
            elif why.startswith("＿"):          # 既に同じ文が入っていた
                row["備考"] = "✅ " + why.lstrip("＿")
            else:
                row["備考"] = "❌ " + why
        out.append(row)
    return out


def today_str() -> str:
    return time.strftime("%Y-%m-%d")
