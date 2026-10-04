"""Build the storyline page from the classic one.

Reads firebase/backup/index-classic.html, writes firebase/public/index.html.
Every cut is by exact text and asserted, so a classic page that has moved
underneath fails loudly instead of producing a half-built page.
"""
import os, re, sys
HOME = os.path.expanduser("~")
V2 = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(V2)
SRC = os.path.join(os.path.dirname(ROOT), "backup", "index-classic.html")
DST = os.path.join(ROOT, "public", "index.html")
V2 = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(V2)

s = open(SRC, encoding="utf-8").read()

def cut(text, start, end, inclusive=True):
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j + (len(end) if inclusive else 0)]

def once(text, old, new):
    assert text.count(old) == 1, (text.count(old), old[:80])
    return text.replace(old, new)

# ------------------------------------------------------------------ sections
def section(sid):
    return cut(s, f'<section id="{sid}"', "</section>")

def inner(sec):
    return sec[sec.index(">") + 1: sec.rindex("</section>")]

dash, signal = section("dash"), section("signal")
intraday, journal = section("intraday"), section("journal")
# The v3 Strategy builder replaces the classic one wholesale -- see v3.js.
BUILDER = '''<section id="builder">
    <div id="sb">
      <div class="chead"><span class="cnum">&#9881;</span><div><h1>Strategy builder</h1>
        <p>Pick a strategy by market view, adjust any leg, and read the payoff before expiry and at it.</p></div></div>
      <div class="card wide" id="sbstatus"><div class="empty">loading the option chain&hellip;</div></div>
      <div class="card wide" style="margin-top:16px"><div id="sbtiles"></div></div>
      <div class="card wide" style="margin-top:16px"><div id="sblegs"></div></div>
      <div id="sbout" style="margin-top:16px"></div>
      <div class="card wide" style="margin-top:16px"><h2>Compare two positions</h2><div id="sbcmp"></div></div>
      <div class="card wide" style="margin-top:16px"><div id="sbchain"></div></div>
    </div>
  </section>'''
tools = "\n".join([BUILDER] + [section(x) for x in ("chain", "oi", "structure", "health")])

world_div = '<div class="card wide" style="margin-top:16px" id="world"></div>'
gates_div = '<div class="card wide" style="margin-top:16px" id="gates"></div>'
dashcards = '<div class="grid g3" style="margin-top:16px" id="dashcards"></div>'
for x in (world_div, gates_div, dashcards):
    assert x in dash
term_grid = cut(dash, '<div class="grid g2" style="margin-top:16px">\n      <div class="card">\n        <h2>Implied volatility',
                '<div id="termnote"></div>\n      </div>\n    </div>')
sizing_card = cut(dash, '<div class="card">\n        <h2>Position sizing', '</p>\n      </div>')
scanner_card = cut(dash, '<div class="card">\n        <h2><button class="csv" data-csv="scan"', '</p>\n      </div>')
failed_card = cut(dash, '<div class="card wide" style="margin-top:16px;border-color:rgba(255,92,124,.35)">',
                  '</p>\n    </div>\n  </section>')[:-len("\n  </section>")]
sigcards = '<div class="grid g2" id="sigcards"></div>'
sigrec = '<div class="grid g2" style="margin-top:16px" id="sigrec"></div>'
assert sigcards in signal and sigrec in signal
payoff_card = cut(signal, '<div class="card wide" style="margin-top:16px">\n      <h2>Payoff at expiry', '</div>\n    </div>')
struct_card = cut(signal, '<div class="card wide" style="margin-top:16px">\n      <h2>All seven structures', 'instead.</p>\n    </div>')

# wording that pointed at the old tabs
failed_card = failed_card.replace("figures on this tab are not", "figures on this site are not")
failed_card = failed_card.replace("Today's\n      paper trade is on the <b>Intraday</b> tab.",
                                  "Today's\n      paper trade and its full record are below.")
struct_card = struct_card.replace("the stop-loss % on the Dashboard's sizing card",
                                  "the stop-loss % on the sizing card above")
sizing_card = sizing_card.replace('<div class="card">', '<div class="card wide" style="margin-top:16px">', 1)
scanner_card = scanner_card.replace('<div class="card">', '<div class="card wide" style="margin-top:16px">', 1)

def head(n, title, sub):
    return (f'<div class="chead"><span class="cnum">{n}</span><div><h1>{title}</h1>'
            f'<p>{sub}</p></div></div>')

def nxt(go, label):
    return f'<div class="cnext"><button data-go="{go}">{label} &rarr;</button></div>'

TODAY = f'''
  <!-- ========================================================= 1 TODAY -->
  <section id="today" class="on">
    {head(1, "Today", "What is the market doing, and what is coming up?")}
    <div class="card wide verdict" id="verdict"></div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card"><h2>In plain English</h2><div id="brief"></div></div>
      <div class="card"><h2>Is option premium cheap or rich today?</h2>
        <div class="dialwrap"><div class="chartbox" id="dial"></div><div class="dialtxt" id="dialnote"></div></div></div>
    </div>
    <div class="card wide" style="margin-top:16px">
      <h2>Where the index could be by expiry &mdash; hover to read any day</h2>
      <div class="chartbox" id="cone"></div>
      <p class="note">The darker band is where the index should finish two times in three, the lighter band nineteen
      times in twenty &mdash; if the normal model holds, which in sharp moves it does not. Orange dashes are the
      breakevens of the model's pick: inside them a seller keeps money at expiry. Pink pins are scheduled events,
      grey pins market holidays.</p>
    </div>
    {world_div}
    {dashcards}
    {nxt("opportunity", "Next: is premium worth selling?")}
  </section>
'''.replace('{UND}', 'NIFTY')

OPP = f'''
  <!-- =================================================== 2 OPPORTUNITY -->
  <section id="opportunity">
    {head(2, "Opportunity", "Is option premium worth selling today? Every check, not the loudest one.")}
    {gates_div.replace(' style="margin-top:16px"', '')}
    {sigcards.replace('id="sigcards"', 'style="margin-top:16px" id="sigcards"')}
    {term_grid}
    {scanner_card}
    {nxt("trade", "Next: the trade and what it could cost")}
  </section>
'''

TRADE = f'''
  <!-- ========================================================= 3 TRADE -->
  <section id="trade">
    {head(3, "The trade", "If the model sold premium: what, how big, and what could go wrong &mdash; in rupees.")}
    {sigrec.replace(' style="margin-top:16px"', '')}
    <div class="card wide" style="margin-top:16px" id="riskcard">
      <h2>Risk in rupees &mdash; move the sliders, or click the chart</h2>
      <div class="rgrid">
        <div class="rctl">
          <label>Structure<select id="rkst"></select></label>
          <label>Lots<input type="number" id="rklots" value="1" min="1" step="1"></label>
          <label>Index moves <span id="rkmoveval"></span><input type="range" id="rkmove"></label>
          <label>Checked <span id="rkdayval"></span><input type="range" id="rkday"></label>
          <div class="rchips" id="rkchips"></div>
        </div>
        <div class="rout" id="rkout"></div>
      </div>
      <div class="chartbox" id="rkchart" style="margin-top:14px"></div>
      <p class="note">Priced with this site's normal model, using one volatility chosen so the model matches today's
      market price for the structure, shrinking as time passes. It does <b>not</b> include a jump in volatility with the
      index still, and a real stop can fill worse than its level. Charges are the site's estimate for the lots chosen.</p>
    </div>
    {payoff_card}
    {sizing_card}
    {struct_card}
    <div class="card wide" style="margin-top:16px" id="calcard"></div>
    <div class="card wide" style="margin-top:16px" id="cattbl"></div>
    {nxt("proof", "Next: has any of this actually worked?")}
  </section>
'''

PROOF = f'''
  <!-- ========================================================= 4 PROOF -->
  <section id="proof">
    {head(4, "Proof", "Has any of this actually worked? The honest record, uncertainty included.")}
    <div class="card wide" id="pmcard"></div>
    <div class="card wide" style="margin-top:16px" id="track"></div>
    {failed_card}
    <div class="subhead">The daily paper trade, in full<small>Today&rsquo;s position, where the premium goes, and every session recorded.</small></div>
    {inner(intraday)}
    <div class="subhead">The weekly signal journal<small>What the weekly model predicted, and what happened.</small></div>
    {inner(journal)}
  </section>
'''

main_old = cut(s, "<main>", "</main>")
s = s.replace(main_old, "<main>\n" + TODAY + OPP + TRADE + PROOF + "\n  <!-- =============================== TOOLS -->\n" + tools + "\n</main>")

# ------------------------------------------------------------------ nav
nav_old = cut(s, "<nav>", "</nav>")
NAV = '''<nav>
  <div class="tabs chapters">
    <button data-t="today" class="on"><i>1</i><span>Today<small>market weather</small></span></button>
    <button data-t="opportunity"><i>2</i><span>Opportunity<small>worth selling?</small></span></button>
    <button data-t="trade"><i>3</i><span>The trade<small>what &amp; how much</small></span></button>
    <button data-t="proof"><i>4</i><span>Proof<small>track record</small></span></button>
  </div>
  <div class="tabs tools"><span class="toolslbl">&nbsp;Tools</span>
    <button data-t="builder">Strategy builder</button>
    <button data-t="chain">Option chain</button>
    <button data-t="oi">Open interest</button>
    <button data-t="structure">Structure price</button>
    <button data-t="health">Data health</button>
  </div>
</nav>
<div class="banner"><b>Personal research tool.</b> Paper trades only &mdash; nothing here places an order, and nothing
here is investment advice. It exists to show what these strategies really do, including when they do not work.</div>'''
s = s.replace(nav_old, NAV)

# ------------------------------------------------------------------ header toggle
s = once(s, '<div class="clock" id="hclock">–</div>\n</header>',
  '''<div class="clock" id="hclock">–</div>
  <button class="themebtn" id="themebtn" title="Switch theme" aria-label="Switch between light and dark theme">
    <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  </button>
</header>''')

# theme before first paint (no flash)
s = once(s, '<meta name="viewport" content="width=device-width, initial-scale=1">',
  '''<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){var t='light';try{t=localStorage.getItem('desk-theme')||'light'}catch(e){}document.documentElement.dataset.theme=t;})();</script>''')

# ------------------------------------------------------------------ css
s = once(s, "</style>", open(os.path.join(V2, "v2.css"), encoding="utf-8").read() + "\n</style>")

# ------------------------------------------------------------------ js hooks
s = once(s, "const tab = () => document.querySelector('.tabs button.on').dataset.t;",
            "const tab = () => (document.querySelector('nav button.on') || {dataset:{t:'today'}}).dataset.t;")
s = once(s, """document.querySelectorAll('.tabs button').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));""",
"""document.querySelectorAll('nav button[data-t]').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('nav button[data-t]').forEach(x=>x.classList.toggle('on',x===b));""")
s = once(s, "    if(t==='dash')    await loadDash();\n    if(t==='signal')  await loadSignal();",
"""    // Each step on its own: one loader failing must not blank the rest of
    // the chapter, which is what a single try around all of them would do.
    const run = async f => { try{ await f(); }catch(e){ console.error(e); } };
    const V = window.V2 || {};
    if(t==='today')   { await run(loadDash); await run(V.today); }
    if(t==='opportunity'){ await run(loadDash); await run(loadSignal); }
    if(t==='trade')   { await run(loadDash); await run(loadSignal); await run(V.trade); await run(() => window.SB && SB.scoreTable()); await run(() => window.SB && SB.calCard()); }
    if(t==='proof')   { await run(() => window.SB && SB.pmCard()); await run(V.proof); await run(loadIntraday); await run(loadJournal); }""")
s = once(s, "el2.oninput=()=>{ if(tab()==='dash') loadDash(); };",
            "el2.oninput=async()=>{ const t=tab(); if(['today','opportunity','trade'].includes(t)){ await loadDash(); if(t==='trade'&&window.V2) V2.trade(); if(t==='trade'&&window.SB) SB.scoreTable(); } };")

# ------------------------------------------------------------------ v3 hooks
# The classic builder's controls are gone, so their top-level bindings must go
# too -- one `$('#clearlegs').onclick` on a missing element throws and stops
# the whole script.
s = once(s, """$('#clearlegs').onclick=()=>{ LEGS=[]; renderBuilder(); };
document.querySelectorAll('[data-lot]').forEach(b=>b.onclick=()=>{
  LOTS=Math.max(1,LOTS+ +b.dataset.lot); $('#lotval').textContent=LOTS;
});
$('#preset').onchange=e=>{ loadPreset(e.target.value); e.target.value=''; };
""", "")
s = once(s, """    if(t==='builder'){
      if(!S) await loadSignal();
      if(!CHAIN) await loadChain(false);
      $('#preset').innerHTML='<option value="">load a structure…</option>'
        +(S?S.structures.map(x=>`<option value="${x.name}">${x.label}</option>`).join(''):'');
      fillAddStrikes();
      renderBuilder();
      renderAB();
    }""", """    if(t==='builder'){ if(window.SB) await SB.load(); }""")
s = once(s, """$('#pinA').onclick = () => pinStructure('A');
$('#pinB').onclick = () => pinStructure('B');
$('#clearAB').onclick = () => { PIN = {A:null, B:null}; renderAB(); };
$('#ablock').onchange = () => renderAB();
""", "")
_i = s.index("$('#addleg').onclick = () => {")
_j = s.index("});\n", s.index("$('#addl').textContent = ADDLOTS;", _i)) + 4
s = s[:_i] + s[_j:]
# Historical-fit POP beside the model POP in the seven-structure table
# (was 'calibrated' until the 26 Sep 2026 ten-year test found no improvement).
s = once(s, """<th class="dimtxt" title="MODELLED probability, from a VIX-derived sigma. The calibration tab measures it: 78.2% claimed, 57.9% delivered. Read net EV first.">POP</th>""",
            """<th class="dimtxt" title="MODELLED probability, normal distribution at the VIX-derived sigma. Read net EV first.">POP</th>
    <th title="Historical-fit POP: the same zone under the distribution of past NIFTY moves. Tested year by year 2019-2026 it was no more accurate than model POP - see 'How accurate is POP?' below. Changes no decision. NIFTY only.">POP hist.</th>""")
s = once(s, """      <td class="dimtxt">${pct(x.pop)}</td>
      <td class="dimtxt" title="workbook""", """      <td class="dimtxt">${pct(x.pop)}</td>
      <td>${x.pop_cal==null?'–':pct(x.pop_cal)}</td>
      <td class="dimtxt" title="workbook""")
# A settled expiry (past 15:30 IST on its day) is not offered on the live session.
s = once(s, "return list.filter(x => (byToday ? (x.dte_from_today ?? x.dte) : x.dte) >= 0);",
            "return list.filter(x => byToday ? (!x.settled && (x.dte_from_today ?? x.dte) >= 0) : x.dte >= 0);")

# the v2 block, last, so everything it calls already exists
s = once(s, "</body>", "<script>\n" + open(os.path.join(V2, "v2.js"), encoding="utf-8").read() + "\n</script>\n</body>")

# v3: the builder, the scored catalogue, the headline filters. The catalogue
# definitions come from catalogue.py -- ONE source for the page and the server.
sys.path.insert(0, os.path.dirname(ROOT))
import json as _json
import catalogue as _cat
_v3 = open(os.path.join(V2, "v3.js"), encoding="utf-8").read()
_v3 = once(_v3, '/*CATALOGUE*/{"groups":[],"strategies":[]}/*END*/',
           '/*CATALOGUE*/' + _json.dumps(_cat.definitions(), separators=(",", ":")) + '/*END*/')
s = once(s, "</body>", "<script>\n" + _v3 + "\n</script>\n</body>")
s = once(s, "</style>", open(os.path.join(V2, "v3.css"), encoding="utf-8").read() + "\n</style>")


# ------------------------------------------------------------------ wording
for old, new in [
    ("columns on the Chain tab inherit", "columns in the option chain (Tools) inherit"),
    ("the strategy behind it was tested and failed — the numbers are on the\n    Dashboard. Today's intraday paper trade is beside this card.",
     "the strategy behind it was tested and failed — the numbers are in\n    chapter 4, Proof. Today's intraday paper trade is beside this card."),
    ("number on the Signal tab. A trade", "number below it. A trade"),
    ("described at the top of this tab: a weekly", "described in chapter 4: a weekly"),
    ("premium turns out to be compensation for, are on the Intraday tab.", "premium turns out to be compensation for, are in chapter 4, Proof."),
    ("and the Structure tab's per-minute series", "and the structure-price tool's per-minute series"),
]:
    s = once(s, old, new)

s = s.replace("<title>NIFTY options desk</title>", "<title>NIFTY options desk</title>", 1)
open(DST, "w", encoding="utf-8").write(s)
print("wrote", DST, len(s))
