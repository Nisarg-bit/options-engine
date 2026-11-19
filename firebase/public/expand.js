/* Every collapsible block on the site shown open, as a normal section (Nov 2026).
   The <details> blocks (rules, research write-ups, glossary, FAQ, Advanced tools,
   header settings) were all closed by default. This opens each one, including
   ones the pages create later, and keeps it open: the summary becomes a plain
   heading rather than a click target. Loaded last, after every other script. */
(function(){
'use strict';
const css=document.createElement('style');
css.textContent=`details.always-open>summary{list-style:none;cursor:default;pointer-events:none;font-weight:600}
details.always-open>summary::-webkit-details-marker{display:none}
details.always-open>summary::marker{content:''}`;
document.head.append(css);
// Header pickers ("Market snapshot settings", "Model metric") are drop-down
// controls, not content: forced open they cover the page. They keep working as menus.
const MENUS='.studio-picker,.studio-header-detail';
// "Advanced tools" in the nav was a drop-down too: its buttons now sit in the nav row.
function flattenTools(){
  const d=document.querySelector('nav details.guide-tools'); if(!d) return;
  const tools=d.querySelector('.tools'); if(!tools) return;
  tools.classList.add('tools-inline'); d.replaceWith(tools);
}
css.textContent+=`nav .tools-inline{display:flex!important;flex-wrap:wrap;align-items:center;gap:2px;position:static!important;
  box-shadow:none!important;background:none!important;border:0!important;border-radius:0!important;
  padding:0!important;margin:0;min-width:0;opacity:1}
nav .tools-inline button{font-size:inherit!important;padding:9px 12px!important}`;
flattenTools();
function openAll(root){
  (root.querySelectorAll?root:document).querySelectorAll('details:not(.always-open)').forEach(d=>{
    if(d.matches(MENUS)) return;
    d.open=true; d.classList.add('always-open');
    d.addEventListener('toggle',()=>{ if(!d.open) d.open=true; });
  });
}
openAll(document);
new MutationObserver(ms=>{ for(const m of ms) for(const n of m.addedNodes){
  if(n.nodeType!==1) continue;
  if(n.matches&&n.matches('details')) openAll(n.parentNode||document); else openAll(n);
}}).observe(document.body,{childList:true,subtree:true});
})();
