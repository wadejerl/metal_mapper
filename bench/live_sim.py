#!/usr/bin/env python3
"""Full-stack live simulator: a pty 'firmware' + the REAL serial_daemon.

Speaks the detector3 protocol on a pty: answers the 'I' handshake, streams
CSV at 10 Hz cycling through phases (RTK recording -> RTK-float degraded ->
idle/no-fix), and honors tuning keys (a/z s/x f/v S I) with the same echo
formats as the firmware — so the web timing panel round-trips for real.

Writes the study into the scratchpad studies dir where the db_map demo
server is looking. Ctrl-C / kill to stop (daemon is SIGTERMed on exit).
"""
import math
import os
import pty
import signal
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'studies', 'live_demo.db')

LAT0, LON0 = 40.786400, -119.206500
M_LAT = 111320.0
M_LON = M_LAT * math.cos(math.radians(LAT0))

master, slave = pty.openpty()
os.set_blocking(master, False)
port = os.ttyname(slave)

for ext in ('', '-wal', '-shm'):
    try:
        os.unlink(DB + ext)
    except FileNotFoundError:
        pass

daemon = subprocess.Popen(
    [sys.executable, os.path.join(REPO, 'serial_daemon.py'),
     '--port', port, '--db', DB, '--study-name', 'live_demo',
     '--baud', '115200', '--min-dist', '20'],
    stdout=open(os.path.join(HERE, 'live_daemon.log'), 'w'),
    stderr=subprocess.STDOUT)

blanking, rx_win, tx_pulse = 16, 3, 120


def send(s):
    try:
        os.write(master, (s + '\r\n').encode())
    except BlockingIOError:
        pass                      # reader stalled; drop the line like a UART


def info():
    send(f'# info fw=6d1ec54 gps_ver=LC29HDANR11A03S,2023/10/26 '
         f'gps_id=UID-DEMO-1 blanking_us={blanking} rx_window_us={rx_win} '
         f'tx_pulse_us={tx_pulse} coil_spacing_mm=500 coil_offset_fore_mm=0 '
         f'coil_offset_right_mm=0 adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')


def handle_keys():
    global blanking, rx_win, tx_pulse
    try:
        data = os.read(master, 64)
    except (BlockingIOError, OSError):
        return
    for b in data:
        ch = chr(b)
        if ch == 'I':
            info()
        elif ch == 'a':
            blanking = min(200, blanking + 2)
            send(f'# blanking={blanking}us  rx_window={rx_win}us')
        elif ch == 'z':
            blanking = max(0, blanking - 2)
            send(f'# blanking={blanking}us  rx_window={rx_win}us')
        elif ch == 's':
            rx_win = min(50, rx_win + 1)
            send(f'# blanking={blanking}us  rx_window={rx_win}us')
        elif ch == 'x':
            rx_win = max(1, rx_win - 1)
            send(f'# blanking={blanking}us  rx_window={rx_win}us')
        elif ch == 'f':
            tx_pulse = min(120, tx_pulse + 1)
            send(f'# tx_pulse={tx_pulse}us')
        elif ch == 'v':
            tx_pulse = max(10, tx_pulse - 1)
            send(f'# tx_pulse={tx_pulse}us')
        elif ch == 'S':
            send(f'# saved (slot 4/128): blanking={blanking}us  '
                 f'rx_window={rx_win}us  tx_pulse={tx_pulse}us  '
                 f'coil_spacing=500mm')


def cleanup(*_):
    daemon.send_signal(signal.SIGTERM)
    sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)

print(f'live sim on {port}, db={DB}', flush=True)

t0 = time.time()
gps_s = 140000.0
n = 0
while True:
    handle_keys()
    t = time.time() - t0
    phase = (t % 60.0)
    # vehicle: crawls east then west along y=30, 1.5 m/s
    leg = (t * 1.5) % 72.0
    x = leg if leg <= 36.0 else 72.0 - leg
    heading = 90.0 if leg <= 36.0 else 270.0
    lat = LAT0 + 30.0 / M_LAT
    lon = LON0 + x / M_LON
    adc = []
    for chn in range(8):
        v = 2000 + ((chn * 37) % 11) - 5
        v += 700 * math.exp(-((x - 18.0) ** 2) / 8.0) if chn in (3, 4) else 0
        adc.append(int(v))
    vin = f'{12.3 + 0.05 * math.sin(t / 10):.3f}' if n % 10 == 0 else None
    tempv = f'{40 + 0.5 * math.sin(t / 30):.1f}' if n % 10 == 0 else None
    tail = f',{vin},{tempv}' if vin else ''

    if phase < 30.0:      # recording: RTK fixed + heading
        send(f'{lat:.9f},{lon:.9f},4,' + ','.join(map(str, adc)) +
             f',{gps_s:.6f},{heading:.2f}' + tail)
    elif phase < 45.0:    # degraded: RTK float, heading present
        send(f'{lat:.9f},{lon:.9f},5,' + ','.join(map(str, adc)) +
             f',{gps_s:.6f},{heading:.2f}' + tail)
    else:                 # idle: no fix (bench/indoors), status pair always
        send(',,,' + ','.join(map(str, adc)) + ',,' +
             f',{vin or "12.3"},{tempv or "40.0"}')
        time.sleep(0.4)   # idle lines are 2 Hz on the wire

    gps_s += 0.1
    n += 1
    time.sleep(0.1)
