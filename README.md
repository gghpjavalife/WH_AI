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
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Use the project interpreter rather than a global `streamlit` command: the
global installation may not contain the project's SQLCipher dependency.

## Local stock context engine

`services.stock_context.fetch_and_build_market_context` builds a Pydantic v2
context without calling an LLM. It uses Yahoo's quote-summary modules, annual
financial statements, full returned option chain, and daily history. Set
`UPSTOX_ANALYTICS_TOKEN` in the project-root `.env` or the environment to use
an Upstox instrument key; the token is never included in the output. The v2
REST full quote has five depth levels, not 30. Intraday contexts instead
require an Upstox V3 `full_d30` stream, which requires the appropriate Upstox
Plus entitlement. Access-token validity and renewal follow Upstox's current
rules; analytics tokens are documented as valid for one year, not indefinitely.

After a broker connects, stock holdings display their local context and
gatekeeper results alongside the existing rules-based research, grouped by
data category. Upstox equity positions use their broker-returned instrument
keys; unclassified positions and derivatives are not treated as stocks.
Other brokers' equity holdings use the existing Yahoo ticker convention and
do not have Upstox-specific data unless an instrument key is available.
Contexts are held in the user's session for at most 15 minutes and refreshed
on the next view or the holdings refresh button. Holdings research runs in a
bounded background pool; the table remains usable with a "Not rated" signal
until its research completes. Provider failures are shown
without treating missing inputs as safe. This local assessment does not call
an LLM and does not gate the existing separate portfolio AI workflow.
Market scans and their context enrichment run in the background with bounded
parallel requests and separate progress indicators. You can switch workspaces
while they run. Large universes can still take time and may hit provider rate
limits; context snapshots retain their original timestamps and may be stale.

The in-memory service API `services.universe_scan.async_execute_universe_scan`
accepts a list of objects with `symbol`, optional `instrument_key`, and optional
`horizon`, plus an Upstox analytics token. It returns one JSON string per input
in the original order, including failures and blocked contexts. Each record
contains `status`, `early_exit`, `context`, `local_gatekeeper`, and `error`.
No LLM is invoked. Intraday scans require a live D30 stream and are not accepted
by this snapshot runner.

The stock tables start unfiltered: use their column controls for primary
filters (opened on demand) and use **Add filters** to search for and add
indicator or context columns. Added columns can be sorted and filtered like
the standard columns. Active filters exclude missing values; clearing them
restores all rows. Copy uses the current filtered page; CSV
includes all filtered results. Filter controls are kept in memory and reset
when a Streamlit rerun remounts the table. Additional fields marked "fraction"
use 0.1 for 10%; fields marked "%" use percentage points.
Select individual stocks and use **Add to cart** to stage them for review in Cart.
The header checkbox selects or clears all filtered stocks, including other pages;
stocks outside the current filter are unchanged.
Selections remain while filtering and paging, and reset when table data changes
or the component remounts. The cart is kept in the current Streamlit session;
adding stocks does not place orders or create reports.
Cart retains signed quantities for the current session: positive means buy and
negative means sell. Quantities use whole shares; arrows beside the read-only
sell percentage change the quantity by one share. The percentage is unavailable
when the holding is unknown. When a holding is known, sell quantities cannot
exceed its whole shares; an unknown holding cannot be checked against this
limit. Existing fractional or excessive quantities must be adjusted before checkout.
Signals are snapshots refreshed when a stock is added again. Per-stock and
all-stock checkout open a review only; they do not trigger an external call or
submit broker orders.
The cart's eye icon opens the saved research panels from the stock table;
re-add an older cart entry if its research snapshot is unavailable.
Cart buy costs and sell proceeds are estimates calculated from the saved table
LTP and quantity in the stock's quote currency. Prices may be delayed; estimates
exclude fees and taxes and are not guaranteed execution prices.
Cart totals are displayed only when every active stock has a usable price and
a known matching quote currency; no currency conversion is assumed.
When both sources provide the same metric, the newer context value is displayed;
the existing research value is used only when context data is unavailable.
Older assessments are not counted against a differing context value, although
the separate research signal still uses its original inputs.
Stock detail panels are stacked and can be expanded individually. Their ratings
count positive directional assessments among assessed checks; unassessed data is
not interpreted as positive or negative. Related details are grouped in priority
order, starting with company and ownership; metrics wrap as space permits.
The 22 local daily technical fields appear in **Trend & momentum** (averages,
ADX, RSI, MACD and money flow), **Risk & intraday** (Bollinger bands, ATR
percentile, choppiness, squeeze and 30-day volatility), and **Performance &
volume** (current and average volume and volume-price trend). The availability
count refers to the context snapshot; a research value may be shown as a
fallback. Open **How the research signal was scored** inside Trend & momentum
for the separate research checks and their original inputs.
When context supplies fundamentals but the research signal lacks them, the data
availability note makes that signal limitation explicit. News ratings use only the explicit
keyword-based headline signals and are not investment recommendations. Fetching a
large portfolio or scan can take time and may hit provider rate limits.

Run the local phase from PowerShell without sending data to an LLM:

```powershell
$env:PYTHONPATH = "src"
python -m services.stock_context RELIANCE.NS --instrument-key "NSE_EQ|INE002A01018"
```

The JSON output contains the full context and local gatekeeper result.
`--sector-symbol` accepts a Yahoo sector benchmark; `--supplemental` accepts
a JSON file containing independently verified sections such as
`{"shareholders":{"promoter_pledged_shares_pct":0.12}}`. The pledge value must
be the fraction of promoter-held shares that are pledged, not promoter
ownership or the fraction of all shares. Only use an actual sourced value.
The gate also requires `fundamentals.altman_model_applicable` to be set to
`true` in that file after independently confirming that the public-manufacturing
Altman model is appropriate for the issuer; otherwise it blocks analysis.
If the gate passes, call `run_triggered_analysis` with an explicit
`triggered=True` and a user-selected LLM callable accepting the complete JSON
payload. The call revalidates the gate and includes warnings and penalties;
it never happens during ingestion. Rebuild intraday contexts immediately
before the user triggers analysis: streaming trade timestamps older than
30 seconds are rejected.

`UpstoxMicrostructureStream` accepts cumulative historical volume norms keyed
by exchange-local `HH:MM`. `fetch_upstox_volume_baseline` builds these from
past one-minute Upstox candles, excludes incomplete sessions and returns the
raw response and warnings; pass those into `baseline_source` and
`baseline_warnings` on the stream to retain provenance. Relative volume
compares cumulative volume through the last fully completed minute with the
same historical interval, and remains unavailable if any live minute is
missing. Register error/close callbacks with
`create_streamer`, connect it before the market opens, and disconnect it when
finished. It requires fresh ticks and all completed one-minute opening bars;
it does not guess relative volume or reconstruct missing bars. The caller must
associate the Yahoo ticker with the correct Upstox instrument key. For NSE/BSE
equities the ISIN is extracted from the supplied instrument-master key; it is
not independently matched against the Yahoo ticker.

The v2 full quote supplies optional circuit bands. Lower-circuit distance is
`(price - lower_limit) / lower_limit * 100`, in percentage points; below 1.5
the local gate blocks analysis. Streamed contexts use the live trade price and
only combine it with quote bands from the same India-local calendar date.
Non-streamed contexts use the Upstox snapshot price (which may be stale), not
the Yahoo price. Missing, invalid or mismatched bands leave proximity unknown
and produce a warning, not a safety clearance or a new mandatory intraday
field. Verify quote freshness and exchange bands independently before use.

For Indian equities, the Upstox fundamentals corporate-actions feed is retained
in `source_data`. Events with an ex/effective date from today through 30 days
ahead, inclusive, are considered upcoming in the India timezone. Split or bonus
events take priority over dividends and suppress only the technical-divergence
rule; they do not bypass other risk blocks. `NONE` means no supported event in
the returned feed in that window, not a guarantee of feed completeness.
Unavailable or malformed relevant dates yield an unknown (`null`) status.
Independently sourced events can be provided in
`supplemental.news.upcoming_corporate_action_event`; the caller is responsible
for their accuracy and timing.

Upstox's quarterly shareholding data is retained separately; promoter
ownership is not a measure of pledging. Its paginated news endpoint covers
only the latest seven days. The public feeds do not reliably provide promoter pledges, dealer gamma
positioning, historical IV percentile, commercial futures positions, block
trade direction, complete holder counts, or headline sentiment. Supply
independently verified data through the `supplemental` sections rather than
assuming unavailable values are zero. Missing critical financial risk inputs
block the local gate. Missing price, SMA 200 or RSI skips the technical
divergence rule with a warning instead; a false divergence flag in that case
means "not assessed", not a clean result. The retained Yahoo option-chain frame is subject to yahooquery replacing
missing cells with zero, so option volume and open-interest totals may be
incomplete. If no instrument key or other news feed was supplied, an empty
news list does not imply there was no news. `services.stock_context_news.parse_news_rss` parses caller-fetched RSS
or Atom; it does not invent sentiment. Raw provider responses are retained in
`source_data`, subject to transformations already performed by yahooquery.

Ownership, annual growth and dividend yields are fractions; market/sector returns, volatility,
VWAP distance and analyst upside are percentage points. Altman Z uses the
public-manufacturing formula and should not be treated as calibrated for banks
or other non-manufacturers; it is unavailable when statement and quote
currencies cannot be matched. This is a research context, not an execution or
investment-advice system. Yahoo data may be delayed or incomplete; validate
source provenance, timestamps, currencies, and supplemental values before use.
Missing values in existing daily OHLCV rows are forward-filled then
backward-filled for technical calculations, with a warning and the original
history retained. These are estimates, not actual trades; completely absent
sessions are not inferred without an exchange calendar. Unfillable or
inconsistent candles still prevent technical calculations and produce a
warning; other local risk checks continue. The Altman
verification matrix and currency check are retained in `source_data`.
The documented Upstox shareholding feed does not supply pledged shares. If a
response explicitly contains a malformed pledge field, an unverified zero is
recorded and the local gate remains blocked until independently verified data
is supplied.
Technical indicators use in-memory pandas and NumPy calculations on all
supported Python versions. EMA and Wilder-style smoothing use recursive
exponential weighting (`adjust=False`); their initialization can differ from
other indicator libraries. Insufficient history or undefined ratios remain
null, rather than being presented as valid numbers.

`services.stock_context_housekeeping.ContextRotatingFileHandler` provides
size-limited logging and removes only aged, explicitly retired context
artifacts (`retired-stock-context-*.frame`, `.stream`, or `.cache`) from its
configured directory on rollover. Attach it to the desired logger and close
it when no longer needed. This opt-in utility does not delete unrelated
system caches or active in-memory dataframes.

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

Portfolio data, broker sessions, and AI analysis results are kept in the current
session rather than saved for restoration. Existing locally saved AI analyses
are removed when that browser/device runs the app. If Turso is configured,
legacy cloud broker registry entries are removed for the current signed-in user;
custom broker profiles in User settings are retained. Cleanup does not erase
backups or other devices and cannot run for a cloud account that is unavailable.
OAuth callback state remains short-lived so broker sign-in can complete.

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
