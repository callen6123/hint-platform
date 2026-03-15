(function(){
  const $ = (id) => document.getElementById(id);
  const logEl = $("log");
  const statusTbody = $("statusTbody");

  // Cached device list from /ui/status for UI controls
  let _devicesCache = [];

  // Device Settings panel (bottom of dashboard; same ids as speak page)
  const deviceSel = $("device");
  const friendlyNameInput = $("friendlyName");
  const saveDeviceCfgBtn = $("saveDeviceCfgBtn");
  const devCfgStatus = $("devCfgStatus");
  const deviceHint = $("deviceHint");

    // Prevent refresh from overwriting while the user is typing
  let _friendlyNameDirty = false;
  let _friendlyNameDirtyForDevice = '';

  // Device Actions controls (optional; only present if dashboard.html includes them)
  const actionDeviceSelect = $("actionDeviceSelect");
  const beepBtn = $("beepBtn");
  const speakerTestBtn = $("speakerTestBtn");

  // Optional: Hub log + runbook controls (only present if dashboard.html includes them)
  const hubCmdLogBtn = $("hubCmdLogBtn");
  const runbookBtn = $("runbookBtn");
  const hubCmdLogPanel = $("hubCmdLogPanel");
  const hubCmdLogPre = $("hubCmdLogPre");
  const hubCmdLogRefreshBtn = $("hubCmdLogRefreshBtn");
  const hubCmdLogCloseBtn = $("hubCmdLogCloseBtn");

  // Optional: Account controls (only present if dashboard.html includes them)
  const accountBtn = $("accountBtn");
  const logoutBtn = $("logoutBtn");
  const accountPanel = $("accountPanel");
  const accountClose = $("accountClose");
  const accountUser = $("accountUser");
  const accountRole = $("accountRole");
  const adminGmSection = $("adminGmSection");

  const myPwCurrent = $("myPwCurrent");
  const myPwNew = $("myPwNew");
  const myPwSave = $("myPwSave");
  const myPwMsg = $("myPwMsg");

  const gmUsername = $("gmUsername");
  const gmPassword = $("gmPassword");
  const gmSet = $("gmSet");
  const gmMsg = $("gmMsg");

  function populateActionDeviceSelect(devices){
    if (!actionDeviceSelect) return;

    const list = Array.isArray(devices) ? devices.slice() : [];
    list.sort((a,b) => {
      const an = String((a && (a.friendly_name || a.device_id)) || '');
      const bn = String((b && (b.friendly_name || b.device_id)) || '');
      return an.localeCompare(bn);
    });

    const prev = actionDeviceSelect.value;
    actionDeviceSelect.innerHTML = '';

    if (!list.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'No devices';
      actionDeviceSelect.appendChild(opt);
      actionDeviceSelect.disabled = true;
      if (beepBtn) beepBtn.disabled = true;
      if (speakerTestBtn) speakerTestBtn.disabled = true;
      return;
    }

    actionDeviceSelect.disabled = false;
    if (beepBtn) beepBtn.disabled = false;
    if (speakerTestBtn) speakerTestBtn.disabled = false;

    for (const d of list) {
      const did = String((d && d.device_id) || '');
      if (!did) continue;
      const label = String((d && d.friendly_name) || did);
      const opt = document.createElement('option');
      opt.value = did;
      opt.textContent = label;
      actionDeviceSelect.appendChild(opt);
    }

    // Restore previous selection if still present
    if (prev && Array.from(actionDeviceSelect.options).some(o => o.value === prev)) {
      actionDeviceSelect.value = prev;
    }
  }

  async function runDeviceAction(path, deviceId){
    if (!deviceId) throw new Error('device_id_required');
    return api(path, 'POST', { device_id: deviceId });
  }

  // Cached hub health snapshot from /ui/health
  let _hubHealth = null;
  let _hubHealthLoadedAt = 0;

  // Populate the Hub Health panel (separate DOM block in dashboard.html)
  function setTextIfEl(id, text){
    const el = $(id);
    if (!el) return;
    el.textContent = (text == null || text === '') ? '—' : String(text);
  }

  function pillTextFromOk(ok){
    if (ok === true) return 'OK';
    if (ok === false) return 'ERR';
    return '—';
  }

  function updateHubHealthPanel(health){
    // Expected shapes:
    //  - {hub_health:{ok:true, checks:{...}}, audio_cache:{...}, ...}
    //  - {ok:true, checks:{...}} (legacy)

    if (!health || typeof health !== 'object') {
      setTextIfEl('hubHealthOverall', '—');
      setTextIfEl('hubHealthMqtt', '—');
      setTextIfEl('hubHealthAudio', '—');
      setTextIfEl('hubHealthElevenlabs', '—');
      setTextIfEl('hubHealthErr', '');
      return;
    }

    const hubH = (health.hub_health && typeof health.hub_health === 'object') ? health.hub_health : health;
    const checks = (hubH.checks && typeof hubH.checks === 'object') ? hubH.checks : (health.checks && typeof health.checks === 'object' ? health.checks : null);

    const overallOk = (typeof hubH.ok === 'boolean') ? hubH.ok : (typeof health.ok === 'boolean' ? health.ok : null);

    const mqttOk = checks && checks.mqtt_tcp ? checks.mqtt_tcp.ok : null;
    const audioOk = checks && checks.audio_server ? checks.audio_server.ok : null;
    const elOk = checks && checks.elevenlabs_api ? checks.elevenlabs_api.ok : null;

    setTextIfEl('hubHealthOverall', pillTextFromOk(overallOk));
    setTextIfEl('hubHealthMqtt', pillTextFromOk(mqttOk));
    setTextIfEl('hubHealthAudio', pillTextFromOk(audioOk));
    setTextIfEl('hubHealthElevenlabs', pillTextFromOk(elOk));

    // Optional error detail line
    let err = '';
    if (checks) {
      const e1 = checks.mqtt_tcp && checks.mqtt_tcp.error ? `MQTT: ${checks.mqtt_tcp.error}` : '';
      const e2 = checks.audio_server && checks.audio_server.error ? `Audio: ${checks.audio_server.error}` : '';
      const e3 = checks.elevenlabs_api && checks.elevenlabs_api.error ? `ElevenLabs: ${checks.elevenlabs_api.error}` : '';
      err = [e1, e2, e3].filter(Boolean).join(' | ');
    }
    setTextIfEl('hubHealthErr', err);
  }
  function fmtBytes(n){
  n = Number(n||0);
  if (!isFinite(n)) return '0';
  const u = ['B','KB','MB','GB'];
  let i = 0;
  while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return `${n.toFixed(i===0?0:1)} ${u[i]}`;
}

async function loadAudioStatus(){
  const audioErr = $("audioErr");
  const audioDir = $("audioDir");
  const audioFiles = $("audioFiles");
  const audioBytes = $("audioBytes");
  const audioOldest = $("audioOldest");
  const audioPolicy = $("audioPolicy");

  // If the dashboard doesn't have the audio panel, don't error.
  if (!audioErr && !audioDir && !audioFiles && !audioBytes && !audioOldest && !audioPolicy) return;

  try {
    const r = await fetch('/ui/audio/status');
    const j = await r.json();
    if (!j || !j.ok) {
      if (audioErr) audioErr.textContent = (j && j.error) ? String(j.error) : 'Error loading audio status.';
      return;
    }

    if (audioErr) audioErr.textContent = '';
    if (audioDir) audioDir.textContent = j.cache_dir || '—';
    if (audioFiles) audioFiles.textContent = String((j.stats && j.stats.files) || 0);
    if (audioBytes) audioBytes.textContent = fmtBytes((j.stats && j.stats.bytes) || 0);
    if (audioOldest) audioOldest.textContent = (j.stats && j.stats.oldest_mtime) ? String(j.stats.oldest_mtime) : '—';

    const pol = j.policy || {};
    if (audioPolicy) {
      audioPolicy.textContent = `ret=${pol.retention_hours}h, maxFiles=${pol.max_files}, maxBytes=${fmtBytes(pol.max_bytes)}`;
    }
  } catch {
    if (audioErr) audioErr.textContent = 'Error loading audio status.';
  }
}

  function log(msg){
    const ts = new Date().toISOString();
    if (!logEl) return;
    logEl.textContent += `[${ts}] ${msg}\n`;
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function api(path, method, body){
    const opts = {
      method,
      headers: {
        'Content-Type': 'application/json'
      }
    };
    if (body) opts.body = JSON.stringify(body);

    const res = await fetch(path, opts);
    const txt = await res.text();
    let data;
    try { data = JSON.parse(txt); } catch { data = { raw: txt }; }
    if (!res.ok) {
      throw new Error(`${res.status} ${res.statusText}: ${JSON.stringify(data)}`);
    }
    return data;
  }

  async function loadMe(){
    const me = await api('/ui/me', 'GET');
    if (accountUser) accountUser.textContent = me.username || '—';
    if (accountRole) accountRole.textContent = me.role || '—';
    const isAdmin = String(me.role || '').toLowerCase() === 'admin';
    if (adminGmSection) adminGmSection.style.display = isAdmin ? '' : 'none';
    return me;
  }

  function showAccountPanel(show){
    if (!accountPanel) return;
    accountPanel.style.display = show ? '' : 'none';
    if (show) {
      if (myPwMsg) myPwMsg.textContent = '';
      if (gmMsg) gmMsg.textContent = '';
      if (myPwCurrent) myPwCurrent.value = '';
      if (myPwNew) myPwNew.value = '';
      if (gmPassword) gmPassword.value = '';
    }
  }

  async function fetchText(path){
    const res = await fetch(path, { method: 'GET' });
    const txt = await res.text();
    if (!res.ok) {
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    return txt;
  }

  function showHubCmdLogPanel(show){
    if (!hubCmdLogPanel) return;
    hubCmdLogPanel.style.display = show ? '' : 'none';
  }

  async function loadHubCmdLog(lines){
    const n = Number(lines || 200);
    const q = isFinite(n) && n > 0 ? n : 200;
    const txt = await fetchText(`/ui/logs/hub_cmd?lines=${encodeURIComponent(String(q))}`);
    if (hubCmdLogPre) {
      hubCmdLogPre.textContent = txt;
    } else {
      // Fallback: dump into the main log area
      log(`hub_cmd.log (last ${q} lines):\n${txt}`);
    }
  }

  function statusClass(s){
    return (s === 'online') ? 'status-online' : 'status-offline';
  }

  function fmt(v){
    return v == null ? '' : String(v);
  }

  function fmtAgeSeconds(sec){
    sec = Number(sec);
    if (!isFinite(sec) || sec < 0) return '';
    if (sec < 5) return 'just now';
    if (sec < 60) return `${Math.floor(sec)}s ago`;
    const m = sec / 60;
    if (m < 60) return `${Math.floor(m)}m ago`;
    const h = m / 60;
    if (h < 48) return `${Math.floor(h)}h ago`;
    const d = h / 24;
    return `${Math.floor(d)}d ago`;
  }

  function isOnline(d){
    if (typeof d.online === 'boolean') return d.online;
    const s = String(d.status || '').toLowerCase();
    return s === 'online';
  }

  function normStatus(d){
    return isOnline(d) ? 'online' : 'offline';
  }

  function offlineReasonLabel(r){
    r = String(r || '').toLowerCase();
    if (r === 'never_seen') return 'Never seen';
    if (r === 'clock_skew') return 'Clock skew';
    if (r === 'stale_last_seen') return 'Stale last_seen';
    return r ? r : 'Offline';
  }

  function offlineDetail(d){
    const reason = offlineReasonLabel(d.offline_reason);
    const ageSec = (typeof d.last_seen_age_sec === 'number') ? d.last_seen_age_sec : null;
    const ageStr = (ageSec != null) ? fmtAgeSeconds(ageSec) : '';
    if (ageStr) return `${reason} (${ageStr})`;
    return reason;
  }

  function esc(s){
    return String(s == null ? '' : s)
      .replaceAll('&','&amp;')
      .replaceAll('<','&lt;')
      .replaceAll('>','&gt;')
      .replaceAll('"','&quot;')
      .replaceAll("'",'&#39;');
  }

  function pill(text){
    return `<span class="pill">${esc(text)}</span>`;
  }

    function buildAudioTopic(deviceId){
    const did = String(deviceId || '').trim();
    return did
      ? `site/${(window.HUB_SITE_ID || 'demo')}/device/${did}/audio/level`
      : `site/${(window.HUB_SITE_ID || 'demo')}/device/<device_id>/audio/level`;
  }

  function renderMqttPanelFromStatus(data){
    const sel = $("mqttDeviceSelect");
    const topicEl = $("mqttTopicValue");
    const copyMsg = $("mqttCopyMsg");
    const copyBtn = $("mqttCopyTopicBtn");
    if (!sel || !topicEl || !copyBtn) return;

    const rows = Array.isArray(data && data.devices) ? data.devices : [];
    const current = sel.value || '';

    const options = rows
      .map((rec) => {
        const did = String((rec && rec.device_id) || '').trim();
        if (!did) return null;
        const label = String((rec && (rec.friendly_name || rec.device_name || rec.device_id)) || did).trim() || did;
        return { did, label };
      })
      .filter(Boolean)
      .sort((a, b) => a.label.localeCompare(b.label) || a.did.localeCompare(b.did));

    sel.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = options.length ? '-- select device --' : '-- no devices --';
    sel.appendChild(placeholder);

    for (const opt of options) {
      const el = document.createElement('option');
      el.value = opt.did;
      el.textContent = opt.label;
      el.dataset.deviceId = opt.did;
      sel.appendChild(el);
    }

    if (current && options.some(o => o.did === current)) {
      sel.value = current;
    } else if (!sel.value && options.length === 1) {
      sel.value = options[0].did;
    }

    topicEl.textContent = buildAudioTopic(sel.value);
    if (copyMsg) copyMsg.textContent = '';
  }

  function renderStatusPanel(data){
    if (!statusTbody) return;

    const devices = Array.isArray(data && data.devices) ? data.devices : [];
    const otaLatest = data ? (data.ota_latest_version || null) : null;

    // HUB status + version
    const hubH = (data && typeof data === 'object') ? (data.hub_health || null) : null;
    const hubOk = !!(hubH && hubH.ok);
    const hubStatus = hubH ? (hubOk ? 'OK' : 'ERR') : '—';
    const hubVer = (data && data.hub_version) ? String(data.hub_version) : '—';

    const rows = [];

    const hubUpdAvail = (data && typeof data.hub_update_available === 'boolean') ? data.hub_update_available : null;
    const hubUpdText = (hubUpdAvail === true) ? 'Update Available' : (hubUpdAvail === false ? 'Up To Date' : '—');
    const hubUpdCls = (hubUpdAvail === true) ? 'status-offline' : (hubUpdAvail === false ? 'status-online' : 'muted');

    rows.push(
      `<tr>`+
        `<td><strong>Hub</strong></td>`+
        `<td>${pill(hubStatus)}</td>`+
        `<td><span class="muted">${esc(hubVer)}</span></td>`+
        `<td><span class="${hubUpdCls}">${esc(hubUpdText)}</span></td>`+
        `<td><button id="hubUpdate">Update Hub</button></td>`+
        `<td></td>`+ // Speak column (empty for Hub)
        `<td></td>`+
      `</tr>`
    );

    for (const d of devices){
      const did = String((d && d.device_id) || '');
      const name = String((d && (d.friendly_name || d.device_id)) || 'Device');
      const st = normStatus(d); // 'online' / 'offline'

      const avRaw = String((d && d.agent_version) || '(unknown)');
      const needsUpdate = otaLatest && avRaw && (avRaw !== '(unknown)') && (avRaw !== String(otaLatest));
      const av = needsUpdate ? `${avRaw} → ${otaLatest}` : avRaw;

      const updText = needsUpdate ? 'Update Available' : 'Up To Date';
      const updCls = needsUpdate ? 'status-offline' : 'status-online';

      rows.push(
        `<tr>`+
          `<td><strong>${esc(name)}</strong></td>`+
          `<td><span class="${statusClass(st)}">${esc(st)}</span></td>`+
          `<td><span class="muted">${esc(av)}</span></td>`+
          `<td><span class="${updCls}">${esc(updText)}</span></td>`+
          `<td>`+
            (did ? `<button data-action="buildUpdate" data-device="${esc(did)}">Build+Update</button>` : '')+
          `</td>`+
          `<td>`+
            (did ? `<a class="btn" href="/speak/${esc(did)}" target="_blank" rel="noopener noreferrer">Speak</a>` : '')+
          `</td>`+
          `<td>`+
            (did ? `<button data-action="beep" data-device="${esc(did)}">Beep</button>` : '')+
          `</td>`+
        `</tr>`
      );
    }

    statusTbody.innerHTML = rows.join('') || '<tr><td colspan="7" class="muted">No devices.</td></tr>';
  }

  function setDevCfgStatus(s){
    if (!devCfgStatus) return;
    devCfgStatus.textContent = s || '';
  }

  function syncFriendlyNameFromSelection(force){
    if (!deviceSel) return;
    const did = String(deviceSel.value || '').trim();
    const d = _devicesCache.find(x => String((x && x.device_id) || '') === did) || null;

    // If user is actively editing for this device, don't overwrite.
    const isEditing = (friendlyNameInput && document.activeElement === friendlyNameInput);
    if (!force && isEditing && _friendlyNameDirty && _friendlyNameDirtyForDevice === did) {
      if (deviceHint) deviceHint.textContent = did ? `Device ID: ${did}` : '';
      return;
    }

    if (friendlyNameInput) {
      friendlyNameInput.value = (d && d.friendly_name) ? String(d.friendly_name) : '';
    }

    if (deviceHint) {
      deviceHint.textContent = did ? `Device ID: ${did}` : '';
    }
  }

  function populateDeviceSettingsPanel(devices){
    if (!deviceSel) return;

    const list = Array.isArray(devices) ? devices.slice() : [];
    const prev = String(deviceSel.value || '');

    deviceSel.innerHTML = '';

    if (!list.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'No devices';
      deviceSel.appendChild(opt);
      deviceSel.disabled = true;
      if (friendlyNameInput) friendlyNameInput.value = '';
      if (deviceHint) deviceHint.textContent = 'No devices paired yet.';
      if (saveDeviceCfgBtn) saveDeviceCfgBtn.disabled = true;
      return;
    }

    // Sort by friendly name then device_id
    list.sort((a,b) => {
      const an = String((a && (a.friendly_name || a.device_id)) || '');
      const bn = String((b && (b.friendly_name || b.device_id)) || '');
      return an.localeCompare(bn);
    });

    for (const d of list) {
      const did = String((d && d.device_id) || '');
      if (!did) continue;
      const fn = d && d.friendly_name ? String(d.friendly_name).trim() : '';
      const st = normStatus(d);

      const opt = document.createElement('option');
      opt.value = did;
      opt.textContent = fn ? `${fn} — ${did} (${st})` : `${did} (${st})`;
      deviceSel.appendChild(opt);
    }

    deviceSel.disabled = false;
    if (saveDeviceCfgBtn) saveDeviceCfgBtn.disabled = false;

    // Restore previous selection if still present
    if (prev && Array.from(deviceSel.options).some(o => o.value === prev)) {
      deviceSel.value = prev;
    }

    syncFriendlyNameFromSelection(false);
  }

  function hubHealthLabel(h){
    h = String(h || '').toLowerCase();
    if (h === 'ok') return 'Hub OK';
    if (h === 'degraded') return 'Hub degraded';
    if (h === 'error') return 'Hub error';
    return 'Hub unknown';
  }

  function warningLabel(w){
    w = String(w || '').toLowerCase();
    if (w === 'hub_unreachable') return 'Hub unreachable';
    if (w === 'device_offline') return 'Device offline';
    return w ? w : '';
  }

  function renderWarnings(d){
    const parts = [];

    // Explicit warning from hub
    const w = warningLabel(d.warning);
    if (w) parts.push(`<div class="warn">⚠ ${esc(w)}</div>`);

    // Hub health (derived)
    const hh = String(d.hub_health || '').toLowerCase();
    if (hh && hh !== 'ok') {
      parts.push(`<div class="warn">⚠ ${esc(hubHealthLabel(hh))}</div>`);
    }

    // Last state details (optional)
    const ls = d.last_state;
    if (ls && typeof ls === 'object') {
      // If we have a hub error string, show it
      const herr = ls.hub_last_err;
      if (herr) parts.push(`<div class="muted">hub err: ${esc(herr)}</div>`);

      // If we have last-ok timestamp, show age if present
      if (typeof ls.hub_last_ok_age_sec === 'number') {
        const age = fmtAgeSeconds(ls.hub_last_ok_age_sec);
        if (age) parts.push(`<div class="muted">hub ok: ${esc(age)}</div>`);
      }
    }

    return parts.join('');
  }

  function speakStateLabel(s){
    s = String(s || '').toLowerCase();
    if (!s) return '—';
    if (s === 'generated') return 'Generated';
    if (s === 'sent') return 'Sent';
    if (s === 'accepted') return 'Accepted';
    if (s === 'completed') return 'Completed';
    if (s === 'error') return 'Error';
    return s;
  }

  function renderSpeakBlock(d){
    const ls = d && d.last_speak;
    if (!ls || !ls.command_id) return '';

    const state = speakStateLabel(ls.state || 'sent');
    const cmdId = String(ls.command_id);

    let html = `<div class="muted" style="margin-top:6px;">Speak: ${esc(state)}<span class="muted"> (${esc(cmdId)})</span></div>`;

    // Stalled speak detection surfaced from /ui/status
    if (d && d.speak_stalled) {
      const reason = String(d.speak_stalled_reason || 'stalled');
      const ageSec = (typeof d.speak_stalled_age_sec === 'number') ? d.speak_stalled_age_sec : null;
      const ageStr = (ageSec != null) ? fmtAgeSeconds(ageSec) : '';
      const ageTxt = ageStr ? ` (${esc(ageStr)})` : '';

      html += `
        <div class="warn" style="margin-top:6px;">
          ⚠ Speak stalled: ${esc(reason)}${ageTxt}
          <div class="row-actions" style="margin-top:6px;">
            <button data-action="speakStop" data-device="${esc(d.device_id)}" data-cmd="${esc(cmdId)}">Stop</button>
            <button data-action="speakRetry" data-device="${esc(d.device_id)}" data-cmd="${esc(cmdId)}">Retry</button>
          </div>
        </div>
      `;
    }

    return html;
  }

  function lastCmd(d){
    const cid = fmt(d.last_command_id || '');
    const st = fmt(d.last_command_status || '');
    if (!cid && !st) return '';
    return `${cid}${st ? ' (' + st + ')' : ''}`;
  }

  function lastResult(d){
    const det = d.last_command_details || null;
    if (!det || typeof det !== 'object') return '';
    const r = fmt(det.result || '');
    const fv = fmt(det.from_version || '');
    const tv = fmt(det.to_version || '');
    if (!r && !fv && !tv) return '';
    if (fv || tv) {
      return `${r}${fv || tv ? ' ' + fv + '→' + tv : ''}`.trim();
    }
    return r;
  }

  function renderBanner(info){
    const b = $("banner");
    if (!b) return;
    if (!info) {
      b.style.display = 'none';
      b.innerHTML = '';
      return;
    }

    const parts = [];
    // if (info.hubHealth) {
    //   const hh = String(info.hubHealth).toLowerCase();
    //   if (hh === 'ok') parts.push(`<div><b>Hub:</b> <span class="pill">OK</span></div>`);
    //   else if (hh === 'error') parts.push(`<div><b>Hub:</b> <span class="pill">ERROR</span> <span class="muted">(unreachable)</span></div>`);
    //   else parts.push(`<div><b>Hub:</b> <span class="muted">unknown</span></div>`);
    // }
    // if (info.otaLatest) {
    //   parts.push(`<div><b>Device OTA:</b> latest <span class="pill">${info.otaLatest}</span></div>`);
    // } else {
    //   parts.push(`<div><b>Device OTA:</b> <span class="muted">no bundle built yet</span></div>`);
    // }

    // if (info.hubVer) {
    //   const hv = `<span class="pill">${info.hubVer}</span>`;
    //   if (info.hubUpd === true) {
    //     const hl = info.hubLatest ? `<span class="pill">${info.hubLatest}</span>` : `<span class="muted">(unknown)</span>`;
    //     parts.push(`<div><b>Hub:</b> ${hv} → ${hl} <span class="muted">(update available)</span></div>`);
    //   } else if (info.hubUpd === false) {
    //     parts.push(`<div><b>Hub:</b> ${hv} <span class="muted">(up to date)</span></div>`);
    //   } else {
    //     parts.push(`<div><b>Hub:</b> ${hv} <span class="muted">(update status unknown)</span></div>`);
    //   }
    // } else {
    //   parts.push(`<div><b>Hub:</b> <span class="muted">version unknown</span></div>`);
    // }

    // if (info.hubErr) {
    //   parts.push(`<div class="muted">Hub update check error: ${String(info.hubErr)}</div>`);
    // }

    // Optional hub health panel (if we have /ui/health)
    if (info.hubHealthDetail && typeof info.hubHealthDetail === 'object') {
      const hd = info.hubHealthDetail;
      const overall = String(hd.ok ? 'ok' : 'error').toLowerCase();

      const checks = (hd.hub_health && hd.hub_health.checks)
        ? hd.hub_health.checks
        : null;

      const mqttOk = (checks && checks.mqtt_tcp && typeof checks.mqtt_tcp.ok === 'boolean')
        ? checks.mqtt_tcp.ok
        : null;

      const audioOk = (checks && checks.audio_server && typeof checks.audio_server.ok === 'boolean')
        ? checks.audio_server.ok
        : null;

      const elOk = (checks && checks.elevenlabs_api && typeof checks.elevenlabs_api.ok === 'boolean')
        ? checks.elevenlabs_api.ok
        : null;

      const pill = (v) => {
        if (v === true) return '<span class="pill">OK</span>';
        if (v === false) return '<span class="pill">ERR</span>';
        return '<span class="muted">—</span>';
      };

      const extra = [];
      // if (checks && checks.audio_cache_dir && checks.audio_cache_dir.path) {
      //   extra.push(`<div class="muted">Audio dir: ${esc(checks.audio_cache_dir.path)}</div>`);
      // }
      if (checks && checks.elevenlabs_api && checks.elevenlabs_api.error) {
        extra.push(`<div class="muted">ElevenLabs: ${esc(checks.elevenlabs_api.error)}</div>`);
      }
      if (checks && checks.mqtt_tcp && checks.mqtt_tcp.error) {
        extra.push(`<div class="muted">MQTT: ${esc(checks.mqtt_tcp.error)}</div>`);
      }

      parts.push(
        `<div style="margin-top:10px;">
          <div><b>Overall:</b> <span class="pill">${esc(hubHealthLabel(overall))}</span></div>
          <div><b>MQTT:</b> ${pill(mqttOk)} <span class="muted">${mqttOk === false ? '(not connected)' : ''}</span></div>
          <div><b>Audio:</b> ${pill(audioOk)}</div>
          <div><b>ElevenLabs:</b> ${pill(elOk)} <span class="muted">${elOk === false ? '(unreachable)' : ''}</span></div>
          ${extra.join('')}
        </div>`
      );
    }

    b.innerHTML = `<h3 style="margin:0 0 8px 0; font-size:14px;">Hub Health</h3>` + parts.join('');
    b.style.display = '';
  }

  async function refresh(){
    try {
      // Fetch status + health in parallel; health is optional (older hub builds may not have it).
      const statusPromise = api('/ui/status', 'GET');
      const healthPromise = fetch('/ui/health', { method: 'GET' })
        .then(async (r) => {
          const t = await r.text();
          let j;
          try { j = JSON.parse(t); } catch { j = null; }
          if (!r.ok) return null;
          return j;
        })
        .catch(() => null);

      const data = await statusPromise;
      const health = await healthPromise;

      if (health && typeof health === 'object') {
        _hubHealth = health;
        _hubHealthLoadedAt = Date.now();
        updateHubHealthPanel(_hubHealth);
      } else {
        // Clear to dashes if /ui/health missing or failed
        updateHubHealthPanel(null);
      }

      const devices = data.devices || [];
      _devicesCache = Array.isArray(devices) ? devices : [];
      populateActionDeviceSelect(_devicesCache);
      populateDeviceSettingsPanel(_devicesCache);

      // Hub health (from /ui/status). If the hub reports an error state,
      // annotate devices so `renderWarnings()` can show it.
      const hubHealthObj = (data && typeof data === 'object') ? (data.hub_health || null) : null;
      const hubHealthOk = !!(hubHealthObj && hubHealthObj.ok);
      if (hubHealthObj && !hubHealthOk) {
        devices.forEach(d => {
          try { d.hub_health = 'error'; } catch(e) {}
        });
      }

      // Enrich end-to-end Speak progress.
      // Prefer matching on command_id. If command_id differs but audio_id matches, still update.
      devices.forEach(d => {
        try {
          const ls = d.last_speak;
          if (!ls || !ls.command_id) return;
          const ack = d.last_ack;
          if (!ack || !ack.status) return;

          const sameCmd = String(ack.command_id || '') === String(ls.command_id || '');
          const sameAudio = (ack.details && typeof ack.details === 'object') &&
                            String(ack.details.audio_id || '') &&
                            String(ack.details.audio_id || '') === String(ls.audio_id || '');

          if (!sameCmd && !sameAudio) return;

          const st = String(ack.status || '').toLowerCase();
          if (st) ls.state = st; // accepted/completed/error

          // capture ack timestamp for age/timeout logic
          if (typeof ack.ts === 'number') ls.ack_ts = ack.ts;
        } catch(e) {}
      });

      const otaLatest = data.ota_latest_version || null;
      const hubVer = data.hub_version || null;
      const hubLatest = data.hub_latest_version || null;
      const hubUpd = (typeof data.hub_update_available === 'boolean') ? data.hub_update_available : null;
      const hubErr = data.hub_update_error || null;
      const hubHealth = hubHealthObj ? (hubHealthOk ? 'ok' : 'error') : null;

      renderBanner({
        otaLatest,
        hubVer,
        hubLatest,
        hubUpd,
        hubErr,
        hubHealth,
        hubHealthDetail: _hubHealth && (_hubHealth.ok === true || _hubHealth.overall) ? _hubHealth : null,
      });

      render(devices, otaLatest);
      renderStatusPanel(data);
      renderMqttPanelFromStatus(data);
    } catch (e) {
      renderBanner(null);
      render([]);
      updateHubHealthPanel(null);
      log(`Refresh failed: ${e.message}`);
    }
  }

  function render(devices, otaLatest){
    const tbody = $("tbody");
    if (!devices.length){
      tbody.innerHTML = `<tr><td colspan=\"9\" class=\"muted\">No devices.</td></tr>`;
      return;
    }

    tbody.innerHTML = devices.map(d => {
      const did = fmt(d.device_id);
      const st = normStatus(d);

      const lastSeenIso = fmt(d.last_seen || '');
      const ageSec = (typeof d.last_seen_age_sec === 'number') ? d.last_seen_age_sec : null;
      const ageStr = (ageSec != null) ? fmtAgeSeconds(ageSec) : '';
      const ls = lastSeenIso;
      const avRaw = fmt(d.agent_version || '(unknown)');
      const svRaw = fmt(d.setup_version || '(unknown)');
      const needsUpdate = otaLatest && avRaw && (avRaw !== '(unknown)') && (avRaw !== String(otaLatest));
      const av = needsUpdate ? `${avRaw} → ${otaLatest}` : avRaw;
      const sv = svRaw;
      const lc = lastCmd(d);
      const lr = lastResult(d);
      return `
        <tr>
          <td>
            ${d.friendly_name ? `<div><b>${String(d.friendly_name)}</b></div>` : ``}
            <span class="pill">${did}</span>
            ${renderWarnings(d) ? `<div style="margin-top:6px;">${renderWarnings(d)}</div>` : ``}
            ${!isOnline(d) ? `<div class="muted" style="margin-top:6px;"><b>${offlineDetail(d)}</b></div>` : ``}
          </td>
          <td class="${statusClass(st)}">${st}</td>
          <td class="muted">${ls}${ageStr ? ` <span class="muted">(${ageStr})</span>` : ''}${!isOnline(d) && d.offline_reason ? ` <span class="muted">[${offlineReasonLabel(d.offline_reason)}]</span>` : ''}</td>
          <td class="muted">${renderSpeakBlock(d) || ''}</td>
          <td>${av}${needsUpdate ? `<div class="muted">Update available</div>` : ``}</td>
          <td>${sv}</td>
          <td class="muted">${lc}</td>
          <td class="muted">${lr}</td>
          <td>
            <div class="row-actions">
              <button data-action="update" data-device="${did}">Update</button>
              <button data-action="buildUpdate" data-device="${did}">Build+Update</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');

  }

  // API key UI removed

  let currentRolloutJobId = null;
  let rolloutFinishedLoggedFor = null;

  function renderRollout(summary){
    const el = $("rolloutStatus");
    if (!summary) {
      el.innerHTML = '<div class="muted">No rollout running.</div>';
      const resumeBtn = $("rolloutResume");
      const cancelBtn = $("rolloutCancel");
      if (resumeBtn) resumeBtn.disabled = true;
      if (cancelBtn) cancelBtn.disabled = true;
      return;
    }
    el.innerHTML = `
      <div><span class="pill">Job</span> ${summary.id}</div>
      <div><span class="pill">State</span> ${summary.state}</div>
      <div><span class="pill">Total</span> ${summary.targets_total}</div>
      <div><span class="pill">Pending</span> ${summary.pending}</div>
      <div><span class="pill">In-flight</span> ${summary.in_flight}</div>
      <div><span class="pill">Done</span> ${summary.done}</div>
      <div><span class="pill">Updated</span> ${summary.updated ?? 0}</div>
      <div><span class="pill">Skipped</span> ${summary.skipped ?? 0}</div>
      <div><span class="pill">Failed</span> ${summary.failed}</div>
      ${Array.isArray(summary.failed_summary) && summary.failed_summary.length ? `
        <div style="flex-basis:100%"><span class="pill">Failure details</span>
          <div class="muted" style="margin-top:4px; white-space:pre-wrap">${summary.failed_summary.map(x => `${x.device_id}: ${x.error || 'error'}`).join('\\n')}</div>
        </div>
      ` : ''}
      ${summary.state === 'halted' ? `<div><span class="pill">Reason</span> ${summary.halted_reason || ''}</div>` : ''}
      <div><span class="pill">Stop on failure</span> ${summary.stop_on_failure ? 'true' : 'false'}</div>
      <div><span class="pill">Max failures</span> ${summary.max_failures ?? 0}</div>
      <div><span class="pill">ACK timeout</span> ${(summary.ack_timeout_sec ?? 120)}s</div>
    `;
    const resumeBtn = $("rolloutResume");
    const cancelBtn = $("rolloutCancel");
    if (resumeBtn && cancelBtn) {
      const st = summary.state;
      resumeBtn.disabled = !(st === 'halted');
      cancelBtn.disabled = !(st && st !== 'canceled' && st !== 'completed' && st !== 'completed_with_failures');
    }
  }

  async function rolloutStatusTick(){
    if (!currentRolloutJobId) return;
    try {
      const data = await api(`/ui/rollout/status?job_id=${encodeURIComponent(currentRolloutJobId)}`, 'GET');
      renderRollout(data.summary);
      if (data.summary && (data.summary.state === 'completed' || data.summary.state === 'completed_with_failures')) {
        if (rolloutFinishedLoggedFor !== currentRolloutJobId) {
          log(`Rollout finished: ${JSON.stringify(data.summary)}`);
          rolloutFinishedLoggedFor = currentRolloutJobId;
        }
      }
    } catch (e) {
      log(`Rollout status failed: ${e.message}`);
    }
  }

  const rolloutUpdateAllBtn = $("rolloutUpdateAll");
  if (rolloutUpdateAllBtn) rolloutUpdateAllBtn.addEventListener('click', async () => {
    try {
      const bs = parseInt($("batchSize").value || '5', 10);
      log(`Starting staged rollout (update), batch_size=${bs}...`);
      const r = await api('/ui/rollout/start', 'POST', { all: true, batch_size: bs, build: false, stop_on_failure: true, max_failures: 1 });
      currentRolloutJobId = r.job_id;
      rolloutFinishedLoggedFor = null;
      renderRollout(r.job ? {
        id: r.job.id,
        state: r.job.state,
        targets_total: (r.job.targets||[]).length,
        pending: (r.job.pending||[]).length,
        in_flight: Object.keys(r.job.in_flight||{}).length,
        done: Object.keys(r.job.done||{}).length,
        updated: 0,
        skipped: 0,
        failed: Object.keys(r.job.failed||{}).length,
        ack_timeout_sec: (r.job.ack_timeout_sec ?? 120),
        stop_on_failure: (r.job.stop_on_failure ?? true),
        max_failures: (r.job.max_failures ?? 1),
      } : null);
      log(`Rollout started: ${currentRolloutJobId}`);
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout start failed: ${e.message}`);
    }
  });

  const rolloutBuildUpdateAllBtn = $("rolloutBuildUpdateAll");
  if (rolloutBuildUpdateAllBtn) rolloutBuildUpdateAllBtn.addEventListener('click', async () => {
    try {
      const bs = parseInt($("batchSize").value || '5', 10);
      log(`Starting staged rollout (build+update), batch_size=${bs}...`);
      const r = await api('/ui/rollout/start', 'POST', { all: true, batch_size: bs, build: true, stop_on_failure: true, max_failures: 1 });
      currentRolloutJobId = r.job_id;
      rolloutFinishedLoggedFor = null;
      renderRollout(r.job ? {
        id: r.job.id,
        state: r.job.state,
        targets_total: (r.job.targets||[]).length,
        pending: (r.job.pending||[]).length,
        in_flight: Object.keys(r.job.in_flight||{}).length,
        done: Object.keys(r.job.done||{}).length,
        updated: 0,
        skipped: 0,
        failed: Object.keys(r.job.failed||{}).length,
        ack_timeout_sec: (r.job.ack_timeout_sec ?? 120),
        stop_on_failure: (r.job.stop_on_failure ?? true),
        max_failures: (r.job.max_failures ?? 1),
      } : null);
      log(`Rollout started: ${currentRolloutJobId}`);
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout start failed: ${e.message}`);
    }
  });

  const rolloutResumeBtn = $("rolloutResume");
  if (rolloutResumeBtn) rolloutResumeBtn.addEventListener('click', async () => {
    if (!currentRolloutJobId) return;
    try {
      log(`Resuming rollout ${currentRolloutJobId}...`);
      await api('/ui/rollout/resume', 'POST', { job_id: currentRolloutJobId });
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout resume failed: ${e.message}`);
    }
  });

  const rolloutCancelBtn = $("rolloutCancel");
  if (rolloutCancelBtn) rolloutCancelBtn.addEventListener('click', async () => {
    if (!currentRolloutJobId) return;
    try {
      log(`Canceling rollout ${currentRolloutJobId}...`);
      await api('/ui/rollout/cancel', 'POST', { job_id: currentRolloutJobId });
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout cancel failed: ${e.message}`);
    }
  });

  const refreshBtn = $("refresh");
  if (refreshBtn) refreshBtn.addEventListener('click', refresh);

    if ($("mqttDeviceSelect")) {
    $("mqttDeviceSelect").addEventListener('change', () => {
      if ($("mqttTopicValue")) {
        $("mqttTopicValue").textContent = buildAudioTopic($("mqttDeviceSelect").value);
      }
      if ($("mqttCopyMsg")) $("mqttCopyMsg").textContent = '';
    });
  }

  if ($("mqttCopyTopicBtn")) {
    $("mqttCopyTopicBtn").addEventListener('click', async () => {
      const topic = buildAudioTopic($("mqttDeviceSelect") ? $("mqttDeviceSelect").value : '');
      if (!$("mqttDeviceSelect") || !$("mqttDeviceSelect").value) {
        if ($("mqttCopyMsg")) $("mqttCopyMsg").textContent = 'Select a device first';
        return;
      }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(topic);
        } else {
          const ta = document.createElement('textarea');
          ta.value = topic;
          ta.setAttribute('readonly', '');
          ta.style.position = 'absolute';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          ta.setSelectionRange(0, ta.value.length);
          const ok = document.execCommand('copy');
          document.body.removeChild(ta);
          if (!ok) throw new Error('execCommand copy failed');
        }
        if ($("mqttCopyMsg")) $("mqttCopyMsg").textContent = 'Copied';
      } catch (err) {
        if ($("mqttCopyMsg")) $("mqttCopyMsg").textContent = 'Copy failed';
      }
    });
  }

    if (friendlyNameInput) {
    friendlyNameInput.addEventListener('input', () => {
      _friendlyNameDirty = true;
      _friendlyNameDirtyForDevice = deviceSel ? String(deviceSel.value || '').trim() : '';
    });
  }

  if (deviceSel) deviceSel.addEventListener('change', () => {
    _friendlyNameDirty = false;
    _friendlyNameDirtyForDevice = String(deviceSel.value || '').trim();
    syncFriendlyNameFromSelection(true);
  });

  if (saveDeviceCfgBtn) saveDeviceCfgBtn.addEventListener('click', async () => {
    const device_id = deviceSel ? String(deviceSel.value || '').trim() : '';
    const friendly_name = friendlyNameInput ? String(friendlyNameInput.value || '').trim() : '';

    if (!device_id) {
      setDevCfgStatus('Pick a device first.');
      return;
    }

    try {
      saveDeviceCfgBtn.disabled = true;
      setDevCfgStatus('Saving…');
      await api('/ui/device/config', 'POST', { device_id, friendly_name });
      setDevCfgStatus('Saved.');
      _friendlyNameDirty = false;
      _friendlyNameDirtyForDevice = device_id;
      await refresh();
    } catch (e) {
      setDevCfgStatus(`Save failed: ${e.message}`);
      log(`Device config save failed: ${e.message}`);
    } finally {
      saveDeviceCfgBtn.disabled = false;
    }
  });

  // Account wiring (optional)
  if (accountBtn) accountBtn.addEventListener('click', async () => {
    try {
      accountBtn.disabled = true;
      await loadMe();
      showAccountPanel(true);
    } catch (e) {
      log(`Account load failed: ${e.message}`);
      alert(`Account load failed: ${e.message}`);
    } finally {
      accountBtn.disabled = false;
    }
  });

  if (accountClose) accountClose.addEventListener('click', () => showAccountPanel(false));

  if (logoutBtn) logoutBtn.addEventListener('click', async () => {
    try {
      logoutBtn.disabled = true;
      await fetch('/logout', { method: 'POST' });
    } catch(e) {
      // ignore
    } finally {
      window.location.href = '/login';
    }
  });

  if (myPwSave) myPwSave.addEventListener('click', async () => {
    if (myPwMsg) myPwMsg.textContent = '';
    const current_password = (myPwCurrent && myPwCurrent.value ? myPwCurrent.value : '').trim();
    const new_password = (myPwNew && myPwNew.value ? myPwNew.value : '').trim();
    if (!current_password || !new_password) {
      if (myPwMsg) myPwMsg.textContent = 'Current and new password are required.';
      return;
    }
    try {
      myPwSave.disabled = true;
      await api('/ui/me/password', 'POST', { current_password, new_password });
      if (myPwMsg) myPwMsg.textContent = 'Password updated.';
      if (myPwCurrent) myPwCurrent.value = '';
      if (myPwNew) myPwNew.value = '';
    } catch (e) {
      if (myPwMsg) myPwMsg.textContent = `Update failed: ${e.message}`;
    } finally {
      myPwSave.disabled = false;
    }
  });

  if (gmSet) gmSet.addEventListener('click', async () => {
    if (gmMsg) gmMsg.textContent = '';
    const username = (gmUsername && gmUsername.value ? gmUsername.value : 'gm').trim();
    const password = (gmPassword && gmPassword.value ? gmPassword.value : '').trim();
    if (!password) {
      if (gmMsg) gmMsg.textContent = 'GM password is required.';
      return;
    }
    try {
      gmSet.disabled = true;
      const r = await api('/ui/admin/gm', 'POST', { username, password });
      if (gmMsg) gmMsg.textContent = r.created ? `GM user "${r.username}" created.` : `GM user "${r.username}" password reset.`;
      if (gmPassword) gmPassword.value = '';
    } catch (e) {
      if (gmMsg) gmMsg.textContent = `GM update failed: ${e.message}`;
    } finally {
      gmSet.disabled = false;
    }
  });

  // Delegated handler (button is rendered dynamically inside STATUS table)
  document.addEventListener('click', async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('#hubUpdate') : null;
    if (!btn) return;

    try {
      btn.disabled = true;
      log('Starting hub update (git pull + restart services)...');
      const r = await api('/ui/hub/update', 'POST', {});
      log(`Hub update triggered: ${JSON.stringify(r)}`);
      log('If hub services restart, the page may briefly fail to refresh; wait a few seconds then refresh.');
    } catch (e) {
      log(`Hub update failed: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  const updateAllBtn = $("updateAll");
  if (updateAllBtn) updateAllBtn.addEventListener('click', async () => {
    try {
      log('Update All...');
      const r = await api('/ui/update', 'POST', { all: true, command_id: `cmd-all-${Date.now()}` });
      log(`Update All result: ${JSON.stringify(r)}`);
      await refresh();
    } catch (e) {
      log(`Update All failed: ${e.message}`);
    }
  });

  const buildUpdateAllBtn = $("buildUpdateAll");
  if (buildUpdateAllBtn) buildUpdateAllBtn.addEventListener('click', async () => {
    try {
      log('Build+Update All...');
      const r = await api('/ui/build-and-update', 'POST', { all: true, command_id: `cmd-build-all-${Date.now()}` });
      log(`Build+Update All result: ${JSON.stringify(r)}`);
      await refresh();
    } catch (e) {
      log(`Build+Update All failed: ${e.message}`);
    }
  });

  // Device Actions wiring (beep + speaker test)
  if (beepBtn) beepBtn.addEventListener('click', async () => {
    try {
      const did = actionDeviceSelect ? actionDeviceSelect.value : '';
      beepBtn.disabled = true;
      log(`Beep: ${did}...`);
      const r = await runDeviceAction('/ui/device/beep', did);
      log(`Beep result: ${JSON.stringify(r)}`);
      await refresh();
    } catch (e) {
      log(`Beep failed: ${e.message}`);
    } finally {
      beepBtn.disabled = false;
    }
  });

  if (speakerTestBtn) speakerTestBtn.addEventListener('click', async () => {
    try {
      const did = actionDeviceSelect ? actionDeviceSelect.value : '';
      speakerTestBtn.disabled = true;
      log(`Speaker test: ${did}...`);
      const r = await runDeviceAction('/ui/device/speaker-test', did);
      log(`Speaker test result: ${JSON.stringify(r)}`);
      await refresh();
    } catch (e) {
      log(`Speaker test failed: ${e.message}`);
    } finally {
      speakerTestBtn.disabled = false;
    }
  });

  // Hub log + runbook wiring (optional)
  if (runbookBtn) runbookBtn.addEventListener('click', () => {
    try {
      window.open('/ui/runbook', '_blank');
    } catch (e) {
      // Fallback: navigate in-place
      window.location.href = '/ui/runbook';
    }
  });

  if (hubCmdLogBtn) hubCmdLogBtn.addEventListener('click', async () => {
    try {
      hubCmdLogBtn.disabled = true;
      showHubCmdLogPanel(true);
      await loadHubCmdLog(300);
      log('Loaded hub command log.');
    } catch (e) {
      log(`Hub log failed: ${e.message}`);
    } finally {
      hubCmdLogBtn.disabled = false;
    }
  });

  if (hubCmdLogRefreshBtn) hubCmdLogRefreshBtn.addEventListener('click', async () => {
    try {
      hubCmdLogRefreshBtn.disabled = true;
      await loadHubCmdLog(300);
    } catch (e) {
      log(`Hub log refresh failed: ${e.message}`);
    } finally {
      hubCmdLogRefreshBtn.disabled = false;
    }
  });

  if (hubCmdLogCloseBtn) hubCmdLogCloseBtn.addEventListener('click', () => {
    showHubCmdLogPanel(false);
  });

  // Delegated handler for device action buttons (works for both hidden legacy table and STATUS table)
  document.addEventListener('click', async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('button[data-action]') : null;
    if (!btn) return;

    const action = btn.getAttribute('data-action');
    const device = btn.getAttribute('data-device');
    const targetCmd = btn.getAttribute('data-cmd');
    if (!action) return;

    try {
      btn.disabled = true;

      if (action === 'update') {
        log(`Updating ${device}...`);
        const r = await api('/ui/update', 'POST', { device_id: device, command_id: `cmd-${device}-${Date.now()}` });
        log(`Update result: ${JSON.stringify(r)}`);

      } else if (action === 'buildUpdate') {
        log(`Build+Update ${device}...`);
        const r = await api('/ui/build-and-update', 'POST', { device_id: device, command_id: `cmd-${device}-${Date.now()}` });
        log(`Build+Update result: ${JSON.stringify(r)}`);

      } else if (action === 'beep') {
        log(`Beep ${device}...`);
        const r = await api('/ui/device/beep', 'POST', { device_id: device });
        log(`Beep result: ${JSON.stringify(r)}`);

      } else if (action === 'speakStop') {
        log(`Speak stop requested for ${device} (target=${targetCmd || 'unknown'})...`);
        const r = await api('/ui/speak/stop', 'POST', { device_id: device, target_command_id: targetCmd || null });
        log(`Speak stop result: ${JSON.stringify(r)}`);

      } else if (action === 'speakRetry') {
        log(`Speak retry requested for ${device} (target=${targetCmd || 'unknown'})...`);
        const r = await api('/ui/speak/retry', 'POST', { device_id: device, target_command_id: targetCmd || null });
        log(`Speak retry result: ${JSON.stringify(r)}`);
      }

      await refresh();
    } catch (e) {
      log(`Action failed for ${device || action}: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  // Auto-refresh always on, no toggle
  refresh();
  loadAudioStatus();
  setInterval(() => {
    refresh(); // includes /ui/health best-effort
    rolloutStatusTick();
    loadAudioStatus();
  }, 5000);
})();