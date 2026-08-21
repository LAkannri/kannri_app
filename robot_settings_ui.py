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


def _link_step_auth_code(steps, field, setting_name):
    """指定した入力欄の手順を「認証コードを入力」に差し替える。

    録画すると、そのとき打った認証コード（もう失効している）がそのまま手順に残る。
    実行のたびにメールから受け取った値を入れるよう、操作ごと置き換える。
    録画のセレクタは活かしたいので、ai_code の入力値だけを {認証コード} にする。
    """
    import copy
    new_steps = copy.deepcopy(steps or [])
    hit = 0
    for step in new_steps:
        if not step:
            continue
        t = str(step.get("対象", step.get("target_description", "")) or "").strip()
        if t != field:
            continue
        step["操作"] = "認証コードを入力"
        step["値"] = setting_name
        for key in ("ai_code", "最強の呪文"):
            if step.get(key):
                _pat = r'''\.fill\(\s*(?:"[^"]*"|'[^']*')\s*\)'''
                step[key] = re.sub(_pat, '.fill("{認証コード}")', str(step[key]), count=1)
        hit += 1
    return new_steps, hit


def guess_code_pattern(body: str, code: str = ""):
    """メール本文から「コードの探し方」を自動で組み立てる。

    正規表現を担当者に書かせるのは無理があるので、
    本文（と、必要ならコードそのもの）を貼れば作れるようにする。
    コードの直前にある言葉を手掛かりにするのが、いちばん誤爆しにくい。
    戻り値：(パターン, 説明) ／ 作れなければ ("", 理由)
    """
    text = str(body or "")
    if not text.strip():
        return "", "本文が空です"

    code = str(code or "").strip()
    if code:
        pos = text.find(code)
        if pos < 0:
            return "", f"本文の中に「{code}」が見つかりませんでした"
    else:
        # コード指定が無ければ、4〜8桁の数字を候補にする
        nums = list(re.finditer(r"\d{4,8}", text))
        if not nums:
            return "", "4〜8桁の数字が見つかりませんでした"
        # 「コード」という語のいちばん近くにある数字を選ぶ
        key = max([text.rfind("認証コード"), text.rfind("コード")])
        best = min(nums, key=lambda m: abs(m.start() - key) if key >= 0 else m.start())
        code, pos = best.group(0), best.start()

    # コードの直前30文字から、手掛かりになる日本語（または英語）の語を探す
    before = text[max(0, pos - 30):pos]
    label = ""
    for word in ["認証コード", "ワンタイムパスワード", "確認コード", "セキュリティコード",
                 "パスコード", "コード", "code", "Code"]:
        if word in before:
            label = word
            break
    if label:
        pattern = re.escape(label) + r"[^0-9]{0,10}([0-9]{" + str(len(code)) + r"})"
        why = f"「{label}」のうしろにある{len(code)}桁の数字を取り出します"
    else:
        pattern = r"([0-9]{" + str(len(code)) + r"})"
        why = f"本文の中の{len(code)}桁の数字を取り出します（手掛かりの言葉が見つからないため）"

    m = re.search(pattern, text)
    if not m or (m.group(1) if m.groups() else m.group(0)) != code:
        return "", "うまく作れませんでした。コードの数字も入れて、もう一度試してください"
    return pattern, why


@st.cache_data(ttl=30, show_spinner=False)
def _read_tab_values(_ws_key: str, _gc, url: str, tab: str):
    """設定タブの中身を読む。Sheets APIは1分60回までなので短時間キャッシュする
    （キャッシュが無いと、画面を触るたびに読みに行って上限に当たり、
      再試行の待ちで『ずっとロード中』に見える）。"""
    sh = _gc.open_by_url(url)
    try:
        return sh.worksheet(tab).get_all_values()
    except Exception:
        return []


AUTH_TAB = "認証コード設定"
AUTH_HEADERS = ["キャリア名", "Gmail検索条件", "抜き出しパターン(正規表現)", "有効"]

def render_auth_code_settings(project_id, config=None, proj_data=None):
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
                vals = _read_tab_values(f"{_url}|{AUTH_TAB}", gc, _url, AUTH_TAB) or [AUTH_HEADERS]
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
        # 入力欄の中身は、この欄自身に覚えさせる（画面を描き直しても消えないように）。
        # 「メールから自動で作る」で作れたときは、欄を作る前にその値を入れておく。
        _pkey = f"authp_{project_id}"
        _made = st.session_state.pop(f"authp_made_{project_id}", None)
        if _made:
            st.session_state[_pkey] = _made
        elif _pkey not in st.session_state:
            st.session_state[_pkey] = (str(cur.get("抜き出しパターン(正規表現)", ""))
                                       or r"認証コード[^0-9]{0,10}([0-9]{4,8})")
        pat = st.text_input("コードの抜き出しかた（正規表現）", key=_pkey,
                            help="( ) の中がコードとして取り出されます")
        st.caption("💡 本文が「認証コードは 123456 です」なら、この既定のままで拾えます。")

        # 📩 実物のメールから、パターンを自動で作る。
        #    正規表現を担当者に書かせるのは現実的でないため、貼るだけで済むようにする。
        with st.expander("📩 届いたメールから自動で作る（おすすめ）", expanded=not str(cur.get("Gmail検索条件", ""))):
            st.caption("届いた認証コードのメールを、そのまま貼り付けてください。"
                       "コードの探し方をアプリが組み立てます。")
            sample = st.text_area("メールの本文", key=f"authsample_{project_id}", height=140,
                                  placeholder="認証コード：5021（届いたメールをそのまま貼ってください）")
            code_hint = st.text_input("そのメールに書かれていたコード（分かれば）",
                                      key=f"authcode_{project_id}", placeholder="例：5021")
            if st.button("📩 このメールから作る", key=f"authgen_{project_id}", type="primary"):
                if not sample.strip():
                    st.warning("メールの本文を貼ってください。")
                else:
                    _p, _why = guess_code_pattern(sample, code_hint)
                    if _p:
                        # 入力欄そのものは書き換えられないので、別の場所に覚えておき、
                        # 次に画面を描くときの初期値として使う
                        st.session_state[f"authp_made_{project_id}"] = _p
                        st.success(f"✅ できました：{_why}")
                        st.caption("下の「コードの探し方」に入りました。保存すれば完了です。")
                        st.rerun()
                    else:
                        st.error(f"❌ {_why}")

        if st.button("💾 二段階認証の設定を保存", key=f"authsave_{project_id}"):
            if not (key_name.strip() and q.strip()):
                st.warning("名前と検索条件を入れてください。")
            else:
                try:
                    with st.spinner("保存しています..."):
                        rows = [r for r in vals[1:] if r and str(r[0]).strip() != key_name.strip()]
                        rows.append([key_name.strip(), q.strip(), pat.strip(), "TRUE"])
                        ws.clear()
                        ws.update(range_name="A1", values=[AUTH_HEADERS] + rows,
                                  value_input_option="USER_ENTERED")
                        st.cache_data.clear()   # 次の表示で最新を読む
                    st.success("保存しました。手順書では、操作を「認証コードを入力」にして、"
                               f"値に `{key_name.strip()}` と書いてください。")
                except Exception as e:
                    st.error(f"保存できませんでした: {e}")

        # 🩺 ちゃんと繋がっているかを確かめる。
        #    設定・GAS・受け取りの3つが揃って初めて動くので、どこで止まっているかを示す。
        if st.button("🩺 設定できているか調べる", key=f"authcheck_{project_id}",
                     use_container_width=True):
            _ok = True
            # ① 設定シートに、この名前の行があるか
            _saved = None
            for r in (_read_tab_values(f"{_url}|{AUTH_TAB}", gc, _url, AUTH_TAB) or [[]])[1:]:
                if r and str(r[0]).strip() == key_name.strip():
                    _saved = r
                    break
            if not _saved:
                st.error("① 設定が保存されていません（上の「💾 二段階認証の設定を保存」を押してください）")
                _ok = False
            else:
                st.success(f"① 設定あり：検索条件「{_saved[1][:40]}」")
                if str(_saved[1]).startswith("[") or "：" in str(_saved[1])[:12]:
                    st.warning("　⚠️ 検索条件がGmailの書き方になっていないかもしれません。"
                               "`subject:ログイン認証コード` のように書きます"
                               "（Gmailの検索窓で試した文字をそのまま貼るのが確実）。")
                if re.search(r"[0-9]{4,}", str(_saved[2])) and "(" not in str(_saved[2]):
                    st.error("　❌ 抜き出しパターンに、コードの数字そのものが入っています。"
                             "上の「📩 届いたメールから自動で作る」で作り直してください。")
                    _ok = False

            # ② GASがコードを書き込めているか
            _codes = _read_tab_values(f"{_url}|認証コード", gc, _url, "認証コード") or []
            _mine = [r for r in _codes[1:] if r and str(r[0]).strip() == key_name.strip()]
            if _mine:
                st.success(f"② コードを受け取れています（最後に取れたのは {_mine[0][2]}）")
            else:
                st.warning("② まだコードを受け取れていません。"
                           "**認証コードのメールが届いている状態で**、"
                           "GASの `fetchAuthCodes` を手動実行してみてください。"
                           "うまくいけば、この行にコードが入ります。")
                _ok = False

            # ③ 手順書側で使う設定になっているか
            _steps_chk = ((config or {}).get("robot_config", {}) or {}).get("steps", []) or []
            _used = [s for s in _steps_chk
                     if str((s or {}).get("操作", "")) == "認証コードを入力"
                     and str((s or {}).get("値", "")).strip() == key_name.strip()]
            if _used:
                st.success(f"③ 手順書の #{_used[0].get('順番')} で、この設定を使う指定になっています")
            else:
                st.warning(f"③ 手順書に「認証コードを入力（値：{key_name.strip()}）」の手順がありません。"
                           "下の「🔁 差し替える」で設定してください。")
                _ok = False

            if _ok:
                st.info("✅ 3つとも整っています。実行時にコードが自動で入ります。")

        # 🔁 録画した「認証コードを打った手順」を、自動入力に差し替える
        #    （録画時のコードは失効しているので、そのままでは毎回失敗する）
        steps_now = ((config or {}).get("robot_config", {}) or {}).get("steps", []) or []
        if steps_now and proj_data is not None:
            st.markdown("---")
            st.markdown("**🔁 録画した手順を、認証コードの自動入力に差し替える**")
            fields = []
            for s_ in steps_now:
                t_ = str((s_ or {}).get("対象", (s_ or {}).get("target_description", "")) or "").strip()
                if t_ and t_ not in fields:
                    fields.append(t_)
            c1, c2 = st.columns([3, 1])
            with c1:
                target_field = st.selectbox("認証コードを入れる欄", fields, key=f"authfield_{project_id}")
            with c2:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("差し替える", key=f"authswap_{project_id}", use_container_width=True):
                    new_steps, hit = _link_step_auth_code(steps_now, target_field, key_name.strip())
                    if hit:
                        config["robot_config"]["steps"] = new_steps
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                        st.success(f"「{target_field}」を『認証コードを入力』に差し替えました（{hit}手順）。"
                                   "実行時は、メールに届いたコードが自動で入ります。")
                        st.rerun()
                    else:
                        st.warning("その欄の手順が見つかりませんでした。")

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
