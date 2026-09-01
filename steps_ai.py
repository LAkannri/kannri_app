"""
🤖 録画コード → 手順書（JSON）への変換

エントリー業務のロボットでも、進捗取り込みのロボットでも同じ変換を使うため、
プロンプトをここに1本化する（同じものを2か所に書くと、直し漏れが起きるため）。

方針：
  ・録画のセレクタは1文字も変えない（＝録画どおりに動かすための肝）
  ・変えてよいのは「入力した値の文字列」だけ
"""

import json
import re

_PW_HINT = re.compile(r"pass|pwd|\bpw\b|secret|パスワード|暗証", re.IGNORECASE)

PROMPT = r"""【役割】あなたはPlaywrightの録画コードを、手順表(JSON)へ変換する変換器です。セレクタを推測で作ってはいけません。

【変換対象】ユーザーの操作行だけを手順にします：fill / click / check / set_checked / select_option / set_input_files。
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
- 操作：fill→「文字を入力」、click→「クリック」、select_option→「選択」、check／set_checked→「チェック」、set_input_files→「ファイルをアップロード」。
- 対象：その要素の name=（無ければ表示テキスト）を、人が読める日本語で。
- 値：__VALUE_RULE__

【出力】説明文は一切書かず、次の形のJSON配列のみを出力：
[ {"順番": 1, "いつ": "常に", "操作": "文字を入力", "対象": "お名前", "値": "{お名前}", "ai_code": "page.get_by_role(\"textbox\", name=\"お名前\").fill(\"{お名前}\")"} ]

【録画コード】
__RECORDED__
"""

# 進捗取り込み用ロボットの値ルール。
# スプシの列は使わないので、録画で打った値をそのまま残す。
# （空にすると、実行時に何も入力されず「メールアドレスを入力してください」等で止まる。
#   隠したい値は、あとから画面で {秘密:名前} に差し替えられる。パスワードは既に伏せ済み。）
VALUE_RULE_INTAKE = (
    "この手順書はサイトからファイルを落とすためのものです。"
    "録画で実際に入力した文字を、値にそのまま入れてください（{列名} には置き換えない）。"
    "すでに {秘密:パスワード} のように書かれている値は、そのまま残すこと。"
)

# SMS一括送信（プッシュプロ）用ロボットの値ルール。
# ログインや画面の操作は録画どおりの値でよいが、渡すCSVだけは毎日ちがう。
# そこでファイルを選ぶ手順の値だけを {アップロードファイル} にして、実行時に差し替える。
VALUE_RULE_SMS = (
    "この手順書は、CSVを取り込んでSMSを一括送信するためのものです。"
    "録画で実際に入力した文字は、値にそのまま入れてください（{列名} には置き換えない）。"
    "ただし set_input_files でファイルを選んだ手順だけは、値を {アップロードファイル} にしてください"
    "（渡すCSVは毎日ちがうため）。"
    "すでに {秘密:パスワード} のように書かれている値は、そのまま残すこと。"
)

# SFコネクタの更新用ロボットの値ルール。
# 実際の操作は「拡張機能 → コネクタ → リフレッシュ → シート名を選ぶ → 手動リフレッシュ」で、
# 変わるのは『選ぶシート名』だけ。そこだけ差し替え印にして、あとは録画どおりに動かす。
VALUE_RULE_REFRESH = (
    "この手順書は、Googleスプレッドシートの拡張機能（Salesforceコネクタ）で、"
    "シートを1つ選んで手動リフレッシュするためのものです。"
    "録画で入力した文字は、値にそのまま入れてください（{列名} には置き換えない）。"
    "ただし『更新するシートの名前を選んだ操作』だけは、"
    "その手順の『対象』と『値』の両方を {更新するシート} にしてください"
    "（実行時にシート名が入れ替わり、登録した枚数ぶん繰り返すため）。"
    "すでに {秘密:パスワード} のように書かれている値は、そのまま残すこと。"
)

VALUE_RULE_DEFAULT = (
    "その項目を表す短い日本語名を {列名} の形で入れてください（例：{お名前}、{電話番号}）。"
    "録画で入力した実際のテスト値（例：自動化太郎）はそのまま書かないこと。"
)


def redact_passwords(recorded_code: str):
    """録画コードのうち、パスワード欄に打った文字を伏せる。
    ログインは本物のパスワードでしか通らないため録画には実物が入るが、
    そのままAIに送ると手順書（＝データベース）に平文で残るので、ここで置き換える。
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


# 🆔 ログインID（メールアドレス／ユーザー名）らしい欄の目印。
#    パスワードほど機械的には見分けられないので、確実そうな語だけに絞る。
#    「id」だけだと data-testid など無関係な指定まで拾ってしまうため入れない。
_ID_HINT = re.compile(
    r"mail|メール|アドレス|ユーザー|username|user_?name|account|アカウント|"
    r"ログインid|loginid|login_?id",
    re.IGNORECASE)


def redact_logins(recorded_code: str):
    """録画コードのうち、ログインID欄に打った文字を伏せる。

    パスワードと違い、IDは業務上そのまま残しても困らないことが多いので既定では伏せない。
    ただしGoogleアカウントのように「IDだけでも他人に渡したくない」ものがあるので、
    画面から選べるようにしてある。伏せた分は {秘密:ログインID} として登録して使う。
    戻り値：(置き換え後のコード, 置き換えた数)
    """
    lines, n = [], 0
    for line in str(recorded_code or "").split("\n"):
        if ".fill(" in line and _ID_HINT.search(line.split(".fill(")[0]):
            new_line = re.sub(r'\.fill\(\s*(["\']).*?\1\s*\)', '.fill("{秘密:ログインID}")',
                              line, count=1)
            if new_line != line:
                n += 1
            lines.append(new_line)
        else:
            lines.append(line)
    return "\n".join(lines), n


def build_prompt(recorded_code: str, value_rule: str = None) -> str:
    """録画コードから、AIに渡すプロンプトを組み立てる。"""
    return (PROMPT
            .replace("__VALUE_RULE__", value_rule or VALUE_RULE_DEFAULT)
            .replace("__RECORDED__", str(recorded_code or "")))


def parse_steps(text: str):
    """AIの返答（JSON）を手順のリストにする。"""
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


# ==========================================
# 🧹 録画に入る「入力枠を選ぶだけのクリック」を落とす
# ==========================================
# 録画すると各入力欄の前に不要なクリックが入る。そのままだと手順が倍に見えて読みにくく、
# 画面が変わると空振りの原因にもなるため、手順書を作る段階で落とす。
_CLICK_OPS = {"クリック", "click"}
_FILL_OPS = {"文字を入力", "fill"}
# これらの語を含むクリックは“必要な操作”として絶対に消さない（ボタン・送信・次へ 等）
_KEEP_CLICK_WORDS = ["次", "送信", "確認", "申請", "申込", "申し込", "確定", "進む", "戻", "追加", "検索",
                     "登録", "同意", "選択", "ボタン", "submit", "next", "confirm", "button", "search",
                     "add", "register", "agree", "ダウンロード", "download", "csv", "出力"]
_SUBMIT_WHEN = {"送信", "申請", "送信する", "申請する", "送信（本番のみ）", "申請（本番のみ）",
                "送信(本番のみ)", "申請(本番のみ)", "送信時", "申請時", "最後に送信"}


def _is_field_focus_click(step, next_step) -> bool:
    """step が『入力枠を選ぶだけの余分なクリック』か。すぐ次が『文字を入力』のときだけ真。"""
    op = str(step.get("操作", step.get("action", "")) or "").strip()
    nop = str(next_step.get("操作", next_step.get("action", "")) or "").strip()
    if op not in _CLICK_OPS or nop not in _FILL_OPS:
        return False
    if str(step.get("いつ", step.get("condition", "")) or "").strip() in _SUBMIT_WHEN:
        return False
    hay = (str(step.get("対象", step.get("target_description", "")) or "") + " "
           + str(step.get("ai_code", step.get("最強の呪文", "")) or "")).lower()
    if any(w.lower() in hay for w in _KEEP_CLICK_WORDS):
        return False
    return True


def strip_redundant_field_clicks(steps):
    """余分なクリックを取り除き、順番を振り直して返す。"""
    if not steps:
        return steps
    ordered = sorted([s for s in steps if s], key=lambda x: x.get("順番", x.get("order", 999)))
    kept = []
    for i, s in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        if nxt is not None and _is_field_focus_click(s, nxt):
            continue
        kept.append(s)
    for i, s in enumerate(kept, 1):
        if "順番" in s:
            s["順番"] = i
        if "order" in s:
            s["order"] = i
    return kept


# ==========================================
# 📅 その日しか通じない指定を、意図の書き方に直す
# ==========================================
# 録画は「そのとき押した場所」を覚える。ファイル名に日付が入っていると、
# 翌日には通じない＝録画したままでは必ず失敗する。
# 作った時点で直しておけば、担当者が気づかないまま古いファイルを
# 取り込んでしまうことがない。
_FILE_EXT = re.compile(r"\.(csv|xlsx?|zip|tsv|txt|pdf)\b", re.IGNORECASE)
_DATE_IN_NAME = re.compile(r"20\d{2}[-/]?\d{2}[-/]?\d{2}")


def looks_daily_changing(text: str) -> bool:
    """『日付入りのファイル名』を指しているか（その日しか通じない指定）。"""
    s = str(text or "")
    return bool(_FILE_EXT.search(s)) and bool(_DATE_IN_NAME.search(s))


def fix_daily_changing_targets(steps):
    """録画が覚えた「日付入りファイル名」を、『最新のファイル』に直す。

    戻り値：(直した手順, 直した内容のリスト)
    """
    fixed = []
    for s in (steps or []):
        if not s:
            continue
        op = str(s.get("操作", s.get("action", "")) or "").strip()
        target = str(s.get("対象", s.get("target_description", "")) or "")
        code = str(s.get("ai_code", s.get("最強の呪文", "")) or "")
        if op not in ("クリック", "click"):
            continue
        if looks_daily_changing(target) or looks_daily_changing(code):
            fixed.append(target or code[:60])
            if "対象" in s or "target_description" not in s:
                s["対象"] = "最新のファイル"
            else:
                s["target_description"] = "最新のファイル"
    return steps, fixed
