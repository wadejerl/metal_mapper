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

### `copy_study.py` — Duplicate a study under a new name

```bash
python copy_study.py SRC "New name" [--host TAG] [--studies-dir DIR] [--no-targets]
```

`SRC` is a study id or a path to its `.db`. The copy gets a fresh id in the
daemon's convention (`<name>_<epoch>__<host>`) and its listed label (meta
`study_name`) set to the new name; `created_at` still says when the data was
surveyed, and `copied_from` / `copied_at` record the provenance. The copy is an
SQLite online backup, so the `-wal` tail — present on every study the daemon
wrote, and growing on one it is writing now — comes across whole (a plain `cp`
of the `.db` drops it). Target sidecars travel with the study unless
`--no-targets`. `--host` stamps another unit's tag so a copy scp'd into that
unit's `studies/` is its own (editable, pushed by its sync) rather than a
read-only foreign study.

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
property of the vehicle mounting, so the unit-wide value lives in config;
edit the file and reload. A single saved study can be drawn with a
different value through the map view's **Cal** panel (kept per study as
`sl_cal_heading_deg`, see *Coil calibration*), and a study saved with its
own rotation (`sl_heading_offset_deg` in its meta, from before this moved
to config) keeps that value.

#### Study list page

- The two things a field operator does are big buttons at the very top:
  **▶ Start Recording** and **📈 View Raw** (which starts streaming
  immediately — no intermediate screen). Everything configurable lives at
  the bottom, below the studies.
- Shows all `.db` files in the studies directory, newest first, and which
  study (if any) the daemon is currently writing
- The per-study summaries (name, point count, legacy in-db pin count) are
  cached by file signature and the cache persists in
  `<studies_dir>/.study_index.json`, so the first page after a reboot does
  not re-count every study db on a cold SD card. Only a changed study is
  re-read; sidecar pins, liveness and the recorded-elsewhere badge are
  taken fresh on every render. (The map view's surveyed-area outlines keep a
  sibling cache, `.coverage.json`, on the same rule.)
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
| 🌐 Map / ✈ Offline | Base layer under the data. **Offline** (the default) is a plain grid plus the bundled geojson outlines — all a crew needs to find a pin, and nothing waits on the internet (a page stuck loading tiles over a thin link drags the nav marker and guidance along with it); **Map** pulls OpenStreetMap tiles and needs a working link. Per browser: a tap is remembered, the default is not, so a future default change reaches every unit |
| − Range slider | ADC counts below zero that map to full green (default 450, max 2000) |
| + Range slider | ADC counts above zero that map to full red (default 450, max 2000) |
| Offset slider | Shifts effective zero of all channels, in counts (default −140, range ±4000) |
| Cal | Coil calibration panel: the coil pitch, antenna 1's offset from the array centre (− left / + right) and the THS→travel heading offset this study is *drawn* with, over a live diagram of the array. Fix a setup error after the fact — the recorded data never changes. **reset to meta** returns to the stamped values. See *Coil calibration* below |
| ±ch | Per-channel bias: trims each coil's zero on top of the global Offset to balance coil-to-coil mismatch |
| 💾 Save View | Persists slider/zero/bias settings and any Cal departures into the study's meta table |
| Filter | `Δ` subtracts the array mean at each instant (common-mode rejection), grouped `2×4` — even and odd coils, the pairs the four ADCs convert together — or `all 8`; `sync` subtracts the study's average GPS-second-synchronous waveform (fold period ½/1/2 s, template 50/25/10 ms) — for the once-per-second interference in recordings made before the September 2026 firmware pacer fix |
| Mode | how overlapping passes resolve on screen: `paint` newest dots cover older; `combine` re-passes add; `mean` every sample on a spot is averaged (noise cancels, hot spots stand out); `peak` a spot keeps its greenest reading. See *Render modes* below |
| Channel strip chart (live) | scrolling graph of all 8 channels' deviation from their zero baselines (so the lines overlay instead of fanning out by DC offset), one shared scale, one line color per channel, newest samples at the right; current raw values listed vertically beside it, colored by the map's value color. Fed from the daemon's full-rate sample stream, so it scrolls with or without a GPS fix and whether or not anything is being recorded |
| Pins by size (saved studies) | takes the strip chart's slot when reviewing a saved study. `r` is the radius around a pin that counts toward its size; **⇅ Rank** sizes every pin of the shown set and lists them biggest first in a side panel (click a row to select and pan to it); **renumber** makes the numbers follow that order (reorders a set you own in place; otherwise saves a new set of yours); **save as set** saves the order under a new name; **✦ Find** proposes the `top N` biggest unmarked green spots as dashed cyan candidate pins — add one with its `+` (or click its ring), or **add all** to save them biggest-first so their numbers ARE the rank; **size pins** draws every pin scaled to its size. See *Pins by size* below |
| Pin set chooser / ☰ List | under Follow: which pin set this screen shows — every unit's saved sets, or *all pins* — display-only, remembered per study in the browser. The last entry, **＋ start new list**, asks for a name (pre-filled `list N`; blank lets the server name it `list N`, cancel does nothing), creates that empty list of yours and shows it: every other pin drops off the map until you pick *all pins* again, and pins you Mark or add from Find join it. **☰** opens the big-type pin list for the truck: one tap on a row selects that pin and starts the arrow-and-distance guidance. See *Pin sets* below |

**Coil calibration.** A points row holds the antenna fix and eight raw
counts; the coils are placed on the map at render time from three numbers
— so a setup error is fixable after the fact, without touching the data.
**Cal** (above **±ch**) opens them for the study on screen: the coil pitch,
where antenna 1 sits across the array (its offset from the array centre,
negative to the left/port, positive to the right/starboard; the current
rig reads −500, the antenna half a metre left of centre), and the heading
offset added to the receiver's THS heading (its antenna baseline) to get
the direction of travel. Each row shows the stamped value in brackets and
turns amber when it differs; a diagram above the rows moves with the
numbers, forward up, antenna 1 in orange. Edits redraw at once; the −/+
buttons step 10 mm or 1° and repeat, speeding up, while held. While the
geometry differs from the stamped values the status banner reads *Saved
study · alt coil cal* and the ⓘ Meta panel shows what the view is drawn
with under the stamped geometry. **Save View** stores the values you
touched with the study (`sl_cal_pitch_mm`, `sl_cal_ant_mm`,
`sl_cal_heading_deg`); **reset to meta** returns to the board's stamped
geometry and the heading offset the study drew with before any Cal
(config.json, or its own saved value), and a Save View after a reset
clears the stored overrides. Saved studies only — a recording is drawn
with the stamped values. Pins already dropped keep their lat/lon; re-run
Find if the carpet moved.

**Surveyed-area outlines.** Grey dashed outlines with a faint grey wash
inside mark the ground every *other* study on the unit has covered, so a
crew starting new territory sees at a glance where the team has already
been — none of the data itself, drawn under the recorded dots and pins and
never in the way of a tap. Each outline is the ground within 2.5 m of any recorded fix (1 m cells,
so edges are coarse; this is a "been here" mark, not a measurement),
computed once per study and cached by file signature in
`<studies_dir>/.coverage.json` — a season of studies is not re-scanned per
page load, and a restart redoes nothing. The viewed study and the study a
daemon is writing are left out. The outlines are fetched once, when a map
view opens, so a study synced in from another unit shows from the next
page load (starting a study, reopening a view) — not on a view already
open.

**Opening a view** shows a large *please wait* banner over the map for the
two slow steps, in order: *Rendering surveyed-area outlines* while a cold
`/coverage` computes every uncached study (seconds each on a Pi; instant
once cached), then *Loading map — N points* while a large study's points
load and paint. A step that finishes within a quarter second never shows
its banner, so a warm, small study opens as before. The messages are
progressive on purpose: if the page dies part-way, the last status (a
hang) or a red *… failed: reason* line (an error, including a non-2xx
reply) stays on screen and says where — a kiosk has no console to read.
An outline or points failure is noted and the rest of the page carries
on; a failed map load leaves the red line in place of *please wait*.

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

**Render modes (the Mode menu)**: in every mode but `paint`, overlapping
dots merge in the value domain instead of painting over each other, so an
earlier pass is never hidden or washed out by a later one — at every zoom
(a screen pixel at full detail, a 4 px bin when zoomed out).
`combine` (default): ground covered on multiple passes shows the *sum* of
what each visit saw — a target seen twice reads roughly twice as strong.
Overlap within a single visit doesn't add (a pixel keeps its extreme
value), so slow driving gains nothing. `mean`: every coil sample that lands
on a pixel is averaged, along-track overlap and repeat passes alike, so
uncorrelated noise cancels toward grey and only ground that reads green on
most of its samples stays green — the view for picking hot spots out of a
noisy carpet. `peak`: a pixel keeps the greenest reading it ever had —
nothing a single sample saw is averaged away (noisy, but nothing hides).
`paint`: classic painting, newest dots cover older ones.

**Pins by size (saved-study review)**: a pin's *size* is the number of
green coil samples within `r` of it over the whole study — green exactly
as the map paints it (filter-corrected deviation ≤ Offset, enabled channels
only), so tune the sliders and filters until the map shows the targets you
believe, then Rank. Every coil sample is binned on a 10 cm grid in local
metres (a 950k-point study scans in well under a second), and a size is a
window count over that grid, so two passes over one spot add up as they do
in `combine` mode. **Find** scores every occupied cell by its window count,
walks them largest-first and keeps a spot unless it is within 1 m (or 2·r)
of an existing pin — any unit's, found or not — or an earlier pick, then
refines each pick to the count-weighted centroid of its window so the pin
lands on the middle of the green. Candidates are not saved until added;
**add all** sends them in one request so the new ids run in size order.
Rank itself never renumbers — existing pins keep their numbers and the
rank is the list order; **renumber** applies that order to a set
(displayed positions change, the `host:N` ids/keys never do — see *Pin
sets* below). The side panel flags itself stale when the sliders or filters
change after a Rank; ↻ re-ranks.

**Pin sets (prioritised dig lists)**: a set is a named, ORDERED list of one
unit's pins, stored in that unit's sidecar (`sets` in
`<study>.targets__<host>.json`, format version 2). In a set a pin's
displayed number is its *position*, so renumbering is a reorder and never
touches the `host:N` keys that other units' marks point at; *all pins*
shows every pin under its id-based label as before. Any viewer can choose
any unit's set to display. **renumber** on a set you own rewrites its order
to the rank panel's order and saves at once; on *all pins* or another
unit's set it does what **save as set** does: creates a set of yours, in
that order, copying other units' pins into your sidecar. A copy sits at the
same spot and remembers its source (`from`); the two are ONE dig spot from
then on — every status flag is read across a pin, its source and every
copy, and written under the source's key, so a find ticked on the truck's
copy retires the base station's original (and vice versa). Saving the same
list again reuses your earlier copies rather than minting more, a copy of a
copy points at the original, another unit's copy of *your* pin resolves
back to your pin, and *all pins* shows the original in place of its copies.
Pins you Mark or add from Find while viewing a set you own join that set;
while another unit's set is shown they can't, so the view falls back to
*all pins* to show them. **＋ start new list** (last entry in the chooser)
asks for a name and creates an empty set of yours — `POST /targets/<id>/sets
{name?, keys: []}`, a missing name defaulting to `list N` — and switches to
it, so a crew arriving at a study full of other units' pins can suppress
them and build a fresh list by Marking or from Find without ranking
anything first. Each pin
carries three status flags —
**visited**, **found**, **skip** — that any unit may set on any pin (in its
own sidecar, like the found-mark always was; found *is* the legacy
`cleared` mark); a flag set by another unit shows dimmed and can only be
cleared there. Deleting a pin takes two taps within 3 s and remains limited
to pins in a file you own. Routes:
`POST /targets/<id>/status {key, visited|found|skip: bool}`,
`POST /targets/<id>/sets {name?, keys}`, `POST /targets/<id>/sets/<sid>
{name?, keys?, delete?}`; `GET /targets/<id>`, `/data` and the SSE tick
ship `sets` alongside `targets`.

**Nav lock on the arrow.** Pins sit where an RTK-fixed receiver said they
were, so the guidance arrow is only as good as the nav receiver's own lock
— a float or GPS-only fix walks the driver metres off the hole while the
arrow looks exactly the same. The approach panel therefore wears the lock
state on the arrow: the arrow is coloured by fix quality and a badge
directly under it reads **RTK FIXED** (green), **RTK FLOAT** (amber,
pulsing — RTK is working but not converged, wait), or red and pulsing for
**GPS · NO RTK** / **DGPS · NO RTK** (corrections not reaching the nav
receiver — check the `jlw_rover_rtk_nav` link), **NO FIX**, **NAV STALE**
(no fix for 5 s, or the page's stream went quiet) and **NAV DOWN**. The nav
marker on the map carries the same state: anything short of RTK fixed gets
a pulsing ring and the label under the marker. `MM.navFixStatus` in
`static/map_core.js` is the single classifier behind both.

**Toolbar (live sessions only)**: `Motion: NNN mm/pt` shows the distance
from the last saved point and reads PAUSED when stationary. The detector
timing controls (blanking, RX window, TX pulse, sample rate, Save Settings)
live on the View Raw page, below.

**Bulk load**: opening a map fetches `/data/<study_id>` (meta, status,
targets, sets, and `last_id`/`n_points`) and then the points themselves from
`/points/<study_id>?upto=<last_id>` as packed little-endian columns
(~50 B/point: uint32 id, float64 lat/lon/gps_ts, float32 heading, uint8 fix,
8× uint16 adc) that the browser views in place as typed arrays. Points used to
ride `/data` as JSON; serving a 950k-point study that way built a gigabyte of
Python objects, and the retained share of each reload ratcheted the worker
until the kernel OOM-killed it on a 2 GB rover Pi. The blob form peaks near
100 MB server-side and spares chromium a 100 MB `JSON.parse`.

**Incremental SSE**: SSE `/stream/<study_id>` then delivers only new rows
every 0.5 s, at most 4000 per tick — 40× the live rate, so the cap never
binds on a live study; a stream opened far behind (a stale tab, a hand-typed
`since_id`) catches up at ~8000 rows/s, about two minutes for the largest
study, instead of materialising the whole table in one tick. The heartbeat
event also carries motion status
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
mode, the render Mode, and the re-zeroed baseline are saved to the study's
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
from the git checkout, and a venv is created from `requirements.txt`. That
venv also carries `pygnssutils`: the `jlw_rover_*` units run its
`gnssserver` and `gnssntripclient` from `/opt/metal_mapper/venv/bin`, so
nothing is expected in the service user's home.
A `git pull` in the checkout therefore updates the app in place; restart
gunicorn afterwards (templates are cached at start). A missing
`templates/index.html` symlink is the classic 500 `TemplateNotFound` in
`/var/log/metal_mapper/error.log` — re-run the install script.

### `gunicorn_config.py`

```python
bind           = "127.0.0.1:8000"
chdir          = "/opt/metal_mapper"  # so db_map imports however gunicorn is launched
pythonpath     = "/opt/metal_mapper"
workers        = 1           # must be 1 — SSE and daemon state are process-local
worker_class   = "gthread"   # threads let SSE hold a connection while others flow
threads        = 8           # every open page holds one SSE stream
timeout        = 0           # disable worker timeout — SSE connections are long-lived
worker_tmp_dir = "/dev/shm"  # spare the SD card
graceful_timeout = 10        # = restart downtime (the kiosk's SSE never drains); bounds the longest surviving request
errorlog       = "/var/log/metal_mapper/error.log"
accesslog      = "/var/log/metal_mapper/access.log"
loglevel       = "info"
```

The config also carries a **worker memory guard**: after every request it
reads the worker's RSS from `/proc` and, past `MM_WORKER_RSS_MAX_MB`
(default 512, `0` disables), stops accepting and lets gunicorn's arbiter fork
a fresh worker — the `max_requests` lever, plus a hard exit after the drain
window because an SSE thread whose client never disconnects would otherwise
keep the old process alive with nobody serving. Open pages reconnect on their
own. It logs `recycling worker: rss ...` to `error.log`; with points served
as columns the worker should never get near it.

### nginx config

Checked in at `nginx/metal-mapper`. nginx is required: the install script
offers to `apt-get install` it when missing (in one go with everything else
a stock Raspberry Pi OS image lacks, see Setup) and stops if that is
declined. It symlinks the site into
`/etc/nginx/sites-enabled/` (and drops the stock default site, which
would otherwise shadow :80). The `location /stream/` block is what keeps
SSE alive through the proxy — buffering off, 24 h read timeout. The
`error_page` block is for the kiosk: its page never reloads itself, so
nginx's own 502 (backend down or still starting) or 504 (a first render
slower than the proxy timeout) would stay on the screen for good — instead
those get a self-retrying "starting…" page, inline so www-data needs no
access to the checkout. Errors the app itself returns pass through
untouched:

```nginx
upstream metal_mapper_app {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name _;

    error_page 502 503 504 @starting;
    location @starting {
        default_type text/html;
        return 503 '<!doctype html>...<meta http-equiv="refresh" content="3">...';
    }

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
user (whoever ran `sudo`), the checkout path and the RTK base host
(`northpole` unless `MM_BASE_HOST` in the gitignored `install.conf` says
otherwise — see `install.conf.example`). No account names or paths live in
the unit templates (the base Pi's `rtkbase/` units are deployed by hand and
assume an `rtk` account).
After a `git pull` that touches a template, re-run the installer: it
re-renders, daemon-reloads, and is idempotent.

Boot order: `jlw_ui_mm` is ordered `After=` `jlw_metalmap` and nginx
(`Wants=`, so a backend that never comes up still gets a screen — nginx's
retrying page, below, says more than black). That ordering is only as good
as what "started" means for `jlw_metalmap`: it is `Type=notify`, but
gunicorn's READY=1 goes out when its socket is bound, *before* the worker
that imports the app exists — so on its own, "active" meant "accepting
connections nobody answers yet", and the kiosk loaded into a request that
sat until nginx gave up (a white page, then nginx's error page). The unit
therefore adds an `ExecStartPost=` loop on `curl http://127.0.0.1:8000/healthz`
(a route that touches nothing slow) which holds it in *activating* until
the worker answers, bounded by `TimeoutStartSec=5min` (then the unit fails
and `Restart=` retries). `start_local_ui.sh` then waits up to a minute for
`http://localhost/` itself — the first landing render, which also warms
the study index — one probe at a time, each allowed the rest of the
window, so a slow first render is waited out rather than abandoned and
re-issued; concurrent renders queue behind one scan in the app. Behind all
that, nginx answers a 502/503/504 of its own with a page that retries
every 3 s (see *nginx config*): a kiosk page never reloads itself, so
chromium that raced the backend at boot used to sit on nginx's error page
until someone restarted it. `curl localhost/healthz` on a unit answers
`{"ok": true, "git": "<hash>"}` — a quick check of what a deploy is running.

The kiosk (`jlw_ui_mm.service` → `start_local_ui.sh`) is chromium in
`--kiosk` mode on the Pi's own display, run as the service user on
`DISPLAY=:0` — so that user has to be logged into a graphical session at
boot. On a Desktop image that is raspi-config's Desktop Autologin (the
installer checks the autologin user and warns if it is someone else). On a
Lite image the installer's package gate adds a bare X login for it —
`lightdm`, `xserver-xorg`, `x11-xserver-utils`, `openbox` — and points
lightdm at the service user via `/etc/lightdm/lightdm.conf.d/50-metal-mapper.conf`
and sets the graphical default target. The launcher finds the browser as
`chromium` (Trixie/Bookworm) or `chromium-browser` (older images) and turns
screen blanking off. The landing page heading shows the unit's short
hostname, so two Pis are told apart at a glance.

Upgrading a Pi installed before September 2026, when the units were
symlinked into `/etc/systemd/system`: run the installer right after the pull
that brought the templates, before any `systemctl daemon-reload` or reboot.
The files those links pointed at were renamed to `*.in`, so the links dangle
until the installer replaces them with rendered files. A plain `systemctl
restart` still works meanwhile (systemd keeps the loaded unit), but a reload
or reboot before the installer runs leaves every `jlw_*` unit `not-found`.

Pis set up before late September 2026 ran `gnssserver`/`gnssntripclient`
from a hand-made `~/pygpsclient` venv. The units now run them from
`/opt/metal_mapper/venv`, which the installer fills from `requirements.txt`:
after the pull, re-run the installer with the package index reachable, then
`sudo systemctl restart jlw_rover_outfeed_nav` and `sudo systemctl restart
--no-block jlw_rover_rtk jlw_rover_rtk_nav`. `~/pygpsclient` can go after that.

The two `jlw_rover_rtk*` units gate their start on the GPS tty existing
and the base station's caster answering (steady amber dot on the home
screen while waiting, instead of the old green/grey restart flapping).
The wait is unbounded, so a plain foreground `systemctl start`/`restart`
of them blocks until the gate passes — add `--no-block` when the base or
GPS may be absent.

All four `jlw_rover_*` units carry `ConditionPathExists=` on the UART they
talk to (`/dev/ttyUSB0` for the nav GPS, `/dev/ttyUSB3` for the heading
GPS). A head unit without that GPS at boot — a bench Pi, a GPS left
unplugged — skips the unit (`inactive`, dot off on the home screen)
instead of failing it (gnssserver relaunching into systemd's start-rate
limit: a red dot for good, and boot-time churn under the kiosk's first
render) or gating forever. The condition is checked at boot and on a
manual start only; systemd does not re-check it on `Restart=` relaunches,
so a USB re-enumeration mid-survey behaves as before. A GPS plugged in
after boot needs `sudo systemctl start jlw_rover_outfeed_nav` (or a
reboot). `systemctl status` on a skipped unit says "Condition check
resulted in ... being skipped".

### Setup commands

```bash
# Optional: override a default (service user, RTK base host)
# cp install.conf.example install.conf && nano install.conf

# One script from the repo checkout sets up the system. It first offers ONE
# apt-get for whatever a stock Raspberry Pi OS (Trixie) image lacks — nginx,
# chromium, rsync, python3-venv, git, and on a Lite image lightdm/X/openbox
# for the kiosk login — and stops if that is declined. Then it renders +
# enables every unit template in the repo root, builds /opt/metal_mapper
# (app symlinks + venv from requirements.txt), creates the gunicorn log dir,
# puts the service user in dialout for the detector's serial port, and
# links the nginx site. Idempotent — re-run after a pull that touches a
# unit template, an overlay, or a dependency.
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
Online base-map tiles (the optional **Map** layer; the default is offline) are © OpenStreetMap contributors (ODbL).

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
