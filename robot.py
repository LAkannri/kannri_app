import sys
import os
import io
import csv
import json
import hashlib
import tomllib
import time
import re
import unicodedata
import urllib.request
import urllib.parse
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

# Windows既定コンソール(cp932)では print の絵文字が UnicodeEncodeError で落ちる。
# 標準出力/エラーをUTF-8にして、ログ出力でクラッシュしないようにする。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==========================================
# 1. 接続キーを読み込む（クラウド=環境変数 / ローカル=secrets.toml）
# ==========================================
def load_secrets() -> dict:
    """GitHub Actions などクラウドでは環境変数を優先し、ローカルでは secrets.toml から読む。"""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return {
            "SUPABASE_URL": os.environ["SUPABASE_URL"],
            "SUPABASE_KEY": os.environ["SUPABASE_KEY"],
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),  # ※クラウドrun(robot.py)では未使用。手順生成はStreamlit側のみ。
            "SLACK_WEBHOOK_URL": os.environ.get("SLACK_WEBHOOK_URL", ""),  # 任意：完了/失敗のSlack通知（未設定なら通知しない）
            "GOOGLE_SERVICE_ACCOUNT_JSON": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),  # 任意：認証付きスプシ読み込み用（未設定なら従来の匿名リンク共有方式）
        }
    try:
        with open(".streamlit/secrets.toml", "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            "接続キーが見つかりません。クラウドでは環境変数 SUPABASE_URL / SUPABASE_KEY を、"
            "ローカルでは .streamlit/secrets.toml を用意してください。"
        )

secrets = load_secrets()
supabase: Client = create_client(secrets["SUPABASE_URL"], secrets["SUPABASE_KEY"])

# ==========================================
# 🖥️ 実行モードと証跡（クラウドは自動 headless）
# ==========================================
def is_headless() -> bool:
    """ENKAN_HEADLESS が指定されればそれに従い、無ければ CI 環境で自動的に headless にする。"""
    val = os.environ.get("ENKAN_HEADLESS")
    if val is not None:
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(os.environ.get("CI"))

ARTIFACTS_DIR = "artifacts"
# 処理済みキーの保持上限（二重申請防止の砦。超過時は古い順に切り捨て、警告を出す）
PROCESSED_KEYS_LIMIT = 20000

def _save_screenshot(page, project_name: str, tag: str = "error"):
    """失敗・中止時などにスクショを残す。クラウドでは目視できないため証跡として重要。"""
    try:
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", str(project_name))[:60]
        path = os.path.join(ARTIFACTS_DIR, f"{safe}_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png")
        page.screenshot(path=path, full_page=True)
        print(f"　📸 スクリーンショットを保存しました: {path}")
        return path
    except Exception as e:
        print(f"　⚠️ スクショ保存に失敗: {e}")
        return None

# CAPTCHA / ボット検知の手掛かり（headless はとくに当たりやすい）
# ⚠️ 「recaptcha」等の裏側スコア型バッジは多くのサイトに常時埋め込まれており、
#    実際にパズルが出ていなくても単語一致で誤検知するため、ここには含めない。
#    実際にユーザーへ壁として提示される、確実な文言のみを対象にする。
_BLOCK_HINTS = ["私はロボットではありません",
                "ロボットではありません", "are you a robot", "cf-challenge", "turnstile"]

def _parse_date(text: str):
    """「2026/09/01」「2026年9月1日」「2026-09-01」などから (年, 月, 日) を取り出す。
    読み取れなければ (None, None, None)。"""
    s = str(text or "").strip()
    m = re.search(r"(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", s)
    if not m:
        m2 = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", re.sub(r"\D", "", s))
        if not m2:
            return (None, None, None)
        m = m2
    y, mo, d = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return (None, None, None)
    return (y, mo, d)


def _date_input(page, target_desc, ai_code):
    """日付を入れる欄を探す。録画の呪文があればそこから、無ければ欄の名前で。"""
    clean = str(target_desc or "").replace("「", "").replace("」", "").replace("*", "").strip()
    clean = re.sub(r"\s*必須\s*$", "", clean)
    cands = []
    # 録画の呪文に書かれたセレクタをそのまま使えると、いちばん確実
    if ai_code:
        m = re.search(r'page\.(get_by_\w+|locator)\((.*?)\)(?=\s*\.)', str(ai_code), re.S)
        if m:
            try:
                cands.append(eval(f"page.{m.group(1)}({m.group(2)})", {"page": page}))
            except Exception:
                pass
    if clean:
        cands += [page.get_by_label(clean, exact=False),
                  page.get_by_placeholder(clean, exact=False),
                  page.locator(f'input[name*="{clean}"]')]
    for c in cands:
        try:
            loc = c.first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _set_date_field(page, target_desc, y, mo, d, ai_code="") -> bool:
    """日付の欄に値を入れる。

    ① まず文字として打ってみる（多くのサイトはこれで入る）
    ② 打てない（読み取り専用）ときは、カレンダーを開いて
       年月を合わせ、日にちのマスを押す
    """
    loc = _date_input(page, target_desc, ai_code)
    if loc is None:
        return False

    # ① 文字で入れる。サイトによって書き方が違うので、通る形を順に試す
    for text in (f"{y}/{mo:02d}/{d:02d}", f"{y}-{mo:02d}-{d:02d}",
                 f"{y}年{mo}月{d}日", f"{y}{mo:02d}{d:02d}"):
        try:
            loc.click(timeout=3000)
            loc.fill("", timeout=2000)
            loc.type(text, delay=40, timeout=5000)
            page.keyboard.press("Escape")          # 開いたカレンダーを閉じる
            got = re.sub(r"\D", "", loc.input_value(timeout=2000) or "")
            if got and got[:4] == str(y) and int(got[4:6] or 0) == mo:
                return True
        except Exception:
            continue

    # ② カレンダーを操作する。年月の見出しを見ながら、次の月へ進める
    try:
        loc.click(timeout=3000)
        for _ in range(24):
            head = ""
            try:
                head = page.locator("[class*=calendar], [class*=datepicker], [role=dialog]").first \
                           .inner_text(timeout=1500)[:120]
            except Exception:
                pass
            hy = re.search(r"(20\d{2})", head)
            hm = re.search(r"(\d{1,2})\s*月", head)
            if hy and hm and int(hy.group(1)) == y and int(hm.group(1)) == mo:
                break
            # 「次の月」に進む。矢印はサイトによって書き方が違うので順に試す
            moved = False
            for sel in ("[aria-label*=次], [aria-label*=Next], [class*=next]",
                        "button:has-text('›')", "button:has-text('>')"):
                try:
                    page.locator(sel).first.click(timeout=1000)
                    moved = True
                    break
                except Exception:
                    continue
            if not moved:
                break
        page.get_by_text(str(d), exact=True).first.click(timeout=3000)
        got = re.sub(r"\D", "", loc.input_value(timeout=2000) or "")
        return bool(got) and got[:4] == str(y)
    except Exception:
        return False


# 📄 ダウンロードのリンクに使われがちな拡張子
_FILE_LINK = re.compile(r"\.(csv|xlsx?|zip|tsv|txt|pdf)(\?|$)", re.IGNORECASE)


def _stamp_in_name(text: str) -> str:
    """ファイル名に入っている日時を、比べられる形（20260824033218）で取り出す。

    例：【LINES】ソフトバンク＃3_20260824T032849+0900.csv → 20260824032849
    見つからなければ空。
    """
    s = str(text or "")
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})[T_\-\s]?(\d{2})?[:]?(\d{2})?[:]?(\d{2})?", s)
    if not m:
        return ""
    return "".join(g or "0" for g in m.groups())


def _newest_download_link(page):
    """『書き出し状況の一覧』のような表から、いちばん新しいファイルのリンクを返す。

    ファイル名は毎回変わるので、名前では指定できない。
    並び順に頼ると（古い順に並ぶ画面で）取り違えるため、
    ファイル名に入っている日時を読んで、いちばん大きいものを選ぶ。
    日時が読み取れないときだけ、上にあるものを使う。
    戻り値：(リンク, 表示されている文字)。見つからなければ (None, "")。
    """
    cands = []                      # (日時, 並び順, リンク, 文字)
    for sel in ('a[href$=".csv"]', 'a[href*=".csv"]', 'a[href$=".xlsx"]',
                'a[href$=".zip"]', 'table a', 'a'):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 40)):
                item = loc.nth(i)
                try:
                    txt = (item.inner_text(timeout=800) or "").strip()
                except Exception:
                    txt = ""
                href = item.get_attribute("href") or ""
                if _FILE_LINK.search(txt) or _FILE_LINK.search(href):
                    label = txt or href
                    cands.append((_stamp_in_name(label), i, item, label))
        except Exception:
            continue
        if cands:
            break                   # 見つかった時点で、その探し方の結果を使う
    if not cands:
        return None, ""
    stamped = [c for c in cands if c[0]]
    if stamped:
        best = max(stamped, key=lambda c: c[0])          # 日時がいちばん新しいもの
    else:
        best = min(cands, key=lambda c: c[1])            # 読み取れないので一番上
    if len(cands) > 1:
        print(f"　🔎 候補 {len(cands)}件から選びました"
              + ("（ファイル名の日時で判断）" if stamped else "（一番上を使用）"))
    return best[2], best[3]


def _is_placeholder_option(text: str) -> bool:
    """プルダウンの「選んでいない状態」を表す選択肢か（-None- / 選択してください 等）。
    お試し実行で代わりに選ぶとき、こういう“空の選択肢”を選んでも意味がないため。"""
    t = str(text or "").strip()
    if not t:
        return True
    if t.strip("-—–_ 　") == "" or t.lower() in ("none", "-none-", "null", "--"):
        return True
    return any(w in t for w in ("選択してください", "選んでください", "指定なし", "未選択", "以下から"))


def _looks_blocked(page) -> bool:
    """画面が CAPTCHA / ボット検知の壁になっていそうか、ざっくり判定する。"""
    try:
        html = (page.content() or "").lower()
    except Exception:
        return False
    return any(hint.lower() in html for hint in _BLOCK_HINTS)

_CAPTCHA_FRAMES = (
    'iframe[src*="/recaptcha/api2/bframe"]',
    'iframe[src*="/recaptcha/enterprise/bframe"]',
    'iframe[src*="hcaptcha.com"]',
    'iframe[title*="reCAPTCHA による確認"]',
    'iframe[title*="recaptcha challenge"]',
)

def _captcha_challenge_visible(page) -> bool:
    """画像パズル（「消火栓を選べ」等）が“実際に画面に出ている”かを見る。
    右下のバッジや、常に埋め込まれている非表示フレームには反応しない
    （それらで毎回止まると、正常な申請までできなくなるため大きさも確認する）。"""
    for sel in _CAPTCHA_FRAMES:
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible():
                continue
            box = loc.bounding_box()
            if box and box.get("width", 0) > 80 and box.get("height", 0) > 80:
                return True
        except Exception:
            continue
    return False

def _wait_for_captcha_cleared(page, headless, project_name, timeout_sec=300) -> bool:
    """画像パズルが出たら、人が解き終わるのを待つ（画面が見えているときだけ）。
    自動突破はしない。headless（無人）では待っても誰も解けないので、すぐ中止する。
    戻り値：True＝解決して続行できる／False＝中止すべき。"""
    _save_screenshot(page, project_name, "captcha")
    if headless:
        print("🛑 画像パズル（CAPTCHA）が表示されました。無人実行では解けないため中止します。")
        return False
    print(f"　🧩 画像パズルが表示されました。ブラウザで解いてください（最大{timeout_sec // 60}分待ちます）...")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(2)
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        if not _captcha_challenge_visible(page):
            print("　✅ パズルが解けたようです。処理を再開します。")
            time.sleep(1)
            return True
    print("🛑 画像パズルが解かれないまま時間切れになりました。")
    return False

# ==========================================
# 🔧 条件判定エンジン（設定駆動・汎用ルールエンジン）
# ==========================================
# 演算子の一覧（UIのプルダウンとそろえる）
OPERATORS = {
    "eq": "一致する",
    "ne": "一致しない",
    "contains": "含む",
    "not_contains": "含まない",
    "empty": "空である",
    "not_empty": "空でない",
    "gt": "より大きい",
    "gte": "以上",
    "lt": "より小さい",
    "lte": "以下",
    "in": "いずれかと一致（カンマ区切り）",
}

def _to_number(s):
    """数値化できれば float、できなければ None を返す。"""
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None

def _eval_single_rule(rule: dict, customer_data: dict) -> bool:
    """1つの条件（列・演算子・値）を評価する。"""
    col = rule.get("col", "")
    op = rule.get("op", "eq")
    expected = str(rule.get("value", "")).strip()
    actual = str(customer_data.get(col, "")).strip()

    if op == "empty":        return actual == ""
    if op == "not_empty":    return actual != ""
    if op == "eq":           return actual == expected
    if op == "ne":           return actual != expected
    if op == "contains":     return expected in actual
    if op == "not_contains": return expected not in actual
    if op == "in":           return actual in [v.strip() for v in expected.split(",")]

    # 数値比較（gt/gte/lt/lte）
    a, e = _to_number(actual), _to_number(expected)
    if a is None or e is None:
        return False
    if op == "gt":  return a > e
    if op == "gte": return a >= e
    if op == "lt":  return a < e
    if op == "lte": return a <= e
    return False

# 🚀 「送信（申請）ステップ」の目印。これらが「いつ」に入っている手順は本番でのみ実行する。
#    （録画は申請ボタンの“直前”まで＝AI手順に送信は含まれない。最後の一押しだけ別管理する。）
SUBMIT_MARKERS = {
    "送信", "申請", "送信する", "申請する",
    "送信（本番のみ）", "申請（本番のみ）", "送信(本番のみ)", "申請(本番のみ)",
    "送信時", "申請時", "最後に送信",
}

def is_submit_marker(condition_name) -> bool:
    """この手順が『送信（申請）ステップ』か（本番でのみ実行する一押し）。"""
    return str(condition_name or "").strip() in SUBMIT_MARKERS

def evaluate_condition(condition_name: str, customer_data: dict, conditions_config=None) -> bool:
    """
    手順の「いつ」に指定されたルール名を、設定（conditions_config）に基づいて評価する。
    - 「常に」系や空 → 必ず実行（True）
    - 定義済みルール → rules を AND/OR で評価
    - 未定義のルール名 → 安全側でスキップ（False）。事故防止のため既定は実行しない。
    """
    if condition_name in [None, "", "always", "常に", "常に実行"]:
        return True

    for group in (conditions_config or []):
        if group.get("name") == condition_name:
            rules = group.get("rules", [])
            if not rules:
                return True
            results = [_eval_single_rule(r, customer_data) for r in rules]
            logic = str(group.get("logic", "AND")).upper()
            return all(results) if logic == "AND" else any(results)

    print(f"　⚠️ 条件ルール「{condition_name}」が未定義のため、安全のためこの手順はスキップします。")
    return False

# ==========================================
# 🔁 値の変換エンジン（コード不要の動的入力）
# ==========================================
def apply_transform(value: str, transform: str) -> str:
    """スプシ由来の値に、現場が選んだ加工を適用する。"""
    if not transform or transform in ["なし", "-", ""]:
        return value
    v = str(value)
    if transform == "ハイフン除去":
        return v.replace("-", "").replace("ー", "").replace("－", "")
    if transform == "数字のみ":
        return re.sub(r"\D", "", v)
    if transform == "市外局番":   # 例: 090-1234-5678 → 090
        return v.split("-")[0] if "-" in v else v
    if transform == "市内局番":   # → 1234
        parts = v.split("-")
        return parts[1] if len(parts) > 1 else ""
    if transform == "加入者番号":  # → 5678
        parts = v.split("-")
        return parts[2] if len(parts) > 2 else ""
    if transform == "郵便番号_上3桁":
        return v.replace("-", "")[:3]
    if transform == "郵便番号_下4桁":
        return v.replace("-", "")[3:7]
    return value

# ==========================================
# 🖐 有人確認モード（A案）：確認画面手前まで自動入力し、送信は人が押す
# ==========================================
def _confirm_command_path(work_dir):
    return os.path.join(work_dir, "command.json")

def _confirm_read_command(work_dir, index):
    """アプリが書いた指示(command.json)を読む。indexが一致したときだけ有効。"""
    if not work_dir:
        return ""
    try:
        with open(_confirm_command_path(work_dir), encoding="utf-8") as f:
            d = json.load(f)
        if int(d.get("index", -999)) == index:
            return str(d.get("action", "")).strip()
    except Exception:
        pass
    return ""

def _confirm_clear_command(work_dir):
    try:
        os.remove(_confirm_command_path(work_dir))
    except Exception:
        pass

def _confirm_write_live(work_dir, data):
    """今まさに待っている案件の状況を live.json に書く（アプリが読んで表示する）。"""
    if not work_dir:
        return
    try:
        with open(os.path.join(work_dir, "live.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

def _hold_completion_screen(page, work_dir, index, total, project_name, captured, timeout_sec=600):
    """申請が終わったあと、完了画面を開いたまま担当者が確認できるようにする。

    完了画面には回線登録番号などが出る（キャリアによる）。すぐ閉じてしまうと
    控えられないので、アプリの「次の案件へ」を押すまで待つ。
    あとから見返せるよう、完了画面のスクリーンショットと文言も残す。
    戻り値：False＝担当者が中止を選んだ（残りは実行しない）。"""
    shot = _save_screenshot(page, project_name, "完了画面")
    try:
        text_after = (page.inner_text("body") or "").strip()
    except Exception:
        text_after = ""
    try:
        url_after = page.url or ""
    except Exception:
        url_after = ""
    _confirm_write_live(work_dir, {
        "phase": "done_review", "index": index, "total": total,
        "captures": captured, "screenshot": shot, "url": url_after,
        # 完了画面の文言（先頭のみ）。『申請完了の合図』や『控える値』の設定に使える。
        "page_text": text_after[:1500], "updated_at": time.strftime("%H:%M:%S")})
    print("　🧾 完了画面を開いたままにしています。番号などを控えたら、アプリで「次の案件へ」を押してください。")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cmd = _confirm_read_command(work_dir, index)
        if cmd in ("next", "done"):
            _confirm_clear_command(work_dir)
            return True
        if cmd == "stop":
            _confirm_clear_command(work_dir)
            return False
        try:
            if page.is_closed():
                return True
        except Exception:
            return True
        time.sleep(2)
    print("　⏱ 待ち時間を過ぎたので次に進みます。")
    return True

def _marker_on_page(page, marker) -> bool:
    """『目印』の文字が今の画面にあるか。ログイン画面が出ているかの判定などに使う。"""
    marker = str(marker or "").strip()
    if not marker:
        return False
    try:
        text = page.inner_text("body")
    except Exception:
        try:
            text = re.sub(r"<[^>]+>", " ", page.content() or "")
        except Exception:
            return False
    return _squash(marker) in _squash(text)

def _progress_settings() -> dict:
    """進捗反映の設定（設定スプシURL・GASのURLと合言葉）をまとめて取り出す。"""
    try:
        res = supabase.table("merchants").select("config_json").eq("id", "__progress__").execute()
        if res.data:
            return res.data[0].get("config_json") or {}
    except Exception:
        pass
    return {}

def _settings_sheet_url():
    """進捗反映の設定スプレッドシートURLを取り出す（認証コードの受け取りに使う）。"""
    return str(_progress_settings().get("settings_url", "") or "")

def _ask_gas_for_code():
    """GAS（ウェブアプリ）に「いま届いているコードを取り出して」と頼む。
    時間ごとの自動実行を待たずに済むので、送信ボタンを押した直後でも取りに行ける。"""
    cfg = _progress_settings()
    url = str(cfg.get("gas_url", "") or "").strip()
    if not url:
        return False
    try:
        import urllib.parse
        import urllib.request
        q = urllib.parse.urlencode({"token": cfg.get("gas_token", ""), "action": "code"})
        with urllib.request.urlopen(f"{url}?{q}", timeout=120) as r:
            r.read()
        return True
    except Exception as e:
        print(f"　⚠️ 認証コードの取り出しを頼めませんでした: {str(e)[:120]}")
        return False

def wait_for_auth_code(carrier: str, since_ts: float, timeout_sec: int = 180, tab: str = "認証コード"):
    """メールから取り出された認証コードを待つ。

    GAS（認証コードの取り出し.gs）が、届いたメールからコードを抜き出して
    スプレッドシートに書く。ここではその行を見張り、
    『送信ボタンを押したあとに届いた』コードだけを受け取る。
    since_ts より古いコードは、前回の使い回しなので採用しない。
    """
    url = _settings_sheet_url()
    if not url:
        print("　⚠️ 認証コードの置き場所（設定スプレッドシート）が未設定です。")
        return ""
    gc = _sa_client_rw()
    if not gc:
        print("　⚠️ サービスアカウントが未設定のため、認証コードを読めません。")
        return ""
    try:
        ws = gc.open_by_url(url).worksheet(tab)
    except Exception as e:
        print(f"　⚠️ 「{tab}」タブを開けません: {e}")
        return ""

    print(f"　⏳ 認証コードのメールを待っています（最大{timeout_sec // 60}分）...")
    deadline = time.time() + timeout_sec
    _asked = 0.0
    while time.time() < deadline:
        # 30秒ごとにGASへ「取り出して」と頼む（定期実行を待たずに済む）
        if time.time() - _asked > 30:
            _asked = time.time()
            _ask_gas_for_code()
        try:
            for row in ws.get_all_values()[1:]:
                if len(row) < 3 or _squash(row[0]) != _squash(carrier):
                    continue
                code, stamp = str(row[1]).strip(), str(row[2]).strip()
                if not code:
                    continue
                try:
                    got = time.mktime(time.strptime(stamp, "%Y/%m/%d %H:%M:%S"))
                except Exception:
                    got = 0
                if got >= since_ts - 60:      # 押した直後に届いたものだけ採用（1分の余裕を見る）
                    # 🔢 桁が足りないコードは採用しない。
                    #    スプシが数字として扱うと 0042 が 42 になることがあり、
                    #    そのまま入れても認証に失敗する（気づきにくいので、ここで止める）。
                    if len(code) < 4:
                        print(f"　⚠️ 受け取ったコードが{len(code)}桁しかありません（{'*' * len(code)}）。"
                              "スプレッドシートの「認証コード」列を、書式『書式なしテキスト』にしてください。")
                        time.sleep(5)
                        continue
                    print(f"　✅ 認証コードを受け取りました（{len(code)}桁・{stamp}）。")
                    return code
        except Exception as e:
            print(f"　⚠️ 認証コードの確認中にエラー: {e}")
        time.sleep(5)
    print("　🛑 認証コードが時間内に届きませんでした。")
    return ""

def _wait_for_human_action(page, work_dir, index, total, message, headless, project_name,
                           marker="", timeout_sec=600):
    """人がブラウザで何かをするのを待つ汎用ステップ（ログイン、メールで届いた認証コードの入力など）。

    ロボットにできない・させないほうがよい操作を、手順書の途中に挟めるようにするための部品。
    アプリの「✅ できました → 続ける」を押すと再開する。
    無人（headless）では誰も操作できないので、待たずに中止する。
    戻り値：True＝続行してよい／False＝中止。"""
    if headless:
        print(f"🛑 「{message}」は人の操作が必要ですが、無人実行では対応できないため中止します。")
        return False
    print(f"　✋ 人の操作待ち：{message}　→ 終わったらアプリで「できました」を押してください。")
    _confirm_write_live(work_dir, {
        "phase": "waiting_human", "index": index, "total": total,
        "message": message, "updated_at": time.strftime("%H:%M:%S")})
    if not work_dir:
        # お試し実行など、アプリと繋がっていない場合は画面の変化を待つ（最大2分）
        try:
            before = page.url
        except Exception:
            before = ""
        for _ in range(60):
            time.sleep(2)
            try:
                if page.url != before:
                    print("　✅ 画面が変わったので処理を再開します。")
                    return True
            except Exception:
                return False
        print("　⏱ 画面が変わらないまま2分たったので、そのまま次に進みます。")
        return True
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        # 🎯 目印（例：「ログイン」）が画面から消えたら、操作が終わったと判断して自動で再開する。
        #    ボタンを押し忘れて止まったままになるのを防ぐ。
        if marker and not _marker_on_page(page, marker):
            print(f"　✅ 画面から「{marker}」が消えたので、操作が終わったとみなして再開します。")
            time.sleep(1)
            return True
        cmd = _confirm_read_command(work_dir, index)
        if cmd in ("human_ok", "next", "done"):
            _confirm_clear_command(work_dir)
            print("　✅ 操作が終わったので処理を再開します。")
            time.sleep(1)
            return True
        if cmd == "stop":
            _confirm_clear_command(work_dir)
            return False
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        time.sleep(2)
    print("🛑 人の操作を待ちましたが、時間切れになりました。")
    _save_screenshot(page, project_name, "wait_human_timeout")
    return False

def _detect_submit_success(page, success_text, success_url_contains):
    """完了サイン（文言／URL）を検知したら True。表記揺れは _squash で吸収。"""
    try:
        visible_after = page.inner_text("body")
    except Exception:
        visible_after = ""
    try:
        html_after = page.content() or ""
    except Exception:
        html_after = ""
    base = visible_after if visible_after.strip() else re.sub(r"<[^>]+>", " ", html_after)
    text_after = _squash(base)
    try:
        url_after = _squash(page.url or "")
    except Exception:
        url_after = ""
    ok_text = bool(success_text and _squash(success_text) in text_after)
    ok_url = bool(success_url_contains and _squash(success_url_contains) in url_after)
    return ok_text or ok_url

# 🔑 ログイン情報（ID/パスワード）の扱い
#    ・値そのものはDBに入れない。暗号文だけを config_json に保存する。
#    ・復号の鍵（ENKAN_SECRET_KEY）は、実行するPCの secrets.toml か環境変数にだけ置く。
#    ・手順書には {秘密:名前} と書き、実行時にここで実際の値へ置き換える。
#    ・置き換えた値はログにも失敗理由にも出さない（伏せ字にする）。
_SECRET_PH = re.compile(r"\{秘密:(.+?)\}")

def _secret_key():
    """復号の鍵を取り出す（環境変数優先。無ければ secrets.toml）。未設定なら None。"""
    return (os.environ.get("ENKAN_SECRET_KEY") or secrets.get("ENKAN_SECRET_KEY", "")).strip() or None

def decrypt_secrets(enc_map: dict) -> dict:
    """保存されている暗号文（名前→暗号文）を復号して 名前→値 にする。
    鍵が無い／壊れている場合は空を返す（呼び出し側でエラーにする）。"""
    if not enc_map:
        return {}
    key = _secret_key()
    if not key:
        print("　⚠️ ログイン情報の鍵（ENKAN_SECRET_KEY）がこのPCに設定されていません。")
        return {}
    try:
        from cryptography.fernet import Fernet
        f = Fernet(key.encode())
    except Exception as e:
        print(f"　⚠️ ログイン情報の鍵が正しくありません: {e}")
        return {}
    out = {}
    for name, token in (enc_map or {}).items():
        try:
            out[name] = f.decrypt(str(token).encode()).decode()
        except Exception:
            print(f"　⚠️ ログイン情報「{name}」を復号できませんでした（鍵が違う可能性）。")
    return out

def _mask_secret(text, values):
    """ログや失敗理由に、パスワード等がそのまま出ないよう伏せ字にする。"""
    s = str(text or "")
    for v in values or []:
        if v and len(str(v)) >= 3:
            s = s.replace(str(v), "****")
    return s

def _radio_selectors(form_choices, value, group_hint=""):
    """「選択肢を調べる」で記録しておいたラジオの“住所表”から、選びたい値の指定方法を返す。

    ラジオは見出し（グループ名）と選択肢の文字が別物で、文字だけでは探し当てられないことが多い。
    そこで id / value / 何番目か を記録しておき、実行時はそれを直接指す。
    group_hint（手順の対象名）が一致するグループを優先し、複数グループがあっても取り違えない。"""
    val = _squash(value or "")
    if not val or not form_choices:
        return []
    hint = _squash(group_hint or "")
    scored = []
    for c in form_choices:
        if (c or {}).get("kind") != "radio" or not c.get("items"):
            continue
        # ヒントがグループ名と一致（または含む）なら、そのグループを優先する
        glabel = _squash(c.get("label", ""))
        priority = 0 if (hint and (hint in glabel or glabel in hint)) else 1
        for item in c["items"]:
            ilabel = _squash(item.get("label", ""))
            if not ilabel:
                continue
            if ilabel == val:
                exact = 0
            elif val in ilabel or ilabel in val:
                exact = 1
            else:
                continue
            sel = item.get("selector") or ""
            if not sel:
                gname = c.get("selector", "")
                sel = f"{gname} >> nth={int(item.get('index', 0))}" if gname else ""
            if sel:
                scored.append((priority, exact, sel))
    scored.sort()
    return [s for _, _, s in scored]

def _extract_captures(page, captures):
    """申請完了画面から『控える値』（例：回線登録番号）を取り出す。
    キャリアごとにルールが違うので、ハードコードせず設定（robot_config.captures）で指示する。
    取り方は2通り：
      - pattern（正規表現）が指定されていれば、その1つ目の( )を使う
      - 無ければ hint（手がかり文言）の直後にある英数字のかたまりを拾う
    取れなかった項目は空文字で返す（＝あとで人が手入力する）。"""
    values = {}
    if not captures:
        return values
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    if not text.strip():
        try:
            text = re.sub(r"<[^>]+>", " ", page.content() or "")
        except Exception:
            text = ""
    for cap in captures:
        name = str((cap or {}).get("name", "") or "").strip()
        if not name:
            continue
        got = ""
        pattern = str(cap.get("pattern", "") or "").strip()
        hint = str(cap.get("hint", "") or "").strip()
        try:
            if pattern:
                m = re.search(pattern, text)
                if m:
                    got = (m.group(1) if m.groups() else m.group(0)).strip()
            elif hint:
                # 例：「回線登録番号 ： 1234-5678-90」→ 1234-5678-90
                m = re.search(re.escape(hint) + r"\s*[:：]?\s*([0-9A-Za-z\-‐－_/]{4,})", text)
                if m:
                    got = m.group(1).strip()
        except Exception as e:
            print(f"　⚠️ 「{name}」の読み取りに失敗: {e}")
        values[name] = got
        print(f"　📋 控える値『{name}』: {got or '（自動では取れませんでした→手入力してください）'}")
    return values

def _sa_client_rw():
    """書き込み権限つきのスプレッドシート接続を返す（未設定なら None）。
    ※ 読み取り用（_fetch_via_service_account）は readonly スコープなので別に用意する。
      シート側で、サービスアカウントを『編集者』として共有しておく必要がある。"""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)

def write_capture_values(config: dict, row: dict, values: dict):
    """控えた値（例：回線登録番号）を、案件IDで行を探してスプシに書き戻す。
    行番号ではなく『キー列の値が一致する行』に書くので、行がずれても取り違えない。
    戻り値は担当者向けのメッセージ（1行1件）。書けなかった理由もここに入れる。"""
    msgs = []
    captures = (config.get("robot_config", {}) or {}).get("captures", []) or []
    if not captures or not values:
        return msgs
    sheet_cfg = config.get("spreadsheet", {}) or {}
    sheet_url = sheet_cfg.get("url", "")
    gc = _sa_client_rw()
    if not gc:
        return ["⚠️ 書き戻しできません：GOOGLE_SERVICE_ACCOUNT_JSON が未設定です"]
    if not sheet_url:
        return ["⚠️ 書き戻しできません：スプレッドシートのURLが未設定です"]
    try:
        sh = gc.open_by_url(sheet_url)
    except Exception as e:
        return [f"⚠️ 書き戻しできません：スプシを開けませんでした（共有を『編集者』にしてください）: {e}"]

    for cap in captures:
        name = str((cap or {}).get("name", "") or "").strip()
        val = str(values.get(name, "") or "").strip()
        if not name or not val:
            continue
        tab = str(cap.get("tab", "") or sheet_cfg.get("tab_name", "") or "").strip()
        col_name = str(cap.get("col", "") or name).strip()
        key_col = str(cap.get("key_col", "") or sheet_cfg.get("key_col", "") or "案件ID").strip()
        key_val = str(row.get(key_col, "") or "").strip()
        if not key_val:
            msgs.append(f"⚠️ 「{name}」を書けません：この案件に「{key_col}」の値がありません")
            continue
        try:
            ws = sh.worksheet(tab)
            headers = ws.row_values(1)
            if col_name not in headers:
                msgs.append(f"⚠️ 「{name}」を書けません：シート『{tab}』に「{col_name}」列がありません")
                continue
            if key_col not in headers:
                msgs.append(f"⚠️ 「{name}」を書けません：シート『{tab}』に「{key_col}」列がありません")
                continue
            key_idx = headers.index(key_col) + 1
            col_idx = headers.index(col_name) + 1
            keys = ws.col_values(key_idx)
            hit = [i + 1 for i, v in enumerate(keys) if str(v).strip() == key_val]
            if not hit:
                msgs.append(f"⚠️ 「{name}」を書けません：{key_col}={key_val} の行が『{tab}』に見つかりません")
                continue
            if len(hit) > 1:
                msgs.append(f"⚠️ 「{name}」を書けません：{key_col}={key_val} の行が複数あります（{len(hit)}件）")
                continue
            ws.update_cell(hit[0], col_idx, val)
            msgs.append(f"✅ 「{name}」を『{tab}』{col_name}列（{hit[0]}行目）に書きました：{val}")
        except Exception as e:
            msgs.append(f"⚠️ 「{name}」の書き戻しに失敗: {e}")
    return msgs

def _wait_for_human_submit(page, work_dir, index, total, row, success_text,
                           success_url_contains, project_name, timeout_sec=1800):
    """人が申請ボタンを押すのを待つ。戻り値は (status, reason)。
    - 完了サイン設定あり → 送信されて完了画面になったら自動で done。
    - アプリ側の指示（done/skip/stop）でも進める。
    done: 送信できた（完了確認）／ skipped: 見送り／ aborted: 中止／ failed: 未確認・timeout。"""
    auto_detect = bool(success_text or success_url_contains)
    _confirm_clear_command(work_dir)
    deadline = time.time() + timeout_sec
    print(f"　✋ 確認待ち：内容を確認し、問題なければ画面の申請ボタンを押してください（{index + 1}/{total}）。")

    def _ask_after_close():
        """ブラウザが閉じられたとき、申請できたのかをアプリで答えてもらう。

        申請したのに『中止』と記録すると、この案件は処理済みにならず、
        次の実行でもう一度申請してしまう。逆に勝手に『完了』にすると、
        本当は出していない案件を出したことにしてしまう。どちらも危ないので、
        画面を閉じた人に聞く（アプリはまだ開いている）。
        """
        print("　🔎 ブラウザが閉じられました。申請できたかどうか、アプリで教えてください。")
        _confirm_write_live(work_dir, {
            "phase": "browser_closed", "index": index, "total": total, "row": row,
            "updated_at": time.strftime("%H:%M:%S")})
        _end = time.time() + 900          # 15分待つ
        while time.time() < _end:
            _c = _confirm_read_command(work_dir, index)
            if _c in ("done", "next"):
                _confirm_clear_command(work_dir)
                return ("done", "ブラウザを閉じたあと、担当者が『申請できた』と回答しました")
            if _c == "skip":
                _confirm_clear_command(work_dir)
                return ("skipped", "ブラウザを閉じたあと、担当者が『申請していない』と回答しました")
            if _c == "stop":
                _confirm_clear_command(work_dir)
                return ("aborted", "担当者が中止しました")
            time.sleep(2)
        return ("failed", "ブラウザが閉じられ、申請できたかどうか確認できませんでした")

    while time.time() < deadline:
        try:
            if page.is_closed():
                return _ask_after_close()
        except Exception:
            return _ask_after_close()
        cmd = _confirm_read_command(work_dir, index)
        if cmd == "skip":
            _confirm_clear_command(work_dir)
            return ("skipped", "担当者がスキップしました")
        if cmd == "stop":
            _confirm_clear_command(work_dir)
            return ("aborted", "担当者が中止しました")
        if cmd == "done":
            _confirm_clear_command(work_dir)
            if auto_detect and not _detect_submit_success(page, success_text, success_url_contains):
                _save_screenshot(page, project_name, "confirm_no_success")
                return ("failed", "送信を確認できませんでした（完了サイン未検出）。画面をご確認ください")
            return ("done", "")
        if auto_detect and _detect_submit_success(page, success_text, success_url_contains):
            return ("done", "")
        _confirm_write_live(work_dir, {
            "phase": "waiting_confirm", "index": index, "total": total, "row": row,
            "auto_detect": auto_detect, "updated_at": time.strftime("%H:%M:%S")})
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return ("failed", "時間内に送信が確認できませんでした（タイムアウト）")

# ==========================================
# 2. 申請漏れを許さない！厳格ロボットエンジン
# ==========================================
def run_robot(project_name: str, customer_data: dict, headless: bool = None,
              allow_submit: bool = True, mode: str = "auto",
              work_dir: str = None, confirm_index: int = 0,
              confirm_total: int = 1, result_out: dict = None) -> bool:
    """1件分の自動入力を実行する。
    allow_submit=False のときは『送信（申請）ステップ』を実行しない（お試し/モック用の安全テスト）。
    本番（run_all_active の LIVE）は既定の allow_submit=True で最後の申請まで行う。
    mode="confirm"（有人確認・A案）：確認画面の手前まで入力し、送信は人が押す。ロボットは押さない。
      完了を検知（または担当者の指示）できたら done。結果は result_out（dict）に格納する。
    """
    if mode == "confirm":
        headless = False          # 人が見て押すので必ず画面表示
        allow_submit = False      # ロボットは送信ボタンを押さない（人が押す）
    if headless is None:
        headless = is_headless()
    submit_mode = "申請まで実行(本番)" if allow_submit else "申請手前まで(テスト)"
    print(f"🚀 【{project_name}】のロボットを起動します...（headless={headless} / {submit_mode}）")

    response = supabase.table("merchants").select("config_json").eq("id", project_name).execute()
    if not response.data:
        print("❌ エラー: 設計図が見つかりません。")
        return False
    
    config = response.data[0]["config_json"]
    target_node_data = config.get("robot_config", {})
    entry_url = target_node_data.get("target_url", target_node_data.get("url"))
    steps = target_node_data.get("steps", [])
    conditions_config = config.get("conditions", [])  # 分岐ルールの定義一覧

    # 🐢 「人間らしくゆっくり操作する(stealth)」設定を実際の操作速度に反映する。
    #    （従来は headless かどうかだけで決まり、設定が無視されていた）
    stealth = bool(target_node_data.get("stealth", True))
    slow_mo = 500 if (stealth or not headless) else 0
    if target_node_data.get("captcha"):
        print("　ℹ️ CAPTCHA自動突破は未対応です。検出時は安全のため送信せず中止します。")

    # ✅ 申請完了の確認サイン（任意）。本番で偽成功を「処理済み」にしないための要。
    # 🔘 「選択肢を調べる」で記録したラジオ／プルダウンの一覧（選択肢ごとの“住所”つき）。
    #    ラジオを確実に選ぶために実行時も使う（無ければ従来どおり文字で探す）。
    form_choices = target_node_data.get("form_choices", []) or []
    # 🔑 ログイン情報を復号して用意する（手順書の {秘密:名前} で使う）
    robot_secrets = decrypt_secrets(target_node_data.get("secrets", {}) or {})
    secret_values = set(robot_secrets.values())
    success_text = str(target_node_data.get("success_text", "") or "").strip()
    success_url_contains = str(target_node_data.get("success_url_contains", "") or "").strip()
    submit_executed = False  # 送信（申請）ステップが実際に実行されたか

    print(f"　⚙️ 設定: stealth={stealth} / slow_mo={slow_mo}ms / 完了確認={'あり' if (success_text or success_url_contains) else 'なし'}")

    with sync_playwright() as p:
        _launch_kwargs = dict(
            headless=headless,
            slow_mo=slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
        )
        _context_kwargs = dict(
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            # 📥 進捗の取り込みでは、サイトからCSV等をダウンロードする手順がある。
            #    受け取れるようにしておく（申請ロボットでは使わないので影響しない）。
            accept_downloads=True,
        )
        _stealth_js = (
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['ja-JP','ja']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            "window.chrome=window.chrome||{runtime:{}};"
        )

        # 🕵️ ローカル(有人)では、専用のChromeプロファイルを使い回す。
        #    Cookie/履歴が貯まって『常連の人間』に見えるので、reCAPTCHA(画像認証)が出にくくなる。
        #    もし一度出ても、その表示中ウィンドウで人が解けば、以降は信頼が貯まり出にくくなる。
        #    保存場所は環境変数 ENKAN_CHROME_PROFILE で変更可。CI(headless)では使わない。
        # ⚠️ 同じサイトを別アカウントで使うロボットがある（例：東京用と東京以外用）。
        #    プロファイルを1つで共有すると、前のアカウントのログインが残っていて、
        #    もう片方のデータを取ってきてしまう＝気づけない取り違えになる。
        #    そこで、ロボットごとに別のプロファイルを使う。
        #    （robot_config.profile に名前を入れると、そのロボット同士で共有もできる）
        profile_dir = os.environ.get("ENKAN_CHROME_PROFILE", "").strip()
        if not profile_dir and not headless:
            _pname = str(target_node_data.get("profile", "") or project_name).strip()
            _pname = re.sub(r'[\\/:*?"<>|]', "_", _pname) or "default"
            profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       ".enkan_profile", _pname)

        browser = None
        if profile_dir:
            os.makedirs(profile_dir, exist_ok=True)
            try:
                context = p.chromium.launch_persistent_context(
                    profile_dir, channel="chrome", **_launch_kwargs, **_context_kwargs)
            except Exception:
                context = p.chromium.launch_persistent_context(
                    profile_dir, **_launch_kwargs, **_context_kwargs)
            print(f"　🕵️ 専用Chromeプロファイルを使用します（常連扱いでCAPTCHAを出にくく）: {profile_dir}")
        else:
            # CI等：プロファイルを使わず通常起動（本物Chromeが無ければChromiumへフォールバック）
            try:
                browser = p.chromium.launch(channel="chrome", **_launch_kwargs)
            except Exception:
                browser = p.chromium.launch(**_launch_kwargs)
            context = browser.new_context(**_context_kwargs)

        # 🕵️ 自動化検知(navigator.webdriver 等)を隠す。これが無いと画像認証(CAPTCHA)を出されやすい。
        context.add_init_script(_stealth_js)

        # 📥 ダウンロードの受け皿。
        #    サイトによっては「CSV出力」→「OK」を押した時点でファイルが落ち始め、
        #    その後に押す「ダウンロード」ボタンが無いことがある。
        #    どの手順で落ちてきても取りこぼさないよう、実行中ずっと見張って保存しておく。
        _dl_dir = work_dir or os.path.join(ARTIFACTS_DIR, "downloads")
        captured_downloads = []

        def _on_download(dl):
            try:
                os.makedirs(_dl_dir, exist_ok=True)
                _p = os.path.join(_dl_dir,
                                  f"{time.strftime('%Y%m%d_%H%M%S')}_{dl.suggested_filename}")
                dl.save_as(_p)
                captured_downloads.append(_p)
                print(f"　📥 ファイルを受け取りました: {_p}")
            except Exception as _e:
                print(f"　⚠️ ダウンロードを保存できませんでした: {str(_e)[:120]}")

        context.on("download", _on_download)

        def _close_browser():
            try:
                context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

        page = context.new_page()
        
        # ★改修1: 待機時間を15秒に設定。早すぎず、無限に止まらないベストな時間。
        page.set_default_timeout(15000)

        if not str(entry_url or "").strip():
            print("❌ エラー: このロボットに『サイトのURL』が設定されていません。"
                  "アプリの設定画面でURLを入れてください。")
            _close_browser()
            return False
        page.goto(entry_url)
        print("✅ サイトを開きました。操作を開始します...")
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except:
            pass
        time.sleep(1)

        # 🤖 ボット検知(CAPTCHA等)の壁に当たっていないか確認。当たっていたら安全に中止。
        if _looks_blocked(page):
            print("🛑 ボット検知（CAPTCHA等）の可能性を検出したため、安全のため中止します。")
            _save_screenshot(page, project_name, "captcha")
            if not headless:
                page.wait_for_timeout(5000)
            _close_browser()
            return False

        has_critical_error = False # ★改修2: 重大なエラー（入力漏れ）があったか記録するフラグ
        has_submit_step = False    # 送信（申請）ステップが手順にあるか（確認モードの完了判定に使う）
        error_reason = ""          # 失敗理由（結果一覧に表示する）

        for step in sorted(steps, key=lambda x: x.get("order", x.get("順番", 999))):
            # もし既にエラーが起きていたら、以降の「送信(Submit)」などは絶対に実行させない
            if has_critical_error:
                print("🛑 前のステップで入力エラーがあったため、以降の処理を安全のために中止します。")
                break

            condition = step.get("condition", step.get("いつ", "常に"))
            is_submit_step = is_submit_marker(condition)

            # 🚀 送信（申請）ステップは特別扱い：テスト/モックではスキップし、本番でのみ実行する。
            if is_submit_step:
                has_submit_step = True
                if mode == "confirm":
                    print("　✋ 送信（申請）は担当者が確認して押します（ロボットは押しません）。")
                    continue
                if not allow_submit:
                    print("　🧪 テストのため『送信（申請）』ステップはスキップしました（本番でのみ実行されます）。")
                    continue
                print("　🚀 最後の『送信（申請）』ステップを実行します（本番モード）。")
                # 送信は条件評価をバイパスして必ず実行（直前のエラーは has_critical_error で既に止まる）
            elif not evaluate_condition(condition, customer_data, conditions_config):
                continue

            raw_action = step.get("action", step.get("操作", ""))
            action_map = {"文字を入力": "fill", "クリック": "click", "選択": "select", "チェック": "check",
                          # ✋ ロボットにやらせない操作（ログイン・認証コード入力など）を人に任せる
                          "人の操作を待つ": "wait_human",
                          # 📥 進捗の取り込み：サイトのボタンを押してファイルを受け取る
                          "ファイルをダウンロード": "download",
                          # 🔐 メールに届いた認証コードを、GASが書いたセルから取って入力する
                          "認証コードを入力": "auth_code",
                          # 📅 カレンダー（日付ピッカー）の欄に日付を入れる
                          "日付を入れる": "date"}
            action = action_map.get(raw_action, raw_action)
            
            target_desc = step.get("target_description", step.get("対象", ""))
            raw_value = step.get("value", step.get("値", ""))
            ai_code = step.get("ai_code", step.get("最強の呪文", ""))

            # 🛠 動的注入エンジン (090問題の解決)
            action_value = str(raw_value)
            ai_code_executable = str(ai_code)
            
            matches = re.findall(r"\{(.+?)\}", action_value + ai_code_executable)
            for match in set(matches):
                if match in customer_data:
                    val = str(customer_data[match])
                    action_value = action_value.replace(f"{{{match}}}", val)
                    # ★改修3: Pythonコードとして実行する際、090等が数字扱いにならないよう、必ず元のコードのまま純粋に置換する
                    ai_code_executable = ai_code_executable.replace(f"{{{match}}}", val)

            # 🔑 {秘密:名前} を、暗号化して保存してあるログイン情報に置き換える。
            #    値はここでだけ実体になり、ログにも保存物にも残さない。
            _needed = set(_SECRET_PH.findall(action_value)) | set(_SECRET_PH.findall(ai_code_executable))
            if _needed:
                # 同じ名前が接続キー（secrets.toml / 環境変数）に直接あれば、そちらも使える。
                # 少人数・1台運用なら、暗号化を使わずSecretsに書くだけでも動かせるようにするため。
                for _n in list(_needed):
                    if _n not in robot_secrets:
                        _direct = os.environ.get(_n) or secrets.get(_n, "")
                        if str(_direct).strip():
                            robot_secrets[_n] = str(_direct)
                            secret_values.add(str(_direct))
                _missing = [n for n in _needed if n not in robot_secrets]
                if _missing:
                    _msg = (f"ログイン情報「{', '.join(_missing)}」を取り出せませんでした。"
                            "司令室で登録されているか、このPCに鍵（ENKAN_SECRET_KEY）が設定されているか確認してください")
                    print(f"　❌ エラー: {_msg}")
                    has_critical_error = True
                    error_reason = error_reason or _msg
                    continue
                for _n in _needed:
                    action_value = action_value.replace("{秘密:" + _n + "}", robot_secrets[_n])
                    ai_code_executable = ai_code_executable.replace("{秘密:" + _n + "}", robot_secrets[_n])

            # 🔎 その手順が「何を入れようとしているか」を必ず1行出す。
            #    止まったときに、値が空だったせいなのか、欄が見つからないせいなのかを
            #    ログだけで切り分けられるようにする（画面を見ていなくても分かる）。
            _from_sheet = bool(re.search(r"\{.+?\}", str(raw_value)))
            if _from_sheet:
                _shown = _mask_secret(str(action_value), secret_values)
                print(f"　　↳ 値：{raw_value} → "
                      + (f"「{_shown}」" if str(action_value).strip() else "（空でした）"))

            # 🕳 セルが空だったときの扱いは、手順ごとに決める（司令室の「空のとき」列）。
            #    飛ばしてよい項目（部屋番号など）と、空では申請できない項目（必須）があるため、
            #    一律には決められない。何も指定が無ければ、これまでどおり空のまま入力する。
            if _from_sheet and not str(action_value).strip():
                _empty_rule = str(step.get("空のとき", step.get("on_empty", "")) or "").strip()
                if _empty_rule == "飛ばす":
                    print(f"　⏭ 「{target_desc}」は空なので、この手順は行いません（設定：飛ばす）。")
                    continue
                if _empty_rule == "止める":
                    _msg = (f"「{target_desc}」が空でした。この項目は空では申請できない設定（止める）のため中止します。"
                            f"スプレッドシートの {raw_value} を確認してください")
                    print(f"　❌ エラー: {_msg}")
                    has_critical_error = True
                    error_reason = error_reason or _msg
                    break

            # ✍️ 手順書の「値」を、録画コードより優先する。
            #    値の書き方でこう決まる：
            #      ・そのままの文字（例：info@example.jp）→ 毎回その文字を入力
            #      ・{列名} や {秘密:名前}                 → 上の置き換えで実際の値になっている
            #      ・空                                    → 録画したときの文字をそのまま使う
            #    録画コード側に古い {列名} が残っていても、値が決まっていればそちらを使う
            #    （録画時に空欄で進めた欄を、手順書の修正だけで直せるようにするため）。
            if (action == "fill" and str(action_value).strip()
                    and not re.search(r"\{.+?\}", str(action_value))
                    and ai_code_executable and ".fill(" in ai_code_executable):
                _safe = str(action_value).replace("\\", "\\\\").replace('"', '\\"')
                ai_code_executable = re.sub(r'''\.fill\(\s*(?:"[^"]*"|'[^']*')\s*\)''',
                                            f'.fill("{_safe}")', ai_code_executable, count=1)

            # 🛡 未置換のプレースホルダーが残っていたら、誤った文字列をそのまま入力・送信しないよう対処する
            #    （手順書のプレースホルダー名とスプシの列名がズレている等、設定ミスの検知）
            unresolved = set(re.findall(r"\{(.+?)\}", action_value + ai_code_executable))
            # 🔐 {認証コード} は、この手順を実行する直前にメールから受け取って入れる。
            #    スプシの項目ではないので、未置換あつかいにしない
            #    （ここで弾くと、認証コードの手順ごと飛ばされてしまう）。
            if action == "auth_code":
                unresolved.discard("認証コード")
            if unresolved:
                if not allow_submit:
                    # お試し（モック）実行：固定のモックデータには全項目は無いのが普通なので、
                    # この手順だけスキップして先へ進む（本番では実データで埋まる）。全体は止めない。
                    print(f"　🧪 お試し：項目「{', '.join(unresolved)}」はモックデータに無いため、"
                          "この手順はスキップして次へ進みます（本番では実データで入力されます）。")
                    continue
                # 本番（実データ）：誤入力・誤送信を防ぐため、この手順を実行せず安全停止する。
                print(f"　❌ エラー: 項目「{', '.join(unresolved)}」がスプシのデータに見つからず、置き換えできませんでした。"
                      "誤入力・誤送信を防ぐため、この手順を実行せず停止します。")
                has_critical_error = True
                error_reason = error_reason or f"項目「{', '.join(unresolved)}」がスプシのデータに見つからず入力できませんでした"
                _save_screenshot(page, project_name, "unresolved_placeholder")
                continue

            # 🔁 列の値に「変換」が指定されていれば適用（例: 電話番号→市外局番）
            transform = step.get("transform", step.get("変換", ""))
            if transform:
                action_value = apply_transform(action_value, transform)

            step_num = step.get('order', step.get('順番', '?'))
            print(f"\n▶️ 手順{step_num}: 「{target_desc}」を処理します...")

            # 🧩 画像パズルが出ていたら、まず人に解いてもらう。
            #    解かないまま次の操作をしても「見つかりません」となり、原因を取り違えるため。
            if _captcha_challenge_visible(page):
                if not _wait_for_captcha_cleared(page, headless, project_name):
                    has_critical_error = True
                    error_reason = error_reason or ("画像パズル（CAPTCHA）が表示されたため中止しました"
                                                    "（自動突破はしません）")
                    break

            # 🈳 選ぶ値が空＝スプシの数式が空を返している。ここで止めて理由を明示する。
            #    ラジオ（クリック／チェック）も同じ。空のまま進むと選択されず、
            #    その選択でしか出てこない次の入力欄が「見つかりません」になり、
            #    スプシ側が原因だと分からなくなるため、ここで名指しして止める。
            _needs_value = (action == "select"
                            or (action in ("click", "check") and re.search(r"\{.+?\}", str(raw_value))))
            if _needs_value and not str(action_value).strip():
                _col = re.findall(r"\{(.+?)\}", str(raw_value)) or ["（列名不明）"]
                _msg = (f"「{target_desc}」に入れる値が空でした。"
                        f"スプシの「{_col[0]}」列が空になっていないか確認してください"
                        "（数式が空文字を返している可能性）")
                print(f"　❌ エラー: {_msg}")
                has_critical_error = True
                error_reason = error_reason or _msg
                _save_screenshot(page, project_name, "empty_value")
                continue

            action_success = False
            select_error = ""   # 選択肢を選べなかったときの、具体的な失敗理由

            # 📅 カレンダー（日付ピッカー）の欄
            #    録画すると「その日のマス」を覚えてしまい、翌日には使えない。
            #    だから日付だけは、専用のやり方で入れる。
            if action == "date":
                _y, _m, _d = _parse_date(action_value)
                if not _y:
                    _msg = (f"「{target_desc}」に入れる日付を読み取れませんでした（値：{action_value}）。"
                            "2026/09/01 のような形にしてください")
                    print(f"　❌ エラー: {_msg}")
                    has_critical_error = True
                    error_reason = error_reason or _msg
                    continue
                if _set_date_field(page, target_desc, _y, _m, _d, ai_code_executable):
                    print(f"　📅 日付を入れました（{_y}/{_m:02d}/{_d:02d}）。")
                    continue
                _msg = (f"「{target_desc}」に日付（{_y}/{_m:02d}/{_d:02d}）を入れられませんでした。"
                        "欄の名前が合っているか、カレンダーが開くかを確認してください")
                print(f"　❌ エラー: {_msg}")
                has_critical_error = True
                error_reason = error_reason or _msg
                _save_screenshot(page, project_name, "date_failed")
                break

            # 🔐 認証コードのステップ：メールが届くのを待って、その値を入力する
            #    「値」にキャリア名（＝GASが書いた行の名前）を入れておく。
            if action == "auth_code":
                _who = str(action_value or "").strip() or project_name
                _code = wait_for_auth_code(_who, time.time())
                if not _code:
                    _msg = (f"認証コードを受け取れませんでした（{_who}）。"
                            "メールが届いているか、GASの設定を確認してください")
                    print(f"　❌ エラー: {_msg}")
                    has_critical_error = True
                    error_reason = error_reason or _msg
                    _save_screenshot(page, project_name, "auth_code_timeout")
                    break
                try:
                    # ⌨️ 認証コード欄は、まとめて入れると途中が捨てられるサイトがある
                    #    （1文字ずつの入力を前提にJavaScriptで制御している）。
                    #    そこで人が打つのと同じように1文字ずつ入れ、入り切ったかを確かめる。
                    _typed = False
                    if ai_code_executable and ai_code_executable != "-" and ".fill(" in ai_code_executable:
                        _sel = ai_code_executable.split(".fill(")[0]
                        try:
                            _loc = eval(_sel, {"page": page})     # 録画のセレクタをそのまま使う
                            _loc.click(timeout=5000)
                            _loc.fill("")
                            _loc.type(_code, delay=120)
                            _got = str(_loc.input_value() or "")
                            if _got != _code:                      # 入り切らなければ入れ直す
                                print(f"　⚠️ {len(_got)}文字しか入らなかったため、入れ直します。")
                                _loc.fill("")
                                for _ch in _code:
                                    _loc.type(_ch, delay=200)
                                _got = str(_loc.input_value() or "")
                            if _got == _code:
                                _typed = True
                            else:
                                print(f"　⚠️ 認証コードが最後まで入りませんでした（{len(_got)}文字）。")
                        except Exception as _e2:
                            print(f"　⚠️ 1文字ずつの入力に失敗、録画どおりの方法を試します: {str(_e2)[:100]}")
                    if not _typed:
                        if ai_code_executable and ai_code_executable != "-":
                            exec(ai_code_executable.replace("{認証コード}", _code),
                                 {"page": page, "time": time})
                        else:
                            page.get_by_label(target_desc.replace("「", "").replace("」", "").strip(),
                                              exact=False).first.fill(_code, timeout=5000)
                    print(f"　🔐 認証コードを入力しました（{len(_code)}桁）。")
                    continue
                except Exception as e:
                    _msg = f"認証コードを入力できませんでした: {str(e)[:150]}"
                    print(f"　❌ エラー: {_msg}")
                    has_critical_error = True
                    error_reason = error_reason or _msg
                    break

            # 📥 ファイルをダウンロードするステップ（進捗の取り込み用）
            #    「対象」＝押すボタンの文言。押した結果のファイルを work_dir に保存する。
            if action == "download":
                _btn = (target_desc or "ダウンロード").replace("「", "").replace("」", "").strip()
                _before = len(captured_downloads)

                # 「最新のファイル」と指定されているときは、すでに何か落ちていても
                # 改めて一番上のリンクを押す。録画した古いファイル名をクリックする手順が
                # 前に残っていると、そちらが落ちてしまうため。
                _want_newest = any(w in _btn for w in ("最新", "一番上", "いちばん上"))

                # ① もう落ちてきている場合（前の手順の「OK」でダウンロードが始まったなど）は、
                #    ボタンを探しに行かずにそれを使う。空振りで止まらないようにするため。
                if _before and not _want_newest:
                    _path = captured_downloads[-1]
                    print(f"　📥 すでにダウンロード済みのファイルを使います: {_path}")
                    if result_out is not None:
                        result_out.setdefault("downloads", []).append(_path)
                    continue

                # ② まだなら押しに行く。ボタン／リンク／ただの文字、どれでも押せるように順に試す。
                _pressed = False
                _errs = []
                # 📄「書き出し状況の一覧」から落とすサイトでは、ファイル名が毎回変わる。
                #    対象に「最新」と書いてあれば、名前では探さず、一番上のリンクを押す。
                if _want_newest:
                    _link, _label = _newest_download_link(page)
                    if _link is not None:
                        try:
                            _link.click(timeout=8000)
                            _pressed = True
                            print(f"　📄 いちばん新しいファイルを選びました：{_label[:60]}")
                        except Exception as e:
                            _errs.append(str(e)[:100])
                    else:
                        _errs.append("ファイルらしいリンクが見つかりませんでした")
                if not _pressed and ai_code_executable and ai_code_executable != "-":
                    try:
                        exec(ai_code_executable, {"page": page, "time": time})
                        _pressed = True
                    except Exception as e:
                        _errs.append(str(e)[:100])
                if not _pressed:
                    for _how in (lambda: page.get_by_role("button", name=_btn, exact=False).first,
                                 lambda: page.get_by_role("link", name=_btn, exact=False).first,
                                 lambda: page.get_by_text(_btn, exact=False).first):
                        try:
                            _how().click(timeout=5000)
                            _pressed = True
                            break
                        except Exception as e:
                            _errs.append(str(e)[:100])

                # ③ 押したあと、ファイルが届くまで待つ（サーバーが作るのに時間がかかることがある）。
                _deadline = time.time() + 120
                while len(captured_downloads) == _before and time.time() < _deadline:
                    page.wait_for_timeout(500)

                if len(captured_downloads) > _before:
                    _path = captured_downloads[-1]
                    print(f"　📥 ダウンロードしました: {_path}")
                    if result_out is not None:
                        result_out.setdefault("downloads", []).append(_path)
                    continue

                _msg = (f"「{_btn}」でファイルをダウンロードできませんでした"
                        + ("（ボタンが見つかりませんでした）" if not _pressed else "（押しましたがファイルが届きませんでした）")
                        + (f": {_errs[0]}" if _errs else ""))
                print(f"　❌ エラー: {_msg}")
                has_critical_error = True
                error_reason = error_reason or _msg
                _save_screenshot(page, project_name, "download_failed")
                break

            # ✋ 人の操作を待つステップ（ログイン／メールの認証コード入力など）
            if action == "wait_human":
                _hmsg = target_desc or "画面の操作"
                # 🎯『目印』（値の欄）を入れておくと、その文字が画面に無いときは待たずに飛ばす。
                #    ログイン済みでログイン画面が出なかった場合に、止まったままにならないため。
                _marker = str(action_value or "").strip()
                if _marker and not _marker_on_page(page, _marker):
                    print(f"　⏭ 画面に「{_marker}」が無いので、この手順（{_hmsg}）は不要と判断して飛ばします。")
                    continue
                if _wait_for_human_action(page, work_dir, confirm_index, confirm_total,
                                          _hmsg, headless, project_name, marker=_marker):
                    continue
                has_critical_error = True
                error_reason = error_reason or f"「{_hmsg}」の人の操作が完了しませんでした"
                break

            # 🔘 0. ラジオは「選択肢を調べる」で記録した“住所”を最優先で使う。
            #    録画の呪文は、表のセルなど『見た目の場所』を押しているだけのことがあり、
            #    その場合クリックは成功するのにラジオは選ばれず、しかも成功扱いになって
            #    気づけない（次の入力欄が出てこず、別の場所でエラーになる）。
            if action in ("click", "check") and str(action_value).strip():
                for _sel in _radio_selectors(form_choices, action_value,
                                             str(step.get("radio_group", "") or target_desc)):
                    try:
                        _el = page.locator(_sel).first
                        _el.check(timeout=2000, force=True)
                        if _el.is_checked():
                            action_success = True
                            print(_mask_secret(f"　🔘 記録しておいた選択肢の場所で選びました（{_sel} ＝ {action_value}）", secret_values))
                            break
                    except Exception:
                        continue

            # 🌟 1. AIが生成したサイト固有の「最強の呪文」を直接実行
            if not action_success and ai_code_executable and ai_code_executable != "-":
                try:
                    exec(ai_code_executable, {"page": page, "time": time})
                    action_success = True
                    print("　✨ AIの呪文で操作に成功しました！")
                    
                    try: page.wait_for_load_state("domcontentloaded", timeout=3000)
                    except: pass
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"　⚠️ AIの呪文が空振りしました。（詳細: {e}）汎用フォールバックに移行します。")

            # 🛡 2. 呪文が失敗した場合は、Playwrightの全機能を使った汎用フォールバック
            if not action_success and action and target_desc:
                try:
                    clean_desc = target_desc.replace("「", "").replace("」", "").strip()
                    
                    if action == "fill":
                        locators = [page.get_by_placeholder(clean_desc, exact=False), page.get_by_label(clean_desc, exact=False), page.locator(target_desc)]
                        # 🧾 日本の申込フォームに多い「表の左に項目名・右に入力欄」の形に対応する。
                        #    <label for=...> で結ばれていないため get_by_label では見つからず、
                        #    録画の id が変わっていると入力欄に辿り着けないため、
                        #    項目名と同じ行／直後にある入力欄を探す。
                        locators.append(page.get_by_role("textbox", name=clean_desc, exact=False))
                        locators.append(page.locator("tr", has_text=clean_desc)
                                        .locator("textarea, input[type='text'], input:not([type])"))
                        if "'" not in clean_desc:
                            locators.append(page.locator(
                                f"xpath=//*[contains(normalize-space(text()),'{clean_desc}')]/following::textarea[1]"))
                            locators.append(page.locator(
                                f"xpath=//*[contains(normalize-space(text()),'{clean_desc}')]/following::input[1]"))
                        for loc in locators:
                            try:
                                loc.first.fill(action_value, timeout=2000)
                                action_success = True
                                break
                            except: pass

                    elif action in ["click", "check"]:
                        locators = [page.get_by_role("radio", name=clean_desc), page.get_by_text(clean_desc, exact=False), page.get_by_role("button", name=clean_desc, exact=False)]
                        # 🔘 ラジオ対策：押したい「値」（スプシ由来。例：有り）でも探す。
                        #    対象名はグループの見出し（例：CB有無）になりがちで、それだけでは選択肢を押せないため。
                        _pick = str(action_value or "").strip()
                        if _pick and _pick != clean_desc:
                            # 📍 最優先：「選択肢を調べる」で記録した“住所表”から直接指す。
                            #    手順に radio_group があればそのグループに限定する（取り違え防止）。
                            _grp = str(step.get("radio_group", "") or "")
                            _mapped = [page.locator(_s) for _s in
                                       _radio_selectors(form_choices, _pick, _grp or clean_desc)]
                            _guess = []
                            if '"' not in _pick and "'" not in _pick:
                                _guess = [
                                    page.get_by_role("radio", name=_pick, exact=False),
                                    page.get_by_label(_pick, exact=False),
                                    page.locator(f"input[type='radio'][value='{_pick}']"),
                                    page.locator("label", has_text=_pick),
                                ]
                            locators = _mapped + _guess + locators
                        # 送信（申請）／『次/送信/確認/申請/ボタン』系や英語(submit/next/button)は、
                        # 送信・次へ系のボタン候補を必ず加える（対象名が英語でも「次へ」を押せるように）
                        _cld = clean_desc.lower()
                        if (is_submit_step
                                or any(w in clean_desc for w in ["次", "送信", "確認", "申請", "申込", "申し込", "確定", "進む", "ボタン"])
                                or any(w in _cld for w in ["submit", "button", "next", "confirm"])):
                            locators.insert(0, page.get_by_role("button", name=clean_desc, exact=False))
                            locators.insert(1, page.locator("input[type='submit'], button[type='submit']"))
                            locators.insert(2, page.get_by_role("button", name="Submit"))
                            locators.insert(3, page.get_by_role("button", name="次へ", exact=False))
                            locators.insert(4, page.get_by_text("次へ", exact=False))

                        for loc in locators:
                            try:
                                target = loc.first
                                try: target.scroll_into_view_if_needed(timeout=500)
                                except: pass
                                
                                if action == "check": target.check(timeout=2000, force=True)
                                else:
                                    try:
                                        target.click(timeout=2000)
                                    except Exception:
                                        # 見た目を自前で描いているラジオ／チェックは input が隠れていて
                                        # クリックできないことがある。その場合は check(force) で選ぶ。
                                        target.check(timeout=1500, force=True)
                                action_success = True
                                break
                            except: pass
                            
                    elif action == "select":
                        try:
                            loc = page.get_by_label(clean_desc, exact=False).first
                            loc.select_option(action_value, timeout=2000)
                            action_success = True
                        except Exception:
                            # 📌 選べなかった理由を具体的に残す。
                            #    工事の時間枠のように「締切を過ぎて選択不可(disabled)になった」場合、
                            #    『見つかりませんでした』では担当者がスプシのどこを直せばよいか分からないため、
                            #    今その画面で選べる選択肢を一覧にして失敗理由に載せる。
                            try:
                                _opts = [t.strip() for t in
                                         loc.locator("option:not([disabled])").all_text_contents() if t.strip()]
                            except Exception:
                                _opts = []
                            # 🧪 お試し実行では、ダミーの値（「テスト」など）が
                            #    プルダウンの選択肢に無いのは当たり前。そこで止まると
                            #    先の手順を確かめられないので、実在する選択肢で進める。
                            #    お試しは申請しないので、何を選んでも実害がない。
                            if _opts and not allow_submit:
                                _real = [o for o in _opts if not _is_placeholder_option(o)]
                                if _real:
                                    try:
                                        loc.select_option(label=_real[0], timeout=2000)
                                        action_success = True
                                        print(f"　🧪 お試しなので、選べる中から『{_real[0]}』を選びました"
                                              f"（本番ではスプレッドシートの値を選びます）。")
                                    except Exception:
                                        pass
                            if _opts and not action_success:
                                select_error = _mask_secret(
                                    f"「{clean_desc}」で『{action_value}』を選べませんでした"
                                    f"（締切等で選択できない可能性）。いま選べるのは："
                                    + " / ".join(_opts[:12]), secret_values)

                    if action_success:
                        print("　👍 汎用フォールバック操作で成功しました！")
                        try: page.wait_for_load_state("domcontentloaded", timeout=3000)
                        except: pass
                        time.sleep(1)
                    else:
                        # 画像パズルで進めていないだけのことがある。原因を取り違えないよう名指しする。
                        if _captcha_challenge_visible(page):
                            _msg = (f"画像パズル（CAPTCHA）が出ていて先に進めませんでした"
                                    f"（「{clean_desc}」まで到達できず）")
                        else:
                            _msg = select_error or f"画面内に「{clean_desc}」が見つかりませんでした"
                            # 「値が空だったせい」なのか「欄が見つからないせい」なのかを、
                            # ここで名指しする。担当者がスプシを直せばよいのか、
                            # 手順書を直せばよいのかが分かるようにするため。
                            if re.search(r"\{.+?\}", str(raw_value)) and not str(action_value).strip():
                                _msg += (f"（このとき入れようとした値 {raw_value} は"
                                         "スプレッドシートで空でした。数式の結果が空になっていないか確認してください）")
                        print(f"　❌ エラー: {_msg}")
                        has_critical_error = True # ★改修4: 見つからなかったらエラーフラグを立てる！
                        error_reason = error_reason or _msg
                        _save_screenshot(page, project_name, "notfound")
                except Exception as e:
                    has_critical_error = True
                    error_reason = error_reason or f"「{target_desc}」の操作中にエラーが発生しました: {e}"
                    _save_screenshot(page, project_name, "exception")

            # 送信（申請）ステップが実際に実行できたら記録（後段の完了確認に使う）
            if is_submit_step and action_success:
                submit_executed = True

        # 🖐 有人確認モード（A案）：入力し終えたら、人が申請ボタンを押すのを待つ。
        if mode == "confirm":
            if has_critical_error:
                status, reason = "failed", (error_reason or "入力中に問題が発生したため停止しました")
                _save_screenshot(page, project_name, "confirm_stopped")
            elif not has_submit_step:
                status, reason = "failed", "送信（申請）ステップが未設定のため申請できません（司令室で追加してください）"
            else:
                status, reason = _wait_for_human_submit(
                    page, work_dir, confirm_index, confirm_total, customer_data,
                    success_text, success_url_contains, project_name)
            # 📋 申請できたら、完了画面から『控える値』（例：回線登録番号）を取り出す。
            #    ブラウザを閉じる前に読むこと（閉じたあとでは二度と取れない）。
            captured = {}
            if status == "done":
                _caps_cfg = target_node_data.get("captures", []) or []
                captured = _extract_captures(page, _caps_cfg)
                # 🧾 完了画面で一旦とまるか。
                #    ・設定がONなら毎回とまる（番号を控える／完了画面の文言を調べる）
                #    ・OFFでも、控えるはずの値が取れていないときは安全のため止める
                #      （ここで止めないと、番号を控える手段が無くなるため）
                _missing = [c for c in _caps_cfg
                            if not str(captured.get(c.get("name", ""), "") or "").strip()]
                if target_node_data.get("hold_completion", True) or _missing:
                    if not _hold_completion_screen(page, work_dir, confirm_index, confirm_total,
                                                   project_name, captured):
                        status, reason = "aborted", "担当者の指示で中止しました"
            if result_out is not None:
                result_out["status"] = status
                result_out["reason"] = reason
                result_out["row"] = customer_data
                result_out["captures"] = captured
            print(f"　🏁 この案件の結果: {status}（{reason or 'OK'}）")
            _close_browser()
            return status == "done"

        # ✅ 送信後の完了確認：申請ボタンを押しただけで「成功」にしない。
        #    成功サインが一致すれば最優先で成功扱い（サイト全体に出る reCAPTCHA 等の誤検知に勝たせる）。
        #    一致が無く、ブロック検出 or 成功サイン未検出なら失敗扱いにし、処理済みに入れない（再試行可能に）。
        if (not has_critical_error) and allow_submit and submit_executed:
            try: page.wait_for_load_state("networkidle", timeout=5000)
            except: pass
            time.sleep(1)
            # 可視テキストを優先（無理ならタグ除去HTML）。正規化して全角半角・空白の揺れを吸収して照合する。
            try: visible_after = page.inner_text("body")
            except Exception: visible_after = ""
            try: html_after = (page.content() or "")
            except Exception: html_after = ""
            base_after = visible_after if visible_after.strip() else re.sub(r"<[^>]+>", " ", html_after)
            text_after = _squash(base_after)
            try: url_after = _squash(page.url or "")
            except Exception: url_after = ""

            ok_text = bool(success_text and (_squash(success_text) in text_after))
            ok_url = bool(success_url_contains and (_squash(success_url_contains) in url_after))

            if ok_text or ok_url:
                # 完了サインを確認できたら、サイト全体のreCAPTCHA等が残っていても成功とみなす
                print("　✅ 申請完了のサインを確認しました。")
            elif _looks_blocked(page):
                print("　🛑 送信後にボット検知の壁を検出（完了サインも未確認）。申請未完了の可能性が高いため失敗扱いにします。")
                _save_screenshot(page, project_name, "after_submit_blocked")
                has_critical_error = True
            elif success_text or success_url_contains:
                print("　❌ 申請完了の確認ができませんでした（成功サイン未検出）。失敗扱いにします。")
                _save_screenshot(page, project_name, "no_success_confirm")
                has_critical_error = True
            else:
                print("　⚠️ 申請を送信しましたが、完了確認の設定（完了画面の文言）が無いため成功は自動確認できていません。"
                      "司令室で『完了画面に出る文言』を設定すると、失敗を検知して再申請できます。")

        # 📥 受け取ったファイルは、手順に「ファイルをダウンロード」が無くても結果に載せる
        #    （「CSV出力」→「OK」だけでファイルが落ちるサイトのため）
        if result_out is not None and captured_downloads:
            _known = result_out.setdefault("downloads", [])
            for _p in captured_downloads:
                if _p not in _known:
                    _known.append(_p)

        # 最終判定
        if has_critical_error:
            print("\n🚨 【警告】申請漏れのリスクがあるため、途中でロボットを停止しました。")
            _save_screenshot(page, project_name, "stopped")
            # （※後ほどここにスプシを❌エラーにする処理を入れます）
        else:
            print("\n✨ 全ての手順が完璧に完了しました！")

        # 有人(ローカル)実行のときだけ、担当者が結果を目視できるよう少し待つ
        if not headless:
            print("10秒後にブラウザを閉じます...")
            try:
                page.wait_for_timeout(10000)   # 目視できるよう少し待つ
            except Exception:
                pass    # ブラウザを閉じられていても、そこで落とさない
        _close_browser()
        return not has_critical_error

# ==========================================
# ☁️ クラウド実行：稼働中の全ロボットをまとめて回す
# ==========================================
def _csv_export_url(sheet_url: str, tab_name: str = "") -> str:
    """Googleスプレッドシートのリンク共有URLから、CSVとして読めるgviz URLを組み立てる。"""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)", sheet_url or "")
    if not m:
        raise ValueError(f"スプシURLからシートIDを取得できませんでした: {sheet_url}")
    sheet_id = m.group(1)
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    if tab_name:
        url += "&sheet=" + urllib.parse.quote(tab_name)
    return url

def _parse_pending(raw_csv: str, trigger_col: str, trigger_val: str) -> list:
    """CSV本文を行(dict)に変換し、ステータス列が指定値の行だけを返す。"""
    if raw_csv.lstrip().startswith("<"):
        # ログイン画面(HTML)が返るのは、リンク共有(閲覧可)になっていないとき
        raise RuntimeError(
            "スプシをCSVとして読めませんでした。共有設定が『リンクを知っている全員（閲覧者）』"
            "になっているか確認してください。"
        )
    reader = csv.DictReader(io.StringIO(raw_csv))
    # 同名の見出しがあると DictReader は後勝ちで上書きし、列の値が静かに消える。
    # 誤った内容での申請を防ぐため、重複ヘッダは明示エラーにする。
    fields = [(h or "").strip() for h in (reader.fieldnames or [])]
    dups = sorted({h for h in fields if h and fields.count(h) > 1})
    if dups:
        raise RuntimeError(
            f"スプシの見出し（ヘッダ）に重複があります: 「{'」「'.join(dups)}」。"
            "各列の見出しは重複しない名前にしてください（重複すると値が正しく取り込めません）。"
        )
    rows = []
    for r in reader:
        clean = {(k or "").strip(): (str(v) if v is not None else "").strip()
                 for k, v in r.items() if k is not None}
        if not any(clean.values()):
            continue  # 空行はスキップ
        # 全件対象（「未エントリー」での絞り込みは廃止。保留はレポート側で非表示にする運用）
        rows.append(clean)
    return rows

def _fetch_via_service_account(sheet_url: str, tab_name: str, trigger_col: str, trigger_val: str):
    """
    サービスアカウント経由（認証あり）でスプシを読み込み、対象行を返す。
    GOOGLE_SERVICE_ACCOUNT_JSON が未設定なら None を返す（呼び出し側で従来の匿名CSV方式にフォールバック）。
    「リンクを知っている全員」にできない、実在の顧客情報を含む本物のシート向け。
    ※ シート側で、このサービスアカウントのメールアドレス（〜@プロジェクト名.iam.gserviceaccount.com）
      を閲覧者として共有しておく必要がある。
    """
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc = gspread.authorize(creds)
    try:
        sh = gc.open_by_url(sheet_url)
    except PermissionError as e:
        # gspread は中身の無い PermissionError を投げるため、そのままだと
        # ログに「ダミーで続行: 」とだけ出て、何が悪いのか分からない。
        _mail = ""
        try:
            _mail = str(info.get("client_email", "") or "")
        except Exception:
            pass
        raise RuntimeError(
            "このスプレッドシートを開く権限がありません。"
            + (f"ロボットのアドレス（{_mail}）に、閲覧者以上で共有してください。" if _mail else "")
            + "（共有 → メールアドレスを貼り付け → 閲覧者）") from e
    try:
        ws = sh.worksheet(tab_name) if tab_name else sh.sheet1
    except Exception as e:
        _names = ""
        try:
            _names = "／".join(w.title for w in sh.worksheets()[:15])
        except Exception:
            pass
        raise RuntimeError(
            f"タブ『{tab_name}』が見つかりません。"
            + (f"このスプレッドシートにあるのは：{_names}" if _names else "")) from e
    values = ws.get_all_values()
    if len(values) < 2:
        return []

    headers = [(h or "").strip() for h in values[0]]
    # 同名の見出しがあると値が正しく取り込めないため、匿名CSV方式と同様に明示エラーにする。
    dups = sorted({h for h in headers if h and headers.count(h) > 1})
    if dups:
        raise RuntimeError(
            f"スプシの見出し（ヘッダ）に重複があります: 「{'」「'.join(dups)}」。"
            "各列の見出しは重複しない名前にしてください（重複すると値が正しく取り込めません）。"
        )

    rows = []
    for data_row in values[1:]:
        clean = {headers[i]: (str(data_row[i]).strip() if i < len(data_row) else "")
                 for i in range(len(headers)) if headers[i]}
        if not any(clean.values()):
            continue  # 空行はスキップ
        # 全件対象（「未エントリー」での絞り込みは廃止。保留はレポート側で非表示にする運用）
        rows.append(clean)
    return rows

def fetch_pending_rows(config: dict) -> list:
    """
    SFAスプレッドシートから「未エントリー」の案件行を取得する。
    ヘッダ名がそのまま手順書の {項目名} に対応する（例: 列『電話番号』→ {電話番号}）。

    GOOGLE_SERVICE_ACCOUNT_JSON が設定されていれば認証付き（サービスアカウント）方式を優先する
    （実在の顧客情報を含み「リンクを知っている全員」にできない本物のシート向け）。
    未設定なら、従来の匿名CSV方式（リンク共有・読み取り専用）にフォールバックする。
    """
    sheet = config.get("spreadsheet", {})
    url = sheet.get("url", "")
    if not url:
        print("　⚠️ スプシURLが未設定のためスキップします。")
        return []
    # スプシに表示された案件は全件エントリーする（保留はレポート側で非表示にする運用）。
    #   ※ trigger_col は二重申請防止の dedup キー計算で status 列を除外するためだけに残す。
    trigger_col = sheet.get("trigger_col", "ステータス")
    trigger_val = sheet.get("trigger_val", "未エントリー")
    tab_name = sheet.get("tab_name", "")
    _target_desc = "全件"

    sa_rows = _fetch_via_service_account(url, tab_name, trigger_col, trigger_val)
    if sa_rows is not None:
        print(f"　🔑 サービスアカウント経由でスプシ読み込み成功：{_target_desc}の対象 {len(sa_rows)} 件")
        return sa_rows

    csv_url = _csv_export_url(url, tab_name)
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig", errors="replace")
    rows = _parse_pending(raw, trigger_col, trigger_val)
    print(f"　📄 スプシ読み込み成功：{_target_desc}の対象 {len(rows)} 件")
    return rows

def _norm_value(v) -> str:
    """表記揺れ（全角半角・前後空白・連続空白）を吸収して比較を安定させる（dedupキー用）。"""
    s = unicodedata.normalize("NFKC", str(v if v is not None else ""))
    return re.sub(r"\s+", " ", s).strip()

def _squash(s) -> str:
    """完了サイン照合用の強正規化：NFKC＋全空白除去＋小文字化。
    タグ分断（受<wbr>付）や全角半角・余分な空白でも一致できるよう、空白を完全に落とす。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s if s is not None else ""))).lower()

def _row_key(row: dict, trigger_col: str, dedup_cols=None) -> str:
    """行を一意に識別するキー（ステータス列は除外）。値は正規化してから比較するので、
    無関係なセルの表記揺れ（全角半角・末尾空白など）での誤再申請を減らす。
    dedup_cols（安定した一意キー列の配列）が指定されればその列だけでキーを作る。"""
    if dedup_cols:
        items = sorted((c, _norm_value(row.get(c, ""))) for c in dedup_cols)
    else:
        items = sorted((k, _norm_value(v)) for k, v in row.items() if k != trigger_col)
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()

def _row_key_legacy(row: dict, trigger_col: str) -> str:
    """旧方式のキー（正規化なし）。既存の _processed_keys と後方互換に判定するため併用する。"""
    items = sorted((k, v) for k, v in row.items() if k != trigger_col)
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()

# ==========================================
# 🔔 通知・証跡・処理済みキーの保存（クラウド無人運用の観測性）
# ==========================================
def notify_slack(config: dict, text: str) -> bool:
    """Slack Incoming Webhook に通知する。SLACK_WEBHOOK_URL 未設定なら何もしない（opt-in）。
    通知失敗で本処理は止めない。slack_id はチャンネル名の目印として本文に前置するだけ
    （Incoming Webhook の投稿先はURL側で固定のため、本文での宛先指定はできない）。"""
    url = os.environ.get("SLACK_WEBHOOK_URL") or secrets.get("SLACK_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        ch = ((config or {}).get("notifications", {}) or {}).get("slack_id", "")
        prefix = f"[{ch}] " if ch else ""
        payload = json.dumps({"text": prefix + str(text)}).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"　⚠️ Slack通知に失敗しました: {e}")
        return False

def _render_slack_success(config: dict, row: dict) -> str:
    """完了通知メッセージ。slack_msg 内の {項目名} を顧客データで置換する。"""
    msg = str(((config or {}).get("notifications", {}) or {}).get("slack_msg") or "自動申請が完了しました。")
    for k, v in (row or {}).items():
        msg = msg.replace(f"{{{k}}}", str(v))
    return msg

def _persist_processed_keys(name: str, keys_list: list) -> bool:
    """処理済みキーを「最新の config_json」に read-modify-write でマージ保存する。
    起動時に読んだ古い config を全置換すると、実行中の司令室編集を踏み潰す（lost update）ため、
    保存直前に最新を取得して _processed_keys だけ上書きする。順序を保ち上限で切り捨てる。"""
    try:
        cur = supabase.table("merchants").select("config_json").eq("id", name).execute()
        cfg = (cur.data[0]["config_json"] if cur.data else {}) or {}
        deduped = list(dict.fromkeys(keys_list))  # 追記順を保持して重複排除
        if len(deduped) > PROCESSED_KEYS_LIMIT:
            print(f"　⚠️ 処理済みキーが上限({PROCESSED_KEYS_LIMIT})超過。古いものを切り捨てます（再申請リスクに注意）。")
            deduped = deduped[-PROCESSED_KEYS_LIMIT:]
        cfg["_processed_keys"] = deduped
        supabase.table("merchants").update({"config_json": cfg}).eq("id", name).execute()
        return True
    except Exception as e:
        print(f"　⚠️ 処理済みキーの保存に失敗しました（次回再申請の可能性）: {e}")
        return False

def _write_run_summary(summary_rows: list, allow_live: bool):
    """1回の実行サマリ（台数・成否）を成果物として残す。無人運用の事後確認用。"""
    try:
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        path = os.path.join(ARTIFACTS_DIR, f"run_summary_{time.strftime('%Y%m%d_%H%M%S')}.json")
        data = {
            "mode": "live" if allow_live else "dryrun",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "robots": summary_rows,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"　🧾 実行サマリを保存しました: {path}")
    except Exception as e:
        print(f"　⚠️ 実行サマリの保存に失敗: {e}")

def _allow_live(explicit=None) -> bool:
    """本番（実ブラウザ操作）を許可するか。既定は安全側でドライラン。"""
    if explicit is not None:
        return explicit
    return os.environ.get("ENKAN_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes", "on")

def run_all_active(headless: bool = None, allow_live: bool = None) -> int:
    """
    is_active=True の全ロボットについて未処理案件を順に実行する。クラウド(Actions)の入口。
    - 読み取り専用のためスプシへ書き戻せない → 処理済み行を Supabase(config_json._processed_keys)
      に記録し、再実行時にスキップ（二重申請の防止）。
    - allow_live=False（既定）は「やる予定」を表示するだけのドライラン。
    戻り値は失敗件数（0 なら全成功）。
    """
    if headless is None:
        headless = is_headless()
    allow_live = _allow_live(allow_live)
    res = supabase.table("merchants").select("*").eq("is_active", True).execute()
    robots = res.data or []
    # 進捗の取り込み用ロボットは申請をしない（ブラウザでファイルを落とすだけ）。
    # 一括実行に混ざると、申請ロボットのつもりで動かしてしまうので外す。
    robots = [r for r in robots
              if str((r.get("config_json") or {}).get("product_type", "")) != "進捗取り込み"
              and not str(r.get("id", "")).startswith("__")]
    mode = "本番(LIVE)" if allow_live else "ドライラン(表示のみ)"
    print(f"☁️ 稼働中ロボット: {len(robots)} 台 / headless={headless} / モード={mode}")
    failures = 0
    summary_rows = []
    for robot in robots:
        name = robot.get("id")
        config = robot.get("config_json", {})
        sheet_cfg = config.get("spreadsheet", {})
        trigger_col = sheet_cfg.get("trigger_col", "ステータス")
        dedup_cols = sheet_cfg.get("dedup_cols") or None  # 任意：安定した一意キー列でのdedup
        # 処理済みキーは「追記順を保持」して扱う（set順は不定で、上限切り捨て時に任意キーが脱落するため）
        processed_list = list(dict.fromkeys(config.get("_processed_keys", [])))
        processed_set = set(processed_list)
        print(f"\n==== ▶ {name} ====")
        try:
            rows = fetch_pending_rows(config)
        except Exception as e:
            print(f"　❌ スプシ読み込みに失敗しました: {e}")
            notify_slack(config, f"❌ {name}: スプシ読み込みに失敗しました（{e}）")
            failures += 1
            summary_rows.append({"robot": name, "targets": 0, "done": 0, "failed": 1, "error": str(e)})
            continue

        # 🛡 dedup_cols の指定列がスプシに無いと、全行が空値で同一キーに潰れ『処理済み扱い』で
        #    大量スキップ（申請漏れ）になる。列の存在を検証し、無ければ安全に全列キーへ切り替える。
        if dedup_cols and rows:
            missing = [c for c in dedup_cols if c not in rows[0]]
            if missing:
                print(f"　⚠️ dedup_cols の列がスプシに見つかりません: {missing}。安全のため全列キーで重複判定します。")
                notify_slack(config, f"⚠️ {name}: dedup_cols 列 {missing} がスプシに無いため、全列キーで重複判定します（設定を確認してください）。")
                dedup_cols = None

        # 未処理判定：新キー(正規化) と 旧キー(legacy) のどちらも未登録なら未処理（後方互換）
        fresh = []
        for r in rows:
            k = _row_key(r, trigger_col, dedup_cols)
            if k in processed_set or _row_key_legacy(r, trigger_col) in processed_set:
                continue
            fresh.append((r, k))
        print(f"　🔎 対象 {len(rows)} 件のうち、未処理は {len(fresh)} 件（処理済みは自動スキップ）。")

        if not allow_live:
            for r, _ in fresh:
                print(f"　🧪 [ドライラン] 実行予定: {r}")
            summary_rows.append({"robot": name, "targets": len(rows), "pending": len(fresh), "mode": "dryrun"})
            continue

        done_count = 0
        fail_count = 0
        for r, k in fresh:
            try:
                ok = run_robot(name, r, headless=headless)
            except Exception as e:
                print(f"　❌ 実行中に例外が発生しました: {e}")
                ok = False
            if ok:
                processed_list.append(k)
                processed_set.add(k)
                # 逐次保存（途中でタイムアウト/クラッシュしても処理済みが巻き戻らない＝二重申請防止）
                _persist_processed_keys(name, processed_list)
                done_count += 1
                notify_slack(config, _render_slack_success(config, r))
            else:
                failures += 1
                fail_count += 1
                notify_slack(config, f"⚠️ {name}: 申請に失敗または中止しました。証跡（artifacts のスクショ）を確認してください。")
        if done_count or fail_count:
            notify_slack(config, f"📊 {name}: 完了 {done_count} 件 / 失敗 {fail_count} 件")
        summary_rows.append({"robot": name, "targets": len(rows), "done": done_count, "failed": fail_count, "mode": "live"})

    _write_run_summary(summary_rows, allow_live)
    print(f"\n✅ 全処理が完了しました（失敗 {failures} 件）。")
    return failures

def _confirm_write_status(work_dir, data):
    """有人確認セッション全体の状況を status.json に書く（アプリが読んで結果一覧を出す）。"""
    try:
        os.makedirs(work_dir, exist_ok=True)
        with open(os.path.join(work_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"　⚠️ 状況の書き出しに失敗: {e}")

def run_confirm_session(project_name: str, work_dir: str, only_keys=None) -> list:
    """🖐 有人確認モード（A案・ローカル専用）：
    未処理の案件を上から順に、確認画面の手前まで自動入力 → 人が申請ボタンを押す → 完了検知 → 次へ。
    成功した案件だけ _processed_keys に記録する（＝次回スキップ）。失敗は記録しない（再実行で拾える）。
    only_keys（集合）を渡すと、そのキーの案件だけを対象にする（＝失敗分だけ再実行）。
    経過と結果は work_dir/status.json・live.json に書き、Streamlitアプリが読んで表示する。"""
    os.makedirs(work_dir, exist_ok=True)
    resp = supabase.table("merchants").select("config_json").eq("id", project_name).execute()
    if not resp.data:
        _confirm_write_status(work_dir, {"phase": "error", "message": "設計図が見つかりません", "results": []})
        return []
    config = resp.data[0]["config_json"] or {}
    sheet_cfg = config.get("spreadsheet", {})
    trigger_col = sheet_cfg.get("trigger_col", "ステータス")
    dedup_cols = sheet_cfg.get("dedup_cols") or None
    processed_list = list(dict.fromkeys(config.get("_processed_keys", [])))
    processed_set = set(processed_list)

    try:
        rows = fetch_pending_rows(config)
    except Exception as e:
        _confirm_write_status(work_dir, {"phase": "error", "message": f"スプシ読み込みに失敗: {e}", "results": []})
        return []

    if dedup_cols and rows:
        missing = [c for c in dedup_cols if c not in rows[0]]
        if missing:
            dedup_cols = None  # 指定列が無ければ全列キーに安全フォールバック

    # 対象の組み立て：通常は未処理のみ／only_keys 指定時はそのキーだけ（＝失敗分の再実行）
    targets = []
    for r in rows:
        k = _row_key(r, trigger_col, dedup_cols)
        legacy = _row_key_legacy(r, trigger_col)
        if only_keys is not None:
            if k in only_keys or legacy in only_keys:
                targets.append((r, k))
        elif k in processed_set or legacy in processed_set:
            continue
        else:
            targets.append((r, k))

    total = len(targets)
    results = []

    def _status(phase, current_index=None, current_row=None):
        _confirm_write_status(work_dir, {
            "project": project_name, "phase": phase, "total": total,
            "current_index": current_index, "current_row": current_row,
            "results": results, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    print(f"🖐 有人確認モード：対象 {total} 件（上から順に確認して送信）")
    _status("started")
    for i, (r, k) in enumerate(targets):
        _confirm_clear_command(work_dir)
        _status("running", i, r)
        result_out = {}
        try:
            run_robot(project_name, r, mode="confirm", work_dir=work_dir,
                      confirm_index=i, confirm_total=total, result_out=result_out)
        except Exception as e:
            result_out = {"status": "failed", "reason": f"実行中にエラー: {e}", "row": r}
        status = result_out.get("status", "failed")
        reason = result_out.get("reason", "")
        captured = result_out.get("captures", {}) or {}
        entry = {"index": i, "row": r, "status": status, "reason": reason, "key": k,
                 "captures": captured, "capture_notes": []}
        results.append(entry)
        if status == "done":
            # ⚠️ 順番が大事：先に「申請済み」を記録する。
            #    番号が取れなくても二重申請だけは絶対に避ける（番号は後から人が入れられる）。
            processed_list.append(k)
            _persist_processed_keys(project_name, processed_list)
            if captured and any(v for v in captured.values()):
                entry["capture_notes"] = write_capture_values(config, r, captured)
                for _m in entry["capture_notes"]:
                    print("　" + _m)
            elif (config.get("robot_config", {}) or {}).get("captures"):
                entry["capture_notes"] = ["⚠️ 自動では控えられませんでした（アプリの一覧から手入力してください）"]
                print("　⚠️ 控える値を自動取得できませんでした（あとで手入力してください）")
            notify_slack(config, _render_slack_success(config, r))
        _status("running", i, r)
        if status == "aborted":
            print("　🛑 担当者の指示で中止しました。")
            break

    _status("finished")
    n_done = sum(1 for x in results if x["status"] == "done")
    n_fail = sum(1 for x in results if x["status"] == "failed")
    n_skip = sum(1 for x in results if x["status"] == "skipped")
    print(f"\n🏁 有人確認モード完了：✅{n_done} / ❌{n_fail} / ⏭{n_skip}")
    return results

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--all"

    if arg == "--intake":
        # 📥 進捗の取り込み：サイトにログインしてファイルを落とすだけのモード
        #    python robot.py --intake <ロボット名> <保存先フォルダ>
        #    申請はしないので送信ステップは実行しない。ダウンロードは work_dir に保存される。
        _name = sys.argv[2]
        _wd = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ARTIFACTS_DIR, "downloads")
        os.makedirs(_wd, exist_ok=True)
        _out = {}
        _ok = run_robot(_name, {}, headless=False, allow_submit=False, work_dir=_wd, result_out=_out)
        _got = _out.get("downloads") or []
        for _p in _got:
            print(f"　✅ 保存: {_p}")
        # 📝 「今回どのファイルを落としたか」を書き残す。
        #    ファイル名は毎回変わるうえ、前回の残りも同じフォルダにあるので、
        #    “いちばん新しいファイル”で当てにいくと古いファイルを掴む事故がある。
        try:
            with open(os.path.join(_wd, "_last_download.json"), "w", encoding="utf-8") as _f:
                json.dump({"時刻": time.strftime("%Y/%m/%d %H:%M:%S"),
                           "ロボット": _name, "ファイル": _got}, _f, ensure_ascii=False, indent=2)
        except Exception as _e:
            print(f"　⚠️ 取得記録を書けませんでした: {str(_e)[:120]}")
        sys.exit(0 if _got else 1)

    if arg == "--confirm":
        # 有人確認モード：python robot.py --confirm <ロボット名> <work_dir> [--only <keys.json>]
        _name = sys.argv[2]
        _wd = sys.argv[3]
        _only = None
        if "--only" in sys.argv:
            try:
                with open(sys.argv[sys.argv.index("--only") + 1], encoding="utf-8") as _f:
                    _only = set(json.load(_f))
            except Exception as _e:
                print(f"　⚠️ --only の読み込みに失敗、全未処理を対象にします: {_e}")
        run_confirm_session(_name, _wd, only_keys=_only)
        sys.exit(0)

    if arg in ("--all", "-a", "all"):
        # クラウド/定期実行：稼働中の全ロボットを実行（失敗があれば非0で終了）
        sys.exit(1 if run_all_active() else 0)

    # 単体テスト：指定ロボットをモック顧客で実行（司令室の「お試し実行」ボタン用）
    #  手順書の全プレースホルダー {列名} を、それっぽいダミー値で自動的に埋める
    #  （こうしないと列名が一致せず大半の入力がスキップされ、フォームが進まずテストにならない）
    def _dummy_for(name: str) -> str:
        n = name.lower()
        if any(w in name for w in ["電話", "番号", "ＴＥＬ", "tel"]) or "phone" in n:
            return "09012345678"
        if "郵便" in name or "zip" in n or "postal" in n:
            return "8140165"
        if "メール" in name or "mail" in n:
            return "test@example.com"
        if "日" in name or "date" in n:
            return "2026/05/03"
        if any(w in name for w in ["氏名", "名前", "お名前"]):
            return "自動化 太郎"
        if "住所" in name:
            return "東京都テスト区1-2-3"
        if any(w in name for w in ["金額", "円", "数量"]):
            return "1000"
        return "テスト"

    test_customer = None
    _cfg = None
    try:
        _resp = supabase.table("merchants").select("config_json").eq("id", arg).execute()
        if _resp.data:
            _cfg = _resp.data[0]["config_json"]
    except Exception as _e:
        print(f"　⚠️ 設計図の読み込みに失敗: {_e}")

    # ① まずはスプシの“実データ（数式適用後の本物の値）”でテストする。
    if _cfg is not None:
        try:
            _rows = fetch_pending_rows(_cfg)
            if _rows:
                test_customer = _rows[0]
                print(f"　🧪 スプシの実データ（1件目）でテストします＝本物の値で入力（申請はしません）。")
            else:
                # 「なぜダミーになったか」が分からないと、直しようがない。
                # どのシートの、どの列が何なら対象なのかまで書く。
                _sp = (_cfg or {}).get("spreadsheet", {}) or {}
                print("　ℹ️ スプシに対象データが無いため、ダミーでテストします。"
                      f"（タブ『{_sp.get('tab_name', '')}』で "
                      f"『{_sp.get('trigger_col', 'ステータス')}』が "
                      f"『{_sp.get('trigger_val', '未エントリー')}』の行を探しました）")
                print("　　※ 実データで試したいときは、その条件に合う行を1行つくってください"
                      "（処理済みとして記録された行は対象外になります）。")
        except Exception as _e:
            print(f"　⚠️ スプシの実データ取得に失敗、ダミーで続行: {_e}")

    # ② 実データが無い/取れない場合は、手順書の {列名} をダミー値で埋めてテストする。
    if test_customer is None:
        test_customer = {
            "顧客_氏名": "自動化 太郎", "電話番号": "090-1234-5678", "郵便番号": "814-0165",
            "代理店名": "株式会社ライフアップ", "メッセージ": "テスト入力です",
        }
        _steps = (_cfg or {}).get("robot_config", {}).get("steps", [])
        _phs = set()
        for _s in (_steps or []):
            for _k in ("値", "value", "ai_code", "最強の呪文"):
                _phs |= set(re.findall(r"\{(.+?)\}", str((_s or {}).get(_k, "") or "")))
        for _p in _phs:
            test_customer.setdefault(_p, _dummy_for(_p))

    # お試しは安全のため『送信（申請）』ステップを実行しない＝申請手前まで。
    sys.exit(0 if run_robot(arg, test_customer, allow_submit=False) else 1)