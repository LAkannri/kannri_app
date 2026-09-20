"""
📣 イレギュラー報告（イレギュラー対応の報告を、1件ずつ書いて「報告」シートに足す）

【困っていたこと】
  - スプシの「イレギュラー対応　レポート提出待ち」を見ながら、備考から必要なところだけ
    コピーして、別のシートに手で並べていた（行がずれる・書き忘れる）。

【この画面の考え方】
  ⭐ **1件ずつ出す**。その案件の備考をそのまま出し（コピーボタン付き）、
     商材・商品名・イレギュラー内容の3つを書いて「報告に足す」だけ。
  ⭐ **報告シートには足すだけ**（`append_row`）。前に書いた分は触らない。
  ⭐ **AIは書き換えの道具**（任意）。ばーっと書いた文を、分かりやすい報告文に整える。
     ⚠️ 勝手に置き換えない：元の文は残して「↩ 元に戻す」で戻せるようにする。
     ⚠️ AIに送るのはその欄の文だけ。押したときだけ送る（黙って送らない）。

⚠️ 判定や整形をここに増やさない。書くのは人。
"""
import json
import time

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import auto_jobs
import characters as ch
import theme

st.set_page_config(page_title="イレギュラー報告 - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📣", "イレギュラー報告",
                  "イレギュラー対応のあった案件を1件ずつ見て、報告シートに足します。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

SETTINGS_ID = "__irregular__"
DEFAULT_WAIT_TAB = "イレギュラー対応　レポート提出待ち"
DEFAULT_REPORT_TAB = "報告"
# 報告シートの見出し（A/B/C）。スプシ側と同じにしておくこと。
REPORT_HEADERS = ["商材", "商品名", "イレギュラー内容"]
PRODUCTS = ["ネット", "電気", "ガス", "電気＆ガス", "その他"]
# 案件を見分ける列（この値で「もう報告した」を覚える）
ID_COL = "案件 ID"


def _load():
    try:
        res = supabase.table("merchants").select("config_json").eq("id", SETTINGS_ID).execute()
        return (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
        return {}


def _save(cfg):
    # ⚠️ 書く直前に読み直して、触ったところだけ変える（別のPCで足した分を消さないため）
    latest = _load()
    latest.update(cfg)
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（イレギュラー報告の設定）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return latest


@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)


def _get_gspread_client():
    sa_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    try:
        return _build_gspread_client(sa_json)
    except Exception:
        return None


def _open(gc, url: str):
    return gc.open_by_url(url) if str(url).startswith("http") else gc.open_by_key(url)


@st.cache_data(ttl=60, show_spinner=False)
def _read_rows(_gc, url: str, tab: str):
    """待ちシートを (見出し, 行) で読む。⚠️ 60秒だけ覚える（開くたびに待たせないため）。"""
    ws = _open(_gc, url).worksheet(tab)
    vals = ws.get_all_values()
    if not vals:
        return [], []
    head = [str(h).strip() for h in vals[0]]
    rows = [dict(zip(head, (r + [""] * len(head))[:len(head)])) for r in vals[1:]
            if any(str(x).strip() for x in r)]
    return head, rows


def _append_report(gc, url: str, tab: str, product: str, name: str, body: str):
    """報告シートに1行足す。

    ⚠️ 足すだけ（前に書いた分は触らない）。
    ⚠️ RAW で書く（スプシに解釈させない。先頭の0や `=` で始まる文が化けないように）。
    """
    ws = _open(gc, url).worksheet(tab)
    ws.append_row([product, name, body], value_input_option="RAW")


def _ai_summary(text: str) -> str:
    """ばーっと書いた文を、報告として読みやすい文に整える（押したときだけ送る）。"""
    import google.generativeai as genai
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "あなたは事務の報告文を整える担当です。次の下書きを、上長が読んで分かる"
        "イレギュラー対応の報告文に書き直してください。\n"
        "・日本語。箇条書きにせず、2〜4文の文章。\n"
        "・**書かれていないことを足さない**（推測・原因の決めつけ・対応方針の創作をしない）。\n"
        "・お客様名・電話番号などが下書きにあれば、そのまま残す。\n"
        "・「何が起きたか → どう対応したか → いまどうなっているか」の順で書く。\n"
        "・前置き（了解しました等）や見出しを付けず、報告文だけを返す。\n\n"
        f"【下書き】\n{text}")
    return (model.generate_content(prompt).text or "").strip()


# ==========================================
# ⚙️ 設定（どのスプシの、どのシートを見るか）
# ==========================================
cfg = _load()
gc = _get_gspread_client()
if not gc:
    st.error("🔑 `GOOGLE_SERVICE_ACCOUNT_JSON` が未設定です。"
             "「⚙️ その他設定」で接続キーを確かめてください。")
    st.stop()

with st.expander("⚙️ 設定（スプレッドシートとシート名）", expanded=not cfg.get("sheet_url")):
    _url = st.text_input("スプレッドシートのURL", value=str(cfg.get("sheet_url", "") or ""),
                         key="irr_url",
                         help="「イレギュラー対応　レポート提出待ち」と「報告」が入っているスプシ")
    _wait = st.text_input("見る側のシート（イレギュラーのあった案件）",
                          value=str(cfg.get("wait_tab", "") or DEFAULT_WAIT_TAB), key="irr_wait")
    _rep = st.text_input("書き足す側のシート（報告）",
                         value=str(cfg.get("report_tab", "") or DEFAULT_REPORT_TAB), key="irr_rep")
    if st.button("💾 保存", type="primary", key="irr_save"):
        cfg = _save({"sheet_url": _url.strip(), "wait_tab": _wait.strip(),
                     "report_tab": _rep.strip()})
        st.cache_data.clear()
        st.success("保存しました。")
        st.rerun()
    st.caption("⚠️ このスプシを、サービスアカウントに**編集者**として共有しておいてください"
               "（報告シートに書き足すため）。")

url = str(cfg.get("sheet_url", "") or "").strip()
wait_tab = str(cfg.get("wait_tab", "") or DEFAULT_WAIT_TAB).strip()
report_tab = str(cfg.get("report_tab", "") or DEFAULT_REPORT_TAB).strip()
if not url:
    st.info("上の「⚙️ 設定」でスプレッドシートのURLを入れてください。")
    st.stop()

# ==========================================
# 📋 1件ずつ、報告を書く
# ==========================================
try:
    head, rows = _read_rows(gc, url, wait_tab)
except Exception as e:
    st.error(f"シート「{wait_tab}」を読めませんでした: {str(e)[:200]}")
    st.stop()

done = list(cfg.get("done") or [])      # もう報告した案件（この画面で覚えておく）

# 🔄 SFコネクタで最新にしてから出す。
#    ⚠️ 更新前の中身で報告を書くと、**昨日のイレギュラー**を見ていることになる。
#    朝は「⏰ 時間指定の自動実行」が同じ処理（auto_jobs.run_irregular）を走らせ、
#    1件でもあればSlackで知らせる。ここは、そのあと人が押し直したいとき用。
_last = str(cfg.get("last_refresh", "") or "")
_today = time.strftime("%Y/%m/%d")
if _last.startswith(_today):
    st.success(f"🔄 きょう {_last} に更新しています。")
else:
    st.warning(f"🔄 **きょうはまだ更新していません**（最終更新：{_last or 'まだありません'}）。"
               "下の「🔄 SFコネクタで更新する」を押すか、朝の自動実行を待ってください。")

c1, c2, c3 = st.columns([2, 1, 2])
with c1:
    if st.button("🔄 SFコネクタで更新する", key="irr_refresh",
                 help="ブラウザが開いて、待ちシートを最新にします（数分かかります）"):
        with st.spinner("🤖 シートを更新しています..."):
            res = auto_jobs.run_irregular(supabase, gc, cfg, notify=False)
        for stp in res["工程"]:
            st.markdown(f"- {stp['結果']} **{stp['工程']}**：{stp['中身']}")
        st.cache_data.clear()
        cfg = _load()
        if res["結果"] == "完了":
            st.rerun()
with c2:
    if st.button("🔄 読み直す", key="irr_reload"):
        st.cache_data.clear()
        st.rerun()
with c3:
    show_done = st.checkbox("✅ 報告済みの案件も出す", value=False, key="irr_showdone")

if not rows:
    st.success(f"📭 いま「{wait_tab}」に案件はありません（2行目以降が空です）。")
    st.stop()

targets = [r for r in rows if show_done or str(r.get(ID_COL, "")).strip() not in done]
st.caption(f"全 {len(rows)}件／このあと報告するもの "
           f"{len([r for r in rows if str(r.get(ID_COL, '')).strip() not in done])}件")

# 備考として出す列（案件IDと個人名以外＝備考のたぐい）を、シートの見出しから決める。
# ⚠️ 列名を決め打ちしない（レポートの列が増えても、そのまま出せるように）。
note_cols = [h for h in head if h and h not in (ID_COL, "個人名")]

for r in targets:
    cid = str(r.get(ID_COL, "") or "").strip()
    who = str(r.get("個人名", "") or "").strip()
    is_done = cid in done
    with st.expander(f"{'✅ ' if is_done else ''}{who or '（名前なし）'}　{cid}",
                     expanded=not is_done and len(targets) <= 5):
        # --- 備考（コピーして使う） ---
        st.caption("📋 右上のコピーボタンで、必要なところをコピーできます。")
        for col in note_cols:
            v = str(r.get(col, "") or "").strip()
            if v:
                st.markdown(f"**{col}**")
                st.code(v, language=None)
        if not any(str(r.get(col, "") or "").strip() for col in note_cols):
            st.caption("（備考は空です）")

        st.markdown("---")
        k = f"irr_{cid or who}"
        a, b = st.columns([1, 2])
        with a:
            product = st.selectbox("商材", PRODUCTS, key=f"{k}_p")
        with b:
            pname = st.text_input("商品名（空でもOK）", key=f"{k}_n",
                                  placeholder="例：ドコモ光／東京ガス")

        # ⚠️ 入力欄の中身は、こちら（`_v`）で持つ。widget のキーに直接入れ直すと
        #    「表示したあとに書き換えた」と言われて落ちるので、**キーに版（`_ver`）を付けて**
        #    作り直す（AIで整えた文・元に戻した文を、その場で欄に出すため）。
        ver = int(st.session_state.get(f"{k}_ver", 0))
        body = st.text_area("イレギュラー内容", value=st.session_state.get(f"{k}_v", ""),
                            key=f"{k}_b{ver}", height=160,
                            placeholder="備考から貼り付けたり、思いつくまま書いてOKです。"
                                        "あとから「✨ AIで整える」で読みやすくできます。")
        st.session_state[f"{k}_v"] = body

        x, y, z = st.columns([1, 1, 2])
        with x:
            if st.button("✨ AIで整える", key=f"{k}_ai", disabled=not body.strip(),
                         help="いま書いた文だけをAIに送って、報告文に整えます"):
                try:
                    with st.spinner("🤖 整えています..."):
                        out = _ai_summary(body)
                    if out:
                        # ⚠️ 元の文は残す（気に入らなければ戻せるように）
                        st.session_state[f"{k}_back"] = body
                        st.session_state[f"{k}_v"] = out
                        st.session_state[f"{k}_ver"] = ver + 1
                        st.rerun()
                    st.warning("AIから返事がありませんでした。そのままでも報告できます。")
                except Exception as e:
                    st.error(f"AIを呼べませんでした: {str(e)[:200]}")
        with y:
            if st.session_state.get(f"{k}_back") and st.button("↩ 元に戻す", key=f"{k}_undo"):
                st.session_state[f"{k}_v"] = st.session_state.pop(f"{k}_back")
                st.session_state[f"{k}_ver"] = ver + 1
                st.rerun()
        with z:
            st.caption("✨ を押したときだけ、この欄の文がAI（Gemini）に送られます。")

        if st.button("📤 報告に足す", type="primary", key=f"{k}_go", disabled=not body.strip()):
            try:
                _append_report(gc, url, report_tab, product, pname.strip(), body.strip())
            except Exception as e:
                st.error(f"「{report_tab}」に書けませんでした: {str(e)[:200]}")
            else:
                if cid and cid not in done:
                    done.append(cid)
                    cfg = _save({"done": done[-2000:]})     # 古いものから捨てる
                st.session_state[f"{k}_v"] = ""          # 次に開いたとき空から書けるように
                st.session_state[f"{k}_ver"] = ver + 1
                st.session_state.pop(f"{k}_back", None)
                st.success(f"✅ 「{report_tab}」に足しました。")
                st.rerun()

# ==========================================
# 📄 いまの報告シート（足した分が並んでいるか、ここで確かめられる）
# ==========================================
with st.expander(f"📄 「{report_tab}」のいまの中身を見る"):
    if st.button("🔄 読み込む", key="irr_repread"):
        st.cache_data.clear()
    try:
        _h, _r = _read_rows(gc, url, report_tab)
        if _r:
            st.dataframe(pd.DataFrame(_r), use_container_width=True, hide_index=True)
            st.caption(f"{len(_r)}件")
        else:
            st.caption("まだ1件もありません。")
    except Exception as e:
        st.error(f"読めませんでした: {str(e)[:200]}")
