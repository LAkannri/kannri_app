"""
🎧 通録ダウンロード（ブルービーンの録音 → Googleドライブ → ブルービーンから一括削除）

  ① ロボット（ブルービーン投入のロボットのログインだけ借りる）が、その月を **1日ずつ** 落とす
     （`robot.py --run <ロボット> <フォルダ> --callrec download`）
  ② 1日ずつの tar を **1本の月まとめ（`voicedata_<1日>_<月末>.tar`）につなぎ直し**、
     Drive の `<年>年/元データZIP/` に入れる（人が月末にしていたことと同じ置き場・同じ名前）
     ⚠️ 人が1か月まとめて落としていた分は、6月・8月が `.crdownload`（途中で切れた）のまま残っていた。
  ②-2 日付フォルダ `<年>年/<月>月/<日>/` にも中身を入れる（ふだんは人が毎日入れている。
     同じ名前・同じ大きさがもうあれば入れない＝抜けていた分だけ埋まる）
  ③ 月まとめが同じ大きさで入ったか、全ファイルが日付フォルダにあるかを1つずつ確かめる
  ④ 全部そろったときだけ、ブルービーンの一括削除（その月の1日〜月末）を押す（`--callrec delete --submit`）

⚠️ 一括削除は取り消せない。①〜③で1つでも欠けたら、消さずに止める。
⚠️ 1か月まとめて落とすと途中で切れ、Chromeが日付なしで取り直して `ダウンロード.htm`
   （ブルービーンのエラー画面）になる（2026-09-30）。だから1日ずつ。
⭐ 動かすのは月末の営業後。月末に失敗したら、翌日（1日）に**先月の分**をやる（`pending_months`）。
設定は Supabase の予約行 `__callrec__`（`robot` / `drive_folder` / `start_month` / `done` / `token_enc` / `last_run`）。
⚠️ DriveのフォルダIDはコードに書かない（公開リポジトリ）。
"""
import datetime
import io
import json
import os
import re
import shutil
import tarfile
import time

ROW = "__callrec__"
ARCHIVE_FOLDER = "元データZIP"
CHUNK = 64 * 1024 * 1024
DEFAULT_ROBOT = "共通_ブルービーン投入"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
_ALL = dict(supportsAllDrives=True)
_ALL_LIST = dict(supportsAllDrives=True, includeItemsFromAllDrives=True)


# ==========================================
# ⚙️ 設定
# ==========================================
def load_cfg(supabase) -> dict:
    try:
        res = supabase.table("merchants").select("config_json").eq("id", ROW).execute()
        return (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception:
        return {}


def save_cfg(supabase, changes: dict):
    """読み直して、触ったところだけ変える（許可と設定が同じ行にあるため）。"""
    cur = load_cfg(supabase)
    for k, v in changes.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    supabase.table("merchants").upsert({
        "id": ROW, "name": "（通録ダウンロードの設定）", "is_active": False,
        "connector_type": "settings", "config_json": cur}).execute()


def folder_id(text: str) -> str:
    s = str(text or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_\-]{10,})", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", s) else ""


# ==========================================
# 📅 どの月をやるか
# ==========================================
def month_range(ym: str) -> tuple:
    y, m = int(ym[:4]), int(ym[5:7])
    first = datetime.date(y, m, 1)
    nxt = datetime.date(y + (m == 12), m % 12 + 1, 1)
    return first.isoformat(), (nxt - datetime.timedelta(days=1)).isoformat()


def _ym(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def pending_months(cfg: dict, today: datetime.date = None) -> list:
    """いまやるべき月（古い順）。

    ・先月がまだ済んでいなければ先月（月末に失敗した翌日の取り返し）
    ・今日が月末なら今月
    `start_month` より前は相手にしない（使いはじめる前の月を消しに行かない）。
    """
    today = today or datetime.date.today()
    done = set(cfg.get("done") or [])
    start = str(cfg.get("start_month", "") or "2026-09")
    prev = _ym(today.replace(day=1) - datetime.timedelta(days=1))
    out = [prev]
    if (today + datetime.timedelta(days=1)).month != today.month:
        out.append(_ym(today))
    return [m for m in out if m >= start and m not in done]


# ==========================================
# 🔑 Drive の許可（GASの書き込みと同じ鍵・別の許可）
# ==========================================
def _token_path() -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EnkanAI")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "drive_oauth_token.json")


def _save_token(supabase, text: str):
    import gas_deploy
    with open(_token_path(), "w", encoding="utf-8") as f:
        f.write(text)
    f = gas_deploy._fernet()
    if f and supabase is not None:
        try:
            save_cfg(supabase, {"token_enc": f.encrypt(text.encode()).decode(),
                                "token_saved_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception:
            pass


def drive_creds(supabase):
    """保存してある許可（期限切れなら更新）。無ければ None。

    ⭐ 許可は Supabase にも暗号化して残す＝自動実行用のPCでも同じ許可を使える。
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import gas_deploy
    raw = ""
    if os.path.isfile(_token_path()):
        try:
            raw = open(_token_path(), encoding="utf-8").read()
        except Exception:
            raw = ""
    if not raw and supabase is not None:
        enc = load_cfg(supabase).get("token_enc", "")
        f = gas_deploy._fernet()
        if enc and f:
            try:
                raw = f.decrypt(str(enc).encode()).decode()
            except Exception:
                raw = ""
    if not raw:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), DRIVE_SCOPES)
    except Exception:
        return None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(supabase, creds.to_json())
        except Exception:
            return None
    return creds if creds.valid else None


def authorize_local(supabase, timeout_sec: int = 180):
    """このPCのブラウザを開いて、1回だけ許可をもらう（01_通録に入れられるアカウントで）。"""
    from google_auth_oauthlib.flow import InstalledAppFlow
    import gas_deploy
    cfg = gas_deploy.client_config()
    if not cfg:
        raise RuntimeError("つなぐための鍵（GOOGLE_OAUTH_CLIENT_JSON）がありません。"
                           "「GASをアプリが書き込む」と同じ鍵を使います")
    flow = InstalledAppFlow.from_client_config(cfg, DRIVE_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  timeout_seconds=timeout_sec, authorization_prompt_message="",
                                  success_message="許可できました。この画面は閉じてください。")
    _save_token(supabase, creds.to_json())
    return creds


def forget(supabase):
    try:
        os.remove(_token_path())
    except Exception:
        pass
    save_cfg(supabase, {"token_enc": None, "token_saved_at": None})


def drive(supabase):
    from googleapiclient.discovery import build
    creds = drive_creds(supabase)
    if not creds:
        raise RuntimeError("Googleドライブの許可がありません（通録ダウンロードの⚙️設定で「🔑 ドライブの許可を出す」）")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def folder_title(dv, fid: str) -> str:
    return dv.files().get(fileId=fid, fields="name", **_ALL).execute().get("name", "")


def _q(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def _child_folder(dv, parent: str, name: str, create: bool):
    q = (f"'{parent}' in parents and name = '{_q(name)}' and trashed = false "
         "and mimeType = 'application/vnd.google-apps.folder'")
    got = dv.files().list(q=q, fields="files(id,name)", pageSize=10, **_ALL_LIST).execute().get("files", [])
    if got:
        return got[0]["id"]
    if not create:
        return ""
    f = dv.files().create(body={"name": name, "parents": [parent],
                                "mimeType": "application/vnd.google-apps.folder"},
                          fields="id", **_ALL).execute()
    return f["id"]


def _files_in(dv, parent: str) -> dict:
    """{名前: [大きさ, …]}（同じ名前が2つあることもある）"""
    out, tok = {}, None
    while True:
        r = dv.files().list(q=f"'{parent}' in parents and trashed = false",
                            fields="nextPageToken,files(name,size)", pageSize=1000,
                            pageToken=tok, **_ALL_LIST).execute()
        for f in r.get("files", []):
            out.setdefault(f["name"], []).append(int(f.get("size") or -1))
        tok = r.get("nextPageToken")
        if not tok:
            return out


def _drive_path(member_name: str):
    """`2026/09/30/xxx.wav` → ("2026年", "9月", "30", "xxx.wav")。形が違えば None。

    ⭐ Driveのフォルダは、これまで人が作ってきた名前に合わせる（年＝2026年／月＝9月／日＝01）。
    """
    parts = [p for p in str(member_name).replace("\\", "/").split("/") if p]
    if len(parts) < 4 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
        return None
    return f"{int(parts[0])}年", f"{int(parts[1])}月", parts[2], parts[-1]


def upload_tar(dv, root: str, tar_path: str, log) -> dict:
    """tar の中身を Drive に入れて、そろったかを確かめる。

    戻り値：{"件数", "入れた", "もとからあった", "欠け": [名前…]}
    """
    from googleapiclient.http import MediaIoBaseUpload
    folders, existing = {}, {}
    added = already = 0
    want = []
    with tarfile.open(tar_path) as t:
        for m in t:
            if not m.isfile():
                continue
            p = _drive_path(m.name)
            if not p:
                raise ValueError(f"tar の中の置き場所が想定と違います：{m.name}")
            y, mo, d, name = p
            key = (y, mo, d)
            if key not in folders:
                fy = _child_folder(dv, root, y, True)
                fm = _child_folder(dv, fy, mo, True)
                folders[key] = _child_folder(dv, fm, d, True)
                existing[key] = _files_in(dv, folders[key])
            want.append((key, name, m.size))
            if m.size in existing[key].get(name, []):
                already += 1
                continue
            data = t.extractfile(m).read()
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype="audio/wav", resumable=True)
            for _try in range(3):
                try:
                    dv.files().create(body={"name": name, "parents": [folders[key]]},
                                      media_body=media, fields="id", **_ALL).execute()
                    break
                except Exception as e:
                    if _try == 2:
                        raise
                    log(f"　　⚠️ {name} を入れ直します（{str(e)[:80]}）")
                    time.sleep(5)
                    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="audio/wav", resumable=True)
            added += 1
    # ③ 入れ終わってから、Driveを読み直して1つずつ照らし合わせる
    now = {k: _files_in(dv, fid) for k, fid in folders.items()}
    missing = [f"{k[2]}/{n}" for k, n, size in want if size not in now[k].get(n, [])]
    return {"件数": len(want), "入れた": added, "もとからあった": already, "欠け": missing}


def join_tars(paths, out_path: str) -> int:
    """1日ずつの tar を、1本の月まとめにつなぐ。戻り値：ファイル数。"""
    n = 0
    seen_dirs = set()
    tmp = out_path + ".part"
    with tarfile.open(tmp, "w") as out:
        for p in paths:
            with tarfile.open(p) as t:
                for m in t:
                    if m.isdir():
                        # 1日ずつの tar には毎回「2026」「2026/09」が入っている。1回だけにする
                        if m.name in seen_dirs:
                            continue
                        seen_dirs.add(m.name)
                    out.addfile(m, t.extractfile(m) if m.isfile() else None)
                    n += m.isfile()
    os.replace(tmp, out_path)
    return n


def upload_file(dv, parent: str, path: str, log) -> tuple:
    """大きいファイルを分けて入れる。(入れたか, Driveにある大きさの並び)

    ⚠️ **同じ名前のファイルがもうあれば、大きさが違っても入れない。**
       人がブルービーンから1か月まとめて落としたtarと、1日ずつつないだtarは、
       中身が同じでも大きさが少し違う。大きさで見比べると同じ名前の月まとめが2本になる。
       録音がそろっているかは、日付フォルダの方で1ファイルずつ確かめている。
    """
    from googleapiclient.http import MediaFileUpload
    name = os.path.basename(path)
    have = _files_in(dv, parent).get(name, [])
    if have:
        return False, have
    media = MediaFileUpload(path, mimetype="application/x-tar", resumable=True, chunksize=CHUNK)
    req = dv.files().create(body={"name": name, "parents": [parent]}, media_body=media,
                            fields="id,size", **_ALL)
    resp, last = None, -1
    while resp is None:
        for _try in range(5):
            try:
                status, resp = req.next_chunk()
                break
            except Exception as e:
                if _try == 4:
                    raise
                log(f"　　⚠️ 送り直します（{str(e)[:80]}）")
                time.sleep(10)
        if status and int(status.progress() * 10) != last:
            last = int(status.progress() * 10)
            log(f"　　… {last * 10}%")
    return True, [int(resp.get("size") or -1)]


# ==========================================
# ▶ 通しで動かす（画面と時間指定が同じ関数を通る）
# ==========================================
def work_dir(ym: str) -> str:
    import sms_runner
    return sms_runner.work_dir("通録ダウンロード", ym)


def _robot(args, wd: str, name: str, timeout_sec: int):
    import sms_runner
    return sms_runner._run_robot_cli(args, os.path.join(wd, name), timeout_sec)


def run_month(supabase, cfg: dict, ym: str, delete: bool = True, log=print) -> dict:
    """その月を ①落とす → ②入れる → ③確かめる → ④消す。"""
    import auto_jobs
    steps = auto_jobs._Steps()
    robot = str(cfg.get("robot", "") or DEFAULT_ROBOT)
    root = folder_id(cfg.get("drive_folder", ""))
    if not root:
        steps.add("準備", "🛑", "保存先のDriveフォルダが未設定です（通録ダウンロードの⚙️設定）")
        return steps.result()
    try:
        dv = drive(supabase)
        folder_title(dv, root)
    except Exception as e:
        steps.add("準備", "🛑", f"Googleドライブにつなげません：{str(e)[:200]}")
        return steps.result()
    start, end = month_range(ym)
    wd = work_dir(ym)
    days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1

    # ① 1日ずつ落とす
    log(f"🎧 {start}〜{end} を1日ずつ落とします…")
    ok, tail = _robot(["--run", robot, wd, "--callrec", "download",
                       "--var", f"開始日={start}", "--var", f"終了日={end}"],
                      wd, "download.log", timeout_sec=days * 30 * 60 + 600)
    try:
        results = json.load(open(os.path.join(wd, "通録_ダウンロード結果.json"), encoding="utf-8"))
    except Exception:
        results = []
    got = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    if not ok or bad or len(got) != days:
        why = "、".join(f"{r['日付'][5:]}（{r.get('理由', '')[:60]}）" for r in bad) or tail[-400:]
        steps.add("① ブルービーンから落とす", "🛑",
                  f"{len(got)}/{days}日 落とせました。落とせなかった日：{why}")
        return steps.result()
    total = sum(r.get("件数", 0) for r in got)
    mb = sum(r.get("バイト", 0) for r in got) / 1048576
    steps.add("① ブルービーンから落とす", "✅", f"{days}日分・{total}件・{mb:.0f}MB")

    # ② 月まとめにつないで、元データZIPへ
    month_tar = os.path.join(wd, f"voicedata_{start}_{end}.tar")
    try:
        log("📦 1本の月まとめにつないでいます…")
        n = join_tars([r["path"] for r in got], month_tar)
        if n != total:
            raise ValueError(f"つないだ数（{n}）が落とした数（{total}）と合いません")
        y_id = _child_folder(dv, root, f"{int(ym[:4])}年", True)
        a_id = _child_folder(dv, y_id, ARCHIVE_FOLDER, True)
        log(f"📤 {os.path.basename(month_tar)} を「{ARCHIVE_FOLDER}」に入れます…")
        _new, _have = upload_file(dv, a_id, month_tar, log)
        size = os.path.getsize(month_tar)
        if _new and size not in _files_in(dv, a_id).get(os.path.basename(month_tar), []):
            raise ValueError("入れたあとで見ると、Driveの大きさが違います")
    except Exception as e:
        steps.add("② 元データZIPに入れる", "🛑", str(e)[:300])
        return steps.result()
    steps.add("② 元データZIPに入れる", "✅",
              f"{os.path.basename(month_tar)}（{size / 1073741824:.2f}GB）"
              + ("を入れました" if _new else
                 ("は同じ大きさのものがもうありました" if size in _have else
                  "は同じ名前のものがもうあるので入れていません（手で入れた月まとめ。録音は日付フォルダで1件ずつ確かめます）")))

    # ②-2 日付フォルダにも（抜けていた分だけ）
    added = already = 0
    missing = []
    for r in got:
        log(f"📤 {r['日付']} をDriveに入れます…")
        try:
            u = upload_tar(dv, root, r["path"], log)
        except Exception as e:
            steps.add("②-2 日付フォルダ", "🛑", f"{r['日付']} でつまずきました：{str(e)[:200]}")
            return steps.result()
        added += u["入れた"]
        already += u["もとからあった"]
        missing += u["欠け"]
    if missing:
        steps.add("②-2 日付フォルダ", "🛑",
                  f"Driveに入っていないファイルが {len(missing)}件 あります（消さずに止めました）："
                  + "、".join(missing[:10]))
        return steps.result()
    steps.add("②-2 日付フォルダ", "✅",
              f"{total}件がそろっています（抜けていて今回入れた {added}件／もとからあった {already}件）")

    # ④ 全部そろったので消す
    if not delete:
        steps.add("③ ブルービーンから一括削除", "⏸", "お試しなので消していません")
        return steps.result()
    log(f"🗑 ブルービーンの {start}〜{end} を一括削除します…")
    ok, tail = _robot(["--run", robot, wd, "--callrec", "delete", "--submit",
                       "--var", f"開始日={start}", "--var", f"終了日={end}"],
                      wd, "delete.log", timeout_sec=3 * 3600)
    line = next((ln.strip() for ln in reversed(tail.splitlines()) if "一括削除：" in ln), "")
    if not ok:
        steps.add("③ ブルービーンから一括削除", "🛑", line or tail[-400:])
        return steps.result()
    steps.add("③ ブルービーンから一括削除", "✅", line.replace("✅ 一括削除：", "") or "消しました")
    cur = load_cfg(supabase)
    save_cfg(supabase, {"done": sorted(set(cur.get("done") or []) | {ym})})
    # 落としたファイルは Drive と照らし合わせ済み。PCの容量を空ける
    for p in [r["path"] for r in got] + [month_tar]:
        try:
            os.remove(p)
        except Exception:
            pass
    return steps.result()


def run(supabase, cfg: dict = None, today: datetime.date = None) -> dict:
    """時間指定から：まだ済んでいない月を古い順に。無ければ ⏹。"""
    import auto_jobs
    cfg = cfg if cfg is not None else load_cfg(supabase)
    months = pending_months(cfg, today)
    if not months:
        steps = auto_jobs._Steps()
        steps.add("通録ダウンロード", "⏹", "きょうやる月はありません（月末か、先月が済んでいないときだけ動きます）")
        return steps.result()
    rows, state = [], "完了"
    for ym in months:
        r = run_month(supabase, cfg, ym)
        for x in r["工程"]:
            x = dict(x)
            x["工程"] = f"{ym[:4]}年{int(ym[5:])}月 {x['工程']}"
            rows.append(x)
        if r["結果"] != "完了":
            state = r["結果"]
            break
    try:
        save_cfg(supabase, {"last_run": {"at": time.strftime("%Y-%m-%d %H:%M"), "結果": state,
                                         "工程": rows}})
    except Exception:
        pass
    return {"結果": state, "工程": rows}
