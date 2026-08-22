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


def service_account_email(sa_json: str) -> str:
    """ロボットがGoogleを使うときのアドレス。フォルダの共有先として画面に出す。"""
    try:
        return str(json.loads(sa_json).get("client_email", "")).strip()
    except Exception:
        return ""


def drive_client_rw(sa_json: str):
    """Driveに書き込めるクライアント（サイトから落としたファイルを保管するのに使う）。"""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)


def ensure_carrier_folder(drive, root_folder_id: str, carrier: str) -> str:
    """取り込みフォルダの下の、そのキャリアのフォルダ。無ければ作る。"""
    found = find_carrier_folder(drive, root_folder_id, carrier)
    if found:
        return found
    meta = {"name": str(carrier).strip(),
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [root_folder_id]}
    return drive.files().create(body=meta, fields="id",
                                supportsAllDrives=True).execute()["id"]


def archive_to_drive(sa_json: str, root_folder_id: str, carrier: str, local_path: str):
    """サイトから落としたファイルを、メール添付のときと同じ取り込みフォルダにも入れる。

    メールで届く分と、サイトから落とす分が別々の場所にあると、
    「あの日の進捗ファイルは？」と探すときに二か所を見ることになるため、
    どちらの方式でも同じフォルダに残す。
    戻り値：入れた場所を表すメッセージ。
    """
    from googleapiclient.http import MediaFileUpload
    drive = drive_client_rw(sa_json)
    try:
        folder = ensure_carrier_folder(drive, root_folder_id, carrier)
    except Exception as e:
        # 404＝ロボット（サービスアカウント）からは、そのフォルダが見えていない。
        # 「見つからない」とだけ言われても直しようがないので、直し方まで書く。
        if "404" in str(e) or "not found" in str(e).lower():
            raise RuntimeError(
                "保管フォルダを開けませんでした。Googleドライブでそのフォルダを、"
                f"ロボットのアドレス（{service_account_email(sa_json)}）に"
                "「編集者」で共有してください。") from e
        raise
    name = os.path.basename(local_path)
    media = MediaFileUpload(local_path, resumable=False)
    drive.files().create(body={"name": name, "parents": [folder]},
                         media_body=media, fields="id",
                         supportsAllDrives=True).execute()
    return f"取り込みフォルダの「{carrier}」に保管しました：{name}"


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


def call_gas(url: str, token: str, action: str = "", timeout: int = 300):
    """GAS（ウェブアプリ）を今すぐ実行する。

    時間ごとの自動実行に頼らず、必要になったタイミングでアプリから呼ぶための入口。
    action は "intake"（メール添付の取り込み）／"code"（認証コードの取り出し）。
    戻り値：(成功したか, メッセージ)
    """
    import urllib.parse
    import urllib.request
    url = str(url or "").strip()
    if not url:
        return False, "GASのURLが設定されていません"
    q = urllib.parse.urlencode({"token": token or "", "action": action or ""})
    try:
        # ウェブアプリはリダイレクトされるので、そのまま追う
        with urllib.request.urlopen(f"{url}?{q}", timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"呼び出せませんでした: {str(e)[:150]}"
    try:
        data = json.loads(body)
    except Exception:
        return False, f"返事を読めませんでした: {body[:150]}"
    if data.get("error"):
        return False, str(data["error"])[:200]
    return True, data


# 📁 サイトから落としたファイルの、一時的な置き場所。
#    アプリのフォルダ（デスクトップ＝OneDrive配下）に置くと、実在の顧客情報が
#    クラウドに同期され、容量も食う。だから同期されない作業用フォルダに置き、
#    保管はDrive側（取り込みフォルダ）に任せる。ここには直近の数件だけ残す。
INTAKE_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.environ.get("TMP") or os.path.expanduser("~"),
    "ENKAN_APP", "取り込み作業")
RECORD_NAME = "_last_download.json"
KEEP_LOCAL_FILES = 3     # 1ロボットあたり、手元に残す最新ファイル数


def intake_dir(robot_name: str) -> str:
    """そのロボットが落としたファイルを置くフォルダ（無ければ作る）。"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(robot_name or "").strip()) or "ロボット"
    path = os.path.join(INTAKE_ROOT, safe)
    os.makedirs(path, exist_ok=True)
    return path


def cleanup_local(folder: str, keep: int = KEEP_LOCAL_FILES):
    """古いファイルを消して、手元には最新の数件だけ残す（PCの容量を食わないように）。"""
    import glob
    files = [f for f in glob.glob(os.path.join(folder, "*"))
             if os.path.isfile(f) and not f.lower().endswith(".log")
             and os.path.basename(f) != RECORD_NAME]
    for f in sorted(files, key=os.path.getmtime, reverse=True)[max(keep, 1):]:
        try:
            os.remove(f)
        except Exception:
            pass


def last_download(folder: str):
    """『さっきの実行で落ちてきたファイル』を返す。無ければ None。

    ファイル名は毎回変わり、前回の分も同じフォルダに残るため、
    “いちばん新しいファイル”では古い分を掴む事故がある。
    そこでロボットが書き残した記録（_last_download.json）を正とする。
    """
    try:
        with open(os.path.join(folder, RECORD_NAME), encoding="utf-8") as f:
            files = json.load(f).get("ファイル") or []
    except Exception:
        return None
    for p in reversed(files):
        if os.path.isfile(p):
            return p
    return None


def local_latest_file(folder: str):
    """ローカルフォルダの中で、いちばん新しいファイルを返す（記録が無い時の保険）。"""
    import glob
    # 実行ログや記録ファイルは中身ではないので、数に入れない
    files = [f for f in glob.glob(os.path.join(folder, "*"))
             if os.path.isfile(f) and not f.lower().endswith(".log")
             and os.path.basename(f) != RECORD_NAME]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def run_download_robot(project_name: str, save_dir: str = None, timeout_sec: int = 600):
    """録画したロボットを動かして、サイトからファイルをダウンロードする（このPCで実行）。
    戻り値：(成功したか, ログの最後のほう, 今回落ちてきたファイルのパス or None)"""
    import subprocess
    import sys
    save_dir = save_dir or intake_dir(project_name)
    os.makedirs(save_dir, exist_ok=True)
    # 前回の記録は消しておく（失敗したのに前回のファイルを掴まないため）
    try:
        os.remove(os.path.join(save_dir, RECORD_NAME))
    except Exception:
        pass
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
    got = last_download(save_dir)
    cleanup_local(save_dir)     # 古い分は消す（保管はDrive側）
    return p.returncode == 0, log, got


HISTORY_TAB = "取り込み履歴"
HISTORY_HEADERS = ["キャリア名", "前回のファイル", "反映日時", "件数"]


def read_history(gc, settings_url: str) -> dict:
    """前回どのファイルを反映したかを読む。
    新しいメールが来ていないのに、前回と同じファイルで貼り直すのを防ぐために使う。"""
    try:
        ws = gc.open_by_url(settings_url).worksheet(HISTORY_TAB)
        return {str(r[0]).strip(): str(r[1]).strip()
                for r in ws.get_all_values()[1:] if r and str(r[0]).strip()}
    except Exception:
        return {}


def write_history(gc, settings_url: str, done: list):
    """反映できたキャリアの記録を残す（キャリア名・ファイル名・日時・件数）。"""
    if not done:
        return
    sh = gc.open_by_url(settings_url)
    try:
        ws = sh.worksheet(HISTORY_TAB)
        rows = ws.get_all_values()[1:]
    except Exception:
        ws = sh.add_worksheet(title=HISTORY_TAB, rows=200, cols=4)
        rows = []
    names = {d["キャリア"] for d in done}
    kept = [r for r in rows if r and str(r[0]).strip() not in names]
    now = time.strftime("%Y/%m/%d %H:%M")
    for d in done:
        kept.append([d["キャリア"], d.get("ファイル", ""), now, str(d.get("件数", 0))])
    ws.clear()
    ws.update(range_name="A1", values=[HISTORY_HEADERS] + kept,
              value_input_option="USER_ENTERED")


def run_one(gc, drive, root_folder_id: str, cfg_row: dict, secrets_map: dict = None,
            local_file: tuple = None, last_file: str = "", dry_run: bool = False):
    """1キャリア分の取り込み〜貼り付け。結果は画面に出すための辞書で返す。

    dry_run=True のときは、貼り付けだけ行わない（読み取り・見出し照合までは同じ）。
    段階ごとのテストでは「最後まで繋がるか」が分からないので、
    実データを書き換えずに通しで確かめるために使う。
    """
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
        # 新しいメールが来ていないと、前回と同じファイルが「いちばん新しい」ままになる。
        # そのまま貼り直すと「今日のデータが来た」と誤解するので、飛ばして知らせる。
        if last_file and str(last_file).strip() == str(fname).strip():
            out["結果"] = "⏭ 新しいファイルが届いていません（前回と同じファイル）"
            return out
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

    # 📄「ファイルの見出しは何行目まで？」＝ 1 なら1行目が見出し、2 なら2行目までが見出し。
    #    見出しの最後の行を列名として使うので、その上の行だけ読み飛ばす。
    try:
        _raw_head = str(cfg_row.get("ファイルの見出し行数",
                                    cfg_row.get("捨てる先頭行数", "1")) or "1").strip()
        skip = max(1, int(_raw_head or 1)) - 1
    except Exception:
        skip = 0
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
        # 📄 見出しの行を数え違えていることが多い（ファイルの見出し行数の設定ミス）。
        #    どの行が見出しか目で見て直せるよう、ファイルの先頭を素のまま添える。
        try:
            h0, r0 = intake_reader.read_table(data, fname, password=password, skip_rows=0)
            out["ファイルの先頭"] = [h0] + r0[:4]
            out["シートの見出し"] = sheet_headers
        except Exception:
            pass
        return out

    if dry_run:
        out["件数"] = len(rows)
        out["結果"] = (f"🧪 ここまでOK：{len(rows)}件を貼り付けられます"
                       f"（{tab} の{keep}行目までの見出しは残ります）")
        out["見出し"] = "／".join(str(h) for h in headers[:8])
        out["先頭の行"] = "／".join(str(c) for c in (rows[0][:6] if rows else []))
        return out

    try:
        n = paste_to_sheet(gc, sheet_id, tab, rows, keep_rows=keep, backup=True)
    except Exception as e:
        out["結果"] = f"❌ 貼り付けに失敗: {str(e)[:150]}"
        return out

    out["件数"] = n
    out["結果"] = f"✅ {n}件を貼り付けました"
    return out
