import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from services.stock_context_upstox import (
    fetch_upstox_news, fetch_upstox_quote, fetch_upstox_shareholdings,
    fetch_upstox_volume_baseline, quote_fields,
    parse_promoter_pledge,
    equity_isin, fetch_upstox_corporate_actions, upcoming_corporate_action,
)


class UpstoxContextTests(unittest.TestCase):
    def test_isin_and_circuit_quote_fields(self):
        self.assertEqual(equity_isin("NSE_EQ|INE002A01018"), "INE002A01018")
        self.assertEqual(equity_isin("BSE_EQ|INE002A01018"), "INE002A01018")
        self.assertIsNone(equity_isin("NSE_FO|INE002A01018"))
        self.assertIsNone(equity_isin("NSE_EQ|RELIANCE"))
        fields = quote_fields({"last_price": 101, "lower_circuit_limit": 100,
                               "upper_circuit_limit": 120})
        self.assertEqual(fields["distance_to_lower_circuit_pct"], 1)
        self.assertEqual(fields["upper_circuit_limit"], 120)
        self.assertEqual(quote_fields({"last_price": 99, "lower_circuit_limit": 100})
                         ["distance_to_lower_circuit_pct"], -1)
        for bad in (None, 0, "bad", float("nan"), True, -5):
            with self.subTest(bad=bad):
                self.assertIsNone(quote_fields({"last_price": 100, "lower_circuit_limit": bad})
                                  ["distance_to_lower_circuit_pct"])
        fields = quote_fields({"last_price": 100, "lower_circuit_limit": 120, "upper_circuit_limit": 90})
        self.assertIsNone(fields["lower_circuit_limit"])
        self.assertIsNone(fields["upper_circuit_limit"])

    @patch("services.stock_context_upstox.configured_value", return_value="test-token")
    def test_corporate_actions_fetch_and_event_selection(self, configured):
        session = Mock()
        session.get.return_value.json.return_value = {"status": "success", "data": []}
        payload = fetch_upstox_corporate_actions("NSE_EQ|INE002A01018", session=session)
        self.assertIn("/fundamentals/INE002A01018/corporate-actions", session.get.call_args.args[0])
        today = date(2026, 1, 1)
        self.assertEqual(upcoming_corporate_action(payload, today=today), "NONE")
        payload["data"] = [
            {"name": "Dividend", "expiry_date": "2026-01-03",
             "event_details": [{"name": "Ex dividend date", "value": "02 Jan 2026"}]},
            {"name": "Split", "expiry_date": "2026-01-04"},
            {"name": "Bonus", "expiry_date": "2026-01-04"},
        ]
        self.assertEqual(upcoming_corporate_action(payload, today=today), "SPLIT")
        self.assertEqual(upcoming_corporate_action({"data": payload["data"][:1]}, today=today), "DIVIDEND_EX")
        self.assertEqual(upcoming_corporate_action({"data": [{"name": "Bonus", "expiry_date": None}]},
                                                    today=today), None)
        self.assertEqual(upcoming_corporate_action({"data": [{"name": "Split", "expiry_date": "2026-02-01"}]},
                                                    today=today), "NONE")
        with self.assertRaises(ValueError):
            fetch_upstox_corporate_actions("NSE_FO|INE002A01018", session=session)
        with self.assertRaises(ValueError):
            upcoming_corporate_action({"data": None}, today=today)

    def test_pledge_fallback_is_distinct_from_verified_zero(self):
        self.assertEqual(parse_promoter_pledge({}), (None, False))
        self.assertEqual(parse_promoter_pledge({"promoter_pledged_shares_pct": 0.0}), (0.0, False))
        for value in ("bad", None, True, float("nan"), -1, 1.1, {}):
            with self.subTest(value=value):
                self.assertEqual(parse_promoter_pledge({"promoter_pledged_shares_pct": value}), (0.0, False))
    @patch("services.stock_context_upstox.configured_value", return_value="test-token")
    def test_quote_requires_instrument_match_and_maps_spread(self, configured):
        response = Mock()
        response.json.return_value = {"data": {"NSE_EQ:ABC": {
            "instrument_token": "NSE_EQ|TEST", "last_price": 105, "average_price": 100,
            "depth": {"buy": [{"price": 104}], "sell": [{"price": 106}]},
        }}}
        session = Mock()
        session.get.return_value = response
        quote = fetch_upstox_quote("NSE_EQ|TEST", session=session)
        mapped = quote_fields(quote)
        self.assertAlmostEqual(mapped["distance_from_vwap_pct"], 5)
        self.assertEqual(mapped["bid_ask_spread"], 2)
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer test-token")
        with self.assertRaises(ValueError):
            fetch_upstox_quote("NSE_EQ|OTHER", session=session)

    def test_crossed_market_is_not_a_valid_spread(self):
        result = quote_fields({"depth": {
            "buy": [{"price": 11}], "sell": [{"price": 10}],
        }})
        self.assertIsNone(result["bid_ask_spread"])

    @patch("services.stock_context_upstox.configured_value", return_value="test-token")
    def test_paginated_news_retains_all_pages(self, configured):
        key = "NSE_EQ|INE002A01018"
        def response(page_number):
            page = Mock()
            page.json.return_value = {
                "status": "success",
                "data": {key: [{"heading": f"Headline {page_number}",
                                "article_link": f"https://example.com/{page_number}",
                                "published_time": 1767607200000, "summary": "Full summary"}]},
                "metadata": {"page": {"page_number": page_number, "page_size": 100,
                                      "total_pages": 2, "total_records": 2}},
            }
            return page
        session = Mock()
        session.get.side_effect = [response(1), response(2)]
        headlines, raw = fetch_upstox_news(key, session=session)
        self.assertEqual(len(headlines), 2)
        self.assertEqual(raw[1]["data"][key][0]["summary"], "Full summary")
        self.assertEqual(session.get.call_args.kwargs["params"]["page_number"], 2)
        self.assertIsNone(headlines[0]["raw_sentiment_score"])

    @patch("services.stock_context_upstox.configured_value", return_value="test-token")
    def test_news_missing_metadata_or_unretrievable_pages_fails(self, configured):
        session = Mock()
        session.get.return_value.json.return_value = {"data": {}}
        with self.assertRaises(ValueError):
            fetch_upstox_news("NSE_EQ|INE002A01018", session=session)
        session.get.return_value.json.return_value = {
            "data": {}, "metadata": {"page": {"page_number": 1, "total_pages": 101, "total_records": 10001}}
        }
        with self.assertRaises(ValueError):
            fetch_upstox_news("NSE_EQ|INE002A01018", session=session)

    @patch("services.stock_context_upstox.configured_value", return_value="test-token")
    def test_shareholding_is_not_a_pledge(self, configured):
        session = Mock()
        session.get.return_value.json.return_value = {
            "data": [{"category": "promoters", "history": [{"period": "Mar 2026", "value": 50}]}]
        }
        result = fetch_upstox_shareholdings("NSE_EQ|INE002A01018", session=session)
        self.assertEqual(result["data"][0]["history"][0]["value"], 50)
        with self.assertRaises(ValueError):
            fetch_upstox_shareholdings("NSE_EQ|../../bad", session=session)

    @patch("services.stock_context_upstox.configured_value", return_value="test-token")
    def test_baseline_requires_complete_sessions_and_preserves_candles(self, configured):
        zone = ZoneInfo("Asia/Kolkata")
        today = datetime.now(zone).date()
        start, end = today - timedelta(days=4), today - timedelta(days=1)
        candles = []
        for day in (start, start + timedelta(days=1)):
            opening = datetime.combine(day, time(9, 15), tzinfo=zone)
            candles.extend([[(opening + timedelta(minutes=i)).isoformat(), 1, 1, 1, 1, 10, 0]
                            for i in range(375)])
        candles.append([datetime.combine(end, time(9, 15), tzinfo=zone).isoformat(), 1, 1, 1, 1, 10, 0])
        session = Mock()
        session.get.return_value.json.return_value = {"data": {"candles": candles}}
        baseline, raw, warnings = fetch_upstox_volume_baseline(
            "NSE_EQ|INE002A01018", start, end, min_sessions=2, session=session
        )
        self.assertEqual(baseline["09:15"], 10)
        self.assertEqual(baseline["15:29"], 3750)
        self.assertEqual(len(raw["data"]["candles"]), 751)
        self.assertIn("Excluded 1", warnings[0])
        with self.assertRaises(ValueError):
            fetch_upstox_volume_baseline("NSE_EQ|INE002A01018", start, end, min_sessions=3, session=session)


if __name__ == "__main__":
    unittest.main()
