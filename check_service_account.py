import tomllib
import json
import sys

with open(".streamlit/secrets.toml", "rb") as f:
    secrets = tomllib.load(f)

sa_json = secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
print("① トークンが設定されているか:", bool(sa_json))

if not sa_json:
    print("→ secrets.toml に GOOGLE_SERVICE_ACCOUNT_JSON がまだ入っていません。")
    sys.exit(0)

try:
    info = json.loads(sa_json)
    print("② JSONとして正しく読めるか: OK")
    print("   client_email:", info.get("client_email", "(見つかりません)"))
    print("   project_id:", info.get("project_id", "(見つかりません)"))
except Exception as e:
    print("② JSONの読み込みに失敗しました")
    print("   例外の種類:", type(e).__name__)
    print("   詳細:", str(e))
    sys.exit(1)

from google.oauth2.service_account import Credentials
import gspread

creds = Credentials.from_service_account_info(
    info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)

SHEET_URL = "https://docs.google.com/spreadsheets/d/11scVOJ-q-FjDvWwWEXb1nLJCoIw6Q2huY1585M9pY2A/edit"
try:
    sh = gc.open_by_url(SHEET_URL)
    print("③ スプシを開けました！タブ一覧:")
    for ws in sh.worksheets():
        print("   -", ws.title)
except Exception as e:
    import traceback
    print("③ スプシを開くのに失敗しました")
    print("   例外の種類:", type(e).__name__)
    print("   詳細:", repr(e))
    print("---- 詳しい発生箇所 ----")
    traceback.print_exc()
