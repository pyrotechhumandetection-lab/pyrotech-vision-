/* PYROTECH VISION — Human Count page (live, backend-driven).
 *
 * Talks to the Human Count module of the shared backend through /api/detections/count/* and
 * /api/detections/count/history. The module runs its own person detector + people tracker on the
 * camera's shared session; every number on this page is reported by the backend - nothing is
 * simulated. Without a backend, the page says so and every control is disabled.
 */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api;
  const esc = ui.esc;
  const MOD = 'count', TITLE = 'Human Count';
  const STATE_UI = {
    backend_unavailable: ['badge-muted', 'Backend unavailable'], no_camera: ['badge-muted', 'No camera selected'],
    stopped: ['badge-muted', 'Stopped'], connecting: ['badge-info', 'Connecting'], processing: ['badge-success', 'Counting'],
    paused: ['badge-warning', 'Paused'], disconnected: ['badge-danger', 'Disconnected'], error: ['badge-danger', 'Backend error']
  };

  let root, camId, status, cfgResp, history = [], timer, histTimer, busy = false, streamOn = false, unsub = [], histView = 'chart';
  let tab = 'head', headCtl = null;   // 'head' = Head Count (still frame), 'live' = live people counting

  function render(container) {
    root = container;
    camId = state.prefs().countCamera || '';
    status = null; cfgResp = null; history = []; streamOn = false;
    tab = state.prefs().countTab === 'live' ? 'live' : 'head';
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<a href="#/detections/all">Detections</a>' + icon('chevronRight', 14) + '<span class="current">' + TITLE + '</span></nav>' +
      '<div class="page-head"><span class="head-icon" style="color:#13D99A">' + icon('users', 24) + '</span><div><h1>' + TITLE + '</h1>' +
      '<p class="sub">Head count on a captured frame (dedicated head detector) · live people counting (person detector + tracker)</p></div>' +
      '<div class="actions"><a class="btn" id="hc-open-cam" href="#/cameras">' + icon('monitor', 16) + '<span>Open camera</span></a><a class="btn btn-ghost" href="#/alerts">' + icon('bell', 16) + '<span>Alerts</span></a></div></div>' +
      '<div id="hc-notice"></div>' +
      '<div class="segmented hc-tabs mb-12" role="tablist" aria-label="Human Count mode">' +
      '<button type="button" role="tab" data-tab="head" aria-selected="' + (tab === 'head') + '" aria-pressed="' + (tab === 'head') + '">' + icon('snapshot', 15) + 'Head count (capture &amp; count)</button>' +
      '<button type="button" role="tab" data-tab="live" aria-selected="' + (tab === 'live') + '" aria-pressed="' + (tab === 'live') + '">' + icon('activity', 15) + 'Live people counting</button></div>' +
      '<section class="card hh-card" id="hc-tab-head" aria-labelledby="hh-h"' + (tab === 'head' ? '' : ' hidden') + '></section>' +
      '<div id="hc-tab-live"' + (tab === 'live' ? '' : ' hidden') + '>' +
      '<div class="mini-stats" id="hc-stats"></div>' +
      '<div class="dl-grid">' +
      '<section class="card dl-live" aria-labelledby="hc-live-h"></section>' +
      '<div class="dl-side"><section class="card" id="hc-status" aria-labelledby="hc-status-h"></section><section class="card" id="hc-settings" aria-labelledby="hc-set-h"></section></div>' +
      '<section class="card dl-events" id="hc-history" aria-labelledby="hc-hist-h"></section></div></div>';
    headCtl = window.AV.headCount.mount(container.querySelector('#hc-tab-head'), { cameraId: camId, onCameraChange: function (id) { switchCamera(id); } });
    headCtl.setActive(tab === 'head');
    container.querySelectorAll('[data-tab]').forEach(function (b) { b.addEventListener('click', function () { setTab(b.dataset.tab); }); });
    renderLive();
    renderStats();
    renderStatus();
    renderSettings();
    renderHistory();
    unsub.push(state.on('backend', onBackend), state.on('cameras', function () { renderCameraSelect(); }));
    onBackend();
  }

  function setTab(t) {
    if (t === tab || !root) return;
    if (headCtl && headCtl.isBusy()) { ui.toast('info', 'Counting in progress', 'Wait for the head count to finish.'); return; }
    tab = t; state.setPref('countTab', t);
    root.querySelectorAll('[data-tab]').forEach(function (b) { const on = b.dataset.tab === t; b.setAttribute('aria-selected', String(on)); b.setAttribute('aria-pressed', String(on)); });
    root.querySelector('#hc-tab-head').hidden = t !== 'head';
    root.querySelector('#hc-tab-live').hidden = t !== 'live';
    headCtl.setActive(t === 'head');
    if (t === 'head') detachStream(); else paintLive(true);
  }

  function backendCams() { return state.cameras().filter(function (c) { return c.source === 'backend'; }); }
  function selectedCam() { return camId ? state.getCamera(camId) : null; }
  function counts() { return (status && status.count) || null; }

  function onBackend() {
    if (!root) return;
    const n = root.querySelector('#hc-notice');
    if (!api.status.connected) {
      n.innerHTML = '<div class="notice notice-warning mb-12">' + icon('server', 16) + '<div><b>Backend not connected.</b> Human Count runs on the detection backend in this repository. Start it with <code class="mono">start-backend.cmd</code>. <a href="#/settings">Backend settings</a></div></div>';
    } else if (!backendCams().length) {
      n.innerHTML = '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div>No cameras are registered on the backend yet. <a href="#/cameras/new">Add a camera</a> in Camera Management.</div></div>';
    } else n.innerHTML = '';
    if (camId && !backendCams().some(function (c) { return c.id === camId; })) camId = '';
    renderCameraSelect();
    if (headCtl) headCtl.onBackend();
    refresh(true);
    clearInterval(timer); clearInterval(histTimer);
    if (api.status.connected) {
      timer = setInterval(function () { if (!document.hidden) refresh(false); }, 2000);
      histTimer = setInterval(function () { if (!document.hidden) loadHistory(); }, 15000);
    }
  }

  // ======================= status =======================
  async function refresh(withConfig) {
    if (!root) return;
    if (!api.status.connected) { status = { state: 'backend_unavailable' }; paint(); return; }
    if (!camId) { status = { state: 'no_camera' }; cfgResp = null; history = []; paint(); renderSettings(); renderHistory(); return; }
    try {
      status = await api.detection.status(MOD, camId);
      if (withConfig) { cfgResp = await api.detection.config(MOD, camId); renderSettings(); loadHistory(); }
    } catch (e) {
      status = { state: 'error', error: { code: e.code || e.kind, message: e.message } };
    }
    paint();
  }

  function stateKey() { return status ? (status.state || 'stopped') : 'connecting'; }
  function paint() { if (!root || busy) return; renderStats(); renderStatus(); paintLive(); renderRecent(); }
  function fmt(v, unit) { return v === null || v === undefined ? '<span class="dim">Unavailable</span>' : esc(v) + (unit || ''); }

  function renderStats() {
    const c = counts(), live = stateKey() === 'processing';
    const tile = function (ic, tone, val, label, title) {
      return '<div class="mini-stat card" title="' + esc(title) + '"><span class="stat-icon sm tone-' + tone + '">' + icon(ic, 18) + '</span><div><b>' + val + '</b><span>' + esc(label) + '</span></div></div>';
    };
    const v = function (x) { return c && x !== undefined && x !== null ? esc(x) : '—'; };
    root.querySelector('#hc-stats').innerHTML =
      tile('users', 'success', live ? v(c && c.current) : '—', 'People now', 'Confirmed people visible in the current frame') +
      tile('user', 'primary', v(c && c.unique), 'People counted', 'Unique people since detection started (each counted once)') +
      tile('activity', 'purple', v(c && c.peak), 'Peak' + (c && c.peakAt ? ' · ' + ui.fmtTime(c.peakAt, { noYear: true }) : ''), 'Highest number of people at the same time') +
      tile('clock', 'warning', c ? Number(c.average60s).toFixed(1) : '—', 'Average (last 60 s)', 'Average people in view over the last minute');
  }

  function renderStatus() {
    const el = root.querySelector('#hc-status');
    const s = status || {}, k = stateKey(), su = STATE_UI[k] || STATE_UI.stopped, m = s.metrics || {}, c = counts();
    const cam = selectedCam();
    el.innerHTML = '<div class="card-head"><h2 id="hc-status-h">Counting Status</h2><span class="spacer"></span><span class="badge ' + su[0] + '">' + (k === 'processing' ? '<span class="dot pulse"></span>' : '') + su[1] + '</span></div>' +
      '<dl class="kv dl-kv">' +
      '<dt>Backend</dt><dd>' + (api.status.connected ? '<span class="badge badge-success">Connected</span>' : '<span class="badge badge-muted">Not connected</span>') + '</dd>' +
      '<dt>Camera</dt><dd>' + (cam ? esc(cam.name) + ' <span class="mono dim">' + esc(cam.id) + '</span>' : '<span class="dim">None selected</span>') + '</dd>' +
      '<dt>Camera IP</dt><dd>' + (cam ? ui.ipHtml(cam) : '<span class="dim">No camera selected</span>') + '</dd>' +
      '<dt>Camera stream</dt><dd>' + esc({ processing: 'Receiving frames', connected: 'Receiving frames', disconnected: 'Disconnected — reconnecting', error: 'Error', not_connected: 'Not connected', connecting: 'Connecting' }[s.cameraState] || (s.cameraState ? s.cameraState : '—')) + '</dd>' +
      '<dt>AI model</dt><dd>' + esc(s.modelFile || 'yolov8n') + ' <span class="dim">· person class</span>' + (s.modelAvailable === false ? ' <span class="badge badge-danger">Model file missing</span>' : '') +
      (s.sharedWith && s.sharedWith.length ? '<div class="dim" style="font-size:12px">Camera shared with: ' + esc(s.sharedWith.map(function (x) { return x === 'human' ? 'Human Detection' : x === 'vehicle' ? 'Vehicle Detection' : x; }).join(', ')) + '</div>' : '') + '</dd>' +
      '<dt>Camera FPS (received)</dt><dd>' + fmt(m.cameraFps, ' fps') + '</dd>' +
      '<dt>AI processing FPS</dt><dd>' + fmt(m.processingFps, ' fps') + (m.inferenceMs != null ? ' <span class="dim">· ' + esc(m.inferenceMs) + ' ms per frame</span>' : '') + '</dd>' +
      '<dt>Tracks active</dt><dd>' + (c ? esc(c.tracksActive) + ' <span class="dim">(incl. people briefly hidden)</span>' : '<span class="dim">—</span>') + '</dd>' +
      '<dt>Last person seen</dt><dd>' + (c && c.lastPersonAt ? esc(ui.fmtTime(c.lastPersonAt)) + ' <span class="dim">(' + esc(ui.relTime(c.lastPersonAt)) + ')</span>' : '<span class="dim">None yet</span>') + '</dd>' +
      '<dt>Counting since</dt><dd>' + (c && c.since ? esc(ui.fmtTime(c.since)) : '<span class="dim">—</span>') + '</dd>' +
      '</dl>' + (s.error && s.error.message ? '<div class="notice notice-danger mt-12">' + icon('alertCircle', 16) + '<div><b>' + esc(s.error.code || 'Error') + '</b> ' + esc(s.error.message) + '</div></div>' : '');
  }

  // ======================= camera select + live =======================
  function renderLive() {
    const el = root.querySelector('.dl-live');
    el.innerHTML = '<div class="card-head"><h2 id="hc-live-h">' + icon('camera', 18) + 'Live ' + TITLE + '</h2><span class="spacer"></span>' +
      '<label class="sr-only" for="hc-cam">Camera</label><select class="select select-sm" id="hc-cam" style="width:auto;max-width:340px"></select></div>' +
      '<div class="dl-controls" role="toolbar" aria-label="Counting controls">' +
      '<button class="btn btn-primary btn-sm" id="hc-start">' + icon('play', 15) + '<span>Start Counting</span></button>' +
      '<button class="btn btn-sm" id="hc-stop">' + icon('square', 15) + '<span>Stop Counting</span></button>' +
      '<button class="btn btn-sm" id="hc-pause">' + icon('power', 15) + '<span>Pause</span></button>' +
      '<button class="btn btn-sm" id="hc-snap">' + icon('snapshot', 15) + '<span>Capture Snapshot</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="hc-refresh">' + icon('refresh', 15) + '<span>Refresh Stream</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="hc-full">' + icon('maximize', 15) + '<span>Full Screen</span></button></div>' +
      '<div class="dl-stage" id="hc-stage"><div class="media-ph" id="hc-ph"></div><img class="dl-img" id="hc-img" alt="Annotated people counting stream" hidden>' +
      '<span class="badge badge-live dl-flag" id="hc-flag" hidden><span class="dot pulse"></span>LIVE · counting</span></div>' +
      '<p class="dim mt-8" style="font-size:12px">' + icon('info', 13) + ' Green boxes are counted people with their tracking ID; grey boxes are not confirmed yet. A person keeps one ID while visible and for the configured time after being hidden, so they are not counted again. Counting keeps running when you leave this page; use Stop to end it.</p>';
    el.querySelector('#hc-cam').addEventListener('change', onCameraChange);
    el.querySelector('#hc-start').addEventListener('click', onStart);
    el.querySelector('#hc-stop').addEventListener('click', onStop);
    el.querySelector('#hc-pause').addEventListener('click', onPause);
    el.querySelector('#hc-snap').addEventListener('click', onSnap);
    el.querySelector('#hc-refresh').addEventListener('click', function () { detachStream(); paintLive(true); ui.toast('info', 'Stream refreshed'); });
    el.querySelector('#hc-full').addEventListener('click', function () { ui.fullscreen(root.querySelector('#hc-stage')); });
    el.querySelector('#hc-img').addEventListener('error', function () {
      if (!streamOn) return;
      detachStream();
      const ph = root && root.querySelector('#hc-ph');
      if (ph) ph.innerHTML = icon('wifiOff', 26) + '<b>Stream interrupted</b><span>The MJPEG stream closed. Use Refresh Stream to reconnect.</span>';
    });
    window.AV.icons.hydrate(el);
    renderCameraSelect();
  }

  function renderCameraSelect() {
    const sel = root && root.querySelector('#hc-cam');
    if (!sel) return;
    const cams = backendCams();
    sel.innerHTML = '<option value="">' + (cams.length ? 'Select a camera…' : (api.status.connected ? 'No backend cameras' : 'Backend unavailable')) + '</option>' + cams.map(function (c) {
      const reason = c.enabled === false ? 'disabled' : '';
      const st = ui.STATUS[ui.camStatus(c)] ? ui.STATUS[ui.camStatus(c)].label : c.status;
      return '<option value="' + esc(c.id) + '"' + (c.id === camId ? ' selected' : '') + (reason ? ' disabled' : '') + '>' + esc(c.name + ' · ' + c.id + ' · ' + (reason || st)) + '</option>';
    }).join('');
    sel.disabled = !api.status.connected;
    if (headCtl) headCtl.renderCameraSelect();
    root.querySelector('#hc-open-cam').href = camId ? '#/cameras/' + encodeURIComponent(camId) : '#/cameras';
  }

  function paintLive(force) {
    const k = stateKey(), ph = root.querySelector('#hc-ph');
    const can = api.status.connected && !!camId;
    const running = !!(status && status.startedAt && ['processing', 'paused', 'disconnected', 'connecting'].indexOf(status.state) >= 0);
    const set = function (id, dis, why) { const b = root.querySelector(id); b.disabled = dis; b.title = dis ? why : ''; };
    set('#hc-start', !can || running, !api.status.connected ? 'Backend not connected' : (!camId ? 'Select a camera first' : 'Counting is already running'));
    set('#hc-stop', !can || !running, 'Counting is not running');
    set('#hc-pause', !can || !(k === 'processing' || k === 'paused'), 'Start counting first');
    set('#hc-snap', !can || !running, 'Start counting first — snapshots come from the running annotated stream');
    set('#hc-refresh', !running, 'Nothing to refresh');
    const pb = root.querySelector('#hc-pause');
    pb.innerHTML = icon(k === 'paused' ? 'play' : 'power', 15) + '<span>' + (k === 'paused' ? 'Resume' : 'Pause') + '</span>';
    if (running && tab === 'live' && (force || !streamOn)) attachStream();
    if ((!running || tab !== 'live') && streamOn) detachStream();
    root.querySelector('#hc-flag').hidden = !(streamOn && k === 'processing');
    if (!streamOn) {
      const msg = {
        backend_unavailable: ['server', 'Backend unavailable', 'Start the detection backend to count people.'],
        no_camera: ['camera', 'No camera selected', 'Choose a camera configured in Camera Management.'],
        stopped: ['square', 'Counting stopped', 'Press Start Counting to connect to the camera and run the people counting AI.'],
        error: ['alertCircle', 'Backend error', (status && status.error && status.error.message) || 'See the status panel.'],
        connecting: ['camera', 'Connecting…', 'Opening the camera stream.']
      }[k] || ['camera', STATE_UI[k] ? STATE_UI[k][1] : k, ''];
      ph.innerHTML = icon(msg[0], 28) + '<b>' + esc(msg[1]) + '</b><span>' + esc(msg[2]) + '</span>';
      ph.classList.toggle('err', k === 'error');
    }
  }

  function attachStream() {
    const img = root.querySelector('#hc-img');
    img.src = api.resolveUrl('cameras/' + encodeURIComponent(camId) + '/stream.mjpeg') + '?v=' + Date.now();
    img.hidden = false;
    root.querySelector('#hc-ph').hidden = true;
    streamOn = true;
  }
  function detachStream() {
    if (!root) return;
    const img = root.querySelector('#hc-img');
    img.removeAttribute('src');   // closes the MJPEG connection -> backend releases this viewer
    img.hidden = true;
    root.querySelector('#hc-ph').hidden = false;
    streamOn = false;
  }

  // ======================= controls =======================
  async function act(btn, label, fn) {
    if (busy) return;
    busy = true;
    ui.setBusy(btn, true, label);
    try { await fn(); }
    catch (e) { ui.toast('error', TITLE + ': ' + (e.code || 'error'), e.message); }
    finally { busy = false; if (root) { ui.setBusy(btn, false); await refresh(false); loadHistory(); } }
  }

  function onCameraChange(e) { switchCamera(e.target.value); }

  async function switchCamera(next) {
    const prev = camId;
    if (next === prev) return;
    if (prev && status && ['processing', 'paused', 'disconnected'].indexOf(stateKey()) >= 0) {
      const stop = await ui.confirm({ title: 'Stop counting on ' + prev + '?', message: TITLE + ' is running on ' + prev + '. Stop it before switching, or keep it running in the background (statistics and alerts continue).', confirmText: 'Stop and switch', cancelText: 'Keep running' });
      if (stop) { try { await api.detection.stop(MOD, prev); ui.toast('success', TITLE + ' stopped on ' + prev); } catch (err) { ui.toast('error', 'Stop failed', err.message); } }
    }
    detachStream();
    camId = next;
    state.setPref('countCamera', camId);
    if (headCtl) headCtl.setCamera(camId);
    renderCameraSelect();
    status = null; history = [];
    renderHistory();
    refresh(true);
  }

  function onStart(e) {
    const cam = selectedCam();
    if (!cam) { ui.toast('warning', 'Select a camera first'); return; }
    if (cam.enabled === false) { ui.toast('warning', 'Camera disabled', 'Enable it in Camera Management.'); return; }
    act(e.currentTarget, 'Starting…', async function () {
      status = { state: 'connecting', cameraState: 'connecting' }; renderStatus();
      const r = await api.detection.start(MOD, camId);
      ui.toast('success', r.message || (TITLE + ' started'), (r.sharedWith && r.sharedWith.length ? 'Sharing the camera with ' + r.sharedWith.join(', ') + '. ' : '') + 'Counting people on ' + cam.name + '.');
      await state.refreshFromBackend(['cameras', 'modules']);
    });
  }
  function onStop(e) {
    act(e.currentTarget, 'Stopping…', async function () {
      detachStream();
      const r = await api.detection.stop(MOD, camId);
      ui.toast('success', r.message || (TITLE + ' stopped'), 'The statistics of this run stay in the history.');
      await state.refreshFromBackend(['cameras', 'modules']);
    });
  }
  function onPause(e) {
    const pausing = stateKey() !== 'paused';
    act(e.currentTarget, pausing ? 'Pausing…' : 'Resuming…', async function () {
      await api.detection.pause(MOD, camId, pausing);
      ui.toast('info', TITLE + (pausing ? ' paused' : ' resumed'), pausing ? 'The stream continues; nobody is counted while paused.' : '');
    });
  }
  function onSnap(e) {
    act(e.currentTarget, 'Saving…', async function () {
      const snap = await api.detection.snapshot(MOD, camId);
      state.addSnapshotFromBackend(snap);
      ui.toast('success', 'Snapshot saved', snap.path ? snap.path : snap.id);
    });
  }

  // ======================= settings =======================
  function renderSettings() {
    const el = root.querySelector('#hc-settings');
    if (!cfgResp) {
      el.innerHTML = '<div class="card-head"><h2 id="hc-set-h">' + icon('sliders', 18) + 'Counting Settings</h2></div><p class="muted" style="font-size:13px">' +
        (api.status.connected ? 'Select a camera to load its ' + TITLE + ' settings.' : 'Settings are stored on the backend per camera.') + '</p>';
      return;
    }
    const c = cfgResp.config, zones = cfgResp.zones || [], cnt = c.counting || {}, al = c.alert || {};
    const num = function (name, v, min, max, step, w) { return '<input class="input input-sm" type="number" name="' + name + '" id="hc-' + name + '" value="' + esc(v) + '" min="' + min + '" max="' + max + '" step="' + step + '" style="width:' + (w || 84) + 'px">'; };
    el.innerHTML = '<div class="card-head"><h2 id="hc-set-h">' + icon('sliders', 18) + 'Counting Settings</h2><span class="spacer"></span><span class="badge badge-info" title="Saved on the backend for this camera">' + esc(cfgResp.cameraId) + '</span></div>' +
      '<form id="hc-form" class="stack" novalidate>' +
      '<div class="field"><label for="hc-conf">Person confidence</label><div class="range-row"><input class="range" type="range" id="hc-conf" name="confidence" min="0.20" max="0.90" step="0.01" value="' + Number(c.confidence).toFixed(2) + '" data-range-out="hc-conf-out"><output class="range-val" id="hc-conf-out"></output></div>' +
      '<span class="field-hint">A new person needs this score; weaker detections (≥ 70 % of it) can only continue someone already being tracked. Default 0.40.</span></div>' +
      '<div class="form-grid"><div class="field"><label for="hc-minFrames">Frames to confirm a person</label>' + num('minFrames', cnt.minFrames != null ? cnt.minFrames : 3, 1, 10, 1) +
      '<span class="field-hint">Detections in this many analysed frames before someone is counted (filters flicker). Default 3.</span></div>' +
      '<div class="field"><label for="hc-keepLostSec">Keep ID when hidden (s)</label>' + num('keepLostSec', cnt.keepLostSec != null ? cnt.keepLostSec : 2, 0.2, 30, 0.1) +
      '<span class="field-hint">A person hidden for less than this keeps the same ID and is not counted again. Default 2 s.</span></div></div>' +
      '<div class="field"><span class="field-label">Counting zones</span>' + (zones.length ? '<div class="row wrap" style="gap:12px">' + zones.map(function (z) {
        return '<label class="check"><input type="checkbox" name="zone" value="' + esc(z.id) + '" ' + ((c.zones || []).indexOf(z.id) >= 0 ? 'checked' : '') + (z.enabled === false ? ' disabled' : '') + '><span><span class="dot" style="color:' + esc(z.color || '#13D99A') + '"></span> ' + esc(z.name) + '</span></label>';
      }).join('') + '</div><span class="field-hint">None selected = whole frame. With zones, people are counted where they stand (bottom-centre of the box).</span>'
        : '<span class="field-hint">No zones drawn for this camera. <a href="#/cameras/' + encodeURIComponent(cfgResp.cameraId) + '/edit">Draw zones in Camera Management</a>.</span>') + '</div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Occupancy alert</b>' + ui.switchHtml('hc-alert-on', !!al.enabled, 'Raise an alert when too many people are in view', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label for="hc-threshold" class="dim" style="font-size:12.5px">People now ≥</label>' + num('threshold', al.threshold || 0, 0, 1000, 1, 76) +
      '<label for="hc-sev" class="dim" style="font-size:12.5px">Severity</label><select class="select select-sm" id="hc-sev" name="severity" style="width:auto">' +
      ['low', 'medium', 'high', 'critical'].map(function (s) { return '<option value="' + s + '"' + (al.severity === s ? ' selected' : '') + '>' + ui.cap(s) + '</option>'; }).join('') + '</select>' +
      '<label for="hc-alertCool" class="dim" style="font-size:12.5px">Cooldown (s)</label>' + num('alertCool', al.cooldownSec != null ? al.cooldownSec : 60, 0, 86400, 1).replace('id="hc-alertCool"', 'id="hc-alertCool"') + '</div>' +
      '<span class="field-hint">0 = off. The alert carries a snapshot of the frame that reached the threshold.</span></div>' +
      '<div class="field-error" id="hc-form-err"></div>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">' + icon('save', 14) + '<span>Apply settings</span></button><button class="btn btn-sm btn-ghost" type="button" id="hc-reset">Reset to defaults</button></div>' +
      '<p class="dim" style="font-size:12px">Settings apply live to the running counter — no restart needed.</p></form>';
    window.AV.modules.wireRanges(el);
    window.AV.icons.hydrate(el);
    const form = el.querySelector('#hc-form');
    form.addEventListener('submit', function (e) { e.preventDefault(); const p = readForm(form); if (p) saveSettings(p, form.querySelector('[type=submit]')); });
    el.querySelector('#hc-reset').addEventListener('click', function () {
      ui.confirm({ title: 'Reset ' + TITLE + ' settings?', message: 'Restore the default counting settings (confidence 0.40, 3 frames, keep ID 2 s, occupancy alert off) for ' + cfgResp.cameraId + '.', confirmText: 'Reset' })
        .then(function (ok) { if (ok) saveSettings(JSON.parse(JSON.stringify(cfgResp.defaults)), null); });
    });
    el.querySelector('#hc-alert-on').addEventListener('change', function (e) { saveSettings({ alert: { enabled: e.target.checked } }, null); });
  }

  function readForm(form) {
    const q = function (n) { return form.querySelector('[name="' + n + '"]'); };
    const err = root.querySelector('#hc-form-err');
    const mf = parseInt(q('minFrames').value, 10), kl = parseFloat(q('keepLostSec').value), th = parseInt(q('threshold').value, 10), cd = parseFloat(q('alertCool').value);
    const bad = !(mf >= 1 && mf <= 10) ? 'Frames to confirm must be 1 – 10.' : !(kl >= 0.2 && kl <= 30) ? 'Keep ID must be 0.2 – 30 s.' : !(th >= 0 && th <= 1000) ? 'Threshold must be 0 – 1000 people.' : !(cd >= 0 && cd <= 86400) ? 'Cooldown must be 0 – 86400 s.' : '';
    if (bad) { err.textContent = bad; return null; }
    return {
      confidence: Math.round(parseFloat(q('confidence').value) * 100) / 100,
      counting: { minFrames: mf, keepLostSec: Math.round(kl * 10) / 10 },
      zones: Array.prototype.map.call(form.querySelectorAll('[name="zone"]:checked'), function (c) { return c.value; }),
      alert: { enabled: root.querySelector('#hc-alert-on').checked, threshold: th, severity: q('severity').value, cooldownSec: cd }
    };
  }

  async function saveSettings(patch, btn) {
    const err = root.querySelector('#hc-form-err');
    if (btn) ui.setBusy(btn, true, 'Applying…');
    try {
      const r = await api.detection.saveConfig(MOD, camId, patch);
      cfgResp.config = r.config;
      if (err) err.textContent = '';
      ui.toast('success', TITLE + ' settings applied', 'Saved on the backend for ' + camId + (stateKey() === 'processing' ? ' and applied to the running counter.' : '.'));
      renderSettings();
    } catch (e) {
      if (err) err.textContent = e.message;
      ui.toast('error', 'Settings not applied', e.message);
      cfgResp = await api.detection.config(MOD, camId).catch(function () { return cfgResp; });
      renderSettings();
    } finally { if (btn && btn.isConnected) ui.setBusy(btn, false); }
  }

  // ======================= history =======================
  async function loadHistory() {
    if (!root || !api.status.connected || !camId) return;
    try {
      const r = await api.detection.history(camId, 60);
      history = (r && r.minutes) || [];
    } catch (e) { history = []; }
    renderHistory();
  }

  function renderHistory() {
    const el = root.querySelector('#hc-history');
    el.innerHTML = '<div class="card-head"><h2 id="hc-hist-h">Count history <span class="dim" style="font-weight:500;font-size:13px">· last 60 minutes, per minute</span></h2><span class="spacer"></span>' +
      '<div class="segmented" role="group" aria-label="History view"><button type="button" data-hv="chart" aria-pressed="' + (histView === 'chart') + '">Chart</button><button type="button" data-hv="table" aria-pressed="' + (histView === 'table') + '">Table</button></div></div>' +
      (history.length ? (histView === 'chart' ? chartHtml(history) : tableHtml(history))
        : ui.emptyState('activity', 'No count history yet', !camId ? 'Select a camera.' : 'Each completed minute of counting is saved on the backend and shown here.')) +
      '<h3 class="hc-sub-h">Recently counted people</h3><div id="hc-recent"></div>';
    el.querySelectorAll('[data-hv]').forEach(function (b) { b.addEventListener('click', function () { histView = b.dataset.hv; renderHistory(); }); });
    bindChartHover(el);
    renderRecent();
  }

  function renderRecent() {
    const el = root && root.querySelector('#hc-recent');
    if (!el) return;
    const c = counts(), recent = (c && c.recent) || [];
    const html = (recent.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>ID</th><th>Counted at</th><th>Confidence</th><th>Position in frame</th></tr></thead><tbody>' + recent.map(function (r) {
        const b = r.box || [];
        return '<tr><td class="mono">#' + esc(r.trackId) + '</td><td>' + esc(ui.fmtTime(r.countedAt)) + '</td><td>' + Number(r.confidence).toFixed(2) + '</td><td class="dim">' + (b.length ? Math.round((b[0] + b[2]) * 50) + '% across · ' + Math.round(b[3] * 100) + '% down' : '—') + '</td></tr>';
      }).join('') + '</tbody></table></div>'
        : '<p class="muted" style="font-size:13px">' + (stateKey() === 'processing' ? 'Nobody counted yet in this run.' : 'Start counting to list the people as they are counted.') + '</p>');
    if (el.innerHTML !== html) el.innerHTML = html;
  }

  /** Bars = most people at once in the minute; line = average people in the minute. One axis (people). */
  function chartHtml(data) {
    // fixed 60-minute timeline ending at the latest minute; minutes without data stay empty
    const SLOTS = 60, last = new Date(data[data.length - 1].t).getTime(), byMin = {};
    data.forEach(function (r) { byMin[Math.round(new Date(r.t).getTime() / 60000)] = r; });
    const base = Math.round(last / 60000) - SLOTS + 1;
    const rows = []; for (let k = 0; k < SLOTS; k++) rows.push(byMin[base + k] || { t: new Date((base + k) * 60000).toISOString(), max: 0, avg: 0, new: 0, empty: true });
    const W = 720, H = 210, L = 34, R = 10, T = 12, B = 26;
    const n = rows.length, max = Math.max(1, Math.max.apply(null, rows.map(function (r) { return r.max; })));
    const top = Math.max(2, Math.ceil(max * 1.15));
    const bw = (W - L - R) / n, x = function (i) { return L + i * bw; }, y = function (v) { return T + (H - T - B) * (1 - v / top); };
    const ticks = [0, Math.round(top / 2), top];
    let svg = '<svg class="hc-chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="People per minute, last ' + n + ' minutes">' +
      ticks.map(function (t) { return '<line class="hc-grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(t) + '" y2="' + y(t) + '"/><text class="hc-axis" x="' + (L - 6) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + t + '</text>'; }).join('');
    rows.forEach(function (r, i) {
      const h = Math.max(0, y(0) - y(r.max)), bx = x(i) + 1, w = Math.max(1, bw - 2);
      const rad = Math.min(4, w / 2, h);
      if (h > 0) svg += '<path class="hc-bar" d="M' + bx + ' ' + y(0) + 'V' + (y(r.max) + rad) + 'q0 -' + rad + ' ' + rad + ' -' + rad + 'H' + (bx + w - rad) + 'q' + rad + ' 0 ' + rad + ' ' + rad + 'V' + y(0) + 'Z"/>';
      if (!r.empty) svg += '<rect class="hc-hit" data-i="' + i + '" x="' + x(i) + '" y="' + T + '" width="' + bw + '" height="' + (H - T - B) + '"/>';
    });
    // average line, broken where minutes have no data
    let seg = [];
    const flush = function () { if (seg.length > 1) svg += '<polyline class="hc-avg" points="' + seg.join(' ') + '"/>'; else if (seg.length === 1) svg += '<circle class="hc-avg-dot" cx="' + seg[0].split(',')[0] + '" cy="' + seg[0].split(',')[1] + '" r="2.5"/>'; seg = []; };
    rows.forEach(function (r, i) { if (r.empty) flush(); else seg.push((x(i) + bw / 2).toFixed(1) + ',' + y(r.avg).toFixed(1)); });
    flush();
    const hhmm = function (t) { const d = new Date(t); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
    const lab = function (i, anchor) { return '<text class="hc-axis" x="' + (anchor === 'end' ? x(i) + bw : anchor === 'start' ? x(i) : x(i) + bw / 2) + '" y="' + (H - 8) + '" text-anchor="' + (anchor || 'middle') + '">' + esc(hhmm(rows[i].t)) + '</text>'; };
    svg += lab(0, 'start') + lab(Math.floor(n / 2)) + lab(n - 1, 'end') + '</svg>';
    window.AV._hcRows = rows;
    return '<div class="hc-legend"><span><i class="hc-key hc-key-bar"></i>Most people at once</span><span><i class="hc-key hc-key-avg"></i>Average people</span><span class="dim">· total counted in the period: ' +
      data.reduce(function (s, r) { return s + (r.new || 0); }, 0) + '</span></div><div class="hc-chart-wrap">' + svg + '<div class="hc-tip" hidden></div></div>';
  }

  function bindChartHover(el) {
    const wrap = el.querySelector('.hc-chart-wrap'); if (!wrap) return;
    const tip = wrap.querySelector('.hc-tip'), rows = window.AV._hcRows || [];
    wrap.querySelectorAll('.hc-hit').forEach(function (h) {
      h.addEventListener('mouseenter', function () {
        const r = rows[+h.dataset.i]; if (!r) return;
        tip.innerHTML = '<b>' + esc(ui.fmtTime(r.t, { noYear: true })) + (r.partial ? ' <span class="dim">(running)</span>' : '') + '</b><span>Most at once: ' + r.max + '</span><span>Average: ' + Number(r.avg).toFixed(1) + '</span><span>Newly counted: ' + (r.new || 0) + '</span>';
        const box = wrap.getBoundingClientRect(), hb = h.getBoundingClientRect();
        tip.style.left = Math.min(box.width - 170, Math.max(0, hb.left - box.left + hb.width / 2 - 80)) + 'px';
        tip.hidden = false; h.classList.add('on');
      });
      h.addEventListener('mouseleave', function () { tip.hidden = true; h.classList.remove('on'); });
    });
  }

  function tableHtml(rows) {
    return '<div class="table-wrap" style="max-height:260px;overflow:auto"><table class="table"><thead><tr><th>Minute</th><th>Most at once</th><th>Average</th><th>Newly counted</th></tr></thead><tbody>' +
      rows.slice().reverse().map(function (r) { return '<tr><td>' + esc(ui.fmtTime(r.t, { noYear: true })) + (r.partial ? ' <span class="badge badge-info">running</span>' : '') + '</td><td>' + r.max + '</td><td>' + Number(r.avg).toFixed(1) + '</td><td>' + (r.new || 0) + '</td></tr>'; }).join('') + '</tbody></table></div>';
  }

  function destroy() {
    clearInterval(timer); clearInterval(histTimer);
    detachStream();
    if (headCtl) { headCtl.destroy(); headCtl = null; }
    unsub.forEach(function (f) { f(); }); unsub = [];
    root = null;
  }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.humanCount = { title: TITLE, render: render, destroy: destroy, onBackend: function () { onBackend(); } };
})();
