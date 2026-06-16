import streamlit as st
import uuid
import pandas as pd
import time
import json
import re
import subprocess
import google.generativeai as genai
from supabase import create_client, Client

# --- ⚙️ システム設定 ---
st.set_page_config(page_title="エンカンAI - 事務作業の自動化パートナー", layout="wide")

# --- 🔗 データベース接続 ---
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase: Client = init_connection()

# --- 🎨 世界最高峰の「優しさ」UIデザイン（CSS） ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=M+PLUS+Rounded+1c:wght@400;500;700&display=swap');
    
    /* 全体を丸みのあるフォントへ */
    html, body, [class*="css"] {
        font-family: 'M PLUS Rounded 1c', sans-serif !important;
        color: #333333 !important;
    }
    
    .stApp { background-color: #FFFFFF; }
    
    /* 統一された「美しい枠」のデザイン */
    .enkan-card {
        background: #FFFFFF;
        padding: 24px;
        border-radius: 20px;
        border: 2px solid #E0F2FE;
        margin-bottom: 24px;
        box-shadow: 0 8px 20px rgba(14, 165, 233, 0.05);
    }
    
    /* 見出しのデザイン */
    .section-title {
        font-size: 20px;
        font-weight: 700;
        color: #0369A1;
        display: flex;
        align-items: center;
        margin-bottom: 16px;
    }
    
    /* ボタン（水色＋丸み＋影） */
    div[data-testid="stButton"] button {
        border-radius: 12px;
        font-weight: 700;
        border: 2px solid #BAE6FD;
        background-color: #FFFFFF;
        color: #0284C7;
        box-shadow: 0 4px 6px rgba(186, 230, 253, 0.2);
        transition: all 0.2s ease;
        width: auto; /* 横長になりすぎない */
        min-width: 120px;
    }
    div[data-testid="stButton"] button:hover {
        background-color: #F0F9FF;
        border-color: #38BDF8;
        transform: translateY(-2px);
    }
    
    /* 削除ボタンなどの特別な色 */
    .delete-btn button {
        color: #EF4444 !important;
        border-color: #FECACA !important;
    }
    
    /* 案内ヘッダー */
    .wizard-header {
        background: #F0F9FF;
        padding: 24px;
        border-radius: 16px;
        border-left: 8px solid #38BDF8;
        margin-bottom: 32px;
    }
</style>
""", unsafe_allow_html=True)

# --- 🧠 セッション管理 ---
if 'view' not in st.session_state: st.session_state.view = 'dashboard'
if 'editing_project' not in st.session_state: st.session_state.editing_project = None

# --- 🛠️ データベース操作 ---
def save_project(project_id, data): supabase.table("merchants").upsert(data).execute()
def get_project_data(project_id):
    res = supabase.table("merchants").select("*").eq("id", project_id).execute()
    return res.data[0] if res.data else None
def delete_project(project_id): supabase.table("merchants").delete().eq("id", project_id).execute()

# ==========================================
# 🏠 画面1: ホーム（ロボット一覧）
# ==========================================
if st.session_state.view == 'dashboard':
    st.markdown("<div class='wizard-header'><h1>🤖 エンカンAI：ホーム</h1><p>あなたが作った自動化ロボットたちがここに集まります。</p></div>", unsafe_allow_html=True)

    # 空の箱を作らず、右寄せでボタンを配置
    _, col_add = st.columns([4, 1])
    with col_add:
        if st.button("＋ 新しいロボットを作る", type="primary", use_container_width=True):
            st.session_state.view = 'step1_basic'
            st.rerun()

    projects = supabase.table("merchants").select("*").execute().data or []
    if not projects:
        st.info("まだロボットがいません。右上のボタンから「手本」を教えてあげましょう！")
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
                    c_metric1.metric("未処理", "12件")
                    c_metric2.metric("本日完了", "45件")
                    
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
    st.markdown("<div class='wizard-header'><h2>🟢 STEP 1：基本情報のセットアップ</h2><p>ロボットの名前と、どの仕事（スプシの行）を自動化するか決めましょう。</p></div>", unsafe_allow_html=True)
    if st.button("⬅ ホームに戻る"): st.session_state.view = 'dashboard'; st.rerun()

    with st.container(border=True):
        st.markdown("<div class='section-title'>📋 ロボットのなまえ</div>", unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1: new_name = st.text_input("なまえをつけてください", placeholder="例：ドコモ光の申込ロボ")
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
                    "robot_config": {"target_url": "", "steps": [], "stealth": True, "captcha": False},
                    "notifications": {"slack_id": "", "slack_msg": "自動申請が完了しました。"},
                    "conditions": []
                }
            }
            save_project(new_name, new_data)
            st.session_state.editing_project = new_name
            st.session_state.view = 'step2_record'
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# ==========================================
# 🎥 画面3: STEP 2（AI学習/録画）
# ==========================================
elif st.session_state.view == 'step2_record':
    project_id = st.session_state.editing_project
    proj_data = get_project_data(project_id)
    config = proj_data["config_json"]

    st.markdown("<div class='wizard-header'><h2>🎥 STEP 2：エンカンAIに手本を見せる</h2><p>あなたが一度だけ入力すれば、AIが完璧な手順書を書き上げます。</p></div>", unsafe_allow_html=True)
    
    with st.container(border=True):
        st.markdown("<div class='section-title'>🌐 入力先のWebサイト</div>", unsafe_allow_html=True)
        target_url = st.text_input("自動入力させたいフォームのURL", value=config["robot_config"].get("target_url", ""))

    if target_url:
        with st.container(border=True):
            st.markdown("<div class='section-title'>🎥 録画の手順</div>", unsafe_allow_html=True)
            st.write("1. 「録画スタート」を押すとブラウザが開きます。")
            st.write("2. **申請ボタンを押す直前**まで、実際のデータを1件分入力してください。")
            st.write("3. 一緒に開いた画面のコードをすべてコピーして、下の枠に貼り付けてください。")
            st.markdown("<p style='color: #EF4444; font-size: 14px; font-weight: bold;'>※もし途中で「私はロボットではありません（画像パズル）」が出た場合は、一旦ブラウザを閉じて、もう一度「録画スタート」からやり直してください。</p>", unsafe_allow_html=True)
            
            if st.button("▶ 録画スタート"):
                # 💡 インデントエラー（空白）だけを修正しました
                subprocess.Popen(["playwright", "codegen", target_url])

        recorded_code = st.text_area("📋 コピペしたコードをここに貼り付け", height=200)
        
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
                        st.session_state.view = 'project_room'; st.rerun()
                    except Exception as e: st.error(f"エラー: {e}")
        st.markdown("</div>", unsafe_allow_html=True)

# ==========================================
# 🎛️ 画面4: 司令室（詳細設定とテスト）
# ==========================================
elif st.session_state.view == 'project_room':
    project_id = st.session_state.editing_project
    proj_data = get_project_data(project_id)
    config = proj_data["config_json"]
    steps_data = config.get("robot_config", {}).get("steps", [])
    
    st.markdown(f"<div class='wizard-header'><h2>🎛️ 司令室：{proj_data['name']}</h2><p>完成まであと一歩です！手順の確認と動作テストを行いましょう。</p></div>", unsafe_allow_html=True)
    
    if st.button("⬅ ホームへ戻る"): st.session_state.view = 'dashboard'; st.rerun()

    # 1. 基本設定（後から編集可能）
    with st.expander("📝 基本設定の書き換え（URLなど）"):
        c1, c2 = st.columns(2)
        with c1:
            e_sheet = st.text_input("SFAスプシURL", value=config.get('spreadsheet', {}).get('url', ''))
            e_tab = st.text_input("タブ名", value=config.get('spreadsheet', {}).get('tab_name', ''))
        with c2:
            e_target = st.text_input("入力フォームURL", value=config.get('robot_config', {}).get('target_url', ''))
        st.caption("※動かす条件は「ステータス」が「未エントリー」の案件で固定されています。")

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
        c_s1, c_s2 = st.columns(2)
        with c_s1:
            stealth_mode = st.checkbox("人間らしくゆっくり操作する", value=config["robot_config"].get("stealth", True), key="stealth")
            captcha_break = st.checkbox("画像パズル(CAPTCHA)を自動で解く", value=config["robot_config"].get("captcha", False), key="captcha")
            st.caption("※画像パズル突破は外部サービスを利用するため、1回ごとに数円の費用が発生します。")
        with c_s2:
            slack_ch = st.text_input("Slackの通知先チャンネル名", value=config["notifications"].get("slack_id", ""))
            slack_msg = st.text_area("完了時の通知メッセージ", value=config["notifications"].get("slack_msg", "自動申請が完了しました。"))

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
        st.markdown("<div class='section-title'>📝 自動入力の手順書</div>", unsafe_allow_html=True)
        
        # 💡 初心者が迷わないための「固定値」と「呪文」の親切なガイド
        st.markdown("""
        <div style='background: #F0F9FF; padding: 16px; border-radius: 12px; border: 1px solid #BAE6FD; margin-bottom: 20px;'>
            <strong style='color: #0369A1; font-size: 16px;'>💡 代理店コードなど「毎回同じ文字（固定値）」を入力させたい場合</strong><br>
            <div style='font-size: 14px; color: #333333; margin-top: 8px; line-height: 1.6;'>
                ① 表の「値」の列に、直接文字（例：<code>123456</code> や <code>株式会社〇〇</code>）を入力してください。<br>
                ② 一番右の「最強の呪文」の中に書かれている文字も、同じように書き換えます。<br>
                <strong style='color: #EF4444;'>※コードを書き換えるのが怖い場合：</strong> 呪文の列の文字を <code>消去して空っぽ</code> にしてしまってOKです！ロボットの「AI自動検索機能」が代わりに画面を探して入力してくれます。
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        columns_order = ["順番", "いつ", "対象", "操作", "値", "変換", "ai_code"]
        # 値の加工オプション（コード不要の動的入力）
        TRANSFORM_OPTIONS = ["", "ハイフン除去", "数字のみ", "市外局番", "市内局番",
                             "加入者番号", "郵便番号_上3桁", "郵便番号_下4桁"]
        
        # 🚨 Noneバグ対策
        clean_steps = []
        for step in steps_data:
            if step and step.get("操作") is not None:
                clean_steps.append(step)
                
        df = pd.DataFrame(clean_steps)
        if df.empty: df = pd.DataFrame(columns=columns_order)
        else:
            for col in columns_order:
                if col not in df.columns: df[col] = None
            df = df[columns_order]

        # 登録されている条件ルールを取得してプルダウンに反映
        conditions = config.get("conditions", [])
        condition_names = ["常に"] + [c["name"] for c in conditions]

        edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key=f"editor_{project_id}",
                                   column_config={
                                       "いつ": st.column_config.SelectboxColumn("いつ実行するか", options=condition_names),
                                       "変換": st.column_config.SelectboxColumn("値の加工", options=TRANSFORM_OPTIONS,
                                                                              help="スプシの値をそのまま入れず加工したいとき（例：電話番号→市外局番）"),
                                       "ai_code": st.column_config.TextColumn("最強の呪文（上級者向け・任意）")
                                   })
        
        if st.button("💾 この内容で保存する", type="primary"):
            config["spreadsheet"] = {"url": e_sheet, "tab_name": e_tab, "trigger_col": "ステータス", "trigger_val": "未エントリー"}
            config["robot_config"]["target_url"] = e_target
            config["robot_config"]["stealth"] = stealth_mode
            config["robot_config"]["captcha"] = captcha_break
            
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
            st.success("設定と手順を完璧に保存しました！")

    # 6. 最後にテスト
    with st.container(border=True):
        st.markdown("<div class='section-title'>🧪 最後にテストをしましょう</div>", unsafe_allow_html=True)
        ct1, ct2 = st.columns(2)
        with ct1:
            if st.button("▶ テスト実行 (申請ボタンの手前まで)", use_container_width=True):
                st.info("ロボットが動きます。ブラウザを見ていてくださいね。")
                subprocess.Popen(["python", "robot.py", project_id])
        with ct2:
            if st.button("✓ テストOK！ロボットを完成させる", type="primary", use_container_width=True):
                st.success("おめでとうございます！これで全自動化ロボットが完成しました。")
                proj_data["is_active"] = True
                save_project(project_id, proj_data)
                time.sleep(1); st.session_state.view = 'dashboard'; st.rerun()