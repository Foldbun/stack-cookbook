/* Foldbun Stack Cookbook site script. Content never depends on it.
   1. theme menu: System / Light / Dark, stored in localStorage "foldbun.theme"; src/scripts/theme-init.js applies the stored
      value before first paint (Starlight needs a resolved data-theme, so "system" resolves to the current colour scheme);
   2. language links: a click writes the cookie foldbun_lang (lang-pref.js; every Foldbun site reads it and lang-pref.js acts on it
      at the next visit). Merely visiting a page stores nothing;
   3. home page matrix: the language filter (see HomeMatrix.astro). */
(function () {
  var root = document.documentElement;
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  var current = get('foldbun.theme') || 'system';
  function apply(t) {
    current = t;
    root.setAttribute('data-theme', t === 'light' || t === 'dark' ? t : mq.matches ? 'dark' : 'light');
    document.querySelectorAll('[data-theme-set]').forEach(function (b) {
      b.setAttribute('aria-checked', b.getAttribute('data-theme-set') === t ? 'true' : 'false');
    });
  }
  apply(current);
  if (mq.addEventListener) mq.addEventListener('change', function () { if (current === 'system') apply('system'); });
  document.querySelectorAll('.js-theme-menu').forEach(function (m) { m.hidden = false; });

  function chooseLang(t) {
    if (window.foldbunLang) window.foldbunLang.set(t.getAttribute('data-set-lang'));
  }
  // A middle-click opens the other locale in a new tab without a click event: store the choice then too.
  document.addEventListener('auxclick', function (e) {
    var t = e.button === 1 && e.target.closest('[data-set-lang]');
    if (t) chooseLang(t);
  });
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-theme-set], [data-set-lang]');
    if (!t) return;
    if (t.hasAttribute('data-theme-set')) {
      var v = t.getAttribute('data-theme-set');
      set('foldbun.theme', v); apply(v);
      var d = t.closest('details'); if (d) d.open = false;
    } else chooseLang(t);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') document.querySelectorAll('details.menu[open]').forEach(function (d) { d.open = false; });
  });

  // Matrix filter: show the use cases that have the chosen language and make the code tabs of every page follow it
  // (Starlight keeps the synced tab choice in localStorage "starlight-synced-tabs__lang", the value is the tab label).
  var matrix = document.querySelector('[data-matrix]');
  if (matrix) {
    var buttons = matrix.querySelectorAll('[data-filter]');
    var filter = function (lang, label, remember) {
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-filter') === lang ? 'true' : 'false'); });
      matrix.setAttribute('data-active', lang);
      var shown = 0;
      matrix.querySelectorAll('tbody tr').forEach(function (tr) {
        var has = lang === 'all' || tr.getAttribute('data-langs').split(' ').indexOf(lang) >= 0;
        tr.hidden = !has;
        if (has) shown++;
      });
      var empty = matrix.querySelector('[data-empty]');
      if (empty) empty.hidden = shown > 0;
      if (remember && lang !== 'all') set('starlight-synced-tabs__lang', label);
    };
    buttons.forEach(function (b) {
      b.hidden = false;
      b.addEventListener('click', function () { filter(b.getAttribute('data-filter'), b.getAttribute('data-label'), true); });
    });
    var stored = get('starlight-synced-tabs__lang');
    matrix.querySelectorAll('[data-filter]').forEach(function (b) {
      if (stored && b.getAttribute('data-label') === stored) filter(b.getAttribute('data-filter'), stored, false);
    });
  }
})();
