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
