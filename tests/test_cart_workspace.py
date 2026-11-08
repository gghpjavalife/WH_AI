from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from features.cart.row import ROW_CSS, ROW_HTML, ROW_JS
from features.cart.workspace import _display_name, _estimate_text, _row_event, _valid_quantity


def _app():
    return AppTest.from_file(str(Path(__file__).resolve().with_name("cart_app_fixture.py")))


class CartWorkspaceTests(unittest.TestCase):
    def test_stale_component_callback_is_ignored(self):
        with patch("features.cart.workspace.st.session_state", {
            "stock_cart": [],
        }), patch("features.cart.workspace.st.warning") as warning:
            _row_event("removed-stock", "removed-widget")
            warning.assert_not_called()

        with patch("features.cart.workspace.st.session_state", {
            "removed-widget": {"event": {"action": "remove"}},
            "stock_cart": [],
        }), patch("features.cart.workspace.st.warning") as warning:
            _row_event("removed-stock", "removed-widget")
            warning.assert_not_called()

    def test_quantity_validation_and_cart_actions(self):
        state = {"stock_cart": [
            {"id": "a", "ticker": "AAA", "quantity": -2, "holding_quantity": 2.5}
        ], "widget": {"event": {"action": "quantity", "quantity": -3}}}
        with patch("features.cart.workspace.st.session_state", state), \
             patch("features.cart.workspace.st.warning") as warning:
            _row_event("a", "widget")
            self.assertEqual(state["stock_cart"][0]["quantity"], -2)
            warning.assert_called_once()
            state["widget"]["event"]["quantity"] = -1
            _row_event("a", "widget")
            self.assertEqual(state["stock_cart"][0]["quantity"], -1)
            state["widget"]["event"] = {"action": "view"}
            _row_event("a", "widget")
            self.assertEqual(state["stock_cart_detail"], "a")
            state["widget"]["event"] = {"action": "review"}
            _row_event("a", "widget")
            self.assertEqual(state["stock_cart_review"], "a")
            state["widget"]["event"] = {"action": "remove"}
            _row_event("a", "widget")
            self.assertEqual(state["stock_cart"], [])
            self.assertIsNone(state["stock_cart_detail"])
        self.assertFalse(_valid_quantity({"holding_quantity": 10}, -1.5))
        self.assertFalse(_valid_quantity({"holding_quantity": 10}, float("nan")))
        self.assertFalse(_valid_quantity({"holding_quantity": 10}, 10**400))
        self.assertFalse(_valid_quantity({"holding_quantity": 10}, -11))
        self.assertTrue(_valid_quantity({"holding_quantity": 10}, -10))
        self.assertTrue(_valid_quantity({"holding_quantity": 0}, -11))

    def test_names_and_currency_estimate(self):
        self.assertEqual(_display_name({"ticker": "BSE", "name": "BSE · BSE"}), "BSE")
        self.assertEqual(_display_name({"ticker": "ABC", "name": "ABC · Company"}), "ABC")
        self.assertEqual(_display_name({"name": "ABC · Company"}), "ABC · Company")
        item = {"ticker": "ABC.NS", "quantity": -3,
                "stock_detail": {"row": {"price": 50}, "config": {}}}
        self.assertEqual(_estimate_text(item), "Estimated: 3 × LTP ₹50.00 = ₹150.00")
        item["ticker"] = "ABC"
        self.assertEqual(_estimate_text(item), "Estimated: 3 × LTP 50.00 = 150.00 (currency unknown)")
        item["name"] = "ABC · Example Ltd (NSE)"
        self.assertEqual(_estimate_text(item), "Estimated: 3 × LTP ₹50.00 = ₹150.00")
        item["name"] = ""
        item["stock_detail"]["row"]["currency"] = "INR"
        self.assertEqual(_estimate_text(item), "Estimated: 3 × LTP ₹50.00 = ₹150.00")

    def test_cart_totals_and_details_remain_accessible(self):
        app = _app()
        app.session_state["stock_cart"] = [
            {"id": "a", "ticker": "PGIL",
             "name": "PGIL · Pearl Global Industries Limited (NSE)",
             "signal": "BUY", "quantity": 3,
             "stock_detail": {"row": {"price": 125.5}, "config": {}}},
            {"id": "b", "ticker": "BBB.BO", "signal": "SELL", "quantity": -2,
             "holding_quantity": 10, "stock_detail": {"row": {"price": 50}, "config": {}}},
        ]
        app.session_state["stock_cart_detail"] = "a"
        with patch("features.cart.workspace.ROW_COMPONENT") as component, \
             patch("features.cart.workspace.render_cart_stock_details") as details:
            app.run()
            self.assertEqual(component.call_count, 2)
            details.assert_called_once()
        self.assertFalse(app.exception)
        self.assertTrue(any("Estimated buy: ₹376.50" in c.value and
                            "Estimated sell: ₹100.00" in c.value and
                            "Net cash needed: ₹276.50" in c.value for c in app.caption))
        with patch("features.cart.workspace.ROW_COMPONENT"), \
             patch("features.cart.workspace.render_cart_stock_details"):
            app.button[-1].click().run()
        self.assertTrue(any("checkout does not place orders" in info.value for info in app.info))
        self.assertTrue(any("Estimated: 2 × LTP ₹50.00 = ₹100.00" in t.value for t in app.text))

    def test_navigation_count_connected_and_empty(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20)
        app.session_state["app_navigation"] = "Cart"
        app.session_state["stock_cart"] = [{"id": "x", "ticker": "XYZ", "signal": "BUY"}]
        with patch("features.cart.workspace.ROW_COMPONENT"):
            app.run()
        self.assertFalse(app.exception)
        self.assertTrue(app.tabs[-1].label.endswith("Cart (1)"))
        self.assertFalse(any("Cart" == x.value for x in app.subheader))
        app.session_state["stock_cart"] = []
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(app.tabs[-1].label.endswith("Cart (0)"))
        self.assertTrue(any("empty" in info.value for info in app.info))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_browser_controls_enforce_holding_and_integer(self):
        js = ROW_JS.replace("export default function", "function render")
        program = r"""
        const assert=require('assert'),calls=[];
        const elements={};
        const el=id=>elements[id] ||= {value:'',className:'',hidden:false,disabled:false,
         removeAttribute(k){delete this[k]},blur(){},textContent:''};
        const parentElement={querySelector:s=>el(s.slice(1))};
        """ + js + r"""
        const data={name:'AAA',signal:'SELL',quantity:-2,held:2.5,estimate:'x',estimateHelp:'x'};
        render({parentElement,data,setTriggerValue:(...args)=>calls.push(args)});
        assert.strictEqual(el('qtyDown').disabled,true);
        assert.strictEqual(el('pctUp').disabled,true);
        assert.strictEqual(el('pct').value,'80.00%');
        el('qty').value='-3';el('qty').onchange();
        assert.strictEqual(calls.length,0);assert.strictEqual(el('qty').value,'-2');
        assert.strictEqual(el('error').hidden,false);
        el('qty').value='-1.5';el('qty').onchange();
        assert.strictEqual(calls.length,0);
        el('pctDown').onclick();
        assert.deepStrictEqual(calls.pop(),['event',{action:'quantity',quantity:-1}]);
        el('qtyUp').onclick();
        assert.deepStrictEqual(calls.pop(),['event',{action:'quantity',quantity:-1}]);
        el('view').onclick();assert.deepStrictEqual(calls.pop(),['event',{action:'view'}]);
        data.quantity=-7;render({parentElement,data,setTriggerValue:(...args)=>calls.push(args)});
        assert.strictEqual(el('review').disabled,true);
        el('qtyUp').onclick();
        assert.deepStrictEqual(calls.pop(),['event',{action:'quantity',quantity:-2}]);
        data.quantity=2;render({parentElement,data,setTriggerValue:(...args)=>calls.push(args)});
        assert.ok(el('qtyBox').className.includes('buy'));assert.strictEqual(el('pctBox').hidden,true);
        """
        result = subprocess.run([shutil.which("node"), "-"], input=program,
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('readonly aria-label="Percentage of holding"', ROW_HTML)
        self.assertIn(".row{display:flex", ROW_CSS)
        self.assertIn("@media(max-width:760px)", ROW_CSS)
        self.assertIn('grid-template-areas:"stock actions" "quantities quantities" "estimate estimate"', ROW_CSS)
        self.assertIn("touch-action:manipulation", ROW_CSS)
        self.assertIn('<div class="quantities">', ROW_HTML)
