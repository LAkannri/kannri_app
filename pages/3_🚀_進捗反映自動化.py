import streamlit as st
import pandas as pd
import json
import re
import characters as ch
import theme
import sf_ui
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

def _extract_folder_id(text: str) -> str:
    """DriveのフォルダURLからIDだけを取り出す（IDをそのまま貼られた場合はそのまま返す）。
    人に「どこからどこまでを切り取って」と説明させないための処理。
    例: https://drive.google.com/drive/folders/1WJxOy...?usp=drive_link → 1WJxOy...
    """
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s.split("?")[0].rstrip("/").split("/")[-1]

def _extract_sheet_id(text: str) -> str:
    """スプレッドシートのURLからIDを取り出す（IDをそのまま貼られた場合はそのまま）。
    例: https://docs.google.com/spreadsheets/d/1tKhA.../edit#gid=0 → 1tKhA..."""
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", s)
    return m.group(1) if m else s.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]

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
    _folder_in = st.text_input("取り込みフォルダ（URLをそのまま貼ってOK）",
                               value=cfg.get("intake_folder_id", ""),
                               placeholder="https://drive.google.com/drive/folders/1WJxOy... または ID だけ",
                               help="メールの添付が保存されるDriveフォルダです。この下にキャリア名のフォルダが並びます。")
    st.caption("""📎 **どこを貼るの？** DriveでフォルダをひらいたときのURL全部でOKです。
`folders/` のうしろから `?` の手前までがIDで、アプリが自動で切り取ります。

```
https://drive.google.com/drive/folders/1WJxOyDvSXv5qnJ1XlNvAGTj4_A4qvsJJ?usp=drive_link
                                       └─────────── ここがID ──────────┘
```
""")
    # URLでもIDでも受け付ける（人に切り取らせない）
    _folder = _extract_folder_id(_folder_in)
    if _folder_in and _folder != _folder_in.strip():
        st.caption(f"→ フォルダIDとして `{_folder}` を使います。")
    if st.button("💾 保存", key="save_settings_url"):
        cfg["settings_url"] = _url.strip()
        cfg["intake_folder_id"] = _folder.strip()
        _save_settings(cfg)
        # 📤 GASにも同じ値を渡す（設定スプシの「基本設定」タブ経由）。
        #    ここに書いておけば、GAS側でフォルダIDを書き直す必要がない＝二重入力を防ぐ。
        _msg = ""
        if cfg["settings_url"] and _get_gspread_client():
            try:
                _sh = _get_gspread_client().open_by_url(cfg["settings_url"])
                try:
                    _bw = _sh.worksheet("基本設定")
                except Exception:
                    _bw = _sh.add_worksheet(title="基本設定", rows=20, cols=2)
                _bw.update(range_name="A1", values=[["項目", "値"],
                                                    ["取り込みフォルダID", cfg["intake_folder_id"]]],
                           value_input_option="USER_ENTERED")
                _msg = "（GASにも同じフォルダIDを渡しました）"
            except Exception as _e:
                _msg = f"（※GASへの受け渡しに失敗: {_e}）"
        st.success("保存しました。" + _msg)
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
            # エラー文が英語で長いので、よくある原因を先に日本語で示す
            _txt = str(e)
            if "accessNotConfigured" in _txt or "has not been used in project" in _txt:
                _pj = re.search(r"project (\d+)", _txt)
                _url_api = ("https://console.developers.google.com/apis/api/drive.googleapis.com/overview"
                            + (f"?project={_pj.group(1)}" if _pj else ""))
                st.error("⚠️ Google Cloud で **Drive API がまだ有効になっていません**（共有の問題ではありません）。")
                st.markdown(f"1. [このリンクを開く]({_url_api})（サービスアカウントを作ったアカウントでログイン）\n"
                            "2. **「有効にする」** を押す\n"
                            "3. 1〜2分待ってから、もう一度このボタンを押す")
                st.caption("スプレッドシートだけ扱っていたときは不要でしたが、"
                           "Driveのフォルダを見るようになったため必要になりました。")
            elif "404" in _txt or "notFound" in _txt:
                st.error("⚠️ そのフォルダが見つかりません。URLが正しいか、"
                         "サービスアカウント（enkan-robot-reader@…）に共有しているか確認してください。")
            elif "403" in _txt:
                st.error("⚠️ 見る権限がありません。フォルダをサービスアカウントに"
                         "**閲覧者**として共有してください。")
            else:
                st.error(f"読めませんでした: {e}")
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
            # 📝 表に直接書くのではなく、1項目ずつ選んで入れていく形にする
            #    （エントリー業務の設定と同じ操作感。何を入れる欄なのかが分かるように）
            _carriers = [c for c in df["キャリア名"].tolist() if str(c).strip()]
            _NEW = "＋ 新しいキャリアを追加"
            _pick = st.selectbox("どのキャリアを設定する？", _carriers + [_NEW], key="cfg_pick")
            _is_new = (_pick == _NEW)
            _cur = ({} if _is_new
                    else df[df["キャリア名"] == _pick].iloc[0].to_dict())

            st.markdown("**1. このキャリアの名前**")
            _name = st.text_input("キャリア名", value=str(_cur.get("キャリア名", "")),
                                  placeholder="例：GMO ドコモ", key="cfg_name",
                                  help="Driveの保存先フォルダ名にもなります")

            st.markdown("**2. どのメールから取り込む？**")
            _query = st.text_input("メールの検索条件", value=str(_cur.get("Gmail検索条件", "")),
                                   placeholder="from:送信元 subject:進捗 has:attachment newer_than:3d",
                                   key="cfg_query")
            st.caption("💡 Gmailの検索窓で実際に検索してみて、うまく絞れた条件をそのままコピーするのが確実です。"
                       "`newer_than:3d` は「3日以内」の意味（古いメールを拾わない保険）。")

            _FILE_KINDS = {"ZIPファイル": r"\.zip$", "Excelファイル": r"\.xlsx?$",
                           "CSVファイル": r"\.csv$", "どれでもよい": "",
                           "自分で指定する": "__custom__"}
            _cur_files = str(_cur.get("添付の絞り込み(正規表現)", "") or "")
            _kind_default = next((k for k, v in _FILE_KINDS.items() if v == _cur_files), "自分で指定する")
            _kind = st.selectbox("添付ファイルの種類", list(_FILE_KINDS.keys()),
                                 index=list(_FILE_KINDS.keys()).index(_kind_default), key="cfg_kind")
            if _FILE_KINDS[_kind] == "__custom__":
                _files = st.text_input("絞り込み（正規表現）", value=_cur_files, key="cfg_files")
            else:
                _files = _FILE_KINDS[_kind]

            st.markdown("**3. どこに貼り付ける？**")
            _sheet_in = st.text_input("貼り付け先のスプレッドシート（URLでもIDでもOK）",
                                      value=str(_cur.get("貼り付け先スプシID", "")), key="cfg_sheet",
                                      placeholder="https://docs.google.com/spreadsheets/d/.../edit")
            _sheet_id = _extract_sheet_id(_sheet_in)
            _tabs = []
            if _sheet_id:
                try:
                    _tabs = [w.title for w in gc.open_by_key(_sheet_id).worksheets()]
                except Exception as _e:
                    st.warning(f"このスプレッドシートを開けませんでした（共有を確認してください）: {str(_e)[:100]}")

            def _tab_select(label, cur, help_text, allow_empty=False):
                """タブ名をプルダウンで選ぶ（読めないときは手入力にフォールバック）"""
                if not _tabs:
                    return st.text_input(label, value=str(cur or ""), help=help_text, key="cfgt_" + label)
                opts = (["（なし）"] if allow_empty else []) + _tabs
                cur = str(cur or "")
                idx = opts.index(cur) if cur in opts else 0
                sel = st.selectbox(label, opts, index=idx, help=help_text, key="cfgs_" + label)
                return "" if sel == "（なし）" else sel

            _src = _tab_select("元データシート（進捗ファイルを貼る先）", _cur.get("元データシート名"),
                               "例：GMO ドコモ元データ")
            _dst = _tab_select("投入用シート（Salesforceに入れる行）", _cur.get("投入用シート名"),
                               "例：GMO ドコモ進捗反映（一括）")
            _chk = _tab_select("確認用シート（任意）", _cur.get("確認用シート名"),
                               "目視確認用。③で中身を見られます", allow_empty=True)

            st.markdown("**4. ファイルの読み方**")
            _skip = st.number_input("ファイルの見出しは何行？", min_value=0, max_value=10,
                                    value=int(str(_cur.get("捨てる先頭行数", "1") or "1").strip() or 1),
                                    key="cfg_skip",
                                    help="その行数を読み飛ばして、下のデータだけを貼り付けます")
            _pw = st.text_input("解錠パスワードの名前（パスワード付き添付のとき）",
                                value=str(_cur.get("解錠パスワードの名前", "")), key="cfg_pw",
                                placeholder="例：ドコモ進捗パス（空欄でOK）")
            st.caption("⚠️ パスワードそのものは書かないでください。"
                       "司令室の「🔑 ログイン情報」で登録した**名前**を入れます。")

            _active = st.checkbox("このキャリアの取り込みを有効にする",
                                  value=(str(_cur.get("有効", "TRUE")).upper() != "FALSE"), key="cfg_active",
                                  help="外すと、設定を消さずに一時停止できます")

            b1, b2 = st.columns([2, 1])
            with b1:
                if st.button("💾 このキャリアの設定を保存", type="primary", key="save_cfg",
                             use_container_width=True):
                    if not _name.strip():
                        st.warning("キャリア名を入れてください。")
                    else:
                        row = {"キャリア名": _name.strip(), "Gmail検索条件": _query.strip(),
                               "添付の絞り込み(正規表現)": _files, "有効": "TRUE" if _active else "FALSE",
                               "貼り付け先スプシID": _sheet_id, "元データシート名": _src,
                               "投入用シート名": _dst, "確認用シート名": _chk,
                               "解錠パスワードの名前": _pw.strip(), "捨てる先頭行数": str(int(_skip)),
                               "オブジェクトAPI名": str(_cur.get("オブジェクトAPI名", "")),
                               "外部IDキー": str(_cur.get("外部IDキー", ""))}
                        base = df[df["キャリア名"] != _name.strip()]
                        merged = pd.concat([base, pd.DataFrame([row])], ignore_index=True)
                        try:
                            n = _write_config_rows(gc, cfg["settings_url"], merged)
                            st.success(f"「{_name}」を保存しました（全{n}件）。GASも次回からこの内容で動きます。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"保存できませんでした: {e}")
            with b2:
                if not _is_new and st.button("🗑 このキャリアを削除", key="del_cfg",
                                             use_container_width=True):
                    try:
                        _write_config_rows(gc, cfg["settings_url"], df[df["キャリア名"] != _pick])
                        st.success(f"「{_pick}」を削除しました。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"削除できませんでした: {e}")

            with st.expander("📋 いまの設定を一覧で見る"):
                st.dataframe(df, use_container_width=True, hide_index=True)

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
    theme.section_title("☁️", "④ Salesforceに投入する（データローダーの代わり）")
    st.caption("CSVの書き出しもダウンロードも要りません。投入用シートを読んで、そのままUPSERTします。")
    sf_ui.render(gc, cfg.get("settings_url", ""), key_prefix="prog")

with st.container(border=True):
    theme.section_title("🚧", "⑤ まとめて反映（これから）")
    st.markdown("""
    ここに「まとめて反映開始」ボタンを作ります。押すとキャリアごとに:

    1. Driveのフォルダから最新の添付を取る（パスワード付きなら解錠）
    2. **ファイルの見出しと、貼り付け先シートの見出しを照合**（違えば貼らずに中止）
    3. 見出しは残したまま、その下を入れ替える
    4. 続けて④の投入まで実行し、結果を一覧表示
    """)
    st.info("🚧 取り込み〜貼り付けの自動化は次に作ります。いまは④の投入だけ単独で使えます。")

st.page_link("pages/2_📝_エントリー業務自動化.py", label="🎬 エントリー業務自動化へ戻る", use_container_width=True)
