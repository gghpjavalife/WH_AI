import unittest
import hashlib
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from wealth_home_ai.features.operations.mutual_funds import validate_mutual_funds
from wealth_home_ai.features.home import build_allocation_chart
from wealth_home_ai.features.portfolio import (
    calculate_allocation,
    empty_debt_holdings,
    validate_debt_holdings,
)
from wealth_home_ai.settings import Settings
from wealth_home_ai.oauth_state_store import (
    consume_oauth_state,
    consume_oauth_state_context,
    create_oauth_state,
)
from wealth_home_ai.upstox_helper import ask_llm_agent, send_telegram_alert
from wealth_home_ai.ui_helpers import (
    broker_connect_button_css,
    same_tab_link_html,
)
from wealth_home_ai.broker_factory import (
    BrokerFactory,
    DhanAdapter,
    UpstoxAdapter,
    ZerodhaAdapter,
)


class PortfolioFeatureTests(unittest.TestCase):
    def test_broker_sign_in_links_navigate_in_the_same_tab(self):
        markup = same_tab_link_html(
            "Connect with Upstox",
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

    def test_broker_login_forms_reject_missing_required_credentials(self):
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
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py")
                ).run()
                app.selectbox[0].select(broker).run()
                if element_type == "button":
                    button = next(
                        button
                        for button in app.button
                        if button.label == button_label
                    )
                    self.assertTrue(button.disabled)
                self.assertFalse(app.exception)
                self.assertIsNone(app.session_state.get("pending_broker_name"))

    def test_connect_controls_update_when_credentials_are_entered(self):
        angel_app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        ).run()
        angel_app.selectbox[0].select("Angel One").run()
        for key, value in (
            ("angel_api_key", "api-key"),
            ("angel_client_id", "client-id"),
            ("angel_password", "password"),
            ("angel_totp_secret", "totp-secret"),
        ):
            angel_app.text_input(key=key).set_value(value).run()
        angel_button = next(
            button
            for button in angel_app.button
            if button.label == "Connect with Angel One →"
        )
        self.assertFalse(angel_button.disabled)
        self.assertFalse(angel_app.exception)

        dhan_app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py")
        ).run()
        dhan_app.selectbox[0].select("Dhan").run()
        for key, value in (
            ("dhan_client_id", "client-id"),
            ("dhan_api_key", "api-key"),
            ("dhan_api_secret", "api-secret"),
        ):
            dhan_app.text_input(key=key).set_value(value).run()
        dhan_button = next(
            button
            for button in dhan_app.button
            if button.label == "Connect with Dhan →"
        )
        self.assertFalse(dhan_button.disabled)
        self.assertFalse(dhan_app.exception)

    def test_upstox_callback_can_resume_without_original_streamlit_session(self):
        oauth_state = secrets.token_urlsafe(32)
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "wealth_home_ai.oauth_state_store._database_path",
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
                with (
                    patch(
                        "wealth_home_ai.broker_factory.UpstoxAdapter.authenticate",
                        return_value="access-token",
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.UpstoxAdapter.fetch_balance",
                        return_value=0,
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.UpstoxAdapter.fetch_positions",
                        return_value=pd.DataFrame(
                            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                        ),
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.UpstoxAdapter.fetch_holdings",
                        return_value=pd.DataFrame(
                            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                        ),
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.UpstoxAdapter.fetch_mutual_fund_holdings",
                        return_value=pd.DataFrame(
                            columns=["Fund", "Folio", "Units", "Avg_NAV", "Latest_NAV"]
                        ),
                    ),
                ):
                    app.run()
                app.run()

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["token"], "access-token")
        self.assertEqual(app.session_state["broker_name"], "Upstox")
        self.assertTrue(
            any(header.value == "Wealth Home AI" for header in app.header)
        )
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
                "wealth_home_ai.oauth_state_store._database_path",
                return_value=database_path,
            ):
                app = AppTest.from_file(
                    str(Path(__file__).resolve().parents[1] / "app.py")
                ).run()
                app.selectbox[0].select("Upstox").run()
                app.text_input(key="upstox_api_key").set_value("app-key").run()
                self.assertIsNone(app.session_state["pending_broker_name"])

                app.text_input(key="upstox_api_secret").set_value("app-secret").run()
                self.assertEqual(app.session_state["pending_broker_name"], "Upstox")
                self.assertTrue(app.session_state["pending_login_url"])

    def test_zerodha_callback_can_resume_without_original_streamlit_session(self):
        oauth_state = secrets.token_urlsafe(32)
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "oauth.sqlite3"
            with patch(
                "wealth_home_ai.oauth_state_store._database_path",
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
                        "wealth_home_ai.broker_factory.ZerodhaAdapter.authenticate",
                        return_value="kite-access-token",
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.ZerodhaAdapter.fetch_balance",
                        return_value=0,
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.ZerodhaAdapter.fetch_positions",
                        return_value=pd.DataFrame(
                            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                        ),
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.ZerodhaAdapter.fetch_holdings",
                        return_value=pd.DataFrame(
                            columns=["Ticker", "Qty", "Avg_Price", "LTP"]
                        ),
                    ),
                    patch(
                        "wealth_home_ai.broker_factory.ZerodhaAdapter.fetch_mutual_fund_holdings",
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
        debt = validate_debt_holdings(
            pd.DataFrame(
                [
                    {
                        "Instrument": "Bond",
                        "Principal": 1000,
                        "Current_Value": 1050,
                        "Annual_Rate_%": 5,
                        "Maturity_Date": "2030-01-01",
                    }
                ]
            )
        )

        summary = calculate_allocation(
            200, equity, trading, mutual_funds, debt
        )

        self.assertEqual(summary["total_investments"], 1366)
        self.assertEqual(summary["invested_amount"], 1280)
        self.assertEqual(summary["total_portfolio_value"], 1566)
        self.assertEqual(summary["total_pnl"], 106)
        self.assertEqual(summary["asset_counts"]["Equity"], 1)
        self.assertEqual(summary["asset_counts"]["Trading"], 1)
        self.assertEqual(summary["asset_counts"]["Debt"], 1)
        self.assertEqual(summary["asset_counts"]["Mutual funds"], 1)
        self.assertEqual(summary["asset_counts"]["Options"], 0)
        self.assertEqual(summary["asset_counts"]["Futures"], 0)
        self.assertEqual(summary["allocation"]["Trading"], 40)
        self.assertEqual(summary["pnl_by_asset"]["Trading"], 10)
        self.assertEqual(summary["cash"], 200)
        chart = build_allocation_chart(summary)
        self.assertEqual(len(chart.data), 6)
        self.assertTrue(all(trace.type == "bar" for trace in chart.data))
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

    def test_debt_validation_drops_blank_editor_rows_and_rejects_negative_values(self):
        debt = pd.DataFrame(
            [
                {
                    "Instrument": "",
                    "Principal": None,
                    "Current_Value": None,
                    "Annual_Rate_%": None,
                    "Maturity_Date": "",
                },
                {
                    "Instrument": "Fixed deposit",
                    "Principal": 1000,
                    "Current_Value": 1020,
                    "Annual_Rate_%": 5,
                    "Maturity_Date": "",
                },
            ]
        )

        self.assertEqual(len(validate_debt_holdings(debt)), 1)
        with self.assertRaises(ValueError):
            validate_debt_holdings(
                debt.assign(Principal=[None, -1])
            )
        with self.assertRaises(ValueError):
            validate_debt_holdings(
                debt.assign(Maturity_Date=["", "not-a-date"])
            )
        self.assertTrue(empty_debt_holdings().empty)

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
        self.assertEqual(funds.loc[0, "NAV_Date"], "")

    def test_provider_model_defaults_include_multiple_gemini_models(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_environment()

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
                "OpenRouter",
                "Groq",
                "Together AI",
                "Mistral",
                "DeepSeek",
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
from wealth_home_ai.features.analysis import render_analysis_panel

st.session_state.setdefault("llm_provider_settings", {})
st.session_state.setdefault("ai_settings_prompt", "")
st.session_state.setdefault("analysis_errors", {})
st.session_state.setdefault("analysis_price_errors", {})
st.session_state.setdefault("analysis_results", {})
st.session_state.setdefault("analysis_mode", "Portfolio and cash review")
st.session_state.setdefault("llm_provider", "Custom OpenAI-compatible")
render_analysis_panel("all", "portfolio", lambda scope: None, lambda scope: None)
"""
        ).run()

        self.assertFalse(app.exception)
        self.assertTrue(
            any(widget.label == "Model ID" for widget in app.text_input)
        )
        self.assertFalse(any(widget.label == "Model" for widget in app.selectbox))

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

        with patch("wealth_home_ai.upstox_helper.genai.Client", return_value=client) as create_client:
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
            "wealth_home_ai.upstox_helper.requests.post",
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
            "wealth_home_ai.upstox_helper.requests.post",
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
        with patch("wealth_home_ai.upstox_helper.requests.post") as post:
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
            "wealth_home_ai.broker_factory.requests.post",
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
            "wealth_home_ai.broker_factory.requests.post",
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
            "wealth_home_ai.broker_factory.requests.get",
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
                "TELEGRAM_BOT_TOKEN": "environment-telegram-token",
                "TELEGRAM_CHAT_ID": "environment-telegram-chat",
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
            "telegram_bot_token",
            "telegram_chat_id",
        ):
            self.assertFalse(hasattr(configured, field_name))

    def test_telegram_alert_uses_session_supplied_token_and_chat(self):
        response = MagicMock()
        response.json.return_value = {"ok": True}
        with patch(
            "wealth_home_ai.upstox_helper.requests.post",
            return_value=response,
        ) as post:
            send_telegram_alert(
                "test notification",
                chat_id="session-chat",
                bot_token="session-bot-token",
            )

        self.assertIn("/botsession-bot-token/sendMessage", post.call_args.args[0])
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"chat_id": "session-chat", "text": "test notification"},
        )

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
                "wealth_home_ai.oauth_state_store._database_path",
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
                "wealth_home_ai.oauth_state_store._database_path",
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
