"""
📱 SMS送信（プッシュプロ一括送信）

パターン（送る文面・条件のちがい）ごとに設定を登録し、「送信をはじめる」で次の順に進む：

  ① シートを更新     … SFコネクタで、登録したシートを順に更新する
  ② 中身をチェック   … 登録したルールで、直すべき行を一覧に出す（ここは人が直す）
  ③ CSVを用意       … スプシのGASが書き出したCSVを受け取る（毎回おなじ名前で置く）
  ④ 一括送信        … 録画したロボットが、そのCSVをプッシュプロに入れて送信する
  ⑤ Salesforceへ投入 … 「送った」ことをSalesforceに残す（データローダーと同じしくみ）

【録画は「⚙️ その他設定」で一度だけ】
SFコネクタの更新もプッシュプロの送信も、どのスプシでも押す場所は同じ。
違うのは「どのシートを選ぶか」「どのファイルを渡すか」だけなので、
共通ロボットを1台ずつ作れば、ここでは**シート名を選ぶだけ**でよい。

【二重送信を防ぐ】
プッシュプロは一括送信なので、送ってしまった分は取り消せない。そこで
  ・送る前に②で弾く（間違った行を送らない）
  ・送った宛先を記録し、次回のCSVから自動で外せるようにする
  ・途中で止まっても「送ったかもしれない」は送信済みに寄せる（二重送信のほうが怖い）
"""
import json
import os

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import characters as ch
import common_robots
import sf_ui
import sms_runner
import theme

st.set_page_config(page_title="SMS送信 - エンカンAI", layout="wide")

theme.inject_theme()

# 🔑 接続キーのファイルが壊れていたら、直す場所を名指しして止める。
#    別のPCに入れるときに、コピーし損ねて動かなくなることがあるため。
import secrets_check
secrets_check.check()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("📱", "SMSを一括で送る",
                  "シートを更新 → 中身をチェック → CSVを用意 → プッシュプロで一括送信、までを一本にします。",
                  color=c["color"])


# ==========================================
# 🔌 つなぎこみ
# ==========================================
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

_REDEPLOY_HINT = """👉 **コードを直しただけでは、公開されているものは変わりません。**

1. Apps Script の右上 **デプロイ → デプロイを管理**
2. いまのデプロイの **鉛筆（編集）** を押す
3. **バージョン**を「**新バージョン**」に変える ← ここを飛ばすと古いままです
4. **デプロイ** を押す（URLは変わりません）

それでも同じなら、スクリプトの中に `const API_TOKEN = 'ここに長い合言葉を書く';` が**残っていないか**（古い版のかたまり）を確かめてください。"""

SETTINGS_ID = "__sms__"          # ロボット一覧には出さない予約行（id が __ で始まる）
CSV_SOURCES = ["GASのURLを叩いて受け取る（推奨）",
               "GASがDriveに書き出したものを使う",
               "ロボットにGASのボタンを押させて受け取る",
               "アプリがシートから作る"]


def _load_settings() -> dict:
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}


def _save_settings(cfg: dict):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（SMS送信の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cfg}).execute()


@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds)


def _get_gspread_client():
    try:
        sa_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        sa_json = ""
    if not sa_json:
        return None
    try:
        return _build_gspread_client(sa_json)
    except Exception:
        return None


def _sa_json() -> str:
    try:
        return st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    except Exception:
        return ""


@st.cache_data(ttl=120, show_spinner=False)
def _tab_gids(_gc, sheet_url: str) -> dict:
    """タブ名 → gid。「そのシートを開く」URLを組み立てるのに使う。"""
    sh = _gc.open_by_url(sheet_url) if sheet_url.startswith("http") else _gc.open_by_key(sheet_url)
    return {w.title: w.id for w in sh.worksheets()}


def _tab_names(_gc, sheet_url: str):
    return list(_tab_gids(_gc, sheet_url).keys())


def _gids_of(pat: dict) -> dict:
    try:
        return _tab_gids(gc, pat.get("sheet_url", "")) if gc else {}
    except Exception:
        return {}


def _csv_sheets(pat: dict):
    """CSVにするシートの一覧。

    はじめは1つしか持てなかった（`gas_sheet`）。増やしたあとも、
    前に登録したパターンがそのまま動くように、両方を読む。
    """
    out = [str(x).strip() for x in (pat.get("gas_sheets") or []) if str(x).strip()]
    if not out and str(pat.get("gas_sheet", "") or "").strip():
        out = [str(pat["gas_sheet"]).strip()]
    return out


def _patterns(cfg: dict):
    return cfg.get("patterns", []) or []


def _find(cfg: dict, name: str):
    for p in _patterns(cfg):
        if p.get("name") == name:
            return p
    return None


def _upsert_pattern(cfg: dict, pat: dict, old_name: str = ""):
    pats = _patterns(cfg)
    key = old_name or pat.get("name")
    for i, p in enumerate(pats):
        if p.get("name") == key:
            pats[i] = pat
            break
    else:
        pats.append(pat)
    cfg["patterns"] = pats
    return cfg


def _robot_picker(label: str, role_key: str, current: str, key: str):
    """使う共通ロボットを選ぶ。ふつうは既定のまま触らなくてよい。"""
    robots = common_robots.list_robots(supabase)
    default = common_robots.ROLES[role_key]["name"]
    cur = current or default
    opts = ["（使わない）"] + robots
    if cur and cur not in opts:
        opts.append(cur)
    picked = st.selectbox(label, opts, index=opts.index(cur) if cur in opts else 0, key=key)
    picked = "" if picked == "（使わない）" else picked
    if picked and picked not in robots:
        st.warning(f"⚠️ ロボット「{picked}」はまだ録画されていません。"
                   "「⚙️ その他設定」の🤖共通ロボットの登録で作ってください（1回でOK）。")
    return picked


@st.cache_data(ttl=120, show_spinner=False)
def _read_tab_cached(_gc, sheet_url: str, tab: str):
    """シートの中身を読む（少しの間おぼえておく）。画面を触るたびに読み直さないため。"""
    return sms_runner.read_tab(_gc, sheet_url, tab)


def _view_sheets(pat: dict):
    """②で中身を見せるシート。

    チェックのルールを作っていなくても見られるようにする
    （ルールを組むのが面倒で使えていない、という声があったため）。
    """
    out = []
    for r in (pat.get("checks", []) or []):
        t = str(r.get("シート", "") or "").strip()
        if t and t not in out:
            out.append(t)
    for t in _csv_sheets(pat) + list(pat.get("refresh_tabs", []) or []):
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


def _sheet_table(gc, pat: dict, tab: str, findings):
    """1シートぶんを表にする。直すべき行には印を付ける。

    戻り値：(表, 直すべき行の数, 見出し→列番号)
    """
    values = _read_tab_cached(gc, pat["sheet_url"], tab)
    if not values:
        return None, 0, {}
    headers = [str(h).strip() for h in values[0]]
    # 見出しが空だと表にできないので、埋める
    headers = [h if h else f"（列{i + 1}）" for i, h in enumerate(headers)]
    why = {}
    for f in findings or []:
        if str(f.get("シート", "")) != tab:
            continue
        why.setdefault(int(f.get("行", 0)), []).append(
            f"{f.get('列', '')}：{f.get('なぜ直すか', '')}")
    rows = []
    for i, row in enumerate(values[1:]):
        if not any(str(c).strip() for c in row):
            continue                       # 表の下の余白は出さない
        no = i + 2
        # 📌「直すところ」は中身が無くても必ず作る。
        #    行によって列があったり無かったりすると、列の並びが揺れて、
        #    書き戻すときに**別の列を上書きしてしまう**。
        rec = {"行": no, "🛠": ("要修正" if no in why else ""),
               "直すところ": " ／ ".join(why.get(no, []))}
        for j, h in enumerate(headers):
            rec[h] = row[j] if j < len(row) else ""
        rows.append(rec)
    # 見出し → スプレッドシートの列番号（1から数える）
    colmap = {h: j + 1 for j, h in enumerate(headers)}
    return pd.DataFrame(rows), len(why), colmap


def _sheet_editor(gc, pat: dict, pname: str, tab: str, findings, key: str):
    """1シートぶんを、直せる表として出す。戻り値：(直した数を書き戻せたか, 直すべき行の数)

    確認の小窓からも、実行画面の 2️⃣ からも、**同じここを通る**
    （別々に書くと、片方だけ直して食い違うため）。
    """
    try:
        df, ng, colmap = _sheet_table(gc, pat, tab, findings)
    except Exception as e:
        st.warning(f"「{tab}」を読めませんでした：{str(e)[:150]}")
        return False, 0
    if df is None or df.empty:
        st.caption("このシートは空です。")
        return False, 0
    if ng:
        st.error(f"🛠 このシートに、直すところが **{ng}行** あります。")
    st.caption(f"{len(df)}行（見出しを除く）／"
               "**表の中を直せます。行番号はスプレッドシートと同じです。**")
    locked = [c for c in ("行", "🛠", "直すところ") if c in df.columns]
    edited = st.data_editor(df, use_container_width=True, hide_index=True, height=360,
                            disabled=locked, num_rows="fixed", key=f"{key}_grid")
    cols = [c for c in df.columns if c not in locked]
    changes, shown = [], []
    for i in range(len(df)):
        rowno = int(df.iloc[i]["行"])
        for c in cols:
            old, new = str(df.iloc[i][c]), str(edited.iloc[i][c])
            if old != new:
                changes.append((rowno, colmap[c], new))
                shown.append({"行": rowno, "列": c, "前": old, "あと": new})
    if shown:
        st.caption("書き戻す内容（まだ反映していません）")
        st.dataframe(pd.DataFrame(shown), use_container_width=True, hide_index=True)
    saved = False
    if st.button(f"💾 直した{len(changes)}か所を書き戻す", disabled=not changes,
                 key=f"{key}_save"):
        try:
            n = sms_runner.write_cells(gc, pat["sheet_url"], tab, changes)
            _read_tab_cached.clear()
            st.session_state[f"sms_saved_{pname}"] = n
            saved = True
        except Exception as e:
            st.error(f"書き戻せませんでした：{str(e)[:200]}\n\n"
                     "そのスプレッドシートを、サービスアカウントに"
                     "**編集者**として共有しているか確認してください。")
    return saved, ng


@st.dialog("👀 中身を見て確認してください", width="large")
def _confirm_dialog(gc, pat: dict, pname: str, tabs, findings):
    """止まったところで、そのままシートを見て・直して・OKを出すための小窓。

    画面を行ったり来たりせずに済むようにするため。
    """
    st.caption("問題がなければ、いちばん下の **OK** を押してください。そのまま次へ進みます。")
    _tabs = [t for t in (tabs or []) if str(t).strip()]
    if not _tabs:
        st.info("見るシートが登録されていません（設定画面の 3️⃣）。")
        if st.button("閉じる", key=f"dlg_close_{pname}"):
            st.session_state.pop(f"sms_dlg_{pname}", None)
            st.rerun()
        return
    _cur = st.selectbox("見るシート", _tabs, key=f"dlg_tab_{pname}")
    _g = _gids_of(pat).get(_cur)
    st.markdown(f"[📝 このシートをスプレッドシートで開く]"
                f"({sms_runner.sheet_tab_url(pat['sheet_url'], _g) if _g else pat['sheet_url']})")
    _saved, _ng = _sheet_editor(gc, pat, pname, _cur, findings, f"dlg_{pname}_{_cur}")
    if _saved:
        st.rerun()
    _sv = st.session_state.pop(f"sms_saved_{pname}", None)
    if _sv:
        st.success(f"✅ {_sv}か所を書き戻しました。")
    st.markdown("---")
    b1, b2 = st.columns([1, 1])
    with b1:
        if st.button("✅ OK（このまま次へ進む）", type="primary", use_container_width=True,
                     key=f"dlg_ok_{pname}"):
            st.session_state[f"sms_ok_{pname}"] = True
            st.session_state[f"sms_auto_{pname}"] = True     # 続きを自動で走らせる
            st.session_state[f"sms_resume_{pname}"] = True   # ①からやり直さない
            st.session_state.pop(f"sms_dlg_{pname}", None)
            st.rerun()
    with b2:
        if st.button("⏸ あとにする（閉じる）", use_container_width=True,
                     key=f"dlg_no_{pname}"):
            st.session_state.pop(f"sms_dlg_{pname}", None)
            st.rerun()


def _prepare_csv(pat: dict, pname: str, src: str, enc: str, gc, sheet: str = ""):
    """CSVを用意する。うまくいかなければ例外を投げる。

    ボタンからも「ぜんぶ実行」からも、**同じここを通る**。
    別々に書くと、片方だけ直して食い違うため。
    戻り値：[(st の関数名, 文言), ...]（画面に出す言葉）
    """
    msgs = []
    # 📄 CSVはシートごとに分けて置く（同じ名前だと、先に作ったほうが消える）
    slot = sms_runner.sheet_slot(pname, sheet)
    if src == CSV_SOURCES[0]:
        if not str(pat.get("gas_url", "")).strip():
            raise RuntimeError("GASのウェブアプリURLが未設定です（設定画面の4️⃣）。")
        _p, gname, grows, extra = sms_runner.fetch_from_gas(
            pat["gas_url"], pat.get("gas_token", ""), sheet, slot,
            keep_drive=bool(pat.get("gas_keep_drive", True)), build=pat.get("gas_build", ""))
        msgs.append(("success", f"✅ GASから受け取りました：`{gname}`（{grows}件）"))
        dmsg = str((extra or {}).get("drive", "") or "")
        if dmsg:
            lv = "warning" if ("残せません" in dmsg or "失敗" in dmsg) else "caption"
            msgs.append((lv, f"📁 Driveの控え：{dmsg}"))
    elif src == CSV_SOURCES[1]:
        sa = _sa_json()
        if not sa:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        _p, dname, _h = sms_runner.fetch_from_drive(
            sa, pat.get("drive_root", ""), pat.get("drive_label", ""), slot)
        msgs.append(("success", f"✅ Driveから受け取りました：`{dname}`"))
    elif src == CSV_SOURCES[2]:
        ok, log = sms_runner.run_export_robot(pat["export_robot"], slot, url=pat["sheet_url"])
        if not sms_runner.adopt_downloaded(slot):
            raise RuntimeError("CSVが落ちてきませんでした。実行ログを確認してください。\n" + str(log)[-800:])
        msgs.append(("success", "✅ 受け取りました。"))
    else:
        _p, n, _h = sms_runner.export_csv(gc, pat["sheet_url"],
                                          sheet or pat.get("csv_tab", ""), slot, enc,
                                          pat.get("skip_empty_col", ""))
        msgs.append(("success", f"✅ {n}件のCSVを作りました。"))
    return msgs


def _run_all_sms(pat: dict, pname: str, gc, src: str, enc: str, do_push: bool,
                 stop_before_send: bool = False, resume: bool = False):
    """①更新 → ②チェック → ③CSV → ④一括送信 を通しで行う。

    ⚠️ 送ったSMSは取り消せない。だから **どこか1つでも駄目なら、そこで止める**。
       止まったときは「送っていない」で終わるようにする（送ってから気づいても遅い）。
    戻り値：[{"工程","結果","中身"}, ...]
    """
    steps = []

    def _add(name, ok, body, mark=""):
        # 「送るものが無い」は失敗ではない。赤で止めると、直すところを探させてしまう。
        steps.append({"工程": name, "結果": (mark or ("✅" if ok else "🛑")), "中身": body})
        return ok

    # --- ① シートを更新 ---
    #     ⚠️ 確認の小窓でOKを押したあとは「続き」から進める。
    #        ここでやり直すと、数分かかる更新をもう一度待たされる。
    tabs = pat.get("refresh_tabs", []) or []
    _prev_ref = st.session_state.get(f"sms_ref_{pname}")
    if resume and _prev_ref and _prev_ref.get("ok"):
        _add("① シートの更新", True, "さきほど更新できているので、やり直しません")
    elif pat.get("refresh_robot") and tabs:
        urls = sms_runner.tab_urls_for(pat["sheet_url"], tabs, _gids_of(pat))
        ok, log = sms_runner.run_refresh_robot(pat["refresh_robot"], pname,
                                               tabs=tabs, tab_urls=urls, url=pat["sheet_url"])
        st.session_state[f"sms_ref_{pname}"] = {
            "ok": ok, "log": log, "表": sms_runner.parse_refresh_log(log, tabs)}
        if not _add("① シートの更新", ok, f"{len(tabs)}枚"
                    if ok else "途中で止まりました（下の1️⃣にログがあります）"):
            return steps
    else:
        _add("① シートの更新", True, "この設定では行いません（手作業）")

    # --- ② 中身の確認 ---
    _rules = pat.get("checks", []) or []
    if _rules:
        try:
            findings, notes = sms_runner.check_rules(gc, pat["sheet_url"], _rules)
        except Exception as e:
            _add("② 中身の確認", False, f"シートを読めませんでした：{e}")
            return steps
        st.session_state[f"sms_find_{pname}"] = {"findings": findings, "notes": notes}
        if findings:
            _add("② 中身の確認", False,
                 f"ルールに引っかかった行が {len(findings)}件 あります。**送信せずに止めました**")
            return steps

    # 👀 目で見て確認するシートがあるなら、**人がOKを出すまで進めない**。
    #    ルール化できないものを、機械に判断させないための工程。
    _watch = pat.get("check_tabs", []) or []
    if _watch and not st.session_state.get(f"sms_ok_{pname}"):
        _add("② 中身の確認", False,
             f"「{'／'.join(_watch)}」を目で見て確認してください（下の 2️⃣ でOKを出せます）",
             mark="⏸")
        return steps
    _add("② 中身の確認", True,
         ("確認済み" if _watch else "確認するシートは登録されていません"))

    # --- ③④ シートのぶん繰り返す（1シート＝1回の送信） ---
    #     ⚠️「すでに送った宛先」の記録は**パターンでまとめて**見る。
    #        シートごとに分けると、同じ人に両方の文面が届いてしまう。
    _sheets = _csv_sheets(pat) or [""]
    days = int(pat.get("dedup_days", 0) or 0)
    _sent_any = False
    for _sh in _sheets:
        _tag = f"（{_sh}）" if _sh else ""
        _slot = sms_runner.sheet_slot(pname, _sh)
        try:
            msgs = _prepare_csv(pat, pname, src, enc, gc, _sh)
        except Exception as e:
            _add(f"③ CSVの用意{_tag}", False, str(e)[:300])
            return steps
        _add(f"③ CSVの用意{_tag}", True, "／".join(t for _l, t in msgs if _l == "success"))

        got = sms_runner.today_csv(_slot)
        if not got:
            _add(f"④ 一括送信{_tag}", False, "今日のCSVが見つかりません")
            return steps

        dup = sms_runner.find_already_sent(pname, sms_runner.csv_dest_keys(got, enc), days)
        if dup:
            n_drop, n_left = sms_runner.drop_already_sent(_slot, enc, days, sent_pattern=pname)
            _add(f"　 二重送信の除外{_tag}", True,
                 f"すでに送った {n_drop}件を外しました（残り {n_left}件）")
            got = sms_runner.today_csv(_slot)
        keys = sms_runner.csv_dest_keys(got, enc)
        if not keys:
            _add(f"④ 一括送信{_tag}", True,
                 "送る宛先が0件でした（このCSVの分はすべて送信済み）", mark="⏹")
            continue

        # 🛑 送るSMSは取り消せないので、「送ります」の確認を取っていなければ、ここで止める。
        if stop_before_send:
            _add(f"④ 一括送信{_tag}", False,
                 f"{len(keys)}件を送る用意ができました。**送信の確認を入れてください**", mark="⏸")
            continue

        ok, log = sms_runner.run_send_robot(pat["send_robot"], _slot, got,
                                            allow_errors=bool(pat.get("allow_errors")))
        if ok:
            result, note = "送信済み", ""
        elif sms_runner.submit_reached(log):
            result, note = "要確認（送ったかもしれない）", "途中で止まりました"
        else:
            result, note = "送信できず", "送信の手前で止まりました"
        # 🚫 プッシュプロに弾かれた宛先は、送られていない。記録に入れない
        #    （入れてしまうと、直したあとに送り直せなくなる）
        _drop = sms_runner.dropped_dests(log)
        _keys_sent = [(n, k) for n, k in keys if k not in _drop]
        sms_runner.record_sent(pname, _keys_sent, result, note)
        st.session_state[f"sms_sent_{pname}"] = {"ok": ok, "log": log,
                                                 "result": result, "n": len(keys)}
        _sent_any = _sent_any or ok
        _why = sms_runner.stop_reason(log) if not ok else ""
        _extra = (f"／弾かれて送られなかった {len(_drop)}件" if _drop else "")
        if not _add(f"④ 一括送信{_tag}", ok,
                    f"{len(_keys_sent)}件：{result}" + _extra + (f"／{_why}" if _why else "")):
            return steps

    # --- ⑤ Salesforceへ投入（頼まれたときだけ） ---
    if do_push:
        out = []
        for ld in (pat.get("loads", []) or []):
            r = sf_ui.push_sheet(gc, pat["sheet_url"], str(ld.get("シート", "")),
                                 str(ld.get("オブジェクト", "")), str(ld.get("照合キー", "")),
                                 ld.get("マッピング", {}) or {}, limit=0)
            out.append({"シート": str(ld.get("シート", "")), "結果": r["結果"],
                        "成功": r["ok"], "失敗": r["ng"],
                        "_errors": r["errors"], "_obj": r["オブジェクト"]})
        st.session_state[f"sms_push_{pname}"] = out
        _add("⑤ Salesforceへ投入", all(not r["失敗"] for r in out),
             "／".join(f"{r['シート']}：{r['結果']}" for r in out) or "投入の設定がありません")
    return steps


cfg = _load_settings()
gc = _get_gspread_client()

st.session_state.setdefault("sms_view", "list")
st.session_state.setdefault("sms_pattern", "")

if not gc:
    st.warning("🔑 接続キー **GOOGLE_SERVICE_ACCOUNT_JSON** が未設定です。"
               "シートの読み取り・CSVの受け取りができません（管理者に設定を依頼してください）。")


# ==========================================
# 📋 画面1：パターン一覧
# ==========================================
if st.session_state.sms_view == "list":
    ch.guide("operate",
             "SMSは<b>パターンごと</b>に登録するよ。録画は「⚙️ その他設定」で1回だけ。"
             "ここでは<b>シート名を選ぶだけ</b>で動くようにしてあるね。")

    top1, top2 = st.columns([1, 3])
    with top1:
        if st.button("＋ パターンを追加", type="primary", use_container_width=True):
            st.session_state.sms_view = "edit"
            st.session_state.sms_pattern = ""
            st.rerun()
    with top2:
        _have = common_robots.list_robots(supabase)
        _need = [r["name"] for r in common_robots.ROLES.values() if r["name"] not in _have]
        if _need:
            st.warning("まだ録画していない共通ロボットがあります：" + "、".join(_need))
        st.page_link("pages/8_⚙️_その他設定.py", label="🤖 共通ロボットの登録へ")

    pats = _patterns(cfg)
    if not pats:
        st.info("まだパターンがありません。「＋ パターンを追加」から、最初の1つを登録しましょう。")
    for p in pats:
        with st.container(border=True):
            a, b, d = st.columns([3, 3, 2])
            with a:
                st.markdown(f"#### 📱 {p.get('name', '(名前なし)')}")
                st.caption(p.get("memo", "") or "　")
            with b:
                ready = bool(p.get("sheet_url")) and bool(p.get("send_robot"))
                st.markdown("<span class='status-active'>準備OK</span>" if ready
                            else "<span class='status-inactive'>設定が足りません</span>",
                            unsafe_allow_html=True)
                _got = sms_runner.today_csv(p.get("name", ""))
                _made = sms_runner.csv_made_at(p.get("name", ""))
                st.caption(f"今日のCSV：{'あり' if _got else 'まだ'}"
                           + (f"（用意：{_made}）" if _made else ""))
                _sent = sms_runner.load_sent(p.get("name", ""))
                if _sent:
                    st.caption(f"送信の記録：{len(_sent)}件ぶん")
            with d:
                # ⭐ 押した瞬間から、最後まで通す。途中で人の確認が要るときだけ、
                #    小窓（ポップアップ）でシートを出して、OKで先へ進む。
                if st.button("▶ 全部実行", key=f"all_{p.get('name')}", use_container_width=True,
                             type="primary", disabled=not ready,
                             help="更新 → 確認 → CSV → 一括送信 まで、続けて実行します。"):
                    st.session_state.sms_view = "run"
                    st.session_state.sms_pattern = p.get("name", "")
                    st.session_state[f"sms_auto_{p.get('name')}"] = True
                    st.rerun()
                if st.button("🔧 個別実行", key=f"go_{p.get('name')}", use_container_width=True,
                             disabled=not ready,
                             help="工程ごとに、自分で押して進めます。"):
                    st.session_state.sms_view = "run"
                    st.session_state.sms_pattern = p.get("name", "")
                    st.session_state.pop(f"sms_auto_{p.get('name')}", None)
                    st.rerun()
                if st.button("⚙️ 設定を直す", key=f"ed_{p.get('name')}", use_container_width=True):
                    st.session_state.sms_view = "edit"
                    st.session_state.sms_pattern = p.get("name", "")
                    st.rerun()

    st.divider()
    st.caption("💻 シートの更新と一括送信はブラウザを開くため、**担当者のPCで開いているとき**だけ実行できます。")


# ==========================================
# ⚙️ 画面2：パターンの設定
# ==========================================
elif st.session_state.sms_view == "edit":
    old_name = st.session_state.sms_pattern
    pat = _find(cfg, old_name) or {
        "name": "", "memo": "", "sheet_url": "",
        "refresh_tabs": [], "refresh_robot": common_robots.ROLES["refresh"]["name"],
        "checks": [],
        "csv_source": CSV_SOURCES[0],
        "gas_url": "", "gas_token": "", "gas_sheets": [], "gas_build": "",
        "check_tabs": [], "auto_send": False, "auto_load": False, "allow_errors": False,
        "gas_keep_drive": True,
        "drive_root": sms_runner.DRIVE_SMS_ROOT, "drive_label": "",
        "export_robot": common_robots.ROLES["export"]["name"],
        "csv_tab": "", "csv_encoding": "Shift_JIS", "skip_empty_col": "",
        "send_robot": common_robots.ROLES["send"]["name"],
        "dedup_days": 0, "loads": [],
    }

    if st.button("⬅ 一覧に戻る"):
        st.session_state.sms_view = "list"
        st.session_state.pop("sms_loads_of", None)
        st.rerun()

    st.markdown(f"### ⚙️ {'パターンを追加' if not old_name else f'「{old_name}」の設定'}")
    st.caption("🎬 録画（手順の登録）は「⚙️ その他設定」の🤖共通ロボットの登録でまとめて行います。"
               "ここでは**どのシートを使うか**だけ決めます。")

    # --- 1. 名前とスプシ ---
    with st.container(border=True):
        theme.section_title("1️⃣", "パターンの名前と、つなぐスプレッドシート")
        name = st.text_input("パターンの名前", value=pat.get("name", ""),
                             placeholder="例：長期不在1回目")
        memo = st.text_input("メモ（何を送るパターンか）", value=pat.get("memo", ""))
        sheet_url = st.text_input("スプレッドシートのURL", value=pat.get("sheet_url", ""),
                                  placeholder="https://docs.google.com/spreadsheets/d/...")
        st.caption("※ サービスアカウントのメールアドレスを、このスプシの**閲覧者**に追加してください。")

    tabs = []
    if gc and sheet_url.strip():
        try:
            tabs = _tab_names(gc, sheet_url.strip())
        except Exception as e:
            st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")

    # --- 2. リフレッシュ ---
    with st.container(border=True):
        theme.section_title("2️⃣", "SFコネクタで更新するシート（複数えらべます）")
        st.caption("選んだシートを、**ブラウザを1回だけ開いて**上から順に更新します。"
                   "開くのは、上の1️⃣で登録した**このパターンのスプレッドシート**です"
                   "（録画したときのスプシではありません）。")
        if tabs:
            _cur = [t for t in (pat.get("refresh_tabs", []) or []) if t in tabs]
            refresh_tabs = st.multiselect("更新するシート", tabs, default=_cur)
        else:
            refresh_tabs = [t.strip() for t in
                            st.text_input("更新するシート（カンマ区切り）",
                                          value="、".join(pat.get("refresh_tabs", []) or [])
                                          ).replace("、", ",").split(",") if t.strip()]
            st.caption("※ スプシURLを入れると、シート名をプルダウンで選べます。")
        refresh_robot = _robot_picker("使うロボット", "refresh",
                                      pat.get("refresh_robot", ""), "sms_refresh_sel")

    # --- 3. チェックのルール ---
    with st.container(border=True):
        theme.section_title("3️⃣", "送る前に、目で見て確認するシート")
        st.caption("ここに登録したシートは、実行の 2️⃣ で**中身がそのまま出ます**。"
                   "見て、必要ならその場で直して、**OKを出すと次へ進みます**。")
        _opts_w = list(dict.fromkeys(list(tabs) + list(pat.get("check_tabs", []) or [])))
        check_tabs = st.multiselect(
            "目で見て確認するシート（複数えらべます）", _opts_w,
            default=[x for x in (pat.get("check_tabs", []) or []) if x in _opts_w],
            help="ルールは要りません。中身を見て、人が判断するためのものです。")
        if not check_tabs:
            st.info("👉 **空のままでもかまいません。** その場合、実行時は"
                    "**確認の画面を出さずに、そのまま次へ進みます。**")

        # 🔍 ルールは「毎回かならず機械的に見たいところ」だけ。あくまで付け足し。
        with st.expander("🔍（任意）決まったルールで自動チェックもする"):
            st.caption("ここに書いたルールに引っかかった行は、"
                       "**0件になるまで送信に進めません**。要らなければ空のままでOKです。")
            with st.expander("📖 ルールの意味"):
                for k, v in sms_runner.RULES.items():
                    st.markdown(f"- **{k}**：{v}")

            # ⚡ 定番のチェックは、押すだけで足せるようにする。
            #    1件ずつ列名を打ち込むのが面倒で、結局使われていなかったため。
            _csvs = _csv_sheets(pat)
            if _csvs:
                _q1, _q2 = st.columns([2, 1])
                with _q1:
                    _qsheet = st.selectbox("よく使うチェックを足す（対象のシート）", _csvs,
                                           key="sms_quicksheet")
                with _q2:
                    st.caption("　")
                    if st.button("⚡ 携帯番号の定番3つを足す", use_container_width=True,
                                 key="sms_quickadd"):
                        _hdr = ""
                        try:
                            _v = _read_tab_cached(gc, pat["sheet_url"], _qsheet) if gc else []
                            _hdr = str(_v[0][0]).strip() if _v and _v[0] else ""
                        except Exception:
                            _hdr = ""
                        _hdr = _hdr or "携帯番号(ハイフンなし)"
                        _now = list(pat.get("checks", []) or [])
                        for _rule, _memo in (("空はNG", "番号が入っていません"),
                                             ("電話番号の形", "携帯番号の形になっていません"),
                                             ("重複はNG", "同じ番号が二重に入っています")):
                            if not any(str(x.get("シート")) == _qsheet
                                       and str(x.get("列")) == _hdr
                                       and str(x.get("ルール")) == _rule for x in _now):
                                _now.append({"シート": _qsheet, "列": _hdr, "ルール": _rule,
                                             "値": "", "メモ": _memo})
                        pat["checks"] = _now
                        st.session_state.pop("sms_checks", None)
                        st.info(f"「{_hdr}」に3つ足しました。**下の「💾 このパターンを保存」**を"
                                "押すまで保存されません。")
                st.caption("※ CSVの1列目（携帯番号）を見て足します。列名が違うときは、下の表で直せます。")

            cdf = pd.DataFrame(pat.get("checks", []) or [],
                               columns=["シート", "列", "ルール", "値", "メモ"])
            if cdf.empty:
                # 📌 空の行に "" を入れると、選択肢に無いので **None と表示される**。
                #    最初から使えるシート名を入れておく。
                cdf = pd.DataFrame([{"シート": (tabs[0] if tabs else ""), "列": "",
                                     "ルール": "空はNG", "値": "", "メモ": ""}])
            _sheet_opts = list(dict.fromkeys(
                [str(x) for x in tabs] + [str(x.get("シート", "")) for x in (pat.get("checks") or [])
                                          if str(x.get("シート", "")).strip()]))
            checks_edited = st.data_editor(
                cdf, num_rows="dynamic", use_container_width=True, key="sms_checks",
                column_config={
                    "シート": (st.column_config.SelectboxColumn(options=_sheet_opts, required=False)
                               if _sheet_opts else st.column_config.TextColumn()),
                    "列": st.column_config.TextColumn(
                        help="そのシートの見出し（1行目）と同じ言葉を、そのまま書いてください。"),
                    "ルール": st.column_config.SelectboxColumn(options=list(sms_runner.RULES.keys())),
                    "値": st.column_config.TextColumn(help="「この文字を含む」などで使う指定。／で区切って複数。"),
                    "メモ": st.column_config.TextColumn(help="担当者に出す一言（例：番号の抜けを埋めてください）"),
                })

            # 🩺 列名がそのシートに無いと、実行時に「列がありません」で空振りする。先に言う。
            if gc:
                _bad = []
                for _r in checks_edited.fillna("").to_dict("records"):
                    _sh, _col = str(_r.get("シート", "")).strip(), str(_r.get("列", "")).strip()
                    if not (_sh and _col):
                        continue
                    try:
                        _v = _read_tab_cached(gc, pat["sheet_url"], _sh)
                        _hs = [str(h).strip() for h in (_v[0] if _v else [])]
                    except Exception:
                        continue
                    if _col not in _hs:
                        _bad.append(f"「{_sh}」に列「{_col}」がありません"
                                    f"（あるのは：{'／'.join([h for h in _hs if h][:8])}）")
                for _m in _bad:
                    st.warning(f"⚠️ {_m}")

    # --- 4. CSVの用意のしかた ---
    with st.container(border=True):
        theme.section_title("4️⃣", "プッシュプロに入れるCSVの用意")
        _cur_src = pat.get("csv_source", CSV_SOURCES[0])
        csv_source = st.radio("どうやって用意しますか", CSV_SOURCES,
                              index=CSV_SOURCES.index(_cur_src) if _cur_src in CSV_SOURCES else 0)
        st.caption("💡 電話番号の頭の0や文字コード（Shift_JIS）の整形は、すでにスプシのGASがやっています。"
                   "同じ整形をアプリでもう一度書くと、片方だけ直して食い違うので、"
                   "できあがったCSVをそのまま使うのがいちばん安全です。")
        st.info(f"📁 置き場所：`取り込みファイル/SMS送信用/{name.strip() or '（パターン名）'}/"
                f"{sms_runner.CSV_NAME}`　"
                "**毎回この同じ名前で上書き**します。だから録画で選んだファイルのまま動きます"
                "（日付つきの控えは `履歴` フォルダに残ります）。")

        gas_url = pat.get("gas_url", "")
        gas_token = pat.get("gas_token", "")
        # 📄 CSVにするシートは複数持てる（1シート＝1回の送信）。
        #    旧い設定（gas_sheet が1つ）も読めるようにしておく。
        gas_sheets = _csv_sheets(pat)
        gas_build = pat.get("gas_build", "")
        gas_keep_drive = bool(pat.get("gas_keep_drive", True))
        drive_root = pat.get("drive_root", sms_runner.DRIVE_SMS_ROOT)
        drive_label = pat.get("drive_label", "")
        export_robot = pat.get("export_robot", "")
        csv_tab = pat.get("csv_tab", "")
        csv_encoding = pat.get("csv_encoding", "Shift_JIS")
        skip_empty_col = pat.get("skip_empty_col", "")

        if csv_source == CSV_SOURCES[0]:
            # 🔗 GASを「ウェブアプリ」としてデプロイしておけば、URLを叩くだけでCSVが返る。
            #    録画も、Driveの共有設定も要らない。CSVを作るのはこれまでどおりGAS。
            st.caption("スプシの Apps Script を **ウェブアプリとしてデプロイ**しておけば、"
                       "URLを叩くだけで、サイドバーのボタンとまったく同じCSVが返ってきます。"
                       "録画も、Driveの共有設定も要りません。")
            gas_url = st.text_input("GASのウェブアプリURL", value=gas_url,
                                    placeholder="https://script.google.com/macros/s/AKfy.../exec")
            # 🔑 合言葉は、この欄に入っているものが正（表示だけだと保存前に変わってしまう）
            # 名札を付けた欄は、いちど空で作られると空を覚えてしまうので、中身を先に用意する
            _tok_key = "sms_tok"
            # ⚠️ 欄の中身は、その欄が作られる**前**にしか入れ替えられない（Streamlitの決まり）。
            _regen_key = _tok_key + "__regen"
            _saved_token = str(gas_token or "").strip()
            if st.session_state.pop(_regen_key, False):
                import secrets as _secrets
                st.session_state[_tok_key] = _secrets.token_urlsafe(24)
            elif not str(st.session_state.get(_tok_key, "") or "").strip():
                if not _saved_token:
                    # ⚠️ URLを入れる前にコードをコピーする人がいる。先に用意しておく。
                    import secrets as _secrets
                    _saved_token = _secrets.token_urlsafe(24)      # 🎲 アプリが用意する
                if _saved_token:
                    st.session_state[_tok_key] = _saved_token
            _tk1, _tk2 = st.columns([3, 1])
            with _tk1:
                gas_token = st.text_input(
                    "合言葉（スクリプトの API_TOKEN と、1文字違わず同じにする）", key=_tok_key)
            with _tk2:
                st.write("")
                if st.button("🎲 作り直す", key="sms_tokgen", use_container_width=True,
                             help="新しい合言葉を作ります。作り直したら、スクリプト側も貼り替えてください。"):
                    st.session_state[_regen_key] = True
                    st.rerun()
            if gas_url.strip():
                st.caption("👆 この文字列を Apps Script の "
                           "`const API_TOKEN = 'ここに長い合言葉を書く';` の "
                           "**`ここに長い合言葉を書く` と入れ替えて**ください（`'` は消さない）。"
                           "そのあと **デプロイ → デプロイを管理 → 鉛筆 → 新バージョン → デプロイ**。")
                st.caption("💡 すでにスクリプトに別の合言葉を書いてあるなら、"
                           "**その文字列をこの欄に貼り替えて**ください（**両方が同じ**であることだけが大事です）。")
                st.warning("⚠️ 入力しただけでは保存されません。"
                           "いちばん下の **「💾 このパターンを保存」** を押してください。")

            # 📜 貼り付けるコードを、合言葉を埋めた状態でここに出す
            _gcode = sms_runner.gas_template("エンカンAI_連携WebAPI.gs", gas_token)
            if _gcode:
                with st.expander("📜 スプシに貼り付けるコード（合言葉は入れてあります）",
                                 expanded=not str(pat.get("gas_url", "")).strip()):
                    st.markdown(
                        "1. スプレッドシート → **拡張機能 → Apps Script**\n"
                        "2. いまのコードの**いちばん下**に、下の内容を**まるごと**貼り付ける\n"
                        "   （いまある関数は消さないこと）\n"
                        "3. 保存して、**デプロイ → 新しいデプロイ → ウェブアプリ**\n"
                        "   （次のユーザーとして実行：**自分** ／ アクセスできるユーザー：**全員**）\n"
                        "4. 出てきた `.../exec` のURLを、上の欄に貼る")
                    st.warning("⚠️ **合言葉の1行だけではありません。** "
                               "`function doGet` を含めて、下の内容を全部貼ってください。")
                    st.success("✅ **このコードは、どのスプレッドシートでも中身は同じ**です。"
                               "書き替えるところはありません（合言葉は入れてあります）。"
                               "「どの処理で作るか」「どのシートをCSVにするか」は、"
                               "下のプルダウンで選びます。")
                    st.error("🧹 **前に貼った古い版が残っていたら、必ず消してください。** 同じ名前（`API_TOKEN` や `doGet`）が2回出てくると、スクリプト全体が動かなくなります。新しい版だけにしてから、**新バージョンでデプロイ**してください。")
                    st.caption("⚠️ 作成の処理が `ui.alert(...)` を使っていると、"
                               "人がいない状態では動きません。"
                               "その場合は直し方をメッセージで出すので、そのとおりに直してください。")
                    st.caption("💡 右上のコピーボタンで、まるごとコピーできます。"
                               "**合言葉を作り直したら、ここも貼り直してください。**")
                    st.markdown("**まずここだけ確認**：貼ったコードの中に、この1行がそのまま入っていますか。")
                    st.code("const API_TOKEN = '" + str(gas_token).strip() + "';",
                            language="javascript")
                    st.caption("Apps Script で `Ctrl + F` → `API_TOKEN` で探して、"
                               "**`ここに長い合言葉を書く` のままなら、それが原因**です。"
                               "上の1行に置き換えて、**新バージョンでデプロイ**してください。")
                    st.code(_gcode, language="javascript")
                    st.download_button("⬇️ ファイルで受け取る", data=_gcode.encode("utf-8"),
                                       file_name="エンカンAI_連携WebAPI.gs", mime="text/plain",
                                       key="sms_gasdl")
            g1, _g2 = st.columns([1, 2])
            with g1:
                if st.button("🔌 つないで中身を見る", use_container_width=True, type="primary"):
                    if not gas_url.strip():
                        st.warning("URLを入れてください。")
                    else:
                        _ok, _data = sms_runner.gas_inspect(gas_url.strip(), gas_token.strip())
                        if _ok:
                            st.session_state["sms_gas_info"] = _data
                            st.success(f"✅ つながりました（{(_data or {}).get('name','')}）。"
                                       "下で、使う処理とシートを選んでください。")
                        else:
                            st.session_state.pop("sms_gas_info", None)
                            st.error(f"❌ {_data}")
                            if "API_TOKEN が未設定" in str(_data):
                                st.info(_REDEPLOY_HINT)
                            elif "2回宣言" in str(_data):
                                st.info("👉 古い版のかたまり（同じ名前の `const` や `doGet`）を"
                                        "消してから、**新バージョンでデプロイ**してください。")
            with _g2:
                st.caption("👆 押すと、**このスプシにある処理とシートを読み取って**、"
                           "下のプルダウンに並べます。コードを読む必要はありません。")

            _info = st.session_state.get("sms_gas_info") or {}
            _fns = _info.get("functions") or []
            _shs = _info.get("sheets") or []

            # 🛠 CSVを作る前に走らせる「作成」の処理（スプシごとに名前が違う）
            _cur_build = [x for x in str(gas_build or "").split(",") if x.strip()]
            if _fns:
                gas_build = ",".join(st.multiselect(
                    "CSVを作る前に走らせる処理（メニューの「作成」にあたるもの）",
                    _fns, default=[x for x in _cur_build if x in _fns],
                    help="走らせないと、前回の中身のままCSVになります。"
                         "ふつうは1つだけ選びます。"))
            else:
                gas_build = st.text_input(
                    "CSVを作る前に走らせる処理（関数名・カンマ区切り）", value=gas_build,
                    placeholder="例：extractLifelineContacts_FINAL",
                    help="上の「🔌 つないで中身を見る」を押すと、選ぶだけになります。")

            # 📄 シート名は、GASにつなぐ前でも 1️⃣ のスプシURLから読めるので、最初から選べるようにする
            if not _shs:
                _shs = list(tabs)
            if _shs:
                _opts = _shs + [x for x in gas_sheets if x not in _shs]
                gas_sheets = st.multiselect(
                    "CSVにするシート（複数えらべます）", _opts,
                    default=[x for x in gas_sheets if x in _opts],
                    help="1シートにつき1回、プッシュプロに入れて送信します"
                         "（例：1回目CSV と 2回目CSV）。")
            else:
                gas_sheets = [x.strip() for x in
                              st.text_input("CSVにするシート（複数はカンマ区切り）",
                                            value="，".join(gas_sheets),
                                            placeholder="例：1回目CSV，2回目CSV",
                                            help="1️⃣にスプレッドシートのURLを入れると、"
                                                 "プルダウンで選べるようになります。"
                                            ).replace("，", ",").split(",") if x.strip()]
            if len(gas_sheets) > 1:
                st.info(f"📄 **{len(gas_sheets)}回に分けて送信します**"
                        f"（{' → '.join(gas_sheets)}）。"
                        "「すでに送った宛先」の記録は**パターンでまとめて**持つので、"
                        "同じ人に両方のシートから届くことはありません。")
            # 📁 Driveへの控えは、そのスプシに保存先が書いてある場合だけ効く。
            #    書いていないスプシでチェックしても何も起きないので、そう分かるようにする。
            _dready = _info.get("driveReady")
            _dsheets = _info.get("driveSheets") or []
            gas_keep_drive = st.checkbox("これまでどおり Drive にも控えを残す", value=gas_keep_drive)
            if gas_keep_drive:
                if not _info:
                    st.caption("💡 Driveに残せるかどうかは、上の「🔌 つないで中身を見る」で分かります。")
                elif not _dready:
                    st.warning("⚠️ このスプシは **Driveへの控えに対応していません**"
                               "（保存先を決める処理がありません）。"
                               "チェックしても残りません。**CSVはアプリに届くので送信はできます。**")
                elif _dsheets and [x for x in gas_sheets if x not in _dsheets]:
                    _ng = [x for x in gas_sheets if x not in _dsheets]
                    st.warning(f"⚠️ シート「{'／'.join(_ng)}」には、Driveの保存先が決められていません。"
                               f"控えを残せるのは：{'／'.join(_dsheets)}。"
                               "**CSVはアプリに届くので送信はできます。**")
                else:
                    st.caption("📁 これまでと同じ場所（`SMS送信用/年/月/日`）に控えを残します。")
            st.caption("💡 控えを残さなくても、アプリ側の `履歴` フォルダに"
                       "**日付つきで30日ぶん**残ります（送った中身の確認用）。")
        elif csv_source == CSV_SOURCES[1]:
            drive_root = st.text_input("DriveのSMS送信用フォルダID", value=drive_root)
            _labels = sms_runner.KNOWN_LABELS + ([drive_label] if drive_label and
                                                 drive_label not in sms_runner.KNOWN_LABELS else [])
            drive_label = st.selectbox("ファイル名の頭（GASの label）", _labels + ["（自分で入力）"],
                                       index=_labels.index(drive_label) if drive_label in _labels else 0)
            if drive_label == "（自分で入力）":
                drive_label = st.text_input("ファイル名の頭を入力", value=pat.get("drive_label", ""))
            st.caption("GASは `SMS送信用/yyyy/M月/d/<この頭>_yyyyMMdd_HHmm.csv` に置きます。"
                       "その日のフォルダから、この頭で始まるいちばん新しいファイルを取ってきます。")
            st.caption("※ このDriveフォルダを、サービスアカウントに**閲覧者**として共有してください。")
        elif csv_source == CSV_SOURCES[2]:
            export_robot = _robot_picker("使うロボット（GASのCSV書き出し）", "export",
                                         export_robot, "sms_export_sel")
            st.caption("※ サイドバーはスプシの中の小さな画面（iframe）なので、録画がうまく"
                       "いかないことがあります。GASのURLを叩く方法のほうが確実です。")
        else:
            e1, e2 = st.columns(2)
            with e1:
                if tabs:
                    csv_tab = st.selectbox("CSVにするシート", tabs,
                                           index=tabs.index(csv_tab) if csv_tab in tabs else 0)
                else:
                    csv_tab = st.text_input("CSVにするシート", value=csv_tab)
            with e2:
                enc_opts = list(sms_runner.ENCODINGS.keys())
                csv_encoding = st.selectbox("文字コード", enc_opts,
                                            index=enc_opts.index(csv_encoding)
                                            if csv_encoding in enc_opts else 0)
            skip_empty_col = st.text_input("この列が空の行は送らない（任意）", value=skip_empty_col,
                                           placeholder="例：連絡先")
            st.warning("⚠️ この方法は、GASがやっている整形（電話番号の頭の0を足すなど）を通りません。"
                       "GASと同じ結果になるか、最初の1回は必ず見比べてください。")

    # --- 5. 送信と、二重送信の防止 ---
    with st.container(border=True):
        theme.section_title("5️⃣", "一括送信と、二重送信の防止")
        # ⭐「▶ 全部実行」がどこまで進むかを、ここで決めておける。
        #    毎回チェックを入れ直すのが手間、という声があったため。
        #    ⚠️ 送信は取り消せないので、既定はOFF。入れるのは担当者の判断。
        # ⭐ SMS非対応の番号など、**直しようがない行**が混ざることがある。
        #    その1件のために全員に送れないのは業務が回らない。
        allow_errors = st.checkbox(
            "**取り込みで弾かれた行があっても、送れる分は送る**",
            value=bool(pat.get("allow_errors", False)), key="sms_allowerr",
            help="プッシュプロの「送信対象のSMSを送信する」と同じです。")
        if allow_errors:
            st.caption("💡 弾かれた宛先は**送られません**。"
                       "その番号は「送信済み」に入れないので、直したあとに送り直せます。"
                       "何が弾かれたかは、実行画面に出ます。")
        else:
            st.caption("⚠️ いまは、弾かれた行が1件でもあると**送らずに止まります**。"
                       "SMS非対応の番号が混ざるなら、上をONにしてください。")

        auto_send = st.checkbox(
            "**「▶ 全部実行」で、一括送信まで自動で行う**",
            value=bool(pat.get("auto_send", False)), key="sms_autosend",
            help="OFFのときは、CSVを用意したところで止まります（そこから手で送れます）。")
        if auto_send:
            st.warning("⚠️ **一覧の「▶ 全部実行」を押しただけで、SMSが送信されます。**"
                       "送ったSMSは取り消せません。"
                       "（送る前のチェック・二重送信の除外・エラー件数の確認は、"
                       "これまでどおり働きます）")
        else:
            st.caption("💡 いまは、CSVを用意したところで止まります。"
                       "毎回チェックを入れるのが手間なら、ここをONにしてください。")
        send_robot = _robot_picker("使うロボット（プッシュプロ）", "send",
                                   pat.get("send_robot", ""), "sms_send_sel")
        st.caption("プッシュプロは**一括送信**なので、送ってしまった分は取り消せません。"
                   "そこで、送った宛先（CSVの1列目）を記録しておき、次のCSVから外せるようにします。")
        _dd = int(pat.get("dedup_days", 0) or 0)
        dedup_days = st.number_input(
            "この日数以内に送った宛先は、二重送信とみなす",
            min_value=0, max_value=365, value=_dd,
            help="暦の日で数えます（時刻ではありません）。")
        st.caption({
            0: "**0：一度でも送った相手には、二度と送りません。**",
            1: "**1：今日送った相手だけ止めます。＝翌日には、同じ相手に送れます。**",
        }.get(int(dedup_days),
              f"**{int(dedup_days)}：今日を含む{int(dedup_days)}日ぶんを止めます。"
              f"＝{int(dedup_days)}日後には、同じ相手に送れます。**"))

    # --- 6. 送ったあとに Salesforce へ入れる（データローダー相当） ---
    #     ここも「全部実行でどこまで行くか」を設定で決められる。
    #     送りっぱなしにせず、「送った」ことをSalesforceに残すための工程。
    if st.session_state.get("sms_loads_of") != (old_name or "＿新規"):
        st.session_state["sms_loads"] = json.loads(json.dumps(pat.get("loads", []) or []))
        st.session_state["sms_loads_of"] = old_name or "＿新規"
    loads = st.session_state["sms_loads"]
    with st.container(border=True):
        theme.section_title("6️⃣", "送ったあとに Salesforce へ入れる（任意）")
        st.caption("SMSを送ったことをSalesforceに残す工程です。"
                   "データローダー自動化と同じしくみで、**APIで直接**入れます"
                   "（Data Loader を開く必要はありません）。要らなければ空のままでOK。")
        for i, ld in enumerate(loads):
            with st.container(border=True):
                h1, h2 = st.columns([5, 1])
                with h1:
                    st.markdown(f"**投入 {i + 1}**")
                with h2:
                    if st.button("🗑", key=f"sms_del_{i}", help="この投入を消す"):
                        loads.pop(i)
                        st.rerun()
                sf_ui.load_editor(gc, sheet_url.strip(), tabs, ld, f"sms_load_{i}")
        auto_load = st.checkbox(
            "**「▶ 全部実行」で、Salesforceへの投入（全件）まで自動で行う**",
            value=bool(pat.get("auto_load", False)), key="sms_autoload",
            help="UPSERTなので、既存の値が上書きされます。")
        if auto_load:
            st.warning("⚠️ 送信のあと、**確認なしで全件を Salesforce に反映します**。")
        if st.button("＋ 投入を追加", key="sms_addload"):
            loads.append({"シート": (tabs[0] if tabs else ""), "オブジェクト": "Opportunity",
                          "照合キー": "Id", "マッピング": {}})
            st.rerun()

    st.divider()
    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button("💾 このパターンを保存", type="primary", use_container_width=True):
            if not name.strip():
                st.warning("パターンの名前を入れてください。")
            else:
                checks = [r for r in checks_edited.fillna("").to_dict("records")
                          if str(r.get("シート", "")).strip() and str(r.get("列", "")).strip()]
                pat_new = {"name": name.strip(), "memo": memo.strip(),
                           "sheet_url": sheet_url.strip(),
                           "refresh_tabs": list(refresh_tabs), "refresh_robot": refresh_robot,
                           "checks": checks, "csv_source": csv_source,
                           "gas_url": str(gas_url).strip(),
                           "gas_token": str(gas_token).strip(),
                           "gas_sheets": [str(x).strip() for x in gas_sheets if str(x).strip()],
                           "check_tabs": [str(x).strip() for x in check_tabs if str(x).strip()],
                           "auto_send": bool(auto_send), "auto_load": bool(auto_load),
                           "allow_errors": bool(allow_errors),
                           "gas_build": str(gas_build).strip(),
                           "gas_keep_drive": bool(gas_keep_drive),
                           "drive_root": str(drive_root).strip(),
                           "drive_label": str(drive_label).strip(),
                           "export_robot": export_robot,
                           "csv_tab": csv_tab, "csv_encoding": csv_encoding,
                           "skip_empty_col": skip_empty_col.strip(),
                           "send_robot": send_robot, "dedup_days": int(dedup_days),
                           "loads": [ld for ld in loads if str(ld.get("シート", "")).strip()]}
                _save_settings(_upsert_pattern(cfg, pat_new, old_name))
                st.session_state.pop("sms_loads_of", None)
                st.session_state.sms_view = "list"
                st.success("保存しました。")
                st.rerun()
    with s2:
        if old_name and st.button("🗑 このパターンを消す"):
            cfg["patterns"] = [p for p in _patterns(cfg) if p.get("name") != old_name]
            _save_settings(cfg)
            st.session_state.sms_view = "list"
            st.rerun()


# ==========================================
# ▶ 画面3：送信をはじめる
# ==========================================
elif st.session_state.sms_view == "run":
    pname = st.session_state.sms_pattern
    pat = _find(cfg, pname)
    if not pat:
        st.error("パターンが見つかりません。")
        st.session_state.sms_view = "list"
        st.stop()

    if st.button("⬅ 一覧に戻る"):
        st.session_state.sms_view = "list"
        st.rerun()

    st.markdown(f"### 📱 {pname}")

    fkey = f"sms_find_{pname}"
    enc = pat.get("csv_encoding", "Shift_JIS")
    src = pat.get("csv_source", CSV_SOURCES[0])

    # ⭐ ①更新 → ②チェック → ③CSV → ④一括送信 を通しで行う。
    #    送ったSMSは取り消せないので、**チェックで1件でも出たら送らずに止める**。
    #    押すだけで送信まで行くボタンなので、確認を入れないと押せないようにしてある。
    with st.container(border=True):
        st.markdown("#### ▶ ぜんぶ実行する")
        _has_ref = bool(pat.get("refresh_robot") and (pat.get("refresh_tabs") or []))
        _flow = ["① シートの更新" if _has_ref else "① 更新（この設定では行いません）",
                 ("② 中身の確認（人が見ます）" if (pat.get("check_tabs") or [])
                  else "② 中身の確認"), "③ CSVの用意", "④ 一括送信"]
        _loads_all = pat.get("loads", []) or []
        st.caption("　→　".join(_flow))
        st.markdown("**② で直すところが1件でも出たら、送信せずに止まります。**")
        st.caption("すでに送った宛先が入っていたら、自動で外してから送ります。")
        # 設定で「ここまで行く」と決めてあれば、毎回チェックを入れ直さなくてよい。
        _set_send = bool(pat.get("auto_send", False))
        _set_load = bool(pat.get("auto_load", False))
        _push_too = _set_load
        if _loads_all and not _set_load:
            _push_too = st.checkbox("送信のあと、Salesforceへの投入（全件）まで行う",
                                    key=f"sms_allpush_{pname}",
                                    help="UPSERTなので既存の値が上書きされます。")
        if _set_send:
            st.warning("⚙️ この設定では、**一括送信まで自動で行います**"
                       + ("（Salesforceへの投入まで）" if _set_load else "")
                       + "。止めたいときは、設定画面の 5️⃣ でOFFにしてください。")
        _ok_all = _set_send or st.checkbox(
            "**最後の一括送信まで、止めずに実行します**（送ったSMSは取り消せません）",
            key=f"sms_allagree_{pname}")
        # 一覧の「▶ 全部実行」で来たときは、押し直さずにそのまま走り出す。
        _auto = st.session_state.pop(f"sms_auto_{pname}", False)
        _resume = st.session_state.pop(f"sms_resume_{pname}", False)
        if _auto and _resume:
            st.info("✅ 確認できたので、**続きから**実行します（シートの更新はやり直しません）。")
        elif _auto:
            st.info("▶ 一覧の「全部実行」から来たので、そのまま実行します。"
                    "**最後の一括送信の手前で、もう一度確認します。**")
        if _auto or st.button("▶ ぜんぶ実行する", type="primary", disabled=not (_ok_all and gc),
                              use_container_width=True, key=f"sms_allgo_{pname}"):
            with st.spinner("順番に実行しています（更新に時間がかかることがあります）..."):
                st.session_state[f"sms_all_{pname}"] = _run_all_sms(
                    pat, pname, gc, src, enc, bool(_push_too),
                    stop_before_send=not _ok_all, resume=bool(_resume))
            st.rerun()
        _allres = st.session_state.get(f"sms_all_{pname}")
        # 👀 人の確認で止まったら、その場で小窓を出す（画面を探しに行かせない）
        if _allres and any(r["結果"] == "⏸" and "目で見て" in str(r["中身"]) for r in _allres):
            st.session_state.setdefault(f"sms_dlg_{pname}", True)
        if st.session_state.get(f"sms_dlg_{pname}") and gc:
            _confirm_dialog(gc, pat, pname, (pat.get("check_tabs") or []),
                            (st.session_state.get(fkey) or {}).get("findings", []))
        if _allres:
            st.dataframe(pd.DataFrame(_allres), use_container_width=True, hide_index=True)
            if all(r["結果"] == "✅" for r in _allres):
                st.success("✅ 最後まで通りました。**プッシュプロ側の送信結果も必ず確認してください。**")
            elif any(r["結果"] == "⏸" for r in _allres):
                if any("送信の確認" in str(r["中身"]) for r in _allres):
                    st.warning("⏸ **CSVまで用意できました。** 上の"
                               "「最後の一括送信まで、止めずに実行します」に"
                               "チェックを入れて、もう一度押すと送信します。"
                               "（下の 4️⃣ から1本ずつ送ることもできます）")
                else:
                    st.warning("⏸ **人の確認待ちです。** 下の 2️⃣ でシートの中身を見て、"
                               "問題なければチェックを入れてから、もう一度押してください。")
            elif any(r["結果"] == "⏹" for r in _allres):
                st.info("⏹ **送るものがありませんでした。**"
                        "CSVの宛先が、すでに送った分だけだったということです。"
                        "エラーではありません。"
                        "どうしても送り直したいときは、下の「📮 送信の記録」から消してください。")
            else:
                st.error("🛑 途中で止まりました。上の表の「中身」を見て、"
                         "対応してから下の各工程で続きを行ってください。")
    ch.guide("operate",
             "順番にいくよ。<b>直すところが残っているうちは、送信ボタンは出さない</b>から安心してね。")

    # --- ① シートを更新 ---
    with st.container(border=True):
        theme.section_title("1️⃣", "SFコネクタでシートを更新する")
        _tabs = pat.get("refresh_tabs", []) or []
        if pat.get("refresh_robot") and _tabs:
            st.caption(f"使うロボット：**{pat['refresh_robot']}**　"
                       f"／ 更新するシート：**{'、'.join(_tabs)}**")
            # 📌 録画したときのURLではなく、このパターンに登録したスプシを開く。
            #    「録画したスプシしか更新できないのでは」と迷わないよう、開く先を必ず見せる。
            st.caption(f"開くスプレッドシート：{pat['sheet_url']}")
            _lg, _lgw = common_robots.login_status(pat["refresh_robot"])
            if _lg:
                st.caption(f"🔐 Googleのログイン状態：あり（最終 {_lgw}）")
            else:
                st.warning("🔐 このロボットのブラウザに**ログイン状態がありません**。"
                           "「⚙️ その他設定」の🔐先にログインしておく、から入っておいてください。")
            st.caption("ブラウザは1回だけ開いて、その中でシートを切り替えながら回します。"
                       "録画したときのURLは使いません（開く先はここに登録したスプシです）。")
            r1, r2 = st.columns([1, 1])
            _one = None
            with r2:
                _one = st.selectbox("お試し（1枚だけ更新してみる）",
                                    ["（使わない）"] + list(_tabs), key=f"sms_one_{pname}")
                _one = None if str(_one).startswith("（") else _one
                if st.button("🧪 この1枚だけ試す", use_container_width=True, disabled=not _one):
                    _u = sms_runner.tab_urls_for(pat["sheet_url"], [_one], _gids_of(pat))
                    with st.spinner(f"「{_one}」だけ更新しています..."):
                        ok, log = sms_runner.run_refresh_robot(pat["refresh_robot"], pname,
                                                               tabs=[_one], tab_urls=_u,
                                                               url=pat["sheet_url"])
                    st.session_state[f"sms_ref_{pname}"] = {
                        "ok": ok, "log": log,
                        "表": sms_runner.parse_refresh_log(log, [_one])}
                    st.rerun()
            with r1:
                if st.button("🔄 ぜんぶ更新する", type="primary", use_container_width=True):
                    _u = sms_runner.tab_urls_for(pat["sheet_url"], _tabs, _gids_of(pat))
                    with st.spinner(f"{len(_tabs)}枚のシートを順に更新しています"
                                    "（レポートによっては数分かかります）..."):
                        ok, log = sms_runner.run_refresh_robot(pat["refresh_robot"], pname,
                                                               tabs=_tabs, tab_urls=_u,
                                                               url=pat["sheet_url"])
                    st.session_state[f"sms_ref_{pname}"] = {
                        "ok": ok, "log": log, "表": sms_runner.parse_refresh_log(log, _tabs)}
                    st.rerun()
            res = st.session_state.get(f"sms_ref_{pname}")
            if res:
                st.dataframe(pd.DataFrame(res["表"]), use_container_width=True, hide_index=True)
                if res["ok"]:
                    st.success("✅ ぜんぶ更新できました。")
                else:
                    st.error("❌ 途中で止まりました。上の表で、どのシートまで進んだか分かります。")
                with st.expander("実行ログ", expanded=not res["ok"]):
                    st.code(res["log"])
        else:
            st.info("このパターンは、シートの更新を**手作業**で行う設定です。")
            if pat.get("sheet_url"):
                st.markdown(f"[📄 スプレッドシートを開く]({pat['sheet_url']})")

    # --- ② 中身を見て、人がOKを出す ---
    #     ⭐ ルールを作らなくても使えるようにする。
    #        「見るシートを登録 → 人が見てOK → 次へ」が本来やりたかったこと。
    #        登録が無ければ、そのまま素通りする。
    _watch = list(pat.get("check_tabs", []) or [])
    _rules = list(pat.get("checks", []) or [])
    _okkey = f"sms_ok_{pname}"
    with st.container(border=True):
        theme.section_title("2️⃣", "中身を見て確認する")
        if not _watch and not _rules:
            st.info("確認するシートは登録されていません。**このまま進みます。**"
                    "（見たいシートがあれば、設定画面の 3️⃣ で登録してください）")
        else:
            # --- ルールがあるときだけ、自動チェックも回す ---
            if _rules:
                c1, c2 = st.columns([1, 3])
                with c1:
                    if st.button("🔍 ルールでチェックする", use_container_width=True,
                                 type="primary", disabled=not gc):
                        with st.spinner("シートを見ています..."):
                            try:
                                findings, notes = sms_runner.check_rules(
                                    gc, pat["sheet_url"], _rules)
                                st.session_state[fkey] = {"findings": findings, "notes": notes}
                            except Exception as e:
                                st.session_state[fkey] = {
                                    "findings": [], "notes": [f"読めませんでした：{e}"]}
                        st.rerun()
                with c2:
                    if pat.get("sheet_url"):
                        st.markdown(f"[📄 スプレッドシートを開いて直す]({pat['sheet_url']})")
                res = st.session_state.get(fkey)
                if res is None:
                    st.caption("まだチェックしていません。")
                else:
                    for n in res.get("notes", []):
                        st.warning(n)
                    if res.get("findings"):
                        st.error(f"🛠 ルールに引っかかった行が **{len(res['findings'])}件** あります。"
                                 "直してから、もう一度チェックしてください。")
                        st.download_button(
                            "⬇️ 一覧をCSVで落とす",
                            data=pd.DataFrame(res["findings"]).to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"要修正_{pname}_{sms_runner.today_stamp()}.csv",
                            mime="text/csv")
                    else:
                        st.success("✅ ルールに引っかかった行はありません。")

            # --- 見るシートを、そのまま出す（ここが本体） ---
            res = st.session_state.get(fkey)
            _show = _watch or _view_sheets(pat)
            if gc and _show:
                _cur = st.selectbox("見るシート", _show, key=f"sms_view_{pname}")
                _c1, _c2 = st.columns([1, 3])
                with _c1:
                    if st.button("🔄 読み直す", key=f"sms_reread_{pname}",
                                 use_container_width=True):
                        _read_tab_cached.clear()
                        st.session_state.pop(_okkey, None)   # 読み直したら、OKは取り消す
                        st.rerun()
                with _c2:
                    _g = _gids_of(pat).get(_cur)
                    st.markdown(f"[📝 このシートを開いて直す]"
                                f"({sms_runner.sheet_tab_url(pat['sheet_url'], _g) if _g else pat['sheet_url']})")
                try:
                    _df, _ng, _colmap = _sheet_table(gc, pat, _cur,
                                                     (res or {}).get("findings", []))
                except Exception as e:
                    _df, _ng, _colmap = None, 0, {}
                    st.warning(f"「{_cur}」を読めませんでした：{str(e)[:150]}")
                if _df is None or _df.empty:
                    st.caption("このシートは空です。")
                else:
                    if _ng:
                        st.error(f"🛠 このシートに、直すところが **{_ng}行** あります。")
                        if st.checkbox("直すところだけ見る", key=f"sms_only_{pname}"):
                            _df = _df[_df["🛠"] == "要修正"]
                    st.caption(f"{len(_df)}行（見出しを除く）／"
                               "**表の中を直せます。行番号はスプレッドシートと同じです。**")
                    _locked = [c for c in ("行", "🛠", "直すところ") if c in _df.columns]
                    _edited = st.data_editor(
                        _df, use_container_width=True, hide_index=True, height=420,
                        disabled=_locked, num_rows="fixed", key=f"sms_edit_{pname}_{_cur}")
                    _cols = [c for c in _df.columns if c not in _locked]
                    _changes, _shown = [], []
                    for _i in range(len(_df)):
                        _rowno = int(_df.iloc[_i]["行"])
                        for _c in _cols:
                            _old, _new = str(_df.iloc[_i][_c]), str(_edited.iloc[_i][_c])
                            if _old != _new:
                                _changes.append((_rowno, _colmap[_c], _new))
                                _shown.append({"行": _rowno, "列": _c, "前": _old, "あと": _new})
                    if _shown:
                        st.caption("書き戻す内容（まだ反映していません）")
                        st.dataframe(pd.DataFrame(_shown), use_container_width=True,
                                     hide_index=True)
                    if st.button(f"💾 直した{len(_changes)}か所を書き戻す", type="primary",
                                 disabled=not _changes, key=f"sms_save_{pname}"):
                        try:
                            _n = sms_runner.write_cells(gc, pat["sheet_url"], _cur, _changes)
                            _read_tab_cached.clear()
                            st.session_state.pop(_okkey, None)
                            st.session_state[f"sms_saved_{pname}"] = _n
                            if _rules:
                                try:
                                    _f2, _n2 = sms_runner.check_rules(gc, pat["sheet_url"], _rules)
                                    st.session_state[fkey] = {"findings": _f2, "notes": _n2}
                                except Exception:
                                    pass
                            st.rerun()
                        except Exception as _e:
                            st.error(f"書き戻せませんでした：{str(_e)[:200]}\n\n"
                                     "そのスプレッドシートを、サービスアカウントに"
                                     "**編集者**として共有しているか確認してください。")
                    _sv = st.session_state.pop(f"sms_saved_{pname}", None)
                    if _sv:
                        st.success(f"✅ {_sv}か所をスプレッドシートに書き戻しました。")

            # --- 人のOK（ここを通らないと先へ進まない） ---
            if _watch:
                st.markdown("---")
                st.checkbox(f"✅ **{'／'.join(_watch)}** を見て、問題ないことを確認しました",
                            key=_okkey)
                if not st.session_state.get(_okkey):
                    st.info("確認できたら、上にチェックを入れてください。次へ進めます。")

    # 次へ進めるか：ルールは0件、目で見るシートは人のOK。どちらも無ければ素通り。
    res = st.session_state.get(fkey)
    _rules_ok = (not _rules) or (bool(res) and not res.get("findings"))
    _watch_ok = (not _watch) or bool(st.session_state.get(_okkey))
    clean = _rules_ok and _watch_ok

    # --- ③ CSVを用意 ---
    #     📄 CSVにするシートは複数持てる（1シート＝1回の送信）。
    #        置き場所はシートごとに分ける（同じ名前だと、先に作ったほうが消える）。
    _sheets = _csv_sheets(pat) or [""]
    with st.container(border=True):
        theme.section_title("3️⃣", "CSVを用意する")
        if not clean:
            if _watch and not st.session_state.get(_okkey):
                st.info("上の 2️⃣ でシートの中身を見て、**チェックを入れる**と、ここのボタンが出ます。")
            else:
                st.info("上の 2️⃣ で「引っかかった行はありません」になると、ここのボタンが出ます。")
        else:
            st.caption(f"用意のしかた：**{src}**")
            if len(_sheets) > 1:
                st.caption(f"**{len(_sheets)}本**作ります（{' ／ '.join(_sheets)}）。"
                           "1シートにつき1回、送信します。")
            if st.button("📄 CSVを用意する"):
                for _sh in _sheets:
                    _tag = f"（{_sh}）" if _sh else ""
                    try:
                        for _lv, _tx in _prepare_csv(pat, pname, src, enc, gc, _sh):
                            getattr(st, _lv)(f"{_tx}{_tag}")
                    except Exception as e:
                        st.error(f"CSVを用意できませんでした{_tag}：{e}")
                st.session_state.pop(f"sms_dup_{pname}", None)

            for _sh in _sheets:
                _slot = sms_runner.sheet_slot(pname, _sh)
                if len(_sheets) > 1:
                    st.markdown(f"**📄 {_sh}**")
                got = sms_runner.today_csv(_slot)
                if not got:
                    _made = sms_runner.csv_made_at(_slot)
                    if _made:
                        st.warning(f"⚠️ 手元のCSVは **{_made}** に用意したものです"
                                   "（今日ではありません）。"
                                   "古いものを送らないよう、もう一度用意してください。")
                    else:
                        st.caption("まだ今日のCSVがありません。")
                else:
                    _n = sms_runner.csv_row_count(got, enc)
                    st.markdown(f"**今日のCSV**：`{os.path.basename(got)}`　"
                                f"（{_n}件・用意 {sms_runner.csv_made_at(_slot)}）")
                    with st.expander(f"👀 中身を先頭だけ見る（送る前の最終確認）"
                                     + (f"／{_sh}" if len(_sheets) > 1 else "")):
                        st.code(sms_runner.csv_preview(got, enc))

    # --- ④ 二重送信チェック → 一括送信 ---
    #     ⚠️「すでに送った宛先」の記録は**パターンでまとめて**見る。
    #        シートごとに分けると、同じ人に両方の文面が届いてしまう。
    with st.container(border=True):
        theme.section_title("4️⃣", "二重送信の確認と、一括送信")
        if not clean:
            st.info("上の 3️⃣ で今日のCSVが用意できると、ここのボタンが出ます。")
        else:
            days = int(pat.get("dedup_days", 0) or 0)
            for _sh in _sheets:
                _slot = sms_runner.sheet_slot(pname, _sh)
                _k = f"{pname}_{_sh}" if _sh else pname
                got = sms_runner.today_csv(_slot)
                if len(_sheets) > 1:
                    st.markdown(f"**📤 {_sh}**")
                if not got:
                    st.info("3️⃣ で今日のCSVを用意すると、ここのボタンが出ます。")
                    continue
                keys = sms_runner.csv_dest_keys(got, enc)
                dup = sms_runner.find_already_sent(pname, keys, days)
                if dup:
                    st.error(f"🛑 このCSVには、**すでに送った宛先が {len(dup)}件** 入っています"
                             + (f"（{days}日以内）" if days else "（過去に一度でも送った分）")
                             + "。そのまま送ると二重送信になります。")
                    st.dataframe(pd.DataFrame(dup), use_container_width=True, hide_index=True)
                    if st.button("✂️ 送った分を外したCSVにする", type="primary",
                                 key=f"sms_drop_{_k}"):
                        n_drop, n_left = sms_runner.drop_already_sent(
                            _slot, enc, days, sent_pattern=pname)
                        st.success(f"✅ {n_drop}件を外しました（残り {n_left}件）。")
                        st.rerun()
                    st.caption("※ どうしても送り直したい場合は、"
                               "下の「送信の記録」から消してください。")
                elif not keys:
                    st.info("⏹ このCSVには送る宛先がありません（0件）。")
                else:
                    st.success(f"✅ 送った宛先とのかぶりはありません（{len(keys)}件）。")
                    agree = st.checkbox(f"この **{len(keys)}件** に、実際にSMSを一括送信します"
                                        "（取り消せません）", key=f"sms_agree_{_k}")
                    if st.button("🚀 一括送信する", type="primary", disabled=not agree,
                                 key=f"sms_send_{_k}"):
                        with st.spinner("ブラウザを開いて送信しています..."):
                            ok, log = sms_runner.run_send_robot(
                                pat["send_robot"], _slot, got,
                                allow_errors=bool(pat.get("allow_errors")))
                        # 📮 送った／送っていないを記録する。
                        #    プッシュプロは一括送信なので1件ごとの成否は分からない。
                        #    途中で止まっても『送信』まで進んでいたら、送られた可能性がある。
                        #    二重送信のほうが取り返しがつかないので、迷ったら「送った」に寄せる。
                        if ok:
                            result, note = "送信済み", ""
                        elif sms_runner.submit_reached(log):
                            result, note = "要確認（送ったかもしれない）", "途中で止まりました"
                        else:
                            result, note = "送信できず", "送信の手前で止まりました"
                        # 🚫 弾かれた宛先は送られていないので、記録に入れない
                        _drop = sms_runner.dropped_dests(log)
                        _keys_sent = [(n, k) for n, k in keys if k not in _drop]
                        sms_runner.record_sent(pname, _keys_sent, result, note)
                        _r = {"ok": ok, "log": log, "result": result,
                              "n": len(_keys_sent), "drop": _drop, "シート": _sh}
                        st.session_state[f"sms_sent_{_k}"] = _r
                        st.session_state[f"sms_sent_{pname}"] = _r
                        st.rerun()

                done = st.session_state.get(f"sms_sent_{_k}")
                if done:
                    if done.get("drop"):
                        st.warning(f"🚫 **{len(done['drop'])}件は、プッシュプロに弾かれて"
                                   "送られませんでした。**（SMS非対応の番号など）"
                                   "この番号は「送信済み」に入れていないので、"
                                   "直せば送り直せます。")
                        st.dataframe(pd.DataFrame([{"送られなかった宛先": d}
                                                   for d in done["drop"]]),
                                     use_container_width=True, hide_index=True)
                    if done["ok"]:
                        st.success(f"✅ {done['n']}件の送信手順が最後まで通りました。"
                                   "プッシュプロ側の送信結果も必ず確認してください。")
                    elif done["result"].startswith("要確認"):
                        st.error(f"⚠️ 途中で止まりましたが、**送信ボタンまで進んでいました**。"
                                 f"{done['n']}件を「要確認（送ったかもしれない）」として"
                                 "記録しました。プッシュプロの送信履歴を見て、"
                                 "実際に送られたか確かめてください。")
                    else:
                        _why = sms_runner.stop_reason(done.get("log", ""))
                        st.error("❌ **送信の手前で止まりました。SMSは送られていません。**"
                                 + (f"\n\n**止まった理由：{_why}**" if _why else "")
                                 + "\n\n送信の記録は増やしていないので、"
                                   "直してから、もう一度送れます。")
                    # 📸 止まった理由は画面を見れば分かることが多い。その場で出す。
                    for _p in sms_runner.shot_paths(done.get("log", "")):
                        st.image(_p, caption=f"止まったときの画面：{os.path.basename(_p)}",
                                 use_container_width=True)
                    with st.expander("実行ログ"
                                     + (f"／{_sh}" if len(_sheets) > 1 else ""),
                                     expanded=not done["ok"]):
                        st.code(done["log"])

    # --- ⑤ 送ったあとに Salesforce へ入れる ---
    #     送りっぱなしにせず、「送った」ことをSalesforceに残す工程。
    #     **送信が通っていないうちは出さない**（送っていないのに記録だけ残さないため）。
    _loads = pat.get("loads", []) or []
    if _loads:
        with st.container(border=True):
            theme.section_title("5️⃣", "送ったあとに Salesforce へ入れる")
            # 📌 ⑤に進む条件は「このセッションで送信した」ではなく「人が送信を確かめた」。
            #    送信済みで④が0件になる日や、通しの確認をしたい日に、ここで詰まっていた。
            _done = st.session_state.get(f"sms_sent_{pname}")
            if not _done:
                st.info("④の一括送信が終わると、ここのボタンが出ます。")
                if st.checkbox("④を通さずに投入する（プッシュプロ側で送信済みを確認しました）",
                               key=f"sms_pushonly_{pname}",
                               help="すでに送信済みの日や、投入だけを確かめたいときに使います。"):
                    _done = {"ok": True}
            elif not _done.get("ok"):
                st.warning("④の送信が最後まで通っていません。"
                           "送れていないのに『送った』と記録すると、あとで追えなくなります。"
                           "先に送信を通してください。")
                if st.checkbox("それでも投入する（プッシュプロ側で送信済みを確認しました）",
                               key=f"sms_pushanyway_{pname}"):
                    _done = {"ok": True}
            if _done and _done.get("ok"):
                st.dataframe(pd.DataFrame([
                    {"投入するシート": str(x.get("シート", "")),
                     "投入先": str(x.get("オブジェクト", "")),
                     "照合キー": str(x.get("照合キー", "")),
                     "マッピング": f"{len(x.get('マッピング', {}) or {})}項目"} for x in _loads]),
                    use_container_width=True, hide_index=True)
                _n_try = st.number_input("お試し件数（先にこれだけ入れて確かめる）",
                                         min_value=1, max_value=200, value=5,
                                         key=f"sms_ntry_{pname}")
                _b1, _b2 = st.columns(2)
                _do_try = _b1.button(f"🧪 各{int(_n_try)}件だけ入れてみる",
                                     use_container_width=True, key=f"sms_try_{pname}")
                _agree_p = st.checkbox("**全件を Salesforce に反映します**"
                                       "（UPSERTなので上書きされます）", key=f"sms_pushall_{pname}")
                _do_all = _b2.button("🚀 全件を投入する", type="primary", use_container_width=True,
                                     disabled=not _agree_p, key=f"sms_pushgo_{pname}")
                if _do_try or _do_all:
                    _lim = int(_n_try) if _do_try else 0
                    _out = []
                    _pg = st.progress(0.0)
                    for _i, _ld in enumerate(_loads):
                        with st.spinner(f"「{_ld.get('シート')}」を投入しています..."):
                            _r = sf_ui.push_sheet(gc, pat["sheet_url"], str(_ld.get("シート", "")),
                                                  str(_ld.get("オブジェクト", "")),
                                                  str(_ld.get("照合キー", "")),
                                                  _ld.get("マッピング", {}) or {}, limit=_lim)
                        _out.append({"シート": str(_ld.get("シート", "")), "結果": _r["結果"],
                                     "成功": _r["ok"], "失敗": _r["ng"],
                                     "_errors": _r["errors"], "_obj": _r["オブジェクト"]})
                        _pg.progress((_i + 1) / len(_loads))
                    st.session_state[f"sms_push_{pname}"] = _out
                    st.rerun()

                _pushed = st.session_state.get(f"sms_push_{pname}")
                if _pushed:
                    st.dataframe(pd.DataFrame([{k: v for k, v in r.items()
                                                if not k.startswith("_")} for r in _pushed]),
                                 use_container_width=True, hide_index=True)
                    for r in _pushed:
                        if r.get("_errors"):
                            st.markdown(f"**❌ {r['シート']} の失敗（{r['失敗']}件）**")
                            sf_ui.render_errors(r["_errors"], r.get("_obj", ""),
                                                key_prefix=f"smserr_{r['シート']}")

    # --- 送信の記録 ---
    with st.expander("📮 送信の記録（誰に送ったか）"):
        sent = sms_runner.load_sent(pname)
        if not sent:
            st.caption("まだ記録はありません。")
        else:
            sdf = pd.DataFrame([{"宛先": k, "日時": v.get("日時", ""),
                                 "結果": v.get("結果", ""), "メモ": v.get("メモ", "")}
                                for k, v in sent.items()])
            sdf = sdf.sort_values("日時", ascending=False)
            st.caption(f"{len(sdf)}件（新しい順）")
            st.dataframe(sdf.head(500), use_container_width=True, hide_index=True)
            st.download_button("⬇️ 記録をCSVで落とす",
                               data=sdf.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"送信履歴_{pname}_{sms_runner.today_stamp()}.csv",
                               mime="text/csv")

            # 🗑 記録から消す＝また送れるようにする。
            #    歯止めを外す操作なので、確かめたことをはっきりさせてからにする。
            st.markdown("---")
            st.markdown("**記録から消す（もう一度送れるようにする）**")
            _pick = st.multiselect("消す宛先", list(sdf["宛先"]), key=f"sms_forget_{pname}")
            _sure = st.checkbox("プッシュプロ側で、**実際には送られていない**ことを確認しました",
                                key=f"sms_forgetok_{pname}")
            if st.button("🗑 選んだ宛先を記録から消す", key=f"sms_forgetgo_{pname}",
                         disabled=not (_pick and _sure)):
                _n = sms_runner.forget_sent(pname, _pick)
                st.success(f"{_n}件を記録から消しました。次のCSVからは外れなくなります。")
                st.rerun()
            st.caption("⚠️ 消すと二重送信の歯止めが外れます。"
                       "**送られていたのに消すと、同じ人に二度届きます。**")

    st.divider()
    st.caption("💻 更新・書き出し・一括送信はブラウザを開くため、**担当者のPCで開いているとき**だけ動きます。")
