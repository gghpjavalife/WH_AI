# GGHP

**Governed Growth & Hedged Portfolios** — a governed multi-broker portfolio companion with
AI-assisted insights and deterministic JEV risk guardrails.

## Run locally

Install the project dependencies, configure credentials and optional runtime
settings in the project-root `.env` file, then start Streamlit. Streamlit runs
use browser IndexedDB for durable application data; no database key or server
database setup is needed for this runtime.

```powershell
uv sync
streamlit run app.py
```

## Keep a free deployment responsive

Streamlit already exposes a lightweight health endpoint at
`https://<your-app-host>/_stcore/health`; it returns HTTP 200 when the app
server is responsive. No extra endpoint code or dependency is required.

The production config disables Streamlit's development file watcher to avoid
unneeded file scanning. Market-data requests are already cached by the app.

To monitor the app with UptimeRobot:

1. Create a free account and choose **Add New Monitor** → **HTTP(s)**.
2. Enter a monitor name and the health URL above.
3. Set the monitoring interval to **5 minutes**, then save.
4. Optionally add an email alert contact to receive downtime notifications.

An uptime monitor can wake a sleeping app and may see a timeout while it starts.
It cannot override a hosting provider's sleep, restart, or usage policies, so
continuous availability is not guaranteed on a free plan.

The SQLite provider remains available for non-Streamlit native runtimes. Before
using that provider on Windows, macOS, or Linux, provision its SQLCipher key
through the supported OS credential manager:

```powershell
$env:PYTHONPATH = "src"
python -m security.database_key provision
```

This setup is not used by Streamlit browser storage. Never configure a shared
server database for user data.

## Create the credentials you need

Create credentials only on the official provider sites below. Beside each
credential section in **User settings**, the app links to the provider's setup
guide. Settings and preferences are stored in the current device's browser
IndexedDB when running in Streamlit.
Email, phone, broker credentials, and AI keys remain session-only unless the
user explicitly consents to encrypted local storage in the settings dialog.
Notification delivery credentials are configured separately by the app operator.

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
| OpenRouter API key | [OpenRouter keys](https://openrouter.ai/settings/keys) |
| xAI API key | [xAI console](https://console.x.ai/) |
| Cerebras API key | [Cerebras Cloud](https://cloud.cerebras.ai/) |
| Operator WhatsApp delivery (Twilio) | [Twilio WhatsApp Sandbox setup](https://www.twilio.com/docs/whatsapp/sandbox) |
| Operator email delivery (Resend) | [Resend API keys](https://resend.com/api-keys) |

For Upstox, Zerodha, and Dhan, register the exact callback URL displayed by
the app in the broker's API-app settings. A custom OpenAI-compatible provider
does not have a shared key-generation page; use the endpoint provider's own
console and enter its API base URL and model ID in **User settings**.
Users enter only their email address and phone number in **User settings →
Profile**. Email and WhatsApp delivery credentials are configured by the app
operator as server secrets; phone notifications use WhatsApp, not SMS. Twilio
sandbox recipients must first join the sandbox.

Runtime settings are centralized in `core.config`. The `.env` file contains
operator-managed integration credentials and setting overrides; safe defaults
are used for omitted options. Deployment secrets should be supplied by the platform secret
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
authenticated email or subject to scope saved user settings. Without the OIDC
gate, settings are scoped only to a browser session and are not suitable for
shared-user isolation.

Optional server-side integration settings are:

| Setting | Purpose |
| --- | --- |
| `RESEND_API_KEY`, `RESEND_SENDER` | Default Resend sender for email delivery; sender/domain must be verified with Resend. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_SENDER` | Optional server-side WhatsApp delivery configuration; each recipient comes from the signed-in user's profile. |
| `WEALTH_HOME_REQUIRE_LOGIN` | Set to `true` to require OIDC sign-in. |

These settings can be environment variables on Render or Hugging Face Spaces,
or top-level values in Streamlit secrets. Broker credentials are configured
only for the four supported built-in integrations: Upstox, Zerodha, Angel One,
and Dhan.

Streamlit browser sessions store user settings and short-lived broker OAuth
callback state in IndexedDB. Confidential settings and OAuth callback context
are encrypted locally with browser Web Crypto. The browser bridge executes
IndexedDB operations in the browser; it does not persist those records to the
Streamlit server filesystem or to a cloud database.

Settings use a device-local repository abstraction. Streamlit selects the
IndexedDB provider; non-Streamlit runtimes select the SQLCipher provider, which
retains the existing `user_settings` and `oauth_states` table structures.
Confidential browser records use AES-GCM through Web Crypto, and native
confidential records retain the existing Fernet layer over SQLCipher.
Encryption does not protect data from someone who can access the browser
profile or native device.
Without OIDC, user settings belong to the
single local app user. OIDC is optional and is not required to use local storage;
when enabled, the authenticated identity scopes settings in the browser store.

The interface's deterministic rules constrain recommendations and app-issued
BUY orders, but they are not broker-side controls and cannot stop orders placed
outside this app. Realized daily loss is user-reported because the integrated
brokers do not provide a verified common realized-P&L feed. Verify all trades
independently before submitting them.

“Free tier” availability is not a zero-cost or uptime guarantee. Hosting,
broker API access, market data, AI model availability/rate limits, Twilio,
and Resend are subject to each provider's changing eligibility,
quotas, terms, and possible charges.

Users choose an AI provider and model in **User settings** using searchable
selectors; they can also type a provider name (for an OpenAI-compatible API) or
a provider-specific model ID. Users can keep multiple configurations and edit
each one. Each configuration must pass a small connection test before it can
be saved individually. The test sends no portfolio data, but may incur the
provider's normal API charge. Keys remain in the
current session by default; users may explicitly approve encrypted local
persistence. Supported providers include Gemini, OpenAI, Anthropic, Groq,
Together AI, Mistral, DeepSeek, OpenRouter, xAI, Cerebras, and custom
OpenAI-compatible endpoints.
The **Ask about this Agent** header button is a local guide for application
features and workflows, including broker connections, workspaces, research,
analysis, risk controls, and notifications. It uses a deterministic in-app
help guide and makes no external AI request, so it does not require a provider
key. The model selector for portfolio analysis is populated from
provider-specific defaults in
`core.config`. Override model lists with the `LLM_PROVIDER_MODELS`
environment setting as a JSON object, for example:

```text
LLM_PROVIDER_MODELS={"OpenAI":["gpt-4o-mini","gpt-4.1-mini"],"Groq":["llama-3.3-70b-versatile"]}
```

Custom OpenAI-compatible endpoints accept a freeform model ID and
require an API base URL, which can be supplied as `LLM_CUSTOM_BASE_URL` or in
User settings. They do not have a predefined or shared model list;
enter the model ID provided by your endpoint. Provider
model availability, access, quotas, and pricing are controlled by each provider
and can change.

The root `app.py` remains the supported Streamlit entry point. Application
code uses a `src/` layout with separate application, broker, core, feature,
security, service, and UI packages.

## Source layout

```text
src/
  application/            Dashboard composition and local help guide
  brokers/                Broker interfaces and provider adapters
  core/                   Runtime settings, constants, and logging
  features/                Analysis and portfolio workspaces by domain
  security/                OAuth state and credential encryption
  services/                AI, cloud, notifications, and application services
  storage/                 StorageProvider implementations and repositories
  ui/                     Shared header, helpers, and user-settings dialog
```

The browser store uses versioned IndexedDB object stores and preserves the
current SQLite record fields for settings and OAuth state. A persistent
anonymous device ID is generated and stored on first launch. Repositories
depend only on the provider contract so synchronization can be introduced
without changing application services.

### Existing native SQLite database migration

This migration applies only to existing databases used by the native SQLite
provider; Streamlit browser storage does not read or write this file. Stop all
native app processes, provision the SQLCipher key above, and make any required
protected backup before converting:

```powershell
$env:PYTHONPATH = "src"
python -m security.database_migration --confirm-plaintext-removal
```

The command builds and validates an encrypted replacement, then atomically
replaces the plaintext file. It deliberately retains no plaintext backup.
Plaintext backups and remnants from storage media may remain recoverable, so
protect any backup and retire it securely. Migration refuses to run when
SQLite WAL/journal sidecars exist; close the app cleanly and retry.

On supported desktop platforms the native provider uses Windows Credential
Manager, macOS Keychain, or Linux Secret Service/KWallet. The Python SQLCipher
driver is `sqlcipher3`; it is not interchangeable with standard-library
`sqlite3`.

Configure one of the four supported brokers in **User settings → Brokers**
or select it directly in Home's broker selector. Upstox, Zerodha, Angel One,
and Dhan are always available; no separate enable step is required. Credentials are
entered in the corresponding broker settings and are not written to `.env` or
read from server environment configuration. Upstox and Zerodha API key/secret pairs identify
the developer app, while each user separately authorizes their own broker account. OAuth state
is random, short-lived, one-time, and stored only as a digest. To complete
Upstox or Zerodha sign-in automatically after the broker redirects to a fresh
Streamlit session, the state store also holds the matching app credentials in
encrypted form until the state expires or is consumed. Locally, the encryption
for OAuth callback contexts uses browser Web Crypto in Streamlit and the
existing Fernet-plus-SQLCipher protection in native SQLite runtimes.

Each OAuth connect control stays disabled until its required credentials are
present. Upstox and Zerodha open the broker authorization directly and finish
the token exchange automatically on callback. Dhan requires a consent-creation
API call before it can provide the broker login URL, so its connect action then
shows an authorization link; complete Dhan authorization in the same browser
tab because its callback does not include a state parameter. Users switching
Upstox accounts should sign out of the hosted login (or use a private browser
window) before authorizing another account. Callback URLs must match the
registered URL exactly. Review or change the shared callback address under
**User settings → Brokers → OAuth callback URL**; it is no longer shown in the
broker connection panel.

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

Profile contact details, broker settings, AI providers, and risk preferences
are managed in **User settings**; there is no user-facing Alerts settings tab.
Email and phone are the per-user notification destinations. Delivery credentials
remain operator-managed server secrets. Public preferences are saved by default.
A clear, optional, explicit encrypted-storage consent applies to contact details
and user-provided broker/AI credentials. With consent unchecked, confidential
values are removed from saved records and need to be entered again in a later
session. The consent notice explains the risks of storing encrypted values on the
current device.
AI settings support multiple tested provider configurations, suggested or
custom model IDs, and OpenAI-compatible custom endpoints.

Each active browser session has its own Streamlit session state; this is not a
durable user directory or account vault. Shared developer-app credentials let
people authorize their own supported broker accounts, but do not turn
account-bound credentials (notably Dhan's) into multi-tenant credentials. For a
shared deployment, enable the OIDC gate described above and use the deployment
limitations documented in that section.

Home is the default page. Before connecting, it provides the broker sign-in
action; configure broker credentials in **User settings** first. After
connecting, Home shows the selected account's cash, portfolio summary,
allocation, and portfolio review.

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
