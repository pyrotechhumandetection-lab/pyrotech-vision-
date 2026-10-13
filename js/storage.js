/* PYROTECH VISION — local persistence (non-sensitive data only).
 * Passwords are NEVER written here. RTSP URLs have embedded user-info stripped before saving. */
(function () {
  'use strict';
  const KEY = 'aivision:v1';
  let memoryFallback = null; // used when localStorage is blocked (private mode, policies)
  let available = true;

  function read() {
    try {
      const raw = window.localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      available = false;
      return memoryFallback;
    }
  }

  function write(obj) {
    const clean = scrub(obj);
    try {
      window.localStorage.setItem(KEY, JSON.stringify(clean));
      return true;
    } catch (e) {
      available = false;
      memoryFallback = clean;
      return false;
    }
  }

  function clear() {
    try { window.localStorage.removeItem(KEY); } catch (e) { /* ignore */ }
    memoryFallback = null;
  }

  /** Remove "user:pass@" from a URL string. Returns {url, hadCredentials}. */
  function stripUrlCredentials(url) {
    if (typeof url !== 'string') return { url: url, hadCredentials: false };
    const m = url.match(/^([a-z][a-z0-9+.-]*:\/\/)([^@\/?#]*@)(.*)$/i);
    if (!m) return { url: url, hadCredentials: false };
    return { url: m[1] + m[3], hadCredentials: true };
  }

  /** Mask credentials for display / logs: rtsp://***:***@host/... */
  function redactUrl(url) {
    if (typeof url !== 'string') return '';
    return url.replace(/^([a-z][a-z0-9+.-]*:\/\/)([^@\/?#]*)@/i, '$1***:***@');
  }

  /** Deep-copy and remove anything that must never be persisted in the browser. */
  function scrub(obj) {
    const copy = JSON.parse(JSON.stringify(obj || {}));
    if (Array.isArray(copy.cameras)) {
      copy.cameras.forEach(function (c) {
        delete c.password;
        if (c.sourceUrl) {
          const s = stripUrlCredentials(c.sourceUrl);
          c.sourceUrl = s.url;
          if (s.hadCredentials) c.urlHadCredentials = true;
        }
      });
    }
    return copy;
  }

  window.AV = window.AV || {};
  window.AV.storage = {
    read: read, write: write, clear: clear,
    stripUrlCredentials: stripUrlCredentials, redactUrl: redactUrl,
    isAvailable: function () { return available; }
  };
})();
