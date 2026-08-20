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

# 進捗取り込み用ロボットの値ルール。スプシの列は使わないので、値は空にしておく。
VALUE_RULE_INTAKE = (
    "この手順書はサイトからファイルを落とすためのものです。ログインIDやパスワードなど"
    "毎回同じ値は、値を空文字にしてください（あとで人が {秘密:名前} に差し替えます）。"
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


def build_prompt(recorded_code: str, value_rule: str = None) -> str:
    """録画コードから、AIに渡すプロンプトを組み立てる。"""
    return (PROMPT
            .replace("__VALUE_RULE__", value_rule or VALUE_RULE_DEFAULT)
            .replace("__RECORDED__", str(recorded_code or "")))


def parse_steps(text: str):
    """AIの返答（JSON）を手順のリストにする。"""
    data = json.loads(text)
    return data if isinstance(data, list) else [data]
