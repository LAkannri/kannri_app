"""
📄 商品情報の更新

キャリアから届いた変更依頼（PDF・Word・画像・メール・文）を入れると、AIが
商品詳細（スプシ）とトークスクリプト（スライド）の直す場所を案にする。
人が見比べて選んだものだけを、すぐ／効く日に自動で直す。中身は product_update.py。
"""
import datetime

import pandas as pd
import streamlit as st
import gemini_key
from supabase import create_client, Client

import auto_jobs
import characters as ch
import product_update as pu
import theme

st.set_page_config(page_title="商品情報の更新 - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📄", "商品情報の更新",
                  "キャリアからの変更依頼を入れると、商品詳細とトークスクリプトの直す場所をAIが探します。"
                  "見比べて選んだものだけを、すぐ、または効く日に自動で直します。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()
SA = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


@st.cache_resource(show_spinner=False)
def _gc_cached(sa_json: str):
    return auto_jobs.gspread_client(sa_json, timeout_sec=60)


def _gc():
    return _gc_cached(SA) if SA else None


cfg = pu.load_cfg(supabase)
files = cfg.get("files") or []
KIND_JA = {"sheet": "スプレッドシート", "slides": "スライド"}
JA_KIND = {v: k for k, v in KIND_JA.items()}


def _schedule_ready() -> bool:
    try:
        sch = auto_jobs.load_row(supabase, "__schedule__")
        return any(it.get("kind") == "product_update" and it.get("enabled", True)
                   for it in sch.get("items") or [])
    except Exception:
        return True       # 読めないときは警告しない（確かめられないのに騒がない）


if not SA:
    st.error("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。スプシ・スライドを読めません。")
    st.stop()

tab_read, tab_log, tab_set = st.tabs(["📥 変更依頼を読む", "📅 予約と記録", "⚙️ 直す先のファイル"])

# ==========================================
# ⚙️ 直す先のファイル
# ==========================================
with tab_set:
    st.markdown("AIが探す・直す先のファイルです。サービスアカウントに**編集者**として共有してください。")
    try:
        import json as _json
        st.caption("共有するアドレス：`" + _json.loads(SA).get("client_email", "") + "`")
    except Exception:
        pass
    df = pd.DataFrame([{"名前": f.get("name", ""), "種類": KIND_JA.get(f.get("kind"), "スプレッドシート"),
                        "URL": f.get("url", "")} for f in files] or [{"名前": "", "種類": "スプレッドシート", "URL": ""}])
    ed = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="pu_files",
                        column_config={
                            "名前": st.column_config.TextColumn("名前", help="例：LLの商品詳細"),
                            "種類": st.column_config.SelectboxColumn("種類", options=list(KIND_JA.values()), required=True),
                            "URL": st.column_config.TextColumn("URL", width="large")})
    if st.button("💾 保存する", key="pu_files_save"):
        rows = []
        for _, r in ed.iterrows():
            name, url = str(r.get("名前") or "").strip(), str(r.get("URL") or "").strip()
            if name and url:
                rows.append({"name": name, "kind": JA_KIND.get(r.get("種類"), "sheet"), "url": url})
        if len({r["name"] for r in rows}) != len(rows):
            st.error("同じ名前のファイルがあります。名前は1つずつ変えてください。")
        else:
            pu.save_cfg(supabase, lambda latest: latest.update({"files": rows}))
            st.success(f"{len(rows)}件を保存しました。")
            st.rerun()
    if st.button("🔌 読めるか確かめる", key="pu_files_check"):
        with st.spinner("読んでいます…"):
            docs, ng = pu.read_docs(_gc(), SA, files)
        for name, d in docs.items():
            n = f"シート {len(d['tabs'])}枚" if d["kind"] == "sheet" else f"スライド {len(d['slides'])}枚"
            ce = pu.can_edit(SA, d["url"])
            if ce is False:
                st.warning(f"⚠️ {name}：{d['title']}（{n}）は読めますが、**書き換えられません**（編集者として共有してください）")
            else:
                st.success(f"✅ {name}：{d['title']}（{n}）" + ("・書き換えもできます" if ce else ""))
        for name, why in ng:
            st.error(f"🛑 {name}：{why}")

# ==========================================
# 📥 変更依頼を読む
# ==========================================
with tab_read:
    if not files:
        st.info("先に「⚙️ 直す先のファイル」で、商品詳細・トークスクリプトのURLを登録してください。")
    ups = st.file_uploader("変更依頼のファイル（ドラッグで入れられます）", type=pu.UPLOAD_TYPES,
                           accept_multiple_files=True, key="pu_upload")
    pasted = st.text_area("文を貼り付ける（メール本文など・任意）", key="pu_text", height=120)
    if st.button("🔎 変更点を読む", type="primary", disabled=not (files and (ups or pasted.strip())),
                 key="pu_analyze"):
        parts, ng = pu.notice_parts([(u.name, u.getvalue()) for u in ups or []], pasted)
        for x in ng:
            st.warning(f"読めなかったファイル：{x}")
        if parts:
            with st.spinner("資料を読んでいます…"):
                docs, dng = pu.read_docs(_gc(), SA, files)
            for name, why in dng:
                st.error(f"🛑 {name} を読めませんでした：{why}（このファイルは探しません）")
            if docs:
                with st.spinner("AIが変更点と直す場所を探しています…（1分ほど）"):
                    try:
                        prop = pu.analyze(gemini_key.api_key(st.secrets), parts, docs)
                        st.session_state.pu_prop = {
                            "prop": prop, "docs": docs,
                            "source": "、".join([u.name for u in ups or []] + (["貼り付けた文"] if pasted.strip() else []))}
                        st.session_state.pu_ver = st.session_state.get("pu_ver", 0) + 1
                    except Exception as e:
                        st.error(f"AIの読み取りに失敗しました：{str(e)[:300]}")

    box = st.session_state.get("pu_prop")
    if box:
        prop, docs = box["prop"], box["docs"]
        st.divider()
        theme.section_title("📝", f"AIが読んだ変更（{prop.get('carrier') or 'キャリア不明'}）")
        st.info(prop.get("summary") or "（要約なし）")

        edits = prop.get("edits") or []
        rows = []
        for e in edits:
            lc = pu.locate(docs, e)
            rows.append({"直す": bool(lc["ok"]), "ファイル": e["file"], "場所": pu.where_label(docs, e),
                         "前": pu._real(e["old"]), "あと": pu._real(e["new"]), "理由": e["reason"],
                         "確かめ": "✅ 直せます" if lc["ok"] else "⚠️ " + lc["why"], "_id": e["id"]})
        if not rows:
            st.warning("直せる場所は見つかりませんでした。下の「手で直すこと」を見てください。")
        else:
            st.caption("「直す」のチェックを外すと、その場所は直しません。「あと」の文字はここで直せます。"
                       "⚠️ の行は、いまの中身と合わないので直せません。")
            edf = st.data_editor(
                pd.DataFrame(rows), use_container_width=True, hide_index=True,
                key=f"pu_edits_{st.session_state.get('pu_ver', 0)}",
                disabled=["ファイル", "場所", "前", "理由", "確かめ", "_id"],
                column_config={"_id": None, "前": st.column_config.TextColumn("前", width="medium"),
                               "あと": st.column_config.TextColumn("あと", width="medium")})
            chosen, bad = [], []
            for _, r in edf.iterrows():
                if not r["直す"]:
                    continue
                e = next(x for x in edits if x["id"] == r["_id"])
                e2 = {**e, "new": str(r["あと"] or "").replace("\n", pu.NL)}
                lc = pu.locate(docs, e2)
                (chosen if lc["ok"] else bad).append((e2, lc))
            for e2, lc in bad:
                st.warning(f"⚠️ {e2['file']}／{pu.where_label(docs, e2)}：{lc['why']}（この行は直しません）")
            if chosen:
                with st.expander(f"👀 直したあとの中身（{len(chosen)}件）", expanded=True):
                    for e2, lc in chosen:
                        st.markdown(f"**{e2['file']}／{pu.where_label(docs, e2)}**")
                        a, b = st.columns(2)
                        a.code(lc["before"] or " ", language=None)
                        b.code(lc["after"] or " ", language=None)

        man = prop.get("manual") or []
        if man:
            theme.section_title("✋", f"手で直すこと（{len(man)}件）")
            for m in man:
                st.markdown(f"- **{m.get('file', '')}**　{m.get('where', '')}：{m.get('what', '')}")

        if rows and chosen:
            st.divider()
            theme.section_title("📅", "いつ直すか")
            eff = str(prop.get("effective_date") or "")
            try:
                d0 = datetime.date.fromisoformat(eff)
            except Exception:
                d0 = datetime.date.today()
            when = st.radio("いつ直すか", ["効く日に自動で直す（予約）", "いますぐ直す"],
                            index=0 if d0 > datetime.date.today() else 1, horizontal=True, key="pu_when")
            if when.startswith("効く日"):
                day = st.date_input("直す日", value=d0, key="pu_day")
                if not eff:
                    st.caption("⚠️ お知らせに効く日が見つかりませんでした。日付を確かめてください。")
                if not _schedule_ready():
                    st.warning("⏰ 時間指定の自動実行に「📄 商品情報の更新」の予定がありません。"
                               "予約しても、その日に自動では直りません（「⏰ 時間指定の自動実行」で毎日の予定を1本足してください）。")
            ok = st.checkbox(f"選んだ{len(chosen)}件を直すことを確かめた", key="pu_ok")
            label = "📅 予約する" if when.startswith("効く日") else "✏️ いま直す"
            if st.button(label, type="primary", disabled=not ok, key="pu_go"):
                apply_on = (day.isoformat() if when.startswith("効く日") else datetime.date.today().isoformat())
                change = pu.new_change(prop, [e2 for e2, _ in chosen], apply_on, box["source"])
                pu.add_change(supabase, change)
                if when.startswith("効く日"):
                    st.success(f"📅 {apply_on} に直す予約をしました（「📅 予約と記録」で見られます）。")
                else:
                    with st.spinner("直しています…"):
                        done = pu.apply_change(supabase, _gc(), SA, pu.load_cfg(supabase), change)
                    (st.success if done["state"] == "applied" else st.warning)(pu.STATES[done["state"]])
                    for line in pu.result_lines(done):
                        st.write(line)
                st.session_state.pop("pu_prop", None)
                st.session_state.pop("pu_ok", None)

# ==========================================
# 📅 予約と記録
# ==========================================
with tab_log:
    changes = list(reversed(cfg.get("changes") or []))
    sched = [x for x in changes if x.get("state") == "scheduled"]
    if sched and not _schedule_ready():
        st.warning("⏰ 時間指定の自動実行に「📄 商品情報の更新」の予定がありません。予約は、その日に自動では直りません。")
    if not changes:
        st.info("まだ記録はありません。")
    for chg in changes[:50]:
        state = pu.STATES.get(chg.get("state"), chg.get("state"))
        head = f"{state}　{chg.get('apply_on', '')}　{chg.get('carrier') or ''}　{chg.get('source') or ''}"
        with st.expander(head, expanded=chg.get("state") in ("scheduled", "partial", "failed")):
            st.caption(f"作った日：{chg.get('created_at')}（{chg.get('by')}）"
                       + (f"／直した日：{chg.get('applied_at')}（{chg.get('applied_by')}）" if chg.get("applied_at") else ""))
            st.write(chg.get("summary") or "")
            res = {r["id"]: r for r in chg.get("results") or []}
            st.dataframe(pd.DataFrame([{
                "結果": (res.get(e["id"]) or {}).get("mark", "—"),
                "ファイル": e["file"], "場所": (e.get("tab") + "!" + e.get("cell")) if e.get("cell") else "スライド",
                "前": pu._real(e["old"]), "あと": pu._real(e["new"]),
                "メモ": (res.get(e["id"]) or {}).get("why", "")} for e in chg.get("edits") or []]),
                use_container_width=True, hide_index=True)
            for m in chg.get("manual") or []:
                st.markdown(f"- ✋ **{m.get('file', '')}**　{m.get('where', '')}：{m.get('what', '')}")
            cid = chg["id"]
            if chg.get("state") == "scheduled":
                a, b = st.columns(2)
                if a.button("▶ いま直す", key=f"pu_now_{cid}"):
                    with st.spinner("直しています…"):
                        pu.apply_change(supabase, _gc(), SA, pu.load_cfg(supabase), chg)
                    st.rerun()
                if b.button("🗑 予約を取り消す", key=f"pu_cancel_{cid}"):
                    pu.update_change(supabase, cid, {"state": "canceled"})
                    st.rerun()
            if chg.get("state") in ("applied", "partial"):
                ok = st.checkbox("元に戻すことを確かめた", key=f"pu_undo_ok_{cid}")
                if st.button("↩ 元に戻す", disabled=not ok, key=f"pu_undo_{cid}"):
                    with st.spinner("戻しています…"):
                        back = pu.undo_edits(_gc(), SA, files, chg.get("edits") or [], chg.get("results") or [])
                    bad = [r for r in back if r["mark"] not in ("✅", "⏭")]
                    pu.update_change(supabase, cid, {"state": "undone" if not bad else chg["state"],
                                                     "undo": back, "undone_at": pu.now_str()})
                    if bad:
                        st.warning("戻せなかった場所があります：" + "／".join(r["why"] for r in bad))
                    else:
                        st.rerun()
