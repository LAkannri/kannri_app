"""
🎧 通録ダウンロード

⚠️ **まだ枠だけです。中身はこれから組みます。**

タブだけ先に置いてあるのは、サイドバーの並びと入口を先に決めておくため。
作るときは、このファイルの中身を書き替えるだけでよく、他の画面には触らない。

【作るときの下ごしらえ（他のタブで既にあるもの）】
  - サイトを開いて落とす      … 共通ロボット＋`robot.py --run`（録画1台で使い回す）
  - 落としたファイルの置き場  … `sms_runner.work_dir("通録ダウンロード", <名前>)`
  - 目で見て確認して消し込む  … `watch_ui.render(...)`
  - スプシのGASを叩く         … `sms_runner.run_gas_action(...)`
  - Salesforceへ入れる        … `sf_ui.load_editor` / `sf_ui.push_sheet`
  設定の置き場は Supabase の予約行（例 `__callrec__`）。他のタブと同じ形にする。
"""
import streamlit as st

import characters as ch
import theme

st.set_page_config(page_title="通録ダウンロード - エンカンAI", layout="wide")

theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🎧", "通録ダウンロード",
                  "通話録音のダウンロードをまとめる場所です。",
                  color=c["color"])

ch.guide("operate",
         "ここは<b>これから作るところ</b>。入口だけ先に置いてあるよ。"
         "何をどう落としたいかが決まったら、ここに組み込むね。")

with st.container(border=True):
    theme.section_title("🚧", "準備中です")
    st.info("**このタブは、まだ中身がありません。**入口だけ先に用意してあります。")
    st.markdown("""
決まっていないのは、たとえばこのあたりです。作るときに教えてください。

- **どこから落とすか**（サイトの画面／管理システムの一覧 など）
- **何を目印に選ぶか**（日付・案件・担当者 など）
- **落としたあと何をするか**（フォルダに貯めるだけ／スプシに記録／Salesforceに紐づける）
- **どれくらいの頻度で、何件くらい**落とすか
""")
    st.caption("💡 サイトを開いて落とす部分は、**録画1台**で作れます"
               "（「⚙️ その他設定」の共通ロボットと同じやり方）。"
               "日付が毎回変わるファイルは、手順の『対象』に "
               "`最新のファイル` と書けば、いちばん新しいものを押せます。")

st.divider()
st.page_link("pages/99_⚙️_その他設定.py", label="⚙️ 設定・共通ロボットの登録へ",
             use_container_width=True)
