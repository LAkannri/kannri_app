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

def _looks_blocked(page) -> bool:
    """画面が CAPTCHA / ボット検知の壁になっていそうか、ざっくり判定する。"""
    try:
        html = (page.content() or "").lower()
    except Exception:
        return False
    return any(hint.lower() in html for hint in _BLOCK_HINTS)

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
    while time.time() < deadline:
        try:
            if page.is_closed():
                return ("aborted", "ブラウザが閉じられました")
        except Exception:
            return ("aborted", "ブラウザが閉じられました")
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
        profile_dir = os.environ.get("ENKAN_CHROME_PROFILE", "").strip()
        if not profile_dir and not headless:
            profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".enkan_profile")

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
            action_map = {"文字を入力": "fill", "クリック": "click", "選択": "select", "チェック": "check"}
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

            # 🛡 未置換のプレースホルダーが残っていたら、誤った文字列をそのまま入力・送信しないよう対処する
            #    （手順書のプレースホルダー名とスプシの列名がズレている等、設定ミスの検知）
            unresolved = set(re.findall(r"\{(.+?)\}", action_value + ai_code_executable))
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

            action_success = False

            # 🌟 1. AIが生成したサイト固有の「最強の呪文」を直接実行
            if ai_code_executable and ai_code_executable != "-":
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
                        for loc in locators:
                            try:
                                loc.first.fill(action_value, timeout=2000)
                                action_success = True
                                break
                            except: pass

                    elif action in ["click", "check"]:
                        locators = [page.get_by_role("radio", name=clean_desc), page.get_by_text(clean_desc, exact=False), page.get_by_role("button", name=clean_desc, exact=False)]
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
                                else: target.click(timeout=2000)
                                action_success = True
                                break
                            except: pass
                            
                    elif action == "select":
                        try:
                            loc = page.get_by_label(clean_desc, exact=False).first
                            loc.select_option(action_value, timeout=2000)
                            action_success = True
                        except: pass
                    
                    if action_success:
                        print("　👍 汎用フォールバック操作で成功しました！")
                        try: page.wait_for_load_state("domcontentloaded", timeout=3000)
                        except: pass
                        time.sleep(1)
                    else:
                        print(f"　❌ エラー: 画面内に「{clean_desc}」が見つかりませんでした。")
                        has_critical_error = True # ★改修4: 見つからなかったらエラーフラグを立てる！
                        error_reason = error_reason or f"画面内に「{clean_desc}」が見つかりませんでした"
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
            if result_out is not None:
                result_out["status"] = status
                result_out["reason"] = reason
                result_out["row"] = customer_data
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
            page.wait_for_timeout(10000)
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
    sh = gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name) if tab_name else sh.sheet1
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
        results.append({"index": i, "row": r, "status": status, "reason": reason, "key": k})
        if status == "done":
            processed_list.append(k)
            _persist_processed_keys(project_name, processed_list)
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
                print("　ℹ️ スプシに対象データが無いため、ダミーでテストします。")
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