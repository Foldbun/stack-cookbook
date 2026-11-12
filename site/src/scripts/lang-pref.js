/* Shared Foldbun language preference: cookie `foldbun_lang` = en | zh, written only on an explicit choice.
   Blocking script in <head>, before first paint, on every page. This is the same script as on the Foldbun Stack documentation
   site, so a choice made on any Foldbun site applies here (see THEME.md).
   1. Exposes window.foldbunLang = { get, set } for site.js (set = explicit choice only).
   2. Preference = the cookie; when absent, migrate an explicit locale from localStorage "stack.langHint" (a dismissal
      value "<page>><suggested>" is not a locale and is ignored) into the cookie.
   3. Preference differs from the page locale (<html lang>) and the page declares its counterpart
      (<meta name="foldbun-alt" content="/zh/x/">, emitted by LangPref.astro only where the counterpart exists and
      never on 404 pages): location.replace(counterpart + search + hash). Equal, none, or no counterpart: nothing.
   All storage access is guarded; without JS nothing happens and the site works as before. */
(function () {
  var NAME = 'foldbun_lang';
  function get() {
    try {
      var m = new RegExp('(?:^|;\\s*)' + NAME + '=([^;]*)').exec(document.cookie);
      return m && (m[1] === 'en' || m[1] === 'zh') ? m[1] : null;
    } catch (e) { return null; }
  }
  function set(v) {
    if (v !== 'en' && v !== 'zh') return;
    try {
      var h = location.hostname;
      var fb = h === 'foldbun.com' || h.slice(-12) === '.foldbun.com';
      document.cookie = NAME + '=' + v + (fb ? '; Domain=foldbun.com' : '') + '; Path=/; Max-Age=31536000; SameSite=Lax' +
        (fb || location.protocol === 'https:' ? '; Secure' : '');
    } catch (e) {}
  }
  function stored(k) {
    try {
      var v = localStorage.getItem(k);
      return v === 'en' ? 'en' : v === 'zh' || v === 'zh-Hans' ? 'zh' : null;
    } catch (e) { return null; }
  }
  window.foldbunLang = { get: get, set: set };
  var pref = get();
  if (!pref) {
    pref = stored('stack.langHint');
    if (pref) set(pref);
  }
  if (!pref) return;
  var root = document.documentElement;
  var page = /^zh/i.test(root.lang) ? 'zh' : 'en';
  var alt = document.querySelector('meta[name="foldbun-alt"]');
  var to = alt && alt.getAttribute('content');
  if (pref === page || !to || to === location.pathname) return;
  location.replace(to + location.search + location.hash);
})();
