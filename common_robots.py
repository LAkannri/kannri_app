"""
🤖 共通ロボットの登録（録画して覚えさせる場所）

SFコネクタの更新も、GASのCSV書き出しも、プッシュプロの一括送信も、
**どのスプレッドシートでも押す場所は同じ**。違うのは「どのシートを選ぶか」
「どのファイルを渡すか」だけ。だから手順書は共通で1台ずつ作れば足りる。

ここで一度だけ録画しておけば、
「📱 SMS送信」でも「🗃 データローダー自動化」でも、**シート名を選ぶだけ**で動く。

  共通_SFコネクタ更新 … 拡張機能 → SFコネクタ → リフレッシュ → シートを選ぶ → 手動リフレッシュ
                        「シートを選ぶ」手順の対象／値を {更新するシート} にしておくと、
                        実行時にシート名が入れ替わり、登録したシートのぶんだけ繰り返す。
  共通_CSV書き出し    … スプシのメニュー → CSV書き出し → 「PC保存＋Drive保存」を押す
  共通_プッシュプロ一括送信 … ログイン → CSVを選ぶ → 一括送信（最後の送信は『送信（本番のみ）』）
                        CSVは毎回おなじ名前で置かれるので、録画で選んだファイルのまま動く。
"""
import os
import re
import time

import pandas as pd
import streamlit as st

import robot_settings_ui
import steps_ai
import theme

ROBOT_KIND = "SMS送信"        # このページで作るロボットの目印（申請用と混ぜないため）

# 役割 → 既定の名前・説明・録画のコツ・値のルール
ROLES = {
    "refresh": {
        "name": "共通_SFコネクタ更新",
        "title": "🔄 SFコネクタでシートを更新する",
        "why": "登録したシートを順に更新します。SMS送信でもデータローダーでも同じ1台を使います。",
        "hint": ("スプレッドシートを開いた状態から、**拡張機能 → Salesforceコネクタ → リフレッシュ**"
                 "と進み、**更新したいシート名を選んで → 手動リフレッシュ** を押すところまで操作してください。"
                 "1枚ぶんでOKです（残りは同じ手順を繰り返します）。"),
        "rule": lambda: steps_ai.VALUE_RULE_REFRESH,
        "check": "sheet_var",
        "login_url": "https://docs.google.com/spreadsheets/",
        "login_note": ("スプレッドシートに鍵がかかっているので、ロボットが開くと"
                       "**Googleのログイン画面**になります。ここで一度だけログインしておけば、"
                       "そのログイン状態はこのロボット専用のブラウザに残るので、次からは素通りできます。"),
        "url_note": ("ここは**録画のときに開くだけ**のURLです。実行するときは、"
                     "「📱 SMS送信」「🗃 データローダー自動化」に登録した"
                     "**そのスプレッドシートのURL**で開きます。"
                     "だから、どのスプシで録画しても構いません。"),
    },
    "export": {
        "name": "共通_CSV書き出し",
        "title": "📄 スプシのGASでCSVを書き出す（ふつうは不要）",
        "why": ("⚠️ **ふつうはこのロボットは要りません。** GASを『ウェブアプリ』としてデプロイすれば、"
                "アプリがURLを叩くだけで同じCSVを受け取れます（`gas/SMS_CSV書き出しWebAPI.gs`）。"
                "サイドバーはスプシの中の小さな画面なので、録画はうまくいかないことがあります。"
                "どうしてもデプロイできないときの逃げ道として残してあります。"),
        "hint": ("スプレッドシートを開いた状態から、メニューの **CSV書き出し** を開き、"
                 "サイドバーの **「⬇ PC保存＋Drive保存」** を押すところまで操作してください。"),
        "rule": lambda: steps_ai.VALUE_RULE_INTAKE,
        "check": "",
        "url_note": "実行するときは、登録したスプレッドシートのURLで開きます。",
    },
    "send": {
        "name": "共通_プッシュプロ一括送信",
        "title": "🚀 プッシュプロで一括送信する",
        "why": "その日のCSVを渡して一括送信します。CSVは毎回おなじ名前で置かれます。",
        "hint": ("プッシュプロにログイン → **CSVを実際に選んで** → 一括送信の直前"
                 "（送信ボタンを押す手前）まで操作してください。"),
        "rule": lambda: steps_ai.VALUE_RULE_SMS,
        "check": "upload_submit",
        "login_url": "https://ppsms.jp/",
        "login_note": ("プッシュプロのログインも、ここで一度済ませておけば次から省けます。"),
        "url_note": "プッシュプロは毎回同じ画面なので、実行時もこのURLで開きます。",
    },
}


def list_robots(supabase):
    """ここで作った共通ロボットの名前を集める。"""
    try:
        rows = supabase.table("merchants").select("id,config_json").execute().data or []
    except Exception:
        return []
    return sorted(r["id"] for r in rows
                  if not str(r["id"]).startswith("__")
                  and str((r.get("config_json") or {}).get("product_type", "")) == ROBOT_KIND)


def robot_row(supabase, name: str):
    try:
        rows = supabase.table("merchants").select("*").eq("id", name).execute().data or []
    except Exception:
        return None, []
    if not rows:
        return None, []
    conf = rows[0].get("config_json") or {}
    return rows[0], (conf.get("robot_config", {}) or {}).get("steps", []) or []


def _save_steps(supabase, row: dict, steps):
    conf = row.get("config_json") or {}
    conf.setdefault("robot_config", {})["steps"] = steps
    supabase.table("merchants").upsert({
        "id": row["id"], "name": row.get("name") or row["id"], "is_active": False,
        "connector_type": "playwright", "config_json": conf}).execute()


def screen_size():
    """このPCの画面の大きさ。取れなければ無難な値を返す。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if w > 400 and h > 300:
            return int(w), int(h)
    except Exception:
        pass
    return 1280, 800


def record_viewport():
    """録画ブラウザの表示領域。画面からはみ出して下が切れないように決める。

    Playwright は既定で 1280x720 の表示領域を作る。ノートPCのように画面が低いと、
    アドレスバーやタブのぶんだけ縦に足りず、**画面の下が切れて押せなくなる**。
    画面の高さから、ブラウザの枠のぶんを引いた大きさにしておく。
    """
    w, h = screen_size()
    return max(900, min(1280, w - 40)), max(420, h - 190)


def profile_dir(robot_name: str) -> str:
    """そのロボット専用のブラウザ（Chromeプロファイル）の置き場所。"""
    base = os.path.dirname(os.path.abspath(__file__))
    name = re.sub(r'[\\/:*?"<>|]', "_", str(robot_name or "default").strip()) or "default"
    return os.path.join(base, ".enkan_profile", name)


def profile_in_use(robot_name: str) -> bool:
    """そのプロファイルを、いま別のブラウザが使っているか。

    Chromeは同じプロファイルを2つ同時に開けない。開いたままだと録画が始まらないので、
    先に気づけるようにする（SingletonLock はChromeが使用中に作る目印）。
    """
    d = profile_dir(robot_name)
    return any(os.path.exists(os.path.join(d, n))
               for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"))


def login_status(robot_name: str):
    """ログイン状態（Cookie）が残っているか。戻り値：(残っているか, 最終更新の日時)

    「ログインしておく」は手順書を作らないので、押しても画面に何も増えない。
    それだと『効いたのかどうか分からない』ので、Cookieの有無を状態として見せる。
    """
    ck = os.path.join(profile_dir(robot_name), "Default", "Network", "Cookies")
    if not os.path.isfile(ck) or os.path.getsize(ck) < 1024:
        return False, ""
    return True, time.strftime("%Y/%m/%d %H:%M", time.localtime(os.path.getmtime(ck)))


def _login_block(role_key: str, robot_name: str):
    """🔐 一度だけログインしておくためのボタン。

    鍵つきスプレッドシートやプッシュプロは、ロボットが開くとログイン画面になる。
    毎回そこで人を待つのは現実的でないので、**最初に一度だけ人がログインしておく**。
    Cookie はこのロボット専用のブラウザ（プロファイル）に残るので、次からは素通りできる。
    Google の二段階認証も、ここで「このデバイスを信頼する」まで済ませれば以後は出ない。
    """
    import subprocess
    import sys
    role = ROLES[role_key]
    if not role.get("login_url"):
        return
    st.markdown("**🔐 ログインしておく（初回だけ）**")
    st.info("📌 ここは**録画ではありません**。ロボットが実際に使うブラウザを開くだけです。"
            "打ち込んだID・パスワード・認証コードは、**どこにも記録されません**"
            "（AIにも送りません／データベースにも保存しません）。"
            "残るのは、そのブラウザの**ログイン状態（Cookie）だけ**です。")
    st.caption(role.get("login_note", ""))
    # 📍 いまログイン状態が残っているかを見せる。
    #    このボタンは手順書を作らないので、画面に何も増えず「効いたのか分からない」ため。
    _ok, _when = login_status(robot_name)
    if _ok:
        st.success(f"✅ このロボットのブラウザには、**ログイン状態が残っています**"
                   f"（最終更新：{_when}）。実行時はログイン画面を素通りできます。")
    else:
        st.warning("まだログイン状態がありません。下のボタンから一度ログインしてください。")
    st.caption(f"置き場所：`{profile_dir(robot_name)}`"
               "（このフォルダはGitHubにも配布ZIPにも入りません）")

    url = st.text_input("ログインしに行く画面", key=f"cr_{role_key}_lgurl",
                        value=role["login_url"])
    b1, b2 = st.columns([1, 1])
    with b1:
        _go = st.button("🔐 ログイン用のブラウザを開く", key=f"cr_{role_key}_lg",
                        use_container_width=True)
    with b2:
        if st.button("🔄 いまの状態を見直す", key=f"cr_{role_key}_lgchk",
                     use_container_width=True,
                     help="ログインしてブラウザを閉じたあと、これを押すと結果が反映されます"):
            st.rerun()
    if _go:
        try:
            _p = subprocess.Popen(
                [sys.executable, "robot.py", "--login", robot_name, url.strip()],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace")
            # すぐ落ちていないか確かめる。落ちていたのに「開きます」と言わないため。
            time.sleep(2)
            if _p.poll() is not None:
                _out = ""
                try:
                    _out = (_p.stdout.read() or "")[-1500:]
                except Exception:
                    pass
                st.error("ブラウザを開けませんでした（すぐ終了しました）。下の内容を確認してください。")
                st.code(_out or "（出力なし）")
            else:
                st.success("ブラウザが開きます（別ウィンドウを探してください）。"
                           "ログイン（必要なら二段階認証）を済ませて、"
                           "**「このデバイスを信頼する」があれば必ずチェック**してから、"
                           "**ブラウザを閉じてください**。閉じた時点で記録されます。")
                st.info("👉 閉じたあと、上の「🔄 いまの状態を見直す」を押すと"
                        "「✅ ログイン状態が残っています」に変わります。"
                        "**手順書は作られません**（ここは録画ではないため）。")
        except Exception as e:
            st.error(f"開けませんでした（このPCで開いていない可能性）: {e}")
    st.caption("💡 それでも実行時にログイン画面が出るときは、下の手順表に "
               "**「人の操作を待つ」** を足しておくと、そこで待って人がログインし、"
               "終わると自動で先へ進みます。")
    st.caption("💡 **録画とは別物なので、どちらが先でも構いません。** 録画（🎬）のブラウザは"
               "まっさらな状態で開くので、ここでログインしておいても、"
               "録画のときはちゃんとログイン画面から記録できます。")


def _record_block(supabase, role_key: str, default_url: str = ""):
    """録画 → 手順書づくり。作れたら画面を作り直す。"""
    import subprocess
    import sys
    role = ROLES[role_key]
    box = f"cr_{role_key}"

    name = st.text_input("ロボットの名前", key=f"{box}_name", value=role["name"])
    st.caption("💡 **この名前のまま**にしておけば、どのページ・どのスプシからも同じ1台を使えます。")
    url = st.text_input("録画するときに開くURL", key=f"{box}_url", value=default_url,
                        placeholder="https://...")
    if role.get("url_note"):
        st.caption("📌 " + role["url_note"])

    # 🔐 録画をどのブラウザで始めるか。
    #    ふつうのcodegenはまっさらなブラウザなので、毎回ログイン画面から始まる。
    #    ロボット自身のブラウザを使えば、ログイン済みの状態から録画できる＝
    #    ログイン操作を録らずに済み、パスワードがAIに渡ることもない。
    _use_profile = False
    if role.get("login_url"):
        _logged, _when = login_status(role["name"])
        _use_profile = st.checkbox(
            "ログイン済みのブラウザで録画する（ログイン画面から始めたくないとき）",
            value=_logged, key=f"{box}_useprof",
            help="ロボット自身のブラウザ（ログイン状態が残っているもの）で録画します。")
        if _use_profile:
            if not _logged:
                st.warning("⚠️ まだログイン状態がありません。先に上の"
                           "「🔐 ログイン用のブラウザを開く」でログインしてください。")
            elif profile_in_use(role["name"]):
                st.error("⚠️ そのブラウザが**まだ開いたまま**です。"
                         "同じブラウザは2つ同時に開けないので、**先に閉じてから**"
                         "録画を始めてください。")
            else:
                st.success("✅ ログイン済みの状態から録画できます。"
                           "**ログインの手順は録らなくてよい**ので、"
                           "拡張機能 → SFコネクタ → …だけを操作してください。")
            st.caption("⚠️ この場合、手順書にログインが入りません。"
                       "セッションが切れた日に自動で入り直せないので、"
                       "下の「✋ ログイン待ちの手順を先頭に足す」を入れておくと安心です。")
        else:
            st.caption("💡 チェックを外すと、まっさらなブラウザで開きます"
                       "（ログイン画面から始まるので、ログイン操作も録画に含められます）。")

    a, b = st.columns(2)
    with a:
        if st.button("🎬 録画を開始する（このPC）", key=f"{box}_rec", use_container_width=True):
            if not url.strip():
                st.warning("先にURLを入れてください。")
            elif _use_profile and profile_in_use(role["name"]):
                st.error("ログイン用のブラウザを先に閉じてください（同じブラウザは2つ開けません）。")
            else:
                _cmd = [sys.executable, "-m", "playwright", "codegen"]
                # 画面からはみ出して下が切れないよう、表示領域を画面に合わせる
                _vw, _vh = record_viewport()
                _cmd.append(f"--viewport-size={_vw},{_vh}")
                if _use_profile:
                    _cmd += ["--channel=chrome",
                             "--user-data-dir=" + profile_dir(role["name"])]
                _cmd.append(url.strip())
                try:
                    _rp = subprocess.Popen(_cmd, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True,
                                           encoding="utf-8", errors="replace")
                    time.sleep(2)
                    if _rp.poll() is not None:
                        _o = ""
                        try:
                            _o = (_rp.stdout.read() or "")[-1500:]
                        except Exception:
                            pass
                        st.error("録画を開始できませんでした（すぐ終了しました）。")
                        st.code(_o or "（出力なし）")
                    else:
                        st.success("ブラウザが開きます。操作が終わったら、"
                                   "録画ウィンドウのコードをコピーして下に貼ってください。")
                except Exception as e:
                    st.error(f"録画を開始できませんでした（このPCで開いていない可能性）: {e}")
    with b:
        _vw, _vh = record_viewport()
        st.caption(f"💡 録画ブラウザは、この画面に合わせて {_vw}×{_vh} で開きます"
                   "（下が切れて押せなくなるのを防ぐため）。"
                   "それでも見きれるときは、ブラウザで **Ctrl と −（マイナス）** を押して"
                   "縮小してください。録画はそのまま続けられます。")
        st.caption("💡 ログイン画面から録画する場合、パスワードは本物で入力してOKです"
                   "（伏せ字にしてから保存します）。")

    code = st.text_area("録画したコードを貼り付け", key=f"{box}_code", height=160)
    # 🔒 AIに送る前に何を伏せるか。パスワードは必ず伏せる（選べない）。
    #    IDは業務上そのままでよいことも多いが、Googleアカウントのように
    #    「IDだけでも渡したくない」ものがあるので、ログインのあるロボットは既定で伏せる。
    hide_id = st.checkbox("ログインID（メールアドレス）も伏せてからAIに送る",
                          value=bool(role.get("login_url")), key=f"{box}_hideid",
                          help="伏せた分は、あとで「🔑 ログイン情報」に "
                               "『ログインID』という名前で登録してください。")
    st.caption("🔒 **パスワードは必ず伏せてから**AI（Gemini）に送ります（外せません）。")
    st.caption("⚠️ 同じ名前で作り直すと、**手順書は新しい録画で置き換わります**"
               "（ログイン情報の設定は残ります）。")

    if st.button("✨ 手順書を作る", key=f"{box}_make", type="primary"):
        if not (name.strip() and code.strip() and url.strip()):
            st.warning("名前・URL・録画したコードの3つが必要です。")
            return
        if not str(st.secrets.get("GEMINI_API_KEY", "")).strip():
            st.error("接続キー GEMINI_API_KEY が未設定です。")
            return
        try:
            import google.generativeai as genai
            # 🔒 AIに送る前に伏せる。ここを通ったコードだけを Gemini に渡す。
            clean, n_red = steps_ai.redact_passwords(code)
            n_id = 0
            if hide_id:
                clean, n_id = steps_ai.redact_logins(clean)
            if n_red:
                st.info(f"🔒 パスワード欄の入力 {n_red}件を伏せました（`{{秘密:パスワード}}` に置き換え）。")
            if n_id:
                st.info(f"🔒 ログインID欄の入力 {n_id}件を伏せました（`{{秘密:ログインID}}` に置き換え）。"
                        "実際のIDは下の「🔑 ログイン情報」に **ログインID** という名前で登録してください。")
            elif hide_id and ".fill(" in code:
                st.warning("⚠️ ログインID欄は見つかりませんでした。手順表の『値』に"
                           "メールアドレスがそのまま残っていないか確かめてください。")
            elif ".fill(" in code:
                # 伏せ字は「欄の名前にパスワードらしい語があるか」で見分けている。
                # 見分けられなかったときに黙っていると、平文のまま保存されてしまう。
                st.warning("⚠️ **パスワード欄を自動で見つけられませんでした。**"
                           "下の手順表の『値』に、本物のパスワードがそのまま残っていないか"
                           "**必ず確かめてください**。残っていたら `{秘密:パスワード}` に書き換え、"
                           "実際の値は下の「🔑 ログイン情報」に登録してください。")
            genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
            model = genai.GenerativeModel("gemini-2.5-flash")
            with st.spinner("🤖 手順書を作っています..."):
                resp = model.generate_content(
                    steps_ai.build_prompt(clean, role["rule"]()),
                    generation_config={"response_mime_type": "application/json"})
            steps = steps_ai.strip_redundant_field_clicks(steps_ai.parse_steps(resp.text))
            for i, stp in enumerate(steps):
                stp["順番"] = i + 1
            old, _ = robot_row(supabase, name.strip())
            conf = (old.get("config_json") or {}) if old else {}
            conf.setdefault("robot_config", {})
            conf["product_type"] = ROBOT_KIND
            conf["needs_recording"] = True
            conf["sms_purpose"] = role_key
            conf["robot_config"].update({"target_url": url.strip(), "steps": steps,
                                         "skeleton": steps, "stealth": True})
            conf.setdefault("spreadsheet", {})
            conf.setdefault("notifications", {})
            conf.setdefault("conditions", [])
            supabase.table("merchants").upsert({
                "id": name.strip(), "name": name.strip(), "is_active": False,
                "connector_type": "playwright", "config_json": conf}).execute()
            st.success(f"✅ ロボット「{name.strip()}」を作りました（{len(steps)}手順）。"
                       "下の表で、変えたいところを直せます。")
            st.rerun()
        except Exception as e:
            st.error(f"手順書を作れませんでした: {e}")


def _steps_editor(supabase, robot_name: str, role_key: str):
    """手順書を表で直す。役割ごとに「足りないもの」を警告する。"""
    row, steps = robot_row(supabase, robot_name)
    if not row:
        st.info("まだ手順書がありません。上で録画してください。")
        return

    # 🔑 ID・パスワードは暗号化して保存し、手順書には {秘密:名前} だけを書く。
    #    鍵（ENKAN_SECRET_KEY）はこのPCの secrets.toml にだけ置くので、
    #    データベース（Supabase）が見られても、値そのものは取り出せない。
    robot_settings_ui.render_login_secrets(row["id"], row.get("config_json") or {}, row)
    key = f"cr_{role_key}_ed"
    df = pd.DataFrame([{"順番": s.get("順番", i + 1),
                        "いつ": s.get("いつ", "常に"),
                        "操作": s.get("操作", s.get("action", "")),
                        "対象": s.get("対象", s.get("target_description", "")),
                        "値": s.get("値", s.get("value", "")),
                        "目印": s.get("目印", "")} for i, s in enumerate(steps)])
    _when = ["常に", "送信（本番のみ）"] + sorted(
        {str(x) for x in df["いつ"].tolist() if str(x) not in ("常に", "送信（本番のみ）")})
    _ops = ["文字を入力", "クリック", "選択", "チェック", "ファイルをアップロード",
            "ファイルをダウンロード", "人の操作を待つ", "日付を入れる"]
    _ops += sorted({str(x) for x in df["操作"].tolist() if str(x) not in _ops})
    edited = st.data_editor(df, key=key, use_container_width=True, num_rows="fixed",
                            column_config={
                                "いつ": st.column_config.SelectboxColumn(options=_when, width="small"),
                                "操作": st.column_config.SelectboxColumn(options=_ops, width="medium"),
                                "目印": st.column_config.TextColumn(
                                    width="small",
                                    help="この文字が画面にある日だけ、その手順を行います。"
                                         "空なら毎回行います（例：ログイン手順に「パスワード」）"),
                            })
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("💾 手順を保存", key=f"{key}_save", use_container_width=True):
            for i, s in enumerate(steps):
                if i < len(edited):
                    r = edited.iloc[i]
                    s["順番"] = int(r["順番"]) if str(r["順番"]).strip() else i + 1
                    s["いつ"] = str(r["いつ"] or "常に")
                    s["操作"] = str(r["操作"] or "")
                    s["対象"] = str(r["対象"] or "")
                    s["値"] = str(r["値"] or "")
                    s["目印"] = str(r.get("目印", "") or "")
            _save_steps(supabase, row, steps)
            st.success("保存しました。")
            st.rerun()

    # 🎯 ログインの手順は「ログイン画面が出た日だけ」行いたい。
    #    ログイン済みの日は入力欄が無いので、目印が無いと「欄が見つかりません」で止まる。
    if ROLES[role_key].get("login_url"):
        _login_steps = [i for i, s in enumerate(steps)
                        if "{秘密:" in str(s.get("値", ""))
                        or any(w in str(s.get("対象", ""))
                               for w in ("パスワード", "ID", "ｉｄ", "メール", "アドレス",
                                         "次へ", "ログイン", "Password", "Email"))]
        _no_marker = [i for i in _login_steps if not str(steps[i].get("目印", "")).strip()]
        if _no_marker:
            st.warning(f"⚠️ ログインらしい手順が {len(_no_marker)}件 ありますが、"
                       "**『目印』が空**です。ログイン済みの日は入力欄が出ないので、"
                       "このままだと「欄が見つかりません」で止まります。")
            st.caption("💡 目印は、その手順の**「対象」（入力欄の名前）をそのまま**使うのが確実です。"
                       "Googleは「メールアドレス → 次へ → パスワード → 次へ」と画面が分かれるので、"
                       "**手順ごとに違う目印**が要ります（全部に同じ文字を入れると、"
                       "最初の画面でパスワード欄を探しに行って止まります）。")
            if st.button("🎯 ログインの手順に目印を付ける（対象の文字を使う）", key=f"{key}_setmk"):
                _n = 0
                for i in _no_marker:
                    # その欄が画面にあれば、欄の名前も画面にあるはず。だから対象をそのまま目印にする。
                    _t = str(steps[i].get("対象", "")).strip()
                    if _t:
                        steps[i]["目印"] = _t
                        _n += 1
                _save_steps(supabase, row, steps)
                st.success(f"{_n}件に目印を付けました（それぞれの「対象」の文字）。"
                           "その欄が画面に出た日だけ入力し、出ない日は飛ばします。"
                           "うまくいかない手順は、表の『目印』を直してください。")
                st.rerun()

    # ✋ ログイン画面が出たときに、そこで人を待つ手順を足せるようにする。
    #    『目印』を入れておけば、ログイン画面が出なかった日は素通りする（止まりっぱなしにならない）。
    if ROLES[role_key].get("login_url"):
        if not any(str(s.get("操作", "")) == "人の操作を待つ" for s in steps):
            if st.button("✋ ログイン待ちの手順を先頭に足す", key=f"{key}_addwait"):
                steps.insert(0, {"順番": 0, "いつ": "常に", "操作": "人の操作を待つ",
                                 "対象": "ログイン", "値": "パスワード", "ai_code": ""})
                for i, stp in enumerate(steps):
                    stp["順番"] = i + 1
                _save_steps(supabase, row, steps)
                st.success("先頭に足しました。『値』の「パスワード」が画面に無い日は素通りします。")
                st.rerun()
            st.caption("💡 実行時にログイン画面で止まるようなら、これを足しておくと"
                       "**人がログインするまで待って、終わったら自動で先へ進みます**。")

    check = ROLES[role_key]["check"]
    _all = " ".join(str(s.get("対象", "")) + str(s.get("値", "")) for s in steps)

    if check == "sheet_var":
        if "{更新するシート}" not in _all:
            st.warning("⚠️ **シート名を差し替える印（`{更新するシート}`）がありません。**"
                       "このままだと、録画したときの1枚しか更新できません。"
                       "『更新したいシートを選ぶ』手順の **対象（または値）を "
                       "`{更新するシート}` に書き換えて**保存してください。")
        else:
            st.success("✅ シート名を差し替える印（`{更新するシート}`）があります。"
                       "登録したシートのぶんだけ、続けて更新します。")

    if check == "upload_submit":
        with c2:
            if st.button("🚀 送信ステップを足す", key=f"{key}_addsub", use_container_width=True):
                st.session_state[f"{key}_ask"] = True
        if st.session_state.get(f"{key}_ask"):
            btn = st.text_input("最後に押す「送信」ボタンの文言", key=f"{key}_label",
                                placeholder="例：一括送信する")
            if st.button("追加する", key=f"{key}_go"):
                if not btn.strip():
                    st.warning("ボタンの文言を入れてください。")
                else:
                    steps.append({"順番": len(steps) + 1, "いつ": "送信（本番のみ）",
                                  "操作": "クリック", "対象": btn.strip(), "値": "", "ai_code": ""})
                    _save_steps(supabase, row, steps)
                    st.session_state[f"{key}_ask"] = False
                    st.rerun()
        if not any(str(s.get("操作", "")) == "ファイルをアップロード" for s in steps):
            st.warning("⚠️ **CSVを渡す手順（ファイルをアップロード）がありません。**"
                       "表の「操作」を『ファイルをアップロード』にして、"
                       "値を `{アップロードファイル}` にしてください。")
        if not any(str(s.get("いつ", "")).startswith(("送信", "申請")) for s in steps):
            st.warning("⚠️ **送信ステップがありません。** このままでは最後の「送信」が押されません。"
                       "「🚀 送信ステップを足す」で追加してください。")

        # ✅ 送れたことの確かめ方（偽の成功で「送った」と記録しないため）
        conf = row.get("config_json") or {}
        rc = conf.get("robot_config", {}) or {}
        st.markdown("**送信できたことの確かめ方**")
        st.caption("送信のあとに画面へ出る文字を入れておくと、"
                   "それが出なければ**失敗としてあつかい、送信済みに記録しません**。"
                   "空のままだと、送れたかどうかを自動で確かめられません。")
        ok_text = st.text_input("完了画面に出る文字", value=str(rc.get("success_text", "") or ""),
                                key=f"{key}_ok", placeholder="例：送信を受け付けました")
        if st.button("💾 確かめ方を保存", key=f"{key}_oksave"):
            rc["success_text"] = ok_text.strip()
            conf["robot_config"] = rc
            supabase.table("merchants").upsert({
                "id": row["id"], "name": row.get("name") or row["id"], "is_active": False,
                "connector_type": "playwright", "config_json": conf}).execute()
            st.success("保存しました。")
            st.rerun()


def render(supabase, default_urls: dict = None):
    """共通ロボットの登録パネルを描く（その他設定ページから呼ぶ）。"""
    default_urls = default_urls or {}
    have = list_robots(supabase)
    st.caption("💻 録画は**この画面を自分のPCで開いているとき**だけできます"
               "（記録用ブラウザがそのPCに開きます）。")
    st.warning("⚠️ 録画で**個人情報を入力しないでください**。"
               "テスト用のダミーを使ってください（入力した内容はAIに送られ、手順書にも残ります）。")

    for key, role in ROLES.items():
        with st.container(border=True):
            theme.section_title("🤖", role["title"])
            st.caption(role["why"])
            exists = role["name"] in have
            _badges = ["<span class='status-active'>手順書あり</span>" if exists
                       else "<span class='status-inactive'>まだ録画していません</span>"]
            if role.get("login_url"):
                _lg, _lgw = login_status(role["name"])
                _badges.append("<span class='status-active'>ログイン済み</span>" if _lg
                               else "<span class='status-inactive'>ログインまだ</span>")
            st.markdown("　".join(_badges), unsafe_allow_html=True)
            if role.get("login_url"):
                with st.expander("🔐 先にログインしておく（録画ではありません・初回だけ）",
                                 expanded=not exists):
                    _login_block(key, role["name"])
            with st.expander("🎬 録画して作る／作り直す", expanded=not exists):
                st.info("録画のコツ：" + role["hint"])
                _record_block(supabase, key, default_urls.get(key, ""))
            if exists:
                with st.expander("📝 手順を見る／直す", expanded=False):
                    _steps_editor(supabase, role["name"], key)
