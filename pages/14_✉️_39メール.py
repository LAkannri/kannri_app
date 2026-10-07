"""
✉️ 39メール（契約のあとに送るご案内メール）

  ① SFコネクタで BOX を更新 → ② お客様ごとに使う文面を決めて、Gmailに下書きを作る
  → ③ DC担当者が下書きを確かめて送り、「完了」にチェック

⭐ 文面・出す条件・どの文面を使うか（振り分け）は、この画面（✏️ 文面と条件）で直す。
   商品が増えた・料金が変わった、はここで文面を足す／直すだけ（GASのコードは触らない）。
⭐ スプシの「確認用 → DC用」に写していた作業は「📨 確認して送る」に置き換え（1件ずつ直して送信＝送った瞬間にDC完了）。
中身は mail39.py。
"""
import copy
import re

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
    keep = {k: set_cfg.get(k) for k in ("sheet_url", "box_tab", "legacy", "region_master_url")}
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
    view = st.radio("画面", ["✉️ 下書きを作る", "📨 確認して送る", "✏️ 文面と条件", "⚙️ 設定"],
                    horizontal=True, key="m39_view", label_visibility="collapsed")
S = cfg["sets"][set_name]

if set_name == "LL" and view != "⚙️ 設定" and not S.get("region_master_url"):
    st.warning("⚡ LL は、電気・ガス・水道の連絡先を「地域マスタ」（別のスプシ）から引きます。"
               "⚙️ 設定で、地域マスタのURLを入れてください。")


# ==========================================
# 🔁 作り直す（作成済みの下書きを消して、もう一度作れるようにする）
# ==========================================
#   ⭐ 文面（✏️ 文面と条件）を直したあと・中身が違ったときに使う。記録を消すと「作る」にチェックできる。
#   ⚠️ 消すのは記録と、まだ送っていない Gmail の下書きだけ（送ったメールは取り消せない）。
#   ⚠️ 前のGAS（スプシの「送信履歴」など）の記録は、送った記録そのものなのでアプリからは消さない＝名指しする。
def _redo_box(ed, keys: dict, redo_kind: dict, logs: dict):
    msg = st.session_state.pop("m39_redo_msg", None)
    if msg:
        if msg[0]:
            st.success(f"🔁 {len(msg[0])} 件を作り直せるようにしました（{'、'.join(msg[0])}）。"
                       "上の表の「作る」にチェックを入れて、もう一度作ってください。")
        for case_no, why in msg[1]:
            st.error(f"{case_no}：{why}")
    picked = [str(r["案件番号"]) for r in ed.to_dict("records") if r.get("🔁 作り直す")]
    if not picked:
        return
    app = [c for c in picked if redo_kind.get(c) in ("app", "済")]
    sent = [c for c in picked if redo_kind.get(c) == "済"]
    legacy = [c for c in picked if redo_kind.get(c) == "legacy"]
    yet = [c for c in picked if c not in redo_kind]
    with st.container(border=True):
        st.markdown(f"#### 🔁 作り直す（{len(app)}件）")
        st.caption("「✅ 作成済み」の記録を消して、もう一度作れるようにします。"
                   "Gmailの古い下書きも一緒に消すので、同じ案件の下書きが2通になりません。"
                   "消したあと、その行の「作る」にチェックを入れて作り直してください。")
        if yet:
            st.info("まだ作っていないので、作り直す必要はありません（そのまま「作る」で作れます）：" + "、".join(yet))
        if legacy:
            st.warning("前のGAS（スプシの「確認用」「DC用」「「送信履歴」」）に残っている記録です。"
                       "送った記録そのものなので、アプリからは消しません。もう一度作るなら、"
                       "スプシのその行を手で消してから「📄 読み直す」を押してください：" + "、".join(legacy))
        if sent:
            st.error("🚨 もう送ったお客様です。作り直して送ると、**お客様に2通目が届きます**"
                     "（1通目は取り消せません）：" + "、".join(sent))
        if not app:
            return
        ok = st.checkbox("Gmailの古い下書きが消えることを確かめました", key=f"m39_redook_{set_name}")
        if st.button(f"🔁 選んだ {len(app)} 件を作り直せるようにする", disabled=not ok, use_container_width=True):
            done, ng = [], []
            for case_no in app:
                why = m.redo(supabase, set_name, keys[case_no])
                (ng.append((case_no, why)) if why else done.append(case_no))
            st.session_state[f"m39_tblver_{set_name}"] = st.session_state.get(f"m39_tblver_{set_name}", 0) + 1
            st.session_state["m39_redo_msg"] = (done, ng)
            st.rerun()


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

    rows, mails, keys, redo_kind = [], {}, {}, {}
    for r in box["rows"]:
        case_no = str(r.get(case_col, "") or "").strip()
        mail = m.compose(S, r, masters=box.get("masters"))
        er = m.enrich(r, S, box.get("masters"))
        key = m.log_key(case_no, mail["template"])
        keys[case_no] = key
        state, pick = "✉️ これから", True
        if key in made_app:
            state, pick = f"✅ 作成済み（{str(logs[key].get('made', ''))[5:16]}）", False
            # 🔁 アプリの記録なので、ここから消して作り直せる
            redo_kind[case_no] = "済" if logs[key].get("done") else "app"
        elif m.legacy_made(box, case_no, mail["template"]):
            state, pick = "✅ 作成済み（前のGAS）", False
            redo_kind[case_no] = "legacy"
        elif "@" not in str(r.get(email_col, "")):
            state, pick = "⚠️ メールアドレスなし", False
        elif mail["error"]:
            state, pick = "⚠️ " + mail["error"], False
        mails[case_no] = (mail, r)
        goods = r.get("*商品") or " / ".join(x for x in (r.get("電力キャリア", ""), r.get("ガスキャリア", "")) if x)
        lk = m.leaks(S, r, mail, box.get("masters")) if not mail["error"] else []
        rows.append({"作る": pick, "案件番号": case_no, "お客様": er.get("お客様名", ""),
                     "商品": goods, "文面": mail["template"] or "—",
                     "担当者": er.get("担当者", ""), "状態": state,
                     "情報漏れ": ("⚠️ " + "・".join(lk)) if lk else ("なし" if state.startswith("✉️") else ""),
                     "🔁 作り直す": False})
    if not rows:
        st.info("BOXにお客様がいません。")
        return
    df = pd.DataFrame(rows)
    n_new = int((df["状態"] == "✉️ これから").sum())
    n_ng = int(df["状態"].str.startswith("⚠️").sum())
    st.markdown(f"**BOX {len(df)}件**　✉️ これから **{n_new}件**　⚠️ 作れない {n_ng}件")
    ver = st.session_state.get(f"m39_tblver_{set_name}", 0)
    ed = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"m39_pick_{set_name}_{len(df)}_{ver}",
        disabled=[c for c in df.columns if c not in ("作る", "🔁 作り直す")],
        column_config={"作る": st.column_config.CheckboxColumn("作る", width="small"),
                       "🔁 作り直す": st.column_config.CheckboxColumn(
                           "🔁 作り直す", width="small",
                           help="✅ 作成済みの下書きを消して、もう一度作れるようにします（文面を直したときなど）")})
    _redo_box(ed, keys, redo_kind, logs)

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

    if S.get("region_master_url"):
        _master_missing(box, rows)

    if bad:
        st.warning("作れない／作成済みの行にチェックが入っています（作りません）：" + "、".join(bad))
    todo = [mails[r["案件番号"]][1] for r in picked if r["状態"].startswith("✉️")]
    acct = m.gmail_creds(supabase)
    if not acct:
        st.warning("Gmailの許可がまだありません（⚙️ 設定の「🔑 Gmailの許可を出す」）。")
    clean = [r for r in todo if not m.leaks(S, r, mails[str(r.get(S.get("case_col", "案件番号"), "")).strip()][0],
                                                box.get("masters"))]
    st.caption(f"🔎 チェックした {len(todo)} 件のうち、情報漏れなし **{len(clean)} 件**／漏れあり **{len(todo) - len(clean)} 件**。"
               "見ているのは機械で見つけられる漏れだけです（未定の日付・●●・地域マスタに無い手配先・担当者やLPガス情報の空など）。"
               "特記事項の読み落としのような中身の判断はしません。")
    a, b = st.columns(2)
    go_draft = a.button(f"✉️ {len(todo)} 件とも下書きを作る（送らない）", disabled=not todo or not acct,
                        use_container_width=True)
    ok_send = b.checkbox(f"漏れの無い {len(clean)} 件は、確かめずに送ってよい（送ると取り消せません）",
                         key=f"m39_okauto_{set_name}")
    go_send = b.button(f"🚀 漏れの無い {len(clean)} 件は送信まで／漏れのある分は下書き", type="primary",
                       disabled=not todo or not acct or not ok_send, use_container_width=True)
    if go_draft or go_send:
        with st.status("作っています…", expanded=True) as stt:
            res = m.make_drafts(supabase, set_name, S, todo, log=st.write, masters=box.get("masters"),
                                send_clean=bool(go_send))
            stt.update(label=f"作りました：{len(res['ok'])}件（うち送信 {len(res['sent'])}件）／作れなかった：{len(res['ng'])}件",
                       state="complete" if not res["ng"] else "error")
        if res["sent"]:
            n, why = _write_history(res["sent"], m.load_log(supabase, set_name))
            st.success(f"📨 情報漏れの無い {len(res['sent'])} 件を送りました（DC完了：{m.AUTO_DC}）"
                       + (f"・送信履歴に {n} 件足しました" if n else ""))
            if why:
                st.error(why)
        if res.get("fusen"):
            st.success(f"📌 {len(res['fusen'])} 件に L-付箋（LPガス情報なし）を付けました："
                       + "／".join(res["fusen"]))
        for case_no, note in res.get("notes") or []:
            st.warning(f"📌 {case_no}：{note}")
        for case_no, why in res["ng"]:
            st.error(f"{case_no}：{why}")
        if res["held"]:
            st.warning("⚠️ 情報漏れのある分は下書きにしました。「📨 確認して送る」で直して送ってください：\n\n"
                       + "\n".join(f"- {c}：{'・'.join(w)}" for c, w in res["held"]))
        elif res["ok"] and not go_send:
            st.success("Gmailの「下書き」に入りました。「📨 確認して送る」で1件ずつ確かめて送信してください（送るとDC完了になります）。")


def _master_missing(box: dict, rows: list):
    """⚡ LL：地域マスタに無かった手配先（これから作るお客様の分だけ）。その場でマスタに足せる。

    ⭐ 地域手配SMSと同じマスタ・同じきまり（確認・出典・追記日を残す／電話番号の形を確かめる）。
       足したものは、地域手配SMS・引越し前SMSにもそのまま効く。
    """
    todo = {r["案件番号"] for r in rows if not r["状態"].startswith("✅")}
    seen, items = set(), []
    case_col = S.get("case_col", "案件番号")
    for r in box["rows"]:
        if str(r.get(case_col, "")).strip() not in todo:
            continue
        for it in m.ll_missing(r, box.get("masters") or {}):
            k = (it["種類"], it["都道府県"], it["市区郡"] if it["種類"] == "水道" else "",
                 it["郵便番号"] if it["種類"] == "ガス" else "")
            if k not in seen:
                seen.add(k)
                items.append(dict(it, 案件=str(r.get(case_col, ""))))
    if not items:
        return
    with st.container(border=True):
        st.markdown(f"##### 🗺 地域マスタに無かった手配先（{len(items)}件）")
        st.caption("このままでもメールは作れて、送れます（地域手配SMSと同じく「管轄の◯◯へお問い合わせください」と入ります）。"
                   "調べて足すと、このメールにも、地域手配SMS・引越し前SMSにも連絡先が入るようになります。"
                   "電話番号は形（0で始まる10〜11桁）を確かめてから書きます。出典（調べたページのURL）と追記日も残ります。")
        for i, it in enumerate(items):
            where = (f"{it['都道府県']}{it['市区郡']}" if it["種類"] == "水道"
                     else f"〒{it['郵便番号']}（{it['都道府県']}{it['市区郡']}）" if it["種類"] == "ガス"
                     else it["都道府県"])
            with st.expander(f"{ {'電力': '⚡', 'ガス': '🔥', '水道': '💧'}[it['種類']] } {it['種類']}：{where}"
                             + (f"　エリア「{it['エリア']}」の連絡先が無い" if it.get("エリア") else "")
                             + f"　（{it['案件']}）"):
                k = f"m39_mm_{i}_{it['種類']}_{where}"
                a, b = st.columns(2)
                label = {"水道": "水道局の名前", "ガス": "ガス会社の名前（エリア名）", "電力": "電力会社の名前"}[it["種類"]]
                name = a.text_input(label, it.get("エリア", ""), key=k + "_n")
                phone = b.text_input("電話番号（引越し・開栓の受付）", key=k + "_p")
                src = st.text_input("出典（調べたページのURL）", key=k + "_s")
                hours = days = ""
                if it["種類"] == "水道":
                    c1, c2 = st.columns(2)
                    hours = c1.text_input("受付時間（任意）", key=k + "_h")
                    days = c2.text_input("営業日・備考（任意）", key=k + "_d")
                if phone and not m.phone_ok(phone):
                    st.warning("電話番号の形ではありません（0で始まる10〜11桁）")
                if st.button("➕ 地域マスタに足す", key=k + "_add",
                             disabled=not name.strip() or not m.phone_ok(phone) or not src.strip()):
                    try:
                        msg = m.add_to_master(_gc(), S["region_master_url"], it["種類"], it, name, phone,
                                              source=src.strip(), hours=hours, days=days)
                        _reread(set_name)
                        st.success(msg + "。読み直します。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"足せませんでした：{str(e)[:200]}")


# ==========================================
# 📨 確認して送る（DC）
# ==========================================
def view_dc():
    """📨 確認して送る。

    ⭐ 担当者 2026-10-04：下書きを作ったら、確認用に出ていた項目を見ながら1件ずつ直し、アプリから送信する。
       送った瞬間に DC 完了（DC担当者＝送った人）＋送信履歴に1行。開いた人は「確認中」になり、ほかの人は送れない。
    """
    names = list(cfg.get("names") or [])
    if "m39_me" not in st.session_state:
        st.session_state.m39_me = st.query_params.get("me", "")
    opts = ["（選んでください）"] + names
    me = st.selectbox("🙋 あなたの名前（DC担当者）", opts,
                      index=opts.index(st.session_state.m39_me) if st.session_state.m39_me in opts else 0,
                      help="名前は ⚙️ 設定の「DC担当者の名前」で足せます")
    if me == opts[0]:
        st.info("名前を選ぶと、確認と送信ができます。")
        return
    if me != st.session_state.m39_me:
        st.session_state.m39_me = me
        st.query_params["me"] = me

    logs = m.load_log(supabase, set_name)
    pend = sorted([(k, v) for k, v in logs.items() if not v.get("done")],
                  key=lambda kv: str(kv[1].get("made", "")))
    ck = f"m39_cur_{set_name}"
    if not pend:
        st.success("確認待ちの下書きはありません。")
    else:
        rows = []
        for k, v in pend:
            other = m._fresh_claim(v, me)
            mine = (v.get("claim") or {}).get("by") == me
            rows.append({"作成": str(v.get("made", ""))[5:16], "案件番号": v.get("case", ""),
                         "お客様": v.get("name", ""), "文面": v.get("tpl", ""), "担当者": v.get("staff", ""),
                         "状態": f"👀 {other} さんが確認中" if other else ("✏️ あなたが確認中" if mine else "✉️ まだ")})
        st.markdown(f"**確認待ち {len(pend)} 件**（古い順）")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     height=min(36 * (len(rows) + 1) + 4, 400))
        keys = [k for k, _ in pend]
        free = [k for k, v in pend if not m._fresh_claim(v, me)]
        a, b = st.columns([3, 1])
        cur = st.session_state.get(ck)
        pick = a.selectbox("どれを確かめる？", keys,
                           index=keys.index(cur) if cur in keys else (keys.index(free[0]) if free else 0),
                           format_func=lambda k: f"{logs[k].get('case', '')}　{logs[k].get('name', '')}　{logs[k].get('tpl', '')}"
                                                 + (f"　👀 {m._fresh_claim(logs[k], me)} さんが確認中" if m._fresh_claim(logs[k], me) else ""),
                           key=f"m39_pick_send_{set_name}")
        b.write("")
        if b.button("✏️ 確かめる", type="primary", use_container_width=True) and pick:
            other = m.claim(supabase, set_name, pick, me)
            if other:
                st.session_state[f"m39_other_{set_name}"] = (pick, other)
            else:
                st.session_state[ck] = pick
            st.rerun()
        oth = st.session_state.get(f"m39_other_{set_name}")
        if oth and oth[0] == pick:
            st.warning(f"{oth[1]} さんが確認中です。ダブらないよう、ほかの下書きを選んでください。")
            if st.button(f"🔁 {oth[1]} さんから引き継ぐ（{oth[1]} さんが止めているときだけ）"):
                m.claim(supabase, set_name, pick, me, force=True)
                st.session_state[ck] = pick
                st.session_state.pop(f"m39_other_{set_name}", None)
                st.rerun()

        cur = st.session_state.get(ck)
        e = logs.get(cur) if cur else None
        if e and not e.get("done"):
            if m._fresh_claim(e, me):
                st.warning(f"{m._fresh_claim(e, me)} さんが引き継ぎました。")
                st.session_state.pop(ck, None)
            else:
                _review(cur, e, me)

    done_msg = st.session_state.pop("m39_sent_msg", None)
    if done_msg:
        st.success(done_msg[0])
        if done_msg[1]:
            st.error(done_msg[1])

    with st.expander("✅ 済んだ分を見る（きょう）"):
        _dc_done(me)


def _review(key: str, e: dict, me: str):
    """1件ぶん：確認項目・直す欄・見え方・送信。"""
    st.divider()
    st.markdown(f"#### ✏️ {e.get('case', '')}　{e.get('name', '')}　（{e.get('tpl', '')}）")
    v = st.session_state.get(f"m39_rv_{key}", 0)
    if not e.get("markup"):
        st.info("この下書きは中身を控えていない古い下書きです。"
                "Gmailの下書きで確かめて送ってから、「✅ 送信済みにする」を押してください。")
        a, b = st.columns(2)
        _sent_button(key, e, me, v, a)
        if b.button("↩ 確認をやめる（ほかの人に渡す）", key=f"m39_rel_{key}_{v}", use_container_width=True):
            m.release(supabase, set_name, key, me)
            st.session_state.pop(f"m39_cur_{set_name}", None)
            st.session_state[f"m39_rv_{key}"] = v + 1
            st.rerun()
        return
    dc = [x for x in (e.get("leaks") or []) if str(x).startswith(m.DC_LEAK)]
    other = [x for x in (e.get("leaks") or []) if x not in dc]
    for x in dc:
        st.warning("📝 **" + m.DC_LEAK + "**（BOXに書いてあること）：\n\n" + str(x)[len(m.DC_LEAK) + 1:]
                   + "\n\nこの中身を見て、本文を直してから送ってください。")
    if other:
        st.error("⚠️ 情報漏れ：" + "・".join(other) + "（直してから送ってください）")
    for f in e.get("follow") or []:
        txt = (f"内容・情報確認／{f.get('内容詳細', '')}／対応先 不動産／次回連絡日 {f.get('次回連絡日', '')}")
        if e.get("fusen_at"):
            st.info(f"📌 案件にL-付箋を付けてあります（{str(e['fusen_at'])[5:16]}）：{txt}")
        elif e.get("fusen_note") or e.get("fusen_error"):
            st.warning("📌 " + str(e.get("fusen_note") or e.get("fusen_error")))
        else:
            st.warning(f"📌 L-付箋がまだ付いていません：{txt}"
                       + "　→ 送ると付けます（「✅ 済んだ分を見る」の「📌 付箋を付け直す」でも付けられます）")
    # 確認項目は折り返して読めるように（表だと狭い画面で中身が切れる。特記事項は長い）
    with st.container(border=True):
        st.markdown("**🔎 確認項目**（これまで「確認用」シートに出ていた項目）")
        items = e.get("check") or []
        half = (len(items) + 1) // 2
        c1, c2 = st.columns(2)
        for col, part in ((c1, items[:half]), (c2, items[half:])):
            with col:
                for lab, val in part:
                    _v = "".join(chr(92) + ch if ch in "`*_[]$<>#|~" + chr(92) else ch for ch in str(val).strip()) or "—"
                    st.markdown(f"**{lab}**：{_v}")
    to = st.text_input("宛先", e.get("to") or e.get("email", ""), key=f"m39_to_{key}_{v}")
    subject = st.text_input("件名", e.get("subject", ""), key=f"m39_sj_{key}_{v}")
    body = st.text_area("本文（直してから送れます）", e.get("markup", ""), height=420, key=f"m39_bd_{key}_{v}",
                        help="✍️ " + m.MARK_HELP)
    with st.expander("👀 送られるメールの見え方（直した内容）", expanded=True):
        imgs = []
        _preview({"subject": subject, "html": m.to_html(body, {}, images=imgs), "images": imgs},
                 to, S.get("from_addr", ""), height=460)
    edited = body != e.get("markup", "") or to != (e.get("to") or "") or subject != e.get("subject", "")
    ok = st.checkbox("宛先・確認項目・中身を確かめました（送ると取り消せません）", key=f"m39_ok_{key}_{v}")
    a, b, c, d = st.columns(4)
    if a.button("📨 送信する（DC完了になります）", type="primary", disabled=not ok, use_container_width=True):
        try:
            with st.spinner("送っています…"):
                done = m.send_now(supabase, set_name, S, key, me, to.strip(), subject, body)
            n, why = _write_history([key], {key: done})
            if done.get("fusen_note"):
                why = (why + "／" if why else "") + "📌 " + str(done["fusen_note"])
            st.session_state["m39_sent_msg"] = (
                f"📨 {e.get('case', '')} を送りました（DC完了：{me}）" + ("・送信履歴に足しました" if n else "")
                + ("・📌 付箋を付けました" if done.get("fusen_at") else ""), why)
            st.session_state.pop(f"m39_cur_{set_name}", None)
        except m.DraftGone:
            st.session_state["m39_sent_msg"] = (
                "", "Gmailにこの下書きがありません（Gmailから直接送った・消した？）。"
                    "送っていれば、「✅ 送信済みにする」で完了にしてください。")
        except Exception as ex:
            st.session_state["m39_sent_msg"] = ("", f"送れませんでした：{str(ex)[:300]}")
        st.rerun()
    if b.button("💾 下書きだけ直す（送らない）", disabled=not edited, use_container_width=True):
        try:
            m.save_draft(supabase, set_name, S, key, to.strip(), subject, body)
            st.session_state[f"m39_rv_{key}"] = v + 1
            st.session_state["m39_sent_msg"] = ("💾 Gmailの下書きも直しました", "")
        except m.DraftGone:
            st.session_state["m39_sent_msg"] = ("", "Gmailにこの下書きがありません（Gmailから直接送った・消した？）")
        except Exception as ex:
            st.session_state["m39_sent_msg"] = ("", f"直せませんでした：{str(ex)[:300]}")
        st.rerun()
    _sent_button(key, e, me, v, c)
    if d.button("↩ 確認をやめる（ほかの人に渡す）", use_container_width=True):
        m.release(supabase, set_name, key, me)
        st.session_state.pop(f"m39_cur_{set_name}", None)
        st.session_state[f"m39_rv_{key}"] = v + 1
        st.rerun()
    if edited:
        st.caption("✏️ 直したところがあります（送ると直した内容で送ります）。")
    with st.expander("🔁 作り直す（この下書きを消して、作り直せるようにする）"):
        st.caption("文面（✏️ 文面と条件）を直したので作り直したい、というときに使います。"
                   "Gmailのこの下書きを消して、「✉️ 下書きを作る」でもう一度作れるようにします。"
                   "直すだけなら、上の「💾 下書きだけ直す」で足ります。")
        okr = st.checkbox("この下書きが消えることを確かめました", key=f"m39_rdok_{key}_{v}")
        if st.button("🔁 作り直せるようにする", disabled=not okr, key=f"m39_rdgo_{key}_{v}"):
            why = m.redo(supabase, set_name, key, me)
            st.session_state["m39_sent_msg"] = ("", why) if why else (
                f"🔁 {e.get('case', '')} の下書きを消しました。「✉️ 下書きを作る」で作り直してください", "")
            if not why:
                st.session_state.pop(f"m39_cur_{set_name}", None)
            st.rerun()


def _sent_button(key: str, e: dict, me: str, v: int = 0, box=None):
    """✅ 送信済みにする（Gmailから直接送ったとき）＝DC完了＋送信履歴に1行。

    ⭐ 担当者 2026-10-07：表のチェックではなく、確かめている画面でそのまま完了にしたい。
    ⚠️ 送信履歴に書いたあとは外せない（`mail39.undo_done`）ので、確認のチェックを入れないと押せない。
    """
    with (box or st).popover("✅ 送信済みにする", use_container_width=True):
        st.caption("Gmailから直接送ったときに押します（**アプリからは送りません**）。"
                   "DC完了（あなたの名前）になり、送信履歴にも1行足します。")
        ok = st.checkbox("このお客様へ、もう送ったことを確かめました", key=f"m39_dnok_{key}_{v}")
        if st.button("✅ 送信済みにする", type="primary", disabled=not ok, key=f"m39_dngo_{key}_{v}"):
            why = m.mark_done(supabase, set_name, key, me)
            if why:
                st.session_state["m39_sent_msg"] = ("", why)
            else:
                n, w = _write_history([key], m.load_log(supabase, set_name))
                fw = m.fusen_after_send(supabase, set_name, S, key)
                if fw:
                    w = (w + "／" if w else "") + "📌 " + fw
                st.session_state["m39_sent_msg"] = (
                    f"✅ {e.get('case', '')} を送信済み（DC完了：{me}）にしました"
                    + ("・送信履歴に足しました" if n else "")
                    + ("・📌 付箋を付けました" if e.get("follow") and not fw else ""), w)
                st.session_state.pop(f"m39_cur_{set_name}", None)
            st.rerun()


def _dc_done(me: str):
    """✅ きょう済んだ分（読むだけ）＋ 送信履歴に書けていない分の書き直し。

    ⭐ 担当者 2026-10-07：完了にするのは「📨 確認して送る」の中だけにした（表のチェックは無くした）。
       2か所で付けられると、どちらで付けたのか分からなくなるため。
    """
    logs = m.load_log(supabase, set_name)
    items = sorted([dict(v, _key=k) for k, v in logs.items() if v.get("done")],
                   key=lambda x: str(x.get("done_at") or x.get("made", "")), reverse=True)
    if items:
        st.dataframe(pd.DataFrame(
            [{"完了": str(x.get("done_at", ""))[5:16], "DC担当": x.get("dc", ""), "案件番号": x.get("case", ""),
              "お客様": x.get("name", ""), "宛先": x.get("email", ""), "文面": x.get("tpl", ""),
              "担当者": x.get("staff", ""), "送信履歴": "✅" if x.get("hist_at") else "⚠️ まだ"} for x in items]),
            hide_index=True, use_container_width=True)
    else:
        st.caption("きょう済んだ下書きはありません。")
    left = [k for k, v in logs.items() if v.get("done") and not v.get("hist_at")]
    if left:
        st.warning(f"完了にしたのに、送信履歴にまだ書けていない下書きが {len(left)} 件あります。")
        if st.button("🔁 送信履歴に書き直す"):
            ok, why = _write_history(left, logs)
            if why:
                st.error(why)
            else:
                st.success(f"送信履歴に {ok} 件足しました")
                st.rerun()
    # 📌 L-付箋が付いていない分（もう対応中の付箋がある・Salesforceにつながらなかった日）
    nof = [k for k, v in logs.items() if v.get("follow") and not v.get("fusen_at")]
    if nof:
        st.warning("📌 L-付箋（LPガス情報なし）が付いていない案件が " + f"{len(nof)} 件あります："
                   + "／".join(f"{logs[k].get('case', '')} {logs[k].get('name', '')}"
                               + (f"（{logs[k].get('fusen_note') or logs[k].get('fusen_error')}）"
                                  if (logs[k].get("fusen_note") or logs[k].get("fusen_error")) else "")
                               for k in nof))
        if st.button("📌 付箋を付け直す"):
            bad = []
            for k in nof:
                why = m.fusen_after_send(supabase, set_name, S, k)
                if why:
                    bad.append(f"{logs[k].get('case', '')}：{why}")
            if bad:
                st.error("📌 付けられませんでした：" + "／".join(bad))
            else:
                st.success(f"📌 {len(nof)} 件に付箋を付けました")
                st.rerun()


def _write_history(keys: list, items: dict) -> tuple:
    """完了した下書きを「「送信履歴」」シートに足し、足せたら記録に印を付ける → (件数, 失敗の理由)。

    ⭐ これまで人が「DC用」から写していた行と同じ並び（mail39.history_row）。
    ⚠️ 失敗しても完了の保存は取り消さない（「🔁 送信履歴に書き直す」でやり直せる）。
    """
    keys = [k for k in keys if k in items and not items[k].get("hist_at")]
    if not keys:
        return 0, ""
    try:
        n = m.write_history(_gc(), S, [items[k] for k in keys])
    except Exception as e:
        return 0, (f"送信履歴に書けませんでした：{str(e)[:200]}"
                   "（スプシをサービスアカウントに**編集者**で共有しているか確かめてください）")
    m.update_log(supabase, set_name, {k: {"hist_at": m.now_stamp()} for k in keys})
    return n, ""


# ==========================================
# ✏️ 文面と条件
# ==========================================
def _known_cols() -> list:
    cols = []
    box, _ = _box(set_name, S) if S.get("sheet_url") else (None, "")
    if box:
        cols += [h for h in box["head"] if h]
    cols += ["お客様名", "担当者", m.CB_KEY, m.TPL_COL]
    if S.get("region_master_url"):
        cols += m.LL_VALUES
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


def _head_text(text: str, n: int = 40) -> str:
    """段落の書き出し（早見表用）。マークは外す。"""
    t = m.to_plain(text, {})
    t = " ".join(x.strip() for x in t.splitlines() if x.strip())
    return t[:n] + ("…" if len(t) > n else "") if t else "（空＝何も書かない）"


def _who(routes: list, name: str) -> str:
    """その文面を使うお客様（振り分けの条件）を日本語で。"""
    ws = [m.describe(r.get("when")) for r in routes if r.get("template") == name]
    if not ws:
        return "⚠️ どのお客様にも使われていません"
    return "　または　".join(ws)


def view_edit():
    """✏️ 文面と条件。

    ⭐ 「どの文面を使うか」と「文面の中身」を1つの画面にまとめる（担当者 2026-10-04：別のタブだと、
       どういう文章がどういう理由で分かれているのか、ぱっと見で分からない）。
       上に全文面の早見表（使うお客様・段落の数）、文面を選ぶと「📌 使うお客様」と「段落の早見表」と中身。
    """
    tpls = S.get("templates") or {}
    if not tpls:
        st.warning("まだ文面がありません。⚙️ 設定の「📥 スプシの文面を取り込む」から始めてください。")
        return
    cols = _known_cols()
    rkey = f"m39_routes_{set_name}"
    if rkey not in st.session_state:
        st.session_state[rkey] = copy.deepcopy(S.get("routes") or [])
    routes = st.session_state[rkey]
    for r in routes:
        r.setdefault("uid", m.new_uid())
    rv = st.session_state.get(rkey + "_v", 0)
    bodies = [n for n in tpls if n not in (m.HEAD, m.FOOT)]

    # ---------- 早見表 ----------
    st.markdown("#### 🗺 早見表：どのお客様に、どの文面が行くか")
    st.caption("上から順に見て、**最初に当てはまった文面**を使います（だから細かい条件の文面ほど上に置きます）。"
               "（冒頭）と（末尾）は、どの文面にも付きます。")
    order, seen = [], set()
    for r in routes:
        n = r.get("template")
        if n in tpls and n not in seen:
            order.append(n)
            seen.add(n)
    order += [n for n in bodies if n not in seen]
    rows = [{"順": "—", "文面": m.HEAD, "使うお客様": "どの文面にも付く（あいさつ）",
             "件名": "", "段落": len(tpls.get(m.HEAD, {}).get("blocks") or [])}]
    for i, n in enumerate(order, 1):
        rows.append({"順": str(i) if n in seen else "—", "文面": n, "使うお客様": _who(routes, n),
                     "件名": tpls[n].get("subject", ""), "段落": len(tpls[n].get("blocks") or [])})
    if m.FOOT in tpls:
        rows.append({"順": "—", "文面": m.FOOT, "使うお客様": "どの文面にも付く（署名）", "件名": "",
                     "段落": len(tpls[m.FOOT].get("blocks") or [])})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                 height=min(36 * (len(rows) + 1) + 4, 640),
                 column_config={"順": st.column_config.TextColumn(width="small"),
                                "段落": st.column_config.NumberColumn(width="small")})

    # ---------- 文面を選ぶ ----------
    st.divider()
    pick = [m.HEAD] + order + ([m.FOOT] if m.FOOT in tpls else [])
    _go = st.session_state.pop(f"m39_tgo_{set_name}", None)
    if _go is not None:
        st.session_state[f"m39_tsel_{set_name}"] = _go
    if st.session_state.get(f"m39_tsel_{set_name}") not in pick:
        st.session_state.pop(f"m39_tsel_{set_name}", None)
    a, b = st.columns([2, 1])
    with a:
        name = st.selectbox("✏️ 直す文面", pick, key=f"m39_tsel_{set_name}",
                            format_func=lambda n: f"{n}　（{_who(routes, n)}）" if n not in (m.HEAD, m.FOOT) else n)
    with b:
        new = st.text_input("＋ 新しい文面の名前", key=f"m39_new_{set_name}", placeholder="例：SB光10G")
        if st.button("＋ 作る（いま選んでいる文面を写す）", disabled=not new.strip() or new.strip() in tpls):
            src = copy.deepcopy(tpls.get(name) or {"subject": "", "blocks": []})
            for blk in src.get("blocks") or []:
                blk["uid"] = m.new_uid()
            m.save_template(supabase, set_name, new.strip(), src)
            # 使うお客様は、写したもとの条件を写して、いちばん上に置く（細かい条件のはずなので）
            src_routes = [copy.deepcopy(r) for r in routes if r.get("template") == name] or [{"when": []}]
            for r in src_routes:
                r.update(uid=m.new_uid(), template=new.strip())
            m.save_cfg(supabase, {"routes": src_routes + [{k: v for k, v in r.items()} for r in routes]}, set_name)
            st.session_state.pop(rkey, None)
            st.session_state[rkey + "_v"] = rv + 1
            st.session_state[f"m39_tgo_{set_name}"] = new.strip()
            st.rerun()

    ekey = f"m39_edit_{set_name}_{name}"
    if ekey not in st.session_state:
        st.session_state[ekey] = copy.deepcopy(tpls[name])
    t = st.session_state[ekey]
    ev = st.session_state.get(ekey + "_v", 0)
    blocks = t.setdefault("blocks", [])

    # ---------- 📌 この文面を使うお客様 ----------
    if name not in (m.HEAD, m.FOOT):
        with st.container(border=True):
            st.markdown("##### 📌 この文面を使うお客様")
            mine = [i for i, r in enumerate(routes) if r.get("template") == name]
            if not mine:
                st.warning("どのお客様にも使われていません。「＋ 使うお客様の条件を足す」で決めてください。")
            for k, i in enumerate(mine):
                r = routes[i]
                if k:
                    st.caption("または")
                st.caption(f"上から {i + 1} 番目に見ます。")
                r["when"] = _cond_editor(r.get("when"), f"m39_rc_{r['uid']}_{rv}", cols)
                x1, x2, x3 = st.columns(3)
                if x1.button("↑ 先に見る", key=f"m39_ru_{r['uid']}", disabled=i == 0,
                             help="もっと上の文面より先に当てはめます（細かい条件の文面を上に）"):
                    routes[i - 1], routes[i] = routes[i], routes[i - 1]
                    st.rerun()
                if x2.button("↓ 後に見る", key=f"m39_rd_{r['uid']}", disabled=i == len(routes) - 1):
                    routes[i + 1], routes[i] = routes[i], routes[i + 1]
                    st.rerun()
                if x3.button("🗑 この条件を外す", key=f"m39_rx_{r['uid']}"):
                    routes.pop(i)
                    st.rerun()
            if st.button("＋ 使うお客様の条件を足す", key=f"m39_radd_{name}"):
                routes.insert(0, {"uid": m.new_uid(), "template": name, "when": []})
                st.rerun()
        t["subject"] = st.text_input("件名", t.get("subject", ""), key=f"m39_subj_{ekey}_{ev}")

    # ---------- 段落の早見表 ----------
    st.markdown("##### 📝 中身（段落）")
    st.caption("段落ごとに「出すとき」を決めます。空のときは、この文面のお客様全員に出します。")
    st.dataframe(pd.DataFrame([{
        "#": i + 1, "呼び名": blk.get("label", ""), "出すとき": m.describe(blk.get("when")),
        "書き出し": _head_text(blk.get("text", "")),
        "グループ": blk.get("group", ""), "つなぎ": "改行" if blk.get("join") == m.JOIN_LINE else "",
    } for i, blk in enumerate(blocks)]), hide_index=True, use_container_width=True,
        height=min(36 * (len(blocks) + 1) + 4, 640),
        column_config={"#": st.column_config.NumberColumn(width="small")})
    st.caption("✍️ 書き方：" + " ／ ".join(f"`{x}`" for x in m.MARK_HELP.split(" ／ ")))

    for i, blk in enumerate(blocks):
        blk.setdefault("uid", m.new_uid())
        u = f"{blk['uid']}_{ev}"
        with st.expander(f"{i + 1}. {blk.get('label') or '（呼び名なし）'}　—　出すとき：{m.describe(blk.get('when'))}"):
            blk["label"] = st.text_input("呼び名（メールには出ません）", blk.get("label", ""), key=f"m39_bl_{u}")
            blk["text"] = st.text_area("文", blk.get("text", ""), height=200, key=f"m39_bt_{u}")
            st.markdown("**出すとき**（空なら、いつも出す）")
            blk["when"] = _cond_editor(blk.get("when"), f"m39_bc_{u}", cols)
            g1, g2 = st.columns(2)
            joins = ["空行をあける", m.JOIN_LINE]
            j = g1.selectbox("前の段落とのあいだ", joins,
                             index=1 if blk.get("join") == m.JOIN_LINE else 0, key=f"m39_bj_{u}",
                             help="「改行」は前の段落のすぐ下に続けます（見出しの下に契約先の案内を続けるときなど）")
            blk["join"] = m.JOIN_LINE if j == m.JOIN_LINE else ""
            blk["group"] = g2.text_input(
                "グループ（どれか1つは必ず出す）", blk.get("group", ""), key=f"m39_bg_{u}",
                help="同じグループの段落がどれも条件に合わないお客様は、下書きを作らずに「⚠️ 作れない」にします"
                     "（例：電力・ガス。契約先の段落がまだ無い新しい商品に気づけます）").strip()
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
    if y2.button("💾 この文面を保存（使うお客様も）", type="primary"):
        m.save_template(supabase, set_name, name, t)
        m.save_cfg(supabase, {"routes": [{k: v for k, v in r.items()} for r in routes]}, set_name)
        for k in (ekey, rkey):
            st.session_state.pop(k, None)
        st.session_state[ekey + "_v"] = ev + 1
        st.session_state[rkey + "_v"] = rv + 1
        st.rerun()
    if y3.button("↩ 保存した状態に戻す"):
        for k in (ekey, rkey):
            st.session_state.pop(k, None)
        st.session_state[ekey + "_v"] = ev + 1
        st.session_state[rkey + "_v"] = rv + 1
        st.rerun()

    if name not in (m.HEAD, m.FOOT):
        with st.expander("🗑 この文面ごと消す"):
            ok = st.checkbox("消してよい（元に戻せません）", key=f"m39_tdel_ok_{ekey}")
            st.caption("使うお客様の条件も一緒に外します。")
            if st.button("🗑 消す", disabled=not ok, key=f"m39_tdel_{ekey}"):
                m.save_template(supabase, set_name, name, None)
                m.save_cfg(supabase, {"routes": [{k: v for k, v in r.items()} for r in routes
                                                 if r.get("template") != name]}, set_name)
                for k in (ekey, rkey):
                    st.session_state.pop(k, None)
                st.session_state[rkey + "_v"] = rv + 1
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
    trial = dict(S, templates=dict(tpls, **{name: t}), routes=routes)
    tname = None if name in (m.HEAD, m.FOOT) else name
    mail = m.compose(trial, row, tname, masters=box.get("masters"))
    if mail["error"]:
        st.error(mail["error"])
    if tname:
        st.caption(f"この文面で、このお客様に出る段落：{'・'.join(str(i) for i in mail['blocks']) or 'なし'}番目"
                   f"（ふだんはこのお客様に「{m.route(trial, m.enrich(row, S, box.get('masters'))) or '合う文面なし'}」を使います）")
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
        master = ""
        if set_name == "LL":
            master = st.text_input("地域マスタのURL（電気・ガス・水道の連絡先を引く別のスプシ）",
                                   S.get("region_master_url", ""),
                                   help="「電力」「ガスエリアデータ」「ガス連絡先」「水道局マスタ」のシートがあるスプシ。"
                                        "サービスアカウントに閲覧者で共有してください")
        hold = st.text_area("情報漏れとみなす言葉（本文にあれば自動では送らない・1行に1つ）",
                            "\n".join(S.get("hold_words") or m.DEFAULT_HOLD_WORDS), height=120,
                            help="「🚀 漏れの無い分は送信まで」で、この言葉が本文に残っているお客様は送らずに下書きにします")
        auto_send = st.checkbox("⏰ 時間指定の自動実行で、情報漏れの無い分は自動で送る（送ると取り消せません）",
                                value=bool(S.get("auto_send")),
                                help="OFF（既定）だと、時間指定では下書きを作るところで止めてSlackで知らせます。"
                                     "ONにすると、情報漏れの無いお客様はそのまま送信して DC完了（自動送信）にします")
        fusen_names = ["江藤", "黒部", "林", "鷲尾", "小湊", "小田", "太田", "宮崎", "三ヶ尻", "五通", "村田",
                       "髙崎", "渡部", "外的", "杉澤", "若松", "今村", "理田"]
        fusen_by = st.selectbox("付箋の添付者（LPガス情報なしの付箋を付けるときの名前）", fusen_names,
                                index=fusen_names.index(S.get("fusen_by") or m.DEFAULT_FUSEN_BY)
                                if (S.get("fusen_by") or m.DEFAULT_FUSEN_BY) in fusen_names else 0)
        names = st.text_area("DC担当者の名前（1行に1人・ネットとLLで共通）",
                             "\n".join(cfg.get("names") or []), height=150)
        if st.form_submit_button("💾 保存", type="primary"):
            split = lambda s: [x.strip() for x in str(s).replace("、", ",").split(",") if x.strip()]
            m.save_cfg(supabase, {
                "sheet_url": url.strip(), "box_tab": box_tab.strip() or "BOX",
                "refresh_robot": robot.strip(), "from_addr": from_addr.strip(),
                "name_tpl": name_tpl.strip(), "staff_cols": split(staff_cols),
                "staff_default": staff_default.strip(), "cb_cols": split(cb_cols),
                "legacy": dict(lg, tabs=split(legacy_tabs)),
                "hold_words": [x.strip() for x in hold.splitlines() if x.strip()],
                "auto_send": bool(auto_send), "fusen_by": fusen_by,
                **({"region_master_url": master.strip()} if set_name == "LL" else {})}, set_name)
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
            # ⚠️ Google Cloud のプロジェクトで Gmail API が有効になっていないとき（2026-10-04 に実際に出た）
            import re as _re
            _pj = _re.search(r"project[s]?[ =/](\d{6,})", info["error"])
            if "has not been used" in info["error"] or "is disabled" in info["error"]:
                _url = ("https://console.cloud.google.com/apis/library/gmail.googleapis.com"
                        + (f"?project={_pj.group(1)}" if _pj else ""))
                st.info(f"Google Cloud で **Gmail API** を有効にしてください（1回だけ）：[有効にするページを開く]({_url})"
                        " →「有効にする」→ 数分待ってから、この画面を開き直します。許可の出し直しは要りません。")
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
    if set_name == "LL":
        st.caption("スプシの「LL」シート（契約先ごとの案内）と「付帯」シート（マルシェ・FP・会員.COM）を、"
                   "電力・ガス・水道・付帯の段落にして取り込みます。地域の連絡先は地域マスタから引きます。"
                   "取り込んだあとはスプシの「LL」「付帯」シートは使いません（新しい電気・ガスの商品は、✏️ 文面と条件で段落を足します）。")
    else:
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
                imp = (m.import_ll if set_name == "LL" else m.import_net)(_gc(), S["sheet_url"])
        except Exception as e:
            st.error(f"読めませんでした：{str(e)[:300]}")
            return
        m.save_cfg(supabase, {"routes": imp["routes"], "templates": imp["templates"],
                              "imported_at": m.now_stamp()}, set_name)
        for k in [k for k in st.session_state
                  if str(k).startswith("m39_") and k not in ("m39_set", "m39_view")]:
            st.session_state.pop(k, None)
        st.success(f"取り込みました：文面 {len(imp['templates']) - 2} 個・振り分け {len(imp['routes'])} 本")
        if imp.get("found"):
            st.caption(f"契約先の案内：{len(imp['found'])} 件（{'、'.join(imp['found'])}）")
        if imp["missing"]:
            st.warning("見つからなかったもの：" + "、".join(imp["missing"]))


{"✉️ 下書きを作る": view_make, "📨 確認して送る": view_dc,
 "✏️ 文面と条件": view_edit, "⚙️ 設定": view_settings}[view]()
