"""💼 FP連携（一声干渉）の画面（エントリー業務自動化のホームから開く）。中身は `fp_toss.py`。"""
import pandas as pd
import streamlit as st

import auto_jobs
import common_robots
import fp_toss
import theme


def _show_steps(res: dict):
    for r in res.get("工程") or []:
        {"✅": st.success, "⏹": st.info, "🛡": st.warning, "⏸": st.warning}.get(r["結果"], st.error)(
            f"{r['結果']} {r['工程']}：{r['中身']}")


@st.cache_resource(show_spinner=False)
def _gc_for(sa_json: str):
    return auto_jobs.gspread_client(sa_json)


def render(supabase):
    gc = _gc_for(st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    try:
        cfg = fp_toss.load(supabase)
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
        return
    url = str(cfg.get("sheet_url", "") or "").strip()

    with st.expander("⚙️ 設定", expanded=not url):
        new_url = st.text_input("「ライフアップ様 リスト」（スプレッドシート）のURL", value=url, key="fp_url",
                                help=f"「{fp_toss.REPORT_TAB}」「{fp_toss.PASTE_TAB}」「{fp_toss.LINK_TAB}」があるスプシ")
        _robots = sorted(set(common_robots.list_robots(supabase) + [auto_jobs.DEFAULT_REFRESH_ROBOT]))
        _rb = cfg.get("refresh_robot") or auto_jobs.DEFAULT_REFRESH_ROBOT
        robot = st.selectbox("更新に使うロボット", _robots,
                             index=_robots.index(_rb) if _rb in _robots else 0, key="fp_robot")
        auto_append = st.checkbox(
            "時間指定の自動実行で、連携分への追記まで自動で行う", value=bool(cfg.get("auto_append")),
            key="fp_auto", help="OFFなら、新しい案件があったところで止めてSlackで知らせます（画面で中身を見て追記します）。")
        if st.button("💾 設定を保存", key="fp_save"):
            fp_toss.save(supabase, {"sheet_url": new_url.strip(), "refresh_robot": robot,
                                    "auto_append": bool(auto_append)})
            st.success("保存しました。")
            st.rerun()
    if not (url and gc):
        st.info("⚙️ 設定でスプレッドシートのURLを入れてください（接続キー GOOGLE_SERVICE_ACCOUNT_JSON も要ります）。")
        return

    st.caption(f"① SFコネクタで「{fp_toss.REPORT_TAB}」を更新 → ② 「{fp_toss.PASTE_TAB}」にまだ無い案件だけ選ぶ → "
               f"③ 貼り付け用のいちばん下に足し、「{fp_toss.LINK_TAB}」の次の行に**投入日**と**B〜G列の数式**、"
               f"顧客対応備考にFPの希望があれば**I列（電話に出やすい時間）・J列（アポ候補日）**を入れる → "
               "④ 連携分に正しく出た案件だけ、Salesforce の**FP登録日**に今日を入れます。")
    _last = cfg.get("last_run") or {}
    if _last:
        st.caption(f"前回の追記：{_last.get('at', '')}（{len(_last.get('added') or [])}件）")

    _pend = cfg.get("sf_pending") or {}
    if _pend:
        st.warning(f"⚠️ 連携分には足したのに、Salesforce の FP登録日が入っていない案件が {len(_pend)}件 あります："
                   + "、".join(list(_pend)[:10]))
        if st.button("🔁 Salesforceの FP登録日だけ入れ直す", key="fp_sf_retry"):
            with st.spinner("入れ直しています…"):
                _mk, _body = fp_toss.sf_step(supabase, cfg, [])
            {"✅": st.success, "🛡": st.warning}.get(_mk, st.error)(f"{_mk} {_body}")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 更新して確認する", type="primary", use_container_width=True, key="fp_refresh"):
            with st.spinner("SFのレポートを更新しています（数分かかることがあります）…"):
                ok, why = fp_toss.refresh(gc, cfg)
            if not ok:
                st.error(f"更新できませんでした：{why}")
                st.session_state.pop("fp_plan", None)
            else:
                with st.spinner("読んでいます…"):
                    st.session_state.fp_plan = fp_toss.plan(gc, url)
    with c2:
        if st.button("👀 更新せずに確認する", use_container_width=True, key="fp_peek"):
            with st.spinner("読んでいます…"):
                st.session_state.fp_plan = fp_toss.plan(gc, url)

    p = st.session_state.get("fp_plan")
    if not p:
        return
    if p["error"]:
        st.error(p["error"])
        return
    if p["dup"]:
        st.caption(f"すでに貼り付け用にある {len(p['dup'])}件は足しません。")
    if p["registered"]:
        st.caption(f"FP登録日がもう入っている {len(p['registered'])}件は足しません。")
    if p["noid"]:
        st.warning(f"案件 ID が空の行が {p['noid']}件 ありました（足しません）。")
    if p["note_error"]:
        st.warning(p["note_error"])
    if not p["new"]:
        st.success("新しい案件はありません。")
        return

    head = p["headers"]

    def _get(it, col):
        return it["view"][head.index(col)] if col in head else ""

    theme.section_title("🆕", f"新しい案件 {len(p['new'])}件")
    st.caption("「希望時間（I列）」「希望日（J列）」は直せます（空にすると入れません）。"
               "顧客対応備考にFPの希望が無ければ空です。")
    df = pd.DataFrame([{
        "足す": True,
        "案件番号": fp_toss.case_label(it, head),
        "お名前": f"{_get(it, '名前（姓）')} {_get(it, '名前（名）')}".strip(),
        "引越し日": _get(it, "引越し日"),
        "希望時間（I列）": it["time"],
        "希望日（J列）": it["day"],
        "顧客対応備考": str(it.get("note") or "")[:300],
    } for it in p["new"]])
    ed = st.data_editor(df, hide_index=True, use_container_width=True, key=f"fp_ed_{id(p)}",
                        disabled=["案件番号", "お名前", "引越し日", "顧客対応備考"])
    pick = []
    for k, (_, r) in enumerate(ed.iterrows()):
        if bool(r["足す"]):
            pick.append({**p["new"][k], "time": str(r["希望時間（I列）"] or "").strip(),
                         "day": str(r["希望日（J列）"] or "").strip()})
    sure = st.checkbox(f"「{fp_toss.LINK_TAB}」に {len(pick)}件 足し、Salesforce の FP登録日を入れることを確かめた",
                       key="fp_sure")
    if st.button(f"📝 {len(pick)}件を連携分に足す", type="primary", disabled=not (sure and pick), key="fp_go"):
        try:
            with st.spinner("書き込んでいます…"):
                r = fp_toss.append(gc, url, pick)
        except Exception as e:
            st.error(str(e))
            return
        steps = auto_jobs._Steps()
        with st.spinner("Salesforce に FP登録日を入れています…"):
            fp_toss.after_append(supabase, cfg, r, steps)
        _show_steps(steps.result())
        st.session_state.pop("fp_plan", None)
