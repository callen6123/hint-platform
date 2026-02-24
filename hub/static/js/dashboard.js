(function(){
  const $ = (id) => document.getElementById(id);
  const logEl = $("log");

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
    if (info.hubHealth) {
      const hh = String(info.hubHealth).toLowerCase();
      if (hh === 'ok') parts.push(`<div><b>Hub:</b> <span class="pill">OK</span></div>`);
      else if (hh === 'error') parts.push(`<div><b>Hub:</b> <span class="pill">ERROR</span> <span class="muted">(unreachable)</span></div>`);
      else parts.push(`<div><b>Hub:</b> <span class="muted">unknown</span></div>`);
    }
    if (info.otaLatest) {
      parts.push(`<div><b>Device OTA:</b> latest <span class="pill">${info.otaLatest}</span></div>`);
    } else {
      parts.push(`<div><b>Device OTA:</b> <span class="muted">no bundle built yet</span></div>`);
    }

    if (info.hubVer) {
      const hv = `<span class="pill">${info.hubVer}</span>`;
      if (info.hubUpd === true) {
        const hl = info.hubLatest ? `<span class="pill">${info.hubLatest}</span>` : `<span class="muted">(unknown)</span>`;
        parts.push(`<div><b>Hub:</b> ${hv} → ${hl} <span class="muted">(update available)</span></div>`);
      } else if (info.hubUpd === false) {
        parts.push(`<div><b>Hub:</b> ${hv} <span class="muted">(up to date)</span></div>`);
      } else {
        parts.push(`<div><b>Hub:</b> ${hv} <span class="muted">(update status unknown)</span></div>`);
      }
    } else {
      parts.push(`<div><b>Hub:</b> <span class="muted">version unknown</span></div>`);
    }

    if (info.hubErr) {
      parts.push(`<div class="muted">Hub update check error: ${String(info.hubErr)}</div>`);
    }

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
      if (checks && checks.audio_cache_dir && checks.audio_cache_dir.path) {
        extra.push(`<div class="muted">Audio dir: ${esc(checks.audio_cache_dir.path)}</div>`);
      }
      if (checks && checks.elevenlabs_api && checks.elevenlabs_api.error) {
        extra.push(`<div class="muted">ElevenLabs: ${esc(checks.elevenlabs_api.error)}</div>`);
      }
      if (checks && checks.mqtt_tcp && checks.mqtt_tcp.error) {
        extra.push(`<div class="muted">MQTT: ${esc(checks.mqtt_tcp.error)}</div>`);
      }

      parts.push(
        `<div style="margin-top:10px;">
          <h3 style="margin:0 0 8px 0; font-size:14px;">Hub Health</h3>
          <div><b>Overall:</b> <span class="pill">${esc(hubHealthLabel(overall))}</span></div>
          <div><b>MQTT:</b> ${pill(mqttOk)} <span class="muted">${mqttOk === false ? '(not connected)' : ''}</span></div>
          <div><b>Audio:</b> ${pill(audioOk)}</div>
          <div><b>ElevenLabs:</b> ${pill(elOk)} <span class="muted">${elOk === false ? '(unreachable)' : ''}</span></div>
          ${extra.join('')}
        </div>`
      );
    }

    b.innerHTML = `<h3 style="margin:0 0 8px 0; font-size:14px;">Updates</h3>` + parts.join('');
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
      tbody.innerHTML = `<tr><td colspan=\"8\" class=\"muted\">No devices.</td></tr>`;
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
            ${renderSpeakBlock(d)}
          </td>
          <td class="${statusClass(st)}">${st}</td>
          <td class="muted">${ls}${ageStr ? ` <span class="muted">(${ageStr})</span>` : ''}${!isOnline(d) && d.offline_reason ? ` <span class="muted">[${offlineReasonLabel(d.offline_reason)}]</span>` : ''}</td>
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

    tbody.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const action = btn.getAttribute('data-action');
        const device = btn.getAttribute('data-device');
        const targetCmd = btn.getAttribute('data-cmd');
        try {
          btn.disabled = true;
          if (action === 'update') {
            log(`Updating ${device}...`);
            const r = await api('/ui/update', 'POST', { device_id: device, command_id: `cmd-${device}-${Date.now()}` });
            log(`Update result: ${JSON.stringify(r)}`);
          } else if (action === 'speakStop') {
            log(`Speak stop requested for ${device} (target=${targetCmd || 'unknown'})...`);
            const r = await api('/ui/speak/stop', 'POST', { device_id: device, target_command_id: targetCmd || null });
            log(`Speak stop result: ${JSON.stringify(r)}`);
          } else if (action === 'speakRetry') {
            log(`Speak retry requested for ${device} (target=${targetCmd || 'unknown'})...`);
            const r = await api('/ui/speak/retry', 'POST', { device_id: device, target_command_id: targetCmd || null });
            log(`Speak retry result: ${JSON.stringify(r)}`);
          } else {
            log(`Build+Update ${device}...`);
            const r = await api('/ui/build-and-update', 'POST', { device_id: device, command_id: `cmd-${device}-${Date.now()}` });
            log(`Build+Update result: ${JSON.stringify(r)}`);
          }
          await refresh();
        } catch (e) {
          log(`Action failed for ${device}: ${e.message}`);
        } finally {
          btn.disabled = false;
        }
      });
    });
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

  const hubUpdateBtn = $("hubUpdate");
  if (hubUpdateBtn) hubUpdateBtn.addEventListener('click', async () => {
    try {
      log('Starting hub update (git pull + restart services)...');
      const r = await api('/ui/hub/update', 'POST', {});
      log(`Hub update triggered: ${JSON.stringify(r)}`);
      log('If hub services restart, the page may briefly fail to refresh; wait a few seconds then refresh.');
    } catch (e) {
      log(`Hub update failed: ${e.message}`);
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

  // Auto-refresh always on, no toggle
  refresh();
  loadAudioStatus();
  setInterval(() => {
    refresh(); // includes /ui/health best-effort
    rolloutStatusTick();
    loadAudioStatus();
  }, 5000);
})();