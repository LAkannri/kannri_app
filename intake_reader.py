"""
📄 取り込んだファイルを読む（形式は自動判別）

キャリアによってファイルの形式がバラバラ（CSV／Excel／パスワード付きZIP…）なので、
担当者に形式を意識させないよう、ここで吸収する。

対応：
  ・CSV      … 文字コードは UTF-8(BOM付き含む) / Shift_JIS を自動で試す
  ・Excel    … .xlsx / .xls（パスワード付きは msoffcrypto-tool で解錠）
  ・ZIP      … 中の最初の表ファイルを読む（パスワード付きは pyzipper / 標準zipで解錠）

戻り値はどれも (見出し, 行) の形にそろえる。呼び出し側は形式を気にしなくてよい。
"""

import io
import os
import re
import zipfile

TABLE_EXT = (".csv", ".txt", ".xlsx", ".xlsm", ".xls")


def _read_csv_bytes(data: bytes, skip_rows: int = 0):
    """CSVを読む。日本語のファイルは Shift_JIS のことが多いので順に試す。"""
    import csv
    last = None
    for enc in ("utf-8-sig", "cp932", "utf-8", "euc_jp"):
        try:
            text = data.decode(enc)
        except Exception as e:
            last = e
            continue
        rows = list(csv.reader(io.StringIO(text)))
        rows = [r for r in rows if any(str(c).strip() for c in r)]   # 空行は捨てる
        if not rows:
            return [], []
        rows = rows[skip_rows:] if skip_rows else rows
        if not rows:
            return [], []
        return rows[0], rows[1:]
    raise ValueError(f"CSVの文字コードを判別できませんでした: {last}")


def _read_excel_bytes(data: bytes, password: str = "", skip_rows: int = 0):
    """Excelを読む。パスワード付きなら解錠してから読む。"""
    import pandas as pd
    buf = io.BytesIO(data)
    # パスワード付き（暗号化）Excelかどうかは、開いてみないと分からないので順に試す
    try:
        df = pd.read_excel(buf, header=None, dtype=str)
    except Exception:
        if not password:
            raise ValueError("このExcelは開けませんでした（パスワード付きの可能性）。"
                             "解錠パスワードを設定してください。")
        try:
            import msoffcrypto
        except ImportError:
            raise ValueError("パスワード付きExcelを開くには msoffcrypto-tool が必要です"
                             "（requirements.txt に入っています。update.bat で更新してください）")
        buf.seek(0)
        dec = io.BytesIO()
        office = msoffcrypto.OfficeFile(buf)
        office.load_key(password=password)
        office.decrypt(dec)
        dec.seek(0)
        df = pd.read_excel(dec, header=None, dtype=str)
    df = df.fillna("")
    values = df.astype(str).values.tolist()
    values = [r for r in values if any(str(c).strip() for c in r)]
    values = values[skip_rows:] if skip_rows else values
    if not values:
        return [], []
    return values[0], values[1:]


def _extract_from_zip(data: bytes, password: str = "", inner_pattern: str = ""):
    """ZIPの中から、目当ての表ファイル（CSV/Excel）を1つ取り出す。

    1つのZIPに、電力会社ごとのファイルが何本も入っていることがある
    （orders_toden… / orders_nichigas… など）。どれを使うかは
    inner_pattern（ファイル名に含まれる文字）で選ぶ。
    指定が無ければ、いちばん最初の表ファイルを使う。
    パスワード付きにも対応（AES暗号のZIPは pyzipper が必要）。
    """
    pat = str(inner_pattern or "").strip()

    def _tables(names):
        return [n for n in names
                if n.lower().endswith(TABLE_EXT) and not n.startswith("__MACOSX")]

    def _pick(names):
        cands = _tables(names)
        if not cands:
            return None
        if not pat:
            return cands[0]
        try:
            hit = [n for n in cands if re.search(pat, os.path.basename(n), re.IGNORECASE)]
        except re.error:                       # 正規表現として不正なら、ただの文字として探す
            hit = [n for n in cands if pat.lower() in os.path.basename(n).lower()]
        if not hit:
            raise ValueError(
                f"ZIPの中に「{pat}」に当てはまるファイルがありません。"
                f"入っているのは：{'、'.join(os.path.basename(n) for n in cands[:10])}")
        return sorted(hit)[0]

    pw = password.encode() if password else None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        name = _pick(zf.namelist())
        if not name:
            raise ValueError("ZIPの中に、読み取れる表ファイル（CSV/Excel）が見つかりませんでした。")
        return name, zf.read(name, pwd=pw)
    except RuntimeError as e:
        # 暗号方式が合わない（AES）ときは pyzipper で開き直す
        if "encrypted" not in str(e).lower() and "password" not in str(e).lower():
            raise
        try:
            import pyzipper
        except ImportError:
            raise ValueError("このZIPを開くには pyzipper が必要です"
                             "（requirements.txt に入っています。update.bat で更新してください）")
        with pyzipper.AESZipFile(io.BytesIO(data)) as zf:
            if pw:
                zf.setpassword(pw)
            name = _pick(zf.namelist())
            if not name:
                raise ValueError("ZIPの中に、読み取れる表ファイルが見つかりませんでした。")
            return name, zf.read(name)


def read_table(data: bytes, filename: str, password: str = "", skip_rows: int = 0,
               inner_pattern: str = ""):
    """ファイルの中身（バイト列）を読んで (見出し, 行) を返す。

    filename は形式の判定に使う。skip_rows はファイル側の見出しが複数行あるときに、
    その行数を読み飛ばすため（貼り付け先の見出しは残したいので、ここで捨てる）。
    inner_pattern は、ZIPの中に何本も入っているときに使うファイルを選ぶための文字。
    """
    name = str(filename or "").lower()
    if name.endswith(".zip"):
        inner, inner_data = _extract_from_zip(data, password, inner_pattern)
        return read_table(inner_data, inner, password=password, skip_rows=skip_rows)
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return _read_excel_bytes(data, password=password, skip_rows=skip_rows)
    if name.endswith((".csv", ".txt")):
        return _read_csv_bytes(data, skip_rows=skip_rows)
    # 拡張子が無い・違う場合は中身から推測する（ZIPは PK で始まる）
    if data[:2] == b"PK":
        inner, inner_data = _extract_from_zip(data, password, inner_pattern)
        return read_table(inner_data, inner, password=password, skip_rows=skip_rows)
    return _read_csv_bytes(data, skip_rows=skip_rows)


def compare_headers(file_headers, sheet_headers):
    """ファイルの見出しと、貼り付け先シートの見出しを突き合わせる。

    別のキャリアのファイルを取り込んでしまう事故を、貼る前に見つけるための確認。
    戻り値：(一致しているか, ファイルにしか無い列, シートにしか無い列)
    """
    def _norm(x):
        return re.sub(r"\s+", "", str(x or "")).lower()

    f = [_norm(h) for h in file_headers if str(h).strip()]
    s = [_norm(h) for h in sheet_headers if str(h).strip()]
    only_file = [h for h, n in zip(file_headers, f) if n and n not in s]
    only_sheet = [h for h, n in zip(sheet_headers, s) if n and n not in f]
    return (not only_file and not only_sheet), only_file, only_sheet


def list_zip_tables(data: bytes, password: str = ""):
    """ZIPの中に入っている表ファイルの名前を返す（画面で選ばせるため）。"""
    names = []
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except Exception:
        try:
            import pyzipper
            with pyzipper.AESZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
        except Exception:
            return []
    return [os.path.basename(n) for n in names
            if n.lower().endswith(TABLE_EXT) and not n.startswith("__MACOSX")]
