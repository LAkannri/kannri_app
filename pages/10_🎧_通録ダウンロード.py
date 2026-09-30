"""
🎧 通録ダウンロード

ブルービーンの録音を、その月の1日から月末まで1日ずつ落とし → Googleドライブの
`<年>年/<月>月/<日>/` に入れ → **全部そろったときだけ** ブルービーンから一括削除する。
中身は `callrec.py`（時間指定の自動実行も同じ関数を通る）。
"""
import datetime

import streamlit as st

import callrec
import characters as ch
import common_robots
import theme

st.set_page_config(page_title="通録ダウンロード - エンカンAI", layout="wide")

theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🎧", "通録ダウンロード",
                  "ブルービーンの録音をGoogleドライブに移して、ブルービーンから消します。",
                  color=c["color"])


@st.cache_resource(show_spinner=False)
def _sb():
    from supabase import create_client
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase = _sb()
cfg = callrec.load_cfg(supabase)
root = callrec.folder_id(cfg.get("drive_folder", ""))
authed = callrec.drive_creds(supabase) is not None


def _show_steps(res: dict):
    for r in res.get("工程") or []:
        {"✅": st.success, "⏹": st.info, "🛡": st.warning, "⏸": st.warning}.get(r["結果"], st.error)(
            f"{r['結果']} {r['工程']}：{r['中身']}")


# ==========================================
# ⚙️ 設定
# ==========================================
with st.expander("⚙️ 設定", expanded=not (root and authed)):
    new_folder = st.text_input("保存先のGoogleドライブのフォルダ（URL）", value=cfg.get("drive_folder", ""),
                               key="cr_folder",
                               help="この下に「2026年」→「9月」→「01」…のフォルダを作って入れます（あれば使います）。")
    _robots = sorted(set(common_robots.list_robots(supabase) + [callrec.DEFAULT_ROBOT]))
    _rb = cfg.get("robot") or callrec.DEFAULT_ROBOT
    robot = st.selectbox("ログインに使うロボット", _robots, index=_robots.index(_rb) if _rb in _robots else 0,
                         key="cr_robot",
                         help="ブルービーン投入のロボットの**ログインの手順とパスワードだけ**を使います。"
                              "一括削除のパスワード欄にも、そのロボットに保存したパスワードを入れます。")
    start_month = st.text_input("この月から扱う（YYYY-MM）", value=cfg.get("start_month", "") or "2026-09",
                                key="cr_start", help="これより前の月は、時間指定では動かしません。")
    if st.button("💾 設定を保存", key="cr_save"):
        callrec.save_cfg(supabase, {"drive_folder": new_folder.strip(), "robot": robot,
                                    "start_month": start_month.strip()})
        st.success("保存しました。")
        st.rerun()

    st.markdown("**🔑 Googleドライブの許可**")
    if authed:
        st.success("許可があります。")
        if root:
            try:
                st.caption(f"保存先：📁 {callrec.folder_title(callrec.drive(supabase), root)}")
            except Exception as e:
                st.error(f"保存先のフォルダを開けませんでした（許可したアカウントが入れないフォルダかもしれません）：{e}")
        if st.button("🔌 許可を取り消す（別のアカウントで出し直す）", key="cr_forget"):
            callrec.forget(supabase)
            st.rerun()
    else:
        st.info("「01_通録」にファイルを入れられるアカウントで、1回だけ許可を出してください。"
                "ブラウザが開きます（許可は暗号化して保存し、自動実行用のPCでも使います）。")
        if st.button("🔑 ドライブの許可を出す", type="primary", key="cr_auth"):
            try:
                with st.spinner("ブラウザで許可してください（3分まで待ちます）…"):
                    callrec.authorize_local(supabase)
                st.success("許可できました。")
                st.rerun()
            except Exception as e:
                st.error(f"許可できませんでした：{e}")

if not (root and authed):
    st.info("⚙️ 設定で、保存先のフォルダとGoogleドライブの許可を用意してください。")
    st.stop()

# ==========================================
# 📋 いまの状況
# ==========================================
done = sorted(cfg.get("done") or [])
pend = callrec.pending_months(cfg)
with st.container(border=True):
    theme.section_title("📋", "いまの状況")
    st.markdown(f"- 済んだ月：{'、'.join(done) if done else 'まだありません'}\n"
                f"- きょう時間指定で動く月：{'、'.join(pend) if pend else 'なし（月末か、先月が済んでいないときだけ動きます）'}")
    _last = cfg.get("last_run") or {}
    if _last:
        with st.expander(f"前回：{_last.get('at', '')}　{_last.get('結果', '')}"):
            _show_steps(_last)

# ==========================================
# ▶ 実行
# ==========================================
with st.container(border=True):
    theme.section_title("▶", "実行する")
    st.warning("⚠️ ブルービーンの注意書きどおり、**架電業務時間外**に動かしてください（サーバーが重くなります）。")
    today = datetime.date.today()
    _prev = (today.replace(day=1) - datetime.timedelta(days=1))
    _choices = sorted({f"{_prev.year:04d}-{_prev.month:02d}", f"{today.year:04d}-{today.month:02d}", *pend})
    _def = pend[0] if pend else _choices[-1]
    ym = st.selectbox("どの月？", _choices, index=_choices.index(_def), key="cr_month")
    s, e = callrec.month_range(ym)
    st.caption(f"{s} 〜 {e} を1日ずつ落として、Driveの「{int(ym[:4])}年/{int(ym[5:])}月/日」に入れます。"
               "Driveに同じ名前・同じ大きさのファイルがもうあれば入れません（入れ直しても重なりません）。")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("📤 落としてDriveに入れるだけ（消さない）", use_container_width=True, key="cr_try"):
            with st.spinner("落としてDriveに入れています（1か月で数十分かかります）…"):
                res = callrec.run_month(supabase, cfg, ym, delete=False)
            st.session_state.cr_res = res
    with col2:
        sure = st.checkbox(f"全部Driveにそろったら、ブルービーンの {s}〜{e} を**一括削除**する（取り消せません）",
                           key="cr_sure")
        if st.button("🚀 落として入れて、そろったら一括削除", type="primary", use_container_width=True,
                     disabled=not sure, key="cr_go"):
            with st.spinner("落としてDriveに入れています。そろったら一括削除します…"):
                res = callrec.run_month(supabase, cfg, ym, delete=True)
            st.session_state.cr_res = res
    if st.session_state.get("cr_res"):
        _show_steps(st.session_state.cr_res)
