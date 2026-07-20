import streamlit as st
import sys
import uuid
import pandas as pd
import time
import json
import re
import subprocess
import google.generativeai as genai
from supabase import create_client, Client
import characters as ch
import theme

# --- ⚙️ システム設定 ---
st.set_page_config(page_title="エンカンAI - 事務作業の自動化パートナー", layout="wide")

# --- 🎨 共有デザインシステム＋サイドバーのブランド（録画担当を強調） ---
theme.inject_theme()
theme.brand_sidebar(active="create")

# --- 🔗 データベース接続（接続キーが無いときは赤いエラーではなくやさしく案内して停止） ---
def _has_secret(key):
    try:
        return bool(st.secrets.get(key))
    except Exception:
        return False

if not (_has_secret("SUPABASE_URL") and _has_secret("SUPABASE_KEY")):
    theme.page_header("🔌", "接続キーがまだ設定されていません",
                      "ロボットの設計図を保存するデータベース（Supabase）につなぐ鍵が必要です。",
                      color=ch.get("manage")["color"])
    ch.guide("manage",
             "ここはわたし（カンナ）の出番。<b>SUPABASE_URL</b> と <b>SUPABASE_KEY</b> を設定すると、"
             "この画面が使えるようになるよ。設定の手順は『その他設定』で案内するね。")
    st.markdown("""
    1. Streamlit Cloud：右下 **Manage app → Settings → Secrets** に3つのキーを貼り付け
    2. GitHub（クラウド自動実行）：**Settings → Secrets and variables → Actions** に同じ3つを登録
    3. 保存したら、このページを再読み込みしてください
    """)
    st.page_link("pages/5_⚙️_その他設定.py", label="⚙️ 設定の手順を見る（カンナの部屋へ）", use_container_width=True)
    st.stop()

@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase: Client = init_connection()

# --- 🎬 案内役（ロクすけ）からのひとこと ---
if 'view' not in st.session_state: st.session_state.view = 'dashboard'
if st.session_state.view == 'dashboard':
    ch.guide("create",
             "ここは自動化を<b>つくる</b>部屋だよ。新しいロボットを作るか、"
             "既存のロボットの手順を整えよう。困ったら各ステップでぼくが声をかけるね！")

# --- 🧠 セッション管理 ---
if 'editing_project' not in st.session_state: st.session_state.editing_project = None

# --- 🛠️ データベース操作 ---
def save_project(project_id, data): supabase.table("merchants").upsert(data).execute()
def get_project_data(project_id):
    res = supabase.table("merchants").select("*").eq("id", project_id).execute()
    return res.data[0] if res.data else None
def delete_project(project_id): supabase.table("merchants").delete().eq("id", project_id).execute()

# ==========================================
# 🧮 カラム設計（●●BOXシートの作成・修正）
# ==========================================
@st.cache_resource
def _get_gspread_client():
    """サービスアカウントでGoogle Sheetsへ読み書きするクライアントを作る。未設定ならNoneを返す。"""
    sa_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)

def _col_letter(n: int) -> str:
    """1始まりの列番号をスプシの列記号に変換する（1→A, 27→AA...）。"""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def _list_all_sheet_names(gc, sheet_url):
    """スプシ内の全タブ名を返す（デバッグ・透明性のため）。"""
    sh = gc.open_by_url(sheet_url)
    return [ws.title for ws in sh.worksheets()]

def _list_box_sheet_names(gc, sheet_url):
    """『BOX』という文字を含むタブ一覧を返す（大元の『BOX』自体は除く）。"""
    sh = gc.open_by_url(sheet_url)
    return [ws.title for ws in sh.worksheets()
            if ws.title.strip().upper() != "BOX" and "BOX" in ws.title.upper()]

def _read_master_box_headers(gc, sheet_url):
    """大元の『BOX』シートの1行目(見出し)を読み込む。無ければ空リストを返す。"""
    sh = gc.open_by_url(sheet_url)
    try:
        ws = sh.worksheet("BOX")
    except Exception:
        return []
    return ws.row_values(1)

def _read_box_sheet(gc, sheet_url, tab_name):
    """指定タブの1行目(見出し)とA2セルの数式を読み込む。"""
    sh = gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    headers = ws.row_values(1)
    formula = ws.acell("A2", value_render_option="FORMULA").value or ""
    return headers, formula

def _draft_box_formula(ref_tab, ref_headers, ref_formula, target_tab, condition_desc, is_new):
    """AIに、既存シートの数式パターンを手本にした新しいFILTER数式を考えてもらう。"""
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-flash')
    action = "新しく作成し" if is_new else "書き直し"
    prompt = f"""
あなたはGoogleスプレッドシートの数式に詳しいエンジニアです。
「{ref_tab}」という既存シートの数式パターンを手本にして、「{target_tab}」というシートの数式を{action}てください。

【手本シート「{ref_tab}」】
1行目の見出し: {ref_headers}
A2セルの数式: {ref_formula}

【今回の条件・変更内容】
{condition_desc}

【ルール】
- 元データは常に「BOX」という名前のシートを参照すること（手本と同じ）
- A2セルに1つのFILTER数式を入れ、配列として下に自動展開される形にすること（手本と同じ書き方）
- 1行目の見出しは、手本と同じ並び（BOXシートと同じ列見出し）にすること
- 絶対に以下のJSON形式のみを出力すること（説明文は不要）
{{"headers": ["見出し1", "見出し2", "..."], "formula": "=IFERROR(FILTER(...), \\"\\")"}}
"""
    response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
    return json.loads(response.text)

def _apply_box_sheet(gc, sheet_url, tab_name, headers, formula, is_new):
    """確認後、実際にシートへ書き込む（新規作成 or 既存の上書き）。"""
    sh = gc.open_by_url(sheet_url)
    if is_new:
        ws = sh.add_worksheet(title=tab_name, rows=200, cols=max(len(headers), 10))
    else:
        ws = sh.worksheet(tab_name)
    ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")
    ws.update(range_name="A2", values=[[formula]], value_input_option="USER_ENTERED")

def _read_final_sheet(gc, sheet_url, tab_name):
    """『●●』最終シートの1行目(見出し)と2行目の各列の数式を読み込む。"""
    sh = gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    headers = ws.row_values(1)
    formulas = ws.row_values(2, value_render_option="FORMULA")
    return headers, formulas

def _get_candidate_fields(config):
    """録画済みの手順から、データ入力が必要な項目（対象・現在のプレースホルダー名）の一覧を返す。"""
    steps = config.get("robot_config", {}).get("steps", [])
    fields, seen = [], set()
    for step in steps:
        if not step:
            continue
        action = step.get("action", step.get("操作", ""))
        if action not in ("文字を入力", "fill", "選択", "select"):
            continue
        target = str(step.get("target_description", step.get("対象", "")) or "").strip()
        ai_code = str(step.get("ai_code", step.get("最強の呪文", "")) or "")
        value = str(step.get("value", step.get("値", "")) or "")
        if target and target not in seen:
            seen.add(target)
            fields.append({"target": target, "current_placeholders": list(set(re.findall(r"\{(.+?)\}", ai_code + value)))})
    return fields

def _draft_final_column_formula(box_tab, box_headers, final_headers, final_formulas, field_desc, target_field):
    """AIに、●●BOXの列を参照する最終シート用の数式を考えてもらう。"""
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-flash')
    examples = "\n".join(f"- {h}: {f}" for h, f in zip(final_headers, final_formulas) if f)
    prompt = f"""
あなたはGoogleスプレッドシートの数式に詳しいエンジニアです。
「{box_tab}」という中間シートを参照して、最終シートの1つの列に入れる数式を考えてください。

【参照元「{box_tab}」の列一覧】
{box_headers}

【最終シートの、他の列の数式の例（参考にしてください）】
{examples if examples else "（まだ他の列に数式はありません）"}

【今回作りたい列】
フォームでの項目名: {target_field}
どう反映したいか: {field_desc}

【ルール】
- 「{box_tab}」シートの列を参照する数式にすること（例: ='{box_tab}'!A2 のような形）
- 2行目に入れる想定の数式にすること（そのまま下の行にコピーされる前提）
- 絶対に以下のJSON形式のみを出力すること（説明文は不要）
{{"column_name": "スプシに使う列の見出し名", "formula": "=..."}}
"""
    response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
    return json.loads(response.text)

def _apply_final_column(gc, sheet_url, tab_name, headers, col_name, formula):
    """最終シートに、指定した列の見出しと2行目の数式を書き込む（既存の列名なら上書き、無ければ末尾に追加）。"""
    sh = gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    if col_name in headers:
        idx = headers.index(col_name) + 1
    else:
        idx = len(headers) + 1
        ws.update(range_name=f"{_col_letter(idx)}1", values=[[col_name]], value_input_option="USER_ENTERED")
    ws.update(range_name=f"{_col_letter(idx)}2", values=[[formula]], value_input_option="USER_ENTERED")

def _sync_placeholder_in_steps(steps, target_field, new_col_name):
    """手順書の中で「対象」がtarget_fieldに一致する手順の値・ai_codeにある既存の{...}を、
    新しい列名に置き換える。渡されたstepsは書き換えず、更新後のコピーを返す。"""
    import copy
    new_steps = copy.deepcopy(steps)
    for step in new_steps:
        if not step:
            continue
        t = str(step.get("target_description", step.get("対象", "")) or "").strip()
        if t != target_field:
            continue
        for key in ("value", "値", "ai_code", "最強の呪文"):
            if step.get(key):
                step[key] = re.sub(r"\{.+?\}", f"{{{new_col_name}}}", str(step[key]))
    return new_steps

# ==========================================
# 🧩 共通パーツ（やさしいUIのための部品）
# ==========================================
WIZARD_STEPS = [("1", "基本情報"), ("2", "手本を見せる"), ("3", "確認・テスト")]

def render_stepper(active_index: int):
    """ウィザードの進捗バー。今どこにいるか一目で分かるようにする（active_index は 0 始まり）。"""
    parts = []
    for i, (num, label) in enumerate(WIZARD_STEPS):
        done, now = i < active_index, i == active_index
        if now:    bg, fg, border = "#0284C7", "#FFFFFF", "#0284C7"
        elif done: bg, fg, border = "#E0F2FE", "#0369A1", "#BAE6FD"
        else:      bg, fg, border = "#FFFFFF", "#9CA3AF", "#E5E7EB"
        mark = "✓" if done else num
        label_color = "#0369A1" if (now or done) else "#9CA3AF"
        parts.append(
            f"<div style='flex:1; text-align:center;'>"
            f"<span style='display:inline-flex; align-items:center; justify-content:center; "
            f"width:34px; height:34px; border-radius:50%; background:{bg}; color:{fg}; "
            f"border:2px solid {border}; font-weight:700;'>{mark}</span>"
            f"<div style='margin-top:6px; font-size:13px; color:{label_color}; "
            f"font-weight:{700 if now else 500};'>{label}</div></div>"
        )
    connector = "<div style='flex:0 0 28px; height:2px; background:#E5E7EB; margin-top:17px;'></div>"
    st.markdown(
        "<div style='display:flex; align-items:flex-start; max-width:540px; margin:0 auto 28px;'>"
        + connector.join(parts) + "</div>",
        unsafe_allow_html=True,
    )

# 「操作」はプルダウンから選ばせる（自由入力で迷わせない）
ACTION_OPTIONS = ["文字を入力", "クリック", "選択", "チェック"]

# 🚀 送信（申請）ステップ：本番でのみ実行する最後の一押し。robot.py の SUBMIT_MARKERS と対応。
SUBMIT_WHEN_LABEL = "送信（本番のみ）"
SUBMIT_WHEN_SET = {
    "送信", "申請", "送信する", "申請する",
    "送信（本番のみ）", "申請（本番のみ）", "送信(本番のみ)", "申請(本番のみ)",
    "送信時", "申請時", "最後に送信",
}

def _is_submit_when(condition) -> bool:
    return str(condition or "").strip() in SUBMIT_WHEN_SET
_ACTION_VERB = {
    "文字を入力": "を入力します", "クリック": "をクリックします",
    "選択": "を選びます", "チェック": "にチェックを入れます",
    "fill": "を入力します", "click": "をクリックします",
    "select": "を選びます", "check": "にチェックを入れます",
}
_TRANSFORM_HINT = {
    "市外局番": "の市外局番だけ", "市内局番": "の市内局番だけ", "加入者番号": "の加入者番号だけ",
    "ハイフン除去": "（ハイフンを除いて）", "数字のみ": "（数字だけ）",
    "郵便番号_上3桁": "の上3桁", "郵便番号_下4桁": "の下4桁",
}

def describe_step(step: dict) -> str:
    """1つの手順を、裏側を知らない人向けのやさしい日本語の文章にする。"""
    target = str(step.get("対象", step.get("target_description", "")) or "").strip()
    action = str(step.get("操作", step.get("action", "")) or "").strip()
    value = str(step.get("値", step.get("value", "")) or "").strip()
    transform = str(step.get("変換", step.get("transform", "")) or "").strip()
    verb = _ACTION_VERB.get(action, "を操作します")

    placeholders = re.findall(r"\{(.+?)\}", value)
    if placeholders:
        val_txt = "・".join(f"お客様の【{p}】" for p in placeholders)
    elif value:
        val_txt = f"「{value}」"
    else:
        val_txt = ""
    if transform in _TRANSFORM_HINT and val_txt:
        val_txt += _TRANSFORM_HINT[transform]

    target_txt = f"「{target}」" if target else "画面の項目"
    if action in ["文字を入力", "fill", "選択", "select"] and val_txt:
        return f"{target_txt}に {val_txt}{verb}"
    return f"{target_txt}{verb}"


# ==========================================
# 🏠 画面1: ホーム（ロボット一覧）
# ==========================================
if st.session_state.view == 'dashboard':
    st.markdown("<div class='wizard-header'><h1>🤖 エンカンAI：ホーム</h1><p>あなたが作った自動化ロボットたちがここに集まります。</p></div>", unsafe_allow_html=True)

    # 完成までの流れを、はじめての人にも一目で
    st.markdown("""
    <div style='display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:-6px 0 18px;'>
      <span style='background:#E0F2FE;color:#0369A1;font-weight:700;border-radius:999px;padding:5px 14px;'>① 名前とスプシ</span>
      <span style='color:#94A3B8;'>→</span>
      <span style='background:#E0F2FE;color:#0369A1;font-weight:700;border-radius:999px;padding:5px 14px;'>② お手本を録画</span>
      <span style='color:#94A3B8;'>→</span>
      <span style='background:#E0F2FE;color:#0369A1;font-weight:700;border-radius:999px;padding:5px 14px;'>③ 確認・テストで完成</span>
    </div>
    """, unsafe_allow_html=True)

    # 空の箱を作らず、右寄せでボタンを配置
    _, col_add = st.columns([4, 1])
    with col_add:
        if st.button("＋ 新しいロボットを作る", type="primary", use_container_width=True):
            st.session_state.view = 'step1_basic'
            st.rerun()

    projects = supabase.table("merchants").select("*").execute().data or []
    if not projects:
        st.info("まだロボットがいません。上の「＋ 新しいロボットを作る」から、最初の1台をつくりましょう！")
    else:
        cols = st.columns(3)
        for i, proj in enumerate(projects):
            with cols[i % 3]:
                # 💡 HTMLのdivを使わず、Streamlitのcontainerで枠を固定します
                with st.container(border=True):
                    st.markdown(f"### {proj['name']}")
                    
                    # 稼働状態のバッジ表示
                    status_text = "✨ 稼働中" if proj['is_active'] else "💤 おやすみ中"
                    st.markdown(f"<span class='{'status-active' if proj['is_active'] else 'status-inactive'}'>{status_text}</span>", unsafe_allow_html=True)
                    
                    st.markdown("<br>", unsafe_allow_html=True)
                    c_metric1, c_metric2 = st.columns(2)
                    c_metric1.metric("未処理", "—")
                    c_metric2.metric("本日完了", "—")
                    st.caption("※件数の自動集計は準備中です")
                    
                    st.markdown("<br>", unsafe_allow_html=True)
                    # ボタン配置：横並びを維持しつつ枠内に収める
                    col_btn1, col_btn2, col_btn3 = st.columns([1.2, 1, 1])
                    with col_btn1:
                        if st.button("✏️ 設定", key=f"edit_{proj['id']}", use_container_width=True):
                            st.session_state.editing_project = proj['id']
                            st.session_state.view = 'project_room'
                            st.rerun()
                    with col_btn2:
                        # トグルスイッチも枠内に綺麗に配置
                        if st.toggle("稼働", value=proj['is_active'], key=f"tog_{proj['id']}") != proj['is_active']:
                            supabase.table("merchants").update({"is_active": not proj['is_active']}).eq("id", proj['id']).execute()
                            st.rerun()
                    with col_btn3:
                        if st.button("🗑 削除", key=f"del_{proj['id']}", use_container_width=True):
                            delete_project(proj['id'])
                            st.rerun()

# ==========================================
# 📝 画面2: STEP 1（基本とトリガー）
# ==========================================
elif st.session_state.view == 'step1_basic':
    render_stepper(0)
    st.markdown("<div class='wizard-header'><h2>🟢 STEP 1：まずはロボットの「名前」と「仕事場所」を決めましょう</h2><p>むずかしい設定はありません。下の空欄をうめるだけでOKです。</p></div>", unsafe_allow_html=True)
    ch.guide("create", "まずはロボットに<b>名前</b>をつけて、データの置き場所（SFAスプシ）を教えてね。ここはうめるだけだから安心して！")
    if st.button("⬅ ホームに戻る"): st.session_state.view = 'dashboard'; st.rerun()

    with st.container(border=True):
        st.markdown("<div class='section-title'>📋 ロボットのなまえ</div>", unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            new_name = st.text_input("なまえをつけてください", placeholder="例：ドコモ光の申込ロボ",
                                     help="ロボットを見分ける名前です。あとから変更できないので、短く分かりやすい名前にしてください。")
            st.caption("⚠️ 他のロボットと同じ名前にすると上書きされます。重複しない名前を。")
        with col2: product_type = st.selectbox("仕事の種類（商材）", ["ネット", "電気", "ガス", "その他"])

    with st.container(border=True):
        st.markdown("<div class='section-title'>📊 どこからデータを取りますか？</div>", unsafe_allow_html=True)
        sheet_url = st.text_input("SFA（スプレッドシート）のURL", placeholder="https://docs.google.com/spreadsheets/d/...")
        active_tab = st.text_input("読み込むタブの名前", placeholder="例：INE用")
        st.caption("※ロボットはこのスプシの「ステータス」が「未エントリー」の案件を自動で見つけます。")

    if st.button("次へ進む ➡️", type="primary"):
        if not new_name or not sheet_url: st.error("なまえとスプシのURLは必ず入力してください！")
        else:
            new_data = {
                "id": new_name, "name": new_name, "is_active": False, "connector_type": "playwright",
                "config_json": {
                    "product_type": product_type,
                    "spreadsheet": {"url": sheet_url, "tab_name": active_tab, "trigger_col": "ステータス", "trigger_val": "未エントリー"},
                    "robot_config": {"target_url": "", "steps": [], "stealth": True, "captcha": False, "success_text": ""},
                    "notifications": {"slack_id": "", "slack_msg": "自動申請が完了しました。"},
                    "conditions": []
                }
            }
            save_project(new_name, new_data)
            st.session_state.editing_project = new_name
            st.session_state.view = 'step2_record'
            st.rerun()

# ==========================================
# 🎥 画面3: STEP 2（AI学習/録画）
# ==========================================
elif st.session_state.view == 'step2_record':
    project_id = st.session_state.editing_project
    proj_data = get_project_data(project_id)
    config = proj_data["config_json"]

    render_stepper(1)
    st.markdown("<div class='wizard-header'><h2>🎥 STEP 2：お手本を一度だけ見せてください</h2><p>あなたが申込フォームに1件入力する様子を記録すると、AIが「手順書」を自動で作ります。プログラムの知識はいりません。</p></div>", unsafe_allow_html=True)
    ch.guide("create", "ここがぼくの本番！あなたが1件入力するところを<b>録画</b>してくれたら、その操作からぼくが手順書を書き起こすよ。むずかしい言葉は分からなくて大丈夫。")
    if st.button("⬅ ホームに戻る"): st.session_state.view = 'dashboard'; st.rerun()

    with st.container(border=True):
        st.markdown("<div class='section-title'>🌐 ① 入力先のWebサイトを教えてください</div>", unsafe_allow_html=True)
        target_url = st.text_input("自動入力させたいフォームのURL", value=config["robot_config"].get("target_url", ""),
                                   placeholder="https://...")

    if target_url:
        with st.container(border=True):
            st.markdown("<div class='section-title'>🎥 ② お手本を記録する</div>", unsafe_allow_html=True)
            st.markdown("""
            <div style='font-size:15px; line-height:1.9;'>
              <b>1.</b> 下の「録画スタート」を押すと、記録用のブラウザが開きます。<br>
              <b>2.</b> いつも通り、<b>申請ボタンを押す“直前”まで</b>テスト用のお客様データを1件だけ入力してください。<br>
              <b>3.</b> 一緒に開いた小さな画面の文字を<b>すべて選んでコピー</b>し、下の枠に貼り付けます。
            </div>
            """, unsafe_allow_html=True)
            st.info("🧩 途中で「私はロボットではありません（画像パズル）」が出たら、ブラウザを閉じて、もう一度「録画スタート」からやり直してください。")
            st.warning("⚠️ **個人情報は入力しないでください。** お名前・電話番号・住所などは、必ず架空のテストデータ"
                       "（例：「自動化 太郎」）を使ってください。ここで入力した内容はAI（Gemini）に送られ、手順書にもそのまま保存されます。")

            st.caption("💻 録画は、この画面を**自分のPCで開いているとき**だけ使えます（記録用ブラウザがそのPCに開きます）。"
                       "クラウド上の画面では録画ブラウザは表示されません。")
            if st.button("▶ 録画スタート"):
                try:
                    subprocess.Popen([sys.executable, "-m", "playwright", "codegen", target_url])
                    st.success("記録用ブラウザを開きました。お手本の入力をして、出てきた文字を下に貼り付けてください。")
                except Exception as e:
                    st.error(f"録画ブラウザを開けませんでした（PCで開いていない可能性があります）。詳細: {e}")

        recorded_code = st.text_area("📋 ③ コピーした文字をここに貼り付け", height=200,
                                     placeholder="録画画面に出てきた文字を、まるごと貼り付けてください")

        with st.expander("😟 うまくいかない・むずかしいと感じたら"):
            st.markdown("""
            - **コピーする文字がどれか分からない**：記録用ブラウザと一緒に開く小さな画面（コードが出る画面）の中身を、全部選んで貼り付ければOKです。中身が分からなくても大丈夫です。<br>
            - **貼り付けても先に進めない**：枠が空のままだと進めません。何か貼り付けてからもう一度お試しください。<br>
            - **それでも難しい**：管理者に「録画した画面のコピー」を送って、代わりに貼り付けてもらってもOKです。
            """, unsafe_allow_html=True)
        
        if st.button("✨ エンカンAIに手順書を作ってもらう", type="primary"):
            if recorded_code:
                with st.spinner("🤖 AIがコードを解析中... しばらくお待ちください。"):
                    try:
                        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                        model = genai.GenerativeModel('gemini-2.5-flash')
                        # 💡 余計な指示は削り、元のシンプルなプロンプトに戻しました
                        prompt = f"""
                        あなたはRPAエンジニアです。以下のPlaywrightコードを解析し、日本語の手順表を作成してください。
                        電話番号や郵便番号などの分割枠は、必ず `.split('-')[0]` のようにPythonの分割コードを `ai_code` に組み込んでください。
                        絶対に出力は以下のJSON配列のみとしてください。
                        [ {{"順番": 1, "いつ": "常に", "操作": "文字を入力", "対象": "お名前", "値": "{{顧客_氏名}}", "ai_code": "..."}} ]
                        コード：\n{recorded_code}
                        """
                        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
                        config["robot_config"]["target_url"] = target_url
                        config["robot_config"]["steps"] = json.loads(response.text)
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                        st.toast("✅ 手順書ができました！内容を確認しましょう。", icon="🎬")
                        st.session_state.view = 'project_room'; st.rerun()
                    except Exception as e:
                        st.error(f"うまく手順書を作れませんでした。貼り付けた内容をもう一度ご確認ください。（詳細: {e}）")

# ==========================================
# 🎛️ 画面4: 司令室（詳細設定とテスト）
# ==========================================
elif st.session_state.view == 'project_room':
    project_id = st.session_state.editing_project
    proj_data = get_project_data(project_id)
    config = proj_data["config_json"]
    steps_data = config.get("robot_config", {}).get("steps", [])
    
    render_stepper(2)
    st.markdown(f"<div class='wizard-header'><h2>🎛️ 仕上げ：{proj_data['name']}</h2><p>あと少しです！ロボットの動きを確認して、テストすれば完成です。</p></div>", unsafe_allow_html=True)
    ch.guide("create", "できあがった手順を一緒に確認しよう。下の<b>「このロボットの動き」</b>を読んで、違っていたら手順書の表で直してね。最後に<b>お試し実行</b>すれば完成だよ！")

    if st.button("⬅ ホームへ戻る"): st.session_state.view = 'dashboard'; st.rerun()

    # 0. このロボットが何をするかを「やさしい日本語」で先に見せる（表を読めなくても分かる）
    valid_steps = [s for s in steps_data if s and (s.get("操作") or s.get("action"))]
    with st.container(border=True):
        st.markdown("<div class='section-title'>👀 このロボットの動き（かんたん確認）</div>", unsafe_allow_html=True)
        if not valid_steps:
            st.info("まだ手順がありません。STEP2の録画でお手本を見せるか、下の表に手順を追加してください。")
        else:
            ordered = sorted(valid_steps, key=lambda x: x.get("順番", x.get("order", 999)))
            lines = []
            has_submit = False
            for s in ordered:
                cond = str(s.get("いつ", s.get("condition", "常に")) or "常に")
                if _is_submit_when(cond):  # 🚀 送信（申請）ステップは特別に表示
                    has_submit = True
                    tgt = str(s.get("対象", s.get("target_description", "")) or "申請ボタン").strip() or "申請ボタン"
                    lines.append(
                        f"<li style='margin-bottom:8px;'><b style='color:#C2410C;'>🚀 本番だけ：</b>"
                        f"「{tgt}」を押して<b>申請を送信</b>します"
                        f"<span style='color:#9CA3AF;'>（お試しでは押しません）</span></li>")
                    continue
                cond_txt = "" if cond in ["", "常に", "always"] else f" <span style='color:#0369A1; font-weight:700;'>（{cond} のときだけ）</span>"
                lines.append(f"<li style='margin-bottom:8px;'>{describe_step(s)}{cond_txt}</li>")
            st.markdown(f"<ol style='font-size:15px; line-height:1.7; padding-left:22px;'>{''.join(lines)}</ol>", unsafe_allow_html=True)
            st.caption("👆 ロボットはこの順番で自動入力します。違っていたら、下の「手順書」の表で直してください。")
            if not has_submit:
                st.warning("⚠️ まだ『送信（申請）ステップ』がありません。このままだと本番でも"
                           "**申請ボタンが押されず、申し込みが完了しません**。下の手順書の下にある"
                           "「🚀 送信ステップを追加」で最後の一押しを設定してください。")

    # 1. 基本設定（後から編集可能）
    with st.expander("📝 基本設定の書き換え（URLなど）"):
        c1, c2 = st.columns(2)
        with c1:
            e_sheet = st.text_input("SFAスプシURL", value=config.get('spreadsheet', {}).get('url', ''))
            e_tab = st.text_input("タブ名", value=config.get('spreadsheet', {}).get('tab_name', ''))
        with c2:
            e_target = st.text_input("入力フォームURL", value=config.get('robot_config', {}).get('target_url', ''))
        st.caption("※動かす条件は「ステータス」が「未エントリー」の案件で固定されています。")

    # 🧮 カラム設計（●●BOXシートの作成・修正） — V1：AIに相談して確認してから反映
    with st.container(border=True):
        st.markdown("<div class='section-title'>🧮 カラム設計（●●BOXシートの作成・修正）</div>", unsafe_allow_html=True)
        st.caption("SFAスプシの『BOX』から商品ごとに抽出する『●●BOX』シートを、AIに相談しながら作成・修正できます。")

        gc = _get_gspread_client()
        if gc is None:
            st.warning("⚠️ この機能を使うには、接続キーに`GOOGLE_SERVICE_ACCOUNT_JSON`（サービスアカウント）の設定が必要です。")
        else:
            # 入力欄に今入っている値を優先（保存前でも、直近に入力したURLで検索できるように）
            box_sheet_url = (e_sheet or config.get('spreadsheet', {}).get('url', '')).strip()
            if not box_sheet_url:
                st.info("先に上の「基本設定の書き換え」でSFAスプシURLを設定してください。")
            else:
                try:
                    existing_box_sheets = _list_box_sheet_names(gc, box_sheet_url)
                except Exception as e:
                    existing_box_sheets = []
                    st.error(f"シート一覧の取得に失敗しました: {e}")

                if not existing_box_sheets:
                    st.info("『BOX』という文字を含むシートが、このスプシの中にまだ見つかりません。")
                with st.expander("🔍 このスプシの全タブ名を確認する"):
                    try:
                        st.write(_list_all_sheet_names(gc, box_sheet_url))
                    except Exception as e:
                        st.error(f"タブ一覧の取得に失敗しました: {e}")

                try:
                    master_headers = _read_master_box_headers(gc, box_sheet_url)
                except Exception:
                    master_headers = []

                col_mode = st.radio("何をしますか？", ["新しい商品のBOXシートを作る", "既存のBOXシートを直す"],
                                    key=f"box_mode_{project_id}", horizontal=True)

                is_new = (col_mode == "新しい商品のBOXシートを作る")
                if is_new:
                    new_product_name = st.text_input("商品名（●●の部分）", placeholder="例：ドコモ光INE",
                                                      key=f"box_new_name_{project_id}")
                    ref_tab = st.selectbox("参考にする既存のBOXシート", existing_box_sheets,
                                           key=f"box_ref_{project_id}") if existing_box_sheets else None
                    target_tab_name = f"{new_product_name}BOX" if new_product_name else ""
                else:
                    new_product_name = ""
                    ref_tab = st.selectbox("直したいBOXシート", existing_box_sheets,
                                           key=f"box_edit_target_{project_id}") if existing_box_sheets else None
                    target_tab_name = ref_tab

                # 📋 大元の『BOX』見出しと、選んだシートの列一覧（A列・B列…付き）を上下に表示する
                if master_headers:
                    st.markdown("**大元の「BOX」シートの列一覧**")
                    master_lines = "\n".join(f"{_col_letter(i+1)}列: {h}" for i, h in enumerate(master_headers))
                    st.code(master_lines, language="text")

                if ref_tab:
                    try:
                        ref_headers, ref_formula = _read_box_sheet(gc, box_sheet_url, ref_tab)
                        st.markdown(f"**「{ref_tab}」の列一覧**")
                        col_lines = "\n".join(f"{_col_letter(i+1)}列: {h}" for i, h in enumerate(ref_headers))
                        st.code(col_lines or "(列が見つかりません)", language="text")
                        if not is_new:
                            st.caption(f"今のA2セルの数式: `{ref_formula}`")
                    except Exception as e:
                        st.error(f"「{ref_tab}」の列一覧の取得に失敗しました: {e}")
                        ref_headers, ref_formula = [], ""
                else:
                    ref_headers, ref_formula = [], ""

                if is_new:
                    condition_desc = st.text_area("抽出条件を説明してください（上の列一覧を見ながら書けます）",
                                                  placeholder="例：B列が「ドコモ光」、BO列が「INE」の行を抽出したい",
                                                  key=f"box_cond_{project_id}")
                else:
                    condition_desc = st.text_area("どう直したいか説明してください（上の列一覧を見ながら書けます）",
                                                  placeholder="例：キャンペーン列（BQ列）も条件に追加したい",
                                                  key=f"box_editcond_{project_id}")

                if st.button("🤖 AIに数式を相談する", key=f"box_ask_{project_id}"):
                    if not ref_tab:
                        st.warning("参考にする（または直したい）BOXシートを選んでください。")
                    elif not condition_desc:
                        st.warning("条件・変更内容を説明してください。")
                    elif is_new and not new_product_name:
                        st.warning("商品名を入力してください。")
                    else:
                        with st.spinner("🤖 AIが数式を考えています..."):
                            try:
                                draft = _draft_box_formula(ref_tab, ref_headers, ref_formula,
                                                           target_tab_name, condition_desc, is_new)
                                st.session_state[f"box_draft_{project_id}"] = {
                                    "tab_name": target_tab_name, "headers": draft["headers"],
                                    "formula": draft["formula"], "is_new": is_new,
                                    "old_headers": ref_headers if not is_new else None,
                                    "old_formula": ref_formula if not is_new else None,
                                }
                            except Exception as e:
                                st.error(f"数式の作成に失敗しました: {e}")

                draft_key = f"box_draft_{project_id}"
                if draft_key in st.session_state:
                    d = st.session_state[draft_key]
                    st.markdown("---")
                    st.markdown(f"**提案：「{d['tab_name']}」**")
                    if not d["is_new"]:
                        st.markdown("**今の状態**")
                        st.code(f"見出し: {d['old_headers']}\n数式: {d['old_formula']}", language="text")
                        st.markdown("**新しい状態（案）**")
                    st.code(f"見出し: {d['headers']}\n数式: {d['formula']}", language="text")

                    cb1, cb2 = st.columns(2)
                    with cb1:
                        if st.button("✅ この内容で反映する", key=f"box_apply_{project_id}", type="primary"):
                            try:
                                _apply_box_sheet(gc, box_sheet_url, d["tab_name"], d["headers"], d["formula"], d["is_new"])
                                st.success(f"「{d['tab_name']}」に反映しました！")
                                del st.session_state[draft_key]
                                st.rerun()
                            except Exception as e:
                                st.error(f"反映に失敗しました: {e}")
                    with cb2:
                        if st.button("✖ 取り消す", key=f"box_cancel_{project_id}"):
                            del st.session_state[draft_key]
                            st.rerun()

    # 🧩 最終シートの列・数式作成＋手順書への自動反映（機能B・C）
    with st.container(border=True):
        st.markdown("<div class='section-title'>🧩 最終シートの列・数式作成（録画・手順書と連携）</div>", unsafe_allow_html=True)
        st.caption("録画で必要になった項目ごとに、●●BOXのどの列をどう反映したいかAIに相談します。"
                   "反映すると、最終シートの列と、手順書のプレースホルダーの両方が同時に更新されます。")

        if gc is None:
            st.info("上の「カラム設計」と同じく、サービスアカウントの設定が必要です。")
        else:
            # 入力欄に今入っている値を優先（保存前でも直近のタブ名で扱えるように）
            final_tab_name = (e_tab or config.get('spreadsheet', {}).get('tab_name', '')).strip()
            if not final_tab_name:
                st.info("先に「基本設定の書き換え」で最終シートの「タブ名」を設定してください。")
            else:
                st.caption(f"最終シート（●●）は「{final_tab_name}」として扱います（基本設定の「タブ名」と同じ）。")
                try:
                    box_choices_for_final = _list_box_sheet_names(gc, box_sheet_url)
                except Exception:
                    box_choices_for_final = []
                box_ref_for_final = (st.selectbox("参照する●●BOXシート", box_choices_for_final,
                                                   key=f"final_box_ref_{project_id}")
                                      if box_choices_for_final else None)

                candidates = _get_candidate_fields(config)
                if not candidates:
                    st.info("録画済みの手順が無いため、項目の候補を自動検出できません。下に直接入力してください（ダウンロード系の商品など）。")
                    target_field = st.text_input("項目名を直接入力", key=f"final_manual_field_{project_id}")
                else:
                    field_options = [c["target"] for c in candidates]
                    target_field = st.selectbox("どの項目について相談しますか？（録画から自動検出）", field_options,
                                                key=f"final_field_select_{project_id}")

                box_headers_for_final, final_headers, final_formulas = [], [], []
                if target_field and box_ref_for_final:
                    try:
                        box_headers_for_final, _ = _read_box_sheet(gc, box_sheet_url, box_ref_for_final)
                        st.markdown(f"**「{box_ref_for_final}」の列一覧**")
                        st.code("\n".join(f"{_col_letter(i+1)}列: {h}" for i, h in enumerate(box_headers_for_final)),
                               language="text")
                    except Exception as e:
                        st.error(f"列一覧の取得に失敗しました: {e}")
                    try:
                        final_headers, final_formulas = _read_final_sheet(gc, box_sheet_url, final_tab_name)
                    except Exception as e:
                        st.error(f"最終シート「{final_tab_name}」の読み込みに失敗しました: {e}")

                    field_desc = st.text_area(
                        f"「{target_field}」をどう反映したいか説明してください（説明せずスキップしてもOK）",
                        key=f"final_desc_{project_id}")

                    fb1, fb2 = st.columns(2)
                    with fb1:
                        ask_final = st.button("🤖 AIに数式を相談する", key=f"final_ask_{project_id}")
                    with fb2:
                        skip_final = st.button("⏭ この項目はスキップ（数式なし）", key=f"final_skip_{project_id}")

                    if ask_final:
                        if not field_desc:
                            st.warning("どう反映したいか説明してください。")
                        else:
                            with st.spinner("🤖 AIが数式を考えています..."):
                                try:
                                    draft2 = _draft_final_column_formula(
                                        box_ref_for_final, box_headers_for_final,
                                        final_headers, final_formulas, field_desc, target_field)
                                    st.session_state[f"final_draft_{project_id}"] = {
                                        "target_field": target_field,
                                        "column_name": draft2["column_name"],
                                        "formula": draft2["formula"],
                                    }
                                except Exception as e:
                                    st.error(f"数式の作成に失敗しました: {e}")

                    if skip_final:
                        st.info(f"「{target_field}」は数式なしのままにします（何も変更しません）。")

                draft2_key = f"final_draft_{project_id}"
                if draft2_key in st.session_state:
                    d2 = st.session_state[draft2_key]
                    st.markdown("---")
                    st.markdown(f"**提案：列「{d2['column_name']}」**")
                    st.code(d2["formula"], language="text")
                    st.caption(f"反映すると、手順「{d2['target_field']}」のプレースホルダーも"
                              f"`{{{d2['column_name']}}}` に揃います。")

                    fa1, fa2 = st.columns(2)
                    with fa1:
                        if st.button("✅ 最終シート＋手順書の両方に反映する", key=f"final_apply_{project_id}", type="primary"):
                            try:
                                _apply_final_column(gc, box_sheet_url, final_tab_name, final_headers,
                                                    d2["column_name"], d2["formula"])
                                new_steps = _sync_placeholder_in_steps(
                                    config.get("robot_config", {}).get("steps", []),
                                    d2["target_field"], d2["column_name"])
                                config["robot_config"]["steps"] = new_steps
                                proj_data["config_json"] = config
                                save_project(project_id, proj_data)
                                st.success(f"「{final_tab_name}」の列「{d2['column_name']}」と、"
                                          "手順書のプレースホルダーの両方に反映しました！")
                                del st.session_state[draft2_key]
                                st.rerun()
                            except Exception as e:
                                st.error(f"反映に失敗しました: {e}")
                    with fa2:
                        if st.button("✖ 取り消す", key=f"final_cancel_{project_id}"):
                            del st.session_state[draft2_key]
                            st.rerun()

    # 2. 自動で書き換わる言葉のリスト（カンペ）
    with st.container(border=True):
        st.markdown("<div class='section-title'>💡 自動で書き換わる言葉の一覧（カンペ）</div>", unsafe_allow_html=True)
        st.write("手順書の「値」の欄に、以下の書き方で入力すると、ロボットが自動でSFAのデータに置き換えて入力します。")
        st.code("{{顧客_氏名}}  {{電話番号}}  {{郵便番号}}  {{住所}}  {{メールアドレス}}", language="text")
        st.markdown("""
        <div style='font-size: 14px; color: #333; margin-top: 8px; line-height: 1.7;'>
            <strong style='color:#0369A1;'>🔁 値の加工：</strong> 電話番号を3つの枠に分けたい等は、「値」に <code>{電話番号}</code> を入れて、
            <strong>「値の加工」列</strong>で <em>市外局番／市内局番／加入者番号</em> などを選ぶだけ（コード不要）。<br>
            <strong style='color:#0369A1;'>🔀 条件で違う値を入れたい：</strong> 同じ「対象」の手順を複数行つくり、それぞれの
            <strong>「いつ」</strong>に別々のルールを指定します（例：商材がドコモ光の行と、au光の行）。条件に合った行だけが実行されます。
        </div>
        """, unsafe_allow_html=True)

    # 3. 増えてきた設定は折りたたみに収納してスッキリ！
    with st.expander("⚙️ ロボットの拡張設定（通知・セキュリティなど）"):
        # ✅ 申請完了の確認サイン（偽成功を防ぐ重要設定）
        success_text = st.text_input("✅ 申請完了の合図（完了画面に出る文言）",
                                     value=config["robot_config"].get("success_text", ""),
                                     placeholder="例：お申し込みを受け付けました")
        st.caption("📌 申請ボタンを押した後の「完了画面」に必ず出る文言を入れてください。"
                   "これを設定すると、本番で**申請が本当に通ったかを確認**し、失敗していたら自動でやり直せます（空のままだと確認できません）。")
        st.markdown("---")
        c_s1, c_s2 = st.columns(2)
        with c_s1:
            stealth_mode = st.checkbox("人間らしくゆっくり操作する", value=config["robot_config"].get("stealth", True), key="stealth")
            st.caption("※ONにすると、クラウドでも操作をゆっくりにしてボット検知を受けにくくします。")
            captcha_break = st.checkbox("画像パズル(CAPTCHA)の自動突破（準備中）", value=config["robot_config"].get("captcha", False), key="captcha", disabled=True)
            st.caption("🚧 自動突破は準備中です。画像パズルを検出したら、設定に関わらず**常に**送信せず安全に停止します（誤申請防止・設定不要）。")
        with c_s2:
            slack_ch = st.text_input("Slackの通知先チャンネル名（目印）", value=config["notifications"].get("slack_id", ""))
            slack_msg = st.text_area("完了時の通知メッセージ", value=config["notifications"].get("slack_msg", "自動申請が完了しました。"))
            st.caption("🔔 通知には別途 **Slack Incoming WebhookのURL**（SLACK_WEBHOOK_URL）の設定が必要です。"
                       "投稿先チャンネルはWebURL側で決まるため、上の欄は本文に付く目印です。`{氏名}`等でデータも差し込めます。")

    # 4. 条件分岐ルール（パターン）の作成 — コードを書かずに「もし〇〇なら」を設定
    # プルダウンの表示名 → robot.py の演算子キー
    OP_OPTIONS = {
        "一致する": "eq",
        "一致しない": "ne",
        "含む": "contains",
        "含まない": "not_contains",
        "空である": "empty",
        "空でない": "not_empty",
        "以上": "gte",
        "より大きい": "gt",
        "以下": "lte",
        "より小さい": "lt",
        "いずれかと一致(カンマ区切り)": "in",
    }
    with st.container(border=True):
        st.markdown("<div class='section-title'>🔀 条件分岐ルール（パターン）の作成</div>", unsafe_allow_html=True)
        st.caption("「この列がこういう値のときだけ実行する手順」をルールとして作ります。下の手順書の『いつ』でこの名前を選ぶと、その条件のときだけ実行されます。")

        # --- 既存ルールの一覧表示（確認・削除） ---
        existing_conditions = config.get("conditions", [])
        if existing_conditions:
            st.markdown("**📋 作成済みのルール**")
            for gi, grp in enumerate(existing_conditions):
                with st.container(border=True):
                    cga, cgb = st.columns([6, 1])
                    with cga:
                        logic_label = "すべて満たす（AND）" if str(grp.get("logic", "AND")).upper() == "AND" else "いずれか満たす（OR）"
                        rules = grp.get("rules", [])
                        st.markdown(f"**🏷 {grp.get('name', '(無名)')}**　<small style='color:#0369A1;'>結合: {logic_label}</small>", unsafe_allow_html=True)
                        if rules:
                            for r in rules:
                                op_label = next((k for k, v in OP_OPTIONS.items() if v == r.get("op")), r.get("op", ""))
                                st.markdown(f"　・「{r.get('col', '')}」が「{r.get('value', '')}」に **{op_label}**")
                        else:
                            st.markdown("　<span style='color:#EF4444;'>※条件が未設定です。下の枠から条件を追加してください。</span>", unsafe_allow_html=True)
                    with cgb:
                        if st.button("🗑 削除", key=f"delrule_{gi}"):
                            config["conditions"].pop(gi)
                            proj_data["config_json"] = config
                            save_project(project_id, proj_data)
                            st.rerun()

        # --- 条件の追加 ---
        st.markdown("**＋ 条件を追加する**")
        st.caption("同じ『ルールの名前』で条件を足すと、複数条件のルールになります（結合のAND/ORで挙動が変わります）。")
        c_r1, c_r2, c_r3, c_r4, c_r5 = st.columns([2, 2, 1.6, 2, 1])
        with c_r1: c_name = st.text_input("ルールの名前", placeholder="例：未成年ルート", key="rule_name")
        with c_r2: c_col = st.text_input("SFAの項目名（列）", placeholder="例：年齢", key="rule_col")
        with c_r3: c_op_label = st.selectbox("条件", list(OP_OPTIONS.keys()), key="rule_op")
        with c_r4: c_val = st.text_input("値", placeholder="例：20", key="rule_val")
        with c_r5:
            c_logic = st.selectbox("結合", ["AND", "OR"], key="rule_logic",
                                   help="同じ名前のルールに条件を足したとき、すべて満たす(AND)か、いずれか(OR)か")
        if st.button("この条件をルールに追加"):
            if c_name and c_col:
                op_key = OP_OPTIONS[c_op_label]
                new_rule = {"col": c_col, "op": op_key, "value": c_val}
                conds = config.setdefault("conditions", [])
                grp = next((g for g in conds if g.get("name") == c_name), None)
                if grp is None:
                    conds.append({"name": c_name, "logic": c_logic, "rules": [new_rule]})
                else:
                    grp["logic"] = c_logic
                    grp.setdefault("rules", []).append(new_rule)
                proj_data["config_json"] = config
                save_project(project_id, proj_data)
                st.rerun()
            else:
                st.warning("「ルールの名前」と「SFAの項目名（列）」は必ず入力してください。")

    # 5. 手順書の確認と編集
    with st.container(border=True):
        st.markdown("<div class='section-title'>📝 自動入力の手順書（こまかい修正用）</div>", unsafe_allow_html=True)

        # やさしい表示と上級者モードの切り替え
        easy_mode = st.toggle("やさしい表示（むずかしい列をかくす・おすすめ）", value=True, key=f"easy_{project_id}")

        if easy_mode:
            st.markdown("""
            <div style='background:#F0F9FF; padding:16px; border-radius:12px; border:1px solid #BAE6FD; margin-bottom:16px; font-size:14px; line-height:1.7;'>
                <b style='color:#0369A1;'>表の見かた</b><br>
                ・<b>対象</b>＝画面のどの欄か（例：お名前）　・<b>操作</b>＝何をするか（プルダウンで選ぶ）<br>
                ・<b>値</b>＝入れる内容。お客様データは <code>{氏名}</code> のように波カッコで。毎回同じ文字はそのまま入力。<br>
                ・<b>値の加工</b>＝電話番号を分けたい等のときだけ選ぶ　・<b>いつ</b>＝条件のときだけ動かしたいとき選ぶ
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style='background:#FFF7ED; padding:16px; border-radius:12px; border:1px solid #FED7AA; margin-bottom:16px; font-size:14px; line-height:1.6;'>
                <b style='color:#C2410C;'>⚙️ 上級者モード：</b> 一番右の「最強の呪文（ai_code）」が表示されています。<br>
                自信がなければ<b>空っぽにしてOK</b>です。ロボットのAI自動検索が代わりに画面を探して入力します。
            </div>
            """, unsafe_allow_html=True)

        columns_order = ["順番", "いつ", "対象", "操作", "値", "変換", "ai_code"]
        TRANSFORM_OPTIONS = ["", "ハイフン除去", "数字のみ", "市外局番", "市内局番",
                             "加入者番号", "郵便番号_上3桁", "郵便番号_下4桁"]

        # 🚨 Noneバグ対策
        clean_steps = [step for step in steps_data if step and step.get("操作") is not None]

        df = pd.DataFrame(clean_steps)
        if df.empty: df = pd.DataFrame(columns=columns_order)
        else:
            for col in columns_order:
                if col not in df.columns: df[col] = None
            df = df[columns_order]

        # プルダウンの選択肢は、既存データに含まれる値も必ず含める（選択肢に無い値での表示エラー防止）
        def _ensure(options, series):
            extra = [v for v in series.dropna().unique().tolist() if v not in options and str(v) != ""]
            return options + extra

        conditions = config.get("conditions", [])
        condition_names = _ensure(["常に"] + [c["name"] for c in conditions] + [SUBMIT_WHEN_LABEL], df["いつ"])
        action_opts = _ensure(list(ACTION_OPTIONS), df["操作"])
        transform_opts = _ensure(list(TRANSFORM_OPTIONS), df["変換"])

        # やさしい表示では「呪文(ai_code)」列をかくす（値は保持される）
        visible_cols = ["順番", "いつ", "対象", "操作", "値", "変換"]
        if not easy_mode:
            visible_cols = visible_cols + ["ai_code"]

        edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key=f"editor_{project_id}",
                                   column_order=visible_cols,
                                   column_config={
                                       "いつ": st.column_config.SelectboxColumn("いつ実行するか", options=condition_names),
                                       "対象": st.column_config.TextColumn("対象（画面の欄）"),
                                       "操作": st.column_config.SelectboxColumn("操作", options=action_opts,
                                                                              help="この欄に何をする？（入力・クリックなど）"),
                                       "変換": st.column_config.SelectboxColumn("値の加工", options=transform_opts,
                                                                              help="スプシの値をそのまま入れず加工したいとき（例：電話番号→市外局番）"),
                                       "ai_code": st.column_config.TextColumn("最強の呪文（上級者向け・任意）")
                                   })
        
        # 🚀 送信（申請）ステップの追加 — 本番でだけ押す「最後の一押し」をワンクリックで用意
        existing_steps = config.get("robot_config", {}).get("steps", [])
        already_has_submit = any(_is_submit_when(s.get("いつ", s.get("condition", ""))) for s in existing_steps if s)
        st.markdown("---")
        st.markdown("**🚀 最後の一押し（送信／申請ボタン）**")
        if already_has_submit:
            st.success("✅ 『送信（申請）ステップ』は設定済みです。お試しでは押されず、本番でだけ実行されます。")
        else:
            st.caption("録画は申請ボタンの“直前”まででOK。最後に押す申請ボタンだけ、ここで1クリック追加します。"
                       "（このステップはお試しでは押さず、本番のクラウドLIVE実行でだけ押されます）")
            sb1, sb2 = st.columns([3, 1])
            with sb1:
                submit_label = st.text_input("申請（送信）ボタンの文言", value="申請する",
                                             key=f"submitlbl_{project_id}",
                                             help="サイト最後の送信ボタンに書かれている文字（例：申請する／送信／この内容で申し込む）")
            with sb2:
                st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
                if st.button("🚀 送信ステップを追加", key=f"addsubmit_{project_id}", use_container_width=True):
                    orders = [int(s.get("順番", s.get("order", 0)) or 0) for s in existing_steps if s]
                    next_order = (max(orders) if orders else 0) + 1
                    existing_steps.append({
                        "順番": next_order, "いつ": SUBMIT_WHEN_LABEL, "対象": (submit_label or "申請する"),
                        "操作": "クリック", "値": "", "変換": "", "ai_code": "",
                    })
                    config["robot_config"]["steps"] = existing_steps
                    proj_data["config_json"] = config
                    save_project(project_id, proj_data)
                    st.toast("🚀 送信ステップを追加しました", icon="✅")
                    st.rerun()
            st.caption("⚠️ 表で編集中の内容がある場合は、先に下の「💾 保存」をしてから追加してください（追加時に再読み込みされます）。")
        st.markdown("---")

        if st.button("💾 この内容で保存する", type="primary"):
            # 既存の spreadsheet 設定（dedup_cols 等）を消さないようにマージ更新する
            sheet_cfg = dict(config.get("spreadsheet", {}))
            sheet_cfg.update({"url": e_sheet, "tab_name": e_tab, "trigger_col": "ステータス", "trigger_val": "未エントリー"})
            config["spreadsheet"] = sheet_cfg
            config["robot_config"]["target_url"] = e_target
            config["robot_config"]["stealth"] = stealth_mode
            config["robot_config"]["captcha"] = captcha_break
            config["robot_config"]["success_text"] = success_text
            
            # 🚨 NaNエラー対策：空っぽのセルを安全な空文字("")に変換して保存する
            steps_to_save = []
            for row in edited_df.to_dict('records'):
                clean_row = {}
                for k, v in row.items():
                    if pd.isna(v):  # 空のセル(NaN)を検知
                        clean_row[k] = ""
                    else:
                        clean_row[k] = v
                steps_to_save.append(clean_row)
            config["robot_config"]["steps"] = steps_to_save
            
            config["notifications"]["slack_id"] = slack_ch
            config["notifications"]["slack_msg"] = slack_msg
            proj_data["config_json"] = config
            save_project(project_id, proj_data)
            st.toast("💾 保存しました！", icon="✅")
            st.success("設定と手順を保存しました！このあと下の「お試し実行」で動きを確認できます。")

    # 6. 最後にテスト
    with st.container(border=True):
        st.markdown("<div class='section-title'>🧪 さいごに、お試し実行してみましょう</div>", unsafe_allow_html=True)
        st.caption("お試しでは、ロボットが入力する様子を確認できます。"
                   "安全のため『送信（申請）ステップ』は押しません（本番のクラウドLIVE実行でだけ押されます）。")
        ct1, ct2 = st.columns(2)
        with ct1:
            if st.button("▶ お試し実行（申請ボタンの手前まで）", use_container_width=True):
                st.info("ロボットが動き出します。開いたブラウザを見守ってくださいね。")
                subprocess.Popen([sys.executable, "robot.py", project_id])
        with ct2:
            if st.button("✓ テストOK！ロボットを完成させる", type="primary", use_container_width=True):
                st.success("おめでとうございます！これで全自動化ロボットが完成しました。")
                proj_data["is_active"] = True
                save_project(project_id, proj_data)
                time.sleep(1); st.session_state.view = 'dashboard'; st.rerun()