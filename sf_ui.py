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
    edited = st.data_editor(
        load_df, num_rows="dynamic", use_container_width=True, key=f"{key_prefix}_load_ed",
        column_config={
            "投入名": st.column_config.TextColumn(help="この投入の呼び名。マッピングもこの名前で紐づきます"),
            "スプシID": st.column_config.TextColumn(width="medium"),
            "投入用シート名": st.column_config.TextColumn(help="例：GMO ドコモ進捗反映（一括）／一括DL"),
            "オブジェクトAPI名": st.column_config.TextColumn(help="例：Opportunity"),
            "キー項目API名": st.column_config.TextColumn(
                help="例：Id ／ Lineregistrationnumber__c ／ GasID__c ／ Powercustomernumber__c"),
            "有効": st.column_config.SelectboxColumn(options=["TRUE", "FALSE"]),
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

    up = st.file_uploader("いまのマッピングファイルを取り込む（.sdl / .csv）",
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
        st.dataframe(pd.DataFrame(res["errors"]), use_container_width=True, hide_index=True)
    else:
        st.success(f"✅ 完了：{res['ok']}件を投入しました（対象 {res['total']}件）")
