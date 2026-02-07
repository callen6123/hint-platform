import json
import time
import subprocess
import socket
from pathlib import Path

import paho.mqtt.client as mqtt

PROVISION_PATH = Path("/etc/hint/provision.json")
AUDIO_PATH = Path("/opt/hint/audio/demo.wav")


def load_provision() -> dict:
    with PROVISION_PATH.open("r") as f:
        return json.load(f)


def play() -> None:
    if not AUDIO_PATH.exists():
        return
    subprocess.call(["aplay", str(AUDIO_PATH)])


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
    c.will_set(state_t, json.dumps({"device_id": device_id, "status": "OFFLINE", "ts": int(time.time())}), qos=1, retain=True)

    if username:
        c.username_pw_set(username, password)

    def publish_ack(command_id: str, status: str) -> None:
        payload = {
            "device_id": device_id,
            "command_id": command_id,
            "status": status,
            "ts": int(time.time()),
        }
        c.publish(ack_t, json.dumps(payload), qos=1, retain=False)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # Subscribe after connect so reconnects recover automatically
        client.subscribe(cmd_t, qos=1)
        # Mark online
        client.publish(state_t, json.dumps({"device_id": device_id, "status": "ONLINE", "ts": int(time.time())}), qos=1, retain=True)

    def on_message(client, userdata, msg):
        # Payload may be bytes; decode safely
        raw = msg.payload.decode("utf-8", errors="replace")
        try:
            d = json.loads(raw)
        except Exception:
            # If the message isn't JSON, ignore but don't crash
            return

        cid = d.get("command_id")
        if not cid:
            return

        publish_ack(str(cid), "accepted")
        play()
        publish_ack(str(cid), "completed")

    c.on_connect = on_connect
    c.on_message = on_message

    # Auto-reconnect behavior
    c.reconnect_delay_set(min_delay=1, max_delay=30)

    c.connect(host, port, keepalive=30)
    c.loop_start()

    # Heartbeat: keep a retained status so the hub can see liveness
    while True:
        c.publish(state_t, json.dumps({"device_id": device_id, "status": "IDLE", "ts": int(time.time())}), qos=1, retain=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
