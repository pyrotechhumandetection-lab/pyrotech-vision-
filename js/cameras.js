/* PYROTECH VISION — Camera management page, camera details, and the connection-test workflow. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog, media = window.AV.media, S = window.AV.storage;
  const esc = ui.esc;

  // ======================= Connection test =======================
  const RESULTS = {
    OK: { title: 'Connection successful', tone: 'success', tips: [] },
    AUTH_FAILED: { title: 'Authentication failed', tone: 'danger', tips: ['Check the username and password configured on the camera.', 'Confirm the account has RTSP / ONVIF (live view) permission.', 'Many cameras lock the account for some minutes after repeated failures.'] },
    UNREACHABLE: { title: 'Camera unreachable', tone: 'danger', tips: ['Ping the camera IP from the backend host (not from this browser).', 'Check VLAN routing and firewall rules for the RTSP port (default 554).', 'Verify camera power / PoE and the network link LED.'] },
    STREAM_UNAVAILABLE: { title: 'RTSP stream unavailable', tone: 'danger', tips: ['Check the stream path. Hikvision: /Streaming/Channels/101 (main) or /102 (sub). Dahua: /cam/realmonitor?channel=1&subtype=0.', 'Confirm the selected stream profile is enabled on the camera.', 'Check the maximum concurrent RTSP sessions on the camera / NVR.'] },
    TIMEOUT: { title: 'Connection timed out', tone: 'warning', tips: ['Increase the connection timeout.', 'Check bandwidth and latency between the backend and the camera.', 'Try the substream profile for low-bandwidth links.'] },
    UNSUPPORTED_FORMAT: { title: 'Unsupported stream format', tone: 'warning', tips: ['Set the camera video encoding to H.264.', 'Disable H.264+/H.265+ "smart" codecs if the decoder rejects them.'] },
    BACKEND_UNAVAILABLE: { title: 'Backend unavailable', tone: 'muted', tips: ['Start the camera backend service and confirm GET ' + api.ENDPOINTS.health + ' returns {"status":"ok"}.', 'Check the API base URL in Settings → Backend connection.'] },
    INVALID_CONFIG: { title: 'Invalid configuration', tone: 'warning', tips: ['Correct the highlighted fields and test again.'] },
    UNKNOWN: { title: 'Connection test failed', tone: 'danger', tips: ['Check the backend logs for details.'] }
  };

  function clean(msg) { return String(msg || '').replace(/[a-z][a-z0-9+.-]*:\/\/[^\s"']+/gi, function (u) { return S.redactUrl(u); }); }

  /** Run a REAL backend connection test. Never fabricates success. Returns a normalised result. */
  async function runTest(target) {
    try {
      const r = await api.cameras.testConnection(target);
      const ok = !!(r && (r.ok === true || r.success === true || r.code === 'OK'));
      return {
        ok: ok, code: ok ? 'OK' : (r && r.code && RESULTS[r.code] ? r.code : 'UNKNOWN'),
        message: clean(r && r.message), frame: r && (r.frame || r.frameUrl || null),
        details: r ? { width: r.width, height: r.height, codec: r.codec, fps: r.fps, latencyMs: r.latencyMs } : {}
      };
    } catch (e) {
      if (e.kind === 'unavailable') return { ok: false, code: 'BACKEND_UNAVAILABLE', message: 'Connection testing requires the camera backend.' };
      if (e.kind === 'timeout') return { ok: false, code: 'TIMEOUT', message: 'The backend did not finish the test in time. ' + clean(e.message) };
      if (e.kind === 'http' && e.code && RESULTS[e.code]) return { ok: false, code: e.code, message: clean(e.message) };
      if (e.kind === 'http' && (e.status === 400 || e.status === 422)) return { ok: false, code: 'INVALID_CONFIG', message: clean(e.message) };
      if (e.kind === 'network') return { ok: false, code: 'BACKEND_UNAVAILABLE', message: clean(e.message) };
      return { ok: false, code: 'UNKNOWN', message: clean(e.message) };
    }
  }

  function resultHtml(r) {
    const d = RESULTS[r.code] || RESULTS.UNKNOWN;
    const toneMap = { success: 'success', danger: 'danger', warning: 'warning', muted: 'info' };
    let html = '<div class="notice notice-' + toneMap[d.tone] + '">' + icon(r.ok ? 'checkCircle' : (r.code === 'BACKEND_UNAVAILABLE' ? 'server' : 'alertCircle'), 18) +
      '<div><b>' + esc(d.title) + '</b>' + (r.message ? '<div>' + esc(r.message) + '</div>' : '') + '</div></div>';
    if (r.ok) {
      const det = r.details || {};
      const parts = [];
      if (det.width && det.height) parts.push(det.width + '×' + det.height);
      if (det.codec) parts.push(det.codec);
      if (det.fps) parts.push(det.fps + ' fps');
      if (det.latencyMs) parts.push(det.latencyMs + ' ms');
      if (parts.length) html += '<p class="muted mt-8" style="font-size:12.5px">Stream: ' + esc(parts.join(' · ')) + '</p>';
      if (!r.frame) html += '<div class="notice notice-warning mt-8">' + icon('alert', 16) + '<div>The backend reported success but returned no decoded frame, so stream decodability is not confirmed.</div></div>';
    }
    if (d.tips.length) html += '<div class="mt-12"><b style="font-size:12.5px">Troubleshooting</b><ul class="tips">' + d.tips.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul></div>';
    return html;
  }

  /** Test a saved camera from list/details and record the real outcome. */
  async function testSaved(cam, btn) {
    ui.setBusy(btn, true, 'Testing…');
    const r = await runTest(cam.source === 'backend' ? cam.id : state.normalizeCamera(cam));
    ui.setBusy(btn, false);
    if (r.code !== 'BACKEND_UNAVAILABLE' && r.code !== 'INVALID_CONFIG') state.recordConnectionResult(cam.id, r);
    ui.modal({ title: 'Connection test — ' + cam.name, icon: 'plug', body: resultHtml(r) + (r.frame ? '<img class="test-frame mt-12" src="' + esc(r.frame) + '" alt="Frame returned by the backend">' : ''), actions: [{ label: 'Close', cls: 'btn-ghost' }] });
  }

  // ======================= Cameras page =======================
  let root = null, unsub = [], filters = { q: '', status: 'all', type: 'all' };

  function render(container, params) {
    root = container;
    const qs = new URLSearchParams((location.hash.split('?')[1]) || '');
    if (qs.get('status')) filters.status = qs.get('status');
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<span class="current">Cameras</span></nav>' +
      '<div class="page-head"><span class="head-icon">' + icon('monitor', 24) + '</span><div><h1>Cameras</h1><p class="sub">Configure camera connections, detection modules and zones.</p></div>' +
      '<div class="actions"><a class="btn btn-primary" href="#/cameras/new">' + icon('plus', 16) + '<span>Add Camera</span></a></div></div>' +
      '<div class="mini-stats" id="cam-stats"></div>' +
      '<div class="toolbar"><div class="search-box">' + icon('search', 16) + '<label class="sr-only" for="cam-q">Search cameras</label><input class="input" id="cam-q" type="search" placeholder="Search name, ID, location, brand…" value="' + esc(filters.q) + '"></div>' +
      '<label class="sr-only" for="cam-status">Status filter</label><select class="select" id="cam-status" style="width:auto">' +
      [['all', 'All statuses'], ['online', 'Online'], ['offline', 'Offline / error'], ['error', 'Connection error'], ['unverified', 'Not verified'], ['disabled', 'Disabled']].map(function (o) { return '<option value="' + o[0] + '"' + (filters.status === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>' +
      '<label class="sr-only" for="cam-type">Camera type filter</label><select class="select" id="cam-type" style="width:auto"><option value="all">All types</option>' +
      cat.CAMERA_TYPES.map(function (t) { return '<option value="' + t.id + '"' + (filters.type === t.id ? ' selected' : '') + '>' + esc(t.name) + '</option>'; }).join('') + '</select>' +
      '<span class="grow"></span><div class="segmented" role="group" aria-label="View mode"><button data-view="grid" aria-pressed="' + (state.prefs().camerasView !== 'list') + '" aria-label="Grid view" title="Grid view">' + icon('layoutGrid', 16) + '</button><button data-view="list" aria-pressed="' + (state.prefs().camerasView === 'list') + '" aria-label="List view" title="List view">' + icon('list', 16) + '</button></div></div>' +
      '<div id="cam-list"></div>';
    container.querySelector('#cam-q').addEventListener('input', ui.debounce(function (e) { filters.q = e.target.value; renderList(); }, 150));
    container.querySelector('#cam-status').addEventListener('change', function (e) { filters.status = e.target.value; renderList(); });
    container.querySelector('#cam-type').addEventListener('change', function (e) { filters.type = e.target.value; renderList(); });
    container.querySelectorAll('[data-view]').forEach(function (b) { b.addEventListener('click', function () {
      state.setPref('camerasView', b.dataset.view);
      container.querySelectorAll('[data-view]').forEach(function (x) { x.setAttribute('aria-pressed', x === b); });
      renderList();
    }); });
    renderList();
    unsub.push(state.on('cameras', renderList), state.on('backend', renderList), state.on('all', renderList));
    // Status-only change: counters update; cards update in place (ui.refreshLive) unless a status filter needs re-filtering.
    unsub.push(state.on('camstatus', function () { if (!root) return; if (filters.status !== 'all') renderList(); else renderStats(); }));
    if (params && params.id) {
      const cam = state.getCamera(params.id);
      if (cam) openDetails(cam, true); else ui.toast('error', 'Camera not found', 'No camera with ID ' + params.id + '.');
    }
  }

  function filtered() {
    const q = filters.q.trim().toLowerCase();
    return state.cameras().filter(function (c) {
      const st = ui.camStatus(c);
      if (filters.status === 'offline' && !(st === 'offline' || st === 'error')) return false;
      if (filters.status !== 'all' && filters.status !== 'offline' && st !== filters.status) return false;
      if (filters.type !== 'all' && c.type !== filters.type) return false;
      if (q && [c.name, c.id, c.location, c.brand, c.model, c.description].join(' ').toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  function renderStats() {
    const all = state.cameras();
    const cnt = function (f) { return all.filter(f).length; };
    root.querySelector('#cam-stats').innerHTML =
      mini('camera', 'purple', all.length, 'Configured') +
      mini('wifi', 'success', cnt(function (c) { return ui.camStatus(c) === 'online'; }), 'Online') +
      mini('wifiOff', 'danger', cnt(function (c) { return ui.camStatus(c) === 'offline'; }), 'Offline') +
      mini('alertCircle', 'warning', cnt(function (c) { return ui.camStatus(c) === 'error'; }), 'Connection errors');
  }

  function renderList() {
    if (!root) return;
    const all = state.cameras();
    renderStats();
    const list = filtered();
    const el = root.querySelector('#cam-list');
    media.release(el);
    if (!list.length) {
      el.innerHTML = '<div class="card">' + (all.length ? ui.emptyState('filter', 'No cameras match', 'Clear the search or filters to see all cameras.', '<button class="btn btn-sm" id="clear-f">Clear filters</button>')
        : ui.emptyState('camera', 'No cameras configured', 'Add a camera to configure its stream, detection modules and zones.', '<a class="btn btn-primary btn-sm" href="#/cameras/new">' + icon('plus', 16) + 'Add Camera</a>')) + '</div>';
      const cf = el.querySelector('#clear-f');
      if (cf) cf.addEventListener('click', function () { filters = { q: '', status: 'all', type: 'all' }; root.querySelector('#cam-q').value = ''; root.querySelector('#cam-status').value = 'all'; root.querySelector('#cam-type').value = 'all'; renderList(); });
      return;
    }
    if (state.prefs().camerasView === 'list') {
      el.innerHTML = '<div class="table-wrap"><table class="table"><thead><tr><th>Camera</th><th>Type</th><th>IP address</th><th>Location</th><th>Status</th><th>Detection</th><th>Last connected</th><th style="text-align:right">Actions</th></tr></thead><tbody>' +
        list.map(function (c) {
          return '<tr><td><button class="link-btn" data-act="details" data-id="' + esc(c.id) + '"><b>' + esc(c.name) + '</b></button><div class="mono dim">' + esc(c.id) + '</div></td><td>' + esc(typeName(c.type)) + '</td><td>' + ui.ipHtml(c) + '</td><td>' + esc(c.location || '—') + '</td>' +
            '<td>' + ui.statusBadge(c) + (c.lastError ? '<div class="dim ellipsis" style="max-width:200px;font-size:11.5px" title="' + esc(c.lastError) + '">' + esc(c.lastError) + '</div>' : '') + '</td><td>' + modChips(c) + '</td><td>' + esc(c.lastConnectedAt ? ui.relTime(c.lastConnectedAt) : 'never') + '</td>' +
            '<td><div class="row" style="justify-content:flex-end;gap:6px">' + actions(c, true) + '</div></td></tr>';
        }).join('') + '</tbody></table></div>';
    } else {
      el.innerHTML = '<div class="cam-grid">' + list.map(card).join('') + '</div>';
    }
    window.AV.icons.hydrate(el);
    bindActions(el);
    media.attach(el);
    media.alignOverlays(el);
  }

  function mini(ic, tone, n, label) { return '<div class="mini-stat card"><span class="stat-icon sm tone-' + tone + '">' + icon(ic, 18) + '</span><div><b>' + n + '</b><span>' + label + '</span></div></div>'; }
  function typeName(t) { const x = cat.CAMERA_TYPES.find(function (y) { return y.id === t; }); return x ? x.name : t; }
  function modChips(c) {
    const mods = Object.keys(c.detection.modules).filter(function (k) { return c.detection.modules[k].enabled; });
    if (!mods.length) return '<span class="dim" style="font-size:12px">None</span>';
    return '<span class="row" style="gap:4px;flex-wrap:wrap">' + mods.map(function (m) { const mm = cat.moduleById(m); return '<span class="mod-chip" title="' + esc(mm.name) + '" style="color:' + mm.color + '">' + icon(mm.icon, 13) + '<span class="sr-only">' + esc(mm.name) + '</span></span>'; }).join('') + '</span>';
  }
  function actions(c, compact) {
    const b = function (act, ic, label, cls) { return '<button class="btn btn-xs ' + (cls || 'btn-ghost') + '" data-act="' + act + '" data-id="' + esc(c.id) + '" title="' + label + '" aria-label="' + label + ' — ' + esc(c.name) + '">' + icon(ic, 14) + (compact ? '' : '<span>' + label + '</span>') + '</button>'; };
    return b('edit', 'edit', 'Edit') + b('test', 'plug', 'Test') + b('live', 'camera', 'Live') + b('snap', 'snapshot', 'Snapshot') + b('delete', 'trash', 'Delete', 'btn-ghost danger-hover');
  }
  function card(c) {
    return '<article class="cam-card card lift"><div class="cam-thumb feed-media">' + media.mediaHtml(c, { overlays: true }) + '</div>' +
      '<div class="cam-body"><div class="row" style="justify-content:space-between;align-items:flex-start"><div style="min-width:0"><button class="link-btn" data-act="details" data-id="' + esc(c.id) + '"><h3 class="ellipsis">' + esc(c.name) + '</h3></button><span class="mono dim">' + esc(c.id) + ' · ' + esc(typeName(c.type)) + '</span></div>' +
      ui.switchHtml('en-' + c.id, c.enabled !== false, (c.enabled !== false ? 'Disable ' : 'Enable ') + c.name, { attrs: 'data-enable="' + esc(c.id) + '"', cls: 'blue' }) + '</div>' +
      '<div class="row wrap" style="gap:6px">' + ui.statusBadge(c) + ui.sourceBadge(c) + '</div>' +
      '<div class="cam-ip-row">' + ui.ipHtml(c) + '</div>' +
      '<dl class="cam-kv"><dt>' + icon('mapPin', 13) + 'Location</dt><dd>' + esc(c.location || '—') + '</dd><dt>' + icon('clock', 13) + 'Last connected</dt><dd data-lc-cam="' + esc(c.id) + '">' + esc(c.lastConnectedAt ? ui.fmtTime(c.lastConnectedAt) : 'Never') + '</dd>' +
      '<dt>' + icon('scan', 13) + 'Detection</dt><dd>' + modChips(c) + '</dd>' + '<dt data-err-cam="' + esc(c.id) + '"' + (c.lastError ? '' : ' hidden') + '>' + icon('alert', 13) + 'Last error</dt><dd class="err-text" data-err-cam="' + esc(c.id) + '"' + (c.lastError ? '' : ' hidden') + '>' + esc(c.lastError || '') + '</dd></dl>' +
      '<div class="cam-actions">' + actions(c, false) + '</div></div></article>';
  }

  function bindActions(el) {
    el.querySelectorAll('[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        const c = state.getCamera(b.dataset.id);
        if (!c) return;
        handleAction(b.dataset.act, c, b);
      });
    });
    el.querySelectorAll('[data-enable]').forEach(function (inp) {
      inp.addEventListener('change', async function () {
        const c = state.getCamera(inp.dataset.enable);
        try { await state.setCameraEnabled(c.id, inp.checked); ui.toast('success', c.name + (inp.checked ? ' enabled' : ' disabled'), inp.checked ? 'Camera will be included in monitoring.' : 'Camera is excluded from monitoring and live view.'); }
        catch (e) { inp.checked = !inp.checked; ui.toast('error', 'Could not change camera state', e.message); }
      });
    });
  }

  async function handleAction(act, c, btn) {
    if (act === 'edit') window.AV.router.go('/cameras/' + encodeURIComponent(c.id) + '/edit');
    else if (act === 'live') window.AV.router.go('/live/' + encodeURIComponent(c.id));
    else if (act === 'test') testSaved(c, btn);
    else if (act === 'snap') media.captureSnapshot(c, btn);
    else if (act === 'details') openDetails(c);
    else if (act === 'delete') {
      const ok = await ui.confirm({ title: 'Delete camera?', danger: true, confirmText: 'Delete camera',
        message: 'Delete "' + c.name + '" (' + c.id + ')? Its connection settings, detection configuration and ' + c.zones.length + ' detection zone(s) will be removed.',
        detail: c.source === 'backend' ? 'The camera will be deleted on the backend.' : 'This removes the camera from this browser\'s local configuration. Existing snapshots and alerts are kept.' });
      if (!ok) return;
      try { await state.deleteCamera(c.id); ui.toast('success', 'Camera deleted', c.name + ' was removed.'); }
      catch (e) { ui.toast('error', 'Delete failed', e.message); }
    }
  }

  function openDetails(c, fromRoute) {
    const mods = Object.keys(c.detection.modules).filter(function (k) { return c.detection.modules[k].enabled; });
    const md = ui.modal({
      title: c.name, icon: 'camera', size: 'lg', headExtra: ui.statusBadge(c),
      body: '<div class="details-grid"><div class="feed-media details-media">' + media.mediaHtml(c, { overlays: true, fit: 'contain' }) + '</div>' +
        '<dl class="kv"><dt>Identifier</dt><dd class="mono">' + esc(c.id) + '</dd><dt>Type</dt><dd>' + esc(typeName(c.type)) + '</dd>' +
        '<dt>IP address</dt><dd>' + ui.ipHtml(c) + '</dd>' +
        '<dt>Source</dt><dd class="mono" style="word-break:break-all">' + esc(S.redactUrl(c.sourceUrl) || '—') + '</dd>' +
        '<dt>Credentials</dt><dd>' + (c.source === 'backend' ? 'Managed by the backend' : (c.username ? 'User "' + esc(c.username) + '" — password not stored in browser' : 'None')) + (c.urlHadCredentials ? '<div class="dim" style="font-size:12px">Credentials embedded in the URL were stripped before saving.</div>' : '') + '</dd>' +
        '<dt>Brand / model</dt><dd>' + esc([c.brand, c.model].filter(Boolean).join(' ') || '—') + '</dd><dt>Location</dt><dd>' + esc(c.location || '—') + '</dd>' +
        '<dt>Stream profile</dt><dd>' + esc({ main: 'Main stream', sub: 'Substream', custom: 'Custom: ' + (c.customStream || '') }[c.streamProfile] || c.streamProfile) + '</dd>' +
        '<dt>AI detection stream</dt><dd>' + esc({ same: 'Same as viewing stream', sub: 'Substream', custom: 'Custom: ' + (c.aiCustomStream || '') }[c.aiStream || 'same'] || c.aiStream) +
          ((c.aiStream || 'same') !== 'same' ? ' <span class="dim">· snapshots ' + (c.snapshotSource === 'ai' ? 'from the AI stream' : 'with a full-resolution original') + '</span>' : '') + '</dd>' +
        '<dt>Timeout / reconnect</dt><dd>' + esc(c.timeoutSec) + ' s · ' + esc({ auto: 'Auto every ' + c.reconnectIntervalSec + ' s', manual: 'Manual', never: 'Never' }[c.reconnect] || c.reconnect) + '</dd>' +
        '<dt>Detection</dt><dd>' + (mods.length ? mods.map(function (m) { return esc(cat.moduleById(m).name) + ' (' + Number(c.detection.modules[m].confidence).toFixed(2) + ')'; }).join(', ') : 'None') + '</dd>' +
        '<dt>Zones</dt><dd>' + (c.zones.length ? c.zones.map(function (z) { return '<span class="badge badge-muted" style="margin:0 4px 4px 0"><span class="dot" style="color:' + esc(z.color) + '"></span>' + esc(z.name) + '</span>'; }).join('') : 'Full frame') + '</dd>' +
        '<dt>Last connected</dt><dd>' + esc(c.lastConnectedAt ? ui.fmtTime(c.lastConnectedAt) : 'Never') + '</dd><dt>Last error</dt><dd>' + esc(c.lastError || '—') + '</dd>' +
        '<dt>Stored in</dt><dd>' + ui.sourceBadge(c) + '</dd><dt>Description</dt><dd>' + esc(c.description || '—') + '</dd></dl></div>',
      actions: [
        { label: 'Test connection', icon: 'plug', cls: 'btn-ghost', close: false, onClick: function (m, b) { testSaved(c, b); return false; } },
        { label: 'Live view', icon: 'camera', cls: 'btn-ghost', onClick: function () { window.AV.router.go('/live/' + encodeURIComponent(c.id)); } },
        { label: 'Edit camera', icon: 'edit', cls: 'btn-primary', onClick: function () { window.AV.router.go('/cameras/' + encodeURIComponent(c.id) + '/edit'); } }
      ],
      onClose: function () { if (fromRoute && /^\/cameras\/[^/]+$/.test(window.AV.router.path())) history.replaceState(null, '', '#/cameras'); }
    });
    media.attach(md.body); media.alignOverlays(md.body);
  }

  function destroy() { unsub.forEach(function (f) { f(); }); unsub = []; if (root) media.release(root); root = null; }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.cameras = { title: 'Cameras', render: render, destroy: destroy };
  window.AV.connection = { runTest: runTest, resultHtml: resultHtml, testSaved: testSaved, RESULTS: RESULTS, clean: clean };
  window.AV.cameraDetails = openDetails;
})();
