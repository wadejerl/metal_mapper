# Metal Mapper

A field instrument system for GPS-tagged pulse-induction metal detection. The STM32H723
embedded firmware acquires ADC samples and streams them over USB serial; a Raspberry Pi
runs a Python daemon that records detections into SQLite studies, and a Flask/Leaflet web
front-end lets you browse saved studies or watch a live survey in real time.

---

## System Architecture

```
STM32H723 Nucleo-144
  └─ USART3 @ 115200 → USB CDC (VCP)
        └─ serial_daemon.py  (Pi / dev machine)
              ├─ ~/metal_mapper/studies/<name>.db  (SQLite, WAL mode)
              └─ /tmp/metal_detector_cmd.fifo  (command back-channel)
                    └─ db_map.py  (Flask + Leaflet web UI)
                          └─ nginx reverse proxy (Pi deployment)
```

### CSV data format from STM32

```
lat,lon,fix_quality,adc_raw,gps_timestamp
40.786213,-119.204512,2,12460,1.050000
```

- `fix_quality`: 0 = no fix, 2 = 2D, 3 = 3D, 4 = RTK fixed, 5 = RTK float
- `adc_raw`: 16-bit unsigned; hardware sum of 16 oversampled 12-bit conversions
  (0–65520). The web UI colors in raw counts — nothing is normalized.
- Status lines prefixed with `#` carry blanking/window echoes: `# blanking=16us rx_window=3us`

---

## Scripts

### `serial_daemon.py` — Data collection daemon

Connects to the STM32 serial port, filters points by minimum spacing, and writes
detections into a SQLite study file. A FIFO at `/tmp/metal_detector_cmd.fifo` lets
the web UI send single-byte commands back to the STM32 (timing adjustment keys).

```bash
python serial_daemon.py \
  --port /dev/cu.usbmodem1103 \
  --db ~/metal_mapper/studies/myrun.db \
  [--baud 115200]          # default 115200 — matches STM32 USART3
  [--min-dist 20]          # mm between saved points; default 20
  [--study-name "My run"]  # human label stored in meta table
  [--debug]                # print each received line + insert confirmations
```

**Distance gating**: every received point is compared against the last *saved* point.
If the straight-line distance (Haversine) is below `--min-dist`, the point is dropped.
This prevents duplicate points when the detector is stationary. The current distance
and paused/moving state are written to the study's `meta` table at ~2 Hz so the web
UI can display them live.

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

- Shows all `.db` files in the studies directory, newest first
- Indicates which study (if any) the daemon is currently writing
- **Start New Study** form: study name (pre-filled `geo_YYYYMMDD_HHMMSS`), serial port
  (remembered from last session), and minimum distance in mm

#### Map view page

**Toolbar controls (always available):**

| Control | What it does |
|---------|-------------|
| ← Studies | Back to study list |
| Study name | Click to rename (Enter saves, Esc cancels) — changes the label shown everywhere; the `.db` filename and URL stay the same |
| ⓘ Meta | Collapsible panel of the study's acquisition provenance: recording setup (gate, min distance, serial port), firmware build, fire mode, ADC oversample and channel order, GPS id/version, PI timing, and coil geometry. Values that drifted mid-study show `stamped → current` and an incomplete header is flagged |
| 🎯 Re-zero | Set per-channel zero baselines from average of last 20 points |
| ⏸ Pause / ▶ Resume | Freeze map pan/add; data still streams |
| − Range slider | ADC counts below zero that map to full green (default 450, max 2000) |
| + Range slider | ADC counts above zero that map to full red (default 450, max 2000) |
| Offset slider | Shifts effective zero of all channels, in counts (default −140, range ±2000) |
| 💾 Save View | Persists slider/zero settings into the study's meta table |
| Filter | `absolute` (raw counts vs zero) or `Δ array mean` (common-mode rejection) |
| Combine | on: overlapping passes accumulate (re-pass adds); off: newest dots cover older |
| Channel strip chart | scrolling graph of all 8 channels' deviation from their zero baselines (so the lines overlay instead of fanning out by DC offset), one shared scale, one line color per channel, newest samples at the right; current raw values listed vertically beside it, colored by the map's value color |

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

**Toolbar controls (live sessions only):**

| Control | What it does |
|---------|-------------|
| Motion: NNN mm/pt | Distance from last saved point; shows PAUSED when stationary |
| Blanking ±2µs | Send `a`/`z` key to STM32 via FIFO |
| RX Window ±1µs | Send `s`/`x` key to STM32 via FIFO |
| 💾 Save Settings | Send `S` key — saves timing to STM32 internal flash |

**Incremental SSE**: initial load fetches all points; SSE `/stream/<study_id>` then
delivers only new rows every 0.5 s. The heartbeat event also carries motion status
so the display updates even when no new points are being saved.

**Per-study slider persistence**: `+ Range`, `− Range`, `Offset`, the filter
mode, the Combine switch, and the re-zeroed baseline are saved to the study's
`meta` table when you click 💾 Save View, and restored automatically the next
time you open that study.

---

## Pi Deployment

### Files to deploy

Copy to `/opt/metal_mapper/` on the Pi:
- `db_map.py`
- `serial_daemon.py`
- `nav_gps.py`
- `gunicorn_config.py`
- `templates/` (whole directory — a missing `templates/index.html` is the
  classic 500 `TemplateNotFound` in `/var/log/metal_mapper/error.log`)
- `static/` (whole directory — vendored Leaflet + `map_core.js`; no CDN)
- `camp_outlines_2025.geojson` (optional GeoJSON overlay)

### `gunicorn_config.py`

```python
bind           = "127.0.0.1:8000"
workers        = 1           # must be 1 — SSE and daemon state are process-local
worker_class   = "gthread"   # threads let SSE hold a connection while others flow
threads        = 4
timeout        = 0           # disable worker timeout — SSE connections are long-lived
worker_tmp_dir = "/dev/shm"  # spare the SD card
errorlog       = "/var/log/metal_mapper/error.log"
accesslog      = "/var/log/metal_mapper/access.log"
loglevel       = "info"
```

### nginx config (`/etc/nginx/sites-enabled/metal_mapper`)

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

### systemd service (`/etc/systemd/system/metal_mapper.service`)

```ini
[Unit]
Description=Metal Mapper Web UI
After=network.target

[Service]
User=pi
Group=pi
WorkingDirectory=/opt/metal_mapper
Environment="PATH=/opt/metal_mapper/venv/bin"
ExecStart=/opt/metal_mapper/venv/bin/gunicorn \
    --config /opt/metal_mapper/gunicorn_config.py \
    db_map:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Setup commands

```bash
# Serial port access (inherit by daemon subprocess)
sudo usermod -a -G dialout pi

# Log directory
sudo mkdir -p /var/log/metal_mapper && sudo chown pi:pi /var/log/metal_mapper

# Venv and dependencies
cd /opt/metal_mapper
python3 -m venv venv
venv/bin/pip install flask pyserial gunicorn

# Service
sudo systemctl daemon-reload
sudo systemctl enable --now metal_mapper

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

## STM32 Firmware (`stm_detector2/`)

MCU: STM32H723ZGTX Nucleo-144. FreeRTOS, HAL, CubeMX. See
[`stm_detector2/CLAUDE.md`](stm_detector2/CLAUDE.md) for the full PI timing chain
documentation.

**Key facts:**
- PI 3-ISR chain: TIM5 CC1 → TIM1 UIE → ADC EOC
- USART3 @ 115200 baud → ST-LINK VCP → USB
- USART2 @ 460800 baud → GPS module (not the host)
- Runtime timing keys: `a`/`z` = blanking ±2µs, `s`/`x` = RX window ±1µs
- `S` = save timing to internal flash (sector 7, 0x080E0000)
- ADC: 16× hardware oversampling with right-shift-4
- GPS bypassed in current development build (fixed coords injected)

---

## Legacy Scripts

These remain in the repo but are superseded by the two-script architecture above:

- **`map_serial_stream.py`** — original all-in-one Flask serial mapper (no persistence)
- **`plot_serial_stream.py`** — PyQtGraph dual-axis timing plotter (still useful)
- **`test_data_generator.py`** — simulated GPS data for offline testing
