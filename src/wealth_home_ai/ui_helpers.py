"""Small, framework-independent helpers for app UI markup."""

from html import escape


BROKER_CONNECT_BUTTON_KEYS = (
    "connect_upstox",
    "connect_angel_one",
    "connect_zerodha",
    "connect_dhan",
)


def broker_connect_button_css() -> str:
    selectors = ", ".join(
        f".st-key-{key} button" for key in BROKER_CONNECT_BUTTON_KEYS
    )
    return (
        "<style>"
        f"{selectors}{{background:linear-gradient(135deg,#22c55e 0%,#15803d 100%)"
        "!important;border:1px solid #16a34a!important;border-radius:.75rem"
        "!important;box-shadow:0 5px 14px rgba(22,163,74,.28)!important;"
        "color:#fff!important;font-weight:650!important;transition:transform .16s"
        " ease,box-shadow .16s ease,filter .16s ease!important}"
        f"{selectors}:hover:not(:disabled){{filter:brightness(1.08);"
        "box-shadow:0 8px 20px rgba(22,163,74,.38)!important;"
        "transform:translateY(-1px)}}"
        f"{selectors}:focus-visible{{outline:3px solid #86efac!important;"
        "outline-offset:3px}}"
        "</style>"
    )


def app_animation_css() -> str:
    return """<style>
@keyframes guru-rise-in {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (prefers-reduced-motion: no-preference) {
  [data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"] > div {
    animation: guru-rise-in 520ms cubic-bezier(.2,.75,.25,1) both;
  }
  [data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"] > div:nth-child(2) {
    animation-delay: 55ms;
  }
  [data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"] > div:nth-child(3) {
    animation-delay: 110ms;
  }
  [data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"] > div:nth-child(4) {
    animation-delay: 165ms;
  }
  [data-testid="stMetric"],
  [data-testid="stPlotlyChart"],
  [data-testid="stVegaLiteChart"],
  [data-testid="stDataFrame"],
  [data-testid="stAlert"] {
    animation: guru-rise-in 480ms cubic-bezier(.2,.75,.25,1) both;
  }
  [data-testid="stMetric"] {
    transition: transform 220ms ease, box-shadow 220ms ease;
  }
  [data-testid="stMetric"]:hover {
    transform: translateY(-3px);
    box-shadow: 0 10px 24px rgba(29, 78, 216, .12);
  }
  [data-testid="stButton"] button,
  [data-testid="stDownloadButton"] button {
    transition: transform 180ms ease, box-shadow 180ms ease, filter 180ms ease;
  }
  [data-testid="stButton"] button:hover:not(:disabled),
  [data-testid="stDownloadButton"] button:hover:not(:disabled) {
    transform: translateY(-2px);
    box-shadow: 0 6px 16px rgba(29, 78, 216, .16);
  }
  [data-testid="stTabs"] button {
    transition: color 180ms ease, background-color 180ms ease;
  }
  [data-testid="stTextInput"] input,
  [data-testid="stNumberInput"] input,
  [data-testid="stSelectbox"] [role="combobox"] {
    transition: box-shadow 180ms ease, border-color 180ms ease;
  }
  [data-testid="stTextInput"] input:focus,
  [data-testid="stNumberInput"] input:focus,
  [data-testid="stSelectbox"] [role="combobox"]:focus {
    box-shadow: 0 0 0 3px rgba(29, 78, 216, .16);
  }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation: none !important;
    transition: none !important;
    scroll-behavior: auto !important;
  }
}
</style>"""


def brand_lockup_html(name: str, expansion: str, description: str) -> str:
    """Animated brand lockup: shield pulse, gradient wordmark, staggered text."""
    return f"""<style>
@keyframes gghp-shield-pulse {{
  0%, 100% {{ box-shadow: 0 0 0 0 rgba(37, 99, 235, .45); }}
  50% {{ box-shadow: 0 0 0 10px rgba(37, 99, 235, 0); }}
}}
@keyframes gghp-shimmer {{
  from {{ background-position: 0% 50%; }}
  to {{ background-position: 200% 50%; }}
}}
@keyframes gghp-fade-up {{
  from {{ opacity: 0; transform: translateY(8px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
.gghp-brand {{ display: flex; align-items: center; gap: .85rem; padding: .2rem 0; min-width: min(100%, 22rem); }}
.gghp-badge {{
  width: 3.15rem; height: 3.15rem; border-radius: 15px; flex: none;
  display: grid; place-items: center; color: #fff; font-size: 1.5rem;
  background: linear-gradient(145deg, #E6B800, #A87500); border: 1px solid rgba(255, 220, 100, .65);
  box-shadow: 0 6px 18px rgba(201, 162, 39, .22);
}}
.gghp-name {{
  margin: 0; font-size: 1.8rem; font-weight: 850; letter-spacing: .11em; line-height: 1.05;
  color: #F2C94C; text-shadow: 0 2px 16px rgba(230, 184, 0, .14);
  background-image: none !important; -webkit-text-fill-color: #F2C94C !important;
}}
.gghp-expansion {{ margin: .24rem 0 0; font-weight: 750; font-size: .88rem; color: #E6EAF2; letter-spacing: .01em; }}
.gghp-expansion .gghp-initial {{ color: #F2C94C; font-weight: 850; text-shadow: 0 1px 8px rgba(230, 184, 0, .22); }}
.gghp-tagline {{ margin: .08rem 0 0; font-size: .78rem; color: #B5BFCE; }}
@media (max-width: 640px) {{
  .gghp-brand {{ gap: .65rem; }}
  .gghp-badge {{ width: 2.7rem; height: 2.7rem; border-radius: 12px; }}
  .gghp-name {{ font-size: 1.55rem; }}
  .gghp-expansion {{ font-size: .8rem; }}
  .gghp-tagline {{ font-size: .72rem; }}
}}
@media (prefers-reduced-motion: no-preference) {{
  .gghp-badge {{ animation: gghp-shield-pulse 3s ease-in-out infinite; }}
  .gghp-name {{ animation: gghp-shimmer 6s linear infinite, gghp-fade-up .6s both; }}
  .gghp-expansion {{ animation: gghp-fade-up .6s .15s both; }}
  .gghp-tagline {{ animation: gghp-fade-up .6s .3s both; }}
}}
</style>
<div class="gghp-brand">
  <div class="gghp-badge" aria-hidden="true">&#128737;</div>
  <div class="gghp-copy">
    <h1 class="gghp-name">{escape(name)}</h1>
    <p class="gghp-expansion"><span class="gghp-initial">G</span>overned <span class="gghp-initial">G</span>rowth &amp; <span class="gghp-initial">H</span>edged <span class="gghp-initial">P</span>ortfolios</p>
    <p class="gghp-tagline"><span class="gghp-tagline-lead">Governed multi-broker portfolio companion</span></p>
  </div>
</div>"""


def gold_theme_css() -> str:
    """Gold-accented dark theme with consistent bordered panels."""
    return """<style>
:root { --gghp-gold: #C9A227; --gghp-gold-bright: #E6B800; --gghp-gold-soft: rgba(201, 162, 39, .16); }
[data-testid="stAppViewContainer"] {
  background: radial-gradient(130% 90% at 50% -20%, rgba(230, 184, 0, .09), transparent 62%), #0B1220;
}
[data-testid="stMainBlockContainer"] {
  padding-top: 4rem;
  padding-bottom: 2.4rem;
  max-width: 1260px;
}
[data-testid="stVerticalBlockBorderWrapper"] {
  border: 1.5px solid var(--gghp-gold) !important;
  border-radius: 14px !important;
  background: linear-gradient(180deg, rgba(16, 24, 39, .96), rgba(11, 18, 32, .98));
  box-shadow: 0 16px 32px rgba(0, 0, 0, .28);
}
[data-testid="stMetric"] {
  border: 2px solid var(--gghp-gold) !important;
  border-top-width: 3px !important;
  background: linear-gradient(180deg, var(--gghp-gold-soft), transparent 60%);
}
[data-testid="stExpander"] details { border: 2px solid var(--gghp-gold) !important; }
[data-testid="stExpander"] summary:hover { background: var(--gghp-gold-soft); }
[data-testid="stTabs"] [role="tablist"] { gap: .4rem; justify-content: space-between; border-bottom: 1px solid rgba(201, 162, 39, .5); }
[data-testid="stTabs"] [role="tab"] { flex: 1 1 0; justify-content: center; border: 1px solid rgba(201, 162, 39, .32); border-bottom: 0; border-radius: .6rem .6rem 0 0; padding: .55rem .35rem; transition: background .18s ease, color .18s ease; }
[data-testid="stTabs"] [role="tab"]:hover { background: rgba(255, 255, 255, .055); }
[data-testid="stTabs"] [role="tab"] [data-testid="stIconMaterial"] { color: #C9A227 !important; }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] { color: var(--gghp-gold-bright); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background-color: var(--gghp-gold-bright) !important; height: 4px !important; }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) { border-color: rgba(201, 162, 39, .7) !important; background: linear-gradient(135deg, rgba(20, 28, 43, .98), rgba(11, 18, 32, .98)); }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBadge"] { border: 1px solid rgba(255, 255, 255, .12); }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBadge"]:has([data-testid="stIconMaterial"]) { border-radius: 999px; padding: .24rem .72rem; box-shadow: 0 2px 12px rgba(0, 0, 0, .2); font-weight: 750; }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBadge"]:has([data-testid="stIconMaterial"]):nth-of-type(1) { border-color: rgba(167, 139, 250, .52); box-shadow: 0 0 0 1px rgba(167, 139, 250, .14), 0 3px 12px rgba(124, 58, 237, .18); }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBadge"]:has([data-testid="stIconMaterial"]):nth-of-type(2) { border-color: rgba(230, 184, 0, .62); box-shadow: 0 0 0 1px rgba(230, 184, 0, .16), 0 3px 12px rgba(201, 162, 39, .2); }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBadge"]:has([data-testid="stIconMaterial"]) [data-testid="stIconMaterial"] { color: #F2C94C !important; filter: none !important; }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBaseButton-secondary"] { border-color: rgba(201, 162, 39, .48); background: rgba(255, 255, 255, .035); }
[data-testid="stVerticalBlockBorderWrapper"]:has(.gghp-brand) [data-testid="stBaseButton-secondary"]:hover { border-color: #E6B800; background: rgba(201, 162, 39, .12); }
[data-testid="stPopover"] [data-testid="stBaseButton-tertiary"] [data-testid="stIconMaterial"] { color: #E6B800 !important; }
[data-testid="stPopover"] [data-testid="stBaseButton-tertiary"]:hover [data-testid="stIconMaterial"] { color: #FFD95A !important; }
[data-testid="stPopover"] [data-testid="stBaseButton-tertiary"]:focus-visible { outline: 2px solid #E6B800 !important; outline-offset: 2px; }
[data-testid="stDataFrame"], [data-testid="stPlotlyChart"] { border: 2px solid var(--gghp-gold); border-radius: 8px; }
[data-testid="stBaseButton-primary"] {
  background: linear-gradient(135deg, #E6B800, #B8860B) !important;
  border-color: #8A6508 !important; color: #1B1400 !important; font-weight: 700;
}
.gghp-brand { padding: .2rem 0 !important; border: 0 !important; margin-bottom: 0; background: transparent !important; }
.gghp-badge { background: linear-gradient(135deg, #E6B800, #B8860B) !important; color: #1B1400 !important; }
.gghp-name { background-image: none !important; color: #F2C94C !important; -webkit-text-fill-color: #F2C94C !important; }
@media (prefers-reduced-motion: no-preference) {
  .gghp-badge { animation-name: gghp-gold-pulse !important; }
  @keyframes gghp-gold-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(230, 184, 0, .5); }
    50% { box-shadow: 0 0 0 10px rgba(230, 184, 0, 0); }
  }
}
</style>"""


def same_tab_link_html(label: str, url: str) -> str:
    safe_url = escape(url, quote=True)
    safe_label = escape(label)
    return (
        f'<a href="{safe_url}" target="_self" rel="noopener noreferrer" '
        'style="display:inline-flex;align-items:center;justify-content:center;'
        'gap:.55rem;padding:.72rem 1.25rem;border:1px solid #16a34a;'
        'border-radius:.75rem;background:linear-gradient(135deg,#22c55e 0%,'
        '#15803d 100%);box-shadow:0 5px 14px rgba(22,163,74,.28);color:#fff;'
        'font-weight:600;text-decoration:none">'
        f"<span>{safe_label}</span>"
        '<span aria-hidden="true" style="font-size:1.1rem">&#8594;</span></a>'
    )
