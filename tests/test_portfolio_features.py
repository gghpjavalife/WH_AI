import unittest
import hashlib
import secrets
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from features.mutual_funds.workspace import validate_mutual_funds
from features.shared.performance import style_returns
from features.home.workspace import ALLOCATION_COLORS, build_allocation_chart
from core.logging import diagnostic_summary, log_failure
from features.portfolio.metrics import (
    calculate_allocation,
)
from core.config import Settings, settings
import security.oauth as oauth_service
from security.oauth import (
    consume_oauth_state,
    consume_oauth_state_context,
    create_oauth_state,
)
from services.notifications import send_email_alert, send_whatsapp_alert
import services.database as secure_database
import services.user_settings as user_settings_service
from services.ai import (
    ask_llm_agent,
    test_ai_provider_configuration as verify_ai_provider_configuration,
)
from ui.helpers import (
    broker_connect_button_css,
    same_tab_link_html,
)
from brokers.factory import (
    BrokerFactory,
    DhanAdapter,
    UpstoxAdapter,
    ZerodhaAdapter,
)


_database_settings_patcher = None
_database_settings_directory = None
_database_module_patchers = []


def setUpModule():
    global _database_settings_patcher, _database_settings_directory
    _database_settings_directory = TemporaryDirectory()
    test_settings = replace(
        settings,
        oauth_database_path=str(
            Path(_database_settings_directory.name) / "app-settings.sqlite3"
        ),
        database_encryption_key=secrets.token_hex(32),
    )
    _database_settings_patcher = patch.object(
        secure_database,
        "settings",
        test_settings,
    )
    _database_settings_patcher.start()
    for module in (user_settings_service, oauth_service):
        module_patcher = patch.object(module, "settings", test_settings)
        module_patcher.start()
        _database_module_patchers.append(module_patcher)


def tearDownModule():
    if _database_settings_patcher is not None:
        _database_settings_patcher.stop()
    for module_patcher in _database_module_patchers:
        module_patcher.stop()
    if _database_settings_directory is not None:
        _database_settings_directory.cleanup()


class PortfolioFeatureTests(unittest.TestCase):
    def _register_broker(
        self,
        app,
        broker: str,
        *,
        credentials: dict[str, str] | None = None,
        consent: bool = False,
    ) -> str:
        app.selectbox(key="selected_broker").select(broker).run(timeout=20)
        app.button(key="open_user_settings").click().run(timeout=20)
        for field, value in (credentials or {}).items():
            app.text_input(key=f"user_settings_{broker}_{field}").set_value(
                value
            ).run(timeout=20)
        app.checkbox(key="settings_sensitive_consent").set_value(consent).run(
            timeout=20
        )
        app.button(key="save_user_settings").click().run(timeout=20)
        return broker

    def test_diagnostics_report_safe_http_status_and_request_id(self):
        api_error = RuntimeError("upstream response includes private content")
        api_error.status = 403
        api_error.reason = "Forbidden"
        api_error.headers = {"X-Request-Id": "request-123"}
        wrapped_error = RuntimeError("Broker API request failed")
        wrapped_error.__cause__ = api_error

        summary = diagnostic_summary("Upstox mutual-fund refresh", wrapped_error)
        self.assertIn("HTTP 403", summary)
        self.assertIn("Forbidden", summary)
        self.assertIn("request_id=request-123", summary)
        self.assertNotIn("private content", summary)

        with self.assertLogs("wealth_home_ai", level="ERROR") as logs:
            log_failure(
                "Upstox mutual-fund refresh",
                wrapped_error,
                broker="Upstox",
            )
        self.assertIn("request_id=request-123", logs.output[0])

    def test_return_table_backgrounds_distinguish_gains_losses_and_neutral(self):
        frame = pd.DataFrame(
            {
                "Ticker": ["GAIN", "LOSS", "FLAT"],
                "Returns_%": [25.0, -8.0, 0.0],
            }
        )

        styled = style_returns(frame, theme="dark")
        html = styled.to_html()

        self.assertIn("background-color: #216B44", html)
        self.assertIn("background-color: #64282F", html)
        self.assertIn("background-color: #172334", html)

    def test_broker_sign_in_links_navigate_in_the_same_tab(self):
        markup = same_tab_link_html(
            "Connect with Zerodha",
            "https://broker.example/authorize?client_id=app&state=one",
        )
        self.assertIn('target="_self"', markup)
        self.assertNotIn('target="_blank"', markup)
        self.assertIn("&amp;state=one", markup)
        self.assertIn(
            "linear-gradient(135deg,#22c55e 0%,#15803d 100%)", markup
        )

        button_css = broker_connect_button_css()
        for key in (
            "connect_upstox",
            "connect_angel_one",
            "connect_zerodha",
            "connect_dhan",
        ):
            self.assertIn(f".st-key-{key} button", button_css)
        self.assertIn(
            "linear-gradient(135deg,#22c55e 0%,#15803d 100%)", button_css
        )

    def test_broker_login_forms_show_missing_credentials_for_all_builtins(self):
        cases = (
            (
                "Upstox",
                "Connect with Upstox",
                "link_button",
            ),
            (
                "Angel One",
                "Connect with Angel One →",
                "button",
            ),
            (
                "Zerodha",
                "Connect with Zerodha",
                "link_button",
            ),
            (
                "Dhan",
                "Connect with Dhan →",
                "button",
            ),
        )
        for broker, button_label, element_type in cases:
            with self.subTest(broker=broker):
                with TemporaryDirectory() as directory:
                    database_path = Path(directory) / "settings.sqlite3"
                    with patch(
                        "services.user_settings.settings",
                        replace(settings, oauth_database_path=str(database_path)),
                    ):
                        app = AppTest.from_file(
                            str(Path(__file__).resolve().parents[1] / "app.py")
                        ).run(timeout=20)
                        app.selectbox(key="selected_broker").select("Upstox").run(
                            timeout=20
                        )
                        self.assertTrue(
                            any(
                                widget.label == "Select a broker"
                                for widget in app.selectbox
                            )
                        )
                        self.assertTrue(
                            any("credentials are not configured" in item.value for item in app.warning)
                        )
                        self._register_broker(app, broker)
                        if broker == "Angel One":
                            self.assertFalse(
                                any(
                                    widget.label == "Callback URL"
                                    for widget in app.text_input
                                )
                            )
                        self.assertTrue(
                            any(
                                widget.label == "Select a broker"
                                for widget in app.selectbox
                            )
                        )
                        if element_type == "button":
                            button = next(
                                button
                                for button in app.button
                                if button.label == button_label
                            )
                            self.assertTrue(button.disabled)
                        self.assertFalse(app.exception)
                        self.assertIsNone(
                            app.session_state.get("pending_broker_name")
                        )
                        self.assertFalse(
                            any(
                                widget.label == "OpenRouter API key"
                                for widget in app.text_input
                            )
                        )
                        self.assertGreaterEqual(len(app.get("popover")), 4)
                        self.assertTrue(
                            any(
                                widget.label == "Select a broker"
                                for widget in app.selectbox
                            )
                        )

    def test_connect_controls_update_when_credentials_are_entered(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "user-settings.sqlite3"
            with patch(
                "services.user_settings.settings",
                replace(settings, oauth_database_path=str(database_path)),
            ):
                for broker, credentials, button_label in (
                    (
                        "Angel One",
                        {
                            "user_settings_Angel One_api_key": "api-key",
                            "user_settings_Angel One_client_id": "client-id",
                            "user_settings_Angel One_password": "password",
                            "user_settings_Angel One_totp_secret": "totp-secret",
                        },
                        "Connect with Angel One →",
                    ),
                    (
                        "Dhan",
                        {
                            "user_settings_Dhan_client_id": "client-id",
                            "user_settings_Dhan_api_key": "api-key",
                            "user_settings_Dhan_api_secret": "api-secret",
                        },
                        "Connect with Dhan →",
                    ),
                ):
                    with self.subTest(broker=broker):
                        app = AppTest.from_file(
                            str(Path(__file__).resolve().parents[1] / "app.py")
                        ).run(timeout=20)
                        self._register_broker(
                            app,
                            broker,
                            credentials={
                                key.removeprefix(f"user_settings_{broker}_"): value
                                for key, value in credentials.items()
                            },
                            consent=True,
                        )
                        broker_input_prefix = {
                            "Angel One": "angel_",
                            "Dhan": "dhan_",
                        }[broker]
                        self.assertFalse(
                            any(
                                widget.key
                                and widget.key.startswith(broker_input_prefix)
                                for widget in app.text_input
                            )
                        )
                        self.assertTrue(
                            any(
                                "saved securely and encrypted" in item.value.lower()
                                for item in app.success
                            )
                        )
                        self.assertFalse(
                            any(
                                "credentials saved" in item.value.lower()
                                for item in app.markdown
                            )
                        )
                        button = next(
                            button
                            for button in app.button
                            if button.label == button_label
                        )
                        self.assertFalse(button.disabled)
                        self.assertFalse(app.exception)

    def test_missing_broker_credentials_can_be_reviewed_and_saved_from_connection_screen(self):
        from services.user_settings import load_user_settings

        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "user-settings.sqlite3"
            with patch(
                "services.user_settings.settings",
                replace(settings, oauth_database_path=str(database_path)),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py")
                ).run(timeout=20)
                app.selectbox(key="selected_broker").select("Upstox").run(
                    timeout=20
                )
                self.assertTrue(
                    any("credentials are not configured" in item.value for item in app.warning)
                )
                self.assertTrue(
                    any(widget.key == "upstox_api_key" for widget in app.text_input)
                )
                self.assertTrue(
                    any(widget.key == "upstox_api_secret" for widget in app.text_input)
                )
                review_button = next(
                    button
                    for button in app.button
                    if button.key == "save_upstox_credentials"
                )
                self.assertTrue(review_button.disabled)
                app.text_input(key="upstox_api_key").set_value("app-key").run(
                    timeout=20
                )
                self.assertFalse(
                    next(
                        button
                        for button in app.button
                        if button.key == "save_upstox_credentials"
                    ).disabled
                )
                app.text_input(key="upstox_api_secret").set_value(
                    "app-secret"
                ).run(timeout=20)
                app.button(key="save_upstox_credentials").click().run(timeout=20)
                self.assertEqual(
                    app.session_state.user_settings_Upstox_api_key,
                    "app-key",
                )
                self.assertEqual(
                    app.session_state.user_settings_Upstox_api_secret,
                    "app-secret",
                )
                app.checkbox(key="settings_sensitive_consent").set_value(
                    True
                ).run(timeout=20)
                app.button(key="save_user_settings").click().run(timeout=20)
                saved = load_user_settings("local-user")
                self.assertEqual(
                    saved["sensitive"]["broker_profiles"]["builtin-upstox"],
                    {"api_key": "app-key", "api_secret": "app-secret"},
                )
                self.assertTrue(saved["sensitive_consent"])

    def test_broker_settings_offer_all_builtin_brokers_without_enable_toggles(self):
        from services.user_settings import load_user_settings

        app_source = """
import streamlit as st
from ui.user_settings import apply_settings_to_session, render_user_settings
if "registry_initialized" not in st.session_state:
    apply_settings_to_session({
        "preferences": {},
        "sensitive": {},
        "sensitive_consent": False,
    })
    st.session_state.registry_initialized = True
render_user_settings("registry-test-user")
"""
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "settings.sqlite3"
            with patch(
                "services.user_settings.settings",
                replace(settings, oauth_database_path=str(database_path)),
            ):
                app = AppTest.from_string(app_source).run(timeout=20)
                app.button(key="open_user_settings").click().run(timeout=20)
                self.assertEqual(
                    [profile["type"] for profile in app.session_state.registered_broker_profiles],
                    ["Upstox", "Zerodha", "Angel One", "Dhan"],
                )
                self.assertTrue(
                    all(
                        profile["active"]
                        for profile in app.session_state.registered_broker_profiles
                    )
                )
                self.assertFalse(
                    any(widget.label == "Broker name" for widget in app.text_input)
                )
                self.assertFalse(
                    any(widget.key == "start_broker_registration" for widget in app.button)
                )
                self.assertFalse(
                    any("Custom read-only" in option for widget in app.selectbox for option in widget.options)
                )
                self.assertFalse(
                    any("Enable " in widget.label for widget in app.checkbox)
                )
                self.assertFalse(
                    any(
                        widget.key == "settings_default_broker_widget"
                        for widget in app.selectbox
                    )
                )
                self.assertTrue(
                    all(
                        not expander.proto.expanded
                        for expander in app.get("expander")
                    )
                )
                app.text_input(key="settings_profile_name").set_value(
                    "Updated profile"
                ).run(timeout=20)
                app.text_input(key="settings_profile_email").set_value(
                    "updated@example.com"
                ).run(timeout=20)
                app.number_input(key="settings_max_allocation_pct").set_value(
                    65.0
                ).run(timeout=20)
                app.text_input(key="user_settings_Upstox_api_key").set_value(
                    "updated-api-key"
                ).run(timeout=20)
                app.checkbox(key="settings_sensitive_consent").set_value(
                    True
                ).run(timeout=20)
                app.button(key="save_user_settings").click().run(timeout=20)
                self.assertEqual(
                    app.session_state.selected_broker,
                    "Upstox",
                )
                self.assertTrue(
                    any("Settings saved successfully" in item.value for item in app.success)
                )
                self.assertTrue(
                    any(button.key == "dismiss_settings_saved" for button in app.button)
                )
                app.button(key="dismiss_settings_saved").click().run(timeout=20)
                self.assertNotIn("user_settings_feedback", app.session_state)
                self.assertFalse(app.session_state.user_settings_dialog_open)
                self.assertFalse(
                    any(
                        widget.key == "settings_profile_name"
                        for widget in app.text_input
                    )
                )

                app.button(key="open_user_settings").click().run(timeout=20)
                self.assertNotIn("settings_default_broker", app.session_state)
                self.assertEqual(
                    app.session_state.settings_profile_name,
                    "Updated profile",
                )
                self.assertEqual(
                    app.session_state.settings_profile_email,
                    "updated@example.com",
                )
                self.assertEqual(
                    app.session_state.settings_max_allocation_pct,
                    65.0,
                )
                self.assertEqual(
                    app.session_state.user_settings_Upstox_api_key,
                    "updated-api-key",
                )
                app.text_input(key="settings_profile_email").set_value(
                    "invalid-email"
                ).run(timeout=20)
                app.button(key="save_user_settings").click().run(timeout=20)
                self.assertTrue(
                    any("valid email address" in item.value for item in app.error)
                )
                app.text_input(key="settings_profile_email").set_value(
                    "updated@example.com"
                ).run(timeout=20)
                app.button(key="save_user_settings").click().run(timeout=20)
                self.assertEqual(
                    load_user_settings("registry-test-user")["preferences"][
                        "broker_profiles"
                    ][0]["name"],
                    "Upstox",
                )
                self.assertNotIn(
                    "default_broker",
                    load_user_settings("registry-test-user")["preferences"],
                )
                self.assertFalse(app.exception)

    def test_upstox_callback_can_resume_without_original_streamlit_session(self):
        oauth_state = secrets.token_urlsafe(32)
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "security.oauth._database_path",
                return_value=database_path,
            ):
                create_oauth_state(
                    oauth_state,
                    {
                        "broker": "Upstox",
                        "api_key": "app-key",
                        "api_secret": "app-secret",
                        "redirect_url": "https://wealth.example.com/",
                    },
                )
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py")
                )
                app.query_params.update(
                    {"code": "one-time-code", "state": oauth_state}
                )
                sync_barrier = Barrier(4)

                def concurrent_result(value):
                    def fetch(_token):
                        sync_barrier.wait(timeout=3)
                        return value

                    return fetch

                with (
                    patch(
                        "brokers.factory.UpstoxAdapter.authenticate",
                        return_value="access-token",
                    ),
                    patch(
                        "brokers.factory.UpstoxAdapter.fetch_profile",
                        return_value={"name": "Test user", "user_id": "test-user"},
                    ),
                    patch(
                        "brokers.factory.UpstoxAdapter.fetch_balance",
                        side_effect=concurrent_result(0),
                    ),
                    patch(
                        "brokers.factory.UpstoxAdapter.fetch_positions",
                        side_effect=concurrent_result(
                            pd.DataFrame(
                                columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                            )
                        ),
                    ),
                    patch(
                        "brokers.factory.UpstoxAdapter.fetch_holdings",
                        side_effect=concurrent_result(
                            pd.DataFrame(
                                columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                            )
                        ),
                    ),
                    patch(
                        "brokers.factory.UpstoxAdapter.fetch_mutual_fund_holdings",
                        side_effect=concurrent_result(
                            pd.DataFrame(
                                columns=[
                                    "Fund",
                                    "Folio",
                                    "Units",
                                    "Avg_NAV",
                                    "Latest_NAV",
                                ]
                            )
                        ),
                    ),
                ):
                    app.run()
                    connected_workspace_loaded = any(
                        status.label == "Upstox data loaded" for status in app.status
                    )
                    app.run()
                    app.session_state["app_navigation"] = "F&O"
                    app.run()
                    fno_analysis_actions_present = {
                        button.key for button in app.button
                    }.issuperset({"run_analysis_options", "run_analysis_futures"})
                    app.session_state["app_navigation"] = "Home"
                    app.run()

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["token"], "access-token")
        self.assertEqual(app.session_state["broker_name"], "Upstox")
        self.assertTrue(
            any(
                "GGHP" in item.value and "gghp-name" in item.value
                for item in app.get("html")
            )
        )
        self.assertFalse(app.sidebar.get("title"))
        self.assertFalse(app.sidebar.get("caption"))
        self.assertEqual(
            app.session_state["app_navigation"],
            ":material/home: Home",
        )
        self.assertEqual(
            [tab.label for tab in app.tabs],
            [
                ":material/home: Home",
                ":material/show_chart: Equities",
                ":material/swap_horiz: Trades",
                ":material/query_stats: F&O",
                ":material/savings: Mutual Funds",
            ],
        )
        self.assertFalse(
            any(
                title.value == "Welcome to Wealth Home AI"
                for title in app.title
            )
        )
        popovers = app.get("popover")
        self.assertGreaterEqual(len(popovers), 3)
        self.assertFalse(
            any(
                expander.label == "Share a portfolio update"
                for expander in app.expander
            )
        )
        self.assertTrue(
            {"Email full portfolio report", "Share summary via WhatsApp"}.issubset(
                {button.label for button in app.button}
            )
        )
        self.assertFalse(
            any(
                header.value == "Your portfolio at a glance"
                for header in app.subheader
            )
        )
        self.assertTrue(connected_workspace_loaded)
        self.assertTrue(fno_analysis_actions_present)
        self.assertTrue(any(button.key == "open_user_settings" for button in app.button))
        self.assertFalse(
            any(button.label == "Finish Upstox sign-in" for button in app.button)
        )
        self.assertFalse(
            any(
                "Re-enter the same developer app credentials" in info.value
                for info in app.info
            )
        )

    def test_upstox_authorization_is_prepared_only_after_both_credentials_exist(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "security.oauth._database_path",
                return_value=database_path,
            ), patch(
                "services.user_settings.settings",
                replace(settings, oauth_database_path=str(database_path)),
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py")
                ).run(timeout=20)
                self._register_broker(
                    app,
                    "Upstox",
                    credentials={"api_key": "app-key"},
                    consent=True,
                )
                self.assertIsNone(app.session_state["pending_broker_name"])
                app.button(key="open_user_settings").click().run(timeout=20)
                app.text_input(
                    key="user_settings_Upstox_api_secret"
                ).set_value("app-secret").run(timeout=20)
                app.button(key="save_user_settings").click().run(timeout=20)
                self.assertEqual(app.session_state["pending_broker_name"], "Upstox")
                self.assertTrue(app.session_state["pending_login_url"])

    def test_zerodha_callback_can_resume_without_original_streamlit_session(self):
        oauth_state = secrets.token_urlsafe(32)
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "security.oauth._database_path",
                return_value=database_path,
            ):
                create_oauth_state(
                    oauth_state,
                    {
                        "broker": "Zerodha",
                        "api_key": "kite-key",
                        "api_secret": "kite-secret",
                        "redirect_url": "https://wealth.example.com/",
                    },
                )
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py")
                )
                app.query_params.update(
                    {"request_token": "request-token", "state": oauth_state}
                )
                with (
                    patch(
                        "brokers.factory.ZerodhaAdapter.authenticate",
                        return_value="kite-access-token",
                    ),
                    patch(
                        "brokers.factory.ZerodhaAdapter.fetch_profile",
                        return_value={"name": "Test user", "user_id": "test-user"},
                    ),
                    patch(
                        "brokers.factory.ZerodhaAdapter.fetch_balance",
                        return_value=0,
                    ),
                    patch(
                        "brokers.factory.ZerodhaAdapter.fetch_positions",
                        return_value=pd.DataFrame(
                            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                        ),
                    ),
                    patch(
                        "brokers.factory.ZerodhaAdapter.fetch_holdings",
                        return_value=pd.DataFrame(
                            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                        ),
                    ),
                    patch(
                        "brokers.factory.ZerodhaAdapter.fetch_mutual_fund_holdings",
                        return_value=pd.DataFrame(
                            columns=["Fund", "Folio", "Units", "Avg_NAV", "Latest_NAV"]
                        ),
                    ),
                ):
                    app.run()

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["token"], "kite-access-token")
        self.assertEqual(app.session_state["broker_name"], "Zerodha")

    def test_allocation_combines_assets_and_tracks_short_exposure(self):
        equity = pd.DataFrame(
            [{"Qty": 2, "Avg_Price": 100, "LTP": 120}]
        )
        trading = pd.DataFrame(
            [{"Qty": -1, "Avg_Price": 50, "LTP": 40}]
        )
        mutual_funds = pd.DataFrame(
            [{"Units": 3, "Avg_NAV": 10, "Latest_NAV": 12}]
        )
        summary = calculate_allocation(
            200, equity, trading, mutual_funds
        )

        self.assertEqual(summary["total_investments"], 316)
        self.assertEqual(summary["invested_amount"], 280)
        self.assertEqual(summary["total_portfolio_value"], 516)
        self.assertEqual(summary["total_pnl"], 56)
        self.assertEqual(summary["asset_counts"]["Equity"], 1)
        self.assertEqual(summary["asset_counts"]["Trading"], 1)
        self.assertEqual(summary["asset_counts"]["Mutual funds"], 1)
        self.assertEqual(summary["asset_counts"]["Options"], 0)
        self.assertEqual(summary["asset_counts"]["Futures"], 0)
        self.assertEqual(summary["allocation"]["Trading"], 40)
        self.assertEqual(summary["pnl_by_asset"]["Trading"], 10)
        self.assertEqual(summary["cash"], 200)
        chart = build_allocation_chart(summary)
        self.assertEqual(len(chart.data), 3)
        self.assertTrue(all(trace.type == "bar" for trace in chart.data))
        self.assertEqual(
            {trace.name for trace in chart.data},
            {"Equity", "Trading", "Mutual funds"},
        )
        self.assertEqual(chart.layout.barmode, "stack")
        self.assertEqual(chart.layout.font.size, 13)
        self.assertEqual(chart.layout.legend.font.size, 12)
        self.assertEqual(chart.data[0].textfont.color, "#FFFFFF")
        self.assertAlmostEqual(
            sum(float(trace.x[0]) for trace in chart.data), 100
        )
        light_chart = build_allocation_chart(summary, "light")
        self.assertEqual(light_chart.layout.font.color, "#1F2937")
        self.assertNotEqual(
            chart.data[0].marker.color,
            light_chart.data[0].marker.color,
        )

    def test_allocation_palette_has_accessible_segment_labels_and_omits_empty_classes(self):
        summary = calculate_allocation(
            0,
            pd.DataFrame(
                [{"Ticker": "ABC", "Qty": 1, "Avg_Price": 100, "LTP": 110}]
            ),
            pd.DataFrame(columns=["Ticker", "Qty", "Avg_Price", "LTP"]),
            pd.DataFrame(columns=["Units", "Latest_NAV"]),
        )
        chart = build_allocation_chart(summary, "dark")

        self.assertEqual([trace.name for trace in chart.data], ["Equity"])
        self.assertEqual(chart.data[0].marker.color, "#2563EB")
        self.assertEqual(chart.data[0].textfont.color, "#FFFFFF")
        for palette in ALLOCATION_COLORS.values():
            self.assertEqual(len(set(palette.values())), len(palette))
            for color in palette.values():
                channels = [
                    int(color[index : index + 2], 16) / 255
                    for index in (1, 3, 5)
                ]
                linear = [
                    channel / 12.92
                    if channel <= 0.04045
                    else ((channel + 0.055) / 1.055) ** 2.4
                    for channel in channels
                ]
                luminance = sum(
                    value * weight
                    for value, weight in zip(linear, (0.2126, 0.7152, 0.0722))
                )
                self.assertGreaterEqual(1.05 / (luminance + 0.05), 4.5)

    def test_mutual_fund_import_normalizes_optional_columns(self):
        funds = validate_mutual_funds(
            pd.DataFrame(
                [
                    {
                        "Fund": "Index fund",
                        "Units": 2,
                        "Avg_NAV": 100,
                        "Latest_NAV": 110,
                    }
                ]
            )
        )

        self.assertEqual(funds.loc[0, "Latest_NAV"], 110)
        self.assertEqual(funds.loc[0, "Folio"], "")
        self.assertEqual(funds.loc[0, "ISIN"], "")
        self.assertEqual(funds.loc[0, "NAV_Date"], "")

    def test_zerodha_fetches_mutual_fund_holdings(self):
        adapter = ZerodhaAdapter(api_key="kite-key", api_secret="kite-secret")
        with patch.object(
            adapter,
            "_request",
            return_value=[
                {
                    "fund": "Index fund",
                    "folio": "folio-123",
                    "tradingsymbol": "INF123456789",
                    "quantity": 2.5,
                    "average_price": 100.0,
                    "last_price": 110.0,
                    "last_price_date": "2026-10-01",
                }
            ],
        ) as request:
            holdings = adapter.fetch_mutual_fund_holdings("session-token")

        request.assert_called_once_with("GET", "/mf/holdings", "session-token")
        self.assertEqual(holdings.loc[0, "Fund"], "Index fund")
        self.assertEqual(holdings.loc[0, "Folio"], "folio-123")
        self.assertEqual(holdings.loc[0, "ISIN"], "INF123456789")
        self.assertEqual(holdings.loc[0, "Units"], 2.5)
        self.assertEqual(holdings.loc[0, "Latest_NAV"], 110.0)
        self.assertEqual(holdings.loc[0, "NAV_Date"], "2026-10-01")

    def test_provider_model_defaults_include_multiple_gemini_models(self):
        with patch.dict("os.environ", {"APP_TITLE": ""}, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(
            settings.app_title,
            "GGHP | Governed Portfolio Companion",
        )
        self.assertEqual(
            settings.llm_provider_models["Gemini"],
            (
                "gemini-3.8-flash",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
            ),
        )

    def test_legacy_single_provider_model_environment_values_are_ignored(self):
        with patch.dict(
            "os.environ",
            {
                "LLM_MODEL": "obsolete-model",
                "LLM_MODEL_OPTIONS": "obsolete-model,another-obsolete-model",
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(
            settings.llm_provider_models["Gemini"],
            (
                "gemini-3.8-flash",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
            ),
        )
        self.assertEqual(settings.llm_provider_models["OpenAI"][0], "gpt-4o-mini")

    def test_provider_model_options_are_configured_per_provider(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(
            set(settings.llm_provider_models),
            {
                "Gemini",
                "OpenAI",
                "Anthropic",
                "Groq",
                "Together AI",
                "Mistral",
                "DeepSeek",
                "OpenRouter",
                "xAI",
                "Cerebras",
                "Custom OpenAI-compatible",
            },
        )
        self.assertTrue(
            all(
                models
                for provider, models in settings.llm_provider_models.items()
                if provider != "Custom OpenAI-compatible"
            )
        )
        self.assertEqual(
            settings.llm_provider_models["Custom OpenAI-compatible"], ()
        )
        self.assertIn("gpt-4o-mini", settings.llm_provider_models["OpenAI"])
        self.assertIn(
            "deepseek-reasoner", settings.llm_provider_models["DeepSeek"]
        )

    def test_custom_openai_compatible_uses_freeform_model_id(self):
        app = AppTest.from_string(
            """
import streamlit as st
from ui.user_settings import apply_settings_to_session, render_user_settings

apply_settings_to_session({
    "preferences": {"ai_provider": "Custom OpenAI-compatible"},
    "sensitive": {},
    "sensitive_consent": False,
})
render_user_settings("settings-test-user")
"""
        ).run(timeout=20)
        app.button(key="open_user_settings").click().run(timeout=20)

        self.assertFalse(app.exception)
        app.button(key="add_ai_provider_config").click().run(timeout=20)
        self.assertTrue(
            any(widget.label == "Suggested model" for widget in app.selectbox)
        )
        app.selectbox(key="settings_ai_editor_suggested_model").select(
            "gemini-2.5-flash"
        ).run(timeout=20)
        self.assertEqual(
            app.text_input(key="settings_ai_editor_model").value,
            "gemini-2.5-flash",
        )
        app.selectbox(key="settings_ai_editor_provider").select(
            "Custom OpenAI-compatible"
        ).run(timeout=20)
        self.assertTrue(
            any(widget.label == "Provider name" for widget in app.text_input)
        )
        self.assertTrue(
            any(widget.label == "Model" for widget in app.text_input)
        )

    def test_ai_provider_and_credentials_are_managed_in_user_settings(self):
        app = AppTest.from_string(
            """
import streamlit as st
from ui.user_settings import apply_settings_to_session, render_user_settings

apply_settings_to_session({
    "preferences": {"ai_provider": "Gemini"},
    "sensitive": {},
    "sensitive_consent": False,
})
render_user_settings("settings-test-user")
"""
        ).run(timeout=20)
        app.button(key="open_user_settings").click().run(timeout=20)

        self.assertFalse(app.exception)
        self.assertTrue(
            any(
                widget.label == "+ AI provider config"
                for widget in app.button
            )
        )
        app.button(key="add_ai_provider_config").click().run(timeout=20)
        self.assertTrue(
            any(widget.label == "AI provider" for widget in app.selectbox)
        )
        self.assertTrue(
            any(widget.label == "API key" for widget in app.text_input)
        )
        self.assertTrue(
            any(widget.label == "Test provider setup" for widget in app.button)
        )

    def test_custom_model_and_provider_setup_must_pass_test_before_save(self):
        from services.user_settings import load_user_settings

        app_source = """
import streamlit as st
from ui.user_settings import apply_settings_to_session, render_user_settings
if "ai_test_initialized" not in st.session_state:
    apply_settings_to_session({
        "preferences": {},
        "sensitive": {},
        "sensitive_consent": False,
    })
    st.session_state.ai_test_initialized = True
render_user_settings("ai-provider-test-user")
"""
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "settings.sqlite3"
            with patch(
                "services.user_settings.settings",
                replace(settings, oauth_database_path=str(database_path)),
            ):
                app = AppTest.from_string(app_source).run(timeout=20)
                app.button(key="open_user_settings").click().run(timeout=20)
                app.button(key="add_ai_provider_config").click().run(timeout=20)
                app.selectbox(key="settings_ai_editor_provider").select(
                    "Custom OpenAI-compatible"
                ).run(timeout=20)
                app.text_input(key="settings_ai_editor_provider_name").set_value(
                    "OpenRouter"
                ).run(timeout=20)
                app.text_input(
                    key="settings_ai_editor_api_key"
                ).set_value("test-api-key").run(timeout=20)
                app.text_input(key="settings_ai_editor_model").set_value(
                    "openai/gpt-test"
                ).run(timeout=20)
                app.text_input(key="settings_ai_editor_base_url").set_value(
                    "https://openrouter.example/api/v1"
                ).run(timeout=20)
                app.checkbox(key="settings_sensitive_consent").set_value(
                    True
                ).run(timeout=20)

                with patch(
                    "ui.user_settings.test_ai_provider_configuration"
                ) as test_provider:
                    app.button(key="test_ai_provider_setup").click().run(timeout=20)
                test_provider.assert_called_once_with(
                    provider="Custom OpenAI-compatible",
                    api_key="test-api-key",
                    model="openai/gpt-test",
                    base_url="https://openrouter.example/api/v1",
                )
                app.button(key="save_ai_provider_config").click().run(timeout=20)
                saved = load_user_settings("ai-provider-test-user")
                self.assertEqual(
                    saved["preferences"]["ai_provider_configs"][0]["provider"],
                    "Custom OpenAI-compatible",
                )
                self.assertEqual(
                    saved["preferences"]["ai_provider_configs"][0]["name"],
                    "OpenRouter",
                )
                self.assertEqual(
                    saved["preferences"]["ai_provider_configs"][0]["model"],
                    "openai/gpt-test",
                )
                config_id = saved["preferences"]["ai_provider_configs"][0]["id"]
                self.assertEqual(
                    saved["sensitive"]["ai_provider_config_keys"][config_id],
                    "test-api-key",
                )
                self.assertEqual(len(saved["preferences"]["ai_provider_configs"]), 1)
                config_id = saved["preferences"]["ai_provider_configs"][0]["id"]
                safe_id = hashlib.sha256(config_id.encode("utf-8")).hexdigest()[:12]
                app.button(key=f"edit_ai_config_{safe_id}").click().run(timeout=20)
                self.assertEqual(
                    app.text_input(key="settings_ai_editor_api_key").value,
                    "test-api-key",
                )
                app.text_input(key="settings_ai_editor_model").set_value(
                    "openai/gpt-updated"
                ).run(timeout=20)
                with patch("ui.user_settings.test_ai_provider_configuration"):
                    app.button(key="test_ai_provider_setup").click().run(timeout=20)
                app.button(key="save_ai_provider_config").click().run(timeout=20)
                saved = load_user_settings("ai-provider-test-user")
                self.assertEqual(len(saved["preferences"]["ai_provider_configs"]), 1)
                self.assertEqual(
                    saved["preferences"]["ai_provider_configs"][0]["model"],
                    "openai/gpt-updated",
                )
                app.button(key="add_ai_provider_config").click().run(timeout=20)
                app.selectbox(key="settings_ai_editor_provider").select(
                    "Groq"
                ).run(timeout=20)
                app.text_input(key="settings_ai_editor_api_key").set_value(
                    "groq-test-key"
                ).run(timeout=20)
                app.text_input(key="settings_ai_editor_model").set_value(
                    "groq-custom-model"
                ).run(timeout=20)
                with patch(
                    "ui.user_settings.test_ai_provider_configuration"
                ):
                    app.button(key="test_ai_provider_setup").click().run(timeout=20)
                app.button(key="save_ai_provider_config").click().run(timeout=20)
                saved = load_user_settings("ai-provider-test-user")
                self.assertEqual(len(saved["preferences"]["ai_provider_configs"]), 2)
                groq_config = next(
                    item
                    for item in saved["preferences"]["ai_provider_configs"]
                    if item["provider"] == "Groq"
                )
                self.assertEqual(
                    saved["sensitive"]["ai_provider_config_keys"][
                        groq_config["id"]
                    ],
                    "groq-test-key",
                )
                safe_id = hashlib.sha256(
                    groq_config["id"].encode("utf-8")
                ).hexdigest()[:12]
                app.button(key=f"use_ai_config_{safe_id}").click().run(timeout=20)
                self.assertEqual(app.session_state.llm_provider, "Groq")
                self.assertEqual(
                    app.session_state.user_llm_api_key_groq,
                    "groq-test-key",
                )
                app.button(key="save_user_settings").click().run(timeout=20)
                saved = load_user_settings("ai-provider-test-user")
                self.assertEqual(
                    saved["preferences"]["ai_active_config_id"],
                    groq_config["id"],
                )
                app.button(key="dismiss_settings_saved").click().run(timeout=20)
                app.button(key="open_user_settings").click().run(timeout=20)
                app.button(key=f"delete_ai_config_{safe_id}").click().run(
                    timeout=20
                )
                app.button(key=f"confirm_delete_ai_config_{safe_id}").click().run(
                    timeout=20
                )
                saved = load_user_settings("ai-provider-test-user")
                self.assertEqual(
                    len(saved["preferences"]["ai_provider_configs"]),
                    1,
                )
                self.assertNotIn(
                    groq_config["id"],
                    saved["sensitive"]["ai_provider_config_keys"],
                )
                self.assertFalse(app.exception)

    def test_ai_provider_key_stays_session_only_without_explicit_consent(self):
        from services.user_settings import load_user_settings

        app_source = """
import streamlit as st
from ui.user_settings import apply_settings_to_session, render_user_settings
if "ai_session_only_initialized" not in st.session_state:
    apply_settings_to_session({
        "preferences": {},
        "sensitive": {},
        "sensitive_consent": False,
    })
    st.session_state.ai_session_only_initialized = True
render_user_settings("ai-session-only-user")
"""
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "settings.sqlite3"
            with patch(
                "services.user_settings.settings",
                replace(settings, oauth_database_path=str(database_path)),
            ):
                app = AppTest.from_string(app_source).run(timeout=20)
                app.button(key="open_user_settings").click().run(timeout=20)
                app.button(key="add_ai_provider_config").click().run(timeout=20)
                app.selectbox(key="settings_ai_editor_provider").select(
                    "OpenAI"
                ).run(timeout=20)
                app.text_input(key="settings_ai_editor_api_key").set_value(
                    "private-session-key"
                ).run(timeout=20)
                app.text_input(key="settings_ai_editor_model").set_value(
                    "custom-openai-model"
                ).run(timeout=20)
                with patch("ui.user_settings.test_ai_provider_configuration"):
                    app.button(key="test_ai_provider_setup").click().run(timeout=20)
                app.button(key="save_ai_provider_config").click().run(timeout=20)
                saved = load_user_settings("ai-session-only-user")
                self.assertFalse(saved["sensitive_consent"])
                self.assertEqual(saved["sensitive"], {})
                self.assertEqual(app.session_state.user_llm_api_key_openai, "private-session-key")
                self.assertEqual(
                    app.session_state.settings_ai_provider_config_keys[
                        saved["preferences"]["ai_provider_configs"][0]["id"]
                    ],
                    "private-session-key",
                )
                self.assertFalse(app.exception)

    def test_ai_provider_connection_test_checks_model_and_credentials(self):
        response = MagicMock()
        response.json.return_value = {
            "choices": [{"message": {"content": "READY"}}]
        }
        with patch("services.ai.requests.post", return_value=response) as post:
            verify_ai_provider_configuration(
                provider="Custom OpenAI-compatible",
                api_key="test-key",
                model="test-model",
                base_url="https://api.example.com/v1",
            )
        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0],
            "https://api.example.com/v1/chat/completions",
        )
        self.assertEqual(post.call_args.kwargs["json"]["model"], "test-model")

        gemini_client = MagicMock()
        gemini_client.__enter__.return_value = gemini_client
        gemini_client.models.generate_content.return_value.text = "READY"
        with patch(
            "services.ai.genai.Client",
            return_value=gemini_client,
        ) as create_client:
            verify_ai_provider_configuration(
                provider="Gemini",
                api_key="gemini-test-key",
                model="gemini-test-model",
            )
        create_client.assert_called_once_with(api_key="gemini-test-key")
        self.assertEqual(
            gemini_client.models.generate_content.call_args.kwargs["model"],
            "gemini-test-model",
        )

        with self.assertRaisesRegex(ValueError, "Enter an API key"):
            verify_ai_provider_configuration(
                provider="OpenAI",
                api_key="",
                model="gpt-4o-mini",
            )

    def test_typed_provider_name_uses_custom_compatible_endpoint(self):
        from ui import user_settings

        session_values = {
            "settings_ai_provider": "Proxy Provider",
            "user_settings_ai_key_custom_openai_compatible": "test-key",
            "user_settings_model_custom_openai_compatible": "openai/gpt-test",
            "settings_ai_custom_base_url": "https://openrouter.example/api/v1",
        }
        with patch.object(user_settings.st, "session_state", session_values):
            configuration = user_settings._selected_ai_configuration()

        self.assertEqual(
            configuration,
            {
                "provider": "Custom OpenAI-compatible",
                "provider_selection": "Proxy Provider",
                "custom_provider_name": "Proxy Provider",
                "api_key": "test-key",
                "model": "openai/gpt-test",
                "base_url": "https://openrouter.example/api/v1",
            },
        )

    def test_provider_model_options_can_be_overridden_by_environment(self):
        with patch.dict(
            "os.environ",
            {
                "LLM_PROVIDER_MODELS": (
                    '{"OpenAI":["org-model-a","org-model-b"],'
                    '"Groq":["org-llama"]}'
                )
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(
            settings.llm_provider_models["OpenAI"],
            ("org-model-a", "org-model-b"),
        )
        self.assertEqual(settings.llm_provider_models["Groq"], ("org-llama",))

    def test_analysis_uses_user_provided_key_and_model(self):
        response = MagicMock(text='{"analysis":"review","cash_deployment_list":[]}')
        chat = MagicMock()
        chat.send_message.return_value = response
        client = MagicMock()
        client.__enter__.return_value = client
        client.chats.create.return_value = chat

        with patch("services.ai.genai.Client", return_value=client) as create_client:
            result = ask_llm_agent(
                portfolio_summary="{}",
                available_cash=1000,
                market_context="portfolio only",
                live_prices={},
                api_key="session-key",
                model="selected-model",
            )

        self.assertEqual(result["analysis"], "review")
        self.assertEqual(create_client.call_args.kwargs["api_key"], "session-key")
        self.assertEqual(client.chats.create.call_args.kwargs["model"], "selected-model")

    def test_analysis_rejects_validly_priced_tickers_outside_scan_candidates(self):
        import json

        targets = [
            {
                "Ticker": ticker,
                "Entry_Price": 100,
                "Target_Price": 110,
                "Stop_Loss": 95,
                "Confidence_Score": 70,
                "Risk_Reward_Ratio": 2,
                "Holding_Period": "2-6 weeks",
                "Sector": "Example",
                "Reasoning": "Fits the scenario",
                "Entry_Rationale": "Wait for entry",
                "Risk_Rationale": "Exit below stop",
            }
            for ticker in ("AAA", "BBB")
        ]
        response = MagicMock(
            text=json.dumps(
                {"analysis": "review", "cash_deployment_list": targets}
            )
        )
        chat = MagicMock()
        chat.send_message.return_value = response
        client = MagicMock()
        client.__enter__.return_value = client
        client.chats.create.return_value = chat

        with patch("services.ai.genai.Client", return_value=client):
            result = ask_llm_agent(
                portfolio_summary="{}",
                available_cash=1000,
                market_context="screened candidates",
                live_prices={"AAA": 105, "BBB": 105},
                candidate_tickers=["AAA"],
                api_key="session-key",
                model="selected-model",
            )

        self.assertEqual(
            [target["Ticker"] for target in result["cash_deployment_list"]],
            ["AAA"],
        )
        self.assertIn("only tickers in that", chat.send_message.call_args.args[0])

    def test_analysis_does_not_use_server_gemini_key(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "server-key"}):
            with self.assertRaisesRegex(RuntimeError, "Enter an API key for Gemini"):
                ask_llm_agent(
                    portfolio_summary="{}",
                    available_cash=1000,
                    market_context="portfolio only",
                    live_prices={},
                )

    def test_analysis_supports_openai_compatible_user_credentials(self):
        response = MagicMock()
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"analysis":"review","cash_deployment_list":[]}'
                    }
                }
            ]
        }
        with patch(
            "services.ai.requests.post",
            return_value=response,
        ) as post:
            result = ask_llm_agent(
                portfolio_summary="{}",
                available_cash=1000,
                market_context="portfolio only",
                live_prices={},
                api_key="user-key",
                model="provider-model",
                provider="OpenAI",
            )

        self.assertEqual(result["analysis"], "review")
        self.assertEqual(
            post.call_args.args[0],
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer user-key",
        )
        self.assertEqual(post.call_args.kwargs["json"]["model"], "provider-model")

    def test_analysis_supports_anthropic_user_credentials(self):
        response = MagicMock()
        response.json.return_value = {
            "content": [
                {"type": "text", "text": '{"analysis":"review","cash_deployment_list":[]}'}
            ]
        }
        with patch(
            "services.ai.requests.post",
            return_value=response,
        ) as post:
            result = ask_llm_agent(
                portfolio_summary="{}",
                available_cash=1000,
                market_context="portfolio only",
                live_prices={},
                api_key="user-key",
                model="claude-model",
                provider="Anthropic",
            )

        self.assertEqual(result["analysis"], "review")
        self.assertEqual(
            post.call_args.args[0],
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "user-key")

    def test_custom_provider_rejects_non_local_insecure_endpoints(self):
        with patch("services.ai.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "secure HTTPS"):
                ask_llm_agent(
                    portfolio_summary="{}",
                    available_cash=1000,
                    market_context="portfolio only",
                    live_prices={},
                    api_key="user-key",
                    model="custom-model",
                    provider="Custom OpenAI-compatible",
                    base_url="http://example.com/v1",
                )

        post.assert_not_called()

    def test_upstox_application_credentials_are_shared_across_user_sessions(self):
        first_user_adapter = UpstoxAdapter(
            api_key="shared-app-key", api_secret="shared-app-secret"
        )
        second_user_adapter = UpstoxAdapter(
            api_key="shared-app-key", api_secret="shared-app-secret"
        )
        first_login_url = first_user_adapter.get_login_url(
            "https://wealth.example.com/"
        )
        second_login_url = second_user_adapter.get_login_url(
            "https://wealth.example.com/"
        )

        self.assertTrue(first_user_adapter.configured)
        self.assertTrue(second_user_adapter.configured)
        self.assertIn("client_id=shared-app-key", first_login_url)
        self.assertIn("client_id=shared-app-key", second_login_url)
        self.assertNotEqual(first_user_adapter.oauth_state, second_user_adapter.oauth_state)

    def test_upstox_adapter_accepts_session_credentials(self):
        adapter = BrokerFactory.create_adapter(
            "Upstox", api_key="session-app-key", api_secret="session-secret"
        )
        login_url = adapter.get_login_url("https://wealth.example.com/")

        self.assertTrue(adapter.configured)
        self.assertIn("client_id=session-app-key", login_url)

    def test_zerodha_uses_kite_connect_oauth_and_checksum(self):
        adapter = ZerodhaAdapter(api_key="kite-key", api_secret="kite-secret")
        login_url = adapter.get_login_url("https://wealth.example.com/")
        self.assertIn("api_key=kite-key", login_url)
        self.assertIsNotNone(adapter.oauth_state)

        response = MagicMock()
        response.json.return_value = {
            "status": "success",
            "data": {"access_token": "zerodha-token"},
        }
        with patch(
            "brokers.factory.requests.post",
            return_value=response,
        ) as post:
            token = adapter.authenticate("request-token", "https://wealth.example.com/")

        checksum = hashlib.sha256(
            b"kite-keyrequest-tokenkite-secret"
        ).hexdigest()
        self.assertEqual(token, "zerodha-token")
        self.assertEqual(post.call_args.kwargs["data"]["checksum"], checksum)
        self.assertEqual(adapter.api_secret, "")

    def test_dhan_generates_consent_and_exchanges_user_token(self):
        adapter = DhanAdapter(
            api_key="dhan-app-key",
            api_secret="dhan-app-secret",
            client_id="client-123",
        )
        consent_response = MagicMock()
        consent_response.json.return_value = {
            "consentAppId": "consent-id",
            "status": "success",
        }
        with patch(
            "brokers.factory.requests.post",
            return_value=consent_response,
        ):
            login_url = adapter.get_login_url("https://wealth.example.com/")
        self.assertIn("consentAppId=consent-id", login_url)

        token_response = MagicMock()
        token_response.json.return_value = {
            "dhanClientId": "client-123",
            "accessToken": "dhan-access-token",
        }
        with patch(
            "brokers.factory.requests.get",
            return_value=token_response,
        ) as get:
            token = adapter.authenticate("token-id", "https://wealth.example.com/")

        self.assertEqual(token, "dhan-access-token")
        self.assertEqual(adapter.api_secret, "")
        self.assertEqual(
            get.call_args.kwargs["params"],
            {"tokenId": "token-id"},
        )

    def test_broker_adapters_do_not_read_server_credentials(self):
        with patch.dict(
            "os.environ",
            {
                "UPSTOX_API_KEY": "environment-upstox-key",
                "UPSTOX_API_SECRET": "environment-upstox-secret",
                "ANGEL_API_KEY": "environment-angel-key",
                "ZERODHA_API_KEY": "environment-kite-key",
                "ZERODHA_API_SECRET": "environment-kite-secret",
                "DHAN_CLIENT_ID": "environment-dhan-client",
                "DHAN_API_KEY": "environment-dhan-key",
                "DHAN_API_SECRET": "environment-dhan-secret",
            },
        ):
            configured = Settings.from_environment()
            upstox = UpstoxAdapter()
            zerodha = ZerodhaAdapter()
            dhan = DhanAdapter()
            angel = BrokerFactory.create_adapter("Angel One")

        self.assertFalse(upstox.configured)
        self.assertFalse(zerodha.configured)
        self.assertFalse(dhan.configured)
        self.assertEqual(angel.api_key, "")
        for field_name in (
            "upstox_api_key",
            "upstox_api_secret",
            "angel_api_key",
            "zerodha_api_key",
            "zerodha_api_secret",
            "dhan_client_id",
            "dhan_api_key",
            "dhan_api_secret",
            "whatsapp_auth_token",
            "email_smtp_password",
        ):
            self.assertFalse(hasattr(configured, field_name))

    def test_upstox_sdk_requests_use_configured_timeouts(self):
        adapter = UpstoxAdapter(api_key="app-key", api_secret="app-secret")
        sdk = MagicMock()
        sdk.get_user_fund_margin.return_value.data.equity.available_margin = 12500
        timeout = (
            settings.http_connect_timeout_seconds,
            settings.http_read_timeout_seconds,
        )
        with (
            patch.object(adapter, "_api_client", return_value=object()),
            patch("brokers.factory.UserApi", return_value=sdk),
        ):
            self.assertEqual(adapter.fetch_balance("access-token"), 12500.0)

        sdk.get_user_fund_margin.assert_called_once_with(
            api_version="2.0",
            segment="SEC",
            _request_timeout=timeout,
        )

    def test_whatsapp_alert_uses_session_supplied_credentials(self):
        response = MagicMock()
        with patch(
            "services.notifications.requests.post",
            return_value=response,
        ) as post:
            send_whatsapp_alert(
                "test notification",
                account_sid="session-sid",
                auth_token="session-token",
                sender="whatsapp:+14155238886",
                recipient="whatsapp:+15551234567",
            )

        self.assertIn("/Accounts/session-sid/Messages.json", post.call_args.args[0])
        self.assertEqual(
            post.call_args.kwargs["auth"],
            ("session-sid", "session-token"),
        )
        self.assertEqual(
            post.call_args.kwargs["data"],
            {
                "From": "whatsapp:+14155238886",
                "To": "whatsapp:+15551234567",
                "Body": "test notification",
            },
        )

    def test_email_alert_uses_tls_and_supplied_smtp_credentials(self):
        with patch("services.notifications.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            send_email_alert(
                "test notification",
                smtp_host="smtp.example.com",
                smtp_port=587,
                username="user@example.com",
                password="app-password",
                sender="user@example.com",
                recipient="alerts@example.com",
            )

        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user@example.com", "app-password")
        server.send_message.assert_called_once()

    def test_profile_contacts_replace_the_alerts_settings_tab(self):
        app = AppTest.from_string(
            """
from ui.user_settings import render_user_settings
render_user_settings("settings-test-user")
"""
        ).run()
        app.button(key="open_user_settings").click().run()

        self.assertFalse(app.exception)
        self.assertNotIn("Alerts", {tab.label for tab in app.tabs})
        self.assertTrue(
            any(widget.label == "Email address" for widget in app.text_input)
        )
        self.assertTrue(
            any(
                widget.label == "10-digit phone number"
                for widget in app.text_input
            )
        )
        self.assertTrue(
            any(widget.label == "Country code" for widget in app.selectbox)
        )
        self.assertFalse(
            any(
                widget.label in {"SMTP server", "Twilio Account SID", "Email recipient"}
                for widget in app.text_input
            )
        )
        self.assertTrue(
            {
                widget.label for widget in app.number_input
            }.issuperset(
                {"Maximum scenario allocation (%)", "Realized daily loss (%)"}
            )
        )
        self.assertFalse(
            any("Telegram" in widget.label for widget in app.text_input)
        )

    def test_saved_profile_contacts_are_prepopulated(self):
        app = AppTest.from_string(
            """
from ui.user_settings import apply_settings_to_session, render_user_settings
apply_settings_to_session({
    "preferences": {},
    "sensitive": {
        "profile": {
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "phone": "+14165551234",
            "phone_country": "Canada",
        },
    },
    "sensitive_consent": True,
})
render_user_settings("settings-test-user")
"""
        ).run()
        app.button(key="open_user_settings").click().run()

        self.assertFalse(app.exception)
        values_by_label = {widget.label: widget.value for widget in app.text_input}
        self.assertEqual(values_by_label["Your name"], "Ada Lovelace")
        self.assertEqual(values_by_label["Email address"], "ada@example.com")
        self.assertEqual(
            values_by_label["10-digit phone number"],
            "4165551234",
        )
        self.assertEqual(
            next(
                widget.value
                for widget in app.selectbox
                if widget.label == "Country code"
            ),
            "🇨🇦 Canada (+1)",
        )
        country_options = next(
            widget.options
            for widget in app.selectbox
            if widget.label == "Country code"
        )
        self.assertIn("🇮🇳 India (+91)", country_options)
        self.assertIn("🇨🇦 Canada (+1)", country_options)

    def test_profile_shows_email_and_phone_validation_errors_as_typed(self):
        app = AppTest.from_string(
            """
from ui.user_settings import render_user_settings
render_user_settings("settings-test-user")
"""
        ).run()
        app.button(key="open_user_settings").click().run()
        app.text_input(key="settings_profile_email").set_value("invalid-email").run()
        app.text_input(key="settings_profile_phone_national").set_value(
            "12345abcde"
        ).run()

        self.assertFalse(app.exception)
        errors = [element.value for element in app.error]
        self.assertTrue(any("valid email address" in error for error in errors))
        self.assertTrue(
            any("exactly 10 digits" in error for error in errors)
        )

    def test_selected_flag_country_code_is_combined_for_saved_phone(self):
        app = AppTest.from_string(
            """
import streamlit as st
from ui.user_settings import _collect_sensitive_settings
st.session_state["settings_profile_phone_country"] = "🇨🇦 Canada (+1)"
st.session_state["settings_profile_phone_national"] = "4165551234"
st.session_state["collected_settings"] = _collect_sensitive_settings()
"""
        ).run()

        self.assertFalse(app.exception)
        saved_profile = app.session_state["collected_settings"]["profile"]
        self.assertEqual(saved_profile["phone"], "+14165551234")
        self.assertEqual(saved_profile["phone_country"], "Canada")

    def test_zerodha_reads_cash_positions_and_live_quotes(self):
        adapter = ZerodhaAdapter(api_key="kite-key", api_secret="kite-secret")
        with patch.object(
            adapter,
            "_request",
            side_effect=[
                {"available": {"cash": 12500.0}},
                {
                    "net": [
                        {
                            "exchange": "NSE",
                            "tradingsymbol": "RELIANCE",
                            "quantity": 2,
                            "average_price": 100.0,
                            "last_price": 105.0,
                        },
                        {
                            "exchange": "NFO",
                            "tradingsymbol": "RELIANCE",
                            "quantity": 25,
                            "average_price": 10.0,
                            "last_price": 12.0,
                        },
                    ]
                },
                {"NSE:RELIANCE": {"last_price": 105.0}},
            ],
        ) as request:
            self.assertEqual(adapter.fetch_balance("session-token"), 12500.0)
            positions = adapter.fetch_positions("session-token")
            prices = adapter.fetch_live_prices("session-token", ["RELIANCE"])

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions.loc[0, "Ticker"], "RELIANCE")
        self.assertEqual(prices, {"RELIANCE": 105.0})
        self.assertEqual(request.call_count, 3)

    def test_zerodha_quotes_dynamic_nse_symbols_outside_static_ticker_map(self):
        adapter = ZerodhaAdapter(api_key="kite-key", api_secret="kite-secret")
        with patch.object(
            adapter,
            "_request",
            return_value={"NSE:NEWLISTING": {"last_price": 125.5}},
        ) as request:
            prices = adapter.fetch_live_prices("session-token", ["NEWLISTING"])

        self.assertEqual(prices, {"NEWLISTING": 125.5})
        self.assertEqual(
            request.call_args.kwargs["params"], {"i": ["NSE:NEWLISTING"]}
        )

    def test_dhan_fetches_balance_and_maps_live_quote(self):
        adapter = DhanAdapter(
            api_key="dhan-key",
            api_secret="dhan-secret",
            client_id="client-123",
        )
        adapter._security_ids_by_ticker = {"RELIANCE": "2885"}
        with patch.object(
            adapter,
            "_request",
            side_effect=[
                {"availabelBalance": "4200.50"},
                {
                    "status": "success",
                    "data": {
                        "NSE_EQ": {"2885": {"last_price": 1540.25}}
                    },
                },
            ],
        ) as request:
            balance = adapter.fetch_balance("session-token")
            prices = adapter.fetch_live_prices(
                "session-token", ["RELIANCE"]
            )

        self.assertEqual(balance, 4200.50)
        self.assertEqual(prices, {"RELIANCE": 1540.25})
        self.assertEqual(request.call_args.kwargs["body"], {"NSE_EQ": [2885]})

    def test_oauth_states_support_concurrent_user_signins_independently(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "security.oauth._database_path",
                return_value=database_path,
            ):
                create_oauth_state("user-one-state")
                create_oauth_state("user-two-state")

                self.assertTrue(consume_oauth_state("user-two-state"))
                self.assertTrue(consume_oauth_state("user-one-state"))
                self.assertFalse(consume_oauth_state("user-one-state"))
                self.assertFalse(consume_oauth_state("unknown-state"))

    def test_oauth_callback_credentials_are_encrypted_and_consumed_once(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "security.oauth._database_path",
                return_value=database_path,
            ):
                create_oauth_state(
                    "encrypted-state",
                    {
                        "broker": "Upstox",
                        "api_key": "sensitive-app-key",
                        "api_secret": "sensitive-app-secret",
                        "redirect_url": "https://wealth.example.com/",
                    },
                )

                database_bytes = database_path.read_bytes()
                self.assertNotIn(b"sensitive-app-secret", database_bytes)
                self.assertEqual(
                    consume_oauth_state_context("encrypted-state"),
                    {
                        "broker": "Upstox",
                        "api_key": "sensitive-app-key",
                        "api_secret": "sensitive-app-secret",
                        "redirect_url": "https://wealth.example.com/",
                    },
                )
                self.assertIsNone(
                    consume_oauth_state_context("encrypted-state")
                )


if __name__ == "__main__":
    unittest.main()
