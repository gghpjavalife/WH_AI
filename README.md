# Wealth Home AI

Streamlit portfolio dashboard with broker adapters, portfolio analysis, and
deterministic recommendation rules.

## Run locally

Install the project dependencies, configure credentials and optional runtime
settings in the single project-root `.env` file, then start Streamlit:

```powershell
uv sync
streamlit run app.py
```

Runtime settings are centralized in `wealth_home_ai.settings`. The `.env` file
contains both credentials and setting overrides; safe defaults are used for
omitted options. Deployment secrets should be supplied by the platform secret
manager rather than committed files.

Connected users choose an AI provider, enter its API key, and select a model
beside the main portfolio analysis button. Keys are kept only in the current
browser session and are never loaded from server environment variables or
written to `.env`. Supported providers include Gemini, OpenAI, Anthropic,
OpenRouter, Groq, Together AI, Mistral, DeepSeek, and custom OpenAI-compatible
endpoints. The model selector is populated from provider-specific defaults in
`wealth_home_ai.settings`. Override model lists with the `LLM_PROVIDER_MODELS`
environment setting as a JSON object, for example:

```text
LLM_PROVIDER_MODELS={"OpenAI":["gpt-4o-mini","gpt-4.1-mini"],"Groq":["llama-3.3-70b-versatile"]}
```

Custom OpenAI-compatible endpoints accept a freeform model ID and
require an API base URL, which can be supplied as `LLM_CUSTOM_BASE_URL` or in
the analysis controls. They do not have a predefined or shared model list;
enter the model ID provided by your endpoint. Provider
model availability, access, quotas, and pricing are controlled by each provider
and can change.

The root `app.py` remains the supported Streamlit entry point. Application
implementation modules live only in the `wealth_home_ai` package.

## Source layout

```text
src/wealth_home_ai/
  dashboard.py            Streamlit UI and session orchestration
  broker_factory.py       Broker interface and Upstox, Angel One, Zerodha, and Dhan adapters
  jev_rules.py            Deterministic recommendation rule engine
  oauth_state_store.py    Expiring, one-time OAuth state persistence
  upstox_helper.py        Multi-provider AI analysis, ATR calculations, and alerts
  features/
    analysis.py           Full-portfolio and scoped AI analysis views
    home.py               Investment summary and interactive allocation chart
    portfolio.py          Portfolio aggregates and debt-entry validation
    operations/           Equity, debt, trading, derivatives, and MF views
```

OAuth state is stored in the project-root `.wealth_home_oauth.sqlite3` file by
default to preserve existing local sign-ins. Set `WEALTH_HOME_OAUTH_DB` to use a
different writable database path in deployments.

Each broker's login form requests credentials after that broker is selected.
Inputs are held in the active Streamlit browser session while needed for that
sign-in and are not written to `.env` or read from server environment
configuration. Upstox and Zerodha API key/secret pairs identify the developer
app, while each user separately authorizes their own broker account. OAuth state
is random, short-lived, one-time, and stored only as a digest. To complete
Upstox or Zerodha sign-in automatically after the broker redirects to a fresh
Streamlit session, the state store also holds the matching app credentials in
encrypted form until the state expires or is consumed. Locally, the encryption
key is created beside the OAuth database in a `.key` file with restricted
permissions where supported; deployments with multiple app instances should
configure the same stable `OAUTH_CREDENTIAL_ENCRYPTION_KEY` secret on every
instance and use shared OAuth storage. Protect and back up that key as a
deployment secret.

Each OAuth connect control stays disabled until its required credentials are
present. Upstox and Zerodha open the broker authorization directly and finish
the token exchange automatically on callback. Dhan requires a consent-creation
API call before it can provide the broker login URL, so its connect action then
shows an authorization link; complete Dhan authorization in the same browser
tab because its callback does not include a state parameter. Users switching
Upstox accounts should sign out of the hosted login (or use a private browser
window) before authorizing another account. Callback URLs must match the
registered URL exactly.

Angel One requires account-specific API key, Client ID, password, and TOTP
secret. Dhan requires a matching Client ID, API key, and API secret; one
credential set cannot grant access to multiple unrelated Dhan accounts. Zerodha
requires a Kite Connect developer app with this app's callback URL registered;
its access token expires daily. Dhan's callback URL must be registered in Dhan
Web and its access token is time-limited. Dhan order placement is subject to
current eligibility and static-IP requirements. Zerodha, Dhan, and Angel One do
not provide the mutual-fund holdings endpoint used by this app, so mutual funds
can be imported from a statement CSV instead.

Telegram notifications are configured in the sidebar using a bot token and
private chat ID. These are session-only inputs, not server defaults. The bot
must be started by the recipient before it can send messages.

This provides per-browser-session isolation, not an application-level user
directory or durable user accounts. Shared app credentials enable multiple
people to authorize their own supported broker accounts; they do not turn
account-bound credentials (notably Dhan's) into multi-tenant credentials. For a
public multi-user deployment, put the app behind an identity provider and
persist user/account mappings and tokens securely in a database or secrets
manager.
keyed by the authenticated identity; do not use a shared portfolio file.

After broker authentication, the workspace shows equity, debt, trading, options,
futures, and mutual-fund areas. Broker-connected equity, trading, and mutual-fund
data are refreshed from the selected adapter. Debt entries are user-managed and
session-scoped. Options and futures are clearly marked as not separately classified until
broker adapters provide verified contract, quote, margin, and order support.
Any derivative positions returned in a generic positions feed may currently
appear under Trading.

Home provides an allocation chart with hover details and a full-portfolio AI
review. Each workspace also has a scoped review action; unsupported/unpriced
areas are analysis-only and are not treated as broker positions or orderable
instruments.
