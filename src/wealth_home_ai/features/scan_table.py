"""Self-contained interactive results table for the stock scanner.

The table renders inside an iframe so it can sort, filter, paginate, expand
rows and copy data entirely in the browser without re-running the Streamlit
script on every click.
"""

from __future__ import annotations

import json

import streamlit as st

TABLE_HEIGHT = 640

_TEMPLATE = r"""
<!doctype html><html><head><meta charset="utf-8"><style>
:root{--pos:#22C55E;--neg:#F87171;--gold:#D4AF37;--fg:#E5E7EB;--bg:transparent;
 --soft:rgba(128,128,128,.10);--line:rgba(128,128,128,.28);--muted:rgba(128,128,128,.95);}
*{box-sizing:border-box;}
html,body{margin:0;overflow:hidden;background:var(--bg);color:var(--fg);
 font:14px/1.45 "Source Sans Pro",system-ui,-apple-system,"Segoe UI",sans-serif;}
.card{display:flex;flex-direction:column;border:2px solid var(--gold);
 border-radius:14px;overflow:hidden;}
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
.scroll{overflow-x:auto;}
table{width:100%;border-collapse:separate;border-spacing:0;}
th{background:var(--bg);padding:.55rem .8rem;text-align:left;
 font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);
 border-bottom:2px solid var(--gold);cursor:pointer;white-space:nowrap;user-select:none;
 }
.filt td{padding:.4rem .5rem;background:rgba(201,162,39,.08);border-bottom:2px solid var(--gold);}
.filt input,.filt select{width:100%;min-width:0;padding:.25rem .4rem;border-radius:6px;border:1px solid var(--line);background:var(--soft);color:var(--fg);font:inherit;font-size:.82rem;}
.filt select option{color:#111;}
.filt .rng{display:flex;gap:.25rem;}
.filt .rng input{width:50%;}
th.nosort{cursor:default;}
th .arrow{display:inline-block;width:1em;}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums;}
td{padding:.55rem .8rem;border-bottom:1px solid var(--line);vertical-align:middle;}
tr.row{cursor:pointer;}
tr.row:hover td{background:rgba(201,162,39,.12);}
tr.row.open td{background:rgba(201,162,39,.18);}
.name{font-weight:600;}
.name small{display:block;color:var(--muted);font-weight:400;font-size:.76rem;}
.caret{display:inline-block;width:1em;color:var(--gold);transition:transform .15s;}
tr.open .caret{transform:rotate(90deg);}
.pill{display:inline-block;padding:.12rem .65rem;border-radius:999px;font-weight:700;font-size:.8rem;}
.pill.SB{background:#16A34A;color:#fff;}
.pill.B{background:rgba(34,197,94,.22);color:var(--pos);}
.pill.H{background:rgba(234,179,8,.22);color:#EAB308;}
.pill.S{background:rgba(239,68,68,.22);color:var(--neg);}
.pos{color:var(--pos);font-weight:600;}.neg{color:var(--neg);font-weight:600;}.neu{color:var(--fg);}
.meter{display:flex;align-items:center;gap:.4rem;min-width:7rem;}
.meter i{flex:1;height:7px;border-radius:999px;background:var(--line);overflow:hidden;}
.meter i b{display:block;height:100%;background:linear-gradient(90deg,#E6B800,#22C55E);}
.tag{display:inline-block;margin:.1rem .25rem .1rem 0;padding:.05rem .5rem;border-radius:999px;
 font-size:.76rem;background:rgba(34,197,94,.14);border:1px solid rgba(34,197,94,.5);}
tr.detail td{padding:0;background:var(--soft);}
.panel{padding:1rem 1.2rem 1.2rem;animation:pop .18s ease-out;}
@keyframes pop{from{opacity:0;transform:translateY(-4px);}to{opacity:1;transform:none;}}
.verdict{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap;margin-bottom:.8rem;
 font-size:.95rem;}
.groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.8rem;}
.group{border:1px solid var(--line);border-radius:10px;padding:.6rem .8rem;background:var(--bg);}
.group h4{margin:0 0 .4rem;font-size:.74rem;letter-spacing:.08em;text-transform:uppercase;
 color:var(--gold);}
.kv{display:flex;justify-content:space-between;gap:.6rem;padding:.12rem 0;font-size:.85rem;}
.kv span:first-child{color:var(--muted);}
.i{display:inline-flex;align-items:center;justify-content:center;width:.95rem;height:.95rem;margin-left:.3rem;border:1px solid var(--gold);border-radius:50%;color:var(--gold);font-size:.62rem;font-weight:700;font-style:normal;text-decoration:none;line-height:1;vertical-align:middle;}
.i:hover{background:var(--gold);color:#111;}
.kv span:last-child{font-weight:600;font-variant-numeric:tabular-nums;}
.checks{margin-top:.8rem;}
.checks h4{margin:0 0 .4rem;font-size:.74rem;letter-spacing:.08em;text-transform:uppercase;
 color:var(--gold);}
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
.toast{position:fixed;right:1rem;bottom:4.2rem;background:var(--gold);color:#111;
 padding:.35rem .8rem;border-radius:8px;font-weight:600;opacity:0;transition:opacity .2s;}
.toast.show{opacity:1;}
@media (prefers-reduced-motion:reduce){.panel{animation:none;}}
</style></head><body>
<div class="card" id="card">
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
const DATA=__DATA__, CFG=__CONFIG__;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
(function theme(){
 try{
  const el=parent.document.querySelector('.stApp')||parent.document.body;
  const cs=parent.getComputedStyle(el);
  const m=cs.backgroundColor.match(/\d+/g)||[0,0,0];
  const lum=(.299*m[0]+.587*m[1]+.114*m[2]);
  const r=document.documentElement.style;
  r.setProperty('--fg',cs.color);
  if(lum>140){r.setProperty('--pos','#15803D');r.setProperty('--neg','#DC2626');
   r.setProperty('--gold','#B8860B');}
 }catch(e){}
})();
const RANK={'STRONG BUY':3,'BUY':2,'HOLD':1,'SELL':0};
const PILL={'STRONG BUY':'SB','BUY':'B','HOLD':'H','SELL':'S'};
const st={f:{q:'',pmin:'',pmax:'',chg:'',rmin:'',rmax:'',sig:'',m1:'',vol:'',str:'',pass:''},key:'signal',dir:-1,page:1,size:10,open:new Set()};
const COLS=[['stock','Stock',0],['price','Price',1],['chg','Day change',1],['m1','1 month',1],['rsi','RSI 14',1],
 ['vol','Volume',1],['signal','Signal',0],['strength','Strength',0]];
if(CFG.showFilters)COLS.push(['pass','Filters',0]);
const sortVal={
 stock:r=>r.label.toLowerCase(),price:r=>r.price,chg:r=>r.chg,m1:r=>r.m1,vol:r=>r.vol,rsi:r=>r.rsi,
 signal:r=>RANK[r.signal]*1000+r.score,strength:r=>r.total?r.pos/r.total:0,
 pass:r=>r.pass?1:0};
const tone=(v,pos)=>v==null?'neu':(pos?'pos':'neg');

function filtered(){
 const f=st.f,q=f.q.trim().toLowerCase();
 const n=v=>v===''?null:parseFloat(v);
 const pmin=n(f.pmin),pmax=n(f.pmax),rmin=n(f.rmin),rmax=n(f.rmax),smin=n(f.str),m1min=n(f.m1),vmin=n(f.vol);
 let rows=DATA.filter(r=>
  (!q||r.ticker.toLowerCase().includes(q)||r.label.toLowerCase().includes(q))&&
  (m1min==null||r.m1>=m1min)&&(vmin==null||r.vol>=vmin)&&(pmin==null||r.price>=pmin)&&(pmax==null||r.price<=pmax)&&
  (!f.chg||(f.chg==='up'?r.chg>=0:r.chg<0))&&
  (rmin==null||r.rsi>=rmin)&&(rmax==null||r.rsi<=rmax)&&
  (!f.sig||r.signal===f.sig)&&
  (smin==null||(r.total&&r.pos/r.total*100>=smin))&&
  (!f.pass||(f.pass==='y')===r.pass));
 const g=sortVal[st.key];
 rows.sort((a,b)=>{const x=g(a),y=g(b);
  const c=x<y?-1:x>y?1:0;return (c||a.ticker.localeCompare(b.ticker))*st.dir;});
 return rows;
}
const info=k=>{const i=CFG.info[k];return i?`<a class="i" href="${esc(i[1])}" target="_blank" rel="noopener noreferrer" title="${esc(i[0])} Click to learn more.">i</a>`:'';};
function detail(r){
 const groups=CFG.groups.map(([title,items])=>{
  const kv=items.filter(([k])=>r.ind[k]).map(([k,l])=>
   `<div class="kv"><span>${esc(l)}${info(k)}</span><span class="${r.ind[k][1]}">${esc(r.ind[k][0])}</span></div>`).join('');
  return kv?`<div class="group"><h4>${esc(title)}</h4>${kv}</div>`:'';
 }).join('');
 const checks=r.checks.map(([n,v,s])=>
  `<span class="ck ${s==='p'?'p':s==='n'?'n':''}" title="${esc(v)}">${s==='p'?'&#10003;':s==='n'?'&#10007;':'&#8211;'} ${esc(n)}${info(CFG.checkInfo[n])}</span>`).join('');
 return `<div class="panel"><div class="verdict"><span class="pill ${PILL[r.signal]}">${r.signal}</span>
  <span>${esc(r.reason)}</span></div><div class="groups">${groups}</div>
  <div class="checks"><h4>Signal checks &middot; ${r.pos} positive of ${r.total}</h4>${checks}</div></div>`;
}
const VIEW={rows:[],slice:[]};
function render(){
 const rows=filtered(), size=Math.max(1,Math.min(500,st.size|0||10));
 const pages=Math.max(1,Math.ceil(rows.length/size));
 st.page=Math.min(Math.max(1,st.page|0||1),pages);
 const start=(st.page-1)*size, slice=rows.slice(start,start+size);
 $('head').innerHTML='<th class="nosort"></th>'+COLS.map(([k,l,n])=>
  `<th data-k="${k}" class="${n?'num':''} ${k==='pass'?'nosort':''}">${l}<span class="arrow">${st.key===k?(st.dir>0?'&#9650;':'&#9660;'):''}</span></th>`).join('');
 $('body').innerHTML=slice.length?slice.map(r=>{
  const open=st.open.has(r.ticker);
  const main=`<tr class="row ${open?'open':''}" data-t="${esc(r.ticker)}">
   <td><span class="caret">&#9656;</span></td>
   <td class="name">${esc(r.label)}</td>
   <td class="num">&#8377;${r.price.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
   <td class="num ${r.chg>=0?'pos':'neg'}">${r.chg>=0?'+':''}${r.chg.toFixed(2)}%</td>
   <td class="num ${r.m1>=0?'pos':'neg'}">${r.m1>=0?'+':''}${r.m1.toFixed(1)}%</td>
   <td class="num ${r.rsiTone}">${r.rsi.toFixed(1)}</td>
   <td class="num ${r.vol>=1?'pos':'neu'}">${r.vol.toFixed(2)}x</td>
   <td><span class="pill ${PILL[r.signal]}">${r.signal}</span></td>
   <td><div class="meter"><i><b style="width:${r.total?Math.round(r.pos/r.total*100):0}%"></b></i><span>${r.pos}/${r.total}</span></div></td>
   ${CFG.showFilters?`<td class="${r.pass?'pos':'neg'}">${r.pass?'&#10003;':'&#10007;'}</td>`:''}</tr>`;
  return main+(open?`<tr class="detail"><td colspan="${COLS.length+1}">${detail(r)}</td></tr>`:'');
 }).join(''):`<tr><td colspan="${COLS.length+1}" class="empty">No stocks match these filters.</td></tr>`;
 $('count').textContent=rows.length?`Showing ${(start+1).toLocaleString()}\u2013${(start+slice.length).toLocaleString()} of ${rows.length.toLocaleString()}`:'0 results';
 $('pages').textContent=pages.toLocaleString();
 $('page').value=st.page;$('page').max=pages;$('size').value=size;
 $('first').disabled=$('prev').disabled=st.page<=1;
 $('next').disabled=$('last').disabled=st.page>=pages;
 VIEW.rows=rows;VIEW.slice=slice;
}
function tableText(rows,sep){
 const cell=v=>{v=v==null?'':String(v);return sep===','&&/[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;};
 const head=['Stock','Ticker','Signal','Signal score','Positive checks','Total checks',...CFG.rawCols,'Reason','Passes filters'];
 const lines=[head.map(cell).join(sep)];
 rows.forEach(r=>lines.push([r.label,r.ticker,r.signal,r.score,r.pos,r.total,...r.raw,r.reason,r.pass?'Yes':'No'].map(cell).join(sep)));
 return lines.join('\n');
}
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800);}
const opt=(v,l)=>`<option value="${v}">${l}</option>`;
const CELL={
 stock:'<input data-f="q" type="search" placeholder="Search symbol or company">',
 price:'<div class="rng"><input data-f="pmin" type="number" placeholder="Min"><input data-f="pmax" type="number" placeholder="Max"></div>',
 chg:'<select data-f="chg">'+opt('','All')+opt('up','Gainers')+opt('down','Losers')+'</select>',
 rsi:'<div class="rng"><input data-f="rmin" type="number" placeholder="Min"><input data-f="rmax" type="number" placeholder="Max"></div>',
 signal:'<select data-f="sig">'+opt('','All')+CFG.signals.map(s=>opt(s,s)).join('')+'</select>',
 strength:'<select data-f="str">'+opt('','Any')+opt('25','25%+')+opt('50','50%+')+opt('75','75%+')+'</select>',
 m1:'<select data-f="m1">'+opt('','All')+opt('0','Positive')+opt('10','10%+')+'</select>',
 vol:'<select data-f="vol">'+opt('','All')+opt('1','1x+')+opt('2','2x+')+'</select>',
 pass:'<select data-f="pass">'+opt('','All')+opt('y','Pass')+opt('n','Fail')+'</select>'};
$('filt').innerHTML='<td></td>'+COLS.map(([k])=>`<td>${CELL[k]}</td>`).join('');
$('filt').oninput=$('filt').onchange=e=>{const k=e.target.dataset.f;if(!k)return;st.f[k]=e.target.value;st.page=1;render();};
$('head').onclick=e=>{const th=e.target.closest('th[data-k]');if(!th||th.classList.contains('nosort'))return;
 const k=th.dataset.k;if(st.key===k)st.dir*=-1;else{st.key=k;st.dir=k==='stock'?1:-1;}render();};
$('body').onclick=e=>{const tr=e.target.closest('tr.row');if(!tr)return;const t=tr.dataset.t;
 st.open.has(t)?st.open.delete(t):st.open.add(t);render();};
$('first').onclick=()=>{st.page=1;render();};
$('prev').onclick=()=>{st.page--;render();};
$('next').onclick=()=>{st.page++;render();};
$('last').onclick=()=>{st.page=1e9;render();};
$('page').onchange=e=>{st.page=parseInt(e.target.value)||1;render();};
$('size').onchange=e=>{st.size=parseInt(e.target.value)||10;st.page=1;render();};
$('csv').onclick=()=>{
 const blob=new Blob(['\ufeff'+tableText(VIEW.rows,',')],{type:'text/csv;charset=utf-8'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='gghp_scan_results.csv';
 document.body.appendChild(a);a.click();a.remove();toast(`Downloaded ${VIEW.rows.length} rows`);};
$('copy').onclick=()=>{
 const text=tableText(VIEW.slice,'\t');
 const done=()=>toast(`Copied ${VIEW.slice.length} rows`);
 const fallback=()=>{const ta=document.createElement('textarea');ta.value=text;
  document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();done();};
 (navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(text).then(done,fallback):fallback();};
function fit(){
 try{
  const h=Math.ceil($('card').getBoundingClientRect().height)+6;
  const f=window.frameElement;
  if(f&&Math.abs(f.offsetHeight-h)>1){f.style.height=h+'px';f.setAttribute('height',h);}
 }catch(e){}
}
const _render=render;
render=function(){_render();fit();};
new ResizeObserver(fit).observe($('card'));
window.addEventListener('load',fit);
render();
</script></body></html>
"""


def render_scan_table(payload: list[dict[str, object]], config: dict[str, object]) -> str:
    """Return the standalone HTML document for the interactive results table."""

    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")

    return _TEMPLATE.replace("__DATA__", dump(payload)).replace(
        "__CONFIG__", dump(config)
    )


def show_scan_table(payload: list[dict[str, object]], config: dict[str, object]) -> None:
    st.iframe(render_scan_table(payload, config), height="content")
