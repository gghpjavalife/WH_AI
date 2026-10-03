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
.gghp-brand {{ display: flex; align-items: center; gap: 1rem; padding: .25rem 0; }}
.gghp-badge {{
  width: 3rem; height: 3rem; border-radius: 14px; flex: none;
  display: grid; place-items: center; color: #fff; font-size: 1.5rem;
  background: linear-gradient(135deg, #1d4ed8, #7c3aed);
}}
.gghp-name {{
  margin: 0; font-size: 2.1rem; font-weight: 800; letter-spacing: .08em; line-height: 1.1;
  background: linear-gradient(90deg, #2563eb, #7c3aed, #0ea5e9, #2563eb);
  background-size: 200% auto; -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.gghp-expansion {{ margin: .15rem 0 0; font-weight: 700; font-size: .95rem; }}
.gghp-tagline {{ margin: 0; font-size: .85rem; opacity: .7; }}
@media (prefers-reduced-motion: no-preference) {{
  .gghp-badge {{ animation: gghp-shield-pulse 3s ease-in-out infinite; }}
  .gghp-name {{ animation: gghp-shimmer 6s linear infinite, gghp-fade-up .6s both; }}
  .gghp-expansion {{ animation: gghp-fade-up .6s .15s both; }}
  .gghp-tagline {{ animation: gghp-fade-up .6s .3s both; }}
}}
</style>
<div class="gghp-brand">
  <div class="gghp-badge" aria-hidden="true">&#128737;</div>
  <div>
    <h1 class="gghp-name">{escape(name)}</h1>
    <p class="gghp-expansion">{escape(expansion)}</p>
    <p class="gghp-tagline">{escape(description)}</p>
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
[data-testid="stTabs"] [role="tablist"] { gap: .25rem; }
[data-testid="stTabs"] [role="tab"] { border-radius: .55rem .55rem 0 0; }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] { color: var(--gghp-gold-bright); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background-color: var(--gghp-gold-bright) !important; height: 4px !important; }
[data-testid="stDataFrame"], [data-testid="stPlotlyChart"] { border: 2px solid var(--gghp-gold); border-radius: 8px; }
[data-testid="stBaseButton-primary"] {
  background: linear-gradient(135deg, #E6B800, #B8860B) !important;
  border-color: #8A6508 !important; color: #1B1400 !important; font-weight: 700;
}
.gghp-brand { padding-bottom: .75rem !important; border-bottom: 4px solid var(--gghp-gold); margin-bottom: .5rem; }
.gghp-badge { background: linear-gradient(135deg, #E6B800, #B8860B) !important; color: #1B1400 !important; }
.gghp-name { background-image: linear-gradient(90deg, #B8860B, #FFD700, #E6B800, #B8860B) !important; }
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
