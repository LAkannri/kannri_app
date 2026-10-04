"""
✉️ 39メール（契約のあとに送るご案内メール）

  ① SFコネクタで BOX を更新 → ② お客様ごとに使う文面を決めて、Gmailに下書きを作る
  → ③ DC担当者が下書きを確かめて送り、「完了」にチェック

⭐ 文面・出す条件・どの文面を使うか（振り分け）は、この画面（✏️ 文面と条件）で直す。
   商品が増えた・料金が変わった、はここで文面を足す／直すだけ（GASのコードは触らない）。
⭐ スプシの「確認用 → DC用」に写していた作業は「✅ DC（確認）」に置き換え。
中身は mail39.py。
"""
import copy

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client

import auto_jobs
import characters as ch
import mail39 as m
import theme

st.set_page_config(page_title="39メール - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("✉️", "39メール",
                  "契約のあとのご案内メールを、お客様ごとに文面を選んでGmailの下書きにします。"
                  "文面と出す条件は、この画面で直せます。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()


@st.cache_resource(show_spinner=False)
def _gc_cached(sa_json: str):
    return auto_jobs.gspread_client(sa_json, timeout_sec=30)


def _gc():
    sa = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    return _gc_cached(sa) if sa else None


@st.cache_data(ttl=600, show_spinner="BOXを読んでいます…")
def _read_box(set_json: str, stamp: str):
    import json
    return m.read_box(_gc(), json.loads(set_json))


def _box(set_name: str, set_cfg: dict):
    """BOXの中身（🔄 か 📄 を押したときだけ読み直す）。読めなければ None と理由。"""
    import json
    if not str(set_cfg.get("sheet_url", "")).strip():
        return None, "⚙️ 設定で、スプレッドシートのURLを入れてください"
    if not _gc():
        return None, "サービスアカウント（GOOGLE_SERVICE_ACCOUNT_JSON）がありません"
    keep = {k: set_cfg.get(k) for k in ("sheet_url", "box_tab", "legacy")}
    stamp = st.session_state.get(f"m39_stamp_{set_name}", "")
    try:
        return _read_box(json.dumps(keep, ensure_ascii=False, sort_keys=True), stamp), ""
    except Exception as e:
        return None, f"スプレッドシートを読めませんでした：{str(e)[:200]}"


def _reread(set_name: str):
    st.session_state[f"m39_stamp_{set_name}"] = m.now_stamp()


def _preview(mail: dict, to: str = "", from_addr: str = "", height: int = 520):
    st.markdown(f"**件名：** {mail.get('subject') or '（なし）'}")
    if to or from_addr:
        st.caption(f"宛先：{to}　／　差出人：{from_addr}")
    html = mail.get("html", "")
    for i, fid in enumerate(mail.get("images") or [], 1):
        html = html.replace(f'src="cid:img{i}"',
                            f'src="https://drive.google.com/thumbnail?id={fid}&sz=w600"')
    components.html(
        f'<div style="font-family:sans-serif;font-size:14px;line-height:1.6;'
        f'background:#fff;color:#222;padding:12px;border:1px solid #ddd;border-radius:8px">{html}</div>',
        height=height, scrolling=True)


# ==========================================
# 🧭 どのセット・どの画面
# ==========================================
cfg = m.load_cfg(supabase)
c1, c2 = st.columns([1, 3])
with c1:
    set_name = st.radio("どちらのメール？", list(m.DEFAULT_SETS.keys()), horizontal=True, key="m39_set",
                        format_func=lambda x: {"ネット": "🌐 ネット", "LL": "⚡ LL"}.get(x, x))
with c2:
    view = st.radio("画面", ["✉️ 下書きを作る", "✅ DC（確認）", "✏️ 文面と条件", "⚙️ 設定"],
                    horizontal=True, key="m39_view", label_visibility="collapsed")
S = cfg["sets"][set_name]

if set_name == "LL" and view != "⚙️ 設定":
    st.info("⚡ LL の39メールは、これからアプリに移します（電気・ガス・水道の連絡先を地域のマスタから引く作りのため、"
            "ネットのあとに取りかかります）。それまではスプシの「下書き作成」ボタンで作ってください。")
    st.stop()


# ==========================================
# ✉️ 下書きを作る
# ==========================================
def view_make():
    if not S.get("templates"):
        st.warning("まだ文面がありません。⚙️ 設定の「📥 スプシの文面を取り込む」から始めてください。")
        return
    b1, b2, b3 = st.columns([1.3, 1, 3])
    with b1:
        if st.button("🔄 BOXを更新する（SFコネクタ）", use_container_width=True,
                     disabled=not S.get("refresh_robot")):
            with st.spinner("SFコネクタで BOX を更新しています（数分かかることがあります）…"):
                ok, log = m.run_refresh(_gc(), S, set_name)
            if ok:
                m.save_cfg(supabase, {"last_refresh": m.now_stamp()}, set_name)
                _reread(set_name)
                st.success("BOXを更新しました")
            else:
                st.error("更新できませんでした。BOXは前の中身のままです。")
                st.code(str(log)[-1500:])
    with b2:
        if st.button("📄 読み直す", use_container_width=True):
            _reread(set_name)
    with b3:
        if S.get("last_refresh"):
            st.caption(f"最後にアプリから更新：{S['last_refresh']}")

    box, err = _box(set_name, S)
    if err:
        st.error(err)
        return
    if box.get("missing_tabs"):
        st.caption("（前のGASの記録のシートが見つかりません：" + "、".join(box["missing_tabs"]) + "）")
    logs = m.load_log(supabase, set_name)
    made_app = {k for k in logs}
    case_col, email_col = S.get("case_col", "案件番号"), S.get("email_col", "メールアドレス")

    rows, mails = [], {}
    for r in box["rows"]:
        case_no = str(r.get(case_col, "") or "").strip()
        mail = m.compose(S, r)
        er = m.enrich(r, S)
        key = m.log_key(case_no, mail["template"])
        state, pick = "✉️ これから", True
        if key in made_app:
            state, pick = f"✅ 作成済み（{str(logs[key].get('made', ''))[5:16]}）", False
        elif key in box["legacy"]:
            state, pick = "✅ 作成済み（前のGAS）", False
        elif "@" not in str(r.get(email_col, "")):
            state, pick = "⚠️ メールアドレスなし", False
        elif mail["error"]:
            state, pick = "⚠️ " + mail["error"], False
        mails[case_no] = (mail, r)
        rows.append({"作る": pick, "案件番号": case_no, "お客様": er.get("お客様名", ""),
                     "商品": r.get("*商品", r.get("商品", "")), "文面": mail["template"] or "—",
                     "担当者": er.get("担当者", ""), "状態": state})
    if not rows:
        st.info("BOXにお客様がいません。")
        return
    df = pd.DataFrame(rows)
    n_new = int((df["状態"] == "✉️ これから").sum())
    n_ng = int(df["状態"].str.startswith("⚠️").sum())
    st.markdown(f"**BOX {len(df)}件**　✉️ これから **{n_new}件**　⚠️ 作れない {n_ng}件")
    ed = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"m39_pick_{set_name}_{len(df)}",
        disabled=[c for c in df.columns if c != "作る"],
        column_config={"作る": st.column_config.CheckboxColumn("作る", width="small")})

    picked = [r for r in ed.to_dict("records") if r["作る"]]
    bad = [r["案件番号"] for r in picked if not r["状態"].startswith("✉️")]
    with st.expander("👀 文面を見る（1件ずつ）", expanded=False):
        sel = st.selectbox("どのお客様？", [r["案件番号"] for r in rows],
                           format_func=lambda x: f"{x}　{next(r['お客様'] for r in rows if r['案件番号'] == x)}"
                                                 f"　{next(r['文面'] for r in rows if r['案件番号'] == x)}")
        if sel:
            mail, r = mails[sel]
            if mail["error"]:
                st.error(mail["error"])
            if mail["template"]:
                st.caption(f"文面「{mail['template']}」の {len(mail['blocks'])} 段落を出しています"
                           f"（{'・'.join(str(i) for i in mail['blocks'])}番目）")
            _preview(mail, r.get(email_col, ""), S.get("from_addr", ""))

    if bad:
        st.warning("作れない／作成済みの行にチェックが入っています（作りません）：" + "、".join(bad))
    todo = [mails[r["案件番号"]][1] for r in picked if r["状態"].startswith("✉️")]
    acct = m.gmail_creds(supabase)
    if not acct:
        st.warning("Gmailの許可がまだありません（⚙️ 設定の「🔑 Gmailの許可を出す」）。")
    if st.button(f"✉️ チェックした {len(todo)} 件の下書きを作る", type="primary",
                 disabled=not todo or not acct):
        with st.status("下書きを作っています…", expanded=True) as stt:
            res = m.make_drafts(supabase, set_name, S, todo, log=st.write)
            stt.update(label=f"作りました：{len(res['ok'])}件／作れなかった：{len(res['ng'])}件",
                       state="complete" if not res["ng"] else "error")
        for case_no, why in res["ng"]:
            st.error(f"{case_no}：{why}")
        if res["ok"]:
            st.success("Gmailの「下書き」に入りました。「✅ DC（確認）」で確かめて、送ったら完了にしてください。")


# ==========================================
# ✅ DC（確認）
# ==========================================
def view_dc():
    logs = m.load_log(supabase, set_name)
    names = list(cfg.get("names") or [])
    show_done = st.toggle("済んだ分も出す（直近）", value=False, key=f"m39_showdone_{set_name}")
    items = []
    for k, v in logs.items():
        if v.get("done") and not show_done:
            continue
        items.append(dict(v, _key=k))
    items.sort(key=lambda x: str(x.get("made", "")), reverse=True)
    if not items:
        st.success("確認待ちの下書きはありません。")
        return
    st.caption("Gmailの「下書き」を開いて中身を確かめ、送ったら DC担当者 を選んで「完了」にチェック → 💾 保存。")
    df = pd.DataFrame([{
        "_key": x["_key"], "作成": str(x.get("made", ""))[5:16], "案件番号": x.get("case", ""),
        "お客様": x.get("name", ""), "宛先": x.get("email", ""), "文面": x.get("tpl", ""),
        "担当者": x.get("staff", ""), "DC担当者": x.get("dc", "") or None, "完了": bool(x.get("done")),
    } for x in items])
    opts = sorted(set(names) | {d for d in df["DC担当者"] if d})
    ver = st.session_state.get(f"m39_dcver_{set_name}", 0)
    ed = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"m39_dc_{set_name}_{ver}",
        disabled=["作成", "案件番号", "お客様", "宛先", "文面", "担当者"],
        column_order=["作成", "案件番号", "お客様", "宛先", "文面", "担当者", "DC担当者", "完了"],
        column_config={"DC担当者": st.column_config.SelectboxColumn("DC担当者", options=opts),
                       "完了": st.column_config.CheckboxColumn("完了", width="small")})
    if st.button("💾 保存", type="primary"):
        before = {r["_key"]: r for r in df.to_dict("records")}
        changes = {}
        for r in ed.to_dict("records"):
            b = before.get(r["_key"]) or {}
            dc = r.get("DC担当者") or ""
            if dc != (b.get("DC担当者") or "") or bool(r["完了"]) != bool(b.get("完了")):
                if r["完了"] and not dc:
                    st.error(f"{r['案件番号']}：DC担当者を選んでから完了にしてください")
                    return
                changes[r["_key"]] = {"dc": dc, "done": bool(r["完了"]),
                                      "done_at": m.now_stamp() if r["完了"] else ""}
        if changes:
            m.update_log(supabase, set_name, changes)
            st.session_state[f"m39_dcver_{set_name}"] = ver + 1
            st.success(f"{len(changes)}件を保存しました")
            st.rerun()
        else:
            st.info("変わったところはありません")


# ==========================================
# ✏️ 文面と条件
# ==========================================
def _known_cols() -> list:
    cols = []
    box, _ = _box(set_name, S) if S.get("sheet_url") else (None, "")
    if box:
        cols += [h for h in box["head"] if h]
    cols += ["お客様名", "担当者", m.CB_KEY, m.TPL_COL]
    for r in S.get("routes") or []:
        cols += [c.get("列", "") for c in r.get("when") or []]
    for t in (S.get("templates") or {}).values():
        for b in t.get("blocks") or []:
            cols += [c.get("列", "") for c in b.get("when") or []]
    return list(dict.fromkeys([c for c in cols if c]))


def _cond_editor(conds, key: str, cols: list):
    # ⚠️ 表に渡す元の中身は、最初に出したときのまま固定する（直したあとの中身を渡し直すと、
    #    足した行が毎回もう一度足される＝st.data_editor は「元からの差分」で覚えているため）
    conds = st.session_state.setdefault(key + "_base", copy.deepcopy(conds or []))
    df = pd.DataFrame([{"列": c.get("列", ""), "op": c.get("op", ""), "値": c.get("値", "")}
                       for c in conds or []] or [], columns=["列", "op", "値"])
    opts = list(dict.fromkeys(cols + [x for x in df["列"] if x]))
    ed = st.data_editor(
        df, key=key, num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={
            "列": st.column_config.SelectboxColumn("BOXの列", options=opts, required=True),
            "op": st.column_config.SelectboxColumn("どうなら", options=m.OPS, required=True),
            "値": st.column_config.TextColumn("値（いくつかなら , で区切る）")})
    out = []
    for r in ed.to_dict("records"):
        col = str(r.get("列") or "").strip()
        if not col:
            continue
        out.append({"列": col, "op": str(r.get("op") or "含む"),
                    "値": "" if r.get("op") in m.NO_VALUE_OPS else str(r.get("値") or "").strip()})
    return out


def view_edit():
    tpls = S.get("templates") or {}
    if not tpls:
        st.warning("まだ文面がありません。⚙️ 設定の「📥 スプシの文面を取り込む」から始めてください。")
        return
    cols = _known_cols()
    st.caption("✍️ 書き方：" + " ／ ".join(f"`{x}`" for x in m.MARK_HELP.split(" ／ ")))
    tab_r, tab_t = st.tabs(["🔀 どの文面を使うか（振り分け）", "📝 文面"])

    # ---------- 振り分け ----------
    with tab_r:
        st.caption("上から順に見て、**最初に条件が合った文面**を使います。どれにも合わないお客様は「⚠️ 作れない」になります。")
        rkey = f"m39_routes_{set_name}"
        if rkey not in st.session_state:
            st.session_state[rkey] = copy.deepcopy(S.get("routes") or [])
        routes = st.session_state[rkey]
        rv = st.session_state.get(rkey + "_v", 0)
        names = [n for n in tpls if n not in (m.HEAD, m.FOOT)]
        for i, r in enumerate(routes):
            r.setdefault("uid", m.new_uid())
            with st.expander(f"{i + 1}. {r.get('template') or '（未設定）'}　←　{m.describe(r.get('when'))}"):
                r["template"] = st.selectbox("使う文面", names,
                                             index=names.index(r["template"]) if r.get("template") in names else 0,
                                             key=f"m39_rt_{r['uid']}_{rv}")
                r["when"] = _cond_editor(r.get("when"), f"m39_rc_{r['uid']}_{rv}", cols)
                a, b, d = st.columns(3)
                if a.button("↑ 上へ", key=f"m39_ru_{r['uid']}", disabled=i == 0):
                    routes[i - 1], routes[i] = routes[i], routes[i - 1]
                    st.rerun()
                if b.button("↓ 下へ", key=f"m39_rd_{r['uid']}", disabled=i == len(routes) - 1):
                    routes[i + 1], routes[i] = routes[i], routes[i + 1]
                    st.rerun()
                if d.button("🗑 消す", key=f"m39_rx_{r['uid']}"):
                    routes.pop(i)
                    st.rerun()
        a, b, d = st.columns([1, 1, 1])
        if a.button("＋ 振り分けを足す"):
            routes.append({"uid": m.new_uid(), "template": names[0] if names else "", "when": []})
            st.rerun()
        if b.button("💾 振り分けを保存", type="primary"):
            m.save_cfg(supabase, {"routes": [{k: v for k, v in r.items()} for r in routes]}, set_name)
            st.session_state.pop(rkey, None)
            st.session_state[rkey + "_v"] = rv + 1
            st.rerun()
        if d.button("↩ 保存した状態に戻す", key="m39_r_back"):
            st.session_state.pop(rkey, None)
            st.session_state[rkey + "_v"] = rv + 1
            st.rerun()

    # ---------- 文面 ----------
    with tab_t:
        order = [m.HEAD] + [n for n in tpls if n not in (m.HEAD, m.FOOT)] + [m.FOOT]
        order = [n for n in order if n in tpls]
        a, b = st.columns([2, 1])
        _go = st.session_state.pop(f"m39_tgo_{set_name}", None)
        if _go is not None:
            st.session_state[f"m39_tsel_{set_name}"] = _go
        if st.session_state.get(f"m39_tsel_{set_name}") not in order:
            st.session_state.pop(f"m39_tsel_{set_name}", None)
        with a:
            name = st.selectbox("どの文面？", order, key=f"m39_tsel_{set_name}",
                                help="（冒頭）と（末尾）は、どの文面にも付きます")
        with b:
            new = st.text_input("＋ 新しい文面の名前", key=f"m39_new_{set_name}", placeholder="例：SB光10G")
            if st.button("＋ 作る（今の文面を写す）", disabled=not new.strip() or new.strip() in tpls):
                src = copy.deepcopy(tpls.get(name) or {"subject": "", "blocks": []})
                for blk in src.get("blocks") or []:
                    blk["uid"] = m.new_uid()
                m.save_template(supabase, set_name, new.strip(), src)
                st.session_state[f"m39_tgo_{set_name}"] = new.strip()
                st.rerun()

        ekey = f"m39_edit_{set_name}_{name}"
        if ekey not in st.session_state:
            st.session_state[ekey] = copy.deepcopy(tpls[name])
        t = st.session_state[ekey]
        ev = st.session_state.get(ekey + "_v", 0)
        used_by = [r.get("template") for r in S.get("routes") or []].count(name)
        if name not in (m.HEAD, m.FOOT):
            t["subject"] = st.text_input("件名", t.get("subject", ""), key=f"m39_subj_{ekey}_{ev}")
            if not used_by:
                st.warning("この文面は、振り分けのどこにも入っていません（このままでは使われません）。")
        blocks = t.setdefault("blocks", [])
        for i, blk in enumerate(blocks):
            blk.setdefault("uid", m.new_uid())
            u = f"{blk['uid']}_{ev}"
            with st.expander(f"{i + 1}. {blk.get('label') or '（呼び名なし）'}　—　{m.describe(blk.get('when'))}"):
                blk["label"] = st.text_input("呼び名（メールには出ません）", blk.get("label", ""), key=f"m39_bl_{u}")
                blk["text"] = st.text_area("文", blk.get("text", ""), height=200, key=f"m39_bt_{u}")
                st.markdown("**出す条件**（空なら、いつも出す）")
                blk["when"] = _cond_editor(blk.get("when"), f"m39_bc_{u}", cols)
                x1, x2, x3 = st.columns(3)
                if x1.button("↑ 上へ", key=f"m39_bu_{u}", disabled=i == 0):
                    blocks[i - 1], blocks[i] = blocks[i], blocks[i - 1]
                    st.rerun()
                if x2.button("↓ 下へ", key=f"m39_bd_{u}", disabled=i == len(blocks) - 1):
                    blocks[i + 1], blocks[i] = blocks[i], blocks[i + 1]
                    st.rerun()
                if x3.button("🗑 この段落を消す", key=f"m39_bx_{u}"):
                    blocks.pop(i)
                    st.rerun()
        y1, y2, y3 = st.columns([1, 1, 1])
        if y1.button("＋ 段落を足す"):
            blocks.append({"uid": m.new_uid(), "label": "", "text": "", "when": []})
            st.rerun()
        if y2.button("💾 この文面を保存", type="primary"):
            m.save_template(supabase, set_name, name, t)
            st.session_state.pop(ekey, None)
            st.session_state[ekey + "_v"] = ev + 1
            st.rerun()
        if y3.button("↩ 保存した状態に戻す"):
            st.session_state.pop(ekey, None)
            st.session_state[ekey + "_v"] = ev + 1
            st.rerun()

        if name not in (m.HEAD, m.FOOT):
            with st.expander("🗑 この文面ごと消す"):
                ok = st.checkbox("消してよい（元に戻せません）", key=f"m39_tdel_ok_{ekey}")
                if used_by:
                    st.caption(f"振り分けで {used_by} か所使われています。先に振り分けから外してください。")
                if st.button("🗑 消す", disabled=not ok or bool(used_by), key=f"m39_tdel_{ekey}"):
                    m.save_template(supabase, set_name, name, None)
                    st.session_state.pop(ekey, None)
                    st.session_state[f"m39_tgo_{set_name}"] = m.HEAD
                    st.rerun()

        # ---------- お試し ----------
        st.divider()
        st.markdown("#### 👀 お試し（保存する前の文面で見られます）")
        box, err = _box(set_name, S)
        if err or not box or not box["rows"]:
            st.caption("BOXにお客様がいないので、お試しできません。" + (err or ""))
            return
        case_col = S.get("case_col", "案件番号")
        cases = [r.get(case_col, "") for r in box["rows"]]
        sel = st.selectbox("BOXのどのお客様で見る？", cases, key=f"m39_try_{set_name}")
        row = box["rows"][cases.index(sel)]
        trial = dict(S, templates=dict(tpls, **{name: t}))
        tname = None if name in (m.HEAD, m.FOOT) else name
        mail = m.compose(trial, row, tname)
        if mail["error"]:
            st.error(mail["error"])
        if tname:
            st.caption(f"この文面で、このお客様に出る段落：{'・'.join(str(i) for i in mail['blocks']) or 'なし'}番目"
                       f"（ふだんの振り分けでは「{m.route(S, m.enrich(row, S)) or '合うものなし'}」）")
        _preview(mail, row.get(S.get("email_col", "メールアドレス"), ""), S.get("from_addr", ""))


# ==========================================
# ⚙️ 設定
# ==========================================
def view_settings():
    with st.form(f"m39_set_{set_name}"):
        url = st.text_input("スプレッドシートのURL（BOXがあるもの）", S.get("sheet_url", ""))
        a, b = st.columns(2)
        box_tab = a.text_input("お客様の一覧のシート", S.get("box_tab", "BOX"))
        robot = b.text_input("BOXを更新するロボット（空なら更新ボタンを出さない）", S.get("refresh_robot", ""))
        from_addr = a.text_input("差出人（From）", S.get("from_addr", ""))
        name_tpl = b.text_input("お客様名の作り方", S.get("name_tpl", "{名前（姓）} {名前（名）}"))
        staff_cols = a.text_input("担当者の列（左から見て、入っている最初の列）",
                                  ", ".join(S.get("staff_cols") or []))
        staff_default = b.text_input("担当者が空のときの名前", S.get("staff_default", "担当者"))
        cb_cols = a.text_input("キャッシュバックの列（左から見て、入っている最初の列）",
                               ", ".join(S.get("cb_cols") or []))
        lg = S.get("legacy") or {}
        legacy_tabs = b.text_input("前にGASで作った記録のシート（二重に作らないために読む）",
                                   ", ".join(lg.get("tabs") or []))
        names = st.text_area("DC担当者の名前（1行に1人・ネットとLLで共通）",
                             "\n".join(cfg.get("names") or []), height=150)
        if st.form_submit_button("💾 保存", type="primary"):
            split = lambda s: [x.strip() for x in str(s).replace("、", ",").split(",") if x.strip()]
            m.save_cfg(supabase, {
                "sheet_url": url.strip(), "box_tab": box_tab.strip() or "BOX",
                "refresh_robot": robot.strip(), "from_addr": from_addr.strip(),
                "name_tpl": name_tpl.strip(), "staff_cols": split(staff_cols),
                "staff_default": staff_default.strip(), "cb_cols": split(cb_cols),
                "legacy": dict(lg, tabs=split(legacy_tabs))}, set_name)
            m.save_cfg(supabase, {"names": [x.strip() for x in names.splitlines() if x.strip()]})
            st.success("保存しました")
            st.rerun()

    st.divider()
    st.markdown("#### 🔑 Gmail（下書きを入れる先）")
    st.caption("下書きは、ここで許可したアカウントのGmailに入ります。全PC共通です（1回だけ）。"
               "差出人（From）は、そのアカウントで「名前を付けて送信」に登録してあるアドレスだけ使えます。")
    if m.gmail_creds(supabase):
        info = m.gmail_account(supabase)
        if info["error"]:
            st.error("許可はありますが、Gmailにつながりません：" + info["error"])
        else:
            st.success(f"✅ {info['email']} のGmailに下書きを入れます")
            fa = str(S.get("from_addr", "")).strip().lower()
            if fa and info["send_as"] and fa not in [x.lower() for x in info["send_as"]]:
                st.error(f"差出人「{S.get('from_addr')}」は、このアカウントで使えません"
                         f"（使えるのは：{'、'.join(info['send_as'])}）。このままだと {info['email']} から送ることになります。")
        if st.button("🗑 許可を消す"):
            m.forget(supabase)
            st.rerun()
    else:
        st.warning("まだ許可がありません。")
    if st.button("🔑 Gmailの許可を出す（このPCのブラウザが開きます）"):
        try:
            with st.spinner("ブラウザで、下書きを入れたいアカウントを選んで「許可」を押してください…"):
                m.authorize_local(supabase)
            st.success("許可できました")
            st.rerun()
        except Exception as e:
            st.error(f"許可できませんでした：{str(e)[:300]}")

    st.divider()
    st.markdown("#### 📥 スプシの文面を取り込む（はじめの1回）")
    if set_name != "ネット":
        st.caption("LL はこれから対応します。")
        return
    st.caption("いまのスプシの文面シート（BIGLOBE・SB光…）と、GASに書いてあった出し分けの条件を、"
               "この画面で直せる形にして取り込みます。取り込んだあとはスプシの文面シートは使いません。")
    has = bool(S.get("templates"))
    ok = True
    if has:
        ok = st.checkbox("いまアプリにある文面と振り分けを、スプシの中身で置き換える（直した分は消えます）",
                         key="m39_imp_ok")
    if st.button("📥 取り込む", disabled=not ok or not S.get("sheet_url")):
        try:
            with st.spinner("文面シートを読んでいます…"):
                imp = m.import_net(_gc(), S["sheet_url"])
        except Exception as e:
            st.error(f"読めませんでした：{str(e)[:300]}")
            return
        m.save_cfg(supabase, {"routes": imp["routes"], "templates": imp["templates"],
                              "imported_at": m.now_stamp()}, set_name)
        for k in [k for k in st.session_state
                  if str(k).startswith("m39_") and k not in ("m39_set", "m39_view")]:
            st.session_state.pop(k, None)
        st.success(f"取り込みました：文面 {len(imp['templates']) - 2} 個・振り分け {len(imp['routes'])} 本")
        if imp["missing"]:
            st.warning("見つからなかったシート：" + "、".join(imp["missing"]))


{"✉️ 下書きを作る": view_make, "✅ DC（確認）": view_dc,
 "✏️ 文面と条件": view_edit, "⚙️ 設定": view_settings}[view]()
