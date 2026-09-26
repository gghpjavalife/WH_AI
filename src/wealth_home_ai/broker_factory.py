"""Broker adapters with a common portfolio and order interface."""

from __future__ import annotations

import abc
import os
import secrets
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Literal, overload
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
from upstox_client import (
    ApiClient,
    Configuration,
    MarketQuoteV3Api,
    MutualFundApi,
    OrderApi,
    PlaceOrderRequest,
    PortfolioApi,
    UserApi,
)
from upstox_client.rest import ApiException

PORTFOLIO_COLUMNS = ["Ticker", "Qty", "Avg_Price", "LTP"]
MUTUAL_FUND_COLUMNS = [
    "Fund",
    "Folio",
    "Units",
    "Avg_NAV",
    "Latest_NAV",
    "NAV_Date",
]

TICKER_MAP = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "INFY": "NSE_EQ|INE009A01021",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "SBIN": "NSE_EQ|INE062A01020",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "ITC": "NSE_EQ|INE154A01025",
    "KOTAKBANK": "NSE_EQ|INE237A01028",
    "LT": "NSE_EQ|INE018A01030",
    "ETERNAL": "NSE_EQ|INE758T01015",
    "TATASTEEL": "NSE_EQ|INE081A01020",
    "IREDA": "NSE_EQ|INE024A01017",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "PNB": "NSE_EQ|INE160A01022",
    "ONGC": "NSE_EQ|INE213A01029",
    "IRFC": "NSE_EQ|INE053F01010",
    "JIOFIN": "NSE_EQ|INE758E01017",
    "SUZLON": "NSE_EQ|INE040H01021",
    "YESBANK": "NSE_EQ|INE528G01035",
    "GMRINFRA": "NSE_EQ|INE776C01039",
    "ALOKIND": "NSE_EQ|INE270A01029",
    "JPPOWER": "NSE_EQ|INE351F01018",
}


class BrokerAPIError(RuntimeError):
    """A broker request failed or returned an unusable response."""


class BrokerCapabilityError(BrokerAPIError):
    """The broker does not expose the requested portfolio capability."""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _number(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BrokerAPIError(f"Broker returned an invalid {field_name}.") from exc
    if not isfinite(number):
        raise BrokerAPIError(f"Broker returned an invalid {field_name}.")
    return number


def _normalize_instrument_key(value: Any) -> str:
    return str(value or "").strip().upper().replace(":", "|")


def _empty_portfolio() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Ticker": pd.Series(dtype="string"),
            "Qty": pd.Series(dtype="float64"),
            "Avg_Price": pd.Series(dtype="float64"),
            "LTP": pd.Series(dtype="float64"),
        },
        columns=PORTFOLIO_COLUMNS,
    )


def _portfolio_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return _empty_portfolio()
    frame = pd.DataFrame(rows, columns=PORTFOLIO_COLUMNS)
    frame["Ticker"] = (
        frame["Ticker"].astype("string").str.strip().str.upper().str.removesuffix("-EQ")
    )
    for column in ("Qty", "Avg_Price", "LTP"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
    if frame["Ticker"].isna().any() or frame["Ticker"].eq("").any():
        raise BrokerAPIError("Broker returned a position without a ticker.")
    if (
        any(
            not isfinite(value)
            for column in ("Qty", "Avg_Price", "LTP")
            for value in frame[column]
        )
        or frame["Avg_Price"].lt(0).any()
        or frame["LTP"].le(0).any()
    ):
        raise BrokerAPIError("Broker returned invalid position pricing data.")
    return frame[PORTFOLIO_COLUMNS]


class BrokerInterface(abc.ABC):
    """Abstract factory product for supported brokers."""

    @abc.abstractmethod
    def get_login_url(self, redirect_url: str) -> str | None:
        """Return an OAuth URL, or None for programmatic authentication."""

    @abc.abstractmethod
    def authenticate(self, code: str | None, redirect_url: str) -> str:
        """Authenticate a broker account and return its session token."""

    @abc.abstractmethod
    def fetch_balance(self, token: str) -> float:
        """Return available cash/margin in INR."""

    @abc.abstractmethod
    def fetch_positions(self, token: str) -> pd.DataFrame:
        """Return positions using PORTFOLIO_COLUMNS."""

    @abc.abstractmethod
    def fetch_holdings(self, token: str) -> pd.DataFrame:
        """Return long-term equity holdings using PORTFOLIO_COLUMNS."""

    @abc.abstractmethod
    def fetch_mutual_fund_holdings(self, token: str) -> pd.DataFrame:
        """Return supported mutual-fund holdings using MUTUAL_FUND_COLUMNS."""

    @abc.abstractmethod
    def fetch_live_prices(
        self, token: str, tickers: list[str] | None = None
    ) -> dict[str, float]:
        """Return authenticated live prices for supported NSE tickers."""

    @abc.abstractmethod
    def fetch_market_risk(self, token: str, ticker: str) -> dict[str, float]:
        """Return a live price, daily ATR, and 3-ATR risk boundary."""

    @abc.abstractmethod
    def place_order(
        self, token: str, ticker: str, qty: int, transaction_type: str
    ) -> Any:
        """Place a market order for an allow-listed NSE ticker."""


class UpstoxAdapter(BrokerInterface):
    """Upstox OAuth 2.0 and official Python SDK adapter."""

    TICKER_MAP = TICKER_MAP
    LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"
    TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

    def __init__(self) -> None:
        self.api_key = os.environ.get("UPSTOX_API_KEY", "").strip()
        self.api_secret = os.environ.get("UPSTOX_API_SECRET", "").strip()
        self.oauth_state: str | None = None
        self._instrument_keys_by_ticker: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def get_login_url(self, redirect_url: str) -> str:
        if not self.configured:
            raise ValueError("UPSTOX_API_KEY and UPSTOX_API_SECRET are required.")
        if not redirect_url.strip():
            raise ValueError("An Upstox callback URL is required.")
        self.oauth_state = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.api_key,
                "redirect_uri": redirect_url,
                "state": self.oauth_state,
            }
        )
        return f"{self.LOGIN_URL}?{query}"

    def authenticate(self, code: str | None, redirect_url: str) -> str:
        if not self.configured:
            raise ValueError("UPSTOX_API_KEY and UPSTOX_API_SECRET are required.")
        if not code:
            raise ValueError("Upstox did not return an authorization code.")
        response = requests.post(
            self.TOKEN_URL,
            data={
                "code": code,
                "client_id": self.api_key,
                "client_secret": self.api_secret,
                "redirect_uri": redirect_url,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=(5, 20),
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise BrokerAPIError("Upstox did not return an access token.")
        self.api_secret = ""
        return token

    @staticmethod
    def _api_client(token: str) -> ApiClient:
        if not token:
            raise ValueError("An Upstox access token is required.")
        configuration = Configuration()
        configuration.access_token = token
        return ApiClient(configuration)

    def fetch_balance(self, token: str) -> float:
        try:
            response = UserApi(self._api_client(token)).get_user_fund_margin(
                api_version="2.0", segment="SEC"
            )
        except ApiException as exc:
            raise BrokerAPIError("Unable to retrieve the Upstox cash balance.") from exc
        data = _field(response, "data")
        equity = _field(data, "equity")
        balance = _field(equity, "available_margin")
        if balance is None:
            raise BrokerAPIError("Upstox returned no available-margin value.")
        return _number(balance, "available margin")

    def fetch_positions(self, token: str) -> pd.DataFrame:
        try:
            response = PortfolioApi(self._api_client(token)).get_positions(
                api_version="2.0"
            )
        except ApiException as exc:
            raise BrokerAPIError("Unable to retrieve Upstox positions.") from exc
        positions = _field(response, "data", []) or []
        rows = []
        for position in positions:
            quantity = _number(_field(position, "quantity"), "position quantity")
            if quantity == 0:
                continue
            ticker = _field(
                position,
                "trading_symbol",
                _field(position, "tradingsymbol", ""),
            )
            instrument_key = _field(position, "instrument_token")
            if instrument_key and ticker:
                self._instrument_keys_by_ticker[str(ticker).strip().upper()] = str(
                    instrument_key
                )
            rows.append(
                {
                    "Ticker": ticker,
                    "Qty": quantity,
                    "Avg_Price": _number(
                        _field(position, "average_price"), "average price"
                    ),
                    "LTP": _number(_field(position, "last_price"), "last price"),
                }
            )
        return _portfolio_frame(rows)

    def fetch_holdings(self, token: str) -> pd.DataFrame:
        try:
            response = PortfolioApi(self._api_client(token)).get_holdings(
                api_version="2.0"
            )
        except ApiException as exc:
            raise BrokerAPIError("Unable to retrieve Upstox equity holdings.") from exc
        holdings = _field(response, "data", []) or []
        rows = []
        instrument_tickers: dict[str, str] = {}
        for holding in holdings:
            quantity = _number(_field(holding, "quantity"), "holding quantity")
            if quantity <= 0:
                continue
            ticker = _field(
                holding,
                "trading_symbol",
                _field(holding, "tradingsymbol", ""),
            )
            instrument_key = _field(holding, "instrument_token")
            if instrument_key and ticker:
                normalized_ticker = str(ticker).strip().upper()
                normalized_key = _normalize_instrument_key(instrument_key)
                instrument_tickers[normalized_key] = normalized_ticker
                self._instrument_keys_by_ticker[normalized_ticker] = str(
                    instrument_key
                )
            rows.append(
                {
                    "Ticker": ticker,
                    "Qty": quantity,
                    "Avg_Price": _number(
                        _field(holding, "average_price"), "average purchase price"
                    ),
                    "LTP": _number(_field(holding, "last_price"), "last price"),
                }
            )
        frame = _portfolio_frame(rows)
        if instrument_tickers:
            try:
                quote_response = MarketQuoteV3Api(self._api_client(token)).get_ltp(
                    instrument_key=",".join(instrument_tickers)
                )
            except ApiException as exc:
                raise BrokerAPIError(
                    "Unable to refresh live prices for Upstox equity holdings."
                ) from exc
            quotes = _field(quote_response, "data", {}) or {}
            live_prices = {}
            for instrument_key, quote in quotes.items():
                normalized_key = _normalize_instrument_key(instrument_key)
                ticker = instrument_tickers.get(normalized_key)
                if ticker is None:
                    normalized_key = _normalize_instrument_key(
                        _field(quote, "instrument_token")
                    )
                    ticker = instrument_tickers.get(normalized_key)
                if ticker is not None:
                    live_prices[ticker] = _number(
                        _field(quote, "last_price"), "live holding price"
                    )
            if live_prices:
                frame["LTP"] = frame.apply(
                    lambda row: live_prices.get(row["Ticker"], row["LTP"]), axis=1
                )
        return frame

    def fetch_mutual_fund_holdings(self, token: str) -> pd.DataFrame:
        try:
            response = MutualFundApi(self._api_client(token)).get_mutual_fund_holdings()
        except ApiException as exc:
            raise BrokerAPIError(
                "Unable to retrieve Upstox mutual-fund holdings."
            ) from exc
        data = _field(response, "data", []) or []
        if isinstance(data, dict):
            data = data.get("holdings", [])
        rows = []
        for holding in data:
            units = _number(_field(holding, "quantity"), "mutual-fund units")
            nav = _number(_field(holding, "last_price"), "latest mutual-fund NAV")
            average_nav = _number(
                _field(holding, "average_price"), "average mutual-fund NAV"
            )
            if units < 0 or nav <= 0 or average_nav < 0:
                raise BrokerAPIError(
                    "Upstox returned invalid mutual-fund holding values."
                )
            rows.append(
                {
                    "Fund": str(_field(holding, "fund", "Unknown fund")),
                    "Folio": str(_field(holding, "folio", "") or ""),
                    "Units": units,
                    "Avg_NAV": average_nav,
                    "Latest_NAV": nav,
                    "NAV_Date": str(_field(holding, "last_price_date", "") or ""),
                }
            )
        return pd.DataFrame(rows, columns=MUTUAL_FUND_COLUMNS)

    def fetch_live_prices(
        self, token: str, tickers: list[str] | None = None
    ) -> dict[str, float]:
        available_instruments = {
            **self.TICKER_MAP,
            **self._instrument_keys_by_ticker,
        }
        symbols = (
            sorted({ticker.strip().upper() for ticker in tickers})
            if tickers is not None
            else sorted(available_instruments)
        )
        unsupported = [
            symbol for symbol in symbols if symbol not in available_instruments
        ]
        if unsupported:
            raise ValueError(
                "Live quotes are limited to known Upstox NSE instrument keys."
            )
        if not symbols:
            return {}
        instrument_keys = ",".join(
            available_instruments[symbol] for symbol in symbols
        )
        try:
            response = MarketQuoteV3Api(self._api_client(token)).get_ltp(
                instrument_key=instrument_keys
            )
        except ApiException as exc:
            raise BrokerAPIError("Unable to retrieve Upstox live market prices.") from exc
        data = _field(response, "data", {}) or {}
        instrument_to_ticker = {
            _normalize_instrument_key(available_instruments[symbol]): symbol
            for symbol in symbols
        }
        prices: dict[str, float] = {}
        for instrument_key, quote in data.items():
            normalized_key = _normalize_instrument_key(instrument_key)
            ticker = instrument_to_ticker.get(normalized_key)
            if ticker is None:
                quote_key = _normalize_instrument_key(
                    _field(quote, "instrument_token")
                )
                ticker = instrument_to_ticker.get(quote_key)
            if ticker is None:
                continue
            price = _number(_field(quote, "last_price"), "live quote")
            if price > 0:
                prices[ticker] = price
        return prices

    def fetch_market_risk(self, token: str, ticker: str) -> dict[str, float]:
        from .upstox_helper import fetch_atr_and_ltp

        symbol = ticker.strip().upper()
        instrument_key = self._instrument_keys_by_ticker.get(symbol)
        return fetch_atr_and_ltp(token, symbol, instrument_key=instrument_key)

    def place_order(
        self, token: str, ticker: str, qty: int, transaction_type: str
    ) -> Any:
        symbol = ticker.strip().upper()
        if symbol not in self.TICKER_MAP:
            raise ValueError("Orders are limited to supported NSE tickers.")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError("Order quantity must be a positive whole number.")
        side = transaction_type.strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("Transaction type must be BUY or SELL.")
        try:
            return OrderApi(self._api_client(token)).place_order(
                PlaceOrderRequest(
                    quantity=qty,
                    product="D",
                    validity="DAY",
                    price=0,
                    tag="WealthHomeAI",
                    instrument_token=self.TICKER_MAP[symbol],
                    order_type="MARKET",
                    transaction_type=side,
                    disclosed_quantity=0,
                    trigger_price=0.0,
                    is_amo=False,
                ),
                api_version="2.0",
            )
        except ApiException as exc:
            raise BrokerAPIError("Upstox rejected the market order.") from exc


class AngelOneAdapter(BrokerInterface):
    """Angel One SmartAPI adapter using session-scoped account credentials."""

    def __init__(
        self,
        api_key: str | None = None,
        client_id: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get("ANGEL_API_KEY", "")).strip()
        self.client_id = (client_id or "").strip()
        self._password = password or ""
        self._totp_secret = totp_secret or ""

    def get_login_url(self, redirect_url: str) -> None:
        return None

    def authenticate(self, code: str | None, redirect_url: str) -> str:
        if not all((self.api_key, self.client_id, self._password, self._totp_secret)):
            raise ValueError(
                "Angel One requires an API key, client ID, password, and TOTP secret."
            )
        password = self._password
        one_time_password = pyotp.TOTP(self._totp_secret).now()
        self._password = ""
        self._totp_secret = ""
        response = SmartConnect(api_key=self.api_key).generateSession(
            self.client_id, password, one_time_password
        )
        self.client_id = ""
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerAPIError("Angel One authentication failed.")
        data = response.get("data") or {}
        token = data.get("jwtToken")
        if not isinstance(token, str) or not token:
            raise BrokerAPIError("Angel One did not return a session token.")
        return token

    def _client(self, token: str) -> SmartConnect:
        if not self.api_key or not token:
            raise ValueError("Angel One API key and session token are required.")
        client = SmartConnect(api_key=self.api_key)
        client.setAccessToken(token)
        return client

    def fetch_balance(self, token: str) -> float:
        response = self._client(token).rmsLimit()
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerAPIError("Unable to retrieve the Angel One cash balance.")
        data = response.get("data") or {}
        balance = data.get("availablecash", data.get("availableCash"))
        if balance is None:
            raise BrokerAPIError("Angel One returned no available-cash value.")
        return _number(balance, "available cash")

    @staticmethod
    def _symbol_details(client: SmartConnect, ticker: str) -> tuple[str, str]:
        symbol = ticker.upper().removesuffix("-EQ")
        response = client.searchScrip("NSE", f"{symbol}-EQ")
        matches = response.get("data") if isinstance(response, dict) else None
        for item in matches or []:
            trading_symbol = str(item.get("tradingsymbol", "")).upper()
            if trading_symbol == f"{symbol}-EQ":
                token = item.get("symboltoken")
                if token:
                    return trading_symbol, str(token)
        raise BrokerAPIError(f"Angel One has no NSE instrument token for {symbol}.")

    def fetch_positions(self, token: str) -> pd.DataFrame:
        client = self._client(token)
        response = client.position()
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerAPIError("Unable to retrieve Angel One positions.")
        rows = []
        for position in response.get("data") or []:
            quantity = _number(position.get("netqty"), "position quantity")
            if quantity == 0:
                continue
            ticker = str(position.get("tradingsymbol", "")).upper().removesuffix("-EQ")
            trading_symbol, symbol_token = self._symbol_details(client, ticker)
            quote = client.ltpData("NSE", trading_symbol, symbol_token)
            if not isinstance(quote, dict) or not quote.get("status"):
                raise BrokerAPIError(f"Unable to retrieve a live quote for {ticker}.")
            quote_data = quote.get("data") or {}
            average_price = position.get(
                "avgnetprice", position.get("averageprice", position.get("avgprice"))
            )
            rows.append(
                {
                    "Ticker": ticker,
                    "Qty": quantity,
                    "Avg_Price": _number(average_price, "average price"),
                    "LTP": _number(quote_data.get("ltp"), "last price"),
                }
            )
        return _portfolio_frame(rows)

    def fetch_holdings(self, token: str) -> pd.DataFrame:
        client = self._client(token)
        response = client.holding()
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerAPIError("Unable to retrieve Angel One equity holdings.")
        rows = []
        for holding in response.get("data") or []:
            quantity = _number(holding.get("quantity"), "holding quantity")
            if quantity <= 0:
                continue
            ticker = str(
                holding.get("tradingsymbol", holding.get("symbol", ""))
            ).upper()
            average_price = holding.get("averageprice", holding.get("avgprice"))
            ticker_symbol = ticker.removesuffix("-EQ")
            symbol_token = holding.get("symboltoken")
            if not ticker or not symbol_token:
                trading_symbol, symbol_token = self._symbol_details(
                    client, ticker_symbol
                )
            else:
                trading_symbol = (
                    ticker if ticker.endswith("-EQ") else f"{ticker}-EQ"
                )
            quote = client.ltpData("NSE", trading_symbol, str(symbol_token))
            if not isinstance(quote, dict) or not quote.get("status"):
                raise BrokerAPIError(
                    f"Unable to retrieve a live quote for {ticker_symbol}."
                )
            last_price = (quote.get("data") or {}).get("ltp")
            rows.append(
                {
                    "Ticker": ticker_symbol,
                    "Qty": quantity,
                    "Avg_Price": _number(average_price, "average purchase price"),
                    "LTP": _number(last_price, "last price"),
                }
            )
        return _portfolio_frame(rows)

    def fetch_mutual_fund_holdings(self, token: str) -> pd.DataFrame:
        raise BrokerCapabilityError(
            "Angel One SmartAPI does not expose a mutual-fund holdings endpoint. "
            "Import a current mutual-fund statement CSV to include those assets."
        )

    def fetch_live_prices(
        self, token: str, tickers: list[str] | None = None
    ) -> dict[str, float]:
        symbols = (
            sorted(set(ticker.strip().upper() for ticker in tickers))
            if tickers is not None
            else sorted(TICKER_MAP)
        )
        unsupported = [symbol for symbol in symbols if symbol not in TICKER_MAP]
        if unsupported:
            raise ValueError("Live quotes are limited to supported NSE tickers.")
        client = self._client(token)
        prices: dict[str, float] = {}
        for ticker in symbols:
            trading_symbol, symbol_token = self._symbol_details(client, ticker)
            response = client.ltpData("NSE", trading_symbol, symbol_token)
            if not isinstance(response, dict) or not response.get("status"):
                raise BrokerAPIError(
                    f"Unable to retrieve the live quote for {ticker}."
                )
            price = _number(
                (response.get("data") or {}).get("ltp"), "live quote"
            )
            if price > 0:
                prices[ticker] = price
        return prices

    def fetch_market_risk(self, token: str, ticker: str) -> dict[str, float]:
        """Return Angel One's live price and daily 14-period ATR floor."""
        client = self._client(token)
        trading_symbol, symbol_token = self._symbol_details(client, ticker)
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        start = now - timedelta(days=60)
        response = client.getCandleData(
            {
                "exchange": "NSE",
                "symboltoken": symbol_token,
                "interval": "ONE_DAY",
                "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                "todate": now.strftime("%Y-%m-%d %H:%M"),
            }
        )
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerAPIError(f"Unable to retrieve daily candles for {ticker}.")
        candles = response.get("data") or []
        if len(candles) < 15:
            raise BrokerAPIError(f"Insufficient daily candles to calculate ATR for {ticker}.")
        import pandas as pd

        from .upstox_helper import calculate_atr

        candle_frame = pd.DataFrame(
            candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"]
        ).sort_values("Timestamp", ascending=True)
        atr = calculate_atr(
            candle_frame["High"], candle_frame["Low"], candle_frame["Close"]
        )
        quote = client.ltpData("NSE", trading_symbol, symbol_token)
        if not isinstance(quote, dict) or not quote.get("status"):
            raise BrokerAPIError(f"Unable to retrieve a live quote for {ticker}.")
        ltp = _number((quote.get("data") or {}).get("ltp"), "last price")
        if atr <= 0 or ltp <= 0:
            raise BrokerAPIError(f"Angel One returned an invalid ATR for {ticker}.")
        return {"LTP": ltp, "ATR": atr, "Risk_Boundary": ltp - 3 * atr}

    def place_order(
        self, token: str, ticker: str, qty: int, transaction_type: str
    ) -> Any:
        symbol = ticker.strip().upper()
        if symbol not in TICKER_MAP:
            raise ValueError("Orders are limited to supported NSE tickers.")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError("Order quantity must be a positive whole number.")
        side = transaction_type.strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("Transaction type must be BUY or SELL.")
        client = self._client(token)
        trading_symbol, symbol_token = self._symbol_details(client, symbol)
        response = client.placeOrder(
            {
                "variety": "NORMAL",
                "tradingsymbol": trading_symbol,
                "symboltoken": symbol_token,
                "transactiontype": side,
                "exchange": "NSE",
                "ordertype": "MARKET",
                "producttype": "DELIVERY",
                "duration": "DAY",
                "price": 0,
                "quantity": str(qty),
            }
        )
        if not response:
            raise BrokerAPIError("Angel One did not acknowledge the market order.")
        return response


class BrokerFactory:
    """Create a supported broker adapter without coupling callers to constructors."""

    @overload
    @staticmethod
    def create_adapter(broker_name: Literal["Upstox"]) -> UpstoxAdapter: ...

    @overload
    @staticmethod
    def create_adapter(
        broker_name: Literal["Angel One"],
        *,
        api_key: str | None = None,
        client_id: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
    ) -> AngelOneAdapter: ...

    @staticmethod
    def create_adapter(
        broker_name: str, **credentials: str | None
    ) -> BrokerInterface:
        if broker_name == "Upstox":
            if credentials:
                raise ValueError("Upstox credentials are read from the environment.")
            return UpstoxAdapter()
        if broker_name == "Angel One":
            unexpected = set(credentials) - {
                "api_key",
                "client_id",
                "password",
                "totp_secret",
            }
            if unexpected:
                raise ValueError("Unsupported Angel One credential field.")
            return AngelOneAdapter(
                api_key=credentials.get("api_key"),
                client_id=credentials.get("client_id"),
                password=credentials.get("password"),
                totp_secret=credentials.get("totp_secret"),
            )
        raise ValueError(f"Unsupported broker: {broker_name}")
