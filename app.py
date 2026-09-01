import sys
import asyncio
# Windowsでブラウザ操作（Playwright）を動かすための設定。
# Python 3.8以降はこれが既定なので、既定と違うときだけ設定する。
# 新しいPythonでは「この書き方は将来なくなります」という警告が出て、
# 起動画面が赤い文字だらけになり、使う人を不安にさせるため。
if sys.platform == "win32":
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            if not isinstance(asyncio.get_event_loop_policy(),
                              asyncio.WindowsProactorEventLoopPolicy):
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

import time

import streamlit as st
from supabase import create_client, Client

import characters as ch
import status
import theme

# 画面の基本設定
st.set_page_config(page_title="エンカンAI - 事務作業の自動化パートナー", layout="wide")

# 共有デザインシステム＋サイドバーのブランド
theme.inject_theme()

# 🔑 接続キーのファイルが壊れていたら、直す場所を名指しして止める。
#    別のPCに入れるときに、コピーし損ねて動かなくなることがあるため。
import secrets_check
secrets_check.check()
theme.brand_sidebar()


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


# ⏱ ホームを開くたびに全部読みに行くと重い。少しのあいだ覚えておく。
@st.cache_data(ttl=60, show_spinner=False)
def _headline():
    return status.headline(status.all_rows(init_connection()))


# --- 入口（玄関）の案内 ---
st.title("🏠 エンカンAI")
st.markdown("#### キャリア申請・進捗・SMS・Salesforce投入まで、事務作業をまるごと自動化するパートナーです。")

with st.chat_message("エンカンAI", avatar="🏠"):
    st.markdown(
        "ようこそ！<br>"
        "**やりたいことを下から選んでください。** はじめての人は「📊 全状況進捗確認」で"
        "いまの様子を見てから選ぶと分かりやすいです。",
        unsafe_allow_html=True,
    )

# ==========================================
# 📌 今日の様子（本物の数字だけを出す）
#    ⚠️ 以前はここに仮の数字（いつも 0 件）が出ていて、
#       動いているのか止まっているのか分からなかった。
# ==========================================
st.write("")
try:
    head = _headline()
except Exception as e:
    head = None
    st.warning(f"いまの様子を読み込めませんでした（{e}）。ページを開き直すか、接続キーを確かめてください。")

if head:
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("動いているロボット", f"{head['稼働中のロボット']} 台",
                  help=f"登録は全部で {head['ロボット合計']} 台です。")
    with m2:
        st.metric("今日動いた工程", f"{head['今日動いた工程']} 件",
                  help="更新・送信・書き出し・取り込みのログが、今日できた数です。")
    with m3:
        st.metric("今日送ったSMS", f"{head['今日送ったSMS']} 件")
    with m4:
        st.metric("今日の証跡", f"{head['今日の証跡']} 枚",
                  help="止まったときに残る画面の写真です。0枚なら、止まった記録はありません。")
    if head["最後に動いた"]:
        st.caption(f"いちばん最後に動いたのは {status.fmt(head['最後に動いた'])}"
                   f"（{status.ago(head['最後に動いた'])}）です。"
                   "／ 数字はこのパソコンでの実行の記録です。")
    else:
        st.caption("このパソコンでの実行の記録は、まだありません。")

st.page_link("pages/1_📊_全状況進捗確認.py", label="📊 くわしい状況を見る（全状況進捗確認）")

st.divider()


def _card(emoji: str, title: str, body: str, page: str = "", label: str = "",
          note: str = ""):
    """ホームの「やること」カード。

    📌 ページのファイル名は番号を振り直すことがある。リンク1つのために
       ホームが真っ白になるのは割に合わないので、失敗しても案内だけ残す。
    """
    with st.container(border=True):
        st.markdown(f"#### {emoji} {title}")
        st.caption(body)
        if note:
            st.caption(note)
        if page:
            try:
                st.page_link(page, label=label or title, use_container_width=True)
            except Exception:
                st.caption("（このページは見つかりませんでした。`update.bat` で最新にすると直ります）")


# ==========================================
# 🗂 毎日の業務
# ==========================================
st.markdown("### 🗂 毎日の業務")
b1, b2 = st.columns(2)
with b1:
    _card("📝", "エントリー業務",
          "SFのレポートを更新して、申請フォームへ入力し、終わった案件をSalesforceへ投入します。"
          "ロボットを作る・直す「司令室」もここです。",
          "pages/2_📝_エントリー業務自動化.py", "📝 エントリー業務自動化へ")
    _card("📱", "SMS送信",
          "パターンごとに、シート更新 → 中身の確認 → CSV作成 → プッシュプロで一括送信まで。"
          "送った宛先は記録して、二重送信を防ぎます。",
          "pages/6_📱_SMS送信.py", "📱 SMS送信へ")
with b2:
    _card("🚀", "進捗反映",
          "各キャリアの進捗ファイルを受け取って、進捗スプレッドシートへ反映します。"
          "確認用シートは、貼り付け先ごとにまとめて件数を見られます。",
          "pages/3_🚀_進捗反映自動化.py", "🚀 進捗反映自動化へ")
    _card("🗃", "データローダー",
          "「このスプシを更新して、この投入をする」をジョブとして登録し、"
          "Salesforceへ API で投入します（Data Loader の代わり）。",
          "pages/7_🗃_データローダー自動化.py", "🗃 データローダー自動化へ")

# ==========================================
# 🛠 見る・ととのえる
# ==========================================
st.markdown("### 🛠 見る・ととのえる")
c1, c2, c3 = st.columns(3)
with c1:
    _card("📊", "全状況進捗確認",
          "いま何が動いていて、どこで止まっているか。実行のログと、止まったときの画面もここで見られます。",
          "pages/1_📊_全状況進捗確認.py", "📊 全状況進捗確認へ")
with c2:
    _card("⚙️", "その他設定",
          "接続キーの確認、共通ロボット（SFコネクタ更新・プッシュプロ送信）の登録、"
          "先にログインしておく操作はここから。",
          "pages/8_⚙️_その他設定.py", "⚙️ その他設定へ")
with c3:
    _card("🧰", "準備中の機能",
          "開通反映・変更キャンセルは、これから作っていくところです。",
          "pages/4_✅_開通反映自動化.py", "✅ 開通反映自動化（準備中）")

st.divider()

# ==========================================
# 👥 案内役
# ==========================================
st.markdown("### 👥 案内役の3人")
st.caption("どの画面にも、担当の案内役がついています。困ったら、その部屋の案内役の言葉を読んでみてください。")
p1, p2, p3 = st.columns(3)
for col, key in zip((p1, p2, p3), ("create", "operate", "manage")):
    c = ch.get(key)
    with col:
        st.markdown(
            f"<div style='background:{c['bg']};border-radius:14px;padding:14px 16px;'>"
            f"<span style='font-size:28px'>{c['avatar']}</span> "
            f"<b style='color:{c['color']}'>{c['name']}</b>"
            f"<span style='color:#64748B;font-size:13px'>（{c['role']}）</span><br>"
            f"<span style='color:#475569;font-size:13px'>{c['tagline']}</span></div>",
            unsafe_allow_html=True,
        )
