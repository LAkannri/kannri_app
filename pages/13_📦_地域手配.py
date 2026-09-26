"""
📦 地域手配（水道・ガス・電気）

【困っていたこと】
  - SFコネクタで更新 → FAXのシートを目で見る → 印刷からFAX → 保存ボタン → 電話で手配 →
    DL用のCSVを落としてData Loader、を毎日手で回していた。
  - FAXの数式がずれて（3件目から氏名が1人ずつずれていた・2026-09-26）、**違うお客様の名前で
    FAXを送るところだった**。目で見ても気づけなかった。

【この画面の考え方】
  ⭐ **FAXに載ったかは、枠ごとに照らし合わせる**（`chiiki.check`）。元データの n件目が
     FAX の n枠目に、同じ電話番号で載っているか。⚠️ が1件でもあれば先へ進ませない。
  ⭐ **FAXを送るのは人**（印刷 → FAX機。送り先を間違えると取り消せないため）。
  ⭐ **手配日は、全部終わってから入れる**（担当者 2026-09-26）。電話・WEBの分は1件ずつ
     「対応した」にチェックし、全部そろうまで投入のボタンは押せない
     （手配していないのに手配日を入れないため）。
  ⭐ 投入は Data Loader の代わりに `sf_ui.push_sheet`（ほかの画面と同じ部品）。
     送るのは「案件 ID」と「手配日」の2つだけ。

⚠️ 判定をここに増やさない。中身は `chiiki.py`、①②は `auto_jobs.run_chiiki`（時間指定と同じ）。
"""
import json

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import auto_jobs
import characters as ch
import chiiki
import chiiki_fax
import common_robots
import gas_deploy
import sf_ui
import sms_runner
import theme

st.set_page_config(page_title="地域手配 - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📦", "地域手配",
                  "水道・ガス・電気の地域手配を、更新 → 抜けチェック → FAX → 手配日の投入まで通します。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()
DEFAULT_REFRESH_ROBOT = auto_jobs.DEFAULT_REFRESH_ROBOT
# スプシの保存ボタンのGAS（Excelにして Drive のフォルダへ置く）。スプシ側の関数名と同じにする。
SAVE_FUNCS = "saveGasToExcel,saveWaterToExcel"
DECIDE = {"": "（まだ決めていない）", "manual": "📞 手で手配する（電話などで）",
          "skip": "⏭ 今回は手配しない（手配日も入れない）"}


def _load():
    try:
        return auto_jobs.load_row(supabase, chiiki.SETTINGS_ID)
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
        return {}


def _save(part: dict):
    # ⚠️ 書く直前に読み直して、触ったところだけ変える（見回り役や別のPCが書いた分を消さない）
    latest = _load()
    latest.update(part)
    supabase.table("merchants").upsert({
        "id": chiiki.SETTINGS_ID, "name": "（地域手配の設定）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return latest


def _save_state(st_):
    _save({"state": st_})


@st.cache_resource(show_spinner=False)
def _gc_for(sa_json: str):
    return auto_jobs.gspread_client(sa_json)


gc = _gc_for(st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
cfg = _load()
url = str(cfg.get("sheet_url", "") or "").strip()

view = st.session_state.setdefault("ck_view", "main")
_h1, _h2 = st.columns([5, 1])
with _h2:
    if view == "main":
        if st.button("⚙️ 設定", use_container_width=True):
            st.session_state.ck_view = "settings"
            st.rerun()
    elif st.button("← 戻る", use_container_width=True):
        st.session_state.ck_view = "main"
        st.rerun()

# ==========================================
# ⚙️ 設定
# ==========================================
if view == "settings":
    with st.container(border=True):
        theme.section_title("📄", "地域手配のスプレッドシート")
        new_url = st.text_input("スプレッドシートのURL", value=url, key="ck_url",
                                help="「地域水道手配」「地域ガス手配」「地域電気手配」と、各FAX・DLのシートがあるスプシ")
        st.caption("更新するシート：" + "・".join(chiiki.REFRESH_TABS)
                   + "　／　投入するシート：" + "・".join(s["dl"] for s in chiiki.SRC.values()))
        _robots = sorted(set(common_robots.list_robots(supabase) + [DEFAULT_REFRESH_ROBOT]))
        _rb = cfg.get("refresh_robot") or DEFAULT_REFRESH_ROBOT
        robot = st.selectbox("更新に使うロボット", _robots,
                             index=_robots.index(_rb) if _rb in _robots else 0, key="ck_robot")
    with st.container(border=True):
        theme.section_title("💾", "送った記録をDriveに保存する（任意）")
        st.caption("スプシの保存ボタン（ガス・水道のFAXをExcelにしてフォルダへ置く）を、アプリから押せるようにします。"
                   "入れていなくても、スプシの保存ボタンを押せば同じです。")
        _auto = gas_deploy.render(
            "ck_gas",
            {"gas_script_url": cfg.get("gas_script_url", ""), "gas_url": cfg.get("gas_url", ""),
             "gas_token": cfg.get("gas_token", ""), "gas_deployment_id": cfg.get("gas_deployment_id", "")})
    with st.container(border=True):
        theme.section_title("📠", "FAXの送り先")
        _ps = chiiki_fax.all_printers()
        _cur = str(cfg.get("fax_printer", "") or "")
        _opts = [""] + [p for p in _ps if p] + ([_cur] if _cur and _cur not in _ps else [])
        fax_printer = st.selectbox(
            "使うFAXプリンタ", _opts, index=_opts.index(_cur) if _cur in _opts else 0,
            format_func=lambda x: x or f"（自動で探す：{('・'.join(chiiki_fax.fax_printers()) or 'このPCでは見つかりません')}）",
            key="ck_printer", help="ふつうは「自動で探す」のまま。京セラのネットワークFAX（NW-FAX）を探します。"
                                    "機械を入れ替えて名前が変わったときや、2台以上あるときに選びます。")
        st.caption("FAXごとの宛先（京セラのアドレス帳の名前）と、送ってよいFAX番号。"
                   "**アドレス帳で選んだ番号・ここの番号・FAXの用紙に書いてある番号**がそろわないと送りません。")
        _bk = chiiki_fax.book(cfg)
        _bdf = pd.DataFrame([{"FAX": k, "宛先名": v.get("宛先名", ""), "FAX番号": v.get("FAX番号", "")}
                             for k, v in _bk.items()])
        _bed = st.data_editor(_bdf, hide_index=True, use_container_width=True, key="ck_book",
                              disabled=["FAX"])
    with st.container(border=True):
        theme.section_title("⏰", "時間指定の自動実行で、どこまで自動で行くか")
        st.caption("「⏰ 時間指定の自動実行」で📦 地域手配を動かしたときの動き。**どちらも既定はOFF**です。"
                   "自動実行用のPCで「🧪 お試し」が通るのを確かめてからONにしてください。")
        auto_fax = st.checkbox("抜けが0件なら、FAXまで自動で送る", value=bool(cfg.get("auto_fax")), key="ck_autofax",
                               help="OFFなら、FAXの手前で止まってSlackで知らせます（画面で完成形を見て送ります）。")
        auto_push = st.checkbox("済んだ案件の手配日を、自動で入れる",
                                value=bool(cfg.get("auto_push")), key="ck_autopush_on",
                                help="ONなら、FAXを送ったとき・「対応した」を押したとき・時間指定で更新する前に、済んだ案件の手配日を入れます。")
    if st.button("💾 保存する", type="primary", key="ck_save"):
        _save({"sheet_url": new_url.strip(), "refresh_robot": robot, "fax_printer": fax_printer,
               "auto_fax": bool(auto_fax), "auto_push": bool(auto_push),
               "fax_book": {r["FAX"]: {"宛先名": str(r["宛先名"] or "").strip(),
                                       "FAX番号": chiiki_fax.digits(r["FAX番号"])}
                            for _, r in _bed.iterrows()},
               **{k: _auto.get(k, "") for k in ("gas_script_url", "gas_url", "gas_token",
                                                  "gas_deployment_id")}})
        st.success("保存しました。")
    st.stop()

# ==========================================
# ▶ 実行
# ==========================================
if not url:
    st.warning("右上の ⚙️ 設定 で、スプレッドシートのURLを入れてください。")
    st.stop()
if not gc:
    st.error("スプシを読むための接続キー（GOOGLE_SERVICE_ACCOUNT_JSON）がありません。")
    st.stop()

state = chiiki.day_state(cfg)
last = cfg.get("last_check") or {}
rows = last.get("rows") or []
# ⚠️ きのうのチェック結果で今日の作業をさせない
if str(last.get("checked_at", ""))[:10].replace("/", "-") != chiiki.today():
    rows = []

# ── ① 更新と ② チェック ──
with st.container(border=True):
    theme.section_title("1️⃣", "更新して、抜けをチェックする")
    st.caption("SFコネクタで3枚（" + "・".join(chiiki.REFRESH_TABS) + "）を更新し、"
               "FAX・WEBに**正しい枠で**載っているかを1件ずつ照らし合わせます。")
    # ⚠️ 済んだのに手配日を入れていない案件があるうちは更新させない（担当者 2026-09-26）：
    #    入れないまま更新すると、その案件がレポートから外れて入れ忘れる／FAXのシートに前のお客様が残る。
    pend = chiiki.to_push(state)
    if pend:
        st.warning(f"⚠️ 済んだのに手配日をまだ入れていない案件が **{len(pend)}件** あります。"
                   "**先に手配日を入れてから**更新してください（入れないまま更新すると、入れ忘れになります）。")
        c_ok = st.checkbox(f"この{len(pend)}件は手配が済んでいます（手配日は取り消せません）", key="ck_prepush_ok")
        if st.button(f"🚀 済んだ{len(pend)}件の手配日を入れる", disabled=not c_ok, key="ck_prepush"):
            with st.spinner("Salesforceへ入れています..."):
                _pr = chiiki.push_all(gc, url, last.get("rows") or [], state)
            _save_state(state)
            for r in _pr:
                st.write(f"{sf_ui.push_mark(r)} **{r['シート']}**：{r.get('結果', '')}")
            if all(sf_ui.push_ok(r) for r in _pr):
                st.rerun()
    b1, b2, b3 = st.columns([1.3, 1.3, 2])
    go_refresh = b1.button("🔄 更新してチェックする", type="primary", use_container_width=True,
                           disabled=bool(pend))
    go_check = b2.button("🔎 チェックだけやり直す", use_container_width=True,
                         help="シートを直したあとなど。更新はしません")
    b3.caption(f"最後の更新：{cfg.get('last_refresh') or '—'}　／　最後のチェック：{last.get('checked_at') or '—'}")
    if go_refresh or go_check:
        with st.spinner("🤖 シートを更新しています（数分かかります）..." if go_refresh
                        else "チェックしています..."):
            _steps, _res = auto_jobs.chiiki_check(supabase, gc, cfg, refresh=bool(go_refresh))
        bad = [s for s in _steps.rows if s["結果"] == "🛑"]
        if bad:
            st.error("🛑 " + "／".join(f"{s['工程']}：{s['中身']}" for s in bad))
        else:
            st.rerun()

if not rows:
    st.info("きょうはまだチェックしていません。上の「🔄 更新してチェックする」を押してください。")
    st.stop()

# ── ② 結果 ──
with st.container(border=True):
    theme.section_title("2️⃣", "振り分けとチェックの結果")
    df = pd.DataFrame([{"済み": "✅" if chiiki.handled(r, state) else "", "状態": r["状態"], "商材": r["商材"], "案件ID": r["案件ID"], "名前": r["名前"],
                        "種別": r["種別"], "行き先": r["行き先"], "理由": r["理由"], "注意": r["注意"]}
                       for r in rows])
    st.dataframe(df, hide_index=True, use_container_width=True)
    opens = chiiki.open_items(rows, state)
    warns = [r for r in rows if r["状態"] == "⚠️"]
    if warns:
        st.markdown(f"**⚠️ 要対応 {len(warns)}件**：直したら「🔎 チェックだけやり直す」。"
                    "直せないものは、ここでどう扱うか決めてください。")
        changed = False
        for r in warns:
            cur = state["decide"].get(r["key"], "")
            v = st.selectbox(f"{r['商材']}｜{r['案件ID']}｜{r['名前']}　— {r['理由']}",
                             list(DECIDE), index=list(DECIDE).index(cur) if cur in DECIDE else 0,
                             format_func=lambda k: DECIDE[k], key=f"ck_dec_{r['key']}_{cur}")
            # ⚠️ キーに今の値を入れる：開きっぱなしの画面が、別のPCで変えた扱いを古い選択で上書きしないように
            if v != cur:
                if v:
                    state["decide"][r["key"]] = v
                else:
                    state["decide"].pop(r["key"], None)
                changed = True
        if changed:
            _save_state(state)
            st.rerun()
    else:
        st.success("✅ 抜けはありません。")
    notes = [r for r in rows if r["注意"]]
    if notes:
        st.warning("💡 注意（止めはしません）：" + "／".join(f"{r['案件ID']} {r['名前']}：{r['注意']}"
                                                          for r in notes))

if opens:
    st.error(f"⚠️ 要対応が {len(opens)}件 残っているので、ここから先へは進めません。")
    st.stop()

# ── ③ FAX ──
faxes = chiiki.fax_items(rows, state)
sent_today = set(state.get("fax_done") or [])
with st.container(border=True):
    theme.section_title("3️⃣", "FAXを送る")
    st.caption(("⭐ 時間指定の自動実行で、抜けが0件なら**そのまま送ります**（⚙️ 設定の「FAXまで自動」がON）。"
                if cfg.get("auto_fax") else
                "⚙️ 設定の「FAXまで自動」がOFFなので、ここで完成形を見てから送ります。"))
    stored = chiiki_fax.stored_today(supabase)
    if stored:
        with st.expander(f"📂 きょう送ったFAX（{len(stored)}通・どのPCからも見られます。あしたには入れ替わります）"):
            import base64
            for f in stored:
                data = base64.b64decode(f["pdf_b64"])
                st.markdown(f"**{f['シート']}** → {f['宛先']}　{f['時刻']} 送信")
                st.download_button("⬇️ PDF", data, file_name=f"{f['シート']}.pdf", mime="application/pdf",
                                   key=f"ck_dl_{f['シート']}_{f['時刻']}")
                try:
                    import pymupdf
                    for pg in pymupdf.open(stream=data, filetype="pdf"):
                        st.image(pg.get_pixmap(dpi=90).tobytes("png"), use_container_width=True)
                except Exception:
                    pass
    # ⚠️ 前に送った案件がまだ載っているシート：送り直すと、前のお客様に二重に届く
    late = chiiki.late_fax_items(rows, state)
    if late:
        st.warning("⚠️ 同じFAXのシートに、前に送った案件がまだ載っています。このシートを送ると前のお客様にも"
                   "**二重に届く**ので、下の案件は手で送るか、今回は手配しないかを決めてください。")
        for r in late:
            v = st.selectbox(f"{r['商材']}｜{r['案件ID']}｜{r['名前']}（{r['行き先']}）", ["", "manual", "skip"],
                             format_func=lambda k: DECIDE[k], key=f"ck_late_{r['key']}")
            if v:
                state["decide"][r["key"]] = v
                _save_state(state)
                st.rerun()
    todo = faxes
    if not todo:
        st.caption("きょうFAXで送るものは、もうありません。" if sent_today else "きょうFAXで送るものはありません。")
    else:
        cnt = {}
        for r in todo:
            where = r["行き先"].split(" ")[1] if " " in r["行き先"] else r["行き先"]
            cnt[where] = cnt.get(where, 0) + 1
        st.markdown("**送るFAX**：" + "、".join(f"{k} {v}件" for k, v in cnt.items()))
        # ⚠️ チェックをやり直したら、前に作ったPDFは使わない（中身が変わっているかもしれない）
        _pd = st.session_state.get("ck_pdfs") or {}
        if _pd.get("checked_at") != last.get("checked_at"):
            _pd = {}
        if st.button("📄 FAXを作る（完成形を見る）", key="ck_mkpdf"):
            with st.spinner("FAXのシートをPDFにしています..."):
                _pd = {"checked_at": last.get("checked_at"),
                       "list": chiiki_fax.make_pdfs(gc, st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
                                                    url, todo)}
            st.session_state["ck_pdfs"] = _pd
        pdfs = _pd.get("list") or []
        if pdfs:
            bk = chiiki_fax.book(cfg)
            for p in pdfs:
                b = bk.get(p["シート"]) or {}
                with st.expander(f"{p['シート']}　{p['件数']}件・{p['ページ'] or '—'}ページ　→　"
                                 f"{b.get('宛先名', '？')}（{b.get('FAX番号', '？')}）", expanded=True):
                    if p.get("エラー"):
                        st.error("🛑 " + p["エラー"])
                    for img in p.get("画像") or []:
                        st.image(img, use_container_width=True)
            jobs, bad = chiiki_fax.plan_jobs(pdfs, cfg)
            printer, pwhy = chiiki_fax.pick_printer(cfg)
            for b_ in bad:
                st.error("🛑 " + b_)
            if pwhy:
                st.warning("⚠️ " + pwhy)
            elif jobs:
                st.caption(f"使うFAXプリンタ：{printer}")
            seen = st.checkbox("✅ 完成形を見た（この中身・この宛先で送ってよい）", key="ck_seen")
            can = bool(jobs) and not bad and not pwhy
            c1, c2 = st.columns(2)
            go_try = c1.button("🧪 お試し（宛先を選んで確かめるだけ・送らない）", disabled=not can,
                               use_container_width=True)
            go_send = c2.button(f"📠 FAXを送る（{len(jobs)}通）", type="primary",
                                disabled=not (can and seen), use_container_width=True)
            if go_try or go_send:
                with st.spinner("京セラのFAXの画面を操作しています（画面に触らないでください）..."):
                    r = chiiki_fax.send_all(supabase, gc, st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
                                            cfg, rows, state, submit=bool(go_send), pdfs=pdfs)
                for x in r["結果"]:
                    st.write(f"{x['結果']} **{x['シート']}**：{x['中身']}")
                for w in r["止めた理由"]:
                    st.error("🛑 " + w)
                if go_send and r["送った"]:
                    _save_state(state)
                    st.success("✅ 送りました。")
        st.markdown("---")
        hand = st.checkbox("✋ 上のFAXは、手で（印刷からFAX機で）全部送った", key="ck_handsent",
                           help="京セラの画面で送ったときに入れます。入れると、上のFAXの案件を「送った」にします。")
        if hand and st.button("この内容で「送った」にする", key="ck_handsave"):
            state["fax_keys"] = sorted(set(state.get("fax_keys") or []) | {r["key"] for r in todo})
            state["fax_done"] = sorted(sent_today | set(cnt))
            _save_state(state)
            st.rerun()
    gas_url = str(cfg.get("gas_url", "") or "").strip()
    if gas_url and sent_today:
        if st.button("💾 もう一度、Driveのフォルダに保存する（ガス・水道のFAX）", key="ck_savefax",
                     help="FAXを送ると自動で保存します。うまくいかなかったときに押します。"):
            with st.spinner("保存しています..."):
                ok, data = sms_runner.run_gas_action(gas_url, str(cfg.get("gas_token", "") or ""),
                                                     "build", build=SAVE_FUNCS, timeout=300)
            st.success("✅ Driveに保存しました。") if ok else st.error(f"❌ {data}")


def _auto_push_if_ready():
    """済んだ瞬間に、その分の手配日を入れる（「投入まで自動」がONのとき）。"""
    if not (cfg.get("auto_push") and chiiki.to_push(state)):
        return None
    res_ = chiiki.push_all(gc, url, rows, state)
    _save_state(state)
    return res_


# ── ④ 電話・WEB ──
manual = chiiki.manual_items(rows, state)
with st.container(border=True):
    theme.section_title("4️⃣", "電話・WEBで手配する（人）")
    if not manual:
        st.caption("きょう手で手配するものはありません。")
    else:
        st.caption("手配し終わったら「対応した」にチェックしてください。**どのPCで押しても同じ状態になります。**"
                   "手配日は、対応した案件だけに入れます（🚨＝利用開始が今日・明日）。")
        mdf = pd.DataFrame([{"対応した": r["key"] in state["done"],
                             "開始": ("🚨 " if chiiki.urgent(r) else "") + (r.get("開始") or "")[5:].replace("-", "/"),
                             "商材": r["商材"], "案件ID": r["案件ID"],
                             "名前": r["名前"], "行き先": r["行き先"] or r["種別"], "注意": r["注意"],
                             "備考に追記": (state["memo"].get(r["key"], "")
                                          if r["商材"] in chiiki.REMARK_FIELD else ""),
                             "備考": "✅ 書きました" if r["key"] in state["remarked"] else "",
                             "_key": r["key"]} for r in manual])
        # ⚠️ キーに今の「対応した」を入れる（開きっぱなしの画面が、別のPCで押した分を上書きしないように）
        ed = st.data_editor(mdf, hide_index=True, use_container_width=True,
                            key="ck_manual_" + str(abs(hash((tuple(sorted(state["done"])),
                                                             tuple(sorted(state["memo"].items())),
                                                             tuple(state["remarked"]))))),
                            column_config={"_key": None, "備考に追記": st.column_config.TextColumn(
                                "備考に追記", help="電気は電力備考、ガスはガス備考、水道は顧客対応備考のうしろに、きょうの日付つきで1行足します"
                                                  "（「対応した」にしたとき。上書きはしません）")},
                            disabled=["開始", "商材", "案件ID", "名前", "行き先", "注意", "備考"])
        done = sorted(set(k for k, v in zip(ed["_key"], ed["対応した"]) if v))
        others = [k for k in state["done"] if k not in set(mdf["_key"])]
        memos = {k: str(v or "").strip() for k, v in zip(ed["_key"], ed["備考に追記"])
                 if k.split(":", 1)[0] in chiiki.REMARK_FIELD and k not in state["remarked"]}
        memo_changed = any(state["memo"].get(k, "") != v for k, v in memos.items())
        if sorted(set(done + others)) != sorted(set(state["done"])) or memo_changed:
            # ⚠️ 書く直前に読み直す（別のPCで押した「対応した」を消さない）
            state = chiiki.day_state(_load())
            state["done"] = sorted(set(done + others))
            state["memo"].update({k: v for k, v in memos.items() if v})
            # 📝 「対応した」＋備考の文がある案件だけ、Salesforceの備考に書き足す（1回だけ）
            for k in state["done"]:
                kind_, cid_ = k.split(":", 1)
                if kind_ in chiiki.REMARK_FIELD and state["memo"].get(k) and k not in state["remarked"]:
                    why_ = sf_ui.append_remark("Opportunity", cid_, chiiki.REMARK_FIELD[kind_],
                                               chiiki.remark_text(state["memo"][k]))
                    if not why_ or why_.startswith("＿"):
                        state["remarked"].append(k)
                    else:
                        st.session_state.setdefault("ck_remark_ng", []).append(f"{cid_}：{why_}")
            _save_state(state)
            _r = _auto_push_if_ready()
            if _r is not None:
                st.session_state["ck_autopush"] = _r
            st.rerun()

for _m in st.session_state.pop("ck_remark_ng", []):
    st.error(f"📝 備考に書き足せませんでした（「対応した」はそのままです。直したら文を入れ直してください）：{_m}")

# ── ⑤ 投入 ──
with st.container(border=True):
    theme.section_title("5️⃣", "手配日をSalesforceへ入れる")
    ok_push, why = chiiki.ready_to_push(rows, state)
    skips = [r for r in rows if state["decide"].get(r["key"]) == "skip"]
    st.caption("入れるもの：" + "・".join(f"{s['dl']}（{s['dl_col']}）" for s in chiiki.SRC.values())
               + "。送るのは「案件 ID」と「手配日」だけで、**手配が済んだ案件だけ**を入れます。"
               + (f"　⏭ 手配しない {len(skips)}件は入れません。" if skips else ""))
    if cfg.get("auto_push"):
        st.caption("⭐ 「投入まで自動」がONなので、済んだ案件はその場で入れます。")
    _ap = st.session_state.pop("ck_autopush", None)
    if _ap:
        st.success("⭐ 済んだ案件の手配日を、自動で入れました。")
        for r in _ap:
            st.write(f"{sf_ui.push_mark(r)} **{r['シート']}**：{r.get('結果', '')}")
    _np = len(state.get("pushed_keys") or [])
    if _np:
        st.info(f"✅ きょう手配日を入れた案件：{_np}件")
    left = chiiki.left_items(rows, state)
    hot = [r for r in left if chiiki.urgent(r)]
    if hot:
        st.error(f"🚨 利用開始が今日・明日なのに、まだ手配が済んでいない案件が {len(hot)}件 あります：\n\n"
                 + "\n".join(f"- {chiiki.left_line(r)}" for r in hot))
    if left:
        st.caption(f"まだ済んでいない案件（{len(left)}件・入れません）：" + "、".join(chiiki.left_line(r) for r in left[:20]))
    if not ok_push:
        st.warning(f"⏸ {why}")
    else:
        st.markdown(f"**入れる案件：{len(chiiki.to_push(state))}件**（FAXを送った・「対応した」にした案件）")
    confirm = st.checkbox("この案件の手配が済んでいることを確かめました（手配日は取り消せません）",
                          key="ck_confirm", disabled=not ok_push)
    if st.button("🚀 手配日を入れる", type="primary", disabled=not (ok_push and confirm)):
        with st.spinner("Salesforceへ入れています..."):
            res = chiiki.push_all(gc, url, rows, state)
        for r in res:
            st.write(f"{sf_ui.push_mark(r)} **{r['シート']}**：{r.get('結果', '')}")
            for e in (r.get("errors") or [])[:10]:
                st.caption(f"　└ {json.dumps(e, ensure_ascii=False)[:200]}")
        _save_state(state)
        if all(sf_ui.push_ok(r) for r in res):
            st.success("✅ 手配日を入れました。")
