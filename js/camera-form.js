/* PYROTECH VISION — Add / Edit Camera page (Reference image 2). */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog, S = window.AV.storage;
  const esc = ui.esc;

  let root, m, password, original, isNew, originalId, editor, dirtyBaseline, saving, previewState, lastTest, unbeforeunload, streamImg, daUnsub = [];

  // ======================= validation =======================
  function parseRtsp(url) {
    const re = /^(rtsps?):\/\/(?:([^:@\/\s]*)(?::([^\/\s]*))?@)?(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(?::(\d*))?(\/[^\s]*)?$/i;
    const mm = re.exec(url);
    if (!mm) return null;
    return { scheme: mm[1].toLowerCase(), user: mm[2], pass: mm[3], host: mm[4], port: mm[5], path: mm[6] || '' };
  }
  function validHost(h) {
    if (/^\[.*\]$/.test(h)) return true; // IPv6 literal
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return h.split('.').every(function (o) { return +o <= 255; });
    if (/^\d+(\.\d+)*$/.test(h)) return false; // partial / malformed IPv4
    return h.length <= 253 && h.split('.').every(function (l) { return /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(l); });
  }
  /** Returns { error, warning }. Syntax only — never implies reachability. */
  function validateSource(type, raw) {
    const v = (raw || '').trim();
    const label = { rtsp: 'RTSP URL', onvif: 'ONVIF device address', webcam: 'Device index or path', file: 'File path or URL' }[type];
    if (!v) return { error: label + ' is required.' };
    if (/\s/.test(v)) return { error: label + ' must not contain spaces.' };
    if (type === 'rtsp') {
      const proto = /^([a-z][a-z0-9+.-]*):/i.exec(v);
      if (!proto || v.indexOf('://') < 0) return { error: 'Malformed URL. Expected rtsp://host:554/path.' };
      if (!/^rtsps?$/i.test(proto[1])) return { error: 'Unsupported protocol "' + proto[1] + ':". Use rtsp:// or rtsps://.' };
      const p = parseRtsp(v);
      if (!p) return { error: 'Malformed RTSP URL. Expected rtsp://host:554/path.' };
      if (!p.host) return { error: 'The URL is missing a host (IP address or hostname).' };
      if (!validHost(p.host)) return { error: 'Host "' + p.host + '" is not a valid IP address or hostname.' };
      if (p.port !== undefined && p.port !== '' && !(+p.port >= 1 && +p.port <= 65535)) return { error: 'Port must be between 1 and 65535.' };
      if (p.port === '') return { error: 'Port is empty — remove the ":" or enter a port (default 554).' };
      if (p.user !== undefined) return { warning: 'This URL contains embedded credentials. They will never be shown in logs and are stripped before local storage — prefer the Username / Password fields.' };
      if (!p.path || p.path === '/') return { warning: 'No stream path given. Most cameras need one (e.g. /Streaming/Channels/101).' };
      return {};
    }
    if (type === 'onvif') {
      if (!/^https?:\/\//i.test(v)) return { error: 'Use the ONVIF device service URL, e.g. http://192.168.1.100/onvif/device_service.' };
      try { const u = new URL(v); if (!validHost(u.hostname.replace(/^\[|\]$/g, '')) && !/^\[/.test(u.host)) return { error: 'Invalid host in the ONVIF address.' }; } catch (e) { return { error: 'Malformed ONVIF address.' }; }
      return {};
    }
    if (type === 'webcam') {
      if (/^\d+$/.test(v) || /^\/dev\/video\d+$/.test(v) || /^video=.+/.test(v)) return {};
      return { error: 'Enter a device index (0, 1, …), /dev/videoN, or a DirectShow name (video=…).' };
    }
    if (type === 'file') {
      if (/^[a-z][a-z0-9+.-]*:\/\//i.test(v) && !/^(https?|rtsps?|file):\/\//i.test(v)) return { error: 'Unsupported protocol. Use a local path, file://, http(s):// or rtsp://.' };
      return {};
    }
    return { error: 'Select a camera type.' };
  }

  function validateConnection(errs) {
    if (!m.name.trim()) errs.name = 'Camera name is required.';
    else if (m.name.trim().length > 64) errs.name = 'Keep the name under 64 characters.';
    else if (state.cameras().some(function (c) { return c.id !== originalId && c.name.trim().toLowerCase() === m.name.trim().toLowerCase(); })) errs.name = 'Another camera already uses this name.';
    if (!cat.CAMERA_TYPES.some(function (t) { return t.id === m.type; })) errs.type = 'Select a supported camera type.';
    const src = validateSource(m.type, m.sourceUrl);
    if (src.error) errs.sourceUrl = src.error;
    if (m.requiresAuth && (m.type === 'rtsp' || m.type === 'onvif')) {
      if (!m.username.trim()) errs.username = 'Username is required when the camera requires authentication.';
    }
    if (!(m.timeoutSec >= 1 && m.timeoutSec <= 120)) errs.timeoutSec = 'Timeout must be 1 – 120 seconds.';
    if (m.reconnect === 'auto' && !(m.reconnectIntervalSec >= 5 && m.reconnectIntervalSec <= 3600)) errs.reconnectIntervalSec = 'Interval must be 5 – 3600 seconds.';
    if (m.streamProfile === 'custom' && !m.customStream.trim()) errs.customStream = 'Enter the custom stream path or profile token.';
    if (m.aiStream !== 'same' && m.type !== 'rtsp') errs.aiStream = 'A separate AI stream is only possible for RTSP cameras.';
    if (m.aiStream === 'custom' && !String(m.aiCustomStream || '').trim()) errs.aiCustomStream = 'Enter the AI stream path, e.g. /Streaming/Channels/102.';
    return errs;
  }

  function validateAll() {
    const errs = validateConnection({});
    if (!/^[A-Za-z0-9_-]{2,32}$/.test(m.id || '')) errs.id = 'Use 2–32 letters, digits, "-" or "_".';
    else if (state.cameras().some(function (c) { return c.id === m.id && c.id !== originalId; })) errs.id = 'Camera ID ' + m.id + ' already exists.';
    // detection modules
    Object.keys(m.detection.modules).forEach(function (k) {
      const e = window.AV.modules.validateSettings(k, m.detection.modules[k]);
      if (Object.keys(e).length) errs['module-' + k] = cat.moduleById(k).name + ': ' + (e.general || e.target || e.modelId || e.missingAfterSec || e.confidence);
      (m.detection.modules[k].zones || []).forEach(function (zid) { if (!m.zones.some(function (z) { return z.id === zid; })) m.detection.modules[k].zones = m.detection.modules[k].zones.filter(function (x) { return x !== zid; }); });
    });
    if (!(m.detection.confidence >= 0.1 && m.detection.confidence <= 0.99)) errs.confidence = 'Detection confidence must be 0.10 – 0.99.';
    // zones
    const zErr = m.zones.map(function (z) { return window.AV.zones.validate(z); }).filter(Boolean);
    if (zErr.length) errs.zones = zErr[0];
    const names = m.zones.map(function (z) { return z.name.trim().toLowerCase(); });
    if (names.some(function (n, i) { return names.indexOf(n) !== i; })) errs.zones = 'Zone names must be unique.';
    // recording
    const r = m.recording;
    if ([0, 3, 5, 10, 15, 30].indexOf(+r.preEventSec) < 0) errs.preEventSec = 'Unsupported pre-event duration.';
    if ([5, 10, 15, 30, 60, 120].indexOf(+r.postEventSec) < 0) errs.postEventSec = 'Unsupported post-event duration.';
    if (!(Number.isInteger(+r.retentionDays) && +r.retentionDays >= 1 && +r.retentionDays <= 365)) errs.retentionDays = 'Retention must be 1 – 365 days.';
    if (!(+r.maxStorageGb >= 1 && +r.maxStorageGb <= 100000)) errs.maxStorageGb = 'Storage limit must be 1 – 100000 GB.';
    if (r.snapshotOnDetection && !r.snapshotClasses.length) errs.snapshotClasses = 'Select at least one detection class, or turn off snapshots on detection.';
    return errs;
  }

  function showErrors(errs) {
    root.querySelectorAll('[data-err]').forEach(function (el) {
      const k = el.dataset.err;
      el.innerHTML = errs[k] ? icon('alertCircle', 13) + '<span>' + esc(errs[k]) + '</span>' : '';
      const inp = root.querySelector('[data-f="' + k + '"]');
      if (inp) inp.setAttribute('aria-invalid', errs[k] ? 'true' : 'false');
    });
    const modErr = Object.keys(errs).filter(function (k) { return k.indexOf('module-') === 0; }).map(function (k) { return errs[k]; });
    root.querySelector('#det-errors').innerHTML = modErr.map(function (t) { return '<div class="field-error">' + icon('alertCircle', 13) + '<span>' + esc(t) + '</span></div>'; }).join('');
  }
  function focusFirstError(errs) {
    const order = ['id', 'name', 'type', 'sourceUrl', 'username', 'password', 'timeoutSec', 'reconnectIntervalSec', 'customStream', 'aiStream', 'aiCustomStream', 'zones', 'confidence', 'preEventSec', 'postEventSec', 'retentionDays', 'maxStorageGb', 'snapshotClasses'];
    const k = order.find(function (x) { return errs[x]; });
    const el = k && (root.querySelector('[data-f="' + k + '"]') || root.querySelector('[data-err="' + k + '"]'));
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); if (el.focus) el.focus({ preventScroll: true }); }
  }

  // ======================= render =======================
  function render(container, params) {
    root = container;
    isNew = !params || !params.id;
    originalId = isNew ? null : params.id;
    password = ''; lastTest = null; saving = false; previewState = 'empty';
    if (isNew) {
      m = cat.newCamera();
      m.id = state.nextCameraId();
      m.requiresAuth = true;
    } else {
      const c = state.getCamera(params.id);
      if (!c) {
        container.innerHTML = ui.emptyState('camera', 'Camera not found', 'No camera with ID "' + params.id + '" exists.', '<a class="btn btn-sm" href="#/cameras">Back to cameras</a>');
        return;
      }
      m = JSON.parse(JSON.stringify(c));
      m.requiresAuth = !!m.username;
    }
    original = JSON.parse(JSON.stringify(m));
    const title = isNew ? 'Add New Camera' : 'Edit Camera';

    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<a href="#/cameras">Cameras</a>' + icon('chevronRight', 14) + '<span class="current">' + (isNew ? 'Add Camera' : esc(m.name)) + '</span></nav>' +
      '<div class="page-head"><span class="head-icon">' + icon(isNew ? 'plus' : 'edit', 24) + '</span><div><h1>' + title + '</h1><p class="sub">Connect your camera, configure detection, and manage monitoring settings.</p></div>' +
      '<div class="actions"><button class="btn btn-ghost btn-lg" id="cf-cancel">Cancel</button><button class="btn btn-lg" id="cf-test">' + icon('plug', 16) + '<span>Test Connection</span></button>' +
      '<button class="btn btn-primary btn-lg" id="cf-save">' + icon('save', 16) + '<span>Save Camera</span></button></div></div>' +
      '<div id="cf-mode"></div>' +
      '<div class="cf-grid">' +
      '<section class="card cf-info" aria-labelledby="s1">' + infoSection() + '</section>' +
      '<section class="card cf-preview" aria-labelledby="s2">' + previewSection() + '</section>' +
      '<section class="card cf-detect" aria-labelledby="s3">' + detectSection() + '</section>' +
      '<div class="cf-side"><section class="card" aria-labelledby="s4" id="cf-detprev"></section><section class="card" aria-labelledby="s5" id="cf-rec"></section></div>' +
      '</div>';
    window.AV.icons.hydrate(container);
    renderMode();
    renderDetPreview();
    renderRecording();
    bindInfo();
    bindPreview();
    bindDetect();
    container.querySelector('#cf-cancel').addEventListener('click', function () { window.AV.router.go(originalId ? '/cameras/' + encodeURIComponent(originalId) : '/cameras'); });
    container.querySelector('#cf-test').addEventListener('click', onTest);
    container.querySelector('#cf-save').addEventListener('click', onSave);
    dirtyBaseline = snapshot();
    unbeforeunload = function (e) { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } };
    window.addEventListener('beforeunload', unbeforeunload);
    if (!isNew && m.source === 'backend' && api.status.connected) loadBackendStream();
    renderDaCam();
    daUnsub = ['camstatus', 'metrics', 'cameras', 'backend'].map(function (evt) { return state.on(evt, renderDaCam); });
  }

  function renderMode() {
    const el = root.querySelector('#cf-mode');
    if (api.status.connected) {
      el.innerHTML = '<div class="notice notice-success mb-12">' + icon('server', 16) + '<div><b>Backend connected.</b> Configuration and credentials are sent to the backend (' + esc(api.base()) + '). The password is never stored in this browser.</div></div>';
    } else {
      el.innerHTML = '<div class="notice notice-warning mb-12">' + icon('alert', 16) + '<div><b>Local configuration mode — no backend connected.</b> Camera metadata will be saved in this browser only. ' +
        'The password is held temporarily in this form and is <b>not</b> persisted anywhere. Connection testing, live preview and snapshots require the camera backend.</div></div>';
    }
  }

  function f(label, key, inner, opts) {
    opts = opts || {};
    return '<div class="field ' + (opts.cls || '') + '"><label for="cf-' + key + '">' + label + (opts.req ? ' <span class="req" aria-hidden="true">*</span>' : '') + (opts.tip || '') + '</label>' + inner +
      (opts.hint ? '<span class="field-hint" id="cf-' + key + '-hint">' + opts.hint + '</span>' : '') + '<span class="field-error" data-err="' + key + '" id="cf-' + key + '-err" aria-live="polite"></span></div>';
  }
  function sourceLabel() { return { rtsp: 'RTSP URL', onvif: 'ONVIF Device Address', webcam: 'Device Index / Path', file: 'File Path / URL' }[m.type]; }
  function sourcePlaceholder() { return { rtsp: 'rtsp://192.168.1.100:554/stream1', onvif: 'http://192.168.1.100/onvif/device_service', webcam: '0  or  /dev/video0', file: '/data/test/gate.mp4  or  http://server/test.mp4' }[m.type]; }

  function infoSection() {
    const tip = '<span class="tip" tabindex="0" aria-label="RTSP URL format help">' + icon('info', 15) + '<span class="tip-body" role="tooltip">Format: <code>rtsp://host[:port]/path</code><br>Port defaults to 554. Examples:<br><code>rtsp://192.168.1.100:554/Streaming/Channels/101</code> (Hikvision)<br><code>rtsp://192.168.1.101:554/cam/realmonitor?channel=1&amp;subtype=0</code> (Dahua)<br>Enter credentials in the Username / Password fields — they are not added to the URL.</span></span>';
    return '<div class="card-head"><h2 id="s1"><span class="step-num">1</span>Camera Information</h2></div>' +
      '<div class="form-grid">' +
      f('Camera Name', 'name', '<input class="input" id="cf-name" data-f="name" maxlength="64" placeholder="e.g. Main Gate Camera" value="' + esc(m.name) + '" required aria-required="true" aria-describedby="cf-name-err">', { req: true, cls: 'span-2' }) +
      f('<span id="cf-src-label">' + sourceLabel() + '</span>', 'sourceUrl', '<input class="input mono-input" id="cf-sourceUrl" data-f="sourceUrl" autocomplete="off" spellcheck="false" placeholder="' + esc(sourcePlaceholder()) + '" value="' + esc(m.sourceUrl) + '" required aria-required="true" aria-describedby="cf-sourceUrl-err cf-sourceUrl-warn">' +
        '<span class="field-warn" id="cf-sourceUrl-warn"></span>', { req: true, cls: 'span-2', tip: tip }) +
      '<div class="span-2"><label class="check"><input type="checkbox" id="cf-auth" ' + (m.requiresAuth ? 'checked' : '') + '><span>Camera requires authentication</span></label></div>' +
      f('Username', 'username', '<input class="input" id="cf-username" data-f="username" autocomplete="off" value="' + esc(m.username) + '" placeholder="admin">') +
      f('Password', 'password', '<div class="input-group"><input class="input" id="cf-password" data-f="password" type="password" autocomplete="new-password" placeholder="' + (isNew ? '••••••••' : 'Leave blank to keep existing') + '" aria-describedby="cf-password-hint">' +
        '<button type="button" class="btn btn-ghost btn-icon addon" id="cf-eye" aria-label="Show password" aria-pressed="false">' + icon('eye', 16) + '</button></div>',
        { hint: api.status.connected ? 'Sent only to the backend over this connection.' : 'Held in this form only — not saved.' }) +
      f('Camera Type', 'type', '<select class="select" id="cf-type" data-f="type">' + cat.CAMERA_TYPES.map(function (t) { return '<option value="' + t.id + '"' + (m.type === t.id ? ' selected' : '') + '>' + esc(t.name) + '</option>'; }).join('') + '</select>', { req: true }) +
      f('Brand / Manufacturer', 'brand', '<select class="select" id="cf-brand"><option value="">Select brand…</option>' + cat.BRANDS.map(function (b) { return '<option' + (m.brand === b ? ' selected' : '') + '>' + b + '</option>'; }).join('') + '</select>') +
      f('Camera Model', 'model', '<input class="input" id="cf-model" value="' + esc(m.model) + '" placeholder="e.g. DS-2CD2143G2-I">') +
      f('Location / Area', 'location', '<input class="input" id="cf-location" list="cf-locations" value="' + esc(m.location) + '" placeholder="e.g. Main Gate"><datalist id="cf-locations">' + cat.LOCATIONS.map(function (l) { return '<option value="' + esc(l) + '">'; }).join('') + '</datalist>') +
      f('Stream Profile', 'streamProfile', '<select class="select" id="cf-streamProfile"><option value="main"' + (m.streamProfile === 'main' ? ' selected' : '') + '>Main stream (full resolution)</option><option value="sub"' + (m.streamProfile === 'sub' ? ' selected' : '') + '>Substream (lower bandwidth)</option><option value="custom"' + (m.streamProfile === 'custom' ? ' selected' : '') + '>Custom stream</option></select>') +
      f('Custom stream path / token', 'customStream', '<input class="input" id="cf-customStream" data-f="customStream" value="' + esc(m.customStream) + '" placeholder="/Streaming/Channels/103">', { cls: 'cf-custom' + (m.streamProfile === 'custom' ? '' : ' hidden') }) +
      f('AI detection stream', 'aiStream', '<select class="select" id="cf-aiStream"><option value="same"' + (m.aiStream === 'same' ? ' selected' : '') + '>Same as the viewing stream</option><option value="sub"' + (m.aiStream === 'sub' ? ' selected' : '') + '>Camera substream (Hikvision / Dahua URL)</option><option value="custom"' + (m.aiStream === 'custom' ? ' selected' : '') + '>Custom AI stream path</option></select>',
        { hint: 'A low-resolution substream for detection saves CPU on the AI box (the model works at 640 px anyway). Falls back to the viewing stream if it cannot be opened.' }) +
      f('AI stream path', 'aiCustomStream', '<input class="input" id="cf-aiCustomStream" data-f="aiCustomStream" value="' + esc(m.aiCustomStream) + '" placeholder="/Streaming/Channels/102">', { cls: 'cf-aicustom' + (m.aiStream === 'custom' ? '' : ' hidden') }) +
      f('Snapshot image', 'snapshotSource', '<select class="select" id="cf-snapshotSource"><option value="main"' + (m.snapshotSource !== 'ai' ? ' selected' : '') + '>Boxes + full-resolution original</option><option value="ai"' + (m.snapshotSource === 'ai' ? ' selected' : '') + '>Boxes only (less CPU)</option></select>', { cls: 'cf-aisnap' + (m.aiStream === 'same' ? ' hidden' : ''), hint: 'The original needs the main stream decoded as well, which costs CPU (measured: 14% of the AI throughput at 1080p, 37% at 4 MP).' }) +
      f('Connection Timeout (s)', 'timeoutSec', '<input class="input" type="number" min="1" max="120" id="cf-timeoutSec" data-f="timeoutSec" value="' + esc(m.timeoutSec) + '">') +
      f('Reconnection', 'reconnect', '<select class="select" id="cf-reconnect"><option value="auto"' + (m.reconnect === 'auto' ? ' selected' : '') + '>Reconnect automatically</option><option value="manual"' + (m.reconnect === 'manual' ? ' selected' : '') + '>Manual reconnect only</option><option value="never"' + (m.reconnect === 'never' ? ' selected' : '') + '>Never reconnect</option></select>') +
      f('Retry interval (s)', 'reconnectIntervalSec', '<input class="input" type="number" min="5" max="3600" id="cf-reconnectIntervalSec" data-f="reconnectIntervalSec" value="' + esc(m.reconnectIntervalSec) + '">', { cls: 'cf-retry' + (m.reconnect === 'auto' ? '' : ' hidden') }) +
      f('Camera ID', 'id', '<input class="input mono-input" id="cf-id" data-f="id" value="' + esc(m.id) + '" ' + (isNew ? '' : 'readonly aria-readonly="true"') + '>', { hint: isNew ? 'Unique identifier used by the backend and in events.' : 'Identifier cannot be changed after creation.' }) +
      f('Description (Optional)', 'description', '<textarea class="textarea" id="cf-description" rows="2" maxlength="300" placeholder="Front gate camera for monitoring entry and exit.">' + esc(m.description) + '</textarea>', { cls: 'span-2' }) +
      '</div>';
  }

  function bindInfo() {
    const q = function (id) { return root.querySelector('#cf-' + id); };
    const bindText = function (id, key, num) {
      q(id).addEventListener('input', function () { m[key] = num ? (q(id).value === '' ? NaN : +q(id).value) : q(id).value; clearErr(key); });
    };
    bindText('name', 'name'); bindText('username', 'username'); bindText('model', 'model'); bindText('location', 'location');
    bindText('description', 'description'); bindText('customStream', 'customStream'); bindText('aiCustomStream', 'aiCustomStream'); bindText('timeoutSec', 'timeoutSec', true);
    bindText('reconnectIntervalSec', 'reconnectIntervalSec', true); bindText('id', 'id');
    q('sourceUrl').addEventListener('input', function () { m.sourceUrl = q('sourceUrl').value; clearErr('sourceUrl'); q('sourceUrl-warn').textContent = ''; renderDaCam(); });
    q('sourceUrl').addEventListener('blur', function () {
      if (!m || !root) return;   // page already closed (blur fires when the form is removed)
      if (!m.sourceUrl.trim()) return;
      const r = validateSource(m.type, m.sourceUrl);
      setErr('sourceUrl', r.error);
      q('sourceUrl-warn').innerHTML = r.warning ? icon('alert', 13) + ' ' + esc(r.warning) : '';
    });
    q('name').addEventListener('input', renderDaCam);
    q('name').addEventListener('blur', function () { if (!m || !root) return; const e = validateConnection({}); setErr('name', m.name ? e.name : ''); });
    q('password').addEventListener('input', function () { password = q('password').value; clearErr('password'); });
    q('eye').addEventListener('click', function () {
      const show = q('password').type === 'password';
      q('password').type = show ? 'text' : 'password';
      q('eye').setAttribute('aria-pressed', show); q('eye').setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      q('eye').innerHTML = icon(show ? 'eyeOff' : 'eye', 16);
    });
    q('auth').addEventListener('change', function () { m.requiresAuth = q('auth').checked; syncAuth(); });
    q('type').addEventListener('change', function () {
      m.type = q('type').value;
      renderDaCam();
      root.querySelector('#cf-src-label').textContent = sourceLabel();
      q('sourceUrl').placeholder = sourcePlaceholder();
      clearErr('sourceUrl'); q('sourceUrl-warn').textContent = '';
      if (m.type === 'webcam' || m.type === 'file') { m.requiresAuth = false; q('auth').checked = false; }
      syncAuth();
    });
    q('brand').addEventListener('change', function () { m.brand = q('brand').value; });
    q('streamProfile').addEventListener('change', function () { m.streamProfile = q('streamProfile').value; root.querySelector('.cf-custom').classList.toggle('hidden', m.streamProfile !== 'custom'); });
    q('aiStream').addEventListener('change', function () {
      m.aiStream = q('aiStream').value;
      root.querySelector('.cf-aicustom').classList.toggle('hidden', m.aiStream !== 'custom');
      root.querySelector('.cf-aisnap').classList.toggle('hidden', m.aiStream === 'same');
    });
    q('snapshotSource').addEventListener('change', function () { m.snapshotSource = q('snapshotSource').value; });
    q('reconnect').addEventListener('change', function () { m.reconnect = q('reconnect').value; root.querySelector('.cf-retry').classList.toggle('hidden', m.reconnect !== 'auto'); });
    syncAuth();
  }
  function syncAuth() {
    const on = m.requiresAuth;
    ['username', 'password'].forEach(function (k) { const el = root.querySelector('#cf-' + k); el.disabled = !on; if (!on) clearErr(k); });
    root.querySelector('#cf-eye').disabled = !on;
  }
  function setErr(key, msg) {
    const el = root.querySelector('[data-err="' + key + '"]'); if (!el) return;
    el.innerHTML = msg ? icon('alertCircle', 13) + '<span>' + esc(msg) + '</span>' : '';
    const inp = root.querySelector('[data-f="' + key + '"]'); if (inp) inp.setAttribute('aria-invalid', msg ? 'true' : 'false');
  }
  function clearErr(key) { setErr(key, ''); }

  // ======================= preview + zones =======================
  function previewSection() {
    const tb = function (tool, ic, label) { return '<button type="button" class="btn btn-sm tool-btn" data-tool="' + tool + '" aria-pressed="false">' + icon(ic, 15) + '<span>' + label + '</span></button>'; };
    return '<div class="card-head"><h2 id="s2"><span class="step-num">2</span>Live Preview &amp; Detection Area</h2><span class="spacer"></span><span id="pv-status"></span></div>' +
      '<div class="da-cam" id="da-cam" aria-live="polite"></div>' +
      '<div class="zone-toolbar" role="toolbar" aria-label="Detection area tools">' +
      tb('select', 'pen', 'Select / Edit') + tb('rectangle', 'square', 'Rectangle') + tb('polygon', 'polygon', 'Polygon') +
      '<button type="button" class="btn btn-sm btn-primary hidden" id="zt-finish">' + icon('check', 15) + '<span>Finish Polygon</span></button>' +
      '<button type="button" class="btn btn-sm hidden" id="zt-cancel">' + icon('x', 15) + '<span>Cancel</span></button>' +
      '<button type="button" class="btn btn-sm" id="zt-undo">' + icon('undo', 15) + '<span>Undo Last</span></button>' +
      '<button type="button" class="btn btn-sm" id="zt-clear">' + icon('trash', 15) + '<span>Clear All</span></button>' +
      '<label class="check zt-check"><input type="checkbox" id="zt-grid"><span>Show Grid</span></label>' +
      '<label class="check zt-check"><input type="checkbox" id="zt-enabled" ' + (m.zonesEnabled !== false ? 'checked' : '') + '><span>Detection areas enabled</span></label></div>' +
      '<div class="pv-stage" id="pv-stage"><div class="pv-media" id="pv-media"></div>' +
      '<div class="pv-time" id="pv-time" hidden></div>' +
      '<div class="pv-tools"><button type="button" class="pv-btn" id="pv-full" aria-label="Full screen preview" title="Full screen">' + icon('maximize', 18) + '</button>' +
      '<button type="button" class="pv-btn" id="pv-snap" aria-label="Capture snapshot" title="Capture snapshot">' + icon('snapshot', 18) + '</button></div></div>' +
      '<p class="zone-hint" id="zone-hint" aria-live="polite"></p>' +
      '<div id="test-result" aria-live="polite"></div>' +
      '<div class="zone-list" id="zone-list"></div>' +
      '<span class="field-error" data-err="zones"></span>' +
      '<p class="dim mt-8" style="font-size:12px">' + icon('info', 13) + ' Zones are stored as normalised coordinates (0–1) of the video frame so they stay aligned at any preview size. The backend must apply them to inference for zone-restricted detection.</p>';
  }

  function bindPreview() {
    const stage = root.querySelector('#pv-stage');
    editor = new window.AV.ZoneEditor(stage, {
      zones: m.zones, colors: cat.ZONE_COLORS, enabled: m.zonesEnabled !== false,
      onChange: function (zones) { m.zones = zones; renderZoneList(); syncToolbar(); clearErr('zones'); },
      onToolChange: syncToolbar
    });
    root.querySelectorAll('[data-tool]').forEach(function (b) { b.addEventListener('click', function () {
      if (!editor.media && b.dataset.tool !== 'select') { ui.toast('info', 'Load a preview first', 'Detection areas are drawn on a real camera frame. Run Test Connection (or open a saved camera with the backend running) to load one.'); return; }
      editor.setTool(b.dataset.tool); editor.canvas.focus();
    }); });
    root.querySelector('#zt-finish').addEventListener('click', function () { const r = editor.finishPolygon(); if (!r.ok) ui.toast('warning', 'Polygon not finished', r.error); else ui.toast('success', 'Zone added', r.zone.name + ' — rename it and choose its detection modules below.'); syncToolbar(); });
    root.querySelector('#zt-cancel').addEventListener('click', function () { editor.cancelDraft(); syncToolbar(); });
    root.querySelector('#zt-undo').addEventListener('click', function () { if (!editor.undo()) ui.toast('info', 'Nothing to undo'); syncToolbar(); });
    root.querySelector('#zt-clear').addEventListener('click', async function () {
      if (!m.zones.length && !editor.isDrawing()) { ui.toast('info', 'No zones to clear'); return; }
      const ok = await ui.confirm({ title: 'Clear all detection areas?', message: 'Remove all ' + m.zones.length + ' zone(s) from this camera? You can still use Undo Last before saving.', confirmText: 'Clear all', danger: true });
      if (ok) { editor.cancelDraft(); editor.clearAll(); }
    });
    root.querySelector('#zt-grid').addEventListener('change', function (e) { editor.setGrid(e.target.checked); });
    root.querySelector('#zt-enabled').addEventListener('change', function (e) { m.zonesEnabled = e.target.checked; editor.setEnabled(e.target.checked); renderZoneList(); });
    root.querySelector('#pv-full').addEventListener('click', function () { ui.fullscreen(stage); });
    root.querySelector('#pv-snap').addEventListener('click', function (e) {
      const saved = !isNew && state.getCamera(originalId);
      if (!saved) { ui.toast('info', 'Save the camera first', 'Snapshots are captured and stored by the backend for saved cameras.'); return; }
      window.AV.media.captureSnapshot(saved, e.currentTarget);
    });
    setPreview('empty');
    renderZoneList();
    syncToolbar();
  }

  function syncToolbar() {
    if (!editor) return;
    root.querySelectorAll('[data-tool]').forEach(function (b) { b.setAttribute('aria-pressed', b.dataset.tool === editor.tool); });
    const drawingPoly = editor.tool === 'polygon' && editor.isDrawing();
    root.querySelector('#zt-finish').classList.toggle('hidden', !drawingPoly);
    root.querySelector('#zt-cancel').classList.toggle('hidden', !editor.isDrawing());
    root.querySelector('#zt-undo').disabled = !editor.canUndo();
    root.querySelector('#zt-clear').disabled = !m.zones.length;
    const hint = root.querySelector('#zone-hint');
    if (!editor.media) hint.textContent = 'Load a preview frame to draw detection areas.';
    else if (editor.tool === 'rectangle') hint.textContent = 'Click and drag on the preview to draw a rectangle.';
    else if (editor.tool === 'polygon') hint.textContent = editor.isDrawing() ? editor.draftPointCount() + ' point(s). Click to add points; click the first point, double-click, or press Enter / Finish Polygon to close. Esc cancels.' : 'Click on the preview to place the first polygon point.';
    else hint.textContent = m.zones.length ? 'Click a zone to select it, drag it to move, drag its handles to reshape. Delete removes it, arrow keys nudge it.' : 'Choose Rectangle or Polygon to draw a detection area.';
  }

  function countAreaNotice() {
    const on = m.detection.modules.count && m.detection.modules.count.enabled;
    if (!on || m.zones.some(function (z) { return (z.modules || []).indexOf('count') >= 0; })) return '';
    return '<div class="notice notice-info mt-8">' + icon('users', 16) + '<div><b>Human Count needs a detection area.</b> ' + (m.zones.length ? 'Tick "Human Count" on a zone below, or draw' : 'Draw') + ' a rectangle or polygon on the camera feed above. Only heads inside it are counted when you press Capture &amp; Count.</div></div>';
  }

  function renderZoneList() {
    const el = root.querySelector('#zone-list');
    if (!m.zones.length) { el.innerHTML = countAreaNotice(); return; }
    const mods = cat.MODULES;
    el.innerHTML = '<h3 class="zl-title">Detection zones (' + m.zones.length + ')</h3>' + m.zones.map(function (z) {
      const coords = z.type === 'rectangle' ? { type: 'rectangle', x: z.x, y: z.y, width: z.width, height: z.height } : { type: 'polygon', points: z.points };
      const err = window.AV.zones.validate(z);
      return '<div class="zone-row' + (editor && editor.selected === z.id ? ' selected' : '') + '" data-zone="' + esc(z.id) + '">' +
        '<input type="color" class="zone-color" value="' + esc(z.color) + '" aria-label="Colour for ' + esc(z.name) + '">' +
        '<input class="input input-sm zone-name" value="' + esc(z.name) + '" maxlength="40" aria-label="Zone name">' +
        '<span class="badge badge-muted">' + icon(z.type === 'rectangle' ? 'square' : 'polygon', 12) + (z.type === 'rectangle' ? 'Rectangle' : 'Polygon · ' + z.points.length + ' pts') + '</span>' +
        ui.switchHtml('zen-' + z.id, z.enabled !== false, 'Enable zone ' + z.name, { attrs: 'data-zen' }) +
        '<button type="button" class="btn btn-xs btn-ghost" data-zsel aria-label="Select zone ' + esc(z.name) + '">' + icon('target', 13) + '</button>' +
        '<button type="button" class="btn btn-xs btn-ghost danger-hover" data-zdel aria-label="Remove zone ' + esc(z.name) + '">' + icon('trash', 13) + '</button>' +
        '<div class="zone-mods" role="group" aria-label="Modules applied in ' + esc(z.name) + '">' + mods.map(function (md) {
          const on = (z.modules || []).indexOf(md.id) >= 0;
          return '<button type="button" class="zmod' + (on ? ' on' : '') + '" data-zmod="' + md.id + '" aria-pressed="' + on + '" title="' + esc(md.name) + '">' + icon(md.icon, 12) + '<span>' + esc(md.name.replace(' Detection', '')) + '</span></button>';
        }).join('') + '</div>' +
        (err ? '<div class="field-error" style="grid-column:1/-1">' + icon('alertCircle', 13) + esc(err) + '</div>' : '') +
        '<details class="zone-coords"><summary>Normalised coordinates</summary><code>' + esc(JSON.stringify(coords)) + '</code></details></div>';
    }).join('') + countAreaNotice();
    window.AV.icons.hydrate(el);
    el.querySelectorAll('.zone-row').forEach(function (row) {
      const id = row.dataset.zone;
      row.querySelector('.zone-name').addEventListener('change', function (e) {
        const v = e.target.value.trim();
        if (!v) { e.target.value = m.zones.find(function (z) { return z.id === id; }).name; ui.toast('warning', 'Zone name required'); return; }
        editor.updateZone(id, { name: v });
      });
      row.querySelector('.zone-color').addEventListener('change', function (e) { editor.updateZone(id, { color: e.target.value }); });
      row.querySelector('[data-zen]').addEventListener('change', function (e) { editor.updateZone(id, { enabled: e.target.checked }); });
      row.querySelector('[data-zsel]').addEventListener('click', function () { editor.setTool('select'); editor.select(id); editor.canvas.focus(); });
      row.querySelector('[data-zdel]').addEventListener('click', function () {
        const z = m.zones.find(function (x) { return x.id === id; });
        editor.removeZone(id);
        ui.toast('info', 'Zone removed', (z ? z.name : 'Zone') + ' removed. Use Undo Last to restore it.');
      });
      row.querySelectorAll('[data-zmod]').forEach(function (b) { b.addEventListener('click', function () {
        const z = m.zones.find(function (x) { return x.id === id; });
        const list = (z.modules || []).slice(); const i = list.indexOf(b.dataset.zmod);
        if (i >= 0) list.splice(i, 1); else list.push(b.dataset.zmod);
        editor.updateZone(id, { modules: list });
        // keep the module's own zone list consistent
        const mc = m.detection.modules[b.dataset.zmod];
        mc.zones = (mc.zones || []).filter(function (x) { return x !== id; }); if (i < 0) mc.zones.push(id);
        if (b.dataset.zmod === 'count' && i < 0) ui.toast('success', 'Human Count area: ' + z.name, 'Heads are counted inside this area when you press Capture & Count (Human Count › Head count) after saving the camera.');
      }); });
    });
  }

  /** Human Count selected: ask for its detection area (drawn on the camera feed, Human Count ticked). */
  function askCountArea() {
    const has = m.zones.some(function (z) { return (z.modules || []).indexOf('count') >= 0; });
    if (has) { ui.toast('info', 'Human Count selected', 'Heads are counted inside the area(s) with Human Count ticked. Use Capture & Count on the Human Count page.'); return; }
    ui.toast('info', 'Select the Human Count detection area', m.zones.length ? 'Tick "Human Count" on an existing zone below, or draw a new rectangle / polygon on the camera feed.' : 'Draw a rectangle or polygon on the camera feed. Only heads inside it are counted.');
    const sec = root.querySelector('.cf-preview');
    if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (editor && editor.media && !m.zones.length) { editor.setTool('rectangle'); syncToolbar(); }
    renderZoneList();
  }

  function setPreview(st, info) {
    previewState = st;
    const box = root.querySelector('#pv-media'), status = root.querySelector('#pv-status'), time = root.querySelector('#pv-time');
    const snapBtn = root.querySelector('#pv-snap');
    const canSnap = !isNew && api.status.connected && m.source === 'backend';
    snapBtn.disabled = !canSnap;
    snapBtn.title = canSnap ? 'Capture snapshot' : 'Snapshot capture needs a saved camera and the camera backend';
    time.hidden = true;
    if (st === 'empty' || st === 'error') {
      if (editor) editor.setMedia(null);
      box.innerHTML = '<div class="media-ph' + (st === 'error' ? ' err' : '') + '">' + icon(st === 'error' ? 'wifiOff' : 'camera', 34) +
        '<b>' + (st === 'error' ? esc(info || 'Preview unavailable') : 'No preview loaded') + '</b>' +
        '<span>' + (api.status.connected ? 'Run Test Connection to fetch a decoded frame from the backend.' : 'Live preview requires the camera backend (RTSP is decoded server-side — browsers cannot play RTSP directly).') + '</span>' +
        '<div class="row wrap" style="justify-content:center;margin-top:6px"><button type="button" class="btn btn-sm" data-pv="test">' + icon('plug', 14) + 'Test connection</button></div></div>';
      box.querySelector('[data-pv="test"]').addEventListener('click', onTest);
      status.innerHTML = st === 'error' ? '<span class="badge badge-danger">' + icon('wifiOff', 12) + 'Error</span>' : '<span class="badge badge-muted">' + icon('wifiOff', 12) + 'Not connected</span>';
    } else if (st === 'connecting') {
      box.innerHTML = '<div class="media-ph"><span class="spinner lg"></span><b>Connecting…</b><span>Waiting for the backend to open the stream and decode a frame.</span></div>';
      status.innerHTML = '<span class="badge badge-info"><span class="spinner" style="width:10px;height:10px;border-width:2px"></span>Connecting</span>';
    }
    syncToolbar();
  }

  /** kind: 'frame' (decoded by a connection test) | 'live' (backend MJPEG stream) */
  function loadPreviewImage(src, kind, ts) {
    const box = root.querySelector('#pv-media'), status = root.querySelector('#pv-status'), time = root.querySelector('#pv-time');
    const img = new Image();
    img.className = 'pv-img'; img.alt = kind === 'live' ? 'Live camera stream' : 'Frame decoded by the backend';
    img.onload = function () {
      box.innerHTML = '';
      box.appendChild(img);
      const labels = { frame: ['badge-success', 'Frame from connection test'], live: ['badge-live', 'LIVE'] };
      box.insertAdjacentHTML('beforeend', '<span class="badge ' + labels[kind][0] + ' pv-flag">' + labels[kind][1] + '</span>');
      status.innerHTML = '<span class="badge badge-success"><span class="dot"></span>Connected</span>';
      time.hidden = false; time.textContent = ui.fmtTime(ts || new Date().toISOString());
      previewState = kind;
      editor.setMedia(img);
      if (editor.tool === 'select' && !m.zones.length) editor.setTool('rectangle');
      syncToolbar();
    };
    img.onerror = function () { setPreview('error', kind === 'live' ? 'Stream could not be decoded by the browser' : 'Image could not be loaded'); };
    img.src = src;
    if (kind === 'live') streamImg = img;
  }

  async function loadBackendStream() {
    setPreview('connecting');
    try {
      const s = await api.cameras.stream(originalId);
      if (s.transport === 'mjpeg' || s.transport === 'image') loadPreviewImage(api.resolveUrl(s.url), 'live');
      else setPreview('error', 'Stream transport "' + s.transport + '" cannot be used for zone drawing — run Test Connection to get a still frame.');
    } catch (e) { setPreview('error', e.message); }
  }

  // ======================= detection settings =======================
  function detectSection() {
    return '<div class="card-head"><h2 id="s3"><span class="step-num">3</span>Detection Settings</h2><p class="sub">Select AI models and configure detection parameters.</p></div>' +
      '<div class="dm-grid" id="dm-grid"></div><div id="det-errors" class="mt-8"></div>' +
      '<div class="dm-sliders"><div class="field"><label for="cf-conf">Detection Confidence</label><div class="range-row"><input class="range" type="range" id="cf-conf" data-f="confidence" min="0.10" max="0.99" step="0.01" value="' + Number(m.detection.confidence).toFixed(2) + '" data-range-out="cf-conf-out"><output class="range-val" id="cf-conf-out">' + Number(m.detection.confidence).toFixed(2) + '</output></div>' +
      '<button type="button" class="link-btn" id="cf-conf-apply" style="font-size:12px;align-self:flex-start">Apply to all modules</button><span class="field-error" data-err="confidence"></span></div>' +
      '<div class="field"><label for="cf-sens">Alert Sensitivity</label><div class="range-row"><input class="range" type="range" id="cf-sens" min="0" max="2" step="1" value="' + ['low', 'medium', 'high'].indexOf(m.detection.sensitivity) + '" data-range-out="cf-sens-out" data-format="sens"><output class="range-val" id="cf-sens-out"></output></div>' +
      '<span class="field-hint">Sensitivity must be translated into backend alert rules (e.g. minimum consecutive frames) where supported.</span></div></div>';
  }
  /** Modules running on the backend for this camera right now (one shared camera connection). */
  function runningModules() {
    const ses = !isNew && api.status.connected ? state.session(originalId) : null;
    return ses && Array.isArray(ses.modules) ? ses.modules : [];
  }
  function renderModuleCards() {
    const el = root.querySelector('#dm-grid');
    const running = runningModules();
    const canRun = !isNew && api.status.connected && m.source === 'backend';
    el.innerHTML = (isNew ? '<div class="dm-primary-note field-hint" style="grid-column:1/-1">' + icon('info', 13) + ' A new camera starts with <b>one primary detection</b>: choose one module; it starts when the camera is saved. Add more modules after saving (they share the same camera connection).</div>' : '') +
      cat.MODULES.map(function (md) {
      const cfg = m.detection.modules[md.id];
      const on = running.indexOf(md.id) >= 0;
      const runBtn = canRun && md.live
        ? '<button type="button" class="dm-run btn btn-sm ' + (on ? 'btn-ghost' : 'btn-primary') + '" data-dmrun="' + md.id + '" data-on="' + (on ? 1 : 0) + '" style="grid-column:1/-1">' + icon(on ? 'square' : 'play', 13) + '<span>' + (on ? 'Running — stop' : 'Add to this camera (start)') + '</span></button>'
        : '';
      return '<div class="dm-card' + (cfg.enabled ? ' on' : '') + '"><span class="dm-icon" style="color:' + md.color + '">' + icon(md.icon, 22) + '</span><b style="line-height:1.25">' + esc(md.name) + (on ? ' <span class="badge badge-success" style="font-size:10px">running</span>' : '') + '</b>' +
        ui.switchHtml('dm-' + md.id, cfg.enabled, (isNew ? 'Primary detection: ' : 'Enable ') + md.name, { attrs: 'data-dm="' + md.id + '"', cls: 'blue' }) +
        '<button type="button" class="dm-settings" data-dms="' + md.id + '" aria-label="' + esc(md.name) + ' settings"><span>Settings</span><span class="dim" style="font-size:11px;margin-left:auto">' + (cfg.enabled ? 'conf ' + Number(cfg.confidence).toFixed(2) : 'off') + '</span>' + icon('chevronDown', 14) + '</button>' + runBtn + '</div>';
    }).join('');
    el.querySelectorAll('[data-dmrun]').forEach(function (b) { b.addEventListener('click', async function () {
      const id = b.dataset.dmrun, on = b.dataset.on === '1', name = cat.moduleById(id).name;
      if (on && !(await ui.confirm({ title: 'Stop ' + name + ' on this camera?', message: 'The other modules keep running on the same camera connection.', confirmText: 'Stop' }))) return;
      ui.setBusy(b, true, on ? 'Stopping…' : 'Starting…');
      try {
        if (on) await api.detection.stop(id, originalId); else await api.detection.start(id, originalId);
        ui.toast('success', name + (on ? ' stopped' : ' started'), on ? '' : 'Runs on the existing camera connection (no second RTSP stream).');
        await state.pollCameras();
      } catch (e) { ui.toast('error', name + ': ' + (e.code || 'error'), e.message); }
      if (root && root.isConnected) renderModuleCards();
    }); });
    el.querySelectorAll('[data-dm]').forEach(function (inp) { inp.addEventListener('change', function () {
      if (isNew && inp.checked) {     // new camera: one primary detection - selecting one clears the others
        Object.keys(m.detection.modules).forEach(function (k) { if (k !== inp.dataset.dm) m.detection.modules[k].enabled = false; });
      }
      m.detection.modules[inp.dataset.dm].enabled = inp.checked;
      renderModuleCards(); showErrors({}); renderZoneList();
      if (inp.checked && inp.dataset.dm === 'count') askCountArea();
      else if (inp.checked) { const rs = window.AV.modules.runtimeStatus(inp.dataset.dm); if (rs.tone !== 'success') ui.toast('info', cat.moduleById(inp.dataset.dm).name + ' selected', 'Saved with the camera configuration. ' + rs.text + '.'); }
    }); });
    el.querySelectorAll('[data-dms]').forEach(function (b) { b.addEventListener('click', async function () {
      const id = b.dataset.dms;
      const next = await window.AV.modules.openSettings(id, m.detection.modules[id], m.zones);
      if (next) {
        m.detection.modules[id] = next;
        if (isNew && next.enabled) Object.keys(m.detection.modules).forEach(function (k) { if (k !== id) m.detection.modules[k].enabled = false; });
        // reflect module→zone mapping on zones
        m.zones.forEach(function (z) {
          const has = (z.modules || []).indexOf(id) >= 0, want = next.zones.indexOf(z.id) >= 0;
          if (has !== want) editor.updateZone(z.id, { modules: want ? (z.modules || []).concat([id]) : z.modules.filter(function (x) { return x !== id; }) }, false);
        });
        renderModuleCards(); renderZoneList();
        ui.toast('success', cat.moduleById(id).name + ' settings applied', 'Remember to save the camera.');
      }
    }); });
  }
  function bindDetect() {
    renderModuleCards();
    window.AV.modules.wireRanges(root.querySelector('.cf-detect'));
    root.querySelector('#cf-conf').addEventListener('input', function (e) { m.detection.confidence = Math.round(parseFloat(e.target.value) * 100) / 100; });
    root.querySelector('#cf-sens').addEventListener('input', function (e) { m.detection.sensitivity = ['low', 'medium', 'high'][+e.target.value]; });
    root.querySelector('#cf-conf-apply').addEventListener('click', function () {
      Object.keys(m.detection.modules).forEach(function (k) { m.detection.modules[k].confidence = m.detection.confidence; });
      renderModuleCards();
      ui.toast('success', 'Confidence applied', 'All modules now use ' + m.detection.confidence.toFixed(2) + '.');
    });
  }

  // ======================= detection preview + recording =======================
  function renderDetPreview() {
    const el = root.querySelector('#cf-detprev');
    const dets = isNew ? [] : state.detections().filter(function (d) { return d.cameraId === originalId; }).slice(0, 4);
    el.innerHTML = '<div class="card-head"><h2 id="s4"><span class="step-num">4</span>Detection Preview</h2><p class="sub">Recent detections from this camera.</p></div>' +
      (dets.length ? '<div class="dp-row">' + dets.map(function (d) {
        const t = ui.eventType(d.type);
        return '<button type="button" class="dp-item" data-det="' + esc(d.id) + '">' + (d.image ? '<img src="' + esc(d.image) + '" alt="" loading="lazy">' : '') + '<span class="dp-tag">' + ui.toneBadge(t.tone, t.label + (d.confidence != null ? ' ' + Number(d.confidence).toFixed(2) : '')) + '</span>' +
          '<span class="dp-time">' + esc(ui.fmtTime(d.timestamp, { noYear: true })) + '</span></button>';
      }).join('') + '</div>'
        : '<div class="dp-empty">' + icon('scan', 20) + '<span>' + (isNew ? 'Detection events will appear here once the camera is saved and the inference backend processes its stream.' : 'No detections reported for this camera yet.') + '</span></div>');
    el.querySelectorAll('[data-det]').forEach(function (b) { b.addEventListener('click', function () { const d = state.detections().find(function (x) { return x.id === b.dataset.det; }); if (d) window.AV.events.openDetection(d); }); });
  }

  function renderRecording() {
    const el = root.querySelector('#cf-rec'), r = m.recording;
    const count = isNew ? 0 : state.snapshots().filter(function (s) { return s.cameraId === originalId; }).length;
    const storage = state.remote.storage;
    const sel = function (key, opts, unit) { return '<select class="select select-sm" id="rc-' + key + '" data-f="' + key + '">' + opts.map(function (o) { return '<option value="' + o + '"' + (+r[key] === o ? ' selected' : '') + '>' + o + ' ' + unit + '</option>'; }).join('') + '</select>'; };
    const classOpts = Object.keys(cat.EVENT_TYPES).map(function (k) { return '<label class="check"><input type="checkbox" name="snapcls" value="' + k + '" ' + (r.snapshotClasses.indexOf(k) >= 0 ? 'checked' : '') + '><span>' + esc(cat.EVENT_TYPES[k].label) + '</span></label>'; }).join('');
    el.innerHTML = '<div class="card-head"><h2 id="s5"><span class="step-num">5</span>Recording &amp; Snapshots</h2></div>' +
      '<div class="rec-grid">' +
      '<div class="rec-row"><span>Save snapshots on detection</span>' + ui.switchHtml('rc-snap', r.snapshotOnDetection, 'Save snapshots on detection') + '</div>' +
      '<div class="rec-row"><label for="rc-preEventSec">Pre-event recording</label>' + sel('preEventSec', [0, 3, 5, 10, 15, 30], 'sec') + '</div>' +
      '<div class="rec-row"><span>Continuous recording</span>' + ui.switchHtml('rc-cont', r.continuous, 'Continuous recording') + '</div>' +
      '<div class="rec-row"><label for="rc-postEventSec">Post-event recording</label>' + sel('postEventSec', [5, 10, 15, 30, 60, 120], 'sec') + '</div>' +
      '<div class="rec-row"><label for="rc-retentionDays">Snapshot retention (days)</label><input class="input input-sm" type="number" min="1" max="365" id="rc-retentionDays" data-f="retentionDays" value="' + esc(r.retentionDays) + '" style="width:110px"></div>' +
      '<div class="rec-row"><label for="rc-maxStorageGb">Max storage (GB)</label><input class="input input-sm" type="number" min="1" max="100000" id="rc-maxStorageGb" data-f="maxStorageGb" value="' + esc(r.maxStorageGb) + '" style="width:110px"></div>' +
      '</div>' +
      '<span class="field-error" data-err="preEventSec"></span><span class="field-error" data-err="postEventSec"></span><span class="field-error" data-err="retentionDays"></span><span class="field-error" data-err="maxStorageGb"></span>' +
      '<details class="snap-classes mt-12" ' + (r.snapshotOnDetection ? 'open' : '') + '><summary>Snapshot detection classes (' + r.snapshotClasses.length + ')</summary><div class="row wrap mt-8" style="gap:10px 16px">' + classOpts + '</div></details>' +
      '<span class="field-error" data-err="snapshotClasses"></span>' +
      '<p class="dim mt-12" style="font-size:12px">' + icon('hardDrive', 13) + ' ' + count + ' snapshot record(s) for this camera · Available storage: ' +
      (storage && typeof storage.capacityBytes === 'number' ? ((storage.capacityBytes - storage.usedBytes) / 1073741824).toFixed(1) + ' GB free (backend)' : 'not reported') +
      '. Snapshots and recordings are captured and stored by the backend; pre/post-event recording requires backend buffering support.</p>';
    el.querySelector('#rc-snap').addEventListener('change', function (e) { r.snapshotOnDetection = e.target.checked; el.querySelector('.snap-classes').open = e.target.checked; clearErr('snapshotClasses'); });
    el.querySelector('#rc-cont').addEventListener('change', function (e) { r.continuous = e.target.checked; if (e.target.checked) ui.toast('info', 'Continuous recording requested', 'Requires backend recording support and sufficient storage.'); });
    ['preEventSec', 'postEventSec'].forEach(function (k) { el.querySelector('#rc-' + k).addEventListener('change', function (e) { r[k] = +e.target.value; }); });
    ['retentionDays', 'maxStorageGb'].forEach(function (k) { el.querySelector('#rc-' + k).addEventListener('input', function (e) { r[k] = e.target.value === '' ? NaN : +e.target.value; clearErr(k); }); });
    el.querySelectorAll('[name="snapcls"]').forEach(function (c) { c.addEventListener('change', function () {
      r.snapshotClasses = Array.prototype.map.call(el.querySelectorAll('[name="snapcls"]:checked'), function (x) { return x.value; });
      el.querySelector('.snap-classes summary').textContent = 'Snapshot detection classes (' + r.snapshotClasses.length + ')';
      clearErr('snapshotClasses');
    }); });
  }

  // ======================= actions =======================
  function buildPayloadForTest() {
    const p = state.normalizeCamera(m);
    delete p.source; delete p.image;
    if (m.requiresAuth && password) p.password = password;
    if (!m.requiresAuth) { p.username = ''; }
    return p;
  }

  async function onTest() {
    const btn = root.querySelector('#cf-test');
    const errs = validateConnection({});
    if (m.requiresAuth && (m.type === 'rtsp' || m.type === 'onvif') && !password && api.status.connected && (isNew || m.source !== 'backend')) errs.password = 'Enter the password to test the connection.';
    showErrors(errs);
    if (Object.keys(errs).length) { focusFirstError(errs); ui.toast('warning', 'Check the connection fields', 'Fix the highlighted fields before testing.'); return; }
    ui.setBusy(btn, true, 'Testing…');
    if (api.status.connected) setPreview('connecting');
    const useSaved = !isNew && m.source === 'backend' && !password && JSON.stringify(connFields(m)) === JSON.stringify(connFields(original));
    const r = await window.AV.connection.runTest(useSaved ? originalId : buildPayloadForTest());
    ui.setBusy(btn, false);
    lastTest = Object.assign({ at: new Date().toISOString(), fields: JSON.stringify(connFields(m)) }, r);
    root.querySelector('#test-result').innerHTML = '<div class="test-box mt-12">' + window.AV.connection.resultHtml(r) + '</div>';
    window.AV.icons.hydrate(root.querySelector('#test-result'));
    if (r.ok && r.frame) loadPreviewImage(r.frame, 'frame');
    else if (r.code === 'BACKEND_UNAVAILABLE') setPreview('empty');
    else if (!r.ok) setPreview('error', window.AV.connection.RESULTS[r.code].title);
    else setPreview('empty');
    if (!isNew && r.code !== 'BACKEND_UNAVAILABLE' && r.code !== 'INVALID_CONFIG') state.recordConnectionResult(originalId, r);
    ui.toast(r.ok ? 'success' : (r.code === 'BACKEND_UNAVAILABLE' ? 'warning' : 'error'), window.AV.connection.RESULTS[r.code].title, r.message);
  }
  function connFields(c) { return { type: c.type, sourceUrl: c.sourceUrl, username: c.username, streamProfile: c.streamProfile, customStream: c.customStream, timeoutSec: c.timeoutSec }; }

  async function onSave() {
    if (saving) return;
    if (editor && editor.isDrawing()) { ui.toast('warning', 'Finish or cancel the polygon', 'A detection area is still being drawn.'); return; }
    const errs = validateAll();
    showErrors(errs);
    if (Object.keys(errs).length) {
      focusFirstError(errs);
      ui.toast('error', 'Camera not saved', Object.keys(errs).length + ' field(s) need attention.');
      return;
    }
    if (api.status.connected && isNew && m.requiresAuth && (m.type === 'rtsp' || m.type === 'onvif') && !password) {
      showErrors({ password: 'Enter the password so the backend can store it securely.' }); focusFirstError({ password: 1 }); return;
    }
    const btn = root.querySelector('#cf-save');
    saving = true; ui.setBusy(btn, true, 'Saving…');
    root.querySelector('#cf-test').disabled = true;
    const toSave = JSON.parse(JSON.stringify(m));
    toSave.name = toSave.name.trim(); toSave.sourceUrl = toSave.sourceUrl.trim();
    if (!toSave.requiresAuth) toSave.username = '';
    // A camera is only "online" if a real backend test of this exact configuration succeeded.
    if (lastTest && lastTest.ok && lastTest.fields === JSON.stringify(connFields(m))) { toSave.status = 'online'; toSave.lastConnectedAt = lastTest.at; toSave.lastError = null; }
    else if (isNew || JSON.stringify(connFields(m)) !== JSON.stringify(connFields(original))) { toSave.status = 'unverified'; toSave.lastError = null; }
    try {
      const res = await state.saveCamera(toSave, { originalId: originalId, password: m.requiresAuth ? password : '' });
      password = '';
      dirtyBaseline = snapshot();
      ui.toast('success', isNew ? 'Camera added' : 'Camera updated',
        res.persistedTo === 'backend' ? res.camera.name + ' saved on the backend.' : res.camera.name + ' saved in this browser only (local mode). ' + (m.requiresAuth ? 'Password was not stored.' : ''));
      // new camera: start its ONE primary detection (other modules are added explicitly later)
      const primary = isNew && res.persistedTo === 'backend' ? cat.MODULES.filter(function (md) { return md.live && toSave.detection.modules[md.id] && toSave.detection.modules[md.id].enabled; }) : [];
      if (primary.length === 1) {
        const pid = primary[0].id, cid = res.camera.id || toSave.id;
        api.detection.start(pid, cid).then(function () { ui.toast('success', primary[0].name + ' started', 'Primary detection of ' + res.camera.name + '.'); state.pollCameras(); })
          .catch(function (e) { ui.toast('error', primary[0].name + ' not started', (e.code ? e.code + ': ' : '') + e.message); });
      }
      window.AV.router.go('/cameras/' + encodeURIComponent(res.camera.id || toSave.id));
    } catch (e) {
      ui.toast('error', 'Save failed — your changes are kept', (e.kind === 'unavailable' ? 'Backend unavailable. ' : '') + window.AV.connection.clean(e.message) + ' You can retry.');
    } finally {
      saving = false;
      if (root && root.isConnected) { ui.setBusy(btn, false); const t = root.querySelector('#cf-test'); if (t) t.disabled = false; }
    }
  }

  function snapshot() { return JSON.stringify(m) + '|' + (password ? 'pw' : ''); }
  function isDirty() { return !!m && !!dirtyBaseline && snapshot() !== dirtyBaseline; }

  async function canLeave() {
    if (!isDirty() || saving) return !saving;
    return ui.confirm({ title: 'Discard unsaved changes?', message: 'You have unsaved changes to this camera. Leaving now will discard them.', confirmText: 'Discard changes', cancelText: 'Keep editing', danger: true });
  }

  // ======================= Detection Area: camera being monitored =======================
  /** IP + live state of the camera this detection area belongs to (real backend data only). */
  function renderDaCam() {
    const el = root && root.querySelector('#da-cam');
    if (!el || !m) return;
    const saved = !isNew ? state.getCamera(originalId) : null;
    const edited = saved && (m.sourceUrl !== saved.sourceUrl || m.type !== saved.type);
    const addr = ui.camAddress({ type: m.type, sourceUrl: m.sourceUrl });
    const live = saved && !edited ? ui.camLive(saved)
      : { cls: 'badge-muted', dot: 'dot-muted', label: isNew ? 'Not monitored yet — save the camera' : 'Changed address not saved yet' };
    const addrHtml = !addr ? '<span class="dim">' + (String(m.sourceUrl || '').trim() ? 'Address not recognised' : 'No address entered') + '</span>'
      : addr.local ? '<span class="dim">' + esc(addr.local) + ' (no IP)</span>'
      : '<b class="cam-ip-host">' + esc(addr.host) + '</b>' + (addr.port ? '<span class="cam-ip-port">:' + esc(addr.port) + '</span>' : '');
    const html = '<span class="da-cam-k">' + icon('monitor', 14) + 'Monitored camera</span>' +
      '<span class="da-cam-name">' + esc((saved && saved.name) || m.name || m.id || 'New camera') + '</span>' +
      '<span class="cam-ip"><span class="cam-ip-addr"><span class="cam-ip-k">' + (addr && !addr.local && !addr.isIp ? 'Host' : 'IP') + '</span>' + addrHtml + '</span>' +
      '<span class="badge ' + live.cls + ' cam-ip-state"><span class="dot ' + live.dot + '"></span>' + esc(live.label) + '</span></span>';
    if (el.innerHTML !== html) el.innerHTML = html;
  }

  function destroy() {
    daUnsub.forEach(function (f) { f(); }); daUnsub = [];
    if (editor) editor.destroy();
    if (streamImg) streamImg.removeAttribute('src');
    window.removeEventListener('beforeunload', unbeforeunload);
    editor = null; streamImg = null; root = null; m = null; password = ''; dirtyBaseline = null;
  }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.cameraForm = { title: 'Add Camera', render: render, destroy: destroy, canLeave: canLeave, _validateSource: validateSource };
})();
