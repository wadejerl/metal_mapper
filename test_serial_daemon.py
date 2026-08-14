#!/usr/bin/env python3
"""Offline tests for serial_daemon.py (detector3 protocol, tick-anchored).

Needs NO hardware: the integration section runs the daemon against a pty
pair and plays the firmware's role (answers the 'I' handshake, feeds CSV).
Run before deploying:  python3 test_serial_daemon.py

NOTE: uses the real /tmp/metal_detector_daemon.pid and command FIFO — do
not run while a real study is being recorded (it aborts if one is live).
"""
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import serial_daemon as sd

# Refuse to disturb a live daemon (field Pi safety).
try:
    with open(sd.PID_FILE) as f:
        _pid = int(f.read().split(':', 1)[0])
    os.kill(_pid, 0)
    print(f'ABORT: a real daemon (pid {_pid}) is running — not testing over it.')
    sys.exit(2)
except (FileNotFoundError, ValueError, ProcessLookupError):
    pass

FAILS = []

def check(name, cond, detail=''):
    if cond:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name}  {detail}')
        FAILS.append(name)


# ── parse_data_line ───────────────────────────────────────────────────────────
print('parse_data_line:')

# Sample line (exact firmware shape): 3 empty GPS + 8 adc + empty ts/heading + tick
L = ',,,100,200,300,400,500,600,700,800,,,4217'
p = sd.parse_data_line(L)
check('sample line', p is not None and p['kind'] == 'sample'
      and p['adc'] == [100,200,300,400,500,600,700,800] and p['tick'] == 4217
      and p['lat'] is None and p['heading'] is None and p['vin'] is None, repr(p))

# Sample line with the 1 Hz status pair
L = ',,,1,2,3,4,5,6,7,8,,,99,12.345,25.1'
p = sd.parse_data_line(L)
check('sample + vin/temp', p is not None and p['kind'] == 'sample'
      and p['vin'] == 12.345 and p['temp'] == 25.1 and p['tick'] == 99, repr(p))

# Anchor line: lat,lon,fix + 8 empty adc + ts,heading,tick
L = '40.123456789,-119.123456789,4,,,,,,,,,123519.500000,180.25,4218'
p = sd.parse_data_line(L)
check('anchor line', p is not None and p['kind'] == 'anchor'
      and p['lat'] == 40.123456789 and p['fix'] == 4 and p['heading'] == 180.25
      and p['gps_ts'] == '123519.500000' and p['tick'] == 4218
      and p['adc'] is None, repr(p))

# Anchor, heading missing (dual-antenna solution not good)
L = '40.1,-119.1,5,,,,,,,,,123519.500000,,4219'
p = sd.parse_data_line(L)
check('anchor no heading', p is not None and p['kind'] == 'anchor'
      and p['heading'] is None and p['fix'] == 5, repr(p))

# Anchor with status pair
L = '40.1,-119.1,4,,,,,,,,,123519.500000,359.99,42,12.345,25.1'
p = sd.parse_data_line(L)
check('anchor + vin/temp', p is not None and p['heading'] == 359.99
      and p['vin'] == 12.345 and p['tick'] == 42, repr(p))

# Heading edge values (anchors)
check('heading 0.00 ok',   sd.parse_data_line('40.1,-119.1,4,,,,,,,,,1.0,0.00,7') is not None)
check('heading 360.00 ok', sd.parse_data_line('40.1,-119.1,4,,,,,,,,,1.0,360.00,7') is not None)
check('heading 360.01 rejected', sd.parse_data_line('40.1,-119.1,4,,,,,,,,,1.0,360.01,7') is None)
check('heading -1 rejected',     sd.parse_data_line('40.1,-119.1,4,,,,,,,,,1.0,-1.0,7') is None)

# Corruption cases
check('short line rejected',      sd.parse_data_line('40.1,-119.1,4') is None)
check('old 13-field rejected',    sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('old 15-field rejected',    sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0,12.3,25.0') is None)
check('17 fields rejected',       sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0,7,12.3,25.0,9') is None)
check('gps+adc both rejected',    sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0,7') is None)
check('all-empty rejected',       sd.parse_data_line(',,,,,,,,,,,,,7') is None)
check('partial GPS rejected',     sd.parse_data_line('40.1,,4,,,,,,,,,1.0,180.0,7') is None)
check('partial adc rejected',     sd.parse_data_line(',,,1,2,3,,5,6,7,8,,,7') is None)
check('sample-with-heading rejected', sd.parse_data_line(',,,1,2,3,4,5,6,7,8,,180.0,7') is None)
check('tick missing rejected',    sd.parse_data_line(',,,1,2,3,4,5,6,7,8,,,') is None)
check('tick garbage rejected',    sd.parse_data_line(',,,1,2,3,4,5,6,7,8,,,7x') is None)
check('tick negative rejected',   sd.parse_data_line(',,,1,2,3,4,5,6,7,8,,,-7') is None)
check('garbage lat rejected',     sd.parse_data_line('4o.1,-119.1,4,,,,,,,,,1.0,180.0,7') is None)
check('lat 91 rejected',          sd.parse_data_line('91.0,-119.1,4,,,,,,,,,1.0,180.0,7') is None)
check('lon -181 rejected',        sd.parse_data_line('40.1,-181.0,4,,,,,,,,,1.0,180.0,7') is None)
check('fix 12 rejected',          sd.parse_data_line('40.1,-119.1,12,,,,,,,,,1.0,180.0,7') is None)
check('adc garbage rejected',     sd.parse_data_line(',,,1,2,x,4,5,6,7,8,,,7') is None)
check('adc negative rejected',    sd.parse_data_line(',,,1,-2,3,4,5,6,7,8,,,7') is None)
check('adc float rejected',       sd.parse_data_line(',,,1,2.5,3,4,5,6,7,8,,,7') is None)
check('nan lat rejected',         sd.parse_data_line('nan,-119.1,4,,,,,,,,,1.0,180.0,7') is None)
check('empty vin rejected',       sd.parse_data_line(',,,1,2,3,4,5,6,7,8,,,7,,25.0') is None)

# ── parse_info_line ───────────────────────────────────────────────────────────
print('parse_info_line:')
L = ('# info fw=a5c5006 gps_ver=LC29HDANR11A03S,2023/10/26,14:23:01 gps_id=? '
     'blanking_us=16 rx_window_us=3 tx_pulse_us=120 coil_spacing_mm=500 '
     'coil_offset_fore_mm=0 coil_offset_right_mm=-10 '
     'sample_rate_hz=500 adc_oversample=16 '
     'adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')
i = sd.parse_info_line(L)
check('info parse', i is not None and i['fw'] == 'a5c5006'
      and i['gps_ver'] == 'LC29HDANR11A03S,2023/10/26,14:23:01'
      and i['gps_id'] == '?' and i['blanking_us'] == '16'
      and i['tx_pulse_us'] == '120'
      and i['coil_offset_right_mm'] == '-10'
      and i['sample_rate_hz'] == '500'
      and i['adc'] == 'PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14', repr(i))
check('non-info # line', sd.parse_info_line('# blanking=16us  rx_window=3us') is None)
check('boot banner', sd.parse_info_line('# stm_detector3 boot') is None)

# ── timing echo regex ─────────────────────────────────────────────────────────
print('RE_TIMING:')
m = sd.RE_TIMING.search('# blanking=16us  rx_window=3us')
check('timing echo', m is not None and m.group(1) == '16' and m.group(2) == '3')
m = sd.RE_TIMING.search('# metal detector pulsing (hw-aligned): tx_pulse=120us blanking=16us rx_window=3us')
check('boot pulse banner also matches', m is not None and m.group(1) == '16')

# ── tx pulse echo regex ───────────────────────────────────────────────────────
print('RE_TXPULSE:')
m = sd.RE_TXPULSE.search('# tx_pulse=90us')
check('tx pulse echo', m is not None and m.group(1) == '90')
m = sd.RE_TXPULSE.search('# metal detector pulsing (hw-aligned): tx_pulse=120us blanking=16us rx_window=3us')
check('boot pulse banner tx match', m is not None and m.group(1) == '120')
m = sd.RE_TXPULSE.search('# saved (slot 3/128): blanking=16us  rx_window=3us  tx_pulse=110us  coil_spacing=500mm')
check('saved banner tx match', m is not None and m.group(1) == '110')
check('timing echo has no tx', sd.RE_TXPULSE.search('# blanking=16us  rx_window=3us') is None)

# ── sample rate echo regex ────────────────────────────────────────────────────
print('RE_RATE:')
m = sd.RE_RATE.search('# sample_rate=500Hz (25 per fix)')
check('rate echo', m is not None and m.group(1) == '500')
m = sd.RE_RATE.search('# saved (slot 3/128): blanking=16us  rx_window=3us  '
                      'tx_pulse=110us  coil_spacing=500mm  '
                      'sample_rate=200Hz')
check('saved banner rate match', m is not None and m.group(1) == '200')
check('no bare-number match', sd.RE_RATE.search('# sample_rate=500') is None)

# ── gate_reason ───────────────────────────────────────────────────────────────
print('gate_reason:')
def mk(lat=40.1, lon=-119.1, fix=4, heading=180.0):
    return {'kind': 'anchor', 'lat': lat, 'lon': lon, 'fix': fix,
            'heading': heading, 'gps_ts': '1.0', 'adc': None, 'tick': 1,
            'vin': None, 'temp': None}
check('rtk+heading passes', sd.gate_reason(mk(), False) == '')
check('fix=5 blocked',      sd.gate_reason(mk(fix=5), False) == 'fix=5')
check('fix=1 blocked',      sd.gate_reason(mk(fix=1), False) == 'fix=1')
check('no heading blocked', sd.gate_reason(mk(heading=None), False) == 'no heading')
check('no anchor blocked',  sd.gate_reason(None, False) == 'no fix')
check('no-gate: fix=1 ok',       sd.gate_reason(mk(fix=1, heading=None), True) == '')
check('no-gate: no anchor still out', sd.gate_reason(None, True) == 'no fix')

# ── slerp_heading ─────────────────────────────────────────────────────────────
print('slerp_heading:')
def hdg_close(a, b, tol=1e-9):
    return abs(((a - b + 180.0) % 360.0) - 180.0) < tol
check('midpoint plain', hdg_close(sd.slerp_heading(80.0, 100.0, 0.5), 90.0))
check('across north',   hdg_close(sd.slerp_heading(350.0, 10.0, 0.5), 0.0))
check('across north reversed', hdg_close(sd.slerp_heading(10.0, 350.0, 0.5), 0.0))
check('endpoints exact', hdg_close(sd.slerp_heading(350.0, 10.0, 0.0), 350.0)
      and hdg_close(sd.slerp_heading(350.0, 10.0, 1.0), 10.0))
check('result in [0,360)', 0.0 <= sd.slerp_heading(350.0, 10.0, 0.75) < 360.0)

# ── TrackRecorder ─────────────────────────────────────────────────────────────
print('TrackRecorder:')

R_MM  = 6_371_000.0 * 1000.0     # same earth radius as latlon_dist_mm
LAT0, LON0 = 40.1, -119.1

def north(mm):
    """Latitude mm metres... millimetres north of LAT0."""
    return LAT0 + math.degrees(mm / R_MM)

def anchor(tick, ts, mm_n=0.0, fix=4, hdg=90.0):
    return {'kind': 'anchor', 'lat': north(mm_n), 'lon': LON0, 'fix': fix,
            'gps_ts': f'{ts:.6f}', 'adc': None, 'heading': hdg,
            'tick': tick, 'vin': None, 'temp': None}

def sample(tick, val):
    return {'kind': 'sample', 'lat': None, 'lon': None, 'fix': None,
            'gps_ts': None, 'adc': [val] * 8, 'heading': None,
            'tick': tick, 'vin': None, 'temp': None}

def mm_of(row):
    return sd.latlon_dist_mm(LAT0, LON0, row['lat'], row['lon'])

# Segment 1: A at 0 mm (tick 1000), B at 44 mm (tick 1004), 4 samples at
# 11/22/33/44 mm (off the exact flush threshold — an exact-20.0 distance
# would flip on float rounding). min_dist=20 → row1 = first sample alone
# (first point always saves), row2 = mean of the 22+33 mm samples, the
# 44 mm sample stays binned for the next segment.
rec = sd.TrackRecorder(20.0, False)
check('first anchor: no rows', rec.on_anchor(anchor(1000, 100.00)) == [])
for k, v in ((1001, 100), (1002, 200), (1003, 300), (1004, 400)):
    rec.on_sample(sample(k, v))
rows = rec.on_anchor(anchor(1004, 100.05, mm_n=44.0))
check('segment 1: two rows', len(rows) == 2, repr(rows))
if len(rows) == 2:
    check('row1 = lone first sample', rows[0]['adc'] == [100] * 8
          and abs(mm_of(rows[0]) - 11.0) < 0.5, repr(rows[0]))
    check('row2 = mean of 22/33 mm bin', rows[1]['adc'] == [250] * 8
          and abs(mm_of(rows[1]) - 27.5) < 0.5, repr(rows[1]))
    check('row1 gps_ts interpolated', rows[0]['gps_ts'] == '100.012500',
          repr(rows[0]['gps_ts']))
    check('rows carry heading + fix', rows[0]['heading'] == 90.0
          and rows[0]['fix'] == 4, repr(rows[0]))
check('44 mm sample still binned', rec.bin_n == 1)

# Segment 2 continues: C at 88 mm (tick 1008), samples at 55/66/77/88 mm.
# The 44 mm sample carried in the bin averages with the 55 mm one.
for k, v in ((1005, 500), (1006, 600), (1007, 700), (1008, 800)):
    rec.on_sample(sample(k, v))
rows = rec.on_anchor(anchor(1008, 100.10, mm_n=88.0))
check('segment 2: two rows', len(rows) == 2, repr(rows))
if len(rows) == 2:
    check('bin carried across segments', rows[0]['adc'] == [450] * 8
          and abs(mm_of(rows[0]) - 49.5) < 0.5, repr(rows[0]))
    check('row4 = mean of 66/77 mm bin', rows[1]['adc'] == [650] * 8
          and abs(mm_of(rows[1]) - 71.5) < 0.5, repr(rows[1]))
check('motion_mm ~16.5 (88 anchor vs 71.5 saved)',
      abs(rec.motion_mm() - 16.5) < 0.5, repr(rec.motion_mm()))

# A continuity break (here: a float anchor) closes the open bin at its own
# centroid rather than letting it average with post-gap samples later.
rows = rec.on_anchor(anchor(2000, 100.15, mm_n=999.0, fix=5))
check('continuity break flushes open bin', len(rows) == 1
      and rows[0]['adc'] == [800] * 8 and abs(mm_of(rows[0]) - 88.0) < 0.5,
      repr(rows))
check('bin empty after break', rec.bin_n == 0)

# Heading slerp through north within a segment
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 200.00, hdg=350.0))
rec.on_sample(sample(11, 100))
rows = rec.on_anchor(anchor(12, 200.05, mm_n=40.0, hdg=10.0))
check('heading slerps across north', len(rows) == 1
      and hdg_close(rows[0]['heading'], 0.0, tol=1e-6), repr(rows))

# Stationary: first sample saves once, then the bin grows and no rows come
# (the DB must not grow while sitting still).
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(100, 300.00))
rec.on_sample(sample(101, 50))
rec.on_sample(sample(102, 60))
rows = rec.on_anchor(anchor(102, 300.05))
check('stationary: first point saves', len(rows) == 1 and rows[0]['adc'] == [50] * 8,
      repr(rows))
still = 0
for i in range(10):
    rec.on_sample(sample(103 + 2 * i, 70))
    rec.on_sample(sample(104 + 2 * i, 80))
    still += len(rec.on_anchor(anchor(104 + 2 * i, 300.10 + 0.05 * i)))
check('stationary: no further rows', still == 0)
check('stationary: bin keeps accumulating', rec.bin_n == 21, rec.bin_n)

# Gate: a segment with one non-RTK anchor is discarded whole
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 400.00))
rec.on_sample(sample(11, 100))
rows = rec.on_anchor(anchor(12, 400.05, mm_n=40.0, fix=5))
check('float anchor kills segment', rows == [] and rec.pending == [])
rec.on_sample(sample(13, 100))
rows = rec.on_anchor(anchor(14, 400.10, mm_n=80.0))
check('gate recovers next segment (prev anchor was float)', rows == [])
rec.on_sample(sample(15, 100))
rows = rec.on_anchor(anchor(16, 400.15, mm_n=120.0))
check('rows resume once both anchors pass', len(rows) == 1, repr(rows))

# No heading blocks in gated mode, records in --no-gate
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 500.00, hdg=None))
rec.on_sample(sample(11, 100))
check('gated: headingless anchors discard',
      rec.on_anchor(anchor(12, 500.05, mm_n=40.0, hdg=None)) == [])
rec = sd.TrackRecorder(20.0, True)
rec.on_anchor(anchor(10, 500.00, fix=1, hdg=None))
rec.on_sample(sample(11, 100))
rows = rec.on_anchor(anchor(12, 500.05, mm_n=40.0, fix=1, hdg=None))
check('no-gate: records fix=1, heading None', len(rows) == 1
      and rows[0]['heading'] is None and rows[0]['fix'] == 1, repr(rows))

# GPS-time continuity: a dropout (dt > MAX_SEGMENT_S) discards the segment
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 600.00))
rec.on_sample(sample(11, 100))
check('dropout discards segment',
      rec.on_anchor(anchor(12, 601.00, mm_n=40.0)) == [])
# Midnight wrap (negative dt) likewise
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 86399.95))
rec.on_sample(sample(11, 100))
check('negative dt discards segment',
      rec.on_anchor(anchor(12, 0.00, mm_n=40.0)) == [])

# Corrupt tick span with a plausible dt
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 700.00))
rec.on_sample(sample(11, 100))
check('absurd tick span discards segment',
      rec.on_anchor(anchor(10_000, 700.05, mm_n=40.0)) == [])

# Tick regression = MCU reboot: buffered state dropped, recording resumes
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(1000, 800.00))
rec.on_sample(sample(1001, 100))
rows = rec.on_anchor(anchor(50, 800.05))
check('reboot: segment dropped, pending cleared', rows == [] and rec.pending == [])
rec.on_sample(sample(51, 100))
rows = rec.on_anchor(anchor(52, 800.10, mm_n=40.0))
check('reboot: recording resumes', len(rows) == 1, repr(rows))

# A sample can outrun its anchor: held for the next segment, not misplaced
rec = sd.TrackRecorder(20.0, False)
rec.on_anchor(anchor(10, 900.00))
rec.on_sample(sample(11, 100))
rec.on_sample(sample(12, 200))
rec.on_sample(sample(13, 300))          # belongs to the NEXT segment
rows = rec.on_anchor(anchor(12, 900.05, mm_n=40.0))
check('outrunning sample held back', rec.pending == [(13, [300] * 8)],
      repr(rec.pending))

# Pending buffer stays bounded through a long fix dropout
rec = sd.TrackRecorder(20.0, False)
for i in range(sd.PENDING_CAP + 500):
    rec.on_sample(sample(i, 1))
check('pending capped', len(rec.pending) == sd.PENDING_CAP, len(rec.pending))

# ── stamp_info / note_drift (in-memory db) ────────────────────────────────────
print('stamp_info / note_drift:')
mc = sd.db_open(':memory:')
# Truncated first info line: header must NOT lock incomplete
partial = sd.parse_info_line('# info fw=abc1234 gps_ver=LC29H blanking_us=16 rx_wi')
sd.stamp_info(mc, partial)
check('partial stamp: fw set', sd.meta_get(mc, 'fw_git_hash') == 'abc1234')
check('partial stamp: info_ok=0', sd.meta_get(mc, 'info_ok') == '0')
check('partial stamp: geometry missing', sd.meta_get(mc, 'coil_spacing_mm') is None)
full = sd.parse_info_line(
    '# info fw=abc1234 gps_ver=LC29H gps_id=UID9 blanking_us=16 rx_window_us=3 '
    'tx_pulse_us=120 coil_spacing_mm=500 coil_offset_fore_mm=0 '
    'coil_offset_right_mm=0 sample_rate_hz=500 adc_oversample=16 '
    'adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')
sd.stamp_info(mc, full)
check('later info fills gaps', sd.meta_get(mc, 'coil_spacing_mm') == '500'
      and sd.meta_get(mc, 'gps_id') == 'UID9'
      and sd.meta_get(mc, 'tx_pulse_us') == '120'
      and sd.meta_get(mc, 'sample_rate_hz') == '500')
check('info_ok now 1', sd.meta_get(mc, 'info_ok') == '1')
check('no false drift flags', sd.meta_get(mc, 'timing_changed') is None
      and sd.meta_get(mc, 'geometry_changed') is None)
# Geometry drift: flagged, never applied to the header
sd.note_drift(mc, 'geometry', {'coil_spacing_mm': '520'})
check('geometry drift flagged', 'coil_spacing_mm 500->520' in (sd.meta_get(mc, 'geometry_changed') or ''))
check('header spacing untouched', sd.meta_get(mc, 'coil_spacing_mm') == '500')
check('current spacing tracked', sd.meta_get(mc, 'coil_spacing_mm_current') == '520')
# TX pulse drift (f/v retune): same contract as the other timing keys
sd.note_drift(mc, 'timing', {'tx_pulse_us': '80'})
check('tx pulse drift flagged', 'tx_pulse_us 120->80' in (sd.meta_get(mc, 'timing_changed') or ''))
check('header tx pulse untouched', sd.meta_get(mc, 'tx_pulse_us') == '120')
check('current tx pulse tracked', sd.meta_get(mc, 'tx_pulse_us_current') == '80')
# Sample rate drift (g/b retune): flagged with the timing keys
sd.note_drift(mc, 'timing', {'sample_rate_hz': '200'})
check('rate drift flagged', 'sample_rate_hz 500->200' in (sd.meta_get(mc, 'timing_changed') or ''))
check('header rate untouched', sd.meta_get(mc, 'sample_rate_hz') == '500')
check('current rate tracked', sd.meta_get(mc, 'sample_rate_hz_current') == '200')
check('synchronous=NORMAL', mc.execute('PRAGMA synchronous').fetchone()[0] == 1)
mc.close()

# ── samples ring (in-memory db) ───────────────────────────────────────────────
print('samples ring:')
mc = sd.db_open(':memory:')

def ring_rows():
    return mc.execute('SELECT id,tick,adc0 FROM live_samples ORDER BY id').fetchall()

sd.samples_flush(mc, [(t, [t * 10] * 8) for t in range(1, 11)])
rows = ring_rows()
check('flush inserts in order', [r[1] for r in rows] == list(range(1, 11)),
      repr(rows))
check('adc columns land', rows[0][2] == 10 and rows[9][2] == 100, repr(rows[:2]))

# Overfill: two flushes totalling > SAMPLES_KEEP must trim to exactly
# SAMPLES_KEEP, dropping the OLDEST rows.
sd.samples_flush(mc, [(t, [1] * 8) for t in range(11, 11 + sd.SAMPLES_KEEP)])
rows = ring_rows()
check('ring trimmed to SAMPLES_KEEP', len(rows) == sd.SAMPLES_KEEP, len(rows))
check('oldest dropped, newest kept', rows[0][1] == 11
      and rows[-1][1] == 10 + sd.SAMPLES_KEEP, repr((rows[0], rows[-1])))
max_id_before = rows[-1][0]

# ids must stay monotonic across a clear (SSE cursors survive a daemon
# restart only because AUTOINCREMENT never reuses ids).
sd.samples_clear(mc)
check('clear empties ring', ring_rows() == [])
sd.samples_flush(mc, [(9000, [5] * 8)])
rows = ring_rows()
check('ids monotonic across clear', rows[0][0] > max_id_before,
      f'{max_id_before} -> {rows[0][0]}')
mc.close()

# ── Command FIFO forwarding ───────────────────────────────────────────────────
# The daemon is a transparent pipe from the FIFO to the serial port: single
# tuning keys AND the word-gated flash commands (SAVE\n / CLEAR\n) must reach
# the wire VERBATIM — the word matching lives in the firmware, not here.
print('command FIFO:')
import threading

class _FakeSer:
    is_open = True
    def __init__(self):
        self.buf = b''
    def write(self, data):
        self.buf += data

sd.FIFO_PATH = os.path.join(tempfile.mkdtemp(), 'cmd_fifo')
sd.ensure_fifo()
_fake_ser = _FakeSer()
sd.start_fifo_thread([_fake_ser], threading.Lock())
_wfd = os.open(sd.FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
os.write(_wfd, b'f')
os.write(_wfd, b'SAVE\n')
os.write(_wfd, b'CLEAR\n')
os.close(_wfd)
_deadline = time.time() + 3
while time.time() < _deadline and len(_fake_ser.buf) < 12:
    time.sleep(0.05)
check('FIFO forwards keys and SAVE/CLEAR words verbatim',
      _fake_ser.buf == b'fSAVE\nCLEAR\n', repr(_fake_ser.buf))

# ── Integration: daemon against a pty pair ────────────────────────────────────
print('integration (pty):')
import pty

SCRATCH = tempfile.mkdtemp(prefix='sd_test_')
DB = os.path.join(SCRATCH, 'itest.db')
for ext in ('', '-wal', '-shm'):
    try: os.unlink(DB + ext)
    except FileNotFoundError: pass

master, slave = pty.openpty()
os.set_blocking(master, False)
slave_name = os.ttyname(slave)

proc = subprocess.Popen(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', slave_name, '--db', DB, '--study-name', 'itest',
     '--min-dist', '20', '--baud', '115200', '--debug'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

INFO = (b'# info fw=a5c5006 gps_ver=LC29H,2023/10/26 gps_id=UID123 '
        b'blanking_us=16 rx_window_us=3 tx_pulse_us=120 coil_spacing_mm=500 '
        b'coil_offset_fore_mm=0 coil_offset_right_mm=0 '
        b'sample_rate_hz=500 adc_oversample=16 '
        b'adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14\r\n')

def read_master():
    try:
        return os.read(master, 256)
    except BlockingIOError:
        return b''

# Wait for the daemon's 'I' handshake, answer it.
deadline = time.time() + 8
got_I = False
while time.time() < deadline:
    d = read_master()
    if b'I' in d:
        got_I = True
        os.write(master, INFO)
        break
    time.sleep(0.05)
check('daemon sent I handshake', got_I)

time.sleep(0.4)

def send(line):
    os.write(master, line.encode() + b'\n')
    time.sleep(0.03)

def anchor_line(tick, ts, mm_n=0.0, fix=4, hdg='90.00', extra=''):
    return (f'{north(mm_n):.9f},{LON0:.9f},{fix},,,,,,,,,'
            f'{ts:.6f},{hdg},{tick}{extra}')

# 1) no fix: sample lines only (idle cadence), one carrying the status pair
send(',,,10,20,30,40,50,60,70,80,,,1,12.345,25.1')
for t in range(2, 8):
    send(f',,,10,20,30,40,50,60,70,80,,,{t}')
    time.sleep(0.1)
# 2) degraded: RTK float anchors — no rows may come of these
send(anchor_line(900, 123518.900000, fix=5))
time.sleep(0.15)
send(anchor_line(902, 123518.950000, fix=5))
time.sleep(0.3)
# 3) RTK fixed but no heading — still gated out
send(anchor_line(950, 123518.980000, hdg=''))
time.sleep(0.3)
# 4) recordable segment: anchor A at 0 mm, 4 samples, anchor B at 44 mm
#    (same shape as the unit test — samples land at 11/22/33/44 mm, clear
#    of the 20 mm flush threshold: rows at 11 mm [100s] and 27.5 mm [250s])
send(anchor_line(1000, 123519.000000, mm_n=0.0))
for tick, val in ((1001, 100), (1002, 200), (1003, 300), (1004, 400)):
    send(f',,,{val},{val},{val},{val},{val},{val},{val},{val},,,{tick}')
send(anchor_line(1004, 123519.050000, mm_n=44.0))
time.sleep(0.4)
# 5) timing echo mid-study
send('# blanking=18us  rx_window=3us')
# 5b) coil-spacing echo mid-study (d key pressed) → geometry drift flag
send('# coil_spacing=510mm')
# 5c) tx-pulse echo mid-study (v key pressed) → timing drift flag
send('# tx_pulse=100us')
# 5d) sample-rate echo mid-study ('b' key pressed) → timing drift flag
send('# sample_rate=200Hz (10 per fix)')
# 5f) noise-corrupted sample — the high-bit byte must poison the field
#     (errors='replace'), not splice it into a valid-looking value
os.write(master, b',,,1,2,3,4,5,\xb26,7,8,,,1005\n')
time.sleep(0.6)

conn = sqlite3.connect(DB)
pts  = conn.execute('SELECT lat,lon,heading,fix,adc0,adc7,gps_ts,vin,temp '
                    'FROM points ORDER BY id').fetchall()
meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
live = conn.execute('SELECT recording,reason,adc0,vin FROM live').fetchone()
live_count = conn.execute('SELECT COUNT(*) FROM live').fetchone()[0]

check('exactly 2 rows recorded', len(pts) == 2, f'got {len(pts)}: {pts}')
if len(pts) == 2:
    check('row1 = lone 11 mm sample', pts[0][4] == 100 and pts[0][5] == 100
          and abs(sd.latlon_dist_mm(LAT0, LON0, pts[0][0], pts[0][1]) - 11.0) < 0.5
          and pts[0][2] == 90.0 and pts[0][3] == 4, repr(pts[0]))
    check('row2 = 22/33 mm bin mean', pts[1][4] == 250
          and abs(sd.latlon_dist_mm(LAT0, LON0, pts[1][0], pts[1][1]) - 27.5) < 0.5,
          repr(pts[1]))
    check('rows carry the 1 Hz status pair', pts[0][7] == 12.345 and pts[0][8] == 25.1,
          repr(pts[0]))
    check('row gps_ts interpolated', pts[0][6] == '123519.012500', repr(pts[0][6]))
check('meta fw stamped', meta.get('fw_git_hash') == 'a5c5006', repr(meta.get('fw_git_hash')))
check('meta gps_id stamped', meta.get('gps_id') == 'UID123')
check('meta adc_order stamped', meta.get('adc_order') == 'PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')
check('meta sample_rate stamped', meta.get('sample_rate_hz') == '500')
check('meta schema_version=3', meta.get('schema_version') == '3')
check('meta gate=rtk+heading', meta.get('gate') == 'rtk+heading')
check('timing change flagged', 'timing_changed' in meta and 'blanking_us 16->18' in meta['timing_changed'],
      repr(meta.get('timing_changed')))
check('header blanking NOT overwritten', meta.get('blanking_us') == '16')
check('current blanking tracked', meta.get('blanking_us_current') == '18')
check('tx pulse change flagged', 'tx_pulse_us 120->100' in meta.get('timing_changed', ''),
      repr(meta.get('timing_changed')))
check('rate change flagged', 'sample_rate_hz 500->200' in meta.get('timing_changed', ''),
      repr(meta.get('timing_changed')))
check('geometry change flagged', 'coil_spacing_mm 500->510' in meta.get('geometry_changed', ''),
      repr(meta.get('geometry_changed')))
check('header spacing NOT overwritten', meta.get('coil_spacing_mm') == '500')
check('info_ok=1', meta.get('info_ok') == '1')
check('single live row', live_count == 1)
check('live row populated', live is not None and live[2] is not None, repr(live))
ring = conn.execute('SELECT COUNT(*), MIN(tick), MAX(tick) '
                    'FROM live_samples').fetchone()
check('live_samples ring populated', ring[0] > 0, repr(ring))
check('ring spans the sample stream', ring[0] <= sd.SAMPLES_KEEP
      and ring[2] >= 1004, repr(ring))
check('raw_mode not stamped in normal mode', 'raw_mode' not in meta)

# 6) stop feeding → 'no data' after 2s
time.sleep(3.5)
live = conn.execute('SELECT recording,reason FROM live').fetchone()
check('no-data detected', live == (0, 'no data'), repr(live))

# 7) garbage stream (wrong port/baud simulation): bytes flow, nothing parses
for _ in range(30):
    send('$GNGGA,123519.00,3745.357,N,12224.916,W,1,12,0.6,9.0,M,,M,,*47')
    time.sleep(0.1)
live = conn.execute('SELECT recording,reason FROM live').fetchone()
check('garbage stream detected', live == (0, 'garbage'), repr(live))

# 8) good data resumes → live recovers, next segment records
#    (tick continues from 1004; B→C dt 0.25 s is within MAX_SEGMENT_S)
send(f',,,500,500,500,500,500,500,500,500,,,1005')
send(f',,,600,600,600,600,600,600,600,600,,,1006')
send(anchor_line(1006, 123519.300000, mm_n=120.0))
for i in range(6):
    send(f',,,700,700,700,700,700,700,700,700,,,{1007 + i}')
    time.sleep(0.1)
live = conn.execute('SELECT recording,reason FROM live').fetchone()
check('recovers after garbage', live == (1, ''), repr(live))
pts = conn.execute('SELECT adc0 FROM points ORDER BY id').fetchall()
check('post-garbage segment recorded rows', len(pts) > 2, repr(pts))

# 9) second daemon while first is alive → refused, PID file untouched
pid1 = open('/tmp/metal_detector_daemon.pid').read().split(':', 1)[0]
p2 = subprocess.run(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', '/dev/tty.nope', '--db', os.path.join(SCRATCH, 'other.db')],
    capture_output=True, text=True, timeout=10)
check('second daemon refused', p2.returncode == 1 and 'refusing to start' in p2.stdout,
      f'rc={p2.returncode} out={p2.stdout[:200]}')
pid_after = open('/tmp/metal_detector_daemon.pid').read().split(':', 1)[0]
check('PID file not clobbered by loser', pid_after == pid1, f'{pid1} -> {pid_after}')

# 10) exclusive port lock: a second reader on the same tty fails loudly
import serial as pyserial
try:
    s2 = pyserial.Serial(slave_name, 115200, exclusive=True)
    s2.close()
    check('exclusive lock blocks second reader', False, 'second open succeeded')
except (pyserial.SerialException, OSError):
    check('exclusive lock blocks second reader', True)

created_at_1 = meta['created_at']
proc.terminate()
try:
    out, _ = proc.communicate(timeout=3)
except subprocess.TimeoutExpired:
    proc.kill()
    out, _ = proc.communicate()
check('daemon log has header stamp', 'study header stamped: fw=a5c5006' in out)
check('daemon log connect', 'connected to' in out)
check('geometry warning in log', 'geometry changed mid-study' in out)
conn.close()

# 11) restart on the SAME db must not rewrite provenance
p3 = subprocess.Popen(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', '/dev/tty.nope', '--db', DB, '--study-name', 'RENAMED'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(1.5)
p3.terminate()
p3.communicate(timeout=5)
conn = sqlite3.connect(DB)
meta2 = dict(conn.execute('SELECT key,value FROM meta').fetchall())
check('created_at preserved on restart', meta2.get('created_at') == created_at_1,
      f'{created_at_1} -> {meta2.get("created_at")}')
check('study_name preserved on restart', meta2.get('study_name') == 'itest',
      repr(meta2.get('study_name')))
conn.close()

# ── Integration: --raw mode (View Raw — display only, never save) ────────────
print('integration (--raw):')

RAWDB = os.path.join(SCRATCH, 'raw.db')
master_r, slave_r = pty.openpty()
os.set_blocking(master_r, False)

proc_r = subprocess.Popen(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', os.ttyname(slave_r), '--db', RAWDB, '--study-name', 'raw',
     '--baud', '115200', '--raw'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

deadline = time.time() + 8
got_I = False
while time.time() < deadline:
    try:
        d = os.read(master_r, 256)
    except BlockingIOError:
        d = b''
    if b'I' in d:
        got_I = True
        os.write(master_r, INFO)
        break
    time.sleep(0.05)
check('raw daemon sent I handshake', got_I)
time.sleep(0.4)

def send_r(line):
    os.write(master_r, line.encode() + b'\n')
    time.sleep(0.03)

# A stream that WOULD record (RTK fixed + heading + >20 mm travel per
# segment) — raw mode must feed the ring and still write zero points rows.
# Keep anchors flowing past several live-row ticks and read straight after:
# reason must be '' (gate satisfied, fix fresh) with recording still 0,
# proving the raw pin rather than a stale-fix side effect.
send_r(anchor_line(100, 200.000000, mm_n=0.0))
tick_r, ts_r, mm_r = 100, 200.00, 0.0
for i in range(16):
    tick_r += 1
    v = 100 + i
    send_r(f',,,{v},{v},{v},{v},{v},{v},{v},{v},,,{tick_r}')
    if i % 4 == 3:
        ts_r += 0.05
        mm_r += 44.0
        send_r(anchor_line(tick_r, ts_r, mm_n=mm_r))
    time.sleep(0.1)

conn_r = sqlite3.connect(RAWDB)
n_pts  = conn_r.execute('SELECT COUNT(*) FROM points').fetchone()[0]
ring   = conn_r.execute('SELECT COUNT(*), MAX(tick) FROM live_samples').fetchone()
live_r = conn_r.execute('SELECT recording,reason FROM live').fetchone()
meta_r = dict(conn_r.execute('SELECT key,value FROM meta').fetchall())
check('raw: zero points rows', n_pts == 0, n_pts)
check('raw: ring populated', ring[0] >= 8 and ring[1] >= 110, repr(ring))
check('raw: recording pinned 0, gate clear', live_r == (0, ''), repr(live_r))
check('raw: raw_mode stamped', meta_r.get('raw_mode') == '1')

proc_r.terminate()
try:
    proc_r.communicate(timeout=3)
except subprocess.TimeoutExpired:
    proc_r.kill()
    proc_r.communicate()
ring_after = conn_r.execute('SELECT COUNT(*) FROM live_samples').fetchone()[0]
check('raw: ring cleared on clean exit', ring_after == 0, ring_after)
conn_r.close()

# ── Integration: nonexistent port at startup ──────────────────────────────────
print('integration (bad port):')
DB2 = os.path.join(SCRATCH, 'itest2.db')
for ext in ('', '-wal', '-shm'):
    try: os.unlink(DB2 + ext)
    except FileNotFoundError: pass
proc2 = subprocess.Popen(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', '/dev/tty.does-not-exist', '--db', DB2],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2.5)
proc2.terminate()
try:
    out2, _ = proc2.communicate(timeout=3)
except subprocess.TimeoutExpired:
    proc2.kill()
    out2, _ = proc2.communicate()
check('loud port error in log', 'ERROR: cannot open' in out2, out2[:400])
check('lists available ports', 'available ports:' in out2)
conn2 = sqlite3.connect(DB2)
live2 = conn2.execute('SELECT recording,reason FROM live').fetchone()
check('live row says no port', live2 == (0, 'no port'), repr(live2))
conn2.close()

print()
if FAILS:
    print(f'{len(FAILS)} FAILURES: {FAILS}')
    sys.exit(1)
print('ALL PASS')
