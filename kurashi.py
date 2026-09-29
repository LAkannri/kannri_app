"""🛡 暮らし安心のエントリー（決済システム → nuworks → エントリー済み）。

画面（`kurashi_ui.py`）と時間指定の自動実行（`auto_jobs.run` の kind="kurashi"）が同じ `run` を通る。

  ① 決済システムの口（/api/enkan/*）から、未エントリーのエントリーCSVと、きょうの解約CSVを受け取る
  ② ロボット（既定「暮らし安心」）が nuworks に新規インポート → 一括解約
  ③ **入れ終わってから**、CSVに入れた会員IDだけを決済システムで「エントリー済み」にする

⚠️ nuworks は**同じエントリーを2回入れると2件になる**（解約は何度入れても平気）。
   そこで、インポートに進む前に「これから入れる会員ID」を控え（`pending`）に書き、
   エントリー済みにできたら消す。控えが残っているうちは、次の回は**入れずに止まる**
   （入ったのか入っていないのかを、人がnuworksを見て決める）。
⚠️ Streamlit を import しない（scheduler から使うため）。
"""
import base64
import datetime as _dt
import json
import os
import secrets as _secrets
import time
import urllib.error
import urllib.parse
import urllib.request

SETTINGS_ID = "__kurashi__"
DEFAULT_BASE_URL = "https://payment-system-zeta-lovat.vercel.app"
# 決済システムで「全案件＋未エントリー」が0件なのを確かめた（2026-09-29）うえでの区切り。
# entered_at を記録する前の案件を、nuworksへもう一度入れないため（決済システム側 export-query 参照）。
DEFAULT_SINCE = "2026-09-01"
DEFAULT_ROBOT = "暮らし安心"
ROBOT_TIMEOUT_SEC = 15 * 60
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(BASE_DIR, "取り込みファイル", "暮らし安心")

_ERRORS = {
    401: "合言葉が違います（Vercelの ENKAN_API_TOKEN と、ここで作った合言葉が同じか確かめてください）",
    404: "決済システムに口がありません（PR「エンカンAIから呼ぶ口を足す」がまだデプロイされていない？）",
    503: "決済システムの口が閉じています（Vercelに ENKAN_API_TOKEN が入っていない・入れたあと再デプロイしていない）",
}


def today_jst() -> str:
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")


# ==========================================
# 設定（Supabase の予約行 __kurashi__）
# ==========================================
def load(sb) -> dict:
    res = sb.table("merchants").select("config_json").eq("id", SETTINGS_ID).execute()
    return (res.data[0].get("config_json") or {}) if res.data else {}


def save(sb, part: dict) -> dict:
    """⚠️ 読み直して、渡した項目だけ変える（別の画面・PCで同時に直した分を消さない）。"""
    cur = load(sb)
    for k, v in part.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    sb.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（暮らし安心の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cur}).execute()
    return cur


def base_url(cfg: dict) -> str:
    return str(cfg.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")


def since(cfg: dict) -> str:
    return str(cfg.get("since") or DEFAULT_SINCE).strip()


def robot_name(cfg: dict) -> str:
    return str(cfg.get("robot") or DEFAULT_ROBOT).strip()


# ==========================================
# 合言葉（決済システムの ENKAN_API_TOKEN と同じもの。暗号化して全PCで共有）
# ==========================================
def _fernet(secrets: dict):
    import slack_notify
    return slack_notify._fernet(secrets or slack_notify._toml())


def token(cfg: dict, secrets: dict = None):
    """(合言葉, 読めなかった理由)。"""
    enc = str(cfg.get("token_enc") or "")
    if not enc:
        return "", "合言葉がまだありません（⚙️ 設定の「🔑 合言葉を作る」）"
    f = _fernet(secrets)
    if f is None:
        return "", "このPCに ENKAN_SECRET_KEY が無いので、合言葉を読めません"
    try:
        return f.decrypt(enc.encode()).decode(), ""
    except Exception:
        return "", "このPCの ENKAN_SECRET_KEY が、合言葉を作ったPCと違うので読めません"


def new_token(sb, secrets: dict = None):
    """新しい合言葉を作って保存する。(合言葉, エラー)。⚠️ 画面に出すのはこの1回だけ。"""
    f = _fernet(secrets)
    if f is None:
        return "", "このPCに ENKAN_SECRET_KEY が無いので、暗号化して保存できません"
    tok = _secrets.token_urlsafe(32)
    save(sb, {"token_enc": f.encrypt(tok.encode()).decode(),
              "token_made": time.strftime("%Y/%m/%d %H:%M")})
    return tok, ""


# ==========================================
# 決済システムの口
# ==========================================
def _call(cfg: dict, secrets: dict, method: str, path: str, params: dict = None, body: dict = None):
    """(JSON, エラー)。"""
    tok, err = token(cfg, secrets)
    if not tok:
        return None, err
    url = base_url(cfg) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return None, _ERRORS.get(e.code, f"決済システムが HTTP {e.code} を返しました：{detail}")
    except Exception as e:
        return None, f"決済システムにつながりませんでした：{str(e)[:200]}"


def fetch(cfg: dict, secrets: dict = None, day: str = None, save_files: bool = True):
    """未エントリーのエントリーCSVと、その日の解約CSVを受け取る。

    ({"entry": {...}, "cancel": {...}}, エラー)。各 {"ids", "count", "path"}。
    """
    day = day or today_jst()
    out = {}
    for key, path, params in (("entry", "/api/enkan/entry", {"since": since(cfg)}),
                              ("cancel", "/api/enkan/cancel", {"from": day, "to": day})):
        js, err = _call(cfg, secrets, "GET", path, params)
        if err:
            return None, f"{'エントリー' if key == 'entry' else '解約'}のCSV：{err}"
        ids = [str(x) for x in (js.get("accountIds") or [])]
        p = ""
        if save_files:
            os.makedirs(WORK_DIR, exist_ok=True)
            p = os.path.join(WORK_DIR, js.get("filename") or f"{key}_{day}.csv")
            with open(p, "wb") as f:
                f.write(base64.b64decode(js.get("csvBase64") or ""))
        out[key] = {"ids": ids, "count": len(ids), "path": p}
    return out, ""


def mark_entered(cfg: dict, secrets: dict, ids: list):
    """(変わった件数, エラー)。渡した会員IDだけエントリー済みにする。"""
    ids = [str(x) for x in ids or [] if str(x).strip()]
    if not ids:
        return 0, ""
    js, err = _call(cfg, secrets, "POST", "/api/enkan/entered", body={"accountIds": ids})
    if err:
        return 0, err
    return int(js.get("count") or 0), ""


# ==========================================
# 通しで動かす
# ==========================================
def _read_log(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def pending(cfg: dict) -> dict:
    return cfg.get("pending") or {}


def run(sb, secrets: dict = None, live: bool = True) -> dict:
    """① 受け取り → ② nuworks → ③ エントリー済み。`auto_jobs._Steps` の形で返す。

    live=False（お試し）はインポートしない（ロボットは『送信（本番のみ）』を飛ばす）・控えも書かない。
    """
    import auto_jobs
    import sms_runner
    steps = auto_jobs._Steps()
    cfg = load(sb)

    pend = pending(cfg)
    if live and pend.get("ids"):
        steps.add("⓪ 前回の控え", "⏸",
                  f"前回（{pend.get('at', '')}）nuworksに入れた可能性がある {len(pend['ids'])}件が、"
                  "まだエントリー済みになっていません。二重に入れないよう、今回は入れずに止めました。"
                  "「🛡 暮らし安心」の画面で、nuworksに入っていたかを選んでください。")
        return steps.result()

    got, err = fetch(cfg, secrets)
    if err:
        steps.add("① 決済システムから受け取る", "🛑", err)
        return steps.result()
    ent, can = got["entry"], got["cancel"]
    steps.add("① 決済システムから受け取る", "✅",
              f"エントリー {ent['count']}件（{since(cfg)} 以降の未エントリー）／解約 {can['count']}件（きょう）")
    if not ent["count"] and not can["count"]:
        steps.add("② nuworks", "⏹", "エントリーも解約も0件なので、nuworksには何もしません")
        return steps.result()

    if live and ent["ids"]:
        # ⚠️ インポートに進む**前に**控える（落ちても、入れたかもしれない案件が分かるように）
        save(sb, {"pending": {"ids": ent["ids"], "at": time.strftime("%Y/%m/%d %H:%M")}})

    robot = robot_name(cfg)
    log_path = os.path.join(WORK_DIR, "robot.log")
    args = ["--run", robot, WORK_DIR,
            "--var", f"エントリーファイル={ent['path']}",
            "--var", f"解約ファイル={can['path']}"]
    if live:
        args.append("--submit")
    ok, tail = sms_runner._run_robot_cli(args, log_path, ROBOT_TIMEOUT_SEC)
    # ⚠️ _run_robot_cli が返すのはログの末尾だけ。インポートに進んだかは、ログ全体で見る
    reached = sms_runner.submit_reached(_read_log(log_path))
    label = "② nuworksに入れる" + ("" if live else "（お試し・インポートしていません）")

    if not ok:
        if live and not reached:
            save(sb, {"pending": None})
            steps.add(label, "🛑", "インポートの手前で止まりました（nuworksには何も入っていません）。\n\n" + tail[-1500:])
        elif live:
            steps.add(label, "🛑", "インポートまで進んでから止まりました。nuworksに入ったかもしれないので、"
                      "画面で確かめて選んでください（控えを残しました）。\n\n" + tail[-1500:])
        else:
            steps.add(label, "🛑", tail[-1500:])
        return steps.result()
    steps.add(label, "✅", f"エントリー {ent['count']}件・解約 {can['count']}件" if live
              else "ログイン → エントリーのCSVを選ぶ、まで通りました")
    if not live:
        return steps.result()

    if not ent["ids"]:
        steps.add("③ エントリー済みにする", "⏹", "エントリーが0件なので、することはありません")
        return steps.result()
    n, err = mark_entered(cfg, secrets, ent["ids"])
    if err:
        save(sb, {"pending": {"ids": ent["ids"], "at": time.strftime("%Y/%m/%d %H:%M"),
                              "phase": "mark"}})
        steps.add("③ エントリー済みにする", "🛑",
                  f"nuworksには入れましたが、エントリー済みにできませんでした：{err}\n"
                  "控えを残したので、次の回は入れずに止まります（二重に入れないため）。"
                  "画面の「nuworksに入っていた」で直せます。")
        return steps.result()
    save(sb, {"pending": None, "last_done": {"at": time.strftime("%Y/%m/%d %H:%M"),
                                              "entry": ent["count"], "cancel": can["count"]}})
    steps.add("③ エントリー済みにする", "✅", f"{n}件をエントリー済みにしました")
    return steps.result()


def resolve_pending(sb, secrets: dict, imported: bool):
    """控えを片付ける。imported=True なら決済システムでエントリー済みにしてから消す。(件数, エラー)。"""
    cfg = load(sb)
    ids = pending(cfg).get("ids") or []
    n = 0
    if imported and ids:
        n, err = mark_entered(cfg, secrets, ids)
        if err:
            return 0, err
    save(sb, {"pending": None})
    return n, ""
