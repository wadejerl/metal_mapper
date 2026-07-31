#!/usr/bin/env python3
"""Offline tests for serial_daemon.py (detector3 protocol).

Needs NO hardware: the integration section runs the daemon against a pty
pair and plays the firmware's role (answers the 'I' handshake, feeds CSV).
Run before deploying:  python3 test_serial_daemon.py

NOTE: uses the real /tmp/metal_detector_daemon.pid and command FIFO — do
not run while a real study is being recorded (it aborts if one is live).
"""
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

# Normal 13-field line with heading (exact firmware shapes: %.9f lat/lon, %.6f ts)
L = '40.123456789,-119.123456789,4,100,200,300,400,500,600,700,800,123519.500000,180.25'
p = sd.parse_data_line(L)
check('13f + heading', p is not None and p['lat'] == 40.123456789
      and p['fix'] == 4 and p['heading'] == 180.25 and p['adc'] == [100,200,300,400,500,600,700,800]
      and p['gps_ts'] == '123519.500000' and p['vin'] is None, repr(p))

# Normal 13-field, heading MISSING → trailing comma
L = '40.123456789,-119.123456789,5,1,2,3,4,5,6,7,8,123519.500000,'
p = sd.parse_data_line(L)
check('13f trailing comma (no heading)', p is not None and p['heading'] is None
      and p['fix'] == 5, repr(p))

# 15-field with heading + status pair
L = '40.1,-119.1,4,1,2,3,4,5,6,7,8,123519.500000,359.99,12.345,25.1'
p = sd.parse_data_line(L)
check('15f + heading + vin/temp', p is not None and p['heading'] == 359.99
      and p['vin'] == 12.345 and p['temp'] == 25.1, repr(p))

# 15-field, no heading, with status pair (empty field mid-line)
L = '40.1,-119.1,4,1,2,3,4,5,6,7,8,123519.500000,,12.345,25.1'
p = sd.parse_data_line(L)
check('15f no heading + vin/temp', p is not None and p['heading'] is None
      and p['vin'] == 12.345, repr(p))

# Idle line, no heading:  ,,,adc*8,  +  ","  +  ",vin,temp"
L = ',,,1,2,3,4,5,6,7,8,,,12.345,25.1'
p = sd.parse_data_line(L)
check('idle no heading', p is not None and p['lat'] is None and p['fix'] is None
      and p['gps_ts'] is None and p['heading'] is None and p['vin'] == 12.345
      and p['adc'] == [1,2,3,4,5,6,7,8], repr(p))

# Idle line WITH heading (bench: dual antennas near window, no fix)
L = ',,,1,2,3,4,5,6,7,8,,180.00,12.345,25.1'
p = sd.parse_data_line(L)
check('idle with heading', p is not None and p['lat'] is None
      and p['heading'] == 180.0, repr(p))

# Heading edge values
check('heading 0.00 ok',   sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,0.00') is not None)
check('heading 360.00 ok', sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,360.00') is not None)
check('heading 360.01 rejected', sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,360.01') is None)
check('heading -1 rejected',     sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,-1.0') is None)

# Corruption cases
check('short line rejected',      sd.parse_data_line('40.1,-119.1,4') is None)
check('14 fields rejected',       sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0,12.3') is None)
check('16 fields rejected',       sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0,12.3,25.0,9') is None)
check('partial GPS rejected',     sd.parse_data_line('40.1,,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('idle-with-ts rejected',    sd.parse_data_line(',,,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('garbage lat rejected',     sd.parse_data_line('4o.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('lat 91 rejected',          sd.parse_data_line('91.0,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('lon -181 rejected',        sd.parse_data_line('40.1,-181.0,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('fix 12 rejected',          sd.parse_data_line('40.1,-119.1,12,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('adc garbage rejected',     sd.parse_data_line('40.1,-119.1,4,1,2,x,4,5,6,7,8,1.0,180.0') is None)
check('adc negative rejected',    sd.parse_data_line('40.1,-119.1,4,1,-2,3,4,5,6,7,8,1.0,180.0') is None)
check('adc float rejected',       sd.parse_data_line('40.1,-119.1,4,1,2.5,3,4,5,6,7,8,1.0,180.0') is None)
check('nan lat rejected',         sd.parse_data_line('nan,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('merged lines rejected',    sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.040.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0') is None)
check('empty vin rejected',       sd.parse_data_line('40.1,-119.1,4,1,2,3,4,5,6,7,8,1.0,180.0,,25.0') is None)

# ── parse_info_line ───────────────────────────────────────────────────────────
print('parse_info_line:')
L = ('# info fw=a5c5006 gps_ver=LC29HDANR11A03S,2023/10/26,14:23:01 gps_id=? '
     'blanking_us=16 rx_window_us=3 tx_pulse_us=120 coil_spacing_mm=500 '
     'coil_offset_fore_mm=0 coil_offset_right_mm=-10 '
     'fire_mode=both adc_oversample=16 adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')
i = sd.parse_info_line(L)
check('info parse', i is not None and i['fw'] == 'a5c5006'
      and i['gps_ver'] == 'LC29HDANR11A03S,2023/10/26,14:23:01'
      and i['gps_id'] == '?' and i['blanking_us'] == '16'
      and i['tx_pulse_us'] == '120'
      and i['coil_offset_right_mm'] == '-10'
      and i['fire_mode'] == 'both'
      and i['adc'] == 'PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14', repr(i))
check('non-info # line', sd.parse_info_line('# blanking=16us  rx_window=3us') is None)
check('boot banner', sd.parse_info_line('# stm_detector3 boot') is None)

# ── timing echo regex ─────────────────────────────────────────────────────────
print('RE_TIMING:')
m = sd.RE_TIMING.search('# blanking=16us  rx_window=3us')
check('timing echo', m is not None and m.group(1) == '16' and m.group(2) == '3')
m = sd.RE_TIMING.search('# metal detector pulsing (hw-aligned): fire_mode=both tx_pulse=120us blanking=16us rx_window=3us')
check('boot pulse banner also matches', m is not None and m.group(1) == '16')

# ── tx pulse echo regex ───────────────────────────────────────────────────────
print('RE_TXPULSE:')
m = sd.RE_TXPULSE.search('# tx_pulse=90us')
check('tx pulse echo', m is not None and m.group(1) == '90')
m = sd.RE_TXPULSE.search('# metal detector pulsing (hw-aligned): fire_mode=both tx_pulse=120us blanking=16us rx_window=3us')
check('boot pulse banner tx match', m is not None and m.group(1) == '120')
m = sd.RE_TXPULSE.search('# saved (slot 3/128): blanking=16us  rx_window=3us  tx_pulse=110us  coil_spacing=500mm')
check('saved banner tx match', m is not None and m.group(1) == '110')
check('timing echo has no tx', sd.RE_TXPULSE.search('# blanking=16us  rx_window=3us') is None)

# ── fire mode echo regex ──────────────────────────────────────────────────────
print('RE_FIRE:')
m = sd.RE_FIRE.search('# fire_mode=odd')
check('fire echo', m is not None and m.group(1) == 'odd')
m = sd.RE_FIRE.search('# saved (slot 3/128): blanking=16us  rx_window=3us  '
                      'tx_pulse=110us  coil_spacing=500mm  fire_mode=even')
check('saved banner fire match', m is not None and m.group(1) == 'even')
check('bad value no match', sd.RE_FIRE.search('# fire_mode=all') is None)

# ── gate_reason ───────────────────────────────────────────────────────────────
print('gate_reason:')
def mk(lat=40.1, lon=-119.1, fix=4, heading=180.0):
    return {'lat': lat, 'lon': lon, 'fix': fix, 'heading': heading,
            'gps_ts': '1.0', 'adc': [0]*8, 'vin': None, 'temp': None}
check('rtk+heading passes', sd.gate_reason(mk(), False) == '')
check('fix=5 blocked',      sd.gate_reason(mk(fix=5), False) == 'fix=5')
check('fix=1 blocked',      sd.gate_reason(mk(fix=1), False) == 'fix=1')
check('no heading blocked', sd.gate_reason(mk(heading=None), False) == 'no heading')
check('idle blocked',       sd.gate_reason(mk(lat=None, lon=None, fix=None), False) == 'no fix')
check('no-gate: fix=1 ok',       sd.gate_reason(mk(fix=1, heading=None), True) == '')
check('no-gate: idle still out', sd.gate_reason(mk(lat=None), True) == 'no fix')

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
    'coil_offset_right_mm=0 fire_mode=both adc_oversample=16 '
    'adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')
sd.stamp_info(mc, full)
check('later info fills gaps', sd.meta_get(mc, 'coil_spacing_mm') == '500'
      and sd.meta_get(mc, 'gps_id') == 'UID9'
      and sd.meta_get(mc, 'tx_pulse_us') == '120')
check('info_ok now 1', sd.meta_get(mc, 'info_ok') == '1')
check('no false drift flags', sd.meta_get(mc, 'timing_changed') is None
      and sd.meta_get(mc, 'geometry_changed') is None
      and sd.meta_get(mc, 'fire_changed') is None)
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
# Fire-mode drift (1/2/3 keys): same flag-never-apply contract
sd.note_drift(mc, 'fire', {'fire_mode': 'odd'})
check('fire drift flagged', 'fire_mode both->odd' in (sd.meta_get(mc, 'fire_changed') or ''))
check('header fire mode untouched', sd.meta_get(mc, 'fire_mode') == 'both')
check('current fire mode tracked', sd.meta_get(mc, 'fire_mode_current') == 'odd')
check('synchronous=NORMAL', mc.execute('PRAGMA synchronous').fetchone()[0] == 1)
mc.close()

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
        b'fire_mode=both adc_oversample=16 '
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

# 1) idle lines — must not insert, live row must say 'no fix'
for _ in range(8):
    send(',,,10,20,30,40,50,60,70,80,,,12.345,25.1')
    time.sleep(0.1)
# 2) degraded: RTK float
for _ in range(6):
    send('40.10000000,-119.10000000,5,1,2,3,4,5,6,7,8,123519.000000,181.00')
    time.sleep(0.1)
# 3) RTK fixed but no heading
for _ in range(6):
    send('40.10000000,-119.10000000,4,1,2,3,4,5,6,7,8,123519.050000,')
    time.sleep(0.1)
# 4) recordable: RTK fixed + heading (same spot → first inserts, rest paused)
for _ in range(6):
    send('40.10000000,-119.10000000,4,11,22,33,44,55,66,77,88,123519.100000,90.00,12.400,25.2')
    time.sleep(0.1)
# 5) move 1 m north → second insert
send('40.10000900,-119.10000000,4,12,23,34,45,56,67,78,89,123519.150000,90.50')
time.sleep(0.4)
# 6) timing echo mid-study
send('# blanking=18us  rx_window=3us')
# 6b) coil-spacing echo mid-study (d key pressed) → geometry drift flag
send('# coil_spacing=510mm')
# 6b') tx-pulse echo mid-study (v key pressed) → timing drift flag
send('# tx_pulse=100us')
# 6b'') fire-mode echo mid-study ('2' key pressed) → fire drift flag
send('# fire_mode=even')
# 6c) noise-corrupted line at a NEW location — the high-bit byte must poison
#     the field (errors='replace'), not splice it into a valid-looking value
os.write(master, b'40.20000\xb2000,-119.20000000,4,1,2,3,4,5,6,7,8,123519.200000,45.00\n')
time.sleep(0.6)

conn = sqlite3.connect(DB)
pts  = conn.execute('SELECT lat,lon,heading,fix,adc0,adc7,gps_ts,vin,temp '
                    'FROM points ORDER BY id').fetchall()
meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
live = conn.execute('SELECT recording,reason,adc0,vin FROM live').fetchone()
live_count = conn.execute('SELECT COUNT(*) FROM live').fetchone()[0]

check('exactly 2 points recorded (corrupt-byte line dropped)', len(pts) == 2,
      f'got {len(pts)}: {pts}')
if len(pts) == 2:
    check('point1 has heading+vin', pts[0][2] == 90.0 and pts[0][7] == 12.4
          and pts[0][4] == 11, repr(pts[0]))
    check('point2 no vin (no pair on line)', pts[1][7] is None and pts[1][2] == 90.5,
          repr(pts[1]))
check('meta fw stamped', meta.get('fw_git_hash') == 'a5c5006', repr(meta.get('fw_git_hash')))
check('meta gps_id stamped', meta.get('gps_id') == 'UID123')
check('meta adc_order stamped', meta.get('adc_order') == 'PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')
check('meta schema_version=3', meta.get('schema_version') == '3')
check('meta gate=rtk+heading', meta.get('gate') == 'rtk+heading')
check('timing change flagged', 'timing_changed' in meta and 'blanking_us 16->18' in meta['timing_changed'],
      repr(meta.get('timing_changed')))
check('header blanking NOT overwritten', meta.get('blanking_us') == '16')
check('current blanking tracked', meta.get('blanking_us_current') == '18')
check('tx pulse change flagged', 'tx_pulse_us 120->100' in meta.get('timing_changed', ''),
      repr(meta.get('timing_changed')))
check('header tx pulse NOT overwritten', meta.get('tx_pulse_us') == '120')
check('current tx pulse tracked', meta.get('tx_pulse_us_current') == '100')
check('geometry change flagged', 'coil_spacing_mm 500->510' in meta.get('geometry_changed', ''),
      repr(meta.get('geometry_changed')))
check('header spacing NOT overwritten', meta.get('coil_spacing_mm') == '500')
check('current spacing tracked', meta.get('coil_spacing_mm_current') == '510')
check('fire change flagged', 'fire_mode both->even' in meta.get('fire_changed', ''),
      repr(meta.get('fire_changed')))
check('header fire mode NOT overwritten', meta.get('fire_mode') == 'both')
check('current fire mode tracked', meta.get('fire_mode_current') == 'even')
check('info_ok=1', meta.get('info_ok') == '1')
check('single live row', live_count == 1)
check('live row populated', live is not None and live[3] is not None, repr(live))

# 7) stop feeding → 'no data' after 2s
time.sleep(3.5)
live = conn.execute('SELECT recording,reason FROM live').fetchone()
check('no-data detected', live == (0, 'no data'), repr(live))

# 8) garbage stream (wrong port/baud simulation): bytes flow, nothing parses
for _ in range(30):
    send('$GNGGA,123519.00,3745.357,N,12224.916,W,1,12,0.6,9.0,M,,M,,*47')
    time.sleep(0.1)
live = conn.execute('SELECT recording,reason FROM live').fetchone()
check('garbage stream detected', live == (0, 'garbage'), repr(live))

# 9) good data resumes → live recovers
for _ in range(6):
    send('40.10000900,-119.10000000,4,12,23,34,45,56,67,78,89,123519.300000,91.00')
    time.sleep(0.1)
live = conn.execute('SELECT recording,reason FROM live').fetchone()
check('recovers after garbage', live == (1, ''), repr(live))

# 10) second daemon while first is alive → refused, PID file untouched
pid1 = open('/tmp/metal_detector_daemon.pid').read().split(':', 1)[0]
p2 = subprocess.run(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', '/dev/tty.nope', '--db', os.path.join(SCRATCH, 'other.db')],
    capture_output=True, text=True, timeout=10)
check('second daemon refused', p2.returncode == 1 and 'refusing to start' in p2.stdout,
      f'rc={p2.returncode} out={p2.stdout[:200]}')
pid_after = open('/tmp/metal_detector_daemon.pid').read().split(':', 1)[0]
check('PID file not clobbered by loser', pid_after == pid1, f'{pid1} -> {pid_after}')

# 11) exclusive port lock: a second reader on the same tty fails loudly
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

# 12) restart on the SAME db must not rewrite provenance
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
