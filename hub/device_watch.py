#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

REGISTRY_PATH = Path("/var/lib/hint/devices.json")

HUB_MQTT_HOST = os.environ.get("HUB_MQTT_HOST", "localhost")
HUB_MQTT_PORT = int(os.environ.get("HUB_MQTT_PORT", "1883"))
HUB_MQTT_USER = os.environ.get("HUB_MQTT_USER", "hubadmin")
HUB_MQTT_PASS = os.environ.get("HUB_MQTT_PASS", "devpass123")
SITE_ID = os.environ.get("HUB_SITE_ID", "demo")

STATE_TOPIC = f"site/{SITE_ID}/device/+/state"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_registry(reg: dict) -> None:
    # Atomic write
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def topic_device_id(topic: str) -> str | None:
    # topic: site/<site>/device/<device_id>/state
    parts = topic.split("/")
    if len(parts) >= 5 and parts[0] == "site" and parts[2] == "device":
        return parts[3]
    return None


def on_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe(STATE_TOPIC, qos=1)


def on_message(client, userdata, msg):
    dev_id = topic_device_id(msg.topic)
    if not dev_id:
        return

    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
    except Exception:
        payload = {}

    raw_status = str(payload.get("status", "")).upper()
    status = "offline" if raw_status == "OFFLINE" else "online"

    reg = read_registry()
    rec = reg.get(dev_id) if isinstance(reg, dict) else None
    if not isinstance(rec, dict):
        rec = {"device_id": dev_id, "site_id": SITE_ID}

    rec["last_seen"] = utc_now_iso()
    rec["status"] = status

    # Preserve paired_at/mqtt_user if already present
    reg[dev_id] = rec
    write_registry(reg)


def main():
    while True:
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hint-device-watch", protocol=mqtt.MQTTv311)
            c.username_pw_set(HUB_MQTT_USER, HUB_MQTT_PASS)
            c.on_connect = on_connect
            c.on_message = on_message
            c.connect(HUB_MQTT_HOST, HUB_MQTT_PORT, keepalive=30)
            c.loop_forever(retry_first_connection=True)
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    main()