from flask import Flask, request, jsonify, send_file, abort
import os
import subprocess
import secrets
import string
import json
import time
from datetime import datetime, timezone
from pathlib import Path

app = Flask(__name__)

PAIR_CODE = "483921"  # you will type this into the device setup page

REGISTRY_DIR = Path("/var/lib/hint")
REGISTRY_PATH = REGISTRY_DIR / "devices.json"
OTA_DIR = Path("/opt/hint/ota")
BUILD_OTA_SCRIPT = Path("/home/hub/hint-platform/hub/build_ota.py")

# In-memory rollout jobs (simple, single-hub process). For durability later, persist to REGISTRY_DIR.
ROLLOUTS: dict[str, dict] = {}


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
    resolved = []
    for did, cid in inflight.items():
        rec = reg.get(did)
        if not isinstance(rec, dict):
            continue
        ack = _device_ack_for(rec, cid)
        if not ack:
            continue

        st = str(ack.get("status", "")).lower()
        if st == "completed":
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
            inflight[did] = cid
        except Exception as e:
            failed[did] = {"status": "error", "error": str(e)}

    job["in_flight"] = inflight
    job["failed"] = failed

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


def publish_update(site_id: str, device_id: str, command_id: str) -> None:
    host, port, user, pw = mqtt_admin_cfg()
    topic = f"site/{site_id}/device/{device_id}/cmd"
    payload = json.dumps({"command_id": command_id, "cmd": "update"})
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
            payload,
        ]
    )



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

    command_id = sanitize_topic_id(body.get("command_id") or f"ota-{int(time.time())}")

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
        try:
            publish_update(site_id, did, command_id)
            sent.append(did)
        except Exception:
            skipped.append(did)

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

    command_id = sanitize_topic_id(body.get("command_id") or f"ota-{int(time.time())}")

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))
        try:
            publish_update(site_id, did, command_id)
            sent.append(did)
        except Exception:
            skipped.append(did)

    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})


#
# UI proxy endpoints (same-origin). No API key required from the browser.

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
    return jsonify({"ok": True, "devices": devices})


@app.post("/ui/update")
def ui_update():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}

    reg = read_registry()
    if not isinstance(reg, dict):
        reg = {}

    targets, err = _fleet_targets(body, reg)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    command_id = sanitize_topic_id(body.get("command_id") or f"ota-{int(time.time())}")

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))
        try:
            publish_update(site_id, did, command_id)
            sent.append(did)
        except Exception:
            skipped.append(did)

    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})



@app.post("/ui/build-and-update")
def ui_build_and_update():
    ar = require_ui_auth()
    if ar is not None:
        return ar

    body = request.get_json(silent=True) or {}

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

    command_id = sanitize_topic_id(body.get("command_id") or f"ota-{int(time.time())}")

    sent: list[str] = []
    skipped: list[str] = []

    for did in targets:
        rec = reg.get(did)
        if not isinstance(rec, dict):
            skipped.append(did)
            continue

        site_id = sanitize_topic_id(str(rec.get("site_id") or "demo"))
        try:
            publish_update(site_id, did, command_id)
            sent.append(did)
        except Exception:
            skipped.append(did)

    return jsonify({"ok": True, "command_id": command_id, "sent": sent, "skipped": skipped})


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

    summary = {
        "id": job.get("id"),
        "state": job.get("state"),
        "batch_size": job.get("batch_size"),
        "targets_total": len(job.get("targets") or []),
        "pending": len(job.get("pending") or []),
        "in_flight": len(job.get("in_flight") or {}),
        "done": len(job.get("done") or {}),
        "failed": len(job.get("failed") or {}),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }

    return jsonify({"ok": True, "job": job, "summary": summary})


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
      <span class=\"muted\">Auto-refresh: 5s</span>
    </div>
    <div class=\"controls right\">
      <button id=\"refresh\">Refresh</button>
      <button id=\"updateAll\">Update All</button>
      <button id=\"buildUpdateAll\">Build+Update All</button>
    </div>
  </header>

  <div class="panel">
    <h3>Staged Rollout</h3>
    <div class="controls">
      <label>Batch size: <input id="batchSize" type="number" min="1" max="50" value="5" /></label>
      <button id="rolloutUpdateAll">Staged Update All</button>
      <button id="rolloutBuildUpdateAll">Staged Build+Update All</button>
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
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id=\"tbody\">
      <tr><td colspan=\"6\" class=\"muted\">Loading…</td></tr>
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

  async function refresh(){
    try {
      const data = await api('/ui/status', 'GET');
      const devices = data.devices || [];
      render(devices);
    } catch (e) {
      render([]);
      log(`Refresh failed: ${e.message}`);
    }
  }

  function render(devices){
    const tbody = $(\"tbody\");
    if (!devices.length){
      tbody.innerHTML = `<tr><td colspan=\"6\" class=\"muted\">No devices.</td></tr>`;
      return;
    }

    tbody.innerHTML = devices.map(d => {
      const did = fmt(d.device_id);
      const st = fmt(d.status || 'unknown');
      const ls = fmt(d.last_seen || '');
      const av = fmt(d.agent_version || '(unknown)');
      const sv = fmt(d.setup_version || '(unknown)');
      return `
        <tr>
          <td><span class=\"pill\">${did}</span></td>
          <td class=\"${statusClass(st)}\">${st}</td>
          <td class=\"muted\">${ls}</td>
          <td>${av}</td>
          <td>${sv}</td>
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
            const r = await api('/ui/update', 'POST', { device_id: device, command_id: `ota-${device}-${Date.now()}` });
            log(`Update result: ${JSON.stringify(r)}`);
          } else {
            log(`Build+Update ${device}...`);
            const r = await api('/ui/build-and-update', 'POST', { device_id: device, command_id: `ota-${device}-${Date.now()}` });
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
      return;
    }
    el.innerHTML = `
      <div><span class="pill">Job</span> ${summary.id}</div>
      <div><span class="pill">State</span> ${summary.state}</div>
      <div><span class="pill">Total</span> ${summary.targets_total}</div>
      <div><span class="pill">Pending</span> ${summary.pending}</div>
      <div><span class="pill">In-flight</span> ${summary.in_flight}</div>
      <div><span class="pill">Done</span> ${summary.done}</div>
      <div><span class="pill">Failed</span> ${summary.failed}</div>
    `;
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
      const r = await api('/ui/rollout/start', 'POST', { all: true, batch_size: bs, build: false });
      currentRolloutJobId = r.job_id;
      rolloutFinishedLoggedFor = null;
      renderRollout(r.job ? {
        id: r.job.id,
        state: r.job.state,
        targets_total: (r.job.targets||[]).length,
        pending: (r.job.pending||[]).length,
        in_flight: Object.keys(r.job.in_flight||{}).length,
        done: Object.keys(r.job.done||{}).length,
        failed: Object.keys(r.job.failed||{}).length,
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
      const r = await api('/ui/rollout/start', 'POST', { all: true, batch_size: bs, build: true });
      currentRolloutJobId = r.job_id;
      rolloutFinishedLoggedFor = null;
      renderRollout(r.job ? {
        id: r.job.id,
        state: r.job.state,
        targets_total: (r.job.targets||[]).length,
        pending: (r.job.pending||[]).length,
        in_flight: Object.keys(r.job.in_flight||{}).length,
        done: Object.keys(r.job.done||{}).length,
        failed: Object.keys(r.job.failed||{}).length,
      } : null);
      log(`Rollout started: ${currentRolloutJobId}`);
      await rolloutStatusTick();
    } catch (e) {
      log(`Rollout start failed: ${e.message}`);
    }
  });

  $(\"refresh\").addEventListener('click', refresh);

  $(\"updateAll\").addEventListener('click', async () => {
    try {
      log('Update All...');
      const r = await api('/ui/update', 'POST', { all: true, command_id: `ota-all-${Date.now()}` });
      log(`Update All result: ${JSON.stringify(r)}`);
      await refresh();
    } catch (e) {
      log(`Update All failed: ${e.message}`);
    }
  });

  $(\"buildUpdateAll\").addEventListener('click', async () => {
    try {
      log('Build+Update All...');
      const r = await api('/ui/build-and-update', 'POST', { all: true, command_id: `ota-build-all-${Date.now()}` });
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