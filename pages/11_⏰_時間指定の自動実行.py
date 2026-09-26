"""
⏰ 時間指定の自動実行

決めた時刻に、決めた業務（進捗反映・SMS送信・データローダー・オートコール投入・SFレポート更新）を
**電源を入れっぱなしのPC**で自動で動かす。

- 予定は Supabase の `__schedule__`。どのPCから登録してもよい（動くのは「自動実行用のPC」だけ）。
- 動かす役は `scheduler.py`（タスクスケジューラが5分おきに呼ぶ）。実行の中身は `auto_jobs.py`
  ＝各ページの「全部実行」と同じもの。
- ⚠️ 送信・投入は取り消せないので、**各業務の「どこまで自動で行くか」の設定に従う**。
  OFF の工程では手前で止まり、Slack で知らせる（この画面で確認を飛ばす設定は作らない）。
"""
import datetime as dt
import uuid

import pandas as pd
import streamlit as st
from supabase import create_client

import auto_jobs
import characters as ch
import scheduler as sch
import slack_notify
import theme

st.set_page_config(page_title="時間指定の自動実行 - エンカンAI", layout="wide")
theme.inject_theme()

import secrets_check  # noqa: E402
secrets_check.check()
theme.brand_sidebar(active="manage")

c = ch.get("manage")
theme.page_header("⏰", "時間指定の自動実行",
                  "決めた時刻に、決めた業務を、電源を入れっぱなしのPCで自動で動かします。",
                  color=c["color"])


@st.cache_resource
def _sb():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase = _sb()
st.session_state.setdefault("sch_edit", None)      # 直している予定のID（"＿新規" は追加）

try:
    cfg = sch.load_schedule(supabase)
    runs = sch.load_runs(supabase)
except Exception as e:
    st.error(f"予定を読み込めませんでした（Supabase が混んでいるようです）: {e}")
    if st.button("🔄 読み込み直す"):
        st.rerun()
    st.stop()

ME = sch.this_host()
HOST = str(cfg.get("host", "") or "")
KINDS = list(auto_jobs.KIND_LABELS.keys())


def _save(mutate):
    """⚠️ 保存の直前に読み直して、変えたところだけ当てる（別のPCで同時に直した分を消さない）。"""
    fresh = sch.load_schedule(supabase)
    mutate(fresh)
    if not sch.save_schedule(supabase, fresh):
        st.error("保存できませんでした。少し待ってもう一度押してください。")
        return False
    return True


# ==========================================
# 🧭 その業務の設定で、どこまで自動で進むか
# ==========================================
@st.cache_data(ttl=60, show_spinner=False)
def _settings(kind: str) -> dict:
    try:
        return auto_jobs.load_row(supabase, auto_jobs.SETTINGS_IDS[kind])
    except Exception:
        return {}


def _find_target(kind: str, name: str):
    key = {"sms": "patterns", "dataloader": "jobs", "autocall": "jobs", "reports": "sets"}.get(kind)
    if not key:
        return None
    return next((x for x in (_settings(kind).get(key) or []) if str(x.get("name", "")) == name), None)


def reach(item: dict):
    """(説明, 人の確認なしで最後まで行くか)。各業務の設定を読んで、止まる場所を先に知らせる。"""
    kind, name = item.get("kind", ""), str(item.get("target", "") or "")
    if kind == "reports":
        return "レポートの更新まで行います（人の確認はありません）。", True
    if kind == "robot":
        return (f"ロボット「{name}」を、**送信（申請）まで**動かします。"
                "取り消せない操作なので、予定に入れる時点が人の判断です。"), True
    if kind == "chiiki":
        _ck = _settings("chiiki")
        _fx, _ps = bool(_ck.get("auto_fax")), bool(_ck.get("auto_push"))
        msg = ("3枚を更新して抜けをチェックし、"
               + ("**抜けが0件ならFAXまで送ります**。" if _fx else "**FAXの手前で止めて**Slackで知らせます（「FAXまで自動」がOFF）。")
               + "電話・WEBの残りはSlackで名指しします。"
               + ("全部済んでいれば**手配日まで入れます**。" if _ps else "手配日は画面で入れます（「投入まで自動」がOFF）。")
               + "⭐ 1日に2回（朝と夕方など）入れると、2回目で残りの知らせと手配日の投入をします。")
        return msg, False
    if kind == "irregular":
        return ("シートを更新して、イレギュラー対応待ちが1件でもあればSlackで知らせます"
                "（報告そのものは、人が「📣 イレギュラー報告」で書きます）。"), True
    if kind == "precheck":
        _pc = _settings("precheck")
        if not str(_pc.get("gas_url", "") or "").strip():
            return ("⚠️ チェックを動かすGASが未設定なので、動かせません"
                    "（「🔎 エントリー前DC」の⚙️設定で入れてください）。", False)
        _plan = "／".join(f"{auto_jobs.precheck_tab_label(t)}→{n or 'いつもの送り先'}"
                          for n, t in auto_jobs.precheck_slack_plan(_pc))
        return ("貼り付けシートを更新してチェックし、**結果をSlackで知らせます**"
                f"（ミスが0件の日も知らせます／送り先：{_plan}）。直すのは人です（Salesforce側）。"), True
    if kind == "progress":
        push = bool(_settings("progress").get("push_salesforce", True))
        return (("ファイルの入手 → 貼り付け → Salesforceへの投入まで行います。" if push
                 else "ファイルの入手 → 貼り付けまで行います（投入はしない設定です）。")
                + "手動アップロードのキャリアは取り込みません。"), True
    job = _find_target(kind, name)
    if not job:
        return f"⚠️ 「{name}」が見つかりません（名前を変えた・消した？）。", False
    notes, full = [], True
    if kind == "dataloader":
        if job.get("loads") and not str(job.get("gas_url", "") or "").strip():
            notes.append("GASが未設定なので、**投入の手前で止まります**")
            full = False
        if job.get("watch_tabs") and job.get("watch_block", True):
            notes.append("確認シートに中身が出ていたら止まります")
        if job.get("loads"):
            if job.get("auto_push"):
                notes.append("④ Salesforceへの投入まで自動で行います")
            else:
                notes.append("「④の投入まで自動で行う」がOFFなので、**投入の手前で止まります**")
                full = False
    elif kind == "autocall":
        if job.get("watch_tabs") and job.get("watch_block", True):
            notes.append("確認シートに中身が出ていたら止まります")
        if job.get("autocalls"):
            if job.get("auto_call"):
                notes.append("ブルービーンへの投入まで自動で行います")
            else:
                notes.append("「投入まで自動で行う」がOFFなので、**投入の手前で止まります**")
                full = False
        if job.get("loads"):
            if job.get("auto_push"):
                notes.append("Salesforceへの投入まで行います")
            else:
                notes.append("Salesforceへの投入は自動がOFFなので、**その手前で止まります**")
                full = False
    elif kind == "sms":
        if job.get("check_tabs"):
            notes.append("「目で見て確認するシート」があるので、**毎回②で止まります**（時間指定には向きません）")
            full = False
        if job.get("checks"):
            notes.append("ルールに引っかかった行があれば止まります")
        if job.get("auto_send"):
            notes.append("一括送信まで自動で行います")
        else:
            notes.append("「一括送信まで自動で行う」がOFFなので、**送信の手前で止まります**")
            full = False
        if job.get("loads"):
            notes.append("Salesforceへの投入まで行います" if job.get("auto_load")
                         else "Salesforceへの投入はしません（自動がOFF）")
    return "。".join(notes) + "。" if notes else "（止まる工程はありません）", full


# ==========================================
# 🖥 動かすPC
# ==========================================
with st.container(border=True):
    theme.section_title("🖥", "自動で動かすPC")
    a, b = st.columns(2)
    with a:
        st.markdown(f"**自動実行用のPC**：{('`' + HOST + '`') if HOST else '（まだ決めていません）'}")
        st.caption(f"いま開いているPC：`{ME}`")
    with b:
        _tick = str(runs.get("last_tick", "") or "")
        _ago = None
        try:
            _ago = (dt.datetime.now() - dt.datetime.strptime(_tick, "%Y/%m/%d %H:%M")).total_seconds() / 60
        except Exception:
            pass
        _run = runs.get("running")
        if _run:
            st.info(f"▶ いま動いています：{_run.get('label', '')}（{_run.get('start', '')} から）")
        if not HOST:
            st.caption("自動実行用のPCを決めると、見回りが始まります。")
        elif _ago is not None and _ago <= 15:
            st.success(f"✅ 見回り中です（最後に見に来たのは {_tick}）")
        elif _run:
            st.caption(f"最後に見に来たのは {_tick or '—'}（実行中は見回りを休みます）")
        else:
            st.warning(f"⚠️ 見回りが止まっているようです（最後に見に来たのは {_tick or 'まだありません'}）。"
                       "PCの電源・ログオン・スリープを確かめてください。")
    # 🔔 Slackに送るのは自動実行用のPCだけ。⚠️ 開いているPCの設定で判断すると、
    #    自動実行用のPCで読めていても、ほかのPCで開いたときに「ありません」と出てしまう。
    #    見回り役が書いた slack_ready を見る（まだ見回りが来ていない・このPCが自動実行用なら、このPCで確かめる）。
    _slack = runs.get("slack_ready") if (HOST and HOST != ME and runs.get("host") == HOST) else None
    _why = str(runs.get("slack_why", "") or "") if _slack is not None else ""
    if _slack is None:
        _u, _src, _why = slack_notify.webhook_url(None, supabase)
        _slack = bool(_u)
    if not _slack:
        st.warning("🔔 **Slackに知らせる送り先が読めません。** 止まっても知らせが届きません。"
                   + (f"（{_why}）" if _why else "")
                   + "「⚙️ その他設定」の **🔔 Slack通知** で、Webhook URL を1回保存してください"
                     "（全PCで使われます。PCごとに入れる必要はありません）。")

    if ME != HOST:
        if HOST:
            st.caption(f"⚠️ 切り替えると、いまの `{HOST}` では動かなくなります（二重に動かさないため、動くのは1台だけ）。")
        _agree = st.checkbox("このPCは、**電源入れっぱなし・自動ログオン・スリープなし**にしてあります",
                             key="sch_pc_agree")
        if st.button("🖥 このPCを自動実行用にする", type="primary", disabled=not _agree):
            ok, msg = sch.install()
            if not ok:
                st.error(f"タスクスケジューラに登録できませんでした：{msg}")
            elif _save(lambda f: f.update({"host": ME})):
                st.success("登録しました。5分以内に見回りが始まります。")
                st.rerun()
    else:
        _inst = sch.installed()
        if _inst:
            st.caption(f"✅ このPCのタスクスケジューラに「{sch.TASK_NAME}」が登録されています（{sch.TICK_MINUTES}分おき）。")
        else:
            st.error("⚠️ このPCが自動実行用ですが、タスクスケジューラに見回りが登録されていません。")
        x1, x2 = st.columns(2)
        with x1:
            if st.button("🔁 見回りを登録し直す", use_container_width=True,
                         help="Pythonを入れ直した・フォルダを移したときに押します"):
                ok, msg = sch.install()
                (st.success if ok else st.error)(msg)
        with x2:
            if st.button("⏹ このPCでの自動実行をやめる", use_container_width=True):
                sch.uninstall()
                if _save(lambda f: f.update({"host": ""})):
                    st.rerun()

    with st.expander("🧰 自動実行用のPCの準備（はじめに1回）"):
        st.markdown("""
1. **自動ログオン**：`Win + R` →「netplwiz」→ ユーザーがログオンするときにパスワードを求める… のチェックを外す
   （ロボットはブラウザを画面に出して動くので、**ログオンしたまま**にします。画面ロックはかかっても動きます）
2. **スリープしない**：設定 → システム → 電源 →「スリープ」を「なし」
3. **Windows Update の再起動**：アクティブ時間を業務の時間帯に合わせる（再起動しても自動ログオンで戻ります）
4. アプリを入れて `start.bat` で一度起動し、この画面で **🖥 このPCを自動実行用にする** を押す
5. 共通ロボット（SFコネクタ更新・プッシュプロ・ブルービーン）の **🔐 先にログインしておく** を、**このPCで**済ませる
   （ログイン状態はPCごとに持つため）
6. 下の予定を1つ作り、**▶ 動かす** で試す（5分以内に、このPCで始まります）

💡 アプリ（`start.bat`）を開いておく必要はありません。見回りはアプリとは別に動きます。
""")

# ==========================================
# 📅 予定
# ==========================================
items = cfg.get("items") or []
editing = st.session_state.sch_edit

if editing is None:
    with st.container(border=True):
        theme.section_title("📅", "予定")
        st.caption("⭐ **人の確認が要らない業務**を、人がいない時間に回すのが基本です。"
                   "途中で人の確認が要る設定のものは、そこで止まって Slack に知らせます（送信・投入はしません）。")
        if st.button("＋ 予定を追加", type="primary"):
            st.session_state.sch_edit = "＿新規"
            st.rerun()
        if not items:
            st.info("まだ予定がありません。")
        _done = runs.get("done") or {}
        _last = {}
        for h in runs.get("history") or []:
            _last.setdefault(str(h.get("id")), h)
        for it in sorted(items, key=lambda i: str(i.get("time", ""))):
            iid = str(it.get("id"))
            with st.container(border=True):
                t1, t2 = st.columns([3, 2])
                with t1:
                    st.markdown(f"**{'' if it.get('enabled', True) else '⏸（止めています）'}"
                                f"{it.get('time', '')}　{sch.describe_when(it)}**　"
                                f"{sch.item_label(it)}")
                    # 今日の時刻が過ぎた（済んだ）なら、明日から数える
                    _now = dt.datetime.now()
                    _hm = sch.parse_hm(it.get("time", "")) or (0, 0)
                    _from = _now.date()
                    if _done.get(iid) == f"{_now:%Y-%m-%d}" or (_now.hour, _now.minute) >= _hm:
                        _from += dt.timedelta(days=1)
                    _nx = sch.next_date(it, _from)
                    if _nx is None:
                        st.caption("🏁 決めた日付はすべて過ぎました（もう動きません）")
                    elif it.get("enabled", True):
                        st.caption(f"次は {_nx.month}/{_nx.day}（{sch.WEEKDAYS[_nx.weekday()]}）")
                    if it.get("kind") == "autocall" and it.get("also_delete_jobs"):
                        st.caption("🗑 入れる前に、" + "・".join(f"「{x}」" for x in it["also_delete_jobs"])
                                   + " の同じ業務のリストも消します")
                    _xs = it.get("extra_slack") or {}
                    if _xs.get("to") and (_xs.get("done") or _xs.get("fail")):
                        st.caption("📣 " + "・".join(f"「{x}」" for x in _xs["to"]) + " にも送ります（"
                                   + "・".join(w for w, on in (("完了", _xs.get("done")),
                                                              ("失敗・確認待ち・見送り", _xs.get("fail"))) if on)
                                   + "）")
                    _txt, _full = reach(it)
                    st.caption(("⭐ 人の確認なしで最後まで：" if _full else "⏸ 途中で止まります：") + _txt)
                    _h = _last.get(iid)
                    if _h:
                        st.caption(f"前回：{_h.get('開始', '')} {sch.STATE_MARK.get(_h.get('結果'), '')}"
                                   f"{_h.get('結果', '')}（{_h.get('きっかけ', '')}）")
                with t2:
                    u1, u2, u3 = st.columns(3)
                    with u1:
                        if st.button("✏️ 直す", key=f"sch_e_{iid}", use_container_width=True):
                            st.session_state.sch_edit = iid
                            st.rerun()
                    with u2:
                        if st.button("▶ 動かす", key=f"sch_r_{iid}", use_container_width=True,
                                     disabled=not HOST,
                                     help="自動実行用のPCが、次の見回り（5分以内）で動かします"):
                            if sch.add_request(supabase, iid, who=ME):
                                st.toast("依頼しました。5分以内に自動実行用のPCで始まります。")
                    with u3:
                        if st.button("🗑 消す", key=f"sch_d_{iid}", use_container_width=True):
                            if _save(lambda f: f.update({"items": [x for x in (f.get("items") or [])
                                                                   if str(x.get("id")) != iid]})):
                                st.rerun()
else:
    cur = next((i for i in items if str(i.get("id")) == editing), None) or {}
    with st.container(border=True):
        theme.section_title("✏️", "予定を追加" if editing == "＿新規" else "予定を直す")
        kind = st.selectbox("業務", KINDS, format_func=lambda k: auto_jobs.KIND_LABELS[k],
                            index=KINDS.index(cur["kind"]) if cur.get("kind") in KINDS else 0,
                            key=f"sch_kind_{editing}")
        try:
            names = auto_jobs.target_names(supabase, kind)
        except Exception as e:
            names = []
            st.error(f"業務の設定を読めませんでした：{e}")
        if kind in ("progress", "irregular", "precheck", "chiiki"):
            target = names[0] if names else ""
            st.caption({"progress": "進捗反映は、有効なキャリアをすべて「順番」どおりに実行します。",
                        "irregular": "イレギュラー報告は、待ちシートを更新して件数を知らせます（1つだけです）。",
                        "chiiki": "地域手配は、その日の続きから進めます（朝：更新→チェック→FAX／"
                                  "2回目：残りの知らせ→手配日の投入）。1つだけです。",
                        "precheck": "エントリー前DCは、貼り付けシートを更新してチェックし、"
                                    "結果をSlackで知らせます（1つだけです）。"}[kind])
        elif not names:
            target = ""
            st.warning("この業務には、まだ登録（ジョブ／パターン／セット）がありません。先にその画面で作ってください。")
        else:
            target = st.selectbox("どれを動かす？", names,
                                  index=names.index(cur["target"]) if cur.get("target") in names else 0,
                                  key=f"sch_target_{editing}")
        hm = sch.parse_hm(cur.get("time", "")) or (6, 0)
        tval = st.time_input("時刻", value=dt.time(hm[0], hm[1]), step=300, key=f"sch_time_{editing}")
        _modes = [sch.REPEAT_WEEKLY, sch.REPEAT_MONTHLY, sch.REPEAT_DATES]
        _mode_labels = {sch.REPEAT_WEEKLY: "曜日で決める", sch.REPEAT_MONTHLY: "毎月の日付で決める",
                        sch.REPEAT_DATES: "日付を選ぶ"}
        repeat = st.radio("いつ動かす？", _modes, format_func=lambda m: _mode_labels[m], horizontal=True,
                          index=_modes.index(cur.get("repeat")) if cur.get("repeat") in _modes else 0,
                          key=f"sch_repeat_{editing}")
        _days = cur.get("days")
        days, month_days, dates = [], [], []
        if repeat == sch.REPEAT_WEEKLY:
            days = st.multiselect("曜日", list(range(7)), format_func=lambda d: sch.WEEKDAYS[d],
                                  default=[int(d) for d in _days] if _days is not None else [0, 1, 2, 3, 4],
                                  key=f"sch_days_{editing}")
        elif repeat == sch.REPEAT_MONTHLY:
            month_days = st.multiselect("毎月の日", list(range(1, 32)) + [sch.MONTH_END],
                                        format_func=sch.month_day_label,
                                        default=[int(d) for d in (cur.get("month_days") or [])],
                                        key=f"sch_mdays_{editing}")
            if any(int(d) >= 29 for d in month_days if int(d) != sch.MONTH_END):
                st.caption("⚠️ 29〜31日は、その日が無い月は動きません。月の最後の日に動かしたいときは「月末」を選びます。")
        else:
            # 選んだ日付は、画面を行き来しても消えないよう session_state に持つ
            _dk, _sk = f"sch_dates_{editing}", f"sch_dsel_{editing}"
            if _dk not in st.session_state:
                st.session_state[_dk] = sorted(str(d) for d in (cur.get("dates") or []))
                st.session_state[_sk] = list(st.session_state[_dk])
            d1, d2 = st.columns([3, 2])
            with d1:
                _dpick = st.date_input("日付", value=dt.date.today(), min_value=dt.date.today(),
                                      format="YYYY/MM/DD", key=f"sch_dpick_{editing}")
            with d2:
                st.write("")
                if st.button("＋ この日を足す", use_container_width=True, key=f"sch_dadd_{editing}"):
                    _new = sorted(set(st.session_state.get(_sk) or []) | {f"{_dpick:%Y-%m-%d}"})
                    st.session_state[_dk] = sorted(set(st.session_state[_dk]) | set(_new))
                    st.session_state[_sk] = _new
                    st.rerun()
            dates = st.multiselect("動かす日付（✕で外せます）", st.session_state[_dk],
                                   format_func=lambda d: f"{d[:4]}/{d[5:7]}/{d[8:10]}"
                                                         f"（{sch.WEEKDAYS[dt.date.fromisoformat(d).weekday()]}）",
                                   key=f"sch_dsel_{editing}")
            if not dates:
                st.caption("日付を選んで「＋ この日を足す」を押してください（何日でも足せます）。")
            elif all(d < f"{dt.date.today():%Y-%m-%d}" for d in dates):
                st.caption("⚠️ 選んだ日付はすべて過ぎています。")
        _when_ok = bool(days or month_days or dates)
        late = st.number_input("何分まで遅れて始めてよいか", min_value=5, max_value=720, step=5,
                               value=int(cur.get("late_min", sch.DEFAULT_LATE_MIN) or sch.DEFAULT_LATE_MIN),
                               key=f"sch_late_{editing}",
                               help="PCが止まっていた・前の実行が長引いたとき、これより遅れたらその日は動かしません"
                                    "（昼に朝の分を送る、を防ぎます）")
        also_delete_jobs = []
        if kind == "autocall" and target:
            # 🗑 この予定のときだけ、ほかのジョブのリストも一緒に消す（朝の新旧リスト → 昨日の当日リスト）。
            #    ⚠️ ジョブの設定に置かない：営業中に画面から入れるときは、当日のリストはまだかけているので消さない。
            _others = [n for n in names if n != target]
            also_delete_jobs = st.multiselect(
                "🗑 この予定では、一緒に前のファイルを消すジョブ（任意）", _others,
                default=[x for x in (cur.get("also_delete_jobs") or []) if x in _others],
                key=f"sch_also_{editing}",
                help="例：朝の「新旧リスト」の予定に「当日」を入れると、N新旧を入れる前に N当日 の前のファイルも消します"
                     "（業務が同じシートだけ）。画面から入れるときは消しません。")
            if also_delete_jobs:
                st.caption("💡 選んだジョブのうち**業務が同じシート**の前のファイルも、投入の前に消します"
                           "（まだかけ終わっていなくても消します。この予定で動かしたときだけです）。")
        notify_done = st.checkbox("うまくいったときも Slack に知らせる",
                                  value=bool(cur.get("notify_done", True)), key=f"sch_nd_{editing}")
        # 📣 いつもの送り先に加えて、グループにも送る（完了／うまくいかなかったときを別々に選ぶ）
        _ex_names = sorted(slack_notify.extra_info(None, supabase).keys())
        _ex_cur = cur.get("extra_slack") or {}
        if _ex_names:
            ex_to = st.multiselect("📣 いつもの送り先に加えて、このグループにも送る（任意）", _ex_names,
                                   default=[x for x in (_ex_cur.get("to") or []) if x in _ex_names],
                                   key=f"sch_ex_to_{editing}",
                                   help="送り先は「⚙️ その他設定」の「📣 ほかの送り先」で足します。"
                                        "いつもの送り先への通知は、上のチェックのとおりで変わりません。")
            ex_done = ex_fail = False
            if ex_to:
                _xc1, _xc2 = st.columns(2)
                with _xc1:
                    ex_done = st.checkbox("うまくいったとき（完了）を送る", value=bool(_ex_cur.get("done", True)),
                                          key=f"sch_ex_done_{editing}")
                with _xc2:
                    ex_fail = st.checkbox("うまくいかなかったとき（失敗・確認待ち・見送り）を送る",
                                          value=bool(_ex_cur.get("fail", False)), key=f"sch_ex_fail_{editing}")
                if not (ex_done or ex_fail):
                    st.caption("⚠️ どちらにもチェックが無いので、このグループには何も送りません。")
            extra_slack = {"to": list(ex_to), "done": bool(ex_done), "fail": bool(ex_fail)} if ex_to else {}
        else:
            extra_slack = dict(_ex_cur)          # 送り先が読めないときは、前の設定をそのまま残す
            st.caption("📣 別のチャンネルにも知らせたいときは、「⚙️ その他設定」の「📣 ほかの送り先」で足してください。")
        enabled = st.checkbox("この予定を使う（外すと、消さずに止めておけます）",
                              value=bool(cur.get("enabled", True)), key=f"sch_en_{editing}")
        if target:
            _txt, _full = reach({"kind": kind, "target": target})
            (st.success if _full else st.warning)(
                ("⭐ この設定では、人の確認なしで最後まで動きます：" if _full
                 else "⏸ この設定では、途中で止まって Slack に知らせます：") + _txt)
            st.caption("止まる場所を変えたいときは、その業務の設定画面の「どこまで自動で行くか」で変えます。")
        s1, s2 = st.columns(2)
        with s1:
            if st.button("💾 保存", type="primary", use_container_width=True,
                         disabled=not (target and _when_ok)):
                new = dict(cur)
                new.update({"id": cur.get("id") or uuid.uuid4().hex[:8], "kind": kind, "target": target,
                            "time": f"{tval.hour:02d}:{tval.minute:02d}", "repeat": repeat,
                            "days": sorted(days), "month_days": sorted(int(d) for d in month_days),
                            "dates": sorted(dates),
                            "late_min": int(late), "notify_done": bool(notify_done),
                            "also_delete_jobs": list(also_delete_jobs),
                            "extra_slack": extra_slack,
                            "enabled": bool(enabled)})
                # ⚠️ 作った／時刻を変えたのが今日のその時刻より後なら、今日の分は動かさない（明日から）
                # （いつ動かすかを変えたときも同じ。今日の日付を足したら、過ぎた時刻の分が「見送り」で飛ぶため）
                _when_keys = ("time", "repeat", "days", "month_days", "dates")
                if not cur or any(new.get(k) != cur.get(k) for k in _when_keys):
                    new["since"] = f"{dt.datetime.now():%Y-%m-%d %H:%M}"

                def _put(f):
                    lst = [x for x in (f.get("items") or []) if str(x.get("id")) != str(new["id"])]
                    lst.append(new)
                    f["items"] = lst

                if _save(_put):
                    st.session_state.pop(f"sch_dates_{editing}", None)
                    st.session_state.pop(f"sch_dsel_{editing}", None)
                    st.session_state.sch_edit = None
                    st.rerun()
        with s2:
            if st.button("戻る", use_container_width=True):
                st.session_state.pop(f"sch_dates_{editing}", None)
                st.session_state.pop(f"sch_dsel_{editing}", None)
                st.session_state.sch_edit = None
                st.rerun()

# ==========================================
# 📜 実行の記録
# ==========================================
with st.container(border=True):
    theme.section_title("📜", "実行の記録")
    hist = runs.get("history") or []
    if not hist:
        st.caption("まだ記録がありません。")
    else:
        if st.button("🔄 最新にする"):
            st.rerun()
        st.dataframe(pd.DataFrame([{"開始": h.get("開始", ""), "予定": h.get("予定", ""),
                                    "結果": f"{sch.STATE_MARK.get(h.get('結果'), '')} {h.get('結果', '')}",
                                    "きっかけ": h.get("きっかけ", ""), "かかった分": h.get("かかった分", "")}
                                   for h in hist[:50]]),
                     use_container_width=True, hide_index=True)
        _pick = st.selectbox("中身を見る", range(min(50, len(hist))),
                             format_func=lambda i: f"{hist[i].get('開始', '')}　{hist[i].get('予定', '')}"
                                                   f"　{hist[i].get('結果', '')}",
                             key="sch_hist_pick")
        st.dataframe(pd.DataFrame([{k: v for k, v in s.items() if k != "Slack"}
                                   for s in (hist[_pick].get("工程") or [])]),
                     use_container_width=True, hide_index=True)
        st.caption("うまくいかなかった分は、その業務のページから実行し直してください。"
                   f"見回りの記録は自動実行用のPCの `取り込みファイル/自動実行/` にもあります。")
