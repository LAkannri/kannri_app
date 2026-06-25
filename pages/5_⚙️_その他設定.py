import streamlit as st
import characters as ch
import theme

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

# --- 管理メニュー（今後拡張） ---
st.markdown("### 🧰 管理メニュー")
st.info("Slack 通知や、ロボットの一括稼働切替などの管理機能は順次このページに追加していきます。")

g1, g2 = st.columns(2)
with g1:
    st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 ロボットを作る・直す", use_container_width=True)
with g2:
    st.page_link("pages/1_📊_全状況進捗確認.py", label="👀 運用の状況を見る", use_container_width=True)
