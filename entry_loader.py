"""
🗃 エントリー後のデータローダー（エントリー業務自動化から呼ぶ画面）

エントリーが終わった案件に「エントリー済み」を書き戻すための投入。
**エントリー業務のあとに毎回やること**なので、レポート更新と同じ場所（エントリーの画面）に置く。

【「🗃 データローダー自動化」ページとの違い】
あちらは「このスプシを更新して、この投入をする」という**ジョブ**の単位で、
スプレッドシートは**1枚**に紐づく。
こちらはエントリー後の後始末なので、**何枚ものスプシにまたがった投入**を
ひとまとまり（＝投入セット）にして、ボタン1つで通せるようにする。
レポート更新（`report_refresh.py`）と同じ形にそろえてある。

【投入の中身は1か所に寄せる】
「どのシートを・どこへ・どのキーで・どの対応表で」の編集は `sf_ui.load_editor`、
実際の投入は `sf_ui.push_sheet` を使う（SMS送信・データローダーと同じもの）。
別々に書くと、片方だけ直して食い違うため。
"""
import json
import uuid

import pandas as pd
import streamlit as st

import salesforce_loader as sfl
import sf_ui
import theme

SETTINGS_ID = "__entry_loads__"   # Supabase の予約行（ロボット一覧には出ない）


# ==========================================
# 💾 設定の読み書き
# ==========================================
def _load(supabase) -> dict:
    try:
        res = supabase.table("merchants").select("*").eq("id", SETTINGS_ID).execute()
        if res.data:
            return res.data[0].get("config_json", {}) or {}
    except Exception as e:
        st.error(f"設定を読み込めませんでした: {e}")
    return {}


def _save(supabase, cfg: dict):
    supabase.table("merchants").upsert({
        "id": SETTINGS_ID, "name": "（エントリー後の投入の設定）", "is_active": False,
        "connector_type": "settings", "config_json": cfg}).execute()


def _sets(cfg) -> list:
    return cfg.get("sets", []) or []


def _find(cfg, name):
    for s in _sets(cfg):
        if s.get("name") == name:
            return s
    return None


# ==========================================
# 📄 スプレッドシート
# ==========================================
SHEET_TIMEOUT_SEC = 30   # ⏱ スプシへの問い合わせを、いつまでも待たないための上限


@st.cache_resource(show_spinner=False)
def _build_gspread_client(sa_json: str):
    """⚠️ 待ち時間の上限を必ず付ける（既定は無制限で、画面が固まったままになる）。"""
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    try:
        gc.http_client.set_timeout(SHEET_TIMEOUT_SEC)
    except Exception:
        pass
    return gc


def _gc():
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
def _tab_names(_gc, sheet_url: str):
    sh = (_gc.open_by_url(sheet_url) if sheet_url.startswith("http")
          else _gc.open_by_key(sheet_url))
    return [w.title for w in sh.worksheets()]


def _label_of(ld: dict, i: int) -> str:
    return str(ld.get("label") or "").strip() or f"スプレッドシート{i + 1}"


def _new_load(src: dict = None) -> dict:
    """編集中の1件（＝投入ひとつ）。

    ⚠️ **入力欄のキーは、行の番号ではなく行そのものに結びつける。**
       番号で付けると、途中の行を消したときに下の行が番号を引き継ぎ、
       Streamlit が覚えている前の入力値がそのまま出る＝
       **消した行と違う行が消えたように見える**（レポート更新で実際に起きた）。
    """
    row = dict(src or {})
    row.setdefault("label", "")
    row.setdefault("url", "")
    row.setdefault("シート", "")
    row.setdefault("オブジェクト", "Opportunity")
    row.setdefault("照合キー", "Id")
    row.setdefault("マッピング", {})
    row["_uid"] = uuid.uuid4().hex[:8]
    return row


def _rows_of(one_set: dict) -> list:
    return list(one_set.get("loads", []) or [])


# ==========================================
# ▶ 実行
# ==========================================
def _check_one(gc, ld: dict) -> str:
    """投入する前に確かめる（送らない）。

    ・マッピングの列が、そのシートの見出しに実在するか
    ・その項目が Salesforce に実在するか
    どちらも `sf_ui.push_sheet` が実行時にも確かめるが、
    **押す前に分かる**ほうが直しやすいので、ここでも同じ2つを見る。
    """
    mapping = dict(ld.get("マッピング", {}) or {})
    if not (ld.get("url") and ld.get("シート") and mapping):
        return "⚠️ スプシ・シート・マッピングのどれかが未設定です"
    heads = sf_ui.sheet_headers(gc, ld["url"], ld["シート"]) if gc else []
    if not heads:
        return "⚠️ シートの見出しを読めませんでした（URLとシート名を確かめてください）"
    missing = [k for k in mapping if k not in heads]
    if missing:
        return "❌ シートに無い列がマッピングにあります：" + "／".join(missing[:5])
    try:
        sf = sfl.connect()
    except Exception as e:
        return f"❌ Salesforceに接続できません: {str(e)[:100]}"
    bad, _ = sfl.check_mapping(sf, ld.get("オブジェクト", ""), mapping)
    if bad:
        return ("❌ Salesforceに無い項目があります："
                + "／".join(str(b.get("スプシの列", b)) for b in bad[:5]))
    return f"✅ 大丈夫です（{len(mapping)}項目）"


def _render_run(supabase, cfg, name: str):
    one_set = _find(cfg, name)
    if not one_set:
        st.error("その投入セットが見つかりませんでした。")
        if st.button("⬅ 一覧に戻る", key="el_run_back0"):
            st.session_state.el_view = "list"
            st.rerun()
        return
    if st.button("⬅ 一覧に戻る", key="el_run_back"):
        st.session_state.el_view = "list"
        st.rerun()

    st.markdown(f"### ▶ 「{name}」を Salesforce に入れる")
    if one_set.get("memo"):
        st.caption(one_set["memo"])

    loads = _rows_of(one_set)
    if not loads:
        st.info("この投入セットには、まだ中身が登録されていません。"
                "「✏️ 設定」から登録してください。")
        return

    gc = _gc()
    if not gc:
        st.error("サービスアカウント（`GOOGLE_SERVICE_ACCOUNT_JSON`）が未設定のため、"
                 "シートを読めません。")
        return

    df = pd.DataFrame([{"投入する": True, "スプレッドシート": _label_of(ld, i),
                        "シート": ld.get("シート", ""),
                        "投入先": ld.get("オブジェクト", ""),
                        "照合キー": ld.get("照合キー", ""),
                        "項目数": len(ld.get("マッピング", {}) or {})}
                       for i, ld in enumerate(loads)])
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"el_pick_{name}",
        disabled=["スプレッドシート", "シート", "投入先", "照合キー", "項目数"],
        column_config={"投入する": st.column_config.CheckboxColumn(
            "投入する", help="チェックを外すと、その投入は行いません。")})
    picked = [ld for ld, keep in zip(loads, list(edited["投入する"])) if keep]

    st.caption("📌 マッピングに書いた列だけを送ります。書いていない列は、シートに何列あっても触りません。")

    # 🩺 押す前に確かめる（送らない）
    if st.button("🩺 シートと照らし合わせる（送りません）", key=f"el_chk_{name}",
                 use_container_width=True, disabled=not picked):
        with st.spinner("確かめています..."):
            st.session_state[f"el_chk_res_{name}"] = [
                {"スプレッドシート": _label_of(ld, i), "シート": ld.get("シート", ""),
                 "結果": _check_one(gc, ld)}
                for i, ld in enumerate(loads) if ld in picked]
        st.rerun()
    _chk = st.session_state.get(f"el_chk_res_{name}")
    if _chk:
        st.dataframe(pd.DataFrame(_chk), use_container_width=True, hide_index=True)

    st.markdown("---")
    t1, t2 = st.columns([1, 2])
    with t1:
        _limit = st.number_input("お試しで入れる件数", min_value=1, max_value=200, value=5,
                                 key=f"el_lim_{name}")
    with t2:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button(f"🧪 お試し（先頭{int(_limit)}件だけ入れてみる）", use_container_width=True,
                     disabled=not picked, key=f"el_try_{name}"):
            st.session_state[f"el_res_{name}"] = _push(gc, loads, picked, int(_limit))
            st.rerun()

    # ⚠️ 全件投入は取り消せない。チェックを入れないと押せないようにする。
    _ok = st.checkbox("内容を確かめました。**全件を Salesforce に入れます**",
                      key=f"el_confirm_{name}")
    if st.button(f"🚀 チェックした{len(picked)}件を全件投入する", type="primary",
                 use_container_width=True, disabled=not (picked and _ok),
                 key=f"el_go_{name}"):
        st.session_state[f"el_res_{name}"] = _push(gc, loads, picked, 0)
        st.rerun()

    res = st.session_state.get(f"el_res_{name}")
    if res:
        st.markdown("#### 結果")
        st.dataframe(pd.DataFrame(res["表"]), use_container_width=True, hide_index=True)
        for i, e in enumerate(res["errors"]):
            if e["errors"]:
                st.markdown(f"**{e['見出し']}：入らなかった行**")
                sf_ui.render_errors(e["errors"], e.get("オブジェクト", ""),
                                    key_prefix=f"el_err_{name}_{i}")


def _push(gc, loads, picked, limit: int) -> dict:
    """選ばれた投入を、上から順に Salesforce へ入れる。"""
    table, errs = [], []
    for i, ld in enumerate(loads):
        if ld not in picked:
            continue
        head = f"{_label_of(ld, i)} ／ {ld.get('シート', '')}"
        with st.spinner(f"{head} を投入しています..."):
            out = sf_ui.push_sheet(gc, ld.get("url", ""), ld.get("シート", ""),
                                   ld.get("オブジェクト", ""), ld.get("照合キー", ""),
                                   dict(ld.get("マッピング", {}) or {}), limit=limit)
        table.append({"スプレッドシート": _label_of(ld, i), "シート": ld.get("シート", ""),
                      "投入先": out.get("オブジェクト", ""), "成功": out.get("ok", 0),
                      "失敗": out.get("ng", 0), "結果": out.get("結果", "")})
        errs.append({"見出し": head, "errors": out.get("errors", []) or [],
                     "オブジェクト": out.get("オブジェクト", "")})
    return {"表": table, "errors": errs}


# ==========================================
# ⚙️ 設定
# ==========================================
EDIT_KEY = "el_edit_loads"   # 保存を押すまでの編集内容（セッションに置く）


def _render_edit(supabase, cfg, old_name: str):
    one_set = _find(cfg, old_name) or {"name": "", "memo": "", "loads": []}

    if st.button("⬅ 一覧に戻る", key="el_edit_back"):
        st.session_state.el_view = "list"
        st.session_state.pop(EDIT_KEY, None)
        st.rerun()

    _title = "投入セットを追加" if not old_name else f"「{old_name}」の設定"
    st.markdown(f"### ⚙️ {_title}")
    st.caption("エントリーが終わった案件に「エントリー済み」を書き戻すなど、"
               "**エントリーのあとにやる投入**をまとめて登録します。"
               "スプレッドシートは何枚にまたがっていても構いません。")

    with st.container(border=True):
        theme.section_title("1️⃣", "このセットの名前")
        name = st.text_input("名前", value=one_set.get("name", ""),
                             placeholder="例：エントリー済みの書き戻し", key="el_name")
        memo = st.text_input("メモ（何のための投入か）", value=one_set.get("memo", ""),
                             key="el_memo")

    if EDIT_KEY not in st.session_state:
        _init = [_new_load(x) for x in (one_set.get("loads", []) or [])]
        st.session_state[EDIT_KEY] = _init or [_new_load()]
    loads = st.session_state[EDIT_KEY]

    gc = _gc()
    with st.container(border=True):
        theme.section_title("2️⃣", "投入の中身（何件でも足せます）")
        if not gc:
            st.warning("サービスアカウント（`GOOGLE_SERVICE_ACCOUNT_JSON`）が未設定のため、"
                       "シート名や列を読めません。先に設定してください。")
        for i, ld in enumerate(loads):
            if "_uid" not in ld:
                ld["_uid"] = uuid.uuid4().hex[:8]
            uid = ld["_uid"]
            with st.container(border=True):
                c1, c2 = st.columns([4, 1])
                with c1:
                    ld["label"] = st.text_input(
                        "呼び名（画面に出る名前）", value=ld.get("label", ""),
                        placeholder="例：LL貼り付け先", key=f"el_lb_{uid}")
                with c2:
                    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                    if st.button("🗑 外す", key=f"el_rm_{uid}", use_container_width=True):
                        loads.pop(i)
                        st.rerun()
                ld["url"] = st.text_input(
                    "スプレッドシートのURL", value=ld.get("url", ""),
                    placeholder="https://docs.google.com/spreadsheets/d/...",
                    key=f"el_url_{uid}")
                tabs = []
                if gc and ld["url"].strip():
                    try:
                        tabs = _tab_names(gc, ld["url"].strip())
                    except Exception as e:
                        st.error(f"スプレッドシートを開けませんでした：{str(e)[:160]}")
                # 「どのシートを・どこへ・どのキーで・どの対応表で」は共通の編集画面を使う
                # （SMS送信・データローダーと同じもの。別々に持つと食い違うため）
                sf_ui.load_editor(gc, ld["url"].strip(), tabs, ld, f"el_ed_{uid}")
        if st.button("＋ 投入を足す", key="el_add"):
            loads.append(_new_load())
            st.rerun()

    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button("💾 保存する", type="primary", use_container_width=True, key="el_save"):
            if not name.strip():
                st.error("名前を入れてください。")
            elif name.strip() != old_name and _find(cfg, name.strip()):
                st.error(f"「{name.strip()}」はすでにあります。別の名前にしてください。")
            else:
                new = {"name": name.strip(), "memo": memo.strip(),
                       "loads": [{"label": str(x.get("label", "")).strip(),
                                  "url": str(x.get("url", "")).strip(),
                                  "シート": str(x.get("シート", "")).strip(),
                                  "オブジェクト": str(x.get("オブジェクト", "")).strip(),
                                  "照合キー": str(x.get("照合キー", "")).strip(),
                                  "マッピング": dict(x.get("マッピング", {}) or {})}
                                 for x in loads if str(x.get("url", "")).strip()]}
                items = [s for s in _sets(cfg) if s.get("name") != old_name]
                items.append(new)
                cfg["sets"] = items
                _save(supabase, cfg)
                st.session_state.pop(EDIT_KEY, None)
                st.session_state.el_view = "list"
                st.toast(f"「{new['name']}」を保存しました", icon="💾")
                st.rerun()
    with s2:
        st.caption("※ 「💾 保存する」を押すまで、ここでの変更は残りません。")


# ==========================================
# 📋 一覧
# ==========================================
def _render_list(supabase, cfg):
    st.caption("エントリーが終わった案件に、**エントリー済みの内容を Salesforce に入れる**ための画面です。"
               "スプレッドシートが何枚に分かれていても、まとめて1回で投入できます"
               "（1つずつ選んで投入することもできます）。")

    _, add = st.columns([4, 1])
    with add:
        if st.button("＋ 投入セットを追加", use_container_width=True, key="el_add_set"):
            st.session_state.el_view = "edit"
            st.session_state.el_set = ""
            st.session_state.pop(EDIT_KEY, None)
            st.rerun()

    items = _sets(cfg)
    if not items:
        st.info("まだ投入セットがありません。"
                "「＋ 投入セットを追加」から登録してください。")
        return

    for s in items:
        nm = s.get("name", "")
        with st.container(border=True):
            st.markdown(f"#### 🗃 {nm}")
            if s.get("memo"):
                st.caption(s["memo"])
            _labels = "／".join(_label_of(x, i) for i, x in enumerate(_rows_of(s)))
            st.caption(f"投入 {len(_rows_of(s))}件　{_labels}")
            c1, c2, c3 = st.columns([1.4, 1, 1])
            with c1:
                if st.button("▶ 投入する", key=f"el_go_btn_{nm}", type="primary",
                             use_container_width=True):
                    st.session_state.el_view = "run"
                    st.session_state.el_set = nm
                    st.rerun()
            with c2:
                if st.button("✏️ 設定", key=f"el_ed_btn_{nm}", use_container_width=True):
                    st.session_state.el_view = "edit"
                    st.session_state.el_set = nm
                    st.session_state.pop(EDIT_KEY, None)
                    st.rerun()
            with c3:
                dk = f"el_del_{nm}"
                if not st.session_state.get(dk):
                    if st.button("🗑 削除", key=f"el_delbtn_{nm}", use_container_width=True):
                        st.session_state[dk] = True
                        st.rerun()
                else:
                    st.warning(f"「{nm}」を消しますか？")
                    d1, d2 = st.columns(2)
                    with d1:
                        if st.button("はい", key=f"el_dy_{nm}", use_container_width=True):
                            cfg["sets"] = [x for x in items if x.get("name") != nm]
                            _save(supabase, cfg)
                            st.session_state.pop(dk, None)
                            st.rerun()
                    with d2:
                        if st.button("やめる", key=f"el_dn_{nm}", use_container_width=True):
                            st.session_state.pop(dk, None)
                            st.rerun()


def render(supabase):
    """エントリー後の投入の画面（エントリー業務自動化のページから呼ぶ）。"""
    if "el_view" not in st.session_state:
        st.session_state.el_view = "list"
    cfg = _load(supabase)
    view = st.session_state.el_view
    if view == "run":
        _render_run(supabase, cfg, st.session_state.get("el_set", ""))
    elif view == "edit":
        _render_edit(supabase, cfg, st.session_state.get("el_set", ""))
    else:
        _render_list(supabase, cfg)
