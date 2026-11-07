import unittest
from unittest.mock import patch

import pandas as pd

from features.trades.workspace import render_trading


class TradingStockDisplayTests(unittest.TestCase):
    def test_equities_and_unverified_contracts_are_each_shown_once(self):
        positions = pd.DataFrame([
            {"Ticker": "ABC", "Qty": 1, "Avg_Price": 90, "LTP": 100,
             "Instrument_Key": "NSE_EQ|INE002A01018"},
            {"Ticker": "ABC FUT", "Qty": 1, "Avg_Price": 90, "LTP": 100,
             "Instrument_Key": "NSE_FO|123"},
        ])
        with patch("features.trades.workspace.st") as ui, \
             patch("features.trades.workspace.render_stock_holdings_table") as stocks, \
             patch("features.trades.workspace.style_returns", side_effect=lambda frame, **_: frame):
            ui.session_state.get.return_value = "Upstox"
            ui.context.theme.type = "dark"
            render_trading(positions)
        stock_frame = stocks.call_args.args[0]
        other_frame = ui.dataframe.call_args.args[0]
        self.assertEqual(list(stock_frame["Ticker"]), ["ABC"])
        self.assertEqual(list(other_frame["Ticker"]), ["ABC FUT"])


if __name__ == "__main__":
    unittest.main()
