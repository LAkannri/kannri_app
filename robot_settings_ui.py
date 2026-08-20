"""
🔑🔐 ロボットの「ログイン情報」と「二段階認証」の設定パネル

エントリー業務でも、進捗の取り込みでも、同じ設定が要る。
どちらの画面からも同じものを出せるよう、ここに1本化する
（同じ画面を2か所に書くと、片方だけ直して食い違うため）。

・ログイン情報 … 値は暗号化して Supabase に保存し、手順書には {秘密:名前} だけを書く
・二段階認証   … メールから認証コードを取り出す条件。GAS(fetchAuthCodes)が読む表に書く
"""

import json
import os
import re

import streamlit as st
from supabase import create_client


@st.cache_resource
def _sb():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


@st.cache_resource(show_spinner=False)
def _gspread(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials
    return gspread.authorize(Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"]))


def _get_gspread_client():
    try:
        sa = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        return None
    if not sa:
        return None
    try:
        return _gspread(sa)
    except Exception:
        return None


def save_project(project_id, data):
    _sb().table("merchants").upsert(data).execute()


def _link_step_secret(steps, field, secret_name):
    """指定した項目の手順を「ログイン情報を使う」形に書き換える（値も ai_code も）。
    録画のときに打った文字が手順書に残らないようにするための差し替え。"""
    import copy
    ph = "{秘密:" + secret_name + "}"
    new_steps = copy.deepcopy(steps or [])
    hit = 0
    for step in new_steps:
        if not step:
            continue
        t = str(step.get("対象", step.get("target_description", "")) or "").strip()
        if t != field:
            continue
        step["値"] = ph
        for key in ("ai_code", "最強の呪文"):
            if not step.get(key):
                continue
            # 引用符の中身ごと差し替える（録画コードは " も ' もあり得る）
            _pat = r'''\.fill\(\s*(?:"[^"]*"|'[^']*')\s*\)'''
            step[key] = re.sub(_pat, '.fill("' + ph + '")', str(step[key]), count=1)
        hit += 1
    return new_steps, hit


AUTH_TAB = "認証コード設定"
AUTH_HEADERS = ["キャリア名", "Gmail検索条件", "抜き出しパターン(正規表現)", "有効"]

def render_auth_code_settings(project_id):
    """🔐 二段階認証（メールに届く認証コード）の設定。

    ログイン情報の下・手順書の上に置く。録画→ID/パス→認証コード→手順書、と
    実際に設定する順番どおりに並べるため。
    設定は進捗反映と同じスプレッドシートに書き、GAS(fetchAuthCodes)がそれを読む。
    """
    with st.expander("🔐 二段階認証（メールに届く認証コード）"):
        st.caption("ログイン後にメールで認証コードが届くサイト向けです。"
                   "設定すると、GASがメールからコードを取り出し、ロボットが自動で入力します。"
                   "設定しない場合は、手順書で「人の操作を待つ」を使って手入力できます。")
        # 置き場所は進捗反映の設定スプレッドシート（GASがそこを読むため）
        try:
            _res = _sb().table("merchants").select("config_json").eq("id", "__progress__").execute()
            _url = str(((_res.data or [{}])[0].get("config_json") or {}).get("settings_url", "") or "")
        except Exception:
            _url = ""
        gc = _get_gspread_client()
        if not (_url and gc):
            st.info("先に「🚀 進捗反映自動化」タブで、設定スプレッドシートを登録してください"
                    "（認証コードの受け渡しに使います）。")
            return
        try:
            sh = gc.open_by_url(_url)
            try:
                ws = sh.worksheet(AUTH_TAB)
                vals = ws.get_all_values()
            except Exception:
                ws = sh.add_worksheet(title=AUTH_TAB, rows=50, cols=4)
                ws.update(range_name="A1", values=[AUTH_HEADERS])
                vals = [AUTH_HEADERS]
        except Exception as e:
            st.warning(f"設定スプレッドシートを開けませんでした: {e}")
            return

        key_name = st.text_input("この設定の名前（手順書の「値」に書く名前）", value=project_id,
                                 key=f"authname_{project_id}")
        cur = {}
        for r in vals[1:]:
            if r and str(r[0]).strip() == key_name.strip():
                cur = dict(zip(AUTH_HEADERS, (list(r) + [""] * 4)[:4]))
                break
        q = st.text_input("認証コードのメールの検索条件", value=str(cur.get("Gmail検索条件", "")),
                          placeholder="from:no-reply@example.jp subject:認証コード",
                          key=f"authq_{project_id}")
        st.caption("💡 Gmailの検索窓で試して、そのメールだけが出る条件をコピーしてください。")
        pat = st.text_input("コードの抜き出しかた（正規表現）",
                            value=str(cur.get("抜き出しパターン(正規表現)", "")
                                      or r"認証コード[^0-9]{0,10}([0-9]{4,8})"),
                            key=f"authp_{project_id}",
                            help="( ) の中がコードとして取り出されます")
        st.caption("💡 本文が「認証コードは 123456 です」なら、この既定のままで拾えます。")
        if st.button("💾 二段階認証の設定を保存", key=f"authsave_{project_id}"):
            if not (key_name.strip() and q.strip()):
                st.warning("名前と検索条件を入れてください。")
            else:
                try:
                    rows = [r for r in vals[1:] if r and str(r[0]).strip() != key_name.strip()]
                    rows.append([key_name.strip(), q.strip(), pat.strip(), "TRUE"])
                    ws.clear()
                    ws.update(range_name="A1", values=[AUTH_HEADERS] + rows,
                              value_input_option="USER_ENTERED")
                    st.success("保存しました。手順書では、操作を「認証コードを入力」にして、"
                               f"値に `{key_name.strip()}` と書いてください。")
                except Exception as e:
                    st.error(f"保存できませんでした: {e}")

def render_login_secrets(project_id, config, proj_data):
    """🔑 ログイン情報（ID・パスワード）の登録パネル。

    録画して手順書を組んだ直後に設定したいものなので、手順書のすぐ上に置く。
    値は暗号化して保存し、手順書には {秘密:名前} だけを書く。"""
    with st.expander("🔑 ログイン情報（ID・パスワード）"):
        st.caption("ログインが必要なサイト向けです。ここで登録すると**暗号化して保存**され、"
                   "手順書には `{秘密:名前}` と書くだけで使えます。"
                   "画面にもデータベースにも、パスワードそのものは残りません。")
        _keyset = bool(str(st.secrets.get("ENKAN_SECRET_KEY", "") or "").strip())
        _enc = config.get("robot_config", {}).get("secrets", {}) or {}
        if not _keyset:
            # 🔑 鍵はアプリが自動で作って secrets.toml に追記する。
            #    以前は「作る→画面に表示→人がメモ帳に貼る→再起動」だったが、
            #    手順が多いうえ、表示した鍵がスクショに写るなど漏れる経路も増えていた。
            #    人が鍵を目にしないほうが安全で、操作も減る。
            st.info("🔑 ログイン情報を暗号化するための鍵を、まだこのPCに用意していません。"
                    "下のボタンを押すと、アプリが自動で作って接続キーのファイルに書き込みます。")
            if st.button("🔑 鍵を用意する（自動）", key=f"genkey_{project_id}", type="primary"):
                try:
                    from cryptography.fernet import Fernet
                    _newkey = Fernet.generate_key().decode()
                    _sec_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".streamlit", "secrets.toml")
                    if not os.path.exists(_sec_path):
                        raise FileNotFoundError(f"{_sec_path} が見つかりません")
                    # 末尾に1行足すだけ。既存の内容には触らない（他の鍵を壊さないため）
                    with open(_sec_path, "r", encoding="utf-8") as _f:
                        _body = _f.read()
                    if "ENKAN_SECRET_KEY" in _body:
                        st.warning("すでに鍵が書かれています。アプリを再起動すると有効になります。")
                    else:
                        with open(_sec_path, "a", encoding="utf-8") as _f:
                            _f.write(f'\n# ログイン情報の暗号化用の鍵（自動生成）\nENKAN_SECRET_KEY = "{_newkey}"\n')
                        st.success("✅ 鍵を用意しました。**アプリを再起動**すると、"
                                   "ここでID・パスワードを登録できるようになります。")
                        st.caption("※このファイルを他のPCにコピーすれば、そのPCでも同じ鍵が使えます"
                                   "（鍵の作り直しは不要です）。")
                except Exception as _e:
                    st.error(f"鍵を用意できませんでした: {_e}")
                    st.caption("クラウド版では設定ファイルに書き込めません。"
                               "その場合は、ローカルのアプリで一度この操作をしてください。")
        else:
            st.success("✅ このPCに鍵が設定されています。")
            _s1, _s2 = st.columns([1, 1])
            with _s1:
                _sname = st.text_input("名前（手順書で使う呼び名）", placeholder="例：ログインID",
                                       key=f"secname_{project_id}")
            with _s2:
                _sval = st.text_input("値（保存後は表示されません）", type="password",
                                      key=f"secval_{project_id}")
            if st.button("💾 暗号化して保存", key=f"secsave_{project_id}"):
                if not (_sname.strip() and _sval):
                    st.warning("名前と値の両方を入れてください。")
                else:
                    try:
                        from cryptography.fernet import Fernet
                        _f = Fernet(str(st.secrets["ENKAN_SECRET_KEY"]).strip().encode())
                        _enc[_sname.strip()] = _f.encrypt(_sval.encode()).decode()
                        config.setdefault("robot_config", {})["secrets"] = _enc
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                        st.success(f"「{_sname.strip()}」を暗号化して保存しました。"
                                   f"手順書の「値」に `{{秘密:{_sname.strip()}}}` と書いて使います。")
                    except Exception as _e:
                        st.error(f"保存できませんでした: {_e}")
        if _enc:
            # 🔁 録画で打った文字が手順書に残らないよう、該当の入力欄を差し替える
            st.markdown("**🔁 録画した手順を、ログイン情報に差し替える**")
            st.caption("📌 **録画は本物のID・パスワードで行ってOKです**（ダミーではログインできないため）。"
                       "手順書を作るときに、**パスワード欄の入力は自動で伏せ字**になります。"
                       "ただし**ログインIDは自動で判別できない**ので、手順書に残したくない場合は"
                       "ここで `{秘密:名前}` に差し替えてください。")
            _sw_fields = []
            for _s in (config.get("robot_config", {}).get("steps", []) or []):
                _t = str((_s or {}).get("対象", (_s or {}).get("target_description", "")) or "").strip()
                if _t and _t not in _sw_fields:
                    _sw_fields.append(_t)
            if _sw_fields:
                _sw1, _sw2, _sw3 = st.columns([2, 2, 1])
                with _sw1:
                    _sw_field = st.selectbox("差し替える入力欄", _sw_fields, key=f"swfield_{project_id}")
                with _sw2:
                    _sw_name = st.selectbox("使うログイン情報", list(_enc.keys()), key=f"swname_{project_id}")
                with _sw3:
                    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                    if st.button("差し替える", key=f"swgo_{project_id}", use_container_width=True):
                        _new_steps, _hit = _link_step_secret(
                            config.get("robot_config", {}).get("steps", []), _sw_field, _sw_name)
                        if _hit:
                            config["robot_config"]["steps"] = _new_steps
                            proj_data["config_json"] = config
                            save_project(project_id, proj_data)
                            st.success(f"「{_sw_field}」を `{{秘密:{_sw_name}}}` に差し替えました（{_hit}手順）。")
                            st.rerun()
                        else:
                            st.warning("その入力欄の手順が見つかりませんでした。")
            st.markdown("---")
            st.markdown("**登録済み（値は表示しません）**")
            for _n in list(_enc.keys()):
                _e1, _e2 = st.columns([4, 1])
                with _e1:
                    st.markdown(f"　🔒 **{_n}**　→ 手順書では `{{秘密:{_n}}}`")
                with _e2:
                    if st.button("削除", key=f"secdel_{project_id}_{_n}"):
                        _enc.pop(_n, None)
                        config["robot_config"]["secrets"] = _enc
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                        st.rerun()
        st.caption("⚠️ 鍵を持っているPCの利用者は、ログイン情報を使えます（復号できます）。"
                   "実行を任せる人には、そのアカウントを預けるのと同じだと考えてください。")
        st.markdown("---")
        st.caption("**別のやり方：接続キーに直接書く**　1台でしか実行しない場合は、ここに登録せず、"
                   "実行するPCの `.streamlit/secrets.toml` に `ログインID = \"xxxx\"` のように"
                   "**同じ名前で**書いてもかまいません（手順書の書き方は同じ `{秘密:ログインID}`）。"
                   "この場合、鍵の配布は不要ですが、PCが増えるたびに全PCへ書く必要があります。"
                   "⚠️ クラウド版アプリのSecretsに書いても、申請を実行するのは各PCなので**そちらには届きません**。")
