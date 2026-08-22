import streamlit as st
import pandas as pd
import json
import os
import re
import tempfile
import characters as ch
import theme
import sf_ui
import intake_runner
import steps_ai
import robot_settings_ui
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
CONFIG_HEADERS = ["キャリア名", "取り込み方法", "Gmail検索条件", "添付の絞り込み(正規表現)", "有効",
                  "貼り付け先スプシID", "元データシート名", "投入用シート名", "確認用シート名",
                  "解錠パスワードの名前", "ファイルの見出し行数", "貼り付け先の見出し行数",
                  "取り込みロボット名", "オブジェクトAPI名", "外部IDキー"]

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

# 📌 Sheets API は「1分に60回」の読み取り上限がある。Streamlit は操作のたびに
#    画面を作り直すため、そのまま読みに行くとすぐ上限に当たる。短時間キャッシュする。
@st.cache_data(ttl=60, show_spinner=False)
def _read_config_rows(_gc, url):
    """設定シートを読む。無ければ見出しだけ作って空で返す。"""
    gc = _gc
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

@st.cache_data(ttl=120, show_spinner=False)
def _list_tabs(_gc, sheet_id: str):
    """スプレッドシートのタブ名一覧。毎回読むとAPIの上限に当たるのでキャッシュする。"""
    return [w.title for w in _gc.open_by_key(sheet_id).worksheets()]

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

def _keep_files() -> int:
    """1キャリアあたり、手元に残しておくファイル数（古い分は自動で消す）。"""
    try:
        return max(1, int(str(cfg.get("keep_generations", 1) or 1)))
    except Exception:
        return 1

def _archive_download(carrier: str, path: str):
    """サイトから落としたファイルを、Driveの保管フォルダにも置く（任意）。

    ふだんはPCの「取り込みファイル」フォルダに入れば足りる。
    メール添付と同じ場所にも残したいときだけONにする設定にしてある。
    保管に失敗しても取り込み自体は続けたいので、ここでは知らせるだけにする。
    """
    if not (path and cfg.get("archive_downloads") and cfg.get("intake_folder_id")):
        return
    try:
        msg = intake_runner.archive_to_drive(
            st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"],
            cfg["intake_folder_id"], str(carrier).strip() or "その他", path,
            keep=_keep_files())
        st.caption(f"☁️ {msg}")
    except Exception as e:
        st.caption("（Driveへの保管はできませんでした。取り込みは続けられます：）")
        st.caption(f"{str(e)[:180]}")

# ==========================================
# ① 設定の置き場所（設定スプレッドシート）
# ==========================================
# 一度決めたら以後さわらない設定なので、ふだんは畳んでおく
#（キャリアが増えても、ここは変わらない）
with st.expander("⚙️ 進捗設定（最初に1回だけ／ふだんは触りません）",
                 expanded=not cfg.get("settings_url")):
    _url = st.text_input("エンカンAI_進捗設定スプレッドシートURL",
                         value=cfg.get("settings_url", ""),
                         placeholder="https://docs.google.com/spreadsheets/d/.../edit",
                         help="キャリアごとの設定が保存されるスプレッドシート。GASも同じものを読みます")
    _folder_in = st.text_input("進捗ダウンロード保管Googleドライブ（URLをそのまま貼ってOK）",
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
    # 🔑 このフォルダは「ロボットのアドレス」に共有しておかないと、
    #    見ることも書き込むこともできない。どこにも書いていないと詰まるので、ここに出す。
    _sa_mail = intake_runner.service_account_email(
        st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    # 共有さえ済んでいれば、キャリアのフォルダは自動で作られる。
    # ふだんは気にしなくてよいので、つまずいたときだけ開ける場所に置く。
    if _sa_mail:
        with st.expander("保存できないと言われたら（フォルダの共有）"):
            st.caption("このフォルダを、下のアドレスに**「編集者」**で共有してください"
                       "（フォルダを右クリック →「共有」→ 貼り付け）。"
                       "共有ドライブの中にある場合は、そのドライブの「メンバーを管理」から"
                       "「投稿者」以上で追加します。")
            st.code(_sa_mail, language=None)
    # 🗑 貯め続けると容量を食うので、何件残すかを決めておく
    _keepgen = st.number_input("キャリアごとに残すファイル数（古い分は自動で消します）",
                               min_value=1, max_value=30,
                               value=max(1, int(str(cfg.get("keep_generations", 1) or 1))),
                               key="cfg_keepgen",
                               help="1 なら「いちばん新しい分だけ残す」。"
                                    "消すのはロボットが入れたファイルだけで、"
                                    "メールの添付など人が置いたものは消しません")
    st.caption(f"📁 サイトから落としたファイルの置き場所："
               f"`{intake_runner.INTAKE_ROOT}`（この下にキャリア名のフォルダができます）")
    _push = st.checkbox("反映のあと、そのままSalesforceへ投入する",
                        value=bool(cfg.get("push_salesforce", True)),
                        key="cfg_push",
                        help="スプレッドシートに貼り付けたあと、続けて投入まで行います。"
                             "OFFにすると貼り付けだけで止まり、投入は手で押します")
    _arch = st.checkbox("落としたファイルを、Googleドライブの保管フォルダにも置く",
                        value=bool(cfg.get("archive_downloads", False)),
                        key="cfg_arch",
                        help="ONにすると、メールの添付と同じフォルダにも残します。"
                             "そのフォルダをロボットのアドレスに共有しておく必要があります")
    _gas_url = st.text_input("エンカンAI_進捗GASのウェブアプリURL",
                             value=cfg.get("gas_url", ""),
                             placeholder="https://script.google.com/macros/s/.../exec",
                             help="Apps Scriptで「デプロイ→ウェブアプリ」にしたときのURL。"
                                  "これを入れると、必要なときだけアプリからGASを呼べます")
    if st.button("💾 保存", key="save_settings_url"):
        cfg["settings_url"] = _url.strip()
        cfg["intake_folder_id"] = _folder.strip()
        cfg["keep_generations"] = int(_keepgen)
        cfg["archive_downloads"] = bool(_arch)
        cfg["push_salesforce"] = bool(_push)
        cfg["gas_url"] = _gas_url.strip()
        # 🔑 GASを呼ぶときの合言葉。URLを知られても勝手に実行されないようにする。
        if not cfg.get("gas_token"):
            import secrets as _secrets
            cfg["gas_token"] = _secrets.token_urlsafe(16)
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
                                                    ["取り込みフォルダID", cfg["intake_folder_id"]],
                                                    ["GAS合言葉", cfg.get("gas_token", "")]],
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

with st.container(border=True):
    theme.section_title("▶", "進捗をまとめて実行")
    st.caption("進捗ファイルを集めて、元データシートを入れ替え、"
               + ("そのままSalesforceへ投入するところまで" if cfg.get("push_salesforce", True)
                  else "貼り付けるところまで")
               + "を一度に行います。ふだんはここを押すだけです。")
    if not (gc and cfg.get("settings_url")):
        st.info("先に「⚙️ 進捗設定」とキャリアの登録をしてください。")
    else:
        try:
            _rows_all = _read_config_rows(gc, cfg["settings_url"])
        except Exception as e:
            _rows_all = pd.DataFrame(columns=CONFIG_HEADERS)
            st.error(f"設定を読めませんでした: {e}")
        _live = _rows_all[_rows_all["有効"].astype(str).str.upper() != "FALSE"]
        _groups = {}
        for _, r in _live.iterrows():
            sid = str(r.get("貼り付け先スプシID", "")).strip()
            if sid:
                _groups.setdefault(sid, []).append(r.to_dict())
        if not _groups:
            st.info("有効なキャリアがまだありません。上で登録してください。")
        for _sid, _members in _groups.items():
            try:
                _title = gc.open_by_key(_sid).title
            except Exception:
                _title = _sid[:12] + "…"
            with st.container(border=True):
                st.markdown(f"**📗 {_title}**　（{len(_members)}キャリア：" +
                            "／".join(str(m['キャリア名']) for m in _members) + "）")
                # 手動アップロードのキャリアは、実行前にファイルを選んでもらう
                for _m in _members:
                    if str(_m.get("取り込み方法", "")).startswith("手動"):
                        st.file_uploader(f"📎 {_m['キャリア名']} のファイルを選ぶ",
                                         key=f"manualfile_{_m['キャリア名']}")
                st.caption("押すと、ファイルの入手 → 元データへの貼り付け"
                           + (" → Salesforceへの投入" if cfg.get("push_salesforce", True) else "")
                           + " まで一度に行います。")
                if st.button(f"🔄 {_title} をまとめて反映", key=f"runsheet_{_sid}",
                             type="primary", use_container_width=True):
                    # Driveが要るのはメール添付方式のキャリアだけ。
                    # サイトから落とす方式しか無いなら、Driveに繋がらなくても実行できる。
                    _need_drive = any(str(m.get("取り込み方法", "メールの添付")) == "メールの添付"
                                      for m in _members)
                    _drive = None
                    try:
                        _drive = intake_runner.drive_client(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
                    except Exception as e:
                        if _need_drive:
                            st.error(f"Driveに接続できません: {e}")
                    if _drive or not _need_drive:
                        # 📨 メール方式のキャリアがあれば、先にGASを呼んで最新の添付を集めてもらう
                        #    （時間ごとの自動実行に頼らず、必要なときだけ動かす）
                        if any(str(m.get("取り込み方法", "メールの添付")) == "メールの添付"
                               for m in _members) and cfg.get("gas_url"):
                            with st.spinner("📨 メールから最新の添付を取り込んでいます..."):
                                _gok, _gmsg = intake_runner.call_gas(
                                    cfg["gas_url"], cfg.get("gas_token", ""), "intake")
                            if _gok:
                                st.caption("📨 メールの取り込みが終わりました。")
                            else:
                                st.warning(f"📨 メールの取り込みを呼べませんでした（{_gmsg}）。"
                                           "すでにDriveにあるファイルで続けます。")
                        # 🔑 パスワード付きファイル用に、司令室で登録した鍵を復号しておく
                        _secrets_map = {}
                        try:
                            import robot as _rb  # 復号処理を使い回す
                            for _p in (supabase.table("merchants").select("config_json").execute().data or []):
                                _enc = ((_p.get("config_json") or {}).get("robot_config", {}) or {}).get("secrets", {})
                                if _enc:
                                    _secrets_map.update(_rb.decrypt_secrets(_enc))
                        except Exception:
                            pass
                        _hist = intake_runner.read_history(gc, cfg["settings_url"])
                        _results = []
                        _bar = st.progress(0.0)
                        for _i, _m in enumerate(_members, 1):
                            _method = str(_m.get("取り込み方法", "") or "メールの添付")
                            _local = None
                            if _method.startswith("サイト"):
                                # 🖥 ブラウザを開くので、このPCで実行する
                                _bot = str(_m.get("取り込みロボット名", "") or "").strip()
                                if not _bot:
                                    _results.append({"キャリア": _m["キャリア名"], "ファイル": "", "件数": 0,
                                                     "結果": "⚠️ 取り込みロボットが未設定"})
                                    _bar.progress(_i / len(_members)); continue
                                # 保存先はロボット名で決める（テスト実行・通しで試すと同じ場所にそろえる）
                                _dir = intake_runner.intake_dir(_m["キャリア名"])
                                with st.spinner(f"{_m['キャリア名']}：ブラウザでダウンロード中..."):
                                    try:
                                        _ok, _log, _newest = intake_runner.run_download_robot(
                                            _bot, _dir, keep=_keep_files())
                                    except Exception as _e:
                                        _ok, _log, _newest = False, str(_e)[:300], None
                                if not (_ok and _newest):
                                    _results.append({"キャリア": _m["キャリア名"], "ファイル": "", "件数": 0,
                                                     "結果": f"❌ ダウンロードできませんでした（{_log[-120:]}）"})
                                    _bar.progress(_i / len(_members)); continue
                                with open(_newest, "rb") as _fh:
                                    _local = (os.path.basename(_newest), _fh.read())
                                # メール添付と同じ保管フォルダにも残す（探す場所を1か所にする）
                                _archive_download(_m["キャリア名"], _newest)
                            elif _method.startswith("手動"):
                                _up = st.session_state.get(f"manualfile_{_m['キャリア名']}")
                                if not _up:
                                    _results.append({"キャリア": _m["キャリア名"], "ファイル": "", "件数": 0,
                                                     "結果": "⚠️ ファイルが選ばれていません（下の欄で選んでください）"})
                                    _bar.progress(_i / len(_members)); continue
                                _local = (_up.name, _up.getvalue())
                            with st.spinner(f"{_m['キャリア名']} を処理中..."):
                                _results.append(intake_runner.run_one(
                                    gc, _drive, cfg.get("intake_folder_id", ""), _m, _secrets_map,
                                    local_file=_local,
                                    last_file=_hist.get(str(_m["キャリア名"]).strip(), "")))
                            _bar.progress(_i / len(_members))
                        _done = [r for r in _results if str(r["結果"]).startswith("✅")]
                        _skip = [r for r in _results if str(r["結果"]).startswith("⏭")]
                        _ng = [r for r in _results
                               if not str(r["結果"]).startswith(("✅", "⏭"))]
                        # 成功したものだけ「前回のファイル」として記録する
                        try:
                            intake_runner.write_history(gc, cfg["settings_url"], _done)
                        except Exception as _e:
                            st.caption(f"（履歴の記録に失敗: {_e}）")

                        st.markdown(f"**結果：✅ 反映 {len(_done)}件／"
                                    f"⏭ 新しいファイルなし {len(_skip)}件／"
                                    f"❌ できなかった {len(_ng)}件**")
                        if _ng:
                            st.error("❌ 反映できなかったキャリア（対応が必要です）")
                            for _r in _ng:
                                st.markdown(f"- **{_r['キャリア']}**：{_r['結果']}")
                        if _skip:
                            st.info("⏭ 新しい進捗ファイルが届いていないキャリア：" +
                                    "／".join(_r["キャリア"] for _r in _skip))
                        if _done:
                            st.success("✅ 反映できたキャリア：" +
                                       "／".join(f"{_r['キャリア']}（{_r['件数']}件）" for _r in _done))
                        with st.expander("📋 詳しい結果を見る"):
                            st.dataframe(pd.DataFrame(_results), use_container_width=True,
                                         hide_index=True)

                        # ☁️ 貼り付けが済んだら、そのままSalesforceへ投入する。
                        #    人がボタンを押して回らずに、進捗の反映を1回で終わらせるため。
                        #    貼れなかったキャリアは投入しない（古い内容を入れてしまわないように）。
                        if _done and cfg.get("push_salesforce", True):
                            st.markdown("---")
                            st.markdown("**☁️ Salesforceへの投入**")
                            for _r in _done:
                                _row = next((m for m in _members
                                             if str(m["キャリア名"]) == str(_r["キャリア"])), None)
                                if not _row:
                                    continue
                                _dst_tab = str(_row.get("投入用シート名", "") or "").strip()
                                if not _dst_tab:
                                    st.markdown(f"- **{_r['キャリア']}**：⏭ 投入用シートが未設定なので飛ばしました")
                                    continue
                                with st.spinner(f"{_r['キャリア']} を投入しています..."):
                                    _pr = sf_ui.push_carrier(
                                        gc, cfg["settings_url"], str(_r["キャリア"]),
                                        str(_row.get("貼り付け先スプシID", "")).strip(), _dst_tab,
                                        str(_row.get("オブジェクトAPI名", "") or "").strip(),
                                        str(_row.get("外部IDキー", "") or "").strip())
                                st.markdown(f"- **{_r['キャリア']}**：{_pr['結果']}")
                                if _pr.get("errors"):
                                    with st.expander(f"{_r['キャリア']} の失敗の中身"):
                                        st.dataframe(pd.DataFrame(_pr["errors"]),
                                                     use_container_width=True, hide_index=True)
                        elif _done:
                            st.caption("Salesforceへの投入はOFFです（⚙️ 進捗設定で切り替えられます）。"
                                       "キャリアの設定内「☁️ マッピングとSalesforceへの投入」から手で投入できます。")

# ==========================================
# ② キャリアごとの取り込み設定
# ==========================================
with st.container(border=True):
    theme.section_title("📥", "キャリアの設定")
    st.caption("キャリアを足したり、取り込み方や貼り付け先を直したりする場所です。"
               "ふだんの実行は、上の「▶ 進捗をまとめて実行」だけで足ります。")
    if not (gc and cfg.get("settings_url")):
        st.info("先に「⚙️ 進捗設定」で、設定スプレッドシートを登録してください。")
    else:
        try:
            df = _read_config_rows(gc, cfg["settings_url"])
        except Exception as e:
            df = None
            st.error(f"設定シートを読めませんでした（共有設定を確認してください）: {e}")
        if df is not None:
            _carriers = [c for c in df["キャリア名"].tolist() if str(c).strip()]
            _editing = st.session_state.get("prog_editing")

            # 📋 ふだんは一覧。設定を触るときだけ中身を開く（エントリー業務のホームと同じ形）
            if not _editing:
                _, _addcol = st.columns([3, 1])
                with _addcol:
                    if st.button("＋ 新しいキャリアを追加", type="primary", use_container_width=True):
                        st.session_state["prog_editing"] = "__new__"
                        st.rerun()
                if not _carriers:
                    st.info("まだキャリアがありません。「＋ 新しいキャリアを追加」から登録してください。")
                else:
                    _cols = st.columns(3)
                    for _i, _cname in enumerate(_carriers):
                        _row_c = df[df["キャリア名"] == _cname].iloc[0]
                        with _cols[_i % 3]:
                            with st.container(border=True):
                                st.markdown(f"### {_cname}")
                                _on = str(_row_c.get("有効", "TRUE")).upper() != "FALSE"
                                st.markdown(f"<span class='{'status-active' if _on else 'status-inactive'}'>"
                                            f"{'✨ 有効' if _on else '💤 停止中'}</span>",
                                            unsafe_allow_html=True)
                                st.caption(f"取り込み：{_row_c.get('取り込み方法', '') or 'メールの添付'}")
                                st.caption(f"貼り付け先：{_row_c.get('元データシート名', '') or '（未設定）'}")
                                _b1, _b2 = st.columns(2)
                                with _b1:
                                    if st.button("✏️ 設定", key=f"edit_{_cname}", use_container_width=True):
                                        st.session_state["prog_editing"] = _cname
                                        st.rerun()
                                with _b2:
                                    # ⚠️ 誤削除の防止：一度では消さず、必ず確認してから消す
                                    _delkey = f"confirm_del_{_cname}"
                                    if not st.session_state.get(_delkey):
                                        if st.button("🗑 削除", key=f"del_{_cname}",
                                                     use_container_width=True):
                                            st.session_state[_delkey] = True
                                            st.rerun()
                                    else:
                                        st.warning(f"「{_cname}」の設定を消しますか？　**元に戻せません。**")
                                        _d1, _d2 = st.columns(2)
                                        with _d1:
                                            if st.button("はい、消す", key=f"delyes_{_cname}",
                                                         type="primary", use_container_width=True):
                                                _write_config_rows(gc, cfg["settings_url"],
                                                                   df[df["キャリア名"] != _cname])
                                                st.cache_data.clear()
                                                st.session_state.pop(_delkey, None)
                                                st.success(f"「{_cname}」を削除しました。")
                                                st.rerun()
                                        with _d2:
                                            if st.button("やめる", key=f"delno_{_cname}",
                                                         use_container_width=True):
                                                st.session_state.pop(_delkey, None)
                                                st.rerun()

            else:
                # ✏️ ここから下は、選んだキャリア（または新規）の設定
                _is_new = (_editing == "__new__")
                _pick = "" if _is_new else _editing
                _cur = ({} if _is_new or _pick not in _carriers
                        else df[df["キャリア名"] == _pick].iloc[0].to_dict())
                _hb1, _hb2 = st.columns([1, 4])
                with _hb1:
                    if st.button("← 一覧に戻る", use_container_width=True):
                        st.session_state.pop("prog_editing", None)
                        st.rerun()
                with _hb2:
                    st.markdown(f"**{'新しいキャリアを追加' if _is_new else f'「{_pick}」の設定'}**")

                st.markdown("**1. このキャリアの名前**")
                _name = st.text_input("キャリア名", value=str(_cur.get("キャリア名", "")),
                                      placeholder="例：GMO ドコモ", key="cfg_name",
                                      help="Driveの保存先フォルダ名にもなります")

                st.markdown("**2. 進捗ファイルをどこから取る？**")
                _METHODS = ["メールの添付", "サイトからダウンロード（録画したロボット）", "手動でアップロード"]
                _cur_method = str(_cur.get("取り込み方法", "") or "メールの添付")
                _method = st.radio("取り込み方法", _METHODS,
                                   index=_METHODS.index(_cur_method) if _cur_method in _METHODS else 0,
                                   key="cfg_method", horizontal=False)

                _query, _robot = "", ""
                if _method == "メールの添付":
                    _query = st.text_input("メールの検索条件", value=str(_cur.get("Gmail検索条件", "")),
                                           placeholder="from:送信元 subject:進捗 has:attachment newer_than:3d",
                                           key="cfg_query")
                    st.caption("💡 Gmailの検索窓で実際に検索してみて、うまく絞れた条件をそのままコピーするのが確実です。"
                               "`newer_than:3d` は「3日以内」の意味（古いメールを拾わない保険）。")
                elif _method.startswith("サイト"):
                    # kintone など、サイトにログインしてCSVを落とすキャリア。
                    # エントリー業務と同じ「録画したロボット」を使い回す（ログイン情報・認証コード待ちも共通）。
                    # 「ファイルをダウンロード」ステップを持つロボットだけを候補にする。
                    # 申請用のロボットが混ざっていると、取り違えて実行してしまうため。
                    try:
                        _bots = []
                        for _p in (supabase.table("merchants").select("id,config_json").execute().data or []):
                            if str(_p["id"]).startswith("__"):
                                continue
                            _steps = ((_p.get("config_json") or {}).get("robot_config", {}) or {}).get("steps", []) or []
                            if any(str((s or {}).get("操作", (s or {}).get("action", ""))) in
                                   ("ファイルをダウンロード", "download") for s in _steps):
                                _bots.append(_p["id"])
                    except Exception:
                        _bots = []
                    if not _bots:
                        st.info("📌 ダウンロード手順を持つロボットがまだありません。下で作れます"
                                "（申請用のロボットとは別に作ります。ログイン情報は使い回せます）。")

                    # 🎬 取り込みロボットは、このタブの中で作れるようにする
                    #    （申請用のロボットとは目的が違うので、作る場所も分けたほうが迷わない）
                    with st.expander("🎬 取り込みロボットを作る／録画をやり直す", expanded=not _bots):
                        # ロボット名はキャリア名をそのまま使う（同じ名前を2回入れさせない）。
                        # ただし同名のロボットが既にあると上書きしてしまうので、そのときだけ後ろに付ける。
                        _rb_name = str(_cur.get("取り込みロボット名", "")).strip()
                        if not _rb_name and _name.strip():
                            try:
                                _taken = {str(p["id"]) for p in
                                          (supabase.table("merchants").select("id").execute().data or [])}
                            except Exception:
                                _taken = set()
                            _rb_name = _name.strip()
                            if _rb_name in _taken:
                                _rb_name = f"{_name.strip()}_進捗取得"
                        if _rb_name:
                            st.caption(f"ロボット名：**{_rb_name}**（キャリア名から自動で決まります）"
                                       + ("　※同じ名前のロボットが既にあるため、後ろに付けました"
                                          if _rb_name != _name.strip() else ""))
                        else:
                            st.warning("先に「1. このキャリアの名前」を入れてください。")
                        _rb_url = st.text_input("サイトのURL（ログイン画面）", key="mk_bot_url",
                                                placeholder="https://xxx.cybozu.com/...")
                        _c1, _c2 = st.columns(2)
                        with _c1:
                            if st.button("🎬 録画を開始する（このPC）", key="mk_bot_rec",
                                         use_container_width=True):
                                if not _rb_url.strip():
                                    st.warning("先にURLを入れてください。")
                                else:
                                    try:
                                        import subprocess, sys
                                        subprocess.Popen([sys.executable, "-m", "playwright", "codegen",
                                                          _rb_url.strip()])
                                        st.success("ブラウザが開きます。ログイン → 検索 → "
                                                   "**ダウンロードボタンを押す**まで操作してください。"
                                                   "終わったら、録画ウィンドウのコードをコピーして下に貼ります。")
                                    except Exception as _e:
                                        st.error(f"録画を開始できませんでした（このPCで開いていない可能性）: {_e}")
                        with _c2:
                            st.caption("💡 パスワードは本物で入力してOKです（伏せ字にしてから保存します）。")
                        _rb_code = st.text_area("録画したコードを貼り付け", key="mk_bot_code", height=160)
                        st.caption("⚠️ 同じ名前で作り直すと、**手順書は新しい録画で置き換わります**"
                                   "（ログイン情報と二段階認証の設定は残ります）。"
                                   "うまくいかない箇所があるときは、ここで録画をやり直すのが早いです。")
                        if st.button("✨ 手順書を作る", key="mk_bot_make", type="primary"):
                            if not (_rb_name.strip() and _rb_code.strip()):
                                st.warning("ロボットの名前と、録画したコードの両方が必要です。")
                            elif not _rb_url.strip():
                                st.warning("サイトのURLを入れてください（ここが空だと実行できません）。")
                            elif not str(st.secrets.get("GEMINI_API_KEY", "")).strip():
                                st.error("接続キー GEMINI_API_KEY が未設定です。")
                            else:
                                try:
                                    import google.generativeai as genai
                                    _code, _nred = steps_ai.redact_passwords(_rb_code)
                                    if _nred:
                                        st.info(f"🔒 パスワード欄の入力 {_nred}件を伏せました。")
                                    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                                    _model = genai.GenerativeModel("gemini-2.5-flash")
                                    with st.spinner("🤖 手順書を作っています..."):
                                        _resp = _model.generate_content(
                                            steps_ai.build_prompt(_code, steps_ai.VALUE_RULE_INTAKE),
                                            generation_config={"response_mime_type": "application/json"})
                                    _steps = steps_ai.parse_steps(_resp.text)
                                    # 録画に入る「入力枠を選ぶだけのクリック」を落とす
                                    _steps = steps_ai.strip_redundant_field_clicks(_steps)
                                    # 最後にダウンロードのステップを足しておく（人が対象名だけ直せばよい状態にする）
                                    _steps.append({"順番": len(_steps) + 1, "いつ": "常に",
                                                   "操作": "ファイルをダウンロード",
                                                   "対象": "ダウンロード", "値": "", "ai_code": ""})
                                    supabase.table("merchants").upsert({
                                        "id": _rb_name.strip(), "name": _rb_name.strip(),
                                        "is_active": False, "connector_type": "playwright",
                                        "config_json": {"product_type": "進捗取り込み",
                                                        "needs_recording": True,
                                                        "robot_config": {"target_url": _rb_url.strip(),
                                                                         "steps": _steps,
                                                                         "skeleton": _steps,
                                                                         "stealth": True},
                                                        "spreadsheet": {}, "notifications": {},
                                                        "conditions": []}}).execute()
                                    # 作ったロボットを、このキャリアの設定にそのまま紐づける
                                    _robot = _rb_name.strip()
                                    st.success(f"✅「{_rb_name}」を作りました（{len(_steps)}手順）。"
                                               "最後に『ファイルをダウンロード』の手順を足してあります。"
                                               "このあと **下の「💾 このキャリアの設定を保存」を押す**と、"
                                               "このロボットが紐づきます。"
                                               "ダウンロードボタンの文言の調整とログイン情報の差し替えは、"
                                               "「📝 エントリー業務自動化」の司令室で行ってください。")
                                except Exception as _e:
                                    st.error(f"手順書を作れませんでした: {_e}")
                    _cur_bot = str(_cur.get("取り込みロボット名", "") or "")
                    if _bots:
                        _opts = ["（未選択）"] + _bots
                        _robot = st.selectbox("使うロボット（ダウンロード手順を録画したもの）", _opts,
                                              index=_opts.index(_cur_bot) if _cur_bot in _opts else 0,
                                              key="cfg_robot")
                        _robot = "" if _robot == "（未選択）" else _robot
                    else:
                        # ロボットは上で作れるので、名前を手打ちさせない
                        _robot = _cur_bot
                    st.warning("⚠️ この方法は**ブラウザを開くため、担当者のPCで動かす必要があります**"
                               "（クラウド版からは実行できません）。")

                    # 🔑🔐 録画のすぐ下で、ログイン情報と二段階認証まで設定できるようにする
                    #     （司令室へ移動せずに、このタブだけで一通り終わるように）
                    #     選択中のロボットが無くても、いま作ろうとしている名前のロボットが
                    #     すでにあるなら、そちらの設定を出す（作った直後にも設定できるように）。
                    _target_bot = _robot or _rb_name
                    _bot_row = None
                    if _target_bot:
                        try:
                            _bot_row = supabase.table("merchants").select("*").eq("id", _target_bot).execute().data
                        except Exception:
                            _bot_row = None
                    if _bot_row:
                        _bot_data = _bot_row[0]
                        _bot_cfg = _bot_data.get("config_json", {}) or {}
                        st.caption(f"↓ ロボット「{_target_bot}」の設定")
                        # 🌐 サイトのURL（空だと実行時に「URLが不正」で落ちるので、ここで直せるように）
                        _cur_url = str((_bot_cfg.get("robot_config", {}) or {}).get("target_url", "") or "")
                        _u1, _u2 = st.columns([4, 1])
                        with _u1:
                            _new_url = st.text_input("サイトのURL（ログイン画面）", value=_cur_url,
                                                     key=f"boturl_{_target_bot}",
                                                     placeholder="https://xxx.example.com/login")
                        with _u2:
                            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                            if st.button("💾 URL保存", key=f"saveurl_{_target_bot}", use_container_width=True):
                                _bot_cfg.setdefault("robot_config", {})["target_url"] = _new_url.strip()
                                _bot_data["config_json"] = _bot_cfg
                                supabase.table("merchants").upsert(_bot_data).execute()
                                st.success("保存しました。")
                                st.rerun()
                        if not _cur_url:
                            st.warning("⚠️ URLが未設定です。これが無いと実行できません（上で入れて保存してください）。")
                        robot_settings_ui.render_login_secrets(_target_bot, _bot_cfg, _bot_data)
                        robot_settings_ui.render_auth_code_settings(_target_bot, _bot_cfg, _bot_data)

                        # 📝 手順書はここで直せるようにする（いらない行を消す・テストする）
                        with st.expander("📝 このロボットの手順書"):
                            _bsteps = (_bot_cfg.get("robot_config", {}) or {}).get("steps", []) or []
                            if not _bsteps:
                                st.info("まだ手順がありません。上で録画してください。")
                            else:
                                _view = pd.DataFrame([
                                    {"順番": i + 1,
                                     "いつ": s.get("いつ", s.get("condition", "常に")),
                                     "操作": s.get("操作", s.get("action", "")),
                                     "対象": s.get("対象", s.get("target_description", "")),
                                     "値": s.get("値", s.get("value", ""))}
                                    for i, s in enumerate([x for x in _bsteps if x])])
                                st.caption("いらない手順は、行の左端をクリックして選び、"
                                           "表の右上に出る🗑（ゴミ箱）で削除できます。"
                                           "編集したら「💾 手順書を保存」を押してください。")
                                _edited_steps = st.data_editor(
                                    _view, use_container_width=True, hide_index=True,
                                    num_rows="dynamic",          # 行の削除・追加ができる
                                    key=f"stepsed_{_target_bot}",
                                    column_config={
                                        "順番": st.column_config.NumberColumn(disabled=True, width="small",
                                                                            help="保存すると振り直されます"),
                                        "操作": st.column_config.SelectboxColumn(
                                            options=["文字を入力", "クリック", "選択", "チェック",
                                                     "人の操作を待つ", "ファイルをダウンロード", "認証コードを入力"]),
                                    })
                                _has_dl = any(str(v) in ("ファイルをダウンロード", "download")
                                              for v in _edited_steps["操作"].fillna("").tolist())
                                if not _has_dl:
                                    st.warning("⚠️ 「ファイルをダウンロード」の手順がありません。"
                                               "これが無いとファイルを受け取れません。")
                                _e1, _e2 = st.columns(2)
                                with _e1:
                                    if st.button("💾 手順書を保存", key=f"savesteps_{_target_bot}",
                                                 type="primary", use_container_width=True):
                                        # 削除された行を除いて組み直す。録画のセレクタ(ai_code)を
                                        # 失わないよう、元の手順は「順番」で突き合わせる。
                                        _clean = [x for x in _bsteps if x]
                                        _keep = []
                                        for _, _row in _edited_steps.iterrows():
                                            _n = _row.get("順番")
                                            _orig = {}
                                            try:
                                                _idx = int(_n) - 1
                                                if 0 <= _idx < len(_clean):
                                                    _orig = dict(_clean[_idx])
                                            except Exception:
                                                _orig = {}
                                            # 空セルは NaN で入ってくる。そのまま保存すると
                                            # JSONに変換できずエラーになるので、空文字に直す。
                                            def _txt(v):
                                                return "" if (v is None or pd.isna(v)) else str(v)
                                            _orig["いつ"] = _txt(_row["いつ"]) or "常に"
                                            _orig["操作"] = _txt(_row["操作"])
                                            _orig["対象"] = _txt(_row["対象"])
                                            _orig["値"] = _txt(_row["値"])
                                            _orig["順番"] = len(_keep) + 1
                                            _keep.append(_orig)
                                        # 保存前にもう一度、NaN や数値が混ざっていないか確かめる
                                        for _st in _keep:
                                            for _k, _v in list(_st.items()):
                                                if isinstance(_v, float) and pd.isna(_v):
                                                    _st[_k] = ""
                                        _bot_cfg.setdefault("robot_config", {})["steps"] = _keep
                                        _bot_data["config_json"] = _bot_cfg
                                        supabase.table("merchants").upsert(_bot_data).execute()
                                        st.success(f"保存しました（{len(_keep)}手順）。")
                                        st.rerun()
                                with _e2:
                                    if st.button("🧪 テスト実行（このPCで動かす）", key=f"teststeps_{_target_bot}",
                                                 use_container_width=True):
                                        _dir = intake_runner.intake_dir(_name.strip() or _target_bot)
                                        with st.spinner("ブラウザを開いて動かしています..."):
                                            try:
                                                _ok, _log, _got = intake_runner.run_download_robot(
                                                    _target_bot, _dir, keep=_keep_files())
                                            except Exception as _e:
                                                _ok, _log, _got = False, str(_e)[:300], None
                                        if _ok and _got:
                                            st.success(f"✅ ダウンロードできました：`{os.path.basename(_got)}`")
                                            st.caption(f"保存先：{_dir}")
                                            _archive_download(_name.strip() or _target_bot, _got)
                                            try:
                                                with open(_got, "rb") as _fh:
                                                    st.download_button("⬇️ 取れたファイルを見る", _fh.read(),
                                                                       file_name=os.path.basename(_got),
                                                                       use_container_width=True)
                                            except Exception:
                                                pass
                                        else:
                                            st.error("❌ ダウンロードできませんでした。下のログを確認してください。")
                                        with st.expander("実行ログ"):
                                            st.text_area("ログ", value=_log or "(なし)", height=220,
                                                         key=f"testlog_{_target_bot}")
                            st.caption("細かい修正（ai_codeなど）はエントリー業務の司令室で行えます。")
                            if st.button("⚙️ この手順書を司令室で開く", key=f"open_room_{_target_bot}",
                                         use_container_width=True):
                                st.session_state.editing_project = _target_bot
                                st.session_state.view = "project_room"
                                st.switch_page("pages/2_📝_エントリー業務自動化.py")
                    else:
                        st.caption("※上でロボットを作ると、ここにログイン情報と二段階認証の設定が出ます。")
                else:
                    st.caption("📌 実行のときに、この画面でファイルを選んで取り込みます。"
                               "メールでもサイトでもない、手渡しのファイル向けです。")

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
                        _tabs = _list_tabs(gc, _sheet_id)
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
                _skip = st.number_input("ファイルの見出しは何行目まで？", min_value=1, max_value=10,
                                        value=max(1, int(str(_cur.get(
                                            "ファイルの見出し行数",
                                            _cur.get("捨てる先頭行数", "1")) or "1").strip() or 1)),
                                        key="cfg_skip",
                                        help="1行目が見出しなら 1。上にタイトル行があって"
                                             "2行目までが見出しなら 2")
                st.caption("💡 **1行目が見出し（No. 申込日 …）なら「1」**。"
                           "上にタイトルなどが入っていて2行目までが見出しなら「2」。")
                _keep = st.number_input("貼り付け先シートの見出しは何行？", min_value=1, max_value=10,
                                        value=int(str(_cur.get("貼り付け先の見出し行数", "1") or "1").strip() or 1),
                                        key="cfg_keep",
                                        help="その行数までは残したまま、その下のデータだけを入れ替えます")
                _pw = st.text_input("解錠パスワードの名前（パスワード付き添付のとき）",
                                    value=str(_cur.get("解錠パスワードの名前", "")), key="cfg_pw",
                                    placeholder="例：ドコモ進捗パス（空欄でOK）")
                st.caption("⚠️ パスワードそのものは書かないでください。"
                           "司令室の「🔑 ログイン情報」で登録した**名前**を入れます。")
                if _method.startswith("サイト"):
                    st.caption("🔓 サイトから落とす方式では、ふつう空欄でOKです"
                               "（鍵がかかっているのはメール添付のファイルなので）。"
                               "サイトのログインに使うIDとパスワードは、上の録画のところで設定します。")

                # ☁️ Salesforceへの投入も、このキャリアの設定として持つ
                st.markdown("**5. Salesforceへの投入**")
                # Salesforceの画面では日本語しか見ないので、「案件 (Opportunity)」の形で選べるようにする
                _objs = sf_ui._object_options()
                _olabels = sf_ui.object_labels()
                _obj_cur = str(_cur.get("オブジェクトAPI名", "") or "Opportunity")
                _obj = st.selectbox("投入先（オブジェクト）", _objs,
                                    index=_objs.index(_obj_cur) if _obj_cur in _objs else 0,
                                    key="cfg_obj", help="ふつうは 案件 です",
                                    format_func=lambda n: f"{_olabels.get(n, n)}（{n}）")
                _keys = sf_ui._key_field_options(_obj) or ["Id"]
                _flabels = sf_ui.field_labels(_obj)
                _key_cur = str(_cur.get("外部IDキー", "") or "Id")
                _key = st.selectbox("照合キー（どの項目で突き合わせるか）", _keys,
                                    index=_keys.index(_key_cur) if _key_cur in _keys else 0,
                                    key="cfg_key",
                                    format_func=lambda n: f"{_flabels.get(n, n)}（{n}）",
                                    help="案件ID＝すでにある案件の更新のみ。"
                                         "回線登録番号・ガスID・電力IDなど＝無ければ新規作成")
                st.caption("💡 選択肢はSalesforceから取ってきた実物です"
                           "（Data Loaderで選ぶ項目と同じ並び）。")

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
                            row = {"キャリア名": _name.strip(), "取り込み方法": _method,
                                   "取り込みロボット名": _robot, "Gmail検索条件": _query.strip(),
                                   "添付の絞り込み(正規表現)": _files, "有効": "TRUE" if _active else "FALSE",
                                   "貼り付け先スプシID": _sheet_id, "元データシート名": _src,
                                   "投入用シート名": _dst, "確認用シート名": _chk,
                                   "解錠パスワードの名前": _pw.strip(), "ファイルの見出し行数": str(int(_skip)),
                                   "貼り付け先の見出し行数": str(int(_keep)),
                                   "オブジェクトAPI名": str(_cur.get("オブジェクトAPI名", "")),
                                   "外部IDキー": str(_cur.get("外部IDキー", ""))}
                            base = df[df["キャリア名"] != _name.strip()]
                            merged = pd.concat([base, pd.DataFrame([row])], ignore_index=True)
                            try:
                                n = _write_config_rows(gc, cfg["settings_url"], merged)
                                st.cache_data.clear()
                                st.success(f"「{_name}」を保存しました（全{n}件）。")
                                st.session_state.pop("prog_editing", None)
                                st.rerun()
                            except Exception as e:
                                st.error(f"保存できませんでした: {e}")
                with b2:
                    if not _is_new and st.button("🗑 このキャリアを削除", key="del_cfg",
                                                 use_container_width=True):
                        try:
                            _write_config_rows(gc, cfg["settings_url"], df[df["キャリア名"] != _pick])
                            st.cache_data.clear()
                            st.success(f"「{_pick}」を削除しました。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"削除できませんでした: {e}")

                if not _is_new and _name.strip():
                    st.markdown("---")
                    # 🧪 段階ごとのテストだと「最後まで繋がるか」が分からないので、
                    #    実データを書き換えずに、取り込み〜貼り付け直前までを通しで試せるようにする。
                    with st.expander("🧪 取り込み〜貼り付けを通しで試す（書き込みません）"):
                        st.caption("ファイルを取ってきて、中身を読んで、貼り付け先の見出しと照合するところまでを"
                                   "本番と同じ手順で行います。**シートには書き込みません**ので、安全に試せます。")
                        # 🔁 サイト方式は、ここでロボットも動かして最初から通す。
                        #    ただし毎回ログインし直すと認証コードを使い、回数制限に当たるので、
                        #    すでに落としたファイルがあるときは、それを使い回すか選べるようにする。
                        _redl = False
                        if _method.startswith("サイト"):
                            _have = intake_runner.last_download(intake_runner.intake_dir(_name.strip())) if _robot else None
                            _redl = st.checkbox(
                                "ロボットを動かしてダウンロードからやり直す"
                                "（数分かかり、メールの認証コードを1回使います）",
                                value=not bool(_have), key=f"redl_{_name}")
                            if _have and not _redl:
                                st.caption(f"前回落としたファイルを使います：{os.path.basename(_have)}")
                        if st.button("🧪 通しで試す", key=f"dryrun_{_name}", use_container_width=True):
                            _row_try = dict(_cur)
                            _row_try.update({"キャリア名": _name.strip(),
                                             "貼り付け先スプシID": _sheet_id,
                                             "元データシート名": _src,
                                             "ファイルの見出し行数": str(int(_skip)),
                                             "貼り付け先の見出し行数": str(int(_keep)),
                                             "解錠パスワードの名前": _pw.strip()})
                            _local_try = None
                            _no_file = False
                            if _method.startswith("サイト"):
                                # サイト方式は、このPCに落としたファイルを使う（Driveは見に行かない）
                                if not _robot:
                                    _no_file = True
                                    st.warning("先に、上の「🤖 取り込みロボット」でどのロボットを使うか選んでください。")
                                    _newest_try = None
                                else:
                                    _dir_try = intake_runner.intake_dir(_name.strip())
                                    _newest_try = intake_runner.last_download(_dir_try)
                                    if _redl or not _newest_try:
                                        with st.spinner("ブラウザを開いてダウンロードしています（数分かかります）..."):
                                            try:
                                                _dok, _dlog, _newest_try = \
                                                    intake_runner.run_download_robot(_robot, _dir_try, keep=_keep_files())
                                            except Exception as _e:
                                                _dok, _dlog, _newest_try = False, str(_e)[:300], None
                                        if not _newest_try:
                                            _no_file = True
                                            st.error("❌ ダウンロードできませんでした。下のログを見てください。")
                                            with st.expander("実行ログ"):
                                                st.text_area("ログ", value=_dlog or "(なし)", height=220,
                                                             key=f"dryrunlog_{_name}")
                                if _newest_try:
                                    with open(_newest_try, "rb") as _fh:
                                        _local_try = (os.path.basename(_newest_try), _fh.read())
                                    st.caption(f"使うファイル：{os.path.basename(_newest_try)}")
                                    st.caption(f"保存先：{os.path.dirname(_newest_try)}")
                                    if _redl:
                                        _archive_download(_name.strip(), _newest_try)
                            _drive_try = None
                            if _local_try is None and not _no_file:
                                try:
                                    _drive_try = intake_runner.drive_client(
                                        st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
                                except Exception as _e:
                                    st.error(f"Driveに接続できません: {_e}")
                            if _local_try is not None or _drive_try is not None:
                                with st.spinner("試しています..."):
                                    _res_try = intake_runner.run_one(
                                        gc, _drive_try, cfg.get("intake_folder_id", ""), _row_try,
                                        local_file=_local_try, dry_run=True)
                                _msg_try = str(_res_try.get("結果", ""))
                                (st.success if _msg_try.startswith("🧪") else st.error)(_msg_try)
                                if _res_try.get("見出し"):
                                    st.caption(f"ファイルの見出し：{_res_try['見出し']}")
                                    st.caption(f"先頭の行：{_res_try['先頭の行']}")
                                    st.info("ここまで確認できました。実際に貼るときは、"
                                            "このページ下の「🔄 まとめて反映する」を押してください。")
                                # 📄 見出しがズレていたら、ファイルの先頭を見せて直せるようにする
                                if _res_try.get("ファイルの先頭"):
                                    st.markdown("**ファイルの先頭（そのまま）**")
                                    _pv = _res_try["ファイルの先頭"]
                                    st.dataframe(pd.DataFrame(
                                        [[f"{_i+1}行目"] + [str(c) for c in _r[:8]] for _i, _r in enumerate(_pv)]),
                                        use_container_width=True, hide_index=True)
                                    st.warning("見出し（No. 申込日 …）が何行目にあるか見てください。\n\n"
                                               "**その行番号を、上の『ファイルの見出しは何行目まで？』に入れてください。**")
                                    st.caption("貼り付け先シートの見出し："
                                               + "／".join(str(h) for h in (_res_try.get("シートの見出し") or [])[:10]))

                    with st.expander("☁️ マッピングとSalesforceへの投入", expanded=False):
                        sf_ui.render_carrier_sf(
                            gc, cfg.get("settings_url", ""), _name.strip(),
                            _sheet_id, str(_cur.get("投入用シート名", "") or _dst),
                            _obj, _key, key_prefix="csf")

                with st.expander("📋 いまの設定を一覧で見る"):
                    st.dataframe(df, use_container_width=True, hide_index=True)

# ==========================================
# 進捗の確認（スプシを開かずに、アプリで中身を見る）
# ==========================================
with st.container(border=True):
    theme.section_title("👀", "進捗を確認する")
    if not (gc and cfg.get("settings_url")):
        st.info("先に「⚙️ 進捗設定」とキャリアの登録をしてください。")
    else:
        try:
            _rows = _read_config_rows(gc, cfg["settings_url"])
        except Exception:
            _rows = pd.DataFrame(columns=CONFIG_HEADERS)
        _names = [r for r in _rows["キャリア名"].tolist() if str(r).strip()]
        if not _names:
            st.info("キャリアを登録すると、ここで中身を確認できます。")
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

