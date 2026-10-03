"""Local, deterministic help answers for GGHP workflows."""

from __future__ import annotations

import re


HELP_TOPICS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "Broker connections",
        (
            "broker",
            "connect",
            "connection",
            "upstox",
            "angel one",
            "zerodha",
            "dhan",
            "api key",
            "login",
            "sign in",
        ),
        (
            "Go to **Home**, choose a broker, enter its required credentials, "
            "and follow its authorization flow. Built-in connections are "
            "available for Upstox, Angel One, Zerodha, and Dhan. Callback URLs "
            "must match the value registered with that broker. Tokens and "
            "portfolio data stay in the active app session; use **Switch "
            "account** in the header to disconnect."
        ),
    ),
    (
        "Workspaces",
        (
            "workspace",
            "tab",
            "tabs",
            "where",
            "equity",
            "trades",
            "trading",
            "f&o",
            "fo",
            "mutual fund",
            "mutual funds",
            "home page",
        ),
        (
            "Use the workspace tabs in the header: **Home** for connection and "
            "portfolio overview, **Equities** for long-term holdings, "
            "**Trades** for trading positions, **F&O** for derivatives, "
            "and **Mutual Funds** for fund holdings and imports."
        ),
    ),
    (
        "Market research and scanner",
        (
            "research",
            "scanner",
            "scan",
            "search stock",
            "find stock",
            "indicators",
            "market",
            "rsi",
            "technical",
            "without broker",
            "no broker",
        ),
        (
            "Market research and scanning are inside the relevant **Equities** "
            "and **Trades** workspaces, not separate header tabs. Stock "
            "research works without a broker using cached public daily NSE "
            "history, which may be delayed or unavailable. Indicators and "
            "signals are historical and informational, not guaranteed "
            "predictions or personalized investment advice."
        ),
    ),
    (
        "Mutual funds",
        (
            "mutual fund",
            "mutual funds",
            "mf",
            "fund",
            "funds",
            "nav",
            "statement csv",
            "import fund",
        ),
        (
            "Open **Mutual Funds**. Holdings are fetched for brokers that expose "
            "an MF feed; otherwise import a current statement CSV there. "
            "Upstox and Zerodha provide an MF holdings feed in this app; Angel "
            "One and Dhan require a statement import."
        ),
    ),
    (
        "F&O support",
        (
            "f&o",
            "fo",
            "option",
            "options",
            "future",
            "futures",
            "derivative",
            "option chain",
            "strike",
            "expiry",
        ),
        (
            "The **F&O** workspace is present, but verified live contract, "
            "option-chain, quote, and margin feeds are not integrated yet. "
            "Live F&O scanning and order execution are therefore unavailable."
        ),
    ),
    (
        "AI portfolio analysis",
        (
            "ai",
            "analysis",
            "analyze",
            "recommendation",
            "llm",
            "provider",
            "model",
            "gemini",
            "groq",
            "openai",
            "anthropic",
            "api key",
        ),
        (
            "Portfolio analysis is optional and does require a key for the "
            "selected AI provider. Configure the provider, API key, and model "
            "beside the portfolio-analysis control, then run analysis "
            "explicitly. **Ask about this Agent does not use that key**; it "
            "answers app-help questions locally. Analysis is advisory and "
            "provider quotas or charges may apply."
        ),
    ),
    (
        "Risk and trading controls",
        (
            "risk",
            "loss",
            "circuit breaker",
            "allocation",
            "buy",
            "sell",
            "order",
            "deploy",
            "stop loss",
            "atr",
            "market exit",
        ),
        (
            "Open **Risk & execution settings** on Home, beside the portfolio "
            "review and order-planning controls. They include a user-reported "
            "realized daily-loss circuit breaker, IST trading-hours check, "
            "eight-position cap, confidence threshold, and allocation limit. "
            "Order buttons require an explicit click and recheck safeguards, "
            "but these app controls cannot restrict orders placed directly "
            "through a broker. Review every order before submission."
        ),
    ),
    (
        "Email and WhatsApp",
        (
            "email",
            "whatsapp",
            "twilio",
            "resend",
            "notification",
            "alert",
            "alerts",
            "share",
            "report",
        ),
        (
            "Use the **Email** and **WhatsApp** controls in the header to "
            "configure destinations. WhatsApp uses Twilio; email supports "
            "SMTP or Resend. Portfolio reports are shared manually from Home; "
            "risk alerts are sent only when a channel is configured."
        ),
    ),
    (
        "Custom brokers and Turso",
        (
            "custom broker",
            "dynamic broker",
            "turso",
            "read only",
            "endpoint",
            "registry",
        ),
        (
            "Custom broker registration is optional and requires Turso plus "
            "an operator-configured HTTPS hostname allow-list. Definitions "
            "are tenant-scoped when OIDC is enabled. These custom adapters are "
            "read-only: they cannot place orders or provide built-in mutual "
            "fund or ATR feeds."
        ),
    ),
    (
        "Privacy and shared deployment",
        (
            "privacy",
            "secure",
            "security",
            "oidc",
            "multi user",
            "multi-user",
            "shared deployment",
            "save",
            "store",
            "credential",
            "credentials",
            "key",
            "keys",
            "agent",
            "session",
        ),
        (
            "Broker tokens, provider keys, notification settings, chat history, "
            "and portfolio data are kept in the current Streamlit session, "
            "not durable user storage. The **Ask about this Agent** guide is "
            "local and needs no AI key; only optional AI portfolio analysis "
            "needs a provider key. OIDC is optional for local use; enable it "
            "and configure a trusted identity provider before exposing a "
            "shared deployment."
        ),
    ),
)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def answer_app_help_question(
    question: str,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Return all relevant documented app guidance without external services."""
    cleaned = question.strip()
    if not cleaned or len(cleaned) > 2000:
        raise ValueError("Ask a question containing 1–2,000 characters.")

    query = _normalize(cleaned)
    words = set(query.split())
    follow_up = (
        len(words) <= 4
        or query.startswith(
            ("and ", "also ", "what about", "how about", "tell me more", "why ")
        )
    )
    if follow_up and history:
        previous_questions = [
            item.get("content", "")
            for item in history[-6:]
            if item.get("role") == "user" and isinstance(item.get("content"), str)
        ]
        if previous_questions:
            query = _normalize(" ".join(previous_questions[-2:]) + " " + query)
            words = set(query.split())

    matches: list[str] = []
    for title, keywords, answer in HELP_TOPICS:
        if any(
            f" {_normalize(keyword)} " in f" {query} "
            for keyword in keywords
        ):
            matches.append(f"### {title}\n\n{answer}")

    if matches:
        return "\n\n".join(matches)
    return (
        "I can help explain broker connections, header workspaces, market "
        "research and scanning, mutual funds, F&O limitations, AI analysis, "
        "risk/order controls, email and WhatsApp, custom brokers, and session "
        "privacy. Ask about one or more of those app features. This guide "
        "answers locally and does not need an AI API key."
    )
