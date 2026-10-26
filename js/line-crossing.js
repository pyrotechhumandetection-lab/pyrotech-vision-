/* PYROTECH VISION — line-crossing modules (Two-Wheeler Helmet at a line, Person Line Crossing & Counting).
 * UI parts used by detection-live.js: module definitions, the line editor drawn on the live picture, the counts /
 * statistics panel and the settings forms. Everything shown comes from the backend (backend/detection/crossing);
 * nothing is simulated. The line is stored on the backend per camera in NORMALISED coordinates (0-1) together with the
 * resolution it was drawn on. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, esc = ui.esc;

  const MOD = {
    twhelmet: { id: 'twhelmet', title: 'Two-Wheeler Helmet (Line)', icon: 'helmet', noun: 'two-wheeler crossing', cross: true,
      sub: 'Draw a line on the live picture · motorcycle / scooter tracked and its rider(s) checked for a helmet when it crosses · separate compliant and helmet-missing alerts · pedestrians and cars are ignored',
      classes: [] },
    pcross: { id: 'pcross', title: 'Person Line Crossing & Counting', icon: 'users', noun: 'crossing', cross: true,
      sub: 'Draw a line on the live picture · people tracked and counted once per crossing, IN and OUT · live counts, timestamps and per-camera statistics',
      classes: [] }
  };
  const VERDICT = { HELMET: ['badge-success', 'Helmet'], MISSING: ['badge-danger', 'No helmet'], UNCERTAIN: ['badge-warning', 'Not verified'] };
  const ALERT_TEXT = { 'Person IN': 'Every person counted IN', 'Person OUT': 'Every person counted OUT' };

  let draft = null, drawMode = false, unfit = null, ctx = null;

  const r4 = function (v) { return Math.round(v * 10000) / 10000; };
  const fmtT = function (iso) { const d = new Date(iso); return isNaN(d) ? '—' : d.toLocaleString(undefined, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false }); };
  const n = function (v, d) { return v == null ? '<span class="dim">—</span>' : esc(typeof v === 'number' && d != null ? v.toFixed(d) : v); };

  // ------------------------------------------------------------------ status rows
  function rows(s, modId) {
    const c = s.cross;
    if (!c) return '<dt>Line crossing</dt><dd><span class="dim">—</span></dd>';
    const m = c.metrics || {}, t = c.tracking || {};
    const line = c.line;
    return '<dt>Line</dt><dd>' + (line ? '<span class="badge badge-success">Drawn</span> <span class="dim mono">A (' + line.x1.toFixed(2) + ', ' + line.y1.toFixed(2) + ') → B (' + line.x2.toFixed(2) + ', ' + line.y2.toFixed(2) + ')' + (line.refWidth ? ' · drawn on ' + esc(line.refWidth) + '×' + esc(line.refHeight) : '') + '</span>'
        : '<span class="badge badge-warning">Not drawn</span> <span class="dim">use “Draw line” above the picture</span>') +
      (c.warnings || []).map(function (w) { return '<div class="dim" style="font-size:12px;color:var(--warning)">' + esc(w) + '</div>'; }).join('') + '</dd>' +
      '<dt>Direction counted</dt><dd>' + esc({ both: 'Both (IN and OUT)', in: 'IN only', out: 'OUT only' }[c.direction] || c.direction) + '</dd>' +
      '<dt>Tracking</dt><dd>' + esc(t.tracksStarted || 0) + ' tracks started · ' + esc(t.tracksLost || 0) + ' ended · ' + esc(t.tracksShort || 0) + ' too short to count · ' + esc(t.handovers || 0) + ' id hand-overs · ' + esc((t.suppressed || {}).duplicate || 0) + ' duplicates suppressed' + ((t.suppressed || {}).pathAroundLineEnd ? ' · <b>' + esc(t.suppressed.pathAroundLineEnd) + ' passed beyond a line end (not counted)</b>' : '') +
        (t.meanTrackFrames != null ? ' <span class="dim">· mean track ' + esc(t.meanTrackFrames) + ' frames</span>' : '') + '<div class="dim" style="font-size:11.5px">Measured while running; these are tracker statistics, not an accuracy figure.</div></dd>' +
      (m.lastError ? '<dt>Last error</dt><dd><span class="badge badge-danger">' + esc(m.errors) + '</span> <span class="dim">' + esc(m.lastError) + '</span></dd>' : '');
  }

  // ------------------------------------------------------------------ counts / statistics panel
  function tile(label, v, tone) { return '<div class="ppe-tile' + (tone ? ' ' + tone : '') + '"><b>' + (v == null ? '—' : esc(v)) + '</b><span>' + esc(label) + '</span></div>'; }

  function panel(modId, s, live) {
    const c = s && s.cross;
    const head = '<div class="card-head"><h2 id="dl-cross-h">' + icon(MOD[modId].icon, 18) + (modId === 'pcross' ? 'Live counts' : 'Helmet checks at the line') + '</h2><span class="spacer"></span>' +
      (c ? '<span class="dim" style="font-size:12px">since ' + esc(fmtT(c.since)) + (c.lastEventAt ? ' · last crossing ' + esc(ui.relTime(c.lastEventAt)) : '') + '</span>' : '') + '</div>';
    if (!c) return head + ui.emptyState(MOD[modId].icon, live ? 'Waiting for the first frame' : 'Not running', 'Start detection, then draw the line on the live picture. Counts start from zero at every start.');
    let body = '';
    if (modId === 'pcross') {
      const w = c.windows || {};
      body = '<div class="ppe-tiles">' + tile('IN', c.counts.in, 'ok') + tile('OUT', c.counts.out, 'warn') + tile('Net (IN − OUT)', c.net) + tile('In view now', (c.tracksNow || []).length) + '</div>' +
        '<div class="table-wrap mt-12"><table class="table ppe-table"><thead><tr><th>Window</th><th>IN</th><th>OUT</th></tr></thead><tbody>' +
        [['Last minute', w.last1Min], ['Last 5 minutes', w.last5Min], ['Last 60 minutes', w.last60Min], ['Since start', c.counts]].map(function (r) { return '<tr><td>' + r[0] + '</td><td>' + (r[1] ? r[1].in : '—') + '</td><td>' + (r[1] ? r[1].out : '—') + '</td></tr>'; }).join('') + '</tbody></table></div>';
    } else {
      const j = c.judge || {};
      body = '<div class="ppe-tiles">' + tile('Helmet compliant', c.counts.compliant, 'ok') + tile('Helmet missing', c.counts.missing, 'bad') + tile('Not verified', c.counts.unverified, 'warn') + tile('Two-wheelers crossed', c.crossings) + '</div>' +
        '<div class="notice ' + (j.custom ? 'notice-info' : 'notice-warning') + ' mt-12">' + icon('info', 16) + '<div><b>Helmet judge: ' + esc(j.model || 'not available') + '.</b> ' +
        (j.custom ? 'A rider-helmet model supplied by you is in use. Its accuracy has not been measured by this software — evaluate it on your own labelled footage (backend/tools/eval_twhelmet.py).'
          : 'The stock PPE detector was trained on construction helmets and is <b>not validated on motorcycle helmets</b>. It answers “helmet” or “no helmet” only on strong evidence over several frames; anything else is “not verified” and raises no compliant / missing alert.') + '</div></div>';
    }
    const rec = c.recent || [];
    body += '<h3 class="hc-sub-h" style="margin-top:14px">Recent crossings</h3>' + (rec.length
      ? '<div class="table-wrap"><table class="table ppe-table"><thead><tr><th>Time</th><th>' + (modId === 'pcross' ? 'Person' : 'Two-wheeler') + '</th><th>Direction</th>' + (modId === 'twhelmet' ? '<th>Helmet</th>' : '') + '<th>Frame</th></tr></thead><tbody>' +
        rec.slice(0, 15).map(function (e) {
          const v = VERDICT[e.verdict];
          return '<tr><td class="mono" style="font-size:12px">' + esc(fmtT(e.at)) + '</td><td>#' + esc(e.trackId) + '</td><td><span class="badge ' + (e.direction === 'in' ? 'badge-info' : 'badge-muted') + '">' + (e.direction === 'in' ? 'IN' : 'OUT') + '</span></td>' +
            (modId === 'twhelmet' ? '<td>' + (v ? '<span class="badge ' + v[0] + '">' + v[1] + '</span>' : '—') + '</td>' : '') + '<td class="mono dim" style="font-size:12px">' + esc(e.frameId) + '</td></tr>';
        }).join('') + '</tbody></table></div>'
      : '<p class="dim" style="font-size:12.5px">' + (live ? 'No crossing yet.' : 'Nothing recorded.') + '</p>');
    if (c.ignoredDirection) body += '<p class="dim" style="font-size:11.5px;margin-top:6px">' + esc(c.ignoredDirection) + ' crossing(s) in the other direction were ignored (direction setting).</p>';
    return head + body;
  }

  // ------------------------------------------------------------------ line editor
  function toolbarHtml(modId) {
    return '<div class="lc-bar" id="lc-bar" role="toolbar" aria-label="Virtual line">' +
      '<button class="btn btn-sm" id="lc-draw" aria-pressed="false">' + icon('edit', 15) + '<span>Draw line</span></button>' +
      '<button class="btn btn-sm" id="lc-flip" title="Swap A and B: IN and OUT trade places">' + icon('refresh', 15) + '<span>Flip IN / OUT</span></button>' +
      '<button class="btn btn-sm btn-ghost" id="lc-clear">' + icon('trash', 15) + '<span>Clear line</span></button>' +
      '<span class="lc-save" id="lc-save" hidden><button class="btn btn-sm btn-primary" id="lc-ok">' + icon('save', 15) + '<span>Save line</span></button><button class="btn btn-sm btn-ghost" id="lc-cancel">Cancel</button></span>' +
      '<span class="dim lc-hint" id="lc-hint"></span></div>';
  }
  function overlayHtml() {
    return '<div class="lc-draw" id="lc-layer" hidden><svg class="lc-svg" id="lc-svg" width="100%" height="100%" aria-hidden="true"></svg></div>';
  }

  function svgLine(a, b, cls) {
    // arrow on the IN side = normal (-dy, dx) of A→B; the overlay is not square, so work in 100x100 box coordinates scaled by the aspect
    const L = ctx.layerRect();
    const dx = (b.x - a.x) * L.w, dy = (b.y - a.y) * L.h, len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, k = 42;
    const tip = { x: mx + nx * k / L.w, y: my + ny * k / L.h };
    const p = function (q) { return (q.x * 100) + '%,' + (q.y * 100) + '%'; };
    return '<line x1="' + a.x * 100 + '%" y1="' + a.y * 100 + '%" x2="' + b.x * 100 + '%" y2="' + b.y * 100 + '%" class="lc-l ' + cls + '"/>' +
      '<line x1="' + mx * 100 + '%" y1="' + my * 100 + '%" x2="' + tip.x * 100 + '%" y2="' + tip.y * 100 + '%" class="lc-a ' + cls + '" marker-end="url(#lc-head)"/>' +
      '<text x="' + tip.x * 100 + '%" y="' + tip.y * 100 + '%" class="lc-t" dx="6" dy="14">IN</text>' +
      '<circle cx="' + a.x * 100 + '%" cy="' + a.y * 100 + '%" r="6" class="lc-p"/><text x="' + a.x * 100 + '%" y="' + a.y * 100 + '%" class="lc-t" dx="9" dy="-8">A</text>' +
      '<circle cx="' + b.x * 100 + '%" cy="' + b.y * 100 + '%" r="6" class="lc-p"/><text x="' + b.x * 100 + '%" y="' + b.y * 100 + '%" class="lc-t" dx="9" dy="-8">B</text>';
  }
  function paintDraft() {
    const svg = ctx.root().querySelector('#lc-svg');
    if (!svg) return;
    const defs = '<defs><marker id="lc-head" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#FFD23F"/></marker></defs>';
    svg.innerHTML = defs + (draft ? svgLine(draft.a, draft.b, 'draft') : '');
  }
  function setHint() {
    const root = ctx.root(), h = root.querySelector('#lc-hint');
    if (!h) return;
    const cfg = ctx.cfgResp() && ctx.cfgResp().config, ln = cfg && cfg.line;
    h.innerHTML = drawMode ? icon('info', 13) + ' <b>Drag on the picture from A to B.</b> The arrow marks the IN side. Esc cancels.'
      : (ln ? icon('info', 13) + ' Line saved for this camera (' + (ln.refWidth ? ln.refWidth + '×' + ln.refHeight : 'normalised') + '). The arrow on the picture shows IN.' : icon('info', 13) + ' No line yet — press Draw line while the picture is live.');
    window.AV.icons.hydrate(h);
  }
  function setDrawMode(on) {
    const root = ctx.root();
    drawMode = on;
    const layer = root.querySelector('#lc-layer'), b = root.querySelector('#lc-draw');
    if (!layer) return;
    layer.hidden = !on && !draft;
    layer.classList.toggle('is-draw', on);
    b.setAttribute('aria-pressed', String(on)); b.classList.toggle('btn-primary', on);
    if (!on && !draft) paintDraft();
    setHint();
  }
  function bind() {
    const root = ctx.root(), layer = root.querySelector('#lc-layer');
    let start = null;
    const pt = function (e) {
      const r = layer.getBoundingClientRect();
      return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
    };
    layer.addEventListener('pointerdown', function (e) { if (!drawMode) return; e.preventDefault(); layer.setPointerCapture(e.pointerId); start = pt(e); draft = { a: start, b: start }; root.querySelector('#lc-save').hidden = true; paintDraft(); });
    layer.addEventListener('pointermove', function (e) { if (start) { draft = { a: start, b: pt(e) }; paintDraft(); } });
    layer.addEventListener('pointerup', function (e) {
      if (!start) return;
      draft = { a: start, b: pt(e) }; start = null; paintDraft();
      const L = ctx.layerRect(), len = Math.hypot((draft.b.x - draft.a.x) * L.w, (draft.b.y - draft.a.y) * L.h);
      if (len < 30) { draft = null; paintDraft(); ui.toast('info', 'Line too short', 'Drag a longer line across the path.'); return; }
      root.querySelector('#lc-save').hidden = false;
    });
    root.querySelector('#lc-draw').addEventListener('click', function () { if (!ctx.streamOn()) { ui.toast('info', 'Start detection first', 'The line is drawn on the live picture.'); return; } setDrawMode(!drawMode); });
    root.querySelector('#lc-cancel').addEventListener('click', discard);
    root.querySelector('#lc-ok').addEventListener('click', saveDraft);
    root.querySelector('#lc-flip').addEventListener('click', flip);
    root.querySelector('#lc-clear').addEventListener('click', clearLine);
    document.addEventListener('keydown', function esc_(e) { if (!ctx || !ctx.root()) { document.removeEventListener('keydown', esc_); return; } if (e.key === 'Escape' && (drawMode || draft)) discard(); });
  }
  function discard() { draft = null; setDrawMode(false); ctx.root().querySelector('#lc-save').hidden = true; paintDraft(); }
  function frameSize() {
    const img = ctx.img(), cm = ctx.status() && ctx.status().cross && ctx.status().cross.frame;
    return { w: (cm && cm.width) || img.naturalWidth || 0, h: (cm && cm.height) || img.naturalHeight || 0 };
  }
  async function saveDraft() {
    if (!draft) return;
    const f = frameSize();
    const line = { x1: r4(draft.a.x), y1: r4(draft.a.y), x2: r4(draft.b.x), y2: r4(draft.b.y), refWidth: f.w || null, refHeight: f.h || null };
    await ctx.save({ line: line }, ctx.root().querySelector('#lc-ok'), false);
    draft = null; setDrawMode(false); ctx.root().querySelector('#lc-save').hidden = true; paintDraft();
  }
  async function flip() {
    const ln = ctx.cfgResp() && ctx.cfgResp().config.line;
    if (!ln) { ui.toast('info', 'No line yet', 'Draw a line first.'); return; }
    await ctx.save({ line: { x1: ln.x2, y1: ln.y2, x2: ln.x1, y2: ln.y1, refWidth: ln.refWidth, refHeight: ln.refHeight } }, ctx.root().querySelector('#lc-flip'), false);
    ui.toast('info', 'IN and OUT swapped', 'The arrow on the picture now points to the other side.');
  }
  async function clearLine() {
    const ln = ctx.cfgResp() && ctx.cfgResp().config.line;
    if (!ln) return;
    if (!(await ui.confirm({ title: 'Clear the line?', message: 'Removes the line of ' + ctx.camId() + '. Counting stops until a new line is drawn.', confirmText: 'Clear line' }))) return;
    await ctx.save({ line: null }, null, false);
  }

  /** Called by detection-live.js after the live card is built / when the stream starts or stops. */
  function attach(context) {
    ctx = context;
    const root = ctx.root();
    if (!root.querySelector('#lc-bar')) return;
    bind();
    setHint();
  }
  function fit(img) {
    if (unfit) { unfit(); unfit = null; }
    const layer = ctx && ctx.root() && ctx.root().querySelector('#lc-layer');
    if (img && layer) unfit = window.AV.media.fitOverlay(img, layer);
  }
  function release() { if (unfit) { unfit(); unfit = null; } draft = null; drawMode = false; ctx = null; }
  function onStream(on) { if (!ctx) return; if (!on) discard(); }
  function refreshHint() { if (ctx) setHint(); }

  // ------------------------------------------------------------------ settings
  const ALERT_LABELS = {
    twhelmet: [['Helmet compliant', 'Helmet compliant (rider wears a helmet)'], ['Helmet missing', 'Helmet missing'], ['Helmet unverified', 'Helmet not verified (needs a look)']],
    pcross: [['Person IN', 'Person crossed IN'], ['Person OUT', 'Person crossed OUT']]
  };
  function settingsHtml(modId, cfgResp) {
    const c = cfgResp.config, d = cfgResp.defaults, al = c.alert || {}, sn = c.snapshot || {};
    const num = function (name, val, min, max, step, w) { return '<input class="input input-sm" type="number" name="' + name + '" id="lc-' + name + '" value="' + esc(val) + '" min="' + min + '" max="' + max + '" step="' + step + '" style="width:' + (w || 84) + 'px">'; };
    const fld = function (label, name, ctl, hint) { return '<div class="field"><label for="lc-' + name + '">' + label + '</label>' + ctl + (hint ? '<span class="field-hint">' + hint + '</span>' : '') + '</div>'; };
    const sel = function (name, opts, cur) { return '<select class="select select-sm" id="lc-' + name + '" name="' + name + '">' + opts.map(function (o) { return '<option value="' + o[0] + '"' + (String(cur) === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>'; };
    const chk = function (name, on, label, hint) { return '<label class="check"><input type="checkbox" name="' + name + '" ' + (on ? 'checked' : '') + '><span>' + label + (hint ? ' <span class="dim">— ' + hint + '</span>' : '') + '</span></label>'; };
    let h = '<div class="card-head"><h2 id="dl-set-h">' + icon('sliders', 18) + 'Settings</h2><span class="spacer"></span><span class="badge badge-info" title="Saved on the backend for this camera">' + esc(cfgResp.cameraId) + '</span></div>' +
      '<form id="dl-form" class="stack" novalidate><div class="form-grid">' +
      fld('Direction counted', 'direction', sel('direction', [['both', 'Both (IN and OUT)'], ['in', 'IN only (towards the arrow)'], ['out', 'OUT only (against the arrow)']], c.direction), 'IN = crossing towards the arrow drawn on the picture. Use Flip IN / OUT to swap sides.') +
      fld('Minimum confidence', 'confidence', num('confidence', c.confidence, 0.05, 0.99, 0.01), 'Default ' + esc(d.confidence) + '.') +
      fld('Analysed frames per second', 'targetFps', num('targetFps', c.targetFps, 1, 30, 1), 'Target ' + esc(d.targetFps) + ' fps per camera. Only the newest frame is analysed; stale frames are dropped and counted.') +
      fld('Reference point', 'refPoint', sel('refPoint', [['bottom', 'Bottom centre (feet / wheels)'], ['center', 'Box centre']], c.refPoint), 'Which point of the box must pass the line.') +
      fld('Frames before a track counts', 'minTrackFrames', num('minTrackFrames', c.minTrackFrames, 1, 30, 1), 'A track seen in fewer frames never counts (ghost detections).') +
      fld('Dead band around the line', 'hysteresis', num('hysteresis', c.hysteresis, 0, 0.3, 0.01), 'Fraction of the object\'s height. Jitter inside it cannot flip the side.') +
      fld('Line ends: count this far beyond A / B', 'endExtend', num('endExtend', c.endExtend, 0, 3, 0.1), 'Fraction of the person\'s width. People passing right at A or B are still counted; 0 = exact segment only.') +
      fld('Same track cannot re-cross within (s)', 'recrossSec', num('recrossSec', c.recrossSec, 0, 60, 0.5), '') +
      fld('Id hand-over window (s)', 'handoverSec', num('handoverSec', c.handoverSec, 0, 5, 0.1), 'A lost id replaced by a new one nearby within this time is the same object (occlusion).') +
      fld('Duplicate window (s)', 'dedupeSec', num('dedupeSec', c.dedupeSec, 0, 10, 0.1), 'Two crossings in one direction at the same spot within this time count once.') +
      fld('Minimum object size', 'minBoxFrac', num('minBoxFrac', c.minBoxFrac, 0.005, 0.5, 0.005), 'Fraction of the picture height; smaller objects are ignored.') + '</div>';
    if (modId === 'pcross') h += chk('excludeRiders', c.excludeRiders !== false, 'Do not count riders of motorcycles / bicycles as people', 'they belong to the vehicle');
    if (modId === 'twhelmet') {
      h += chk('includeBicycles', !!c.includeBicycles, 'Also check bicycles', 'off = motorcycles / scooters only') +
        '<div class="dl-sub"><b>Helmet judge</b><div class="form-grid mt-8">' +
        fld('Helmet needs confidence ≥', 'helmetMinConf', num('helmetMinConf', c.helmetMinConf, 0.25, 0.99, 0.01), 'Default ' + esc(d.helmetMinConf) + '.') +
        fld('No-helmet needs confidence ≥', 'noHelmetMinConf', num('noHelmetMinConf', c.noHelmetMinConf, 0.25, 0.99, 0.01), 'Default ' + esc(d.noHelmetMinConf) + '. Needed in two views of the head.') +
        fld('Frames that must say “no helmet”', 'minMissingVotes', num('minMissingVotes', c.minMissingVotes, 1, 8, 1), 'Of the last 8 judged frames of that rider, with none saying helmet.') +
        fld('Frames that must say “helmet”', 'minHelmetVotes', num('minHelmetVotes', c.minHelmetVotes, 1, 8, 1), '') + '</div></div>';
    }
    h += '<div class="field"><label for="lc-snapmode">Snapshot of each crossing</label>' + sel('snapmode', [['crossing', 'Save the frame of the crossing'], ['off', 'No snapshot (still counted)']], sn.mode || 'crossing') + '</div>' +
      '<div class="dl-sub"><div class="row" style="justify-content:space-between"><b>Alert generation</b>' + ui.switchHtml('dl-alert-on', !!al.enabled, 'Generate alerts', { cls: 'blue' }) + '</div>' +
      '<div class="row wrap mt-8" style="gap:12px">' + ALERT_LABELS[modId].map(function (l) { return '<label class="check"><input type="checkbox" name="alertl" value="' + esc(l[0]) + '" ' + ((al.labels || []).indexOf(l[0]) >= 0 ? 'checked' : '') + '><span>' + esc(l[1]) + '</span></label>'; }).join('') + '</div>' +
      '<div class="row wrap mt-8" style="gap:10px"><label class="dim" style="font-size:12.5px" for="lc-severity">' + (modId === 'twhelmet' ? 'Missing severity' : 'Severity') + '</label>' +
      sel('severity', ['low', 'medium', 'high', 'critical'].map(function (x) { return [x, ui.cap(x)]; }), al.severity) +
      (modId === 'twhelmet' ? '<label class="dim" style="font-size:12.5px" for="lc-compliantSeverity">Compliant severity</label>' + sel('compliantSeverity', ['low', 'medium', 'high', 'critical'].map(function (x) { return [x, ui.cap(x)]; }), al.compliantSeverity || 'low') : '') +
      '<label class="dim" style="font-size:12.5px" for="lc-alertCool">Same track again within (s)</label>' + num('alertCool', al.cooldownSec || 0, 0, 3600, 1) + '</div>' +
      '<span class="field-hint">' + (modId === 'pcross' ? 'Counting does not need alerts: every crossing is counted and recorded. Alerts are off by default (one per person would be noisy).' : 'One event per two-wheeler and crossing. “Compliant” and “Helmet missing” are separate alerts.') + '</span></div>' +
      '<div class="notice notice-warning">' + icon('info', 16) + '<div>' + (modId === 'twhelmet' ? 'Helmet checks assist supervisors; they do not certify compliance. “Not verified” is a normal answer when the evidence is weak.' : 'Counts depend on detection and tracking quality; crowding and occlusion can still miss or merge people.') + '</div></div>' +
      '<div class="field-error" id="dl-form-err"></div>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">' + icon('save', 14) + '<span>Apply settings</span></button><button class="btn btn-sm btn-ghost" type="button" id="dl-reset">Reset to defaults</button></div>' +
      '<p class="dim" style="font-size:12px">Settings apply live — no restart needed. The line itself is edited on the live picture.</p></form>';
    return h;
  }
  function readForm(modId, form, errEl) {
    const q = function (nm) { return form.querySelector('[name="' + nm + '"]'); };
    const f = function (nm) { return parseFloat(q(nm).value); };
    const out = {
      direction: q('direction').value, confidence: f('confidence'), targetFps: f('targetFps'), refPoint: q('refPoint').value,
      minTrackFrames: parseInt(q('minTrackFrames').value, 10), hysteresis: f('hysteresis'), endExtend: f('endExtend'), recrossSec: f('recrossSec'), handoverSec: f('handoverSec'),
      dedupeSec: f('dedupeSec'), minBoxFrac: f('minBoxFrac'), snapshot: { mode: q('snapmode').value },
      alert: { severity: q('severity').value, cooldownSec: f('alertCool'), labels: Array.prototype.map.call(form.querySelectorAll('[name="alertl"]:checked'), function (c) { return c.value; }) }
    };
    if (modId === 'pcross') out.excludeRiders = q('excludeRiders').checked;
    if (modId === 'twhelmet') {
      out.includeBicycles = q('includeBicycles').checked;
      out.helmetMinConf = f('helmetMinConf'); out.noHelmetMinConf = f('noHelmetMinConf');
      out.minMissingVotes = parseInt(q('minMissingVotes').value, 10); out.minHelmetVotes = parseInt(q('minHelmetVotes').value, 10);
      out.alert.compliantSeverity = q('compliantSeverity').value;
    }
    const nums = [out.confidence, out.targetFps, out.minTrackFrames, out.hysteresis, out.endExtend, out.recrossSec, out.handoverSec, out.dedupeSec, out.minBoxFrac, out.alert.cooldownSec];
    if (nums.some(function (x) { return !isFinite(x); })) { errEl.textContent = 'Fill in every number.'; return null; }
    return out;
  }

  window.AV.cross = { MOD: MOD, rows: rows, panel: panel, toolbarHtml: toolbarHtml, overlayHtml: overlayHtml, attach: attach, fit: fit, release: release,
    onStream: onStream, refreshHint: refreshHint, settingsHtml: settingsHtml, readForm: readForm };
})();
