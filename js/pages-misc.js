/* PYROTECH VISION — Detection module pages, Recordings, Reports, AI Models, Not-found. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog;
  const esc = ui.esc;
  window.AV.pages = window.AV.pages || {};

  function crumb(items) {
    return '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + items.map(function (it, i) {
      return icon('chevronRight', 14) + (i === items.length - 1 || !it[1] ? '<span class="current">' + esc(it[0]) + '</span>' : '<a href="' + it[1] + '">' + esc(it[0]) + '</a>');
    }).join('') + '</nav>';
  }

  // ======================= Detection module page =======================
  (function () {
    let root = null, unsub = [], page = 1;
    function render(container, params) {
      root = container; page = 1;
      const id = params.module;
      const mod = id === 'all' ? null : cat.moduleById(id);
      if (id !== 'all' && !mod) { container.innerHTML = ui.emptyState('scan', 'Unknown detection module', '"' + id + '" is not a known module.', '<a class="btn btn-sm" href="#/detections/all">All detections</a>'); return; }
      container.innerHTML = crumb([['Detections', '#/detections/all'], [mod ? mod.long : 'All detections']]) +
        '<div class="page-head"><span class="head-icon" style="color:' + (mod ? mod.color : 'var(--primary)') + '">' + icon(mod ? mod.icon : 'scan', 24) + '</span><div><h1>' + esc(mod ? mod.long : 'All Detections') + '</h1><p class="sub">' + esc(mod ? mod.desc : 'Every detection event reported across all modules and cameras.') + '</p></div>' +
        (mod ? '<div class="actions">' + window.AV.modules.runtimeBadge(mod.id) + ui.switchHtml('dp-en', state.globalModules()[mod.id] && state.globalModules()[mod.id].enabled, 'Enable ' + mod.name + ' in interface', { stateText: true }) + '</div>' : '') + '</div>' +
        '<div class="dash-grid" style="grid-template-areas:none;grid-template-columns:minmax(0,1fr) minmax(300px,380px)"><section class="card" id="dp-events"></section><section class="card" id="dp-cams"></section></div>';
      const en = container.querySelector('#dp-en');
      if (en) en.addEventListener('change', function () { state.setGlobalModule(mod.id, en.checked); en.closest('.switch').querySelector('.state-text').textContent = en.checked ? 'On' : 'Off'; ui.toast('success', mod.name + (en.checked ? ' enabled' : ' disabled'), 'Interface setting saved. ' + window.AV.modules.runtimeStatus(mod.id).text + '.'); });
      draw(id, mod);
      unsub.push(state.on('all', function () { draw(id, mod); }), state.on('cameras', function () { draw(id, mod); }), state.on('backend', function () { draw(id, mod); }));
    }
    function draw(id, mod) {
      if (!root) return;
      let ev = state.detections();
      if (mod) ev = ev.filter(function (d) { return ui.eventType(d.type).module === mod.id; });
      const size = 10, pages = Math.max(1, Math.ceil(ev.length / size)); page = Math.min(page, pages);
      const items = ev.slice((page - 1) * size, page * size);
      const evEl = root.querySelector('#dp-events');
      evEl.innerHTML = '<div class="card-head"><h2>Detection events</h2><span class="spacer"></span><span class="badge badge-muted">' + ev.length + ' total</span></div>' +
        (items.length ? '<div class="det-row" style="grid-template-columns:repeat(auto-fill,minmax(170px,1fr))">' + items.map(function (d) {
          const t = ui.eventType(d.type);
          return '<button class="det-card" data-det="' + esc(d.id) + '"><span class="det-img">' + (d.image ? '<img src="' + esc(d.image) + '" alt="" loading="lazy">' : '') + '<span class="det-label">' + ui.toneBadge(t.tone, d.label || t.label) + '</span></span>' +
            '<span class="det-info"><b class="ellipsis">' + esc(state.cameraName(d.cameraId)) + '</b><span>' + esc(ui.fmtTime(d.timestamp)) + '</span><span>Confidence: ' + ui.confText(d) + '</span></span></button>';
        }).join('') + '</div>' + ui.pager(ev.length, page, size)
          : ui.emptyState('scan', 'No detection events', api.status.connected ? 'The inference backend has not reported events for this module.' : 'Events are produced by the inference backend. Connect it to receive detections.'));
      evEl.querySelectorAll('[data-det]').forEach(function (b) { b.addEventListener('click', function () { const d = state.detections().find(function (x) { return x.id === b.dataset.det; }); if (d) window.AV.events.openDetection(d); }); });
      evEl.querySelectorAll('[data-page]').forEach(function (b) { b.addEventListener('click', function () { page = +b.dataset.page; draw(id, mod); }); });
      const cams = state.cameras().filter(function (c) { return !mod || Object.keys(c.detection.modules).some(function (k) { return (!mod || k === mod.id) && c.detection.modules[k].enabled; }); });
      const camEl = root.querySelector('#dp-cams');
      camEl.innerHTML = '<div class="card-head"><h2>' + (mod ? 'Cameras using this module' : 'Cameras with detection') + '</h2></div>' +
        (cams.length ? '<ul class="alert-list">' + cams.map(function (c) {
          const mc = mod ? c.detection.modules[mod.id] : null;
          return '<li class="row" style="padding:8px 0;border-bottom:1px solid var(--border)"><span class="grow"><b class="ellipsis" style="display:block">' + esc(c.name) + '</b><span class="dim" style="font-size:12px">' + (mc ? 'Confidence ' + Number(mc.confidence).toFixed(2) + ' · ' + ((mc.zones || []).length ? mc.zones.length + ' zone(s)' : 'full frame') : esc(c.location || '')) + '</span></span>' +
            '<a class="btn btn-xs" href="#/cameras/' + encodeURIComponent(c.id) + '/edit">Configure</a></li>';
        }).join('') + '</ul>' : '<p class="muted" style="font-size:13px">No cameras use this module yet.</p>') +
        '<a class="btn btn-sm btn-ghost mt-12" href="#/cameras">' + icon('monitor', 14) + 'Manage cameras</a>';
    }
    function destroy() { unsub.forEach(function (x) { x(); }); unsub = []; root = null; }
    window.AV.pages.detections = { title: 'Detections', render: render, destroy: destroy };
  })();

  // ======================= Recordings =======================
  window.AV.pages.recordings = {
    title: 'Recordings',
    render: function (container) {
      const cams = state.cameras();
      container.innerHTML = crumb([['Recordings']]) +
        '<div class="page-head"><span class="head-icon">' + icon('film', 24) + '</span><div><h1>Recordings</h1><p class="sub">Event clips and continuous recordings stored by the backend.</p></div>' +
        '<div class="actions"><button class="btn" disabled title="Requires the recording service on the backend">' + icon('play', 16) + 'Browse recordings</button></div></div>' +
        '<div class="placeholder-hero"><section class="card"><div class="card-head"><h2>' + icon('server', 18) + 'Backend recording service required</h2></div>' +
        '<p class="muted" style="line-height:1.6">Recording playback is not available in this frontend yet. Video is recorded and stored by the backend, which then exposes a recordings index and a browser-playable clip URL for each event.</p>' +
        '<ul class="feature-list mt-16"><li>' + icon('check', 16) + 'Per-camera pre / post-event duration and continuous-recording settings are already configurable (below).</li><li>' + icon('check', 16) + 'Proposed endpoint: <code class="mono">GET /recordings?cameraId=&amp;from=&amp;to=</code> → [{ id, cameraId, start, end, url, eventId }]</li><li>' + icon('check', 16) + 'Clips should be served as MP4 (H.264) or HLS so browsers can play them.</li></ul>' +
        '<div class="row wrap mt-16"><a class="btn btn-primary btn-sm" href="#/settings">' + icon('server', 14) + 'Configure backend</a><a class="btn btn-sm" href="#/snapshots">' + icon('image', 14) + 'Open snapshots</a></div></section>' +
        '<section class="card"><div class="card-head"><h2>Recording configuration per camera</h2></div>' +
        (cams.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>Camera</th><th>Continuous</th><th>Pre / post</th><th>Retention</th><th></th></tr></thead><tbody>' + cams.map(function (c) {
          const r = c.recording;
          return '<tr><td>' + esc(c.name) + '</td><td>' + (r.continuous ? '<span class="badge badge-info">On</span>' : '<span class="badge badge-muted">Off</span>') + '</td><td>' + r.preEventSec + ' s / ' + r.postEventSec + ' s</td><td>' + r.retentionDays + ' d · ' + r.maxStorageGb + ' GB</td><td style="text-align:right"><a class="btn btn-xs" href="#/cameras/' + encodeURIComponent(c.id) + '/edit">Edit</a></td></tr>';
        }).join('') + '</tbody></table></div>' : ui.emptyState('camera', 'No cameras', 'Add a camera to configure recording.')) + '</section></div>';
    }
  };

  // ======================= Reports =======================
  window.AV.pages.reports = {
    title: 'Reports',
    render: function (container) {
      const today = new Date().toISOString().slice(0, 10), weekAgo = new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10);
      container.innerHTML = crumb([['Reports']]) +
        '<div class="page-head"><span class="head-icon">' + icon('report', 24) + '</span><div><h1>Reports</h1><p class="sub">Export alerts, detections and camera inventory as CSV for audits and shift handover.</p></div></div>' +
        '<div class="placeholder-hero"><section class="card"><div class="card-head"><h2>Generate export</h2></div><form id="rp-form" class="stack" novalidate>' +
        '<div class="field"><label for="rp-kind">Report</label><select class="select" id="rp-kind"><option value="alerts">Alert log</option><option value="detections">Detection events</option><option value="cameras">Camera inventory</option></select></div>' +
        '<div class="form-grid"><div class="field"><label for="rp-from">From</label><input class="input" type="date" id="rp-from" value="' + weekAgo + '"></div><div class="field"><label for="rp-to">To</label><input class="input" type="date" id="rp-to" value="' + today + '"></div></div>' +
        '<span class="field-error" id="rp-err"></span>' +
        '<div class="row"><button class="btn btn-primary" type="submit">' + icon('download', 16) + 'Download CSV</button><span class="dim" id="rp-count" style="font-size:12.5px"></span></div></form></section>' +
        '<section class="card"><div class="card-head"><h2>About report data</h2></div><ul class="feature-list"><li>' + icon('check', 16) + 'Exports contain the records currently available to this console: backend records when connected, plus local configuration.</li><li>' + icon('check', 16) + 'Camera exports never contain passwords; URLs are exported with credentials masked.</li><li>' + icon('info', 16) + 'Scheduled PDF reports need a backend reporting service (not yet available).</li></ul></section></div>';
      const form = container.querySelector('#rp-form');
      const count = function () {
        const rows = build(); container.querySelector('#rp-count').textContent = rows ? (rows.length - 1) + ' row(s) will be exported' : '';
      };
      function build() {
        const kind = container.querySelector('#rp-kind').value, from = container.querySelector('#rp-from').value, to = container.querySelector('#rp-to').value;
        const err = container.querySelector('#rp-err');
        if (from && to && from > to) { err.textContent = '"From" must be on or before "To".'; return null; }
        err.textContent = '';
        const inRange = function (ts) { const d = (ts || '').slice(0, 10); return (!from || d >= from) && (!to || d <= to); };
        const keep = function (r) { return !r.demo; };
        if (kind === 'alerts') return [['alert_id', 'event_id', 'time', 'severity', 'type', 'camera_id', 'camera', 'status', 'acknowledged_by', 'notes']].concat(
          state.alerts().filter(function (a) { return keep(a) && inRange(a.timestamp); }).map(function (a) { return [a.id, a.eventId, a.timestamp, a.severity, ui.eventLabel(a), a.cameraId, state.cameraName(a.cameraId), a.status, a.acknowledgedBy || '', (a.notes || []).map(function (n) { return n.text; }).join(' | '), a.demo ? 'yes' : 'no']; }));
        if (kind === 'detections') return [['event_id', 'time', 'type', 'module', 'camera_id', 'camera', 'confidence']].concat(
          state.detections().filter(function (d) { return keep(d) && inRange(d.timestamp); }).map(function (d) { const t = ui.eventType(d.type); return [d.id, d.timestamp, ui.eventLabel(d), t.module, d.cameraId, state.cameraName(d.cameraId), d.confidence != null ? d.confidence : '', d.demo ? 'yes' : 'no']; }));
        return [['camera_id', 'name', 'type', 'source_url_masked', 'location', 'brand', 'model', 'status', 'enabled', 'modules', 'zones', 'stored_in']].concat(
          state.cameras().filter(keep).map(function (c) { return [c.id, c.name, c.type, window.AV.storage.redactUrl(c.sourceUrl), c.location, c.brand, c.model, c.status, c.enabled !== false ? 'yes' : 'no', Object.keys(c.detection.modules).filter(function (k) { return c.detection.modules[k].enabled; }).join(' '), c.zones.length, c.source, c.demo ? 'yes' : 'no']; }));
      }
      form.addEventListener('change', count);
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        const rows = build(); if (!rows) return;
        if (rows.length < 2) { ui.toast('warning', 'Nothing to export', 'No records match the selected range.'); return; }
        const csv = rows.map(function (r) { return r.map(function (v) { v = String(v === undefined || v === null ? '' : v); if (/^[=+\-@]/.test(v)) v = "'" + v; return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }).join(','); }).join('\r\n');
        const name = 'aivision_' + container.querySelector('#rp-kind').value + '_' + new Date().toISOString().slice(0, 10) + '.csv';
        ui.download(name, '﻿' + csv, 'text/csv;charset=utf-8');
        ui.toast('success', 'Report exported', name + ' · ' + (rows.length - 1) + ' row(s)');
      });
      count();
    }
  };

  // ======================= AI Models =======================
  (function () {
    let root = null, unsub = [];
    function render(container) {
      root = container;
      container.innerHTML = crumb([['AI Models']]) +
        '<div class="page-head"><span class="head-icon">' + icon('cpu', 24) + '</span><div><h1>AI Models</h1><p class="sub">Detection modules and the model registry reported by the inference backend.</p></div>' +
        '<div class="actions"><button class="btn" id="md-reload">' + icon('refresh', 16) + 'Reload registry</button></div></div><div id="md-body"></div>';
      container.querySelector('#md-reload').addEventListener('click', async function (e) {
        const b = e.currentTarget; ui.setBusy(b, true, 'Reloading…');
        await window.AV.app.checkBackend(true);
        ui.setBusy(b, false);
        ui.toast(api.status.connected && state.remote.modules ? 'success' : 'warning', api.status.connected ? (state.remote.modules ? 'Registry reloaded' : 'Registry unavailable') : 'Backend not connected', api.status.connected ? (state.remote.errors.modules || '') : 'The model registry is read from GET /detection-modules.');
        draw();
      });
      draw();
      unsub.push(state.on('modules', draw), state.on('backend', draw));
    }
    function draw() {
      if (!root) return;
      const g = state.globalModules();
      root.querySelector('#md-body').innerHTML =
        (!api.status.connected ? '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div>No backend connected — model load/run state is unknown. Toggles below only change the interface setting.</div></div>' : (state.remote.errors.modules ? '<div class="notice notice-warning mb-12">' + icon('alert', 16) + '<div>Model registry error: ' + esc(state.remote.errors.modules) + '</div></div>' : '')) +
        '<div class="table-wrap"><table class="table"><thead><tr><th>Module</th><th>Interface</th><th>Backend model</th><th>Classes</th><th>Cameras</th><th></th></tr></thead><tbody>' + cat.MODULES.map(function (m) {
          const rt = state.moduleRuntime(m.id);
          const n = state.cameras().filter(function (c) { return c.detection.modules[m.id] && c.detection.modules[m.id].enabled; }).length;
          return '<tr><td><span class="row"><span style="color:' + m.color + '">' + icon(m.icon, 20) + '</span><span><b>' + esc(m.long) + '</b><div class="dim" style="font-size:12px">' + esc(m.desc) + '</div></span></span></td>' +
            '<td>' + ui.switchHtml('mdl-' + m.id, g[m.id] && g[m.id].enabled, 'Enable ' + m.name, { attrs: 'data-m="' + m.id + '"' }) + '</td><td>' + window.AV.modules.runtimeBadge(m.id) + (rt && rt.models ? '<div class="dim" style="font-size:12px">' + rt.models.length + ' model(s)</div>' : '') + '</td>' +
            '<td style="max-width:240px">' + esc(rt && rt.classes ? rt.classes.join(', ') : (m.classes.length ? m.classes.join(', ') + ' (expected)' : 'Model-defined')) + '</td><td>' + n + '</td>' +
            '<td style="text-align:right"><a class="btn btn-xs" href="#/detections/' + m.id + '">Open</a></td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<div class="card mt-16"><div class="card-head"><h2>Registry contract</h2></div><p class="muted" style="font-size:13px">The backend should return the modules it can run from <code class="mono">GET ' + esc(api.base()) + '/detection-modules</code>:</p><div class="code-block mt-8">[\n  { "id": "helmet", "loaded": true, "running": true, "classes": ["helmet", "no_helmet"] },\n  { "id": "custom", "loaded": true, "running": false, "classes": [],\n    "models": [{ "id": "yolo-forklift-v2", "name": "Forklift v2", "classes": ["forklift"] }] }\n]</div></div>';
      root.querySelectorAll('[data-m]').forEach(function (inp) { inp.addEventListener('change', function () { state.setGlobalModule(inp.dataset.m, inp.checked); ui.toast('success', cat.moduleById(inp.dataset.m).name + (inp.checked ? ' enabled' : ' disabled'), 'Interface setting saved.'); }); });
      window.AV.icons.hydrate(root);
    }
    function destroy() { unsub.forEach(function (x) { x(); }); unsub = []; root = null; }
    window.AV.pages.models = { title: 'AI Models', render: render, destroy: destroy };
  })();

  // ======================= Not found =======================
  window.AV.pages.notFound = {
    title: 'Not found',
    render: function (container) { container.innerHTML = '<div class="card mt-16">' + ui.emptyState('alert', 'Page not found', 'The address "' + location.hash + '" does not match any page.', '<a class="btn btn-primary btn-sm" href="#/dashboard">Go to dashboard</a>') + '</div>'; }
  };
})();
