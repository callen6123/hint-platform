import machine, time, math

# I2C setup (matches your main.py)
i2c = machine.I2C(0, sda=machine.Pin(4), scl=machine.Pin(5), freq=400000)

PCA9685_ADDR = 0x40

# Minimal PCA9685 helpers
def write(reg, val):
    i2c.writeto_mem(PCA9685_ADDR, reg, bytes([val]))

def read(reg):
    return i2c.readfrom_mem(PCA9685_ADDR, reg, 1)[0]

# reset
write(0x00, 0x00)

# set 50hz for servos
prescale = int(25000000/(4096*50)-1)
oldmode = read(0x00)
write(0x00, (oldmode & 0x7F) | 0x10)
write(0xFE, prescale)
write(0x00, oldmode)
time.sleep_ms(5)
write(0x00, oldmode | 0xA1)

def set_servo_deg(channel, deg):
    min_us = 500
    max_us = 2500
    period_us = 20000

    pulse = min_us + (max_us-min_us)*(deg/180)
    off = int((pulse/period_us)*4096)

    reg = 0x06 + 4*channel
    data = bytes((0,0, off & 0xFF, off>>8))
    i2c.writeto_mem(PCA9685_ADDR, reg, data)

print("Starting narrow servo sweep")

while True:
    for d in range(47,77):
        set_servo_deg(0,d)
        time.sleep(0.03)

    for d in range(77,47,-1):
        set_servo_deg(0,d)
        time.sleep(0.03)