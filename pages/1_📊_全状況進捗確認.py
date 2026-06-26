import streamlit as st
import characters as ch
import theme

st.set_page_config(page_title="自動化を見守る - エンカンAI", layout="wide")

# 共有デザインシステム＋サイドバーのブランド（運用担当を強調）
theme.inject_theme()
theme.brand_sidebar(active="operate")

# --- 👀 ミハリ（運用担当）の見守り部屋 ---
ch.hero("operate", subtitle="毎日の自動申請がちゃんと動いたか、ここで見守ります。")

ch.guide("operate",
         "おつかれさま！ここは自動化を<b>見守る</b>部屋だよ。"
         "今日の処理結果や、止まってしまった案件がないかを一緒に確認しよう。")

st.write("")

# --- 今日の状況（現状はモック表示） ---
st.markdown("### 📊 今日の見守りダイジェスト")
st.caption("※ 下の数値はまだ仮表示です。クラウド自動実行（GitHub Actions）の結果を取り込めるようにしていきます。")
c1, c2, c3 = st.columns(3)
with c1:
    st.success("処理できた件数\n### 0 件")
with c2:
    st.warning("これから処理する件数\n### 0 件")
with c3:
    st.error("止まってしまった件数\n### 0 件")

st.divider()

# --- ミハリの毎日チェックリスト ---
st.markdown("### ✅ ミハリの毎日チェック")
st.markdown("""
- **① 申請フォームでの自動実行（クラウド）** … 毎朝 8:00（JST）に自動で動きます。
- **② 止まった案件はないか** … 入力欄が見つからない等で止まると、証跡スクショが残ります。
- **③ あやしい壁（CAPTCHA）に当たっていないか** … 当たったら安全のため送信せず中止します。
""")

with st.expander("🧪 「本番実行」と「ドライラン（おためし）」のちがい"):
    st.markdown("""
- 既定（毎朝の自動実行）は **ドライラン**：対象の件数を確認するだけで、実際の申請はしません。
- 本当に申請させたいときは、管理者に **手動で本番実行（live）** をお願いしてください。
- これは「二重申請」や「うっかり誤申請」を防ぐための安全のしくみです。
""")

st.divider()

# --- 困ったときの導線 ---
st.markdown("### 🆘 困ったら")
g1, g2 = st.columns(2)
with g1:
    st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 手順を直す（ロクすけの部屋へ）", use_container_width=True)
with g2:
    st.page_link("pages/5_⚙️_その他設定.py", label="⚙️ 設定を確認する（カンナの部屋へ）", use_container_width=True)
