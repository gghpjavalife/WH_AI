"""Typed application constants and enum-backed registries."""

from enum import Enum, StrEnum
from pathlib import Path


class ProjectPath(Enum):
    ROOT = Path(__file__).resolve().parents[2]


class AppIdentity(StrEnum):
    BRAND = "GGHP"
    BRAND_EXPANSION = "Governed Growth & Hedged Portfolios"
    BRAND_DESCRIPTION = "Governed multi-broker portfolio companion"
    TITLE = "GGHP | Governed Portfolio Companion"
    TIMEZONE = "Asia/Kolkata"
    # DEFAULT_REDIRECT_URI = "http://localhost:8501"
    DEFAULT_REDIRECT_URI = "https://gghp-ai-agent-dev.streamlit.app"

class AppTab(StrEnum):
    HOME = "Home"
    EQUITIES = "Equities"
    TRADES = "Trades"
    DERIVATIVES = "F&O"
    MUTUAL_FUNDS = "Mutual Funds"


class AppNavigation(StrEnum):
    HOME = ":material/home: Home"
    EQUITIES = ":material/show_chart: Equities"
    TRADES = ":material/swap_horiz: Trades"
    DERIVATIVES = ":material/query_stats: F&O"
    MUTUAL_FUNDS = ":material/savings: Mutual Funds"


class LegacyAppNavigation(Enum):
    HOME = ("Home", AppTab.HOME)
    EQUITIES = ("Equities", AppTab.EQUITIES)
    EQUITY_DEBT = ("Equity / Debt", AppTab.EQUITIES)
    DEBT = ("Debt", AppTab.EQUITIES)
    TRADING = ("Trading", AppTab.TRADES)
    TRADES = ("Trades", AppTab.TRADES)
    OPTIONS = ("Options", AppTab.DERIVATIVES)
    FUTURES = ("Futures", AppTab.DERIVATIVES)
    DERIVATIVES = ("F&O", AppTab.DERIVATIVES)
    MUTUAL_FUNDS = ("Mutual Funds", AppTab.MUTUAL_FUNDS)
    MUTUAL_FUNDS_LOWERCASE = ("Mutual funds", AppTab.MUTUAL_FUNDS)
    MARKET_RESEARCH = ("Market research", AppTab.EQUITIES)
    MARKET_SCANNER = ("Market scanner", AppTab.EQUITIES)
    ICON_HOME = (AppNavigation.HOME.value, AppTab.HOME)
    ICON_EQUITY_DEBT = (":material/account_balance: Equity / Debt", AppTab.EQUITIES)
    ICON_EQUITIES = (AppNavigation.EQUITIES.value, AppTab.EQUITIES)
    ICON_TRADES = (AppNavigation.TRADES.value, AppTab.TRADES)
    ICON_TRADES_ALT = (":material/swap_vert: Trades", AppTab.TRADES)
    ICON_DERIVATIVES = (AppNavigation.DERIVATIVES.value, AppTab.DERIVATIVES)
    ICON_MUTUAL_FUNDS = (AppNavigation.MUTUAL_FUNDS.value, AppTab.MUTUAL_FUNDS)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def tab(self) -> AppTab:
        return self.value[1]

    @classmethod
    def target_for(cls, label: str) -> AppTab | None:
        return next((item.tab for item in cls if item.label == label), None)


class PortfolioColumn(StrEnum):
    TICKER = "Ticker"
    QUANTITY = "Qty"
    AVERAGE_PRICE = "Avg_Price"
    LAST_TRADED_PRICE = "LTP"


class MutualFundColumn(StrEnum):
    FUND = "Fund"
    FOLIO = "Folio"
    ISIN = "ISIN"
    UNITS = "Units"
    AVERAGE_NAV = "Avg_NAV"
    LATEST_NAV = "Latest_NAV"
    NAV_DATE = "NAV_Date"


class LLMProvider(StrEnum):
    GEMINI = "Gemini"
    OPENAI = "OpenAI"
    ANTHROPIC = "Anthropic"
    GROQ = "Groq"
    TOGETHER_AI = "Together AI"
    MISTRAL = "Mistral"
    DEEPSEEK = "DeepSeek"
    OPENROUTER = "OpenRouter"
    XAI = "xAI"
    CEREBRAS = "Cerebras"
    CUSTOM_OPENAI_COMPATIBLE = "Custom OpenAI-compatible"


class LLMEndpoint(StrEnum):
    OPENAI = "https://api.openai.com/v1"
    ANTHROPIC = "https://api.anthropic.com/v1"
    GROQ = "https://api.groq.com/openai/v1"
    TOGETHER_AI = "https://api.together.xyz/v1"
    MISTRAL = "https://api.mistral.ai/v1"
    DEEPSEEK = "https://api.deepseek.com/v1"
    OPENROUTER = "https://openrouter.ai/api/v1"
    XAI = "https://api.x.ai/v1"
    CEREBRAS = "https://api.cerebras.ai/v1"


class LLMProviderBaseURL(Enum):
    GEMINI = (LLMProvider.GEMINI, None)
    OPENAI = (LLMProvider.OPENAI, LLMEndpoint.OPENAI.value)
    ANTHROPIC = (LLMProvider.ANTHROPIC, LLMEndpoint.ANTHROPIC.value)
    GROQ = (LLMProvider.GROQ, LLMEndpoint.GROQ.value)
    TOGETHER_AI = (LLMProvider.TOGETHER_AI, LLMEndpoint.TOGETHER_AI.value)
    MISTRAL = (LLMProvider.MISTRAL, LLMEndpoint.MISTRAL.value)
    DEEPSEEK = (LLMProvider.DEEPSEEK, LLMEndpoint.DEEPSEEK.value)
    OPENROUTER = (LLMProvider.OPENROUTER, LLMEndpoint.OPENROUTER.value)
    XAI = (LLMProvider.XAI, LLMEndpoint.XAI.value)
    CEREBRAS = (LLMProvider.CEREBRAS, LLMEndpoint.CEREBRAS.value)
    CUSTOM_OPENAI_COMPATIBLE = (LLMProvider.CUSTOM_OPENAI_COMPATIBLE, None)

    @property
    def provider(self) -> LLMProvider:
        return self.value[0]

    @property
    def base_url(self) -> str | None:
        return self.value[1]


class LLMProviderModels(Enum):
    GEMINI = (
        LLMProvider.GEMINI,
        ("gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"),
    )
    OPENAI = (
        LLMProvider.OPENAI,
        ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"),
    )
    ANTHROPIC = (
        LLMProvider.ANTHROPIC,
        (
            "claude-3-5-haiku-latest",
            "claude-3-7-sonnet-latest",
            "claude-sonnet-4-20250514",
        ),
    )
    GROQ = (
        LLMProvider.GROQ,
        (
            "llama-3.3-70b-versatile",
            "deepseek-r1-distill-llama-70b",
            "llama-3.1-8b-instant",
        ),
    )
    TOGETHER_AI = (
        LLMProvider.TOGETHER_AI,
        (
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Llama-3.1-8B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
        ),
    )
    MISTRAL = (
        LLMProvider.MISTRAL,
        ("mistral-small-latest", "mistral-large-latest", "open-mistral-nemo"),
    )
    DEEPSEEK = (
        LLMProvider.DEEPSEEK,
        ("deepseek-chat", "deepseek-reasoner"),
    )
    OPENROUTER = (
        LLMProvider.OPENROUTER,
        (
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "google/gemini-2.5-flash",
        ),
    )
    XAI = (LLMProvider.XAI, ("grok-3-mini", "grok-3"))
    CEREBRAS = (
        LLMProvider.CEREBRAS,
        ("llama-3.3-70b", "qwen-3-32b", "llama3.1-8b"),
    )
    CUSTOM_OPENAI_COMPATIBLE = (LLMProvider.CUSTOM_OPENAI_COMPATIBLE, ())

    @property
    def provider(self) -> LLMProvider:
        return self.value[0]

    @property
    def models(self) -> tuple[str, ...]:
        return self.value[1]


class BrokerEndpoint(StrEnum):
    UPSTOX_API_BASE = "https://api.upstox.com"
    UPSTOX_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"
    UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
    ZERODHA_API_BASE = "https://api.kite.trade"
    ZERODHA_LOGIN_URL = "https://kite.zerodha.com/connect/login"
    DHAN_API_BASE = "https://api.dhan.co/v2"
    DHAN_AUTH_BASE = "https://auth.dhan.co"
    DHAN_INSTRUMENTS_URL = (
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    )


class MarketDataEndpoint(StrEnum):
    YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
    NSE_EQUITY_MASTER = (
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    )
    BSE_EQUITY_MASTER = (
        "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    )


class RuntimeDefault(Enum):
    HTTP_CONNECT_TIMEOUT_SECONDS = 5
    HTTP_READ_TIMEOUT_SECONDS = 20
    DATABASE_TIMEOUT_SECONDS = 10
    OAUTH_STATE_TTL_SECONDS = 600
    MARKET_LOOKBACK_DAYS = 60
    ATR_PERIOD = 14
    ATR_MULTIPLIER = 3.0
    TRADING_SESSION_START = "09:15"
    TRADING_SESSION_END = "15:30"
    SYNC_INTERVAL_MINUTES = 5
    DASHBOARD_POLL_SECONDS = 300
    LLM_TEMPERATURE = 0.1
    LLM_MAX_RECOMMENDATIONS = 5
    LLM_RETRY_ATTEMPTS = 4
    LLM_RETRY_INITIAL_DELAY_SECONDS = 1.0
    LLM_RETRY_MAX_DELAY_SECONDS = 6.0
    LLM_RETRY_EXPONENT = 2.0
    LLM_RETRY_JITTER = 0.2
    MARKET_DATA_TIMEOUT = (5, 12)


class MarketDataPattern(StrEnum):
    SYMBOL = r"^[A-Z0-9&.-]{1,20}$"


class TickerInstrument(Enum):
    RELIANCE = ("RELIANCE", "NSE_EQ|INE002A01018")
    TCS = ("TCS", "NSE_EQ|INE467B01029")
    INFY = ("INFY", "NSE_EQ|INE009A01021")
    HDFCBANK = ("HDFCBANK", "NSE_EQ|INE040A01034")
    ICICIBANK = ("ICICIBANK", "NSE_EQ|INE090A01021")
    SBIN = ("SBIN", "NSE_EQ|INE062A01020")
    BHARTIARTL = ("BHARTIARTL", "NSE_EQ|INE397D01024")
    ITC = ("ITC", "NSE_EQ|INE154A01025")
    KOTAKBANK = ("KOTAKBANK", "NSE_EQ|INE237A01028")
    LT = ("LT", "NSE_EQ|INE018A01030")
    ETERNAL = ("ETERNAL", "NSE_EQ|INE758T01015")
    TATASTEEL = ("TATASTEEL", "NSE_EQ|INE081A01020")
    IREDA = ("IREDA", "NSE_EQ|INE024A01017")
    TATAMOTORS = ("TATAMOTORS", "NSE_EQ|INE155A01022")
    PNB = ("PNB", "NSE_EQ|INE160A01022")
    ONGC = ("ONGC", "NSE_EQ|INE213A01029")
    IRFC = ("IRFC", "NSE_EQ|INE053F01010")
    JIOFIN = ("JIOFIN", "NSE_EQ|INE758E01017")
    SUZLON = ("SUZLON", "NSE_EQ|INE040H01021")
    YESBANK = ("YESBANK", "NSE_EQ|INE528G01035")
    GMRINFRA = ("GMRINFRA", "NSE_EQ|INE776C01039")
    ALOKIND = ("ALOKIND", "NSE_EQ|INE270A01029")
    JPPOWER = ("JPPOWER", "NSE_EQ|INE351F01018")

    instrument_key: str

    def __new__(cls, ticker: str, instrument_key: str):
        member = object.__new__(cls)
        member._value_ = ticker
        member.instrument_key = instrument_key
        return member


def default_llm_provider_models() -> dict[str, tuple[str, ...]]:
    return {item.provider.value: item.models for item in LLMProviderModels}


def ticker_instrument_keys() -> dict[str, str]:
    return {ticker.value: ticker.instrument_key for ticker in TickerInstrument}


def portfolio_column_names() -> list[str]:
    return [column.value for column in PortfolioColumn]


def mutual_fund_column_names() -> list[str]:
    return [column.value for column in MutualFundColumn]
