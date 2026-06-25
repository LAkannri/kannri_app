"""
担当キャラ（ロール別の案内役）を定義する共通モジュール。

エンカンAI には 3 人の案内役がいて、それぞれの担当業務をやさしくナビします。
- ロクすけ（録画担当）：自動化を「つくる」
- ミハリ（運用担当）  ：自動化を「見守る」
- カンナ（管理者）    ：全体を「ととのえる（管理）」

どのページからも `import characters` で使えます（プロジェクト直下に配置）。
"""
import streamlit as st

# ロールキー → キャラ設定
CHARACTERS = {
    "create": {
        "name": "ロクすけ",
        "avatar": "🎬",
        "role": "録画担当",
        "mission": "自動化をつくる",
        "color": "#0EA5E9",   # sky
        "bg": "#F0F9FF",
        "page": "pages/2_📝_エントリー業務自動化.py",
        "tagline": "お手本を一度見せてくれたら、ロボットの手順書はぼくが作るよ！",
    },
    "operate": {
        "name": "ミハリ",
        "avatar": "👀",
        "role": "運用担当",
        "mission": "自動化を見守る",
        "color": "#16A34A",   # green
        "bg": "#F0FDF4",
        "page": "pages/1_📊_全状況進捗確認.py",
        "tagline": "今日の案件がちゃんと処理できたか、わたしが一緒に見張るよ。",
    },
    "manage": {
        "name": "カンナ",
        "avatar": "⚙️",
        "role": "管理者",
        "mission": "全体を管理する",
        "color": "#9333EA",   # purple
        "bg": "#FAF5FF",
        "page": "pages/5_⚙️_その他設定.py",
        "tagline": "接続キーやロボットの稼働、クラウド実行の設定はわたしにおまかせ。",
    },
}


def get(key: str) -> dict:
    """ロールキーからキャラ設定を取り出す。"""
    return CHARACTERS[key]


def guide(key: str, message: str):
    """
    担当キャラがしゃべる吹き出しを表示する（チャット風）。
    画面上部やステップの節目に置くと、案内役がついている安心感を出せる。
    """
    c = CHARACTERS[key]
    with st.chat_message(c["name"], avatar=c["avatar"]):
        st.markdown(
            f"<span style='font-weight:700;color:{c['color']}'>"
            f"{c['name']}（{c['role']}）</span><br>{message}",
            unsafe_allow_html=True,
        )


def hero(key: str, subtitle: str = ""):
    """ページ冒頭に置く、担当キャラ入りの見出しバナー。"""
    c = CHARACTERS[key]
    sub = f"<div style='color:#666;margin-top:6px;font-size:14px;'>{subtitle}</div>" if subtitle else ""
    st.markdown(
        f"""
        <div style="background:{c['bg']};border-left:8px solid {c['color']};
             border-radius:16px;padding:20px 24px;margin-bottom:20px;
             display:flex;align-items:center;gap:18px;">
          <div style="font-size:46px;line-height:1;">{c['avatar']}</div>
          <div>
            <div style="font-size:22px;font-weight:700;color:{c['color']};">{c['mission']}</div>
            <div style="color:#444;margin-top:2px;">{c['name']}（{c['role']}）が案内します</div>
            {sub}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def role_card(key: str):
    """
    ホーム画面に置く、ロール選択カード。
    カードの下に、その担当ページへ移動できるリンクを表示する。
    """
    c = CHARACTERS[key]
    st.markdown(
        f"""
        <div style="background:{c['bg']};border:2px solid {c['color']}33;
             border-radius:20px;padding:24px;margin-bottom:12px;min-height:210px;
             box-shadow:0 8px 20px {c['color']}14;">
          <div style="font-size:54px;line-height:1;">{c['avatar']}</div>
          <div style="font-size:20px;font-weight:700;color:{c['color']};margin-top:12px;">
            {c['mission']}
          </div>
          <div style="color:#555;font-weight:600;margin-top:2px;">
            {c['name']}（{c['role']}）
          </div>
          <div style="color:#666;font-size:14px;margin-top:10px;line-height:1.6;">
            {c['tagline']}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.page_link(c["page"], label=f"{c['avatar']} {c['mission']}", use_container_width=True)
