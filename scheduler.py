"""
⏰ 時間指定の自動実行（見回り役）

Windows のタスクスケジューラが **5分おきに** `python scheduler.py --tick` を呼ぶ。
呼ばれるたびに Supabase の予定（`__schedule__`）を読み、時刻が来たものを順に動かして、
結果を記録（`__schedule_runs__`）し、Slack に知らせる。

⭐ **時刻の登録はアプリの画面で行う**（「⏰ 時間指定の自動実行」ページ）。タスクスケジューラには
   「5分おきに見回る」1本しか登録しない＝人がタスクスケジューラを触らなくてよい。
⭐ **動くのは「自動実行用」に決めたPCだけ**（`__schedule__.host`＝PC名）。ほかのPCに見回りが
   登録されていても何もしない（二重に送信・投入しないため）。
⚠️ 見回りは**ログオンしている間だけ**動く（タスクの既定）。ロボットはブラウザを画面に出して動かすので、
   自動実行用のPCは自動ログオン・スリープなしにしておく。
⚠️ 1回の実行が長いと次の見回りと重なる。**ロックファイル**で1つずつにする（OSが持つロックなので、
   途中で落ちても残らない）。

使い方：
  python scheduler.py --tick                 見回り（タスクスケジューラから）
  python scheduler.py --run <予定ID>          その予定をいますぐ動かす（試すとき）
  python scheduler.py --install              このPCに見回りを登録する（5分おき）
  python scheduler.py --uninstall            見回りの登録を外す
"""
import datetime as dt
import json
import re
import os
import socket
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)                      # タスクスケジューラは作業フォルダを決められないため
sys.path.insert(0, BASE_DIR)

import auto_jobs  # noqa: E402

SCHEDULE_ID = "__schedule__"            # 予定（画面で編集する）
RUNS_ID = "__schedule_runs__"           # 実行の記録（見回り役だけが書く）
REQUESTS_ID = "__schedule_req__"        # 「次の見回りで動かす」の依頼（画面が足し、見回り役が消す）
TASK_NAME = "EnkanAI_時間指定の自動実行"
TICK_MINUTES = 5
HISTORY_LIMIT = 300
DEFAULT_LATE_MIN = 60                   # これ以上遅れたら動かさない（PCが止まっていた日に、昼に朝の分を送らない）
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
LOG_DIR = os.path.join(BASE_DIR, "取り込みファイル", "自動実行")


def this_host() -> str:
    return socket.gethostname()


def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "見回りの記録.log"), "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y/%m/%d %H:%M:%S}  {msg}\n")


# ==========================================
# 🗄 Supabase の読み書き
# ==========================================
def _save_row(sb, row_id: str, name: str, cfg: dict):
    err = None
    for wait in (0, 3, 8):
        if wait:
            time.sleep(wait)
        try:
            sb.table("merchants").upsert({
                "id": row_id, "name": name, "is_active": False,
                "connector_type": "settings", "config_json": cfg}).execute()
            return True
        except Exception as e:
            err = e
    _log(f"⚠️ {row_id} を保存できませんでした: {str(err)[:200]}")
    return False


def load_schedule(sb) -> dict:
    return auto_jobs.load_row(sb, SCHEDULE_ID)


def save_schedule(sb, cfg: dict):
    return _save_row(sb, SCHEDULE_ID, "（時間指定の自動実行の予定）", cfg)


def load_runs(sb) -> dict:
    return auto_jobs.load_row(sb, RUNS_ID)


def save_runs(sb, runs: dict):
    return _save_row(sb, RUNS_ID, "（時間指定の自動実行の記録）", runs)


def add_request(sb, item_id: str, who: str = ""):
    """画面から「次の見回りで動かす」。⚠️ 読み直して足す（ほかの依頼を消さない）。"""
    req = auto_jobs.load_row(sb, REQUESTS_ID)
    lst = [r for r in (req.get("requests") or []) if r.get("id") != item_id]
    lst.append({"id": item_id, "at": f"{dt.datetime.now():%Y/%m/%d %H:%M}", "who": who})
    req["requests"] = lst
    return _save_row(sb, REQUESTS_ID, "（時間指定の自動実行の依頼）", req)


def _take_requests(sb) -> list:
    req = auto_jobs.load_row(sb, REQUESTS_ID)
    lst = req.get("requests") or []
    if lst:
        req["requests"] = []
        _save_row(sb, REQUESTS_ID, "（時間指定の自動実行の依頼）", req)
    return [r.get("id") for r in lst]


# ==========================================
# 🕒 いつ動かすか
# ==========================================
def parse_hm(s: str):
    try:
        h, m = str(s).strip().split(":")[:2]
        return int(h), int(m)
    except Exception:
        return None


def due_state(item: dict, now: dt.datetime, done_on: str) -> str:
    """その予定を、いま動かすか。"run"（動かす）／"late"（遅すぎて動かさない）／""（何もしない）"""
    if not item.get("enabled", True):
        return ""
    if not runs_on(item, now.date()):
        return ""
    hm = parse_hm(item.get("time", ""))
    if not hm:
        return ""
    at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    today = f"{now:%Y-%m-%d}"
    if done_on == today or now < at:
        return ""
    # ⚠️ 予定を作った（時刻を変えた）のが今日のその時刻より後なら、今日の分はもう過ぎている＝明日から
    since = str(item.get("since", "") or "")
    if since and since >= f"{at:%Y-%m-%d %H:%M}":
        return ""
    late = int(item.get("late_min", DEFAULT_LATE_MIN) or DEFAULT_LATE_MIN)
    if now - at > dt.timedelta(minutes=late):
        return "late"
    return "run"


MONTH_END = 99  # 「月末」（月によって30日・31日・28日が変わるため、数字とは別に持つ）
REPEAT_WEEKLY, REPEAT_MONTHLY, REPEAT_DATES = "weekly", "monthly", "dates"


def runs_on(item: dict, day: dt.date) -> bool:
    """その日が実行日か。repeat＝weekly（曜日）／monthly（毎月の日）／dates（決めた日付）"""
    mode = item.get("repeat") or REPEAT_WEEKLY
    if mode == REPEAT_DATES:
        return f"{day:%Y-%m-%d}" in [str(d) for d in (item.get("dates") or [])]
    if mode == REPEAT_MONTHLY:
        mdays = [int(d) for d in (item.get("month_days") or [])]
        last = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        return day.day in mdays or (MONTH_END in mdays and day == last)
    days = item.get("days")
    if days is None:
        days = list(range(7))
    return day.weekday() in [int(d) for d in days]


def next_date(item: dict, today: dt.date):
    """今日以降でいちばん近い実行日（無ければ None）。決めた日付がすべて過ぎた予定を見分けるため"""
    if (item.get("repeat") or REPEAT_WEEKLY) == REPEAT_DATES:
        rest = sorted(d for d in (str(x) for x in (item.get("dates") or [])) if d >= f"{today:%Y-%m-%d}")
        return dt.date.fromisoformat(rest[0]) if rest else None
    for i in range(62):
        d = today + dt.timedelta(days=i)
        if runs_on(item, d):
            return d
    return None


def month_day_label(d: int) -> str:
    return "月末" if int(d) == MONTH_END else f"{int(d)}日"


def describe_when(item: dict) -> str:
    mode = item.get("repeat") or REPEAT_WEEKLY
    if mode == REPEAT_DATES:
        ds = sorted(str(d) for d in (item.get("dates") or []))
        if not ds:
            return "（日付なし）"
        txt = "・".join(f"{int(d[5:7])}/{int(d[8:10])}" for d in ds[:5])
        return txt + (f" ほか{len(ds) - 5}日" if len(ds) > 5 else "")
    if mode == REPEAT_MONTHLY:
        return "毎月 " + "・".join(month_day_label(d) for d in sorted(int(x) for x in (item.get("month_days") or [])))
    return describe_days(item.get("days"))


def describe_days(days) -> str:
    if days is None or sorted(int(d) for d in days) == list(range(7)):
        return "毎日"
    ds = sorted(int(d) for d in days)
    if ds == [0, 1, 2, 3, 4]:
        return "平日"
    return "・".join(WEEKDAYS[d] for d in ds)


def item_label(item: dict) -> str:
    kind = auto_jobs.KIND_LABELS.get(item.get("kind", ""), item.get("kind", ""))
    target = str(item.get("target", "") or "")
    # 進捗反映・イレギュラー報告・エントリー前DCは対象が1つしかないので、名前を付け足さない
    return kind if item.get("kind") in ("progress", "irregular", "precheck") else f"{kind}「{target}」"


# ==========================================
# 🔔 Slack
# ==========================================
def slack(secrets: dict, text: str) -> bool:
    """送り先は slack_notify で探す（このPCの secrets.toml → 画面で保存した共有の送り先）。"""
    import slack_notify
    url, _src, why = slack_notify.webhook_url(secrets)
    if not url:
        if why:
            _log(f"⚠️ Slackに送れません: {why}")
        return False
    ok, err = slack_notify.post(url, text)
    if not ok:
        _log(f"⚠️ Slackに送れませんでした: {err}")
    return ok


def extra_targets(item: dict, state: str) -> list:
    """この結果を、ほかの送り先（グループ）のどれに送るか。

    予定の `extra_slack`＝{"to": [名前…], "done": 完了を送る, "fail": 完了以外（失敗・確認待ち・見送り）を送る}。
    ⚠️ いつもの送り先への通知とは別。こちらを決めても、いつもの送り先の動きは変えない。
    """
    ex = item.get("extra_slack") or {}
    want = ex.get("done") if state == "完了" else ex.get("fail")
    return [str(n) for n in (ex.get("to") or []) if str(n).strip()] if want else []


def slack_extra(secrets: dict, item: dict, state: str, text: str):
    import slack_notify
    for name in extra_targets(item, state):
        url, why = slack_notify.extra_url(name, secrets)
        if not url:
            _log(f"⚠️ Slack（{name}）に送れません: {why}")
            continue
        ok, err = slack_notify.post(url, text)
        if not ok:
            _log(f"⚠️ Slack（{name}）に送れませんでした: {err}")


STATE_MARK = {"完了": "✅", "確認待ち": "⏸", "失敗": "🛑", "見送り": "⏭"}
LOOK_MARK = "🛡"


def has_look(res: dict) -> bool:
    """完了でも、人に見てほしいこと（🛡 上書きしなかった行）があるか。

    ⭐ 守りは失敗ではないので「完了」にするが、**Slackは送る**（担当者の希望 2026-09-23）。
       「完了は知らせない」設定（`notify_done` OFF）の予定でも、この日だけは知らせる。
    """
    return any(str(s.get("結果", "")) == LOOK_MARK for s in res.get("工程", []) or [])


QUIET_MARKS = ("✅", "📭", "⏭", "⏹")      # 「問題なし」の印。Slackでは1行ずつ並べない


def _top_split(text: str) -> list:
    """「／」で区切る。ただし（…）の中の「／」では区切らない（「項目…／Id…」を割らないため）。"""
    parts, buf, depth = [], "", 0
    for ch in text:
        depth += ch in "（("
        depth -= ch in "）)" and depth > 0
        if ch == "／" and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _drop_detail(text: str) -> str:
    """🛡・✏️ の文から、いちばん外側の（…）を落とす。Id・項目の並びはアプリで見られるので、Slackには載せない。
    ⚠️ 失敗の文の（…）は残す（「HTTP 404」のように、原因がそこに書いてあるため）。"""
    if not text.startswith((LOOK_MARK, "✏️")):
        return text.strip()
    out, depth = "", 0
    for ch in text:
        if ch in "（(":
            depth += 1
        elif ch in "）)" and depth:
            depth -= 1
        elif not depth:
            out += ch
    return out.strip()


_LEAD_MARKS = re.compile(r"^(?:[🛑❌⏸🛡⏭⏹📭✅❓🔀🔁📞]|⚠️?|✏️?|\s)+")


def _plain(text: str) -> str:
    """頭の印（🛑・⚠️ など）を外す。状態は1行目に書いてあるので、行ごとには付けない。"""
    return _LEAD_MARKS.sub("", str(text)).strip()


def _step_lines(s: dict) -> list:
    """投入以外の工程を、投入（`sf_ui.slack_brief`）と同じ「名前　理由」の行にする。

    ⭐ 担当者の指定（2026-09-26）：`東宝ハウスDL　値相違の為上書きNG：1件` の形にそろえる。
    ⚠️ シートが何枚もある工程は「A：📭 0件…／B：🛑 …」と1つの文につながっているので、
       問題の無い分（✅・📭）は落とし、残りをシートごとに1行にする。
    """
    name = str(s.get("工程", ""))
    groups = []                   # [[見出し, [文…]]]
    for p in _top_split(str(s.get("中身", ""))):
        m = re.match(r"^([^：（]{1,40})：(.*)$", p)
        if m and not p.startswith(QUIET_MARKS + (LOOK_MARK, "✏️", "⚠️", "🛑")):
            groups.append([m.group(1), [m.group(2)]])
        elif groups:
            groups[-1][1].append(p)
        else:
            groups.append(["", [p]])
    if len(groups) <= 1:                        # 1本だけなら「工程名　理由」
        texts = groups[0][1] if groups else []
        body = "／".join(_plain(_drop_detail(t)) for t in texts if t.strip())
        return [f"{name}　{body}"[:200] if body else name]
    out = []
    for label, texts in groups:
        texts = [_plain(_drop_detail(t)) for t in texts if not t.strip().startswith(QUIET_MARKS)]
        if texts:
            out.append(f"{label or name}　" + "／".join(texts))
    return [x[:200] for x in out] or [name]


def slack_text(item: dict, res: dict, started: str) -> str:
    """Slackの文。見てほしい工程だけを並べる。

    ⭐ 形は担当者の指定（2026-09-26）：
        *🗃 データローダー「不動産付箋付けDL」*　完了（上書きしなかった行あり）　08:20 開始
        東宝ハウスDL　値相違の為上書きNG：1件　上書き条件OKの為上書き：1件
        　└ 値相違の為上書きNG：006…
    投入の工程は `sf_ui.slack_brief` が作った行（工程の "Slack"）をそのまま使う。
    """
    state = res.get("結果", "")
    look = has_look(res)
    head = f"*{item_label(item)}*　{state}{'（上書きしなかった行あり）' if look and state == '完了' else ''}"            f"　{str(started)[-5:]} 開始"
    lines = []
    for s in res.get("工程", []) or []:
        if "Slack" in s:
            lines += s["Slack"]
        elif str(s.get("結果", "")) not in QUIET_MARKS:
            lines += _step_lines(s)
    if len(lines) > 25:
        lines = lines[:25] + [f"…ほか{len(lines) - 25}行（アプリの記録を見てください）"]
    if state == "確認待ち":
        lines.append("👉 アプリで確認してから、続きを実行してください（送信・投入はしていません）")
    elif state == "失敗":
        lines.append("👉 「⏰ 時間指定の自動実行」で記録を見て、そのページから実行し直してください")
    return "\n".join([head] + lines)


# ==========================================
# ▶ 実行
# ==========================================
def run_item(sb, secrets: dict, item: dict, reason: str = "時刻") -> dict:
    started = f"{dt.datetime.now():%Y/%m/%d %H:%M}"
    _log(f"▶ {item_label(item)} を始めます（{reason}）")
    runs = load_runs(sb)
    runs["running"] = {"id": item.get("id"), "label": item_label(item), "start": started}
    save_runs(sb, runs)
    t0 = time.time()
    res = auto_jobs.run(item.get("kind", ""), str(item.get("target", "") or ""), secrets,
                        also_delete_jobs=item.get("also_delete_jobs") or [])
    minutes = round((time.time() - t0) / 60, 1)
    _log(f"■ {item_label(item)}：{res.get('結果')}（{minutes}分）")
    runs = load_runs(sb)          # 読み直して足す（実行中に画面が依頼を足していることがある）
    runs["running"] = None
    if reason == "時刻":
        # ⚠️ 手で・画面から動かした分は「今日の分」にしない（朝のうちに試しても、時刻が来たら動く）
        runs.setdefault("done", {})[str(item.get("id"))] = f"{dt.datetime.now():%Y-%m-%d}"
    hist = runs.get("history") or []
    hist.insert(0, {"id": item.get("id"), "予定": item_label(item), "きっかけ": reason,
                    "開始": started, "かかった分": minutes, "結果": res.get("結果"),
                    "工程": [{k: v for k, v in s.items() if k != "詳細"} for s in res.get("工程", [])],
                    "PC": this_host()})
    runs["history"] = hist[:HISTORY_LIMIT]
    save_runs(sb, runs)
    _text = slack_text(item, res, started)
    # ⭐ 完了でも 🛡（上書きしなかった行）があれば知らせる（`notify_done` がOFFの予定でも）
    if res.get("結果") != "完了" or item.get("notify_done", True) or has_look(res):
        slack(secrets, _text)
    slack_extra(secrets, item, res.get("結果", ""), _text)
    return res


class _Lock:
    """1台のPCで見回りを1つずつにする。OSのロックなので、落ちても残らない。"""

    def __init__(self):
        d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EnkanAI")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, "scheduler.lock")
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *a):
        try:
            self.f.close()
        except Exception:
            pass


def tick():
    secrets = auto_jobs.load_secrets()
    sb = auto_jobs.supabase_client(secrets)
    cfg = load_schedule(sb)
    host = str(cfg.get("host", "") or "")
    if not host or host != this_host():
        return 0          # 自動実行用のPCではない（二重に動かさない）
    with _Lock() as got:
        if not got:
            return 0      # 前の見回りがまだ動いている
        runs = load_runs(sb)
        runs["last_tick"] = f"{dt.datetime.now():%Y/%m/%d %H:%M}"
        runs["host"] = this_host()
        # 🔔 Slackに送るのはこのPCだけ。画面は「開いているPC」ではなく、このPCで送り先が読めるかで警告を出す
        #    （共有の送り先があっても、このPCの ENKAN_SECRET_KEY が違うと読めないため）
        import slack_notify
        _u, _src, _why = slack_notify.webhook_url(secrets, sb)
        runs["slack_ready"] = bool(_u)
        runs["slack_why"] = _why
        save_runs(sb, runs)
        items = {str(i.get("id")): i for i in (cfg.get("items") or [])}
        # 🙋 画面からの「次の見回りで動かす」
        for rid in _take_requests(sb):
            if rid in items:
                run_item(sb, secrets, items[rid], reason="画面から依頼")
        # 🕒 時刻が来たもの（早い順）
        for item in sorted(items.values(), key=lambda i: str(i.get("time", ""))):
            cfg_now = load_schedule(sb)        # 長い実行のあいだに消された・止められた予定は動かさない
            cur = next((i for i in (cfg_now.get("items") or []) if str(i.get("id")) == str(item.get("id"))), None)
            if not cur or str(cfg_now.get("host", "")) != this_host():
                continue
            runs = load_runs(sb)
            state = due_state(cur, dt.datetime.now(), (runs.get("done") or {}).get(str(cur.get("id")), ""))
            if state == "run":
                run_item(sb, secrets, cur)
            elif state == "late":
                # 🛑 PCが止まっていた・前の実行が長引いた。遅れて送る・入れるほうが困るので、見送って知らせる
                started = f"{dt.datetime.now():%Y/%m/%d %H:%M}"
                res = {"結果": "見送り", "工程": [{"工程": "開始", "結果": "⏭",
                                                  "中身": f"{cur.get('time')} の予定から "
                                                        f"{cur.get('late_min', DEFAULT_LATE_MIN)}分以上遅れたので、今日は動かしませんでした"}]}
                runs.setdefault("done", {})[str(cur.get("id"))] = f"{dt.datetime.now():%Y-%m-%d}"
                hist = runs.get("history") or []
                hist.insert(0, {"id": cur.get("id"), "予定": item_label(cur), "きっかけ": "時刻",
                                "開始": started, "かかった分": 0, "結果": "見送り",
                                "工程": res["工程"], "PC": this_host()})
                runs["history"] = hist[:HISTORY_LIMIT]
                save_runs(sb, runs)
                _log(f"⏭ {item_label(cur)}：遅れたので見送り")
                _text = slack_text(cur, res, started)
                slack(secrets, _text)
                slack_extra(secrets, cur, "見送り", _text)
    return 0


# ==========================================
# 🧰 タスクスケジューラへの登録
# ==========================================
def _pythonw() -> str:
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.path.isfile(w) else exe


def task_command() -> str:
    return f'"{_pythonw()}" "{os.path.join(BASE_DIR, "scheduler.py")}" --tick'


def install() -> tuple:
    """5分おきの見回りを、このPCのタスクスケジューラに登録する（ログオン中だけ動く＝画面が出せる）。"""
    if os.name != "nt":
        return False, "Windows のPCで登録してください。"
    p = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", task_command(),
                        "/SC", "MINUTE", "/MO", str(TICK_MINUTES), "/F"],
                       capture_output=True, text=True, encoding="cp932", errors="replace")
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def uninstall() -> tuple:
    if os.name != "nt":
        return False, "Windows のPCで行ってください。"
    p = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, text=True, encoding="cp932", errors="replace")
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def installed() -> bool:
    if os.name != "nt":
        return False
    p = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                       capture_output=True, text=True, encoding="cp932", errors="replace")
    return p.returncode == 0


def main(argv):
    # pythonw（窓を出さない起動）では出力先が無いので、記録のフォルダに向ける
    if sys.stdout is None or sys.stderr is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        _out = open(os.path.join(LOG_DIR, "見回りの出力.log"), "a", encoding="utf-8")
        sys.stdout = sys.stdout or _out
        sys.stderr = sys.stderr or _out
    if "--tick" in argv:
        try:
            return tick()
        except Exception as e:
            import traceback
            _log(f"❌ 見回りでエラー: {e}\n{traceback.format_exc()[-1500:]}")
            return 1
    if "--run" in argv:
        rid = argv[argv.index("--run") + 1]
        secrets = auto_jobs.load_secrets()
        sb = auto_jobs.supabase_client(secrets)
        item = next((i for i in (load_schedule(sb).get("items") or []) if str(i.get("id")) == rid), None)
        if not item:
            print(f"予定 {rid} が見つかりません")
            return 1
        res = run_item(sb, secrets, item, reason="手で実行")
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    # 🤖 ロボットを1台だけ手で動かす。⚠️ 既定は**送らないお試し**。
    #    本当に送るときだけ --submit を付ける（手で打った1回で送ってしまわないように）。
    if "--robot" in argv:
        rname = argv[argv.index("--robot") + 1]
        secrets = auto_jobs.load_secrets()
        sb = auto_jobs.supabase_client(secrets)
        gc = auto_jobs.gspread_client(secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "") or "")
        _sub = "--submit" in argv
        print("🚀 本番（実際に送ります）" if _sub else "🧪 お試し（送りません）")
        res = auto_jobs.run_one_robot(sb, rname, gc, submit=_sub)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get("結果") != "失敗" else 1
    if "--install" in argv:
        ok, msg = install()
        print(msg)
        return 0 if ok else 1
    if "--uninstall" in argv:
        ok, msg = uninstall()
        print(msg)
        return 0 if ok else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
