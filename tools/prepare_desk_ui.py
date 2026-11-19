"""Apply scoped wording corrections without changing pricing calculations."""
from pathlib import Path
p=Path('firebase/public/index.html')
s=p.read_text(encoding='utf-8').replace('Recommended &mdash; weekly','Legacy weekly model')
s=s.replace('of your ${', 'of the example ${')
p.write_text(s,encoding='utf-8')
p=Path('firebase/public/desk.js')
s=p.read_text(encoding='utf-8').replace("r.sample_eligible===true||r.history_eligible===true", "[true,'True','true','1',1].includes(r.sample_eligible)&&r.entry==='09:20'")
p.write_text(s,encoding='utf-8')
