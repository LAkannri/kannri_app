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

### ① アプリを持ってくる

**すでに動いているPCから渡す場合**（GitHubのログイン不要）

1. 動いているアプリで **⚙️ その他設定 → 📦 ZIPを作る → ⬇️ ダウンロード**
2. できた `ENKAN_APP.zip` を相手に渡す（USB・DMなど）
3. 好きな場所（デスクトップなど）に展開
4. **`update.bat` をダブルクリック**

**GitHubから直接落とす場合**

1. 緑の **Code** ボタン → **Download ZIP**
2. 好きな場所に展開（フォルダ名は `kannri_app-main` のままでOK。
   気になるなら変えても構いません。名前は動作に関係しません）
3. **`update.bat` をダブルクリック**

`update.bat` が、足りないものを自動でそろえます。

- Gitが入っていなければ、**その場でインストール**します（`winget` を使用）。
  自動で入れられない環境では、ダウンロードページを開きます
- そのあと、そのフォルダを**Gitの管理下に切り替え**ます

これで以後の更新は `update.bat` のダブルクリックだけで済みます。

> **自分で clone したい場合**（Gitが入っている前提）
>
> ```
> cd "$env:USERPROFILE\Desktop"
> git clone https://github.com/LAkannri/kannri_app.git ENKAN_APP
> ```
>
> 初回はGitHubのログインを求められます。会社のアカウントでログインしてください。

### ② （参考）clone で持ってくる場合

PowerShell を開いて、置きたい場所へ移動します（例：デスクトップ）。

```
cd "$env:USERPROFILE\Desktop"
```

続けて、落とします。

```
git clone https://github.com/LAkannri/kannri_app.git ENKAN_APP
```

初回はGitHubのログインを求められます。ブラウザが開くので、
会社のGitHubアカウントでログインしてください。
デスクトップに `ENKAN_APP` フォルダができます。

> **Gitを使いたくない場合**：GitHubの緑の **Code** ボタン → **Download ZIP** →
> 好きな場所に展開。ただし更新のたびに落とし直しになります。

> ⚠️ すでにZIPで展開したフォルダがある場合は、**別の場所**に clone して、
> `secrets.toml` だけコピーしてください。同じ場所には上書きできません。

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
**Pythonが入っていなければ、ここで自動的にインストールします。**
「このアプリがデバイスに変更を加えることを許可しますか？」と聞かれたら
**［はい］**を押してください（押さないと入りません）。

2回目以降も、足りない部品があれば自動で入れ直します。

### ⑤ 最初の1回だけ

- **ブラウザのログインはやり直し**になります（`.enkan_profile` はGitに入らないため）。
  ロボットを動かすと、ログイン手順から実行され、認証コードも使います。
- サイトからダウンロードするロボットは、**このPCで**動かす必要があります
  （クラウド版からは実行できません）。

---

## 🔄 アプリを最新にする（2回目以降）

アプリは頻繁に直しています。**朝いちばんに1回**やっておくと確実です。

### `update.bat` をダブルクリックするだけ

数秒で終わります。中では `git pull` が動き、部品が増えていれば自動で入れ直します。
Gitが入っていないPCでは、Gitのインストールから面倒を見ます。

PowerShell から手で行う場合：

```
cd "$env:USERPROFILE\Desktop\ENKAN_APP"
git pull
```

> Streamlit を開いたままでも更新できますが、**更新後は一度閉じて
> `start.bat` で開き直して**ください（動いているアプリは古いままのため）。

### ZIPで落とした場合

GitHubの緑の **Code** → **Download ZIP** で落とし直し、フォルダを上書きします。

⚠️ `.streamlit\secrets.toml` は上書きされません（ZIPに入っていないため）。
**フォルダごと消してから展開しないでください**（接続キーが消えます）。

### 更新しても消えないもの

| | |
|---|---|
| 接続キー（`.streamlit\secrets.toml`） | Gitの管理外なので、そのまま残ります |
| キャリアの設定・ロボットの手順 | Supabase／スプレッドシートにあるので、PCとは無関係 |
| ブラウザのログイン状態（`.enkan_profile`） | そのPCに残ります |

---

## 👨‍💼 管理者向け：運用情報

### 接続キーの配布

新しい録画担当者に共有するもの：

| 何を | どうやって |
|---|---|
| GitHubリポジトリのURL | Slack で共有 |
| `secrets.toml` そのもの | USB や 自分宛のDM など。**作り直さずコピー**する |
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
