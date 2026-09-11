"""
👀 目で見て確認するシート（共通部品）

「検討エラーリスト」「フラグエラーリスト」のように、**出方が決まっていないので
自動判定できない**シートを、人が見て・直して・消し込むための画面。

⚠️ **データローダー自動化とオートコール投入の両方から呼ぶ。**
   同じ画面を2か所に書くと、片方だけ直して必ず食い違う
   （このアプリで何度も踏んでいるところ）。直すときはこのファイルだけ。

考え方：
  - 直すのは **Salesforce側**（またはスプシの元データ）。
  - スプシの行を消すのは **その日の作業の目印**。翌日は元から作り直されるので、
    直っていれば自然に出なくなる。直っていなければ、また出るのが正しい。
"""
import json
import os
import time

import pandas as pd
import streamlit as st

import sms_runner

DONE_COL = "✅ 対応した"
MAX_ROWS = 200            # 画面に出す上限（多すぎると開けなくなる）
KEEP_LOG = 500            # 消し込み記録に残す回数


def read(gc, sheet_url: str, tabs):
    """確認するシートを読む。戻り値：確認結果のリスト（空なら対象なし）。"""
    out = []
    for t in (tabs or []):
        try:
            sh = (gc.open_by_url(sheet_url) if str(sheet_url).startswith("http")
                  else gc.open_by_key(sheet_url))
            values = sh.worksheet(t).get_all_values()
            heads = [str(h).strip() for h in (values[0] if values else [])]
            rows = values[1:] if values else []
        except Exception as e:
            out.append({"シート": t, "件数": -1, "見出し": [], "行": [],
                        "メモ": f"読めませんでした：{str(e)[:120]}"})
            continue
        rows = [r for r in rows if any(str(x).strip() for x in r)]
        out.append({"シート": t, "件数": len(rows), "見出し": heads,
                    "行": rows[:MAX_ROWS], "メモ": ""})
    return out


def id_column(heads) -> int:
    """案件IDらしい列を探す（見つからなければ1列目）。"""
    for i, h in enumerate(heads):
        if "id" in str(h).lower().replace(" ", "").replace("　", ""):
            return i
    return 0


def log_cleared(work_root: str, job_name: str, tab: str, rows):
    """消し込んだ行を記録に残す。

    ⚠️ 消すと「何を消したか」が分からなくなる。あとで
       「あの案件どうしたっけ」と言われたときに答えられるようにしておく。
    """
    if not rows:
        return
    try:
        path = os.path.join(sms_runner.work_dir(work_root, job_name), "消し込み記録.json")
        old = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f) or []
        old.append({"日時": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "シート": tab, "行": [list(r) for r in rows]})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old[-KEEP_LOG:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass          # 記録に失敗しても、消し込みそのものは止めない


def render(gc, sheet_url: str, job_name: str, found, wkey: str, ns: str,
           work_root: str, tabs=None, fix_where: str = "Salesforce"):
    """確認シートの中身を出す（一覧からも実行画面からも、同じものを使う）。

    ns        ：ウィジェットの名札を分けるための目印（"list" / "run"）
    work_root ：消し込み記録の置き場（`取り込みファイル/<ここ>/<ジョブ名>/`）
    tabs      ：消し込んだあと読み直すためのシート名一覧
    fix_where ：どこで直すかの言い方（画面の文言に出る）
    """
    for f in found:
        if f["メモ"]:
            st.warning(f"シート「{f['シート']}」：{f['メモ']}")
            continue
        if f["件数"] == 0:
            st.success(f"✅ 「{f['シート']}」は空でした（対応することはありません）。")
            continue
        st.error(f"🛠 「{f['シート']}」に **{f['件数']}件** 出ています。"
                 f"中身を見て、{fix_where}で対応してください。")
        try:
            cols = [h or f"列{i + 1}" for i, h in enumerate(f["見出し"])]
            body = [(r + [""] * len(f["見出し"]))[:len(f["見出し"])] for r in f["行"]]

            # 📋 案件IDは検索に貼るので、すぐコピーできるようにする。
            #    同じ案件が商材ぶん何行にもなるので、重複は畳む。
            ix = id_column(f["見出し"])
            ids = list(dict.fromkeys(
                str(r[ix]).strip() for r in body if ix < len(r) and str(r[ix]).strip()))
            if ids:
                st.caption(f"📋 {cols[ix]}（{len(ids)}件・右上のボタンでまとめてコピーできます）")
                st.code("\n".join(ids), language=None)

            df = pd.DataFrame(body, columns=cols)
            # ⭐ 1件ずつ消し込めるようにする。直したらチェックを入れて、スプシから消す。
            df.insert(0, DONE_COL, False)
            edited = st.data_editor(
                df, use_container_width=True, hide_index=True,
                key=f"w_ed_{ns}_{job_name}_{f['シート']}",
                disabled=cols,          # 中身は直せない（消すだけ）
                column_config={DONE_COL: st.column_config.CheckboxColumn(
                    DONE_COL, help=f"{fix_where}で直し終わったものにチェック")})
            picked = [i for i, v in enumerate(edited[DONE_COL].fillna(False).tolist()) if v]
            d1, d2 = st.columns([1, 2])
            with d1:
                if st.button(f"🗑 対応した分を消す（{len(picked)}件）",
                             key=f"w_del_{ns}_{job_name}_{f['シート']}",
                             use_container_width=True,
                             disabled=not picked or not gc):
                    cnt, gone, missed = sms_runner.delete_rows_matching(
                        gc, sheet_url, f["シート"], [body[i] for i in picked])
                    log_cleared(work_root, job_name, f["シート"], gone)
                    if cnt:
                        st.success(f"「{f['シート']}」から {cnt}件 消しました。")
                    if missed:
                        st.warning(f"⚠️ {len(missed)}件は見つからなかったので消していません"
                                   "（その間にスプレッドシート側が変わったようです）。"
                                   "もう一度「🔍 確認する」を押してください。")
                    st.session_state[wkey] = read(gc, sheet_url, tabs or [])
                    st.rerun()
            with d2:
                st.caption(f"{fix_where}で直したものにチェックを入れて押すと、"
                           "**スプレッドシートのその行を消します**。消したものは記録に残ります。")
            st.download_button(
                f"⬇️ 「{f['シート']}」をCSVで落とす",
                data=pd.DataFrame(body, columns=cols).to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{f['シート']}_{sms_runner.today_stamp()}.csv",
                mime="text/csv", key=f"w_dl_{ns}_{job_name}_{f['シート']}")
        except Exception as e:
            st.caption(f"（表にできませんでした：{str(e)[:120]}）")
