"""接続キーのファイル（.streamlit/secrets.toml）を、人が読める言葉で点検する。

⚠️ なぜ要るか：
   別のPCにアプリを入れるとき、このファイルをコピーし損ねて壊れることがある
   （実際に、いちばん最後の `"` が欠けたまま持っていって動かなくなった）。
   そのままだと Python の長いエラーが画面いっぱいに出るだけで、
   非エンジニアの担当者には**どこを直せばよいか分からない**。
   だから、**どの行の何が悪いか**と**直し方**を出して止める。

⚠️ ここでは**値を画面に出さない**。出すのは行番号と、書き方の間違いだけ。

画面なしでも使える：
   python secrets_check.py
"""

import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")

# 無いと動かないもの（あとから足したものは、ここに入れない）
REQUIRED = ["SUPABASE_URL", "SUPABASE_KEY"]

NL = chr(10)
Q = chr(34)

# 文字コードの目印（これが付いていると、TOMLとして読めないことがある）
BOMS = [
    (b"\xef\xbb\xbf", "UTF-8 BOM付き"),
    (b"\xff\xfe", "UTF-16 LE"),
    (b"\xfe\xff", "UTF-16 BE"),
]


def _hint(text: str, line_no: int) -> str:
    """その行を見て、ありがちな間違いを言い当てる。"""
    lines = text.split(NL)
    if not (1 <= line_no <= len(lines)):
        return ("ファイルの終わりで、文字列が閉じていません。"
                "**いちばん下の行の終わりに `" + Q + "` があるか**確かめてください。")
    line = lines[line_no - 1]
    key = line.split("=")[0].strip() if "=" in line else ""
    if line.count(Q) % 2 == 1:
        return ("**" + str(line_no) + "行目（" + key + "）の終わりに `" + Q + "` がありません。**"
                "値の前と後ろを `" + Q + "` ではさんでください。")
    if "'''" in line or '"""' in line:
        return (str(line_no) + "行目からの複数行の値が、閉じていないようです。"
                "`'''` で始めたら、**最後も `'''` で閉じて**ください。")
    return str(line_no) + "行目のあたりの書き方を見直してください。"


def _line_no(err) -> int:
    import re
    m = re.search(r"line (\d+)", str(err))
    return int(m.group(1)) if m else 0


def check(stop: bool = True) -> bool:
    """壊れていたら、直し方を画面に出して止める。問題なければ True。"""
    import streamlit as st

    if not os.path.isfile(PATH):
        st.error("🔑 **接続キーのファイルがありません。**")
        st.markdown("つくる場所：`" + PATH + "`")
        st.markdown("`.streamlit/secrets.toml.example` をコピーして名前を "
                    "`secrets.toml` に変え、値を入れてください。"
                    "**すでに動いているPCがあるなら、そのファイルをそのままコピー**"
                    "するのがいちばん確実です。")
        if stop:
            st.stop()
        return False

    raw = open(PATH, "rb").read()
    bom = next((name for sig, name in BOMS if raw.startswith(sig)), "")
    try:
        text = raw.decode("utf-8-sig")
    except Exception as e:
        st.error("🔑 **接続キーのファイルの文字コードが違います。**")
        st.markdown("メモ帳で開き、[名前を付けて保存] → **文字コードを『UTF-8』**にして"
                    "保存し直してください。")
        st.caption(str(e)[:200])
        if stop:
            st.stop()
        return False

    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception as e:
        st.error("🔑 **接続キーのファイル（secrets.toml）が壊れています。**")
        st.markdown("場所：`" + PATH + "`")
        n = _line_no(e)
        if n:
            st.markdown(_hint(text, n))
        if bom:
            st.warning("⚠️ このファイルは **" + bom + "** で保存されています。"
                       "メモ帳の[名前を付けて保存]で、文字コードを**UTF-8**にしてください。")
        st.info("💡 **いちばん確実な直し方**：すでに動いているPCの "
                "`.streamlit` フォルダの `secrets.toml` を、"
                "このPCの同じ場所に**そのままコピー**してください。" + NL + NL +
                "⚠️ とくに `ENKAN_SECRET_KEY` は、**PCごとに違うと"
                "暗号化して保存したパスワードが読めなくなります**。"
                "手で打ち直さず、コピーしてください。")
        st.caption("くわしく調べるには、ENKAN_APP のフォルダで "
                   "`python secrets_check.py` を実行してください。")
        with st.expander("開発者向け：元のエラー"):
            st.code(str(e))
        if stop:
            st.stop()
        return False

    missing = [k for k in REQUIRED if k not in data]
    if missing:
        st.error("🔑 **接続キーが足りません：** " + "／".join(missing))
        st.markdown("`" + PATH + "` に、この項目を書き足してください。")
        if stop:
            st.stop()
        return False
    return True


# ==========================================
# 画面なしでの点検（別のPCで、そのまま実行して確かめる）
#   python secrets_check.py
# ⚠️ 値は出さない。出すのは行番号と、書き方の間違いだけ。
# ==========================================
def report() -> str:
    out = ["=== 接続キーのファイル 点検 ===", "場所: " + PATH]
    if not os.path.isfile(PATH):
        out.append("結果: ❌ ファイルがありません。")
        return NL.join(out)

    raw = open(PATH, "rb").read()
    out.append("大きさ: " + str(len(raw)) + " バイト")
    bom = next((name for sig, name in BOMS if raw.startswith(sig)), "")
    if bom:
        out.append("文字コード: ⚠️ " + bom)
        out.append("  → メモ帳の[名前を付けて保存]で、文字コードを『UTF-8』にしてください。")
    try:
        text = raw.decode("utf-8-sig")
    except Exception as e:
        out.append("結果: ❌ UTF-8として読めません（" + str(e)[:120] + "）")
        out.append("  → メモ帳で開き、[名前を付けて保存] → 文字コード『UTF-8』で保存し直してください。")
        return NL.join(out)

    lines = text.split(NL)
    out.append("行数: " + str(len(lines)) + " ／ 文字数: " + str(len(text)))
    last = next((x for x in reversed(lines) if x.strip()), "")
    out.append("最後の中身のある行: " + (last.split("=")[0].strip() or "?") + " = …"
               "（" + str(len(last)) + "文字／終わりが " + Q + " か: "
               + ("はい" if last.rstrip().endswith(Q) else "❌ いいえ") + "）")

    bad = [i + 1 for i, x in enumerate(lines)
           if "=" in x and not x.lstrip().startswith("#") and x.count(Q) % 2 == 1]
    if bad:
        out.append("⚠️ " + Q + " の数が合わない行: " + str(bad) + "（前と後ろではさめていない）")

    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception as e:
        out.append("結果: ❌ 読み取れません → " + str(e))
        n = _line_no(e)
        if n:
            out.append("  → " + _hint(text, n).replace("**", ""))
        out.append("  → 動いているPCのファイルを、そのままコピーするのが確実です。")
        return NL.join(out)

    out.append("結果: ✅ 書き方は正しく読めました。")
    missing = [k for k in REQUIRED if k not in data]
    out.append("必要なキー: "
               + ("❌ 足りない → " + "／".join(missing) if missing else "✅ そろっています"))
    for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "ENKAN_SECRET_KEY", "GEMINI_API_KEY"):
        out.append("  " + k + ": " + ("あり" if k in data else "（なし）"))
    if "ENKAN_SECRET_KEY" in data:
        import hashlib
        h = hashlib.sha256(str(data["ENKAN_SECRET_KEY"]).encode()).hexdigest()[:8]
        out.append("  ENKAN_SECRET_KEY の照合印: " + h
                   + "（動いているPCと同じ印なら、同じ鍵です）")
    return NL.join(out)


if __name__ == "__main__":
    print(report())
