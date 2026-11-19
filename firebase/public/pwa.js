/* Installable-app support: registers the service worker and shows an
 * "Install app" button in the header when the browser offers installation.
 * iOS Safari has no install event, so it gets a one-line "Add to Home Screen" hint instead.
 */
(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function (e) { console.warn('service worker failed', e); });
    });
  }

  var standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (standalone) return; // already running as the installed app

  var css = document.createElement('style');
  css.textContent =
    '.pwa-install{display:none;align-items:center;gap:6px;margin-left:8px;padding:6px 12px;border-radius:999px;' +
    'border:1px solid var(--edge,rgba(255,255,255,.12));background:var(--glass,rgba(255,255,255,.06));color:inherit;' +
    'font:600 12px/1 var(--sans,system-ui);cursor:pointer}' +
    '.pwa-install.show{display:inline-flex}.pwa-install svg{width:14px;height:14px}' +
    '.pwa-hint{position:fixed;left:12px;right:12px;bottom:12px;z-index:9999;padding:12px 14px;border-radius:14px;' +
    'background:#0b1020;color:#eaf0ff;font:13px/1.4 var(--sans,system-ui);box-shadow:0 8px 30px rgba(0,0,0,.35);' +
    'display:flex;gap:10px;align-items:center}.pwa-hint button{margin-left:auto;background:none;border:0;color:inherit;' +
    'font-size:18px;cursor:pointer}';
  document.head.appendChild(css);

  var btn = document.createElement('button');
  btn.className = 'pwa-install';
  btn.type = 'button';
  btn.setAttribute('aria-label', 'Install this desk as an app');
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M12 3v12m0 0-4-4m4 4 4-4M5 21h14"/></svg>Install app';

  function mount() {
    var anchor = document.getElementById('themebtn');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
    else (document.querySelector('header') || document.body).appendChild(btn);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();

  var deferred = null;
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferred = e;
    btn.classList.add('show');
  });
  btn.addEventListener('click', function () {
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.finally(function () { deferred = null; btn.classList.remove('show'); });
  });
  window.addEventListener('appinstalled', function () { btn.classList.remove('show'); });

  // iPhone / iPad: Safari only supports Share -> Add to Home Screen.
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var dismissed = false;
  try { dismissed = localStorage.getItem('pwa-hint-dismissed') === '1'; } catch (e) {}
  if (ios && !dismissed) {
    window.addEventListener('load', function () {
      var hint = document.createElement('div');
      hint.className = 'pwa-hint';
      hint.setAttribute('role', 'note');
      hint.innerHTML = '<span>Install: tap <b>Share</b> then <b>Add to Home Screen</b>.</span>' +
        '<button type="button" aria-label="Dismiss">×</button>';
      hint.querySelector('button').onclick = function () {
        hint.remove();
        try { localStorage.setItem('pwa-hint-dismissed', '1'); } catch (e) {}
      };
      document.body.appendChild(hint);
    });
  }
})();
