"""📊 いまの状況を集める（画面に依存しない）。

ホーム（`app.py`）と「全状況進捗確認」（`pages/1_...`）が、同じ数字を見るための道具。
⚠️ **仮の数字は置かない。** 集められないものは「—」にして、その理由を画面に書く。
   以前のホームは「本日の処理件数 0 件」と**常に0を出していた**ため、
   動いているのか止まっているのか分からなかった。

どこから集めるか：
- Supabase の `merchants` … ロボット一覧と、各業務の設定（`__sms__` などの予約行）
- `取り込みファイル/` の中のログ・記録 … **このPCで動かした結果**
- `artifacts/` のスクショ … うまくいかなかったときの証跡

⚠️ ここでは Google スプレッドシートを読まない（1枚読むだけで数秒かかり、
   画面を開くたびに固まるため）。中身の確認は各ページで行う。
"""
import glob
import json
import os
import re
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INTAKE_ROOT = os.path.join(BASE_DIR, "取り込みファイル")
ARTIFACT_DIR = os.path.join(BASE_DIR, "artifacts")

# 取り込みファイルの中で、「進捗反映のキャリア」ではないフォルダ
NON_CARRIER_DIRS = ("SMS送信用", "データローダー", "レポート更新")

# 予約行（ロボット一覧には出さない設定の置き場所）
SETTINGS_IDS = {
    "sms": "__sms__",
    "dataloader": "__dataloader__",
    "reports": "__reports__",
    "entry_loads": "__entry_loads__",
    "progress": "__progress__",
}


# ==========================================
# ⏱ 時刻の道具
# ==========================================
def fmt(ts) -> str:
    """時刻を「9/1 08:30」の形に。分からなければ「—」。"""
    if not ts:
        return "—"
    return time.strftime("%m/%d %H:%M", time.localtime(ts))


def is_today(ts) -> bool:
    if not ts:
        return False
    return time.strftime("%Y%m%d", time.localtime(ts)) == time.strftime("%Y%m%d")


def ago(ts) -> str:
    """「3時間前」のような、ざっくりした言い方。"""
    if not ts:
        return ""
    d = max(0, time.time() - ts)
    if d < 90:
        return "たった今"
    if d < 3600:
        return f"{int(d // 60)}分前"
    if d < 86400:
        return f"{int(d // 3600)}時間前"
    return f"{int(d // 86400)}日前"


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _parse_stamp(text: str) -> float:
    """「2026/08/26 06:07:31」のような文字列を時刻に。読めなければ0。"""
    for f in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return time.mktime(time.strptime(str(text or "").strip(), f))
        except Exception:
            continue
    return 0.0


# ==========================================
# 🗃 Supabase（1回だけ問い合わせて、みんなで使い回す）
# ==========================================
def all_rows(supabase) -> list:
    try:
        return supabase.table("merchants").select("*").execute().data or []
    except Exception:
        return []


def settings_of(rows, key: str) -> dict:
    """予約行の設定（`__sms__` など）を取り出す。"""
    sid = SETTINGS_IDS.get(key, key)
    for r in rows:
        if str(r.get("id", "")) == sid:
            return r.get("config_json", {}) or {}
    return {}


def robots(rows) -> list:
    """ロボット一覧（設定の予約行は除く）。"""
    out = []
    for r in rows:
        rid = str(r.get("id", ""))
        if rid.startswith("__"):
            continue
        cfg = r.get("config_json", {}) or {}
        steps = (cfg.get("robot_config", {}) or {}).get("steps", []) or []
        out.append({
            "名前": r.get("name") or rid,
            "種別": cfg.get("product_type", "") or "その他",
            "稼働中": bool(r.get("is_active")),
            "手順数": len(steps),
            "送信ステップ": _has_submit(steps),
        })
    out.sort(key=lambda x: (not x["稼働中"], x["種別"], x["名前"]))
    return out


def _has_submit(steps):
    """『送信（本番のみ）』の印が付いた手順があるか（分からなければ None）。

    印が無いロボットは、本番でも申請・送信が完了しない（司令室でも警告している）。
    判定は `robot.py` に一本化する（同じ言葉の一覧を2か所に書かない）。
    ⚠️ `robot.py` は Playwright を読み込むので、入っていないPCでは import できない。
       そのときは **False と言い切らず None**（画面には「—」と出す）。
       「送信ステップが無い」と嘘をつくと、直さなくてよいものを直しに行かせてしまう。
    """
    try:
        import robot
    except Exception:
        return None
    return any(robot.is_submit_marker(s.get("いつ", s.get("condition", ""))) for s in steps)


# ==========================================
# 📱 SMS送信の状況
# ==========================================
def sms_status(rows) -> list:
    """パターンごとに、CSVの用意と送信の記録をまとめる。"""
    import sms_runner
    cfg = settings_of(rows, "sms")
    out = []
    for pat in cfg.get("patterns", []) or []:
        name = str(pat.get("name", "") or "")
        if not name:
            continue
        enc = pat.get("csv_encoding", "Shift_JIS")
        sheets = [str(x).strip() for x in (pat.get("gas_sheets") or []) if str(x).strip()]
        if not sheets and str(pat.get("gas_sheet", "") or "").strip():
            sheets = [str(pat["gas_sheet"]).strip()]

        csvs = []
        for sh in (sheets or [""]):
            slot = sms_runner.sheet_slot(name, sh)
            path = sms_runner.csv_path(slot)
            ts = _mtime(path)
            csvs.append({
                "シート": sh or "（未設定）",
                "今日のぶん": bool(sms_runner.today_csv(slot)),
                "作った時刻": ts,
                "件数": (sms_runner.csv_row_count(path, enc) if ts else 0),
                "最後の送信ログ": _mtime(os.path.join(sms_runner.pattern_dir(slot), "send.log")),
            })

        sent = sms_runner.load_sent(name)
        today = time.strftime("%Y/%m/%d")
        last_ts, sent_today = 0.0, 0
        for rec in sent.values():
            t = _parse_stamp(rec.get("日時", ""))
            last_ts = max(last_ts, t)
            if str(rec.get("日時", "")).startswith(today):
                sent_today += 1

        folder = sms_runner.pattern_dir(name)
        out.append({
            "パターン": name,
            "CSV": csvs,
            "送信済み合計": len(sent),
            "今日送った件数": sent_today,
            "最後の送信": last_ts,
            "最後の更新ログ": _mtime(os.path.join(folder, "refresh.log")),
            "自動で送る": bool(pat.get("auto_send")),
            "自動で投入": bool(pat.get("auto_load")),
            "エラーがあっても送る": bool(pat.get("allow_errors")),
        })
    return out


# ==========================================
# 🗃 データローダー／🔄 レポート更新／🗃 エントリー後の投入
# ==========================================
def dataloader_status(rows) -> list:
    import sms_runner
    out = []
    for job in settings_of(rows, "dataloader").get("jobs", []) or []:
        name = str(job.get("name", "") or "")
        if not name:
            continue
        folder = sms_runner.work_dir("データローダー", name)
        out.append({
            "ジョブ": name,
            "更新するシート": len(job.get("refresh_tabs", []) or []),
            "目で見て確認": len(job.get("watch_tabs", []) or []),
            "投入": len(job.get("loads", []) or []),
            "最後の更新ログ": _mtime(os.path.join(folder, "refresh.log")),
            "自動で投入": bool(job.get("auto_push")),
        })
    return out


def report_status(rows) -> list:
    import sms_runner
    out = []
    for s in settings_of(rows, "reports").get("sets", []) or []:
        name = str(s.get("name", "") or "")
        if not name:
            continue
        sheets = s.get("sheets", []) or []
        folder = sms_runner.work_dir("レポート更新", name)
        out.append({
            "セット": name,
            "スプレッドシート": len(sheets),
            "シート合計": sum(len(x.get("tabs", []) or []) for x in sheets),
            "最後の更新ログ": _mtime(os.path.join(folder, "refresh.log")),
        })
    return out


def entry_load_status(rows) -> list:
    out = []
    for s in settings_of(rows, "entry_loads").get("sets", []) or []:
        name = str(s.get("name", "") or "")
        if not name:
            continue
        loads = s.get("loads", []) or []
        out.append({
            "セット": name,
            "投入": len(loads),
            "スプレッドシート": len({str(x.get("url", "")) for x in loads if x.get("url")}),
        })
    return out


# ==========================================
# 🚀 進捗反映（このPCに残っている取り込みの記録）
# ==========================================
def intake_status() -> list:
    """キャリアごとの、最後に取り込んだファイル。

    ⚠️ 設定スプレッドシートは読まない（遅いため）。ここに出るのは
       **このPCで取り込んだ記録**（`取り込みファイル/<キャリア>/_last_download.json`）。
    """
    out = []
    if not os.path.isdir(INTAKE_ROOT):
        return out
    for name in sorted(os.listdir(INTAKE_ROOT)):
        folder = os.path.join(INTAKE_ROOT, name)
        if not os.path.isdir(folder) or name in NON_CARRIER_DIRS:
            continue
        rec, ts, files = {}, 0.0, []
        try:
            with open(os.path.join(folder, "_last_download.json"), encoding="utf-8") as f:
                rec = json.load(f) or {}
            ts = _parse_stamp(rec.get("時刻", ""))
            files = [os.path.basename(p) for p in (rec.get("ファイル") or [])]
        except Exception:
            pass
        log = os.path.join(folder, "intake.log")
        out.append({
            "キャリア": name,
            "最後の取り込み": ts,
            "ファイル": files[0] if files else "",
            "ログ": log if os.path.isfile(log) else "",
            "ログの時刻": _mtime(log),
        })
    out.sort(key=lambda x: -(x["最後の取り込み"] or x["ログの時刻"]))
    return out


# ==========================================
# 🧾 動いた記録（ログ）と、うまくいかなかった証跡（スクショ）
# ==========================================
LOG_KINDS = {"refresh.log": "更新", "send.log": "送信", "export.log": "書き出し",
             "intake.log": "取り込み"}


def recent_logs(days: int = 7, limit: int = 40) -> list:
    """最近動かしたロボットのログを、新しい順に。"""
    since = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(INTAKE_ROOT, "**", "*.log"), recursive=True):
        ts = _mtime(path)
        if ts < since:
            continue
        rel = os.path.relpath(path, INTAKE_ROOT).replace("\\", "/")
        parts = rel.split("/")
        out.append({
            "時刻": ts,
            "工程": LOG_KINDS.get(parts[-1], parts[-1]),
            "どこ": " / ".join(parts[:-1]) or "—",
            "ファイル": path,
        })
    out.sort(key=lambda x: -x["時刻"])
    return out[:limit]


def log_result(path: str) -> str:
    """ログの終わりを見て、うまくいったかどうかを短い言葉にする。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()[-20000:]
    except Exception:
        return ""
    try:
        import sms_runner
        why = sms_runner.stop_reason(text)
    except Exception:
        why = ""
    if "🚨" in text or "🛑" in text or "❌" in text:
        return f"⚠️ 止まりました：{why}" if why else "⚠️ 途中で止まりました"
    if "✅" in text or "🎉" in text:
        return "✅ 最後まで動きました"
    return ""


# 例：「ドコモGMO進捗_download_failed_20260821_104950.png」
# ⚠️ ロボット名は貪欲に取らない（`count_over` のように、できごとの名前も `_` を含むため）。
_SHOT_RE = re.compile(r"^(?P<robot>.+?)_(?P<kind>[a-z_]+)_(?P<stamp>\d{8}_\d{6})\.png$")

SHOT_WORDS = {
    "notfound": "欄やボタンが見つからなかった",
    "stopped": "途中で止めた",
    "captcha": "ロボット判定の壁に当たった",
    "signed_out": "ログインが切れていた",
    "download_failed": "ファイルを受け取れなかった",
    "auth_code_timeout": "認証コードのメールが来なかった",
    "wait_human_timeout": "人の操作を待ちきれなかった",
    "wrong_sheet": "違うシートを開いていた",
    "count_over": "エラー件数が0でないので送らなかった",
    "count_not_found": "エラー件数を読み取れなかった",
    "no_success_confirm": "完了の合図を確かめられなかった",
    "confirm_no_success": "完了の合図を確かめられなかった",
    "confirm_stopped": "確認の途中で止めた",
    "after_submit_blocked": "送信のあとで壁に当たった",
    "unresolved_placeholder": "{項目名}の中身が分からなかった",
    "empty_value": "入れる値が空だった",
    "upload_failed": "ファイルを渡せなかった",
    "no_file_link": "ダウンロードのリンクが無かった",
    "newest_click_failed": "最新のファイルを押せなかった",
    "mail_link_failed": "メールのリンクを開けなかった",
    "mail_link_timeout": "ログインのメールが来なかった",
    "date_failed": "日付を入れられなかった",
    "wait_timeout": "待ち時間を過ぎた",
    "exception": "思わぬエラーが出た",
    "error": "エラーが出た",
}


def recent_shots(days: int = 7, limit: int = 24) -> list:
    """うまくいかなかったときの画面（証跡）を、新しい順に。"""
    since = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(ARTIFACT_DIR, "*.png")):
        ts = _mtime(path)
        if ts < since:
            continue
        m = _SHOT_RE.match(os.path.basename(path))
        out.append({
            "時刻": ts,
            "ロボット": m.group("robot") if m else os.path.basename(path),
            "できごと": SHOT_WORDS.get(m.group("kind"), m.group("kind")) if m else "",
            "ファイル": path,
        })
    out.sort(key=lambda x: -x["時刻"])
    return out[:limit]


# ==========================================
# 📌 いちばん上に出す「今日の3つ」
# ==========================================
def headline(rows) -> dict:
    """ホームと確認ページの、両方で使う要約。"""
    # ⚠️ 「今日の数」だけを見ると、昨日まで動いていたのに
    #    「記録はまだありません」と出てしまう。少し広めに読んでから、今日のぶんを数える。
    logs = recent_logs(days=60, limit=500)
    shots = recent_shots(days=60, limit=500)
    sms = sms_status(rows)
    rbs = robots(rows)
    return {
        "稼働中のロボット": sum(1 for r in rbs if r["稼働中"]),
        "ロボット合計": len(rbs),
        "今日動いた工程": sum(1 for l in logs if is_today(l["時刻"])),
        "今日送ったSMS": sum(p["今日送った件数"] for p in sms),
        "今日の証跡": sum(1 for s in shots if is_today(s["時刻"])),
        "最後に動いた": logs[0]["時刻"] if logs else 0.0,
    }
