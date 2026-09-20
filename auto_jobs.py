"""
⏰ 画面を出さずに業務を通しで動かす部品（時間指定の自動実行と、各ページの両方から使う）

⭐ **実行の中身は、ここ1か所に置く。** 画面のボタンから動かしたときと、
   時間指定（`scheduler.py`）で動かしたときで、やることが食い違わないようにするため。
   各ページは、ここにある関数を呼んで結果を画面に出すだけにする。

⭐ **人の確認が要る工程では、時間指定の実行は止まる。**
   各業務の設定にある「どこまで自動で行くか」（`auto_push` / `auto_call` / `auto_send` / `auto_load`）
   が ON の工程だけ通し、OFF ならその手前で「⏸ 確認待ち」で終わる（scheduler が Slack で知らせる）。
   送信・投入は取り消せないので、時間指定だからといって確認を飛ばさない。

結果の形：{"結果": "完了"|"確認待ち"|"失敗", "工程": [{"工程","結果","中身"}, ...]}
工程の「結果」の印：✅ 通った ／ 🛑 つまずいた ／ ⏸ 人の確認待ち ／ ⏹ やることが無かった
"""
import json
import os
import re
import time

import sms_runner

try:
    import tomllib
except ImportError:          # Python 3.10 以前
    import tomli as tomllib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 各業務の設定が入っている Supabase の予約行
SETTINGS_IDS = {
    "progress": "__progress__",
    "sms": "__sms__",
    "dataloader": "__dataloader__",
    "autocall": "__autocall__",
    "reports": "__reports__",
}
KIND_LABELS = {
    "progress": "🚀 進捗反映",
    "sms": "📱 SMS送信",
    "dataloader": "🗃 データローダー",
    "autocall": "📞 オートコール投入",
    "reports": "🔄 SFレポート更新",
}
DEFAULT_REFRESH_ROBOT = "共通_SFコネクタ更新"


# ==========================================
# 🔌 つなぎこみ（Streamlit の外でも動くように、secrets.toml を直接読む）
# ==========================================
def load_secrets() -> dict:
    try:
        with open(os.path.join(BASE_DIR, ".streamlit", "secrets.toml"), "rb") as f:
            out = dict(tomllib.load(f))
    except Exception:
        out = {}
    for k in ("SUPABASE_URL", "SUPABASE_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "SLACK_WEBHOOK_URL"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def supabase_client(secrets: dict = None):
    from supabase import create_client
    s = secrets or load_secrets()
    return create_client(s["SUPABASE_URL"], s["SUPABASE_KEY"])


def gspread_client(sa_json: str, timeout_sec: int = 60):
    """⏱ 待ち時間の上限を付ける（無人で固まったままにしないため）。"""
    if not sa_json:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    try:
        gc.http_client.set_timeout(timeout_sec)
    except Exception:
        pass
    return gc


def load_row(supabase, row_id: str) -> dict:
    """予約行の config_json を読む。⚠️ Supabase は時々 504 を返すので、待って読み直す。"""
    err = None
    for wait in (0, 3, 8, 20):
        if wait:
            time.sleep(wait)
        try:
            res = supabase.table("merchants").select("config_json").eq("id", row_id).execute()
            return (res.data[0].get("config_json") or {}) if res.data else {}
        except Exception as e:
            err = e
    raise RuntimeError(f"設定（{row_id}）を読み込めませんでした: {str(err)[:200]}")


def tab_gids(gc, sheet_url: str) -> dict:
    if not (gc and str(sheet_url or "").strip()):
        return {}
    try:
        sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
        return {w.title: w.id for w in sh.worksheets()}
    except Exception:
        return {}


def target_names(supabase, kind: str) -> list:
    """時間指定で選べる対象（ジョブ名・パターン名・セット名など）。"""
    cfg = load_row(supabase, SETTINGS_IDS[kind])
    if kind == "progress":
        return ["（有効なキャリアすべて）"]
    key = {"sms": "patterns", "dataloader": "jobs", "autocall": "jobs", "reports": "sets"}[kind]
    return [str(x.get("name", "")) for x in (cfg.get(key) or []) if str(x.get("name", "")).strip()]


class _Steps:
    """工程の記録。止まった理由で「失敗」か「確認待ち」かを決める。"""

    def __init__(self):
        self.rows = []

    def add(self, name, mark, body=""):
        self.rows.append({"工程": name, "結果": mark, "中身": str(body)})
        return mark

    def result(self):
        marks = [r["結果"] for r in self.rows]
        if "🛑" in marks:
            state = "失敗"
        elif "⏸" in marks:
            state = "確認待ち"
        else:
            state = "完了"
        return {"結果": state, "工程": self.rows}


def _push_rows(gc, sheet_url, loads):
    """登録した投入を順に行う（中身は sf_ui.push_sheet＝画面と同じもの）。"""
    import sf_ui
    out = []
    for ld in loads or []:
        r = sf_ui.push_sheet(gc, sheet_url, str(ld.get("シート", "")),
                             str(ld.get("オブジェクト", "")), str(ld.get("照合キー", "")),
                             ld.get("マッピング", {}) or {}, limit=0,
                             send_blanks=bool(ld.get("空も送る", False)))
        out.append({"シート": str(ld.get("シート", "")), "結果": r.get("結果", ""),
                    "ok": r.get("ok", 0), "ng": r.get("ng", 0),
                    "投入なし": bool(r.get("投入なし"))})
    return out


def _push_ok(r) -> bool:
    """投入の結果を「通った」とみなすか。

    ⭐ 中身は `sf_ui.push_ok` 1か所。📭 0件で投入しなかったものは「通った」
       （やることが無かっただけ。失敗にしない）。
    """
    import sf_ui
    return sf_ui.push_ok(r)


def _watch_step(steps, gc, job):
    """目で見て確認するシート。中身が出ていて『止める』設定なら ⏸。戻り値：先へ進んでよいか。"""
    import watch_ui
    tabs = job.get("watch_tabs", []) or []
    if not tabs:
        return True
    found = watch_ui.read(gc, job.get("sheet_url", ""), tabs)
    hit = [f for f in found if f["件数"] != 0]
    body = "／".join(f"{f['シート']}：{'読めません' if f['件数'] < 0 else str(f['件数']) + '件'}"
                    for f in found)
    if hit and job.get("watch_block", True):
        steps.add("③ 目で見て確認", "⏸", body + "。**投入はしていません**（対応して0件にしてから実行してください）")
        return False
    steps.add("③ 目で見て確認", "✅", (body + "（設定により止めずに進みました）") if hit else "0件")
    return True


# ==========================================
# 🗃 データローダー自動化
# ==========================================
def run_dataloader(supabase, gc, job: dict) -> dict:
    """①更新 → ②GASで作り直し → ③目で見て確認 → ④Salesforce投入（`auto_push` のときだけ）。"""
    steps = _Steps()
    name = str(job.get("name", ""))
    tabs = job.get("refresh_tabs", []) or []
    loads = job.get("loads", []) or []
    if tabs and job.get("refresh_robot"):
        folder = sms_runner.work_dir("データローダー", name)
        urls = sms_runner.tab_urls_for(job["sheet_url"], tabs, tab_gids(gc, job["sheet_url"]))
        ok, log = sms_runner.run_sheet_refresh(job["refresh_robot"], folder, tabs=tabs,
                                               tab_urls=urls, url=job["sheet_url"])
        if steps.add("① シートの更新", "✅" if ok else "🛑",
                     f"{len(tabs)}枚" if ok else sms_runner.stop_reason(log) or log[-300:]) == "🛑":
            return steps.result()
    gas_url = str(job.get("gas_url", "") or "").strip()
    if gas_url:
        gok, gdata = sms_runner.run_gas_action(gas_url, str(job.get("gas_token", "") or ""),
                                               action="build", timeout=900,
                                               build=str(job.get("gas_build", "") or ""))
        if steps.add("② シートの作り直し", "✅" if gok else "🛑",
                     "作り直しました" if gok else str(gdata)[:500]) == "🛑":
            return steps.result()
    elif loads:
        # ⚠️ 投入用シートはGASが作る。走らせずに投入すると、前回の中身のまま送ってしまう。
        steps.add("② シートの作り直し", "⏸",
                  "GASのURLが未設定なので、作り直せません。**投入はしていません**（設定画面の3️⃣）")
        return steps.result()
    if not _watch_step(steps, gc, job):
        return steps.result()
    if not loads:
        return steps.result()
    if not job.get("auto_push"):
        steps.add("④ Salesforceへ投入", "⏸",
                  "設定で「④の投入まで自動で行う」がOFFなので、投入の手前で止めました")
        return steps.result()
    out = _push_rows(gc, job["sheet_url"], loads)
    steps.add("④ Salesforceへ投入", "✅" if all(_push_ok(r) for r in out) else "🛑",
              "／".join(f"{r['シート']}：{r['結果']}" for r in out))
    return steps.result()


# ==========================================
# 📞 オートコール投入
# ==========================================
AUTOCALL_DEFAULT_VARS = ["タイトル"]
GYOMU = "業務"
ACD = "作業グループ"


def ac_vars_of(job) -> list:
    """このジョブで差し込む名前の一覧（手順書の `{名前}` に対応）。"""
    names = [str(x).strip() for x in (job.get("vars") or AUTOCALL_DEFAULT_VARS) if str(x).strip()]
    return names or list(AUTOCALL_DEFAULT_VARS)


def ac_select_step(steps, word):
    """対象に word を含む『選択』の手順の位置（無ければ None）。"""
    for i, s in enumerate(steps or []):
        if str(s.get("操作", "")) in ("選択", "select") and word in str(s.get("対象", "")):
            return i
    return None


def ac_gyomu_value(cfg, label: str) -> str:
    """表で選んだ名前 → ブルービーンに渡す値。覚えていなければ名前のまま渡す（名前でも選べる）。"""
    for o in ((cfg.get("bluebean_options") or {}).get(GYOMU) or {}).get("options", []) or []:
        if o.get("label") == label:
            return str(o.get("value", ""))
    return label


def ac_make_csv(job, entry):
    """④の前半：そのシートのCSVをGASから受け取る。

    ⚠️ アプリで作り直さない。整形はGASが持っているので、できあがりを受け取るだけ。
    """
    import glob
    import shutil
    sheet = str(entry.get("シート", "") or "").strip()
    url = str(job.get("gas_url", "") or "").strip()
    if not url:
        raise RuntimeError("GASのURLが未設定です（設定画面の3️⃣で登録してください）。")
    path, name, rows, _raw = sms_runner.fetch_from_gas(
        url, str(job.get("gas_token", "") or ""), sheet,
        sms_runner.sheet_slot(job.get("name", ""), sheet), keep_drive=False, build="",
        root=sms_runner.AUTOCALL_ROOT)
    # 📛 ブルービーンの一覧に出るファイル名は、渡したファイルの名前そのもの。
    #    「送信データ.csv」のままだと、どれが何の投入か一覧で見分けられない。
    #    **シート名＋日時**で渡す（前回の分はこのフォルダから消す。控えは「履歴」にある）。
    _base = re.sub(r'[\\/:*?"<>|]', "_", sheet) or "オートコール"
    _dir = os.path.dirname(path)
    for _old in glob.glob(os.path.join(_dir, glob.escape(_base) + "_*.csv")):
        try:
            os.remove(_old)
        except Exception:
            pass
    named = os.path.join(_dir, f"{_base}_{time.strftime('%Y%m%d_%H%M')}.csv")
    shutil.copyfile(path, named)
    return named, os.path.basename(named), rows


def _csv_has_rows(path) -> bool:
    """CSVに見出しのほかに中身があるか（0件のCSVは見出しだけ）。"""
    try:
        with open(path, "rb") as f:
            text = f.read().decode("cp932", errors="replace")
    except Exception:
        return False
    return len([x for x in text.splitlines() if x.strip()]) > 1


def ac_job_csvs(job, made=None):
    """そのジョブの全シートのリスト（CSV）を、**1シートに1つずつ**集める。

    ⭐ 投入したあと「弾かれた番号が、同じジョブの別のリストにも入っている番号か」を
       ロボットが見分けるのに使う（後追いの7枚などで同じ人が重なるのは想定どおり）。
    ⚠️ **1シートに1つだけ**にすること。同じ中身のファイル（`送信データ.csv` と `シート名_日時.csv`）を
       二重に数えると、1回しか出てこない番号まで「重なり」に見えてしまう。
    ⚠️ 今回作っていないシート（0件・今回は入れないシート）は、前に作った分を使う。
       ブルービーンにも前に入れた分が残っているので、重なりの相手はその中身で合っている。
    """
    import glob
    made = made or {}
    out = []
    for e in job.get("autocalls") or []:
        sheet = str(e.get("シート", "") or "").strip()
        if not sheet:
            continue
        if made.get(sheet):
            out.append(made[sheet])
            continue
        d = sms_runner.pattern_dir(sms_runner.sheet_slot(str(job.get("name", "") or ""), sheet),
                                   sms_runner.AUTOCALL_ROOT)
        cands = [p for p in glob.glob(os.path.join(glob.escape(d), "*.csv"))
                 if os.path.basename(p) != sms_runner.CSV_NAME]
        if not cands:
            cands = glob.glob(os.path.join(glob.escape(d), glob.escape(sms_runner.CSV_NAME)))
        if not cands:
            continue
        p = max(cands, key=os.path.getmtime)
        if not _csv_has_rows(p):
            # 📭 きょうが0件のシートは、いまのCSVに番号が入っていない。
            #    でもブルービーンには前に入れた分が残っている（0件の日は前のファイルを消さない）ので、
            #    控えの中で**いちばん新しい、中身のあるもの**を1つだけ見る。
            for h in sorted(glob.glob(os.path.join(glob.escape(d), sms_runner.HISTORY_DIR, "*.csv")),
                            key=os.path.getmtime, reverse=True):
                if _csv_has_rows(h):
                    p = h
                    break
        out.append(p)
    return out


def ac_prepare(supabase, cfg, job, entry):
    """1枚ぶんの下ごしらえ：差し込む値を決めて、CSVを受け取る。戻り値：(値, CSVのパス, 名前, 件数)"""
    import common_robots
    sheet = str(entry.get("シート", "") or "").strip()
    robot_name = job.get("call_robot") or common_robots.ROLES["autocall"]["name"]
    variables = {v: str(entry.get(v, "") or "") for v in ac_vars_of(job)}
    _row, _steps = common_robots.robot_row(supabase, robot_name)
    # 🗑 投入の前に、同じ業務・同じシート名で前に入れたファイルを消す（見つけたら確認なしで全部）。
    #    同じデータが残っているとブルービーンで処理失敗になる。時間指定でも最後まで通すため、人の確認は取らない。
    #    ⚠️ 業務で絞らないと、ほかの業務の同じ名前のリストまで消すので、業務が空なら動かさない。
    if not any(str(s.get("操作", "")) == common_robots.BB_DELETE_OP for s in _steps):
        raise RuntimeError(f"ロボット「{robot_name}」の手順書に、前のファイルを消す手順がありません"
                           "（設定画面の「🗑 前のファイルを消す手順を足す」を押してください）。")
    if not str(entry.get(GYOMU, "") or "").strip():
        raise RuntimeError(f"「{sheet}」の業務が選ばれていません（設定画面の5️⃣で選んでください）。")
    variables.update({"削除モード": "探して削除", "削除の業務": str(entry[GYOMU]).strip(),
                      "削除のシート": sheet})
    _gi = ac_select_step(_steps, GYOMU)
    if _gi is not None and "{" + GYOMU + "}" in str(_steps[_gi].get("値", "")):
        # 業務が空のときは上で止めている（違う業務＝録画のときのものに投入しかねないため）
        variables[GYOMU] = ac_gyomu_value(cfg, str(entry[GYOMU]).strip())
    # 作業グループは業務で決まる（2つ以上出る業務だけ、決まりの名前を渡す。無ければ出てきた1つ）
    _acd = sms_runner.acd_for(cfg, entry.get(GYOMU, ""))
    if _acd:
        if ac_select_step(_steps, ACD) is None:
            # 選ぶ手順が無いまま動かすと、決めた作業グループが使われないまま投入してしまう
            raise RuntimeError(f"業務「{entry.get(GYOMU, '')}」は作業グループを「{_acd}」に決めてありますが、"
                               f"ロボットの手順書に作業グループを選ぶ手順がありません。")
        variables[sms_runner.ACD_PICK_VAR] = _acd
    path, name, rows = ac_make_csv(job, entry)
    return variables, path, name, rows


def ac_deleted_count(log: str):
    """周のログから、投入の前に消した前のファイルの件数を拾う（消していなければ 0、分からなければ None）。"""
    m = re.findall(r"前に入れたファイルを (\d+)件 消しました", str(log or ""))
    if m:
        return int(m[-1])
    return 0 if "前に入れたファイルはありませんでした" in str(log or "") else None


def ac_dup_note(log: str) -> str:
    """「無効なデータは、同じジョブの重なりだった」ときの一言（無ければ空）。

    ⭐ 通ったシートはログを開かないので、**表の中で分かるようにする**
       （0件ではないのに弾かれた行があった、という事実は担当者に伝える）。
    """
    m = re.findall(r"弾かれた (\d+)行は、すべて同じジョブの別のリストにも入っている番号でした",
                   str(log or ""))
    return (f"🔁 弾かれた {m[-1]}行は、同じジョブの別のリストにも入っている番号でした"
            "（重なりなので失敗にしていません）。") if m else ""


def _ac_zero_result(sheet, name):
    # 📭 0件の日は入れるものが無い。見出しだけのCSVを入れるとブルービーンでエラーになるので、
    #    ロボットを動かさず「完了（投入なし）」として扱う（ほかのシートと同じく先へ進む）。
    return {"シート": sheet, "ok": True, "log": "0件だったので、投入しませんでした。",
            "CSV": name, "件数": 0, "投入まで進んだ": False,
            "理由": "0件のため投入なし", "投入なし": True}


def _ac_gas_busy(ex) -> bool:
    """CSVの受け取りで、Google側の混雑（404／429／時間切れ）で止まったか。＝ブルービーンには何もしていない。"""
    s = str(ex)
    return "呼び出せませんでした" in s and any(w in s for w in ("404", "429", "timed out", "タイムアウト"))


def autocall_many(supabase, cfg, job, entries, submit: bool):
    """④：シートごとにCSVを用意し、ブルービーンへは**ブラウザ1回・ログイン1回**で続けて入れる。

    ⚠️ 前は1枚ごとにロボットを起動し直していたので、シートの数だけログインから始まっていた。
    1枚が止まっても次のシートへ進む（ロボット側の keep_going）。結果はシートごとに返す。
    """
    return autocall_pairs(supabase, cfg, [(job, e) for e in entries], submit,
                          str(job.get("name", "") or "オートコール"))


def autocall_pairs(supabase, cfg, pairs, submit: bool, slot: str):
    """④の本体。pairs＝[(ジョブ, シート), …]。**ジョブをまたいでも**ブラウザ1回・ログイン1回で入れる。
    使うロボットが違うジョブが混ざっていたら、ロボットごとに1回ずつ開く。
    """
    import common_robots
    default_robot = common_robots.ROLES["autocall"]["name"]
    res = [None] * len(pairs)

    def _pass(indexes):
        groups = {}                       # ロボット名 → (周, 周に入れたシート)
        for i in indexes:
            job, e = pairs[i]
            sheet = str(e.get("シート", "") or "").strip()
            jn = str(job.get("name", "") or "")
            try:
                variables, path, name, rows = ac_prepare(supabase, cfg, job, e)
            except Exception as ex:
                res[i] = {"ジョブ": jn, "シート": sheet, "ok": False, "log": str(ex), "CSV": "", "件数": 0,
                          "投入まで進んだ": False, "理由": str(ex)[:120], "CSVを受け取れず": _ac_gas_busy(ex)}
                continue
            if rows == 0:
                res[i] = {"ジョブ": jn, **_ac_zero_result(sheet, name)}
                continue
            rounds, picked = groups.setdefault(job.get("call_robot") or default_robot, ([], []))
            rounds.append({"label": sheet,
                           "vars": {**variables, "アップロードファイル": path, "CSVファイル": path}})
            picked.append((i, jn, sheet, name, rows))
        # 📞 そのジョブの全シートのリストを、周ごとの値として渡しておく。
        #    投入のあと「弾かれた番号が、同じジョブの別のリストにも入っている番号か」を
        #    ロボットが見分けるのに使う（重なりなら失敗にしない）。
        _made = {}
        for _rn, (rounds, picked) in groups.items():
            for (i, jn, sheet, _name, _rows), rd in zip(picked, rounds):
                _made.setdefault(jn, {})[sheet] = rd["vars"].get("アップロードファイル", "")
        for _rn, (rounds, picked) in groups.items():
            for (i, jn, sheet, _name, _rows), rd in zip(picked, rounds):
                rd["vars"][sms_runner.JOB_LISTS_VAR] = ac_job_csvs(pairs[i][0], _made.get(jn, {}))
        for robot_name, (rounds, picked) in groups.items():
            _ok, _tail, per = sms_runner.run_autocall_rounds(robot_name, slot, rounds, submit=submit)
            for (i, jn, sheet, name, rows), r in zip(picked, per):
                res[i] = {"ジョブ": jn, "シート": sheet, "ok": r["ok"], "log": r["log"], "CSV": name,
                          "件数": rows, "投入まで進んだ": r["submitted"], "理由": r["reason"],
                          "消した前のファイル": ac_deleted_count(r["log"]),
                          "重なり": ac_dup_note(r["log"])}

    _pass(range(len(pairs)))
    # 🔁 GASが混んでいてCSVを受け取れなかったシートは、**ほかのシートを入れ終わってから**もう1回だけ受け取り直して入れる。
    #    ⚠️ 全部実行で 404 のまま止まり、人が押し直したら通った。CSVを受け取る前に止まった＝ブルービーンには何もしていないので、
    #    入れ直しても重ならない。完全に自動で回すため、人の押し直しをここで代わりに行う。
    _again = [i for i, r in enumerate(res) if r and r.get("CSVを受け取れず")]
    if _again:
        _pass(_again)
        for i in _again:
            res[i]["自動でやり直し"] = True
    for r in res:
        r["本番"] = bool(submit)          # 止まった分だけやり直すとき、同じやり方（お試し／本番）で行う
    return res


def run_autocall(supabase, gc, cfg, job: dict) -> dict:
    """①更新 →（②GAS）→（③確認）→ ④ブルービーン投入（`auto_call`）→（⑤SF投入・`auto_push`）。"""
    steps = _Steps()
    tabs = job.get("refresh_tabs", []) or []
    sheet_url = str(job.get("sheet_url", "") or "").strip()
    if tabs:
        if not sheet_url:
            steps.add("① シートの更新", "🛑", "スプレッドシートのURLが未設定です")
            return steps.result()
        urls = sms_runner.tab_urls_for(sheet_url, tabs, tab_gids(gc, sheet_url))
        folder = sms_runner.pattern_dir(job.get("name", ""), sms_runner.AUTOCALL_ROOT)
        ok, log = sms_runner.run_sheet_refresh(job.get("refresh_robot") or DEFAULT_REFRESH_ROBOT,
                                               folder, tabs=tabs, tab_urls=urls, url=sheet_url)
        # ⚠️ 更新でつまずいたまま入れると、古いリストを入れて同じお客様に二度かける。止める。
        if steps.add("① シートの更新", "✅" if ok else "🛑",
                     f"{len(tabs)}枚" if ok else sms_runner.stop_reason(log) or log[-300:]) == "🛑":
            return steps.result()
    gas_url = str(job.get("gas_url", "") or "").strip()
    if gas_url and str(job.get("gas_build", "") or "").strip():
        gok, gdata = sms_runner.run_gas_action(gas_url, str(job.get("gas_token", "") or ""),
                                               action="build", timeout=900,
                                               build=str(job.get("gas_build", "") or ""))
        if steps.add("② シートの作り直し", "✅" if gok else "🛑",
                     "作り直しました" if gok else str(gdata)[:500]) == "🛑":
            return steps.result()
    if not _watch_step(steps, gc, job):
        return steps.result()
    calls = job.get("autocalls", []) or []
    if calls:
        if not job.get("auto_call"):
            steps.add("④ ブルービーンへ投入", "⏸",
                      "設定で「投入まで自動で行う」がOFFなので、投入の手前で止めました")
            return steps.result()
        res = autocall_many(supabase, cfg, job, calls, submit=True)
        ng = [r for r in res if not r.get("ok")]
        # 📭 0件のシートは「投入なし」とはっきり書く（通知だけ見て「入れた」と思わないように）
        body = "／".join(f"{r['シート']}：" + ("📭 0件のため投入なし" if r.get("投入なし") else
                                             ((f"前のファイル{r['消した前のファイル']}件を消して" if r.get("消した前のファイル") else "")
                                              + (f"{r['件数']}件" if r.get("ok") else f"止まりました（{r.get('理由', '')}）")
                                              + str(r.get("重なり", "") or "")))
                        for r in res)
        if steps.add("④ ブルービーンへ投入", "🛑" if ng else "✅", body) == "🛑":
            return steps.result()
    loads = job.get("loads", []) or []
    if loads:
        if not job.get("auto_push"):
            steps.add("⑤ Salesforceへ投入", "⏸", "設定で自動投入がOFFなので、投入していません")
            return steps.result()
        out = _push_rows(gc, sheet_url, loads)
        steps.add("⑤ Salesforceへ投入", "✅" if all(_push_ok(r) for r in out) else "🛑",
                  "／".join(f"{r['シート']}：{r['結果']}" for r in out))
    return steps.result()


# ==========================================
# 📱 SMS送信
# ==========================================
SMS_CSV_SOURCES = ["GASのURLを叩いて受け取る（推奨）",
                   "GASがDriveに書き出したものを使う",
                   "アプリがシートから作る"]


def sms_csv_sheets(pat: dict):
    """CSVにするシートの一覧。

    はじめは1つしか持てなかった（`gas_sheet`）。増やしたあとも、
    前に登録したパターンがそのまま動くように、両方を読む。
    """
    out = [str(x).strip() for x in (pat.get("gas_sheets") or []) if str(x).strip()]
    if not out and str(pat.get("gas_sheet", "") or "").strip():
        out = [str(pat["gas_sheet"]).strip()]
    return out


def sms_gas_build_of(pat: dict, src: str) -> str:
    """②の確認より前に走らせる「作成」の処理名を返す（無ければ空）。

    ⭐ 作成が走る前のシートを人が見ても、映るのは**前回の中身**なので、
       確認したことにならない。だからCSVを作るときではなく、確認の前に走らせる。
    """
    if src != SMS_CSV_SOURCES[0]:
        return ""
    if not str(pat.get("gas_url", "") or "").strip():
        return ""
    return str(pat.get("gas_build", "") or "").strip()


def sms_gas_done(state, pname: str) -> str:
    """このパターンで、作成をもう走らせたか（走らせた時刻の文字。まだなら空）。"""
    return str(state.get(f"sms_gasb_{pname}", "") or "")


def sms_run_gas_build(state, pat: dict, pname: str, src: str):
    """GASの「作成」を走らせて、シートを作り直す。

    ⚠️ 走らせると、**②で人が直したセルも作り直しで消える**。
       だから確認より後では走らせない。走らせたことを覚えておき、③では走らせ直さない。
    戻り値：(うまくいったか, 画面に出す文言)
    """
    build = sms_gas_build_of(pat, src)
    ok, data = sms_runner.run_gas_action(pat["gas_url"], pat.get("gas_token", ""),
                                         action="build", timeout=900, build=build)
    if not ok:
        # ⚠️ ここで短く切らないこと。GASからの返事には**直し方**まで書いてあるのに、
        #    途中で切れて「ui.alert(...) を if」で終わり、何をすればよいか分からなかった。
        msg = str(data)
    else:
        state[f"sms_gasb_{pname}"] = time.strftime("%Y/%m/%d %H:%M")
        cnt = (data or {}).get("件数") or {}
        body = "／".join(f"{k}：{v}件" for k, v in cnt.items() if v != -1)
        msg = f"「{build}」を走らせました" + (f"（{body}）" if body else "")
    state[f"sms_gasres_{pname}"] = {"ok": bool(ok), "msg": msg}
    return bool(ok), msg


def sms_prepare_csv(state, pat: dict, pname: str, src: str, enc: str, gc, sheet: str = "",
                    sa_json: str = ""):
    """CSVを用意する。うまくいかなければ例外を投げる。

    ボタンからも「ぜんぶ実行」からも時間指定からも、**同じここを通る**。
    別々に書くと、片方だけ直して食い違うため。
    戻り値：[(st の関数名, 文言), ...]（画面に出す言葉）
    """
    msgs = []
    # 📄 CSVはシートごとに分けて置く（同じ名前だと、先に作ったほうが消える）
    slot = sms_runner.sheet_slot(pname, sheet)
    if src == SMS_CSV_SOURCES[0]:
        if not str(pat.get("gas_url", "")).strip():
            raise RuntimeError("GASのウェブアプリURLが未設定です（設定画面の4️⃣）。")
        # ⚠️ 作成（build）は ①-2 で済ませてある。ここで走らせ直すと、
        #    **②で人が直したセルを消してしまう**（直した意味がなくなる）。
        _b = "" if sms_gas_done(state, pname) else str(pat.get("gas_build", "") or "")
        _p, gname, grows, extra = sms_runner.fetch_from_gas(
            pat["gas_url"], pat.get("gas_token", ""), sheet, slot,
            keep_drive=bool(pat.get("gas_keep_drive", True)), build=_b)
        msgs.append(("success", f"✅ GASから受け取りました：`{gname}`（{grows}件）"))
        dmsg = str((extra or {}).get("drive", "") or "")
        if dmsg:
            lv = "warning" if ("残せません" in dmsg or "失敗" in dmsg) else "caption"
            msgs.append((lv, f"📁 Driveの控え：{dmsg}"))
    elif src == SMS_CSV_SOURCES[1]:
        if not sa_json:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        _p, dname, _h = sms_runner.fetch_from_drive(
            sa_json, pat.get("drive_root", ""), pat.get("drive_label", ""), slot)
        msgs.append(("success", f"✅ Driveから受け取りました：`{dname}`"))
    else:
        _p, n, _h = sms_runner.export_csv(gc, pat["sheet_url"],
                                          sheet or pat.get("csv_tab", ""), slot, enc,
                                          pat.get("skip_empty_col", ""))
        msgs.append(("success", f"✅ {n}件のCSVを作りました。"))
    return msgs


def sms_run_all(state, pat: dict, pname: str, gc, src: str, enc: str, do_push: bool,
                stop_before_send: bool = False, resume: bool = False, sa_json: str = ""):
    """①更新 → ②チェック → ③CSV → ④一括送信 を通しで行う。

    state：途中の結果を置いておく入れ物（画面からは st.session_state、時間指定からは dict）。
    ⚠️ 送ったSMSは取り消せない。だから **どこか1つでも駄目なら、そこで止める**。
       止まったときは「送っていない」で終わるようにする（送ってから気づいても遅い）。
    戻り値：[{"工程","結果","中身"}, ...]
    """
    import sf_ui
    steps = []
    state.pop(f"sms_all_drop_{pname}", None)   # 前回の分を持ち越さない
    if not resume:
        # 作り直しは、この実行の中で1回だけ（③で走らせ直さないための目印）
        state.pop(f"sms_gasb_{pname}", None)

    def _add(name, ok, body, mark=""):
        # 「送るものが無い」は失敗ではない。赤で止めると、直すところを探させてしまう。
        steps.append({"工程": name, "結果": (mark or ("✅" if ok else "🛑")), "中身": body})
        return ok

    # --- ① シートを更新 ---
    #     ⚠️ 確認の小窓でOKを押したあとは「続き」から進める。
    #        ここでやり直すと、数分かかる更新をもう一度待たされる。
    tabs = pat.get("refresh_tabs", []) or []
    _prev_ref = state.get(f"sms_ref_{pname}")
    if resume and _prev_ref and _prev_ref.get("ok"):
        _add("① シートの更新", True, "さきほど更新できているので、やり直しません")
    elif pat.get("refresh_robot") and tabs:
        urls = sms_runner.tab_urls_for(pat["sheet_url"], tabs, tab_gids(gc, pat.get("sheet_url", "")))
        ok, log = sms_runner.run_refresh_robot(pat["refresh_robot"], pname,
                                               tabs=tabs, tab_urls=urls, url=pat["sheet_url"])
        state[f"sms_ref_{pname}"] = {
            "ok": ok, "log": log, "表": sms_runner.parse_refresh_log(log, tabs)}
        if not _add("① シートの更新", ok, f"{len(tabs)}枚"
                    if ok else "途中で止まりました（下の1️⃣にログがあります）"):
            return steps
    else:
        _add("① シートの更新", True, "この設定では行いません（手作業）")

    # --- ①-2 GASでシートを作り直す ---
    #     ⭐ **②で人が中身を見る前に**走らせる。作成が走る前のシートを見ても、
    #        映るのは前回の中身なので、確認したことにならない。
    #     ⚠️ 続き（resume）のときはやり直さない。人が直したセルを消してしまうため。
    if sms_gas_build_of(pat, src):
        if resume and sms_gas_done(state, pname):
            _add("①-2 シートの作り直し", True, "さきほど作り直しているので、やり直しません")
        else:
            _gok, _gmsg = sms_run_gas_build(state, pat, pname, src)
            # 表のセルは短く。全文は表の下に出す（長い文は表の中では読めないため）
            _short = _gmsg if len(_gmsg) <= 120 else _gmsg[:120] + "…（下に全文）"
            if not _add("①-2 シートの作り直し", _gok, _short):
                return steps

    # --- ② 中身の確認 ---
    _rules = pat.get("checks", []) or []
    if _rules:
        try:
            findings, notes = sms_runner.check_rules(gc, pat["sheet_url"], _rules)
        except Exception as e:
            _add("② 中身の確認", False, f"シートを読めませんでした：{e}")
            return steps
        state[f"sms_find_{pname}"] = {"findings": findings, "notes": notes}
        if findings:
            _add("② 中身の確認", False,
                 f"ルールに引っかかった行が {len(findings)}件 あります。**送信せずに止めました**")
            return steps

    # 👀 目で見て確認するシートがあるなら、**人がOKを出すまで進めない**。
    #    ルール化できないものを、機械に判断させないための工程。
    _watch = pat.get("check_tabs", []) or []
    if _watch and not state.get(f"sms_ok_{pname}"):
        _add("② 中身の確認", False,
             f"「{'／'.join(_watch)}」を目で見て確認してください（下の 2️⃣ でOKを出せます）",
             mark="⏸")
        return steps
    _add("② 中身の確認", True,
         ("確認済み" if _watch else "確認するシートは登録されていません"))

    # --- ③④ シートのぶん繰り返す（1シート＝1回の送信） ---
    #     ⚠️「すでに送った宛先」の記録は**パターンでまとめて**見る。
    #        シートごとに分けると、同じ人に両方の文面が届いてしまう。
    _sheets = sms_csv_sheets(pat) or [""]
    days = int(pat.get("dedup_days", 0) or 0)
    for _sh in _sheets:
        _tag = f"（{_sh}）" if _sh else ""
        _slot = sms_runner.sheet_slot(pname, _sh)
        try:
            msgs = sms_prepare_csv(state, pat, pname, src, enc, gc, _sh, sa_json=sa_json)
        except Exception as e:
            _add(f"③ CSVの用意{_tag}", False, str(e)[:300])
            return steps
        _add(f"③ CSVの用意{_tag}", True, "／".join(t for _l, t in msgs if _l == "success"))

        got = sms_runner.today_csv(_slot)
        if not got:
            _add(f"④ 一括送信{_tag}", False, "今日のCSVが見つかりません")
            return steps

        _before = sms_runner.csv_dest_keys(got, enc)
        dup = sms_runner.find_already_sent(pname, _before, days)
        if dup:
            n_drop, n_left = sms_runner.drop_already_sent(_slot, enc, days, sent_pattern=pname)
            _add(f"　 二重送信の除外{_tag}", True,
                 f"すでに送った {n_drop}件を外しました（残り {n_left}件）")
            got = sms_runner.today_csv(_slot)
        keys = sms_runner.csv_dest_keys(got, enc)
        if not keys:
            _add(f"④ 一括送信{_tag}", True,
                 "📭 CSVが0件でした（送る相手がいません）" if not _before else
                 "送る宛先が0件でした（このCSVの分はすべて送信済み）", mark="⏹")
            continue

        # 🛑 送るSMSは取り消せないので、「送ります」の確認を取っていなければ、ここで止める。
        if stop_before_send:
            _add(f"④ 一括送信{_tag}", False,
                 f"{len(keys)}件を送る用意ができました。**送信の確認を入れてください**", mark="⏸")
            continue

        ok, log = sms_runner.run_send_robot(pat["send_robot"], _slot, got,
                                            allow_errors=bool(pat.get("allow_errors")))
        if ok:
            result, note = sms_runner.RESULT_SENT, ""
        elif sms_runner.submit_reached(log):
            result, note = sms_runner.RESULT_MAYBE, "送信の操作まで進んだあと、止まりました"
        else:
            result, note = sms_runner.RESULT_NOT, "送信の手前で止まりました"
        # 🚫 プッシュプロに弾かれた宛先は、送られていない。記録に入れない
        #    （入れてしまうと、直したあとに送り直せなくなる）
        _drop = sms_runner.dropped_dests(log)
        state[f"sms_all_drop_{pname}"] = (
            list(state.get(f"sms_all_drop_{pname}") or []) + _drop)
        _keys_sent = [(n, k) for n, k in keys if k not in _drop]
        sms_runner.record_sent(pname, _keys_sent, result, note)
        state[f"sms_sent_{pname}"] = {"ok": ok, "log": log,
                                      "result": result, "n": len(keys)}
        _why = sms_runner.stop_reason(log) if not ok else ""
        _extra = (f"／弾かれて送られなかった {len(_drop)}件" if _drop else "")
        if not _add(f"④ 一括送信{_tag}", ok,
                    f"{len(_keys_sent)}件：{result}" + _extra + (f"／{_why}" if _why else "")):
            return steps

    # --- ⑤ Salesforceへ投入（頼まれたときだけ） ---
    #     ⚠️ 送信の手前で止めた（⏸）ときは投入しない。送っていない人を「送信済み」にしないため。
    if do_push and not any(s["結果"] == "⏸" for s in steps):
        out = []
        for ld in (pat.get("loads", []) or []):
            r = sf_ui.push_sheet(gc, pat["sheet_url"], str(ld.get("シート", "")),
                                 str(ld.get("オブジェクト", "")), str(ld.get("照合キー", "")),
                                 ld.get("マッピング", {}) or {}, limit=0)
            out.append({"シート": str(ld.get("シート", "")), "結果": r["結果"],
                        "成功": r["ok"], "失敗": r["ng"],
                        "投入なし": bool(r.get("投入なし")),
                        "_errors": r["errors"], "_obj": r["オブジェクト"]})
        state[f"sms_push_{pname}"] = out
        # 📭 0件（投入なし）は「通った」。⚠️ 失敗件数だけを見ていたので、
        #    「シートを読めません」のように1件も送れなかったときが ✅ になっていた。
        _add("⑤ Salesforceへ投入", all(_push_ok(r) for r in out),
             "／".join(f"{r['シート']}：{r['結果']}" for r in out) or "投入の設定がありません")
    return steps


def run_sms(gc, pat: dict, sa_json: str = "") -> dict:
    """時間指定用：送信は `auto_send`、投入は `auto_load` がONのときだけ。目で見る確認があれば ⏸。"""
    enc = pat.get("csv_encoding", "Shift_JIS")
    src = pat.get("csv_source", SMS_CSV_SOURCES[0])
    if src not in SMS_CSV_SOURCES:
        src = SMS_CSV_SOURCES[0]          # 廃止した選択肢は、推奨のやり方として扱う
    rows = sms_run_all({}, pat, str(pat.get("name", "")), gc, src, enc,
                       do_push=bool(pat.get("auto_load")) and bool(pat.get("loads")),
                       stop_before_send=not pat.get("auto_send"), sa_json=sa_json)
    steps = _Steps()
    steps.rows = rows
    return steps.result()


# ==========================================
# 🔄 SFレポート更新
# ==========================================
def run_reports(gc, one_set: dict) -> dict:
    """セットの全レポートを、ブラウザ1回で上から順に更新する（中身は report_refresh._run）。"""
    import report_refresh
    steps = _Steps()
    rows = report_refresh._rows_of(one_set)
    if not rows:
        steps.add("更新", "⏹", "更新するシートが登録されていません")
        return steps.result()
    r = report_refresh._run(gc, one_set, rows)
    bad = [t for t in r["表"] if not str(t.get("結果", "")).startswith("✅")]
    body = f"{len(rows)}枚" if r["ok"] and not bad else \
        "／".join(f"{t['シート']}：{t['結果']}" for t in bad) or sms_runner.stop_reason(r["log"])
    steps.add("レポートの更新", "✅" if r["ok"] and not bad else "🛑", body)
    return steps.result()


# ==========================================
# 🚀 進捗反映
# ==========================================
PROGRESS_CONFIG_TAB = "取り込み設定"


def progress_order_num(v) -> float:
    """「順番」欄を数として読む。空や文字は 9999（＝いちばん後ろ）にする。
    全角の「１」で書かれても読めるように NFKC で正規化してから見る。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(v or "")).strip()
    try:
        return float(s)
    except Exception:
        return 9999.0


def progress_keep_files(cfg) -> int:
    """1キャリアあたり、手元に残しておくファイル数（古い分は自動で消す）。"""
    try:
        return max(1, int(str(cfg.get("keep_generations", 1) or 1)))
    except Exception:
        return 1


def progress_secrets_map(supabase, cfg) -> dict:
    """🔑 パスワード付きファイル用に、登録済みの鍵を復号しておく（進捗設定＋司令室の分）。"""
    out = {}
    try:
        import robot as _rb  # 復号処理を使い回す
        if cfg.get("secrets"):
            out.update(_rb.decrypt_secrets(cfg["secrets"]))
        for _p in (supabase.table("merchants").select("config_json").execute().data or []):
            _enc = ((_p.get("config_json") or {}).get("robot_config", {}) or {}).get("secrets", {})
            if _enc:
                out.update(_rb.decrypt_secrets(_enc))
    except Exception:
        pass
    return out


def progress_archive_download(cfg, carrier: str, path: str, sa_json: str, say=print):
    """サイトから落としたファイルを、Driveの保管フォルダにも置く（任意）。

    保管に失敗しても取り込み自体は続けたいので、ここでは知らせるだけにする。
    """
    import intake_runner
    if not (path and cfg.get("archive_downloads") and cfg.get("intake_folder_id")):
        return
    try:
        msg = intake_runner.archive_to_drive(
            sa_json, cfg["intake_folder_id"], str(carrier).strip() or "その他", path,
            keep=progress_keep_files(cfg))
        say(f"☁️ {msg}")
    except Exception as e:
        say(f"（Driveへの保管はできませんでした。取り込みは続けられます：）{str(e)[:180]}")


def progress_intake_one(gc, drive, cfg, m: dict, secrets_map: dict, hist: dict,
                        sa_json: str = "", manual_file=None, say=print):
    """1キャリアぶん：ファイルを手に入れて、元データへ貼る。

    画面の「進捗をまとめて実行」と時間指定の両方が、ここを通る。
    manual_file：手動アップロードのキャリアに渡す (名前, 中身)。無ければ止める。
    戻り値：(結果の行, 取り込み不要のキャリアか)
    """
    import intake_runner
    name = m["キャリア名"]
    method = str(m.get("取り込み方法", "") or "メールの添付")
    local = None
    if method.startswith("取り込み不要"):
        # IMPORTRANGE等で元データが自動更新されるキャリア。貼り付ける物は無いが、投入は行う。
        return ({"キャリア": name, "ファイル": "（取り込み不要）", "件数": 0,
                 "結果": "⏭ 取り込みは不要です（元データは自動で入ります）"}, True)
    if method == "メールの添付" and cfg.get("gas_url") and not cfg.get("use_drive_intake"):
        mp, mmsg = intake_runner.fetch_mail_file(cfg["gas_url"], cfg.get("gas_token", ""),
                                                 str(name), keep=progress_keep_files(cfg))
        if not mp:
            return ({"キャリア": name, "ファイル": "", "件数": 0,
                     "結果": f"⚠️ メールから受け取れませんでした（{mmsg}）"}, False)
        with open(mp, "rb") as fh:
            local = (os.path.basename(mp), fh.read())
        say(f"📨 {name}：{mmsg}")
    if method.startswith("サイト"):
        # 🖥 ブラウザを開くので、このPCで実行する
        bot = str(m.get("取り込みロボット名", "") or "").strip()
        if not bot:
            return ({"キャリア": name, "ファイル": "", "件数": 0, "結果": "⚠️ 取り込みロボットが未設定"}, False)
        # 保存先はロボット名で決める（テスト実行・通しで試すと同じ場所にそろえる）
        d = intake_runner.intake_dir(name)
        try:
            ok, log, newest = intake_runner.run_download_robot(bot, d, keep=progress_keep_files(cfg))
        except Exception as e:
            ok, log, newest = False, str(e)[:300], None
        # 📌 ファイルが消えていることがあるので、開く前に確かめる
        #    （落ちてきた直後の掃除で消えた事故があった）
        if not (ok and newest and os.path.isfile(newest)):
            why = (f"❌ ダウンロードできませんでした（{log[-120:]}）" if not (ok and newest) else
                   "❌ 落ちてきたファイルが見つかりません（保存先から消えています）")
            return ({"キャリア": name, "ファイル": "", "件数": 0, "結果": why}, False)
        with open(newest, "rb") as fh:
            local = (os.path.basename(newest), fh.read())
        # メール添付と同じ保管フォルダにも残す（探す場所を1か所にする）
        progress_archive_download(cfg, name, newest, sa_json, say=say)
    elif method.startswith("手動"):
        if not manual_file:
            return ({"キャリア": name, "ファイル": "", "件数": 0,
                     "結果": "⚠️ ファイルが選ばれていません（下の欄で選んでください）"}, False)
        local = manual_file
    return (intake_runner.run_one(gc, drive, cfg.get("intake_folder_id", ""), m, secrets_map,
                                  backup=bool(cfg.get("make_backup", False)),
                                  local_file=local,
                                  last_file=hist.get(str(name).strip(), "")), False)


def progress_members(gc, cfg) -> dict:
    """有効なキャリアを、貼り付け先スプシごとに「順番」どおり並べて返す。{スプシID: [行, ...]}"""
    sh = gc.open_by_url(cfg["settings_url"])
    values = sh.worksheet(PROGRESS_CONFIG_TAB).get_all_values()
    if not values:
        return {}
    heads = values[0]
    rows = [dict(zip(heads, (r + [""] * len(heads))[:len(heads)])) for r in values[1:]]
    # ⚠️ 同じ番号・空のときは、いまのシートの並びをそのまま保つ（sorted は stable）
    rows = sorted(rows, key=lambda r: progress_order_num(r.get("順番", "")))
    groups = {}
    for r in rows:
        if str(r.get("有効", "")).upper() == "FALSE":
            continue
        sid = str(r.get("貼り付け先スプシID", "")).strip()
        if sid and str(r.get("キャリア名", "")).strip():
            groups.setdefault(sid, []).append(r)
    return groups


def run_progress(supabase, gc, cfg: dict, sa_json: str = "") -> dict:
    """時間指定用：有効な全キャリアを「反映して投入」（投入は進捗設定の push_salesforce に従う）。

    ⚠️ 手動アップロードのキャリアは人がファイルを選ぶので、時間指定では取り込まない（止めて知らせる）。
    """
    import intake_runner
    import sf_ui
    steps = _Steps()
    if not (gc and cfg.get("settings_url")):
        steps.add("準備", "🛑", "進捗設定（設定スプシ）が未登録です")
        return steps.result()
    try:
        groups = progress_members(gc, cfg)
    except Exception as e:
        steps.add("準備", "🛑", f"取り込み設定を読めませんでした：{str(e)[:200]}")
        return steps.result()
    if not groups:
        steps.add("準備", "⏹", "有効なキャリアがありません")
        return steps.result()
    do_push = bool(cfg.get("push_salesforce", True))
    need_drive = cfg.get("use_drive_intake") and any(
        str(m.get("取り込み方法", "メールの添付")) == "メールの添付"
        for ms in groups.values() for m in ms)
    drive = None
    try:
        drive = intake_runner.drive_client(sa_json)
    except Exception as e:
        if need_drive:
            steps.add("準備", "🛑", f"Driveに接続できません: {e}")
            return steps.result()
    if need_drive and cfg.get("gas_url"):
        gok, gmsg = intake_runner.call_gas(cfg["gas_url"], cfg.get("gas_token", ""), "intake")
        steps.add("📨 メールの取り込み", "✅" if gok else "⏹",
                  "終わりました" if gok else f"呼べませんでした（{gmsg}）。すでにDriveにあるファイルで続けます")
    secrets_map = progress_secrets_map(supabase, cfg)
    hist = intake_runner.read_history(gc, cfg["settings_url"])
    for sid, members in groups.items():
        results, no_intake = [], []
        for m in members:
            if str(m.get("取り込み方法", "")).startswith("手動"):
                results.append({"キャリア": m["キャリア名"], "ファイル": "", "件数": 0,
                                "結果": "⏸ 手動アップロードのキャリアは、時間指定では取り込めません"})
                continue
            r, skip = progress_intake_one(gc, drive, cfg, m, secrets_map, hist, sa_json=sa_json,
                                          say=lambda s: None)
            results.append(r)
            if skip:
                no_intake.append(str(m["キャリア名"]))
        done = [r for r in results if str(r["結果"]).startswith("✅")]
        try:
            intake_runner.write_history(gc, cfg["settings_url"], done)
        except Exception:
            pass
        for r in results:
            res = str(r["結果"])
            mark = ("✅" if res.startswith("✅") else "⏹" if res.startswith("⏭")
                    else "⏸" if res.startswith("⏸") else "🛑")
            steps.add(f"反映：{r['キャリア']}", mark,
                      (f"{r['件数']}件" if mark == "✅" else res))
        if not do_push:
            continue
        # ⚠️ 投入は「設定した順番」で、貼れたキャリア＋取り込み不要のキャリアだけ
        #    （貼れていないのに投入すると、古い内容を入れてしまうため）。
        ok_names = {str(r["キャリア"]) for r in done} | set(no_intake)
        for m in members:
            cname = str(m["キャリア名"])
            tab = str(m.get("投入用シート名", "") or "").strip()
            if cname not in ok_names or not tab:
                continue
            obj = str(m.get("オブジェクトAPI名", "") or "").strip()
            pr = sf_ui.push_carrier(gc, cfg["settings_url"], cname, sid, tab, obj,
                                    str(m.get("外部IDキー", "") or "").strip())
            if pr.get("errors"):
                try:
                    intake_runner.save_errors(cname, obj, pr["errors"])
                except Exception:
                    pass
            steps.add(f"投入：{cname}", ("⏹" if pr.get("投入なし") else
                                         "✅" if str(pr["結果"]).startswith("✅") else "🛑"), pr["結果"])
    return steps.result()


# ==========================================
# ▶ まとめて呼ぶ入口（scheduler.py から）
# ==========================================
def run(kind: str, target: str, secrets: dict = None) -> dict:
    """種類と対象の名前で実行する。見つからない・設定が読めないときは「失敗」で返す（例外は出さない）。"""
    s = secrets or load_secrets()
    try:
        sb = supabase_client(s)
        sa = s.get("GOOGLE_SERVICE_ACCOUNT_JSON", "") or ""
        gc = gspread_client(sa)
        cfg = load_row(sb, SETTINGS_IDS[kind])
        if kind == "progress":
            return run_progress(sb, gc, cfg, sa_json=sa)
        key = {"sms": "patterns", "dataloader": "jobs", "autocall": "jobs", "reports": "sets"}[kind]
        one = next((x for x in (cfg.get(key) or []) if str(x.get("name", "")) == target), None)
        if not one:
            return {"結果": "失敗", "工程": [{"工程": "準備", "結果": "🛑",
                                              "中身": f"「{target}」が見つかりません（名前を変えた・消した？）"}]}
        if kind in ("sms", "dataloader", "autocall") and not gc:
            return {"結果": "失敗", "工程": [{"工程": "準備", "結果": "🛑",
                                              "中身": "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です"}]}
        if kind == "sms":
            return run_sms(gc, one, sa_json=sa)
        if kind == "dataloader":
            return run_dataloader(sb, gc, one)
        if kind == "autocall":
            return run_autocall(sb, gc, cfg, one)
        return run_reports(gc, one)
    except Exception as e:
        import traceback
        return {"結果": "失敗", "工程": [{"工程": "実行", "結果": "🛑",
                                          "中身": f"思わぬエラーで止まりました：{str(e)[:300]}",
                                          "詳細": traceback.format_exc()[-2000:]}]}
