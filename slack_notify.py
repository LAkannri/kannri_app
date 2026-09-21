"""
🔔 Slack 通知の送り先（Incoming Webhook の URL）を、全PCで共有する

⭐ **URLはPCごとに `secrets.toml` へ書かせない。** 「⚙️ その他設定」で1回貼れば、
   `ENKAN_SECRET_KEY` で暗号化して Supabase の予約行 `__slack__` に入れ、**全PCがそこから読む**。
   PCごとに書き写す形だと、担当者のPCに少しずつ入れることになり、入っていないPCだけ通知が来ない
   （GASの鍵 `GOOGLE_OAUTH_CLIENT_JSON` と同じ考え方）。

探す順：環境変数（GitHub Actions）→ このPCの secrets.toml → Supabase（暗号化）。
⚠️ 暗号化を解くには、そのPCの `ENKAN_SECRET_KEY` が保存したPCと同じである必要がある
   （パスワードの保存と同じ鍵。違うPCでは「読めません」になる）。
⚠️ Streamlit を import しない（robot.py・scheduler.py からも使うため）。
"""
import json
import os
import time
import urllib.request

try:
    import tomllib
except ImportError:          # Python 3.10 以前
    import tomli as tomllib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROW = "__slack__"
KEY = "SLACK_WEBHOOK_URL"
_CACHE = {}


def _toml() -> dict:
    try:
        with open(os.path.join(BASE_DIR, ".streamlit", "secrets.toml"), "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def _secret(secrets: dict, name: str) -> str:
    return str(os.environ.get(name) or (secrets or {}).get(name, "") or "").strip()


def _fernet(secrets: dict):
    key = _secret(secrets, "ENKAN_SECRET_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def _sb(secrets: dict, sb=None):
    if sb is not None:
        return sb
    try:
        from supabase import create_client
        return create_client(_secret(secrets, "SUPABASE_URL"), _secret(secrets, "SUPABASE_KEY"))
    except Exception:
        return None


def _row(sb) -> dict:
    res = sb.table("merchants").select("config_json").eq("id", ROW).execute()
    return (res.data[0].get("config_json") or {}) if res.data else {}


def looks_like_webhook(url: str) -> bool:
    return str(url or "").strip().startswith("https://hooks.slack.com/")


def webhook_url(secrets: dict = None, sb=None, fresh: bool = False):
    """送り先のURLと、どこから読んだか。戻り値：(URL, "環境変数"|"このPC"|"共有"|"", 読めなかった理由)"""
    s = secrets if secrets is not None else _toml()
    if os.environ.get(KEY):
        return os.environ[KEY].strip(), "環境変数", ""
    if str(s.get(KEY, "") or "").strip():
        return str(s[KEY]).strip(), "このPC", ""
    if not fresh and "shared" in _CACHE and time.time() - _CACHE["at"] < 300:
        return _CACHE["shared"]
    out = ("", "", "")
    client = _sb(s, sb)
    if client is not None:
        try:
            enc = str(_row(client).get("url_enc", "") or "")
            if enc:
                f = _fernet(s)
                if not f:
                    out = ("", "", "このPCに ENKAN_SECRET_KEY が無いので、共有の送り先を読めません")
                else:
                    try:
                        out = (f.decrypt(enc.encode()).decode(), "共有", "")
                    except Exception:
                        out = ("", "", "このPCの ENKAN_SECRET_KEY が、保存したPCと違うので読めません")
        except Exception as e:
            out = ("", "", f"Supabaseを読めませんでした: {str(e)[:120]}")
    _CACHE.update({"shared": out, "at": time.time()})
    return out


def shared_info(secrets: dict = None, sb=None) -> dict:
    """共有の送り先が保存されているか（中身は出さない）。{"saved": bool, "saved_at", "saved_by"}"""
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return {"saved": False}
    try:
        r = _row(client)
    except Exception:
        return {"saved": False}
    return {"saved": bool(r.get("url_enc")), "saved_at": r.get("saved_at", ""), "saved_by": r.get("saved_by", "")}


def save_shared(url: str, who: str = "", secrets: dict = None, sb=None):
    """全PCで使う送り先を保存する。戻り値：(うまくいったか, 言葉)"""
    s = secrets if secrets is not None else _toml()
    url = str(url or "").strip()
    if not looks_like_webhook(url):
        return False, "Slack の Incoming Webhook の URL（https://hooks.slack.com/ で始まるもの）を貼ってください。"
    f = _fernet(s)
    if not f:
        return False, "このPCに ENKAN_SECRET_KEY が無いので、暗号化して保存できません。"
    client = _sb(s, sb)
    if client is None:
        return False, "Supabase につながりません。"
    try:
        cur = _row(client)          # ⚠️ 読み直して足す（ほかの項目を消さない）
        cur.update({"url_enc": f.encrypt(url.encode()).decode(),
                    "saved_at": time.strftime("%Y/%m/%d %H:%M"), "saved_by": who})
        client.table("merchants").upsert({
            "id": ROW, "name": "（Slack通知の送り先）", "is_active": False,
            "connector_type": "settings", "config_json": cur}).execute()
    except Exception as e:
        return False, f"保存できませんでした: {str(e)[:160]}"
    _CACHE.clear()
    return True, "保存しました。どのPCからも、この送り先に通知します。"


def clear_shared(secrets: dict = None, sb=None):
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return False, "Supabase につながりません。"
    try:
        cur = _row(client)
        for k in ("url_enc", "saved_at", "saved_by"):
            cur.pop(k, None)
        client.table("merchants").upsert({
            "id": ROW, "name": "（Slack通知の送り先）", "is_active": False,
            "connector_type": "settings", "config_json": cur}).execute()
    except Exception as e:
        return False, f"消せませんでした: {str(e)[:160]}"
    _CACHE.clear()
    return True, "消しました。"


# ==========================================
# 📣 ほかの送り先（名前つき。時間指定の予定ごとに「このグループにも送る」を選ぶ）
#    ⭐ いつもの送り先（上）とは別に持つ。いつもの送り先への通知の動きは何も変えない。
#    行 `__slack__` の `extra`＝{名前: {"url_enc", "saved_at", "saved_by"}}
# ==========================================
def _put_row(client, cur: dict):
    client.table("merchants").upsert({
        "id": ROW, "name": "（Slack通知の送り先）", "is_active": False,
        "connector_type": "settings", "config_json": cur}).execute()


def extra_info(secrets: dict = None, sb=None) -> dict:
    """保存してあるほかの送り先（中身は出さない）。{名前: {"saved_at", "saved_by"}}"""
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return {}
    try:
        ex = _row(client).get("extra") or {}
    except Exception:
        return {}
    return {n: {"saved_at": v.get("saved_at", ""), "saved_by": v.get("saved_by", "")}
            for n, v in ex.items() if isinstance(v, dict) and v.get("url_enc")}


def extra_url(name: str, secrets: dict = None, sb=None):
    """名前の送り先のURL。戻り値：(URL, 読めなかった理由)"""
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return "", "Supabase につながりません"
    try:
        enc = str(((_row(client).get("extra") or {}).get(name) or {}).get("url_enc", "") or "")
    except Exception as e:
        return "", f"Supabaseを読めませんでした: {str(e)[:120]}"
    if not enc:
        return "", f"送り先「{name}」は保存されていません（消された可能性があります）"
    f = _fernet(s)
    if not f:
        return "", "このPCに ENKAN_SECRET_KEY が無いので、送り先を読めません"
    try:
        return f.decrypt(enc.encode()).decode(), ""
    except Exception:
        return "", "このPCの ENKAN_SECRET_KEY が、保存したPCと違うので読めません"


def save_extra(name: str, url: str, who: str = "", secrets: dict = None, sb=None):
    """ほかの送り先を名前つきで保存する（同じ名前なら差し替え）。戻り値：(うまくいったか, 言葉)"""
    s = secrets if secrets is not None else _toml()
    name, url = str(name or "").strip(), str(url or "").strip()
    if not name:
        return False, "送り先の名前を入れてください（例：TSグループ）。"
    if not looks_like_webhook(url):
        return False, "Slack の Incoming Webhook の URL（https://hooks.slack.com/ で始まるもの）を貼ってください。"
    f = _fernet(s)
    if not f:
        return False, "このPCに ENKAN_SECRET_KEY が無いので、暗号化して保存できません。"
    client = _sb(s, sb)
    if client is None:
        return False, "Supabase につながりません。"
    try:
        cur = _row(client)          # ⚠️ 読み直して足す（いつもの送り先・ほかの送り先を消さない）
        cur.setdefault("extra", {})[name] = {"url_enc": f.encrypt(url.encode()).decode(),
                                             "saved_at": time.strftime("%Y/%m/%d %H:%M"), "saved_by": who}
        _put_row(client, cur)
    except Exception as e:
        return False, f"保存できませんでした: {str(e)[:160]}"
    return True, f"「{name}」を保存しました。時間指定の予定で、この送り先を選べます。"


def clear_extra(name: str, secrets: dict = None, sb=None):
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return False, "Supabase につながりません。"
    try:
        cur = _row(client)
        (cur.get("extra") or {}).pop(name, None)
        _put_row(client, cur)
    except Exception as e:
        return False, f"消せませんでした: {str(e)[:160]}"
    return True, f"「{name}」を消しました。"


def post(url: str, text: str, timeout: int = 20):
    """1通送る。戻り値：(送れたか, 言葉)"""
    try:
        req = urllib.request.Request(url, data=json.dumps({"text": str(text)}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=timeout).read()
        return True, ""
    except Exception as e:
        return False, str(e)[:160]


def send(text: str, secrets: dict = None, sb=None):
    """送り先を探して1通送る。送り先が無ければ何もしない。戻り値：(送れたか, 言葉)"""
    url, _src, why = webhook_url(secrets, sb)
    if not url:
        return False, why or "送り先がありません"
    return post(url, text)
