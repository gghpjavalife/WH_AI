"""Compact cart row component."""

import streamlit as st

ROW_HTML = """
<div class="row">
 <div class="stock"><span id="name"></span><span id="signal"></span></div>
 <div class="quantities">
  <div class="stepper" id="qtyBox">
   <input id="qty" type="number" step="1" aria-label="Quantity">
   <span class="arrows"><button id="qtyUp" aria-label="Increase quantity">▴</button><button id="qtyDown" aria-label="Decrease quantity">▾</button></span>
  </div>
  <div class="stepper percentage" id="pctBox">
   <input id="pct" readonly aria-label="Percentage of holding">
   <span class="arrows"><button id="pctUp" aria-label="Increase sell quantity">▴</button><button id="pctDown" aria-label="Decrease sell quantity">▾</button></span>
  </div>
 </div>
 <span id="estimate" class="estimate"></span>
 <div class="actions">
  <button id="view" class="action view" title="View stock details" aria-label="View stock details"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button>
  <button id="review" class="action review" title="Review stock (no order is placed)" aria-label="Review stock"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5z M8 12l2.5 2.5L16 9"/></svg></button>
  <button id="remove" class="action remove" title="Remove from cart" aria-label="Remove from cart"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16 M9 7V4h6v3 M7 7l1 13h8l1-13 M10 10v7 M14 10v7"/></svg></button>
 </div>
</div><div id="error" class="error" role="alert" hidden></div>
"""

ROW_CSS = """
:host{font-family:var(--st-font,sans-serif);color:var(--st-text-color);}
*{box-sizing:border-box}.row{display:flex;align-items:center;gap:.65rem;min-width:0;width:100%;
 padding:.55rem .7rem;border:1px solid var(--st-border-color,#ad9137);border-radius:9px}
.stock{position:relative;flex:1 1 10rem;min-width:0;max-width:230px;padding-top:.45rem}
#name{font-weight:700;overflow:hidden;text-overflow:ellipsis;display:block}
#signal{position:absolute;top:-.25rem;right:0;font-size:.62rem;font-weight:700;
 padding:.08rem .3rem;border-radius:4px;background:var(--st-secondary-background-color,#1d2938)}
#signal.buy{color:#22a85a}#signal.sell{color:#ef5555}
.quantities{display:flex;align-items:center;gap:.4rem;flex:none}
.stepper{display:flex;align-items:stretch;width:95px;height:40px;border:1px solid #888;border-radius:7px;overflow:hidden;flex-shrink:0}
.stepper.buy{border-color:#22a85a;background:rgba(34,168,90,.10)}
.stepper.sell{border-color:#ef5555;background:rgba(239,85,85,.10)}
.stepper input{width:100%;min-width:0;background:transparent;color:inherit;border:0;padding:0 .4rem;font:inherit;outline:none}
.stepper input::-webkit-inner-spin-button,.stepper input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.stepper input[type=number]{-moz-appearance:textfield;appearance:textfield}
.arrows{display:flex;flex-direction:column;border-left:1px solid #8885}
.arrows button{width:28px;min-height:19px;flex:1;background:transparent;color:inherit;border:0;cursor:pointer;line-height:1}
.arrows button:hover:not(:disabled){background:#8883}.arrows button:disabled{opacity:.3;cursor:not-allowed}
.percentage{width:100px}.percentage[hidden],[hidden]{display:none!important}
.estimate{font-size:.82rem;flex:1 1 10rem;min-width:0;overflow-wrap:anywhere}
.actions{display:flex;gap:.35rem;flex:none}.action{width:40px;height:40px;border-radius:7px;background:transparent;cursor:pointer;font-size:1rem;touch-action:manipulation}
.action:disabled{opacity:.35;cursor:not-allowed}.view{color:#53aaff;border:1px solid #53aaff}
.review{color:#2dc979;border:1px solid #2dc979}.remove{color:#f06d6d;border:1px solid #f06d6d}
.action svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;vertical-align:middle}
.action:hover:not(:disabled){background:#8883}.error{color:#f06d6d;font-size:.8rem;margin:.25rem}
@media(max-width:760px){
 .row{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"stock actions" "quantities quantities" "estimate estimate";gap:.55rem .7rem;padding:.65rem}
 .stock{grid-area:stock;max-width:none;min-height:2rem}
 .quantities{grid-area:quantities;width:100%;flex-wrap:wrap}
 .stepper{width:min(9rem,100%);height:44px}
 .percentage{width:min(7rem,100%)}
 .estimate{grid-area:estimate;width:100%;font-size:.86rem;line-height:1.4}
 .actions{grid-area:actions;gap:.35rem}
 .action{width:42px;height:42px}
 .arrows button{min-height:21px}
}
@media(max-width:380px){
 .row{gap:.5rem;padding:.55rem}
 .actions{gap:.2rem}
 .action{width:38px;height:40px}
 .stepper{width:min(8rem,100%)}
}
"""

ROW_JS = """
export default function({parentElement,data,setTriggerValue}) {
 const $=id=>parentElement.querySelector('#'+id);
 const quantity=Number(data.quantity), held=Number(data.held)||0;
 const min=held>0?-Math.floor(held):null;
 const qty=$('qty'), error=$('error');
 $('name').textContent=data.name;
 $('name').title=data.name;
 $('signal').textContent=data.signal;
 $('signal').className=data.signal.includes('BUY')?'buy':data.signal.includes('SELL')?'sell':'';
 $('qtyBox').className='stepper '+(quantity>0?'buy':quantity<0?'sell':'');
 qty.value=String(quantity);
 qty.title=held>0?'Whole shares; selling is limited to the recorded holding.':'Whole shares; holding is unknown, so a sell limit cannot be checked.';
 if(min!==null)qty.min=String(min);else qty.removeAttribute('min');
 const below= min!==null && quantity<min;
 const down=Math.ceil(quantity)-1, up=below?min:Math.floor(quantity)+1;
 $('qtyDown').disabled=min!==null&&down<min;
 $('qtyUp').disabled=false;
 $('pctBox').hidden=!(quantity<0&&held>0);
 $('pct').value=held>0?(Math.abs(quantity)/held*100).toFixed(2)+'%':'';
 $('pct').title='Read-only percentage of holding; arrows adjust the sell quantity by one share.';
 $('pctUp').disabled=min!==null&&down<min;
 $('pctDown').disabled=quantity>=0;
 $('estimate').textContent=data.estimate;
 $('estimate').title=data.estimateHelp;
 $('review').disabled=quantity===0||!Number.isInteger(quantity)||below;
 const fail=message=>{error.textContent=message;error.hidden=false;};
 const commit=value=>{
  if(!Number.isSafeInteger(value)){fail('Enter a whole number of shares.');qty.value=String(quantity);return;}
  if(min!==null&&value<min){fail('Cannot sell more than the held whole shares.');qty.value=String(quantity);return;}
  error.hidden=true;
  if(value!==quantity)setTriggerValue('event',{action:'quantity',quantity:value});
 };
 qty.onchange=()=>{if(qty.value.trim()===''){fail('Enter a whole number of shares.');qty.value=String(quantity);return;}commit(Number(qty.value));};
 qty.onkeydown=e=>{
  if(e.key==='ArrowUp'||e.key==='ArrowDown'){e.preventDefault();commit(e.key==='ArrowUp'?up:down);}
  if(e.key==='Enter'){e.preventDefault();qty.blur();}
 };
 $('qtyUp').onclick=()=>commit(up);
 $('qtyDown').onclick=()=>commit(down);
 $('pctUp').onclick=()=>commit(down);
 $('pctDown').onclick=()=>commit(Math.min(0,up));
 $('view').onclick=()=>setTriggerValue('event',{action:'view'});
 $('review').onclick=()=>setTriggerValue('event',{action:'review'});
 $('remove').onclick=()=>setTriggerValue('event',{action:'remove'});
}
"""

ROW_COMPONENT = st.components.v2.component(
    "stock_cart_row", html=ROW_HTML, css=ROW_CSS, js=ROW_JS,
)
