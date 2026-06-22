#!/usr/bin/env python3
"""
serial_daemon.py — Metal Mapper serial collection daemon.

Connects to the STM32 serial port, reads CSV detection data into a SQLite3
study file, and forwards command bytes from a named FIFO back to the device.

Usage:
    python serial_daemon.py --port /dev/ttyACM0 --db ~/metal_mapper/studies/foo.db
                            [--baud 460800] [--study-name "Back yard test"]
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


def latlon_dist_mm(lat1, lon1, lat2, lon2):
    """Flat-earth distance in millimetres — good to < 0.1 % for distances under 1 km."""
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) * 0.5))
    return math.sqrt(dlat * dlat + dlon * dlon) * R * 1000.0

FIFO_PATH = '/tmp/metal_detector_cmd.fifo'
PID_FILE  = '/tmp/metal_detector_daemon.pid'

# Matches status lines echoed by the STM32 after a timing adjustment:
#   # blanking=16us  rx_window=3us
RE_STATUS = re.compile(r'#.*blanking=(\d+)us.*rx_window=(\d+)us')


# ── Database ──────────────────────────────────────────────────────────────────

def db_open(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    # WAL mode lets Flask read the db concurrently while we write.
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS detections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            lat         REAL,
            lon         REAL,
            fix_quality INTEGER,
            adc_raw     INTEGER,
            gps_ts      TEXT
        )''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')
    conn.commit()
    return conn


def meta_set(conn, key, val):
    conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(val)))
    conn.commit()


# ── PID file ──────────────────────────────────────────────────────────────────

def write_pid(db_path):
    with open(PID_FILE, 'w') as f:
        f.write(f'{os.getpid()}:{os.path.abspath(db_path)}')


def cleanup_pid():
    try:
        os.unlink(PID_FILE)
    except FileNotFoundError:
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Metal Mapper serial daemon')
    ap.add_argument('--port',        required=True, help='Serial port, e.g. /dev/ttyACM0')
    ap.add_argument('--db',          required=True, help='Output SQLite3 file path')
    ap.add_argument('--baud',        type=int, default=460800)
    ap.add_argument('--study-name',  default='', help='Human-readable study label')
    ap.add_argument('--min-dist',    type=float, default=20.0,
                    metavar='MM',
                    help='Minimum distance in mm between saved points (default 50)')
    ap.add_argument('--debug',       action='store_true',
                    help='Print each received line to stdout (useful for troubleshooting)')
    args = ap.parse_args()

    db_path = os.path.expanduser(args.db)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    ensure_fifo()
    write_pid(db_path)

    conn = db_open(db_path)
    name = args.study_name or os.path.splitext(os.path.basename(db_path))[0]
    meta_set(conn, 'study_name',  name)
    meta_set(conn, 'serial_port', args.port)
    meta_set(conn, 'created_at',  str(time.time()))

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
          f'  min_dist={args.min_dist:.0f}mm', flush=True)

    last_saved      = None   # (lat, lon) of last inserted point
    last_motion_t   = 0.0    # last time motion meta was written

    while True:
        # ── Connect (or reconnect) to serial port ────────────────────────────
        try:
            with lock:
                ser_ref[0] = serial.Serial(args.port, args.baud, timeout=1)
            ser = ser_ref[0]
            print(f'[daemon] connected to {args.port}', flush=True)

            # ── Read loop ────────────────────────────────────────────────────
            while True:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode('ascii', errors='ignore').strip()
                if not line:
                    continue

                if args.debug:
                    print(f'[daemon] rx: {line!r}', flush=True)

                # Comment lines may carry a blanking/window status echo.
                if line.startswith('#'):
                    m = RE_STATUS.search(line)
                    if m:
                        meta_set(conn, 'blanking_us',  m.group(1))
                        meta_set(conn, 'rx_window_us', m.group(2))
                    continue

                # CSV: lat,lon,fix_quality,adc_raw,gps_timestamp
                parts = line.split(',')
                if len(parts) < 5:
                    continue
                try:
                    lat = float(parts[0])
                    lon = float(parts[1])
                    fq  = int(parts[2])
                    adc = int(parts[3])
                    gts = parts[4].strip()
                except ValueError:
                    if args.debug:
                        print(f'[daemon] parse fail: {line!r}', flush=True)
                    continue

                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue

                # Distance from last saved point
                if last_saved is not None:
                    dist_mm = latlon_dist_mm(last_saved[0], last_saved[1], lat, lon)
                else:
                    dist_mm = None  # first point — always save

                # Update motion status in meta at ~2 Hz (avoid hammering the db)
                now = time.time()
                if now - last_motion_t >= 0.5:
                    paused = dist_mm is not None and dist_mm < args.min_dist
                    meta_set(conn, 'motion_mm',     f'{dist_mm:.0f}' if dist_mm is not None else '0')
                    meta_set(conn, 'motion_paused', '1' if paused else '0')
                    last_motion_t = now

                # Skip if not far enough from last saved point
                if dist_mm is not None and dist_mm < args.min_dist:
                    continue

                conn.execute(
                    'INSERT INTO detections (ts,lat,lon,fix_quality,adc_raw,gps_ts) '
                    'VALUES (?,?,?,?,?,?)',
                    (time.time(), lat, lon, fq, adc, gts)
                )
                conn.commit()
                last_saved = (lat, lon)
                if args.debug:
                    dist_str = f'{dist_mm:.0f} mm' if dist_mm is not None else 'first'
                    print(f'[daemon] inserted: adc={adc} ({lat:.6f},{lon:.6f}) dist={dist_str}',
                          flush=True)

        except serial.SerialException as e:
            print(f'[daemon] serial error: {e} — retry in 2s', flush=True)
            with lock:
                try:
                    if ser_ref[0]:
                        ser_ref[0].close()
                except Exception:
                    pass
                ser_ref[0] = None
            time.sleep(2)

        except Exception as e:
            print(f'[daemon] unexpected error: {e}', flush=True)
            time.sleep(0.1)


if __name__ == '__main__':
    try:
        main()
    finally:
        cleanup_pid()
