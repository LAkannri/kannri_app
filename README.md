# 🤖 エンカンAI

事務作業のキャリア申請を完全自動化するシステム。

> 📌 **利用者の方へ：** セットアップ方法は [`はじめにお読みください.html`](./はじめにお読みください.html) を見てください。
> （ファイルをダブルクリックするとブラウザで開きます）

---

## 📋 これは何？

SFA（スプレッドシート）の案件データを元に、各キャリア（電気・ガス・ネット）の申請フォームを自動入力するロボットです。Playwright で動きます。

- **顔（UI）**: Streamlit
- **脳（DB）**: Supabase + Google Spreadsheet
- **手足（自動操作）**: Playwright
- **AI（手順生成）**: Gemini

---

## 🚀 利用者向け：セットアップ手順

3ステップだけです。詳細は [`はじめにお読みください.html`](./はじめにお読みください.html) をブラウザで開いてください。

1. **Pythonをインストール**（[python.org](https://www.python.org/downloads/) から、PATHにチェック）
2. **このリポジトリをZIPでダウンロード**（緑の Code ボタン → Download ZIP）
3. **`.streamlit/secrets.toml.example` を `secrets.toml` にリネームして、接続キーを記入**
4. **起動ファイルをダブルクリック**
   - Windows: `起動_Windows.bat`
   - Mac: `起動_Mac.command`

初回は自動で必要な部品をインストールします（5〜10分かかります）。

---

## 👨‍💼 管理者向け：運用情報

### 接続キーの配布

新しい録画担当者に共有するもの：

| 何を | どうやって |
|---|---|
| GitHubリポジトリのURL | Slack で共有 |
| `secrets.toml` の中身（3つのキー） | Slack DM などセキュアな経路で共有 |
| `はじめにお読みください.html` | リポジトリ内に同梱済 |

### secrets.toml に入れるキー

```toml
SUPABASE_URL    = "https://xxxxx.supabase.co"
SUPABASE_KEY    = "eyJhbGc..."  # anon key
GEMINI_API_KEY  = "AIzaSy..."
```

### ファイル構成

```
enkan-ai/
├── app.py                          # Streamlit メインアプリ
├── robot.py                        # Playwright 自動操作エンジン
├── pages/                          # サブページ
├── requirements.txt                # Python依存パッケージ
├── 起動_Windows.bat                # Windows用ランチャー（自動セットアップ付）
├── 起動_Mac.command                # Mac用ランチャー（自動セットアップ付）
├── はじめにお読みください.html     # 利用者向けセットアップガイド
├── README.md                       # このファイル
├── .gitignore                      # secrets.toml などを除外
└── .streamlit/
    └── secrets.toml.example        # 接続キーのテンプレート
```

### 開発環境のセットアップ（管理者・開発者向け）

```bash
# リポジトリをクローン
git clone https://github.com/<org>/enkan-ai.git
cd enkan-ai

# 仮想環境を作成（推奨）
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 依存パッケージをインストール
pip install -r requirements.txt
playwright install chromium

# secrets.toml を設置
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# エディタで開いて接続キーを記入

# 起動
streamlit run app.py
```

---

## ☁️ 管理者向け：クラウドで毎日自動実行する（GitHub Actions）

担当者のPCを開かなくても、クラウド上でロボットを毎日自動実行できます（`.github/workflows/run-robots.yml`）。

1. リポジトリの **Settings → Secrets and variables → Actions** で、以下3つの Secret を登録：
   - `SUPABASE_URL` / `SUPABASE_KEY` / `GEMINI_API_KEY`
2. SFAスプレッドシートの共有を **「リンクを知っている全員（閲覧者）」** にしておく
   （ロボットは認証なしの読み取りで未エントリー行を取得します）。
3. これで毎日 **JST 08:00**（UTC 23:00）に `python robot.py --all` が実行されます。
   **Actions タブ → 「自動申請ロボット（毎日実行）」→ Run workflow** で手動実行も可能。
4. 失敗・中止・CAPTCHA 検出時のスクリーンショットは、実行結果の **Artifacts（`robot-artifacts`）** から確認できます。

> 🛡 **安全のしくみ（重要）**
> - **スケジュール実行は必ず「ドライラン」**（対象件数を表示するだけで、実際の申請操作はしません）。
>   本当に申請させるときは、**手動実行で `live` を ON** にしてください。
> - スプシは読み取り専用のため**ステータスの自動書き戻しはできません**。代わりに処理済みの案件を
>   システム側で記録し、**同じ案件の二重申請を防止**します。
> - ローカルでは画面を見ながら、クラウドでは自動で headless で動きます（`ENKAN_HEADLESS=1/0` で切替可）。

---

## 🛣️ 今後のロードマップ

- [x] GitHub Actions による毎日自動実行（クラウド稼働）
- [x] SFAスプシからの自動読み込み（リンク共有・読み取り専用）
- [ ] ステータス書き戻し（読み取り専用のため未対応。サービスアカウント方式が前提）
- [ ] `exec()` の構造化アクション置き換え（セキュリティ向上）
- [ ] 全進捗確認ダッシュボード（売上目標管理・稼働管理）
- [ ] 開通進捗反映モード
- [ ] 変更キャンセル管理モード + Slack通知
- [ ] 設定モード（URL保管庫・機密保管庫・マニュアル）

---

## 📞 困ったときは

- 利用者向けトラブル → `はじめにお読みください.html` の【Q&A】を参照
- システムのバグ・要望 → 管理者に連絡 or GitHub Issues

---

🌈 **快適な自動化ライフを！**
