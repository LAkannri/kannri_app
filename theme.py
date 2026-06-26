"""
エンカンAI 共有デザインシステム（theme）。

これまで各ページにバラバラに書かれていたCSS（フォント・色・ボタン・カード等）を
ここに一本化する。各ページは先頭で `theme.inject_theme()` を呼ぶだけで、
アプリ全体で統一された「やさしい・丸み・水色」の見た目になる。

提供するもの：
- inject_theme()        … 全体CSSを注入（フォント/背景/ボタン/カード/バッジ等）
- brand_sidebar()       … サイドバーにブランド＋案内役3人を常設
- page_header(...)      … ページ冒頭の見出しバナー（キャラ対応）
- section_title(...)    … カード内のセクション見出し
- COLORS               … 色トークン
"""
import streamlit as st
import characters as ch

# --- 色トークン（ここを変えれば全体の色が変わる） ---
COLORS = {
    "primary": "#0284C7",      # メインの水色
    "primary_dark": "#0369A1",
    "primary_light": "#E0F2FE",
    "border": "#E2E8F0",
    "ink": "#1F2937",          # 本文の文字色
    "ink_soft": "#475569",
    "bg": "#F8FAFC",           # アプリ背景
    "success": "#16A34A",
    "warning": "#D97706",
    "danger": "#EF4444",
}

_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=M+PLUS+Rounded+1c:wght@400;500;700;800&display=swap');

    /* --- 全体：丸みフォント＋落ち着いた背景 --- */
    html, body, [class*="css"] {
        font-family: 'M PLUS Rounded 1c', sans-serif !important;
        color: #1F2937 !important;
    }
    .stApp { background-color: #F8FAFC; }
    h1, h2, h3 { color: #0F172A; font-weight: 800; letter-spacing: .2px; }

    /* --- ボタン（丸み＋水色＋やわらかい影） --- */
    div[data-testid="stButton"] button {
        border-radius: 12px;
        font-weight: 700;
        border: 2px solid #BAE6FD;
        background-color: #FFFFFF;
        color: #0284C7;
        box-shadow: 0 2px 6px rgba(2,132,199,0.08);
        transition: all 0.2s ease;
    }
    div[data-testid="stButton"] button:hover {
        background-color: #F0F9FF;
        border-color: #38BDF8;
        transform: translateY(-1px);
        box-shadow: 0 6px 16px rgba(2,132,199,0.15);
    }
    /* 主要アクション（type="primary"）は塗りつぶし */
    div[data-testid="stButton"] button[kind="primary"] {
        background-color: #0284C7; color: #FFFFFF; border-color: #0284C7;
    }
    div[data-testid="stButton"] button[kind="primary"]:hover {
        background-color: #0369A1; border-color: #0369A1;
    }

    /* --- st.container(border=True) をやさしいカードに --- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 18px !important;
        border-color: #E2E8F0 !important;
        box-shadow: 0 4px 14px rgba(15,23,42,0.04);
    }

    /* --- 入力欄の角丸 --- */
    .stTextInput input, .stTextArea textarea { border-radius: 10px; }
    div[data-baseweb="select"] { border-radius: 10px; }

    /* --- ページ移動リンクを大きめボタン風に --- */
    div[data-testid="stPageLink"] a {
        border-radius: 12px;
        border: 2px solid #E2E8F0;
        padding: 10px 14px;
        font-weight: 700;
        justify-content: center;
        transition: all 0.2s ease;
    }
    div[data-testid="stPageLink"] a:hover {
        background-color: #F0F9FF;
        border-color: #BAE6FD;
        transform: translateY(-1px);
    }

    /* --- カード／見出しの共通クラス（各ページから利用） --- */
    .enkan-card {
        background: #FFFFFF; padding: 24px; border-radius: 18px;
        border: 1px solid #E2E8F0; margin-bottom: 20px;
        box-shadow: 0 4px 14px rgba(15,23,42,0.04);
    }
    .section-title {
        font-size: 18px; font-weight: 800; color: #0369A1;
        display: flex; align-items: center; gap: 8px; margin-bottom: 14px;
    }
    .wizard-header {
        background: linear-gradient(180deg, #F0F9FF, #FFFFFF);
        padding: 22px 24px; border-radius: 16px;
        border-left: 8px solid #38BDF8; margin-bottom: 24px;
        box-shadow: 0 4px 14px rgba(56,189,248,0.08);
    }
    .wizard-header h1, .wizard-header h2 { margin: 0; }
    .wizard-header p { margin: 6px 0 0; color: #475569; }

    /* --- 稼働状態バッジ（これまで未定義で効いていなかった） --- */
    .status-active {
        display: inline-block; padding: 3px 12px; border-radius: 999px;
        background: #DCFCE7; color: #15803D; font-weight: 700; font-size: 13px;
    }
    .status-inactive {
        display: inline-block; padding: 3px 12px; border-radius: 999px;
        background: #F1F5F9; color: #64748B; font-weight: 700; font-size: 13px;
    }

    /* --- サイドバーのブランド --- */
    .enkan-brand-name { font-size: 22px; font-weight: 800; color: #0284C7; }
    .enkan-brand-sub  { color: #64748B; font-size: 13px; margin-top: 2px; }
    .enkan-cast-row   { display:flex; align-items:center; gap:8px; margin:8px 0; }
    .enkan-cast-emoji { font-size: 22px; line-height: 1; }
    .enkan-cast-name  { font-weight: 700; font-size: 14px; }
    .enkan-cast-role  { color:#64748B; font-size: 12px; }
</style>
"""


def inject_theme():
    """全ページ共通のCSSを注入する（各ページの先頭で1回呼ぶ）。"""
    st.markdown(_CSS, unsafe_allow_html=True)


def brand_sidebar(active: str = None):
    """
    サイドバーにブランド名と「案内役の3人」を常設する。
    active にロールキー（create/operate/manage）を渡すと、その担当を強調表示。
    """
    with st.sidebar:
        st.markdown(
            "<div class='enkan-brand-name'>🏠 エンカンAI</div>"
            "<div class='enkan-brand-sub'>事務作業の自動化パートナー</div>",
            unsafe_allow_html=True,
        )
        st.divider()
        st.markdown("<div style='font-weight:800;color:#334155;margin-bottom:4px;'>案内役の3人</div>",
                    unsafe_allow_html=True)
        for key in ("create", "operate", "manage"):
            c = ch.get(key)
            highlight = (key == active)
            bg = f"background:{c['bg']};border-radius:10px;padding:6px 8px;" if highlight else ""
            st.markdown(
                f"<div class='enkan-cast-row' style='{bg}'>"
                f"<span class='enkan-cast-emoji'>{c['avatar']}</span>"
                f"<span><span class='enkan-cast-name' style='color:{c['color']}'>{c['name']}</span>"
                f"<br><span class='enkan-cast-role'>{c['role']}・{c['mission']}</span></span></div>",
                unsafe_allow_html=True,
            )


def page_header(emoji: str, title: str, subtitle: str = "", color: str = None):
    """ページ冒頭の見出しバナー（wizard-header と同じ世界観）。"""
    color = color or COLORS["primary"]
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(
        f"<div class='wizard-header' style='border-left-color:{color};'>"
        f"<h2 style='color:{COLORS['primary_dark']};'>{emoji} {title}</h2>{sub}</div>",
        unsafe_allow_html=True,
    )


def section_title(emoji: str, text: str):
    """カード内のセクション見出し。"""
    st.markdown(f"<div class='section-title'>{emoji} {text}</div>", unsafe_allow_html=True)
