import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
import characters as ch

# 画面の基本設定
st.set_page_config(page_title="エンカンAI - 事務作業の自動化パートナー", layout="wide")

# ★やさしいUIのためのカスタムCSS（丸みフォント＋クリーンな背景）
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=M+PLUS+Rounded+1c:wght@400;500;700&display=swap');
    html, body, [class*="css"] {
        font-family: 'M PLUS Rounded 1c', sans-serif !important;
        color: #333333 !important;
    }
    .stApp { background-color: #FFFFFF; }
    h1, h2, h3 { color: #333333; font-weight: 700; }
    /* ページ移動リンクを大きめのボタン風に */
    div[data-testid="stPageLink"] a {
        border-radius: 12px;
        border: 2px solid #E5E7EB;
        padding: 10px 14px;
        font-weight: 700;
        justify-content: center;
        transition: all 0.2s ease;
    }
    div[data-testid="stPageLink"] a:hover {
        background-color: #F9FAFB;
        transform: translateY(-1px);
    }
</style>
""", unsafe_allow_html=True)

# --- 入口（玄関）の案内 ---
st.title("エンカンAI")
st.markdown("#### キャリア申請（電気・ガス・ネット）の事務作業を、まるごと自動化するパートナーです。")

# 受付役からのひとこと
with st.chat_message("エンカンAI", avatar="🏠"):
    st.markdown(
        "ようこそ！やりたいことに合わせて、**3人の担当**が案内します。<br>"
        "下のカードから、今日のあなたの役割を選んでください。",
        unsafe_allow_html=True,
    )

st.write("")

# --- 3人の担当キャラ（ロール選択） ---
col1, col2, col3 = st.columns(3)
with col1:
    ch.role_card("create")    # 録画担当：自動化をつくる
with col2:
    ch.role_card("operate")   # 運用担当：自動化を見守る
with col3:
    ch.role_card("manage")    # 管理者：全体を管理する

st.divider()

# --- 全体サマリー（ミハリの見守りダイジェスト：現状はモック表示） ---
st.markdown("#### 👀 ミハリの見守りダイジェスト")
st.caption("※ 下の数値はまだ仮表示です。実際の自動実行の結果がここに反映されるようにしていきます。")
m1, m2, m3 = st.columns(3)
with m1:
    st.info("本日の自動化処理件数\n### 0 件")
with m2:
    st.success("稼働中のロボット\n### 1 台")
with m3:
    st.error("要確認エラー\n### 0 件")
