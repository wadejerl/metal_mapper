#!/usr/bin/env python3
"""Generate 'drift_demo': a no-gate bench study with NO headings — what a
stationary unit records when GPS drift (no RTK) fakes the movement. Every
point has lat/lon + fix=1 but heading NULL, so the view must fall back to
antenna dots + track instead of the coil carpet."""
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import serial_daemon as sd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'studies')
LAT0, LON0 = 40.786400, -119.206500
M_LAT = 111320.0
M_LON = M_LAT * math.cos(math.radians(LAT0))

path = os.path.join(OUT, 'drift_demo.db')
for ext in ('', '-wal', '-shm'):
    try:
        os.unlink(path + ext)
    except FileNotFoundError:
        pass

conn = sd.db_open(path)
sd.meta_set(conn, 'study_name', 'drift_demo')
sd.meta_set(conn, 'created_at', str(time.time()))
for k, v in {'fw_git_hash': '6d1ec54', 'gps_id': 'UID-DEMO-1',
             'blanking_us': '16', 'rx_window_us': '3', 'tx_pulse_us': '120',
             'coil_spacing_mm': '500', 'coil_offset_fore_mm': '0',
             'coil_offset_right_mm': '0', 'info_ok': '1',
             'schema_version': '3', 'gate': 'none',
             'min_dist_mm': '20'}.items():
    sd.meta_set(conn, k, v)

# deterministic drift walk (sums of incommensurate sines ~ GPS wander)
t = time.time() - 900
x = y = 0.0
for i in range(300):
    x += 0.25 * math.sin(i * 0.31) + 0.15 * math.sin(i * 0.071 + 1.0)
    y += 0.25 * math.cos(i * 0.23) + 0.15 * math.sin(i * 0.113 + 2.0)
    lat = LAT0 + y / M_LAT
    lon = LON0 + x / M_LON
    adc = [2000 + ((ch * 37) % 11) - 5 + (300 if ch == 2 and 100 < i < 130 else 0)
           for ch in range(8)]
    conn.execute(
        'INSERT INTO points (ts,lat,lon,heading,fix,'
        'adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,vin,temp) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (t + i * 2, round(lat, 9), round(lon, 9), None, 1, *adc,
         f'{150000 + i:.2f}', None, None))
conn.commit()
conn.close()
print('drift_demo: 300 heading-less points')
