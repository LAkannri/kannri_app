"""
🔒 クラウド版のログイン（パスワード1つ・全員共通）

⭐ **クラウド（Streamlit Cloud）で開いたときだけ**、パスワードを聞いてから画面を出す。
   担当者のPC（start.bat）では聞かない（そのPCを使える人は、もう secrets.toml を持っている）。

- パスワードは Supabase の予約行 `__login__` に**ハッシュだけ**を置く（元の文字は残さない）。
  ⚠️ 暗号化（ENKAN_SECRET_KEY）ではなくハッシュにしてある。クラウドに鍵が無くても確かめられ、
     Supabase を見られても元のパスワードは分からない。
- 変えるのは「⚙️ その他設定 → 🔒 クラウド版のログイン」（`render_settings`）。
  変えると `ver` が変わり、**ほかの人のログインも切れる**（古いパスワードで入ったままにしない）。
- まだ設定していないときは、クラウドでは**開かせない**（最初に来た人が決められると意味がないため）。
  担当者のPCのアプリから設定する。
- Supabase が読めないときも開かせない（確かめられないまま通さない）。
- 呼ぶのは `secrets_check.check()` の最後＝全ページの先頭で必ず通る。

クラウドかどうか：環境変数 `ENKAN_REQUIRE_LOGIN`（1＝聞く／0＝聞かない）があればそれ、
無ければ Streamlit Cloud の置き場所（`/mount/src/`）で見分ける。
"""
import hashlib
import hmac
import os
import secrets as _rand
import time

import streamlit as st

ROW = "__login__"
ITER = 200_000
MIN_LEN = 8
RECHECK_SEC = 60          # 変えたパスワードを、ほかの人の画面がこの秒数のうちに読む
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def is_cloud() -> bool:
    v = str(os.environ.get("ENKAN_REQUIRE_LOGIN", "")).strip()
    if v in ("1", "0"):
        return v == "1"
    return BASE_DIR.replace("\\", "/").startswith("/mount/src/")


def _sb(sb=None):
    if sb is not None:
        return sb
    from supabase import create_client
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


def _row(sb) -> dict:
    res = sb.table("merchants").select("config_json").eq("id", ROW).execute()
    return (res.data[0].get("config_json") or {}) if res.data else {}


def _hash(pw: str, salt: str, n: int = ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), n).hex()


def _verify(pw: str, cur: dict) -> bool:
    if not cur.get("hash") or not cur.get("salt"):
        return False
    got = _hash(pw, cur["salt"], int(cur.get("n") or ITER))
    return hmac.compare_digest(got, cur["hash"])


@st.cache_data(ttl=RECHECK_SEC, show_spinner=False)
def _current() -> dict:
    """いまの設定。画面を開くたびに Supabase へ行かないよう少し覚える。"""
    return _row(_sb())


def info(sb=None) -> dict:
    try:
        cur = _row(_sb(sb))
    except Exception as e:
        return {"error": str(e)[:160]}
    return {"saved": bool(cur.get("hash")), "saved_at": cur.get("saved_at", ""),
            "saved_by": cur.get("saved_by", "")}


def set_password(new: str, confirm: str, current: str = "", need_current: bool = True,
                 who: str = "", sb=None):
    """戻り値：(できたか, 言葉)"""
    new = str(new or "")
    if len(new) < MIN_LEN:
        return False, f"パスワードは{MIN_LEN}文字以上にしてください。"
    if new != str(confirm or ""):
        return False, "確認のために入れたパスワードが違います。"
    if new.strip() != new:
        return False, "パスワードの前後に空白は入れられません。"
    try:
        client = _sb(sb)
        cur = _row(client)                       # ⚠️ 読み直して足す
    except Exception as e:
        return False, f"Supabase を読めませんでした: {str(e)[:160]}"
    if cur.get("hash") and need_current and not _verify(str(current or ""), cur):
        time.sleep(1.5)
        return False, "いまのパスワードが違います。"
    salt = _rand.token_hex(16)
    cur.update({"salt": salt, "n": ITER, "hash": _hash(new, salt), "ver": _rand.token_hex(8),
                "saved_at": time.strftime("%Y/%m/%d %H:%M"), "saved_by": who})
    try:
        client.table("merchants").upsert({
            "id": ROW, "name": "（クラウド版のログイン）", "is_active": False,
            "connector_type": "settings", "config_json": cur}).execute()
    except Exception as e:
        return False, f"保存できませんでした: {str(e)[:160]}"
    _current.clear()
    st.session_state["_login_ver"] = cur["ver"]      # 変えた本人は入ったまま
    return True, "保存しました。クラウド版は、次から新しいパスワードで入ります（ほかの人のログインは1分以内に切れます）。"


def require():
    """クラウドなら、ログインするまで先へ進ませない。"""
    if not is_cloud():
        return
    try:
        cur = _current()
    except Exception as e:
        st.error("🔒 ログインの設定を読めなかったので、開けません。少し待ってから読み込み直してください。")
        st.caption(str(e)[:200])
        st.stop()
    if not cur.get("hash"):
        st.error("🔒 **クラウド版のパスワードが、まだ決まっていません。**")
        st.markdown("担当者のPCでアプリを開き、**⚙️ その他設定 → 🔒 クラウド版のログイン** で決めてください。"
                    "決めるまで、クラウド版は誰も開けません。")
        st.stop()
    if st.session_state.get("_login_ver") and st.session_state["_login_ver"] == cur.get("ver"):
        with st.sidebar:
            if st.button("🔓 ログアウト", key="_logout_btn", use_container_width=True):
                st.session_state.pop("_login_ver", None)
                st.rerun()
        return
    st.session_state.pop("_login_ver", None)

    st.markdown("## 🔒 エンカンAI（クラウド版）")
    st.caption("社内の人だけが使う画面です。パスワードを入れてください。")
    with st.form("_login_form"):
        pw = st.text_input("パスワード", type="password")
        ok = st.form_submit_button("ログイン", type="primary")
    if ok:
        if _verify(pw, cur):
            st.session_state["_login_ver"] = cur.get("ver")
            st.session_state.pop("_login_ng", None)
            st.rerun()
        n = int(st.session_state.get("_login_ng", 0)) + 1
        st.session_state["_login_ng"] = n
        time.sleep(min(10, 1.5 * n))             # 当てずっぽうを遅くする
        st.error("パスワードが違います。")
    st.stop()


def render_settings(sb=None, who: str = ""):
    """⚙️ その他設定の画面。"""
    st.markdown("### 🔒 クラウド版のログイン")
    st.caption("クラウド（Streamlit Cloud）で開いたときだけ、このパスワードを聞きます。"
               "担当者のPCで開くときは聞きません。パスワードは元の文字を残さない形（ハッシュ）で Supabase に置きます。")
    i = info(sb)
    if i.get("error"):
        st.error(f"Supabase を読めませんでした：{i['error']}")
        return
    if i.get("saved"):
        st.success(f"✅ 設定済みです（{i.get('saved_at', '')}・{i.get('saved_by', '')}）")
    else:
        st.warning("まだ決まっていません。**決めるまで、クラウド版は誰も開けません。**")
    cloud = is_cloud()
    need_cur = bool(i.get("saved")) and cloud
    with st.form("_login_set_form", clear_on_submit=True):
        c0 = st.text_input("いまのパスワード", type="password",
                           disabled=not need_cur,
                           help=None if need_cur else "担当者のPCからは、いまのパスワード無しで変えられます（忘れたとき用）。")
        c1, c2 = st.columns(2)
        new = c1.text_input(f"新しいパスワード（{MIN_LEN}文字以上）", type="password")
        conf = c2.text_input("新しいパスワード（確認）", type="password")
        go = st.form_submit_button("💾 変更する" if i.get("saved") else "💾 決める")
    if go:
        ok, msg = set_password(new, conf, c0, need_current=need_cur, who=who, sb=sb)
        (st.success if ok else st.error)(msg)
    if i.get("saved"):
        st.caption("変えると、クラウド版を開いているほかの人も、1分以内にログインし直しになります。")
