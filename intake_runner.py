"""
🔄 取り込み〜貼り付けの実行

Driveに貯まった進捗ファイル（GASがメールから保存したもの／サイトからダウンロードしたもの）を
読んで、進捗スプレッドシートの元データシートに貼り替える。

安全のための決まりごと：
  ・貼り付け先の見出しは残す（数式や列の並びを壊さないため）。何行が見出しかは設定できる。
  ・貼る前に見出しを突き合わせ、違っていたら貼らずに中止する（別キャリアのファイル対策）。
  ・0件のファイルでは貼らない（全消しの事故を防ぐ）。
  ・貼る前のデータは、必要なら退避シートに残せる。
"""

import io
import json
import os
import re
import time

import intake_reader


def drive_client(sa_json: str):
    """Driveを読むためのクライアント（読み取り専用）。"""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds)


def find_carrier_folder(drive, root_folder_id: str, carrier: str):
    """取り込みフォルダの下から、そのキャリアのフォルダを探す。"""
    q = (f"'{root_folder_id}' in parents and trashed=false "
         "and mimeType='application/vnd.google-apps.folder'")
    for f in drive.files().list(q=q, fields="files(id,name)", pageSize=100).execute().get("files", []):
        if str(f["name"]).strip() == str(carrier).strip():
            return f["id"]
    return None


def latest_file(drive, folder_id: str):
    """フォルダの中で、いちばん新しいファイルを1つ返す。"""
    q = f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    files = drive.files().list(q=q, fields="files(id,name,modifiedTime,size)",
                               orderBy="modifiedTime desc", pageSize=5).execute().get("files", [])
    return files[0] if files else None


def download_bytes(drive, file_id: str) -> bytes:
    """Driveのファイルを読み込む。"""
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    req = drive.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def paste_to_sheet(gc, sheet_id: str, tab: str, rows, keep_rows: int = 1, backup: bool = False):
    """見出しを残したまま、その下のデータを入れ替える。

    keep_rows: 貼り付け先の見出しが何行あるか（その下から貼る）。
    backup: 貼る前の内容を退避シートに残すか。
    """
    sh = gc.open_by_key(str(sheet_id).strip())
    ws = sh.worksheet(str(tab).strip())
    keep = max(int(keep_rows or 1), 0)

    if backup:
        try:
            old = ws.get_all_values()
            if old:
                name = f"{tab}_backup_{time.strftime('%m%d_%H%M')}"[:99]
                bw = sh.add_worksheet(title=name, rows=len(old) + 5, cols=max(len(old[0]), 5))
                bw.update(range_name="A1", values=old, value_input_option="USER_ENTERED")
        except Exception:
            pass   # 退避に失敗しても、本体の処理は続ける（貼り替え自体は安全に行える）

    # 見出しより下を消してから貼る（消さないと、前回の方が行数が多いとき残ってしまう）
    last = ws.row_count
    if last > keep:
        ws.batch_clear([f"A{keep + 1}:ZZ{last}"])
    if rows:
        need = keep + len(rows)
        if need > ws.row_count:
            ws.add_rows(need - ws.row_count)
        ws.update(range_name=f"A{keep + 1}", values=rows, value_input_option="USER_ENTERED")
    return len(rows)


def local_latest_file(folder: str):
    """ローカルフォルダの中で、いちばん新しいファイルを返す（サイトからダウンロードした分）。"""
    import glob
    files = [f for f in glob.glob(os.path.join(folder, "*")) if os.path.isfile(f)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def run_download_robot(project_name: str, save_dir: str, timeout_sec: int = 600):
    """録画したロボットを動かして、サイトからファイルをダウンロードする（このPCで実行）。
    戻り値：(成功したか, ログの最後のほう)"""
    import subprocess
    import sys
    os.makedirs(save_dir, exist_ok=True)
    log_path = os.path.join(save_dir, "intake.log")
    with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
        p = subprocess.run([sys.executable, "robot.py", "--intake", project_name, save_dir],
                           stdout=lf, stderr=subprocess.STDOUT, timeout=timeout_sec,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        with open(log_path, encoding="utf-8", errors="replace") as lf:
            log = lf.read()[-2000:]
    except Exception:
        log = ""
    return p.returncode == 0, log


def run_one(gc, drive, root_folder_id: str, cfg_row: dict, secrets_map: dict = None,
            local_file: tuple = None):
    """1キャリア分の取り込み〜貼り付け。結果は画面に出すための辞書で返す。"""
    carrier = str(cfg_row.get("キャリア名", "")).strip()
    out = {"キャリア": carrier, "件数": 0, "結果": "", "ファイル": ""}

    sheet_id = str(cfg_row.get("貼り付け先スプシID", "")).strip()
    tab = str(cfg_row.get("元データシート名", "")).strip()
    if not (sheet_id and tab):
        out["結果"] = "⚠️ 貼り付け先が未設定"
        return out

    # 📄 ファイルの入手元は3通り。ここで (ファイル名, 中身) にそろえてから、以降は共通処理。
    if local_file:
        fname, data = local_file
        out["ファイル"] = fname
    else:
        folder = find_carrier_folder(drive, root_folder_id, carrier) if drive else None
        if not folder:
            out["結果"] = f"⚠️ 取り込みフォルダに「{carrier}」がありません"
            return out
        f = latest_file(drive, folder)
        if not f:
            out["結果"] = "⚠️ 新しいファイルがありません"
            return out
        out["ファイル"] = f["name"]
        fname = f["name"]
        try:
            data = download_bytes(drive, f["id"])
        except Exception as e:
            out["結果"] = f"❌ ファイルを取得できません: {str(e)[:120]}"
            return out

    pw_name = str(cfg_row.get("解錠パスワードの名前", "")).strip()
    password = str((secrets_map or {}).get(pw_name, "")) if pw_name else ""
    if pw_name and not password:
        out["結果"] = f"⚠️ 解錠パスワード「{pw_name}」が登録されていません"
        return out

    try:
        skip = int(str(cfg_row.get("捨てる先頭行数", "1") or "1").strip() or 1)
    except Exception:
        skip = 1
    try:
        headers, rows = intake_reader.read_table(data, fname, password=password, skip_rows=skip)
    except Exception as e:
        out["結果"] = f"❌ ファイルを読めません: {str(e)[:150]}"
        return out
    if not rows:
        out["結果"] = "⚠️ 中身が0件のため貼り付けを中止しました"
        return out

    try:
        keep = int(str(cfg_row.get("貼り付け先の見出し行数", "1") or "1").strip() or 1)
    except Exception:
        keep = 1

    # 貼り付け先の見出し（最後の見出し行）と突き合わせる
    try:
        ws = gc.open_by_key(sheet_id).worksheet(tab)
        sheet_headers = ws.row_values(keep)
    except Exception as e:
        out["結果"] = f"❌ 貼り付け先を開けません: {str(e)[:120]}"
        return out

    same, only_file, only_sheet = intake_reader.compare_headers(headers, sheet_headers)
    if not same:
        out["結果"] = ("❌ 見出しが違うので貼り付けを中止しました"
                       + (f"／ファイルだけ: {', '.join(map(str, only_file[:5]))}" if only_file else "")
                       + (f"／シートだけ: {', '.join(map(str, only_sheet[:5]))}" if only_sheet else ""))
        return out

    try:
        n = paste_to_sheet(gc, sheet_id, tab, rows, keep_rows=keep, backup=True)
    except Exception as e:
        out["結果"] = f"❌ 貼り付けに失敗: {str(e)[:150]}"
        return out

    out["件数"] = n
    out["結果"] = f"✅ {n}件を貼り付けました"
    return out
