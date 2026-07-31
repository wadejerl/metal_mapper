#!/usr/bin/env python3
"""Fake nav-GPS NMEA-over-TCP server: simulates a vehicle circling the demo
field at ~3 m/s, emitting RTK-fixed GGA+RMC at 5 Hz on port 50310."""
import math
import socket
import threading
import time

LAT0, LON0 = 40.786400, -119.206500
PORT = 50310

def cs(body):
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f'${body}*{c:02X}\r\n'

def to_nmea_lat(lat):
    d = int(abs(lat)); m = (abs(lat) - d) * 60
    return f'{d:02d}{m:07.4f}', 'N' if lat >= 0 else 'S'

def to_nmea_lon(lon):
    d = int(abs(lon)); m = (abs(lon) - d) * 60
    return f'{d:03d}{m:07.4f}', 'E' if lon >= 0 else 'W'

def vehicle(t):
    # ellipse around the field, ~3 m/s
    ang = t * 0.06
    x = 20 + 26 * math.cos(ang)
    y = 12 + 16 * math.sin(ang)
    lat = LAT0 + y / 111320.0
    lon = LON0 + x / (111320.0 * math.cos(math.radians(LAT0)))
    # course = tangent direction
    dx, dy = -26 * math.sin(ang), 16 * math.cos(ang)
    cog = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    spd = math.hypot(dx, dy) * 0.06
    return lat, lon, cog, spd

def serve(conn):
    try:
        t0 = time.time()
        while True:
            t = time.time() - t0
            lat, lon, cog, spd = vehicle(t)
            la, ns = to_nmea_lat(lat)
            lo, ew = to_nmea_lon(lon)
            hms = time.strftime('%H%M%S') + '.00'
            kn = spd / 0.514444
            conn.sendall(
                (cs(f'GNGGA,{hms},{la},{ns},{lo},{ew},4,18,0.7,12.0,M,-25.0,M,1.0,0') +
                 cs(f'GNRMC,{hms},A,{la},{ns},{lo},{ew},{kn:.2f},{cog:.1f},260726,,,R')
                 ).encode())
            time.sleep(0.2)
    except OSError:
        pass
    finally:
        conn.close()

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', PORT))
srv.listen(4)
print(f'fake nav GPS on tcp://127.0.0.1:{PORT}', flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
