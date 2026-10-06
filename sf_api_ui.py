"""
☁️ SFコネクタを使わない更新 ─ どのシートを切り替えたかの画面（⚙️ その他設定から呼ぶ）

中身は sf_report_sheet.py。ここは「確かめる」「人が見て決める」「止める」だけ。
"""
import pandas as pd
import streamlit as st

import sf_report_sheet as m


def _gc():
    import auto_jobs
    return auto_jobs.gspread_client(st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""), timeout_sec=60)


def render(supabase):
    st.markdown("### ☁️ SFコネクタを使わない更新")
    st.caption("SFコネクタで更新していたシートを、**Salesforceのレポートから直接**書き写します"
               "（ロボット・ブラウザ・Googleのログインが要らない＝クラウドの画面からも動きます）。"
               "**確かめて値が変わらないと分かったシートだけ**切り替わり、ほかはこれまでどおりSFコネクタで更新します。"
               "SFコネクタの設定はシートに残るので、スプシからコネクタで更新することもできます。")
    cfg = m.load_cfg(supabase)
    on = st.toggle("確かめたシートは、Salesforceから直接更新する", value=bool(cfg.get("on", True)),
                   key="sfapi_on", help="外すと、全部のシートがこれまでどおりSFコネクタ（ロボット）で更新します")
    if on != bool(cfg.get("on", True)):
        m.save_cfg(supabase, {"on": on})
        st.rerun()

    tabs = cfg.get("tabs") or {}
    rows = []
    for tid, t in sorted(tabs.items(), key=lambda kv: (kv[1].get("sheet_name", ""), kv[1].get("tab", ""))):
        res = t.get("result", "")
        label = m.LABEL.get(res, "—")
        if res == m.CHECK and t.get("approved"):
            label = "✅ 切り替え済み（人がOK）"
        rows.append({"_id": tid, "スプレッドシート": t.get("sheet_name", ""), "シート": t.get("tab", ""),
                     "結果": label, "わけ": " ／ ".join(t.get("problems") or []) or "—",
                     "メモ": " ／ ".join(t.get("notes") or []), "確かめた日時": t.get("checked_at", ""),
                     "人がOK": bool(t.get("approved"))})
    if not rows:
        st.info("まだ確かめていません。下の「🔍 全部確かめる」を押してください。")
    else:
        n_ok = sum(1 for r in rows if r["結果"].startswith("✅"))
        st.write(f"**{n_ok} / {len(rows)} 枚**が Salesforce から直接の更新に切り替わっています。")
        df = pd.DataFrame(rows)
        ed = st.data_editor(
            df.drop(columns=["_id"]), hide_index=True, use_container_width=True, key="sfapi_table",
            disabled=["スプレッドシート", "シート", "結果", "わけ", "メモ", "確かめた日時"],
            column_config={"人がOK": st.column_config.CheckboxColumn(
                help="🔎 のシートだけ。わけを見て、切り替えてよければチェック → 下の「💾 保存」")})
        st.caption("🔎 人が見て決める：レポートの列がシートと違う・行の並びが違う・ある列がほぼ全部違う、など。"
                   "わけを見て、そのシートを見ている数式が崩れないと分かれば「人がOK」にします。"
                   "⚠️ 書き方が違う（日付が文字になる、など）シートは切り替えません。")
        if st.button("💾 「人がOK」を保存", key="sfapi_save"):
            ch = {}
            for r, (_, e) in zip(rows, ed.iterrows()):
                want = bool(e["人がOK"])
                if want != r["人がOK"]:
                    if want and (tabs.get(r["_id"]) or {}).get("result") != m.CHECK:
                        st.warning(f"「{r['シート']}」は 🔎 ではないので、人がOKにはできません")
                        continue
                    ch[r["_id"]] = {"approved": want}
            if ch:
                m.save_cfg(supabase, tabs=ch)
                st.success(f"{len(ch)}枚を保存しました")
                st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        sure = st.checkbox("確かめる（各スプシに試しのシート【API試し】を一瞬だけ作って消します）", key="sfapi_sure")
    with c2:
        if st.button("🔍 全部確かめる（30分〜1時間）", disabled=not sure, key="sfapi_verify"):
            gc = _gc()
            if gc is None:
                st.error("サービスアカウント（GOOGLE_SERVICE_ACCOUNT_JSON）がありません")
                return
            bar = st.progress(0.0, text="スプシを探しています…")
            try:
                m.verify_all(supabase, gc, on_progress=lambda i, n, t: bar.progress(
                    i / max(n, 1), text=f"{i}/{n}：{t['sheet_name']} ／ {t['tab']}"))
            except Exception as e:
                st.error(f"途中で止まりました：{e}")
                return
            st.rerun()
