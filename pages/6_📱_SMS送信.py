"""
📱 SMS送信（プッシュプロ一括送信）

パターン（送る文面・条件のちがい）ごとに設定を登録し、「送信をはじめる」で次の順に進む：

  ① シートを更新     … SFコネクタで、登録したシートを順に更新する
  ② 中身をチェック   … 登録したルールで、直すべき行を一覧に出す（ここは人が直す）
  ③ CSVを用意       … スプシのGASが書き出したCSVを受け取る（毎回おなじ名前で置く）
  ④ 一括送信        … 録画したロボットが、そのCSVをプッシュプロに入れて送信する

【録画は「⚙️ その他設定」で一度だけ】
SFコネクタの更新もプッシュプロの送信も、どのスプシでも押す場所は同じ。
違うのは「どのシートを選ぶか」「どのファイルを渡すか」だけなので、
共通ロボットを1台ずつ作れば、ここでは**シート名を選ぶだけ**でよい。

【二重送信を防ぐ】
プッシュプロは一括送信なので、送ってしまった分は取り消せない。そこで
  ・送る前に②で弾く（間違った行を送らない）
  ・送った宛先を記録し、次回のCSVから自動で外せるようにする
  ・途中で止まっても「送ったかもしれない」は送信済みに寄せる（二重送信のほうが怖い）
"""
import os

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import characters as ch
import common_robots
import sms_runner
import theme

st.set_page_config(page_title="SMS送信 - エンカンAI", layout="wide")

theme.inject_theme()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📱", "SMSを一括で送る",
                  "シートを更新 → 中身をチェック → CSVを用意 → プッシュプロで一括送信、までを一本にします。",
                  color=c["color"])


# ==========================================
# 🔌 つなぎこみ
# ==========================================
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

SETTINGS_ID = "__sms__"          # ロボット一覧には出さない予約行（id が __ で始まる）
_GAS_GUIDE = """1. スプレッドシート → **拡張機能 → Apps Script**
2. いまのコードの**いちばん下**に、`gas/SMS_CSV書き出しWebAPI.gs` の中身を貼り付ける
   （`EXPORT_CONFIG` / `buildCsvString_` は既にあるので消さないこと）
3. `API_TOKEN` を**長い合言葉**に書き換える
4. 右上 **デプロイ → 新しいデプロイ → ウェブアプリ**
   - 次のユーザーとして実行：**自分**
   - アクセスできるユーザー：**全員**
5. 出てきた `.../macros/s/AKfy.../exec` を下に貼る

⚠️ 「全員」は **URLを知っていれば誰でも叩ける**という意味です。合言葉を必ず設定してください（合わない呼び出しはGASが断ります）。"""

CSV_SOURCES = ["GASのURLを叩いて受け取る（推奨）",
               "GASがDriveに書き出したものを使う",
               "ロボットにGASのボタンを押させて受け取る",
               "アプリがシートから作る"]


def _load_settings() -> dict:
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}


def _save_settings(cfg: dict):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（SMS送信の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cfg}).execute()


@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    import gspread
    import json
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


def _sa_json() -> str:
    try:
        return st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        return ""


@st.cache_data(ttl=120, show_spinner=False)
def _tab_gids(_gc, sheet_url: str) -> dict:
    """タブ名 → gid。「そのシートを開く」URLを組み立てるのに使う。"""
    sh = _gc.open_by_url(sheet_url) if sheet_url.startswith("http") else _gc.open_by_key(sheet_url)
    return {w.title: w.id for w in sh.worksheets()}


def _tab_names(_gc, sheet_url: str):
    return list(_tab_gids(_gc, sheet_url).keys())


def _gids_of(pat: dict) -> dict:
    try:
        return _tab_gids(gc, pat.get("sheet_url", "")) if gc else {}
    except Exception:
        return {}


def _patterns(cfg: dict):
    return cfg.get("patterns", []) or []


def _find(cfg: dict, name: str):
    for p in _patterns(cfg):
        if p.get("name") == name:
            return p
    return None


def _upsert_pattern(cfg: dict, pat: dict, old_name: str = ""):
    pats = _patterns(cfg)
    key = old_name or pat.get("name")
    for i, p in enumerate(pats):
        if p.get("name") == key:
            pats[i] = pat
            break
    else:
        pats.append(pat)
    cfg["patterns"] = pats
    return cfg


def _robot_picker(label: str, role_key: str, current: str, key: str):
    """使う共通ロボットを選ぶ。ふつうは既定のまま触らなくてよい。"""
    robots = common_robots.list_robots(supabase)
    default = common_robots.ROLES[role_key]["name"]
    cur = current or default
    opts = ["（使わない）"] + robots
    if cur and cur not in opts:
        opts.append(cur)
    picked = st.selectbox(label, opts, index=opts.index(cur) if cur in opts else 0, key=key)
    picked = "" if picked == "（使わない）" else picked
    if picked and picked not in robots:
        st.warning(f"⚠️ ロボット「{picked}」はまだ録画されていません。"
                   "「⚙️ その他設定」の🤖共通ロボットの登録で作ってください（1回でOK）。")
    return picked


cfg = _load_settings()
gc = _get_gspread_client()

st.session_state.setdefault("sms_view", "list")
st.session_state.setdefault("sms_pattern", "")

if not gc:
    st.warning("🔑 接続キー **GOOGLE_SERVICE_ACCOUNT_JSON** が未設定です。"
               "シートの読み取り・CSVの受け取りができません（管理者に設定を依頼してください）。")


# ==========================================
# 📋 画面1：パターン一覧
# ==========================================
if st.session_state.sms_view == "list":
    ch.guide("operate",
             "SMSは<b>パターンごと</b>に登録するよ。録画は「⚙️ その他設定」で1回だけ。"
             "ここでは<b>シート名を選ぶだけ</b>で動くようにしてあるね。")

    top1, top2 = st.columns([1, 3])
    with top1:
        if st.button("＋ パターンを追加", type="primary", use_container_width=True):
            st.session_state.sms_view = "edit"
            st.session_state.sms_pattern = ""
            st.rerun()
    with top2:
        _have = common_robots.list_robots(supabase)
        _need = [r["name"] for r in common_robots.ROLES.values() if r["name"] not in _have]
        if _need:
            st.warning("まだ録画していない共通ロボットがあります：" + "、".join(_need))
        st.page_link("pages/8_⚙️_その他設定.py", label="🤖 共通ロボットの登録へ")

    pats = _patterns(cfg)
    if not pats:
        st.info("まだパターンがありません。「＋ パターンを追加」から、最初の1つを登録しましょう。")
    for p in pats:
        with st.container(border=True):
            a, b, d = st.columns([3, 3, 2])
            with a:
                st.markdown(f"#### 📱 {p.get('name', '(名前なし)')}")
                st.caption(p.get("memo", "") or "　")
            with b:
                ready = bool(p.get("sheet_url")) and bool(p.get("send_robot"))
                st.markdown("<span class='status-active'>準備OK</span>" if ready
                            else "<span class='status-inactive'>設定が足りません</span>",
                            unsafe_allow_html=True)
                _got = sms_runner.today_csv(p.get("name", ""))
                _made = sms_runner.csv_made_at(p.get("name", ""))
                st.caption(f"今日のCSV：{'あり' if _got else 'まだ'}"
                           + (f"（用意：{_made}）" if _made else ""))
                _sent = sms_runner.load_sent(p.get("name", ""))
                if _sent:
                    st.caption(f"送信の記録：{len(_sent)}件ぶん")
            with d:
                if st.button("▶ 送信をはじめる", key=f"go_{p.get('name')}", use_container_width=True,
                             type="primary", disabled=not ready):
                    st.session_state.sms_view = "run"
                    st.session_state.sms_pattern = p.get("name", "")
                    st.rerun()
                if st.button("⚙️ 設定を直す", key=f"ed_{p.get('name')}", use_container_width=True):
                    st.session_state.sms_view = "edit"
                    st.session_state.sms_pattern = p.get("name", "")
                    st.rerun()

    st.divider()
    st.caption("💻 シートの更新と一括送信はブラウザを開くため、**担当者のPCで開いているとき**だけ実行できます。")


# ==========================================
# ⚙️ 画面2：パターンの設定
# ==========================================
elif st.session_state.sms_view == "edit":
    old_name = st.session_state.sms_pattern
    pat = _find(cfg, old_name) or {
        "name": "", "memo": "", "sheet_url": "",
        "refresh_tabs": [], "refresh_robot": common_robots.ROLES["refresh"]["name"],
        "checks": [],
        "csv_source": CSV_SOURCES[0],
        "gas_url": "", "gas_token": "", "gas_sheet": "", "gas_keep_drive": True,
        "drive_root": sms_runner.DRIVE_SMS_ROOT, "drive_label": "",
        "export_robot": common_robots.ROLES["export"]["name"],
        "csv_tab": "", "csv_encoding": "Shift_JIS", "skip_empty_col": "",
        "send_robot": common_robots.ROLES["send"]["name"],
        "dedup_days": 0,
    }

    if st.button("⬅ 一覧に戻る"):
        st.session_state.sms_view = "list"
        st.rerun()

    st.markdown(f"### ⚙️ {'パターンを追加' if not old_name else f'「{old_name}」の設定'}")
    st.caption("🎬 録画（手順の登録）は「⚙️ その他設定」の🤖共通ロボットの登録でまとめて行います。"
               "ここでは**どのシートを使うか**だけ決めます。")

    # --- 1. 名前とスプシ ---
    with st.container(border=True):
        theme.section_title("1️⃣", "パターンの名前と、つなぐスプレッドシート")
        name = st.text_input("パターンの名前", value=pat.get("name", ""),
                             placeholder="例：長期不在1回目")
        memo = st.text_input("メモ（何を送るパターンか）", value=pat.get("memo", ""))
        sheet_url = st.text_input("スプレッドシートのURL", value=pat.get("sheet_url", ""),
                                  placeholder="https://docs.google.com/spreadsheets/d/...")
        st.caption("※ サービスアカウントのメールアドレスを、このスプシの**閲覧者**に追加してください。")

    tabs = []
    if gc and sheet_url.strip():
        try:
            tabs = _tab_names(gc, sheet_url.strip())
        except Exception as e:
            st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")

    # --- 2. リフレッシュ ---
    with st.container(border=True):
        theme.section_title("2️⃣", "SFコネクタで更新するシート（複数えらべます）")
        st.caption("選んだシートを、**ブラウザを1回だけ開いて**上から順に更新します。"
                   "開くのは、上の1️⃣で登録した**このパターンのスプレッドシート**です"
                   "（録画したときのスプシではありません）。")
        if tabs:
            _cur = [t for t in (pat.get("refresh_tabs", []) or []) if t in tabs]
            refresh_tabs = st.multiselect("更新するシート", tabs, default=_cur)
        else:
            refresh_tabs = [t.strip() for t in
                            st.text_input("更新するシート（カンマ区切り）",
                                          value="、".join(pat.get("refresh_tabs", []) or [])
                                          ).replace("、", ",").split(",") if t.strip()]
            st.caption("※ スプシURLを入れると、シート名をプルダウンで選べます。")
        refresh_robot = _robot_picker("使うロボット", "refresh",
                                      pat.get("refresh_robot", ""), "sms_refresh_sel")

    # --- 3. チェックのルール ---
    with st.container(border=True):
        theme.section_title("3️⃣", "送る前のチェック（直すべき行を洗い出す）")
        st.caption("ここに書いたルールに引っかかった行が、送信前に一覧で出ます。")
        with st.expander("📖 ルールの意味"):
            for k, v in sms_runner.RULES.items():
                st.markdown(f"- **{k}**：{v}")
        cdf = pd.DataFrame(pat.get("checks", []) or [],
                           columns=["シート", "列", "ルール", "値", "メモ"])
        if cdf.empty:
            cdf = pd.DataFrame([{"シート": "", "列": "", "ルール": "空はNG", "値": "", "メモ": ""}])
        checks_edited = st.data_editor(
            cdf, num_rows="dynamic", use_container_width=True, key="sms_checks",
            column_config={
                "シート": (st.column_config.SelectboxColumn(options=tabs) if tabs
                           else st.column_config.TextColumn()),
                "ルール": st.column_config.SelectboxColumn(options=list(sms_runner.RULES.keys())),
                "値": st.column_config.TextColumn(help="「この文字を含む」などで使う指定。／で区切って複数。"),
                "メモ": st.column_config.TextColumn(help="担当者に出す一言（例：番号の抜けを埋めてください）"),
            })

    # --- 4. CSVの用意のしかた ---
    with st.container(border=True):
        theme.section_title("4️⃣", "プッシュプロに入れるCSVの用意")
        _cur_src = pat.get("csv_source", CSV_SOURCES[0])
        csv_source = st.radio("どうやって用意しますか", CSV_SOURCES,
                              index=CSV_SOURCES.index(_cur_src) if _cur_src in CSV_SOURCES else 0)
        st.caption("💡 電話番号の頭の0や文字コード（Shift_JIS）の整形は、すでにスプシのGASがやっています。"
                   "同じ整形をアプリでもう一度書くと、片方だけ直して食い違うので、"
                   "できあがったCSVをそのまま使うのがいちばん安全です。")
        st.info(f"📁 置き場所：`取り込みファイル/SMS送信用/{name.strip() or '（パターン名）'}/"
                f"{sms_runner.CSV_NAME}`　"
                "**毎回この同じ名前で上書き**します。だから録画で選んだファイルのまま動きます"
                "（日付つきの控えは `履歴` フォルダに残ります）。")

        gas_url = pat.get("gas_url", "")
        gas_token = pat.get("gas_token", "")
        gas_sheet = pat.get("gas_sheet", "")
        gas_keep_drive = bool(pat.get("gas_keep_drive", True))
        drive_root = pat.get("drive_root", sms_runner.DRIVE_SMS_ROOT)
        drive_label = pat.get("drive_label", "")
        export_robot = pat.get("export_robot", "")
        csv_tab = pat.get("csv_tab", "")
        csv_encoding = pat.get("csv_encoding", "Shift_JIS")
        skip_empty_col = pat.get("skip_empty_col", "")

        if csv_source == CSV_SOURCES[0]:
            # 🔗 GASを「ウェブアプリ」としてデプロイしておけば、URLを叩くだけでCSVが返る。
            #    録画も、Driveの共有設定も要らない。CSVを作るのはこれまでどおりGAS。
            st.caption("スプシの Apps Script を **ウェブアプリとしてデプロイ**しておけば、"
                       "URLを叩くだけで、サイドバーのボタンとまったく同じCSVが返ってきます。"
                       "録画も、Driveの共有設定も要りません。")
            with st.expander("📖 GAS側の準備（初回だけ・スプシごと）"):
                st.markdown(_GAS_GUIDE)
            gas_url = st.text_input("GASのウェブアプリURL", value=gas_url,
                                    placeholder="https://script.google.com/macros/s/AKfy.../exec")
            # 🔑 合言葉はアプリが作る。人に考えさせない。
            gas_token = str(gas_token or "").strip()
            if gas_url.strip() and not gas_token:
                import secrets as _secrets
                gas_token = st.session_state.setdefault("sms_token_new",
                                                        _secrets.token_urlsafe(24))
            if gas_token:
                st.markdown("**合言葉（アプリが作りました。1回だけスクリプトに貼ってください）**")
                st.code(gas_token, language=None)
                st.caption("👆 これを Apps Script の "
                           "`const API_TOKEN = 'ここに長い合言葉を書く';` の "
                           "**`ここに長い合言葉を書く` と入れ替えて**ください（`'` は消さない）。"
                           "そのあと **デプロイ → デプロイを管理 → 鉛筆 → 新バージョン → デプロイ**。")
                with st.expander("すでに別の合言葉を決めてある場合はこちら"):
                    _man = st.text_input("スクリプトに書いてある合言葉", value="",
                                         type="password", key="sms_token_manual")
                    if _man.strip():
                        gas_token = _man.strip()
            g1, _g2 = st.columns([1, 2])
            with g1:
                if st.button("🔌 つながるか試す", use_container_width=True):
                    if not gas_url.strip():
                        st.warning("URLを入れてください。")
                    else:
                        ok, msg = sms_runner.check_gas_csv(gas_url.strip(), gas_token.strip())
                        (st.success if ok else st.error)(msg)
                        if ok:
                            st.session_state["sms_gas_sheets"] = sms_runner.gas_sheet_names(
                                gas_url.strip(), gas_token.strip())
            _gsheets = st.session_state.get("sms_gas_sheets") or []
            if _gsheets:
                _opts = _gsheets + ([gas_sheet] if gas_sheet and gas_sheet not in _gsheets else [])
                gas_sheet = st.selectbox("CSVにするシート（GASの EXPORT_CONFIG の名前）", _opts,
                                         index=_opts.index(gas_sheet) if gas_sheet in _opts else 0)
            else:
                gas_sheet = st.text_input("CSVにするシート（GASの EXPORT_CONFIG の名前）",
                                          value=gas_sheet, placeholder="例：CSV／1回目CSV")
            gas_keep_drive = st.checkbox("これまでどおり Drive にも控えを残す", value=gas_keep_drive)
        elif csv_source == CSV_SOURCES[1]:
            drive_root = st.text_input("DriveのSMS送信用フォルダID", value=drive_root)
            _labels = sms_runner.KNOWN_LABELS + ([drive_label] if drive_label and
                                                 drive_label not in sms_runner.KNOWN_LABELS else [])
            drive_label = st.selectbox("ファイル名の頭（GASの label）", _labels + ["（自分で入力）"],
                                       index=_labels.index(drive_label) if drive_label in _labels else 0)
            if drive_label == "（自分で入力）":
                drive_label = st.text_input("ファイル名の頭を入力", value=pat.get("drive_label", ""))
            st.caption("GASは `SMS送信用/yyyy/M月/d/<この頭>_yyyyMMdd_HHmm.csv` に置きます。"
                       "その日のフォルダから、この頭で始まるいちばん新しいファイルを取ってきます。")
            st.caption("※ このDriveフォルダを、サービスアカウントに**閲覧者**として共有してください。")
        elif csv_source == CSV_SOURCES[2]:
            export_robot = _robot_picker("使うロボット（GASのCSV書き出し）", "export",
                                         export_robot, "sms_export_sel")
            st.caption("※ サイドバーはスプシの中の小さな画面（iframe）なので、録画がうまく"
                       "いかないことがあります。GASのURLを叩く方法のほうが確実です。")
        else:
            e1, e2 = st.columns(2)
            with e1:
                if tabs:
                    csv_tab = st.selectbox("CSVにするシート", tabs,
                                           index=tabs.index(csv_tab) if csv_tab in tabs else 0)
                else:
                    csv_tab = st.text_input("CSVにするシート", value=csv_tab)
            with e2:
                enc_opts = list(sms_runner.ENCODINGS.keys())
                csv_encoding = st.selectbox("文字コード", enc_opts,
                                            index=enc_opts.index(csv_encoding)
                                            if csv_encoding in enc_opts else 0)
            skip_empty_col = st.text_input("この列が空の行は送らない（任意）", value=skip_empty_col,
                                           placeholder="例：連絡先")
            st.warning("⚠️ この方法は、GASがやっている整形（電話番号の頭の0を足すなど）を通りません。"
                       "GASと同じ結果になるか、最初の1回は必ず見比べてください。")

    # --- 5. 送信と、二重送信の防止 ---
    with st.container(border=True):
        theme.section_title("5️⃣", "一括送信と、二重送信の防止")
        send_robot = _robot_picker("使うロボット（プッシュプロ）", "send",
                                   pat.get("send_robot", ""), "sms_send_sel")
        st.caption("プッシュプロは**一括送信**なので、送ってしまった分は取り消せません。"
                   "そこで、送った宛先（CSVの1列目）を記録しておき、次のCSVから外せるようにします。")
        _dd = int(pat.get("dedup_days", 0) or 0)
        dedup_days = st.number_input(
            "この日数以内に送った宛先は、二重送信とみなす（0＝一度でも送ったら二度と送らない）",
            min_value=0, max_value=365, value=_dd)

    st.divider()
    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button("💾 このパターンを保存", type="primary", use_container_width=True):
            if not name.strip():
                st.warning("パターンの名前を入れてください。")
            else:
                checks = [r for r in checks_edited.fillna("").to_dict("records")
                          if str(r.get("シート", "")).strip() and str(r.get("列", "")).strip()]
                pat_new = {"name": name.strip(), "memo": memo.strip(),
                           "sheet_url": sheet_url.strip(),
                           "refresh_tabs": list(refresh_tabs), "refresh_robot": refresh_robot,
                           "checks": checks, "csv_source": csv_source,
                           "gas_url": str(gas_url).strip(),
                           "gas_token": str(gas_token).strip(),
                           "gas_sheet": str(gas_sheet).strip(),
                           "gas_keep_drive": bool(gas_keep_drive),
                           "drive_root": str(drive_root).strip(),
                           "drive_label": str(drive_label).strip(),
                           "export_robot": export_robot,
                           "csv_tab": csv_tab, "csv_encoding": csv_encoding,
                           "skip_empty_col": skip_empty_col.strip(),
                           "send_robot": send_robot, "dedup_days": int(dedup_days)}
                _save_settings(_upsert_pattern(cfg, pat_new, old_name))
                st.session_state.sms_view = "list"
                st.success("保存しました。")
                st.rerun()
    with s2:
        if old_name and st.button("🗑 このパターンを消す"):
            cfg["patterns"] = [p for p in _patterns(cfg) if p.get("name") != old_name]
            _save_settings(cfg)
            st.session_state.sms_view = "list"
            st.rerun()


# ==========================================
# ▶ 画面3：送信をはじめる
# ==========================================
elif st.session_state.sms_view == "run":
    pname = st.session_state.sms_pattern
    pat = _find(cfg, pname)
    if not pat:
        st.error("パターンが見つかりません。")
        st.session_state.sms_view = "list"
        st.stop()

    if st.button("⬅ 一覧に戻る"):
        st.session_state.sms_view = "list"
        st.rerun()

    st.markdown(f"### 📱 {pname}")
    ch.guide("operate",
             "順番にいくよ。<b>直すところが残っているうちは、送信ボタンは出さない</b>から安心してね。")

    fkey = f"sms_find_{pname}"
    enc = pat.get("csv_encoding", "Shift_JIS")
    src = pat.get("csv_source", CSV_SOURCES[0])

    # --- ① シートを更新 ---
    with st.container(border=True):
        theme.section_title("1️⃣", "SFコネクタでシートを更新する")
        _tabs = pat.get("refresh_tabs", []) or []
        if pat.get("refresh_robot") and _tabs:
            st.caption(f"使うロボット：**{pat['refresh_robot']}**　"
                       f"／ 更新するシート：**{'、'.join(_tabs)}**")
            # 📌 録画したときのURLではなく、このパターンに登録したスプシを開く。
            #    「録画したスプシしか更新できないのでは」と迷わないよう、開く先を必ず見せる。
            st.caption(f"開くスプレッドシート：{pat['sheet_url']}")
            _lg, _lgw = common_robots.login_status(pat["refresh_robot"])
            if _lg:
                st.caption(f"🔐 Googleのログイン状態：あり（最終 {_lgw}）")
            else:
                st.warning("🔐 このロボットのブラウザに**ログイン状態がありません**。"
                           "「⚙️ その他設定」の🔐先にログインしておく、から入っておいてください。")
            st.caption("ブラウザは1回だけ開いて、その中でシートを切り替えながら回します。"
                       "録画したときのURLは使いません（開く先はここに登録したスプシです）。")
            r1, r2 = st.columns([1, 1])
            _one = None
            with r2:
                _one = st.selectbox("お試し（1枚だけ更新してみる）",
                                    ["（使わない）"] + list(_tabs), key=f"sms_one_{pname}")
                _one = None if str(_one).startswith("（") else _one
                if st.button("🧪 この1枚だけ試す", use_container_width=True, disabled=not _one):
                    _u = sms_runner.tab_urls_for(pat["sheet_url"], [_one], _gids_of(pat))
                    with st.spinner(f"「{_one}」だけ更新しています..."):
                        ok, log = sms_runner.run_refresh_robot(pat["refresh_robot"], pname,
                                                               tabs=[_one], tab_urls=_u,
                                                               url=pat["sheet_url"])
                    st.session_state[f"sms_ref_{pname}"] = {
                        "ok": ok, "log": log,
                        "表": sms_runner.parse_refresh_log(log, [_one])}
                    st.rerun()
            with r1:
                if st.button("🔄 ぜんぶ更新する", type="primary", use_container_width=True):
                    _u = sms_runner.tab_urls_for(pat["sheet_url"], _tabs, _gids_of(pat))
                    with st.spinner(f"{len(_tabs)}枚のシートを順に更新しています"
                                    "（レポートによっては数分かかります）..."):
                        ok, log = sms_runner.run_refresh_robot(pat["refresh_robot"], pname,
                                                               tabs=_tabs, tab_urls=_u,
                                                               url=pat["sheet_url"])
                    st.session_state[f"sms_ref_{pname}"] = {
                        "ok": ok, "log": log, "表": sms_runner.parse_refresh_log(log, _tabs)}
                    st.rerun()
            res = st.session_state.get(f"sms_ref_{pname}")
            if res:
                st.dataframe(pd.DataFrame(res["表"]), use_container_width=True, hide_index=True)
                if res["ok"]:
                    st.success("✅ ぜんぶ更新できました。")
                else:
                    st.error("❌ 途中で止まりました。上の表で、どのシートまで進んだか分かります。")
                with st.expander("実行ログ", expanded=not res["ok"]):
                    st.code(res["log"])
        else:
            st.info("このパターンは、シートの更新を**手作業**で行う設定です。")
            if pat.get("sheet_url"):
                st.markdown(f"[📄 スプレッドシートを開く]({pat['sheet_url']})")

    # --- ② チェック ---
    with st.container(border=True):
        theme.section_title("2️⃣", "中身をチェックする")
        if not pat.get("checks"):
            st.info("チェックのルールが登録されていません（設定画面の3️⃣で追加できます）。"
                    "このまま進むこともできます。")
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("🔍 チェックする", use_container_width=True, type="primary", disabled=not gc):
                with st.spinner("シートを見ています..."):
                    try:
                        findings, notes = sms_runner.check_rules(gc, pat["sheet_url"],
                                                                 pat.get("checks", []))
                        st.session_state[fkey] = {"findings": findings, "notes": notes}
                    except Exception as e:
                        st.session_state[fkey] = {"findings": [], "notes": [f"読めませんでした：{e}"]}
                st.rerun()
        with c2:
            if pat.get("sheet_url"):
                st.markdown(f"[📄 スプレッドシートを開いて直す]({pat['sheet_url']})")

        res = st.session_state.get(fkey)
        if res is None:
            st.caption("まだチェックしていません。")
        else:
            for n in res.get("notes", []):
                st.warning(n)
            findings = res.get("findings", [])
            if findings:
                st.error(f"🛠 直すところが **{len(findings)}件** あります。"
                         "スプレッドシートで直してから、もう一度チェックしてください。")
                st.dataframe(pd.DataFrame(findings), use_container_width=True, hide_index=True)
                st.download_button("⬇️ 直すところの一覧をCSVで落とす",
                                   data=pd.DataFrame(findings).to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"要修正_{pname}_{sms_runner.today_stamp()}.csv",
                                   mime="text/csv")
            else:
                st.success("✅ 直すところはありませんでした。送信に進めます。")

    # --- ③ CSVを用意 ---
    res = st.session_state.get(fkey)
    clean = bool(res) and not res.get("findings")
    with st.container(border=True):
        theme.section_title("3️⃣", "CSVを用意する")
        if not clean:
            st.info("上の 2️⃣ で「直すところはありません」になると、ここのボタンが出ます。")
        else:
            st.caption(f"用意のしかた：**{src}**")
            if st.button("📄 CSVを用意する"):
                try:
                    if src == CSV_SOURCES[0]:
                        if not str(pat.get("gas_url", "")).strip():
                            raise RuntimeError("GASのウェブアプリURLが未設定です（設定画面の4️⃣）。")
                        with st.spinner("GASにCSVを作ってもらっています..."):
                            path, gname, grows = sms_runner.fetch_from_gas(
                                pat["gas_url"], pat.get("gas_token", ""),
                                pat.get("gas_sheet", ""), pname,
                                keep_drive=bool(pat.get("gas_keep_drive", True)))
                        st.success(f"✅ GASから受け取りました：`{gname}`（{grows}件）")
                    elif src == CSV_SOURCES[1]:
                        sa = _sa_json()
                        if not sa:
                            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
                        with st.spinner("Driveから今日のCSVを取ってきています..."):
                            path, dname, _h = sms_runner.fetch_from_drive(
                                sa, pat.get("drive_root", ""), pat.get("drive_label", ""), pname)
                        st.success(f"✅ Driveから受け取りました：`{dname}`")
                    elif src == CSV_SOURCES[2]:
                        with st.spinner("ブラウザを開いてCSVを書き出しています..."):
                            ok, log = sms_runner.run_export_robot(pat["export_robot"], pname,
                                                                  url=pat["sheet_url"])
                        got = sms_runner.adopt_downloaded(pname)
                        if got:
                            st.success("✅ 受け取りました。")
                        else:
                            st.error("❌ CSVが落ちてきませんでした。ログを確認してください。")
                            with st.expander("実行ログ", expanded=True):
                                st.code(log)
                    else:
                        path, n, _h = sms_runner.export_csv(
                            gc, pat["sheet_url"], pat["csv_tab"], pname, enc,
                            pat.get("skip_empty_col", ""))
                        st.success(f"✅ {n}件のCSVを作りました。")
                    st.session_state.pop(f"sms_dup_{pname}", None)
                except Exception as e:
                    st.error(f"CSVを用意できませんでした：{e}")

            got = sms_runner.today_csv(pname)
            if not got:
                _made = sms_runner.csv_made_at(pname)
                if _made:
                    st.warning(f"⚠️ 手元のCSVは **{_made}** に用意したものです（今日ではありません）。"
                               "古いものを送らないよう、もう一度用意してください。")
                else:
                    st.caption("まだ今日のCSVがありません。")
            else:
                _n = sms_runner.csv_row_count(got, enc)
                st.markdown(f"**今日のCSV**：`{os.path.basename(got)}`　"
                            f"（{_n}件・用意 {sms_runner.csv_made_at(pname)}）")
                with st.expander("👀 中身を先頭だけ見る（送る前の最終確認）"):
                    st.code(sms_runner.csv_preview(got, enc))

    # --- ④ 二重送信チェック → 一括送信 ---
    got = sms_runner.today_csv(pname)
    with st.container(border=True):
        theme.section_title("4️⃣", "二重送信の確認と、一括送信")
        if not (clean and got):
            st.info("上の 3️⃣ で今日のCSVが用意できると、ここのボタンが出ます。")
        else:
            days = int(pat.get("dedup_days", 0) or 0)
            keys = sms_runner.csv_dest_keys(got, enc)
            dup = sms_runner.find_already_sent(pname, keys, days)
            if dup:
                st.error(f"🛑 このCSVには、**すでに送った宛先が {len(dup)}件** 入っています"
                         + (f"（{days}日以内）" if days else "（過去に一度でも送った分）")
                         + "。そのまま送ると二重送信になります。")
                st.dataframe(pd.DataFrame(dup), use_container_width=True, hide_index=True)
                if st.button("✂️ 送った分を外したCSVにする", type="primary"):
                    n_drop, n_left = sms_runner.drop_already_sent(pname, enc, days)
                    st.success(f"✅ {n_drop}件を外しました（残り {n_left}件）。")
                    st.rerun()
                st.caption("※ どうしても送り直したい場合は、下の「送信の記録」から消してください。")
            else:
                st.success(f"✅ 送った宛先とのかぶりはありません（{len(keys)}件）。")
                agree = st.checkbox(f"この **{len(keys)}件** に、実際にSMSを一括送信します（取り消せません）",
                                    key=f"sms_agree_{pname}")
                if st.button("🚀 一括送信する", type="primary", disabled=not agree):
                    with st.spinner("ブラウザを開いて送信しています..."):
                        ok, log = sms_runner.run_send_robot(pat["send_robot"], pname, got)
                    # 📮 送った／送っていないを記録する。
                    #    プッシュプロは一括送信なので1件ごとの成否は分からない。
                    #    途中で止まっても『送信』まで進んでいたら、送られた可能性がある。
                    #    二重送信のほうが取り返しがつかないので、迷ったら「送った」に寄せる。
                    if ok:
                        result, note = "送信済み", ""
                    elif sms_runner.submit_reached(log):
                        result, note = "要確認（送ったかもしれない）", "途中で止まりました"
                    else:
                        result, note = "送信できず", "送信の手前で止まりました"
                    sms_runner.record_sent(pname, keys, result, note)
                    st.session_state[f"sms_sent_{pname}"] = {"ok": ok, "log": log,
                                                             "result": result, "n": len(keys)}
                    st.rerun()

            done = st.session_state.get(f"sms_sent_{pname}")
            if done:
                if done["ok"]:
                    st.success(f"✅ {done['n']}件の送信手順が最後まで通りました。"
                               "プッシュプロ側の送信結果も必ず確認してください。")
                elif done["result"].startswith("要確認"):
                    st.error(f"⚠️ 途中で止まりましたが、**送信ボタンまで進んでいました**。"
                             f"{done['n']}件を「要確認（送ったかもしれない）」として記録しました。"
                             "プッシュプロの送信履歴を見て、実際に送られたか確かめてください。")
                else:
                    st.error("❌ 送信の手前で止まりました。送信の記録は増やしていません"
                             "（直してから、もう一度送れます）。")
                with st.expander("実行ログ", expanded=not done["ok"]):
                    st.code(done["log"])

    # --- 送信の記録 ---
    with st.expander("📮 送信の記録（誰に送ったか）"):
        sent = sms_runner.load_sent(pname)
        if not sent:
            st.caption("まだ記録はありません。")
        else:
            sdf = pd.DataFrame([{"宛先": k, "日時": v.get("日時", ""),
                                 "結果": v.get("結果", ""), "メモ": v.get("メモ", "")}
                                for k, v in sent.items()])
            sdf = sdf.sort_values("日時", ascending=False)
            st.caption(f"{len(sdf)}件（新しい順）")
            st.dataframe(sdf.head(500), use_container_width=True, hide_index=True)
            st.download_button("⬇️ 記録をCSVで落とす",
                               data=sdf.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"送信履歴_{pname}_{sms_runner.today_stamp()}.csv",
                               mime="text/csv")

    st.divider()
    st.caption("💻 更新・書き出し・一括送信はブラウザを開くため、**担当者のPCで開いているとき**だけ動きます。")
