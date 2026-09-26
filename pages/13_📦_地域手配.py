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
    if st.button("💾 保存する", type="primary", key="ck_save"):
        _save({"sheet_url": new_url.strip(), "refresh_robot": robot,
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
    b1, b2, b3 = st.columns([1.3, 1.3, 2])
    go_refresh = b1.button("🔄 更新してチェックする", type="primary", use_container_width=True)
    go_check = b2.button("🔎 チェックだけやり直す", use_container_width=True,
                         help="シートを直したあとなど。更新はしません")
    b3.caption(f"最後の更新：{cfg.get('last_refresh') or '—'}　／　最後のチェック：{last.get('checked_at') or '—'}")
    if go_refresh or go_check:
        with st.spinner("🤖 シートを更新しています（数分かかります）..." if go_refresh
                        else "チェックしています..."):
            r = auto_jobs.run_chiiki(supabase, gc, cfg, refresh=bool(go_refresh))
        bad = [s for s in r["工程"] if s["結果"] == "🛑"]
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
    df = pd.DataFrame([{"状態": r["状態"], "商材": r["商材"], "案件ID": r["案件ID"], "名前": r["名前"],
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
                             format_func=lambda k: DECIDE[k], key=f"ck_dec_{r['key']}")
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
with st.container(border=True):
    theme.section_title("3️⃣", "FAXを送る（人）")
    if not faxes:
        st.caption("きょうFAXで送るものはありません。")
    else:
        cnt = {}
        for r in faxes:
            where = r["行き先"].split(" ")[1] if " " in r["行き先"] else r["行き先"]
            cnt[where] = cnt.get(where, 0) + 1
        st.markdown("**送るFAX**：" + "、".join(f"{k} {v}件" for k, v in cnt.items()))
        st.caption("スプシの各FAXシートを、印刷 → FAX機を選ぶ → 送り先を選ぶ、で送ってください。")
        gas_url = str(cfg.get("gas_url", "") or "").strip()
        if gas_url:
            if st.button("💾 送った記録をDriveに保存する（ガス・水道のFAX）", key="ck_savefax"):
                with st.spinner("保存しています..."):
                    ok, data = sms_runner.run_gas_action(gas_url, str(cfg.get("gas_token", "") or ""),
                                                         "build", build=SAVE_FUNCS, timeout=300)
                if ok:
                    st.success("✅ Driveに保存しました。")
                else:
                    st.error(f"❌ {data}")
        else:
            st.caption("送った記録は、スプシの保存ボタン【フォルダへ保存】で残してください"
                       "（⚙️ 設定でGASを入れると、ここから押せます）。")
        sent = st.checkbox("✅ FAXを全部送った", value=bool(state.get("fax_sent")), key="ck_faxsent")
        if sent != bool(state.get("fax_sent")):
            state["fax_sent"] = sent
            _save_state(state)
            st.rerun()

# ── ④ 電話・WEB ──
manual = chiiki.manual_items(rows, state)
with st.container(border=True):
    theme.section_title("4️⃣", "電話・WEBで手配する（人）")
    if not manual:
        st.caption("きょう手で手配するものはありません。")
    else:
        st.caption("手配し終わったら「対応した」にチェックしてください。**全部そろうまで、手配日は入れません。**")
        mdf = pd.DataFrame([{"対応した": r["key"] in state["done"], "商材": r["商材"], "案件ID": r["案件ID"],
                             "名前": r["名前"], "行き先": r["行き先"] or r["種別"], "注意": r["注意"],
                             "_key": r["key"]} for r in manual])
        ed = st.data_editor(mdf, hide_index=True, use_container_width=True, key="ck_manual",
                            column_config={"_key": None},
                            disabled=["商材", "案件ID", "名前", "行き先", "注意"])
        done = sorted(set(k for k, v in zip(ed["_key"], ed["対応した"]) if v))
        others = [k for k in state["done"] if k not in set(mdf["_key"])]
        if sorted(set(done + others)) != sorted(set(state["done"])):
            state["done"] = sorted(set(done + others))
            _save_state(state)
            st.rerun()

# ── ⑤ 投入 ──
with st.container(border=True):
    theme.section_title("5️⃣", "手配日をSalesforceへ入れる")
    ok_push, why = chiiki.ready_to_push(rows, state)
    skips = [r for r in rows if state["decide"].get(r["key"]) == "skip"]
    st.caption("入れるもの：" + "・".join(f"{s['dl']}（{s['dl_col']}）" for s in chiiki.SRC.values())
               + "。送るのは「案件 ID」と「手配日」だけです。"
               + (f"　⏭ 手配しない {len(skips)}件は入れません。" if skips else ""))
    if state.get("pushed"):
        st.info("きょうはもう入れました。もう一度入れても、同じ手配日が入るだけです。")
    if not ok_push:
        st.warning(f"⏸ まだ入れられません：{why}")
    confirm = st.checkbox("手配が全部終わったことを確かめました（手配日は取り消せません）",
                          key="ck_confirm", disabled=not ok_push)
    if st.button("🚀 手配日を入れる", type="primary", disabled=not (ok_push and confirm)):
        with st.spinner("Salesforceへ入れています..."):
            res = chiiki.push_all(gc, url, rows, state)
        for r in res:
            st.write(f"{sf_ui.push_mark(r)} **{r['シート']}**：{r.get('結果', '')}")
            for e in (r.get("errors") or [])[:10]:
                st.caption(f"　└ {json.dumps(e, ensure_ascii=False)[:200]}")
        if all(sf_ui.push_ok(r) for r in res):
            state["pushed"] = True
            _save_state(state)
            st.success("✅ 手配日を入れました。")
