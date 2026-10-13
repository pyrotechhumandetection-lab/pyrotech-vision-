/* PYROTECH VISION — interactive detection-area (zone) editor.
 *
 * Zones are stored in NORMALISED image coordinates (0..1) relative to the video frame,
 * not the preview container. The canvas is laid over the exact image content rectangle
 * (accounting for object-fit letterboxing), so zones stay on the same pixels when the
 * preview is resized, goes full screen, or the stream resolution differs from the box.
 *
 *   rectangle: { type:'rectangle', x, y, width, height }
 *   polygon:   { type:'polygon', points:[{x,y}, ...] }
 */
(function () {
  'use strict';
  const HANDLE = 7;           // px handle radius
  const CLOSE_DIST = 12;      // px to snap-close a polygon

  function clamp01(v) { return Math.max(0, Math.min(1, v)); }
  function round4(v) { return Math.round(v * 10000) / 10000; }

  function zonePoints(z) {
    if (z.type === 'rectangle') return [{ x: z.x, y: z.y }, { x: z.x + z.width, y: z.y }, { x: z.x + z.width, y: z.y + z.height }, { x: z.x, y: z.y + z.height }];
    return z.points || [];
  }
  function polyArea(pts) {
    let a = 0;
    for (let i = 0; i < pts.length; i++) { const p = pts[i], q = pts[(i + 1) % pts.length]; a += p.x * q.y - q.x * p.y; }
    return Math.abs(a) / 2;
  }
  function segIntersect(a, b, c, d) {
    function o(p, q, r) { const v = (q.y - p.y) * (r.x - q.x) - (q.x - p.x) * (r.y - q.y); return v > 1e-12 ? 1 : v < -1e-12 ? -1 : 0; }
    return o(a, b, c) !== o(a, b, d) && o(c, d, a) !== o(c, d, b) && o(a, b, c) !== 0 && o(a, b, d) !== 0;
  }
  function selfIntersects(pts) {
    const n = pts.length;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      if (Math.abs(i - j) <= 1 || (i === 0 && j === n - 1)) continue;
      if (segIntersect(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n])) return true;
    }
    return false;
  }
  function pointInPoly(p, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const a = pts[i], b = pts[j];
      if (((a.y > p.y) !== (b.y > p.y)) && (p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x)) inside = !inside;
    }
    return inside;
  }

  /** Returns an error string, or null if the zone geometry is valid. */
  function validateZone(z) {
    if (!z || !z.name || !String(z.name).trim()) return 'Zone needs a name.';
    if (z.type === 'rectangle') {
      const vals = [z.x, z.y, z.width, z.height];
      if (vals.some(function (v) { return typeof v !== 'number' || !isFinite(v); })) return 'Rectangle has invalid coordinates.';
      if (z.width < 0.01 || z.height < 0.01) return 'Rectangle "' + z.name + '" is too small.';
      if (z.x < 0 || z.y < 0 || z.x + z.width > 1.0001 || z.y + z.height > 1.0001) return 'Rectangle "' + z.name + '" extends outside the frame.';
      return null;
    }
    if (z.type === 'polygon') {
      const pts = z.points || [];
      if (pts.length < 3) return 'Polygon "' + z.name + '" needs at least 3 points.';
      if (pts.some(function (p) { return !(p.x >= 0 && p.x <= 1 && p.y >= 0 && p.y <= 1); })) return 'Polygon "' + z.name + '" has points outside the frame.';
      if (polyArea(pts) < 0.0005) return 'Polygon "' + z.name + '" has (almost) zero area.';
      if (selfIntersects(pts)) return 'Polygon "' + z.name + '" crosses itself.';
      return null;
    }
    return 'Unknown zone shape.';
  }

  class ZoneEditor {
    /**
     * @param {HTMLElement} stage  positioned container holding the media element
     * @param {object} opts { zones, onChange(zones, selectedId), colors, onToolChange(tool) }
     */
    constructor(stage, opts) {
      this.stage = stage;
      this.opts = opts || {};
      this.zones = JSON.parse(JSON.stringify(opts.zones || []));
      this.history = [];
      this.tool = 'select';
      this.showGrid = false;
      this.enabled = opts.enabled !== false;
      this.selected = null;
      this.draft = null;     // in-progress drawing
      this.drag = null;
      this.hover = null;
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'zone-canvas';
      this.canvas.tabIndex = 0;
      this.canvas.setAttribute('role', 'application');
      this.canvas.setAttribute('aria-label', 'Detection area editor. Choose Rectangle or Polygon, then draw on the preview. Enter finishes a polygon, Escape cancels, Delete removes the selected zone, arrow keys move it.');
      stage.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d');
      this._bind();
    }

    setMedia(media) {
      if (this._unfit) this._unfit();
      this.media = media;
      if (!media) { this.canvas.style.display = 'none'; return; }
      this.canvas.style.display = '';
      const self = this;
      this._unfit = window.AV.media.fitOverlay(media, this.canvas);
      if (this._ro) this._ro.disconnect();
      this._ro = new ResizeObserver(function () { self._resize(); });
      this._ro.observe(this.canvas);
      this._resize();
    }

    _resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
      this.w = w; this.h = h;
      this.canvas.width = Math.max(1, Math.round(w * dpr));
      this.canvas.height = Math.max(1, Math.round(h * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }

    // ----- public API -----
    setTool(tool) {
      if (this.draft && tool !== this.tool) this.cancelDraft();
      this.tool = tool;
      this.canvas.dataset.tool = tool;
      if (this.opts.onToolChange) this.opts.onToolChange(tool);
      this.draw();
    }
    setGrid(on) { this.showGrid = on; this.draw(); }
    setEnabled(on) { this.enabled = on; this.canvas.classList.toggle('zones-disabled', !on); this.draw(); }
    getZones() { return JSON.parse(JSON.stringify(this.zones)); }
    setZones(zones) { this.zones = JSON.parse(JSON.stringify(zones || [])); this.selected = null; this._changed(false); }
    select(id) { this.selected = id; this._changed(false); }
    canUndo() { return this.history.length > 0 || (this.draft && this.draft.type === 'polygon' && this.draft.points.length > 0); }

    undo() {
      if (this.draft && this.draft.type === 'polygon' && this.draft.points.length) {
        this.draft.points.pop();
        if (!this.draft.points.length) this.draft = null;
        this.draw(); this._notify();
        return true;
      }
      if (!this.history.length) return false;
      this.zones = JSON.parse(this.history.pop());
      if (!this.zones.some(function (z) { return z.id === this.selected; }, this)) this.selected = null;
      this._changed(false);
      return true;
    }
    clearAll() { if (!this.zones.length) return; this._snap(); this.zones = []; this.selected = null; this.draft = null; this._changed(true); }
    removeZone(id) { this._snap(); this.zones = this.zones.filter(function (z) { return z.id !== id; }); if (this.selected === id) this.selected = null; this._changed(true); }
    updateZone(id, patch, snapshot) {
      const z = this.zones.find(function (x) { return x.id === id; });
      if (!z) return;
      if (snapshot !== false) this._snap();
      Object.assign(z, patch);
      this._changed(true);
    }
    finishPolygon() {
      if (!this.draft || this.draft.type !== 'polygon') return { ok: false, error: 'No polygon is being drawn.' };
      if (this.draft.points.length < 3) return { ok: false, error: 'A polygon needs at least 3 points.' };
      const z = this._newZone({ type: 'polygon', points: this.draft.points.map(function (p) { return { x: round4(p.x), y: round4(p.y) }; }) });
      const err = validateZone(z);
      if (err) return { ok: false, error: err };
      this._snap();
      this.zones.push(z);
      this.selected = z.id;
      this.draft = null;
      this._changed(true);
      return { ok: true, zone: z };
    }
    cancelDraft() { this.draft = null; this.draw(); this._notify(); }
    isDrawing() { return !!this.draft; }
    draftPointCount() { return this.draft && this.draft.points ? this.draft.points.length : 0; }

    destroy() {
      if (this._unfit) this._unfit();
      if (this._ro) this._ro.disconnect();
      window.removeEventListener('pointerup', this._up);
      this.canvas.remove();
    }

    // ----- internals -----
    _newZone(geom) {
      const colors = this.opts.colors || ['#3478FF'];
      const used = this.zones.map(function (z) { return z.color; });
      const color = colors.find(function (c) { return used.indexOf(c) < 0; }) || colors[this.zones.length % colors.length];
      let n = this.zones.length + 1;
      const names = this.zones.map(function (z) { return z.name; });
      while (names.indexOf('Zone ' + n) >= 0) n++;
      return Object.assign({ id: 'z' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), name: 'Zone ' + n, color: color, enabled: true, modules: [] }, geom);
    }
    _snap() { this.history.push(JSON.stringify(this.zones)); if (this.history.length > 50) this.history.shift(); }
    _changed(dirty) { this.draw(); this._notify(dirty); }
    _notify(dirty) { if (this.opts.onChange) this.opts.onChange(this.getZones(), this.selected, dirty !== false); }

    _pt(e) {
      const r = this.canvas.getBoundingClientRect();
      return { x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) };
    }
    _px(p) { return { x: p.x * this.w, y: p.y * this.h }; }
    _dist(a, b) { const A = this._px(a), B = this._px(b); return Math.hypot(A.x - B.x, A.y - B.y); }

    _hitHandle(p) {
      const z = this.zones.find(function (x) { return x.id === this.selected; }, this);
      if (!z) return null;
      const pts = zonePoints(z);
      for (let i = 0; i < pts.length; i++) if (this._dist(p, pts[i]) <= HANDLE + 4) return { zone: z, index: i };
      return null;
    }
    _hitZone(p) {
      for (let i = this.zones.length - 1; i >= 0; i--) if (pointInPoly(p, zonePoints(this.zones[i]))) return this.zones[i];
      return null;
    }

    _bind() {
      const c = this.canvas, self = this;
      c.addEventListener('pointerdown', function (e) { self._down(e); });
      c.addEventListener('pointermove', function (e) { self._move(e); });
      this._up = function (e) { self._pointerUp(e); };
      window.addEventListener('pointerup', this._up);
      c.addEventListener('dblclick', function (e) { if (self.tool === 'polygon' && self.draft) { e.preventDefault(); self._finishFromUi(); } });
      c.addEventListener('keydown', function (e) { self._key(e); });
    }

    _finishFromUi() {
      const r = this.finishPolygon();
      if (!r.ok && window.AV.ui) window.AV.ui.toast('warning', 'Polygon not finished', r.error);
    }

    _down(e) {
      if (!this.enabled) return;
      if (e.button !== undefined && e.button !== 0) return;
      const p = this._pt(e);
      this.canvas.focus({ preventScroll: true });
      if (this.tool === 'rectangle') {
        this.draft = { type: 'rectangle', a: p, b: p };
        this.canvas.setPointerCapture(e.pointerId);
      } else if (this.tool === 'polygon') {
        if (!this.draft) this.draft = { type: 'polygon', points: [], cursor: p };
        const pts = this.draft.points;
        if (pts.length >= 3 && this._dist(p, pts[0]) <= CLOSE_DIST) { this._finishFromUi(); return; }
        if (pts.length && this._dist(p, pts[pts.length - 1]) < 3) return; // ignore double taps on same spot
        pts.push(p);
        this._notify(false);
      } else {
        const h = this._hitHandle(p);
        if (h) { this._snap(); this.drag = { mode: 'vertex', zone: h.zone, index: h.index }; this.canvas.setPointerCapture(e.pointerId); return; }
        const z = this._hitZone(p);
        this.selected = z ? z.id : null;
        if (z) { this._snap(); this.drag = { mode: 'move', zone: z, start: p, orig: JSON.parse(JSON.stringify(z)), moved: false }; this.canvas.setPointerCapture(e.pointerId); }
        this._changed(false);
      }
      this.draw();
    }

    _move(e) {
      const p = this._pt(e);
      if (this.draft && this.draft.type === 'rectangle') { this.draft.b = p; this.draw(); return; }
      if (this.draft && this.draft.type === 'polygon') { this.draft.cursor = p; this.draw(); return; }
      if (this.drag) {
        const z = this.drag.zone;
        if (this.drag.mode === 'vertex') {
          if (z.type === 'rectangle') {
            const pts = zonePoints(z); pts[this.drag.index] = p;
            const opp = pts[(this.drag.index + 2) % 4];
            z.x = round4(Math.min(p.x, opp.x)); z.y = round4(Math.min(p.y, opp.y));
            z.width = round4(Math.abs(p.x - opp.x)); z.height = round4(Math.abs(p.y - opp.y));
            // keep the same physical corner under the pointer after normalisation
            this.drag.index = (p.x <= opp.x ? (p.y <= opp.y ? 0 : 3) : (p.y <= opp.y ? 1 : 2));
          } else { z.points[this.drag.index] = { x: round4(p.x), y: round4(p.y) }; }
        } else {
          const dx = p.x - this.drag.start.x, dy = p.y - this.drag.start.y, o = this.drag.orig;
          this.drag.moved = this.drag.moved || Math.abs(dx) + Math.abs(dy) > 0.002;
          if (z.type === 'rectangle') {
            z.x = round4(clamp01(Math.min(1 - o.width, Math.max(0, o.x + dx))));
            z.y = round4(clamp01(Math.min(1 - o.height, Math.max(0, o.y + dy))));
          } else {
            const xs = o.points.map(function (q) { return q.x; }), ys = o.points.map(function (q) { return q.y; });
            const cdx = Math.max(-Math.min.apply(null, xs), Math.min(1 - Math.max.apply(null, xs), dx));
            const cdy = Math.max(-Math.min.apply(null, ys), Math.min(1 - Math.max.apply(null, ys), dy));
            z.points = o.points.map(function (q) { return { x: round4(q.x + cdx), y: round4(q.y + cdy) }; });
          }
        }
        this.draw();
        return;
      }
      const hv = this.tool === 'select' ? (this._hitHandle(p) ? 'handle' : (this._hitZone(p) ? 'zone' : null)) : null;
      if (hv !== this.hover) { this.hover = hv; this.canvas.style.cursor = hv === 'handle' ? 'grab' : hv === 'zone' ? 'move' : ''; }
    }

    _pointerUp() {
      if (this.draft && this.draft.type === 'rectangle') {
        const a = this.draft.a, b = this.draft.b;
        const z = this._newZone({ type: 'rectangle', x: round4(Math.min(a.x, b.x)), y: round4(Math.min(a.y, b.y)), width: round4(Math.abs(a.x - b.x)), height: round4(Math.abs(a.y - b.y)) });
        this.draft = null;
        if (z.width >= 0.01 && z.height >= 0.01) { this._snap(); this.zones.push(z); this.selected = z.id; this._changed(true); }
        else this.draw();
        return;
      }
      if (this.drag) {
        const moved = this.drag.mode === 'vertex' || this.drag.moved;
        if (!moved) this.history.pop(); // no-op click: discard snapshot
        const z = this.drag.zone;
        this.drag = null;
        if (moved && z.type === 'polygon' && validateZone(z)) {
          window.AV.ui && window.AV.ui.toast('warning', 'Invalid shape', validateZone(z) + ' Use Undo to revert.');
        }
        this._changed(moved);
      }
    }

    _key(e) {
      if (e.key === 'Escape' && this.draft) { e.preventDefault(); e.stopPropagation(); this.cancelDraft(); return; }
      if (e.key === 'Enter' && this.draft && this.draft.type === 'polygon') { e.preventDefault(); this._finishFromUi(); return; }
      if ((e.key === 'Backspace') && this.draft) { e.preventDefault(); this.undo(); return; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); this.undo(); return; }
      const z = this.zones.find(function (x) { return x.id === this.selected; }, this);
      if (!z) return;
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); this.removeZone(z.id); return; }
      const step = e.shiftKey ? 0.02 : 0.005;
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (d) {
        e.preventDefault(); this._snap();
        if (z.type === 'rectangle') { z.x = round4(Math.max(0, Math.min(1 - z.width, z.x + d[0]))); z.y = round4(Math.max(0, Math.min(1 - z.height, z.y + d[1]))); }
        else {
          const xs = z.points.map(function (q) { return q.x; }), ys = z.points.map(function (q) { return q.y; });
          const dx = Math.max(-Math.min.apply(null, xs), Math.min(1 - Math.max.apply(null, xs), d[0]));
          const dy = Math.max(-Math.min.apply(null, ys), Math.min(1 - Math.max.apply(null, ys), d[1]));
          z.points = z.points.map(function (q) { return { x: round4(q.x + dx), y: round4(q.y + dy) }; });
        }
        this._changed(true);
      }
    }

    draw() {
      const ctx = this.ctx, w = this.w, h = this.h;
      if (!w || !h) return;
      ctx.clearRect(0, 0, w, h);
      if (this.showGrid) {
        ctx.save(); ctx.strokeStyle = 'rgba(255,255,255,0.16)'; ctx.lineWidth = 1;
        for (let i = 1; i < 10; i++) {
          ctx.beginPath(); ctx.moveTo(Math.round(w * i / 10) + 0.5, 0); ctx.lineTo(Math.round(w * i / 10) + 0.5, h); ctx.stroke();
          ctx.beginPath(); ctx.moveTo(0, Math.round(h * i / 10) + 0.5); ctx.lineTo(w, Math.round(h * i / 10) + 0.5); ctx.stroke();
        }
        ctx.restore();
      }
      const self = this;
      this.zones.forEach(function (z) {
        const pts = zonePoints(z).map(function (p) { return self._px(p); });
        if (pts.length < 2) return;
        const active = self.enabled && z.enabled !== false;
        ctx.save();
        ctx.globalAlpha = active ? 1 : 0.45;
        ctx.beginPath(); pts.forEach(function (p, i) { i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y); }); ctx.closePath();
        ctx.fillStyle = hexA(z.color, z.id === self.selected ? 0.32 : 0.22); ctx.fill();
        ctx.lineWidth = z.id === self.selected ? 2.5 : 2; ctx.strokeStyle = z.color;
        if (!active) ctx.setLineDash([6, 5]);
        ctx.stroke(); ctx.setLineDash([]);
        // vertices
        pts.forEach(function (p) {
          ctx.beginPath(); ctx.arc(p.x, p.y, z.id === self.selected ? HANDLE - 1 : 4, 0, Math.PI * 2);
          ctx.fillStyle = '#fff'; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = z.color; ctx.stroke();
        });
        // label
        const top = pts.reduce(function (a, p) { return p.y < a.y ? p : a; }, pts[0]);
        const cx = pts.reduce(function (s, p) { return s + p.x; }, 0) / pts.length;
        const label = z.name + (active ? '' : ' (off)');
        ctx.font = '600 12px Inter, Segoe UI, system-ui, sans-serif';
        const tw = ctx.measureText(label).width + 14;
        const lx = Math.max(2, Math.min(w - tw - 2, cx - tw / 2)), ly = Math.max(2, Math.min(h - 24, top.y + 8));
        ctx.fillStyle = z.color; roundRect(ctx, lx, ly, tw, 22, 5); ctx.fill();
        ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle'; ctx.fillText(label, lx + 7, ly + 11.5);
        ctx.restore();
      });
      // in-progress drawing
      if (this.draft) {
        ctx.save(); ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.fillStyle = 'rgba(52,120,255,.18)';
        if (this.draft.type === 'rectangle') {
          const a = this._px(this.draft.a), b = this._px(this.draft.b);
          ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(a.x - b.x), Math.abs(a.y - b.y));
          ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(a.x - b.x), Math.abs(a.y - b.y));
        } else {
          const pts = this.draft.points.map(function (p) { return self._px(p); });
          if (pts.length) {
            ctx.beginPath(); pts.forEach(function (p, i) { i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y); });
            if (this.draft.cursor) { const c = this._px(this.draft.cursor); ctx.lineTo(c.x, c.y); }
            ctx.stroke(); ctx.setLineDash([]);
            pts.forEach(function (p, i) {
              ctx.beginPath(); ctx.arc(p.x, p.y, i === 0 && pts.length >= 3 ? 8 : 5, 0, Math.PI * 2);
              ctx.fillStyle = i === 0 && pts.length >= 3 ? '#13D99A' : '#fff'; ctx.fill();
            });
          }
        }
        ctx.restore();
      }
    }
  }

  function hexA(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return 'rgba(52,120,255,' + a + ')';
    const n = parseInt(m[1], 16);
    return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  window.AV.ZoneEditor = ZoneEditor;
  window.AV.zones = { validate: validateZone, points: zonePoints };
})();
