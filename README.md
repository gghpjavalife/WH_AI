# Wealth Home AI

Streamlit portfolio dashboard with broker adapters, portfolio analysis, and
deterministic recommendation rules.

## Run locally

Install the project dependencies, configure broker and Gemini credentials in
environment variables (or the ignored local `.env` file), then start Streamlit:

```powershell
uv sync
streamlit run app.py
```

The root `app.py` remains the supported Streamlit entry point. Application
implementation modules live only in the `wealth_home_ai` package.

## Source layout

```text
src/wealth_home_ai/
  dashboard.py            Streamlit UI and session orchestration
  broker_factory.py       Broker interface, factory, and broker adapters
  jev_rules.py            Deterministic recommendation rule engine
  oauth_state_store.py    Expiring, one-time OAuth state persistence
  upstox_helper.py        Gemini analysis, ATR calculations, and alerts
```

OAuth state is stored in the project-root `.wealth_home_oauth.sqlite3` file by
default to preserve existing local sign-ins. Set `WEALTH_HOME_OAUTH_DB` to use a
different writable database path in deployments.
