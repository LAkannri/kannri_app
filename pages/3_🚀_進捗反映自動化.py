import streamlit as st
import pandas as pd
import json
import characters as ch
import theme
from supabase import create_client, Client

st.set_page_config(page_title="進捗反映の自動化 - エンカンAI", layout="wide")

theme.inject_theme()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🚀", "進捗反映を自動化",
                  "各キャリアの進捗ファイルを取り込んで、進捗スプレッドシートへ反映します。",
                  color=c["color"])

# ==========================================
# 🔌 接続（エントリー業務のページと同じ鍵を使う）
# ==========================================
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase: Client = init_connection()

# 進捗反映の設定は、ロボット一覧に出さない予約行に保存する（id が __ で始まる行）
SETTINGS_ID = "__progress__"

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
        "id": SETTINGS_ID, "name": "（進捗反映の設定）", "is_active": False,
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

# 取り込み設定シートの見出し。GAS（進捗メール添付の取り込み.gs）と同じ並びにしておくこと。
CONFIG_TAB = "取り込み設定"
CONFIG_HEADERS = ["キャリア名", "Gmail検索条件", "添付の絞り込み(正規表現)", "有効",
                  "貼り付け先スプシID", "元データシート名", "投入用シート名", "確認用シート名",
                  "解錠パスワードの名前", "捨てる先頭行数", "オブジェクトAPI名", "外部IDキー"]

def _read_config_rows(gc, url):
    """設定シートを読む。無ければ見出しだけ作って空で返す。"""
    sh = gc.open_by_url(url)
    try:
        ws = sh.worksheet(CONFIG_TAB)
    except Exception:
        ws = sh.add_worksheet(title=CONFIG_TAB, rows=100, cols=len(CONFIG_HEADERS))
        ws.update(range_name="A1", values=[CONFIG_HEADERS])
        ws.freeze(rows=1)
        return pd.DataFrame(columns=CONFIG_HEADERS)
    values = ws.get_all_values()
    if not values:
        ws.update(range_name="A1", values=[CONFIG_HEADERS])
        return pd.DataFrame(columns=CONFIG_HEADERS)
    headers = values[0]
    rows = [(r + [""] * len(headers))[:len(headers)] for r in values[1:]]
    df = pd.DataFrame(rows, columns=headers)
    for h in CONFIG_HEADERS:          # 列が足りなければ足す（見出しを増やしたとき用）
        if h not in df.columns:
            df[h] = ""
    return df[CONFIG_HEADERS]

def _write_config_rows(gc, url, df):
    """設定シートを丸ごと書き直す（見出し＋中身）。GASはこの表を読んで動く。"""
    sh = gc.open_by_url(url)
    try:
        ws = sh.worksheet(CONFIG_TAB)
    except Exception:
        ws = sh.add_worksheet(title=CONFIG_TAB, rows=100, cols=len(CONFIG_HEADERS))
    body = [CONFIG_HEADERS] + df.fillna("").astype(str).values.tolist()
    ws.clear()
    ws.update(range_name="A1", values=body, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    return len(body) - 1

ch.guide("operate",
         "ここでキャリアごとの取り込み設定をするよ。設定はスプレッドシートに保存されるから、"
         "メールを取りに行くGASと、このアプリの両方が同じ設定を見るんだ。")

cfg = _load_settings()
gc = _get_gspread_client()

# ==========================================
# ① 設定の置き場所（設定スプレッドシート）
# ==========================================
with st.container(border=True):
    theme.section_title("⚙️", "① 設定の置き場所")
    st.caption("キャリアごとの取り込み設定を保存するスプレッドシートです。"
               "GAS（メールを取りに行く仕掛け）も同じシートを読むので、1か所にまとまります。")
    _url = st.text_input("設定スプレッドシートのURL", value=cfg.get("settings_url", ""),
                         placeholder="https://docs.google.com/spreadsheets/d/.../edit")
    _folder = st.text_input("取り込みフォルダID（GASの setup() で表示されたID）",
                            value=cfg.get("intake_folder_id", ""),
                            placeholder="例：1CUcMIgkHsYbpzsQMvs4ctBzoPqwUWiAZ",
                            help="メールの添付が保存されるDriveフォルダです。この下にキャリア名のフォルダが並びます。")
    if st.button("💾 保存", key="save_settings_url"):
        cfg["settings_url"] = _url.strip()
        cfg["intake_folder_id"] = _folder.strip()
        _save_settings(cfg)
        st.success("保存しました。")
        st.rerun()
    # 📁 フォルダが読めるか（共有できているか）をその場で確認する
    if cfg.get("intake_folder_id") and st.button("📁 フォルダを確認する", key="check_folder"):
        try:
            import gspread  # 認証情報の使い回しのため
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
            _creds = Credentials.from_service_account_info(
                json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"]),
                scopes=["https://www.googleapis.com/auth/drive.readonly"])
            _drive = build("drive", "v3", credentials=_creds)
            _sub = _drive.files().list(
                q=f"'{cfg['intake_folder_id']}' in parents and trashed=false",
                fields="files(id,name,mimeType,modifiedTime)", orderBy="modifiedTime desc",
                pageSize=20).execute().get("files", [])
            if _sub:
                st.success(f"✅ 読めました。中身 {len(_sub)}件：" +
                           "／".join(f["name"] for f in _sub[:10]))
            else:
                st.info("✅ 読めましたが、中身はまだ空です（GASがメールを取り込むと入ります）。")
        except Exception as e:
            st.error(f"読めませんでした。フォルダをサービスアカウントに共有しているか確認してください: {e}")
    if not _get_gspread_client():
        st.error("接続キー GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。これが無いとスプシを読み書きできません。")
    elif cfg.get("settings_url"):
        st.caption("※このスプレッドシートを、サービスアカウントに**編集者**として共有しておいてください。")

# ==========================================
# ② キャリアごとの取り込み設定
# ==========================================
with st.container(border=True):
    theme.section_title("📥", "② キャリアごとの取り込み設定")
    if not (gc and cfg.get("settings_url")):
        st.info("先に①で設定スプレッドシートを登録してください。")
    else:
        try:
            df = _read_config_rows(gc, cfg["settings_url"])
        except Exception as e:
            df = None
            st.error(f"設定シートを読めませんでした（共有設定を確認してください）: {e}")
        if df is not None:
            st.caption("1行＝1キャリアです。**有効**を FALSE にすると、行を消さずに一時停止できます。"
                       "パスワードそのものは書かず、司令室で登録した**名前だけ**を書いてください。")
            edited = st.data_editor(
                df, num_rows="dynamic", use_container_width=True, key="cfg_editor",
                column_config={
                    "キャリア名": st.column_config.TextColumn(help="保存先フォルダの名前になります。例：ドコモ光"),
                    "Gmail検索条件": st.column_config.TextColumn(
                        width="large", help="Gmailの検索窓と同じ書き方。例：from:xxx subject:進捗 has:attachment newer_than:3d"),
                    "添付の絞り込み(正規表現)": st.column_config.TextColumn(help="例：\\.(zip|xlsx|csv)$　空なら全部"),
                    "有効": st.column_config.SelectboxColumn(options=["TRUE", "FALSE"], help="FALSEで一時停止"),
                    "貼り付け先スプシID": st.column_config.TextColumn(
                        width="medium", help="LL進捗反映／N進捗反映など、貼り付け先のスプレッドシートID"),
                    "元データシート名": st.column_config.TextColumn(
                        help="取り込んだファイルを貼り付けるシート。例：GMO ドコモ元データ"),
                    "投入用シート名": st.column_config.TextColumn(
                        help="Salesforceに投入する行が並ぶシート。例：GMO ドコモ進捗反映（一括）"),
                    "確認用シート名": st.column_config.TextColumn(
                        help="（任意）目視確認用のシート。アプリの③で中身を見られます"),
                    "オブジェクトAPI名": st.column_config.TextColumn(help="Salesforceの投入先。例：Opportunity"),
                    "外部IDキー": st.column_config.TextColumn(help="UPSERTの突き合わせに使う外部ID項目のAPI名"),
                    "解錠パスワードの名前": st.column_config.TextColumn(
                        help="パスワード付き添付のとき。司令室の「🔑 ログイン情報」で登録した名前"),
                    "捨てる先頭行数": st.column_config.TextColumn(help="ファイル側の見出しが何行あるか。ふつうは1"),
                })
            if st.button("💾 この内容で保存する", type="primary", key="save_cfg"):
                try:
                    n = _write_config_rows(gc, cfg["settings_url"], edited)
                    st.success(f"{n}件の設定を保存しました。GASも次回からこの内容で動きます。")
                except Exception as e:
                    st.error(f"保存できませんでした: {e}")

# ==========================================
# ③ これから作るところ
# ==========================================
# ==========================================
# ③ 進捗の確認（スプシを開かずに、アプリで中身を見る）
# ==========================================
with st.container(border=True):
    theme.section_title("👀", "③ 進捗を確認する")
    if not (gc and cfg.get("settings_url")):
        st.info("先に①②の設定をしてください。")
    else:
        try:
            _rows = _read_config_rows(gc, cfg["settings_url"])
        except Exception:
            _rows = pd.DataFrame(columns=CONFIG_HEADERS)
        _names = [r for r in _rows["キャリア名"].tolist() if str(r).strip()]
        if not _names:
            st.info("②でキャリアを登録すると、ここで中身を確認できます。")
        else:
            v1, v2 = st.columns([2, 2])
            with v1:
                _pick = st.selectbox("キャリア", _names, key="view_carrier")
            _row = _rows[_rows["キャリア名"] == _pick].iloc[0]
            _sheet_opts = {"確認用シート": _row.get("確認用シート名", ""),
                           "元データ": _row.get("元データシート名", ""),
                           "投入用（一括）": _row.get("投入用シート名", "")}
            _sheet_opts = {k: v for k, v in _sheet_opts.items() if str(v).strip()}
            if not _sheet_opts:
                st.warning("このキャリアにはシート名が設定されていません（②で設定してください）。")
            else:
                with v2:
                    _which = st.selectbox("どのシートを見る？", list(_sheet_opts.keys()), key="view_which")
                if st.button("👀 中身を見る", key="view_go", use_container_width=True):
                    try:
                        _sh = gc.open_by_key(str(_row.get("貼り付け先スプシID", "")).strip())
                        _ws = _sh.worksheet(_sheet_opts[_which])
                        _vals = _ws.get_all_values()[:200]   # 先頭200行だけ（重くしない）
                        if not _vals:
                            st.info("このシートは空です。")
                        else:
                            _hdr = _vals[0]
                            _body = [(r + [""] * len(_hdr))[:len(_hdr)] for r in _vals[1:]]
                            st.caption(f"「{_sheet_opts[_which]}」の先頭{len(_body)}行"
                                       "（⚠️ 実データのため個人情報が含まれます）")
                            st.dataframe(pd.DataFrame(_body, columns=[h or f"列{i+1}" for i, h in enumerate(_hdr)]),
                                         use_container_width=True, hide_index=True)
                    except Exception as e:
                        st.error(f"読めませんでした: {e}")

with st.container(border=True):
    theme.section_title("🚧", "④ 反映の実行（これから）")
    st.markdown("""
    ここに「まとめて反映開始」ボタンを作ります。押すとキャリアごとに:

    1. Driveのフォルダから最新の添付を取る（パスワード付きなら解錠）
    2. **ファイルの見出しと、貼り付け先シートの見出しを照合**（違えば貼らずに中止）
    3. 見出しは残したまま、その下を入れ替える
    4. 結果を一覧表示（件数・エラーの有無）

    まずは①②の設定を作るところまで動きます。
    """)
    st.info("🚧 実行部分は次に作ります。先に1キャリア分の設定を入れて、動きを確認しましょう。")

st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 エントリー業務自動化へ戻る", use_container_width=True)
