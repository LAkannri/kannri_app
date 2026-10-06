import streamlit as st
import characters as ch
import common_robots
import theme
import gemini_key
from supabase import create_client, Client

st.set_page_config(page_title="全体を管理する - エンカンAI", layout="wide")

# 共有デザインシステム＋サイドバーのブランド（管理者を強調）
theme.inject_theme()

# 🔑 接続キーのファイルが壊れていたら、直す場所を名指しして止める。
#    別のPCに入れるときに、コピーし損ねて動かなくなることがあるため。
import secrets_check
secrets_check.check()
theme.brand_sidebar(active="manage")

# --- ⚙️ カンナ（管理者）の管理部屋 ---
ch.hero("manage", subtitle="接続キー・ロボットの稼働・クラウド実行をここで管理します。")

ch.guide("manage",
         "ここは全体を<b>ととのえる</b>部屋。接続キーやクラウド実行の設定はわたしが案内するね。"
         "まずは下のチェックがそろっているか確認しよう。")

st.write("")

# --- 接続キーの状態（secrets が読めているかを確認） ---
st.markdown("### 🔑 接続キーの状態")
KEY_LABELS = {
    "SUPABASE_URL": "Supabase URL",
    "SUPABASE_KEY": "Supabase キー",
    "GEMINI_API_KEY": "Gemini APIキー",
    "SF_PASSWORD": "Salesforce のログイン",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "スプレッドシートの鍵",
}
cols = st.columns(len(KEY_LABELS))
for col, (key, label) in zip(cols, KEY_LABELS.items()):
    with col:
        ok = False
        try:
            if key == "GEMINI_API_KEY":
                ok = bool(gemini_key.api_key(st.secrets))
            elif key == "SF_PASSWORD":
                # ⚠️ ユーザー名とパスワードの両方がそろって初めて接続できる
                ok = bool(str(st.secrets.get("SF_USERNAME", "") or "").strip()
                          and str(st.secrets.get("SF_PASSWORD", "") or "").strip())
            else:
                ok = bool(st.secrets.get(key))
        except Exception:
            ok = False
        if ok:
            st.success(f"✅ {label}\n設定済み")
        else:
            st.error(f"⚠️ {label}\n未設定")
st.caption("※ Salesforce のログインは **SF_USERNAME／SF_PASSWORD（＋必要なら SF_SECURITY_TOKEN）**です"
           "（APIキーではありません）。スプレッドシートの鍵（GOOGLE_SERVICE_ACCOUNT_JSON）とあわせて、"
           "**SFコネクタを使わない更新**に要ります。"
           "クラウド版は Streamlit Cloud の Manage app → Settings → Secrets、"
           "クラウド実行（GitHub Actions）は GitHub の Secrets に登録します。")

st.divider()

# --- クラウド自動実行のしくみ ---
st.markdown("### ☁️ クラウド自動実行（GitHub Actions）")
st.markdown("""
- **毎朝 8:00（JST）に自動実行**：担当者のPCを開かなくても、クラウドでロボットが動きます。
- **スケジュール実行は必ずドライラン**：対象件数を表示するだけで、実際の申請はしません（安全）。
- **本番実行**：GitHub の Actions タブ →「Run workflow」で **`live` を ON** にしたときだけ申請します。
- **二重申請の防止**：処理済みの案件はシステムが記録し、次回から自動でスキップします。
""")

with st.expander("🛡️ スプレッドシート連携の前提（重要）"):
    st.markdown("""
- SFAスプレッドシートの共有を **「リンクを知っている全員（閲覧者）」** にしてください。
- 読み取り専用のため、スプシの「ステータス」列は **自動では更新されません**
  （二重申請はシステム側の記録で防ぎます）。
- ステータスの書き戻しが必要な場合は、サービスアカウント方式への切替が前提になります。
""")

st.divider()

# --- 🤖 共通ロボットの登録（ここで一度録画すれば、どのページからも使える） ---
#     SFコネクタの更新もプッシュプロの送信も、どのスプシでも押す場所は同じ。
#     違うのは「どのシートを選ぶか」「どのファイルを渡すか」だけなので、
#     録画は1台ずつで足りる。ページごとに録らせない。
st.markdown("### 🤖 共通ロボットの登録")
st.caption("ここで一度だけ録画しておけば、**「📱 SMS送信」でも「🗃 データローダー自動化」でも、"
           "シート名を選ぶだけ**で動きます。スプレッドシートが増えても録画し直しは要りません。")


@st.cache_resource
def _sb():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


try:
    common_robots.render(_sb(), default_urls={"send": "https://ppsms.jp/"})
except Exception as _e:
    st.error(f"共通ロボットの画面を出せませんでした：{_e}")

st.divider()

# --- ☁️ SFコネクタを使わない更新（確かめて、値が変わらないシートだけ切り替える） ---
import sf_api_ui
try:
    sf_api_ui.render(_sb())
except Exception as _e:
    st.error(f"SFコネクタを使わない更新の画面を出せませんでした：{_e}")

st.divider()

import socket
# --- 🔒 クラウド版のログイン（パスワードは Supabase にハッシュで置く・ここで変える） ---
import login_gate
login_gate.render_settings(_sb(), who=socket.gethostname())

st.divider()

# --- 🤖 Gemini の APIキー（全PCで共有） ---
#     ⚠️ secrets.toml はもう配ってあるので、キーを変えるたびに配り直さない。
#        ここで保存したキーは secrets.toml のキーより優先される（古いキーが勝たないように）。
st.markdown("### 🤖 Gemini の APIキー")
st.caption("AI（手順書づくり・DCルール・商品情報の更新など）で使うキーです。"
           "**ここで保存すると、どのPCも5分以内にこのキーに切り替わります**（各PCの secrets.toml より優先）。")
_g_key, _g_src, _g_why = gemini_key.source(st.secrets, _sb(), fresh=True)
_g_info = gemini_key.shared_info(None, _sb())
if _g_src == "共有":
    st.success(f"✅ 共有のキーを使っています（{_g_info.get('saved_at', '')}・{_g_info.get('saved_by', '')}）。末尾 …{_g_key[-4:]}")
elif _g_why:
    st.error(f"⚠️ 共有のキーが保存されていますが、このPCでは読めません：{_g_why}（いまは secrets.toml のキーで動いています）")
elif _g_src == "このPC":
    st.info(f"このPCの secrets.toml のキーを使っています（末尾 …{_g_key[-4:]}）。共有のキーはまだありません。")
elif _g_src == "環境変数":
    st.info("環境変数の GEMINI_API_KEY を使っています（いちばん優先）。")
else:
    st.warning("キーがありません。AIの機能は使えません。")
_gk1, _gk2 = st.columns([3, 1])
with _gk1:
    _new_gk = st.text_input("新しい Gemini APIキー", type="password", key="gemini_key_new",
                            placeholder="AIza… または AQ.…", help="Google AI Studio で作ったキー。保存すると画面には出しません。")
with _gk2:
    st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)
    if st.button("💾 確かめて保存", use_container_width=True, disabled=not _new_gk, key="gemini_save"):
        _ok, _msg = gemini_key.test(_new_gk)
        if _ok:
            _ok, _msg = gemini_key.save_shared(_new_gk, who=socket.gethostname(), sb=_sb())
        (st.success if _ok else st.error)(_msg)
if _g_info.get("saved"):
    _agree_gclear = st.checkbox("共有のキーを消します（各PCの secrets.toml のキーに戻ります）", key="gemini_clear_ok")
    if st.button("🗑 共有のキーを消す", disabled=not _agree_gclear, key="gemini_clear"):
        _ok, _msg = gemini_key.clear_shared(sb=_sb())
        (st.success if _ok else st.error)(_msg)

st.divider()

# --- 🔔 Slack通知の送り先（全PCで共有） ---
#     ⚠️ URLをPCごとに secrets.toml へ書かせない。担当者のPCに少しずつ入れることになり、
#        入っていないPCだけ通知が来ない。ここで1回保存すれば、暗号化して Supabase に入り、全PCが読む。
import socket
import slack_notify

st.markdown("### 🔔 Slack通知の送り先")
st.caption("ロボットの完了・失敗や、時間指定の自動実行の結果を知らせる先です。"
           "**ここで1回保存すれば、どのPCからも通知します**（PCごとに入れる必要はありません）。")
_s_url, _s_src, _s_why = slack_notify.webhook_url(None, _sb(), fresh=True)
_s_info = slack_notify.shared_info(None, _sb())
if _s_src == "このPC":
    st.info("このPCは `secrets.toml` の SLACK_WEBHOOK_URL を使っています（そちらが優先されます）。")
elif _s_url:
    st.success(f"✅ 保存済みです（{_s_info.get('saved_at', '')}・{_s_info.get('saved_by', '')}）。このPCでも読めています。")
elif _s_why:
    st.error(f"⚠️ 保存されていますが、このPCでは読めません：{_s_why}")
else:
    st.warning("まだ保存されていません。")

_sl1, _sl2 = st.columns([3, 1])
with _sl1:
    _new_hook = st.text_input("SlackのWebhook URL", type="password", key="slack_hook_new",
                              placeholder="https://hooks.slack.com/services/…",
                              help="Slack の「Incoming Webhook」で作ったURL。保存すると画面には出しません。")
with _sl2:
    st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)
    if st.button("💾 保存", use_container_width=True, disabled=not _new_hook):
        _ok, _msg = slack_notify.save_shared(_new_hook, who=socket.gethostname(), sb=_sb())
        (st.success if _ok else st.error)(_msg)
        if _ok:
            slack_notify.post(_new_hook, f"🔔 エンカンAIの通知先に登録しました（{socket.gethostname()} から）")
_t1, _t2 = st.columns(2)
with _t1:
    if st.button("📨 テストで1通送る", use_container_width=True, disabled=not _s_url):
        _ok, _err = slack_notify.post(_s_url, f"📨 エンカンAIからのテスト通知です（{socket.gethostname()}）")
        (st.success if _ok else st.error)("送りました。Slackに届いたか確かめてください。" if _ok
                                          else f"送れませんでした：{_err}")
with _t2:
    if _s_info.get("saved"):
        _agree_clear = st.checkbox("保存した送り先を消します（どのPCからも通知しなくなります）", key="slack_clear_ok")
        if st.button("🗑 送り先を消す", use_container_width=True, disabled=not _agree_clear):
            _ok, _msg = slack_notify.clear_shared(sb=_sb())
            (st.success if _ok else st.error)(_msg)

# --- 📣 ほかの送り先（グループ） ---
#     時間指定の予定ごとに「このグループにも送る（完了／うまくいかなかったとき）」を選ぶための送り先。
#     ⚠️ いつもの送り先（上）とは別。こちらを足しても、いつもの送り先への通知は変わらない。
st.markdown("#### 📣 ほかの送り先（グループ）")
st.caption("時間指定の自動実行で、**いつもの送り先に加えて**別のチャンネルにも知らせたいときに使います。"
           "Slack の Webhook は1本につき1チャンネルなので、送りたいチャンネル用に作ったURLを名前をつけて保存します。"
           "どの予定で送るか・完了と失敗のどちらを送るかは「⏰ 時間指定の自動実行」の予定ごとに選びます。")
_ex = slack_notify.extra_info(None, _sb())
for _en, _ei in _ex.items():
    _e1, _e2, _e3 = st.columns([3, 1, 1])
    with _e1:
        st.markdown(f"**{_en}**　<span style='color:gray;font-size:0.85em'>"
                    f"{_ei.get('saved_at', '')}・{_ei.get('saved_by', '')}</span>", unsafe_allow_html=True)
    with _e2:
        if st.button("📨 テスト", key=f"slack_ex_t_{_en}", use_container_width=True):
            _u, _why = slack_notify.extra_url(_en, None, _sb())
            _ok, _err = (slack_notify.post(_u, f"📨 エンカンAIからのテスト通知です（{_en}・{socket.gethostname()}）")
                         if _u else (False, _why))
            (st.success if _ok else st.error)("送りました。Slackに届いたか確かめてください。" if _ok
                                              else f"送れませんでした：{_err}")
    with _e3:
        if st.button("🗑 消す", key=f"slack_ex_d_{_en}", use_container_width=True):
            _ok, _msg = slack_notify.clear_extra(_en, sb=_sb())
            (st.success if _ok else st.error)(_msg)
            if _ok:
                st.rerun()
_x1, _x2, _x3 = st.columns([2, 3, 1])
with _x1:
    _ex_name = st.text_input("送り先の名前", key="slack_ex_name", placeholder="例：TSグループ")
with _x2:
    _ex_hook = st.text_input("そのチャンネルのWebhook URL", type="password", key="slack_ex_hook",
                             placeholder="https://hooks.slack.com/services/…",
                             help="同じ名前で保存すると差し替えます。保存すると画面には出しません。")
with _x3:
    st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)
    if st.button("＋ 足す", use_container_width=True, disabled=not (_ex_name and _ex_hook)):
        _ok, _msg = slack_notify.save_extra(_ex_name, _ex_hook, who=socket.gethostname(), sb=_sb())
        (st.success if _ok else st.error)(_msg)
        if _ok:
            slack_notify.post(_ex_hook, f"🔔 エンカンAIの通知先「{_ex_name.strip()}」に登録しました（{socket.gethostname()} から）")

st.divider()

# --- 💾 このアプリをPCに入れる（フォルダごとダウンロード） ---
#     録画・エントリー実行はブラウザを開くため、担当者のPCで動かす必要がある。
#     そのためのフォルダを、アプリ自身がZIPにして配る（＝いま動いている最新版がそのまま手に入る）。
st.markdown("### 💾 このアプリをPCに入れる")
st.caption("録画・お試し実行・エントリー実行は、担当者のPCで動かす必要があります（ブラウザを開くため）。"
           "下のボタンで、**いま動いているこのアプリ一式**をダウンロードできます。")

_EXCLUDE_DIRS = {".git", "venv", "__pycache__", "artifacts", ".enkan_profile", ".streamlit_cache",
                 # 進捗ファイル（実在の顧客情報）。配る物に混ぜない
                 "取り込みファイル"}
_EXCLUDE_FILES = {"secrets.toml", ".setup_done"}

@st.cache_data(show_spinner=False, ttl=300)
def _build_zip() -> bytes:
    """アプリ一式をZIPにする。接続キー（secrets.toml）は絶対に入れない。"""
    import io, os, zipfile
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".venv")]
            for f in files:
                if f in _EXCLUDE_FILES or f.endswith((".pyc", ".log")):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, base)
                try:
                    zf.write(full, os.path.join("ENKAN_APP", rel))
                except Exception:
                    pass
    return buf.getvalue()

_z1, _z2 = st.columns([1, 2])
with _z1:
    if st.button("📦 ZIPを作る", use_container_width=True):
        st.session_state["_app_zip"] = _build_zip()
with _z2:
    if st.session_state.get("_app_zip"):
        st.download_button("⬇️ ダウンロード（ENKAN_APP.zip）", data=st.session_state["_app_zip"],
                           file_name="ENKAN_APP.zip", mime="application/zip",
                           use_container_width=True)
        st.caption(f"サイズ：約 {len(st.session_state['_app_zip']) // 1024} KB")

with st.expander("📖 ダウンロードしたあとの手順"):
    st.markdown("""
1. ZIPを展開して、好きな場所（デスクトップなど）に置く
2. `.streamlit/secrets.toml.example` をコピーして **`secrets.toml`** にリネーム
3. その中に接続キーを記入（管理者から安全な方法で受け取ってください）
4. **`start.bat`**（Macは `start.command`）をダブルクリック
   - 初回は必要な部品の導入に5〜10分かかります

**⚠️ 接続キー（secrets.toml）はZIPに入っていません。** 機密情報なので、別途受け渡してください。

**次回以降の更新**：`update.bat` をダブルクリックすると最新になります
（Gitが入っていない場合は、またここからZIPを落として上書きしてください）。
""")

st.divider()

# --- 管理メニュー（今後拡張） ---
st.markdown("### 🧰 管理メニュー")
st.info("Slack 通知や、ロボットの一括稼働切替などの管理機能は順次このページに追加していきます。")

g1, g2 = st.columns(2)
with g1:
    st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 ロボットを作る・直す", use_container_width=True)
with g2:
    st.page_link("pages/1_📊_全状況進捗確認.py", label="👀 運用の状況を見る", use_container_width=True)
