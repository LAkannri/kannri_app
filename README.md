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

## 🛣️ 今後のロードマップ

- [ ] GitHub Actions による毎日自動実行（クラウド稼働）
- [ ] SFAスプシからの自動読み込み + ステータス書き戻し
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
