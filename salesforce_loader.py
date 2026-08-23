"""
☁️ Salesforce への投入（データローダーの代わり）

いままでは「スプシ →（GASで）CSV書き出し → ダウンロード → Data Loader で UPSERT」だったところを、
アプリから直接 UPSERT する。CSVの書き出しもダウンロードも不要になる。

設計方針：
  ・CRM は将来変わる可能性があるため、ここだけを差し替え可能な部品として独立させる。
    入口は「表（見出し＋行）を渡す」だけ。呼び出し側は Salesforce を知らなくてよい。
  ・接続情報は環境変数か .streamlit/secrets.toml から読む（コードにも設定シートにも書かない）。
      SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN / SF_DOMAIN(login or test)
  ・UPSERT なので二重投入にはならないが、間違った項目の上書きは戻せない。
    そのため「お試し（件数制限）」と「事前チェック（項目名の確認）」を必ず通せるようにする。
"""

import os
import tomllib


def _secrets() -> dict:
    """接続情報を読む。環境変数優先、無ければ secrets.toml。"""
    if os.environ.get("SF_USERNAME"):
        return {k: os.environ.get(k, "") for k in
                ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_DOMAIN")}
    try:
        with open(".streamlit/secrets.toml", "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def connect(overrides: dict = None):
    """Salesforce に接続する。失敗したら分かりやすい日本語で例外を投げる。"""
    from simple_salesforce import Salesforce
    s = dict(_secrets())
    s.update(overrides or {})
    user = str(s.get("SF_USERNAME", "") or "").strip()
    pw = str(s.get("SF_PASSWORD", "") or "").strip()
    if not (user and pw):
        raise RuntimeError("Salesforce の接続情報（SF_USERNAME / SF_PASSWORD）が設定されていません。"
                           "`.streamlit/secrets.toml` に追記してください。")
    return Salesforce(
        username=user,
        password=pw,
        security_token=str(s.get("SF_SECURITY_TOKEN", "") or ""),
        domain=str(s.get("SF_DOMAIN", "login") or "login").strip() or "login",
    )


def describe_fields(sf, object_api: str) -> dict:
    """対象オブジェクトの項目一覧を {API名: ラベル} で返す。マッピングの確認に使う。"""
    meta = getattr(sf, object_api).describe()
    return {f["name"]: f.get("label", f["name"]) for f in meta.get("fields", [])}


def check_mapping(sf, object_api: str, mapping: dict):
    """マッピング（スプシ列名 → Salesforce項目API名）が実在するか確かめる。
    投入してから「項目が無い」で失敗するのを防ぐための事前チェック。
    戻り値：(問題のあるもののリスト, 項目一覧)"""
    fields = describe_fields(sf, object_api)
    bad = [{"スプシの列": k, "Salesforce項目": v, "状態": "この項目はありません"}
           for k, v in (mapping or {}).items() if v and v not in fields]
    return bad, fields


def describe_field_types(sf, object_api: str) -> dict:
    """項目の種類を {API名: "date"/"datetime"/"boolean"/"double"...} で返す。

    スプレッドシートの値は「2026/08/25」「1,200」のような“人が読む形”なので、
    そのまま送ると Salesforce に日付として受け取ってもらえない。
    どの項目がどの種類かを知って、送る前に整えるために使う。
    """
    meta = getattr(sf, object_api).describe()
    return {f["name"]: str(f.get("type", "")) for f in meta.get("fields", [])}


_DATE_PATTERNS = (
    r"^(\d{4})[/\-\.年](\d{1,2})[/\-\.月](\d{1,2})日?$",   # 2026/08/25・2026年8月25日
    r"^(\d{4})(\d{2})(\d{2})$",                            # 20260825
)
_TRUE_WORDS = {"true", "1", "yes", "y", "はい", "○", "◯", "有", "あり", "済", "on"}
_FALSE_WORDS = {"false", "0", "no", "n", "いいえ", "×", "無", "なし", "未", "off"}


def _to_iso_date(val: str) -> str:
    """「2026/08/25」などを「2026-08-25」にする。分からない形はそのまま返す。"""
    import re
    s = str(val).strip().split(" ")[0].split("T")[0]
    for pat in _DATE_PATTERNS:
        m = re.match(pat, s)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return str(val).strip()


def coerce_value(val: str, ftype: str) -> str:
    """スプシの値を、その項目の種類に合わせた形に整える。"""
    s = str(val).strip()
    if ftype == "date":
        return _to_iso_date(s)
    if ftype == "datetime":
        parts = s.replace("T", " ").split(" ", 1)
        day = _to_iso_date(parts[0])
        clock = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "00:00:00"
        if clock.count(":") == 1:
            clock += ":00"
        return f"{day}T{clock}Z"
    if ftype == "boolean":
        low = s.lower()
        if low in _TRUE_WORDS:
            return "true"
        if low in _FALSE_WORDS:
            return "false"
        return s
    if ftype in ("double", "currency", "percent", "int", "long"):
        # 「1,200円」「12%」のような書き方から、数だけ取り出す
        num = s.replace(",", "").replace("￥", "").replace("¥", "").replace("円", "")
        num = num.replace("%", "").replace("％", "").strip()
        return num or s
    return s


def build_records(headers, rows, mapping: dict, skip_empty_key: str = "",
                  field_types: dict = None):
    """スプシの表を、Salesforce に渡すレコードの形に変換する。

    mapping: {スプシの列名: Salesforceの項目API名}
    skip_empty_key: この項目が空の行は投入しない（ふつうは外部IDキーを指定する）
    field_types: 項目の種類（渡すと日付や数値を送れる形に整える）
    空文字の項目は送らない（既存の値を空で上書きしてしまうのを防ぐ）。
    """
    idx = {h: i for i, h in enumerate(headers)}
    types = field_types or {}
    records, skipped = [], 0
    for row in rows:
        rec = {}
        for col, field in (mapping or {}).items():
            if not field or col not in idx:
                continue
            val = row[idx[col]] if idx[col] < len(row) else ""
            val = "" if val is None else str(val).strip()
            if val == "":
                continue
            rec[field] = coerce_value(val, types.get(field, ""))
        if not rec:
            continue
        if skip_empty_key and not rec.get(skip_empty_key):
            skipped += 1
            continue
        records.append(rec)
    return records, skipped


def upsert(sf, object_api: str, external_id_field: str, records, limit: int = 0):
    """UPSERT を実行する（外部IDで突き合わせ、無ければ作成・あれば更新）。

    limit を指定すると、その件数だけ投入する（お試し用）。
    戻り値：{"ok": 成功件数, "ng": 失敗件数, "errors": [失敗の内容...]}
    """
    if limit and limit > 0:
        records = records[:limit]
    if not records:
        return {"ok": 0, "ng": 0, "errors": [], "total": 0}

    ng, errors, ok = 0, [], 0
    # 200件ずつ送る（Salesforce の一括APIの上限に合わせる）
    for i in range(0, len(records), 200):
        chunk = records[i:i + 200]
        try:
            res = getattr(sf.bulk, object_api).upsert(chunk, external_id_field, batch_size=200)
        except Exception as e:
            ng += len(chunk)
            errors.append({"行": f"{i + 1}〜{i + len(chunk)}", "内容": str(e)[:300]})
            continue
        for j, r in enumerate(res or []):
            if r.get("success"):
                ok += 1
            else:
                ng += 1
                _msgs = r.get("errors") or []
                _txt = "；".join(str(m.get("message", m)) for m in _msgs) if _msgs else "原因不明"
                if len(errors) < 50:   # エラーが大量でも画面が壊れないよう上限をつける
                    errors.append({"行": i + j + 2, "内容": _txt[:300]})
    return {"ok": ok, "ng": ng, "errors": errors, "total": len(records)}


def _unescape_properties(text: str) -> str:
    """Java properties 形式のエスケープを、元の日本語に戻す。

    .sdl は Data Loader が書き出す Java properties 形式で、日本語が
    `\\u5DE5\\u4E8B` のように退避されている。先頭にBOM(\\uFEFF)が付くこともあり、
    半角スペースは `\\ ` と書かれる。ここを戻さないと列名が一致しない。
    """
    s = str(text or "")
    try:
        s = s.encode("ascii", "backslashreplace").decode("unicode_escape")
    except Exception:
        pass
    return s.replace("\\ ", " ").replace("﻿", "").strip()


def parse_sdl(text: str) -> dict:
    """Data Loader のマッピングファイル(.sdl)を読み込んで {列名: 項目API名} にする。
    いままで使っていた対応表をそのまま使えるようにするため（作り直しの手間と写し間違いを防ぐ）。
    形式は `列名=項目API名` の行が並んだもの。#で始まる行と空行は無視する。"""
    mapping = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        col, field = line.split("=", 1)
        col = _unescape_properties(col)
        field = field.strip()
        if col and field:
            mapping[col] = field
    return mapping
