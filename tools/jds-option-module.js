/* JDS Hub module navigation. Calculation/rendering hooks stay in shared tools. */
(() => {
  const $ = s => document.querySelector(s);
  $('.jds-site-header .nav-links')?.append($('#themebtn'));
  const nav = $('nav');
  nav.setAttribute('aria-label', 'Option engine tools');
  nav.classList.add('jds-module-nav');
  const groups = [
    ['Monitor', [['today', 'Models & status'], ['trade', 'Paper trades'], ['proof', 'Performance']]],
    ['Analyse', [['builder', 'Strategy builder'], ['chain', 'Option chain'], ['oi', 'Open interest'], ['structure', 'Market structure']]],
    ['Reference', [['opportunity', 'Model research'], ['health', 'Data health'], ['learn', 'Help & glossary']]]
  ];
  const buttons = new Map([...nav.querySelectorAll('[data-t]')].map(b => [b.dataset.t, b]));
  // Keep the existing button objects, listeners and nav delegation intact.
  nav.replaceChildren();
  for (const [label, entries] of groups) {
    const group = document.createElement('div'); group.className = 'jds-tool-group';
    const heading = document.createElement('span'); heading.textContent = label; group.append(heading);
    for (const [id, title] of entries) {
      const button = buttons.get(id); if (!button) continue;
      button.textContent = title; button.setAttribute('aria-label', title); group.append(button);
      const pageTitle = $(`#${id} .chead h1`); if (pageTitle) pageTitle.textContent = title;
    }
    nav.append(group);
  }
  const layout = document.createElement('div'); layout.className = 'jds-module-layout';
  nav.before(layout); layout.append(nav, $('main'));
  // No standalone landing page, product directory, FAQ, footer or brand shell.
  const overview = $('#today');
  overview.querySelector('.guide-card')?.remove();
  overview.querySelector('.chead p').textContent = 'Monitor the running paper model and the separate confirmation experiment.';
  const context = $('#desk-overview').previousElementSibling;
  if (context?.classList.contains('guide-card')) {
    context.querySelector('h2').textContent = 'Model status';
  }
  const headings = $('#learn').querySelectorAll('.guide-card');
  headings[0]?.remove();
  $('#learn .chead p').textContent = 'Definitions and explanations for the Option engine tools.';
  document.querySelectorAll('[data-guide-go="today"]').forEach(b => b.textContent = 'Models & status');
  const notes = $('#proof .guide-card .desk-note');
  if (notes) notes.textContent = 'Select a strategy first. Confirmation forward progress is under Models & status. This journal contains the opening spread and the older straddle control.';
  const reset = document.createElement('button'); reset.className = 'desk-btn'; reset.textContent = 'Clear search';
  reset.onclick = () => { const input = $('#guide-term'); input.value = ''; input.dispatchEvent(new Event('input')); input.focus(); };
  $('#guide-term').parentElement.after(reset);
})();
