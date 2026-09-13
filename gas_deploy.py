# -*- coding: utf-8 -*-
"""🔧 GASを、アプリが直接書き込んで公開する（コピペをなくす）

これまでは「アプリが出したコードを人がコピー → Apps Script に貼る →
合言葉を書き替える → デプロイ → 出てきたURLをアプリに貼り戻す」だった。
手順が多く、どこか1つでも抜けると「API_TOKEN が未設定です」で止まる。

ここでは **Apps Script API** を使って、アプリが自分で
  ① いまのコードを読む（丸ごと控えを取る）
  ② 合言葉を入れた連携コードを **専用のファイルとして** 書き込む
  ③ 新しいバージョンを作り、ウェブアプリとして公開（デプロイ）する
  ④ 出てきた `.../exec` のURLを設定に書き戻す
まで行う。人が貼るのは **スクリプトのURL 1つだけ**。

⚠️ Apps Script API は **サービスアカウントでは使えない**。スプシの持ち主
   （＝ふだんスクリプトを開いている人）の Google アカウントで、1回だけ
   「つないでよい」と許可してもらう（OAuth）。その許可は使い回せる。

⚠️ そのアカウントで **Apps Script API を「オン」** にしておく必要がある。
   https://script.google.com/home/usersettings （1回だけ）

⭐ 書き込む前に、**いまのコードを丸ごと控える**（`取り込みファイル/GASの控え/`）。
   人のスクリプトを触るので、元に戻せない状態にはしない。
"""

import json
import os
import re
import time

# 🔑 許してもらう範囲。これ以上は求めない（読み書きと公開だけ）
SCOPES = ["https://www.googleapis.com/auth/script.projects",
          "https://www.googleapis.com/auth/script.deployments"]

# 📄 アプリが書き込むファイルの名前。**専用ファイル**にすることで、
#    人が書いた関数と混ざらず、入れ直しも「このファイルだけ差し替え」で済む。
FILE_NAME = "EnkanAI_連携WebAPI"
MANIFEST = "appsscript"

BEGIN_MARK = "// ===== エンカンAI 連携ここから（アプリが自動で書き替えます）====="
END_MARK = "// ===== エンカンAI 連携ここまで ====="

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(APP_DIR, "取り込みファイル", "GASの控え")

USER_SETTINGS_URL = "https://script.google.com/home/usersettings"


# ==========================================
# 🗝 つなぐための鍵（OAuthクライアント）と、許可の保管
# ==========================================
def _secret(name: str, default: str = "") -> str:
    try:
        import streamlit as st
        v = st.secrets.get(name, "")
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "") or default


def _state_dir() -> str:
    """許可（トークン）を置く場所。⚠️ OneDriveの中には置かない。"""
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(root, "EnkanAI")
    os.makedirs(d, exist_ok=True)
    return d


def token_path() -> str:
    return os.path.join(_state_dir(), "gas_oauth_token.json")


_CLIENT_CACHE = {"raw": None}      # 画面を作り直すたびにSupabaseへ聞きにいかない


def _valid_client(raw: str):
    """クライアントJSONとして読めるかを見る。読めなければ None。"""
    try:
        cfg = json.loads(str(raw or ""))
    except Exception:
        return None
    if "installed" not in cfg and "web" not in cfg:
        return None
    return cfg


def client_config():
    """OAuthクライアント（アプリがGoogleに名乗るための鍵）。無ければ None。

    ⭐ **探す順**：secrets.toml → このPCのファイル → **Supabase**。

    ⚠️ この鍵を「PCごとに secrets.toml へ書き写す」形にしてはいけない。
       アプリを配るたび、全員に同じ書き替えをさせることになる（実際にそこで詰まった）。
       Google の「デスクトップアプリ」のクライアントは、**そもそも秘密として
       持てない種類**のもの（利用者の手元に必ず配られる）。だから、
       **Supabase に1回入れておけば、他のPCは何もしなくてよい**。
    """
    raw = _secret("GOOGLE_OAUTH_CLIENT_JSON", "")
    if not raw:
        p = os.path.join(APP_DIR, ".streamlit", "google_oauth_client.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                raw = ""
    if not raw:
        if _CLIENT_CACHE["raw"] is None:
            _CLIENT_CACHE["raw"] = _client_from_db()
        raw = _CLIENT_CACHE["raw"]
    return _valid_client(raw)


def _client_from_db() -> str:
    """Supabase に入れてある鍵を読む（鍵があれば復号する）。"""
    sb = _supabase()
    if not sb:
        return ""
    try:
        res = sb.table("merchants").select("*").eq("id", AUTH_ROW).execute()
        if not res.data:
            return ""
        cfg = res.data[0].get("config_json") or {}
    except Exception:
        return ""
    enc = str(cfg.get("client_enc", "") or "")
    if enc:
        f = _fernet()
        if f:
            try:
                return f.decrypt(enc.encode()).decode()
            except Exception:
                return ""
        return ""
    return str(cfg.get("client_plain", "") or "")


def save_client_json(text: str):
    """鍵を Supabase に入れる（＝**全PCで使えるようにする**）。

    ⭐ これをやっておけば、他の担当者のPCでは **secrets.toml を触らなくてよい**。
    ⚠️ 読める形は「デスクトップアプリ」のJSONだけ。おかしければここで止める。
    """
    text = str(text or "").strip()
    if not _valid_client(text):
        raise RuntimeError("これは OAuth クライアントのJSONではないようです。"
                           "`installed` か `web` で始まる中身を、丸ごと貼ってください。")
    sb = _supabase()
    if not sb:
        raise RuntimeError("Supabaseにつながらないので保存できません。")
    row = {}
    f = _fernet()
    if f:
        row["client_enc"] = f.encrypt(text.encode()).decode()
    else:
        # 🔑 暗号化の鍵が無いPCでも、他のPCが読めるように入れておく。
        #    （デスクトップアプリのクライアントは、そもそも秘密にできない種類のもの）
        row["client_plain"] = text
    try:
        res = sb.table("merchants").select("*").eq("id", AUTH_ROW).execute()
        cur = (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception:
        cur = {}
    cur.pop("client_enc", None)
    cur.pop("client_plain", None)
    cur.update(row)
    cur["client_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    sb.table("merchants").upsert({
        "id": AUTH_ROW, "name": "（GAS書き込みの許可）", "is_active": False,
        "connector_type": "settings", "config_json": cur}).execute()
    _CLIENT_CACHE["raw"] = text


def have_client() -> bool:
    return client_config() is not None


AUTH_ROW = "__gas_auth__"          # ロボット一覧には出さない予約行


def _fernet():
    key = _secret("ENKAN_SECRET_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def _supabase():
    try:
        from supabase import create_client
        url, key = _secret("SUPABASE_URL"), _secret("SUPABASE_KEY")
        if not (url and key):
            return None
        return create_client(url, key)
    except Exception:
        return None


def _mirror_save(text: str):
    """⭐ 許可を Supabase にも（暗号化して）残す。

    許可を取れるのは「ブラウザを開けるPC」だけ。ここに残しておけば、
    別のPCやクラウドのアプリからも同じ許可を使える。
    鍵（ENKAN_SECRET_KEY）が無いPCでは何もしない。
    """
    f, sb = _fernet(), _supabase()
    if not (f and sb):
        return
    try:
        # ⚠️ 同じ行に「鍵（client）」も入っている。丸ごと書くと消えるので、**足す**。
        res = sb.table("merchants").select("*").eq("id", AUTH_ROW).execute()
        cur = (res.data[0].get("config_json") or {}) if res.data else {}
        cur["token_enc"] = f.encrypt(text.encode()).decode()
        cur["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sb.table("merchants").upsert({
            "id": AUTH_ROW, "name": "（GAS書き込みの許可）", "is_active": False,
            "connector_type": "settings", "config_json": cur}).execute()
    except Exception:
        pass


def _mirror_load() -> str:
    f, sb = _fernet(), _supabase()
    if not (f and sb):
        return ""
    try:
        res = sb.table("merchants").select("*").eq("id", AUTH_ROW).execute()
        if not res.data:
            return ""
        enc = (res.data[0].get("config_json") or {}).get("token_enc", "")
        return f.decrypt(str(enc).encode()).decode() if enc else ""
    except Exception:
        return ""


def save_token_json(text: str):
    """許可（token.json の中身）を保存する。手で貼り付ける道もここを通る。"""
    text = str(text or "").strip()
    if not text:
        return
    json.loads(text)                     # 壊れていたらここで止める
    with open(token_path(), "w", encoding="utf-8") as f:
        f.write(text)
    _mirror_save(text)


def forget():
    """許可を消す（別のアカウントでつなぎ直したいとき）。

    ⚠️ 消すのは**許可だけ**。同じ行にある鍵（client）は残す
       （消すと、全PCで「鍵がありません」に戻ってしまう）。
    """
    try:
        os.remove(token_path())
    except Exception:
        pass
    sb = _supabase()
    if not sb:
        return
    try:
        res = sb.table("merchants").select("*").eq("id", AUTH_ROW).execute()
        cur = (res.data[0].get("config_json") or {}) if res.data else {}
        cur.pop("token_enc", None)
        sb.table("merchants").upsert({
            "id": AUTH_ROW, "name": "（GAS書き込みの許可）", "is_active": False,
            "connector_type": "settings", "config_json": cur}).execute()
    except Exception:
        pass


def load_creds():
    """保存してある許可を返す（期限切れなら更新する）。無ければ None。"""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    raw = ""
    if os.path.isfile(token_path()):
        try:
            with open(token_path(), encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            raw = ""
    if not raw:
        raw = _mirror_load()
        if raw:
            try:
                with open(token_path(), "w", encoding="utf-8") as f:
                    f.write(raw)
            except Exception:
                pass
    if not raw:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except Exception:
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_token_json(creds.to_json())
        except Exception:
            return None
    return creds if (creds and creds.valid) else None


def authorized() -> bool:
    try:
        return load_creds() is not None
    except Exception:
        return False


def authorize_local(timeout_sec: int = 180):
    """このPCのブラウザを開いて、1回だけ許可をもらう。

    ⚠️ クラウド（Streamlit Cloud）ではブラウザを開けない。
       その場合は、PCで許可を取ってから `token.json` を貼り付ける。
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    cfg = client_config()
    if not cfg:
        raise RuntimeError("つなぐための鍵（GOOGLE_OAUTH_CLIENT_JSON）が設定されていません")
    flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
    try:
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                      timeout_seconds=timeout_sec,
                                      authorization_prompt_message="",
                                      success_message="許可できました。この画面は閉じてください。")
    except TypeError:                     # 古い版には timeout_seconds が無い
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    save_token_json(creds.to_json())
    return creds


# ==========================================
# 🔗 スクリプトのURL → スクリプトID
# ==========================================
def is_sheet_url(url: str) -> bool:
    """スプレッドシート（やドキュメント）のURLか。⚠️ これはスクリプトではない。"""
    s = str(url or "").strip().lower()
    return ("docs.google.com" in s) or ("/spreadsheets/" in s)


def script_id_of(url_or_id: str) -> str:
    """Apps Script の画面のURLから、スクリプトIDだけ取り出す。

    受け付ける形：
      https://script.google.com/home/projects/<ID>/edit
      https://script.google.com/u/0/home/projects/<ID>/edit
      https://script.google.com/macros/d/<ID>/edit
      <ID> そのまま
    ⚠️ `.../macros/s/AKfy.../exec`（公開URL）は**別もの**なので受け付けない。
    ⚠️ **スプレッドシートのURL（`docs.google.com/spreadsheets/d/…`）も受け付けない。**
       `/d/` の形が同じなので、以前はスプシのIDをそのまま渡してしまい、
       Googleから `Invalid script key` が返っていた（実際に起きた）。
       スプシのIDからスクリプトIDは分からないので、**人に貼り直してもらう**しかない。
    """
    s = str(url_or_id or "").strip()
    if not s:
        return ""
    if "/macros/s/" in s or is_sheet_url(s):
        return ""
    m = re.search(r"/(?:projects|d)/([A-Za-z0-9_\-]{20,})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", s):
        return s
    return ""


# ==========================================
# 🧹 前に「手で貼った」古い版を見つけて外す
# ==========================================
def _strip_old_block(src: str):
    """人が貼った古い連携コードを切り取る。戻り値：(残り, 切り取った分)

    貼り方の案内が「いまのコードのいちばん下に貼る」だったので、
    **見出しから終わりまで**を切る。手前に説明のコメント枠があれば、それごと。
    """
    idx = None
    for mk in ("エンカンAI 連携の入口", "// 🔑 合言葉", "const API_TOKEN"):
        i = src.find(mk)
        if i >= 0:
            idx = i if idx is None else min(idx, i)
    if idx is None:
        return src, ""
    head = src[:idx]
    j = head.rfind("/**")
    if j >= 0 and "*/" not in src[j:idx]:
        idx = j                            # 直前の説明コメントごと
    return src[:idx].rstrip() + "\n", src[idx:]


def _backup(script_id: str, content: dict) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR,
                        f"{script_id[:12]}_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    return path


def _service(creds):
    from googleapiclient.discovery import build
    return build("script", "v1", credentials=creds, cache_discovery=False)


def _friendly(e) -> str:
    """Googleからのエラーを、次にやることが分かる日本語にする。"""
    msg = str(e)
    if "not enabled the Apps Script API" in msg or "user has not enabled" in msg.lower():
        return ("このGoogleアカウントで **Apps Script API がオフ**になっています。"
                f"{USER_SETTINGS_URL} を開いて、スイッチを**オン**にしてから、"
                "もう一度押してください（1回だけの作業です）。")
    if "SERVICE_DISABLED" in msg or "has not been used in project" in msg:
        return ("つなぐための鍵を作った Google Cloud のプロジェクトで、"
                "**Apps Script API を有効**にしてください"
                "（下のエラー文の中にあるURLを開くと、その場で有効にできます）：\n\n"
                + msg[:400])
    if "Invalid script key" in msg:
        # ⚠️ ほぼ「スプレッドシートのURLを貼った」。IDの形が似ているので気づきにくい。
        return ("そのIDはスクリプトのものではありません（**スプレッドシートのURLを貼ると"
                "こうなります**）。そのスプシで **拡張機能 → Apps Script** を開き、"
                "そのときのアドレス（`https://script.google.com/…/projects/…/edit`）を"
                "貼り直してください。\n\n"
                "※ このエラーは**読もうとした時点**で出ています。"
                "スクリプトは**まだ何も書き替えていません**。")
    if "Requested entity was not found" in msg or "404" in msg:
        return ("そのスクリプトが見つかりません。**スクリプトのURL**が正しいか、"
                "許可したGoogleアカウントに**そのスプシを見る権限があるか**を確かめてください。")
    if "403" in msg or "PERMISSION_DENIED" in msg:
        return ("許可したGoogleアカウントに、このスクリプトを直す権限がありません。"
                "スプシの持ち主のアカウントでつなぎ直してください。\n\n" + msg[:300])
    return msg[:500]


def _script_token(files) -> str:
    """スクリプトに入っている合言葉（エンカンAIの専用ファイルの宣言）。無ければ空。"""
    for f in files or []:
        if f.get("name") != FILE_NAME:
            continue
        m = re.search(r"(?m)^const API_TOKEN\s*=\s*'([^']+)'\s*;", str(f.get("source", "") or ""))
        if m and "合言葉" not in m.group(1):
            return m.group(1)
    return ""


def install(script_url: str, api_token: str, deployment_id: str = "",
            gas_file: str = "エンカンAI_連携WebAPI.gs", log=None, regen: bool = False) -> dict:
    """スクリプトに連携コードを書き込み、ウェブアプリとして公開する。

    ⭐ **合言葉はスクリプトに1つ**。同じスプシを使うジョブ／パターンが何個あっても、
       スクリプトに入っている合言葉をそのまま使う（`regen` のときだけ作り直す）。
       ⚠️ 以前はジョブごとの合言葉で書き替えていたため、あとから別のジョブで押すと
       **先に入れたジョブが「合言葉が違います」で動かなくなった**（実際に起きた）。
    ⚠️ 渡された公開先（deployment_id）が**このスクリプトのものでなければ使わない**。
       新しいジョブに別のスプシの公開先が紛れ込み、別のスクリプトのURLを保存していた（実際に起きた）。

    戻り値：{"url", "deployment_id", "version", "backup", "removed", "script_id", "token"}
    """
    def _say(t):
        if log:
            log(t)

    sid = script_id_of(script_url)
    if not sid:
        raise RuntimeError(
            "スクリプトのURLが読み取れません。スプシの **拡張機能 → Apps Script** を開いて、"
            "そのときのアドレス（`https://script.google.com/…/projects/…/edit`）を貼ってください。"
            "`/macros/s/…/exec`（公開用のURL）ではありません。")
    creds = load_creds()
    if not creds:
        raise RuntimeError("まだGoogleにつないでいません。先に「🔐 Googleにつなぐ」を押してください。")

    import sms_runner
    svc = _service(creds)
    try:
        _say("いまのコードを読んでいます…")
        content = svc.projects().getContent(scriptId=sid).execute()
    except Exception as e:
        raise RuntimeError(_friendly(e))
    files = content.get("files", []) or []
    backup = _backup(sid, content)
    _say(f"控えを取りました：{backup}")

    _have = _script_token(files)
    token = str(api_token or "").strip()
    if _have and not regen:
        if token and token != _have:
            _say("このスクリプトに入っている合言葉に合わせます（同じスプシのほかの設定を止めないため）。")
        token = _have
    if not token:
        import secrets as _secrets
        token = _secrets.token_urlsafe(24)
    source = sms_runner.gas_template(gas_file, token)
    if not source:
        raise RuntimeError(f"配るコード（gas/{gas_file}）が読めませんでした。")
    source = BEGIN_MARK + "\n" + source.rstrip() + "\n" + END_MARK + "\n"

    # 🧹 前に手で貼った版が残っていると、同じ名前が2回出てスクリプト全体が動かなくなる
    removed, conflicts = [], []
    for f in files:
        if f.get("name") == FILE_NAME or f.get("type") != "SERVER_JS":
            continue
        s = str(f.get("source", "") or "")
        if ("const API_TOKEN" not in s) and not re.search(r"function\s+doGet\s*\(", s):
            continue
        if "エンカンAI" in s:
            new, cut = _strip_old_block(s)
            if cut:
                f["source"] = new
                removed.append({"ファイル": f.get("name", ""), "外した文字数": len(cut)})
        else:
            conflicts.append(str(f.get("name", "")))
    if conflicts:
        raise RuntimeError(
            "このスクリプトには、エンカンAIのものではない `doGet`／`API_TOKEN` が既にあります"
            f"（{', '.join(conflicts)}）。同じ名前が2つあると**スクリプト全体が動かなくなる**ので、"
            "書き込みを中止しました。どちらを残すか決めてから、もう一度押してください。")

    for f in files:
        if f.get("name") == FILE_NAME:
            f["source"] = source
            f["type"] = "SERVER_JS"
            break
    else:
        files.append({"name": FILE_NAME, "type": "SERVER_JS", "source": source})

    # 📄 公開のしかた（自分として実行／全員がアクセス）は、この設定ファイルで決まる
    for f in files:
        if f.get("name") == MANIFEST:
            try:
                man = json.loads(f.get("source") or "{}")
            except Exception:
                man = {}
            break
    else:
        man = {}
        f = {"name": MANIFEST, "type": "JSON", "source": "{}"}
        files.append(f)
    man.setdefault("timeZone", "Asia/Tokyo")
    man.setdefault("exceptionLogging", "STACKDRIVER")
    man.setdefault("runtimeVersion", "V8")
    man["webapp"] = {"access": "ANYONE_ANONYMOUS", "executeAs": "USER_DEPLOYING"}
    f["source"] = json.dumps(man, ensure_ascii=False, indent=2)

    did = str(deployment_id or "").strip()
    try:
        _say("コードを書き込んでいます…")
        svc.projects().updateContent(scriptId=sid, body={"files": files}).execute()
        _say("新しいバージョンを作っています…")
        ver = svc.projects().versions().create(
            scriptId=sid,
            body={"description": "エンカンAI 連携 " + time.strftime("%Y-%m-%d %H:%M")}).execute()
        vnum = int(ver.get("versionNumber", 0) or 0)
        conf = {"versionNumber": vnum, "manifestFileName": MANIFEST,
                "description": "エンカンAI 連携"}
        if did and did not in _deployment_ids(svc, sid):
            _say("前の設定の公開先は、このスクリプトのものではなかったので使いません。")
            did = ""
        did = did or _find_deployment(svc, sid)
        if did:
            _say("公開しているものを、新しいバージョンに差し替えています…")
            dep = svc.projects().deployments().update(
                scriptId=sid, deploymentId=did,
                body={"deploymentConfig": conf}).execute()
        else:
            _say("ウェブアプリとして公開しています…")
            dep = svc.projects().deployments().create(scriptId=sid, body=conf).execute()
    except Exception as e:
        raise RuntimeError(_friendly(e))

    did = str(dep.get("deploymentId", "") or did or "")
    url = ""
    for ep in dep.get("entryPoints", []) or []:
        u = ((ep.get("webApp") or {}).get("url") or "").strip()
        if u:
            url = u
            break
    if not url and did:
        url = f"https://script.google.com/macros/s/{did}/exec"
    return {"url": url, "deployment_id": did, "version": vnum,
            "backup": backup, "removed": removed, "script_id": sid, "token": token}


def _deployment_ids(svc, sid: str) -> set:
    try:
        res = svc.projects().deployments().list(scriptId=sid, pageSize=50).execute()
    except Exception:
        return set()
    return {str(d.get("deploymentId", "")) for d in res.get("deployments", []) or []}


def _find_deployment(svc, sid: str) -> str:
    """すでにウェブアプリとして公開してあるものを探す（URLを変えないため）。"""
    try:
        res = svc.projects().deployments().list(scriptId=sid, pageSize=50).execute()
    except Exception:
        return ""
    for d in res.get("deployments", []) or []:
        conf = d.get("deploymentConfig") or {}
        if not conf.get("versionNumber"):
            continue                      # 「HEAD」（テスト用）は触らない
        for ep in d.get("entryPoints", []) or []:
            if ep.get("entryPointType") == "WEB_APP" or "webApp" in ep:
                return str(d.get("deploymentId", "") or "")
    return ""


def restore(script_url: str, backup_file: str) -> str:
    """控えから元に戻す（書き込みで壊れたときの逃げ道）。"""
    sid = script_id_of(script_url)
    creds = load_creds()
    if not (sid and creds):
        raise RuntimeError("スクリプトのURLか、Googleへの許可がありません。")
    with open(backup_file, encoding="utf-8") as f:
        content = json.load(f)
    try:
        _service(creds).projects().updateContent(
            scriptId=sid, body={"files": content.get("files", [])}).execute()
    except Exception as e:
        raise RuntimeError(_friendly(e))
    return "元に戻しました（公開しているものは、もう一度入れ直すと新しくなります）。"


# ==========================================
# 🖥 設定画面（SMS送信・データローダーの両方から呼ぶ）
# ==========================================
# ⚠️ 画面を2つ書くと、片方だけ直して食い違う。**ここ1か所**に置いて、
#    どのページからも同じものを出す。
SETUP_HELP = """**アプリ全体で1回だけの用意（管理者・だいたい5分）**

1. Google Cloud で **OAuthクライアントID（デスクトップアプリ）** を作り、
   JSONをダウンロードする。
2. そのプロジェクトで **Apps Script API** を有効にする。
3. スプシの持ち主のアカウントで {url} を開き、
   **Apps Script API** のスイッチを**オン**にする。
4. ダウンロードしたJSONを、**下の欄に貼って保存**する。

この4つは**アプリに1回**きりです。**スプレッドシートごとにも、
パターンごとにも作りません**（増えても何もしません）。
以後、スプシごとにやるのは「スクリプトのURLを貼って押すだけ」です。
"""


def render(prefix: str, values: dict, token_key: str = "", url_key: str = "",
           on_save=None) -> dict:
    """GASの設定欄。**アプリが書き込んで公開する道だけ**を出す。

    ⚠️ 手で貼る道は残していない。2通りあると、どちらをしたか分からなくなり、
       「合言葉が食い違う」「古い版が残る」という事故がそのまま残るため。

    values … {"gas_script_url", "gas_url", "gas_token", "gas_deployment_id"}
    token_key / url_key … 呼び出し側に入力欄がある場合の名札（いまは未使用でよい）
    on_save … 入れ終わったあと、その場で保存したいときの呼び出し先（任意）
    戻り値 … 画面での操作を反映した values（保存は呼び出し側）
    """
    import streamlit as st
    out = dict(values or {})
    out["auto"] = False
    # ⚠️ **保存前でも、入れ直した結果が正**。ここを見ないと、入れた直後に画面が
    #    作り直された時点で、まだ保存していない新しいURL・合言葉が消えてしまう。
    if token_key and str(st.session_state.get(token_key, "") or "").strip():
        out["gas_token"] = str(st.session_state[token_key]).strip()
    # ⚠️ **出来上がった欄（widget）の中身は、あとから書き替えられない**（Streamlitの決まり）。
    #    入れ直しが終わったときは印だけ立てて、**次に画面を作るときに**外す。
    #    （直に書き替えて「cannot be modified after the widget is instantiated」で止まった）
    if st.session_state.pop(prefix + "_gasregen_off", False):
        st.session_state[prefix + "_gasregen"] = False
    _done = st.session_state.get(prefix + "_gasinst") or {}
    # ⚠️ 覚えている結果が、いま貼ってあるスクリプトのものでなければ使わない。
    #    「＋ ジョブを追加」は毎回同じ名札なので、前に作ったジョブの公開先が新しいジョブに入り、
    #    **別のスプシのURLを保存していた**（トス表作成のジョブで実際に起きた）。
    _now_sid = script_id_of(st.session_state.get(prefix + "_gasscript")
                            or out.get("gas_script_url", "") or "")
    if _done and _done.get("script_id") and _done.get("script_id") != _now_sid:
        st.session_state.pop(prefix + "_gasinst", None)
        _done = {}
    if _done.get("url"):
        out["gas_url"] = _done["url"]
        out["gas_deployment_id"] = _done.get("deployment_id", "")
        if _done.get("token"):
            out["gas_token"] = _done["token"]

    st.markdown("##### 🤖 GASはアプリが入れます（コピペなし）")

    if not have_client():
        st.error("⚠️ **このアプリにはまだ「GASを書き込む鍵」がありません。** "
                 "下の用意を済ませてください（管理者の作業です）。")
        st.info("🔑 この鍵は、**このアプリが Google に名乗るための身分証**です。"
                "**アプリ全体で1つ**あればよく、"
                "**スプレッドシートごとにも、パターンごとにも作りません**。"
                "⭐ ここで1回入れれば **Supabaseに入る**ので、"
                "**他の担当者のPCでは何もしなくてよくなります**"
                "（`secrets.toml` を全員に書き替えてもらう必要はありません）。")
        st.markdown(SETUP_HELP.format(url=USER_SETTINGS_URL))
        _cj = st.text_area("ダウンロードしたJSONの中身を、まるごとここに貼る",
                           key=prefix + "_gasclient", height=120,
                           placeholder='{"installed":{"client_id":"...","client_secret":"..."}}')
        if st.button("💾 この鍵を全PCで使えるようにする", key=prefix + "_gasclientsave",
                     type="primary"):
            try:
                save_client_json(_cj)
                st.success("✅ 入れました。**このアプリを使う全員のPCで使えます**。")
                st.rerun()
            except Exception as e:
                st.error(f"❌ {e}")
        if str(out.get("gas_url", "") or "").strip():
            st.info("すでに登録されているURLはそのまま使えます："
                    + str(out["gas_url"]))
        return out

    _res = _done
    if _res:
        st.success("✅ 書き込んで公開しました。**URLも合言葉も、アプリが入れてあります**。")
        st.caption(f"バージョン {_res.get('version','')}／控え：`{_res.get('backup','')}`")
        if _res.get("removed"):
            st.warning("🧹 前に手で貼ってあった古い版を外しました："
                       + "、".join(f"{r['ファイル']}（{r['外した文字数']}文字）"
                                   for r in _res["removed"])
                       + "。おかしければ、上の控えから戻せます。")
        _v = _res.get("verify")
        if _v and _v[0]:
            st.success("✅ " + str(_v[1]))
        elif _v:
            st.error("入れましたが、まだ返事がありません：" + str(_v[1]))
            st.caption("スクリプトを**一度も承認していない**と、こうなります。"
                       "Apps Script を開いて関数を1回実行し、アクセスを承認してから、"
                       "もう一度この画面で確かめてください。")

    if not authorized():
        st.warning("まだGoogleにつないでいません。**スプシの持ち主のアカウント**で1回だけ許可してください。")
    _a1, _a2 = st.columns([1, 2])
    with _a1:
        if st.button("🔐 Googleにつなぐ" if not authorized() else "🔁 つなぎ直す",
                     key=prefix + "_gasauth", use_container_width=True):
            try:
                with st.spinner("ブラウザが開きます。許可の画面で「許可」を押してください…"):
                    authorize_local()
                st.success("つながりました。")
                st.rerun()
            except Exception as e:
                st.error(f"つなげませんでした：{e}")
                st.caption("ブラウザが開かなかったときは、もう一度押してみてください。")
    with _a2:
        st.caption("許可は1回だけです（このPCと、鍵があれば他のPCでも使い回します）。"
                   f"うまくいかないときは {USER_SETTINGS_URL} の "
                   "**Apps Script API** がオンかを確かめてください。")

    out["gas_script_url"] = st.text_input(
        "スクリプトのURL（スプシ → 拡張機能 → Apps Script を開いたときのアドレス）",
        value=str(out.get("gas_script_url", "") or ""), key=prefix + "_gasscript",
        placeholder="https://script.google.com/u/0/home/projects/xxxxxxxx/edit")
    _sid = script_id_of(out["gas_script_url"])
    if out["gas_script_url"].strip() and not _sid:
        if is_sheet_url(out["gas_script_url"]):
            # ⚠️ いちばん多い間違い。スプシのURLとスクリプトのURLは別もの。
            st.error("これは**スプレッドシートのURL**です（スクリプトのURLではありません）。"
                     "そのスプシで **拡張機能 → Apps Script** を開くと、"
                     "アドレスが `https://script.google.com/…/projects/…/edit` に変わります。"
                     "**そちらのアドレス**を貼ってください。")
        else:
            st.error("このURLからはスクリプトが分かりません。"
                     "**`/exec` で終わる公開用のURLではなく**、Apps Script を開いたときの"
                     "アドレスを貼ってください。")

    _b1, _b2 = st.columns([1, 2])
    with _b1:
        _go = st.button("🚀 GASを入れて公開する", key=prefix + "_gasinstall",
                        type="primary", use_container_width=True, disabled=not _sid)
    with _b2:
        st.caption("押すと、**いまのコードの控えを取ってから**、連携コードを専用ファイルとして"
                   "書き込み、新しいバージョンで公開します。"
                   "出てきた `.../exec` のURLと合言葉は、そのまま設定に入ります。")

    # 🔎 いま何が入っているか（読むだけ。手で書き替えさせない＝食い違いを作らない）
    with st.expander("🔎 いまの設定を見る／合言葉を作り直す"):
        st.caption("この2つは**アプリが入れます**。手で書き替える必要はありません。")
        st.text("呼び出し先（/exec）：" + (str(out.get("gas_url", "") or "") or "（まだありません）"))
        st.text("合言葉：" + (str(out.get("gas_token", "") or "") or "（まだありません）"))
        _regen = st.checkbox("合言葉を作り直して入れ直す",
                             key=prefix + "_gasregen",
                             help="URLを知られてしまったときなど。次に「🚀 GASを入れて公開する」を"
                                  "押したときに、新しい合言葉で書き替えます")

    if _go:
        _tok = str(out.get("gas_token", "") or "").strip()
        if _regen:
            import secrets as _secrets
            _tok = _secrets.token_urlsafe(24)          # 🎲 合言葉もアプリが作る
        # ⚠️ 前の設定の結果（別のジョブ・別のスクリプト）を持ち越さない
        st.session_state.pop(prefix + "_gasinst", None)
        _box = st.empty()
        # ⚠️ **書き込みと、その後始末は分けて包む。**
        #    後始末（保存・確認）でつまずいたとき、まとめて ❌ にすると
        #    「書き込めなかった」と読めてしまい、**もう一度押させてしまう**（実際に起きた）。
        r = None
        try:
            with st.spinner("スクリプトに書き込んでいます…"):
                r = install(out["gas_script_url"], _tok,
                            str(out.get("gas_deployment_id", "") or ""),
                            log=lambda t: _box.caption(t), regen=bool(_regen))
                _tok = r.get("token") or _tok
        except Exception as e:
            _box.empty()
            st.error(f"❌ {e}")
        if r:
            r["token"] = _tok
            out["gas_url"] = r["url"]
            out["gas_token"] = _tok
            out["gas_deployment_id"] = r["deployment_id"]
            out["auto"] = True
            if token_key:
                st.session_state[token_key] = _tok
            if url_key:
                st.session_state[url_key] = r["url"]
            st.session_state[prefix + "_gasinst"] = r
            st.session_state[prefix + "_gasregen_off"] = True
            _box.empty()
            try:
                if on_save:
                    on_save(out)
                # 🩺 入れたら、その場でつながるか確かめる（言いっぱなしにしない）
                import sms_runner
                ok, msg = sms_runner.check_gas_csv(r["url"], _tok)
                # ⚠️ このあと画面を作り直すので、**その場に出したものは消える**。
                #    結果は持たせて、作り直したあとに出す。
                r["verify"] = [bool(ok), str(msg)]
                st.session_state[prefix + "_gasinst"] = r
            except Exception as e:
                st.warning("⚠️ **書き込みと公開は終わっています**（もう一度押す必要はありません）。"
                           f"そのあとの処理でつまずきました：{e}")
            st.rerun()
    return out
