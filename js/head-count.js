/* PYROTECH VISION — Human Head Count: capture ONE still frame and count every visible head inside the
 * detection area.
 *
 *   1. select a camera (its live feed is shown),
 *   2. draw / select the Human Count detection area (rectangle or polygon) on that feed - it is saved in the
 *      camera's Detection Area zones with the "Human Count" module ticked, so the camera form shows it too,
 *   3. Capture & Count: the backend grabs one frame, runs the dedicated head detector inside the area and
 *      saves the original + annotated images. The count shown here and the annotated image always come from
 *      that same captured frame; nothing updates until the next Capture & Count.
 *
 * Mounted by the Human Count page (js/human-count.js) as its "Head count" tab.
 */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cat = window.AV.catalog;
  const esc = ui.esc;
  const MOD = 'count';
  const AREA_COLORS = ['#13D99A'].concat(cat.ZONE_COLORS.filter(function (c) { return c !== '#13D99A'; }));

  function mount(el, opts) {
    opts = opts || {};
    let camId = '', cam = null, zones = [], editor = null, dirty = false, result = null, view = 'annotated';
    let settingsResp = null, recent = [], busy = false, active = true, loadSeq = 0, mediaImg = null;

    el.innerHTML =
      '<div class="card-head"><h2 id="hh-h">' + icon('users', 18) + 'Head Count <span class="dim" style="font-weight:500;font-size:13px">· capture a still frame and count every visible head</span></h2><span class="spacer"></span>' +
      '<label class="sr-only" for="hh-cam">Camera</label><select class="select select-sm" id="hh-cam" style="width:auto;max-width:340px"></select></div>' +
      '<ol class="hh-steps" id="hh-steps" aria-label="Steps"></ol>' +
      '<div class="hh-grid">' +
      '<div class="hh-left">' +
      '<div class="zone-toolbar" role="toolbar" aria-label="Detection area tools">' +
      tool('select', 'pen', 'Select / Edit') + tool('rectangle', 'square', 'Rectangle') + tool('polygon', 'polygon', 'Polygon') +
      '<button type="button" class="btn btn-sm btn-primary hidden" id="hh-finish">' + icon('check', 15) + '<span>Finish Polygon</span></button>' +
      '<button type="button" class="btn btn-sm hidden" id="hh-cancel">' + icon('x', 15) + '<span>Cancel</span></button>' +
      '<button type="button" class="btn btn-sm" id="hh-undo">' + icon('undo', 15) + '<span>Undo</span></button>' +
      '<button type="button" class="btn btn-sm" id="hh-clear">' + icon('trash', 15) + '<span>Clear</span></button>' +
      '<button type="button" class="btn btn-sm" id="hh-whole" title="Use the whole camera frame as the detection area">' + icon('maximize', 15) + '<span>Whole frame</span></button>' +
      '<button type="button" class="btn btn-sm" id="hh-save" disabled>' + icon('save', 15) + '<span>Save area</span></button></div>' +
      '<div class="pv-stage hh-stage" id="hh-stage"><div class="pv-media" id="hh-media"></div>' +
      '<div class="hh-busy" id="hh-busy" hidden><span class="spinner lg"></span><b>Capturing frame &amp; counting heads…</b><span id="hh-busy-sub">The first count also loads the head detection model.</span></div></div>' +
      '<p class="zone-hint" id="hh-hint" aria-live="polite"></p>' +
      '<div id="hh-areas"></div>' +
      '</div>' +
      '<div class="hh-right">' +
      '<div class="hh-total" id="hh-total" aria-live="polite"></div>' +
      '<button type="button" class="btn btn-primary hh-capture" id="hh-capture">' + icon('snapshot', 18) + '<span>Capture &amp; Count</span></button>' +
      '<p class="dim hh-capture-note">One frame is captured from the camera and analysed. The count does not change until you capture again.</p>' +
      '<dl class="kv dl-kv hh-meta" id="hh-meta"></dl>' +
      '<details class="hh-settings" id="hh-settings"><summary>' + icon('sliders', 15) + '<span>Detection settings</span></summary><div id="hh-settings-body"></div></details>' +
      '</div></div>' +
      '<div class="hh-result" id="hh-result"></div>' +
      '<h3 class="hc-sub-h">Recent head counts</h3><div id="hh-recent"></div>';
    window.AV.icons.hydrate(el);

    function tool(t, ic, label) { return '<button type="button" class="btn btn-sm tool-btn" data-tool="' + t + '" aria-pressed="false">' + icon(ic, 15) + '<span>' + label + '</span></button>'; }
    const $ = function (s) { return el.querySelector(s); };

    // ------------------------------------------------------------ zone editor
    editor = new window.AV.ZoneEditor($('#hh-stage'), {
      zones: [], colors: AREA_COLORS, enabled: true,
      onChange: function (zs, sel, isDirty) {
        zs.forEach(function (z) { if ((z.modules || []).indexOf(MOD) < 0) { z.modules = (z.modules || []).concat([MOD]); editor.updateZone(z.id, { modules: z.modules }, false); } });
        if (isDirty) dirty = true;
        syncToolbar(); renderAreas(); renderSteps();
      },
      onToolChange: syncToolbar
    });
    el.querySelectorAll('.tool-btn[data-tool]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (!editor.media && b.dataset.tool !== 'select') { ui.toast('info', 'No camera frame yet', 'Select a camera - the area is drawn on its live feed.'); return; }
        editor.setTool(b.dataset.tool); editor.canvas.focus();
      });
    });
    $('#hh-finish').addEventListener('click', function () {
      const r = editor.finishPolygon();
      if (!r.ok) ui.toast('warning', 'Polygon not finished', r.error);
      syncToolbar();
    });
    $('#hh-cancel').addEventListener('click', function () { editor.cancelDraft(); syncToolbar(); });
    $('#hh-undo').addEventListener('click', function () { editor.undo(); syncToolbar(); });
    $('#hh-clear').addEventListener('click', async function () {
      if (!editor.getZones().length) return;
      const ok = await ui.confirm({ title: 'Clear the detection area?', message: 'Remove all Human Count areas of this camera? Use Save area to store the change.', confirmText: 'Clear', danger: true });
      if (ok) { editor.clearAll(); syncToolbar(); }
    });
    $('#hh-whole').addEventListener('click', function () {
      const z = { id: 'hc' + Date.now().toString(36), name: uniqueName('Whole frame'), color: AREA_COLORS[0], enabled: true, modules: [MOD], type: 'rectangle', x: 0, y: 0, width: 1, height: 1 };
      editor.setZones([z]); dirty = true; renderAreas(); renderSteps(); syncToolbar();
      ui.toast('info', 'Whole frame selected', 'Heads anywhere in the frame will be counted. Save area to keep it.');
    });
    $('#hh-save').addEventListener('click', function () { saveArea(true); });
    $('#hh-capture').addEventListener('click', capture);
    $('#hh-cam').addEventListener('change', function (e) { if (opts.onCameraChange) opts.onCameraChange(e.target.value); else setCamera(e.target.value); });

    function uniqueName(base) {
      const names = zones.filter(function (z) { return (z.modules || []).indexOf(MOD) < 0; }).map(function (z) { return z.name.toLowerCase(); })
        .concat(editor.getZones().map(function (z) { return z.name.toLowerCase(); }));
      let n = base, i = 2;
      while (names.indexOf(n.toLowerCase()) >= 0) n = base + ' ' + i++;
      return n;
    }

    function syncToolbar() {
      const drawing = editor.isDrawing(), has = editor.getZones().length > 0;
      el.querySelectorAll('.tool-btn[data-tool]').forEach(function (b) { b.setAttribute('aria-pressed', String(editor.tool === b.dataset.tool)); b.disabled = !editor.media || busy; });
      $('#hh-finish').classList.toggle('hidden', !(drawing && editor.tool === 'polygon'));
      $('#hh-cancel').classList.toggle('hidden', !drawing);
      $('#hh-undo').disabled = !editor.canUndo() || busy;
      $('#hh-clear').disabled = !has || busy;
      $('#hh-whole').disabled = !camId || busy;
      $('#hh-save').disabled = !dirty || busy || !camId;
      $('#hh-save').classList.toggle('btn-primary', dirty);
      const hint = $('#hh-hint');
      if (!camId) hint.textContent = 'Select a camera to draw its detection area on the live feed.';
      else if (drawing && editor.tool === 'polygon') hint.textContent = 'Click to add points. Click the first point, press Enter or Finish Polygon to close the area. Esc cancels.';
      else if (drawing) hint.textContent = 'Drag to draw the rectangle.';
      else if (!has) hint.innerHTML = '<b>Draw the detection area for Human Count:</b> choose Rectangle or Polygon and draw on the camera feed, or use Whole frame. Only heads inside the area are counted.';
      else hint.textContent = (dirty ? 'Unsaved changes — Save area (Capture & Count also saves it). ' : '') + 'Drag an area to move it, drag its handles to reshape it. Delete removes the selected area.';
      const cap = $('#hh-capture');
      cap.disabled = busy || !camId || !api.status.connected;
      cap.title = !api.status.connected ? 'Backend not connected' : !camId ? 'Select a camera first' : '';
      cap.querySelector('span').textContent = busy ? 'Counting…' : (result ? 'Capture & Count again' : 'Capture & Count');
    }

    function renderSteps() {
      const has = editor.getZones().length > 0;
      const st = [
        ['Select camera', !!camId],
        ['Draw / select detection area', has && !dirty],
        ['Capture & Count', !!result]
      ];
      $('#hh-steps').innerHTML = st.map(function (s, i) {
        return '<li class="' + (s[1] ? 'done' : '') + '"><span class="step-num">' + (s[1] ? icon('check', 12) : i + 1) + '</span>' + esc(s[0]) + '</li>';
      }).join('');
    }

    function renderAreas() {
      const list = editor.getZones(), box = $('#hh-areas');
      if (!list.length) { box.innerHTML = ''; return; }
      box.innerHTML = '<div class="hh-area-list">' + list.map(function (z) {
        const shared = (z.modules || []).filter(function (m) { return m !== MOD; });
        return '<div class="hh-area" data-z="' + esc(z.id) + '"><span class="dot" style="color:' + esc(z.color) + '"></span>' +
          '<input class="input input-sm hh-area-name" value="' + esc(z.name) + '" maxlength="40" aria-label="Area name">' +
          '<span class="dim" style="font-size:12px">' + (z.type === 'rectangle' ? 'Rectangle' : 'Polygon · ' + (z.points || []).length + ' points') + (shared.length ? ' · also used by ' + esc(shared.map(function (m) { const md = cat.moduleById(m); return md ? md.name : m; }).join(', ')) : '') + '</span>' +
          ui.switchHtml('hh-en-' + z.id, z.enabled !== false, 'Use area ' + z.name, { attrs: 'data-en' }) +
          '<button type="button" class="btn btn-xs btn-ghost danger-hover" data-del aria-label="Remove area ' + esc(z.name) + '">' + icon('trash', 13) + '</button></div>';
      }).join('') + '</div>';
      window.AV.icons.hydrate(box);
      box.querySelectorAll('.hh-area').forEach(function (row) {
        const id = row.dataset.z;
        row.querySelector('.hh-area-name').addEventListener('change', function (e) {
          const v = e.target.value.trim();
          if (!v) { ui.toast('warning', 'Area name required'); renderAreas(); return; }
          editor.updateZone(id, { name: v });
        });
        row.querySelector('[data-en]').addEventListener('change', function (e) { editor.updateZone(id, { enabled: e.target.checked }); });
        row.querySelector('[data-del]').addEventListener('click', function () { editor.removeZone(id); });
      });
    }

    // ------------------------------------------------------------ camera + live feed
    function renderCameraSelect() {
      const sel = $('#hh-cam');
      const cams = state.cameras().filter(function (c) { return c.source === 'backend'; });
      sel.innerHTML = '<option value="">' + (cams.length ? 'Select a camera…' : (api.status.connected ? 'No backend cameras' : 'Backend unavailable')) + '</option>' + cams.map(function (c) {
        const ad = ui.camAddress(c), ip = ad && ad.host ? ad.host : '';
        return '<option value="' + esc(c.id) + '"' + (c.id === camId ? ' selected' : '') + (c.enabled === false ? ' disabled' : '') + '>' + esc(c.name + ' · ' + c.id + (ip ? ' · ' + ip : '') + (c.enabled === false ? ' · disabled' : '')) + '</option>';
      }).join('');
      sel.disabled = !api.status.connected || busy;
    }

    function setMediaPlaceholder(ic, title, text, err) {
      editor.setMedia(null); mediaImg = null;
      $('#hh-media').innerHTML = '<div class="media-ph' + (err ? ' err' : '') + '">' + icon(ic, 28) + '<b>' + esc(title) + '</b><span>' + esc(text || '') + '</span></div>';
      syncToolbar();
    }

    function releaseFeed() {
      if (mediaImg) { mediaImg.onload = mediaImg.onerror = null; mediaImg.removeAttribute('src'); }   // closes the MJPEG connection
      mediaImg = null;
      editor.setMedia(null);
    }

    async function loadFeed() {
      const seq = ++loadSeq;
      releaseFeed();
      if (!camId || !active) return;
      $('#hh-media').innerHTML = '<div class="media-ph"><span class="spinner lg"></span><b>Opening the live feed…</b><span>The detection area is drawn on the camera\'s live picture.</span></div>';
      try {
        const s = await api.cameras.stream(camId);
        if (seq !== loadSeq) return;
        if (s.transport !== 'mjpeg' && s.transport !== 'image') { setMediaPlaceholder('alertCircle', 'Live feed not available', 'Stream transport ' + s.transport + ' cannot be drawn on.', true); return; }
        const img = new Image();
        img.className = 'pv-img'; img.alt = 'Live feed of ' + (cam ? cam.name : camId);
        img.onload = function () {
          if (seq !== loadSeq) return;
          const box = $('#hh-media'); box.innerHTML = ''; box.appendChild(img);
          box.insertAdjacentHTML('beforeend', '<span class="badge badge-live pv-flag"><span class="dot pulse"></span>LIVE</span>');
          editor.setMedia(img);
          if (!editor.getZones().length && editor.tool === 'select') editor.setTool('rectangle');
          syncToolbar();
          img.onload = null;
        };
        img.onerror = function () { if (seq === loadSeq) setMediaPlaceholder('wifiOff', 'Camera feed unavailable', 'The backend could not open the camera stream. Check the camera in Camera Management.', true); };
        img.src = api.resolveUrl(s.url) + '?v=' + Date.now();
        mediaImg = img;
      } catch (e) { if (seq === loadSeq) setMediaPlaceholder('alertCircle', 'Live feed unavailable', e.message, true); }
    }

    async function setCamera(id, force) {
      if (id === camId && !force) return;
      if (dirty && camId && id !== camId) {
        const save = await ui.confirm({ title: 'Save the detection area?', message: 'The Human Count area of ' + camId + ' has unsaved changes.', confirmText: 'Save', cancelText: 'Discard' });
        if (save) await saveArea(false);
      }
      camId = id || ''; cam = null; zones = []; dirty = false; result = null; view = 'annotated';
      editor.setZones([]);
      renderCameraSelect(); renderResult(); renderMeta(); renderTotal(); renderRecent();
      if (!camId) { setMediaPlaceholder('camera', 'No camera selected', 'Choose a camera to see its live feed and draw the detection area.'); renderSteps(); renderAreas(); return; }
      try {
        cam = await api.cameras.get(camId);
        if (cam.id !== camId) return;
        zones = cam.zones || [];
        editor.setZones(zones.filter(function (z) { return (z.modules || []).indexOf(MOD) >= 0; }));
        dirty = false;
      } catch (e) { ui.toast('error', 'Camera could not be loaded', e.message); }
      renderAreas(); renderSteps(); renderMeta(); syncToolbar();
      loadFeed();
      loadRecent();
    }

    // ------------------------------------------------------------ save area
    async function saveArea(toast) {
      if (!camId) return false;
      const mine = editor.getZones();
      for (let i = 0; i < mine.length; i++) {
        const err = window.AV.zones.validate(mine[i]);
        if (err) { ui.toast('warning', 'Detection area not valid', err); return false; }
      }
      const mineIds = mine.map(function (z) { return z.id; });
      // zones of other modules stay untouched; an area also used by another module keeps its other modules
      const merged = zones.filter(function (z) { return mineIds.indexOf(z.id) < 0; }).map(function (z) {
        if ((z.modules || []).indexOf(MOD) >= 0) { const m = z.modules.filter(function (x) { return x !== MOD; }); return m.length ? Object.assign({}, z, { modules: m }) : null; }
        return z;
      }).filter(Boolean);
      const others = merged.map(function (z) { return z.name.trim().toLowerCase(); });
      const all = merged.concat(mine.map(function (z) {
        const name = others.indexOf(z.name.trim().toLowerCase()) >= 0 ? z.name.trim() + ' (count)' : z.name.trim();
        return Object.assign({}, z, { name: name, modules: z.modules && z.modules.indexOf(MOD) >= 0 ? z.modules : (z.modules || []).concat([MOD]) });
      }));
      ui.setBusy($('#hh-save'), true, 'Saving…');
      try {
        await api.detection.saveZones(camId, all);
        zones = all; dirty = false;
        editor.setZones(all.filter(function (z) { return z.modules.indexOf(MOD) >= 0; }));
        if (toast) ui.toast('success', 'Detection area saved', mine.length ? mine.length + ' Human Count area(s) saved for ' + camId + '. They also appear in the camera\'s Detection Area.' : 'Human Count area removed for ' + camId + '.');
        state.refreshFromBackend(['cameras']);
        return true;
      } catch (e) {
        ui.toast('error', 'Detection area not saved', e.message);
        return false;
      } finally { ui.setBusy($('#hh-save'), false); renderAreas(); renderSteps(); syncToolbar(); }
    }

    // ------------------------------------------------------------ capture & count
    async function capture() {
      if (busy || !camId) return;
      if (editor.isDrawing()) { ui.toast('info', 'Finish the area first', 'Finish or cancel the polygon you are drawing.'); return; }
      let body = { cameraId: camId };
      const enabled = editor.getZones().filter(function (z) { return z.enabled !== false; });
      if (!enabled.length) {
        const whole = await ui.confirm({ title: 'No detection area selected', message: (editor.getZones().length ? 'All Human Count areas of ' + camId + ' are switched off.' : 'No Human Count area is drawn for ' + camId + '.') + ' Draw a rectangle or polygon on the camera feed to count only that area, or count heads in the whole frame.', confirmText: 'Count whole frame', cancelText: 'Draw an area' });
        if (!whole) { if (editor.media) editor.setTool('rectangle'); syncToolbar(); return; }
        body.fullFrame = true;
      } else if (dirty) {
        if (!(await saveArea(false))) return;
      }
      if (!body.fullFrame) body.zoneIds = editor.getZones().filter(function (z) { return z.enabled !== false; }).map(function (z) { return z.id; });
      busy = true; syncToolbar(); renderCameraSelect();
      $('#hh-busy').hidden = false;
      $('#hh-busy-sub').textContent = settingsResp && settingsResp.modelLoaded ? 'Running the head detector on the captured frame.' : 'The first count also loads the head detection model (a few seconds).';
      const t0 = Date.now();
      try {
        const r = await api.detection.headCount(body);
        result = r; view = 'annotated';
        if (r.snapshot) state.addSnapshotFromBackend(r.snapshot);
        if (settingsResp) settingsResp.modelLoaded = true;
        ui.toast(r.totalHeads ? 'success' : 'info', r.totalHeads ? r.totalHeads + ' head' + (r.totalHeads === 1 ? '' : 's') + ' counted' : 'No heads detected', (r.cameraName || camId) + ' · captured ' + r.capturedAtLocal + ' · ' + ((Date.now() - t0) / 1000).toFixed(1) + ' s');
        loadRecent();
      } catch (e) {
        if (e.code === 'NOT_FOUND') ui.toast('error', 'Backend needs a restart', 'The running backend is an older version without Head Count. Close the backend window and start start.cmd (or start-backend.cmd) again, then retry.');
        else ui.toast('error', 'Head count failed', e.message);
        if (e.code === 'ROI_REQUIRED') { if (editor.media) editor.setTool('rectangle'); }
      } finally {
        busy = false; $('#hh-busy').hidden = true;
        renderTotal(); renderMeta(); renderResult(); renderSteps(); syncToolbar(); renderCameraSelect();
      }
    }

    function renderTotal() {
      const t = $('#hh-total');
      if (!result) {
        t.innerHTML = '<span class="hh-num dim">—</span><span class="hh-lbl">Total heads detected</span><span class="hh-sub dim">' + (camId ? 'Press Capture &amp; Count' : 'Select a camera') + '</span>';
        return;
      }
      t.innerHTML = '<span class="hh-num' + (result.totalHeads ? '' : ' zero') + '">' + esc(result.totalHeads) + '</span><span class="hh-lbl">Total heads detected</span>' +
        '<span class="hh-sub">' + esc(result.fullFrame ? 'Whole frame' : (result.roi || []).map(function (z) { return z.name; }).join(', ')) + ' · ' + esc(result.capturedAtLocal) + '</span>' +
        (result.totalHeads ? '' : '<span class="hh-none">' + icon('info', 14) + esc(result.message || 'No heads were detected in the selected area.') + '</span>');
      window.AV.icons.hydrate(t);
    }

    function renderMeta() {
      const m = $('#hh-meta'), r = result, c = camId ? state.getCamera(camId) : null;
      const row = function (k, v) { return '<dt>' + k + '</dt><dd>' + v + '</dd>'; };
      m.innerHTML =
        row('Camera', c ? esc(c.name) + ' <span class="mono dim">' + esc(c.id) + '</span>' : '<span class="dim">None selected</span>') +
        row('Camera IP', c ? ui.ipHtml(c) : '<span class="dim">—</span>') +
        row('Detection type', 'Human Head Count') +
        row('Captured at', r ? esc(r.capturedAtLocal) + ' <span class="dim">(' + (r.frameSource === 'session' ? 'frame from the running stream' : 'frame read from the camera') + ')</span>' : '<span class="dim">Not captured yet</span>') +
        row('Frame', r ? esc(r.width + ' × ' + r.height) : '<span class="dim">—</span>') +
        row('AI model', esc((r && r.model) || (settingsResp && settingsResp.model) || 'head_yolov8m (head detector)') + ' <span class="dim">· ' + esc(((r && r.engine) || (settingsResp && settingsResp.engine) || '').replace('openvino', 'OpenVINO').replace('pytorch', 'PyTorch')) + '</span>') +
        (r ? row('Processing', esc(Math.round(r.inferenceMs)) + ' ms detection <span class="dim">· confidence ≥ ' + esc(r.settings.confidence) + ', NMS IoU ' + esc(r.settings.iou) + '</span>') : '');
    }

    function renderResult() {
      const box = $('#hh-result'), r = result;
      if (!r) { box.innerHTML = ''; return; }
      const snap = r.snapshot || {};
      const ann = api.resolveUrl(snap.url), org = snap.originalUrl ? api.resolveUrl(snap.originalUrl) : null;
      box.innerHTML = '<div class="card-head"><h3 class="hc-sub-h" style="margin:0">Annotated snapshot <span class="dim" style="font-weight:500">· ' + esc(snap.id || '') + '</span></h3><span class="spacer"></span>' +
        '<div class="segmented" role="group" aria-label="Image"><button type="button" data-v="annotated" aria-pressed="' + (view === 'annotated') + '">Annotated</button><button type="button" data-v="original" aria-pressed="' + (view === 'original') + '"' + (org ? '' : ' disabled') + '>Original</button></div>' +
        '<a class="btn btn-sm btn-ghost" id="hh-dl" href="' + esc(view === 'original' && org ? org : ann) + '?download=1" download>' + icon('download', 14) + '<span>Download</span></a>' +
        '<a class="btn btn-sm btn-ghost" href="#/snapshots">' + icon('image', 14) + '<span>All snapshots</span></a></div>' +
        '<div class="hh-shot"><img id="hh-img" src="' + esc(view === 'original' && org ? org : ann) + '" alt="' + (view === 'original' ? 'Original captured frame' : 'Annotated frame with ' + esc(r.totalHeads) + ' numbered head boxes') + '"></div>' +
        (r.heads.length ? '<details class="hh-heads"><summary>Detected heads (' + r.heads.length + ')</summary><div class="table-wrap" style="max-height:240px;overflow:auto"><table class="table"><thead><tr><th>#</th><th>Confidence</th><th>Box (x1, y1, x2, y2 px)</th><th>Size</th></tr></thead><tbody>' +
          r.heads.map(function (h) { const b = h.box; return '<tr><td class="mono">' + h.id + '</td><td>' + Number(h.confidence).toFixed(2) + '</td><td class="mono dim">' + b.join(', ') + '</td><td class="dim">' + (b[2] - b[0]) + ' × ' + (b[3] - b[1]) + ' px</td></tr>'; }).join('') +
          '</tbody></table></div></details>' : '');
      window.AV.icons.hydrate(box);
      box.querySelectorAll('[data-v]').forEach(function (b) { b.addEventListener('click', function () { view = b.dataset.v; renderResult(); }); });
    }

    // ------------------------------------------------------------ recent results
    async function loadRecent() {
      if (!camId || !api.status.connected) { recent = []; renderRecent(); return; }
      try { const r = await api.detection.headCounts(camId, 12); recent = r.results || []; } catch (e) { recent = []; }
      renderRecent();
    }
    function renderRecent() {
      const box = $('#hh-recent');
      if (!recent.length) { box.innerHTML = '<p class="muted" style="font-size:13px">' + (camId ? 'No head counts saved for this camera yet. Each Capture &amp; Count is saved (original + annotated image) and listed here and in Snapshots.' : 'Select a camera.') + '</p>'; return; }
      box.innerHTML = '<div class="hh-recent">' + recent.map(function (s) {
        return '<button type="button" class="hh-rc' + (result && result.snapshot && result.snapshot.id === s.id ? ' on' : '') + '" data-id="' + esc(s.id) + '"><img loading="lazy" src="' + esc(api.resolveUrl(s.url)) + '" alt=""><span><b>' + esc(s.headCount) + '</b> head' + (s.headCount === 1 ? '' : 's') + '</span><span class="dim">' + esc(s.capturedAtLocal || ui.fmtTime(s.timestamp)) + '</span></button>';
      }).join('') + '</div>';
      box.querySelectorAll('.hh-rc').forEach(function (b) {
        b.addEventListener('click', function () {
          const s = recent.find(function (x) { return x.id === b.dataset.id; }); if (!s) return;
          result = { totalHeads: s.headCount, heads: s.heads || [], roi: s.roi || [], fullFrame: !!s.fullFrame, capturedAtLocal: s.capturedAtLocal || ui.fmtTime(s.timestamp),
            frameSource: s.frameSource, width: s.width, height: s.height, model: s.model, engine: s.engine, inferenceMs: s.inferenceMs || 0,
            settings: s.headSettings || {}, snapshot: s, message: s.headCount ? null : 'No heads were detected in the selected area.' };
          view = 'annotated';
          renderTotal(); renderMeta(); renderResult(); renderRecent(); renderSteps(); syncToolbar();
          $('#hh-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
    }

    // ------------------------------------------------------------ settings
    async function loadSettings() {
      if (!api.status.connected) { settingsResp = null; renderSettings(); return; }
      try { settingsResp = await api.detection.headSettings(); } catch (e) { settingsResp = null; }
      renderSettings(); renderMeta();
    }
    function renderSettings() {
      const b = $('#hh-settings-body');
      if (!settingsResp) { b.innerHTML = '<p class="muted" style="font-size:13px">Settings are stored on the backend.</p>'; return; }
      const s = settingsResp.settings, d = settingsResp.defaults;
      b.innerHTML = '<form id="hh-form" class="stack" novalidate>' +
        '<div class="field"><label for="hh-conf">Head confidence</label><div class="range-row"><input class="range" type="range" id="hh-conf" name="confidence" min="0.05" max="0.95" step="0.01" value="' + Number(s.confidence).toFixed(2) + '" data-range-out="hh-conf-out"><output class="range-val" id="hh-conf-out"></output></div>' +
        '<span class="field-hint">Minimum score for a head. Lower finds more partly hidden / distant heads but risks false boxes. Default ' + d.confidence + '.</span></div>' +
        '<div class="field"><label for="hh-iou">Duplicate suppression (NMS IoU)</label><div class="range-row"><input class="range" type="range" id="hh-iou" name="iou" min="0.10" max="0.90" step="0.01" value="' + Number(s.iou).toFixed(2) + '" data-range-out="hh-iou-out"><output class="range-val" id="hh-iou-out"></output></div>' +
        '<span class="field-hint">Boxes overlapping more than this are one head. Lower merges more (fewer duplicates, but touching heads may merge). Default ' + d.iou + '.</span></div>' +
        '<div class="form-grid"><div class="field"><label for="hh-size">Detection resolution</label><select class="select select-sm" id="hh-size" name="imgSize">' +
        [640, 960, 1280, 1600, 1920].map(function (v) { return '<option value="' + v + '"' + (+s.imgSize === v ? ' selected' : '') + '>' + v + ' px' + (v === d.imgSize ? ' (default)' : '') + '</option>'; }).join('') + '</select>' +
        '<span class="field-hint">Higher finds smaller heads; slower.</span></div>' +
        '<div class="field"><label for="hh-tiling">Tiled detection</label><select class="select select-sm" id="hh-tiling" name="tiling">' +
        [['auto', 'Auto (large areas)'], ['on', 'Always'], ['off', 'Off']].map(function (o) { return '<option value="' + o[0] + '"' + (s.tiling === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>' +
        '<span class="field-hint">Also scans overlapping tiles so small distant heads keep enough pixels.</span></div>' +
        '<div class="field"><label for="hh-min">Smallest head (px)</label><input class="input input-sm" type="number" id="hh-min" name="minHeadPx" min="2" max="200" step="1" value="' + esc(s.minHeadPx) + '" style="width:84px">' +
        '<span class="field-hint">Smaller boxes are ignored - too small to tell a head from other objects. Default ' + d.minHeadPx + '.</span></div></div>' +
        '<div class="field-error" id="hh-form-err"></div>' +
        '<div class="row"><button class="btn btn-primary btn-sm" type="submit">' + icon('save', 14) + '<span>Save settings</span></button><button class="btn btn-sm btn-ghost" type="button" id="hh-reset">Reset to defaults</button></div>' +
        '<p class="dim" style="font-size:12px">Used for every camera\'s next Capture &amp; Count.</p></form>';
      window.AV.modules.wireRanges(b);
      window.AV.icons.hydrate(b);
      const f = b.querySelector('#hh-form');
      f.addEventListener('submit', function (e) {
        e.preventDefault();
        const mh = parseInt(f.minHeadPx.value, 10);
        if (!(mh >= 2 && mh <= 200)) { b.querySelector('#hh-form-err').textContent = 'Smallest head must be 2 – 200 px.'; return; }
        saveSettings({ confidence: +f.confidence.value, iou: +f.iou.value, imgSize: +f.imgSize.value, tiling: f.tiling.value, minHeadPx: mh }, f.querySelector('[type=submit]'));
      });
      b.querySelector('#hh-reset').addEventListener('click', function () { saveSettings(Object.assign({}, settingsResp.defaults), null); });
    }
    async function saveSettings(s, btn) {
      if (btn) ui.setBusy(btn, true, 'Saving…');
      try { settingsResp = Object.assign(settingsResp || {}, await api.detection.saveHeadSettings(s)); ui.toast('success', 'Head count settings saved', 'Used for the next Capture & Count.'); renderSettings(); }
      catch (e) { const er = el.querySelector('#hh-form-err'); if (er) er.textContent = e.message; ui.toast('error', 'Settings not saved', e.message); }
      finally { if (btn && btn.isConnected) ui.setBusy(btn, false); }
    }

    // ------------------------------------------------------------ lifecycle
    function setActive(on) {
      active = on;
      if (!on) releaseFeed();
      else if (camId && !mediaImg) loadFeed();
      syncToolbar();
    }
    function onBackend() {
      renderCameraSelect();
      if (api.status.connected) { loadSettings(); if (camId && !cam) setCamera(camId, true); }
      syncToolbar(); renderMeta();
    }
    function destroy() { loadSeq++; releaseFeed(); editor.destroy(); }

    renderCameraSelect(); renderSteps(); renderTotal(); renderMeta(); renderSettings(); renderRecent(); syncToolbar();
    setMediaPlaceholder('camera', 'No camera selected', 'Choose a camera to see its live feed and draw the detection area.');
    if (api.status.connected) loadSettings();
    if (opts.cameraId) setCamera(opts.cameraId, true);

    return { setCamera: setCamera, setActive: setActive, onBackend: onBackend, renderCameraSelect: renderCameraSelect, destroy: destroy, isBusy: function () { return busy; } };
  }

  window.AV.headCount = { mount: mount };
})();
