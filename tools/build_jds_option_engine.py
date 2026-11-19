"""Generate the JDS edition from the unchanged standalone desk source.

Run after the desk build. Output is staged locally; does not deploy anything.
"""
from pathlib import Path
import shutil
import re

ROOT = Path(__file__).resolve().parents[1]
JDS = Path('C:/Users/Nisarg/Desktop/jds-wealth-website')
OUT = ROOT / 'data/jds-option-integration'
PUBLIC = OUT / 'public'
PUBLIC.mkdir(parents=True, exist_ok=True)
for item in JDS.iterdir():
    if item.is_file() and item.suffix in {'.html', '.css', '.js', '.json', '.png'} and not item.name.startswith(('.', '_')) and item.name not in {'firebase.json', 'jds-wealth-website_bonds-raw.json'}:
        shutil.copy2(item, PUBLIC / item.name)
hub = (JDS / 'hub.html').read_text(encoding='utf-8')
tile = '''<a class="htile" href="option-engine.html">
        <span class="chip plan">PAPER RESEARCH</span>
        <div class="hic" aria-hidden="true">⌁</div>
        <h3>Option engine</h3>
        <p>Explore Indian index options, follow paper trades, review results and learn how the strategies work.</p>
        <span class="go">Open Option engine →</span>
      </a>'''
if 'href="option-engine.html"' not in hub:
    hub = hub.replace('<a class="htile" href="tools.html">', tile + '\n      <a class="htile" href="tools.html">', 1)
(PUBLIC / 'hub.html').write_text(hub, encoding='utf-8')
assets = PUBLIC / 'option-engine-assets'
assets.mkdir(exist_ok=True)
for name in ['desk.css', 'desk.js', 'guide.js', 'intraday-validation.json', 'daily-spread-research.json']:
    shutil.copy2(ROOT / 'firebase/public' / name, assets / name)
html = (ROOT / 'firebase/public/index.html').read_text(encoding='utf-8')
html = html.replace('<title>NIFTY options desk</title>', "<title>Option engine | JD's Wealth Management</title>")
html = html.replace('desk-theme', 'jdtheme')
html = html.replace('<link rel="stylesheet" href="/studio.css">', '')
html = html.replace('<script src="/studio.js"></script>', '<script src="/option-engine-assets/jds-module.js"></script>')
shutil.copy2(ROOT / 'tools/jds-option-module.js', assets / 'jds-module.js')
for name in ['desk.css', 'studio.css', 'desk.js', 'guide.js', 'studio.js', 'intraday-validation.json', 'daily-spread-research.json']:
    html = html.replace('/' + name, '/option-engine-assets/' + name)
auth = '''<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore-compat.js"></script>
<script src="/firebase-config.js?v=2"></script>
<script>document.documentElement.classList.add('jds-checking');document.addEventListener('jd-auth',function(){document.documentElement.classList.remove('jds-checking')});window.RTDB_URL='https://jds-wealth-management-default-rtdb.asia-southeast1.firebasedatabase.app';</script>
<script src="/auth-guard.js?v=2"></script>
<link rel="icon" href="/logo.png">
<link rel="stylesheet" href="/option-engine-assets/jds.css">
'''
html = html.replace('</head>', auth + '</head>')
shell = '''<div class="jds-shell" role="banner">
<a class="jds-brand" href="/hub.html"><img src="/logo.png" alt=""><span>JD's Wealth Management<small>MANAGING WEALTH, SECURING FUTURES</small></span></a>
<div class="jds-links"><a href="/hub.html">← Back to Hub</a></div>
</div><div class="jds-context"><strong>Option engine</strong><span>Research &amp; paper trading · No broker orders</span></div>'''
html = html.replace('<body>', '<body>' + shell, 1)
shared_nav = re.search(r'<nav>.*?</nav>', hub, re.S).group(0)
shared_nav = shared_nav.replace('<nav>', '<div class="jds-site-header" role="navigation" aria-label="JDS website">', 1).replace('</nav>', '</div>')
html = html.replace(shell, shared_nav + '''<div class="jds-context"><div class="jds-crumb"><a href="/index.html">Home</a><span>/</span><a href="/hub.html">My Wealth Hub</a><span>/ Option engine</span></div><strong>Option engine</strong><span>Explore option models, follow paper trades and review performance from your wealth hub.</span><span class="jds-paper-label">PAPER RESEARCH · NO BROKER ORDERS</span></div>''')
html = html.replace('<div class="mark"></div>', '')
html = html.replace('<span id="brand">NIFTY</span> options desk', '<span id="brand">NIFTY</span> · market snapshot')
(PUBLIC / 'option-engine.html').write_text(html, encoding='utf-8')
css = '''@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Manrope:wght@400;500;600;700;800&display=swap');
:root,:root[data-theme=light]{--sans:Manrope,'Segoe UI',sans-serif;--desk-accent:#1a5fa8;--desk-bg:#f4f8fc;--desk-panel:#fff;--desk-ink:#22303f;--desk-muted:#64748b;--desk-line:#e2e8f0;--studio-tint:#edf4fb;--a1:#1a5fa8;--a2:#3aa544}
:root[data-theme=dark]{--desk-accent:#9ac7f5;--desk-bg:#0b1726;--desk-panel:#13263b;--desk-ink:#eaf2fb;--desk-muted:#b4c5d8;--desk-line:#2c4561;--studio-tint:#1c3650}
.jds-checking body{visibility:hidden}.jds-checking #jdGate{visibility:visible}
.jds-shell{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px max(24px,calc((100vw - 1392px)/2));background:var(--desk-panel);border-bottom:1px solid var(--desk-line)}
.jds-brand{display:flex;align-items:center;gap:12px;color:var(--desk-accent);text-decoration:none;font:700 23px Fraunces,Georgia,serif}.jds-brand img{width:48px;height:48px;object-fit:contain}.jds-brand small{display:block;font:700 9px Manrope,sans-serif;letter-spacing:1.6px;color:#3aa544;margin-top:5px}
.jds-links{display:flex;gap:12px;flex-wrap:wrap}.jds-links a{font-size:12px;color:var(--desk-accent);padding:9px 12px;border:1px solid var(--desk-line);border-radius:9px;text-decoration:none}
.jds-context{display:flex;align-items:center;justify-content:space-between;gap:15px;background:linear-gradient(120deg,#0b2e55,#12467e 70%,#1a6c69);color:#fff;padding:22px 28px}.jds-context strong{font:600 28px Fraunces,Georgia,serif}.jds-context span{font-size:12px;opacity:.9}
.studio-hero h1,.studio-section-head h2,.studio-live-heading,.studio-faq h2,.chead h1{font-family:Fraunces,Georgia,serif;letter-spacing:-.02em}.studio-hero{box-shadow:0 10px 28px rgba(18,70,126,.07)}.studio-flow{background:var(--studio-tint)}.studio-product,.guide-card,.card{border-radius:16px!important}.studio-product:hover{box-shadow:0 10px 28px rgba(18,70,126,.09)}
@media(max-width:700px){.jds-shell{align-items:flex-start;flex-direction:column;padding:16px}.jds-brand{font-size:20px}.jds-context{align-items:flex-start;flex-direction:column;padding:18px}.jds-context strong{font-size:24px}.jds-links a{min-height:40px}.jds-links{width:100%}}
'''
(assets / 'jds.css').write_text(css, encoding='utf-8')
with (assets / 'jds.css').open('a', encoding='utf-8') as f:
    f.write('''
.jds-module-layout{display:grid;grid-template-columns:210px minmax(0,1fr);max-width:1600px;margin:auto;align-items:start}
nav.jds-module-nav{display:block;position:sticky!important;top:0;padding:24px 14px!important;border-right:1px solid var(--desk-line);border-bottom:0;min-height:calc(100vh - 100px)}
.jds-tool-group{display:grid;gap:4px;margin-bottom:26px}.jds-tool-group>span{font-size:10px;text-transform:uppercase;letter-spacing:.15em;color:var(--desk-muted);padding:0 12px 8px}.jds-tool-group button{text-align:left;padding:10px 12px;border:0;background:transparent;font-size:13px;width:100%}
.jds-module-layout main{width:100%;min-width:0;padding:26px!important}.chead h1{font-family:Fraunces,Georgia,serif}.jds-module-layout .chead{margin-bottom:20px!important}.jds-module-layout .guide-card{border-radius:14px}header .brand{font-size:13px}header .brand small{font-size:11px}header{padding:14px 24px!important}.themebtn{position:static!important;transform:none!important;margin-left:auto}
@media(max-width:900px){.jds-module-layout{grid-template-columns:1fr}nav.jds-module-nav{position:relative!important;min-height:0;display:flex;gap:16px;overflow:auto;border-right:0;border-bottom:1px solid var(--desk-line);padding:12px!important}.jds-tool-group{display:flex;align-items:center;gap:4px;margin:0}.jds-tool-group>span{display:none}.jds-tool-group button{width:auto}.jds-module-layout main{padding:16px!important}}
''')
config = '''{"hosting":{"public":"public","ignore":["**/.*"],"headers":[{"source":"**","headers":[{"key":"Cache-Control","value":"no-cache"}]}]}}'''
(OUT / 'firebase.json').write_text(config, encoding='utf-8')
print(f'Built JDS edition: {PUBLIC}')
with (assets / 'jds.css').open('a', encoding='utf-8') as f:
    f.write((ROOT / 'tools/jds-option-design.css').read_text(encoding='utf-8'))
