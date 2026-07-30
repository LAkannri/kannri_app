# -*- coding: utf-8 -*-
"""フォームの選択肢（プルダウン<select> / ラジオボタン）を吸い出す小さな道具。

司令室の「数式作成」から呼ばれ、対象フォームを開いて、
- プルダウン（<select>）：ラベルと、選べる選択肢の一覧
- ラジオ：同じグループのラベルと、各肢のラベル
を取得する。ローカル（担当者PC）実行専用。

2つのモード：
  1) 単発（最初のページだけ）:
        python form_inspect.py "<URL>"
     → JSON を標準出力に1行で返す。

  2) 対話（「次へ」で進む複数ページ用。ブラウザを開いたまま合図待ち）:
        python form_inspect.py --interactive "<URL>" "<作業フォルダ>"
     作業フォルダ内のファイルでStreamlitとやり取りする：
        req.txt  … Streamlit が「今のページを取得して」と書く（連番）
        resp.json… このスクリプトが取得結果を書く（{"req": 連番, "controls": [...]}）
        stop.txt … Streamlit が「ブラウザを閉じて」と書く
"""
import sys
import os
import json
import time

# ページ内で選択肢を集める JavaScript。<select> と radio をまとめて拾う。
_EXTRACT_JS = r"""
() => {
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const id = el.getAttribute('id');
    if (id) {
      const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const pl = el.closest('label');
    if (pl && pl.innerText.trim()) return pl.innerText.trim();
    const name = el.getAttribute('name');
    if (name) return name;
    return '';
  };

  // 手順のai_codeに使える安定セレクタ（id優先→name→なし）
  const selFor = (el) => {
    if (el.id) return '#' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id);
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
    return '';
  };

  const controls = [];

  document.querySelectorAll('select').forEach((sel) => {
    const opts = Array.from(sel.options)
      .map(o => (o.label || o.textContent || o.value || '').trim())
      .filter(t => t.length > 0);
    controls.push({ kind: 'select', label: labelFor(sel), options: opts, selector: selFor(sel) });
  });

  const groups = {};
  document.querySelectorAll('input[type="radio"]').forEach((r) => {
    const g = r.getAttribute('name') || '(no-name)';
    if (!groups[g]) groups[g] = [];
    let lab = '';
    if (r.getAttribute('aria-label')) lab = r.getAttribute('aria-label').trim();
    else {
      const id = r.getAttribute('id');
      if (id) {
        const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (l) lab = l.innerText.trim();
      }
      if (!lab) {
        const pl = r.closest('label');
        if (pl) lab = pl.innerText.trim();
      }
    }
    if (!lab) lab = r.getAttribute('value') || '';
    if (lab) groups[g].push(lab);
  });
  Object.keys(groups).forEach((g) => {
    controls.push({ kind: 'radio', label: g, options: groups[g], selector: 'input[name="' + g + '"]' });
  });

  return controls;
}
"""


def _clean(controls):
    """空ラベル・空選択肢を整え、重複を順序保って除去する。"""
    cleaned = []
    for c in controls or []:
        seen, uniq = set(), []
        for o in (c.get("options") or []):
            if o and o not in seen:
                seen.add(o)
                uniq.append(o)
        if uniq:
            cleaned.append({"kind": c.get("kind", ""), "label": c.get("label", ""),
                            "options": uniq, "selector": c.get("selector", "")})
    return cleaned


# 自動化検知(navigator.webdriver 等)を隠す初期化スクリプト。
# これが無いと、サイトによっては「次へ／送信」ボタンを無効化してくる。
_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""


def _new_page(p):
    args = ["--disable-blink-features=AutomationControlled"]
    # できれば本物のChrome（channel='chrome'）で開く＝検知されにくい。無ければ同梱Chromiumに戻す。
    try:
        browser = p.chromium.launch(headless=False, channel="chrome", args=args)
    except Exception:
        browser = p.chromium.launch(headless=False, args=args)
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
    )
    context.add_init_script(_STEALTH_JS)  # ページ読み込み前に検知フラグを隠す
    page = context.new_page()
    page.set_default_timeout(15000)
    return browser, page


def inspect(url: str) -> dict:
    """単発：最初のページの選択肢を取得して返す。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page = _new_page(p)
        page.goto(url)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        controls = page.evaluate(_EXTRACT_JS)
        browser.close()
    return {"ok": True, "controls": _clean(controls)}


def inspect_interactive(url: str, workdir: str, max_seconds: int = 900):
    """対話：ブラウザを開いたまま、Streamlitからの合図で今のページを取得する。

    「次へ」で進む複数ページフォーム用。人が目的ページまで手動で進めてから取得できる。
    """
    from playwright.sync_api import sync_playwright

    req_path = os.path.join(workdir, "req.txt")
    resp_path = os.path.join(workdir, "resp.json")
    stop_path = os.path.join(workdir, "stop.txt")

    def _read_req():
        try:
            with open(req_path, "r", encoding="utf-8") as f:
                return int((f.read() or "0").strip() or "0")
        except Exception:
            return 0

    def _write_resp(rid, controls):
        tmp = resp_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"req": rid, "controls": _clean(controls)}, f, ensure_ascii=False)
        os.replace(tmp, resp_path)  # 半端な読み取りを避けるため原子的に置き換え

    with sync_playwright() as p:
        browser, page = _new_page(p)
        page.goto(url)
        start = time.time()
        last_done = 0
        while time.time() - start < max_seconds:
            if os.path.exists(stop_path):
                break
            # ブラウザが人に閉じられたら終了
            try:
                if page.is_closed():
                    break
            except Exception:
                break
            rid = _read_req()
            if rid and rid != last_done:
                try:
                    controls = page.evaluate(_EXTRACT_JS)
                    _write_resp(rid, controls)
                except Exception as e:
                    _write_resp(rid, [{"kind": "error", "label": str(e), "options": []}])
                last_done = rid
            time.sleep(0.3)
        try:
            browser.close()
        except Exception:
            pass


def main():
    # Windowsでも日本語が化けないよう、標準出力をUTF-8にする
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = sys.argv[1:]
    if args and args[0] == "--interactive":
        if len(args) < 3:
            print(json.dumps({"ok": False, "error": "引数不足（URLと作業フォルダが必要）"}, ensure_ascii=False))
            return
        inspect_interactive(args[1].strip(), args[2].strip())
        return

    if not args or not args[0].strip():
        print(json.dumps({"ok": False, "error": "URLが指定されていません。"}, ensure_ascii=False))
        return
    try:
        result = inspect(args[0].strip())
    except Exception as e:
        result = {"ok": False, "error": str(e)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
