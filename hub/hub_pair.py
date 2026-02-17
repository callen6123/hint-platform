from flask import Flask, request, jsonify, send_file, abort
import os
import sys
import subprocess
import secrets
import string
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error

# Ensure repo root is on sys.path even when this file is executed as a script
# (e.g. ExecStart=/usr/bin/python3 /home/hub/hint-platform/hub/hub_pair.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Local hub modules
# audio_server.py: stores/serves generated audio with per-device token auth
# tts.py: ElevenLabs client wrapper
try:
    # When executed as a package module: python3 -m hub.hub_pair
    from hub.audio_server import bp as audio_bp, store_audio, build_audio_url
except Exception:
    try:
        # When executed as a script from /home/hub/hint-platform/hub
        from audio_server import bp as audio_bp, store_audio, build_audio_url
    except Exception:
        audio_bp = None
        store_audio = None
        build_audio_url = None

try:
    # Prefer package import
    from hub import tts
except Exception:
    try:
        import tts
    except Exception:
        tts = None

app = Flask(__name__)

# Register audio blueprint if available
if audio_bp is not None:
    app.register_blueprint(audio_bp)

# Base URL devices should use to fetch hub-hosted artifacts (OTA + audio)
HUB_BASE_URL = os.environ.get("HUB_BASE_URL", "http://hinthub.local:5000")

PAIR_CODE = "483921"  # you will type this into the device setup page

REGISTRY_DIR = Path("/var/lib/hint")
REGISTRY_PATH = REGISTRY_DIR / "devices.json"
OTA_DIR = Path("/opt/hint/ota")
BUILD_OTA_SCRIPT = Path("/home/hub/hint-platform/hub/build_ota.py")

HUB_REPO_DIR = Path("/home/hub/hint-platform")
HUB_UPDATE_SCRIPT = Path("/opt/hint/hub/update_hub.sh")
HUB_UPDATE_UNIT_TEMPLATE = Path("/etc/systemd/system/hint-hub-update@.service")

# In-memory rollout jobs (simple, single-hub process). For durability later, persist to REGISTRY_DIR.
ROLLOUTS: dict[str, dict] = {}

# Hub update check cache (avoid running git fetch on every /ui/status refresh)
_HUB_UPDATE_CACHE: dict = {
    "checked_at_ts": 0.0,
    "ttl_sec": 12 * 60 * 60,  # 12 hours
    "hub_version": None,
    "hub_latest_version": None,
    "hub_update_available": None,
    "error": None,
}

# ElevenLabs voices cache (avoid hitting API on every page load)
_VOICES_CACHE: dict = {
    "checked_at_ts": 0.0,
    "ttl_sec": 6 * 60 * 60,  # 6 hours
    "voices": None,          # list[dict]
    "error": None,
}

def _get_elevenlabs_voices_cached(force: bool = False) -> dict:
    """Return {ok, voices:[{voice_id,name,category,labels}], error, checked_at}."""
    now = time.time()
    ttl = float(_VOICES_CACHE.get("ttl_sec") or 0)
    last = float(_VOICES_CACHE.get("checked_at_ts") or 0)

    if (not force) and ttl > 0 and (now - last) < ttl and _VOICES_CACHE.get("voices") is not None:
        return {
            "ok": True,
            "voices": _VOICES_CACHE.get("voices") or [],
            "error": _VOICES_CACHE.get("error"),
            "checked_at": datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last else None,
        }

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        _VOICES_CACHE["checked_at_ts"] = now
        _VOICES_CACHE["voices"] = []
        _VOICES_CACHE["error"] = "missing_ELEVENLABS_API_KEY"
        return {
            "ok": False,
            "voices": [],
            "error": "missing_ELEVENLABS_API_KEY",
            "checked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }

    url = "https://api.elevenlabs.io/v1/voices"
    req = urllib.request.Request(url, headers={"xi-api-key": api_key, "Accept": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        voices = payload.get("voices") if isinstance(payload, dict) else None
        out_list: list[dict] = []
        if isinstance(voices, list):
            for v in voices:
                if not isinstance(v, dict):
                    continue
                vid = str(v.get("voice_id") or "").strip()
                name = str(v.get("name") or "").strip()
                if not vid or not name:
                    continue
                out_list.append({
                    "voice_id": vid,
                    "name": name,
                    "category": v.get("category"),
                    "labels": v.get("labels") if isinstance(v.get("labels"), dict) else {},
                })

        # Sort by name for UI
        out_list.sort(key=lambda x: (str(x.get("name") or "").lower(), str(x.get("voice_id") or "")))

        _VOICES_CACHE["checked_at_ts"] = now
        _VOICES_CACHE["voices"] = out_list
        _VOICES_CACHE["error"] = None

        return {
            "ok": True,
            "voices": out_list,
            "error": None,
            "checked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }

    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        _VOICES_CACHE["checked_at_ts"] = now
        _VOICES_CACHE["voices"] = []
        _VOICES_CACHE["error"] = str(e)
        return {
            "ok": False,
            "voices": [],
            "error": str(e),
            "checked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }
    except Exception as e:
        _VOICES_CACHE["checked_at_ts"] = now
        _VOICES_CACHE["voices"] = []
        _VOICES_CACHE["error"] = str(e)
        return {
            "ok": False,
            "voices": [],
            "error": str(e),
            "checked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }


def _rollout_new_id() -> str:
    return sanitize_topic_id(f"roll-{int(time.time())}-{secrets.token_hex(4)}")


def _device_ack_for(rec: dict, command_id: str) -> dict | None:
    ack = rec.get("last_ack") if isinstance(rec, dict) else None
    if isinstance(ack, dict) and ack.get("command_id") == command_id:
        return ack
    return None


def _advance_rollout(job: dict, reg: dict) -> None:
    """Advance rollout state by inspecting devices.json for matching last_ack."""
    if not job or job.get("state") not in ("running", "building"):
        return

    # If we were building, mark running once build completes (build occurs synchronously in start).
    if job.get("state") == "building":
        job["state"] = "running"

    inflight: dict = job.get("in_flight") or {}
    done: dict = job.get("done") or {}
    failed: dict = job.get("failed") or {}

    # Resolve in-flight devices
    # ACK timeout
    ack_timeout_sec = int(job.get("ack_timeout_sec") or 120)
    ack_timeout_sec = max(5, min(ack_timeout_sec, 3600))
    now_ts = time.time()

    resolved = []
    for did, v in list(inflight.items()):
        cid = v.get("cid") if isinstance(v, dict) else v
        sent_at = v.get("sent_at") if isinstance(v, dict) else None
        sent_ts = float(sent_at) if isinstance(sent_at, (int, float)) else None

        rec = reg.get(did)
        if not isinstance(rec, dict):
            continue

        # Timeout if no ACK within threshold
        if sent_ts is not None and (now_ts - sent_ts) > ack_timeout_sec:
            failed[did] = {
                "status": "error",
                "error": "ack_timeout",
                "command_id": cid,
                "details": {"timeout_sec": ack_timeout_sec},
                "ts": int(now_ts),
            }
            resolved.append(did)
            continue

        ack = _device_ack_for(rec, cid)
        if not ack:
            continue

        st = str(ack.get("status", "")).lower()
        if st == "completed":
            # Classify: updated vs already_latest (skipped)
            details = ack.get("details") if isinstance(ack.get("details"), dict) else {}
            result = str(details.get("result", "")).lower()
            if result == "already_latest":
                ack = dict(ack)
                ack["rollout_result"] = "skipped"
            elif result == "updated":
                ack = dict(ack)
                ack["rollout_result"] = "updated"
            else:
                ack = dict(ack)
                ack["rollout_result"] = "completed"
            done[did] = ack
            resolved.append(did)
        elif st == "error" or st.startswith("error"):
            failed[did] = ack
            resolved.append(did)

    for did in resolved:
        inflight.pop(did, None)

    job["in_flight"] = inflight
    job["done"] = done
    job["failed"] = failed

    # Safety: halt further dispatch if failures have reached threshold
    if job.get("stop_on_failure"):
        max_failures = int(job.get("max_failures") or 0)
        if max_failures == 0 and len(failed) > 0:
            job["state"] = "halted"
            job["halted_reason"] = f"failure:{len(failed)}"
            job["finished_at"] = utc_now_iso()
            return
        if max_failures > 0 and len(failed) >= max_failures:
            job["state"] = "halted"
            job["halted_reason"] = f"failures_reached:{len(failed)}>={max_failures}"
            job["finished_at"] = utc_now_iso()
            return

    # If nothing in-flight, dispatch next batch
    if inflight:
        return

    pending: list[str] = job.get("pending") or []
    if not pending:
        job["state"] = "completed" if not failed else "completed_with_failures"
        job["finished_at"] = utc_now_iso()
        return

    batch_size = int(job.get("batch_size") or 1)
    next_batch = pending[:batch_size]
    job["pending"] = pending[batch_size:]

    # Publish updates for the batch
    for did in next_batch:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            failed[did] = {"status": "error", "error": "missing_registry"}
            continue
        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))
        cid = sanitize_topic_id(f"{job['id']}-{did}")
        try:
            publish_update(site_id, did, cid)
            _mark_sent_in_registry(reg, did, cid, "update")
            inflight[did] = {"cid": cid, "sent_at": time.time()}
        except Exception as e:
            failed[did] = {"status": "error", "error": str(e)}

    if next_batch:
        write_registry(reg)
    job["in_flight"] = inflight
    job["failed"] = failed

    if job.get("stop_on_failure"):
        max_failures = int(job.get("max_failures") or 0)
        if max_failures == 0 and len(failed) > 0:
            job["state"] = "halted"
            job["halted_reason"] = f"failure:{len(failed)}"
            job["finished_at"] = utc_now_iso()
            return
        if max_failures > 0 and len(failed) >= max_failures:
            job["state"] = "halted"
            job["halted_reason"] = f"failures_reached:{len(failed)}>={max_failures}"
            job["finished_at"] = utc_now_iso()
            return

def sanitize_topic_id(s: str) -> str:
    # Keep MQTT topic segments simple and safe
    return "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in s)[:64]


def gen_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_mqtt_user(username: str, password: str) -> None:
    passwd_file = "/etc/mosquitto/hint/passwd"

    # Create or update user in password file
    subprocess.run(
        ["sudo", "mosquitto_passwd", "-b", passwd_file, username, password],
        check=True,
    )


def ensure_acl(device_id: str, username: str) -> None:
    acl_file = "/etc/mosquitto/hint/acl"
    base = f"site/demo/device/{device_id}"

    block = f"""
user {username}
topic read {base}/cmd
topic write {base}/ack
topic write {base}/state
"""

    # Append only if this user is not already present
    if os.path.exists(acl_file):
        # Read via sudo in case the file is not readable by the service user.
        res = subprocess.run(
            ["sudo", "cat", acl_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        if f"user {username}" in res.stdout:
            return

    subprocess.run(
        ["sudo", "tee", "-a", acl_file],
        input=("\n" + block.strip() + "\n").encode("utf-8"),
        check=True,
    )


def restart_mosquitto() -> None:
    subprocess.run(["sudo", "systemctl", "restart", "mosquitto"], check=True)


# Registry helpers
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_registry_dir() -> None:
    # Create /var/lib/hint owned by root:hub so the hub user can read it.
    subprocess.run(
        [
            "sudo",
            "install",
            "-d",
            "-m",
            "775",
            "-o",
            "root",
            "-g",
            "hub",
            str(REGISTRY_DIR),
        ],
        check=True,
    )


def read_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {}

    # Read via sudo in case permissions are tightened.
    res = subprocess.run(
        ["sudo", "cat", str(REGISTRY_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    txt = res.stdout.strip()
    if not txt:
        return {}
    try:
        d = json.loads(txt)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_registry(reg: dict) -> None:
    ensure_registry_dir()

    # Write atomically: write temp file as hub user, then sudo move into place.
    tmp_path = Path(f"/tmp/hint-devices-{os.getpid()}.json")
    tmp_path.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    subprocess.run(["sudo", "mv", str(tmp_path), str(REGISTRY_PATH)], check=True)
    subprocess.run(["sudo", "chown", "root:hub", str(REGISTRY_PATH)], check=True)
    subprocess.run(["sudo", "chmod", "664", str(REGISTRY_PATH)], check=True)


def upsert_device(device_id: str, site_id: str, mqtt_user: str, ota_token: str) -> None:
    reg = read_registry()
    rec = reg.get(device_id) if isinstance(reg, dict) else None
    if not isinstance(rec, dict):
        rec = {}

    rec.update(
        {
            "device_id": device_id,
            "site_id": site_id,
            "mqtt_user": mqtt_user,
            "ota_token": ota_token,
            "paired_at": utc_now_iso(),
            "last_seen": rec.get("last_seen"),
            "status": rec.get("status", "paired"),
        }
    )

    reg[device_id] = rec
    write_registry(reg)

@app.post("/pair/complete")
def pair_complete():
    data = request.get_json(force=True)
    if data.get("pair_code") != PAIR_CODE:
        return jsonify({"ok": False, "error": "Invalid pairing code"}), 401

    device_id = sanitize_topic_id(data.get("device_id") or "device")
    topic_base = f"site/demo/device/{device_id}"

    # Generate per-device MQTT credentials
    mqtt_user = f"dev_{device_id}"
    mqtt_pass = gen_password()

    ota_token = secrets.token_urlsafe(24)

    # Ensure broker has user + ACL
    ensure_mqtt_user(mqtt_user, mqtt_pass)
    ensure_acl(device_id, mqtt_user)
    restart_mosquitto()


    upsert_device(device_id=device_id, site_id="demo", mqtt_user=mqtt_user, ota_token=ota_token)

    return jsonify({
        "ok": True,
        "site_id": "demo",
        "device_id": device_id,
        "mqtt": {
            "host": "hinthub.local",
            "port": 1883,
            "username": mqtt_user,
            "password": mqtt_pass
        },
        "topics": {
            "cmd": f"{topic_base}/cmd",
            "ack": f"{topic_base}/ack",
            "state": f"{topic_base}/state"
        },
        "ota": {
            "base_url": "http://hinthub.local:5000",
            "device_id": device_id,
            "token": ota_token
        }
    })


# OTA authentication and endpoints
def require_ota_auth() -> tuple[str, dict]:
    device_id = sanitize_topic_id(request.args.get("device_id") or "")
    token = request.args.get("token") or ""
    if not device_id or not token:
        abort(403)

    reg = read_registry()
    rec = reg.get(device_id) if isinstance(reg, dict) else None
    if not isinstance(rec, dict):
        abort(403)

    if rec.get("ota_token") != token:
        abort(403)

    return device_id, rec


@app.get("/ota/latest")
def ota_latest():
    require_ota_auth()
    p = OTA_DIR / "latest.json"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="application/json")


@app.get("/ota/file/<path:filename>")
def ota_file(filename: str):
    require_ota_auth()

    # Only serve files inside OTA_DIR
    ota_root = OTA_DIR.resolve()
    p = (OTA_DIR / filename).resolve()
    if not str(p).startswith(str(ota_root)):
        abort(403)
    if not p.exists():
        abort(404)

    return send_file(str(p), mimetype="application/gzip")

# Dashboard-triggered fleet actions


def require_dashboard_key() -> None:
    want = os.environ.get("HINT_DASH_API_KEY")
    got = request.headers.get("X-Api-Key")
    if not want or not got or got != want:
        abort(403)


# UI auth (no secrets in browser). Protect /dashboard and /ui/* with HTTP Basic Auth.

def require_ui_auth() -> None:
    user = os.environ.get("HINT_UI_USER")
    pw = os.environ.get("HINT_UI_PASS")
    if not user or not pw:
        abort(403)

    auth = request.authorization
    if not auth or auth.username != user or auth.password != pw:
        resp = jsonify({"ok": False, "error": "auth_required"})
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = 'Basic realm="Hint Hub"'
        return resp


def mqtt_admin_cfg() -> tuple[str, int, str, str]:
    host = os.environ.get("HUB_MQTT_HOST", "localhost")
    port = int(os.environ.get("HUB_MQTT_PORT", "1883"))
    user = os.environ.get("HUB_MQTT_USER", "hubadmin")
    pw = os.environ.get("HUB_MQTT_PASS", "devpass123")
    return host, port, user, pw


# --- OTA/hub update helpers ---

def _read_ota_latest_version() -> str | None:
    """Return the OTA bundle version from /opt/hint/ota/latest.json (short git sha), or None."""
    p = OTA_DIR / "latest.json"
    if not p.exists():
        return None
    try:
        txt = p.read_text(encoding="utf-8").strip()
        if not txt:
            return None
        d = json.loads(txt)
        if isinstance(d, dict):
            v = d.get("version")
            if v:
                return sanitize_topic_id(str(v))
    except Exception:
        return None
    return None


def _git_run(args: list[str], cwd: Path) -> str:
    res = subprocess.run(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _hub_update_status_cached(force: bool = False) -> dict:
    """Check if hub has updates available (origin/main differs from HEAD) with caching."""
    now = time.time()
    ttl = float(_HUB_UPDATE_CACHE.get("ttl_sec") or 0)
    last = float(_HUB_UPDATE_CACHE.get("checked_at_ts") or 0)

    if (not force) and ttl > 0 and (now - last) < ttl:
        return {
            "hub_version": _HUB_UPDATE_CACHE.get("hub_version"),
            "hub_latest_version": _HUB_UPDATE_CACHE.get("hub_latest_version"),
            "hub_update_available": _HUB_UPDATE_CACHE.get("hub_update_available"),
            "hub_update_error": _HUB_UPDATE_CACHE.get("error"),
            "hub_checked_at": datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last else None,
        }

    out = {
        "hub_version": None,
        "hub_latest_version": None,
        "hub_update_available": None,
        "hub_update_error": None,
        "hub_checked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }

    try:
        # Current commit
        head = _git_run(["git", "rev-parse", "--short", "HEAD"], HUB_REPO_DIR)
        head = sanitize_topic_id(head)
        out["hub_version"] = head

        # Refresh remote refs (this is why we cache)
        _git_run(["git", "fetch", "--quiet", "origin"], HUB_REPO_DIR)

        # Remote main commit
        remote = _git_run(["git", "rev-parse", "--short", "origin/main"], HUB_REPO_DIR)
        remote = sanitize_topic_id(remote)
        out["hub_latest_version"] = remote

        if head and remote:
            out["hub_update_available"] = (head != remote)
        else:
            out["hub_update_available"] = None

    except Exception as e:
        out["hub_update_error"] = str(e)
        out["hub_update_available"] = None

    # Update cache
    _HUB_UPDATE_CACHE["checked_at_ts"] = now
    _HUB_UPDATE_CACHE["hub_version"] = out.get("hub_version")
    _HUB_UPDATE_CACHE["hub_latest_version"] = out.get("hub_latest_version")
    _HUB_UPDATE_CACHE["hub_update_available"] = out.get("hub_update_available")
    _HUB_UPDATE_CACHE["error"] = out.get("hub_update_error")

    return out


# --- Hub update helpers ---

def _sudo_run(cmd: list[str]) -> None:
    subprocess.run(["sudo"] + cmd, check=True)


def _start_hub_update_background() -> dict:
    """Kick off a hub update in the background and return a small status object.

    We must return BEFORE restarting hint-hub-pair or the HTTP request will get cut off.

    Strategy:
    - Prefer a root-owned template unit at /etc/systemd/system/hint-hub-update@.service.
    - Otherwise, use a root-owned script at /opt/hint/hub/update_hub.sh.
    - Otherwise, use a shell one-liner.
    """
    job_id = sanitize_topic_id(f"hubupd-{int(time.time())}-{secrets.token_hex(3)}")

    # Preferred: a root-owned template unit that runs the update script.
    # This is more lock-down friendly than sudo systemd-run.
    if HUB_UPDATE_UNIT_TEMPLATE.exists():
        # Use --no-block so we can return the HTTP response before the update job
        # restarts this service.
        _sudo_run(["systemctl", "start", "--no-block", f"hint-hub-update@{job_id}.service"])
        return {"ok": True, "job_id": job_id, "mode": "unit"}

    # Fallback: run the script in the background via systemd-run so it survives process restarts.
    if HUB_UPDATE_SCRIPT.exists():
        _sudo_run(
            [
                "systemd-run",
                f"--unit={job_id}",
                "--collect",
                "--no-ask-password",
                str(HUB_UPDATE_SCRIPT),
            ]
        )
        return {"ok": True, "job_id": job_id, "mode": "script"}

    # Fallback: systemd-run a shell command.
    cmd = (
        f"cd {str(HUB_REPO_DIR)} "
        "&& git pull --ff-only "
        "&& systemctl restart hint-device-watch "
        "&& systemctl restart hint-hub-pair"
    )
    _sudo_run(
        [
            "systemd-run",
            f"--unit={job_id}",
            "--collect",
            "--no-ask-password",
            "/bin/bash",
            "-lc",
            cmd,
        ]
    )
    return {"ok": True, "job_id": job_id, "mode": "inline"}

def make_command_payload(
    command_id: str,
    cmd: str,
    params: dict | None = None,
    ttl_sec: int | None = None,
    priority: int | None = None,
) -> dict:
    """Command Schema v1."""
    now = int(time.time())
    ttl = int(ttl_sec if ttl_sec is not None else 300)
    ttl = max(5, min(ttl, 3600))
    pr = int(priority if priority is not None else 5)
    pr = max(0, min(pr, 10))

    return {
        "schema": 1,
        "command_id": sanitize_topic_id(command_id),
        "cmd": str(cmd),
        "params": params if isinstance(params, dict) else {},
        "ttl_sec": ttl,
        "priority": pr,
        "issued_at": now,
    }


def _rec_recent_ids(rec: dict) -> list[str]:
    v = rec.get("recent_command_ids")
    if isinstance(v, list):
        out: list[str] = []
        for x in v:
            xs = sanitize_topic_id(str(x))
            if xs:
                out.append(xs)
        return out[:50]
    return []


def _mark_sent_in_registry(reg: dict, device_id: str, cmd_id: str, cmd: str) -> None:
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return

    cmd_id = sanitize_topic_id(cmd_id)
    if not cmd_id:
        return

    recent = _rec_recent_ids(rec)
    if cmd_id in recent:
        recent = [x for x in recent if x != cmd_id]
    recent.append(cmd_id)
    rec["recent_command_ids"] = recent[-20:]  # keep last 20

    rec["last_sent_command_id"] = cmd_id
    rec["last_sent_cmd"] = str(cmd)
    rec["last_sent_at"] = utc_now_iso()


def publish_command(site_id: str, device_id: str, payload: dict) -> None:
    host, port, user, pw = mqtt_admin_cfg()
    topic = f"site/{site_id}/device/{device_id}/cmd"
    subprocess.check_call(
        [
            "mosquitto_pub",
            "-h",
            host,
            "-p",
            str(port),
            "-u",
            user,
            "-P",
            pw,
            "-t",
            topic,
            "-m",
            json.dumps(payload),
        ]
    )

def publish_update(site_id: str, device_id: str, command_id: str) -> None:
    payload = make_command_payload(command_id=command_id, cmd="update")
    publish_command(site_id, device_id, payload)



def _fleet_targets(body: dict, reg: dict) -> tuple[list[str], str | None]:
    """Return (targets, error). Targets are sanitized device IDs."""
    single = body.get("device_id")
    many = body.get("device_ids")
    all_devices = bool(body.get("all"))

    if single:
        return [sanitize_topic_id(str(single))], None
    if isinstance(many, list) and many:
        return [sanitize_topic_id(str(x)) for x in many], None
    if all_devices:
        return [sanitize_topic_id(str(k)) for k in reg.keys()], None

    return [], "Provide device_id, device_ids, or all=true"


@app.post("/fleet/update")
def fleet_update():
    require_dashboard_key()

    body = request.get_json(silent=True) or {}

    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    command_id = sanitize_topic_id(body.get("command_id") or f"cmd-{int(time.time())}")

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    targets, err = _fleet_targets(body, reg)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

        recent = _rec_recent_ids(rec)
        if command_id in recent:
            skipped.append(did)
            continue

        try:
            payload = make_command_payload(
                command_id=command_id,
                cmd="update",
                ttl_sec=ttl_sec,
                priority=priority,
            )
            publish_command(site_id, did, payload)
            _mark_sent_in_registry(reg, did, command_id, "update")
            sent.append(did)
        except Exception:
            skipped.append(did)
   
    if sent:
        write_registry(reg)
    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})


@app.get("/fleet/status")
def fleet_status():
    require_dashboard_key()

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    devices = []
    for did, rec in reg.items():
        if not isinstance(rec, dict):
            continue
        devices.append(rec)

    # stable ordering
    devices.sort(key=lambda r: str(r.get("device_id", "")))

    return jsonify({"ok": True, "devices": devices})


@app.post("/fleet/build-and-update")
def fleet_build_and_update():
    require_dashboard_key()

    body = request.get_json(silent=True) or {}

    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    # Build OTA bundle first
    if not BUILD_OTA_SCRIPT.exists():
        return jsonify({"ok": False, "error": f"Missing build script: {BUILD_OTA_SCRIPT}"}), 500

    try:
        # Run build as the service user; build_ota.py already writes to /opt/hint/ota
        subprocess.check_call(["/usr/bin/python3", str(BUILD_OTA_SCRIPT)])
    except Exception as e:
        return jsonify({"ok": False, "error": f"OTA build failed: {e}"}), 500

    # Then publish updates
    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    targets, err = _fleet_targets(body, reg)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    command_id = sanitize_topic_id(body.get("command_id") or f"cmd-{int(time.time())}")

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

        recent = _rec_recent_ids(rec)
        if command_id in recent:
            skipped.append(did)
            continue

        try:
            payload = make_command_payload(
                command_id=command_id,
                cmd="update",
                ttl_sec=ttl_sec,
                priority=priority,
            )
            publish_command(site_id, did, payload)
            _mark_sent_in_registry(reg, did, command_id, "update")
            sent.append(did)
        except Exception:
            skipped.append(did)

    if sent:
        write_registry(reg)

    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})


#
#
# UI proxy endpoints (same-origin). No API key required from the browser.


@app.get("/ui/voices")
def ui_voices():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    force = bool(request.args.get("force"))
    info = _get_elevenlabs_voices_cached(force=force)
    return jsonify({
        "ok": bool(info.get("ok")),
        "voices": info.get("voices") or [],
        "checked_at": info.get("checked_at"),
        "error": info.get("error"),
    })

# --- UI TTS speak endpoint ---

@app.post("/ui/speak")
def ui_speak():
    """Generate speech on the hub, store it, and command one device to play it.

    Body:
      {"device_id": "...", "text": "...", "command_id": "optional", "ttl_sec": 300, "priority": 5,
       "tts": {"voice_id": "...", "model": "...", "stability": 0.5, "similarity_boost": 0.5}}
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    device_id = sanitize_topic_id(str(body.get("device_id") or ""))
    text = str(body.get("text") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id_required"}), 400
    if not text:
        return jsonify({"ok": False, "error": "text_required"}), 400

    if tts is None or not hasattr(tts, "synthesize"):
        return jsonify({"ok": False, "error": "tts_not_configured"}), 500
    if store_audio is None or build_audio_url is None:
        return jsonify({"ok": False, "error": "audio_server_not_configured"}), 500

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return jsonify({"ok": False, "error": "unknown_device"}), 404

    site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))
    ota_token = str(rec.get("ota_token") or "")
    if not ota_token:
        return jsonify({"ok": False, "error": "missing_ota_token"}), 500

    # Optional TTS options
    tts_opts = body.get("tts") if isinstance(body.get("tts"), dict) else {}

    # Ensure required ElevenLabs params are present
    # Our tts.synthesize() requires keyword-only voice_id.
    if not isinstance(tts_opts, dict):
        tts_opts = {}

    default_voice_id = (
        os.environ.get("ELEVENLABS_VOICE_ID")
        or os.environ.get("ELEVENLABS_DEFAULT_VOICE_ID")
        or ""
    ).strip()

    if not str(tts_opts.get("voice_id") or "").strip():
        if default_voice_id:
            tts_opts["voice_id"] = default_voice_id
        else:
            return jsonify({
                "ok": False,
                "error": "voice_id_required",
                "hint": "Provide tts.voice_id in the request, or set ELEVENLABS_VOICE_ID in /etc/hint/hub.env",
            }), 400

    # Synthesize audio bytes on hub
    try:
        res = tts.synthesize(text=text, **tts_opts)

        # Support multiple return shapes:
        #  - (audio_bytes, ext)
        #  - dict-like {audio_bytes/audio, ext/format}
        #  - object with attributes (e.g. TTSResult)
        audio_bytes = None
        ext = None

        if isinstance(res, tuple) and len(res) >= 1:
            audio_bytes = res[0]
            if len(res) >= 2:
                ext = res[1]
        elif isinstance(res, dict):
            audio_bytes = res.get("audio_bytes") or res.get("audio") or res.get("bytes")
            ext = res.get("ext") or res.get("format") or res.get("mime")
        else:
            # Attribute-based (TTSResult, etc.)
            for a in ("audio_bytes", "audio", "bytes"):
                if hasattr(res, a):
                    audio_bytes = getattr(res, a)
                    break
            for a in ("ext", "format", "mime"):
                if hasattr(res, a):
                    ext = getattr(res, a)
                    break

    except TypeError as e:
        # If the signature changed, surface a clear error
        msg = str(e)
        if "voice_id" in msg:
            return jsonify({
                "ok": False,
                "error": "voice_id_required",
                "detail": msg,
                "hint": "Set ELEVENLABS_VOICE_ID or pass tts.voice_id",
            }), 400
        return jsonify({
            "ok": False,
            "error": "tts_type_error",
            "detail": msg,
        }), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"tts_failed:{e}"}), 500

    # Normalize
    if isinstance(audio_bytes, memoryview):
        audio_bytes = audio_bytes.tobytes()

    if not isinstance(audio_bytes, (bytes, bytearray)) or len(audio_bytes) == 0:
        return jsonify({"ok": False, "error": "tts_empty_audio"}), 500

    ext = str(ext or "mp3").strip().lstrip(".")
    if not ext or any(ch in ext for ch in ("/", "\\", " ")):
        ext = "mp3"

    # Store audio and normalize the returned identifier (store_audio may return a string id,
    # a dict, a tuple, or an object like StoredAudio).
    try:
        stored = store_audio(bytes(audio_bytes), ext=ext)
    except Exception as e:
        return jsonify({"ok": False, "error": f"audio_store_failed:{e}"}), 500

    audio_id = None
    if isinstance(stored, str):
        audio_id = stored
    elif isinstance(stored, (tuple, list)) and stored:
        audio_id = stored[0]
    elif isinstance(stored, dict):
        audio_id = stored.get("audio_id") or stored.get("id")
    else:
        # Attribute-based (e.g. StoredAudio)
        for a in ("audio_id", "id"):
            if hasattr(stored, a):
                audio_id = getattr(stored, a)
                break

    audio_id = str(audio_id or "").strip()
    if not audio_id:
        return jsonify({"ok": False, "error": "audio_store_failed:missing_audio_id"}), 500

    try:
        url = build_audio_url(
            base_url=HUB_BASE_URL,
            audio_id=audio_id,
            device_id=device_id,
            token=ota_token,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"audio_url_failed:{e}"}), 500

    command_id = sanitize_topic_id(body.get("command_id") or f"speak-{device_id}-{int(time.time())}")
    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    payload = make_command_payload(
        command_id=command_id,
        cmd="speak",
        params={
            "url": url,
            "audio_id": audio_id,
            "text": text,
            "ext": ext,
        },
        ttl_sec=ttl_sec if ttl_sec is not None else 300,
        priority=priority if priority is not None else 5,
    )

    try:
        publish_command(site_id, device_id, payload)
        _mark_sent_in_registry(reg, device_id, command_id, "speak")
        write_registry(reg)
    except Exception as e:
        return jsonify({"ok": False, "error": f"mqtt_publish_failed:{e}"}), 500

    return jsonify({
        "ok": True,
        "device_id": device_id,
        "command_id": command_id,
        "audio_id": audio_id,
        "url": url,
    })

@app.get("/ui/status")
def ui_status():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    devices = []
    for _, rec in reg.items():
        if isinstance(rec, dict):
            devices.append(rec)

    devices.sort(key=lambda r: str(r.get("device_id", "")))
    ota_latest = _read_ota_latest_version()
    hub_status = _hub_update_status_cached(force=False)

    return jsonify({
        "ok": True,
        "devices": devices,
        "ota_latest_version": ota_latest,
        "hub_version": hub_status.get("hub_version"),
        "hub_latest_version": hub_status.get("hub_latest_version"),
        "hub_update_available": hub_status.get("hub_update_available"),
        "hub_checked_at": hub_status.get("hub_checked_at"),
        "hub_update_error": hub_status.get("hub_update_error"),
    })


@app.post("/ui/update")
def ui_update():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}

    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    targets, err = _fleet_targets(body, reg)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    command_id = sanitize_topic_id(body.get("command_id") or f"cmd-{int(time.time())}")

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

        recent = _rec_recent_ids(rec)
        if command_id in recent:
            skipped.append(did)
            continue

        try:
            payload = make_command_payload(
                command_id=command_id,
                cmd="update",
                ttl_sec=ttl_sec,
                priority=priority,
            )
            publish_command(site_id, did, payload)
            _mark_sent_in_registry(reg, did, command_id, "update")
            sent.append(did)
        except Exception:
            skipped.append(did)

    if sent:
        write_registry(reg)

    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})



@app.post("/ui/build-and-update")
def ui_build_and_update():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}

    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    if not BUILD_OTA_SCRIPT.exists():
        return jsonify({"ok": False, "error": f"Missing build script: {BUILD_OTA_SCRIPT}"}), 500

    try:
        subprocess.check_call(["/usr/bin/python3", str(BUILD_OTA_SCRIPT)])
    except Exception as e:
        return jsonify({"ok": False, "error": f"OTA build failed: {e}"}), 500

    # then publish updates
    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    targets, err = _fleet_targets(body, reg)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    command_id = sanitize_topic_id(body.get("command_id") or f"cmd-{int(time.time())}")

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

        recent = _rec_recent_ids(rec)
        if command_id in recent:
            skipped.append(did)
            continue

        try:
            payload = make_command_payload(
                command_id=command_id,
                cmd="update",
                ttl_sec=ttl_sec,
                priority=priority,
            )
            publish_command(site_id, did, payload)
            _mark_sent_in_registry(reg, did, command_id, "update")
            sent.append(did)
        except Exception:
            skipped.append(did)

    if sent:
        write_registry(reg)

    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})




# --- UI hub update endpoint ---

@app.post("/ui/hub/update")
def ui_hub_update():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    try:
        info = _start_hub_update_background()
        return jsonify(info)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# --- UI staged rollout endpoints ---

@app.post("/ui/rollout/start")
def ui_rollout_start():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    batch_size = int(body.get("batch_size") or 5)
    batch_size = max(1, min(batch_size, 50))

    build = bool(body.get("build"))

    ack_timeout_sec = int(body.get("ack_timeout_sec") or 120)
    ack_timeout_sec = max(5, min(ack_timeout_sec, 3600))

    # Safety controls
    stop_on_failure = bool(body.get("stop_on_failure", True))
    max_failures = int(body.get("max_failures") or 1)
    max_failures = max(0, min(max_failures, 1000))

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    targets, err = _fleet_targets(body, reg)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    job_id = _rollout_new_id()
    job = {
        "id": job_id,
        "state": "building" if build else "running",
        "batch_size": batch_size,
        "targets": targets,
        "pending": list(targets),
        "in_flight": {},
        "done": {},
        "failed": {},
        "started_at": utc_now_iso(),
        "ack_timeout_sec": ack_timeout_sec,
        "stop_on_failure": stop_on_failure,
        "max_failures": max_failures,
        "halted_reason": None,
    }

    # Optional build step (synchronous)
    if build:
        if not BUILD_OTA_SCRIPT.exists():
            return jsonify({"ok": False, "error": f"Missing build script: {BUILD_OTA_SCRIPT}"}), 500
        try:
            subprocess.check_call(["/usr/bin/python3", str(BUILD_OTA_SCRIPT)])
        except Exception as e:
            return jsonify({"ok": False, "error": f"OTA build failed: {e}"}), 500

    # Dispatch first batch immediately
    _advance_rollout(job, reg)

    ROLLOUTS[job_id] = job
    return jsonify({"ok": True, "job_id": job_id, "job": job})


@app.get("/ui/rollout/status")
def ui_rollout_status():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    job_id = request.args.get("job_id") or ""
    job_id = sanitize_topic_id(job_id)
    job = ROLLOUTS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown_job"}), 404

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    _advance_rollout(job, reg)

    done_map = job.get("done") or {}
    updated_cnt = 0
    skipped_cnt = 0
    for _did, _ack in done_map.items():
        if isinstance(_ack, dict):
            rr = str(_ack.get("rollout_result", "")).lower()
            if rr == "updated":
                updated_cnt += 1
            elif rr == "skipped":
                skipped_cnt += 1

    # Build a compact failure summary for the UI (avoid dumping full job.failed)
    failed_map = job.get("failed") or {}
    failed_summary = []
    if isinstance(failed_map, dict):
        for _did, _ack in list(failed_map.items())[:20]:
            item = {"device_id": _did}
            if isinstance(_ack, dict):
                item["error"] = _ack.get("error") or _ack.get("status")
                item["command_id"] = _ack.get("command_id")
                item["ts"] = _ack.get("ts")
            else:
                item["error"] = str(_ack)
            failed_summary.append(item)

    summary = {
        "id": job.get("id"),
        "state": job.get("state"),
        "halted_reason": job.get("halted_reason"),
        "stop_on_failure": bool(job.get("stop_on_failure")),
        "max_failures": int(job.get("max_failures") or 0),
        "batch_size": job.get("batch_size"),
        "ack_timeout_sec": int(job.get("ack_timeout_sec") or 120),
        "targets_total": len(job.get("targets") or []),
        "pending": len(job.get("pending") or []),
        "in_flight": len(job.get("in_flight") or {}),
        "done": len(done_map),
        "failed": len(job.get("failed") or {}),
        "failed_summary": failed_summary,
        "updated": updated_cnt,
        "skipped": skipped_cnt,
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }

    return jsonify({"ok": True, "job": job, "summary": summary})


# --- Resume and Cancel endpoints for rollouts ---

@app.post("/ui/rollout/resume")
def ui_rollout_resume():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    job_id = sanitize_topic_id(body.get("job_id") or "")
    job = ROLLOUTS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown_job"}), 404

    if job.get("state") != "halted":
        return jsonify({"ok": False, "error": "not_halted", "state": job.get("state")}), 400

    # Resume should retry devices that previously failed, then continue remaining pending.
    failed_map = job.get("failed") or {}
    failed_ids = list(failed_map.keys()) if isinstance(failed_map, dict) else []

    pending = job.get("pending") or []
    if not isinstance(pending, list):
        pending = []

    inflight = job.get("in_flight") or {}
    if not isinstance(inflight, dict):
        inflight = {}

    # Requeue failed devices at the front, preserving order and removing duplicates.
    new_pending: list[str] = []
    for did in failed_ids + pending:
        did = sanitize_topic_id(str(did))
        if not did:
            continue
        if did in inflight:
            continue
        if did not in new_pending:
            new_pending.append(did)

    job["pending"] = new_pending
    job["failed"] = {}

    job["state"] = "running"
    job["halted_reason"] = None
    job.pop("finished_at", None)

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    _advance_rollout(job, reg)

    return jsonify({"ok": True, "job": job})


@app.post("/ui/rollout/cancel")
def ui_rollout_cancel():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    job_id = sanitize_topic_id(body.get("job_id") or "")
    job = ROLLOUTS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown_job"}), 404

    job["state"] = "canceled"
    job["finished_at"] = utc_now_iso()

    return jsonify({"ok": True, "job": job})


@app.get("/speak")
def speak_page():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    html = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Hint Hub - Speak</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 16px; }
    header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    nav a { margin-right: 10px; text-decoration: none; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eee; }
    .panel { margin-top: 14px; padding: 12px; border: 1px solid #ddd; border-radius: 8px; }
    label { display:block; margin: 10px 0 6px; font-weight: 600; }
    select, textarea, input[type=text] { width: 100%; max-width: 860px; padding: 8px; font-size: 14px; }
    textarea { height: 120px; }
    button { padding: 8px 12px; cursor: pointer; }
    .row { display:flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .muted { color:#666; }
    .log { margin-top: 12px; padding: 10px; background: #111; color: #eee; border-radius: 6px; height: 180px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace; font-size: 12px; }
  </style>
</head>
<body>
  <header>
    <div class=\"row\">
      <span class=\"pill\">Hint Hub</span>
      <nav>
        <a href=\"/speak\"><b>Speak</b></a>
        <a href=\"/dashboard\">Updates</a>
      </nav>
    </div>
  </header>

  <div class=\"panel\">
    <div class=\"row\">
      <div>
        <label for=\"device\">Device</label>
        <select id=\"device\"></select>
        <div class=\"muted\" id=\"deviceHint\"></div>
      </div>
      <div>
        <label for=\"voice\">Voice</label>
        <select id=\"voice\"></select>
        <div class=\"muted\" id=\"voiceHint\"></div>
      </div>
    </div>

    <label for=\"text\">Message</label>
    <textarea id=\"text\" placeholder=\"Type what you want the character to say…\"></textarea>

    <div class=\"row\" style=\"margin-top:10px;\">
      <button id=\"speakBtn\">Generate + Play</button>
      <button id=\"refreshBtn\">Refresh devices/voices</button>
      <span class=\"muted\" id=\"status\"></span>
    </div>
  </div>

  <div class=\"log\" id=\"log\"></div>

<script>
(function(){
  const $ = (id) => document.getElementById(id);
  const logEl = $("log");

  function log(msg){
    const ts = new Date().toISOString();
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

  function setStatus(s){
    $("status").textContent = s || '';
  }

  function savePrefs(){
    try {
      localStorage.setItem('hint_voice_id', $("voice").value || '');
      localStorage.setItem('hint_device_id', $("device").value || '');
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

  async function loadDevices(){
    const data = await api('/ui/status', 'GET');
    const devices = (data.devices || []).filter(d => d && d.device_id);
    const sel = $("device");
    sel.innerHTML = '';
    devices.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.device_id;
      const st = d.status || 'unknown';
      opt.textContent = `${d.device_id} (${st})`;
      sel.appendChild(opt);
    });
    $("deviceHint").textContent = devices.length ? '' : 'No devices paired yet.';
    return devices;
  }

  async function loadVoices(force){
    const qs = force ? '?force=1' : '';
    const data = await api('/ui/voices' + qs, 'GET');
    const voices = (data.voices || []).filter(v => v && v.voice_id && v.name);
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

  async function refreshAll(forceVoices){
    setStatus('Loading…');
    const prefs = loadPrefs();
    const devices = await loadDevices();
    const voices = await loadVoices(!!forceVoices);

    if (prefs.device_id) {
      $("device").value = prefs.device_id;
    }
    if (prefs.voice_id) {
      $("voice").value = prefs.voice_id;
    }

    // If pref not present or invalid, just keep first option.
    savePrefs();
    setStatus('Ready');
    log(`Loaded devices=${devices.length}, voices=${voices.length}`);
  }

  $("device").addEventListener('change', savePrefs);
  $("voice").addEventListener('change', savePrefs);

  $("refreshBtn").addEventListener('click', async () => {
    try {
      await refreshAll(true);
    } catch(e){
      setStatus('Error');
      log(`Refresh failed: ${e.message}`);
    }
  });

  $("speakBtn").addEventListener('click', async () => {
    const device_id = $("device").value || '';
    const voice_id = $("voice").value || '';
    const text = ($("text").value || '').trim();
    if (!device_id) { log('Pick a device first.'); return; }
    if (!voice_id) { log('Pick a voice first.'); return; }
    if (!text) { log('Type a message first.'); return; }

    try {
      $("speakBtn").disabled = true;
      setStatus('Generating…');
      log(`Speak: device=${device_id}, voice=${voice_id}, chars=${text.length}`);
      const r = await api('/ui/speak', 'POST', { device_id, text, tts: { voice_id } });
      log(`Queued: ${JSON.stringify(r)}`);
      setStatus('Sent');
    } catch(e){
      setStatus('Error');
      log(`Speak failed: ${e.message}`);
    } finally {
      $("speakBtn").disabled = false;
    }
  });

  refreshAll(false).catch(e => log(`Init failed: ${e.message}`));
})();
</script>
</body>
</html>"""

    return html

# Dashboard HTML shell route
@app.get("/dashboard")
def dashboard():
    ar = require_ui_auth()
    if ar is not None:
        return ar
    # Public HTML shell; API operations are still protected by X-Api-Key.
    html = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Hint Hub Dashboard</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 16px; }
    header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    input[type=password] { padding: 6px 8px; min-width: 260px; }
    button { padding: 6px 10px; cursor: pointer; }
    table { border-collapse: collapse; width: 100%; margin-top: 14px; }
    th, td { border-bottom: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: #f7f7f7; }
    .status-online { color: #0a7; font-weight: 600; }
    .status-offline { color: #a00; font-weight: 600; }
    .muted { color: #666; }
    .row-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .log { margin-top: 14px; padding: 10px; background: #111; color: #eee; border-radius: 6px; height: 180px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace; font-size: 12px; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eee; }
    .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .right { margin-left: auto; }
    .panel { margin-top: 14px; padding: 10px; border: 1px solid #ddd; border-radius: 8px; }
    .panel h3 { margin: 0 0 8px 0; font-size: 14px; }
    .kv { display: flex; gap: 12px; flex-wrap: wrap; }
    .kv div { padding: 2px 0; }
    input[type=number] { padding: 6px 8px; width: 90px; }
  </style>
</head>
<body>
  <header>
    <div class=\"controls\">
      <span class=\"pill\">Hint Hub Dashboard</span>
      <a href=\"/speak\" class=\"muted\" style=\"text-decoration:none;\">Speak</a>
      <span class=\"muted\">Auto-refresh: 5s</span>
    </div>
    <div class=\"controls right\">
      <button id=\"refresh\">Refresh</button>
      <button id=\"hubUpdate\">Update Hub</button>
      <button id=\"updateAll\">Update All</button>
      <button id=\"buildUpdateAll\">Build+Update All</button>
    </div>
  </header>

  <div id="banner" class="panel" style="display:none;"></div>

    <div class="panel">
    <h3>Staged Rollout</h3>
    <div class="controls">
      <label>Batch size: <input id="batchSize" type="number" min="1" max="50" value="5" /></label>
      <button id="rolloutUpdateAll">Staged Update All</button>
      <button id="rolloutBuildUpdateAll">Staged Build+Update All</button>
      <button id="rolloutResume" disabled>Retry failed</button>
      <button id="rolloutCancel" disabled>Cancel</button>
      <span class="muted">Runs batches sequentially and tracks ACK completion.</span>
    </div>
    <div class="kv" id="rolloutStatus" style="margin-top:8px;">
      <div class="muted">No rollout running.</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Device</th>
        <th>Status</th>
        <th>Last Seen (UTC)</th>
        <th>Agent</th>
        <th>Setup</th>
        <th>Last Cmd</th>
        <th>Last Result</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id=\"tbody\">
      <tr><td colspan=\"8\" class=\"muted\">Loading…</td></tr>
    </tbody>
  </table>

  <div class=\"log\" id=\"log\"></div>

<script>
(function(){
  const $ = (id) => document.getElementById(id);
  const logEl = $(\"log\");

  function log(msg){
    const ts = new Date().toISOString();
    logEl.textContent += `[${ts}] ${msg}\\n`;
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

    b.innerHTML = `<h3 style="margin:0 0 8px 0; font-size:14px;">Updates</h3>` + parts.join('');
    b.style.display = '';
  }

  async function refresh(){
    try {
      const data = await api('/ui/status', 'GET');
      const devices = data.devices || [];
      const otaLatest = data.ota_latest_version || null;
      const hubVer = data.hub_version || null;
      const hubLatest = data.hub_latest_version || null;
      const hubUpd = (typeof data.hub_update_available === 'boolean') ? data.hub_update_available : null;
      const hubErr = data.hub_update_error || null;
      renderBanner({ otaLatest, hubVer, hubLatest, hubUpd, hubErr });
      render(devices, otaLatest);
    } catch (e) {
      renderBanner(null);
      render([]);
      log(`Refresh failed: ${e.message}`);
    }
  }

  function render(devices, otaLatest){
    const tbody = $(\"tbody\");
    if (!devices.length){
      tbody.innerHTML = `<tr><td colspan=\"8\" class=\"muted\">No devices.</td></tr>`;
      return;
    }

    tbody.innerHTML = devices.map(d => {
      const did = fmt(d.device_id);
      const st = fmt(d.status || 'unknown');
      const ls = fmt(d.last_seen || '');
      const avRaw = fmt(d.agent_version || '(unknown)');
      const svRaw = fmt(d.setup_version || '(unknown)');
      const needsUpdate = otaLatest && avRaw && (avRaw !== '(unknown)') && (avRaw !== String(otaLatest));
      const av = needsUpdate ? `${avRaw} → ${otaLatest}` : avRaw;
      const sv = svRaw;
      const lc = lastCmd(d);
      const lr = lastResult(d);
      return `
        <tr>
          <td><span class=\"pill\">${did}</span></td>
          <td class=\"${statusClass(st)}\">${st}</td>
          <td class=\"muted\">${ls}</td>
          <td>${av}${needsUpdate ? `<div class="muted">Update available</div>` : ``}</td>
          <td>${sv}</td>
          <td class=\"muted\">${lc}</td>
          <td class=\"muted\">${lr}</td>
          <td>
            <div class=\"row-actions\">
              <button data-action=\"update\" data-device=\"${did}\">Update</button>
              <button data-action=\"buildUpdate\" data-device=\"${did}\">Build+Update</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');

    tbody.querySelectorAll('button[data-action]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const action = btn.getAttribute('data-action');
        const device = btn.getAttribute('data-device');
        try {
          btn.disabled = true;
          if (action === 'update') {
            log(`Updating ${device}...`);
            const r = await api('/ui/update', 'POST', { device_id: device, command_id: `cmd-${device}-${Date.now()}` });
            log(`Update result: ${JSON.stringify(r)}`);
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

  $("rolloutUpdateAll").addEventListener('click', async () => {
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

  $("rolloutBuildUpdateAll").addEventListener('click', async () => {
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

  $("rolloutResume").addEventListener('click', async () => {
    if (!currentRolloutJobId) return;
    try {
      log(`Resuming rollout ${currentRolloutJobId}...`);
      await api('/ui/rollout/resume', 'POST', { job_id: currentRolloutJobId });
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout resume failed: ${e.message}`);
    }
  });

  $("rolloutCancel").addEventListener('click', async () => {
    if (!currentRolloutJobId) return;
    try {
      log(`Canceling rollout ${currentRolloutJobId}...`);
      await api('/ui/rollout/cancel', 'POST', { job_id: currentRolloutJobId });
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout cancel failed: ${e.message}`);
    }
  });

  $(\"refresh\").addEventListener('click', refresh);

  $("hubUpdate").addEventListener('click', async () => {
    try {
      log('Starting hub update (git pull + restart services)...');
      const r = await api('/ui/hub/update', 'POST', {});
      log(`Hub update triggered: ${JSON.stringify(r)}`);
      log('If hub services restart, the page may briefly fail to refresh; wait a few seconds then refresh.');
    } catch (e) {
      log(`Hub update failed: ${e.message}`);
    }
  });

  $(\"updateAll\").addEventListener('click', async () => {
    try {
      log('Update All...');
      const r = await api('/ui/update', 'POST', { all: true, command_id: `cmd-all-${Date.now()}` });
      log(`Update All result: ${JSON.stringify(r)}`);
      await refresh();
    } catch (e) {
      log(`Update All failed: ${e.message}`);
    }
  });

  $(\"buildUpdateAll\").addEventListener('click', async () => {
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
  setInterval(() => {
    refresh();
    rolloutStatusTick();
  }, 5000);
})();
</script>
</body>
</html>"""
    return html

if __name__ == "__main__":
    # 0.0.0.0 so the device can reach it on the LAN
    app.run(host="0.0.0.0", port=5000)