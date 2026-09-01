"""同じ画面のファイルが2つ残っていたら、古いほうを消す。

⚠️ なぜ要るか：
   ZIPで上書きすると「新しいファイルを置く」だけで、
   **名前が変わって不要になったファイルは残る**。
   Streamlit は同じ名前の画面が2つあると、
   「Multiple Pages specified with URL pathname …」で起動しなくなる。
   （例：その他設定が 6番 から 8番 になったとき、6番が残っていた）

   ファイル名の「番号を外した部分」で見比べ、**番号が大きいほうを残す**。
   番号は増える方向にしか振り直さないので、これで新しいほうが残る。

   python cleanup_pages.py        … 消す
   python cleanup_pages.py --dry  … 消さずに、何を消すかだけ出す
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.join(HERE, "pages")


def stale_pages():
    """(消すもの, 残すもの) の組を返す。"""
    if not os.path.isdir(PAGES):
        return []
    groups = {}
    for name in os.listdir(PAGES):
        if not name.endswith(".py"):
            continue
        m = re.match(r"^(\d+)_(.+)$", name)
        if not m:
            continue
        groups.setdefault(m.group(2), []).append((int(m.group(1)), name))
    out = []
    for _key, items in groups.items():
        if len(items) < 2:
            continue
        items.sort()                      # 番号の小さい順
        keep = items[-1][1]               # いちばん大きい番号を残す
        for _no, name in items[:-1]:
            out.append((name, keep))
    return out


def main(dry: bool = False) -> int:
    pairs = stale_pages()
    if not pairs:
        print("画面のファイルに重複はありません。")
        return 0
    for old, keep in pairs:
        print(f"重複：{old}（古い） ／ {keep}（残す）")
        if dry:
            continue
        try:
            os.remove(os.path.join(PAGES, old))
            print(f"  → 消しました：{old}")
        except Exception as e:
            print(f"  → 消せませんでした：{e}")
    return len(pairs)


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
