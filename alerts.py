"""
🔔 対応が済んでいない通知（時間指定の自動実行が Slack に送った「見てほしいこと」）。

Slack は件数が多い日に埋もれるので、同じ中身をアプリにも残し、
**人が「✅ 完了」を押すまで**左のメニューに件数を出す（0件なら何も出さない）。

- 置き場は Supabase の予約行2つ（どのPCから見ても同じ数字になる）：
  - `__alerts__`      … 通知そのもの。**書くのは見回り役（scheduler.py）だけ**
  - `__alerts_done__` … 完了の印（誰が・いつ・何で対応したか）。**書くのは画面だけ**
  ⚠️ 1つの行を両方が書くと、同時に書いたときに片方が消える（`__schedule_req__` と同じ考え）。
- ⭐ 自動では消さない。次の回が成功しても、前の回で送れなかった人を確かめたとは限らないため。
- ⚠️ Streamlit はこのファイルの上では import しない（scheduler.py からも使うため）。
"""
import datetime as dt
import socket
import uuid

import auto_jobs

ALERTS_ID = "__alerts__"
DONE_ID = "__alerts_done__"
KEEP = 500                      # 通知はこれだけ残す（古い完了済みから捨てる。未完了は捨てない）
REFRESH_SEC = 30                # 画面が見直す間隔（ほかのPCで押した完了が、これだけで反映される）

# 業務の種類 → 件数を出すページ（pages/ のファイル名）
PAGE_OF_KIND = {
    "progress": "3_🚀_進捗反映自動化.py",
    "sms": "6_📱_SMS送信.py",
    "dataloader": "7_🗃_データローダー自動化.py",
    "autocall": "8_📞_オートコール投入.py",
    "reports": "2_📝_エントリー業務自動化.py",
    "irregular": "12_📣_イレギュラー報告.py",
    "precheck": "9_🔎_エントリー前DC.py",
    "chiiki": "2_📝_エントリー業務自動化.py",
    "kurashi": "2_📝_エントリー業務自動化.py",
    "marche": "2_📝_エントリー業務自動化.py",
    "fp_toss": "2_📝_エントリー業務自動化.py",
    "callrec": "10_🎧_通録ダウンロード.py",
    "product_update": "15_📄_商品情報の更新.py",
    "robot": "2_📝_エントリー業務自動化.py",
    "login": "99_⚙️_その他設定.py",
    "schedule": "11_⏰_時間指定の自動実行.py",
}
SUMMARY_PAGE = "1_📊_全状況進捗確認.py"   # ここには全部の合計を出す


def _save(sb, row_id: str, name: str, cfg: dict) -> bool:
    for _ in range(3):
        try:
            sb.table("merchants").upsert({
                "id": row_id, "name": name, "is_active": False,
                "connector_type": "settings", "config_json": cfg}).execute()
            return True
        except Exception:
            continue
    return False


def page_of(kind: str) -> str:
    return PAGE_OF_KIND.get(kind or "", PAGE_OF_KIND["schedule"])


# ==========================================
# ✍️ 足す（見回り役だけが呼ぶ）
# ==========================================
def add(sb, kind: str, title: str, text: str, state: str = "", item_id: str = "") -> bool:
    """Slackに送ったのと同じ中身を1件足す。⚠️ 読み直して足す（ほかの通知を消さない）。"""
    try:
        row = auto_jobs.load_row(sb, ALERTS_ID)
        done = done_map(sb)
    except Exception:
        return False
    lst = row.get("alerts") or []
    lst.insert(0, {"id": uuid.uuid4().hex[:12], "at": f"{dt.datetime.now():%Y/%m/%d %H:%M}",
                   "kind": kind or "schedule", "page": page_of(kind), "title": title,
                   "text": text, "state": state, "item": str(item_id or ""),
                   "pc": socket.gethostname()})
    if len(lst) > KEEP:
        # 古い完了済みから捨てる（未完了は、どれだけ古くても残す）
        over = len(lst) - KEEP
        keep = []
        for a in reversed(lst):
            if over and a.get("id") in done:
                over -= 1
                continue
            keep.append(a)
        lst = list(reversed(keep))
    row["alerts"] = lst
    return _save(sb, ALERTS_ID, "（対応が済んでいない通知）", row)


# ==========================================
# 👀 読む
# ==========================================
def done_map(sb) -> dict:
    return (auto_jobs.load_row(sb, DONE_ID).get("done") or {})


def load(sb) -> tuple:
    """(未完了の通知, 完了した通知) を新しい順で返す。完了には印（誰が・いつ・何で）を付ける。"""
    alerts = auto_jobs.load_row(sb, ALERTS_ID).get("alerts") or []
    done = done_map(sb)
    open_, closed = [], []
    for a in alerts:
        if a.get("id") in done:
            closed.append({**a, "done": done[a["id"]]})
        else:
            open_.append(a)
    return open_, closed


def counts(open_alerts: list) -> dict:
    """ページ（ファイル名）→ 未完了の件数。全状況進捗確認には合計を入れる。"""
    out = {}
    for a in open_alerts:
        p = a.get("page") or page_of(a.get("kind"))
        out[p] = out.get(p, 0) + 1
    if open_alerts:
        out[SUMMARY_PAGE] = len(open_alerts)
    return out


# ==========================================
# ✅ 完了にする／戻す（画面だけが呼ぶ）
# ==========================================
def mark_done(sb, ids: list, note: str = "") -> bool:
    row = auto_jobs.load_row(sb, DONE_ID)          # ⚠️ 書く直前に読み直す（ほかのPCの完了を消さない）
    done = row.get("done") or {}
    for i in ids:
        done[i] = {"at": f"{dt.datetime.now():%Y/%m/%d %H:%M}", "pc": socket.gethostname(),
                   "note": note}
    # もう通知の一覧に無いものの印は捨てる（増え続けないように）
    try:
        alive = {a.get("id") for a in (auto_jobs.load_row(sb, ALERTS_ID).get("alerts") or [])}
        done = {k: v for k, v in done.items() if k in alive or k in ids}
    except Exception:
        pass
    row["done"] = done
    return _save(sb, DONE_ID, "（通知の対応済み）", row)


def undo(sb, alert_id: str) -> bool:
    row = auto_jobs.load_row(sb, DONE_ID)
    done = row.get("done") or {}
    done.pop(alert_id, None)
    row["done"] = done
    return _save(sb, DONE_ID, "（通知の対応済み）", row)


# ==========================================
# 🔴 左のメニューに件数を出す（全ページの theme.brand_sidebar から）
# ==========================================
def _nav_path(page_file: str) -> str:
    """pages/ のファイル名 → メニューのリンクの末尾（Streamlit と同じ決め方）。"""
    import re
    stem = page_file.rsplit(".", 1)[0]
    stem = re.sub(r"^[0-9]+[_ -]*", "", stem)          # 先頭の番号
    parts = stem.split("_", 1)
    if len(parts) == 2 and not re.search(r"\w", parts[0]):  # 先頭の絵文字
        stem = parts[1]
    return stem


def badge_css(cnt: dict) -> str:
    rules = []
    for page, n in cnt.items():
        if n <= 0:
            continue
        path = _nav_path(page)
        label = "99+" if n > 99 else str(n)
        rules.append(
            f'[data-testid="stSidebarNav"] a[href$="/{path}"]::after,'
            f'[data-testid="stSidebarNav"] a[href$="/{_pct(path)}"]::after'
            f'{{content:"{label}";}}')
    if not rules:
        return ""
    return ("<style>"
            '[data-testid="stSidebarNav"] a{position:relative;}'
            '[data-testid="stSidebarNav"] a::after{position:absolute;right:10px;top:50%;'
            'transform:translateY(-50%);min-width:20px;height:20px;padding:0 6px;border-radius:10px;'
            'background:#EF4444;color:#fff;font-size:12px;font-weight:800;line-height:20px;'
            'text-align:center;box-sizing:border-box;}'
            + "".join(rules) + "</style>")


def _pct(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


# ==========================================
# 🖥 画面（Streamlit の中でだけ使う）
# ==========================================
STATE_ICON = {"失敗": "🛑", "確認待ち": "⏸", "見送り": "⏭", "完了": "🛡", "ログイン切れ": "🔐"}
_CACHED = {}


def _st_funcs():
    """Supabase の接続と、未完了・完了の読み込み（少しのあいだ覚えておく）。"""
    import streamlit as st
    if not _CACHED:
        @st.cache_resource
        def _sb():
            from supabase import create_client
            return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

        @st.cache_data(ttl=REFRESH_SEC - 5, show_spinner=False)
        def _alerts_now():
            return load(_sb())

        _CACHED.update(sb=_sb, load=_alerts_now)
    return _CACHED


def clear_cache():
    """完了を押したあと、件数と一覧をすぐ描き直すため。"""
    try:
        _st_funcs()["load"].clear()
    except Exception:
        pass


def sidebar_badges():
    """30秒おきに Supabase を見直して、メニューの件数を描き直す。つまずいても画面は止めない。"""
    import streamlit as st

    @st.fragment(run_every=REFRESH_SEC)
    def _draw():
        try:
            css = badge_css(counts(_st_funcs()["load"]()[0]))
        except Exception:
            css = ""
        st.markdown(css or "<span></span>", unsafe_allow_html=True)

    _draw()


def _one(st, sb, a: dict, key: str, expanded: bool, show_link: bool):
    icon = STATE_ICON.get(a.get("state", ""), "🔔")
    with st.expander(f"{icon} {a.get('title', '')}　{a.get('at', '')}（{a.get('state', '')}）",
                     expanded=expanded):
        st.code(a.get("text", ""), language=None, wrap_lines=True)
        c1, c2, c3 = st.columns([3, 1, 1]) if show_link else (*st.columns([3, 1]), None)
        with c1:
            note = st.text_input("何で対応したか（任意）", key=f"{key}_note_{a['id']}",
                                 placeholder="例：SMS送信のページから送り直した／Salesforceを手で直した",
                                 label_visibility="collapsed")
        with c2:
            if st.button("✅ 完了", key=f"{key}_done_{a['id']}", use_container_width=True):
                if mark_done(sb, [a["id"]], note):
                    clear_cache()
                    st.rerun()
                else:
                    st.error("保存できませんでした。もう一度押してください。")
        if c3 is not None:
            with c3:
                st.page_link(f"pages/{a.get('page') or page_of(a.get('kind'))}", label="ページへ ▶")


def render_box(page_file: str = ""):
    """対応が済んでいない通知を出して、その場で完了にできる。

    page_file を渡すと、そのページの分だけ（無ければ何も出さない）。空なら全部（全状況進捗確認）。
    どちらで完了を押しても同じ印（`__alerts_done__`）なので、もう片方からも消える。
    """
    import streamlit as st

    @st.fragment(run_every=REFRESH_SEC)
    def _box():
        f = _st_funcs()
        try:
            open_, closed = f["load"]()
        except Exception as e:
            if not page_file:
                st.warning(f"通知を読み込めませんでした：{str(e)[:200]}")
            return
        sb = f["sb"]()
        if page_file:
            mine = [a for a in open_ if (a.get("page") or page_of(a.get("kind"))) == page_file]
            if not mine:
                return
            with st.container(border=True):
                st.markdown(f"**🔔 このページの、対応が済んでいない通知（{len(mine)}件）**")
                st.caption("Slack に送ったのと同じ文です。対応したら「✅ 完了」を押してください"
                           "（📊 全状況進捗確認からも完了にできます）。")
                for i, a in enumerate(mine):
                    _one(st, sb, a, "alp", expanded=i < 3, show_link=False)
            return
        st.markdown(f"### 🔔 対応が済んでいない通知（{len(open_)}件）")
        st.caption("時間指定の自動実行が Slack に送った「見てほしいこと」です。対応したら「✅ 完了」を押してください"
                   "（その業務のページの上にも出ていて、そこからも完了にできます）。"
                   "押すまで左のメニューに件数が出ます（どのPCから見ても同じです。30秒ごとに読み直します）。"
                   "次の回が成功しても、自動では消えません。")
        if not open_:
            st.success("対応が済んでいない通知はありません。")
        for a in open_:
            _one(st, sb, a, "al", expanded=True, show_link=True)
        if closed:
            with st.expander(f"✅ 完了にした通知（新しい順・{min(len(closed), 30)}件）"):
                for a in closed[:30]:
                    d = a.get("done") or {}
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.markdown(f"{STATE_ICON.get(a.get('state', ''), '🔔')} **{a.get('title', '')}**　{a.get('at', '')}　"
                                    f"→ {d.get('at', '')} 完了（{d.get('pc', '')}）"
                                    + (f"：{d.get('note')}" if d.get("note") else ""))
                    with c2:
                        if st.button("↩ 戻す", key=f"al_undo_{a['id']}"):
                            undo(sb, a["id"])
                            clear_cache()
                            st.rerun()

    _box()


def page_box_for_caller(depth: int = 2):
    """呼んだページ（pages/ のファイル）の分だけ出す。theme.brand_sidebar から呼ぶ。"""
    import os
    import sys
    try:
        fn = os.path.basename(sys._getframe(depth).f_code.co_filename)
    except Exception:
        return
    if fn and fn != SUMMARY_PAGE and fn in set(PAGE_OF_KIND.values()):
        render_box(fn)
