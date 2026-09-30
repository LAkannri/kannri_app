"""🌐 RENXA（多言語窓口）連携の画面（エントリー業務自動化のホームから開く）。中身は `renxa.py`。"""
import pandas as pd
import streamlit as st

import auto_jobs
import common_robots
import renxa
import theme


def _show_steps(res: dict):
    for r in res.get("工程") or []:
        {"✅": st.success, "⏹": st.info, "🛡": st.warning, "⏸": st.warning}.get(r["結果"], st.error)(
            f"{r['結果']} {r['工程']}：{r['中身']}")


@st.cache_resource(show_spinner=False)
def _gc_for(sa_json: str):
    return auto_jobs.gspread_client(sa_json)


def _settings(supabase, cfg):
    url = str(cfg.get("sheet_url", "") or "").strip()
    with st.expander("⚙️ 設定", expanded=not url):
        new_url = st.text_input("RENXA のスプレッドシートのURL", value=url, key="rx_url",
                                help=f"SFコネクタで更新する「{renxa.REPORT_TAB}」があるスプシ")
        _robots = sorted(set(common_robots.list_robots(supabase) + [auto_jobs.DEFAULT_REFRESH_ROBOT]))
        _rb = cfg.get("refresh_robot") or auto_jobs.DEFAULT_REFRESH_ROBOT
        refresh_robot = st.selectbox("更新に使うロボット", _robots,
                                     index=_robots.index(_rb) if _rb in _robots else 0, key="rx_rrobot")
        st.markdown("**🔐 トヨクモ（kintone連携サービス）**")
        login_url = st.text_input("ログインのURL", value=str(cfg.get("login_url") or ""), key="rx_login",
                                  help="account.kintoneapp.com/login?backUrl=… の形。ログインしたあとは backUrl の画面を開きます。")
        login_mail = st.text_input("ログインに使うメールアドレス",
                                   value=str(cfg.get("login_mail") or renxa.DEFAULT_LOGIN_MAIL), key="rx_mail")
        form_url = st.text_input("フォームのURL（任意）", value=str(cfg.get("form_url") or ""), key="rx_form",
                                 help="「不動産個人情報取得フォーム」を開いたときのURL。入れておくと、"
                                      "Renxa株式会社 → お客様対応状況一覧 → フォーム、とたどらずに直接開きます。")
        company = st.text_input("検索で選ぶ会社コード", value=str(cfg.get("company_code") or renxa.DEFAULT_COMPANY_CODE),
                                key="rx_code", help="株式会社LIFEAP＝7760")
        st.markdown("**📝 フォームの選び方**")
        st.caption(f"申込種別は、検討理由が「{renxa.MAIL_REASON}」なら「{renxa.APPLY_MAIL}」、"
                   f"それ以外は「{renxa.APPLY_PHONE}」。グローバルの国籍は「{renxa.NATIONALITY}」、"
                   f"言語はSFの「言語_RENXA連携」（空・フォームに無い言語は{renxa.DEFAULT_LANG}）。表で直せます。")
        _wr = list(renxa.WATER_RULES)
        water = st.selectbox("水道の種別の決め方", _wr, format_func=renxa.WATER_RULES.get,
                             index=_wr.index(cfg.get("water_rule")) if cfg.get("water_rule") in _wr else 0, key="rx_water",
                             help="Salesforceに水道のフラグが無いので、契約Pack内容で決めます。")
        _kr = list(renxa.KANA_RULES)
        kana = st.selectbox("フリガナが空のとき", _kr, format_func=renxa.KANA_RULES.get,
                            index=_kr.index(cfg.get("kana_rule")) if cfg.get("kana_rule") in _kr else 0, key="rx_kana")
        st.markdown("**⏰ 時間指定の自動実行**")
        auto_staff = st.text_input("時間指定のときの店舗担当者名", value=str(cfg.get("auto_staff") or renxa.DEFAULT_STAFF),
                                   key="rx_astaff")
        auto_send = st.checkbox("時間指定の自動実行で、回答まで自動で行う", value=bool(cfg.get("auto_send")), key="rx_auto",
                                help="OFFなら、送る案件があったところで止めてSlackで知らせます（画面で中身を見て入れます）。")
        if st.button("💾 設定を保存", key="rx_save"):
            renxa.save(supabase, {"sheet_url": new_url.strip(), "refresh_robot": refresh_robot,
                                  "login_url": login_url.strip(), "login_mail": login_mail.strip(),
                                  "form_url": form_url.strip(), "company_code": company.strip(),
                                  "water_rule": water, "kana_rule": kana,
                                  "auto_staff": auto_staff.strip(), "auto_send": bool(auto_send)})
            st.success("保存しました。フォームの入れ方（ログイン・会社コード・フォームのURL）を変えたら、"
                       "下の「🛠 手順書を作り直す」も押してください。")
            st.rerun()

    with st.expander("🤖 ロボット（フォームの入れ方）", expanded=False):
        name = renxa.robot_name(cfg)
        try:
            have = bool(supabase.table("merchants").select("id").eq("id", name).execute().data)
        except Exception:
            have = False
        st.caption(f"ロボット「{name}」が、ブラウザを1回だけ開いて、全件を順にフォームへ入れます"
                   "（ログインも1回）。押す場所は画面の見出しの文字から探すので、録画はしなくてよいです。")
        sure = True
        if have:
            sure = st.checkbox("司令室で直した手順書を、作り直しで上書きしてよい", key="rx_rebuild_ok")
        if st.button("🛠 手順書を作り直す" if have else "🛠 ロボットを作る", disabled=not sure, key="rx_build"):
            try:
                st.success(renxa.install_robot(supabase, renxa.load(supabase)))
            except Exception as e:
                st.error(str(e))
        if have:
            st.info("📧 ログインは**メールのリンク**です（info@lifeap.co.jp に届くメールを、GMOの認証コードと同じGASが読みます）。"
                    f"設定スプシの「認証コード設定」に「{name}」の行を入れてあります。"
                    "リンクが取れないときは、司令室の「🔐 メールで届く認証」に届いたメールの本文を貼って直してください。")
            if st.button("✏️ 司令室で開く", key="rx_room"):
                st.session_state.editing_project = name
                st.session_state.view = 'project_room'
                st.rerun()


def _pending(supabase, cfg):
    sent = dict(cfg.get("sent") or {})
    ask = {k: v for k, v in sent.items() if (v or {}).get("state") in ("要確認", "送信前")}
    done = [k for k, v in sent.items() if (v or {}).get("state") == "回答済み"]
    if done:
        st.warning(f"⚠️ フォームには回答したのに、Salesforce の多言語窓口連携状況が「連携済み」になっていない案件が "
                   f"{len(done)}件 あります：" + "、".join(done[:10]))
        if st.button("🔁 Salesforceの連携済みだけ入れ直す", key="rx_sf_retry"):
            with st.spinner("入れ直しています…"):
                _mk, _body = renxa.sf_step(supabase, [])
            {"✅": st.success, "🛡": st.warning, "⏹": st.info}.get(_mk, st.error)(f"{_mk} {_body}")
    if ask:
        st.error(f"🛑 回答したか分からない案件が {len(ask)}件 あります（控えが残っているあいだは送りません）。"
                 "トヨクモの「お客様対応状況一覧」で入っているか確かめてください。")
        df = pd.DataFrame([{"案件ID": k, "いつ": (v or {}).get("at", ""), "状態": (v or {}).get("state", ""),
                            "入っていた": False, "入っていなかった": False} for k, v in ask.items()])
        ed = st.data_editor(df, hide_index=True, use_container_width=True, key=f"rx_ask_{len(ask)}",
                            disabled=["案件ID", "いつ", "状態"])
        yes = [r["案件ID"] for _, r in ed.iterrows() if r["入っていた"] and not r["入っていなかった"]]
        no = [r["案件ID"] for _, r in ed.iterrows() if r["入っていなかった"] and not r["入っていた"]]
        if st.button("💾 確かめた結果を入れる", disabled=not (yes or no), key="rx_ask_go"):
            if yes:
                renxa._mark_sent(supabase, yes, "回答済み")
                _mk, _body = renxa.sf_step(supabase, yes)
                {"✅": st.success, "🛡": st.warning}.get(_mk, st.error)(f"{_mk} {_body}")
            if no:
                renxa.forget(supabase, no)
                st.success(f"{len(no)}件の控えを消しました（次の実行で送ります）。")
            st.rerun()


def render(supabase):
    gc = _gc_for(st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    try:
        cfg = renxa.load(supabase)
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
        return
    _settings(supabase, cfg)
    url = str(cfg.get("sheet_url", "") or "").strip()
    if not (url and gc):
        st.info("⚙️ 設定でスプレッドシートのURLを入れてください（接続キー GOOGLE_SERVICE_ACCOUNT_JSON も要ります）。")
        return

    st.caption(f"① SFコネクタで「{renxa.REPORT_TAB}」を更新 → ② 出てきた案件の中身を Salesforce から読み、"
               "フォームの選び方（電気・ガス・水道・ネット）を**SFのフラグに合わせて**決める → "
               "③ ロボットがトヨクモの「不動産個人情報取得フォーム」に1件ずつ入れて回答 → "
               f"④ 回答した案件だけ、Salesforce の**多言語窓口連携状況**を「{renxa.LINKED}」にします。")
    _last = cfg.get("last_run") or {}
    if _last:
        st.caption(f"前回：{_last.get('at', '')}（{'本番' if _last.get('submit') else 'お試し'}・"
                   f"できた {len(_last.get('ok') or [])}件／止まった {len(_last.get('ng') or [])}件）")
    _pending(supabase, cfg)

    staff = st.text_input("店舗担当者名（フォームに入れる名前）", key="rx_staff",
                          value=st.session_state.get("rx_staff_keep", ""),
                          help=f"時間指定で動くときは「{cfg.get('auto_staff') or renxa.DEFAULT_STAFF}」が入ります。")
    st.session_state.rx_staff_keep = staff
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 更新して確認する", type="primary", use_container_width=True, key="rx_refresh",
                     disabled=not staff.strip()):
            with st.spinner("SFのレポートを更新しています（数分かかることがあります）…"):
                ok, why = renxa.refresh(gc, cfg)
            if not ok:
                st.error(f"更新できませんでした：{why}")
                st.session_state.pop("rx_plan", None)
            else:
                with st.spinner("読んでいます…"):
                    st.session_state.rx_plan = renxa.plan(gc, cfg, staff.strip())
    with c2:
        if st.button("👀 更新せずに確認する", use_container_width=True, key="rx_peek", disabled=not staff.strip()):
            with st.spinner("読んでいます…"):
                st.session_state.rx_plan = renxa.plan(gc, cfg, staff.strip())
    if not staff.strip():
        st.caption("👆 先に店舗担当者名を入れてください。")

    res = st.session_state.get("rx_result")
    if res:
        _show_steps(res)
        if st.button("結果を閉じる", key="rx_close"):
            st.session_state.pop("rx_result", None)
            st.rerun()

    p = st.session_state.get("rx_plan")
    if not p:
        return
    if p["error"]:
        st.error(p["error"])
        return
    for k, msg in (("linked", "すでに連携済み"), ("not_target", "多言語窓口連携状況が「対象外」"),
                   ("held", "前に送った控えが残っている（上で確かめてください）")):
        if p[k]:
            st.caption(f"{msg}の {len(p[k])}件は送りません：" + "、".join(p[k][:10]))
    if p["missing"]:
        st.warning("Salesforce に見つからない案件IDがあります：" + "、".join(p["missing"][:10]))
    if p["noid"]:
        st.warning(f"案件 ID が空・形の違う行が {p['noid']}件 ありました（送りません）。")
    if not p["items"]:
        st.success("送る案件はありません。")
        return

    theme.section_title("🌐", f"送る案件 {len(p['items'])}件")
    st.caption("直せる列は表の中で直せます（直したら、もう一度入れられるか確かめます）。"
               "⚠️ が付いた案件は、直すまで送りません。「決めた理由」は、案内不要にした設備とそのSFのフラグです。")
    rows = []
    for it in p["items"]:
        v = it["vars"]
        rows.append({"送る": not it["problems"], "案件番号": renxa.case_label(it),
                     **{k: v.get(k, "") for k in renxa.EDITABLE},
                     "電話": "-".join(x for x in (v.get("電話番号1"), v.get("電話番号2"), v.get("電話番号3")) if x),
                     "郵便番号": v.get("郵便番号", ""), "生年月日": v.get("生年月日", ""), "入居日": v.get("入居日", ""),
                     "決めた理由": "／".join(it["why"]), "⚠️": "・".join(it["problems"])})
    df = pd.DataFrame(rows)
    col_cfg = {
        "電気の種別": st.column_config.SelectboxColumn(options=list(renxa.ELEC_OPTS)),
        "ガスの種別": st.column_config.SelectboxColumn(options=list(renxa.GAS_OPTS)),
        "水道の種別": st.column_config.SelectboxColumn(options=list(renxa.WATER_OPTS)),
        "インターネットの種別": st.column_config.SelectboxColumn(options=list(renxa.NET_OPTS)),
        "申込種別": st.column_config.SelectboxColumn(options=list(renxa.APPLY_OPTS)),
        "言語": st.column_config.SelectboxColumn(options=list(renxa.FORM_LANGS)),
    }
    ed = st.data_editor(df, hide_index=True, use_container_width=True, key=f"rx_ed_{id(p)}", column_config=col_cfg,
                        disabled=["案件番号", "電話", "郵便番号", "生年月日", "入居日", "決めた理由", "⚠️"])
    pick, still_bad = [], []
    for k, (_, r) in enumerate(ed.iterrows()):
        it = p["items"][k]
        v = dict(it["vars"])
        for c in renxa.EDITABLE:
            v[c] = renxa._clean(r[c])
        probs = renxa.validate(v)
        if not bool(r["送る"]):
            continue
        if probs:
            still_bad.append(f"{renxa.case_label(it)}：{'・'.join(probs)}")
            continue
        pick.append({**it, "vars": v})
    if still_bad:
        st.warning("⚠️ まだ入れられない案件があります（送りません）：\n\n" + "\n\n".join(still_bad))

    st.markdown("---")
    a, b = st.columns(2)
    with a:
        st.caption("🧪 お試し：先頭の1件だけ、フォームの**回答の手前（確認画面）まで**入れます。回答はしません。")
        if st.button("🧪 お試し（先頭1件・回答しない）", disabled=not pick, use_container_width=True, key="rx_try"):
            with st.spinner("ロボットがフォームに入れています（ログインのメールを待つことがあります）…"):
                out = renxa.send(supabase, cfg, pick[:1], submit=False)
            steps = auto_jobs._Steps()
            renxa.after_send(supabase, out, steps, submit=False)
            st.session_state.rx_result = steps.result()
            st.rerun()
    with b:
        sure = st.checkbox(f"{len(pick)}件を RENXA に回答し、Salesforce を「{renxa.LINKED}」にすることを確かめた"
                           "（取り消せません）", key="rx_sure")
        if st.button(f"🚀 {len(pick)}件を回答する", type="primary", disabled=not (sure and pick),
                     use_container_width=True, key="rx_go"):
            with st.spinner("ロボットがフォームに入れて回答しています…"):
                out = renxa.send(supabase, cfg, pick, submit=True)
            steps = auto_jobs._Steps()
            with st.spinner("Salesforce を連携済みにしています…"):
                renxa.after_send(supabase, out, steps, submit=True)
            st.session_state.rx_result = steps.result()
            st.session_state.pop("rx_plan", None)
            st.session_state.pop("rx_sure", None)
            st.rerun()
