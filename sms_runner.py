"""
📱 SMS送信（プッシュプロ一括送信）の下ごしらえ。

画面（pages/6_📱_SMS送信.py）から使う道具をここにまとめる。
やることは大きく4つ：

1. リフレッシュ … Salesforceコネクタで連携しているシートを、録画したロボットが更新する
2. チェック     … 「この列が空はNG」などのルールで、直すべき行を洗い出す
3. CSVにする    … 送信用シートを CSV にして「取り込みファイル/SMS送信用」に置く
                   （今日の分だけを残し、昨日までの分は消す）
4. 一括送信     … 録画したロボットが、その CSV をプッシュプロに入れて送信する

画面（Streamlit）に依存しないよう、ここには st を持ち込まない。
"""
import csv
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

INTAKE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "取り込みファイル")
SMS_ROOT = os.path.join(INTAKE_ROOT, "SMS送信用")

# CSVの文字コード（プッシュプロ側の取り込み仕様に合わせて選ぶ）
ENCODINGS = {
    "UTF-8（BOMつき）": "utf-8-sig",
    "Shift_JIS": "cp932",
    "UTF-8（BOMなし）": "utf-8",
}


def _safe(name: str) -> str:
    """フォルダ名に使えない文字を落とす。"""
    return re.sub(r'[\\/:*?"<>|]', "_", str(name or "").strip()) or "その他"


def pattern_dir(pattern: str) -> str:
    """このパターンのCSVを置くフォルダ（無ければ作る）。"""
    path = os.path.join(SMS_ROOT, _safe(pattern))
    os.makedirs(path, exist_ok=True)
    return path


def today_stamp() -> str:
    return time.strftime("%Y%m%d")


CSV_NAME = "送信データ.csv"       # プッシュプロに渡すファイル。毎回この名前で上書きする
HISTORY_DIR = "履歴"              # 日付つきの控え（証跡）。送信には使わない
KEEP_HISTORY_DAYS = 30


def sheet_slot(pattern: str, sheet: str = "") -> str:
    """CSVの置き場所の名前。

    1パターンで複数のシートをCSVにできるので、**シートごとに分けて置く**
    （同じ名前に上書きすると、先に作ったほうが消える）。
    ⚠️ 送信の記録（誰に送ったか）は**パターンでまとめる**。
       同じ人に、別のシートから二度届くのを防ぐため。
    """
    sheet = str(sheet or "").strip()
    return f"{pattern}／{sheet}" if sheet else str(pattern)


def csv_path(pattern: str) -> str:
    """プッシュプロに渡すCSVの置き場所。**毎回この同じ名前**で上書きする。

    ファイル名が毎日変わると、録画したときのパスが翌日には通じない。
    名前を固定しておけば、録画で選んだファイルをそのまま使い続けられる
    （＝スプシが増えても録画し直さなくてよい）。
    """
    return os.path.join(pattern_dir(pattern), CSV_NAME)


def history_dir(pattern: str) -> str:
    path = os.path.join(pattern_dir(pattern), HISTORY_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _keep_history(pattern: str, data: bytes, label: str = ""):
    """その日に送ったファイルの控えを残す（あとで「何を送ったか」を確かめられるように）。"""
    name = f"{time.strftime('%Y%m%d_%H%M%S')}{('_' + _safe(label)) if label else ''}.csv"
    path = os.path.join(history_dir(pattern), name)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        return ""
    # 古い控えは片づける（増え続けないように）
    limit = time.time() - KEEP_HISTORY_DAYS * 86400
    for old in glob.glob(os.path.join(history_dir(pattern), "*.csv")):
        try:
            if os.path.getmtime(old) < limit:
                os.remove(old)
        except Exception:
            pass
    return path


def _put_csv(pattern: str, data: bytes, label: str = ""):
    """CSVを「毎回同じ名前」で置き、控えも残す。戻り値：(パス, 控えのパス)"""
    path = csv_path(pattern)
    with open(path, "wb") as f:
        f.write(data)
    return path, _keep_history(pattern, data, label)


def today_csv(pattern: str):
    """きょう用意したCSVを返す。前の日のままなら None（古いものを送らないため）。"""
    path = csv_path(pattern)
    if not os.path.isfile(path):
        return None
    if time.strftime("%Y%m%d", time.localtime(os.path.getmtime(path))) != today_stamp():
        return None
    return path


def csv_made_at(pattern: str) -> str:
    path = csv_path(pattern)
    if not os.path.isfile(path):
        return ""
    return time.strftime("%Y/%m/%d %H:%M", time.localtime(os.path.getmtime(path)))



# ==========================================
# 📄 シートを読む／CSVにする
# ==========================================
def read_tab(gc, sheet_url: str, tab: str):
    """スプレッドシートの1タブを、そのまま2次元の表で返す（1行目＝見出し）。"""
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    ws = sh.worksheet(tab)
    return ws.get_all_values()


def _a1(row: int, col: int) -> str:
    """(3, 2) → "B3" 。列は1から数える。"""
    name = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        name = chr(65 + rem) + name
    return f"{name}{row}"


def write_cells(gc, sheet_url: str, tab: str, changes) -> int:
    """直したセルだけを、スプレッドシートに書き戻す。

    changes：[(行番号, 列番号, 新しい値), ...]（どちらも1から数える。行は見出しが1行目）
    ⚠️ **変えたセルだけ**を送る。表ごと上書きすると、
       画面に出していない列や数式まで消してしまうため。
    ⚠️ **RAW で書く**（USER_ENTERED にしない）。
       USER_ENTERED はスプレッドシートに解釈させるので、
       携帯番号 `090…` を数値と見なして**先頭の0を落とす**。
       このアプリが扱うのは電話番号なので、そのままの文字で書き込む。
    戻り値：書き戻した件数
    """
    changes = [c for c in (changes or [])]
    if not changes:
        return 0
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    ws = sh.worksheet(tab)
    ws.batch_update([{"range": _a1(r, c), "values": [[v]]} for r, c, v in changes],
                    value_input_option="RAW")
    return len(changes)


def _csv_bytes(values, encoding: str) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for row in values:
        w.writerow(row)
    text = buf.getvalue()
    if encoding == "cp932":
        # Shift_JIS に無い文字（絵文字・㈱など）で落ちないよう、置き換えて出す
        return text.encode("cp932", errors="replace")
    return text.encode(encoding)


def export_csv(gc, sheet_url: str, tab: str, pattern: str, encoding_label: str = "Shift_JIS",
               skip_empty_col: str = ""):
    """送信用シートを CSV にして、パターンのフォルダに**毎回同じ名前で**置く。

    戻り値：(CSVのパス, データ件数, 控えのパス)
    """
    values = read_tab(gc, sheet_url, tab)
    if not values:
        raise ValueError(f"シート「{tab}」が空です。")

    headers = values[0]
    rows = values[1:]
    # 見出しだけの行や、まるごと空の行は入れない（プッシュプロ側で空行エラーになるため）
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    # 「この列が空の行は送らない」（例：連絡先が無い行）
    if skip_empty_col and skip_empty_col in headers:
        idx = headers.index(skip_empty_col)
        rows = [r for r in rows if idx < len(r) and str(r[idx]).strip()]

    enc = ENCODINGS.get(encoding_label, "cp932")
    data = _csv_bytes([headers] + rows, enc)
    path, hist = _put_csv(pattern, data, "作成")
    return path, len(rows), hist


# ==========================================
# ✅ 送信前チェック（直すべき行を洗い出す）
# ==========================================
# ルール名 → 説明。画面のプルダウンにもそのまま出す。
RULES = {
    "空はNG": "その列が空の行を見つけます（例：電話番号が入っていない）",
    "数字だけ": "数字以外（文字・記号）が混ざっている行を見つけます",
    "電話番号の形": "日本の携帯・固定番号として不自然な行を見つけます",
    "重複はNG": "同じ値が2回以上ある行を見つけます（例：同じ番号に2通送ってしまう）",
    "この文字を含む": "指定した文字が入っている行を見つけます（例：「テスト」）",
    "この文字を含まない": "指定した文字が入っていない行を見つけます",
    "決まった値のどれか": "指定した値（／区切り）以外が入っている行を見つけます",
    "文字数の上限": "指定した文字数を超えている行を見つけます（SMSの本文など）",
}
# 「値」欄の入力が必要なルール
RULES_NEED_VALUE = {"この文字を含む", "この文字を含まない", "決まった値のどれか", "文字数の上限"}


def _norm(s) -> str:
    """全角半角・空白のゆれを揃える（見た目が同じなのに違う扱いになるのを防ぐ）。"""
    return unicodedata.normalize("NFKC", str(s or "")).strip()


_PHONE_OK = re.compile(r"^(0\d{9,10})$")


def _judge(rule: str, value: str, param: str, seen: dict, raw_col_values, row_i: int):
    """1セルを1ルールで見て、問題があれば理由の文字列を返す。無ければ空文字。"""
    v = _norm(value)
    if rule == "空はNG":
        return "空です" if not v else ""
    if not v:
        # 空のセルは「空はNG」以外では見ない（空チェックは上のルールの仕事）
        return ""
    if rule == "数字だけ":
        return "" if v.isdigit() else "数字以外が入っています"
    if rule == "電話番号の形":
        digits = re.sub(r"[-ー－\s]", "", v)
        return "" if _PHONE_OK.match(digits) else "電話番号として不自然です"
    if rule == "重複はNG":
        # 「090-1234-5678」と「09012345678」を別物と見ると、同じ人に2通いってしまう。
        # 数字だけの並びになる場合に限って、ハイフンを外してから見比べる。
        _bare = re.sub(r"[-ー－\s]", "", v)
        if _bare.isdigit():
            v = _bare
        first = seen.get(v)
        if first is not None and first != row_i:
            return f"{first} 行目と同じ値です"
        seen.setdefault(v, row_i)
        return ""
    if rule == "この文字を含む":
        return "" if param and param in v else f"「{param}」が入っていません"
    if rule == "この文字を含まない":
        return f"「{param}」が入っています" if param and param in v else ""
    if rule == "決まった値のどれか":
        allowed = [_norm(x) for x in str(param or "").split("/") if _norm(x)]
        return "" if v in allowed else f"「{param}」のどれでもありません"
    if rule == "文字数の上限":
        try:
            limit = int(str(param).strip())
        except Exception:
            return ""
        return f"{len(v)}文字（上限{limit}）" if len(v) > limit else ""
    return ""


def check_rules(gc, sheet_url: str, rules):
    """設定されたルールで全シートを見て、直すべき行の一覧を返す。

    rules の1件：{"シート": "送信用", "列": "電話番号", "ルール": "空はNG", "値": "", "メモ": ""}
    戻り値：(見つかった問題のリスト, 読めなかったシートのメモ)
    """
    findings, notes = [], []
    by_tab = {}
    for r in rules or []:
        if not str(r.get("シート", "")).strip() or not str(r.get("ルール", "")).strip():
            continue
        by_tab.setdefault(str(r["シート"]).strip(), []).append(r)

    for tab, tab_rules in by_tab.items():
        try:
            values = read_tab(gc, sheet_url, tab)
        except Exception as e:
            notes.append(f"シート「{tab}」を読めませんでした：{str(e)[:120]}")
            continue
        if not values:
            notes.append(f"シート「{tab}」は空でした。")
            continue
        headers = [str(h).strip() for h in values[0]]
        rows = values[1:]
        for r in tab_rules:
            col = str(r.get("列", "")).strip()
            rule = str(r.get("ルール", "")).strip()
            param = str(r.get("値", "")).strip()
            memo = str(r.get("メモ", "")).strip()
            if col not in headers:
                notes.append(f"シート「{tab}」に列「{col}」がありません（ルール：{rule}）。")
                continue
            idx = headers.index(col)
            seen = {}
            col_values = [(row[idx] if idx < len(row) else "") for row in rows]
            for i, cell in enumerate(col_values):
                # 行がまるごと空なら、見なくてよい（表の下の余白）
                if not any(str(c).strip() for c in rows[i]):
                    continue
                why = _judge(rule, cell, param, seen, col_values, i + 2)
                if why:
                    findings.append({
                        "シート": tab, "行": i + 2, "列": col, "いまの値": str(cell),
                        "なぜ直すか": (memo + "／" if memo else "") + why,
                    })
    return findings, notes


# ==========================================
# 🤖 録画したロボットを動かす
# ==========================================
def _run_robot_cli(args, log_path: str, timeout_sec: int):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    base = os.path.dirname(os.path.abspath(__file__))
    timed_out = False
    code = 1
    with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
        try:
            p = subprocess.run([sys.executable, os.path.join(base, "robot.py")] + args,
                               stdout=lf, stderr=subprocess.STDOUT, timeout=timeout_sec,
                               cwd=base, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            code = p.returncode
        except subprocess.TimeoutExpired:
            # 待っても終わらなかった。どこまで進んだかはログに残っているので、それを見せる。
            timed_out = True
    try:
        with open(log_path, encoding="utf-8", errors="replace") as lf:
            log = lf.read()[-3000:]
    except Exception:
        log = ""
    if timed_out:
        log += (f"\n\n❌ 全体で {timeout_sec // 60} 分たっても終わらなかったので中止しました。"
                "\n（どのシートまで進んだかは、上のログで確かめてください）")
    return (not timed_out) and code == 0, log


def sheet_tab_url(sheet_url: str, gid) -> str:
    """そのタブを開くためのURL（#gid=... を付ける）。

    SFコネクタの更新は「タブを開いて更新を押す」だけで、押す場所はどのタブでも同じ。
    だから録画は1回でよく、**開く先だけタブごとに差し替えて**繰り返せばよい。
    """
    base = str(sheet_url or "").split("#")[0].split("?")[0].rstrip("/")
    if base.endswith("/edit"):
        base = base[:-len("/edit")]
    return f"{base}/edit#gid={gid}"


def work_dir(root_name: str, name: str) -> str:
    """「取り込みファイル/<用途>/<名前>」のフォルダ（無ければ作る）。

    SMS送信のほか、データローダー自動化からも同じ作りで使う。
    """
    path = os.path.join(INTAKE_ROOT, _safe(root_name), _safe(name))
    os.makedirs(path, exist_ok=True)
    return path


# SFコネクタの更新手順で「どのシートを選ぶか」を表す差し込み名。
# 手順書の『対象』か『値』に {更新するシート} と書いておくと、実行時にシート名が入る。
REFRESH_VAR = "更新するシート"


def run_sheet_refresh(robot_name: str, folder: str, tabs=None, url: str = None,
                      tab_urls=None, timeout_sec: int = 0):
    """SFコネクタの更新を、**ブラウザを1回開いたまま**シートのぶんだけ繰り返す。

    SFコネクタは「いま開いているシート」を更新する作りなので、
    1周ごとに **そのシートを開いてから** 同じ手順をなぞる（tab_urls）。
    毎回ブラウザを開き直すと、そのたびにログインと画面移動が要って遅く、失敗も増える。
    ⏳ レポートによっては更新に何分もかかるので、待ち時間はたっぷり取る。
    戻り値：(成功したか, ログの最後のほう)
    """
    os.makedirs(folder, exist_ok=True)
    args = ["--run", robot_name, folder]
    if url:
        args += ["--url", url]
    tabs = [t for t in (tabs or []) if str(t).strip()]
    if tabs:
        args += ["--each", f"{REFRESH_VAR}=" + ",".join(tabs)]
        if tab_urls:
            args += ["--each-url", " ".join(str(u) for u in tab_urls)]
    # ⏱ 全体の持ち時間は、シートの枚数に合わせて自動で決める。
    #    1枚あたり最大1時間まで待てるので、枚数ぶん足しておかないと
    #    「ロボットはまだ待っているのに、こちらが先に打ち切る」ことになる。
    if not timeout_sec:
        timeout_sec = 1800 + 3900 * max(1, len(tabs))
    return _run_robot_cli(args, os.path.join(folder, "refresh.log"), timeout_sec)


def tab_urls_for(sheet_url: str, tabs, gids: dict):
    """更新するシートの並びに合わせて、そのシートを開くURLを作る。"""
    out = []
    for t in tabs or []:
        gid = (gids or {}).get(t)
        out.append(sheet_tab_url(sheet_url, gid) if gid is not None else sheet_url)
    return out


def parse_refresh_log(log: str, tabs):
    """実行ログから「どのシートまで進んだか」を読み取って、結果の表にする。

    ロボットは1周ごとに `🔁 i/n：更新するシート = <名前>` を出す。
    途中で止まったときに、どこで止まったのかを担当者が一目で分かるようにする。
    """
    done = re.findall(r"🔁\s*(\d+)/\d+：" + re.escape(REFRESH_VAR) + r"\s*=\s*(.+)", str(log or ""))
    reached = [m[1].strip() for m in done]
    failed = "❌" in str(log or "")
    out = []
    for t in tabs:
        if t not in reached:
            out.append({"シート": t, "結果": "⏭ 未実行"})
        elif failed and reached and t == reached[-1]:
            out.append({"シート": t, "結果": "❌ 失敗"})
        else:
            out.append({"シート": t, "結果": "✅ OK"})
    return out


def run_refresh_robot(robot_name: str, pattern: str, tabs=None, url: str = None,
                      tab_urls=None, timeout_sec: int = 0):
    """SMS送信のパターン用に、シート更新ロボットを動かす。"""
    return run_sheet_refresh(robot_name, pattern_dir(pattern), tabs=tabs, url=url,
                             tab_urls=tab_urls, timeout_sec=timeout_sec)


def run_export_robot(robot_name: str, pattern: str, url: str = None, timeout_sec: int = 600):
    """スプシのGAS（CSV書き出しサイドバー）のボタンを、ロボットに押させる。

    GASの「⬇ PC保存＋Drive保存」は PC へのダウンロードも走るので、
    落ちてきたファイルはそのまま `adopt_downloaded` で今日の分として採用できる。
    """
    folder = pattern_dir(pattern)
    # 前回の記録は消しておく（失敗したのに前回のファイルを掴まないため）
    try:
        os.remove(os.path.join(folder, "_last_download.json"))
    except Exception:
        pass
    args = ["--run", robot_name, folder]
    if url:
        args += ["--url", url]
    return _run_robot_cli(args, os.path.join(folder, "export.log"), timeout_sec)


def run_send_robot(robot_name: str, pattern: str, csv_path: str, timeout_sec: int = 900,
                   submit: bool = True):
    """プッシュプロに CSV を入れて一括送信するロボットを動かす（このPCで実行）。

    submit=True … `--submit` を付けて送信ステップまで実行する（本番）
    submit=False … 送信ステップは飛ばす＝**取り込みまでを確かめるお試し**。
        録画の手直しを、実際に送らずに試せるようにするため。
    戻り値：(成功したか, ログの最後のほう)
    """
    folder = pattern_dir(pattern)
    # お試しのときは見張りを付ける（印の無い送信手順があれば動かさない）
    args = ["--run", robot_name, folder] \
        + (["--submit"] if submit else ["--guard-submit"]) \
        + ["--file", csv_path]
    return _run_robot_cli(args, os.path.join(folder, "send.log"), timeout_sec)


def send_test_dir() -> str:
    """送信ロボットのお試し用フォルダ（本番のパターンと混ぜない）。"""
    return pattern_dir("＿お試し")


def sample_csvs() -> list:
    """お試しに使える、これまでのパターンのCSV（新しい順）。"""
    out = []
    for path in glob.glob(os.path.join(SMS_ROOT, "*", CSV_NAME)):
        try:
            out.append((os.path.basename(os.path.dirname(path)), path, os.path.getmtime(path)))
        except Exception:
            pass
    out.sort(key=lambda x: x[2], reverse=True)
    return [(n, p) for n, p, _m in out]


# ==========================================
# 📥 GAS が Drive に書き出した CSV を取ってくる
# ==========================================
# 各スプレッドシートの GAS（サイドバーの「⬇ PC保存＋Drive保存」）は、
#   <SMS送信用フォルダ>/yyyy/M月/d/<ラベル>_yyyyMMdd_HHmm.csv
# という決まりで Shift_JIS の CSV を置く。整形（電話番号の頭の0など）も
# GAS が済ませているので、アプリ側で作り直さず**その成果物をそのまま使う**。
# 同じ整形ロジックを2か所に持つと、片方だけ直して食い違う事故が起きるため。
# ⚠️ 実在のフォルダIDは、コードに書かない（このリポジトリは公開されているため）。
#    `.streamlit/secrets.toml` に DRIVE_SMS_ROOT として持たせる。
#    未設定でも、画面でフォルダIDを入れれば使える。
def _drive_sms_root() -> str:
    v = os.environ.get("DRIVE_SMS_ROOT", "").strip()
    if v:
        return v
    try:
        import tomllib
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, ".streamlit", "secrets.toml"), "rb") as f:
            return str(tomllib.load(f).get("DRIVE_SMS_ROOT", "") or "").strip()
    except Exception:
        return ""


DRIVE_SMS_ROOT = _drive_sms_root()   # GAS の ROOT_FOLDER_IDS["CSV"]

# 画面のプルダウン用：パターン名の候補 → GAS が付けるファイル名の頭
KNOWN_LABELS = [
    "長期不在1回目_CSV", "長期不在2回目_CSV",
    "引越前未手配_CSV", "SMS送信希望_CSV", "FPR送信_CSV",
]


def _drive_child_folder(drive, parent_id: str, name: str):
    from intake_runner import _ALL_DRIVES
    q = (f"'{parent_id}' in parents and trashed=false "
         "and mimeType='application/vnd.google-apps.folder'")
    for f in drive.files().list(q=q, fields="files(id,name)", pageSize=200,
                                **_ALL_DRIVES).execute().get("files", []):
        if str(f["name"]).strip() == str(name).strip():
            return f["id"]
    return None


def today_drive_path() -> list:
    """GAS が作る「今日のフォルダ」の名前（yyyy / M月 / d）。ゼロ埋めしないのがGASの流儀。"""
    now = time.localtime()
    return [time.strftime("%Y", now), f"{now.tm_mon}月", str(now.tm_mday)]


def fetch_from_drive(sa_json: str, root_folder_id: str, label_prefix: str, pattern: str):
    """GAS が今日書き出した CSV を Drive から取ってきて、パターンのフォルダに置く。

    置くときの名前は毎回同じ（`送信データ.csv`）。録画で選んだファイルを使い続けられる。
    戻り値：(手元のCSVのパス, Drive上のファイル名, 控えのパス)
    """
    import intake_runner
    drive = intake_runner.drive_client(sa_json)

    folder_id = root_folder_id or DRIVE_SMS_ROOT
    walked = []
    for part in today_drive_path():
        nxt = _drive_child_folder(drive, folder_id, part)
        if not nxt:
            raise FileNotFoundError(
                f"Drive に今日のフォルダ（{'/'.join(walked + [part])}）がありません。"
                "スプレッドシートで「CSV書き出し」を押して、Drive保存まで済ませてください。")
        walked.append(part)
        folder_id = nxt

    from intake_runner import _ALL_DRIVES
    q = f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    files = drive.files().list(q=q, fields="files(id,name,modifiedTime)",
                               orderBy="modifiedTime desc", pageSize=200,
                               **_ALL_DRIVES).execute().get("files", [])
    label = str(label_prefix or "").strip()
    if label:
        files = [f for f in files if str(f["name"]).startswith(label)]
    if not files:
        raise FileNotFoundError(
            f"今日のフォルダ（{'/'.join(walked)}）に「{label or 'CSV'}」で始まるファイルがありません。"
            "スプレッドシートで「CSV書き出し」を押しましたか？")

    newest = files[0]          # modifiedTime の新しい順。同日に押し直しても最後の分になる
    data = intake_runner.download_bytes(drive, newest["id"])
    path, hist = _put_csv(pattern, data, "Drive")
    return path, str(newest["name"]), hist


def adopt_downloaded(pattern: str):
    """録画ロボットがブラウザで落としてきた CSV を、その日の送信ファイルとして採用する。

    GAS のサイドバーは「PC保存」も同時に行うので、ロボットにボタンを押させると
    そのままダウンロードされる。それを毎回同じ名前に置き直して使う。
    戻り値：CSVのパス（無ければ None）
    """
    folder = pattern_dir(pattern)
    try:
        with open(os.path.join(folder, "_last_download.json"), encoding="utf-8") as f:
            files = json.load(f).get("ファイル") or []
    except Exception:
        files = []
    got = None
    for p in reversed(files):
        if os.path.isfile(p) and p.lower().endswith(".csv"):
            got = p
            break
    if not got:
        return None
    with open(got, "rb") as f:
        data = f.read()
    path, _hist = _put_csv(pattern, data, "ロボット")
    # 元のダウンロードは残さない（どれを送るのか迷わないように）
    try:
        if os.path.abspath(got) != os.path.abspath(path):
            os.remove(got)
    except Exception:
        pass
    return path


# ==========================================
# 📮 送信の記録（二重送信を防ぐ・送れた分と送れなかった分を残す）
# ==========================================
SENT_NAME = "送信履歴.json"
SENT_LIMIT = 50000


def _sent_path(pattern: str) -> str:
    return os.path.join(pattern_dir(pattern), SENT_NAME)


def load_sent(pattern: str) -> dict:
    """{正規化した宛先: {"日時":..., "結果":..., "件名":...}}"""
    try:
        with open(_sent_path(pattern), encoding="utf-8") as f:
            return json.load(f).get("送信済み", {}) or {}
    except Exception:
        return {}


def _save_sent(pattern: str, sent: dict):
    # 増えすぎたら古い順に切り捨てる（追記の順番は保たれる）
    if len(sent) > SENT_LIMIT:
        sent = dict(list(sent.items())[-SENT_LIMIT:])
    try:
        with open(_sent_path(pattern), "w", encoding="utf-8") as f:
            json.dump({"更新": time.strftime("%Y/%m/%d %H:%M:%S"), "送信済み": sent},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _dest_key(v) -> str:
    """宛先の見た目のゆれを揃える。`090-1234-5678` と `09012345678` は同じ人。"""
    v = unicodedata.normalize("NFKC", str(v or "")).strip()
    bare = re.sub(r"[-ー－\s]", "", v)
    return bare if bare.isdigit() else v


def csv_dest_keys(path: str, encoding_label: str = "Shift_JIS"):
    """CSVの**1列目**を宛先とみなして、正規化した一覧を返す。

    GAS が作るCSVは、どのパターンも1列目が携帯番号（頭の0付けもGASが済ませている）。
    行の並びは (行番号, 宛先) で返す。
    """
    enc = ENCODINGS.get(encoding_label, "cp932")
    try:
        with open(path, "rb") as f:
            text = f.read().decode(enc, errors="replace")
    except Exception:
        return []
    out = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if i == 0 or not row:
            continue
        k = _dest_key(row[0])
        if k:
            out.append((i + 1, k))
    return out


def still_blocked(rec, within_days: int = 0) -> bool:
    """この記録は、まだ「送ってはいけない」ものか。

    ⚠️ **日付で数える**（時刻ではない）。
       時間で数えると、昨日の夕方に送った相手へ今朝は送れない
       （15時間しか経っていないため）。担当者が言う「翌日なら送ってよい」と
       食い違うので、暦の日で数える。
       within_days=0 … 一度でも送ったら、もう送らない
       within_days=1 … 今日送った分だけ止める（＝翌日には送れる）
       within_days=3 … 今日を含む3日ぶんを止める
    """
    if not rec:
        return False
    if not within_days:
        return True
    stamp = str(rec.get("日時", "") or "")[:10]        # yyyy/mm/dd
    if not stamp:
        return True                                    # 日付が読めないときは、止める側に倒す
    try:
        sent_day = time.strptime(stamp, "%Y/%m/%d")
    except Exception:
        return True
    from datetime import date
    d = date(sent_day.tm_year, sent_day.tm_mon, sent_day.tm_mday)
    return (date.today() - d).days < int(within_days)


def find_already_sent(pattern: str, keys, within_days: int = 0):
    """すでに送った宛先を返す。within_days>0 ならその日数以内のものだけ見る。"""
    sent = load_sent(pattern)
    hits = []
    for row_no, k in keys:
        rec = sent.get(k)
        if not still_blocked(rec, within_days):
            continue
        hits.append({"行": row_no, "宛先": k, "前回": rec.get("日時", ""),
                     "前回の結果": rec.get("結果", "")})
    return hits


def record_sent(pattern: str, keys, result: str, note: str = ""):
    """送った宛先を記録する。

    プッシュプロは一括送信なので、1件ごとの成否はこちらでは分からない。
    **迷ったら「送った」に寄せる**（二重送信のほうが取り返しがつかないため）。
    result は「送信済み」／「要確認（送ったかもしれない）」／「送信できず」。
    """
    sent = load_sent(pattern)
    now = time.strftime("%Y/%m/%d %H:%M:%S")
    for _row_no, k in keys:
        if result == "送信できず" and k not in sent:
            continue          # 送っていないものは記録しない（次回そのまま送れるように）
        sent[k] = {"日時": now, "結果": result, "メモ": note}
    _save_sent(pattern, sent)
    return len(keys)


def forget_sent(pattern: str, dests):
    """送信の記録から、指定した宛先を消す（＝また送れるようにする）。

    ⚠️ 消すと二重送信の歯止めが外れる。
       「プッシュプロ側で、実際には送られていない」と人が確かめたときだけ使う。
    戻り値：消した件数
    """
    sent = load_sent(pattern)
    n = 0
    for d in dests or []:
        k = _dest_key(d)
        if k in sent:
            del sent[k]
            n += 1
    if n:
        _save_sent(pattern, sent)
    return n


# ロボットが送信ステップに入るとき、必ずこの1行を出す。
# ⚠️ これ以外の手がかりで判定しないこと。
#    以前は「🚀 と『送信』の両方がログにあれば」で見ていたため、
#    起動時の「🚀【共通_プッシュプロ一括送信】のロボットを起動します」に当たってしまい、
#    **何をしても必ず『要確認（送ったかもしれない）』**になっていた。
SUBMIT_MARK = "最後の『送信（申請）』ステップを実行します"


def submit_reached(log: str) -> bool:
    """ログを見て、最後の『送信』ステップに入ったかを判定する。

    ここまで進んでいたら、途中で落ちていても**送られた可能性がある**。
    そのときは「要確認」として記録し、次に同じ宛先を自動では送らない。
    """
    return SUBMIT_MARK in str(log or "")


def stop_reason(log: str) -> str:
    """なぜ止まったのかを、ひとことで返す（分からなければ空）。

    「エラー件数で止まったのか、二重送信で止まったのか分からない」という声があったため、
    結果の欄にそのまま出せる言葉にして返す。
    """
    t = str(log or "")
    for line in reversed(t.splitlines()):
        line = line.strip()
        if line.startswith("🛑") and "件 あります" in line:
            return line.lstrip("🛑 ").strip()          # 件数の確認で止めた
        if line.startswith("🛑") and "お試し実行を中止" in line:
            return "『送信（本番のみ）』の印が無い送信手順があるため、動かしませんでした"
    if "ログイン" in t and ("切れ" in t or "サインイン" in t):
        return "ログインが切れていました"
    for line in reversed(t.splitlines()):
        line = line.strip()
        if line.startswith("❌"):
            return line.lstrip("❌ ").strip()[:160]
    return ""


def drop_already_sent(pattern: str, encoding_label: str = "Shift_JIS", within_days: int = 0,
                      sent_pattern: str = ""):
    """すでに送った宛先の行を、その日のCSVから取り除く（二重送信の防止）。

    pattern … CSVの置き場所（シートごとに分かれる）
    sent_pattern … 送信の記録を見る先。省略時は pattern と同じ。
        複数シートのときは**パターン名**を渡す（記録はまとめて持つため）。
    戻り値：(取り除いた件数, 残った件数)
    """
    path = csv_path(pattern)
    if not os.path.isfile(path):
        return 0, 0
    enc = ENCODINGS.get(encoding_label, "cp932")
    with open(path, "rb") as f:
        text = f.read().decode(enc, errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return 0, 0
    sent = load_sent(sent_pattern or pattern)

    def _is_sent(v) -> bool:
        # 判定は still_blocked に一本化してある（2か所に書くと必ず食い違う）
        return still_blocked(sent.get(_dest_key(v)), within_days)

    head, body = rows[0], rows[1:]
    keep = [r for r in body if r and not _is_sent(r[0])]
    dropped = len(body) - len(keep)
    if dropped:
        _put_csv(pattern, _csv_bytes([head] + keep, enc), "重複除外")
    return dropped, len(keep)


def csv_row_count(path: str, encoding_label: str = "Shift_JIS") -> int:
    """CSVのデータ行数（見出しを除く）。"""
    enc = ENCODINGS.get(encoding_label, "cp932")
    try:
        with open(path, "rb") as f:
            text = f.read().decode(enc, errors="replace")
    except Exception:
        return 0
    return max(0, sum(1 for r in csv.reader(io.StringIO(text)) if any(str(x).strip() for x in r)) - 1)


def csv_preview(path: str, encoding_label: str = "Shift_JIS", lines: int = 6) -> str:
    enc = ENCODINGS.get(encoding_label, "cp932")
    try:
        with open(path, "rb") as f:
            text = f.read(8000).decode(enc, errors="replace")
    except Exception:
        return ""
    return "\n".join(text.splitlines()[:lines])


# ==========================================
# 🔗 GAS（ウェブアプリ）からCSVをもらう
# ==========================================
# 各スプシの GAS を「ウェブアプリ」としてデプロイしておけば、
# サイドバーのボタンを押させる録画は要らない。URLを叩けば同じCSVが返ってくる。
# 入れ方は gas/SMS_CSV書き出しWebAPI.gs のコメントに書いてある。
#
# ⚠️ デプロイの「アクセスできるユーザー：全員」は、URLを知っていれば誰でも叩けるという意味。
#    必ず合言葉（token）を設定し、合わない呼び出しは GAS 側で断る。
def gas_inspect(gas_url: str, token: str):
    """そのスプシに何があるかをGASに聞く（シート名・関数名）。

    スプシごとに関数名もシート名も違うので、**人がコードを読んで書き分けずに済むよう**、
    アプリが聞いて選択肢として出す。戻り値：(OKか, 中身 or エラー文)
    """
    import intake_runner
    return intake_runner.call_gas(gas_url_fixed(gas_url), token, "inspect", timeout=90)


def check_gas_csv(gas_url: str, token: str):
    """GASにつながるか・合言葉が合っているかを確かめる。戻り値：(OKか, メッセージ)"""
    ok, data = gas_inspect(gas_url, token)
    if ok:
        return True, f"つながりました（{(data or {}).get('name', '')}）。"
    msg = str(data)
    if "合言葉" in msg:
        return False, "つながりましたが、合言葉が合っていません。GAS側の API_TOKEN と見比べてください。"
    return False, msg


def fetch_from_gas(gas_url: str, token: str, sheet_name: str, pattern: str,
                   keep_drive: bool = True, build: str = ""):
    """GASにCSVを作らせて、その場で受け取る。

    Drive を経由しないので、フォルダIDの設定も共有の権限も要らない。
    そして「いま作られたもの」がそのまま返るので、
    “その日のフォルダの中でいちばん新しいファイル”を当てにいく必要もない。
    戻り値：(手元のCSVのパス, GASが付けたファイル名, 件数, GASの返事そのまま)
    """
    import base64
    import intake_runner
    ok, data = intake_runner.call_gas(gas_url_fixed(gas_url), token, "csv",
                                      extra={"sheet": sheet_name,
                                             "build": str(build or ""),
                                             "drive": "1" if keep_drive else "0"},
                                      timeout=600)
    if not ok:
        raise RuntimeError(str(data))
    name = str((data or {}).get("filename", "") or "").strip()
    content = (data or {}).get("content")
    if not (name and content):
        raise RuntimeError("CSVを受け取れませんでした（GASの返事に中身がありません）")
    raw = base64.b64decode(content)
    path, _hist = _put_csv(pattern, raw, "GAS")
    return path, name, int((data or {}).get("rows", 0) or 0), (data or {})


def gas_url_fixed(gas_url: str) -> str:
    """GASのURLを、アプリから呼べる形に直す。

    Apps Script の画面には `.../a/macros/会社のドメイン/s/AKfy.../exec` の形で出ることがある。
    この形は開くたびにGoogleのログインを求めるので、**公開範囲を「全員」にしても弾かれる**。
    `/macros/s/AKfy.../exec` に直せば、そのまま呼べる。
    """
    import intake_runner
    return intake_runner.normalize_gas_url(gas_url)


def run_gas_action(gas_url: str, token: str, action: str = "", timeout: int = 900,
                   build: str = ""):
    """GAS（ウェブアプリ）を呼ぶ。データローダーのシート作り直しなどに使う。

    build には、走らせる処理の名前（カンマ区切り）。スプシごとに違うので、
    GASに書かせず**アプリが持って渡す**（コードを読んで書き分けずに済むように）。
    戻り値：(うまくいったか, 返ってきた中身 or エラーの文)
    """
    import intake_runner
    return intake_runner.call_gas(gas_url_fixed(gas_url), token, action,
                                  extra={"build": str(build or "")}, timeout=timeout)


# ==========================================
# 📜 GASに貼り付けるコードを、アプリから配る
# ==========================================
# チャットやファイル便で渡すと、どれが最新か分からなくなるし、
# 合言葉を人が書き替える手間も残る。アプリが**合言葉を埋めた状態**で出す。
GAS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gas")
_TOKEN_PLACEHOLDER = "'ここに長い合言葉を書く'"


def gas_template(filename: str, token: str = "") -> str:
    """配る用のGASコードを読み、合言葉を埋めて返す。読めなければ空文字。"""
    try:
        with open(os.path.join(GAS_DIR, filename), encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return ""
    if token:
        # ⚠️ 置き換えるのは **合言葉を書く1行だけ**。
        #    以前は全部を置き換えていたため、判定に使っている行まで書き換わり、
        #    正しい合言葉を入れても必ず「未設定です」と返る状態になっていた。
        tok = "'" + str(token).strip() + "'"
        text = re.sub(r"(?m)^(const API_TOKEN\s*=\s*)" + re.escape(_TOKEN_PLACEHOLDER) + r"(\s*;)",
                      lambda m: m.group(1) + tok + m.group(2), text, count=1)
        # 配る前に自分で確かめる（黙って壊れたコードを渡さない）
        if ("const API_TOKEN = " + tok) not in text:
            raise RuntimeError("GASコードに合言葉を入れられませんでした。開発者に連絡してください。")
    return text
