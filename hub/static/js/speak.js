(function(){
  const $ = (id) => document.getElementById(id);
  const logEl = $("log");

  let cachedDevices = [];
  let cachedVoices = [];
  let selectedPhraseId = '';

  function log(msg){
    const ts = new Date().toISOString();
    if (!logEl) return;
    logEl.textContent += `[${ts}] ${msg}\n`;
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function api(path, method, body){
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    const txt = await res.text();
    let data;
    try { data = JSON.parse(txt); } catch { data = { raw: txt }; }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${JSON.stringify(data)}`);
    return data;
  }

  // --- Helper to poll /ui/status for device ACK ---
  async function getStatus(){
    return await api('/ui/status', 'GET');
  }

  function findDeviceInStatus(status, device_id){
    const devices = (status && status.devices) ? status.devices : [];
    return devices.find(d => String(d.device_id || '') === String(device_id || '')) || null;
  }

  async function waitForAck(device_id, command_id, timeoutMs, onProgress){
    const start = Date.now();
    const timeout = (typeof timeoutMs === 'number' && timeoutMs > 0) ? timeoutMs : 12000;

    // We want the terminal ACK for a command. Devices usually emit:
    //   accepted -> completed (or error)
    // We poll /ui/status until we see a matching last_ack.

    let lastSeen = null;
    let lastStatus = null;

    while ((Date.now() - start) < timeout) {
      try {
        const st = await getStatus();
        const dev = findDeviceInStatus(st, device_id);
        const ack = dev && dev.last_ack ? dev.last_ack : null;

        if (ack && String(ack.command_id || '') === String(command_id || '')) {
          lastSeen = ack;
          const status = String(ack.status || '').toLowerCase();

          // Only emit progress when status changes (avoids log spam)
          if (status && status !== lastStatus) {
            lastStatus = status;
            if (typeof onProgress === 'function') {
              try { onProgress(ack); } catch(e) {}
            }
          }

          // Keep waiting if we only have an intermediate ACK.
          if (status === 'accepted') {
            // continue polling
          } else {
            return ack;
          }
        }
      } catch(e) {
        // ignore transient errors during polling
      }
      await new Promise(r => setTimeout(r, 400));
    }

    // If we timed out but we did see something (like accepted), return it.
    return lastSeen;
  }

  function setStatus(s){
    const el = $("status");
    if (!el) return;
    el.textContent = s || '';
  }

  function updateCharCount(){
    const ta = $("text");
    const cc = $("charCount");
    if (!ta || !cc) return;
    const len = String(ta.value || '').length;
    const max = parseInt((ta.dataset && ta.dataset.maxchars) ? ta.dataset.maxchars : '', 10);
    const hasMax = Number.isFinite(max) && max > 0;

    cc.textContent = hasMax ? `${len} / ${max}` : String(len);

    // Optional visual cue when exceeding max
    if (hasMax) {
      if (len > max) cc.classList.add('over');
      else cc.classList.remove('over');
    }
  }

  function setSpeakProgress(state, meta){
    const statusEl = $("speakStatus");
    const metaEl = $("speakMeta");
    const panel = $("speakProgress");
    if (!statusEl || !metaEl) return;

    const labels = {
      idle: 'Idle',
      generated: 'Generated',
      sent: 'Sent',
      accepted: 'Accepted',
      completed: 'Completed',
      error: 'Error'
    };

    statusEl.textContent = labels[state] || state || 'Idle';
    statusEl.className = 'status ' + (state || 'idle');
    metaEl.textContent = meta || '';

    if (state === 'completed' && panel) {
      panel.classList.add('flash-ok');
      setTimeout(() => panel.classList.remove('flash-ok'), 1000);
    }
  }

  function setDevCfgStatus(s){
    const el = $("devCfgStatus");
    if (!el) return;
    el.textContent = s || '';
  }

  function setVolumeStatus(s){
    const el = $("volumeStatus");
    if (!el) return;
    el.textContent = s || '';
  }

  function setVolumeValueUI(v){
    const out = $("volumeValue");
    const slider = $("volumeSlider");
    if (slider && (v === undefined || v === null || Number.isNaN(Number(v)))) {
      v = Number(slider.value || 0);
    }
    const pct = Math.max(0, Math.min(100, Number(v) || 0));
    if (out) out.textContent = `${pct}%`;
    if (slider) slider.value = String(pct);
  }

  async function loadDeviceVolume(device_id){
    if (!device_id) return;
    try {
      const data = await api(`/ui/device/volume?device_id=${encodeURIComponent(device_id)}`, 'GET');
      if (data && data.volume_percent !== undefined && data.volume_percent !== null) {
        setVolumeValueUI(Number(data.volume_percent));
      }
      setVolumeStatus('');
    } catch(e){
      // Non-fatal: leave slider as-is
      setVolumeStatus('');
    }
  }

  async function saveDeviceVolume(device_id, volume_percent){
    if (!device_id) return;
    const pct = Math.max(0, Math.min(100, Number(volume_percent) || 0));
    try {
      setVolumeStatus('Saving…');
      await api('/ui/device/volume', 'POST', { device_id, volume_percent: pct });
      setVolumeStatus('Saved.');
    } catch(e){
      setVolumeStatus('Save failed.');
      log(`Volume save failed: ${e.message}`);
    }
  }


  // --- Playback speed (ElevenLabs) ---
  function setSpeedStatus(s){
    const el = $("speedStatus");
    if (!el) return;
    el.textContent = s || '';
  }

  function setSpeedValueUI(v){
    const out = $("speedValue");
    const slider = $("speedSlider");
    if (slider && (v === undefined || v === null || Number.isNaN(Number(v)))) {
      v = Number(slider.value || 1.0);
    }
    const sp = Math.max(0.5, Math.min(2.0, Number(v) || 1.0));
    if (out) out.textContent = `${sp.toFixed(2)}×`;
    if (slider) slider.value = String(sp);
  }

  function saveSpeedPref(sp){
    try { localStorage.setItem('hint_speed', String(sp)); } catch {}
  }

  function loadSpeedPref(){
    try {
      const v = parseFloat(localStorage.getItem('hint_speed') || '');
      return Number.isFinite(v) ? v : 1.0;
    } catch {
      return 1.0;
    }
  }

  function getCurrentSpeed(){
    const slider = $("speedSlider");
    if (!slider) return loadSpeedPref();
    const v = parseFloat(slider.value || '1.0');
    return Number.isFinite(v) ? v : loadSpeedPref();
  }

  function setPhraseStatus(s){
    const el = $("phraseStatus");
    if (!el) return;
    el.textContent = s || '';
  }
  function setSelectedPhrase(pid){
    selectedPhraseId = pid || '';
    const delBtn = $("deletePhraseBtn");
    if (delBtn) delBtn.disabled = !selectedPhraseId;
  }

  // --- Saved Audio (common phrases) ---
  const SAVED_AUDIO_LABELS = {
    intro_welcome: 'Intro/Welcome',
    outro_win: 'Outro/Win',
    outro_lose: 'Outro/Lose',
    time_30: 'Time: 30 min',
    time_15: 'Time: 15 min',
    time_5: 'Time: 5 min',
  };

  function saLabel(key){
    return SAVED_AUDIO_LABELS[key] || key || 'Saved Audio';
  }

  function setSavedAudioGenStatus(s){
    const el = $("savedAudioGenStatus");
    if (!el) return;
    el.textContent = s || '';
  }

  function setFreqAudioGenStatus(s){
    const el = $("freqAudioGenStatus");
    if (!el) return;
    el.textContent = s || '';
  }

  function setFreqAudioPlayStatus(s){
    const el = $("freqAudioPlayStatus");
    if (!el) return;
    el.textContent = s || '';
  }

  function snippetText(text, maxLen){
    const max = (typeof maxLen === 'number' && maxLen > 10) ? maxLen : 42;
    const t = String(text || '').trim().replace(/\s+/g, ' ');
    if (!t) return '';
    return t.length <= max ? t : (t.slice(0, max - 1) + '…');
  }

  function isFreqKey(key){
    return /^freq_(?:[1-9]|10)$/.test(String(key || ''));
  }

  async function loadSavedAudioDefs(){
    // Optional endpoint; if not implemented yet, silently ignore.
    try {
      const data = await api('/ui/saved-audio/list', 'GET');
      const items = (data && data.items) ? data.items : {};

      document.querySelectorAll('input.savedAudioText').forEach(inp => {
        const key = inp.getAttribute('data-key') || '';
        const it = items[key] || null;
        if (it && typeof it.text === 'string' && !inp.value) {
          inp.value = it.text;
        }
      });

      // Frequently Used Audio (freq_1..freq_10): prefill text inputs
      document.querySelectorAll('input.freqAudioText').forEach(inp => {
        const key = inp.getAttribute('data-key') || '';
        const it = items[key] || null;
        if (it && typeof it.text === 'string' && !inp.value) {
          inp.value = it.text;
        }
      });

      document.querySelectorAll('button.savedAudioPlay').forEach(btn => {
        const key = btn.getAttribute('data-key') || '';
        const it = items[key] || null;
        // If endpoint reports existence, disable play when missing.
        if (it && typeof it.exists === 'boolean') {
          btn.disabled = !it.exists;
          btn.title = it.exists ? 'Play saved audio' : 'No saved audio yet (generate first)';
        }
      });

      // Frequently Used Audio Playback buttons: label = snippet of stored text
      document.querySelectorAll('button.freqAudioPlay').forEach(btn => {
        const key = btn.getAttribute('data-key') || '';
        const it = items[key] || null;
        const txt = (it && typeof it.text === 'string') ? it.text : '';
        const snip = snippetText(txt, 60);

        // Always show a usable label
        btn.textContent = snip || '(empty)';

        if (it && typeof it.exists === 'boolean') {
          btn.disabled = !it.exists;
          btn.title = it.exists ? 'Play saved audio' : 'No saved audio yet (generate first)';
        } else {
          // If list endpoint doesn't provide exists, guess from audio_id
          const has = !!(it && (it.audio_id || it.id));
          btn.disabled = !has;
          btn.title = has ? 'Play saved audio' : 'No saved audio yet (generate first)';
        }
      });
    } catch(e) {
      // Endpoint not present yet; leave UI enabled.
    }
  }

  function savePrefs(){
    try {
      localStorage.setItem('hint_voice_id', $("voice").value || '');
      localStorage.setItem('hint_device_id', $("device").value || '');
      localStorage.setItem('hint_speed', String(getCurrentSpeed()));
    } catch {}
  }

  function loadPrefs(){
    try {
      return {
        voice_id: localStorage.getItem('hint_voice_id') || '',
        device_id: localStorage.getItem('hint_device_id') || '',
      };
    } catch {
      return { voice_id:'', device_id:'' };
    }
  }

  function applyDeviceDefaultVoice(){
    const did = $("device").value || '';
    const d = cachedDevices.find(x => (x.device_id || '') === did);
    const dv = d && d.default_voice_id ? String(d.default_voice_id) : '';
    if (!dv) return;
    // Only set if voice exists
    const has = cachedVoices.some(v => String(v.voice_id) === dv);
    if (has) {
      $("voice").value = dv;
      savePrefs();
    }
  }

  async function loadDevices(){
    const data = await api('/ui/status', 'GET');
    const devices = (data.devices || []).filter(d => d && d.device_id);
    cachedDevices = devices;
    const sel = $("device");
    sel.innerHTML = '';
    devices.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.device_id;
      const st = d.status || 'unknown';
      const fn = d.friendly_name ? String(d.friendly_name).trim() : '';
      opt.textContent = fn ? `${fn} — ${d.device_id} (${st})` : `${d.device_id} (${st})`;
      sel.appendChild(opt);
    });
    $("deviceHint").textContent = devices.length ? '' : 'No devices paired yet.';
    return devices;
  }

  async function loadVoices(force){
    const qs = force ? '?force=1' : '';
    const data = await api('/ui/voices' + qs, 'GET');
    const voices = (data.voices || []).filter(v => v && v.voice_id && v.name);
    cachedVoices = voices;
    const sel = $("voice");
    sel.innerHTML = '';
    voices.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v.voice_id;
      opt.textContent = v.name;
      sel.appendChild(opt);
    });
    const checkedAt = data.checked_at ? ` (cached: ${data.checked_at})` : '';
    $("voiceHint").textContent = voices.length ? `Loaded ${voices.length} voice(s)${checkedAt}` : `No voices returned${checkedAt}`;
    return voices;
  }

  async function loadPhrases(){
    const data = await api('/ui/phrases', 'GET');
    return (data.phrases || []).filter(p => p && p.id && (p.text || ''));
  }

  function renderPhrases(list){
    const el = $("phrases");
    el.innerHTML = '';
    list.forEach(p => {
      const b = document.createElement('button');
      b.className = 'phraseBtn';
      b.textContent = p.label || p.id;
      b.addEventListener('click', () => {
        $("text").value = p.text || '';
        $("phraseLabel").value = p.label || '';
        setSelectedPhrase(p.id);
        setPhraseStatus(`Selected ${p.label || p.id}`);
      });
      el.appendChild(b);
    });
    if (!list.length) {
      const span = document.createElement('div');
      span.className = 'muted small';
      span.textContent = 'No saved phrases yet.';
      el.appendChild(span);
    }
  }

  async function refreshAll(forceVoices){
    setStatus('Loading…');
    const prefs = loadPrefs();
    const devices = await loadDevices();
    const voices = await loadVoices(!!forceVoices);

    if (prefs.device_id) {
      $("device").value = prefs.device_id;
    }

    // Load volume for currently selected device (non-fatal)
    try { await loadDeviceVolume($("device")?.value || ''); } catch(e) {}

    if (prefs.voice_id) {
      $("voice").value = prefs.voice_id;
    }
    // Set friendlyName input for selected device (if present in the page)
    const fnEl = $("friendlyName");
    if (fnEl) {
      const curDid = $("device").value || '';
      const curDev = cachedDevices.find(x => (x.device_id||'') === curDid);
      fnEl.value = (curDev && curDev.friendly_name) ? String(curDev.friendly_name) : '';
    }

    // If pref not present or invalid, just keep first option.
    savePrefs();
    applyDeviceDefaultVoice();
    setStatus('Ready');
    log(`Loaded devices=${devices.length}, voices=${voices.length}`);

    // Saved phrases panel was removed; keep code safe if elements are missing.
    if ($("phrases")) {
      const phrases = await loadPhrases();
      renderPhrases(phrases);
      setSelectedPhrase('');
      setPhraseStatus('');
    }

    // Prefill Saved Audio inputs / enable play buttons (optional)
    await loadSavedAudioDefs();
  }

  $("device").addEventListener('change', () => {
    savePrefs();
    applyDeviceDefaultVoice();
    loadDeviceVolume($("device")?.value || '');
  });
  $("voice").addEventListener('change', savePrefs);

  const refreshBtn = $("refreshBtn");
  if (refreshBtn) refreshBtn.addEventListener('click', async () => {
    try {
      await refreshAll(true);
    } catch(e){
      setStatus('Error');
      log(`Refresh failed: ${e.message}`);
    }
  });


  const volumeSlider = $("volumeSlider");
  if (volumeSlider) {
    // live UI update while dragging
    volumeSlider.addEventListener('input', () => setVolumeValueUI());
    // commit on release
    volumeSlider.addEventListener('change', () => {
      const did = $("device")?.value || '';
      setVolumeValueUI();
      saveDeviceVolume(did, volumeSlider.value);
    });
    // initialize UI readout
    setVolumeValueUI(Number(volumeSlider.value || 0));
  }


  const speedSlider = $("speedSlider");
  if (speedSlider) {
    // Initialize from localStorage (so it's sticky across refreshes)
    const sp0 = loadSpeedPref();
    speedSlider.value = String(sp0);
    setSpeedValueUI(sp0);

    // live UI update while dragging
    speedSlider.addEventListener('input', () => setSpeedValueUI());

    // persist on release
    speedSlider.addEventListener('change', () => {
      setSpeedValueUI();
      const sp = getCurrentSpeed();
      saveSpeedPref(sp);
      setSpeedStatus('Saved.');
      setTimeout(() => setSpeedStatus(''), 800);
    });
  }


  $("speakBtn").addEventListener('click', async () => {
    const device_id = $("device").value || '';
    const voice_id = $("voice").value || '';
    const text = ($("text").value || '').trim();
    if (!device_id) { log('Pick a device first.'); return; }
    if (!voice_id) { log('Pick a voice first.'); return; }
    if (!text) { log('Type a message first.'); return; }

    // Optional max char guard (matches data-maxchars on textarea)
    const ta = $("text");
    const max = parseInt((ta && ta.dataset && ta.dataset.maxchars) ? ta.dataset.maxchars : '', 10);
    if (Number.isFinite(max) && max > 0 && text.length > max) {
      log(`Message too long (${text.length}/${max}).`);
      setStatus('Too long');
      setSpeakProgress('error', `Message too long (${text.length}/${max})`);
      return;
    }

    try {
      // Disable primary controls while request is in-flight
      // NOTE: Keep Cancel enabled so the user can stop playback mid-flight.
      $("speakBtn").disabled = true;
      if ($("cancelBtn")) $("cancelBtn").disabled = false;
      if ($("refreshBtn")) $("refreshBtn").disabled = true;
      if ($("device")) $("device").disabled = true;
      if ($("voice")) $("voice").disabled = true;

      setStatus('Generating…');
      setSpeakProgress('generated', 'Generating audio on hub…');
      log(`Speak: device=${device_id}, voice=${voice_id}, chars=${text.length}`);
      const speed = getCurrentSpeed();
      const r = await api('/ui/speak', 'POST', { device_id, text, tts: { voice_id, speed } });
      // Hub has generated/stored audio and published the command at this point.
      setSpeakProgress('sent', r && r.command_id ? `Command ID: ${r.command_id}` : 'Sent');
      log(`Queued: ${JSON.stringify(r)}`);
      // --- Poll for device ACK after speak command ---
      if (r && r.command_id) {
        setStatus(`Sent (command_id=${r.command_id})`);

        const ack = await waitForAck(device_id, r.command_id, 45000, (a) => {
          const st = String(a.status || '').toLowerCase();
          log(`Device ACK: ${JSON.stringify(a)}`);
          if (st === 'accepted') {
            setStatus('Accepted');
            setSpeakProgress('accepted', `Command ID: ${r.command_id}`);
          } else if (st) {
            setStatus(st.charAt(0).toUpperCase() + st.slice(1));
            setSpeakProgress(st, `Command ID: ${r.command_id}`);
          }
        });

        if (!ack) {
          log(`No ACK seen yet for ${r.command_id} (poll timeout).`);
          setStatus('Sent (no ACK yet)');
        } else {
          const st = String(ack.status || '').toLowerCase();
          if (st === 'completed') {
            setStatus('Completed');
            setSpeakProgress('completed', `Command ID: ${r.command_id}`);
          } else if (st) {
            setStatus(st.charAt(0).toUpperCase() + st.slice(1));
            setSpeakProgress(st, `Command ID: ${r.command_id}`);
          }
        }
      } else {
        setStatus('Sent');
      }
    } catch(e){
      setStatus('Error');
      setSpeakProgress('error', e.message);
      log(`Speak failed: ${e.message}`);
    } finally {
      $("speakBtn").disabled = false;
      if ($("cancelBtn")) $("cancelBtn").disabled = false;
      if ($("refreshBtn")) $("refreshBtn").disabled = false;
      if ($("device")) $("device").disabled = false;
      if ($("voice")) $("voice").disabled = false;
    }
  });

  $("cancelBtn").addEventListener('click', async () => {
    const device_id = $("device").value || '';
    const mode = (document.getElementById('cancelMode')?.value || 'stop');
    if (!device_id) { log('Pick a device first.'); return; }

    try {
      $("cancelBtn").disabled = true;
      if ($("speakBtn")) $("speakBtn").disabled = true;
      setStatus('Canceling…');
      log(`Cancel: device=${device_id}, mode=${mode}`);
      const r = await api('/ui/cancel', 'POST', { device_id, mode });
      log(`Cancel sent: ${JSON.stringify(r)}`);
      // --- Poll for device ACK after cancel command ---
      if (r && r.command_id) {
        setStatus(`Cancel sent (command_id=${r.command_id})`);

        const ack = await waitForAck(device_id, r.command_id, 15000, (a) => {
          log(`Cancel ACK: ${JSON.stringify(a)}`);
          const st = String(a.status || '').toLowerCase();
          if (st === 'accepted') setStatus('Cancel accepted');
          else if (st) setStatus(`Cancel ${st}`);
        });

        if (!ack) {
          log(`No cancel ACK seen yet for ${r.command_id} (poll timeout).`);
          setStatus('Cancel sent (no ACK yet)');
        } else {
          const st = String(ack.status || '').toLowerCase();
          if (st === 'completed') setStatus('Cancel completed');
          else setStatus(`Cancel ${st}`);
        }
      } else {
        setStatus('Cancel sent');
      }
    } catch(e) {
      setStatus('Error');
      log(`Cancel failed: ${e.message}`);
    } finally {
      $("cancelBtn").disabled = false;
      if ($("speakBtn")) $("speakBtn").disabled = false;
    }
  });

  const savePhraseBtn = $("savePhraseBtn");
  if (savePhraseBtn) savePhraseBtn.addEventListener('click', async () => {
    const label = ($("phraseLabel")?.value || '').trim();
    const text = ($("text")?.value || '').trim();
    if (!label) { setPhraseStatus('Label is required.'); return; }
    if (!text) { setPhraseStatus('Text is required.'); return; }
    try {
      setPhraseStatus('Saving…');
      const body = { label, text };
      if (selectedPhraseId) body.id = selectedPhraseId;
      const r = await api('/ui/phrases', 'POST', body);
      setSelectedPhrase(r.id);
      setPhraseStatus('Saved.');
      const phrases = await loadPhrases();
      renderPhrases(phrases);
    } catch(e){
      setPhraseStatus('Save failed.');
      log(`Phrase save failed: ${e.message}`);
    }
  });

  const deletePhraseBtn = $("deletePhraseBtn");
  if (deletePhraseBtn) deletePhraseBtn.addEventListener('click', async () => {
    if (!selectedPhraseId) return;
    try {
      setPhraseStatus('Deleting…');
      await api('/ui/phrases/delete', 'POST', { id: selectedPhraseId });
      setSelectedPhrase('');
      if ($("phraseLabel")) $("phraseLabel").value = '';
      setPhraseStatus('Deleted.');
      const phrases = await loadPhrases();
      renderPhrases(phrases);
    } catch(e){
      setPhraseStatus('Delete failed.');
      log(`Phrase delete failed: ${e.message}`);
    }
  });

  const saveDevBtn = $("saveDeviceCfgBtn");
  if (saveDevBtn) saveDevBtn.addEventListener('click', async () => {
    const device_id = $("device").value || '';
    const friendly_name = (document.getElementById('friendlyName')?.value || '').trim();
    const default_voice_id = $("voice").value || '';
    if (!device_id) { setDevCfgStatus('Pick a device first.'); return; }
    try {
      setDevCfgStatus('Saving…');
      await api('/ui/device/config', 'POST', { device_id, friendly_name, default_voice_id });
      setDevCfgStatus('Saved.');
      await refreshAll(false);
    } catch(e){
      setDevCfgStatus('Save failed.');
      log(`Device config save failed: ${e.message}`);
    }
  });

  const textArea = $("text");
  if (textArea) {
    textArea.addEventListener('input', updateCharCount);
    updateCharCount();
  }

  // --- Saved Audio Playback: play previously generated audio on the selected device ---
  document.addEventListener('click', async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('button.savedAudioPlay,button.freqAudioPlay') : null;
    if (!btn) return;

    const key = btn.getAttribute('data-key') || '';
    const isFreq = btn.classList.contains('freqAudioPlay') || isFreqKey(key);
    const device_id = $("device").value || '';
    if (!device_id) { log('Pick a device first.'); return; }
    if (!key) { log('Missing saved-audio key.'); return; }

    try {
      btn.disabled = true;
      (isFreq ? setFreqAudioPlayStatus : setSavedAudioPlayStatus)(`Playing: ${isFreq ? `#${key.replace('freq_','')}` : saLabel(key)}…`);
      log(`SavedAudio play: device=${device_id}, key=${key}`);

      const r = await api('/ui/saved-audio/play', 'POST', { device_id, key });
      log(`SavedAudio play queued: ${JSON.stringify(r)}`);
      (isFreq ? setFreqAudioPlayStatus : setSavedAudioPlayStatus)(`Sent.`);

      // Optionally poll for ACK if a command_id is returned
      if (r && r.command_id) {
        const ack = await waitForAck(device_id, r.command_id, 30000, (a) => {
          log(`SavedAudio ACK: ${JSON.stringify(a)}`);
        });
        if (ack && String(ack.status || '').toLowerCase() === 'completed') {
          (isFreq ? setFreqAudioPlayStatus : setSavedAudioPlayStatus)(`Completed.`);
        }
      }
    } catch(e) {
      (isFreq ? setFreqAudioPlayStatus : setSavedAudioPlayStatus)(`Play failed: ${e.message}`);
      log(`SavedAudio play failed: ${e.message}`);
    } finally {
      btn.disabled = false;
      setTimeout(() => (isFreq ? setFreqAudioPlayStatus : setSavedAudioPlayStatus)(''), 1200);
    }
  });

  // --- Saved Audio Generation: generate/store audio on hub for later playback ---
  document.addEventListener('click', async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest('button.savedAudioGen,button.freqAudioGen') : null;
    if (!btn) return;

    const key = btn.getAttribute('data-key') || '';
    const isFreq = btn.classList.contains('freqAudioGen') || isFreqKey(key);
    const device_id = $("device").value || '';
    const voice_id = $("voice").value || '';
    const speed = getCurrentSpeed();
    let inp = null;
    const cls = isFreq ? 'freqAudioText' : 'savedAudioText';
    try {
      inp = document.querySelector(`input.${cls}[data-key="${(window.CSS && CSS.escape) ? CSS.escape(key) : key}"]`);
    } catch(e) {
      // Fallback: find by iterating
      document.querySelectorAll(`input.${cls}`).forEach(x => {
        if (!inp && (x.getAttribute('data-key') || '') === key) inp = x;
      });
    }
    const text = (inp && inp.value) ? String(inp.value).trim() : '';

    if (!device_id) { log('Pick a device first.'); return; }
    if (!voice_id) { log('Pick a voice first.'); return; }
    if (!key) { log('Missing saved-audio key.'); return; }
    if (!text) { (isFreq ? setFreqAudioGenStatus : setSavedAudioGenStatus)('Text is required.'); return; }

    try {
      btn.disabled = true;
      (isFreq ? setFreqAudioGenStatus : setSavedAudioGenStatus)(`Generating…`);
      log(`SavedAudio generate: device=${device_id}, voice=${voice_id}, key=${key}, chars=${text.length}`);

      const r = await api('/ui/saved-audio/generate', 'POST', { device_id, key, text, tts: { voice_id, speed } });
      log(`SavedAudio generated: ${JSON.stringify(r)}`);
      (isFreq ? setFreqAudioGenStatus : setSavedAudioGenStatus)(`Saved.`);

      // Refresh saved-audio availability (if endpoint exists)
      await loadSavedAudioDefs();
    } catch(e) {
      (isFreq ? setFreqAudioGenStatus : setSavedAudioGenStatus)(`Generate failed: ${e.message}`);
      log(`SavedAudio generate failed: ${e.message}`);
    } finally {
      btn.disabled = false;
      setTimeout(() => (isFreq ? setFreqAudioGenStatus : setSavedAudioGenStatus)(''), 1400);
    }
  });

  refreshAll(false).catch(e => log(`Init failed: ${e.message}`));
})();
