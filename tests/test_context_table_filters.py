import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from features.equities.scan_table import (
    _COMPONENT_JS, _DETAIL_JS, _TABLE_JS, _add_rows_to_cart,
    render_cart_stock_details, render_scan_table, show_scan_table,
)

NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(0, 'utf8');
const elements = {};
function element(id) {
 return elements[id] ||= {
  id, innerHTML:'', value:'', dataset:{}, style:{}, hidden:id==='fieldPicker',
  classList:{add(){},remove(){}}, attributes:{},
  querySelectorAll(){return this.inputs||[]}, remove(){},
  setAttribute(k,v){this.attributes[k]=v}, focus(){this.focused=true}
 };
}
global.document = {
 querySelector(selector){return element(selector.slice(1))},
 body:{appendChild(){}}, createElement:()=>element('created')
};
global.window = {};
for (const name of ['sessionStorage','localStorage']) {
 Object.defineProperty(global.window,name,{get(){throw Error('Storage must not be accessed')}});
}
global.navigator = {};
global.assert = assert;
global.calls = [];
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
vm.runInThisContext(script + `
const chooseField = (index, checked=true) =>
 $('fieldList').onchange({target:{dataset:{field:String(index)},checked}});
const filter = (index,bound,value) =>
 $('filt').oninput({target:{dataset:{extra:String(index),bound},value}});
const sort = key =>
 $('head').onclick({target:{closest:selector=>selector==='th[data-k]'?({dataset:{k:key},classList:{contains:()=>false}}):null}});
const removeField = index =>
 $('head').onclick({target:{closest:selector=>selector==='[data-remove-field]'?({dataset:{removeField:String(index)}}):null}});
` + '\n' + process.argv[1]);
"""


def row(ticker, raw, **values):
    return {
        "ticker": ticker, "label": ticker, "price": None, "chg": None,
        "m1": None, "vol": None, "rsi": None, "signal": "BUY", "score": 0,
        "pos": 0, "total": 0, "pass": True, "reason": "", "checks": [],
        "ind": {}, "raw": raw, **values,
    }


@unittest.skipUnless(NODE, "Node.js is required to exercise the browser table")
class ContextTableFilterTests(unittest.TestCase):
    def run_table(self, rows, config, assertions):
        config = {"signals": ["BUY"], "groups": [], "info": {}, "checkInfo": {},
                  "rawCols": [], **config}
        html = render_scan_table(rows, config)
        html = html.replace("const setTriggerValue=()=>{};",
                            "const setTriggerValue=(...args)=>global.calls.push(args);")
        result = subprocess.run([NODE, "-e", HARNESS, assertions], input=html,
                                text=True, encoding="utf-8", capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return html

    def test_default_scanner_signal_follows_stock(self):
        self.run_table([row("ABC", [])], {}, r"""
            assert.deepStrictEqual(COLS.slice(0,2).map(c=>c[0]),['stock','signal']);
            assert.ok($('head').innerHTML.indexOf('>Stock') < $('head').innerHTML.indexOf('>Signal'));
            assert.ok($('head').innerHTML.indexOf('>Signal') < $('head').innerHTML.indexOf('>Price'));
        """)

    def test_allocation_is_neutral_while_returns_remain_directional(self):
        self.run_table(
            [row("A", [], allocationPct=12.5, returnPct=5),
             row("B", [], allocationPct=-1, returnPct=-2)],
            {"columns": [["ticker", "Ticker", 0], ["signal", "Signal", 0],
                         ["returnPct", "Returns %", 1, "percent"],
                         ["allocationPct", "Allocation %", 1, "percent"]]},
            r"""
            const body=$('body').innerHTML;
            assert.ok(body.includes('class="num neu">12.50%</td>'));
            assert.ok(body.includes('class="num neu">-1.00%</td>'));
            assert.ok(body.includes('class="num pos">+5.00%</td>'));
            assert.ok(body.includes('class="num neg">-2.00%</td>'));
            """,
        )

    def test_picker_dynamic_columns_filters_sort_and_clear(self):
        html = self.run_table(
            [row("A", [0.1, "Tech", 5], price=5),
             row("B", [0.2, "Bank", 10], price=10),
             row("C", [None, None, None])],
            {"rawCols": ["return", "sector", "score"],
             "rawTypes": ["number", "text", "number"],
             "rawUnits": {"return": "fraction"}},
            r"""
            assert.strictEqual($('fieldPicker').hidden,true);
            $('fieldToggle').onclick();
            assert.strictEqual($('fieldPicker').hidden,false);
            assert.strictEqual($('fieldToggle').attributes['aria-expanded'],'true');
            $('fieldSearch').value='return';$('fieldSearch').oninput();
            assert.ok($('fieldList').innerHTML.includes('fraction: 0.1 = 10%'));
            assert.ok(!$('fieldList').innerHTML.includes('Sector'));
            chooseField(0);
            assert.ok($('head').innerHTML.includes('Return (fraction: 0.1 = 10%)'));
            assert.ok($('filt').innerHTML.includes('data-extra="0"'));
            assert.ok(!$('filt').innerHTML.includes('<details open'));
            assert.ok($('body').innerHTML.includes('>0.1</td>'));
            filter(0,'min','0.15');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B']);
            chooseField(1);
            assert.strictEqual(st.extra[0].min,'0.15');
            assert.ok($('fieldList').innerHTML.indexOf('Selected (2)') < $('fieldList').innerHTML.indexOf('No matching fields.'));
            assert.ok($('fieldList').innerHTML.includes('Sector'));
            assert.ok($('head').innerHTML.includes('aria-label="Remove Sector column"'));
            filter(1,'search','bank');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B']);
            filter(0,'min','');filter(1,'search','');
            sort('raw:0');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B','A','C']);
            sort('raw:0');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['A','B','C']);
            sort('raw:1');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['A','B','C']);
            removeField(1);
            assert.ok(!$('head').innerHTML.includes('Sector'));
            assert.ok(!('1' in st.extra));
            assert.strictEqual(st.key,'signal');
            $('filt').oninput({target:{dataset:{f:'pmin'},value:'6'}});
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B']);
            $('clearFilters').onclick();
            assert.strictEqual(VIEW.rows.length,3);
            assert.deepStrictEqual(Object.keys(st.extra),['0']);
            assert.strictEqual(st.extra[0].min,'');
            assert.ok($('head').innerHTML.includes('Return (fraction: 0.1 = 10%)'));
            assert.strictEqual(st.f.pmin,'');
            """,
        )
        self.assertNotIn("Select all fields", html)
        self.assertNotIn('id="activeFilters"', html)

    def test_selected_fields_stay_above_matching_results_and_remove_resets_filter(self):
        self.run_table(
            [row("A", [1, "Bank", 2]), row("B", [3, "Tech", 5])],
            {"rawCols": ["Score", "Sector", "Price"], "rawTypes": ["number", "text", "number"]},
            r"""
            chooseField(1);chooseField(2);
            $('fieldSearch').value='score';$('fieldSearch').oninput();
            const list=$('fieldList').innerHTML;
            assert.ok(list.indexOf('Sector') < list.indexOf('Search results'));
            assert.ok(list.indexOf('Price') < list.indexOf('Search results'));
            assert.ok(list.indexOf('Search results') < list.indexOf('Score'));
            filter(1,'search','bank');sort('raw:1');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['A']);
            removeField(1);
            assert.strictEqual(st.key,'signal');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker).sort(),['A','B']);
            assert.ok(!$('head').innerHTML.includes('data-remove-field="1"'));
            assert.ok($('head').innerHTML.includes('data-remove-field="2"'));
            assert.ok(!$('fieldList').innerHTML.includes('Sector'));
            assert.strictEqual(st.extra[2].min,'');
            """,
        )

    def test_text_number_missing_escaping_and_exports(self):
        malicious = '<img src=x onerror="bad()">'
        html = self.run_table(
            [row("A", ["Technology", 0], price=0),
             row("B", ["technology", 5], price=5),
             row("C", [malicious, None]), row("D", [False, 10])],
            {"rawCols": [malicious, "Count"], "rawTypes": ["text", "number"],
             "exportColumns": [["ticker", "Ticker"]]},
            r"""
            chooseField(0);chooseField(1);
            assert.ok(!$('head').innerHTML.includes('<img'));
            assert.ok(!$('fieldList').innerHTML.includes('<img'));
            assert.ok(!$('body').innerHTML.includes('<img'));
            assert.ok($('body').innerHTML.includes('&lt;img'));
            assert.ok($('filt').innerHTML.includes('&lt;img'));
            filter(0,'search','TECH');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker).sort(),['A','B']);
            filter(1,'min','0');filter(1,'max','0');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['A']);
            assert.ok(tableText(VIEW.rows,',').includes('A,Technology,0'));
            filter(1,'max','');filter(0,'search','');
            filter(0,'choice',String(rawFields[0].values.indexOf('false')));
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['D']);
            chooseField(0,false);
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker).sort(),['A','B','D']);
            $('clearFilters').onclick();
            assert.strictEqual(VIEW.rows.length,4);
            assert.deepStrictEqual(Object.keys(st.extra),['1']);
            assert.strictEqual(st.extra[1].min,'');
            assert.strictEqual(st.extra[1].max,'');
            """,
        )
        self.assertIn(json.dumps(malicious), html)

    def test_precision_wrapping_and_clear_keeps_text_filter_column(self):
        long_text = "long<unsafe>" * 80
        self.run_table(
            [row("A", [0.123456789012345, long_text]),
             row("B", [0.123456789012346, "z"]),
             row("C", [None, None])],
            {"rawCols": ["Return", "Narrative"],
             "rawTypes": ["number", "text"], "rawUnits": {"Return": "fraction"}},
            r"""
            chooseField(0);chooseField(1);
            assert.ok($('head').innerHTML.includes('fraction: 0.1 = 10%'));
            assert.ok($('body').innerHTML.includes('0.123456789012345'));
            assert.ok($('body').innerHTML.includes('long&lt;unsafe&gt;'.repeat(80)));
            assert.ok($('body').innerHTML.includes('class="raw-value '));
            filter(0,'min','0.123456789012346');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B']);
            filter(1,'search','z');
            $('clearFilters').onclick();
            assert.deepStrictEqual(Object.keys(st.extra),['0','1']);
            assert.strictEqual(st.extra[0].min,'');
            assert.strictEqual(st.extra[1].search,'');
            assert.strictEqual(VIEW.rows.length,3);
            sort('raw:0');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B','A','C']);
            sort('raw:0');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['A','B','C']);
            sort('raw:1');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B','A','C']);
            sort('raw:1');
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['A','B','C']);
            filter(1,'search','z');
            chooseField(1,false);
            assert.strictEqual(VIEW.rows.length,3);
            assert.ok(!$('head').innerHTML.includes('Narrative'));
            """,
        )

    def test_primary_filters_and_paging(self):
        self.run_table(
            [row("A", [], price=0), row("B", [], price=10), row("C", [])], {},
            r"""
            assert.ok($('filt').innerHTML.includes('aria-label="Minimum price"'));
            $('filt').oninput({target:{dataset:{f:'pmin'},value:'0'}});
            assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker).sort(),['A','B']);
            sort('price');assert.deepStrictEqual(VIEW.rows.map(r=>r.ticker),['B','A']);
            $('size').onchange({target:{value:'1'}});
            assert.strictEqual(VIEW.slice.length,1);
            $('next').onclick();assert.strictEqual(VIEW.slice[0].ticker,'A');
            $('clearFilters').onclick();assert.strictEqual(VIEW.rows.length,3);
            """,
        )

    def test_add_to_cart_selection_and_empty_popup(self):
        html = self.run_table(
            [row("A", [], price=1), row("B", [], price=2), row("<C>", [], price=3)], {},
            r"""
            const select=(index,checked)=>$('body').onchange({target:{closest:s=>
             s==='[data-row-select]'?{dataset:{rowSelect:String(index)},checked}:null}});
            assert.strictEqual($('addToCart').hidden,false);
            $('addToCart').onclick();
            assert.strictEqual($('cartWarning').hidden,false);
            assert.strictEqual(calls.length,0);
            $('cartWarningClose').onclick();
            assert.strictEqual($('cartWarning').hidden,true);
            assert.ok($('body').innerHTML.includes('aria-label="Select A"'));
            $('body').onclick({target:{closest:s=>s==='[data-row-select]'?{}:
             s==='tr.row'?{dataset:{t:'A'}}:null}});
            assert.strictEqual(st.open.size,0);
            select(0,true);select(2,true);
            assert.strictEqual(st.selected.size,2);
            $('size').onchange({target:{value:'1'}});$('next').onclick();
            assert.strictEqual(st.selected.size,2);
            $('filt').oninput({target:{dataset:{f:'pmin'},value:'2'}});
            assert.strictEqual(st.selected.size,2);
            sort('price');assert.strictEqual(st.selected.size,2);
            $('addToCart').onclick();
            assert.deepStrictEqual(calls,[['add_to_cart',[0,2]]]);
            select(0,false);
            assert.strictEqual($('addToCart').hidden,false);
            """,
        )
        self.assertIn('.checkout-action{margin-right:auto;}', html)
        self.assertIn('title="Select tickers to add to your cart, then review them and trigger checkout from Cart."', html)
        self.assertNotIn('<div class="cart-info">', html)

    def test_select_all_filtered_stocks_across_pages_and_preserve_hidden_selection(self):
        self.run_table(
            [row("A", [], price=1), row("B", [], price=2), row("C", [], price=3)], {},
            r"""
            const select=(index,checked)=>$('body').onchange({target:{closest:s=>
             s==='[data-row-select]'?{dataset:{rowSelect:String(index)},checked}:null}});
            const selectAll=checked=>$('head').onchange({target:{id:'selectAll',checked}});
            assert.ok($('head').innerHTML.includes('aria-label="Select all filtered stocks"'));
            assert.ok(!$('head').innerHTML.includes('>Select</th>'));
            assert.strictEqual($('selectAll').checked,false);
            assert.strictEqual($('selectAll').indeterminate,false);
            select(0,true);
            assert.strictEqual($('selectAll').indeterminate,true);
            $('filt').oninput({target:{dataset:{f:'pmin'},value:'2'}});
            $('size').onchange({target:{value:'1'}});
            selectAll(true);
            assert.deepStrictEqual([...st.selected].sort(),[0,1,2]);
            assert.strictEqual($('selectAll').checked,true);
            selectAll(false);
            assert.deepStrictEqual([...st.selected],[0]);
            assert.strictEqual($('selectAll').checked,false);
            assert.strictEqual($('selectAll').indeterminate,false);
            $('filt').oninput({target:{dataset:{f:'pmin'},value:'10'}});
            assert.strictEqual($('selectAll').disabled,true);
            assert.deepStrictEqual([...st.selected],[0]);
            $('clearFilters').onclick();
            assert.strictEqual($('selectAll').indeterminate,true);
            selectAll(true);
            assert.strictEqual(st.selected.size,3);
            selectAll(false);
            assert.strictEqual(st.selected.size,0);
            assert.strictEqual($('addToCart').hidden,false);
            """,
        )

    def test_dynamic_values_use_only_explicit_signal_colors(self):
        self.run_table(
            [row("A", [1, 2, 3], ind={
                "gain": ["1", "pos"], "risk": ["2", "neg"], "plain": ["3", "neu"],
            })],
            {"rawCols": ["Gain", "Risk", "Plain"], "rawTypes": ["number"] * 3,
             "rawIndicatorKeys": {"Gain": "gain", "Risk": "risk", "Plain": "plain"}},
            r"""
            chooseField(0);chooseField(1);chooseField(2);
            assert.ok($('body').innerHTML.includes('class="raw-value num pos">1</td>'));
            assert.ok($('body').innerHTML.includes('class="raw-value num neg">2</td>'));
            assert.ok($('body').innerHTML.includes('class="raw-value num neu">3</td>'));
            """,
        )

    def test_signal_is_plain_colored_text_without_pill_or_button(self):
        html = self.run_table(
            [row("A", [], signal="STRONG BUY"), row("B", [], signal="BUY"),
             row("C", [], signal="HOLD"), row("D", [], signal="SELL"),
             row("E", [], signal="STRONG SELL"), row("F", [], signal="NOT RATED")],
            {"signals": ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL", "NOT RATED"]},
            r"""
            for(const [cls,label] of [['SB','STRONG BUY'],['B','BUY'],['H','HOLD'],
                                      ['S','SELL'],['SS','STRONG SELL'],['NR','NOT RATED']])
             assert.ok($('body').innerHTML.includes(`<span class="signal-value ${cls}">${label}</span>`));
            assert.ok(!$('body').innerHTML.includes('class="pill'));
            assert.ok(detail(DATA[0]).includes('<span class="signal-value SB">STRONG BUY</span>'));
            """,
        )
        self.assertIn('.signal-value.SB,.signal-value.B{color:var(--pos);}', html)
        self.assertIn('.signal-value.S,.signal-value.SS{color:var(--neg);}', html)
        self.assertIn('.signal-value.H{color:var(--fg);}', html)

    def test_concise_headers_and_ambiguous_provider_metrics(self):
        html = self.run_table(
            [row("A", [1, 2, 3, 4, 5])],
            {"rawCols": ["Technicals · RSI 14", "Fundamentals · Price",
                         "Provider alpha · Price", "Provider beta · Price",
                         "Technical momentum · SMA 20"],
             "rawTypes": ["number"] * 5},
            r"""
            for(let i=0;i<5;i++)chooseField(i);
            assert.ok($('head').innerHTML.includes('>RSI 14 · Technicals<span class="arrow"'));
            assert.ok($('head').innerHTML.includes('>SMA 20<span class="arrow"'));
            assert.ok($('head').innerHTML.includes('>Price · Fundamentals<span class="arrow"'));
            assert.ok($('head').innerHTML.includes('>Price · Provider alpha<span class="arrow"'));
            assert.ok($('head').innerHTML.includes('>Price · Provider beta<span class="arrow"'));
            assert.ok(!$('head').innerHTML.includes('class="i"'));
            assert.ok($('fieldList').innerHTML.includes('Technicals · RSI 14'));
            assert.ok($('head').innerHTML.includes('title="Technicals · RSI 14"'));
            """,
        )
        self.assertIn('grid-template-columns:repeat(auto-fit,minmax(min(12rem,100%),1fr))', html)
        self.assertIn('.field-picker{position:absolute', html)
        self.assertIn('th.raw-head{text-transform:none', html)
        self.assertIn('.extra-filters .controls{justify-content:flex-end;}', html)
        self.assertIn('top:calc(100% - .2rem);right:.9rem', html)
        self.assertIn('id="refresh" type="button" hidden>Refresh</button>', html)

    def test_duplicate_context_columns_are_hidden_but_raw_export_is_retained(self):
        self.run_table(
            [row("A", [61, 61])],
            {"rawCols": ["RSI 14", "Technicals · rsi 14"], "rawTypes": ["number", "number"],
             "hiddenRawCols": ["Technicals · rsi 14"]},
            r"""
            $('fieldToggle').onclick();
            assert.ok($('fieldList').innerHTML.includes('RSI 14'));
            assert.ok(!$('fieldList').innerHTML.includes('Technicals · Rsi 14'));
            assert.deepStrictEqual(rawFields.map(f=>f.index),[0]);
            assert.ok(tableText(VIEW.rows,',').includes('61,61'));
            """,
        )

    def test_component_instance_isolation_and_update(self):
        data = {"payload": [row("A", [1]), row("B", [2])],
                "config": {"rawCols": ["Value"], "rawTypes": ["number"],
                           "signals": ["BUY"], "groups": [], "info": {},
                           "checkInfo": {}}, "canRefresh": True}
        js = _COMPONENT_JS.replace("export default function(component)", "function componentRender(component)")
        program = r"""
        const assert=require('assert');
        function parent() {
         const els={};
         return {querySelector(s){const id=s.slice(1);return els[id] ||= {
          innerHTML:'',value:'',dataset:{},hidden:id==='fieldPicker',attributes:{},
          classList:{add(){},remove(){}},querySelectorAll(){return []},
          setAttribute(k,v){this.attributes[k]=v},focus(){}}}};
        }
        """ + js + r"""
        const first=parent(),second=parent(),calls=[],fresh=[];
        const data=JSON.parse(process.argv[2]);
        const mount=p=>componentRender({parentElement:p,data,setTriggerValue:(...x)=>calls.push(x)});
        const cleanup=mount(first);mount(second);
        first.querySelector('#fieldList').onchange({target:{dataset:{field:'0'},checked:true}});
        first.querySelector('#body').onchange({target:{closest:s=>s==='[data-row-select]'?
         {dataset:{rowSelect:'0'},checked:true}:null}});
        first.querySelector('#addToCart').onclick();
        assert.deepStrictEqual(calls[0],['add_to_cart',[0]]);
        second.querySelector('#addToCart').onclick();
        assert.strictEqual(second.querySelector('#cartWarning').hidden,false);
        first.querySelector('#filt').oninput({target:{dataset:{extra:'0',bound:'min'},value:'2'}});
        assert.ok(first.querySelector('#count').textContent.includes('of 1'));
        assert.ok(second.querySelector('#count').textContent.includes('of 2'));
        first.querySelector('#refresh').onclick();
        assert.strictEqual(calls[1][0],'refresh');
        cleanup();mount(first);
        assert.ok(first.querySelector('#count').textContent.includes('of 1'));
        data.payload.push({...data.payload[1],ticker:'C',label:'C',raw:[3]});
        componentRender({parentElement:first,data,setTriggerValue:(...x)=>fresh.push(x)});
        first.querySelector('#addToCart').onclick();
        assert.strictEqual(first.querySelector('#cartWarning').hidden,false);
        assert.ok(first.querySelector('#count').textContent.includes('of 2'));
        assert.ok(second.querySelector('#count').textContent.includes('of 2'));
        first.querySelector('#refresh').onclick();
        assert.strictEqual(fresh[0][0],'refresh');
        """
        result = subprocess.run([NODE, "-", json.dumps(data)], input=program,
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cart_details_match_table_and_update_safely(self):
        malicious = '<img src=x onerror="bad()">'
        data = {
            "payload": [row("ABC", [], reason=malicious, summary="Long summary",
                            ind={"metric": [malicious, "pos"], "holding_quantity": ["12", "neu"],
                                 "holding_value": ["₹12345678.90123", "neu"]},
                            checks=[["Check", malicious, "p"]],
                            groupRatings={"Trend": {"positive": 1, "rated": 1}},
                            extraGroups=[["Holding details", [["holding_quantity", "Holding qty"],
                                                              ["holding_value", "Market value"]]]],
                            extraSections=[{"title": "Recent company headlines",
                                            "note": malicious,
                                            "items": [{"title": malicious, "url": "javascript:alert(1)",
                                                       "publisher": malicious, "sentiment": 1},
                                                      {"title": "Safe", "url": "https://example.com/news"}]}])],
            "config": {"signals": ["BUY"], "rawCols": [], "columns": [["stock", "Stock", 0]],
                       "groups": [["Trend", [["metric", "Metric"]]]],
                       "info": {"metric": ["Help", "javascript:alert(1)"]}, "checkInfo": {}},
        }
        prefix = _TABLE_JS.split("const VIEW={rows:[],slice:[]};", 1)[0]
        js = _DETAIL_JS.replace("export default function(component)", "function componentRender(component)")
        program = r"""
        const assert=require('assert');
        const data=JSON.parse(process.argv[2]);
        function parent() {
         const els={};
         return {querySelector(s){return els[s] ||= {innerHTML:'',querySelectorAll(){return []}}}};
        }
        """ + js + "\nfunction tableDetail(inputData) {\n" + prefix + r"""
        return detail(DATA[0]);
        }
        const first=parent(),second=parent();
        componentRender({parentElement:first,data});
        const html=first.querySelector('#cartDetail').innerHTML;
        assert.strictEqual(html,tableDetail(data));
        for(const text of ['Trend &amp; momentum','Holding details','News &amp; events',
                           'Research signal checks','Long summary','&lt;img','https://example.com/news',
                           'title="₹12345678.90123">₹1.23Cr</span>'])
         assert.ok(html.includes(text),text);
        assert.ok(!html.includes('<img'));
        assert.ok(!html.includes('href="javascript:'));
        const secondData=JSON.parse(JSON.stringify(data));
        secondData.payload[0].reason='Updated reason';
        secondData.config.groups[0][1][0][1]='Updated metric';
        componentRender({parentElement:second,data:secondData});
        assert.ok(second.querySelector('#cartDetail').innerHTML.includes('Updated reason'));
        assert.strictEqual(first.querySelector('#cartDetail').innerHTML,html);
        first.querySelector('#cartDetail').querySelectorAll=()=>
         [{dataset:{panel:JSON.stringify(['ABC','category:Trend & momentum'])}}];
        componentRender({parentElement:first,data:secondData});
        const updated=first.querySelector('#cartDetail').innerHTML;
        assert.ok(updated.includes('data-panel="[&quot;ABC&quot;,&quot;category:Trend &amp; momentum&quot;]" open'));
        assert.strictEqual(updated.replace('" open>', '">'),tableDetail(secondData));
        assert.ok(first.querySelector('#cartDetail').innerHTML.includes('Updated metric'));
        """
        result = subprocess.run([NODE, "-", json.dumps(data)], input=program,
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TableMountTests(unittest.TestCase):
    def test_mount_wires_key_data_and_refresh_callback(self):
        callback = lambda: None
        with patch("features.equities.scan_table._COMPONENT") as component:
            show_scan_table([], {"tableKey": "holdings:one"}, on_refresh=callback)
            self.assertEqual(component.call_args.kwargs["key"], "holdings:one")
            self.assertTrue(component.call_args.kwargs["data"]["canRefresh"])
            self.assertIs(component.call_args.kwargs["on_refresh_change"], callback)
            show_scan_table([], {"tableKey": "scanner:one"})
            self.assertFalse(component.call_args.kwargs["data"]["canRefresh"])
            self.assertTrue(callable(component.call_args.kwargs["on_refresh_change"]))
            self.assertTrue(callable(component.call_args.kwargs["on_add_to_cart_change"]))

    def test_cart_add_preserves_existing_edits_and_holding_quantity(self):
        state = {"stock_cart_review": True}
        rows = [row("ABC.NS", [], quantity=12, signal="SELL"),
                row("abc.ns", [], quantity=3), row("XYZ.BO", [], quantity=-2.5)]
        with patch("features.equities.scan_table.st.session_state", state):
            self.assertEqual(_add_rows_to_cart(rows, [0, 1, 2, -1, True]), (2, 1))
            self.assertEqual(state["stock_cart"][0]["quantity"], -12)
            self.assertEqual(state["stock_cart"][0]["holding_quantity"], 12)
            self.assertEqual(state["stock_cart"][1]["quantity"], 1)
            self.assertIsNone(state["stock_cart"][1]["holding_quantity"])
            self.assertFalse(state["stock_cart_review"])
            state["stock_cart"][0]["quantity"] = -6
            self.assertEqual(_add_rows_to_cart(rows, [0]), (0, 1))
            self.assertEqual(state["stock_cart"][0]["quantity"], -6)
            self.assertEqual(state["stock_cart"][0]["signal"], "SELL")

    def test_component_cart_callback_reads_trigger(self):
        state = {"holdings:one": {"add_to_cart": [0]}}
        config = {"tableKey": "holdings:one", "groups": [["Trend", [["x", "X"]]]]}
        with patch("features.equities.scan_table.st.session_state", state), \
             patch("features.equities.scan_table.st.toast"), \
             patch("features.equities.scan_table._COMPONENT") as component:
            show_scan_table([row("ABC.NS", [])], config)
            component.call_args.kwargs["on_add_to_cart_change"]()
        self.assertEqual(state["stock_cart"][0]["ticker"], "ABC.NS")
        self.assertEqual(state["stock_cart"][0]["stock_detail"]["config"], config)

    def test_cart_snapshot_is_detached_and_refreshed_on_readd(self):
        state = {}
        original = row("ABC", [], quantity=8, extraSections=[{"title": "News", "note": "old"}])
        config = {"groups": [["Trend", [["x", "Old"]]]]}
        with patch("features.equities.scan_table.st.session_state", state):
            _add_rows_to_cart([original], [0], config)
            item = state["stock_cart"][0]
            item["quantity"] = -3
            original["extraSections"][0]["note"] = "new"
            config["groups"][0][1][0][1] = "New"
            self.assertEqual(item["stock_detail"]["row"]["extraSections"][0]["note"], "old")
            self.assertEqual(item["stock_detail"]["config"]["groups"][0][1][0][1], "Old")
            self.assertEqual(_add_rows_to_cart([original], [0], config), (0, 1))
            self.assertEqual(item["quantity"], -3)
            self.assertEqual(item["stock_detail"]["row"]["extraSections"][0]["note"], "new")
            self.assertEqual(item["stock_detail"]["config"]["groups"][0][1][0][1], "New")
            original["extraSections"][0]["note"] = "later"
            self.assertEqual(item["stock_detail"]["row"]["extraSections"][0]["note"], "new")

    def test_cart_detail_mount_and_legacy_fallback(self):
        saved = {"row": row("ABC", []), "config": {"groups": []}}
        with patch("features.equities.scan_table._DETAIL_COMPONENT") as component, \
             patch("features.equities.scan_table.st.info") as info:
            render_cart_stock_details({"id": "ABC", "stock_detail": saved})
            self.assertEqual(component.call_args.kwargs["data"],
                             {"payload": [saved["row"]], "config": saved["config"]})
            self.assertTrue(component.call_args.kwargs["key"].startswith("cart-stock-detail-"))
            render_cart_stock_details({"id": "legacy"})
            info.assert_called_once()
            self.assertEqual(component.call_count, 1)


if __name__ == "__main__":
    unittest.main()
