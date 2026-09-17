# stm_detector3 — firmware state & host-side design

*As of 2026-08 (500 Hz decoupled-anchor protocol). Board: STM32G474CBT6
LQFP48, 128 KB flash, dual coil sets, 8 analog channels, Quectel
dual-antenna GNSS on USART2.*

## Serial protocol (USART1, 460800 8N1)

Lines starting `#` are non-data. Everything else is CSV, one 14-field
grammar (16 when the 1 Hz `vin_V,temp_C` status pair is appended):

```
lat,lon,fix,adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,heading,tick[,vin_V,temp_C]
```

Exactly one field group is populated per line — a line with both, neither,
or a partial group is corrupt and must be rejected:

- **Sample line** (ADC group only): `,,,adc0..7,,,tick` — one per ADC frame
  at the sampling rate (500 Hz default). Never carries GPS fields or
  heading.
- **Anchor line** (GPS group only): `lat,lon,fix,,,,,,,,,gps_ts,heading,tick`
  — one per valid GGA fix (~20 Hz), ADC fields empty. Its `tick` is the
  most recently *completed* ADC frame, paced to land ~1 ms before the fix:
  the host recovers per-sample positions by interpolating between
  consecutive anchors in tick space.
- `tick` is a monotonic count of pacer firings. Every pacer period burns a
  tick even if its ADC cycle was skipped, so frames are evenly spaced in
  time and a tick gap is an honest time gap — linear interpolation stays
  exact. Ticks reset only at MCU reboot (host detects the regression).
- Wire ordering: an anchor always follows the sample line bearing its tick;
  samples may run a line or two *ahead* of the anchor that will cover them
  (the host holds them for the next segment).
- Sampling is a **fixed rate**, never speed-adaptive — baseline stability
  wants a constant thermal/electrical cadence. Runtime-selectable
  20/40/100/200/500 Hz (`g`/`b` keys, persisted); rate only changes SNR
  per map bin, not the parse.
- `heading` (degrees true, from `$--THS`) is **empty** unless the
  dual-antenna solution is good (status `A`) and fresh (<1 s). Never stale,
  never guessed.
- `fix` is GGA fix quality: 4 = RTK fixed, 5 = RTK float, 2 = DGPS, 1 = GPS.
- **No fix**: sample lines continue at the full configured rate and no
  anchor lines are emitted — bench tools get the whole 8-channel stream
  with no GPS attached, and the analog front end keeps its field thermal
  duty cycle (a cold idle would make bench baselines lie about field
  baselines). Same grammar, so degradation needs no special casing; the
  daemon stores nothing without gate-passing anchors.
- The `vin_V,temp_C` status pair rides on whichever data line is due once
  the 1 s timer expires (sample or anchor).
- ADC order: PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14 (also reported in the
  info line, so the mapping survives pin changes).

### Info line (the study-header source)

```
# info fw=a5c5006 gps_ver=<PQTMVERNO reply> gps_id=<PQTMUNIQID reply>
  blanking_us=16 rx_window_us=3 tx_pulse_us=120 coil_spacing_mm=330
  coil_offset_fore_mm=0 coil_offset_right_mm=500
  sample_rate_hz=500 adc_oversample=16
  adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14
  detector_mode=all slot_us=200 bl8=16,16,16,16,16,16,16,16
  rx8=3,3,3,3,3,3,3,3 tx8=120,120,120,120,120,120,120,120
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
| `a`/`z` | blanking ±1 µs (0–200) |
| `s`/`x` | rx window ±1 µs (1–50) |
| `d`/`c` | coil spacing ±10 mm (50–5000) |
| `f`/`v` | TX pulse (coil charge) ±1 µs (10–120) — longer charges push more energy into the coil but heat the TX FETs |
| `g`/`b` | sample rate up/down through 20/40/100/200/500 Hz (echoes `# sample_rate=..Hz (N per fix)`; refused in raster mode if 8 slots wouldn't fit the new interval) |
| `0`–`7` | select the channel raster edits target |
| `8` | select all 8 channels (raster edits move in lockstep) |
| `r` | toggle raster mode ↔ all-at-once |
| `SAVE`+Enter | save settings to flash — word-gated so a stray noise byte on the console RX line can never trigger a flash write |
| `CLEAR`+Enter | restore compile-time defaults + save — word-gated like `SAVE` (both flash writes; a stray noise byte must never trigger either) |
| `I` | print info line |
| `G` | toggle GPS passthrough (`# $PQTMTXT/$PQTMTAR/$--THS` echo, default off) |

Adjusting TX pulse echoes `# tx_pulse=..us`. In raster mode `a`/`z`,
`s`/`x`, `f`/`v` edit the **selected channel's** table entry instead of the
shared globals (all 8 in lockstep when `8` is selected), and every
raster-affecting change (mode toggle, selection, table edit) echoes one
`# rastercfg detector_mode=.. slot_us=.. ch=.. bl8=.. rx8=.. tx8=..` line —
deliberately NEW token names, so a per-channel edit can never trip the
daemon's legacy `blanking=`/`tx_pulse=` drift patterns with one channel's
value. The same echo also fires once at boot (an MCU reset mid-study
reverts the selection to `all` and mode/tables to the saved record — the
echo re-syncs the daemon's live values) and on `CLEAR` (a defaults restore
is a mode+table+selection change like any other; the saved banner alone
carries only the legacy global tokens). A change that would break the
slot-fit rule is reverted with
`# raster limit: 8 slots of ..us must fit the ..us sample interval`.

### Raster mode

- Two detector modes, toggled with `r` and persisted. **all** (the original
  behavior): all 8 coils fire simultaneously with the shared global timing
  triple. **raster**: each sample interval starts with a sweep of 8
  fixed-period slots; slot *k* fires only channel *k*'s bus with channel
  *k*'s own {tx, blanking, rx} from the per-channel table and converts only
  channel *k* (single injected conversion, ~23 µs). The two tunings are
  independent — mode flips never mangle either.
- **One frame per sweep**: wire grammar, tick cadence, daemon and DB are
  untouched — a raster frame is the same 8-value sample line, its channels
  just measured `slot_us` apart. At 1.5 m/s the 1.6 ms sweep is ~2 mm of
  along-track skew — negligible vs the 20 mm bin, and the header carries
  `slot_us` so the renderer can correct it if it ever matters.
- **Slot period auto-derives**: max(200 µs, worst-channel tx+bl+rx + ~25 µs
  ADC + margin), rounded up to the pacer's 100 µs ticks and reported as
  `slot_us`. Fit rule: `8 × slot ≤ sample interval` — edits, mode toggles,
  rate steps and even a stale saved record that would break it are refused
  (reverted / fall back to all mode) with a console notice.
- **Channel↔bus map**: evens on Bus A (TIM2→PA5), odds on Bus B (TIM3→PB4)
  — INFERRED from the ADC pair structure, encoded in one firmware macro
  (`MD_CH_SET_B`) so a logic-analyzer check can flip it in one line. During
  a slot the idle set's four outputs are forced inactive (coils silent,
  integrators held in reset); a GPS fix landing mid-sweep defers the pacer
  resync to the sweep boundary so slot spacing inside a sweep is never
  disturbed.

### Settings persistence

Append-log in flash page 31 (0x0801F000, outside the 124 K app region, so
reflashing firmware never touches it). Two record generations share the
page — the loader scans for BOTH magics and the newest valid record of
either kind wins; page erase only when the slots fill:

- **MDT3** (32 bytes, 128 slots/page — legacy, read-only now): blanking
  (bits 23:0; bits 31:24 held the retired fire mode — ignored on load), rx
  window (bits 7:0; samples-per-period in bits 15:8, where 0 marks a
  pre-rate record and loads the default), coil spacing, fore/starboard
  antenna offsets, TX pulse. Loading one seeds the raster table from the
  globals and starts in all mode.
- **MDT4** (64 bytes, 64 slots/page, only at even 32-byte slots): the MDT3
  words (detector mode now lives in blanking bits 31:24) plus the three
  8-entry per-channel tables (bl/rx/tx) and two reserved words written
  erased, so a future field can tell "older record" from a real 0. Save
  always writes MDT4.

Records from older firmware load with defaults for the fields they predate
(0xFFFFFFFF sentinel / out-of-range word); a saved raster mode that no
longer fits the saved sample rate falls back to all mode with a console
notice. A blank or corrupted (ECC-purged) page auto-writes the defaults at
boot, so a unit always carries a valid record. Defaults: blanking 16 µs,
rx 3 µs, TX pulse 120 µs (per-channel table seeded the same), spacing
330 mm, fore offset 0, starboard offset 500 mm, 25 samples/period
(500 Hz), all mode.

## Host-side design

### serial_daemon.py (IMPLEMENTED 2026-07-22; regression tests in
### test_serial_daemon.py — run them before deploying, no hardware needed)

- **Recording model** (500 Hz protocol): samples between two consecutive
  anchors form a *segment*. A segment records only when **both** anchors
  pass the gate — `fix == 4` (RTK fixed) AND heading present (`--no-gate`
  relaxes to any valid position for bench work; meta `gate` records the
  mode) — and its spans are sane (gps_ts delta in (0, 0.5 s], tick span
  ≤ 1000). Each sample's position is linearly interpolated between the
  anchors in tick space (heading via shortest-arc slerp), then fed into a
  running **bin**: when a sample lands ≥ `--min-dist` mm (default 20)
  from the last saved row, the bin flushes as one row — position =
  centroid, adc = per-channel mean, fix = worst in bin, gps_ts = newest.
  Stationary → the bin grows and no rows are written (the DB does not
  grow when parked). Bins carry across segments; any continuity break
  (gate fail, dropout, reboot) flushes the open bin at its own centroid
  so pre- and post-gap positions can never average into a phantom row.
  Rows insert with one commit per anchor (~20/s), not per row.
- **Tick regression** (anchor tick ≤ previous anchor's) means the MCU
  rebooted: pending samples are discarded and the chain restarts at the
  next anchor.
- **Live row**: single-row `live` table REPLACEd in place at ~2 Hz in
  *every* mode — constant DB size, and the DB stays the comms link between
  HW and UI. Columns: `updated_at` (daemon heartbeat), `data_at` (last
  data line), lat/lon/fix/heading (from the newest anchor), adc0..7 (from
  the newest sample line), vin/temp (carried between 1 Hz pairs), gps_ts,
  `recording` flag, `reason`. Reason vocabulary:
  - `''` — recording
  - `fix=N` — position but not RTK fixed (N = GGA quality)
  - `no heading` — RTK fixed but dual-antenna solution absent
  - `no fix` — no anchor fresher than 1 s (idle/bench mode emits no
    anchors, so this covers indoors and sky-loss alike)
  - `no data` — wire silent > 2 s (firmware emits sample lines
    continuously even with no fix, so this is a cable/power problem)
  - `garbage` — bytes flowing but nothing parses (wrong port, wrong baud,
    wedged firmware) — checked every loop iteration, so a garbage stream
    can never freeze the live row at a stale `recording=1`
  - `no port` — serial port can't be opened (retried every 2 s; startup
    log lists the ports that DO exist)
- **Sample ring**: `live_samples` table — a bounded ring (`SAMPLES_KEEP` =
  2048 rows, ~4 s at 500 Hz) of the raw full-rate stream (`id`
  AUTOINCREMENT, `tick`, `adc0..7`), flushed on the live-row cadence and
  trimmed in the same transaction. Feeds the web UI's strip charts at the
  full sample rate regardless of fix/recording state. AUTOINCREMENT keeps
  ids monotonic across restarts *on the same db file* so an SSE cursor can
  never see stale rows as new; `/start_raw` recreates the raw db (sequence
  resets to 1), which the SSE generator handles by re-seeding its cursor on
  id regression. The ring is cleared at daemon start and clean exit (a
  kill -9 leftover is also fenced server-side: samples are only served
  while a daemon is attached).
- **`--raw` (View Raw mode)**: everything above runs — live row, sample
  ring, info handshake, command FIFO — but recordable rows are dropped
  before insert and `recording` is pinned 0 (meta `raw_mode=1`). Used by
  the web UI's View Raw screen against a throwaway db in the temp dir:
  full-rate display for bench test/calibration with nothing saved.
- **Study header**: at connect the daemon sends `I` and stamps meta:
  `fw_git_hash`, `gps_ver`, `gps_id`, `blanking_us`, `rx_window_us`,
  `tx_pulse_us`,
  `coil_spacing_mm`, `coil_offset_fore_mm`, `coil_offset_right_mm`,
  `adc_order`, `detector_mode`, `slot_us`, `bl8`/`rx8`/`tx8` (the raster
  per-channel tables, 8-value CSVs), plus `schema_version=3`, `gate`,
  `min_dist_mm`,
  `study_name`/`created_at` (stamp-once — a daemon restart on the same db
  can't rewrite provenance). Header keys are first-wins **per key** (a
  truncated info reply gets completed by the next one); `info_ok` is '1'
  only when every header key is present — an old-firmware board without
  the raster tokens now parks at `info_ok=0` (the UI's incomplete-header
  warning doubles as the reflash nudge; data still records fine).
- **Mid-study drift**: timing echoes (`# blanking=..us rx_window=..us`,
  `# tx_pulse=..us`, `# sample_rate=..Hz`), raster echoes (`# rastercfg
  detector_mode=.. slot_us=.. ch=.. bl8=.. rx8=.. tx8=..`), coil-spacing
  echoes (`# coil_spacing=..mm`),
  and any later info line, update `<key>_current` meta and set
  `timing_changed` / `geometry_changed` flags — the header is never
  silently updated (a retune means a new study by convention). The flags
  list every diverged key of their kind, recomputed from the `_current`
  values, so single-key echoes can't erase an earlier drift. The
  `rastercfg` echo's `ch=` field is the console's live channel selection
  — pure UI state, stored as plain `raster_sel` meta (never a header key)
  to drive the raw view's channel chips.
- **Points schema**: one row per flushed bin — `ts, lat, lon, heading
  (NULL ok), fix, adc0..adc7 (8 int columns), gps_ts`, nullable
  `vin`/`temp` (latest status pair at insert time). Per-coil positions
  are NOT stored — the visualizer computes them from lat/lon + heading +
  coil geometry.
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
user geojson overlays still rendered. The SSE stream carries the daemon's
sample ring alongside new rows, so the strip chart scrolls at the full
sample rate with no fix and no recording; **View Raw** (`/raw` +
`/start_raw`, daemon `--raw`) is the map-less version of that: telemetry +
timing controls + a large shared-scale chart (autoscale on one shared
raw axis, Δ-from-zero overlay, or absolute 0–65535), nothing written. The operator (800x480) view is the remaining
piece — to be designed separately.

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
