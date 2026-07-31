#!/usr/bin/env python3
"""
db_map.py — Metal Mapper web UI (detector3 schema, schema_version 3).

Single Flask app, multiple views over one core:
    /               study list + daemon/system control      (engineering)
    /view/<id>      full-control map view                    (engineering)
    /run            operator kiosk view, 800x480             (planned next)

The core reads study DBs written by serial_daemon.py (points/live/meta),
derives ALL status truth server-side in derive_status() — views render
it, they never re-derive — and folds in the optional nav-GPS subsystem
(nav_gps.py, NMEA over TCP). The detector daemon and the nav GPS are
each independently optional: with neither present this is a saved-study
viewer, which is a fully supported mode.

Configuration lives in ~/metal_mapper/config.json (serial_port, baud,
nav_host, nav_port, studies_dir) and is editable from the index page;
CLI flags override for bench runs, e.g. on a Mac with the hardware:

    python3 db_map.py --serial-port /dev/cu.usbmodem1103 \\
                      --nav-host 192.168.1.50 --nav-port 50012

Deployment (Pi): gunicorn 'db_map:app' — workers=1, gthread. Every open
map view holds one SSE thread, and a client that vanishes without FIN
(WiFi drop, laptop lid) pins its thread until the next write fails —
size threads well above the expected view count: 8 minimum, 16 is cheap.
"""

import argparse
import datetime
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_file,
                   stream_with_context, url_for)

from nav_gps import NavReader

CONFIG_FILE   = Path.home() / 'metal_mapper' / 'config.json'
FIFO_PATH     = '/tmp/metal_detector_cmd.fifo'
PID_FILE      = '/tmp/metal_detector_daemon.pid'
DAEMON_SCRIPT = Path(__file__).parent / 'serial_daemon.py'
STATIC_DIR    = Path(__file__).parent / 'static'

LIVE_STALE_S  = 3.0    # live.updated_at older than this + PID alive = wedged
SSE_PERIOD_S  = 0.5

DEFAULT_CONFIG = {
    'serial_port': '/dev/ttyACM0',
    'baud':        460800,
    'nav_host':    '127.0.0.1',
    'nav_port':    50012,
    'studies_dir': str(Path.home() / 'metal_mapper' / 'studies'),
}

# CLI overrides (bench runs); never written back to config.json.
_cli_overrides = {}

_config_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_FILE.read_text())
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except Exception:
        pass
    cfg.update(_cli_overrides)
    # A hand-edited config.json must never take the app down (the saved-study
    # viewer has to work with NO working config): coerce or fall back per key.
    for key in ('serial_port', 'nav_host', 'studies_dir'):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            cfg[key] = DEFAULT_CONFIG[key]
    for key, hi in (('baud', 100_000_000), ('nav_port', 65535)):
        try:
            cfg[key] = int(cfg[key])
            if not (0 < cfg[key] <= hi):
                raise ValueError
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CONFIG[key]
    return cfg


def save_config(**kwargs):
    """Merge kwargs into config.json. Returns False instead of raising on an
    unwritable filesystem — persistence is best-effort, never a 500. The
    write is tmp+rename so a power cut can't leave a truncated config."""
    with _config_lock:
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        cfg.update(kwargs)
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(cfg, indent=2))
            os.replace(tmp, CONFIG_FILE)
            return True
        except OSError as e:
            print(f'[db_map] WARNING: could not write {CONFIG_FILE}: {e}',
                  flush=True)
            return False


def studies_dir():
    d = Path(load_config()['studies_dir'])
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Leaflet bootstrap (offline-friendly once cached) ─────────────────────────

_LEAFLET_VER  = '1.9.4'
_LEAFLET_BASE = f'https://unpkg.com/leaflet@{_LEAFLET_VER}/dist'


def ensure_leaflet():
    STATIC_DIR.mkdir(exist_ok=True)
    for fname in ('leaflet.js', 'leaflet.css'):
        dest = STATIC_DIR / fname
        if not dest.exists():
            url = f'{_LEAFLET_BASE}/{fname}'
            print(f'[db_map] downloading {url}', flush=True)
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                print(f'[db_map] WARNING: could not download {fname}: {e}',
                      flush=True)


ensure_leaflet()

_VERSION_FILE = Path(__file__).parent / 'version.txt'


def _git_hash():
    try:
        h = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=Path(__file__).parent, stderr=subprocess.DEVNULL
        ).decode().strip()
        _VERSION_FILE.write_text(h)
        return h
    except Exception:
        pass
    try:
        return _VERSION_FILE.read_text().strip()
    except Exception:
        return 'unknown'


GIT_HASH = _git_hash()

app = Flask(__name__)


@app.before_request
def _reject_cross_origin_posts():
    """Browsers attach an Origin header to cross-site POSTs. This UI is
    same-origin only, so a mismatched Origin means some other web page is
    trying to drive the instrument (stop a recording, retune the detector).
    Non-browser clients send no Origin and are unaffected.

    Hostnames only — ports are deliberately ignored. A proxy that strips
    the port from Host (nginx's $host, ssh -L tunnels) would otherwise 403
    every legitimate POST, and a same-host-different-port 'attacker' is
    already on the instrument itself."""
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin:
            o_host = (urlparse(origin).hostname or '').lower()
            r_host = (urlparse('//' + request.host).hostname or '').lower()
            if o_host != r_host:
                abort(403)


# ── Nav subsystem (lazy singleton — one reader thread per worker) ────────────

_nav = None
_nav_lock = threading.Lock()


def nav():
    global _nav
    if _nav is None:
        with _nav_lock:            # two first requests must not both start()
            if _nav is None:
                cfg = load_config()
                reader = NavReader(cfg['nav_host'], cfg['nav_port'])
                reader.start()
                _nav = reader
    return _nav


# ── Template filters ──────────────────────────────────────────────────────────

@app.template_filter('basename')
def _basename(s):
    return os.path.basename(s) if s else ''


@app.template_filter('ts_fmt')
def _ts_fmt(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return '—'


# ── Daemon / liveness ─────────────────────────────────────────────────────────

def get_daemon_status():
    """Read PID file; return {running, pid, db_path}."""
    try:
        content = Path(PID_FILE).read_text().strip()
        pid_str, db_path = content.split(':', 1)
        pid = int(pid_str)
        try:
            # A daemon we spawned that died hard (OOM, kill -9) is a zombie
            # child of THIS process and would pass the kill(pid, 0) probe
            # forever, wedging start/stop. Reap it here.
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                raise ProcessLookupError
        except ChildProcessError:
            pass                 # not our child — probe liveness normally
        os.kill(pid, 0)          # raises OSError if the process is gone
        return {'running': True, 'pid': pid, 'db_path': os.path.abspath(db_path)}
    except Exception:
        return {'running': False, 'pid': None, 'db_path': None}


def is_live(db_path):
    """True iff the daemon process is attached to this specific db file.
    (Says nothing about whether data is flowing — derive_status does.)"""
    s = get_daemon_status()
    return s['running'] and s['db_path'] == os.path.abspath(str(db_path))


# ── Study DB access ───────────────────────────────────────────────────────────

_STUDY_ID_RE = re.compile(r'[A-Za-z0-9_\-.]+')


def safe_study_id(study_id):
    """Study ids map straight to filenames — reject anything path-like."""
    if not _STUDY_ID_RE.fullmatch(study_id) or '..' in study_id:
        abort(404)
    return study_id


def get_db_path(study_id):
    return str(studies_dir() / f'{safe_study_id(study_id)}.db')


def db_ro(db_path):
    """Read-only open; safe concurrently with the WAL-mode daemon."""
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def read_meta(db_path):
    try:
        conn = db_ro(db_path)
        m = dict(conn.execute('SELECT key,value FROM meta').fetchall())
        conn.close()
        return m
    except Exception:
        return {}


_POINT_COLS = ('id', 'ts', 'lat', 'lon', 'heading', 'fix',
               'adc0', 'adc1', 'adc2', 'adc3', 'adc4', 'adc5', 'adc6', 'adc7',
               'gps_ts', 'vin', 'temp')


def load_points(db_path, since_id=0):
    """Points as dicts; adc packed into a list, values left as raw counts —
    all channel math (zeros, ranges, coil positions) happens client-side."""
    try:
        conn = db_ro(db_path)
        rows = conn.execute(
            f'SELECT {",".join(_POINT_COLS)} FROM points WHERE id > ? ORDER BY id',
            (since_id,)).fetchall()
        conn.close()
    except Exception:
        return []
    return [
        {
            'id': r[0], 'ts': r[1], 'lat': r[2], 'lon': r[3],
            'heading': r[4], 'fix': r[5],
            'adc': list(r[6:14]),
            'gps_ts': r[14], 'vin': r[15], 'temp': r[16],
        }
        for r in rows
    ]


_LIVE_COLS = ('updated_at', 'data_at', 'lat', 'lon', 'fix', 'heading',
              'adc0', 'adc1', 'adc2', 'adc3', 'adc4', 'adc5', 'adc6', 'adc7',
              'vin', 'temp', 'gps_ts', 'recording', 'reason')


def read_live(db_path):
    """The daemon's single live row, or None (pre-first-heartbeat)."""
    try:
        conn = db_ro(db_path)
        r = conn.execute(
            f'SELECT {",".join(_LIVE_COLS)} FROM live WHERE id=1').fetchone()
        conn.close()
    except Exception:
        return None
    if r is None:
        return None
    d = dict(zip(_LIVE_COLS, r))
    d['adc'] = [d.pop(f'adc{i}') for i in range(8)]
    return d


# ── Status derivation — THE single source of truth for every view ────────────

TIMING_KEYS = ('blanking_us', 'rx_window_us', 'tx_pulse_us')


def derive_status(db_path, meta=None):
    """Detector-side status for one study db. Views render this verbatim.

    state: 'recording' | 'not_recording' | 'wedged' | 'stopped'
      - wedged: daemon PID alive & attached but live.updated_at is stale
        (or the row never appeared) — restart territory.
      - stopped: no daemon attached to this db (saved-study view).
    """
    if meta is None:
        meta = read_meta(db_path)
    live = read_live(db_path)
    now = time.time()
    mine = is_live(db_path)

    if mine:
        if live is None or now - live['updated_at'] > LIVE_STALE_S:
            state = 'wedged'
        elif live['recording']:
            state = 'recording'
        else:
            state = 'not_recording'
    else:
        state = 'stopped'

    warnings = []
    if meta:
        if meta.get('gps_id') == '?' or meta.get('info_ok') == '0':
            warnings.append('GPS / unit-identity failure — info line incomplete '
                            'or GPS module not answering')
        for kind in ('timing', 'geometry', 'fire'):
            v = meta.get(kind + '_changed')
            if v:
                diffs = v.split(':', 1)[1] if ':' in v else v
                warnings.append(f'{kind} changed mid-study ({diffs}) — '
                                f'data before/after does not line up')

    timing = {k: meta.get(k + '_current') or meta.get(k) for k in TIMING_KEYS}

    return {
        'state':         state,
        'reason':        (live or {}).get('reason', ''),
        'live':          live,
        'live_age_s':    round(now - live['updated_at'], 1) if live else None,
        'warnings':      warnings,
        'timing':        timing,
        'motion_mm':     meta.get('motion_mm'),
        'motion_paused': meta.get('motion_paused'),
    }


# ── Coil geometry (Python reference; static/map_core.js mirrors this) ────────
#
# ASSUMED ARRAY MODEL: the 8 coils sit in a line PERPENDICULAR to the
# direction of travel (cross-track), centered on the array center, which is
# (coil_offset_fore_mm forward, coil_offset_right_mm starboard) of the GPS
# antenna. adc index i = coil i, laid out port -> starboard, so coil 0 is
# far left and coil 7 far right at offsets (i - 3.5) * spacing.
#
# The detector GPS may report heading along its antenna baseline rather than
# the direction of travel — mounting-dependent, unknown until field test.
# heading_off_deg (per-study meta sl_heading_offset_deg, set from the view)
# is added to the reported heading before any of this math to absorb that.

_M_PER_DEG_LAT = 111320.0


def coil_positions(lat, lon, heading_deg, spacing_mm, fore_mm, right_mm,
                   heading_off_deg=0.0):
    """[[lat, lon] x 8] for one antenna fix. heading in true degrees."""
    h = math.radians(heading_deg + heading_off_deg)
    fwd_n, fwd_e = math.cos(h), math.sin(h)          # forward unit (N, E)
    rgt_n, rgt_e = -math.sin(h), math.cos(h)         # starboard unit (N, E)
    fore_m = fore_mm / 1000.0
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat))
    out = []
    for i in range(8):
        right_m = (right_mm + (i - 3.5) * spacing_mm) / 1000.0
        d_n = fore_m * fwd_n + right_m * rgt_n
        d_e = fore_m * fwd_e + right_m * rgt_e
        out.append([lat + d_n / _M_PER_DEG_LAT,
                    lon + d_e / m_per_deg_lon])
    return out


# ── Study list ────────────────────────────────────────────────────────────────

def list_studies():
    studies = []
    for p in sorted(studies_dir().glob('*.db'),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            conn = db_ro(str(p))
            meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
            count = conn.execute('SELECT COUNT(*) FROM points').fetchone()[0]
            latest = conn.execute(
                'SELECT ts FROM points ORDER BY id DESC LIMIT 1').fetchone()
            conn.close()
        except Exception:
            meta, count, latest = {}, 0, None
        studies.append({
            'id':         p.stem,
            'name':       meta.get('study_name', p.stem),
            'created_at': meta.get('created_at', p.stat().st_ctime),
            'count':      count,
            'latest_ts':  latest[0] if latest else None,
            'live':       is_live(str(p)),
            'fw':         meta.get('fw_git_hash', ''),
        })
    return studies


# ── Views ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    cfg = load_config()
    default_name = datetime.datetime.now().strftime('geo_%Y%m%d_%H%M%S')
    return render_template(
        'index.html',
        studies=list_studies(),
        daemon_status=get_daemon_status(),
        nav=nav().snapshot(),
        cfg=cfg,
        error=request.args.get('error'),
        default_study_name=default_name,
        git_hash=GIT_HASH,
    )


@app.route('/view/<study_id>')
def view_study(study_id):
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return redirect(url_for('index'))
    meta = read_meta(db_path)
    try:
        conn = db_ro(db_path)
        first = conn.execute('SELECT lat,lon FROM points ORDER BY id LIMIT 1').fetchone()
        conn.close()
        center = [first[0], first[1]] if first else [37.7749, -122.4194]
    except Exception:
        center = [37.7749, -122.4194]
    geojson_files = sorted(p.name for p in Path(__file__).parent.glob('*.geojson'))
    return render_template(
        'view.html',
        study_id=study_id,
        study_name=meta.get('study_name', study_id),
        is_live=is_live(db_path),
        map_center=center,
        geojson_files=geojson_files,
        git_hash=GIT_HASH,
    )


# ── JSON API ──────────────────────────────────────────────────────────────────

@app.route('/data/<study_id>')
def get_data(study_id):
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    points = load_points(db_path)
    meta = read_meta(db_path)
    return jsonify({
        'points':  points,
        'last_id': points[-1]['id'] if points else 0,
        'is_live': is_live(db_path),
        'meta':    meta,
        'status':  derive_status(db_path, meta),
        'nav':     nav().snapshot(),
    })


@app.route('/navfix')
def navfix():
    return jsonify(nav().snapshot())


@app.route('/stream/<study_id>')
def stream_study(study_id):
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        abort(404)

    def as_id(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    # The browser's SSE auto-reconnect re-requests the SAME URL, so the
    # query param alone would replay the whole session. Each tick carries
    # an 'id:' field and reconnects resume from Last-Event-ID instead.
    since_id = as_id(request.headers.get('Last-Event-ID',
                                         request.args.get('since_id', 0)))

    def gen():
        nonlocal since_id
        while True:
            rows = load_points(db_path, since_id)
            if rows:
                since_id = rows[-1]['id']
            payload = {
                'type':   'tick',
                'rows':   rows,
                'status': derive_status(db_path),
                'nav':    nav().snapshot(),
            }
            yield f'id: {since_id}\ndata: {json.dumps(payload)}\n\n'
            time.sleep(SSE_PERIOD_S)

    return Response(
        stream_with_context(gen()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/settings/<study_id>', methods=['POST'])
def save_settings(study_id):
    """Persist per-study view settings (sliders, channel mask, zeros)."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(force=True)
    mapping = {
        'sl_plus_range':  data.get('plus_range'),
        'sl_minus_range': data.get('minus_range'),
        'sl_offset':      data.get('offset'),
        'sl_dot_size':    data.get('dot_size'),
        'sl_channels':    json.dumps(data['channels']) if data.get('channels') is not None else None,
        'sl_zero8':       json.dumps(data['zero8']) if data.get('zero8') is not None else None,
        'sl_heading_offset_deg': data.get('heading_offset'),
    }
    try:
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA busy_timeout=1000')
        for key, val in mapping.items():
            if val is not None:
                conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                             (key, str(val)))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/recalibrate/<study_id>', methods=['POST'])
def recalibrate(study_id):
    """Per-channel zero baselines from the last 20 points (bench: park the
    array over clean ground, hit re-zero). Persisted immediately."""
    db_path = get_db_path(study_id)
    try:
        conn = db_ro(db_path)
        rows = conn.execute(
            'SELECT adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7 '
            'FROM points ORDER BY id DESC LIMIT 20').fetchall()
        conn.close()
    except Exception:
        return jsonify({'success': False, 'error': 'DB error'})
    if not rows:
        return jsonify({'success': False, 'error': 'No data yet'})
    zero8 = [round(sum(r[i] for r in rows) / len(rows), 1) for i in range(8)]
    try:
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA busy_timeout=1000')
        conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                     ('sl_zero8', json.dumps(zero8)))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    return jsonify({'success': True, 'zero8': zero8})


# ── Daemon control ────────────────────────────────────────────────────────────

def new_study_id(name):
    """Study id from a user-entered name. ASCII-only by construction — the
    id doubles as the db filename and must always satisfy safe_study_id()."""
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', name).strip('_-') if name else ''
    return f'{safe or "study"}_{int(time.time())}'


@app.route('/start_daemon', methods=['POST'])
def start_daemon():
    if get_daemon_status()['running']:
        return redirect(url_for('index', error='Daemon is already running'))

    cfg      = load_config()
    port     = request.form.get('port', cfg['serial_port']).strip()
    baud     = request.form.get('baud', str(cfg['baud'])).strip()
    min_dist = request.form.get('min_dist', '20').strip()
    name     = request.form.get('study_name', '').strip()
    no_gate  = request.form.get('no_gate') == 'on'

    # Validate HERE: a value the daemon's argparse rejects would kill it
    # before the db exists, and the operator would get no feedback at all.
    if not port:
        return redirect(url_for('index', error='Serial port is required'))
    try:
        # NOT isdigit(): it accepts Unicode digits like '²' that int() rejects
        if int(baud) <= 0:
            raise ValueError
    except ValueError:
        return redirect(url_for('index', error=f'Bad baud rate: {baud!r}'))
    try:
        float(min_dist)
    except ValueError:
        return redirect(url_for('index', error=f'Bad min distance: {min_dist!r}'))

    sid = new_study_id(name)
    db_path = str(studies_dir() / f'{sid}.db')
    log_path = str(studies_dir() / f'{sid}.log')

    # --opt=value form so a study name (or port) starting with '-' can't be
    # eaten by argparse as an option.
    cmd = [sys.executable, str(DAEMON_SCRIPT),
           '--port=' + port, '--baud=' + baud,
           '--db=' + db_path, '--study-name=' + (name or sid),
           '--min-dist=' + min_dist]
    if no_gate:
        cmd.append('--no-gate')
    save_config(serial_port=port, baud=int(baud))
    print(f'[web] launching daemon: {" ".join(cmd)}', flush=True)
    print(f'[web] daemon log: {log_path}', flush=True)
    try:
        log_fh = open(log_path, 'w')
    except OSError as e:
        return redirect(url_for('index', error=f'Cannot write study log: {e}'))
    subprocess.Popen(cmd, stdout=log_fh,
                     stderr=subprocess.STDOUT, close_fds=True)

    # Wait briefly for the PID file rather than a blind sleep.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if get_daemon_status()['running']:
            print(f'[web] daemon up (pid={get_daemon_status()["pid"]})', flush=True)
            break
        time.sleep(0.1)
    else:
        return redirect(url_for('index', error=(
            f'Daemon did not start — see {os.path.basename(log_path)} '
            f'in the studies folder')))

    return redirect(url_for('view_study', study_id=sid))


@app.route('/stop_daemon', methods=['POST'])
def stop_daemon():
    status = get_daemon_status()
    if status['running']:
        # A stale PID file (daemon killed -9 outside our process tree) can
        # name a recycled pid owned by an unrelated process — never signal
        # anything that isn't actually serial_daemon.
        try:
            cmdline = subprocess.check_output(
                ['ps', '-p', str(status['pid']), '-o', 'command='],
                stderr=subprocess.DEVNULL).decode()
        except subprocess.CalledProcessError:
            cmdline = ''                 # ps ran: pid is gone or recycled
        except OSError:
            # ps itself could not run (fork failure on a loaded Pi). The PID
            # file says this is our daemon — fail SAFE and signal it rather
            # than unlinking the file and orphaning a live recording.
            cmdline = 'serial_daemon'
        if 'serial_daemon' in cmdline:
            try:
                os.kill(status['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            try:
                os.unlink(PID_FILE)      # stale — clear it so the UI recovers
            except OSError:
                pass
    return redirect(url_for('index'))


@app.route('/cmd', methods=['POST'])
def send_cmd():
    """Forward one tuning key to the firmware via the daemon's FIFO."""
    data = request.get_json(force=True)
    # Encode FIRST: slicing the str keeps one codepoint, which can be up to
    # 4 UTF-8 bytes — the firmware must see exactly one key byte.
    cmd = str(data.get('cmd', '')).encode('ascii', errors='ignore')[:1]
    if not cmd:
        return jsonify({'error': 'No command'}), 400
    try:
        fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, cmd)
        os.close(fd)
    except OSError:
        return jsonify({'error': 'Daemon not running'}), 503
    # Current values land in meta asynchronously; SSE refreshes the panel.
    status = get_daemon_status()
    timing = {}
    if status['db_path']:
        meta = read_meta(status['db_path'])
        timing = {k: meta.get(k + '_current') or meta.get(k)
                  for k in TIMING_KEYS}
    return jsonify({'ok': True, 'timing': timing})


@app.route('/nav_config', methods=['POST'])
def nav_config():
    data = request.get_json(force=True)
    host = str(data.get('host', '')).strip()
    try:
        port = int(data.get('port', 0))
    except (TypeError, ValueError):
        port = 0
    if not host or not (0 < port < 65536):
        return jsonify({'error': 'need host and port 1-65535'}), 400
    nav().configure(host, port)      # runtime switch first — persistence is
    persisted = save_config(nav_host=host, nav_port=port)   # best-effort
    return jsonify({'ok': True, 'persisted': persisted,
                    'nav': nav().snapshot()})


@app.route('/geojson/<path:filename>')
def serve_geojson(filename):
    p = Path(__file__).parent / filename
    if p.exists() and p.suffix == '.geojson' and p.parent == Path(__file__).parent:
        return send_file(str(p), mimetype='application/json')
    return '', 404


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Metal Mapper web front-end')
    ap.add_argument('--web-port', type=int, default=5000)
    ap.add_argument('--studies-dir', help='override studies directory')
    ap.add_argument('--serial-port', help='default serial port for new studies')
    ap.add_argument('--baud', type=int, help='default baud for new studies')
    ap.add_argument('--nav-host', help='NMEA-over-TCP host for the nav GPS')
    ap.add_argument('--nav-port', type=int, help='NMEA-over-TCP port')
    args = ap.parse_args()

    for k in ('studies_dir', 'serial_port', 'baud', 'nav_host', 'nav_port'):
        v = getattr(args, k)
        if v is not None:
            _cli_overrides[k] = v

    print(f'Metal Mapper starting on http://localhost:{args.web_port}')
    print(f'Studies directory: {studies_dir()}')
    cfg = load_config()
    print(f'Nav GPS source: {cfg["nav_host"]}:{cfg["nav_port"]} (NMEA over TCP)')
    nav()   # start the reader up front so the first page shows real state
    app.run(host='0.0.0.0', port=args.web_port, debug=False, threaded=True)
