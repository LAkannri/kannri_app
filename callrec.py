"""
🎧 通録ダウンロード（ブルービーンの録音 → Googleドライブ → ブルービーンから一括削除）

  ⓪ **毎日（営業後）**：きょうの分と、前の日の分（取り直し）を落として日付フォルダ
     `<年>年/<月>月/<日>/` に入れる（`run_daily`）。落とした1日ずつの tar は月末までPCに取っておく（`day_tar`）。
     ⚠️ 前の日を取り直すのは、営業後に落としたあとに入った録音を拾うため（月末に消してしまわないように）。
  ① 月末：取っておいた1日ずつを使う。その日が終わってから落としていない日だけ落とし直す（`day_complete`）。
     ブルービーンから1か月まとめて落とさない（途中で切れる）。
  ② 1日ずつの tar を1本の月まとめ（`voicedata_<1日>_<月末>.tar`）につなぎ、Drive の `<年>年/元データZIP/` へ。
     ⚠️ 同じ名前がもうあれば入れない（人が落とした月まとめと大きさが少し違い、2本になるため）。
  ③ 全ファイルが日付フォルダに同じ名前・同じ大きさであるかを1つずつ確かめる（抜けていれば入れる）
  ④ 全部そろったときだけ、ブルービーンの一括削除（その月の1日〜月末）を押す（`--callrec delete --submit`）

⚠️ 一括削除は取り消せない。①〜③で1つでも欠けたら、消さずに止める。
⚠️ 1か月まとめて落とすと途中で切れ、Chromeが日付なしで取り直して `ダウンロード.htm`
   （ブルービーンのエラー画面）になる（2026-09-30）。人が落としていた元データZIPも6月・8月は `.crdownload` のまま。
⭐ 時間指定は「毎日・営業後」の1本（`run`）。月末は月の締めまで行い、月末に失敗したら翌日に先月の分をやる（`pending_months`）。
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
DAYS_PER_LAUNCH = 5      # ロボット1回で落とす日数（7〜8日分でロボットごと落ちていた）
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


def callrec_days(start: str, end: str) -> list:
    d0, d1 = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


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
    """1日ずつの tar の置き場。⚠️ OneDriveの外（1か月で数GB。同期とぶつかるため）。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EnkanAI", "通録", ym)
    os.makedirs(base, exist_ok=True)
    return base


_LOCK = {"n": 0, "f": None}


class _Only1:
    """このPCで通録ダウンロードを同時に1つだけにする（OSのロック＝落ちても残らない）。

    ⚠️ 画面の月末ボタンを押したとき、別の実行が月まとめを送っている最中で、
       同じファイルを作り直そうとして「アクセスが拒否されました」で止まった（2026-09-30）。
       同じ実行の中（run → run_daily → run_month）は入れ子で通す。
    """

    def __enter__(self):
        if _LOCK["n"]:
            _LOCK["n"] += 1
            return True
        d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "EnkanAI", "通録")
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, "callrec.lock"), "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        _LOCK.update(n=1, f=f)
        return True

    def __exit__(self, *a):
        if _LOCK["n"] > 1:
            _LOCK["n"] -= 1
            return
        if _LOCK["f"]:
            try:
                _LOCK["f"].close()
            except Exception:
                pass
        _LOCK.update(n=0, f=None)


BUSY = "このPCで別の通録ダウンロードが動いています（終わってからもう一度）"


def day_tar(day: str) -> str:
    """その日の tar の置き場（月のフォルダ）。月末につなぐまで取っておく。"""
    return os.path.join(work_dir(day[:7]), f"voicedata_{day}.tar")


def day_complete(day: str) -> bool:
    """その日の tar が「その日が終わってから」落としたものか。

    ⚠️ 営業後に落としても、そのあとに入った録音は入っていない。月末にそのまま消すと、
       Driveに無い録音を消してしまう。だから毎日、前の日の分を取り直す（`run_daily`）。
    """
    p = day_tar(day)
    if not os.path.isfile(p):
        return False
    got = datetime.date.fromtimestamp(os.path.getmtime(p))
    return got > datetime.date.fromisoformat(day)


def _robot(args, wd: str, name: str, timeout_sec: int):
    import sms_runner
    return sms_runner._run_robot_cli(args, os.path.join(wd, name), timeout_sec)


def download_days(cfg: dict, days: list, log=print) -> tuple:
    """ロボットで日を落とす（月ごとにブラウザ1回）。戻り値：(落とせた日の結果, 落とせなかった日の説明)"""
    days = sorted(set(days))
    if not days:
        return [], ""
    robot = str(cfg.get("robot", "") or DEFAULT_ROBOT)
    log(f"🎧 ブルービーンから落とします：{'、'.join(d[5:] for d in days)}")
    got, why = [], {}
    for ym in sorted({d[:7] for d in days}):
        left = [d for d in days if d[:7] == ym]
        wd = work_dir(ym)
        res_path = os.path.join(wd, "通録_ダウンロード結果.json")
        # ⚠️ ロボット（Playwright）ごと落ちることがある（7〜8日分ごとに実際に起きた）。
        #    1回に落とすのは DAYS_PER_LAUNCH 日まで。落とせた日は残し、残りの日だけ起動し直す。
        #    1日も進まない起動が3回続いたら止める。
        launch = stuck = 0
        while left and stuck < 3:
            launch += 1
            batch = left[:DAYS_PER_LAUNCH]
            if launch > 1:
                log(f"　🔁 残りの {len(left)}日を続けます（ロボット {launch}回目）")
            try:
                os.remove(res_path)
            except Exception:
                pass
            ok, tail = _robot(["--run", robot, wd, "--callrec", "download", "--var", "日付=" + ",".join(batch)],
                              wd, f"download_{launch}.log", timeout_sec=len(batch) * 30 * 60 + 600)
            try:
                results = json.load(open(res_path, encoding="utf-8"))
            except Exception:
                results = []
            for r in results:
                if r.get("ok"):
                    got.append(r)
                    why.pop(r["日付"], None)
                else:
                    why[r["日付"]] = str(r.get("理由", ""))[:60]
            done_ok = {r["日付"] for r in results if r.get("ok")}
            for d in batch:
                if d not in done_ok and d not in why:
                    why[d] = "ロボットがそこまで進めませんでした：" + tail.strip().splitlines()[-1][:120] if tail.strip() else "ロボットが止まりました"
            stuck = 0 if done_ok else stuck + 1
            left = [d for d in left if d not in done_ok]
    bad = [f"{d[5:]}（{why.get(d, '')}）" for d in days if d not in {r["日付"] for r in got}]
    return got, "、".join(bad)


def put_days(dv, root: str, results: list, log=print) -> tuple:
    """落とした日を日付フォルダに入れて確かめる。(入れた, もとからあった, 欠け)"""
    added = already = 0
    missing = []
    for r in results:
        log(f"📤 {r['日付']} をDriveの日付フォルダに入れます…")
        u = upload_tar(dv, root, r["path"], log)
        added += u["入れた"]
        already += u["もとからあった"]
        missing += u["欠け"]
    return added, already, missing


def _open_drive(supabase, cfg, steps):
    root = folder_id(cfg.get("drive_folder", ""))
    if not root:
        steps.add("準備", "🛑", "保存先のDriveフォルダが未設定です（通録ダウンロードの⚙️設定）")
        return None, None
    try:
        dv = drive(supabase)
        folder_title(dv, root)
        return dv, root
    except Exception as e:
        steps.add("準備", "🛑", f"Googleドライブにつなげません：{str(e)[:200]}")
        return None, None


def run_daily(supabase, cfg: dict, today: datetime.date = None, log=print, steps=None) -> dict:
    """毎日：きょうの分と、前の日の分（取り直し）を落として日付フォルダに入れる。"""
    import auto_jobs
    steps = steps or auto_jobs._Steps()
    with _Only1() as got:
        if not got:
            steps.add("通録ダウンロード", "🛑", BUSY)
            return steps.result()
        return _run_daily(supabase, cfg, today, log, steps)


def _run_daily(supabase, cfg, today, log, steps):
    today = today or datetime.date.today()
    dv, root = _open_drive(supabase, cfg, steps)
    if not dv:
        return steps.result()
    start = str(cfg.get("start_month", "") or "2026-09")
    yday = (today - datetime.timedelta(days=1)).isoformat()
    days = [today.isoformat()]
    # 前の日は、その日が終わってから落としていなければ取り直す（営業後に入った録音を拾う）
    if yday[:7] >= start and yday[:7] not in set(cfg.get("done") or []) and not day_complete(yday):
        days.insert(0, yday)
    got, bad = download_days(cfg, days, log)
    if bad:
        steps.add("毎日 ① ブルービーンから落とす", "🛑", f"落とせなかった日：{bad}")
    if got:
        steps.add("毎日 ① ブルービーンから落とす", "✅",
                  "、".join(f"{r['日付'][5:]}（{r['件数']}件）" for r in got))
        try:
            added, already, missing = put_days(dv, root, got, log)
        except Exception as e:
            steps.add("毎日 ② 日付フォルダに入れる", "🛑", str(e)[:300])
            return steps.result()
        if missing:
            steps.add("毎日 ② 日付フォルダに入れる", "🛑",
                      f"Driveに入っていないファイルが {len(missing)}件：" + "、".join(missing[:10]))
        else:
            steps.add("毎日 ② 日付フォルダに入れる", "✅",
                      f"そろっています（今回入れた {added}件／もとからあった {already}件）")
    return steps.result()


def run_month(supabase, cfg: dict, ym: str, delete: bool = True, log=print, steps=None,
              today: datetime.date = None) -> dict:
    """月末：取っておいた1日ずつをつなぐ → 元データZIP → 日付フォルダを確かめる → 一括削除。

    その日が終わってから落としていない日（毎日の実行が止まっていた日など）だけ、ここで落とし直す。
    ⚠️ 月末の当日は、営業後に動かす前提で「きょうの分」を最後のひと落としにする。
    """
    import auto_jobs
    steps = steps or auto_jobs._Steps()
    with _Only1() as got:
        if not got:
            steps.add("通録ダウンロード", "🛑", BUSY)
            return steps.result()
        return _run_month(supabase, cfg, ym, delete, log, steps, today)


def _run_month(supabase, cfg, ym, delete, log, steps, today):
    today = today or datetime.date.today()
    start, end = month_range(ym)
    if end > today.isoformat():
        steps.add("月末 ① 月まとめ", "🛑", f"{ym} はまだ終わっていません（月末は {end}）")
        return steps.result()
    dv, root = _open_drive(supabase, cfg, steps)
    if not dv:
        return steps.result()
    days = callrec_days(start, end)
    wd = work_dir(ym)
    # 落とし直す日：その日が終わってから落としていない日。月末の当日は今落としたものを使う
    need = [d for d in days if not day_complete(d)
            and not (d == today.isoformat() and os.path.isfile(day_tar(d))
                     and datetime.date.fromtimestamp(os.path.getmtime(day_tar(d))) == today
                     and time.time() - os.path.getmtime(day_tar(d)) < 3 * 3600)]
    if need:
        got, bad = download_days(cfg, need, log)
        if bad:
            steps.add("月末 ① ブルービーンから落とす", "🛑", f"落とせなかった日：{bad}（消さずに止めました）")
            return steps.result()
        steps.add("月末 ① ブルービーンから落とす", "✅",
                  f"毎日の分に無かった {len(got)}日を落としました：" + "、".join(r["日付"][5:] for r in got))
    paths = [day_tar(d) for d in days]
    lost = [d for d, p in zip(days, paths) if not os.path.isfile(p)]
    if lost:
        steps.add("月末 ① 月まとめ", "🛑", f"1日ずつのファイルがありません：{'、'.join(lost)}")
        return steps.result()

    # ② 月まとめにつないで、元データZIPへ
    month_tar = os.path.join(wd, f"voicedata_{start}_{end}.tar")
    try:
        log("📦 1本の月まとめにつないでいます…")
        total = join_tars(paths, month_tar)
        y_id = _child_folder(dv, root, f"{int(ym[:4])}年", True)
        a_id = _child_folder(dv, y_id, ARCHIVE_FOLDER, True)
        log(f"📤 {os.path.basename(month_tar)} を「{ARCHIVE_FOLDER}」に入れます…")
        _new, _have = upload_file(dv, a_id, month_tar, log)
        size = os.path.getsize(month_tar)
        if _new and size not in _files_in(dv, a_id).get(os.path.basename(month_tar), []):
            raise ValueError("入れたあとで見ると、Driveの大きさが違います")
    except Exception as e:
        steps.add("月末 ② 元データZIPに入れる", "🛑", str(e)[:300])
        return steps.result()
    steps.add("月末 ② 元データZIPに入れる", "✅",
              f"{os.path.basename(month_tar)}（{total}件・{size / 1073741824:.2f}GB）"
              + ("を入れました" if _new else
                 ("は同じ大きさのものがもうありました" if size in _have else
                  "は同じ名前のものがもうあるので入れていません（録音は日付フォルダで1件ずつ確かめます）")))

    # ③ 日付フォルダに全部あるか（抜けていれば入れる）
    try:
        added, already, missing = put_days(dv, root, [{"日付": d, "path": p} for d, p in zip(days, paths)], log)
    except Exception as e:
        steps.add("月末 ③ 日付フォルダを確かめる", "🛑", str(e)[:300])
        return steps.result()
    if missing:
        steps.add("月末 ③ 日付フォルダを確かめる", "🛑",
                  f"Driveに入っていないファイルが {len(missing)}件 あります（消さずに止めました）："
                  + "、".join(missing[:10]))
        return steps.result()
    steps.add("月末 ③ 日付フォルダを確かめる", "✅",
              f"{total}件がそろっています（抜けていて今回入れた {added}件／もとからあった {already}件）")

    # ④ 全部そろったので消す
    if not delete:
        steps.add("月末 ④ ブルービーンから一括削除", "⏸", "お試しなので消していません")
        return steps.result()
    log(f"🗑 ブルービーンの {start}〜{end} を一括削除します…")
    robot = str(cfg.get("robot", "") or DEFAULT_ROBOT)
    ok, tail = _robot(["--run", robot, wd, "--callrec", "delete", "--submit",
                       "--var", f"開始日={start}", "--var", f"終了日={end}"],
                      wd, "delete.log", timeout_sec=3 * 3600)
    line = next((ln.strip() for ln in reversed(tail.splitlines()) if "一括削除：" in ln), "")
    if not ok:
        steps.add("月末 ④ ブルービーンから一括削除", "🛑", line or tail[-400:])
        return steps.result()
    steps.add("月末 ④ ブルービーンから一括削除", "✅", line.replace("✅ 一括削除：", "") or "消しました")
    cur = load_cfg(supabase)
    save_cfg(supabase, {"done": sorted(set(cur.get("done") or []) | {ym})})
    # Drive と照らし合わせ済み。PCの容量を空ける
    for p in paths + [month_tar]:
        try:
            os.remove(p)
        except Exception:
            pass
    return steps.result()


def run(supabase, cfg: dict = None, today: datetime.date = None) -> dict:
    """時間指定から（毎日・営業後）：きょうの分を入れる。月末（と、先月が済んでいない日）は月の締めも。"""
    import auto_jobs
    cfg = cfg if cfg is not None else load_cfg(supabase)
    today = today or datetime.date.today()
    steps = auto_jobs._Steps()
    with _Only1() as got:
        if not got:
            steps.add("通録ダウンロード", "🛑", BUSY)
            return steps.result()
        run_daily(supabase, cfg, today, steps=steps)
        for ym in pending_months(cfg, today):
            if any(r["結果"] == "🛑" for r in steps.rows):
                break
            run_month(supabase, load_cfg(supabase), ym, today=today, steps=steps)
    res = steps.result()
    try:
        save_cfg(supabase, {"last_run": {"at": time.strftime("%Y-%m-%d %H:%M"), **res}})
    except Exception:
        pass
    return res
