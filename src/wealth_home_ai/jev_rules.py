"""Deterministic forward-chaining rules for reviewing AI trade candidates."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from math import floor, isfinite
from typing import Any
from zoneinfo import ZoneInfo

from .broker_factory import TICKER_MAP

SECTOR_BY_TICKER = {
    "RELIANCE": "Energy",
    "TCS": "Information Technology",
    "INFY": "Information Technology",
    "HDFCBANK": "Financials",
    "ICICIBANK": "Financials",
    "SBIN": "Financials",
    "BHARTIARTL": "Telecommunications",
    "ITC": "Consumer Staples",
    "KOTAKBANK": "Financials",
    "LT": "Industrials",
    "ETERNAL": "Consumer Discretionary",
    "TATASTEEL": "Materials",
    "IREDA": "Financials",
    "TATAMOTORS": "Consumer Discretionary",
    "PNB": "Financials",
    "ONGC": "Energy",
    "IRFC": "Financials",
    "JIOFIN": "Financials",
    "SUZLON": "Industrials",
    "YESBANK": "Financials",
    "GMRINFRA": "Industrials",
    "ALOKIND": "Consumer Discretionary",
    "JPPOWER": "Utilities",
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def buy_execution_block_reason(
    realized_daily_loss_pct: float,
    buy_lock_until: datetime | None,
    active_positions_count: int,
    now: datetime | None = None,
) -> tuple[str | None, datetime | None]:
    """Recheck time, circuit-breaker, and focus-cap rules at order submission."""
    current_time = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    else:
        current_time = current_time.astimezone(ZoneInfo("Asia/Kolkata"))

    if isinstance(buy_lock_until, datetime):
        if buy_lock_until.tzinfo is None:
            buy_lock_until = buy_lock_until.replace(
                tzinfo=ZoneInfo("Asia/Kolkata")
            )
        else:
            buy_lock_until = buy_lock_until.astimezone(ZoneInfo("Asia/Kolkata"))
        if buy_lock_until > current_time:
            return (
                f"New buys are locked until {buy_lock_until:%Y-%m-%d %H:%M} IST.",
                buy_lock_until,
            )
    else:
        buy_lock_until = None

    if realized_daily_loss_pct <= -3.0:
        lock_until = current_time + timedelta(hours=24)
        return (
            "New buys are locked for 24 hours because reported realized daily "
            "loss reached -3%.",
            lock_until,
        )
    if current_time.weekday() >= 5 or not (
        time(9, 45) <= current_time.time() <= time(15, 0)
    ):
        return (
            "Live buys are allowed only Monday–Friday, 09:45–15:00 IST.",
            None,
        )
    if active_positions_count >= 8:
        return ("New buys are blocked at the limit of eight active positions.", None)
    return None, None


class JevRuleEngine:
    """Evaluate candidates in fixed order and retain a human-readable audit trail."""

    def __init__(self, facts: dict[str, Any]) -> None:
        self.facts = facts
        self.audit_trail: list[str] = []
        self.steps: list[str] = self.audit_trail
        self.approved_trades: list[dict[str, Any]] = []

    def _existing_sectors(self) -> set[str]:
        sectors = self.facts.get("sector_holdings") or []
        if isinstance(sectors, dict):
            known_sectors = set(SECTOR_BY_TICKER.values())
            existing = set()
            for key, value in sectors.items():
                if str(key).strip() in known_sectors:
                    existing.add(str(key).strip())
                elif str(value).strip() in known_sectors:
                    existing.add(str(value).strip())
        else:
            existing = {str(sector).strip() for sector in sectors if sector}
        portfolio = self.facts.get("portfolio")
        if portfolio is not None and "Ticker" in getattr(portfolio, "columns", []):
            existing.update(
                SECTOR_BY_TICKER.get(str(ticker).upper(), "")
                for ticker in portfolio["Ticker"].dropna()
            )
            if "Sector" in portfolio.columns:
                existing.update(
                    str(sector).strip()
                    for sector in portfolio["Sector"].dropna()
                    if str(sector).strip()
                )
        existing.discard("")
        return existing

    def _validated_candidates(self) -> list[dict[str, Any]]:
        by_ticker: dict[str, dict[str, Any]] = {}
        rejected = 0
        allowed_tickers = set(TICKER_MAP)
        allowed_tickers.update(
            str(ticker).strip().upper()
            for ticker in (self.facts.get("live_prices") or {})
        )
        raw_candidates = self.facts.get("llm_targets") or []
        if not isinstance(raw_candidates, (list, tuple)):
            self.audit_trail.append(
                "Input validation: candidate list was malformed; no targets accepted."
            )
            return []
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                rejected += 1
                continue
            ticker = str(candidate.get("Ticker", "")).strip().upper()
            entry_price = _finite_number(
                candidate.get("Entry_Price", candidate.get("Target_Price"))
            )
            target_price = _finite_number(candidate.get("Target_Price"))
            score = _finite_number(candidate.get("Confidence_Score"))
            if (
                ticker not in allowed_tickers
                or entry_price is None
                or entry_price <= 0
                or target_price is None
                or target_price <= 0
                or score is None
                or not 0 <= score <= 100
            ):
                rejected += 1
                continue
            validated_candidate = {
                **candidate,
                "Ticker": ticker,
                "Entry_Price": entry_price,
                "Target_Price": target_price,
                "Confidence_Score": score,
                "Sector": SECTOR_BY_TICKER.get(ticker, candidate.get("Sector")),
            }
            previous = by_ticker.get(ticker)
            if previous is None or score > previous["Confidence_Score"]:
                by_ticker[ticker] = validated_candidate
            if previous is not None:
                rejected += 1
        if rejected:
            self.audit_trail.append(
                f"Input validation: rejected {rejected} malformed, unsupported, "
                "or duplicate target(s)."
            )
        return list(by_ticker.values())

    def run(self) -> tuple[list[dict[str, Any]], list[str]]:
        self.audit_trail = ["Starting deterministic forward-chaining evaluation."]
        self.steps = self.audit_trail
        self.approved_trades = []
        candidates = self._validated_candidates()

        now = self.facts.get("now")
        if not isinstance(now, datetime):
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
        elif now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        else:
            now = now.astimezone(ZoneInfo("Asia/Kolkata"))

        loss_pct = _finite_number(
            self.facts.get("realized_daily_loss_pct", 0.0)
        )
        loss_pct = loss_pct if loss_pct is not None else 0.0
        buy_lock_until = self.facts.get("buy_lock_until")
        if isinstance(buy_lock_until, datetime):
            if buy_lock_until.tzinfo is None:
                buy_lock_until = buy_lock_until.replace(
                    tzinfo=ZoneInfo("Asia/Kolkata")
                )
            else:
                buy_lock_until = buy_lock_until.astimezone(
                    ZoneInfo("Asia/Kolkata")
                )
        else:
            buy_lock_until = None
        if loss_pct <= -3:
            if buy_lock_until is None or buy_lock_until <= now:
                buy_lock_until = now + timedelta(hours=24)
            self.facts["buy_lock_until"] = buy_lock_until
            self.audit_trail.append(
                "Rule 1 — Rule A (Portfolio circuit breaker): realized daily loss is "
                f"{loss_pct:.2f}% (at or below -3%); BUY execution is locked "
                f"until {buy_lock_until.isoformat()}."
            )
            self.audit_trail.append(
                "Evaluation stopped: the portfolio circuit breaker blocks new buys."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()
        if buy_lock_until is not None and buy_lock_until > now:
            self.facts["buy_lock_until"] = buy_lock_until
            self.audit_trail.append(
                "Rule 1 — Rule A (Portfolio circuit breaker): existing BUY lock remains "
                f"active until {buy_lock_until.isoformat()}."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()
        self.facts["buy_lock_until"] = None
        self.audit_trail.append(
            "Rule 1 — Rule A (Portfolio circuit breaker): no active daily-loss lock."
        )

        trading_open = (
            now.weekday() < 5
            and time(9, 45) <= now.time() <= time(15, 0)
        )
        if not trading_open:
            self.audit_trail.append(
                "Rule 2 — Rule B (Trading window): closed; new trades are allowed "
                "only Monday–Friday, 09:45–15:00 Asia/Kolkata."
            )
            self.audit_trail.append(
                "Evaluation stopped: outside the configured trading window."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()
        self.audit_trail.append(
            "Rule 2 — Rule B (Trading window): current IST time is inside the "
            "Monday–Friday 09:45–15:00 window."
        )

        portfolio = self.facts.get("portfolio")
        supplied_count = self.facts.get("active_positions_count")
        if supplied_count is None and portfolio is not None:
            try:
                active_positions = portfolio.loc[portfolio["Qty"].ne(0)]
                supplied_count = len(active_positions)
            except (AttributeError, KeyError, TypeError):
                supplied_count = 0
        position_count = _finite_number(supplied_count)
        position_count = max(int(position_count or 0), 0)
        if position_count >= 8:
            self.audit_trail.append(
                "Rule 3 — Rule C (Position limit): "
                f"{position_count} active positions meet/exceed the limit of 8; "
                "new trades are blocked."
            )
            self.audit_trail.append(
                "Evaluation stopped: the active-position focus cap is reached."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()
        self.audit_trail.append(
            f"Rule 3 — Rule C (Position limit): {position_count} of 8 active positions."
        )

        confidence_passed = [
            candidate
            for candidate in candidates
            if candidate["Confidence_Score"] >= 70
        ]
        self.audit_trail.append(
            "Rule 4 — Rule D (Confidence threshold): "
            f"{len(confidence_passed)} of {len(candidates)} valid target(s) "
            "passed the 70% minimum."
        )
        if not confidence_passed:
            self.audit_trail.append("Evaluation stopped: no high-confidence candidates.")
            return self.approved_trades, self.audit_trail.copy()

        cash = _finite_number(
            self.facts.get("cash_balance", self.facts.get("available_cash", 0))
        )
        cash = max(cash or 0.0, 0.0)
        limits = self.facts.get("user_allocation_limits") or {}
        cap_value = (
            limits.get("max_allocation_pct", 100.0)
            if isinstance(limits, dict)
            else self.facts.get("max_allocation_pct", 100.0)
        )
        allocation_cap_pct = _finite_number(
            self.facts.get("max_allocation_pct", cap_value)
        )
        if allocation_cap_pct is None or not 0 <= allocation_cap_pct <= 100:
            self.audit_trail.append(
                "Rule 5 — Rule E/F (Allocation limit): invalid allocation cap; no trades "
                "were approved."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()
        allocatable_cash = cash * allocation_cap_pct / 100
        if cash > 20_000:
            self.audit_trail.append(
                "Rule 5 — Rule E (Capital allocation): sufficient cash; equal-weight "
                f"allocation uses {allocation_cap_pct:.1f}% cap "
                f"(INR {allocatable_cash:,.2f} total)."
            )
            allocation = allocatable_cash / len(confidence_passed)
            for candidate in confidence_passed:
                quantity = floor(allocation / candidate["Entry_Price"])
                if quantity > 0 and (
                    quantity * candidate["Entry_Price"] <= allocatable_cash
                ):
                    self.approved_trades.append({**candidate, "Qty": quantity})
            self.audit_trail.append(
                f"Rule 5 approved {len(self.approved_trades)} affordable "
                "equal-weight allocation(s) within the cap."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()

        self.audit_trail.append(
            "Rule 6 — Rule F (Limited-cash prioritization): scarce cash; prioritizing "
            "unheld sectors, then confidence, within the capped wallet budget."
        )
        held_sectors = self._existing_sectors()
        ranked = sorted(
            confidence_passed,
            key=lambda candidate: (
                0
                if candidate.get("Sector")
                and candidate["Sector"] not in held_sectors
                else 1,
                -candidate["Confidence_Score"],
                candidate["Ticker"],
            ),
        )
        selected = next(
            (
                candidate
                for candidate in ranked
                if candidate["Entry_Price"] <= allocatable_cash
            ),
            None,
        )
        if selected is None:
            self.audit_trail.append(
                "Rule 6 selected no target: none of the ranked candidates fit the "
                "allocation cap and wallet budget."
            )
        else:
            quantity = floor(allocatable_cash / selected["Entry_Price"])
            if quantity > 0:
                self.approved_trades = [{**selected, "Qty": quantity}]
                self.audit_trail.append(
                    f"Rule 6 approved {selected['Ticker']} ({selected.get('Sector') or 'unknown sector'}), "
                    f"confidence {selected['Confidence_Score']:.1f}%, quantity {quantity}."
                )
        return self.approved_trades.copy(), self.audit_trail.copy()
