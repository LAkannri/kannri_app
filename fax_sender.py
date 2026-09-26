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
    for b in win.descendants(control_type="Button"):
        if re.search(pattern, b.window_text() or ""):
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


def send_one(job: dict, printer: str, submit: bool, dump_dir: str) -> dict:
    name, num = job["宛先名"], digits(job["FAX番号"])
    res = {"シート": job["シート"], "結果": "🛑", "中身": ""}
    pr = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--print", job["pdf"], printer])
    main = None
    try:
        main = _app_window(MAIN_TITLE, WAIT_DIALOG)
        main.set_focus()
        say("京セラの画面が開きました：", main.window_text())
        _button(main, r"アドレス帳より選択").click_input()
        bk = _app_window(BOOK_TITLE, 30)
        # ⭐ 行は**FAX番号**で探す（名前はアドレス帳の書き方が違う＝「横浜市水道」、
        #    表の行から名前が読めないこともある）。番号がちょうど1行に当たるときだけ選ぶ
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
        _button(bk, r"^OK$").click_input()
        time.sleep(1)
        # 送信設定の画面の宛先リスト：ちょうど1件・番号が一致
        rows = [t for _, t in _rows(main)]
        nums = [digits(x) for t in rows for x in t if len(digits(x)) >= 10]
        if len(rows) != 1 or nums != [num]:
            raise RuntimeError(f"宛先リストが想定と違います（{rows}）。送りません")
        if submit:
            _button(main, r"^送信").click_input()
            res.update({"結果": "✅", "中身": f"{name}（{num}）へ送信しました"})
        else:
            _button(main, r"^キャンセル").click_input()
            res.update({"結果": "🧪", "中身": f"{name}（{num}）を選んで確かめました（お試しなので送っていません）"})
        say(res["中身"])
    except Exception as e:
        res["中身"] = str(e)[:400]
        say("🛑", res["中身"])
        try:
            _dump(_app_window(BOOK_TITLE, 1), os.path.join(dump_dir, f"部品_アドレス帳_{job['シート']}.txt"))
        except Exception:
            pass
        if main is not None:
            _dump(main, os.path.join(dump_dir, f"部品_{job['シート']}.txt"))
            try:
                for w in (_app_window(BOOK_TITLE, 1),):
                    _button(w, r"キャンセル").click_input()
            except Exception:
                pass
            try:
                _button(main, r"^キャンセル").click_input()
                res["中身"] += "（京セラの画面はキャンセルで閉じました）"
            except Exception:
                res["中身"] += "（⚠️ 京セラの画面を閉じられませんでした。画面を確かめてください）"
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
