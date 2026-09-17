#!/usr/bin/env python3
"""
test_db_map.py — regression battery for db_map.py + nav_gps.py.

No hardware needed: study DBs are built with serial_daemon's own db_open/
live_write (so the schema can never drift from the writer), the nav GPS is
a throwaway local TCP server, and the daemon PID file is redirected into a
temp dir. Run before deploying:

    python3 test_db_map.py
"""

import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import nav_gps
import serial_daemon as sd

FAILED = []


def check(name, ok, detail=''):
    print(f'  {"ok  " if ok else "FAIL"} {name}' + (f'  {detail}' if not ok and detail else ''))
    if not ok:
        FAILED.append(name)


# ── Scratch environment (set up BEFORE importing db_map) ─────────────────────

SCRATCH = tempfile.mkdtemp(prefix='dbmap_test_')
STUDIES = os.path.join(SCRATCH, 'studies')
os.makedirs(STUDIES)

# Deterministic host identity: HOST_TAG is read once at import, and the
# multi-unit tests below depend on knowing whose sidecar is whose.
os.environ['MM_HOST_TAG'] = 'benchA'

import db_map  # noqa: E402

db_map._cli_overrides.update({
    'studies_dir': STUDIES,
    'nav_host': '127.0.0.1',
    'nav_port': 1,               # nothing listens: nav must fail soft
})
db_map.PID_FILE = os.path.join(SCRATCH, 'daemon.pid')
db_map.CONFIG_FILE = Path(SCRATCH) / 'config.json'
# Disarm the once-per-boot empty-study purge: several fixtures here are
# 0-point studies on purpose. The purge gets its own tests, isolated.
db_map._purged = True


def make_study(name, n_points=5, heading=90.0, with_live=None, meta=None):
    """Build a study db exactly the way serial_daemon would."""
    path = os.path.join(STUDIES, f'{name}.db')
    conn = sd.db_open(path)
    # gps_ts varies per row (a column shift/reorder can't hide behind equal
    # values); row 3 is NULL and row 4 junk text, exercising both error arms
    # of load_columns' num() — those rows must come through as None, not 500.
    rows = [{'lat': 40.1 + i * 1e-6, 'lon': -119.1, 'heading': heading,
             'fix': 4, 'adc': [1000 + ch * 100 + i for ch in range(8)],
             'gps_ts': (None if i == 3 else 'garbled' if i == 4
                        else '%.3f' % (123519.10 + i * 0.025))}
            for i in range(n_points)]
    sd.insert_rows(conn, rows, 12.4, 25.0)
    for k, v in (meta or {}).items():
        sd.meta_set(conn, k, v)
    if with_live is not None:
        sd.live_write(conn, with_live.get('point'), with_live['recording'],
                      with_live['reason'], time.time(), 12.4, 25.0)
    conn.close()
    return path


# ── nav_gps: NMEA parsing ─────────────────────────────────────────────────────

print('nav_gps parser:')


def nmea(body):
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f'${body}*{cs:02X}'


GGA = nmea('GNGGA,123519.00,4807.0380,N,01131.0000,E,4,12,0.9,545.4,M,46.9,M,,')
RMC = nmea('GNRMC,123519.00,A,4807.0380,N,01131.0000,E,4.2,84.4,230394,,,A')
VTG = nmea('GNVTG,84.4,T,,M,4.2,N,7.8,K,A')

k, f = nav_gps.parse_nmea(GGA)
check('GGA parse', k == 'gga' and abs(f['lat'] - 48.1173) < 1e-4
      and abs(f['lon'] - 11.5166667) < 1e-4 and f['quality'] == 4
      and f['nsats'] == 12 and f['hdop'] == 0.9, repr(f))
k, f = nav_gps.parse_nmea(RMC)
check('RMC parse', k == 'rmc' and f['valid']
      and abs(f['speed_mps'] - 4.2 * 0.514444) < 1e-6 and f['cog_deg'] == 84.4)
k, f = nav_gps.parse_nmea(VTG)
check('VTG parse', k == 'vtg' and abs(f['speed_mps'] - 7.8 / 3.6) < 1e-6
      and f['cog_deg'] == 84.4)
check('bad checksum rejected', nav_gps.parse_nmea(GGA[:-1] + '0')[0] is None)
check('no checksum rejected', nav_gps.parse_nmea('$GNGGA,123519,4807.038,N')[0] is None)
check('garbage rejected', nav_gps.parse_nmea('!@#$%^&*')[0] is None)
nofix = nmea('GNGGA,123519.00,,,,,0,00,99.9,,M,,M,,')
k, f = nav_gps.parse_nmea(nofix)
check('GGA without fix', k == 'gga' and f['lat'] is None and f['quality'] == 0)
south = nmea('GNGGA,123519.00,3345.0000,S,15112.0000,W,1,08,1.1,10.0,M,,M,,')
k, f = nav_gps.parse_nmea(south)
check('S/W hemispheres negative', f['lat'] < 0 and f['lon'] < 0)

# ── nav_gps: live reader against a local TCP server ──────────────────────────

print('nav_gps reader:')

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', 0))
srv.listen(1)
nav_port = srv.getsockname()[1]
client_holder = {}


def serve_once():
    c, _ = srv.accept()
    client_holder['c'] = c
    c.sendall((GGA + '\r\n' + RMC + '\r\n').encode())


t = threading.Thread(target=serve_once, daemon=True)
t.start()

rd = nav_gps.NavReader('127.0.0.1', nav_port)
rd.start()
deadline = time.time() + 5
snap = {}
while time.time() < deadline:
    snap = rd.snapshot()
    if snap.get('lat') is not None:
        break
    time.sleep(0.05)
check('reader connected', snap.get('connected') is True, repr(snap))
check('reader fix fields', snap.get('quality') == 4
      and snap.get('cog_deg') == 84.4 and snap.get('speed_mps') is not None,
      repr(snap))
check('reader not stale', snap.get('stale') is False, repr(snap))

client_holder['c'].close()
srv.close()
deadline = time.time() + 5
while time.time() < deadline:
    snap = rd.snapshot()
    if not snap['connected']:
        break
    time.sleep(0.05)
check('reader fails soft on disconnect', snap['connected'] is False
      and snap['error'] is not None, repr(snap))
check('last fix survives disconnect', snap['lat'] is not None)

# ── coil geometry ─────────────────────────────────────────────────────────────

print('coil_positions:')

# Facing north (h=0): starboard = east. coil0 is far port = west of antenna.
pos = db_map.coil_positions(40.0, -119.0, 0.0, 500, 0, 0)
check('8 coils', len(pos) == 8)
check('north: coil0 west / coil7 east',
      pos[0][1] < -119.0 < pos[7][1] and abs(pos[0][0] - 40.0) < 1e-9,
      repr((pos[0], pos[7])))
span_m = (pos[7][1] - pos[0][1]) * 111320.0 * __import__('math').cos(
    __import__('math').radians(40.0))
check('north: span = 7 * spacing', abs(span_m - 3.5) < 0.01, f'{span_m:.3f} m')
mid = [(pos[3][0] + pos[4][0]) / 2, (pos[3][1] + pos[4][1]) / 2]
check('north: array centered on antenna',
      abs(mid[0] - 40.0) < 1e-9 and abs(mid[1] + 119.0) < 1e-9, repr(mid))

# Facing east (h=90): starboard = south -> coil0 is NORTH of the antenna.
pos = db_map.coil_positions(40.0, -119.0, 90.0, 500, 0, 0)
check('east: coil0 north / coil7 south',
      pos[0][0] > 40.0 > pos[7][0] and abs(pos[0][1] + 119.0) < 1e-9,
      repr((pos[0], pos[7])))

# Fore offset pushes the whole array forward (north when facing north).
pos = db_map.coil_positions(40.0, -119.0, 0.0, 500, 2000, 0)
check('fore offset: array 2 m north',
      all(abs((p[0] - 40.0) * 111320.0 - 2.0) < 0.01 for p in pos))

# Right offset shifts the center starboard (east when facing north).
pos = db_map.coil_positions(40.0, -119.0, 0.0, 500, 0, 1000)
mid_e = ((pos[3][1] + pos[4][1]) / 2 + 119.0) * 111320.0 * __import__('math').cos(
    __import__('math').radians(40.0))
check('right offset: center 1 m east', abs(mid_e - 1.0) < 0.01, f'{mid_e:.3f}')

# Heading offset: heading h with offset d == heading h+d with offset 0.
# (This is the sl_heading_offset_deg knob for a baseline-aligned GPS heading.)
pos_off = db_map.coil_positions(40.0, -119.0, 0.0, 500, 300, 200,
                                heading_off_deg=90.0)
pos_ref = db_map.coil_positions(40.0, -119.0, 90.0, 500, 300, 200)
check('heading offset == rotated heading',
      all(abs(a[0] - b[0]) < 1e-12 and abs(a[1] - b[1]) < 1e-12
          for a, b in zip(pos_off, pos_ref)))
pos_neg = db_map.coil_positions(40.0, -119.0, 90.0, 500, 0, 0,
                                heading_off_deg=-90.0)
pos_n = db_map.coil_positions(40.0, -119.0, 0.0, 500, 0, 0)
check('negative offset unwinds heading',
      all(abs(a[0] - b[0]) < 1e-12 and abs(a[1] - b[1]) < 1e-12
          for a, b in zip(pos_neg, pos_n)))

# ── derive_status matrix ──────────────────────────────────────────────────────

print('derive_status:')

# No PID file at all -> stopped, no live row needed.
p_saved = make_study('saved1', meta={'study_name': 'saved1'})
st = db_map.derive_status(p_saved)
check('saved study: stopped', st['state'] == 'stopped', repr(st['state']))
check('saved study: no warnings', st['warnings'] == [])

# Daemon attached (our own pid), recording.
pt = {'lat': 40.1, 'lon': -119.1, 'fix': 4, 'heading': 90.0,
      'adc': list(range(8)), 'gps_ts': 'x'}
p_rec = make_study('rec1', with_live={'point': pt, 'recording': 1, 'reason': ''})
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_rec)}')
st = db_map.derive_status(p_rec)
check('recording', st['state'] == 'recording', repr(st))
check('live row surfaced', st['live'] is not None and st['live']['adc'] == list(range(8)))

# Not recording with a reason.
p_deg = make_study('deg1', with_live={'point': pt, 'recording': 0, 'reason': 'fix=5'})
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_deg)}')
st = db_map.derive_status(p_deg)
check('not_recording + reason', st['state'] == 'not_recording'
      and st['reason'] == 'fix=5', repr((st['state'], st['reason'])))

# Wedged: PID alive but the live heartbeat is stale.
conn = sqlite3.connect(p_deg)
conn.execute('UPDATE live SET updated_at = ?', (time.time() - 10,))
conn.commit(); conn.close()
st = db_map.derive_status(p_deg)
check('wedged on stale heartbeat', st['state'] == 'wedged', repr(st['state']))

# Wedged: PID alive but live row never appeared.
p_norow = make_study('norow1')
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_norow)}')
st = db_map.derive_status(p_norow)
check('wedged when live row missing', st['state'] == 'wedged')

# Dead PID -> stopped.
Path(db_map.PID_FILE).write_text(f'999999:{os.path.abspath(p_rec)}')
st = db_map.derive_status(p_rec)
check('dead pid: stopped', st['state'] == 'stopped')
os.unlink(db_map.PID_FILE)

# Warnings from meta.
p_warn = make_study('warn1', meta={
    'gps_id': '?', 'info_ok': '0',
    'timing_changed': '1753500000:tx_pulse_us 120->80',
    'geometry_changed': '1753500000:coil_spacing_mm 500->510',
    'blanking_us': '16', 'blanking_us_current': '18',
    'rx_window_us': '3', 'tx_pulse_us': '120', 'tx_pulse_us_current': '80',
    'sample_rate_hz': '500', 'sample_rate_hz_current': '100',
    'detector_mode': 'all', 'detector_mode_current': 'raster',
    'slot_us': '200',
    'bl8': '16,16,16,16,16,16,16,16', 'bl8_current': '16,16,16,16,16,16,16,18',
    'rx8': '3,3,3,3,3,3,3,3', 'tx8': '120,120,120,120,120,120,120,120',
    'raster_sel': '3',
})
st = db_map.derive_status(p_warn)
check('identity warning', any('identity' in w for w in st['warnings']), repr(st['warnings']))
check('timing drift warning', any('tx_pulse_us 120->80' in w for w in st['warnings']))
check('geometry drift warning', any('coil_spacing_mm' in w for w in st['warnings']))
check('timing prefers _current', st['timing']['blanking_us'] == '18'
      and st['timing']['tx_pulse_us'] == '80' and st['timing']['rx_window_us'] == '3',
      repr(st['timing']))
# The raw screen's rate slider tracks the firmware through this field —
# a g/b echo lands in meta as sample_rate_hz_current and must win here.
check('timing carries sample_rate_hz (prefers _current)',
      st['timing']['sample_rate_hz'] == '100', repr(st['timing']))
# The raster keys ride the same timing dict (raw view's mode row, chips and
# per-channel readout). raster_sel is echo-only live state — no _current
# twin ever exists, so the plain-key fallback is what carries it.
check('timing carries raster keys (prefers _current)',
      st['timing']['detector_mode'] == 'raster'
      and st['timing']['slot_us'] == '200'
      and st['timing']['bl8'] == '16,16,16,16,16,16,16,18'
      and st['timing']['rx8'] == '3,3,3,3,3,3,3,3'
      and st['timing']['tx8'] == '120,120,120,120,120,120,120,120'
      and st['timing']['raster_sel'] == '3', repr(st['timing']))

# ── Flask routes ──────────────────────────────────────────────────────────────

print('flask routes:')

client = db_map.app.test_client()

r = client.get('/')
check('index 200', r.status_code == 200)
check('index lists study', b'saved1' in r.data)
check('index shows nav chip', b'NAV GPS' in r.data)
# Column dropped so the Created date fits one line at 800px.
check('index has no Last Activity column', b'Last Activity' not in r.data)

r = client.get('/view/saved1')
check('view 200', r.status_code == 200)
# timing controls are raw-screen-only (clipped at 800x480 on the map view)
check('view has no timing controls', b'TX Pulse' not in r.data
      and b'timing-panel' not in r.data)
check('view loads map_core.js', b'map_core.js' in r.data)
# version-stamped: kiosk Chromium heuristic-caches /static (Flask sends no
# Cache-Control), so deploys must flip the URL or stale JS survives reloads
check('static assets version-stamped', b'map_core.js?v=' in r.data)

r = client.get('/view/does_not_exist')
check('view of missing study redirects', r.status_code == 302)

r = client.get('/data/saved1')
d = r.get_json()
check('data 200', r.status_code == 200)
cols = d['points']
check('data is columnar', len(cols['lat']) == 5 and len(cols['adc']) == 8
      and len(cols['adc'][0]) == 5 and cols['heading'][0] == 90.0,
      repr({k: (v if k != 'adc' else v[0]) for k, v in cols.items()}))
# gps_ts rides along as floats — the client's sync filter folds on it.
# Per-row values differ (alignment is observable), NULL and junk text both
# come through as None (num()'s TypeError/ValueError arms).
check('data carries gps_ts column', len(cols['gps_ts']) == 5
      and abs(cols['gps_ts'][0] - 123519.10) < 1e-6
      and abs(cols['gps_ts'][2] - 123519.15) < 1e-6
      and cols['gps_ts'][3] is None and cols['gps_ts'][4] is None,
      repr(cols.get('gps_ts')))
check('data last_id from id column', d['last_id'] == cols['id'][-1] == 5)
check('data carries meta + status + nav',
      'meta' in d and d['status']['state'] == 'stopped' and 'connected' in d['nav'])

# env_stats: whole-run min/avg/max of the vin/temp stamped on each point.
# saved1's fixture stamps 12.4/25.0 everywhere, so all three collapse.
es = d['env_stats']
check('env_stats over points',
      abs(es['vin']['min'] - 12.4) < 1e-9 and abs(es['vin']['max'] - 12.4) < 1e-9
      and abs(es['vin']['avg'] - 12.4) < 1e-9 and abs(es['temp']['avg'] - 25.0) < 1e-9,
      repr(es))

# Mixed values aggregate across batches; NULL pairs (rows written before the
# first 1 Hz report) are skipped, and an all-NULL study yields None fields.
p_env = os.path.join(STUDIES, 'env1.db')
conn = sd.db_open(p_env)
mk = lambda i: {'lat': 40.2, 'lon': -119.2, 'heading': 90.0, 'fix': 4,
                'adc': [1000] * 8, 'gps_ts': '123519.10'}
sd.insert_rows(conn, [mk(0)], None, None)      # pre-first-report row
sd.insert_rows(conn, [mk(1)], 12.0, 20.0)
sd.insert_rows(conn, [mk(2)], 13.0, 30.0)
conn.close()
es = db_map.env_stats(p_env)
check('env_stats mixed + null-skip',
      es['vin']['min'] == 12.0 and es['vin']['max'] == 13.0
      and abs(es['vin']['avg'] - 12.5) < 1e-9
      and es['temp']['min'] == 20.0 and es['temp']['max'] == 30.0, repr(es))
p_env0 = make_study('env0', n_points=0)
check('env_stats empty study', db_map.env_stats(p_env0) ==
      {'vin': None, 'temp': None}, repr(db_map.env_stats(p_env0)))
check('env_stats missing db', db_map.env_stats('/nonexistent.db') ==
      {'vin': None, 'temp': None})

# /env_stats endpoint: the map view re-fetches here when a daemon detaches
# from an open study page (live → saved flip) — the load-time stats are stale.
r = client.get('/env_stats/env1')
es = r.get_json()
check('env_stats endpoint', r.status_code == 200
      and es['vin']['min'] == 12.0 and es['vin']['max'] == 13.0, repr(es))
check('env_stats endpoint 404', client.get('/env_stats/nope').status_code == 404)

# Empty study list while a daemon runs (raw session on a fresh install): the
# start panel is hidden, so the empty-table hint must not say 'start one above'.
_saved_over = dict(db_map._cli_overrides)
_empty_dir = os.path.join(SCRATCH, 'empty_studies')
os.makedirs(_empty_dir, exist_ok=True)
db_map._cli_overrides['studies_dir'] = _empty_dir
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(db_map.RAW_DB)}')
r = client.get('/')
check('empty list + daemon: no dangling start hint',
      b'start one above' not in r.data and b'stop the running daemon' in r.data)
os.unlink(db_map.PID_FILE)
r = client.get('/')
check('empty list, no daemon: start-above hint',
      b'start one above' in r.data)
db_map._cli_overrides.clear()
db_map._cli_overrides.update(_saved_over)

# ── startup purge of 0-point studies ─────────────────────────────────────────

print('empty-study purge:')

_purge_dir = os.path.join(SCRATCH, 'purge_studies')
os.makedirs(_purge_dir, exist_ok=True)
db_map._cli_overrides['studies_dir'] = _purge_dir


def _mini_study(name, n):
    path = os.path.join(_purge_dir, f'{name}.db')
    conn = sd.db_open(path)
    rows = [{'lat': 40.0, 'lon': -119.0, 'heading': 90.0, 'fix': 4,
             'adc': [1000] * 8, 'gps_ts': '123519.10'}] * n
    sd.insert_rows(conn, rows, 12.4, 25.0)
    conn.close()
    Path(path[:-3] + '.log').write_text('log')
    return path


# Logs written before the web process booted are fair game; logs from THIS
# boot may belong to a start that just failed (the error page points at
# them). The fixtures' logs are brand new, so push _BOOT_TS into the future
# to mark them as pre-boot for the baseline tests.
_saved_boot = db_map._BOOT_TS
db_map._BOOT_TS = time.time() + 3600

p_zero  = _mini_study('zap_me', 0)
p_full  = _mini_study('keep_me', 3)
p_live0 = _mini_study('live_zero', 0)
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_live0)}')
gone = db_map.purge_empty_studies()
check('purge removes only the empty study', gone == ['zap_me'], repr(gone))
check('purge deletes db + log',
      not os.path.exists(p_zero) and not os.path.exists(p_zero[:-3] + '.log'))
check('purge keeps non-empty study', os.path.exists(p_full)
      and os.path.exists(p_full[:-3] + '.log'))
check('purge keeps live 0-point study', os.path.exists(p_live0))
os.unlink(db_map.PID_FILE)

# Unreadable db: not provably empty, must survive. (live_zero lost its
# daemon guard above, so THIS pass legitimately removes it.)
p_junk = os.path.join(_purge_dir, 'corrupt.db')
Path(p_junk).write_bytes(b'this is not a sqlite file')
gone = db_map.purge_empty_studies()
check('purge skips unreadable db',
      os.path.exists(p_junk) and gone == ['live_zero'], repr(gone))

# The index route fires the purge exactly once per process.
p_zero2 = _mini_study('zap2', 0)
db_map._purged = False
client.get('/')
check('index triggers purge once', not os.path.exists(p_zero2))
p_zero3 = _mini_study('zap3', 0)
client.get('/')
check('second index render does not purge', os.path.exists(p_zero3))

# A log written AFTER boot survives even when its empty db is purged: it
# may hold the traceback of the start that just failed.
db_map._BOOT_TS = time.time() - 3600
p_fresh = _mini_study('fresh_fail', 0)
gone = db_map.purge_empty_studies()
check('purge keeps this-boot log of purged study',
      not os.path.exists(p_fresh)
      and os.path.exists(p_fresh[:-3] + '.log'), repr(gone))

# Orphan logs (a start that died before creating its db): pre-boot ones are
# swept, this-boot ones are kept.
db_map._BOOT_TS = time.time() + 3600
orph_old = os.path.join(_purge_dir, 'orph_old.log')
Path(orph_old).write_text('dead start, previous boot')
db_map.purge_empty_studies()
check('purge sweeps pre-boot orphan log', not os.path.exists(orph_old))
db_map._BOOT_TS = time.time() - 3600
orph_new = os.path.join(_purge_dir, 'orph_new.log')
Path(orph_new).write_text('dead start, this boot')
db_map.purge_empty_studies()
check('purge keeps this-boot orphan log', os.path.exists(orph_new))

db_map._BOOT_TS = _saved_boot

# Ownership gates: a foreign study pulled mid-recording legitimately has 0
# points — purging it would just start a delete/resurrect churn with the
# next sync pull. Legacy (untagged) ids are purgeable only while sync is
# off; our own tagged studies purge as usual (and take their sidecars).
db_map._BOOT_TS = time.time() + 3600
p_for0 = _mini_study('pull_1758000001__mm9', 0)
p_own0 = _mini_study('mine_1758000002__benchA', 0)
sc_own0 = os.path.join(_purge_dir,
                       'mine_1758000002__benchA.targets__mm9.json')
Path(sc_own0).write_text('{}')
gone = db_map.purge_empty_studies()
check('purge keeps foreign 0-point study',
      os.path.exists(p_for0) and 'pull_1758000001__mm9' not in gone,
      repr(gone))
check('purge takes own 0-point study + its sidecars',
      not os.path.exists(p_own0) and not os.path.exists(sc_own0), repr(gone))
p_leg0 = _mini_study('legacy_zero', 0)
db_map.CONFIG_FILE.write_text(json.dumps({'sync_hub': 'rsync://hub/mod'}))
gone = db_map.purge_empty_studies()
check('purge keeps legacy 0-point study while sync is on',
      os.path.exists(p_leg0) and gone == [], repr(gone))
db_map.CONFIG_FILE.unlink()
gone = db_map.purge_empty_studies()
check('purge takes legacy 0-point study with sync off',
      not os.path.exists(p_leg0) and gone == ['legacy_zero'], repr(gone))
os.unlink(p_for0)
db_map._BOOT_TS = _saved_boot

# study_owner / new_study_id: the '__<host>' tag routes the sync; a legacy
# id whose NAME part contains '__' must never parse as foreign.
sid = db_map.new_study_id('geo test')
check('new study id carries host tag', sid.endswith('__benchA')
      and db_map.safe_study_id(sid) == sid
      and db_map.study_owner(sid) == 'benchA', repr(sid))
check('ownership parse',
      db_map.study_owner('run_123') is None
      and db_map.study_owner('my__probe_1758') is None
      and db_map.study_owner('x_1758__mm9') == 'mm9'
      and db_map.is_foreign_study('x_1758__mm9') is True
      and db_map.is_foreign_study('x_1758__benchA') is False
      and db_map.is_foreign_study('run_123') is False)

# A candidate that gains rows between the lock-free scan and the locked
# delete pass must survive (the recount under the lock catches it). The
# hook rides get_daemon_status, which pass 2 calls right before recounting.
p_race = _mini_study('race_gain', 0)
_orig_gds = db_map.get_daemon_status


def _sneaky_gds():
    conn = sd.db_open(p_race)
    sd.insert_rows(conn, [{'lat': 40.0, 'lon': -119.0, 'heading': 90.0,
                           'fix': 4, 'adc': [1] * 8, 'gps_ts': '1'}],
                   12.4, 25.0)
    conn.close()
    db_map.get_daemon_status = _orig_gds   # fire once
    return _orig_gds()


db_map.get_daemon_status = _sneaky_gds
gone = db_map.purge_empty_studies()
db_map.get_daemon_status = _orig_gds
check('purge recount keeps candidate that gained rows',
      os.path.exists(p_race) and 'race_gain' not in gone, repr(gone))

# list_studies: a db purged between its stat and open (concurrent request)
# must be skipped, not rendered as a ghost row — while a corrupt-but-
# PRESENT db stays visible to the operator.
p_ghost = _mini_study('ghost', 2)
_orig_db_ro = db_map.db_ro


def _vanishing_db_ro(path):
    if path.endswith('ghost.db'):
        os.unlink(path)
    return _orig_db_ro(path)


db_map.db_ro = _vanishing_db_ro
names = [s['id'] for s in db_map.list_studies()]
db_map.db_ro = _orig_db_ro
check('list_studies skips db purged mid-listing', 'ghost' not in names,
      repr(names))
check('list_studies keeps corrupt-but-present db', 'corrupt' in names,
      repr(names))

db_map._purged = True
db_map._cli_overrides['studies_dir'] = STUDIES

# Ordering is CREATION time (newest study first), never file mtime —
# reopening / re-viewing an old study must not bubble it to the top.
p_old = make_study('order_old', meta={'created_at': '1000'})
p_new = make_study('order_new', meta={'created_at': '2000'})
db_map._study_cache.clear()
os.utime(p_old)          # old db touched LAST: mtime would put it on top
order = [s['id'] for s in db_map.list_studies()
         if s['id'].startswith('order_')]
check('list_studies sorts by created_at not mtime',
      order == ['order_new', 'order_old'], repr(order))
for p in (p_old, p_new):
    os.unlink(p)
db_map._study_cache.clear()

# Columns must agree with the row loader (the SSE path still uses rows —
# a divergence would render live points differently from loaded ones).
rows = db_map.load_points(os.path.join(STUDIES, 'saved1.db'))
colsd = db_map.load_columns(os.path.join(STUDIES, 'saved1.db'))


def _num(x):
    """Mirror load_columns' tolerant float: NULL/junk gps_ts → None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


check('rows/columns equivalent',
      all(colsd['id'][i] == p['id'] and colsd['lat'][i] == p['lat']
          and colsd['lon'][i] == p['lon'] and colsd['heading'][i] == p['heading']
          and colsd['fix'][i] == p['fix']
          and [colsd['adc'][ch][i] for ch in range(8)] == p['adc']
          and (colsd['gps_ts'][i] is None if _num(p['gps_ts']) is None
               else abs(colsd['gps_ts'][i] - _num(p['gps_ts'])) < 1e-9)
          for i, p in enumerate(rows)))
check('columns empty on missing db',
      db_map.load_columns('/nonexistent.db')['id'] == [])

# Path traversal must 404, never touch the filesystem.
r = client.get('/data/..%2f..%2fetc%2fpasswd')
check('traversal blocked (data)', r.status_code == 404, r.status_code)
r = client.get('/view/x%2e%2e%2fy')
check('traversal blocked (view)', r.status_code == 404, r.status_code)
r = client.get('/geojson/../serial_daemon.py')
check('traversal blocked (geojson)', r.status_code == 404, r.status_code)

# Settings roundtrip (zero8 as JSON). channels/heading_offset are obsolete
# (channel toggles removed; rotation lives in config.json) — a stale client
# still sending them must not write those keys.
r = client.post('/settings/saved1', json={
    'plus_range': 250, 'minus_range': 50, 'offset': -10,
    'filter': 'cm', 'combine': '0',
    'sync_period': '1', 'sync_res': '25', 'cm_group': 'sets',
    'zero8': [1000.5] * 8,
    'bias8': [0, 10, -20, 0, 0, 0, 0, 40],
    'channels': [True, True, False, True, True, True, True, False],
    'heading_offset': -90,
})
check('settings saved', r.get_json().get('ok') is True)
m = db_map.read_meta(p_saved)
check('settings persisted', m.get('sl_plus_range') == '250'
      and json.loads(m['sl_zero8']) == [1000.5] * 8, repr(m))
check('obsolete channel/rotation keys ignored',
      'sl_channels' not in m and 'sl_heading_offset_deg' not in m,
      repr(sorted(m)))
check('filter persisted', m.get('sl_filter') == 'cm',
      repr(m.get('sl_filter')))
check('sync tuning persisted', m.get('sl_sync_period') == '1'
      and m.get('sl_sync_res') == '25',
      repr((m.get('sl_sync_period'), m.get('sl_sync_res'))))
check('cm grouping persisted', m.get('sl_cm_group') == 'sets',
      repr(m.get('sl_cm_group')))
check('combine persisted', m.get('sl_combine') == '0',
      repr(m.get('sl_combine')))
check('bias8 persisted', json.loads(m.get('sl_bias8', 'null'))
      == [0, 10, -20, 0, 0, 0, 0, 40], repr(m.get('sl_bias8')))
# zero8/bias8 junk must be rejected at the door (400), never persisted:
# stored junk would round-trip into every future /view load.
# 10**400: json parses to an unbounded int — math.isfinite() would
# OverflowError into a 500; the validator must still answer 400.
for bad in ('abc', 5, {'a': 1}, [1] * 9, [1] * 7, ['x'] * 8,
            [True] * 8, [1e308] * 8, [70000] * 8, [10 ** 400] * 8):
    r = client.post('/settings/saved1', json={'bias8': bad})
    check('bias8 junk rejected: %.20r' % (bad,), r.status_code == 400,
          (bad, r.status_code))
    r = client.post('/settings/saved1', json={'zero8': bad})
    check('zero8 junk rejected: %.20r' % (bad,), r.status_code == 400,
          (bad, r.status_code))
m = db_map.read_meta(p_saved)
check('junk save left settings untouched',
      json.loads(m['sl_bias8']) == [0, 10, -20, 0, 0, 0, 0, 40]
      and json.loads(m['sl_zero8']) == [1000.5] * 8, repr(m.get('sl_bias8')))

# ── Dig targets: per-host sidecars, merged views, cross-unit rules ───────────
# New flags always go to THIS unit's sidecar json (never into the study db):
# the db belongs to whoever recorded it, and one-writer-per-file is the
# invariant the studies sync (sync_studies.sh) rests on.


def db_ro_has_no_targets_table(path):
    conn = sqlite3.connect(path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM sqlite_master "
                         "WHERE type='table' AND name='targets'").fetchone()[0]
    finally:
        conn.close()
    return n == 0


_sc_own = Path(STUDIES) / 'saved1.targets__benchA.json'
r = client.get('/targets/saved1')
check('targets empty before any mark', r.status_code == 200
      and r.get_json()['targets'] == [], repr(r.get_json()))
# legacy delete body ({'id': N}) before the db table exists — graceful no-op
r = client.post('/targets/saved1/delete', json={'id': 1})
check('target delete without table is ok', r.status_code == 200
      and r.get_json().get('ok') is True
      and r.get_json()['targets'] == [], repr(r.get_json()))
r = client.post('/targets/saved1', json={'lat': 40.1000005, 'lon': -119.1})
d = r.get_json()
check('target marked and keyed', r.status_code == 200
      and d.get('key') == 'benchA:1'
      and len(d['targets']) == 1
      and d['targets'][0]['lat'] == 40.1000005
      and d['targets'][0]['lon'] == -119.1
      and d['targets'][0]['key'] == 'benchA:1'
      and d['targets'][0]['own'] is True
      and d['targets'][0]['deletable'] is True
      and d['targets'][0]['label'] == 'benchA-1'
      and d['targets'][0]['cleared'] is False, repr(d))
check('target went to the sidecar, not the db',
      _sc_own.exists()
      and json.loads(_sc_own.read_text())['host'] == 'benchA'
      and db_ro_has_no_targets_table(p_saved))
r = client.post('/targets/saved1', json={'lat': 40.2, 'lon': -119.2})
check('second target keyed benchA:2', r.get_json().get('key') == 'benchA:2')
d = client.get('/data/saved1').get_json()
check('data ships targets',
      [t['key'] for t in d.get('targets', [])] == ['benchA:1', 'benchA:2'],
      repr(d.get('targets')))
r = client.post('/targets/saved1/delete', json={'key': 'benchA:1'})
check('target delete keeps survivors',
      [t['key'] for t in r.get_json()['targets']] == ['benchA:2'],
      repr(r.get_json()))
r = client.post('/targets/saved1', json={'lat': 40.3, 'lon': -119.3})
check('numbers never reused after delete',
      r.get_json().get('key') == 'benchA:3', repr(r.get_json()))
r = client.post('/targets/saved1', json={'lat': 'junk'})
check('target bad payload 400', r.status_code == 400, r.status_code)
r = client.post('/targets/saved1', json={'lat': 91.0, 'lon': 0.0})
check('target out-of-range 400', r.status_code == 400, r.status_code)
r = client.get('/targets/no_such_study')
check('targets 404 on unknown study', r.status_code == 404, r.status_code)

# Legacy in-db targets (pre-sidecar studies) still render and still delete —
# on a study this unit is authoritative for.
conn = sqlite3.connect(p_saved)
conn.execute('CREATE TABLE IF NOT EXISTS targets ('
             ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
             ' lat REAL NOT NULL, lon REAL NOT NULL, created_at REAL)')
conn.execute('INSERT INTO targets (id, lat, lon, created_at) '
             'VALUES (7, 40.5, -119.5, 100.0)')
conn.commit()
conn.close()
d = client.get('/targets/saved1').get_json()
legacy = [t for t in d['targets'] if t['src'] == 'db']
check('legacy in-db target merges in',
      len(legacy) == 1 and legacy[0]['key'] == 'db:7'
      and legacy[0]['label'] == '7' and legacy[0]['deletable'] is True,
      repr(d['targets']))

# The other unit's sidecar (as the sync would deliver it): renders merged,
# refuses deletion here, but can be marked found from here.
_sc_mm9 = Path(STUDIES) / 'saved1.targets__mm9.json'
_sc_mm9.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'mm9', 'seq': 1,
    'targets': [{'id': 1, 'lat': 40.9, 'lon': -119.9, 'created_at': 50.0}],
    'cleared': {}}))
d = client.get('/targets/saved1').get_json()
mm9 = [t for t in d['targets'] if t['src'] == 'mm9']
check('foreign sidecar target merges in',
      len(mm9) == 1 and mm9[0]['key'] == 'mm9:1'
      and mm9[0]['own'] is False and mm9[0]['deletable'] is False
      and mm9[0]['label'] == 'mm9-1', repr(d['targets']))
check('merged list sorts by created_at',
      [t['key'] for t in d['targets']][:2] == ['mm9:1', 'db:7'],
      repr([t['key'] for t in d['targets']]))
r = client.post('/targets/saved1/delete', json={'key': 'mm9:1'})
check('foreign target delete refused 403', r.status_code == 403
      and 'found' in r.get_json()['error'], (r.status_code, r.get_json()))
r = client.post('/targets/saved1/clear', json={'key': 'mm9:1'})
d = r.get_json()
mm9 = [t for t in d['targets'] if t['key'] == 'mm9:1']
check('foreign target marked found from here',
      r.status_code == 200 and mm9[0]['cleared'] is True
      and mm9[0]['cleared_by'] == 'benchA', repr(d['targets']))
check('found-mark went to OUR sidecar',
      json.loads(_sc_own.read_text())['cleared'].get('mm9:1') is not None
      and json.loads(_sc_mm9.read_text())['cleared'] == {})
r = client.post('/targets/saved1/clear', json={'key': 'mm9:1',
                                               'cleared': False})
mm9 = [t for t in r.get_json()['targets'] if t['key'] == 'mm9:1']
check('found-mark toggles back off', mm9[0]['cleared'] is False,
      repr(r.get_json()['targets']))
r = client.post('/targets/saved1/clear', json={'key': 'bad key!'})
check('clear bad key 400', r.status_code == 400, r.status_code)
r = client.post('/targets/saved1/clear', json={'key': 'benchA:999'})
check('clear of a key naming no target 404 (no sidecar litter)',
      r.status_code == 404
      and 'benchA:999' not in json.loads(_sc_own.read_text())['cleared'],
      r.status_code)
codes = []
for body in ('null', '[1, 2]', '"x"'):
    for route in ('delete', 'clear'):
        r = client.post(f'/targets/saved1/{route}', data=body,
                        content_type='application/json')
        codes.append(r.status_code)
check('non-dict JSON bodies → 400, never 500', codes == [400] * 6, codes)

# A found-mark set by the OTHER unit can't be un-marked here — it lives in
# their sidecar, so popping ours would silently change nothing. Our own
# mark layered on top pops fine, leaving theirs standing.
_sc_mm9.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'mm9', 'seq': 1,
    'targets': [{'id': 1, 'lat': 40.9, 'lon': -119.9, 'created_at': 50.0}],
    'cleared': {'db:7': 55.0}}))
d = client.get('/targets/saved1').get_json()
db7 = [t for t in d['targets'] if t['key'] == 'db:7'][0]
check("other unit's mark: cleared, not cleared_own",
      db7['cleared'] is True and db7['cleared_own'] is False
      and db7['cleared_by'] == 'mm9', repr(db7))
r = client.post('/targets/saved1/clear', json={'key': 'db:7',
                                               'cleared': False})
check("un-marking another unit's mark refused 403, names the unit",
      r.status_code == 403 and 'mm9' in r.get_json()['error'],
      (r.status_code, r.get_json()))
r = client.post('/targets/saved1/clear', json={'key': 'db:7'})
db7 = [t for t in r.get_json()['targets'] if t['key'] == 'db:7'][0]
check('our mark on top: cleared_own true', r.status_code == 200
      and db7['cleared_own'] is True and db7['cleared_by'] == 'benchA',
      repr(db7))
r = client.post('/targets/saved1/clear', json={'key': 'db:7',
                                               'cleared': False})
db7 = [t for t in r.get_json()['targets'] if t['key'] == 'db:7'][0]
check("popping our layer leaves the other unit's mark standing",
      r.status_code == 200 and db7['cleared'] is True
      and db7['cleared_own'] is False, repr(db7))
_sc_mm9.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'mm9', 'seq': 1,
    'targets': [{'id': 1, 'lat': 40.9, 'lon': -119.9, 'created_at': 50.0}],
    'cleared': {}}))

# Concurrent flags must not lose an update: the route serializes the
# sidecar read-modify-write under _sidecar_lock. The monkeypatched sleep
# widens the race window so a MISSING lock fails deterministically here,
# not once a season in the field.
_orig_load = db_map._load_own_sidecar
def _slow_load(study_id):
    sc = _orig_load(study_id)
    time.sleep(0.05)
    return sc
db_map._load_own_sidecar = _slow_load
try:
    conc_keys = []
    def _flag(i):
        r = db_map.app.test_client().post(
            '/targets/saved1', json={'lat': 40.5 + i * 1e-4, 'lon': -119.5})
        conc_keys.append((r.get_json() or {}).get('key'))
    ths = [threading.Thread(target=_flag, args=(i,)) for i in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
finally:
    db_map._load_own_sidecar = _orig_load
own_now = [t['key'] for t in client.get('/targets/saved1').get_json()['targets']
           if t['own']]
check('4 concurrent flags: 4 distinct keys, none lost',
      len(set(conc_keys)) == 4 and None not in conc_keys
      and set(conc_keys) <= set(own_now)
      and len(own_now) == len(set(own_now)), (conc_keys, own_now))
for k in conc_keys:
    client.post('/targets/saved1/delete', json={'key': k})

# seq floor: a mangled/rolled-back seq field must never renumber — other
# units' found-marks point at 'benchA:N' keys forever.
_sc_own.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'benchA', 'seq': 0,
    'targets': [{'id': 6, 'lat': 40.6, 'lon': -119.6, 'created_at': 60.0}],
    'cleared': {}}))
r = client.post('/targets/saved1', json={'lat': 40.61, 'lon': -119.61})
check('mangled seq floors at max existing id (no key reuse)',
      r.get_json().get('key') == 'benchA:7', repr(r.get_json()))
for k in ('benchA:6', 'benchA:7'):
    client.post('/targets/saved1/delete', json={'key': k})

# ...but a hand-edited GIANT id must not poison the floor: ids beyond the
# key-regex ceiling (9 digits) are ignored, so new keys stay parseable.
_sc_own.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'benchA', 'seq': 0,
    'targets': [{'id': 10**12, 'lat': 40.6, 'lon': -119.6,
                 'created_at': 60.0}],
    'cleared': {}}))
r = client.post('/targets/saved1', json={'lat': 40.62, 'lon': -119.62})
check('giant hand-edited id does not poison the seq floor',
      r.get_json().get('key') == 'benchA:1', repr(r.get_json()))
client.post('/targets/saved1/delete', json={'key': 'benchA:1'})

# the raw seq SCALAR gets the same ceiling: a mangled giant seq would mint
# a 10-digit key no unit could ever delete or mark found
_sc_own.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'benchA', 'seq': 5000000000,
    'targets': [{'id': 3, 'lat': 40.6, 'lon': -119.6, 'created_at': 60.0}],
    'cleared': {}}))
r = client.post('/targets/saved1', json={'lat': 40.63, 'lon': -119.63})
check('giant seq scalar clamped; floor recovers from target ids',
      r.get_json().get('key') == 'benchA:4', repr(r.get_json()))
client.post('/targets/saved1/delete', json={'key': 'benchA:4'})

# AT the 9-digit ceiling, refuse the flag instead of minting past it
_sc_own.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'benchA', 'seq': 999999999,
    'targets': [], 'cleared': {}}))
r = client.post('/targets/saved1', json={'lat': 40.64, 'lon': -119.64})
check('seq at the key ceiling: flag refused, no unparseable key',
      r.status_code == 400 and 'exhausted' in r.get_json()['error'],
      (r.status_code, r.get_json()))
_sc_own.write_text(json.dumps({
    'version': 1, 'study': 'saved1', 'host': 'benchA', 'seq': 0,
    'targets': [], 'cleared': {}}))

# A study RECORDED on the other unit: our flags still work (they go to our
# sidecar), but its in-db legacy targets are not ours to delete.
p_foreign = make_study('run_1758000000__mm9', n_points=2)
conn = sqlite3.connect(p_foreign)
conn.execute('CREATE TABLE targets (id INTEGER PRIMARY KEY AUTOINCREMENT,'
             ' lat REAL NOT NULL, lon REAL NOT NULL, created_at REAL)')
conn.execute('INSERT INTO targets (lat, lon, created_at) '
             'VALUES (40.7, -119.7, 60.0)')
conn.commit()
conn.close()
r = client.post('/targets/run_1758000000__mm9',
                json={'lat': 40.71, 'lon': -119.71})
check('flagging on a foreign study goes to our sidecar',
      r.get_json().get('key') == 'benchA:1'
      and (Path(STUDIES)
           / 'run_1758000000__mm9.targets__benchA.json').exists(),
      repr(r.get_json()))
r = client.post('/targets/run_1758000000__mm9/delete', json={'id': 1})
check('foreign study in-db target delete refused 403',
      r.status_code == 403 and 'mm9' in r.get_json()['error'],
      (r.status_code, r.get_json()))
d = client.get('/data/run_1758000000__mm9').get_json()
check('foreign study data merges db + our sidecar',
      sorted(t['key'] for t in d['targets']) == ['benchA:1', 'db:1']
      and [t for t in d['targets'] if t['key'] == 'db:1'][0]['deletable']
      is False, repr(d['targets']))

# The landing page badges studies the other unit recorded.
r = client.get('/')
check('index badges foreign study', b'badge-sync' in r.data
      and '⇄ mm9'.encode() in r.data)

# leave saved1 with no targets — and no foreign fixtures — so later payload
# checks see the base study set
for key in ('benchA:2', 'benchA:3', 'db:7'):
    client.post('/targets/saved1/delete', json={'key': key})
_sc_mm9.unlink()
_sc_own.unlink()          # drops the leftover cleared-map entries too
os.unlink(p_foreign)
(Path(STUDIES) / 'run_1758000000__mm9.targets__benchA.json').unlink()
db_map._study_cache.clear()
check('targets cleared',
      client.get('/targets/saved1').get_json()['targets'] == [])

# Rotation: /data serves config.json's heading_offset_deg unless the study
# saved its own sl_heading_offset_deg before the move to config.
d = client.get('/data/saved1').get_json()
check('data serves default rotation',
      d['meta'].get('sl_heading_offset_deg') == '270',
      repr(d['meta'].get('sl_heading_offset_deg')))
db_map.CONFIG_FILE.write_text(json.dumps({'heading_offset_deg': 90}))
d = client.get('/data/saved1').get_json()
check('data follows config rotation',
      d['meta'].get('sl_heading_offset_deg') == '90',
      repr(d['meta'].get('sl_heading_offset_deg')))
db_map.CONFIG_FILE.unlink()
make_study('rot1', meta={'sl_heading_offset_deg': '0'})
d = client.get('/data/rot1').get_json()
check('study meta rotation beats config',
      d['meta'].get('sl_heading_offset_deg') == '0',
      repr(d['meta'].get('sl_heading_offset_deg')))

# Rename updates the label (meta study_name); the .db filename is untouched.
r = client.post('/rename/saved1', json={'name': 'Renamed Run'})
check('rename ok', r.get_json().get('ok') is True, repr(r.get_json()))
m = db_map.read_meta(p_saved)
check('rename persisted', m.get('study_name') == 'Renamed Run',
      repr(m.get('study_name')))
check('rename keeps filename', os.path.exists(p_saved))
r = client.get('/view/saved1')
check('view shows new name', b'Renamed Run' in r.data)
check('view has meta panel', b'metaPanel' in r.data and b'META_GROUPS' in r.data)
# filters: independent checkboxes + tuning controls in the template, and
# the renderer API they drive in map_core.js
check('view ships filter checkboxes + tuning', b'cmChk' in r.data
      and b'syncChk' in r.data and b'cm_sync' in r.data
      and b'syncPer' in r.data and b'syncRes' in r.data
      and b'cmGrp' in r.data)
check('view ships target marking UI', b'markBtn' in r.data
      and b'tgtPanel' in r.data and b'/targets/' in r.data
      and b'updateApproach' in r.data)
# multi-unit target UI: key-based selection, found toggle, delete gating,
# foreign/cleared pin styling, SSE-driven refresh via setTargets
check('view ships multi-unit target UI', b'tgtFoundBtn' in r.data
      and b'toggleFound' in r.data and b'setTargets' in r.data
      and b'tgt-foreign' in r.data and b'tgt-cleared' in r.data
      and b'selTargetKey' in r.data and b'tgtDelBtn' in r.data
      and b'cleared_own' in r.data)
check('view ships per-channel bias panel', b'biasBtn' in r.data
      and b'biasPanel' in r.data and b'sl_bias8' in r.data
      and b'effZero8' in r.data)
check('view ships bias sliders + AGC auto', b'onBiasSlide' in r.data
      and b'autoBias' in r.data and b'biasS' in r.data)
check('view ships HUD dead-stream gates', b'lastAdcAt' in r.data
      and b'lastNavAt' in r.data)
_vjs = client.get('/static/map_core.js').data
check('renderer ships sync filter API',
      b'setSyncCfg' in _vjs and b'ensureSync' in _vjs
      and b'setCmSets' in _vjs)
check('renderer LOD bins coil footprint', b'cellCh' in _vjs)
check('renderer applies bias upstream of filters',
      b'zEff' in _vjs and b'bias8' in _vjs)
r = client.get('/')
check('index shows new name', b'Renamed Run' in r.data)
r = client.post('/rename/saved1', json={'name': '   '})
check('rename rejects blank', r.status_code == 400, r.status_code)
r = client.post('/rename/saved1', json={'name': 'x' * 121})
check('rename rejects overlong', r.status_code == 400, r.status_code)
r = client.post('/rename/no_such_study', json={'name': 'x'})
check('rename missing study 404', r.status_code == 404, r.status_code)

# Re-zero computes per-channel means over the last points.
r = client.post('/recalibrate/saved1')
d = r.get_json()
check('recalibrate ok', d.get('success') is True, repr(d))
# channels were 1000+ch*100+i for i in 0..4 -> mean = 1002 + ch*100
check('recalibrate per-channel means',
      d['zero8'] == [1002.0 + ch * 100 for ch in range(8)], repr(d.get('zero8')))
m = db_map.read_meta(p_saved)
check('recalibrate persisted', json.loads(m['sl_zero8'])[0] == 1002.0)

# /cmd with no daemon FIFO -> 503, not a crash.
db_map.FIFO_PATH = os.path.join(SCRATCH, 'no_such_fifo')
r = client.post('/cmd', json={'cmd': 'f'})
check('cmd without daemon -> 503', r.status_code == 503)

# /navfix works with nav down (fail-soft snapshot).
r = client.get('/navfix')
d = r.get_json()
check('navfix fail-soft', r.status_code == 200 and d['connected'] is False
      and d['error'] is not None, repr(d))

# nav_config validation.
r = client.post('/nav_config', json={'host': '', 'port': 999})
check('nav_config rejects empty host', r.status_code == 400)
r = client.post('/nav_config', json={'host': '127.0.0.1', 'port': 70000})
check('nav_config rejects bad port', r.status_code == 400)
r = client.post('/nav_config', json={'host': '127.0.0.1', 'port': 2})
check('nav_config applies', r.status_code == 200 and r.get_json()['ok'] is True)
cfgd = json.loads((Path(SCRATCH) / 'config.json').read_text())
check('nav_config persisted', cfgd['nav_host'] == '127.0.0.1' and cfgd['nav_port'] == 2)

# SSE stream: first tick carries rows + status + nav, for a SAVED study too.
r = client.get('/stream/saved1?since_id=0',
               headers={'Accept': 'text/event-stream'})
first = next(r.response)
evt = json.loads(first.decode().split('data: ', 1)[1].strip())
check('sse tick shape', evt['type'] == 'tick' and len(evt['rows']) == 5
      and evt['status']['state'] == 'stopped' and 'connected' in evt['nav'],
      repr(list(evt.keys())))
check('sse: no samples while stopped', evt['samples'] == [],
      repr(evt['samples'])[:120])
# targets ride every tick: a flag synced in from the other unit must show
# up on an open map without a reload.
check('sse tick carries targets', evt.get('targets') == [],
      repr(evt.get('targets')))
r.response.close()

# Landing-page nav chip is SSE-fed: the kiosk loads the page at boot,
# usually before the nav source is accepting, and a one-shot server-side
# render would show "down" forever. Reserved id must not hit study lookup.
r = client.get('/stream/__nav__', headers={'Accept': 'text/event-stream'})
first = next(r.response)
evt = json.loads(first.decode().split('data: ', 1)[1].strip())
check('nav stream: tick carries nav snapshot + daemon state',
      'connected' in evt['nav'] and evt['daemon_running'] is False,
      repr(evt)[:120])
# services + ipv4s ride the same tick (real values on the Pi, null
# elsewhere — assert presence only so the suite also passes ON the Pi).
check('nav stream: tick carries services field',
      'services' in evt, repr(evt)[:120])
check('nav stream: tick carries ipv4s field',
      'ipv4s' in evt, repr(evt)[:120])
r.response.close()
r = client.get('/')
check('index: nav chip fed by nav stream', b'/stream/__nav__' in r.data
      and b'new EventSource' in r.data and b'id="navChip"' in r.data)
# nginx answers a gunicorn-restart window with a 502, which kills an
# EventSource permanently — the page must reopen it itself (the map and
# raw views learned this the hard way; the kiosk parks here 24/7).
check('index: nav stream reopened on fatal SSE error',
      b'EventSource.CLOSED' in r.data)

# ⏻ power button: confirm overlay on the landing page; the route needs a
# working `sudo -n shutdown` and must surface refusal instead of hanging.
check('index: power button + shutdown confirm overlay',
      b'class="powerbtn"' in r.data and b'id="shutdownOverlay"' in r.data
      and b'action="/shutdown"' in r.data)
# The live-session warning is toggled by the SSE tick (a session started
# from another client must warn on this long-lived page), so the element
# is always in the DOM — hidden while no daemon runs.
check('index: shutdown warning present but hidden while idle',
      b'id="shutdownWarn"' in r.data
      and b'hidden' in r.data[r.data.index(b'id="shutdownWarn"') - 80:
                             r.data.index(b'id="shutdownWarn"') + 80])


class _FakeRun:
    def __init__(self, rc, err=''):
        self.returncode, self.stderr, self.stdout = rc, err, ''


_real_run = db_map.subprocess.run
db_map.subprocess.run = \
    lambda *a, **k: _FakeRun(1, 'sudo: a password is required')
r = client.post('/shutdown', follow_redirects=True)
check('shutdown: sudo refusal surfaces on landing page',
      b'shutdown failed' in r.data and b'password is required' in r.data)
db_map.subprocess.run = lambda *a, **k: _FakeRun(0)
# Post/Redirect/Get: reloading (or restoring, after the next power-on)
# the terminal tab must re-issue a harmless GET, never the POST.
r = client.post('/shutdown')
check('shutdown: success redirects to a GET terminal page',
      r.status_code == 302 and r.headers['Location'].endswith('/shutting_down'))
db_map.subprocess.run = _real_run
r = client.get('/shutting_down')
check('shutting_down: plain GET renders terminal page',
      r.status_code == 200 and b'Shutting down' in r.data)
r = client.get('/shutdown')
check('shutdown: GET refused', r.status_code == 405)

# ── Pi service panel ──────────────────────────────────────────────────────────

print('service panel:')

# No systemctl and no `ip` -> None/None and the panel never renders
# (Jinja-gated, not display:none). Forced, not assumed: this branch must
# also pass when the suite runs ON the Pi, where the binaries exist —
# and the TTL caches could be primed by earlier index renders there.
_sys_bins = (db_map.SYSTEMCTL_BIN,
             '/usr/sbin/ip', '/sbin/ip', '/usr/bin/ip', '/bin/ip')
_real_exists = db_map.os.path.exists
db_map.os.path.exists = \
    lambda p: False if p in _sys_bins else _real_exists(p)
db_map._svc_cache = {'t': None, 'val': None, 'refreshing': False}
db_map._ip_cache = {'t': None, 'val': None, 'refreshing': False}
try:
    check('service_states: None without systemctl',
          db_map.service_states() is None)
    check('local_ipv4s: None without an ip binary',
          db_map.local_ipv4s() is None)
    r = client.get('/')
    check('index: no service panel without systemctl',
          b'id="svcPanel"' not in r.data)
finally:
    db_map.os.path.exists = _real_exists

# On the Pi: ONE `systemctl is-active a b c` call — one state per stdout
# line in argument order; the nonzero exit (any unit inactive) is noise.
_units = sorted(p.name for p in db_map._REPO_DIR.glob('*.service'))
_lines = ['active'] * len(_units)
_lines[0] = 'failed'
_real_run = db_map.subprocess.run
db_map.os.path.exists = \
    lambda p: True if p == db_map.SYSTEMCTL_BIN else _real_exists(p)
_fr = _FakeRun(3)                      # nonzero: some unit isn't active
_fr.stdout = '\n'.join(_lines) + '\n'  # states arrive on stdout
db_map.subprocess.run = lambda *a, **k: _fr
# Reset the TTL cache going in (a real Pi systemctl may have primed it —
# the mock would never run) and going out (later index/SSE requests must
# not serve our fake 'failed' for the next 10 s).
db_map._svc_cache = {'t': None, 'val': None, 'refreshing': False}
try:
    _st = db_map.service_states()
finally:
    db_map.os.path.exists = _real_exists
    db_map.subprocess.run = _real_run
    db_map._svc_cache = {'t': None, 'val': None, 'refreshing': False}
check('service_states: units mapped to states in argument order',
      _st is not None and list(_st) == _units and _st[_units[0]] == 'failed'
      and all(_st[u] == 'active' for u in _units[1:]), repr(_st))

# local_ipv4s(): parse `ip -j -4 addr` JSON — loopback and inet6 excluded,
# iface name rides along. Same forced-binary + cache-reset dance.
_ip_json = json.dumps([
    {'ifname': 'lo',    'addr_info': [{'family': 'inet',  'local': '127.0.0.1'}]},
    {'ifname': 'wlan0', 'addr_info': [{'family': 'inet',  'local': '192.168.4.1'}]},
    {'ifname': 'eth0',  'addr_info': [{'family': 'inet',  'local': '10.1.2.3'},
                                      {'family': 'inet6', 'local': 'fe80::1'}]},
])
db_map.os.path.exists = \
    lambda p: True if p == '/usr/sbin/ip' else _real_exists(p)
_fr = _FakeRun(0)
_fr.stdout = _ip_json
db_map.subprocess.run = lambda *a, **k: _fr
db_map._ip_cache = {'t': None, 'val': None, 'refreshing': False}
try:
    _ips = db_map.local_ipv4s()
finally:
    db_map.os.path.exists = _real_exists
    db_map.subprocess.run = _real_run
    db_map._ip_cache = {'t': None, 'val': None, 'refreshing': False}
check('local_ipv4s: iface + addr, no loopback, no inet6',
      _ips == ['wlan0 192.168.4.1', 'eth0 10.1.2.3'], repr(_ips))

# A failed `ip` run (busybox/old iproute2 rejecting -j: rc!=0, usage on
# stderr, EMPTY stdout) is "couldn't determine" (None) — it must never be
# read or cached as a positive 'no IPv4'.
db_map.os.path.exists = \
    lambda p: True if p == '/usr/sbin/ip' else _real_exists(p)
_fr = _FakeRun(1, 'ip: unrecognized option: j')
db_map.subprocess.run = lambda *a, **k: _fr
db_map._ip_cache = {'t': None, 'val': None, 'refreshing': False}
try:
    _ips = db_map.local_ipv4s()
    _cached = db_map._ip_cache['val']
finally:
    db_map.os.path.exists = _real_exists
    db_map.subprocess.run = _real_run
    db_map._ip_cache = {'t': None, 'val': None, 'refreshing': False}
check('local_ipv4s: failed ip run is None, not cached as []',
      _ips is None and _cached is None, repr((_ips, _cached)))

# ── View Raw (--raw daemon + /raw routes + samples over SSE) ─────────────────

print('view raw:')

# The raw scratch db is redirected into SCRATCH so the real temp dir is
# never touched. Routes read the module global at request time.
db_map.RAW_DB = os.path.join(SCRATCH, 'raw_view.db')

check('RAW_ID resolves outside studies dir',
      db_map.get_db_path(db_map.RAW_ID) == db_map.RAW_DB
      and not db_map.get_db_path(db_map.RAW_ID).startswith(STUDIES))

# No daemon: View Raw is a one-touch start (POST /start_raw, no intermediate
# screen). With a daemon up it becomes a plain link to /raw (viewer attach).
r = client.get('/')
check('index has View Raw button',
      b'View Raw' in r.data and b'action="/start_raw"' in r.data)
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_saved)}')
r = client.get('/')
check('index View Raw is a link while daemon runs',
      b'href="/raw"' in r.data and b'action="/start_raw"' not in r.data)
# A daemon running while the operator sits on the landing page means a
# client reset mid-run: the primary button is the way back in, and the
# start form stays hidden. Assert the COMPOSED anchor — the studies table
# always contains href="/view/saved1" on its own, which would mask a
# broken live_id derivation (e.g. basename instead of stem).
check('index offers Return to Live Study while recording',
      'href="/view/saved1">▶ Return to Live Study'.encode() in r.data
      and b'Start Recording' not in r.data)

# Live map view: exit collapses into Stop & Exit (2-line label: the view
# toolbar is width-squeezed at 800px); a saved study is just a viewer and
# keeps plain navigation.
# NB: assert on rendered markup — both templates' JS contains a literal
# '← Studies' string (the stale-label swap when the daemon dies), so the
# plain-exit BUTTON is identified by its composed onclick form.
_studies_btn = 'onclick="location.href=\'/\'">← Studies'.encode()
r = client.get('/view/saved1')
check('live view exit is Stop & Exit',
      b'Stop<br>&amp; Exit' in r.data and _studies_btn not in r.data)
# The map strip has no scale selector, so it must pin the zero-referenced
# overlay explicitly — StripChart's default 'auto' became raw shared-axis.
check('view strip chart pins zero-overlay mode',
      b"chart.setScaleMode('zero')" in r.data)
# 800px layout: fix type rides above Stop & Exit, Vin/T sit under the strip
# chart — the info-col copies of these are clipped off-screen while driving.
check('live view: fix badge + vin/temp row in the visible zone',
      b'id="fixBadge"' in r.data and b'id="vinLiveRow"' in r.data
      and b'id="vinVal"' in r.data and b'id="tempVal"' in r.data)
os.unlink(db_map.PID_FILE)
r = client.get('/view/saved1')
check('saved view keeps plain Studies exit',
      _studies_btn in r.data and b'Stop<br>&amp; Exit' not in r.data)
check('saved view: no fix badge (live-only element)',
      b'id="fixBadge"' not in r.data)

# Stopped: the start form, no stream.
r = client.get('/raw')
check('raw stopped: 200 + start form', r.status_code == 200
      and b'/start_raw' in r.data and b'Start Streaming' in r.data)
r = client.get('/raw?error=boom')
# No URL strip here: this message points at the failure log, and a reload
# must keep pointing at it (unlike the streaming branch's one-shot note).
check('raw stopped: error surfaced', b'boom' in r.data)
check('raw stopped: error survives reload (no URL strip)',
      b'replaceState' not in r.data)

# Raw mode: daemon (faked as us) attached to the raw db.
conn = sd.db_open(db_map.RAW_DB)
pt_raw = {'lat': None, 'lon': None, 'fix': None, 'heading': None,
          'adc': [7] * 8, 'gps_ts': None}
sd.live_write(conn, pt_raw, 0, '', time.time(), 12.4, 25.0)
sd.samples_flush(conn, [(t, [t] * 8) for t in range(1, 9)])
conn.close()
Path(db_map.PID_FILE).write_text(
    f'{os.getpid()}:{os.path.abspath(db_map.RAW_DB)}')

r = client.get('/raw')
check('raw mode: streaming banner, no start form',
      b'STREAMING' in r.data and b'action="/start_raw"' not in r.data,
      r.status_code)
# raw is the ONLY home of the detector timing controls (removed from view)
check('raw has timing controls', b'TX Pulse' in r.data
      and b'timing-panel' in r.data)
# Raster UI: mode toggle (fw 'r' key), client-built channel chips
# (buildChips), and the per-channel table readout (tableVal indexes the
# bl8/rx8/tx8 CSVs by the selected chip via st.timing.raster_sel).
check('raw ships raster mode toggle + chips wiring',
      b"sendCmd('r')" in r.data and b'buildChips' in r.data
      and b'id="chipRow"' in r.data and b'tableVal' in r.data
      and b'raster_sel' in r.data)
# Channel legend: per-channel show/hide checkboxes + σ readout. The rows
# are client-built (buildVals), so assert the wiring ships, not the DOM.
check('raw ships channel-visibility + σ wiring',
      b'onChanVis' in r.data and b'updateStats' in r.data)
_js = client.get('/static/map_core.js').data
check('StripChart ships vis + stats API',
      b'setChanVis' in _js and b'stats()' in _js)
# Rate slider: 5 stops browsing the firmware's 20/40/100/200/500 Hz list.
# Save Settings moved OUT of the timing panel (its old slot IS the slider
# row) — it must render after the panel's closing tag.
check('raw has rate slider', b'id="rateSl"' in r.data
      and b'max="4"' in r.data)
# Outside-ness proven structurally: between the panel's opening <div> and
# the Save button, opens must equal closes — the panel (and every row in
# it) closed before the button. Nested back inside, opens would lead by 1.
# find() not index(): a missing marker must FAIL the check, not abort the
# whole harness with a ValueError.
_i0 = r.data.find(b'<div id="timing-panel"')
_i1 = r.data.find(b'Save<br>Settings')
_seg = r.data[_i0:_i1]
check('save button sits outside the timing panel',
      0 <= _i0 < _i1 and _seg.count(b'<div') == _seg.count(b'</div>'),
      (_i0, _i1))
# Stop and exit are ONE action — a daemon must never keep running after
# the operator lands back on the studies page. (Match the plain-exit
# BUTTON by its composed markup: the JS stale-label swap contains a bare
# '← Studies' string.)
check('raw mode: single Stop & Exit, no separate Studies exit',
      b'/stop_daemon' in r.data and b'value="raw"' not in r.data
      and b'Stop &amp; Exit' in r.data
      and 'onclick="location.href=\'/\'">← Studies'.encode() not in r.data)
check('raw mode: loads map_core.js', b'map_core.js' in r.data)
# Autoscale must be ONE shared axis over raw counts so channels compare;
# the per-channel-zero overlay is its own labelled mode, not "autoscale".
_scale_opts = (b'<option value="auto">autoscale</option>',
               '<option value="zero">Δ from zero</option>'.encode(),
               b'<option value="abs">0 \xe2\x80\x93 65535</option>')
check('raw mode: scale select = autoscale / Δ from zero / 0-65535',
      all(o in r.data for o in _scale_opts)
      and r.data.index(_scale_opts[0]) < r.data.index(_scale_opts[1])
      < r.data.index(_scale_opts[2]))
# e.g. a second /start_raw refused while streaming redirects here with
# ?error=... — the streaming branch must show it, not swallow it. It's a
# dismissible one-shot note (errNote) and the URL param is stripped
# client-side so a reload doesn't repeat it.
r = client.get('/raw?error=boom')
check('raw mode: error surfaced while streaming', b'boom' in r.data
      and b'errNote' in r.data and b'replaceState' in r.data)
# The dismiss handler must be parsed before the note paints — a tap during
# page streaming on slow Wi-Fi must not hit an undefined function.
check('raw mode: dismissErr defined before errNote',
      r.data.index(b'function dismissErr') < r.data.index(b'id="errNote"'))

# Raw study is addressable for /data (raw.html seeds meta from it) but must
# never appear in the studies list.
r = client.get('/data/' + db_map.RAW_ID)
check('data on raw db 200', r.status_code == 200
      and r.get_json()['status']['state'] in ('recording', 'not_recording'),
      r.status_code)
# The daemon box may honestly say "running — raw_view.db", but the raw db
# must never get a study row (it lives outside studies_dir).
r = client.get('/')
check('raw db not listed as a study', b'/view/raw_view' not in r.data
      and b'/view/__raw__' not in r.data)
# A raw session has no map to return to — the way back in is /raw itself.
check('index offers Return to Raw View while raw daemon runs',
      b'Return to Raw View' in r.data
      and b'Return to Live Study' not in r.data)

# SSE on the raw stream: first tick ships the whole ring, next tick only
# new rows past the cursor.
r = client.get('/stream/' + db_map.RAW_ID)
first = json.loads(next(r.response).decode().split('data: ', 1)[1].strip())
check('sse raw: first tick seeds whole ring',
      len(first['samples']) == 8 and first['samples'][0]['tick'] == 1
      and first['samples'][0]['adc'] == [1] * 8 and first['rows'] == [],
      repr(first['samples'][:2]))
conn = sqlite3.connect(db_map.RAW_DB)
conn.execute('INSERT INTO live_samples '
             '(tick,adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7) '
             'VALUES (100,1,2,3,4,5,6,7,8)')
conn.commit(); conn.close()
second = json.loads(next(r.response).decode().split('data: ', 1)[1].strip())
check('sse raw: cursor advances, only new samples',
      len(second['samples']) == 1 and second['samples'][0]['tick'] == 100,
      repr(second['samples']))

# A held connection must survive /start_raw recreating the db: the unlink
# resets the AUTOINCREMENT sequence, so without a cursor reset this stream
# would wait ~a whole session for the old ids to come back around.
for ext in ('', '-wal', '-shm'):
    try:
        os.unlink(db_map.RAW_DB + ext)
    except OSError:
        pass
conn = sd.db_open(db_map.RAW_DB)
sd.live_write(conn, pt_raw, 0, '', time.time(), 12.4, 25.0)
sd.samples_flush(conn, [(t, [t] * 8) for t in (501, 502, 503)])
conn.close()
third = json.loads(next(r.response).decode().split('data: ', 1)[1].strip())
if not third['samples']:      # one tick may straddle the recreate: allow it
    third = json.loads(next(r.response).decode().split('data: ', 1)[1].strip())
check('sse raw: cursor resets after db recreate',
      [s['tick'] for s in third['samples']] == [501, 502, 503],
      repr(third['samples']))
r.response.close()

# Study mode: daemon on a normal study → /raw is a viewer for it. The
# exit rule holds here too: Stop & Exit ends the RECORDING (deliberate —
# any exit to the studies page stops the daemon); the non-destructive way
# out is the study-map link.
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_rec)}')
r = client.get('/raw')
check('raw study mode: viewer with study link',
      b'/view/rec1' in r.data and b'action="/start_raw"' not in r.data)
check('raw study mode: exit is Stop & Exit (stops the recording)',
      b'Stop &amp; Exit' in r.data and b'/stop_daemon' in r.data
      and 'onclick="location.href=\'/\'">← Studies'.encode() not in r.data)

# A live study's SSE now carries samples too (the map-view strip chart).
conn = sqlite3.connect(p_rec)
conn.execute('INSERT INTO live_samples '
             '(tick,adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7) '
             'VALUES (55,9,9,9,9,9,9,9,9)')
conn.commit(); conn.close()
r = client.get('/stream/rec1?since_id=0')
first = json.loads(next(r.response).decode().split('data: ', 1)[1].strip())
check('sse live study: samples present',
      len(first['samples']) == 1 and first['samples'][0]['tick'] == 55,
      repr(first['samples']))
r.response.close()
os.unlink(db_map.PID_FILE)

# The stale-ring fence for real: rec1's ring still holds that row, but with
# the daemon gone (state 'stopped') it must NOT be served — a kill -9
# leftover would otherwise replay as live data. (This is the test that
# fails if the state gate is deleted from stream_study.)
r = client.get('/stream/rec1?since_id=0')
first = json.loads(next(r.response).decode().split('data: ', 1)[1].strip())
check('sse: leftover ring not served while stopped',
      first['status']['state'] == 'stopped' and first['samples'] == [],
      repr((first['status']['state'], first['samples'])))
r.response.close()

# start_raw takes no form fields (port comes from config, baud is the
# daemon's own default); its only pre-spawn rejection is a daemon already
# running, and the bounce must land back on /raw, not the index.
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_saved)}')
r = client.post('/start_raw')
check('start_raw refuses while daemon running',
      r.status_code == 302 and 'error=' in r.headers['Location'],
      r.headers.get('Location'))
check('start_raw redirects back to /raw',
      '/raw' in r.headers['Location'], r.headers.get('Location'))
os.unlink(db_map.PID_FILE)

# Stop and exit are one action: every stop lands on the studies page, and
# stray form fields (including the retired from=raw) can't redirect it.
r = client.post('/stop_daemon', data={'from': 'raw'})
check('stop always lands on the studies page',
      r.status_code == 302 and not r.headers['Location'].endswith('/raw'),
      r.headers.get('Location'))
r = client.post('/stop_daemon', data={'from': 'http://evil.example'})
check('stop with junk from → index',
      r.status_code == 302 and not r.headers['Location'].endswith('/raw'),
      r.headers.get('Location'))

# ── study-list cache ──────────────────────────────────────────────────────────

# Summaries are cached per db and re-read only when the file signature
# (db + -wal mtime/size) changes — the landing page must not COUNT-scan a
# season of studies on every render.
db_map._study_cache.clear()
before = {s['id']: s['count'] for s in db_map.list_studies()}
_openings = []
_orig_ro2 = db_map.db_ro


def _counting_ro(path):
    _openings.append(path)
    return _orig_ro2(path)


db_map.db_ro = _counting_ro
again = {s['id']: s['count'] for s in db_map.list_studies()}
db_map.db_ro = _orig_ro2
check('study cache: unchanged dbs not re-opened',
      again == before and not _openings, repr(_openings))

conn = sd.db_open(p_rec)
sd.insert_rows(conn, [{'lat': 40.2, 'lon': -119.2, 'heading': 90.0,
                       'fix': 4, 'adc': [1] * 8, 'gps_ts': '123520.10'}],
               12.4, 25.0)
conn.close()
after = {s['id']: s['count'] for s in db_map.list_studies()}
check('study cache: write invalidates via file signature',
      after['rec1'] == before['rec1'] + 1
      and all(after[k] == v for k, v in before.items() if k != 'rec1'),
      repr((before.get('rec1'), after.get('rec1'))))

# A transient read failure (fd exhaustion, SD-card hiccup) must NOT be
# cached: a quiescent file's signature never changes, so caching the
# 0-point fallback would pin it until process restart.
db_map._study_cache.clear()


def _failing_ro(path):
    if path.endswith('rec1.db'):
        raise sqlite3.OperationalError('disk I/O error')
    return _orig_ro2(path)


db_map.db_ro = _failing_ro
hiccup = {s['id']: s['count'] for s in db_map.list_studies()}
db_map.db_ro = _orig_ro2
healed = {s['id']: s['count'] for s in db_map.list_studies()}
check('study cache: transient read failure not cached',
      hiccup['rec1'] == 0 and healed['rec1'] == after['rec1'],
      repr((hiccup.get('rec1'), healed.get('rec1'))))

# ── review-driven hardening ───────────────────────────────────────────────────

print('hardening:')

# Study ids from user-entered names must always satisfy safe_study_id
# (ASCII-only, no leading '-') or the daemon records into a db no route
# can ever address.
for nm in ('café test', '北京 run', '-north pass', '', 'plain_OK-1'):
    sid = db_map.new_study_id(nm)
    check(f'study id safe for {nm!r}',
          db_map._STUDY_ID_RE.fullmatch(sid) is not None
          and not sid.startswith('-'), sid)

# /stream: bad since_id tolerated, unknown study 404s, ticks carry id:,
# and Last-Event-ID (browser reconnect) beats the stale query param.
r = client.get('/stream/saved1?since_id=abc')
first = next(r.response)
r.response.close()
check('stream bad since_id tolerated', first.decode().startswith('id: '))
r = client.get('/stream/no_such_study')
check('stream unknown study 404', r.status_code == 404, r.status_code)
r = client.get('/stream/saved1?since_id=0', headers={'Last-Event-ID': '3'})
first = next(r.response)
r.response.close()
evt = json.loads(first.decode().split('data: ', 1)[1].strip())
check('stream resumes from Last-Event-ID',
      len(evt['rows']) == 2 and evt['rows'][0]['id'] == 4,
      repr([p['id'] for p in evt['rows']]))
check('stream ticks carry id:', first.decode().startswith('id: 5\n'))

# /cmd must emit at most ONE byte on the FIFO (a codepoint can be 4 bytes).
fifo = os.path.join(SCRATCH, 'cmd_fifo')
os.mkfifo(fifo)
db_map.FIFO_PATH = fifo
rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
r = client.post('/cmd', json={'cmd': '\U0001F600'})
check('cmd rejects non-ascii', r.status_code == 400, r.status_code)
r = client.post('/cmd', json={'cmd': 'f'})
data = os.read(rfd, 16)
check('cmd writes exactly one byte',
      r.get_json().get('ok') is True and data == b'f', repr(data))
# Rate slider steps arrive as individual g/b keys — plain single bytes.
r = client.post('/cmd', json={'cmd': 'g'})
data = os.read(rfd, 16)
check('cmd rate key g forwards one byte', data == b'g', repr(data))
r = client.post('/cmd', json={'cmd': 'b'})
data = os.read(rfd, 16)
check('cmd rate key b forwards one byte', data == b'b', repr(data))
# SAVE and CLEAR are the only multi-byte commands: firmware word-gates both
# flash writes ("WORD"+Enter), so the wire bytes must be word plus newline.
r = client.post('/cmd', json={'cmd': 'SAVE'})
data = os.read(rfd, 16)
check('cmd SAVE sends word + newline',
      r.get_json().get('ok') is True and data == b'SAVE\n', repr(data))
r = client.post('/cmd', json={'cmd': 'CLEAR'})
data = os.read(rfd, 16)
check('cmd CLEAR sends word + newline',
      r.get_json().get('ok') is True and data == b'CLEAR\n', repr(data))
# Anything else multi-char still truncates to its first byte ('Sx' must
# never reach the wire as anything that could accumulate toward a save).
r = client.post('/cmd', json={'cmd': 'SAVE '})
data = os.read(rfd, 16)
check('cmd non-exact SAVE truncates to one byte', data == b'S', repr(data))
os.close(rfd)

# Cross-origin browser POSTs are refused; same-origin sails through, and
# ports are ignored (nginx $host / ssh -L tunnels strip them from Host).
r = client.post('/settings/saved1', json={'offset': 1},
                headers={'Origin': 'http://evil.example'})
check('cross-origin POST blocked', r.status_code == 403, r.status_code)
r = client.post('/settings/saved1', json={'offset': 1},
                headers={'Origin': 'http://localhost'})
check('same-origin POST allowed', r.status_code == 200, r.status_code)
r = client.post('/settings/saved1', json={'offset': 1},
                headers={'Origin': 'http://localhost:8080'})
check('same-host other-port POST allowed', r.status_code == 200, r.status_code)

# Hand-edited config.json with wrong types must fall back, never 500.
orig_cfg_text = (db_map.CONFIG_FILE.read_text()
                 if db_map.CONFIG_FILE.exists() else None)
saved_overrides = dict(db_map._cli_overrides)
# ('baud' is a legacy key — configs written before it moved into the
# daemon still carry it; it must be ignored, not crash anything.)
db_map.CONFIG_FILE.write_text(json.dumps(
    {'nav_port': None, 'baud': 'fast', 'studies_dir': 123, 'nav_host': '',
     'heading_offset_deg': 'north'}))
db_map._cli_overrides.clear()
cfg = db_map.load_config()
check('config type fallback',
      cfg['nav_port'] == 50012
      and isinstance(cfg['studies_dir'], str) and cfg['nav_host'] == '127.0.0.1'
      and cfg['heading_offset_deg'] == 270,
      repr(cfg))
db_map._cli_overrides.update(saved_overrides)
if orig_cfg_text is not None:
    db_map.CONFIG_FILE.write_text(orig_cfg_text)

# save_config: atomic tmp+rename write, reports success.
ok = db_map.save_config(nav_host='10.0.0.5')
cfgd = json.loads(db_map.CONFIG_FILE.read_text())
check('save_config atomic roundtrip', ok is True and cfgd['nav_host'] == '10.0.0.5')

# start_daemon validates before spawning; bad values bounce with an error.
try:
    os.unlink(db_map.PID_FILE)
except FileNotFoundError:
    pass
r = client.post('/start_daemon', data={'port': '/dev/x', 'min_dist': 'abc'})
check('start_daemon rejects bad min_dist',
      r.status_code == 302 and 'error=' in r.headers['Location'])
r = client.post('/start_daemon', data={'port': '', 'min_dist': '20'})
check('start_daemon rejects empty port',
      r.status_code == 302 and 'error=' in r.headers['Location'])

# A zombie child daemon (kill -9 / OOM) must be reaped, not reported alive.
import subprocess as _sp
child = _sp.Popen([sys.executable, '-c', 'pass'])
time.sleep(0.5)                      # child has exited -> zombie until reaped
Path(db_map.PID_FILE).write_text(f'{child.pid}:{p_saved}')
st = db_map.get_daemon_status()
check('zombie daemon reported not running', st['running'] is False, repr(st))
os.unlink(db_map.PID_FILE)

# Held-coordinates GGA with quality 0 is NOT a fix: fix age must not refresh.
rd = nav_gps.NavReader('127.0.0.1', 1)
GGA_HELD = nmea('GNGGA,123519.00,4807.0380,N,01131.0000,E,0,3,9.9,545.4,M,46.9,M,,')
rd._apply(*nav_gps.parse_nmea(GGA_HELD))
snap = rd.snapshot()
check('quality-0 GGA stays stale',
      snap['stale'] is True and snap['fix_age_s'] is None
      and snap['quality'] == 0, repr(snap))
rd._apply(*nav_gps.parse_nmea(GGA))
check('quality>0 GGA refreshes fix age', rd.snapshot()['stale'] is False)

print()
if FAILED:
    print(f'{len(FAILED)} FAILURE(S): {FAILED}')
    sys.exit(1)
print('ALL PASS')
