import streamlit as st
import sys
import os
import copy
import tempfile
import uuid
import pandas as pd
import time
import json
import re
import unicodedata
import subprocess
import google.generativeai as genai
from supabase import create_client, Client
import characters as ch
import theme
import robot_settings_ui

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
    st.page_link("pages/6_⚙️_その他設定.py", label="⚙️ 設定の手順を見る（カンナの部屋へ）", use_container_width=True)
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
def _build_gspread_client(sa_json: str):
    """サービスアカウントのJSON文字列からクライアントを作る（成功結果だけをキャッシュ）。"""
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)

def _get_gspread_client():
    """接続キーの有無チェックはキャッシュの外で毎回行う（後から設定しても再起動不要で反映される）。
    未設定なら None。設定済みなら、その内容をキーにしたクライアントを返す。"""
    try:
        sa_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        return None
    if not sa_json:
        return None
    try:
        return _build_gspread_client(sa_json)
    except Exception:
        return None

def _col_letter(n: int) -> str:
    """1始まりの列番号をスプシの列記号に変換する（1→A, 27→AA...）。"""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def _stable_list(session_key, fresh):
    """一時的な取得失敗（空リスト）でも直前に取れた一覧を保持し、選択肢が消えないようにする。
    APIレート制限などで瞬間的に空が返っても、選び直しにならないための保険。"""
    if fresh:
        st.session_state[session_key] = fresh
        return fresh
    return st.session_state.get(session_key, fresh)

def _render_columns_table(headers, caption=None, values=None):
    """列一覧を、スプシと同じように「列記号を横に並べた表」で表示する（横スクロール可）。
    values（2行目の実際の値）を渡すと、項目名の下に「値(例)」の行も表示する。"""
    if not headers:
        st.caption("(列が見つかりません)")
        return
    if caption:
        st.markdown(f"**{caption}**")
    cols = [_col_letter(i + 1) for i in range(len(headers))]
    data = [list(headers)]
    index = ["項目名"]
    if values is not None:
        v = (list(values) + [""] * len(headers))[:len(headers)]
        data.append(v)
        index.append("値(例)")
    df = pd.DataFrame(data, columns=cols, index=index)
    st.dataframe(df, use_container_width=False)

# 📌 スプシ読み取りは Sheets API のレート制限(既定60回/分)に当たりやすいため、
#    Streamlitの再実行ごとに毎回叩かないよう短時間キャッシュする（_gc は未ハッシュ）。
#    書き込み後は呼び出し側で st.cache_data.clear() して最新を取り直す。
@st.cache_data(ttl=120, show_spinner=False)
def _read_headers_and_sample(_gc, sheet_url, tab_name):
    """1行目(見出し)と2行目(実際の値・計算後)を読み込む。シートが無ければ空。"""
    sh = _gc.open_by_url(sheet_url)
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        return [], []
    headers = ws.row_values(1)
    sample = ws.row_values(2)
    return headers, sample

@st.cache_data(ttl=120, show_spinner=False)
def _list_all_sheet_names(_gc, sheet_url):
    """スプシ内の全タブ名を返す（デバッグ・透明性のため）。"""
    sh = _gc.open_by_url(sheet_url)
    return [ws.title for ws in sh.worksheets()]

@st.cache_data(ttl=120, show_spinner=False)
def _list_box_sheet_names(_gc, sheet_url):
    """『BOX』または『原本』という文字を含むタブ一覧を返す（大元の『BOX』自体は除く）。"""
    sh = _gc.open_by_url(sheet_url)
    return [ws.title for ws in sh.worksheets()
            if ws.title.strip().upper() != "BOX"
            and ("BOX" in ws.title.upper() or "原本" in ws.title)]

@st.cache_data(ttl=120, show_spinner=False)
def _read_box_sheet(_gc, sheet_url, tab_name):
    """指定タブの1行目(見出し)とA2セルの数式を読み込む。"""
    sh = _gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    headers = ws.row_values(1)
    formula = ws.acell("A2", value_render_option="FORMULA").value or ""
    return headers, formula

def _gen_json(model, prompt, retries=2):
    """Geminiにプロンプトを送りJSON応答を得る。429(無料枠のレート超過)なら、
    エラーが示す待ち秒数だけ自動で待って再試行する（最大 retries 回）。"""
    last = None
    for attempt in range(retries + 1):
        try:
            return model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        except Exception as e:
            last = e
            msg = str(e)
            is_rate = ("429" in msg) or ("quota" in msg.lower()) or ("rate limit" in msg.lower())
            if is_rate and attempt < retries:
                m = (re.search(r"retry.?delay[\s\S]*?seconds\D+(\d+)", msg, re.IGNORECASE)
                     or re.search(r"retry in (\d+)", msg, re.IGNORECASE))
                wait = min((int(m.group(1)) + 1) if m else 20, 65)
                time.sleep(wait)
                continue
            raise
    raise last

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
- 案件ID（BOXシートのA列）が空の行は絶対に含めないこと。FILTERの条件に (BOX!A2:A<>"") を必ずANDで加える
  （案件IDが入っている行だけを抽出する＝案件が無い行に文字が出ないようにするため）
- 1行目の見出しは、手本と同じ並び（BOXシートと同じ列見出し）にすること
- 絶対に以下のJSON形式のみを出力すること（説明文は不要）
{{"headers": ["見出し1", "見出し2", "..."], "formula": "=IFERROR(FILTER(...), \\"\\")"}}
"""
    response = _gen_json(model, prompt)
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

@st.cache_data(ttl=120, show_spinner=False)
def _final_sheet_exists(_gc, sheet_url, tab_name):
    """『●●』最終シートが既に存在するか。"""
    sh = _gc.open_by_url(sheet_url)
    return any(ws.title == tab_name for ws in sh.worksheets())

@st.cache_data(ttl=120, show_spinner=False)
def _read_final_sheet(_gc, sheet_url, tab_name):
    """『●●』最終シートの1行目(見出し)と2行目の各列の数式を読み込む。
    シートがまだ無ければ空リストを返す（新規商品でこれから作る場合）。"""
    sh = _gc.open_by_url(sheet_url)
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        return [], []
    headers = ws.row_values(1)
    formulas = ws.row_values(2, value_render_option="FORMULA")
    return headers, formulas

@st.cache_data(ttl=120, show_spinner=False)
def _read_computed_preview(_gc, sheet_url, tab_name, n_rows=5):
    """指定シートの、計算後の値（数式ではなく結果）を先頭n行だけ読み込んでDataFrameで返す。
    BOXにテスト案件を入れた状態で、数式が正しく展開されているか目視確認するためのプレビュー用。"""
    sh = _gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    values = ws.get(f"A1:ZZ{n_rows + 1}")  # 計算後の表示値（既定の render option）
    if not values:
        return pd.DataFrame()
    headers = values[0]
    rows = values[1:]
    # 各行の長さを見出しに揃える（短い行は空文字で埋める）
    norm = [(r + [""] * (len(headers) - len(r)))[:len(headers)] for r in rows]
    return pd.DataFrame(norm, columns=[h or f"列{i+1}" for i, h in enumerate(headers)])

def _get_candidate_fields(config):
    """録画済みの手順から、対象（項目）の一覧を返す。
    文字入力・選択・チェック・クリックなど、対象名を持つ手順すべてを対象にする
    （ラジオボタンやプルダウンも含める）。送信（申請）ステップは除外する。
    値が不要な手順（次へボタン等）は、UI側で説明を空欄にすればスキップされる。"""
    steps = config.get("robot_config", {}).get("steps", [])
    fields, seen = [], set()
    for step in steps:
        if not step:
            continue
        cond = step.get("いつ", step.get("condition", ""))
        if _is_submit_when(cond):  # 送信（申請）ステップは対象外
            continue
        target = str(step.get("target_description", step.get("対象", "")) or "").strip()
        if not target or target in seen:
            continue
        ai_code = str(step.get("ai_code", step.get("最強の呪文", "")) or "")
        value = str(step.get("value", step.get("値", "")) or "")
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
- 「案件IDが空の行を空白にする」処理はこちらで自動的に外側に付けるので、数式に含めなくてよい
  （案件が有る前提で、指定された加工・条件だけを書くこと）
- 絶対に以下のJSON形式のみを出力すること（説明文は不要）
{{"column_name": "スプシに使う列の見出し名", "formula": "=..."}}
"""
    response = _gen_json(model, prompt)
    return json.loads(response.text)

def _draft_all_final_columns(box_tab, box_headers, final_headers, final_formulas, field_descs):
    """複数項目の数式を、AIに1回のリクエストでまとめて作ってもらう（API呼び出しを項目数分の1に）。
    field_descs: {項目名: 説明}。戻り値は [{target_field, column_name, formula}, ...]。"""
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-flash')
    examples = "\n".join(f"- {h}: {f}" for h, f in zip(final_headers, final_formulas) if f)
    items = "\n".join(f'- 項目「{k}」: {v}' for k, v in field_descs.items())
    prompt = f"""
あなたはGoogleスプレッドシートの数式に詳しいエンジニアです。
「{box_tab}」という中間シートを参照して、最終シートの複数の列に入れる数式を、まとめて考えてください。

【参照元「{box_tab}」の列一覧】
{box_headers}

【最終シートの、他の列の数式の例（参考にしてください）】
{examples if examples else "（まだ他の列に数式はありません）"}

【今回作りたい列（項目名と、どう反映したいか）】
{items}

【ルール】
- 各項目について、「{box_tab}」シートの列を参照する数式を考えること（例: ='{box_tab}'!A2 のような形）
- 2行目に入れる想定の数式にすること（そのまま下の行にコピーされる前提）
- 「案件IDが空の行を空白にする」処理はこちらで自動的に外側に付けるので、数式に含めなくてよい
  （案件が有る前提で、指定された加工・条件だけを書くこと。指定された条件があればそれは数式内に残すこと）
- 電話番号・郵便番号などの分割は SPLIT を使わないこと（SPLITは「090」を数値90に変換し先頭の0が消える）。
  REGEXEXTRACT や LEFT/RIGHT/MID を使い、必要なら TO_TEXT で囲んで、必ず文字列として先頭の0を保持すること。
- 入力された項目すべてを、漏れなく出力すること
- 絶対に以下のJSON配列のみを出力すること（説明文は不要）
[{{"target_field": "フォームでの項目名", "column_name": "スプシに使う列の見出し名", "formula": "=..."}}]
"""
    response = _gen_json(model, prompt)
    data = json.loads(response.text)
    return data if isinstance(data, list) else [data]

def _norm_key(s):
    """項目名の照合用キー（全角半角・空白・大文字小文字の違いを吸収）。
    AIが項目名をそのまま返さない（微妙に整形して返す）ことがあり、
    完全一致だけだと数式が拾えず「黙って未反映」になるため。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or ""))).lower()

def _apply_final_column(gc, sheet_url, tab_name, headers, col_name, formula):
    """最終シートに、指定した列の見出しと2行目の数式を書き込む（既存の列名なら上書き、無ければ末尾に追加）。
    最終シートがまだ無ければ新規作成する（新規商品でこれから作る場合）。
    戻り値：書き込んだ列番号（1始まり）。どこに書いたかを画面で示すために使う。"""
    sh = gc.open_by_url(sheet_url)
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        ws = sh.add_worksheet(title=tab_name, rows=200, cols=max(len(headers) + 1, 10))
    if col_name in headers:
        idx = headers.index(col_name) + 1
    else:
        idx = len(headers) + 1
    # 🧯 シートの列数が足りないと 400(grid limits) になるので、必要なら列を追加する
    try:
        if idx > ws.col_count:
            ws.add_cols(idx - ws.col_count)
    except Exception:
        pass
    if col_name not in headers:
        ws.update(range_name=f"{_col_letter(idx)}1", values=[[col_name]], value_input_option="USER_ENTERED")
    # 数式が空のときは2行目を触らない（「見出しだけ作る」用。既存数式も消さない）
    if formula:
        ws.update(range_name=f"{_col_letter(idx)}2", values=[[formula]], value_input_option="USER_ENTERED")
    return idx

def _sheet_ref(name):
    """数式で使うシート参照名を安全にクォートする（例：ドコモGMOBOX → 'ドコモGMOBOX'）。
    名前に ' が含まれる場合は '' に二重化してエスケープする。"""
    return "'" + str(name).replace("'", "''") + "'"

def _wrap_blank_when_no_id(formula, box_tab, id_col="A", row=2):
    """『案件IDが空の行は空白にする』デフォルトガードで数式を包む。
    案件が無い行に、固定文字や参照の結果が残ってしまうのを防ぐ（全列の既定挙動）。

    参照する●●BOX/原本シートのA列（＝案件ID）が空なら空白、入っていれば元の数式を評価する。
    ユーザーがAIに作らせた条件（例：商材が○○のときだけ）は inner としてそのまま残るので、
    結果は「案件IDガード ＋ ユーザーの条件」の合成になる。2行目に置く前提（下にコピーして使う）。"""
    f = str(formula or "").strip()
    if not f:
        return f  # 見出しだけ作る等、数式が無いときは触らない
    if f.startswith("="):
        f = f[1:]
    guard = f'IF({_sheet_ref(box_tab)}!${id_col}{row}="","",'
    # 既に同じガードが付いているときは二重に包まない（再反映時の入れ子防止）
    if f.startswith(guard):
        return "=" + f
    return f"={guard}{f})"

def _parse_pasted_headers(text: str):
    """貼り付け/入力した列名を配列にする。タブ・カンマ・改行のいずれの区切りにも対応。"""
    if not text:
        return []
    parts = re.split(r"[\t,\n]+", text.strip())
    return [p.strip() for p in parts if p.strip()]

def _append_to_desc(desc_key, sentence):
    """テンプレの例文を、指定した説明欄（session_state）に追記する。
    ボタンの on_click から呼ぶ（ウィジェット生成前に実行されるので安全に書き換えられる）。"""
    cur = str(st.session_state.get(desc_key, ""))
    st.session_state[desc_key] = (cur + ("\n" if cur else "") + sentence)

def _set_final_headers(gc, sheet_url, tab_name, headers):
    """最終シートの1行目(見出し)を、指定した列名でまとめて設定する。無ければ新規作成する。"""
    sh = gc.open_by_url(sheet_url)
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        ws = sh.add_worksheet(title=tab_name, rows=200, cols=max(len(headers) + 1, 10))
    # 🧯 列数が足りないと 400(grid limits) になるので、必要なら列を追加する
    try:
        if len(headers) > ws.col_count:
            ws.add_cols(len(headers) - ws.col_count)
    except Exception:
        pass
    ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")

def _sync_placeholder_in_steps(steps, target_field, new_col_name, old_names=None):
    """手順書の中で「対象」がtarget_fieldに一致する手順の値・ai_codeにある{...}を、新しい列名に置き換える。
    old_names を渡した場合は、その名前の{...}だけを置換する（複数プレースホルダーの取り違え防止）。
    old_names が無い場合のみ、従来どおり全ての{...}を置き換える。渡されたstepsは書き換えず、更新後のコピーを返す。"""
    import copy
    new_steps = copy.deepcopy(steps)
    for step in new_steps:
        if not step:
            continue
        t = str(step.get("target_description", step.get("対象", "")) or "").strip()
        if t != target_field:
            continue
        for key in ("value", "値", "ai_code", "最強の呪文"):
            if not step.get(key):
                continue
            s = str(step[key])
            if old_names:
                for on in old_names:
                    if on:
                        s = s.replace("{" + on + "}", "{" + new_col_name + "}")
                step[key] = s
            else:
                step[key] = re.sub(r"\{.+?\}", f"{{{new_col_name}}}", s)
    return new_steps

def _link_step_value(steps, target_field, col, old_names=None):
    """「対象」が target_field の手順を、スプシ列 {col} 連動に書き換える（値だけ差し替え・位置はそのまま）。
    - 値の列を {col} に
    - ai_code の入力値を {col} に：
        文字を入力 → fill("{col}") ／ 選択 → select_option("{col}") ／ チェック(ラジオ) → name="{col}"
    - 既存の {…}（old_names 指定時はそれだけ）も {col} に置換
    列を作らなかった項目（=この関数を呼ばない項目）は録画の値のまま＝固定。
    渡された steps は変更せず、更新後のコピーを返す。"""
    import copy
    ph = "{" + col + "}"
    new_steps = copy.deepcopy(steps)
    for step in new_steps:
        if not step:
            continue
        t = str(step.get("target_description", step.get("対象", "")) or "").strip()
        if t != target_field:
            continue
        op = str(step.get("action", step.get("操作", "")) or "")
        # 値の列を {col} に
        step["値"] = ph
        for key in ("ai_code", "最強の呪文"):
            if not step.get(key):
                continue
            ai = str(step[key])
            # 既存プレースホルダーの置換
            if old_names:
                for on in old_names:
                    if on:
                        ai = ai.replace("{" + on + "}", ph)
            else:
                ai = re.sub(r"\{.+?\}", ph, ai)
            # ハードコードされた入力値の差し替え（セレクタ＝位置はそのまま）。
            # 操作名の表記ゆれ（「項目を選択」「ラジオボタンを選択」など）に負けないよう、
            # ai_code の中身からも種類を判定する（.fill / .select_option / role="radio"）。
            is_radio = (bool(re.search(r'get_by_role\(\s*["\']radio["\']', ai))
                        or ("ラジオ" in op) or (op in ("チェック", "check")))
            if (".fill(" in ai) or (op in ("文字を入力", "fill")):
                ai = re.sub(r'\.fill\(\s*(["\']).*?\1\s*\)', f'.fill("{ph}")', ai, count=1)
            elif (".select_option(" in ai) or (op in ("選択", "select")) or (("選択" in op) and not is_radio):
                ai = re.sub(r'\.select_option\(\s*(["\']).*?\1\s*\)', f'.select_option("{ph}")', ai, count=1)
            elif is_radio:
                # ラジオは「押す選択肢名」をセルの値にする＝name="有り" → name="{列名}"
                ai = re.sub(r'name\s*=\s*(["\']).*?\1', f'name="{ph}"', ai, count=1)
            step[key] = ai
    return new_steps

def _rename_placeholder_in_steps(steps, old_name, new_name):
    """全手順の {旧名} を {新名} に置き換える（対象に関係なく横断置換）。コピーを返す。"""
    out = copy.deepcopy(steps or [])
    for s in out:
        if not s:
            continue
        for k in ("値", "value", "ai_code", "最強の呪文"):
            if s.get(k):
                s[k] = str(s[k]).replace("{" + old_name + "}", "{" + new_name + "}")
    return out

def _current_col_for_field(steps, field):
    """手順書で「対象」がfieldの手順が今使っている {列名} を返す（無ければ空）。"""
    for s in (steps or []):
        if str((s or {}).get("対象", (s or {}).get("target_description", "")) or "").strip() != field:
            continue
        for k in ("値", "value", "ai_code", "最強の呪文"):
            m = re.findall(r"\{(.+?)\}", str((s or {}).get(k, "") or ""))
            if m:
                return m[0]
    return ""

def _rename_final_header(gc, sheet_url, tab_name, old_name, new_name):
    """最終シートの1行目の見出しを、その場で旧名→新名に改名する（列は動かさない）。"""
    sh = gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    headers = ws.row_values(1)
    if old_name not in headers:
        return False
    idx = headers.index(old_name) + 1
    ws.update_cell(1, idx, new_name)
    return True

def _count_data_rows(_gc, sheet_url, tab_name):
    """指定シートのA列（案件ID）に値が入っている行数を返す（見出し行を含む）。"""
    sh = _gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    return len(ws.col_values(1))

def _copy_formulas_down(gc, sheet_url, tab_name, last_row):
    """最終シートの2行目の数式を、3行目〜last_row行目までコピーする。
    スプシ本体の「コピー＆貼り付け」機能（PASTE_FORMULA）を使うので、
    相対参照（BOXの2行目→3行目…）は自動で1行ずつずれる。"""
    if last_row < 3:
        return 0
    sh = gc.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    n_cols = len(ws.row_values(2, value_render_option="FORMULA"))
    if n_cols <= 0:
        return 0
    if ws.row_count < last_row:
        ws.add_rows(last_row - ws.row_count)
    sh.batch_update({"requests": [{"copyPaste": {
        "source": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2,
                   "startColumnIndex": 0, "endColumnIndex": n_cols},
        "destination": {"sheetId": ws.id, "startRowIndex": 2, "endRowIndex": last_row,
                        "startColumnIndex": 0, "endColumnIndex": n_cols},
        "pasteType": "PASTE_FORMULA"}}]})
    return last_row - 2

def _set_step_radio_group(steps, field, group_label):
    """手順書の該当項目に「どのラジオグループか」を記録する（`radio_group`）。
    ラジオは同じ文言の選択肢が複数グループにあることがあり、文字だけでは取り違えるため、
    司令室で対応づけた選択欄を手順側にも残して、実行時に名指しできるようにする。"""
    for s in (steps or []):
        if str((s or {}).get("対象", (s or {}).get("target_description", "")) or "").strip() != field:
            continue
        if group_label:
            s["radio_group"] = group_label
        else:
            s.pop("radio_group", None)
    return steps

def _check_formula_sources(headers, formulas, expected_box):
    """最終シートの各列が、どのシートを参照しているかを調べる。

    列ごとに参照先が違うと、同じ行なのに『案件IDはA社／値はB社』のように
    別の案件が混ざる（＝控えた番号を別案件に書いてしまう）。それを見つけるための確認。
    戻り値：(問題のある列のリスト, 参照先ごとの列数)"""
    bad, counts = [], {}
    for h, f in zip(headers, formulas):
        if not f or not str(f).startswith("="):
            continue
        refs = set(re.findall(r"'([^']+)'!", str(f))) | set(re.findall(r"(?<!['\w])([A-Za-z0-9_ぁ-んァ-ヶ一-龥【】]+)!", str(f)))
        refs = {r for r in refs if r}
        if not refs:
            continue
        for r in refs:
            counts[r] = counts.get(r, 0) + 1
        if expected_box and any(r != expected_box for r in refs):
            bad.append({"列": h, "参照しているシート": "／".join(sorted(refs))})
    return bad, counts

_PW_HINT = re.compile(r"pass|pwd|\bpw\b|secret|パスワード|暗証", re.IGNORECASE)

def _redact_passwords(recorded_code: str):
    """録画コードのうち、パスワード欄に打った文字を伏せる。

    ログインは本物のパスワードでしか通らないため、録画には実物が入ってしまう。
    そのままAIに送ると、生成された手順書（＝データベース）に平文で残るので、
    貼り付けの時点で {秘密:パスワード} に置き換えてしまう。
    戻り値：(置き換え後のコード, 置き換えた数)"""
    lines, n = [], 0
    for line in str(recorded_code or "").split("\n"):
        if ".fill(" in line and _PW_HINT.search(line.split(".fill(")[0]):
            new_line = re.sub(r'\.fill\(\s*(["\']).*?\1\s*\)', '.fill("{秘密:パスワード}")', line, count=1)
            if new_line != line:
                n += 1
            lines.append(new_line)
        else:
            lines.append(line)
    return "\n".join(lines), n

def _link_step_secret(steps, field, secret_name):
    """指定した項目の手順を「ログイン情報を使う」形に書き換える（値も ai_code も）。

    録画のときに打った文字（ダミーでも実物でも）が手順書に残らないようにするための差し替え。
    `.fill("...")` の中身と「値」欄を {秘密:名前} にするので、実際の値は実行時にだけ現れる。"""
    import copy
    ph = "{秘密:" + secret_name + "}"
    new_steps = copy.deepcopy(steps or [])
    hit = 0
    for step in new_steps:
        if not step:
            continue
        t = str(step.get("対象", step.get("target_description", "")) or "").strip()
        if t != field:
            continue
        step["値"] = ph
        for key in ("ai_code", "最強の呪文"):
            if not step.get(key):
                continue
            ai = str(step[key])
            ai = re.sub(r'\.fill\(\s*(["\']).*?\1\s*\)', f'.fill("{ph}")', ai, count=1)
            step[key] = ai
        hit += 1
    return new_steps, hit

def _autolink_radio_groups(steps, form_choices):
    """手順書のラジオ操作に「どのグループか」（`radio_group`）を自動で書き込む。

    録画の「対象」は、選択肢そのもの（例：ペア回線あり）か、グループの見出しを
    まとめた文字列（例：無し 有り GMO判断OK）になっている。どちらも
    『選択肢を調べる』で取った一覧と突き合わせれば、どのグループか特定できる。
    人が項目ごとに対応づけしなくても効くようにするのが狙い（設定漏れ防止）。
    戻り値は (steps, 紐づけできた件数のリスト)。"""
    radios = [c for c in (form_choices or []) if (c or {}).get("kind") == "radio" and c.get("options")]
    if not radios:
        return steps, []
    linked = []
    for s in (steps or []):
        if not s:
            continue
        op = str(s.get("操作", s.get("action", "")) or "")
        if op not in ("チェック", "クリック", "check", "click"):
            continue
        target = _norm_key(s.get("対象", s.get("target_description", "")))
        if not target:
            continue
        hit = ""
        for c in radios:
            opts = [_norm_key(o) for o in c["options"]]
            joined = "".join(opts)
            if target in opts or target == joined or target == _norm_key(c.get("label", "")):
                hit = str(c.get("label", "") or "")
                break
        if hit:
            s["radio_group"] = hit
            linked.append(f"{s.get('対象', s.get('target_description', ''))} → {hit}")
    return steps, linked


def _write_capture_value(gc, sheet_url, tab, col_name, key_col, key_val, value):
    """控えた値（例：回線登録番号）を、キー列（案件ID）が一致する行に書き込む。
    行番号ではなく値で行を探すので、行がずれても別の案件に書いてしまわない。
    戻り値は担当者向けの結果メッセージ（成功は ✅ で始まる）。"""
    key_val = str(key_val or "").strip()
    value = str(value or "").strip()
    if not (tab and col_name and key_col):
        return "⚠️ 設定が足りません（シート名・列名・照合列を確認してください）"
    if not key_val:
        return f"⚠️ この案件に「{key_col}」の値がないため書けません"
    ws = gc.open_by_url(sheet_url).worksheet(tab)
    headers = ws.row_values(1)
    for _need in (col_name, key_col):
        if _need not in headers:
            return f"⚠️ シート『{tab}』に「{_need}」列がありません"
    keys = ws.col_values(headers.index(key_col) + 1)
    hit = [i + 1 for i, v in enumerate(keys) if str(v).strip() == key_val]
    if not hit:
        return f"⚠️ {key_col}={key_val} の行が『{tab}』に見つかりません"
    if len(hit) > 1:
        return f"⚠️ {key_col}={key_val} の行が『{tab}』に{len(hit)}件あります（1件に絞ってください）"
    ws.update_cell(hit[0], headers.index(col_name) + 1, value)
    return f"✅ 『{tab}』{col_name}列（{hit[0]}行目）に書きました：{value}"

def _consult_carrier_rule(request_text, current_captures, current_conditions, sheet_headers, product_type):
    """担当者が日本語で書いた『このキャリア特有のルール』を、既存の設定スキーマに翻訳する。

    翻訳先は2つだけに絞る（安全のため）：
      - captures  ：申請したあとに控える値（例：完了画面の回線登録番号）
      - conditions：条件分岐ルール（例：〇〇のときだけこの手順を実行）
    どちらにも当てはまらない＝仕組みの追加が必要なものは kind="none" を返し、
    開発者向けの依頼メモ（dev_request）を作る。AIに「何でもできるフリ」をさせないための逃げ道。"""
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-flash')
    prompt = f"""
あなたは、申請自動化ロボットの設定を作るアシスタントです。
担当者が日本語で書いた「このキャリア特有のルール」を、下の2つの設定のどちらかに翻訳してください。
どちらにも当てはまらない場合は、無理に当てはめず kind を "none" にしてください（とても重要）。

【この商材】{product_type or "（未設定）"}
【スプレッドシートの列一覧】{sheet_headers}
【今の「控える値」設定】{json.dumps(current_captures, ensure_ascii=False)}
【今の「条件分岐」設定】{json.dumps(current_conditions, ensure_ascii=False)}

【翻訳先1：captures（申請したあとに控える値）】
申請の完了画面に出る番号などを読み取って、スプシに書き戻す設定。
{{"name": "控える値の名前", "hint": "完了画面でその値の直前に出る文言", "tab": "書き込むシート名",
  "col": "書き込む列の見出し", "key_col": "案件を照合する列（既定は案件ID）", "pattern": "（任意）正規表現"}}

【翻訳先2：conditions（条件分岐ルール）】
ある条件のときだけ手順を実行する設定。opは eq/ne/contains/not_contains/empty/not_empty/gt/gte/lt/lte/in のいずれか。
{{"name": "ルール名", "logic": "AND または OR", "rules": [{{"col": "列名", "op": "eq", "value": "値"}}]}}

【担当者の相談内容】
{request_text}

【ルール】
- 列名は、上の「スプレッドシートの列一覧」に実在するものだけを使うこと（無ければ new_columns に書く）
- 既存の設定と同じ名前のものは、置き換えるつもりで出力すること
- summary は、非エンジニアにも分かる日本語で「何をする設定か」を2文以内で説明すること
- 当てはまらない場合は kind="none" とし、dev_request に「何をしたいのか・なぜ既存設定で無理か」を書くこと
- 絶対に以下のJSONのみを出力すること
{{"kind": "captures" または "conditions" または "none",
  "summary": "日本語の説明",
  "captures": [], "conditions": [],
  "new_columns": ["スプシに新しく必要な列名（あれば）"],
  "dev_request": "kindがnoneのときだけ：開発者への依頼メモ"}}
"""
    response = _gen_json(model, prompt)
    data = json.loads(response.text)
    return data if isinstance(data, dict) else {}

def _capture_setup_check(gc, sheet_url, final_tab, cap):
    """『控える値』の書き戻しに必要な準備が揃っているかを調べ、(結果リスト, 不足リスト) を返す。
    担当者が自分で列を作れなくても済むように、何が足りないかを日本語で示す。"""
    checks, missing = [], []
    box_tab = str(cap.get("tab", "") or "").strip()
    col_name = str(cap.get("col", "") or cap.get("name", "")).strip()
    key_col = str(cap.get("key_col", "") or "案件ID").strip()
    try:
        sh = gc.open_by_url(sheet_url)
    except Exception as e:
        return ([f"⚠️ スプシを開けません（共有を『編集者』にしてください）: {e}"], [])
    try:
        box_ws = sh.worksheet(box_tab)
        box_headers = box_ws.row_values(1)
        checks.append(f"✅ 書き込み先シート『{box_tab}』があります")
    except Exception:
        return ([f"⚠️ シート『{box_tab}』が見つかりません（設定のシート名を確認してください）"], [])
    if col_name in box_headers:
        checks.append(f"✅ 『{box_tab}』に「{col_name}」列があります")
    else:
        checks.append(f"⚠️ 『{box_tab}』に「{col_name}」列がありません")
        missing.append("box_col")
    if key_col in box_headers:
        checks.append(f"✅ 『{box_tab}』に照合用の「{key_col}」列があります")
    else:
        checks.append(f"⚠️ 『{box_tab}』に照合用の「{key_col}」列がありません")
        missing.append("box_key")
    try:
        final_headers = sh.worksheet(final_tab).row_values(1)
    except Exception:
        return (checks + [f"⚠️ ロボットが読むシート『{final_tab}』が見つかりません"], missing)
    if key_col in final_headers:
        checks.append(f"✅ ロボットが読む『{final_tab}』に「{key_col}」列があります")
    else:
        checks.append(f"⚠️ ロボットが読む『{final_tab}』に「{key_col}」列がありません"
                      "（これが無いと、どの案件の番号か分からず書き戻せません）")
        missing.append("final_key")
    return (checks, missing)

def _capture_setup_fix(gc, sheet_url, final_tab, cap):
    """足りない列を自動で用意する。
    - 書き込み先シート（BOX等）に、空の『控える値』列を作る
    - ロボットが読むシートに、照合用の列（案件ID）を数式で作り、下までコピーする
    戻り値は担当者向けメッセージのリスト。"""
    out = []
    box_tab = str(cap.get("tab", "") or "").strip()
    col_name = str(cap.get("col", "") or cap.get("name", "")).strip()
    key_col = str(cap.get("key_col", "") or "案件ID").strip()
    sh = gc.open_by_url(sheet_url)
    box_ws = sh.worksheet(box_tab)
    box_headers = box_ws.row_values(1)

    if col_name not in box_headers:
        idx = len(box_headers) + 1
        if idx > box_ws.col_count:
            box_ws.add_cols(idx - box_ws.col_count)
        box_ws.update(range_name=f"{_col_letter(idx)}1", values=[[col_name]], value_input_option="USER_ENTERED")
        box_headers.append(col_name)
        out.append(f"✅ 『{box_tab}』に「{col_name}」列（空欄）を作りました＝ここに番号が入ります")

    if key_col not in box_headers:
        out.append(f"⚠️ 『{box_tab}』に「{key_col}」列がありません。案件を見分ける列の見出し名を"
                   f"設定の「照合列」に合わせてください（自動では作れません）")
        return out

    final_ws = sh.worksheet(final_tab)
    final_headers = final_ws.row_values(1)
    if key_col in final_headers:
        return out or [f"✅ 準備はすでに整っています"]

    idx = len(final_headers) + 1
    if idx > final_ws.col_count:
        final_ws.add_cols(idx - final_ws.col_count)
    letter = _col_letter(idx)
    key_letter = _col_letter(box_headers.index(key_col) + 1)
    formula = (f"=IF({_sheet_ref(box_tab)}!${key_letter}2=\"\",\"\","
               f"{_sheet_ref(box_tab)}!${key_letter}2)")
    final_ws.update(range_name=f"{letter}1", values=[[key_col]], value_input_option="USER_ENTERED")
    final_ws.update(range_name=f"{letter}2", values=[[formula]], value_input_option="USER_ENTERED")
    out.append(f"✅ 『{final_tab}』に「{key_col}」列を作りました（{letter}列・{box_tab}から自動で引いてきます）")

    # 下の行までコピー（案件数ぶん）。ロボットは1行ずつ読むので、全行に必要。
    last_row = max(len(box_ws.col_values(box_headers.index(key_col) + 1)), 2)
    if last_row >= 3:
        if final_ws.row_count < last_row:
            final_ws.add_rows(last_row - final_ws.row_count)
        sh.batch_update({"requests": [{"copyPaste": {
            "source": {"sheetId": final_ws.id, "startRowIndex": 1, "endRowIndex": 2,
                       "startColumnIndex": idx - 1, "endColumnIndex": idx},
            "destination": {"sheetId": final_ws.id, "startRowIndex": 2, "endRowIndex": last_row,
                            "startColumnIndex": idx - 1, "endColumnIndex": idx},
            "pasteType": "PASTE_FORMULA"}}]})
        out.append(f"✅ その列を{last_row}行目までコピーしました")
    return out

def _generate_steps_from_design(skeleton, design):
    """録画の骨組み(skeleton)に設計(design)を当てて手順書を生成する。
    - 列名がある項目（=スプシ連動）→ 値を {列名} に差し替え（_link_step_value）
    - それ以外（スキップ・設計なし）→ 骨組みの値のまま＝固定
    骨組みの順番・セレクタ・操作・送信ステップはそのまま保持される。"""
    import copy
    steps = copy.deepcopy(skeleton or [])
    for field, d in (design or {}).items():
        if isinstance(d, dict) and d.get("col"):
            steps = _link_step_value(steps, field, d["col"])
    # 🧹 入力枠を選ぶだけの余分なクリックは、作り直し時にも入れない
    return _strip_redundant_field_clicks(steps)

def _revert_field_to_recorded(steps, skeleton, field):
    """指定した対象(field)の手順だけ、録画の骨組みの値・呪文(ai_code)に戻す（＝スプシ連動をやめて固定化）。
    位置・順番・いつは今のまま保持し、値と呪文だけ録画時の内容に差し替える。骨組みに無ければ何もしない。
    渡された steps は変更せず、更新後のコピーを返す。"""
    import copy
    src = None
    for s in (skeleton or []):
        if not s:
            continue
        if str(s.get("対象", s.get("target_description", "")) or "").strip() == field:
            src = s
            break
    if src is None:
        return steps
    rec_val = src.get("値", src.get("value", ""))
    rec_ai = src.get("ai_code", src.get("最強の呪文", ""))
    out = copy.deepcopy(steps or [])
    for s in out:
        if not s:
            continue
        if str(s.get("対象", s.get("target_description", "")) or "").strip() != field:
            continue
        s["値"] = rec_val
        if "value" in s:
            s["value"] = rec_val
        s["ai_code"] = rec_ai
        if "最強の呪文" in s:
            s["最強の呪文"] = rec_ai
    return out

# 「入力枠を選ぶだけの余分なクリック」を手順書に入れないための判定・除去
_CLICK_OPS = {"クリック", "click"}
_FILL_OPS = {"文字を入力", "fill"}
# これらの語を含むクリックは“必要な操作”として絶対に消さない（ボタン・送信・次へ 等）
_KEEP_CLICK_WORDS = ["次", "送信", "確認", "申請", "申込", "申し込", "確定", "進む", "戻", "追加", "検索",
                     "登録", "同意", "選択", "ボタン", "submit", "next", "confirm", "button", "search",
                     "add", "register", "agree"]

def _is_field_focus_click(step, next_step):
    """step が『入力枠を選ぶだけの余分なクリック』か。すぐ次が『文字を入力』のときだけ真。
    ボタン/送信/次へ 等のナビ系や送信ステップは、必要なので絶対に対象外にする（消さない）。"""
    op = str(step.get("操作", step.get("action", "")) or "").strip()
    nop = str(next_step.get("操作", next_step.get("action", "")) or "").strip()
    if op not in _CLICK_OPS or nop not in _FILL_OPS:
        return False
    if _is_submit_when(step.get("いつ", step.get("condition", ""))):
        return False
    hay = (str(step.get("対象", step.get("target_description", "")) or "") + " "
           + str(step.get("ai_code", step.get("最強の呪文", "")) or "")).lower()
    if any(w.lower() in hay for w in _KEEP_CLICK_WORDS):
        return False
    return True

def _strip_redundant_field_clicks(steps):
    """手順書から『入力枠を選ぶだけの余分なクリック』を取り除き、順番を振り直して返す。
    録画すると各入力欄の前に不要なクリックが入るため、作成段階でこれを落とす。
    ボタン・送信・次へ・チェック・プルダウンなど必要な操作は残す。"""
    if not steps:
        return steps
    ordered = sorted([s for s in steps if s], key=lambda x: x.get("順番", x.get("order", 999)))
    kept = []
    for i, s in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        if nxt is not None and _is_field_focus_click(s, nxt):
            continue  # 余分なクリックは入れない
        kept.append(s)
    for i, s in enumerate(kept, 1):
        if "順番" in s:
            s["順番"] = i
        if "order" in s:
            s["order"] = i
    return kept

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
ACTION_OPTIONS = ["文字を入力", "クリック", "選択", "チェック", "日付を入れる", "人の操作を待つ",
                  "ファイルをダウンロード", "認証コードを入力"]

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
    "人の操作を待つ": "を、あなたが操作するまで待ちます（ログインや認証コードなど）",
    "ファイルをダウンロード": "を押して、ファイルをダウンロードします",
    "認証コードを入力": "に、メールに届いた認証コードを入力します（自動で受け取ります）",
    "auth_code": "に、メールに届いた認証コードを入力します（自動で受け取ります）",
    "download": "を押して、ファイルをダウンロードします",
    "wait_human": "を、あなたが操作するまで待ちます（ログインや認証コードなど）",
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


def _robot_health(config, final_headers=None):
    """完成前チェック。(ok:bool, ラベル, ヒント) のリストを返す。
    final_headers を渡すと、手順の{列名}が最終シートに存在するかも確認する。"""
    checks = []
    rc = config.get("robot_config", {})
    steps = [s for s in rc.get("steps", []) if s and (s.get("操作") or s.get("action"))]
    sheet = config.get("spreadsheet", {})

    checks.append((bool(steps), "手順が1つ以上ある",
                   "STEP2で録画するか、下の手順書の表に手順を追加してください。"))
    has_submit = any(_is_submit_when(s.get("いつ", s.get("condition", ""))) for s in steps)
    checks.append((has_submit, "送信（申請）ステップがある",
                   "手順書の下の「🚀 送信ステップを追加」で、送信ボタンの文言を設定してください。"))
    checks.append((bool(sheet.get("url")), "SFAスプシURLが設定されている",
                   "「基本設定の書き換え」でスプシURLを入れてください。"))
    checks.append((bool(sheet.get("tab_name")), "最終シートのタブ名が決まっている",
                   "「最終シートの列・数式作成」でシートを選ぶ/作ると決まります。"))
    if final_headers is not None:
        ph = set()
        for s in steps:
            for key in ("値", "value", "ai_code", "最強の呪文"):
                v = s.get(key)
                if v:
                    ph.update(re.findall(r"\{(.+?)\}", str(v)))
        unknown = sorted(p for p in ph if p not in final_headers)
        checks.append((not unknown, "手順の{列名}がすべて最終シートに存在する",
                       (f"最終シートに無い列: 「{'」「'.join(unknown)}」。"
                        "カラム設計で作るか、名前を合わせてください。") if unknown else ""))
    return checks

def _render_health_checklist(checks, compact=True):
    """健康診断チェックの結果を表示する。compact=Trueは一覧、Falseはヒント付き詳細。"""
    for ok, label, hint in checks:
        mark = "✅" if ok else "⬜"
        if compact:
            st.markdown(f"{mark} {label}")
        else:
            if ok:
                st.markdown(f"✅ {label}")
            else:
                st.markdown(f"⚠️ **{label}** — {hint}")

def _section_header(title, done=None):
    """セクションの見出し。done=True のときは、枠（st.container border）の左辺全体を緑にする
    完了マーカーを見出し内に埋め込む（CSSの :has() で枠のborder-leftを色付けする）。"""
    mark = "✅ " if done else ""
    marker = "<span class='enkan-done-green'></span>" if done else ""
    st.markdown(f"<div class='section-title'>{mark}{title}</div>{marker}", unsafe_allow_html=True)


# ==========================================
# 🏠 画面1: ホーム（ロボット一覧）
# ==========================================
def render_entry_runner(project_id, config):
    """🖐 エントリー実行パネル（有人確認モード）。
    確認画面の手前まで自動入力 → 人が申請ボタンを押す → 完了を確認して次の案件へ。
    設定（司令室）とは切り離し、毎日の運用はこの画面だけで完結するようにしている。"""
    _cproc_key = f"confirm_proc_{project_id}"
    _cwd_key = f"confirm_wd_{project_id}"
    _safe_pid = re.sub(r"[^0-9A-Za-z_-]", "_", str(project_id))
    _cwd = st.session_state.get(_cwd_key) or os.path.join(tempfile.gettempdir(), f"enkan_confirm_{_safe_pid}")
    st.session_state[_cwd_key] = _cwd
    try:
        os.makedirs(_cwd, exist_ok=True)
    except Exception:
        pass
    _proc = st.session_state.get(_cproc_key)
    _c_running = _proc is not None and _proc.poll() is None

    def _c_read(_name):
        try:
            with open(os.path.join(_cwd, _name), encoding="utf-8") as _f:
                return json.load(_f)
        except Exception:
            return None

    def _c_clean():
        for _n in ("status.json", "live.json", "command.json", "only_keys.json"):
            try:
                os.remove(os.path.join(_cwd, _n))
            except Exception:
                pass

    def _c_command(_index, _action):
        try:
            with open(os.path.join(_cwd, "command.json"), "w", encoding="utf-8") as _f:
                json.dump({"index": _index, "action": _action}, _f)
        except Exception as _e:
            st.error(f"指示の送信に失敗しました: {_e}")

    def _c_launch(_only_path=None):
        _c_clean()
        _cmd = [sys.executable, "robot.py", "--confirm", project_id, _cwd]
        if _only_path:
            _cmd += ["--only", _only_path]
        try:
            _p = subprocess.Popen(_cmd)
            st.session_state[_cproc_key] = _p
            st.rerun()
        except Exception as _e:
            st.error(f"起動に失敗しました（このPCで開いていない可能性）: {_e}")

    # ▶ 起動 / 実行中の操作
    if not _c_running:
        st.caption("未エントリーの案件を上から順に、確認画面の手前まで自動で入力します。"
                   "内容を確かめて“あなたが”申請ボタンを押すと、次の案件に進みます。"
                   "💻 自分のPCでアプリを開いているときだけ使えます。")
        if st.button("▶ エントリーを開始する", key=f"confirm_start_{project_id}",
                     type="primary", use_container_width=True):
            _c_launch()
    else:
        st.info("🖐 実行中です。開いたブラウザで内容を確認し、下のボタンで送信/スキップしてください。")
        if st.button("🔄 状況を更新", key=f"confirm_refresh_{project_id}"):
            st.rerun()

    # 現在“確認待ち”の案件があればボタンを出す
    _live = _c_read("live.json")
    if _c_running and _live and _live.get("phase") == "waiting_confirm":
        _idx = int(_live.get("index", 0)); _tot = int(_live.get("total", 1))
        st.markdown(f"**✋ 確認中の案件（{_idx + 1} / {_tot}）**")
        st.dataframe(pd.DataFrame([_live.get("row", {})]), use_container_width=True, hide_index=True)
        if _live.get("auto_detect"):
            st.caption("💡 完了サインを設定済みです。ブラウザで申請ボタンを押して完了画面になれば自動で次へ進みます。")
        else:
            st.caption("⚠️ 完了サイン（完了画面の文言）が未設定です。送信したら下の「送信できた→次へ」を押してください。")
        _b1, _b2, _b3 = st.columns(3)
        with _b1:
            if st.button("✅ 送信できた→次へ", key=f"cmd_done_{project_id}_{_idx}", use_container_width=True):
                _c_command(_idx, "done"); st.rerun()
        with _b2:
            if st.button("⏭ この案件をスキップ", key=f"cmd_skip_{project_id}_{_idx}", use_container_width=True):
                _c_command(_idx, "skip"); st.rerun()
        with _b3:
            if st.button("🛑 中止（残りは実行しない）", key=f"cmd_stop_{project_id}_{_idx}",
                         use_container_width=True):
                _c_command(_idx, "stop"); st.rerun()

    # 🔎 ブラウザが閉じられた：申請できたのかを聞く。
    #    ここで正しく答えてもらわないと、出した案件をもう一度出す／出していない案件を
    #    出したことにする、のどちらかが起きる。
    if _c_running and _live and _live.get("phase") == "browser_closed":
        _idx = int(_live.get("index", 0)); _tot = int(_live.get("total", 1))
        st.warning(f"🔎 **ブラウザが閉じられました（案件 {_idx + 1}/{_tot}）**　"
                   "この案件は、申請できましたか？")
        st.dataframe(pd.DataFrame([_live.get("row", {})]), use_container_width=True, hide_index=True)
        st.caption("「申請できた」を選ぶと処理済みとして記録し、次から同じ案件を出しません。"
                   "「まだ出していない」なら、あとでもう一度実行できます。")
        _q1, _q2, _q3 = st.columns(3)
        with _q1:
            if st.button("✅ 申請できた", key=f"cmd_closed_done_{project_id}_{_idx}",
                         type="primary", use_container_width=True):
                _c_command(_idx, "done"); st.rerun()
        with _q2:
            if st.button("↩ まだ出していない", key=f"cmd_closed_skip_{project_id}_{_idx}",
                         use_container_width=True):
                _c_command(_idx, "skip"); st.rerun()
        with _q3:
            if st.button("🛑 中止（残りもやめる）", key=f"cmd_closed_stop_{project_id}_{_idx}",
                         use_container_width=True):
                _c_command(_idx, "stop"); st.rerun()

    # ✋ 人の操作待ち（ログイン・メールの認証コード入力など）。ロボットは待っている。
    if _c_running and _live and _live.get("phase") == "waiting_human":
        _idx = int(_live.get("index", 0)); _tot = int(_live.get("total", 1))
        st.warning(f"✋ **あなたの操作待ちです（案件 {_idx + 1}/{_tot}）**　"
                   f"→ {_live.get('message', '画面の操作')}")
        st.caption("開いているブラウザで操作してください。終わったら下のボタンを押すと、ロボットが続きから再開します。")
        _h1, _h2 = st.columns([2, 1])
        with _h1:
            if st.button("✅ できました → 続ける", key=f"cmd_human_{project_id}_{_idx}",
                         type="primary", use_container_width=True):
                _c_command(_idx, "human_ok"); st.rerun()
        with _h2:
            if st.button("🛑 中止", key=f"cmd_humanstop_{project_id}_{_idx}", use_container_width=True):
                _c_command(_idx, "stop"); st.rerun()

    # 🧾 申請直後：完了画面を開いたまま、番号を控える／完了画面の文言を確認する
    if _c_running and _live and _live.get("phase") == "done_review":
        _idx = int(_live.get("index", 0)); _tot = int(_live.get("total", 1))
        st.success(f"✅ 案件 {_idx + 1}/{_tot} の申請が完了しました。完了画面を開いたままにしています。")
        _caps_cfg = config.get("robot_config", {}).get("captures", []) or []
        _got = _live.get("captures", {}) or {}
        for _c in _caps_cfg:
            _cn = str(_c.get("name", "") or "")
            _v = str(_got.get(_cn, "") or "")
            if _v:
                st.info(f"📋 {_cn}：**{_v}**（このあとスプシに書き戻します）")
            else:
                st.warning(f"📋 {_cn} は自動で読み取れませんでした。完了画面を見て、下に入力してください。")
        if not _caps_cfg:
            st.caption("💡 この画面に出ている番号などを控えてください。"
                       "毎回自動で控えたい場合は、司令室の「📋 申請したあとに控える値」に設定できます。")
        # 完了画面の文言：『申請完了の合図』や『控える値の手がかり文言』を決めるのに使う
        if _live.get("page_text"):
            with st.expander("🔎 完了画面に出ている文字（設定の参考に使えます）"):
                st.caption("⚠️ 実データのため個人情報が含まれます。")
                st.text_area("完了画面の文言", value=_live.get("page_text", ""), height=200,
                             key=f"donetext_{project_id}_{_idx}")
                st.caption("この中の決まり文句を「✅ 申請完了の合図」に、番号の直前の言葉を"
                           "「📋 控える値」の手がかり文言に設定すると、次回から自動で確認・控えができます。")
        if _live.get("screenshot"):
            st.caption(f"📸 完了画面の画像を保存しました：{_live['screenshot']}")
        _n1, _n2 = st.columns([2, 1])
        with _n1:
            if st.button("➡ 控えました。次の案件へ", key=f"cmd_next_{project_id}_{_idx}",
                         type="primary", use_container_width=True):
                _c_command(_idx, "next"); st.rerun()
        with _n2:
            if st.button("🛑 ここで終了", key=f"cmd_stopdone_{project_id}_{_idx}", use_container_width=True):
                _c_command(_idx, "stop"); st.rerun()

    # 📋 結果一覧（最新1回だけ）。個人情報を含むためクラウド(DB)には保存せず、
    #    このPCの作業フォルダ(status.json)にだけ残す＝アプリを閉じても同じPCなら残る。
    _status = _c_read("status.json")
    _results = (_status or {}).get("results", []) or []
    _at = (_status or {}).get("updated_at", "")
    _total = int((_status or {}).get("total", len(_results)) or 0)

    if _status and _status.get("phase") == "error":
        st.error(f"実行できませんでした: {_status.get('message', '')}")

    if _results:
        st.markdown("---")
        _n_done = sum(1 for x in _results if x.get("status") == "done")
        _n_fail = sum(1 for x in _results if x.get("status") == "failed")
        _n_skip = sum(1 for x in _results if x.get("status") in ("skipped", "aborted"))
        _finished = (_status or {}).get("phase") == "finished"
        _left = max(_total - len(_results), 0)
        st.markdown(f"**📋 {'実行結果' if _finished else '進行中'}：全{_total}件中 "
                    f"✅エントリー完了 {_n_done}／❌エラー {_n_fail}／⏭スキップ {_n_skip}**"
                    + (f"　（残り{_left}件）" if _left else "")
                    + (f"　🕒 {_at}" if _at else ""))
        if _finished and _left:
            st.warning(f"⚠️ 途中で止まったため、{_left}件は手つかずです（次回の実行でまた対象になります）。")
        for x in _results:
            _s = x.get("status")
            _no = int(x.get("index", 0)) + 1
            if _s == "failed":
                st.error(f"❌ 案件{_no}：エントリーできませんでした — {x.get('reason', '')}")
                st.dataframe(pd.DataFrame([x.get("row", {})]), use_container_width=True, hide_index=True)
            elif _s == "done":
                st.success(f"✅ 案件{_no}：送信できました")
                # 📋 控える値（例：回線登録番号）。自動で取れていれば表示、取れていなければ手入力。
                _caps = config.get("robot_config", {}).get("captures", []) or []
                if _caps:
                    for _m in (x.get("capture_notes") or []):
                        (st.caption if _m.startswith("✅") else st.warning)(_m)
                    _missing = [c for c in _caps
                                if not str((x.get("captures") or {}).get(c.get("name", ""), "") or "").strip()]
                    if _missing:
                        st.caption("↓ 完了画面に出ている番号を見て入力すると、スプシに書き戻します。")
                        for _c in _missing:
                            _cn = str(_c.get("name", "") or "")
                            _mk = f"capman_{project_id}_{_no}_{_cn}"
                            mc1, mc2 = st.columns([3, 1])
                            with mc1:
                                st.text_input(f"{_cn}（案件{_no}）", key=_mk,
                                              placeholder="完了画面の番号をそのまま入力")
                            with mc2:
                                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                                if st.button("スプシに記入", key=f"capbtn_{project_id}_{_no}_{_cn}",
                                             use_container_width=True):
                                    _v = str(st.session_state.get(_mk, "") or "").strip()
                                    _gc_w = _get_gspread_client()
                                    if not _v:
                                        st.warning("番号を入力してください。")
                                    elif not _gc_w:
                                        st.error("接続キー GOOGLE_SERVICE_ACCOUNT_JSON が未設定のため書き戻せません。")
                                    else:
                                        try:
                                            _sheet_cfg = config.get("spreadsheet", {}) or {}
                                            _kc = str(_c.get("key_col", "") or "案件ID").strip()
                                            _msg = _write_capture_value(
                                                _gc_w, _sheet_cfg.get("url", ""),
                                                str(_c.get("tab", "") or _sheet_cfg.get("tab_name", "")).strip(),
                                                str(_c.get("col", "") or _cn).strip(), _kc,
                                                (x.get("row", {}) or {}).get(_kc, ""), _v)
                                            (st.success if _msg.startswith("✅") else st.error)(_msg)
                                        except Exception as _e:
                                            st.error(f"書き戻しに失敗しました: {_e}")
            elif _s == "skipped":
                st.caption(f"⏭ 案件{_no}：スキップ（{x.get('reason', '')}）")
            elif _s == "aborted":
                st.caption(f"🛑 案件{_no}：中止（{x.get('reason', '')}）")

        # 🔁 失敗分だけ再実行（済んだ案件には触れないので安全）
        _failed_keys = [x.get("key") for x in _results if x.get("status") == "failed" and x.get("key")]
        if _failed_keys and not _c_running:
            st.caption("直したあと、失敗した案件だけをもう一度実行できます（成功済みには触れません）。")
            if st.button(f"🔁 失敗分だけ再実行（{len(_failed_keys)}件）", key=f"confirm_retry_{project_id}",
                         use_container_width=True):
                _only = os.path.join(_cwd, "only_keys.json")
                try:
                    with open(_only, "w", encoding="utf-8") as _f:
                        json.dump(_failed_keys, _f)
                    _c_launch(_only_path=_only)
                except Exception as _e:
                    st.error(f"再実行の準備に失敗しました: {_e}")

    # 実行中はゆっくり自動更新（画面を見ながら操作できるように）
    if _c_running:
        time.sleep(2)
        st.rerun()

# ==========================================
# ▶ エントリー実行画面（運用）：設定（司令室）とは分けて、毎日の作業はここだけで完結させる
# ==========================================
if st.session_state.view == 'run_entry':
    _run_id = st.session_state.get("running_project")
    _run_proj = get_project_data(_run_id) if _run_id else None
    if not _run_proj:
        st.error("ロボットが見つかりませんでした。")
        if st.button("← ホームに戻る"):
            st.session_state.view = 'dashboard'; st.rerun()
    else:
        _run_cfg = _run_proj.get("config_json", {}) or {}
        st.markdown(f"<div class='wizard-header'><h1>▶ {_run_proj.get('name', _run_id)}：エントリー実行</h1>"
                    "<p>1件ずつ内容を確認しながら申請します。</p></div>", unsafe_allow_html=True)
        _rb1, _rb2 = st.columns([1, 4])
        with _rb1:
            if st.button("← ホームに戻る", use_container_width=True):
                st.session_state.view = 'dashboard'; st.rerun()
        with _rb2:
            if st.button("⚙️ このロボットの設定を開く", use_container_width=True):
                st.session_state.editing_project = _run_id
                st.session_state.view = 'project_room'; st.rerun()
        if not _run_proj.get("is_active"):
            st.caption("💡 このロボットは「全自動 おやすみ中」です（クラウドの無人実行はされません）。"
                       "この画面からの手動エントリーは問題なく使えます。")
        st.markdown("---")
        render_entry_runner(_run_id, _run_cfg)

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
    # 「__」で始まる行は設定の置き場所（例：進捗反映の設定）なので、ロボット一覧には出さない
    projects = [p for p in projects if not str(p.get("id", "")).startswith("__")]
    # 進捗の取り込み用ロボットは「進捗反映自動化」のページで扱う。
    # 申請用と混ざると、取り違えて本番実行してしまうので、ここには出さない。
    projects = [p for p in projects
                if str((p.get("config_json") or {}).get("product_type", "")) != "進捗取り込み"]
    if not projects:
        st.info("まだロボットがいません。上の「＋ 新しいロボットを作る」から、最初の1台をつくりましょう！")
    else:
        cols = st.columns(3)
        for i, proj in enumerate(projects):
            with cols[i % 3]:
                # 💡 HTMLのdivを使わず、Streamlitのcontainerで枠を固定します
                with st.container(border=True):
                    st.markdown(f"### {proj['name']}")
                    
                    # 稼働状態のバッジ表示（＝クラウドでの全自動実行のON/OFF。手動実行はOFFでもできる）
                    status_text = "✨ 全自動 稼働中" if proj['is_active'] else "💤 全自動 おやすみ中"
                    st.markdown(f"<span class='{'status-active' if proj['is_active'] else 'status-inactive'}'>{status_text}</span>", unsafe_allow_html=True)
                    
                    st.markdown("<br>", unsafe_allow_html=True)
                    # ▶ 毎日の運用はここから。設定（司令室）に入らずにエントリーできる。
                    if st.button("▶ エントリー開始", key=f"run_{proj['id']}", type="primary",
                                 use_container_width=True,
                                 help="未エントリーの案件を1件ずつ確認しながら申請します（このPCで実行）。"):
                        st.session_state.running_project = proj['id']
                        st.session_state.view = 'run_entry'
                        st.rerun()
                    if not proj['is_active']:
                        st.caption("※全自動はOFF。手動なら今すぐ実行できます")

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
                        # 🔁 このスイッチ＝クラウドでの「全自動」実行の対象にするか。
                        #    OFFでも上の「▶ エントリー開始」で手動実行はできる（別物だと分かる名前にする）。
                        if st.toggle("全自動稼働", value=proj['is_active'], key=f"tog_{proj['id']}",
                                     help="ONにすると、クラウドの自動実行（無人）の対象になります。"
                                          "OFFでも「▶ エントリー開始」で手動のエントリーはできます。"
                                     ) != proj['is_active']:
                            supabase.table("merchants").update({"is_active": not proj['is_active']}).eq("id", proj['id']).execute()
                            st.rerun()
                    with col_btn3:
                        _delkey = f"confirm_del_{proj['id']}"
                        if not st.session_state.get(_delkey):
                            if st.button("🗑 削除", key=f"del_{proj['id']}", use_container_width=True):
                                st.session_state[_delkey] = True
                                st.rerun()
                        else:
                            # ⚠️ 誤削除防止：一度では消さず、必ず確認してから削除する
                            st.warning(f"「{proj['id']}」を本当に消しますか？\n**元に戻せません。**")
                            _dc1, _dc2 = st.columns(2)
                            with _dc1:
                                if st.button("はい、消す", key=f"delyes_{proj['id']}", type="primary",
                                             use_container_width=True):
                                    delete_project(proj['id'])
                                    st.session_state.pop(_delkey, None)
                                    st.toast(f"「{proj['id']}」を削除しました", icon="🗑")
                                    st.rerun()
                            with _dc2:
                                if st.button("キャンセル", key=f"delno_{proj['id']}", use_container_width=True):
                                    st.session_state.pop(_delkey, None)
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
        with col2: product_type = st.selectbox("仕事の種類（商材）", ["ネット", "電気", "ガス", "電気＆ガス", "その他"])

    with st.container(border=True):
        st.markdown("<div class='section-title'>📊 どこからデータを取りますか？</div>", unsafe_allow_html=True)
        sheet_url = st.text_input("SFA（スプレッドシート）のURL", placeholder="https://docs.google.com/spreadsheets/d/...")
        active_tab = st.text_input("読み込むタブの名前（任意・あとで決められます）", placeholder="例：INE用")
        st.caption("※タブ名は、あとで司令室の「最終シート」の段階で新規作成／既存から選んで決められます。"
                   "ロボットはこのスプシの「ステータス」が「未エントリー」の案件を処理します。")

    with st.container(border=True):
        st.markdown("<div class='section-title'>🎬 このロボットの種類は？</div>", unsafe_allow_html=True)
        entry_type = st.radio(
            "作業のタイプを選んでください",
            ["フォーム入力を自動化する（Webフォームに入力／録画します）",
             "CSV・Excelなど、フォーム入力ではない（録画しません）"],
            key="new_entry_type")
        needs_recording = entry_type.startswith("フォーム入力")
        if needs_recording:
            st.caption("次の画面でお手本を録画し、AIが手順書を作ります。")
        else:
            st.caption("録画は行わず、すぐにカラム設計（スプシの列・数式の設定）に進みます。")

    if st.button("次へ進む ➡️", type="primary"):
        if not new_name or not sheet_url: st.error("なまえとスプシのURLは必ず入力してください！")
        else:
            new_data = {
                "id": new_name, "name": new_name, "is_active": False, "connector_type": "playwright",
                "config_json": {
                    "product_type": product_type,
                    "needs_recording": needs_recording,
                    "spreadsheet": {"url": sheet_url, "tab_name": active_tab, "trigger_col": "ステータス", "trigger_val": "未エントリー"},
                    "robot_config": {"target_url": "", "steps": [], "stealth": True, "captcha": False, "success_text": ""},
                    "notifications": {"slack_id": "", "slack_msg": "自動申請が完了しました。"},
                    "conditions": []
                }
            }
            save_project(new_name, new_data)
            st.session_state.editing_project = new_name
            # 録画が不要なタイプは STEP2 を飛ばして司令室（カラム設計含む）へ直行する
            st.session_state.view = 'step2_record' if needs_recording else 'project_room'
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
                # 🔒 パスワード欄に打った文字は、AIに送る前にここで伏せる。
                #    ログインは本物でしか通らないので録画には実物が入るが、
                #    それをそのまま手順書（＝データベース）に残さないため。
                recorded_code, _n_redacted = _redact_passwords(recorded_code)
                if _n_redacted:
                    st.info(f"🔒 パスワード欄の入力 {_n_redacted}件を伏せました（`{{秘密:パスワード}}` に置き換え）。"
                            "実際のパスワードは「🔑 ログイン情報」で登録してください。"
                            "念のため、この画面の貼り付け欄も消しておくと安心です。")
                with st.spinner("🤖 AIがコードを解析中... しばらくお待ちください。"):
                    try:
                        # 📋 値の差し込み先候補：最終シートの列名が読めれば、値を {列名} に正しく対応づけられる
                        _cols_hint = ""
                        try:
                            _gc_rec = _get_gspread_client()
                            _url_rec = config.get("spreadsheet", {}).get("url", "")
                            _tab_rec = config.get("spreadsheet", {}).get("tab_name", "")
                            if _gc_rec and _url_rec and _tab_rec:
                                _fh_rec, _ = _read_final_sheet(_gc_rec, _url_rec, _tab_rec)
                                _cols_hint = "、".join(h for h in (_fh_rec or []) if h)
                        except Exception:
                            _cols_hint = ""

                        if _cols_hint:
                            _value_rule = (
                                "下の『使える列名』のうち、その入力に意味が最も近いものだけを {列名} の形で入れてください（例：{電話番号}）。"
                                "使える列名：" + _cols_hint + "。"
                                "どれにも当てはまらない入力は、値を空文字にしてください（後で人が最終シートの列を割り当てます）。"
                            )
                        else:
                            _value_rule = (
                                "その項目を表す短い日本語名を {列名} の形で入れてください（例：{お名前}、{電話番号}）。"
                                "録画で入力した実際のテスト値（例：自動化太郎）はそのまま書かないこと。"
                            )

                        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                        model = genai.GenerativeModel('gemini-2.5-flash')
                        # 🎯 録画のセレクタを"そのまま"ai_codeに使う（Geminiに書き換えさせない）＝録画通りに動かすための肝
                        prompt_tmpl = r"""【役割】あなたはPlaywrightの録画コードを、手順表(JSON)へ変換する変換器です。セレクタを推測で作ってはいけません。

【変換対象】ユーザーの操作行だけを手順にします：fill / click / check / set_checked / select_option。
次の行は手順にしないで無視してください：import、browser や context や page の生成、page.goto、expect、コメント行。

【最重要ルール（録画を壊さない）】
- 各手順の `ai_code` には、その操作の元のPlaywright文を **そのままコピー** してください。
- セレクタ（get_by_role / get_by_label / get_by_placeholder / locator など）は **絶対に書き換え・簡略化・推測しないこと。1文字も変えない。**
- 変えてよいのは「入力した値の文字列」だけです。下の『値』ルールに従って差し込み用に置き換えます。
  例：page.get_by_role("textbox", name="お名前").fill("自動化太郎")
    → ai_code: page.get_by_role("textbox", name="お名前").fill("{お名前}")
- 電話番号・郵便番号などの分割入力は、.split('-')[0] のような分割コードを ai_code 内に残す／組み込むこと。

【各列の作り方】
- 順番：1から連番。
- いつ：基本は「常に」。
- 操作：fill→「文字を入力」、click→「クリック」、select_option→「選択」、check／set_checked→「チェック」。
- 対象：その要素の name=（無ければ表示テキスト）を、人が読める日本語で。
- 値：__VALUE_RULE__

【出力】説明文は一切書かず、次の形のJSON配列のみを出力：
[ {"順番": 1, "いつ": "常に", "操作": "文字を入力", "対象": "お名前", "値": "{お名前}", "ai_code": "page.get_by_role(\"textbox\", name=\"お名前\").fill(\"{お名前}\")"} ]

【録画コード】
__RECORDED__
"""
                        prompt = prompt_tmpl.replace("__VALUE_RULE__", _value_rule).replace("__RECORDED__", recorded_code)
                        response = _gen_json(model, prompt)
                        config["robot_config"]["target_url"] = target_url
                        _new_steps = json.loads(response.text)
                        # 🧹 録画で入る「入力枠を選ぶだけの余分なクリック」は最初から入れない
                        #    （ボタン・送信・次へ・チェック・プルダウンなど必要な操作は残す）
                        _new_steps = _strip_redundant_field_clicks(_new_steps)
                        config["robot_config"]["steps"] = _new_steps
                        # 🦴 録画の骨組みを保存（設計から手順書を作り直す土台。値差し替え前の状態）
                        config["robot_config"]["skeleton"] = copy.deepcopy(_new_steps)
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
    # 完了したセクションの枠（st.container border）の左辺全体を緑にする。
    # 見出し内に置いた .enkan-done-green マーカーを含む枠を :has() で狙う。
    st.markdown("""
    <style>
      div[data-testid="stVerticalBlockBorderWrapper"]:has(.enkan-done-green) {
        border-left: 7px solid #16A34A !important;
      }
      .enkan-done-green { display: none; }
    </style>
    """, unsafe_allow_html=True)
    st.markdown(f"<div class='wizard-header'><h2>🎛️ 仕上げ：{proj_data['name']}</h2><p>あと少しです！ロボットの動きを確認して、テストすれば完成です。</p></div>", unsafe_allow_html=True)
    ch.guide("create", "できあがった手順を一緒に確認しよう。下の<b>「このロボットの動き」</b>を読んで、違っていたら手順書の表で直してね。最後に<b>お試し実行</b>すれば完成だよ！")

    _hb1, _hb2 = st.columns([1, 1.4])
    with _hb1:
        if st.button("⬅ ホームへ戻る", use_container_width=True):
            st.session_state.view = 'dashboard'; st.rerun()
    with _hb2:
        if st.button("🎬 録画をやり直す（手順だけ作り直す）", use_container_width=True,
                     help="カラム設計（最終シート・数式）やスプシ設定はそのまま。録画から手順書(steps)だけ作り直します。"):
            st.session_state.view = 'step2_record'; st.rerun()

    # 🧭 この画面の地図（縦に長いので、上から順の並びを最初に見せて「今どこ？」を防ぐ）
    _is_form_robot = config.get("needs_recording", True)
    _map_items = [
        "👀 このロボットの動き（かんたん確認）",
        "📝 基本設定の書き換え（URLなど）",
        "🧮 カラム設計（最終シート・数式の作成）",
        "🎯 このキャリアだけの特別ルール（完了の合図・控える値・AI相談）",
        "⚙️ こまかい設定（操作の速さ・Slack通知）",
        "🔀 条件分岐ルール（パターン）",
        "📝 自動入力の手順書（こまかい修正）",
    ]
    if not _is_form_robot:
        _map_items.append("📦 届け方 ／ ⚙️ GASジョブ連携（CSV・Excel型のみ）")
    _map_items.append("🧪 さいごに、お試し実行")
    with st.expander("🧭 この画面の地図（上から順の目次）", expanded=True):
        st.markdown("この画面は上から順にこう並んでいます。使うところまでスクロールしてください。")
        st.markdown("\n".join(f"{i}. {t}" for i, t in enumerate(_map_items, 1)))

    # 0. このロボットが何をするかを「やさしい日本語」で先に見せる（表を読めなくても分かる）
    valid_steps = [s for s in steps_data if s and (s.get("操作") or s.get("action"))]

    # 各セクションの「完了したか」を判定（見出しの左に色帯を出すため）。読み取りはキャッシュ済みで軽い。
    steps_done = bool(valid_steps)
    box_done = False
    final_done = False
    try:
        _gc0 = _get_gspread_client()
        _url0 = config.get("spreadsheet", {}).get("url", "")
        _tab0 = config.get("spreadsheet", {}).get("tab_name", "")
        if _gc0 and _url0:
            box_done = bool(_list_box_sheet_names(_gc0, _url0))
            if _tab0:
                _h0, _f0 = _read_final_sheet(_gc0, _url0, _tab0)
                final_done = any(bool(x) for x in _f0)
    except Exception:
        pass

    # 🗂 司令室を「タブ」に分割（縦長対策）。タブ切替は再実行せず表示だけ変わるので入力値は保持される。
    _tab_confirm, _tab_cols, _tab_steps, _tab_deliver, _tab_test = st.tabs(
        ["👀 確認", "🧮 基本・カラム設計", "🛠 手順・設定", "📦 届け方・GAS", "🧪 テスト"])
    with _tab_confirm:
        # 🗺 設定の見取り図：いま何がどう設定されていて、それが「どのタブのどこ」にあるかを1枚で見せる。
        #    設定が増えて探せなくなったため。ここは読むだけ（何も書き換えない）。
        with st.expander("🗺 設定の見取り図（今の状態と、設定場所の一覧）", expanded=True):
            _sc = config.get("spreadsheet", {}) or {}
            _rc = config.get("robot_config", {}) or {}
            _nt = config.get("notifications", {}) or {}
            _steps_all = _rc.get("steps", []) or []
            _linked = sum(1 for s in _steps_all if re.search(r"\{.+?\}", str((s or {}).get("値", "") or "")))
            _submit = sum(1 for s in _steps_all if _is_submit_when(s.get("いつ", s.get("condition", ""))))
            _fc = _rc.get("form_choices", []) or []
            _fc_items = sum(1 for c in _fc if c.get("items"))
            _caps = _rc.get("captures", []) or []
            _radio_linked = sum(1 for s in _steps_all if s.get("radio_group"))

            def _row(name, ok, state, where):
                return {"設定": ("✅ " if ok else "⚠️ ") + name, "今の状態": state, "設定する場所": where}

            _rows = [
                _row("全自動稼働（クラウド無人実行）", bool(proj_data.get("is_active")),
                     "ON" if proj_data.get("is_active") else "OFF（手動のみ）", "ホームのスイッチ"),
                _row("スプレッドシート", bool(_sc.get("url")),
                     f"シート名：{_sc.get('tab_name', '（未設定）')}", "🧮 基本・カラム設計 → 📝 基本設定の書き換え"),
                _row("申請フォームのURL", bool(_rc.get("target_url")),
                     (_rc.get("target_url", "") or "（未設定）")[:40] + "…", "🧮 基本・カラム設計 → 📝 基本設定の書き換え"),
                _row("参照する●●BOXシート", bool(_rc.get("box_ref")),
                     _rc.get("box_ref", "") or "未設定（数式を作る前に必ず選んでください）",
                     "🧮 基本・カラム設計 → 参照する●●BOXシート"),
                _row("入力の手順書", bool(_steps_all),
                     f"{len(_steps_all)}手順（うちスプシ連動 {_linked}）", "🛠 手順・設定 → 📝 自動入力の手順書"),
                _row("送信（申請）ステップ", _submit > 0,
                     f"{_submit}件" if _submit else "未設定＝本番でも申請が完了しません",
                     "🛠 手順・設定 → 📝 自動入力の手順書"),
                _row("申請完了の合図", bool(_rc.get("success_text") or _rc.get("success_url_contains")),
                     _rc.get("success_text", "") or "未設定＝申請が通ったか確認できません",
                     "🛠 手順・設定 → 🎯 特別ルール"),
                _row("フォームの選択肢（調べた結果）", bool(_fc),
                     f"{len(_fc)}件（うち選択肢の場所つき {_fc_items}件／ラジオ紐づけ済み手順 {_radio_linked}件）",
                     "🧮 基本・カラム設計 → 🔍 このフォームの選択肢を調べる"),
                _row("申請後に控える値", True,
                     "／".join(c.get("name", "") for c in _caps) if _caps else "なし（このキャリアは不要）",
                     "🛠 手順・設定 → 🎯 特別ルール"),
                _row("条件分岐ルール", True,
                     f"{len(config.get('conditions', []) or [])}件", "🛠 手順・設定 → 🔀 条件分岐ルール"),
                _row("Slack通知", bool(_nt.get("slack_id") or _nt.get("slack_msg")),
                     _nt.get("slack_id", "") or "未設定", "🛠 手順・設定 → ⚙️ こまかい設定"),
            ]
            st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
            st.caption("⚠️ は「まだ設定していない／確認したほうがよい」印です。実際のエントリーは"
                       "**ホームの「▶ エントリー開始」**から行います（この画面は設定用）。")

        with st.expander("👀 このロボットの動き（かんたん確認）", expanded=True):
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

    with _tab_cols:
        # 1. 基本設定（後から編集可能）
        with st.expander("📝 基本設定の書き換え（URLなど）"):
            c1, c2 = st.columns(2)
            with c1:
                e_sheet = st.text_input("SFAスプシURL", value=config.get('spreadsheet', {}).get('url', ''))
                e_tab = st.text_input("タブ名", value=config.get('spreadsheet', {}).get('tab_name', ''))
            with c2:
                e_target = st.text_input("入力フォームURL", value=config.get('robot_config', {}).get('target_url', ''))
            st.caption("スプシに表示された案件を上から全件エントリーします（保留したい案件はレポート側で非表示にしてください）。")

        # 🧮 カラム設計（●●BOXシートの作成・修正） — V1：AIに相談して確認してから反映
        with st.container(border=True):
            hdr1, hdr2 = st.columns([4, 1])
            with hdr1:
                _section_header("🧮 カラム設計（●●BOXシートの作成・修正）", done=box_done)
            with hdr2:
                if st.button("🔄 最新に更新", key=f"coldesign_refresh_{project_id}",
                             help="スプシを直接編集したときは、これを押すと最新の内容を読み直します。"):
                    st.cache_data.clear()
                    st.rerun()
            st.caption("SFAスプシの『BOX』から商品ごとに抽出する『●●BOX』シートを、AIに相談しながら作成・修正できます。"
                       "（読み込みは負荷軽減のため約2分キャッシュされます。直後の変更を見たいときは「最新に更新」）")

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
                        st.warning(f"シート一覧を取得できませんでした（一時的な可能性・直前の一覧を使います）: {e}")
                    existing_box_sheets = _stable_list(f"stable_boxsheets_{project_id}", existing_box_sheets)

                    if not existing_box_sheets:
                        st.info("『BOX』という文字を含むシートが、このスプシの中にまだ見つかりません。")
                    with st.expander("🔍 このスプシの全タブ名を確認する"):
                        try:
                            st.write(_list_all_sheet_names(gc, box_sheet_url))
                        except Exception as e:
                            st.error(f"タブ一覧の取得に失敗しました: {e}")

                    try:
                        master_headers, master_sample = _read_headers_and_sample(gc, box_sheet_url, "BOX")
                    except Exception:
                        master_headers, master_sample = [], []

                    col_mode = st.radio("何をしますか？", ["新しい商品のBOXシートを作る", "既存のBOXシートを直す"],
                                        key=f"box_mode_{project_id}", horizontal=True)

                    is_new = (col_mode == "新しい商品のBOXシートを作る")
                    if is_new:
                        new_product_name = st.text_input("タブ名（末尾の「BOX」は自動でつきます）",
                                                          placeholder="例：SB【INE】", key=f"box_new_name_{project_id}")
                        target_tab_name = f"{new_product_name}BOX" if new_product_name else ""
                        if new_product_name:
                            st.caption(f"作成されるシート名：**{target_tab_name}**")
                        ref_tab = st.selectbox("参考にする既存のBOXシート", existing_box_sheets,
                                               key=f"box_ref_{project_id}") if existing_box_sheets else None
                    else:
                        new_product_name = ""
                        ref_tab = st.selectbox("直したいBOXシート", existing_box_sheets,
                                               key=f"box_edit_target_{project_id}") if existing_box_sheets else None
                        target_tab_name = ref_tab

                    # 📋 大元の『BOX』見出しと、選んだシートの列一覧（列記号付き・2行目の値も表示）を上下に表示する
                    if master_headers:
                        _render_columns_table(master_headers, caption="大元の「BOX」シートの列一覧", values=master_sample)
                        if master_sample:
                            st.caption("⚠️ 「値(例)」は実際のデータの1件目です（個人情報を含む場合があります）。")

                    if ref_tab:
                        try:
                            ref_headers, ref_formula = _read_box_sheet(gc, box_sheet_url, ref_tab)
                            # 新規作成では列はBOXと同じになるので参考シートの列一覧は出さない（冗長なため）。
                            # 既存の修正では、今いじっているシートの列・実際の値・現在の数式を表示する。
                            if not is_new:
                                _, ref_sample = _read_headers_and_sample(gc, box_sheet_url, ref_tab)
                                _render_columns_table(ref_headers, caption=f"「{ref_tab}」の列一覧", values=ref_sample)
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
                            _render_columns_table(d['old_headers'], caption="今の列一覧")
                            st.caption(f"今の数式: `{d['old_formula']}`")
                            st.markdown("**新しい状態（案）**")
                        _render_columns_table(d['headers'], caption="作成される列一覧（案）")
                        st.caption("数式（案）:")
                        st.code(d['formula'], language="text")

                        cb1, cb2 = st.columns(2)
                        with cb1:
                            if st.button("✅ この内容で反映する", key=f"box_apply_{project_id}", type="primary"):
                                try:
                                    _apply_box_sheet(gc, box_sheet_url, d["tab_name"], d["headers"], d["formula"], d["is_new"])
                                    st.success(f"「{d['tab_name']}」に反映しました！")
                                    del st.session_state[draft_key]
                                    st.cache_data.clear()  # 書き込み後は最新を取り直す
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"反映に失敗しました: {e}")
                        with cb2:
                            if st.button("✖ 取り消す", key=f"box_cancel_{project_id}"):
                                del st.session_state[draft_key]
                                st.rerun()

        # 🧩 最終シートの列・数式作成＋手順書への自動反映（機能B・C）
        with st.container(border=True):
            _section_header("🧩 最終シートの列・数式作成（録画・手順書と連携）", done=final_done)
            st.caption("録画で必要になった項目ごとに、●●BOXのどの列をどう反映したいかAIに相談します。"
                       "反映すると、最終シートの列と、手順書のプレースホルダーの両方が同時に更新されます。")

            # 🔍 このフォームの選択肢（プルダウン/ラジオ）を取得して表示する。
            #    「次へ」で進む複数ページ型に対応：ブラウザを開いたまま、人が目的ページまで進めて取得できる。
            _form_url = config.get("robot_config", {}).get("target_url", "")
            _dir_key = f"insp_dir_{project_id}"
            _proc_key = f"insp_proc_{project_id}"
            _req_key = f"insp_req_{project_id}"
            _results_key = f"insp_results_{project_id}"
            # 保存済みの選択肢があれば復元する（ブラウザ更新しても消えないように）
            if _results_key not in st.session_state:
                st.session_state[_results_key] = list(config.get("robot_config", {}).get("form_choices", []))
            _rlink = st.session_state.pop(f"radiolink_{project_id}", None)
            if _rlink:
                st.success("🔘 ラジオの選択肢を手順書に紐づけました：" + "／".join(_rlink))
            with st.expander("🔍 このフォームの選択肢を調べる（プルダウン／ラジオの表記を確認）", expanded=False):
                st.caption("フォームが受け付ける選択肢（例：時間帯なら 12／13…）を吸い出します。"
                           "これを見て、スプシがどの表記になるよう数式を組めばよいか決められます。")
                st.caption("💻 これは**自分のPCで開いているとき**だけ使えます（取得用ブラウザがこのPCに開きます）。")

                _proc = st.session_state.get(_proc_key)
                _active = _proc is not None and _proc.poll() is None

                if not _form_url:
                    st.info("先にSTEP2でフォームのURLを設定してください。")
                elif not _active:
                    # まだブラウザが開いていない → 開始ボタン
                    if _proc is not None and _proc.poll() is not None:
                        st.info("取得用ブラウザは閉じられました。もう一度開くと続けて調べられます。")
                    if st.button("🔍 ブラウザを開いて調べ始める", key=f"insp_open_{project_id}"):
                        try:
                            _wdir = tempfile.mkdtemp(prefix="enkan_insp_")
                            _script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "form_inspect.py")
                            _p = subprocess.Popen([sys.executable, _script, "--interactive", _form_url, _wdir])
                            st.session_state[_dir_key] = _wdir
                            st.session_state[_proc_key] = _p
                            st.session_state[_req_key] = 0
                            st.session_state.setdefault(_results_key, [])
                            st.rerun()
                        except Exception as e:
                            st.error(f"ブラウザを開けませんでした（PCで開いていない可能性）。詳細: {e}")
                else:
                    # ブラウザ稼働中 → 目的ページまで人が進めてから取得
                    st.info("🖥 取得用ブラウザが開いています。**「次へ」で調べたいページまで進めてから**、下のボタンを押してください。"
                            "何ページでも繰り返せます。")
                    _cc1, _cc2 = st.columns([2, 1])
                    with _cc1:
                        if st.button("✅ 今のページの選択肢を取得", key=f"insp_grab_{project_id}", use_container_width=True):
                            _wdir = st.session_state.get(_dir_key)
                            _rid = int(st.session_state.get(_req_key, 0)) + 1
                            st.session_state[_req_key] = _rid
                            _got = None
                            try:
                                with open(os.path.join(_wdir, "req.txt"), "w", encoding="utf-8") as f:
                                    f.write(str(_rid))
                                _resp_path = os.path.join(_wdir, "resp.json")
                                with st.spinner("今のページを読み取り中..."):
                                    for _ in range(120):  # 最大約36秒待つ
                                        try:
                                            with open(_resp_path, "r", encoding="utf-8") as f:
                                                _d = json.load(f)
                                            if int(_d.get("req", 0)) == _rid:
                                                _got = _d.get("controls", [])
                                                break
                                        except Exception:
                                            pass
                                        time.sleep(0.3)
                            except Exception as e:
                                st.error(f"取得に失敗しました: {e}")
                            if _got is None:
                                st.warning("取得できませんでした。ブラウザが閉じていないかご確認ください。")
                            else:
                                _acc = st.session_state.setdefault(_results_key, [])
                                # 同じ選択欄をもう一度調べたら、新しい方で置き換える。
                                # （以前は「あれば足さない」だったため、選択肢の“住所”が付く前の
                                #   古いデータが残り続け、取り直しても実行時に使えなかった）
                                _idx_of = {(c.get("kind"), c.get("label")): i for i, c in enumerate(_acc)}
                                for _c in _got:
                                    _k = (_c.get("kind"), _c.get("label"))
                                    if _k in _idx_of:
                                        _acc[_idx_of[_k]] = _c
                                    else:
                                        _idx_of[_k] = len(_acc)
                                        _acc.append(_c)
                                # 💾 プロジェクトに保存（更新しても残す）
                                config.setdefault("robot_config", {})["form_choices"] = _acc
                                # 🔘 調べた直後に、手順書のラジオ操作へ自動で紐づける
                                #    （人が項目ごとに対応づけしなくても実行時に選べるように）
                                _st_now, _linked = _autolink_radio_groups(
                                    config["robot_config"].get("steps", []), _acc)
                                config["robot_config"]["steps"] = _st_now
                                if _linked:
                                    st.session_state[f"radiolink_{project_id}"] = _linked
                                proj_data["config_json"] = config
                                save_project(project_id, proj_data)
                                st.rerun()
                    with _cc2:
                        if st.button("✖ 終了（閉じる）", key=f"insp_stop_{project_id}", use_container_width=True):
                            _wdir = st.session_state.get(_dir_key)
                            try:
                                with open(os.path.join(_wdir, "stop.txt"), "w", encoding="utf-8") as f:
                                    f.write("1")
                            except Exception:
                                pass
                            try:
                                _proc.terminate()
                            except Exception:
                                pass
                            st.session_state[_proc_key] = None
                            st.rerun()

                # これまでに集めた選択肢（全ページ分）を表示
                _acc = st.session_state.get(_results_key, [])
                if _acc:
                    st.markdown("---")
                    st.success(f"これまでに {len(_acc)} 個の選択欄が見つかりました：")
                    st.caption("録画で抜けた項目は「＋手順に追加」で足せます（再録画不要）。値はあとで列と紐づけ＝スプシ連動になります。")
                    # 既に手順書にある対象名（重複追加を防ぐ）
                    _existing_targets = {str(s.get("対象", s.get("target_description", "")) or "").strip()
                                         for s in config.get("robot_config", {}).get("steps", []) if s}
                    for _i, _c in enumerate(_acc):
                        if _c.get("kind") == "error":
                            continue
                        _kind = "プルダウン" if _c.get("kind") == "select" else "ラジオ"
                        _lab = _c.get("label") or "（名前なし）"
                        _r1, _r2 = st.columns([3, 1])
                        with _r1:
                            st.markdown(f"**{_kind}：{_lab}**")
                            st.caption("選べる値：　" + "　／　".join(_c.get("options", [])))
                        with _r2:
                            if not _c.get("selector"):
                                st.caption("（追加不可）")
                            elif _lab in _existing_targets:
                                st.caption("✅ 手順にあり")
                            elif st.button("＋ 手順に追加", key=f"insp_addstep_{project_id}_{_i}", use_container_width=True):
                                _sel = _c["selector"]
                                if _c.get("kind") == "select":
                                    _ai = f'page.locator("{_sel}").select_option("{{{_lab}}}")'
                                    _op = "選択"
                                else:
                                    _ai = f'page.get_by_role("radio", name="{{{_lab}}}").check()'
                                    _op = "チェック"
                                _steps2 = config.get("robot_config", {}).get("steps", [])
                                _orders = [int(s.get("順番", s.get("order", 0)) or 0) for s in _steps2 if s]
                                _nxt = (max(_orders) if _orders else 0) + 1
                                _newstep = {"順番": _nxt, "いつ": "常に", "操作": _op, "対象": _lab,
                                            "値": "{" + _lab + "}", "変換": "", "ai_code": _ai}
                                _steps2.append(_newstep)
                                config["robot_config"]["steps"] = _steps2
                                # 🦴 骨組みにも追加（作り直しても残る）
                                _skel = config.get("robot_config", {}).get("skeleton", [])
                                if not any(str(s.get("対象", "") or "").strip() == _lab for s in _skel if s):
                                    _skel.append(copy.deepcopy(_newstep))
                                    config["robot_config"]["skeleton"] = _skel
                                proj_data["config_json"] = config
                                save_project(project_id, proj_data)
                                st.toast(f"手順に「{_lab}」を追加しました", icon="✅")
                                st.rerun()
                    if st.button("🗑 一覧をクリア", key=f"insp_clear_{project_id}"):
                        st.session_state[_results_key] = []
                        config.setdefault("robot_config", {})["form_choices"] = []
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                        st.rerun()

            if gc is None:
                st.info("上の「カラム設計」と同じく、サービスアカウントの設定が必要です。")
            else:
                # 最終シート（●●）は「既存を使う」か「新しく作る」で決める。
                # ここで決めた名前がロボットの読み込み先(tab_name)になる（STEP1では決めなくてよい）。
                saved_tab = config.get('spreadsheet', {}).get('tab_name', '').strip()
                try:
                    all_sheets = _list_all_sheet_names(gc, box_sheet_url)
                except Exception:
                    all_sheets = []
                final_candidates = [t for t in all_sheets
                                    if t.strip().upper() != "BOX" and "BOX" not in t.upper() and "原本" not in t]
                final_candidates = _stable_list(f"stable_finalcands_{project_id}", final_candidates)
                final_mode = st.radio("最終シートは？", ["既存のシートを使う", "新しく作る"],
                                      index=0 if (saved_tab and saved_tab in final_candidates) else 1,
                                      key=f"final_mode_{project_id}", horizontal=True)
                if final_mode == "既存のシートを使う":
                    if final_candidates:
                        d_idx = final_candidates.index(saved_tab) if saved_tab in final_candidates else 0
                        final_tab_name = st.selectbox("使う最終シート", final_candidates, index=d_idx,
                                                      key=f"final_pick_{project_id}")
                    else:
                        st.info("使えそうな既存シートが見つかりません。「新しく作る」を選んでください。")
                        final_tab_name = ""
                else:
                    final_tab_name = st.text_input("新しい最終シートの名前", value=saved_tab,
                                                   placeholder="例：SB【INE】",
                                                   key=f"final_new_name_{project_id}").strip()

                if final_tab_name and final_tab_name != saved_tab:
                    if st.button(f"💾 最終シートを「{final_tab_name}」に決定して保存", key=f"final_settab_{project_id}"):
                        sheet_cfg = dict(config.get('spreadsheet', {}))
                        sheet_cfg['tab_name'] = final_tab_name
                        config['spreadsheet'] = sheet_cfg
                        proj_data['config_json'] = config
                        save_project(project_id, proj_data)
                        st.success(f"最終シートを「{final_tab_name}」に設定しました。")
                        st.rerun()

                if not final_tab_name:
                    st.info("最終シートを選ぶ／新しい名前を入力してください。")
                else:
                    st.caption(f"最終シートは「{final_tab_name}」として扱います。")
                    try:
                        final_exists = _final_sheet_exists(gc, box_sheet_url, final_tab_name)
                    except Exception:
                        final_exists = True  # 判定できないときは既存扱い（余計な新規作成を避ける）
                    if not final_exists:
                        st.info(f"「{final_tab_name}」シートはまだありません。列を反映すると、このシートを新しく作成します。")
                    try:
                        box_choices_for_final = _list_box_sheet_names(gc, box_sheet_url)
                    except Exception:
                        box_choices_for_final = []
                    box_choices_for_final = _stable_list(f"stable_boxchoices_{project_id}", box_choices_for_final)
                    # 📌 参照BOXはロボットごとに保存する。以前は開き直すたび先頭のシートに戻り、
                    #    気づかないまま別商材のBOX（例：SB【INE】BOX）を参照した数式が作られていた。
                    _boxref_key = f"final_box_ref_{project_id}"
                    _saved_boxref = str(config.get("robot_config", {}).get("box_ref", "") or "")
                    if box_choices_for_final and _boxref_key not in st.session_state and _saved_boxref in box_choices_for_final:
                        st.session_state[_boxref_key] = _saved_boxref
                    box_ref_for_final = (st.selectbox("参照する●●BOXシート", box_choices_for_final,
                                                       key=_boxref_key,
                                                       help="このロボットの数式が参照するBOXシートです。選ぶと保存され、次に開いたときもこのままです。")
                                          if box_choices_for_final else None)
                    if box_ref_for_final and box_ref_for_final != _saved_boxref:
                        # 選び直したらすぐ保存（次に開いたときも同じBOXを参照する）
                        config.setdefault("robot_config", {})["box_ref"] = box_ref_for_final
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                    if _saved_boxref and box_ref_for_final and _saved_boxref != box_ref_for_final:
                        st.warning(f"⚠️ 参照BOXを「{_saved_boxref}」から「{box_ref_for_final}」に変更しました。"
                                   "このあと作る数式は新しいBOXを参照します。")

                    # ✏️ 最終シートの1行目（列名）を自分でまとめて入力する（スプシからコピペも可）
                    # フォーム入力ロボットは、下の「項目→列名」設計で録画の項目から列を作る／既存を直すので、
                    # 手動での列名貼り付けは不要（左詰めで1行目を上書きする事故も防ぐ）。CSV・Excel型のみ表示する。
                    if not config.get("needs_recording", True):
                        with st.expander("✏️ 最終シートの列名を自分で入力する（1行目）"):
                            st.caption("スプシ／Excelの1行目をコピーして①の欄に貼り付け→「↧ セルに分ける」を押すと、"
                                       "1項目ずつ下の表のセルに分かれます。表は直接なおしたり、行を足し引きもできます。")
                            _grid_key = f"final_headers_grid_{project_id}"
                            # ① 貼り付け欄（タブ区切りのスプシコピーをそのまま受ける）→ ボタンで下の表に取り込む
                            pasted = st.text_area("① ここにスプシの1行目を貼り付け（タブ区切りのままでOK）",
                                                  key=f"final_paste_headers_{project_id}",
                                                  placeholder="氏名\t電話番号\t郵便番号 …（← タブ区切り。カンマ・改行区切りも可）")
                            if st.button("↧ セルに分ける（下の表に取り込む）", key=f"final_import_headers_{project_id}"):
                                hs = _parse_pasted_headers(pasted)
                                if not hs:
                                    st.warning("貼り付け欄が空です。スプシの1行目をコピーして貼り付けてください。")
                                else:
                                    st.session_state[_grid_key] = pd.DataFrame({"列名": hs})
                                    st.rerun()
                            # ② 分かれたセルを直接編集（スプシのように1マス1項目。行の追加・削除も可）
                            _cur_df = st.session_state.get(_grid_key, pd.DataFrame({"列名": pd.Series([], dtype="object")}))
                            edited = st.data_editor(
                                _cur_df, num_rows="dynamic", use_container_width=False,
                                key=f"final_headers_editor_{project_id}",
                                column_config={"列名": st.column_config.TextColumn(
                                    "列名（1マス＝1項目）", help="スプシの1行目にそのまま入る見出しです。")})
                            parsed_headers = [str(x).strip() for x in edited["列名"].tolist()
                                              if str(x).strip() and str(x).strip().lower() != "nan"]
                            if parsed_headers:
                                _render_columns_table(parsed_headers,
                                                      caption=f"この並びで1行目を作ります（全{len(parsed_headers)}列）")
                            if st.button("📝 この列名で1行目を作る", key=f"final_set_headers_{project_id}"):
                                if not parsed_headers:
                                    st.warning("列名を入力してください（上の①に貼り付けて「セルに分ける」か、表に直接入力）。")
                                else:
                                    try:
                                        _set_final_headers(gc, box_sheet_url, final_tab_name, parsed_headers)
                                        st.success(f"「{final_tab_name}」の1行目を設定しました！")
                                        st.cache_data.clear()  # 書き込み後は最新を取り直す
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"設定に失敗しました: {e}")

                    candidates = _get_candidate_fields(config)
                    field_options = [c["target"] for c in candidates]
                    batch_draft_key = f"final_batch_draft_{project_id}"
                    fix_draft_key = f"fix_draft_{project_id}"
                    TRANSFORM_TEMPLATES = {
                        "そのまま入れる": "「{col}」の値をそのまま入れたい",
                        "市外局番（電話番号の1つ目）": "「{col}」の電話番号の市外局番（最初のハイフンより前）だけを入れたい。先頭の0が消えないよう、SPLITは使わずREGEXEXTRACTで文字列のまま取り出すこと。",
                        "市内局番（電話番号の2つ目）": "「{col}」の電話番号の市内局番（1つ目と2つ目のハイフンの間）だけを入れたい。先頭の0が消えないよう、SPLITは使わずREGEXEXTRACTで文字列のまま取り出すこと。",
                        "加入者番号（電話番号の3つ目）": "「{col}」の電話番号の加入者番号（最後のハイフンより後）だけを入れたい。先頭の0が消えないよう、SPLITは使わずREGEXEXTRACTで文字列のまま取り出すこと。",
                        "ハイフンを除く": "「{col}」からハイフン（-）を取り除いた値を入れたい",
                        "数字だけ取り出す": "「{col}」から数字だけを取り出した値を入れたい",
                        "郵便番号の上3桁": "「{col}」の郵便番号の上3桁だけを入れたい。先頭の0が消えないよう、数値化せず文字列（TEXT/REGEXEXTRACT/LEFT）で取り出すこと。",
                        "郵便番号の下4桁": "「{col}」の郵便番号の下4桁だけを入れたい。先頭の0が消えないよう、数値化せず文字列（TEXT/REGEXEXTRACT/RIGHT）で取り出すこと。",
                        "固定の文字を入れる": "この列にはいつも同じ文字（例：）を入れたい",
                    }

                    box_headers_for_final, final_headers, final_formulas = [], [], []
                    if not box_ref_for_final:
                        st.warning("先に上の「参照する●●BOXシート」を選んでください。")
                    else:
                        try:
                            box_headers_for_final, box_sample = _read_headers_and_sample(gc, box_sheet_url, box_ref_for_final)
                            _render_columns_table(box_headers_for_final, caption=f"「{box_ref_for_final}」の列一覧", values=box_sample)
                            if box_sample:
                                st.caption("⚠️ 「値(例)」は実際のデータの1件目です（個人情報を含む場合があります）。")
                        except Exception as e:
                            st.error(f"列一覧の取得に失敗しました: {e}")
                        try:
                            final_headers, final_formulas = _read_final_sheet(gc, box_sheet_url, final_tab_name)
                        except Exception:
                            final_headers, final_formulas = [], []

                        if not field_options:
                            manual = st.text_input("項目名を直接入力（録画が無い商品など・カンマ/改行区切りで複数可）",
                                                   key=f"final_manual_fields_{project_id}")
                            field_options = _parse_pasted_headers(manual)

                        # 事前準備：3択の定数と、項目→既存列/数式の対応
                        MODE_FORMULA = "数式を入れる"
                        MODE_HEADER = "見出しだけ作る（数式は後日）"
                        MODE_SKIP = "スキップ（列を作らない）"
                        field_placeholders = {c["target"]: c.get("current_placeholders", []) for c in candidates}
                        col_to_formula = {h: f for h, f in zip(final_headers, final_formulas) if f}

                        # ① 各項目を「数式を入れる／見出しだけ作る／スキップ」から選ぶ（AIに列名を作らせない）
                        if field_options:
                            st.markdown("**① 各項目をどうするか決める（1つずつ）**")
                            st.caption("項目ごとに「数式を入れる／見出しだけ作る（数式は後日）／スキップ（列を作らない）」を選びます。"
                                       "列名はあなたが決めます（AIが勝手に列を増やしません）。")

                            store = st.session_state.setdefault(f"batchstore_{project_id}", {})        # 説明文
                            choicestore = st.session_state.setdefault(f"choicestore_{project_id}", {})  # フォーム選択肢
                            modestore = st.session_state.setdefault(f"modestore_{project_id}", {})      # 3択
                            colstore = st.session_state.setdefault(f"colstore_{project_id}", {})        # 列名
                            # ラジオはどのグループの選択肢かまで覚える（同じ文言が複数グループにあるため）
                            grouplabelstore = st.session_state.setdefault(f"grouplabel_{project_id}", {})
                            # 保存済みの設計を復元（セッションが切れても残す）。セッション中は一度だけ。
                            _design_loaded_key = f"design_loaded_{project_id}"
                            if not st.session_state.get(_design_loaded_key):
                                for _fld, _d in (config.get("robot_config", {}).get("design", {}) or {}).items():
                                    if not isinstance(_d, dict):
                                        continue
                                    if _d.get("mode"):
                                        modestore.setdefault(_fld, _d["mode"])
                                    if _d.get("col"):
                                        colstore.setdefault(_fld, _d["col"])
                                    if _d.get("choice"):
                                        choicestore.setdefault(_fld, _d["choice"])
                                    if _d.get("radio_group"):
                                        grouplabelstore.setdefault(_fld, _d["radio_group"])
                                st.session_state[_design_loaded_key] = True

                            bidx_key = f"batch_idx_{project_id}"
                            bidx = max(0, min(st.session_state.get(bidx_key, 0), len(field_options) - 1))
                            f = field_options[bidx]

                            st.progress((bidx + 1) / len(field_options))

                            # 🔀 好きな項目へ直接ジャンプ（「次へ」の連打を避ける）。✅=入力済み
                            def _fmt_jump(i):
                                _done = "✅" if str(store.get(field_options[i], "")).strip() else "⬜"
                                return f"{i + 1}. {field_options[i]} {_done}"
                            _jump = st.selectbox("▼ 項目を選んで移動", list(range(len(field_options))),
                                                 index=bidx, format_func=_fmt_jump)
                            if _jump != bidx:
                                # 移動前に、今の項目の入力を保存する（描画されない欄はStreamlitが値を破棄するため）
                                _wk = f"batchdesc_{project_id}_{f}"
                                if _wk in st.session_state:
                                    store[f] = st.session_state[_wk]
                                _mk = f"mode_{project_id}_{f}"
                                if _mk in st.session_state:
                                    modestore[f] = st.session_state[_mk]
                                # 列は「選ぶ」か「新しく作る」のどちらか。今出ているほうを覚える
                                _pk = f"colpick_{project_id}_{f}"
                                _ck = f"colname_{project_id}_{f}"
                                if st.session_state.get(_mk) != MODE_SKIP:
                                    _sel = st.session_state.get(_pk)
                                    if _sel and _sel != "＋ 新しい列を作る":
                                        colstore[f] = _sel
                                    elif _ck in st.session_state:
                                        colstore[f] = st.session_state[_ck]
                                st.session_state[bidx_key] = _jump
                                st.rerun()

                            st.markdown(f"**項目 {bidx + 1} / {len(field_options)}：「{f}」**")

                            # この項目に対応する既存列・数式（あれば）
                            existing_formula, existing_col = "", ""
                            for ph in field_placeholders.get(f, []):
                                if ph in col_to_formula:
                                    existing_formula, existing_col = col_to_formula[ph], ph
                                    break
                            if existing_col:
                                st.caption(f"現在の対応列：「{existing_col}」" + ("（数式あり）" if existing_formula else "（数式なし）"))
                                if existing_formula:
                                    st.code(existing_formula, language="text")

                            # 3択（既存で数式ありなら初期はスキップ、それ以外は数式を入れる）
                            mode_key = f"mode_{project_id}_{f}"
                            if mode_key not in st.session_state:
                                st.session_state[mode_key] = modestore.get(f) or (MODE_SKIP if existing_formula else MODE_FORMULA)
                            mode = st.radio("この項目をどうする？", [MODE_FORMULA, MODE_HEADER, MODE_SKIP], key=mode_key)
                            modestore[f] = mode

                            # 列名（スキップ以外で使う）。
                            # すでにあるシートでは、列名を手で打たせると1文字の違いで繋がらない。
                            # 実際にある列から選べるようにして、打ち間違いを無くす。
                            if mode != MODE_SKIP:
                                default_col = existing_col or f
                                _NEW = "＋ 新しい列を作る"
                                _opts = [_NEW] + list(final_headers)
                                _pick_key = f"colpick_{project_id}_{f}"
                                _saved = colstore.get(f, default_col)
                                if _pick_key not in st.session_state:
                                    st.session_state[_pick_key] = _saved if _saved in final_headers else _NEW
                                _picked = st.selectbox(
                                    "どの列に入れる？", _opts, key=_pick_key,
                                    help="すでにある列を選べば、その列を使います（新しい列は増えません）。"
                                         "無ければ「＋ 新しい列を作る」で名前を決めます。")
                                col_key = f"colname_{project_id}_{f}"
                                if _picked == _NEW:
                                    if col_key not in st.session_state:
                                        st.session_state[col_key] = _saved if _saved not in final_headers else f
                                    colstore[f] = st.text_input(
                                        "新しく作る列の名前", key=col_key,
                                        help="この名前の列を最終シートの右端に足します")
                                else:
                                    colstore[f] = _picked
                                    _fx = col_to_formula.get(_picked, "")
                                    if _fx and mode == MODE_FORMULA:
                                        st.warning("⚠️ この列にはすでに数式が入っています。"
                                                   "このまま反映すると**上書き**されます。"
                                                   "いまの数式を残すなら「見出しだけ作る」を選んでください。")
                                        st.code(_fx, language="text")
                                    elif _fx:
                                        st.caption("この列の数式はそのまま残します（手順書だけこの列に繋ぎます）。")
                            else:
                                colstore.pop(f, None)

                            # ↩ この項目だけ「録画の動作(固定)」に戻す（列連動をやめる）。連動中のときだけ表示。
                            _skel_rv = config.get("robot_config", {}).get("skeleton", [])
                            _cur_col_rv = _current_col_for_field(config.get("robot_config", {}).get("steps", []), f)
                            if _skel_rv and _cur_col_rv:
                                if st.button("↩ この項目を録画の動作(固定)に戻す", key=f"revert_{project_id}_{f}",
                                             help="スプシ連動をやめ、録画したときの値・動きに戻します。列は消しませんが、この項目はセルを見ずに録画どおり動きます。"):
                                    _steps_rv = config.get("robot_config", {}).get("steps", [])
                                    config["robot_config"]["steps"] = _revert_field_to_recorded(_steps_rv, _skel_rv, f)
                                    # 設計もスキップにして、今後の反映/リセットで再連動しないようにする
                                    config.get("robot_config", {}).setdefault("design", {})[f] = {"mode": MODE_SKIP}
                                    proj_data["config_json"] = config
                                    save_project(project_id, proj_data)
                                    modestore[f] = MODE_SKIP
                                    st.session_state.pop(mode_key, None)
                                    st.session_state.pop(f"colname_{project_id}_{f}", None)
                                    st.success(f"「{f}」を録画の動作(固定)に戻しました。")
                                    st.cache_data.clear()
                                    st.rerun()

                            # 📍 フォームの選択欄との対応づけは、数式を作らないとき（既存の列を使うとき）にも要る。
                            #    ここで選んでおかないと、ラジオがどのグループの選択肢か手順書に残らず、
                            #    同じ文言の別グループを選んでしまう。だからモードに関わらず出す。
                            widget_key = f"batchdesc_{project_id}_{f}"
                            if mode == MODE_FORMULA:
                                if widget_key not in st.session_state:
                                    st.session_state[widget_key] = store.get(f, "")
                                st.text_area(f"「{f}」をどう反映したいか", key=widget_key, height=80,
                                             placeholder="例：「電話番号」列の市外局番だけを入れたい")
                                store[f] = st.session_state.get(widget_key, "")

                            if mode != MODE_SKIP:
                                _insp_ctrls = [c for c in st.session_state.get(f"insp_results_{project_id}", [])
                                               if c.get("kind") in ("select", "radio") and c.get("options")]
                                if _insp_ctrls:
                                    _clabels = ["（対応づけない）"] + [
                                        f"{'プルダウン' if c['kind'] == 'select' else 'ラジオ'}：{c.get('label') or '（名前なし）'}（{len(c['options'])}択）"
                                        for c in _insp_ctrls]
                                    _cmap_key = f"choicemap_{project_id}_{f}"
                                    if _cmap_key not in st.session_state:
                                        _dl = "（対応づけない）"
                                        _sv = choicestore.get(f)
                                        if _sv:
                                            for _ix, _cc in enumerate(_insp_ctrls):
                                                if _cc.get("options") == _sv:
                                                    _dl = _clabels[_ix + 1]
                                                    # 以前に対応づけ済みの項目にも、ラジオならグループ名を補う
                                                    # （この機能を作る前に設定した項目を、選び直さずに活かすため）
                                                    if _cc.get("kind") == "radio" and not grouplabelstore.get(f):
                                                        grouplabelstore[f] = str(_cc.get("label", "") or "")
                                                    break
                                        st.session_state[_cmap_key] = _dl
                                    _csel = st.selectbox("この項目の『フォームの選択欄』を見る（選択肢を確認）", _clabels, key=_cmap_key,
                                                         help="選ぶと、その選択欄で選べる値を下に表示します。必要なものだけ説明にコピペ／挿入できます。")
                                    if _csel != "（対応づけない）":
                                        _ctrl = _insp_ctrls[_clabels.index(_csel) - 1]
                                        _opts = _ctrl["options"]
                                        choicestore[f] = _opts
                                        # 📍 どの選択欄かを手順書にも記録する（ラジオは同じ文言の選択肢が
                                        #    複数グループにあると取り違えるため、グループを名指しできるようにする）
                                        if _ctrl.get("kind") == "radio":
                                            grouplabelstore[f] = str(_ctrl.get("label", "") or "")
                                        else:
                                            grouplabelstore.pop(f, None)
                                        st.caption("この選択欄で選べる値（必要なものを下の説明にコピペ／挿入してください）：")
                                        st.code("　".join(_opts), language="text")
                                        if mode == MODE_FORMULA:
                                            st.button("＋ 選択肢を説明に挿入", key=f"insopts_{project_id}_{f}",
                                                      on_click=_append_to_desc,
                                                      args=(widget_key, "フォームの選択肢：" + " / ".join(_opts)))
                                    else:
                                        choicestore.pop(f, None)
                                        grouplabelstore.pop(f, None)

                            if mode == MODE_FORMULA:
                                with st.expander("🧩 説明の書き方の例（クリックで上の欄に入る）"):
                                    tt1, tt2 = st.columns(2)
                                    with tt1:
                                        tmpl_col = st.selectbox("列", box_headers_for_final or ["（列なし）"],
                                                                key=f"tmpl_col_{project_id}")
                                    with tt2:
                                        tmpl_kind = st.selectbox("加工", list(TRANSFORM_TEMPLATES.keys()),
                                                                 key=f"tmpl_kind_{project_id}")
                                    _tmpl_sentence = TRANSFORM_TEMPLATES[tmpl_kind].format(col=tmpl_col)
                                    st.code(_tmpl_sentence, language="text")
                                    st.button("＋ この項目の説明に追加", key=f"tmpl_add_{project_id}",
                                              on_click=_append_to_desc,
                                              args=(f"batchdesc_{project_id}_{f}", _tmpl_sentence))
                            else:
                                store[f] = ""

                            nav1, nav2, nav3 = st.columns([1, 1, 2])
                            with nav1:
                                if st.button("⬅ 前へ", key=f"batch_prev_{project_id}", disabled=(bidx == 0),
                                             use_container_width=True):
                                    st.session_state[bidx_key] = bidx - 1
                                    st.rerun()
                            with nav2:
                                if st.button("次へ ➡", key=f"batch_next_{project_id}",
                                             disabled=(bidx >= len(field_options) - 1), use_container_width=True):
                                    st.session_state[bidx_key] = bidx + 1
                                    st.rerun()
                            with nav3:
                                _n_formula = sum(1 for ff in field_options if modestore.get(ff) == MODE_FORMULA)
                                _n_header = sum(1 for ff in field_options if modestore.get(ff) == MODE_HEADER)
                                st.caption(f"数式:{_n_formula}／見出しだけ:{_n_header}／全{len(field_options)}項目")

                            if st.button("🤖 数式を作って反映する", type="primary", key=f"batch_ask_{project_id}"):
                                # 数式モードの項目だけAIへ（列名はこちらで固定＝AIに作らせない）
                                filled = {}
                                for ff in field_options:
                                    if modestore.get(ff) != MODE_FORMULA:
                                        continue
                                    _desc = str(store.get(ff, "")).strip()
                                    if not _desc:
                                        continue
                                    filled[ff] = _desc
                                _need_ai = bool(filled)
                                if _need_ai and not str(st.secrets.get("GEMINI_API_KEY", "")).strip():
                                    st.error("⚠️ AIを使うには接続キーに GEMINI_API_KEY の設定が必要です。"
                                             "（ローカルは .streamlit/secrets.toml、クラウドは Secrets に追加してください）")
                                else:
                                    try:
                                        ai_formulas = {}
                                        if _need_ai:
                                            with st.spinner(f"🤖 {len(filled)}項目の数式をまとめて作っています..."):
                                                drafts = _draft_all_final_columns(
                                                    box_ref_for_final, box_headers_for_final,
                                                    final_headers, final_formulas, filled)
                                            for d in drafts:
                                                ai_formulas[d.get("target_field", "")] = d.get("formula", "")
                                        # 🔎 AIが項目名を少し変えて返しても拾えるようにする（表記ゆれ吸収）
                                        _ai_norm = {_norm_key(k): v for k, v in ai_formulas.items() if v}
                                        # プラン組み立て（列名は colstore＝ユーザー指定を使う）
                                        plan = []
                                        for ff in field_options:
                                            m = modestore.get(ff, MODE_SKIP)
                                            if m == MODE_SKIP:
                                                continue
                                            col = str(colstore.get(ff, "") or ff).strip()
                                            if m == MODE_FORMULA:
                                                if not str(store.get(ff, "")).strip():
                                                    continue
                                                _got = ai_formulas.get(ff, "") or _ai_norm.get(_norm_key(ff), "")
                                                plan.append({"field": ff, "col": col, "mode": m,
                                                             "formula": _got})
                                            else:  # 見出しだけ
                                                plan.append({"field": ff, "col": col, "mode": m, "formula": ""})
                                        # 💾 確認は挟まず、そのまま反映する（その場で個別修正はしないため）
                                        steps_now = config.get("robot_config", {}).get("steps", [])
                                        # 各項目が「今使っている列名」を先に記録（列名変更＝改名として扱うため）
                                        _old_cols = {d["field"]: _current_col_for_field(steps_now, d["field"]) for d in plan}
                                        fh = list(final_headers)
                                        _report = []  # 📋 何をどこに書いたかの記録（画面に出して「黙って未反映」を防ぐ）
                                        for d in plan:
                                            col = d["col"]
                                            if not col:
                                                _report.append({"項目": d["field"], "列": "（列名が空）", "セル": "-",
                                                                "結果": "⚠️ 何もしていません"})
                                                continue
                                            # 🔁 列名を変えた場合は「改名」＝旧列の見出しをその場で付け替える（新列を作らない）
                                            _old = _old_cols.get(d["field"], "")
                                            if _old and _old != col and _old in fh:
                                                try:
                                                    _rename_final_header(gc, box_sheet_url, final_tab_name, _old, col)
                                                except Exception:
                                                    pass
                                                fh = [col if h == _old else h for h in fh]
                                            # 🛡️ デフォルトガード：案件ID（参照BOXのA列）が空の行は空白にする。
                                            #    案件が無い行に固定文字や参照結果が残るのを防ぐ（ユーザーの条件はそのまま内側に残す）。
                                            _f = d.get("formula", "")
                                            if _f:
                                                _f = _wrap_blank_when_no_id(_f, box_ref_for_final)
                                            _new_col = col not in fh
                                            _idx = _apply_final_column(gc, box_sheet_url, final_tab_name, fh, col, _f)
                                            if _f:
                                                _res = ("✅ 数式を書き込み" + ("（新しい列を作成）" if _new_col else "（上書き）"))
                                            elif d["mode"] == MODE_FORMULA:
                                                _res = "⚠️ AIが数式を返さなかったので2行目は元のままです"
                                            else:
                                                _res = "見出しだけ作成（2行目は触っていません）"
                                            _report.append({"項目": d["field"], "列": col,
                                                            "セル": f"{_col_letter(_idx)}2" if _idx else "-",
                                                            "結果": _res})
                                            if col not in fh:
                                                fh.append(col)
                                            steps_now = _link_step_value(
                                                steps_now, d["field"], col,
                                                old_names=field_placeholders.get(d["field"], []))
                                            # 📍 ラジオはどのグループの選択肢かを手順にも書いておく
                                            #    （実行時、同じ文言の選択肢を別グループから選ばないため）
                                            #    ⚠️ 対応づけが分からないときは触らない。空で上書きすると、
                                            #       前に設定したグループまで消えてしまうため。
                                            if grouplabelstore.get(d["field"]):
                                                steps_now = _set_step_radio_group(
                                                    steps_now, d["field"], grouplabelstore[d["field"]])
                                        # 💾 設計（項目→モード・列名・選択欄）を保存＝次回も残る／作り直しに使える
                                        _design = {}
                                        for _ff in field_options:
                                            _m = modestore.get(_ff)
                                            if not _m:
                                                continue
                                            _entry = {"mode": _m}
                                            if colstore.get(_ff):
                                                _entry["col"] = colstore[_ff]
                                            if choicestore.get(_ff):
                                                _entry["choice"] = choicestore[_ff]
                                            if grouplabelstore.get(_ff):
                                                _entry["radio_group"] = grouplabelstore[_ff]
                                            _design[_ff] = _entry
                                        config["robot_config"]["design"] = _design
                                        # 🔘 スキップした項目もふくめ、ラジオの紐づけは毎回やり直す
                                        steps_now, _ = _autolink_radio_groups(
                                            steps_now, config["robot_config"].get("form_choices", []))
                                        config["robot_config"]["steps"] = steps_now
                                        proj_data["config_json"] = config
                                        save_project(project_id, proj_data)
                                        # 「数式を入れる」なのに説明が空＝AIに送られず何も起きない項目も記録する
                                        for ff in field_options:
                                            if modestore.get(ff) == MODE_FORMULA and not str(store.get(ff, "")).strip():
                                                _report.append({"項目": ff, "列": str(colstore.get(ff, "") or ff),
                                                                "セル": "-", "結果": "⚠️ 説明が空のため何もしていません"})
                                        # 📋 rerun後もレポートが残るように保存（今までは一瞬で消えて気づけなかった）
                                        st.session_state[f"apply_report_{project_id}"] = _report
                                        st.cache_data.clear()
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"反映に失敗しました: {e}")

                    # 📋 直前の「反映」の結果（どの列のどのセルに書いたか／書けなかったか）
                    _rep = st.session_state.get(f"apply_report_{project_id}")
                    if _rep:
                        st.markdown("---")
                        _ng = [r for r in _rep if str(r.get("結果", "")).startswith("⚠️")]
                        _ok = len(_rep) - len(_ng)
                        if _ng:
                            st.warning(f"⚠️ {len(_ng)}項目は書き込めていません（下の表の「結果」を確認してください）。"
                                       f"／書き込めたのは{_ok}項目です。")
                        else:
                            st.success(f"✅ {_ok}項目を書き込みました。下のプレビューで確認できます。")
                        st.dataframe(pd.DataFrame(_rep), use_container_width=True, hide_index=True)
                        if st.button("この結果を閉じる", key=f"apply_report_close_{project_id}"):
                            st.session_state.pop(f"apply_report_{project_id}", None)
                            st.rerun()

                    # 🔍 参照先のチェック：列ごとに違うBOXを見ていると、案件が混ざる（別案件に書き戻す事故）
                    st.markdown("---")
                    st.markdown("**🔍 数式の参照先チェック（案件の取り違え防止）**")
                    st.caption("最終シートの列が、全部おなじ●●BOXを見ているかを確認します。"
                               "列ごとに参照先が違うと、同じ行なのに『案件IDはA社／値はB社』のように"
                               "別の案件が混ざり、控えた番号を別案件に書いてしまいます。")
                    if st.button("🔍 参照先をチェックする", key=f"srccheck_{project_id}",
                                 use_container_width=True):
                        try:
                            _bad, _counts = _check_formula_sources(final_headers, final_formulas,
                                                                   box_ref_for_final or "")
                            if _counts:
                                st.caption("参照しているシートと、その列数：" +
                                           "／".join(f"{k}：{v}列" for k, v in sorted(_counts.items(),
                                                                                   key=lambda x: -x[1])))
                            if _bad:
                                st.error(f"⚠️ {len(_bad)}列が「{box_ref_for_final}」以外を参照しています。"
                                         "この列の数式を作り直してください（違う案件の値が入ります）。")
                                st.dataframe(pd.DataFrame(_bad), use_container_width=True, hide_index=True)
                            else:
                                st.success(f"✅ すべての列が「{box_ref_for_final}」を参照しています。取り違えの心配はありません。")
                        except Exception as e:
                            st.error(f"チェックできませんでした: {e}")

                    # ⬇️ 2行目の数式を下の行までコピー（全項目つくり終わったあとの仕上げ）
                    st.markdown("---")
                    st.markdown("**⬇️ 数式を下の行までコピー（仕上げ）**")
                    st.caption("数式は2行目にしか入っていません。全部つくり終わったら、このボタンで下の行まで"
                               "一気にコピーします（スプシのコピペと同じ動きなので、参照は自動で1行ずつずれます）。")
                    if final_tab_name:
                        _default_rows = 200
                        try:
                            if box_ref_for_final:
                                _n = _count_data_rows(gc, box_sheet_url, box_ref_for_final)
                                if _n >= 2:
                                    _default_rows = _n
                        except Exception:
                            pass
                        cd1, cd2 = st.columns([1, 2])
                        with cd1:
                            _last_row = st.number_input("何行目までコピーしますか？", min_value=3, max_value=5000,
                                                        value=int(max(_default_rows, 3)), step=10,
                                                        key=f"copydown_rows_{project_id}")
                        with cd2:
                            st.caption(f"「{final_tab_name}」の2行目の数式を、3行目〜{int(_last_row)}行目まで貼り付けます。"
                                       "⚠️ その範囲に手入力した値があると上書きされます（案件IDが空の行は空白のままです）。")
                        if st.button("⬇️ 2行目の数式を下までコピー", key=f"copydown_btn_{project_id}",
                                     use_container_width=True):
                            try:
                                with st.spinner("数式をコピーしています..."):
                                    _n_copied = _copy_formulas_down(gc, box_sheet_url, final_tab_name, int(_last_row))
                                if _n_copied > 0:
                                    st.success(f"3行目〜{int(_last_row)}行目（{_n_copied}行）にコピーしました！"
                                               "下のプレビューで確認できます。")
                                    st.cache_data.clear()
                                else:
                                    st.warning("コピーできる数式が2行目に見つかりませんでした。")
                            except Exception as e:
                                st.error(f"コピーに失敗しました: {e}")
                    else:
                        st.info("先に最終シートを選んでください。")

                    # 🔍 計算結果のプレビュー（BOXにテスト案件を入れた状態で、数式が正しく展開されているか確認）
                    st.markdown("---")
                    st.markdown("**🔍 計算結果をプレビュー（テスト確認）**")
                    st.caption("BOXに対象の案件（テスト用データ）を入れた状態で、各シートに数式で正しく値が"
                               "展開されているかを確認できます（計算後の値を読み取るだけで、何も書き換えません）。")
                    preview_choices = [t for t in [box_ref_for_final, final_tab_name] if t]
                    if preview_choices:
                        pv_tab = st.selectbox("どのシートを確認しますか？", preview_choices,
                                              key=f"preview_tab_{project_id}")
                        if st.button("🔍 先頭5行をプレビュー", key=f"preview_btn_{project_id}"):
                            try:
                                with st.spinner("計算結果を読み込んでいます..."):
                                    df_preview = _read_computed_preview(gc, box_sheet_url, pv_tab, n_rows=5)
                                if df_preview.empty:
                                    st.info("表示できるデータがありませんでした（BOXに対象案件が無い可能性があります）。")
                                else:
                                    st.caption("⚠️ 実データを入れている場合、この表には個人情報が表示されます。")
                                    st.dataframe(df_preview, use_container_width=True)
                            except Exception as e:
                                st.error(f"プレビューの取得に失敗しました: {e}")

    with _tab_steps:
        # 3. 増えてきた設定は折りたたみに収納してスッキリ！
        # 📌 よく使う設定（申請のルール）と、ほぼ触らない設定（こまかい設定）を分ける。
        #    以前は全部ひとつの折りたたみに入っていて閉じており、探せなかったため。
        with st.expander("🎯 このキャリアだけの特別ルール（完了の合図・控える値・AI相談）", expanded=True):
            # ✅ 申請完了の確認サイン（偽成功を防ぐ重要設定）
            success_text = st.text_input("✅ 申請完了の合図（完了画面に出る文言）",
                                         value=config["robot_config"].get("success_text", ""),
                                         placeholder="例：お申し込みを受け付けました")
            st.caption("📌 申請ボタンを押した後の「完了画面」に必ず出る文言を入れてください。"
                       "これを設定すると、本番で**申請が本当に通ったかを確認**し、失敗していたら自動でやり直せます（空のままだと確認できません）。")
            hold_completion = st.checkbox(
                "申請のあと、完了画面で一旦とまる（番号などを控える時間をとる）",
                value=config["robot_config"].get("hold_completion", True), key=f"holddone_{project_id}")
            st.caption("ONだと、1件ごとに完了画面を開いたまま止まり、「➡ 次の案件へ」を押すまで待ちます。"
                       "OFFにすると自動で次に進みます（控える値をすべて自動で読み取れた場合のみ。"
                       "読み取れなかったときは、OFFでも安全のため止まります）。")
            st.markdown("---")

            # 🔑 ログイン情報：値そのものはDBに入れず、暗号文だけを保存する。
            #    復号の鍵は、実行するPCの secrets.toml（または環境変数）にだけ置く。

            # 🤝 キャリア特有ルールの相談窓口：日本語で書くと、AIが既存の設定に翻訳して提案する。
            #    保存は必ず人の承認を挟む（AIが黙って設定を壊さないようにするため）。
            with st.expander("🤝 このキャリアだけのルールを相談する（AIが設定に翻訳します）"):
                st.caption("「申請したあとに出る番号を控えたい」「〇〇のときだけこの入力をしたい」など、"
                           "日本語で書いてください。AIが設定の形に翻訳して提案します。"
                           "**提案を見て「はい」を押すまで、設定は変わりません。**")
                _ask_key = f"rule_ask_{project_id}"
                st.text_area("どんなルールですか？", key=_ask_key, height=90,
                             placeholder="例：申請が終わると完了画面に回線登録番号が出るので、控えてスプシのBOXに書き戻したい")
                _prop_key = f"rule_prop_{project_id}"
                if st.button("🤖 相談する", key=f"rule_go_{project_id}"):
                    _q = str(st.session_state.get(_ask_key, "") or "").strip()
                    if not _q:
                        st.warning("相談内容を書いてください。")
                    elif not str(st.secrets.get("GEMINI_API_KEY", "")).strip():
                        st.error("⚠️ AIを使うには接続キー GEMINI_API_KEY の設定が必要です。")
                    else:
                        try:
                            # ロボットが読むシートの見出し（AIが実在する列名だけを使えるように渡す）
                            _hdrs = []
                            try:
                                _gc_r = _get_gspread_client()
                                _sc_r = config.get("spreadsheet", {}) or {}
                                if _gc_r and _sc_r.get("url") and _sc_r.get("tab_name"):
                                    _hdrs, _ = _read_headers_and_sample(_gc_r, _sc_r["url"], _sc_r["tab_name"])
                            except Exception:
                                _hdrs = []
                            with st.spinner("🤖 設定の形に翻訳しています..."):
                                st.session_state[_prop_key] = _consult_carrier_rule(
                                    _q, config["robot_config"].get("captures", []) or [],
                                    config.get("conditions", []) or [], _hdrs,
                                    config.get("product_type", ""))
                        except Exception as _e:
                            st.error(f"相談に失敗しました: {_e}")

                _prop = st.session_state.get(_prop_key)
                if _prop:
                    _kind = str(_prop.get("kind", "none"))
                    st.markdown("**🤖 AIからの提案**")
                    st.info(_prop.get("summary", "（説明なし）"))
                    if _prop.get("new_columns"):
                        st.warning("この設定には、スプシに次の列が必要です：" + " / ".join(_prop["new_columns"]))
                    if _kind == "none":
                        st.error("これは今の設定だけでは実現できません（仕組みの追加が必要です）。")
                        st.caption("↓ この内容をそのまま開発担当に渡してください。")
                        st.code(_prop.get("dev_request", "") or "（内容なし）", language="text")
                    elif _kind in ("captures", "conditions"):
                        _label = "申請後に控える値" if _kind == "captures" else "条件分岐ルール"
                        _new_items = _prop.get(_kind, []) or []
                        st.caption(f"追加・置き換えされる「{_label}」（この内容で保存されます）")
                        st.dataframe(pd.DataFrame(_new_items), use_container_width=True, hide_index=True)
                        ra1, ra2 = st.columns(2)
                        with ra1:
                            if st.button("✅ この内容で保存する", key=f"rule_ok_{project_id}",
                                         type="primary", use_container_width=True):
                                _cur = (config["robot_config"].get("captures", []) or []) if _kind == "captures" \
                                    else (config.get("conditions", []) or [])
                                # ↩ 元に戻せるように、変更前の設定を控えておく
                                config.setdefault("robot_config", {})["_undo"] = {"kind": _kind, "before": _cur}
                                _names = {str(i.get("name", "")) for i in _new_items}
                                _merged = [i for i in _cur if str(i.get("name", "")) not in _names] + _new_items
                                if _kind == "captures":
                                    config["robot_config"]["captures"] = _merged
                                else:
                                    config["conditions"] = _merged
                                proj_data["config_json"] = config
                                save_project(project_id, proj_data)
                                st.session_state.pop(_prop_key, None)
                                st.success(f"保存しました（{_label}：{len(_merged)}件）。"
                                           "うまくいかなければ「↩ 元に戻す」で戻せます。")
                                st.rerun()
                        with ra2:
                            if st.button("✖ 使わない", key=f"rule_ng_{project_id}", use_container_width=True):
                                st.session_state.pop(_prop_key, None)
                                st.rerun()

                _undo = config.get("robot_config", {}).get("_undo")
                if _undo:
                    if st.button("↩ 直前のAI提案を取り消して元に戻す", key=f"rule_undo_{project_id}"):
                        if _undo.get("kind") == "captures":
                            config["robot_config"]["captures"] = _undo.get("before", [])
                        else:
                            config["conditions"] = _undo.get("before", [])
                        config["robot_config"].pop("_undo", None)
                        proj_data["config_json"] = config
                        save_project(project_id, proj_data)
                        st.success("元に戻しました。")
                        st.rerun()

            # 📋 申請後に控える値（例：ドコモ光の回線登録番号）。キャリアごとに違うので設定で持つ。
            st.markdown("**📋 申請したあとに控える値（例：回線登録番号）**")
            st.caption("申請が終わると完了画面に番号が出るキャリア向けの設定です。1件ごとに番号を読み取って、"
                       "スプシの決めた列に書き戻します（案件IDで行を探すので、行がずれても取り違えません）。"
                       "番号が出ないキャリアは、この表を空のままにしてください。")
            _caps_cur = config["robot_config"].get("captures", []) or []
            _caps_df = pd.DataFrame(_caps_cur if _caps_cur else [],
                                    columns=["name", "hint", "tab", "col", "key_col", "pattern"])
            _caps_edited = st.data_editor(
                _caps_df, num_rows="dynamic", use_container_width=True, key=f"caps_ed_{project_id}",
                column_config={
                    "name": st.column_config.TextColumn("控える値の名前", help="例：回線登録番号"),
                    "hint": st.column_config.TextColumn("完了画面の手がかり文言",
                                                        help="この文言のすぐ後ろにある番号を読み取ります。例：回線登録番号"),
                    "tab": st.column_config.TextColumn("書き込むシート名", help="例：BOX（案件が全部入っているシート）"),
                    "col": st.column_config.TextColumn("書き込む列の見出し", help="例：回線登録番号"),
                    "key_col": st.column_config.TextColumn("案件を照合する列", help="例：案件ID（空なら案件ID）"),
                    "pattern": st.column_config.TextColumn("（上級）正規表現", help="手がかり文言で拾えないときだけ使用"),
                })
            st.caption("⚠️ 書き戻しには、サービスアカウントをそのスプシの**編集者**として共有しておく必要があります"
                       "（閲覧者のままだと書けません）。数式が入っている列には書けないので、素の列を用意してください。")
            if st.button("💾 控える値の設定を保存", key=f"caps_save_{project_id}"):
                _rows = []
                for _r in _caps_edited.fillna("").to_dict("records"):
                    _nm = str(_r.get("name", "") or "").strip()
                    if not _nm:
                        continue
                    _rows.append({"name": _nm,
                                  "hint": str(_r.get("hint", "") or "").strip(),
                                  "tab": str(_r.get("tab", "") or "").strip(),
                                  "col": str(_r.get("col", "") or _nm).strip(),
                                  "key_col": str(_r.get("key_col", "") or "案件ID").strip(),
                                  "pattern": str(_r.get("pattern", "") or "").strip()})
                config["robot_config"]["captures"] = _rows
                proj_data["config_json"] = config
                save_project(project_id, proj_data)
                st.success(f"{len(_rows)}件の設定を保存しました。")

            # 🩺 準備できているかの自動チェック＆自動セットアップ（担当者が列を自分で作らなくて済むように）
            _caps_saved = config["robot_config"].get("captures", []) or []
            if _caps_saved:
                _sheet_cfg_cap = config.get("spreadsheet", {}) or {}
                _final_tab_cap = str(_sheet_cfg_cap.get("tab_name", "") or "").strip()
                _url_cap = str(_sheet_cfg_cap.get("url", "") or "").strip()
                ck1, ck2 = st.columns(2)
                with ck1:
                    _do_check = st.button("🩺 準備できているか調べる", key=f"caps_check_{project_id}",
                                          use_container_width=True)
                with ck2:
                    _do_fix = st.button("🔧 足りない列を自動で作る", key=f"caps_fix_{project_id}",
                                        use_container_width=True,
                                        help="書き込み用の空列と、案件を見分けるための列を、アプリが用意します。")
                if _do_check or _do_fix:
                    _gc_cap = _get_gspread_client()
                    if not _gc_cap:
                        st.error("接続キー GOOGLE_SERVICE_ACCOUNT_JSON が未設定です（これが無いと書き戻せません）。")
                    elif not (_url_cap and _final_tab_cap):
                        st.error("スプレッドシートのURL／シート名が未設定です（基本情報を確認してください）。")
                    else:
                        for _cap in _caps_saved:
                            st.markdown(f"**「{_cap.get('name', '')}」の準備状況**")
                            try:
                                if _do_fix:
                                    for _m in _capture_setup_fix(_gc_cap, _url_cap, _final_tab_cap, _cap):
                                        (st.success if _m.startswith("✅") else st.warning)(_m)
                                _res, _miss = _capture_setup_check(_gc_cap, _url_cap, _final_tab_cap, _cap)
                                for _m in _res:
                                    (st.caption if _m.startswith("✅") else st.warning)(_m)
                                if not _miss:
                                    st.success("✅ 準備OK。この項目は申請後に自動で書き戻せます。")
                                elif not _do_fix:
                                    st.info("👆「🔧 足りない列を自動で作る」を押すと、アプリが用意します。")
                            except Exception as _e:
                                st.error(f"確認できませんでした: {_e}")
                        st.cache_data.clear()
        with st.expander("⚙️ こまかい設定（操作の速さ・Slack通知）"):
            st.caption("ふだんは触らなくて大丈夫な設定です。")
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
        with st.expander("🔀 条件分岐ルール（パターン）の作成", expanded=False):
            st.caption("「スプシのこの列がこういう値のときだけ、この手順を実行する」を作れます。"
                       "例：`CB有無` が `有り` のときだけ、金額の入力をする。"
                       "ここで作った名前を、下の手順書の『いつ』で選んで使います。"
                       "（入れる“値”を変えたいだけなら、ここではなくスプシの数式で行います）")

            # --- 既存ルールの一覧表示（確認・削除） ---
            existing_conditions = config.get("conditions", [])
            if existing_conditions:
                st.markdown("**📋 作成済みのルール**")
                for gi, grp in enumerate(existing_conditions):
                    with st.container(border=True):
                        cga, cgb = st.columns([6, 1])
                        with cga:
                            logic_label = "条件をすべて満たす" if str(grp.get("logic", "AND")).upper() == "AND" else "条件をどれか1つ満たす"
                            rules = grp.get("rules", [])
                            st.markdown(f"**🏷 {grp.get('name', '(無名)')}**　<small style='color:#0369A1;'>結合: {logic_label}</small>", unsafe_allow_html=True)
                            if rules:
                                for ri, r in enumerate(rules):
                                    op_label = next((k for k, v in OP_OPTIONS.items() if v == r.get("op")), r.get("op", ""))
                                    _rc1, _rc2 = st.columns([6, 1])
                                    with _rc1:
                                        st.markdown(f"　・「{r.get('col', '')}」が「{r.get('value', '')}」に **{op_label}**")
                                    with _rc2:
                                        # 条件1つだけを取り消せるようにする（今まではルールごと消すしかなかった）
                                        if st.button("削除", key=f"delcond_{gi}_{ri}", help="この条件だけを取り消します"):
                                            config["conditions"][gi]["rules"].pop(ri)
                                            proj_data["config_json"] = config
                                            save_project(project_id, proj_data)
                                            st.rerun()
                                if len(rules) > 1:
                                    # 条件が2つ以上あるときだけ、AND/ORを後から変えられるようにする
                                    _lg = st.selectbox("この2つ以上の条件の扱い", ["AND", "OR"],
                                                       index=0 if str(grp.get("logic", "AND")).upper() == "AND" else 1,
                                                       format_func=lambda x: "すべて満たす" if x == "AND" else "どれか1つ満たす",
                                                       key=f"logicfix_{gi}")
                                    if _lg != str(grp.get("logic", "AND")).upper():
                                        config["conditions"][gi]["logic"] = _lg
                                        proj_data["config_json"] = config
                                        save_project(project_id, proj_data)
                                        st.rerun()
                            else:
                                st.markdown("　<span style='color:#EF4444;'>※条件が未設定です。下の枠から条件を追加してください。</span>", unsafe_allow_html=True)
                        with cgb:
                            if st.button("🗑 削除", key=f"delrule_{gi}"):
                                config["conditions"].pop(gi)
                                proj_data["config_json"] = config
                                save_project(project_id, proj_data)
                                st.rerun()

            # 📋 使える列を、カラム設計と同じ「列記号つきの表」で見せる（手打ちで間違えないように）
            _cond_headers, _cond_sample = [], []
            try:
                _gc_c = _get_gspread_client()
                _sc_c = config.get("spreadsheet", {}) or {}
                if _gc_c and _sc_c.get("url") and _sc_c.get("tab_name"):
                    _cond_headers, _cond_sample = _read_headers_and_sample(
                        _gc_c, _sc_c["url"], _sc_c["tab_name"])
            except Exception:
                _cond_headers, _cond_sample = [], []
            if _cond_headers:
                with st.expander(f"📋 使える列の一覧（「{config.get('spreadsheet', {}).get('tab_name', '')}」の見出し）",
                                 expanded=False):
                    _render_columns_table(_cond_headers, caption="この見出し名を②で選びます（値の例つき）",
                                          values=_cond_sample)

            # --- 条件の追加 ---
            st.markdown("**＋ 条件を追加する**")
            st.caption("同じ名前でもう1つ条件を足すと、条件が2つのルールになります"
                       "（⑤で「すべて満たす」か「どれか1つ満たす」かを選べます）。")
            c_r1, c_r2, c_r3, c_r4, c_r5 = st.columns([2, 2, 1.6, 2, 1.4])
            with c_r1:
                c_name = st.text_input("① このルールの名前", placeholder="例：CBありのとき", key="rule_name",
                                       help="手順書の『いつ』で、この名前を選んで使います。分かりやすい名前を付けてください。")
            with c_r2:
                if _cond_headers:
                    # 実在する見出しから選ばせる（打ち間違い＝ルールが効かない事故を防ぐ）
                    _cc_opts = ["（列を選ぶ）"] + [h for h in _cond_headers if h] + ["✏️ 直接入力する"]
                    _cc_sel = st.selectbox("② 見る列", _cc_opts, key="rule_col_sel")
                    if _cc_sel == "✏️ 直接入力する":
                        c_col = st.text_input("列の見出し名", placeholder="例：CB有無", key="rule_col")
                    else:
                        c_col = "" if _cc_sel == "（列を選ぶ）" else _cc_sel
                        _i = _cond_headers.index(c_col) if c_col in _cond_headers else -1
                        if _i >= 0 and _i < len(_cond_sample) and _cond_sample[_i]:
                            st.caption(f"今の値の例：{_cond_sample[_i]}")
                else:
                    c_col = st.text_input("② 見る列（スプシの見出し名）", placeholder="例：CB有無", key="rule_col",
                                          help="ロボットが読むシートの見出し名を、そのまま入れてください。")
            with c_r3:
                c_op_label = st.selectbox("③ くらべ方", list(OP_OPTIONS.keys()), key="rule_op")
            with c_r4:
                c_val = st.text_input("④ くらべる値", placeholder="例：有り", key="rule_val",
                                      help="「空である」「空でない」を選んだときは、ここは空のままでOKです。")
            with c_r5:
                c_logic = st.selectbox("⑤ 条件が複数のとき", ["AND", "OR"], key="rule_logic",
                                       format_func=lambda x: "すべて満たす" if x == "AND" else "どれか1つ満たす",
                                       help="同じ名前のルールに条件を足したときの扱いです。1つだけなら気にしなくて大丈夫。")
            # 📖 いま作ろうとしている内容を、そのまま日本語の文にして見せる（設定ミスに気づけるように）
            if c_name or c_col:
                st.info(f"👉 「{c_col or '（列）'}」が「{c_val or '（値）'}」に **{c_op_label}** とき、"
                        f"『{c_name or '（ルール名）'}』として扱います。"
                        f"　手順書の『いつ』で「{c_name or '（ルール名）'}」を選んだ手順だけが実行されます。")
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

        # 🔑 ログイン情報は、手順書のすぐ上に置く。
        #    録画して手順を組んだ流れのまま `{秘密:名前}` を用意できるようにするため
        #    （以前は「特別ルール」の中にあり、手順書から遠かった）。
        robot_settings_ui.render_login_secrets(project_id, config, proj_data)
        robot_settings_ui.render_auth_code_settings(project_id, config, proj_data)

        # 5. 手順書の確認と編集
        with st.expander("📝 自動入力の手順書（こまかい修正用）", expanded=True):

            # 🔄 設計から手順書を作り直す（骨組み＝録画の位置・順番 ＋ 設計＝列連動/固定 から生成）
            #   骨組み未保存の既存ロボットでは、今の手順書を土台に使う。
            _skel_now = config.get("robot_config", {}).get("skeleton") or config.get("robot_config", {}).get("steps", [])
            _design_now = config.get("robot_config", {}).get("design", {})
            _rb1, _rb2 = st.columns([3, 2])
            with _rb1:
                st.caption("手で直した手順書を、設計どおりに戻します（＝手直し前に戻す）。※シートには触りません。")
            with _rb2:
                if st.button("🔄 設計から手順書を作り直す", key=f"regen_steps_{project_id}",
                             use_container_width=True, disabled=not _skel_now,
                             help="録画していない（骨組みが無い）場合は使えません。"):
                    _regen = _generate_steps_from_design(_skel_now, _design_now)
                    config["robot_config"]["steps"] = _regen
                    proj_data["config_json"] = config
                    save_project(project_id, proj_data)
                    st.success("設計から手順書を作り直しました。下の表で確認できます。")
                    st.rerun()
            if not _skel_now:
                st.caption("（骨組みが未保存です。一度「録画をやり直す」で作り直すと、以降この機能が使えます。）")

            # やさしい表示と上級者モードの切り替え
            easy_mode = st.toggle("やさしい表示（むずかしい列をかくす・おすすめ）", value=True, key=f"easy_{project_id}")

            if easy_mode:
                st.markdown("<div style='background:#F0F9FF; padding:16px; border-radius:12px; border:1px solid #BAE6FD; margin-bottom:16px; font-size:14px; line-height:1.8;'><b style='color:#0369A1;'>📋 この表の見かた・直し方</b><br>ロボットは上から順に、<b>録画で覚えた動き</b>を1つずつ実行します。<br>・<b>値</b>：<code>{列名}</code> が入っていれば、その列の<b>スプシのセルの中身</b>を入れます（プルダウン・ラジオも、<b>セルの文字と同じ選択肢</b>を自動で選びます）。<code>{}</code> が無ければ<b>録画したときの値のまま（固定）</b>です。<br>・<b>値の“列”を設定したい／連動をやめて録画の動きに戻したい</b>ときは <b>「基本・カラム設計」タブ</b>で（列を当てる＝連動／各項目の <b>「↩ 録画の動作に戻す」</b>で固定に戻る）。※表の「値」に直接 <code>{列名}</code> を打っても呪文が変わらず効きません。<br>・<b>いつ／操作</b>：プルダウンから選べます。<b>対象</b>は「画面のどの欄か」。<br><b>直したいとき</b>：表のセルを直接なおせます（要らない手順は行ごと削除もOK）。ただし<b>「対象」や右端の「最強の呪文（ai_code）」は録画が作る部分</b>なので、基本さわらなくて大丈夫。大きく変えたいときは上の<b>「🎬 録画をやり直す」</b>。<br><b>書き間違えても大丈夫</b>：上の<b>「🔄 設計から手順書を作り直す」</b>を押せば、<b>手で直す前（設計どおりの状態）に戻せます</b>。</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div style='background:#FFF7ED; padding:16px; border-radius:12px; border:1px solid #FED7AA; margin-bottom:16px; font-size:14px; line-height:1.6;'><b style='color:#C2410C;'>⚙️ 上級者モード：</b> 一番右の「最強の呪文（ai_code）」が表示されています。<br>自信がなければ<b>空っぽにしてOK</b>です。ロボットのAI自動検索が代わりに画面を探して入力します。</div>", unsafe_allow_html=True)

            # 「変換（値の加工）」列は非表示（加工はスプシの数式でやる方針）。既存データはデータ上は保持する。
            columns_order = ["順番", "いつ", "対象", "操作", "値", "空のとき", "変換", "ai_code"]

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

            # 「値」は最終シートの列を選ぶ形にする（{列名}）。何列目かは下の一覧で確認できる。
            final_cols_for_editor = []
            try:
                _gc_e = _get_gspread_client()
                _url_e = config.get("spreadsheet", {}).get("url", "")
                _tab_e = config.get("spreadsheet", {}).get("tab_name", "")
                if _gc_e and _url_e and _tab_e:
                    final_cols_for_editor, _ = _read_final_sheet(_gc_e, _url_e, _tab_e)
            except Exception:
                final_cols_for_editor = []

            conditions = config.get("conditions", [])
            condition_names = _ensure(["常に"] + [c["name"] for c in conditions] + [SUBMIT_WHEN_LABEL], df["いつ"])
            action_opts = _ensure(list(ACTION_OPTIONS), df["操作"])

            # 「値の加工(変換)」列は表示しない（スプシ数式へ移行）。ai_code はやさしい表示ではかくす。
            visible_cols = ["順番", "いつ", "対象", "操作", "値", "空のとき"]
            if not easy_mode:
                visible_cols = visible_cols + ["ai_code"]

            if final_cols_for_editor:
                _render_columns_table(final_cols_for_editor, caption="最終シートの列（「値」で選べる項目・何列目か）")
                st.caption("👆「値」の列では、ここにある列（＝最終シートの見出し）を選びます。加工はスプシの数式側で行うので、"
                           "手順書に「値の加工」列はありません。")
            st.caption("✋ **操作に「人の操作を待つ」を選ぶと**、そこでロボットが止まり、あなたがブラウザで"
                       "ログインや認証コード入力をしてから続けられます。"
                       "「値」に**目印の文字**（例：`ログイン`）を入れておくと、"
                       "その文字が画面に無いとき＝すでにログイン済みのときは、待たずに自動で飛ばします。")

            edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key=f"editor_{project_id}",
                                       column_order=visible_cols,
                                       column_config={
                                           "いつ": st.column_config.SelectboxColumn("いつ実行するか", options=condition_names),
                                           "対象": st.column_config.TextColumn("対象（画面の欄）"),
                                           "操作": st.column_config.SelectboxColumn("操作", options=action_opts,
                                                                                  help="この欄に何をする？（入力・クリックなど）。"
                                                                                       "カレンダーで日を選ぶ欄は「日付を入れる」"),
                                           "値": st.column_config.TextColumn("値（入れる／選ぶ列）※「人の操作を待つ」では目印の文字",
                                                                             help="最終シートの列を {列名} の形で入力。上の一覧で列名と何列目かを確認できます。"),
                                           "空のとき": st.column_config.SelectboxColumn(
                                               "セルが空のとき",
                                               options=["入力する", "飛ばす", "止める"],
                                               default="入力する",
                                               help="スプシのセルが空だったときにどうするか。"
                                                    "入力する＝空のまま入れる（既定）／"
                                                    "飛ばす＝この手順をしない（任意の項目向け）／"
                                                    "止める＝空なら申請せず中止する（必須の項目向け）"),
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
                        _submit_step = {
                            "順番": next_order, "いつ": SUBMIT_WHEN_LABEL, "対象": (submit_label or "申請する"),
                            "操作": "クリック", "値": "", "変換": "", "ai_code": "",
                        }
                        existing_steps.append(_submit_step)
                        config["robot_config"]["steps"] = existing_steps
                        # 🦴 送信ステップは骨組みにも追加（作り直しても残る）
                        _skel = config.get("robot_config", {}).get("skeleton", [])
                        if not any(_is_submit_when(s.get("いつ", s.get("condition", ""))) for s in _skel if s):
                            _skel.append(copy.deepcopy(_submit_step))
                            config["robot_config"]["skeleton"] = _skel
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
                config["robot_config"]["hold_completion"] = hold_completion
            
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

        # ==========================================
        # 📦 届け方（納品先）※非フォーム型のみ。実行の中身は後日実装（今日は設定の器だけ）
        # ==========================================
    with _tab_deliver:
        if config.get("needs_recording", True):
            st.caption("この種別（フォーム型）では『届け方・GASジョブ』は使いません。CSV・Excel型のときに使えます。")
        if not config.get("needs_recording", True):
            _delivery = config.get("delivery", {})
            _section_header("📦 届け方（できた最終シートをどこへ渡すか）")
            st.caption("最終シートを『丸ごと1枚』としてどこへ届けるかを決めます。"
                       "セル単位ではなくシートまるごとが単位です。"
                       "※実際に送る動き（実行）は後日実装予定。今は設定だけ保存できます。")
            _methods = {
                "none": "まだ決めない",
                "spreadsheet_paste": "別スプシへ丸ごとコピペ（サービスアカウントで直接書込み）",
                "download_submit": "ダウンロード→システムへ投入（Web操作＝録画が必要）",
                "email": "メールで送る（丸ごと添付）",
                "chat": "チャットで送る（Slack等）",
            }
            _keys = list(_methods.keys())
            _cur = _delivery.get("method", "none")
            _sel = st.radio("届け方", _keys, index=(_keys.index(_cur) if _cur in _keys else 0),
                            format_func=lambda k: _methods[k], key=f"delivery_method_{project_id}")
            _new_delivery = {"method": _sel}
            if _sel == "spreadsheet_paste":
                st.info("⚠️ 貼り付け先スプシに『サービスアカウントのメール（〜@…iam.gserviceaccount.com）』を"
                        "**編集者**で共有しておく必要があります。他社スプシ等で共有に入れられない場合は"
                        "メール/チャットへ切り替えてください。")
                _sp = _delivery.get("spreadsheet", {})
                _sp_url = st.text_input("貼り付け先スプシのURL", value=_sp.get("url", ""), key=f"delivery_sp_url_{project_id}")
                _sp_tab = st.text_input("貼り付け先タブ名", value=_sp.get("tab", ""), key=f"delivery_sp_tab_{project_id}")
                _sp_mode = st.radio("入れ方", ["append", "overwrite"],
                                    index=(0 if _sp.get("mode", "append") == "append" else 1),
                                    format_func=lambda m: "末尾に追記" if m == "append" else "上書き",
                                    key=f"delivery_sp_mode_{project_id}")
                _new_delivery["spreadsheet"] = {"url": _sp_url, "tab": _sp_tab, "mode": _sp_mode}
            elif _sel == "download_submit":
                st.info("この方式は『ファイルを選ぶ→アップロード→送信』のWeb操作を、後日“録画”で覚えさせます（中身は後日）。")
                _note = st.text_area("投入先システムのメモ（URL・手順のメモなど）",
                                     value=_delivery.get("submit_note", ""), key=f"delivery_submit_note_{project_id}")
                _new_delivery["submit_note"] = _note
            elif _sel == "email":
                _em = _delivery.get("email", {})
                _em_to = st.text_input("宛先メール", value=_em.get("to", ""), key=f"delivery_em_to_{project_id}")
                _em_sub = st.text_input("件名", value=_em.get("subject", ""), key=f"delivery_em_sub_{project_id}")
                _em_body = st.text_area("本文", value=_em.get("body", ""), key=f"delivery_em_body_{project_id}")
                st.caption("※送信は人の最終OKゲートを挟む予定。実際の送信は後日実装。")
                _new_delivery["email"] = {"to": _em_to, "subject": _em_sub, "body": _em_body}
            elif _sel == "chat":
                _cht = _delivery.get("chat", {})
                _ch_wh = st.text_input("チャットのWebhook URL（Slack等）", value=_cht.get("webhook", ""), key=f"delivery_ch_wh_{project_id}")
                _ch_msg = st.text_area("メッセージ本文", value=_cht.get("message", ""), key=f"delivery_ch_msg_{project_id}")
                _new_delivery["chat"] = {"webhook": _ch_wh, "message": _ch_msg}
            if st.button("💾 届け方の設定を保存", key=f"delivery_save_{project_id}"):
                config["delivery"] = _new_delivery
                proj_data["config_json"] = config
                save_project(project_id, proj_data)
                st.success("届け方の設定を保存しました。（実際に送る動きは後日実装）")

        # ==========================================
        # ⚙️ GASジョブ連携（実行タイミングの管理・成否チェック）※実行の中身は後日実装
        # ==========================================
        if not config.get("needs_recording", True):
            _gas = config.get("gas_job", {})
            _section_header("⚙️ GASジョブ連携（このキャリアのGASを動かす／状況を見る）")
            st.caption("スプシに置いたGAS（生成スクリプト）を、このアプリから実行タイミング管理する枠です。"
                       "GASを『ウェブアプリ』としてデプロイしてURLを貼ると、アプリが決めた時刻に叩けます。"
                       "※実際に叩く・成否を出す動きは後日実装。今は設定の保存だけ。")
            _enabled = st.checkbox("このGASジョブを有効にする（ON/OFF）", value=_gas.get("enabled", False), key=f"gas_enabled_{project_id}")
            _gas_url = st.text_input("GASウェブアプリのURL（doPost用）", value=_gas.get("webapp_url", ""), key=f"gas_url_{project_id}")
            _gas_token = st.text_input("合言葉トークン（URL悪用防止）", value=_gas.get("token", ""), key=f"gas_token_{project_id}")
            _sched = _gas.get("schedule", {})
            _gc1, _gc2 = st.columns([1, 2])
            with _gc1:
                _gas_time = st.text_input("実行時刻（例 08:00）", value=_sched.get("time", "08:00"), key=f"gas_time_{project_id}")
            with _gc2:
                _gas_days = st.multiselect("実行する曜日", ["月", "火", "水", "木", "金", "土", "日"],
                                           default=_sched.get("days", ["月", "火", "水", "木", "金"]), key=f"gas_days_{project_id}")
            st.markdown("---")
            _rc1, _rc2 = st.columns(2)
            with _rc1:
                st.button("▶ 今すぐ実行（準備中）", key=f"gas_run_{project_id}", disabled=True, use_container_width=True)
            with _rc2:
                st.button("🔄 前回の成否を見る（準備中）", key=f"gas_status_{project_id}", disabled=True, use_container_width=True)
            _last = _gas.get("last_result")
            st.caption(f"前回結果：{_last}" if _last else "前回結果：まだありません（実行・エントリー成否チェックは後日実装）。")
            if st.button("💾 GASジョブ設定を保存", key=f"gas_save_{project_id}"):
                config["gas_job"] = {
                    "enabled": _enabled, "webapp_url": _gas_url, "token": _gas_token,
                    "schedule": {"time": _gas_time, "days": _gas_days},
                    "last_result": _gas.get("last_result"),
                }
                proj_data["config_json"] = config
                save_project(project_id, proj_data)
                st.success("GASジョブ設定を保存しました。（実行・成否表示は後日実装）")

    with _tab_test:
        # 6. 最後にテスト
        with st.expander("🧪 さいごに、お試し実行してみましょう", expanded=True):

            # 🩺 完成前チェック（登録前の健康診断）。最終シートの列も読めれば{列名}の存在も確認する。
            _final_headers_for_check = None
            try:
                _gc_check = _get_gspread_client()
                _tab_for_check = config.get("spreadsheet", {}).get("tab_name", "")
                if _gc_check and _tab_for_check and config.get("spreadsheet", {}).get("url"):
                    _final_headers_for_check, _ = _read_final_sheet(_gc_check, config["spreadsheet"]["url"], _tab_for_check)
            except Exception:
                _final_headers_for_check = None
            health = _robot_health(config, final_headers=_final_headers_for_check)
            problems = [c for c in health if not c[0]]
            if problems:
                st.warning(f"⚠️ 完成前に確認したい項目が {len(problems)} 件あります：")
                _render_health_checklist(problems, compact=False)
            else:
                st.success("✅ 完成前チェックはすべてOKです。お試し実行で動きを確認して完成させましょう。")

            st.caption("お試しでは、ロボットが入力する様子を確認できます。"
                       "安全のため『送信（申請）ステップ』は押しません（本番のクラウドLIVE実行でだけ押されます）。")
            ct1, ct2 = st.columns(2)
            with ct1:
                # 📝 実行ログをファイルに残す。ブラウザが閉じると何が起きたか分からなくなるため、
                #    「どの手順で・なぜ止まったか」を後から画面で読めるようにする。
                _log_path = os.path.join(tempfile.gettempdir(),
                                         f"enkan_try_{re.sub(r'[^0-9A-Za-z_-]', '_', str(project_id))}.log")
                if st.button("▶ お試し実行（申請ボタンの手前まで）", use_container_width=True):
                    st.info("ロボットが動き出します。開いたブラウザを見守ってくださいね。")
                    try:
                        _lf = open(_log_path, "w", encoding="utf-8", errors="replace")
                        subprocess.Popen([sys.executable, "robot.py", project_id],
                                         stdout=_lf, stderr=subprocess.STDOUT,
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                    except Exception as _e:
                        st.error(f"起動に失敗しました: {_e}")
            with ct2:
                if st.button("🔎 お試しの結果ログを見る", use_container_width=True,
                             key=f"trylog_{project_id}"):
                    st.session_state[f"show_trylog_{project_id}"] = True
            if st.session_state.get(f"show_trylog_{project_id}"):
                if os.path.exists(_log_path):
                    try:
                        with open(_log_path, encoding="utf-8", errors="replace") as _f:
                            _log = _f.read()
                    except Exception as _e:
                        _log = f"（ログを読めませんでした: {_e}）"
                    _bad = [ln for ln in _log.split("\n") if "❌" in ln or "🛑" in ln]
                    if _bad:
                        st.error("止まった原因（この行を見てください）:\n\n" + "\n\n".join(_bad[:5]))
                    st.text_area("実行ログ（新しい順に読むなら下から）", value=_log, height=280,
                                 key=f"trylogbox_{project_id}")
                    st.caption("※実行中は途中までしか出ません。もう一度このボタンを押すと最新まで読み直します。")
                else:
                    st.info("まだログがありません。先に「▶ お試し実行」を押してください。")

            ct3, _ = st.columns(2)
            with ct3:
                if st.button("✓ テストOK！ロボットを完成させる", type="primary", use_container_width=True):
                    if problems:
                        st.error("未設定の項目が残っています。上の⚠️を確認してから完成させてください。"
                                 "（それでも完成にする場合は、もう一度押してください）")
                        if st.session_state.get(f"force_complete_{project_id}"):
                            proj_data["is_active"] = True
                            save_project(project_id, proj_data)
                            st.success("おめでとうございます！ロボットを「全自動稼働」にしました。")
                            time.sleep(1); st.session_state.view = 'dashboard'; st.rerun()
                        st.session_state[f"force_complete_{project_id}"] = True
                    else:
                        st.success("おめでとうございます！これで全自動化ロボットが完成しました。")
                        proj_data["is_active"] = True
                        save_project(project_id, proj_data)
                    time.sleep(1); st.session_state.view = 'dashboard'; st.rerun()

        # 🖐 実際のエントリー（申請）は、ホームの「▶ エントリー開始」から行います。
        #    ここは設定の動作確認用（お試し実行）だけを置く＝設定画面と運用画面を分けるため。
        st.info("🖐 実際のエントリー（本番の申請）は、**ホームの「▶ エントリー開始」** から行います。ここは設定どおり動くかを確かめる「お試し」の場所です。")