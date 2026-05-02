import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st

# 画面の基本設定（絵文字は最小限に）
st.set_page_config(page_title="自動化統括プラットフォーム", layout="wide")

# ★モダンUIを実現するカスタムCSS
st.markdown("""
<style>
    /* 全体のフォントと背景をクリーンに */
    .stApp {
        background-color: #FAFAFA;
    }
    
    /* ボタンの滑らかなアニメーションと影 */
    .stButton>button {
        border-radius: 6px;
        border: 1px solid #E0E0E0;
        background-color: #FFFFFF;
        box-shadow: 0 2px 4px rgba(0,0,0,0.02);
        transition: all 0.3s ease;
        font-weight: 500;
    }
    .stButton>button:hover {
        box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        transform: translateY(-1px);
        border-color: #CCCCCC;
    }
    
    /* プルダウンや入力欄のフォーカス時のハイライト */
    .stSelectbox div[data-baseweb="select"] {
        border-radius: 6px;
        transition: all 0.3s ease;
    }
    .stSelectbox div[data-baseweb="select"]:hover {
        box-shadow: 0 2px 8px rgba(0,0,0,0.05);
    }
    
    /* ヘッダー周りの余白調整 */
    h1, h2, h3 {
        color: #333333;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)

st.title("Automation Control Center")
st.markdown("左側のサイドバーから、実行したい業務メニューを選択してください。")

# トップページ用の簡単なサマリーパネル（モックアップ）
col1, col2, col3 = st.columns(3)
with col1:
    st.info("本日の自動化処理件数\n### 0 件")
with col2:
    st.success("稼働中のプロジェクト\n### 1 件")
with col3:
    st.error("要確認エラー\n### 0 件")