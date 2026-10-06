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
def _split(v) -> list:
    return [x.strip() for x in str(v or "").replace("、", ",").split(",")
            if x.strip() and x.strip() not in ("nan", "None")]


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
        ref = st.multiselect("① 更新するシート（SFのレポートが入っているシート・上から順に更新）",
                             all_tabs or cur_ref, default=cur_ref, key=f"fz_ref_{set_name}")
        robot = st.text_input("更新に使うロボット", value=one.get("refresh_robot", "")
                              or auto_jobs.DEFAULT_REFRESH_ROBOT, key=f"fz_robot_{set_name}")
        st.caption("② 架電で見るシート：「状態の選択肢」はカンマ区切り（例：対応中,不出,完了）。"
                   "「不動産ごとにまとめる付箋」に付箋の内容（例：出電_催促）を入れると、"
                   "その付箋は不動産（店舗/顧客名）ごとにまとめて出ます。シートに「案件 ID」の列が要ります。")
        df = pd.DataFrame([{"シート": t.get("name", ""),
                            "状態の選択肢": ",".join(t.get("status") or []),
                            "不動産ごとにまとめる付箋": ",".join(t.get("group") or [])}
                           for t in one.get("tabs") or []]
                          or [{"シート": "", "状態の選択肢": "", "不動産ごとにまとめる付箋": ""}])
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
                st_ = _split(r.get("状態の選択肢"))
                tabs.append({"name": nm, "status": st_ or ["対応中", "不出", "完了"],
                             "group": _split(r.get("不動産ごとにまとめる付箋"))})
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
    do_ref = st.button("🔄 SFのレポートを更新する", type="primary",
                       disabled=not one.get("refresh_tabs"),
                       help="①のシートを更新してから、付箋のシートを読み直します")
with b2:
    if st.button("📄 スプシを読み直す", help="更新はせず、いまのスプシの中身を読み直します"):
        fusen.save_cfg(supabase, {"sets": {set_name: dict(one, reread=fusen.now_stamp())}})
        st.rerun()
with b3:
    if one.get("last_refresh"):
        st.caption(f"最後の更新：{one['last_refresh']}（{one.get('last_refresh_by', '')}）"
                   + ("" if str(one["last_refresh"]).startswith(fusen.today()) else "　⚠️ きょうはまだ更新していません"))

if do_ref:
    with st.spinner("更新しています…（画面を閉じないでください）"):
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
def _on_edit(key: str, ids: list, cur: dict):
    ch_ = (st.session_state.get(key) or {}).get("edited_rows") or {}
    changes = {}
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
        changes[cid] = {"s": status, "m": None if memo is None else str(memo)}
    if changes:
        try:
            fusen.write_states(supabase, set_name, me, changes, cur)
        except Exception as e:
            st.toast(f"🛑 保存できませんでした：{str(e)[:120]}")


def _on_bulk(ids: list, status: str, cur: dict, store: str):
    """🏢 その不動産の案件を、まとめて同じ状態にする（1回で書く）。"""
    if status == "対応中":
        busy = sorted({(cur.get(c) or {}).get("user") for c in ids
                       if (cur.get(c) or {}).get("s") == "対応中" and (cur.get(c) or {}).get("user") != me})
        if busy:
            st.toast(f"⚠️ 「{store}」は {'・'.join(busy)}さんが対応中です。"
                     "二重にかけないよう、先に声をかけてください。")
            return
    try:
        fusen.write_states(supabase, set_name, me,
                           {c: {"s": "" if status == "—" else status} for c in ids}, cur)
    except Exception as e:
        st.toast(f"🛑 保存できませんでした：{str(e)[:120]}")


def _keep(e: dict, only_open: bool, only_me: bool) -> bool:
    if only_open and e.get("s") and e.get("s") != "対応中":
        return False
    if only_me and e.get("user") != me:
        return False
    return True


def _table(rows, keytag: str, status_opts: list, cur: dict, prev: dict, stamp: str, hide=()):
    """案件の表（状態とメモだけ直せる）。状態が変わったときだけ作り直す（入力中に消えないように）。"""
    table, ids = [], []
    for r in rows:
        cid = r.get(fusen.ID_COL, "")
        e = cur.get(cid) or {}
        p = prev.get(cid) or {}
        ids.append(cid)
        table.append({
            "状態": e.get("s") or "—",
            "対応者": e.get("user", "") if e.get("s") or e.get("m") else "",
            "時刻": fusen.stamp_short(e.get("t")) if e else "",
            "メモ": e.get("m", ""),
            "前回": f"{p.get('d', '')[5:].replace('-', '/')} {p.get('s', '')}（{p.get('user', '')}）" if p else "",
            **{k: v for k, v in r.items()
               if k not in ("状態", "対応者", "時刻", "メモ", "前回") and k not in hide}})
    df = pd.DataFrame(table)
    ver = hashlib.md5(json.dumps(
        [x["状態"] + x["メモ"] + x["対応者"] for x in table] + ids + [stamp],
        ensure_ascii=False).encode()).hexdigest()[:10]
    key = f"fz_ed_{set_name}_{keytag}_{ver}"
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
        on_change=_on_edit, args=(key, ids, cur))


def _groups(t: dict, groups, status_opts: list, cur: dict, prev: dict, stamp: str):
    """🏢 不動産ごとのかたまり。1つの不動産に何件あるかを先に出し、まとめて状態を付けられる。"""
    words = "・".join(t.get("group") or [])
    n = sum(len(g) for _, g in groups)
    st.markdown(f"#### 🏢 {words}（不動産ごと）　{n}件／{len(groups)}社")
    st.caption("同じ不動産の案件は1回の連絡でまとめて伝えられます。"
               "右のボタンで、その不動産の案件をまとめて同じ状態にできます（1件ずつは表で）。")
    for store, grows in groups:
        ids = [r.get(fusen.ID_COL, "") for r in grows]
        sts = [(cur.get(c) or {}).get("s", "") for c in ids]
        left = sum(1 for x in sts if not x or x == "対応中")
        who = sorted({(cur.get(c) or {}).get("user") for c in ids
                      if (cur.get(c) or {}).get("s") == "対応中"})
        mark = "✅" if left == 0 else ("📞" if who else "🏢")
        tag = hashlib.md5(store.encode()).hexdigest()[:8]
        with st.container(border=True):
            h1, h2 = st.columns([3, 4])
            h1.markdown(f"**{mark} {store}**　{len(grows)}件"
                        + (f"（まだ {left}件）" if left else "（済み）")
                        + (f"　📞 {'・'.join(who)}さんが対応中" if who else ""))
            cols = h2.columns([2] + [3] * len(status_opts))
            cols[0].markdown("まとめて：")
            for b, sv in zip(cols[1:], status_opts):
                b.button("外す" if sv == "—" else sv, help=f"この不動産の{len(grows)}件をまとめて「{'空' if sv == '—' else sv}」にします",
                         key=f"fz_bulk_{set_name}_{t['name']}_{tag}_{sv}",
                         on_click=_on_bulk, args=(ids, sv, cur, store), use_container_width=True)
            _table(grows, f"{t['name']}_g{tag}", status_opts, cur, prev, stamp, hide=(fusen.GROUP_COL,))


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
            rows = [r for r in rows if _keep(cur.get(r.get(fusen.ID_COL, "")) or {}, only_open, only_me)]
            groups, rest = fusen.split_groups(rows, d.get("head"), t.get("group"))
            if groups:
                _groups(t, groups, status_opts, cur, prev, stamp)
                if rest:
                    st.markdown("#### そのほかの付箋")
            if rest:
                _table(rest, t["name"], status_opts, cur, prev, stamp)
            if not groups and not rest:
                st.caption("条件に合う案件はありません")
    st.caption(f"🔁 {TICK_SEC}秒ごとに、ほかのPCの操作を読み直しています。"
               "スプシの中身は「更新」か「読み直す」を押したときだけ読みます。")


_board()
