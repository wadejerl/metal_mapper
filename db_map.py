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

Configuration lives in ~/metal_mapper/config.json (serial_port,
nav_host, nav_port, studies_dir) and is editable from the index page;
baud is fixed in serial_daemon.py (custom system, one console rate);
CLI flags override for bench runs, e.g. on a Mac with the hardware:

    python3 db_map.py --serial-port /dev/cu.usbmodem1103 \\
                      --nav-host 192.168.1.50 --nav-port 50010

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
import socket
import sqlite3
import subprocess
import sys
import tempfile
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
NAV_SSE_PERIOD_S = 2.0     # nav chip only — connection state changes slowly
NAV_SSE_MAX_TICKS = 150    # ~5 min per response; see stream_nav()

# View Raw (system test / calibration): a reserved study id whose db lives
# in the temp dir, NOT the studies dir — it never appears in the study list
# and holds no points (the daemon runs with --raw). str, not Path: it is
# compared against PID-file contents.
RAW_ID = '__raw__'
RAW_DB = os.path.join(tempfile.gettempdir(), 'metal_mapper_raw.db')

DEFAULT_CONFIG = {
    'serial_port': '/dev/ttyACM0',
    'nav_host':    '127.0.0.1',
    'nav_port':    50010,
    'studies_dir': str(Path.home() / 'metal_mapper' / 'studies'),
    # Added to the GPS-reported heading before coil geometry — corrects a
    # heading aligned to the antenna baseline instead of direction of travel.
    # A property of the vehicle mounting, so it lives here, not per study.
    'heading_offset_deg': 270,
    # rsync destination for the studies hub (the RTK base Pi), e.g.
    # 'rsync://<base-pi>:8730/mm_studies'. Empty = sync disabled;
    # sync_studies.sh (run by jlw_mm_sync.timer) reads it from here.
    'sync_hub': '',
}

# This unit's identity in filenames. New study ids end in '__<tag>' and dig
# targets flagged here go to '<study>.targets__<tag>.json' — ownership that
# sync_studies.sh routes by. The tag can never contain '_' (sanitized to
# '-'), so the trailing '__<tag>' token is unambiguous even in ids whose
# user-chosen name part contains '__'. MM_HOST_TAG is a test hook.
HOST_TAG = (os.environ.get('MM_HOST_TAG')
            or re.sub(r'[^A-Za-z0-9-]', '-',
                      socket.gethostname().split('.')[0]) or 'unit')

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
    # sync_hub: empty means disabled, so '' is a legitimate value here.
    if not isinstance(cfg.get('sync_hub'), str):
        cfg['sync_hub'] = ''
    try:
        cfg['nav_port'] = int(cfg['nav_port'])
        if not (0 < cfg['nav_port'] <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        cfg['nav_port'] = DEFAULT_CONFIG['nav_port']
    try:
        h = float(cfg['heading_offset_deg'])
        if not math.isfinite(h):
            raise ValueError
        cfg['heading_offset_deg'] = int(h) if h.is_integer() else h
    except (TypeError, ValueError):
        cfg['heading_offset_deg'] = DEFAULT_CONFIG['heading_offset_deg']
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

# resolve(): on the Pi this file is a SYMLINK in /opt/metal_mapper pointing
# into the git checkout — .parent of the link is /opt (no .git, no *.service.in
# templates), .parent of the resolved path is the repo.
_REPO_DIR = Path(__file__).resolve().parent

_VERSION_FILE = _REPO_DIR / 'version.txt'

# Absolute path: jlw_metalmap.service sets PATH to the venv bin only, so a
# bare 'git' never resolves under gunicorn (fallback covers exotic hosts).
_GIT_BIN = '/usr/bin/git' if os.path.exists('/usr/bin/git') else 'git'


def _git_hash():
    try:
        h = subprocess.check_output(
            [_GIT_BIN, 'rev-parse', '--short', 'HEAD'],
            cwd=_REPO_DIR, stderr=subprocess.DEVNULL
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


@app.context_processor
def _inject_asset_version():
    """`?v={{ asset_v }}` on static includes. Flask serves /static with no
    Cache-Control header, so browsers apply HEURISTIC freshness (10% of the
    file's age) and can keep serving a stale map_core.js without ever
    revalidating — surviving a pull, a gunicorn restart AND a plain reload
    on the kiosk. A version query flips the URL per deploy instead."""
    return {'asset_v': GIT_HASH}


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
    if study_id == RAW_ID:
        return RAW_DB
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


def load_columns(db_path):
    """Bulk study points as parallel column arrays for /data. The browser
    fills typed arrays straight from these; dict-per-point at 20 Hz study
    sizes is megabytes of JSON key overhead and object churn on both ends.
    adc is 8 arrays (one per channel), values raw counts. Only the fields
    the renderer/popups need — ts/vin/temp stay row-level (/stream).
    gps_ts (TEXT in the db) ships as floats: the sync filter folds on it.
    """
    empty = {'id': [], 'lat': [], 'lon': [], 'heading': [], 'fix': [],
             'adc': [[] for _ in range(8)], 'gps_ts': []}
    try:
        conn = db_ro(db_path)
        rows = conn.execute(
            'SELECT id,lat,lon,heading,fix,'
            'adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7,gps_ts '
            'FROM points ORDER BY id').fetchall()
        conn.close()
    except Exception:
        return empty
    if not rows:
        return empty

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    t = list(zip(*rows))                     # transpose at C speed
    return {'id': t[0], 'lat': t[1], 'lon': t[2], 'heading': t[3],
            'fix': t[4], 'adc': t[5:13],
            'gps_ts': [num(x) for x in t[13]]}


def env_stats(db_path):
    """Whole-study min/avg/max of the 1 Hz vin/temp pairs the daemon stamps
    onto every point row. SQL aggregates skip NULLs (rows written before the
    first pair arrived); a study with no pairs at all gets None per field."""
    try:
        conn = db_ro(db_path)
        try:
            r = conn.execute('SELECT MIN(vin), AVG(vin), MAX(vin),'
                             '       MIN(temp), AVG(temp), MAX(temp) '
                             'FROM points').fetchone()
        finally:
            conn.close()   # a failing SELECT must not strand the handle
    except Exception:
        return {'vin': None, 'temp': None}

    def trio(mn, avg, mx):
        return None if avg is None else {'min': mn, 'avg': avg, 'max': mx}
    return {'vin': trio(*r[0:3]), 'temp': trio(*r[3:6])}


# ── Multi-unit ownership + dig-target sidecars ────────────────────────────────
# Two head units share studies through a hub (the RTK base Pi) via
# sync_studies.sh. The whole scheme rests on ONE invariant: every synced
# file has exactly one writing unit, so rsync never needs to merge.
#   - Study dbs are written only by the unit that recorded them (its tag is
#     the id's trailing '__<tag>'); everyone else treats them read-only —
#     in-db writes made elsewhere (view settings, legacy targets) are local
#     and get overwritten by the next pull.
#   - Dig targets flagged on ANY unit therefore go to a per-host sidecar,
#     '<study>.targets__<tag>.json', not into the db. Each unit writes only
#     its own sidecar; views render the union of all of them.

def study_owner(study_id):
    """Host tag a study id carries, or None for pre-sync (legacy) ids.
    The tag can't contain '_', so a trailing '__<something-with-_>' (e.g.
    a legacy id whose NAME had '__': 'my__probe_1758...') parses as legacy,
    never as a bogus owner."""
    if '__' not in study_id:
        return None
    tail = study_id.rsplit('__', 1)[1]
    return tail if re.fullmatch(r'[A-Za-z0-9-]+', tail) else None


def is_foreign_study(study_id):
    owner = study_owner(study_id)
    return owner is not None and owner != HOST_TAG


# One writer at a time within THIS process (workers=1 gthread): two
# concurrent target POSTs must not interleave their read-modify-write.
_sidecar_lock = threading.Lock()


def _own_sidecar_path(study_id):
    return studies_dir() / f'{study_id}.targets__{HOST_TAG}.json'


def _read_sidecar(path):
    """Tolerant read: a torn or hand-mangled sidecar reads as empty rather
    than 500ing every map view that merges it."""
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_own_sidecar(data):
    """tmp+fsync+rename in the studies dir, so a power cut can't leave a
    torn file and rsync always ships a complete document. Torn sidecars
    matter more than most files: a truncated one reads as empty, and an
    empty sidecar resets the seq that keeps 'host:N' keys unique."""
    path = _own_sidecar_path(data['study'])
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        f.write(json.dumps(data, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # the rename itself is only metadata until the DIRECTORY is synced —
    # ext4 on the Pi commits that every ~5 s, and a power cut inside the
    # window reverts to the previous version (a just-acked flag vanishes,
    # or worse, a seq already pushed to the hub rolls back and re-mints a
    # key a peer's found-mark points at)
    try:
        dfd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass    # some filesystems refuse directory fsync; best effort


def _load_own_sidecar(study_id):
    d = _read_sidecar(_own_sidecar_path(study_id))
    targets = d.get('targets') if isinstance(d.get('targets'), list) else []
    seq = d.get('seq') if isinstance(d.get('seq'), int) else 0
    # Floor seq at the highest id present: '<host>:N' keys are what OTHER
    # units' found-marks point at, so a mangled seq field must never let N
    # be reused — a stale mark would strike a brand-new target. Ids beyond
    # _TARGET_KEY_RE's 9 digits are hand-edit garbage; flooring on one
    # would push every future key past the parseable range. The raw seq
    # scalar gets the same ceiling (a mangled seq mints unparseable keys
    # just as surely as a mangled id); the target-id floor below recovers
    # the true max, and add_target refuses to mint past the ceiling.
    if not 0 <= seq <= 999999999:
        seq = 0
    for t in targets:
        if isinstance(t, dict) and isinstance(t.get('id'), int) \
                and 0 < t['id'] <= 999999999:
            seq = max(seq, t['id'])
    return {'version': 1, 'study': study_id, 'host': HOST_TAG,
            'seq': seq, 'targets': targets,
            'cleared': d.get('cleared') if isinstance(d.get('cleared'), dict)
                       else {}}


def load_targets(db_path, study_id=None):
    """Merged dig targets: the legacy in-db table plus every per-host
    sidecar present for the study. Keys 'src:id' are globally unique
    ('db' = the in-db table); ids never renumber (AUTOINCREMENT in the db,
    a monotonic seq per sidecar), so a dig list survives edits. 'cleared'
    is an annotation any unit may add in its OWN sidecar — a target is
    cleared if anyone marked it found."""
    merged = []
    try:
        conn = db_ro(db_path)
        try:
            rows = conn.execute('SELECT id, lat, lon, created_at '
                                'FROM targets ORDER BY id').fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    for r in rows:
        merged.append({'src': 'db', 'id': r[0], 'lat': r[1], 'lon': r[2],
                       'created_at': r[3]})

    cleared = {}                        # key -> set of hosts that marked it
    if study_id and study_id != RAW_ID:
        for p in sorted(studies_dir().glob(f'{study_id}.targets__*.json')):
            d = _read_sidecar(p)
            host = d.get('host')
            if not isinstance(host, str) \
                    or not re.fullmatch(r'[A-Za-z0-9-]+', host):
                continue
            for t in d.get('targets') or []:
                try:
                    merged.append({'src': host, 'id': int(t['id']),
                                   'lat': float(t['lat']),
                                   'lon': float(t['lon']),
                                   'created_at': t.get('created_at')})
                except (TypeError, KeyError, ValueError):
                    continue        # one bad entry must not hide the rest
            for k in (d.get('cleared') or {}):
                cleared.setdefault(k, set()).add(host)

    foreign_study = is_foreign_study(study_id) if study_id else False
    for t in merged:
        t['key'] = f"{t['src']}:{t['id']}"
        t['own'] = t['src'] == HOST_TAG
        # Legacy pins keep their bare number; sidecar pins carry the
        # flagging unit so 'mm1-3' and 'mm2-3' never collide on a radio.
        t['label'] = str(t['id']) if t['src'] == 'db' \
            else f"{t['src']}-{t['id']}"
        marks = cleared.get(t['key']) or set()
        t['cleared'] = bool(marks)
        # cleared_own: whether OUR sidecar holds a mark — only that one can
        # be un-marked here (popping our sidecar can't undo another unit's).
        t['cleared_own'] = HOST_TAG in marks
        t['cleared_by'] = (HOST_TAG if t['cleared_own'] else min(marks)) \
            if marks else None
        # Deletable = we own the file it lives in: our sidecar, or the
        # study db when this unit recorded it (foreign db writes would
        # just be silently reverted by the next sync pull).
        t['deletable'] = t['own'] or (t['src'] == 'db' and not foreign_study)

    def _created(t):
        try:
            return float(t['created_at'])
        except (TypeError, ValueError):
            return 0.0
    merged.sort(key=_created)
    return merged


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


def load_samples(db_path, since_id=0, cap=4096):
    """Rows of the daemon's live_samples ring newer than since_id, oldest
    first. Empty on any trouble (pre-ring db, table mid-rewrite): the strip
    chart just skips a beat rather than the stream dying."""
    try:
        conn = db_ro(db_path)
        rows = conn.execute(
            'SELECT id,tick,adc0,adc1,adc2,adc3,adc4,adc5,adc6,adc7 '
            'FROM live_samples WHERE id > ? ORDER BY id LIMIT ?',
            (since_id, cap)).fetchall()
        conn.close()
    except Exception:
        return []
    return [{'id': r[0], 'tick': r[1], 'adc': list(r[2:])} for r in rows]


def samples_max_id(db_path):
    """Newest ring id, or None (empty ring / no table / no db)."""
    try:
        conn = db_ro(db_path)
        mx = conn.execute('SELECT MAX(id) FROM live_samples').fetchone()[0]
        conn.close()
        return mx
    except Exception:
        return None


# ── Status derivation — THE single source of truth for every view ────────────

TIMING_KEYS = ('blanking_us', 'rx_window_us', 'tx_pulse_us', 'sample_rate_hz',
               # Raster mode: detector_mode/slot_us and the per-channel tables
               # come from the info line (stamped) or rastercfg echoes
               # (<key>_current); raster_sel is echo-only live UI state, so the
               # meta.get(k) fallback below just returns None until one arrives.
               'detector_mode', 'slot_us', 'pulse_us', 'bl8', 'rx8', 'tx8',
               'raster_sel')


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
        # Header trouble comes in three flavours that used to share one
        # "GPS / unit-identity failure" line. A LIVE daemon re-sends 'I'
        # while info_ok is '0' (--info-retry-s), so only then can we
        # promise it keeps asking; a saved study just is what it is.
        info_ok = meta.get('info_ok')
        again = ' The daemon keeps re-asking.' if (mine and info_ok == '0') else ''
        gps_id, gps_ver = meta.get('gps_id'), meta.get('gps_ver')
        ident_missing = gps_id in (None, '?') or gps_ver in (None, '?')
        if (info_ok == '0' and meta.get('fw_git_hash') is None
                and gps_id is None and gps_ver is None):
            # Nothing at all came back to the connect-time 'I' (the daemon
            # stamps fw from the very first reply; a '?' means one arrived).
            warnings.append('Detector never answered the info request — port '
                            'open but no detector3 firmware talking?' + again)
        else:
            if ident_missing and (info_ok == '0' or gps_id == '?' or gps_ver == '?'):
                # The common, benign one: the GPS module powers up after the
                # MCU and can miss the boot-time version/ID query while GGA
                # flows fine. Worded as a state, not a verdict — it is also
                # what an unplugged module looks like. ('?' stamped verbatim
                # = pre-2026-09-20 daemon.)
                warnings.append('GPS identity not reported — the module had '
                                'not answered when asked (it powers up after '
                                'the MCU, or is off); position data is '
                                'unaffected.' + again)
            if info_ok == '0':
                # Anything ELSE missing = truncated info line or firmware
                # older than this host. info_missing (daemon >= 2026-09-20)
                # says which keys; without it, identity missing is assumed
                # to be the whole story.
                miss = meta.get('info_missing')
                if miss is None:
                    other = [] if ident_missing else ['?']
                else:
                    other = [k for k in miss.split(',')
                             if k and k not in ('gps_id', 'gps_ver')]
                if other:
                    detail = '' if other == ['?'] else f' (missing {", ".join(other)})'
                    warnings.append(f'Study header incomplete{detail} — info '
                                    'line truncated or firmware older than '
                                    'this host')
        if meta.get('gps_cfg') == 'noreply':
            # Firmware >= 2026-09-20 reports whether the GPS module answered
            # its config burst (boot / resent / pending / noreply / silent).
            # Only noreply is trouble: the module talks but answered neither
            # the boot burst nor the re-sends in full, so it may be running
            # whatever it saved on an earlier boot (GSV still on, another
            # rate...). 'silent' is just the GPS off — already visible as no
            # fix. No 'keeps re-asking' suffix: the firmware's re-sends are
            # spent; the remedy is a reset with the module already powered.
            warnings.append('GPS module did not acknowledge its configuration '
                            '— it may be running stale settings (boot burst '
                            'and re-sends unanswered; reset the MCU with the '
                            'GPS already powered)')
        for kind in ('timing', 'geometry'):
            v = meta.get(kind + '_changed')
            if v:
                diffs = v.split(':', 1)[1] if ':' in v else v
                warnings.append(f'{kind} changed mid-study ({diffs}) — '
                                f'data before/after does not line up')

    timing = {k: meta.get(k + '_current') or meta.get(k) for k in TIMING_KEYS}

    # Link health (daemon's LinkStats, JSON) and the last '# pace' report —
    # raw-view telemetry. Absent on pre-2026-09 daemons / saved studies;
    # a torn or hand-edited value degrades to None, never a 500.
    link = None
    if meta.get('link'):
        try:
            link = json.loads(meta['link'])
        except (TypeError, ValueError):
            link = None
        if not isinstance(link, dict):
            link = None
    pace = None
    if meta.get('pace_last'):
        try:
            pace_at = float(meta.get('pace_at'))
            if not math.isfinite(pace_at):
                pace_at = None      # NaN/inf would leave the SSE JSON unparsable
        except (TypeError, ValueError):
            pace_at = None
        # age from the server clock: daemon and Flask share the Pi's clock,
        # the browser's may not agree with it
        pace = {'text':  meta['pace_last'], 'at': pace_at,
                'age_s': round(now - pace_at, 1) if pace_at is not None else None}

    return {
        'state':         state,
        'reason':        (live or {}).get('reason', ''),
        'live':          live,
        'live_age_s':    round(now - live['updated_at'], 1) if live else None,
        'warnings':      warnings,
        'timing':        timing,
        'motion_mm':     meta.get('motion_mm'),
        'motion_paused': meta.get('motion_paused'),
        'link':          link,
        'pace':          pace,
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
# heading_off_deg is added to the reported heading before any of this math to
# absorb that. It comes from config.json (heading_offset_deg — a vehicle
# mounting property); a study whose meta saved its own sl_heading_offset_deg
# before the move to config keeps that value.

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

# Study summaries are re-read only when the files change: COUNT(*) walks
# the whole points table, and on the Pi's SD card doing that for a season
# of studies made every return to the landing page crawl. Keyed per path;
# only the live study's signature moves between renders.
_study_cache = {}


def _study_sig(p):
    """Change signature of a study db. WAL mode: writes land in -wal first
    and only reach the main file on checkpoint, so both must be watched."""
    sig = []
    for f in (str(p), str(p) + '-wal'):
        try:
            st = os.stat(f)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def list_studies():
    studies = []

    for p in sorted(studies_dir().glob('*.db')):
        sig = _study_sig(p)
        cached = _study_cache.get(str(p))
        if cached and cached[0] == sig:
            summary = dict(cached[1])
        else:
            try:
                ctime = p.stat().st_ctime
            except OSError:
                continue                  # deleted between glob and here
            read_ok = True
            try:
                conn = db_ro(str(p))
                meta = dict(
                    conn.execute('SELECT key,value FROM meta').fetchall())
                count = conn.execute(
                    'SELECT COUNT(*) FROM points').fetchone()[0]
                conn.close()
            except Exception:
                if not p.exists():
                    continue              # purged between stat and open
                # corrupt-but-present db: list it so the operator sees it
                meta, count = {}, 0
                read_ok = False
            owner = study_owner(p.stem)
            summary = {
                'id':         p.stem,
                'name':       meta.get('study_name', p.stem),
                'created_at': meta.get('created_at', ctime),
                'count':      count,
                'fw':         meta.get('fw_git_hash', ''),
                # badge for studies another unit recorded (synced in)
                'foreign_owner': owner if owner and owner != HOST_TAG
                                 else None,
            }
            if read_ok:
                # Never cache the failure fallback: a quiescent file's
                # signature would pin one transient I/O hiccup (fd
                # exhaustion, SD-card blip) as '0 points' until restart.
                # Failed reads retry on every render instead.
                _study_cache[str(p)] = (sig, dict(summary))
        # liveness is daemon state, not file state — never cached
        summary['live'] = is_live(str(p))
        studies.append(summary)

    def _created(s):
        # created_at is the daemon's meta stamp (string) or the ctime float
        # fallback for headerless dbs — coerce, and let anything unparseable
        # sort to the bottom rather than 500 the landing page.
        try:
            return float(s['created_at'])
        except (TypeError, ValueError):
            return 0.0

    # Newest STUDY first — creation order, deliberately not file mtime:
    # reopening or re-viewing an old study must not bubble it to the top.
    studies.sort(key=_created, reverse=True)
    return studies


# Module import ≈ web process boot. Logs written after this instant belong
# to THIS boot's failed starts — the error page may be pointing at them.
_BOOT_TS = time.time()


def _older_than_boot(path):
    try:
        return os.path.getmtime(path) < _BOOT_TS
    except OSError:
        return False


def _points_count(path):
    """COUNT of recorded points, or None if the db can't be read (an
    unreadable db is not provably empty)."""
    try:
        conn = db_ro(str(path))
        try:
            return conn.execute('SELECT COUNT(*) FROM points').fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


def purge_empty_studies():
    """Delete studies with ZERO recorded points — every aborted or failed
    start leaves one behind and they clutter the list. Runs once per process
    (first page load after boot). Never touches the db a running daemon
    holds, and skips anything unreadable: a corrupt db is not provably
    empty, so it stays. Removes the study's .db (+-wal/-shm), plus .log
    files from BEFORE this boot (a fresh log is the only diagnostic of a
    start that just failed — 'Daemon did not start, see the log' must not
    point at a file this same page load deleted)."""
    removed = []

    def _purgeable(p):
        """Only studies THIS unit is authoritative for. A foreign study
        pulled mid-recording legitimately has 0 points (daemon up, gate not
        passed yet) — purging it here would just re-pull it next sync, a
        delete/resurrect churn loop. Legacy (pre-sync) ids are purgeable
        only while sync is off: once a hub exists, the pull would resurrect
        those too."""
        owner = study_owner(p.stem)
        if owner is not None:
            return owner == HOST_TAG
        return not load_config()['sync_hub']

    # Pass 1, lock-free: the COUNT scan walks every study db and can take
    # a while over a season of studies on an SD card — a Start tap must
    # not queue behind it.
    candidates = [p for p in sorted(studies_dir().glob('*.db'))
                  if _purgeable(p) and _points_count(p) == 0]
    # Pass 2, under the spawn lock: /start_daemon and /start_raw hold it
    # from before the daemon spawns until its PID file exists, so no daemon
    # can be mid-birth while this runs. Re-verify each candidate right
    # before deleting — the daemon writes its PID file BEFORE creating the
    # db, so a fresh look here always sees the owner of a brand-new 0-point
    # db, and an empty db is cheap to re-count.
    with _spawn_lock:
        for p in candidates:
            ds = get_daemon_status()
            if ds.get('running') and \
                    os.path.abspath(ds['db_path']) == os.path.abspath(str(p)):
                continue
            if _points_count(p) != 0:
                continue                  # gained rows (or vanished) since
            victims = [str(p), str(p) + '-wal', str(p) + '-shm']
            # its target sidecars too — an orphan sidecar would linger in
            # the dir forever with no study to render it
            victims += [str(sc) for sc in
                        studies_dir().glob(f'{p.stem}.targets__*.json')]
            log = str(p.with_suffix('.log'))
            if _older_than_boot(log):
                victims.append(log)
            for victim in victims:
                try:
                    os.unlink(victim)
                except OSError:
                    pass
            removed.append(p.stem)
    # Orphan logs (a start that died before creating its db) — same
    # this-boot protection. No lock needed: any start, even one reusing an
    # old study name, rewrites its log at spawn, making it this-boot.
    for lg in sorted(studies_dir().glob('*.log')):
        if not lg.with_suffix('.db').exists() and _older_than_boot(lg):
            try:
                os.unlink(str(lg))
            except OSError:
                pass
    if removed:
        print(f'[web] purged {len(removed)} empty studies: '
              f'{", ".join(removed)}', flush=True)
    return removed


# Once per process: gunicorn has no reliable post-fork hook here, so the
# first index render after boot is "system start" from the operator's seat.
_purged = False


# ── Pi service health ─────────────────────────────────────────────────────────

# Absolute path: jlw_metalmap.service sets PATH to the venv bin only.
SYSTEMCTL_BIN = '/usr/bin/systemctl'

SVC_TTL_S = 10.0   # one fork per ~10 s no matter how many clients
# t None = cold, never fetched. NOT 0.0: monotonic()'s epoch is boot time
# on Linux, so a gunicorn started <TTL after Pi power-on would read a 0.0
# cache as "fresh" and skip the first fetch.
_svc_cache = {'t': None, 'val': None, 'refreshing': False}
_ip_cache  = {'t': None, 'val': None, 'refreshing': False}
_svc_lock = threading.Lock()


def _ttl_cached(cache, fetch):
    """TTL cache + single-flight around a subprocess-backed fetch.

    The lock guards ONLY the cache dict — never the fork. systemctl is a
    D-Bus round trip to PID 1; on a sick Pi it can take the full timeout
    (or, in D-state, outlive even SIGKILL — subprocess.run's timeout
    bounds communicate(), then waits UNBOUNDED for the killed child).
    Holding the lock through that would park every nav SSE tick and every
    landing-page render on one refresh, eating the 8-gthread pool that
    /shutdown and /stop_daemon also live in. Instead one caller refreshes
    (single-flight) while the rest return the last snapshot immediately;
    worst case a wedged fork costs exactly one thread and the display
    freezes at its last state — the rest of the UI keeps answering.

    fetch() returning None means "couldn't determine": the previous
    snapshot is kept, so callers never flicker to empty — but the attempt
    is still TTL-stamped, so a persistently failing fetch (ip wedged to
    its 5 s timeout on a sick Pi) retries once per TTL, not once per 2 s
    SSE tick with a thread pinned in subprocess.run the whole while.
    """
    with _svc_lock:
        if cache['refreshing'] or (
                cache['t'] is not None
                and time.monotonic() - cache['t'] < SVC_TTL_S):
            # Fresh(-ly attempted), or someone else is refreshing: serve
            # the last snapshot. None only near cold start — SSE clients
            # get a real value on a later tick, and the index Jinja gate
            # hides the affected markup for that one render.
            return cache['val']
        cache['refreshing'] = True
    val = None
    try:
        val = fetch()
        return val if val is not None else cache['val']
    finally:
        with _svc_lock:
            cache['t'] = time.monotonic()   # stamp failures too
            if val is not None:
                cache['val'] = val
            cache['refreshing'] = False


def service_states():
    """State of every unit templated in the repo root (*.service.in, rendered
    by install_pi_system.sh under the same name minus .in) as {unit: state},
    or None off the Pi (no systemctl) so callers hide the panel entirely.

    `systemctl is-active a b c` prints one state per line in argument
    order (active/inactive/failed/activating/unknown) and exits nonzero
    when any unit is not active — the exit code is ignored on purpose.
    Cached: the landing page's nav SSE would otherwise fork a subprocess
    every 2 s per open client.
    """
    # _REPO_DIR, not .parent: on the Pi the units live in the checkout, not
    # in /opt/metal_mapper where this file is symlinked from.
    units = sorted(p.name[:-3] for p in _REPO_DIR.glob('*.service.in'))
    if not units or not os.path.exists(SYSTEMCTL_BIN):
        return None

    def fetch():
        try:
            r = subprocess.run([SYSTEMCTL_BIN, 'is-active', *units],
                               capture_output=True, text=True, timeout=5)
            lines = r.stdout.splitlines()
        except Exception:
            lines = []
        return {u: (lines[i].strip() if i < len(lines) and lines[i].strip()
                    else 'unknown')
                for i, u in enumerate(units)}

    return _ttl_cached(_svc_cache, fetch)


def local_ipv4s():
    """The box's non-loopback IPv4s as ['wlan0 192.168.4.1', ...] — the
    quickest "is the network side up" check on the landing page. [] means
    positively no addresses (worth showing); None means undeterminable
    (no `ip` binary — bench Mac), so callers hide the line entirely.

    `ip -j -4 addr` is pure netlink (no D-Bus), JSON out; iproute2 path
    differs across images, so candidates are probed — absolute paths, as
    the service PATH holds only the venv bin.
    """
    def fetch():
        for ip_bin in ('/usr/sbin/ip', '/sbin/ip', '/usr/bin/ip', '/bin/ip'):
            if os.path.exists(ip_bin):
                break
        else:
            return None
        try:
            r = subprocess.run([ip_bin, '-j', '-4', 'addr'],
                               capture_output=True, text=True, timeout=5)
            # busybox ip / pre-4.13 iproute2 reject -j: usage on stderr,
            # empty stdout, rc!=0 — that's "couldn't determine", NOT
            # "no addresses". iproute2 prints [] on genuine none.
            if r.returncode != 0:
                return None
            ifaces = json.loads(r.stdout)
        except Exception:
            return None
        return [f"{itf.get('ifname', '?')} {ai['local']}"
                for itf in ifaces
                for ai in itf.get('addr_info', [])
                if ai.get('family') == 'inet'
                and not ai.get('local', '').startswith('127.')]

    return _ttl_cached(_ip_cache, fetch)


# ── Views ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    global _purged
    if not _purged:
        _purged = True
        purge_empty_studies()
    cfg = load_config()
    default_name = datetime.datetime.now().strftime('geo_%Y%m%d_%H%M%S')
    ds = get_daemon_status()
    # A running daemon means the operator belongs back in that session
    # (client reset, browser restart): hand the template the map to return
    # to — or None if it's a raw (nothing-saved) session.
    live_id = None
    if ds['running'] and \
            os.path.abspath(ds['db_path']) != os.path.abspath(RAW_DB):
        live_id = Path(ds['db_path']).stem
    return render_template(
        'index.html',
        studies=list_studies(),
        daemon_status=ds,
        live_id=live_id,
        nav=nav().snapshot(),
        services=service_states(),
        ipv4s=local_ipv4s(),
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
    points = load_columns(db_path)
    meta = read_meta(db_path)
    # Rotation follows config.json unless this study saved its own value
    # (pre-move studies); the client reads it out of meta either way.
    meta.setdefault('sl_heading_offset_deg',
                    str(load_config()['heading_offset_deg']))
    return jsonify({
        'points':    points,
        'last_id':   points['id'][-1] if points['id'] else 0,
        'is_live':   is_live(db_path),
        'meta':      meta,
        'status':    derive_status(db_path, meta),
        'env_stats': env_stats(db_path),
        'nav':       nav().snapshot(),
        'targets':   load_targets(db_path, study_id),
    })


@app.route('/env_stats/<study_id>')
def get_env_stats(study_id):
    """Light re-aggregation for the map view: when a daemon detaches from an
    open study page (live → saved flip over SSE), the run has grown since the
    page-load /data snapshot — the client refreshes just the stats here."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    return jsonify(env_stats(db_path))


@app.route('/navfix')
def navfix():
    return jsonify(nav().snapshot())


# Reserved (double-underscore, like RAW_ID) so it can never shadow a real
# study in /stream/<study_id> — and it must live under /stream/ because the
# Pi's nginx scopes SSE proxying (buffering off, 24h read timeout) to that
# path. Feeds the landing page's nav chip: the kiosk loads that page at
# boot, often before gnssserver is up, and a one-shot server-side render
# would show "down" forever even after the reader heals.
NAV_STREAM_ID = '__nav__'


@app.route(f'/stream/{NAV_STREAM_ID}')
def stream_nav():
    # Bounded on purpose: a phone that walks out of WiFi range never FINs,
    # so an endless generator would hold one of gunicorn's few worker
    # threads until the kernel gives up on the socket (~15 min) — a couple
    # of those and the pool starves. Ending the response after ~5 min
    # self-expires zombies; live clients auto-reconnect (stateless ticks,
    # nothing to resume). Carries daemon_running so the landing page's
    # shutdown-confirm warning tracks sessions started from other clients.
    def gen():
        for _ in range(NAV_SSE_MAX_TICKS):
            payload = {'nav': nav().snapshot(),
                       'daemon_running': get_daemon_status()['running'],
                       'services': service_states(),
                       'ipv4s': local_ipv4s()}
            yield f'data: {json.dumps(payload)}\n\n'
            time.sleep(NAV_SSE_PERIOD_S)
    return Response(
        stream_with_context(gen()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


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
        # Sample cursor is per-connection, not resumed via Last-Event-ID:
        # the ring is an ephemeral scope trace, so a reconnect just refills
        # the chart from whatever the ring currently holds.
        s_cur = 0
        while True:
            rows = load_points(db_path, since_id)
            if rows:
                since_id = rows[-1]['id']
            status = derive_status(db_path)
            # Raw full-rate samples only while a daemon is attached — a
            # saved study's leftover ring (daemon killed -9) is stale data.
            samples = (load_samples(db_path, s_cur)
                       if status['state'] != 'stopped' else [])
            if samples:
                s_cur = samples[-1]['id']
            elif s_cur:
                # Ring id regression means /start_raw recreated the db (the
                # unlink resets the AUTOINCREMENT sequence): without a reset,
                # a connection held across stop→start waits for ids that may
                # take the whole new session to reappear — a live banner over
                # a frozen chart. New-session ids are all unseen, so
                # re-seeding from 0 cannot re-send anything.
                mx = samples_max_id(db_path)
                if mx is not None and mx < s_cur:
                    s_cur = 0
            payload = {
                'type':    'tick',
                'rows':    rows,
                'samples': samples,
                'status':  status,
                'nav':     nav().snapshot(),
                # Sync can land another unit's flags (or a found-mark) at
                # any moment — ship the merged list every tick; the client
                # only re-renders when it actually changed.
                'targets': load_targets(db_path, study_id),
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

    def arr8(key):
        """zero8/bias8 must be exactly 8 finite in-scale numbers — junk here
        round-trips into every future /view load, so reject at the door."""
        a = data.get(key)
        if a is None:
            return None
        if (not isinstance(a, list) or len(a) != 8
                or not all(isinstance(v, (int, float))
                           and not isinstance(v, bool)
                           # also rejects NaN/±inf (compares False) and is
                           # int-exact: isfinite() would OverflowError on a
                           # JSON int too big for float — a 500, not our 400
                           and abs(v) <= 65535
                           for v in a)):
            raise ValueError('%s must be 8 finite numbers' % key)
        return json.dumps(a)

    try:
        zero8_json = arr8('zero8')
        bias8_json = arr8('bias8')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    mapping = {
        'sl_plus_range':  data.get('plus_range'),
        'sl_minus_range': data.get('minus_range'),
        'sl_offset':      data.get('offset'),
        'sl_filter':      data.get('filter'),
        'sl_sync_period': data.get('sync_period'),
        'sl_sync_res':    data.get('sync_res'),
        'sl_cm_group':    data.get('cm_group'),
        'sl_combine':     data.get('combine'),
        'sl_zero8':       zero8_json,
        'sl_bias8':       bias8_json,
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


@app.route('/targets/<study_id>')
def get_targets(study_id):
    """Recall a study's marked dig targets (also shipped inside /data)."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'targets': load_targets(db_path, study_id)})


_TARGET_KEY_RE = re.compile(r'(db|[A-Za-z0-9-]{1,64}):(\d{1,9})')


def _parse_target_key(data):
    """'src:id' from a request body; legacy bodies ({'id': N}) mean the
    in-db table. Returns (src, id) or None."""
    if not isinstance(data, dict):    # 'null'/'[1]'/'"x"' are valid JSON
        return None
    key = data.get('key')
    if key is None and 'id' in data:
        key = f"db:{data['id']}"
    m = _TARGET_KEY_RE.fullmatch(str(key)) if key is not None else None
    return (m.group(1), int(m.group(2))) if m else None


@app.route('/targets/<study_id>', methods=['POST'])
def add_target(study_id):
    """Mark a dig target at an exact lat/lon; returns its key. Targets go
    to THIS unit's sidecar file, never into the study db — the db belongs
    to whoever recorded it, and sidecars are what lets two head units flag
    targets on the same study without ever writing the same file (see the
    ownership block above load_targets)."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(force=True)
    try:
        lat, lon = float(data['lat']), float(data['lon'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'lat/lon required'}), 400
    if not (math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({'error': 'lat/lon out of range'}), 400
    if study_id == RAW_ID:
        # Raw sessions are ephemeral bench affairs with no map view — a
        # sidecar here would only leak '__raw__' files into the sync.
        return jsonify({'error': 'raw sessions have no targets'}), 400
    try:
        with _sidecar_lock:
            sc = _load_own_sidecar(study_id)
            if sc['seq'] >= 999999999:
                # the key regex tops out at 9 digits; minting past it would
                # create a pin no unit can ever delete or mark found
                return jsonify({'error': 'target ids exhausted'
                                         ' for this study'}), 400
            sc['seq'] += 1
            sc['targets'].append({'id': sc['seq'], 'lat': lat, 'lon': lon,
                                  'created_at': time.time()})
            _write_own_sidecar(sc)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'key': f'{HOST_TAG}:{sc["seq"]}',
                    'targets': load_targets(db_path, study_id)})


@app.route('/targets/<study_id>/delete', methods=['POST'])
def delete_target(study_id):
    """Delete a target THIS unit is allowed to delete: one from our own
    sidecar, or a legacy in-db one when we recorded the study. Anything
    else lives in a file another unit owns — deleting it here would either
    do nothing (their sidecar) or be reverted by the next sync pull (their
    db), so refuse and point at mark-found instead."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    parsed = _parse_target_key(request.get_json(force=True))
    if not parsed:
        return jsonify({'error': 'key required'}), 400
    src, tid = parsed
    if src == HOST_TAG and study_id != RAW_ID:
        try:
            with _sidecar_lock:
                sc = _load_own_sidecar(study_id)
                sc['targets'] = [t for t in sc['targets']
                                 if t.get('id') != tid]
                sc['cleared'].pop(f'{src}:{tid}', None)
                _write_own_sidecar(sc)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    elif src == 'db':
        if is_foreign_study(study_id):
            return jsonify({'error': f'recorded on {study_owner(study_id)} '
                                     '— mark it found instead'}), 403
        try:
            conn = sqlite3.connect(db_path)
            conn.execute('PRAGMA busy_timeout=1000')
            try:
                conn.execute('DELETE FROM targets WHERE id=?', (tid,))
                conn.commit()
            except sqlite3.OperationalError as e:
                # no targets table yet — nothing to delete, like GET.
                # Anything else (locked, readonly, I/O) must NOT
                # masquerade as success.
                if 'no such table' not in str(e).lower():
                    raise
            conn.close()
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        return jsonify({'error': f'flagged on {src} '
                                 '— mark it found instead'}), 403
    return jsonify({'ok': True, 'targets': load_targets(db_path, study_id)})


@app.route('/targets/<study_id>/clear', methods=['POST'])
def clear_target(study_id):
    """Mark any unit's target found (or un-mark it). The annotation goes in
    OUR sidecar keyed by the target's 'src:id' — the dig crew on either
    unit can retire a pin without touching the file that defines it."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    if study_id == RAW_ID:
        return jsonify({'error': 'raw sessions have no targets'}), 400
    data = request.get_json(force=True)
    parsed = _parse_target_key(data)
    if not parsed:
        return jsonify({'error': 'key required'}), 400
    key = f'{parsed[0]}:{parsed[1]}'
    want = bool(data.get('cleared', True))
    try:
        with _sidecar_lock:      # checks atomic with the write, so a
            # racing delete can't strand an orphan mark past validation
            target = next((t for t in load_targets(db_path, study_id)
                           if t['key'] == key), None)
            if target is None:
                # never annotate a key no target carries: each bogus entry
                # would live in the sidecar forever and ride the sync
                return jsonify({'error': 'no such target'}), 404
            if not want and target['cleared'] and not target['cleared_own']:
                # the mark lives in another unit's sidecar; popping ours
                # would change nothing (views OR every sidecar's marks)
                return jsonify({'error': "marked found on "
                                         f"{target['cleared_by']} "
                                         "— un-mark it there"}), 403
            sc = _load_own_sidecar(study_id)
            if want:
                sc['cleared'][key] = time.time()
            else:
                sc['cleared'].pop(key, None)
            _write_own_sidecar(sc)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'targets': load_targets(db_path, study_id)})


@app.route('/rename/<study_id>', methods=['POST'])
def rename_study(study_id):
    """Update the study's human label (meta study_name). The .db filename —
    and with it the study id and URL — never changes: it is the daemon's
    open path while a study is live, and the stable id everywhere else."""
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(force=True)
    name = str(data.get('name') or '').strip()
    if not name or len(name) > 120:
        return jsonify({'error': 'name must be 1-120 characters'}), 400
    try:
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA busy_timeout=1000')
        conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                     ('study_name', name))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'name': name})


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
    id doubles as the db filename and must always satisfy safe_study_id().
    The trailing '__<host>' marks which unit recorded it: sync routing
    (push mine, pull theirs) keys on it, and it keeps two units that start
    a study the same second from colliding at the hub."""
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', name).strip('_-') if name else ''
    return f'{safe or "study"}_{int(time.time())}__{HOST_TAG}'


# One spawn request at a time: the not-running check, the raw-db unlink and
# the Popen must not interleave with a second submit — the start forms have
# no double-click guard, and the first request can sit up to 3 s in its PID
# poll. Without this, /start_raw's unlink can delete the db a just-claimed
# daemon already opened, leaving it streaming into an unlinked inode the web
# process can never read. Held lock ≤ ~3 s; the loser then sees 'running'
# and bounces cleanly. (workers=1 gthread — one process, so a Lock covers.)
_spawn_lock = threading.Lock()


def _serialized_spawn(fn):
    def wrapper(*a, **kw):
        with _spawn_lock:
            return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


@app.route('/start_daemon', methods=['POST'])
@_serialized_spawn
def start_daemon():
    if get_daemon_status()['running']:
        return redirect(url_for('index', error='Daemon is already running'))

    cfg      = load_config()
    port     = request.form.get('port', cfg['serial_port']).strip()
    min_dist = request.form.get('min_dist', '20').strip()
    name     = request.form.get('study_name', '').strip()
    no_gate  = request.form.get('no_gate') == 'on'

    # Validate HERE: a value the daemon's argparse rejects would kill it
    # before the db exists, and the operator would get no feedback at all.
    if not port:
        return redirect(url_for('index', error='Serial port is required'))
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
           '--port=' + port,
           '--db=' + db_path, '--study-name=' + (name or sid),
           '--min-dist=' + min_dist]
    if no_gate:
        cmd.append('--no-gate')
    save_config(serial_port=port)
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


@app.route('/raw')
def raw_view():
    """View Raw: full-rate channel display for system test & calibration.
    Attaches to whatever daemon is running — a --raw session (nothing
    saved) or a live study (viewer only) — and offers to start a raw
    session when none is."""
    cfg = load_config()
    ds  = get_daemon_status()
    mode, sid = 'stopped', None
    if ds['running']:
        if ds['db_path'] == os.path.abspath(RAW_DB):
            mode, sid = 'raw', RAW_ID
        else:
            sid  = os.path.splitext(os.path.basename(ds['db_path']))[0]
            mode = 'study'
    return render_template('raw.html', mode=mode, sid=sid, cfg=cfg,
                           error=request.args.get('error'),
                           git_hash=GIT_HASH)


@app.route('/start_raw', methods=['POST'])
@_serialized_spawn
def start_raw():
    """Launch the daemon in --raw mode against the throwaway temp-dir db."""
    if get_daemon_status()['running']:
        return redirect(url_for('raw_view', error='Daemon is already running'))

    # No form fields: raw mode always attaches to the configured port at
    # the daemon's built-in baud — set-and-forget on a custom system.
    port = load_config()['serial_port']

    # Fresh scratch db every session: db_open would happily reuse last
    # week's meta (fw hash, timing header) and lie in the telemetry strip.
    for ext in ('', '-wal', '-shm'):
        try:
            os.unlink(RAW_DB + ext)
        except OSError:
            pass

    log_path = RAW_DB[:-3] + '.log'
    cmd = [sys.executable, str(DAEMON_SCRIPT),
           '--port=' + port,
           '--db=' + RAW_DB, '--study-name=raw', '--raw']
    print(f'[web] launching raw daemon: {" ".join(cmd)}', flush=True)
    print(f'[web] raw daemon log: {log_path}', flush=True)
    try:
        log_fh = open(log_path, 'w')
    except OSError as e:
        return redirect(url_for('raw_view', error=f'Cannot write log: {e}'))
    subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                     close_fds=True)

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if get_daemon_status()['running']:
            print(f'[web] raw daemon up (pid={get_daemon_status()["pid"]})',
                  flush=True)
            break
        time.sleep(0.1)
    else:
        return redirect(url_for('raw_view', error=(
            f'Daemon did not start — see {os.path.basename(log_path)} '
            f'in the temp dir')))
    return redirect(url_for('raw_view'))


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
                # Wait (bounded) for it to actually die: the redirect lands
                # on the studies page, which must not flash a running-daemon
                # state — with a Return-to-Live button — right after the
                # operator pressed Stop. Typical exit is well under 500 ms.
                for _ in range(60):                        # ≤ 3 s
                    if not get_daemon_status()['running']:
                        break
                    time.sleep(0.05)
        else:
            try:
                os.unlink(PID_FILE)      # stale — clear it so the UI recovers
            except OSError:
                pass
    # Stop and exit are one action everywhere — every stop lands on the
    # studies page. (The old from=raw stay-on-page redirect died with the
    # separate Stop button.)
    return redirect(url_for('index'))


# Absolute paths: jlw_metalmap.service sets PATH to the venv bin only, so
# neither sudo nor shutdown would resolve through the environment.
SUDO_BIN     = '/usr/bin/sudo'
SHUTDOWN_BIN = '/usr/sbin/shutdown'


@app.route('/shutdown', methods=['POST'])
def shutdown_host():
    """Power the mapper computer off. The confirm step is the landing
    page's overlay — this endpoint is the point of no return. gunicorn
    runs as the Pi user, who has passwordless sudo; `-n` makes any
    refusal fail fast and land back on the landing page as an error
    instead of hanging on a password prompt. systemd then stops
    jlw_metalmap.service, which SIGTERMs the whole cgroup — a recording
    daemon flushes and closes its db cleanly on the way down."""
    try:
        r = subprocess.run([SUDO_BIN, '-n', SHUTDOWN_BIN, '-h', 'now'],
                           capture_output=True, text=True, timeout=10)
    except Exception as e:                    # missing binary, timeout
        return redirect(url_for('index', error=f'shutdown failed: {e}'))
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or 'sudo refused').strip()
        msg = msg.splitlines()[-1] if msg else 'sudo refused'
        return redirect(url_for('index', error=f'shutdown failed: {msg}'))
    # Post/Redirect/Get: the tab that pressed the button must end up on a
    # plain GET, or reloading it — or a browser restoring tabs after the
    # box is next powered on — would re-fire the POST and shut it down
    # again. The redirect roundtrip is localhost-milliseconds; systemd
    # unit teardown is far slower, so the GET lands before we die.
    return redirect(url_for('shutting_down'))


@app.route('/shutting_down')
def shutting_down():
    """Terminal page after a successful /shutdown (see PRG note above)."""
    return render_template('shutdown.html', git_hash=GIT_HASH)


@app.route('/cmd', methods=['POST'])
def send_cmd():
    """Forward one tuning key to the firmware via the daemon's FIFO."""
    data = request.get_json(force=True)
    raw = str(data.get('cmd', ''))
    if raw in ('SAVE', 'CLEAR'):
        # Flash writes are word-gated in the firmware (word + Enter) so a
        # stray noise byte on the console line can never commit to flash.
        # SAVE persists settings; CLEAR restores defaults and persists.
        cmd = raw.encode('ascii') + b'\n'
    else:
        # Encode FIRST: slicing the str keeps one codepoint, which can be up
        # to 4 UTF-8 bytes — the firmware must see exactly one key byte.
        cmd = raw.encode('ascii', errors='ignore')[:1]
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
    ap.add_argument('--nav-host', help='NMEA-over-TCP host for the nav GPS')
    ap.add_argument('--nav-port', type=int, help='NMEA-over-TCP port')
    args = ap.parse_args()

    for k in ('studies_dir', 'serial_port', 'nav_host', 'nav_port'):
        v = getattr(args, k)
        if v is not None:
            _cli_overrides[k] = v

    print(f'Metal Mapper starting on http://localhost:{args.web_port}')
    print(f'Studies directory: {studies_dir()}')
    cfg = load_config()
    print(f'Nav GPS source: {cfg["nav_host"]}:{cfg["nav_port"]} (NMEA over TCP)')
    nav()   # start the reader up front so the first page shows real state
    app.run(host='0.0.0.0', port=args.web_port, debug=False, threaded=True)
