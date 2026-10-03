"""Broker adapters with a common portfolio and order interface."""

from __future__ import annotations

import abc
import csv
import hashlib
import ipaddress
import io
import secrets
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Literal, overload
from urllib.parse import urlencode, urlsplit
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

from .settings import settings

PORTFOLIO_COLUMNS = ["Ticker", "Qty", "Avg_Price", "LTP"]
MUTUAL_FUND_COLUMNS = [
    "Fund",
    "Folio",
    "ISIN",
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

    def fetch_profile(self, token: str) -> dict[str, str]:
        """Return safe, non-sensitive profile fields when supported."""
        raise BrokerCapabilityError("This broker does not expose a profile endpoint.")

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
    LOGIN_URL = settings.upstox_login_url
    TOKEN_URL = settings.upstox_token_url

    def __init__(
        self, api_key: str | None = None, api_secret: str | None = None
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = api_secret or ""
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
            timeout=(
                settings.http_connect_timeout_seconds,
                settings.http_read_timeout_seconds,
            ),
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

    @staticmethod
    def _request_timeout() -> tuple[int, int]:
        return (
            settings.http_connect_timeout_seconds,
            settings.http_read_timeout_seconds,
        )

    def fetch_balance(self, token: str) -> float:
        try:
            response = UserApi(self._api_client(token)).get_user_fund_margin(
                api_version="2.0",
                segment="SEC",
                _request_timeout=self._request_timeout(),
            )
        except ApiException as exc:
            raise BrokerAPIError("Unable to retrieve the Upstox cash balance.") from exc
        data = _field(response, "data")
        equity = _field(data, "equity")
        balance = _field(equity, "available_margin")
        if balance is None:
            raise BrokerAPIError("Upstox returned no available-margin value.")
        return _number(balance, "available margin")

    def fetch_profile(self, token: str) -> dict[str, str]:
        try:
            response = UserApi(self._api_client(token)).get_profile(
                api_version="2.0",
                _request_timeout=self._request_timeout(),
            )
        except ApiException as exc:
            raise BrokerAPIError("Unable to retrieve the Upstox profile.") from exc
        data = _field(response, "data", {}) or {}
        return {
            "name": str(
                _field(data, "user_name", _field(data, "user_shortname", "")) or ""
            ),
            "user_id": str(_field(data, "user_id", "") or ""),
        }

    def fetch_positions(self, token: str) -> pd.DataFrame:
        try:
            response = PortfolioApi(self._api_client(token)).get_positions(
                api_version="2.0",
                _request_timeout=self._request_timeout(),
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
                api_version="2.0",
                _request_timeout=self._request_timeout(),
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
                    instrument_key=",".join(instrument_tickers),
                    _request_timeout=self._request_timeout(),
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
            response = MutualFundApi(self._api_client(token)).get_mutual_fund_holdings(
                _request_timeout=self._request_timeout()
            )
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
                    "ISIN": str(_field(holding, "instrument_key", "") or ""),
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
        if tickers is not None:
            try:
                from .market_research import fetch_nse_equity_master

                nse_master = fetch_nse_equity_master()
                for row in nse_master.itertuples(index=False):
                    symbol = str(row.Symbol).strip().upper()
                    isin = str(row.ISIN).strip().upper()
                    if symbol and isin:
                        available_instruments.setdefault(symbol, f"NSE_EQ|{isin}")
            except (BrokerAPIError, requests.RequestException, RuntimeError, ValueError):
                pass
        symbols = (
            sorted({ticker.strip().upper() for ticker in tickers})
            if tickers is not None
            else sorted(available_instruments)
        )
        symbols = [symbol for symbol in symbols if symbol in available_instruments]
        if not symbols:
            return {}
        instrument_keys = ",".join(
            available_instruments[symbol] for symbol in symbols
        )
        try:
            response = MarketQuoteV3Api(self._api_client(token)).get_ltp(
                instrument_key=instrument_keys,
                _request_timeout=self._request_timeout(),
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
                _request_timeout=self._request_timeout(),
            )
        except ApiException as exc:
            raise BrokerAPIError("Upstox rejected the market order.") from exc


class ZerodhaAdapter(BrokerInterface):
    """Kite Connect adapter for Zerodha accounts."""

    LOGIN_URL = settings.zerodha_login_url
    API_BASE = settings.zerodha_api_base

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.api_key = (api_key or "").strip()
        self.api_secret = api_secret or ""
        self.oauth_state: str | None = None
        self._instrument_tokens: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def get_login_url(self, redirect_url: str) -> str:
        if not self.configured:
            raise ValueError("A Zerodha API key and API secret are required.")
        if not redirect_url.strip():
            raise ValueError("A Zerodha callback URL is required.")
        self.oauth_state = secrets.token_urlsafe(32)
        params = urlencode(
            {
                "v": "3",
                "api_key": self.api_key,
                "redirect_params": urlencode({"state": self.oauth_state}),
            }
        )
        return f"{self.LOGIN_URL}?{params}"

    def authenticate(self, code: str | None, redirect_url: str) -> str:
        if not self.configured:
            raise ValueError("A Zerodha API key and API secret are required.")
        if not code:
            raise ValueError("Zerodha did not return a request token.")
        checksum = hashlib.sha256(
            f"{self.api_key}{code}{self.api_secret}".encode("utf-8")
        ).hexdigest()
        try:
            response = requests.post(
                f"{self.API_BASE}/session/token",
                data={
                    "api_key": self.api_key,
                    "request_token": code,
                    "checksum": checksum,
                },
                headers={"X-Kite-Version": "3"},
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BrokerAPIError("Unable to exchange the Zerodha request token.") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise BrokerAPIError("Zerodha did not return an access token.")
        self.api_secret = ""
        return token

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        if not token:
            raise ValueError("A Zerodha access token is required.")
        try:
            response = requests.request(
                method,
                f"{self.API_BASE}{path}",
                headers={
                    "X-Kite-Version": "3",
                    "Authorization": f"token {self.api_key}:{token}",
                },
                params=params,
                data=data,
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BrokerAPIError(f"Zerodha request failed: {path}.") from exc
        except ValueError as exc:
            raise BrokerAPIError("Zerodha returned an invalid response.") from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise BrokerAPIError(f"Zerodha rejected the request: {path}.")
        return payload.get("data") or {}

    def fetch_balance(self, token: str) -> float:
        data = self._request("GET", "/user/margins/equity", token)
        available = data.get("available") or {}
        balance = available.get("cash", available.get("live_balance"))
        if balance is None:
            raise BrokerAPIError("Zerodha returned no available-cash balance.")
        return _number(balance, "available cash")

    def fetch_profile(self, token: str) -> dict[str, str]:
        data = self._request("GET", "/user/profile", token)
        return {
            "name": str(data.get("user_name") or data.get("user_shortname") or ""),
            "user_id": str(data.get("user_id") or ""),
        }

    def fetch_positions(self, token: str) -> pd.DataFrame:
        data = self._request("GET", "/portfolio/positions", token)
        rows = []
        for position in data.get("net") or []:
            if str(position.get("exchange", "")).upper() != "NSE":
                continue
            quantity = _number(position.get("quantity"), "position quantity")
            if quantity == 0:
                continue
            ticker = str(position.get("tradingsymbol", "")).strip().upper()
            ltp = _number(position.get("last_price"), "last price")
            if ticker and ltp > 0:
                rows.append(
                    {
                        "Ticker": ticker,
                        "Qty": quantity,
                        "Avg_Price": _number(
                            position.get("average_price"), "average price"
                        ),
                        "LTP": ltp,
                    }
                )
        return _portfolio_frame(rows)

    def fetch_holdings(self, token: str) -> pd.DataFrame:
        holdings = self._request("GET", "/portfolio/holdings", token)
        rows = []
        for holding in holdings:
            if str(holding.get("exchange", "")).upper() != "NSE":
                continue
            quantity = _number(holding.get("quantity"), "holding quantity")
            ltp = _number(holding.get("last_price"), "last price")
            if quantity <= 0 or ltp <= 0:
                continue
            rows.append(
                {
                    "Ticker": holding.get("tradingsymbol"),
                    "Qty": quantity,
                    "Avg_Price": _number(
                        holding.get("average_price"), "average purchase price"
                    ),
                    "LTP": ltp,
                }
            )
        return _portfolio_frame(rows)

    def fetch_mutual_fund_holdings(self, token: str) -> pd.DataFrame:
        holdings = self._request("GET", "/mf/holdings", token)
        if not isinstance(holdings, list):
            raise BrokerAPIError(
                "Zerodha returned an invalid mutual-fund holdings response."
            )
        rows = []
        for holding in holdings:
            if not isinstance(holding, dict):
                raise BrokerAPIError(
                    "Zerodha returned an invalid mutual-fund holding."
                )
            units = _number(holding.get("quantity"), "mutual-fund units")
            latest_nav = _number(holding.get("last_price"), "latest mutual-fund NAV")
            average_nav = _number(
                holding.get("average_price"), "average mutual-fund NAV"
            )
            if units < 0 or latest_nav <= 0 or average_nav < 0:
                raise BrokerAPIError(
                    "Zerodha returned invalid mutual-fund holding values."
                )
            rows.append(
                {
                    "Fund": str(holding.get("fund") or "Unknown fund"),
                    "Folio": str(holding.get("folio") or ""),
                    "ISIN": str(holding.get("tradingsymbol") or ""),
                    "Units": units,
                    "Avg_NAV": average_nav,
                    "Latest_NAV": latest_nav,
                    "NAV_Date": str(holding.get("last_price_date") or ""),
                }
            )
        return pd.DataFrame(rows, columns=MUTUAL_FUND_COLUMNS)

    def fetch_live_prices(
        self, token: str, tickers: list[str] | None = None
    ) -> dict[str, float]:
        symbols = sorted(
            {ticker.strip().upper() for ticker in tickers}
            if tickers is not None
            else set(TICKER_MAP)
        )
        if not symbols:
            return {}
        data = self._request(
            "GET",
            "/quote/ltp",
            token,
            params={"i": [f"NSE:{symbol}" for symbol in symbols]},
        )
        prices = {}
        for symbol in symbols:
            quote = data.get(f"NSE:{symbol}")
            if isinstance(quote, dict):
                price = _number(quote.get("last_price"), "live quote")
                if price > 0:
                    prices[symbol] = price
        return prices

    def _instrument_token(self, token: str, ticker: str) -> str:
        if not self._instrument_tokens:
            try:
                response = requests.get(
                    f"{self.API_BASE}/instruments/NSE",
                    headers={
                        "X-Kite-Version": "3",
                        "Authorization": f"token {self.api_key}:{token}",
                    },
                    timeout=(
                        settings.http_connect_timeout_seconds,
                        settings.http_read_timeout_seconds,
                    ),
                )
                response.raise_for_status()
                instruments = csv.DictReader(io.StringIO(response.text))
                self._instrument_tokens = {
                    row["tradingsymbol"].strip().upper(): row["instrument_token"]
                    for row in instruments
                    if row.get("exchange") == "NSE"
                    and row.get("instrument_type") == "EQ"
                    and row.get("tradingsymbol")
                    and row.get("instrument_token")
                }
            except (requests.RequestException, KeyError, csv.Error) as exc:
                raise BrokerAPIError(
                    "Could not load the Zerodha NSE instrument list."
                ) from exc
        instrument_token = self._instrument_tokens.get(ticker)
        if not instrument_token:
            raise BrokerAPIError(f"Zerodha has no NSE equity instrument for {ticker}.")
        return instrument_token

    def fetch_market_risk(self, token: str, ticker: str) -> dict[str, float]:
        symbol = ticker.strip().upper()
        if symbol not in TICKER_MAP:
            raise ValueError("Risk checks are limited to supported NSE tickers.")
        instrument_token = self._instrument_token(token, symbol)
        now = datetime.now(ZoneInfo(settings.timezone))
        start = now - timedelta(days=settings.market_lookback_days)
        data = self._request(
            "GET",
            f"/instruments/historical/{instrument_token}/day",
            token,
            params={
                "from": start.strftime("%Y-%m-%d %H:%M:%S"),
                "to": now.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        candles = data.get("candles") or []
        if len(candles) < settings.atr_period + 1:
            raise BrokerAPIError(f"Insufficient Zerodha candles for {symbol}.")
        frame = pd.DataFrame(
            candles,
            columns=["Timestamp", "Open", "High", "Low", "Close", "Volume", "OI"],
        ).sort_values("Timestamp")
        from .upstox_helper import calculate_atr

        atr = calculate_atr(frame["High"], frame["Low"], frame["Close"])
        quote = self._request(
            "GET",
            "/quote/ltp",
            token,
            params={"i": f"NSE:{symbol}"},
        )
        quote_data = quote.get(f"NSE:{symbol}") or {}
        ltp = _number(quote_data.get("last_price"), "live price")
        if ltp <= 0:
            raise BrokerAPIError(f"Zerodha returned an invalid live price for {symbol}.")
        return {
            "LTP": ltp,
            "ATR": atr,
            "Risk_Boundary": ltp - settings.atr_multiplier * atr,
        }

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
        return self._request(
            "POST",
            "/orders/regular",
            token,
            data={
                "exchange": "NSE",
                "tradingsymbol": symbol,
                "transaction_type": side,
                "order_type": "MARKET",
                "quantity": qty,
                "product": "CNC",
                "validity": "DAY",
                "market_protection": -1,
                "tag": "WealthHomeAI",
            },
        )


class DhanAdapter(BrokerInterface):
    """DhanHQ adapter using each account's Dhan API key and secret."""

    AUTH_BASE = settings.dhan_auth_base
    API_BASE = settings.dhan_api_base
    INSTRUMENTS_URL = settings.dhan_instruments_url

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        client_id: str | None = None,
    ):
        self.api_key = (api_key or "").strip()
        self.api_secret = api_secret or ""
        self.client_id = (client_id or "").strip()
        self._consent_id: str | None = None
        self._security_ids_by_ticker: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.client_id)

    def get_login_url(self, redirect_url: str) -> str:
        if not self.configured:
            raise ValueError(
                "Dhan requires your Client ID, API key, and API secret."
            )
        if not redirect_url.strip():
            raise ValueError("Register and enter a Dhan callback URL.")
        try:
            response = requests.post(
                f"{self.AUTH_BASE}/app/generate-consent",
                params={"client_id": self.client_id},
                headers={
                    "app_id": self.api_key,
                    "app_secret": self.api_secret,
                },
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BrokerAPIError("Could not start Dhan account authorization.") from exc
        except ValueError as exc:
            raise BrokerAPIError("Dhan returned an invalid consent response.") from exc
        consent_id = payload.get("consentAppId") if isinstance(payload, dict) else None
        if not isinstance(consent_id, str) or not consent_id:
            raise BrokerAPIError("Dhan did not create an authorization session.")
        self._consent_id = consent_id
        return (
            f"{self.AUTH_BASE}/login/consentApp-login?"
            + urlencode({"consentAppId": consent_id})
        )

    def authenticate(self, code: str | None, redirect_url: str) -> str:
        if not self.configured or not self._consent_id:
            raise ValueError("Start a Dhan authorization before exchanging its token.")
        if not code:
            raise ValueError("Dhan did not return a consent token.")
        try:
            response = requests.get(
                f"{self.AUTH_BASE}/app/consumeApp-consent",
                params={"tokenId": code},
                headers={
                    "app_id": self.api_key,
                    "app_secret": self.api_secret,
                },
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BrokerAPIError("Could not exchange the Dhan consent token.") from exc
        except ValueError as exc:
            raise BrokerAPIError("Dhan returned an invalid access-token response.") from exc
        token = payload.get("accessToken") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise BrokerAPIError("Dhan did not return an access token.")
        response_client_id = str(payload.get("dhanClientId") or "").strip()
        if response_client_id and response_client_id != self.client_id:
            raise BrokerAPIError("Dhan returned a different client ID than expected.")
        self.api_secret = ""
        self._consent_id = None
        return token

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not token or not self.client_id:
            raise ValueError("A Dhan client ID and access token are required.")
        try:
            response = requests.request(
                method,
                f"{self.API_BASE}{path}",
                headers={
                    "access-token": token,
                    "client-id": self.client_id,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                params=params,
                json=body,
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BrokerAPIError(f"Dhan request failed: {path}.") from exc
        except ValueError as exc:
            raise BrokerAPIError("Dhan returned an invalid response.") from exc
        if isinstance(payload, dict) and payload.get("errorCode"):
            raise BrokerAPIError(
                str(payload.get("errorMessage") or "Dhan rejected the request.")
            )
        return payload

    def fetch_balance(self, token: str) -> float:
        payload = self._request("GET", "/fundlimit", token)
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise BrokerAPIError("Dhan returned no available-funds record.")
        balance = next(
            (
                payload[key]
                for key in (
                    "availableBalance",
                    "availabelBalance",
                    "withdrawableBalance",
                    "sodLimit",
                )
                if payload.get(key) is not None
            ),
            None,
        )
        if balance is None:
            raise BrokerAPIError("Dhan returned no available-cash balance.")
        return _number(balance, "available cash")

    def fetch_profile(self, token: str) -> dict[str, str]:
        payload = self._request("GET", "/profile", token)
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise BrokerAPIError("Dhan returned an invalid profile response.")
        return {
            "name": str(payload.get("dhanClientName") or payload.get("name") or ""),
            "user_id": str(
                payload.get("dhanClientId") or payload.get("clientId") or ""
            ),
        }

    def _load_instruments(self, token: str) -> None:
        if self._security_ids_by_ticker:
            return
        try:
            response = requests.get(
                self.INSTRUMENTS_URL,
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
            )
            response.raise_for_status()
            instruments = pd.read_csv(
                io.StringIO(response.text),
                usecols=[
                    "EXCH_ID",
                    "SEGMENT",
                    "SECURITY_ID",
                    "ISIN",
                    "INSTRUMENT",
                    "SERIES",
                    "SYMBOL_NAME",
                ],
                dtype=str,
            )
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise BrokerAPIError("Could not load the Dhan NSE instrument list.") from exc
        isin_to_ticker = {
            _normalize_instrument_key(key).split("|")[-1]: ticker
            for ticker, key in TICKER_MAP.items()
        }
        nse_equities = instruments[
            instruments["EXCH_ID"].str.upper().eq("NSE")
            & instruments["SEGMENT"].str.upper().eq("E")
            & instruments["INSTRUMENT"].str.upper().eq("EQUITY")
        ]
        for row in nse_equities.itertuples(index=False):
            ticker = isin_to_ticker.get(_normalize_instrument_key(row.ISIN))
            if ticker is None and str(row.SERIES).upper() == "EQ":
                ticker = str(row.SYMBOL_NAME).strip().upper()
            if ticker and str(row.SECURITY_ID).strip().isdigit():
                self._security_ids_by_ticker[ticker] = str(row.SECURITY_ID).strip()
        if not self._security_ids_by_ticker:
            raise BrokerAPIError("The Dhan instrument list had no supported NSE equities.")

    def fetch_live_prices(
        self, token: str, tickers: list[str] | None = None
    ) -> dict[str, float]:
        self._load_instruments(token)
        requested_symbols = (
            {ticker.strip().upper() for ticker in tickers}
            if tickers is not None
            else set(TICKER_MAP)
        )
        unsupported = sorted(
            requested_symbols - self._security_ids_by_ticker.keys()
        )
        symbols = sorted(requested_symbols & self._security_ids_by_ticker.keys())
        if not symbols:
            return {}
        payload = self._request(
            "POST",
            "/marketfeed/ltp",
            token,
            body={
                "NSE_EQ": [
                    int(self._security_ids_by_ticker[symbol]) for symbol in symbols
                ]
            },
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        quotes = data.get("NSE_EQ", {}) if isinstance(data, dict) else {}
        prices = {}
        for symbol in symbols:
            quote = quotes.get(self._security_ids_by_ticker[symbol])
            if isinstance(quote, dict):
                price = _number(quote.get("last_price"), "live quote")
                if price > 0:
                    prices[symbol] = price
        return prices

    def fetch_positions(self, token: str) -> pd.DataFrame:
        self._load_instruments(token)
        positions = self._request("GET", "/positions", token)
        if not isinstance(positions, list):
            raise BrokerAPIError("Dhan returned an invalid positions response.")
        symbols = {
            str(position.get("tradingSymbol", "")).strip().upper()
            for position in positions
            if str(position.get("exchangeSegment", "")).upper() == "NSE_EQ"
            and _number(position.get("netQty", 0), "position quantity") != 0
        }
        mapped_symbols = sorted(symbols & self._security_ids_by_ticker.keys())
        prices = self.fetch_live_prices(token, mapped_symbols) if mapped_symbols else {}
        rows = []
        for position in positions:
            if str(position.get("exchangeSegment", "")).upper() != "NSE_EQ":
                continue
            ticker = str(position.get("tradingSymbol", "")).strip().upper()
            quantity = _number(position.get("netQty", 0), "position quantity")
            if quantity == 0 or ticker not in prices:
                continue
            rows.append(
                {
                    "Ticker": ticker,
                    "Qty": quantity,
                    "Avg_Price": _number(
                        position.get("buyAvg", position.get("costPrice")),
                        "average price",
                    ),
                    "LTP": prices[ticker],
                }
            )
        return _portfolio_frame(rows)

    def fetch_holdings(self, token: str) -> pd.DataFrame:
        self._load_instruments(token)
        holdings = self._request("GET", "/holdings", token)
        if not isinstance(holdings, list):
            raise BrokerAPIError("Dhan returned an invalid holdings response.")
        rows = []
        for holding in holdings:
            ticker = str(holding.get("tradingSymbol", "")).strip().upper()
            quantity = _number(holding.get("totalQty", 0), "holding quantity")
            if quantity <= 0 or ticker not in self._security_ids_by_ticker:
                continue
            rows.append(
                {
                    "Ticker": ticker,
                    "Qty": quantity,
                    "Avg_Price": _number(
                        holding.get("avgCostPrice"), "average purchase price"
                    ),
                    "LTP": 1.0,
                }
            )
        if not rows:
            return _empty_portfolio()
        prices = self.fetch_live_prices(
            token, [str(row["Ticker"]) for row in rows]
        )
        for row in rows:
            price = prices.get(str(row["Ticker"]))
            if price is None:
                raise BrokerAPIError(
                    f"Dhan returned no current market quote for {row['Ticker']}."
                )
            row["LTP"] = price
        return _portfolio_frame(rows)

    def fetch_mutual_fund_holdings(self, token: str) -> pd.DataFrame:
        raise BrokerCapabilityError(
            "DhanHQ does not expose a mutual-fund holdings endpoint. "
            "Import a current mutual-fund statement CSV to include these assets."
        )

    def fetch_market_risk(self, token: str, ticker: str) -> dict[str, float]:
        symbol = ticker.strip().upper()
        if symbol not in TICKER_MAP:
            raise ValueError("Risk checks are limited to supported NSE tickers.")
        self._load_instruments(token)
        security_id = self._security_ids_by_ticker.get(symbol)
        if not security_id:
            raise BrokerAPIError(f"Dhan has no NSE equity instrument for {symbol}.")
        now = datetime.now(ZoneInfo(settings.timezone))
        start = now.date() - timedelta(days=settings.market_lookback_days)
        candles = self._request(
            "POST",
            "/charts/historical",
            token,
            body={
                "securityId": security_id,
                "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY",
                "expiryCode": 0,
                "oi": False,
                "fromDate": start.isoformat(),
                "toDate": now.date().isoformat(),
            },
        )
        if not isinstance(candles, dict):
            raise BrokerAPIError("Dhan returned invalid historical candle data.")
        frame = pd.DataFrame(
            {
                "High": candles.get("high", []),
                "Low": candles.get("low", []),
                "Close": candles.get("close", []),
            }
        )
        from .upstox_helper import calculate_atr

        atr = calculate_atr(frame["High"], frame["Low"], frame["Close"])
        prices = self.fetch_live_prices(token, [symbol])
        ltp = prices.get(symbol)
        if ltp is None or ltp <= 0:
            raise BrokerAPIError(f"Dhan returned no valid live price for {symbol}.")
        return {
            "LTP": ltp,
            "ATR": atr,
            "Risk_Boundary": ltp - settings.atr_multiplier * atr,
        }

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
        self._load_instruments(token)
        security_id = self._security_ids_by_ticker.get(symbol)
        if not security_id:
            raise BrokerAPIError(f"Dhan has no NSE equity instrument for {symbol}.")
        payload = self._request(
            "POST",
            "/orders",
            token,
            body={
                "dhanClientId": self.client_id,
                "correlationId": "WealthHomeAI",
                "transactionType": side,
                "exchangeSegment": "NSE_EQ",
                "productType": "CNC",
                "orderType": "MARKET",
                "validity": "DAY",
                "securityId": security_id,
                "quantity": qty,
                "disclosedQuantity": 0,
                "price": 0,
                "triggerPrice": 0,
                "afterMarketOrder": False,
            },
        )
        if not isinstance(payload, dict) or not payload.get("orderId"):
            raise BrokerAPIError("Dhan did not acknowledge the market order.")
        return payload


class AngelOneAdapter(BrokerInterface):
    """Angel One SmartAPI adapter using session-scoped account credentials."""

    def __init__(
        self,
        api_key: str | None = None,
        client_id: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
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

    def fetch_profile(self, token: str) -> dict[str, str]:
        response = self._client(token).getProfile(token)
        if not isinstance(response, dict) or not response.get("status"):
            raise BrokerAPIError("Unable to retrieve the Angel One profile.")
        data = response.get("data") or {}
        return {
            "name": str(data.get("name") or data.get("clientName") or ""),
            "user_id": str(data.get("clientcode") or self.client_id or ""),
        }

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
        client = self._client(token)
        prices: dict[str, float] = {}
        for ticker in symbols:
            try:
                trading_symbol, symbol_token = self._symbol_details(client, ticker)
            except BrokerAPIError:
                continue
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
        """Return Angel One's live price and configured ATR risk floor."""
        client = self._client(token)
        trading_symbol, symbol_token = self._symbol_details(client, ticker)
        now = datetime.now(ZoneInfo(settings.timezone))
        start = now - timedelta(days=settings.market_lookback_days)
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
        return {
            "LTP": ltp,
            "ATR": atr,
            "Risk_Boundary": ltp - settings.atr_multiplier * atr,
        }

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


class GenericDynamicAdapter(BrokerInterface):
    """Tenant-configured, read-only adapter for approved HTTPS REST APIs.

    Endpoint hosts must be explicitly allow-listed by the server operator. This
    adapter never places orders and never accepts endpoint hosts from the client.
    """

    supports_live_orders = False

    def __init__(self, configuration: dict[str, Any]) -> None:
        base_url = str(configuration.get("api_base", "")).strip().rstrip("/")
        parsed = urlsplit(base_url)
        allowed_hosts = set(settings.dynamic_broker_allowed_hosts)
        hostname = (parsed.hostname or "").lower()
        try:
            is_ip_address = bool(hostname and ipaddress.ip_address(hostname))
        except ValueError:
            is_ip_address = False
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or is_ip_address
            or hostname not in allowed_hosts
        ):
            raise ValueError(
                "Custom broker APIs must use an operator-allow-listed HTTPS hostname."
            )
        endpoint_config = configuration.get("endpoints")
        if not isinstance(endpoint_config, dict):
            raise ValueError("Custom broker endpoint paths are required.")
        self.api_base = base_url
        self.name = str(configuration.get("name", "Custom broker")).strip()
        self.endpoints: dict[str, str] = {}
        for key in ("balance", "positions", "holdings", "profile", "live_prices"):
            path = str(endpoint_config.get(key, "")).strip()
            if not path:
                continue
            if (
                not path.startswith("/")
                or path.startswith("//")
                or "://" in path
                or "?" in path
                or "#" in path
                or "\\" in path
                or any(part == ".." for part in path.split("/"))
            ):
                raise ValueError(f"Custom broker {key} endpoint must be a safe path.")
            self.endpoints[key] = path
        if not {"balance", "positions", "holdings"}.issubset(self.endpoints):
            raise ValueError(
                "Custom broker must define balance, positions, and holdings paths."
            )

    def get_login_url(self, redirect_url: str) -> None:
        return None

    def authenticate(self, code: str | None, redirect_url: str) -> str:
        token = (code or "").strip()
        if not token:
            raise ValueError("Enter a bearer token for this custom broker.")
        if len(token) > 4096:
            raise ValueError("Custom broker bearer token exceeds the allowed length.")
        return token

    def _get_json(
        self, endpoint: str, token: str, *, params: dict[str, str] | None = None
    ) -> Any:
        path = self.endpoints.get(endpoint)
        if path is None:
            raise BrokerCapabilityError(
                f"{self.name} does not define a {endpoint.replace('_', ' ')} endpoint."
            )
        try:
            response = requests.get(
                f"{self.api_base}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=(
                    settings.http_connect_timeout_seconds,
                    settings.http_read_timeout_seconds,
                ),
                allow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise BrokerAPIError(
                f"Custom broker {endpoint.replace('_', ' ')} request failed."
            ) from exc
        except ValueError as exc:
            raise BrokerAPIError("Custom broker returned invalid JSON.") from exc

    @staticmethod
    def _data(payload: Any, expected_type: type) -> Any:
        if isinstance(payload, expected_type):
            return payload
        if isinstance(payload, dict):
            value = payload.get("data")
            if isinstance(value, expected_type):
                return value
            for key in ("balance", "positions", "holdings", "prices"):
                value = payload.get(key)
                if isinstance(value, expected_type):
                    return value
        raise BrokerAPIError("Custom broker response does not match the required schema.")

    def fetch_balance(self, token: str) -> float:
        payload = self._get_json("balance", token)
        if isinstance(payload, dict):
            if "data" in payload and isinstance(payload["data"], dict):
                payload = payload["data"]
            balance = payload.get("balance", payload.get("available_cash"))
        else:
            balance = payload
        return _number(balance, "available cash")

    @staticmethod
    def _portfolio(payload: Any) -> pd.DataFrame:
        items = GenericDynamicAdapter._data(payload, list)
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise BrokerAPIError("Custom broker returned an invalid position row.")
            rows.append(
                {
                    "Ticker": item.get("Ticker", item.get("ticker")),
                    "Qty": item.get("Qty", item.get("qty", item.get("quantity"))),
                    "Avg_Price": item.get(
                        "Avg_Price", item.get("avg_price", item.get("average_price"))
                    ),
                    "LTP": item.get("LTP", item.get("ltp", item.get("last_price"))),
                }
            )
        return _portfolio_frame(rows)

    def fetch_positions(self, token: str) -> pd.DataFrame:
        return self._portfolio(self._get_json("positions", token))

    def fetch_holdings(self, token: str) -> pd.DataFrame:
        return self._portfolio(self._get_json("holdings", token))

    def fetch_mutual_fund_holdings(self, token: str) -> pd.DataFrame:
        return pd.DataFrame(columns=MUTUAL_FUND_COLUMNS)

    def fetch_profile(self, token: str) -> dict[str, str]:
        if "profile" not in self.endpoints:
            return {"name": self.name, "user_id": ""}
        payload = self._get_json("profile", token)
        if not isinstance(payload, dict):
            raise BrokerAPIError("Custom broker returned an invalid profile.")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise BrokerAPIError("Custom broker returned an invalid profile.")
        return {
            "name": str(data.get("name") or data.get("display_name") or ""),
            "user_id": str(data.get("user_id") or data.get("client_id") or ""),
        }

    def fetch_live_prices(
        self, token: str, tickers: list[str] | None = None
    ) -> dict[str, float]:
        symbols = sorted({symbol.strip().upper() for symbol in tickers or []})
        if not symbols:
            return {}
        payload = self._get_json(
            "live_prices", token, params={"tickers": ",".join(symbols)}
        )
        data = self._data(payload, dict)
        prices: dict[str, float] = {}
        for ticker in symbols:
            if ticker in data:
                price = _number(data[ticker], "live quote")
                if price > 0:
                    prices[ticker] = price
        return prices

    def fetch_market_risk(self, token: str, ticker: str) -> dict[str, float]:
        raise BrokerCapabilityError(
            "Custom read-only broker does not provide verified ATR candle data."
        )

    def place_order(
        self, token: str, ticker: str, qty: int, transaction_type: str
    ) -> Any:
        raise BrokerCapabilityError(
            "Custom dynamic brokers are read-only; live order placement is disabled."
        )


class BrokerFactory:
    """Create a supported broker adapter without coupling callers to constructors."""

    @overload
    @staticmethod
    def create_adapter(
        broker_name: Literal["Upstox"],
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> UpstoxAdapter: ...

    @overload
    @staticmethod
    def create_adapter(
        broker_name: Literal["Zerodha"],
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> ZerodhaAdapter: ...

    @overload
    @staticmethod
    def create_adapter(
        broker_name: Literal["Dhan"],
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        client_id: str | None = None,
    ) -> DhanAdapter: ...

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
            unexpected = set(credentials) - {"api_key", "api_secret"}
            if unexpected:
                raise ValueError("Unsupported Upstox credential field.")
            return UpstoxAdapter(
                api_key=credentials.get("api_key"),
                api_secret=credentials.get("api_secret"),
            )
        if broker_name == "Zerodha":
            unexpected = set(credentials) - {"api_key", "api_secret"}
            if unexpected:
                raise ValueError("Unsupported Zerodha credential field.")
            return ZerodhaAdapter(
                api_key=credentials.get("api_key"),
                api_secret=credentials.get("api_secret"),
            )
        if broker_name == "Dhan":
            unexpected = set(credentials) - {"api_key", "api_secret", "client_id"}
            if unexpected:
                raise ValueError("Unsupported Dhan credential field.")
            return DhanAdapter(
                api_key=credentials.get("api_key"),
                api_secret=credentials.get("api_secret"),
                client_id=credentials.get("client_id"),
            )
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

    @staticmethod
    def create_dynamic_adapter(
        configuration: dict[str, Any],
    ) -> GenericDynamicAdapter:
        """Construct a validated, read-only adapter from a tenant registry record."""
        return GenericDynamicAdapter(configuration)
