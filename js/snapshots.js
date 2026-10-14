/* PYROTECH VISION — Snapshot management. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog;
  const esc = ui.esc;
  let root = null, unsub = [], f = { q: '', from: '', to: '', cam: '', type: '', sev: '', veh: '' }, page = 1;
  // Vehicle categories come from the backend's detection metadata (snapshot.vehicleClass, from the class label).
  const VEH = [['all', 'All vehicles'], ['car', 'Cars'], ['truck', 'Trucks'], ['two_wheeler', 'Two-wheelers']];

  function render(container) {
    root = container; page = 1;
    const cams = state.cameras();
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<span class="current">Snapshots</span></nav>' +
      '<div class="page-head"><span class="head-icon">' + icon('snapshot', 24) + '</span><div><h1>Snapshots</h1><p class="sub">Frames saved by the backend on detection or on request.</p></div></div>' +
      '<div id="sn-note"></div>' +
      '<div class="toolbar"><div class="search-box">' + icon('search', 16) + '<label class="sr-only" for="sn-q">Search snapshots</label><input class="input" id="sn-q" type="search" placeholder="Search camera, event ID, type…" value="' + esc(f.q) + '"></div>' +
      '<label class="sr-only" for="sn-from">From date</label><input class="input" type="date" id="sn-from" value="' + esc(f.from) + '" style="width:auto" title="From date">' +
      '<label class="sr-only" for="sn-to">To date</label><input class="input" type="date" id="sn-to" value="' + esc(f.to) + '" style="width:auto" title="To date">' +
      '<label class="sr-only" for="sn-cam">Camera</label><select class="select" id="sn-cam" style="width:auto"><option value="">All cameras</option>' + cams.map(function (c) { return '<option value="' + esc(c.id) + '"' + (f.cam === c.id ? ' selected' : '') + '>' + esc(c.name) + '</option>'; }).join('') + '</select>' +
      '<label class="sr-only" for="sn-type">Detection type</label><select class="select" id="sn-type" style="width:auto"><option value="">All types</option>' + Object.keys(cat.EVENT_TYPES).map(function (k) { return '<option value="' + k + '"' + (f.type === k ? ' selected' : '') + '>' + esc(cat.EVENT_TYPES[k].label) + '</option>'; }).join('') + '</select>' +
      '<label class="sr-only" for="sn-sev">Severity</label><select class="select" id="sn-sev" style="width:auto"><option value="">All severities</option>' + ['critical', 'high', 'medium', 'low'].map(function (s) { return '<option value="' + s + '"' + (f.sev === s ? ' selected' : '') + '>' + ui.cap(s) + '</option>'; }).join('') + '</select>' +
      '<button class="btn btn-ghost" id="sn-reset">' + icon('x', 14) + 'Reset</button><span class="grow"></span>' +
      '<div class="segmented" role="group" aria-label="View mode"><button data-view="grid" aria-pressed="' + (state.prefs().snapshotsView !== 'list') + '" aria-label="Grid view">' + icon('layoutGrid', 16) + '</button><button data-view="list" aria-pressed="' + (state.prefs().snapshotsView === 'list') + '" aria-label="List view">' + icon('list', 16) + '</button></div></div>' +
      '<div id="sn-veh"></div>' +
      '<div id="sn-list"></div>';
    const on = function (id, ev, key) { container.querySelector(id).addEventListener(ev, function (e) { f[key] = e.target.value; page = 1; renderList(); }); };
    container.querySelector('#sn-q').addEventListener('input', ui.debounce(function (e) { f.q = e.target.value; page = 1; renderList(); }, 150));
    on('#sn-from', 'change', 'from'); on('#sn-to', 'change', 'to');
    container.querySelector('#sn-cam').addEventListener('change', function (e) { f.cam = e.target.value; f.veh = ''; page = 1; renderList(); }); on('#sn-type', 'change', 'type'); on('#sn-sev', 'change', 'sev');
    container.querySelector('#sn-reset').addEventListener('click', function () { f = { q: '', from: '', to: '', cam: '', type: '', sev: '', veh: '' }; destroy(); render(container); window.AV.icons.hydrate(container); });
    container.querySelectorAll('[data-view]').forEach(function (b) { b.addEventListener('click', function () {
      state.setPref('snapshotsView', b.dataset.view); container.querySelectorAll('[data-view]').forEach(function (x) { x.setAttribute('aria-pressed', x === b); }); renderList();
    }); });
    renderList();
    unsub.push(state.on('snapshots', renderList), state.on('backend', renderList), state.on('all', renderList));
  }

  function filtered() {
    const q = f.q.trim().toLowerCase();
    return state.snapshots().filter(function (s) {
      const d = (s.timestamp || '').slice(0, 10);
      if (f.from && d < f.from) return false;
      if (f.to && d > f.to) return false;
      if (f.cam && s.cameraId !== f.cam) return false;
      if (f.type && s.type !== f.type) return false;
      if (f.sev && s.severity !== f.sev) return false;
      if (f.veh && !vehMatch(s, f.veh)) return false;
      if (q && [state.cameraName(s.cameraId), s.cameraId, s.eventId, s.id, ui.eventType(s.type).label].join(' ').toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  function vehMatch(s, veh) { return s.module === 'vehicle' && (veh === 'all' || s.vehicleClass === veh); }

  /** A road camera = one that has vehicle detection enabled or has vehicle snapshots on the backend. */
  function isRoadCamera(camId) {
    const cam = state.getCamera(camId);
    if (cam && cam.detection && cam.detection.modules && cam.detection.modules.vehicle && cam.detection.modules.vehicle.enabled) return true;
    return state.snapshots().some(function (s) { return s.cameraId === camId && s.module === 'vehicle'; });
  }

  /** Cars / Trucks / Two-wheelers filter for the selected road camera (counts from backend records). */
  function renderVehicleFilter() {
    const el = root.querySelector('#sn-veh');
    if (!f.cam || !isRoadCamera(f.cam)) { el.innerHTML = ''; if (f.veh) f.veh = ''; return; }
    const keep = f.veh; f.veh = '';
    const base = filtered();                 // every other filter applied, vehicle category not yet
    f.veh = keep;
    const count = function (k) { return base.filter(function (s) { return vehMatch(s, k); }).length; };
    el.innerHTML = '<div class="sn-veh" role="group" aria-label="Vehicle type filter"><span class="sn-veh-k">' + icon('car', 15) + 'Vehicle type</span>' +
      '<div class="segmented">' + VEH.map(function (v) {
        return '<button type="button" data-veh="' + v[0] + '" aria-pressed="' + (f.veh === v[0]) + '">' + esc(v[1]) + ' <span class="sn-veh-n">' + count(v[0]) + '</span></button>';
      }).join('') + '</div>' +
      (f.veh ? '<button type="button" class="btn btn-sm btn-ghost" id="sn-veh-clear">' + icon('x', 13) + 'Clear vehicle filter</button>' : '') + '</div>';
    el.querySelectorAll('[data-veh]').forEach(function (b) {
      b.addEventListener('click', function () { f.veh = f.veh === b.dataset.veh ? '' : b.dataset.veh; page = 1; renderList(); });
    });
    const c = el.querySelector('#sn-veh-clear');
    if (c) c.addEventListener('click', function () { f.veh = ''; page = 1; renderList(); });
    window.AV.icons.hydrate(el);
  }

  function typeLabel(s) { return s.title || (s.type === 'manual' ? 'Manual capture' : ui.eventType(s.type).label); }

  function renderList() {
    if (!root) return;
    const all = state.snapshots();
    renderVehicleFilter();
    root.querySelector('#sn-note').innerHTML = (!api.status.connected ? '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div>Snapshots are stored by the camera backend. Connect it to list saved images.</div></div>' : '');
    const list = filtered();
    const size = window.AV.config.pageSize;
    const pages = Math.max(1, Math.ceil(list.length / size)); page = Math.min(page, pages);
    const items = list.slice((page - 1) * size, page * size);
    const el = root.querySelector('#sn-list');
    if (!list.length) {
      el.innerHTML = '<div class="card">' + ui.emptyState('image', all.length ? 'No snapshots match' : 'No snapshots yet', all.length ? 'Adjust the filters to see more snapshots.' : 'Snapshots appear here after the backend saves frames on detection or manual capture.') + '</div>';
      return;
    }
    if (state.prefs().snapshotsView === 'list') {
      el.innerHTML = '<div class="table-wrap"><table class="table"><thead><tr><th>Image</th><th>Camera</th><th>Type</th><th>Time</th><th>Confidence</th><th>Event</th><th style="text-align:right">Actions</th></tr></thead><tbody>' + items.map(function (s) {
        return '<tr><td><button class="link-btn" data-a="view" data-id="' + esc(s.id) + '" aria-label="Preview snapshot ' + esc(s.id) + '"><img class="thumb" src="' + esc(s.image) + '" alt="" loading="lazy"></button></td><td>' + esc(state.cameraName(s.cameraId)) + '</td><td>' + esc(typeLabel(s)) + '</td><td>' + esc(ui.fmtTime(s.timestamp)) + '</td><td>' + ui.confText(s) + '</td><td class="mono">' + esc(s.eventId || '—') + '</td>' +
          '<td><div class="row" style="justify-content:flex-end;gap:6px">' + acts(s) + '</div></td></tr>';
      }).join('') + '</tbody></table></div>';
    } else {
      el.innerHTML = '<div class="snap-grid">' + items.map(function (s) {
        return '<article class="snap-card"><button class="snap-img" data-a="view" data-id="' + esc(s.id) + '" aria-label="Preview snapshot from ' + esc(state.cameraName(s.cameraId)) + '"><img src="' + esc(s.image) + '" alt="" loading="lazy"></button>' +
          '<div class="snap-body"><b class="ellipsis">' + esc(state.cameraName(s.cameraId)) + '</b><span class="row" style="gap:6px">' + ui.toneBadge(s.type === 'manual' ? 'info' : ui.eventType(s.type).tone, typeLabel(s)) + (s.severity ? ui.sevBadge(s.severity) : '') + '</span>' +
          '<span>' + esc(ui.fmtTime(s.timestamp)) + '</span><span>Confidence: ' + ui.confText(s) + (s.eventId ? ' · <span class="mono">' + esc(s.eventId) + '</span>' : '') + '</span></div>' +
          '<div class="snap-actions">' + acts(s) + '</div></article>';
      }).join('') + '</div>';
    }
    el.insertAdjacentHTML('beforeend', ui.pager(list.length, page, size));
    el.querySelectorAll('[data-page]').forEach(function (b) { b.addEventListener('click', function () { page = +b.dataset.page; renderList(); root.scrollIntoView({ behavior: 'smooth' }); }); });
    el.querySelectorAll('[data-a]').forEach(function (b) { b.addEventListener('click', function () {
      const s = state.snapshots().find(function (x) { return x.id === b.dataset.id; }); if (!s) return;
      if (b.dataset.a === 'view') openPreview(s);
      if (b.dataset.a === 'dl') downloadSnap(s);
      if (b.dataset.a === 'del') del(s);
    }); });
  }
  function acts(s) {
    return '<button class="btn btn-xs btn-ghost" data-a="view" data-id="' + esc(s.id) + '" aria-label="Preview">' + icon('eye', 13) + '</button>' +
      '<button class="btn btn-xs btn-ghost" data-a="dl" data-id="' + esc(s.id) + '" aria-label="Download">' + icon('download', 13) + '</button>' +
      '<button class="btn btn-xs btn-ghost danger-hover" data-a="del" data-id="' + esc(s.id) + '" aria-label="Delete">' + icon('trash', 13) + '</button>';
  }

  function openPreview(s) {
    ui.modal({ title: typeLabel(s) + ' — ' + state.cameraName(s.cameraId), icon: 'image', size: 'xl',
      body: '<img class="preview-img" src="' + esc(s.image) + '" alt="Snapshot from ' + esc(state.cameraName(s.cameraId)) + '">' +
        '<dl class="kv mt-16"><dt>Snapshot ID</dt><dd class="mono">' + esc(s.id) + '</dd><dt>Camera</dt><dd>' + esc(state.cameraName(s.cameraId)) + ' <span class="mono dim">' + esc(s.cameraId) + '</span></dd><dt>Time</dt><dd>' + esc(ui.fmtTime(s.timestamp)) + '</dd>' +
        '<dt>Detection</dt><dd>' + esc(typeLabel(s)) + '</dd><dt>Confidence</dt><dd>' + ui.confText(s) + '</dd><dt>Event</dt><dd class="mono">' + esc(s.eventId || '—') + '</dd>' +
        (s.cameraIp ? '<dt>Camera IP</dt><dd class="mono">' + esc(s.cameraIp) + '</dd>' : '') +
        (s.trackId != null ? '<dt>Track</dt><dd class="mono">#' + esc(s.trackId) + '</dd>' : '') +
        (s.type === 'head_count' ? '<dt>Heads detected</dt><dd><b>' + esc(s.headCount) + '</b>' + (s.roi && s.roi.length ? ' <span class="dim">in ' + esc(s.roi.map(function (z) { return z.name; }).join(', ')) + '</span>' : (s.fullFrame ? ' <span class="dim">in the whole frame</span>' : '')) + '</dd>' +
          '' : '') +
        (s.originalUrl ? '<dt>Original frame</dt><dd><a href="' + esc(s.originalUrl) + '" target="_blank" rel="noopener">' + (s.type === 'head_count' ? 'Open the unannotated frame' : 'Open the full-resolution original (main stream)') + '</a></dd>' : '') +
        '</dl>',
      actions: [
        { label: 'Delete', icon: 'trash', cls: 'btn-danger', close: false, onClick: function (m) { del(s).then(function (ok) { if (ok) m.close(); }); return false; } },
        { label: 'Download', icon: 'download', cls: 'btn-ghost', close: false, onClick: function () { downloadSnap(s); return false; } },
        { label: 'Close', cls: 'btn-primary' }
      ] });
  }

  function downloadSnap(s) {
    const a = document.createElement('a');
    a.href = s.image;
    const ext = (/\.(jpe?g|png|webp)(\?|$)/i.exec(s.image) || [, 'jpg'])[1];
    a.download = (s.cameraId + '_' + (s.timestamp || '').replace(/[:.]/g, '-') + '_' + s.id + '.' + ext);
    document.body.appendChild(a); a.click(); a.remove();
    ui.toast('success', 'Download started', a.download);
  }

  async function del(s) {
    const ok = await ui.confirm({ title: 'Delete snapshot?', danger: true, confirmText: 'Delete', message: 'Delete snapshot ' + s.id + ' from ' + state.cameraName(s.cameraId) + ' (' + ui.fmtTime(s.timestamp) + ')? This cannot be undone.' });
    if (!ok) return false;
    try { await state.deleteSnapshot(s.id); ui.toast('success', 'Snapshot deleted', s.id); return true; }
    catch (e) { ui.toast('error', 'Delete failed', e.message); return false; }
  }

  function destroy() { unsub.forEach(function (x) { x(); }); unsub = []; root = null; }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.snapshots = { title: 'Snapshots', render: render, destroy: destroy };
  window.AV.snapshotsUi = { openPreview: openPreview };
})();
