/* PYROTECH VISION — hash router with page lifecycle and leave-guards (unsaved changes). */
(function () {
  'use strict';
  const routes = [];          // { pattern: RegExp, keys: [], page }
  let current = null;         // { page, params, path }
  let ignoreNext = false;
  let lastHash = '';

  function register(path, page) {
    const keys = [];
    const re = new RegExp('^' + path.replace(/:[a-zA-Z]+/g, function (m) { keys.push(m.slice(1)); return '([^/]+)'; }) + '/?$');
    routes.push({ pattern: re, keys: keys, page: page, path: path });
  }

  function match(path) {
    for (let i = 0; i < routes.length; i++) {
      const m = routes[i].pattern.exec(path);
      if (m) {
        const params = {};
        routes[i].keys.forEach(function (k, j) { params[k] = decodeURIComponent(m[j + 1]); });
        return { route: routes[i], params: params };
      }
    }
    return null;
  }

  function currentPath() { return (location.hash.replace(/^#/, '') || '/dashboard').split('?')[0]; }

  async function handle() {
    if (ignoreNext) { ignoreNext = false; return; }
    const path = currentPath();
    if (current && current.page.canLeave) {
      const ok = await current.page.canLeave();
      if (!ok) { ignoreNext = true; location.hash = lastHash || '#/dashboard'; return; }
    }
    const found = match(path) || match('/not-found');
    if (current && current.page.destroy) { try { current.page.destroy(); } catch (e) { console.error('[AI VISION] page destroy failed:', e); } }
    const container = document.getElementById('page');
    if (window.AV.ui && window.AV.ui.stopStreams) window.AV.ui.stopStreams(container);
    container.innerHTML = '';
    container.classList.remove('page-enter'); void container.offsetWidth; container.classList.add('page-enter');
    current = { page: found.route.page, params: found.params, path: path };
    lastHash = location.hash || '#/dashboard';
    try {
      await found.route.page.render(container, found.params);
    } catch (e) {
      console.error('[AI VISION] render failed:', e);
      container.innerHTML = window.AV.ui.emptyState('alert', 'This page failed to load', e.message || String(e), '<a class="btn btn-sm" href="#/dashboard">Back to dashboard</a>');
    }
    window.AV.icons.hydrate(container);
    document.dispatchEvent(new CustomEvent('av:route', { detail: { path: path, page: found.route.page } }));
    const main = document.getElementById('main');
    if (main) main.scrollTop = 0;
    window.scrollTo(0, 0);
  }

  function go(path) {
    if (('#' + path) === location.hash) handle(); else location.hash = path;
  }

  /** Re-render the current page without lifecycle guard (e.g., after data changes). */
  function refresh() { if (current && current.page.refresh) current.page.refresh(); }

  function start() {
    window.addEventListener('hashchange', handle);
    if (!location.hash) { history.replaceState(null, '', '#/dashboard'); }
    handle();
  }

  window.AV.router = { register: register, start: start, go: go, refresh: refresh, current: function () { return current; }, path: currentPath };
})();
