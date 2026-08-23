"""
☁️ Salesforce 投入パネル（画面部品）

進捗反映・開通反映など、複数のページから同じ形で使えるようにまとめたもの。
「どのシートを・どのオブジェクトに・どのキーで入れるか」を表で持ち、
マッピング（スプシの列名 → Salesforceの項目API名）も表で編集できる。

設定は設定スプレッドシートの2つのタブに保存する：
  ・投入設定  … 1行＝1つの投入（例：N-ドコモ／LL一括）
  ・マッピング … 1行＝1項目（投入名ごとに複数行）

CRM が将来変わっても、実際に投入する処理（salesforce_loader.py）だけ差し替えれば済むよう、
画面はここ、通信はあちら、と分けてある。
"""

import io
import pandas as pd
import streamlit as st

import salesforce_loader as sfl

LOAD_TAB = "投入設定"
LOAD_HEADERS = ["投入名", "スプシID", "投入用シート名", "オブジェクトAPI名", "キー項目API名", "有効"]

MAP_TAB = "マッピング"
MAP_HEADERS = ["投入名", "スプシの列名", "Salesforce項目API名"]


# Sheets API の読み取り上限（1分60回）に当たらないよう、短時間キャッシュする
@st.cache_data(ttl=60, show_spinner=False)
def _read_tab(_gc, url, tab, headers):
    """設定タブを読む。無ければ見出しだけ作って空で返す。"""
    gc = _gc
    sh = gc.open_by_url(url)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        ws = sh.add_worksheet(title=tab, rows=200, cols=max(len(headers), 6))
        ws.update(range_name="A1", values=[headers])
        ws.freeze(rows=1)
        return pd.DataFrame(columns=headers)
    values = ws.get_all_values()
    if not values:
        ws.update(range_name="A1", values=[headers])
        return pd.DataFrame(columns=headers)
    hdr = values[0]
    rows = [(r + [""] * len(hdr))[:len(hdr)] for r in values[1:]]
    df = pd.DataFrame(rows, columns=hdr)
    for h in headers:
        if h not in df.columns:
            df[h] = ""
    return df[headers]


def _write_tab(gc, url, tab, headers, df):
    sh = gc.open_by_url(url)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        ws = sh.add_worksheet(title=tab, rows=200, cols=max(len(headers), 6))
    body = [headers] + df.fillna("").astype(str).values.tolist()
    ws.clear()
    ws.update(range_name="A1", values=body, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    st.cache_data.clear()   # 書き込んだら読み直す
    return len(body) - 1


@st.cache_data(ttl=600, show_spinner=False)
def _key_field_options(object_api: str):
    """照合キーに使える項目（Id と外部ID）をSalesforceから取ってくる。

    「どの項目で突き合わせるか」は Data Loader と同じ選択肢にしたいので、
    実物から拾ってプルダウンにする（打ち間違いも防げる）。
    """
    try:
        sf = sfl.connect()
        meta = getattr(sf, object_api).describe()
    except Exception:
        return []
    opts = ["Id"] + [f["name"] for f in meta.get("fields", []) if f.get("externalId")]
    return list(dict.fromkeys(opts))


@st.cache_data(ttl=600, show_spinner=False)
def _object_options():
    """投入先に選べるオブジェクト。ふだん使うものを前に出す。"""
    try:
        sf = sfl.connect()
        names = [o["name"] for o in sf.describe()["sobjects"]
                 if o.get("createable") and not o.get("deprecatedAndHidden")]
    except Exception:
        return ["Opportunity"]
    # 案件（Opportunity）が既定。ほかもすぐ選べるように後ろに並べる
    head = [n for n in ("Opportunity", "Account", "Contact") if n in names]
    return head + [n for n in names if n not in head]


@st.cache_data(ttl=600, show_spinner=False)
def object_labels():
    """オブジェクトのAPI名 → 日本語ラベル（例：Opportunity → 案件）。

    ふだんSalesforceの画面では日本語しか見ないので、API名だけ出されても分からない。
    Data Loader と同じ「案件 (Opportunity)」の形で選べるようにする。
    """
    try:
        sf = sfl.connect()
        return {o["name"]: o.get("label", o["name"]) for o in sf.describe()["sobjects"]}
    except Exception:
        return {}


@st.cache_data(ttl=600, show_spinner=False)
def field_labels(object_api: str):
    """項目のAPI名 → 日本語ラベル（例：GasID__c → ガスID）。"""
    try:
        sf = sfl.connect()
        meta = getattr(sf, object_api).describe()
        return {f["name"]: f.get("label", f["name"]) for f in meta.get("fields", [])}
    except Exception:
        return {}


def _read_sheet_table(gc, sheet_id, tab):
    """投入元のシートを読んで (見出し, 行) で返す。"""
    ws = gc.open_by_key(str(sheet_id).strip()).worksheet(str(tab).strip())
    values = ws.get_all_values()
    if not values:
        return [], []
    return values[0], values[1:]


def render(gc, settings_url: str, key_prefix: str = "sf"):
    """Salesforce投入パネルを描く。gc＝gspreadクライアント、settings_url＝設定スプシ。"""
    if not (gc and settings_url):
        st.info("先に設定スプレッドシートを登録してください。")
        return

    # --- ① 投入設定（どのシートを、どこへ、どのキーで） ---
    st.markdown("**① 投入設定**")
    st.caption("1行＝1つの投入です。ネットはキャリアごと、ライフラインは「一括DL」1本、"
               "のように分けて登録できます。**キー項目**が `Id` のときは既存レコードの更新のみ、"
               "外部ID（回線登録番号・ガスID・電力IDなど）のときは無ければ新規作成もされます。")
    try:
        load_df = _read_tab(gc, settings_url, LOAD_TAB, LOAD_HEADERS)
    except Exception as e:
        st.error(f"投入設定を読めませんでした: {e}")
        return
    # 投入先は「案件（Opportunity）」が既定。照合キーは実物から選べるようにする
    _objs = _object_options()
    _keys = _key_field_options("Opportunity") or ["Id"]
    _col_obj = (st.column_config.SelectboxColumn("オブジェクトAPI名", options=_objs,
                                                 default="Opportunity",
                                                 help="投入先。ふつうは 案件（Opportunity）")
                if _objs else st.column_config.TextColumn("オブジェクトAPI名"))
    _col_key = (st.column_config.SelectboxColumn("キー項目API名", options=_keys, default="Id",
                                                 help="どの項目で突き合わせるか。"
                                                      "Id＝既存レコードの更新のみ。"
                                                      "外部ID（回線登録番号・ガスID・電力ID等）＝無ければ新規作成")
                if _keys else st.column_config.TextColumn("キー項目API名"))
    st.caption("💡 照合キーの選択肢は、Salesforceから取ってきた実物です"
               "（Data Loaderの「field for matching」と同じ並び）。")
    edited = st.data_editor(
        load_df, num_rows="dynamic", use_container_width=True, key=f"{key_prefix}_load_ed",
        column_config={
            "投入名": st.column_config.TextColumn(help="この投入の呼び名。マッピングもこの名前で紐づきます"),
            "スプシID": st.column_config.TextColumn(width="medium"),
            "投入用シート名": st.column_config.TextColumn(help="例：GMO ドコモ進捗反映（一括）／一括DL"),
            "オブジェクトAPI名": _col_obj,
            "キー項目API名": _col_key,
            "有効": st.column_config.SelectboxColumn(options=["TRUE", "FALSE"], default="TRUE"),
        })
    if st.button("💾 投入設定を保存", key=f"{key_prefix}_save_load"):
        try:
            n = _write_tab(gc, settings_url, LOAD_TAB, LOAD_HEADERS, edited)
            st.success(f"{n}件を保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした: {e}")

    names = [n for n in edited["投入名"].tolist() if str(n).strip()]
    if not names:
        st.info("投入設定を1行つくると、下でマッピングと実行ができます。")
        return

    st.markdown("---")
    target = st.selectbox("どの投入を扱う？", names, key=f"{key_prefix}_target")
    row = edited[edited["投入名"] == target].iloc[0]

    # --- ② マッピング（列名 → 項目API名） ---
    st.markdown("**② マッピング（スプシの列名 → Salesforceの項目）**")
    st.caption("項目が増えたら行を足すだけです。いま使っているマッピングファイル(.sdl)やCSVから取り込めます。")
    try:
        map_all = _read_tab(gc, settings_url, MAP_TAB, MAP_HEADERS)
    except Exception as e:
        st.error(f"マッピングを読めませんでした: {e}")
        return

    st.caption("📥 いま Data Loader で使っているマッピングファイル（.sdl）や、"
               "「スプシの列名, Salesforceの項目名」の2列CSVを取り込めます。"
               "取り込めば、以後はこの表を使うので、ファイルの管理は不要になります。")
    up = st.file_uploader("マッピングファイルを取り込む（.sdl / .csv）",
                          type=["sdl", "csv", "txt"], key=f"{key_prefix}_up")
    if up is not None and st.button("📥 この内容を取り込む", key=f"{key_prefix}_import"):
        try:
            text = up.getvalue().decode("utf-8", errors="replace")
            if up.name.lower().endswith(".csv"):
                _df = pd.read_csv(io.StringIO(text))
                pairs = {str(r[0]).strip(): str(r[1]).strip() for r in _df.values if len(r) >= 2}
            else:
                pairs = sfl.parse_sdl(text)
            add = pd.DataFrame([{"投入名": target, "スプシの列名": k, "Salesforce項目API名": v}
                                for k, v in pairs.items()])
            keep = map_all[map_all["投入名"] != target]       # 同じ投入名の古い分は入れ替える
            merged = pd.concat([keep, add], ignore_index=True)
            _write_tab(gc, settings_url, MAP_TAB, MAP_HEADERS, merged)
            st.success(f"{len(add)}項目を取り込みました。")
            st.rerun()
        except Exception as e:
            st.error(f"取り込めませんでした: {e}")

    mine = map_all[map_all["投入名"] == target][["スプシの列名", "Salesforce項目API名"]]
    map_ed = st.data_editor(mine, num_rows="dynamic", use_container_width=True,
                            key=f"{key_prefix}_map_ed")
    if st.button("💾 マッピングを保存", key=f"{key_prefix}_save_map"):
        try:
            add = map_ed.copy()
            add.insert(0, "投入名", target)
            keep = map_all[map_all["投入名"] != target]
            _write_tab(gc, settings_url, MAP_TAB, MAP_HEADERS,
                       pd.concat([keep, add], ignore_index=True))
            st.success("保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした: {e}")

    mapping = {str(r["スプシの列名"]).strip(): str(r["Salesforce項目API名"]).strip()
               for _, r in map_ed.iterrows() if str(r.get("スプシの列名", "")).strip()}

    # --- ③ 事前チェックと実行 ---
    st.markdown("---")
    st.markdown("**③ 投入する**")
    obj = str(row.get("オブジェクトAPI名", "") or "").strip()
    key_field = str(row.get("キー項目API名", "") or "").strip()
    st.caption(f"投入先：**{obj or '（未設定）'}** ／ 照合キー：**{key_field or '（未設定）'}**"
               + ("　※ Id なので既存レコードの更新のみです" if key_field == "Id" else ""))

    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        do_check = st.button("🩺 事前チェック", key=f"{key_prefix}_check", use_container_width=True)
    with c2:
        n_try = st.number_input("お試し件数", min_value=1, max_value=200, value=5,
                                key=f"{key_prefix}_ntry")
        do_try = st.button(f"🧪 {int(n_try)}件だけ投入", key=f"{key_prefix}_try", use_container_width=True)
    with c3:
        do_all = st.button("🚀 全件を投入", key=f"{key_prefix}_all", type="primary",
                           use_container_width=True)

    if not (do_check or do_try or do_all):
        return
    if not (obj and key_field and mapping):
        st.error("オブジェクト・キー項目・マッピングをすべて設定してください。")
        return

    try:
        headers, rows = _read_sheet_table(gc, row["スプシID"], row["投入用シート名"])
    except Exception as e:
        st.error(f"投入用シートを読めませんでした: {e}")
        return
    if not headers:
        st.warning("投入用シートが空です。")
        return

    records, skipped = sfl.build_records(headers, rows, mapping, skip_empty_key=key_field)
    st.caption(f"シートの行数 {len(rows)}／投入対象 {len(records)}件"
               + (f"（キーが空のため {skipped}件は対象外）" if skipped else ""))

    # 列名の食い違いは、投入してからでは気づきにくいので先に出す
    missing_cols = [c for c in mapping if c not in headers]
    if missing_cols:
        st.warning("⚠️ シートに無い列がマッピングにあります：" + "／".join(missing_cols))

    try:
        sf = sfl.connect()
    except Exception as e:
        st.error(f"Salesforceに接続できませんでした: {e}")
        return

    bad, _fields = sfl.check_mapping(sf, obj, mapping)
    if bad:
        st.error("⚠️ Salesforceに存在しない項目があります。投入を中止しました。")
        st.dataframe(pd.DataFrame(bad), use_container_width=True, hide_index=True)
        return
    st.success("✅ 項目はすべてSalesforceに実在します。")

    if do_check:
        if records:
            st.caption("投入される内容（先頭3件）")
            st.dataframe(pd.DataFrame(records[:3]), use_container_width=True, hide_index=True)
        return

    limit = int(n_try) if do_try else 0
    with st.spinner("投入しています..."):
        res = sfl.upsert(sf, obj, key_field, records, limit=limit)
    if res["ng"]:
        st.error(f"完了：成功 {res['ok']}件 ／ 失敗 {res['ng']}件")
        st.caption("下の表の「原因」を見てください。どの案件かは左端の照合キーの値で分かります。"
                   "英語の原文も「元のメッセージ」に残してあります。")
        st.dataframe(pd.DataFrame(res["errors"]), use_container_width=True, hide_index=True)
    else:
        st.success(f"✅ 完了：{res['ok']}件を投入しました（対象 {res['total']}件）")


def load_mapping(gc, settings_url: str, carrier: str) -> dict:
    """そのキャリアのマッピング（スプシの列名 → Salesforceの項目API名）。"""
    map_all = _read_tab(gc, settings_url, MAP_TAB, MAP_HEADERS)
    mine = map_all[map_all["投入名"] == carrier]
    return {str(r["スプシの列名"]).strip(): str(r["Salesforce項目API名"]).strip()
            for _, r in mine.iterrows() if str(r.get("スプシの列名", "")).strip()}


def push_carrier(gc, settings_url: str, carrier: str, sheet_id: str, tab: str,
                 obj: str, key_field: str, limit: int = 0) -> dict:
    """1キャリア分をSalesforceへ投入する（画面を出さない版）。

    「進捗を反映する」の流れの中から続けて呼べるようにするためのもの。
    ボタンを押して回らずに、貼り付け〜投入までを一度に終わらせたい、という用途。
    戻り値：{"結果": 人が読む一行, "ok": 成功数, "ng": 失敗数, "errors": [...]}
    """
    out = {"結果": "", "ok": 0, "ng": 0, "errors": []}
    if not (obj and key_field):
        out["結果"] = ("⚠️ 投入先・照合キーが未設定です（キャリアの設定の"
                       "「5. Salesforceへの投入」で選んで保存してください）")
        return out
    mapping = load_mapping(gc, settings_url, carrier)
    if not mapping:
        out["結果"] = (f"⚠️ 「{carrier}」のマッピングがまだありません（キャリアの設定の"
                       "「5. Salesforceへの投入」→「🗺 項目のマッピング」で、"
                       "いつものマッピングファイル(.sdl)を取り込んでください）")
        return out
    try:
        headers, rows = _read_sheet_table(gc, sheet_id, tab)
    except Exception as e:
        out["結果"] = f"❌ 投入用シートを読めません: {str(e)[:120]}"
        return out
    if not headers:
        out["結果"] = "⚠️ 投入用シートが空です"
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

    # 日付「2026/08/25」などは、そのままでは受け取ってもらえないので整えてから送る
    _types = sfl.describe_field_types(sf, obj)
    records, skipped = sfl.build_records(headers, rows, mapping,
                                         skip_empty_key=key_field, field_types=_types)
    if not records:
        out["結果"] = "⚠️ 投入できる行がありません（照合キーが空）"
        return out

    res = sfl.upsert(sf, obj, key_field, records, limit=limit)
    out.update({"ok": res["ok"], "ng": res["ng"], "errors": res["errors"]})
    if not res["ng"]:
        out["結果"] = (f"✅ Salesforceへ{res['ok']}件を投入しました"
                       + (f"（{skipped}件はキーが空で対象外）" if skipped else ""))
    else:
        # いちばん多い原因を一行で添える。表を開かなくても、何をすればよいか分かるように。
        _reasons = [str(e.get("原因", "")) for e in res["errors"] if e.get("原因")]
        _top = max(set(_reasons), key=_reasons.count) if _reasons else ""
        out["結果"] = (f"⚠️ 投入：成功 {res['ok']}件／失敗 {res['ng']}件"
                       + (f"　いちばん多い原因：{_top}" if _top else ""))
    return out


def render_carrier_sf(gc, settings_url: str, carrier: str, sheet_id: str, tab: str,
                      obj: str, key_field: str, key_prefix: str = "csf"):
    """キャリア1件分のSalesforce投入（マッピングと実行）。

    投入設定を別表で持つと「どのキャリアの設定か」を人が突き合わせることになるため、
    キャリアの設定画面の中で完結させる。マッピングはキャリア名で紐づけて保存する。
    """
    if not (gc and settings_url):
        st.info("先に「⚙️ 進捗設定」を済ませてください。")
        return
    if not (sheet_id and tab):
        st.info("先に「投入用シート」を選んでください。")
        return

    try:
        map_all = _read_tab(gc, settings_url, MAP_TAB, MAP_HEADERS)
    except Exception as e:
        st.error(f"マッピングを読めませんでした: {e}")
        return

    st.caption("📥 いま Data Loader で使っているマッピングファイル（.sdl）や、"
               "「スプシの列名, Salesforceの項目名」の2列CSVを取り込めます。"
               "取り込めば、以後はこの表を使うのでファイルの管理は不要です。")
    up = st.file_uploader("マッピングファイルを取り込む（.sdl / .csv）",
                          type=["sdl", "csv", "txt"], key=f"{key_prefix}_up_{carrier}")
    if up is not None and st.button("📥 この内容を取り込む", key=f"{key_prefix}_imp_{carrier}"):
        try:
            text = up.getvalue().decode("utf-8", errors="replace")
            if up.name.lower().endswith(".csv"):
                _df = pd.read_csv(io.StringIO(text))
                pairs = {str(r[0]).strip(): str(r[1]).strip() for r in _df.values if len(r) >= 2}
            else:
                pairs = sfl.parse_sdl(text)
            add_df = pd.DataFrame([{"投入名": carrier, "スプシの列名": k, "Salesforce項目API名": v}
                                   for k, v in pairs.items()])
            keep = map_all[map_all["投入名"] != carrier]
            _write_tab(gc, settings_url, MAP_TAB, MAP_HEADERS,
                       pd.concat([keep, add_df], ignore_index=True))
            st.success(f"{len(add_df)}項目を取り込みました。")
            st.rerun()
        except Exception as e:
            st.error(f"取り込めませんでした: {e}")

    mine = map_all[map_all["投入名"] == carrier][["スプシの列名", "Salesforce項目API名"]]
    map_ed = st.data_editor(mine, num_rows="dynamic", use_container_width=True,
                            key=f"{key_prefix}_map_{carrier}")
    if st.button("💾 マッピングを保存", key=f"{key_prefix}_savemap_{carrier}"):
        try:
            add_df = map_ed.copy()
            add_df.insert(0, "投入名", carrier)
            keep = map_all[map_all["投入名"] != carrier]
            _write_tab(gc, settings_url, MAP_TAB, MAP_HEADERS,
                       pd.concat([keep, add_df], ignore_index=True))
            st.success("保存しました。")
        except Exception as e:
            st.error(f"保存できませんでした: {e}")

    mapping = {str(r["スプシの列名"]).strip(): str(r["Salesforce項目API名"]).strip()
               for _, r in map_ed.iterrows() if str(r.get("スプシの列名", "")).strip()}

    st.markdown("---")
    st.caption(f"投入先：**{obj or '（未設定）'}** ／ 照合キー：**{key_field or '（未設定）'}**"
               + ("　※ Id なので既存レコードの更新のみです" if key_field == "Id" else ""))
    c1, c2, c3 = st.columns(3)
    with c1:
        do_check = st.button("🩺 事前チェック", key=f"{key_prefix}_chk_{carrier}",
                             use_container_width=True)
    with c2:
        n_try = st.number_input("お試し件数", 1, 200, 5, key=f"{key_prefix}_n_{carrier}")
        do_try = st.button(f"🧪 {int(n_try)}件だけ投入", key=f"{key_prefix}_try_{carrier}",
                           use_container_width=True)
    with c3:
        do_all = st.button("🚀 全件を投入", key=f"{key_prefix}_all_{carrier}",
                           type="primary", use_container_width=True)

    if not (do_check or do_try or do_all):
        return
    if not (obj and key_field and mapping):
        st.error("投入先・照合キー・マッピングをそろえてください。")
        return
    try:
        headers, rows = _read_sheet_table(gc, sheet_id, tab)
    except Exception as e:
        st.error(f"投入用シートを読めませんでした: {e}")
        return
    if not headers:
        st.warning("投入用シートが空です。")
        return

    missing_cols = [c for c in mapping if c not in headers]
    if missing_cols:
        st.warning("⚠️ シートに無い列がマッピングにあります：" + "／".join(missing_cols))

    try:
        sf = sfl.connect()
    except Exception as e:
        st.error(f"Salesforceに接続できませんでした: {e}")
        return
    bad, _fields = sfl.check_mapping(sf, obj, mapping)
    if bad:
        st.error("⚠️ Salesforceに存在しない項目があります。投入を中止しました。")
        st.dataframe(pd.DataFrame(bad), use_container_width=True, hide_index=True)
        return
    st.success("✅ 項目はすべてSalesforceに実在します。")
    # 日付や数値は、Salesforceが受け取れる形に整えてから送る
    _types = sfl.describe_field_types(sf, obj)
    records, skipped = sfl.build_records(headers, rows, mapping,
                                         skip_empty_key=key_field, field_types=_types)
    st.caption(f"シートの行数 {len(rows)}／投入対象 {len(records)}件"
               + (f"（キーが空のため {skipped}件は対象外）" if skipped else ""))
    if do_check:
        if records:
            st.caption("投入される内容（先頭3件）")
            st.dataframe(pd.DataFrame(records[:3]), use_container_width=True, hide_index=True)
        return

    with st.spinner("投入しています..."):
        res = sfl.upsert(sf, obj, key_field, records, limit=int(n_try) if do_try else 0)
    if res["ng"]:
        st.error(f"完了：成功 {res['ok']}件 ／ 失敗 {res['ng']}件")
        st.caption("下の表の「原因」を見てください。どの案件かは左端の照合キーの値で分かります。"
                   "英語の原文も「元のメッセージ」に残してあります。")
        st.dataframe(pd.DataFrame(res["errors"]), use_container_width=True, hide_index=True)
    else:
        st.success(f"✅ 完了：{res['ok']}件を投入しました（対象 {res['total']}件）")
