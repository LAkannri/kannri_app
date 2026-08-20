import streamlit as st
import pandas as pd
import json
import characters as ch
import theme
import sf_ui
from supabase import create_client, Client

st.set_page_config(page_title="開通反映の自動化 - エンカンAI", layout="wide")

theme.inject_theme()
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

with st.container(border=True):
    theme.section_title("⚙️", "① 設定の置き場所")
    st.caption("開通反映用の設定を保存するスプレッドシートです。"
               "進捗反映と同じものを使ってもかまいません（タブが分かれるので混ざりません）。")
    _url = st.text_input("設定スプレッドシートのURL", value=cfg.get("settings_url", ""),
                         placeholder="https://docs.google.com/spreadsheets/d/.../edit")
    if st.button("💾 保存", key="save_kaitsuu_url"):
        cfg["settings_url"] = _url.strip()
        _save_settings(cfg)
        st.success("保存しました。")
        st.rerun()
    if not gc:
        st.error("接続キー GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")

with st.container(border=True):
    theme.section_title("☁️", "② Salesforceに投入する")
    st.caption("開通の反映では、案件ID（Id）だけでなく **ガスID・電力ID** で照合することもあります。"
               "キー項目に外部ID（`GasID__c` / `Powercustomernumber__c` など）を指定してください。")
    sf_ui.render(gc, cfg.get("settings_url", ""), key_prefix="kaitsuu")

st.page_link("pages/3_🚀_進捗反映自動化.py", label="🚀 進捗反映自動化へ", use_container_width=True)
