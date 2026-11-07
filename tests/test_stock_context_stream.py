import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.stock_context_stream import UpstoxMicrostructureStream


class UpstoxStreamingTests(unittest.TestCase):
    def test_complete_opening_range_and_staleness(self):
        zone = ZoneInfo("Asia/Kolkata")
        start = datetime(2026, 1, 5, 9, 15, tzinfo=zone)
        stream = UpstoxMicrostructureStream("NSE_EQ|TEST", {"09:29": 750})
        def ms(value):
            return str(int(value.timestamp() * 1000))
        for i in range(15):
            observed = start + timedelta(minutes=i + 1)
            stream.on_message({"currentTs": ms(observed), "feeds": {"NSE_EQ|TEST": {
                "requestMode": "full_d30", "fullFeed": {"marketFF": {
                    "ltpc": {"ltp": 103, "ltt": ms(observed)}, "atp": 100, "vtt": "2000",
                    "marketLevel": {"bidAskQuote": [{"bidP": 102, "askP": 104}]},
                    "marketOHLC": {"ohlc": [{"interval": "I1", "ts": ms(start + timedelta(minutes=i)),
                                               "high": 102, "low": 99, "vol": 100}]},
                }},
            }}})
        result = stream.snapshot(now=start + timedelta(minutes=15))
        self.assertEqual(result["opening_range_breakout"], "ORB_BULLISH")
        self.assertEqual(result["relative_volume_intraday"], 2)
        self.assertAlmostEqual(result["distance_from_vwap_pct"], 3)
        self.assertEqual(result["bid_ask_spread"], 2)
        partial = start + timedelta(minutes=15, seconds=20)
        stream.on_message({"currentTs": ms(partial), "feeds": {"NSE_EQ|TEST": {
            "requestMode": "full_d30", "fullFeed": {"marketFF": {
                "ltpc": {"ltp": 103, "ltt": ms(partial)}, "atp": 100, "vtt": "9000",
                "marketLevel": {"bidAskQuote": [{"bidP": 102, "askP": 104}]},
            }},
        }}})
        self.assertEqual(stream.snapshot(now=partial)["relative_volume_intraday"], 2)
        with self.assertRaises(ValueError):
            stream.snapshot(now=start + timedelta(minutes=16, seconds=1))
        def extra_bar(minute, high, low):
            observed = start + timedelta(minutes=minute + 1)
            stream.on_message({"currentTs": ms(observed), "feeds": {"NSE_EQ|TEST": {
                "requestMode": "full_d30", "fullFeed": {"marketFF": {
                    "ltpc": {"ltp": 100, "ltt": ms(observed)}, "atp": 100, "vtt": "2000",
                    "marketLevel": {"bidAskQuote": [{"bidP": 99, "askP": 101}]},
                    "marketOHLC": {"ohlc": [{"interval": "I1",
                                               "ts": ms(start + timedelta(minutes=minute)),
                                               "high": high, "low": low, "vol": 100}]},
                }},
            }}})
            return observed
        observed = extra_bar(15, 104, 98)
        self.assertEqual(stream.snapshot(now=observed)["opening_range_breakout"], "NONE")
        observed = extra_bar(16, 103, 99)
        self.assertEqual(stream.snapshot(now=observed)["opening_range_breakout"], "INSIDE_BAR")

    def test_incomplete_opening_range_remains_missing(self):
        zone = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 1, 5, 9, 30, tzinfo=zone)
        ms = str(int(now.timestamp() * 1000))
        stream = UpstoxMicrostructureStream("NSE_EQ|TEST", {})
        stream.on_message({"currentTs": ms, "feeds": {"NSE_EQ|TEST": {
            "requestMode": "full_d30", "fullFeed": {"marketFF": {"ltpc": {"ltp": 100, "ltt": ms}}}
        }}})
        result = stream.snapshot(now=now)
        self.assertIsNone(result["opening_range_breakout"])
        self.assertIsNone(result["relative_volume_intraday"])


if __name__ == "__main__":
    unittest.main()
