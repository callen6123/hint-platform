from flask import Flask, request, jsonify, send_file, abort, render_template, Response, session, redirect, url_for, make_response, g
from jinja2 import ChoiceLoader, FileSystemLoader
import os
import sys
import subprocess
import secrets
import string
import json
import html as _html
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error

from werkzeug.security import generate_password_hash, check_password_hash

import socket
import threading

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

# Explicitly point Flask/Jinja at templates/static so the service works
# regardless of current working directory or where the folders are placed.
# We support BOTH layouts:
#   - repo root:   <repo>/templates and <repo>/static
#   - hub module:  <repo>/hub/templates and <repo>/hub/static
TEMPLATES_DIRS = [
    str(REPO_ROOT / "templates"),
    str(REPO_ROOT / "hub" / "templates"),
]
STATIC_DIR = str(REPO_ROOT / "static")
HUB_STATIC_DIR = str(REPO_ROOT / "hub" / "static")

# Prefer repo-root static if present, otherwise hub/static.
_static_dir = STATIC_DIR if Path(STATIC_DIR).is_dir() else HUB_STATIC_DIR


app = Flask(
    __name__,
    static_folder=_static_dir,
    static_url_path="/static",
)

# Allow Jinja to search multiple template directories.
app.jinja_loader = ChoiceLoader([FileSystemLoader(p) for p in TEMPLATES_DIRS])

# --- UI session/cookie auth (product-grade UX) ---
# Cookie session login for browser UX; Basic Auth remains as fallback for curl/debugging.
# IMPORTANT: set HINT_UI_SECRET (or FLASK_SECRET_KEY) in production to a stable random value.
# If behind HTTPS (nginx), set HINT_UI_COOKIE_SECURE=1.

_UI_SECRET = (os.environ.get("HINT_UI_SECRET") or os.environ.get("FLASK_SECRET_KEY") or "").strip()
if not _UI_SECRET:
    # Fallback so the hub still starts; for real deployments provide a stable secret via env.
    _UI_SECRET = secrets.token_urlsafe(32)

app.secret_key = _UI_SECRET

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(int(os.environ.get("HINT_UI_COOKIE_SECURE", "0") or "0"))

try:
    # 12 hours default
    app.permanent_session_lifetime = int(os.environ.get("HINT_UI_SESSION_LIFETIME_SEC", "43200"))
except Exception:
    pass

# Register audio blueprint if available
if audio_bp is not None:
    app.register_blueprint(audio_bp)

# Base URL devices should use to fetch hub-hosted artifacts (OTA + audio)
HUB_BASE_URL = os.environ.get("HUB_BASE_URL", "http://hinthub.local:5000")

PAIR_CODE = "483921"  # you will type this into the device setup page

REGISTRY_DIR = Path("/var/lib/hint")
REGISTRY_PATH = REGISTRY_DIR / "devices.json"
UI_CONFIG_PATH = REGISTRY_DIR / "ui_config.json"
UI_USERS_PATH = REGISTRY_DIR / "ui_users.json"

# --- Hub audit log (append-only JSONL) ---
HUB_LOG_DIR = Path("/var/lib/hint/logs")
HUB_CMD_LOG_PATH = HUB_LOG_DIR / "hub_cmd.log"
_HUB_CMD_LOG_LOCK = threading.Lock()


# --- Log tail helper for UI ---
def _tail_text_file(path: Path, max_lines: int = 500) -> str:
    """Return the last `max_lines` lines of a UTF-8 text file (best-effort)."""
    try:
        max_lines = int(max_lines)
    except Exception:
        max_lines = 500
    max_lines = max(10, min(max_lines, 5000))

    try:
        if not path.exists():
            return ""

        # Read the whole file for simplicity; log files are expected to be small.
        # If this grows, we can implement a true tail.
        txt = path.read_text(encoding="utf-8", errors="replace")
        if not txt:
            return ""
        lines = txt.splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail) + ("\n" if tail else "")
    except Exception:
        return ""

def _append_hub_cmd_log(event: dict) -> None:
    """Append a single JSON line to /var/lib/hint/logs/hub_cmd.log (best-effort)."""
    try:
        if not isinstance(event, dict):
            return

        # Normalize timestamp
        event = dict(event)
        event.setdefault("ts", utc_now_iso())

        HUB_LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True)

        with _HUB_CMD_LOG_LOCK:
            with open(HUB_CMD_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return


def _redact_payload_for_log(payload: dict) -> dict:
    """Return a shallow-redacted copy of a command payload for audit logging.

    We redact secrets embedded in URLs (ota/audio tokens) and keep the rest.
    """
    try:
        if not isinstance(payload, dict):
            return {}
        p = dict(payload)
        params = p.get("params")
        if isinstance(params, dict):
            params2 = dict(params)
            # Redact URL tokens (audio/ota)
            if "url" in params2 and isinstance(params2.get("url"), str):
                params2["url"] = "<redacted>"
            p["params"] = params2
        return p
    except Exception:
        return {}

# --- Registry and config helpers (atomic, no sudo) ---

def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON to `path` without using sudo.

    This assumes the service user (hub) has write permission to the target directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")

    # Best-effort permissions; if the service user lacks permission this will raise.
    try:
        os.chmod(tmp, 0o664)
    except Exception:
        pass

    os.replace(tmp, path)

    try:
        os.chmod(path, 0o664)
    except Exception:
        pass


def _read_json_file(path: Path, default: dict) -> dict:
    try:
        txt = path.read_text(encoding="utf-8").strip()
        if not txt:
            return dict(default)
        d = json.loads(txt)
        return d if isinstance(d, dict) else dict(default)
    except FileNotFoundError:
        return dict(default)
    except Exception:
        return dict(default)
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

# --- Hub audio cache cleanup (retention / max files / max bytes) ---

def _cleanup_audio_cache_if_needed(force: bool = False) -> None:
    """Best-effort cleanup of hub audio cache.

    Controlled by env vars:
      - HINT_AUDIO_RETENTION_HOURS (default 24)
      - HINT_AUDIO_MAX_FILES (default 2000)
      - HINT_AUDIO_MAX_BYTES (default 536870912 = 512MB)
      - HINT_AUDIO_CLEANUP_INTERVAL_SEC (default 300)
      - HINT_AUDIO_CACHE_DIR (default /var/lib/hint/audio_cache)

    NOTE: This runs in-process (Flask). It is best-effort and never raises.
    """
    try:
        now = time.time()
        last = float(getattr(_cleanup_audio_cache_if_needed, "_last_ts", 0.0))
        interval = int(os.environ.get("HINT_AUDIO_CLEANUP_INTERVAL_SEC", "300"))
        interval = max(10, min(interval, 24 * 3600))
        if (not force) and (now - last) < interval:
            return
        _cleanup_audio_cache_if_needed._last_ts = now  # type: ignore[attr-defined]

        cache_dir = Path(os.environ.get("HINT_AUDIO_CACHE_DIR", "/var/lib/hint/audio_cache"))
        if not cache_dir.exists() or not cache_dir.is_dir():
            return

        retention_hours = int(os.environ.get("HINT_AUDIO_RETENTION_HOURS", "24"))
        retention_hours = max(1, min(retention_hours, 24 * 30))  # cap at 30 days
        cutoff = now - (retention_hours * 3600)

        max_files = int(os.environ.get("HINT_AUDIO_MAX_FILES", "2000"))
        max_files = max(0, min(max_files, 200000))

        max_bytes = int(os.environ.get("HINT_AUDIO_MAX_BYTES", str(512 * 1024 * 1024)))
        max_bytes = max(0, max_bytes)

        files: list[tuple[float, int, Path]] = []
        total_bytes = 0

        # First pass: delete expired by mtime, collect survivors
        for p in cache_dir.iterdir():
            try:
                if not p.is_file():
                    continue
                st = p.stat()
                mtime = float(st.st_mtime)
                size = int(st.st_size)

                if mtime < cutoff:
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    continue

                files.append((mtime, size, p))
                total_bytes += size
            except Exception:
                continue

        # Oldest first
        files.sort(key=lambda t: t[0])

        # Enforce max_files
        if max_files > 0 and len(files) > max_files:
            to_delete = len(files) - max_files
            for i in range(to_delete):
                try:
                    _mtime, _size, fp = files[i]
                    total_bytes -= int(_size)
                    fp.unlink()
                except Exception:
                    pass
            files = files[to_delete:]

        # Enforce max_bytes
        if max_bytes > 0 and total_bytes > max_bytes:
            for _mtime, _size, fp in files:
                if total_bytes <= max_bytes:
                    break
                try:
                    total_bytes -= int(_size)
                    fp.unlink()
                except Exception:
                    pass

    except Exception:
        return

def _audio_cache_status() -> dict:
    cache_dir = Path(os.environ.get("HINT_AUDIO_CACHE_DIR", "/var/lib/hint/audio_cache"))

    retention_hours = int(os.environ.get("HINT_AUDIO_RETENTION_HOURS", "24"))
    max_files = int(os.environ.get("HINT_AUDIO_MAX_FILES", "2000"))
    max_bytes = int(os.environ.get("HINT_AUDIO_MAX_BYTES", str(512 * 1024 * 1024)))

    out = {
        "ok": True,
        "cache_dir": str(cache_dir),
        "policy": {
            "retention_hours": retention_hours,
            "max_files": max_files,
            "max_bytes": max_bytes,
        },
        "stats": {
            "files": 0,
            "bytes": 0,
            "oldest_mtime": None,
            "newest_mtime": None,
        },
    }

    try:
        if not cache_dir.exists() or not cache_dir.is_dir():
            out["ok"] = False
            out["error"] = "cache_dir_missing"
            return out

        files = 0
        total = 0
        oldest = None
        newest = None

        for p in cache_dir.iterdir():
            try:
                if not p.is_file():
                    continue
                st = p.stat()
                files += 1
                total += int(st.st_size)
                mt = float(st.st_mtime)
                oldest = mt if oldest is None else min(oldest, mt)
                newest = mt if newest is None else max(newest, mt)
            except Exception:
                continue

        out["stats"]["files"] = files
        out["stats"]["bytes"] = total
        out["stats"]["oldest_mtime"] = datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat() if oldest else None
        out["stats"]["newest_mtime"] = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None
        return out

    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
        return out


@app.get("/ui/audio/status")
def ui_audio_status():
    ar = require_ui_auth()
    if ar is not None:
        return ar
    return jsonify(_audio_cache_status())


@app.post("/ui/audio/cleanup")
def ui_audio_cleanup():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    _cleanup_audio_cache_if_needed(force=True)
    return jsonify(_audio_cache_status())

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
        try:
            txt = Path(acl_file).read_text(encoding="utf-8", errors="replace")
            if f"user {username}" in txt:
                return
        except Exception:
            # If unreadable, proceed to append (tee runs under sudo below).
            pass

    subprocess.run(
        ["sudo", "tee", "-a", acl_file],
        input=("\n" + block.strip() + "\n").encode("utf-8"),
        check=True,
    )


def restart_mosquitto() -> None:
    subprocess.run(["sudo", "systemctl", "restart", "mosquitto"], check=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_registry_dir() -> None:
    """Ensure /var/lib/hint exists.

    NOTE: This function does not use sudo. The directory must already be writable
    by the service user (hub), typically via root:hub ownership and group write.
    """
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)


def read_registry() -> dict:
    return _read_json_file(REGISTRY_PATH, default={})


def write_registry(reg: dict) -> None:
    ensure_registry_dir()
    if not isinstance(reg, dict):
        reg = {}
    _atomic_write_json(REGISTRY_PATH, reg)


# --- UI config helpers ---

def read_ui_config() -> dict:
    """UI config stored in /var/lib/hint/ui_config.json.

    Shape:
      {
        "devices": {
          "device-...": {"friendly_name": "...", "default_voice_id": "..."}
        },
        "phrases": [
          {"id": "p-...", "label": "...", "text": "..."}
        ]
      }
    """
    d = _read_json_file(UI_CONFIG_PATH, default={"devices": {}, "phrases": []})

    # Normalize
    if not isinstance(d.get("devices"), dict):
        d["devices"] = {}
    if not isinstance(d.get("phrases"), list):
        d["phrases"] = []
    return d


def write_ui_config(cfg: dict) -> None:
    ensure_registry_dir()

    if not isinstance(cfg, dict):
        cfg = {}
    if not isinstance(cfg.get("devices"), dict):
        cfg["devices"] = {}
    if not isinstance(cfg.get("phrases"), list):
        cfg["phrases"] = []

    _atomic_write_json(UI_CONFIG_PATH, cfg)


def get_device_ui_cfg(device_id: str) -> dict:
    cfg = read_ui_config()
    devs = cfg.get("devices") if isinstance(cfg, dict) else None
    if not isinstance(devs, dict):
        return {}
    v = devs.get(device_id)
    return v if isinstance(v, dict) else {}


# --- UI users/auth storage ---

def _default_users_payload() -> dict:
    # Shape:
    # {
    #   "users": {
    #     "admin": {"role": "admin", "password_hash": "...", "created_at": "...", "updated_at": "..."},
    #     "gm": {"role": "gm", "password_hash": "...", "created_at": "...", "updated_at": "..."}
    #   }
    # }
    return {"users": {}}


def read_ui_users() -> dict:
    d = _read_json_file(UI_USERS_PATH, default=_default_users_payload())
    if not isinstance(d, dict):
        d = _default_users_payload()
    if not isinstance(d.get("users"), dict):
        d["users"] = {}
    return d


def write_ui_users(d: dict) -> None:
    ensure_registry_dir()
    if not isinstance(d, dict):
        d = _default_users_payload()
    if not isinstance(d.get("users"), dict):
        d["users"] = {}
    _atomic_write_json(UI_USERS_PATH, d)


def _bootstrap_ui_users_if_needed() -> dict:
    """Ensure ui_users.json exists.

    Bootstrap source of truth:
      - If ui_users.json exists: use it.
      - Else: require HINT_UI_USER/HINT_UI_PASS to seed an initial admin.
        Optionally seed a GM user via HINT_UI_GM_USER/HINT_UI_GM_PASS.

    This keeps backward compatibility with the original env-based single user.
    """
    if UI_USERS_PATH.exists():
        return read_ui_users()

    admin_user = (os.environ.get("HINT_UI_USER") or "").strip()
    admin_pass = (os.environ.get("HINT_UI_PASS") or "").strip()
    if not admin_user or not admin_pass:
        # No file and no env seed; caller should treat as misconfigured.
        return _default_users_payload()

    now = utc_now_iso()
    users = {
        admin_user: {
            "role": "admin",
            "password_hash": generate_password_hash(admin_pass),
            "created_at": now,
            "updated_at": now,
        }
    }

    gm_user = (os.environ.get("HINT_UI_GM_USER") or "").strip()
    gm_pass = (os.environ.get("HINT_UI_GM_PASS") or "").strip()
    if gm_user and gm_pass:
        users[gm_user] = {
            "role": "gm",
            "password_hash": generate_password_hash(gm_pass),
            "created_at": now,
            "updated_at": now,
        }

    payload = {"users": users}
    try:
        write_ui_users(payload)
    except Exception:
        # Best-effort; if permissions are wrong, auth will fail and health will show registry issues.
        pass
    return payload


def _get_user_record(username: str) -> dict | None:
    username = str(username or "").strip()
    if not username:
        return None
    users_payload = _bootstrap_ui_users_if_needed()
    users = users_payload.get("users") if isinstance(users_payload, dict) else None
    if not isinstance(users, dict):
        return None
    rec = users.get(username)
    return rec if isinstance(rec, dict) else None


def _verify_ui_credentials(username: str, password: str) -> dict | None:
    rec = _get_user_record(username)
    if not rec:
        return None
    ph = rec.get("password_hash")
    if not isinstance(ph, str) or not ph:
        return None
    try:
        ok = check_password_hash(ph, str(password or ""))
    except Exception:
        ok = False
    if not ok:
        return None
    return {"username": str(username).strip(), "role": str(rec.get("role") or "")}


def _current_ui_user() -> dict | None:
    # Session-based
    try:
        if session.get("ui_auth") is True:
            u = str(session.get("ui_user") or "").strip()
            r = str(session.get("ui_role") or "").strip()
            if u:
                return {"username": u, "role": r}
    except Exception:
        pass

    # Basic auth fallback (env or ui_users)
    auth = request.authorization
    if auth and auth.username and auth.password:
        # Prefer ui_users.json if present/bootstrapable
        v = _verify_ui_credentials(auth.username, auth.password)
        if v:
            return v

        # Back-compat: env user/pass
        user_env = (os.environ.get("HINT_UI_USER") or "").strip()
        pw_env = (os.environ.get("HINT_UI_PASS") or "").strip()
        if user_env and pw_env and auth.username == user_env and auth.password == pw_env:
            return {"username": user_env, "role": "admin"}

    return None


def _require_admin_user() -> dict | None:
    cu = _current_ui_user()
    if not cu:
        return None
    if str(cu.get("role") or "").lower() == "admin":
        return cu
    return None


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




# UI auth.
# Primary UX: cookie-based session set by /login.
# Fallback: HTTP Basic Auth (still useful for curl/debugging).

def _ui_basic_auth_ok() -> bool:
    # Back-compat for curl/debugging: allow either ui_users.json credentials or env credentials.
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return False

    # Prefer ui_users.json
    if _verify_ui_credentials(auth.username, auth.password):
        return True

    # Fallback to env
    user = (os.environ.get("HINT_UI_USER") or "").strip()
    pw = (os.environ.get("HINT_UI_PASS") or "").strip()
    return bool(user and pw and auth.username == user and auth.password == pw)


def _ui_session_ok() -> bool:
    try:
        return bool(session.get("ui_auth") is True and str(session.get("ui_user") or "").strip())
    except Exception:
        return False


def _ui_wants_html() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    return "text/html" in accept


def require_ui_auth():
    """Enforce UI auth for /dashboard, /speak, and /ui/*.

    Returns:
      - None if authorized
      - A Flask Response (redirect to /login for HTML pages, or 401 JSON)
    """
    cu = _current_ui_user()
    if cu:
        # Stash on Flask global for convenience
        try:
            g.ui_user = cu
        except Exception:
            pass
        return None

    path = request.path or ""

    # UI pages: redirect to login.
    if path in ("/dashboard", "/speak") or _ui_wants_html():
        nxt = request.full_path if request.query_string else path
        try:
            return redirect(url_for("ui_login", next=nxt))
        except Exception:
            return redirect("/login")

    # API endpoints: 401 JSON (still advertises Basic fallback)
    resp = jsonify({"ok": False, "error": "auth_required"})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = 'Basic realm="Hint Hub"'
    return resp

def require_ui_admin():
    """Admin-only guard for selected UI pages (e.g. /dashboard).

    Returns:
      - None if authorized as admin
      - A Flask Response (redirect for HTML pages, or 403 JSON)
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    cu = _current_ui_user() or {}
    role = str(cu.get("role") or "").lower()
    if role == "admin":
        return None

    # Browser page: redirect non-admin away from /dashboard
    if _ui_wants_html() or (request.path or "") == "/dashboard":
        return redirect("/speak")

    # API-style call
    return jsonify({"ok": False, "error": "admin_required"}), 403

# --- Login/logout routes (session cookie) ---

@app.get("/login")
def ui_login():
    # If already authed, go to dashboard (or next)
    if _current_ui_user() is not None:
        nxt = request.args.get("next") or "/dashboard"
        return redirect(nxt)

    nxt = request.args.get("next") or "/dashboard"
    msg = request.args.get("msg") or ""

    # Render message safely (avoid Jinja-looking artifacts inside an f-string)
    msg = str(msg or "")
    msg_html = ""
    if msg:
        msg_html = (
            "<div class='notice' style='margin-bottom:12px;color:#b00020;'>"
            + _html.escape(msg)
            + "</div>"
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hint Hub Login</title>
  <link rel="stylesheet" href="/static/css/hub.css" />
</head>
<body>
  <div class="page" style="max-width: 520px; margin: 40px auto; padding: 16px;">
    <h1 style="margin: 0 0 12px 0;">Hint Hub</h1>
    <p style="margin: 0 0 18px 0; opacity: 0.85;">Sign in to access the dashboard.</p>
    {msg_html}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{nxt}" />
      <div style="display:flex; flex-direction:column; gap:10px;">
        <label>Username<br><input name="username" autocomplete="username" style="width:100%; padding:10px;" /></label>
        <label>Password<br><input name="password" type="password" autocomplete="current-password" style="width:100%; padding:10px;" /></label>
        <button type="submit" style="padding: 10px;">Sign in</button>
      </div>
    </form>
  </div>
</body>
</html>"""

    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.post("/login")
def ui_login_post():
    # Ensure user store exists (or is bootstrapped from env on first run)
    users_payload = _bootstrap_ui_users_if_needed()
    users = users_payload.get("users") if isinstance(users_payload, dict) else None
    if not isinstance(users, dict) or len(users) == 0:
        # No users configured and no env seed available
        abort(403)

    # Support form POST primarily, JSON as fallback
    form_user = request.form.get("username") if request.form else None
    form_pw = request.form.get("password") if request.form else None

    if form_user is None and form_pw is None:
        body = request.get_json(silent=True) or {}
        form_user = body.get("username")
        form_pw = body.get("password")

    u = str(form_user or "").strip()
    p = str(form_pw or "").strip()
    nxt = (request.form.get("next") if request.form else None) or (request.args.get("next") or "/dashboard")

    # Prevent malformed/templated values (e.g. "{nxt}") and open redirects
    nxt = str(nxt or "").strip()
    if ("{" in nxt) or ("}" in nxt) or (not nxt.startswith("/")):
        nxt = "/dashboard"

    v = _verify_ui_credentials(u, p)
    if v:
        session["ui_auth"] = True
        session["ui_user"] = v.get("username")
        session["ui_role"] = v.get("role")
        session.permanent = True
        return redirect(nxt)

    try:
        return redirect(url_for("ui_login", next=nxt, msg="Invalid credentials"))
    except Exception:
        return redirect("/login?msg=Invalid%20credentials")


@app.post("/logout")
def ui_logout():
    try:
        session.clear()
    except Exception:
        pass
    return redirect("/login")


# --- UI user management endpoints ---

@app.get("/ui/me")
def ui_me():
    ar = require_ui_auth()
    if ar is not None:
        return ar
    cu = _current_ui_user() or {}
    return jsonify({
        "ok": True,
        "username": cu.get("username"),
        "role": cu.get("role"),
    })


@app.post("/ui/me/password")
def ui_change_my_password():
    """Change password for the currently logged-in user.

    Body:
      {"current_password": "...", "new_password": "..."}
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    cu = _current_ui_user()
    if not cu:
        return jsonify({"ok": False, "error": "auth_required"}), 401

    body = request.get_json(silent=True) or {}
    cur = str(body.get("current_password") or "").strip()
    newp = str(body.get("new_password") or "").strip()

    if not cur or not newp:
        return jsonify({"ok": False, "error": "current_password_and_new_password_required"}), 400
    if len(newp) < 8:
        return jsonify({"ok": False, "error": "new_password_too_short", "min_len": 8}), 400

    v = _verify_ui_credentials(str(cu.get("username") or ""), cur)
    if not v:
        return jsonify({"ok": False, "error": "invalid_current_password"}), 400

    users_payload = read_ui_users()
    users = users_payload.get("users") if isinstance(users_payload, dict) else None
    if not isinstance(users, dict):
        return jsonify({"ok": False, "error": "user_store_corrupt"}), 500

    uname = str(cu.get("username") or "").strip()
    rec = users.get(uname)
    if not isinstance(rec, dict):
        return jsonify({"ok": False, "error": "unknown_user"}), 404

    rec = dict(rec)
    rec["password_hash"] = generate_password_hash(newp)
    rec["updated_at"] = utc_now_iso()
    users[uname] = rec
    users_payload["users"] = users
    write_ui_users(users_payload)

    _append_hub_cmd_log({
        "event": "ui_password_changed",
        "username": uname,
        "role": str(rec.get("role") or ""),
    })

    return jsonify({"ok": True})


@app.post("/ui/admin/gm")
def ui_admin_create_or_reset_gm():
    """Admin: create or reset the game master user.

    Body:
      {"username": "gm" (optional), "password": "..."}

    Notes:
      - If username omitted, defaults to "gm".
      - Creates the user if missing.
      - Always sets/overwrites the password.
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    admin = _require_admin_user()
    if not admin:
        return jsonify({"ok": False, "error": "admin_required"}), 403

    body = request.get_json(silent=True) or {}
    uname = str(body.get("username") or "gm").strip()
    pw = str(body.get("password") or "").strip()

    if not uname:
        return jsonify({"ok": False, "error": "username_required"}), 400
    if not pw:
        return jsonify({"ok": False, "error": "password_required"}), 400
    if len(pw) < 8:
        return jsonify({"ok": False, "error": "password_too_short", "min_len": 8}), 400

    users_payload = _bootstrap_ui_users_if_needed()
    users = users_payload.get("users") if isinstance(users_payload, dict) else None
    if not isinstance(users, dict):
        return jsonify({"ok": False, "error": "user_store_corrupt"}), 500

    now = utc_now_iso()
    existing = users.get(uname)
    created = not isinstance(existing, dict)

    users[uname] = {
        "role": "gm",
        "password_hash": generate_password_hash(pw),
        "created_at": (existing.get("created_at") if isinstance(existing, dict) else now),
        "updated_at": now,
    }

    users_payload["users"] = users
    write_ui_users(users_payload)

    _append_hub_cmd_log({
        "event": "ui_gm_set",
        "admin_username": str(admin.get("username") or ""),
        "gm_username": uname,
        "created": created,
    })

    return jsonify({"ok": True, "username": uname, "created": created})


def mqtt_admin_cfg() -> tuple[str, int, str, str]:
    host = os.environ.get("HUB_MQTT_HOST", "localhost")
    port = int(os.environ.get("HUB_MQTT_PORT", "1883"))
    user = os.environ.get("HUB_MQTT_USER", "hubadmin")
    pw = os.environ.get("HUB_MQTT_PASS", "devpass123")
    return host, port, user, pw


# --- Hub health helpers ---

def _tcp_check(host: str, port: int, timeout_sec: float = 2.0) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout_sec)):
            return True, None
    except Exception as e:
        return False, str(e)


def _dir_writable_check(p: Path) -> tuple[bool, str | None]:
    try:
        p.mkdir(parents=True, exist_ok=True)
        test = p / f".writecheck.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        test.write_text("ok\n", encoding="utf-8")
        try:
            test.unlink()
        except Exception:
            pass
        return True, None
    except Exception as e:
        return False, str(e)


def _registry_rw_check() -> tuple[bool, str | None]:
    # Validate we can read and write devices.json atomically (without sudo)
    try:
        reg = read_registry()
        if not isinstance(reg, dict):
            reg = {}
        # Do NOT mutate content; just perform a write/read round-trip to ensure perms.
        write_registry(reg)
        _ = read_registry()
        return True, None
    except Exception as e:
        return False, str(e)


def _tts_config_check() -> tuple[bool, str | None]:
    # Keep this lightweight; we don't want to burn API quota on a health ping.
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return False, "missing_ELEVENLABS_API_KEY"
    if tts is None or not hasattr(tts, "synthesize"):
        return False, "tts_module_not_configured"
    return True, None


def _hub_health_snapshot() -> dict:
    now_iso = utc_now_iso()

    # Registry
    reg_ok, reg_err = _registry_rw_check()

    # Audio cache dir
    cache_dir = Path(os.environ.get("HINT_AUDIO_CACHE_DIR", "/var/lib/hint/audio_cache"))
    cache_ok, cache_err = _dir_writable_check(cache_dir)

    # MQTT connectivity (TCP reachability)
    mqtt_host, mqtt_port, _user, _pw = mqtt_admin_cfg()
    mqtt_ok, mqtt_err = _tcp_check(mqtt_host, mqtt_port, timeout_sec=2.0)

    # TTS config
    tts_ok, tts_err = _tts_config_check()

    # Audio server wiring
    audio_ok = bool(audio_bp is not None and store_audio is not None and build_audio_url is not None)
    audio_err = None if audio_ok else "audio_server_not_configured"

    # ElevenLabs reachability (lightweight; uses cached /v1/voices call)
    el_ok = None
    el_err = None
    el_checked_at = None
    try:
        el_info = _get_elevenlabs_voices_cached(force=False)
        el_ok = bool(el_info.get("ok"))
        el_err = el_info.get("error")
        el_checked_at = el_info.get("checked_at")
    except Exception as e:
        el_ok = False
        el_err = str(e)

    # Treat el_ok=None as "unknown" (don’t fail hard), but el_ok=False fails health.
    ok = bool(reg_ok and cache_ok and mqtt_ok and tts_ok and audio_ok and (el_ok is not False))

    return {
        "ok": ok,
        "ts": now_iso,
        "checks": {
            "registry_rw": {"ok": reg_ok, "error": reg_err},
            "audio_cache_dir": {"ok": cache_ok, "error": cache_err, "path": str(cache_dir)},
            "mqtt_tcp": {"ok": mqtt_ok, "error": mqtt_err, "host": mqtt_host, "port": int(mqtt_port)},
            "tts_config": {"ok": tts_ok, "error": tts_err},
            "audio_server": {"ok": audio_ok, "error": audio_err},
            "elevenlabs_api": {"ok": el_ok, "error": el_err, "checked_at": el_checked_at},
        },
    }


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

    cmd_id = str((payload or {}).get("command_id") or "")
    cmd = str((payload or {}).get("cmd") or "")

    # Log that we are about to attempt a publish.
    _append_hub_cmd_log({
        "event": "cmd_issued",
        "site_id": str(site_id),
        "device_id": str(device_id),
        "topic": topic,
        "command_id": cmd_id,
        "cmd": cmd,
        "payload": _redact_payload_for_log(payload if isinstance(payload, dict) else {}),
    })

    try:
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

        # Publish succeeded.
        _append_hub_cmd_log({
            "event": "cmd_published",
            "site_id": str(site_id),
            "device_id": str(device_id),
            "topic": topic,
            "command_id": cmd_id,
            "cmd": cmd,
        })

    except Exception as e:
        # Publish failed.
        _append_hub_cmd_log({
            "event": "cmd_publish_failed",
            "site_id": str(site_id),
            "device_id": str(device_id),
            "topic": topic,
            "command_id": cmd_id,
            "cmd": cmd,
            "error": str(e),
        })
        raise

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


# --- UI device config and phrases endpoints ---

@app.post("/ui/device/config")
def ui_device_config_set():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    device_id = sanitize_topic_id(str(body.get("device_id") or ""))
    if not device_id:
        return jsonify({"ok": False, "error": "device_id_required"}), 400

    friendly_name = body.get("friendly_name")
    default_voice_id = body.get("default_voice_id")

    if friendly_name is not None:
        friendly_name = str(friendly_name).strip()
        # keep it sane
        friendly_name = friendly_name[:64]

    if default_voice_id is not None:
        default_voice_id = str(default_voice_id).strip()
        default_voice_id = default_voice_id[:128]

    cfg = read_ui_config()
    devs = cfg.get("devices")
    if not isinstance(devs, dict):
        devs = {}
        cfg["devices"] = devs

    rec = devs.get(device_id)
    if not isinstance(rec, dict):
        rec = {}

    if friendly_name is not None:
        rec["friendly_name"] = friendly_name
    if default_voice_id is not None:
        rec["default_voice_id"] = default_voice_id

    devs[device_id] = rec
    write_ui_config(cfg)

    return jsonify({"ok": True, "device_id": device_id, "config": rec})


@app.get("/ui/phrases")
def ui_phrases_get():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    cfg = read_ui_config()
    phrases = cfg.get("phrases") if isinstance(cfg, dict) else []
    if not isinstance(phrases, list):
        phrases = []

    # sanitize output
    out = []
    for p in phrases:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        label = str(p.get("label") or "").strip()
        textv = str(p.get("text") or "").strip()
        if not pid:
            continue
        out.append({"id": pid, "label": label, "text": textv})

    out.sort(key=lambda x: (x.get("label") or x.get("id") or ""))
    return jsonify({"ok": True, "phrases": out})


@app.post("/ui/phrases")
def ui_phrases_upsert():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    label = str(body.get("label") or "").strip()
    textv = str(body.get("text") or "").strip()
    pid = str(body.get("id") or "").strip()

    if not label:
        return jsonify({"ok": False, "error": "label_required"}), 400
    if not textv:
        return jsonify({"ok": False, "error": "text_required"}), 400

    label = label[:64]
    textv = textv[:2000]

    if not pid:
        pid = sanitize_topic_id(f"p-{int(time.time())}-{secrets.token_hex(3)}")

    cfg = read_ui_config()
    phrases = cfg.get("phrases")
    if not isinstance(phrases, list):
        phrases = []
        cfg["phrases"] = phrases

    updated = False
    for p in phrases:
        if isinstance(p, dict) and str(p.get("id") or "") == pid:
            p["label"] = label
            p["text"] = textv
            updated = True
            break

    if not updated:
        phrases.append({"id": pid, "label": label, "text": textv})

    write_ui_config(cfg)
    return jsonify({"ok": True, "id": pid, "label": label, "text": textv, "updated": updated})


@app.post("/ui/phrases/delete")
def ui_phrases_delete():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    pid = str(body.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "id_required"}), 400

    cfg = read_ui_config()
    phrases = cfg.get("phrases") if isinstance(cfg, dict) else None
    if not isinstance(phrases, list):
        return jsonify({"ok": True, "deleted": False})

    before = len(phrases)
    cfg["phrases"] = [p for p in phrases if not (isinstance(p, dict) and str(p.get("id") or "") == pid)]
    deleted = len(cfg["phrases"]) != before
    if deleted:
        write_ui_config(cfg)

    return jsonify({"ok": True, "deleted": deleted, "id": pid})


# --- UI cancel endpoint ---


@app.post("/ui/cancel")
def ui_cancel():
    """Send a cancel command to a device.

    Body:
      {
        "device_id": "...",
        "command_id": "optional",
        "ttl_sec": 300,
        "priority": 10,
        "mode": one of "stop", "clear_queue", "stop_and_clear" (default "stop")
      }

    The device agent is responsible for interpreting cancel (stop current audio and/or clear queue).
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    device_id = sanitize_topic_id(str(body.get("device_id") or ""))
    if not device_id:
        return jsonify({"ok": False, "error": "device_id_required"}), 400

    # Parse and validate mode
    mode = str(body.get("mode") or "stop").strip().lower()
    allowed_modes = ("stop", "clear_queue", "stop_and_clear")
    if mode not in allowed_modes:
        return jsonify({
            "ok": False,
            "error": "invalid_mode",
            "allowed": list(allowed_modes),
        }), 400

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return jsonify({"ok": False, "error": "unknown_device"}), 404

    site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

    command_id = sanitize_topic_id(body.get("command_id") or f"cancel-{device_id}-{int(time.time())}")
    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    payload = make_command_payload(
        command_id=command_id,
        cmd="cancel",
        params={"mode": mode},
        ttl_sec=ttl_sec if ttl_sec is not None else 300,
        priority=priority if priority is not None else 10,
    )

    try:
        publish_command(site_id, device_id, payload)
        _mark_sent_in_registry(reg, device_id, command_id, "cancel")
        write_registry(reg)
    except Exception as e:
        return jsonify({"ok": False, "error": f"mqtt_publish_failed:{e}"}), 500

    return jsonify({
        "ok": True,
        "device_id": device_id,
        "command_id": command_id,
        "mode": mode,
    })


# --- UI speak stop endpoint ---
@app.post("/ui/speak/stop")
def ui_speak_stop():
    """Convenience endpoint: stop current playback on a device.

    Body:
      {"device_id": "...", "command_id": "optional", "ttl_sec": 300, "priority": 10}

    This simply publishes a `cancel` command with mode="stop".
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    device_id = sanitize_topic_id(str(body.get("device_id") or ""))
    if not device_id:
        return jsonify({"ok": False, "error": "device_id_required"}), 400

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return jsonify({"ok": False, "error": "unknown_device"}), 404

    site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

    command_id = sanitize_topic_id(body.get("command_id") or f"cancel-{device_id}-{int(time.time())}")
    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    payload = make_command_payload(
        command_id=command_id,
        cmd="cancel",
        params={"mode": "stop"},
        ttl_sec=ttl_sec if ttl_sec is not None else 300,
        priority=priority if priority is not None else 10,
    )

    try:
        publish_command(site_id, device_id, payload)
        _mark_sent_in_registry(reg, device_id, command_id, "cancel")
        write_registry(reg)
    except Exception as e:
        return jsonify({"ok": False, "error": f"mqtt_publish_failed:{e}"}), 500

    return jsonify({
        "ok": True,
        "device_id": device_id,
        "command_id": command_id,
        "mode": "stop",
    })


# --- UI device speaker test (beep) ---

@app.post("/ui/device/beep")
def ui_beep():
    """Send a short speaker test command to a device.

    Body:
      {"device_id": "...", "command_id": "optional", "ttl_sec": 60, "priority": 10}

    The device agent should implement cmd="beep" (or alias) and play a short bundled beep WAV.
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    device_id = sanitize_topic_id(str(body.get("device_id") or ""))
    if not device_id:
        return jsonify({"ok": False, "error": "device_id_required"}), 400

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return jsonify({"ok": False, "error": "unknown_device"}), 404

    site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))

    command_id = sanitize_topic_id(body.get("command_id") or f"beep-{device_id}-{int(time.time())}")
    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    # Default short TTL + high-ish priority for a speaker test.
    payload = make_command_payload(
        command_id=command_id,
        cmd="beep",
        params={},
        ttl_sec=ttl_sec if ttl_sec is not None else 60,
        priority=priority if priority is not None else 10,
    )

    # Optional: track the last beep command for UI/debugging.
    try:
        rec["last_beep"] = {
            "command_id": command_id,
            "issued_at": int(time.time()),
            "state": "sent",
        }
        reg[device_id] = rec
        write_registry(reg)
    except Exception:
        pass

    # Audit log: request intent (publish_command also logs cmd_issued/cmd_published).
    _append_hub_cmd_log({
        "event": "beep_requested",
        "site_id": str(site_id),
        "device_id": str(device_id),
        "command_id": str(command_id),
    })

    try:
        publish_command(site_id, device_id, payload)
        _mark_sent_in_registry(reg, device_id, command_id, "beep")
        write_registry(reg)
    except Exception as e:
        return jsonify({"ok": False, "error": f"mqtt_publish_failed:{e}"}), 500

    return jsonify({
        "ok": True,
        "device_id": device_id,
        "command_id": command_id,
    })


# Back-compat alias for UI callers
@app.post("/ui/device/speaker-test")
def ui_speaker_test():
    return ui_beep()

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

    # Prefer per-device default voice set via UI config, then env default.
    ui_dev_cfg = get_device_ui_cfg(device_id)
    default_voice_id = str(ui_dev_cfg.get("default_voice_id") or "").strip()
    if not default_voice_id:
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

    # Best-effort cleanup of hub audio cache (does not raise)
    _cleanup_audio_cache_if_needed()

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

    # Track speak progress for the dashboard (end-to-end)
    try:
        rec_last_speak = {
            "command_id": command_id,
            "issued_at": int(time.time()),
            "sent_at": None,
            "state": "generated",  # generated -> sent -> accepted/completed/error (from last_ack)
            "text": text[:200],
            "voice_id": str(tts_opts.get("voice_id") or "")[:128],
            "ext": ext,
            "audio_id": audio_id,
        }
        rec["last_speak"] = rec_last_speak
        reg[device_id] = rec
        # Write now so the dashboard can show the job even if MQTT publish fails.
        write_registry(reg)
        _append_hub_cmd_log({
            "event": "speak_generated",
            "site_id": str(site_id),
            "device_id": str(device_id),
            "command_id": str(command_id),
            "audio_id": str(audio_id),
            "ext": str(ext),
            "voice_id": str(tts_opts.get("voice_id") or ""),
            "text": text[:200],
        })
    except Exception:
        pass

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

        # Mark last_speak as sent (dashboard uses this + last_ack to show progress)
        try:
            rec2 = reg.get(device_id)
            if isinstance(rec2, dict):
                ls = rec2.get("last_speak")
                if isinstance(ls, dict) and str(ls.get("command_id") or "") == command_id:
                    ls = dict(ls)
                    ls["sent_at"] = int(time.time())
                    ls["state"] = "sent"
                    rec2["last_speak"] = ls
                    reg[device_id] = rec2
        except Exception:
            pass

        write_registry(reg)

        # Best-effort cleanup of hub audio cache after storing/serving audio
        _cleanup_audio_cache_if_needed()

    except Exception as e:
        return jsonify({"ok": False, "error": f"mqtt_publish_failed:{e}"}), 500

    return jsonify({
        "ok": True,
        "device_id": device_id,
        "command_id": command_id,
        "audio_id": audio_id,
        "url": url,
    })

@app.get("/health")
def health():
    """Hub health endpoint.

    Intended for devices and external monitors.
    Does NOT require UI auth.

    Returns 200 when all checks pass, otherwise 503.
    """
    snap = _hub_health_snapshot()
    code = 200 if snap.get("ok") else 503
    return jsonify(snap), code


# Authenticated hub health endpoint for the dashboard
# --- Authenticated hub health endpoint for the dashboard ---
@app.get("/ui/health")
def ui_health():
    """Authenticated hub health endpoint for the dashboard."""
    ar = require_ui_auth()
    if ar is not None:
        return ar

    snap = _hub_health_snapshot()
    audio = _audio_cache_status()

    ota_latest = _read_ota_latest_version()
    hub_status = _hub_update_status_cached(force=False)

    return jsonify({
        "ok": bool(snap.get("ok")),
        "hub_health": snap,
        "audio_cache": audio,
        "ota_latest_version": ota_latest,
        "hub_version": hub_status.get("hub_version"),
        "hub_latest_version": hub_status.get("hub_latest_version"),
        "hub_update_available": hub_status.get("hub_update_available"),
        "hub_checked_at": hub_status.get("hub_checked_at"),
        "hub_update_error": hub_status.get("hub_update_error"),
    })


# --- UI: Hub command log tail endpoint ---
@app.get("/ui/logs/hub_cmd")
def ui_hub_cmd_log_tail():
    """Return the last N lines of the hub command audit log.

    Query:
      - lines: int (default 500, max 5000)

    This is meant for quick demo debugging without SSH.
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    try:
        lines = int(request.args.get("lines") or "500")
    except Exception:
        lines = 500

    text = _tail_text_file(HUB_CMD_LOG_PATH, max_lines=lines)
    if not text:
        text = ""  # empty is fine

    resp = Response(text, mimetype="text/plain; charset=utf-8")
    # Helpful default filename when opened in a browser
    resp.headers["Content-Disposition"] = "inline; filename=hub_cmd.log"
    return resp


# --- UI: Demo runbook page ---
@app.get("/ui/runbook")
def ui_runbook_page():
    """Small ops runbook for demos (same-origin, authenticated)."""
    ar = require_ui_auth()
    if ar is not None:
        return ar

    html = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Hint Hub Runbook</title>
  <link rel=\"stylesheet\" href=\"/static/css/hub.css\" />
</head>
<body>
  <div class=\"page\" style=\"max-width: 900px; margin: 24px auto; padding: 16px;\">
    <div style=\"display:flex; align-items:center; justify-content:space-between; gap:12px;\">
      <h1 style=\"margin:0;\">Demo Runbook</h1>
      <div style=\"display:flex; gap:10px;\">
        <a class=\"btn\" href=\"/dashboard\">Dashboard</a>
        <a class=\"btn\" href=\"/speak\">Speak</a>
      </div>
    </div>

    <h2>Fast checks</h2>
    <ul>
      <li><b>No sound?</b> Use <b>Beep</b> and <b>Speaker Test</b> buttons on the Dashboard for that device.</li>
      <li><b>Speak stuck on “Accepted”</b> (often after device agent restart): click <b>Cancel/Stop</b> once.</li>
      <li><b>Device offline</b>: confirm power/network, then restart the agent: <code>sudo systemctl restart hint-device-agent</code> (or your service name).</li>
      <li><b>Hub looks unhealthy</b>: check the health panel on Dashboard; then view recent log tail below.</li>
    </ul>

    <h2>Where to look</h2>
    <ul>
      <li><b>Hub audit log</b>: <a href=\"/ui/logs/hub_cmd?lines=800\">/ui/logs/hub_cmd</a></li>
      <li><b>Hub service logs</b>: <code>sudo journalctl -u hint-hub-pair -n 200 --no-pager</code></li>
      <li><b>Device agent logs</b>: <code>sudo journalctl -u hint-device-agent -n 200 --no-pager</code></li>
    </ul>

    <h2>What “stalled speak” means</h2>
    <ul>
      <li><b>Sent</b> but no ACK: device didn’t receive / process the command (MQTT / agent down).</li>
      <li><b>Accepted</b> but no Completed: agent restarted mid-playback, audio backend stuck, or output device issue.</li>
    </ul>

    <h2>Recent hub_cmd.log (tail)</h2>
    <pre style=\"white-space:pre-wrap; background: rgba(0,0,0,0.04); padding: 12px; border-radius: 8px;\" id=\"log\">Loading…</pre>
    <script>
      fetch('/ui/logs/hub_cmd?lines=200')
        .then(r => r.text())
        .then(t => { document.getElementById('log').textContent = t || '(empty)'; })
        .catch(e => { document.getElementById('log').textContent = 'Failed to load log: ' + e; });
    </script>
  </div>
</body>
</html>"""

    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp
# --- UI cancel endpoint ---

@app.post("/ui/speak/retry")
def ui_speak_retry():
    """Retry the most recent speak for a device (re-send the already-generated audio).

    Body:
      {"device_id": "...", "command_id": "optional", "ttl_sec": 300, "priority": 5}

    Uses devices.json fields:
      - rec.ota_token
      - rec.last_speak.audio_id / ext / text

    Creates a NEW command_id by default so the device does not dedupe it.
    """
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}
    device_id = sanitize_topic_id(str(body.get("device_id") or ""))
    if not device_id:
        return jsonify({"ok": False, "error": "device_id_required"}), 400

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return jsonify({"ok": False, "error": "unknown_device"}), 404

    last = rec.get("last_speak") if isinstance(rec.get("last_speak"), dict) else None
    if not isinstance(last, dict):
        return jsonify({"ok": False, "error": "no_last_speak"}), 400

    audio_id = str(last.get("audio_id") or "").strip()
    ext = str(last.get("ext") or "wav").strip().lstrip(".")
    text = str(last.get("text") or "").strip()

    if not audio_id:
        return jsonify({"ok": False, "error": "no_last_speak_audio_id"}), 400

    site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))
    ota_token = str(rec.get("ota_token") or "").strip()
    if not ota_token:
        return jsonify({"ok": False, "error": "missing_ota_token"}), 500

    if build_audio_url is None:
        return jsonify({"ok": False, "error": "audio_server_not_configured"}), 500

    try:
        url = build_audio_url(
            base_url=HUB_BASE_URL,
            audio_id=audio_id,
            device_id=device_id,
            token=ota_token,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"audio_url_failed:{e}"}), 500

    # New command id (avoid device-side dedupe)
    command_id = sanitize_topic_id(body.get("command_id") or f"speakretry-{device_id}-{int(time.time())}")
    ttl_sec = body.get("ttl_sec")
    priority = body.get("priority")

    # Track retry in registry for UI
    try:
        rec_last_speak = {
            "command_id": command_id,
            "issued_at": int(time.time()),
            "sent_at": None,
            "state": "generated",
            "text": text[:200],
            "voice_id": str(last.get("voice_id") or "")[:128],
            "ext": ext,
            "audio_id": audio_id,
            "retry_of": str(last.get("command_id") or "")[:128],
        }
        rec["last_speak"] = rec_last_speak
        reg[device_id] = rec
        write_registry(reg)
        _append_hub_cmd_log({
            "event": "speak_generated",
            "site_id": str(site_id),
            "device_id": str(device_id),
            "command_id": str(command_id),
            "audio_id": str(audio_id),
            "ext": str(ext),
            "voice_id": str(last.get("voice_id") or ""),
            "text": text[:200],
        })
    except Exception:
        pass

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

        # Mark last_speak as sent
        try:
            rec2 = reg.get(device_id)
            if isinstance(rec2, dict):
                ls = rec2.get("last_speak")
                if isinstance(ls, dict) and str(ls.get("command_id") or "") == command_id:
                    ls = dict(ls)
                    ls["sent_at"] = int(time.time())
                    ls["state"] = "sent"
                    rec2["last_speak"] = ls
                    reg[device_id] = rec2
        except Exception:
            pass

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

    ui_cfg = read_ui_config()
    ui_devs = ui_cfg.get("devices") if isinstance(ui_cfg, dict) else {}
    if not isinstance(ui_devs, dict):
        ui_devs = {}

    devices = []
    changed = False

    for did_key, rec in reg.items():
        if not isinstance(rec, dict):
            continue

        # Work on the canonical registry record for this device_id
        device_id = str(rec.get("device_id") or did_key)

        # --- Derive online/offline from last_seen ---
        # last_seen is expected to be an ISO string (UTC). If missing or too old,
        # treat device as offline.
        try:
            offline_after = int(os.environ.get("HINT_DEVICE_OFFLINE_AFTER_SEC", "60"))
        except Exception:
            offline_after = 60
        offline_after = max(5, min(offline_after, 24 * 3600))

        last_seen_iso = rec.get("last_seen")
        last_seen_ts = None
        if isinstance(last_seen_iso, str) and last_seen_iso.strip():
            try:
                s = last_seen_iso.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                last_seen_ts = int(dt.timestamp())
            except Exception:
                last_seen_ts = None

        now_ts = int(time.time())
        age_sec = (now_ts - last_seen_ts) if isinstance(last_seen_ts, int) else None
        is_online = bool(age_sec is not None and age_sec >= 0 and age_sec <= offline_after)
        offline_reason = None
        if not is_online:
            if last_seen_ts is None:
                offline_reason = "never_seen"
            elif age_sec is not None and age_sec < 0:
                offline_reason = "clock_skew"
            else:
                offline_reason = "stale_last_seen"

        # Merge UI config fields into response record
        drec = dict(rec)
        # --- Last reported device state (from MQTT state topic) ---
        # The device agent publishes a retained JSON state payload. We store the last payload
        # in the registry as `last_state` (if present). Surface key health fields to the UI.
        last_state = rec.get("last_state")
        if not isinstance(last_state, dict):
            # Backward/alternate naming safety
            last_state = rec.get("state") if isinstance(rec.get("state"), dict) else None

        if isinstance(last_state, dict):
            # Keep the full state available for debugging
            drec["last_state"] = last_state

            # Hub reachability / audio health (published by device)
            drec["hub_health_url"] = last_state.get("hub_health_url")
            drec["hub_reachable"] = last_state.get("hub_reachable")
            drec["hub_rtt_ms"] = last_state.get("hub_rtt_ms")
            drec["hub_last_ok_at"] = last_state.get("hub_last_ok_at")
            drec["hub_last_err_at"] = last_state.get("hub_last_err_at")
            drec["hub_last_err"] = last_state.get("hub_last_err")

            # Optional: device-side audio/player details if present
            drec["audio_device"] = last_state.get("audio_device")
            drec["audio_backend"] = last_state.get("audio_backend")

            # Simple derived status for dashboard badges
            hr = last_state.get("hub_reachable")
            if hr is True:
                drec["hub_health"] = "ok"
            elif hr is False:
                drec["hub_health"] = "error"
            else:
                drec["hub_health"] = "unknown"

        # Preserve the raw registry status separately
        drec["registry_status"] = drec.get("status")

        # Derived online/offline fields for UI
        drec["online"] = is_online
        drec["offline_after_sec"] = offline_after
        drec["last_seen_ts"] = last_seen_ts
        drec["last_seen_age_sec"] = age_sec
        drec["offline_reason"] = offline_reason

        # Normalize status for the UI table
        drec["status"] = "online" if is_online else "offline"

        u = ui_devs.get(device_id)
        if isinstance(u, dict):
            if "friendly_name" in u:
                drec["friendly_name"] = u.get("friendly_name")
            if "default_voice_id" in u:
                drec["default_voice_id"] = u.get("default_voice_id")
        # If the device is online but cannot reach the hub's audio health endpoint,
        # expose a warning flag for the dashboard.
        if drec.get("online") and drec.get("hub_reachable") is False:
            drec["warning"] = "hub_unreachable"
        elif not drec.get("online"):
            drec["warning"] = "device_offline"
        else:
            drec["warning"] = None

        # --- Stalled speak detection (dashboard actions) ---
        # A speak is considered "stalled" when it's in a mid-flight state (sent/accepted)
        # longer than a configured threshold.
        stalled = False
        stalled_reason = None
        stalled_age_sec = None

        # Thresholds (seconds) for early warning flags.
        # These are separate from the hard timeout transitions below.
        try:
            stall_sent_after = int(os.environ.get("HINT_SPEAK_STALL_SENT_AFTER_SEC", "25"))
        except Exception:
            stall_sent_after = 25
        try:
            stall_accepted_after = int(os.environ.get("HINT_SPEAK_STALL_ACCEPTED_AFTER_SEC", "60"))
        except Exception:
            stall_accepted_after = 60
        stall_sent_after = max(5, min(stall_sent_after, 3600))
        stall_accepted_after = max(10, min(stall_accepted_after, 24 * 3600))

        ls2 = drec.get("last_speak") if isinstance(drec.get("last_speak"), dict) else None
        if isinstance(ls2, dict):
            st = str(ls2.get("state") or "").strip().lower()

            # If we already marked it timeout/error, it is definitely stalled.
            if st in ("timeout", "error"):
                stalled = True
                stalled_reason = str(ls2.get("error") or st)

            # Otherwise, compute how long it's been in-flight.
            else:
                base_ts = None
                if st == "accepted" and isinstance(ls2.get("ack_ts"), (int, float)):
                    base_ts = int(ls2.get("ack_ts"))
                elif isinstance(ls2.get("sent_at"), (int, float)):
                    base_ts = int(ls2.get("sent_at"))
                elif isinstance(ls2.get("issued_at"), (int, float)):
                    base_ts = int(ls2.get("issued_at"))

                if base_ts is not None:
                    stalled_age_sec = max(0, int(time.time()) - base_ts)

                    if st == "sent" and stalled_age_sec >= stall_sent_after:
                        stalled = True
                        stalled_reason = f"sent_no_ack_{stalled_age_sec}s"
                    elif st == "accepted" and stalled_age_sec >= stall_accepted_after:
                        stalled = True
                        stalled_reason = f"accepted_no_complete_{stalled_age_sec}s"

        drec["speak_stalled"] = bool(stalled)
        drec["speak_stalled_reason"] = stalled_reason
        drec["speak_stalled_age_sec"] = stalled_age_sec

        # --- Reconcile last_speak state with last_ack (end-to-end progress) ---
        # We want the UI to show an end-to-end pipeline:
        #   generated -> sent -> accepted -> completed
        # plus: error/timeout when ACKs do not arrive.
        #
        # Notes:
        # - `last_ack.ts` comes from the device (epoch seconds).
        # - `last_speak.issued_at/sent_at` are hub timestamps.
        # - We only reconcile when the ACK's command_id matches last_speak.command_id.
        ls = rec.get("last_speak") if isinstance(rec.get("last_speak"), dict) else None
        ack = rec.get("last_ack") if isinstance(rec.get("last_ack"), dict) else None

        # Timeouts (seconds) can be tuned via env vars.
        # - accepted timeout: how long after we send before we expect *any* ACK.
        # - completed timeout: how long after accepted before we expect completion.
        try:
            ack_accept_timeout = int(os.environ.get("HINT_SPEAK_ACK_ACCEPT_TIMEOUT_SEC", "20"))
        except Exception:
            ack_accept_timeout = 20
        try:
            # Default shorter so the Speak UI doesn't stay stuck on "accepted" if the device restarts mid-playback.
            # Can be overridden in hub env.
            ack_complete_timeout = int(os.environ.get("HINT_SPEAK_ACK_COMPLETE_TIMEOUT_SEC", "60"))
        except Exception:
            ack_complete_timeout = 60
        ack_accept_timeout = max(5, min(ack_accept_timeout, 3600))
        ack_complete_timeout = max(10, min(ack_complete_timeout, 24 * 3600))

        now_ts = int(time.time())

        def _set_last_speak_state(new_ls: dict, *, reason: str | None = None) -> None:
            """Persist updated last_speak into both response and registry."""
            nonlocal changed

            # Best-effort audit log of state transitions
            try:
                prev_ls = rec.get("last_speak") if isinstance(rec.get("last_speak"), dict) else {}
                prev_state = str(prev_ls.get("state") or "").strip().lower()
                next_state = str(new_ls.get("state") or "").strip().lower()

                if next_state and next_state != prev_state:
                    _append_hub_cmd_log({
                        "event": "speak_state",
                        "site_id": str(rec.get("site_id") or "demo"),
                        "device_id": str(rec.get("device_id") or device_id),
                        "command_id": str(new_ls.get("command_id") or ""),
                        "audio_id": str(new_ls.get("audio_id") or ""),
                        "from": prev_state,
                        "to": next_state,
                        "reason": reason,
                        "ack": rec.get("last_ack") if isinstance(rec.get("last_ack"), dict) else {},
                    })
            except Exception:
                pass

            rec["last_speak"] = new_ls
            drec["last_speak"] = new_ls
            reg[device_id] = rec
            changed = True

        if isinstance(ls, dict):
            # Always include last_speak in response even if we don't change it.
            drec["last_speak"] = ls

            ls_cid = str(ls.get("command_id") or "")
            ls_state = str(ls.get("state") or "").strip().lower()
            sent_at = ls.get("sent_at")
            issued_at = ls.get("issued_at")
            ack_ts = ls.get("ack_ts")  # when we first saw an ACK (epoch seconds)

            # If we have a matching ACK, use it as truth.
            if isinstance(ack, dict) and str(ack.get("command_id") or "") == ls_cid and ls_cid:
                ack_status = str(ack.get("status") or "").strip().lower()
                if ack_status:
                    # Normalize status names
                    if ack_status in ("accepted", "completed"):
                        if ls_state != ack_status:
                            new_ls = dict(ls)
                            new_ls["state"] = ack_status
                            # record first-ack timestamp
                            if "ts" in ack and new_ls.get("ack_ts") is None:
                                new_ls["ack_ts"] = ack.get("ts")
                            if ack_status == "completed":
                                new_ls["completed_at"] = ack.get("ts") if "ts" in ack else now_ts
                            _set_last_speak_state(new_ls, reason="ack_status")
                        else:
                            # Keep response consistent
                            drec["last_speak"] = ls
                    else:
                        # Treat anything else as error-like
                        if ls_state != "error":
                            new_ls = dict(ls)
                            new_ls["state"] = "error"
                            new_ls["error"] = ack.get("error") or ack.get("details") or ack_status
                            if "ts" in ack and new_ls.get("ack_ts") is None:
                                new_ls["ack_ts"] = ack.get("ts")
                            _set_last_speak_state(new_ls, reason="ack_error")

            # No matching ACK yet: enforce timeouts based on hub timestamps.
            else:
                # Establish a baseline start time for timeout checks
                base = None
                if isinstance(sent_at, (int, float)):
                    base = int(sent_at)
                elif isinstance(issued_at, (int, float)):
                    base = int(issued_at)

                # If we never even marked it sent, don't timeout it here.
                if base is not None:
                    # 1) Waiting for first ACK after send
                    if ls_state in ("generated", "sent"):
                        if (now_ts - base) > ack_accept_timeout:
                            new_ls = dict(ls)
                            new_ls["state"] = "timeout"
                            new_ls["error"] = f"ack_timeout_after_{ack_accept_timeout}s"
                            new_ls["timeout_at"] = now_ts
                            _set_last_speak_state(new_ls, reason="ack_timeout")

                    # 2) Waiting for completion after accepted
                    elif ls_state == "accepted":
                        # Prefer ack_ts (device-provided) if present, else fall back to base
                        accepted_base = int(ack_ts) if isinstance(ack_ts, (int, float)) else base
                        if (now_ts - accepted_base) > ack_complete_timeout:
                            new_ls = dict(ls)
                            new_ls["state"] = "timeout"
                            new_ls["error"] = f"complete_timeout_after_{ack_complete_timeout}s"
                            new_ls["timeout_at"] = now_ts
                            _set_last_speak_state(new_ls, reason="complete_timeout")

        devices.append(drec)

    # Persist only when we actually advanced last_speak.state based on ACKs
    if changed:
        try:
            write_registry(reg)
        except Exception:
            pass


    devices.sort(key=lambda r: str(r.get("device_id", "")))
    ota_latest = _read_ota_latest_version()
    hub_status = _hub_update_status_cached(force=False)

    hub_health = _hub_health_snapshot()

    return jsonify({
        "ok": True,
        "devices": devices,
        "hub_health": hub_health,
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
def ui_speak_page():
    ar = require_ui_auth()
    if ar is not None:
        return ar
    return render_template("speak.html")

# Dashboard HTML shell route
@app.get("/dashboard")
def dashboard():
    ar = require_ui_admin()
    if ar is not None:
        return ar
    return render_template("dashboard.html")


if __name__ == "__main__":
    # When launched via systemd ExecStart=/usr/bin/python3 .../hub_pair.py
    # we must explicitly start the Flask dev server.
    host = (os.environ.get("HUB_BIND") or "0.0.0.0").strip()
    try:
        port = int(os.environ.get("HUB_PORT") or "5000")
    except Exception:
        port = 5000

    # Never enable the reloader under systemd (it will fork/re-exec and confuse service state)
    app.run(host=host, port=port, debug=False, use_reloader=False)
        # MQTT inbound ACK topic handler (not shown in full here)...
        # After: rec["last_ack"] = payload
        # Insert:
    try:
        _append_hub_cmd_log({
            "event": "ack_received",
            "site_id": str(rec.get("site_id") or "demo"),
            "device_id": str(rec.get("device_id") or device_id),
            "command_id": str((rec.get("last_ack") or {}).get("command_id") or ""),
            "ack": rec.get("last_ack") if isinstance(rec.get("last_ack"), dict) else {},
        })
    except Exception:
        pass
#
# MQTT inbound message handler (ACK/state)
#

def handle_inbound_mqtt_message(topic: str, payload: dict, reg: dict):
    """Process inbound MQTT messages from devices. Called from MQTT client loop."""
    # Example topic: site/demo/device/device123/ack
    #                site/demo/device/device123/state
    # payload is already decoded as dict
    parts = topic.split("/")
    if len(parts) < 5 or parts[0] != "site" or parts[2] != "device":
        return
    site_id = parts[1]
    device_id = parts[3]
    msg_type = parts[4]
    rec = reg.get(device_id)
    if not isinstance(rec, dict):
        return

    if msg_type == "ack":
        ack = payload if isinstance(payload, dict) else {}
        rec["last_ack"] = ack
        try:
            _append_hub_cmd_log({
                "event": "ack_received",
                "site_id": str(rec.get("site_id") or "demo"),
                "device_id": str(rec.get("device_id") or device_id),
                "command_id": str((rec.get("last_ack") or {}).get("command_id") or ""),
                "ack": rec.get("last_ack") if isinstance(rec.get("last_ack"), dict) else {},
            })
        except Exception:
            pass
        # ... rest of ack handling ...
    elif msg_type == "state":
        state = payload if isinstance(payload, dict) else {}
        rec["last_state"] = state
        # ... rest of state handling ...