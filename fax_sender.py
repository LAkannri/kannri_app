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
    # 京セラは A3横（人の印刷設定と同じ）。ブラザーの PC-FAX は A4横（A3を受けない機種がある）
    dm.PaperSize = 9 if "brother" in printer.lower() else 8
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
    _u32.GetParent.restype = wintypes.HWND
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


def _btn_label(h) -> str:
    """ボタンの文字（NFKC・アクセスキーの & と (&S) を外す）。"""
    t = unicodedata.normalize("NFKC", w_text(h))
    return re.sub(r"\(&.\)|&", "", t).strip()


def w_button(dlg, pattern):
    """文字が合うボタン（半角カナも NFKC でそろえて見る）。
    ⚠️ 枠（グループボックス）も Button の仲間なので外す（押しても何も起きない）。"""
    for c in w_children(dlg):
        if w_class(c) != "Button" or (_u32.GetWindowLongW(c, -16) & 0xF) == 7:
            continue
        if re.search(pattern, _btn_label(c)):
            return c
    raise RuntimeError(f"ボタン「{pattern}」が見つかりません")


def w_item(dlg, ctrl_id):
    h = _u32.GetDlgItem(dlg, ctrl_id)
    if not h:
        raise RuntimeError(f"部品 {ctrl_id} が見つかりません")
    return h


def w_press(dlg, ctrl):
    """ボタンが押された合図を画面に置く（返事は待たない）。"""
    # ボタンの親（タブの中の画面のこともある）に送る
    cid = _u32.GetDlgCtrlID(ctrl)
    _u32.PostMessageW(_u32.GetParent(ctrl) or dlg, WM_COMMAND, cid & 0xFFFF, ctrl)


def w_click(ctrl):
    """ボタン自身に「押された」を置く（BM_CLICK・返事は待たない）。"""
    _u32.PostMessageW(ctrl, 0x00F5, 0, 0)


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
            btn = w_button(main, r"^送信$")
            say("「送信」を押します")
            w_press(main, btn)
            if not w_gone(main, 20):
                say("画面が閉じないので、送信ボタンそのものを押し直します")
                w_click(btn)
            # 🛑 ⭐ 送信設定の画面が閉じた＝送った。閉じなければ**送れていない**ので ✅ にしない
            #    （閉じないまま ✅ にして、送っていないFAXの手配日まで入れた・2026-09-29）
            if not w_gone(main, 30):
                raise RuntimeError("「送信」を押しても京セラの画面が閉じませんでした＝送れていません")
            main = None
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


# ──────────────────────────────────────────
# ブラザー PC-FAX（MFC-J4450N・PC-FAX v.3.2）
# 部品の番号は tools/fax_probe.py brother で調べた（2026-10-01）：
#   番号の欄=3038・送信先追加=1002・宛先の一覧=1004（SysTreeView32）・件数=1005（"0/50"）・
#   全削除=3029・キャンセル=3035・送信=3036
# ⭐ アドレス帳は使わない。番号を打って「送信先追加」→ 一覧が**ちょうど1件でその番号**のときだけ送信
# ──────────────────────────────────────────
BROTHER_TITLE = r"^Brother PC-FAX$"
# ⚠️ 前のFAXの画面・エラーの小窓も「Brother PC-FAX」の名前で出る。題名だけでは見分けられないので、
#    中に番号の欄（3038）があるかで「送信の画面かどうか」を決める（`_br_is_send_dialog`）。
BR_ANY_TITLE = r".*(PC-?FAX|ピーシーファクス).*"
BR_NUM, BR_ADD, BR_TREE, BR_COUNT, BR_CLEAR, BR_CANCEL, BR_SEND = 3038, 1002, 1004, 1005, 3029, 3035, 3036
WM_SETTEXT = 0x000C
WM_CLOSE = 0x0010
EN_CHANGE = 0x0300


def _exe_of(h) -> str:
    """その窓を出しているプログラムの名前（分からなければ空）。"""
    try:
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        k32 = ctypes.windll.kernel32
        ph = k32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not ph:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(600)
            n = ctypes.c_ulong(600)
            if k32.QueryFullProcessImageNameW(ph, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(ph)
    except Exception:
        pass
    return ""


def _br_is_send_dialog(h) -> bool:
    """FAXを送る画面（番号の欄と送信ボタンがある）か。エラーの小窓・前の画面と見分ける。"""
    return bool(_u32.GetDlgItem(h, BR_NUM)) and bool(_u32.GetDlgItem(h, BR_SEND))


def _br_message(h) -> str:
    """その窓に出ている文（エラーの小窓の文を読む）。"""
    out = []
    for c in w_children(h):
        if w_class(c) in ("Static", "SysLink"):
            t = (w_text(c) or "").strip()
            if t and t not in out and not t.startswith("&"):
                out.append(t)
    return " / ".join(out[:6])


def _br_size(h):
    """(左, 上, 幅, 高さ)。読めなければ全部 0。"""
    try:
        r = wintypes.RECT()
        _u32.GetWindowRect(h, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        return 0, 0, 0, 0


def _br_blocking(h) -> bool:
    """**人が答えないと先へ進めない画面**か（送信の画面／ボタンのある小窓）。
    ⚠️ 大きさ0・画面の外・最小化・ボタンの無い窓は数えない。ブラザーの PC-FAX は、
    画面に何も出ていなくても `PCFaxTxDial.exe` の窓を持っていることがあり、
    **開いていないのに毎回「閉じてよいですか」になった**（2026-10-07 実機）。"""
    if _u32.IsIconic(h):
        return False
    x, y, w, hh = _br_size(h)
    if w <= 0 or hh <= 0 or x <= -30000 or y <= -30000:
        return False
    if _br_is_send_dialog(h):
        return True
    return any(w_class(c) == "Button" and _u32.IsWindowVisible(c) for c in w_children(h))


def _br_open_windows(blocking_only: bool = False) -> list:
    """いま開いている PC-FAX らしい窓。[(h, 題名, 送信の画面か, 出ている文, プログラム名)]
    blocking_only＝**人が答えないと進めない画面だけ**（`_br_blocking`）。"""
    out = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if _u32.IsWindowVisible(h):
            t = unicodedata.normalize("NFKC", w_text(h) or "")
            if re.match(BR_ANY_TITLE, t, re.I) and not (blocking_only and not _br_blocking(h)):
                out.append((h, t, _br_is_send_dialog(h), _br_message(h), _exe_of(h)))
        return True
    _u32.EnumWindows(cb_t(cb), 0)
    return out


def _br_scene() -> str:
    """いま出ている PC-FAX の画面を、人が読める形で（止まった理由を名指しするため）。"""
    try:
        wins = _br_open_windows()
    except Exception as e:
        return f"（画面を調べられませんでした：{e}）"
    if not wins:
        return "（PC-FAX の画面は出ていません）"
    return "／".join(f"「{t}」{'＝送信の画面' if ok else ''}{('：' + msg) if msg else ''}"
                     f"{('（' + exe + '）') if exe else ''}"
                     f"[{_br_size(h)[2]}x{_br_size(h)[3]}{'' if _br_blocking(h) else '・答える必要なし'}]"
                     for h, t, ok, msg, exe in wins)


def _br_where(h) -> str:
    """その窓が**どこに出ているか**（人が探せるように）。"""
    x, y, w, hh = _br_size(h)
    sw = _u32.GetSystemMetrics(0) or 0        # SM_CXSCREEN
    sh = _u32.GetSystemMetrics(1) or 0
    out = f"位置 {x},{y}・大きさ {w}x{hh}"
    if _u32.IsIconic(h):
        out += "・最小化"
    elif x + w <= 0 or y + hh <= 0 or (sw and x >= sw) or (sh and y >= sh):
        out += "・画面の外（別のモニタ・画面外）"
    else:
        out += "・ほかの窓の後ろ"
    return out


def _br_bring_front(h):
    """その窓を画面の見えるところへ出す（人が答えられるように）。
    ⚠️ 確認の小窓はタスクバーに出ないので、後ろに隠れると**どこにも見当たらない**（2026-10-07 実機）。"""
    try:
        _u32.ShowWindow(h, 9)                                      # SW_RESTORE
        x, y, w, hh = _br_size(h)
        sw, sh = _u32.GetSystemMetrics(0) or 0, _u32.GetSystemMetrics(1) or 0
        off = x + w <= 0 or y + hh <= 0 or (sw and x >= sw) or (sh and y >= sh)
        flags = 0x0040 | (0x0001 if not off else 0)                # SHOWWINDOW（画面の外なら動かす）
        _u32.SetWindowPos(h, -1, 80, 80, 0, 0, flags)              # HWND_TOPMOST
        _u32.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)      # HWND_NOTOPMOST・大きさも位置もそのまま
        _u32.SetForegroundWindow(h)
        _u32.FlashWindow(h, True)
    except Exception:
        pass


def _br_pid(h) -> int:
    """その窓を出しているプログラムの番号（PID）。読めなければ 0。"""
    try:
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _br_kill(h) -> str:
    """⚠️ その窓を出しているプログラム（`PCFaxTxDial.exe`）を終わらせる。
    ⭐ **人がはっきり頼んだときだけ**呼ぶ（書きかけのFAXは消える）。
    どうしても閉じられない画面のために、タスクマネージャーを開かせないための逃げ道
    （2026-10-07 実機で、残った確認の小窓が閉じられず、人が手で終了した）。"""
    pid, exe = _br_pid(h), (_exe_of(h) or "（不明）")
    if not pid:
        return "プログラムの番号（PID）が読めませんでした"
    try:
        p = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:
        return f"終了できませんでした（{exe}・PID {pid}）：{str(e)[:120]}"
    lines = [x.strip() for x in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if x.strip()]
    tail = ("：" + lines[-1][:160]) if lines else ""
    return (f"{'終了しました' if p.returncode == 0 else '終了できませんでした'}"
            f"（{exe}・PID {pid}）{tail}")


def _br_close(h):
    """ブラザーの画面を閉じる（キャンセル → OK → ✕）。⚠️ 確認の小窓が出たら答える
    （答えないと閉じられず、次の実行が「同時に使えません」で止まる）。
    ⭐ **どの閉じ方を試して、どうだったかを必ずログに出す**。前は何も残らなかったので、
    閉じられなかったときに次の手が分からなかった（2026-10-07 実機・人が手で終了した）。"""
    for name, act in (("キャンセルのボタン", lambda: w_press(h, w_item(h, BR_CANCEL))),
                      ("キャンセル（IDCANCEL）", lambda: w_cancel(h)),
                      ("✕（WM_CLOSE）", lambda: _u32.PostMessageW(h, WM_CLOSE, 0, 0))):
        try:
            act()
        except Exception as e:
            say("　閉じ方「", name, "」は使えませんでした：", str(e)[:100])
            continue
        say("　閉じ方「", name, "」を試しました")
        for _ in range(3):
            if w_gone(h, 4):
                say("　→ 閉じました（", name, "）")
                return True
            p, msg = _br_popup(h)
            if not p:
                break
            _br_answer(p, msg)
        say("　→ 閉じませんでした（", name, "）")
    if _u32.IsWindow(h):
        say("　🛑 どの閉じ方でも閉じませんでした（", _br_where(h), "）。",
            _exe_of(h) or "PCFaxTxDial.exe", " を終了すれば消えます",
            "（tools\\fax_windows.bat の k）")
        return False
    return True


def w_tree_texts(h, limit=20):
    """宛先の一覧（SysTreeView32）の文字。読めなければ None。"""
    box = {}

    def run():
        try:
            from pywinauto.controls.common_controls import TreeViewWrapper
            tv = TreeViewWrapper(h)
            out = []
            for r in tv.roots():
                out.append(r.text() or "")
                out += [c.text() or "" for c in r.sub_elements()]
            box["t"] = out
        except Exception as e:
            box["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive() or "err" in box:
        say("⚠️ 宛先の一覧を読めませんでした：", box.get("err", "固まった"))
        return None
    return box["t"]


def _br_count(dlg) -> str:
    return w_text(w_item(dlg, BR_COUNT)).strip()


def _br_count_n(dlg):
    """宛先の件数（`1/50` の左の数）。読めなければ None。
    ⚠️ 文字（`0/50`）で見比べない：空白・表記が変わると「空にできません」で止まる。"""
    m = re.search(r"(\d+)\s*/\s*\d+", unicodedata.normalize("NFKC", _br_count(dlg)))
    return int(m.group(1)) if m else None


# 🗨 ブラザーは「全削除」のあとに確認の小窓を出す。⚠️ 答えないと画面が止まったまま残り、
#    次の実行が「複数のアプリケーションで同時にFAXを使用することはできません」になる（2026-10-07 実機）。
BR_CLEAR_OK_WORDS = ("削除", "クリア")              # 全削除の確認＝答えてよい（何も送らない）
BR_NG_WORDS = ("できません", "エラー", "失敗", "異常", "中止されました")
ID_YES, ID_NO = 6, 7


def _br_popup(dlg=None, wait: float = 0.0):
    """送信の画面ではない『Brother PC-FAX』の小窓（確認・お知らせ）。(h, 出ている文)。"""
    end = time.time() + wait
    while True:
        for h, _t, is_send, msg, _exe in _br_open_windows(blocking_only=True):
            if not is_send and (dlg is None or h != dlg):
                return h, msg
        if time.time() >= end:
            return None, ""
        time.sleep(0.3)


def _br_answer(h, msg: str = "", yes=None) -> bool:
    """小窓に答える。⚠️ 決めていないとき、文に「送信」が入っていたら**いいえ**
    （人が「送る」と言っていない場面で、小窓のはいを押して送ってしまわない）。"""
    if yes is None:
        yes = "送信" not in (msg or "")
    pat = r"^(はい|OK|Yes)$" if yes else r"^(いいえ|No|キャンセル)$"
    say("🗨 小窓：", msg, "→", "はい" if yes else "いいえ")
    try:
        b = w_button(h, pat)
        w_press(h, b)
        if w_gone(h, 10):
            return True
        w_click(b)                    # ボタン自身に「押された」を置く
        if w_gone(h, 5):
            return True
    except Exception:
        pass
    for cid in ((ID_YES, ID_OK) if yes else (ID_NO, ID_CANCEL)):
        _u32.PostMessageW(h, WM_COMMAND, cid, 0)
        if w_gone(h, 5):
            return True
    return not _u32.IsWindow(h)


def _br_after_clear(dlg):
    """「全削除」のあとの確認の小窓に答える（出ないこともある）。"""
    h, msg = _br_popup(dlg, wait=5)
    if not h:
        return
    if not any(w in msg for w in BR_CLEAR_OK_WORDS):
        _br_answer(h, msg, yes=False)              # 知らない小窓は「いいえ」で閉じて止まる
        raise RuntimeError(f"思っていたのと違う小窓が出ました：{msg}")
    if not _br_answer(h, msg):
        raise RuntimeError(f"小窓に答えられませんでした：{msg}")


def send_one_brother(job: dict, printer: str, submit: bool, dump_dir: str) -> dict:
    num = digits(job["FAX番号"])
    res = {"シート": job["シート"], "結果": "🛑", "中身": ""}
    # 🛑 ⚠️ 前のFAXの画面が残っていると、ブラザーは
    #    「複数のアプリケーションで同時にFAXを使用することはできません」で断る（実際に出た）。
    #    そのまま印刷するとFAXが1本余計に積まれるので、**印刷する前に**気づいて止める。
    try:
        before = _br_open_windows(blocking_only=True)
    except Exception as e:
        # ⚠️ ここで落ちると結果が1行も残らない（見張りのために実行そのものを止めない）
        say("（開いている画面を調べられませんでした：", str(e)[:150], "）")
        before = []
    if before and os.environ.get("ENKAN_FAX_FORCE") == "1":
        say("⚠️ 前の画面が残っていますが、人が「それでも進む」と決めたので続けます：", _br_scene())
        before = []
    stop = []
    for h, t, is_send, msg, exe in before:
        where = _br_where(h)
        if not is_send and any(w in msg for w in BR_CLEAR_OK_WORDS) and "送信" not in msg:
            # ⭐ 前の実行が残した「全削除の確認」＝答えて閉じてよい（何も送らない）
            say("前の確認の小窓が残っていたので、答えて閉じます：", msg, "（", where, "）")
            if _br_close(h):
                continue
            stop.append(f"「{t}」：{msg}（{where}・{exe}・閉じられませんでした）")
        elif is_send:
            stop.append(f"「{t}」＝前の送信の画面（{where}・{exe}）")
        else:
            stop.append(f"「{t}」：{msg or '（文なし）'}（{where}・{exe}）")
    if stop:
        for h, *_r in before:
            if _u32.IsWindow(h):
                _br_bring_front(h)      # ⭐ 小窓はタスクバーに出ない＝探せないので、前に出す
        res["中身"] = ("前のFAXの画面が開いたままです（ブラザーは同時に1つしか使えません）。"
                       "見つけやすいように画面のいちばん前に出しました。閉じてから、もう一度試してください"
                       "（消えないときは tools\\fax_windows.bat を動かして k＝PC-FAX のプログラムを終了）："
                       + "／".join(stop))
        say("🛑", res["中身"])
        return res
    pr = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--print", job["pdf"], printer])
    dlg = None
    try:
        dlg = w_top(BROTHER_TITLE, WAIT_DIALOG)
        say("ブラザーの画面が開きました")
        time.sleep(1)
        # ⚠️ エラーの小窓も同じ題名で出る。番号の欄が無ければ送信の画面ではない＝その文を名指しする
        if not _br_is_send_dialog(dlg):
            msg = _br_message(dlg)
            raise RuntimeError(f"FAXの送信画面ではなく、お知らせの小窓が出ました：{msg or '（文を読めません）'}")
        n = _br_count_n(dlg)
        if n is None:
            raise RuntimeError(f"宛先の件数を読めません（{_br_count(dlg)!r}）。送りません")
        if n:
            say("前の宛先が残っているので「全削除」を押します：", _br_count(dlg))
            w_press(dlg, w_item(dlg, BR_CLEAR))
            _br_after_clear(dlg)          # ⚠️ 確認の小窓が出る。答えないと止まったまま残る
            for _ in range(20):
                time.sleep(0.25)
                if _br_count_n(dlg) == 0:
                    break
        if _br_count_n(dlg) != 0:
            raise RuntimeError(f"宛先の一覧を空にできません（{_br_count(dlg)}）。送りません")
        box = w_item(dlg, BR_NUM)
        buf = ctypes.create_unicode_buffer(num)
        _send_timeout(box, WM_SETTEXT, 0, ctypes.addressof(buf))
        time.sleep(0.3)
        if digits(w_text(box)) != num:
            raise RuntimeError(f"番号の欄に {num} を入れられません（{w_text(box)!r}）。送りません")
        # ⚠️ 文字を置くだけでは「欄が変わった」が画面に伝わらず、「送信先追加」が押せないままのことがある。
        #    変わったことだけを知らせる（EN_CHANGE。番号は上で入れたものがそのまま読める）
        _u32.PostMessageW(_u32.GetParent(box) or dlg, WM_COMMAND,
                          (_u32.GetDlgCtrlID(box) & 0xFFFF) | (EN_CHANGE << 16), box)
        time.sleep(0.3)
        add = w_item(dlg, BR_ADD)
        if not _u32.IsWindowEnabled(add):
            say("⚠️ 「送信先追加」は押せない状態に見えます（それでも押してみます）")
        say("番号を入れました：", num, "→「送信先追加」を押します")
        w_press(dlg, add)
        for _ in range(20):
            time.sleep(0.25)
            if _br_count_n(dlg):
                break
        cnt = _br_count(dlg)
        texts = w_tree_texts(w_item(dlg, BR_TREE))
        say("宛先の一覧：", cnt, texts)
        # ⭐ 件数がちょうど1件・一覧に出ている番号がその番号（違う宛先なら送らない）
        if _br_count_n(dlg) != 1:
            raise RuntimeError(f"宛先が1件になりません（{cnt}）。送りません")
        if texts is None or [digits(t) for t in texts if digits(t)] != [num]:
            raise RuntimeError(f"宛先の一覧が想定と違います（{texts}）。送りません")
        if submit:
            say("「送信」を押します")
            w_press(dlg, w_item(dlg, BR_SEND))
            # 🗨 お知らせ・確認の小窓が出たら、文を読んで答える（エラーらしければ送れていない）
            h, msg = _br_popup(dlg, wait=5)
            if h:
                # ⭐ ここは人が「本当に送る」と決めたあと＝送信の確認には「はい」と答える
                if any(w in msg for w in BR_NG_WORDS):
                    _br_answer(h, msg, yes=True)               # OKで閉じるだけ
                    raise RuntimeError(f"送信できませんでした：{msg}")
                if not _br_answer(h, msg, yes=True):
                    raise RuntimeError(f"送信の確認の小窓に答えられませんでした：{msg}")
            # 🛑 画面が閉じた＝送信に渡した。閉じなければ送れていない
            if not w_gone(dlg, 30):
                raise RuntimeError("「送信」を押してもブラザーの画面が閉じませんでした＝送れていません")
            dlg = None
            res.update({"結果": "✅", "中身": f"{job.get('宛先名', '')}（{num}）へ送信しました"})
        else:
            say("お試しなので「キャンセル」を押します")
            res.update({"結果": "🧪", "中身": f"{num} を宛先に入れて確かめました（お試しなので送っていません）"})
            if not _br_close(dlg):
                res["中身"] += "（⚠️ ブラザーの画面が閉じていません。画面を確かめてください）"
            dlg = None
        say(res["中身"])
    except Exception as e:
        res["中身"] = str(e)[:400]
        say("🛑", res["中身"])
        if dlg:
            w_dump(dlg, os.path.join(dump_dir, f"部品_{job['シート']}.txt"))
            # ⚠️ 片付けで落ちない（お知らせの小窓にはキャンセルのボタンが無い）。
            #    閉じられないと、次の実行が「同時に使えません」で止まる
            res["中身"] += ("（ブラザーの画面は閉じました）" if _br_close(dlg)
                           else "（⚠️ ブラザーの画面を閉じられませんでした。手で閉じてください）")
        else:
            res["中身"] += "（いまの画面：" + _br_scene() + "）"
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
        one = send_one_brother if "brother" in cfg["printer"].lower() else send_one
        r = one(job, cfg["printer"], cfg["submit"], os.path.dirname(job_path))
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
