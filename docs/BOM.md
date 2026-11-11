# Bill of Materials

Prices are rough USD estimates (Nov 2026). Each line is tracked as a `purchase` issue on the [project board](https://github.com/users/rutvijdhotey/projects/5) — close it when the part arrives.

## Already owned

| Part | Notes |
|---|---|
| Raspberry Pi 3 (B or B+) | Either works |
| microSD card (16 GB+, A1) | Re-flash with Pi OS Lite 64-bit |
| 5 V micro-USB power adapter | **Verify label says 5 V ≥ 2.5 A** — otherwise buy a 3 A supply |
| PLA + PETG filament, 3D printer | Friend's printer |

## To buy — must-have

| Qty | Part | Purpose | ~Cost |
|---|---|---|---|
| 1 | 28BYJ-48 5 V stepper + ULN2003 driver board | Turns the pocket disc | $3 |
| 1 | IR break-beam sensor pair (3 mm or 5 mm) | Confirms each treat dropped | $3 |
| 1 | VL53L0X time-of-flight breakout (with on-board regulator & pull-ups) | Hopper level | $4 |
| 1 | 5 mm LED assortment (green, blue, amber) + 330 Ω resistors | Status LEDs | $3 |
| 2 | 12 mm momentary push buttons (panel mount) | Power, treat | $2 |
| 1 | Raspberry Pi proto HAT (or half-size perfboard + 2×20 female header) | Permanent wiring | $5 |
| 1 | JST-XH 2.54 mm connector kit + 22 AWG hook-up wire | Pluggable module connections | $10 |
| 1 | M3 screw assortment + M3 heat-set inserts | Assembly | $10 |
| 1 | Silicone/rubber adhesive feet (≥ 4) | Non-slip base | $3 |
| — | Steel washers / coins / small sand bag | Base weight | $2 |
| 1 | Breadboard + 40-pin T-cobbler + Dupont jumper kit (M-M, M-F, F-F) | No-solder prototype (M2) | $10 |
| 1 | Soldering iron kit (temp-controlled, solder, flux, tip cleaner) | Proto HAT (M4) | $25 |
| 1 | Braided cable sleeve (~1 m) | Chew protection | $3 |

**Must-have subtotal: ~$83** (≈ $48 without the soldering kit and breadboard kit, which are reusable tools).

## Optional

| Qty | Part | Purpose | ~Cost |
|---|---|---|---|
| 1 | DS3231 RTC module | Correct time after power cuts without internet | $3 |
| 1 | 5 V 3 A micro-USB power supply | Only if current adapter < 2.5 A | $10 |
| 1 | A3144 hall sensor + 6×3 mm magnet | Disc homing, only if stepper alignment drifts | $2 |
