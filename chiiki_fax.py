"""
📠 地域手配のFAXを作って送る（`pages/13_📦_地域手配.py` から使う）。Streamlit は import しない。

【人がやっていたこと】
  スプシのFAXシートを開く → ファイル → 印刷 → 次へ → 「…NW-FAX」を選ぶ →
  （お客様1名なのに2枚目に行ったら1枚目だけ）→ 印刷 → 京セラ Network FAX の画面 →
  アドレス帳より選択 → 宛先を選んで追加 → OK → 送信

【ここでやること】
  ① `make_pdfs`：FAXシートを**PDFにして**、お客様の電話番号が1人も載っていないページを外す。
     ⚠️ 全員の電話番号がPDFのどこかに載っているかを確かめ、欠けていたら作らない。
     ⭐ Chromeの印刷画面は通さない（動きが不安定で、空の2枚目が出る原因でもあった）。
  ② 人が完成形を画面で見る（担当者の希望 2026-09-26：「FAXの完成形を見るのを挟む」）。
  ③ `send`：`fax_sender.py` を別の処理で動かし、PDFを NW-FAX のプリンタに渡して、
     京セラの画面でアドレス帳から宛先を選び、送信を押す。
     ⚠️ **宛先の確かめ方**：アドレス帳で選んだ番号が、**そのFAXの用紙に書いてある番号**と同じか。
        違えば送らない（送り先の間違いは取り消せず、お客様の情報が別の所へ届く）。
     ⚠️ 自社の番号（用紙の「申込者」欄）は宛先の候補から外す。

⚠️ プリンタの名前は変わることがある（担当者 2026-09-26）ので、決め打ちしない：
   ドライバ名・プリンタ名に「Network FAX」「NW-FAX」を含むものを探す。設定で選び直せる。
"""
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "取り込みファイル", "地域手配", "FAX")

# FAXシート → 京セラのアドレス帳の名前と、送ってよいFAX番号（設定 `fax_book` で直せる）。
# ⚠️ 番号は3つの突き合わせに使う：アドレス帳で選んだ番号＝この番号＝用紙に書いてある番号。
#    東京都水道局の用紙には23区と多摩の2つのFAX番号が載っているので、「用紙にある」だけでは足りない。
DEFAULT_BOOK = {
    "東京都水道局FAX": {"宛先名": "東京都水道局", "FAX番号": "0357900572"},
    "千葉県水道局FAX": {"宛先名": "千葉県水道局", "FAX番号": "0432723333"},
    "川崎市水道局FAX": {"宛先名": "川崎市水道局", "FAX番号": "0442000041"},
    "横浜市水道局FAX": {"宛先名": "横浜市水道局", "FAX番号": "0458484281"},
    "東京ガスFAX": {"宛先名": "東京ガス", "FAX番号": "0333449363"},
}


def book(cfg: dict) -> dict:
    out = {k: dict(v) for k, v in DEFAULT_BOOK.items()}
    for k, v in (cfg.get("fax_book") or {}).items():
        out.setdefault(k, {}).update({kk: str(vv).strip() for kk, vv in (v or {}).items()})
    return out
# 自社の番号（用紙の申込者欄に載っている。宛先としては使わない）
OWN_NUMBERS = {"0367099238", "0367099237", "08003007310"}
FAX_PRINTER_HINTS = ("network fax", "nw-fax", "nwfax")


def digits(v) -> str:
    return re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", str(v or "")))


def today_dir() -> str:
    d = os.path.join(WORK, time.strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    return d


# ──────────────────────────────────────────
# ① PDFを作る
# ──────────────────────────────────────────
def _export_pdf(sa_json: str, sheet_id: str, gid: int) -> bytes:
    """スプシの1シートをPDFにする（印刷画面と同じ：A3横・幅に合わせる）。"""
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    cr = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive.readonly"])
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=pdf&gid={gid}"
           "&size=A3&portrait=false&fitw=true&gridlines=false&printtitle=false"
           "&sheetnames=false&pagenum=UNDEFINED")
    r = AuthorizedSession(cr).get(url, timeout=180)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"PDFにできませんでした（HTTP {r.status_code}）")
    return r.content


def fax_numbers_on_form(text: str) -> list:
    """用紙に書いてある電話・FAX番号らしいもの（自社の番号は除く）。"""
    t = unicodedata.normalize("NFKC", text or "")
    found = re.findall(r"0\d{1,4}[\-‐－ー\s()（）]*\d{2,4}[\-‐－ー\s()（）]*\d{4}", t)
    out = []
    for f in found:
        d = digits(f)
        if 10 <= len(d) <= 11 and d not in OWN_NUMBERS and d not in out:
            out.append(d)
    return out


def make_pdfs(gc, sa_json: str, sheet_url: str, rows: list) -> list:
    """FAXで送る案件（chiiki.check の rows のうち区分 fax・✅）から、FAXごとのPDFを作る。

    戻り値：[{"シート","件数","pdf","画像":[png…],"ページ":"2/3","欠け":[…],"用紙の番号":[…],"エラー"}]
    """
    import pymupdf
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    gids = {w.title: w.id for w in sh.worksheets()}
    by_sheet = {}
    for r in rows:
        if r.get("区分") != "fax" or r.get("状態") != "✅":
            continue
        where = str(r.get("行き先", "")).replace("📠", "").strip().split(" ")[0]
        by_sheet.setdefault(where, []).append(r)
    # 電話番号は元データから引く（check の行には名前しかない）
    import chiiki
    phones = {}
    for kind_name, spec in chiiki.SRC.items():
        vals = sh.worksheet(spec["tab"]).get_all_values()
        if not vals:
            continue
        head = vals[0]
        if chiiki.ID_COL in head and chiiki.PHONE_COL in head:
            i, p = head.index(chiiki.ID_COL), head.index(chiiki.PHONE_COL)
            for row in vals[1:]:
                if len(row) > max(i, p):
                    phones[chiiki.key_of(kind_name, row[i])] = digits(row[p])
    folder = today_dir()
    out = []
    for sheet, items in by_sheet.items():
        res = {"シート": sheet, "件数": len(items), "pdf": "", "画像": [], "ページ": "",
               "欠け": [], "用紙の番号": [], "エラー": ""}
        try:
            data = _export_pdf(sa_json, sh.id, gids[sheet])
            doc = pymupdf.open(stream=data, filetype="pdf")
            want = {r["key"]: phones.get(r["key"], "") for r in items}
            keep, seen = [], set()
            for i, page in enumerate(doc):
                d = digits(page.get_text())
                hit = [k for k, ph in want.items() if ph and ph in d]
                if hit or i == 0 and not keep:
                    keep.append(i)
                    seen.update(hit)
            res["欠け"] = [f"{r['案件ID']} {r['名前']}" for r in items if r["key"] not in seen]
            _cust = set(v for v in want.values() if v)       # お客様の番号は宛先の候補にしない
            res["用紙の番号"] = [n for n in fax_numbers_on_form(doc[0].get_text()) if n not in _cust]
            res["ページ"] = f"{len(keep)}/{len(doc)}"
            if res["欠け"]:
                res["エラー"] = "PDFに載っていないお客様がいます：" + "、".join(res["欠け"])
            else:
                doc.select(keep)
                path = os.path.join(folder, f"{sheet}.pdf")
                doc.save(path)
                res["pdf"] = path
                for j, page in enumerate(doc):
                    png = os.path.join(folder, f"{sheet}_{j + 1}.png")
                    page.get_pixmap(dpi=110).save(png)
                    res["画像"].append(png)
        except Exception as e:
            res["エラー"] = str(e)[:300]
        out.append(res)
    return out


# ──────────────────────────────────────────
# ③ 送る
# ──────────────────────────────────────────
def fax_printers() -> list:
    """このPCにある、京セラのネットワークFAXらしいプリンタの名前。"""
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        out = []
        for p in win32print.EnumPrinters(flags, None, 2):
            name, drv = str(p.get("pPrinterName", "")), str(p.get("pDriverName", ""))
            if any(h in (name + " " + drv).lower() for h in FAX_PRINTER_HINTS):
                out.append(name)
        return out
    except Exception:
        return []


def all_printers() -> list:
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [str(p[2]) for p in win32print.EnumPrinters(flags)]
    except Exception:
        return []


def pick_printer(cfg: dict):
    """(プリンタ名, 理由)。設定で選んであればそれ、無ければ自動で1台に決まるときだけ。"""
    chosen = str(cfg.get("fax_printer", "") or "").strip()
    if chosen:
        return (chosen, "") if chosen in all_printers() else (
            "", f"設定のFAXプリンタ「{chosen}」がこのPCにありません（⚙️ 設定で選び直してください）")
    cands = fax_printers()
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return "", "このPCに京セラのネットワークFAX（NW-FAX）のプリンタが見つかりません"
    return "", "FAXのプリンタが2台以上あります（⚙️ 設定でどれを使うか選んでください）：" + "、".join(cands)


def plan_jobs(pdfs: list, cfg: dict):
    """(jobs, 止める理由のリスト)。宛先が決められないFAXがあれば、どれも送らない。"""
    bk, jobs, bad = book(cfg), [], []
    for p in pdfs:
        if p.get("エラー") or not p.get("pdf"):
            bad.append(f"{p['シート']}：{p.get('エラー') or 'PDFがありません'}")
            continue
        b = bk.get(p["シート"]) or {}
        name, num = str(b.get("宛先名", "")).strip(), digits(b.get("FAX番号", ""))
        if not (name and num):
            bad.append(f"{p['シート']}：宛先名かFAX番号が決まっていません（⚙️ 設定）")
        elif num not in (p.get("用紙の番号") or []):
            bad.append(f"{p['シート']}：決めてあるFAX番号 {num} が、用紙に書いてありません（用紙：{'、'.join(p.get('用紙の番号') or []) or 'なし'}）")
        else:
            jobs.append({"シート": p["シート"], "pdf": p["pdf"], "宛先名": name, "FAX番号": num})
    return jobs, bad


def send(jobs: list, printer: str, submit: bool, timeout: int = 900) -> list:
    """jobs＝plan_jobs の結果（[{"シート","pdf","宛先名","FAX番号"}]）。`fax_sender.py` を別の処理で動かす。

    submit=False のときは、宛先を選んで確かめたところで「キャンセル」する（送らない）。
    戻り値：[{"シート","結果","中身"}]
    """
    folder = today_dir()
    job_path = os.path.join(folder, "送る.json")
    out_path = os.path.join(folder, "結果.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump({"printer": printer, "submit": bool(submit), "jobs": jobs}, f, ensure_ascii=False)
    if os.path.exists(out_path):
        os.remove(out_path)
    log_path = os.path.join(folder, "fax.log")
    with open(log_path, "w", encoding="utf-8") as lg:
        try:
            subprocess.run([sys.executable, os.path.join(HERE, "fax_sender.py"), job_path, out_path],
                           stdout=lg, stderr=subprocess.STDOUT, timeout=timeout, cwd=HERE)
        except subprocess.TimeoutExpired:
            pass
    try:
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        tail = ""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-600:]
        except Exception:
            pass
        return [{"シート": j.get("シート", ""), "結果": "🛑", "中身": "送る処理が最後まで動きませんでした：" + tail}
                for j in jobs]
