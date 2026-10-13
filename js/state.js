/* PYROTECH VISION — application state + data repository.
 *
 * Data sources, in priority order:
 *   1. Backend (when /system/health reports OK): cameras, alerts, snapshots, detections, modules, metrics.
 *   2. Local configuration (browser storage): cameras the operator saved while no backend was connected.
 *      Metadata only — never passwords. Clearly labelled "Local only" in the UI.
 * There is no demonstration / sample data: records that older versions stored with demo:true are
 * removed from browser storage when the app loads.
 */
(function () {
  'use strict';
  const S = window.AV.storage;
  const api = window.AV.api;
  const cat = window.AV.catalog;

  const DEFAULT_PREFS = {
    theme: 'light', motion: 'system', apiBase: '', profileName: 'Admin User', profileRole: 'Administrator',
    dashGrid: 3, dashFilter: 'all', camerasView: 'grid', snapshotsView: 'grid', liveLayout: 4, liveSelection: [],
    navDetectionsOpen: true, showOverlays: true
  };

  let db = null;                 // persisted local data
  const remote = {               // backend data (memory only)
    cameras: null, alerts: null, snapshots: null, detections: null, modules: null, metrics: null, storage: null,
    errors: {}
  };
  const listeners = {};
  let lastEventsVersion = null;     // /events/version token of the alert / detection / snapshot lists held

  function load() {
    const stored = S.read();
    if (stored && stored.version === 1) {
      db = stored;
      db.prefs = Object.assign({}, DEFAULT_PREFS, db.prefs || {});
      db.globalModules = Object.assign(defaultGlobalModules(), db.globalModules || {});   // adds modules introduced later (e.g. Human Count)
    } else {
      db = { version: 1, prefs: Object.assign({}, DEFAULT_PREFS), globalModules: defaultGlobalModules() };
    }
    // Remove demonstration records saved by earlier versions; never create new ones.
    ['cameras', 'detections', 'alerts', 'snapshots'].forEach(function (k) {
      db[k] = (Array.isArray(db[k]) ? db[k] : []).filter(function (r) { return r && !r.demo; });
    });
    delete db.prefs.demoMode; delete db.seededAt;
    // The application always opens in light mode; dark mode can be switched on for the session.
    db.prefs.theme = 'light';
    persist();
  }

  function defaultGlobalModules() {
    const g = {};
    cat.MODULES.forEach(function (m) { g[m.id] = { enabled: m.id !== 'custom' && m.id !== 'vehicle' }; });
    return g;
  }

  function persist() { S.write(db); }

  function on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); return function () { off(evt, fn); }; }
  function off(evt, fn) { listeners[evt] = (listeners[evt] || []).filter(function (f) { return f !== fn; }); }
  function emit(evt, payload) { (listeners[evt] || []).slice().forEach(function (fn) { try { fn(payload); } catch (e) { console.error('[AI VISION] listener error:', e); } }); }

  // ---------- prefs ----------
  function prefs() { return db ? db.prefs : DEFAULT_PREFS; }
  function setPref(key, value) { db.prefs[key] = value; persist(); emit('prefs', { key: key, value: value }); }

  // ---------- helpers ----------
  function visible(list) { return (list || []).filter(function (r) { return !r.demo; }); }
  function byTimeDesc(a, b) { return String(b.timestamp).localeCompare(String(a.timestamp)); }

  function normalizeCamera(c, source) {
    const base = cat.newCamera();
    const out = Object.assign(base, c || {});
    out.detection = Object.assign(cat.defaultDetection(), c && c.detection || {});
    out.detection.modules = Object.assign(cat.defaultDetection().modules, c && c.detection && c.detection.modules || {});
    out.recording = Object.assign(cat.defaultRecording(), c && c.recording || {});
    out.zones = Array.isArray(out.zones) ? out.zones : [];
    out.source = source || out.source || 'local';
    delete out.password;
    return out;
  }

  // ---------- cameras ----------
  function cameras() {
    const list = [];
    if (remote.cameras) remote.cameras.forEach(function (c) { list.push(normalizeCamera(c, 'backend')); });
    db.cameras.forEach(function (c) {
      if (c.demo) return;
      if (list.some(function (x) { return x.id === c.id; })) return;
      list.push(normalizeCamera(c, 'local'));
    });
    return list;
  }
  function getCamera(id) { return cameras().find(function (c) { return c.id === id; }) || null; }
  function cameraName(id) { const c = getCamera(id); return c ? c.name : id; }

  function nextCameraId() {
    const all = cameras().concat(db.cameras);
    let n = 1;
    all.forEach(function (c) { const m = /^CAM-(\d+)$/.exec(c.id || ''); if (m) n = Math.max(n, parseInt(m[1], 10) + 1); });
    return 'CAM-' + String(n).padStart(2, '0');
  }

  /** Build the payload sent to the backend. Password only travels to the backend, never storage. */
  function toPayload(cam, password) {
    const p = JSON.parse(JSON.stringify(cam));
    delete p.source; delete p.image; delete p.demo;
    if (password) p.password = password;
    return p;
  }

  /**
   * Save camera. Returns { camera, persistedTo: 'backend'|'local', credentialsPersisted: bool }.
   * Throws ApiError if the backend is connected but rejects the save.
   */
  async function saveCamera(cam, opts) {
    opts = opts || {};
    const isNew = !opts.originalId;
    const now = new Date().toISOString();
    cam.updatedAt = now;
    if (isNew) cam.createdAt = now;

    if (api.status.connected) {
      const payload = toPayload(cam, opts.password);
      const saved = isNew ? await api.cameras.create(payload) : await api.cameras.update(opts.originalId, payload);
      await refreshFromBackend(['cameras']);
      return { camera: normalizeCamera(saved || cam, 'backend'), persistedTo: 'backend', credentialsPersisted: !!opts.password };
    }

    // Local configuration (metadata only) while no backend is connected.
    const local = normalizeCamera(cam, 'local');
    delete local.source;
    const idx = db.cameras.findIndex(function (c) { return c.id === (opts.originalId || cam.id); });
    if (idx >= 0) db.cameras[idx] = local; else db.cameras.push(local);
    persist();
    emit('cameras');
    return { camera: normalizeCamera(local), persistedTo: 'local', credentialsPersisted: false };
  }

  async function deleteCamera(id) {
    const cam = getCamera(id);
    if (!cam) throw new Error('Camera not found.');
    if (cam.source === 'backend') {
      await api.cameras.remove(id);
      await refreshFromBackend(['cameras']);
    } else {
      db.cameras = db.cameras.filter(function (c) { return c.id !== id; });
      persist();
    }
    emit('cameras');
  }

  async function setCameraEnabled(id, enabled) {
    const cam = getCamera(id);
    if (!cam) return;
    cam.enabled = enabled;
    if (cam.source === 'backend') {
      await api.cameras.update(id, toPayload(cam));
      await refreshFromBackend(['cameras']);
    } else {
      const c = db.cameras.find(function (x) { return x.id === id; });
      if (c) { c.enabled = enabled; persist(); }
    }
    emit('cameras');
  }

  /** Record a REAL connection test result reported by the backend. */
  function recordConnectionResult(id, result) {
    const c = db.cameras.find(function (x) { return x.id === id; });
    const target = c || (remote.cameras || []).find(function (x) { return x.id === id; });
    if (!target) return;
    if (result.ok) { target.status = 'online'; target.lastConnectedAt = new Date().toISOString(); target.lastError = null; }
    else { target.status = 'error'; target.lastError = result.message || result.code || 'Connection failed'; }
    if (c) persist();
    emit('cameras');
  }

  // ---------- detections / alerts / snapshots ----------
  function detections() { return (remote.detections || []).concat(visible(db.detections)).sort(byTimeDesc); }
  function alerts() { return (remote.alerts || []).concat(visible(db.alerts)).sort(byTimeDesc); }
  function snapshots() { return (remote.snapshots || []).concat(visible(db.snapshots)).sort(byTimeDesc); }
  function unackCount() { return alerts().filter(function (a) { return a.status !== 'acknowledged'; }).length; }

  /** Acknowledge an alert (backend alerts use the backend). */
  async function acknowledgeAlert(id, note) {
    const remoteAlert = (remote.alerts || []).find(function (a) { return a.id === id; });
    if (remoteAlert) {
      await api.alerts.acknowledge(id, note);
      await refreshFromBackend(['alerts']);
    } else {
      const a = db.alerts.find(function (x) { return x.id === id; });
      if (!a) throw new Error('Alert not found.');
      a.status = 'acknowledged';
      a.acknowledgedAt = new Date().toISOString();
      a.acknowledgedBy = db.prefs.profileName;
      if (note) a.notes.push({ text: note, at: a.acknowledgedAt, by: db.prefs.profileName });
      persist();
    }
    emit('alerts');
  }

  function addAlertNote(id, text) {
    const a = db.alerts.find(function (x) { return x.id === id; });
    if (!a) throw new Error('Notes on backend alerts require backend note support.');
    a.notes.push({ text: text, at: new Date().toISOString(), by: db.prefs.profileName });
    persist();
    emit('alerts');
  }

  async function deleteSnapshot(id) {
    const r = (remote.snapshots || []).find(function (s) { return s.id === id; });
    if (r) { await api.snapshots.remove(id); await refreshFromBackend(['snapshots']); }
    else { db.snapshots = db.snapshots.filter(function (s) { return s.id !== id; }); persist(); }
    emit('snapshots');
  }

  /** Called after the backend confirms a saved snapshot. */
  function addSnapshotFromBackend(snap) {
    remote.snapshots = remote.snapshots || [];
    remote.snapshots.unshift(fromBackend(snap));
    emit('snapshots');
  }

  // ---------- modules ----------
  function globalModules() { return db.globalModules; }
  function setGlobalModule(id, enabled) { db.globalModules[id] = Object.assign({}, db.globalModules[id], { enabled: enabled }); persist(); emit('modules'); }
  /** Backend model registry entry for a module, or null when unknown. */
  function moduleRuntime(id) {
    if (!remote.modules) return null;
    return remote.modules.find(function (m) { return m.id === id; }) || null;
  }

  // ---------- backend sync ----------
  async function refreshFromBackend(which) {
    if (!api.status.connected) {
      ['cameras', 'alerts', 'snapshots', 'detections', 'modules', 'metrics', 'storage'].forEach(function (k) { remote[k] = null; });
      return;
    }
    const tasks = {
      cameras: function () { return api.cameras.list(); },
      alerts: function () { return api.alerts.list({ limit: 5000 }); },
      snapshots: function () { return api.snapshots.list({ limit: 5000 }); },
      detections: function () { return api.detections.list({ limit: 5000 }); },
      modules: function () { return api.modules.list(); },
      storage: function () { return api.system.storage(); }
    };
    let keys = which || Object.keys(tasks);
    let evVersion = null;
    if (!which) {   // periodic full refresh: skip the large event lists when nothing changed
      try { evVersion = (await api.system.eventsVersion()).version || null; } catch (e) { evVersion = null; }
      if (evVersion && evVersion === lastEventsVersion && remote.alerts && remote.detections && remote.snapshots) {
        keys = keys.filter(function (k) { return ['alerts', 'detections', 'snapshots'].indexOf(k) < 0; });
      }
    }
    const sigOf = function (k) {
      const v = remote[k];
      if (v === null || v === undefined) return 'none';
      if (k === 'cameras') return camSig(v, CAM_STRUCT) + '#' + camSig(v, CAM_STATUS);
      if (Array.isArray(v)) return v.length + '|' + (v[0] ? v[0].id + v[0].status : '') + '|' + v.filter(function (r) { return r.status === 'acknowledged'; }).length;
      return JSON.stringify(v);
    };
    const before = {}, hadData = {};
    keys.forEach(function (k) { before[k] = sigOf(k); hadData[k] = remote[k] !== null && remote[k] !== undefined; });
    const errBefore = JSON.stringify(remote.errors);
    const structBefore = camSig(remote.cameras, CAM_STRUCT);
    await Promise.all(keys.map(async function (k) {
      try {
        const data = await tasks[k]();
        remote[k] = Array.isArray(data) ? data.map(fromBackend)
          : (data && Array.isArray(data.items) ? data.items.map(fromBackend) : data);
        delete remote.errors[k];
      } catch (e) {
        remote[k] = null;
        remote.errors[k] = e.message;
      }
    }));
    if (!which && evVersion && ['alerts', 'detections', 'snapshots'].every(function (k) { return !remote.errors[k]; })) lastEventsVersion = evVersion;
    // Emit only what changed, so periodic refreshes do not re-render pages (and restart live streams)
    // when nothing is new. First load or a changed error state still re-renders everything.
    if (keys.some(function (k) { return !hadData[k]; }) || JSON.stringify(remote.errors) !== errBefore) { emit('backend'); return; }
    const EVT = { alerts: 'alerts', snapshots: 'snapshots', detections: 'detections', modules: 'modules', storage: 'snapshots' };
    const fired = {};
    keys.forEach(function (k) {
      if (sigOf(k) === before[k]) return;
      const evt = k === 'cameras' ? (camSig(remote.cameras, CAM_STRUCT) !== structBefore ? 'cameras' : 'camstatus') : EVT[k];
      if (evt && !fired[evt]) { fired[evt] = true; emit(evt); }
    });
  }

  function fromBackend(r) {
    const o = Object.assign({ source: 'backend' }, r);
    if (o.image) o.image = api.resolveUrl(o.image);
    if (o.url) o.url = api.resolveUrl(o.url);
    if (o.originalUrl) o.originalUrl = api.resolveUrl(o.originalUrl);   // Head Count: unannotated frame
    return o;
  }

  // ---------- live camera status (IP, status, active session) ----------
  function camSig(list, keys) { return (list || []).map(function (c) { return keys.map(function (k) { return String(c[k]); }).join('~'); }).join('|'); }
  const CAM_STRUCT = ['id', 'name', 'sourceUrl', 'type', 'enabled'];
  const CAM_STATUS = ['status', 'lastError', 'lastConnectedAt', 'hasCredentials'];
  function sessSig(m) { return m && m.sessions ? m.sessions.map(function (x) { return x.cameraId + ':' + x.state; }).sort().join('|') : 'x'; }

  /** Poll cameras + running sessions (every few seconds). Structural changes emit 'cameras'
   *  (pages re-render); status / session changes emit 'camstatus' (updated in place, so live
   *  streams are not restarted). */
  async function pollCameras() {
    if (!api.status.connected) return;
    const before = { s: camSig(remote.cameras, CAM_STRUCT), t: camSig(remote.cameras, CAM_STATUS), m: sessSig(remote.metrics) };
    try {
      const data = await api.cameras.list();
      remote.cameras = (Array.isArray(data) ? data : (data && data.items) || []).map(fromBackend);
      delete remote.errors.cameras;
    } catch (e) { remote.errors.cameras = e.message; }
    try { remote.metrics = await api.system.metrics(); delete remote.errors.metrics; }
    catch (e) { remote.errors.metrics = e.message; }
    if (camSig(remote.cameras, CAM_STRUCT) !== before.s) emit('cameras');
    else if (camSig(remote.cameras, CAM_STATUS) !== before.t || sessSig(remote.metrics) !== before.m) emit('camstatus');
    emit('metrics');
  }

  /** Running backend session for a camera ({cameraId, state, modules, ...}) or null. */
  function session(camId) {
    const m = remote.metrics;
    return m && m.sessions ? (m.sessions.find(function (x) { return x.cameraId === camId; }) || null) : null;
  }

  /** Lightweight poll for new events (alerts / detections / snapshots). Emits only what changed. */
  async function pollEvents() {
    if (!api.status.connected) return;
    // Download the alert / detection / snapshot lists only when the backend reports a change
    // (they are up to 5000 records each; re-sending them every 5 s loads a busy backend).
    let version = null;
    try {
      version = (await api.system.eventsVersion()).version || null;
      if (version && version === lastEventsVersion && remote.alerts && remote.detections && remote.snapshots) return;
    } catch (e) { version = null; }   // older backend without /events/version: full refresh as before
    let failed = false;
    const sig = function (list) { return list ? list.length + '|' + (list[0] && (list[0].id + list[0].status)) + '|' + list.filter(function (a) { return a.status === 'acknowledged'; }).length : 'x'; };
    const before = { alerts: sig(remote.alerts), detections: sig(remote.detections), snapshots: sig(remote.snapshots) };
    const keys = ['alerts', 'detections', 'snapshots'];
    await Promise.all(keys.map(async function (k) {
      try { const d = await api[k === 'detections' ? 'detections' : k].list({ limit: 5000 }); remote[k] = (Array.isArray(d) ? d : (d && d.items) || []).map(fromBackend); delete remote.errors[k]; }
      catch (e) { remote.errors[k] = e.message; failed = true; }
    }));
    lastEventsVersion = failed ? null : version;
    keys.forEach(function (k) { if (sig(remote[k]) !== before[k]) emit(k); });
  }

  async function refreshMetrics() {
    if (!api.status.connected) { remote.metrics = null; return null; }
    try { remote.metrics = await api.system.metrics(); delete remote.errors.metrics; }
    catch (e) { remote.metrics = null; remote.errors.metrics = e.message; }
    emit('metrics');
    return remote.metrics;
  }

  // ---------- maintenance ----------
  function clearAll() { S.clear(); load(); emit('all'); }
  function exportConfig() {
    return JSON.stringify({ exportedAt: new Date().toISOString(), app: 'AI VISION', note: 'Passwords are never exported.',
      cameras: db.cameras.map(function (c) { const x = Object.assign({}, c); delete x.password; return x; }),
      globalModules: db.globalModules, prefs: db.prefs }, null, 2);
  }
  function importConfig(text) {
    const data = JSON.parse(text);
    if (!data || !Array.isArray(data.cameras)) throw new Error('File does not contain an PYROTECH VISION camera export.');
    data.cameras.forEach(function (c) {
      if (!c.id || !c.name) return;
      const n = normalizeCamera(c, 'local'); delete n.source; n.demo = false; n.status = 'unverified';
      const i = db.cameras.findIndex(function (x) { return x.id === n.id; });
      if (i >= 0) db.cameras[i] = n; else db.cameras.push(n);
    });
    if (data.globalModules) db.globalModules = Object.assign(db.globalModules, data.globalModules);
    persist();
    emit('all');
    return data.cameras.length;
  }

  window.AV.state = {
    load: load, on: on, off: off, emit: emit, prefs: prefs, setPref: setPref,
    cameras: cameras, getCamera: getCamera, cameraName: cameraName, nextCameraId: nextCameraId,
    saveCamera: saveCamera, deleteCamera: deleteCamera, setCameraEnabled: setCameraEnabled, recordConnectionResult: recordConnectionResult,
    detections: detections, alerts: alerts, snapshots: snapshots, unackCount: unackCount,
    acknowledgeAlert: acknowledgeAlert, addAlertNote: addAlertNote, deleteSnapshot: deleteSnapshot, addSnapshotFromBackend: addSnapshotFromBackend,
    globalModules: globalModules, setGlobalModule: setGlobalModule, moduleRuntime: moduleRuntime,
    refreshFromBackend: refreshFromBackend, refreshMetrics: refreshMetrics, pollCameras: pollCameras, session: session, remote: remote, pollEvents: pollEvents, fromBackend: fromBackend,
    clearAll: clearAll, exportConfig: exportConfig, importConfig: importConfig,
    normalizeCamera: normalizeCamera
  };
})();
