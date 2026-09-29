"""
🛑 変更・キャンセル自動化（LL／N）

  ① SFコネクタで「BOX」を更新 → ② キャリアごとのシートを見る（振り分け漏れ・利用開始が近い案件）
  → ③ GASでメールの下書きを作る（送るのは人・Gmailから） → ④ 依頼した案件の付箋を「完了」にする

⭐ 振り分けはスプシの数式、メールの中身はスプシのGAS。アプリは「押す・見せる・確かめる」だけ。
   中身は `cancel.py`（LL と N は同じ作りで、設定だけが違う）。
"""
import time

import pandas as pd
import streamlit as st
from supabase import create_client, Client

import auto_jobs
import cancel
import characters as ch
import gas_deploy
import sms_runner
import theme

st.set_page_config(page_title="変更・キャンセル - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check
secrets_check.check()
theme.brand_sidebar(active="manage")

c = ch.get("manage")
theme.page_header("🛑", "変更・キャンセル自動化",
                  "変更・キャンセルの付箋が付いた案件を、キャリアへ依頼して「完了」にするまで。",
                  color=c["color"])


@st.cache_resource
def init_connection():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()


@st.cache_resource(show_spinner=False)
def _build_gc(sa_json: str):
    return auto_jobs.gspread_client(sa_json)


gc = _build_gc(st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")) \
    if st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "") else None
if not gc:
    st.error("🔑 `GOOGLE_SERVICE_ACCOUNT_JSON` が未設定です。「⚙️ その他設定」で接続キーを確かめてください。")
    st.stop()


def _load() -> dict:
    try:
        return auto_jobs.load_row(supabase, cancel.SETTINGS_ID)
    except Exception as e:
        st.error(str(e))
        return {}


def _save_set(name: str, part: dict) -> dict:
    """⚠️ 書く直前に読み直して、そのセットの触ったところだけ変える（別のPC・別のセットを消さない）。"""
    latest = _load()
    sets = dict(latest.get("sets") or {})
    cur = dict(sets.get(name) or {})
    cur.update(part)
    sets[name] = cur
    latest["sets"] = sets
    supabase.table("merchants").upsert({
        "id": cancel.SETTINGS_ID, "name": "（変更・キャンセルの設定）", "is_active": False,
        "connector_type": "settings", "config_json": latest}).execute()
    return cur


def _state(raw: dict) -> dict:
    """きょうの進み具合。日が変われば空から（下書き・完了は日ごとの話）。"""
    s = dict(raw.get("state") or {})
    if s.get("date") != cancel.today_str():
        s = {"date": cancel.today_str(), "drafts": [], "done": {}}
    s.setdefault("drafts", [])
    s.setdefault("done", {})
    return s


@st.cache_data(ttl=60, show_spinner=False)
def _read_all(_gc, url: str, box_tab: str, sheets: tuple, ver: int):
    return cancel.read_all(_gc, url, box_tab, list(sheets))


def _settings(name: str, raw: dict, sc: dict):
    k = f"cx_{name}_"
    st.markdown("##### 📄 スプレッドシート")
    url = st.text_input("スプレッドシートのURL", value=str(raw.get("sheet_url", "") or ""), key=k + "url",
                        help="BOX（SFコネクタのレポート）とキャリアごとのシートが入っているスプシ")
    c1, c2 = st.columns(2)
    box_tab = c1.text_input("SFレポートのシート（SFコネクタで更新する）", value=sc["box_tab"], key=k + "box")
    sender_cell = c2.text_input("担当者名を書くセル（GASがメールの名乗りに使う）",
                                value=sc.get("sender_cell", ""), key=k + "sender",
                                help="例：ボタン!B17。空ならアプリからは書きません")
    c3, c4 = st.columns(2)
    check_field = c3.text_input("付箋チェックの項目（API名）", value=sc.get("check_field", ""), key=k + "chk",
                                help="「完了」にする項目。LL＝Lc__c（L-付箋：チェック）／N＝Stickycheck__c")
    remark = c4.text_input("備考に「◯◯依頼済」を足す項目（商材種別=API名）",
                           value=", ".join(f"{a}={b}" for a, b in (sc.get("remark_fields") or {}).items()),
                           key=k + "rmk", help="例：ガス=GasRemarks__c, 電気＆ガス=GasRemarks__c。"
                                               "空なら備考には何も足しません")
    date_cols = st.text_input("利用開始が近いか見る列（BOXの見出し・カンマ区切り）",
                              value=", ".join(sc.get("date_cols") or []), key=k + "dates",
                              help="今日〜3営業日後（土日祝を除く）に入っている案件を、下書きの前に名指しします")

    st.markdown("##### ✉️ GASが下書きを作るもの")
    st.caption("関数はスプシのGASにあるものをそのまま呼びます。シートは、中身があるかを見るためのものです"
               "（カンマ区切り）。")
    ddf = pd.DataFrame(sc.get("drafts") or [], columns=["名前", "関数", "シート"])
    ded = st.data_editor(ddf, num_rows="dynamic", hide_index=True, use_container_width=True, key=k + "drafts")
    manual = st.text_input("✋ 人が連携するシート（GASがメールを作らないもの・カンマ区切り）",
                           value=", ".join(sc.get("manual_sheets") or []), key=k + "manual")

    st.markdown("##### 🤖 GAS")
    gas = gas_deploy.render(k + "gas", {
        "gas_script_url": raw.get("gas_script_url", ""), "gas_url": raw.get("gas_url", ""),
        "gas_token": raw.get("gas_token", ""), "gas_deployment_id": raw.get("gas_deployment_id", "")})

    if st.button("💾 保存", type="primary", key=k + "save"):
        rf = {}
        for part in cancel.split_names(remark):
            if "=" in part:
                a, b = part.split("=", 1)
                if a.strip() and b.strip():
                    rf[a.strip()] = b.strip()
        drafts = [{"名前": str(r.get("名前") or "").strip(), "関数": str(r.get("関数") or "").strip(),
                   "シート": str(r.get("シート") or "").strip()}
                  for r in ded.to_dict("records") if str(r.get("関数") or "").strip()]
        _save_set(name, {
            "sheet_url": url.strip(), "box_tab": box_tab.strip(), "sender_cell": sender_cell.strip(),
            "check_field": check_field.strip(), "remark_fields": rf,
            "date_cols": cancel.split_names(date_cols), "drafts": drafts,
            "manual_sheets": cancel.split_names(manual),
            "gas_script_url": str(gas.get("gas_script_url", "") or ""),
            "gas_url": str(gas.get("gas_url", "") or ""), "gas_token": str(gas.get("gas_token", "") or ""),
            "gas_deployment_id": str(gas.get("gas_deployment_id", "") or "")})
        st.cache_data.clear()
        st.success("保存しました。")
        st.rerun()
    st.caption("⚠️ このスプシを、サービスアカウントに**編集者**として共有しておいてください"
               "（担当者名のセルに書くため）。")


def _render(name: str):
    raw = dict((_load().get("sets") or {}).get(name) or {})
    sc = cancel.merged(name, raw)
    cols = sc["cols"]
    state = _state(raw)
    k = f"cx_{name}_"
    url = str(raw.get("sheet_url", "") or "").strip()

    with st.expander("⚙️ 設定", expanded=not url):
        _settings(name, raw, sc)
    if not url:
        st.info("上の「⚙️ 設定」でスプレッドシートのURLを入れてください。")
        return

    # ── ① 更新 ──
    with st.container(border=True):
        theme.section_title("1️⃣", f"SFレポート（{sc['box_tab']}）を最新にする")
        last = str(raw.get("last_refresh", "") or "")
        if last.startswith(time.strftime("%Y/%m/%d")):
            st.caption(f"🕒 最後に更新：{last}")
        else:
            st.warning(f"⚠️ きょうはまだ更新していません（最後：{last or 'なし'}）。古い中身のまま依頼しないように、先に更新してください。")
        if st.button("🔄 SFコネクタで更新する", key=k + "refresh", type="primary"):
            robot = str(raw.get("refresh_robot", "") or auto_jobs.DEFAULT_REFRESH_ROBOT)
            urls = sms_runner.tab_urls_for(url, [sc["box_tab"]], auto_jobs.tab_gids(gc, url))
            with st.spinner("SFコネクタで更新しています（1〜2分）..."):
                ok, log = sms_runner.run_sheet_refresh(robot, sms_runner.work_dir("変更キャンセル", name),
                                                       tabs=[sc["box_tab"]], tab_urls=urls, url=url)
            if ok:
                _save_set(name, {"last_refresh": time.strftime("%Y/%m/%d %H:%M")})
                st.session_state[k + "ver"] = st.session_state.get(k + "ver", 0) + 1
                st.success("✅ 更新しました。")
                st.rerun()
            else:
                st.error("❌ 更新できませんでした：" + (sms_runner.stop_reason(log) or log[-300:]))

    # ── ② 読む ──
    draft_sheets = []
    for d in sc.get("drafts") or []:
        draft_sheets += cancel.split_names(d.get("シート"))
    manual_sheets = list(sc.get("manual_sheets") or [])
    try:
        head, box, sheets = _read_all(gc, url, sc["box_tab"], tuple(draft_sheets + manual_sheets),
                                      st.session_state.get(k + "ver", 0))
    except Exception as e:
        st.error(f"❌ シートを読めませんでした：{str(e)[:200]}")
        return
    box = [r for r in box if str(r.get(cols["id"], "") or "").strip()]
    listed = cancel.where_listed(box, sheets, cols)
    missing = [r for r in box if cancel.needs_carrier(r, cols) and not listed.get(str(r[cols["id"]]).strip())]
    urgent = cancel.urgent_rows(box, sc.get("date_cols"))

    with st.container(border=True):
        theme.section_title("2️⃣", "キャリアごとの中身を見る")
        m1, m2, m3 = st.columns(3)
        m1.metric("付箋の付いた案件", f"{len(box)}件")
        m2.metric("どのシートにも載っていない", f"{len(missing)}件")
        m3.metric("利用開始が3営業日以内", f"{len(urgent)}件")
        if missing:
            st.warning("⚠️ **キャリアへの依頼が要るのに、どのシートにも載っていない案件**があります。"
                       "スプシの数式が拾えていないキャリアか、下の「GASが外す行」になっています。手で連携してください。")
            st.dataframe(pd.DataFrame([{"案件番号": r.get(cols["no"], ""), "名前": r.get(cols["name"], ""),
                                        "電力": r.get(cols.get("power", ""), ""),
                                        "ガス": r.get(cols.get("gas", ""), ""),
                                        "商材": r.get(cols["kind"], ""), "内容": r.get(cols["content"], "")}
                                       for r in missing]), hide_index=True, use_container_width=True)
        if urgent:
            st.error("🚨 **利用開始（立会）が今日〜"
                     f"{cancel.cutoff().strftime('%m/%d')}の案件**があります。メールだけで間に合うか確かめてください。")
            st.dataframe(pd.DataFrame([{"日付": d.strftime("%m/%d"), "どの日": col, "案件番号": r.get(cols["no"], ""),
                                        "名前": r.get(cols["name"], ""), "内容": r.get(cols["content"], "")}
                                       for r, col, d in urgent]), hide_index=True, use_container_width=True)

        def _sheet_block(names, label):
            for n in names:
                d = sheets.get(n) or {}
                if d.get("error"):
                    st.markdown(f"- ❌ **{n}**：読めませんでした（{d['error']}）")
                    continue
                cnt = len(d.get("rows") or [])
                if d.get("dropped"):
                    st.error(f"❌ **{n}**：中身のある行が {len(d['dropped'])}件 ありますが、"
                             f"1列目（{(d.get('head') or ['?'])[0]}）が空かエラーなので、**GASはメールに入れません**。"
                             "スプシの数式（IDを引くところ）を確かめてください。")
                    st.dataframe(pd.DataFrame(d["dropped"], columns=d["head"]), hide_index=True,
                                 use_container_width=True)
                if not cnt:
                    st.markdown(f"- {label} **{n}**：0件")
                    continue
                with st.expander(f"{label} {n}：{cnt}件", expanded=(label == "✋")):
                    st.dataframe(pd.DataFrame(d["rows"], columns=d["head"]), hide_index=True,
                                 use_container_width=True)

        st.markdown("**✉️ GASがメールを作るシート**")
        _sheet_block(draft_sheets, "✉️")
        st.markdown("**✋ 人が連携するシート**（メールは作られません）")
        _sheet_block(manual_sheets, "✋")

    # ── ③ 下書き ──
    with st.container(border=True):
        theme.section_title("3️⃣", "メールの下書きを作る（GAS）")
        drafts = [d for d in (sc.get("drafts") or []) if str(d.get("関数", "")).strip()]
        who_now = cancel.read_sender(gc, url, sc.get("sender_cell", "")) if sc.get("sender_cell") else ""
        who = st.text_input("担当者名（メールの「株式会社ライフアップの◯◯」・備考の名前）", value=who_now,
                            key=k + "who")
        if not drafts:
            st.caption("このセットには、GASが下書きを作るものがありません。")
        elif not str(raw.get("gas_url", "") or "").strip():
            st.warning("⚠️ GASがまだ入っていません。「⚙️ 設定 → 🤖 GAS」から入れてください。")
        else:
            def _has(d):
                return any((sheets.get(n) or {}).get("rows") for n in cancel.split_names(d.get("シート")))
            labels = {f"{d['名前']}（{d['関数']}）": d for d in drafts}
            pick = st.multiselect("作る下書き（中身のあるものだけ最初から選んであります）", list(labels),
                                  default=[x for x, d in labels.items() if _has(d)], key=k + "pick")
            st.caption("📮 下書きは **GASを公開したアカウントのGmail** に入ります。中身を確かめてから、Gmailで送ってください。"
                       "ライフイン24のExcelは、Driveのきょうのフォルダが無いと作られません（GASが黙って飛ばします）。")
            ok_to = True
            if state["drafts"]:
                last = state["drafts"][-1]
                st.warning(f"⚠️ きょうは {last.get('at', '')} に、もう下書きを作っています（{last.get('by', '')}）。"
                           "もう一度作ると、同じ下書きが2通できます。")
                ok_to = st.checkbox("同じ下書きがもう一度できてもよい", key=k + "again")
            if urgent:
                ok_to = st.checkbox("🚨 利用開始が近い案件があることを確かめた", key=k + "urg") and ok_to
            if st.button("✉️ 下書きを作る", type="primary", key=k + "mk",
                         disabled=not (pick and ok_to and who.strip())):
                try:
                    if sc.get("sender_cell") and who.strip() != who_now:
                        cancel.write_sender(gc, url, sc["sender_cell"], who)
                    with st.spinner("GASで下書きを作っています..."):
                        ok, data = cancel.make_drafts(raw, [labels[x]["関数"] for x in pick])
                except Exception as e:
                    ok, data = False, str(e)
                if ok:
                    state["drafts"].append({"at": time.strftime("%H:%M"), "by": who.strip(),
                                            "funcs": [labels[x]["関数"] for x in pick]})
                    _save_set(name, {"state": state})
                    st.success("✅ 下書きを作りました。Gmailの「下書き」を確かめてから送ってください。")
                else:
                    st.error(f"❌ 下書きを作れませんでした：{str(data)[:300]}")

    # ── ④ 完了 ──
    with st.container(border=True):
        theme.section_title("4️⃣", "依頼した案件の付箋を「完了」にする（Salesforce）")
        if not box:
            st.caption("付箋の付いた案件はありません。")
            return
        who = str(st.session_state.get(k + "who", "") or "").strip()
        rf = sc.get("remark_fields") or {}
        df = pd.DataFrame([{
            "完了にする": False,
            "済": state["done"].get(str(r[cols["id"]]).strip(), ""),
            "案件番号": r.get(cols["no"], ""), "名前": r.get(cols["name"], ""),
            "商材": r.get(cols["kind"], ""), "内容": r.get(cols["content"], ""),
            "対応先": r.get(cols["to"], ""), "載っているシート": "、".join(listed.get(str(r[cols["id"]]).strip()) or []),
            "いまのチェック": r.get(cols["check"], ""),
            "備考に足す": (cancel.remark_text(r, cols, who) if cancel.remark_field(r, cols, rf) else ""),
            "_id": str(r[cols["id"]]).strip()} for r in box])
        st.caption("メールを送った・キャリアへ連携した案件にチェックを入れてください。送るのは**付箋チェックの1項目だけ**で、"
                   "備考は**うしろに1行足す**だけです（上書きしません）。")
        ed = st.data_editor(df, hide_index=True, use_container_width=True, key=k + "done_" + str(len(state["done"])),
                            column_config={"_id": None},
                            disabled=[c_ for c_ in df.columns if c_ != "完了にする"])
        chosen = set(x for x, v in zip(ed["_id"], ed["完了にする"]) if v)
        if not who:
            st.caption("⚠️ 備考に書く名前が空です。3️⃣の「担当者名」を入れてください。")
        if st.button(f"✅ 選んだ {len(chosen)}件 を「完了」にする", type="primary", key=k + "push",
                     disabled=not chosen):
            rows = [r for r in box if str(r[cols["id"]]).strip() in chosen]
            with st.spinner("Salesforceに入れています..."):
                try:
                    res = cancel.mark_done(sc, rows, who)
                except Exception as e:
                    st.error(f"❌ {str(e)[:300]}")
                    res = []
            for r in res:
                if r["完了"] == "✅":
                    state["done"][r["案件ID"]] = "✅ " + time.strftime("%H:%M")
            if res:
                _save_set(name, {"state": state})
                st.dataframe(pd.DataFrame(res).drop(columns=["案件ID"]), hide_index=True, use_container_width=True)
                st.caption("次に1️⃣で更新すると、完了にした案件はレポートから外れます。")


tabs = st.tabs(["⚡ LL（電気・ガス）", "🌐 N（ネット）"])
with tabs[0]:
    _render("LL")
with tabs[1]:
    _render("N")
