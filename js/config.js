/* PYROTECH VISION — runtime configuration.
 * Edit these values (or set window.AIVISION_CONFIG before this file loads) to point the
 * frontend at your camera / inference backend. Operators can also override apiBase
 * from Settings → Backend connection (stored as a non-secret UI preference). */
(function () {
  'use strict';
  const defaults = {
    appName: 'AI VISION',
    // Base URL of the camera/inference backend REST API. Relative ('/api') when the
    // backend is served from the same origin; absolute ('http://10.0.0.5:8080/api') otherwise.
    apiBase: '/api',
    // Milliseconds before an ordinary API request is treated as timed out.
    requestTimeoutMs: 8000,
    // Connection tests can legitimately take longer (RTSP handshake + first frame decode).
    connectionTestTimeoutMs: 25000,
    // How often to poll /system/health and /system/metrics while connected (ms).
    healthPollMs: 30000,
    metricsPollMs: 5000,
    // How often to fetch new alerts / detections / snapshots from the backend (ms).
    eventPollMs: 5000,
    // How often to refresh camera status, IPs and running sessions from the backend (ms).
    cameraPollMs: 4000,
    // Page sizes for long lists.
    pageSize: 12,
    // Maximum simultaneous streams in Live View (protects low-spec control-room PCs).
    maxConcurrentStreams: 9
  };
  window.AV = window.AV || {};
  window.AV.config = Object.assign({}, defaults, window.AIVISION_CONFIG || {});
})();
