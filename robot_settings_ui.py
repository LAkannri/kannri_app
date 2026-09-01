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


def guess_link_pattern(body: str):
    """メール本文から「ログインのリンクの探し方」を組み立てる。

    メールリンク認証（マジックリンク）は、毎回うしろの合言葉だけが変わる。
    だから **変わらない前半（ドメイン＋パス）を手掛かりに、うしろを丸ごと取る**。
    戻り値：(パターン, 説明) ／ 作れなければ ("", 理由)
    """
    text = str(body or "")
    if not text.strip():
        return "", "本文が空です"
    urls = re.findall(r"https?://[^\s\"'<>\)]+", text)
    if not urls:
        return "", ("本文にURLが見つかりませんでした。"
                    "ボタンだけのメールのときは、ボタンを右クリックして"
                    "「リンクのアドレスをコピー」したものを貼ってください")
    # 合言葉つきのリンク（? がある）を優先し、その中でいちばん長いものを選ぶ
    cands = [u for u in urls if "?" in u] or urls
    url = max(cands, key=len)
    head = url.split("?")[0]
    pattern = "(" + re.escape(head) + r"[^\s\"'<>]*)"
    why = f"「{head}」で始まるリンクを、まるごと取り出します"
    m = re.search(pattern, text)
    if not m:
        return "", "うまく作れませんでした（貼り付けた本文を見直してください）"
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


def build_gmail_query(sender: str = "", subject: str = "", body_phrase: str = "") -> str:
    """Gmailの検索条件を組み立てる。

    担当者にGmailの検索記法（from: / subject: / 引用符）を覚えさせないための処理。
    差出人と件名が分かればそれで十分に絞れる。両方とも分からないときだけ本文の言葉を使う。
    """
    parts = []
    sender = str(sender or "").strip()
    subject = str(subject or "").strip()
    body_phrase = str(body_phrase or "").strip()
    if sender:
        parts.append(f"from:{sender}")
    if subject:
        # 件名は必ず引用符でくくる。【】や / を含む件名だと、くくらないと
        # Gmailが記号のところで切ってしまい、別のメールまで拾ってしまうため。
        parts.append('subject:"{}"'.format(subject.replace('"', "")))
    if not parts and body_phrase:
        parts.append(f'"{body_phrase}"')
    return " ".join(parts)


AUTH_TAB = "認証コード設定"
AUTH_HEADERS = ["キャリア名", "Gmail検索条件", "抜き出しパターン(正規表現)", "有効"]

def render_auth_code_settings(project_id, config=None, proj_data=None):
    """🔐 二段階認証（メールに届く認証コード）の設定。

    ログイン情報の下・手順書の上に置く。録画→ID/パス→認証コード→手順書、と
    実際に設定する順番どおりに並べるため。
    設定は進捗反映と同じスプレッドシートに書き、GAS(fetchAuthCodes)がそれを読む。
    """
    with st.expander("🔐 メールで届く認証（コード／ログインのリンク）"):
        st.caption("ログインのときにメールが届くサイト向けです。"
                   "設定すると、GASがメールから**認証コード**または**ログインのリンク**を取り出し、"
                   "ロボットが自動で入力（リンクなら自動で開く）します。"
                   "設定しない場合は、手順書で「人の操作を待つ」を使って手作業でもできます。")
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
        # 📩 メールに届くのは「数字のコード」か「ログインのリンク」か。
        #    どちらもGASの取り出し方（正規表現）が違うだけで、通る道は同じ。
        _KIND_CODE, _KIND_LINK = "認証コード（数字）", "ログインのリンク（URL）"
        _saved_pat = str(cur.get("抜き出しパターン(正規表現)", ""))
        _kind_default = 1 if "http" in _saved_pat else 0
        kind = st.radio("メールに届くもの", [_KIND_CODE, _KIND_LINK],
                        index=_kind_default, horizontal=True, key=f"authkind_{project_id}")
        is_link = (kind == _KIND_LINK)
        if is_link:
            st.caption("🔗 メール内のボタン（ログインのリンク）を、ロボットが開きます。"
                       "リンクは使い切りなので、**実行のたびに届いた最新のもの**を取りに行きます。")

        _qkey = f"authq_{project_id}"
        _qmade = st.session_state.pop(f"authq_made_{project_id}", None)
        if _qmade:
            st.session_state[_qkey] = _qmade
        elif _qkey not in st.session_state:
            st.session_state[_qkey] = str(cur.get("Gmail検索条件", ""))
        q = st.text_input(("ログインのメールの探し方" if is_link else "認証コードのメールの探し方"),
                          key=_qkey,
                          placeholder=("from:no-reply@example.com subject:ログイン" if is_link
                                       else "from:no-reply@example.jp subject:認証コード"))
        st.caption("💡 下の「📩 届いたメールから設定を作る」を使えば、ここは自動で入ります。")
        # 入力欄の中身は、この欄自身に覚えさせる（画面を描き直しても消えないように）。
        # 「メールから自動で作る」で作れたときは、欄を作る前にその値を入れておく。
        _pkey = f"authp_{project_id}"
        _made = st.session_state.pop(f"authp_made_{project_id}", None)
        if _made:
            st.session_state[_pkey] = _made
        elif _pkey not in st.session_state:
            st.session_state[_pkey] = (_saved_pat
                                       or (r"(https?://[^\s\"'<>]+)" if is_link
                                           else r"認証コード[^0-9]{0,10}([0-9]{4,8})"))
        pat = st.text_input(("リンクの抜き出しかた（正規表現）" if is_link
                             else "コードの抜き出しかた（正規表現）"), key=_pkey,
                            help="( ) の中が取り出されます")
        st.caption("💡 下の「📩 届いたメールから自動で作る」に本文を貼れば、ここは自動で入ります。"
                   if is_link else
                   "💡 本文が「認証コードは 123456 です」なら、この既定のままで拾えます。")

        # 📩 実物のメールから、パターンを自動で作る。
        #    正規表現を担当者に書かせるのは現実的でないため、貼るだけで済むようにする。
        with st.expander("📩 届いたメールから自動で作る（おすすめ）", expanded=not str(cur.get("Gmail検索条件", ""))):
            st.caption("届いたメールを、そのまま貼り付けてください。"
                       + ("リンクの探し方をアプリが組み立てます。" if is_link
                          else "コードの探し方をアプリが組み立てます。"))
            _m1, _m2 = st.columns(2)
            with _m1:
                _from = st.text_input("差出人（メールの送信元アドレス）", key=f"authfrom_{project_id}",
                                      placeholder="例：no-reply@gmobb.jp")
            with _m2:
                _subj = st.text_input("件名（毎回同じ部分）", key=f"authsubj_{project_id}",
                                      placeholder="例：ログイン認証コード")
            st.caption("💡 どちらか片方だけでもかまいません。"
                       "件名は毎回変わらない部分だけでOK（記号や日付は入れないほうが確実）。")
            sample = st.text_area("メールの本文", key=f"authsample_{project_id}", height=140,
                                  placeholder=("https://example.com/login?…（ログインのリンクを含めて"
                                               "そのまま貼ってください）" if is_link
                                               else "認証コード：5021（届いたメールをそのまま貼ってください）"))
            if is_link:
                st.caption("⚠️ ボタンだけで**リンクの文字が見えない**メールのときは、"
                           "ボタンを右クリック →「リンクのアドレスをコピー」して、それを貼ってください。")
                code_hint = ""
            else:
                code_hint = st.text_input("そのメールに書かれていたコード（分かれば）",
                                          key=f"authcode_{project_id}", placeholder="例：5021")
            if st.button("📩 このメールから設定を作る", key=f"authgen_{project_id}", type="primary"):
                if not sample.strip():
                    st.warning("メールの本文を貼ってください。")
                else:
                    _p, _why = (guess_link_pattern(sample) if is_link
                                else guess_code_pattern(sample, code_hint))
                    # 本文の最初の行は、たいてい毎回同じ文言なので検索の手掛かりに使える
                    _first = next((ln.strip() for ln in sample.splitlines() if ln.strip()), "")
                    _query = build_gmail_query(_from, _subj, _first[:20])
                    _msgs = []
                    if _query:
                        st.session_state[f"authq_made_{project_id}"] = _query
                        _msgs.append(f"🔎 メールの探し方：`{_query}`")
                    else:
                        _msgs.append("⚠️ 差出人か件名を入れると、メールを絞り込めます（空だと似た他のメールも拾います）")
                    _what = "リンク" if is_link else "コード"
                    if _p:
                        st.session_state[f"authp_made_{project_id}"] = _p
                        _msgs.append(f"🔗 {_what}の取り出し方：{_why}")
                    else:
                        _msgs.append(f"❌ {_what}の取り出し方は作れませんでした（{_why}）")
                    st.success("　／　".join(_msgs))
                    if _p or _query:
                        st.caption("上の欄に入りました。内容を見て「💾 この設定を保存」を押してください。")
                        st.rerun()

        if st.button("💾 この設定を保存", key=f"authsave_{project_id}"):
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
                    if is_link:
                        st.success("保存しました。手順書では、操作を「**メールのリンクを開く**」にして、"
                                   f"値に `{key_name.strip()}` と書いてください"
                                   "（メールを送らせるボタンを押す手順の、すぐ次に置きます）。")
                    else:
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
                if (not is_link) and re.search(r"[0-9]{4,}", str(_saved[2])) and "(" not in str(_saved[2]):
                    st.error("　❌ 抜き出しパターンに、コードの数字そのものが入っています。"
                             "上の「📩 届いたメールから自動で作る」で作り直してください。")
                    _ok = False
                if is_link and "http" not in str(_saved[2]):
                    st.error("　❌ 取り出し方がリンク用になっていません（`http` が入っていません）。"
                             "上の「📩 届いたメールから自動で作る」で作り直してください。")
                    _ok = False

            # ② GASがコードを書き込めているか
            _codes = _read_tab_values(f"{_url}|認証コード", gc, _url, "認証コード") or []
            _mine = [r for r in _codes[1:] if r and str(r[0]).strip() == key_name.strip()]
            _what2 = "ログインのリンク" if is_link else "コード"
            if _mine:
                st.success(f"② {_what2}を受け取れています（最後に取れたのは {_mine[0][2]}）")
                if is_link and not str(_mine[0][1]).lower().startswith("http"):
                    st.error("　❌ 取れているのはURLではありません。取り出し方を見直してください。")
                    _ok = False
            else:
                st.warning(f"② まだ{_what2}を受け取れていません。"
                           "**そのメールが届いている状態で**、"
                           "GASの `fetchAuthCodes` を手動実行してみてください。"
                           "うまくいけば、この行にコードが入ります。")
                _ok = False

            # ③ 手順書側で使う設定になっているか
            _steps_chk = ((config or {}).get("robot_config", {}) or {}).get("steps", []) or []
            _want_op = "メールのリンクを開く" if is_link else "認証コードを入力"
            _used = [s for s in _steps_chk
                     if str((s or {}).get("操作", "")) == _want_op
                     and str((s or {}).get("値", "")).strip() == key_name.strip()]
            if _used:
                st.success(f"③ 手順書の #{_used[0].get('順番')} で、この設定を使う指定になっています")
                if is_link and not str(_used[0].get("目印", "") or "").strip():
                    # ⚠️ ログイン済みの日はメールが来ない。目印が無いと、来ないメールを
                    #    ずっと待って失敗する（気づきにくいので、設定の時点で言う）。
                    st.warning("　⚠️ この手順に**目印**が入っていません。ログイン済みの日は"
                               "メールが届かないので、**来ないメールを待ち続けて失敗します**。"
                               "ログイン画面にしか出ない文字（例：`ログイン`）を目印に入れてください。"
                               "メールを送らせるボタンの手順にも、同じ目印を入れます。")
            else:
                st.warning(f"③ 手順書に「{_want_op}（値：{key_name.strip()}）」の手順がありません。"
                           + ("手順書の表で、メールを送らせるボタンの次の行に足してください。"
                              if is_link else "下の「🔁 差し替える」で設定してください。"))
                _ok = False

            if _ok:
                st.info("✅ 3つとも整っています。実行時に"
                        + ("ログインのリンクが自動で開きます。" if is_link
                           else "コードが自動で入ります。"))

        # 🔁 録画した「認証コードを打った手順」を、自動入力に差し替える
        #    （録画時のコードは失効しているので、そのままでは毎回失敗する）
        steps_now = ((config or {}).get("robot_config", {}) or {}).get("steps", []) or []
        if steps_now and proj_data is not None and not is_link:
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

def render_browser_dialog_settings(project_id, config, proj_data):
    """🗨 ブラウザ本体の小窓（「◯◯を入力してください」）への答えを登録する。

    ⚠️ これは**ページの中のモーダルではない**。ブラウザが出す小窓なので、
       **録画に写らず、手順書にも書けない**。
    ⚠️ そして Playwright は、何も用意しないとこの小窓を**勝手にキャンセルする**。
       メールのリンクを開いた先で「ログインするためのemailを入力してください」と
       聞かれる作り（メールリンク認証）は、それだと必ず失敗する。
    🛡 見覚えのない小窓にまでOKを押すと、確かめないまま申請してしまうことがある。
       だから「この文字が入っている小窓だけ」に答える。
    """
    with st.expander("🗨 ブラウザの小窓に自動で答える（メールリンク認証など）"):
        st.caption("メールのリンクを開いた直後に、ブラウザが**小さな窓**で"
                   "「ログインするためのemailを入力してください」と聞いてくることがあります。"
                   "この窓は**録画に写らない**ので、ここで答えを登録しておきます。")
        _rc = config.setdefault("robot_config", {})
        _marker = st.text_input(
            "小窓に出る文字（この文字が入っているときだけ答えます）",
            value=str(_rc.get("dialog_marker", "") or ""),
            placeholder="例：email", key=f"dlgmark_{project_id}")
        _answer = st.text_input(
            "入れる答え", value=str(_rc.get("dialog_answer", "") or ""),
            placeholder="例：info@lifeap.co.jp　または　{秘密:ログインID}",
            key=f"dlgans_{project_id}")
        st.caption("💡 `{秘密:名前}` と書けば、🔑ログイン情報に登録した値（暗号化保存）を使えます。")
        st.warning("⚠️ 文字を空にすると、この機能は使いません（小窓はこれまでどおり閉じられます）。"
                   "**「送信しますか？」のような確認の窓に答えてしまわないよう**、"
                   "その窓だけに出る言葉を入れてください。")
        try:
            _wait_now = int(_rc.get("mail_wait_sec", 180) or 180)
        except Exception:
            _wait_now = 180
        _wait = st.number_input("メールが届くのを待つ時間（秒）", min_value=60, max_value=1800,
                                step=30, value=max(60, min(1800, _wait_now)),
                                key=f"dlgwait_{project_id}")
        st.caption("認証コード・ログインのリンクの両方に効きます（既定は180秒）。"
                   "メールが遅いサイトでは延ばしてください。")
        if st.button("💾 小窓の設定を保存", key=f"dlgsave_{project_id}"):
            _rc["dialog_marker"] = _marker.strip()
            _rc["dialog_answer"] = _answer.strip()
            _rc["mail_wait_sec"] = int(_wait)
            proj_data["config_json"] = config
            save_project(project_id, proj_data)
            st.success("保存しました。" + ("小窓が出た日だけ、自動で答えます。" if _marker.strip()
                                        else "小窓への自動応答は使いません。"))


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
                    # このファイルはアプリ直下にあるので、.streamlit も同じ階層にある
                    _sec_path = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
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
