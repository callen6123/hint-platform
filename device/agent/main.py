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
from pathlib import Path

import paho.mqtt.client as mqtt

PROVISION_PATH = Path("/etc/hint/provision.json")

AUDIO_PATH = Path("/opt/hint/audio/demo.wav")

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
    try:
        txt = Path("/proc/asound/cards").read_text()
    except Exception:
        return None

    for line in txt.splitlines():
        # lines with: 'N [CARDID ]: ... - ...'
        if "[" in line and "]" in line and ":" in line:
            # crude but effective
            if "USB" in line or "usb" in line:
                # extract CARDID between [...]
                cardid = line.split("[", 1)[1].split("]", 1)[0].strip()
                if cardid:
                    return cardid
    return None


def play() -> None:
    if not AUDIO_PATH.exists():
        print(f"[AUDIO] missing file: {AUDIO_PATH}")
        return

    card = _find_usb_card()
    if card:
        cmd = ["aplay", "-D", f"hw:{card},0", str(AUDIO_PATH)]
    else:
        cmd = ["aplay", str(AUDIO_PATH)]

    # Retry a few times to handle USB “not ready” moments
    for attempt in range(1, 6):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"[AUDIO] played {AUDIO_PATH} via {cmd}")
            return

        err = (res.stderr or "").strip()
        out = (res.stdout or "").strip()
        print(f"[AUDIO] attempt {attempt}/5 failed rc={res.returncode} cmd={cmd}")
        if out:
            print(f"[AUDIO] stdout: {out}")
        if err:
            print(f"[AUDIO] stderr: {err}")

        time.sleep(0.5)

    print("[AUDIO] giving up after retries")


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


def main() -> None:
    prov = load_provision()
    topics = prov["topics"]
    cmd_t = topics["cmd"]
    ack_t = topics["ack"]
    state_t = topics["state"]

    device_id = prov.get("device_id", socket.gethostname())

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
        c.publish(ack_t, json.dumps(payload), qos=1, retain=False)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # Subscribe after connect so reconnects recover automatically
        client.subscribe(cmd_t, qos=1)
        # Mark online
        online_payload = {"device_id": device_id, "status": "ONLINE", "ts": int(time.time())}
        online_payload.update(current_versions())
        client.publish(state_t, json.dumps(online_payload), qos=1, retain=True)

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
                res = do_ota_update(prov)
                # completed with details, so hub can track progress
                publish_ack(str(cid), "completed", res)

                # If we actually applied a new version, exit so systemd restarts on the new code.
                if res.get("result") == "updated":
                    time.sleep(0.5)
                    os._exit(0)

            elif cmd == "play":
                play()
                publish_ack(str(cid), "completed")

            else:
                # Back-compat: treat unknown command as play test
                play()
                publish_ack(str(cid), "completed")

        except Exception as e:
            publish_ack(str(cid), "error", {"error": str(e)})

    c.on_connect = on_connect
    c.on_message = on_message

    # Auto-reconnect behavior
    c.reconnect_delay_set(min_delay=1, max_delay=30)

    c.connect(host, port, keepalive=30)
    c.loop_start()

    # Heartbeat: keep a retained status so the hub can see liveness
    while True:
        idle_payload = {"device_id": device_id, "status": "IDLE", "ts": int(time.time())}
        idle_payload.update(current_versions())
        c.publish(state_t, json.dumps(idle_payload), qos=1, retain=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
