"""
京セラ Network FAX の画面を、どのやり方なら読めるか調べる（送らない・押さない。読むだけ）。

使い方：京セラの「宛先の選択」（アドレス帳）の画面を開いたまま、tools\\fax_probe.bat をダブルクリック。
結果は 取り込みファイル\\地域手配\\FAX\\調べた結果.txt に書き、メモ帳で開く。

⚠️ お試しのロボットが、アドレス帳を開いたところで結果を返さずに固まった（2026-09-26）。
   どの読み方で固まるのかを見るため、3つのやり方をそれぞれ20秒だけ試す（固まっても次へ進む）。
"""
import ctypes
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "取り込みファイル", "地域手配", "FAX", "調べた結果.txt")
LIMIT = 20

lines = []


def say(*a):
    t = " ".join(str(x) for x in a)
    lines.append(t)
    try:
        print(t, flush=True)
    except Exception:
        pass
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def within(title, fn):
    """fn を LIMIT 秒だけ待つ。固まったら『固まった』と書いて先へ進む。"""
    box = {}

    def run():
        try:
            box["ok"] = fn()
        except Exception:
            box["err"] = traceback.format_exc()[-1500:]
    th = threading.Thread(target=run, daemon=True)
    t0 = time.time()
    th.start()
    th.join(LIMIT)
    say(f"\n===== {title}（{time.time() - t0:.1f}秒）")
    if th.is_alive():
        say(f"XX {LIMIT}秒たっても返ってきません＝このやり方で固まります")
    elif "err" in box:
        say("XX エラー：\n" + box["err"])
    else:
        say(box.get("ok"))


# ── ① Windows の窓の一覧（いちばん素朴なやり方・固まらない）
user32 = ctypes.windll.user32
SMTO_ABORTIFHUNG = 0x0002
WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E


def text_of(h):
    n = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(h, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 2000, ctypes.byref(n)):
        return "（読めない）"
    buf = ctypes.create_unicode_buffer(n.value + 1)
    r = ctypes.c_size_t()
    user32.SendMessageTimeoutW(h, WM_GETTEXT, n.value + 1, buf, SMTO_ABORTIFHUNG, 2000, ctypes.byref(r))
    return buf.value


def class_of(h):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def top_windows():
    out = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if user32.IsWindowVisible(h):
            t = text_of(h)
            if t:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                hung = bool(user32.IsHungAppWindow(h))
                out.append((h, t, class_of(h), pid.value, hung))
        return True
    user32.EnumWindows(cb_t(cb), 0)
    return out


def children(h):
    out = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(c, _):
        out.append(f"  {class_of(c)}\t{text_of(c)!r}\tid={user32.GetDlgCtrlID(c)}")
        return True
    user32.EnumChildWindows(h, cb_t(cb), 0)
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    say("京セラの画面を調べます（読むだけ。何も押しません）", time.strftime("%Y-%m-%d %H:%M:%S"))
    say("Python：", sys.version.split()[0], "64bit" if sys.maxsize > 2**32 else "32bit")
    wins = top_windows()
    kyo = [w for w in wins if "Network FAX" in w[1] or "宛先の選択" in w[1]]
    say("\n===== ① 京セラの窓")
    for h, t, c, pid, hung in kyo:
        say(f"{t}｜{c}｜pid={pid}｜応答なし={hung}")
    for h, t, c, pid, hung in kyo:
        try:
            import psutil
            p = psutil.Process(pid)
            say(f"  pid={pid} のプログラム：{p.name()}｜{' '.join(p.cmdline())[:200]}")
        except Exception:
            pass
    book = [w for w in kyo if "宛先の選択" in w[1]]
    if not book:
        say("\nXX 「宛先の選択」の画面が見つかりません。アドレス帳を開いてからもう一度ダブルクリックしてください")
        return
    bh = book[0][0]
    within("② 宛先の選択の部品（Windowsの素朴なやり方）", lambda: children(bh))

    def win32_list():
        from pywinauto import Application
        dlg = Application(backend="win32").connect(handle=bh).window(handle=bh)
        out = []
        for lv in dlg.children(class_name="SysListView32"):
            out.append(f"表 {lv.handle}：{lv.item_count()}行・{lv.column_count()}列")
            for i in range(lv.item_count()):
                out.append("  " + " | ".join(lv.get_item(i, c).text() for c in range(max(1, lv.column_count()))))
        return "\n".join(out) or "SysListView32 の表がありません"
    within("③ 表を win32 のやり方で読む", win32_list)

    def uia_list():
        from pywinauto import Desktop
        w = Desktop(backend="uia").window(handle=bh)
        out = []
        for it in w.descendants(control_type="ListItem"):
            out.append("  " + " | ".join([it.window_text()] + [c.window_text() for c in it.children()]))
        return "\n".join(out) or "ListItem がありません"
    within("④ 表を UIA のやり方で読む", uia_list)

    def uia_buttons():
        from pywinauto import Desktop
        w = Desktop(backend="uia").window(handle=bh)
        return "\n".join(f"  {b.window_text()!r}" for b in w.descendants(control_type="Button"))
    within("⑤ ボタンを UIA のやり方で読む", uia_buttons)
    say("\n終わりました。この画面（またはメモ帳）をスクショで送ってください。")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.startfile(OUT)
        except Exception:
            pass
        os._exit(0)
