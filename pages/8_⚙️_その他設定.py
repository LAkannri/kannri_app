import streamlit as st
import characters as ch
import common_robots
import theme
from supabase import create_client, Client

st.set_page_config(page_title="全体を管理する - エンカンAI", layout="wide")

# 共有デザインシステム＋サイドバーのブランド（管理者を強調）
theme.inject_theme()
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
}
cols = st.columns(len(KEY_LABELS))
for col, (key, label) in zip(cols, KEY_LABELS.items()):
    with col:
        ok = False
        try:
            ok = bool(st.secrets.get(key))
        except Exception:
            ok = False
        if ok:
            st.success(f"✅ {label}\n設定済み")
        else:
            st.error(f"⚠️ {label}\n未設定")
st.caption("※ クラウド実行では GitHub の Secrets（Settings → Secrets and variables → Actions）に同じ3つを登録します。")

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
