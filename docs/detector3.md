# stm_detector3 — firmware state & host-side design

*As of firmware `a5c5006` (2026-07-22). Board: STM32G474CBT6 LQFP48, 128 KB
flash, dual coil sets, 8 analog channels, Quectel dual-antenna GNSS on USART2.*

## Serial protocol (USART1, 460800 8N1)

Lines starting `#` are non-data. Everything else is CSV.

### Data lines

```
lat,lon,fix,adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,heading[,vin_V,temp_C]
```

- One line per valid GGA fix (~20 Hz). The `vin_V,temp_C` status pair is
  appended once per second.
- `heading` (degrees true, from `$--THS`) is **empty** unless the
  dual-antenna solution is good (status `A`) and fresh (<1 s). Never stale,
  never guessed.
- `fix` is GGA fix quality: 4 = RTK fixed, 5 = RTK float, 2 = DGPS, 1 = GPS.
- **Idle line**: after 500 ms without a valid fix, a line is emitted every
  500 ms with the GPS fields (`lat,lon,fix,gps_ts`) empty, ADC values live,
  and the status pair always present. Lab/bench mode with no sky view.
- ADC order: PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14 (also reported in the
  info line, so the mapping survives pin changes).

### Info line (the study-header source)

```
# info fw=a5c5006 gps_ver=<PQTMVERNO reply> gps_id=<PQTMUNIQID reply>
  blanking_us=16 rx_window_us=3 tx_pulse_us=120 coil_spacing_mm=500
  coil_offset_fore_mm=0 coil_offset_right_mm=0
  adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14
```

(One line on the wire; space-separated `key=value`.) Printed at boot and on
the `I` console key. **The STM32 boots long before the Pi's daemon connects —
the daemon must send `I` at connect and parse the reply**, never rely on the
boot banner. GPS identity fields report `?` if the module never answered
(replies are NMEA-checksum-gated so a glitched reply can't persist).

### Console keys

QWERTY column pairs adjust values (top key raises); capitals are specials.

| Keys | Action |
|------|--------|
| `a`/`z` | blanking ±2 µs (0–200) |
| `s`/`x` | rx window ±1 µs (1–50) |
| `d`/`c` | coil spacing ±10 mm (50–5000) |
| `f`/`v` | TX pulse (coil charge) ±1 µs (10–120) — longer charges push more energy into the coil but heat the TX FETs |
| `S` | save settings to flash |
| `D` | restore compile-time defaults + save |
| `I` | print info line |
| `G` | toggle GPS passthrough (`# $PQTMTXT/$PQTMTAR/$--THS` echo, default off) |

Adjusting TX pulse echoes `# tx_pulse=..us`.

### Settings persistence

Append-log in flash page 31 (0x0801F000, outside the 124 K app region, so
reflashing firmware never touches it). Newest valid 32-byte record wins;
page erase only when its 128 slots fill. Fields: blanking, rx window, coil
spacing, fore/starboard antenna offsets, TX pulse. Records from older
firmware load with defaults for the fields they predate (0xFFFFFFFF
sentinel / out-of-range word). A blank or corrupted (ECC-purged) page
auto-writes the defaults at boot, so a unit always carries a valid record.
Defaults: blanking 16 µs, rx 3 µs, TX pulse 120 µs, spacing 500 mm,
offsets 0.

## Host-side design

### serial_daemon.py (IMPLEMENTED 2026-07-22; regression tests in
### test_serial_daemon.py — run them before deploying, no hardware needed)

- **Recording gate**: INSERT a point only when `fix == 4` (RTK fixed) AND
  heading present, plus the min-distance motion filter. `--no-gate`
  relaxes it to any valid position for bench work (meta `gate` records
  which mode the study ran under).
- **Live row**: single-row `live` table REPLACEd in place at ~2 Hz in
  *every* mode — constant DB size, and the DB stays the comms link between
  HW and UI. Columns: `updated_at` (daemon heartbeat), `data_at` (last
  data line), lat/lon/fix/heading, adc0..7, vin/temp (carried between 1 Hz
  pairs), gps_ts, `recording` flag, `reason`. Reason vocabulary:
  - `''` — recording
  - `fix=N` — position but not RTK fixed (N = GGA quality)
  - `no heading` — RTK fixed but dual-antenna solution absent
  - `no fix` — idle lines (bench/indoors)
  - `no data` — wire silent > 2 s (firmware emits ≥ every 500 ms, so this
    is a cable/power problem)
  - `garbage` — bytes flowing but nothing parses (wrong port, wrong baud,
    wedged firmware) — checked every loop iteration, so a garbage stream
    can never freeze the live row at a stale `recording=1`
  - `no port` — serial port can't be opened (retried every 2 s; startup
    log lists the ports that DO exist)
- **Study header**: at connect the daemon sends `I` and stamps meta:
  `fw_git_hash`, `gps_ver`, `gps_id`, `blanking_us`, `rx_window_us`,
  `tx_pulse_us`,
  `coil_spacing_mm`, `coil_offset_fore_mm`, `coil_offset_right_mm`,
  `adc_order`, plus `schema_version=3`, `gate`, `min_dist_mm`,
  `study_name`/`created_at` (stamp-once — a daemon restart on the same db
  can't rewrite provenance). Header keys are first-wins **per key** (a
  truncated info reply gets completed by the next one); `info_ok` is '1'
  only when every header key is present.
- **Mid-study drift**: timing echoes (`# blanking=..us rx_window=..us`,
  `# tx_pulse=..us`) and coil-spacing echoes (`# coil_spacing=..mm`),
  and any later info line, update `<key>_current` meta and set
  `timing_changed` / `geometry_changed` flags — the header is never
  silently updated (a retune means a new study by convention). The flags
  list every diverged key of their kind, recomputed from the `_current`
  values, so single-key echoes can't erase an earlier drift.
- **Points schema**: one row per kept line — `ts, lat, lon, heading (NULL
  ok), fix, adc0..adc7 (8 int columns), gps_ts`, nullable `vin`/`temp` on
  the lines that carried them. Per-coil positions are NOT stored — the
  visualizer computes them from lat/lon + heading + coil geometry.
- **Robustness** (adversarially reviewed): port opened `exclusive=True`
  (second reader fails loudly instead of splitting the byte stream); PID
  file refused if a live daemon owns it (double-Start race); sqlite
  errors never tear down the serial session (guarded writes, throttled
  logging that survives a full disk); WAL + `synchronous=NORMAL` (no
  per-commit fsync on the Pi SD card); `errors='replace'` decoding so a
  noise-corrupted byte can't splice a field into a valid-looking value.
- No detector2 back-compat anywhere.

### db_map.py (web UI — IMPLEMENTED: engineering view)

Rewritten for this schema (single Flask app; `test_db_map.py` is the
regression battery, `bench/` the no-hardware test rig). Beyond the design
below: a per-study **Rot°** heading offset (`sl_heading_offset_deg`, added
to reported heading before all coil math — absorbs a GPS that reports
heading along the antenna baseline instead of direction of travel);
heading-NULL points render as antenna dots + track instead of the coil
carpet (no-gate / drift studies); Leaflet + tiles degrade offline with
user geojson overlays still rendered. The operator (800x480) view is the
remaining piece — to be designed separately.

- 8 streams rendered per point: coil positions = antenna lat/lon + heading
  rotation + (spacing, fore/right offsets) from study meta.
- No heading fallback: full heading + RTK fixed is required for recorded
  data, so plotting never guesses (heading-less points show as antenna
  dots only).
- Status display decisions (the `live` row provides all of it):
  - GPS identity `?` in the header (or `info_ok=0`) → UI reports **GPS /
    unit-identity failure** as such.
  - `reason` distinguishes no data / garbage / no port / no fix /
    degraded fix / no heading; a stale `updated_at` (> ~3 s) with the PID
    file present means the daemon is wedged; PID file absent means it
    isn't running.
  - Live view shows recording state loudly (LIVE — NOT RECORDING banner
    with reason) — silent non-recording is the failure mode to avoid.
  - `timing_changed` / `geometry_changed` meta flags → loud warning on
    the study view (data before/after the change doesn't line up).

## Hardware v2 change list (parked)

- Swap UARTs so the host lands on bootloader-capable pins: host → USART2
  PA2/PA3 (ROM bootloader listens there), GPS → USART1 PB6/PB7 (bootloader
  ignores those). Enables field reflash via AN3155/stm32flash, no ST-Link.
- BOOT0 (= PB8, currently unused): pull-down + a way to drive it high;
  ideally BOOT0 and NRST to Pi GPIOs for fully remote recovery.
- Verify external pull-downs hold coil TX gates (PA5/PB4) and switch
  inputs safe while all MCU pins float in bootloader/reset.
- Coil-width provisioning may move off console keys later (user note).
