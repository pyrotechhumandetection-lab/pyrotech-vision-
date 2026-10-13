/* PYROTECH VISION — Live View: multi-camera grid with lazy stream loading and cleanup. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, media = window.AV.media;
  const esc = ui.esc;
  const LAYOUTS = [1, 2, 4, 6, 9];
  let root = null, unsub = [], layout = 4, slots = [];

  function colsFor(n) { return { 1: 1, 2: 2, 4: 2, 6: 3, 9: 3 }[n]; }
  function nineAllowed() { return window.innerWidth >= 1100; }

  function render(container, params) {
    root = container;
    const p = state.prefs();
    layout = LAYOUTS.indexOf(p.liveLayout) >= 0 ? p.liveLayout : 4;
    if (layout === 9 && !nineAllowed()) layout = 6;
    const cams = state.cameras().filter(function (c) { return c.enabled !== false; });
    slots = (p.liveSelection || []).filter(function (id) { return !id || state.getCamera(id); });
    if (!slots.length) slots = cams.map(function (c) { return c.id; });
    if (params && params.id) {
      if (!state.getCamera(params.id)) ui.toast('error', 'Camera not found', params.id);
      else { layout = 1; slots = [params.id].concat(slots.filter(function (x) { return x !== params.id; })); }
    }
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<span class="current">Live View</span></nav>' +
      '<div class="page-head"><span class="head-icon">' + icon('camera', 24) + '</span><div><h1>Live View</h1><p class="sub">Monitor multiple cameras. Streams load only for visible tiles and stop when you leave.</p></div>' +
      '<div class="actions"><div class="segmented" role="group" aria-label="Grid layout" id="lv-layouts">' + LAYOUTS.map(function (n) {
        const dis = n === 9 && !nineAllowed();
        return '<button data-layout="' + n + '" aria-pressed="' + (n === layout) + '" ' + (dis ? 'disabled title="9-camera grid needs a wider screen"' : 'title="' + n + ' camera' + (n > 1 ? 's' : '') + '"') + '>' + icon(n === 1 ? 'square' : 'layoutGrid', 14) + n + '</button>';
      }).join('') + '</div>' +
      '<label class="check" style="margin-left:6px"><input type="checkbox" id="lv-ov" ' + (p.showOverlays ? 'checked' : '') + '><span>Detection overlays</span></label>' +
      '<a class="btn" href="#/cameras/new">' + icon('plus', 16) + 'Add Camera</a></div></div>' +
      '<div id="lv-note"></div><div id="lv-grid"></div>';
    window.AV.icons.hydrate(container);
    container.querySelectorAll('[data-layout]').forEach(function (b) { b.addEventListener('click', function () {
      layout = +b.dataset.layout; state.setPref('liveLayout', layout);
      container.querySelectorAll('[data-layout]').forEach(function (x) { x.setAttribute('aria-pressed', x === b); });
      renderGrid();
    }); });
    container.querySelector('#lv-ov').addEventListener('change', function (e) { state.setPref('showOverlays', e.target.checked); renderGrid(); });
    renderGrid();
    unsub.push(state.on('cameras', renderGrid), state.on('backend', renderGrid));
  }

  function renderGrid() {
    if (!root) return;
    const grid = root.querySelector('#lv-grid');
    media.release(grid);
    const cams = state.cameras();
    while (slots.length < layout) slots.push('');
    const note = root.querySelector('#lv-note');
    note.innerHTML = !api.status.connected ? '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div>No backend connected — no live video is available. Live video requires the camera backend to decode RTSP and serve a browser-compatible stream.</div></div>' : '';
    if (!cams.length) {
      grid.innerHTML = '<div class="card">' + ui.emptyState('camera', 'No cameras to show', 'Add a camera first.', '<a class="btn btn-primary btn-sm" href="#/cameras/new">' + icon('plus', 16) + 'Add Camera</a>') + '</div>';
      return;
    }
    const overlays = state.prefs().showOverlays;
    let html = '<div class="live-grid" style="--cols:' + colsFor(layout) + '">';
    for (let i = 0; i < layout; i++) {
      const cam = slots[i] ? state.getCamera(slots[i]) : null;
      html += '<div class="live-tile' + (cam ? '' : ' empty-slot') + '" data-slot="' + i + '"><div class="feed-media">' +
        (cam ? media.mediaHtml(cam, { overlays: overlays, fit: layout === 1 ? 'contain' : 'cover' }) : '<div class="media-ph">' + icon('plus', 26) + '<b>Empty slot</b><span>Choose a camera from the list above.</span></div>') +
        '<div class="tile-head"><label class="sr-only" for="slot-' + i + '">Camera for tile ' + (i + 1) + '</label><select class="select select-sm grow" id="slot-' + i + '" data-slotsel="' + i + '"><option value="">— Empty —</option>' +
        cams.map(function (c) { return '<option value="' + esc(c.id) + '"' + (cam && cam.id === c.id ? ' selected' : '') + (c.enabled === false ? ' disabled' : '') + '>' + esc(c.name) + (c.enabled === false ? ' (disabled)' : '') + '</option>'; }).join('') + '</select>' +
        (cam ? ui.statusBadge(cam) : '') + '</div>' +
        (cam ? '<div class="feed-bottom"><span class="feed-time">' + esc(cam.lastConnectedAt ? 'Last connected ' + ui.relTime(cam.lastConnectedAt) : 'Not verified') + '</span><span class="row" style="gap:6px">' +
          '<button class="feed-btn" data-a="audio" aria-label="Audio" title="Audio is only available when the backend stream includes audio">' + icon('volumeX', 16) + '</button>' +
          '<button class="feed-btn" data-a="snap" aria-label="Capture snapshot" title="Capture snapshot">' + icon('snapshot', 16) + '</button>' +
          '<button class="feed-btn" data-a="cfg" aria-label="Configure camera" title="Configure camera">' + icon('settings', 16) + '</button>' +
          '<button class="feed-btn" data-a="full" aria-label="Full screen" title="Full screen">' + icon('maximize', 16) + '</button></span></div>' : '') +
        '</div></div>';
    }
    grid.innerHTML = html + '</div>';
    grid.querySelectorAll('[data-slotsel]').forEach(function (s) { s.addEventListener('change', function () {
      slots[+s.dataset.slotsel] = s.value; state.setPref('liveSelection', slots.slice()); renderGrid();
    }); });
    grid.querySelectorAll('.live-tile').forEach(function (tile) {
      const cam = state.getCamera(slots[+tile.dataset.slot]);
      tile.querySelectorAll('[data-a]').forEach(function (b) { b.addEventListener('click', function () {
        if (b.dataset.a === 'full') ui.fullscreen(tile.querySelector('.feed-media'));
        if (b.dataset.a === 'snap') media.captureSnapshot(cam, b);
        if (b.dataset.a === 'cfg') window.AV.router.go('/cameras/' + encodeURIComponent(cam.id) + '/edit');
        if (b.dataset.a === 'audio') {
          const slot = tile.querySelector('[data-stream-cam]');
          const v = slot && slot.querySelector('video');
          if (!slot || !slot.dataset.audio || !v) { ui.toast('info', 'No audio', 'This stream does not report an audio track.'); return; }
          v.muted = !v.muted; b.innerHTML = icon(v.muted ? 'volumeX' : 'activity', 16);
        }
      }); });
    });
    media.attach(grid);
    media.alignOverlays(grid);
  }

  function destroy() { unsub.forEach(function (f) { f(); }); unsub = []; if (root) media.release(root); root = null; }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.live = { title: 'Live View', render: render, destroy: destroy };
})();
