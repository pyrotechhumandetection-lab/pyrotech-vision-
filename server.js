/*
 * PYROTECH VISION — Network-accessible development server.
 *
 * Start:
 * npm run dev
 *
 * Default:
 * HOST=0.0.0.0
 * PORT=5173
 *
 * Backend:
 * BACKEND_URL=http://127.0.0.1:8000/api
 *
 * Access from the host PC:
 * http://localhost:5173
 *
 * Access from another PC:
 * http://<HOST_PC_IPV4>:5173
 *
 * IMPORTANT:
 * 0.0.0.0 allows connections on the host PC's network interfaces.
 * It does not assign an IP address or configure network routing.
 */

'use strict';

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const ROOT = __dirname;

// --------------------------------------------------
// Load .env without external dependencies
// --------------------------------------------------

(function loadDotEnv() {
  let content;

  try {
    content = fs.readFileSync(path.join(ROOT, '.env'), 'utf8');
  } catch {
    return;
  }

  content.split(/\r?\n/).forEach((line) => {
    const match = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);

    if (!match || line.trim().startsWith('#')) {
      return;
    }

    const key = match[1];
    let value = match[2];

    if (
      value.length >= 2 &&
      (value[0] === '"' || value[0] === "'") &&
      value[value.length - 1] === value[0]
    ) {
      value = value.slice(1, -1);
    }

    if (process.env[key] === undefined || process.env[key] === '') {
      process.env[key] = value;
    }
  });
})();

// --------------------------------------------------
// Configuration
// --------------------------------------------------

const PORT = Number.parseInt(process.env.PORT || '5173', 10);

// Listen on all IPv4 network interfaces by default.
const HOST = process.env.HOST || '0.0.0.0';

const BACKEND_SETTING = (
  process.env.BACKEND_URL || 'http://127.0.0.1:8000/api'
).trim();

const BACKEND = /^(off|none|false|0)$/i.test(BACKEND_SETTING)
  ? null
  : new URL(BACKEND_SETTING);

// Only serve approved frontend files.
const PUBLIC =
  /^\/(index\.html|favicon\.ico|(css|js|assets)\/[^\s]+)$/;

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.md': 'text/markdown; charset=utf-8',
};

// --------------------------------------------------
// Response helper
// --------------------------------------------------

function send(res, statusCode, body, contentType) {
  res.writeHead(statusCode, {
    'Content-Type': contentType || 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });

  res.end(body);
}

// --------------------------------------------------
// Backend API proxy
// --------------------------------------------------

function proxy(req, res) {
  if (!BACKEND) {
    if (req.url.split('?')[0] === '/api/system/health') {
      return send(
        res,
        200,
        JSON.stringify({
          status: 'unavailable',
          message: 'No backend configured.',
        })
      );
    }

    return send(
      res,
      503,
      JSON.stringify({
        status: 'unavailable',
        code: 'BACKEND_UNAVAILABLE',
        message: 'Backend is disabled.',
      })
    );
  }

  const subPath = req.url.replace(/^\/api/, '') || '/';

  const target = new URL(
    BACKEND.pathname.replace(/\/$/, '') + subPath,
    BACKEND
  );

  const transport = target.protocol === 'https:' ? https : http;

  const headers = Object.assign({}, req.headers, {
    host: target.host,
  });
  // Hop-by-hop headers belong to the browser connection, not to the backend connection.
  ['connection', 'keep-alive', 'proxy-connection'].forEach((h) => delete headers[h]);

  const isStream = /\/stream\.mjpeg(\?|$)/.test(req.url);

  const upstream = transport.request(
    target,
    {
      method: req.method,
      headers,
      // One backend connection per request: Node >= 19 reuses sockets by default, and a socket the
      // backend has already closed then fails with ECONNRESET (seen as a random 502 in the UI).
      agent: false,
      timeout: isStream ? 0 : 60000,
    },
    (backendResponse) => {
      res.writeHead(backendResponse.statusCode, backendResponse.headers);
      backendResponse.pipe(res);
    }
  );

  upstream.on('timeout', () => {
    upstream.destroy(new Error('Backend request timed out'));
  });

  res.on('close', () => {
    if (!res.writableFinished) {
      upstream.destroy();
    }
  });

  upstream.on('error', (error) => {
    if (res.destroyed || res.writableEnded) {
      return;
    }

    if (
      !res.headersSent &&
      req.url.split('?')[0] === '/api/system/health'
    ) {
      return send(
        res,
        200,
        JSON.stringify({
          status: 'unavailable',
          message: `Backend is not running (${error.code || 'ERROR'}).`,
        })
      );
    }

    if (!res.headersSent) {
      send(
        res,
        502,
        JSON.stringify({
          status: 'error',
          code: 'BACKEND_UNAVAILABLE',
          message: `Backend is not reachable (${error.code || 'ERROR'}).`,
        })
      );
    } else {
      res.end();
    }
  });

  req.pipe(upstream);
}

// --------------------------------------------------
// HTTP server
// --------------------------------------------------

const server = http.createServer((req, res) => {
  // Forward API requests to the Python backend.
  if (req.url.startsWith('/api/') || req.url === '/api') {
    return proxy(req, res);
  }

  let relativePath;

  try {
    relativePath = decodeURIComponent(
      new URL(req.url, 'http://localhost').pathname
    );
  } catch {
    return send(res, 400, 'Bad request', 'text/plain');
  }

  if (relativePath === '/') {
    relativePath = '/index.html';
  }

  relativePath = relativePath.replace(/\\/g, '/');

  if (!PUBLIC.test(relativePath)) {
    return send(res, 404, 'Not found', 'text/plain');
  }

  const filePath = path.resolve(ROOT, '.' + relativePath);

  // Prevent paths outside the project root.
  if (!filePath.startsWith(ROOT + path.sep)) {
    return send(res, 403, 'Forbidden', 'text/plain');
  }

  const relativeFile = path.relative(ROOT, filePath);

  if (
    /(^|[\\/])(server\.js|package\.json|node_modules)([\\/]|$)/.test(
      relativeFile
    )
  ) {
    return send(res, 404, 'Not found', 'text/plain');
  }

  fs.stat(filePath, (error, stats) => {
    if (error || !stats.isFile()) {
      return send(res, 404, 'Not found', 'text/plain');
    }

    res.writeHead(200, {
      'Content-Type':
        TYPES[path.extname(filePath).toLowerCase()] ||
        'application/octet-stream',
      'Cache-Control': 'no-cache',
      'X-Content-Type-Options': 'nosniff',
    });

    fs.createReadStream(filePath).pipe(res);
  });
});

// --------------------------------------------------
// Start server
// --------------------------------------------------

server.on('error', (error) => {
  console.error('PYROTECH VISION server failed to start:', error.message);

  if (error.code === 'EADDRINUSE') {
    console.error(`Port ${PORT} is already in use.`);
  }

  if (error.code === 'EADDRNOTAVAIL') {
    console.error(`Host address ${HOST} is not available.`);
  }

  process.exitCode = 1;
});

server.listen(PORT, HOST, () => {
  console.log('');
  console.log('======================================');
  console.log('PYROTECH VISION FRONTEND');
  console.log('======================================');
  console.log(`Listening host : ${HOST}`);
  console.log(`Port : ${PORT}`);
  console.log(`Local URL : http://localhost:${PORT}`);
  console.log(`Backend URL : ${BACKEND ? BACKEND.href : 'Disabled'}`);
  console.log('');
  console.log('Access from another PC using:');
  console.log(`http://<HOST-PC-IPV4>:${PORT}`);
  console.log('======================================');
  console.log('');
});
