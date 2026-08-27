"""接続キーのファイル（.streamlit/secrets.toml）を、人が読める言葉で点検する。

⚠️ なぜ要るか：
   別のPCにアプリを入れるとき、このファイルをコピーし損ねて壊れることがある
   （実際に、いちばん最後の `"` が欠けたまま持っていって動かなくなった）。
   そのままだと Python の長いエラーが画面いっぱいに出るだけで、
   非エンジニアの担当者には**どこを直せばよいか分からない**。
   だから、**どの行の何が悪いか**と**直し方**を出して止める。

⚠️ ここでは**値を画面に出さない**。出すのは行番号と、書き方の間違いだけ。
"""

import os

import streamlit as st

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")

# 無いと動かないもの（あとから足したものは、ここに入れない）
REQUIRED = ["SUPABASE_URL", "SUPABASE_KEY"]


def _read():
    with open(PATH, encoding="utf-8") as f:
        return f.read()


def _hint(text: str, line_no: int) -> str:
    """その行を見て、ありがちな間違いを言い当てる。"""
    lines = text.split("\n")
    if not (1 <= line_no <= len(lines)):
        return ("ファイルの終わりで、文字列が閉じていません。"
                "**いちばん下の行の終わりに `\"` があるか**確かめてください。")
    line = lines[line_no - 1]
    key = line.split("=")[0].strip() if "=" in line else ""
    q = line.count('"')
    if q % 2 == 1:
        return (f"**{line_no}行目（{key}）の終わりに `\"` がありません。**"
                "値の前と後ろを `\"` ではさんでください。")
    if "'''" in line or '"""' in line:
        return (f"{line_no}行目からの複数行の値が、閉じていないようです。"
                "`'''` で始めたら、**最後も `'''` で閉じて**ください。")
    return f"{line_no}行目のあたりの書き方を見直してください。"


def check(stop: bool = True) -> bool:
    """壊れていたら、直し方を出して止める。問題なければ True。"""
    if not os.path.isfile(PATH):
        st.error("🔑 **接続キーのファイルがありません。**")
        st.markdown(f"つくる場所：`{PATH}`")
        st.markdown("`.streamlit/secrets.toml.example` をコピーして名前を "
                    "`secrets.toml` に変え、値を入れてください。"
                    "**すでに動いているPCがあるなら、そのファイルをそのままコピー**"
                    "するのがいちばん確実です。")
        if stop:
            st.stop()
        return False

    try:
        text = _read()
    except Exception as e:
        st.error(f"🔑 接続キーのファイルを読めませんでした：{str(e)[:200]}")
        if stop:
            st.stop()
        return False

    try:
        import tomllib
        tomllib.loads(text)
    except Exception as e:
        # 「line 51 column 66」のような場所を取り出して、日本語で言い直す
        import re
        m = re.search(r"line (\d+)", str(e))
        line_no = int(m.group(1)) if m else 0
        st.error("🔑 **接続キーのファイル（secrets.toml）が壊れています。**")
        st.markdown(f"場所：`{PATH}`")
        if line_no:
            st.markdown(_hint(text, line_no))
        st.info("💡 **いちばん確実な直し方**：すでに動いているPCの "
                "`.streamlit\\\\secrets.toml` を、このPCの同じ場所に**そのままコピー**"
                "してください。\n\n"
                "⚠️ とくに `ENKAN_SECRET_KEY` は、**PCごとに違うと"
                "暗号化して保存したパスワードが読めなくなります**。"
                "手で打ち直さず、コピーしてください。")
        with st.expander("開発者向け：元のエラー"):
            st.code(str(e))
        if stop:
            st.stop()
        return False

    missing = [k for k in REQUIRED if k not in st.secrets]
    if missing:
        st.error("🔑 **接続キーが足りません：** " + "／".join(missing))
        st.markdown(f"`{PATH}` に、この項目を書き足してください。")
        if stop:
            st.stop()
        return False
    return True
