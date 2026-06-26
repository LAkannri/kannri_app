import streamlit as st
import characters as ch
import theme

st.set_page_config(page_title="変更・キャンセルの管理（近日公開） - エンカンAI", layout="wide")

# 共有デザインシステム＋サイドバー（管理者を強調）
theme.inject_theme()
theme.brand_sidebar(active="manage")

c = ch.get("manage")
theme.page_header("🛑", "変更・キャンセルの管理を自動化（近日公開）",
                  "申し込み内容の変更や取り消しを、安全に管理・反映する機能を準備中です。",
                  color=c["color"])
ch.guide("manage",
         "ここは近日公開。変更やキャンセルは間違えると大きなトラブルになるから、"
         "わたし（カンナ）が安全のしくみ込みで用意するね。もう少し待っていてね。")

with st.container(border=True):
    theme.section_title("✨", "できるようになる予定のこと")
    st.markdown("""
    - 変更・キャンセル対象を **SFAから自動で抽出**
    - 取り消し漏れ・二重取消を防ぐ **安全チェック**
    - 処理結果を **記録・通知**
    """)

st.info("🚧 この機能はまだ準備中です。いまは『エントリー業務自動化』からロボットを作れます。")

g1, g2 = st.columns(2)
with g1:
    st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 ロボットを作る（ロクすけ）", use_container_width=True)
with g2:
    st.page_link("pages/5_⚙️_その他設定.py", label="⚙️ 設定・管理を見る（カンナ）", use_container_width=True)
