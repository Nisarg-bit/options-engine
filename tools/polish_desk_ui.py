"""One-time presentation refinement of the reviewed dashboard assets."""
from pathlib import Path
p=Path('firebase/public/desk.js')
s=p.read_text(encoding='utf-8')
s=s.replace('let data=null,shown=[];', 'let data=null,shown=[],lastPayload=null,updating=false;')
s=s.replace('async function update(){try{data=await DATA.daytrade();', "async function update(){if(updating)return;updating=true;try{const next=await DATA.daytrade();const signature=JSON.stringify([UND,next,SRC.live?.confirmation_forward]);if(signature===lastPayload)return;lastPayload=signature;data=next;")
s=s.replace("renderJournal();}}\n// Retain", "renderJournal();}finally{updating=false}}\n// Retain")
s=s.replace("}catch(err){data=null;", "}catch(err){lastPayload=null;data=null;")
s=s.replace('Confirmed breakout · long option</h2>', 'NIFTY confirmation · long option</h2>')
s=s.replace('<h2>Opening spread</h2>', '<h2>NIFTY opening spread</h2>')
s=s.replace("'<div class=\"desk-empty\">", "'<div class=\"desk-empty\">")
s=s.replace('Paper data unavailable: ${e(err.message)}', "${!SRC.live?.daytrade ? 'Connecting to paper journals…' : 'Paper data unavailable: '+e(err.message)}")
s=s.replace("let ready=false;", '')
s=s.replace('ready=true;clearInterval(startup);','clearInterval(startup);')
s=s.replace('<label>From<input', '<label>Search sessions<input type="search" id="desk-search" placeholder="Date, status or exit reason"></label><label>From<input')
s=s.replace(".filter(r=>(!q('#desk-from').value", ".filter(r=>(!q('#desk-search').value||[r.date,r.status,r.exit_reason].join(' ').toLowerCase().includes(q('#desk-search').value.toLowerCase()))&&(!q('#desk-from').value")
s=s.replace("['desk-strategy','desk-from'", "['desk-search','desk-strategy','desk-from'")
s=s.replace("q('#desk-from').value='';q('#desk-to')", "q('#desk-search').value='';q('#desk-from').value='';q('#desk-to')")
# Give touch and keyboard users the same exact point readout as hover users.
s=s.replace('<circle cx="${55+i*640/(points.length-1)}"', '<circle tabindex="0" data-point="${e(r.date)}: ${e(money(r[key]))}" aria-label="${e(r.date)}: ${e(money(r[key]))}" cx="${55+i*640/(points.length-1)}"')
s=s.replace("let data=null", "const pointReadout=document.createElement('p');pointReadout.className='desk-note';pointReadout.setAttribute('aria-live','polite');pointReadout.textContent='Tap or focus a chart point for its value.';perf.append(pointReadout);for(const event of ['click','focusin'])perf.addEventListener(event,ev=>{const point=ev.target.closest('[data-point]');if(point)pointReadout.textContent=point.dataset.point;});\nlet data=null")
# Keep the session choice responsive without clearing filters on unrelated updates.
s=s.replace("nav.querySelector('[data-t].on')", "q('#undsel').addEventListener('change',()=>{lastPayload=null;update()});\nnav.querySelector('[data-t].on')")
p.write_text(s,encoding='utf-8')
