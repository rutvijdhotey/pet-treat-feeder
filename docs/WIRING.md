# Wiring & Pin Map

The breadboard prototype (M2) and the soldered proto HAT (M4) use **this exact pin map**, so moving to solder is a straight copy.

All GPIO numbers are **BCM**. Physical pin numbers are on the 40-pin header.

## Pin map

| Function | BCM GPIO | Physical pin | Direction | Notes |
|---|---|---|---|---|
| Power button | GPIO 3 | 5 | In | To GND. Board has on-board pull-up. `gpio-shutdown` overlay. Only pin that wakes a halted Pi. |
| Stepper IN1 | GPIO 5 | 29 | Out | ULN2003 IN1 |
| Stepper IN2 | GPIO 6 | 31 | Out | ULN2003 IN2 |
| Stepper IN3 | GPIO 13 | 33 | Out | ULN2003 IN3 |
| Stepper IN4 | GPIO 19 | 35 | Out | ULN2003 IN4 |
| Break-beam receiver | GPIO 16 | 36 | In | Internal pull-up; LOW when beam broken |
| LED Power (green) | GPIO 12 | 32 | Out | 330 Ω to anode, cathode to GND |
| LED Connected (blue) | GPIO 20 | 38 | Out | 330 Ω |
| LED Supply (amber) | GPIO 21 | 40 | Out | 330 Ω |
| Treat button | GPIO 26 | 37 | In | To GND, internal pull-up |
| I2C-3 SDA (software) | GPIO 23 | 16 | I/O | VL53L0X SDA (+ DS3231 SDA, optional) |
| I2C-3 SCL (software) | GPIO 24 | 18 | I/O | VL53L0X SCL (+ DS3231 SCL, optional) |
| 5 V | — | 2, 4 | Power | ULN2003 + motor, break-beam emitter |
| 3.3 V | — | 1, 17 | Power | VL53L0X VIN, break-beam receiver, DS3231 |
| GND | — | 6, 9, 14, 20, 25, 30, 34, 39 | Power | Common ground |

Reserved / unused: GPIO 2 (hardware I2C SDA, disabled), GPIO 14/15 (UART, kept free for serial console debugging).

## `/boot/firmware/config.txt`

```ini
# Hardware I2C off: GPIO 3 is the power button
dtparam=i2c_arm=off

# Safe shutdown on GPIO 3 (press again to wake)
dtoverlay=gpio-shutdown,gpio_pin=3,active_low=1,gpio_pull=up

# Software I2C bus 3 on GPIO 23 (SDA) / 24 (SCL) for the VL53L0X
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24
```

When the optional DS3231 RTC is added, it shares bus 3. Verify the RTC overlay on the software bus during M5 (`i2c-rtc-gpio,ds3231,i2c_gpio_sda=23,i2c_gpio_scl=24` replaces the `i2c-gpio` line).

## Power budget

| Load | Typical current |
|---|---|
| Pi 3 (Wi-Fi active) | ~500–700 mA |
| 28BYJ-48 via ULN2003 (only while moving) | ~240 mA |
| IR break-beam | ~20 mA |
| VL53L0X | ~20 mA |
| 3 LEDs | ~15 mA |
| **Peak** | **~1.0 A** |

A 2.5 A supply leaves ample headroom. Phone chargers rated 1–2 A cause brown-outs when the motor starts.

## Wiring notes

- Motor coils are de-energised after each move (all IN pins LOW).
- The VL53L0X breakout must have on-board pull-ups (most do); `i2c-gpio` has no internal pull-ups.
- Keep the break-beam emitter and receiver aligned across the chute throat; the chute model has 3 mm/5 mm holes for them.
- On the proto HAT every module connects through a **JST-XH** plug, keyed so it can't be reversed.
