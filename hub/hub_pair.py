from flask import Flask, request, jsonify, send_file, abort
import os
import subprocess
import secrets
import string
import json
from datetime import datetime, timezone
from pathlib import Path

app = Flask(__name__)

PAIR_CODE = "483921"  # you will type this into the device setup page

REGISTRY_DIR = Path("/var/lib/hint")
REGISTRY_PATH = REGISTRY_DIR / "devices.json"
OTA_DIR = Path("/opt/hint/ota")

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

if __name__ == "__main__":
    # 0.0.0.0 so the device can reach it on the LAN
    app.run(host="0.0.0.0", port=5000)