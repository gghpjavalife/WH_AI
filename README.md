# GGHP

**Governed Growth & Hedged Portfolios** — a governed multi-broker portfolio companion with
AI-assisted insights and deterministic JEV risk guardrails.

## Run locally

Install the project dependencies, configure credentials and optional runtime
settings in the single project-root `.env` file, then start Streamlit:

```powershell
uv sync
streamlit run app.py
```

## Create the credentials you need

Create credentials only on the official provider sites below. Beside each
credential field, the app explains why it is needed, how to obtain the right
value, and links to the provider's setup guide. Interactive credentials stay
in the current browser session.

| Purpose | Official setup page |
| --- | --- |
| Upstox API app | [Upstox developer apps](https://account.upstox.com/developer/apps) |
| Angel One API key | [Angel One SmartAPI](https://smartapi.angelone.in/) |
| Zerodha API key and secret | [Kite Connect](https://kite.trade/) |
| Dhan API credentials | [DhanHQ API documentation](https://dhanhq.co/docs/v2/) |
| Gemini API key | [Google AI Studio](https://aistudio.google.com/app/apikey) |
| OpenAI API key | [OpenAI platform](https://platform.openai.com/api-keys) |
| Anthropic API key | [Anthropic console](https://console.anthropic.com/settings/keys) |
| Groq API key | [Groq console](https://console.groq.com/keys) |
| Together AI API key | [Together AI settings](https://api.together.xyz/settings/api-keys) |
| Mistral API key | [Mistral console](https://console.mistral.ai/api-keys/) |
| DeepSeek API key | [DeepSeek platform](https://platform.deepseek.com/api_keys) |
| Twilio WhatsApp sender and credentials | [Twilio WhatsApp Sandbox setup](https://www.twilio.com/docs/whatsapp/sandbox) |
| Gmail SMTP app password | [Google app passwords](https://support.google.com/accounts/answer/185833) |

For Upstox, Zerodha, and Dhan, register the exact callback URL displayed by
the app in the broker's API-app settings. A custom OpenAI-compatible provider
does not have a shared key-generation page; use the endpoint provider's own
console and enter its API base URL and model ID in the analysis controls.
WhatsApp and email alerts can be configured from the app header. WhatsApp uses
Twilio's WhatsApp API; join the Twilio sandbox with the recipient before sending
test messages. Email requires an SMTP server supporting TLS on port 465 (SSL) or
587 (STARTTLS). Notification settings are held only in the active browser
session.

Runtime settings are centralized in `wealth_home_ai.settings`. The `.env` file
contains both credentials and setting overrides; safe defaults are used for
omitted options. Deployment secrets should be supplied by the platform secret
manager or Streamlit's ignored `.streamlit/secrets.toml`, rather than committed
files. Environment variables take precedence over Streamlit secrets.

## Shared deployment and optional integrations

Local use does not require sign-in. Before exposing a shared deployment, enable
the OIDC gate with `WEALTH_HOME_REQUIRE_LOGIN=true` and configure Streamlit's
`[auth]` settings in `.streamlit/secrets.toml` (or the hosting provider's
equivalent Streamlit secrets configuration):

```toml
[auth]
redirect_uri = "https://your-app.example.com/oauth2callback"
cookie_secret = "generate-a-long-random-secret"
client_id = "your-oidc-client-id"
client_secret = "your-oidc-client-secret"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

Register the exact redirect URI with the selected OIDC provider. For local use,
replace it with `http://localhost:8501/oauth2callback`. The app uses the
authenticated email or subject to scope custom broker definitions. Without the
OIDC gate, custom definitions are scoped only to a browser session and are not
suitable for shared-user isolation.

Optional server-side integration settings are:

| Setting | Purpose |
| --- | --- |
| `TURSO_PRIMARY_DB_URL`, `TURSO_AUTH_TOKEN` | Enable the tenant-scoped custom broker registry in Turso. |
| `DYNAMIC_BROKER_ALLOWED_HOSTS` | Comma-separated, operator-controlled HTTPS host allow-list for custom broker APIs. |
| `RESEND_API_KEY`, `RESEND_SENDER` | Default Resend sender for email delivery; sender/domain must be verified with Resend. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_SENDER`, `TWILIO_WHATSAPP_RECIPIENT` | Optional server defaults for WhatsApp notifications. |
| `WEALTH_HOME_REQUIRE_LOGIN` | Set to `true` to require OIDC sign-in. |

These settings can be environment variables on Render or Hugging Face Spaces,
or top-level values in Streamlit secrets. The app creates its
`dynamic_broker_registry` table in Turso on first use. Each definition requires
an allow-listed HTTPS API host and `balance`, `positions`, and `holdings`
endpoint paths. Responses must use the documented balance and equity-row
fields in `GenericDynamicAdapter`; optional `profile` and `live_prices` paths
may also be configured. Custom adapters reject redirects and are read-only:
they cannot place orders, access mutual-fund or ATR feeds, or bypass the
operator's server-side hostname allow-list. Broker bearer tokens are not stored
in the registry.

OAuth recovery state is stored in SQLite, not Turso. Keep
`WEALTH_HOME_OAUTH_DB` on persistent storage and configure the same
`OAUTH_CREDENTIAL_ENCRYPTION_KEY` across instances that share that database.
SQLite needs a storage topology that supports its locking and journal semantics;
do not point multiple independent instances at unrelated local disks and expect
OAuth callbacks to resume across them. If the host cannot provide compatible
shared storage, use a single app instance or adopt a shared OAuth-state store
before scaling horizontally.

Broker tokens, portfolio data, notification preferences, and AI chat remain in
the active Streamlit session; they are not durable user records. OIDC identifies
users and scopes the optional broker registry, but does not persist broker
credentials or portfolios. Use a single-instance deployment or a deliberately
designed encrypted persistence layer if durable sessions are required.

The interface's deterministic rules constrain recommendations and app-issued
BUY orders, but they are not broker-side controls and cannot stop orders placed
outside this app. Realized daily loss is user-reported because the integrated
brokers do not provide a verified common realized-P&L feed. Verify all trades
independently before submitting them.

“Free tier” availability is not a zero-cost or uptime guarantee. Hosting,
broker API access, market data, AI model availability/rate limits, Turso,
Twilio, and Resend are subject to each provider's changing eligibility,
quotas, terms, and possible charges.

Connected users choose an AI provider, enter its API key, and select a model
beside the main portfolio analysis button. Keys are kept only in the current
browser session and are never loaded from server environment variables or
written to `.env`. Supported providers include Gemini, OpenAI, Anthropic,
Groq, Together AI, Mistral, DeepSeek, and custom OpenAI-compatible endpoints.
The **Ask about this Agent** header button is a local guide for application
features and workflows, including broker connections, workspaces, research,
analysis, risk controls, and notifications. It uses a deterministic in-app
help guide and makes no external AI request, so it does not require a provider
key. The model selector for portfolio analysis is populated from
provider-specific defaults in
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
  market_research.py      Public daily NSE history and transparent technical signals
  jev_rules.py            Deterministic recommendation rule engine
  oauth_state_store.py    Expiring, one-time OAuth state persistence
  upstox_helper.py        Multi-provider AI analysis, ATR calculations, and alerts
  features/
    analysis.py           Full-portfolio and scoped AI analysis views
    home.py               Investment summary and interactive allocation chart
    market.py             Broker-independent stock search and watchlist scanner
    portfolio.py          Portfolio aggregates
    operations/           Equity, trading, derivatives, and MF views
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

Generate a Fernet-compatible deployment key with the installed dependency:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the generated value as `OAUTH_CREDENTIAL_ENCRYPTION_KEY` in the deployment
secret manager, not in source control. Local development creates and reuses a
protected `.key` file automatically. See
[Streamlit secrets management](https://docs.streamlit.io/develop/concepts/connections/secrets-management)
for deployment secret configuration guidance.

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
current eligibility and static-IP requirements. Upstox and Zerodha provide API
endpoints used by this app to fetch mutual-fund holdings. Angel One SmartAPI and
Dhan do not expose a mutual-fund holdings feed here, so import those holdings
from a current statement CSV when using either broker.

WhatsApp and email notifications are configured from the header and use
session-only credentials. WhatsApp sandbox recipients must first join the
Twilio sandbox; email credentials must be accepted by the selected SMTP server.

Broker API credentials, AI provider keys, and notification credentials are kept
in the active app session only; the app does not save them in browser storage or
server configuration. Re-enter them after the session ends.

Each active browser session has its own Streamlit session state; this is not a
durable user directory or account vault. Shared developer-app credentials let
people authorize their own supported broker accounts, but do not turn
account-bound credentials (notably Dhan's) into multi-tenant credentials. For a
shared deployment, enable the OIDC gate described above and use the deployment
limitations documented in that section.

Home is the default page. Before connecting, it provides the broker sign-in
form; after connecting, it shows the selected account's cash, portfolio
summary, allocation, and portfolio review. WhatsApp and email configuration
remain available from the header whether or not a broker is connected.

The compact header navigation has Home, Equities, Trades, F&O, and Mutual
Funds workspaces. Market research and scanning live inside each relevant
workspace rather than in separate top-level tabs. Equity, trades, and mutual funds each have dedicated views.
Mutual-fund research screens the saved holdings by return and NAV date. Equity
and trades search/scanning use cached (15-minute) public NSE daily history from
[Yahoo Finance](https://finance.yahoo.com/), which may be delayed or unavailable.
Equity filters focus on price, RSI, average liquidity, and medium-term trend;
trade filters additionally screen relative volume and ATR-normalized movement.
Both display daily change, SMA/EMA trend, RSI, MACD and histogram, ATR and
price-normalized ATR, ADX with directional indexes, CCI, MFI, 20-day OBV change,
Bollinger bands and width, stochastic %K/%D, annualized 20-day volatility,
one- and three-month returns, relative volume, and the 52-week range/position.
F&O research and scanning remain clearly unavailable until verified contract,
option-chain, quote, and margin feeds are integrated. Missing market data is
never substituted with unrelated asset-class indicators. Recommendations are
explainable historical signals, not personalized investment advice or order
instructions; scanning never submits orders.

Broker sync, quote, notification, OAuth, and AI-provider failures are logged to
the Streamlit server output with the affected operation and safe HTTP/request
metadata when available. The workspace's **Refresh diagnostics** expander shows
the same sanitized feed diagnostics. Authorization headers, tokens, secrets,
response bodies, and portfolio records are not written to these diagnostic logs.

Home provides an allocation chart with hover details and a full-portfolio AI
review. Each workspace also has a scoped review action; unsupported/unpriced
areas are analysis-only and are not treated as broker positions or orderable
instruments.
