#!/usr/bin/env python3
"""
serial_daemon.py — Metal Mapper serial collection daemon (detector3 protocol).

Connects to the STM32 serial port, reads detector3 CSV data into a SQLite3
study file, and forwards command bytes from a named FIFO back to the device.
Protocol reference: docs/detector3.md.

Data lines (13 fields, 15 when the 1 Hz vin/temp pair is appended):
    lat,lon,fix,adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,heading[,vin,temp]
- heading is empty (line ends with ',') unless the dual-antenna solution is
  good — never stale, never guessed.
- Idle lines (no valid fix for 500 ms) have lat/lon/fix/gps_ts empty, live
  ADC values, and ALWAYS carry the vin/temp pair (so they are 15 fields).

Recording gate: points are INSERTed only when fix == 4 (RTK fixed) AND a
heading is present (plus the min-distance motion filter). --no-gate relaxes
this to any valid position, for bench work.

The single-row `live` table is UPDATEd in place at ~2 Hz in every mode
(recording, degraded, idle, no data, no port) so the DB stays the comms
link between hardware and UI without growing while not recording.

Usage:
    python serial_daemon.py --port /dev/ttyACM0 --db ~/metal_mapper/studies/foo.db
                            [--baud 460800] [--study-name "Back yard test"]
                            [--min-dist 20] [--no-gate] [--debug]
"""

import argparse
import math
import os
import re
import select
import signal
import sqlite3
import stat
import sys
import time
import threading

import serial
from serial.tools import list_ports


def latlon_dist_mm(lat1, lon1, lat2, lon2):
    """Flat-earth distance in millimetres — good to < 0.1 % for distances under 1 km."""
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) * 0.5))
    return math.sqrt(dlat * dlat + dlon * dlon) * R * 1000.0

FIFO_PATH = '/tmp/metal_detector_cmd.fifo'
PID_FILE  = '/tmp/metal_detector_daemon.pid'

SCHEMA_VERSION  = '3'
LIVE_PERIOD_S   = 0.5   # min interval between live-row writes
NO_DATA_AFTER_S = 2.0   # silence longer than this → reason 'no data'
                        # (firmware emits at least every 500 ms, even indoors)

# Timing values in '#' lines: the keypress echo "# blanking=16us  rx_window=3us",
# but also the boot pulse banner and settings-loaded line (seen if the MCU
# reboots mid-study — exactly the case worth flagging).
RE_TIMING = re.compile(r'blanking=(\d+)us\s+rx_window=(\d+)us')
# TX pulse (coil charge time) in '#' lines: the f/v keypress echo
# "# tx_pulse=90us", plus the boot pulse banner and settings-loaded/saved
# lines. It scales the coil charge energy — every ADC value depends on it.
RE_TXPULSE = re.compile(r'tx_pulse=(\d+)us')
# Coil-spacing value in '#' lines: the d/c keypress echo "# coil_spacing=510mm"
# and the settings-loaded/saved banners. Recorded data depends on geometry,
# so a mid-study change must be flagged just like a timing change.
RE_COIL = re.compile(r'coil_spacing=(\d+)mm')
# Fire mode in '#' lines: the 1/2/3 keypress echo "# fire_mode=odd" and the
# settings banners. Gates which TX set(s) strobe (odd=A/PA5, even=B/PB4);
# adjacent-coil coupling changes with it, so ADC values before/after a
# mid-study switch are not comparable.
RE_FIRE = re.compile(r'fire_mode=(odd|even|both)')
# Info line: "# info fw=... gps_ver=... key=value ..."
RE_INFO_KV = re.compile(r'(\w+)=(\S+)')

# Fields the info line contributes to the study header, in stamp order.
# 'fw' and 'adc' are renamed for clarity in meta.
INFO_META_KEYS = {
    'fw':                   'fw_git_hash',
    'gps_ver':              'gps_ver',
    'gps_id':               'gps_id',
    'blanking_us':          'blanking_us',
    'rx_window_us':         'rx_window_us',
    'tx_pulse_us':          'tx_pulse_us',
    'coil_spacing_mm':      'coil_spacing_mm',
    'coil_offset_fore_mm':  'coil_offset_fore_mm',
    'coil_offset_right_mm': 'coil_offset_right_mm',
    'fire_mode':            'fire_mode',        # which TX set(s) strobe: odd/even/both
    'adc_oversample':       'adc_oversample',   # ADC counts are sums of this many conversions
    'adc':                  'adc_order',
}


# ── Line parsing ──────────────────────────────────────────────────────────────

def parse_data_line(line):
    """Parse one detector3 CSV data line.

    Returns a dict with keys lat/lon/fix/gps_ts (all None on idle lines),
    adc (list of 8 ints), heading (None when absent), vin/temp (None unless
    the line carried the 1 Hz status pair) — or None if the line is not a
    well-formed data line (wrong field count, partial GPS fields, garbage
    values). At ~20 Hz, dropping a corrupt line is always the right call.
    """
    parts = line.split(',')
    if len(parts) not in (13, 15):
        return None

    # lat, lon, fix, gps_ts are all-present (positioned) or all-empty (idle).
    # A mix means the line was corrupted in transit.
    pos_fields = (parts[0], parts[1], parts[2], parts[11])
    present = [p != '' for p in pos_fields]
    if any(present) != all(present):
        return None

    try:
        if all(present):
            lat, lon, fix = float(parts[0]), float(parts[1]), int(parts[2])
            gps_ts = parts[11]
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
                    and 0 <= fix <= 9):
                return None
        else:
            lat = lon = fix = gps_ts = None

        adc = [int(p) for p in parts[3:11]]
        if any(not (0 <= a <= 65535) for a in adc):
            return None

        heading = None
        if parts[12] != '':
            heading = float(parts[12])
            if not (0.0 <= heading <= 360.0):
                return None

        vin = temp = None
        if len(parts) == 15:
            vin, temp = float(parts[13]), float(parts[14])
    except ValueError:
        return None

    return {'lat': lat, 'lon': lon, 'fix': fix, 'gps_ts': gps_ts,
            'adc': adc, 'heading': heading, 'vin': vin, 'temp': temp}


def parse_info_line(line):
    """Parse '# info key=value ...' into a raw dict, or None."""
    s = line.lstrip('#').strip()
    if not s.startswith('info'):
        return None
    return dict(RE_INFO_KV.findall(s[4:]))


def gate_reason(point, no_gate):
    """Why this point is NOT recordable ('' = gate passed)."""
    if point['lat'] is None:
        return 'no fix'
    if no_gate:
        return ''
    if point['fix'] != 4:
        return f"fix={point['fix']}"
    if point['heading'] is None:
        return 'no heading'
    return ''


# ── Database ──────────────────────────────────────────────────────────────────

def db_open(path):
    conn = sqlite3.connect(path)
    # WAL mode lets Flask read the db concurrently while we write.
    conn.execute('PRAGMA journal_mode=WAL')
    # NORMAL in WAL fsyncs only at checkpoints, not per-commit — a power cut
    # loses at most the last few commits, never corrupts. The default FULL
    # fsyncs every commit (~26/s while recording), and SD-card wear-leveling
    # stalls would back the read loop up behind the 460800-baud stream.
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS points (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL    NOT NULL,
            lat     REAL    NOT NULL,
            lon     REAL    NOT NULL,
            heading REAL,
            fix     INTEGER NOT NULL,
            adc0 INTEGER NOT NULL, adc1 INTEGER NOT NULL,
            adc2 INTEGER NOT NULL, adc3 INTEGER NOT NULL,
            adc4 INTEGER NOT NULL, adc5 INTEGER NOT NULL,
            adc6 INTEGER NOT NULL, adc7 INTEGER NOT NULL,
            gps_ts  TEXT,
            vin     REAL,
            temp    REAL
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')
    # Single-row live state — REPLACEd in place, so the file never grows
    # while idle/degraded/stationary. updated_at is the daemon heartbeat;
    # data_at is when the last data line arrived (NULL until the first one).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS live (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at REAL    NOT NULL,
            data_at    REAL,
            lat REAL, lon REAL, fix INTEGER, heading REAL,
            adc0 INTEGER, adc1 INTEGER, adc2 INTEGER, adc3 INTEGER,
            adc4 INTEGER, adc5 INTEGER, adc6 INTEGER, adc7 INTEGER,
            vin REAL, temp REAL,
            gps_ts    TEXT,
            recording INTEGER NOT NULL,
            reason    TEXT    NOT NULL
        )''')
    conn.commit()
    return conn


def meta_set(conn, key, val):
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(val)))
    conn.commit()


def meta_get(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def meta_setdefault(conn, key, val):
    """Stamp-once: a daemon restart on an existing study must not rewrite
    its provenance (created_at, study_name)."""
    if meta_get(conn, key) is None:
        meta_set(conn, key, val)


def motion_set(conn, dist_mm, paused):
    """Both motion keys in one transaction (one commit, not two)."""
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                 ('motion_mm', f'{dist_mm:.0f}' if dist_mm is not None else '0'))
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                 ('motion_paused', '1' if paused else '0'))
    conn.commit()


_last_db_err_t = 0.0

def db_guard(fn, *args):
    """Run a DB write; a sqlite error must not tear down the serial session.

    Disk-full or a lock collision with the web UI is a storage problem —
    reconnecting the port (and burning seconds on a re-handshake) doesn't
    fix it and loses good data. Log at most every 10 s, and guard the log
    write itself: when the disk is full, print-to-logfile fails too.
    Returns True on success so callers can skip success-only state updates.
    """
    global _last_db_err_t
    try:
        fn(*args)
        return True
    except sqlite3.Error as e:
        now = time.time()
        if now - _last_db_err_t >= 10.0:
            _last_db_err_t = now
            try:
                print(f'[daemon] DB error (continuing): {e}', flush=True)
            except OSError:
                pass
        return False


def live_write(conn, point, recording, reason, data_at, vin, temp):
    """REPLACE the single live row. point may be None (no data / no port)."""
    p = point or {}
    adc = p.get('adc') or [None] * 8
    conn.execute(
        'INSERT OR REPLACE INTO live VALUES (1,?,?,?,?,?,?,'
        '?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (time.time(), data_at,
         p.get('lat'), p.get('lon'), p.get('fix'), p.get('heading'),
         *adc, vin, temp, p.get('gps_ts'),
         1 if recording else 0, reason))
    conn.commit()


def insert_point(conn, point):
    conn.execute(
        'INSERT INTO points (ts,lat,lon,heading,fix,'
        'adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,vin,temp) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (time.time(), point['lat'], point['lon'], point['heading'],
         point['fix'], *point['adc'], point['gps_ts'],
         point['vin'], point['temp']))
    conn.commit()


TIMING_KEYS   = ('blanking_us', 'rx_window_us', 'tx_pulse_us')
GEOMETRY_KEYS = ('coil_spacing_mm', 'coil_offset_fore_mm', 'coil_offset_right_mm')
FIRE_KEYS     = ('fire_mode',)


def stamp_info(conn, info, debug=False):
    """Stamp the study header from a parsed info line.

    First-wins PER KEY: a truncated first reply (MCU rebooting mid-line)
    must not lock the header incomplete — later info lines fill whatever
    is still missing, but never overwrite a stamped value. info_ok stays
    '0' until every header key is present. Changed timing/geometry on a
    later info line is flagged, not applied — a retune means a new study
    by convention, so a mid-study change must be visible, not silent.
    """
    first = meta_get(conn, 'fw_git_hash') is None
    for src, dst in INFO_META_KEYS.items():
        if src in info and meta_get(conn, dst) is None:
            meta_set(conn, dst, info[src])
    complete = all(meta_get(conn, dst) is not None
                   for dst in INFO_META_KEYS.values())
    meta_set(conn, 'info_ok', '1' if complete else '0')
    meta_set(conn, 'info_at', f'{time.time():.1f}')
    if first:
        print(f'[daemon] study header stamped: fw={info.get("fw", "?")} '
              f'gps_id={info.get("gps_id", "?")} '
              f'coil_spacing_mm={info.get("coil_spacing_mm", "?")}'
              + ('' if complete else '  (INCOMPLETE — awaiting next info line)'),
              flush=True)
    note_drift(conn, 'timing',   {k: info[k] for k in TIMING_KEYS   if k in info})
    note_drift(conn, 'geometry', {k: info[k] for k in GEOMETRY_KEYS if k in info})
    note_drift(conn, 'fire',     {k: info[k] for k in FIRE_KEYS     if k in info})


def note_drift(conn, kind, current):
    """Track live values of header keys; flag (never apply) a change.

    current: {header_key: value-as-string}. Writes <key>_current for the
    UI and sets <kind>_changed once a value diverges from the stamped
    header. Recorded data depends on these — geometry most of all, since
    the visualizer computes all 8 coil positions from the header — so a
    silent mid-study change is the failure mode to avoid.

    The flag lists ALL of the kind's diverged keys (recomputed from the
    _current values), not just this call's: echoes arrive one key at a
    time (a/z vs f/v), and a later echo must not erase an earlier drift
    from the flag.
    """
    if not current:
        return
    for key, val in current.items():
        meta_set(conn, key + '_current', str(val))
    diffs = []
    for key in {'timing': TIMING_KEYS, 'geometry': GEOMETRY_KEYS,
                'fire': FIRE_KEYS}[kind]:
        cur     = meta_get(conn, key + '_current')
        stamped = meta_get(conn, key)
        if cur is not None and stamped is not None and cur != stamped:
            diffs.append(f'{key} {stamped}->{cur}')
    if diffs:
        flag = kind + '_changed'
        if meta_get(conn, flag) is None:
            print(f'[daemon] WARNING: {kind} changed mid-study '
                  f'({", ".join(diffs)}) — convention is a new study per '
                  f'retune', flush=True)
        meta_set(conn, flag, f'{time.time():.0f}:' + ','.join(diffs))


# ── PID file ──────────────────────────────────────────────────────────────────

def write_pid(db_path):
    with open(PID_FILE, 'w') as f:
        f.write(f'{os.getpid()}:{os.path.abspath(db_path)}')


def claim_pid_file(db_path):
    """Refuse to start if a live daemon already owns the PID file.

    db_map's check-then-spawn has a ~0.5 s window where a double-clicked
    Start button launches two daemons; without this, the loser overwrites
    the winner's PID file and both read the same tty, shredding the byte
    stream between them. The exclusive port lock backstops the race that
    remains. A stale file from a dead pid is reclaimed silently.
    """
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().split(':', 1)[0])
    except (FileNotFoundError, ValueError):
        pid = None
    if pid is not None and pid != os.getpid():
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True   # exists, different user — still alive
        if alive:
            print(f'[daemon] ERROR: another daemon (pid {pid}) is already '
                  f'running per {PID_FILE} — refusing to start', flush=True)
            sys.exit(1)
    write_pid(db_path)


def cleanup_pid():
    """Remove the PID file only if this process owns it — a refused second
    daemon exiting must not delete the rightful owner's file."""
    try:
        with open(PID_FILE) as f:
            if int(f.read().split(':', 1)[0]) != os.getpid():
                return
        os.unlink(PID_FILE)
    except (FileNotFoundError, ValueError):
        pass


# ── Command FIFO ──────────────────────────────────────────────────────────────

def ensure_fifo():
    """Create /tmp/metal_detector_cmd.fifo if it doesn't exist (or recreate if
    a regular file is in the way)."""
    if os.path.exists(FIFO_PATH):
        if not stat.S_ISFIFO(os.stat(FIFO_PATH).st_mode):
            os.unlink(FIFO_PATH)
            os.mkfifo(FIFO_PATH)
    else:
        os.mkfifo(FIFO_PATH)


def start_fifo_thread(ser_ref, lock):
    """Background thread: forward bytes from the FIFO to the serial port.

    The FIFO is opened with O_RDWR so this process permanently holds the read
    end open.  That means Flask can open the write end (O_WRONLY|O_NONBLOCK)
    at any time without blocking, and we never see a spurious EOF when Flask
    closes its end between commands.
    """
    fd = os.open(FIFO_PATH, os.O_RDWR | os.O_NONBLOCK)

    def _run():
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    data = os.read(fd, 64)
                    if data:
                        with lock:
                            s = ser_ref[0]
                            if s and s.is_open:
                                s.write(data)
            except Exception:
                time.sleep(0.1)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ── Handshake ─────────────────────────────────────────────────────────────────

def request_info(ser, ser_lock, debug=False, tries=3, wait_s=2.0):
    """Send 'I' and wait for the '# info ...' reply.

    The STM32 boots long before this daemon connects, so the boot banner is
    long gone — the info line must be requested. Data lines that arrive
    while waiting are discarded (a couple of seconds at worst, at connect).
    Returns the parsed info dict, or None if the device never answered.
    """
    for attempt in range(1, tries + 1):
        with ser_lock:
            ser.write(b'I')
        deadline = time.time() + wait_s
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode('ascii', errors='replace').strip()
            if debug and line:
                print(f'[daemon] handshake rx: {line!r}', flush=True)
            info = parse_info_line(line) if line.startswith('#') else None
            if info:
                return info
        print(f'[daemon] no info reply (attempt {attempt}/{tries})', flush=True)
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Metal Mapper serial daemon (detector3)')
    ap.add_argument('--port',        required=True, help='Serial port, e.g. /dev/ttyACM0')
    ap.add_argument('--db',          required=True, help='Output SQLite3 file path')
    ap.add_argument('--baud',        type=int, default=460800)
    ap.add_argument('--study-name',  default='', help='Human-readable study label')
    ap.add_argument('--min-dist',    type=float, default=20.0,
                    metavar='MM',
                    help='Minimum distance in mm between saved points (default 20)')
    ap.add_argument('--no-gate',     action='store_true',
                    help='Record any valid position (default: require RTK '
                         'fixed + heading) — bench use')
    ap.add_argument('--debug',       action='store_true',
                    help='Print each received line to stdout (useful for troubleshooting)')
    args = ap.parse_args()

    db_path = os.path.expanduser(args.db)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    claim_pid_file(db_path)
    ensure_fifo()

    conn = db_open(db_path)
    name = args.study_name or os.path.splitext(os.path.basename(db_path))[0]
    meta_setdefault(conn, 'study_name', name)
    meta_setdefault(conn, 'created_at', str(time.time()))
    meta_set(conn, 'serial_port',    args.port)
    meta_set(conn, 'schema_version', SCHEMA_VERSION)
    meta_set(conn, 'min_dist_mm',    f'{args.min_dist:.0f}')
    meta_set(conn, 'gate',           'none' if args.no_gate else 'rtk+heading')

    def on_exit(sig, frame):
        cleanup_pid()
        try:
            conn.close()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_exit)
    signal.signal(signal.SIGINT,  on_exit)

    ser_ref = [None]
    lock    = threading.Lock()
    start_fifo_thread(ser_ref, lock)

    print(f'[daemon] pid={os.getpid()}  db={db_path}  port={args.port}'
          f'  min_dist={args.min_dist:.0f}mm'
          f'  gate={"OFF" if args.no_gate else "rtk+heading"}', flush=True)

    last_saved     = None   # (lat, lon) of last inserted point
    last_motion_t  = 0.0    # last time motion meta was written
    last_live_t    = 0.0    # last time the live row was written
    last_data_t    = None   # wall time of last parsed data line
    last_vin       = None   # status pair arrives at 1 Hz; carried between lines
    last_temp      = None
    port_error_announced = False

    # Live row exists from the first moment, so the UI never reads an
    # empty table while the port is still coming up.
    live_write(conn, None, False, 'no port', None, None, None)

    while True:
        # ── Connect (or reconnect) to serial port ────────────────────────────
        try:
            with lock:
                # exclusive: a second reader on the same tty (another daemon,
                # ModemManager probing) silently splits the byte stream —
                # make it fail loudly here instead.
                ser_ref[0] = serial.Serial(args.port, args.baud, timeout=1,
                                           exclusive=True)
            ser = ser_ref[0]
            port_error_announced = False
            print(f'[daemon] connected to {args.port}', flush=True)

            ser.reset_input_buffer()
            info = request_info(ser, lock, debug=args.debug)
            if info:
                db_guard(stamp_info, conn, info, args.debug)
            else:
                print(f'[daemon] ERROR: device did not answer the I '
                      f'(info) request — port open but no detector3 '
                      f'firmware talking?', flush=True)
                if meta_get(conn, 'info_ok') is None:
                    meta_set(conn, 'info_ok', '0')

            conn_t     = time.time()  # staleness grace starts at connect
            last_raw_t = None         # last time ANY bytes arrived

            # ── Read loop ────────────────────────────────────────────────────
            while True:
                raw = ser.readline()
                now = time.time()

                # Staleness check on EVERY iteration, not just read timeout:
                # a wrong port, wrong baud, or wedged firmware can deliver
                # bytes forever without one line ever parsing, and the live
                # row must say so rather than freeze at its last state. The
                # firmware emits a parseable line at least every 500 ms, so
                # fresh data always resets this. 'garbage' = bytes arriving
                # but nothing parses; 'no data' = wire silent.
                fresh_ref = max(last_data_t or 0.0, conn_t)
                if (now - fresh_ref > NO_DATA_AFTER_S
                        and now - last_live_t >= LIVE_PERIOD_S):
                    flowing = last_raw_t is not None and now - last_raw_t <= NO_DATA_AFTER_S
                    db_guard(live_write, conn, None, False,
                             'garbage' if flowing else 'no data',
                             last_data_t, last_vin, last_temp)
                    last_live_t = now

                if not raw:
                    continue
                last_raw_t = now

                # errors='replace', NOT 'ignore': deleting a noise-corrupted
                # byte can splice a damaged field into a shorter but valid-
                # looking value that passes range checks; the replacement
                # char poisons int()/float() so the whole line drops instead.
                line = raw.decode('ascii', errors='replace').strip()
                if not line:
                    continue

                if args.debug:
                    print(f'[daemon] rx: {line!r}', flush=True)

                # '#' lines: info replies (I key), timing and coil echoes.
                # A '# settings loaded' banner carries timing AND spacing,
                # so both patterns are checked (no elif).
                if line.startswith('#'):
                    info = parse_info_line(line)
                    if info:
                        db_guard(stamp_info, conn, info, args.debug)
                        continue
                    m = RE_TIMING.search(line)
                    if m:
                        db_guard(note_drift, conn, 'timing',
                                 {'blanking_us': m.group(1),
                                  'rx_window_us': m.group(2)})
                    m = RE_TXPULSE.search(line)
                    if m:
                        db_guard(note_drift, conn, 'timing',
                                 {'tx_pulse_us': m.group(1)})
                    m = RE_COIL.search(line)
                    if m:
                        db_guard(note_drift, conn, 'geometry',
                                 {'coil_spacing_mm': m.group(1)})
                    m = RE_FIRE.search(line)
                    if m:
                        db_guard(note_drift, conn, 'fire',
                                 {'fire_mode': m.group(1)})
                    continue

                point = parse_data_line(line)
                if point is None:
                    if args.debug:
                        print(f'[daemon] parse fail: {line!r}', flush=True)
                    continue

                last_data_t = now
                if point['vin'] is not None:
                    last_vin, last_temp = point['vin'], point['temp']

                reason    = gate_reason(point, args.no_gate)
                recording = reason == ''

                # Live row at ~2 Hz in every mode — recording or not.
                if now - last_live_t >= LIVE_PERIOD_S:
                    db_guard(live_write, conn, point, recording, reason,
                             last_data_t, last_vin, last_temp)
                    last_live_t = now

                if not recording:
                    continue

                # Distance from last saved point
                if last_saved is not None:
                    dist_mm = latlon_dist_mm(last_saved[0], last_saved[1],
                                             point['lat'], point['lon'])
                else:
                    dist_mm = None  # first point — always save

                # Update motion status in meta at ~2 Hz (avoid hammering the db)
                if now - last_motion_t >= 0.5:
                    paused = dist_mm is not None and dist_mm < args.min_dist
                    db_guard(motion_set, conn, dist_mm, paused)
                    last_motion_t = now

                # Skip if not far enough from last saved point
                if dist_mm is not None and dist_mm < args.min_dist:
                    continue

                # last_saved only advances on a successful insert, so a
                # transient DB failure retries on the next line.
                if db_guard(insert_point, conn, point):
                    last_saved = (point['lat'], point['lon'])
                    if args.debug:
                        dist_str = f'{dist_mm:.0f} mm' if dist_mm is not None else 'first'
                        print(f'[daemon] inserted: adc={point["adc"]} '
                              f'({point["lat"]:.7f},{point["lon"]:.7f}) '
                              f'hdg={point["heading"]} dist={dist_str}', flush=True)

        # OSError too: unsupported-ioctl and unplug-mid-read failures arrive
        # as raw OSError, and deserve the same loud-once + 2 s retry.
        except (serial.SerialException, OSError) as e:
            if not port_error_announced:
                port_error_announced = True
                print(f'[daemon] ERROR: cannot open/read {args.port}: {e}',
                      flush=True)
                avail = [p.device for p in list_ports.comports()]
                print(f'[daemon] available ports: '
                      f'{", ".join(avail) if avail else "(none found)"}',
                      flush=True)
                print(f'[daemon] retrying every 2s...', flush=True)
            with lock:
                try:
                    if ser_ref[0]:
                        ser_ref[0].close()
                except Exception:
                    pass
                ser_ref[0] = None
            db_guard(live_write, conn, None, False, 'no port',
                     last_data_t, last_vin, last_temp)
            time.sleep(2)

        except Exception as e:
            print(f'[daemon] unexpected error: {e}', flush=True)
            time.sleep(0.1)


if __name__ == '__main__':
    try:
        main()
    finally:
        cleanup_pid()
