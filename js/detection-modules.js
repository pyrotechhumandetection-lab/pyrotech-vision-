/* PYROTECH VISION — detection module configuration panels (shared by camera form and dashboard). */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, cat = window.AV.catalog, state = window.AV.state, api = window.AV.api;
  const esc = ui.esc;

  /** Human-readable runtime status. Never claims "running" unless the backend says so. */
  function runtimeStatus(modId) {
    if (!api.status.connected) return { tone: 'muted', text: 'Model status unknown — backend not connected', short: 'Not connected' };
    const rt = state.moduleRuntime(modId);
    if (!rt) return { tone: 'muted', text: 'Not reported by the backend model registry', short: 'Not reported' };
    if (rt.running) return { tone: 'success', text: 'Model running on at least one camera (reported by backend)', short: 'Running' };
    if (rt.loaded) return { tone: 'warning', text: 'Model available on the backend but not running', short: 'Available' };
    return { tone: 'danger', text: 'Model not loaded on the backend', short: 'Not loaded' };
  }
  function runtimeBadge(modId) {
    const s = runtimeStatus(modId);
    const cls = { success: 'badge-success', warning: 'badge-warning', danger: 'badge-danger', muted: 'badge-muted' }[s.tone];
    return '<span class="badge ' + cls + '" title="' + esc(s.text) + '">' + icon('cpu', 12) + esc(s.short) + '</span>';
  }

  /** null = unknown (no registry), true/false = backend-reported support. */
  function classSupported(modId, cls) {
    const rt = state.moduleRuntime(modId);
    if (!rt || !Array.isArray(rt.classes)) return null;
    return rt.classes.indexOf(cls) >= 0;
  }

  function confField(id, value) {
    const v = Number(value || 0.5);
    return '<div class="field"><label for="' + id + '">Confidence threshold</label><div class="range-row">' +
      '<input class="range" type="range" id="' + id + '" min="0.10" max="0.99" step="0.01" value="' + v.toFixed(2) + '" style="--pct:' + ((v - 0.1) / 0.89 * 100) + '%" data-range-out="' + id + '-out">' +
      '<output class="range-val" id="' + id + '-out" for="' + id + '">' + v.toFixed(2) + '</output></div><span class="field-hint">Detections below this score are discarded by the inference service (0.10 – 0.99).</span></div>';
  }
  function check(name, label, checked, disabledReason) {
    return '<label class="check" ' + (disabledReason ? 'title="' + esc(disabledReason) + '"' : '') + '><input type="checkbox" name="' + name + '" ' + (checked ? 'checked' : '') + (disabledReason ? ' disabled' : '') + '><span>' + esc(label) + (disabledReason ? ' <span class="dim">(not supported by model)</span>' : '') + '</span></label>';
  }
  function zonesField(cfg, zones) {
    if (!zones || !zones.length) return '<div class="field"><span class="field-label">Applicable detection zones</span><span class="field-hint">No zones drawn yet — the module will analyse the full frame. Draw zones in the Live Preview panel.</span></div>';
    return '<div class="field"><span class="field-label">Applicable detection zones</span><div class="row wrap" style="gap:14px">' +
      zones.map(function (z) {
        return '<label class="check"><input type="checkbox" name="zone" value="' + esc(z.id) + '" ' + ((cfg.zones || []).indexOf(z.id) >= 0 ? 'checked' : '') + '><span><span class="dot" style="color:' + esc(z.color) + '"></span> ' + esc(z.name) + '</span></label>';
      }).join('') + '</div><span class="field-hint">None selected = full frame.</span></div>';
  }
  function classUnknownNote(modId) {
    const rt = state.moduleRuntime(modId);
    if (rt && Array.isArray(rt.classes)) return '<div class="notice notice-success">' + icon('checkCircle', 16) + '<div>Classes supported by the loaded model: <b>' + esc(rt.classes.join(', ') || 'none') + '</b>. Unsupported classes are disabled.</div></div>';
    return '<div class="notice notice-warning">' + icon('alert', 16) + '<div>Class support has not been verified — no model registry is connected. Selected classes are stored as <b>requested</b> classes; the backend must reject any class its model cannot detect.</div></div>';
  }

  // AI frame sampling defaults of the backend (backend/config.py, AIV_* settings) - empty field = default
  const SCHED = {
    vehicle: { mode: 'adaptive', idleCheckSec: 10, idleAfterSec: 10, intervalSec: 2, captureFps: 16, imgSize: 640 },
    human: { mode: 'adaptive', idleCheckSec: 10, idleAfterSec: 10, intervalSec: 4, captureFps: 6, imgSize: 640, motionGate: true, fallbackSec: 30 },
    count: { mode: 'adaptive', idleCheckSec: 10, idleAfterSec: 10, intervalSec: 4, captureFps: 6, imgSize: 640 },
    object: { mode: 'adaptive', idleCheckSec: 10, idleAfterSec: 10, motionGate: true, fallbackSec: 30 }
  };
  function num(name, label, val, dflt, hint, step) {
    return '<div class="field"><label>' + esc(label) + '</label><input class="input" type="number" name="sch-' + name + '" step="' + (step || 'any') + '" min="0" value="' + (val === undefined || val === null ? '' : esc(val)) + '" placeholder="default ' + esc(dflt) + '"><span class="field-hint">' + hint + '</span></div>';
  }
  function scheduleHtml(modId, cfg) {
    const d = SCHED[modId];
    if (!d) return '';
    const s = cfg.schedule || {};
    const mode = s.mode || d.mode;
    let h = '<div class="field"><span class="field-label">AI frame sampling</span>' +
      '<select class="select" name="sch-mode" style="margin-bottom:8px">' +
      [['adaptive', 'Adaptive (recommended): every frame while something happens, idle when the scene is still'], ['interval', 'Fixed interval: one analysed frame every N seconds'], ['every', 'Every frame, always (highest CPU)']]
        .map(function (o) { return '<option value="' + o[0] + '"' + (mode === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>' +
      '<div class="grid-2" style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px">';
    h += num('idleCheckSec', 'Adaptive: AI check while idle every (s)', s.idleCheckSec, d.idleCheckSec + ' s', 'Safety check when nothing moves.');
    h += num('idleAfterSec', 'Adaptive: idle after (s) without motion', s.idleAfterSec, d.idleAfterSec + ' s', 'Motion inside this module\'s zones, an unconfirmed object' + (modId === 'count' ? ' or people in view' : '') + ' keeps it active.');
    if ('intervalSec' in d) h += num('intervalSec', 'Analyse one frame every (s)', s.intervalSec, d.intervalSec + ' s', '0 = every frame (tracking confirmation). With an interval, detections are confirmed per analysed frame.');
    if ('captureFps' in d) h += num('captureFps', 'Frames processed / shown per second', s.captureFps, d.captureFps + ' fps', 'Frames above this are skipped after decoding (set the camera\'s own fps to save decoding too).');
    if ('imgSize' in d) h += num('imgSize', 'Model input size (px)', s.imgSize, d.imgSize, 'Multiple of 32. Smaller = faster, but small / distant objects are missed more often.', 32);
    if ('fallbackSec' in d) h += num('fallbackSec', 'Check without motion every (s)', s.fallbackSec, d.fallbackSec + ' s', 'AI check even when nothing moves (motion is a trigger, not a detector).');
    h += '</div>';
    if ('motionGate' in d) h += check('sch-motionGate', 'Motion-triggered analysis (skip AI while the picture does not change)', s.motionGate === undefined ? d.motionGate : !!s.motionGate);
    return h + '</div>';
  }

  /** Settings form body for one module. */
  function settingsHtml(modId, cfg, zones) {
    const m = cat.moduleById(modId);
    const p = 'ms-' + modId;
    let body = '<div class="row wrap" style="gap:8px;margin-bottom:12px">' + runtimeBadge(modId) + '</div><div class="stack">';
    body += '<label class="check"><input type="checkbox" name="enabled" ' + (cfg.enabled ? 'checked' : '') + '><span><b>Enable ' + esc(m.long) + ' for this camera</b></span></label>';
    body += confField(p + '-conf', cfg.confidence);
    if (modId === 'helmet') {
      const sNo = classSupported('helmet', 'no_helmet');
      body += '<div class="field"><span class="field-label">Detect</span><div class="row wrap" style="gap:16px">' +
        check('detectHelmet', 'Construction helmets', cfg.detectHelmet) +
        check('detectMissing', 'Missing helmets (person without helmet)', cfg.detectMissing && sNo !== false, sNo === false ? 'The loaded model has no no_helmet class' : '') + '</div></div>';
      body += check('alertOnMissing', 'Raise an alert when a missing helmet is confirmed', cfg.alertOnMissing);
      body += classUnknownNote('helmet');
    } else if (modId === 'ppe') {
      body += '<div class="field"><span class="field-label">Required PPE items</span><div class="row wrap" style="gap:16px">' +
        ['helmet', 'vest', 'gloves', 'boots'].map(function (c) {
          const sup = classSupported('ppe', c);
          const label = { helmet: 'Helmet', vest: 'Safety vest', gloves: 'Gloves', boots: 'Safety boots' }[c];
          return check('cls-' + c, label, cfg.classes && cfg.classes[c] && sup !== false, sup === false ? 'The loaded PPE model has no ' + c + ' class' : '');
        }).join('') + '</div></div>';
      body += '<div class="field"><label for="' + p + '-rule">Compliance rule</label><select class="select" id="' + p + '-rule" name="rule">' +
        '<option value="all"' + (cfg.rule === 'all' ? ' selected' : '') + '>Every required item must be present</option>' +
        '<option value="any"' + (cfg.rule === 'any' ? ' selected' : '') + '>Flag only when all required items are missing</option></select></div>';
      body += check('alertOnMissing', 'Raise an alert when required PPE is missing', cfg.alertOnMissing);
      body += classUnknownNote('ppe');
    } else if (modId === 'fire') {
      const fc = cfg.classes || { fire: cfg.fire !== false, smoke: cfg.smoke !== false };
      const fsev = (cfg.alert && cfg.alert.severity) || cfg.severity || 'critical';
      body += '<div class="field"><span class="field-label">Detect (fire &amp; smoke model firedetect-11s)</span><div class="row wrap" style="gap:16px">' + check('cls-fire', 'Fire / flame', fc.fire !== false) + check('cls-smoke', 'Smoke', fc.smoke !== false) + '</div></div>';
      body += '<div class="field"><label for="' + p + '-sev">Fire alert severity</label><select class="select" id="' + p + '-sev" name="severity">' +
        ['critical', 'high', 'medium', 'low'].map(function (s) { return '<option value="' + s + '"' + (fsev === s ? ' selected' : '') + '>' + ui.cap(s) + '</option>'; }).join('') + '</select></div>';
      body += '<p class="field-hint">The slider above is the fallback confidence; fire (0.30) and smoke (0.65) have their own thresholds. Smoke rules, the smoke verifier and the alert window are on the Fire &amp; Smoke Detection page.</p>';
      body += '<div class="notice notice-danger">' + icon('alert', 16) + '<div>Video fire/smoke analytics are a supplementary aid. They do not replace a certified fire detection and alarm system.</div></div>';
    } else if (modId === 'human') {
      const hc = cfg.classes || {};
      body += '<div class="field"><span class="field-label">Classes (existing YOLOv8 + ByteTrack pipeline)</span><div class="row wrap" style="gap:16px">' +
        check('cls-person', 'Person', hc.person !== false) + check('cls-rider', 'Rider (person on a 2-wheeler)', hc.rider !== false) +
        check('cls-personFar', 'Distant people (extra upscaled pass - slower)', !!hc.personFar) + '</div></div>';
      body += '<p class="field-hint">Counting of confirmed unique tracks is built into the pipeline. For live control, snapshots and alerts use the Human Detection page.</p>';
    } else if (modId === 'count') {
      body += '<p class="field-hint">People counting AI (person detector + people tracker). Each person is counted once while visible. Live counts, statistics, counting settings and occupancy alerts are on the Human Count page.</p>';
    } else if (modId === 'vehicle') {
      const vc = cfg.classes || {};
      body += '<div class="field"><span class="field-label">Vehicle classes (existing YOLOv8 + ByteTrack pipeline)</span><div class="row wrap" style="gap:16px">' +
        [['twoWheeler', '2-Wheeler (bicycle, motorcycle)'], ['fourWheeler', '4-Wheeler (car)'], ['bus', 'Bus'], ['truck', 'Truck']].map(function (c) { return check('cls-' + c[0], c[1], vc[c[0]] !== false); }).join('') + '</div></div>';
      body += '<p class="field-hint">Auto-rickshaws have no COCO class; the model usually reports them as 2- or 4-wheelers. Per-class thresholds, snapshots and alerts are on the Vehicle Detection page.</p>';
    } else if (modId === 'object') {
      body += '<p class="field-hint">Marked-object monitoring: draw a box around any object on the live picture of the <a href="#/detections/object">Object Monitoring</a> page; an alert is raised when it is <b>moved</b> or goes <b>missing</b>. It works from the object\'s appearance, so no class has to be chosen here. Detection zones are not used by this module.</p>';
    } else if (modId === 'custom') {
      const rt = state.moduleRuntime('custom');
      const models = rt && Array.isArray(rt.models) ? rt.models : [];
      if (!models.length) {
        body += ui.emptyState('model', 'No model registry available', api.status.connected ? 'The backend did not report any custom models. Register a model on the inference server, then reopen this panel.' : 'Custom models are listed from the backend model registry (GET /detection-modules). Connect the backend to select a model.');
        body += '<input type="hidden" name="modelId" value="' + esc(cfg.modelId || '') + '">';
      } else {
        body += '<div class="field"><label for="' + p + '-model">Model <span class="req">*</span></label><select class="select" id="' + p + '-model" name="modelId"><option value="">Select a model…</option>' +
          models.map(function (md) { return '<option value="' + esc(md.id) + '"' + (cfg.modelId === md.id ? ' selected' : '') + '>' + esc(md.name || md.id) + '</option>'; }).join('') + '</select><span class="field-error" data-err="modelId"></span></div>';
        const sel = models.find(function (md) { return md.id === cfg.modelId; });
        body += '<div class="field-hint" id="' + p + '-classes">Supported classes: ' + esc(sel && sel.classes ? sel.classes.join(', ') : '—') + '</div>';
      }
    }
    body += scheduleHtml(modId, cfg);
    if (modId !== 'object') body += zonesField(cfg, zones);
    return body + '</div>';
  }

  /** Read form values back into a module config (does not mutate input). */
  function readSchedule(root, modId, cfg) {
    if (!SCHED[modId]) return;
    const sch = Object.assign({}, cfg.schedule || {});
    root.querySelectorAll('input[name^="sch-"], select[name^="sch-"]').forEach(function (inp) {
      const k = inp.name.slice(4);
      if (inp.type === 'checkbox') sch[k] = inp.checked;
      else if (k === 'mode') sch[k] = inp.value;
      else if (inp.value.trim() === '') delete sch[k];            // empty = backend default
      else sch[k] = k === 'imgSize' ? parseInt(inp.value, 10) : parseFloat(inp.value);
    });
    if (Object.keys(sch).length) cfg.schedule = sch; else delete cfg.schedule;
  }
  function readSettings(root, modId, prev) {
    const cfg = JSON.parse(JSON.stringify(prev || {}));
    const q = function (n) { return root.querySelector('[name="' + n + '"]'); };
    readSchedule(root, modId, cfg);
    cfg.enabled = q('enabled').checked;
    const range = root.querySelector('.range');
    cfg.confidence = Math.round(parseFloat(range.value) * 100) / 100;
    ['detectHelmet', 'detectMissing', 'alertOnMissing', 'fire', 'smoke', 'counting', 'regionMonitoring'].forEach(function (k) { if (q(k)) cfg[k] = q(k).checked && !q(k).disabled; });
    if (modId === 'fire') {
      cfg.classes = { fire: !!(q('cls-fire') && q('cls-fire').checked), smoke: !!(q('cls-smoke') && q('cls-smoke').checked) };
      cfg.alert = Object.assign({}, typeof cfg.alert === 'object' && cfg.alert ? cfg.alert : {}, { severity: q('severity').value });
      delete cfg.fire; delete cfg.smoke; delete cfg.severity;
      cfg.zones = Array.prototype.map.call(root.querySelectorAll('[name="zone"]:checked'), function (c) { return c.value; });
      return cfg;
    }
    if (modId === 'object') {
      ['target', 'missingAfterSec'].forEach(function (k) { delete cfg[k]; });
      if (typeof cfg.alert !== 'object') delete cfg.alert;
      cfg.zones = [];
      return cfg;
    }
    if (modId === 'ppe' || modId === 'vehicle' || modId === 'human') {
      cfg.classes = cfg.classes || {};
      root.querySelectorAll('[name^="cls-"]').forEach(function (c) { cfg.classes[c.name.slice(4)] = c.checked && !c.disabled; });
    }
    ['rule', 'severity', 'target', 'alert', 'modelId'].forEach(function (k) { if (q(k)) cfg[k] = q(k).value.trim(); });
    if (q('missingAfterSec')) cfg.missingAfterSec = parseInt(q('missingAfterSec').value, 10);
    cfg.zones = Array.prototype.map.call(root.querySelectorAll('[name="zone"]:checked'), function (c) { return c.value; });
    return cfg;
  }

  /** Validate a module config. Returns { field: message } (empty = valid). */
  function validateSettings(modId, cfg) {
    const e = {};
    if (!(cfg.confidence >= 0.1 && cfg.confidence <= 0.99)) e.confidence = 'Confidence must be between 0.10 and 0.99.';
    if (!cfg.enabled) return e;
    if (modId === 'helmet' && !cfg.detectHelmet && !cfg.detectMissing) e.general = 'Select at least one helmet class to detect.';
    if (modId === 'ppe' && !Object.keys(cfg.classes || {}).some(function (k) { return cfg.classes[k]; })) e.general = 'Select at least one required PPE item.';
    if (modId === 'fire' && !(cfg.classes ? (cfg.classes.fire || cfg.classes.smoke) : (cfg.fire || cfg.smoke))) e.general = 'Enable fire detection, smoke detection or both.';
    if (modId === 'vehicle' && !Object.keys(cfg.classes || {}).some(function (k) { return cfg.classes[k]; })) e.general = 'Select at least one vehicle class.';
    if (modId === 'human' && !(cfg.classes && (cfg.classes.person || cfg.classes.rider))) e.general = 'Select Person and/or Rider.';
    if (modId === 'custom' && !cfg.modelId) e.modelId = 'Select a model from the backend registry.';
    const sc = cfg.schedule || {};
    if (sc.intervalSec !== undefined && !(sc.intervalSec >= 0 && sc.intervalSec <= 600)) e.general = 'Analysis interval must be 0 - 600 s.';
    if (sc.captureFps !== undefined && !(sc.captureFps >= 0 && sc.captureFps <= 60)) e.general = 'Frames per second must be 0 - 60.';
    if (sc.imgSize !== undefined && !(sc.imgSize >= 160 && sc.imgSize <= 1920 && sc.imgSize % 32 === 0)) e.general = 'Model input size must be a multiple of 32 between 160 and 1920.';
    if (sc.fallbackSec !== undefined && !(sc.fallbackSec >= 1 && sc.fallbackSec <= 3600)) e.general = 'Fallback check must be 1 - 3600 s.';
    return e;
  }

  /** Wire range sliders inside root so the numeric output and fill follow the thumb. */
  function wireRanges(root) {
    root.querySelectorAll('input.range').forEach(function (r) {
      const upd = function () {
        const min = parseFloat(r.min), max = parseFloat(r.max), v = parseFloat(r.value);
        r.style.setProperty('--pct', ((v - min) / (max - min) * 100) + '%');
        const out = r.dataset.rangeOut && document.getElementById(r.dataset.rangeOut);
        if (out) out.textContent = r.dataset.format === 'sens' ? ['Low', 'Medium', 'High'][v] : v.toFixed(2);
      };
      r.addEventListener('input', upd); upd();
    });
  }

  /** Open a modal to edit one module's settings for a camera config. Resolves with new cfg or null. */
  function openSettings(modId, cfg, zones) {
    const m = cat.moduleById(modId);
    return new Promise(function (resolve) {
      const md = ui.modal({
        title: m.long + ' — settings', icon: m.icon, size: 'lg',
        body: '<form class="module-settings" novalidate>' + settingsHtml(modId, cfg, zones) + '<div class="field-error mt-12" data-err="general"></div></form>',
        actions: [
          { label: 'Cancel', cls: 'btn-ghost' },
          { label: 'Apply settings', cls: 'btn-primary', icon: 'check', onClick: function (api) {
            const form = api.body.querySelector('form');
            const next = readSettings(form, modId, cfg);
            const errs = validateSettings(modId, next);
            form.querySelectorAll('[data-err]').forEach(function (el) { el.textContent = errs[el.dataset.err] || ''; });
            if (Object.keys(errs).length) return false;
            resolve(next);
          } }
        ],
        onClose: function () { resolve(null); }
      });
      wireRanges(md.body);
      const modelSel = md.body.querySelector('[name="modelId"]');
      if (modelSel && modelSel.tagName === 'SELECT') modelSel.addEventListener('change', function () {
        const rt = state.moduleRuntime('custom'); const sel = rt && rt.models.find(function (x) { return x.id === modelSel.value; });
        const out = md.body.querySelector('#ms-custom-classes'); if (out) out.textContent = 'Supported classes: ' + (sel && sel.classes ? sel.classes.join(', ') : '—');
      });
    });
  }

  window.AV.modules = { runtimeStatus: runtimeStatus, runtimeBadge: runtimeBadge, settingsHtml: settingsHtml, readSettings: readSettings, validateSettings: validateSettings, openSettings: openSettings, wireRanges: wireRanges };
})();
