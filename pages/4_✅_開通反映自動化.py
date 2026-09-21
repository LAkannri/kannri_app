import streamlit as st
import json
import characters as ch
import theme
import sf_ui
from supabase import create_client, Client

st.set_page_config(page_title="開通反映の自動化 - エンカンAI", layout="wide")

theme.inject_theme()

# 🔑 接続キーのファイルが壊れていたら、直す場所を名指しして止める。
#    別のPCに入れるときに、コピーし損ねて動かなくなることがあるため。
import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("✅", "開通反映を自動化",
                  "開通した案件をSalesforceへ反映します（ガスID・電力IDなどで照合）。",
                  color=c["color"])

# ==========================================
# 🔌 接続（他のページと同じ鍵を使う）
# ==========================================
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase: Client = init_connection()

# 開通反映の設定は、ロボット一覧に出さない予約行に保存する（進捗反映とは別の行）
SETTINGS_ID = "__kaitsuu__"

def _load_settings():
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}

def _save_settings(cfg):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（開通反映の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cfg}).execute()

@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)

def _get_gspread_client():
    try:
        sa_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        sa_json = ""
    if not sa_json:
        return None
    try:
        return _build_gspread_client(sa_json)
    except Exception:
        return None

ch.guide("operate",
         "ここは開通の反映だよ。進捗反映とは別に、開通したかどうかをSalesforceへ入れる場所。"
         "ガスIDや電力IDで照合するときもここで設定できるよ。")

cfg = _load_settings()
gc = _get_gspread_client()


@st.cache_data(ttl=120, show_spinner=False)
def _tab_names(_gc, url: str):
    sh = _gc.open_by_url(url) if url.startswith("http") else _gc.open_by_key(url)
    return [w.title for w in sh.worksheets()]


# ⭐ 投入の設定は、どの画面でも sf_ui.load_editor 1つ（前はこのページだけ設定スプシの別の表で持っていた）。
#    設定は予約行 __kaitsuu__ の config_json.loads（1件＝{"url", "シート", "オブジェクト", "照合キー", "マッピング", …}）。
with st.container(border=True):
    theme.section_title("☁️", "Salesforceに投入する")
    st.caption("開通の反映では、案件ID（Id）だけでなく **ガスID・電力ID** で照合することもあります。"
               "照合キーに外部ID（`GasID__c` / `Powercustomernumber__c` など）を選んでください。"
               "設定の中身は、データローダー・進捗反映と同じです。")
    if not gc:
        st.error("接続キー GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        st.stop()
    import uuid
    if "kt_loads" not in st.session_state:
        st.session_state["kt_loads"] = [dict(x) for x in (cfg.get("loads") or [])]
    loads = st.session_state["kt_loads"]
    for ld in loads:
        ld.setdefault("_uid", uuid.uuid4().hex[:10])
    for i, ld in enumerate(loads):
        uid = ld["_uid"]
        with st.container(border=True):
            head, summ = st.empty(), st.empty()
            with st.expander("⚙️ 設定"):
                ld["名前"] = st.text_input("この投入の名前", value=str(ld.get("名前", "") or ""),
                                           placeholder="例：ガス開通", key=f"kt_name_{uid}")
                ld["url"] = st.text_input("スプレッドシートのURL", value=str(ld.get("url", "") or ""),
                                          key=f"kt_url_{uid}",
                                          placeholder="https://docs.google.com/spreadsheets/d/.../edit").strip()
                tabs = []
                if ld["url"]:
                    try:
                        tabs = _tab_names(gc, ld["url"])
                    except Exception as e:
                        st.warning(f"このスプレッドシートを開けませんでした（共有を確かめてください）：{str(e)[:120]}")
                sf_ui.load_editor(gc, ld["url"], tabs, ld, key=f"kt_{uid}")
                if st.checkbox("🗑 この投入を消す（保存で確定します）", key=f"kt_del_{uid}"):
                    ld["_del"] = True
                else:
                    ld.pop("_del", None)
            head.markdown(f"**{i + 1}. {ld.get('名前') or '（名前なし）'}**　（シート：{ld.get('シート') or '未選択'}）")
            summ.caption(sf_ui.load_summary(ld, n_map=len(ld.get("マッピング") or {})))
            if ld.get("url") and ld.get("シート") and ld.get("マッピング"):
                c1, c2, c3 = st.columns([1, 1, 2])
                n_try = c1.number_input("お試しの件数", 1, 100, 3, key=f"kt_n_{uid}")
                ok = c3.checkbox("全件を投入します（取り消せません）", key=f"kt_ok_{uid}")
                go_try = c2.button("🧪 お試し", key=f"kt_try_{uid}")
                go_all = c3.button("🚀 全件投入", key=f"kt_all_{uid}", disabled=not ok, type="primary")
                if go_try or go_all:
                    with st.spinner("投入しています..."):
                        r = sf_ui.push_sheet(gc, ld["url"], ld["シート"], ld["オブジェクト"], ld["照合キー"],
                                             ld["マッピング"], limit=int(n_try) if go_try else 0,
                                             send_blanks=bool(ld.get("空も送る", False)),
                                             no_overwrite=sf_ui.sfl.no_overwrite(ld),
                                             overwrite_if=sf_ui.sfl.overwrite_if(ld))
                    st.session_state[f"kt_res_{uid}"] = r
                r = st.session_state.get(f"kt_res_{uid}")
                if r:
                    st.markdown(r.get("結果", ""))
                    if r.get("errors"):
                        sf_ui.render_errors(r["errors"], ld["オブジェクト"], key_prefix=f"kt_err_{uid}")
    b1, b2 = st.columns(2)
    if b1.button("➕ 投入を足す", key="kt_add"):
        loads.append({"url": "", "シート": "", "オブジェクト": "Opportunity", "照合キー": "Id",
                      "マッピング": {}, "_uid": uuid.uuid4().hex[:10]})
        st.rerun()
    if b2.button("💾 投入の設定を保存", key="kt_save", type="primary"):
        keep = [ld for ld in loads if not ld.get("_del")]
        cfg["loads"] = [{k: v for k, v in ld.items() if not str(k).startswith("_")} for ld in keep]
        _save_settings(cfg)
        st.session_state["kt_loads"] = keep
        st.success(f"投入 {len(keep)}本の設定を保存しました。")
        st.rerun()

st.page_link("pages/3_🚀_進捗反映自動化.py", label="🚀 進捗反映自動化へ", use_container_width=True)
