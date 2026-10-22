/* PYROTECH VISION — Vehicle Detection, Human Detection and Fire & Smoke Detection pages (live, backend-driven).
 *
 * Both pages talk to the existing person + vehicle pipeline through /api/detections/{module}/*.
 * The two modules are configured, started, stopped, snapshotted and alerted independently,
 * but share one camera connection and one inference pass per camera on the backend.
 * Nothing here is simulated: with no backend, the page says so and every control is disabled.
 */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog;
  const esc = ui.esc;

  const MOD = {
    vehicle: { id: 'vehicle', title: 'Vehicle Detection', icon: 'car', noun: 'vehicle',
      classes: [['twoWheeler', '2-Wheeler', 'bicycle + motorcycle'], ['fourWheeler', '4-Wheeler', 'car'], ['bus', 'Bus', ''], ['truck', 'Truck', '']] },
    human: { id: 'human', title: 'Human Detection', icon: 'user', noun: 'person',
      classes: [['person', 'Person', ''], ['rider', 'Rider', 'person on a 2-wheeler'], ['personFar', 'Person (far)', 'extra upscaled pass, slower']] },
    // Fire & Smoke: its own model + smoke rules on the same camera session (backend/detection/fire_smoke.py)
    fire: { id: 'fire', title: 'Fire & Smoke Detection', icon: 'flame', noun: 'fire / smoke',
      sub: 'Fire & smoke model firedetect-11s · smoke verifier (smoke / cloud / dust) · smoke rules · alert after M of the last K analysed frames',
      classes: [['fire', 'Fire', ''], ['smoke', 'Smoke', '']] }
  };
  function modTitle(x) { return MOD[x] ? MOD[x].title : ((cat.moduleById(x) || {}).long || x); }
  const STATE_UI = {
    backend_unavailable: ['badge-muted', 'Backend unavailable'], no_camera: ['badge-muted', 'No camera selected'],
    stopped: ['badge-muted', 'Stopped'], connecting: ['badge-info', 'Connecting'], connected: ['badge-info', 'Connected'],
    processing: ['badge-success', 'Processing'], paused: ['badge-warning', 'Paused'], disconnected: ['badge-danger', 'Disconnected'],
    error: ['badge-danger', 'Backend error']
  };

  let root, mod, camId, status, cfgResp, timer, evTimer, busy = false, streamOn = false, unsub = [], lastEvSig = '', pollFails = 0, retryN = 0, retryTimer = null;

  function prefKey() { return 'detCamera_' + mod.id; }

  function render(container, params) {
    root = container;
    mod = MOD[params.module];
    camId = state.prefs()[prefKey()] || '';
    status = null; cfgResp = null; streamOn = false; lastEvSig = '';
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<a href="#/detections/all">Detections</a>' + icon('chevronRight', 14) + '<span class="current">' + mod.title + '</span></nav>' +
      '<div class="page-head"><span class="head-icon" style="color:' + cat.moduleById(mod.id).color + '">' + icon(mod.icon, 24) + '</span><div><h1>' + mod.title + '</h1>' +
      '<p class="sub">' + (mod.sub || ('Existing YOLOv8 + ByteTrack pipeline · ' + mod.classes.map(function (c) { return c[1]; }).join(', '))) + '</p></div>' +
      '<div class="actions"><a class="btn" id="dl-open-cam" href="#/cameras">' + icon('monitor', 16) + '<span>Open camera</span></a><a class="btn btn-ghost" href="#/alerts">' + icon('bell', 16) + '<span>Alerts</span></a></div></div>' +
      '<div id="dl-notice"></div>' +
      '<div class="dl-grid">' +
      '<section class="card dl-live" aria-labelledby="dl-live-h"></section>' +
      '<div class="dl-side"><section class="card" id="dl-status" aria-labelledby="dl-status-h"></section><section class="card" id="dl-settings" aria-labelledby="dl-set-h"></section></div>' +
      '<section class="card dl-events" id="dl-events" aria-labelledby="dl-ev-h"></section></div>';
    renderLive();
    renderStatus();
    renderSettings();
    renderEvents([]);
    unsub.push(state.on('backend', onBackend), state.on('cameras', function () { renderCameraSelect(); }));
    onBackend();
  }

  function backendCams() { return state.cameras().filter(function (c) { return c.source === 'backend'; }); }
  function selectedCam() { return camId ? state.getCamera(camId) : null; }

  function onBackend() {
    if (!root) return;
    const n = root.querySelector('#dl-notice');
    if (!api.status.connected) {
      n.innerHTML = '<div class="notice notice-warning mb-12">' + icon('server', 16) + '<div><b>Backend not connected.</b> ' + mod.title + ' runs on the detection backend in this repository. Start it with <code class="mono">start-backend.cmd</code> (or <code class="mono">python backend/main.py</code>). <a href="#/settings">Backend settings</a></div></div>';
    } else if (!backendCams().length) {
      n.innerHTML = '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div>No cameras are registered on the backend yet. <a href="#/cameras/new">Add a camera</a> in Camera Management — this page uses that configuration (no separate credentials here).</div></div>';
    } else n.innerHTML = '';
    if (camId && !backendCams().some(function (c) { return c.id === camId; })) camId = '';
    renderCameraSelect();
    refresh(true);
    clearInterval(timer); clearInterval(evTimer);
    if (api.status.connected) {
      timer = setInterval(function () { if (!document.hidden) refresh(false); }, 2000);
      evTimer = setInterval(function () { if (!document.hidden) loadEvents(); }, 5000);
    }
  }

  // ======================= A. status =======================
  async function refresh(withConfig) {
    if (!root) return;
    if (!api.status.connected) { status = { state: 'backend_unavailable' }; paint(); return; }
    if (!camId) { status = { state: 'no_camera' }; cfgResp = null; paint(); renderSettings(); renderEvents([]); return; }
    try {
      status = await api.detection.status(mod.id, camId);
      pollFails = 0;
      if (withConfig) { cfgResp = await api.detection.config(mod.id, camId); renderSettings(); loadEvents(); }
    } catch (e) {
      // a single slow / failed status poll keeps the running stream and the last status (no flicker);
      // the error is shown after 3 failures in a row or when it is a real backend answer (HTTP error)
      pollFails++;
      if (streamOn && status && status.startedAt && e.kind !== 'http' && pollFails < 3) {
        console.warn('[PYROTECH] status poll failed (' + pollFails + '), keeping the live view:', e.message);
        return;
      }
      status = { state: 'error', error: { code: e.code || e.kind, message: e.message } };
    }
    paint();
  }

  function stateKey() {
    if (!status) return 'connecting';
    if (status.state === 'stopped' && status.cameraState && ['connected', 'processing'].indexOf(status.cameraState) >= 0) return 'stopped';
    return status.state || 'stopped';
  }

  function paint() { if (!root || busy) return; renderStatus(); paintLive(); }

  function fmt(v, unit) { return v === null || v === undefined ? '<span class="dim">Unavailable</span>' : esc(v) + (unit || ''); }
  function counts(obj) {
    const keys = Object.keys(obj || {}).filter(function (k) { return obj[k]; });
    return keys.length ? keys.map(function (k) { return '<span class="badge badge-muted">' + esc(k) + ' <b>' + obj[k] + '</b></span>'; }).join(' ') : '<span class="dim">0</span>';
  }

  /** AI frame sampling of this module (backend scheduler): interval, measured analyses per second, and what
   *  happened to the other frames - all counters measured by the backend, never the configured value. */
  function schedRow(m, modId) {
    const sc = m.schedule && m.schedule[modId];
    if (!sc) return '';
    const every = sc.intervalSec ? 'one frame every ' + sc.intervalSec + ' s' : 'every frame';
    const fps = sc.aiFps != null ? esc(sc.aiFps) + ' analyses/s <span class="dim">(measured, last ' + esc(sc.aiFpsWindowSec) + ' s)</span>' : '<span class="dim">measuring…</span>';
    const parts = [esc(sc.framesProcessed) + ' analysed', esc(sc.skippedSampling) + ' skipped by interval'];
    if (sc.motionGate) parts.push(esc(sc.skippedNoMotion) + ' AI calls avoided (no motion)', esc(sc.fallbackRuns) + ' fallback checks');
    if (sc.rejectedStale) parts.push(esc(sc.rejectedStale) + ' stale');
    if (sc.replacedQueue) parts.push(esc(sc.replacedQueue) + ' older queued frames replaced');
    if (m.framesSkippedCapture) parts.push(esc(m.framesSkippedCapture) + ' above ' + esc(m.captureFpsTarget) + ' fps cap');
    return '<dt>AI sampling</dt><dd>' + every + (sc.motionGate ? ' <span class="badge badge-muted">motion-triggered</span>' : '') + ' · ' + fps +
      '<div class="dim" style="font-size:11.5px">' + parts.join(' · ') + (sc.lastFrameAgeMs != null ? ' · last frame ' + esc(sc.lastFrameAgeMs) + ' ms old at analysis (frame #' + esc(sc.lastFrameId) + ')' : '') + '</div></dd>';
  }

  /** Frames received from the camera / analysed by the AI / skipped while the AI was busy (all measured). */
  function framesLine(m) {
    if (!m.framesReceived) return '<span class="dim">Unavailable</span>';
    const queue = m.framePolicy === 'queue';
    const total = (m.framesProcessed || 0) + (m.framesDropped || 0);
    const pct = total ? Math.round(m.framesDropped / total * 100) : 0;
    return esc(m.framesReceived) + ' received since connect · ' + esc(m.framesProcessed || 0) + ' analysed · ' + esc(m.framesDropped || 0) +
      (queue ? ' dropped (queue full)' : ' skipped') + (total ? ' <span class="dim">(' + pct + '%)</span>' : '') +
      (m.cameraGaps ? ' <span class="dim">· ' + esc(m.cameraGaps) + ' missing in the camera stream</span>' : '');
  }
  /** Which stream the AI analyses (substream for detection, main for viewing / HD snapshots). */
  function aiStreamLine(m) {
    const a = m.aiStream || {};
    const names = { same: 'Viewing stream', sub: 'Substream', custom: 'Custom AI stream', main: 'Main stream' };
    let out = esc(names[a.active] || 'Viewing stream') + (m.aiResolution ? ' <span class="dim">· ' + esc(m.aiResolution) + '</span>' : '');
    if (a.requested && a.requested !== 'same' && a.active === 'main') out += ' <span class="badge badge-warning">Fallback</span> <span class="dim">' + esc(names[a.requested]) + ' not available: ' + esc(a.reason || '') + '</span>';
    if (m.viewResolution) out += ' <span class="dim">· main stream ' + esc(m.viewResolution) + ' for HD snapshots</span>';
    return out;
  }
  /** Which stage limits the frame rate, from the measured numbers only. */
  function bottleneck(m, k) {
    if (k !== 'processing' || m.cameraFps == null) return '<span class="dim">Shown while detection is running</span>';
    if (m.streamFps && m.cameraFps < m.streamFps * 0.85)
      return '<span class="badge badge-warning">Camera / network / decoding</span> <span class="dim">receiving ' + esc(m.cameraFps) + ' of ' + esc(m.streamFps) + ' fps the stream announces</span>';
    if (m.framePolicy === 'queue' && m.queueCapacity && m.queueLen >= m.queueCapacity * 0.8)
      return '<span class="badge badge-warning">AI inference (overloaded)</span> <span class="dim">frame queue ' + esc(m.queueLen) + ' of ' + esc(m.queueCapacity) + ' frames; the oldest are dropped and counted</span>';
    if (m.processingFps != null && m.processingFps >= m.cameraFps * 0.9)
      return '<span class="badge badge-success">None</span> <span class="dim">the AI analyses every frame the camera sends</span>';
    return '<span class="badge badge-warning">AI inference</span> <span class="dim">' + esc(m.loopMs != null ? m.loopMs : m.inferenceMs) + ' ms per analysed frame, the camera sends a frame every ' +
      (m.cameraFps ? Math.round(1000 / m.cameraFps) : '?') + ' ms</span>';
  }

  function renderStatus() {
    const el = root.querySelector('#dl-status');
    const s = status || {}, k = stateKey(), su = STATE_UI[k] || STATE_UI.stopped, m = s.metrics || {};
    const cam = selectedCam();
    el.innerHTML = '<div class="card-head"><h2 id="dl-status-h">Detection Status</h2><span class="spacer"></span><span class="badge ' + su[0] + '">' + (k === 'processing' ? '<span class="dot pulse"></span>' : '') + su[1] + '</span></div>' +
      '<dl class="kv dl-kv">' +
      '<dt>Backend</dt><dd>' + (api.status.connected ? '<span class="badge badge-success">Connected</span>' : '<span class="badge badge-muted">Not connected</span>') + '</dd>' +
      '<dt>Camera</dt><dd>' + (cam ? esc(cam.name) + ' <span class="mono dim">' + esc(cam.id) + '</span>' : '<span class="dim">None selected</span>') + '</dd>' +
      '<dt>Camera IP</dt><dd>' + (cam ? ui.ipHtml(cam) : '<span class="dim">No camera selected</span>') + '</dd>' +
      '<dt>Camera stream</dt><dd>' + esc({ processing: 'Receiving frames', connected: 'Receiving frames', disconnected: 'Disconnected — reconnecting', error: 'Error', not_connected: 'Not connected', connecting: 'Connecting' }[s.cameraState] || (s.cameraState ? s.cameraState : '—')) + '</dd>' +
      '<dt>Detection engine</dt><dd>' + esc(su[1]) + (s.modelAvailable === false ? ' <span class="badge badge-danger">Model file missing</span>' : (s.modelFile ? ' <span class="dim">' + esc(s.modelFile) + '</span>' : '')) +
      (s.sharedWith && s.sharedWith.length ? '<div class="dim" style="font-size:12px">' + (mod.id === 'fire' ? 'Sharing the camera connection with: ' : 'Sharing the camera\'s inference pass with: ') + esc(s.sharedWith.map(modTitle).join(', ')) + '</div>' : '') + '</dd>' +
      '<dt>In view now</dt><dd>' + (k === 'processing' ? counts(s.currentCounts) : '<span class="dim">—</span>') + '</dd>' +
      (mod.id === 'fire' ? fireRows(s) :
      '<dt>Confirmed tracks</dt><dd>' + (s.uniqueCounts && Object.keys(s.uniqueCounts).length ? counts(s.uniqueCounts) + '<div class="dim" style="font-size:11.5px">Since start. ByteTrack can re-assign an ID after occlusion.</div>' : '<span class="dim">—</span>') + '</dd>') +
      '<dt>Camera FPS (received)</dt><dd>' + fmt(m.cameraFps, ' fps') + (m.streamFps ? ' <span class="dim">· stream reports ' + esc(m.streamFps) + ' fps</span>' : '') + (m.resolution ? ' <span class="dim">· ' + esc(m.resolution) + '</span>' : '') + '</dd>' +
      '<dt>AI stream</dt><dd>' + aiStreamLine(m) + '</dd>' +
      (mod.id === 'fire'
        ? '<dt>Fire model FPS</dt><dd>' + fmt(m.fireAnalysedFps, ' fps analysed') + (s.fire && s.fire.engine ? ' <span class="dim">· ' + esc(s.fire.engine) + ', ' + esc(s.fire.inferenceMs) + ' ms</span>' : '') +
          (m.fireSkipped ? ' <span class="dim">· ' + esc(m.fireSkipped) + ' frames skipped while the model was busy</span>' : '') + '</dd>'
        : '<dt>AI processing FPS</dt><dd>' + fmt(m.processingFps, ' fps') + '</dd>') +
      schedRow(m, mod.id) +
      '<dt>Live view FPS</dt><dd>' + fmt(m.displayFps, ' fps sent') + (m.displayEncodeMs != null ? ' <span class="dim">· ' + esc(m.displayEncodeMs) + ' ms JPEG encode (own thread)</span>' : '') + '</dd>' +
      (m.lastDisconnectReason ? '<dt>Last disconnect</dt><dd class="dim">' + esc(m.lastDisconnectReason) + '</dd>' : '') +
      '<dt>Frames</dt><dd>' + framesLine(m) + '</dd>' +
      '<dt>AI inference time</dt><dd>' + fmt(m.inferenceMs, ' ms') + (m.inferenceMsRecent != null ? ' <span class="dim">· latest ' + esc(m.inferenceMsRecent) + ' ms</span>' : '') + (m.engine ? ' <span class="dim">· ' + esc(m.engine) + ' (' + esc(m.device || 'CPU') + (m.precision ? ', ' + esc(m.precision) : '') + (m.imgSize ? ', ' + esc(m.imgSize) + ' px' : '') + ')</span>' : '') + (m.loopMs != null ? ' <span class="dim">· ' + esc(m.loopMs) + ' ms per frame incl. tracking/drawing</span>' : '') + '</dd>' +
      (m.framePolicy === 'queue'
        ? '<dt>Queue / latency</dt><dd>' + esc(m.queueLen || 0) + ' of ' + fmt(m.queueCapacity, ' frames') + ' waiting <span class="dim">(peak ' + esc(m.queuePeak || 0) + ')</span> · ' + fmt(m.latencyMs, ' ms') + ' receive → analysed' + (m.latencyMsMax != null ? ' <span class="dim">(max ' + esc(m.latencyMsMax) + ' ms)</span>' : '') + (m.disconnections ? ' <span class="dim">· ' + m.disconnections + ' disconnection(s)</span>' : '') + '</dd>'
        : '<dt>Frame age at analysis</dt><dd>' + fmt(m.frameAgeMs, ' ms') + (m.disconnections ? ' <span class="dim">· ' + m.disconnections + ' disconnection(s)</span>' : '') + '</dd>') +
      '<dt>Bottleneck</dt><dd>' + bottleneck(m, k) + '</dd>' +
      '<dt>Last ' + mod.noun + ' detected</dt><dd>' + (s.lastDetectionAt ? esc(ui.fmtTime(s.lastDetectionAt)) + ' <span class="dim">(' + esc(ui.relTime(s.lastDetectionAt)) + ')</span>' : '<span class="dim">None yet</span>') + '</dd>' +
      '</dl>' + (s.error && s.error.message ? '<div class="notice notice-danger mt-12">' + icon('alertCircle', 16) + '<div><b>' + esc(s.error.code || 'Error') + '</b> ' + esc(s.error.message) + '</div></div>' : '');
  }

  /** Fire & Smoke status rows (alert rule, verifier, alerts since start) - all reported by the backend. */
  function fireRows(s) {
    const f = s.fire;
    if (!f) return '<dt>Alerts since start</dt><dd><span class="dim">—</span></dd>';
    const rule = Object.keys(f.rule || {}).map(function (k) { return k + ': ' + f.rule[k]; }).join(' · ');
    return '<dt>Alerts since start</dt><dd>' + counts(f.alerts) + (f.active && f.active.length ? ' <span class="badge badge-danger">' + esc(f.active.join(' + ')) + ' active</span>' : '') + '</dd>' +
      '<dt>Alert rule</dt><dd>' + esc(rule || '—') + '<div class="dim" style="font-size:11.5px">Thresholds ' + esc(Object.keys(f.thresholds || {}).map(function (k) { return k + ' ≥ ' + (f.thresholds[k] > 1 ? 'off' : f.thresholds[k]); }).join(', ')) + (f.smokeRules ? ' · weaker smoke only with fire nearby or rising/growing' : '') + '</div></dd>' +
      '<dt>Smoke verifier</dt><dd>' + (f.verifier === 'on' ? '<span class="badge badge-success">On</span> <span class="dim">smoke / cloud / dust check on every smoke box</span>' : '<span class="badge badge-muted">' + esc(f.verifier) + '</span>') + '</dd>' +
      (f.greenOverlay ? '<dt>Camera overlay</dt><dd><span class="badge badge-warning">Green boxes in the video</span> <span class="dim">erased before detection — better switch the camera\'s own target overlay off</span></dd>' : '') +
      (f.errors ? '<dt>Detection errors</dt><dd><span class="badge badge-danger">' + esc(f.errors) + '</span></dd>' : '');
  }

  // ======================= B + C. camera select + live =======================
  function renderLive() {
    const el = root.querySelector('.dl-live');
    el.innerHTML = '<div class="card-head"><h2 id="dl-live-h">' + icon('camera', 18) + 'Live ' + mod.title + '</h2><span class="spacer"></span>' +
      '<label class="sr-only" for="dl-cam">Camera</label><select class="select select-sm" id="dl-cam" style="width:auto;max-width:340px"></select></div>' +
      '<div class="dl-controls" role="toolbar" aria-label="Detection controls">' +
      '<button class="btn btn-primary btn-sm" id="dl-start">' + icon('play', 15) + '<span>Start Detection</span></button>' +
      '<button class="btn btn-sm" id="dl-stop">' + icon('square', 15) + '<span>Stop Detection</span></button>' +
      '<button class="btn btn-sm" id="dl-pause">' + icon('power', 15) + '<span>Pause</span></button>' +
      '<button class="btn btn-sm" id="dl-snap">' + icon('snapshot', 15) + '<span>Capture Snapshot</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="dl-refresh">' + icon('refresh', 15) + '<span>Refresh Stream</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="dl-full">' + icon('maximize', 15) + '<span>Full Screen</span></button></div>' +
      '<div class="dl-stage" id="dl-stage"><div class="media-ph" id="dl-ph"></div><img class="dl-img" id="dl-img" alt="Annotated ' + mod.noun + ' detection stream" hidden>' +
      '<span class="badge badge-live dl-flag" id="dl-flag" hidden><span class="dot pulse"></span>LIVE · annotated</span></div>' +
      '<p class="dim mt-8" style="font-size:12px">' + icon('info', 13) + (mod.id === 'fire' ? ' Red = fire, blue = smoke, thin grey = weak smoke (needs more evidence; "cloud?/dust?" = verifier verdict). A red frame and banner show while an alert is active. ' : ' Boxes, labels and confidences are drawn by the backend pipeline (MJPEG). ') + 'Detection keeps running when you leave this page; use Stop to end it.</p>';
    el.querySelector('#dl-cam').addEventListener('change', onCameraChange);
    el.querySelector('#dl-start').addEventListener('click', onStart);
    el.querySelector('#dl-stop').addEventListener('click', onStop);
    el.querySelector('#dl-pause').addEventListener('click', onPause);
    el.querySelector('#dl-snap').addEventListener('click', onSnap);
    el.querySelector('#dl-refresh').addEventListener('click', function () { detachStream(); paintLive(true); ui.toast('info', 'Stream refreshed'); });
    el.querySelector('#dl-full').addEventListener('click', function () { ui.fullscreen(root.querySelector('#dl-stage')); });
    el.querySelector('#dl-img').addEventListener('error', function () {
      if (!streamOn) return;
      detachStream();
      // reconnect automatically (2, 4, 8 … max 15 s) while detection is still running
      retryN = Math.min(retryN + 1, 4);
      const wait = Math.min(15, Math.pow(2, retryN));
      const ph = root && root.querySelector('#dl-ph');
      if (ph) ph.innerHTML = icon('wifiOff', 26) + '<b>Stream interrupted</b><span>Reconnecting in ' + wait + ' s… (or use Refresh Stream)</span>';
      clearTimeout(retryTimer);
      retryTimer = setTimeout(function () { if (root && !streamOn) refresh(false); }, wait * 1000);
    });
    el.querySelector('#dl-img').addEventListener('load', function () { retryN = 0; });
    window.AV.icons.hydrate(el);
    renderCameraSelect();
  }

  function renderCameraSelect() {
    const sel = root && root.querySelector('#dl-cam');
    if (!sel) return;
    const cams = backendCams();
    sel.innerHTML = '<option value="">' + (cams.length ? 'Select a camera…' : (api.status.connected ? 'No backend cameras' : 'Backend unavailable')) + '</option>' + cams.map(function (c) {
      const reason = c.enabled === false ? 'disabled' : (c.compatible === false ? 'not compatible' : '');
      const st = ui.STATUS[ui.camStatus(c)] ? ui.STATUS[ui.camStatus(c)].label : c.status;
      return '<option value="' + esc(c.id) + '"' + (c.id === camId ? ' selected' : '') + (reason ? ' disabled' : '') + '>' + esc(c.name + ' · ' + c.id + ' · ' + (reason || st)) + '</option>';
    }).join('');
    sel.disabled = !api.status.connected;
    const oc = root.querySelector('#dl-open-cam');
    oc.href = camId ? '#/cameras/' + encodeURIComponent(camId) : '#/cameras';
  }

  function paintLive(force) {
    const k = stateKey(), ph = root.querySelector('#dl-ph'), img = root.querySelector('#dl-img');
    const can = api.status.connected && !!camId;
    const running = !!(status && status.startedAt && ['processing', 'paused', 'disconnected', 'connecting'].indexOf(status.state) >= 0);
    const set = function (id, dis, why) { const b = root.querySelector(id); b.disabled = dis; b.title = dis ? why : ''; };
    set('#dl-start', !can || running, !api.status.connected ? 'Backend not connected' : (!camId ? 'Select a camera first' : 'Detection is already running'));
    set('#dl-stop', !can || !running, 'Detection is not running');
    set('#dl-pause', !can || !(k === 'processing' || k === 'paused'), 'Start detection first');
    set('#dl-snap', !can || !running, 'Start detection first — snapshots come from the running annotated stream');
    set('#dl-refresh', !running, 'Nothing to refresh');
    const pb = root.querySelector('#dl-pause');
    pb.innerHTML = icon(k === 'paused' ? 'play' : 'power', 15) + '<span>' + (k === 'paused' ? 'Resume' : 'Pause') + '</span>';
    if (running && (force || !streamOn)) attachStream();
    if (!running && streamOn) detachStream();
    root.querySelector('#dl-flag').hidden = !(streamOn && k === 'processing');
    if (!streamOn) {
      const msg = {
        backend_unavailable: ['server', 'Backend unavailable', 'Start the detection backend to use live detection.'],
        no_camera: ['camera', 'No camera selected', 'Choose a camera configured in Camera Management.'],
        stopped: ['square', 'Detection stopped', 'Press Start Detection to connect to the camera and run the ' + mod.noun + ' model.'],
        error: ['alertCircle', 'Backend error', (status && status.error && status.error.message) || 'See the status panel.'],
        connecting: ['camera', 'Connecting…', 'Opening the camera stream.']
      }[k] || ['camera', STATE_UI[k] ? STATE_UI[k][1] : k, ''];
      ph.innerHTML = icon(msg[0], 28) + '<b>' + esc(msg[1]) + '</b><span>' + esc(msg[2]) + '</span>';
      ph.classList.toggle('err', k === 'error');
    }
  }

  function attachStream() {
    const img = root.querySelector('#dl-img');
    // cache-buster so Refresh opens a new MJPEG connection; the backend still shares one session
    img.src = api.resolveUrl('cameras/' + encodeURIComponent(camId) + '/stream.mjpeg') + '?v=' + Date.now();
    img.hidden = false;
    root.querySelector('#dl-ph').hidden = true;
    streamOn = true;
  }
  function detachStream() {
    if (!root) return;
    const img = root.querySelector('#dl-img');
    img.removeAttribute('src');   // closes the MJPEG connection -> backend releases this viewer
    img.hidden = true;
    root.querySelector('#dl-ph').hidden = false;
    streamOn = false;
  }

  // ======================= controls =======================
  async function act(btn, label, fn) {
    if (busy) return;
    busy = true;
    ui.setBusy(btn, true, label);
    try { await fn(); }
    catch (e) { ui.toast('error', mod.title + ': ' + (e.code || 'error'), e.message); }
    finally { busy = false; if (root) { ui.setBusy(btn, false); await refresh(false); } }
  }

  async function onCameraChange(e) {
    const next = e.target.value;
    const prev = camId;
    if (prev && status && ['processing', 'paused', 'disconnected'].indexOf(stateKey()) >= 0) {
      const stop = await ui.confirm({ title: 'Stop detection on ' + prev + '?', message: mod.title + ' is running on ' + prev + '. Stop it before switching, or keep it running in the background (alerts and snapshots continue).', confirmText: 'Stop and switch', cancelText: 'Keep running' });
      if (stop) { try { await api.detection.stop(mod.id, prev); ui.toast('success', mod.title + ' stopped on ' + prev); } catch (err) { ui.toast('error', 'Stop failed', err.message); } }
    }
    detachStream();
    camId = next;
    state.setPref(prefKey(), camId);
    renderCameraSelect();
    status = null;
    refresh(true);
  }

  function onStart(e) {
    const cam = selectedCam();
    if (!cam) { ui.toast('warning', 'Select a camera first'); return; }
    if (cam.enabled === false) { ui.toast('warning', 'Camera disabled', 'Enable it in Camera Management.'); return; }
    act(e.currentTarget, 'Starting…', async function () {
      status = { state: 'connecting', cameraState: 'connecting' }; renderStatus();
      const r = await api.detection.start(mod.id, camId);
      ui.toast('success', r.message || (mod.title + ' started'), (r.sharedWith && r.sharedWith.length ? 'Sharing the camera with ' + r.sharedWith.join(', ') + '.' : '') + ' Processing ' + cam.name + '.');
      await state.refreshFromBackend(['cameras', 'modules']);
    });
  }
  function onStop(e) {
    act(e.currentTarget, 'Stopping…', async function () {
      detachStream();
      const r = await api.detection.stop(mod.id, camId);
      ui.toast('success', r.message || (mod.title + ' stopped'));
      await state.refreshFromBackend(['cameras', 'modules']);
    });
  }
  function onPause(e) {
    const pausing = stateKey() !== 'paused';
    act(e.currentTarget, pausing ? 'Pausing…' : 'Resuming…', async function () {
      await api.detection.pause(mod.id, camId, pausing);
      ui.toast('info', mod.title + (pausing ? ' paused' : ' resumed'), pausing ? 'The stream continues; no ' + mod.noun + ' events are generated while paused.' : '');
    });
  }
  function onSnap(e) {
    act(e.currentTarget, 'Saving…', async function () {
      const snap = await api.detection.snapshot(mod.id, camId);
      state.addSnapshotFromBackend(snap);
      ui.toast('success', 'Snapshot saved', snap.path ? snap.path : snap.id);
    });
  }

  // ======================= D. settings =======================
  function renderSettings() {
    if (mod.id === 'fire') return renderFireSettings();
    const el = root.querySelector('#dl-settings');
    if (!cfgResp) {
      el.innerHTML = '<div class="card-head"><h2 id="dl-set-h">' + icon('sliders', 18) + 'Detection Settings</h2></div><p class="muted" style="font-size:13px">' +
        (api.status.connected ? 'Select a camera to load its ' + mod.title + ' settings.' : 'Settings are stored on the backend per camera.') + '</p>';
      return;
    }
    const c = cfgResp.config, zones = cfgResp.zones || [];
    const labels = mod.classes.map(function (x) { return x[1]; });
    const num = function (name, v, min, max, step, w) { return '<input class="input input-sm" type="number" name="' + name + '" value="' + esc(v) + '" min="' + min + '" max="' + max + '" step="' + step + '" style="width:' + (w || 84) + 'px">'; };
    const ccEditable = mod.id === 'vehicle' ? labels : ['Person'];
    el.innerHTML = '<div class="card-head"><h2 id="dl-set-h">' + icon('sliders', 18) + 'Detection Settings</h2><span class="spacer"></span><span class="badge badge-info" title="Saved on the backend for this camera">' + esc(cfgResp.cameraId) + '</span></div>' +
      '<form id="dl-form" class="stack" novalidate>' +
      '<div class="field"><label for="dl-conf">Base confidence (predictor)</label><div class="range-row"><input class="range" type="range" id="dl-conf" name="confidence" min="0.10" max="0.95" step="0.01" value="' + Number(c.confidence).toFixed(2) + '" data-range-out="dl-conf-out"><output class="range-val" id="dl-conf-out"></output></div>' +
      '<span class="field-hint">Original value 0.35. Each class must also pass its own minimum below.</span></div>' +
      '<div class="field"><span class="field-label">Classes &amp; per-class minimum confidence</span><div class="dl-classes">' + mod.classes.map(function (x) {
        const on = (c.classes || {})[x[0]] !== false && !(x[0] === 'personFar' && !(c.classes || {}).personFar);
        return '<label class="check"><input type="checkbox" name="cls-' + x[0] + '" ' + (on ? 'checked' : '') + '><span>' + esc(x[1]) + (x[2] ? ' <span class="dim">(' + esc(x[2]) + ')</span>' : '') + '</span></label>' +
          (ccEditable.indexOf(x[1]) >= 0 ? num('cc-' + x[1], (c.classConf || {})[x[1]] != null ? c.classConf[x[1]] : '', 0.05, 0.99, 0.01) : '<span class="dim" style="font-size:12px">' + (x[1] === 'Rider' ? 'uses Person' : 'fixed 0.25') + '</span>');
      }).join('') + '</div></div>' +
      '<div class="field"><span class="field-label">Detection zones</span>' + (zones.length ? '<div class="row wrap" style="gap:12px">' + zones.map(function (z) {
        return '<label class="check"><input type="checkbox" name="zone" value="' + esc(z.id) + '" ' + ((c.zones || []).indexOf(z.id) >= 0 ? 'checked' : '') + (z.enabled === false ? ' disabled' : '') + '><span><span class="dot" style="color:' + esc(z.color || '#3478FF') + '"></span> ' + esc(z.name) + '</span></label>';
      }).join('') + '</div><span class="field-hint">None selected = full frame. Events count only objects whose box centre is inside a selected zone.</span>'
        : '<span class="field-hint">No zones drawn for this camera. <a href="#/cameras/' + encodeURIComponent(cfgResp.cameraId) + '/edit">Draw zones in Camera Management</a>.</span>') + '</div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Snapshot saving</b>' + ui.switchHtml('dl-snap-on', c.snapshot.mode !== 'off', 'Save snapshots automatically', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap" style="gap:12px;margin-top:8px">' + labels.map(function (l) { return '<label class="check"><input type="checkbox" name="snapl" value="' + esc(l) + '" ' + ((c.snapshot.labels || []).indexOf(l) >= 0 ? 'checked' : '') + '><span>' + esc(l) + '</span></label>'; }).join('') + '</div>' +
      '<div class="row mt-8" style="gap:8px"><label for="dl-scool" class="dim" style="font-size:12.5px">Cooldown per class (s)</label>' + num('snapCool', c.snapshot.cooldownSec, 0, 86400, 1).replace('name="snapCool"', 'name="snapCool" id="dl-scool"') + '</div>' +
      '<span class="field-hint">Saved when the tracker confirms a NEW track (not every frame). Alerts always get a snapshot while saving is on.</span></div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Alert generation</b>' + ui.switchHtml('dl-alert-on', !!c.alert.enabled, 'Generate alerts', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap" style="gap:12px;margin-top:8px">' + labels.map(function (l) { return '<label class="check"><input type="checkbox" name="alertl" value="' + esc(l) + '" ' + ((c.alert.labels || []).indexOf(l) >= 0 ? 'checked' : '') + '><span>' + esc(l) + '</span></label>'; }).join('') + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label for="dl-sev" class="dim" style="font-size:12.5px">Severity</label><select class="select select-sm" id="dl-sev" name="severity" style="width:auto">' +
      ['low', 'medium', 'high', 'critical'].map(function (s) { return '<option value="' + s + '"' + (c.alert.severity === s ? ' selected' : '') + '>' + ui.cap(s) + (s === 'low' ? ' (informational)' : '') + '</option>'; }).join('') + '</select>' +
      '<label for="dl-acool" class="dim" style="font-size:12.5px">Cooldown per class (s)</label>' + num('alertCool', c.alert.cooldownSec, 0, 86400, 1).replace('name="alertCool"', 'name="alertCool" id="dl-acool"') + '</div></div>' +
      '<div class="field-error" id="dl-form-err"></div>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">' + icon('save', 14) + '<span>Apply settings</span></button><button class="btn btn-sm btn-ghost" type="button" id="dl-reset">Reset to original values</button></div>' +
      '<p class="dim" style="font-size:12px">All settings apply live to the running pipeline — no restart needed.</p></form>';
    window.AV.modules.wireRanges(el);
    window.AV.icons.hydrate(el);
    const form = el.querySelector('#dl-form');
    form.addEventListener('submit', function (e) { e.preventDefault(); saveSettings(readForm(form), form.querySelector('[type=submit]')); });
    el.querySelector('#dl-reset').addEventListener('click', function () {
      ui.confirm({ title: 'Reset ' + mod.title + ' settings?', message: 'Restore the original pipeline values (confidence 0.35, original per-class thresholds, default snapshot and alert policy) for ' + cfgResp.cameraId + '.', confirmText: 'Reset' })
        .then(function (ok) { if (ok) saveSettings(JSON.parse(JSON.stringify(cfgResp.defaults)), null, true); });
    });
    // the two toggles take effect immediately on the backend
    el.querySelector('#dl-snap-on').addEventListener('change', function (e) { saveSettings({ snapshot: { mode: e.target.checked ? 'new_track' : 'off' } }, null); });
    el.querySelector('#dl-alert-on').addEventListener('change', function (e) { saveSettings({ alert: { enabled: e.target.checked } }, null); });
  }

  function readForm(form) {
    const q = function (n) { return form.querySelector('[name="' + n + '"]'); };
    const out = { confidence: Math.round(parseFloat(q('confidence').value) * 100) / 100, classes: {}, classConf: {}, zones: [], snapshot: {}, alert: {} };
    mod.classes.forEach(function (x) {
      out.classes[x[0]] = q('cls-' + x[0]).checked;
      const n = q('cc-' + x[1]);
      if (n && n.value !== '') out.classConf[x[1]] = parseFloat(n.value);
    });
    out.zones = Array.prototype.map.call(form.querySelectorAll('[name="zone"]:checked'), function (c) { return c.value; });
    out.snapshot.labels = Array.prototype.map.call(form.querySelectorAll('[name="snapl"]:checked'), function (c) { return c.value; });
    out.snapshot.cooldownSec = parseFloat(q('snapCool').value);
    out.snapshot.mode = root.querySelector('#dl-snap-on').checked ? 'new_track' : 'off';
    out.alert.enabled = root.querySelector('#dl-alert-on').checked;
    out.alert.labels = Array.prototype.map.call(form.querySelectorAll('[name="alertl"]:checked'), function (c) { return c.value; });
    out.alert.severity = q('severity').value;
    out.alert.cooldownSec = parseFloat(q('alertCool').value);
    return out;
  }

  // ---- Fire & Smoke settings (the standalone detector's config.yaml: detection, alert, smoke_rules)
  function renderFireSettings() {
    const el = root.querySelector('#dl-settings');
    if (!cfgResp) {
      el.innerHTML = '<div class="card-head"><h2 id="dl-set-h">' + icon('sliders', 18) + 'Detection Settings</h2></div><p class="muted" style="font-size:13px">' +
        (api.status.connected ? 'Select a camera to load its Fire & Smoke settings.' : 'Settings are stored on the backend per camera.') + '</p>';
      return;
    }
    const c = cfgResp.config, d = cfgResp.defaults, zones = cfgResp.zones || [];
    const cl = c.classes || {}, cc = c.classConf || {}, v = c.verifier || {}, r = c.alertRule || {}, sr = c.smokeRules || {}, al = c.alert || {};
    const num = function (name, val, min, max, step, w) { return '<input class="input input-sm" type="number" name="' + name + '" value="' + esc(val) + '" min="' + min + '" max="' + max + '" step="' + step + '" style="width:' + (w || 80) + 'px">'; };
    const sev = function (name, val) { return '<select class="select select-sm" name="' + name + '" style="width:auto">' + ['low', 'medium', 'high', 'critical'].map(function (x) { return '<option value="' + x + '"' + (val === x ? ' selected' : '') + '>' + ui.cap(x) + '</option>'; }).join('') + '</select>'; };
    el.innerHTML = '<div class="card-head"><h2 id="dl-set-h">' + icon('sliders', 18) + 'Detection Settings</h2><span class="spacer"></span><span class="badge badge-info" title="Saved on the backend for this camera">' + esc(cfgResp.cameraId) + '</span></div>' +
      '<form id="dl-form" class="stack" novalidate>' +
      '<div class="field"><span class="field-label">Classes &amp; minimum confidence</span><div class="dl-classes">' +
      '<label class="check"><input type="checkbox" name="cls-fire" ' + (cl.fire !== false ? 'checked' : '') + '><span>Fire</span></label>' + num('cc-Fire', cc.Fire != null ? cc.Fire : d.classConf.Fire, 0.05, 0.99, 0.01) +
      '<label class="check"><input type="checkbox" name="cls-smoke" ' + (cl.smoke !== false ? 'checked' : '') + '><span>Smoke</span></label>' + num('cc-Smoke', cc.Smoke != null ? cc.Smoke : d.classConf.Smoke, 0.05, 0.99, 0.01) +
      '</div><span class="field-hint">Original values: fire 0.30, smoke 0.65 (stronger smoke alerts directly).</span></div>' +
      '<div class="form-grid"><div class="field"><label for="fs-imgsz">Model input size</label><select class="select select-sm" id="fs-imgsz" name="imgSize">' +
      [416, 480, 640].map(function (x) { return '<option value="' + x + '"' + (+c.imgSize === x ? ' selected' : '') + '>' + x + ' px' + (x === d.imgSize ? ' (original)' : '') + '</option>'; }).join('') + '</select><span class="field-hint">416 faster · 480 better on small / far fires.</span></div>' +
      '<div class="field"><label for="fs-every">Analyse every Nth frame</label>' + num('everyNFrames', c.everyNFrames, 1, 10, 1).replace('name="everyNFrames"', 'name="everyNFrames" id="fs-every"') + '<span class="field-hint">Original 2. Frames are also skipped while the model is busy.</span></div></div>' +
      '<label class="check"><input type="checkbox" name="degreen" ' + (c.removeGreenOverlay ? 'checked' : '') + '><span>Erase the camera\'s green target-overlay boxes before detection <span class="dim">(the model calls them smoke)</span></span></label>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Smoke verifier</b>' + ui.switchHtml('fs-ver-on', !!v.enabled, 'Smoke verifier', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label class="dim" style="font-size:12.5px" for="fs-pmin">Minimum smoke probability</label>' + num('minSmokeProb', v.minSmokeProb, 0, 1, 0.05).replace('name="minSmokeProb"', 'name="minSmokeProb" id="fs-pmin"') + '</div>' +
      '<span class="field-hint">Smoke boxes that look like cloud or dust only count as weak smoke.</span></div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Smoke rules (weak smoke)</b>' + ui.switchHtml('fs-sr-on', !!sr.enabled, 'Smoke rules', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label class="dim" style="font-size:12.5px" for="fs-low">Weak smoke from</label>' + num('lowConf', sr.lowConf, 0.05, 0.99, 0.01).replace('name="lowConf"', 'name="lowConf" id="fs-low"') +
      '<label class="dim" style="font-size:12.5px" for="fs-pers">rising/growing for (s)</label>' + num('persistS', sr.persistS, 0, 3600, 1).replace('name="persistS"', 'name="persistS" id="fs-pers"') +
      '<label class="dim" style="font-size:12.5px" for="fs-long">or seen for (s, 0 = never)</label>' + num('longPersistS', sr.longPersistS, 0, 86400, 1).replace('name="longPersistS"', 'name="longPersistS" id="fs-long"') + '</div>' +
      '<span class="field-hint">Smoke between this value and the smoke threshold alerts only with a fire box nearby, or when it rises / grows. Sideways drift (clouds, people, vehicles) is ignored.</span></div>' +
      '<div class="dl-sub"><b>Alert rule</b><div class="row wrap mt-8" style="gap:10px"><label class="dim" style="font-size:12.5px" for="fs-win">Look at the last</label>' + num('windowFrames', r.windowFrames, 1, 60, 1).replace('name="windowFrames"', 'name="windowFrames" id="fs-win"') +
      '<span class="dim" style="font-size:12.5px">analysed frames; alert when fire is in</span>' + num('hitsFire', (r.minHits || {}).fire, 1, 60, 1) + '<span class="dim" style="font-size:12.5px">/ smoke in</span>' + num('hitsSmoke', (r.minHits || {}).smoke, 1, 60, 1) + '<span class="dim" style="font-size:12.5px">of them</span></div></div>' +
      '<div class="field"><span class="field-label">Detection zones</span>' + (zones.length ? '<div class="row wrap" style="gap:12px">' + zones.map(function (z) {
        return '<label class="check"><input type="checkbox" name="zone" value="' + esc(z.id) + '" ' + ((c.zones || []).indexOf(z.id) >= 0 ? 'checked' : '') + (z.enabled === false ? ' disabled' : '') + '><span><span class="dot" style="color:' + esc(z.color || '#3478FF') + '"></span> ' + esc(z.name) + '</span></label>';
      }).join('') + '</div><span class="field-hint">None selected = full frame (original behaviour). With zones, only boxes whose centre is inside count.</span>'
        : '<span class="field-hint">Full frame (original behaviour). Zones can be drawn in Camera Management.</span>') + '</div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Alert generation</b>' + ui.switchHtml('dl-alert-on', al.enabled !== false, 'Generate alerts', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label class="dim" style="font-size:12.5px">Fire severity</label>' + sev('severity', al.severity) + '<label class="dim" style="font-size:12.5px">Smoke severity</label>' + sev('smokeSeverity', al.smokeSeverity) +
      '<label class="dim" style="font-size:12.5px" for="fs-cool">Cooldown per class (s)</label>' + num('alertCool', al.cooldownSec, 0, 86400, 1).replace('name="alertCool"', 'name="alertCool" id="fs-cool"') + '</div>' +
      '<span class="field-hint">Every confirmed fire / smoke event is saved with an annotated snapshot (Snapshots page). With alerts off, events and snapshots are still recorded.</span></div>' +
      '<div class="notice notice-danger">' + icon('alert', 16) + '<div>Video fire/smoke analytics are a supplementary aid. They do not replace a certified fire detection and alarm system.</div></div>' +
      '<div class="field-error" id="dl-form-err"></div>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">' + icon('save', 14) + '<span>Apply settings</span></button><button class="btn btn-sm btn-ghost" type="button" id="dl-reset">Reset to original values</button></div>' +
      '<p class="dim" style="font-size:12px">Settings apply live to the running detector — no restart needed.</p></form>';
    window.AV.icons.hydrate(el);
    const form = el.querySelector('#dl-form');
    form.addEventListener('submit', function (e) { e.preventDefault(); const p = readFireForm(form); if (p) saveSettings(p, form.querySelector('[type=submit]'), true); });
    el.querySelector('#dl-reset').addEventListener('click', function () {
      ui.confirm({ title: 'Reset Fire & Smoke settings?', message: 'Restore the original detector values (fire 0.30, smoke 0.65, 480 px, every 2nd frame, smoke verifier and smoke rules on, fire 2 / smoke 3 of the last 6 frames, cooldown 10 s) for ' + cfgResp.cameraId + '.', confirmText: 'Reset' })
        .then(function (ok) { if (ok) saveSettings(JSON.parse(JSON.stringify(cfgResp.defaults)), null, true); });
    });
    el.querySelector('#dl-alert-on').addEventListener('change', function (e) { saveSettings({ alert: { enabled: e.target.checked } }, null, true); });
  }

  function readFireForm(form) {
    const q = function (n) { return form.querySelector('[name="' + n + '"]'); };
    const f = function (n) { return parseFloat(q(n).value); };
    const err = root.querySelector('#dl-form-err');
    if (!q('cls-fire').checked && !q('cls-smoke').checked) { err.textContent = 'Select fire, smoke or both.'; return null; }
    const win = parseInt(q('windowFrames').value, 10), hf = parseInt(q('hitsFire').value, 10), hs = parseInt(q('hitsSmoke').value, 10);
    if (!(win >= 1 && hf >= 1 && hs >= 1)) { err.textContent = 'Alert rule values must be at least 1.'; return null; }
    return {
      classes: { fire: q('cls-fire').checked, smoke: q('cls-smoke').checked },
      classConf: { Fire: f('cc-Fire'), Smoke: f('cc-Smoke') },
      imgSize: parseInt(q('imgSize').value, 10), everyNFrames: parseInt(q('everyNFrames').value, 10),
      removeGreenOverlay: q('degreen').checked,
      verifier: { enabled: root.querySelector('#fs-ver-on').checked, minSmokeProb: f('minSmokeProb') },
      smokeRules: { enabled: root.querySelector('#fs-sr-on').checked, lowConf: f('lowConf'), persistS: f('persistS'), longPersistS: f('longPersistS') },
      alertRule: { windowFrames: win, minHits: { fire: hf, smoke: hs } },
      zones: Array.prototype.map.call(form.querySelectorAll('[name="zone"]:checked'), function (c) { return c.value; }),
      alert: { enabled: root.querySelector('#dl-alert-on').checked, severity: q('severity').value, smokeSeverity: q('smokeSeverity').value, cooldownSec: f('alertCool') }
    };
  }

  async function saveSettings(patch, btn, full) {
    const err = root.querySelector('#dl-form-err');
    if (!full && patch.classes && !Object.keys(patch.classes).some(function (k) { return k !== 'personFar' && patch.classes[k]; })) { if (err) err.textContent = 'Select at least one class.'; return; }
    if (btn) ui.setBusy(btn, true, 'Applying…');
    try {
      const r = await api.detection.saveConfig(mod.id, camId, patch);
      cfgResp.config = r.config;
      if (err) err.textContent = '';
      ui.toast('success', mod.title + ' settings applied', 'Saved on the backend for ' + camId + (status && stateKey() === 'processing' ? ' and applied to the running pipeline.' : '.'));
      renderSettings();
    } catch (e) {
      if (err) err.textContent = e.message;
      ui.toast('error', 'Settings not applied', e.message);
      cfgResp = await api.detection.config(mod.id, camId).catch(function () { return cfgResp; });
      renderSettings();
    } finally { if (btn && btn.isConnected) ui.setBusy(btn, false); }
  }

  // ======================= events =======================
  async function loadEvents() {
    if (!root || !api.status.connected || !camId) return;
    try {
      const list = (await api.detection.events(mod.id, camId, 24)).map(state.fromBackend);
      const sig = list.length + '|' + (list[0] && list[0].id);
      if (sig !== lastEvSig) { lastEvSig = sig; renderEvents(list); state.pollEvents(); }
    } catch (e) { /* status panel shows backend errors */ }
  }

  function renderEvents(list) {
    const el = root.querySelector('#dl-events');
    el.innerHTML = '<div class="card-head"><h2 id="dl-ev-h">Recent ' + mod.noun + ' events</h2><span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/snapshots">Snapshots</a><a class="btn btn-sm btn-ghost" href="#/alerts">All alerts</a></div>' +
      (list.length ? '<div class="det-row dl-ev-row">' + list.map(function (d) {
        const t = ui.eventType(d.type);
        return '<button class="det-card" data-ev="' + esc(d.id) + '"><span class="det-img">' + (d.image ? '<img src="' + esc(d.image) + '" alt="" loading="lazy">' : '<span class="media-ph">' + icon('image', 20) + '<span>No snapshot (policy/cooldown)</span></span>') +
          '<span class="det-label">' + ui.toneBadge(t.tone, d.label || t.label) + '</span></span><span class="det-info"><b class="ellipsis">' + esc(d.label) + (d.trackId != null ? ' · #' + esc(d.trackId) : '') + '</b><span>' + esc(ui.fmtTime(d.timestamp)) + '</span>' +
          '<span>Confidence: ' + (d.confidence != null ? Number(d.confidence).toFixed(2) : '<span class="dim">n/a</span>') + (d.alertId ? ' · ' + icon('bell', 11) + ' alert' : '') + (d.zone ? ' · ' + esc(d.zone) : '') + '</span></span></button>';
      }).join('') + '</div>'
        : ui.emptyState('scan', 'No ' + mod.noun + ' events yet', !camId ? 'Select a camera.' : (mod.id === 'fire' ? 'An event (with snapshot) is recorded when fire or smoke is confirmed by the alert rule — not every frame.' : 'Events appear when the tracker confirms a new ' + mod.noun + ' (not every frame).')));
    el.querySelectorAll('[data-ev]').forEach(function (b) { b.addEventListener('click', function () { const d = list.find(function (x) { return x.id === b.dataset.ev; }); if (d) window.AV.events.openDetection(d); }); });
  }

  function destroy() {
    clearInterval(timer); clearInterval(evTimer); clearTimeout(retryTimer);
    detachStream();
    unsub.forEach(function (f) { f(); }); unsub = [];
    root = null;
  }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.detectionLive = { title: 'Detection', render: render, destroy: destroy, onBackend: function () { onBackend(); } };
})();
