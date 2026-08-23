# 🤖 エンカンAI

事務作業のキャリア申請を完全自動化するシステム。

> 📌 **利用者の方へ：** セットアップ方法は [`manual.html`](./manual.html) を見てください。
> （ファイルをダブルクリックするとブラウザで開きます）

> 🌐 **クラウド版アプリ（招待制）**：https://kannriapp-du7gfq3kfsajsadwfruw9f.streamlit.app/
> 設定の確認・変更はここからできます（携帯からも可）。
> ただし**エントリー実行・お試し実行・選択肢を調べる**はブラウザを開く機能のため、
> 自分のPCで起動したアプリからのみ使えます。
> 接続キーの設定は `share.streamlit.io` → アプリの ⋮ → Settings → Secrets。

---

## 📋 これは何？

SFA（スプレッドシート）の案件データを元に、各キャリア（電気・ガス・ネット）の申請フォームを自動入力するロボットです。Playwright で動きます。

- **顔（UI）**: Streamlit
- **脳（DB）**: Supabase + Google Spreadsheet
- **手足（自動操作）**: Playwright
- **AI（手順生成）**: Gemini

---

## 🚀 利用者向け：セットアップ手順

詳細は [`manual.html`](./manual.html) をブラウザで開いてください。

### ① Python を入れる（まだ入っていないPCだけ）

まず、入っているか確かめます。**PowerShell**（または コマンドプロンプト）を開いて：

```
python --version
```

`Python 3.11.x` のようにバージョンが出れば、この手順は不要です。
「認識されていません」と出たら、入っていないので入れてください。

1. [python.org/downloads](https://www.python.org/downloads/) を開き、**Download Python** を押す
2. ダウンロードした `python-3.x.x-amd64.exe` を実行
3. ⚠️ **最初の画面の「Add python.exe to PATH」に必ずチェック**を入れる
   （ここを忘れると、起動ファイルが「Pythonが見つかりません」で止まります）
4. 「Install Now」を押して、終わったらPCを一度ログインし直す
5. もう一度 `python --version` で確認する

> ※ Microsoft Store 版の Python でも動きますが、うまくいかないときは
> python.org 版を入れ直してください。

### ② アプリを持ってくる

- **Gitがあるなら**：`git clone`（以後 `update.bat` で最新にできます）
- **無いなら**：GitHubの緑の **Code** ボタン → **Download ZIP** → 好きな場所に展開

### ③ 接続キーを置く

`.streamlit/secrets.toml` は **Gitに入りません**（機密のため）。
すでに動いているPCの `.streamlit\secrets.toml` を、**そのままコピー**してください。

⚠️ 中の `ENKAN_SECRET_KEY` が違うと、登録済みのID・パスワードを開けません。
新しく作らず、必ず既存のファイルをコピーすること。

（まっさらに作る場合のみ、`secrets.toml.example` をコピーして `secrets.toml` にリネームし、値を記入）

### ④ 起動する

- Windows: `start.bat` をダブルクリック
- Mac: `start.command` をダブルクリック

初回は必要な部品を自動でインストールします（5〜10分）。
2回目以降も、足りない部品があれば自動で入れ直します。

### ⑤ 最初の1回だけ

- **ブラウザのログインはやり直し**になります（`.enkan_profile` はGitに入らないため）。
  ロボットを動かすと、ログイン手順から実行され、認証コードも使います。
- サイトからダウンロードするロボットは、**このPCで**動かす必要があります
  （クラウド版からは実行できません）。

---

## 👨‍💼 管理者向け：運用情報

### 接続キーの配布

新しい録画担当者に共有するもの：

| 何を | どうやって |
|---|---|
| GitHubリポジトリのURL | Slack で共有 |
| `secrets.toml` の中身（3つのキー） | Slack DM などセキュアな経路で共有 |
| `manual.html` | リポジトリ内に同梱済 |

### secrets.toml に入れるキー

```toml
# 必須
SUPABASE_URL    = "https://xxxxx.supabase.co"
SUPABASE_KEY    = "eyJhbGc..."          # anon key
GEMINI_API_KEY  = "AIzaSy..."           # 手順書の自動生成に使う

# 進捗反映を使うなら必須
GOOGLE_SERVICE_ACCOUNT_JSON = "{...}"   # スプレッドシート／Driveを読む
ENKAN_SECRET_KEY = "..."                # ID・パスワードの暗号化用
SF_USERNAME = "..."                     # Salesforceへの投入
SF_PASSWORD = "..."
SF_SECURITY_TOKEN = "..."
```

> ⚠️ **`ENKAN_SECRET_KEY` はPCごとに作り直さないこと。**
> これが違うと、登録済みのID・パスワードを開けなくなります。
> 別のPCで使うときは、必ず既存の `secrets.toml` をコピーしてください。

### ファイル構成

```
enkan-ai/
├── app.py                          # Streamlit メインアプリ
├── robot.py                        # Playwright 自動操作エンジン
├── pages/                          # サブページ
├── requirements.txt                # Python依存パッケージ
├── start.bat                       # Windows用ランチャー（自動セットアップ付）
├── start.command                   # Mac用ランチャー（自動セットアップ付）
├── update.bat                      # 最新に更新する（git pull ＋ 部品の追加）
├── manual.html                     # 利用者向けセットアップガイド
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
- [ ] 進捗反映モード
- [ ] 変更キャンセル管理モード + Slack通知
- [ ] 設定モード（URL保管庫・機密保管庫・マニュアル）

---

## 📞 困ったときは

- 利用者向けトラブル → `manual.html` の【Q&A】を参照
- システムのバグ・要望 → 管理者に連絡 or GitHub Issues

---

🌈 **快適な自動化ライフを！**
