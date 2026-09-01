import os
import time

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import characters as ch
import status
import theme

st.set_page_config(page_title="全状況進捗確認 - エンカンAI", layout="wide")

# 共有デザインシステム＋サイドバーのブランド（運用担当を強調）
theme.inject_theme()

# 🔑 接続キーのファイルが壊れていたら、直す場所を名指しして止める。
#    別のPCに入れるときに、コピーし損ねて動かなくなることがあるため。
import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()


# ⏱ 開くたびに Supabase とフォルダを読みに行くと重いので、少しのあいだ覚えておく。
#    「🔄 最新にする」を押せば、すぐに読み直す。
@st.cache_data(ttl=60, show_spinner="いまの様子を集めています…")
def _snapshot():
    rows = status.all_rows(supabase)
    return {
        "見出し": status.headline(rows),
        "ロボット": status.robots(rows),
        "SMS": status.sms_status(rows),
        "データローダー": status.dataloader_status(rows),
        "レポート更新": status.report_status(rows),
        "投入": status.entry_load_status(rows),
        "進捗反映": status.intake_status(),
        "ログ": status.recent_logs(days=7),
        "証跡": status.recent_shots(days=7),
        "読んだ時刻": time.time(),
    }


c = ch.get("operate")
theme.page_header("📊", "全状況進捗確認",
                  "いま何が動いていて、どこで止まっているのかを、この1画面で確かめます。",
                  color=c["color"])

ch.guide("operate",
         "ここは自動化を<b>見守る</b>部屋だよ。下に出ているのは"
         "<b>このパソコンで動かした記録</b>。気になるところから、"
         "そのまま担当のページへ行けるようにしてあるよ。")

_t1, _t2 = st.columns([3, 1])
with _t2:
    if st.button("🔄 最新にする", use_container_width=True):
        _snapshot.clear()
        st.rerun()

snap = _snapshot()
head = snap["見出し"]
with _t1:
    st.caption(f"読み込んだのは {time.strftime('%H:%M:%S', time.localtime(snap['読んだ時刻']))}。"
               "／ スプレッドシートの中身までは読みません（開くのに時間がかかるため）。"
               "中身の確認は、それぞれのページで行ってください。")

# ==========================================
# 📌 今日の様子
# ==========================================
st.markdown("### 📌 今日の様子")
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric("動いているロボット", f"{head['稼働中のロボット']} 台",
              help=f"登録は全部で {head['ロボット合計']} 台です。")
with m2:
    st.metric("今日動いた工程", f"{head['今日動いた工程']} 件",
              help="更新・送信・書き出し・取り込みのログが、今日できた数です。")
with m3:
    st.metric("今日送ったSMS", f"{head['今日送ったSMS']} 件",
              help="送信の記録に、今日の日付で入った宛先の数です。")
with m4:
    st.metric("今日の証跡", f"{head['今日の証跡']} 枚",
              help="止まったときに残る画面の写真です。0枚なら、止まった記録はありません。")

if head["最後に動いた"]:
    st.caption(f"いちばん最後に動いたのは {status.fmt(head['最後に動いた'])}"
               f"（{status.ago(head['最後に動いた'])}）です。")
else:
    st.caption("このパソコンでの実行の記録は、まだありません。")

st.divider()

# ==========================================
# 📱 SMS送信
# ==========================================
st.markdown("### 📱 SMS送信")
if not snap["SMS"]:
    st.info("パターンがまだ登録されていません。")
else:
    _rows = []
    for p in snap["SMS"]:
        for cv in p["CSV"]:
            _rows.append({
                "パターン": p["パターン"],
                "CSVにするシート": cv["シート"],
                "今日のCSV": "✅ あり" if cv["今日のぶん"] else "—",
                "CSVの件数": cv["件数"],
                "CSVを作った": status.fmt(cv["作った時刻"]),
                "今日送った": p["今日送った件数"],
                "送信済み合計": p["送信済み合計"],
                "最後の送信": status.fmt(p["最後の送信"]),
                "確認なしで送る": "⚠️ ON" if p["自動で送る"] else "OFF",
            })
    st.dataframe(pd.DataFrame(_rows), hide_index=True, use_container_width=True)
    st.caption("「今日のCSV」が「—」なら、今日のぶんはまだ作っていません（前の日のCSVは送りません）。"
               "「確認なしで送る」が ON のパターンは、毎回のチェック無しで送信まで進みます。")
st.page_link("pages/6_📱_SMS送信.py", label="📱 SMS送信のページへ")

st.divider()

# ==========================================
# 🗃 データローダー ／ 🔄 レポート更新・投入
# ==========================================
d1, d2 = st.columns(2)
with d1:
    st.markdown("### 🗃 データローダー自動化")
    if snap["データローダー"]:
        st.dataframe(pd.DataFrame([{
            "ジョブ": j["ジョブ"],
            "更新するシート": j["更新するシート"],
            "目で見て確認": j["目で見て確認"],
            "投入": j["投入"],
            "最後の更新": status.fmt(j["最後の更新ログ"]),
            "確認なしで投入": "⚠️ ON" if j["自動で投入"] else "OFF",
        } for j in snap["データローダー"]]), hide_index=True, use_container_width=True)
    else:
        st.info("ジョブがまだ登録されていません。")
    st.page_link("pages/7_🗃_データローダー自動化.py", label="🗃 データローダー自動化のページへ")

with d2:
    st.markdown("### 🔄 レポート更新 ／ 🗃 エントリー後の投入")
    if snap["レポート更新"]:
        st.dataframe(pd.DataFrame([{
            "更新セット": s["セット"],
            "スプシ": s["スプレッドシート"],
            "シート": s["シート合計"],
            "最後の更新": status.fmt(s["最後の更新ログ"]),
        } for s in snap["レポート更新"]]), hide_index=True, use_container_width=True)
    else:
        st.info("更新セットがまだ登録されていません。")
    if snap["投入"]:
        st.dataframe(pd.DataFrame([{
            "投入セット": s["セット"], "スプシ": s["スプレッドシート"], "投入": s["投入"],
        } for s in snap["投入"]]), hide_index=True, use_container_width=True)
    st.page_link("pages/2_📝_エントリー業務自動化.py",
                 label="📝 エントリー業務のホームへ（更新・投入はここから）")

st.divider()

# ==========================================
# 🚀 進捗反映（このPCの取り込み記録）
# ==========================================
st.markdown("### 🚀 進捗反映（取り込みの記録）")
st.caption("キャリアごとに、**このパソコンで最後に取り込んだファイル**です。"
           "貼り付け先スプレッドシートの中身は、進捗反映のページで確認できます。")
if snap["進捗反映"]:
    st.dataframe(pd.DataFrame([{
        "キャリア": r["キャリア"],
        "最後の取り込み": status.fmt(r["最後の取り込み"]),
        "経過": status.ago(r["最後の取り込み"]),
        "受け取ったファイル": r["ファイル"] or "—",
    } for r in snap["進捗反映"]]), hide_index=True, use_container_width=True)
else:
    st.info("取り込みの記録はまだありません。")
st.page_link("pages/3_🚀_進捗反映自動化.py", label="🚀 進捗反映自動化のページへ")

st.divider()

# ==========================================
# 🤖 ロボット
# ==========================================
st.markdown("### 🤖 ロボット")
if snap["ロボット"]:
    st.dataframe(pd.DataFrame([{
        "ロボット": r["名前"],
        "種別": r["種別"],
        "状態": "🟢 稼働中" if r["稼働中"] else "⚪ お休み",
        "手順の数": r["手順数"],
        "送信ステップ": ("—" if r["送信ステップ"] is None
                   else ("✅ あり" if r["送信ステップ"] else "⚠️ なし")),
    } for r in snap["ロボット"]]), hide_index=True, use_container_width=True)
    st.caption("「送信ステップ」が ⚠️ なしのロボットは、本番でも最後の一押し（申請・送信）をしません。"
               "申請まで終わらせたいロボットは、司令室で「🚀 送信ステップを追加」してください。")
else:
    st.info("ロボットがまだ登録されていません。")

st.divider()

# ==========================================
# 🧾 最近の動き
# ==========================================
st.markdown("### 🧾 最近の動き（この1週間）")
if snap["ログ"]:
    for l in snap["ログ"][:12]:
        res = status.log_result(l["ファイル"])
        line = (f"**{status.fmt(l['時刻'])}**（{status.ago(l['時刻'])}）"
                f"｜{l['工程']}｜{l['どこ']}")
        if res.startswith("⚠️"):
            st.warning(f"{line}\n\n{res}")
        elif res:
            st.success(f"{line}\n\n{res}")
        else:
            st.write(line)
        with st.expander("ログの終わりを見る"):
            try:
                with open(l["ファイル"], encoding="utf-8", errors="replace") as f:
                    st.code(f.read()[-4000:], language=None)
            except Exception as e:
                st.caption(f"読めませんでした：{e}")
else:
    st.info("この1週間の実行の記録はありません。")

st.divider()

# ==========================================
# 🖼 止まったときの画面
# ==========================================
st.markdown("### 🖼 止まったときの画面（証跡）")
st.caption("ロボットが止まったときに残る画面の写真です。何が出ていたのかを、そのまま見られます。")
if snap["証跡"]:
    cols = st.columns(3)
    for i, s in enumerate(snap["証跡"][:9]):
        with cols[i % 3]:
            st.markdown(f"**{s['ロボット']}**  \n{status.fmt(s['時刻'])}｜{s['できごと']}")
            try:
                st.image(s["ファイル"], use_container_width=True)
            except Exception:
                st.caption(os.path.basename(s["ファイル"]))
else:
    st.success("この1週間、止まった記録はありません。")

st.divider()

# ==========================================
# 🆘 困ったら
# ==========================================
st.markdown("### 🆘 困ったら")
g1, g2, g3 = st.columns(3)
with g1:
    st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 手順を直す（司令室）",
                 use_container_width=True)
with g2:
    st.page_link("pages/8_⚙️_その他設定.py", label="⚙️ 設定・共通ロボットの登録",
                 use_container_width=True)
with g3:
    st.page_link("app.py", label="🏠 ホームへもどる", use_container_width=True)
