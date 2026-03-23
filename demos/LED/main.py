import json
import math
import signal
import sys
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt
from rpi_ws281x import Color, PixelStrip

# =========================
# USER CONFIG
# =========================

DEVICE_NAME = "hal-led-pi4"
MQTT_BROKER = "192.168.50.1"
MQTT_PORT = 1883
MQTT_USER = "hubadmin"
MQTT_PASS = "devpass123"
MQTT_TOPIC = "site/expo/device/8dceb90297ef4884ba7d01207d14d3e9/audio/level"
MQTT_DEBUG = False
MQTT_KEEPALIVE = 120

# WS2812 settings (BCM GPIO numbering)
INNER_LED_PIN = 18              # reactive inner ring
INNER_LED_COUNT = 12
INNER_LED_BRIGHTNESS = 255      # 0-255

OUTER_LED_PIN = 13              # pulsing outer ring
OUTER_LED_COUNT = 10
OUTER_LED_BRIGHTNESS = 200       # 0-255

# LED color for audio pulse
INNER_BASE_COLOR = (255, 100, 0)   # bright orange-yellow center glow
OUTER_BASE_COLOR = (255, 0, 0)      # pure red outer ring
IDLE_COLOR = (0, 0, 0)

# Audio / sync tuning
NOISE_FLOOR = 0.02
AUTO_OFF_MS = 120
LEVEL_SMOOTHING = 0.20
FADE_UP_RATE = 0.25
FADE_DOWN_RATE = 0.18
MIN_FLASH_BRIGHTNESS = 0.08
OUTER_PULSE_MIN = 0.10
OUTER_PULSE_MAX = 0.75
OUTER_PULSE_SPEED = 0.04

# Adaptive peak logic
PEAK_MIN = 0.04
PEAK_DECAY = 0.90
TRIGGER_FRAC = 0.54
RISE_DELTA = 0.012
FLASH_HOLD_MS = 120
FLASH_REFRACTORY_MS = 120

LOOP_SLEEP_SEC = 0.02
STALE_PACKET_SEC = 2.0

# WS281x config
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_INVERT = False
INNER_LED_CHANNEL = 0
OUTER_LED_CHANNEL = 1

# =========================
# GLOBAL STATE
# =========================

latest_level = 0.0
latest_payload_ts = 0.0
last_msg_monotonic = time.monotonic()
flash_active = False
flash_until_ms = 0
last_flash_ms = 0
rolling_peak = PEAK_MIN
prev_level = 0.0
running = True
state_lock = threading.Lock()
display_level = 0.0
outer_phase = 0.0


# =========================
# LED HELPERS
# =========================


def ticks_ms() -> int:
    return int(time.monotonic() * 1000)



def scale_color(color: tuple[int, int, int], brightness: float) -> tuple[int, int, int]:
    brightness = max(0.0, min(1.0, brightness))
    return (
        int(color[0] * brightness),
        int(color[1] * brightness),
        int(color[2] * brightness),
    )



def rgb_to_color(rgb: tuple[int, int, int]) -> int:
    return Color(rgb[0], rgb[1], rgb[2])



def set_all(strip: PixelStrip, color: tuple[int, int, int]) -> None:
    packed = rgb_to_color(color)
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, packed)
    strip.show()



def set_all_scaled(strip: PixelStrip, color: tuple[int, int, int], brightness_cap: float, level: float) -> None:
    level = max(0.0, min(1.0, level))
    scaled = scale_color(color, brightness_cap * level)
    packed = rgb_to_color(scaled)
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, packed)
    strip.show()



def smooth_display_level(current_level: float, target_level: float, active: bool) -> float:
    if active and target_level > 0.0:
        target = MIN_FLASH_BRIGHTNESS + ((1.0 - MIN_FLASH_BRIGHTNESS) * target_level)
        if target > current_level:
            return current_level + ((target - current_level) * FADE_UP_RATE)
        return current_level + ((target - current_level) * FADE_DOWN_RATE)

    return current_level * (1.0 - FADE_DOWN_RATE)



def update_outer_pulse() -> float:
    global outer_phase

    outer_phase += OUTER_PULSE_SPEED
    if outer_phase > math.tau:
        outer_phase -= math.tau

    sine_val = (math.sin(outer_phase) + 1.0) / 2.0
    return OUTER_PULSE_MIN + (sine_val * (OUTER_PULSE_MAX - OUTER_PULSE_MIN))


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

        if MQTT_DEBUG:
            print("MQTT RAW topic=", msg.topic)
            print("MQTT RAW level=", level, "playing=", playing, "ts=", msg_ts, "now=", time.time())

        # Temporarily disable stale-packet dropping while debugging.
        # if msg_ts and msg_ts + STALE_PACKET_SEC < time.time():
        #     return

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
            print("MQTT RX level=", latest_level, "playing=", playing, "ts=", msg_ts)

    except Exception as exc:
        print("MQTT parse error:", exc)


# =========================
# LED AUDIO SYNC
# =========================


def update_flash_state(level: float, age_ms: int, now_ms: int) -> tuple[float, bool]:
    global flash_active, flash_until_ms, last_flash_ms, rolling_peak, prev_level, display_level

    if age_ms >= AUTO_OFF_MS:
        flash_active = False
        flash_until_ms = 0
        last_flash_ms = 0
        rolling_peak = PEAK_MIN
        prev_level = 0.0
        display_level = 0.0
        return 0.0, False

    if level > rolling_peak:
        rolling_peak = level
    else:
        rolling_peak = max(PEAK_MIN, rolling_peak * PEAK_DECAY)

    trigger_level = rolling_peak * TRIGGER_FRAC
    rising_enough = (level - prev_level) >= RISE_DELTA
    refractory_done = (now_ms - last_flash_ms) >= FLASH_REFRACTORY_MS

    if level >= trigger_level and rising_enough and refractory_done:
        flash_active = True
        flash_until_ms = now_ms + FLASH_HOLD_MS
        last_flash_ms = now_ms

    if flash_active and now_ms >= flash_until_ms:
        flash_active = False

    prev_level = level
    return level, flash_active


# =========================
# MAIN
# =========================


def handle_shutdown(_signum: int, _frame: Any) -> None:
    global running
    running = False



def build_strip(pin: int, count: int, brightness: int, channel: int) -> PixelStrip:
    strip = PixelStrip(
        count,
        pin,
        LED_FREQ_HZ,
        LED_DMA,
        LED_INVERT,
        brightness,
        channel,
    )
    strip.begin()
    return strip



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
    global running, display_level

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    inner_strip = build_strip(INNER_LED_PIN, INNER_LED_COUNT, INNER_LED_BRIGHTNESS, INNER_LED_CHANNEL)
    outer_strip = build_strip(OUTER_LED_PIN, OUTER_LED_COUNT, OUTER_LED_BRIGHTNESS, OUTER_LED_CHANNEL)

    set_all(inner_strip, IDLE_COLOR)
    set_all(outer_strip, IDLE_COLOR)

    client = build_mqtt_client()
    print("LED audio sync ready")

    try:
        while running:
            with state_lock:
                level = latest_level
                age_ms = int((time.monotonic() - last_msg_monotonic) * 1000)

            now_ms = ticks_ms()
            level, active = update_flash_state(level, age_ms, now_ms)

            smoothed_level = display_level + ((level - display_level) * LEVEL_SMOOTHING)
            display_level = smooth_display_level(display_level, smoothed_level, active)

            if MQTT_DEBUG and active:
                print("SYNC active level=", level, "display_level=", display_level, "age_ms=", age_ms)

            outer_level = update_outer_pulse()
            set_all_scaled(outer_strip, OUTER_BASE_COLOR, 1.0, outer_level)

            if display_level < 0.01:
                display_level = 0.0
                set_all(inner_strip, IDLE_COLOR)
            else:
                set_all_scaled(inner_strip, INNER_BASE_COLOR, 1.0, display_level)

            time.sleep(LOOP_SLEEP_SEC)
    finally:
        try:
            set_all(inner_strip, IDLE_COLOR)
            set_all(outer_strip, IDLE_COLOR)
        except Exception:
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())