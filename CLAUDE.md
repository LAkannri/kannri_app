# CLAUDE.md

このファイルは、このリポジトリで作業する Claude Code 向けのガイドです。

## ⚠️ 最重要ルール：Git のコミット／プッシュ名義

このリポジトリへの参加は **協力者（collaborator）** としての立場です。

- コミット・プッシュは必ず **Claude** 名義で行うこと。
- オーナー個人（`kura-nakakura`）のアカウント名義では **絶対にコミット・プッシュしない**。
- 現在の git 設定（厳守）：
  - `user.name`  = `Claude`
  - `user.email` = `noreply@anthropic.com`
- 作業ブランチは `claude/...`（例：`claude/dazzling-shannon-02iziu`）を使用し、
  許可なく `main` へ直接プッシュしない。
- プッシュ後は PR を作成（無ければドラフトで作成）する。
- モデル識別子（`claude-opus-...` 等）をコミットメッセージ・PR・コード・コメントに含めない。

## 🎯 このアプリの目的（エンカンAI）

事務作業の **キャリア申請（電気・ガス・ネット）を完全自動化** する RPA システム。

SFA（Google スプレッドシート）の案件データを元に、各キャリアの Web 申請フォームへ
Playwright で自動入力する。担当者が一度だけ「手本」を録画すれば、AI（Gemini）が
日本語の手順書を自動生成し、以後はロボットが代理入力する、という思想で作られている。

対象ユーザーは非エンジニアの録画担当者。そのため UI と文言は徹底的に「やさしく」
設計されている（絵文字・丸みフォント・かみ砕いた説明）。

## 🏗️ アーキテクチャ

| 役割 | 技術 |
|---|---|
| 顔（UI） | Streamlit（マルチページ） |
| 脳（DB） | Supabase（`merchants` テーブル）+ Google Spreadsheet（SFA） |
| 手足（自動操作） | Playwright（Chromium。ローカルは表示／クラウドは `headless`） |
| AI（手順生成） | Google Gemini（`gemini-2.5-flash`） |

### データモデル

ロボットの設定はすべて Supabase の `merchants` テーブルに JSON で保存される。

- `id` / `name`：ロボット名（= 主キー）
- `is_active`：稼働中かどうか
- `connector_type`：`"playwright"`
- `config_json`：
  - `product_type`：商材種別（ネット／電気／ガス／その他）
  - `spreadsheet`：SFA の `url` / `tab_name` / `trigger_col`(=ステータス) / `trigger_val`(=未エントリー)
  - `robot_config`：`target_url` / `steps`（手順書）/ `stealth` / `captcha`
  - `notifications`：`slack_id` / `slack_msg`
  - `conditions`：条件分岐ルール（パターン）一覧 ← 下記「ルールエンジン」参照

### 手順書（steps）の形

各ステップは日本語キー（旧）と英語キー（新）の両対応。`robot.py` が両方を吸収する。

```json
{ "順番": 1, "いつ": "常に", "操作": "文字を入力", "対象": "お名前",
  "値": "{顧客_氏名}", "変換": "", "ai_code": "..." }
```

- `操作` は `文字を入力→fill` / `クリック→click` / `選択→select` / `チェック→check` にマップ。
- `値` や `ai_code` 内の `{項目名}` は実行時に顧客データ（スプシ行）で動的置換される
  （例：`{電話番号}`）。`090` などが数値化しないよう純粋な文字列置換で行う。
- `変換`（任意）：置換後の値に加工を適用（コード不要）。`robot.py:apply_transform` が処理。
  対応：`ハイフン除去` / `数字のみ` / `市外局番` / `市内局番` / `加入者番号` /
  `郵便番号_上3桁` / `郵便番号_下4桁`。

### 分岐ルールエンジン（`conditions`）

ハードコードを廃し、設定駆動で評価する（`robot.py:evaluate_condition`）。
各ステップの `いつ` にルール名を指定すると、その条件成立時のみ実行される。

```json
{ "name": "未成年ルート", "logic": "AND",
  "rules": [ { "col": "年齢", "op": "lt", "value": "20" } ] }
```

- `op`：`eq`/`ne`/`contains`/`not_contains`/`empty`/`not_empty`/`gt`/`gte`/`lt`/`lte`/`in`
- `logic`：`AND`（全条件）/ `OR`（いずれか）。同名ルールに条件を追加すると複数条件になる。
- `常に`・空 → 必ず実行。**未定義のルール名 → 安全側でスキップ（False）**（事故防止）。
- 「条件で違う値を入れる」は、同じ `対象` の手順を複数行つくり、各行の `いつ` に
  別ルールを指定して実現する（値マッピング専用スキーマは設けていない）。

## 📁 ファイル構成

```
kannri_app/
├── app.py                    # Streamlit トップページ（サマリーのモック）
├── robot.py                  # Playwright 自動操作エンジン（CLI 単体実行可）
├── pages/
│   ├── 1_📊_全状況進捗確認.py        # 準備中（プレースホルダ）
│   ├── 2_📝_エントリー業務自動化.py  # ★中核：ロボット作成ウィザード＋司令室
│   ├── 3_🚀_開通進捗反映自動化.py    # 準備中
│   ├── 4_🛑_変更キャンセル自動化.py  # 準備中
│   └── 5_⚙️_その他設定.py           # 準備中
├── manual.html               # 利用者向けセットアップガイド
├── requirements.txt          # 依存パッケージ
├── start.bat / start.command # Windows / Mac 用ランチャー（自動セットアップ付）
├── README.md
├── .gitignore                # secrets.toml などを除外
└── .streamlit/
    └── secrets.toml.example  # 接続キーのテンプレート
```

`pages/2_📝_エントリー業務自動化.py` がアプリの中核。4つのビュー（`st.session_state.view`）で動く：
1. `dashboard`：ロボット一覧
2. `step1_basic`：基本情報（名前・SFA URL）
3. `step2_record`：Playwright codegen で録画 → Gemini で手順書生成
4. `project_room`：司令室（手順編集・条件分岐・テスト実行）

## 🔑 接続キー（secrets）

`.streamlit/secrets.toml` に以下を設定（`.gitignore` 済み・コミット禁止）：

```toml
SUPABASE_URL    = "https://xxxxx.supabase.co"
SUPABASE_KEY    = "eyJhbGc..."  # anon key
GEMINI_API_KEY  = "AIzaSy..."
```

> 機密情報は絶対にコミットしないこと。`secrets.toml` / `.setup_done` は除外済み。

## 🚀 開発・実行

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # 値を記入
streamlit run app.py
```

ロボット単体テスト：`python robot.py "<ロボット名/プロジェクトID>"`（モック顧客で実行）

## ☁️ クラウド実行（GitHub Actions / 担当者PC非依存）

`.github/workflows/run-robots.yml` が毎日（UTC 23:00＝JST 08:00）＋手動で起動し、
`python robot.py --all` を実行する。担当者のPCを開かなくてもクラウドで動く。

- **鍵の読み込み**（`robot.py:load_secrets`）：環境変数 `SUPABASE_URL`/`SUPABASE_KEY`/`GEMINI_API_KEY`
  があればそれを優先（CI 向け）、無ければ `.streamlit/secrets.toml`（ローカル向け）。
- **GitHub Secrets**：リポジトリの Settings → Secrets and variables → Actions に上記3つを登録する。
- **headless 切替**（`robot.py:is_headless`）：`ENKAN_HEADLESS=1/0` を明示。未指定なら
  `CI` 環境変数があるとき自動で headless（ワークフローは `CI: "true"` を渡す）。
- **実行モード**：`--all`＝稼働中（`is_active=True`）の全ロボットを処理（`run_all_active`）。
  引数にロボット名→そのロボットをモック顧客で単体実行。
- **二重起動防止**：`concurrency` で同時実行を抑止（二重申請の防止）。
- **証跡**：失敗・中止・CAPTCHA 検出時に `artifacts/` へスクショ保存（`_save_screenshot`）。
  ワークフローは `artifacts/` を成果物としてアップロード（14日保持）。
- **ボット検知の安全停止**（`_looks_blocked`）：CAPTCHA 等の壁を検出したら送信せず中止。

## 📄 SFAスプレッドシート連携（リンク共有・読み取り専用）

`fetch_pending_rows`（`robot.py`）が、スプシを**CSVとして読み取り**未エントリー行を取得する。

- **接続方式**：認証なし。スプシの共有を「**リンクを知っている全員（閲覧者）**」にする。
  gviz エンドポイント `…/gviz/tq?tqx=out:csv&sheet=<タブ名>`（`_csv_export_url`）で取得。
  非公開だとログインHTMLが返るため、`_parse_pending` が分かりやすいエラーにする。
- **列のマッピング**：スプシの**ヘッダ名がそのまま** `{項目名}` に対応（列『電話番号』→ `{電話番号}`）。
- **絞り込み**：`trigger_col`（既定『ステータス』）が `trigger_val`（既定『未エントリー』）の行のみ。
- **二重申請の防止**：読み取り専用で**スプシへ書き戻せない**ため、処理済み行のハッシュ
  （`_row_key`、ステータス列は除外）を **Supabase の `config_json._processed_keys`** に保存し、
  再実行時はスキップする。直近 5000 件に制限。
- **本番ゲート**（`ENKAN_ALLOW_LIVE`）：既定は**ドライラン**（対象を表示するだけ／実ブラウザ操作なし）。
  `1` のときのみ実申請。ワークフローでは手動実行 `live=true` のときだけ ON、
  **スケジュール実行は常にドライラン**（事故防止）。
- ⚠️ **書き戻し不可の制約**：read-only のためスプシの「ステータス」列は自動更新されない
  （担当者の目視では未処理のまま見える）。二重申請は `_processed_keys` で防ぐ。
  ステータス書き戻しが必要ならサービスアカウント方式への切替が前提。

## 🛣️ ロードマップ（README より）

- [x] GitHub Actions による毎日自動実行（クラウド稼働）
- [x] SFA スプシからの自動読み込み（リンク共有・読み取り専用、`fetch_pending_rows`）
- [ ] ステータス書き戻し（read-only のため未対応。要・サービスアカウント方式）
- [ ] `exec()` の構造化アクション置き換え（セキュリティ向上）
- [ ] 進捗ダッシュボード／開通進捗反映／変更キャンセル管理＋Slack 通知／設定モード

## ⚠️ 既知の注意点

- `robot.py` は `ai_code` を `exec()` で実行している。AI 生成コードを動的実行するため、
  信頼できる入力前提。将来的に構造化アクションへ置き換える方針（ロードマップ参照）。
- 手順書の編集で空セルが `NaN`/`None` になる問題に対し、保存時に空文字へ正規化する処理がある。
- ブラウザは、ローカル（有人）では表示・10秒待機で目視でき、クラウド（CI）では headless で動く。
