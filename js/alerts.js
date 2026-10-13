/* PYROTECH VISION — Alert management + shared event/alert detail dialogs. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog;
  const esc = ui.esc;

  // ======================= shared detail dialogs =======================
  function openDetection(d) {
    const t = ui.eventType(d.type);
    const alert = state.alerts().find(function (a) { return a.eventId === d.id; });
    const snap = state.snapshots().find(function (s) { return s.eventId === d.id; });
    const m = ui.modal({
      title: ui.eventLabel(d), icon: (cat.moduleById(t.module) || {}).icon || 'scan', size: 'lg', 
      body: (d.image ? '<div class="event-media"><img src="' + esc(d.image) + '" alt="Detection image"></div>' : '') +
        '<dl class="kv mt-16"><dt>Event ID</dt><dd class="mono">' + esc(d.id) + '</dd><dt>Detection</dt><dd>' + ui.toneBadge(t.tone, d.label || t.label) + (d.trackId != null ? ' <span class="dim">track #' + esc(d.trackId) + '</span>' : '') + '</dd>' +
        '<dt>Module</dt><dd>' + esc((cat.moduleById(t.module) || { name: t.module }).name) + '</dd><dt>Camera</dt><dd>' + esc(state.cameraName(d.cameraId)) + ' <span class="mono dim">' + esc(d.cameraId) + '</span></dd>' +
        '<dt>Time</dt><dd>' + esc(ui.fmtTime(d.timestamp)) + '</dd><dt>Confidence</dt><dd>' + ui.confText(d) + '</dd>' +
        (d.zone ? '<dt>Zone</dt><dd>' + esc(d.zone) + '</dd>' : '') + '<dt>Alert</dt><dd>' + (alert ? ui.sevBadge(alert.severity) + ' ' + esc(alert.status === 'acknowledged' ? 'Acknowledged' : 'Open') : 'No alert raised') + '</dd>' +
        '</dl>',
      actions: [
        { label: 'Go to camera', icon: 'camera', cls: 'btn-ghost', onClick: function () { window.AV.router.go('/cameras/' + encodeURIComponent(d.cameraId)); } },
        snap ? { label: 'Open snapshot', icon: 'image', cls: 'btn-ghost', onClick: function () { setTimeout(function () { window.AV.snapshotsUi.openPreview(snap); }, 220); } } : null,
        alert ? { label: 'View alert', icon: 'bell', cls: 'btn-primary', onClick: function () { setTimeout(function () { openAlert(alert.id); }, 220); } } : { label: 'Close', cls: 'btn-primary' }
      ].filter(Boolean)
    });
    return m;
  }

  function openAlert(id) {
    const a = state.alerts().find(function (x) { return x.id === id; });
    if (!a) { ui.toast('error', 'Alert not found', id); return; }
    const t = ui.eventType(a.type);
    const isLocal = a.source !== 'backend';
    const snap = state.snapshots().find(function (s) { return s.eventId === a.eventId; });
    const body = function () {
      return (a.image ? '<div class="event-media"><img src="' + esc(a.image) + '" alt="Alert snapshot"></div>' : '') +
        '<dl class="kv mt-16"><dt>Alert ID</dt><dd class="mono">' + esc(a.id) + '</dd><dt>Severity</dt><dd>' + ui.sevBadge(a.severity) + '</dd><dt>Event</dt><dd>' + esc(ui.eventLabel(a)) + (a.description ? '<div class="dim" style="font-size:12.5px">' + esc(a.description) + '</div>' : '') + (a.eventId ? ' <span class="mono dim">' + esc(a.eventId) + '</span>' : '') + '</dd>' +
        '<dt>Camera</dt><dd>' + esc(state.cameraName(a.cameraId)) + '</dd><dt>Time</dt><dd>' + esc(ui.fmtTime(a.timestamp)) + '</dd><dt>Confidence</dt><dd>' + ui.confText(a) + '</dd>' +
        '<dt>Status</dt><dd>' + (a.status === 'acknowledged' ? '<span class="badge badge-success">' + icon('check', 12) + 'Acknowledged</span> ' + (a.acknowledgedAt ? '<span class="dim">by ' + esc(a.acknowledgedBy || 'operator') + ', ' + esc(ui.fmtTime(a.acknowledgedAt)) + '</span>' : '') : '<span class="badge badge-warning">Open</span>') +
        (isLocal ? '<div class="dim" style="font-size:12px">Local workflow: acknowledgement and notes are stored in this browser.</div>' : '') + '</dd>' +
        '</dl>' +
        '<div class="divider"></div><h3 style="font-size:14px">Notes</h3>' +
        ((a.notes || []).length ? '<ul class="notes">' + a.notes.map(function (n) { return '<li>' + esc(n.text) + '<small>' + esc(n.by || '') + ' · ' + esc(ui.fmtTime(n.at)) + '</small></li>'; }).join('') + '</ul>' : '<p class="dim" style="font-size:13px">No notes.</p>') +
        (isLocal ? '<div class="field mt-12"><label for="al-note">Add note</label><textarea class="textarea" id="al-note" rows="2" maxlength="500" placeholder="e.g. Supervisor informed, worker issued helmet."></textarea><span class="field-error" id="al-note-err"></span></div>'
          : '<p class="dim mt-8" style="font-size:12px">Notes on backend alerts are sent with acknowledgement.</p>');
    };
    const md = ui.modal({
      title: ui.eventLabel(a), icon: 'bell', size: 'lg', body: body(),
      actions: [
        { label: 'Go to camera', icon: 'camera', cls: 'btn-ghost', onClick: function () { window.AV.router.go('/cameras/' + encodeURIComponent(a.cameraId)); } },
        snap ? { label: 'Open snapshot', icon: 'image', cls: 'btn-ghost', onClick: function () { setTimeout(function () { window.AV.snapshotsUi.openPreview(snap); }, 220); } } : null,
        isLocal ? { label: 'Add note', icon: 'note', cls: 'btn-ghost', close: false, onClick: function (m) {
          const ta = m.body.querySelector('#al-note'); const v = ta.value.trim();
          if (!v) { m.body.querySelector('#al-note-err').textContent = 'Write a note first.'; ta.focus(); return false; }
          try { state.addAlertNote(a.id, v); refresh(m); ui.toast('success', 'Note added'); } catch (e) { ui.toast('error', 'Could not add note', e.message); }
          return false;
        } } : null,
        a.status !== 'acknowledged' ? { label: 'Acknowledge', icon: 'check', cls: 'btn-primary', close: false, onClick: function (m, b) {
          const ta = m.body.querySelector('#al-note');
          ui.setBusy(b, true, 'Saving…');
          state.acknowledgeAlert(a.id, ta ? ta.value.trim() : '').then(function () { ui.toast('success', 'Alert acknowledged', ui.eventLabel(a) + ' — ' + state.cameraName(a.cameraId)); m.close(); })
            .catch(function (e) { ui.setBusy(b, false); ui.toast('error', 'Acknowledge failed', e.message); });
          return false;
        } } : { label: 'Close', cls: 'btn-primary' }
      ].filter(Boolean)
    });
    function refresh(m) { const cur = state.alerts().find(function (x) { return x.id === id; }); if (cur) Object.assign(a, cur); m.body.innerHTML = body(); window.AV.icons.hydrate(m.body); }
    return md;
  }

  // ======================= Alerts page =======================
  let root = null, unsub = [], f = { q: '', sev: '', status: '', cam: '', type: '' }, page = 1;

  function render(container) {
    root = container; page = 1;
    const cams = state.cameras();
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<span class="current">Alerts</span></nav>' +
      '<div class="page-head"><span class="head-icon">' + icon('bell', 24) + '</span><div><h1>Alerts</h1><p class="sub">Review, acknowledge and annotate alerts raised from detection events.</p></div>' +
      '<div class="actions"><button class="btn" id="al-ackall">' + icon('check', 16) + 'Acknowledge visible</button></div></div>' +
      '<div class="mini-stats" id="al-stats"></div><div id="al-note-top"></div>' +
      '<div class="toolbar"><div class="search-box">' + icon('search', 16) + '<label class="sr-only" for="al-q">Search alerts</label><input class="input" id="al-q" type="search" placeholder="Search alerts…" value="' + esc(f.q) + '"></div>' +
      sel('al-sev', 'sev', 'All severities', ['critical', 'high', 'medium', 'low'].map(function (s) { return [s, ui.cap(s)]; })) +
      sel('al-status', 'status', 'All statuses', [['new', 'Open'], ['acknowledged', 'Acknowledged']]) +
      sel('al-cam', 'cam', 'All cameras', cams.map(function (c) { return [c.id, c.name]; })) +
      sel('al-type', 'type', 'All event types', Object.keys(cat.EVENT_TYPES).map(function (k) { return [k, cat.EVENT_TYPES[k].label]; })) + '</div>' +
      '<div id="al-list"></div>';
    container.querySelector('#al-q').addEventListener('input', ui.debounce(function (e) { f.q = e.target.value; page = 1; renderList(); }, 150));
    ['sev', 'status', 'cam', 'type'].forEach(function (k) { container.querySelector('#al-' + k).addEventListener('change', function (e) { f[k] = e.target.value; page = 1; renderList(); }); });
    container.querySelector('#al-ackall').addEventListener('click', ackVisible);
    const qs = new URLSearchParams(location.hash.split('?')[1] || '');
    if (qs.get('id')) setTimeout(function () { openAlert(qs.get('id')); }, 50);
    renderList();
    unsub.push(state.on('alerts', renderList), state.on('backend', renderList), state.on('all', renderList));
  }
  function sel(id, key, all, opts) {
    return '<label class="sr-only" for="' + id + '">' + all + '</label><select class="select" id="' + id + '" style="width:auto"><option value="">' + all + '</option>' + opts.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (f[key] === o[0] ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join('') + '</select>';
  }
  function filtered() {
    const q = f.q.trim().toLowerCase();
    return state.alerts().filter(function (a) {
      if (f.sev && a.severity !== f.sev) return false;
      if (f.status && (a.status === 'acknowledged' ? 'acknowledged' : 'new') !== f.status) return false;
      if (f.cam && a.cameraId !== f.cam) return false;
      if (f.type && a.type !== f.type) return false;
      if (q && [a.id, a.eventId, ui.eventLabel(a), state.cameraName(a.cameraId), a.severity].join(' ').toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }
  function renderList() {
    if (!root) return;
    const all = state.alerts();
    const c = function (sv) { return all.filter(function (a) { return a.severity === sv && a.status !== 'acknowledged'; }).length; };
    root.querySelector('#al-stats').innerHTML =
      mini('bell', 'primary', state.unackCount(), 'Open alerts') + mini('alert', 'danger', c('critical') + c('high'), 'Open critical / high') + mini('alertCircle', 'warning', c('medium'), 'Open medium') + mini('checkCircle', 'success', all.filter(function (a) { return a.status === 'acknowledged'; }).length, 'Acknowledged');
    root.querySelector('#al-note-top').innerHTML = '';
    const list = filtered(), size = window.AV.config.pageSize;
    const pages = Math.max(1, Math.ceil(list.length / size)); page = Math.min(page, pages);
    const items = list.slice((page - 1) * size, page * size);
    const el = root.querySelector('#al-list');
    if (!list.length) { el.innerHTML = '<div class="card">' + ui.emptyState('bell', all.length ? 'No alerts match' : 'No alerts', all.length ? 'Adjust the filters.' : 'Alerts appear when the inference backend reports detections that match alert rules.') + '</div>'; return; }
    el.innerHTML = '<div class="table-wrap"><table class="table alert-table"><thead><tr><th>Severity</th><th>Snapshot</th><th>Event</th><th>Camera</th><th>Time</th><th>Status</th><th style="text-align:right">Actions</th></tr></thead><tbody>' + items.map(function (a) {
      const t = ui.eventType(a.type);
      return '<tr><td class="sev">' + ui.sevBadge(a.severity) + '</td><td>' + (a.image ? '<img class="thumb" src="' + esc(a.image) + '" alt="" loading="lazy">' : '<span class="dim">—</span>') + '</td>' +
        '<td><button class="link-btn" data-a="view" data-id="' + esc(a.id) + '"><b>' + esc(ui.eventLabel(a)) + '</b></button><div class="mono dim">' + esc(a.id) + '</div></td>' +
        '<td><a href="#/cameras/' + encodeURIComponent(a.cameraId) + '">' + esc(state.cameraName(a.cameraId)) + '</a></td><td>' + esc(ui.fmtTime(a.timestamp)) + '<div class="dim" style="font-size:11.5px">' + esc(ui.relTime(a.timestamp)) + '</div></td>' +
        '<td>' + (a.status === 'acknowledged' ? '<span class="badge badge-success">' + icon('check', 12) + 'Acknowledged</span>' : '<span class="badge badge-warning">Open</span>') + ((a.notes || []).length ? ' <span class="badge badge-muted" title="Notes">' + icon('note', 12) + a.notes.length + '</span>' : '') + '</td>' +
        '<td><div class="row" style="justify-content:flex-end;gap:6px"><button class="btn btn-xs btn-ghost" data-a="view" data-id="' + esc(a.id) + '">' + icon('eye', 13) + 'Details</button>' +
        (a.status !== 'acknowledged' ? '<button class="btn btn-xs" data-a="ack" data-id="' + esc(a.id) + '">' + icon('check', 13) + 'Acknowledge</button>' : '') + '</div></td></tr>';
    }).join('') + '</tbody></table></div>' + ui.pager(list.length, page, size);
    el.querySelectorAll('[data-page]').forEach(function (b) { b.addEventListener('click', function () { page = +b.dataset.page; renderList(); }); });
    el.querySelectorAll('[data-a]').forEach(function (b) { b.addEventListener('click', async function () {
      if (b.dataset.a === 'view') openAlert(b.dataset.id);
      if (b.dataset.a === 'ack') {
        ui.setBusy(b, true, '…');
        try { await state.acknowledgeAlert(b.dataset.id); ui.toast('success', 'Alert acknowledged', b.dataset.id); }
        catch (e) { ui.setBusy(b, false); ui.toast('error', 'Acknowledge failed', e.message); }
      }
    }); });
  }
  function mini(ic, tone, n, label) { return '<div class="mini-stat card"><span class="stat-icon sm tone-' + tone + '">' + icon(ic, 18) + '</span><div><b>' + n + '</b><span>' + label + '</span></div></div>'; }

  async function ackVisible() {
    const open = filtered().filter(function (a) { return a.status !== 'acknowledged'; });
    if (!open.length) { ui.toast('info', 'Nothing to acknowledge', 'No open alerts in the current view.'); return; }
    const ok = await ui.confirm({ title: 'Acknowledge ' + open.length + ' alert(s)?', message: 'All open alerts matching the current filters will be marked acknowledged by ' + state.prefs().profileName + '.', confirmText: 'Acknowledge all' });
    if (!ok) return;
    let failed = 0;
    for (const a of open) { try { await state.acknowledgeAlert(a.id); } catch (e) { failed++; } }
    ui.toast(failed ? 'warning' : 'success', 'Acknowledged ' + (open.length - failed) + ' alert(s)', failed ? failed + ' failed.' : '');
  }

  function destroy() { unsub.forEach(function (x) { x(); }); unsub = []; root = null; }

  window.AV.events = { openDetection: openDetection, openAlert: openAlert };
  window.AV.pages = window.AV.pages || {};
  window.AV.pages.alerts = { title: 'Alerts', render: render, destroy: destroy };
})();
