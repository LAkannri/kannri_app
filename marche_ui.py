"""🚚 引越マルシェの画面（エントリー業務自動化のホームから開く）。中身は `marche.py`。"""
import pandas as pd
import streamlit as st

import auto_jobs
import common_robots
import marche
import theme


def _show_result(res: dict):
    icon = {"完了": "✅", "失敗": "🛑", "確認待ち": "⏸"}.get(res.get("結果", ""), "ℹ️")
    st.markdown(f"**{icon} {res.get('結果', '')}**")
    for r in res.get("工程") or []:
        with st.expander(f"{r.get('結果', '')} {r.get('工程', '')}", expanded=r.get("結果") in ("🛑", "⏸")):
            st.text(r.get("中身", ""))


@st.cache_resource(show_spinner=False)
def _gc_for(sa_json: str):
    return auto_jobs.gspread_client(sa_json)


def render(supabase):
    gc = _gc_for(st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    try:
        cfg = marche.load(supabase)
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
        return
    url = str(cfg.get("sheet_url", "") or "").strip()

    with st.expander("⚙️ 設定", expanded=not url):
        new_url = st.text_input("トスアップシート（スプレッドシート）のURL", value=url, key="mc_url",
                                help=f"「{marche.REPORT_TAB}」「{marche.PASTE_TAB}」「{marche.LINK_TAB}」があるスプシ")
        _robots = sorted(set(common_robots.list_robots(supabase) + [auto_jobs.DEFAULT_REFRESH_ROBOT]))
        _rb = cfg.get("refresh_robot") or auto_jobs.DEFAULT_REFRESH_ROBOT
        robot = st.selectbox("更新に使うロボット", _robots,
                             index=_robots.index(_rb) if _rb in _robots else 0, key="mc_robot")
        auto_append = st.checkbox(
            "時間指定の自動実行で、連携シートへの追記まで自動で行う", value=bool(cfg.get("auto_append")),
            key="mc_auto", help="OFFなら、新しい案件があったところで止めてSlackで知らせます（画面で中身を見て追記します）。")
        if st.button("💾 設定を保存", key="mc_save"):
            marche.save(supabase, {"sheet_url": new_url.strip(), "refresh_robot": robot,
                                   "auto_append": bool(auto_append)})
            st.success("保存しました。")
            st.rerun()
    if not (url and gc):
        st.info("⚙️ 設定でスプレッドシートのURLを入れてください（接続キー GOOGLE_SERVICE_ACCOUNT_JSON も要ります）。")
        return

    st.caption(f"① SFコネクタで「{marche.REPORT_TAB}」を更新 → ② 「{marche.PASTE_TAB}」にまだ無い案件だけ選ぶ → "
               f"③ 貼付用のいちばん下に足し、「{marche.LINK_TAB}」の同じ行に**トス日**と**備考**"
               "（顧客対応備考に書いてあるマルシェの希望時間）を入れます。")
    _last = cfg.get("last_run") or {}
    if _last:
        st.caption(f"前回の追記：{_last.get('at', '')}（{len(_last.get('added') or [])}件）")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 更新して確認する", type="primary", use_container_width=True, key="mc_refresh"):
            with st.spinner("SFのレポートを更新しています（数分かかることがあります）…"):
                ok, why = marche.refresh(gc, cfg)
            if not ok:
                st.error(f"更新できませんでした：{why}")
                st.session_state.pop("mc_plan", None)
            else:
                st.session_state.mc_plan = marche.plan(gc, url)
    with c2:
        if st.button("👀 更新せずに確認する", use_container_width=True, key="mc_peek"):
            st.session_state.mc_plan = marche.plan(gc, url)

    p = st.session_state.get("mc_plan")
    if not p:
        return
    if p["error"]:
        st.error(p["error"])
        return
    if p["dup"]:
        st.caption(f"すでに貼付用にある {len(p['dup'])}件は足しません。")
    if p["noid"]:
        st.warning(f"案件 ID が空の行が {p['noid']}件 ありました（足しません）。")
    if not p["new"]:
        st.success("新しい案件はありません。")
        return

    head = p["headers"]

    def _get(it, col):
        return it["view"][head.index(col)] if col in head else ""

    theme.section_title("🆕", f"新しい案件 {len(p['new'])}件")
    st.caption("「連携シートの備考」は直せます（空にすると備考は入れません）。顧客対応備考にマルシェの話が無ければ空です。")
    df = pd.DataFrame([{
        "足す": True,
        "案件番号": marche.case_label(it, head),
        "お名前": f"{_get(it, '名前（姓）')} {_get(it, '名前（名）')}".strip(),
        "引越し日": _get(it, "引越し日"),
        "連携シートの備考": it["note"],
        "顧客対応備考": str(_get(it, marche.NOTE_COL))[:300],
    } for it in p["new"]])
    ed = st.data_editor(df, hide_index=True, use_container_width=True, key=f"mc_ed_{id(p)}",
                        disabled=["案件番号", "お名前", "引越し日", "顧客対応備考"])
    pick = []
    for k, (_, r) in enumerate(ed.iterrows()):
        if bool(r["足す"]):
            pick.append({**p["new"][k], "note": str(r["連携シートの備考"] or "").strip()})
    sure = st.checkbox(f"連携シート（引越マルシェが見るシート）に {len(pick)}件 足すことを確かめた", key="mc_sure")
    if st.button(f"📝 {len(pick)}件を連携シートに足す", type="primary", disabled=not (sure and pick), key="mc_go"):
        try:
            with st.spinner("書き込んでいます…"):
                r = marche.append(gc, url, pick)
        except Exception as e:
            st.error(str(e))
            return
        if r["added"]:
            st.success(f"✅ {len(r['added'])}件を足しました（貼付用 {r['rows'][0]}〜{r['rows'][1]} 行目／"
                       f"連携シート {r['link_rows'][0]}〜{r['link_rows'][1]} 行目・備考 {r.get('notes', 0)}件）。")
            import time as _t
            marche.save(supabase, {"last_run": {"at": _t.strftime("%Y/%m/%d %H:%M"), "added": r["added"]}})
        if r["skipped"]:
            st.info(f"直前に別で足されていた {len(r['skipped'])}件は飛ばしました。")
        st.session_state.pop("mc_plan", None)
