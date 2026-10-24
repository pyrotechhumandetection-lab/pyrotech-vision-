/* PYROTECH VISION — Dashboard (Reference image 1). */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog, media = window.AV.media;
  const esc = ui.esc;
  const DASH_MODULES = ['helmet', 'ppe', 'fire', 'human', 'vehicle', 'count', 'twhelmet', 'pcross', 'object', 'custom'];

  let root = null, unsub = [], metricsTimer = null, fpsHistory = [], detTab = 'all', detPage = 0, alertSev = 'all';

  function render(container) {
    root = container;
    container.innerHTML =
      '<div class="page-head"><div><h1>Dashboard</h1><p class="sub">Monitor cameras, AI detections, and system activity from one place.</p></div>' +
      '<div class="actions"><button class="btn" id="dash-refresh"><i data-icon="refresh" data-size="16"></i><span>Refresh</span></button>' +
      '<a class="btn btn-primary" href="#/cameras/new"><i data-icon="plus" data-size="16"></i><span>Add Camera</span></a></div></div>' +
      '<div id="dash-mode"></div>' +
      '<section class="stats-row" id="dash-stats" aria-label="Summary statistics"></section>' +
      '<div class="dash-grid">' +
      '  <section class="card dash-feeds" id="dash-feeds" aria-labelledby="feeds-h"></section>' +
      '  <section class="card dash-modules" id="dash-modules" aria-labelledby="mods-h"></section>' +
      '  <section class="card dash-dets" id="dash-dets" aria-labelledby="dets-h"></section>' +
      '  <section class="card dash-alerts" id="dash-alerts" aria-labelledby="alerts-h"></section>' +
      '  <section class="card dash-perf" id="dash-perf" aria-labelledby="perf-h"></section>' +
      '  <section class="card dash-storage" id="dash-storage" aria-labelledby="stor-h"></section>' +
      '</div>';
    container.querySelector('#dash-refresh').addEventListener('click', onRefresh);
    renderAll();
    unsub.push(state.on('cameras', function () { renderStats(); renderFeeds(); renderPerf(); }));
    // Status-only change: update counters and tile captions in place (live streams keep running).
    unsub.push(state.on('camstatus', function () { if (!root) return; renderStats(); renderPerf(); updateTiles(); }));
    unsub.push(state.on('alerts', function () { renderStats(); renderAlerts(); }));
    unsub.push(state.on('snapshots', function () { renderStats(); renderStorage(); }));
    unsub.push(state.on('modules', renderModules));
    unsub.push(state.on('detections', function () { renderDets(); renderStats(); }));
    unsub.push(state.on('backend', renderAll));
    unsub.push(state.on('all', renderAll));
    unsub.push(state.on('metrics', function () { renderPerf(); }));
    startMetrics();
  }

  function renderAll() {
    if (!root) return;
    renderMode(); renderStats(); renderFeeds(); renderModules(); renderDets(); renderAlerts(); renderPerf(); renderStorage();
  }

  function renderMode() {
    const el = root.querySelector('#dash-mode');
    el.innerHTML = api.status.connected ? '' : '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div><b>' + (api.status.checked ? 'Backend not connected.' : 'Checking backend…') + '</b> ' +
      'Camera status, live video, detections, alerts, snapshots and system metrics come from the camera backend only and appear once it is reachable. <a href="#/settings">Configure backend</a></div></div>';
  }

  // ---------- statistics ----------
  function renderStats() {
    const cams = state.cameras();
    const online = cams.filter(function (c) { return ui.camStatus(c) === 'online'; }).length;
    const offline = cams.filter(function (c) { const s = ui.camStatus(c); return s === 'offline' || s === 'error'; }).length;
    const unver = cams.filter(function (c) { const s = ui.camStatus(c); return s === 'unverified' || s === 'disabled'; }).length;
    const alerts = state.alerts(), unack = state.unackCount();
    const snaps = state.remote.storage && typeof state.remote.storage.snapshotCount === 'number' ? state.remote.storage.snapshotCount : state.snapshots().length;
    const pct = function (n) { return cams.length ? Math.round(n / cams.length * 100) + '%' : '0%'; };
    const card = function (ic, tone, n, label, trend, trendTone, href) {
      return '<a class="stat-card card lift" href="' + href + '"><span class="stat-icon tone-' + tone + '">' + icon(ic, 26) + '</span>' +
        '<span class="stat-body"><span class="stat-num">' + n + '</span><span class="stat-label">' + label + '</span></span>' +
        (trend ? '<span class="stat-trend t-' + trendTone + '">' + trend + '</span>' : '') + '</a>';
    };
    root.querySelector('#dash-stats').innerHTML =
      card('camera', 'purple', cams.length, 'Total Cameras', unver ? unver + ' not verified' : '', 'muted', '#/cameras') +
      card('wifi', 'success', online, 'Online', pct(online), 'success', '#/cameras?status=online') +
      card('wifiOff', 'danger', offline, 'Offline / Error', pct(offline), 'danger', '#/cameras?status=offline') +
      card('bell', 'warning', alerts.length, 'Total Alerts', unack ? unack + ' open' : 'none open', unack ? 'danger' : 'muted', '#/alerts') +
      card('image', 'primary', snaps, 'Snapshots Saved', '', 'muted', '#/snapshots');
  }

  // ---------- live feeds ----------
  function renderFeeds() {
    const el = root.querySelector('#dash-feeds');
    media.release(el);
    const p = state.prefs();
    const grid = p.dashGrid || 3, filter = p.dashFilter || 'all';
    let cams = state.cameras();
    if (filter === 'online') cams = cams.filter(function (c) { return ui.camStatus(c) === 'online'; });
    if (filter === 'offline') cams = cams.filter(function (c) { const s = ui.camStatus(c); return s === 'offline' || s === 'error'; });
    if (filter === 'detecting') cams = cams.filter(function (c) { return enabledMods(c).length; });
    const limit = grid * 2;
    const shown = cams.slice(0, limit);
    el.innerHTML =
      '<div class="card-head"><h2 id="feeds-h"><span class="dot dot-success"></span>Live Camera Feeds</h2><span class="spacer"></span>' +
      '<label class="sr-only" for="feed-filter">Filter cameras</label><select class="select select-sm" id="feed-filter" style="width:auto">' +
      [['all', 'All cameras'], ['online', 'Online'], ['offline', 'Offline / error'], ['detecting', 'With detection enabled']].map(function (o) { return '<option value="' + o[0] + '"' + (filter === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>' +
      '<div class="segmented" role="group" aria-label="Grid size">' + [2, 3, 4].map(function (n) { return '<button type="button" data-grid="' + n + '" aria-pressed="' + (grid === n) + '" title="' + n + ' columns">' + n + '×</button>'; }).join('') + '</div>' +
      '<a class="btn btn-sm btn-ghost" href="#/live">View All</a></div>' +
      (shown.length ? '<div class="feed-grid" style="--cols:' + grid + '">' + shown.map(tile).join('') + '</div>' +
        (cams.length > limit ? '<p class="muted mt-12" style="font-size:12.5px">Showing ' + limit + ' of ' + cams.length + ' cameras. <a href="#/live">Open Live View</a> to see more.</p>' : '')
        : ui.emptyState('camera', cams.length === 0 && state.cameras().length === 0 ? 'No cameras configured' : 'No cameras match this filter',
          state.cameras().length === 0 ? 'Add your first camera to start monitoring. Connection testing and live video require the camera backend.' : 'Change the filter to see other cameras.',
          '<a class="btn btn-primary btn-sm" href="#/cameras/new">' + icon('plus', 16) + 'Add Camera</a>'));
    window.AV.icons.hydrate(el);
    el.querySelector('#feed-filter').addEventListener('change', function (e) { state.setPref('dashFilter', e.target.value); renderFeeds(); });
    el.querySelectorAll('[data-grid]').forEach(function (b) { b.addEventListener('click', function () { state.setPref('dashGrid', parseInt(b.dataset.grid, 10)); renderFeeds(); }); });
    el.querySelectorAll('[data-act]').forEach(function (b) {
      b.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        const cam = state.getCamera(b.dataset.id);
        if (!cam) return;
        if (b.dataset.act === 'snap') media.captureSnapshot(cam, b);
        if (b.dataset.act === 'full') ui.fullscreen(b.closest('.feed-tile').querySelector('.feed-media'));
        if (b.dataset.act === 'open') window.AV.router.go('/live/' + encodeURIComponent(cam.id));
      });
    });
    media.attach(el);
    media.alignOverlays(el);
  }

  function tileTime(cam) {
    return ui.camStatus(cam) === 'online' && cam.lastConnectedAt ? 'Last connected ' + ui.fmtTime(cam.lastConnectedAt) : 'No frame received';
  }
  function updateTiles() {
    root.querySelectorAll('#dash-feeds .feed-tile[data-cam]').forEach(function (t) {
      const cam = state.getCamera(t.dataset.cam);
      if (!cam) return;
      const st = ui.STATUS[ui.camStatus(cam)];
      const dot = t.querySelector('.feed-top .dot'), time = t.querySelector('.feed-time');
      if (dot) { dot.className = 'dot ' + st.dot; dot.title = st.label; }
      if (time) time.textContent = tileTime(cam);
    });
  }

  function enabledMods(cam) { return Object.keys(cam.detection.modules).filter(function (k) { return cam.detection.modules[k].enabled; }); }

  function tile(cam) {
    const st = ui.camStatus(cam);
    const mods = enabledMods(cam);
    const time = tileTime(cam);
    return '<article class="feed-tile" data-cam="' + esc(cam.id) + '" aria-label="' + esc(cam.name) + '">' +
      '<div class="feed-media">' + media.mediaHtml(cam, { overlays: state.prefs().showOverlays }) +
      '<div class="feed-top"><span class="dot ' + ui.STATUS[st].dot + '" title="' + ui.STATUS[st].label + '"></span><span class="ellipsis">' + esc(cam.name) + '</span></div>' +
      '<div class="feed-bottom"><span class="feed-time">' + esc(time) + '</span><span class="row" style="gap:6px">' +
      '<button class="feed-btn" data-act="snap" data-id="' + esc(cam.id) + '" aria-label="Capture snapshot from ' + esc(cam.name) + '" title="Capture snapshot">' + icon('snapshot', 16) + '</button>' +
      '<button class="feed-btn" data-act="open" data-id="' + esc(cam.id) + '" aria-label="Open ' + esc(cam.name) + ' in Live View" title="Open camera">' + icon('external', 16) + '</button>' +
      '<button class="feed-btn" data-act="full" data-id="' + esc(cam.id) + '" aria-label="Full screen ' + esc(cam.name) + '" title="Full screen">' + icon('maximize', 16) + '</button></span></div></div>' +
      '<div class="feed-meta"><span class="feed-id"><span class="mono dim">' + esc(cam.id) + '</span>' + ui.ipHtml(cam) + '</span><span class="row" style="gap:4px;flex-wrap:wrap;justify-content:flex-end">' +
      (mods.length ? mods.slice(0, 4).map(function (m) { const mm = cat.moduleById(m); return '<span class="mod-chip" title="' + esc(mm.name) + ' enabled" style="color:' + mm.color + '">' + icon(mm.icon, 13) + '</span>'; }).join('') : '<span class="dim" style="font-size:12px">No detection</span>') +
      '<a class="btn btn-xs btn-ghost" href="#/cameras/' + encodeURIComponent(cam.id) + '" aria-label="Details for ' + esc(cam.name) + '">Details</a></span></div></article>';
  }

  // ---------- modules ----------
  function renderModules() {
    const el = root.querySelector('#dash-modules');
    const g = state.globalModules();
    el.innerHTML = '<div class="card-head"><h2 id="mods-h">Detection Modules</h2><span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/models">Manage</a></div>' +
      '<div class="module-grid">' + DASH_MODULES.map(function (id) {
        const m = cat.moduleById(id), on = g[id] && g[id].enabled;
        return '<div class="module-card' + (on ? ' on' : '') + '"><div class="row" style="justify-content:space-between;align-items:flex-start"><span class="mod-icon" style="color:' + m.color + '">' + icon(m.icon, 26) + '</span>' +
          ui.switchHtml('gm-' + id, on, 'Enable ' + m.name + ' in the interface', { attrs: 'data-mod="' + id + '"' }) + '</div>' +
          '<h3>' + esc(m.name) + '</h3><p>' + esc(m.desc) + '</p><div class="row" style="justify-content:space-between;margin-top:auto;gap:6px">' + window.AV.modules.runtimeBadge(id) +
          '<button class="btn btn-xs btn-ghost btn-icon" data-mset="' + id + '" aria-label="' + esc(m.name) + ' settings" title="Settings">' + icon('settings', 14) + '</button></div></div>';
      }).join('') + '</div>' +
      '<p class="dim mt-12" style="font-size:12px">Toggles enable a module in the interface. A module only analyses video once the backend reports its model as running.</p>';
    el.querySelectorAll('[data-mod]').forEach(function (inp) {
      inp.addEventListener('change', function () {
        const m = cat.moduleById(inp.dataset.mod);
        state.setGlobalModule(m.id, inp.checked);
        const rs = window.AV.modules.runtimeStatus(m.id);
        ui.toast(inp.checked ? 'success' : 'info', m.name + (inp.checked ? ' enabled' : ' disabled'), 'Interface setting saved. Model status: ' + rs.text + '.');
      });
    });
    el.querySelectorAll('[data-mset]').forEach(function (b) { b.addEventListener('click', function () { openModulePanel(b.dataset.mset); }); });
  }

  /** Global module panel: status, enable switch, cameras using the module. */
  function openModulePanel(id) {
    const m = cat.moduleById(id), g = state.globalModules();
    const cams = state.cameras().filter(function (c) { return c.detection.modules[id] && c.detection.modules[id].enabled; });
    const rs = window.AV.modules.runtimeStatus(id);
    const md = ui.modal({
      title: m.long, icon: m.icon, size: 'lg',
      body: '<p class="muted">' + esc(m.desc) + '</p><dl class="kv mt-16"><dt>Interface state</dt><dd>' + ui.switchHtml('mp-en', g[id] && g[id].enabled, 'Enable ' + m.name, { stateText: true }) + '</dd>' +
        '<dt>Model status</dt><dd>' + window.AV.modules.runtimeBadge(id) + ' <span class="muted" style="font-size:12.5px">' + esc(rs.text) + '</span></dd>' +
        '<dt>Classes</dt><dd>' + esc((state.moduleRuntime(id) && state.moduleRuntime(id).classes || m.classes).join(', ') || 'Defined by the selected model') + '</dd></dl>' +
        '<div class="divider"></div><h3 style="font-size:14.5px;margin-bottom:10px">Cameras using this module (' + cams.length + ')</h3>' +
        (cams.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>Camera</th><th>Confidence</th><th>Zones</th><th></th></tr></thead><tbody>' + cams.map(function (c) {
          const mc = c.detection.modules[id];
          return '<tr><td>' + esc(c.name) + '</td><td>' + Number(mc.confidence).toFixed(2) + '</td><td>' + ((mc.zones || []).length || 'Full frame') + '</td><td style="text-align:right"><a class="btn btn-xs" href="#/cameras/' + encodeURIComponent(c.id) + '/edit">Configure</a></td></tr>';
        }).join('') + '</tbody></table></div>' : '<p class="muted">No camera has this module enabled. Enable it per camera on the Add / Edit Camera page.</p>'),
      actions: [{ label: 'Close', cls: 'btn-ghost' }, { label: 'Open module page', cls: 'btn-primary', onClick: function () { window.AV.router.go('/detections/' + id); } }]
    });
    md.body.querySelector('#mp-en').addEventListener('change', function (e) {
      state.setGlobalModule(id, e.target.checked);
      e.target.closest('.switch').querySelector('.state-text').textContent = e.target.checked ? 'On' : 'Off';
      ui.toast('success', m.name + (e.target.checked ? ' enabled' : ' disabled'), 'Interface setting saved.');
    });
    md.el.querySelectorAll('a[href^="#"]').forEach(function (a) { a.addEventListener('click', function () { md.close(); }); });
  }

  // ---------- recent detections ----------
  function renderDets() {
    const el = root.querySelector('#dash-dets');
    const tabs = [['all', 'All'], ['helmet', 'Helmet'], ['ppe', 'PPE'], ['fire', 'Fire & Smoke'], ['human', 'Human'], ['vehicle', 'Vehicle'], ['twhelmet', 'Two-wheeler helmet'], ['pcross', 'Line crossing'], ['object', 'Object']];
    let list = state.detections();
    if (detTab !== 'all') list = list.filter(function (d) { return ui.eventType(d.type).module === detTab; });
    const per = 5, pages = Math.max(1, Math.ceil(list.length / per));
    detPage = Math.min(detPage, pages - 1);
    const pageItems = list.slice(detPage * per, detPage * per + per);
    el.innerHTML = '<div class="card-head"><h2 id="dets-h">' + icon('scan', 18) + 'Recent Detections</h2><span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/detections/all">View All</a></div>' +
      '<div class="tabs mb-12" role="tablist" aria-label="Filter detections">' + tabs.map(function (t) { return '<button class="tab" role="tab" aria-selected="' + (detTab === t[0]) + '" data-tab="' + t[0] + '">' + t[1] + '</button>'; }).join('') + '</div>' +
      (pageItems.length ? '<div class="det-row">' + pageItems.map(detCard).join('') + '</div>' +
        '<div class="row mt-12" style="justify-content:center;gap:6px"><button class="btn btn-xs btn-ghost" data-dp="-1" ' + (detPage === 0 ? 'disabled' : '') + ' aria-label="Previous detections">' + icon('chevronLeft', 14) + '</button>' +
        Array.from({ length: pages }).map(function (_, i) { return '<span class="pg-dot' + (i === detPage ? ' on' : '') + '"></span>'; }).join('') +
        '<button class="btn btn-xs btn-ghost" data-dp="1" ' + (detPage >= pages - 1 ? 'disabled' : '') + ' aria-label="Next detections">' + icon('chevronRight', 14) + '</button></div>'
        : ui.emptyState('scan', 'No detections', api.status.connected ? 'The inference service has not reported any detections for this filter.' : 'Detection events are produced by the inference backend. None are available for this filter.'));
    el.querySelectorAll('[data-tab]').forEach(function (b) { b.addEventListener('click', function () { detTab = b.dataset.tab; detPage = 0; renderDets(); }); });
    el.querySelectorAll('[data-dp]').forEach(function (b) { b.addEventListener('click', function () { detPage += parseInt(b.dataset.dp, 10); renderDets(); }); });
    el.querySelectorAll('[data-det]').forEach(function (b) { b.addEventListener('click', function () { const d = state.detections().find(function (x) { return x.id === b.dataset.det; }); if (d) window.AV.events.openDetection(d); }); });
  }
  function detCard(d) {
    const t = ui.eventType(d.type);
    return '<button class="det-card" data-det="' + esc(d.id) + '" aria-label="' + esc(ui.eventLabel(d) + ' on ' + state.cameraName(d.cameraId)) + '">' +
      '<span class="det-img">' + (d.image ? '<img src="' + esc(d.image) + '" alt="" loading="lazy">' : '<span class="media-ph">' + icon('image', 22) + '</span>') +
      '<span class="det-label">' + ui.toneBadge(t.tone, d.label || t.label) + '</span>' + '</span>' +
      '<span class="det-info"><b class="ellipsis">' + esc(state.cameraName(d.cameraId)) + '</b><span>' + esc(ui.fmtTime(d.timestamp)) + '</span><span>Confidence: ' + ui.confText(d) + '</span></span></button>';
  }

  // ---------- recent alerts ----------
  function renderAlerts() {
    const el = root.querySelector('#dash-alerts');
    let list = state.alerts();
    if (alertSev !== 'all') list = list.filter(function (a) { return a.severity === alertSev; });
    el.innerHTML = '<div class="card-head"><h2 id="alerts-h">Recent Alerts</h2><span class="spacer"></span>' +
      '<label class="sr-only" for="alert-sev">Severity filter</label><select class="select select-sm" id="alert-sev" style="width:auto">' +
      ['all', 'critical', 'high', 'medium', 'low'].map(function (s) { return '<option value="' + s + '"' + (alertSev === s ? ' selected' : '') + '>' + (s === 'all' ? 'All severities' : ui.cap(s)) + '</option>'; }).join('') + '</select>' +
      '<a class="btn btn-sm btn-ghost" href="#/alerts">View All</a></div>' +
      (list.length ? '<ul class="alert-list">' + list.slice(0, 5).map(function (a) {
        const t = ui.eventType(a.type), m = cat.moduleById(t.module) || cat.MODULES[0];
        return '<li><button class="alert-item sev-' + esc(a.severity) + (a.status === 'acknowledged' ? ' acked' : '') + '" data-alert="' + esc(a.id) + '">' +
          '<span class="alert-ic">' + icon(m.icon, 20) + '</span><span class="grow" style="min-width:0"><b class="ellipsis">' + esc(ui.eventLabel(a)) + '</b><span class="ellipsis">' + esc(state.cameraName(a.cameraId)) + '</span><span>' + esc(ui.fmtTime(a.timestamp)) + '</span></span>' +
          '<span class="col-end">' + ui.sevBadge(a.severity) + (a.status === 'acknowledged' ? '<span class="dim" style="font-size:11px">' + icon('check', 11) + ' Ack</span>' : '') + '</span>' +
          (a.image ? '<img class="alert-thumb" src="' + esc(a.image) + '" alt="" loading="lazy">' : '') + icon('chevronRight', 16) + '</button></li>';
      }).join('') + '</ul>' : ui.emptyState('bell', 'No alerts', alertSev === 'all' ? 'No alerts have been raised.' : 'No ' + alertSev + ' alerts.'));
    el.querySelector('#alert-sev').addEventListener('change', function (e) { alertSev = e.target.value; renderAlerts(); });
    el.querySelectorAll('[data-alert]').forEach(function (b) { b.addEventListener('click', function () { window.AV.events.openAlert(b.dataset.alert); }); });
  }

  // ---------- performance ----------
  function gauge(label, val, color) {
    const has = typeof val === 'number' && isFinite(val);
    const pct = has ? Math.max(0, Math.min(100, val)) : 0;
    const C = 2 * Math.PI * 34;
    return '<div class="gauge" role="img" aria-label="' + label + ' ' + (has ? Math.round(pct) + ' percent' : 'unavailable') + '"><svg viewBox="0 0 80 80" width="84" height="84"><circle cx="40" cy="40" r="34" fill="none" stroke="var(--border)" stroke-width="7"/>' +
      (has ? '<circle cx="40" cy="40" r="34" fill="none" stroke="' + color + '" stroke-width="7" stroke-linecap="round" stroke-dasharray="' + (C * pct / 100) + ' ' + C + '" transform="rotate(-90 40 40)"/>' : '') +
      '</svg><span class="gauge-in"><small>' + label + '</small><b>' + (has ? Math.round(pct) + '%' : '—') + '</b></span></div>';
  }
  function renderPerf() {
    const el = root.querySelector('#dash-perf');
    const m = state.remote.metrics;
    const cams = state.cameras();
    const onlineReal = cams.filter(function (c) { return ui.camStatus(c) === 'online'; }).length;
    let body;
    if (!m) {
      body = '<div class="perf-grid">' + gauge('CPU', null) + gauge('RAM', null) + gauge('GPU', null) +
        '<div class="perf-side"><div class="notice notice-info">' + icon('activity', 16) + '<div><b>Metrics unavailable.</b> ' +
        (api.status.connected ? 'The backend did not return /system/metrics' + (state.remote.errors.metrics ? ' (' + esc(state.remote.errors.metrics) + ')' : '') + '.' : 'Connect the monitoring backend to see CPU, RAM, GPU, inference latency and processing FPS.') +
        '</div></div><div class="perf-kv"><span>Inference latency</span><b>Unavailable</b><span>Processing FPS</span><b>Unavailable</b><span>Cameras verified online</span><b>' + onlineReal + ' / ' + cams.length + '</b></div></div></div>';
    } else {
      const fps = typeof m.fps === 'number' ? m.fps : null;
      if (fps !== null) { fpsHistory.push(fps); if (fpsHistory.length > 40) fpsHistory.shift(); }
      body = '<div class="perf-grid">' + gauge('CPU', m.cpu, '#13D99A') + gauge('RAM', m.ram, '#3478FF') + (typeof m.gpu === 'number' ? gauge('GPU', m.gpu, '#8057FF') : '<div class="gauge na"><span class="gauge-in"><small>GPU</small><b class="dim" style="font-size:12px">Not reported</b></span></div>') +
        '<div class="perf-side"><div class="row" style="justify-content:space-between"><span class="muted">AI Processing FPS</span><b style="color:var(--success)">' + (fps !== null ? fps.toFixed(1) + ' FPS' : 'Not reported') + '</b></div>' + spark(fpsHistory) +
        '<div class="perf-kv"><span>Camera FPS (received)</span><b>' + (typeof m.cameraFps === 'number' ? m.cameraFps.toFixed(1) + ' FPS' : 'Not reported') + '</b><span>Inference latency</span><b>' + (typeof m.latencyMs === 'number' ? m.latencyMs.toFixed(0) + ' ms' : 'Not reported') + '</b><span>Cameras online (backend)</span><b>' + (typeof m.camerasOnline === 'number' ? m.camerasOnline + ' / ' + (m.camerasTotal != null ? m.camerasTotal : '?') : onlineReal + ' / ' + cams.length) + '</b></div>' +
        '<span class="dim" style="font-size:11.5px">Live from /system/metrics · updated ' + ui.fmtTime(new Date().toISOString()) + '</span></div></div>';
    }
    el.innerHTML = '<div class="card-head"><h2 id="perf-h">System Performance</h2><span class="spacer"></span>' + (m ? '<span class="badge badge-success"><span class="dot pulse"></span>Live</span>' : '<span class="badge badge-muted">Unavailable</span>') + '</div>' + body;
  }
  function spark(arr) {
    if (arr.length < 2) return '<div class="spark-ph dim">Collecting samples…</div>';
    const max = Math.max.apply(null, arr.concat([1])), w = 300, h = 60;
    const pts = arr.map(function (v, i) { return (i / (arr.length - 1) * w).toFixed(1) + ',' + (h - v / max * (h - 6) - 3).toFixed(1); }).join(' ');
    return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true"><polyline points="' + pts + '" fill="none" stroke="#13D99A" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
  }

  // ---------- storage ----------
  function fmtBytes(b) { if (typeof b !== 'number') return '—'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + ' ' + u[i]; }
  function renderStorage() {
    const el = root.querySelector('#dash-storage');
    const s = state.remote.storage;
    const snaps = state.snapshots();
    let body;
    if (s && typeof s.usedBytes === 'number' && typeof s.capacityBytes === 'number' && s.capacityBytes > 0) {
      const pct = s.usedBytes / s.capacityBytes * 100;
      body = '<div class="row"><span class="stat-icon tone-primary sm">' + icon('hardDrive', 20) + '</span><div class="grow"><div class="row" style="justify-content:space-between"><b>' + fmtBytes(s.usedBytes) + ' / ' + fmtBytes(s.capacityBytes) + '</b><b style="color:var(--success)">' + pct.toFixed(1) + '%</b></div>' +
        '<div class="bar mt-8"><span style="width:' + Math.min(100, pct) + '%"></span></div></div></div>' +
        '<div class="perf-kv mt-12"><span>Snapshots</span><b>' + (s.snapshotCount != null ? s.snapshotCount : snaps.length) + '</b><span>Recordings</span><b>' + (typeof s.recordingBytes === 'number' ? fmtBytes(s.recordingBytes) : 'Not reported') + '</b></div>';
    } else {
      body = '<div class="row"><span class="stat-icon tone-primary sm">' + icon('hardDrive', 20) + '</span><div class="grow"><b>' + snaps.length + ' snapshot records</b>' +
        '<div class="dim" style="font-size:12.5px">Storage used / capacity: unavailable — ' + (api.status.connected ? 'backend did not report /system/storage.' : 'connect the backend.') + '</div></div></div>';
    }
    el.innerHTML = '<div class="card-head"><h2 id="stor-h">Storage Usage</h2><span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/snapshots">Manage snapshots</a></div>' + body;
  }

  // ---------- refresh / metrics ----------
  async function onRefresh(e) {
    const btn = e.currentTarget;
    ui.setBusy(btn, true, 'Refreshing…');
    root.querySelector('#dash-stats').querySelectorAll('.stat-num').forEach(function (n) { n.innerHTML = '<span class="skeleton" style="display:inline-block;width:44px;height:26px"></span>'; });
    try {
      await window.AV.app.checkBackend(true);
      await state.refreshMetrics();
      renderAll();
      ui.toast(api.status.connected ? 'success' : 'info', 'Dashboard refreshed', api.status.connected ? 'Data reloaded from the backend.' : 'Backend not reachable — no live data available.');
    } catch (err) { ui.toast('error', 'Refresh failed', err.message); renderAll(); }
    finally { ui.setBusy(btn, false); }
  }
  function startMetrics() {
    stopMetrics();
    if (!api.status.connected) return;
    state.refreshMetrics();
    metricsTimer = setInterval(function () { if (!document.hidden) state.refreshMetrics(); }, window.AV.config.metricsPollMs);
  }
  function stopMetrics() { if (metricsTimer) clearInterval(metricsTimer); metricsTimer = null; }

  function destroy() {
    stopMetrics();
    unsub.forEach(function (f) { f(); }); unsub = [];
    if (root) media.release(root);
    root = null;
  }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.dashboard = { title: 'Dashboard', render: render, destroy: destroy, refresh: renderAll, onBackend: function () { renderAll(); startMetrics(); } };
})();
