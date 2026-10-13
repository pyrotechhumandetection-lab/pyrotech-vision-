/* PYROTECH VISION — UI helpers: escaping, formatting, toasts, modals, confirm dialogs, shared renderers. */
(function () {
  'use strict';
  const icon = window.AV.icon;

  function esc(v) {
    return String(v === undefined || v === null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function h(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }

  function fmtTime(iso, opts) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return '—';
    const date = d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: opts && opts.noYear ? undefined : 'numeric' });
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    return date + ' ' + time;
  }
  function relTime(iso) {
    if (!iso) return 'never';
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 45) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }
  function debounce(fn, ms) { let t; return function () { const a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); }; }
  function throttle(fn, ms) { let last = 0, t; return function () { const a = arguments, now = Date.now(); clearTimeout(t); if (now - last >= ms) { last = now; fn.apply(this, a); } else { t = setTimeout(function () { last = Date.now(); fn.apply(null, a); }, ms - (now - last)); } }; }
  function uid(prefix) { return (prefix || 'id') + '-' + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-3); }

  // ---------- toasts ----------
  function toast(type, title, msg, ms) {
    let stack = $('#toast-stack');
    const iconName = { success: 'checkCircle', error: 'alertCircle', warning: 'alert', info: 'info' }[type] || 'info';
    const el = h('<div class="toast toast-' + type + '" role="' + (type === 'error' ? 'alert' : 'status') + '">' + icon(iconName, 20) +
      '<div class="grow"><div class="t-title">' + esc(title) + '</div>' + (msg ? '<div class="t-msg">' + esc(msg) + '</div>' : '') + '</div>' +
      '<button class="t-close" aria-label="Dismiss notification">' + icon('x', 16) + '</button></div>');
    const close = function () { if (!el.isConnected) return; el.classList.add('leaving'); setTimeout(function () { el.remove(); }, 230); };
    el.querySelector('.t-close').addEventListener('click', close);
    stack.appendChild(el);
    while (stack.children.length > 4) stack.firstElementChild.remove();
    setTimeout(close, ms || (type === 'error' ? 8000 : 4500));
    return close;
  }

  /** Close live MJPEG connections inside an element before it is discarded (browsers allow only ~6 per host). */
  function stopStreams(el) {
    if (!el) return;
    el.querySelectorAll('img').forEach(function (i) { if (/\.mjpeg(\?|$)/.test(i.getAttribute('src') || '')) i.removeAttribute('src'); });
    el.querySelectorAll('video').forEach(function (v) { try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) { /* ignore */ } });
  }

  // ---------- modal ----------
  const openModals = [];
  function modal(opts) {
    const id = uid('m');
    const prevFocus = document.activeElement;
    const bd = h('<div class="modal-backdrop" role="presentation"><div class="modal ' + (opts.size || '') + '" role="dialog" aria-modal="true" aria-labelledby="' + id + '-t">' +
      '<div class="modal-head">' + (opts.icon ? '<span style="color:var(--primary)">' + icon(opts.icon, 20) + '</span>' : '') + '<h2 id="' + id + '-t">' + esc(opts.title || '') + '</h2>' +
      (opts.headExtra || '') + '<button class="btn btn-ghost btn-icon" data-close aria-label="Close dialog">' + icon('x', 18) + '</button></div>' +
      '<div class="modal-body"></div>' + (opts.actions && opts.actions.length ? '<div class="modal-foot"></div>' : '') + '</div></div>');
    const body = bd.querySelector('.modal-body');
    if (typeof opts.body === 'string') body.innerHTML = opts.body; else if (opts.body) body.appendChild(opts.body);
    const api = { el: bd, body: body, close: close, setBusy: null };
    if (opts.actions) {
      const foot = bd.querySelector('.modal-foot');
      opts.actions.forEach(function (a) {
        const b = h('<button class="btn ' + (a.cls || '') + '">' + (a.icon ? icon(a.icon, 16) : '') + '<span>' + esc(a.label) + '</span></button>');
        b.addEventListener('click', function () { if (a.onClick) { const r = a.onClick(api, b); if (r === false) return; } if (a.close !== false) close(a.value); });
        foot.appendChild(b);
      });
    }
    let resolved = false, resolveFn;
    const promise = new Promise(function (r) { resolveFn = r; });
    function close(value) {
      if (resolved) return; resolved = true;
      stopStreams(bd);
      bd.classList.add('closing');
      setTimeout(function () { bd.remove(); }, 200);
      openModals.splice(openModals.indexOf(api), 1);
      document.removeEventListener('keydown', onKey, true);
      if (opts.onClose) opts.onClose(value);
      if (prevFocus && prevFocus.focus) try { prevFocus.focus(); } catch (e) { /* ignore */ }
      resolveFn(value);
    }
    function onKey(e) {
      if (openModals[openModals.length - 1] !== api) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(undefined); }
      if (e.key === 'Tab') { // focus trap
        const f = $$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', bd).filter(function (x) { return !x.disabled && x.offsetParent !== null; });
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    bd.addEventListener('mousedown', function (e) { if (e.target === bd && opts.dismissable !== false) close(undefined); });
    bd.querySelectorAll('[data-close]').forEach(function (b) { b.addEventListener('click', function () { close(undefined); }); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(bd);
    openModals.push(api);
    window.AV.icons.hydrate(bd);
    setTimeout(function () {
      const target = bd.querySelector('[autofocus]') || bd.querySelector('.modal-body input, .modal-body select, .modal-body textarea') || bd.querySelector('.modal-foot .btn-primary, .modal-foot .btn-danger') || bd.querySelector('[data-close]');
      if (target) target.focus();
    }, 30);
    api.promise = promise;
    return api;
  }

  function confirmDialog(o) {
    const m = modal({
      title: o.title || 'Are you sure?', size: 'sm', icon: o.danger ? 'alert' : 'info',
      body: '<p class="muted" style="line-height:1.6">' + esc(o.message || '') + '</p>' + (o.detail ? '<div class="notice notice-warning mt-12">' + icon('alert', 16) + '<div>' + esc(o.detail) + '</div></div>' : ''),
      actions: [
        { label: o.cancelText || 'Cancel', cls: 'btn-ghost', value: false },
        { label: o.confirmText || 'Confirm', cls: o.danger ? 'btn-danger' : 'btn-primary', value: true }
      ]
    });
    return m.promise.then(function (v) { return v === true; });
  }

  // ---------- button busy state ----------
  function setBusy(btn, busy, label) {
    if (!btn) return;
    if (busy) {
      btn.dataset.html = btn.innerHTML;
      btn.classList.add('is-busy'); btn.disabled = true; btn.setAttribute('aria-busy', 'true');
      btn.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>' + esc(label || 'Working…') + '</span>';
    } else {
      if (btn.dataset.html) btn.innerHTML = btn.dataset.html;
      btn.classList.remove('is-busy'); btn.disabled = false; btn.removeAttribute('aria-busy');
    }
  }

  // ---------- shared renderers ----------
  const STATUS = {
    online: { label: 'Online', cls: 'badge-success', dot: 'dot-success' },
    offline: { label: 'Offline', cls: 'badge-danger', dot: 'dot-danger' },
    error: { label: 'Connection error', cls: 'badge-danger', dot: 'dot-danger' },
    unverified: { label: 'Not verified', cls: 'badge-muted', dot: 'dot-muted' },
    disabled: { label: 'Disabled', cls: 'badge-muted', dot: 'dot-muted' }
  };
  function camStatus(cam) { return cam.enabled === false ? 'disabled' : (STATUS[cam.status] ? cam.status : 'unverified'); }
  function statusBadge(cam) {
    const s = STATUS[camStatus(cam)];
    return '<span class="badge ' + s.cls + '" data-status-cam="' + esc(cam.id) + '"><span class="dot ' + s.dot + '"></span>' + s.label + '</span>';
  }

  // ---------- camera address + live activity (from real backend data only) ----------
  /** Host / IP and port taken from the camera's configured source URL (credentials never included). */
  function camAddress(cam) {
    if (!cam) return null;
    if (cam.type === 'webcam') return { local: 'USB / local device' };
    const src = String(cam.sourceUrl || '').trim();
    const m = /^([a-z][a-z0-9+.-]*):\/\/([^/?#]*)/i.exec(src);
    if (!m) return cam.type === 'file' ? { local: 'Video file' } : null;
    let auth = m[2];
    const at = auth.lastIndexOf('@');
    if (at >= 0) auth = auth.slice(at + 1);
    const hm = /^(\[[^\]]+\]|[^:]+)(?::(\d+))?$/.exec(auth);
    if (!hm || !hm[1]) return null;
    const host = hm[1].replace(/^\[|\]$/g, '');
    const isIp = /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.indexOf(':') >= 0;
    return { host: host, port: hm[2] || '', isIp: isIp, scheme: m[1].toLowerCase() };
  }
  const LIVE = {
    processing: ['badge-success', 'Active · detecting', 'dot-success pulse'],
    connected: ['badge-success', 'Active · streaming', 'dot-success pulse'],
    connecting: ['badge-warning', 'Connecting…', 'dot-warning'],
    disconnected: ['badge-danger', 'Disconnected · reconnecting', 'dot-danger'],
    error: ['badge-danger', 'Stream error', 'dot-danger']
  };
  /** Live activity of a camera: running backend session first, otherwise its last known status. */
  function camLive(cam) {
    const api = window.AV.api, state = window.AV.state;
    if (cam.enabled === false) return { cls: 'badge-muted', label: 'Disabled', dot: 'dot-muted', active: false };
    if (cam.source !== 'backend') return { cls: 'badge-muted', label: api.status.connected ? 'Not on backend' : 'Backend not connected', dot: 'dot-muted', active: false };
    if (!api.status.connected) return { cls: 'badge-muted', label: 'Backend not connected', dot: 'dot-muted', active: false };
    const sess = state.session(cam.id);
    if (sess && LIVE[sess.state]) { const l = LIVE[sess.state]; return { cls: l[0], label: l[1], dot: l[2], active: sess.state === 'processing' || sess.state === 'connected' }; }
    if (!state.remote.metrics) return { cls: 'badge-muted', label: 'Status unavailable', dot: 'dot-muted', active: false };
    const st = camStatus(cam);
    if (st === 'error') return { cls: 'badge-danger', label: 'Connection error', dot: 'dot-danger', active: false };
    if (st === 'offline') return { cls: 'badge-danger', label: 'Offline', dot: 'dot-danger', active: false };
    return { cls: 'badge-muted', label: 'Not streaming', dot: 'dot-muted', active: false };
  }
  function ipInner(cam) {
    const a = camAddress(cam), l = camLive(cam);
    const addr = !a ? '<span class="dim">Address not available</span>'
      : a.local ? '<span class="dim">' + esc(a.local) + ' (no IP)</span>'
      : '<b class="cam-ip-host">' + esc(a.host) + '</b>' + (a.port ? '<span class="cam-ip-port">:' + esc(a.port) + '</span>' : '');
    return '<span class="cam-ip-addr"><span class="cam-ip-k">' + (a && !a.local && !a.isIp ? 'Host' : 'IP') + '</span>' + addr + '</span>' +
      '<span class="badge ' + l.cls + ' cam-ip-state"><span class="dot ' + l.dot + '"></span>' + esc(l.label) + '</span>';
  }
  /** Camera IP + live state. Elements are refreshed in place by refreshLive() when backend data changes. */
  function ipHtml(cam) {
    return '<span class="cam-ip" data-ip-cam="' + esc(cam.id) + '" title="Address the backend connects to, taken from the camera\'s source URL">' + ipInner(cam) + '</span>';
  }
  /** Update every IP / status element inside root from the latest backend data (no re-render). */
  function refreshLive(root) {
    const state = window.AV.state;
    (root || document).querySelectorAll('[data-ip-cam]').forEach(function (el) {
      const cam = state.getCamera(el.dataset.ipCam);
      if (!cam) return;
      const html = ipInner(cam);
      if (el.innerHTML !== html) el.innerHTML = html;
    });
    (root || document).querySelectorAll('[data-lc-cam]').forEach(function (el) {
      const cam = state.getCamera(el.dataset.lcCam);
      if (cam) el.textContent = cam.lastConnectedAt ? fmtTime(cam.lastConnectedAt) : 'Never';
    });
    (root || document).querySelectorAll('[data-err-cam]').forEach(function (el) {
      const cam = state.getCamera(el.dataset.errCam);
      if (!cam) return;
      el.hidden = !cam.lastError;
      if (el.tagName === 'DD') el.textContent = cam.lastError || '';
    });
    (root || document).querySelectorAll('[data-status-cam]').forEach(function (el) {
      const cam = state.getCamera(el.dataset.statusCam);
      if (!cam) return;
      const s = STATUS[camStatus(cam)];
      const html = '<span class="dot ' + s.dot + '"></span>' + s.label;
      el.className = 'badge ' + s.cls;
      if (el.innerHTML !== html) el.innerHTML = html;
    });
  }
  const SEV = { critical: 'badge-critical', high: 'badge-danger', medium: 'badge-warning', low: 'badge-info' };
  function sevBadge(sev) { return '<span class="badge ' + (SEV[sev] || 'badge-muted') + '">' + icon(sev === 'low' ? 'info' : 'alert', 12) + esc(cap(sev || 'unknown')) + '</span>'; }
  function sourceBadge(cam) {
    if (cam.source === 'backend') return '<span class="badge badge-info" title="Stored by the backend">' + icon('server', 12) + 'Backend</span>';
    return '<span class="badge badge-purple" title="Saved in this browser only — not persisted by a backend">' + icon('hardDrive', 12) + 'Local only</span>';
  }
  function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
  function eventType(type) { return window.AV.catalog.EVENT_TYPES[type] || { label: cap(String(type || 'event').replace(/_/g, ' ')), module: 'custom', severity: 'low', tone: 'info' }; }
  /** Display title of an event/alert/snapshot record: backend title ("Vehicle detected: Truck") or the type label. */
  function eventLabel(rec) { return (rec && rec.title) || eventType(rec && rec.type).label; }
  function toneBadge(tone, text) {
    const map = { success: 'badge-success', danger: 'badge-danger', warning: 'badge-warning', critical: 'badge-critical', info: 'badge-info', purple: 'badge-purple' };
    return '<span class="badge ' + (map[tone] || 'badge-muted') + '">' + esc(text) + '</span>';
  }
  function confText(rec) {
    if (rec.confidence === undefined || rec.confidence === null) return '<span class="dim">Not reported</span>';
    return Number(rec.confidence).toFixed(2);
  }
  function emptyState(iconName, title, text, actionsHtml) {
    return '<div class="empty"><div class="empty-icon">' + icon(iconName, 24) + '</div><h3>' + esc(title) + '</h3><p>' + esc(text) + '</p>' + (actionsHtml ? '<div class="row wrap" style="justify-content:center">' + actionsHtml + '</div>' : '') + '</div>';
  }
  function pager(total, page, size) {
    const pages = Math.max(1, Math.ceil(total / size));
    const from = total ? (page - 1) * size + 1 : 0, to = Math.min(total, page * size);
    return '<div class="pager"><span>Showing ' + from + '–' + to + ' of ' + total + '</span><div class="row">' +
      '<button class="btn btn-sm btn-ghost" data-page="' + (page - 1) + '" ' + (page <= 1 ? 'disabled' : '') + ' aria-label="Previous page">' + icon('chevronLeft', 16) + '</button>' +
      '<span>Page ' + page + ' / ' + pages + '</span>' +
      '<button class="btn btn-sm btn-ghost" data-page="' + (page + 1) + '" ' + (page >= pages ? 'disabled' : '') + ' aria-label="Next page">' + icon('chevronRight', 16) + '</button></div></div>';
  }
  function switchHtml(id, checked, label, opts) {
    opts = opts || {};
    return '<label class="switch ' + (opts.cls || '') + '" title="' + esc(opts.title || label) + '"><input type="checkbox" id="' + esc(id) + '" ' + (checked ? 'checked' : '') + ' ' + (opts.disabled ? 'disabled' : '') +
      ' aria-label="' + esc(label) + '" ' + (opts.attrs || '') + '><span class="track"></span>' + (opts.stateText ? '<span class="state-text">' + (checked ? 'On' : 'Off') + '</span>' : '') + '</label>';
  }
  function download(filename, content, mime) {
    const blob = content instanceof Blob ? content : new Blob([content], { type: mime || 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }
  /** Request fullscreen for an element with a toast fallback. */
  function fullscreen(el) {
    if (!el) return;
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    const fn = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!fn) { toast('warning', 'Full screen not supported', 'This browser does not support the Fullscreen API.'); return; }
    Promise.resolve(fn.call(el)).catch(function (e) { toast('error', 'Could not enter full screen', e && e.message); });
  }

  window.AV.ui = {
    esc: esc, $: $, $$: $$, h: h, fmtTime: fmtTime, relTime: relTime, debounce: debounce, throttle: throttle, uid: uid,
    toast: toast, modal: modal, confirm: confirmDialog, setBusy: setBusy, STATUS: STATUS, camStatus: camStatus,
    statusBadge: statusBadge, sevBadge: sevBadge, sourceBadge: sourceBadge, camAddress: camAddress, camLive: camLive, ipHtml: ipHtml, refreshLive: refreshLive, cap: cap, eventType: eventType,
    toneBadge: toneBadge, eventLabel: eventLabel, confText: confText, emptyState: emptyState, pager: pager, switchHtml: switchHtml, download: download,
    fullscreen: fullscreen, stopStreams: stopStreams, openModalCount: function () { return openModals.length; }
  };
})();
