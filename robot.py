import sys
import os
import io
import csv
import hashlib
import tomllib
import time
import re
import urllib.request
import urllib.parse
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 接続キーを読み込む（クラウド=環境変数 / ローカル=secrets.toml）
# ==========================================
def load_secrets() -> dict:
    """GitHub Actions などクラウドでは環境変数を優先し、ローカルでは secrets.toml から読む。"""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        return {
            "SUPABASE_URL": os.environ["SUPABASE_URL"],
            "SUPABASE_KEY": os.environ["SUPABASE_KEY"],
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
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
_BLOCK_HINTS = ["recaptcha", "hcaptcha", "captcha", "私はロボットではありません",
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
# 2. 申請漏れを許さない！厳格ロボットエンジン
# ==========================================
def run_robot(project_name: str, customer_data: dict, headless: bool = None,
              allow_submit: bool = True) -> bool:
    """1件分の自動入力を実行する。
    allow_submit=False のときは『送信（申請）ステップ』を実行しない（お試し/モック用の安全テスト）。
    本番（run_all_active の LIVE）は既定の allow_submit=True で最後の申請まで行う。
    """
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
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            slow_mo=0 if headless else 500,
            args=["--disable-blink-features=AutomationControlled"]
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
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
            browser.close()
            return False

        has_critical_error = False # ★改修2: 重大なエラー（入力漏れ）があったか記録するフラグ

        for step in sorted(steps, key=lambda x: x.get("order", x.get("順番", 999))):
            # もし既にエラーが起きていたら、以降の「送信(Submit)」などは絶対に実行させない
            if has_critical_error:
                print("🛑 前のステップで入力エラーがあったため、以降の処理を安全のために中止します。")
                break

            condition = step.get("condition", step.get("いつ", "常に"))

            # 🚀 送信（申請）ステップは特別扱い：テスト/モックではスキップし、本番でのみ実行する。
            if is_submit_marker(condition):
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
                        if "次" in clean_desc or "送信" in clean_desc or "確認" in clean_desc:
                            locators.insert(0, page.get_by_role("button", name="Submit"))
                            locators.insert(1, page.locator("input[type='submit'], button[type='submit']"))

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
                        _save_screenshot(page, project_name, "notfound")
                except Exception as e:
                    has_critical_error = True
                    _save_screenshot(page, project_name, "exception")

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
        browser.close()
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
    rows = []
    for r in reader:
        clean = {(k or "").strip(): (str(v) if v is not None else "").strip()
                 for k, v in r.items() if k is not None}
        if not any(clean.values()):
            continue  # 空行はスキップ
        if clean.get(trigger_col, "") == trigger_val:
            rows.append(clean)
    return rows

def fetch_pending_rows(config: dict) -> list:
    """
    SFAスプレッドシート（リンク共有・読み取り専用）から「未エントリー」の案件行を取得する。
    ヘッダ名がそのまま手順書の {項目名} に対応する（例: 列『電話番号』→ {電話番号}）。
    """
    sheet = config.get("spreadsheet", {})
    url = sheet.get("url", "")
    if not url:
        print("　⚠️ スプシURLが未設定のためスキップします。")
        return []
    trigger_col = sheet.get("trigger_col", "ステータス")
    trigger_val = sheet.get("trigger_val", "未エントリー")
    csv_url = _csv_export_url(url, sheet.get("tab_name", ""))
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig", errors="replace")
    rows = _parse_pending(raw, trigger_col, trigger_val)
    print(f"　📄 スプシ読み込み成功：『{trigger_col}』が『{trigger_val}』の対象 {len(rows)} 件")
    return rows

def _row_key(row: dict, trigger_col: str) -> str:
    """行を一意に識別するキー（ステータス列は除外。処理済み判定＝二重申請防止に使う）。"""
    items = sorted((k, v) for k, v in row.items() if k != trigger_col)
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()

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
    for robot in robots:
        name = robot.get("id")
        config = robot.get("config_json", {})
        trigger_col = config.get("spreadsheet", {}).get("trigger_col", "ステータス")
        processed = set(config.get("_processed_keys", []))
        print(f"\n==== ▶ {name} ====")
        try:
            rows = fetch_pending_rows(config)
        except Exception as e:
            print(f"　❌ スプシ読み込みに失敗しました: {e}")
            failures += 1
            continue

        fresh = [(r, _row_key(r, trigger_col)) for r in rows]
        fresh = [(r, k) for r, k in fresh if k not in processed]
        print(f"　🔎 対象 {len(rows)} 件のうち、未処理は {len(fresh)} 件（処理済みは自動スキップ）。")

        if not allow_live:
            for r, _ in fresh:
                print(f"　🧪 [ドライラン] 実行予定: {r}")
            continue

        newly_done = False
        for r, k in fresh:
            if run_robot(name, r, headless=headless):
                processed.add(k)
                newly_done = True
            else:
                failures += 1
        # 処理済みキーを保存（肥大化を防ぐため直近5000件に制限）
        if newly_done:
            config["_processed_keys"] = list(processed)[-5000:]
            supabase.table("merchants").update({"config_json": config}).eq("id", name).execute()

    print(f"\n✅ 全処理が完了しました（失敗 {failures} 件）。")
    return failures

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--all"

    if arg in ("--all", "-a", "all"):
        # クラウド/定期実行：稼働中の全ロボットを実行（失敗があれば非0で終了）
        sys.exit(1 if run_all_active() else 0)

    # 単体テスト：指定ロボットをモック顧客で実行（司令室の「お試し実行」ボタン用）
    mock_customer = {
        "顧客_氏名": "自動化 太郎",
        "電話番号": "090-1234-5678",
        "郵便番号": "814-0165",
        "年齢": 25,
        "商材名": "ドコモ光",
        "家族割": "あり",
        "希望日時": "2026/05/03",
        "代理店名": "株式会社ライフアップ",
        "発信番号": "0800921454",
        "メッセージ": "テスト入力です",
    }
    # お試し（モック）は安全のため『送信（申請）』ステップを実行しない＝申請手前まで。
    sys.exit(0 if run_robot(arg, mock_customer, allow_submit=False) else 1)