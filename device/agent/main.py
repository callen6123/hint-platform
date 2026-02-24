import json
import time
import subprocess
import socket
import hashlib
import shutil
import tempfile
import urllib.request
import os
import sys
import signal
import threading
import queue
from pathlib import Path

import paho.mqtt.client as mqtt

PROVISION_PATH = Path("/etc/hint/provision.json")


AUDIO_PATH = Path("/opt/hint/audio/demo.wav")

# Audio cache and speak config
AUDIO_CACHE_DIR = Path(os.environ.get("HINT_AUDIO_CACHE_DIR", "/opt/hint/audio_cache"))
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SPEAK_DOWNLOAD_TIMEOUT = int(os.environ.get("HINT_SPEAK_DOWNLOAD_TIMEOUT_SEC", "30"))
SPEAK_MAX_BYTES = int(os.environ.get("HINT_SPEAK_MAX_BYTES", str(10 * 1024 * 1024)))  # 10MB

# Audio output selection
# - If HINT_AUDIO_CARDID is set (e.g., "Device"), we prefer that ALSA card id.
# - Otherwise we auto-detect a USB card from /proc/asound/cards.
HINT_AUDIO_CARDID = os.environ.get("HINT_AUDIO_CARDID", "").strip() or None

# Optional ALSA device string override. If set, used as-is for aplay -D.
# Examples: "plughw:Device,0", "default", "sysdefault:CARD=Device"
HINT_AUDIO_ALSA_DEVICE = os.environ.get("HINT_AUDIO_ALSA_DEVICE", "").strip() or None

# State publish behavior
HEARTBEAT_SEC = int(os.environ.get("HINT_STATE_HEARTBEAT_SEC", "30"))
HEARTBEAT_SEC = max(5, min(HEARTBEAT_SEC, 3600))
STATE_DEBOUNCE_SEC = float(os.environ.get("HINT_STATE_DEBOUNCE_SEC", "1.0"))
STATE_DEBOUNCE_SEC = max(0.0, min(STATE_DEBOUNCE_SEC, 10.0))

# Hub reachability health (device -> hub HTTP)
HINT_HUB_HEALTH_URL = os.environ.get("HINT_HUB_HEALTH_URL", "http://hinthub.local:5000/audio/health").strip()
HINT_HUB_HEALTH_INTERVAL_SEC = float(os.environ.get("HINT_HUB_HEALTH_INTERVAL_SEC", "20"))
HINT_HUB_HEALTH_INTERVAL_SEC = max(5.0, min(HINT_HUB_HEALTH_INTERVAL_SEC, 300.0))
HINT_HUB_HEALTH_TIMEOUT_SEC = float(os.environ.get("HINT_HUB_HEALTH_TIMEOUT_SEC", "2.5"))
HINT_HUB_HEALTH_TIMEOUT_SEC = max(0.5, min(HINT_HUB_HEALTH_TIMEOUT_SEC, 10.0))

# OTA paths
OTA_STAGING = Path("/opt/hint/agent_staging")
OTA_RELEASES = Path("/opt/hint/agent_releases")
AGENT_CURRENT = Path("/opt/hint/agent_current")
SETUP_RELEASES = Path("/opt/hint/device_setup_releases")
SETUP_CURRENT = Path("/opt/hint/device_setup_current")

def load_provision() -> dict:
    with PROVISION_PATH.open("r") as f:
        return json.load(f)


def _symlink_target_name(p: Path) -> str | None:
    """Return the final path component of the resolved symlink target, or None."""
    try:
        return p.resolve().name
    except Exception:
        return None


def current_versions() -> dict:
    agent_ver = _symlink_target_name(AGENT_CURRENT)
    setup_ver = _symlink_target_name(SETUP_CURRENT)

    # If symlinks still point to the legacy fixed folders, normalize to "legacy"
    if agent_ver == "agent":
        agent_ver = "legacy"
    if setup_ver == "device_setup":
        setup_ver = "legacy"

    return {
        "agent_version": agent_ver,
        "setup_version": setup_ver,
    }


def _find_usb_card() -> str | None:
    # Parse /proc/asound/cards. Example lines:
    #  0 [Device         ]: USB-Audio - USB Audio Device
    # If HINT_AUDIO_CARDID is set, prefer that exact card id.
    try:
        txt = Path("/proc/asound/cards").read_text()
    except Exception:
        return None

    # First pass: exact match to configured card id
    if HINT_AUDIO_CARDID:
        for line in txt.splitlines():
            if "[" in line and "]" in line and ":" in line:
                cardid = line.split("[", 1)[1].split("]", 1)[0].strip()
                if cardid == HINT_AUDIO_CARDID:
                    return cardid

    # Second pass: first USB-ish card
    for line in txt.splitlines():
        if "[" in line and "]" in line and ":" in line:
            if "USB" in line or "usb" in line:
                cardid = line.split("[", 1)[1].split("]", 1)[0].strip()
                if cardid:
                    return cardid

    return None


def _aplay_cmd_for(path: Path) -> list[str]:
    """Return the *primary* player command for a file."""

    # Explicit override: the user can force a full ALSA device string.
    if HINT_AUDIO_ALSA_DEVICE:
        if path.suffix.lower() == ".mp3" and shutil.which("mpg123"):
            return ["mpg123", str(path)]
        return ["aplay", "-D", HINT_AUDIO_ALSA_DEVICE, str(path)]

    card = _find_usb_card()

    if path.suffix.lower() == ".mp3" and shutil.which("mpg123"):
        if card:
            return ["mpg123", "-a", f"plughw:{card},0", str(path)]
        return ["mpg123", str(path)]

    if card:
        # Use plughw so ALSA can convert formats (e.g., U8->S16_LE) for USB devices.
        return ["aplay", "-D", f"plughw:{card},0", str(path)]

    return ["aplay", str(path)]

def _player_cmd_candidates(path: Path) -> list[list[str]]:
    """Return a list of player commands to try in order."""
    primary = _aplay_cmd_for(path)
    candidates: list[list[str]] = [primary]

    # WAV fallbacks
    if primary and primary[0] == "aplay":
        # If primary uses -D <device>, try default device, then bare.
        if "-D" in primary:
            candidates.append(["aplay", "-D", "default", str(path)])
        candidates.append(["aplay", str(path)])

        # PulseAudio fallback if present (WAV)
        if shutil.which("paplay") and path.suffix.lower() in {".wav", ".wave"}:
            candidates.append(["paplay", str(path)])

    # MP3 fallbacks
    if primary and primary[0] == "mpg123":
        candidates.append(["mpg123", str(path)])

    # De-dup while preserving order
    uniq: list[list[str]] = []
    seen = set()
    for c in candidates:
        t = tuple(c)
        if t in seen:
            continue
        seen.add(t)
        uniq.append(c)

    return uniq

# --- Helper: sniff audio type by magic bytes
def sniff_audio_type(path: Path) -> str:
    """Return 'wav', 'mp3', or 'unknown' based on file magic."""
    try:
        with path.open("rb") as f:
            header = f.read(12)
    except Exception:
        return "unknown"

    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "wav"

    # MP3: ID3 tag or MPEG frame sync
    if header.startswith(b"ID3"):
        return "mp3"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return "mp3"

    return "unknown"


def play_file(path: Path) -> None:
    if not path.exists():
        print(f"[AUDIO] missing file: {path}")
        return

    primary = _aplay_cmd_for(path)

    # Fallbacks: try default device if a specific ALSA device fails.
    fallbacks: list[list[str]] = []

    if primary and primary[0] == "aplay":
        # If primary uses -D plughw:..., try 'default' and finally bare 'aplay'.
        if "-D" in primary:
            fallbacks.append(["aplay", "-D", "default", str(path)])
        fallbacks.append(["aplay", str(path)])

    if primary and primary[0] == "mpg123":
        # If primary uses -a plughw:..., try without explicit device.
        fallbacks.append(["mpg123", str(path)])

    cmd_candidates = [primary] + fallbacks

    # Retry a few times to handle USB “not ready” moments
    for attempt in range(1, 6):
        last_err = ""
        last_out = ""
        last_rc = 0
        last_cmd: list[str] = []

        for cmd in cmd_candidates:
            last_cmd = cmd
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                print(f"[AUDIO] played {path} via {cmd}")
                return

            last_rc = res.returncode
            last_err = (res.stderr or "").strip()
            last_out = (res.stdout or "").strip()

        err = last_err
        out = last_out
        print(f"[AUDIO] attempt {attempt}/5 failed rc={last_rc} cmd={last_cmd}")
        if out:
            print(f"[AUDIO] stdout: {out}")
        if err:
            print(f"[AUDIO] stderr: {err}")

        # If the WAV is in an unsupported PCM format, try converting to 16-bit PCM WAV.
        # (Some USB DACs only advertise S16_LE.)
        if ("Sample format non available" in err) and shutil.which("sox"):
            try:
                converted = path.with_suffix(".s16.wav")
                conv_cmd = [
                    "sox",
                    str(path),
                    "-b",
                    "16",
                    "-e",
                    "signed-integer",
                    str(converted),
                ]
                conv = subprocess.run(conv_cmd, capture_output=True, text=True)
                if conv.returncode == 0 and converted.exists():
                    print(f"[AUDIO] converted to 16-bit PCM via {conv_cmd}: {converted}")

                    primary2 = _aplay_cmd_for(converted)
                    fallbacks2: list[list[str]] = []
                    if primary2 and primary2[0] == "aplay":
                        if "-D" in primary2:
                            fallbacks2.append(["aplay", "-D", "default", str(converted)])
                        fallbacks2.append(["aplay", str(converted)])
                    if primary2 and primary2[0] == "mpg123":
                        fallbacks2.append(["mpg123", str(converted)])

                    for cmd2 in [primary2] + fallbacks2:
                        res2 = subprocess.run(cmd2, capture_output=True, text=True)
                        if res2.returncode == 0:
                            print(f"[AUDIO] played converted {converted} via {cmd2}")
                            return

                    # If we got here, all converted attempts failed; log last result
                    err2 = (res2.stderr or "").strip() if 'res2' in locals() else ""
                    out2 = (res2.stdout or "").strip() if 'res2' in locals() else ""
                    print(f"[AUDIO] converted play failed rc={res2.returncode if 'res2' in locals() else 'n/a'} cmd={cmd2 if 'cmd2' in locals() else 'n/a'}")
                    if out2:
                        print(f"[AUDIO] stdout: {out2}")
                    if err2:
                        print(f"[AUDIO] stderr: {err2}")
            except Exception as e:
                print(f"[AUDIO] conversion fallback failed: {e}")

        time.sleep(0.5)

    print("[AUDIO] giving up after retries")


def play() -> None:
    play_file(AUDIO_PATH)

def _write_beep_wav(dst: Path, *, hz: float = 880.0, duration_sec: float = 0.25, rate: int = 16000) -> None:
    """Generate a short 16-bit PCM mono beep WAV."""
    import math
    import wave
    import struct

    n = max(1, int(duration_sec * rate))
    amp = 0.25  # keep it quiet-ish

    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(rate)
        for i in range(n):
            t = i / rate
            s = math.sin(2.0 * math.pi * hz * t)

            # short fade-in/out to avoid clicks
            fade = 1.0
            edge = int(0.02 * rate)
            if i < edge:
                fade = i / max(1, edge)
            elif i > (n - edge):
                fade = max(0.0, (n - i) / max(1, edge))

            v = int(max(-1.0, min(1.0, s * amp * fade)) * 32767)
            w.writeframesraw(struct.pack("<h", v))

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str]) -> None:
    subprocess.check_call(cmd)


def do_ota_update(prov: dict) -> dict:
    ota = prov.get("ota")
    if not isinstance(ota, dict):
        raise RuntimeError("No OTA info in provision.json")

    base = ota.get("base_url")
    device_id = ota.get("device_id")
    token = ota.get("token")

    if not base or not device_id or not token:
        raise RuntimeError("Incomplete OTA credentials")

    params = f"device_id={device_id}&token={token}"
    latest_url = f"{base}/ota/latest?{params}"

    # Fetch manifest
    with urllib.request.urlopen(latest_url, timeout=30) as r:
        latest = json.loads(r.read().decode("utf-8"))

    ver = latest["version"]
    fname = latest["filename"]
    want_sha = latest["sha256"]

    cur = current_versions().get("agent_version")
    if cur == ver:
        return {
            "result": "already_latest",
            "from_version": cur,
            "to_version": ver,
        }

    file_url = f"{base}/ota/file/{fname}?{params}"

    OTA_STAGING.mkdir(parents=True, exist_ok=True)
    OTA_RELEASES.mkdir(parents=True, exist_ok=True)
    SETUP_RELEASES.mkdir(parents=True, exist_ok=True)

    tmp = OTA_STAGING / fname

    # Download bundle
    with urllib.request.urlopen(file_url, timeout=60) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f)

    # Verify checksum
    got = sha256_file(tmp)
    if got != want_sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum mismatch: {got} != {want_sha}")

    # Extract
    work = Path(tempfile.mkdtemp(prefix="hint-ota-", dir=str(OTA_STAGING)))
    run(["tar", "-xzf", str(tmp), "-C", str(work)])

    dev = work / "device"
    agent_src = dev / "agent"
    setup_src = dev / "device_setup"

    if not agent_src.exists() or not setup_src.exists():
        raise RuntimeError("Invalid OTA bundle layout")

    agent_dst = OTA_RELEASES / ver
    setup_dst = SETUP_RELEASES / ver

    if agent_dst.exists():
        shutil.rmtree(agent_dst)
    if setup_dst.exists():
        shutil.rmtree(setup_dst)

    shutil.copytree(agent_src, agent_dst)
    shutil.copytree(setup_src, setup_dst)

    # Atomically switch
    AGENT_CURRENT.unlink(missing_ok=True)
    AGENT_CURRENT.symlink_to(agent_dst)

    SETUP_CURRENT.unlink(missing_ok=True)
    SETUP_CURRENT.symlink_to(setup_dst)

    # Restart setup so provisioning UI (if ever used again) runs the updated code.
    # Do NOT restart hint-agent from inside itself; we will exit after ACK and let systemd restart.
    run(["systemctl", "restart", "hint-setup"])

    return {
        "result": "updated",
        "from_version": cur,
        "to_version": ver,
    }

def download_speak_audio(url: str, *, audio_id: str | None = None) -> Path:
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Choose filename
    safe_id = (audio_id or f"speak-{int(time.time())}").replace("/", "_").replace("..", "_")
    # Start as .bin and rename once we inspect magic bytes
    out_path = AUDIO_CACHE_DIR / f"{safe_id}.bin"

    req = urllib.request.Request(url, headers={"User-Agent": "hint-agent/1"})
    with urllib.request.urlopen(req, timeout=SPEAK_DOWNLOAD_TIMEOUT) as r:
        # Basic size guard
        clen = r.headers.get("Content-Length")
        if clen is not None:
            try:
                if int(clen) > SPEAK_MAX_BYTES:
                    raise RuntimeError(f"audio_too_large: {clen} bytes")
            except ValueError:
                pass

        # Stream to temp then atomic move
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        total = 0
        with tmp.open("wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > SPEAK_MAX_BYTES:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"audio_too_large: exceeded {SPEAK_MAX_BYTES} bytes")
                f.write(chunk)

        tmp.replace(out_path)

        # Decide final extension by magic bytes (never let MP3 be named .wav)
        try:
            atype = sniff_audio_type(out_path)
            if atype == "wav":
                final_path = out_path.with_suffix(".wav")
                out_path.rename(final_path)
                return final_path
            if atype == "mp3":
                final_path = out_path.with_suffix(".mp3")
                out_path.rename(final_path)
                print(f"[AUDIO] downloaded MP3; saved as {final_path}")
                return final_path

            # Unknown: show a short preview for debugging
            with out_path.open("rb") as f:
                preview = f.read(32)
            raise RuntimeError(f"invalid_audio_format: magic={preview!r}")
        except Exception:
            out_path.unlink(missing_ok=True)
            raise

    return out_path

def main() -> None:
    prov = load_provision()
    topics = prov["topics"]
    cmd_t = topics["cmd"]
    ack_t = topics["ack"]
    state_t = topics["state"]

    device_id = prov.get("device_id", socket.gethostname())

    # --- Hub reachability state (from device perspective)
    hub_lock = threading.Lock()
    hub_reachable: bool | None = None
    hub_last_ok_at: int | None = None
    hub_last_err_at: int | None = None
    hub_last_err: str | None = None
    hub_rtt_ms: int | None = None

    def _check_hub_once() -> tuple[bool, int | None, str | None]:
        """Return (ok, rtt_ms, err)."""
        if not HINT_HUB_HEALTH_URL:
            return True, None, None
        t0 = time.time()
        try:
            req = urllib.request.Request(
                HINT_HUB_HEALTH_URL,
                headers={"User-Agent": "hint-agent/health"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=HINT_HUB_HEALTH_TIMEOUT_SEC) as r:
                # Any 2xx is considered OK.
                code = getattr(r, "status", None) or r.getcode()
                if code < 200 or code >= 300:
                    raise RuntimeError(f"http_{code}")
                # Read a small amount to ensure the connection is usable.
                r.read(256)
            rtt = int((time.time() - t0) * 1000)
            return True, rtt, None
        except Exception as e:
            rtt = int((time.time() - t0) * 1000)
            return False, rtt, str(e)

    def hub_health_loop() -> None:
        nonlocal hub_reachable, hub_last_ok_at, hub_last_err_at, hub_last_err, hub_rtt_ms
        while True:
            ok, rtt, err = _check_hub_once()
            now_i = int(time.time())
            with hub_lock:
                hub_reachable = ok
                hub_rtt_ms = rtt
                if ok:
                    hub_last_ok_at = now_i
                    hub_last_err_at = None
                    hub_last_err = None
                else:
                    hub_last_err_at = now_i
                    hub_last_err = (err or "unknown")[:200]
            # Nudge the hub with a retained state refresh when this flips.
            try:
                # force=True ensures it publishes even if status hasn't changed.
                publish_state(last_state or "IDLE", force=True)
            except Exception:
                pass
            time.sleep(HINT_HUB_HEALTH_INTERVAL_SEC)

    # (Hub health thread will be started after MQTT client is ready.)

    last_state: str | None = None
    last_state_publish_ts: float = 0.0

    # --- Speak/Play job queue (single-consumer)
    job_q: "queue.Queue[dict]" = queue.Queue()
    q_lock = threading.Lock()

    # Currently-playing process/job (protected by q_lock)
    current_proc: subprocess.Popen | None = None
    current_job: dict | None = None

    # Playback/cancel observability
    playback_state: str = "idle"  # idle|starting|playing|stopping
    cancel_event = threading.Event()

    def _kill_proc(p: subprocess.Popen) -> None:
        try:
            if p.poll() is not None:
                return

            # Prefer killing the whole process group so children (if any) also stop.
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                p.terminate()

            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()
        except Exception:
            pass

    def cancel_jobs(*, mode: str = "stop", command_id: str | None = None) -> dict:
        """Cancel playback and/or queued jobs.

        Modes:
          - stop: stop current playback only
          - clear_queue: remove queued jobs only
          - stop_and_clear: stop current playback and remove queued jobs

        Back-compat: if command_id is provided, we will try to stop that command_id
        if it is currently playing, and remove matching queued jobs.
        """
        nonlocal current_proc, current_job, playback_state

        mode = (mode or "stop").strip().lower()
        if mode not in {"stop", "clear_queue", "stop_and_clear"}:
            mode = "stop"

        removed = 0
        stopped = False

        with q_lock:
            state_before = playback_state

            should_stop_current = False
            if current_job is not None:
                if command_id and current_job.get("command_id") == command_id:
                    should_stop_current = True
                elif mode in {"stop", "stop_and_clear"}:
                    should_stop_current = True

            if should_stop_current:
                cancel_event.set()

            if current_proc is not None and current_job is not None and should_stop_current:
                playback_state = "stopping"
                _kill_proc(current_proc)
                stopped = True

            state_after = playback_state

        # Drain queue; optionally remove jobs depending on mode/command_id.
        keep: list[dict] = []
        while True:
            try:
                j = job_q.get_nowait()
            except queue.Empty:
                break

            remove_this = False

            if command_id:
                # Targeted cancel: remove only matching queued jobs.
                remove_this = (j.get("command_id") == command_id)
            else:
                # Mode-based cancel.
                if mode in {"clear_queue", "stop_and_clear"}:
                    remove_this = True

            if remove_this:
                removed += 1
                job_q.task_done()
                continue

            keep.append(j)
            job_q.task_done()

        for j in keep:
            job_q.put(j)

        details = {
            "mode": mode,
            "target_command_id": command_id,
            "stopped": stopped,
            "removed": removed,
            "state_before": state_before,
            "state_after": state_after,
        }
        print(f"[CANCEL] {details}")
        return details

    def enqueue_job(job: dict) -> None:
        job_q.put(job)

    def _spawn_player(cmd: list[str]) -> subprocess.Popen:
        """Spawn a player process for a given command list."""
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def _wait_player(p: subprocess.Popen) -> tuple[int, str, str]:
        """Wait for player and return (rc, stdout, stderr)."""
        rc = p.wait()
        out = (p.stdout.read() if p.stdout else "").strip()
        err = (p.stderr.read() if p.stderr else "").strip()
        return rc, out, err

    def worker_loop() -> None:
        nonlocal current_proc, current_job, playback_state

        while True:
            job = job_q.get()
            try:
                cancel_event.clear()

                with q_lock:
                    current_job = job
                    playback_state = "starting"

                publish_state("PLAYING")

                cid = str(job.get("command_id", ""))
                jtype = job.get("type")

                if jtype == "play":
                    path = AUDIO_PATH
                    atype = sniff_audio_type(path)

                elif jtype == "speak":
                    url = job.get("url")
                    audio_id = job.get("audio_id")
                    if not url:
                        raise RuntimeError("missing_speak_url")

                    path = download_speak_audio(url, audio_id=str(audio_id) if audio_id else None)
                    atype = sniff_audio_type(path)

                elif jtype == "beep":
                    path = AUDIO_CACHE_DIR / f"beep-{int(time.time())}.wav"
                    _write_beep_wav(path)
                    atype = "wav"

                else:
                    raise RuntimeError(f"unknown_job_type:{jtype}")

                # Spawn player so we can cancel it mid-playback.
                # Try a fallback chain (preferred ALSA device -> default -> bare -> paplay).
                cmd_candidates = _player_cmd_candidates(path)

                rc = 1
                out = ""
                err = ""
                cmd_used: list[str] | None = None

                for cmd in cmd_candidates:
                    cmd_used = cmd
                    p = _spawn_player(cmd)

                    with q_lock:
                        current_proc = p
                        playback_state = "playing"

                    # If cancel came in during the small window between job start and spawn,
                    # stop immediately and treat as cancelled.
                    if cancel_event.is_set():
                        with q_lock:
                            playback_state = "stopping"
                        _kill_proc(p)

                    rc, out, err = _wait_player(p)

                    # If cancelled, stop trying fallbacks.
                    if cancel_event.is_set() or playback_state == "stopping":
                        break

                    if rc == 0:
                        break

                    # If WAV format is not supported, convert to 16-bit PCM WAV via sox and retry chain.
                    if (
                        path.suffix.lower() in {".wav", ".wave"}
                        and "Sample format non available" in (err or "")
                        and shutil.which("sox")
                    ):
                        try:
                            converted = path.with_suffix(".s16.wav")
                            conv_cmd = ["sox", str(path), "-b", "16", "-e", "signed-integer", str(converted)]
                            conv = subprocess.run(conv_cmd, capture_output=True, text=True)
                            if conv.returncode == 0 and converted.exists():
                                print(f"[AUDIO] converted to 16-bit PCM via {conv_cmd}: {converted}")
                                path = converted
                                cmd_candidates = _player_cmd_candidates(path)
                                continue
                        except Exception as _e:
                            print(f"[AUDIO] conversion fallback failed: {_e}")

                # Decide cancellation before clearing state.
                with q_lock:
                    was_cancelled = cancel_event.is_set() or (playback_state == "stopping")
                    current_proc = None
                    playback_state = "idle"

                if cmd_used:
                    if rc == 0:
                        print(f"[AUDIO] played {path} via {cmd_used}")
                    else:
                        print(f"[AUDIO] play failed rc={rc} via {cmd_used}")

                if out:
                    print(f"[AUDIO] stdout: {out}")
                if err:
                    print(f"[AUDIO] stderr: {err}")

                if was_cancelled:
                    # Clean cancellation outcome (not an error)
                    details = {
                        "cancelled": True,
                        "rc": rc,
                        "file": str(path),
                        "audio_type": atype,
                    }
                    if jtype == "speak":
                        details["audio_id"] = job.get("audio_id")
                    publish_ack(cid, "completed", details)
                    publish_state("IDLE")
                    continue

                if rc != 0:
                    raise RuntimeError(f"audio_play_failed_rc={rc}")

                # Success
                if jtype == "speak":
                    publish_ack(cid, "completed", {
                        "audio_id": job.get("audio_id"),
                        "file": str(path),
                        "audio_type": atype,
                    })
                else:
                    publish_ack(cid, "completed", {"file": str(path), "audio_type": atype})

                publish_state("IDLE")

            except Exception as e:
                with q_lock:
                    playback_state = "idle"
                publish_state("IDLE")
                try:
                    publish_ack(str(job.get("command_id", "")), "error", {"error": str(e)})
                except Exception:
                    pass
            finally:
                with q_lock:
                    current_job = None
                    current_proc = None
                    playback_state = "idle"
                job_q.task_done()

    worker = threading.Thread(target=worker_loop, name="hint-agent-worker", daemon=True)
    worker.start()

    def publish_state(status: str, *, force: bool = False) -> None:
        nonlocal last_state, last_state_publish_ts
        now = time.time()
        if not force:
            # If status is changing, allow immediate publish but debounce rapid flips.
            if last_state != status and (now - last_state_publish_ts) < STATE_DEBOUNCE_SEC:
                return
            # If status is the same, avoid re-sending unless heartbeat loop calls with force.
            if last_state == status:
                return

        payload = {"device_id": device_id, "status": status, "ts": int(now)}
        payload.update(current_versions())

        # Attach hub reachability snapshot
        try:
            with hub_lock:
                payload["hub_reachable"] = hub_reachable
                payload["hub_last_ok_at"] = hub_last_ok_at
                payload["hub_last_err_at"] = hub_last_err_at
                payload["hub_last_err"] = hub_last_err
                payload["hub_rtt_ms"] = hub_rtt_ms
                payload["hub_health_url"] = HINT_HUB_HEALTH_URL
        except Exception:
            pass

        c.publish(state_t, json.dumps(payload), qos=1, retain=True)
        last_state = status
        last_state_publish_ts = now

    mqtt_cfg = prov["mqtt"]
    host = mqtt_cfg["host"]
    port = int(mqtt_cfg.get("port", 1883))
    username = mqtt_cfg.get("username")
    password = mqtt_cfg.get("password")

    client_id = f"{device_id}-{socket.gethostname()}"
    c = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )

    # Last Will: mark offline if we disappear
    will_payload = {"device_id": device_id, "status": "OFFLINE", "ts": int(time.time())}
    will_payload.update(current_versions())
    c.will_set(state_t, json.dumps(will_payload), qos=1, retain=True)

    if username:
        c.username_pw_set(username, password)

    def publish_ack(command_id: str, status: str, details: dict | None = None) -> None:
        payload = {
            "device_id": device_id,
            "command_id": command_id,
            "status": status,
            "ts": int(time.time()),
        }
        payload.update(current_versions())
        if details is not None:
            payload["details"] = details

        # Include audio device selection snapshot for debugging
        try:
            payload["audio_cardid"] = _find_usb_card()
            if HINT_AUDIO_ALSA_DEVICE:
                payload["audio_alsa_device"] = HINT_AUDIO_ALSA_DEVICE
        except Exception:
            pass

        c.publish(ack_t, json.dumps(payload), qos=1, retain=False)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # Subscribe after connect so reconnects recover automatically
        client.subscribe(cmd_t, qos=1)
        # Mark online (retained)
        publish_state("ONLINE", force=True)

    def on_message(client, userdata, msg):
        # Payload may be bytes; decode safely
        raw = msg.payload.decode("utf-8", errors="replace")
        try:
            d = json.loads(raw)
        except Exception:
            return

        cid = d.get("command_id")
        if not cid:
            return

        publish_ack(str(cid), "accepted")

        try:
            cmd = d.get("cmd")

            if cmd == "update":
                publish_state("UPDATING")
                res = do_ota_update(prov)
                publish_ack(str(cid), "completed", res)
                publish_state("IDLE")

                # If we actually applied a new version, exit so systemd restarts on the new code.
                if res.get("result") == "updated":
                    time.sleep(0.5)
                    os._exit(0)

            elif cmd == "cancel":
                params = d.get("params")
                if not isinstance(params, dict):
                    params = {}

                # New API: params.mode in {stop, clear_queue, stop_and_clear}
                mode = params.get("mode")

                # Back-compat:
                # - params.all=true => stop_and_clear
                # - params.command_id=<id> => targeted cancel
                target_id = params.get("command_id")
                if mode is None:
                    if bool(params.get("all", False)):
                        mode = "stop_and_clear"
                    else:
                        mode = "stop"

                details = cancel_jobs(
                    mode=str(mode) if mode else "stop",
                    command_id=str(target_id) if target_id else None,
                )

                # Include current playback state snapshot
                with q_lock:
                    details["playback_state"] = playback_state
                    if current_job is not None:
                        details["current_command_id"] = current_job.get("command_id")

                publish_ack(str(cid), "completed", details)

            elif cmd == "play":
                # enqueue a local play job
                enqueue_job({"type": "play", "command_id": str(cid)})

            elif cmd == "speak":
                params = d.get("params")
                if not isinstance(params, dict):
                    params = {}

                url = params.get("url") or d.get("url")
                audio_id = params.get("audio_id") or d.get("audio_id")

                if not url or not isinstance(url, str):
                    raise RuntimeError("missing_speak_url")

                enqueue_job({
                    "type": "speak",
                    "command_id": str(cid),
                    "url": url,
                    "audio_id": audio_id,
                })
            elif cmd == "beep":
                enqueue_job({"type": "beep", "command_id": str(cid)})

            else:
                # Back-compat: treat unknown command as play test
                enqueue_job({"type": "play", "command_id": str(cid)})

        except Exception as e:
            publish_state("IDLE")
            publish_ack(str(cid), "error", {"error": str(e)})

    c.on_connect = on_connect
    c.on_message = on_message

    # Auto-reconnect behavior
    c.reconnect_delay_set(min_delay=1, max_delay=30)

    c.connect(host, port, keepalive=30)
    c.loop_start()

    # Start hub health thread (after MQTT client is ready; publish_state uses c.publish)
    threading.Thread(target=hub_health_loop, name="hub-health", daemon=True).start()

    # Heartbeat: keep a retained status so the hub can see liveness.
    # We already publish immediately on state changes; this only sends a periodic refresh
    # if nothing else has been published recently.
    while True:
        now = time.time()

        # If nothing has been published for HEARTBEAT_SEC, publish the current state.
        if (now - last_state_publish_ts) >= HEARTBEAT_SEC:
            if last_state is None:
                last_state = "IDLE"

            payload = {"device_id": device_id, "status": last_state, "ts": int(now)}
            payload.update(current_versions())

            # Attach hub reachability snapshot
            try:
                with hub_lock:
                    payload["hub_reachable"] = hub_reachable
                    payload["hub_last_ok_at"] = hub_last_ok_at
                    payload["hub_last_err_at"] = hub_last_err_at
                    payload["hub_last_err"] = hub_last_err
                    payload["hub_rtt_ms"] = hub_rtt_ms
                    payload["hub_health_url"] = HINT_HUB_HEALTH_URL
            except Exception:
                pass

            c.publish(state_t, json.dumps(payload), qos=1, retain=True)
            last_state_publish_ts = now

        # 1s tick keeps latency low for state changes while preventing spam.
        time.sleep(1)


if __name__ == "__main__":
    main()
