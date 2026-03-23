import json
import math
import signal
import sys
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt
from smbus2 import SMBus

# =========================
# USER CONFIG
# =========================

DEVICE_NAME = "bird-servo-pi4"
MQTT_BROKER = "192.168.50.1"
MQTT_PORT = 1883
MQTT_USER = "hubadmin"
MQTT_PASS = "devpass123"
MQTT_TOPIC = "site/expo/device/7b4520e5f39d4b20bfcee61e8ea8f04b/audio/level"
MQTT_DEBUG = False
MQTT_KEEPALIVE = 120

# Linux I2C bus on Raspberry Pi
I2C_BUS_NUM = 1
PCA9685_ADDR = 0x40
SERVO_CHANNEL = 0

# Mouth tuning
SERVO_CLOSED_DEG = 77
SERVO_OPEN_DEG = 52
SERVO_IDLE_DEG = 77

NOISE_FLOOR = 0.02
AUTO_CLOSE_MS = 120

# Adaptive peak-triggered mouth flapping
PEAK_MIN = 0.04
PEAK_DECAY = 0.90
FLAP_TRIGGER_FRAC = 0.54
FLAP_RISE_DELTA = 0.008
FLAP_OPEN_MS = 115
FLAP_REFRACTORY_MS = 130

LOOP_SLEEP_SEC = 0.02
STALE_PACKET_SEC = 2.0

# =========================
# PCA9685 DRIVER
# =========================


class PCA9685:
    def __init__(self, bus: SMBus, address: int = 0x40) -> None:
        self.bus = bus
        self.address = address
        self.reset()
        self.set_pwm_freq(50)

    def _write(self, reg: int, value: int) -> None:
        self.bus.write_byte_data(self.address, reg, value & 0xFF)

    def _read(self, reg: int) -> int:
        return self.bus.read_byte_data(self.address, reg)

    def reset(self) -> None:
        self._write(0x00, 0x00)

    def set_pwm_freq(self, freq_hz: float) -> None:
        prescaleval = 25000000.0
        prescaleval /= 4096.0
        prescaleval /= float(freq_hz)
        prescaleval -= 1.0
        prescale = int(math.floor(prescaleval + 0.5))

        oldmode = self._read(0x00)
        sleep = (oldmode & 0x7F) | 0x10
        self._write(0x00, sleep)
        self._write(0xFE, prescale)
        self._write(0x00, oldmode)
        time.sleep(0.005)
        self._write(0x00, oldmode | 0xA1)

    def set_pwm(self, channel: int, on: int, off: int) -> None:
        reg = 0x06 + 4 * channel
        self.bus.write_i2c_block_data(
            self.address,
            reg,
            [on & 0xFF, (on >> 8) & 0xFF, off & 0xFF, (off >> 8) & 0xFF],
        )


class Servo:
    def __init__(self, pca: PCA9685, channel: int, min_us: int = 500, max_us: int = 2500, freq: int = 50) -> None:
        self.pca = pca
        self.channel = channel
        self.min_us = min_us
        self.max_us = max_us
        self.freq = freq
        self.period_us = int(1_000_000 / freq)

    def write_deg(self, deg: float) -> None:
        deg = max(0.0, min(180.0, float(deg)))
        pulse_us = int(self.min_us + (self.max_us - self.min_us) * (deg / 180.0))
        off = int((pulse_us / self.period_us) * 4096)
        off = max(0, min(4095, off))
        self.pca.set_pwm(self.channel, 0, off)


# =========================
# GLOBAL STATE
# =========================

latest_level = 0.0
latest_payload_ts = 0.0
last_msg_monotonic = time.monotonic()
mouth_is_open = False
mouth_open_until_ms = 0
last_flap_ms = 0
rolling_peak = PEAK_MIN
prev_level = 0.0
running = True
state_lock = threading.Lock()


# =========================
# MQTT CALLBACKS
# =========================


def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, rc: int, _properties: Any = None) -> None:
    if rc == 0:
        client.subscribe(MQTT_TOPIC, qos=0)
        print(f"MQTT connected, subscribed to: {MQTT_TOPIC}")
    else:
        print(f"MQTT connect failed rc={rc}")


def on_message(_client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
    global latest_level, latest_payload_ts, last_msg_monotonic

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        msg_ts = float(payload.get("ts", 0.0) or 0.0)
        level = float(payload.get("level", 0.0) or 0.0)
        playing = bool(payload.get("playing", False))

        if msg_ts and msg_ts + STALE_PACKET_SEC < time.time():
            return

        with state_lock:
            if msg_ts < latest_payload_ts:
                return
            latest_payload_ts = msg_ts

            if not playing:
                level = 0.0
            if level < NOISE_FLOOR:
                level = 0.0

            latest_level = max(0.0, min(1.0, level))
            last_msg_monotonic = time.monotonic()

        if MQTT_DEBUG:
            print("MQTT RX topic=", msg.topic)
            print("MQTT RX msg=", payload)

    except Exception as exc:
        print("MQTT parse error:", exc)


# =========================
# MOUTH CONTROL
# =========================


def ticks_ms() -> int:
    return int(time.monotonic() * 1000)


def update_mouth_state(level: float, age_ms: int, now_ms: int) -> float:
    global mouth_is_open, mouth_open_until_ms, last_flap_ms, rolling_peak, prev_level

    if age_ms >= AUTO_CLOSE_MS:
        mouth_is_open = False
        mouth_open_until_ms = 0
        last_flap_ms = 0
        rolling_peak = PEAK_MIN
        prev_level = 0.0
        return SERVO_CLOSED_DEG

    if level > rolling_peak:
        rolling_peak = level
    else:
        rolling_peak = max(PEAK_MIN, rolling_peak * PEAK_DECAY)

    trigger_level = rolling_peak * FLAP_TRIGGER_FRAC
    rising_enough = (level - prev_level) >= FLAP_RISE_DELTA
    refractory_done = (now_ms - last_flap_ms) >= FLAP_REFRACTORY_MS

    if level >= trigger_level and rising_enough and refractory_done:
        mouth_is_open = True
        mouth_open_until_ms = now_ms + FLAP_OPEN_MS
        last_flap_ms = now_ms

    if mouth_is_open and now_ms >= mouth_open_until_ms:
        mouth_is_open = False

    prev_level = level
    return SERVO_OPEN_DEG if mouth_is_open else SERVO_CLOSED_DEG


# =========================
# MAIN
# =========================


def handle_shutdown(_signum: int, _frame: Any) -> None:
    global running
    running = False


def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(client_id=DEVICE_NAME, protocol=mqtt.MQTTv311)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
    client.loop_start()
    return client


def main() -> int:
    global running

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    bus = SMBus(I2C_BUS_NUM)
    pca = PCA9685(bus, PCA9685_ADDR)
    servo = Servo(pca, SERVO_CHANNEL)
    servo.write_deg(SERVO_IDLE_DEG)
    time.sleep(0.5)

    client = build_mqtt_client()
    print("Servo mouth ready")

    try:
        while running:
            with state_lock:
                level = latest_level
                age_ms = int((time.monotonic() - last_msg_monotonic) * 1000)

            now_ms = ticks_ms()
            deg = update_mouth_state(level, age_ms, now_ms)
            servo.write_deg(deg)
            time.sleep(LOOP_SLEEP_SEC)
    finally:
        try:
            servo.write_deg(SERVO_CLOSED_DEG)
        except Exception:
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        try:
            bus.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())