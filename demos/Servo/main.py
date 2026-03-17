import time
import math
import json
import machine
import network
from simple import MQTTClient

# =========================
# USER CONFIG
# =========================

DEVICE_NAME = "servo-mouth-demo"
MQTT_BROKER = "192.168.50.1"   # your hub IP
MQTT_PORT = 1883
MQTT_USER = "hubadmin"
MQTT_PASS = "devpass123"

# Subscribe broadly for demo convenience
MQTT_TOPIC = b"site/demo/device/b17c894acb294400ae81bac3c43882aa/audio/level"
MQTT_DEBUG = True
MQTT_PING_INTERVAL_MS = 10000

# Static IP for the Pico
USE_DHCP = False
STATIC_IP = ("192.168.50.20", "255.255.255.0", "0.0.0.0", "0.0.0.0")

# W55RP20-EVB-Pico Ethernet pins (from wiznet_init defaults)

ETH_SPI_ID = 0
ETH_SCK_PIN = 21
ETH_MOSI_PIN = 23
ETH_MISO_PIN = 22
ETH_CS_PIN = 20
ETH_RST_PIN = 25

# PCA9685 / servo settings
I2C_ID = 0
I2C_SDA_PIN = 4
I2C_SCL_PIN = 5
PCA9685_ADDR = 0x40
SERVO_CHANNEL = 0

# Mouth tuning
SERVO_CLOSED_DEG = 77
SERVO_OPEN_DEG = 52
SERVO_IDLE_DEG = 77

NOISE_FLOOR = 0.02          # ignore tiny values
AUTO_CLOSE_MS = 120         # force closed if messages go stale

# Adaptive peak-triggered mouth flapping
PEAK_MIN = 0.04             # minimum peak reference so very soft voices can still trigger
PEAK_DECAY = 0.90           # rolling peak decay per loop (lower = adapts faster)
FLAP_TRIGGER_FRAC = 0.54    # trigger when level exceeds this fraction of rolling peak
FLAP_RISE_DELTA = 0.008     # require some rise vs previous sample to avoid staying open on sustained tones
FLAP_OPEN_MS = 115           # how long each flap stays open
FLAP_REFRACTORY_MS = 130     # minimum time between flap triggers


# =========================
# PCA9685 DRIVER
# =========================

class PCA9685:
    def __init__(self, i2c, address=0x40):
        self.i2c = i2c
        self.address = address
        self.reset()
        self.set_pwm_freq(50)

    def _write(self, reg, value):
        self.i2c.writeto_mem(self.address, reg, bytes([value]))

    def _read(self, reg):
        return self.i2c.readfrom_mem(self.address, reg, 1)[0]

    def reset(self):
        self._write(0x00, 0x00)

    def set_pwm_freq(self, freq_hz):
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
        time.sleep_ms(5)
        self._write(0x00, oldmode | 0xA1)

    def set_pwm(self, channel, on, off):
        reg = 0x06 + 4 * channel
        data = bytes((
            on & 0xFF,
            (on >> 8) & 0xFF,
            off & 0xFF,
            (off >> 8) & 0xFF,
        ))
        self.i2c.writeto_mem(self.address, reg, data)


class Servo:
    def __init__(self, pca, channel, min_us=500, max_us=2500, freq=50):
        self.pca = pca
        self.channel = channel
        self.min_us = min_us
        self.max_us = max_us
        self.freq = freq
        self.period_us = int(1000000 / freq)

    def write_deg(self, deg):
        deg = max(0, min(180, deg))
        pulse_us = int(self.min_us + (self.max_us - self.min_us) * (deg / 180.0))
        off = int((pulse_us / self.period_us) * 4096)
        off = max(0, min(4095, off))
        self.pca.set_pwm(self.channel, 0, off)


# =========================
# GLOBAL STATE
# =========================

latest_level = 0.0
mouth_is_open = False
mouth_open_until_ms = 0
last_flap_ms = 0
rolling_peak = PEAK_MIN
prev_level = 0.0
last_msg_ms = time.ticks_ms()
client = None
last_ping_ms = time.ticks_ms()


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

    # Hardware reset of the WIZnet chip
    rst.value(0)
    time.sleep_ms(100)
    rst.value(1)
    time.sleep_ms(500)

    print("Creating WIZNET5K interface...")
    nic = network.WIZNET5K(spi, cs, rst)

    try:
        nic.active(True)
    except Exception:
        # Some firmware builds auto-activate
        pass

    if USE_DHCP:
        print("Using DHCP...")
        try:
            nic.ifconfig('dhcp')
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

    cid = DEVICE_NAME
    client = MQTTClient(
        client_id=cid,
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
# MOUTH CONTROL
# =========================

def update_mouth_state(level, age_ms, now_ms):
    global mouth_is_open, mouth_open_until_ms, last_flap_ms, rolling_peak, prev_level

    # If messages have gone stale, force closed and reset state.
    if age_ms >= AUTO_CLOSE_MS:
        mouth_is_open = False
        mouth_open_until_ms = 0
        last_flap_ms = 0
        rolling_peak = PEAK_MIN
        prev_level = 0.0
        return SERVO_CLOSED_DEG

    # Track a rolling peak so soft and loud voices normalize similarly.
    if level > rolling_peak:
        rolling_peak = level
    else:
        rolling_peak = max(PEAK_MIN, rolling_peak * PEAK_DECAY)

    trigger_level = rolling_peak * FLAP_TRIGGER_FRAC
    rising_enough = (level - prev_level) >= FLAP_RISE_DELTA
    refractory_done = time.ticks_diff(now_ms, last_flap_ms) >= FLAP_REFRACTORY_MS

    # Trigger a new flap only on a rising peak, not sustained loudness.
    if level >= trigger_level and rising_enough and refractory_done:
        mouth_is_open = True
        mouth_open_until_ms = time.ticks_add(now_ms, FLAP_OPEN_MS)
        last_flap_ms = now_ms

    # Close when the open window expires.
    if mouth_is_open and time.ticks_diff(mouth_open_until_ms, now_ms) <= 0:
        mouth_is_open = False

    prev_level = level
    return SERVO_OPEN_DEG if mouth_is_open else SERVO_CLOSED_DEG


def main():
    global latest_level, last_ping_ms

    nic = connect_ethernet()
    connect_mqtt()

    i2c = machine.I2C(
        I2C_ID,
        sda=machine.Pin(I2C_SDA_PIN),
        scl=machine.Pin(I2C_SCL_PIN),
        freq=400000,
    )

    pca = PCA9685(i2c, PCA9685_ADDR)
    servo = Servo(pca, SERVO_CHANNEL)
    servo.write_deg(SERVO_IDLE_DEG)
    time.sleep_ms(500)

    print("Servo mouth ready")

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
        deg = update_mouth_state(latest_level, age, now_ms)
        servo.write_deg(deg)

        time.sleep_ms(20)


main()