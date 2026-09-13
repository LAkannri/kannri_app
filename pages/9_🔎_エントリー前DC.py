"""
🔎 エントリー前DC（エントリーする前の、内容チェック）

エントリーの前に「必要項目の中身が正しいか」を見る工程。
いまはスプレッドシートの**条件付き書式で色を付けて**確かめている。

【困っていたこと】
  - ルールを足すのが面倒
  - **何を登録してあるのか、誰も覚えていない**
  - 結局「いま何が確認できる状態なのか」が分からない

【この画面がやること】
  ⭐ **何をチェックしているかを、一覧で見せる**（これが本丸）。
     スプシ側に `updateAllAndAddNotes` があり、条件付き書式を読み取って
     `全ルール一覧` シートを作ってくれる。それをアプリから走らせて、読んで、
     シートごとに畳んで出す。人がスプシを開いて探し回らなくてよくなる。
  ⭐ 誤りが出ている案件は「目で見て確認するシート」として出し、
     **1件ずつ消し込める**（データローダー自動化とまったく同じ部品）。

⚠️ **色が付いている行そのものは、アプリからは読めない。**
   Google の API は「条件付き書式のルール」は返すが、**その結果の色は返さない**。
   だから誤りの一覧は、**スプシのGAS側で作って1枚のシートに書き出す**必要がある。
   ここを勝手に真似して書くと、スプシのルールと二重管理になって必ず食い違うので、
   アプリは**作らない・読むだけ**にしている。
"""
import json

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import characters as ch
import common_robots
import gas_deploy
import sms_runner
import theme
import watch_ui

st.set_page_config(page_title="エントリー前DC - エンカンAI", layout="wide")

theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🔎", "エントリー前DC",
                  "エントリーする前に、必要項目の中身が正しいかを確かめます。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

SETTINGS_ID = "__precheck__"
WORK_ROOT = "エントリー前DC"
DEFAULT_RULE_SHEET = "全ルール一覧"
DEFAULT_RULE_BUILD = "updateAllAndAddNotes"
DEFAULT_CHECK_TABS = ["Nチェック", "Eチェック", "Gチェック"]
# ① SFコネクタで更新するシート（チェック用シートは、この貼り付けシートを映しているだけ）
DEFAULT_REFRESH_TABS = ["N貼り付け", "E貼り付け", "G貼り付け"]
DEFAULT_REFRESH_ROBOT = "共通_SFコネクタ更新"
# 「全ルール一覧」の見出し（スプシのGASが作る形）
RULE_COLS = ["シート名", "適用範囲", "数式/条件詳細", "背景色", "説明"]


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
        "id": SETTINGS_ID, "name": "（エントリー前DCの設定）", "is_active": False,
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


def _do_refresh(sheet_url: str, tabs, robot_name: str):
    """① SFコネクタで貼り付けシートを更新する（オートコール投入・データローダーと同じしくみ）。

    ⚠️ 開く先は**必ずこのスプシ**を渡す。渡さないと、ロボットは録画したときのスプシを開いて
       そちらを更新してしまう（オートコール投入で実際に起きた）。
    """
    try:
        gids = _tab_gids(gc, sheet_url) if gc else {}
    except Exception:
        gids = {}
    urls = sms_runner.tab_urls_for(sheet_url, tabs, gids)
    folder = sms_runner.pattern_dir("SFコネクタ更新", WORK_ROOT)
    ok, log = sms_runner.run_sheet_refresh(robot_name or DEFAULT_REFRESH_ROBOT, folder,
                                           tabs=tabs, tab_urls=urls, url=sheet_url)
    return ok, log, sms_runner.refresh_results(log, len(tabs))


def _read_rules(gc, sheet_url: str, tab: str):
    """`全ルール一覧` を読む。戻り値：(見出し, 行)"""
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    values = sh.worksheet(tab).get_all_values()
    if not values:
        return [], []
    heads = [str(h).strip() for h in values[0]]
    rows = [r for r in values[1:] if any(str(x).strip() for x in r)]
    return heads, rows


cfg = _load()
gc = _get_gspread_client()

if not gc:
    st.warning("🔑 接続キー **GOOGLE_SERVICE_ACCOUNT_JSON** が未設定です（管理者に設定を依頼してください）。")

ch.guide("operate",
         "ここは<b>エントリーの前に中身を確かめる</b>部屋。"
         "<b>いま何をチェックしているか</b>を一覧で出すから、"
         "「何を登録したか忘れた」がなくなるよ。")

st.session_state.setdefault("pc_view", "main")

# ==========================================
# ⚙️ 設定（最初に1回だけ）
# ==========================================
with st.expander("⚙️ 設定（最初に1回だけ／ふだんは触りません）",
                 expanded=not str(cfg.get("sheet_url", "")).strip()):
    sheet_url = st.text_input("チェック用スプレッドシートのURL",
                              value=cfg.get("sheet_url", ""),
                              placeholder="https://docs.google.com/spreadsheets/d/...",
                              key="pc_url")
    st.caption("※ サービスアカウントのメールアドレスを、このスプシの**閲覧者**"
               "（誤りの行を消し込むなら**編集者**）に追加してください。")

    tabs = []
    if gc and sheet_url.strip():
        try:
            tabs = _tabs_of(gc, sheet_url.strip())
        except Exception as e:
            st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")

    st.markdown("**① SFコネクタで更新するシート**")
    st.caption("チェック用シート（Nチェック など）は、貼り付けシートをそのまま映しています。"
               "**貼り付けシートを最新にしてから**チェックを見ます。")
    _cur_rt = cfg.get("refresh_tabs")
    _cur_rt = DEFAULT_REFRESH_TABS if _cur_rt is None else _cur_rt
    if tabs:
        refresh_tabs = st.multiselect("更新するシート", tabs,
                                      default=[t for t in _cur_rt if t in tabs], key="pc_rtabs")
    else:
        refresh_tabs = [t.strip() for t in
                        st.text_input("更新するシート（カンマ区切り）", value="、".join(_cur_rt),
                                      key="pc_rtabs_txt").replace("、", ",").split(",")
                        if t.strip()]
    _robots = sorted(set(common_robots.list_robots(supabase) + [DEFAULT_REFRESH_ROBOT]))
    _rb = cfg.get("refresh_robot") or DEFAULT_REFRESH_ROBOT
    refresh_robot = st.selectbox("使うロボット", _robots,
                                 index=_robots.index(_rb) if _rb in _robots else 0,
                                 key="pc_rrobot")

    st.markdown("**ルールの一覧を作る（スプシのGAS）**")
    st.caption("スプシ側の処理が、条件付き書式を読み取って"
               f"「{DEFAULT_RULE_SHEET}」シートを作ります。アプリはそれを読んで出すだけです。")
    _auto = gas_deploy.render(
        "pc_gas",
        {"gas_script_url": cfg.get("gas_script_url", ""),
         "gas_url": cfg.get("gas_url", ""), "gas_token": cfg.get("gas_token", ""),
         "gas_deployment_id": cfg.get("gas_deployment_id", "")})

    _info = st.session_state.get("pc_gasinfo") or {}
    if st.button("🔌 つないで中身を見る", key="pc_inspect"):
        _u = str(_auto.get("gas_url", "") or "").strip()
        if not _u:
            st.warning("まず上の「🚀 GASを入れて公開する」を押してください。")
        else:
            ok, data = sms_runner.run_gas_action(_u, str(_auto.get("gas_token", "") or ""),
                                                 "inspect", timeout=90)
            if ok:
                st.session_state["pc_gasinfo"] = data
                st.success(f"✅ つながりました（{(data or {}).get('name', '')}）。")
            else:
                st.error(f"❌ {data}")
    _info = st.session_state.get("pc_gasinfo") or {}
    _fns = _info.get("functions") or []
    _cur_build = str(cfg.get("rule_build", DEFAULT_RULE_BUILD) or DEFAULT_RULE_BUILD)
    if _fns:
        rule_build = st.selectbox("ルール一覧を作る処理", _fns,
                                  index=_fns.index(_cur_build) if _cur_build in _fns else 0,
                                  key="pc_build")
    else:
        rule_build = st.text_input("ルール一覧を作る処理（関数名）", value=_cur_build,
                                   key="pc_build_txt")

    _opts = tabs or [DEFAULT_RULE_SHEET]
    _cur_rs = str(cfg.get("rule_sheet", DEFAULT_RULE_SHEET) or DEFAULT_RULE_SHEET)
    if tabs:
        rule_sheet = st.selectbox("ルール一覧のシート", _opts,
                                  index=_opts.index(_cur_rs) if _cur_rs in _opts else 0,
                                  key="pc_rulesheet")
    else:
        rule_sheet = st.text_input("ルール一覧のシート", value=_cur_rs, key="pc_rulesheet_txt")

    st.markdown("**誤りが出ている案件を見るシート（任意）**")
    st.caption("スプシのGASが誤りを1枚にまとめて書き出しているなら、ここに登録すると"
               "**中身を出して1件ずつ消し込めます**（データローダー自動化と同じ操作です）。")
    _curw = [t for t in (cfg.get("watch_tabs", []) or []) if t in tabs]
    if tabs:
        watch_tabs = st.multiselect("確認するシート", tabs, default=_curw, key="pc_wtabs")
    else:
        watch_tabs = [t.strip() for t in
                      st.text_input("確認するシート（カンマ区切り）",
                                    value="、".join(cfg.get("watch_tabs", []) or []),
                                    key="pc_wtabs_txt").replace("、", ",").split(",")
                      if t.strip()]

    if st.button("💾 保存", type="primary", key="pc_save"):
        cfg.update({
            "sheet_url": sheet_url.strip(),
            "refresh_tabs": list(refresh_tabs), "refresh_robot": refresh_robot,
            "gas_script_url": str(_auto.get("gas_script_url", "") or ""),
            "gas_url": str(_auto.get("gas_url", "") or ""),
            "gas_token": str(_auto.get("gas_token", "") or ""),
            "gas_deployment_id": str(_auto.get("gas_deployment_id", "") or ""),
            "rule_build": rule_build, "rule_sheet": rule_sheet,
            "watch_tabs": list(watch_tabs),
        })
        _save(cfg)
        st.success("保存しました。")
        st.rerun()

_url = str(cfg.get("sheet_url", "") or "").strip()
if not _url:
    st.info("まず上の⚙️設定で、チェック用スプレッドシートのURLを入れてください。")
    st.stop()

st.markdown(f"[📄 スプレッドシートを開く]({_url})")

# ==========================================
# ① 貼り付けシートを最新にする（SFコネクタ）
# ==========================================
_rtabs = cfg.get("refresh_tabs")
_rtabs = DEFAULT_REFRESH_TABS if _rtabs is None else _rtabs
with st.container(border=True):
    theme.section_title("①", "SFコネクタで貼り付けシートを最新にする")
    if not _rtabs:
        st.info("更新するシートが登録されていません（⚙️設定で登録できます）。")
    else:
        u1, u2 = st.columns([1, 3])
        with u1:
            _go_ref = st.button("🔄 更新する", type="primary", use_container_width=True,
                                disabled=not gc, key="pc_refresh")
        with u2:
            st.caption(f"更新するシート：{'、'.join(_rtabs)}"
                       f"　／　使うロボット：{cfg.get('refresh_robot') or DEFAULT_REFRESH_ROBOT}"
                       "　／　ブラウザが開き、1枚ずつ更新します（担当者のPCで開いているときだけ動きます）。")
        if _go_ref:
            with st.spinner(f"{len(_rtabs)}枚のシートを更新しています..."):
                _ok, _log, _tbl = _do_refresh(_url, _rtabs, cfg.get("refresh_robot"))
            st.session_state["pc_ref"] = {"ok": _ok, "log": _log, "表": _tbl,
                                          "時刻": sms_runner.today_stamp()}
        _r = st.session_state.get("pc_ref")
        if _r:
            (st.success if _r["ok"] else st.error)(
                "✅ 更新しました。下でチェックを見てください。" if _r["ok"] else "❌ 更新でつまずきました。")
            if _r.get("表") is not None:
                st.dataframe(pd.DataFrame(_r["表"]), use_container_width=True, hide_index=True)
            with st.expander("実行ログ", expanded=not _r["ok"]):
                st.text(str(_r["log"])[-4000:])

# ==========================================
# 📋 いま何をチェックしているか
# ==========================================
with st.container(border=True):
    theme.section_title("📋", "いま何をチェックしているか")
    st.caption("スプシの条件付き書式を読み取って作られた一覧です。"
               "**ここを見れば、何が確認できる状態なのかが分かります。**")

    r1, r2 = st.columns([1, 3])
    with r1:
        if st.button("🔄 ルール一覧を最新にする", type="primary", use_container_width=True,
                     disabled=not str(cfg.get("gas_url", "")).strip()):
            with st.spinner("スプシのGASを走らせています..."):
                ok, data = sms_runner.run_gas_action(
                    str(cfg.get("gas_url", "")), str(cfg.get("gas_token", "") or ""),
                    action="build", timeout=600,
                    build=str(cfg.get("rule_build", DEFAULT_RULE_BUILD) or ""))
            if ok:
                st.session_state.pop("pc_rules", None)
                st.success("✅ 一覧を作り直しました。")
            else:
                st.error(f"❌ {data}")
                if "getUi" in str(data) or "ui.alert" in str(data):
                    st.info("👉 その処理は**画面を出す命令**（`SpreadsheetApp.getUi().alert`）で"
                            "終わっているため、人がいない実行では落ちます。"
                            "最後の `alert` を `try { … } catch (e) {}` で囲むか、"
                            "アプリから呼ぶ用に分けてください。")
    with r2:
        st.caption("押すと、スプシ側の処理が条件付き書式を読み直して"
                   f"「{cfg.get('rule_sheet', DEFAULT_RULE_SHEET)}」を作り直します。"
                   "ルールを足した・直したあとに押してください。")

    if st.button("📖 一覧を読み込む", key="pc_load", disabled=not gc):
        try:
            heads, rows = _read_rules(gc, _url, str(cfg.get("rule_sheet", DEFAULT_RULE_SHEET)))
            st.session_state["pc_rules"] = {"見出し": heads, "行": rows}
        except Exception as e:
            st.error(f"読めませんでした：{str(e)[:160]}")

    _r = st.session_state.get("pc_rules")
    if not _r:
        st.caption("まだ読み込んでいません。")
    elif not _r["行"]:
        st.warning(f"「{cfg.get('rule_sheet', DEFAULT_RULE_SHEET)}」が空でした。"
                   "上の「🔄 ルール一覧を最新にする」を押してください。")
    else:
        heads = _r["見出し"] or RULE_COLS
        df = pd.DataFrame([(r + [""] * len(heads))[:len(heads)] for r in _r["行"]],
                          columns=[h or f"列{i + 1}" for i, h in enumerate(heads)])
        st.success(f"✅ いま **{len(df)}本** のルールが登録されています。")

        q = st.text_input("🔍 しぼり込み（列名・言葉で探せます）", key="pc_q",
                          placeholder="例：携帯番号／空欄／NG")
        if q.strip():
            _mask = df.apply(lambda row: row.astype(str).str.contains(q.strip(), case=False,
                                                                      na=False).any(), axis=1)
            df = df[_mask]
            st.caption(f"{len(df)}本 が当てはまりました。")

        # 📑 シートごとに畳む（Nチェック／Eチェック／Gチェック が混ざると読みにくい）
        _sheet_col = heads[0] if heads else "シート名"
        if _sheet_col in df.columns:
            for name, part in df.groupby(_sheet_col, sort=False):
                with st.expander(f"**{name}**（{len(part)}本）", expanded=len(df) <= 30):
                    st.dataframe(part.drop(columns=[_sheet_col]),
                                 use_container_width=True, hide_index=True)
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)

        st.download_button("⬇️ ルール一覧をCSVで落とす",
                           data=df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"エントリー前DC_ルール一覧_{sms_runner.today_stamp()}.csv",
                           mime="text/csv", key="pc_dl")

    st.caption("💡 ルールそのものを足す・直すのは、いまのところ**スプレッドシートの条件付き書式**です。"
               "直したら、上の「🔄 ルール一覧を最新にする」を押すと、ここの表示も追いつきます。")

# ==========================================
# 👀 誤りが出ている案件
# ==========================================
_watch = cfg.get("watch_tabs", []) or []
with st.container(border=True):
    theme.section_title("👀", "誤りが出ている案件")
    if not _watch:
        st.info("確認するシートが登録されていません（⚙️設定で登録できます）。")
        st.caption("⚠️ **色が付いている行そのものは、アプリからは読めません。**"
                   "Googleの仕組み上、条件付き書式は「ルール」は取れても"
                   "「結果の色」は取れないためです。"
                   "誤りの一覧は、スプシのGAS側で**1枚のシートに書き出して**ください。"
                   "そのシートをここに登録すれば、中身を出して消し込めます。")
    else:
        wkey = "pc_watch"
        if st.button("🔍 確認する", type="primary", disabled=not gc, key="pc_check"):
            with st.spinner("確認するシートを読んでいます..."):
                st.session_state[wkey] = watch_ui.read(gc, _url, _watch)
            st.rerun()
        found = st.session_state.get(wkey)
        if found is None:
            st.caption("まだ確認していません。")
        else:
            watch_ui.render(gc, _url, "エントリー前DC", found, wkey, "pc",
                            work_root=WORK_ROOT, tabs=_watch,
                            fix_where="スプレッドシート")

st.divider()
st.caption("💻 ルール一覧の作り直しはスプシのGASが行います（ブラウザは要りません）。")
