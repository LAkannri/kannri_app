import sys
import tomllib
import time
import re
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 接続キーを読み込む
# ==========================================
with open(".streamlit/secrets.toml", "rb") as f:
    secrets = tomllib.load(f)

supabase: Client = create_client(secrets["SUPABASE_URL"], secrets["SUPABASE_KEY"])

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
def run_robot(project_name: str, customer_data: dict):
    print(f"🚀 【{project_name}】のロボットを起動します...")
    
    response = supabase.table("merchants").select("config_json").eq("id", project_name).execute()
    if not response.data:
        print("❌ エラー: 設計図が見つかりません。")
        return
    
    config = response.data[0]["config_json"]
    target_node_data = config.get("robot_config", {})
    entry_url = target_node_data.get("target_url", target_node_data.get("url"))
    steps = target_node_data.get("steps", [])
    conditions_config = config.get("conditions", [])  # 分岐ルールの定義一覧
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=500,
            args=["--disable-blink-features=AutomationControlled"] 
        ) 
        
        context = browser.new_context(viewport={"width": 1280, "height": 800})
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

        has_critical_error = False # ★改修2: 重大なエラー（入力漏れ）があったか記録するフラグ

        for step in sorted(steps, key=lambda x: x.get("order", x.get("順番", 999))):
            # もし既にエラーが起きていたら、以降の「送信(Submit)」などは絶対に実行させない
            if has_critical_error:
                print("🛑 前のステップで入力エラーがあったため、以降の処理を安全のために中止します。")
                break

            condition = step.get("condition", step.get("いつ", "常に"))
            if not evaluate_condition(condition, customer_data, conditions_config):
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
                except Exception as e:
                    has_critical_error = True

        # 最終判定
        if has_critical_error:
            print("\n🚨 【警告】申請漏れのリスクがあるため、途中でロボットを停止しました。スプレッドシートにエラーを記録します。")
            # （※後ほどここにスプシを❌エラーにする処理を入れます）
        else:
            print("\n✨ 全ての手順が完璧に完了しました！")

        print("10秒後にブラウザを閉じます...")
        page.wait_for_timeout(10000)
        browser.close()

if __name__ == "__main__":
    target_project = sys.argv[1] if len(sys.argv) > 1 else "ドコモ光 新規申込"
    
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
        "メッセージ": "テスト入力です"
    }
    
    run_robot(target_project, mock_customer)