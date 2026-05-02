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
# 🔧 条件判定エンジン
# ==========================================
def evaluate_condition(condition_name: str, customer_data: dict) -> bool:
    if condition_name in ["always", "常に", "常に実行"]:
        return True
    
    if "未成年" in condition_name and "年齢" in customer_data:
        if int(customer_data["年齢"]) < 20: return True
        else: return False
            
    if "家族割" in condition_name:
        if customer_data.get("家族割") in ["あり", "希望する", True]: return True
        else: return False

    return True

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
            if not evaluate_condition(condition, customer_data):
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