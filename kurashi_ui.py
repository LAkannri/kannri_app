"""🛡 暮らし安心の画面（エントリー業務自動化のホームから開く）。中身は `kurashi.py`。"""
import datetime as _dt

import streamlit as st

import kurashi


def _secrets() -> dict:
    try:
        return dict(st.secrets)
    except Exception:
        return {}


def _show_result(res: dict):
    icon = {"完了": "✅", "失敗": "🛑", "確認待ち": "⏸"}.get(res.get("結果", ""), "ℹ️")
    st.markdown(f"**{icon} {res.get('結果', '')}**")
    for r in res.get("工程") or []:
        with st.expander(f"{r.get('結果', '')} {r.get('工程', '')}", expanded=r.get("結果") in ("🛑", "⏸")):
            st.text(r.get("中身", ""))


def _settings(supabase, cfg: dict, sec: dict):
    with st.expander("⚙️ 設定", expanded=not cfg.get("token_enc")):
        url = st.text_input("決済システムのURL", value=kurashi.base_url(cfg), key="ka_url",
                            help="管理ボードのアドレスの、/admin より前の部分です。")
        try:
            _since0 = _dt.date.fromisoformat(kurashi.since(cfg))
        except Exception:
            _since0 = _dt.date.fromisoformat(kurashi.DEFAULT_SINCE)
        since = st.date_input("この日より前の申込は拾わない", value=_since0, key="ka_since",
                              help="決済システムで「エントリー済み」を記録しはじめる前の案件を、"
                                   "nuworksへもう一度入れないための区切りです。ふだんは変えません。")
        robots = []
        try:
            res = supabase.table("merchants").select("id").execute()
            robots = sorted(str(x["id"]) for x in (res.data or []) if not str(x["id"]).startswith("__"))
        except Exception:
            pass
        cur_robot = kurashi.robot_name(cfg)
        if cur_robot not in robots:
            robots = [cur_robot] + robots
        robot = st.selectbox("nuworksに入れるロボット", robots, index=robots.index(cur_robot), key="ka_robot")
        if st.button("💾 設定を保存", key="ka_save"):
            kurashi.save(supabase, {"base_url": url.strip().rstrip("/"), "since": since.isoformat(),
                                    "robot": robot})
            st.success("保存しました。")
            st.rerun()

        st.markdown("---")
        st.markdown("**🔑 合言葉**（決済システムの `ENKAN_API_TOKEN` と同じもの）")
        if cfg.get("token_enc"):
            _, err = kurashi.token(cfg, sec)
            if err:
                st.warning(err)
            else:
                st.caption(f"✅ 保存してあります（{cfg.get('token_made', '')} に作成）。"
                           "暗号化してあるので、ここには出しません。")
        remake = True
        if cfg.get("token_enc"):
            remake = st.checkbox("作り直す（Vercelの ENKAN_API_TOKEN も貼り直しになります）", key="ka_remake")
        if st.button("🔑 合言葉を作る", key="ka_tok", disabled=not remake):
            tok, err = kurashi.new_token(supabase, sec)
            if err:
                st.error(err)
            else:
                st.session_state["ka_new_token"] = tok
        if st.session_state.get("ka_new_token"):
            st.warning("⚠️ この合言葉が出るのは**今だけ**です。右上のボタンでコピーして、"
                       "Vercel → 決済システムのプロジェクト → Settings → Environment Variables に "
                       "`ENKAN_API_TOKEN` として貼り、**Redeploy** してください。")
            st.code(st.session_state["ka_new_token"], language=None)
            if st.button("貼り終わった（画面から消す）", key="ka_tok_hide"):
                st.session_state.pop("ka_new_token", None)
                st.rerun()


def render(supabase):
    sec = _secrets()
    cfg = kurashi.load(supabase)
    _settings(supabase, cfg, sec)

    last = cfg.get("last_done") or {}
    if last:
        st.caption(f"前回の本番：{last.get('at', '')}（エントリー {last.get('entry', 0)}件・解約 {last.get('cancel', 0)}件）")

    # 🛡 前回、入れたかもしれないのにエントリー済みにできていない案件
    pend = kurashi.pending(cfg)
    if pend.get("ids"):
        with st.container(border=True):
            st.error(f"🛡 前回（{pend.get('at', '')}）nuworksに入れた可能性がある {len(pend['ids'])}件が、"
                     "まだエントリー済みになっていません。**二重に入れないよう、本番は止めてあります。**")
            st.code("\n".join(pend["ids"]), language=None)
            st.caption("nuworksで、この会員IDが入っているかを見てから選んでください。")
            ok = st.checkbox("nuworksを見て確かめました", key="ka_pend_ok")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 入っていた → エントリー済みにする", disabled=not ok, key="ka_pend_yes",
                             use_container_width=True):
                    n, err = kurashi.resolve_pending(supabase, sec, True)
                    if err:
                        st.error(f"エントリー済みにできませんでした（控えは残してあります）：{err}")
                    else:
                        st.rerun()
            with c2:
                if st.button("↩ 入っていなかった → 控えを消す（次の回に入れる）", disabled=not ok,
                             key="ka_pend_no", use_container_width=True):
                    kurashi.resolve_pending(supabase, sec, False)
                    st.rerun()

    st.markdown("#### 🔌 いまの件数を見る（読むだけ）")
    if st.button("🔎 決済システムから件数を読む", key="ka_peek"):
        got, err = kurashi.fetch(cfg, sec, save_files=False)
        if err:
            st.error(err)
        else:
            st.session_state["ka_peek_res"] = got
    got = st.session_state.get("ka_peek_res")
    if got:
        st.info(f"エントリー **{got['entry']['count']}件**（{kurashi.since(cfg)} 以降の未エントリー）／"
                f"解約 **{got['cancel']['count']}件**（きょう）")
        if got["entry"]["ids"]:
            st.code("\n".join(got["entry"]["ids"]), language=None)

    st.markdown("#### ▶ 実行")
    st.caption("① 決済システムからCSVを受け取る → ② nuworksに新規インポート・一括解約 → "
               "③ 入れた会員IDだけエントリー済みにする。時間指定の自動実行（🛡 暮らし安心）も同じ流れです。")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🧪 お試し（インポートしない）", use_container_width=True, key="ka_try"):
            with st.spinner("ロボットが動いています（ブラウザが開きます）…"):
                st.session_state["ka_res"] = kurashi.run(supabase, sec, live=False)
    with c2:
        sure = st.checkbox("nuworksに本当に入れます（取り消せません）", key="ka_sure")
        if st.button("🚀 本番（入れて、エントリー済みにする）", type="primary", disabled=not sure,
                     use_container_width=True, key="ka_live"):
            with st.spinner("ロボットが動いています（ブラウザが開きます）…"):
                st.session_state["ka_res"] = kurashi.run(supabase, sec, live=True)
    if st.session_state.get("ka_res"):
        _show_result(st.session_state["ka_res"])
