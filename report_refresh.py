"""
🔄 SFレポートの更新（エントリー業務自動化から呼ぶ画面）

エントリーを始める前に、**もとになるSFのレポートを最新にする**ための画面。

【なぜ別に用意したか】
SMS送信・データローダーの「①更新」は、そのパターン／ジョブに紐づいた
**1つのスプレッドシート**の中のシートを更新する作りになっている。
けれどエントリーの前に更新したいレポートは、**いくつものスプレッドシートに散らばっている**。
そこで「更新セット」＝スプシ何枚ぶんかのレポートのまとまり、を登録できるようにした。

  ・セットの中身を **ぜんぶまとめて更新** できる（ブラウザは1回だけ開いて回す）
  ・**チェックしたものだけ** 更新もできる（直したいレポートが1つのときのため）

【ロボットは共通1台】
SFコネクタの更新は、どのスプシ・どのシートでも押す場所は同じ。違うのは開く先だけ。
だから録画は「⚙️ その他設定 → 🤖 共通ロボットの登録」で作った1台をそのまま使い、
実行時に **開くURLだけ差し替える**（`robot.py --each-url`）。
"""
import json
import uuid

import pandas as pd
import streamlit as st

import common_robots
import sms_runner
import theme

SETTINGS_ID = "__reports__"      # Supabase の予約行（ロボット一覧には出ない）
WORK_ROOT = "レポート更新"        # 取り込みファイル/レポート更新/<セット名>


# ==========================================
# 💾 設定の読み書き
# ==========================================
def _load(supabase) -> dict:
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}


def _save(supabase, cfg: dict):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（SFレポート更新の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cfg}).execute()


def _sets(cfg) -> list:
    return cfg.get("sets", []) or []


def _find(cfg, name):
    for s in _sets(cfg):
        if s.get("name") == name:
            return s
    return None


# ==========================================
# 📄 スプレッドシート（シート名の一覧を出すために読む）
# ==========================================
SHEET_TIMEOUT_SEC = 30   # ⏱ スプシへの問い合わせを、いつまでも待たないための上限


@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    """シート名を読むためのつなぎこみ。

    ⚠️ **待ち時間の上限を必ず付ける。** 既定では返事が来ないときに永遠に待つので、
    通信が途切れた1回のせいで**設定画面がずっと固まったまま**になる（実際に起きた）。
    上限を付けておけば、固まらずに「開けませんでした」と出て、押し直せる。
    """
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    try:
        gc.http_client.set_timeout(SHEET_TIMEOUT_SEC)
    except Exception:
        # 古い版には set_timeout が無い。そのときは通信そのものに上限を付ける。
        sess = getattr(getattr(gc, "http_client", None), "session", None) or \
            getattr(gc, "session", None)
        if sess is not None:
            _orig = sess.request

            def _request(method, url, **kw):
                if not kw.get("timeout"):
                    kw["timeout"] = SHEET_TIMEOUT_SEC
                return _orig(method, url, **kw)

            sess.request = _request
    return gc


def _gc():
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
def _tab_gids(_gc, sheet_url: str) -> dict:
    sh = (_gc.open_by_url(sheet_url) if sheet_url.startswith("http")
          else _gc.open_by_key(sheet_url))
    return {w.title: w.id for w in sh.worksheets()}


def _gids_of(gc, url: str) -> dict:
    try:
        return _tab_gids(gc, url.strip()) if (gc and url.strip()) else {}
    except Exception:
        return {}


def _label_of(sheet: dict, i: int) -> str:
    return str(sheet.get("label") or "").strip() or f"スプレッドシート{i + 1}"


def _rows_of(one_set: dict) -> list:
    """セットの中身を「1行＝1レポート」に並べ直す（画面にも実行にもこの並びを使う）。"""
    rows = []
    for i, sh in enumerate(one_set.get("sheets", []) or []):
        for t in sh.get("tabs", []) or []:
            rows.append({"スプレッドシート": _label_of(sh, i), "シート": t,
                         "url": str(sh.get("url", "")).strip()})
    return rows


def _count(one_set: dict) -> int:
    return len(_rows_of(one_set))


# ==========================================
# ▶ 実行
# ==========================================
def _run(gc, one_set: dict, rows: list):
    """選ばれたレポートを、ブラウザを1回だけ開いて上から順に更新する。

    スプシが何枚に分かれていても、1周ごとに「そのシートのURL」を開いてから
    同じ手順をなぞるので、ブラウザは開き直さなくてよい（ログインも1回で済む）。
    """
    robot = one_set.get("robot", "")
    folder = sms_runner.work_dir(WORK_ROOT, one_set.get("name", "セット"))
    tabs, urls = [], []
    gid_cache = {}
    for r in rows:
        url = r["url"]
        if url not in gid_cache:
            gid_cache[url] = _gids_of(gc, url)
        gid = gid_cache[url].get(r["シート"])
        tabs.append(r["シート"])
        urls.append(sms_runner.sheet_tab_url(url, gid) if gid is not None else url)
    ok, log = sms_runner.run_sheet_refresh(robot, folder, tabs=tabs, tab_urls=urls,
                                           url=urls[0] if urls else "")
    results = sms_runner.refresh_results(log, len(rows))
    table = [{"スプレッドシート": r["スプレッドシート"], "シート": r["シート"], "結果": res}
             for r, res in zip(rows, results)]
    return {"ok": ok, "log": log, "表": table}


def _render_run(supabase, cfg, name: str):
    one_set = _find(cfg, name)
    if not one_set:
        st.error("その更新セットが見つかりませんでした。")
        if st.button("⬅ 一覧に戻る", key="rr_run_back0"):
            st.session_state.rr_view = "list"
            st.rerun()
        return
    if st.button("⬅ 一覧に戻る", key="rr_run_back"):
        st.session_state.rr_view = "list"
        st.rerun()

    st.markdown(f"### ▶ 「{name}」のレポートを更新する")
    if one_set.get("memo"):
        st.caption(one_set["memo"])

    rows = _rows_of(one_set)
    if not rows:
        st.info("このセットには、まだ更新するレポートが登録されていません。"
                "「✏️ 設定」からスプレッドシートとシートを登録してください。")
        return

    robot = one_set.get("robot", "")
    if not robot:
        st.warning("使うロボットが選ばれていません。「✏️ 設定」で選んでください。")
        return
    if robot not in common_robots.list_robots(supabase):
        st.warning(f"⚠️ ロボット「{robot}」はまだ録画されていません。"
                   "「⚙️ その他設定」の🤖共通ロボットの登録で作ってください（1回でOKです）。")

    st.caption(f"使うロボット：**{robot}**")
    _lg, _lgw = common_robots.login_status(robot)
    if _lg:
        st.caption(f"🔐 Googleのログイン状態：あり（最終 {_lgw}）")
    else:
        st.warning("🔐 このロボットのブラウザに**ログイン状態がありません**。"
                   "「⚙️ その他設定」の🔐先にログインしておく、から入っておいてください。")

    gc = _gc()
    if not gc:
        st.warning("⚠️ サービスアカウント（`GOOGLE_SERVICE_ACCOUNT_JSON`）が未設定のため、"
                   "**シートを名指しで開けません**。1枚のスプシに複数のシートを登録している場合、"
                   "違うシートを更新してしまわないよう、ロボットが途中で止まります。")

    st.caption("ブラウザは1回だけ開いて、その中でスプレッドシートとシートを切り替えながら回します。"
               "録画したときのURLは使いません（開く先は、下の一覧のスプレッドシートです）。")

    df = pd.DataFrame([{"更新する": True, "スプレッドシート": r["スプレッドシート"],
                        "シート": r["シート"]} for r in rows])
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"rr_pick_{name}",
        disabled=["スプレッドシート", "シート"],
        column_config={"更新する": st.column_config.CheckboxColumn(
            "更新する", help="チェックを外すと、そのレポートは更新しません。")})
    picked = [r for r, keep in zip(rows, list(edited["更新する"])) if keep]

    b1, b2 = st.columns(2)
    with b1:
        if st.button(f"🔄 ぜんぶ更新する（{len(rows)}件）", type="primary",
                     use_container_width=True, key=f"rr_all_{name}"):
            with st.spinner(f"{len(rows)}件のレポートを順に更新しています"
                            "（レポートによっては数分かかります）..."):
                st.session_state[f"rr_res_{name}"] = _run(gc, one_set, rows)
            st.rerun()
    with b2:
        if st.button(f"✅ チェックしたものだけ更新する（{len(picked)}件）",
                     use_container_width=True, disabled=not picked, key=f"rr_sel_{name}"):
            with st.spinner(f"{len(picked)}件のレポートを順に更新しています..."):
                st.session_state[f"rr_res_{name}"] = _run(gc, one_set, picked)
            st.rerun()

    res = st.session_state.get(f"rr_res_{name}")
    if res:
        st.dataframe(pd.DataFrame(res["表"]), use_container_width=True, hide_index=True)
        if res["ok"]:
            st.success("✅ ぜんぶ更新できました。")
        else:
            st.error("❌ 途中で止まりました。上の表で、どこまで進んだか分かります。"
                     "直したら、そのレポートだけチェックして更新し直せます。")
        with st.expander("実行ログ", expanded=not res["ok"]):
            st.code(res["log"])


# ==========================================
# ⚙️ 設定
# ==========================================
EDIT_KEY = "rr_edit_sheets"   # 保存を押すまでの編集内容（セッションに置く）


def _render_edit(supabase, cfg, old_name: str):
    one_set = _find(cfg, old_name) or {
        "name": "", "memo": "", "robot": common_robots.ROLES["refresh"]["name"], "sheets": []}

    if st.button("⬅ 一覧に戻る", key="rr_edit_back"):
        st.session_state.rr_view = "list"
        st.session_state.pop(EDIT_KEY, None)
        st.rerun()

    _title = "更新セットを追加" if not old_name else f"「{old_name}」の設定"
    st.markdown(f"### ⚙️ {_title}")
    st.caption("🎬 録画（手順の登録）は「⚙️ その他設定」の🤖共通ロボットの登録で1回だけ行います。"
               "ここでは**どのスプレッドシートの、どのシートを更新するか**だけ決めます。")

    with st.container(border=True):
        theme.section_title("1️⃣", "このセットの名前")
        name = st.text_input("名前", value=one_set.get("name", ""),
                             placeholder="例：朝いちのレポート更新", key="rr_name")
        memo = st.text_input("メモ（何のための更新か）", value=one_set.get("memo", ""),
                             key="rr_memo")
        robots = common_robots.list_robots(supabase)
        default = common_robots.ROLES["refresh"]["name"]
        cur = one_set.get("robot", "") or default
        opts = list(dict.fromkeys(list(robots) + ([cur] if cur else []))) or [default]
        robot = st.selectbox("使うロボット", opts,
                             index=opts.index(cur) if cur in opts else 0, key="rr_robot")
        if robot and robot not in robots:
            st.warning(f"⚠️ ロボット「{robot}」はまだ録画されていません。"
                       "「⚙️ その他設定」の🤖共通ロボットの登録で作ってください（1回でOKです）。")

    # 📄 スプレッドシートは何枚でも足せる（保存を押すまで確定しない）
    if EDIT_KEY not in st.session_state:
        _init = [_new_row(s) for s in (one_set.get("sheets", []) or [])]
        st.session_state[EDIT_KEY] = _init or [_new_row()]
    sheets = st.session_state[EDIT_KEY]

    gc = _gc()
    with st.container(border=True):
        theme.section_title("2️⃣", "更新するレポート（スプレッドシートは何枚でも足せます）")
        if not gc:
            st.caption("※ サービスアカウント（`GOOGLE_SERVICE_ACCOUNT_JSON`）を設定すると、"
                       "シート名をプルダウンで選べます。")
        for i, sh in enumerate(sheets):
            if "_uid" not in sh:
                sh["_uid"] = uuid.uuid4().hex[:8]
            uid = sh["_uid"]
            with st.container(border=True):
                c1, c2 = st.columns([4, 1])
                with c1:
                    sh["label"] = st.text_input(
                        "呼び名（画面に出る名前）", value=sh.get("label", ""),
                        placeholder="例：LL貼り付け先", key=f"rr_lb_{uid}")
                with c2:
                    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                    if st.button("🗑 外す", key=f"rr_rm_{uid}", use_container_width=True):
                        sheets.pop(i)
                        st.rerun()
                sh["url"] = st.text_input(
                    "スプレッドシートのURL", value=sh.get("url", ""),
                    placeholder="https://docs.google.com/spreadsheets/d/...", key=f"rr_url_{uid}")
                names = []
                if gc and sh["url"].strip():
                    try:
                        names = list(_tab_gids(gc, sh["url"].strip()).keys())
                    except Exception as e:
                        st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")
                if names:
                    _cur = [t for t in (sh.get("tabs", []) or []) if t in names]
                    sh["tabs"] = st.multiselect("更新するシート（複数えらべます）", names,
                                                default=_cur, key=f"rr_tabs_{uid}")
                else:
                    _typed = st.text_input("更新するシート（カンマ区切り）",
                                           value="、".join(sh.get("tabs", []) or []),
                                           key=f"rr_tabt_{uid}")
                    sh["tabs"] = [t.strip() for t in
                                  _typed.replace("、", ",").split(",") if t.strip()]
        if st.button("＋ スプレッドシートを足す", key="rr_add_sheet"):
            sheets.append(_new_row())
            st.rerun()

    _bad = [t for sh in sheets for t in (sh.get("tabs", []) or []) if "," in t]
    if _bad:
        st.warning("⚠️ シート名に「,」が入っていると、順に回すときに区切りと区別できません："
                   + "／".join(_bad))

    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button("💾 保存する", type="primary", use_container_width=True, key="rr_save"):
            if not name.strip():
                st.error("名前を入れてください。")
            elif name.strip() != old_name and _find(cfg, name.strip()):
                st.error(f"「{name.strip()}」はすでにあります。別の名前にしてください。")
            else:
                new = {"name": name.strip(), "memo": memo.strip(), "robot": robot,
                       "sheets": [{"label": str(s.get("label", "")).strip(),
                                   "url": str(s.get("url", "")).strip(),
                                   "tabs": list(s.get("tabs", []) or [])}
                                  for s in sheets if str(s.get("url", "")).strip()]}
                items = [s for s in _sets(cfg) if s.get("name") != old_name]
                items.append(new)
                cfg["sets"] = items
                _save(supabase, cfg)
                st.session_state.pop(EDIT_KEY, None)
                st.session_state.rr_view = "list"
                st.toast(f"「{new['name']}」を保存しました", icon="💾")
                st.rerun()
    with s2:
        st.caption("※ 「💾 保存する」を押すまで、ここでの変更は残りません。")


def _new_row(src: dict = None) -> dict:
    """編集中の1行（＝スプシ1枚）。

    ⚠️ **入力欄のキーは、行の番号ではなく行そのものに結びつける。**
    番号で付けると、途中の行を消したときに下の行が番号を引き継ぎ、
    Streamlit が覚えている前の入力値がそのまま出る＝**消した行と違う行が消えたように見える**
    （実際に起きた）。行ごとの目印を持たせておけば、消した行の入力欄も一緒に消える。
    """
    row = dict(src or {})
    row.setdefault("label", "")
    row.setdefault("url", "")
    row.setdefault("tabs", [])
    row["_uid"] = uuid.uuid4().hex[:8]
    return row


# ==========================================
# 📋 一覧
# ==========================================
def _render_list(supabase, cfg):
    st.caption("エントリーを始める前に、もとになる**SFのレポートを最新にする**ための画面です。"
               "スプレッドシートが何枚に分かれていても、**まとめて1回で**更新できます。")

    _, add = st.columns([4, 1])
    with add:
        if st.button("＋ 更新セットを追加", use_container_width=True, key="rr_add"):
            st.session_state.rr_view = "edit"
            st.session_state.rr_set = ""
            st.session_state.pop(EDIT_KEY, None)
            st.rerun()

    items = _sets(cfg)
    if not items:
        st.info("まだ更新セットがありません。"
                "「＋ 更新セットを追加」から、更新したいレポートを登録してください。")
        return

    for s in items:
        nm = s.get("name", "")
        with st.container(border=True):
            st.markdown(f"#### 🔄 {nm}")
            if s.get("memo"):
                st.caption(s["memo"])
            _labels = "／".join(_label_of(sh, i) for i, sh in
                               enumerate(s.get("sheets", []) or []))
            st.caption(f"スプレッドシート {len(s.get('sheets', []) or [])}枚"
                       f"／レポート {_count(s)}件　{_labels}")
            c1, c2, c3 = st.columns([1.4, 1, 1])
            with c1:
                if st.button("▶ 更新する", key=f"rr_go_{nm}", type="primary",
                             use_container_width=True):
                    st.session_state.rr_view = "run"
                    st.session_state.rr_set = nm
                    st.rerun()
            with c2:
                if st.button("✏️ 設定", key=f"rr_ed_{nm}", use_container_width=True):
                    st.session_state.rr_view = "edit"
                    st.session_state.rr_set = nm
                    st.session_state.pop(EDIT_KEY, None)
                    st.rerun()
            with c3:
                dk = f"rr_del_{nm}"
                if not st.session_state.get(dk):
                    if st.button("🗑 削除", key=f"rr_delbtn_{nm}", use_container_width=True):
                        st.session_state[dk] = True
                        st.rerun()
                else:
                    st.warning(f"「{nm}」を消しますか？")
                    d1, d2 = st.columns(2)
                    with d1:
                        if st.button("はい", key=f"rr_dy_{nm}", use_container_width=True):
                            cfg["sets"] = [x for x in items if x.get("name") != nm]
                            _save(supabase, cfg)
                            st.session_state.pop(dk, None)
                            st.rerun()
                    with d2:
                        if st.button("やめる", key=f"rr_dn_{nm}", use_container_width=True):
                            st.session_state.pop(dk, None)
                            st.rerun()


def render(supabase):
    """SFレポート更新の画面（エントリー業務自動化のページから呼ぶ）。"""
    if "rr_view" not in st.session_state:
        st.session_state.rr_view = "list"
    cfg = _load(supabase)
    view = st.session_state.rr_view
    if view == "run":
        _render_run(supabase, cfg, st.session_state.get("rr_set", ""))
    elif view == "edit":
        _render_edit(supabase, cfg, st.session_state.get("rr_set", ""))
    else:
        _render_list(supabase, cfg)
