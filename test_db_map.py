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
import math
import struct
import os
import socket
import sqlite3
import subprocess
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
    # values); row 3 is NULL and row 4 junk text, exercising both failure
    # arms of the /points gps_ts converter — those rows must come through
    # as NaN, not 500.
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
    'pulse_us': '200', 'pulse_us_current': '4200',
    'bl8': '16,16,16,16,16,16,16,16', 'bl8_current': '16,16,16,16,16,16,16,18',
    'rx8': '3,3,3,3,3,3,3,3', 'tx8': '120,120,120,120,120,120,120,120',
    'raster_sel': '3',
})
st = db_map.derive_status(p_warn)
check('identity warning', any('identity' in w for w in st['warnings']), repr(st['warnings']))
check('identity warning says position is fine',
      any('identity' in w and 'position data is unaffected' in w for w in st['warnings']),
      repr(st['warnings']))
# A saved study (no daemon attached) must not promise that anything re-asks.
check('stopped study: no re-asking promise',
      not any('re-asking' in w for w in st['warnings']), repr(st['warnings']))
check("old-daemon '?' alone: no header-incomplete warning",
      not any('header incomplete' in w for w in st['warnings']), repr(st['warnings']))
# The daemon (2026-09-20) no longer stamps '?': identity is simply absent
# with info_ok=0 — same warning.
p_noid = make_study('warn_noid', meta={'info_ok': '0', 'fw_git_hash': 'abc1234',
                                        'gps_ver': 'LG580P03'})
st_noid = db_map.derive_status(p_noid)
check('absent gps_id + info_ok=0 → identity warning',
      any('identity' in w for w in st_noid['warnings']), repr(st_noid['warnings']))
# ...and with a live daemon attached the warning says it keeps asking.
Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_noid)}')
st_noid_live = db_map.derive_status(p_noid)
os.unlink(db_map.PID_FILE)
check('live study: identity warning says the daemon keeps re-asking',
      any('identity' in w and 're-asking' in w for w in st_noid_live['warnings']),
      repr(st_noid_live['warnings']))
# Old daemon, glitched VERNO reply only: gps_ver '?' with info_ok=1 must
# still surface (the old outer gate looked at gps_id alone).
p_verq = make_study('warn_verq', meta={'info_ok': '1', 'fw_git_hash': 'abc1234',
                                        'gps_ver': '?', 'gps_id': 'OK,16,AB'})
check("gps_ver '?' alone → identity warning",
      any('identity' in w for w in db_map.derive_status(p_verq)['warnings']))
# Identity present but the header still incomplete = a different problem
# (truncated line / old firmware) and must not be blamed on the GPS.
p_trunc = make_study('warn_trunc', meta={'info_ok': '0', 'fw_git_hash': 'abc1234',
                                          'gps_ver': 'LG580P03', 'gps_id': 'OK,16,AB'})
st_trunc = db_map.derive_status(p_trunc)
check('incomplete header with identity → header warning, not GPS',
      any('header incomplete' in w for w in st_trunc['warnings'])
      and not any('identity' in w for w in st_trunc['warnings']),
      repr(st_trunc['warnings']))
# info_missing (daemon >= 2026-09-20) makes it precise: both problems at
# once → both warnings, the header one naming the non-identity keys.
p_miss = make_study('warn_miss', meta={'info_ok': '0', 'fw_git_hash': 'abc1234',
                                        'gps_ver': 'LG580P03',
                                        'info_missing': 'gps_id,bl8,tx8'})
st_miss = db_map.derive_status(p_miss)
check('info_missing: identity AND header warnings',
      any('identity' in w for w in st_miss['warnings'])
      and any('header incomplete (missing bl8, tx8)' in w for w in st_miss['warnings']),
      repr(st_miss['warnings']))
p_missid = make_study('warn_missid', meta={'info_ok': '0', 'fw_git_hash': 'abc1234',
                                            'gps_ver': 'LG580P03', 'info_missing': 'gps_id'})
st_missid = db_map.derive_status(p_missid)
check('info_missing = identity only → no header warning',
      any('identity' in w for w in st_missid['warnings'])
      and not any('header incomplete' in w for w in st_missid['warnings']),
      repr(st_missid['warnings']))
# Nothing answered the connect-time 'I' at all: not a GPS problem.
p_noans = make_study('warn_noans', meta={'info_ok': '0'})
st_noans = db_map.derive_status(p_noans)
check('no info reply at all → never-answered warning, not GPS',
      any('never answered' in w for w in st_noans['warnings'])
      and not any('identity' in w for w in st_noans['warnings']),
      repr(st_noans['warnings']))
p_ok = make_study('warn_none', meta={'info_ok': '1', 'gps_id': 'OK,16,AB', 'gps_ver': 'X',
                                      'info_missing': ''})
check('complete header → no header warning',
      not any('identity' in w or 'header' in w or 'answered' in w
              for w in db_map.derive_status(p_ok)['warnings']))
# Firmware gives up on a module without a unique ID: gps_id=none is a value.
p_none = make_study('warn_idnone', meta={'info_ok': '1', 'gps_id': 'none', 'gps_ver': 'X',
                                          'fw_git_hash': 'abc1234', 'info_missing': ''})
check('gps_id=none (module has no unique ID) → no warning',
      not any('identity' in w or 'header' in w
              for w in db_map.derive_status(p_none)['warnings']))
# gps_cfg (firmware >= 2026-09-20): did the GPS module take its boot config.
# Only 'noreply' earns a warning — 'silent' is the GPS being off (already
# visible as no fix), 'pending' settles within seconds, boot/resent are fine.
for val in ('boot', 'resent', 'pending', 'silent'):
    p_cfg = make_study(f'warn_cfg_{val}',
                       meta={'info_ok': '1', 'gps_id': 'OK,16,AB', 'gps_ver': 'X',
                             'fw_git_hash': 'abc1234', 'info_missing': '', 'gps_cfg': val})
    st_cfg = db_map.derive_status(p_cfg)
    check(f'gps_cfg={val} → no configuration warning',
          not any('configuration' in w for w in st_cfg['warnings']), repr(st_cfg['warnings']))
p_cfgno = make_study('warn_cfg_noreply',
                     meta={'info_ok': '1', 'gps_id': 'OK,16,AB', 'gps_ver': 'X',
                           'fw_git_hash': 'abc1234', 'info_missing': '', 'gps_cfg': 'noreply'})
st_cfgno = db_map.derive_status(p_cfgno)
check('gps_cfg=noreply → stale-settings warning, no identity/header warning',
      any('configuration' in w and 'stale' in w for w in st_cfgno['warnings'])
      and not any('identity' in w or 'header' in w for w in st_cfgno['warnings']),
      repr(st_cfgno['warnings']))
# noreply alongside a missing identity: both warnings, independently.
p_cfgboth = make_study('warn_cfg_both', meta={'info_ok': '0', 'fw_git_hash': 'abc1234',
                                               'info_missing': 'gps_ver,gps_id',
                                               'gps_cfg': 'noreply'})
st_cfgboth = db_map.derive_status(p_cfgboth)
check('gps_cfg=noreply + identity missing → both warnings',
      any('identity' in w for w in st_cfgboth['warnings'])
      and any('configuration' in w for w in st_cfgboth['warnings']),
      repr(st_cfgboth['warnings']))
# Live study: the config warning never promises re-asking (the firmware's
# re-sends are spent), and pending/silent stay quiet there too.
for val, want in (('noreply', True), ('pending', False), ('silent', False)):
    p_cfglive = make_study(f'warn_cfg_live_{val}',
                           meta={'info_ok': '1', 'gps_id': 'OK,16,AB', 'gps_ver': 'X',
                                 'fw_git_hash': 'abc1234', 'info_missing': '', 'gps_cfg': val})
    Path(db_map.PID_FILE).write_text(f'{os.getpid()}:{os.path.abspath(p_cfglive)}')
    st_cfglive = db_map.derive_status(p_cfglive)
    os.unlink(db_map.PID_FILE)
    cfgw = [w for w in st_cfglive['warnings'] if 'configuration' in w]
    check(f'live gps_cfg={val} → configuration warning '
          f'{"present, no re-asking" if want else "absent"}',
          bool(cfgw) == want and not any('re-asking' in w for w in cfgw),
          repr(st_cfglive['warnings']))
# A fresh db before any handshake (no info_ok yet) says nothing.
p_fresh = make_study('warn_fresh', meta={'study_name': 'x'})
check('no handshake yet → no header warnings',
      not any('identity' in w or 'header' in w or 'answered' in w
              for w in db_map.derive_status(p_fresh)['warnings']))
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
      and st['timing']['pulse_us'] == '4200'
      and st['timing']['bl8'] == '16,16,16,16,16,16,16,18'
      and st['timing']['rx8'] == '3,3,3,3,3,3,3,3'
      and st['timing']['tx8'] == '120,120,120,120,120,120,120,120'
      and st['timing']['raster_sel'] == '3', repr(st['timing']))

# Link health + pacer report (raw view row 2) ride the status too. The
# daemon writes `link` as JSON and the '# pace' payload verbatim; a saved
# study from an older daemon has neither, and a torn value must degrade to
# None rather than take the SSE tick down.
check('old study: no link/pace', st['link'] is None and st['pace'] is None)
_lk = {'samples': 4808, 'anchors': 481, 'samples_s': 200.1, 'anchors_s': 20.0,
       'spa_min': 9, 'spa_max': 11, 'tick_gaps': 0, 'frames_lost': 0,
       'reboots': 0, 'eq_ticks': 0}
p_link = make_study('link1', meta={
    'link': json.dumps(_lk),
    'pace_last': 'det=all ivl_us=5000 fires=1000 skips=0 jit_max_us=3',
    'pace_at': f'{time.time() - 5:.1f}'})
st = db_map.derive_status(p_link)
check('link parsed from meta', st['link'] == _lk, repr(st['link']))
check('pace text + age from the server clock',
      st['pace']['text'].startswith('det=all ivl_us=5000')
      and st['pace']['at'] is not None and 4.0 <= st['pace']['age_s'] <= 8.0,
      repr(st['pace']))
p_torn = make_study('link2', meta={'link': '{"samples": 1', 'pace_last': 'det=x',
                                   'pace_at': 'soon'})
st = db_map.derive_status(p_torn)
check('torn link JSON degrades to None', st['link'] is None)
check('pace with unparsable stamp keeps its text',
      st['pace'] == {'text': 'det=x', 'at': None, 'age_s': None}, repr(st['pace']))
p_list = make_study('link3', meta={'link': '[1,2]'})
check('non-object link degrades to None', db_map.derive_status(p_list)['link'] is None)
# float('nan') / float('inf') parse fine but serialise as NaN/Infinity,
# which the browser's JSON.parse rejects — the whole SSE tick would die.
p_nan = make_study('link4', meta={'pace_last': 'det=x', 'pace_at': 'nan'})
st = db_map.derive_status(p_nan)
check('non-finite pace_at degrades to None', st['pace']['at'] is None
      and st['pace']['age_s'] is None, repr(st['pace']))
check('status serialises as strict JSON',
      'NaN' not in json.dumps(st) and 'Infinity' not in json.dumps(st))

# ── Flask routes ──────────────────────────────────────────────────────────────

print('flask routes:')

client = db_map.app.test_client()

r = client.get('/')
check('index 200', r.status_code == 200)
check('index lists study', b'saved1' in r.data)
check('index shows nav chip', b'NAV GPS' in r.data)
import socket as _socket
_host = _socket.gethostname().split('.')[0]
check('index heading names this host',
      f'<h1>Metal Mapper <span class="host">{_host}</span></h1>'.encode() in r.data, _host)
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
# Source wiring of the bulk load: the page must fetch /points and fill the
# store from the decoded columns — the old JSON shape is gone from both
# ends, and nothing may still read d.points.
_view_src = (db_map._REPO_DIR / 'templates' / 'view.html').read_text()
_core_src = (db_map._REPO_DIR / 'static' / 'map_core.js').read_text()
_raw_src = (db_map._REPO_DIR / 'templates' / 'raw.html').read_text()
check('view wires /points → decodePoints → fillBinary, with retries',
      "fetch('/points/' + STUDY_ID + '?upto=' + upto)" in _view_src
      and 'MM.decodePoints(buf)' in _view_src and 'store.fillBinary(cols)' in _view_src
      and 'fetchPoints(d.last_id, 3)' in _view_src
      and 'd.points' not in _view_src and 'fillColumns' not in _view_src
      and 'd.points' not in _raw_src)
check('map_core exports decodePoints, store has fillBinary, fillColumns gone',
      'decodePoints,' in _core_src and 'fillBinary(cols)' in _core_src
      and 'function decodePoints(buf)' in _core_src and 'fillColumns' not in _core_src)

# Base layer defaults OFFLINE (field units have thin or no internet; a page
# waiting on tiles drags the nav marker and guidance with it) under a fresh
# localStorage key, and the default is never persisted: the old page wrote
# its 'online' default on first load, so flipping the default alone would
# never have reached a deployed kiosk. The toggle flips the CURRENT mode,
# not the stored one — with nothing stored the stored-key toggle was a no-op.
check('view: base layer defaults offline, own key, persisted only on a tap',
      "const MAP_BASE_KEY = 'mapBase'" in _view_src
      and "applyMapMode(localStorage.getItem(MAP_BASE_KEY) || 'offline', false)" in _view_src
      and "if (persist) localStorage.setItem(MAP_BASE_KEY, mode)" in _view_src
      and "applyMapMode(mapMode === 'offline' ? 'online' : 'offline', true)" in _view_src
      and "localStorage.removeItem('mapMode')" in _view_src
      and "setItem('mapMode'" not in _view_src
      and "tile.openstreetmap.org" in _view_src)

r = client.get('/view/does_not_exist')
check('view of missing study redirects', r.status_code == 302)

r = client.get('/data/saved1')
d = r.get_json()
check('data 200', r.status_code == 200)
# Points no longer ride /data (the JSON form OOM-killed the worker on a
# 2 GB Pi): it hands the client the extent, the points come from /points.
check('data carries extent, not points', 'points' not in d
      and d['n_points'] == 5 and d['last_id'] == 5, repr(d.keys()))
check('data carries meta + status + nav',
      'meta' in d and d['status']['state'] == 'stopped' and 'connected' in d['nav'])


def decode_points(body):
    """Independent reader of the /points wire format (mirrors the JS
    decodePoints): magic, uint32 header length, JSON header, 8-aligned
    little-endian columns. Returns {name: array.array}, plus n/last_id."""
    import array as _array
    assert body[:4] == b'MMP1', body[:8]
    hlen = struct.unpack_from('<I', body, 4)[0]
    hdr = json.loads(body[8:8 + hlen])
    base = (8 + hlen + 7) & ~7
    tc = {'u32': 'I', 'f64': 'd', 'f32': 'f', 'u8': 'B', 'u16': 'H'}
    out = {'n': hdr['n'], 'last_id': hdr['last_id'], 'cols': {}}
    for name, dtype, off, count in hdr['cols']:
        a = _array.array(tc[dtype])
        at = base + off
        assert at % 8 == 0, (name, at)
        assert at + count * a.itemsize <= len(body), (name, at, count, len(body))
        a.frombytes(body[at:at + count * a.itemsize])
        assert len(a) == count == hdr['n'], (name, len(a), count, hdr['n'])
        out['cols'][name] = a
    return out


r = client.get('/points/saved1')
check('points 200 octet-stream', r.status_code == 200
      and r.mimetype == 'application/octet-stream'
      and r.headers.get('Content-Length') == str(len(r.data)),
      (r.status_code, r.mimetype, r.headers.get('Content-Length'), len(r.data)))
pts = decode_points(r.data)
pc = pts['cols']
check('points columns + dtypes', pts['n'] == 5 and pts['last_id'] == 5
      and list(pc) == ['id', 'lat', 'lon', 'heading', 'fix'] +
      [f'adc{i}' for i in range(8)] + ['gps_ts']
      and pc['id'].typecode == 'I' and pc['lat'].typecode == 'd'
      and pc['heading'].typecode == 'f' and pc['fix'].typecode == 'B'
      and pc['adc0'].typecode == 'H' and pc['gps_ts'].typecode == 'd',
      repr([(k, a.typecode) for k, a in pc.items()]))
check('points values', list(pc['id']) == [1, 2, 3, 4, 5]
      and abs(pc['lat'][2] - (40.1 + 2e-6)) < 1e-12 and pc['lon'][0] == -119.1
      and pc['heading'][0] == 90.0 and list(pc['fix']) == [4] * 5
      and [pc[f'adc{ch}'][3] for ch in range(8)] == [1003 + ch * 100 for ch in range(8)],
      repr({k: list(a)[:5] for k, a in pc.items()}))
# gps_ts rides along as floats — the client's sync filter folds on it.
# Per-row values differ (alignment is observable), NULL and junk text both
# come through as NaN (the converter's TypeError/ValueError arms) — and the
# partial C-speed extend before the failing value must have been rolled
# back, or rows 0-2 would appear twice and the column would outrun n.
check('points gps_ts column', abs(pc['gps_ts'][0] - 123519.10) < 1e-6
      and abs(pc['gps_ts'][2] - 123519.15) < 1e-6
      and math.isnan(pc['gps_ts'][3]) and math.isnan(pc['gps_ts'][4]),
      repr(list(pc['gps_ts'])))
# ?upto bounds the load at the last_id /data reported (live-study
# consistency); junk means no bound; 0 legitimately means nothing yet.
pts3 = decode_points(client.get('/points/saved1?upto=3').data)
check('points upto bound', pts3['n'] == 3 and pts3['last_id'] == 3
      and list(pts3['cols']['id']) == [1, 2, 3], repr(pts3['n']))
check('points upto junk = unbounded',
      decode_points(client.get('/points/saved1?upto=x').data)['n'] == 5)
pts0 = decode_points(client.get('/points/saved1?upto=0').data)
check('points upto 0 = empty blob', pts0['n'] == 0 and pts0['last_id'] == 0
      and all(len(a) == 0 for a in pts0['cols'].values()))
check('points missing study 404', client.get('/points/nope').status_code == 404)
# Batches: the columns must be identical however the rows are chunked.
_pb = db_map.POINTS_BATCH
db_map.POINTS_BATCH = 2
try:
    pts_b = decode_points(client.get('/points/saved1').data)
finally:
    db_map.POINTS_BATCH = _pb
check('points identical across batch boundaries',   # NaN bits compare equal
      all(pts_b['cols'][k].tobytes() == pc[k].tobytes() for k in pc),
      'batch=2 differs')

# Slow-path sentinels: a hand-built db without the daemon's NOT NULLs and
# with out-of-range counts — NULL heading/gps_ts → NaN, NULL fix → 255,
# adc clamped into uint16, NULL lat/lon → NaN. Nothing may 500 or shift.
_pp = os.path.join(STUDIES, 'permissive.db')
_pc = sqlite3.connect(_pp)
_pc.execute('CREATE TABLE points (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,'
            ' lat REAL, lon REAL, heading REAL, fix INTEGER,'
            ' adc0 INTEGER, adc1 INTEGER, adc2 INTEGER, adc3 INTEGER,'
            ' adc4 INTEGER, adc5 INTEGER, adc6 INTEGER, adc7 INTEGER,'
            ' gps_ts TEXT, vin REAL, temp REAL)')
_pc.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
_pc.executemany('INSERT INTO points (ts,lat,lon,heading,fix,adc0,adc1,adc2,adc3,'
                'adc4,adc5,adc6,adc7,gps_ts,vin,temp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [
    (1.0, 40.0, -119.0, 12.5, 4, 1, 2, 3, 4, 5, 6, 7, 8, '10.5', 12.0, 25.0),
    (2.0, None, None, None, None, 70000, -5, 3, 4, 5, 6, 7, 8, None, 12.0, 25.0),
    (3.0, 40.2, -119.2, 'north', 300, 65535, 0, 3, 4, 5, 6, 7, 8, 'x', 12.0, 25.0),
    # infinities and a >int64 count: int(inf) raises OverflowError, which
    # once escaped the converters and blanked the whole study
    (4.0, float('inf'), -119.3, float('inf'), float('inf'), float('inf'), 2 ** 40,
     3, 4, 5, 6, 7, 8, 'inf', 12.0, 25.0),
])
_pc.commit(); _pc.close()
r = client.get('/points/permissive')
pp = decode_points(r.data)['cols'] if r.status_code == 200 else {}
check('points permissive schema 200', r.status_code == 200, r.status_code)
check('points sentinels', bool(pp) and list(pp['id']) == [1, 2, 3, 4]
      and math.isnan(pp['lat'][1]) and math.isnan(pp['lon'][1])
      and abs(pp['lat'][2] - 40.2) < 1e-12
      and abs(pp['heading'][0] - 12.5) < 1e-6 and math.isnan(pp['heading'][1])
      and math.isnan(pp['heading'][2])                  # text → NaN
      and list(pp['fix']) == [4, 255, 255, 255]         # NULL, >255, inf → none
      and list(pp['adc0']) == [1, 65535, 65535, 0]      # inf count = junk → 0
      and list(pp['adc1']) == [2, 0, 0, 65535]          # 2**40 clamps
      and abs(pp['gps_ts'][0] - 10.5) < 1e-9 and math.isnan(pp['gps_ts'][1])
      and math.isnan(pp['gps_ts'][2])
      and pp['lat'][3] == math.inf and pp['heading'][3] == math.inf
      and pp['gps_ts'][3] == math.inf,                  # floats keep their inf
      repr({k: list(a) for k, a in pp.items()}))
os.unlink(_pp)
db_map._study_cache.clear()

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
colsl = db_map.load_point_columns(os.path.join(STUDIES, 'saved1.db'))
colsd = {spec[0]: a for spec, a in zip(db_map.POINT_COLUMNS, colsl)}


def _num(x):
    """Mirror the transport's tolerant float: NULL/junk gps_ts → None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


check('rows/columns equivalent', len(rows) == len(colsd['id']) == 5 and
      all(colsd['id'][i] == p['id'] and colsd['lat'][i] == p['lat']
          and colsd['lon'][i] == p['lon']
          and abs(colsd['heading'][i] - p['heading']) < 1e-4
          and colsd['fix'][i] == p['fix']
          and [colsd[f'adc{ch}'][i] for ch in range(8)] == p['adc']
          and (math.isnan(colsd['gps_ts'][i]) if _num(p['gps_ts']) is None
               else abs(colsd['gps_ts'][i] - _num(p['gps_ts'])) < 1e-9)
          for i, p in enumerate(rows)))
check('columns empty on missing db',
      all(len(a) == 0 for a in db_map.load_point_columns('/nonexistent.db')))
_tot, _gen = db_map.encode_points(db_map.load_point_columns('/nonexistent.db'))
_blob = b''.join(_gen)
check('empty blob well-formed', len(_blob) == _tot and _blob[:4] == b'MMP1'
      and json.loads(_blob[8:8 + struct.unpack_from('<I', _blob, 4)[0]])['n'] == 0)

# SSE replay is capped per tick: a stream opened far behind its study must
# not materialise the whole table (1.9 GB of dicts for 950k rows) — it
# catches up cap rows per tick and each event's id: is the batch's last row,
# so a reconnect resumes exactly there.
check('load_points honours cap',
      [p['id'] for p in db_map.load_points(os.path.join(STUDIES, 'saved1.db'),
                                           0, cap=2)] == [1, 2]
      and [p['id'] for p in db_map.load_points(os.path.join(STUDIES, 'saved1.db'),
                                               2, cap=2)] == [3, 4]
      and len(db_map.load_points(os.path.join(STUDIES, 'saved1.db'), 0)) == 5)
_cap, _per = db_map.STREAM_ROWS_CAP, db_map.SSE_PERIOD_S
db_map.STREAM_ROWS_CAP, db_map.SSE_PERIOD_S = 2, 0.01
try:
    r = client.get('/stream/saved1?since_id=0',
                   headers={'Accept': 'text/event-stream'})
    ticks = []
    for _ in range(3):
        raw = next(r.response).decode()
        ticks.append((raw.split('\n', 1)[0],
                      [p['id'] for p in json.loads(raw.split('data: ', 1)[1])['rows']]))
    r.response.close()
finally:
    db_map.STREAM_ROWS_CAP, db_map.SSE_PERIOD_S = _cap, _per
check('sse replay capped and resumable',
      ticks == [('id: 2', [1, 2]), ('id: 4', [3, 4]), ('id: 5', [5])], repr(ticks))

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
    'filter': 'cm', 'combine': '0', 'mode': 'mean',
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
check('render mode persisted', m.get('sl_mode') == 'mean',
      repr(m.get('sl_mode')))
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

# Cal overrides — the geometry a study is DRAWN with (sl_cal_*): a number
# stores, null clears that one key, a save without `cal` leaves them
# alone, junk 400s and nothing stamped by the board is ever touched.
# saved1 carries no board geometry; stamp the real thing so the "never
# touched" check compares values, not None == None
_c = sqlite3.connect(p_saved)
_c.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',
               [('coil_spacing_mm', '330'), ('coil_offset_fore_mm', '0'),
                ('coil_offset_right_mm', '500')])
_c.commit(); _c.close()
m = db_map.read_meta(p_saved)
stamped_before = {k: m.get(k) for k in ('coil_spacing_mm', 'coil_offset_fore_mm',
                                        'coil_offset_right_mm')}
check('fixture: board geometry stamped',
      stamped_before == {'coil_spacing_mm': '330', 'coil_offset_fore_mm': '0',
                         'coil_offset_right_mm': '500'}, stamped_before)
r = client.post('/settings/saved1',
                json={'cal': {'pitch_mm': 340, 'ant_mm': -480, 'heading_deg': 90}})
check('cal saved', r.status_code == 200 and r.get_json().get('ok') is True,
      (r.status_code, r.get_json()))
m = db_map.read_meta(p_saved)
check('cal persisted', (m.get('sl_cal_pitch_mm'), m.get('sl_cal_ant_mm'),
                        m.get('sl_cal_heading_deg')) == ('340', '-480', '90'),
      repr({k: v for k, v in m.items() if k.startswith('sl_cal')}))
r = client.post('/settings/saved1', json={'cal': {'ant_mm': None}})
m = db_map.read_meta(p_saved)
check('cal null clears one key, keeps the others',
      r.status_code == 200 and 'sl_cal_ant_mm' not in m
      and m.get('sl_cal_pitch_mm') == '340' and m.get('sl_cal_heading_deg') == '90',
      repr({k: v for k, v in m.items() if k.startswith('sl_cal')}))
r = client.post('/settings/saved1', json={'offset': 5})
m = db_map.read_meta(p_saved)
check('save without cal leaves cal alone', m.get('sl_cal_pitch_mm') == '340'
      and m.get('sl_cal_heading_deg') == '90' and m.get('sl_offset') == '5',
      repr({k: v for k, v in m.items() if k.startswith('sl_')}))
for bad in ('abc', [340], {'pitch_mm': 'x'}, {'pitch_mm': 10}, {'pitch_mm': 5001},
            {'pitch_mm': True}, {'pitch_mm': 10 ** 400}, {'ant_mm': 6000},
            {'ant_mm': [1]}, {'heading_deg': 361}, {'heading_deg': -361},
            # the test client round-trips these as bare NaN/Infinity tokens
            {'pitch_mm': float('nan')}, {'ant_mm': float('inf')},
            {'heading_deg': -float('inf')}):
    r = client.post('/settings/saved1', json={'cal': bad})
    check('cal junk rejected: %.24r' % (bad,), r.status_code == 400,
          (bad, r.status_code))
m = db_map.read_meta(p_saved)
check('cal junk left the stored cal untouched',
      m.get('sl_cal_pitch_mm') == '340' and m.get('sl_cal_heading_deg') == '90'
      and 'sl_cal_ant_mm' not in m,
      repr({k: v for k, v in m.items() if k.startswith('sl_cal')}))
check('cal never touches the stamped board geometry',
      {k: m.get(k) for k in stamped_before} == stamped_before,
      (stamped_before, {k: m.get(k) for k in stamped_before}))
r = client.post('/settings/saved1',
                json={'cal': {'pitch_mm': None, 'ant_mm': None, 'heading_deg': None}})
m = db_map.read_meta(p_saved)
check('cal all-null = back to stamped (no sl_cal_* left)',
      r.status_code == 200 and not any(k.startswith('sl_cal') for k in m),
      repr(sorted(k for k in m if k.startswith('sl_cal'))))

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

# Batch add — the review view's "add all" sends its picks biggest-first in
# ONE request: ids run in list order (id order IS the size rank) and the
# sidecar is written once. A bad pin anywhere fails the whole batch and
# mints nothing.
r = client.post('/targets/saved1', json={'targets': [
    {'lat': 40.41, 'lon': -119.41}, {'lat': 40.42, 'lon': -119.42},
    {'lat': 40.43, 'lon': -119.43}]})
d = r.get_json()
_new = {t['key']: t for t in d.get('targets', [])}
check('target batch add mints sequential keys in list order',
      r.status_code == 200
      and d.get('keys') == ['benchA:4', 'benchA:5', 'benchA:6']
      and d.get('key') == 'benchA:6'
      and [_new[k]['lat'] for k in d['keys']] == [40.41, 40.42, 40.43]
      and _new['benchA:4']['deletable'] is True, repr(d))
_sc_seq = json.loads(_sc_own.read_text())['seq']
check('target batch advances the sidecar seq once per pin', _sc_seq == 6, _sc_seq)
r = client.post('/targets/saved1', json={'targets': []})
check('target batch empty list 400', r.status_code == 400, r.status_code)
r = client.post('/targets/saved1', json={'targets': 'nope'})
check('target batch non-list 400', r.status_code == 400, r.status_code)
r = client.post('/targets/saved1', json={'targets': [
    {'lat': 40.5, 'lon': -119.5}, {'lat': 'junk'}]})
check('target batch with one bad pin 400 and mints nothing',
      r.status_code == 400
      and json.loads(_sc_own.read_text())['seq'] == 6, r.status_code)
r = client.post('/targets/saved1', json={'targets':
    [{'lat': 40.5, 'lon': -119.5}] * (db_map.TARGET_BATCH_MAX + 1)})
check('target batch over the cap 400', r.status_code == 400, r.status_code)
r = client.post('/targets/saved1', json={'targets': [
    {'lat': 40.5, 'lon': -119.5}, {'lat': 10 ** 400, 'lon': 0}]})
check('target batch with an unbounded int 400 and mints nothing',
      r.status_code == 400
      and json.loads(_sc_own.read_text())['seq'] == 6, r.status_code)
for k in ('benchA:4', 'benchA:5', 'benchA:6'):
    client.post('/targets/saved1/delete', json={'key': k})
check('target batch pins delete like any other',
      [t['key'] for t in client.get('/targets/saved1').get_json()['targets']]
      == ['benchA:2', 'benchA:3'],
      repr(client.get('/targets/saved1').get_json()))

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
# The landing page counts each study's dig targets (sidecars + legacy table)
# where the FW hash used to be; FW moved past the View Map button, so it
# hangs off the 800px kiosk screen and shows on a wider console.
_row = {s['id']: s for s in db_map.list_studies()}['saved1']
check("list_studies counts a study's dig targets (sidecar + legacy)",
      _row['targets'] == len(d['targets']) and _row['targets'] >= 2,
      (_row['targets'], len(d['targets'])))
_idx = client.get('/').data
_h0 = _idx.index(b'<th>Study</th>')
_hdr = _idx[_h0:_idx.index(b'</tr>', _h0)]
check('index columns: Points, Targets, View Map, then FW last',
      _hdr.index(b'Points') < _hdr.index(b'Targets') < _hdr.index(b'<th></th>') < _hdr.index(b'FW'))
_r0 = _idx.index(b'/view/saved1')
check('index row: View Map button precedes the FW cell',
      _r0 < _idx.index(b'class="fwcol"', _r0) < _idx.index(b'</tr>', _r0))
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
check('view ships multi-unit target UI', b'tgtStatus' in r.data
      and b'setStatus' in r.data and b'setTargets' in r.data
      and b'tgt-foreign' in r.data and b'tgt-cleared' in r.data
      and b'selTargetKey' in r.data and b'tgtDelBtn' in r.data
      and b'armedDelete' in r.data)
check('view ships pin-set chooser + truck list', b'pinSetSel' in r.data
      and b'onSetChange' in r.data and b'listPanel' in r.data
      and b'renderListPanel' in r.data and b'renumberPins' in r.data
      and b'saveAsSet' in r.data and b'mm_pinset_' in r.data)
_v = r.data.decode()
_snl = _v[_v.find('function startNewList()'):]
_snl = _snl[:_snl.find('\n}\n') + 3]
check('view ships "start new list" in the set chooser: asks a name, loud on failure',
      b'<option value="__new__">\xef\xbc\x8b start new list</option>' in r.data
      and b"if (key === '__new__') {" in r.data and 'newListBusy' in _snl
      and "prompt('Name for the new list:'" in _snl and 'if (name == null) return;' in _snl
      and 'const body = {keys: []};' in _snl and 'body.name = name.trim().slice(0, 60)' in _snl
      and _snl.index("prompt(") < _snl.index('newListBusy = true')     # cancel never trips the guard
      and "alert('start new list: ' + d.error)" in _snl
      and ".catch(e => alert('start new list failed: ' + e))" in _snl
      # the switch is blind to an answer naming a set it does not carry:
      # say so instead of parking curSet on a key the chooser cannot show
      and '(d.sets || []).some(s => s.key === d.set_key)' in _snl
      and _snl.index('s.key === d.set_key') < _snl.index('onSetChange(d.set_key)')
      and 'setPinnedUntil' not in _v,                                  # the 10 s guard is gone
      repr(_snl[:120]))
# Flask's jsonify sorts keys; the SSE tick must too, or the view's "same
# data" string compare misses on the first tick after every POST answer.
_dm = Path(db_map.__file__).read_text()
check('SSE tick serialises with sort_keys (matches jsonify; view skips redundant redraws)',
      "json.dumps(payload, sort_keys=True)" in _dm)
check('view ships per-channel bias panel', b'biasBtn' in r.data
      and b'biasPanel' in r.data and b'sl_bias8' in r.data
      and b'effZero8' in r.data)
check('view ships bias sliders + AGC auto', b'onBiasSlide' in r.data
      and b'autoBias' in r.data and b'biasS' in r.data)
# Cal: the button sits in the row ABOVE ±ch (source order = screen order),
# the panel carries its diagram + reset, Save View ships the departures,
# the banner flags an altered geometry, and the renderer applies the
# per-study overrides with antenna-from-centre = -right_mm.
_mc_src = (Path(__file__).parent / 'static' / 'map_core.js').read_text()
check('view ships coil calibration (Cal) panel', b'id="calBtn"' in r.data
      and b'id="calPanel"' in r.data and b'resetCal' in r.data
      and b'calSvg' in r.data and b'calHold(' in r.data
      and r.data.index(b'id="calBtn"') < r.data.index(b'id="biasBtn"')
      and b"fit('calPanel', mapTop + 10)" in r.data
      and b"how === 'clear' ? null : cal[k]" in r.data
      and ' \u00b7 alt coil cal'.encode('utf-8') in r.data
      and b'calBase = calFromGeom(MM.geomFromMeta(meta, {stamped: true}))' in r.data)
# the override/baseline branch, by its exact text — a swapped ternary would
# still contain every key name
check('renderer: sl_cal_* override the stamped values, antenna-from-centre = -right_mm',
      "const cal = k => stamped ? null : num(k);" in _mc_src
      and "right_mm:   ant != null ? -ant : (right != null ? right : 500)," in _mc_src
      and "rot_deg:    rot != null ? rot : (rot0 != null ? rot0 : 270)," in _mc_src)
# ...and for real, where a JS engine is at hand (macOS ships JavaScriptCore):
# the stamped view ignores the Cal keys, the default view applies them with
# the sign flip, a bare meta falls back to 330 / 500 / 270
_jsc = '/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc'
if os.path.exists(_jsc):
    _stub = ("var window = this, L = {}, console = {log(){}, warn(){}, error(){}},"
             " performance = {now(){ return 0; }},"
             " document = {createElement(){ return {getContext(){ return null; }}; }};\n")
    _probe = ("var m = {coil_spacing_mm: '330', coil_offset_fore_mm: '0',"
              " coil_offset_right_mm: '500', sl_heading_offset_deg: '270',"
              " sl_cal_pitch_mm: '340', sl_cal_ant_mm: '-480', sl_cal_heading_deg: '90'};\n"
              "print(JSON.stringify([MM.geomFromMeta(m), MM.geomFromMeta(m, {stamped: true}),"
              " MM.geomFromMeta({}), MM.geomFromMeta({sl_cal_ant_mm: 'x', sl_cal_pitch_mm: '0'})]));\n")
    _js = Path(tempfile.gettempdir()) / 'mm_geom_probe.js'
    _js.write_text(_stub + _mc_src + _probe)
    _out = subprocess.run([_jsc, str(_js)], capture_output=True, text=True, timeout=30)
    try:
        _g = json.loads(_out.stdout.strip().splitlines()[-1])
    except Exception:
        _g = None
    check('renderer geomFromMeta (JavaScriptCore): overrides, stamped view, defaults',
          _g == [{'spacing_mm': 340, 'fore_mm': 0, 'right_mm': 480, 'rot_deg': 90},
                 {'spacing_mm': 330, 'fore_mm': 0, 'right_mm': 500, 'rot_deg': 270},
                 {'spacing_mm': 330, 'fore_mm': 0, 'right_mm': 500, 'rot_deg': 270},
                 {'spacing_mm': 330, 'fore_mm': 0, 'right_mm': 500, 'rot_deg': 270}],
          (_g, _out.stderr[:300]))
else:
    print('  (skip) renderer geomFromMeta behavioural check: no JavaScriptCore here')
check('view ships HUD dead-stream gates', b'lastAdcAt' in r.data
      and b'lastNavAt' in r.data)
# nav lock on the approach arrow: badge under the arrow, arrow coloured by
# the shared classifier, amber/red pulse, transport gate feeds the classifier
check('view ships nav lock status on the approach arrow', b'id="tgtFix"' in r.data
      and b"MM.navFixStatus(nv, navFresh)" in r.data
      and b"fixEl.className = 'fix-' + st.level;" in r.data
      and b'#tgtFix.fix-warn' in r.data and b'#tgtFix.fix-bad' in r.data
      and b'fixpulse-bad' in r.data
      and r.data.index(b'id="tgtArrow"') < r.data.index(b'id="tgtFix"') < r.data.index(b'id="tgtDist"')
      and b"navLayer.update(Object.assign({}, lastNav, {stale: true}));" in r.data)
check('renderer ships navFixStatus + marker halo/tag',
      'function navFixStatus(nav, fresh)' in _mc_src
      and 'nav-halo nav-halo-${st.level}' in _mc_src and 'class="nav-tag"' in _mc_src
      and 'class="nav-up"' in _mc_src
      and 'navFixStatus,' in _mc_src.split('return {coilPositions')[1][:200])
_css = client.get('/static/style.css').data
check('stylesheet ships nav marker halo/tag rules', b'.nav-halo-bad' in _css
      and b'.nav-tag' in _css and b'@keyframes nav-halo-pulse' in _css
      and b'.nav-up' in _css and b'rotate(var(--unrot, 0deg))' in _css)
check('view: ranked pin swaps the lat/lon line for the size row',
      b"document.getElementById('tgtLL').style.display = ranked ? 'none' : '';" in r.data)
if os.path.exists(_jsc):
    # the classifier, for real: every state the HUD can show, plus the
    # marker html carrying the tag only when not RTK fixed
    _probe2 = (
        "var made = []; L = {divIcon(o){ return o; }, marker(ll, o){ var m = {ll, icon: o.icon,"
        " addTo(){ return m; }, setLatLng(x){ m.ll = x; }, setIcon(i){ m.icon = i; },"
        " bindTooltip(){}, getLatLng(){ return m.ll; }}; made.push(m); return m; }};\n"
        "var map = {removeLayer(){}};\n"
        "var base = {connected: true, lat: 1, lon: 2, stale: false, speed_mps: 0, cog_deg: null};\n"
        "var cases = [[Object.assign({}, base, {quality: 4}), true], [Object.assign({}, base, {quality: 5}), true],"
        " [Object.assign({}, base, {quality: 2}), true], [Object.assign({}, base, {quality: 1}), true],"
        " [Object.assign({}, base, {quality: 0}), true], [Object.assign({}, base, {quality: null}), true],"
        " [Object.assign({}, base, {quality: 4, stale: true}), true], [Object.assign({}, base, {quality: 4}), false],"
        " [{connected: false, error: 'x'}, true], [null, true], [Object.assign({}, base, {quality: 4}), undefined]];\n"
        "var out = cases.map(function(c){ var s = MM.navFixStatus(c[0], c[1]); return [s.code, s.level, s.label, s.ok]; });\n"
        "var nl = MM.NavLayer(map); nl.update(Object.assign({}, base, {quality: 5}));"
        " var h5 = made[0].icon.html; nl.update(Object.assign({}, base, {quality: 4})); var h4 = made[0].icon.html;\n"
        "nl.update(Object.assign({}, base, {quality: 1, speed_mps: 3, cog_deg: 45})); var h1 = made[0].icon.html;\n"
        "nl.update(Object.assign({}, base, {quality: 2})); var h2 = made[0].icon.html;\n"
        "print(JSON.stringify({out, tag5: h5.indexOf('nav-tag') >= 0 && h5.indexOf('RTK FLOAT') >= 0 && h5.indexOf('nav-halo-warn') >= 0"
        " && h5.indexOf('class=\"nav-up\"') >= 0 && h5.indexOf('border-color:#ffee00') >= 0,"
        " tag4: h4.indexOf('nav-tag') >= 0 || h4.indexOf('nav-halo') >= 0 || h4.indexOf('nav-up') >= 0,"
        " tag1: h1.indexOf('GPS · NO RTK') >= 0 && h1.indexOf('nav-halo-bad') >= 0 && h1.indexOf('rotate(45deg)') >= 0"
        " && h1.indexOf('border-color:#ff4444') >= 0 && h1.indexOf('color:#ff4444\">GPS') >= 0 && h1.indexOf('border-bottom:22px solid #ffaa00') >= 0,"
        " tag2: h2.indexOf('DGPS · NO RTK') >= 0 && h2.indexOf('border-color:#ff4444') >= 0 && h2.indexOf('background:#ffee00') >= 0}));\n")
    _js2 = Path(tempfile.gettempdir()) / 'mm_navfix_probe.js'
    _js2.write_text(_stub + _mc_src + _probe2)
    _out2 = subprocess.run([_jsc, str(_js2)], capture_output=True, text=True, timeout=30)
    try:
        _n = json.loads(_out2.stdout.strip().splitlines()[-1])
    except Exception:
        _n = None
    check('renderer navFixStatus (JavaScriptCore): every HUD state + marker tag only when not fixed',
          _n == {'out': [['fixed', 'ok', 'RTK FIXED', True], ['float', 'warn', 'RTK FLOAT', False],
                         ['nortk', 'bad', 'DGPS · NO RTK', False], ['nortk', 'bad', 'GPS · NO RTK', False],
                         ['nofix', 'bad', 'NO FIX', False], ['nofix', 'bad', 'NO FIX', False],
                         ['stale', 'bad', 'NAV STALE', False], ['dead', 'bad', 'NAV STALE', False],
                         ['down', 'bad', 'NAV DOWN', False], ['down', 'bad', 'NAV DOWN', False],
                         ['fixed', 'ok', 'RTK FIXED', True]],
                 'tag5': True, 'tag4': False, 'tag1': True, 'tag2': True},
          (_n, _out2.stderr[:300]))
else:
    print('  (skip) renderer navFixStatus behavioural check: no JavaScriptCore here')
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
check('sse tick carries sets', evt.get('sets') == [], repr(evt.get('sets')))
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

# ── Pin sets + status flags (sidecar v2) ─────────────────────────────────────
# A set is a named ORDERED list of OUR pin ids; the displayed number is the
# position, so a renumber is a reorder and never touches a key. Status
# (visited / found / skip) is per pin, set by any unit in its own file.
print('pin sets + status:')
_ps = 'pinsets_1700000000__benchA'
make_study(_ps, n_points=3, meta={'study_name': 'pinsets'})
_ps_own = Path(STUDIES) / f'{_ps}.targets__benchA.json'
_ps_mm9 = Path(STUDIES) / f'{_ps}.targets__mm9.json'
_ps_mm9.write_text(json.dumps({
    'version': 1, 'study': _ps, 'host': 'mm9', 'seq': 2,
    'targets': [{'id': 1, 'lat': 40.91, 'lon': -119.91, 'created_at': 50.0},
                {'id': 2, 'lat': 40.92, 'lon': -119.92, 'created_at': 51.0}],
    'cleared': {'mm9:2': 60.0}}))
d = client.get(f'/targets/{_ps}').get_json()
check('GET /targets carries sets (none yet) and status/mine per pin',
      d['sets'] == [] and len(d['targets']) == 2
      and d['targets'][1]['status'] == {'found': True, 'visited': False,
                                        'skip': False}
      and d['targets'][1]['mine'] == {'found': False, 'visited': False,
                                      'skip': False}, repr(d))
# our own pins: three, batch (ids 1..3)
r = client.post(f'/targets/{_ps}', json={'targets': [
    {'lat': 40.1, 'lon': -119.1}, {'lat': 40.2, 'lon': -119.2},
    {'lat': 40.3, 'lon': -119.3}]})
check('sets fixture: three own pins', r.get_json().get('keys')
      == ['benchA:1', 'benchA:2', 'benchA:3'], repr(r.get_json()))
# status flags: any unit's pin, only flags in the body change
r = client.post(f'/targets/{_ps}/status',
                json={'key': 'mm9:1', 'visited': True, 'skip': True})
d = r.get_json()
t = [x for x in d['targets'] if x['key'] == 'mm9:1'][0]
check('status sets visited+skip on a foreign pin, found untouched',
      r.status_code == 200
      and t['status'] == {'found': False, 'visited': True, 'skip': True}
      and t['mine']['visited'] is True, repr(t))
check('marks went to OUR sidecar, not theirs',
      json.loads(_ps_own.read_text())['marks'].get('mm9:1', {}).get('skip')
      is True and 'marks' not in json.loads(_ps_mm9.read_text()), None)
r = client.post(f'/targets/{_ps}/status', json={'key': 'mm9:1', 'skip': False})
t = [x for x in r.get_json()['targets'] if x['key'] == 'mm9:1'][0]
check('status clears one flag, leaves the other',
      t['status']['skip'] is False and t['status']['visited'] is True, repr(t))
r = client.post(f'/targets/{_ps}/status', json={'key': 'mm9:1', 'found': True})
t = [x for x in r.get_json()['targets'] if x['key'] == 'mm9:1'][0]
check('status found rides the legacy cleared mark',
      t['cleared'] is True and t['cleared_own'] is True
      and t['status']['found'] is True
      and json.loads(_ps_own.read_text())['cleared'].get('mm9:1') is not None,
      repr(t))
r = client.post(f'/targets/{_ps}/status', json={'key': 'mm9:2', 'found': False})
check("clearing another unit's found flag refused 403",
      r.status_code == 403 and 'another unit' in r.get_json()['error'],
      (r.status_code, r.get_json()))
r = client.post(f'/targets/{_ps}/status', json={'key': 'mm9:1'})
check('status with no flag 400', r.status_code == 400, r.status_code)
r = client.post(f'/targets/{_ps}/status', json={'key': 'benchA:99', 'skip': True})
check('status on a key naming no pin 404', r.status_code == 404, r.status_code)
r = client.post(f'/targets/{_ps}/status', data='[1]',
                content_type='application/json')
check('status non-dict body 400', r.status_code == 400, r.status_code)

# create a set: own pins by id, a foreign pin COPIED (one dig spot: the
# copy and its source share every status mark)
r = client.post(f'/targets/{_ps}/sets',
                json={'name': 'dig list A',
                      'keys': ['benchA:3', 'mm9:1', 'benchA:1']})
d = r.get_json()
check('create set: key, one copy, sets listed',
      r.status_code == 200 and d['set_key'] == 'benchA/1' and d['copied'] == 1
      and len(d['sets']) == 1 and d['sets'][0]['name'] == 'dig list A'
      and d['sets'][0]['own'] is True
      and d['sets'][0]['keys'] == ['benchA:3', 'benchA:4', 'benchA:1'],
      repr(d.get('sets')))
cp = [x for x in d['targets'] if x['key'] == 'benchA:4'][0]
check('copied pin sits at the source spot and shares its status',
      cp['lat'] == 40.91 and cp['lon'] == -119.91 and cp['root'] == 'mm9:1'
      and cp['status'] == {'found': True, 'visited': True, 'skip': False}
      and cp['mine']['found'] is True
      and 'benchA:4' not in json.loads(_ps_own.read_text())['cleared'],
      repr(cp))
check("copy remembers where it came from",
      [t for t in json.loads(_ps_own.read_text())['targets']
       if t['id'] == 4][0].get('from') == 'mm9:1', None)
# reorder = renumber: positions change, keys never do
r = client.post(f'/targets/{_ps}/sets/1',
                json={'keys': ['benchA:1', 'benchA:3', 'benchA:4']})
d = r.get_json()
check('reorder own set keeps keys, changes order',
      r.status_code == 200
      and d['sets'][0]['keys'] == ['benchA:1', 'benchA:3', 'benchA:4'],
      repr(d.get('sets')))
r = client.post(f'/targets/{_ps}/sets/1', json={'keys': ['benchA:1', 'mm9:2']})
check("reorder with another unit's pin refused 400 (save as new set)",
      r.status_code == 400 and 'new set' in r.get_json()['error'],
      (r.status_code, r.get_json()))
r = client.post(f'/targets/{_ps}/sets/1', json={'name': 'dig list A2'})
check('rename own set', r.get_json()['sets'][0]['name'] == 'dig list A2',
      repr(r.get_json().get('sets')))
r = client.post(f'/targets/{_ps}/sets/7', json={'name': 'x'})
check('update of a set we do not have 404', r.status_code == 404, r.status_code)
r = client.post(f'/targets/{_ps}/sets/1', json={})
check('update with nothing to change 400', r.status_code == 400, r.status_code)
# new pins join the set being viewed
r = client.post(f'/targets/{_ps}', json={'lat': 40.5, 'lon': -119.5, 'set': 1})
d = r.get_json()
check('add with set: new pin appended to that set',
      d['sets'][0]['keys'][-1] == d['key'] == 'benchA:5', repr(d.get('sets')))
r = client.post(f'/targets/{_ps}', json={'lat': 40.6, 'lon': -119.6, 'set': 9})
check('add with a set we do not have 404 and mints nothing',
      r.status_code == 404
      and json.loads(_ps_own.read_text())['seq'] == 5, r.status_code)
# delete drops the pin from every set and takes our marks on it along
client.post(f'/targets/{_ps}/status', json={'key': 'benchA:3', 'visited': True})
client.post(f'/targets/{_ps}/sets', json={'name': 'tmp', 'keys': ['benchA:3', 'benchA:1']})
r = client.post(f'/targets/{_ps}/delete', json={'key': 'benchA:3'})
d = r.get_json()
ks = {x['key']: x['keys'] for x in d['sets']}
check('delete removes the pin from every one of our sets and drops our marks',
      ks['benchA/1'] == ['benchA:1', 'benchA:4', 'benchA:5']
      and ks['benchA/2'] == ['benchA:1']
      and 'benchA:3' not in json.loads(_ps_own.read_text())['marks'],
      repr((ks, json.loads(_ps_own.read_text())['marks'])))
r = client.post(f'/targets/{_ps}/sets/2', json={'delete': True})
check('tmp set gone', r.status_code == 200 and 'benchA/2' not in
      [x['key'] for x in r.get_json()['sets']], r.status_code)
# another unit's set shows up read-only, in ITS key space
_ps_mm9.write_text(json.dumps({
    'version': 2, 'study': _ps, 'host': 'mm9', 'seq': 2,
    'targets': [{'id': 1, 'lat': 40.91, 'lon': -119.91, 'created_at': 50.0},
                {'id': 2, 'lat': 40.92, 'lon': -119.92, 'created_at': 51.0}],
    'cleared': {'mm9:2': 60.0}, 'marks': {'benchA:1': {'skip': True}},
    'sets': [{'sid': 3, 'name': 'their list', 'order': [2, 1, 9]}],
    'set_seq': 3}))
d = client.get(f'/targets/{_ps}').get_json()
theirs = [s for s in d['sets'] if s['key'] == 'mm9/3']
check("foreign set listed read-only with 'host:id' keys, ours first",
      len(theirs) == 1 and theirs[0]['own'] is False
      and theirs[0]['keys'] == ['mm9:2', 'mm9:1', 'mm9:9']
      and d['sets'][0]['own'] is True, repr(d['sets']))
b1 = [x for x in d['targets'] if x['key'] == 'benchA:1'][0]
check("another unit's skip mark shows on our pin, not as mine",
      b1['status']['skip'] is True and b1['mine']['skip'] is False, repr(b1))
r = client.post(f'/targets/{_ps}/status', json={'key': 'benchA:1', 'skip': False})
check("clearing their skip mark from here refused 403", r.status_code == 403,
      r.status_code)
r = client.post(f'/targets/{_ps}/sets/3', json={'name': 'mine now'})
check("updating another unit's set 404 (not ours)", r.status_code == 404,
      r.status_code)
# save-as-new-set from a foreign list: a pin we already copied is reused
# (benchA:4 stands for mm9:1), the rest copied, order kept, and 'resolved'
# tells the client which of our keys stands for each requested key
r = client.post(f'/targets/{_ps}/sets',
                json={'name': 'their list, ranked', 'keys': ['mm9:1', 'mm9:2']})
d = r.get_json()
_sid_ranked = int(d['set_key'].split('/')[1])
mine2 = [s for s in d['sets'] if s['key'] == d['set_key']][0]
check('save foreign list as own set: earlier copy reused, new one minted',
      d['copied'] == 1 and mine2['keys'] == ['benchA:4', 'benchA:6']
      and d['resolved'] == ['benchA:4', 'benchA:6']
      and [x for x in d['targets'] if x['key'] == 'benchA:6'][0]['status']['found']
      is True, repr((d.get('copied'), d.get('resolved'), d.get('sets'))))
r = client.post(f'/targets/{_ps}/sets',
                json={'name': 'again', 'keys': ['mm9:2', 'benchA:6', 'mm9:1']})
d = r.get_json()
check('saving the same spots again mints nothing: source + copy = one slot',
      d['copied'] == 0 and d['resolved'] == ['benchA:6', 'benchA:6', 'benchA:4']
      and [s for s in d['sets'] if s['key'] == d['set_key']][0]['keys']
      == ['benchA:6', 'benchA:4']
      and json.loads(_ps_own.read_text())['seq'] == 6, repr(d.get('sets')))
r = client.post(f'/targets/{_ps}/sets/{d["set_key"].split("/")[1]}',
                json={'delete': True})
r = client.post(f'/targets/{_ps}/sets', json={'name': 'x', 'keys': ['benchA:1', 'benchA:1']})
_sid_dup = int(r.get_json()['set_key'].split('/')[1])
check('duplicate keys collapse',
      [s for s in r.get_json()['sets'] if s['key'] == r.get_json()['set_key']][0]['keys']
      == ['benchA:1'], repr(r.get_json().get('sets')))
r = client.post(f'/targets/{_ps}/sets', json={'name': 'x', 'keys': ['nope:1']})
check('set with a key naming no pin 404', r.status_code == 404, r.status_code)
r = client.post(f'/targets/{_ps}/sets', json={'name': '', 'keys': []})
check('set with an empty name 400', r.status_code == 400, r.status_code)
# a set without a name is no longer refused: it gets 'list <sid>' (the
# chooser's "start new list") — exercised on its own fixture below, so the
# sid sequence of this one stays put
r = client.post(f'/targets/{_ps}/sets', json={'name': 'k', 'keys': ['bad key']})
check('set with a malformed key 400', r.status_code == 400, r.status_code)
r = client.post(f'/targets/{_ps}/sets/42', json={'delete': True})
check('delete of a set we do not have 404', r.status_code == 404, r.status_code)
r = client.post(f'/targets/{_ps}/sets/{_sid_ranked}', json={'delete': True})
check(f"delete of OUR sid {_sid_ranked} leaves the other unit's mm9/3 standing",
      r.status_code == 200
      and f'benchA/{_sid_ranked}' not in [s['key'] for s in r.get_json()['sets']]
      and 'mm9/3' in [s['key'] for s in r.get_json()['sets']],
      repr(r.get_json().get('sets')))
r = client.post(f'/targets/{_ps}/sets/{_sid_dup}', json={'delete': True})
d = r.get_json()
check('delete own set removes the list, keeps its pins',
      f'benchA/{_sid_dup}' not in [s['key'] for s in d['sets']]
      and any(x['key'] == 'benchA:6' for x in d['targets']), repr(d.get('sets')))
# a mangled peer file is filtered PER ENTRY: junk sets, junk order members,
# a junk flag value, a bool/zero/huge sid are dropped; the valid neighbours
# (a set, its int members, a real mark) survive
_ps_mm9.write_text(json.dumps({
    'version': 2, 'study': _ps, 'host': 'mm9', 'seq': 2,
    'targets': [{'id': 1, 'lat': 40.91, 'lon': -119.91, 'created_at': 50.0},
                {'id': 2, 'lat': 40.92, 'lon': -119.92, 'created_at': 51.0}],
    'cleared': {},
    'marks': {'benchA:1': 'yes', 'mm9:1': {'skip': True}},
    'sets': [{'sid': 'x'}, 7, {'sid': 4, 'order': 'no'}, {'sid': 0, 'order': [1]},
             {'sid': True, 'order': [1]}, {'sid': 10**12, 'order': [1]},
             {'sid': 5, 'name': 'ok', 'order': [1, 'x', None, 2.5, True, 2]}]}))
d = client.get(f'/targets/{_ps}').get_json()
theirs = [x for x in d['sets'] if x['host'] == 'mm9']
m1 = [x for x in d['targets'] if x['key'] == 'mm9:1'][0]
b1 = [x for x in d['targets'] if x['key'] == 'benchA:1'][0]
check('mangled peer entries skipped one by one, valid neighbours kept',
      any(x['key'] == 'mm9:2' for x in d['targets'])
      and [x['key'] for x in theirs] == ['mm9/5']
      and theirs[0]['keys'] == ['mm9:1', 'mm9:2']
      and m1['status']['skip'] is True and b1['status']['skip'] is False,
      repr((theirs, m1['status'], b1['status'])))
d = client.get(f'/data/{_ps}').get_json()
check('/data ships sets', isinstance(d.get('sets'), list) and len(d['sets']) >= 1,
      repr(d.get('sets')))

# ── "start new list": an empty own set, default-named, to Mark into ──────────
print('start new list:')
_nl = 'newlist_1700000001__benchA'
make_study(_nl, n_points=3, meta={'study_name': 'newlist'})
_nl_own = Path(STUDIES) / f'{_nl}.targets__benchA.json'
(Path(STUDIES) / f'{_nl}.targets__mm9.json').write_text(json.dumps({
    'version': 1, 'study': _nl, 'host': 'mm9', 'seq': 1,
    'targets': [{'id': 1, 'lat': 40.91, 'lon': -119.91, 'created_at': 50.0}],
    'cleared': {}}))
r = client.post(f'/targets/{_nl}/sets', json={'keys': []})
d = r.get_json()
check('new list: empty own set, named list 1, nothing copied, other pins untouched',
      r.status_code == 200 and d['set_key'] == 'benchA/1' and d['copied'] == 0
      and d['resolved'] == [] and len(d['sets']) == 1
      and d['sets'][0]['name'] == 'list 1' and d['sets'][0]['own'] is True
      and d['sets'][0]['keys'] == [] and d['sets'][0]['count'] == 0
      and [x['key'] for x in d['targets']] == ['mm9:1'],
      repr((r.status_code, d.get('sets'), d.get('error'))))
check('new list persisted in our sidecar',
      json.loads(_nl_own.read_text())['sets'][0]['name'] == 'list 1'
      and json.loads(_nl_own.read_text())['sets'][0]['order'] == [], None)
r = client.post(f'/targets/{_nl}', json={'lat': 40.5, 'lon': -119.5, 'set': 1})
d = r.get_json()
check('pin marked while the new list is shown joins it',
      d['key'] == 'benchA:1' and d['sets'][0]['keys'] == ['benchA:1'], repr(d.get('sets')))
r = client.post(f'/targets/{_nl}/sets', json={'keys': []})
d = r.get_json()
check('another new list numbers on (list 2), still empty',
      d['set_key'] == 'benchA/2' and d['sets'][1]['name'] == 'list 2'
      and d['sets'][1]['keys'] == [] and d['sets'][0]['keys'] == ['benchA:1'],
      repr(d.get('sets')))
r = client.post(f'/targets/{_nl}/sets', json={'name': 'mine', 'keys': []})
check('explicit name on an empty set kept', r.get_json()['sets'][2]['name'] == 'mine',
      repr(r.get_json().get('sets')))
r = client.post(f'/targets/{_nl}/sets', json={})
check('create set without keys 400', r.status_code == 400
      and 'keys' in r.get_json()['error'], (r.status_code, r.get_json()))
r = client.post(f'/targets/{_nl}/sets', json={'name': '', 'keys': []})
check('blank name still 400 (absent is the default, blank is a typo)', r.status_code == 400,
      r.status_code)
r = client.post(f'/targets/{_nl}/sets', json={'name': '  ', 'keys': []})
check('whitespace name 400', r.status_code == 400, r.status_code)

# ── status is per dig SPOT: a copy and its source are one pin ──────────────
# our benchA:4 is a copy of mm9:1, benchA:6 of mm9:2 (both still in set 1/2)
r = client.post(f'/targets/{_ps}/status', json={'key': 'benchA:4', 'found': False})
d = r.get_json()
own = json.loads(_ps_own.read_text())
check('un-finding the copy pops our mark on the source too',
      r.status_code == 200
      and all(x['status']['found'] is False for x in d['targets']
              if x['key'] in ('benchA:4', 'mm9:1'))
      and 'mm9:1' not in own['cleared'] and 'benchA:4' not in own['cleared'],
      repr(own['cleared']))
r = client.post(f'/targets/{_ps}/status',
                json={'key': 'benchA:6', 'found': True, 'skip': True})
d = r.get_json()
own = json.loads(_ps_own.read_text())
src = [x for x in d['targets'] if x['key'] == 'mm9:2'][0]
cpy = [x for x in d['targets'] if x['key'] == 'benchA:6'][0]
check('found/skip ticked on the copy show on the source, mine on both',
      src['status'] == {'found': True, 'visited': False, 'skip': True}
      and cpy['status'] == src['status'] and src['mine']['found'] is True
      and cpy['mine']['skip'] is True, repr((src, cpy)))
check("...and are written under the SOURCE key (a v1 reader sees the find)",
      'mm9:2' in own['cleared'] and 'benchA:6' not in own['cleared']
      and own['marks'].get('mm9:2', {}).get('skip') is True
      and 'benchA:6' not in own['marks'], repr((own['cleared'], own['marks'])))
# the other way: the source's owner finds it → our copy reads found, not ours
_mm9 = json.loads(_ps_mm9.read_text()); _mm9['cleared'] = {'mm9:1': 70.0}
_ps_mm9.write_text(json.dumps(_mm9))
d = client.get(f'/targets/{_ps}').get_json()
cpy = [x for x in d['targets'] if x['key'] == 'benchA:4'][0]
check("source found on its unit → our copy reads found, not as mine",
      cpy['status']['found'] is True and cpy['mine']['found'] is False, repr(cpy))
r = client.post(f'/targets/{_ps}/status', json={'key': 'benchA:4', 'found': False})
check("...so clearing it from the copy is refused 403", r.status_code == 403,
      r.status_code)
# layered: both units hold 'skip' on mm9:1 (theirs above, ours now); clearing
# ours is 200, theirs still shows; with our last own flag gone the marks
# entry is popped — no {'at': ts} litter riding the sync
r = client.post(f'/targets/{_ps}/status', json={'key': 'benchA:4', 'skip': True})
r = client.post(f'/targets/{_ps}/status',
                json={'key': 'mm9:1', 'skip': False, 'visited': False})
m1 = [x for x in r.get_json()['targets'] if x['key'] == 'mm9:1'][0]
own = json.loads(_ps_own.read_text())
check('layered skip: clearing ours 200, theirs still shows, our entry popped',
      r.status_code == 200 and m1['status']['skip'] is True
      and m1['mine'] == {'found': False, 'visited': False, 'skip': False}
      and 'mm9:1' not in own['marks'] and 'benchA:4' not in own['marks'],
      repr((r.status_code, m1, own['marks'])))
# a third unit copied OUR copy, and copied one of our own pins
_ps_mm2 = Path(STUDIES) / f'{_ps}.targets__mm2.json'
_ps_mm2.write_text(json.dumps({
    'version': 2, 'study': _ps, 'host': 'mm2', 'seq': 4,
    'targets': [{'id': 1, 'lat': 40.92, 'lon': -119.92, 'created_at': 90.0,
                 'from': 'benchA:6'},
                {'id': 2, 'lat': 40.1, 'lon': -119.1, 'created_at': 91.0,
                 'from': 'benchA:1'},
                {'id': 3, 'lat': 1.0, 'lon': 1.0, 'created_at': 92.0, 'from': 'mm2:4'},
                {'id': 4, 'lat': 1.0, 'lon': 1.0, 'created_at': 93.0, 'from': 'mm2:3'}],
    'cleared': {}, 'marks': {}, 'sets': [], 'set_seq': 0}))
d = client.get(f'/targets/{_ps}').get_json()
t = {x['key']: x for x in d['targets']}
check('copy of a copy roots at the original and shares its status; a from-cycle ends',
      t['mm2:1']['root'] == 'mm9:2' and t['mm2:1']['status']['found'] is True
      and t['mm2:2']['root'] == 'benchA:1'
      and t['mm2:3']['root'] in ('mm2:3', 'mm2:4'), repr({k: v['root'] for k, v in t.items()}))
r = client.post(f'/targets/{_ps}/sets', json={'name': 'via mm2', 'keys': ['mm2:2', 'mm2:1']})
d = r.get_json()
check("saving another unit's copies of OUR pins copies nothing",
      d['copied'] == 0 and d['resolved'] == ['benchA:1', 'benchA:6'], repr(d.get('resolved')))
r = client.post(f'/targets/{_ps}/sets/1', json={'keys': ['mm2:2', 'benchA:4']})
check("reorder may name another unit's copy of our pin (resolves to ours)",
      r.status_code == 200
      and [x for x in r.get_json()['sets'] if x['key'] == 'benchA/1'][0]['keys']
      == ['benchA:1', 'benchA:4'], repr(r.get_json().get('sets')))
_row = {s['id']: s for s in db_map.list_studies()}[_ps]
t = {x['key']: x for x in d['targets']}
check('landing counts a copy and its source as one pin',
      _row['targets'] == sum(1 for x in t.values()
                             if x['src'] != 'db' and x['from'] not in t) == 5,
      repr(_row['targets']))
_ps_mm2.unlink()

# ── guard paths on the new routes ──────────────────────────────────────────
codes, raw = [], []
for route in ('status', 'sets', 'sets/1'):
    codes.append(client.post(f'/targets/no_such_study/{route}',
                             json={'name': 'x', 'keys': [], 'key': 'benchA:1',
                                   'skip': True}).status_code)
    # raw session: 400 when its db exists, 404 when not — never a sidecar
    raw.append(client.post(f'/targets/{db_map.RAW_ID}/{route}',
                           json={'name': 'x', 'keys': [], 'key': 'benchA:1',
                                 'skip': True}).status_code)
    for body in ('null', '[1, 2]', '"x"'):
        codes.append(client.post(f'/targets/{_ps}/{route}', data=body,
                                 content_type='application/json').status_code)
check('sets/status: unknown study 404, raw refused, non-dict bodies 400, never 500',
      codes == [404, 400, 400, 400] * 3 and all(c in (400, 404) for c in raw)
      and not list(Path(STUDIES).glob(f'{db_map.RAW_ID}.targets__*.json')),
      (codes, raw))
_seq = json.loads(_ps_own.read_text())['seq']
codes = [client.post(f'/targets/{_ps}', json={'lat': 40.7, 'lon': -119.7,
                                              'set': v}).status_code
         for v in ('x', 0, -1, 1.5, True, 10**12)]
check('add with a malformed set field 400 and mints nothing',
      codes == [400] * 6 and json.loads(_ps_own.read_text())['seq'] == _seq, codes)
r = client.post(f'/targets/{_ps}', json={'targets': [{'lat': 40.71, 'lon': -119.71},
                                                     {'lat': 40.72, 'lon': -119.72}],
                                         'set': 1})
d = r.get_json()
check('batch add with set appends every minted id in order',
      r.status_code == 200 and len(d['keys']) == 2
      and [x for x in d['sets'] if x['key'] == 'benchA/1'][0]['keys'][-2:] == d['keys'],
      repr(d.get('sets')))
r = client.post(f'/targets/{_ps}/sets/1', json={'name': 'n' * 61})
check('rename over 60 chars 400', r.status_code == 400, r.status_code)
r = client.post(f'/targets/{_ps}/sets/1', json={'keys': ['benchA:99']})
check('reorder naming an id of ours that does not exist 400',
      r.status_code == 400, r.status_code)

# ── a torn peer file reads as empty, never a 500 ──────────────────────────
_ps_mm9.write_text(json.dumps({'version': 2, 'study': _ps, 'host': 'mm9',
                               'seq': 1, 'targets': 5, 'cleared': [[1]],
                               'marks': 3, 'sets': {'a': 1}}))
codes = [client.get('/').status_code, client.get(f'/targets/{_ps}').status_code,
         client.get(f'/data/{_ps}').status_code]
d = client.get(f'/targets/{_ps}').get_json()
check('peer file with non-list targets / non-dict cleared: landing, /targets, /data 200',
      codes == [200, 200, 200] and any(x['key'] == 'benchA:1' for x in d['targets'])
      and not any(x['src'] == 'mm9' for x in d['targets']), repr((codes, d.get('targets'))))
_ps_mm9.write_text(json.dumps(_mm9))

# ── own sidecar: one id predicate for every reader, set_seq floor ──────────
_ps_own.write_text(json.dumps({
    'version': 2, 'study': _ps, 'host': 'benchA', 'seq': 3,
    'targets': [{'id': i, 'lat': 40.0 + i, 'lon': -119.0, 'created_at': 1.0}
                for i in (1, 2, 3)],
    'cleared': {}, 'marks': {},
    'sets': [{'sid': 0, 'name': 'zero', 'order': [1]},
             {'sid': True, 'name': 'bool', 'order': [1]},
             {'sid': 10**12, 'name': 'huge', 'order': [1]},
             {'sid': 5, 'name': 'five', 'order': [1, 2, True, 0, 10**12]}],
    'set_seq': 0}))
d = client.get(f'/targets/{_ps}').get_json()
check('own sets with a zero/bool/huge sid are invisible to every reader alike',
      [x['key'] for x in d['sets'] if x['own']] == ['benchA/5']
      and d['sets'][0]['keys'] == ['benchA:1', 'benchA:2'], repr(d['sets']))
r = client.post(f'/targets/{_ps}/sets', json={'name': 'six', 'keys': ['benchA:1']})
check('mangled set_seq floors at the highest existing sid (no set-key reuse)',
      r.status_code == 200 and r.get_json()['set_key'] == 'benchA/6'
      and 'benchA/5' in [x['key'] for x in r.get_json()['sets']],
      repr(r.get_json().get('set_key')))
own = json.loads(_ps_own.read_text())
check('the rewrite dropped only the unusable sets',
      [x['sid'] for x in own['sets']] == [5, 6] and own['set_seq'] == 6, repr(own['sets']))
own['set_seq'] = 5 * 10**9; own['sets'] = 'junk'
_ps_own.write_text(json.dumps(own))
r = client.post(f'/targets/{_ps}/sets', json={'name': 'one', 'keys': ['benchA:1']})
check('giant set_seq scalar clamped, non-list sets dropped, nothing 500s',
      r.status_code == 200 and r.get_json()['set_key'] == 'benchA/1',
      (r.status_code, r.get_json().get('set_key')))

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
_units = sorted(p.name[:-3] for p in db_map._REPO_DIR.glob('*.service.in'))
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

# The RTK units hold in 'activating' (ExecStartPre reachability gate)
# whenever no base station is on the network — the panel must show that as
# a steady amber 'wait' dot, distinct from green (feeding) / grey (off) /
# red (failed), in BOTH the Jinja render and the SSE updater.
_real_svc_fn, _real_ip_fn = db_map.service_states, db_map.local_ipv4s
db_map.service_states = lambda: {'jlw_rover_rtk.service': 'activating',
                                 'jlw_metalmap.service': 'active'}
db_map.local_ipv4s = lambda: ['wlan0 192.168.4.1']
try:
    r = client.get('/')
finally:
    db_map.service_states, db_map.local_ipv4s = _real_svc_fn, _real_ip_fn
check("index: 'activating' unit renders the amber wait dot",
      b'class="dot wait"' in r.data and b'class="dot on"' in r.data)
check("index: SSE dot updater knows the 'activating' state",
      b"state === 'activating' ? ' wait'" in r.data)

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
# Two GPS systems, two labeled fixes: the heading GPS readouts (fq-text +
# fixBadge, both tooltipped with jlw_rover_rtk) carry the HDG prefix in JS,
# and the nav chip's tooltip names jlw_rover_rtk_nav. 'RTK Fixed x2' must
# be attributable at a glance.
check('live view: both GPS fixes labeled (HDG prefix + service tooltips)',
      r.data.count(b"'HDG '") == 2 and r.data.count(b'jlw_rover_rtk"') == 2
      and b'jlw_rover_rtk_nav"' in r.data)
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
# Pulse-spacing row (raster only): +/- buttons drive the fw h/n keys, the
# value renders st.timing.pulse_us; the rate slider pre-clamps its walk to
# the sweep-fit (8 slots per interval) via the slotUsNow tracker.
check('raw ships pulse-spacing wiring',
      b'id="pulseRow"' in r.data and b'id="pulseVal"' in r.data
      and b"pulseCmd('h')" in r.data and b"pulseCmd('n')" in r.data
      and b'pulse_us' in r.data and b'slotUsNow' in r.data
      and b'PULSE_STEPS' in r.data)
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
      cfg['nav_port'] == 50010
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

# ── unit templates vs the installer vs requirements.txt ─────────────────────
# install_pi_system.sh renders the *.service.in / *.timer.in templates and
# builds /opt/metal_mapper/venv from requirements.txt. Three things must
# stay in step: every @MM_*@ placeholder a template uses is one the
# installer fills (and vice versa — no dead plumbing); no unit reaches into
# a home directory (a virgin Pi has nothing there — the gnss tools moved
# into the venv in Sept 2026 after exactly that bit a fresh install); and
# every venv tool a unit runs is provided by a requirements.txt entry.
print('unit templates:')
import re as _re
_repo = db_map._REPO_DIR
_tpls = sorted(list(_repo.glob('*.service.in')) + list(_repo.glob('*.timer.in')))
_filled = set(_re.findall(r's\|(@MM_[A-Z_]+@)\|', (_repo / 'install_pi_system.sh').read_text()))
_req_names = {_re.split(r'[<>=!~ ]', ln, 1)[0].strip().lower()
              for ln in (_repo / 'requirements.txt').read_text().splitlines()
              if ln.strip() and not ln.startswith('#')}
_venv_tool_dist = {'gunicorn': 'gunicorn',
                   'gnssserver': 'pygnssutils', 'gnssntripclient': 'pygnssutils'}
_used, _home, _bad_exec, _venv_tools = set(), [], [], {}
for _t in _tpls:
    _body = '\n'.join(l for l in _t.read_text().splitlines()
                      if not l.lstrip().startswith('#'))
    _used |= set(_re.findall(r'@MM_[A-Z_]+@', _body))
    if _re.search(r'@MM_HOME@|/home/|(^|[\s=])~/', _body):
        _home.append(_t.name)
    for _m in _re.finditer(r'^Exec\w*=(?:[-+!:]|@(?!MM_))*(\S+)', _body, _re.M):
        _exe = _m.group(1)
        if not _exe.startswith(('/opt/metal_mapper/', '@MM_REPO@/', '/bin/', '/usr/bin/')):
            _bad_exec.append(f'{_t.name}: {_exe}')
        if _exe.startswith('/opt/metal_mapper/venv/bin/'):
            _venv_tools[_exe.rsplit('/', 1)[1]] = _t.name
check('templates: at least the web app, kiosk and rover units exist', len(_tpls) >= 6, len(_tpls))
check('templates: every placeholder used is one the installer fills',
      _used <= _filled, repr(sorted(_used - _filled)))
check('installer: fills no placeholder that no template uses',
      _filled <= _used, repr(sorted(_filled - _used)))
check('templates: nothing runs from a home directory', not _home, repr(_home))
check('templates: Exec* binaries live in /opt/metal_mapper, the checkout or /usr/bin',
      not _bad_exec, repr(_bad_exec))
_missing = {t: _venv_tool_dist.get(t) for t in _venv_tools
            if _venv_tool_dist.get(t) not in _req_names}
check('templates: every venv tool a unit runs is in requirements.txt',
      bool(_venv_tools) and not _missing, repr(_missing or _venv_tools))
# A head unit without a GPS on the UART a rover unit wants (a bench Pi, a
# GPS unplugged at boot) must SKIP that unit, not fail it (gnssserver
# relaunching every 2 s into systemd's start-rate limit: a red dot for
# good and boot-time churn) or gate it forever (the rtk units' ExecStartPre
# loop: amber for good). ConditionPathExists= on the very device the unit
# talks to does that; it is only checked at boot / manual start, so a USB
# re-enumeration mid-survey still gets Restart='s relaunch loop.
_rover_bad = []
for _t in _tpls:
    if not _t.name.startswith('jlw_rover_'):
        continue
    _body = '\n'.join(l for l in _t.read_text().splitlines()
                      if not l.lstrip().startswith('#'))
    _cond = _re.findall(r'^ConditionPathExists=(\S+)$', _body, _re.M)
    _devs = set(_re.findall(r'/dev/ttyUSB\d+', _body))
    if len(_cond) != 1 or _devs != {_cond[0]}:
        _rover_bad.append(f'{_t.name}: condition={_cond} devices={sorted(_devs)}')
check('rover units: ConditionPathExists= names the one UART the unit uses (skip, not fail, without it)',
      not _rover_bad and sum(t.name.startswith('jlw_rover_') for t in _tpls) == 4,
      repr(_rover_bad))
# The kiosk launcher resolves the browser (Trixie: chromium; older images:
# chromium-browser) instead of hardcoding one path; the installer's package
# gate covers every apt package the head unit needs beyond a stock image.
_ui = (_repo / 'start_local_ui.sh').read_text()
check('kiosk: start_local_ui.sh resolves chromium, no hardcoded /usr/bin path',
      'command -v chromium' in _ui and '/usr/bin/chromium' not in _ui)
# Boot ordering: the kiosk raced the backend and sat on a white page, then
# nginx's error page, for good. The kiosk unit orders After= (and Wants=,
# never Requires=) the web app and nginx; the web app's "started" has to
# mean "the worker answers" — gunicorn's READY=1 only means "socket bound"
# — so its unit probes /healthz in ExecStartPost; the launcher additionally
# polls the landing page (bounded, under the unit's 90 s start timeout)
# before chromium loads it.
_unit = lambda n: '\n'.join(l for l in (_repo / n).read_text().splitlines()
                            if not l.lstrip().startswith('#'))
_ui_unit, _web_unit = _unit('jlw_ui_mm.service.in'), _unit('jlw_metalmap.service.in')
_after = _re.search(r'^After=(.*)$', _ui_unit, _re.M)
_wants = _re.search(r'^Wants=(.*)$', _ui_unit, _re.M)
check('kiosk unit: After= web app + nginx (+ the display), Wants= not Requires=',
      _after and {'jlw_metalmap.service', 'nginx.service', 'lightdm.service'}
      <= set(_after.group(1).split())
      and _wants and 'jlw_metalmap.service' in _wants.group(1).split()
      and 'Requires=' not in _ui_unit, repr((_after, _wants)))
import inspect as _inspect
import gunicorn.arbiter as _garb
# READY=1 goes out in Arbiter.start() — socket bound — and the worker that
# imports the app is only forked by manage_workers() afterwards: that is
# why Type=notify alone left After= meaning "accepting, not answering".
_run_src = _inspect.getsource(_garb.Arbiter.run)
check('gunicorn: READY=1 is sent in start(), before manage_workers() forks the importing worker',
      'READY=1' in _inspect.getsource(_garb.Arbiter.start)
      and 'self.start()' in _run_src and 'self.manage_workers()' in _run_src
      and _run_src.index('self.start()') < _run_src.index('self.manage_workers()'))
# So the unit stays Type=notify (socket bound before probing starts) and
# adds an ExecStartPost loop on /healthz: systemd runs it after READY=1 and
# holds the unit in "activating" — After= dependents wait — until it exits.
# Bounded by TimeoutStartSec (then failed → Restart=). Absolute paths: the
# unit's PATH is the venv only. -f so an HTTP error is "not yet"; a
# --max-time so a probe parked in the accept queue gives up and retries.
_post = _re.search(r'^ExecStartPost=(.*)$', _web_unit, _re.M)
_tos = _re.search(r'^TimeoutStartSec=(\S+)$', _web_unit, _re.M)
_post_s = _post.group(1) if _post else ''
check('web unit: Type=notify + ExecStartPost loops on /healthz until the worker answers, bounded by TimeoutStartSec',
      _re.search(r'^Type=notify$', _web_unit, _re.M) is not None
      and 'http://127.0.0.1:8000/healthz' in _post_s
      and _re.search(r'\buntil\b.*\bdo\b.*\bsleep\b.*\bdone\b', _post_s)
      and '/usr/bin/curl -f' in _post_s and _re.search(r'--max-time\s+[1-9]\d*\b', _post_s)
      and not _re.search(r'(?<![/\w])(curl|sleep)\s', _post_s)       # bare names: venv-only PATH
      and _tos is not None and _re.search(r'^Restart=on-failure$', _web_unit, _re.M),
      repr((_post_s[:90], _tos and _tos.group(1))))
# The probe's target touches nothing slow: no study scan, no daemon status.
_saved_ls, _saved_ds = db_map.list_studies, db_map.get_daemon_status
def _boom(*a, **k):
    raise AssertionError('/healthz touched something slow')
db_map.list_studies = db_map.get_daemon_status = _boom
try:
    _hz = client.get('/healthz')
finally:
    db_map.list_studies, db_map.get_daemon_status = _saved_ls, _saved_ds
check('/healthz: 200 {ok, git} without a study scan or daemon status',
      _hz.status_code == 200 and _hz.get_json().get('ok') is True
      and 'git' in _hz.get_json(), repr((_hz.status_code, _hz.get_json())))
# Concurrent landing renders during a cold scan (kiosk probe + nginx's
# retrying page + a laptop) must queue behind ONE scan, not each start their own.
check('list_studies: renders serialise on _scan_lock (one cold scan, never stacked)',
      isinstance(getattr(db_map, '_scan_lock', None), type(db_map.threading.Lock()))
      and 'with _scan_lock' in _inspect.getsource(db_map.list_studies))
# One probe at a time, each given the REST of the window: a short per-probe
# timeout abandoned a slow first render and re-issued it every few seconds,
# stacking cold study scans until the worker's pool was full (the kiosk
# then loaded into nginx's 504 and stayed there).
# Matched on the comment-stripped script with whitespace/quoting-tolerant
# regexes — a comment mentioning --kiosk or xset must not flip the checks.
_ui_code = '\n'.join(l for l in _ui.splitlines() if not l.lstrip().startswith('#'))
_gate = _re.search(r'^WAIT_S=(\d+)\b', _ui_code, _re.M)
_pos = lambda pat: (lambda m: m.start() if m else -1)(_re.search(pat, _ui_code))
_p_left, _p_probe = _pos(r'left=\$\(\(\s*deadline\s*-\s*SECONDS\s*\)\)'), \
    _pos(r'probe\s+"?\$left"?\s*&&\s*break')
_p_xset, _p_kiosk = _pos(r'xset\s+s\s+off'), _pos(r'--kiosk')
check('kiosk: launcher waits on http://localhost/ before chromium, one probe at a time, bounded < 90 s',
      _gate is not None and 0 < int(_gate.group(1)) < 90
      and 'URL=http://localhost/' in _ui_code
      and _re.search(r'deadline=\$\(\(\s*SECONDS\s*\+\s*WAIT_S\s*\)\)', _ui_code)
      # the remaining window is read once and never passed as 0 (= no timeout)
      and _p_left >= 0 and _re.search(r'\[\s+"?\$left"?\s+-gt\s+0\s+\]\s*\|\|\s*break', _ui_code)
      and 0 <= _p_left < _p_probe < _p_kiosk
      and _re.search(r'--max-time\s+"?\$1"?', _ui_code) and _re.search(r'-T\s+"?\$1"?', _ui_code)
      and 'curl -sf' in _ui_code and 'wget -q' in _ui_code, repr(_gate))
# X can come up DURING the wait (lightdm active = the daemon, not the
# session): blanking-off must be asked after it, right before chromium.
check('kiosk: xset (screen blanking off) runs after the wait, right before chromium',
      0 <= _p_probe < _p_xset < _p_kiosk, repr((_p_probe, _p_xset, _p_kiosk)))
# nginx's own error pages are the one thing a kiosk page can park on for
# good (it never reloads itself): 502/503/504 get a self-retrying page.
_ngx = '\n'.join(l for l in (_repo / 'nginx' / 'metal-mapper').read_text().splitlines()
                 if not l.lstrip().startswith('#'))
_ngx_body = _re.search(r"return 503 '([^']*)';", _ngx)
_ngx_ep = _re.search(r'^\s*error_page((?:\s+\d+)+)\s+@starting;', _ngx, _re.M)
_ngx_loc = _ngx[_ngx.find('location @starting'):_ngx.find('return 503')]
check('nginx: 502/503/504 answered by a self-retrying page, app errors untouched',
      _ngx_ep is not None and {'502', '504'} <= set(_ngx_ep.group(1).split())
      and 'location @starting' in _ngx and _ngx_body is not None
      and 'http-equiv="refresh"' in _ngx_body.group(1)
      and '$' not in _ngx_body.group(1)          # nginx expands $vars in return strings
      # a named location keeps the request URI: without `types { }` a
      # mime-mapped extension on it would beat default_type and the HTML
      # body would go out as text/plain — the refresh never runs
      and _re.search(r'types\s*\{\s*\}', _ngx_loc) and 'default_type text/html;' in _ngx_loc
      and not _re.search(r'^\s*proxy_intercept_errors\s+on\s*;', _ngx, _re.M),
      repr(_ngx_body and _ngx_body.group(1)[:80]))
_inst = (_repo / 'install_pi_system.sh').read_text()
_gate_missing = [p for p in ('nginx', 'chromium', 'rsync', 'python3-venv', 'git', 'lightdm',
                             'curl')
                 if not _re.search(r'missing\+=\([^)]*\b' + _re.escape(p) + r'\b', _inst)]
check('installer: package gate covers nginx, chromium, rsync, python3-venv, git, lightdm, curl',
      not _gate_missing, repr(_gate_missing))

# ── study index: the landing page's per-study summaries persist ───────────────
# Without it the first render after boot COUNTed every study db on a cold
# SD card and the kiosk parked on nginx's timeout page.

print('study index:')
_idx_dir = tempfile.mkdtemp(prefix='dbmap_idx_')
_idx_saved_over = dict(db_map._cli_overrides)
db_map._cli_overrides['studies_dir'] = _idx_dir


def _idx_study(name, n):
    path = os.path.join(_idx_dir, f'{name}.db')
    conn = sd.db_open(path)
    sd.insert_rows(conn, [{'lat': 40.0, 'lon': -119.0, 'heading': 90.0,
                           'fix': 4, 'adc': [1000] * 8, 'gps_ts': '1.0'}
                          for _ in range(n)], 12.4, 25.0)
    sd.meta_set(conn, 'study_name', name)
    conn.close()
    return path


_idx_a_id = f'idx_a_1700000000__{db_map.HOST_TAG}'
_idx_b_id = f'idx_b_1700000001__{db_map.HOST_TAG}'
_idx_a, _idx_b = _idx_study(_idx_a_id, 3), _idx_study(_idx_b_id, 0)
_idx_file = os.path.join(_idx_dir, '.study_index.json')
db_map._study_cache.clear()
db_map._study_cache_loaded = False
_ls = {s['id']: s['count'] for s in db_map.list_studies()}
check('study index: a render writes <studies_dir>/.study_index.json',
      os.path.exists(_idx_file) and _ls == {_idx_a_id: 3, _idx_b_id: 0}, repr(_ls))
_idx_json = json.load(open(_idx_file))
check('study index: keyed by db path -> [signature, summary]',
      set(_idx_json) == {_idx_a, _idx_b} and _idx_json[_idx_a][1]['count'] == 3
      and len(_idx_json[_idx_a][0]) == 2, repr(_idx_json)[:200])
# a fresh process serves unchanged studies from the file without opening a
# db — the purge's _indexed_count first (that is the boot order: index()
# purges before it lists), then the render. The stub raises a
# BaseException: every db_ro call site catches Exception and would turn an
# AssertionError into the 'unreadable db' fallback, hiding the open.
db_map._study_cache.clear()
db_map._study_cache_loaded = False
_real_db_ro = db_map.db_ro


class _DbOpened(BaseException):
    pass


def _no_db(path):
    raise _DbOpened('db opened: ' + str(path))


db_map.db_ro = _no_db
try:
    _cnt_a = db_map._indexed_count(Path(_idx_a))
    _ls2 = {s['id']: s['count'] for s in db_map.list_studies()}
    _idx_err = None
except _DbOpened as e:
    _ls2, _cnt_a, _idx_err = None, None, e
finally:
    db_map.db_ro = _real_db_ro
check('study index: cold process renders unchanged studies with no db opened',
      _idx_err is None and _ls2 == _ls and _cnt_a == 3, repr(_idx_err or _ls2))
check('study index: foreign_owner is computed per render, not persisted',
      all('foreign_owner' not in v[1] for v in json.load(open(_idx_file)).values())
      and all(s['foreign_owner'] is None for s in db_map.list_studies()))
# a changed db (new rows) is re-counted and the file rewritten
time.sleep(0.02)
conn = sd.db_open(_idx_a)
sd.insert_rows(conn, [{'lat': 40.0, 'lon': -119.0, 'heading': 90.0, 'fix': 4,
                       'adc': [1000] * 8, 'gps_ts': '2.0'}], 12.4, 25.0)
conn.close()
_ls3 = {s['id']: s['count'] for s in db_map.list_studies()}
check('study index: a changed db is re-counted and the file rewritten',
      _ls3[_idx_a_id] == 4 and json.load(open(_idx_file))[_idx_a][1]['count'] == 4,
      repr(_ls3))
# a torn file is ignored and rebuilt
with open(_idx_file, 'w') as f:
    f.write('{"not": json')
db_map._study_cache.clear()
db_map._study_cache_loaded = False
_ls4 = {s['id']: s['count'] for s in db_map.list_studies()}
check('study index: a torn index file is ignored and rebuilt',
      _ls4 == _ls3 and json.load(open(_idx_file))[_idx_a][1]['count'] == 4, repr(_ls4))
# purge pass 1 trusts the index: one real COUNT, for the re-verify of the
# 0-point candidate only; the non-empty study is never opened. Cold, as at
# boot: purge runs before any render has loaded the index in-process.
db_map._study_cache.clear()
db_map._study_cache_loaded = False
_pc_calls = []
_real_pc = db_map._points_count


def _pc(p):
    _pc_calls.append(str(p))
    return _real_pc(p)


db_map._points_count = _pc
try:
    _idx_removed = db_map.purge_empty_studies()
finally:
    db_map._points_count = _real_pc
check('study index: purge pass 1 reads the index, re-counts only the candidate',
      _idx_removed == [_idx_b_id] and _pc_calls == [_idx_b]
      and not os.path.exists(_idx_b) and os.path.exists(_idx_a),
      repr((_idx_removed, _pc_calls)))
db_map._cli_overrides.clear()
db_map._cli_overrides.update(_idx_saved_over)
db_map._study_cache.clear()
db_map._study_cache_loaded = False

# ── coverage outlines: where the team has already surveyed ──────────────────

print('coverage outlines:')
_R = db_map.COVERAGE_RADIUS_M


def _ring_area(ring):          # shoelace in [lon, lat]; the sign is the orientation
    return sum(ring[k][0] * ring[k + 1][1] - ring[k + 1][0] * ring[k][1]
               for k in range(len(ring) - 1)) / 2


def _bbox(ring):
    xs = [q[0] for q in ring]
    ys = [q[1] for q in ring]
    return min(xs), min(ys), max(xs), max(ys)


_lat0, _lon0 = 40.0, -119.0
_mlat = db_map._M_PER_DEG_LAT
_mlon = _mlat * math.cos(math.radians(_lat0))


def _fix(x_m, y_m):            # (lat, lon) of a point x m east / y m north
    return (_lat0 + y_m / _mlat, _lon0 + x_m / _mlon)


check('coverage: no fixes → no rings', db_map.coverage_rings([]) == [])
# a straight 30 m east-west track: one closed counter-clockwise ring about
# 30 + 2R long and 2R tall, with every fix inside it
_line = [_fix(x * 0.5, 0.0) for x in range(61)]
_rings = db_map.coverage_rings(_line)
_bb = _bbox(_rings[0]) if _rings else (0, 0, 0, 0)
_w, _h = (_bb[2] - _bb[0]) * _mlon, (_bb[3] - _bb[1]) * _mlat
check('coverage: straight track → one closed CCW ring',
      len(_rings) == 1 and _rings[0][0] == _rings[0][-1] and _ring_area(_rings[0]) > 0,
      repr((len(_rings), _rings[0][:3] if _rings else None)))
check('coverage: the ring spans the track plus the reach',
      30 + 2 * _R - 2 <= _w <= 30 + 2 * _R + 2 and 2 * _R - 1 <= _h <= 2 * _R + 2,
      repr((_w, _h)))
check('coverage: every fix lies inside the ring',
      all(_bb[0] < lo < _bb[2] and _bb[1] < la < _bb[3] for la, lo in _line))
# a 40 m square loop: an outer CCW ring plus one CW hole around the
# un-driven middle — the hole is what tells a crew the middle is still open
_sq = ([_fix(x * 0.5, 0.0) for x in range(81)] + [_fix(40.0, y * 0.5) for y in range(81)]
       + [_fix(x * 0.5, 40.0) for x in range(81)] + [_fix(0.0, y * 0.5) for y in range(81)])
_rings = db_map.coverage_rings(_sq)
_outer = [rg for rg in _rings if _ring_area(rg) > 0]
_holes = [rg for rg in _rings if _ring_area(rg) < 0]
check('coverage: loop → one outer ring and one hole',
      len(_outer) == 1 and len(_holes) == 1, repr(len(_rings)))
if _holes:
    _hb = _bbox(_holes[0])
    _cla, _clo = _fix(20.0, 20.0)
    check('coverage: the hole boxes the un-driven middle',
          _hb[0] < _clo < _hb[2] and _hb[1] < _cla < _hb[3]
          and 40 - 2 * _R - 3 < (_hb[2] - _hb[0]) * _mlon < 40 - 2 * _R + 2, repr(_hb))
# covered cells touching only at corners stay separate simple rings (radius
# 0: each fix covers just its own cell). An X of five cells: the centre cell
# meets each arm at a pinch corner that is reached MID-ring whatever order
# the tracer starts in, so the left-most-turn branch is really exercised — a
# two-cell fixture started its trace AT the pinch and never entered it.
# (The grid hangs off the first fix, so 0.6 offsets keep fixes inside cells.)
_rings = db_map.coverage_rings([_fix(0.5, 0.5), _fix(1.6, 1.6), _fix(2.6, 0.6),
                                _fix(0.6, 2.6), _fix(2.6, 2.6)], radius_m=0)
check('coverage: corner-touching regions stay separate simple rings',
      len(_rings) == 5
      and all(len(rg) == 5 and len(set(map(tuple, rg))) == 4 for rg in _rings),
      repr(_rings))
# a region meeting its OWN hole at a corner: an 8x6 block minus a 2x2 hole
# whose corner touches a missing cell on the outside. Locally that pinch
# looks like two regions touching, so the turn rule alone traces ONE ring
# that revisits the pinch vertex (no hole ring at all, and a polyline the
# simplifier could turn into a bowtie); the split makes it outer + hole.
_cells = [(i, j) for i in range(8) for j in range(6)
          if (i, j) not in {(3, 3), (3, 4), (4, 3), (4, 4), (5, 5)}]
_rings = db_map.coverage_rings([_fix(0, 0)] + [_fix(i + .5, j + .5) for i, j in _cells],
                               radius_m=0, simplify=0)
check('coverage: a hole pinched against the outside splits into outer + hole',
      len(_rings) == 2 and sorted(_ring_area(rg) > 0 for rg in _rings) == [False, True]
      and all(len(rg) - 1 == len(set(map(tuple, rg[:-1]))) for rg in _rings),
      repr([(len(rg), _ring_area(rg) > 0) for rg in _rings]))
# Douglas-Peucker: a diagonal track's staircase becomes a few segments.
# Fixes are kept 0.1 m off the cell corners: an exact integer-metre fix would
# floor() to either side of the corner independently in lon and lat, and the
# stray cells that scatter made the old bound (< 45 vertices) measure float
# aliasing rather than the simplifier.
_diag = [_fix(0, 0)] + [_fix(x * 0.5 + 0.1, x * 0.5 + 0.1) for x in range(1, 101)]
_rings = db_map.coverage_rings(_diag)
check('coverage: a diagonal outline is simplified, not a 1 m staircase',
      len(_rings) == 1 and len(_rings[0]) <= 12,
      repr(len(_rings[0]) if _rings else None))

# the route: every OTHER study with points, cached by file signature and
# persisted in <studies_dir>/.coverage.json
_cov_dir = tempfile.mkdtemp(prefix='dbmap_cov_')
_cov_saved_over = dict(db_map._cli_overrides)
db_map._cli_overrides['studies_dir'] = _cov_dir


def _cov_study(name, fixes):
    path = os.path.join(_cov_dir, f'{name}.db')
    conn = sd.db_open(path)
    if fixes:
        sd.insert_rows(conn, [{'lat': la, 'lon': lo, 'heading': 90.0, 'fix': 4,
                               'adc': [1000] * 8, 'gps_ts': '1.0'}
                              for la, lo in fixes], 12.4, 25.0)
    sd.meta_set(conn, 'study_name', 'named ' + name)
    conn.close()
    return path


def _cov_ids(study_id):
    fc = client.get(f'/coverage/{study_id}').get_json()
    return sorted(f['properties']['id'] for f in fc['features'])


_cov_a_id = f'cov_a_1700000000__{db_map.HOST_TAG}'
_cov_b_id = f'cov_b_1700000001__{db_map.HOST_TAG}'
_cov_c_id = 'cov_c_1700000002__other'
_cov_a = _cov_study(_cov_a_id, _line)
_cov_b = _cov_study(_cov_b_id, [])
_cov_c = _cov_study(_cov_c_id, [_fix(200.0 + x, 100.0) for x in range(10)])
_cov_file = os.path.join(_cov_dir, '.coverage.json')
db_map._coverage_cache.clear()
db_map._coverage_cache_loaded = False
db_map._study_cache.clear()
db_map._study_cache_loaded = False
r = client.get(f'/coverage/{_cov_a_id}')
_fc = r.get_json()
_g = _fc['features'][0]['geometry'] if _fc['features'] else {}
check('coverage route: the other studies with points — not the viewed or the empty one',
      r.status_code == 200 and _fc['type'] == 'FeatureCollection'
      and [f['properties']['id'] for f in _fc['features']] == [_cov_c_id]
      and _g.get('type') == 'MultiPolygon'
      and _g['coordinates'][0][0][0] == _g['coordinates'][0][0][-1],
      repr(_fc)[:300])
check('coverage route: from the other study, this one shows',
      _cov_ids(_cov_c_id) == [_cov_a_id])
check('coverage route: name comes from the study index when it is warm',
      _fc['features'][0]['properties']['name'] == _cov_c_id
      and db_map.list_studies() is not None
      and client.get(f'/coverage/{_cov_a_id}').get_json()['features'][0]['properties']['name']
      == 'named ' + _cov_c_id)
_cj = json.load(open(_cov_file))
check('coverage: persisted as <studies_dir>/.coverage.json keyed by db path',
      set(_cj) == {_cov_a, _cov_b, _cov_c} and _cj[_cov_b][1] == []
      and len(_cj[_cov_c][0]) == 2 and _cj[_cov_c][1] == _g['coordinates'][0],
      repr(sorted(_cj))[:200])
# cold process: served from the file with no db opened
db_map._coverage_cache.clear()
db_map._coverage_cache_loaded = False
db_map.db_ro = _no_db
try:
    _cold = _cov_ids(_cov_a_id) + _cov_ids(_cov_c_id)
    _cov_err = None
except _DbOpened as e:
    _cold, _cov_err = None, e
finally:
    db_map.db_ro = _real_db_ro
check('coverage: cold process serves every outline with no db opened',
      _cov_err is None and _cold == [_cov_c_id, _cov_a_id], repr(_cov_err or _cold))
# the live study is left out (PID file names it), and comes back after
db_map.PID_FILE = os.path.join(SCRATCH, 'daemon.pid')
with open(db_map.PID_FILE, 'w') as f:
    f.write(f'{os.getpid()}:{_cov_c}')
_live_ids = _cov_ids(_cov_a_id)
os.unlink(db_map.PID_FILE)
check('coverage route: the study the daemon is writing is left out',
      _live_ids == [] and _cov_ids(_cov_a_id) == [_cov_c_id], repr(_live_ids))
# a changed db (a second, distant track) is recomputed: two outer rings now
time.sleep(0.02)
conn = sd.db_open(_cov_c)
sd.insert_rows(conn, [{'lat': la, 'lon': lo, 'heading': 90.0, 'fix': 4,
                       'adc': [1000] * 8, 'gps_ts': '2.0'}
                      for la, lo in (_fix(300.0 + x, 100.0) for x in range(10))],
               12.4, 25.0)
conn.close()
_fc3 = client.get(f'/coverage/{_cov_a_id}').get_json()
check('coverage: a changed db is recomputed and the file rewritten',
      len(_fc3['features'][0]['geometry']['coordinates']) == 2
      and len(json.load(open(_cov_file))[_cov_c][1]) == 2,
      repr(len(_fc3['features'][0]['geometry']['coordinates'])))
# an unreadable db is skipped and NOT cached (the next request tries again)
db_map._coverage_cache.clear()
db_map._coverage_cache_loaded = False
os.unlink(_cov_file)


def _cov_failing_ro(path):
    if path.endswith(os.path.basename(_cov_c)):
        raise sqlite3.OperationalError('disk I/O error')
    return _real_db_ro(path)


db_map.db_ro = _cov_failing_ro
try:
    _fail_ids = _cov_ids(_cov_a_id)
finally:
    db_map.db_ro = _real_db_ro
check('coverage: an unreadable db is skipped, not cached',
      _fail_ids == [] and _cov_c not in json.load(open(_cov_file))
      and _cov_ids(_cov_a_id) == [_cov_c_id], repr(_fail_ids))
# a torn cache file is ignored and rebuilt
with open(_cov_file, 'w') as f:
    f.write('{"not": json')
db_map._coverage_cache.clear()
db_map._coverage_cache_loaded = False
check('coverage: a torn cache file is rebuilt',
      _cov_ids(_cov_a_id) == [_cov_c_id] and _cov_c in json.load(open(_cov_file)))
# ids the router lets through but safe_study_id must refuse ('..%2Fx' never
# reaches the route body — werkzeug's [^/]+ converter rejects the decoded
# '../x' first — so on its own it proved nothing about the guard)
check('coverage route: a path-like id is refused',
      client.get('/coverage/..').status_code == 404
      and client.get('/coverage/a%20b').status_code == 404
      and client.get('/coverage/..%2Fx').status_code == 404)
db_map._cli_overrides.clear()
db_map._cli_overrides.update(_cov_saved_over)
db_map._coverage_cache.clear()
db_map._coverage_cache_loaded = False
db_map._study_cache.clear()
db_map._study_cache_loaded = False
# the page draws it on its own pane, unfilled and non-interactive
check('view: coverage layer on its own pane, non-interactive, even-odd grey wash',
      "fetch('/coverage/' + STUDY_ID)" in _view_src
      and "mapObj.createPane('coverage')" in _view_src
      and "pane: 'coverage', interactive: false" in _view_src
      and "fillRule: 'evenodd'" in _view_src and 'fillOpacity: 0.1' in _view_src)
# the wait banners, in order: outlines first, then the points load; the
# status is cleared after the first paint; every step's failure is NOTED on
# the banner (sticky red line — a kiosk has no console) rather than
# swallowed, non-2xx responses count as failures, and the study load is
# reached whatever happened to the outlines
check('view: wait banners — outlines, then map; failures stay on screen',
      _view_src.index("setLoadBanner('Rendering surveyed-area outlines")
      < _view_src.index("setLoadBanner('Loading map")
      and "loadCoverage().then(loadStudy, loadStudy)" in _view_src
      and "requestAnimationFrame(() => setLoadBanner(null))" in _view_src
      and "noteLoadError('Surveyed-area outlines', e)" in _view_src
      and "noteLoadError('Points', e)" in _view_src
      and "noteLoadError('Map load', e)" in _view_src
      and _view_src.count('okOrThrow(r).json()') == 2
      and 'id="loadBanner" hidden' in _view_src
      and 'setTimeout(renderLoadBanner, 250)' in _view_src
      and "el.hidden = !bannerStatus && !loadErrors.length" in _view_src)

# ── copy_study.py ─────────────────────────────────────────────────────────────

print('copy_study.py:')
import copy_study
_cs_src = make_study('survey_1700000000__mm1', n_points=7,
                     meta={'study_name': 'survey', 'created_at': '1700000000.5'})
# another unit's sidecar rides along: its tag (so its ownership) is kept
_cs_sc = Path(STUDIES) / 'survey_1700000000__mm1.targets__mm2.json'
_cs_sc.write_text(json.dumps({'version': 1, 'study': 'survey_1700000000__mm1',
                              'host': 'mm2', 'seq': 1, 'cleared': {},
                              'targets': [{'id': 1, 'lat': 40.1, 'lon': -119.1,
                                           'created_at': 1.0}]}))
# an open writer keeps its last commit in -wal (no checkpoint until close):
# a plain file copy of the .db would not see this row, the backup must
_cs_hold = sqlite3.connect(_cs_src)
_cs_hold.execute("INSERT INTO meta VALUES ('wal_only', '1')")
_cs_hold.commit()
_cs_id, _cs_db, _cs_n, _cs_copied = copy_study.copy_study(
    _cs_src, 'Copy Of It', 'mm3', Path(STUDIES), now=1700009999.25)
check('copy_study: id follows <name>_<epoch>__<host>',
      _cs_id == 'Copy_Of_It_1700009999__mm3', _cs_id)
_cs_c = sqlite3.connect(f'file:{_cs_db}?mode=ro', uri=True)
_cs_m = dict(_cs_c.execute('SELECT key,value FROM meta').fetchall())
check('copy_study: every point comes across',
      _cs_n == 7 and _cs_c.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 7, _cs_n)
check('copy_study: WAL tail included (online backup, not a file copy)',
      _cs_m.get('wal_only') == '1')
check('copy_study: listed name is the new one; created_at kept; provenance recorded',
      _cs_m['study_name'] == 'Copy Of It' and _cs_m['created_at'] == '1700000000.5'
      and _cs_m['copied_from'] == 'survey_1700000000__mm1'
      and _cs_m['copied_at'].startswith('1700009999.25'), repr(_cs_m))
check('copy_study: copy is in WAL mode like the daemon writes',
      _cs_c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal')
_cs_c.close()
_cs_src_m = dict(_cs_hold.execute('SELECT key,value FROM meta').fetchall())
check('copy_study: source untouched',
      _cs_src_m['study_name'] == 'survey' and 'copied_from' not in _cs_src_m)
_cs_hold.close()
_cs_sc2 = Path(STUDIES) / f'{_cs_id}.targets__mm2.json'
_cs_d2 = json.loads(_cs_sc2.read_text()) if _cs_sc2.exists() else {}
check('copy_study: sidecar follows, tag kept, study field re-pointed',
      _cs_copied == [_cs_sc2] and _cs_d2.get('host') == 'mm2'
      and _cs_d2.get('study') == _cs_id and len(_cs_d2.get('targets') or []) == 1,
      repr(_cs_d2))
db_map._cli_overrides['studies_dir'] = STUDIES
_cs_list = {s['id']: s for s in db_map.list_studies()}
_cs_row = _cs_list.get(_cs_id, {})
check('copy_study: landing lists the copy under its new name, owner mm3, 1 target',
      _cs_row.get('name') == 'Copy Of It' and _cs_row.get('targets') == 1
      and _cs_row.get('foreign_owner') == ('mm3' if db_map.HOST_TAG != 'mm3' else None),
      repr(_cs_row))
for _cs_what, _cs_args in (('a bad host tag', dict(host='bad_tag')),
                           ('an existing destination id', dict(host='mm3'))):
    try:
        copy_study.copy_study(_cs_src, 'Copy Of It', dst_dir=Path(STUDIES),
                              now=1700009999.25, **_cs_args)
        _cs_refused = False
    except SystemExit:
        _cs_refused = True
    check(f'copy_study: refuses {_cs_what}', _cs_refused)

# ── gunicorn_config: worker memory guard ─────────────────────────────────────
# The config is plain Python gunicorn exec()s; load it the same way and drive
# the post_request hook with a stand-in worker. The real hook arms a Timer
# that hard-exits the process after the drain window — stubbed here, or the
# test runner would die 7 s later.

print()
print('gunicorn_config:')
import importlib.util as _ilu
import types as _types
_gs = _ilu.spec_from_file_location(
    'gunicorn_config', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'gunicorn_config.py'))
gcfg = _ilu.module_from_spec(_gs)
_gs.loader.exec_module(gcfg)
check('gunicorn: gthread, 1 worker, short drain',
      gcfg.workers == 1 and gcfg.worker_class == 'gthread'
      and gcfg.graceful_timeout == 10 and gcfg.timeout == 0)
check('gunicorn: rss threshold defaults to 512 MB', gcfg.WORKER_RSS_MAX_MB == 512)
_rss_real = gcfg.worker_rss_mb()
check('gunicorn: rss reader is a number on Linux, None elsewhere',
      (_rss_real is None) == (not os.path.exists('/proc/self/statm'))
      and (_rss_real is None or _rss_real > 0), repr(_rss_real))


class _FakeLog:
    def __init__(self):
        self.msgs = []

    def warning(self, fmt, *a):
        self.msgs.append(fmt % a)


class _FakeTimer:
    armed = []

    def __init__(self, interval, fn, args=()):
        self.interval, self.fn, self.args, self.daemon = interval, fn, args, False

    def start(self):
        _FakeTimer.armed.append(self)


gcfg.threading = _types.SimpleNamespace(Timer=_FakeTimer)
_env = {'REQUEST_METHOD': 'GET', 'PATH_INFO': '/points/big'}


def _worker():
    return _types.SimpleNamespace(alive=True, log=_FakeLog())


w = _worker(); gcfg.worker_rss_mb = lambda: 511.9
gcfg.post_request(w, None, _env, None)
check('guard: under threshold leaves worker alone', w.alive and not w.log.msgs)
w = _worker(); gcfg.worker_rss_mb = lambda: None
gcfg.post_request(w, None, _env, None)
check('guard: no /proc = no-op', w.alive and not w.log.msgs)
w = _worker(); gcfg.worker_rss_mb = lambda: 700.0
gcfg.post_request(w, None, _env, None)
check('guard: over threshold stops accepting + logs the request',
      not w.alive and len(w.log.msgs) == 1
      and 'rss 700 MB >= 512 MB after GET /points/big' in w.log.msgs[0],
      repr(w.log.msgs))
check('guard: hard-exit backstop armed past the drain window, daemonised',
      len(_FakeTimer.armed) == 1 and _FakeTimer.armed[0].daemon
      and _FakeTimer.armed[0].interval == gcfg.graceful_timeout + 2
      and _FakeTimer.armed[0].fn is os._exit and _FakeTimer.armed[0].args == (0,),
      repr([(x.interval, x.fn, x.args) for x in _FakeTimer.armed]))
gcfg.post_request(w, None, _env, None)          # requests still draining
check('guard: a dying worker is not recycled twice',
      len(w.log.msgs) == 1 and len(_FakeTimer.armed) == 1)
w = _worker(); gcfg.WORKER_RSS_MAX_MB = 0
gcfg.post_request(w, None, _env, None)
check('guard: MM_WORKER_RSS_MAX_MB=0 disables', w.alive and not w.log.msgs)
gcfg.WORKER_RSS_MAX_MB = 512

print()
if FAILED:
    print(f'{len(FAILED)} FAILURE(S): {FAILED}')
    sys.exit(1)
print('ALL PASS')
