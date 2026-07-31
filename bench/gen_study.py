#!/usr/bin/env python3
"""Generate synthetic detector3 studies for db_map browser verification.

Study 'serpentine_demo': a lawnmower pattern over a ~40x25 m field with two
buried targets (one hot, one negative response), RTK fixed, heading present,
vin/temp at 1 Hz — written with serial_daemon's own db_open/insert_point so
the schema is exactly what the real daemon produces.

Study 'warn_demo': small study carrying drift flags + identity failure, to
exercise the warning pills and banner states.
"""
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import serial_daemon as sd
import db_map  # for coil_positions (the tested reference)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'studies')
os.makedirs(OUT, exist_ok=True)

LAT0, LON0 = 40.786400, -119.206500          # Black Rock City, on the overlays
M_LAT = 111320.0
M_LON = M_LAT * math.cos(math.radians(LAT0))

def ll(x_east_m, y_north_m):
    return LAT0 + y_north_m / M_LAT, LON0 + x_east_m / M_LON

# Targets: (x, y, amplitude in counts, radius m)
TARGETS = [(12.0, 6.0, +900, 1.2), (28.0, 14.0, -650, 1.5)]

HEADER = {
    'fw_git_hash': '6d1ec54', 'gps_ver': 'LC29HDANR11A03S,2023/10/26',
    'gps_id': 'UID-DEMO-1', 'blanking_us': '16', 'rx_window_us': '3',
    'tx_pulse_us': '120', 'coil_spacing_mm': '500',
    'coil_offset_fore_mm': '0', 'coil_offset_right_mm': '0',
    'adc_order': 'PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14',
    'info_ok': '1', 'schema_version': '3', 'gate': 'rtk+heading',
    'min_dist_mm': '20',
}

def adc_at(lat, lon, heading):
    pos = db_map.coil_positions(lat, lon, heading, 500, 0, 0)
    out = []
    for ch in range(8):
        v = 2000.0 + ((ch * 37) % 11) - 5          # per-channel baseline-ish
        clat, clon = pos[ch]
        cx = (clon - LON0) * M_LON
        cy = (clat - LAT0) * M_LAT
        for tx, ty, amp, rad in TARGETS:
            d2 = (cx - tx) ** 2 + (cy - ty) ** 2
            v += amp * math.exp(-d2 / (2 * rad * rad))
        # deterministic "noise"
        v += 3.0 * math.sin(cx * 7.3 + cy * 3.1 + ch)
        out.append(max(0, min(65535, int(round(v)))))
    return out

def gen_serpentine():
    path = os.path.join(OUT, 'serpentine_demo.db')
    if os.path.exists(path):
        os.unlink(path)
    conn = sd.db_open(path)
    sd.meta_set(conn, 'study_name', 'serpentine_demo')
    sd.meta_set(conn, 'created_at', str(time.time()))
    for k, v in HEADER.items():
        sd.meta_set(conn, k, v)

    speed, dt = 2.0, 0.2                      # 2 m/s, 10 Hz points
    step = speed * dt
    t = time.time() - 3600
    gps_s = 120000.0
    n = 0
    y = 2.0
    passes = 7
    for row in range(passes):
        east = (row % 2 == 0)
        heading = 90.0 if east else 270.0
        xs = [2.0 + i * step for i in range(int(36 / step))]
        if not east:
            xs = [38.0 - (x - 2.0) for x in xs]
        for x in xs:
            lat, lon = ll(x, y)
            pt = {'lat': round(lat, 9), 'lon': round(lon, 9),
                  'heading': heading + 2.0 * math.sin(x / 5.0),
                  'fix': 4, 'adc': adc_at(lat, lon, heading),
                  'gps_ts': f'{gps_s:.2f}',
                  'vin': round(12.3 + 0.05 * math.sin(n / 40.0), 3) if n % 10 == 0 else None,
                  'temp': round(38.0 + 0.3 * math.sin(n / 60.0), 1) if n % 10 == 0 else None}
            # emulate the daemon's insert (it expects vin/temp keys present)
            conn.execute('BEGIN')
            conn.execute(
                'INSERT INTO points (ts,lat,lon,heading,fix,'
                'adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,vin,temp) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (t, pt['lat'], pt['lon'], pt['heading'], pt['fix'],
                 *pt['adc'], pt['gps_ts'], pt['vin'], pt['temp']))
            conn.commit()
            t += dt
            gps_s += dt
            n += 1
        y += 3.5                              # next pass, one array-width over
    conn.close()
    print(f'serpentine_demo: {n} points, targets at {TARGETS}')

def gen_warn():
    path = os.path.join(OUT, 'warn_demo.db')
    if os.path.exists(path):
        os.unlink(path)
    conn = sd.db_open(path)
    sd.meta_set(conn, 'study_name', 'warn_demo')
    sd.meta_set(conn, 'created_at', str(time.time()))
    h = dict(HEADER)
    h.update({'gps_id': '?', 'info_ok': '0'})
    for k, v in h.items():
        sd.meta_set(conn, k, v)
    sd.meta_set(conn, 'timing_changed', f'{time.time():.0f}:tx_pulse_us 120->80')
    sd.meta_set(conn, 'tx_pulse_us_current', '80')
    sd.meta_set(conn, 'geometry_changed', f'{time.time():.0f}:coil_spacing_mm 500->510')
    sd.meta_set(conn, 'coil_spacing_mm_current', '510')
    t = time.time() - 600
    for i in range(60):
        lat, lon = ll(2 + i * 0.3, 0.0)
        conn.execute(
            'INSERT INTO points (ts,lat,lon,heading,fix,'
            'adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts,vin,temp) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (t + i, lat, lon, 90.0, 4, *[2000 + c for c in range(8)],
             f'{130000+i:.2f}', None, None))
    conn.commit()
    conn.close()
    print('warn_demo: 60 points with drift + identity warnings')

gen_serpentine()
gen_warn()
