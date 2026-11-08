"""Typed stock context and deterministic analysis gate.

Ownership and dividend yields are fractions; fields named ``*_pct`` in
intraday, futures and return data are percentage points. The promoter pledge
and short-interest fields are fractions despite their names.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from core.logging import logger


class InvestmentHorizon(str, Enum):
    LONG_TERM_EQUITY = "LONG_TERM_EQUITY"
    SHORT_TERM_TRADING = "SHORT_TERM_TRADING"
    INTRADAY_TRADING = "INTRADAY_TRADING"


Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0)]
Positive = Annotated[float, Field(strict=True, allow_inf_nan=False, gt=0)]
Fraction = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=1)]
Percentile = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=100)]
Bias = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-1, le=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
Integer = Annotated[int, Field(strict=True)]


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, revalidate_instances="always")


class CompanyProfile(ContextModel):
    official_name: str | None = None
    ticker: str
    isin: str | None = None
    sector: str | None = None
    industry: str | None = None
    full_summary: str | None = None
    country: str | None = None
    full_time_employees: Count | None = None

    @model_validator(mode="after")
    def valid_ticker(self) -> CompanyProfile:
        if not self.ticker.strip():
            raise ValueError("ticker must not be blank")
        return self


class FundamentalRatios(ContextModel):
    market_cap: Positive | None = None
    float_market_cap: NonNegative | None = None
    trailing_pe: Number | None = None
    forward_pe: Number | None = None
    peg_ratio: Number | None = None
    price_to_book: Number | None = None
    price_to_sales: Number | None = None
    enterprise_value_to_ebitda: Number | None = None
    gross_margin: Number | None = None
    operating_margin: Number | None = None
    net_margin: Number | None = None
    roe: Number | None = None
    roa: Number | None = None
    debt_to_equity: Number | None = None
    free_cash_flow: Number | None = None
    dividend_yield: Fraction | None = None
    shares_outstanding: Count | None = None
    piotroski_f_score: Annotated[int, Field(strict=True, ge=0, le=9)] | None = None
    altman_z_score: Number | None = None
    altman_model_applicable: StrictBool | None = None


class TechnicalIndicators(ContextModel):
    sma_20: Optional[Positive] = None
    sma_50: Optional[Positive] = None
    sma_200: Optional[Positive] = None
    ema_20: Optional[Positive] = None
    ema_50: Optional[Positive] = None
    ema_200: Optional[Positive] = None
    rsi_14: Optional[Percentile] = None
    macd_line: Optional[Number] = None
    macd_signal: Optional[Number] = None
    macd_hist: Optional[Number] = None
    bb_upper: Optional[Number] = None
    bb_lower: Optional[Number] = None
    bb_middle: Optional[Number] = None
    adx_14: Optional[Percentile] = None
    current_volume: Count | None = None
    avg_volume_3m: Count | None = None
    money_flow_index_14: Optional[Percentile] = None
    atr_percentile_14: Optional[Percentile] = None
    chop_index_14: Optional[Percentile] = None
    squeeze_alert: StrictBool | None = None
    volume_price_trend: Optional[Number] = None
    historical_volatility_30: Optional[NonNegative] = None


class ShareholderDistributionMetrics(ContextModel):
    pct_held_by_institutions: Fraction | None = None
    pct_held_by_insiders: Fraction | None = None
    pct_held_by_retail: Fraction | None = None
    total_institutional_holders: Count | None = None
    net_shares_bought_quarterly: Integer | None = None
    top_10_holders_trend: Literal["Accumulation", "Distribution", "Neutral"] | None = None
    institutional_block_trade_bias: Bias | None = None
    short_interest_pct_float: NonNegative | None = None
    insider_transactions_bias: Number | None = None
    promoter_pledged_shares_pct: Fraction | None = None
    promoter_pledge_verified: StrictBool | None = None

    @model_validator(mode="after")
    def check_allocation(self) -> ShareholderDistributionMetrics:
        allocations = (self.pct_held_by_institutions, self.pct_held_by_insiders, self.pct_held_by_retail)
        if all(value is not None for value in allocations) and abs(sum(allocations) - 1) > 0.0200000001:
            raise ValueError("ownership allocations must total 100% within 2 percentage points")
        return self


class IntradayMicrostructureMetrics(ContextModel):
    vwap: Positive | None = None
    distance_from_vwap_pct: Number | None = None
    opening_range_breakout: Literal["ORB_BULLISH", "ORB_BEARISH", "INSIDE_BAR", "NONE"] | None = None
    time_of_day_segment: Literal["MORNING_RUSH", "MIDDAY_LULL", "POWER_HOUR"] | None = None
    relative_volume_intraday: NonNegative | None = None
    bid_ask_spread: NonNegative | None = None
    upper_circuit_limit: Positive | None = None
    lower_circuit_limit: Positive | None = None
    distance_to_lower_circuit_pct: Number | None = None

    @model_validator(mode="after")
    def valid_circuit_limits(self) -> IntradayMicrostructureMetrics:
        if (self.upper_circuit_limit is not None and self.lower_circuit_limit is not None
                and self.lower_circuit_limit > self.upper_circuit_limit):
            raise ValueError("lower circuit limit cannot exceed upper circuit limit")
        return self


class GrowthAndEarningsQuality(ContextModel):
    revenue_growth_yoy: Number | None = None
    earnings_growth_yoy: Number | None = None
    earnings_quality_score: Number | None = None


class OptionsChainMetrics(ContextModel):
    put_call_volume_ratio: NonNegative | None = None
    put_call_oi_ratio: NonNegative | None = None
    implied_volatility_percentile: Percentile | None = None
    implied_move_percentage: NonNegative | None = None
    gamma_exposure_bias: Literal["NET_LONG_GAMMA", "NET_SHORT_GAMMA", "NEUTRAL"] | None = None
    unusual_options_activity: StrictBool | None = None


class FuturesMarketMetrics(ContextModel):
    front_month_premium_pct: Number | None = None
    open_interest_change_pct: Number | None = None
    term_structure_regime: Literal["CONTANGO", "BACKWARDATION"] | None = None
    commercial_net_position: Number | None = None


class AnalystConsensus(ContextModel):
    consensus_rating: str | None = None
    mean_price_target: NonNegative | None = None
    high_price_target: NonNegative | None = None
    low_price_target: NonNegative | None = None
    upside_percentage: Number | None = None
    earnings_surprise_pct_q4: Number | None = None


class MacroContext(ContextModel):
    sector_return_5d: Number | None = None
    sector_return_30d: Number | None = None
    beta_to_sp500: Number | None = None
    market_regime_bias: Literal["Risk-On", "Risk-Off", "Neutral"] | None = None


class NewsHeadline(ContextModel):
    headline: str | None = None
    timestamp: datetime | None = None
    source: str | None = None
    url: str | None = None
    raw_sentiment_score: Number | None = None


class NewsHeadlines(ContextModel):
    headlines: list[NewsHeadline] = Field(default_factory=list)
    upcoming_corporate_action_event: Literal["SPLIT", "BONUS", "DIVIDEND_EX", "NONE"] | None = None


class StockSignalContext(ContextModel):
    horizon: InvestmentHorizon
    current_price: Positive | None = None
    profile: CompanyProfile
    fundamentals: FundamentalRatios = Field(default_factory=FundamentalRatios)
    technicals: TechnicalIndicators = Field(default_factory=TechnicalIndicators)
    shareholders: ShareholderDistributionMetrics = Field(default_factory=ShareholderDistributionMetrics)
    intraday: IntradayMicrostructureMetrics | None = None
    growth: GrowthAndEarningsQuality = Field(default_factory=GrowthAndEarningsQuality)
    options: OptionsChainMetrics = Field(default_factory=OptionsChainMetrics)
    futures: FuturesMarketMetrics | None = None
    analyst: AnalystConsensus = Field(default_factory=AnalystConsensus)
    macro: MacroContext = Field(default_factory=MacroContext)
    news: NewsHeadlines = Field(default_factory=NewsHeadlines)
    source_data: dict[str, Any] = Field(default_factory=dict)
    data_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_context(self) -> StockSignalContext:
        if self.horizon == InvestmentHorizon.INTRADAY_TRADING:
            required = ("vwap", "distance_from_vwap_pct", "opening_range_breakout",
                        "time_of_day_segment", "relative_volume_intraday", "bid_ask_spread")
            if self.intraday is None or any(getattr(self.intraday, name) is None for name in required):
                raise ValueError("intraday horizon requires all six live microstructure metrics")
        try:
            json.dumps(self.source_data, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("source_data must contain finite JSON values") from exc
        return self


class LocalGatekeeperResponse(ContextModel):
    is_eligible_for_llm: bool
    local_warnings: list[str]
    technical_divergence_detected: bool
    macro_risk_penalty: NonNegative


class TradeSignalResponse(ContextModel):
    recommendation: Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
    confidence_score: Fraction
    structural_rationale: str


def local_gatekeeper(context: StockSignalContext) -> LocalGatekeeperResponse:
    """Block on missing critical risk data; warn when divergence cannot be assessed."""
    context = StockSignalContext.model_validate(context)
    f, s, t = context.fundamentals, context.shareholders, context.technicals
    warnings: list[str] = []
    eligible = True
    penalty = 0.0
    divergence = False

    def block(reason: str) -> None:
        nonlocal eligible
        eligible = False
        warnings.append(reason)

    if f.altman_model_applicable is not True:
        block("Public-manufacturing Altman model applicability is not independently confirmed.")
    if f.altman_z_score is None:
        block("Altman Z-score unavailable; insolvency risk cannot be assessed.")
    elif f.altman_z_score < 1.81:
        block("Altman Z-score below 1.81: insolvency risk.")

    if s.promoter_pledged_shares_pct is None or s.promoter_pledge_verified is False:
        block("Promoter pledged shares unavailable; capital risk cannot be assessed.")
    elif s.promoter_pledged_shares_pct > 0.50:
        block("Promoter pledged shares exceed 50%: critical capital risk.")
    elif s.promoter_pledged_shares_pct > 0.25:
        warnings.append("Promoter pledged shares exceed 25%: critical capital risk.")

    if f.market_cap is None or f.float_market_cap is None:
        block("Market capitalization or float capitalization unavailable; liquidity cannot be assessed.")
    elif f.float_market_cap > f.market_cap:
        block("Float capitalization exceeds market capitalization; liquidity data is inconsistent.")
    elif f.float_market_cap / f.market_cap < 0.10:
        block("Public float below 10%: illiquidity and slippage risk.")

    if f.dividend_yield is None or f.free_cash_flow is None:
        block("Dividend yield or free cash flow unavailable; payout strain cannot be assessed.")
    elif f.dividend_yield > 0.05 and f.free_cash_flow < 0:
        warnings.append("High dividend yield with negative free cash flow: payout capital strain.")
        penalty = 0.1  # Fixed penalty, in the same 0–1 scale as confidence.

    if context.intraday is not None:
        distance = context.intraday.distance_to_lower_circuit_pct
        if (context.horizon == InvestmentHorizon.INTRADAY_TRADING
                and context.current_price is not None and context.intraday.lower_circuit_limit is not None):
            computed = (context.current_price - context.intraday.lower_circuit_limit) / context.intraday.lower_circuit_limit * 100
            distance = computed if distance is None else min(distance, computed)
        if distance is None:
            warnings.append("Lower circuit proximity unavailable; circuit risk cannot be assessed.")
        elif distance < 1.5:
            warning = "Critical circuit risk: price is less than 1.5% above the lower circuit limit."
            logger.critical("%s", warning)
            block(warning)

    if context.news.upcoming_corporate_action_event in ("SPLIT", "BONUS"):
        warning = "Technical divergence check skipped due to an upcoming split or bonus."
        logger.warning("%s", warning)
        warnings.append(warning)
    elif context.current_price is None or t.sma_200 is None or t.rsi_14 is None:
        warning = "Price, SMA 200 or RSI unavailable; technical divergence check skipped."
        logger.warning("%s", warning)
        warnings.append(warning)
    elif context.current_price < t.sma_200 and t.rsi_14 > 70:
        divergence = True
        warnings.append("Price below SMA 200 with RSI above 70: technical divergence.")
    if context.news.upcoming_corporate_action_event is None:
        warnings.append("Corporate action status unknown; technical divergence may be affected.")

    return LocalGatekeeperResponse(
        is_eligible_for_llm=eligible, local_warnings=warnings,
        technical_divergence_detected=divergence, macro_risk_penalty=penalty,
    )


def run_triggered_analysis(
    context: StockSignalContext, *, triggered: bool,
    llm_callable: Callable[[str], Any],
) -> TradeSignalResponse:
    """Revalidate and gate a fresh snapshot before invoking an explicitly triggered LLM."""
    if triggered is not True:
        raise ValueError("Analysis requires an explicit user trigger.")
    snapshot = StockSignalContext.model_validate(context.model_dump(mode="python"))
    if snapshot.horizon == InvestmentHorizon.INTRADAY_TRADING:
        timestamps = snapshot.source_data.get("upstox_stream_timestamp")
        traded_at = timestamps.get("traded_at") if isinstance(timestamps, dict) else None
        try:
            if not isinstance(traded_at, str):
                raise ValueError("missing timestamp")
            traded = datetime.fromisoformat(traded_at.replace("Z", "+00:00"))
            if traded.tzinfo is None or traded.utcoffset() is None:
                raise ValueError("timestamp must be timezone-aware")
            age = (datetime.now(timezone.utc) - traded).total_seconds()
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Intraday analysis requires a valid timezone-aware traded_at timestamp.") from exc
        if age < 0 or age > 30:
            raise ValueError("Intraday market tick is stale or future-dated; rebuild the context.")
    gate = local_gatekeeper(snapshot)
    if not gate.is_eligible_for_llm:
        raise ValueError("Analysis blocked by local gatekeeper: " + "; ".join(gate.local_warnings))
    payload = snapshot.model_dump(mode="json")
    payload["local_gatekeeper"] = gate.model_dump(mode="json")
    result = llm_callable(json.dumps(payload, allow_nan=False))
    if isinstance(result, (str, bytes, bytearray)):
        return TradeSignalResponse.model_validate_json(result)
    return TradeSignalResponse.model_validate(result)
