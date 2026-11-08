import shutil
import subprocess
import unittest

import pandas as pd

from features.analysis.holdings import _build_holding_scan_row
from features.analysis.stock_context import append_context_to_row
from features.equities.scanner import build_scan_table_payload
from features.equities.scan_table import render_scan_table
from services.stock_context_models import StockSignalContext
from test_context_table_filters import HARNESS, row


@unittest.skipUnless(shutil.which("node"), "Node.js is required to exercise the browser table")
class StockPanelRatingTests(unittest.TestCase):
    def test_ratings_count_only_directionally_assessed_values(self):
        data = row("ABC", [], ind={
            "green": ["12", "pos"], "red": ["5", "neg"], "neutral": ["Company", "neu"],
            "missing": ["Unavailable", "pos"], "other": ["<script>", "neu"],
            "unknown": ["Unverified", "unknown"], "long": ["x" * 300, "neu"],
            "momentum": ["62", "pos"], "risk": ["3", "neg"],
        }, extraGroups=[["Unrated", [["missing", "Missing"], ["other", "Other"]]]],
            groupRatings={"Trend": {"positive": 1, "rated": 2},
                          "Momentum": {"positive": 1, "rated": 1},
                          "Risk and range": {"positive": 0, "rated": 1},
                          "Unrated": {"positive": 0, "rated": 0}},
            contextInfo={"green": 'Risk "tooltip" <script>'},
            checks=[["RSI", "60", "p"]],
            pos=1, total=1,
            extraSections=[{"title": "Warnings", "note": "Test"},
                           {"title": "News", "items": [
                               {"title": "Gain", "url": "https://finance.yahoo.com/news/1",
                                "publisher": "News", "sentiment": 1},
                               {"title": "Loss", "url": "https://finance.yahoo.com/news/2",
                                "publisher": "News", "sentiment": -1},
                               {"title": "Other", "url": "", "publisher": "News"},
                           ]}])
        html = render_scan_table([data], {
            "signals": ["BUY"], "info": {}, "checkInfo": {}, "rawCols": [],
            "fieldInfo": {"green": "Shared metric details."},
            "groups": [["Trend", [["green", "Good"], ["red", "Bad"], ["neutral", "Neutral"],
                                  ["missing", "Unknown"], ["unknown", "Unverified"],
                                  ["long", "Long"]]],
                       ["Momentum", [["momentum", "Momentum reading"]]],
                       ["Risk and range", [["risk", "Risk reading"]]]],
        })
        assertions = r"""
        const detailHtml=detail(DATA[0]);
        assert.ok(detailHtml.includes('Trend &amp; momentum <span class="group-rating pos">2 positive of 3 assessed'));
        assert.ok(!detailHtml.includes('0 positive of 0 assessed'));
        assert.strictEqual((detailHtml.match(/<details class="group"/g)||[]).length,5);
        assert.ok(detailHtml.indexOf('Risk &amp; intraday') < detailHtml.indexOf('Trend &amp; momentum'));
        assert.ok(!detailHtml.includes('<summary>Signal checks'));
        assert.ok(detailHtml.includes('class="subgroup-title">Trend</div>'));
        assert.ok(detailHtml.includes('class="subgroup-title">Momentum</div>'));
        assert.ok(detailHtml.includes('Momentum reading'));
        assert.ok(detailHtml.includes('class="metrics-grid"'));
        assert.ok(detailHtml.includes('class="kv full-width"'));
        assert.ok(!detailHtml.includes('<details class="group" open'));
        assert.ok(detailHtml.includes('class="missing">Unavailable'));
        assert.ok(detailHtml.includes('class="missing">Unverified'));
        assert.ok(detailHtml.includes('class="neu">Company'));
        assert.ok(detailHtml.includes('Risk &quot;tooltip&quot; &lt;script&gt;'));
        assert.ok(detailHtml.includes('Shared metric details.'));
        assert.ok(detailHtml.includes('View full details</summary>' + 'x'.repeat(300)));
        assert.ok(detailHtml.includes('<summary>Warnings</summary>'));
        assert.ok(detailHtml.includes('Neutral: reported information; no directional assessment is implied.'));
        assert.ok(detailHtml.includes('How the research signal was scored (1 positive of 1 assessed)'));
        assert.ok(detailHtml.includes('RSI: 60'));
        assert.ok(detailHtml.includes('original research checks and inputs'));
        assert.ok(detailHtml.includes('News <span class="group-rating neu">1 positive of 2 assessed'));
        assert.ok(detailHtml.includes('class="tag news pos"'));
        assert.ok(detailHtml.includes('class="tag news neg"'));
        assert.ok(detailHtml.includes('class="tag news neu"'));
        assert.ok(detailHtml.includes('Keyword sentiment is not investment advice.'));
        st.panelOpen.add(JSON.stringify(['ABC','category:Trend & momentum']));
        assert.ok(detail(DATA[0]).includes('&quot;category:Trend &amp; momentum&quot;]" open>'));
        assert.ok(!detailHtml.includes('<script>'));
        """
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                input=html, text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('.metrics-grid{display:flex;flex-wrap:wrap;', html)
        self.assertIn('.panel{position:sticky;left:0;width:100cqi;max-width:100%;', html)
        self.assertIn('.scroll{overflow-x:auto;container-type:inline-size;}', html)

    def test_related_context_and_news_sections_are_merged_in_priority_order(self):
        data = row("ABC", [], ind={
            "circuit": ["1%", "neg"], "fundamental": ["12", "neu"],
            "event": ["SPLIT", "neu"], "profile": ["ABC", "neu"],
        }, extraGroups=[
            ["Company profile", [["profile", "Company name"]]],
            ["Corporate actions", [["event", "Event"]]],
            ["Fundamentals", [["fundamental", "P/E"]]],
            ["Intraday market", [["circuit", "Circuit distance"]]],
        ], groupRatings={"Intraday market": {"positive": 0, "rated": 1}},
            extraSections=[
                {"title": "Local stock context gate", "note": "Critical risk warning"},
                {"title": "Recent company headlines", "items": [
                    {"title": "A", "url": "https://finance.yahoo.com/news/1", "sentiment": 1}]},
                {"title": "Context headlines", "values": [["Headline", "Context news B"]]},
                {"title": "Data availability", "note": "A source is unavailable"},
            ])
        html = render_scan_table([data], {
            "signals": ["BUY"], "info": {}, "checkInfo": {}, "rawCols": [], "groups": [],
        })
        assertions = r"""
        const h=detail(DATA[0]);
        const titles=['Company &amp; ownership','Risk &amp; intraday','Fundamentals &amp; outlook','News &amp; events','Data availability'];
        for(let i=1;i<titles.length;i++)assert.ok(h.indexOf('<summary>'+titles[i-1])<h.indexOf('<summary>'+titles[i]));
        assert.strictEqual((h.match(/<summary>News &amp; events/g)||[]).length,1);
        assert.ok(h.includes('Risk &amp; intraday <span class="group-rating neg">0 positive of 1 assessed'));
        assert.ok(h.includes('News &amp; events <span class="group-rating pos">1 positive of 1 assessed'));
        for(const v of ['Critical risk warning','Circuit distance','P/E','SPLIT','Context news B','Company name','A source is unavailable'])assert.ok(h.includes(v));
        """
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                input=html, text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_detail_values_abbreviate_only_explicit_rupees_and_known_counts(self):
        data = row("ABC", ["12345678.90123"], ind={
            "rupees": ["₹12345678.90123", "neu"],
            "provider_currency": ["USD 12345678.90123", "neu"],
            "unknown_currency": ["12345678.90123", "neu"],
            "context_technicals_avg_volume_3m": ["1234567", "neu"],
            "context_technicals_current_volume": ["1.234e+8", "neu"],
            "percentage": ["1.234567%", "neu"],
            "ratio": ["1.234567", "neu"],
        }, extraGroups=[
            ["Holding details", [["rupees", "Market value"]]],
            ["Fundamentals", [
                ["provider_currency", "Free cash flow"],
                ["unknown_currency", "Market capitalization"],
                ["percentage", "Profit margin"],
                ["ratio", "Debt / equity"],
            ]],
            ["Performance and volume", [
                ["context_technicals_avg_volume_3m", "Average volume"],
                ["context_technicals_current_volume", "Current volume"],
            ]],
        ])
        html = render_scan_table([data], {
            "signals": ["BUY"], "info": {}, "checkInfo": {}, "rawCols": ["Provider value"],
            "groups": [],
        })
        assertions = r"""
        const before=JSON.stringify(DATA[0]),h=detail(DATA[0]);
        assert.ok(h.includes('title="₹12345678.90123">₹1.23Cr</span>'));
        assert.ok(h.includes('title="1234567">12.35L</span>'));
        assert.ok(h.includes('>USD 12345678.90123</span>'));
        assert.ok(h.includes('>12345678.90123</span>'));
        assert.ok(h.includes('>1.234567%</span>'));
        assert.ok(h.includes('>1.234567</span>'));
        assert.ok(h.includes('>1.234e+8</span>'));
        assert.strictEqual(JSON.stringify(DATA[0]),before);
        assert.ok(tableText(VIEW.rows,',').includes('12345678.90123'));
        """
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                input=html, text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_context_market_caps_format_for_indian_ticker_only(self):
        context = {
            "profile": {"ticker": "BSE.NS"},
            "fundamentals": {"market_cap": 1543290486784.0,
                             "float_market_cap": 1478723271400.0},
        }
        for ticker, expected_inr in (("BSE.NS", True), ("OTHER", False)):
            data, config = build_scan_table_payload(
                [{"Ticker": ticker, "Recommendation": "HOLD"}], {}
            )
            data = data[0]
            context["profile"]["ticker"] = ticker
            append_context_to_row(data, config, {"context": context, "gate": {}})
            html = render_scan_table([data], config)
            assertions = r"""
            const before=JSON.stringify(DATA[0]),h=detail(DATA[0]);
            if(EXPECT_INR){
             assert.ok(h.includes('title="1543290486784.0">₹1,54,329.05Cr</span>'));
             assert.ok(h.includes('title="1478723271400.0">₹1,47,872.33Cr</span>'));
            }else{
             assert.ok(h.includes('>1543290486784.0</span>'));
             assert.ok(h.includes('>1478723271400.0</span>'));
            }
            assert.strictEqual(JSON.stringify(DATA[0]),before);
            assert.ok(tableText(VIEW.rows,',').includes('1543290486784'));
            """.replace("EXPECT_INR", "true" if expected_inr else "false")
            result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                    input=html, text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_context_market_cap_formats_merged_legacy_field(self):
        data, config = build_scan_table_payload(
            [{"Ticker": "ABC", "Recommendation": "HOLD"}], {}
        )
        item = data[0]
        item["extraGroups"] = [["Fundamentals", [["fundamental_0", "Market capitalization"]]]]
        item["ind"]["fundamental_0"] = ["INR 100.00", "neu"]
        append_context_to_row(item, config, {
            "context": {"profile": {"ticker": "ABC.BO"},
                        "fundamentals": {"market_cap": 1543290486784.0}},
            "gate": {},
        })
        self.assertEqual(item["panelCurrencies"]["fundamental_0"], "INR")
        html = render_scan_table([item], config)
        assertions = 'assert.ok(detail(DATA[0]).includes(\'title="1543290486784.0">₹1,54,329.05Cr</span>\'));'
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                input=html, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_technical_coverage_and_values_render_for_holdings_and_scanners(self):
        context = StockSignalContext.model_validate({
            "horizon": "LONG_TERM_EQUITY", "profile": {"ticker": "ABC.NS"},
            "technicals": {"rsi_14": 56, "sma_20": 90, "squeeze_alert": False},
        }).model_dump(mode="json")
        holdings, holding_config = _build_holding_scan_row(pd.Series({"Ticker": "ABC", "LTP": 100}), {})
        scanner, scan_config = build_scan_table_payload([{"Ticker": "ABC", "Recommendation": "HOLD"}], {})
        for item, config in ((holdings, holding_config), (scanner[0], scan_config)):
            append_context_to_row(item, config, {"context": context, "gate": {}})
            html = render_scan_table([item], config)
            assertions = r"""
            const h=detail(DATA[0]);
            assert.ok(h.includes('Local daily context: 3 of 22 fields available'));
            for(const v of ['20-day average','RSI','Squeeze alert','ATR percentile (14)',
                            'Choppiness index (14)','Current volume','Average volume (3 months)',
                            '30-day annualized volatility']) assert.ok(h.includes(v),v);
            assert.ok(h.includes('Unavailable'));
            assert.ok(h.includes('>No</span>'));
            """
            result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                    input=html, text=True, encoding="utf-8", capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_headlines_link_only_to_safe_https_urls(self):
        data = row("ABC", [], extraSections=[{"title": "Context headlines", "items": [
            {"title": "Upstox headline", "url": "https://upstox.com/news/story?a=1&b=2",
             "publisher": "Upstox", "published": "2026-01-01", "sentiment": None},
            {"title": "Unsafe", "url": "javascript:alert(1)", "sentiment": None},
            {"title": "Credential", "url": "https://user:pass@example.com/", "sentiment": None},
            {"title": "RSS headline", "url": "https://example.com/article", "sentiment": None},
        ]}])
        html = render_scan_table([data], {
            "signals": ["BUY"], "info": {}, "checkInfo": {}, "rawCols": [], "groups": [],
        })
        assertions = r"""
        const h=detail(DATA[0]);
        assert.ok(h.includes('href="https://upstox.com/news/story?a=1&amp;b=2"'));
        assert.ok(h.includes('href="https://example.com/article"'));
        assert.ok(h.includes('target="_blank" rel="noopener noreferrer"'));
        assert.ok(!h.includes('href="javascript:'));
        assert.ok(!h.includes('href="https://user:pass'));
        assert.ok(h.includes('>Unsafe</span>'));
        assert.ok(h.includes('>Credential</span>'));
        assert.ok(h.includes('Upstox · 2026-01-01'));
        """
        result = subprocess.run([shutil.which("node"), "-e", HARNESS, assertions],
                                input=html, text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
