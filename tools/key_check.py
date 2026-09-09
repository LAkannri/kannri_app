# -*- coding: utf-8 -*-
"""🔑 このPCの鍵（ENKAN_SECRET_KEY）が、他のPCと同じものかを確かめる。

⚠️ なぜ要るか：
   `ENKAN_SECRET_KEY` は **PCごとに違うと、暗号化して保存したものが読めなくなる**。
   ・ロボットのログイン情報（ID・パスワード）
   ・GASを書き込むための許可（`__gas_auth__`）
   ところが、ファイルが「ある」だけでは分からない。**別の鍵で作り直してしまうと、
   画面上は正常に見えるのに、実行のときだけ「復号できません」で止まる。**
   そこで、実際に保存されているものを1つ復号してみて、白黒つける。

   ⚠️ 中身は表示しない（読めたかどうかだけ出す）。

使い方：
   python tools/key_check.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NL = chr(10)


def _secret(name: str) -> str:
    """secrets.toml か環境変数から読む（Streamlitを起動せずに読みたいので自前で）。"""
    v = os.environ.get(name, "")
    if v:
        return v
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".streamlit", "secrets.toml")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(name):
                    part = line.split("=", 1)
                    if len(part) == 2:
                        return part[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def main() -> int:
    key = _secret("ENKAN_SECRET_KEY").strip()
    if not key:
        print("[NG] ENKAN_SECRET_KEY がありません。")
        print("     動いているPCの .streamlit/secrets.toml を、そのままコピーしてください。")
        print("     ⚠️ 手で作り直すと、保存してあるパスワードが読めなくなります。")
        return 1

    try:
        from cryptography.fernet import Fernet
        f = Fernet(key.encode())
    except Exception as e:
        print(f"[NG] 鍵の形が正しくありません（{e}）。")
        print("     動いているPCの secrets.toml をそのままコピーしてください。")
        return 1

    url, anon = _secret("SUPABASE_URL"), _secret("SUPABASE_KEY")
    if not (url and anon):
        print("[--] Supabaseの接続キーが無いので、他のPCと同じ鍵かは確かめられません。")
        print("     鍵の形は正しいです。")
        return 0

    try:
        from supabase import create_client
        sb = create_client(url, anon)
        rows = sb.table("merchants").select("id,config_json").execute().data or []
    except Exception as e:
        print(f"[--] Supabaseに聞けませんでした（{str(e)[:120]}）。あとで試してください。")
        return 0

    # 🔎 暗号化して保存されているものを集める（中身は見ない）
    targets = []
    for r in rows:
        cfg = r.get("config_json") or {}
        enc = (cfg.get("robot_config") or {}).get("secrets") or {}
        for name, val in enc.items():
            targets.append((f"{r.get('id')} のログイン情報「{name}」", val))
        if cfg.get("token_enc"):
            targets.append(("GAS書き込みの許可", cfg["token_enc"]))
        if cfg.get("client_enc"):
            targets.append(("GASを書き込む鍵", cfg["client_enc"]))

    if not targets:
        print("[--] まだ暗号化して保存されたものがありません（確かめようがありません）。")
        print("     鍵の形は正しいので、このまま使えます。")
        return 0

    ok, ng = [], []
    for label, val in targets:
        try:
            f.decrypt(str(val).encode())
            ok.append(label)
        except Exception:
            ng.append(label)

    if not ng:
        print(f"[OK] このPCの鍵で、保存されているもの {len(ok)} 件すべてを読めました。")
        print("     他のPCと同じ鍵です。")
        return 0

    print(f"[NG] {len(ng)} 件が読めませんでした（このPCの鍵が違います）。")
    for label in ng[:5]:
        print("     ・" + label)
    print("     動いているPCの .streamlit/secrets.toml を、そのままコピーしてください。")
    print("     ⚠️ 手で打ち直さないこと。1文字でも違うと読めません。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
