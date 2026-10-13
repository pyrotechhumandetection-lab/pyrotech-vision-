/* PYROTECH VISION — Object Monitoring page (live, backend-driven).
 *
 * The marked-object monitor (backend/detection/object_monitor) on the camera's shared session:
 * draw a box around any object on the live picture; the backend watches it and raises an alert when
 * it is MOVED or goes MISSING (people walking in front of it -> OCCLUDED, no alert). The desktop
 * window's mouse box and keys of the original program become the controls on this page.
 * Nothing is simulated: every state, score and event comes from the backend.
 */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api;
  const esc = ui.esc;
  const MOD = 'object', TITLE = 'Object Monitoring';
  const STATE_UI = {
    backend_unavailable: ['badge-muted', 'Backend unavailable'], no_camera: ['badge-muted', 'No camera selected'],
    stopped: ['badge-muted', 'Stopped'], connecting: ['badge-info', 'Connecting'], processing: ['badge-success', 'Monitoring'],
    paused: ['badge-warning', 'Paused'], disconnected: ['badge-danger', 'Disconnected'], error: ['badge-danger', 'Backend error']
  };
  const OBJ_STATE = {
    STABLE: ['badge-success', 'In place'], CHECKING: ['badge-warning', 'Checking'], OCCLUDED: ['badge-info', 'Covered'],
    MOVED: ['badge-danger', 'MOVED'], MISSING: ['badge-danger', 'MISSING'], PAUSED: ['badge-muted', 'Paused (picture changed)']
  };

  let root, camId, status, cfgResp, timer, evTimer, busy = false, streamOn = false, unsub = [], drawMode = false, draft = null, unfit = null, lastEvSig = '';

  function render(container) {
    root = container;
    camId = state.prefs().objCamera || '';
    status = null; cfgResp = null; streamOn = false; drawMode = false; draft = null; lastEvSig = '';
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<a href="#/detections/all">Detections</a>' + icon('chevronRight', 14) + '<span class="current">' + TITLE + '</span></nav>' +
      '<div class="page-head"><span class="head-icon" style="color:#2BB8FF">' + icon('box', 24) + '</span><div><h1>' + TITLE + '</h1>' +
      '<p class="sub">Marked-object monitor · draw a box around an object · alert when it is moved or missing · YOLOE names objects and spots people</p></div>' +
      '<div class="actions"><a class="btn" id="om-open-cam" href="#/cameras">' + icon('monitor', 16) + '<span>Open camera</span></a><a class="btn btn-ghost" href="#/alerts">' + icon('bell', 16) + '<span>Alerts</span></a></div></div>' +
      '<div id="om-notice"></div>' +
      '<div class="dl-grid">' +
      '<section class="card dl-live" aria-labelledby="om-live-h"></section>' +
      '<div class="dl-side"><section class="card" id="om-status" aria-labelledby="om-status-h"></section><section class="card" id="om-settings" aria-labelledby="om-set-h"></section></div>' +
      '<div class="dl-events om-lower"><section class="card" id="om-objects" aria-labelledby="om-obj-h"></section>' +
      '<section class="card" id="om-events" aria-labelledby="om-ev-h"></section></div></div>';
    renderLive(); renderStatus(); renderSettings(); renderObjects(); renderEvents([]);
    unsub.push(state.on('backend', onBackend), state.on('cameras', function () { renderCameraSelect(); }));
    onBackend();
  }

  function backendCams() { return state.cameras().filter(function (c) { return c.source === 'backend'; }); }
  function selectedCam() { return camId ? state.getCamera(camId) : null; }
  function view() { return (status && status.object) || null; }

  function onBackend() {
    if (!root) return;
    const n = root.querySelector('#om-notice');
    if (!api.status.connected) n.innerHTML = '<div class="notice notice-warning mb-12">' + icon('server', 16) + '<div><b>Backend not connected.</b> Object Monitoring runs on the detection backend in this repository. Start it with <code class="mono">start_backend.cmd</code>. <a href="#/settings">Backend settings</a></div></div>';
    else if (!backendCams().length) n.innerHTML = '<div class="notice notice-info mb-12">' + icon('info', 16) + '<div>No cameras are registered on the backend yet. <a href="#/cameras/new">Add a camera</a> in Camera Management.</div></div>';
    else n.innerHTML = '';
    if (camId && !backendCams().some(function (c) { return c.id === camId; })) camId = '';
    renderCameraSelect();
    refresh(true);
    clearInterval(timer); clearInterval(evTimer);
    if (api.status.connected) {
      timer = setInterval(function () { if (!document.hidden) refresh(false); }, 1500);
      evTimer = setInterval(function () { if (!document.hidden) loadEvents(); }, 5000);
    }
  }

  async function refresh(withConfig) {
    if (!root) return;
    if (!api.status.connected) { status = { state: 'backend_unavailable' }; paint(); return; }
    if (!camId) { status = { state: 'no_camera' }; cfgResp = null; paint(); renderSettings(); renderEvents([]); return; }
    try {
      status = await api.detection.status(MOD, camId);
      if (withConfig) { cfgResp = await api.detection.config(MOD, camId); renderSettings(); loadEvents(); }
    } catch (e) { status = { state: 'error', error: { code: e.code || e.kind, message: e.message } }; }
    paint();
  }

  function stateKey() { return status ? (status.state || 'stopped') : 'connecting'; }
  function running() { return !!(status && status.startedAt && ['processing', 'paused', 'disconnected', 'connecting'].indexOf(status.state) >= 0); }
  function paint() { if (!root || busy) return; renderStatus(); paintLive(); renderObjects(); }

  // ======================= status =======================
  function renderStatus() {
    const el = root.querySelector('#om-status');
    const s = status || {}, k = stateKey(), su = STATE_UI[k] || STATE_UI.stopped, m = s.metrics || {}, v = view(), cam = selectedCam();
    const l2 = v && v.layer2;
    const counts = {};
    ((v && v.objects) || []).forEach(function (o) { counts[o.state] = (counts[o.state] || 0) + 1; });
    el.innerHTML = '<div class="card-head"><h2 id="om-status-h">Monitoring Status</h2><span class="spacer"></span><span class="badge ' + su[0] + '">' + (k === 'processing' ? '<span class="dot pulse"></span>' : '') + su[1] + '</span></div>' +
      '<dl class="kv dl-kv">' +
      '<dt>Backend</dt><dd>' + (api.status.connected ? '<span class="badge badge-success">Connected</span>' : '<span class="badge badge-muted">Not connected</span>') + '</dd>' +
      '<dt>Camera</dt><dd>' + (cam ? esc(cam.name) + ' <span class="mono dim">' + esc(cam.id) + '</span>' : '<span class="dim">None selected</span>') + '</dd>' +
      '<dt>Camera IP</dt><dd>' + (cam ? ui.ipHtml(cam) : '<span class="dim">No camera selected</span>') + '</dd>' +
      '<dt>Marked objects</dt><dd>' + (v ? (v.objects.length ? Object.keys(counts).map(function (st) { const u = OBJ_STATE[st] || ['badge-muted', st]; return '<span class="badge ' + u[0] + '">' + esc(u[1]) + ' <b>' + counts[st] + '</b></span>'; }).join(' ') : '<span class="dim">None — draw a box on the picture</span>') + (v.pendingRestore ? ' <span class="dim">(' + v.pendingRestore + ' saved object(s) being restored)</span>' : '') : '<span class="dim">—</span>') + '</dd>' +
      '<dt>Picture check</dt><dd>' + (v ? (v.scenePaused ? '<span class="badge badge-warning">Paused — whole picture changed</span> <span class="dim">lights / IR / camera moved? Use Accept positions.</span>' : '<span class="badge badge-success">Normal</span> <span class="dim">' + esc(v.checks) + ' checks</span>') : '<span class="dim">—</span>') + '</dd>' +
      '<dt>Layer 2 detector</dt><dd>' + (l2 ? (l2.model ? esc(l2.model) + (l2.latencyS != null ? ' <span class="dim">· ' + esc(l2.latencyS) + ' s per pass · ' + esc(l2.people) + ' people in view</span>' : ' <span class="dim">· loading…</span>') : '<span class="badge badge-warning">Not available</span> <span class="dim">' + esc(l2.error || 'disabled') + '</span>') : esc(s.modelFile || 'yoloe-26n-seg.pt') + (s.modelAvailable === false ? ' <span class="badge badge-danger">Model file missing</span>' : '')) + '</dd>' +
      '<dt>Layer 3 (Grounding DINO)</dt><dd>' + (v ? (v.layer3 === 'on' ? '<span class="badge badge-success">On</span> <span class="dim">names on mark / alert</span>' : '<span class="badge badge-muted">' + esc(v.layer3 === 'off' ? 'Off' : 'Not installed') + '</span>' + (v.layer3 !== 'off' ? ' <span class="dim">' + esc(String(v.layer3).replace(/^unavailable: /, '')) + '</span>' : '')) : '<span class="dim">—</span>') + '</dd>' +
      '<dt>Camera FPS (received)</dt><dd>' + (m.cameraFps != null ? esc(m.cameraFps) + ' fps' : '<span class="dim">Unavailable</span>') + (m.resolution ? ' <span class="dim">· ' + esc(m.resolution) + '</span>' : '') + '</dd>' +
      '<dt>Frames analysed</dt><dd>' + (m.processingFps != null ? esc(m.processingFps) + ' fps' : '<span class="dim">—</span>') + (m.inferenceMs != null ? ' <span class="dim">· ' + esc(m.inferenceMs) + ' ms per frame (all modules)</span>' : '') + '</dd>' +
      (s.sharedWith && s.sharedWith.length ? '<dt>Camera shared with</dt><dd>' + esc(s.sharedWith.map(function (x) { return (window.AV.catalog.moduleById(x) || {}).long || x; }).join(', ')) + '</dd>' : '') +
      '<dt>Last alert</dt><dd>' + (s.lastDetectionAt ? esc(ui.fmtTime(s.lastDetectionAt)) + ' <span class="dim">(' + esc(ui.relTime(s.lastDetectionAt)) + ')</span>' : '<span class="dim">None yet</span>') + '</dd>' +
      '</dl>' + (s.error && s.error.message ? '<div class="notice notice-danger mt-12">' + icon('alertCircle', 16) + '<div><b>' + esc(s.error.code || 'Error') + '</b> ' + esc(s.error.message) + '</div></div>' : '');
  }

  // ======================= live + marking =======================
  function renderLive() {
    const el = root.querySelector('.dl-live');
    el.innerHTML = '<div class="card-head"><h2 id="om-live-h">' + icon('camera', 18) + 'Live ' + TITLE + '</h2><span class="spacer"></span>' +
      '<label class="sr-only" for="om-cam">Camera</label><select class="select select-sm" id="om-cam" style="width:auto;max-width:340px"></select></div>' +
      '<div class="dl-controls" role="toolbar" aria-label="Monitoring controls">' +
      '<button class="btn btn-primary btn-sm" id="om-start">' + icon('play', 15) + '<span>Start Monitoring</span></button>' +
      '<button class="btn btn-sm" id="om-stop">' + icon('square', 15) + '<span>Stop</span></button>' +
      '<button class="btn btn-sm" id="om-pause">' + icon('power', 15) + '<span>Pause</span></button>' +
      '<button class="btn btn-sm" id="om-mark" aria-pressed="false">' + icon('square', 15) + '<span>Mark object</span></button>' +
      '<button class="btn btn-sm" id="om-accept" title="Current positions become the new home of every object (original key R)">' + icon('check', 15) + '<span>Accept positions</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="om-snap">' + icon('snapshot', 15) + '<span>Snapshot</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="om-full">' + icon('maximize', 15) + '<span>Full Screen</span></button></div>' +
      '<div class="dl-stage" id="om-stage"><div class="media-ph" id="om-ph"></div><img class="dl-img" id="om-img" alt="Annotated object monitoring stream" hidden>' +
      '<div class="om-draw" id="om-draw" hidden><div class="om-box" id="om-box" hidden></div></div>' +
      '<span class="badge badge-live dl-flag" id="om-flag" hidden><span class="dot pulse"></span>LIVE · monitoring</span></div>' +
      '<div class="om-markbar" id="om-markbar" hidden><span class="dim">New object</span><input class="input input-sm" id="om-name" maxlength="60" placeholder="Name (optional — the detector suggests one)" aria-label="Object name">' +
      '<button class="btn btn-primary btn-sm" id="om-add">' + icon('check', 14) + '<span>Monitor this object</span></button><button class="btn btn-sm btn-ghost" id="om-cancel">Cancel</button></div>' +
      '<p class="dim mt-8" style="font-size:12px" id="om-hint">' + icon('info', 13) + ' Green = in place, yellow = checking, orange = covered (someone in front of it), red = MOVED / MISSING (dashed = where it was). The picture of the object you draw is its reference — mark objects while they are in their normal place. Monitoring keeps running when you leave this page.</p>';
    el.querySelector('#om-cam').addEventListener('change', onCameraChange);
    el.querySelector('#om-start').addEventListener('click', onStart);
    el.querySelector('#om-stop').addEventListener('click', onStop);
    el.querySelector('#om-pause').addEventListener('click', onPause);
    el.querySelector('#om-snap').addEventListener('click', onSnap);
    el.querySelector('#om-full').addEventListener('click', function () { ui.fullscreen(root.querySelector('#om-stage')); });
    el.querySelector('#om-mark').addEventListener('click', function () { setDrawMode(!drawMode); });
    el.querySelector('#om-accept').addEventListener('click', onAccept);
    el.querySelector('#om-add').addEventListener('click', onAdd);
    el.querySelector('#om-cancel').addEventListener('click', function () { clearDraft(); });
    el.querySelector('#om-name').addEventListener('keydown', function (e) { if (e.key === 'Enter') onAdd(); if (e.key === 'Escape') clearDraft(); });
    el.querySelector('#om-img').addEventListener('error', function () {
      if (!streamOn) return;
      detachStream();
      const ph = root && root.querySelector('#om-ph');
      if (ph) ph.innerHTML = icon('wifiOff', 26) + '<b>Stream interrupted</b><span>The MJPEG stream closed. It reconnects with the next status update.</span>';
    });
    bindDrawing(el.querySelector('#om-draw'));
    window.AV.icons.hydrate(el);
    renderCameraSelect();
  }

  function renderCameraSelect() {
    const sel = root && root.querySelector('#om-cam');
    if (!sel) return;
    const cams = backendCams();
    sel.innerHTML = '<option value="">' + (cams.length ? 'Select a camera…' : (api.status.connected ? 'No backend cameras' : 'Backend unavailable')) + '</option>' + cams.map(function (c) {
      const reason = c.enabled === false ? 'disabled' : '';
      const st = ui.STATUS[ui.camStatus(c)] ? ui.STATUS[ui.camStatus(c)].label : c.status;
      return '<option value="' + esc(c.id) + '"' + (c.id === camId ? ' selected' : '') + (reason ? ' disabled' : '') + '>' + esc(c.name + ' · ' + c.id + ' · ' + (reason || st)) + '</option>';
    }).join('');
    sel.disabled = !api.status.connected;
    root.querySelector('#om-open-cam').href = camId ? '#/cameras/' + encodeURIComponent(camId) : '#/cameras';
  }

  function paintLive() {
    const k = stateKey(), ph = root.querySelector('#om-ph');
    const can = api.status.connected && !!camId, run = running();
    const set = function (id, dis, why) { const b = root.querySelector(id); b.disabled = dis; b.title = dis ? why : (b.dataset.title || b.title); };
    set('#om-start', !can || run, !api.status.connected ? 'Backend not connected' : (!camId ? 'Select a camera first' : 'Monitoring is already running'));
    set('#om-stop', !can || !run, 'Monitoring is not running');
    set('#om-pause', !can || !(k === 'processing' || k === 'paused'), 'Start monitoring first');
    set('#om-mark', !can || k !== 'processing', 'Start monitoring first — objects are marked on the live picture');
    set('#om-accept', !can || k !== 'processing' || !(view() && view().objects.length), 'Nothing to accept');
    set('#om-snap', !can || !run, 'Start monitoring first');
    const pb = root.querySelector('#om-pause');
    pb.innerHTML = icon(k === 'paused' ? 'play' : 'power', 15) + '<span>' + (k === 'paused' ? 'Resume' : 'Pause') + '</span>';
    if (k !== 'processing' && drawMode) setDrawMode(false);
    if (run && !streamOn) attachStream();
    if (!run && streamOn) detachStream();
    root.querySelector('#om-flag').hidden = !(streamOn && k === 'processing');
    if (!streamOn) {
      const msg = {
        backend_unavailable: ['server', 'Backend unavailable', 'Start the detection backend to monitor objects.'],
        no_camera: ['camera', 'No camera selected', 'Choose a camera configured in Camera Management.'],
        stopped: ['square', 'Monitoring stopped', 'Press Start Monitoring, then mark objects on the live picture.'],
        error: ['alertCircle', 'Backend error', (status && status.error && status.error.message) || 'See the status panel.'],
        connecting: ['camera', 'Connecting…', 'Opening the camera stream.']
      }[k] || ['camera', STATE_UI[k] ? STATE_UI[k][1] : k, ''];
      ph.innerHTML = icon(msg[0], 28) + '<b>' + esc(msg[1]) + '</b><span>' + esc(msg[2]) + '</span>';
      ph.classList.toggle('err', k === 'error');
    }
  }

  function attachStream() {
    const img = root.querySelector('#om-img');
    img.src = api.resolveUrl('cameras/' + encodeURIComponent(camId) + '/stream.mjpeg') + '?v=' + Date.now();
    img.hidden = false;
    root.querySelector('#om-ph').hidden = true;
    if (unfit) unfit();
    unfit = window.AV.media.fitOverlay(img, root.querySelector('#om-draw'));
    streamOn = true;
  }
  function detachStream() {
    if (!root) return;
    const img = root.querySelector('#om-img');
    img.removeAttribute('src');
    img.hidden = true;
    root.querySelector('#om-ph').hidden = false;
    if (unfit) { unfit(); unfit = null; }
    streamOn = false;
    setDrawMode(false);
  }

  // ---- drawing a box on the live picture (normalised to the image content)
  function setDrawMode(on) {
    if (!root) return;
    drawMode = on;
    const d = root.querySelector('#om-draw'), b = root.querySelector('#om-mark');
    d.hidden = !on;
    b.setAttribute('aria-pressed', String(on));
    b.classList.toggle('btn-primary', on);
    root.querySelector('#om-hint').innerHTML = icon('info', 13) + (on ? ' <b>Drag a box around the object</b> on the live picture, then name it (optional) and press Monitor this object. Esc cancels.'
      : ' Green = in place, yellow = checking, orange = covered (someone in front of it), red = MOVED / MISSING (dashed = where it was). The picture of the object you draw is its reference — mark objects while they are in their normal place. Monitoring keeps running when you leave this page.');
    window.AV.icons.hydrate(root.querySelector('#om-hint'));
    if (!on) clearDraft();
  }
  function clearDraft() {
    draft = null;
    if (!root) return;
    root.querySelector('#om-box').hidden = true;
    root.querySelector('#om-markbar').hidden = true;
    root.querySelector('#om-name').value = '';
  }
  function bindDrawing(layer) {
    let start = null;
    const pt = function (e) {
      const r = layer.getBoundingClientRect();
      return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
    };
    const show = function (a, b) {
      const box = root.querySelector('#om-box');
      const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y), w = Math.abs(a.x - b.x), h = Math.abs(a.y - b.y);
      box.style.left = (x * 100) + '%'; box.style.top = (y * 100) + '%'; box.style.width = (w * 100) + '%'; box.style.height = (h * 100) + '%';
      box.hidden = false;
      return { x: x, y: y, width: w, height: h };
    };
    layer.addEventListener('pointerdown', function (e) { if (!drawMode) return; e.preventDefault(); layer.setPointerCapture(e.pointerId); start = pt(e); show(start, start); root.querySelector('#om-markbar').hidden = true; });
    layer.addEventListener('pointermove', function (e) { if (start) show(start, pt(e)); });
    layer.addEventListener('pointerup', function (e) {
      if (!start) return;
      const b = show(start, pt(e)); start = null;
      const img = root.querySelector('#om-img');
      const fw = img.naturalWidth || 1920, fh = img.naturalHeight || 1080;
      if (b.width * fw < 8 || b.height * fh < 8) { clearDraft(); ui.toast('info', 'Box too small', 'Drag a box around the whole object.'); return; }
      draft = b;
      root.querySelector('#om-markbar').hidden = false;
      root.querySelector('#om-name').focus();
    });
    document.addEventListener('keydown', function esc_(e) { if (!root) { document.removeEventListener('keydown', esc_); return; } if (e.key === 'Escape' && drawMode) setDrawMode(false); });
  }

  async function onAdd() {
    if (!draft) return;
    const btn = root.querySelector('#om-add'), name = root.querySelector('#om-name').value.trim();
    ui.setBusy(btn, true, 'Marking…');
    try {
      await api.detection.markObject(camId, { x: round4(draft.x), y: round4(draft.y), width: round4(draft.width), height: round4(draft.height) }, name);
      ui.toast('success', 'Object marked', (name ? '"' + name + '" is' : 'The object is') + ' now monitored. ' + (name ? '' : 'The detector suggests a name in a few seconds.'));
      setDrawMode(false);
      setTimeout(function () { refresh(false); }, 700);
    } catch (e) { ui.toast('error', 'Object not marked', e.message); }
    finally { ui.setBusy(btn, false); }
  }
  function round4(v) { return Math.round(v * 10000) / 10000; }

  // ======================= controls =======================
  async function act(btn, label, fn) {
    if (busy) return;
    busy = true;
    ui.setBusy(btn, true, label);
    try { await fn(); }
    catch (e) { ui.toast('error', TITLE + ': ' + (e.code || 'error'), e.message); }
    finally { busy = false; if (root) { ui.setBusy(btn, false); await refresh(false); loadEvents(); } }
  }
  async function onCameraChange(e) {
    const next = e.target.value, prev = camId;
    if (prev && status && ['processing', 'paused', 'disconnected'].indexOf(stateKey()) >= 0) {
      const stop = await ui.confirm({ title: 'Stop monitoring on ' + prev + '?', message: TITLE + ' is running on ' + prev + '. Stop it before switching, or keep it running in the background (alerts continue).', confirmText: 'Stop and switch', cancelText: 'Keep running' });
      if (stop) { try { await api.detection.stop(MOD, prev); ui.toast('success', TITLE + ' stopped on ' + prev); } catch (err) { ui.toast('error', 'Stop failed', err.message); } }
    }
    detachStream();
    camId = next;
    state.setPref('objCamera', camId);
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
      const r = await api.detection.start(MOD, camId);
      ui.toast('success', r.message || (TITLE + ' started'), 'Press Mark object and draw a box around each object to watch.');
      await state.refreshFromBackend(['cameras', 'modules']);
    });
  }
  function onStop(e) {
    act(e.currentTarget, 'Stopping…', async function () {
      detachStream();
      const r = await api.detection.stop(MOD, camId);
      ui.toast('success', r.message || (TITLE + ' stopped'), 'Marked objects are remembered and re-referenced from the first frame when monitoring starts again.');
      await state.refreshFromBackend(['cameras', 'modules']);
    });
  }
  function onPause(e) {
    const pausing = stateKey() !== 'paused';
    act(e.currentTarget, pausing ? 'Pausing…' : 'Resuming…', async function () {
      await api.detection.pause(MOD, camId, pausing);
      ui.toast('info', TITLE + (pausing ? ' paused' : ' resumed'), pausing ? 'No checks and no alerts while paused.' : '');
    });
  }
  function onSnap(e) {
    act(e.currentTarget, 'Saving…', async function () {
      const snap = await api.detection.snapshot(MOD, camId);
      state.addSnapshotFromBackend(snap);
      ui.toast('success', 'Snapshot saved', snap.path ? snap.path : snap.id);
    });
  }
  async function onAccept(e) {
    const ok = await ui.confirm({ title: 'Accept current positions?', message: 'Every marked object\'s current position becomes its new home and its reference picture is taken again (missing objects keep their old reference). Use this after you moved objects on purpose or after the camera view changed.', confirmText: 'Accept positions' });
    if (!ok) return;
    act(e.currentTarget, 'Applying…', async function () { await api.detection.objectAction(camId, 'accept'); ui.toast('success', 'Positions accepted'); });
  }

  // ======================= marked objects =======================
  function renderObjects() {
    const el = root && root.querySelector('#om-objects');
    if (!el) return;
    const v = view(), objs = (v && v.objects) || [];
    const sig = JSON.stringify([objs, v && v.messages && v.messages[0], running()]);
    if (el.dataset.sig === sig) return;
    if (el.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;   // don't interrupt a rename
    el.dataset.sig = sig;
    const msgs = (v && v.messages) || [];
    el.innerHTML = '<div class="card-head"><h2 id="om-obj-h">Marked objects <span class="dim" style="font-weight:500;font-size:13px">· ' + objs.length + '</span></h2><span class="spacer"></span>' +
      '<button class="btn btn-sm btn-ghost danger-hover" id="om-clear"' + (objs.length && running() ? '' : ' disabled') + '>' + icon('trash', 14) + '<span>Remove all</span></button></div>' +
      (objs.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>#</th><th>Name</th><th>State</th><th>Match</th><th>Moved</th><th>Named by</th><th></th></tr></thead><tbody>' + objs.map(function (o) {
        const u = OBJ_STATE[o.state] || ['badge-muted', o.state];
        return '<tr data-id="' + o.id + '"><td class="mono">#' + o.id + '</td><td><input class="input input-sm om-rename" value="' + esc(o.name) + '" maxlength="60" aria-label="Name of object ' + o.id + '" style="min-width:140px"></td>' +
          '<td><span class="badge ' + u[0] + '">' + esc(u[1]) + '</span></td><td>' + Number(o.score).toFixed(2) + '</td><td>' + (o.state === 'MOVED' ? esc(Math.round(o.distance)) + ' px' : '<span class="dim">—</span>') + '</td>' +
          '<td class="dim">' + esc({ user: 'you', detector: 'Layer 2 detector', dino: 'Grounding DINO', '': '—' }[o.namedBy] || o.namedBy) + '</td>' +
          '<td><button class="btn btn-xs btn-ghost danger-hover" data-del aria-label="Stop monitoring object ' + o.id + '">' + icon('trash', 13) + '</button></td></tr>';
      }).join('') + '</tbody></table></div>'
        : ui.emptyState('box', 'No objects marked', running() ? 'Press Mark object and drag a box around an object on the live picture.' : 'Start monitoring on a camera, then mark the objects to watch.')) +
      '<h3 class="hc-sub-h">Activity</h3>' + (msgs.length ? '<ul class="om-log">' + msgs.map(function (m) { return '<li><span class="mono dim">' + esc(ui.fmtTime(m.t, { noYear: true })) + '</span> ' + esc(m.text) + '</li>'; }).join('') + '</ul>' : '<p class="muted" style="font-size:13px">Marking, naming, restored and removed messages appear here.</p>');
    window.AV.icons.hydrate(el);
    const clr = el.querySelector('#om-clear');
    if (clr) clr.addEventListener('click', async function () {
      const ok = await ui.confirm({ title: 'Remove all marked objects?', message: 'Stop monitoring all ' + objs.length + ' object(s) on ' + camId + '?', confirmText: 'Remove all', danger: true });
      if (ok) act(clr, 'Removing…', async function () { await api.detection.objectAction(camId, 'clear'); ui.toast('success', 'All objects removed'); });
    });
    el.querySelectorAll('tr[data-id]').forEach(function (tr) {
      const id = tr.dataset.id;
      tr.querySelector('[data-del]').addEventListener('click', async function () {
        try { await api.detection.removeObject(camId, id); ui.toast('success', 'Object #' + id + ' removed'); setTimeout(function () { refresh(false); }, 700); }
        catch (e) { ui.toast('error', 'Not removed', e.message); }
      });
      tr.querySelector('.om-rename').addEventListener('change', async function (e) {
        const name = e.target.value.trim();
        if (!name) { ui.toast('warning', 'Name required'); return; }
        try { await api.detection.renameObject(camId, id, name); ui.toast('success', 'Object #' + id + ' renamed', name); e.target.blur(); refresh(false); }
        catch (err) { ui.toast('error', 'Not renamed', err.message); }
      });
    });
  }

  // ======================= settings =======================
  function renderSettings() {
    const el = root.querySelector('#om-settings');
    if (!cfgResp) {
      el.innerHTML = '<div class="card-head"><h2 id="om-set-h">' + icon('sliders', 18) + 'Monitoring Settings</h2></div><p class="muted" style="font-size:13px">' + (api.status.connected ? 'Select a camera to load its Object Monitoring settings.' : 'Settings are stored on the backend per camera.') + '</p>';
      return;
    }
    const c = cfgResp.config, m = c.monitor, d = c.detector, al = c.alert;
    const num = function (name, val, min, max, step, w) { return '<input class="input input-sm" type="number" name="' + name + '" value="' + esc(val) + '" min="' + min + '" max="' + max + '" step="' + step + '" style="width:' + (w || 80) + 'px">'; };
    const row = function (label, input, hint) { return '<div class="field"><label>' + label + '</label>' + input + (hint ? '<span class="field-hint">' + hint + '</span>' : '') + '</div>'; };
    el.innerHTML = '<div class="card-head"><h2 id="om-set-h">' + icon('sliders', 18) + 'Monitoring Settings</h2><span class="spacer"></span><span class="badge badge-info" title="Saved on the backend for this camera">' + esc(cfgResp.cameraId) + '</span></div>' +
      '<form id="om-form" class="stack" novalidate>' +
      '<div class="form-grid">' +
      row('Moved: shift more than (px)', num('move_threshold_px', m.move_threshold_px, 1, 5000, 1), 'and') +
      row('Moved: shift more than (% of size)', num('move_threshold_pct', m.move_threshold_pct, 1, 1000, 1), 'Both must be exceeded. Original 40 px and 30 %.') +
      row('Moved alert after (s)', num('min_moved_s', m.min_moved_s, 0, 3600, 0.5), 'Original 2 s.') +
      row('Missing alert after (s)', num('min_missing_s', m.min_missing_s, 0, 3600, 0.5), 'Original 5 s.') +
      row('Bad checks in a row', num('confirm_checks', m.confirm_checks, 1, 50, 1), 'Before an alert. Original 4.') +
      row('Good checks to clear', num('restore_checks', m.restore_checks, 1, 50, 1), 'Back in place. Original 3.') +
      row('Covered counts as missing after (s)', num('max_occlusion_s', m.max_occlusion_s, 0, 86400, 1), 'Original 120 s.') +
      row('Alert cooldown per object (s)', num('cooldown_s', m.cooldown_s, 0, 86400, 1), 'Original 30 s.') +
      '</div>' +
      '<details class="hh-settings"><summary>' + icon('sliders', 15) + '<span>Advanced matching</span></summary><div class="form-grid">' +
      row('In place: match score ≥', num('present_score', m.present_score, 0.05, 0.99, 0.01), 'Original 0.60.') +
      row('Missing evidence: score <', num('lost_score', m.lost_score, 0.05, 0.99, 0.01), 'Original 0.40.') +
      row('Found elsewhere: score ≥', num('found_score', m.found_score, 0.05, 0.99, 0.01), 'Original 0.60.') +
      row('Check every N frames', num('check_every_n_frames', m.check_every_n_frames, 1, 50, 1), 'Original 5.') +
      row('Features', '<select class="select select-sm" name="features">' + ['orb', 'sift'].map(function (x) { return '<option' + (m.features === x ? ' selected' : '') + '>' + x + '</option>'; }).join('') + '</select>', 'orb fast · sift for plain objects (applies to new marks).') +
      row('Tracker (while displaced)', '<select class="select select-sm" name="tracker">' + ['csrt', 'kcf', 'mil', 'none'].map(function (x) { return '<option' + (m.tracker === x ? ' selected' : '') + '>' + x + '</option>'; }).join('') + '</select>', 'Original csrt (applies to new marks).') +
      '</div></details>' +
      '<div class="dl-sub"><b>Layer 2 detector (names, people)</b><div class="row wrap mt-8" style="gap:10px">' +
      '<label class="dim" style="font-size:12.5px">Run every (s)</label>' + num('intervalS', d.intervalS, 0.5, 600, 0.5) +
      '<label class="dim" style="font-size:12.5px">Naming confidence</label>' + num('nameConf', d.nameConf, 0.01, 0.99, 0.01) +
      '<label class="check"><input type="checkbox" name="showLabels" ' + (d.showLabels ? 'checked' : '') + '><span>Show labels on the video</span></label></div>' +
      '<span class="field-hint">' + esc(d.model) + ' at ' + esc(d.imgsz) + ' px with the original 23-name office vocabulary. It never raises alarms.</span></div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Alert generation</b>' + ui.switchHtml('om-alert-on', al.enabled !== false, 'Generate alerts', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label class="dim" style="font-size:12.5px">Severity</label><select class="select select-sm" name="severity" style="width:auto">' +
      ['low', 'medium', 'high', 'critical'].map(function (x) { return '<option value="' + x + '"' + (al.severity === x ? ' selected' : '') + '>' + ui.cap(x) + '</option>'; }).join('') + '</select></div>' +
      '<span class="field-hint">Every MOVED / MISSING is saved with an annotated snapshot (where it was, where it is now). With alerts off, events and snapshots are still recorded.</span></div>' +
      '<div class="field-error" id="om-form-err"></div>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">' + icon('save', 14) + '<span>Apply settings</span></button><button class="btn btn-sm btn-ghost" type="button" id="om-reset">Reset to original values</button></div>' +
      '<p class="dim" style="font-size:12px">Thresholds apply live to the running monitor.</p></form>';
    window.AV.icons.hydrate(el);
    const form = el.querySelector('#om-form');
    form.addEventListener('submit', function (e) { e.preventDefault(); saveSettings(readForm(form), form.querySelector('[type=submit]')); });
    el.querySelector('#om-reset').addEventListener('click', function () {
      ui.confirm({ title: 'Reset Object Monitoring settings?', message: 'Restore the original monitor values for ' + cfgResp.cameraId + '.', confirmText: 'Reset' })
        .then(function (ok) { if (ok) saveSettings(JSON.parse(JSON.stringify(cfgResp.defaults)), null); });
    });
    el.querySelector('#om-alert-on').addEventListener('change', function (e) { saveSettings({ alert: { enabled: e.target.checked } }, null); });
  }

  function readForm(form) {
    const q = function (n) { return form.querySelector('[name="' + n + '"]'); };
    const f = function (n) { return parseFloat(q(n).value); };
    const mon = {};
    ['move_threshold_px', 'move_threshold_pct', 'min_moved_s', 'min_missing_s', 'max_occlusion_s', 'cooldown_s', 'present_score', 'lost_score', 'found_score'].forEach(function (k) { mon[k] = f(k); });
    ['confirm_checks', 'restore_checks', 'check_every_n_frames'].forEach(function (k) { mon[k] = parseInt(q(k).value, 10); });
    mon.features = q('features').value; mon.tracker = q('tracker').value;
    return { monitor: mon, detector: { intervalS: f('intervalS'), nameConf: f('nameConf'), showLabels: q('showLabels').checked },
      alert: { enabled: root.querySelector('#om-alert-on').checked, severity: q('severity').value } };
  }

  async function saveSettings(patch, btn) {
    const err = root.querySelector('#om-form-err');
    if (btn) ui.setBusy(btn, true, 'Applying…');
    try {
      const r = await api.detection.saveConfig(MOD, camId, patch);
      cfgResp.config = r.config;
      if (err) err.textContent = '';
      ui.toast('success', TITLE + ' settings applied', 'Saved on the backend for ' + camId + (stateKey() === 'processing' ? ' and applied to the running monitor.' : '.'));
      renderSettings();
    } catch (e) {
      if (err) err.textContent = e.message;
      ui.toast('error', 'Settings not applied', e.message);
    } finally { if (btn && btn.isConnected) ui.setBusy(btn, false); }
  }

  // ======================= events =======================
  async function loadEvents() {
    if (!root || !api.status.connected || !camId) return;
    try {
      const list = (await api.detection.events(MOD, camId, 24)).map(state.fromBackend);
      const sig = list.length + '|' + (list[0] && list[0].id);
      if (sig !== lastEvSig) { lastEvSig = sig; renderEvents(list); state.pollEvents(); }
    } catch (e) { /* status panel shows backend errors */ }
  }
  function renderEvents(list) {
    const el = root.querySelector('#om-events');
    el.innerHTML = '<div class="card-head"><h2 id="om-ev-h">Recent object events</h2><span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/snapshots">Snapshots</a><a class="btn btn-sm btn-ghost" href="#/alerts">All alerts</a></div>' +
      (list.length ? '<div class="det-row dl-ev-row">' + list.map(function (d) {
        const t = ui.eventType(d.type);
        return '<button class="det-card" data-ev="' + esc(d.id) + '"><span class="det-img">' + (d.image ? '<img src="' + esc(d.image) + '" alt="" loading="lazy">' : '<span class="media-ph">' + icon('check', 20) + '<span>' + esc(t.label) + '</span></span>') +
          '<span class="det-label">' + ui.toneBadge(t.tone, t.label) + '</span></span><span class="det-info"><b class="ellipsis">' + esc(d.label || '') + '</b><span>' + esc(ui.fmtTime(d.timestamp)) + '</span>' +
          '<span class="ellipsis">' + esc(d.description || '') + (d.alertId ? ' · alert' : '') + '</span></span></button>';
      }).join('') + '</div>'
        : ui.emptyState('scan', 'No object events yet', !camId ? 'Select a camera.' : 'An event with a snapshot is recorded when a marked object is confirmed MOVED or MISSING.'));
    el.querySelectorAll('[data-ev]').forEach(function (b) { b.addEventListener('click', function () { const d = list.find(function (x) { return x.id === b.dataset.ev; }); if (d) window.AV.events.openDetection(d); }); });
  }

  function destroy() {
    clearInterval(timer); clearInterval(evTimer);
    detachStream();
    unsub.forEach(function (f) { f(); }); unsub = [];
    root = null;
  }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.objectMonitor = { title: TITLE, render: render, destroy: destroy, onBackend: function () { onBackend(); } };
})();
