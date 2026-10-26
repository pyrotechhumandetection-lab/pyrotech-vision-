/* PYROTECH VISION — application shell: routing table, sidebar, top bar, search, notifications, backend health. */
(function () {
  'use strict';
  const AV = window.AV, ui = AV.ui, icon = AV.icon, state = AV.state, api = AV.api, router = AV.router, P = AV.pages;
  const esc = ui.esc;
  let healthTimer = null;

  // ---------- prefs ----------
  function applyPrefs() {
    const p = state.prefs();
    document.documentElement.setAttribute('data-theme', p.theme === 'light' ? 'light' : 'dark');
    applyMotion();
    const tb = document.getElementById('theme-btn');
    tb.innerHTML = icon(p.theme === 'light' ? 'moon' : 'sun', 19);
    tb.setAttribute('aria-label', p.theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
    tb.title = tb.getAttribute('aria-label');
    document.getElementById('profile-name').textContent = p.profileName;
    document.getElementById('profile-role').textContent = p.profileRole;
    document.getElementById('avatar').textContent = (p.profileName || 'U').split(/\s+/).map(function (w) { return w[0]; }).join('').slice(0, 2).toUpperCase();
    const grp = document.getElementById('nav-detections');
    grp.classList.toggle('open', p.navDetectionsOpen !== false);
    document.getElementById('nav-det-toggle').setAttribute('aria-expanded', p.navDetectionsOpen !== false);
  }
  function applyMotion() {
    if (state.prefs().motion === 'reduce') document.documentElement.setAttribute('data-motion', 'reduce');
    else document.documentElement.removeAttribute('data-motion');
  }
  function setTheme(t) { state.setPref('theme', t); applyPrefs(); }

  // ---------- backend ----------
  async function checkBackend(force) {
    const was = api.status.connected;
    await api.checkHealth();
    await state.refreshFromBackend();
    if (api.status.connected) await state.pollCameras();   // live sessions for camera IP / active state
    updateConn();
    if (force || was !== api.status.connected) {
      state.emit('backend');
      const cur = router.current();
      if (cur && cur.page.onBackend) cur.page.onBackend();
    }
    if (was && !api.status.connected) ui.toast('error', 'Backend connection lost', api.status.error || '');
    if (!was && api.status.connected && api.status.checked && force !== true) ui.toast('success', 'Backend connected', api.base());
    updateBadges();
    return api.status;
  }
  function updateConn() {
    const pill = document.getElementById('conn-pill'), txt = document.getElementById('conn-text');
    const dot = pill.querySelector('.dot'), sdot = document.getElementById('sys-dot'), sdesc = document.getElementById('sys-desc');
    let cls, label, desc;
    if (api.status.connected) { cls = 'dot-success'; label = 'Backend connected'; desc = 'Backend online'; }
    else if (!api.status.checked) { cls = 'dot-muted'; label = 'Checking…'; desc = 'Checking backend…'; }
    else { cls = 'dot-warning'; label = 'Backend not connected'; desc = 'Backend not connected'; }
    dot.className = 'dot ' + cls; sdot.className = 'dot ' + cls;
    txt.textContent = label; sdesc.textContent = desc;
    sdesc.style.color = api.status.connected ? 'var(--success)' : 'var(--warning)';
    pill.title = api.status.connected ? 'Connected to ' + api.base() : (api.status.error || 'Backend not connected');
  }
  let eventTimer = null, cameraTimer = null;
  function startHealthPolling() {
    clearInterval(healthTimer);
    healthTimer = setInterval(function () { if (!document.hidden) checkBackend(false); }, AV.config.healthPollMs);
    // New alerts / detections / snapshots from the backend (badge, Recent Alerts, Snapshots page)
    clearInterval(eventTimer);
    eventTimer = setInterval(function () { if (!document.hidden && api.status.connected) state.pollEvents().then(updateBadges); }, AV.config.eventPollMs || 5000);
    // Camera list + running sessions (IP, status, active camera), so connection changes show within seconds.
    clearInterval(cameraTimer);
    cameraTimer = setInterval(function () { if (!document.hidden && api.status.connected) state.pollCameras(); }, AV.config.cameraPollMs || 4000);
  }

  // ---------- badges ----------
  function updateBadges() {
    const n = state.unackCount();
    const nb = document.getElementById('nav-alert-count'), cb = document.getElementById('notif-count');
    [nb, cb].forEach(function (b) { b.hidden = !n; b.textContent = n > 99 ? '99+' : n; });
    document.getElementById('notif-btn').setAttribute('aria-label', 'Notifications' + (n ? ', ' + n + ' open alerts' : ''));
  }

  // ---------- nav ----------
  function highlightNav(path) {
    document.querySelectorAll('[data-nav]').forEach(function (a) {
      const p = a.dataset.nav;
      const on = path === p || (p !== '/dashboard' && path.indexOf(p + '/') === 0) || (p === '/cameras' && path.indexOf('/cameras') === 0);
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    if (path.indexOf('/detections/') === 0) { const g = document.getElementById('nav-detections'); g.classList.add('open'); document.getElementById('nav-det-toggle').setAttribute('aria-expanded', 'true'); }
  }
  function setSidebar(open) {
    document.getElementById('app').classList.toggle('nav-open', open);
    document.getElementById('menu-toggle').setAttribute('aria-expanded', open);
  }

  // ---------- navigation: drag to resize, hide / show (desktop); both remembered in this browser ----------
  const NAV_W_KEY = 'aiv.navWidth', NAV_HIDE_KEY = 'aiv.navHidden', NAV_MIN = 200, NAV_MAX = 420, NAV_DEF = 248;
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  function toggleNavHidden(force) {
    const app = document.getElementById('app');
    const hide = typeof force === 'boolean' ? force : !app.classList.contains('nav-hidden');
    app.classList.toggle('nav-hidden', hide);
    const b = document.getElementById('menu-toggle');
    b.setAttribute('aria-expanded', String(!hide)); b.title = hide ? 'Show navigation (Ctrl+B)' : 'Hide navigation (Ctrl+B)';
    lsSet(NAV_HIDE_KEY, hide ? '1' : '0');
  }
  function initNavResize() {
    const app = document.getElementById('app'), bar = document.getElementById('sidebar-split');
    const clamp = function (w) { return Math.round(Math.max(NAV_MIN, Math.min(NAV_MAX, w))); };
    let w = clamp(parseInt(lsGet(NAV_W_KEY), 10) || NAV_DEF);
    const apply = function () { app.style.setProperty('--sidebar-w', w + 'px'); bar.setAttribute('aria-valuenow', String(w)); };
    bar.setAttribute('aria-valuemin', String(NAV_MIN)); bar.setAttribute('aria-valuemax', String(NAV_MAX)); apply();
    if (lsGet(NAV_HIDE_KEY) === '1' && window.innerWidth > 1024) toggleNavHidden(true); else toggleNavHidden(false);
    bar.addEventListener('pointerdown', function (ev) {
      ev.preventDefault();
      const x0 = ev.clientX, w0 = w;
      bar.setPointerCapture(ev.pointerId); app.classList.add('is-resizing');
      const mv = function (e) { w = clamp(w0 + e.clientX - x0); apply(); };
      const up = function () { bar.removeEventListener('pointermove', mv); bar.removeEventListener('pointerup', up); bar.removeEventListener('pointercancel', up); app.classList.remove('is-resizing'); lsSet(NAV_W_KEY, String(w)); };
      bar.addEventListener('pointermove', mv); bar.addEventListener('pointerup', up); bar.addEventListener('pointercancel', up);
    });
    bar.addEventListener('keydown', function (e) {
      const step = e.shiftKey ? 40 : 10;
      if (e.key === 'ArrowRight') w = clamp(w + step); else if (e.key === 'ArrowLeft') w = clamp(w - step); else return;
      e.preventDefault(); apply(); lsSet(NAV_W_KEY, String(w));
    });
    bar.addEventListener('dblclick', function () { w = NAV_DEF; apply(); lsSet(NAV_W_KEY, String(w)); });
  }

  // ---------- popovers ----------
  function closePopovers(except) {
    ['notif-panel', 'profile-menu'].forEach(function (id) {
      if (id === except) return;
      const el = document.getElementById(id); el.classList.remove('open');
    });
    if (except !== 'notif-panel') document.getElementById('notif-btn').setAttribute('aria-expanded', 'false');
    if (except !== 'profile-menu') document.getElementById('profile-btn').setAttribute('aria-expanded', 'false');
  }
  function togglePopover(id, btnId) {
    const el = document.getElementById(id), open = !el.classList.contains('open');
    closePopovers(id);
    el.classList.toggle('open', open);
    document.getElementById(btnId).setAttribute('aria-expanded', open);
    return open;
  }

  function renderNotifications() {
    const el = document.getElementById('notif-panel');
    const open = state.alerts().filter(function (a) { return a.status !== 'acknowledged'; });
    let html = '<div class="row" style="padding:12px 14px;border-bottom:1px solid var(--border)"><b class="grow">Notifications</b><a href="#/alerts" class="btn btn-xs btn-ghost" data-close-pop>View all alerts</a></div><div style="max-height:380px;overflow:auto;padding:6px">';
    if (!api.status.connected && api.status.checked) html += '<div class="notice notice-warning" style="margin:6px">' + icon('server', 16) + '<div>Backend not connected — live detections and alerts are not being received.</div></div>';
    if (!open.length) html += ui.emptyState('checkCircle', 'All caught up', 'No open alerts.');
    open.slice(0, 8).forEach(function (a) {
      const t = ui.eventType(a.type);
      html += '<button class="menu-item" data-open-alert="' + esc(a.id) + '" style="align-items:flex-start"><span style="margin-top:2px">' + ui.sevBadge(a.severity) + '</span><span class="grow" style="min-width:0"><b style="display:block">' + esc(ui.eventLabel(a)) + '</b><span class="dim ellipsis" style="display:block;font-size:12px">' + esc(state.cameraName(a.cameraId)) + ' · ' + esc(ui.relTime(a.timestamp)) + '</span></span></button>';
    });
    if (open.length > 8) html += '<p class="dim" style="font-size:12px;padding:6px 10px">+' + (open.length - 8) + ' more open alerts</p>';
    el.innerHTML = html + '</div>';
    el.querySelectorAll('[data-open-alert]').forEach(function (b) { b.addEventListener('click', function () { closePopovers(); AV.events.openAlert(b.dataset.openAlert); }); });
    el.querySelectorAll('[data-close-pop]').forEach(function (b) { b.addEventListener('click', function () { closePopovers(); }); });
  }

  // ---------- global search ----------
  let hits = [], hitIdx = -1;
  function search(q) {
    q = q.trim().toLowerCase();
    const res = document.getElementById('search-results'), input = document.getElementById('global-search');
    if (!q) { res.classList.remove('open'); input.setAttribute('aria-expanded', 'false'); return; }
    const m = function (s) { return String(s || '').toLowerCase().indexOf(q) >= 0; };
    const groups = [
      ['Cameras', state.cameras().filter(function (c) { return m(c.name) || m(c.id) || m(c.location) || m(c.brand); }).slice(0, 5).map(function (c) { return { ic: 'camera', t: c.name, s: c.id + ' · ' + (c.location || '') + ' · ' + ui.STATUS[ui.camStatus(c)].label, go: function () { router.go('/cameras/' + encodeURIComponent(c.id)); } }; })],
      ['Detections', state.detections().filter(function (d) { return m(ui.eventLabel(d)) || m(d.id) || m(state.cameraName(d.cameraId)); }).slice(0, 5).map(function (d) { return { ic: 'scan', t: ui.eventLabel(d), s: state.cameraName(d.cameraId) + ' · ' + ui.fmtTime(d.timestamp), go: function () { AV.events.openDetection(d); } }; })],
      ['Alerts', state.alerts().filter(function (a) { return m(ui.eventLabel(a)) || m(a.id) || m(a.severity) || m(state.cameraName(a.cameraId)); }).slice(0, 5).map(function (a) { return { ic: 'bell', t: ui.eventLabel(a) + ' · ' + ui.cap(a.severity), s: state.cameraName(a.cameraId) + ' · ' + ui.fmtTime(a.timestamp), go: function () { AV.events.openAlert(a.id); } }; })],
      ['Snapshots', state.snapshots().filter(function (s) { return m(s.id) || m(s.eventId) || m(state.cameraName(s.cameraId)) || m(ui.eventType(s.type).label); }).slice(0, 5).map(function (s) { return { ic: 'image', t: s.id + ' · ' + ui.eventType(s.type).label, s: state.cameraName(s.cameraId) + ' · ' + ui.fmtTime(s.timestamp), go: function () { AV.snapshotsUi.openPreview(s); } }; })]
    ];
    hits = []; hitIdx = -1;
    let html = '';
    groups.forEach(function (g) {
      if (!g[1].length) return;
      html += '<div class="search-group">' + g[0] + '</div>';
      g[1].forEach(function (h) { hits.push(h); html += '<button class="search-hit" role="option" data-hit="' + (hits.length - 1) + '">' + icon(h.ic, 16) + '<span class="grow" style="min-width:0"><span class="ellipsis" style="display:block">' + esc(h.t) + '</span><small class="ellipsis">' + esc(h.s) + '</small></span></button>'; });
    });
    res.innerHTML = html || '<div class="empty" style="padding:18px">No matches for "' + esc(q) + '"</div>';
    res.classList.add('open'); input.setAttribute('aria-expanded', 'true');
    res.querySelectorAll('[data-hit]').forEach(function (b) { b.addEventListener('mousedown', function (e) { e.preventDefault(); pick(+b.dataset.hit); }); });
  }
  function pick(i) {
    const h = hits[i]; if (!h) return;
    const input = document.getElementById('global-search');
    input.value = ''; search(''); input.blur();
    h.go();
  }
  function moveHit(d) {
    if (!hits.length) return;
    hitIdx = (hitIdx + d + hits.length) % hits.length;
    document.querySelectorAll('.search-hit').forEach(function (b, i) { b.classList.toggle('active', i === hitIdx); if (i === hitIdx) b.scrollIntoView({ block: 'nearest' }); });
  }

  // ---------- dialogs ----------
  function openSystemStatus() {
    const cams = state.cameras();
    ui.modal({ title: 'System status', icon: 'activity', size: 'sm',
      body: '<dl class="kv"><dt>Backend</dt><dd>' + (api.status.connected ? '<span class="badge badge-success">Connected</span>' : '<span class="badge badge-warning">Not connected</span>') + '</dd><dt>API base</dt><dd class="mono">' + esc(api.base()) + '</dd>' +
        '<dt>Last check</dt><dd>' + esc(api.status.checkedAt ? ui.fmtTime(api.status.checkedAt) : '—') + '</dd>' + (api.status.error ? '<dt>Error</dt><dd>' + esc(api.status.error) + '</dd>' : '') +
        '<dt>Cameras</dt><dd>' + cams.length + ' configured · ' + cams.filter(function (c) { return ui.camStatus(c) === 'online'; }).length + ' online' + '</dd>' +
        '<dt>Open alerts</dt><dd>' + state.unackCount() + '</dd><dt>Data source</dt><dd>' + (api.status.connected ? 'Backend' : 'Backend not connected') + '</dd>' +
        '<dt>Local storage</dt><dd>' + (AV.storage.isAvailable() ? 'Available' : 'Blocked — in-memory only') + '</dd></dl>',
      actions: [{ label: 'Re-check now', icon: 'refresh', cls: 'btn-ghost', close: false, onClick: function (m, b) { ui.setBusy(b, true, 'Checking…'); checkBackend(true).then(function () { m.close(); openSystemStatus(); }); return false; } }, { label: 'Backend settings', cls: 'btn-primary', onClick: function () { router.go('/settings'); } }] });
  }
  function openShortcuts() {
    ui.modal({ title: 'Keyboard shortcuts', icon: 'keyboard', size: 'sm',
      body: '<dl class="kv"><dt><span class="mono">Ctrl + K</span></dt><dd>Focus global search</dd><dt><span class="mono">↑ / ↓ / Enter</span></dt><dd>Navigate search results</dd><dt><span class="mono">Esc</span></dt><dd>Close dialogs, menus, cancel polygon</dd>' +
        '<dt><span class="mono">Enter</span></dt><dd>Finish polygon (zone editor)</dd><dt><span class="mono">Ctrl + Z</span></dt><dd>Undo last zone edit</dd><dt><span class="mono">Delete</span></dt><dd>Remove selected zone</dd><dt><span class="mono">Arrows</span></dt><dd>Nudge selected zone (Shift = larger)</dd></dl>',
      actions: [{ label: 'Close', cls: 'btn-primary' }] });
  }
  function openAbout() {
    ui.modal({ title: 'About AI VISION', icon: 'logo', size: 'sm',
      body: '<p class="muted" style="line-height:1.6">PYROTECH VISION is the operator console for an AI camera detection system. This frontend manages cameras, detection modules, zones, alerts and snapshots. Video decoding, inference, recording and credential storage are performed by the backend.</p><dl class="kv mt-16"><dt>Frontend</dt><dd>1.0.0 (vanilla JS, no build step)</dd><dt>Backend</dt><dd>' + esc(api.status.info && api.status.info.version ? api.status.info.version : (api.status.connected ? 'Connected' : 'Not connected')) + '</dd></dl>',
      actions: [{ label: 'Close', cls: 'btn-primary' }] });
  }

  // ---------- bind shell ----------
  function bindShell() {
    AV.icons.hydrate(document.body);
    document.getElementById('nav-det-toggle').addEventListener('click', function () {
      const g = document.getElementById('nav-detections'); const open = !g.classList.contains('open');
      g.classList.toggle('open', open); this.setAttribute('aria-expanded', open); state.setPref('navDetectionsOpen', open);
    });
    initNavResize();
    document.getElementById('menu-toggle').addEventListener('click', function () {
      if (window.innerWidth > 1024) toggleNavHidden(); else setSidebar(!document.getElementById('app').classList.contains('nav-open'));
    });
    document.addEventListener('keydown', function (e) { if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b' && window.innerWidth > 1024) { e.preventDefault(); toggleNavHidden(); } });
    document.getElementById('sidebar-backdrop').addEventListener('click', function () { setSidebar(false); });
    document.getElementById('sidebar').addEventListener('click', function (e) { if (e.target.closest('a.nav-item')) setSidebar(false); });
    document.getElementById('system-status-card').addEventListener('click', openSystemStatus);
    document.getElementById('conn-pill').addEventListener('click', openSystemStatus);
    document.getElementById('theme-btn').addEventListener('click', function () { setTheme(state.prefs().theme === 'light' ? 'dark' : 'light'); });
    document.getElementById('notif-btn').addEventListener('click', function (e) { e.stopPropagation(); if (togglePopover('notif-panel', 'notif-btn')) renderNotifications(); });
    document.getElementById('profile-btn').addEventListener('click', function (e) { e.stopPropagation(); if (togglePopover('profile-menu', 'profile-btn')) document.querySelector('#profile-menu .menu-item').focus(); });
    document.getElementById('profile-menu').addEventListener('click', function (e) {
      const b = e.target.closest('[data-act]'); if (!b) return;
      closePopovers();
      const a = b.dataset.act;
      if (a === 'profile') AV.settingsUi.openProfile();
      if (a === 'prefs') router.go('/settings');
      if (a === 'shortcuts') openShortcuts();
      if (a === 'about') openAbout();
    });
    document.getElementById('profile-menu').addEventListener('keydown', function (e) {
      const items = Array.prototype.slice.call(this.querySelectorAll('.menu-item')); const i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    });
    document.addEventListener('click', function (e) { if (!e.target.closest('.popover') && !e.target.closest('#notif-btn') && !e.target.closest('#profile-btn')) closePopovers(); });

    const input = document.getElementById('global-search');
    input.addEventListener('input', ui.debounce(function () { search(input.value); }, 120));
    input.addEventListener('focus', function () { if (input.value) search(input.value); });
    input.addEventListener('blur', function () { setTimeout(function () { document.getElementById('search-results').classList.remove('open'); input.setAttribute('aria-expanded', 'false'); }, 150); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); moveHit(1); }
      if (e.key === 'ArrowUp') { e.preventDefault(); moveHit(-1); }
      if (e.key === 'Enter') { e.preventDefault(); pick(hitIdx >= 0 ? hitIdx : 0); }
      if (e.key === 'Escape') { input.value = ''; search(''); input.blur(); }
    });
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); input.focus(); input.select(); }
      if (e.key === 'Escape' && !ui.openModalCount()) { closePopovers(); setSidebar(false); }
    });
    document.addEventListener('av:route', function (e) {
      highlightNav(e.detail.path);
      document.title = (e.detail.page.title || 'AI VISION') + ' · AI VISION';
      closePopovers();
    });
    state.on('alerts', updateBadges); state.on('all', updateBadges);
    // Camera IPs / statuses / active sessions: refresh every visible element in place.
    ['camstatus', 'metrics', 'cameras', 'backend'].forEach(function (evt) { state.on(evt, function () { ui.refreshLive(document); }); });
    window.addEventListener('beforeunload', function () { AV.media.release(); });
    document.addEventListener('visibilitychange', function () { if (!document.hidden && api.status.checked) checkBackend(false); });
  }

  function registerRoutes() {
    router.register('/dashboard', P.dashboard);
    router.register('/cameras', P.cameras);
    router.register('/cameras/new', P.cameraForm);
    router.register('/cameras/:id/edit', Object.assign({}, P.cameraForm, { title: 'Edit Camera' }));
    router.register('/cameras/:id', P.cameras);
    router.register('/live', P.live);
    router.register('/live/:id', P.live);
    // Human and Vehicle detection are wired to the real backend pipeline; other modules keep the generic page.
    router.register('/detections/vehicle', Object.assign({}, P.detectionLive, { title: 'Vehicle Detection', render: function (c) { return P.detectionLive.render(c, { module: 'vehicle' }); } }));
    router.register('/detections/count', P.humanCount);
    router.register('/detections/fire', Object.assign({}, P.detectionLive, { title: 'Fire & Smoke Detection', render: function (c) { return P.detectionLive.render(c, { module: 'fire' }); } }));
    router.register('/detections/object', P.objectMonitor);
    router.register('/detections/ppe', Object.assign({}, P.detectionLive, { title: 'PPE Detection', render: function (c) { return P.detectionLive.render(c, { module: 'ppe' }); } }));
    router.register('/detections/twhelmet', Object.assign({}, P.detectionLive, { title: 'Two-Wheeler Helmet (Line)', render: function (c) { return P.detectionLive.render(c, { module: 'twhelmet' }); } }));
    router.register('/detections/pcross', Object.assign({}, P.detectionLive, { title: 'Person Line Crossing & Counting', render: function (c) { return P.detectionLive.render(c, { module: 'pcross' }); } }));
    router.register('/detections/human', Object.assign({}, P.detectionLive, { title: 'Human Detection', render: function (c) { return P.detectionLive.render(c, { module: 'human' }); } }));
    router.register('/detections/:module', P.detections);
    router.register('/alerts', P.alerts);
    router.register('/snapshots', P.snapshots);
    router.register('/recordings', P.recordings);
    router.register('/reports', P.reports);
    router.register('/models', P.models);
    router.register('/settings', P.settings);
    router.register('/not-found', P.notFound);
  }

  function init() {
    state.load();
    applyPrefs();
    bindShell();
    registerRoutes();
    updateBadges();
    updateConn();
    router.start();
    checkBackend(true).then(startHealthPolling);
  }

  AV.app = { checkBackend: checkBackend, setTheme: setTheme, applyPrefs: applyPrefs, applyMotion: applyMotion };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
