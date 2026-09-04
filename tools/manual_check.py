"""
📘 設定マニュアルのずれを見つける道具。

画面（Streamlit）の入力欄を機械的に数え上げて、前回マニュアルを直したときの
記録（docs/manual_items.json）と見比べる。

  増えた項目 → マニュアルに書き足す必要がある
  消えた項目 → マニュアルから消す必要がある
  変わった項目 → 文言を直す必要がある

⚠️ 文章そのものを作り直すことはしない（それは人／AIの仕事）。
   この道具の役目は「どこが変わったか」を見落とさせないこと。

使い方：
  python tools/manual_check.py            いまの項目を一覧で見る
  python tools/manual_check.py --check    記録と見比べる（ずれていれば表示）
  python tools/manual_check.py --save     いまの状態を記録に取り込む（マニュアルを直したあと）
"""
import ast
import glob
import json
import os
import sys

# ⚠️ Windowsのコマンド画面は既定が cp932 なので、絵文字を出すと落ちる。
#    ここで UTF-8 に切り替えておく（落ちたら何も表示されず、ずれに気づけない）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "docs", "manual_items.json")
MANUAL = os.path.join(ROOT, "docs", "エンカンAI_設定マニュアル.html")

# 「人が値を決める欄」だけを数える。ボタンや見出しは対象外
# （ボタンが増えても設定項目は増えないので、鳴らしすぎると読まれなくなる）
WIDGETS = {
    "text_input", "text_area", "selectbox", "multiselect", "checkbox",
    "number_input", "radio", "toggle", "file_uploader", "date_input",
    "time_input", "slider", "select_slider", "color_picker",
}
# 表の中の列（手順書の「いつ」「対象」など）も設定項目なので拾う
COLUMN_CONFIGS = {
    "TextColumn", "SelectboxColumn", "NumberColumn", "CheckboxColumn",
    "DateColumn", "ListColumn",
}

# 見に行くファイル。画面を持つものだけ（robot.py などの実行側は対象外）
TARGET_GLOBS = [
    "app.py",
    "pages/*.py",
    "common_robots.py",
    "entry_loader.py",
    "report_refresh.py",
    "robot_settings_ui.py",
    "sf_ui.py",
]


def _label(node):
    """呼び出しの第1引数が文字列ならそれを返す（f文字列や変数は対象外）。"""
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value.strip()
    for kw in node.keywords:
        if kw.arg == "label" and isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str):
            return kw.value.value.strip()
    return None


def _items_in(path):
    """1ファイルから「種類｜ラベル」の一覧を作る。"""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError) as e:
        print(f"⚠️ {path} を読めませんでした：{e}", file=sys.stderr)
        return []

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        owner = node.func.value

        # st.text_input("…") の形
        if attr in WIDGETS and isinstance(owner, ast.Name) and owner.id == "st":
            label = _label(node)
            if label:
                found.append(f"{attr}｜{label}")

        # st.column_config.TextColumn("…") の形（表の列）
        elif attr in COLUMN_CONFIGS:
            label = _label(node)
            if label:
                found.append(f"列｜{label}")

    # 同じ画面に同じ文言が2回出ることがあるので、順序を保ったまま重複を消す
    return list(dict.fromkeys(found))


def collect():
    """対象ファイルぜんぶを数え上げる。"""
    out = {}
    for pattern in TARGET_GLOBS:
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            items = _items_in(path)
            if items:
                out[rel] = sorted(items)
    return out


def _load_snapshot():
    if not os.path.exists(SNAPSHOT):
        return None
    try:
        with open(SNAPSHOT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_snapshot(data):
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def diff(old, new):
    """記録といまを見比べて、ファイルごとの 増えた／消えた を返す。"""
    changes = {}
    for path in sorted(set(old) | set(new)):
        before = set(old.get(path, []))
        after = set(new.get(path, []))
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            changes[path] = {"増えた": added, "消えた": removed}
    return changes


def main():
    args = set(sys.argv[1:])
    now = collect()

    if "--save" in args:
        _save_snapshot(now)
        total = sum(len(v) for v in now.values())
        print(f"✅ いまの状態を記録しました（{len(now)}ファイル・{total}項目）。")
        return 0

    if "--check" not in args:
        for path, items in now.items():
            print(f"\n■ {path}（{len(items)}項目）")
            for it in items:
                print(f"   {it}")
        print(f"\n合計 {sum(len(v) for v in now.values())} 項目")
        return 0

    # --check：記録と見比べる
    old = _load_snapshot()
    if old is None:
        print("📘 マニュアルの記録がまだありません。"
              "`python tools/manual_check.py --save` で作ってください。")
        return 0

    changes = diff(old, now)
    if not changes:
        return 0  # ずれていないときは何も言わない（毎回鳴ると読まれなくなる）

    print("📘 **設定マニュアルとのずれがあります。**"
          " 画面の入力欄が変わったので、"
          "`docs/エンカンAI_設定マニュアル.html` も直してください。")
    for path, c in changes.items():
        print(f"\n■ {path}")
        for it in c["増えた"]:
            print(f"   ＋ 増えた：{it}")
        for it in c["消えた"]:
            print(f"   － 消えた：{it}")
    print("\n直したら：")
    print("  1. docs/エンカンAI_設定マニュアル.html の該当の表を書き足す／消す")
    print("  2. python tools/manual_check.py --save  で記録を更新する")
    print("  3. マニュアルのページ（Artifact）を同じURLに publish し直す")
    print("     → URLは CLAUDE.md の「📘 設定マニュアル」に書いてあります")
    return 0


if __name__ == "__main__":
    sys.exit(main())
