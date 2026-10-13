/* PYROTECH VISION — backend API client (integration boundary).
 *
 * Every method here talks to the real backend. None of them fabricate success:
 * when the backend is unreachable they reject with ApiError(kind: 'unavailable').
 * Local configuration storage lives in state.js, not here.
 *
 * Proposed REST contract (adapt paths in ENDPOINTS to the real backend):
 *   GET/POST        /cameras                 GET/PUT/DELETE /cameras/:id
 *   POST            /cameras/test-connection (unsaved config)   POST /cameras/:id/test-connection
 *   GET             /cameras/:id/status      GET /cameras/:id/stream   POST /cameras/:id/snapshot
 *   PUT             /cameras/:id/detection-config
 *   GET             /snapshots   DELETE /snapshots/:id
 *   GET             /alerts      POST /alerts/:id/acknowledge
 *   GET             /detections  GET /detection-modules
 *   GET             /system/health   GET /system/metrics   GET /system/storage
 */
(function () {
  'use strict';
  const cfg = window.AV.config;

  const ENDPOINTS = {
    health: '/system/health',
    metrics: '/system/metrics',
    storage: '/system/storage',
    cameras: '/cameras',
    camera: function (id) { return '/cameras/' + encodeURIComponent(id); },
    testNew: '/cameras/test-connection',
    test: function (id) { return '/cameras/' + encodeURIComponent(id) + '/test-connection'; },
    status: function (id) { return '/cameras/' + encodeURIComponent(id) + '/status'; },
    stream: function (id) { return '/cameras/' + encodeURIComponent(id) + '/stream'; },
    snapshot: function (id) { return '/cameras/' + encodeURIComponent(id) + '/snapshot'; },
    detectionConfig: function (id) { return '/cameras/' + encodeURIComponent(id) + '/detection-config'; },
    snapshots: '/snapshots',
    snapshotItem: function (id) { return '/snapshots/' + encodeURIComponent(id); },
    alerts: '/alerts',
    alertAck: function (id) { return '/alerts/' + encodeURIComponent(id) + '/acknowledge'; },
    detections: '/detections',
    modules: '/detection-modules',
    // Human / Vehicle detection modules (existing person+vehicle pipeline)
    det: function (mod, action) { return '/detections/' + encodeURIComponent(mod) + '/' + action; }
  };

  class ApiError extends Error {
    constructor(kind, message, extra) {
      super(message);
      this.name = 'ApiError';
      this.kind = kind;            // 'unavailable' | 'timeout' | 'network' | 'http' | 'parse'
      Object.assign(this, extra || {});
    }
  }

  const status = {
    connected: false,     // true only after a successful /system/health
    checked: false,
    checking: false,
    checkedAt: null,
    error: null,
    info: null            // health payload from backend
  };

  function base() {
    let b = (window.AV.state && window.AV.state.prefs && window.AV.state.prefs().apiBase) || cfg.apiBase || '/api';
    return b.replace(/\/+$/, '');
  }

  function backendReachableInPrinciple() {
    // Relative API paths cannot work when the page is opened straight from disk.
    return !(location.protocol === 'file:' && !/^https?:\/\//i.test(base()));
  }

  async function request(method, path, body, opts) {
    opts = opts || {};
    if (!backendReachableInPrinciple()) {
      throw new ApiError('unavailable', 'Backend unavailable: page opened from file:// with a relative API URL.');
    }
    const ctrl = new AbortController();
    const timeout = opts.timeout || cfg.requestTimeoutMs;
    const timer = setTimeout(function () { ctrl.abort(); }, timeout);
    let res;
    try {
      res = await fetch(base() + path, {
        method: method,
        headers: body !== undefined ? { 'Content-Type': 'application/json', 'Accept': 'application/json' } : { 'Accept': 'application/json' },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: opts.signal || ctrl.signal,
        credentials: 'same-origin'
      });
    } catch (e) {
      clearTimeout(timer);
      if (e && e.name === 'AbortError') throw new ApiError('timeout', 'The backend did not respond within ' + Math.round(timeout / 1000) + ' s.');
      throw new ApiError('network', 'Could not reach the backend at ' + base() + '.');
    }
    clearTimeout(timer);
    let data = null;
    const text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    if (!res.ok) {
      // 404/501/503 from a server with no backend routes = backend not implemented.
      // A JSON error with error_code comes from the real backend (e.g. CAMERA_NOT_FOUND) - not "backend missing".
      const code = data && (data.error_code || data.code);
      const notImpl = !code && (res.status === 404 || res.status === 501 || res.status === 503);
      const msg = (data && (data.message || data.error)) || ('HTTP ' + res.status);
      throw new ApiError(notImpl ? 'unavailable' : 'http', msg, { status: res.status, code: code, data: data });
    }
    if (text && data === null) throw new ApiError('parse', 'Backend returned a non-JSON response.');
    return data;
  }

  async function checkHealth() {
    status.checking = true;
    try {
      const info = await request('GET', ENDPOINTS.health, undefined, { timeout: 5000 });
      status.connected = !!info && (info.status === 'ok' || info.ok === true || info.healthy === true);
      status.error = status.connected ? null : 'Health endpoint did not report an OK status.';
      status.info = info;
    } catch (e) {
      status.connected = false;
      status.error = e.message;
      status.info = null;
    }
    status.checking = false;
    status.checked = true;
    status.checkedAt = new Date().toISOString();
    return status;
  }

  function requireBackend(what) {
    if (!status.connected) {
      throw new ApiError('unavailable', (what || 'This action') + ' requires the camera backend.');
    }
  }

  const api = {
    ApiError: ApiError,
    ENDPOINTS: ENDPOINTS,
    status: status,
    base: base,
    request: request,
    checkHealth: checkHealth,

    cameras: {
      list: function () { requireBackend('Loading cameras'); return request('GET', ENDPOINTS.cameras); },
      get: function (id) { requireBackend('Loading the camera'); return request('GET', ENDPOINTS.camera(id)); },
      create: function (payload) { requireBackend('Saving the camera'); return request('POST', ENDPOINTS.cameras, payload); },
      update: function (id, payload) { requireBackend('Saving the camera'); return request('PUT', ENDPOINTS.camera(id), payload); },
      remove: function (id) { requireBackend('Deleting the camera'); return request('DELETE', ENDPOINTS.camera(id)); },
      /** Test an unsaved configuration (payload) or a saved camera (id). */
      testConnection: function (idOrPayload) {
        requireBackend('Connection testing');
        if (typeof idOrPayload === 'string') return request('POST', ENDPOINTS.test(idOrPayload), {}, { timeout: cfg.connectionTestTimeoutMs });
        return request('POST', ENDPOINTS.testNew, idOrPayload, { timeout: cfg.connectionTestTimeoutMs });
      },
      status: function (id) { requireBackend('Camera status'); return request('GET', ENDPOINTS.status(id)); },
      /** Expected: { transport: 'mjpeg'|'image'|'hls'|'webrtc', url, width, height, audio } */
      stream: function (id) { requireBackend('Live streaming'); return request('GET', ENDPOINTS.stream(id)); },
      /** Expected: { id, url, cameraId, timestamp, width, height } once the image is saved server-side. */
      snapshot: function (id) { requireBackend('Snapshot capture'); return request('POST', ENDPOINTS.snapshot(id), {}, { timeout: 15000 }); },
      saveDetectionConfig: function (id, cfgBody) { requireBackend('Saving detection configuration'); return request('PUT', ENDPOINTS.detectionConfig(id), cfgBody); }
    },
    snapshots: {
      list: function (q) { requireBackend('Loading snapshots'); return request('GET', ENDPOINTS.snapshots + toQuery(q)); },
      remove: function (id) { requireBackend('Deleting snapshots'); return request('DELETE', ENDPOINTS.snapshotItem(id)); }
    },
    alerts: {
      list: function (q) { requireBackend('Loading alerts'); return request('GET', ENDPOINTS.alerts + toQuery(q)); },
      acknowledge: function (id, note) { requireBackend('Acknowledging alerts'); return request('POST', ENDPOINTS.alertAck(id), { note: note || '' }); }
    },
    detections: {
      list: function (q) { requireBackend('Loading detections'); return request('GET', ENDPOINTS.detections + toQuery(q)); }
    },
    modules: {
      /** Expected: [{ id, name, loaded: bool, running: bool, classes: [..], models: [{id,name,classes}] }] */
      list: function () { requireBackend('Model registry'); return request('GET', ENDPOINTS.modules); }
    },
    /** Human / Vehicle detection control (module = 'human' | 'vehicle'). */
    detection: {
      status: function (mod, cameraId) { requireBackend('Detection status'); return request('GET', ENDPOINTS.det(mod, 'status') + toQuery({ cameraId: cameraId }), undefined, { timeout: 5000 }); },
      config: function (mod, cameraId) { requireBackend('Detection settings'); return request('GET', ENDPOINTS.det(mod, 'config') + toQuery({ cameraId: cameraId })); },
      saveConfig: function (mod, cameraId, cfgBody) { requireBackend('Detection settings'); return request('PUT', ENDPOINTS.det(mod, 'config'), { cameraId: cameraId, config: cfgBody }); },
      start: function (mod, cameraId) { requireBackend('Starting detection'); return request('POST', ENDPOINTS.det(mod, 'start'), { cameraId: cameraId }, { timeout: 75000 }); },
      stop: function (mod, cameraId) { requireBackend('Stopping detection'); return request('POST', ENDPOINTS.det(mod, 'stop'), { cameraId: cameraId }, { timeout: 15000 }); },
      pause: function (mod, cameraId, paused) { requireBackend('Pausing detection'); return request('POST', ENDPOINTS.det(mod, paused ? 'pause' : 'resume'), { cameraId: cameraId }); },
      snapshot: function (mod, cameraId) { requireBackend('Snapshot capture'); return request('POST', ENDPOINTS.det(mod, 'snapshot'), { cameraId: cameraId }, { timeout: 15000 }); },
      events: function (mod, cameraId, limit) { requireBackend('Detection events'); return request('GET', ENDPOINTS.det(mod, 'events') + toQuery({ cameraId: cameraId, limit: limit || 50 })); },
      history: function (cameraId, minutes) { requireBackend('Human Count history'); return request('GET', ENDPOINTS.det('count', 'history') + toQuery({ cameraId: cameraId, minutes: minutes || 60 })); },
      // Human Head Count (still frame): capture one frame and count the heads inside the detection area
      headCount: function (body) { requireBackend('Head count'); return request('POST', '/detections/count/head-count', body, { timeout: 120000 }); },
      headCounts: function (cameraId, limit) { requireBackend('Head count results'); return request('GET', '/detections/count/head-counts' + toQuery({ cameraId: cameraId, limit: limit || 12 }), undefined, { timeout: 8000 }); },
      headSettings: function () { requireBackend('Head count settings'); return request('GET', '/detections/count/head-settings', undefined, { timeout: 8000 }); },
      saveHeadSettings: function (s) { requireBackend('Head count settings'); return request('PUT', '/detections/count/head-settings', s, { timeout: 8000 }); },
      // Object Monitoring: marked objects on a running camera
      objects: function (cameraId) { requireBackend('Object Monitoring'); return request('GET', '/detections/object/objects' + toQuery({ cameraId: cameraId }), undefined, { timeout: 8000 }); },
      markObject: function (cameraId, box, name) { requireBackend('Object Monitoring'); return request('POST', '/detections/object/objects', { cameraId: cameraId, box: box, name: name || '' }, { timeout: 15000 }); },
      renameObject: function (cameraId, id, name) { requireBackend('Object Monitoring'); return request('PUT', '/detections/object/objects/' + encodeURIComponent(id), { cameraId: cameraId, name: name }, { timeout: 8000 }); },
      removeObject: function (cameraId, id) { requireBackend('Object Monitoring'); return request('DELETE', '/detections/object/objects/' + encodeURIComponent(id) + toQuery({ cameraId: cameraId }), undefined, { timeout: 8000 }); },
      objectAction: function (cameraId, action) { requireBackend('Object Monitoring'); return request('POST', '/detections/object/' + action, { cameraId: cameraId }, { timeout: 8000 }); },
      saveZones: function (cameraId, zones) { requireBackend('Detection area'); return request('PUT', '/cameras/' + encodeURIComponent(cameraId) + '/detection-config', { zones: zones }, { timeout: 15000 }); }
    },
    system: {
      metrics: function () { requireBackend('System metrics'); return request('GET', ENDPOINTS.metrics, undefined, { timeout: 4000 }); },
      storage: function () { requireBackend('Storage metrics'); return request('GET', ENDPOINTS.storage, undefined, { timeout: 4000 }); },
      eventsVersion: function () { requireBackend('Event changes'); return request('GET', '/events/version', undefined, { timeout: 8000 }); }
    }
  };

  /** Backend returns media URLs relative to the API base ("cameras/CAM-01/stream.mjpeg"). */
  function resolveUrl(u) {
    if (!u || typeof u !== 'string') return u;
    if (/^(https?:|data:|blob:)/i.test(u) || u.charAt(0) === '/') return u;
    return base() + '/' + u.replace(/^\.?\//, '');
  }
  api.resolveUrl = resolveUrl;

  function toQuery(q) {
    if (!q) return '';
    const s = Object.keys(q).filter(function (k) { return q[k] !== undefined && q[k] !== '' && q[k] !== null; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(q[k]); }).join('&');
    return s ? '?' + s : '';
  }

  window.AV.api = api;
})();
