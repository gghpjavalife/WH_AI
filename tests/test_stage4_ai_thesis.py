from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

import pytest
from google.genai import types as gemini_types

from services.stage4_ai_thesis import (
    DEFAULT_MODEL,
    JevFirewallError,
    TradeSignalResponse,
    build_governed_ai_payload,
    call_gemini_structured_thesis,
    generate_governed_ai_thesis_cached,
    sanitize_trade_signal,
)


NOW = 1_700_000_000.0
KEY = "NSE_EQ|INE002A01018"


def _context(**kwargs):
    return build_governed_ai_payload(
        instrument_key=KEY,
        symbol="RELIANCE",
        exchange="NSE",
        stage0_macro={"^NSEI": {"close": 22_000}},
        stage1_quote={
            "last_price": 2500,
            "timestamp": NOW - 10,
            "is_circuit_locked": False,
            "vwap": 2490,
        },
        stage2_decision={
            "passed": True,
            "signal_tier": "BUY",
            "score": 0.8,
            "features": {"rsi_14": 56},
        },
        now_epoch=NOW,
        **kwargs,
    )


def _signal(**kwargs):
    values = {
        "symbol": "RELIANCE",
        "stance": "BUY",
        "confidence_score": 0.9,
        "time_horizon": "SWING_1_5D",
        "entry_price": 2500,
        "stop_loss": 2400,
        "target_price": 2700,
        "executive_thesis": "  A concise thesis.  ",
    }
    values.update(kwargs)
    return TradeSignalResponse(**values)


def test_firewall_blocks_missing_quote_circuit_lock_gate_rejection_and_default_staleness():
    base = {
        "instrument_key": KEY,
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "stage0_macro": {},
        "stage2_decision": {"passed": True},
        "now_epoch": NOW,
    }
    with pytest.raises(JevFirewallError, match="Missing valid Stage 1"):
        build_governed_ai_payload(stage1_quote=None, **base)
    with pytest.raises(JevFirewallError, match="circuit-locked"):
        build_governed_ai_payload(
            stage1_quote={
                "last_price": 2500,
                "timestamp": NOW,
                "is_circuit_locked": True,
            },
            **base,
        )
    with pytest.raises(JevFirewallError, match="Gatekeeper"):
        build_governed_ai_payload(
            stage1_quote={"last_price": 2500, "timestamp": NOW},
            stage2_decision={"passed": False, "reject_reasons": ["LOW_VOLUME"]},
            **{key: value for key, value in base.items() if key != "stage2_decision"},
        )
    with pytest.raises(JevFirewallError, match="stale"):
        build_governed_ai_payload(
            stage1_quote={"last_price": 2500, "timestamp": NOW - 901},
            **base,
        )
    stale_context = build_governed_ai_payload(
        stage1_quote={"last_price": 2500, "timestamp": NOW - 2044},
        allow_stale_quote=True,
        **base,
    )
    assert stale_context.data_staleness_seconds["stage1_quote_age_s"] == 2044
    assert any("STAGE1_QUOTE_STALE" in item for item in stale_context.missing_data_caveats)
    assert sanitize_trade_signal(_signal(), stale_context).confidence_score == 0.65
    with pytest.raises(JevFirewallError, match="future"):
        build_governed_ai_payload(
            stage1_quote={"last_price": 2500, "timestamp": NOW + 1},
            **base,
        )


def test_payload_audits_ages_caveats_and_compacts_optional_fields():
    context = build_governed_ai_payload(
        instrument_key=KEY,
        symbol="RELIANCE",
        exchange="NSE",
        stage0_macro={"^NSEI": {"close": 22_000}},
        stage1_quote={
            "last_price": 2500,
            "timestamp": NOW - 15,
            "unused_large_field": "not passed through",
        },
        stage2_decision={"passed": True, "features": {"rsi_14": 54}},
        stage3_enrichment={
            "fetched_at": NOW - 25,
            "derivatives": {"status": "NOT_FNO"},
            "news_and_consensus": {"status": "UNAVAILABLE"},
        },
        now_epoch=NOW,
        stage0_macro_fetched_at=NOW - 30,
    )
    assert context.data_staleness_seconds == {
        "stage0_macro_age_s": 30,
        "stage1_quote_age_s": 15,
        "stage3_enrichment_age_s": 25,
    }
    assert context.missing_data_caveats == [
        "FUNDAMENTALS_PENDING_OR_UNAVAILABLE",
        "DERIVATIVES_UNAVAILABLE (NOT_FNO)",
        "NEWS_CONSENSUS_UNAVAILABLE (UNAVAILABLE)",
    ]
    assert "unused_large_field" not in context.live_quote
    assert context.model_dump_json(exclude_none=True)


def test_payload_includes_compact_final_screened_candidate_cohort():
    context = build_governed_ai_payload(
        instrument_key=KEY,
        symbol="RELIANCE",
        exchange="NSE",
        stage0_macro={},
        stage1_quote={"last_price": 2500, "timestamp": NOW},
        stage2_decision={"passed": True, "features": {"rsi_14": 56}},
        screened_candidates=[
            {
                "symbol": "RELIANCE",
                "gatekeeper_score": 82,
                "last_price": 2500,
            },
            {
                "symbol": "TCS",
                "gatekeeper_score": 91,
                "last_price": 3500,
            },
        ],
        now_epoch=NOW,
    )

    assert [item["symbol"] for item in context.screened_candidates] == [
        "RELIANCE",
        "TCS",
    ]
    assert "screened_candidates" in context.model_dump_json(exclude_none=True)


def test_firewall_rejects_mismatched_instrument_and_stale_enrichment():
    with pytest.raises(JevFirewallError, match="valid equity instrument key"):
        build_governed_ai_payload(
            instrument_key="NSE_FO|INE002A01018",
            symbol="RELIANCE",
            exchange="NSE",
            stage0_macro={},
            stage1_quote={"last_price": 2500, "timestamp": NOW},
            stage2_decision={"passed": True},
            now_epoch=NOW,
        )
    with pytest.raises(JevFirewallError, match="Stage 3 enrichment is stale"):
        build_governed_ai_payload(
            instrument_key=KEY,
            symbol="RELIANCE",
            exchange="NSE",
            stage0_macro={},
            stage1_quote={"last_price": 2500, "timestamp": NOW},
            stage2_decision={"passed": True},
            stage3_enrichment={"fetched_at": NOW - 60},
            now_epoch=NOW,
            max_enrichment_age_seconds=30,
        )


def test_sanitizer_clamps_confidence_and_enforces_bullish_and_bearish_geometry():
    context = _context()
    bullish = sanitize_trade_signal(
        _signal(confidence_score=1.5, stop_loss=2510, target_price=2490),
        context,
    )
    assert bullish.confidence_score == 0.65
    assert bullish.stop_loss is None
    assert bullish.target_price is None
    assert bullish.entry_price == 2500
    assert "FUNDAMENTALS_PENDING_OR_UNAVAILABLE" in bullish.data_gap_warnings
    assert "INVALID_BULLISH_STOP_LOSS_REMOVED" in bullish.data_gap_warnings
    assert "INVALID_BULLISH_TARGET_REMOVED" in bullish.data_gap_warnings

    bearish = sanitize_trade_signal(
        _signal(stance="SELL", stop_loss=2400, target_price=2600),
        context,
    )
    assert bearish.stop_loss is None
    assert bearish.target_price is None
    assert bearish.risk_reward_ratio is None
    assert "INVALID_BEARISH_STOP_LOSS_REMOVED" in bearish.data_gap_warnings
    assert "INVALID_BEARISH_TARGET_REMOVED" in bearish.data_gap_warnings


def test_sanitizer_calculates_directional_risk_reward_and_rejects_wrong_symbol():
    context = _context()
    bullish = sanitize_trade_signal(_signal(), context)
    assert bullish.risk_reward_ratio == 2.0
    with pytest.raises(ValueError, match="different stock symbol"):
        sanitize_trade_signal(_signal(symbol="OTHER"), context)


def test_gemini_call_uses_schema_and_sanitizes_response():
    context = _context()
    response = _signal(confidence_score=1.2).model_dump_json()
    request_models = []

    def generate_content(self, **kwargs):
        request_models.append(kwargs["model"])
        return type("Response", (), {"text": response})()

    client = type(
        "Client",
        (),
        {
            "models": type(
                "Models",
                (),
                {"generate_content": generate_content},
            )()
        },
    )()
    with (
        patch("services.stage4_ai_thesis.genai", type("Genai", (), {"Client": lambda **_: client})),
        patch(
            "services.stage4_ai_thesis.types",
            type(
                "Types",
                (),
                {
                    "GenerateContentConfig": lambda **kwargs: kwargs,
                    "Schema": lambda **kwargs: kwargs,
                    "Type": type(
                        "Type",
                        (),
                        {
                            "STRING": "STRING",
                            "NUMBER": "NUMBER",
                            "ARRAY": "ARRAY",
                            "OBJECT": "OBJECT",
                        },
                    ),
                },
            ),
        ),
    ):
        result = call_gemini_structured_thesis(context, "test-key")
    assert result.confidence_score == 0.65
    assert DEFAULT_MODEL == "gemini-3.8-flash"
    assert request_models == ["gemini-3.8-flash"]


def test_gemini_response_schema_uses_supported_schema_fields():
    from services.stage4_ai_thesis import _gemini_response_schema

    schema = _gemini_response_schema(gemini_types)
    serialized = schema.model_dump(by_alias=True, exclude_none=True)
    assert serialized["type"] == "OBJECT"
    assert serialized["properties"]["stance"]["enum"] == [
        "STRONG_BUY",
        "BUY",
        "HOLD",
        "AVOID",
        "SELL",
    ]
    assert "additionalProperties" not in str(serialized)


def test_cached_entrypoint_reuses_context_model_and_account_scoped_result(tmp_path):
    context = _context()
    signal = _signal().model_dump()
    with patch(
        "services.stage4_ai_thesis.call_gemini_structured_thesis",
        return_value=TradeSignalResponse(**signal),
    ) as call:
        first = generate_governed_ai_thesis_cached(
            context, "key-one", db_path=tmp_path / "thesis.sqlite3"
        )
        second = generate_governed_ai_thesis_cached(
            context, "key-one", db_path=tmp_path / "thesis.sqlite3"
        )
        other_account = generate_governed_ai_thesis_cached(
            context, "key-two", db_path=tmp_path / "thesis.sqlite3"
        )
    assert first["cache_source"] == "LIVE_LLM"
    assert second["cache_source"] == "CACHE"
    assert other_account["cache_source"] == "LIVE_LLM"
    assert call.call_count == 2


def test_concurrent_cached_requests_share_one_generation(tmp_path):
    context = _context()
    calls = 0
    lock = threading.Lock()

    def generate(*_args, **_kwargs):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.15)
        return _signal()

    db_path = tmp_path / "concurrent.sqlite3"
    with patch("services.stage4_ai_thesis.call_gemini_structured_thesis", side_effect=generate):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: generate_governed_ai_thesis_cached(
                        context, "test-key", db_path=db_path
                    ),
                    range(2),
                )
            )
    assert calls == 1
    assert {result["cache_source"] for result in results} == {"LIVE_LLM", "CACHE"}
