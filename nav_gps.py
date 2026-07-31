#!/usr/bin/env python3
"""
nav_gps.py — background reader for the vehicle navigation GPS.

The nav GPS (RTK-corrected, no true heading — only course-over-ground
derived from movement) is served as plain NMEA sentences on a TCP port.
In our rig a PyGPSClient socket server provides it, but the contract is
just "NMEA over TCP": gpsd's raw port, socat, or ser2net all work, and
the host:port is configurable so the whole thing can point at a Pi from
a Mac during bench work.

One NavReader keeps ONE latest-fix snapshot in memory. Nothing is
persisted — guidance positions are disposable; the RTK GPS on the
detector remains the position of record for study data.

Fail-soft is the contract: no nav source just means
snapshot()['connected'] is False and the UI greys out the nav marker.
The reader thread reconnects forever and never raises.
"""

import socket
import threading
import time

RECONNECT_S  = 2.0    # pause between connection attempts
SOCKET_TO_S  = 5.0    # read timeout — a silent socket counts as down
STALE_FIX_S  = 5.0    # snapshot marks the fix stale after this


def _nmea_checksum_ok(line):
    """Validate '$...*hh'. Lines without a checksum are rejected — the only
    upstream we trust is a GNSS receiver, and those always emit one."""
    if not line.startswith('$') or '*' not in line:
        return False
    body, _, tail = line[1:].partition('*')
    if len(tail) < 2:
        return False
    try:
        want = int(tail[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _parse_latlon(val, hemi, degdigits):
    """NMEA ddmm.mmmm / dddmm.mmmm + hemisphere -> signed degrees."""
    if not val or not hemi:
        return None
    try:
        deg = int(val[:degdigits])
        minutes = float(val[degdigits:])
    except ValueError:
        return None
    out = deg + minutes / 60.0
    return -out if hemi in ('S', 'W') else out


def parse_nmea(line):
    """Parse one NMEA sentence into (kind, fields) or (None, None).

    Kinds:
      'gga' -> {lat, lon, quality, nsats, hdop, alt_m}   (lat/lon None w/o fix)
      'rmc' -> {valid, speed_mps, cog_deg}               (cog None when absent)
      'vtg' -> {speed_mps, cog_deg}
    Talker ID is ignored ($GPGGA/$GNGGA/... all match).
    """
    line = line.strip()
    if not _nmea_checksum_ok(line):
        return None, None
    body = line[1:].partition('*')[0]
    parts = body.split(',')
    typ = parts[0][-3:] if len(parts[0]) >= 3 else ''

    try:
        if typ == 'GGA' and len(parts) >= 10:
            quality = int(parts[6]) if parts[6] else 0
            return 'gga', {
                'lat':     _parse_latlon(parts[2], parts[3], 2),
                'lon':     _parse_latlon(parts[4], parts[5], 3),
                'quality': quality,
                'nsats':   int(parts[7]) if parts[7] else None,
                'hdop':    float(parts[8]) if parts[8] else None,
                'alt_m':   float(parts[9]) if parts[9] else None,
            }
        if typ == 'RMC' and len(parts) >= 9:
            return 'rmc', {
                'valid':     parts[2] == 'A',
                'speed_mps': float(parts[7]) * 0.514444 if parts[7] else None,
                'cog_deg':   float(parts[8]) if parts[8] else None,
            }
        if typ == 'VTG' and len(parts) >= 8:
            return 'vtg', {
                'cog_deg':   float(parts[1]) if parts[1] else None,
                'speed_mps': float(parts[7]) / 3.6 if parts[7] else None,
            }
    except (ValueError, IndexError):
        pass
    return None, None


class NavReader:
    """Background NMEA-over-TCP reader with a lock-protected latest fix."""

    def __init__(self, host, port):
        self._lock = threading.Lock()
        self._host = host
        self._port = int(port)
        self._epoch = 0          # bumped by configure() to force a reconnect
        self._thread = None
        self._connected = False
        self._error = 'not started'
        self._fix = {}           # merged GGA/RMC/VTG fields
        self._fix_at = None      # wall time of last positional update
        self._data_at = None     # wall time of last valid sentence

    # ── public API ───────────────────────────────────────────────────────────

    def start(self):
        """Idempotent; spawns the reader thread on first call."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name='nav-gps', daemon=True)
        self._thread.start()

    def configure(self, host, port):
        """Point at a different NMEA source; the thread reconnects."""
        with self._lock:
            self._host = host
            self._port = int(port)
            self._epoch += 1

    def snapshot(self):
        """Current nav state as a plain dict (safe to jsonify)."""
        now = time.time()
        with self._lock:
            fix = dict(self._fix)
            fix_age = (now - self._fix_at) if self._fix_at else None
            connected = self._connected
            out = {
                'connected': connected,
                'host': self._host,
                'port': self._port,
                'error': None if connected else self._error,
                'fix_age_s': round(fix_age, 1) if fix_age is not None else None,
                'stale': (fix_age is None or fix_age > STALE_FIX_S),
            }
        out.update({k: fix.get(k) for k in
                    ('lat', 'lon', 'quality', 'nsats', 'hdop', 'alt_m',
                     'speed_mps', 'cog_deg')})
        return out

    # ── reader thread ────────────────────────────────────────────────────────

    def _apply(self, kind, f):
        with self._lock:
            self._data_at = time.time()
            if kind == 'gga':
                self._fix.update(f)
                # Some receivers hold last-known coordinates with quality 0
                # instead of blanking the fields — that is NOT a fix and must
                # not keep the snapshot looking fresh.
                if f['lat'] is not None and f['quality'] > 0:
                    self._fix_at = time.time()
            elif kind == 'rmc':
                if f['speed_mps'] is not None:
                    self._fix['speed_mps'] = f['speed_mps']
                # COG only meaningful when the receiver deems the fix valid
                self._fix['cog_deg'] = f['cog_deg'] if f['valid'] else None
            elif kind == 'vtg':
                if f['speed_mps'] is not None:
                    self._fix['speed_mps'] = f['speed_mps']
                if f['cog_deg'] is not None:
                    self._fix['cog_deg'] = f['cog_deg']

    def _set_down(self, err):
        with self._lock:
            self._connected = False
            self._error = err

    def _run(self):
        while True:
            with self._lock:
                host, port, epoch = self._host, self._port, self._epoch
            try:
                sock = socket.create_connection((host, port), timeout=SOCKET_TO_S)
            except OSError as e:
                self._set_down(f'connect {host}:{port}: {e}')
                time.sleep(RECONNECT_S)
                continue
            sock.settimeout(SOCKET_TO_S)
            with self._lock:
                self._connected = True
                self._error = None
            buf = b''
            try:
                while True:
                    with self._lock:
                        if self._epoch != epoch:
                            break              # reconfigured — reconnect
                    chunk = sock.recv(4096)
                    if not chunk:
                        self._set_down('connection closed')
                        break
                    buf += chunk
                    while b'\n' in buf:
                        raw, _, buf = buf.partition(b'\n')
                        kind, f = parse_nmea(
                            raw.decode('ascii', errors='replace'))
                        if kind:
                            self._apply(kind, f)
                    if len(buf) > 65536:
                        buf = b''              # no newline in 64K: not NMEA
            except OSError as e:
                self._set_down(f'read: {e}')
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._lock:
                reconfigured = self._epoch != epoch
            if reconfigured:
                # snapshot() must not claim a live connection to the NEW
                # endpoint before it has ever been contacted — and there is
                # no reason to sit out the reconnect pause on a user switch.
                # (Checked here, not just at the break: a reconfigure landing
                # while recv() blocks surfaces as a timeout instead.)
                self._set_down('reconfiguring')
                continue
            time.sleep(RECONNECT_S)
