"""Deterministic forward-chaining rules for reviewing AI trade candidates."""

from __future__ import annotations

from math import floor, isfinite
from typing import Any

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


class JevRuleEngine:
    """Evaluate candidates in fixed order and retain a human-readable audit trail."""

    def __init__(self, facts: dict[str, Any]) -> None:
        self.facts = facts
        self.audit_trail: list[str] = []
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
        self.approved_trades = []
        candidates = self._validated_candidates()

        confidence_passed = [
            candidate
            for candidate in candidates
            if candidate["Confidence_Score"] >= 70
        ]
        self.audit_trail.append(
            "Rule 1 (Confidence Boundary): "
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
        if cash > 20_000:
            self.audit_trail.append(
                "Rule 2 (Capital Adequacy Check): abundant cash; equal-weight "
                f"allocation budget is INR {cash / len(confidence_passed):,.2f} per target."
            )
            allocation = cash / len(confidence_passed)
            for candidate in confidence_passed:
                quantity = floor(allocation / candidate["Entry_Price"])
                if quantity > 0:
                    self.approved_trades.append({**candidate, "Qty": quantity})
            self.audit_trail.append(
                f"Rule 2 approved {len(self.approved_trades)} affordable equal-weight allocation(s)."
            )
            return self.approved_trades.copy(), self.audit_trail.copy()

        self.audit_trail.append(
            "Rule 3 (Ordinal Triage Filter): scarce cash; prioritizing "
            "unheld sectors, then confidence, within the wallet budget."
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
                if candidate["Entry_Price"] <= cash
            ),
            None,
        )
        if selected is None:
            self.audit_trail.append(
                "Rule 3 selected no target: none of the ranked candidates fit the wallet."
            )
        else:
            quantity = floor(cash / selected["Entry_Price"])
            if quantity > 0:
                self.approved_trades = [{**selected, "Qty": quantity}]
                self.audit_trail.append(
                    f"Rule 3 approved {selected['Ticker']} ({selected.get('Sector') or 'unknown sector'}), "
                    f"confidence {selected['Confidence_Score']:.1f}%, quantity {quantity}."
                )
        return self.approved_trades.copy(), self.audit_trail.copy()
