"""
📞 オートコール投入（ブルービーン）

データローダー自動化とは**別の業務**。作業の道具がちょこちょこ重なるだけなので、
画面は分けてある。ただし**中身の部品は共有**する（同じ画面を2か所に書かない）。

  ① 登録したシートを、SFコネクタで更新する
  ② （任意）スプシのGASでシートを作り直す      ← トス表作成だけ使う
  ③ （任意）目で見て確認するシート              ← 出たら消し込む
  ④ シートごとにCSVを作って、ブルービーンへ投入
  ⑤ （任意）Salesforceへ投入                    ← トス表作成だけ使う

【CSVは作り直さない】
電話番号の頭の0や文字コードの整形は、スプシのGASが持っている。
同じ整形をアプリに書くと片方だけ直して食い違うので、
**GASの `action=csv&sheet=…` を叩いて、できあがったCSVを受け取る**だけにする。

【タイトルとプルダウンは、シートごとに登録して差し込む】
録画は仮の値でよい。実行時に `--var 名前=値` で差し替える（共通ロボットと同じ考え方）。
だからシートが増えても録画し直さない。
"""
import json
import re
import time

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import characters as ch
import common_robots
import gas_deploy
import sf_ui
import sms_runner
import theme
import watch_ui

st.set_page_config(page_title="オートコール投入 - エンカンAI", layout="wide")

theme.inject_theme()

# 🔑 接続キーのファイルが壊れていたら、直す場所を名指しして止める。
import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📞", "オートコール投入",
                  "シートを更新して、シートごとにCSVを作り、ブルービーンへ投入します。",
                  color=c["color"])


# ==========================================
# 🔌 つなぎこみ
# ==========================================
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

SETTINGS_ID = "__autocall__"
WORK_ROOT = sms_runner.AUTOCALL_ROOT          # 取り込みファイル/オートコール投入/<ジョブ／シート>
DEFAULT_REFRESH_ROBOT = "共通_SFコネクタ更新"
DEFAULT_CALL_ROBOT = common_robots.ROLES["autocall"]["name"]
DEFAULT_VARS = ["タイトル"]                    # 手順書の値に {タイトル} と書いておく


def _load() -> dict:
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}


def _save(cfg: dict):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（オートコール投入の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cfg}).execute()


@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)


def _get_gspread_client():
    try:
        sa_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        sa_json = ""
    if not sa_json:
        return None
    try:
        return _build_gspread_client(sa_json)
    except Exception:
        return None


@st.cache_data(ttl=120, show_spinner=False)
def _tabs_of(_gc, sheet_url: str):
    sh = _gc.open_by_url(sheet_url) if sheet_url.startswith("http") else _gc.open_by_key(sheet_url)
    return [w.title for w in sh.worksheets()]


@st.cache_data(ttl=120, show_spinner=False)
def _tab_gids(_gc, sheet_url: str) -> dict:
    sh = _gc.open_by_url(sheet_url) if sheet_url.startswith("http") else _gc.open_by_key(sheet_url)
    return {w.title: w.id for w in sh.worksheets()}


def _robots():
    """共通ロボットの名前を集める（その他設定で録画したもの）。"""
    try:
        rows = supabase.table("merchants").select("id,config_json").execute().data or []
    except Exception:
        return []
    return sorted(r["id"] for r in rows
                  if not str(r["id"]).startswith("__")
                  and str((r.get("config_json") or {}).get("product_type", "")) == "SMS送信")


def _jobs(cfg):
    return cfg.get("jobs", []) or []


def _find(cfg, name):
    for j in _jobs(cfg):
        if j.get("name") == name:
            return j
    return None


def _vars_of(job) -> list:
    """このジョブで差し込む名前の一覧（手順書の `{名前}` に対応）。"""
    names = [str(x).strip() for x in (job.get("vars") or DEFAULT_VARS) if str(x).strip()]
    return names or list(DEFAULT_VARS)


def _slot(job_name: str, sheet: str) -> str:
    """CSVの置き場所の名前。**シートごとに分ける**（同じ名前だと先のものが消える）。"""
    return sms_runner.sheet_slot(job_name, sheet)


# ==========================================
# 🎛 業務（プルダウン）はシートごとに選ぶ
# ==========================================
# 投入するものによって業務が変わる。選択肢はブルービーンから読み込んで**覚えておく**
# （cfg["bluebean_options"]["業務"]）。新しい業務が増えたら読み込み直すと足される。
# 作業グループ（ACD）は、業務を選ぶと1つだけ出てくるので、ロボットがそれを選ぶ
# （手順書の値＝『出てきた1つを選ぶ』。robot.py の ONLY_OPTION_WORDS）。
GYOMU = "業務"
ACD = "作業グループ"
ONLY_ONE = "出てきた1つを選ぶ"
OPTIONS_KEY = "bluebean_options"


def _select_step(steps, word):
    """対象に word を含む『選択』の手順の位置（無ければ None）。"""
    for i, s in enumerate(steps or []):
        if str(s.get("操作", "")) in ("選択", "select") and word in str(s.get("対象", "")):
            return i
    return None


def _gyomu_options(cfg) -> list:
    return ((cfg.get(OPTIONS_KEY) or {}).get(GYOMU) or {}).get("options", []) or []


def _gyomu_value(cfg, label: str) -> str:
    """表で選んだ名前 → ブルービーンに渡す値。覚えていなければ名前のまま渡す（名前でも選べる）。"""
    for o in _gyomu_options(cfg):
        if o.get("label") == label:
            return str(o.get("value", ""))
    return label


def _merge_options(old, new):
    """読み込んだ選択肢を、覚えているものに足す。**前に覚えたものは消さない。**"""
    by = {str(o.get("value", "")): dict(o) for o in (old or [])}
    added = []
    for o in new or []:
        v = str(o.get("value", ""))
        if v not in by:
            added.append(o.get("label", v))
        by[v] = {"value": v, "label": o.get("label", v)}
    return list(by.values()), added


def _step_vars(steps) -> list:
    """手順書の値・対象に書いてある {名前} のうち、カードで入れるもの（業務・秘密・ファイルを除く）。"""
    out = []
    for s in steps or []:
        for m in re.findall(r"\{(.+?)\}", str(s.get("値", "")) + str(s.get("対象", ""))):
            if m.startswith("秘密:") or m in (GYOMU, "アップロードファイル", "CSVファイル") or m in out:
                continue
            out.append(m)
    return out


def _set_select_value(step, value: str):
    step["値"] = value
    code = str(step.get("ai_code", "") or "")
    if ".select_option(" in code:
        step["ai_code"] = re.sub(r"\.select_option\(.*\)\s*$", f'.select_option("{value}")', code)


cfg = _load()
gc = _get_gspread_client()

st.session_state.setdefault("ac_view", "list")
st.session_state.setdefault("ac_job", "")

if not gc:
    st.warning("🔑 接続キー **GOOGLE_SERVICE_ACCOUNT_JSON** が未設定です（管理者に設定を依頼してください）。")

if DEFAULT_CALL_ROBOT not in _robots():
    st.warning(f"🤖 まだ **{DEFAULT_CALL_ROBOT}** を録画していません。"
               "「⚙️ その他設定」の🤖共通ロボットの登録から、1台だけ録画してください"
               "（シートが増えても録り直しは要りません）。")


# ==========================================
# 🔧 工程（実行の中身）
# ==========================================
def _do_refresh(job, tabs):
    """① SFコネクタでシートを更新する。ブラウザは1回だけ開いて回す。"""
    sheet_url = str(job.get("sheet_url", "") or "").strip()
    if not sheet_url:
        return False, "スプレッドシートのURLが未設定なので、更新しませんでした（設定画面の1️⃣）。", None
    gids = {}
    try:
        gids = _tab_gids(gc, sheet_url) if gc else {}
    except Exception:
        gids = {}
    # ⚠️ 開く先は**必ずこのジョブのスプシ**を渡す（SMS送信・データローダーと同じ tab_urls_for）。
    #    以前は gid が取れないと URL を渡さず、ロボットが**録画したときのスプシ（FPR送信）**を開いて
    #    そちらのSFコネクタを更新してしまった（実際に起きた）。
    #    gid が分からないシートもスプシのURLで開き、ロボットがシート名を確かめて違えば止まる。
    urls = sms_runner.tab_urls_for(sheet_url, tabs, gids)
    folder = sms_runner.pattern_dir(job.get("name", ""), WORK_ROOT)
    ok, log = sms_runner.run_sheet_refresh(
        job.get("refresh_robot") or DEFAULT_REFRESH_ROBOT, folder,
        tabs=tabs, tab_urls=urls, url=sheet_url)
    return ok, log, sms_runner.refresh_results(log, len(tabs))


def _do_gas(job):
    """② スプシのGASを呼んで、シートを作り直す（登録があるときだけ）。"""
    url = str(job.get("gas_url", "") or "").strip()
    if not url:
        return None, "GASのURLが未設定なので、この工程は行いません。"
    return sms_runner.run_gas_action(url, str(job.get("gas_token", "") or ""),
                                     action="build", timeout=900,
                                     build=str(job.get("gas_build", "") or ""))


def _do_watch(job):
    return watch_ui.read(gc, job["sheet_url"], job.get("watch_tabs", []) or [])


def _make_csv(job, entry):
    """④の前半：そのシートのCSVをGASから受け取る。

    ⚠️ アプリで作り直さない。整形はGASが持っているので、できあがりを受け取るだけ。
    """
    sheet = str(entry.get("シート", "") or "").strip()
    url = str(job.get("gas_url", "") or "").strip()
    if not url:
        raise RuntimeError("GASのURLが未設定です（設定画面の3️⃣で登録してください）。")
    path, name, rows, _raw = sms_runner.fetch_from_gas(
        url, str(job.get("gas_token", "") or ""), sheet,
        _slot(job.get("name", ""), sheet), keep_drive=False, build="",
        root=WORK_ROOT)
    # 📛 ブルービーンの一覧に出るファイル名は、渡したファイルの名前そのもの。
    #    「送信データ.csv」のままだと、どれが何の投入か一覧で見分けられない。
    #    **シート名＋日時**で渡す（前回の分はこのフォルダから消す。控えは「履歴」にある）。
    import glob
    import os
    import shutil
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


def _do_autocall(job, entry, submit: bool, delete_ids=None):
    """④の後半：CSVを渡して、ブルービーンへ投入する。

    delete_ids … 「消して入れ直す」で人が選んだ、先に消すファイルのID。
                 ロボットは消し終わってから、同じブラウザのまま投入へ進む。
    """
    sheet = str(entry.get("シート", "") or "").strip()
    robot_name = job.get("call_robot") or DEFAULT_CALL_ROBOT
    variables = {v: str(entry.get(v, "") or "") for v in _vars_of(job)}
    variables["削除モード"] = "削除" if delete_ids else ""
    variables["削除するID"] = ",".join(str(x) for x in (delete_ids or []))
    _row, _steps = common_robots.robot_row(supabase, robot_name)
    _gi = _select_step(_steps, GYOMU)
    if _gi is not None and "{" + GYOMU + "}" in str(_steps[_gi].get("値", "")):
        _label = str(entry.get(GYOMU, "") or "").strip()
        if not _label:
            # 空のまま動かすと、違う業務（録画のときのもの）に投入しかねない
            raise RuntimeError(f"「{sheet}」の業務が選ばれていません（設定画面の5️⃣で選んでください）。")
        variables[GYOMU] = _gyomu_value(cfg, _label)
    path, name, rows = _make_csv(job, entry)
    ok, log = sms_runner.run_autocall_robot(
        robot_name,
        _slot(job.get("name", ""), sheet), path, variables=variables,
        submit=submit)
    # 止まった理由（例：無効なデータが 3件）を表にそのまま出す。ログを開かなくても分かるように
    _why = [l.split("エラー:", 1)[1].strip() for l in str(log).splitlines() if "❌ エラー:" in l]
    return {"シート": sheet, "ok": ok, "log": log, "CSV": name, "件数": rows,
            "投入まで進んだ": sms_runner.submit_reached(log), "理由": _why[-1] if _why else ""}


@st.dialog("🔁 消して入れ直す：消すファイルを選んでください", width="large")
def _redo_dialog(job, calls, jname):
    """探した候補を出して、カードごとに消すものを人に選ばせる。選んだものだけ消して投入する。

    ⭐ 回し切ったリスト（発信待ち0・自動再架電0・作業保存済＝全件数）は最初からチェック。
       まだかけられるお客様がいるリストは、チェックを外した状態で ⚠️ を付ける
       （回し切る前に新しいリストに変えることもあるので、止めずに人に決めさせる）。
    """
    found = st.session_state.get(f"ac_redo_{jname}") or []
    plan = []
    for f in found:
        e = calls[f["i"]]
        sheet = str(e.get("シート", "") or "")
        st.markdown(f"#### 📞 {sheet}（{e.get(GYOMU, '')}）")
        if not f["ok"]:
            st.error("前のファイルを探せませんでした。このシートは、消すのも入れるのもやめておきます。")
            with st.expander("ログ"):
                st.text(str(f["log"])[-3000:])
            plan.append((e, None, False))
            continue
        ids = []
        if not f["cands"]:
            st.caption("前に入れたファイルは見つかりませんでした（もう削除済み・まだ入れていない）。")
        for c in f["cands"]:
            L = c.get("発信リスト") or {}
            done = (not L) or bool(L.get("回し切り"))
            label = (f"ID {c.get('インポートID')}｜{c.get('ファイル名')}｜{c.get('インポート日時')}｜"
                     + (f"発信リスト {L.get('名称')}：全件数 {L.get('全件数')}・作業保存済 {L.get('作業保存済')}"
                        f"・発信待ち {L.get('発信待ち')}・自動再架電 {L.get('自動再架電')}" if L else "発信リストなし"))
            if not done:
                st.warning("⚠️ このリストは、まだかけられるお客様がいます。")
            if st.checkbox(label, value=done, key=f"ac_redo_{jname}_{f['i']}_{c.get('インポートID')}"):
                ids.append(str(c.get("インポートID")))
        go = st.checkbox("このシートを投入する", value=bool(ids) or not f["cands"],
                         key=f"ac_redo_go_{jname}_{f['i']}")
        if f["cands"] and not ids and go:
            st.warning("⚠️ 前のファイルを消さずに入れると、データが重なって**処理失敗**になることがあります。")
        plan.append((e, ids, go))
        st.divider()
    agree = st.checkbox("**選んだファイルを消してから、投入します**（消したものは戻せません）",
                        key=f"ac_redo_agree_{jname}")
    b1, b2 = st.columns(2)
    with b1:
        if st.button("🔁 実行する", type="primary", disabled=not agree, use_container_width=True):
            res = []
            for e, ids, go in plan:
                sheet = str(e.get("シート", "") or "")
                if ids is None or not go:
                    res.append({"シート": sheet, "ok": False, "log": "行いませんでした", "CSV": "", "件数": 0,
                                "投入まで進んだ": False, "理由": "消す・入れるをやめました"})
                    continue
                with st.spinner(f"「{sheet}」を消して入れ直しています..."):
                    try:
                        res.append(_do_autocall(job, e, submit=True, delete_ids=ids))
                    except Exception as ex:
                        res.append({"シート": sheet, "ok": False, "log": str(ex), "CSV": "", "件数": 0,
                                    "投入まで進んだ": False, "理由": str(ex)[:120]})
            st.session_state[f"ac_res_{jname}"] = res
            st.session_state.pop(f"ac_redo_{jname}", None)
            st.rerun()
    with b2:
        if st.button("やめる（何もしない）", use_container_width=True):
            st.session_state.pop(f"ac_redo_{jname}", None)
            st.rerun()


def _do_push(job, limit=0):
    """⑤ Salesforceへの投入（登録があるときだけ）。"""
    out = []
    for ld in (job.get("loads", []) or []):
        r = sf_ui.push_sheet(gc, job["sheet_url"], str(ld.get("シート", "")),
                             str(ld.get("オブジェクト", "")), str(ld.get("照合キー", "")),
                             ld.get("マッピング", {}) or {}, limit=limit,
                             send_blanks=bool(ld.get("空も送る", False)))
        out.append({"シート": str(ld.get("シート", "")), "結果": r.get("結果", ""),
                    "ok": r.get("ok", 0), "ng": r.get("ng", 0)})
    return out


# ==========================================
# 📋 一覧
# ==========================================
if st.session_state.ac_view == "list":
    ch.guide("operate",
             "ここは<b>オートコール投入</b>の部屋。シートを更新して、"
             "シートごとにCSVを作って、ブルービーンに入れるところまでやるよ。")

    a, _b = st.columns([1, 3])
    with a:
        if st.button("＋ ジョブを追加", type="primary", use_container_width=True):
            st.session_state.ac_view = "edit"
            st.session_state.ac_job = ""
            st.session_state.pop("ac_calls_of", None)    # 前に開いた編集の途中を持ち込まない
            st.rerun()
    with _b:
        st.caption("ジョブ＝「このスプシの、このシートたちを更新して、"
                   "このシートたちをオートコールに入れる」のひとまとまり。")

    jobs = _jobs(cfg)
    if not jobs:
        st.info("まだジョブがありません。「＋ ジョブを追加」から、最初の1つを登録しましょう。")
    for j in jobs:
        with st.container(border=True):
            col1, col2, col3 = st.columns([3, 3, 2])
            with col1:
                st.markdown(f"#### 📞 {j.get('name', '(名前なし)')}")
                st.caption(j.get("memo", "") or "　")
            with col2:
                _t = j.get("refresh_tabs", []) or []
                _a = j.get("autocalls", []) or []
                _l = j.get("loads", []) or []
                _w = j.get("watch_tabs", []) or []
                st.caption("更新するシート：" + ("、".join(_t) if _t else "（なし）"))
                st.caption("オートコールに入れる：" +
                           ("、".join(str(x.get("シート", "")) for x in _a) if _a else "（なし）"))
                if _l:
                    st.caption("Salesforceへ投入：" +
                               "、".join(str(x.get("シート", "")) for x in _l))
                if _w:
                    st.caption("目で見て確認：" + "、".join(_w))
            with col3:
                if st.button("▶ 全部実行", key=f"ac_all_{j.get('name')}", type="primary",
                             use_container_width=True, disabled=not (j.get("autocalls") or []),
                             help="更新 →（作り直し）→（確認）→ 投入 まで、続けて実行します。"):
                    st.session_state.ac_view = "run"
                    st.session_state.ac_job = j.get("name", "")
                    st.session_state[f"ac_auto_{j.get('name')}"] = True
                    st.session_state.pop(f"ac_each_{j.get('name')}", None)
                    st.rerun()
                if st.button("🔧 個別実行", key=f"ac_run_{j.get('name')}",
                             use_container_width=True,
                             help="工程ごとに、自分で押して進めます。"):
                    st.session_state.ac_view = "run"
                    st.session_state.ac_job = j.get("name", "")
                    st.session_state.pop(f"ac_auto_{j.get('name')}", None)
                    # 個別実行のときだけ、実行画面で「今回入れるもの」を選べるようにする
                    st.session_state[f"ac_each_{j.get('name')}"] = True
                    st.session_state.pop(f"ac_pick_{j.get('name')}", None)
                    st.rerun()
                if st.button("⚙️ 設定を直す", key=f"ac_ed_{j.get('name')}",
                             use_container_width=True):
                    st.session_state.ac_view = "edit"
                    st.session_state.ac_job = j.get("name", "")
                    st.session_state.pop("ac_calls_of", None)    # 保存していない途中を持ち込まない
                    st.rerun()

            # 👀 確認シートは、実行画面に入らなくてもここで見られる。
            #    ⚠️ スプシは読むのに数秒かかるので、押したときだけ読む。
            if _w:
                _lk = f"ac_watch_{j.get('name', '')}"
                _found = st.session_state.get(_lk)
                w1, w2 = st.columns([1, 3])
                with w1:
                    if st.button("👀 確認シートを見る" if _found is None else "🔄 読み直す",
                                 key=f"ac_lw_{j.get('name')}", use_container_width=True,
                                 disabled=not gc):
                        with st.spinner("確認シートを読んでいます..."):
                            st.session_state[_lk] = _do_watch(j)
                        st.rerun()
                with w2:
                    st.caption("ここで中身を見て、直したものは**そのまま消し込めます**。")
                if _found is not None:
                    watch_ui.render(gc, j["sheet_url"], str(j.get("name", "")), _found,
                                    _lk, "list", work_root=WORK_ROOT,
                                    tabs=_w, fix_where="スプレッドシート")

    st.divider()
    st.caption("💻 シートの更新とブルービーンへの投入はブラウザを開くため、"
               "**担当者のPCで開いているとき**だけ動きます。")


# ==========================================
# ⚙️ 設定
# ==========================================
elif st.session_state.ac_view == "edit":
    old_name = st.session_state.ac_job
    job = _find(cfg, old_name) or {
        "name": "", "memo": "", "sheet_url": "",
        "refresh_tabs": [], "refresh_robot": DEFAULT_REFRESH_ROBOT,
        "watch_tabs": [], "watch_block": True,
        "vars": list(DEFAULT_VARS), "autocalls": [],
        "call_robot": DEFAULT_CALL_ROBOT, "auto_call": False,
        "loads": [], "auto_push": False,
    }

    if st.button("⬅ 一覧に戻る"):
        st.session_state.ac_view = "list"
        st.rerun()

    st.markdown(f"### ⚙️ {'ジョブを追加' if not old_name else f'「{old_name}」の設定'}")

    # --- 1️⃣ 名前とスプシ ---
    with st.container(border=True):
        theme.section_title("1️⃣", "ジョブの名前と、つなぐスプレッドシート")
        name = st.text_input("ジョブの名前", value=job.get("name", ""),
                             placeholder="例：トス表作成", key="ac_name")
        memo = st.text_input("メモ", value=job.get("memo", ""), key="ac_memo")
        sheet_url = st.text_input("スプレッドシートのURL", value=job.get("sheet_url", ""),
                                  placeholder="https://docs.google.com/spreadsheets/d/...",
                                  key="ac_url")
        st.caption("※ サービスアカウントのメールアドレスを、このスプシの**閲覧者**"
                   "（確認シートを消し込むなら**編集者**）に追加してください。")

    tabs = []
    if gc and sheet_url.strip():
        try:
            tabs = _tabs_of(gc, sheet_url.strip())
        except Exception as e:
            st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")

    # --- 2️⃣ 更新するシート ---
    with st.container(border=True):
        theme.section_title("2️⃣", "SFコネクタで更新するシート")
        _cur = [t for t in (job.get("refresh_tabs", []) or []) if t in tabs]
        if tabs:
            refresh_tabs = st.multiselect("更新するシート", tabs, default=_cur, key="ac_rtabs")
        else:
            refresh_tabs = [t.strip() for t in
                            st.text_input("更新するシート（カンマ区切り）",
                                          value="、".join(job.get("refresh_tabs", []) or []),
                                          key="ac_rtabs_txt").replace("、", ",").split(",")
                            if t.strip()]
            st.caption("※ スプシURLを入れると、シート名をプルダウンで選べます。")
        _opts = sorted(set(_robots() + [DEFAULT_REFRESH_ROBOT]))
        _rb = job.get("refresh_robot") or DEFAULT_REFRESH_ROBOT
        refresh_robot = st.selectbox("使うロボット", _opts,
                                     index=_opts.index(_rb) if _rb in _opts else 0,
                                     key="ac_rrobot")

    # --- 3️⃣ GAS（任意） ---
    with st.container(border=True):
        theme.section_title("3️⃣", "スプシのGAS（CSVを作る／シートを作り直す）")
        st.caption("**CSVはGASから受け取ります**（アプリで作り直しません）。"
                   "トス表作成のように、投入用シートを作り直す処理があるジョブでは、"
                   "その処理もここで選びます。")
        _auto = gas_deploy.render(
            f"ac_{old_name or '＿新規'}",
            {"gas_script_url": job.get("gas_script_url", ""),
             "gas_url": job.get("gas_url", ""), "gas_token": job.get("gas_token", ""),
             "gas_deployment_id": job.get("gas_deployment_id", "")})
        gas_script_url = str(_auto.get("gas_script_url", "") or "")
        gas_url = str(_auto.get("gas_url", "") or "")
        gas_token = str(_auto.get("gas_token", "") or "")
        gas_deployment_id = str(_auto.get("gas_deployment_id", "") or "")
        _infokey = f"ac_gasinfo_{old_name or '＿新規'}"
        if st.button("🔌 つないで中身を見る", key="ac_inspect"):
            if not gas_url.strip():
                st.warning("まず上の「🚀 GASを入れて公開する」を押してください。")
            else:
                _iok, _idata = sms_runner.run_gas_action(gas_url.strip(), gas_token.strip(),
                                                         "inspect", timeout=90)
                if _iok:
                    st.session_state[_infokey] = _idata
                    st.success(f"✅ つながりました（{(_idata or {}).get('name', '')}）。"
                               "下で処理を選び、**必ず「💾 このジョブを保存」**を押してください。")
                    # ⚠️ CSVを作るのはスプシ側の buildCsvString_。無いスプシでは、つながってもCSVは受け取れない。
                    #    実行してから「buildCsvString_ がありません」で止まる前に、ここで知らせる。
                    if not (_idata or {}).get("csvReady"):
                        st.warning("⚠️ **このスプシには、CSVを作る関数（buildCsvString_）がありません。**"
                                   "このままだと、実行したときにCSVを受け取れず止まります。"
                                   "SMS送信用のスプシにある、CSVを作る処理がこのスプシにも要ります。")
                else:
                    st.error(f"❌ {_idata}")
        _info = st.session_state.get(_infokey) or {}
        _fns = _info.get("functions") or []
        _cur_build = [x for x in str(job.get("gas_build", "") or "").split(",") if x.strip()]
        if _fns:
            gas_build = ",".join(st.multiselect(
                "CSVを作る前に走らせる処理（要るジョブだけ）", _fns,
                default=[x for x in _cur_build if x in _fns], key="ac_build",
                help="トス表作成のように、シートを作り直してから投入するジョブで使います。"))
        else:
            gas_build = st.text_input("走らせる処理（関数名・カンマ区切り）",
                                      value=",".join(_cur_build), key="ac_build_txt",
                                      help="上の「🔌 つないで中身を見る」を押すと、選ぶだけになります。")

    # --- 4️⃣ 確認シート（任意） ---
    with st.container(border=True):
        theme.section_title("4️⃣", "目で見て確認するシート（任意）")
        st.caption("毎回同じルールにできないものを登録します。"
                   "実行時に中身を出して、直したら**その場で消し込めます**。")
        _curw = [t for t in (job.get("watch_tabs", []) or []) if t in tabs]
        if tabs:
            watch_tabs = st.multiselect("確認するシート", tabs, default=_curw, key="ac_wtabs")
        else:
            watch_tabs = [t.strip() for t in
                          st.text_input("確認するシート（カンマ区切り）",
                                        value="、".join(job.get("watch_tabs", []) or []),
                                        key="ac_wtabs_txt").replace("、", ",").split(",")
                          if t.strip()]
        watch_block = st.checkbox("中身が出ていたら、投入のボタンを出さない",
                                  value=bool(job.get("watch_block", True)), key="ac_wblock")

    # --- 5️⃣ オートコール投入 ---
    with st.container(border=True):
        theme.section_title("5️⃣", "オートコールに入れるシート（シートごとに1回）")
        st.caption("**1シート＝1回の投入**です。シートごとにCSVを作って、続けて投入します。")

        # 🧩 カードごとに入れる項目（タイトルなど）は、**ロボットの手順書から自動で決める**。
        #    以前は「差し込む項目の名前（カンマ区切り）」を人に書かせていたが、
        #    全カード共通の欄なので「全部同じタイトルになる？」と誤解された。
        #    手順書の値に書いてある {名前} を拾えば、書かせる必要がない。
        _crobot = job.get("call_robot") or DEFAULT_CALL_ROBOT
        _crow, _csteps = common_robots.robot_row(supabase, _crobot)
        var_names = _step_vars(_csteps) if _crow is not None else _vars_of(job)
        st.caption("📝 カードごとに入れる項目："
                   + ("、".join(var_names) if var_names else "（業務のほかに、入れる項目はありません）")
                   + "（ロボットの手順書の {名前} から自動で決まります。値はカードごとに別々です）")

        # 🎛 業務：ブルービーンから読み込んだ選択肢を覚えておき、表でシートごとに選ぶ
        _gi = _select_step(_csteps, GYOMU)
        _ai = _select_step(_csteps, ACD)
        if _crow is not None and _gi is None:
            st.warning(f"⚠️ ロボット「{_crobot}」の手順書に、**業務を選ぶ手順がありません**。"
                       "業務はシートごとに選べません（録画のときの業務のまま投入されます）。")
        elif _crow is not None:
            _need = [f"業務を `{{{GYOMU}}}` に"] if "{" + GYOMU + "}" not in str(_csteps[_gi].get("値", "")) else []
            if _ai is not None and str(_csteps[_ai].get("値", "")).strip() != ONLY_ONE:
                _need.append(f"作業グループを `{ONLY_ONE}` に")
            if _need:
                st.warning("⚠️ ロボットの手順書が、まだ**録画のときの業務・作業グループのまま**です。"
                           "このままだと、表で選んだ業務は使われません。")
                if st.button("🔧 手順書を、業務をシートごとに選べる形にする（" + "／".join(_need) + "）",
                             key="ac_fixgyomu"):
                    _set_select_value(_csteps[_gi], "{" + GYOMU + "}")
                    if _ai is not None:
                        _set_select_value(_csteps[_ai], ONLY_ONE)
                    common_robots._save_steps(supabase, _crow, _csteps)
                    st.session_state["ac_opt_msg"] = "✅ 手順書を直しました。"
                    st.rerun()
            st.caption(f"💡 **作業グループ（ACD）は選ばなくてOK**。業務を選ぶと1つだけ出てくるので、"
                       "ロボットがそれを選びます（2つ以上出ていたら、選ばずに止まります）。")

            # 📋 投入のあと、無効なデータ件数を確かめる（2件以上＝エラー）。
            #    共通ロボットの登録画面と同じ部品を使う（2か所に書くと食い違う）。
            common_robots.import_check_block(supabase, _crow, _csteps, "ac")
            common_robots.bb_delete_block(supabase, _crow, _csteps, "ac")

        _opts = _gyomu_options(cfg)
        _meta = (cfg.get(OPTIONS_KEY) or {}).get(GYOMU) or {}
        o1, o2 = st.columns([1, 2])
        with o1:
            _read_go = st.button("🔄 ブルービーンから業務を読み込む", key="ac_readgyomu",
                                 use_container_width=True, disabled=_gi is None)
        with o2:
            st.caption(f"覚えている業務：**{len(_opts)}件**（最後に読み込んだ日時："
                       f"{_meta.get('updated') or 'まだ'}）。新しい業務が増えたら押してください。"
                       "**前に覚えたものは消えません。** ブラウザが開いてログインし、"
                       "業務の選択肢を読むだけです（何も選ばず、投入もしません）。")
        if _read_go:
            with st.spinner("ブルービーンにログインして、業務の選択肢を読んでいます..."):
                _rok, _new, _rlog = sms_runner.read_select_options(
                    _crobot, str(_csteps[_gi].get("対象", "")).strip())
            if _rok:
                _merged, _added = _merge_options(_opts, _new)
                cfg.setdefault(OPTIONS_KEY, {})[GYOMU] = {
                    "options": _merged, "updated": time.strftime("%Y/%m/%d %H:%M")}
                _save(cfg)
                st.session_state["ac_opt_msg"] = (
                    f"✅ 業務を {len(_new)}件 読み込みました。"
                    + (f"新しく覚えたもの：{'、'.join(_added)}" if _added else "新しい業務はありませんでした。"))
                st.rerun()
            else:
                st.error("❌ 業務の選択肢を読み込めませんでした。下のログを確かめてください。")
                with st.expander("実行ログ", expanded=True):
                    st.text(_rlog[-4000:])
        if st.session_state.get("ac_opt_msg"):
            st.success(st.session_state.pop("ac_opt_msg"))
        if _opts:
            with st.expander(f"覚えている業務の一覧（{len(_opts)}件）"):
                st.dataframe(pd.DataFrame([{"業務": o.get("label", ""), "ブルービーンでの番号": o.get("value", "")}
                                           for o in _opts]), use_container_width=True, hide_index=True)

        # 📋 投入の一覧（1枚＝1回の投入）。表だと「行を足す場所」が分かりにくく、
        #    複数登録できないと思われたので、6️⃣と同じカードにする。
        #    ⚠️ 入力欄のキーは番号ではなく、カードごとの目印（_uid）に結びつける
        #       （番号だと、途中のカードを消したときに下のカードが前の値を引き継ぐ）。
        if st.session_state.get("ac_calls_of") != (old_name or "＿新規"):
            st.session_state["ac_calls_list"] = [
                dict(e, _uid=f"c{i}_{int(time.time() * 1000)}")
                for i, e in enumerate(job.get("autocalls", []) or [])]
            st.session_state["ac_calls_of"] = old_name or "＿新規"
        calls_list = st.session_state["ac_calls_list"]
        _labels = [o.get("label", "") for o in _opts]
        # 前に選んだ業務がブルービーンから消えていても、選んだ値は消さずに出す
        _labels += [str(e.get(GYOMU, "")) for e in calls_list
                    if e.get(GYOMU) and e.get(GYOMU) not in _labels]

        st.markdown(f"**投入するもの（{len(calls_list)}件・上から順に投入します）**")
        if not calls_list:
            st.info("まだありません。下の「＋ ブルービーンへの投入を追加」か「まとめて足す」で登録してください。")
        for i, e in enumerate(calls_list):
            u = e["_uid"]
            with st.container(border=True):
                h1, h2, h3 = st.columns([6, 1, 1])
                with h1:
                    st.markdown(f"**📞 ブルービーンへ {i + 1}**")
                with h2:
                    if i > 0 and st.button("⬆", key=f"ac_up_{u}", help="1つ上へ（先に投入する）"):
                        calls_list[i - 1], calls_list[i] = calls_list[i], calls_list[i - 1]
                        st.rerun()
                with h3:
                    if st.button("🗑", key=f"ac_del_{u}", help="この投入を消す"):
                        calls_list.pop(i)
                        st.rerun()
                c1, c2 = st.columns(2)
                with c1:
                    _cur = str(e.get("シート", "") or "")
                    if tabs:
                        _topts = [""] + tabs + ([_cur] if _cur and _cur not in tabs else [])
                        e["シート"] = st.selectbox("シート", _topts, index=_topts.index(_cur),
                                                  key=f"ac_sheet_{u}",
                                                  format_func=lambda x: x or "（選んでください）")
                    else:
                        e["シート"] = st.text_input("シート", value=_cur, key=f"ac_sheet_{u}")
                with c2:
                    _g = str(e.get(GYOMU, "") or "")
                    if _labels:
                        _gopts = [""] + _labels
                        e[GYOMU] = st.selectbox("業務", _gopts,
                                                index=_gopts.index(_g) if _g in _gopts else 0,
                                                key=f"ac_gyomu_{u}",
                                                format_func=lambda x: x or "（選んでください）")
                    else:
                        e[GYOMU] = st.text_input("業務", value=_g, key=f"ac_gyomu_{u}",
                                                 help="上の「🔄 ブルービーンから業務を読み込む」を押すと、選ぶだけになります。")
                for v in var_names:
                    e[v] = st.text_input(v, value=str(e.get(v, "") or ""), key=f"ac_var_{v}_{u}")

        a1, a2 = st.columns([1, 2])
        with a1:
            if st.button("＋ ブルービーンへの投入を追加", key="ac_addcall", use_container_width=True):
                calls_list.append({"シート": "", GYOMU: "", "_uid": f"n{int(time.time() * 1000)}"})
                st.rerun()
        with a2:
            if tabs:
                _bulk = st.multiselect("シートをまとめて足す（選んだ順に1件ずつ足します）", tabs,
                                       key="ac_bulk")
                if st.button("まとめて足す", key="ac_bulkadd", disabled=not _bulk):
                    for _n, _t in enumerate(_bulk):
                        calls_list.append({"シート": _t, GYOMU: "",
                                           "_uid": f"b{_n}_{int(time.time() * 1000)}"})
                    st.session_state.pop("ac_bulk", None)
                    st.rerun()
        st.caption("💡 同じシートを、別のタイトルや業務で何件入れても構いません。")

        _opts_c = sorted(set(_robots() + [DEFAULT_CALL_ROBOT]))
        _cb = job.get("call_robot") or DEFAULT_CALL_ROBOT
        call_robot = st.selectbox("使うロボット（ブルービーン）", _opts_c,
                                  index=_opts_c.index(_cb) if _cb in _opts_c else 0,
                                  key="ac_crobot")
        auto_call = st.checkbox("**「▶ 全部実行」で、投入まで自動で行う**",
                                value=bool(job.get("auto_call", False)), key="ac_autocall")
        if auto_call:
            st.warning("⚠️ **一覧の「▶ 全部実行」を押しただけで、オートコールに投入されます。**")
        else:
            st.caption("💡 いまは、CSVを作ったところで止まります（そこから手で投入できます）。")

    # --- 6️⃣ Salesforceへの投入（任意） ---
    if st.session_state.get("ac_loads_of") != (old_name or "＿新規"):
        st.session_state["ac_loads"] = json.loads(json.dumps(job.get("loads", []) or []))
        st.session_state["ac_loads_of"] = old_name or "＿新規"
    loads = st.session_state["ac_loads"]
    with st.container(border=True):
        theme.section_title("6️⃣", "Salesforceへ入れる（任意・トス表作成だけ）")
        st.caption("データローダー自動化とまったく同じしくみです。要らなければ空のままでOK。")
        for i, ld in enumerate(loads):
            with st.container(border=True):
                h1, h2 = st.columns([5, 1])
                with h1:
                    st.markdown(f"**投入 {i + 1}**")
                with h2:
                    if st.button("🗑", key=f"ac_delload_{i}", help="この投入を消す"):
                        loads.pop(i)
                        st.rerun()
                sf_ui.load_editor(gc, sheet_url.strip(), tabs, ld, f"ac_load_{i}")
        auto_push = st.checkbox("**「▶ 全部実行」で、Salesforceへの投入まで自動で行う**",
                                value=bool(job.get("auto_push", False)), key="ac_autopush")
        if auto_push:
            st.warning("⚠️ **確認なしで全件を Salesforce に反映します**（UPSERT＝上書き）。")
        if st.button("＋ 投入を追加", key="ac_addload"):
            loads.append({"シート": (tabs[0] if tabs else ""), "オブジェクト": "Opportunity",
                          "照合キー": "Id", "マッピング": {}})
            st.rerun()

    st.divider()
    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button("💾 このジョブを保存", type="primary", use_container_width=True):
            if not name.strip():
                st.warning("ジョブの名前を入れてください。")
            else:
                calls = []
                for r in calls_list:
                    if not str(r.get("シート", "")).strip():
                        continue
                    e = {"シート": str(r["シート"]).strip(), GYOMU: str(r.get(GYOMU, "") or "").strip()}
                    for v in var_names:
                        e[v] = str(r.get(v, "") or "").strip()
                    calls.append(e)
                new = dict(job)
                new.update({
                    "name": name.strip(), "memo": memo.strip(),
                    "sheet_url": sheet_url.strip(),
                    "refresh_tabs": list(refresh_tabs), "refresh_robot": refresh_robot,
                    "gas_script_url": gas_script_url, "gas_url": gas_url,
                    "gas_token": gas_token, "gas_deployment_id": gas_deployment_id,
                    "gas_build": gas_build,
                    "watch_tabs": list(watch_tabs), "watch_block": bool(watch_block),
                    "vars": var_names, "autocalls": calls,
                    "call_robot": call_robot, "auto_call": bool(auto_call),
                    "loads": loads, "auto_push": bool(auto_push),
                })
                jobs = [x for x in _jobs(cfg) if x.get("name") != old_name]
                jobs.append(new)
                cfg["jobs"] = jobs
                _save(cfg)
                st.session_state.ac_view = "list"
                st.session_state.pop("ac_loads_of", None)
                st.session_state.pop("ac_calls_of", None)
                st.success("保存しました。")
                st.rerun()
    with s2:
        if old_name and st.button("🗑 このジョブを消す"):
            cfg["jobs"] = [x for x in _jobs(cfg) if x.get("name") != old_name]
            _save(cfg)
            st.session_state.ac_view = "list"
            st.rerun()


# ==========================================
# ▶ 実行
# ==========================================
else:
    jname = st.session_state.ac_job
    job = _find(cfg, jname)
    if not job:
        st.warning("ジョブが見つかりません。")
        if st.button("⬅ 一覧に戻る"):
            st.session_state.ac_view = "list"
            st.rerun()
        st.stop()

    if st.button("⬅ 一覧に戻る"):
        st.session_state.ac_view = "list"
        st.rerun()

    st.markdown(f"### ▶ 「{jname}」を実行する")
    st.caption("① SFコネクタで更新 →（② 作り直し）→（③ 確認）→ ④ オートコール投入"
               "→（⑤ Salesforce投入）")

    _calls = job.get("autocalls", []) or []
    _watch = job.get("watch_tabs", []) or []
    _auto = st.session_state.pop(f"ac_auto_{jname}", False)

    # --- ① 更新 ---
    with st.container(border=True):
        theme.section_title("1️⃣", "SFコネクタでシートを更新する")
        _tabs = job.get("refresh_tabs", []) or []
        if not _tabs:
            st.info("更新するシートが登録されていません（この工程は行いません）。")
        else:
            st.caption(f"使うロボット：**{job.get('refresh_robot') or DEFAULT_REFRESH_ROBOT}**"
                       f"　／　開くスプレッドシート：{job.get('sheet_url', '')}")
            if _auto or st.button("🔄 更新する", type="primary", disabled=not gc):
                with st.spinner(f"① {len(_tabs)}枚のシートを更新しています..."):
                    _ok, _log, _tbl = _do_refresh(job, _tabs)
                st.session_state[f"ac_ref_{jname}"] = {"ok": _ok, "log": _log, "表": _tbl}
        _r = st.session_state.get(f"ac_ref_{jname}")
        if _r:
            (st.success if _r["ok"] else st.error)(
                "✅ 更新しました。" if _r["ok"] else "❌ 更新でつまずきました。")
            if _r.get("表") is not None:
                st.dataframe(pd.DataFrame(_r["表"]), use_container_width=True, hide_index=True)
            with st.expander("実行ログ", expanded=not _r["ok"]):
                st.text(_r["log"][-4000:])

    # --- ② GAS（任意） ---
    if str(job.get("gas_url", "") or "").strip():
        with st.container(border=True):
            theme.section_title("2️⃣", "スプシのGASでシートを作り直す")
            if _auto or st.button("🔁 作り直す", disabled=not str(job.get("gas_build", "")).strip()):
                with st.spinner("② シートを作り直しています..."):
                    _gok, _gdata = _do_gas(job)
                st.session_state[f"ac_gasr_{jname}"] = {"ok": _gok, "data": _gdata}
                # ⭐ 作り直した直後が、いちばん新しいエラーの姿。ここで読んでおく。
                if _gok and _watch and gc:
                    st.session_state[f"ac_watch_{jname}"] = _do_watch(job)
            _g = st.session_state.get(f"ac_gasr_{jname}")
            if _g:
                if _g["ok"]:
                    st.success("✅ 作り直しました。")
                elif _g["ok"] is None:
                    st.caption(str(_g["data"]))
                else:
                    st.error(f"❌ {_g['data']}")

    # --- ③ 確認シート（任意） ---
    watch_ok = True
    if _watch:
        with st.container(border=True):
            theme.section_title("3️⃣", "目で見て確認するシート")
            wkey = f"ac_watch_{jname}"
            if st.button("🔍 確認する", disabled=not gc):
                with st.spinner("確認するシートを読んでいます..."):
                    st.session_state[wkey] = _do_watch(job)
                st.rerun()
            found = st.session_state.get(wkey)
            if found is None:
                st.caption("まだ確認していません。")
                watch_ok = not job.get("watch_block", True)
            else:
                _bad = [f for f in found if f["件数"] != 0]
                watch_ui.render(gc, job["sheet_url"], jname, found, wkey, "run",
                                work_root=WORK_ROOT, tabs=_watch,
                                fix_where="スプレッドシート")
                if not _bad:
                    watch_ok = True
                else:
                    watch_ok = not job.get("watch_block", True)
                    _n = sum(f["件数"] for f in _bad if f["件数"] > 0)
                    if watch_ok:
                        st.info(f"⚠️ {_n}件 出ていますが、設定（投入のボタンを出さない＝OFF）"
                                "にしたがって、**このまま投入に進めます**。")
                    else:
                        st.info("直したものは表でチェックして**「🗑 対応した分を消す」**を"
                                "押してください。**0件になれば投入へ進めます。**")

    # --- ④ オートコール投入 ---
    with st.container(border=True):
        theme.section_title("4️⃣", "ブルービーンへ投入する（シートごと）")
        if not _calls:
            st.warning("投入するシートがありません（設定画面の5️⃣で登録してください）。")
        elif not watch_ok:
            st.info("上の 3️⃣ の確認が終わると、投入のボタンが出ます。")
        else:
            st.dataframe(pd.DataFrame(_calls), use_container_width=True, hide_index=True)
            # 🎯 個別実行のときだけ、今回入れるものを選べる（1枚だけ入れ直す、など）。
            #    ⚠️ 全部しか選べないと、1枚だけ失敗したときに、通った分まで入れ直して重なり、処理失敗になる。
            #    全部実行は「全部」のまま（黙って一部だけにならないように）。
            if st.session_state.get(f"ac_each_{jname}"):
                _labels_run = [f"{_n + 1}. {e.get('シート', '')}"
                               + (f"（{e.get('タイトル')}）" if e.get("タイトル") else "")
                               for _n, e in enumerate(_calls)]
                _picked = st.multiselect("今回入れるもの（外したものは何もしません）", _labels_run,
                                         default=_labels_run, key=f"ac_pick_{jname}")
                _calls = [e for _n, e in enumerate(_calls) if _labels_run[_n] in _picked]
            st.caption(f"シートごとに、CSVを作って → 渡して投入します。（{len(_calls)}回）")
            _set_call = bool(job.get("auto_call", False))
            if _set_call:
                st.warning("⚙️ この設定では、**投入まで自動で行います**。")
            t1, t2 = st.columns([1, 1])
            with t1:
                if st.button("🧪 お試し（投入の手前まで）", use_container_width=True,
                             disabled=not gc):
                    res = []
                    for e in _calls:
                        with st.spinner(f"「{e.get('シート')}」を試しています..."):
                            try:
                                res.append(_do_autocall(job, e, submit=False))
                            except Exception as ex:
                                res.append({"シート": e.get("シート"), "ok": False,
                                            "log": str(ex), "CSV": "", "件数": 0,
                                            "投入まで進んだ": False})
                    st.session_state[f"ac_res_{jname}"] = res
                    st.rerun()
            with t2:
                _agree = _set_call or st.checkbox(
                    "**実際に投入します**（取り消せません）", key=f"ac_agree_{jname}")
                if (_auto and _set_call) or st.button("🚀 投入する", type="primary",
                                                      use_container_width=True,
                                                      disabled=not (_agree and gc)):
                    res = []
                    for e in _calls:
                        with st.spinner(f"「{e.get('シート')}」を投入しています..."):
                            try:
                                res.append(_do_autocall(job, e, submit=True))
                            except Exception as ex:
                                res.append({"シート": e.get("シート"), "ok": False,
                                            "log": str(ex), "CSV": "", "件数": 0,
                                            "投入まで進んだ": False})
                    st.session_state[f"ac_res_{jname}"] = res
                    st.rerun()

            # 🔁 消して入れ直す：①消す候補を探す（何も変えない）→ ②小窓で人が選ぶ → ③消してから投入
            st.markdown("**🔁 消して入れ直す**")
            st.caption("同じ業務・同じシート名で前に入れたファイルを、一覧（3ページ目まで）から探して、"
                       "消してから投入します。**まず候補を探すだけ**なので、押しても何も消えません。")
            _rrow, _rsteps = common_robots.robot_row(supabase, job.get("call_robot") or DEFAULT_CALL_ROBOT)
            _has_del = any(str(s.get("操作", "")) == common_robots.BB_DELETE_OP for s in _rsteps)
            if not _has_del:
                st.warning("ロボットの手順書に『前回のファイルを削除』がありません"
                           "（設定画面の5️⃣「🗑 前のファイルを消す手順を足す」で足してください）。")
            if st.button("🔎 消す候補を探す（まだ何も消しません）", use_container_width=True,
                         disabled=not (_has_del and gc), key=f"ac_find_{jname}"):
                _found = []
                for _i, e in enumerate(_calls):
                    _sh = str(e.get("シート", "") or "")
                    with st.spinner(f"「{_sh}」の前のファイルを探しています..."):
                        _fok, _cands, _flog = sms_runner.find_autocall_imports(
                            job.get("call_robot") or DEFAULT_CALL_ROBOT, _slot(jname, _sh),
                            str(e.get(GYOMU, "") or ""), _sh)
                    _found.append({"i": _i, "ok": _fok, "cands": _cands, "log": _flog})
                st.session_state[f"ac_redo_{jname}"] = _found
                st.rerun()
            if st.session_state.get(f"ac_redo_{jname}"):
                _redo_dialog(job, _calls, jname)

        _res = st.session_state.get(f"ac_res_{jname}")
        if _res:
            st.dataframe(pd.DataFrame([{"シート": r["シート"], "CSV": r["CSV"],
                                        "件数": r["件数"],
                                        "結果": ("✅ 通りました" if r["ok"] else
                                               "⚠️ 投入操作まで進みました" if r["投入まで進んだ"]
                                               else "❌ 投入できず"),
                                        "理由": r.get("理由", "")} for r in _res]),
                         use_container_width=True, hide_index=True)
            for r in _res:
                if not r["ok"]:
                    with st.expander(f"「{r['シート']}」のログ", expanded=True):
                        st.text(str(r["log"])[-4000:])

    # --- ⑤ Salesforceへ投入（任意） ---
    if job.get("loads"):
        with st.container(border=True):
            theme.section_title("5️⃣", "Salesforceへ入れる（任意）")
            _set_push = bool(job.get("auto_push", False))
            if _set_push:
                st.warning("⚙️ この設定では、**投入まで自動で行います**。")
            n_try = st.number_input("お試し件数（先にこれだけ入れて確かめる）",
                                    min_value=1, max_value=200, value=5, key=f"ac_ntry_{jname}")
            p1, p2 = st.columns(2)
            with p1:
                if st.button(f"🧪 お試し（先頭{int(n_try)}件）", use_container_width=True,
                             disabled=not gc):
                    st.session_state[f"ac_push_{jname}"] = _do_push(job, limit=int(n_try))
                    st.rerun()
            with p2:
                _ok_push = _set_push or st.checkbox(
                    "**全件を Salesforce に反映します**（UPSERTなので上書きされます）",
                    key=f"ac_pushagree_{jname}")
                if st.button("🚀 全件投入", type="primary", use_container_width=True,
                             disabled=not (_ok_push and gc)):
                    st.session_state[f"ac_push_{jname}"] = _do_push(job, limit=0)
                    st.rerun()
            _p = st.session_state.get(f"ac_push_{jname}")
            if _p:
                st.dataframe(pd.DataFrame([{"シート": r.get("シート", ""),
                                            "結果": r.get("結果", ""),
                                            "成功": r.get("ok", 0), "失敗": r.get("ng", 0)}
                                           for r in _p]),
                             use_container_width=True, hide_index=True)

    st.divider()
    st.caption("💻 シートの更新とブルービーンへの投入は、**担当者のPCで開いているとき**だけ動きます。"
               "Salesforceへの投入はクラウドからでも動きます。")
