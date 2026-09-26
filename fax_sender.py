"""
📠 京セラ Network FAX でFAXを送るロボット（`chiiki_fax.send` が別の処理として呼ぶ）。

  python fax_sender.py <送る.json> <結果.json>
  python fax_sender.py --print <pdf> <プリンタ名>   … PDFをプリンタに渡すだけ（中で使う）
  python fax_sender.py --dump                        … 開いている京セラの画面の部品を書き出す（調整用）

1通ごと：
  ① PDFを NW-FAX のプリンタに渡す（別の処理。京セラの画面はそちらで開く）
  ② 京セラの「送信設定」画面を待つ → 「アドレス帳より選択」
  ③ 「宛先の選択」で宛先名を選び → 追加 → OK
  ④ 宛先リストが**ちょうど1件**で、番号が決めてある番号と同じか確かめる。違えば キャンセル
  ⑤ submit なら 送信、お試しなら キャンセル（送らない）

⚠️ 京セラの画面の部品名は、2026-09-26 に担当者の画面の画像から書いた。まだ実機で動かしていない。
   合わないときは `--dump` で部品名を書き出して直す。止まったときは部品の一覧をログに出す。
"""
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

# ⚠️ 「送信設定」まで見る。同じ「Kyocera Network FAX」で始まる「送信管理」の画面も開いていて、
#    そちらをつかんで「アドレス帳より選択が無い」で止まった（2026-09-26 お試し）
MAIN_TITLE = r".*Network FAX.*送信設定.*"   # 「Kyocera Network FAX - 送信設定 - 千葉県水道局FAX.pdf」
BOOK_TITLE = r".*宛先の選択.*"
WAIT_DIALOG = 120


# ⚠️ ログはファイルに書く。Windowsの既定（cp932）では 🛑 などが書けず、止まった理由ごと落ちる
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def digits(v) -> str:
    return re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", str(v or "")))


# ──────────────────────────────────────────
# PDF → プリンタ
# ──────────────────────────────────────────
def print_pdf(pdf: str, printer: str, outfile: str = ""):
    """PDFを1ページずつ画像にして、プリンタに描く（A3横）。京セラの画面はここで開く。"""
    import pymupdf
    import win32con
    import win32gui
    import win32print
    import win32ui
    from PIL import Image, ImageWin
    h = win32print.OpenPrinter(printer)
    try:
        dm = win32print.GetPrinter(h, 2)["pDevMode"]
    finally:
        win32print.ClosePrinter(h)
    dm.PaperSize = 8          # A3
    dm.Orientation = 2        # 横
    hdc_raw = win32gui.CreateDC("WINSPOOL", printer, dm)
    dc = win32ui.CreateDCFromHandle(hdc_raw)
    W, H = dc.GetDeviceCaps(win32con.HORZRES), dc.GetDeviceCaps(win32con.VERTRES)
    doc = pymupdf.open(pdf)
    # outfile＝試すとき用（「Microsoft Print to PDF」でファイルに出す）
    dc.StartDoc(os.path.basename(pdf), outfile) if outfile else dc.StartDoc(os.path.basename(pdf))
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        s = min(W / img.width, H / img.height)
        w, hh = int(img.width * s), int(img.height * s)
        x, y = (W - w) // 2, (H - hh) // 2
        dc.StartPage()
        ImageWin.Dib(img).draw(dc.GetHandleOutput(), (x, y, x + w, y + hh))
        dc.EndPage()
    dc.EndDoc()
    dc.DeleteDC()


# ──────────────────────────────────────────
# 京セラの画面
# ──────────────────────────────────────────
def _app_window(title_re, timeout):
    from pywinauto import Desktop
    end = time.time() + timeout
    while time.time() < end:
        for w in Desktop(backend="uia").windows():
            try:
                if re.match(title_re, w.window_text() or ""):
                    return w
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError(f"画面が出てきません（{title_re}・{timeout}秒）")


def _button(win, pattern):
    # ⚠️ 京セラの画面の「キャンセル」は半角カナ（ｷｬﾝｾﾙ）。NFKC でそろえてから見る
    #    （全角で探していたので、つまずいたときに画面を閉じられず、開いたまま残っていた）
    for b in win.descendants(control_type="Button"):
        if re.search(pattern, unicodedata.normalize("NFKC", b.window_text() or "")):
            return b
    raise RuntimeError(f"ボタン「{pattern}」が見つかりません")


def _rows(win):
    """表（宛先リスト・アドレス帳）の行ごとの文字。"""
    out = []
    for it in win.descendants(control_type="ListItem"):
        try:
            texts = [c.window_text() for c in it.descendants()] + [it.window_text()]
        except Exception:
            texts = [it.window_text()]
        out.append((it, [t for t in texts if t]))
    return out


def _lists_win32(win):
    """アドレス帳の表（SysListView32）を win32 のやり方で読む。[(表, [[列の文字…] 行ごと])]。
    ⚠️ 京セラの画面は古いWindowsの部品で、UIAでは行の中の文字が読めないことがある（2026-09-26 お試しで止まった）。"""
    from pywinauto import Application
    dlg = Application(backend="win32").connect(handle=win.handle).window(handle=win.handle)
    out = []
    for lv in dlg.children(class_name="SysListView32"):
        rows = []
        for i in range(lv.item_count()):
            rows.append([lv.get_item(i, c).text() or "" for c in range(max(1, lv.column_count()))])
        out.append((lv, rows))
    return out


def _book_detail(bk):
    """アドレス帳の下の欄（選んだ宛先の『名前・改行・番号』・部品番号 1143）。読めなければ None。"""
    try:
        from pywinauto import Application
        dlg = Application(backend="win32").connect(handle=bk.handle).window(handle=bk.handle)
        for e in dlg.children(class_name="Edit"):
            t = e.window_text() or ""
            if e.control_id() == 1143 or "\n" in t:
                return t
    except Exception as e:
        say("下の欄を読めませんでした：", e)
    return None


def _pick_in_book(bk, name, num):
    """宛先の選択：FAX番号がちょうど1行に当たる行を選んで「追加 >」→ 右の追加リストに1件だけ入ったか確かめる。"""
    lists = []
    say("アドレス帳の表を読みます（win32）")
    try:
        lists = _lists_win32(bk)
    except Exception as e:
        say("win32 では表を読めませんでした：", e)
    if lists:
        say("アドレス帳の表：", [rows for _, rows in lists])
        # 左＝アドレス帳（行が多い方）、右＝追加リスト
        left, left_rows = max(lists, key=lambda x: len(x[1]))
        hit = [i for i, r in enumerate(left_rows) if num in [digits(x) for x in r]]
        if len(hit) != 1:
            raise RuntimeError(f"アドレス帳で番号 {num}（{name}）の行が{len(hit)}件でした（1件のときだけ選びます）。"
                               f"読めた行：{left_rows[:12]}")
        i = hit[0]
        say("アドレス帳の行：", left_rows[i])
        left.ensure_visible(i)
        left.get_item(i).click_input()
        time.sleep(0.3)
        if not left.is_selected(i):
            left.select(i)
            time.sleep(0.3)
        # ⭐ 画面の下の欄（選んだ宛先の名前と番号）に、この番号が出ているか＝京セラ自身が選んだと認めたか
        shown = _book_detail(bk)
        say("選んだ宛先の欄：", repr(shown))
        if shown is not None and num not in digits(shown):
            raise RuntimeError(f"アドレス帳で選べていません（下の欄は {shown!r}）。送りません")
        _button(bk, r"^追加").click_input()
        time.sleep(0.8)
        # ⭐ 右の追加リストに、この番号の1件だけが入ったか（違う宛先を選んでいたら、ここで止まる）
        others = [(lv, [r for r in rows]) for lv, rows in _lists_win32(bk) if lv.handle != left.handle]
        added = [r for _, rows in others for r in rows]
        if len(added) != 1 or num not in [digits(x) for x in added[0]]:
            raise RuntimeError(f"追加リストが想定と違います（{added}）。送りません")
        return
    # win32 で表が見つからないときは UIA で
    rows_bk = _rows(bk)
    hit = [(it, t) for it, t in rows_bk if num in [digits(x) for x in t]]
    if len(hit) != 1:
        seen_rows = ["｜".join(t) for _, t in rows_bk][:12]
        raise RuntimeError(f"アドレス帳で番号 {num}（{name}）の行が{len(hit)}件でした（1件のときだけ選びます）。"
                           f"読めた行：{seen_rows}")
    it, texts = hit[0]
    say("アドレス帳の行：", texts)
    it.click_input()
    time.sleep(0.3)
    try:
        if not it.is_selected():
            it.select()
    except Exception:
        pass
    _button(bk, r"^追加").click_input()
    time.sleep(0.5)


def _dump(win, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            for c in win.descendants():
                try:
                    f.write(f"{c.element_info.control_type}\t{c.window_text()!r}\t{c.element_info.automation_id}\n")
                except Exception:
                    pass
    except Exception:
        pass


# ──────────────────────────────────────────
# Windows の素朴なやり方（押すのは「合図を置くだけ」＝返事を待たない）
# ⚠️ pywinauto で押すと、アドレス帳（閉じるまで戻らない画面）を開いたところで返事を待って固まった
#    （2026-09-26 お試しで3回）。tools/fax_probe.py では同じ画面が1秒かからずに読めたので、
#    押す・読むはこちらのやり方にそろえる。読むときも2秒で打ち切る。
# 部品の番号は tools/fax_probe.py で調べた：アドレス帳の表=1137・下の欄=1143・追加=1139・
#    追加リスト=1138・件数=1149・OK=1・ｷｬﾝｾﾙ=2
# ──────────────────────────────────────────
import ctypes
import threading
from ctypes import wintypes

_u32 = ctypes.windll.user32 if os.name == "nt" else None
if _u32:
    _u32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.GetDlgItem.restype = wintypes.HWND
WM_GETTEXT, WM_GETTEXTLENGTH, WM_COMMAND, WM_KEYDOWN, WM_KEYUP = 0x000D, 0x000E, 0x0111, 0x0100, 0x0101
VK_HOME, VK_DOWN = 0x24, 0x28
LVM_GETITEMCOUNT = 0x1004
SMTO_ABORTIFHUNG = 0x0002
ID_OK, ID_CANCEL = 1, 2
ID_BOOK_LIST, ID_BOOK_DETAIL, ID_BOOK_ADD, ID_BOOK_ADDED = 1137, 1143, 1139, 1138


def _send_timeout(h, msg, wp=0, lp=0):
    r = ctypes.c_size_t()
    if not _u32.SendMessageTimeoutW(h, msg, wp, lp, SMTO_ABORTIFHUNG, 2000, ctypes.byref(r)):
        return None
    return r.value


def w_text(h) -> str:
    n = _send_timeout(h, WM_GETTEXTLENGTH)
    if n is None:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    r = ctypes.c_size_t()
    _u32.SendMessageTimeoutW(h, WM_GETTEXT, n + 1, ctypes.addressof(buf), SMTO_ABORTIFHUNG, 2000, ctypes.byref(r))
    return buf.value


def w_class(h) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(h, buf, 256)
    return buf.value


def w_top(title_re, timeout):
    """題名が合う窓（見えているもの）を待つ。"""
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    end = time.time() + timeout
    while True:
        hit = []

        def cb(h, _):
            if _u32.IsWindowVisible(h) and re.match(title_re, w_text(h) or ""):
                hit.append(h)
            return True
        _u32.EnumWindows(cb_t(cb), 0)
        if hit:
            return hit[0]
        if time.time() > end:
            raise RuntimeError(f"画面が出てきません（{title_re}・{timeout}秒）")
        time.sleep(0.5)


def w_children(h) -> list:
    out = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(c, _):
        out.append(c)
        return True
    _u32.EnumChildWindows(h, cb_t(cb), 0)
    return out


def w_button(dlg, pattern):
    """文字が合うボタン（半角カナも NFKC でそろえて見る）。"""
    for c in w_children(dlg):
        if w_class(c) == "Button" and re.search(pattern, unicodedata.normalize("NFKC", w_text(c))):
            return c
    raise RuntimeError(f"ボタン「{pattern}」が見つかりません")


def w_item(dlg, ctrl_id):
    h = _u32.GetDlgItem(dlg, ctrl_id)
    if not h:
        raise RuntimeError(f"部品 {ctrl_id} が見つかりません")
    return h


def w_press(dlg, ctrl):
    """ボタンが押された合図を画面に置く（返事は待たない）。"""
    cid = _u32.GetDlgCtrlID(ctrl)
    _u32.PostMessageW(dlg, WM_COMMAND, cid & 0xFFFF, ctrl)


def w_cancel(dlg):
    _u32.PostMessageW(dlg, WM_COMMAND, ID_CANCEL, 0)


def w_gone(h, timeout=10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not _u32.IsWindow(h) or not _u32.IsWindowVisible(h):
            return True
        time.sleep(0.3)
    return False


def w_list_rows(h_list, limit=20):
    """表（SysListView32）の中身を読む。固まったら limit 秒で打ち切る（None を返す）。"""
    box = {}

    def run():
        try:
            from pywinauto.controls.common_controls import ListViewWrapper
            lv = ListViewWrapper(h_list)
            box["rows"] = [[lv.get_item(i, c).text() or "" for c in range(max(1, lv.column_count()))]
                           for i in range(lv.item_count())]
        except Exception as e:
            box["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive():
        say(f"⚠️ 表を{limit}秒で読めませんでした")
        return None
    if "err" in box:
        say("⚠️ 表を読めませんでした：", box["err"])
        return None
    return box["rows"]


def w_dump(h, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            for c in w_children(h):
                f.write(f"{w_class(c)}\t{w_text(c)!r}\tid={_u32.GetDlgCtrlID(c)}\n")
    except Exception:
        pass


def _pick_row(bk, num):
    """アドレス帳で、番号がちょうど1行に当たる行を選ぶ。選べたかは下の欄（1143）の番号で確かめる。"""
    lst = w_item(bk, ID_BOOK_LIST)
    rows = w_list_rows(lst)
    say("アドレス帳の表：", rows)
    if rows is None:
        raise RuntimeError("アドレス帳の表を読めませんでした")
    hit = [i for i, r in enumerate(rows) if num in [digits(x) for x in r]]
    if len(hit) != 1:
        raise RuntimeError(f"アドレス帳で番号 {num} の行が{len(hit)}件でした（1件のときだけ選びます）。読めた行：{rows[:12]}")
    i = hit[0]
    say("選ぶ行：", i + 1, "行目", rows[i])
    # 表にキー操作を送る（先頭へ → ↓ を i 回）。返事は待たない
    for vk in [VK_HOME] + [VK_DOWN] * i:
        _u32.PostMessageW(lst, WM_KEYDOWN, vk, 0)
        _u32.PostMessageW(lst, WM_KEYUP, vk, 0xC0000001)
        time.sleep(0.15)
    for _ in range(20):
        time.sleep(0.25)
        shown = w_text(w_item(bk, ID_BOOK_DETAIL))
        if num in digits(shown):
            say("選べました（下の欄）：", repr(shown))
            return
    raise RuntimeError(f"アドレス帳で選べませんでした（下の欄は {shown!r}）。送りません")


def send_one(job: dict, printer: str, submit: bool, dump_dir: str) -> dict:
    name, num = job["宛先名"], digits(job["FAX番号"])
    res = {"シート": job["シート"], "結果": "🛑", "中身": ""}
    pr = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--print", job["pdf"], printer])
    main = bk = None
    try:
        main = w_top(MAIN_TITLE, WAIT_DIALOG)
        say("京セラの画面が開きました：", w_text(main))
        time.sleep(1)
        say("「アドレス帳より選択」を押します")
        w_press(main, w_button(main, r"アドレス帳より選択"))
        bk = w_top(BOOK_TITLE, 30)
        say("アドレス帳の画面が開きました")
        time.sleep(0.5)
        _pick_row(bk, num)
        say("「追加 >」を押します")
        w_press(bk, w_item(bk, ID_BOOK_ADD))
        time.sleep(1)
        # ⭐ 追加リストにちょうど1件・その番号か（違う宛先を選んでいたら、ここで止まる）
        added = w_list_rows(w_item(bk, ID_BOOK_ADDED))
        say("追加リスト：", added)
        if not added or len(added) != 1 or num not in [digits(x) for x in added[0]]:
            raise RuntimeError(f"追加リストが想定と違います（{added}）。送りません")
        say("「OK」を押します")
        w_press(bk, w_item(bk, ID_OK))
        if not w_gone(bk):
            raise RuntimeError("アドレス帳の画面が閉じません")
        bk = None
        time.sleep(1)
        # 送信設定の画面の宛先リスト：ちょうど1件・番号が一致
        rows = []
        for c in w_children(main):
            if w_class(c) == "SysListView32":
                rows += [r for r in (w_list_rows(c) or []) if any(r)]
        say("宛先リスト：", rows)
        nums = [digits(x) for t in rows for x in t if len(digits(x)) >= 10]
        if len(rows) != 1 or nums != [num]:
            raise RuntimeError(f"宛先リストが想定と違います（{rows}）。送りません")
        if submit:
            say("「送信」を押します")
            w_press(main, w_button(main, r"^送信"))
            res.update({"結果": "✅", "中身": f"{name}（{num}）へ送信しました"})
        else:
            say("お試しなので「ｷｬﾝｾﾙ」を押します")
            w_cancel(main)
            res.update({"結果": "🧪", "中身": f"{name}（{num}）を選んで確かめました（お試しなので送っていません）"})
        if not w_gone(main, 20):
            res["中身"] += "（⚠️ 京セラの画面が閉じていません。画面を確かめてください）"
        say(res["中身"])
    except Exception as e:
        res["中身"] = str(e)[:400]
        say("🛑", res["中身"])
        if bk:
            w_dump(bk, os.path.join(dump_dir, f"部品_アドレス帳_{job['シート']}.txt"))
            w_cancel(bk)
            w_gone(bk, 5)
        if main:
            w_dump(main, os.path.join(dump_dir, f"部品_{job['シート']}.txt"))
            w_cancel(main)
            res["中身"] += ("（京セラの画面はキャンセルで閉じました）" if w_gone(main, 10)
                           else "（⚠️ 京セラの画面を閉じられませんでした。画面を確かめてください）")
    finally:
        try:
            pr.wait(timeout=120)
        except Exception:
            pr.kill()
    return res


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--print":
        print_pdf(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--dump":
        w = _app_window(MAIN_TITLE, 10)
        _dump(w, "京セラの部品.txt")
        print("京セラの部品.txt に書き出しました")
        return
    job_path, out_path = sys.argv[1], sys.argv[2]
    # ⭐ 固まったときに「どこで固まったか」をログに残す（45秒ごとに、いま動いている行を書き出す）。
    #    京セラの画面で止まったまま結果が返らず、アプリにも何も出なかった（2026-09-26 お試し）
    import faulthandler
    faulthandler.dump_traceback_later(45, repeat=True, file=sys.stderr)
    with open(job_path, encoding="utf-8") as f:
        cfg = json.load(f)
    out = []
    for job in cfg["jobs"]:
        r = send_one(job, cfg["printer"], cfg["submit"], os.path.dirname(job_path))
        out.append(r)
        if r["結果"] == "🛑":
            # ⚠️ 1通つまずいたら残りは送らない（同じ原因で続けて間違えないため）
            for j in cfg["jobs"][len(out):]:
                out.append({"シート": j["シート"], "結果": "⏭", "中身": "前のFAXで止まったので送っていません"})
            break
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
