"""
📠 FAXが本当に送れるかを、ダミーの1枚で試す（ブラザー PC-FAX／京セラ NW-FAX）。

  python tools\\fax_test.py --to <FAX番号> [--name 宛先の名前] [--printer "プリンタ名"] [--submit]

ふだんは tools\\fax_test.bat をダブルクリックする（番号を聞いて、まずお試し → 確かめてから送る）。

⭐ 本番とまったく同じ道を通る（`chiiki_fax.send` → `fax_sender.py`）。違うのは
   「スプシのFAXシートではなく、ダミーの1枚を送る」ことだけ。
⚠️ **`--submit` を付けない限り送らない**：番号を入れて宛先が1件になったことを確かめ、
   **送信ボタンの手前でキャンセル**する。
⚠️ 送り先の番号・名前はコードに書かない（このリポジトリは公開）。毎回引数で渡す。
⚠️ 作業フォルダは本番と分ける（`…/FAX/<日付>/テスト`）＝本番の `結果.json` / `fax.log` を上書きしない。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import chiiki_fax  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(*a):
    print(*a, flush=True)


# ──────────────────────────────────────────
# ダミーのFAX（1枚・A4横。本番のFAXシートと同じ向き）
# ──────────────────────────────────────────
def make_dummy_pdf(folder: str, name: str, num: str) -> tuple:
    """(pdfのパス, 見本のpngのパス)。⚠️ 用紙に送り先の番号を刷る
    （本番と同じ『用紙に書いてある番号と照らし合わせる』を、この試験でも通すため）。"""
    import pymupdf
    os.makedirs(folder, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)      # A4横
    page.draw_rect(pymupdf.Rect(28, 28, 814, 567), color=(0, 0, 0), width=1.2)
    lines = [
        (30, "【テスト送信】エンカンAI"),
        (18, "FAXが送れるかを確かめるための試験です。お手数ですが、ご対応は不要です。"),
        (0, ""),
        (18, f"宛先　　：{name}（{num}）"),
        (18, f"送信日時：{time.strftime('%Y年%m月%d日 %H時%M分')}"),
        (18, f"送信元PC：{chiiki_fax.pc()}"),
        (0, ""),
        (14, "TEST FAX - no action needed."),
        (14, f"FAX {num}"),
    ]
    y = 90
    for size, text in lines:
        if text:
            try:
                page.insert_text((50, y), text, fontname="japan", fontsize=size)
            except Exception:
                # 日本語の字が出せないPCでも、番号だけは必ず刷る
                page.insert_text((50, y), text.encode("ascii", "replace").decode(), fontsize=size)
        y += (size or 14) + 16
    pdf = os.path.join(folder, "テスト送信.pdf")
    doc.save(pdf)
    png = os.path.join(folder, "テスト送信.png")
    doc[0].get_pixmap(dpi=110).save(png)
    # ⭐ 本番と同じ確かめ方で、用紙に番号が刷れているかを見る（読めない紙を送らない）
    found = chiiki_fax.fax_numbers_on_form(doc[0].get_text())
    if num not in found:
        raise RuntimeError(f"ダミーの用紙に番号 {num} が刷れていません（読めた番号：{found or 'なし'}）")
    return pdf, png


# ──────────────────────────────────────────
# 設定（Supabase）は読めたら使う＝このPCで選んであるFAXプリンタ
# ──────────────────────────────────────────
def load_cfg() -> dict:
    try:
        import tomllib
        with open(os.path.join(HERE, ".streamlit", "secrets.toml"), "rb") as f:
            sec = tomllib.load(f)
        from supabase import create_client
        sb = create_client(sec["SUPABASE_URL"], sec["SUPABASE_KEY"])
        res = sb.table("merchants").select("config_json").eq("id", "__chiiki__").execute()
        return (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception as e:
        say(f"（設定を読めませんでした：{str(e)[:120]}　→ プリンタはこのPCから探します）")
        return {}


def choose_printer(given: str) -> str:
    if given:
        return given
    printer, why = chiiki_fax.pick_printer(load_cfg())
    if printer:
        return printer
    say("⚠️ " + why)
    cands = chiiki_fax.fax_printers() or chiiki_fax.all_printers()
    if not cands:
        raise RuntimeError("このPCにプリンタが見つかりません")
    for i, p in enumerate(cands, 1):
        say(f"  {i}. {p}")
    try:
        ans = input("使うプリンタの番号を入れてください（やめるときは Enter）：").strip()
    except EOFError:
        ans = ""
    if not ans.isdigit() or not (1 <= int(ans) <= len(cands)):
        raise RuntimeError("プリンタを選びませんでした")
    return cands[int(ans) - 1]


def show_windows() -> int:
    """いま開いている PC-FAX の画面を調べて、**画面のいちばん前に出す**（送らない・閉じない）。
    ⚠️ 確認の小窓はタスクバーに出ないので、後ろに隠れると「開いていると言われても見当たらない」になる。"""
    import fax_sender
    try:
        wins = fax_sender._br_open_windows()
    except Exception as e:
        say(f"🛑 調べられませんでした：{str(e)[:200]}")
        return 1
    if not wins:
        say("✅ PC-FAX の画面は1つも開いていません。")
        return 0
    say(f"PC-FAX の画面が {len(wins)} つ見つかりました：")
    for h, t, ok, msg, exe in wins:
        need = fax_sender._br_blocking(h)
        say(f"  ・{t}{'（送信の画面）' if ok else ''}{('：' + msg) if msg else ''}")
        say(f"　　{fax_sender._br_where(h)}／{exe}／"
            f"{'⚠️ 答えないと次のFAXが送れません' if need else '答える必要なし（アプリが持っているだけ）'}")
        fax_sender._br_bring_front(h)
    say("")
    say("↑ 画面のいちばん前に出しました。確認の小窓は「いいえ」で閉じてください（何も送りません）。")
    try:
        ans = input("どうしますか？（y＝閉じる／k＝PC-FAX のプログラムを終了する／"
                    "そのほか＝自分で閉じる）：").strip().lower()
    except EOFError:
        ans = ""
    if ans == "k":
        kill_windows(wins)
    elif ans == "y":
        for h, t, _ok, msg, _exe in wins:
            say("閉じます：", t, msg, "→", "閉じました" if fax_sender._br_close(h) else "閉じられませんでした")
    else:
        return 0
    left = fax_sender._br_open_windows(blocking_only=True)
    if left and ans != "k":
        say("⚠️ まだ残っています：" + fax_sender._br_scene())
        say("　もう一度 fax_windows.bat を動かして k を選べば、プログラムごと終われます。")
    elif left:
        say("⚠️ まだ残っています：" + fax_sender._br_scene())
    else:
        say("✅ 閉じました。")
    return 0 if not left else 1


def kill_windows(wins) -> None:
    """⚠️ PC-FAX のプログラム（`PCFaxTxDial.exe`）を終わらせる。
    ⭐ **人が k と答えたときだけ**。書きかけのFAXは消えるので、先にそう伝える。
    どうしても閉じられない画面のために、タスクマネージャーを開かせないための逃げ道。"""
    import fax_sender
    say("")
    say("⚠️ PC-FAX のプログラムを終了します（書きかけのFAXがあれば消えます・送信済みの分は消えません）。")
    done = set()
    for h, t, _ok, _msg, _exe in wins:
        pid = fax_sender._br_pid(h)
        if not pid or pid in done:
            continue
        done.add(pid)
        say("　", t, "→", fax_sender._br_kill(h))
    if not done:
        say("　（終了するプログラムが分かりませんでした）")


def clear_leftovers(printer: str):
    """⚠️ ブラザーは同時に1つしかFAXを使えない（「複数のアプリケーションで同時にFAXを使用することは
    できません」）。**人が答えないと進めない画面**（送信の画面・ボタンのある小窓）が残っていたら、
    人に見せて、頼まれたときだけ閉じる。
    ⚠️ 画面に何も出ていないのに `PCFaxTxDial.exe` の窓が残っていることがあるので、
    大きさ0・画面の外・ボタンの無い窓は数えない（`fax_sender._br_blocking`）。"""
    if not chiiki_fax.is_brother(printer):
        return
    import fax_sender

    def now():
        try:
            return fax_sender._br_open_windows(blocking_only=True)
        except Exception as e:
            say(f"（開いている画面を調べられませんでした：{str(e)[:120]}）")
            return []

    def show(wins):
        for h, t, ok, msg, exe in wins:
            x, y, w, hh = fax_sender._br_size(h)
            say(f"  ・{t}{'（送信の画面）' if ok else ''}{('：' + msg) if msg else ''}"
                f"{('　' + exe) if exe else ''}　[{w}x{hh}]")

    wins = now()
    if not wins:
        return
    say("⚠️ 答える必要のある PC-FAX の画面が開いています（ブラザーは同時に1つしか使えません）：")
    show(wins)
    say("　前に止まったときの画面です（確認の小窓は「いいえ」で閉じます＝何も送りません）。")
    try:
        ans = input("どうしますか？（y＝閉じる／k＝PC-FAX のプログラムを終了する／"
                    "そのほか＝やめる）：").strip().lower()
    except EOFError:
        ans = ""
    if ans == "k":
        kill_windows(wins)
        time.sleep(2)
        if not now():
            say("✅ 前の画面は無くなりました。続けます。")
            return
        say("⚠️ まだ残っています：" + fax_sender._br_scene())
        ans = "y"
    if ans != "y":
        raise SystemExit("🛑 前の画面が開いたままなので、やめました（手で閉じてから、もう一度）")
    for _round in range(4):          # 小窓 → その後ろの送信の画面、と続くので繰り返す
        wins = now()
        if not wins:
            say("✅ 前の画面を閉じました。続けます。")
            return
        for h, t, _ok, msg, _exe in wins:
            say("閉じます：", t, msg, "→", "閉じました" if fax_sender._br_close(h) else "閉じられませんでした")
        time.sleep(1)
    # ⭐ 閉じられないときでも、人が決めたら進む（本当に邪魔なら、ブラザーが断ってその文を出す）
    say("")
    say("⚠️ まだ閉じられていない画面があります：")
    show(now())
    try:
        ans = input("どうしますか？（k＝PC-FAX のプログラムを終了して進む（おすすめ）／"
                    "y＝そのまま進む／そのほか＝やめる）：").strip().lower()
    except EOFError:
        ans = ""
    if ans == "k":
        kill_windows(now())
        time.sleep(2)
        if not now():
            say("✅ 前の画面は無くなりました。続けます。")
            return
        say("⚠️ まだ残っています：" + fax_sender._br_scene())
    elif ans != "y":
        raise SystemExit("🛑 やめました")
    os.environ["ENKAN_FAX_FORCE"] = "1"      # 送る処理にも伝える（止めずに進む）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", help="送り先のFAX番号")
    ap.add_argument("--show", action="store_true",
                    help="いま開いている PC-FAX の画面を調べて、画面の前に出す（FAXは送らない）")
    ap.add_argument("--name", default="テスト宛先", help="宛先の名前（用紙に刷るだけ）")
    ap.add_argument("--printer", default="", help="使うFAXプリンタ（省略＝設定／自動）")
    ap.add_argument("--submit", action="store_true", help="⚠️ 本当に送る（付けないと送信の手前でキャンセル）")
    a = ap.parse_args()
    if a.show:
        return show_windows()
    if not a.to:
        raise SystemExit("🛑 --to（送り先のFAX番号）を入れてください")
    num = chiiki_fax.digits(a.to)
    if not (10 <= len(num) <= 11):
        raise SystemExit(f"🛑 FAX番号が10〜11桁ではありません（{a.to!r} → {num!r}）")
    if num in chiiki_fax.OWN_NUMBERS:
        raise SystemExit(f"🛑 {num} は自社の番号です")

    folder = os.path.join(chiiki_fax.today_dir(), "テスト")
    os.makedirs(folder, exist_ok=True)
    printer = choose_printer(a.printer.strip())
    kind = "ブラザー PC-FAX" if chiiki_fax.is_brother(printer) else "京セラ NW-FAX"
    say(f"使うプリンタ：{printer}（{kind}の画面を押します）")
    if not chiiki_fax.is_brother(printer):
        say("⚠️ 京セラはアドレス帳から宛先を選ぶので、この番号がアドレス帳に入っていないと止まります")
    clear_leftovers(printer)
    pdf, png = make_dummy_pdf(folder, a.name, num)
    say(f"ダミーのFAXを作りました：{pdf}")
    if not a.submit:
        try:
            os.startfile(png)      # 見本を開く（人が中身を見てから送るため）
        except Exception:
            say(f"見本：{png}")

    job = {"シート": "テスト送信", "pdf": pdf, "宛先名": a.name, "FAX番号": num}
    say("")
    say(f"▶ {'🚨 本当に送ります' if a.submit else '🧪 お試し（送信ボタンの手前でキャンセルします）'}"
        f"　宛先 {a.name}（{num}）")
    say("　FAXの画面が出ます。終わるまで、マウス・キーボードに触らないでください。")
    res = chiiki_fax.send([job], printer, submit=a.submit, folder=folder)
    say("")
    for r in res:
        say(f"{r['結果']} {r['シート']}：{r['中身']}")
    say("")
    say(f"くわしい記録：{os.path.join(folder, 'fax.log')}")
    ok = all(r["結果"] in ("✅", "🧪") for r in res) and bool(res)
    if ok and not a.submit:
        say("🧪 送信ボタンの手前まで通りました（送っていません）。本当に送るときは --submit を付けます。")
    elif ok:
        say("✅ 送信まで通りました。相手に届いたかを確かめてください。")
    else:
        say("🛑 止まりました。上の理由と、記録（fax.log）・部品_*.txt を見てください。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
