"""
🗃 データローダー自動化

やることは2つだけ。**ひたすら繰り返す作業**をひとまとめにする。

  ① 登録したシートを、SFコネクタで更新する（タブ複数可・上から順に）
  ② 登録したシートを、そのまま Salesforce に入れる（UPSERT）

②は Data Loader（デスクトップ版）の代わりに、アプリから直接 Salesforce へ入れる
（`salesforce_loader.py`）。CSVの書き出しもダウンロードも要らなくなる。
いま使っているマッピングファイル（.sdl）はそのまま取り込めるので、対応表を作り直す必要はない。

【設定は「シート名 × マッピング」だけ】
別表に投入設定を登録させると、同じことを2か所に書くことになる。
ジョブにはもうスプシが登録されているので、**シートを選んで、そこにマッピングを紐づける**だけでよい。
「🩺 照らし合わせ」で、マッピングの列名がそのシートに実在するか・Salesforceに実在するかを、
投入する前に確かめられる。

【ロボットは共通1台】
SFコネクタの更新は、どのスプシ・どのシートでも押す場所は同じ。違うのは開く先だけ。
だから手順書は「SMS送信」で作った共通ロボットをそのまま使い、
実行時に**開くURLだけ差し替える**（`robot.py --run ... --url ...`）。
"""
import io
import json

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import characters as ch
import salesforce_loader as sfl
import sf_ui
import sms_runner
import theme

st.set_page_config(page_title="データローダー自動化 - エンカンAI", layout="wide")

theme.inject_theme()
theme.brand_sidebar(active="operate")

c = ch.get("operate")
theme.page_header("🗃", "データローダーを自動化",
                  "登録したシートを更新して、そのまま Salesforce に入れるところまでを一本にします。",
                  color=c["color"])


# ==========================================
# 🔌 つなぎこみ
# ==========================================
@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()

SETTINGS_ID = "__dataloader__"
WORK_ROOT = "データローダー"                    # 取り込みファイル/データローダー/<ジョブ名>
DEFAULT_REFRESH_ROBOT = "共通_SFコネクタ更新"   # SMS送信ページで作る共通ロボット


def _load() -> dict:
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}


def _save(cfg: dict):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（データローダーの設定）", "is_active": False,
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


@st.cache_data(ttl=120, show_spinner=False)
def _tab_gids(_gc, sheet_url: str) -> dict:
    sh = _gc.open_by_url(sheet_url) if sheet_url.startswith("http") else _gc.open_by_key(sheet_url)
    return {w.title: w.id for w in sh.worksheets()}


@st.cache_data(ttl=120, show_spinner=False)
def _sheet_headers(_gc, sheet_url: str, tab: str):
    """そのシートの見出し（1行目）。マッピングと照らし合わせるために読む。"""
    sh = _gc.open_by_url(sheet_url) if sheet_url.startswith("http") else _gc.open_by_key(sheet_url)
    values = sh.worksheet(tab).get_all_values()
    return [str(h).strip() for h in (values[0] if values else [])]


def _read_table(gc, sheet_url: str, tab: str):
    sh = gc.open_by_url(sheet_url) if sheet_url.startswith("http") else gc.open_by_key(sheet_url)
    values = sh.worksheet(tab).get_all_values()
    if not values:
        return [], []
    return [str(h).strip() for h in values[0]], values[1:]


@st.cache_data(ttl=600, show_spinner=False)
def _key_options(object_api: str):
    """照合キーに使える項目（Id と外部ID）。Data Loader の選択肢と同じ並び。"""
    return sf_ui._key_field_options(object_api) or ["Id"]


def _robots():
    """SMS送信ページで作った共通ロボットの名前を集める。"""
    try:
        rows = supabase.table("merchants").select("id,config_json").execute().data or []
    except Exception:
        return []
    return sorted(r["id"] for r in rows
                  if not str(r["id"]).startswith("__")
                  and str((r.get("config_json") or {}).get("product_type", "")) == "SMS送信")


def _jobs(cfg):
    jobs = cfg.get("jobs", []) or []
    # 🧹 前の形（投入を「投入名」の文字列で持っていた版）で保存された分を、いまの形に読み替える。
    #    そのままだと画面が落ちるので、読むときに直しておく。
    for j in jobs:
        j["loads"] = [x if isinstance(x, dict)
                      else {"シート": str(x), "オブジェクト": "Opportunity",
                            "照合キー": "Id", "マッピング": {}}
                      for x in (j.get("loads", []) or [])]
    return jobs


def _find(cfg, name):
    for j in _jobs(cfg):
        if j.get("name") == name:
            return j
    return None


def _push_one(gc, sheet_url: str, tab: str, obj: str, key_field: str, mapping: dict,
              limit: int = 0) -> dict:
    """1つの投入を実行する。Data Loader の1ジョブにあたる。

    投入する前に「シートに列があるか」「Salesforceに項目があるか」を必ず確かめ、
    どちらかが欠けていたら**送らずに止める**（間違った上書きは戻せないため）。
    """
    out = {"結果": "", "ok": 0, "ng": 0, "errors": [], "オブジェクト": obj}
    if not (obj and key_field and mapping):
        out["結果"] = "⚠️ オブジェクト・照合キー・マッピングのどれかが未設定です"
        return out
    try:
        headers, rows = _read_table(gc, sheet_url, tab)
    except Exception as e:
        out["結果"] = f"❌ シート「{tab}」を読めません: {str(e)[:120]}"
        return out
    if not headers:
        out["結果"] = f"⚠️ シート「{tab}」が空です"
        return out

    missing = [k for k in mapping if k not in headers]
    if missing:
        out["結果"] = "❌ シートに無い列がマッピングにあります：" + "／".join(missing[:5])
        return out

    try:
        sf = sfl.connect()
    except Exception as e:
        out["結果"] = f"❌ Salesforceに接続できません: {str(e)[:120]}"
        return out
    bad, _f = sfl.check_mapping(sf, obj, mapping)
    if bad:
        out["結果"] = ("❌ Salesforceに無い項目があるので中止しました："
                       + "／".join(str(b.get("スプシの列", b)) for b in bad[:5]))
        out["errors"] = bad
        return out

    types = sfl.describe_field_types(sf, obj)
    records, skipped, merged = sfl.build_records(headers, rows, mapping,
                                                 skip_empty_key=key_field, field_types=types)
    if not records:
        out["結果"] = "⚠️ 投入できる行がありません（照合キーが空）"
        return out

    res = sfl.upsert(sf, obj, key_field, records, limit=limit)
    out.update({"ok": res["ok"], "ng": res["ng"], "errors": res["errors"]})
    if not res["ng"]:
        out["結果"] = (f"✅ {res['ok']}件を投入しました"
                       + (f"（{skipped}件はキーが空で対象外）" if skipped else "")
                       + (f"（重なっていた{merged}件は1つにまとめました）" if merged else ""))
    else:
        _reasons = [str(e.get("原因", "")) for e in res["errors"] if e.get("原因")]
        _top = max(set(_reasons), key=_reasons.count) if _reasons else ""
        out["結果"] = (f"⚠️ 成功 {res['ok']}件／失敗 {res['ng']}件"
                       + (f"　いちばん多い原因：{_top}" if _top else ""))
    return out


cfg = _load()
gc = _get_gspread_client()

st.session_state.setdefault("dl_view", "list")
st.session_state.setdefault("dl_job", "")

if not gc:
    st.warning("🔑 接続キー **GOOGLE_SERVICE_ACCOUNT_JSON** が未設定です（管理者に設定を依頼してください）。")


# ==========================================
# 📋 画面1：ジョブ一覧
# ==========================================
if st.session_state.dl_view == "list":
    st.session_state.pop("dl_loads", None)
    ch.guide("operate",
             "ここは<b>ひたすら繰り返す</b>ところ。ジョブを選んで「実行する」を押せば、"
             "シートの更新から Salesforce への投入まで、わたしが順番にやるね。")

    t1, t2 = st.columns([1, 3])
    with t1:
        if st.button("＋ ジョブを追加", type="primary", use_container_width=True):
            st.session_state.dl_view = "edit"
            st.session_state.dl_job = ""
            st.rerun()
    with t2:
        st.caption("ジョブ＝「このスプシの、このシートたちを更新して、このシートを投入する」のひとまとまり。")

    jobs = _jobs(cfg)
    if not jobs:
        st.info("まだジョブがありません。「＋ ジョブを追加」から、最初の1つを登録しましょう。")
    for j in jobs:
        with st.container(border=True):
            a, b, d = st.columns([3, 3, 2])
            with a:
                st.markdown(f"#### 🗃 {j.get('name', '(名前なし)')}")
                st.caption(j.get("memo", "") or "　")
            with b:
                _t = j.get("refresh_tabs", []) or []
                _l = j.get("loads", []) or []
                st.caption("更新するシート：" + ("、".join(_t) if _t else "（なし）"))
                st.caption("投入するシート：" + ("、".join(str(x.get("シート", "")) for x in _l)
                                                if _l else "（なし）"))
                _w = j.get("watch_tabs", []) or []
                if _w:
                    st.caption("目で見て確認：" + "、".join(_w))
            with d:
                if st.button("▶ 実行する", key=f"run_{j.get('name')}", type="primary",
                             use_container_width=True, disabled=not _l):
                    st.session_state.dl_view = "run"
                    st.session_state.dl_job = j.get("name", "")
                    st.rerun()
                if st.button("⚙️ 設定を直す", key=f"ed_{j.get('name')}", use_container_width=True):
                    st.session_state.dl_view = "edit"
                    st.session_state.dl_job = j.get("name", "")
                    st.rerun()

    st.divider()
    st.caption("💻 シートの更新はブラウザを開くため担当者のPCが必要です。"
               "Salesforceへの投入はクラウドからでも動きます。")


# ==========================================
# ⚙️ 画面2：ジョブの設定
# ==========================================
elif st.session_state.dl_view == "edit":
    old_name = st.session_state.dl_job
    job = _find(cfg, old_name) or {"name": "", "memo": "", "sheet_url": "",
                                   "refresh_tabs": [], "refresh_robot": DEFAULT_REFRESH_ROBOT,
                                   "loads": []}

    # 投入の並びは、保存を押すまで画面の中で編集する（行を足す／消すたびに保存させない）
    if st.session_state.get("dl_loads_of") != (old_name or "＿新規"):
        st.session_state["dl_loads"] = json.loads(json.dumps(job.get("loads", []) or []))
        st.session_state["dl_loads_of"] = old_name or "＿新規"
    loads = st.session_state["dl_loads"]

    if st.button("⬅ 一覧に戻る"):
        st.session_state.dl_view = "list"
        st.session_state.pop("dl_loads_of", None)
        st.rerun()

    st.markdown(f"### ⚙️ {'ジョブを追加' if not old_name else f'「{old_name}」の設定'}")

    with st.container(border=True):
        theme.section_title("1️⃣", "ジョブの名前と、つなぐスプレッドシート")
        name = st.text_input("ジョブの名前", value=job.get("name", ""), placeholder="例：長期不在")
        memo = st.text_input("メモ", value=job.get("memo", ""))
        sheet_url = st.text_input("スプレッドシートのURL", value=job.get("sheet_url", ""),
                                  placeholder="https://docs.google.com/spreadsheets/d/...")

    gids = {}
    if gc and sheet_url.strip():
        try:
            gids = _tab_gids(gc, sheet_url.strip())
        except Exception as e:
            st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")
    tabs = list(gids.keys())

    with st.container(border=True):
        theme.section_title("2️⃣", "SFコネクタで更新するシート（複数えらべます）")
        st.caption("選んだシートを、上から順に更新します。"
                   "**押す場所はどのスプシ・どのシートでも同じ**なので、ロボットは共通1台でOKです。"
                   "開くのは、上の1️⃣で登録した**このジョブのスプレッドシート**です"
                   "（録画したときのスプシではありません）。")
        if tabs:
            _cur = [t for t in (job.get("refresh_tabs", []) or []) if t in tabs]
            refresh_tabs = st.multiselect("更新するシート", tabs, default=_cur)
        else:
            refresh_tabs = [t.strip() for t in
                            st.text_input("更新するシート（カンマ区切り）",
                                          value="、".join(job.get("refresh_tabs", []) or [])
                                          ).replace("、", ",").split(",") if t.strip()]
            st.caption("※ スプシURLを入れると、シート名をプルダウンで選べます。")

        _robs = _robots()
        _cur_rob = job.get("refresh_robot", DEFAULT_REFRESH_ROBOT)
        _opts = ["（使わない：手で更新する）"] + _robs
        if _cur_rob and _cur_rob not in _opts:
            _opts.append(_cur_rob)
        refresh_robot = st.selectbox("使うロボット", _opts,
                                     index=_opts.index(_cur_rob) if _cur_rob in _opts else 0)
        refresh_robot = "" if refresh_robot.startswith("（") else refresh_robot
        if refresh_robot and refresh_robot not in _robs:
            st.warning(f"⚠️ ロボット「{refresh_robot}」はまだ作られていません。"
                       "「📱 SMS送信」ページの2️⃣で録画して作ってください（1回でOK・共通で使えます）。")

    # --- 3️⃣ 目で見て確認するシート（自動判定できないもの） ---
    with st.container(border=True):
        theme.section_title("3️⃣", "目で見て確認するシート（投入しない）")
        st.caption("「検討エラーリスト」のように、**どんなパターンが出るか決まっていない**ので"
                   "自動では判定できないシートを登録します。ここに登録したシートは投入しません。"
                   "実行のとき、**2行目以降に何か出ていたら知らせて、投入の前で止めます**。")
        if tabs:
            _curw = [t for t in (job.get("watch_tabs", []) or []) if t in tabs]
            watch_tabs = st.multiselect("確認するシート", tabs, default=_curw,
                                        help="例：検討エラーリスト")
        else:
            watch_tabs = [t.strip() for t in
                          st.text_input("確認するシート（カンマ区切り）",
                                        value="、".join(job.get("watch_tabs", []) or [])
                                        ).replace("、", ",").split(",") if t.strip()]
        watch_block = st.checkbox(
            "中身が出ていたら、確認するまで投入のボタンを出さない", value=job.get("watch_block", True),
            help="見落としたまま投入してしまうのを防ぎます。確認して対応したら、"
                 "チェックを入れれば先に進めます。")

    # --- 4️⃣ 投入：シートを選んで、マッピングを紐づけるだけ ---
    with st.container(border=True):
        theme.section_title("4️⃣", "Salesforceへの投入（シート × マッピング）")
        st.caption("投入するシートを選んで、そこに **いまお使いのマッピング（.sdl）** を紐づけます。"
                   "別表への登録は要りません。上から順に投入します。")

        _obj_opts = sf_ui._object_options()
        _obj_labels = sf_ui.object_labels()

        def _obj_name(o):
            return f"{_obj_labels.get(o, o)}（{o}）" if _obj_labels.get(o) else o

        for i, ld in enumerate(loads):
            with st.container(border=True):
                h1, h2 = st.columns([5, 1])
                with h1:
                    st.markdown(f"**投入 {i + 1}**")
                with h2:
                    if st.button("🗑", key=f"dl_del_{i}", help="この投入を消す"):
                        loads.pop(i)
                        st.rerun()

                a, b, d = st.columns([2, 2, 2])
                with a:
                    if tabs:
                        _t = ld.get("シート", "")
                        ld["シート"] = st.selectbox("投入するシート", tabs,
                                                    index=tabs.index(_t) if _t in tabs else 0,
                                                    key=f"dl_tab_{i}")
                    else:
                        ld["シート"] = st.text_input("投入するシート", value=ld.get("シート", ""),
                                                     key=f"dl_tab_{i}")
                with b:
                    _o = ld.get("オブジェクト", "Opportunity")
                    ld["オブジェクト"] = st.selectbox(
                        "投入先", _obj_opts, format_func=_obj_name,
                        index=_obj_opts.index(_o) if _o in _obj_opts else 0, key=f"dl_obj_{i}")
                with d:
                    _keys = _key_options(ld["オブジェクト"])
                    _k = ld.get("照合キー", "Id")
                    ld["照合キー"] = st.selectbox(
                        "照合キー", _keys, index=_keys.index(_k) if _k in _keys else 0,
                        key=f"dl_key_{i}",
                        help="Id＝既存レコードの更新のみ。外部ID＝無ければ新規作成もされます。")

                mapping = dict(ld.get("マッピング", {}) or {})

                up = st.file_uploader("マッピングファイルを取り込む（.sdl / .csv）",
                                      type=["sdl", "csv", "txt"], key=f"dl_up_{i}")
                if up is not None and st.button("📥 この内容を取り込む", key=f"dl_imp_{i}"):
                    try:
                        text = up.getvalue().decode("utf-8", errors="replace")
                        if up.name.lower().endswith(".csv"):
                            _df = pd.read_csv(io.StringIO(text))
                            pairs = {str(r[0]).strip(): str(r[1]).strip()
                                     for r in _df.values if len(r) >= 2}
                        else:
                            pairs = sfl.parse_sdl(text)
                        ld["マッピング"] = pairs
                        st.success(f"{len(pairs)}項目を取り込みました。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"取り込めませんでした: {e}")

                if mapping:
                    _flabels = sf_ui.field_labels(ld["オブジェクト"])
                    mdf = pd.DataFrame([{"スプシの列名": k, "Salesforce項目API名": v}
                                        for k, v in mapping.items()])
                    med = st.data_editor(mdf, num_rows="dynamic", use_container_width=True,
                                         key=f"dl_map_{i}")
                    ld["マッピング"] = {str(r["スプシの列名"]).strip(): str(r["Salesforce項目API名"]).strip()
                                        for _, r in med.iterrows()
                                        if str(r.get("スプシの列名", "")).strip()}

                    # 🩺 投入する前に、マッピングとシートの見出しを突き合わせる
                    if st.button("🩺 シートと照らし合わせる", key=f"dl_chk_{i}"):
                        try:
                            heads = _sheet_headers(gc, sheet_url.strip(), ld["シート"])
                        except Exception as e:
                            heads = []
                            st.error(f"シートを読めませんでした：{str(e)[:160]}")
                        if heads:
                            miss = [k for k in ld["マッピング"] if k not in heads]
                            extra = [h for h in heads if h and h not in ld["マッピング"]]
                            if miss:
                                st.error("❌ **シートに無い列**がマッピングにあります（このままだと投入できません）："
                                         + "／".join(miss))
                            else:
                                st.success(f"✅ マッピングの列 {len(ld['マッピング'])}件は、"
                                           f"すべてシート「{ld['シート']}」にあります。")
                            if extra:
                                st.caption("（参考）シートにあってマッピングに無い列："
                                           + "／".join(extra[:20])
                                           + ("…" if len(extra) > 20 else ""))
                            # Salesforce側にも実在するか
                            try:
                                sf = sfl.connect()
                                bad, _f = sfl.check_mapping(sf, ld["オブジェクト"], ld["マッピング"])
                                if bad:
                                    st.error("❌ **Salesforceに無い項目**があります：")
                                    st.dataframe(pd.DataFrame(bad), use_container_width=True,
                                                 hide_index=True)
                                else:
                                    st.success("✅ 項目はすべて Salesforce に実在します。")
                            except Exception as e:
                                st.warning(f"Salesforceの確認はできませんでした：{str(e)[:160]}")
                    if _flabels:
                        with st.expander("項目の日本語名を見る"):
                            st.dataframe(pd.DataFrame(
                                [{"スプシの列名": k, "Salesforce項目": f"{_flabels.get(v, v)}（{v}）"}
                                 for k, v in ld["マッピング"].items()]),
                                use_container_width=True, hide_index=True)
                else:
                    st.info("まだマッピングがありません。**いま Data Loader で使っている .sdl ファイル**を"
                            "上から取り込んでください（作り直す必要はありません）。")

        if st.button("＋ 投入を追加"):
            loads.append({"シート": (tabs[0] if tabs else ""), "オブジェクト": "Opportunity",
                          "照合キー": "Id", "マッピング": {}})
            st.rerun()

    st.divider()
    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button("💾 このジョブを保存", type="primary", use_container_width=True):
            if not name.strip():
                st.warning("ジョブの名前を入れてください。")
            else:
                new = {"name": name.strip(), "memo": memo.strip(), "sheet_url": sheet_url.strip(),
                       "refresh_tabs": list(refresh_tabs), "refresh_robot": refresh_robot,
                       "watch_tabs": list(watch_tabs), "watch_block": bool(watch_block),
                       "loads": [ld for ld in loads if str(ld.get("シート", "")).strip()]}
                jobs = _jobs(cfg)
                for i, j in enumerate(jobs):
                    if j.get("name") == (old_name or new["name"]):
                        jobs[i] = new
                        break
                else:
                    jobs.append(new)
                cfg["jobs"] = jobs
                _save(cfg)
                st.session_state.dl_view = "list"
                st.session_state.pop("dl_loads_of", None)
                st.success("保存しました。")
                st.rerun()
    with s2:
        if old_name and st.button("🗑 このジョブを消す"):
            cfg["jobs"] = [j for j in _jobs(cfg) if j.get("name") != old_name]
            _save(cfg)
            st.session_state.dl_view = "list"
            st.session_state.pop("dl_loads_of", None)
            st.rerun()


# ==========================================
# ▶ 画面3：実行
# ==========================================
elif st.session_state.dl_view == "run":
    jname = st.session_state.dl_job
    job = _find(cfg, jname)
    if not job:
        st.error("ジョブが見つかりません。")
        st.session_state.dl_view = "list"
        st.stop()

    if st.button("⬅ 一覧に戻る"):
        st.session_state.dl_view = "list"
        st.rerun()

    st.markdown(f"### 🗃 {jname}")
    folder = sms_runner.work_dir(WORK_ROOT, jname)

    # --- ① シートを更新 ---
    with st.container(border=True):
        theme.section_title("1️⃣", "SFコネクタでシートを更新する")
        _tabs = job.get("refresh_tabs", []) or []
        if job.get("refresh_robot") and _tabs:
            st.caption(f"使うロボット：**{job['refresh_robot']}**　"
                       f"／ 更新するシート：**{'、'.join(_tabs)}**（上から順に更新します）")
            # 📌 録画したときのURLではなく、このジョブに登録したスプシを開く。
            st.caption(f"開くスプレッドシート：{job['sheet_url']}")
            st.caption("ブラウザは1回だけ開いて、その中でシートを切り替えながら回します。"
                       "録画したときのURLは使いません（開く先はここに登録したスプシです）。")
            r1, r2 = st.columns([1, 1])
            with r2:
                _one = st.selectbox("お試し（1枚だけ更新してみる）",
                                    ["（使わない）"] + list(_tabs), key=f"dl_one_{jname}")
                _one = None if str(_one).startswith("（") else _one
                if st.button("🧪 この1枚だけ試す", use_container_width=True, disabled=not _one):
                    with st.spinner(f"「{_one}」だけ更新しています..."):
                        ok, log = sms_runner.run_sheet_refresh(job["refresh_robot"], folder,
                                                               tabs=[_one],
                                                               url=job["sheet_url"])
                    st.session_state[f"dl_ref_{jname}"] = {
                        "ok": ok, "log": log,
                        "表": sms_runner.parse_refresh_log(log, [_one])}
                    st.rerun()
            with r1:
                if st.button("🔄 ぜんぶ更新する", type="primary", use_container_width=True):
                    with st.spinner(f"{len(_tabs)}枚のシートを順に更新しています..."):
                        ok, log = sms_runner.run_sheet_refresh(job["refresh_robot"], folder,
                                                               tabs=_tabs,
                                                               url=job["sheet_url"])
                    st.session_state[f"dl_ref_{jname}"] = {
                        "ok": ok, "log": log,
                        "表": sms_runner.parse_refresh_log(log, _tabs)}
                    st.rerun()
            res = st.session_state.get(f"dl_ref_{jname}")
            if res:
                st.dataframe(pd.DataFrame(res["表"]), use_container_width=True, hide_index=True)
                if res["ok"]:
                    st.success("✅ ぜんぶ更新できました。")
                else:
                    st.error("❌ 途中で止まりました。上の表で、どのシートまで進んだか分かります。")
                with st.expander("実行ログ", expanded=not res["ok"]):
                    st.code(res["log"])
        else:
            st.info("このジョブは、シートの更新を**手作業**で行う設定です。")
            if job.get("sheet_url"):
                st.markdown(f"[📄 スプレッドシートを開く]({job['sheet_url']})")

    # --- ② 目で見て確認するシート ---
    #     「検討エラーリスト」のように、出方が決まっていないので自動判定できないもの。
    #     2行目以降に何か出ていたら、投入の前でいったん止めて人に見てもらう。
    _watch = job.get("watch_tabs", []) or []
    watch_ok = True
    if _watch:
        with st.container(border=True):
            theme.section_title("2️⃣", "目で見て確認するシート")
            st.caption("ここは自動で良し悪しを決められないところです。"
                       f"**{'、'.join(_watch)}** に何か出ていないか見ます。")
            wkey = f"dl_watch_{jname}"
            w1, w2 = st.columns([1, 3])
            with w1:
                if st.button("🔍 確認する", type="primary", use_container_width=True, disabled=not gc):
                    found = []
                    for t in _watch:
                        try:
                            heads, rows = _read_table(gc, job["sheet_url"], t)
                        except Exception as e:
                            found.append({"シート": t, "件数": -1, "見出し": [],
                                          "行": [], "メモ": f"読めませんでした：{str(e)[:120]}"})
                            continue
                        rows = [r for r in rows if any(str(x).strip() for x in r)]
                        found.append({"シート": t, "件数": len(rows), "見出し": heads,
                                      "行": rows[:200], "メモ": ""})
                    st.session_state[wkey] = found
                    st.rerun()
            with w2:
                if job.get("sheet_url"):
                    st.markdown(f"[📄 スプレッドシートを開いて対応する]({job['sheet_url']})")

            found = st.session_state.get(wkey)
            if found is None:
                st.caption("まだ確認していません。")
                watch_ok = not job.get("watch_block", True)
            else:
                _n = sum(f["件数"] for f in found if f["件数"] > 0)
                _bad = [f for f in found if f["件数"] != 0]
                for f in found:
                    if f["メモ"]:
                        st.warning(f"シート「{f['シート']}」：{f['メモ']}")
                    elif f["件数"] == 0:
                        st.success(f"✅ 「{f['シート']}」は空でした（対応することはありません）。")
                    else:
                        st.error(f"🛠 「{f['シート']}」に **{f['件数']}件** 出ています。"
                                 "中身を見て、スプレッドシートで対応してください。")
                        try:
                            _df = pd.DataFrame(
                                [(r + [""] * len(f["見出し"]))[:len(f["見出し"])] for r in f["行"]],
                                columns=[h or f"列{i + 1}" for i, h in enumerate(f["見出し"])])
                            st.dataframe(_df, use_container_width=True, hide_index=True)
                            st.download_button(
                                f"⬇️ 「{f['シート']}」をCSVで落とす",
                                data=_df.to_csv(index=False).encode("utf-8-sig"),
                                file_name=f"{f['シート']}_{sms_runner.today_stamp()}.csv",
                                mime="text/csv", key=f"dlw_dl_{f['シート']}")
                        except Exception as _e:
                            st.caption(f"（表にできませんでした：{str(_e)[:120]}）")
                if not _bad:
                    watch_ok = True
                else:
                    st.info("対応が終わったら、もう一度「🔍 確認する」を押してください。"
                            "スプレッドシートの行を消さない運用なら、下にチェックを入れれば先に進めます。")
                    watch_ok = st.checkbox(
                        f"上の {_n}件 は**確認して対応しました**（このまま投入に進みます）",
                        key=f"dl_watch_ok_{jname}")
                    if not job.get("watch_block", True):
                        watch_ok = True

    # --- ③ Salesforceへ投入 ---
    with st.container(border=True):
        theme.section_title("3️⃣" if _watch else "2️⃣", "Salesforceへ入れる（データローダーの代わり）")
        loads = job.get("loads", []) or []
        if not loads:
            st.warning("投入の設定がありません（設定画面の4️⃣で追加してください）。")
        elif not watch_ok:
            st.info("上の 2️⃣ の確認が終わると、投入のボタンが出ます"
                    "（見落としたまま投入してしまうのを防ぐためです）。")
        else:
            st.dataframe(pd.DataFrame([
                {"投入するシート": str(x.get("シート", "")),
                 "投入先": str(x.get("オブジェクト", "")),
                 "照合キー": str(x.get("照合キー", "")),
                 "マッピング": f"{len(x.get('マッピング', {}) or {})}項目"} for x in loads]),
                use_container_width=True, hide_index=True)

            n_try = st.number_input("お試し件数（先にこれだけ入れて確かめる）",
                                    min_value=1, max_value=200, value=5)
            b1, b2 = st.columns(2)
            do_try = b1.button(f"🧪 各{int(n_try)}件だけ入れてみる", use_container_width=True)
            agree = st.checkbox("**全件を Salesforce に反映します**（UPSERTなので上書きされます）")
            do_all = b2.button("🚀 全件を投入する", type="primary", use_container_width=True,
                               disabled=not agree)

            if do_try or do_all:
                limit = int(n_try) if do_try else 0
                out, prog = [], st.progress(0.0)
                for i, ld in enumerate(loads):
                    with st.spinner(f"「{ld.get('シート')}」を投入しています...（{i + 1}/{len(loads)}）"):
                        r = _push_one(gc, job["sheet_url"], str(ld.get("シート", "")),
                                      str(ld.get("オブジェクト", "")), str(ld.get("照合キー", "")),
                                      ld.get("マッピング", {}) or {}, limit=limit)
                    out.append({"シート": str(ld.get("シート", "")), "結果": r["結果"],
                                "成功": r["ok"], "失敗": r["ng"],
                                "_errors": r["errors"], "_obj": r["オブジェクト"]})
                    prog.progress((i + 1) / len(loads))
                st.session_state[f"dl_push_{jname}"] = out
                st.rerun()

            pushed = st.session_state.get(f"dl_push_{jname}")
            if pushed:
                st.dataframe(pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")}
                                           for r in pushed]),
                             use_container_width=True, hide_index=True)
                for r in pushed:
                    if r.get("_errors"):
                        st.markdown(f"**❌ {r['シート']} の失敗（{r['失敗']}件）**")
                        sf_ui.render_errors(r["_errors"], r.get("_obj", ""),
                                            key_prefix=f"dlerr_{r['シート']}")

    st.divider()
    st.caption("💻 シートの更新はブラウザを開くため、**担当者のPCで開いているとき**だけ動きます。"
               "Salesforceへの投入はクラウドからでも動きます。")
