"""
🔎 エントリー前DC（エントリーする前の、内容チェック）

SFに登録したデータに誤りが無いかを、エントリーの前に確かめる工程。

【困っていたこと】
  - ルールを足すのが面倒（条件付き書式の画面で数式を書く）
  - **何を登録してあるのか、誰も覚えていない**
  - 色を探しに行かないと、どこがミスか分からない

【この画面の考え方】
  ⭐ **ルールの正本はスプシの「DCルール表」**（1行＝1ルール・日本語のルール名とNGの理由つき）。
     条件付き書式は正本にしない（数式しか残らず、何のルールか分からなくなるため）。
  ⭐ **ミスだけを理由つきで拾う**：スプシのGAS（`gas/エンカンAI_DC.gs` の `enkanDcRun`）が
     ルール表の数式で全案件を判定し、NGの行だけを「DCエラー一覧」に書き出す。アプリはそれを読む。
     ⚠️ 色は Google の仕組み上どこからも読めないので、**同じ数式を判定用の隠しシートで計算させる**。
  ⭐ **アプリから新しいルールを入れられる**：日本語で書く → AIが数式にする →
     「いまのデータで試す」（`enkanDcTry`）で何件引っかかるか見てから登録。
  ⭐ **「このルールどうなってる？」が聞ける**：AIが列の名前を使って説明する。

⚠️ 判定をアプリ（Python）に書かない。スプシの数式と二重になり、必ず食い違う。
"""
import json
import re
import time

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import auto_jobs
import characters as ch
import common_robots
import gas_deploy
import slack_notify
import sms_runner
import theme

st.set_page_config(page_title="エントリー前DC - エンカンAI", layout="wide")

theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🔎", "エントリー前DC",
                  "エントリーする前に、SFに登録したデータに誤りが無いかを確かめます。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

SETTINGS_ID = "__precheck__"
DEFAULT_CHECK_TABS = ["Nチェック", "Eチェック", "Gチェック"]
# ⭐ 名前の正本は auto_jobs（時間指定の自動実行と同じものを見る＝片方だけ直して食い違わない）
CHECK_LABELS = auto_jobs.PRECHECK_LABELS
# ① SFコネクタで更新するシート（チェック用シートは、この貼り付けシートを映しているだけ）
DEFAULT_REFRESH_TABS = auto_jobs.PRECHECK_REFRESH_TABS
DEFAULT_REFRESH_ROBOT = auto_jobs.DEFAULT_REFRESH_ROBOT
# スプシ側（gas/エンカンAI_DC.gs）と同じ名前。変えるときは両方直す。
RULE_SHEET = "DCルール表"
OUT_SHEET = "DCエラー一覧"
TRY_SHEET = "DC試し"
TRY_OUT_SHEET = "DC試し結果"
FORMULA_COL = "条件（2行目の形の数式）"
RULE_HEADS = ["ID", "ON", "対象", "種類", "ルール名", "NGの理由", "見る列", FORMULA_COL, "メモ", "もと", "登録日"]
# 画面で直せる列（数式は「＋ ルールを足す」で作る。表で1文字消すと全部がNGになりうるため）
EDITABLE = ["ON", "種類", "ルール名", "NGの理由", "メモ"]
# LL（電気・ガス）のエントリーの締め。ルール表の黄信号・赤信号の数式（TIME(18,0,0)）と同じにする。
# ⭐ 正本は auto_jobs（Slackの文もここを見る）。
ENTRY_CUTOFF = auto_jobs.PRECHECK_CUTOFF


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


def _open(gc, sheet_url: str):
    return gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)


@st.cache_data(ttl=120, show_spinner=False)
def _tabs_of(_gc, sheet_url: str):
    return [w.title for w in _open(_gc, sheet_url).worksheets()]


@st.cache_data(ttl=600, show_spinner=False)
def _columns_of(_gc, sheet_url: str, tab: str):
    """チェック用シートの「列の記号 → 見出し」。AIに数式を作らせる・説明させるときに渡す。"""
    from gspread.utils import rowcol_to_a1
    heads = _open(_gc, sheet_url).worksheet(tab).row_values(1)
    return [(re.sub(r"\d", "", rowcol_to_a1(1, i + 1)), str(h).strip())
            for i, h in enumerate(heads)]


@st.cache_data(ttl=120, show_spinner=False)
def _preview(sheet_url: str, tab: str, n: int = 3) -> pd.DataFrame:
    """チェック用シートの見出し＋先頭 n 件（案件番号のある行）。列名は「K：ガス立合希望日」の形。"""
    from gspread.utils import rowcol_to_a1
    values = _open(gc, sheet_url).worksheet(tab).get_all_values()
    if not values:
        return pd.DataFrame()
    heads = values[0]
    # 見出しの無い列が末尾に続くことがあるので、見出しか値のある最後の列までにする
    width = max([i + 1 for i, h in enumerate(heads) if str(h).strip()] or [0])
    key = heads.index("案件番号") if "案件番号" in heads else None
    rows = [r for r in values[1:] if (key is None and any(str(x).strip() for x in r))
            or (key is not None and len(r) > key and str(r[key]).strip())][:n]
    cols, seen = [], set()
    for i in range(width):
        label = f"{re.sub(r'[0-9]', '', rowcol_to_a1(1, i + 1))}：{str(heads[i]).strip() or '（見出しなし）'}"
        cols.append(label if label not in seen else f"{label}（{i + 1}）")
        seen.add(label)
    return pd.DataFrame([(r + [""] * width)[:width] for r in rows], columns=cols)


def _do_refresh(sheet_url: str, tabs, robot_name: str):
    """① SFコネクタで貼り付けシートを更新する（オートコール投入・データローダーと同じしくみ）。

    ⭐ 中身は `auto_jobs.precheck_refresh` 1か所（時間指定の自動実行も同じものを通る）。
    ⚠️ 開く先は**必ずこのスプシ**を渡す。渡さないと、ロボットは録画したときのスプシを開いて
       そちらを更新してしまう（オートコール投入で実際に起きた）。
    """
    return auto_jobs.precheck_refresh(gc, {"sheet_url": sheet_url, "refresh_tabs": list(tabs),
                                           "refresh_robot": robot_name})


def _read_sheet(sheet_url: str, tab: str) -> pd.DataFrame:
    """シートを表で読む（見出しは1行目）。無ければ空の表。"""
    try:
        values = _open(gc, sheet_url).worksheet(tab).get_all_values()
    except Exception:
        return pd.DataFrame()
    if not values:
        return pd.DataFrame()
    heads = [str(h).strip() for h in values[0]]
    rows = [(r + [""] * len(heads))[:len(heads)] for r in values[1:] if any(str(x).strip() for x in r)]
    return pd.DataFrame(rows, columns=heads)


def _gas(action_build: str, timeout: int = 600):
    return sms_runner.run_gas_action(str(cfg.get("gas_url", "") or ""),
                                     str(cfg.get("gas_token", "") or ""),
                                     action="build", timeout=timeout, build=action_build)


def _gemini(prompt: str, as_json: bool = False):
    """AIに頼む。⚠️ 無料枠は1日20回ほど。使い切ったら、そう伝えて止める。"""
    import google.generativeai as genai
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-flash")
    try:
        resp = model.generate_content(
            prompt, generation_config={"response_mime_type": "application/json"} if as_json else None)
    except Exception as e:
        if "429" in str(e) or "quota" in str(e).lower():
            raise RuntimeError("AIの今日の無料枠を使い切りました。明日もう一度押してください。")
        raise
    return json.loads(resp.text) if as_json else resp.text


DELETED_KEY = "deleted_rules"   # 消したルールの控え（⚠️ 消すと何を消したか分からなくなるため残す）
DELETED_LIMIT = 50


def _deleted_rules() -> list:
    return list(cfg.get(DELETED_KEY, []) or [])


def _write_deleted(rows: list):
    """控えを書き替える。⚠️ 書く直前に読み直して差し替える（別のPC・別のタブの直しを踏み潰さない）。"""
    latest = _load()
    latest[DELETED_KEY] = rows[-DELETED_LIMIT:]
    _save(latest)
    cfg[DELETED_KEY] = latest[DELETED_KEY]


def _bump_rules_ver():
    """表を作り直す合図。⚠️ 行が増減したのに同じキーのままだと、st.data_editor が
    前のチェック（🗑）を**行の位置**で覚えていて、別のルールに付け替わる。"""
    st.session_state["pc_rule_ver"] = st.session_state.get("pc_rule_ver", 0) + 1


def _is_on(v) -> bool:
    return str(v).strip().upper() in ("TRUE", "✅", "1", "ON")


def _columns_text(tab: str) -> str:
    return "\n".join(f"{l}列：{h or '（見出しなし）'}" for l, h in _columns_of(gc, _url, tab))


cfg = _load()
gc = _get_gspread_client()

if not gc:
    st.warning("🔑 接続キー **GOOGLE_SERVICE_ACCOUNT_JSON** が未設定です（管理者に設定を依頼してください）。")

ch.guide("operate",
         "ここは<b>エントリーの前に、SFのデータにミスが無いか確かめる</b>部屋。"
         "更新して「チェックする」を押すと、<b>ミスがある案件だけ</b>を理由つきで出すよ。")

# ==========================================
# ⚙️ 設定（最初に1回だけ）
# ==========================================
with st.expander("⚙️ 設定（最初に1回だけ／ふだんは触りません）",
                 expanded=not str(cfg.get("sheet_url", "")).strip()):
    sheet_url = st.text_input("チェック用スプレッドシートのURL",
                              value=cfg.get("sheet_url", ""),
                              placeholder="https://docs.google.com/spreadsheets/d/...",
                              key="pc_url")
    st.caption("※ サービスアカウントのメールアドレスを、このスプシの**編集者**に追加してください"
               "（ルール表への登録とチェックの結果の書き出しに使います）。")

    tabs = []
    if gc and sheet_url.strip():
        try:
            tabs = _tabs_of(gc, sheet_url.strip())
        except Exception as e:
            st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")

    st.markdown("**① SFコネクタで更新するシート**")
    st.caption("チェック用シート（Nチェック など）は、貼り付けシートをそのまま映しています。"
               "**貼り付けシートを最新にしてから**チェックします。")
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

    st.markdown("**② チェックするGAS（スプシの中で判定します）**")
    st.caption(f"スプシの「{RULE_SHEET}」のルールで全案件を判定し、ミスだけを「{OUT_SHEET}」に書き出します。")
    _auto = gas_deploy.render(
        "pc_gas",
        {"gas_script_url": cfg.get("gas_script_url", ""),
         "gas_url": cfg.get("gas_url", ""), "gas_token": cfg.get("gas_token", ""),
         "gas_deployment_id": cfg.get("gas_deployment_id", "")})
    if st.button("🔌 つないで中身を見る", key="pc_inspect"):
        _u = str(_auto.get("gas_url", "") or "").strip()
        if not _u:
            st.warning("まず上の「🚀 GASを入れて公開する」を押してください。")
        else:
            ok, data = sms_runner.run_gas_action(_u, str(_auto.get("gas_token", "") or ""),
                                                 "inspect", timeout=90)
            if not ok:
                st.error(f"❌ {data}")
            elif "enkanDcRun" not in ((data or {}).get("functions") or []):
                st.error("❌ つながりましたが、このスプシのGASに**チェックの処理（enkanDcRun）がありません**。"
                         "開発者に「エンカンAI_DC.gs を入れて」と伝えてください。")
            else:
                st.success(f"✅ つながりました（{(data or {}).get('name', '')}）。チェックの処理も入っています。")

    st.markdown("**③ 結果の送り先（Slack）**")
    st.caption("⭐ **ネットとライフラインで、送り先を分けられます**（見る人が違うため）。"
               "同じ送り先にしたものは1通にまとめて送ります。"
               "送り先（グループ）の登録は「⚙️ その他設定」の🔔Slack通知の送り先から。")
    try:
        _groups = sorted(slack_notify.extra_info(sb=supabase).keys())
    except Exception:
        _groups = []
    _cur_to = dict(cfg.get(auto_jobs.PRECHECK_SLACK_KEY, {}) or {})
    _opts = [""] + _groups
    _to_cols = st.columns(len(auto_jobs.PRECHECK_CHECK_TABS))
    slack_to = {}
    for _i, _tab in enumerate(auto_jobs.PRECHECK_CHECK_TABS):
        _now = str(_cur_to.get(_tab, "") or "")
        with _to_cols[_i]:
            slack_to[_tab] = st.selectbox(
                f"{CHECK_LABELS.get(_tab, _tab)}（{_tab}）の結果", _opts,
                index=_opts.index(_now) if _now in _opts else 0,
                format_func=lambda n: n or "（いつもの送り先）", key=f"pc_slack_{_tab}")
    if not _groups:
        st.caption("※ ほかの送り先がまだ登録されていないので、いまは全部「いつもの送り先」に届きます。")
    _miss = [f"{CHECK_LABELS.get(t, t)}→{n}" for t, n in _cur_to.items()
             if str(n or "").strip() and str(n) not in _groups]
    if _miss:
        st.warning("⚠️ 保存してある送り先が見つかりません（消された？）："
                   + "／".join(_miss) + "　このままだと、そのぶんは送れません。")

    if st.button("💾 保存", type="primary", key="pc_save"):
        cfg.update({
            "sheet_url": sheet_url.strip(),
            "refresh_tabs": list(refresh_tabs), "refresh_robot": refresh_robot,
            auto_jobs.PRECHECK_SLACK_KEY: {k: v for k, v in slack_to.items() if v},
            "gas_script_url": str(_auto.get("gas_script_url", "") or ""),
            "gas_url": str(_auto.get("gas_url", "") or ""),
            "gas_token": str(_auto.get("gas_token", "") or ""),
            "gas_deployment_id": str(_auto.get("gas_deployment_id", "") or ""),
        })
        _save(cfg)
        st.success("保存しました。")
        st.rerun()

_url = str(cfg.get("sheet_url", "") or "").strip()
if not _url:
    st.info("まず上の⚙️設定で、チェック用スプレッドシートのURLを入れてください。")
    st.stop()
_has_gas = bool(str(cfg.get("gas_url", "") or "").strip())

st.markdown(f"[📄 スプレッドシートを開く]({_url})")

# ==========================================
# ▶ ①と②を続けて通す（時間指定の自動実行と、まったく同じ中身）
# ==========================================
with st.container(border=True):
    theme.section_title("▶", "ぜんぶ実行する（更新 → チェック）")
    a1, a2 = st.columns([1, 3])
    with a1:
        _go_all = st.button("▶ ぜんぶ実行する", type="primary", use_container_width=True,
                            disabled=not (gc and _has_gas), key="pc_all")
    with a2:
        _lastrun = str(cfg.get("last_run", "") or "")
        _plan = "／".join(f"{auto_jobs.precheck_tab_label(t)}→{n or 'いつもの送り先'}"
                          for n, t in auto_jobs.precheck_slack_plan(cfg))
        st.caption("①の更新 → ②のチェックを続けて通します。"
                   "中身は「⏰ 時間指定の自動実行」とまったく同じものです"
                   f"（時間指定で動かしたときは、結果をSlackに送ります：{_plan}）。"
                   + (f"　／　前回：{_lastrun}" if _lastrun else ""))
    if _go_all:
        with st.spinner("🤖 シートを更新して、チェックしています..."):
            # ⚠️ 画面から押したときはSlackに送らない（人が見ているため）
            _res = auto_jobs.run_precheck(supabase, gc, cfg, notify=False)
        for _s in _res["工程"]:
            st.markdown(f"- {_s['結果']} **{_s['工程']}**：{_s['中身']}")
        if _res["結果"] == "完了":
            st.session_state["pc_result"] = _read_sheet(_url, OUT_SHEET)

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
            st.session_state["pc_ref"] = {"ok": _ok, "log": _log, "表": _tbl}
        _r = st.session_state.get("pc_ref")
        if _r:
            (st.success if _r["ok"] else st.error)(
                "✅ 更新しました。下の②でチェックしてください。" if _r["ok"] else "❌ 更新でつまずきました。")
            if _r.get("表") is not None:
                st.dataframe(pd.DataFrame(_r["表"]), use_container_width=True, hide_index=True)
            with st.expander("実行ログ", expanded=not _r["ok"]):
                st.text(str(_r["log"])[-4000:])

# ==========================================
# ② チェックする → ミスだけを理由つきで出す
# ==========================================
with st.container(border=True):
    theme.section_title("②", "チェックする（ミスがある案件だけを出します）")
    k1, k2, k3 = st.columns([1, 1, 2])
    with k1:
        _go_chk = st.button("🔍 チェックする", type="primary", use_container_width=True,
                            disabled=not (gc and _has_gas), key="pc_check")
    with k2:
        _go_last = st.button("📖 前回の結果を見る", use_container_width=True,
                             disabled=not gc, key="pc_last")
    with k3:
        st.caption(f"「{RULE_SHEET}」でONのルールで、全案件を判定します（数十秒かかります）。"
                   "**スプシには何も書き替えません**（結果の一覧を作るだけ）。")
    if _go_chk:
        with st.spinner("スプシでチェックしています..."):
            # ⭐ 中身は auto_jobs（時間指定の自動実行と同じもの）
            ok, data = auto_jobs.precheck_check(cfg)
        if not ok:
            st.error(f"❌ チェックできませんでした：{data}")
        else:
            st.session_state["pc_result"] = _read_sheet(_url, OUT_SHEET)
    if _go_last:
        st.session_state["pc_result"] = _read_sheet(_url, OUT_SHEET)

    df = st.session_state.get("pc_result")
    if df is None:
        st.caption("まだチェックしていません。")
    elif df.empty or "ルールID" not in df.columns:
        st.warning(f"「{OUT_SHEET}」が読めませんでした。先に「🔍 チェックする」を押してください。")
    else:
        _when = str(df["実行日時"].iloc[0]) if len(df) else ""
        # 🕕 黄信号・赤信号は「チェックした時刻が18時（LLのエントリーの締め）の前か後か」で変わる。
        #    締めの前後にチェックすると「あれ？どっちだっけ」になるので、時刻と残り時間を先に出す。
        _m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2})", _when)
        if _m:
            _y, _mo, _d, _hh, _mm = map(int, _m.groups())
            _mins = (ENTRY_CUTOFF[0] * 60 + ENTRY_CUTOFF[1]) - (_hh * 60 + _mm)
            _cut = f"{ENTRY_CUTOFF[0]}:{ENTRY_CUTOFF[1]:02d}"
            _is_today = (_y, _mo, _d) == tuple(time.localtime()[:3])
            _span = (lambda m: f"{m // 60}時間{m % 60}分" if m >= 60 else f"{m}分")(abs(_mins))
            _day = "" if _is_today else f"{_mo}/{_d} の "
            if _mins > 0:
                st.info(f"🕕 **{_day}{_hh}:{_mm:02d} にチェックした結果です。LLのエントリーの締め（{_cut}）まで あと{_span}**"
                        "　→ 🟡 黄信号は、締めまでにエントリーすればセーフ。")
            else:
                st.warning(f"🕕 **{_day}{_hh}:{_mm:02d} にチェックした結果です。LLのエントリーの締め（{_cut}）を{_span} 過ぎています**"
                           "　→ 🟡 だったものは、登録日が空なら 🔴（今からでは間に合わない）になっています。")
            if not _is_today:
                st.caption("⚠️ 今日のチェックではありません。「🔍 チェックする」を押し直してください。")
        warn = df[df["種類"].astype(str).str.startswith("⚠️")]
        hits = df[(df["ルールID"].astype(str) != "") & ~df["種類"].astype(str).str.startswith("⚠️")]
        if hits.empty:
            st.success(f"✅ ミスはありませんでした（{_when} のチェック）。")
        else:
            _cases = hits.groupby(["対象", "行"]).ngroups
            m1, m2, m3 = st.columns(3)
            m1.metric("ミスがある案件", f"{_cases}件")
            m2.metric("NG", f"{int((hits['種類'] == 'NG').sum())}か所")
            m3.metric("注意（ギリギリなど）", f"{int((hits['種類'] == '注意').sum())}か所")
            st.caption(f"{_when} のチェック結果です。SFで直したら、①で更新してもう一度チェックすると消えます。")
            for tab, part in hits.groupby("対象", sort=False):
                with st.expander(f"**{CHECK_LABELS.get(tab, tab)}**（{tab}）："
                                 f"{part.groupby('行').ngroups}件", expanded=True):
                    view = (part.assign(**{"理由": "【" + part["種類"] + "】" + part["NGの理由"]})
                            .groupby(["行", "案件番号", "個人名"], sort=False)
                            .agg({"理由": "\n".join, "見る列の中身": "\n".join})
                            .reset_index().drop(columns=["行"]))
                    st.dataframe(view, use_container_width=True, hide_index=True,
                                 column_config={"理由": st.column_config.TextColumn(width="large"),
                                                "見る列の中身": st.column_config.TextColumn(width="large")})
            st.download_button("⬇️ ミスの一覧をCSVで落とす",
                               data=hits.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"エントリー前DC_ミス一覧_{sms_runner.today_stamp()}.csv",
                               mime="text/csv", key="pc_dl")
        if not warn.empty:
            st.warning("⚠️ **判定できなかったルールがあります**（ルールの数式か、シートの名前を確かめてください）。")
            st.dataframe(warn[["対象", "ルールID", "ルール名", "NGの理由", "見る列の中身"]]
                         .rename(columns={"NGの理由": "何が起きたか", "見る列の中身": "数式"}),
                         use_container_width=True, hide_index=True)

# ==========================================
# ③ いま何をチェックしているか（ルール表）
# ==========================================
with st.container(border=True):
    theme.section_title("📋", "いま何をチェックしているか（ルール表）")
    st.caption(f"ルールの本物はスプシの「{RULE_SHEET}」です。ここで ON／OFF・名前・理由を直せます。"
               "⚠️ スプシの条件付き書式を直しても、ここのチェックには効きません。")
    if st.button("📖 ルール表を読む" if "pc_rules" not in st.session_state else "🔄 読み直す",
                 key="pc_rules_load", disabled=not gc):
        st.session_state["pc_rules"] = _read_sheet(_url, RULE_SHEET)
        st.session_state.pop("pc_explain", None)
    rules = st.session_state.get("pc_rules")
    if rules is None:
        st.caption("まだ読んでいません。")
    elif rules.empty or "ID" not in rules.columns:
        st.warning(f"「{RULE_SHEET}」シートが見つからないか、空でした。")
    else:
        rules = rules.copy()
        rules["ON"] = rules["ON"].map(_is_on)
        f1, f2, f3 = st.columns([1, 1, 2])
        with f1:
            _tabsel = st.selectbox("対象", ["すべて"] + list(dict.fromkeys(rules["対象"])), key="pc_rf_tab",
                                   format_func=lambda t: t if t == "すべて" else f"{CHECK_LABELS.get(t, t)}（{t}）")
        with f2:
            _only_warn = st.checkbox("⚠️ 要確認だけ", key="pc_rf_warn",
                                     help="メモに ⚠️ が付いているルール（列ずれの疑いなど）だけ出します")
        with f3:
            _q = st.text_input("🔍 しぼり込み", key="pc_rf_q", placeholder="例：SB光／郵便番号／期限")
        view = rules
        if _tabsel != "すべて":
            view = view[view["対象"] == _tabsel]
        if _only_warn:
            view = view[view["メモ"].astype(str).str.contains("⚠️")]
        if _q.strip():
            view = view[view.apply(lambda r: r.astype(str).str.contains(_q.strip(), case=False).any(), axis=1)]
        st.caption(f"ON {int(rules['ON'].sum())}本 ／ 全 {len(rules)}本（表示 {len(view)}本）")
        view = view.copy()
        view.insert(0, "🗑", False)
        _rv = st.session_state.get("pc_rule_ver", 0)
        edited = st.data_editor(
            view[["🗑", "ID", "ON", "対象", "種類", "ルール名", "NGの理由", "メモ"]],
            use_container_width=True, hide_index=True, key=f"pc_rule_editor{_rv}",
            disabled=["ID", "対象"],
            column_config={"🗑": st.column_config.CheckboxColumn(
                               "🗑 消す", width="small",
                               help="チェックしてから、下の「🗑 選んだルールを消す」を押します"),
                           "ON": st.column_config.CheckboxColumn(width="small"),
                           "種類": st.column_config.SelectboxColumn(options=["NG", "注意"], width="small"),
                           "NGの理由": st.column_config.TextColumn(width="large"),
                           "メモ": st.column_config.TextColumn(width="medium")})
        # 変わったセルだけを書き戻す（表ごと上書きすると、数式の列や他の人の直しを消す）
        changes = []
        for _, row in edited.iterrows():
            old = rules[rules["ID"] == row["ID"]].iloc[0]
            for col in EDITABLE:
                if str(row[col]) != str(old[col]):
                    changes.append((row["ID"], col, row[col]))
        if changes:
            st.info(f"✏️ {len(changes)}か所 変えました（まだスプシに書いていません）。")
            if st.button("💾 ルール表に書き戻す", type="primary", key="pc_rule_save"):
                try:
                    ws = _open(gc, _url).worksheet(RULE_SHEET)
                    values = ws.get_all_values()
                    heads = [str(h).strip() for h in values[0]]
                    ids = [r[heads.index("ID")] if r else "" for r in values]
                    from gspread.utils import rowcol_to_a1
                    batch = []
                    for rid, col, val in changes:
                        if rid not in ids or col not in heads:
                            continue
                        batch.append({"range": rowcol_to_a1(ids.index(rid) + 1, heads.index(col) + 1),
                                      "values": [[bool(val) if col == "ON" else str(val)]]})
                    ws.batch_update(batch, value_input_option="RAW")
                    st.session_state["pc_rules"] = _read_sheet(_url, RULE_SHEET)
                    _bump_rules_ver()
                    st.success(f"✅ {len(batch)}か所 書き戻しました。次のチェックから効きます。")
                    st.rerun()
                except Exception as e:
                    st.error(f"書き戻せませんでした：{str(e)[:200]}")

        # 🗑 いらなくなったルールを、行ごと消す
        # ⚠️ 取り消せない操作なので、確認のチェックを入れないと押せない。
        #    消した中身は控えに残して、戻せるようにする。
        _gone = [str(r["ID"]) for _, r in edited.iterrows() if bool(r.get("🗑"))]
        if _gone:
            _dead = rules[rules["ID"].astype(str).isin(_gone)]
            st.warning(f"🗑 次の {len(_dead)}本 を、ルール表から**行ごと**消します。"
                       "しばらく止めたいだけなら、消さずに **ON のチェックを外す**ほうが安全です。")
            st.dataframe(_dead[["ID", "対象", "種類", "ルール名", "NGの理由"]],
                         use_container_width=True, hide_index=True)
            _ok_del = st.checkbox(f"上の {len(_dead)}本 を消してよいことを確かめました",
                                  key=f"pc_rule_del_ok{_rv}")
            if st.button("🗑 選んだルールを消す", key=f"pc_rule_del{_rv}", disabled=not _ok_del):
                try:
                    ws = _open(gc, _url).worksheet(RULE_SHEET)
                    # ⚠️ 消す直前に読み直し、IDとルール名がそっくり同じ行だけを消す
                    #    （画面に出したときの行番号で消すと、その間に足された／消された行があると別の行を消す）
                    values = ws.get_all_values()
                    heads = [str(h).strip() for h in values[0]]
                    _ic, _nc = heads.index("ID"), heads.index("ルール名")

                    def _cell(row, i):
                        return str((list(row) + [""] * len(heads))[i]).strip()

                    targets, missing = [], []
                    for rid in _gone:
                        old_row = rules[rules["ID"].astype(str) == rid].iloc[0]
                        hit = [i for i, r in enumerate(values)
                               if i and _cell(r, _ic) == rid
                               and _cell(r, _nc) == str(old_row["ルール名"]).strip()]
                        if len(hit) == 1:
                            targets.append((hit[0],
                                            (list(values[hit[0]]) + [""] * len(heads))[:len(heads)],
                                            str(old_row["対象"]), str(old_row["ルール名"])))
                        else:
                            missing.append(rid)
                    if missing:
                        st.error("❌ 見つからない（または同じIDが2つある）ので、**1本も消しませんでした**："
                                 + "／".join(missing) + "。「🔄 読み直す」を押してから、もう一度お願いします。")
                    else:
                        _write_deleted(_deleted_rules() + [
                            {"uid": f"{v[_ic]}-{time.time():.0f}-{n}", "ID": v[_ic], "ルール名": nm,
                             "対象": tgt, "消した日": time.strftime("%Y/%m/%d"),
                             "見出し": heads, "中身": v}
                            for n, (_i, v, tgt, nm) in enumerate(targets)])
                        # ⚠️ 下から消す（上から消すと、下の行の番号がずれる）
                        for _i, _v, _t, _n in sorted(targets, key=lambda x: x[0], reverse=True):
                            ws.delete_rows(_i + 1)
                        st.session_state["pc_rules"] = _read_sheet(_url, RULE_SHEET)
                        _bump_rules_ver()
                        st.success(f"✅ {len(targets)}本を消しました。次のチェックから効きます。"
                                   "（控えは下の「🗑 消したルール」から戻せます）")
                        st.rerun()
                except Exception as e:
                    st.error(f"消せませんでした：{str(e)[:200]}")

        _hist = _deleted_rules()
        if _hist:
            with st.expander(f"🗑 消したルール（控え・新しい順 {len(_hist)}本）"):
                st.caption("消した中身をここに残しています。「↩ 戻す」でルール表のいちばん下に付け直します"
                           f"（IDもそのまま）。⚠️ 控えは新しい順に {DELETED_LIMIT}本 まで。")
                for _d in list(reversed(_hist)):
                    h1, h2 = st.columns([5, 1])
                    with h1:
                        st.write(f"**{_d.get('ID', '')}**：{_d.get('ルール名', '')}　"
                                 f"（{CHECK_LABELS.get(_d.get('対象', ''), _d.get('対象', ''))}・"
                                 f"{_d.get('消した日', '')} に消しました）")
                    with h2:
                        if st.button("↩ 戻す", key=f"pc_rule_undo_{_d.get('uid', _d.get('ID'))}"):
                            try:
                                ws = _open(gc, _url).worksheet(RULE_SHEET)
                                _now = ws.get_all_values()
                                _hd = [str(x).strip() for x in _now[0]]
                                if any(str((list(r) + [""] * len(_hd))[_hd.index("ID")]).strip()
                                       == str(_d.get("ID", "")) for r in _now[1:]):
                                    st.error(f"❌ {_d.get('ID')} は、もうルール表にあります（戻しませんでした）。")
                                else:
                                    # 見出しの並びが変わっていても、見出しの名前で入れ直す
                                    _was = dict(zip(_d.get("見出し", []), _d.get("中身", [])))
                                    ws.append_row([_was.get(h, "") for h in _hd], value_input_option="RAW")
                                    _write_deleted([x for x in _deleted_rules()
                                                    if x.get("uid") != _d.get("uid")])
                                    st.session_state["pc_rules"] = _read_sheet(_url, RULE_SHEET)
                                    _bump_rules_ver()
                                    st.success(f"✅ {_d.get('ID')} を戻しました。")
                                    st.rerun()
                            except Exception as e:
                                st.error(f"戻せませんでした：{str(e)[:200]}")

        # 💬 このルールどうなってる？
        st.markdown("**💬 このルールどうなってる？**")
        e1, e2 = st.columns([3, 1])
        with e1:
            _pick = st.selectbox("聞きたいルール", list(view["ID"]), key="pc_explain_pick",
                                 format_func=lambda i: f"{i}：{rules[rules['ID'] == i]['ルール名'].iloc[0]}")
        _rule = rules[rules["ID"] == _pick].iloc[0] if _pick else None
        with e2:
            st.write("")
            _go_exp = st.button("💬 AIに聞く", use_container_width=True, key="pc_explain_go",
                                disabled=_rule is None)
        if _rule is not None:
            st.caption(f"見る列：{_rule['見る列']}　／　数式：`{_rule[FORMULA_COL]}`")
            if _rule.get("メモ"):
                st.caption(f"メモ：{_rule['メモ']}")
        _exp = st.session_state.setdefault("pc_explain", {})
        if _go_exp and _rule is not None:
            try:
                with st.spinner("AIに聞いています..."):
                    _exp[_pick] = _gemini(
                        "事務の担当者向けに、スプレッドシートのチェックルールを説明してください。\n"
                        "次の数式が TRUE になる（＝NGになる）のは、どんな案件のときかを、"
                        "**列の記号ではなく列の名前を使って**、箇条書き3〜5行で書いてください。"
                        "挨拶・前置き・まとめの文は書かない。\n"
                        "見出しの無い列を見ている、ルール名と狙いが合っていない、などがあるときだけ、"
                        "最後に「⚠️ 気になる点：」を1〜2行で足してください（無ければ書かない）。\n\n"
                        f"【シート】{_rule['対象']}\n【ルール名】{_rule['ルール名']}\n"
                        f"【数式（2行目の形）】{_rule[FORMULA_COL]}\n\n【列の一覧】\n{_columns_text(_rule['対象'])}")
            except Exception as e:
                st.error(str(e)[:200])
        if _pick in _exp:
            st.info(_exp[_pick])

# ==========================================
# ④ 新しいルールを足す
# ==========================================
with st.container(border=True):
    theme.section_title("＋", "新しいルールを足す")
    st.caption("どんなときにミスとしたいかを日本語で書くと、AIが数式にします。"
               "**登録する前に、いまのデータで何件引っかかるか試せます**。")
    # ⚠️ 判定用の隠しシート（DC判定_Nチェック）も「チェック」で終わるので外す
    _targets = [t for t in (tabs or DEFAULT_CHECK_TABS)
                if t.endswith("チェック") and not t.startswith("DC")]
    n1, n2 = st.columns([1, 3])
    with n1:
        new_tab = st.selectbox("どのチェック", _targets or DEFAULT_CHECK_TABS, key="pc_new_tab",
                               format_func=lambda t: f"{CHECK_LABELS.get(t, t)}（{t}）")
    # 👀 選んだシートの見出しと中身（先頭3件）を見ながら書けるように。
    #    列の記号（K など）も一緒に出す：AIが作った数式の「見る列」と見比べられる。
    with st.expander(f"👀 {new_tab} の項目と中身（先頭3件・横にスクロールできます）", expanded=True):
        try:
            _pv = _preview(_url, new_tab)
            if _pv.empty:
                st.caption("案件がまだありません（見出しだけ出します）。")
            st.dataframe(_pv, use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(f"読めませんでした：{str(e)[:120]}")
    with n2:
        new_text = st.text_area("どんなときミスにしたい？", key="pc_new_text", height=80,
                                placeholder="例：商品がSB光で、乗換前キャリアが入っているのに、選択プランCPが乗換CPになっていない")
    if st.button("✨ AIに数式を作ってもらう", key="pc_new_ai",
                 disabled=not (gc and new_text.strip())):
        _ex = st.session_state.get("pc_rules")
        _samples = ""
        if _ex is not None and not _ex.empty and FORMULA_COL in _ex.columns:
            _samples = "\n".join(f"- {r['ルール名']}：{r[FORMULA_COL]}"
                                 for _, r in _ex[_ex["対象"] == new_tab].head(6).iterrows())
        try:
            with st.spinner("AIが数式を作っています..."):
                out = _gemini(
                    "Googleスプレッドシートの条件付き書式で使う数式を作ってください。\n"
                    "決まり：\n"
                    "- 見出しは1行目、データは2行目から。**2行目の形**で書く（例：$J2=\"SB光\"）。列には必ず $ を付ける。\n"
                    "- NGのとき TRUE になる数式にする。先頭の = は付けない。\n"
                    "- 文字の一部を含むかは REGEXMATCH を使う。空欄は =\"\" で見る。\n"
                    "- 列の一覧に無い列は使わない。\n"
                    "次のJSONだけを返してください："
                    '{"ルール名": "短い名前", "NGの理由": "担当者に出す1文（です・ます）", '
                    '"種類": "NG か 注意", "見る列": "K,L のように使う列の記号", "数式": "…", '
                    '"確認してほしいこと": "あいまいで決めつけた点があれば。無ければ空"}\n\n'
                    f"【シート】{new_tab}\n【ミスにしたいこと】{new_text.strip()}\n\n"
                    f"【列の一覧】\n{_columns_text(new_tab)}\n\n【このシートの今のルールの例】\n{_samples}",
                    as_json=True)
            st.session_state["pc_new"] = out
            for k, v in (("pc_new_name", "ルール名"), ("pc_new_why", "NGの理由"),
                         ("pc_new_cols", "見る列"), ("pc_new_formula", "数式")):
                st.session_state[k] = str(out.get(v, "") or "").lstrip("=")
            st.session_state["pc_new_kind"] = "注意" if str(out.get("種類")) == "注意" else "NG"
            st.session_state.pop("pc_try", None)
        except Exception as e:
            st.error(f"作れませんでした：{str(e)[:200]}")

    if st.session_state.get("pc_new"):
        _note = str(st.session_state["pc_new"].get("確認してほしいこと", "") or "").strip()
        if _note:
            st.warning(f"🤔 AIからの確認：{_note}")
        a1, a2 = st.columns([3, 1])
        with a1:
            st.text_input("ルール名", key="pc_new_name")
        with a2:
            st.selectbox("種類", ["NG", "注意"], key="pc_new_kind")
        st.text_input("NGの理由（ミスの一覧に出る文）", key="pc_new_why")
        b1, b2 = st.columns([1, 3])
        with b1:
            st.text_input("見る列", key="pc_new_cols")
        with b2:
            st.text_input("条件（2行目の形の数式）", key="pc_new_formula")

        def _new_row(rid: str, on=True):
            return [rid, on, new_tab, st.session_state["pc_new_kind"], st.session_state["pc_new_name"].strip(),
                    st.session_state["pc_new_why"].strip(), st.session_state["pc_new_cols"].strip(),
                    st.session_state["pc_new_formula"].strip().lstrip("="),
                    f"アプリで追加：{new_text.strip()}", "アプリ", time.strftime("%Y/%m/%d")]

        t1, t2 = st.columns([1, 1])
        with t1:
            if st.button("🧪 いまのデータで試す", use_container_width=True, key="pc_new_try",
                         disabled=not (_has_gas and st.session_state.get("pc_new_formula", "").strip())):
                try:
                    sh = _open(gc, _url)
                    try:
                        ws = sh.worksheet(TRY_SHEET)
                    except Exception:
                        ws = sh.add_worksheet(TRY_SHEET, rows=5, cols=len(RULE_HEADS))
                    ws.clear()
                    # 数式が計算されないよう、文字のまま（RAW）書く
                    ws.update(values=[RULE_HEADS, _new_row("試し")], range_name="A1",
                              value_input_option="RAW")
                    with st.spinner("いまのデータで試しています..."):
                        ok, data = _gas("enkanDcTry")
                    if not ok:
                        st.error(f"❌ 試せませんでした：{data}")
                    else:
                        st.session_state["pc_try"] = _read_sheet(_url, TRY_OUT_SHEET)
                except Exception as e:
                    st.error(f"試せませんでした：{str(e)[:200]}")
        _try = st.session_state.get("pc_try")
        if _try is not None:
            if _try.empty or "ルールID" not in _try.columns:
                st.warning("結果が読めませんでした。")
            else:
                _bad = _try[_try["種類"].astype(str).str.startswith("⚠️")]
                _hit = _try[(_try["ルールID"].astype(str) != "") & ~_try["種類"].astype(str).str.startswith("⚠️")]
                if not _bad.empty:
                    st.error("⚠️ この数式は計算できませんでした。数式を直してもう一度試してください。")
                elif _hit.empty:
                    st.info("いまのデータでは **0件** でした（引っかかる案件がありません）。"
                            "思ったとおりか確かめてから登録してください。")
                else:
                    st.success(f"いまのデータで **{len(_hit)}件** 引っかかりました。思ったとおりか確かめてください。")
                    st.dataframe(_hit[["案件番号", "個人名", "見る列の中身"]],
                                 use_container_width=True, hide_index=True)
        with t2:
            _ok_add = st.checkbox("試した結果を見て、登録してよいと確かめました", key="pc_new_agree",
                                  disabled=_try is None)
            if st.button("💾 ルール表に登録する", type="primary", use_container_width=True,
                         key="pc_new_add", disabled=not (_ok_add and st.session_state.get("pc_new_name", "").strip())):
                try:
                    ws = _open(gc, _url).worksheet(RULE_SHEET)
                    values = ws.get_all_values()
                    heads = [str(h).strip() for h in values[0]]
                    pre = new_tab[:1] if new_tab[:1] in "NEG" else "X"
                    nums = [int(m.group(1)) for r in values[1:]
                            for m in [re.match(rf"^{pre}(\d+)$", str(r[heads.index('ID')]).strip())] if m]
                    # ⚠️ 消したIDは使い回さない（同じIDで別のルールになると、前のエラー一覧と食い違う）
                    nums += [int(m.group(1)) for d in _deleted_rules()
                             for m in [re.match(rf"^{pre}(\d+)$", str(d.get("ID", "")).strip())] if m]
                    rid = f"{pre}{(max(nums) + 1) if nums else 1:02d}"
                    ws.append_row(_new_row(rid), value_input_option="RAW")
                    for k in ("pc_new", "pc_try", "pc_new_agree"):
                        st.session_state.pop(k, None)
                    st.session_state["pc_rules"] = _read_sheet(_url, RULE_SHEET)
                    st.success(f"✅ {rid} として登録しました。次の「🔍 チェックする」から効きます。")
                except Exception as e:
                    st.error(f"登録できませんでした：{str(e)[:200]}")

st.divider()
st.caption("💻 ①の更新はブラウザを開くので、担当者のPCで開いているときだけ動きます。"
           "②〜④はスプシとGASだけで動きます。")
