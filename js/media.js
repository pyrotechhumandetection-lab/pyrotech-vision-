/* PYROTECH VISION — camera media: preview tiles, lazy stream attach/cleanup, overlay alignment, snapshots.
 *
 * Browsers cannot play RTSP. Live video must come from the backend via a browser transport:
 *   GET /cameras/:id/stream -> { transport: 'mjpeg' | 'image' | 'hls' | 'webrtc', url, audio? }
 * 'mjpeg' / 'image' render in <img>, 'hls' in <video> (native HLS only; add hls.js if needed),
 * 'webrtc' requires a signalling integration that is not part of this frontend yet.
 */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, api = window.AV.api, state = window.AV.state;
  const active = new Set();       // elements currently holding a stream
  let observer = null;

  /** Rectangle (relative to media element box) actually covered by image pixels. */
  function contentRect(media) {
    const box = { w: media.clientWidth, h: media.clientHeight };
    const nw = media.naturalWidth || media.videoWidth || 0, nh = media.naturalHeight || media.videoHeight || 0;
    if (!nw || !nh || !box.w || !box.h) return { x: 0, y: 0, w: box.w, h: box.h };
    const fit = getComputedStyle(media).objectFit || 'fill';
    if (fit === 'fill') return { x: 0, y: 0, w: box.w, h: box.h };
    const s = fit === 'cover' ? Math.max(box.w / nw, box.h / nh) : Math.min(box.w / nw, box.h / nh);
    const w = nw * s, h = nh * s;
    return { x: (box.w - w) / 2, y: (box.h - h) / 2, w: w, h: h };
  }

  /** Keep an absolutely-positioned overlay aligned with the image content (handles letterbox/crop). */
  function fitOverlay(media, overlay) {
    function apply() {
      const r = contentRect(media);
      overlay.style.left = (media.offsetLeft + r.x) + 'px';
      overlay.style.top = (media.offsetTop + r.y) + 'px';
      overlay.style.width = r.w + 'px';
      overlay.style.height = r.h + 'px';
    }
    apply();
    media.addEventListener('load', apply);
    media.addEventListener('loadedmetadata', apply);
    const ro = new ResizeObserver(apply);
    ro.observe(media);
    return function () { ro.disconnect(); };
  }

  function zonesSvg(zones) {
    const parts = (zones || []).filter(function (z) { return z.enabled !== false; }).map(function (z) {
      const c = ui.esc(z.color || '#3478FF');
      if (z.type === 'rectangle') {
        return '<rect x="' + z.x + '" y="' + z.y + '" width="' + z.width + '" height="' + z.height + '" fill="' + c + '" fill-opacity=".22" stroke="' + c + '" stroke-width="2" vector-effect="non-scaling-stroke"/>';
      }
      const pts = (z.points || []).map(function (p) { return p.x + ',' + p.y; }).join(' ');
      return '<polygon points="' + pts + '" fill="' + c + '" fill-opacity=".22" stroke="' + c + '" stroke-width="2" vector-effect="non-scaling-stroke"/>';
    }).join('');
    return '<svg class="zone-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">' + parts + '</svg>';
  }

  /**
   * Markup for a camera media area. Never shows a "LIVE" badge unless a real backend stream attaches.
   * opts: { overlays: bool, fit: 'cover'|'contain' }
   */
  function mediaHtml(cam, opts) {
    opts = opts || {};
    const fit = opts.fit || 'cover';
    const st = ui.camStatus(cam);
    if (cam.enabled === false) {
      return '<div class="media-ph">' + icon('power', 26) + '<b>Camera disabled</b><span>Enable it on the Cameras page to stream.</span></div>';
    }
    if (cam.source === 'backend' && api.status.connected) {
      return '<div class="media-slot" data-stream-cam="' + ui.esc(cam.id) + '" data-fit="' + fit + '" data-overlays="' + (opts.overlays ? '1' : '') + '">' +
        '<div class="media-ph">' + '<span class="spinner lg"></span><span>Waiting for stream…</span></div></div>';
    }
    if (st === 'offline' || st === 'error') {
      return '<div class="media-ph err">' + icon('wifiOff', 26) + '<b>' + (st === 'error' ? 'Connection error' : 'Camera offline') + '</b><span>' + ui.esc(cam.lastError || 'No frames received.') + '</span></div>';
    }
    return '<div class="media-ph">' + icon('camera', 26) + '<b>No live stream</b><span>' + (api.status.connected ? 'Camera is not registered on the backend yet.' : 'Live video requires the camera backend.') + '</span></div>';
  }

  /** Lazily attach backend streams for any [data-stream-cam] inside root. */
  function attach(root) {
    const slots = root.querySelectorAll('[data-stream-cam]');
    if (!slots.length) return;
    if (!observer) {
      observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting && !e.target.dataset.loaded) { e.target.dataset.loaded = '1'; loadStream(e.target); }
        });
      }, { rootMargin: '100px' });
    }
    slots.forEach(function (s) { observer.observe(s); });
  }

  async function loadStream(slot) {
    const id = slot.dataset.streamCam;
    try {
      const s = await api.cameras.stream(id);
      if (!slot.isConnected) return;
      const fit = slot.dataset.fit || 'cover';
      let el;
      if (s.transport === 'mjpeg' || s.transport === 'image') {
        el = document.createElement('img');
        el.alt = 'Live stream from ' + state.cameraName(id);
      } else if (s.transport === 'hls') {
        el = document.createElement('video');
        if (!el.canPlayType('application/vnd.apple.mpegurl')) throw new Error('This browser has no native HLS support. Add an HLS player or use MJPEG/WebRTC transport.');
        el.muted = true; el.autoplay = true; el.playsInline = true;
      } else {
        throw new Error('Stream transport "' + (s.transport || 'unknown') + '" is not supported by this frontend yet.');
      }
      el.className = 'media-el'; el.style.objectFit = fit;
      el.addEventListener('error', function () { showErr(slot, 'Stream stopped or could not be decoded by the browser.'); });
      el.src = api.resolveUrl(s.url);
      slot.innerHTML = '';
      slot.appendChild(el);
      slot.insertAdjacentHTML('beforeend', '<span class="badge badge-live media-flag"><span class="dot pulse"></span>LIVE</span>');
      if (slot.dataset.overlays) {
        const cam = state.getCamera(id);
        if (cam && cam.zones.length) { slot.insertAdjacentHTML('beforeend', zonesSvg(cam.zones)); fitOverlay(el, slot.querySelector('.zone-overlay')); }
      }
      if (s.audio) slot.dataset.audio = '1';
      active.add(slot);
    } catch (e) {
      showErr(slot, e.message);
    }
  }
  function showErr(slot, msg) {
    if (!slot.isConnected) return;
    slot.innerHTML = '<div class="media-ph err">' + icon('alertCircle', 24) + '<b>Stream unavailable</b><span>' + ui.esc(msg) + '</span></div>';
  }

  /** Align zone overlays with the media images inside root. */
  function alignOverlays(root) {
    root.querySelectorAll('.media-slot').forEach(function (slot) {
      const img = slot.querySelector('.media-el'), svg = slot.querySelector('.zone-overlay');
      if (img && svg && !slot._dispose) slot._dispose = fitOverlay(img, svg);
    });
  }

  /** Stop all streams inside root (or everywhere) — call on navigation. */
  function release(root) {
    active.forEach(function (slot) {
      if (root && !root.contains(slot) && slot.isConnected) return;
      slot.querySelectorAll('img').forEach(function (i) { i.removeAttribute('src'); });
      slot.querySelectorAll('video').forEach(function (v) { v.pause(); v.removeAttribute('src'); v.load(); });
      active.delete(slot);
    });
    if (observer && !root) { observer.disconnect(); observer = null; }
  }

  /** Real snapshot workflow: backend captures + saves, frontend displays the stored result. */
  async function captureSnapshot(cam, btn) {
    if (!api.status.connected) {
      ui.toast('warning', 'Snapshot not captured', 'Snapshot capture requires the camera backend. Nothing was saved.');
      return null;
    }
    ui.setBusy(btn, true, 'Capturing…');
    try {
      const snap = await api.cameras.snapshot(cam.id);
      if (!snap || !(snap.url || snap.image)) throw new Error('Backend did not return a saved image reference.');
      state.addSnapshotFromBackend(Object.assign({ cameraId: cam.id, type: 'manual', timestamp: new Date().toISOString(), source: 'backend' }, snap, { image: snap.image || snap.url }));
      ui.toast('success', 'Snapshot saved', 'Saved by the backend for ' + cam.name + '.');
      return snap;
    } catch (e) {
      ui.toast('error', 'Snapshot failed', e.message);
      return null;
    } finally { ui.setBusy(btn, false); }
  }

  window.AV.media = { mediaHtml: mediaHtml, attach: attach, alignOverlays: alignOverlays, release: release, fitOverlay: fitOverlay, contentRect: contentRect, zonesSvg: zonesSvg, captureSnapshot: captureSnapshot };
})();
