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

import db_map  # noqa: E402

db_map._cli_overrides.update({
    'studies_dir': STUDIES,
    'nav_host': '127.0.0.1',
    'nav_port': 1,               # nothing listens: nav must fail soft
})
db_map.PID_FILE = os.path.join(SCRATCH, 'daemon.pid')
db_map.CONFIG_FILE = Path(SCRATCH) / 'config.json'


def make_study(name, n_points=5, heading=90.0, with_live=None, meta=None):
    """Build a study db exactly the way serial_daemon would."""
    path = os.path.join(STUDIES, f'{name}.db')
    conn = sd.db_open(path)
    for i in range(n_points):
        pt = {'lat': 40.1 + i * 1e-6, 'lon': -119.1, 'heading': heading,
              'fix': 4, 'adc': [1000 + ch * 100 + i for ch in range(8)],
              'gps_ts': '123519.10', 'vin': 12.4, 'temp': 25.0}
        sd.insert_point(conn, pt)
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
})
st = db_map.derive_status(p_warn)
check('identity warning', any('identity' in w for w in st['warnings']), repr(st['warnings']))
check('timing drift warning', any('tx_pulse_us 120->80' in w for w in st['warnings']))
check('geometry drift warning', any('coil_spacing_mm' in w for w in st['warnings']))
check('timing prefers _current', st['timing']['blanking_us'] == '18'
      and st['timing']['tx_pulse_us'] == '80' and st['timing']['rx_window_us'] == '3',
      repr(st['timing']))

# ── Flask routes ──────────────────────────────────────────────────────────────

print('flask routes:')

client = db_map.app.test_client()

r = client.get('/')
check('index 200', r.status_code == 200)
check('index lists study', b'saved1' in r.data)
check('index shows nav chip', b'NAV GPS' in r.data)

r = client.get('/view/saved1')
check('view 200', r.status_code == 200)
check('view has tx pulse control', b'TX Pulse' in r.data)
check('view loads map_core.js', b'map_core.js' in r.data)

r = client.get('/view/does_not_exist')
check('view of missing study redirects', r.status_code == 302)

r = client.get('/data/saved1')
d = r.get_json()
check('data 200', r.status_code == 200)
cols = d['points']
check('data is columnar', len(cols['lat']) == 5 and len(cols['adc']) == 8
      and len(cols['adc'][0]) == 5 and cols['heading'][0] == 90.0,
      repr({k: (v if k != 'adc' else v[0]) for k, v in cols.items()}))
check('data last_id from id column', d['last_id'] == cols['id'][-1] == 5)
check('data carries meta + status + nav',
      'meta' in d and d['status']['state'] == 'stopped' and 'connected' in d['nav'])

# Columns must agree with the row loader (the SSE path still uses rows —
# a divergence would render live points differently from loaded ones).
rows = db_map.load_points(os.path.join(STUDIES, 'saved1.db'))
colsd = db_map.load_columns(os.path.join(STUDIES, 'saved1.db'))
check('rows/columns equivalent',
      all(colsd['id'][i] == p['id'] and colsd['lat'][i] == p['lat']
          and colsd['lon'][i] == p['lon'] and colsd['heading'][i] == p['heading']
          and colsd['fix'][i] == p['fix']
          and [colsd['adc'][ch][i] for ch in range(8)] == p['adc']
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
    'zero8': [1000.5] * 8,
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
check('combine persisted', m.get('sl_combine') == '0',
      repr(m.get('sl_combine')))

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
r.response.close()

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
db_map.CONFIG_FILE.write_text(json.dumps(
    {'nav_port': None, 'baud': 'fast', 'studies_dir': 123, 'nav_host': '',
     'heading_offset_deg': 'north'}))
db_map._cli_overrides.clear()
cfg = db_map.load_config()
check('config type fallback',
      cfg['nav_port'] == 50012 and cfg['baud'] == 460800
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
r = client.post('/start_daemon', data={'port': '/dev/x', 'baud': '46080o',
                                       'min_dist': '20'})
check('start_daemon rejects bad baud',
      r.status_code == 302 and 'error=' in r.headers['Location'],
      r.headers.get('Location'))
r = client.post('/start_daemon', data={'port': '/dev/x', 'baud': '²',
                                       'min_dist': '20'})
check('start_daemon rejects unicode digit baud',
      r.status_code == 302 and 'error=' in r.headers['Location'],
      r.status_code)
r = client.post('/start_daemon', data={'port': '/dev/x', 'baud': '115200',
                                       'min_dist': 'abc'})
check('start_daemon rejects bad min_dist',
      r.status_code == 302 and 'error=' in r.headers['Location'])
r = client.post('/start_daemon', data={'port': '', 'baud': '115200',
                                       'min_dist': '20'})
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
