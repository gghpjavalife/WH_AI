"""Interactive, browser-local table shared by scanner and holdings research."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from copy import deepcopy

import streamlit as st

TABLE_HEIGHT = 640

_TEMPLATE = r"""
<!doctype html><html><head><meta charset="utf-8"><style>
:host,.card{--pos:#22C55E;--neg:#F87171;--gold:#D4AF37;--fg:var(--st-text-color,#E5E7EB);--bg:var(--st-background-color,#20232a);
 --soft:rgba(128,128,128,.10);--line:rgba(128,128,128,.28);--muted:rgba(128,128,128,.95);}
*{box-sizing:border-box;}
.card{color:var(--st-text-color,var(--fg));
 font:14px/1.45 "Source Sans Pro",system-ui,-apple-system,"Segoe UI",sans-serif;}
.card{display:flex;flex-direction:column;min-width:0;border:2px solid var(--gold);
 border-radius:14px;position:relative;}
.bar{display:flex;flex-wrap:wrap;gap:.6rem .9rem;align-items:center;padding:.7rem .9rem;
 border-bottom:2px solid var(--gold);background:rgba(201,162,39,.10);}
.bar input[type=search]{flex:1 1 14rem;min-width:11rem;padding:.4rem .7rem;border-radius:8px;
 border:1px solid var(--line);background:var(--soft);color:var(--fg);font:inherit;}
.chips{display:flex;gap:.35rem;flex-wrap:wrap;}
.chip{cursor:pointer;user-select:none;padding:.2rem .7rem;border-radius:999px;font-size:.78rem;
 font-weight:700;border:1px solid var(--line);opacity:.45;background:transparent;color:var(--fg);}
.chip.on{opacity:1;}
.chip[data-s="STRONG BUY"].on{background:#16A34A;color:#fff;border-color:#16A34A;}
.chip[data-s="BUY"].on{background:rgba(34,197,94,.22);color:var(--pos);border-color:var(--pos);}
.chip[data-s="HOLD"].on{background:rgba(234,179,8,.22);color:#EAB308;border-color:#EAB308;}
.chip[data-s="SELL"].on{background:rgba(239,68,68,.22);color:var(--neg);border-color:var(--neg);}
.bar label{display:flex;gap:.35rem;align-items:center;font-size:.85rem;cursor:pointer;}
.count{font-size:.82rem;color:var(--muted);}
.scroll{overflow-x:auto;container-type:inline-size;}
table{width:100%;min-width:max-content;table-layout:auto;border-collapse:separate;border-spacing:0;}
th{background:var(--bg);padding:.55rem .8rem;text-align:left;
 font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);
 border-bottom:2px solid var(--gold);cursor:pointer;white-space:nowrap;user-select:none;
 }
.filt td{padding:.4rem .8rem;background:rgba(201,162,39,.08);border-bottom:2px solid var(--gold);}
.filt input,.filt select{width:100%;min-width:0;padding:.25rem .4rem;border-radius:6px;border:1px solid var(--line);background:var(--soft);color:var(--fg);font:inherit;font-size:.82rem;}
.filt select option{color:#111;}
.filt .rng{display:flex;gap:.25rem;}
.filt .rng input{width:50%;}
.filt details{min-width:5rem;}
.filt summary{cursor:pointer;color:var(--muted);font-size:.78rem;white-space:nowrap;}
.filt details[open] summary{margin-bottom:.3rem;}
.extra-filters{position:relative;padding:.6rem .9rem;border-bottom:1px solid var(--line);}
.extra-filters .controls,.extra-filter{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;}
.extra-filters .controls{justify-content:flex-end;}
.extra-filters .controls .btn{padding:.4rem .8rem;font-size:.84rem;font-weight:600;}
.checkout-action{margin-right:auto;}
.cart-warning{position:absolute;z-index:6;left:.9rem;top:calc(100% - .2rem);padding:.8rem;
 max-width:min(24rem,calc(100% - 1.8rem));border:1px solid var(--gold);border-radius:10px;
 background:var(--st-background-color,#20232a);box-shadow:0 12px 30px rgba(0,0,0,.3);}
.cart-warning[hidden]{display:none;}
.cart-warning .btn{margin-top:.5rem;}
.row-select,.select-all{accent-color:var(--gold);cursor:pointer;}
th.select-column,td.select-column{text-align:center;padding-left:.5rem;padding-right:.5rem;}
.field-picker{position:absolute;top:calc(100% - .2rem);right:.9rem;z-index:5;
 width:min(32rem,calc(100% - 1.8rem));
 padding:.8rem;border:1px solid var(--gold);border-radius:12px;
 background:var(--st-background-color,#20232a);box-shadow:0 12px 30px rgba(0,0,0,.3);}
.field-picker[hidden]{display:none;}
.field-picker input[type=search]{width:100%;}
.picker-heading{display:flex;align-items:center;justify-content:space-between;gap:.5rem;
 margin-bottom:.55rem;font-weight:700;color:var(--fg);}
.picker-close{border:0;background:transparent;color:var(--muted);font:inherit;
 font-size:1.2rem;line-height:1;cursor:pointer;padding:.15rem .35rem;}
.picker-close:hover{color:var(--gold);}
.field-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(12rem,100%),1fr));gap:.3rem;
 max-height:14rem;overflow:auto;margin-top:.4rem;}
.field-list label,.select-fields{display:flex;align-items:center;gap:.4rem;font-size:.85rem;}
.field-list label{padding:.35rem;border-radius:6px;cursor:pointer;min-width:0;overflow-wrap:anywhere;}
.field-list label:hover{background:rgba(201,162,39,.15);}
.field-list .field-section{grid-column:1 / -1;color:var(--muted);font-size:.76rem;font-weight:700;padding:.2rem .35rem;}
.field-list label.selected{background:rgba(201,162,39,.10);}
.extra-filter{margin-top:.4rem;}
.extra-filters select,.extra-filters input{padding:.25rem .4rem;border-radius:6px;border:1px solid var(--line);background:var(--soft);color:var(--fg);font:inherit;}
.extra-filters select option{color:#111;}
.extra-filter input{width:7rem;}
th.nosort{cursor:default;}
th .arrow{display:inline-block;width:1em;}
th.raw-head{text-transform:none;letter-spacing:normal;}
.remove-field{margin-left:.3rem;border:0;border-radius:4px;padding:.05rem .3rem;
 background:transparent;color:var(--muted);font:inherit;font-size:1rem;cursor:pointer;}
.remove-field:hover,.remove-field:focus-visible{color:var(--neg);background:var(--soft);}
th.num,td.num{text-align:left;font-variant-numeric:tabular-nums;}
td{padding:.55rem .8rem;border-bottom:1px solid var(--line);vertical-align:middle;text-align:left;
 white-space:normal;overflow-wrap:anywhere;}
td.raw-value{min-width:8rem;max-width:22rem;white-space:pre-wrap;overflow-wrap:anywhere;}
tr.row{cursor:pointer;}
tr.row:hover td{background:rgba(201,162,39,.12);}
tr.row.open td{background:rgba(201,162,39,.18);}
.name{font-weight:600;}
.name small{display:block;color:var(--muted);font-weight:400;font-size:.76rem;}
.caret{display:inline-block;width:1em;color:var(--gold);transition:transform .15s;}
tr.open .caret{transform:rotate(90deg);}
.signal-value{font-weight:600;}
.signal-value.SB,.signal-value.B{color:var(--pos);}
.signal-value.S,.signal-value.SS{color:var(--neg);}
.signal-value.H{color:var(--fg);}
.signal-value.NR{color:var(--muted);}
.pos{color:var(--pos);font-weight:600;}.neg{color:var(--neg);font-weight:600;}
.neu,.missing{color:var(--fg);}
.meter{display:flex;align-items:center;gap:.4rem;min-width:7rem;}
.meter i{flex:1;height:7px;border-radius:999px;background:var(--line);overflow:hidden;}
.meter i b{display:block;height:100%;background:linear-gradient(90deg,#E6B800,#22C55E);}
.tag{display:inline-block;margin:.1rem .25rem .1rem 0;padding:.05rem .5rem;border-radius:999px;
 font-size:.76rem;background:rgba(34,197,94,.14);border:1px solid rgba(34,197,94,.5);}
.tag.news{white-space:normal;text-decoration:none;color:var(--fg);max-width:100%;}
.tag.news.pos{color:var(--pos);border-color:var(--pos);}
.tag.news.neg{color:var(--neg);border-color:var(--neg);background:rgba(239,68,68,.12);}
.tag.news.neu{color:var(--fg);border-color:var(--line);background:var(--soft);}
.tag.news:hover{background:rgba(201,162,39,.2);border-color:var(--gold);}
tr.detail td{padding:0;background:var(--soft);}
.panel{position:sticky;left:0;width:100cqi;max-width:100%;
 padding:1rem 1.2rem 1.2rem;animation:pop .18s ease-out;}
@keyframes pop{from{opacity:0;transform:translateY(-4px);}to{opacity:1;transform:none;}}
.verdict{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap;margin-bottom:.8rem;
 font-size:.95rem;}
.groups{display:flex;flex-direction:column;gap:.6rem;}
.group{border:1px solid var(--line);border-radius:10px;padding:.6rem .8rem;background:var(--bg);min-width:0;}
.group>summary{cursor:pointer;color:var(--gold);font-size:.8rem;font-weight:700;
 letter-spacing:.04em;overflow-wrap:anywhere;}
.group[open]>summary{margin-bottom:.55rem;}
.group-rating{display:inline;font-size:.78rem;letter-spacing:normal;text-transform:none;
 margin-left:.5rem;font-weight:600;}
.subgroup+.subgroup{margin-top:.8rem;border-top:1px solid var(--line);padding-top:.6rem;}
.subgroup-title{margin:0 0 .4rem;color:var(--muted);font-size:.77rem;font-weight:700;}
.coverage{margin:.35rem 0;color:var(--muted);font-size:.8rem;}
.check-details{margin:.4rem 0;}
.check-details summary{cursor:pointer;color:var(--gold);font-size:.82rem;}
.check-details p{color:var(--muted);font-size:.8rem;}
.metrics-grid{display:flex;flex-wrap:wrap;gap:.3rem 1.4rem;align-items:baseline;}
.kv{display:flex;align-items:baseline;gap:.5rem;padding:.12rem 0;font-size:.85rem;
 min-width:0;overflow-wrap:anywhere;white-space:pre-wrap;}
.metrics-grid>.kv{flex:0 1 auto;max-width:100%;}
.metrics-grid>.kv.full-width{flex-basis:100%;}
.kv>span{min-width:0;}
.kv details{min-width:0;max-width:70%;}
.kv details summary{cursor:pointer;color:var(--gold);}
.kv span:first-child{color:var(--muted);}
.i{display:inline-flex;align-items:center;justify-content:center;width:.95rem;height:.95rem;margin-left:.3rem;border:1px solid var(--gold);border-radius:50%;color:var(--gold);font-size:.62rem;font-weight:700;font-style:normal;text-decoration:none;line-height:1;vertical-align:middle;}
.i:hover{background:var(--gold);color:#111;}
.kv span:last-child{font-weight:600;font-variant-numeric:tabular-nums;}
.ck{display:inline-flex;gap:.35rem;align-items:center;margin:.15rem .3rem .15rem 0;
 padding:.12rem .6rem;border-radius:999px;font-size:.78rem;border:1px solid var(--line);}
.ck.p{border-color:var(--pos);background:rgba(34,197,94,.12);}
.ck.n{border-color:var(--neg);background:rgba(239,68,68,.12);}
.foot{display:flex;flex-wrap:wrap;gap:.6rem 1rem;align-items:center;padding:.6rem .9rem;
 border-top:2px solid var(--gold);background:rgba(201,162,39,.10);font-size:.85rem;}
.foot input[type=number]{width:4.6rem;padding:.25rem .4rem;border-radius:8px;
 border:1px solid var(--line);background:var(--soft);color:var(--fg);font:inherit;}
.btn{cursor:pointer;padding:.25rem .65rem;border-radius:8px;border:1px solid var(--gold);
 background:transparent;color:var(--fg);font:inherit;}
.btn:hover:not(:disabled){background:rgba(201,162,39,.25);}
.btn:disabled{opacity:.35;cursor:default;}
.pager{display:flex;gap:.35rem;align-items:center;}
.spacer{margin-left:auto;display:flex;gap:.5rem;align-items:center;}
.empty{padding:2rem;text-align:center;color:var(--muted);}
.toast{position:absolute;right:1rem;bottom:4.2rem;background:var(--gold);color:#111;
 padding:.35rem .8rem;border-radius:8px;font-weight:600;opacity:0;transition:opacity .2s;}
.toast.show{opacity:1;}
@media (prefers-reduced-motion:reduce){.panel{animation:none;}}
</style></head><body>
<div class="card" id="card">
 <div class="extra-filters" id="extraFilters">
  <div class="controls" id="filterControls">
   <button class="btn checkout-action" id="addToCart" type="button" title="Select tickers to add to your cart, then review them and trigger checkout from Cart.">Add to cart</button>
   <button class="btn" id="refresh" type="button" hidden>Refresh</button>
   <button class="btn" id="fieldToggle" type="button" aria-expanded="false" aria-controls="fieldPicker">Add filters</button>
   <button class="btn" id="clearFilters" type="button">Clear filters</button></div>
  <div class="cart-warning" id="cartWarning" role="alertdialog" aria-labelledby="cartWarningText" hidden>
   <strong id="cartWarningText">Select at least one stock to add to your cart.</strong>
   <div><button class="btn" id="cartWarningClose" type="button">OK</button></div>
  </div>
  <div class="field-picker" id="fieldPicker" role="dialog" aria-label="Add filter columns" hidden>
   <div class="picker-heading"><span>Choose columns</span><button class="picker-close" id="fieldClose" type="button" aria-label="Close column picker">&times;</button></div>
   <input id="fieldSearch" type="search" placeholder="Search fields" aria-label="Search fields">
   <div class="field-list" id="fieldList"></div>
  </div>
 </div>
 <div class="scroll"><table><thead><tr id="head"></tr><tr id="filt" class="filt"></tr></thead><tbody id="body"></tbody></table></div>
 <div class="foot">
  <div class="pager">
   <button class="btn" id="first" title="First page">&#9198;</button>
   <button class="btn" id="prev" title="Previous page">&#9664;</button>
   <span>Page <input id="page" type="number" min="1" value="1"> of <b id="pages">1</b></span>
   <button class="btn" id="next" title="Next page">&#9654;</button>
   <button class="btn" id="last" title="Last page">&#9197;</button>
  </div>
  <span>Records per page <input id="size" type="number" min="1" max="500" value="10"></span>
  <span class="count" id="count"></span>
  <span class="spacer">
   <button class="btn" id="copy">Copy page</button>
   <button class="btn" id="csv">Download CSV</button>
  </span>
 </div>
</div>
<div class="toast" id="toast"></div>
<script>
let DATA=inputData.payload, CFG=inputData.config;
const $=id=>parentElement.querySelector('#'+id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const RANK={'STRONG BUY':4,'BUY':3,'HOLD':2,'SELL':1,'STRONG SELL':0,'NOT RATED':-1};
const PILL={'STRONG BUY':'SB','BUY':'B','HOLD':'H','SELL':'S','STRONG SELL':'SS','NOT RATED':'NR'};
const st={f:{q:'',pmin:'',pmax:'',chg:'',rmin:'',rmax:'',sig:'',m1:'',vol:'',str:'',pass:''},ranges:Object.create(null),extra:Object.create(null),key:'signal',dir:-1,page:1,size:10,open:new Set(),panelOpen:new Set(),selected:new Set()};
const sortVal={
 stock:r=>r.label.toLowerCase(),price:r=>r.price,chg:r=>r.chg,m1:r=>r.m1,vol:r=>r.vol,rsi:r=>r.rsi,
 signal:r=>(RANK[r.signal]??-1)*1000+(Number(r.score)||0),strength:r=>r.total?r.pos/r.total:0,
 pass:r=>r.pass?1:0};
let COLS;
function buildColumns(){
 COLS=(CFG.columns||[['stock','Stock',0],['signal','Signal',0],['price','Price',1],['chg','Day change',1],['m1','1 month',1],['rsi','RSI 14',1],
  ['vol','Volume',1],['strength','Strength',0]]).map(c=>[...c]);
 if(CFG.showFilters&&!COLS.some(c=>c[0]==='pass'))COLS.push(['pass','Filters',0]);
 COLS.forEach(([key])=>{if(!sortVal[key])sortVal[key]=r=>r[key];});
}
buildColumns();
const tone=(v,pos)=>v==null?'neu':(pos?'pos':'neg');
const number=v=>{
 if(typeof v!=='number'&&(typeof v!=='string'||!v.trim()))return null;
 const n=Number(v);return Number.isFinite(n)?n:null;
};
const inRange=(value,min,max)=>{
 const lo=number(min),hi=number(max);
 if(lo===null&&hi===null)return true;
 const n=number(value);return n!==null&&(lo===null||n>=lo)&&(hi===null||n<=hi);
};
const text=v=>{
 if(typeof v==='boolean')return String(v);
 return typeof v==='string'&&v.trim()?v:null;
};
const friendlyLabel=label=>String(label).replace(/([a-z0-9])([A-Z])/g,'$1 $2')
 .replace(/[_-]+/g,' ').replace(/\s+/g,' ').trim()
 .replace(/(^|[·]\s*)([a-z])/g,(_,prefix,letter)=>prefix+letter.toUpperCase());
const fieldPriority=label=>{
 const s=label.toLowerCase();
 if(/\b(signal|score|rating|eligibility)\b/.test(s))return 0;
 if(/\b(price|return|change|p.?l|profit|quantity|value)\b/.test(s))return 1;
 if(/\b(sector|industry|event|news)\b/.test(s))return 2;
 if(/\b(rsi|volume|momentum|trend|technical)\b/.test(s))return 3;
 if(/\b(valuation|fundamentals|earnings|revenue|debt)\b/.test(s))return 4;
 return 5;
};
let rawFields;
let orderedFields;
function buildFields(){
 rawFields=(CFG.rawCols||[]).map((label,index)=>{
 const type=Array.isArray(CFG.rawTypes)?CFG.rawTypes[index]:
  (DATA.some(r=>number((r.raw||[])[index])!==null)?'number':null);
 const values=type==='text'?[...new Set(DATA.map(r=>text((r.raw||[])[index])).filter(v=>v!==null))].sort():[];
 const unit=(CFG.rawUnits||{})[label];
 const suffix=unit==='fraction'?' (fraction: 0.1 = 10%)':unit==='percentage points'?' (%)':'';
 const full=friendlyLabel(label),parts=full.split(/\s*·\s*/),metric=parts.pop();
 return {label:full+suffix,rawLabel:label,metric,category:parts.join(' · '),suffix,index,type,values};
}).filter(f=>(f.type==='number'||f.type==='text')&&!(CFG.hiddenRawCols||[]).includes(f.rawLabel));
 const counts=new Map();
 for(const field of rawFields){const key=field.metric.toLowerCase();counts.set(key,(counts.get(key)||0)+1);}
 for(const [,label] of COLS){const key=friendlyLabel(label).toLowerCase();counts.set(key,(counts.get(key)||0)+1);}
 for(const field of rawFields){
  const ambiguous=counts.get(field.metric.toLowerCase())>1;
  field.header=field.metric+(ambiguous?' · '+(field.category||'Raw'):'')+field.suffix;
 }
 const headers=new Map();
 for(const field of rawFields){const key=field.header.toLowerCase();headers.set(key,(headers.get(key)||0)+1);}
 for(const field of rawFields)if(headers.get(field.header.toLowerCase())>1)field.header+=` [${field.index+1}]`;
 orderedFields=[...rawFields].sort((a,b)=>fieldPriority(a.label)-fieldPriority(b.label)||a.label.localeCompare(b.label)||a.index-b.index);
}
buildFields();
const selectedFields=()=>Object.keys(st.extra).map(i=>rawFields.find(f=>f.index===Number(i))).filter(Boolean);
const extraMatch=(index,filter,r)=>{
 const value=(r.raw||[])[index];
 if(filter.type==='number')return inRange(value,filter.min,filter.max);
 const search=filter.search.trim().toLowerCase();
 if(filter.choice===''&&!search)return true;
 const v=text(value);if(v===null)return false;
 const field=rawFields.find(f=>f.index===Number(index));
 return (filter.choice===''||v===field.values[Number(filter.choice)])&&
  (!search||v.toLowerCase().includes(search));
};

function filtered(){
 const f=st.f,q=f.q.trim().toLowerCase();
 const n=number;
 const pmin=n(f.pmin),pmax=n(f.pmax),rmin=n(f.rmin),rmax=n(f.rmax),smin=n(f.str),m1min=n(f.m1),vmin=n(f.vol);
 let rows=DATA.filter(r=>
  (!q||r.ticker.toLowerCase().includes(q)||r.label.toLowerCase().includes(q))&&
  inRange(r.m1,m1min,'')&&inRange(r.vol,vmin,'')&&inRange(r.price,pmin,pmax)&&
  (!f.chg||(number(r.chg)!==null&&(f.chg==='up'?number(r.chg)>=0:number(r.chg)<0)))&&
  inRange(r.rsi,rmin,rmax)&&
  (!f.sig||r.signal===f.sig)&&
  (smin==null||(number(r.total)>0&&number(r.pos)!==null&&r.pos/r.total*100>=smin))&&
  (!f.pass||(f.pass==='y')===r.pass)&&
  Object.entries(st.ranges).every(([key,range])=>inRange(r[key],range.min,range.max))&&
  Object.entries(st.extra).every(([index,filter])=>extraMatch(index,filter,r)));
 const rawIndex=st.key.startsWith('raw:')?Number(st.key.slice(4)):null;
 const field=rawFields.find(f=>f.index===rawIndex);
 const g=field?(r=>field.type==='number'?number((r.raw||[])[rawIndex]):text((r.raw||[])[rawIndex])):sortVal[st.key];
 rows.sort((a,b)=>{const x=g(a),y=g(b);
  if(x==null&&y!=null)return 1;if(y==null&&x!=null)return -1;
  const c=x<y?-1:x>y?1:0;return (c||a.ticker.localeCompare(b.ticker))*st.dir;});
 return rows;
}
const info=(k,r,label)=>{
 const i=(CFG.info||{})[k], description=(r.contextInfo||{})[k], shared=(CFG.fieldInfo||{})[k];
 const help=[i?.[0],shared,description].filter(Boolean).join(' ')||
  `${label||k||'Value'}: reported information; no directional assessment is implied.`;
 const url=i?.[1]?safeNewsUrl(i[1]):null;
 return url?`<a class="i" href="${url}" target="_blank" rel="noopener noreferrer" title="${esc(help)}" aria-label="${esc(help)}">i</a>`:
  `<span class="i" tabindex="0" title="${esc(help)}" aria-label="${esc(help)}">i</span>`;
};
function panel(r,id,title,body,positive=0,rated=0){
 const panelKey=JSON.stringify([r.ticker,id]);
 const color=rated?(positive>rated-positive?'pos':positive<rated-positive?'neg':'neu'):'neu';
 return `<details class="group" data-panel="${esc(panelKey)}"${st.panelOpen.has(panelKey)?' open':''}>
  <summary>${esc(title)}${rated?` <span class="group-rating ${color}">${positive} positive of ${rated} assessed</span>`:''}</summary>${body}</details>`;
}
const safeNewsUrl=v=>{try{const u=new URL(v);return u.protocol==='https:'&&!u.username&&!u.password?esc(u.href):null;}catch{return null;}};
const panelCategories={
 'Risk and range':'Risk & intraday','Intraday market':'Risk & intraday',
 'Context overview':'Risk & intraday','Local stock context gate':'Risk & intraday',
 'Trend':'Trend & momentum','Momentum':'Trend & momentum',
 'Indicator availability':'Trend & momentum','Research signal checks':'Trend & momentum',
 'Performance and volume':'Performance & volume',
 'Fundamentals':'Fundamentals & outlook','Annual growth and earnings quality':'Fundamentals & outlook',
 'Analyst consensus':'Fundamentals & outlook','Fundamental screen':'Fundamentals & outlook',
 'Corporate actions':'News & events','Recent company headlines':'News & events','Context headlines':'News & events',
 'Company profile':'Company & ownership','Ownership and trading activity':'Company & ownership',
 'Market and sector':'Market context','Options':'Derivatives','Futures':'Derivatives',
 'Local stock context':'Data availability','Unavailable context sections':'Data availability',
 'Technical indicators':'Data availability','Data availability':'Data availability'
};
const panelOrder=['Company & ownership','Risk & intraday','Trend & momentum','Performance & volume',
 'Fundamentals & outlook','News & events','Market context','Derivatives',
 'Holding details','Data availability'];
const panelCountFields=new Set([
 'context_profile_full_time_employees','context_fundamentals_shares_outstanding',
 'context_shareholders_total_institutional_holders','context_shareholders_net_shares_bought_quarterly',
 'context_technicals_current_volume','context_technicals_avg_volume_3m'
]);
function compactIndianValue(value,prefix){
 const match=String(value).trim().match(/^([+-]?)(?:(\d{1,3}(?:,\d{3})+|\d+))(?:\.(\d+))?$/);
 if(!match)return null;
 const sign=match[1],whole=match[2].replace(/,/g,''),fraction=match[3]||'';
 const integer=BigInt(whole);
 const unit=integer>=10000000n?{value:10000000n,label:'Cr'}:
  integer>=100000n?{value:100000n,label:'L'}:null;
 if(!unit)return null;
 const scale=10n**BigInt(fraction.length);
 const numerator=integer*scale+BigInt(fraction||'0');
 const denominator=unit.value*scale;
 const rounded=(numerator*100n+denominator/2n)/denominator;
 const digits=rounded.toString().padStart(3,'0');
 const unitInteger=digits.slice(0,-2);
 const grouped=unitInteger.length>3?
  unitInteger.slice(0,-3).replace(/\B(?=(\d{2})+(?!\d))/g,',')+','+unitInteger.slice(-3):unitInteger;
 return `${sign==='-'&&rounded!==0n?'-':''}${prefix}${grouped}.${digits.slice(-2)}${unit.label}`;
}
function compactPanelValue(key,value,currency){
 const amount=String(value).trim().match(/^(₹\s*|INR\s+)(.+)$/i);
 if(amount)return compactIndianValue(amount[2],'₹');
 if(currency==='INR'&&/^[-+]?\d+(?:\.\d+)?$/.test(String(value).trim()))
  return compactIndianValue(value,'₹')??`₹${value}`;
 return panelCountFields.has(key)?compactIndianValue(value,''):null;
}
function detail(r){
 const categories=new Map();
 const add=(title,body,positive=0,rated=0)=>{
  if(!body)return;
  const category=panelCategories[title]||title;
  if(!categories.has(category))categories.set(category,{parts:[],positive:0,rated:0});
  const entry=categories.get(category);
  entry.parts.push({title,body});
  entry.positive+=positive;entry.rated+=rated;
 };
 [...(CFG.groups||[]),...(r.extraGroups||[])].forEach(([title,items])=>{
  const holdingColumns={holding_quantity:'quantity',holding_average:'average',holding_value:'holdingValue',
   holding_pnl:'pnl',holding_return:'returnPct',holding_allocation:'allocationPct'};
  const present=items.filter(([k])=>r.ind[k]&&(title!=='Holding details'||
   (!COLS.some(c=>c[0]===holdingColumns[k])&&r.ind[k][1]!=='unknown')));
  const tone=k=>{
   const [value,t]=r.ind[k];
   if(t==='unknown'||value==null||/^(unavailable|n\/a|—)$/i.test(String(value).trim()))return 'missing';
   return ['pos','neg','neu'].includes(t)?t:'neu';
  };
  const score=(r.groupRatings||{})[title]||{positive:0,rated:0,label:'Not rated'};
  const green=Number(score.positive)||0, rated=Number(score.rated)||0;
  const kv=present.map(([k,l])=>{
   const value=String(r.ind[k][0]);
   const compact=compactPanelValue(k,value,(r.panelCurrencies||{})[k]),abbreviated=compact!==null&&compact!==value;
   const rendered=abbreviated?compact:value;
   const tooltip=abbreviated?` title="${esc(value)}"`:'';
   const displayed=rendered.length>160?`<details class="${tone(k)}"${tooltip}><summary>View full details</summary>${esc(rendered)}</details>`:
    `<span class="${tone(k)}"${tooltip}>${esc(rendered)}</span>`;
   return `<div class="kv${value.length>160?' full-width':''}"><span>${esc(l)}${info(k,r,l)}</span>${displayed}</div>`;
  }).join('');
  if(kv)add(title,`<div class="metrics-grid">${kv}</div>`,green,rated);
 });
 const coverage=r.technicalCoverage;
 if(coverage){
  const source=coverage.contextAvailable?'Local daily context':'Local daily context unavailable';
  add('Indicator availability',`<p class="coverage">${source}: ${Number(coverage.available)||0} of ${Number(coverage.total)||0} fields available. Unavailable fields may reflect insufficient history or missing data; research values can be shown as a fallback.</p>`);
 }
 const checks=r.checks.map(([n,v,s])=>
  `<span class="ck ${s==='p'?'p pos':s==='n'?'n neg':'neu'}">${s==='p'?'&#10003;':s==='n'?'&#10007;':'&#8211;'} ${esc(n)}: ${esc(v)}${info((CFG.checkInfo||{})[n],r,n)}</span>`).join('');
 (r.extraSections||[]).forEach(section=>{
  const items=section.items||[];
  const links=items.map(item=>{
   const url=safeNewsUrl(item.url),cls=`tag news ${item.sentiment===1?'pos':item.sentiment===-1?'neg':'neu'}`;
   const metadata=`${item.publisher||'Unknown'} · ${item.published||'Unknown'} · ${item.sentimentLabel||'Not assessed'}`;
   const title=url?`<a class="${cls}" href="${url}" target="_blank" rel="noopener noreferrer" title="${esc(metadata)}">${esc(item.title)}</a>`:
    `<span class="${cls}" title="${esc(metadata)}">${esc(item.title)}</span>`;
   return `${title}${info('headline',{contextInfo:{headline:`Publisher: ${item.publisher||'Unknown'}; published: ${item.published||'Unknown'}. ${item.sentimentLabel||'Not assessed'}. Keyword sentiment is not investment advice.`}},'headline')}`;
  }).join('');
  const values=(section.values||[]).map(([label,value])=>
   `<div class="kv"><span>${esc(label)}${info(null,r,label)}</span><span class="neu">${esc(value)}</span></div>`).join('');
  const directional=items.filter(item=>item.sentiment===1||item.sentiment===-1);
  add(section.title,`${section.note?`<p>${esc(section.note)}</p>`:''}${links}${values?`<div class="metrics-grid">${values}</div>`:''}`,
   directional.filter(item=>item.sentiment===1).length,directional.length);
 });
 if(r.checks.length)add('Research signal checks',`<details class="check-details"><summary>How the research signal was scored (${Number(r.pos)||0} positive of ${Number(r.total)||0} assessed)</summary><p>These are the original research checks and inputs; they can differ from the newer context values above.</p>${checks}</details>`);
 const groups=[...categories.entries()].sort(([a],[b])=>{
  const ai=panelOrder.indexOf(a),bi=panelOrder.indexOf(b);
  return (ai<0?panelOrder.length:ai)-(bi<0?panelOrder.length:bi)||a.localeCompare(b);
 }).map(([title,entry])=>{
  const body=entry.parts.map(part=>`<div class="subgroup">${entry.parts.length>1||part.title!==title?`<div class="subgroup-title">${esc(part.title)}</div>`:''}${part.body}</div>`).join('');
  return panel(r,`category:${title}`,title,body,entry.positive,entry.rated);
 }).join('');
 return `<div class="panel"><div class="verdict"><span class="signal-value ${PILL[r.signal]||'H'}">${esc(r.signal)}</span>
  <span>${esc(r.reason)}</span></div>${r.summary?`<p>${esc(r.summary)}</p>`:''}<div class="groups">${groups}</div></div>`;
}
const VIEW={rows:[],slice:[]};
function render(){
 const rows=filtered(), size=Math.max(1,Math.min(500,st.size|0||10));
 const pages=Math.max(1,Math.ceil(rows.length/size));
 st.page=Math.min(Math.max(1,st.page|0||1),pages);
 const start=(st.page-1)*size, slice=rows.slice(start,start+size);
 const fields=selectedFields();
 const rowIndexes=new Map(DATA.map((row,index)=>[row,index]));
 $('head').innerHTML='<th class="nosort"></th><th class="nosort select-column"><input id="selectAll" class="select-all" type="checkbox" aria-label="Select all filtered stocks" title="Select all filtered stocks"></th>'+COLS.map(([k,l,n])=>
  `<th data-k="${esc(k)}" class="${n?'num':''} ${k==='pass'?'nosort':''}">${esc(l)}<span class="arrow">${st.key===k?(st.dir>0?'&#9650;':'&#9660;'):''}</span></th>`).join('')+
  fields.map(f=>`<th data-k="raw:${f.index}" class="raw-head ${f.type==='number'?'num':''}" title="${esc(f.label)}">${esc(f.header)}<span class="arrow">${st.key==='raw:'+f.index?(st.dir>0?'&#9650;':'&#9660;'):''}</span><button type="button" class="remove-field" data-remove-field="${f.index}" aria-label="Remove ${esc(f.label)} column" title="Remove ${esc(f.label)} column">&times;</button></th>`).join('');
 const cell=(key,type,r)=>{
  if(key==='stock')return `<td class="name">${esc(r.label)}</td>`;
  if(key==='signal')return `<td><span class="signal-value ${PILL[r.signal]||'H'}">${esc(r.signal)}</span></td>`;
  if(key==='strength')return `<td><div class="meter"><i><b style="width:${r.total?Math.round(r.pos/r.total*100):0}%"></b></i><span>${r.pos}/${r.total}</span></div></td>`;
  if(key==='pass')return `<td class="${r.pass?'pos':'neg'}">${r.pass?'&#10003;':'&#10007;'}</td>`;
  const value=r[key], format=type||({price:'currency',chg:'percent',m1:'percent',rsi:'number',vol:'multiplier'}[key]||'text');
  if(value==null||value===''||(typeof value==='number'&&!Number.isFinite(value)))return `<td class="${COLS.find(c=>c[0]===key)?.[2]?'num':''}">—</td>`;
  if(format==='currency'||format==='currency-change')return `<td class="num ${format==='currency-change'?(Number(value)>0?'pos':Number(value)<0?'neg':'neu'):''}">&#8377;${Number(value).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>`;
  if(format==='percent')return `<td class="num ${key==='allocationPct'?'neu':Number(value)>0?'pos':Number(value)<0?'neg':'neu'}">${key!=='allocationPct'&&Number(value)>0?'+':''}${Number(value).toFixed(2)}%</td>`;
  if(format==='multiplier')return `<td class="num neu">${Number(value).toFixed(2)}x</td>`;
  if(format==='number')return `<td class="num ${key==='rsi'&&['pos','neg'].includes((r.ind||{}).RSI_14?.[1])?r.ind.RSI_14[1]:'neu'}">${Number(value).toLocaleString('en-IN',{maximumFractionDigits:2})}</td>`;
  return `<td class="${COLS.find(c=>c[0]===key)?.[2]?'num':''}">${esc(value)}</td>`;
 };
 $('body').innerHTML=slice.length?slice.map(r=>{
  const open=st.open.has(r.ticker);
  const main=`<tr class="row ${open?'open':''}" data-t="${esc(r.ticker)}">
   <td><span class="caret">&#9656;</span></td><td class="select-column"><input class="row-select" type="checkbox" data-row-select="${rowIndexes.get(r)}" aria-label="Select ${esc(r.label)}"${st.selected.has(rowIndexes.get(r))?' checked':''}></td>${COLS.map(([key,, ,type])=>cell(key,type,r)).join('')}${fields.map(f=>{
    const v=(r.raw||[])[f.index],key=(CFG.rawIndicatorKeys||{})[f.rawLabel],t=(r.ind||{})[key]?.[1];
    return `<td class="raw-value ${f.type==='number'?'num':''} ${v==null||v===''?'missing':t==='pos'||t==='neg'?t:'neu'}">${v==null||v===''?'—':esc(v)}</td>`;
   }).join('')}</tr>`;
  return main+(open?`<tr class="detail"><td colspan="${COLS.length+fields.length+2}">${detail(r)}</td></tr>`:'');
 }).join(''):`<tr><td colspan="${COLS.length+fields.length+2}" class="empty">No stocks match these filters.</td></tr>`;
 $('count').textContent=rows.length?`Showing ${(start+1).toLocaleString()}\u2013${(start+slice.length).toLocaleString()} of ${rows.length.toLocaleString()}`:'0 results';
 $('pages').textContent=pages.toLocaleString();
 $('page').value=st.page;$('page').max=pages;$('size').value=size;
 $('first').disabled=$('prev').disabled=st.page<=1;
 $('next').disabled=$('last').disabled=st.page>=pages;
 VIEW.rows=rows;VIEW.slice=slice;
 const count=rows.reduce((n,r)=>n+(st.selected.has(rowIndexes.get(r))?1:0),0);
 const selectAll=$('selectAll');
 selectAll.disabled=!rows.length;
 selectAll.checked=rows.length>0&&count===rows.length;
 selectAll.indeterminate=count>0&&count<rows.length;
}
function tableText(rows,sep){
 const cell=v=>{v=v==null?'':String(v);return sep===','&&/[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;};
 const head=CFG.exportColumns?[...CFG.exportColumns.map(([,label])=>label),...CFG.rawCols]:
  ['Stock','Ticker','Signal','Signal score','Positive checks','Total checks',...CFG.rawCols,'Reason','Passes filters'];
 const lines=[head.map(cell).join(sep)];
 rows.forEach(r=>lines.push((CFG.exportColumns?
  [...CFG.exportColumns.map(([key])=>r[key]??''),...r.raw]:
  [r.label,r.ticker,r.signal,r.score,r.pos,r.total,...r.raw,r.reason,r.pass?'Yes':'No']).map(cell).join(sep)));
 return lines.join('\n');
}
let toastTimer;
function toast(msg){
 const t=$('toast');t.textContent=msg;t.classList.add('show');
 if(toastTimer)clearTimeout(toastTimer);
 toastTimer=setTimeout(()=>{t.classList.remove('show');toastTimer=null;},1800);
}
const opt=(v,l)=>`<option value="${esc(v)}">${esc(l)}</option>`;
const CELL={
 stock:'<input data-f="q" type="search" placeholder="Search symbol or company" aria-label="Search symbol or company">',
 ticker:'<input data-f="q" type="search" placeholder="Search ticker" aria-label="Search ticker">',
 price:'<div class="rng"><input data-f="pmin" type="number" placeholder="Min" aria-label="Minimum price"><input data-f="pmax" type="number" placeholder="Max" aria-label="Maximum price"></div>',
 chg:'<select data-f="chg" aria-label="Day change">'+opt('','All')+opt('up','Gainers')+opt('down','Losers')+'</select>',
 rsi:'<div class="rng"><input data-f="rmin" type="number" placeholder="Min" aria-label="Minimum RSI"><input data-f="rmax" type="number" placeholder="Max" aria-label="Maximum RSI"></div>',
 signal:'<select data-f="sig" aria-label="Signal">'+opt('','All')+CFG.signals.map(s=>opt(s,s)).join('')+'</select>',
 strength:'<select data-f="str" aria-label="Strength">'+opt('','Any')+opt('25','25%+')+opt('50','50%+')+opt('75','75%+')+'</select>',
 m1:'<select data-f="m1" aria-label="One month change">'+opt('','All')+opt('0','Positive')+opt('10','10%+')+'</select>',
 vol:'<select data-f="vol" aria-label="Volume">'+opt('','All')+opt('1','1x+')+opt('2','2x+')+'</select>',
 pass:'<select data-f="pass" aria-label="Filter result">'+opt('','All')+opt('y','Pass')+opt('n','Fail')+'</select>'};
function extraControl(field){
  const index=field.index,filter=st.extra[index],label=esc(field.label);
  return field.type==='number'?`
   <input type="number" step="any" data-extra="${index}" data-bound="min" value="${esc(filter.min)}" placeholder="Min" aria-label="${label} minimum">
   <input type="number" step="any" data-extra="${index}" data-bound="max" value="${esc(filter.max)}" placeholder="Max" aria-label="${label} maximum">`:
   `<select data-extra="${index}" data-bound="choice" aria-label="${label} exact value">
    ${opt('','All values')}${field.values.map((v,i)=>`<option value="${i}"${filter.choice===String(i)?' selected':''}>${esc(v)}</option>`).join('')}</select>
   <input type="search" data-extra="${index}" data-bound="search" value="${esc(filter.search)}" placeholder="Search text" aria-label="${label} text search">`;
}
function renderFieldList(){
 const q=$('fieldSearch').value.trim().toLowerCase();
 const selected=orderedFields.filter(f=>f.index in st.extra);
 const available=orderedFields.filter(f=>!(f.index in st.extra)&&f.label.toLowerCase().includes(q));
 const checkbox=({index,label},checked)=>`<label${checked?' class="selected"':''}><input type="checkbox" data-field="${index}"${checked?' checked':''}> ${esc(label)}</label>`;
 $('fieldList').innerHTML=(selected.length?`<div class="field-section">Selected (${selected.length})</div>${selected.map(f=>checkbox(f,true)).join('')}`:'')+
  (available.length?`<div class="field-section">${q?'Search results':'Available fields'}</div>${available.map(f=>checkbox(f,false)).join('')}`:
   '<span class="field-section">No matching fields.</span>');
}
function setField(field,checked){
 if(checked&&!(field.index in st.extra))st.extra[field.index]=field.type==='number'?
  {type:'number',min:'',max:''}:{type:'text',choice:'',search:''};
 else if(!checked){
  delete st.extra[field.index];
  if(st.key==='raw:'+field.index){st.key='signal';st.dir=-1;}
 }
}
$('fieldToggle').onclick=()=>{
 const open=$('fieldPicker').hidden;
 $('fieldPicker').hidden=!open;$('fieldToggle').setAttribute('aria-expanded',String(open));
 if(open)$('fieldSearch').focus();
};
$('fieldClose').onclick=()=>{
 $('fieldPicker').hidden=true;$('fieldToggle').setAttribute('aria-expanded','false');
 $('fieldToggle').focus();
};
$('card').onkeydown=e=>{
 if(e.key==='Escape'&&!$('fieldPicker').hidden){
  $('fieldClose').onclick();
 }
};
$('fieldSearch').oninput=renderFieldList;
$('fieldList').onchange=e=>{
 const field=rawFields.find(f=>String(f.index)===e.target.dataset.field);if(!field)return;
 setField(field,e.target.checked);renderFieldList();renderFilters();st.page=1;render();
};
$('clearFilters').onclick=()=>{
 Object.keys(st.f).forEach(k=>st.f[k]='');st.ranges=Object.create(null);st.page=1;
 for(const filter of Object.values(st.extra)){
  if(filter.type==='number'){filter.min='';filter.max='';}
  else{filter.choice='';filter.search='';}
 }
 renderFieldList();renderFilters();render();
};
$('refresh').onclick=()=>setTriggerValue('refresh',Date.now());
$('addToCart').onclick=()=>{
 const selected=[...st.selected].filter(i=>DATA[i]);
 if(!selected.length){$('cartWarning').hidden=false;$('cartWarningClose').focus();return;}
 $('cartWarning').hidden=true;
 setTriggerValue('add_to_cart',selected);
};
$('cartWarningClose').onclick=()=>{
 $('cartWarning').hidden=true;$('addToCart').focus();
};
function renderFilters(){
 const open=new Set([...$('filt').querySelectorAll('details[open]')].map(el=>el.dataset.filterKey));
 $('filt').innerHTML='<td></td><td></td>'+COLS.map(([k,label,numeric])=>{
  if(CFG.showColumnFilters===false)return '<td></td>';
  const control=CELL[k]||(numeric?`<div class="rng"><input type="number" step="any" data-range="${esc(k)}" data-bound="min" placeholder="Min" aria-label="${esc(label)} minimum"><input type="number" step="any" data-range="${esc(k)}" data-bound="max" placeholder="Max" aria-label="${esc(label)} maximum"></div>`:'');
  return `<td>${control?`<details data-filter-key="primary:${esc(k)}"${open.has('primary:'+k)?' open':''}><summary aria-label="Filter ${esc(label)}">Filter</summary>${control}</details>`:''}</td>`;
 }).join('')+selectedFields().map(field=>`<td><details data-filter-key="raw:${field.index}"${open.has('raw:'+field.index)?' open':''}><summary aria-label="Filter ${esc(field.label)}">Filter</summary>${extraControl(field)}</details></td>`).join('');
 $('filt').querySelectorAll('[data-f]').forEach(el=>el.value=st.f[el.dataset.f]||'');
 $('filt').querySelectorAll('[data-range]').forEach(el=>el.value=(st.ranges[el.dataset.range]||{})[el.dataset.bound]||'');
}
 $('filt').oninput=$('filt').onchange=e=>{
  const {f,range,bound,extra}=e.target.dataset;
  if(f)st.f[f]=e.target.value;
  else if(range&&(bound==='min'||bound==='max')){if(!st.ranges[range])st.ranges[range]={min:'',max:''};st.ranges[range][bound]=e.target.value;}
  else if(extra!==undefined&&st.extra[extra]&&['min','max','choice','search'].includes(bound))st.extra[extra][bound]=e.target.value;
  else return;
  st.page=1;render();
 };
renderFieldList();renderFilters();
$('head').onclick=e=>{const remove=e.target.closest('[data-remove-field]');
 if(remove){const field=rawFields.find(f=>String(f.index)===remove.dataset.removeField);
  if(field){setField(field,false);st.page=1;renderFieldList();renderFilters();render();}return;}
 if(e.target.closest('.i'))return;const th=e.target.closest('th[data-k]');if(!th||th.classList.contains('nosort'))return;
 const k=th.dataset.k;if(st.key===k)st.dir*=-1;else{st.key=k;st.dir=k==='stock'?1:-1;}render();};
$('head').onchange=e=>{
 if(e.target.id!=='selectAll')return;
 const indexes=new Map(DATA.map((row,index)=>[row,index]));
 for(const row of VIEW.rows){
  const index=indexes.get(row);
  e.target.checked?st.selected.add(index):st.selected.delete(index);
 }
 render();
};
$('body').onclick=e=>{
 if(e.target.closest('[data-row-select]'))return;
 const summary=e.target.closest('summary');
 if(summary&&summary.parentElement?.dataset.panel){
  const d=summary.parentElement,k=d.dataset.panel;
  d.open?st.panelOpen.delete(k):st.panelOpen.add(k);
  return;
 }
 const tr=e.target.closest('tr.row');if(!tr)return;const t=tr.dataset.t;
 st.open.has(t)?st.open.delete(t):st.open.add(t);render();};
$('body').onchange=e=>{
 const input=e.target.closest('[data-row-select]');if(!input)return;
 const index=Number(input.dataset.rowSelect);
 if(!Number.isInteger(index)||!DATA[index])return;
 input.checked?st.selected.add(index):st.selected.delete(index);render();
};
$('first').onclick=()=>{st.page=1;render();};
$('prev').onclick=()=>{st.page--;render();};
$('next').onclick=()=>{st.page++;render();};
$('last').onclick=()=>{st.page=1e9;render();};
$('page').onchange=e=>{st.page=parseInt(e.target.value)||1;render();};
$('size').onchange=e=>{st.size=parseInt(e.target.value)||10;st.page=1;render();};
$('csv').onclick=()=>{
 const blob=new Blob(['\ufeff'+tableText(VIEW.rows,',')],{type:'text/csv;charset=utf-8'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='gghp_scan_results.csv';
 document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(a.href);toast(`Downloaded ${VIEW.rows.length} rows`);};
$('copy').onclick=()=>{
 const text=tableText(VIEW.slice,'\t');
 const done=()=>toast(`Copied ${VIEW.slice.length} rows`);
 const fallback=()=>{const ta=document.createElement('textarea');ta.value=text;
  document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();done();};
 (navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(text).then(done,fallback):fallback();};
$('refresh').hidden=!inputData.canRefresh;
render();
return {
 dispose(){if(toastTimer){clearTimeout(toastTimer);toastTimer=null;}},
 update(next){
  const choices={};
  const previousLabels=(CFG.rawCols||[]).slice();
  for(const [index,filter] of Object.entries(st.extra)){
   if(filter.type==='text'&&filter.choice!==''){
    const field=rawFields.find(f=>f.index===Number(index));
    choices[index]=field?.values[Number(filter.choice)];
   }
  }
  DATA=next.payload;CFG=next.config;st.selected.clear();$('cartWarning').hidden=true;buildColumns();buildFields();
  CELL.signal='<select data-f="sig" aria-label="Signal">'+opt('','All')+CFG.signals.map(s=>opt(s,s)).join('')+'</select>';
  for(const index of Object.keys(st.extra)){
   const field=rawFields.find(f=>f.index===Number(index));
   if(!field||field.type!==st.extra[index].type||previousLabels[index]!==CFG.rawCols[index])delete st.extra[index];
   else if(index in choices){
    const choice=field.values.indexOf(choices[index]);
    // Keep an absent exact value as an active filter rather than silently broadening.
    if(choice<0){field.values.push(choices[index]);st.extra[index].choice=String(field.values.length-1);}
    else st.extra[index].choice=String(choice);
   }
  }
  if(st.key.startsWith('raw:')&&!(st.key.slice(4) in st.extra)){st.key='signal';st.dir=-1;}
  if(!st.key.startsWith('raw:')&&!COLS.some(c=>c[0]===st.key)){st.key=COLS[0]?.[0]||'signal';st.dir=1;}
  $('refresh').hidden=!next.canRefresh;
  renderFieldList();renderFilters();render();
 }
};
</script></body></html>
"""


_CSS = "\n" + _TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
_HTML = "\n" + _TEMPLATE.split("<body>", 1)[1].split("<script>", 1)[0]
_TABLE_JS = _TEMPLATE.split("<script>", 1)[1].split("</script>", 1)[0]
_COMPONENT_JS = """
function initTable(parentElement, inputData, setTriggerValue) {
""" + _TABLE_JS + """
}
export default function(component) {
 const {parentElement, data, setTriggerValue} = component;
 let instance = parentElement.__scanTable;
 if (instance && instance.card !== parentElement.querySelector('#card')) {
  instance.dispose();
  instance = null;
 }
 if (!instance) {
  const trigger = {current:setTriggerValue};
  instance = initTable(parentElement, data, (...args)=>trigger.current(...args));
  instance.trigger = trigger;
  instance.card = parentElement.querySelector('#card');
  parentElement.__scanTable = instance;
  instance.signature = JSON.stringify(data);
 } else {
  instance.trigger.current = setTriggerValue;
  const signature = JSON.stringify(data);
  if (signature !== instance.signature) {
   instance.update(data);
   instance.signature = signature;
  }
 }
 return () => instance.dispose();
}
"""

_COMPONENT = st.components.v2.component(
    "equities_scan_table", html=_HTML, css=_CSS, js=_COMPONENT_JS
)

_DETAIL_JS = """
function initDetail(parentElement, inputData) {
""" + _TABLE_JS.split("const VIEW={rows:[],slice:[]};", 1)[0] + """
 const target=$('cartDetail');
 function renderDetail(){target.innerHTML=detail(DATA[0]);}
 renderDetail();
 return {
  update(next){
   st.panelOpen=new Set([...target.querySelectorAll('details.group[open]')].map(el=>el.dataset.panel));
   DATA=next.payload;CFG=next.config;buildColumns();buildFields();renderDetail();
  }
 };
}
export default function(component) {
 const {parentElement, data}=component;
 let instance=parentElement.__cartStockDetail;
 if(instance && instance.card!==parentElement.querySelector('#card')) instance=null;
 const signature=JSON.stringify(data);
 if(!instance){
  instance=initDetail(parentElement,data);
  instance.card=parentElement.querySelector('#card');
  parentElement.__cartStockDetail=instance;
 } else if(signature!==instance.signature) instance.update(data);
 instance.signature=signature;
}
"""

_DETAIL_COMPONENT = st.components.v2.component(
    "cart_stock_details",
    html='\n<div class="card" id="card"><div class="scroll"><div id="cartDetail"></div></div></div>\n',
    css=_CSS,
    js=_DETAIL_JS,
)


def _add_rows_to_cart(
    payload: list[dict[str, object]], indexes: object, config: dict[str, object] | None = None,
) -> tuple[int, int]:
    """Stage rows and refresh their detail snapshots without overwriting quantities."""
    if not isinstance(indexes, list):
        return 0, 0
    cart = st.session_state.setdefault("stock_cart", [])
    existing = {item["id"]: item for item in cart}
    processed: set[str] = set()
    added = skipped = 0
    for index in indexes:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(payload):
            continue
        row = payload[index]
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        identity = ticker.upper()
        if identity in processed:
            skipped += 1
            continue
        processed.add(identity)
        try:
            holding_quantity = float(row["quantity"]) if row.get("quantity") is not None else None
        except (TypeError, ValueError, OverflowError):
            holding_quantity = None
        if holding_quantity is not None and (not math.isfinite(holding_quantity) or holding_quantity <= 0):
            holding_quantity = None
        signal = str(row.get("signal") or "NOT RATED")
        snapshot = {"row": deepcopy(row), "config": deepcopy(config or {})}
        if identity in existing:
            item = existing[identity]
            item["signal"] = signal
            item["name"] = str(row.get("label") or ticker)
            item["stock_detail"] = snapshot
            if holding_quantity is not None:
                item["holding_quantity"] = holding_quantity
            skipped += 1
            continue
        quantity = holding_quantity or 1.0
        if signal.upper() in {"SELL", "STRONG SELL"}:
            quantity = -quantity
        cart.append({
            "id": identity,
            "ticker": ticker,
            "name": str(row.get("label") or ticker),
            "signal": signal,
            "quantity": quantity,
            "holding_quantity": holding_quantity,
            "stock_detail": snapshot,
        })
        existing[identity] = cart[-1]
        added += 1
    if added or skipped:
        st.session_state["stock_cart_review"] = None
    return added, skipped


def render_cart_stock_details(item: dict[str, object]) -> None:
    """Render the saved table detail for one cart item, or explain a missing snapshot."""
    snapshot = item.get("stock_detail")
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("row"), dict)
            or not isinstance(snapshot.get("config"), dict)):
        st.info("Stock details are unavailable. Re-add this stock from a research table.")
        return
    identity = str(item.get("id") or item.get("ticker") or "")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    _DETAIL_COMPONENT(
        key=f"cart-stock-detail-{digest}",
        data={"payload": [snapshot["row"]], "config": snapshot["config"]},
    )


def render_scan_table(payload: list[dict[str, object]], config: dict[str, object]) -> str:
    """Return a standalone document for tests and non-Streamlit previews."""
    data = json.dumps(
        {"payload": payload, "config": config, "canRefresh": False},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    js = "const inputData=" + data + ";\nconst parentElement=document;\n" \
         "const setTriggerValue=()=>{};\n" + _TABLE_JS.replace(
             "return {\n dispose()", "const standaloneInstance = {\n dispose()", 1
         )
    return "<!doctype html><html><head><meta charset=\"utf-8\"><style>" + _CSS + \
        "</style></head><body>" + _HTML + "<script>" + js + "</script></body></html>"


def show_scan_table(
    payload: list[dict[str, object]],
    config: dict[str, object],
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Mount the table; tableKey must uniquely identify each mounted instance."""
    key = str(config.get("tableKey", "equities-scan-table"))

    def add_to_cart() -> None:
        indexes = st.session_state[key].get("add_to_cart")
        added, skipped = _add_rows_to_cart(payload, indexes, config)
        if added:
            st.toast(f"Added {added} stock{'s' if added != 1 else ''} to Cart.")
        if skipped:
            st.toast(f"{skipped} already in Cart; quantities were kept and details refreshed.")
        if not added and not skipped:
            st.warning("No valid stocks could be added to Cart.")

    _COMPONENT(
        key=key,
        data={"payload": payload, "config": config, "canRefresh": on_refresh is not None},
        on_refresh_change=on_refresh or (lambda: None),
        on_add_to_cart_change=add_to_cart,
    )
