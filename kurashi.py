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
import re
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
# 解約も同じ考え方（決済システムの cancel_entered_at が空か）。2026-09-30 までの解約は、手で入れてあるので
# 決済システム側（p006）で済みにしてある。ここはそれより前を拾わないための保険。
DEFAULT_CANCEL_SINCE = "2026-09-30"
DEFAULT_ROBOT = "暮らし安心"
DEFAULT_CANCEL_ROBOT = "暮らし安心_解約"
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


def cancel_since(cfg: dict) -> str:
    return str(cfg.get("cancel_since") or DEFAULT_CANCEL_SINCE)


def cancel_robot_name(cfg: dict) -> str:
    return str(cfg.get("cancel_robot") or DEFAULT_CANCEL_ROBOT).strip()


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
    """未エントリーのエントリーCSVと、解約未エントリーの解約CSVを受け取る。

    ({"entry": {...}, "cancel": {...}}, エラー)。各 {"ids", "count", "path"}。
    ⭐ 解約も「きょうの分」ではなく「まだ解約エントリー済みでない分」（入れ忘れた日の解約を次の回に拾う）。
    """
    day = day or today_jst()
    out = {}
    for key, path, params in (("entry", "/api/enkan/entry", {"since": since(cfg)}),
                              ("cancel", "/api/enkan/cancel", {"todo": "1", "since": cancel_since(cfg)})):
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


def mark_cancel_entered(cfg: dict, secrets: dict, ids: list):
    """(変わった件数, エラー)。渡した会員IDだけ解約エントリー済みにする。"""
    ids = [str(x) for x in ids or [] if str(x).strip()]
    if not ids:
        return 0, ""
    js, err = _call(cfg, secrets, "POST", "/api/enkan/cancel-entered", body={"accountIds": ids})
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


def _entry(sb, secrets: dict, live: bool, steps) -> None:
    """① 受け取り → ② 新規インポート → ②-2 エントリー済み → ③ 一括解約。工程は `steps` に足す。

    live=False（お試し）はインポートしない（ロボットは『送信（本番のみ）』を飛ばす）・控えも書かない。
    """
    cfg = load(sb)

    pend = pending(cfg)
    if live and pend.get("ids"):
        steps.add("⓪ 前回の控え", "⏸",
                  f"前回（{pend.get('at', '')}）nuworksに入れた可能性がある {len(pend['ids'])}件が、"
                  "まだエントリー済みになっていません。二重に入れないよう、今回は入れずに止めました。"
                  "「🛡 暮らし安心」の画面で、nuworksに入っていたかを選んでください。")
        return

    got, err = fetch(cfg, secrets)
    if err:
        steps.add("① 決済システムから受け取る", "🛑", err)
        return
    ent, can = got["entry"], got["cancel"]
    steps.add("① 決済システムから受け取る", "✅",
              f"エントリー {ent['count']}件（{since(cfg)} 以降の未エントリー）／"
              f"解約 {can['count']}件（{cancel_since(cfg)} 以降の解約未エントリー）")
    if not ent["count"] and not can["count"]:
        steps.add("② nuworks", "⏹", "エントリーも解約も0件なので、nuworksには何もしません")
        return

    # ⚠️ エントリーと解約はロボットを分けて、0件のほうは動かさない。
    # nuworks は正常に入ると「完了しました／はい」だけを出すが、うまくいかないと
    # エントリー：「データソースにフィールドが定義されていません」→OK →「完了しました」、解約：OKだけ、になる（担当者が確認 2026-09-30）。
    # 0件のCSVを入れてエラーにしない。ロボットは「はい」が出なければ止まる＝エラーを成功と取り違えない。
    if ent["count"]:
        # 新規インポートで止まっても、一括解約は入れる（解約は何度入れても平気で、きょうのうちに入れたいため）
        _import_entry(sb, secrets, cfg, ent, live, steps)
    else:
        steps.add("② nuworksに新規インポート", "⏹", "エントリーが0件なので、新規インポートはしません")
    if can["count"]:
        _import_cancel(cfg, secrets, can, live, steps)
    else:
        steps.add("③ nuworksで一括解約", "⏹", "解約未エントリーが0件なので、一括解約はしません")


# nuworks の小窓の文に、これがあればうまくいっていない
POPUP_NG_WORDS = ("エラー", "定義されていません", "できません", "失敗", "見つかりません", "不正", "無効", "中止")
POPUP_DONE_WORD = "完了しました"
_POPUP_RE = re.compile(r"🗨 小窓：(.*?) →「")


def popups(log: str) -> list:
    """ロボットの『小窓に答える』が答えた小窓の文（出た順）。"""
    return [m.group(1).strip() for m in _POPUP_RE.finditer(log or "")]


def popup_verdict(texts: list) -> str:
    """"ng"（エラーらしい文がある）／"done"（完了しました だけ）／"unknown"（出ない・知らない文）。"""
    if any(w in t for t in texts for w in POPUP_NG_WORDS):
        return "ng"
    if any(POPUP_DONE_WORD in t for t in texts):
        return "done"
    return "unknown"


def popup_note(texts: list) -> str:
    return ("nuworksの小窓：" + "　→　".join(f"「{t}」" for t in texts)) if texts else "nuworksの小窓は出ませんでした"


def _run(robot: str, var: str, path: str, log_name: str, live: bool):
    """(ok, ログの末尾, インポートまで進んだか, 小窓の文)。"""
    import sms_runner
    log_path = os.path.join(WORK_DIR, log_name)
    args = ["--run", robot, WORK_DIR, "--var", f"{var}={path}"]
    if live:
        args.append("--submit")
    ok, tail = sms_runner._run_robot_cli(args, log_path, ROBOT_TIMEOUT_SEC)
    # ⚠️ _run_robot_cli が返すのはログの末尾だけ。インポートに進んだか・小窓の文は、ログ全体で見る
    log = _read_log(log_path)
    return ok, tail, sms_runner.submit_reached(log), popups(log)


def _import_entry(sb, secrets: dict, cfg: dict, ent: dict, live: bool, steps) -> bool:
    """新規インポート → エントリー済みにする。最後まで通れば True。"""
    if live:
        # ⚠️ インポートに進む**前に**控える（落ちても、入れたかもしれない案件が分かるように）
        save(sb, {"pending": {"ids": ent["ids"], "at": time.strftime("%Y/%m/%d %H:%M")}})
    ok, tail, reached, pops = _run(robot_name(cfg), "エントリーファイル", ent["path"], "robot.log", live)
    label = "② nuworksに新規インポート" + ("" if live else "（お試し・インポートしていません）")
    # ⚠️ 新規は二重に入れると2件になるので、「完了しました」が出てエラーらしい文が無いときだけ入ったとみなす。
    #    小窓が出ない・知らない文だけのときは、控えを残して人に確かめてもらう。
    if ok and live:
        verdict = popup_verdict(pops)
        if verdict == "ng":
            steps.add(label, "🛑", popup_note(pops) + "\nエラーらしい文が出たので、入ったとみなしません。"
                      "nuworksで確かめて、画面で選んでください（控えを残しました）。")
            return False
        if verdict == "unknown":
            steps.add(label, "⏸", popup_note(pops) + "\n「完了しました」が確かめられなかったので、入ったとみなしません。"
                      "nuworksで確かめて、画面で選んでください（控えを残しました）。")
            return False

    if not ok:
        if live and not reached:
            save(sb, {"pending": None})
            steps.add(label, "🛑", "インポートの手前で止まりました（nuworksには何も入っていません）。\n\n" + tail[-1500:])
        elif live:
            steps.add(label, "🛑", "インポートを押したあと「完了しました」が出ませんでした。nuworksがエラー"
                      "（例：データソースにフィールドが定義されていません）を出したかもしれません。"
                      "入ったかどうかをnuworksで確かめて、画面で選んでください（控えを残しました）。\n\n" + tail[-1500:])
        else:
            steps.add(label, "🛑", tail[-1500:])
        return False
    steps.add(label, "✅", f"エントリー {ent['count']}件（{popup_note(pops)}）" if live
              else "ログイン → エントリーのCSVを選ぶ、まで通りました")
    if not live:
        return True

    n, err = mark_entered(cfg, secrets, ent["ids"])
    if err:
        save(sb, {"pending": {"ids": ent["ids"], "at": time.strftime("%Y/%m/%d %H:%M"),
                              "phase": "mark"}})
        steps.add("②-2 エントリー済みにする", "🛑",
                  f"nuworksには入れましたが、エントリー済みにできませんでした：{err}\n"
                  "控えを残したので、次の回は入れずに止まります（二重に入れないため）。"
                  "画面の「nuworksに入っていた」で直せます。")
        return True     # 解約は何度入れても平気なので、続けて入れる
    save(sb, {"pending": None, "last_done": {"at": time.strftime("%Y/%m/%d %H:%M"),
                                              "entry": ent["count"]}})
    steps.add("②-2 エントリー済みにする", "✅", f"{n}件をエントリー済みにしました")
    return True


def _import_cancel(cfg: dict, secrets: dict, can: dict, live: bool, steps) -> None:
    """一括解約 → 解約エントリー済みにする。解約は何度入れても平気なので、控えは持たない
    （止まったら解約エントリー済みにしない＝次の回にもう一度入れる）。"""
    ok, tail, reached, pops = _run(cancel_robot_name(cfg), "解約ファイル", can["path"], "robot_cancel.log", live)
    label = "③ nuworksで一括解約" + ("" if live else "（お試し・インポートしていません）")
    # ⭐ 解約は、エラーらしい文が出たときだけ失敗にする。小窓が出なくても入っていた（2026-10-04 担当者が確認）。
    if ok and live and popup_verdict(pops) == "ng":
        steps.add(label, "🛑", popup_note(pops) + "\nエラーらしい文が出たので、解約エントリー済みにしません。"
                  "次の回にもう一度入れます（解約は入れ直しても平気です）。")
        return
    if not ok:
        why = ("インポートを押したあと「完了しました」が出ませんでした（nuworksがエラーを出したかもしれません）。"
               "解約エントリー済みにしていないので、次の回にもう一度入れます（解約は入れ直しても平気です）。\n\n"
               if live and reached else "")
        steps.add(label, "🛑", why + tail[-1500:])
        return
    steps.add(label, "✅", f"解約 {can['count']}件（{popup_note(pops)}）" if live else "ログイン → 解約のCSVを選ぶ、まで通りました")
    if not live:
        return
    n, err = mark_cancel_entered(cfg, secrets, can["ids"])
    if err:
        steps.add("③-2 解約エントリー済みにする", "🛑",
                  f"nuworksには入れましたが、解約エントリー済みにできませんでした：{err}\n"
                  "次の回にもう一度入れます（解約は入れ直しても平気です）。")
        return
    steps.add("③-2 解約エントリー済みにする", "✅", f"{n}件を解約エントリー済みにしました")


def run(sb, secrets: dict = None, live: bool = True) -> dict:
    """① 受け取り → ② nuworks → ③ エントリー済み →（本番のみ）④ Salesforceへ進捗反映。

    `auto_jobs._Steps` の形で返す。live=False（お試し）はインポートしない・控えも書かない・Salesforceにも入れない。
    ④はエントリーの結果にかかわらず行う（決済システムの今の中身をそのまま写すだけで、何度入れても同じになるため）。
    """
    import auto_jobs
    steps = auto_jobs._Steps()
    _entry(sb, secrets, live, steps)
    if live and load(sb).get("progress_on", True):
        progress(sb, secrets, steps)
    return steps.result()


# ==========================================
# ④ Salesforceへの進捗反映（決済システムの「データローダ」CSVと同じ中身）
# ==========================================
DEFAULT_PROGRESS_FROM = "2026-08-01"
PROGRESS_OBJECT = "Opportunity"
# 付帯OP_暮らし安心 / 課金開始日（暮らし安心） / 解約日（暮らし安心）（担当者 2026-09-30：既存の項目を使う）
F_OPTION, F_CHARGE, F_CANCEL = "Field33__c", "Field57__c", "OgkaisenDate__c"


def progress_from(cfg: dict) -> str:
    return str(cfg.get("progress_from") or DEFAULT_PROGRESS_FROM).strip()


def option_value(plan: str) -> str:
    """決済システムの付帯名 → 付帯OP_暮らし安心 の選択肢。分からなければ空（送らない）。

    プラス（「暮らし安心プラスB」も）→ 確定案件_プラス／プレミアム → 確定案件_プレミアム（担当者 2026-09-30）。
    """
    import unicodedata
    p = unicodedata.normalize("NFKC", str(plan or ""))
    if "プレミアム" in p:
        return "確定案件_プレミアム"
    if "プラス" in p:
        return "確定案件_プラス"
    return ""


def progress_records(rows):
    """(送る行, 送らなかった行の説明)。

    ⭐ 付帯OPは入っていても上書きする。解約した案件も同じ（担当者 2026-09-30）。
    ⚠️ 解約日が空の案件は解約日を送らない（Salesforceの今の値を消さない）。
    """
    import re
    out, skipped = [], []
    for r in rows or []:
        sid = str(r.get("safCaseNo") or "").strip()
        if not re.fullmatch(r"006[0-9A-Za-z]{12}([0-9A-Za-z]{3})?", sid):
            skipped.append(f"{sid or '（空）'}：SAF案件番号が案件IDの形ではありません")
            continue
        rec = {"Id": sid}
        charge = str(r.get("chargeStartDate") or "").strip()
        cancel = str(r.get("canceledDate") or "").strip()
        if charge:
            rec[F_CHARGE] = charge
        if cancel:
            rec[F_CANCEL] = cancel
        # 解約した案件も付帯OPは送る（担当者 2026-09-30：上書きでよい）
        opt = option_value(r.get("serviceName"))
        if opt:
            rec[F_OPTION] = opt
        else:
            skipped.append(f"{sid}：付帯名「{r.get('serviceName', '')}」が選択肢に読み替えられないので、付帯OPは送りません")
        if len(rec) > 1:
            out.append(rec)
    return out, skipped


def progress(sb, secrets: dict, steps, limit: int = 0) -> dict:
    """決済システムから「データローダ」の中身を受け取り、案件に付帯OP・課金開始日・解約日を入れる。"""
    import salesforce_loader
    cfg = load(sb)
    label = "④ Salesforceへ進捗反映"
    js, err = _call(cfg, secrets, "GET", "/api/enkan/dataloader",
                    {"from": progress_from(cfg), "to": today_jst()})
    if err:
        steps.add(label, "🛑", f"決済システムから受け取れませんでした：{err}")
        return {}
    recs, skipped = progress_records(js.get("rows") or [])
    note = ("\n".join(["", "送らなかった行："] + skipped[:20]) if skipped else "")
    if not recs:
        steps.add(label, "⏹", f"入れる案件がありません（{progress_from(cfg)} 以降）" + note)
        return {}
    try:
        sf = salesforce_loader.connect()
        res = salesforce_loader.upsert(sf, PROGRESS_OBJECT, "Id", recs, limit=limit)
    except Exception as e:
        steps.add(label, "🛑", f"Salesforceにつながりませんでした：{str(e)[:200]}")
        return {}
    msg = f"{res.get('ok', 0)}件を反映（{progress_from(cfg)} 以降・{len(recs)}件）"
    if res.get("ng"):
        errs = "\n".join(f"{e.get('Id', '')}：{e.get('原因', '')}"
                         for e in (res.get("errors") or [])[:20])
        steps.add(label, "🛑", f"{msg}／失敗 {res['ng']}件\n{errs}" + note)
    else:
        save(sb, {"last_progress": {"at": time.strftime("%Y/%m/%d %H:%M"), "count": res.get("ok", 0)}})
        steps.add(label, "✅", msg + note)
    return res


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
