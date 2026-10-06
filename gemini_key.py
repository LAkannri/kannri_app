"""
🤖 Gemini の APIキーを、全PCで共有する

⭐ **キーを変えるたびに secrets.toml を配り直さない。** 「⚙️ その他設定」で1回貼れば、
   `ENKAN_SECRET_KEY` で暗号化して Supabase の予約行 `__gemini__` に入れ、**全PCがそこから読む**
   （Slack通知の送り先 `slack_notify` と同じ考え方）。

探す順：環境変数 → **共有（Supabase）** → このPCの secrets.toml。
⚠️ Slack と違い、**共有を secrets.toml より先に見る**。secrets.toml はもう配ってあり、
   古いキーが入ったままのPCがある。そちらが勝つと、共有で差し替えた意味がなくなる。
⚠️ 共有が読めない（鍵が無い・違う）PCは、secrets.toml のキーで動く（止めない）。
⚠️ Streamlit を import しない（画面以外からも使えるように）。
"""
import os
import time

from slack_notify import _toml, _secret, _fernet, _sb

ROW = "__gemini__"
KEY = "GEMINI_API_KEY"
_CACHE = {}


def _row(sb) -> dict:
    res = sb.table("merchants").select("config_json").eq("id", ROW).execute()
    return (res.data[0].get("config_json") or {}) if res.data else {}


def _shared(s, sb=None, fresh=False):
    """共有のキー。戻り値：(キー, 読めなかった理由)"""
    if not fresh and "v" in _CACHE and time.time() - _CACHE["at"] < 300:
        return _CACHE["v"]
    out = ("", "")
    client = _sb(s, sb)
    if client is not None:
        try:
            enc = str(_row(client).get("key_enc", "") or "")
            if enc:
                f = _fernet(s)
                if not f:
                    out = ("", "このPCに ENKAN_SECRET_KEY が無いので、共有のキーを読めません")
                else:
                    try:
                        out = (f.decrypt(enc.encode()).decode(), "")
                    except Exception:
                        out = ("", "このPCの ENKAN_SECRET_KEY が、保存したPCと違うので読めません")
        except Exception as e:
            out = ("", f"Supabaseを読めませんでした: {str(e)[:120]}")
    _CACHE.update({"v": out, "at": time.time()})
    return out


def source(secrets=None, sb=None, fresh=False):
    """戻り値：(キー, "環境変数"|"共有"|"このPC"|"", 共有を読めなかった理由)"""
    s = secrets if secrets is not None else _toml()
    if os.environ.get(KEY):
        return os.environ[KEY].strip(), "環境変数", ""
    key, why = _shared(s, sb, fresh)
    if key:
        return key, "共有", ""
    local = _secret(s, KEY)
    return local, ("このPC" if local else ""), why


def api_key(secrets=None, sb=None) -> str:
    """使うキー（無ければ空）。st.secrets をそのまま渡せる。"""
    return source(secrets, sb)[0]


def shared_info(secrets=None, sb=None) -> dict:
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return {"saved": False}
    try:
        r = _row(client)
    except Exception:
        return {"saved": False}
    return {"saved": bool(r.get("key_enc")), "saved_at": r.get("saved_at", ""), "saved_by": r.get("saved_by", "")}


def _write(client, cur):
    client.table("merchants").upsert({
        "id": ROW, "name": "（Gemini APIキー）", "is_active": False,
        "connector_type": "settings", "config_json": cur}).execute()


def save_shared(key: str, who: str = "", secrets=None, sb=None):
    s = secrets if secrets is not None else _toml()
    key = str(key or "").strip()
    # ⚠️ Googleはキーの形を変えた（古い `AIza…` と、新しい `AQ.…`）。
    #   形で弾くと、正しい新しいキーを貼っても保存できない（2026-10-06に実際に起きた）。
    if not key.startswith(("AIza", "AQ.")):
        return False, ("Gemini の APIキー（`AIza…` または `AQ.…` で始まるもの）を貼ってください。"
                       "Google AI Studio の「Get API key」で作ったキーです。")
    f = _fernet(s)
    if not f:
        return False, "このPCに ENKAN_SECRET_KEY が無いので、暗号化して保存できません。"
    client = _sb(s, sb)
    if client is None:
        return False, "Supabase につながりません。"
    try:
        cur = _row(client)          # ⚠️ 読み直して足す
        cur.update({"key_enc": f.encrypt(key.encode()).decode(),
                    "saved_at": time.strftime("%Y/%m/%d %H:%M"), "saved_by": who})
        _write(client, cur)
    except Exception as e:
        return False, f"保存できませんでした: {str(e)[:160]}"
    _CACHE.clear()
    return True, "保存しました。どのPCも、5分以内にこのキーを使います（secrets.toml のキーより優先）。"


def clear_shared(secrets=None, sb=None):
    s = secrets if secrets is not None else _toml()
    client = _sb(s, sb)
    if client is None:
        return False, "Supabase につながりません。"
    try:
        cur = _row(client)
        for k in ("key_enc", "saved_at", "saved_by"):
            cur.pop(k, None)
        _write(client, cur)
    except Exception as e:
        return False, f"消せませんでした: {str(e)[:160]}"
    _CACHE.clear()
    return True, "共有のキーを消しました。各PCの secrets.toml のキーに戻ります。"


def test(key: str):
    """キーが使えるか（モデル一覧を引くだけ）。戻り値：(OK, 言葉)"""
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        next(iter(genai.list_models()))
        return True, "このキーは使えます。"
    except Exception as e:
        return False, f"使えませんでした: {str(e)[:200]}"
