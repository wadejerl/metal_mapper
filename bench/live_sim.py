#!/usr/bin/env python3
"""Full-stack live simulator: a pty 'firmware' + the REAL serial_daemon.

Speaks the detector3 protocol on a pty: answers the 'I' handshake, streams
sample lines (adc+tick, 50 Hz) with anchor lines (gps+heading+tick, 10 Hz)
cycling through phases (RTK recording -> RTK-float degraded -> idle/no-fix,
where anchors stop but samples keep streaming), and honors tuning keys
(a/z s/x f/v g/b I, plus the word-gated SAVE/CLEAR + enter) with the same
echo formats as the firmware — so the web timing panel round-trips for
real. g/b step the ADVERTISED sample rate through the firmware's
20/40/100/200/500 Hz list (echo only — the pty keeps streaming at its
scaled-down real rate; the daemon is rate-agnostic so nothing downstream
cares).

Writes the study to bench/studies/live_demo.db (gitignored); run db_map
with --studies-dir bench/studies to view it. Ctrl-C / kill to stop (the
daemon is SIGTERMed on exit).
"""
import math
import os
import pty
import random
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
RATES = [20, 40, 100, 200, 500]     # mirrors md_rate_steps * 20 Hz
rate_idx = 4                        # firmware default: 500 Hz
ADC_NOISE = [2, 18, 4, 9, 1, 27, 6, 12]   # per-channel σ, deliberately uneven


def send(s):
    try:
        os.write(master, (s + '\r\n').encode())
    except BlockingIOError:
        pass                      # reader stalled; drop the line like a UART


def info():
    send(f'# info fw=6d1ec54 gps_ver=LC29HDANR11A03S,2023/10/26 '
         f'gps_id=UID-DEMO-1 blanking_us={blanking} rx_window_us={rx_win} '
         f'tx_pulse_us={tx_pulse} coil_spacing_mm=500 coil_offset_fore_mm=0 '
         f'coil_offset_right_mm=0 '
         f'sample_rate_hz={RATES[rate_idx]} '
         f'adc_oversample=16 adc=PA0,PA1,PA6,PA7,PB1,PB13,PB12,PB14')


SAVE_WORD = 'SAVE'
CLEAR_WORD = 'CLEAR'
save_pos = 0                # chars matched (mirrors the firmware matchers)
clear_pos = 0


def _word_step(word, pos, ch):
    """One matcher step; returns (new_pos, consumed) — same semantics as
    the firmware's md_word_step()."""
    if pos < len(word) and ch == word[pos]:
        return pos + 1, True
    if pos > 0:
        pos = 1 if ch == word[0] else 0
        return pos, pos != 0
    return 0, False


def _send_saved():
    send(f'# saved (slot 4/128): blanking={blanking}us  '
         f'rx_window={rx_win}us  tx_pulse={tx_pulse}us  '
         f'coil_spacing=500mm  '
         f'sample_rate={RATES[rate_idx]}Hz')


def handle_keys():
    global blanking, rx_win, tx_pulse, save_pos, clear_pos, rate_idx
    try:
        data = os.read(master, 64)
    except (BlockingIOError, OSError):
        return
    for b in data:
        ch = chr(b)
        # Word-gated flash writes, same matchers as md_console_poll():
        # SAVE+Enter saves, CLEAR+Enter restores defaults + saves.
        if save_pos == 4:
            save_pos = 0
            if ch in '\r\n':
                _send_saved()
                clear_pos = 0
                continue
        if clear_pos == 5:
            clear_pos = 0
            if ch in '\r\n':
                blanking, rx_win, tx_pulse, rate_idx = 16, 3, 120, 4
                send('# defaults restored')
                _send_saved()
                save_pos = 0
                continue
        save_pos, used_s = _word_step(SAVE_WORD, save_pos, ch)
        clear_pos, used_c = _word_step(CLEAR_WORD, clear_pos, ch)
        if used_s or used_c:
            continue
        if ch == 'I':
            info()
        elif ch == 'a':
            blanking = min(200, blanking + 1)
            send(f'# blanking={blanking}us  rx_window={rx_win}us')
        elif ch == 'z':
            blanking = max(0, blanking - 1)
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
        elif ch in 'gb':
            rate_idx = (min(len(RATES) - 1, rate_idx + 1) if ch == 'g'
                        else max(0, rate_idx - 1))
            send(f'# sample_rate={RATES[rate_idx]}Hz '
                 f'({RATES[rate_idx] // 20} per fix)')


def cleanup(*_):
    daemon.send_signal(signal.SIGTERM)
    sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)

print(f'live sim on {port}, db={DB}', flush=True)

t0 = time.time()
gps_s = 140000.0
tick = 0
last_status = -10.0
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
        # Asymmetric per-channel noise, like the real array — exercises the
        # raw screen's per-channel σ readout with visibly different figures.
        v += random.gauss(0.0, ADC_NOISE[chn])
        adc.append(int(v))
    adc_csv = ','.join(map(str, adc))
    # status pair rides on whichever data line is due once per second
    if t - last_status >= 1.0:
        tail = (f',{12.3 + 0.05 * math.sin(t / 10):.3f}'
                f',{40 + 0.5 * math.sin(t / 30):.1f}')
        last_status = t
    else:
        tail = ''

    if phase < 45.0:      # anchors flowing: RTK fixed, then RTK-float degraded
        fix = 4 if phase < 30.0 else 5
        for _ in range(5):        # 5 sample frames per fix interval
            tick += 1
            send(f',,,{adc_csv},,,{tick}')
        send(f'{lat:.9f},{lon:.9f},{fix},,,,,,,,,{gps_s:.6f},{heading:.2f},'
             f'{tick}' + tail)
    else:                 # idle: no fix (bench/indoors) — samples keep
        for i in range(5):    # streaming at full rate, no anchors
            tick += 1
            send(f',,,{adc_csv},,,{tick}' + (tail if i == 4 else ''))

    gps_s += 0.1
    time.sleep(0.1)
