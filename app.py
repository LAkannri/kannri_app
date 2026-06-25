import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
import characters as ch
import theme

# 画面の基本設定
st.set_page_config(page_title="エンカンAI - 事務作業の自動化パートナー", layout="wide")

# 共有デザインシステム＋サイドバーのブランド
theme.inject_theme()
theme.brand_sidebar()

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
