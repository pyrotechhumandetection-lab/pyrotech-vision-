/* PYROTECH VISION — Settings, profile dialog. */
(function () {
  'use strict';
  const ui = window.AV.ui, icon = window.AV.icon, state = window.AV.state, api = window.AV.api, cfg = window.AV.config;
  const esc = ui.esc;
  let root = null, unsub = [];

  function render(container) {
    root = container;
    const p = state.prefs();
    container.innerHTML =
      '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#/dashboard">' + icon('dashboard', 15) + '</a>' + icon('chevronRight', 14) + '<span class="current">Settings</span></nav>' +
      '<div class="page-head"><span class="head-icon">' + icon('settings', 24) + '</span><div><h1>Settings</h1><p class="sub">Backend connection, appearance and local configuration.</p></div></div>' +
      '<div class="settings-grid">' +
      '<section class="card" id="set-backend" aria-labelledby="sb-h"></section>' +
      '<section class="card" aria-labelledby="sa-h"><div class="card-head"><h2 id="sa-h">' + icon('sun', 18) + 'Appearance</h2></div><div class="stack">' +
      '<div class="field"><label for="st-theme">Theme</label><select class="select" id="st-theme"><option value="light"' + (p.theme !== 'dark' ? ' selected' : '') + '>Light (default)</option><option value="dark"' + (p.theme === 'dark' ? ' selected' : '') + '>Dark</option></select><span class="field-hint">The console always opens in light mode.</span></div>' +
      '<div class="field"><label for="st-motion">Animations</label><select class="select" id="st-motion"><option value="system"' + (p.motion !== 'reduce' ? ' selected' : '') + '>Follow system setting</option><option value="reduce"' + (p.motion === 'reduce' ? ' selected' : '') + '>Reduce motion</option></select></div>' +
      '<label class="check"><input type="checkbox" id="st-ov" ' + (p.showOverlays ? 'checked' : '') + '><span>Show detection-zone overlays on camera tiles</span></label></div></section>' +
      '<section class="card" aria-labelledby="sl-h"><div class="card-head"><h2 id="sl-h">' + icon('hardDrive', 18) + 'Local configuration</h2></div><div class="stack">' +
      '<p class="muted" style="font-size:13px;line-height:1.6">Without a backend, camera metadata is saved in this browser' + (window.AV.storage.isAvailable() ? '' : ' <b>(browser storage is blocked — data is kept in memory for this tab only)</b>') + '. <b>Passwords are never stored here</b>, and credentials embedded in RTSP URLs are stripped before saving.</p>' +
      '<div class="row wrap"><button class="btn btn-sm" id="st-export">' + icon('download', 14) + 'Export configuration</button>' +
      '<label class="btn btn-sm" for="st-import" tabindex="0" role="button" id="st-import-lbl">' + icon('upload', 14) + 'Import configuration</label><input type="file" id="st-import" accept="application/json,.json" class="sr-only">' +
      '<button class="btn btn-sm btn-danger" id="st-clear">' + icon('trash', 14) + 'Clear local data</button></div></div></section>' +
      '</div>';
    renderBackend();
    const q = function (s) { return container.querySelector(s); };
    q('#st-theme').addEventListener('change', function (e) { window.AV.app.setTheme(e.target.value); ui.toast('success', 'Theme updated'); });
    q('#st-motion').addEventListener('change', function (e) { state.setPref('motion', e.target.value); window.AV.app.applyMotion(); ui.toast('success', 'Animation preference saved'); });
    q('#st-ov').addEventListener('change', function (e) { state.setPref('showOverlays', e.target.checked); ui.toast('success', 'Overlay preference saved'); });
    q('#st-export').addEventListener('click', function () { ui.download('aivision-config-' + new Date().toISOString().slice(0, 10) + '.json', state.exportConfig(), 'application/json'); ui.toast('success', 'Configuration exported', 'Passwords are never included.'); });
    q('#st-import-lbl').addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); q('#st-import').click(); } });
    q('#st-import').addEventListener('change', function (e) {
      const file = e.target.files[0]; if (!file) return;
      const r = new FileReader();
      r.onload = function () { try { const n = state.importConfig(r.result); ui.toast('success', 'Configuration imported', n + ' camera(s) imported as "Not verified". Re-enter passwords when testing.'); } catch (err) { ui.toast('error', 'Import failed', err.message); } e.target.value = ''; };
      r.onerror = function () { ui.toast('error', 'Import failed', 'Could not read the file.'); };
      r.readAsText(file);
    });
    q('#st-clear').addEventListener('click', async function () {
      if (!await ui.confirm({ title: 'Clear all local data?', danger: true, confirmText: 'Clear local data', message: 'This deletes every locally saved camera, alert acknowledgement, note and preference in this browser.', detail: 'Export your configuration first if you may need it. Backend data is not affected.' })) return;
      state.clearAll(); window.AV.app.applyPrefs(); ui.toast('success', 'Local data cleared'); window.AV.router.go('/dashboard');
    });
    unsub.push(state.on('backend', renderBackend));
  }

  function renderBackend() {
    if (!root) return;
    const el = root.querySelector('#set-backend');
    const p = state.prefs(), st = api.status;
    el.innerHTML = '<div class="card-head"><h2 id="sb-h">' + icon('server', 18) + 'Backend connection</h2><span class="spacer"></span>' +
      (st.connected ? '<span class="badge badge-success"><span class="dot"></span>Connected</span>' : '<span class="badge badge-muted"><span class="dot"></span>Not connected</span>') + '</div>' +
      '<form class="stack" id="sb-form" novalidate><div class="field"><label for="sb-url">API base URL</label><input class="input mono-input" id="sb-url" value="' + esc(p.apiBase || cfg.apiBase) + '" placeholder="/api or http://10.0.0.5:8080/api" spellcheck="false">' +
      '<span class="field-hint">Default from js/config.js: <code class="mono">' + esc(cfg.apiBase) + '</code>. The console calls <code class="mono">GET {base}/system/health</code> and expects <code class="mono">{"status":"ok"}</code>.</span><span class="field-error" id="sb-err"></span></div>' +
      '<div class="row wrap"><button class="btn btn-primary btn-sm" type="submit">' + icon('plug', 14) + 'Save &amp; check connection</button><button class="btn btn-sm btn-ghost" type="button" id="sb-default">Use default</button></div></form>' +
      '<dl class="kv mt-16"><dt>Last check</dt><dd>' + esc(st.checkedAt ? ui.fmtTime(st.checkedAt) : 'Not yet') + '</dd><dt>Result</dt><dd>' + (st.connected ? 'Health OK' + (st.info && st.info.version ? ' · version ' + esc(st.info.version) : '') : esc(st.error || '—')) + '</dd>' +
      (location.protocol === 'file:' ? '<dt>Note</dt><dd>Opened from file:// — a relative API URL cannot work. Run <code class="mono">npm start</code> or use an absolute URL.</dd>' : '') + '</dl>';
    window.AV.icons.hydrate(el);
    el.querySelector('#sb-default').addEventListener('click', function () { el.querySelector('#sb-url').value = cfg.apiBase; });
    el.querySelector('#sb-form').addEventListener('submit', async function (e) {
      e.preventDefault();
      const v = el.querySelector('#sb-url').value.trim().replace(/\/+$/, '');
      const err = el.querySelector('#sb-err');
      if (!v || !(/^\/[^\s]*$/.test(v) || /^https?:\/\/[^\s/]+(\/[^\s]*)?$/i.test(v))) { err.textContent = 'Enter a path starting with "/" or an http(s):// URL.'; return; }
      err.textContent = '';
      state.setPref('apiBase', v === cfg.apiBase ? '' : v);
      const b = e.submitter || el.querySelector('[type=submit]');
      ui.setBusy(b, true, 'Checking…');
      await window.AV.app.checkBackend(true);
      if (b.isConnected) ui.setBusy(b, false);
      ui.toast(api.status.connected ? 'success' : 'warning', api.status.connected ? 'Backend connected' : 'Backend not reachable', api.status.connected ? api.base() : api.status.error);
      renderBackend();
    });
  }

  function destroy() { unsub.forEach(function (x) { x(); }); unsub = []; root = null; }

  function openProfile() {
    const p = state.prefs();
    ui.modal({ title: 'Profile information', icon: 'user', size: 'sm',
      body: '<form class="stack" id="pf" novalidate><div class="field"><label for="pf-name">Display name</label><input class="input" id="pf-name" maxlength="40" value="' + esc(p.profileName) + '"><span class="field-error" id="pf-err"></span></div>' +
        '<div class="field"><label for="pf-role">Role label</label><input class="input" id="pf-role" maxlength="40" value="' + esc(p.profileRole) + '"></div>' +
        '<p class="dim" style="font-size:12px">Used to attribute alert acknowledgements and notes on this console. User accounts and permissions require backend authentication.</p></form>',
      actions: [{ label: 'Cancel', cls: 'btn-ghost' }, { label: 'Save', cls: 'btn-primary', onClick: function (m) {
        const n = m.body.querySelector('#pf-name').value.trim();
        if (!n) { m.body.querySelector('#pf-err').textContent = 'Name is required.'; return false; }
        state.setPref('profileName', n); state.setPref('profileRole', m.body.querySelector('#pf-role').value.trim() || 'Operator');
        window.AV.app.applyPrefs(); ui.toast('success', 'Profile updated');
      } }] });
  }

  window.AV.pages = window.AV.pages || {};
  window.AV.pages.settings = { title: 'Settings', render: render, destroy: destroy };
  window.AV.settingsUi = { openProfile: openProfile };
})();
