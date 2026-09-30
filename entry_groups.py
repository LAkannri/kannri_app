"""
📁 エントリー業務自動化のホームの「タブ」（N／LL／LL(SW)／その他）

⭐ どのタブに出すかは、**スプシから決める**（人に振り分けさせない）。
SFレポート更新（`__reports__`）とエントリー後の投入（`__entry_loads__`）のセットに
`group` を持たせ、そのセットに入っているスプシを使うロボットは、そのタブに出す。
⚠️ スプシのURLはコードに書かない（公開リポジトリ）。

どのスプシとも合わないロボットは「その他のエントリー」。
エントリーではないロボット（HTB同意メールの再送など）は、ロボットの設定で
`entry_group = "non_entry"` にしたものだけ「エントリー以外」に出す。
"""
import re

GROUPS = [("N", "🌐 N（ネット）"), ("LL", "⚡ LL（電気・ガス）"), ("LLSW", "🔁 LL(SW)")]
OTHER = "other"          # その他のエントリー
NON_ENTRY = "non_entry"  # エントリー以外
TABS = GROUPS + [(OTHER, "🧩 その他")]

# ロボットの設定で選べる置き場所（空＝スプシから自動で決める）
PLACES = {"": "自動（使っているスプシで決める）",
          "N": "🌐 N（ネット）", "LL": "⚡ LL（電気・ガス）", "LLSW": "🔁 LL(SW)",
          OTHER: "🧩 その他のエントリー", NON_ENTRY: "📨 エントリー以外"}


def label(group: str) -> str:
    return dict(TABS).get(group, group)


def sheet_key(url: str) -> str:
    """スプシのURLから、そのスプシを見分ける文字（/d/ のあと）を取り出す。"""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", str(url or ""))
    return m.group(1) if m else ""


def _cfg(supabase, row_id: str) -> dict:
    try:
        res = supabase.table("merchants").select("config_json").eq("id", row_id).execute()
        return (res.data[0].get("config_json") or {}) if res.data else {}
    except Exception:
        return {}


def group_sheets(supabase) -> dict:
    """{group: {スプシの見分け文字,…}}。更新セットと投入セットの両方から集める。"""
    out = {g: set() for g, _ in GROUPS}
    for row_id, key in (("__reports__", "sheets"), ("__entry_loads__", "loads")):
        for s in _cfg(supabase, row_id).get("sets", []) or []:
            g = s.get("group")
            if g in out:
                out[g].update(k for k in (sheet_key(x.get("url")) for x in s.get(key, []) or []) if k)
    return out


def robot_group(proj: dict, gmap: dict) -> str:
    """ロボットを出すタブ。設定で決めてあればそれ、無ければ使っているスプシで決める。"""
    cfg = proj.get("config_json") or {}
    g = str(cfg.get("entry_group") or "")
    if g in PLACES and g:
        return g
    k = sheet_key((cfg.get("spreadsheet") or {}).get("url"))
    for grp, keys in gmap.items():
        if k and k in keys:
            return grp
    return OTHER
