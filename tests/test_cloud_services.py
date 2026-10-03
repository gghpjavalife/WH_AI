import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from wealth_home_ai import broker_factory, cloud_services
from wealth_home_ai.app_help import answer_app_help_question
from wealth_home_ai.broker_factory import (
    BrokerCapabilityError,
    GenericDynamicAdapter,
)
from wealth_home_ai.settings import settings
from wealth_home_ai.upstox_helper import (
    ask_llm_agent,
    send_whatsapp_update,
    whatsapp_update_is_configured,
)


class CloudServiceTests(unittest.TestCase):
    def test_turso_uses_https_hrana_and_parameterized_arguments(self):
        response = Mock()
        response.json.return_value = {
            "results": [
                {
                    "type": "ok",
                    "response": {
                        "type": "execute",
                        "result": {
                            "cols": [{"name": "name"}, {"name": "balance"}],
                            "rows": [
                                [
                                    {"type": "text", "value": "Ada"},
                                    {"type": "float", "value": 1250.5},
                                ]
                            ],
                        },
                    },
                },
                {"type": "ok", "response": {"type": "close"}},
            ]
        }
        with (
            patch.dict(
                os.environ,
                {
                    "TURSO_PRIMARY_DB_URL": "libsql://portfolio-example.turso.io",
                    "TURSO_AUTH_TOKEN": "session-test-token",
                },
                clear=True,
            ),
            patch("wealth_home_ai.cloud_services.requests.post", return_value=response) as post,
        ):
            rows = cloud_services.execute_turso_query(
                "SELECT name, balance FROM users WHERE id = ?",
                ("tenant-1",),
            )

        self.assertEqual(rows, [{"name": "Ada", "balance": 1250.5}])
        self.assertEqual(
            post.call_args.args[0],
            "https://portfolio-example.turso.io/v2/pipeline",
        )
        request_body = post.call_args.kwargs["json"]["requests"][0]["stmt"]
        self.assertEqual(request_body["sql"], "SELECT name, balance FROM users WHERE id = ?")
        self.assertEqual(
            request_body["args"], [{"type": "text", "value": "tenant-1"}]
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer session-test-token",
        )

    def test_turso_rejects_non_https_database_urls_before_network_request(self):
        with (
            patch.dict(
                os.environ,
                {
                    "TURSO_PRIMARY_DB_URL": "http://portfolio-example.turso.io",
                    "TURSO_AUTH_TOKEN": "session-test-token",
                },
                clear=True,
            ),
            patch("wealth_home_ai.cloud_services.requests.post") as post,
        ):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                cloud_services.execute_turso_query("SELECT 1")

        post.assert_not_called()

    def test_resend_sends_html_through_authenticated_https_endpoint(self):
        response = Mock()
        response.json.return_value = {"id": "email-123"}
        with patch(
            "wealth_home_ai.cloud_services.requests.post", return_value=response
        ) as post:
            message_id = cloud_services.send_resend_email(
                "Portfolio",
                "<p>Summary</p>",
                "user@example.com",
                api_key="resend-test-key",
                sender="Wealth Home <updates@example.com>",
            )

        self.assertEqual(message_id, "email-123")
        self.assertEqual(post.call_args.args[0], "https://api.resend.com/emails")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer resend-test-key",
        )
        self.assertEqual(post.call_args.kwargs["json"]["html"], "<p>Summary</p>")

    def test_whatsapp_uses_server_configuration_without_exposing_it_to_ui(self):
        configuration = {
            "TWILIO_ACCOUNT_SID": "AC-test",
            "TWILIO_AUTH_TOKEN": "twilio-test-token",
            "TWILIO_WHATSAPP_SENDER": "whatsapp:+14155238886",
            "TWILIO_WHATSAPP_RECIPIENT": "whatsapp:+15551234567",
        }
        with (
            patch.dict(os.environ, configuration, clear=True),
            patch("wealth_home_ai.notifications.requests.post") as post,
        ):
            self.assertTrue(whatsapp_update_is_configured())
            send_whatsapp_update("Portfolio update")

        self.assertEqual(
            post.call_args.args[0],
            "https://api.twilio.com/2010-04-01/Accounts/AC-test/Messages.json",
        )
        self.assertEqual(
            post.call_args.kwargs["auth"],
            ("AC-test", "twilio-test-token"),
        )

    def test_dynamic_adapter_requires_allowlisted_host_and_is_read_only(self):
        allowlisted = replace(
            settings,
            dynamic_broker_allowed_hosts=("api.broker.example",),
        )
        configuration = {
            "name": "Example broker",
            "api_base": "https://api.broker.example",
            "endpoints": {
                "balance": "/balance",
                "positions": "/positions",
                "holdings": "/holdings",
            },
        }
        with patch.object(broker_factory, "settings", allowlisted):
            adapter = GenericDynamicAdapter(configuration)
            self.assertEqual(adapter.authenticate("session-token", ""), "session-token")
            with self.assertRaises(BrokerCapabilityError):
                adapter.place_order("session-token", "TCS", 1, "BUY")
            with self.assertRaisesRegex(ValueError, "allow-listed"):
                GenericDynamicAdapter(
                    {
                        **configuration,
                        "api_base": "https://attacker.example",
                    }
                )
            with self.assertRaisesRegex(ValueError, "safe path"):
                GenericDynamicAdapter(
                    {
                        **configuration,
                        "endpoints": {
                            **configuration["endpoints"],
                            "positions": "//attacker.example/steal",
                        },
                    }
                )
        self.assertFalse(adapter.supports_live_orders)

    def test_dynamic_broker_registry_scopes_records_to_hashed_tenant(self):
        mock_query = Mock(return_value=[])
        configuration = {
            "api_base": "https://api.broker.example",
            "endpoints": {
                "balance": "/balance",
                "positions": "/positions",
                "holdings": "/holdings",
            },
        }
        with (
            patch.object(
                broker_factory,
                "settings",
                replace(
                    settings,
                    dynamic_broker_allowed_hosts=("api.broker.example",),
                ),
            ),
            patch("wealth_home_ai.cloud_services.execute_turso_query", mock_query),
        ):
            cloud_services.register_dynamic_broker(
                "person@example.com", "Example broker", configuration
            )

        statement, arguments = mock_query.call_args_list[-1].args
        self.assertIn("ON CONFLICT(owner_id, name)", statement)
        self.assertNotIn("person@example.com", arguments)
        self.assertEqual(len(arguments[0]), 64)
        self.assertEqual(arguments[1], "Example broker")
        self.assertNotIn("access_token", arguments[2])

    def test_app_help_answers_compound_questions_locally_without_provider_keys(self):
        answer = answer_app_help_question(
            "How can I scan stocks without a broker, and is live F&O scanning available?"
        )

        self.assertIn("Market research and scanner", answer)
        self.assertIn("works without a broker", answer)
        self.assertIn("F&O support", answer)
        self.assertIn("not integrated yet", answer)

    def test_app_help_explains_provider_keys_are_only_for_portfolio_analysis(self):
        answer = answer_app_help_question("Why do I need an AI API key?")

        self.assertIn("Portfolio analysis is optional", answer)
        self.assertIn("Ask about this Agent does not use that key", answer)

    def test_positional_analysis_signature_uses_selected_provider(self):
        response = Mock()
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
            "wealth_home_ai.upstox_helper.requests.post", return_value=response
        ) as post:
            result = ask_llm_agent(
                "provider-key",
                "portfolio",
                1000,
                provider="Groq",
                model="llama-3.3-70b-versatile",
            )

        self.assertEqual(result["analysis"], "review")
        self.assertEqual(
            post.call_args.args[0],
            "https://api.groq.com/openai/v1/chat/completions",
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["model"],
            "llama-3.3-70b-versatile",
        )


if __name__ == "__main__":
    unittest.main()
