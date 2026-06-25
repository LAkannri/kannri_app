import streamlit as st
import characters as ch
import theme

st.set_page_config(page_title="開通進捗の反映（近日公開） - エンカンAI", layout="wide")

# 共有デザインシステム＋サイドバー（運用担当を強調）
theme.inject_theme()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🚀", "開通進捗の反映を自動化（近日公開）",
                  "キャリアの開通状況をSFAスプレッドシートへ自動で反映する機能を準備中です。",
                  color=c["color"])
ch.guide("operate",
         "ここは近日公開だよ！開通したかどうかをわたしが毎日チェックして、"
         "スプシに自動で反映できるようにする予定。完成したらここでお知らせするね。")

with st.container(border=True):
    theme.section_title("✨", "できるようになる予定のこと")
    st.markdown("""
    - キャリア各社の **開通状況を自動で確認**
    - SFAスプシの「開通状況」列を **自動で更新**
    - 開通したら **Slackでお知らせ**
    """)

st.info("🚧 この機能はまだ準備中です。いまは『エントリー業務自動化』からロボットを作れます。")

g1, g2 = st.columns(2)
with g1:
    st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 ロボットを作る（ロクすけ）", use_container_width=True)
with g2:
    st.page_link("pages/1_📊_全状況進捗確認.py", label="👀 運用の状況を見る（ミハリ）", use_container_width=True)
