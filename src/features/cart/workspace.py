"""Compact, session-local stock cart with review-only checkout."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import streamlit as st

from features.equities.scan_table import render_cart_stock_details
from features.cart.row import ROW_COMPONENT


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _cart() -> list[dict[str, Any]]:
    entries = st.session_state.setdefault("stock_cart", [])
    unique = []
    seen = set()
    for entry in entries:
        item_id = entry["id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        item = dict(entry)
        item["quantity"] = _number(item.get("quantity", 1), 1)
        item["holding_quantity"] = max(0.0, _number(item.get("holding_quantity")))
        unique.append(item)
    st.session_state["stock_cart"] = unique
    return unique


def _key(item_id: str, suffix: str) -> str:
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return f"stock_cart_{digest}_{suffix}"


def _remove(item_id: str) -> None:
    st.session_state["stock_cart"] = [
        item for item in st.session_state["stock_cart"] if item["id"] != item_id
    ]
    st.session_state["stock_cart_review"] = None
    st.session_state.pop(_key(item_id, "error"), None)
    if st.session_state.get("stock_cart_detail") == item_id:
        st.session_state["stock_cart_detail"] = None


def _toggle_detail(item_id: str) -> None:
    current = st.session_state.get("stock_cart_detail")
    st.session_state["stock_cart_detail"] = None if current == item_id else item_id


def _estimate(item: dict[str, Any]) -> tuple[float | None, float | None]:
    snapshot = item.get("stock_detail")
    row = snapshot.get("row") if isinstance(snapshot, dict) else None
    price = _number(row.get("price")) if isinstance(row, dict) else 0.0
    if price <= 0:
        return None, None
    amount = abs(item["quantity"]) * price
    return price, amount if math.isfinite(amount) else None


def _estimate_text(item: dict[str, Any]) -> str:
    price, amount = _estimate(item)
    if price is None or amount is None:
        return "LTP or estimated amount unavailable"
    quantity = item["quantity"]
    currency = _quote_currency(item)
    prefix = "₹" if currency == "INR" else f"{currency} " if currency else ""
    suffix = " (currency unknown)" if currency is None else ""
    return f"Estimated: {abs(quantity):g} × LTP {prefix}{price:,.2f} = {prefix}{amount:,.2f}{suffix}"


def _quote_currency(item: dict[str, Any]) -> str | None:
    snapshot = item.get("stock_detail")
    row = snapshot.get("row") if isinstance(snapshot, dict) else None
    currency = row.get("currency") if isinstance(row, dict) else None
    if isinstance(currency, str) and currency.strip():
        return currency.strip().upper()
    ticker = str(item.get("ticker") or "").upper()
    return "INR" if ticker.endswith((".NS", ".BO")) else None


def _review_text(item: dict[str, Any]) -> str:
    quantity = item["quantity"]
    action = "Buy" if quantity > 0 else "Sell" if quantity < 0 else "No action"
    text = f"{item['ticker']} · {action} {abs(quantity):g} · Signal: {item.get('signal', 'NOT RATED')}"
    if quantity < 0 and item["holding_quantity"] > 0:
        text += f" · {abs(quantity) / item['holding_quantity'] * 100:g}% of holding"
    return f"{text} · {_estimate_text(item)}"


def _valid_quantity(item: dict[str, Any], quantity: object) -> bool:
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
        return False
    try:
        value = float(quantity)
    except OverflowError:
        return False
    if not math.isfinite(value) or not value.is_integer() or abs(value) > 9007199254740991:
        return False
    held = _number(item.get("holding_quantity"))
    return held <= 0 or value >= -math.floor(held)


def _row_event(item_id: str, key: str) -> None:
    event = st.session_state[key].get("event")
    if not isinstance(event, dict):
        st.warning("Unable to apply this cart action.")
        return
    item = next((entry for entry in st.session_state["stock_cart"] if entry["id"] == item_id), None)
    if item is None:
        st.warning("This stock is no longer in the cart.")
        return
    action = event.get("action")
    if action == "quantity":
        quantity = event.get("quantity")
        if not _valid_quantity(item, quantity):
            st.warning("Enter whole shares and do not sell more than the holding.")
            return
        item["quantity"] = quantity
        st.session_state["stock_cart_review"] = None
    elif action == "view":
        _toggle_detail(item_id)
    elif action == "remove":
        _remove(item_id)
    elif action == "review":
        if not item["quantity"] or not _valid_quantity(item, item["quantity"]):
            st.warning("Choose a valid, nonzero quantity before checkout.")
            return
        st.session_state["stock_cart_review"] = item_id
    else:
        st.warning("Unknown cart action.")


def _display_name(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("ticker") or "")
    parts = name.split(" · ", 1)
    return parts[0] if len(parts) == 2 and parts[0].strip().casefold() == parts[1].strip().casefold() else name


def render_cart_workspace() -> None:
    items = _cart()

    if not items:
        st.session_state["stock_cart_review"] = None
        st.info("Your cart is empty. Add stocks from a holdings or scanner table.",
                icon=":material/add_shopping_cart:")
        st.button("Check out", type="primary", disabled=True)
        return

    for item in items:
        item_id = item["id"]
        key = _key(item_id, "row")
        ROW_COMPONENT(
            key=key,
            data={"name": _display_name(item), "signal": str(item.get("signal") or "NOT RATED").upper(),
                  "quantity": item["quantity"], "held": item["holding_quantity"],
                  "estimate": _estimate_text(item),
                  "estimateHelp": "Saved table price may be delayed; fees and taxes excluded. Checkout is review only."},
            on_event_change=lambda item_id=item_id, key=key: _row_event(item_id, key),
        )
        if item["quantity"] and not _valid_quantity(item, item["quantity"]):
            st.warning(f"{item['ticker']}: adjust to whole shares within your holding before checkout.")
        if st.session_state.get("stock_cart_detail") == item_id:
            render_cart_stock_details(item)

    active = [item for item in items if item["quantity"]]
    if active:
        missing = sum(_estimate(item)[1] is None for item in active)
        currencies = {_quote_currency(item) for item in active}
        if missing:
            st.caption(f"Cart totals unavailable: {missing} {'stock has' if missing == 1 else 'stocks have'} no usable saved LTP.")
        elif None in currencies or len(currencies) != 1:
            st.caption("Cart totals unavailable: quote currencies are unknown or differ; no currency conversion is applied.")
        else:
            currency = next(iter(currencies))
            buys = sum(_estimate(item)[1] for item in active if item["quantity"] > 0)
            sells = sum(_estimate(item)[1] for item in active if item["quantity"] < 0)
            symbol = "₹" if currency == "INR" else f"{currency} "
            st.caption(f"Estimated buy: {symbol}{buys:,.2f} · "
                       f"Estimated sell: {symbol}{sells:,.2f} · "
                       f"Net cash needed: {symbol}{buys - sells:,.2f}")

    can_review = any(i["quantity"] for i in items) and all(
        _valid_quantity(i, i["quantity"]) for i in items
    )
    if st.button("Check out all", type="primary", disabled=not can_review):
        st.session_state["stock_cart_review"] = "__all__"
    review = st.session_state.get("stock_cart_review")
    if review:
        st.info("Review only: checkout does not place orders or send anything to your broker.")
        for item in items:
            if item["quantity"] and (review == "__all__" or review == item["id"]):
                st.text(_review_text(item))
