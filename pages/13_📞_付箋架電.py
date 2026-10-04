"""
📞 付箋架電（TS用／総務用）

  ① SFコネクタで【SF】のシートを更新 → ② 付箋のシートを読んで一覧に出す
  → ③ 1件ずつ「対応中／不出／完了」を選ぶ（だれが・いつ、も残る）

⭐ チェックは案件IDに結びつけて Supabase に持つ（スプシのA〜D列は使わない＝更新してもずれない）。
⭐ ほかのPCの操作は数秒で出る（表だけ数秒おきに書き直す。そのとき読むのは Supabase の小さな行だけ）。
⭐ スプシの中身は Supabase に入れない（画面に出すためにその場で読むだけ）。
中身は fusen.py。
"""
import hashlib
import json

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import auto_jobs
import characters as ch
import fusen
import sms_runner
import theme

st.set_page_config(page_title="付箋架電 - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📞", "付箋架電",
                  "付箋の案件を1件ずつ見て、対応中・不出・完了を付けます。ほかのPCの操作も数秒で出ます。",
                  color=c["color"])

TICK_SEC = 3            # 表を書き直す間隔（読むのは Supabase だけ）
SHEET_TTL_SEC = 600     # スプシの中身を覚えておく時間（🔄 か更新で読み直す）


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


@st.cache_data(ttl=SHEET_TTL_SEC, show_spinner="スプレッドシートを読んでいます…")
def _read(url: str, tabs: tuple, stamp: str):
    """⚠️ stamp（最後に更新した時刻など）が変わったときだけ読み直す。
    ほかのPCで更新しても、その時刻が Supabase に残るので、こちらも読み直す。"""
    return fusen.read_tabs(_gc(), url, list(tabs))


# ==========================================
# 🙋 だれが操作しているか
# ==========================================
cfg = fusen.load_cfg(supabase)
names = list(cfg.get("names") or [])
qp_me = st.query_params.get("me", "")
if "fz_me" not in st.session_state:
    st.session_state.fz_me = qp_me

c1, c2 = st.columns([2, 3])
with c1:
    opts = ["（選んでください）"] + names + ["＋ 名前を足す"]
    idx = opts.index(st.session_state.fz_me) if st.session_state.fz_me in opts else 0
    pick = st.selectbox("🙋 あなたの名前", opts, index=idx, key="fz_pick")
with c2:
    if pick == "＋ 名前を足す":
        new = st.text_input("名前（チームで同じ書き方にしてください）", key="fz_new").strip()
        if new and st.button("この名前で始める"):
            if new not in names:
                fusen.save_cfg(supabase, {"names": names + [new]})
            st.session_state.fz_me = new
            st.query_params["me"] = new
            st.rerun()
if pick not in ("（選んでください）", "＋ 名前を足す"):
    if pick != st.session_state.fz_me:
        st.session_state.fz_me = pick
        st.query_params["me"] = pick
me = st.session_state.fz_me if st.session_state.fz_me in names else ""

set_names = list(cfg["sets"].keys())
set_name = st.radio("どちらの付箋？", set_names, horizontal=True, key="fz_set")
one = cfg["sets"][set_name]


# ==========================================
# ⚙️ 設定
# ==========================================
def _render_settings():
    with st.expander("⚙️ 設定（スプレッドシートと、見るシート）", expanded=not one.get("sheet_url")):
        url = st.text_input("スプレッドシートのURL", value=one.get("sheet_url", ""),
                            key=f"fz_url_{set_name}")
        gids = auto_jobs.tab_gids(_gc(), url.strip()) if url.strip() else {}
        all_tabs = list(gids.keys())
        if url.strip() and not all_tabs:
            st.warning("シートの一覧を読めませんでした。ロボット用のアカウント"
                       "（`enkan-robot-reader@enkan-503001.iam.gserviceaccount.com`）に"
                       "このスプシを「閲覧者」で共有してください。")
        cur_ref = [t for t in one.get("refresh_tabs") or [] if t in all_tabs or not all_tabs]
        ref = st.multiselect("① SFコネクタで更新するシート（上から順に更新）",
                             all_tabs or cur_ref, default=cur_ref, key=f"fz_ref_{set_name}")
        robot = st.text_input("更新に使うロボット", value=one.get("refresh_robot", "")
                              or auto_jobs.DEFAULT_REFRESH_ROBOT, key=f"fz_robot_{set_name}")
        st.caption("② 架電で見るシート：「状態の選択肢」はカンマ区切り（例：対応中,不出,完了）。"
                   "シートに「案件 ID」の列が要ります。")
        df = pd.DataFrame([{"シート": t.get("name", ""),
                            "状態の選択肢": ",".join(t.get("status") or [])}
                           for t in one.get("tabs") or []] or [{"シート": "", "状態の選択肢": ""}])
        ed = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, hide_index=True,
            key=f"fz_tabs_{set_name}",
            column_config={"シート": st.column_config.SelectboxColumn(
                options=all_tabs or list(df["シート"]), required=True)})
        if st.button("💾 保存", key=f"fz_save_{set_name}"):
            tabs = []
            for _, r in ed.iterrows():
                nm = str(r.get("シート") or "").strip()
                if not nm or nm == "nan":
                    continue
                st_ = [s.strip() for s in str(r.get("状態の選択肢") or "").replace("、", ",").split(",")
                       if s.strip() and s.strip() != "nan"]
                tabs.append({"name": nm, "status": st_ or ["対応中", "不出", "完了"]})
            new_one = dict(one, sheet_url=url.strip(), refresh_tabs=ref,
                           refresh_robot=robot.strip(), tabs=tabs)
            fusen.save_cfg(supabase, {"sets": {set_name: new_one}})
            st.success("保存しました")
            st.rerun()


_render_settings()

if not one.get("sheet_url") or not one.get("tabs"):
    st.info("⚙️ 設定で、スプレッドシートのURLと見るシートを入れてください。")
    st.stop()
if not me:
    st.info("🙋 まず、あなたの名前を選んでください（だれが対応したかを残すため）。")
    st.stop()

# ==========================================
# 🔄 更新
# ==========================================
b1, b2, b3 = st.columns([2, 2, 4])
with b1:
    do_ref = st.button("🔄 SFコネクタで更新する", type="primary",
                       disabled=not one.get("refresh_tabs"),
                       help="①のシートを更新してから、付箋のシートを読み直します（数分かかります）")
with b2:
    if st.button("📄 スプシを読み直す", help="更新はせず、いまのスプシの中身を読み直します"):
        fusen.save_cfg(supabase, {"sets": {set_name: dict(one, reread=fusen.now_stamp())}})
        st.rerun()
with b3:
    if one.get("last_refresh"):
        st.caption(f"最後の更新：{one['last_refresh']}（{one.get('last_refresh_by', '')}）"
                   + ("" if str(one["last_refresh"]).startswith(fusen.today()) else "　⚠️ きょうはまだ更新していません"))

if do_ref:
    with st.spinner("SFコネクタで更新しています…（画面を閉じないでください）"):
        ok, log = fusen.run_refresh(_gc(), one, set_name,
                                    one.get("refresh_robot") or auto_jobs.DEFAULT_REFRESH_ROBOT)
    if ok:
        fusen.save_cfg(supabase, {"sets": {set_name: dict(
            one, last_refresh=fusen.now_stamp(), last_refresh_by=me)}})
        st.success("更新しました")
        st.rerun()
    else:
        st.error(f"🛑 更新できませんでした：{sms_runner.stop_reason(log) or '下のログを見てください'}")
        with st.expander("ログ"):
            st.code(log[-3000:])


# ==========================================
# 📋 一覧（数秒おきに、状態だけ読み直す）
# ==========================================
def _on_edit(key: str, ids: list, tab_status: list, cur: dict):
    ch_ = (st.session_state.get(key) or {}).get("edited_rows") or {}
    for ri, change in ch_.items():
        cid = ids[int(ri)]
        status = change.get("状態")
        memo = change.get("メモ")
        if status is not None:
            status = "" if status in (None, "—") else status
            other = cur.get(cid) or {}
            if status == "対応中" and other.get("s") == "対応中" and other.get("user") != me:
                st.toast(f"⚠️ {other.get('user')}さんが対応中です（{fusen.stamp_short(other.get('t'))}〜）。"
                         "二重にかけないよう、先に声をかけてください。")
                continue
        try:
            fusen.write_state(supabase, set_name, me, cid,
                              status=status, memo=None if memo is None else str(memo))
        except Exception as e:
            st.toast(f"🛑 保存できませんでした：{str(e)[:120]}")


@st.fragment(run_every=TICK_SEC)
def _board():
    try:
        live = fusen.load_cfg(supabase)["sets"][set_name]
        states = fusen.load_states(supabase, set_name)
    except Exception as e:
        st.warning(f"状態を読めませんでした（少し待つと読み直します）：{str(e)[:120]}")
        return
    stamp = f"{live.get('last_refresh', '')}|{live.get('reread', '')}"
    tabs = tuple(t["name"] for t in live.get("tabs") or [])
    try:
        data = _read(live["sheet_url"], tabs, stamp)
    except Exception as e:
        st.error(f"スプレッドシートを読めませんでした：{str(e)[:200]}")
        return
    cur, prev = fusen.merge(states)

    f1, f2 = st.columns(2)
    only_open = f1.toggle("まだ対応していない案件だけ", key="fz_only_open")
    only_me = f2.toggle("自分が触った案件だけ", key="fz_only_me")

    labels = []
    for t in live.get("tabs") or []:
        rows = (data.get(t["name"]) or {}).get("rows") or []
        n_done = sum(1 for r in rows if (cur.get(r.get(fusen.ID_COL, "")) or {}).get("s"))
        labels.append(f"{t['name']}（{n_done}/{len(rows)}）")
    for t, box in zip(live.get("tabs") or [], st.tabs(labels or ["—"])):
        with box:
            d = data.get(t["name"]) or {}
            if d.get("error"):
                st.warning(d["error"])
                continue
            rows = d.get("rows") or []
            if not rows:
                st.info("📭 いま出ている案件はありません")
                continue
            status_opts = ["—"] + list(t.get("status") or [])
            table, ids = [], []
            for r in rows:
                cid = r.get(fusen.ID_COL, "")
                e = cur.get(cid) or {}
                if only_open and e.get("s") and e.get("s") != "対応中":
                    continue
                if only_me and e.get("user") != me:
                    continue
                p = prev.get(cid) or {}
                ids.append(cid)
                table.append({
                    "状態": e.get("s") or "—",
                    "対応者": e.get("user", "") if e.get("s") or e.get("m") else "",
                    "時刻": fusen.stamp_short(e.get("t")) if e else "",
                    "メモ": e.get("m", ""),
                    "前回": f"{p.get('d', '')[5:].replace('-', '/')} {p.get('s', '')}（{p.get('user', '')}）" if p else "",
                    **{k: v for k, v in r.items() if k not in ("状態", "対応者", "時刻", "メモ", "前回")}})
            if not table:
                st.caption("条件に合う案件はありません")
                continue
            df = pd.DataFrame(table)
            # 状態が変わったときだけ表を作り直す（入力中に勝手に消えないように）
            ver = hashlib.md5(json.dumps(
                [table[i]["状態"] + table[i]["メモ"] + table[i]["対応者"] for i in range(len(table))]
                + [stamp, str(only_open), str(only_me)], ensure_ascii=False).encode()).hexdigest()[:10]
            key = f"fz_ed_{set_name}_{t['name']}_{ver}"
            st.data_editor(
                df, key=key, hide_index=True, use_container_width=True,
                height=min(38 + 35 * len(df), 640),
                disabled=[col for col in df.columns if col not in ("状態", "メモ")],
                column_config={
                    "状態": st.column_config.SelectboxColumn(options=status_opts, required=True, width="small"),
                    "メモ": st.column_config.TextColumn(width="medium"),
                    "対応者": st.column_config.TextColumn(width="small"),
                    "時刻": st.column_config.TextColumn(width="small"),
                },
                on_change=_on_edit, args=(key, ids, t.get("status") or [], cur))
    st.caption(f"🔁 {TICK_SEC}秒ごとに、ほかのPCの操作を読み直しています。"
               "スプシの中身は「更新」か「読み直す」を押したときだけ読みます。")


_board()
