from __future__ import annotations

from concurrent.futures import Future
from queue import Queue
from threading import Lock
import time
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from features.equities import pipeline


_UNIVERSE = [
    {
        "symbol": "ABC",
        "trading_symbol": "ABC",
        "name": "ABC Limited",
        "isin": "INABC",
        "segment": "NSE_EQ",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|ABC",
    }
]
_APP = """
from features.equities.scanner import render_market_scanner
render_market_scanner(broker_connected=False, scope="Equities", symbols=["ABC"])
"""


def test_pipeline_controls_and_results_table_render_before_data_is_loaded():
    app = AppTest.from_string(_APP)
    app.session_state["equities_jev_v3_enabled_min_last_price"] = False
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch(
            "features.equities.scan_table.show_scan_table"
        ) as show_table,
        patch.object(pipeline._PIPELINE_EXECUTOR, "submit", return_value=Future()),
    ):
        app.run(timeout=10)

    assert not app.exception
    assert app.selectbox[0].value == "Select stocks to scan"
    labels = [button.label for button in app.button]
    stage_labels = [
        next(label for label in labels if stage in label)
        for stage in (
            "Universe",
            "Live pulse",
            "Technical screen",
            "Enrichment",
            "AI thesis",
        )
    ]
    assert all("0/0" in label for label in stage_labels)
    assert "0/0" in stage_labels[3]
    assert "0/0" in stage_labels[4]
    assert not any("Stage 0" in label for label in stage_labels)
    assert not any("Custom hover details" in label for label in labels)
    assert all("/" in label for label in stage_labels)
    assert len(app.toggle) == 8
    assert all(control.value for control in app.toggle)
    info_labels = [
        item.value
        for item in app.markdown
        if 'class="jev-rule-label"' in item.value
    ]
    assert len(info_labels) == 9
    assert all('title="' in item and 'aria-label="Info:' in item for item in info_labels)
    assert any("bid/ask spread exceeds this percentage" in item for item in info_labels)
    assert any("not a Jev rejection rule" in item for item in info_labels)
    assert app.session_state["equities_jev_v3_enabled_min_last_price"] is False
    assert app.session_state["equities_jev_v5_enabled_min_last_price"] is True
    assert all("`" in label for label in stage_labels)
    assert not app.slider
    show_table.assert_called_once()
    payload, config = show_table.call_args.args
    assert payload == []
    assert config["showCart"] is False


def test_scanner_requires_explicit_stock_selection_before_editing_jev_rules():
    app = AppTest.from_string(_APP)
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch("features.equities.scan_table.show_scan_table"),
        patch.object(pipeline, "_advance_automatic_stages") as advance_stages,
    ):
        app.run(timeout=10)
        app.selectbox[0].select("Pick individual stocks").run(timeout=10)

    assert not app.exception
    assert app.multiselect[0].value == []
    assert app.session_state["market_equities_scan_symbols"] == []
    assert all(label.endswith("0/0`") for label in [
        button.label
        for button in app.button
        if any(stage in button.label for stage in (
            "Universe",
            "Live pulse",
            "Technical screen",
            "Enrichment",
            "AI thesis",
        ))
    ])
    advance_stages.assert_not_called()


def test_opening_jev_controls_does_not_start_a_technical_rescreen():
    from services.stage2_feature_gatekeeper import GatekeeperConfig

    signature = pipeline._key_signature(["ABC"])
    state = pipeline._new_pipeline_state(signature)
    state["statuses"] = ["success", "success", "refresh_required", "success", "idle"]
    state["gatekeeper_config"] = {
        name: getattr(GatekeeperConfig(), name)
        for name in GatekeeperConfig.__dataclass_fields__
    }
    state["gatekeeper_settings_version"] = 4
    app = AppTest.from_string(_APP)
    app.session_state["_equity_pipeline_state"] = {"equities": state}
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch("features.equities.scan_table.show_scan_table"),
        patch.object(pipeline, "_advance_automatic_stages") as advance_stages,
    ):
        app.run(timeout=10)

    assert not app.exception
    assert state["statuses"] == ["success", "success", "refresh_required", "success", "idle"]
    assert all(control.value for control in app.toggle)
    advance_stages.assert_not_called()


def test_jev_editor_remains_clickable_without_selected_stocks():
    state = pipeline._new_pipeline_state("empty")
    assert pipeline._stage2_editor_disabled(state) is False
    state["statuses"][3] = "running"
    assert pipeline._stage2_editor_disabled(state) is True
    state["statuses"][3] = "idle"
    state["statuses"][4] = "running"
    assert pipeline._stage2_editor_disabled(state) is True


def test_changing_one_jev_rule_updates_only_that_rule_and_rescreens():
    app = AppTest.from_string(_APP)
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch("features.equities.scan_table.show_scan_table"),
        patch.object(pipeline, "_advance_automatic_stages"),
    ):
        app.run(timeout=10)
        app.toggle(key="equities_jev_v5_enabled_min_last_price").set_value(False).run(
            timeout=10
        )

    state = app.session_state["_equity_pipeline_state"]["equities"]
    assert not app.exception
    assert state["gatekeeper_config"]["enable_min_last_price"] is False
    assert all(
        state["gatekeeper_config"][field] is True
        for field in (
            "enable_max_spread_pct",
            "enable_min_daily_volume",
            "enable_min_adx_trend",
            "enable_max_choppiness_index",
            "enable_min_piotroski_score",
            "enable_min_altman_z_score",
            "enable_max_promoter_pledge_pct",
        )
    )
    assert state["statuses"][2] == "idle"


def test_ai_thesis_action_waits_until_screened_stocks_are_available():
    app = AppTest.from_string(_APP)
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch("features.equities.scan_table.show_scan_table"),
        patch.object(pipeline, "_advance_automatic_stages"),
    ):
        app.run(timeout=10)

    state = app.session_state["_equity_pipeline_state"]["equities"]
    assert not app.exception
    assert app.button(key="equity_pipeline_equities_stage_4").disabled
    assert state["statuses"][4] == "idle"


def test_stage4_generates_theses_for_every_screened_stock_in_one_batch():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    state = pipeline._new_pipeline_state("batch")
    keys = ["NSE_EQ|ABC", "NSE_EQ|DEF"]
    instruments = [
        {"instrument_key": key, "symbol": key.split("|")[1], "segment": "NSE_EQ"}
        for key in keys
    ]
    state["stage2_decisions"] = {
        key: GatekeeperDecision(
            instrument_key=key,
            symbol=key.split("|")[1],
            passed=True,
            signal_tier="BUY",
            score=0.8,
            features={"last_price": 100, "rsi_14": 55},
        )
        for key in keys
    }
    state["stage2_rows"] = [{"Instrument_key": key} for key in keys]
    state["stage3_results"] = {key: {"status": "ok"} for key in keys}
    quotes = {
        key: {"last_price": 100, "timestamp": time.time()}
        for key in keys
    }
    inline_executor = type(
        "InlineExecutor",
        (),
        {
            "submit": lambda _self, fn, *args: _completed_future(fn, *args),
        },
    )()
    generated = []

    def generate(context, **kwargs):
        generated.append(context)
        return {"thesis": {"symbol": context["symbol"]}}

    with (
        patch.object(pipeline, "get_fundamental_snapshots", return_value={}),
        patch.object(pipeline, "build_governed_ai_payload", side_effect=lambda **kwargs: {
            "symbol": kwargs["symbol"]
        }),
        patch.object(pipeline, "_stage4_gemini_api_key", return_value="test-key"),
        patch.object(pipeline, "generate_governed_ai_thesis_cached", side_effect=generate),
        patch.object(pipeline, "_STAGE4_EXECUTOR", inline_executor),
    ):
        pipeline._start_stage4_batch(
            state,
            instruments,
            {"macro": {}},
            quotes,
            time.time(),
        )
        pipeline._consume_stage4_future(state)

    assert len(generated) == 2
    assert {item["symbol"] for item in generated} == {"ABC", "DEF"}
    assert set(state["stage4_results"]) == set(keys)
    assert state["statuses"][4] == "success"
    assert state["messages"][4] == "AI theses generated for all 2 screened stocks."


def _completed_future(function, *args):
    future = Future()
    future.set_result(function(*args))
    return future


def test_missing_hidden_jev_widget_state_is_restored_from_saved_rules():
    from services.stage2_feature_gatekeeper import GatekeeperConfig

    signature = pipeline._key_signature(["ABC"])
    state = pipeline._new_pipeline_state(signature)
    state["gatekeeper_config"] = {
        name: getattr(GatekeeperConfig(), name)
        for name in GatekeeperConfig.__dataclass_fields__
    }
    state["gatekeeper_config"]["enable_min_last_price"] = False
    state["gatekeeper_settings_version"] = 4
    app = AppTest.from_string(_APP)
    app.session_state["_equity_pipeline_state"] = {"equities": state}
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch("features.equities.scan_table.show_scan_table"),
        patch.object(pipeline, "_advance_automatic_stages"),
    ):
        app.run(timeout=10)

    assert not app.exception
    assert app.session_state["equities_jev_v5_enabled_min_last_price"] is False
    assert all(
        app.session_state[f"equities_jev_v5_enabled_{name}"] is True
        for name in (
            "max_spread_pct",
            "min_daily_volume",
            "min_adx_trend",
            "max_choppiness_index",
            "min_piotroski_score",
            "min_altman_z_score",
            "max_promoter_pledge_pct",
        )
    )


def test_stale_jev_widget_state_cannot_turn_off_saved_rules():
    from services.stage2_feature_gatekeeper import GatekeeperConfig

    signature = pipeline._key_signature(["ABC"])
    state = pipeline._new_pipeline_state(signature)
    state["gatekeeper_config"] = {
        name: getattr(GatekeeperConfig(), name)
        for name in GatekeeperConfig.__dataclass_fields__
    }
    state["gatekeeper_settings_version"] = 4
    app = AppTest.from_string(_APP)
    app.session_state["_equity_pipeline_state"] = {"equities": state}
    app.session_state["equities_jev_v5_enabled_min_last_price"] = False
    with (
        patch.object(
            pipeline,
            "read_stage0_cache",
            return_value={"universe": _UNIVERSE, "macro": {}, "updated_at": 1},
        ),
        patch("features.equities.scan_table.show_scan_table"),
        patch.object(pipeline, "_advance_automatic_stages"),
    ):
        app.run(timeout=10)

    assert not app.exception
    assert app.session_state["equities_jev_v5_enabled_min_last_price"] is True
    assert all(control.value for control in app.toggle)


def test_stage4_reads_the_gemini_key_saved_in_user_settings():
    with (
        patch.object(pipeline, "configured_value", return_value=""),
        patch.object(
            pipeline.st,
            "session_state",
            {
                "settings_ai_active_config_id": "gemini-config",
                "settings_ai_provider_configs": [
                    {"id": "gemini-config", "provider": "Gemini"},
                ],
                "settings_ai_provider_config_keys": {
                    "gemini-config": " user-settings-key ",
                },
            },
        ),
    ):
        assert pipeline._stage4_gemini_api_key() == "user-settings-key"


def test_stage_totals_follow_each_stages_actual_input_set():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    state = pipeline._new_pipeline_state("universe")
    state["stage2_decisions"] = {
        f"NSE_EQ|PASS{index}": GatekeeperDecision(
            instrument_key=f"NSE_EQ|PASS{index}",
            symbol=f"PASS{index}",
            passed=True,
            signal_tier="BUY",
            score=80,
            features={"last_price": 123.0},
        )
        for index in range(2)
    }

    totals = pipeline._stage_totals(2763, [{}] * 2500, state)

    assert totals == [2763, 2500, 2500, 2, 2]


def test_completed_live_pulse_stays_green_while_technical_screen_runs():
    state = pipeline._new_pipeline_state("universe")
    state["stage1_result"] = {
        "source": "LIVE",
        "quotes": {"quote-key-1": {}, "quote-key-2": {}},
    }
    state["stage2_attempted"] = True
    state["statuses"][2] = "running"

    counts = pipeline._stage_counts(state, _UNIVERSE)

    assert counts[1] == 2
    assert pipeline._stage_display_status(state, 1) == "success"
    assert pipeline._stage_display_status(state, 2) == "running"


def test_stale_live_pulse_remains_refresh_required_during_technical_screen():
    state = pipeline._new_pipeline_state("universe")
    state["stage1_result"] = {
        "source": "STALE_CACHE",
        "quotes": {"NSE_EQ|ABC": {}},
    }
    state["stage2_attempted"] = True
    state["statuses"][1] = "refresh_required"
    state["statuses"][2] = "running"

    assert pipeline._stage_display_status(state, 1) == "refresh_required"


def test_stage2_coverage_explains_missing_history_without_blame_on_quotes():
    message = pipeline._stage2_coverage_message(2430, 2500, 2500)

    assert message.startswith("Features: 2430/2500; quotes: 2500/2500.")
    assert "70 stock(s) lack sufficient local daily candle history" in message
    assert "sync history" in message
    assert "refresh quotes" not in message


def test_stage3_coverage_explains_not_fno_as_expected():
    available, message, actionable_gaps = pipeline._stage3_coverage_message(
        {
            "NSE_EQ|ABC": {
                "derivatives": {"status": "NOT_FNO"},
                "news_and_consensus": {"status": "OK"},
            }
        }
    )

    assert available == 1
    assert not actionable_gaps
    assert "1/2 feeds available" in message
    assert "NOT_FNO for 1" in message
    assert "expected for non-F&O stocks" in message


def test_stage3_coverage_marks_provider_throttling_as_actionable():
    available, message, actionable_gaps = pipeline._stage3_coverage_message(
        {
            "NSE_EQ|ABC": {
                "derivatives": {
                    "status": "RATE_LIMITED",
                    "endpoint": "option_contract",
                    "retry_after_seconds": 60,
                },
                "news_and_consensus": {"status": "OK"},
            }
        }
    )

    assert available == 1
    assert actionable_gaps
    assert "RATE_LIMITED" in message
    assert "option_contract endpoint" in message
    assert "retry after 60s" in message


def test_automatic_pipeline_runs_stages_in_order_and_starts_stage3_for_passes():
    state = pipeline._new_pipeline_state("universe")
    state["stage0_result"] = {"instruments": _UNIVERSE}
    call_order = []

    def run_stage1(current, instruments, _session_state):
        call_order.append("stage1")
        current["stage1_result"] = {"quotes": {"NSE_EQ|ABC": {}}}

    def run_stage2(current, _instruments, _config):
        call_order.append("stage2")
        current["stage2_attempted"] = True

    def run_stage3(current, _instruments):
        call_order.append("stage3")

    with (
        patch.object(pipeline, "_handle_stage1", side_effect=run_stage1),
        patch.object(pipeline, "_handle_stage2", side_effect=run_stage2),
        patch.object(pipeline, "_start_stage3_batch", side_effect=run_stage3),
    ):
        pipeline._advance_automatic_stages(state, ["ABC"], _UNIVERSE)

    assert call_order == ["stage1", "stage2", "stage3"]
    assert state["stage1_attempted"] is True
    assert state["stage3_future"] is None


def test_automatic_stage3_enriches_all_gatekeeper_approved_stocks():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    state = pipeline._new_pipeline_state("universe")
    state["stage0_result"] = {}
    state["stage1_result"] = {"quotes": {"NSE_EQ|ABC": {}}}
    state["stage1_attempted"] = True
    state["stage2_attempted"] = True
    state["stage2_decisions"] = {
        "NSE_EQ|ABC": GatekeeperDecision(
            instrument_key="NSE_EQ|ABC",
            symbol="ABC",
            passed=True,
            signal_tier="BUY",
            score=80,
            features={"last_price": 123.0},
        ),
        "NSE_EQ|DEF": GatekeeperDecision(
            instrument_key="NSE_EQ|DEF",
            symbol="DEF",
            passed=True,
            signal_tier="BUY",
            score=75,
            features={"last_price": 90.0},
        ),
    }

    with patch.object(pipeline, "_start_stage3_batch") as enrich_stock:
        pipeline._advance_automatic_stages(state, ["ABC"], _UNIVERSE)

    enrich_stock.assert_called_once_with(state, _UNIVERSE)


def test_stage3_batch_enriches_only_stage2_passes():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    decisions = [
        GatekeeperDecision(
            instrument_key="NSE_EQ|ABC",
            symbol="ABC",
            passed=True,
            signal_tier="BUY",
            score=80,
            features={"last_price": 123.0},
        ),
        GatekeeperDecision(
            instrument_key="NSE_EQ|DEF",
            symbol="DEF",
            passed=False,
            signal_tier="REJECTED",
            score=0,
            features={"last_price": 90.0},
        ),
    ]
    with patch.object(
        pipeline,
        "get_stage3_enrichment_cached",
        return_value={"news_and_consensus": {"status": "OK"}},
    ) as fetch:
        results, errors = pipeline._stage3_batch_worker(
            decisions,
            _UNIVERSE,
            {},
            "token",
            Queue(),
        )

    assert list(results) == ["NSE_EQ|ABC"]
    assert not errors
    fetch.assert_called_once()


def test_stage3_batch_fetches_multiple_stocks_concurrently_with_a_bound():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    decisions = [
        GatekeeperDecision(
            instrument_key=f"NSE_EQ|SYM{index}",
            symbol=f"SYM{index}",
            passed=True,
            signal_tier="BUY",
            score=80,
            features={"last_price": 123.0},
        )
        for index in range(8)
    ]
    instruments = [
        {
            "instrument_key": decision.instrument_key,
            "symbol": decision.symbol,
            "segment": "NSE_EQ",
        }
        for decision in decisions
    ]
    active = 0
    max_active = 0
    lock = Lock()

    def fetch(**kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"news_and_consensus": {"status": "OK"}}

    with patch.object(pipeline, "get_stage3_enrichment_cached", side_effect=fetch):
        results, errors = pipeline._stage3_batch_worker(
            decisions, instruments, {}, "token", Queue()
        )

    assert len(results) == 8
    assert not errors
    assert 1 < max_active <= pipeline._STAGE3_MAX_CONCURRENCY


def test_stage3_progress_is_visible_while_batch_is_running():
    state = pipeline._new_pipeline_state("universe")
    state["stage3_future"] = Future()
    state["stage3_progress_events"] = Queue()
    state["stage3_progress_events"].put(
        {
            "completed": 2,
            "enriched_delta": 2,
            "total": 5,
            "current": "XYZ",
        }
    )

    pipeline._consume_stage3_future(state)

    assert state["statuses"][3] == "running"
    assert state["stage3_progress"]["enriched"] == 2
    assert "2 of 5 stocks" in state["messages"][3]
    assert "XYZ" in state["messages"][3]


def test_selected_bse_ticker_resolves_only_to_bse_equity_listing():
    universe = [
        {
            "symbol": "ABC",
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|ABC",
            "isin": "INABC",
        },
        {
            "symbol": "ABC.BO",
            "segment": "BSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "BSE_EQ|ABC",
            "isin": "INABC",
        },
    ]

    selected = pipeline._selected_instruments(["ABC.BO"], universe)

    assert [row["instrument_key"] for row in selected] == ["BSE_EQ|ABC"]


def test_unqualified_symbol_uses_bse_when_no_nse_listing_is_available():
    selected = pipeline._selected_instruments(
        ["ABC"],
        [
            {
                "symbol": "ABC",
                "segment": "BSE_EQ",
                "instrument_type": "EQ",
                "instrument_key": "BSE_EQ|ABC",
                "isin": "INABC",
            }
        ],
    )

    assert [row["instrument_key"] for row in selected] == ["BSE_EQ|ABC"]


def test_stage2_rows_convert_gatekeeper_decisions_to_table_contract():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    decision = GatekeeperDecision(
        instrument_key="NSE_EQ|ABC",
        symbol="ABC",
        passed=True,
        signal_tier="BUY",
        score=72.0,
        features={
            "last_price": 120.0,
            "sma_20": 115.0,
            "rsi_14": 56.0,
            "above_vwap": True,
            "adx_14": 25.0,
            "choppiness_14": 50.0,
        },
    )

    rows = pipeline._stage2_table_rows(
        [
            decision,
            GatekeeperDecision(
                instrument_key="NSE_EQ|DEF",
                symbol="DEF",
                passed=False,
                signal_tier="REJECTED",
                score=0.0,
                features={"last_price": 90.0},
            ),
        ],
        _UNIVERSE,
        {"NSE_EQ|ABC": {"last_price": 120.0, "close": 118.0}},
    )

    assert rows[0]["Ticker"] == "ABC"
    assert rows[0]["Recommendation"] == "BUY"
    assert rows[0]["Passes_filters"] is True
    assert rows[0]["Price"] == 120.0
    assert rows[0]["Total_signals"] == 22
    assert len(rows[0]["Indicator_checks"]) == 22
    assert rows[0]["Indicator_checks"][0][0] == "SMA 20"


def test_technical_indicator_table_lists_all_22_calculated_features():
    rows = pipeline._technical_indicator_table(
        {
            "last_price": 120,
            "sma_20": 115,
            "sma_50": 110,
            "sma_200": 100,
            "ema_21": 117,
            "macd_line": 1.2,
            "macd_signal": 0.8,
            "macd_hist": 0.4,
            "rsi_14": 56,
            "bb_upper": 125,
            "bb_lower": 105,
            "bb_width_pct": 12,
            "atr_14": 3,
            "atr_percentile": 40,
            "squeeze_alert": False,
            "adx_14": 25,
            "choppiness_14": 50,
            "mfi_14": 60,
            "vpt": 1,
            "rvol_20": 1.2,
            "stoch_k": 55,
            "roc_10": 3,
            "dist_52w_high_pct": -5,
        }
    )

    assert len(rows) == 22
    assert all(row["Value"] != "Unavailable" for row in rows)
    assert next(row for row in rows if row["Indicator"] == "ADX 14")["Read"] == "Positive"


def test_pipeline_stock_details_are_grouped_for_the_expanded_table_row():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    state = pipeline._new_pipeline_state("universe")
    decision = GatekeeperDecision(
        instrument_key="NSE_EQ|ABC",
        symbol="ABC",
        passed=True,
        signal_tier="BUY",
        score=72,
        features={"last_price": 120, "sma_20": 115, "adx_14": 25},
    )
    state["stage2_decisions"] = {"NSE_EQ|ABC": decision}
    state["stage1_result"] = {
        "quotes": {"NSE_EQ|ABC": {"last_price": 120, "vwap": 119}}
    }
    state["stage2_fundamentals"] = {"ABC": {"sector": "Technology"}}
    state["stage3_results"] = {
        "NSE_EQ|ABC": {
            "derivatives": {"status": "NOT_FNO"},
            "news_and_consensus": {"status": "OK", "sector": "Technology"},
        }
    }
    state["stage4_results"] = {
        "NSE_EQ|ABC": {
            "thesis": {
                "confidence_score": 0.87,
                "stance": "BUY",
                "executive_thesis": "Relative strength supports the thesis.",
            }
        }
    }
    rows = pipeline._stage2_table_rows(
        [decision],
        _UNIVERSE,
        {"NSE_EQ|ABC": {"last_price": 120, "vwap": 119}},
    )

    detailed_rows = pipeline._pipeline_details_for_rows(
        rows,
        state,
        _UNIVERSE,
        {"nifty": 22000},
    )

    details = detailed_rows[0]["Pipeline_details"]
    assert detailed_rows[0]["AI_confidence"] == 87.0
    group_names = [group[0] for group in details["groups"]]
    assert group_names == [
        "Company and listing",
        "Live quote",
        "Technical indicators",
        "Jev screening",
        "Fundamentals",
        "Derivatives and options",
        "Analyst outlook",
        "Recent news",
        "Market context",
        "AI thesis",
    ]
    assert len(
        next(group for group in details["groups"] if group[0] == "Technical indicators")[1]
    ) == 22
    assert details["group_ratings"]["Technical indicators"] == {
        "positive": 2,
        "rated": 2,
    }


def test_pipeline_stock_panels_explain_missing_feeds():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    decision = GatekeeperDecision(
        instrument_key="NSE_EQ|ABC",
        symbol="ABC",
        passed=True,
        signal_tier="BUY",
        score=72,
        features={"last_price": 120},
    )
    rows = pipeline._stage2_table_rows([decision], _UNIVERSE, {})

    detailed_rows = pipeline._pipeline_details_for_rows(
        rows,
        pipeline._new_pipeline_state("universe"),
        _UNIVERSE,
    )

    details = detailed_rows[0]["Pipeline_details"]
    values = details["indicators"]
    groups = {
        title: {label: values[key][0] for key, label in items}
        for title, items in details["groups"]
    }
    assert groups["Live quote"]["Status"] == "Quote not available"
    assert groups["Market context"]["Status"] == "Macro context not available"
    assert groups["AI thesis"]["Status"] == "Run AI thesis to generate a governed view"


def test_stage4_context_uses_all_final_passes_not_rejected_stocks():
    from services.stage2_feature_gatekeeper import GatekeeperDecision

    state = pipeline._new_pipeline_state("universe")
    state["stage2_decisions"] = {
        "NSE_EQ|ABC": GatekeeperDecision(
            instrument_key="NSE_EQ|ABC",
            symbol="ABC",
            passed=True,
            signal_tier="BUY",
            score=82,
            features={"last_price": 120, "rsi_14": 56},
        ),
        "NSE_EQ|DEF": GatekeeperDecision(
            instrument_key="NSE_EQ|DEF",
            symbol="DEF",
            passed=False,
            signal_tier="REJECTED",
            score=0,
            features={"last_price": 90},
        ),
        "NSE_EQ|GHI": GatekeeperDecision(
            instrument_key="NSE_EQ|GHI",
            symbol="GHI",
            passed=True,
            signal_tier="STRONG_BUY",
            score=91,
            features={"last_price": 210, "adx_14": 28, "rvol_20": 1.4},
        ),
        "NSE_EQ|OLD": GatekeeperDecision(
            instrument_key="NSE_EQ|OLD",
            symbol="OLD",
            passed=True,
            signal_tier="BUY",
            score=90,
            features={"last_price": 150},
        ),
    }
    state["stage2_rows"] = [
        {"Instrument_key": "NSE_EQ|ABC"},
        {"Instrument_key": "NSE_EQ|DEF"},
        {"Instrument_key": "NSE_EQ|GHI"},
    ]

    candidates = pipeline._stage4_screened_candidates(
        state,
        [
            {"instrument_key": "NSE_EQ|ABC", "symbol": "ABC", "segment": "NSE_EQ"},
            {"instrument_key": "NSE_EQ|DEF", "symbol": "DEF", "segment": "NSE_EQ"},
            {"instrument_key": "NSE_EQ|GHI", "symbol": "GHI", "segment": "NSE_EQ"},
        ],
        {"NSE_EQ|ABC": {"last_price": 121}},
    )

    assert [candidate["symbol"] for candidate in candidates] == ["ABC", "GHI"]
    assert candidates[0]["last_price"] == 121
    assert candidates[0]["gatekeeper_score"] == 82
