# Metal Mapper

This is an 8 channel implementation of the common open-source pulse induction metal detector
circuit known as Clone-PI.  Each channel comprises 4 signals: pulse control (when active, 
the coil is exposed to a DC voltage converging on saturation), integrator reset (shorts an 
integration cap on an op-amp) to enable only during the integration window (Rx Window), 
the gain cut, and the integrator signal feed.  The gain is cut and integrator not fed 
except during active measurement.  There are 4 ADCs, and 2 sets of the aforementioned 
control signals, arranged even/odd for ch0-7.  As of this writing (9.26.2026), 200Hz
all-at-once sampling is in use at 51/10/100 (blanking/window/pulse).  After determining 
a bit of coil-to-coil skew, future versions will separate out the pulse firing circuitry.

There is a heading and RTK equipped GPS receiver providing heading and location data 
to the STM32 which is used to tag the PI detection values.  A default rate limit of 20mm/point
is implemented so that sitting still doesn't cause the data file to grow uncontrollably.  

The GPS has its own UART connection to the same pi that is connected to the STM32, and 
the only purpose of that connection is for the pi to provide the RTK correction stream
of data to the GPS receiver.  The STM32 is responsible for pairing the measured PI data
with the location data.  The pi daemon will not log data unless an RTK fix + heading 
has been achieved.  

A web-based UI runs on the pi that is attached to the metal detector hardware and provides
a live display of detector values from the db file that is written by the serial daemon 
as it reads from the STM32 a CSV stream.  There is additionally a 3rd GPS antenna on the 
mobile vehicle which provides navigation.  It also receives an RTK correction stream and is
used for vehicle guidance and after-the-fact target finding.

The rest of this shit is mostly the AI, but is useful in general and factual.  

A field instrument system for GPS-tagged pulse-induction metal detection. The STM32G474
embedded firmware pulses two coil sets, samples 8 analog channels at up to 500 Hz, and streams
them over serial; a Raspberry Pi runs a Python daemon that reconstructs sample positions
between RTK fixes and records them into SQLite studies, and a Flask/Leaflet web front-end
lets you browse saved studies or watch a live survey in real time.

---

## System Architecture

```
STM32G474 detector board  (+ Quectel dual-antenna RTK GNSS on USART2)
  └─ USART1 @ 460800
        └─ serial_daemon.py  (Pi / dev machine)
              ├─ ~/metal_mapper/studies/<name>.db  (SQLite, WAL mode)
              └─ /tmp/metal_detector_cmd.fifo  (command back-channel)
                    └─ db_map.py  (Flask + Leaflet web UI)
                          └─ nginx reverse proxy (Pi deployment)
```

### CSV data format from STM32

One 14-field grammar (16 when the once-per-second `vin_V,temp_C` status pair is
appended); exactly one field group populated per line:

```
lat,lon,fix,adc0..adc7,gps_ts,heading,tick[,vin_V,temp_C]
,,,33421,33398,33440,33415,33402,33388,33429,33410,,,184223        ← sample (200 Hz as run; up to 500 Hz)
40.786213504,-119.204512395,4,,,,,,,,,123519.050000,87.25,184225   ← anchor (~20 Hz)
```

- **Sample lines** carry ADC + tick only; **anchor lines** carry one GPS fix +
  the tick of the newest ADC frame completed when the fix parsed (within one
  sample interval before it — the pacer free-runs). The daemon
  interpolates each sample's position between consecutive anchors in tick
  space, then averages samples into one saved row per `--min-dist` mm of
  travel. No fix → anchors stop but samples keep streaming at full rate
  (bench visibility; nothing records without anchors).
- `fix`: GGA quality — 4 = RTK fixed, 5 = RTK float, 2 = DGPS, 1 = GPS
- `adcN`: 16-bit unsigned; hardware sum of 16 oversampled 12-bit conversions
  (0–65520). The web UI colors in raw counts — nothing is normalized.
- `heading`: degrees true from the dual-antenna solution; empty unless valid
  and fresh — never stale, never guessed.
- Lines prefixed with `#` are non-data: boot banner, info line (study header
  source), and settings echoes like `# blanking=16us rx_window=3us`.

Full protocol details (tick semantics, console keys, flash persistence,
daemon recording model): [`docs/detector3.md`](docs/detector3.md).
Multi-unit studies sync (two head units through the RTK base Pi hub,
per-host dig-target sidecars): [`docs/studies_sync.md`](docs/studies_sync.md).

---

## Scripts

### `serial_daemon.py` — Data collection daemon

Connects to the STM32 serial port, reconstructs where each sample (200 Hz as
the instrument is run; the firmware goes to 500 Hz) was taken by interpolating
between RTK anchors, and writes distance-binned rows into
a SQLite study file. A FIFO at `/tmp/metal_detector_cmd.fifo` lets the web UI
send single-byte commands back to the STM32 (timing adjustment keys).

```bash
python serial_daemon.py \
  --port /dev/cu.usbmodem1103 \
  --db ~/metal_mapper/studies/myrun.db \
  [--baud 460800]          # default 460800 — matches STM32 USART1
  [--min-dist 20]          # mm of travel per saved row; default 20
  [--study-name "My run"]  # human label stored in meta table
  [--no-gate]              # record without RTK-fixed+heading (bench work)
  [--debug]                # print each received line + insert confirmations
```

**Distance binning**: interpolated samples accumulate in a running bin; each
time one lands `--min-dist` mm from the last saved row the bin flushes as a
single row (centroid position, per-channel mean ADC). At the 200 Hz the
instrument runs at and 20 mm spacing that averages ~10 samples per row at
survey speed (~25 at 500 Hz) — the oversampling
becomes SNR instead of database bloat — and a stationary detector writes
nothing at all. Recording requires RTK-fixed + dual-antenna heading on both
ends of a segment (unless `--no-gate`); the live recording state is written
to the study's single-row `live` table at ~2 Hz so the web UI can display it.

**PID file**: `{pid}:{abs_db_path}` written to `/tmp/metal_detector_daemon.pid` on
startup, removed on exit. The web UI uses this to detect whether a study is live.

---

### `db_map.py` — Web front-end

Flask + Leaflet map. Lists saved studies and lets you view them or start a new live
recording session.

```bash
python db_map.py [--web-port 5000]
```

Open: `http://localhost:5000`

**Studies directory**: `~/metal_mapper/studies/*.db`  
**Config (sticky port etc.)**: `~/metal_mapper/config.json`

`config.json` also holds `heading_offset_deg` (default 270): added to the
GPS-reported heading before the coil array is drawn, correcting a heading
aligned to the antenna baseline instead of the direction of travel. It is a
property of the vehicle mounting, so it lives in config rather than in the
web UI; edit the file and reload. A study saved with its own rotation
(`sl_heading_offset_deg` in its meta, from before this moved to config)
keeps that value.

#### Study list page

- The two things a field operator does are big buttons at the very top:
  **▶ Start Recording** and **📈 View Raw** (which starts streaming
  immediately — no intermediate screen). Everything configurable lives at
  the bottom, below the studies.
- Shows all `.db` files in the studies directory, newest first, and which
  study (if any) the daemon is currently writing
- On the first page load after the app starts, studies with **zero recorded
  points** are deleted (db + log) — failed and aborted starts otherwise
  clutter the list. A study a running daemon holds is never touched, and
  logs written since this boot are kept (a start that just failed may be
  showing you its log). Orphan logs from before the boot are swept too.
- **Recording Settings** panel (bottom): study name (pre-filled
  `geo_YYYYMMDD_HHMMSS`), serial port (remembered from last session),
  minimum distance in mm, bench-mode gate bypass — submitted by the Start
  Recording button up top
- While a daemon runs, **View Raw** becomes a viewer onto the running
  study's stream (see below), and the primary button becomes **▶ Return
  to Live Study** (or **📈 Return to Raw View** for a raw session) — a
  client reset mid-run lands the operator one tap from where they were
- The **NAV GPS chip updates live** (SSE `/stream/__nav__`): the kiosk
  loads this page at boot, usually before the nav source is accepting
  connections, so a one-shot render would show "down" forever
- **⏻ power button** (top-right corner): confirm overlay, then a proper
  OS shutdown via `POST /shutdown` → `sudo -n /usr/sbin/shutdown -h now`.
  Passwordless sudo is assumed — true for the stock Raspberry Pi OS
  first user; a hardened install needs a NOPASSWD sudoers rule for
  `/usr/sbin/shutdown`. `-n` means any refusal surfaces as an error on
  the landing page instead of hanging. Success lands on GET
  `/shutting_down` (Post/Redirect/Get — reloading or restoring that tab
  after the next power-on must not re-fire the POST). systemd stops the
  web service on the way down, which cleanly SIGTERMs any recording
  daemon in its cgroup. The confirm warns if a session is live, tracked
  over the same SSE tick as the nav chip so sessions started from other
  clients still warn on the long-lived kiosk page.

#### Map view page

**Toolbar controls (always available):**

| Control | What it does |
|---------|-------------|
| ← Studies / ■ Stop & Exit | On a saved study: back to the study list. On the live study the two are one action — leaving the map ends the recording, so a daemon can never keep running behind the operator's back |
| Study name | Click to rename (Enter saves, Esc cancels) — changes the label shown everywhere; the `.db` filename and URL stay the same |
| ⓘ Meta | Collapsible panel of the study's acquisition provenance: recording setup (gate, min distance, serial port), firmware build, ADC oversample and channel order, GPS id/version, PI timing, and coil geometry. Values that drifted mid-study show `stamped → current` and an incomplete header is flagged |
| 🎯 Re-zero | Set per-channel zero baselines from average of last 20 points |
| ◎ Follow ON / OFF | Map auto-pans to the newest recorded point. OFF frees the map to pan/zoom/mark a live study (e.g. from a laptop at base) — display-only, per browser; recording and marks keep streaming |
| − Range slider | ADC counts below zero that map to full green (default 450, max 2000) |
| + Range slider | ADC counts above zero that map to full red (default 450, max 2000) |
| Offset slider | Shifts effective zero of all channels, in counts (default −140, range ±2000) |
| 💾 Save View | Persists slider/zero settings into the study's meta table |
| Filter | `Δ` subtracts the array mean at each instant (common-mode rejection), grouped `2×4` — even and odd coils, the pairs the four ADCs convert together — or `all 8`; `sync` subtracts the study's average GPS-second-synchronous waveform (fold period ½/1/2 s, template 50/25/10 ms) — for the once-per-second interference in recordings made before the September 2026 firmware pacer fix |
| Combine | on: overlapping passes accumulate (re-pass adds); off: newest dots cover older |
| Channel strip chart | scrolling graph of all 8 channels' deviation from their zero baselines (so the lines overlay instead of fanning out by DC offset), one shared scale, one line color per channel, newest samples at the right; current raw values listed vertically beside it, colored by the map's value color. Live sessions feed it from the daemon's full-rate sample stream, so it scrolls with or without a GPS fix and whether or not anything is being recorded; saved studies seed it from their recorded rows |

**Color scheme**: at zero → gray, above zero → red, below zero → green,
in raw ADC counts. Zero is per channel; offset/ranges are shared. Baselines
auto-seed from the study's first 10 points until Re-zero sets them.

**Δ array mean filter**: subtracts the mean zeroed deviation of the coils
at each instant before coloring. Anything that moves the whole array
together — thermal drift, bulk ground response — cancels to gray instead of
striping whole passes red/green; only differential (target-like) signal keeps
color, with a faint opposite-sign halo on neighboring coils (inherent to mean
subtraction). Trade-off: a response that lifts all 8 coils equally is itself
common-mode and is suppressed — hence a toggle, not a replacement. Click
popups show both the raw ADC and the filtered Δ that drives the color.

**Rendering at scale**: all dots draw onto a single canvas from columnar
typed arrays (~90 B/point) — no per-dot map objects — so studies of
hundreds of thousands of points load in about a second and stay
responsive. Zoomed out past ~30k in-view points the renderer switches to
peak-preserving binning: each screen cell shows its strongest deviation,
so a hit can never be averaged away; zooming in returns to per-coil dots.
Dots are sized automatically — exactly one coil spacing wide on screen —
so the 8 coil tracks tile into a seamless swath at every zoom.
Click any dot (old or new) for its channel, raw ADC, position, heading,
and fix. Points recorded without a heading draw as hollow blue antenna
dots — coil positions are never guessed.

**Combine (multi-pass accumulation)**: with Combine checked (default),
overlapping dots merge in the value domain instead of painting over each
other — an earlier pass is never hidden or washed out by a later one, and
ground covered on multiple passes shows the *sum* of what each visit saw:
a target seen twice reads roughly twice as strong. Overlap within a single
visit doesn't add (a pixel keeps its extreme value), so slow driving gains
nothing. Unchecked: classic painting, newest dots cover older ones.

**Toolbar (live sessions only)**: `Motion: NNN mm/pt` shows the distance
from the last saved point and reads PAUSED when stationary. The detector
timing controls (blanking, RX window, TX pulse, sample rate, Save Settings)
live on the View Raw page, below.

**Incremental SSE**: initial load fetches all points; SSE `/stream/<study_id>` then
delivers only new rows every 0.5 s. The heartbeat event also carries motion status
so the display updates even when no new points are being saved, and — while a
daemon is attached — the newest raw samples from the daemon's `live_samples`
ring (the strip chart's feed; the first tick ships the whole ~4 s ring so the
chart starts full).

**Saved-study environment stats**: opening a saved study replaces the live
Vin/T readout in the toolbar with whole-run stats — `min–max · avg` for both
supply voltage and board temperature, aggregated over every recorded point
(each point carries the most recent 1 Hz vin/temp report; rows written
before the first report are skipped).

**Per-study slider persistence**: `+ Range`, `− Range`, `Offset`, the filter
mode, the Combine switch, and the re-zeroed baseline are saved to the study's
`meta` table when you click 💾 Save View, and restored automatically the next
time you open that study.

#### View Raw page

System-test and calibration screen (`/raw`; the study list's View Raw button
starts it in one touch): a compact telemetry strip across the top (fix
quality, heading, position, GPS time, Vin, temperature, measured sample
rate, firmware build, port — sized to stay one row on an 800px-wide
display), the Detector Timing controls, and the same range/offset sliders
and filter as the map view — and in place of the map, a large version of
the channel chart fed at the full sample rate, taking all remaining screen
height. **Nothing is written to the points database**: the button launches
`serial_daemon.py --raw` against a throwaway db in the temp dir (never
listed as a study), where the daemon keeps its live row and sample ring for
the UI but drops every recordable row, with the recording flag pinned 0.
No GPS needed — bench units stream with no antenna connected.

**Detector Timing controls** (sent to the STM32 through the daemon's FIFO):

| Control | What it does |
|---------|-------------|
| Blanking ±1µs | `a`/`z` key |
| RX Window ±1µs | `s`/`x` key |
| TX Pulse ±1µs | `f`/`v` key |
| Rate slider | Browse the 20/40/100/200/500 Hz sample-rate list — each stop away from the current rate sends one `g`/`b` step |
| 💾 Save Settings | Sends the word `SAVE` + newline — saves timing to the STM32's flash (word-gated so console-line noise can't trigger it) |

- **Scale select**: `autoscale` fits ONE shared axis (gain and offset) to
  the raw counts, so channel levels and amplitudes compare directly —
  channels at different DC points fan into separate traces; `Δ from zero`
  offsets each channel to its own zero baseline so the traces overlay
  (dashed zero line, like the map-view strip) — shapes compare, levels
  don't; `0 – 65535` plots raw counts on the full ADC range — where each
  analog stage actually sits.
- Channel zeros are set automatically from the first ~half second of the
  stream (display-only; nothing is stored or sent to the detector).
- If a *study* daemon is already running, `/raw` becomes a raw viewer for
  that study's stream. The exit rule is the same here: **■ Stop & Exit
  ends the recording study** — to keep recording, leave via 🗺 Open study
  map instead.
- Data-flow faults (`no port` / `no data` / `garbage`) show as an amber
  STREAMING FAULT banner; missing GPS is normal here and stays quiet.
- **Link** (second telemetry row): stream integrity from the sample ticks
  — samples/s, fixes/s, frames per fix (rate/20 ±1 as the free-running
  pacer slides against the GPS clock), tick gaps, frames lost in them,
  MCU reboots (tick went backwards and the stream continued from there; a
  lone mangled tick is counted separately, in the tooltip). Green means no
  gap and no reboot since the session started. It counts what the daemon
  parsed: a line garbled in flight is a one-frame gap, and anything
  upstream of the firmware's tick counter is invisible here — that is what
  the P line's `skips` covers. **Pacer P** asks the firmware for its
  `# pace` diagnostics line (interval jitter, skipped starts, GGA spacing,
  NMEA parse time — see `docs/detector3.md`, *Pacer*), shown verbatim
  with its age. Together they separate a software artefact from an analog
  one without a scope.
- **■ Stop & Exit** is the only way out while streaming: stopping and
  returning to the studies page are one action (same rule as the live
  map). Starting takes no options — it attaches to the configured serial
  port at the daemon's built-in baud.

---

## Pi Deployment

### App directory: `/opt/metal_mapper`

Built by `install_pi_system.sh` (see Setup below): the app files —
`db_map.py`, `serial_daemon.py`, `nav_gps.py`, `gunicorn_config.py`,
`sync_studies.sh`, `static/`, `templates/`, and every `*.geojson` overlay — are **symlinked**
from the git checkout, and a venv is created from `requirements.txt`.
A `git pull` in the checkout therefore updates the app in place; restart
gunicorn afterwards (templates are cached at start). A missing
`templates/index.html` symlink is the classic 500 `TemplateNotFound` in
`/var/log/metal_mapper/error.log` — re-run the install script.

### `gunicorn_config.py`

```python
bind           = "127.0.0.1:8000"
workers        = 1           # must be 1 — SSE and daemon state are process-local
worker_class   = "gthread"   # threads let SSE hold a connection while others flow
threads        = 8           # every open page holds one SSE stream
timeout        = 0           # disable worker timeout — SSE connections are long-lived
worker_tmp_dir = "/dev/shm"  # spare the SD card
errorlog       = "/var/log/metal_mapper/error.log"
accesslog      = "/var/log/metal_mapper/access.log"
loglevel       = "info"
```

### nginx config

Checked in at `nginx/metal-mapper`; the install script symlinks it into
`/etc/nginx/sites-enabled/` (and drops the stock default site, which
would otherwise shadow :80). The `location /stream/` block is what keeps
SSE alive through the proxy — buffering off, 24 h read timeout:

```nginx
upstream metal_mapper_app {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name _;

    location /stream/ {
        proxy_pass         http://metal_mapper_app;
        proxy_set_header   Host $host;
        proxy_set_header   Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 24h;
    }

    location / {
        proxy_pass       http://metal_mapper_app;
        proxy_set_header Host $host;
        proxy_buffering  off;
    }
}
```

### systemd services

The unit files live in the repo root as templates (`jlw_metalmap.service.in`
runs the web UI; the `jlw_rover_*` units feed it GPS; `jlw_ui_mm.service.in`
is the kiosk browser; `jlw_mm_sync.timer.in` + `.service.in` run the studies
sync, see [`docs/studies_sync.md`](docs/studies_sync.md);
`jlw_rover_outfeed_mmdebug.service.in` is a debug tap that mirrors a third
serial port to TCP 50011 — installed like the rest, not used in normal
operation). `install_pi_system.sh` renders them into
`/etc/systemd/system`, filling the `@MM_*@` placeholders with the service
user (whoever ran `sudo`), its home, the checkout path and the RTK base host
(`northpole` unless `MM_BASE_HOST` in the gitignored `install.conf` says
otherwise — see `install.conf.example`). No account names or paths live in
the unit templates (the base Pi's `rtkbase/` units are deployed by hand and
assume an `rtk` account).
After a `git pull` that touches a template, re-run the installer: it
re-renders, daemon-reloads, and is idempotent.

Upgrading a Pi installed before September 2026, when the units were
symlinked into `/etc/systemd/system`: run the installer right after the pull
that brought the templates, before any `systemctl daemon-reload` or reboot.
The files those links pointed at were renamed to `*.in`, so the links dangle
until the installer replaces them with rendered files. A plain `systemctl
restart` still works meanwhile (systemd keeps the loaded unit), but a reload
or reboot before the installer runs leaves every `jlw_*` unit `not-found`.

The two `jlw_rover_rtk*` units gate their start on the GPS tty existing
and the base station's caster answering (steady amber dot on the home
screen while waiting, instead of the old green/grey restart flapping).
The wait is unbounded, so a plain foreground `systemctl start`/`restart`
of them blocks until the gate passes — add `--no-block` when the base or
GPS may be absent.

### Setup commands

```bash
# Optional: override a default (service user, its home, RTK base host)
# cp install.conf.example install.conf && nano install.conf

# One script from the repo checkout sets up the system: renders + enables
# every unit template in the repo root, builds /opt/metal_mapper (app
# symlinks + venv from requirements.txt), creates the gunicorn log dir, and
# puts the service user in dialout for the detector's serial port.
# Idempotent — re-run after a pull that touches a unit template, an
# overlay, or a dependency.
./install_pi_system.sh

# Tail daemon log while a study is running
tail -f ~/metal_mapper/studies/<name>.log
```

---

## Local Development

```bash
pip install flask pyserial gunicorn

# Terminal 1 — web UI
python db_map.py

# Terminal 2 — daemon (once STM32 is connected)
python serial_daemon.py --port /dev/cu.usbmodem1103 \
  --db ~/metal_mapper/studies/test.db --debug
```

---

## STM32 Firmware (`stm_detector3/`)

MCU: STM32G474CBT6 (LQFP48, 128 KB flash, DBANK=0). FreeRTOS, HAL, CubeMX.
See [`docs/detector3.md`](docs/detector3.md) for the serial protocol, console
keys, and settings persistence, and [`docs/analog_channel.md`](docs/analog_channel.md)
for the analog channel as built, from the schematic.

**Key facts:**
- Pulse chain is a hardware timer timeline (TIM2 master); TIM7 paces ADC
  frames at the saved sample rate — 200 Hz as run (10 frames per GPS fix),
  selectable 20–500 Hz, 500 Hz being the ceiling
- Dual coil sets — Set A (even channels, TX on PA5) and Set B (odd channels,
  TX on PB4) — both fire every cycle
- USART1 @ 460800 baud → host (Pi daemon)
- USART2 @ 460800 baud → Quectel dual-antenna RTK GNSS, 20 Hz GGA + THS
  true heading, configured by the firmware at boot
- Runtime keys: blanking, RX window, TX pulse, coil spacing, sample rate,
  raster mode (channels fired one at a time) and its pulse spacing;
  typing `SAVE` + enter saves to a flash append-log (page 31, survives
  reflash)
- ADC: 16× hardware oversampling (sum, no shift → 0–65520 counts)
- Flashing a factory-fresh chip needs the DBANK option bit cleared first:
  use `stm_detector3/provision_virgin_board.sh`

The STM32H723 Nucleo-144 predecessors (`stm_detector`, `stm_detector2`) were
retired from the tree in August 2026; they survive in the history and in the
`v0.1` / `v0.2` release tags.

---

## License

Metal Mapper is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE). Copyright (C) 2025–2026 Jeremy Wade.

Third-party components keep their own licenses, unchanged:

| Component | Where | License |
|---|---|---|
| STM32G4 HAL (STMicroelectronics) | `stm_detector3/Drivers/STM32G4xx_HAL_Driver` | BSD-3-Clause |
| STM32G4 CMSIS device headers (STMicroelectronics) | `stm_detector3/Drivers/CMSIS/Device/ST/STM32G4xx` | Apache-2.0 |
| CMSIS core (Arm) | `stm_detector3/Drivers/CMSIS/Include` | Apache-2.0 |
| FreeRTOS kernel (Amazon) | `stm_detector3/Middlewares/Third_Party/FreeRTOS` | MIT |
| minmea NMEA parser (Kosma Moczek) | `stm_detector3/Core/Src/minmea.c`, `Core/Inc/minmea.h` | [WTFPL](http://www.wtfpl.net/txt/copying/) |
| Leaflet 1.9.4 (Vladimir Agafonkin) | `static/leaflet.js`, `static/leaflet.css` | [BSD-2-Clause](https://github.com/Leaflet/Leaflet/blob/v1.9.4/LICENSE) |
| STM32CubeMX-generated init code (outside the `USER CODE` blocks), startup, linker scripts | `stm_detector3/Core`, `stm_detector3/*.ld` | STMicroelectronics, AS-IS per the file headers |

Everything not listed is Metal Mapper and under the GPL above, whatever
boilerplate header STM32CubeMX stamped on the file (`minmea_compat.h`, the
`USER CODE` blocks of `main.c`, and so on).

Map data: `street_outlines_2026.geojson` and `city_blocks_2026.geojson` are
Black Rock City datasets from the Burning Man Project, used under the
[Terms of Service for Burning Man APIs and Datasets](https://innovate.burningman.org/terms-of-service-for-burning-man-apis-and-datasets/).
Online base-map tiles are © OpenStreetMap contributors (ODbL).

The `v0.1`–`v0.4` release tags carry the previous season's files
(`camp_outlines_2025.geojson`, `street_outlines.geojson`,
`trash_fence.geojson`) under the same terms. `v0.1` and `v0.2` also carry the
retired STM32H723 prototypes (`stm_detector/`, `stm_detector2/`) and their
third-party code: STM32H7 HAL (BSD-3-Clause), STM32H7 CMSIS device headers
and CMSIS core (Apache-2.0), FreeRTOS (MIT), LwIP (BSD-3-Clause, per-file
headers), minmea (WTFPL), and ST's BSP drivers under `Drivers/BSP` —
BSD-3-Clause in the STM32CubeH7 package, though CubeIDE did not copy that
`LICENSE.md`, so their headers read AS-IS. ST's USB Host Library (SLA0044)
was removed from those snapshots.

The firmware comments cite ST's RM0440 reference manual (STM32G4) by
section; the public repository does not include that document — download it
from st.com.
