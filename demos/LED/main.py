import time
import json
import machine
import network
import neopixel
from simple import MQTTClient

# =========================
# USER CONFIG
# =========================

DEVICE_NAME = "led-audio-demo"
MQTT_BROKER = "192.168.50.1"   # your hub IP
MQTT_PORT = 1883
MQTT_USER = "hubadmin"
MQTT_PASS = "devpass123"

MQTT_TOPIC = b"site/demo/device/b17c894acb294400ae81bac3c43882aa/audio/level"
MQTT_DEBUG = True
MQTT_PING_INTERVAL_MS = 10000

# Static IP for the Pico
USE_DHCP = False
STATIC_IP = ("192.168.50.21", "255.255.255.0", "0.0.0.0", "0.0.0.0")

# W55RP20-EVB-Pico Ethernet pins
ETH_SPI_ID = 0
ETH_SCK_PIN = 21
ETH_MOSI_PIN = 23
ETH_MISO_PIN = 22
ETH_CS_PIN = 20
ETH_RST_PIN = 25

# WS2812 settings
LED_PIN = 15              # change if needed
LED_COUNT = 30
LED_BRIGHTNESS = 0.75     # overall cap, 0.0 to 1.0

# LED color for audio pulse
BASE_COLOR = (253, 10, 0)   
IDLE_COLOR = (0, 0, 0)

# Audio / sync tuning
NOISE_FLOOR = 0.02
AUTO_OFF_MS = 120
LEVEL_SMOOTHING = 0.35
FADE_UP_RATE = 0.35
FADE_DOWN_RATE = 0.18
MIN_FLASH_BRIGHTNESS = 0.08

# Adaptive peak logic, similar to mouth servo version
PEAK_MIN = 0.04
PEAK_DECAY = 0.90
TRIGGER_FRAC = 0.54
RISE_DELTA = 0.008
FLASH_HOLD_MS = 90
FLASH_REFRACTORY_MS = 100

# =========================
# GLOBAL STATE
# =========================

latest_level = 0.0
flash_active = False
flash_until_ms = 0
last_flash_ms = 0
rolling_peak = PEAK_MIN
prev_level = 0.0
last_msg_ms = time.ticks_ms()
client = None
last_ping_ms = time.ticks_ms()
display_level = 0.0

# =========================
# LED HELPERS
# =========================

np = neopixel.NeoPixel(machine.Pin(LED_PIN), LED_COUNT)

def scale_color(color, brightness):
    brightness = max(0.0, min(1.0, brightness))
    return (
        int(color[0] * brightness),
        int(color[1] * brightness),
        int(color[2] * brightness),
    )

def set_all(color):
    for i in range(LED_COUNT):
        np[i] = color
    np.write()

def set_all_scaled(color, level):
    level = max(0.0, min(1.0, level))
    scaled = scale_color(color, LED_BRIGHTNESS * level)
    for i in range(LED_COUNT):
        np[i] = scaled
    np.write()


def smooth_display_level(current_level, target_level, active):
    if active and target_level > 0.0:
        target = MIN_FLASH_BRIGHTNESS + ((1.0 - MIN_FLASH_BRIGHTNESS) * target_level)
        if target > current_level:
            return current_level + ((target - current_level) * FADE_UP_RATE)
        return current_level + ((target - current_level) * FADE_DOWN_RATE)

    return current_level * (1.0 - FADE_DOWN_RATE)

# =========================
# MQTT CALLBACK
# =========================

def on_message(topic, msg):
    global latest_level, last_msg_ms

    try:
        if MQTT_DEBUG:
            print("MQTT RX topic=", topic)
            print("MQTT RX msg=", msg)

        payload = json.loads(msg)
        level = float(payload.get("level", 0.0))
        playing = bool(payload.get("playing", False))

        if not playing:
            level = 0.0

        if level < NOISE_FLOOR:
            level = 0.0

        level = max(0.0, min(1.0, level))
        latest_level = level
        last_msg_ms = time.ticks_ms()

    except Exception as e:
        print("MQTT parse error:", e)

# =========================
# NETWORK
# =========================

def connect_ethernet():
    print("Initializing Ethernet...")

    spi = machine.WIZNET_PIO_SPI(
        31250000,
        sck=ETH_SCK_PIN,
        mosi=ETH_MOSI_PIN,
        miso=ETH_MISO_PIN,
    )
    cs = machine.Pin(ETH_CS_PIN)
    rst = machine.Pin(ETH_RST_PIN, machine.Pin.OUT)

    rst.value(0)
    time.sleep_ms(100)
    rst.value(1)
    time.sleep_ms(500)

    print("Creating WIZNET5K interface...")
    nic = network.WIZNET5K(spi, cs, rst)

    try:
        nic.active(True)
    except Exception:
        pass

    if USE_DHCP:
        print("Using DHCP...")
        try:
            nic.ifconfig("dhcp")
        except Exception:
            pass

        t0 = time.ticks_ms()
        while nic.ifconfig()[0] == "0.0.0.0":
            if time.ticks_diff(time.ticks_ms(), t0) > 15000:
                raise RuntimeError("DHCP timeout")
            time.sleep_ms(250)
    else:
        print("Using static IP:", STATIC_IP[0])
        nic.ifconfig(STATIC_IP)

    print("Ethernet up:", nic.ifconfig())
    return nic

def connect_mqtt():
    global client

    client = MQTTClient(
        client_id=DEVICE_NAME,
        server=MQTT_BROKER,
        port=MQTT_PORT,
        user=MQTT_USER,
        password=MQTT_PASS,
        keepalive=120,
    )
    client.set_callback(on_message)
    client.connect()
    client.subscribe(MQTT_TOPIC)
    print("MQTT connected, subscribed to:", MQTT_TOPIC)

# =========================
# LED AUDIO SYNC
# =========================

def update_flash_state(level, age_ms, now_ms):
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
    refractory_done = time.ticks_diff(now_ms, last_flash_ms) >= FLASH_REFRACTORY_MS

    if level >= trigger_level and rising_enough and refractory_done:
        flash_active = True
        flash_until_ms = time.ticks_add(now_ms, FLASH_HOLD_MS)
        last_flash_ms = now_ms

    if flash_active and time.ticks_diff(flash_until_ms, now_ms) <= 0:
        flash_active = False

    prev_level = level
    return level, flash_active

# =========================
# MAIN
# =========================

def main():
    global last_ping_ms, display_level

    set_all(IDLE_COLOR)

    nic = connect_ethernet()
    connect_mqtt()

    print("LED audio sync ready")

    while True:
        try:
            client.check_msg()

            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, last_ping_ms) >= MQTT_PING_INTERVAL_MS:
                try:
                    client.ping()
                    if MQTT_DEBUG:
                        print("MQTT ping")
                except Exception as e:
                    print("MQTT ping failed:", e)
                    raise
                last_ping_ms = now_ms

        except Exception as e:
            print("MQTT error, reconnecting:", e)
            set_all(IDLE_COLOR)
            time.sleep_ms(1000)
            try:
                connect_mqtt()
                last_ping_ms = time.ticks_ms()
            except Exception as e2:
                print("Reconnect failed:", e2)
                time.sleep_ms(2000)
                continue

        now_ms = time.ticks_ms()
        age = time.ticks_diff(now_ms, last_msg_ms)
        level, active = update_flash_state(latest_level, age, now_ms)

        smoothed_level = display_level + ((level - display_level) * LEVEL_SMOOTHING)
        display_level = smooth_display_level(display_level, smoothed_level, active)

        if display_level < 0.01:
            display_level = 0.0
            set_all(IDLE_COLOR)
        else:
            set_all_scaled(BASE_COLOR, display_level)

        time.sleep_ms(20)

main()