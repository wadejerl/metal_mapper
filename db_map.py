#!/usr/bin/env python3
"""
db_map.py — Metal Mapper web front-end with SQLite3 study storage.

Lists saved studies from ~/metal_mapper/studies/ and displays them on an
interactive Leaflet map.  When a live recording daemon is running the map
updates in real time and the detector timing controls (blanking / rx window)
are shown.

Usage:
    python db_map.py [--web-port 5000]
"""

import argparse
import datetime
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import threading
import urllib.request
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect,
                   render_template_string, request, send_file,
                   stream_with_context, url_for)

STUDIES_DIR   = Path.home() / 'metal_mapper' / 'studies'
CONFIG_FILE   = Path.home() / 'metal_mapper' / 'config.json'
FIFO_PATH     = '/tmp/metal_detector_cmd.fifo'
PID_FILE      = '/tmp/metal_detector_daemon.pid'
DAEMON_SCRIPT = Path(__file__).parent / 'serial_daemon.py'
STATIC_DIR    = Path(__file__).parent / 'static'

_LEAFLET_VER = '1.9.4'
_LEAFLET_BASE = f'https://unpkg.com/leaflet@{_LEAFLET_VER}/dist'

def ensure_leaflet():
    """Download Leaflet JS+CSS into static/ if not already present."""
    STATIC_DIR.mkdir(exist_ok=True)
    for fname in ('leaflet.js', 'leaflet.css'):
        dest = STATIC_DIR / fname
        if not dest.exists():
            url = f'{_LEAFLET_BASE}/{fname}'
            print(f'[db_map] downloading {url}', flush=True)
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                print(f'[db_map] WARNING: could not download {fname}: {e}', flush=True)

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


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(**kwargs):
    cfg = load_config()
    cfg.update(kwargs)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

app = Flask(__name__)

# In-memory calibration state keyed by study_id.
# Resets on server restart — user re-zeros via button.
_calib: dict      = {}
_calib_lock       = threading.Lock()


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


# ── Daemon / liveness helpers ─────────────────────────────────────────────────

def get_daemon_status():
    """Read PID file; return {running, pid, db_path}."""
    try:
        content = Path(PID_FILE).read_text().strip()
        pid_str, db_path = content.split(':', 1)
        pid = int(pid_str)
        os.kill(pid, 0)          # raises OSError if the process is gone
        return {'running': True, 'pid': pid, 'db_path': os.path.abspath(db_path)}
    except Exception:
        return {'running': False, 'pid': None, 'db_path': None}


def is_live(db_path):
    """True iff the daemon is actively writing to this specific db file."""
    s = get_daemon_status()
    return s['running'] and s['db_path'] == os.path.abspath(str(db_path))


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db_path(study_id):
    return str(STUDIES_DIR / f'{study_id}.db')


def db_ro(db_path):
    """Open db read-only; safe to use concurrently with the WAL-mode daemon."""
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def read_meta(db_path):
    try:
        conn = db_ro(db_path)
        m = dict(conn.execute('SELECT key,value FROM meta').fetchall())
        conn.close()
        return m
    except Exception:
        return {}


def load_points(db_path, since_id=0):
    """Return rows as dicts with value normalized to [0, 1] (adc_raw / 65535)."""
    try:
        conn = db_ro(db_path)
        rows = conn.execute(
            'SELECT id,ts,lat,lon,fix_quality,adc_raw,gps_ts '
            'FROM detections WHERE id > ? ORDER BY id',
            (since_id,)
        ).fetchall()
        conn.close()
    except Exception:
        return []
    return [
        {
            'id':          r[0],
            'ts':          r[1],
            'lat':         r[2],
            'lon':         r[3],
            'fix_quality': r[4],
            'value':       (r[5] or 0) / 65535.0,
            'gps_ts':      r[6],
        }
        for r in rows
    ]


# ── Calibration state ─────────────────────────────────────────────────────────

def get_calib(study_id):
    with _calib_lock:
        if study_id not in _calib:
            _calib[study_id] = {
                'zero_value':  None,
                'plus_range':  0.04,
                'minus_range': 0.04,
            }
        return dict(_calib[study_id])


def set_calib(study_id, **kwargs):
    with _calib_lock:
        if study_id not in _calib:
            _calib[study_id] = {'zero_value': None, 'plus_range': 0.04, 'minus_range': 0.04}
        _calib[study_id].update(kwargs)


def list_studies():
    STUDIES_DIR.mkdir(parents=True, exist_ok=True)
    studies = []
    for p in sorted(STUDIES_DIR.glob('*.db'), key=lambda x: x.stat().st_mtime, reverse=True):
        sid = p.stem
        try:
            conn    = db_ro(str(p))
            meta    = dict(conn.execute('SELECT key,value FROM meta').fetchall())
            count   = conn.execute('SELECT COUNT(*) FROM detections').fetchone()[0]
            latest  = conn.execute(
                'SELECT ts FROM detections ORDER BY id DESC LIMIT 1'
            ).fetchone()
            conn.close()
        except Exception:
            meta, count, latest = {}, 0, None
        studies.append({
            'id':         sid,
            'name':       meta.get('study_name', sid),
            'created_at': meta.get('created_at', p.stat().st_ctime),
            'count':      count,
            'latest_ts':  latest[0] if latest else None,
            'live':       is_live(str(p)),
        })
    return studies


# ── Index page ────────────────────────────────────────────────────────────────

INDEX_TMPL = """<!DOCTYPE html>
<html>
<head>
  <title>Metal Mapper — Studies</title>
  <meta charset="utf-8">
  <style>
    body { font-family: Arial, sans-serif; background: #1a1a2e; color: #eee;
           margin: 0; padding: 24px; }
    h1   { color: #e94560; margin-top: 0; }
    .daemon-box { padding: 8px 16px; border-radius: 6px; display: inline-flex;
                  align-items: center; gap: 16px; margin-bottom: 22px;
                  font-weight: bold; font-size: 13px; }
    .daemon-on  { background: #1b5e20; color: #a5d6a7; }
    .daemon-off { background: #37474f; color: #90a4ae; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 28px; }
    th { background: #16213e; color: #a0a0c0; text-align: left; padding: 10px; }
    td { padding: 9px 10px; border-bottom: 1px solid #252545; }
    tr:hover td { background: #1f2b50; }
    a.view-btn { background: #0f3460; color: #e94560; padding: 4px 14px;
                 border-radius: 4px; text-decoration: none; font-size: 13px; }
    a.view-btn:hover { background: #e94560; color: #fff; }
    .live-badge { background: #1b5e20; color: #a5d6a7; padding: 2px 8px;
                  border-radius: 10px; font-size: 11px; font-weight: bold;
                  margin-left: 6px; }
    .new-study { background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
                 padding: 20px; max-width: 480px; }
    .new-study h2 { color: #e94560; margin-top: 0; font-size: 16px; }
    .new-study label { display: block; margin-top: 12px; color: #aaa; font-size: 12px; }
    .new-study input { width: 100%; box-sizing: border-box; padding: 7px; margin-top: 4px;
                       background: #0f3460; border: 1px solid #2a4080; color: #eee;
                       border-radius: 4px; font-size: 14px; }
    .new-study button { margin-top: 16px; background: #e94560; color: #fff; border: none;
                        padding: 9px 24px; border-radius: 4px; cursor: pointer;
                        font-size: 14px; font-weight: bold; }
    .new-study button:hover { background: #c73652; }
    .stop-btn { background: #b71c1c; color: #fff; border: none; padding: 5px 12px;
                border-radius: 4px; cursor: pointer; font-size: 12px; }
    .err { color: #ef9a9a; font-size: 13px; margin-top: 8px; }
    .empty { color: #555; text-align: center; padding: 20px; }
  </style>
</head>
<body>
<h1>Metal Mapper</h1>
<div style="color:#555; font-size:11px; font-family:monospace; margin-top:-14px; margin-bottom:18px;">{{ git_hash }}</div>

{% set ds = daemon_status %}
{% if ds.running %}
  <div class="daemon-box daemon-on">
    ● Daemon running — {{ ds.db_path | basename }}
    <form method="post" action="/stop_daemon" style="display:inline;">
      <button class="stop-btn">■ Stop</button>
    </form>
  </div>
{% else %}
  <div class="daemon-box daemon-off">○ No daemon running</div>
{% endif %}

{% if not ds.running %}
<div class="new-study">
  <h2>Start New Study</h2>
  <form method="post" action="/start_daemon">
    <label>Study Name</label>
    <input name="study_name" value="{{ default_study_name }}">
    <label>Serial Port</label>
    <input name="port" value="{{ default_port }}" placeholder="/dev/ttyACM0">
    <label>Min Distance (mm)</label>
    <input name="min_dist" value="20" type="number" min="1" max="10000">
    <button type="submit">▶ Start Recording</button>
  </form>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</div>
{% endif %}

<table>
  <tr>
    <th>Study</th>
    <th>Created</th>
    <th>Points</th>
    <th>Last Activity</th>
    <th></th>
  </tr>
  {% for s in studies %}
  <tr>
    <td>
      {{ s.name }}
      {% if s.live %}<span class="live-badge">● LIVE</span>{% endif %}
    </td>
    <td>{{ s.created_at | ts_fmt }}</td>
    <td>{{ s.count }}</td>
    <td>{{ s.latest_ts | ts_fmt if s.latest_ts else '—' }}</td>
    <td><a class="view-btn" href="/view/{{ s.id }}">View Map</a></td>
  </tr>
  {% else %}
  <tr><td colspan="5" class="empty">No studies yet — start one below.</td></tr>
  {% endfor %}
</table>

</body>
</html>"""


# ── Map view page ─────────────────────────────────────────────────────────────

VIEW_TMPL = """<!DOCTYPE html>
<html>
<head>
  <title>{{ study_name }} — Metal Mapper</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="/static/leaflet.css"/>
  <style>
    body { margin:0; padding:0; font-family:Arial,sans-serif; background:#111; }
    #map { position:absolute; top:120px; left:0; right:0; bottom:0; }
    #toolbar {
      position:absolute; top:0; left:0; right:0; height:120px;
      background:rgba(0,0,0,0.88); color:#fff; padding:8px 14px;
      z-index:1000; display:flex; align-items:center; gap:14px; overflow:hidden;
    }
    .toolbar-sep { width:1px; background:rgba(255,255,255,0.15); align-self:stretch; margin:4px 0; }
    /* title / badges */
    #title-block { display:flex; flex-direction:column; gap:4px; min-width:120px; }
    #title-block .study-name { font-size:14px; font-weight:bold; max-width:160px;
                               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .live-badge   { background:#1b5e20; color:#a5d6a7; padding:2px 8px;
                    border-radius:10px; font-size:11px; font-weight:bold; display:inline-block; }
    .saved-badge  { background:#37474f; color:#90a4ae; padding:2px 8px;
                    border-radius:10px; font-size:11px; display:inline-block; }
    /* buttons */
    .btn { border:none; padding:5px 12px; border-radius:4px; cursor:pointer;
           font-size:12px; font-weight:bold; color:#fff; }
    .btn-green { background:#2e7d32; }
    .btn-blue  { background:#1565c0; }
    .btn-stack { display:flex; flex-direction:column; gap:5px; }
    /* range bar */
    .range-bar { width:440px; height:13px; position:relative;
                 border:2px solid #fff; border-radius:4px; user-select:none; }
    .zero-mark  { position:absolute; width:3px; height:100%; background:#fff;
                  border:1px solid #000; top:0; z-index:10; pointer-events:none; }
    .val-mark   { position:absolute; width:2px; height:100%;
                  background:rgba(255,255,0,0.9); border:1px solid rgba(255,255,255,0.7);
                  top:0; z-index:15; pointer-events:none; transition:left 0.1s ease-out; }
    .slider-row { display:flex; align-items:center; gap:6px; font-size:12px; }
    .slider-row label { min-width:56px; }
    .slider-row input[type=range] { width:120px; height:4px; }
    .slider-row .sv { min-width:36px; font-family:monospace; }
    .sliders-col { display:flex; flex-direction:column; gap:4px; }
    /* info */
    .info-col { display:flex; flex-direction:column; gap:4px; font-size:12px;
                min-width:90px; }
    #last-val  { font-size:22px; font-family:monospace; font-weight:bold; }
    #fq-text   { font-weight:bold; color:#ff0; font-size:12px; }
    /* timing panel */
    #timing-panel {
      display:none;
      background:rgba(255,255,255,0.07); border:1px solid rgba(255,255,255,0.18);
      border-radius:6px; padding:8px 12px; font-size:12px; min-width:160px;
    }
    #timing-panel h4 { margin:0 0 6px 0; color:#a5d6a7; font-size:10px;
                       text-transform:uppercase; letter-spacing:.05em; }
    .tm-row { display:flex; align-items:center; gap:5px; margin-bottom:4px; }
    .tm-lbl  { min-width:62px; color:#aaa; }
    .tm-val  { min-width:32px; font-family:monospace; color:#fff; }
    .tm-btn  { background:#0d47a1; color:#fff; border:none; padding:1px 7px;
               border-radius:3px; cursor:pointer; font-size:11px; }
    .tm-btn:hover { background:#1565c0; }
    .save-btn { background:#4a148c; color:#fff; border:none; padding:3px 10px;
                border-radius:3px; cursor:pointer; font-size:11px;
                font-weight:bold; margin-top:5px; width:100%; }
    .save-btn:hover { background:#6a1b9a; }
    /* back */
    #back { background:#37474f; color:#ccc; border:none; padding:4px 10px;
            border-radius:4px; cursor:pointer; font-size:12px; }
    #back:hover { background:#546e7a; }
    #mapToggle { background:#37474f; color:#90a4ae; border:1px solid #546e7a;
                 padding:3px 10px; border-radius:4px; cursor:pointer; font-size:11px; }
    #mapToggle.offline { background:#1a237e; color:#7986cb; border-color:#3949ab; }
  </style>
  <script src="/static/leaflet.js"></script>
</head>
<body>
<div id="toolbar">

  <button id="back" onclick="location.href='/'">← Studies</button>

  <div id="title-block">
    <span class="study-name" title="{{ study_name }}">{{ study_name }}</span>
    {% if is_live %}
      <span class="live-badge">● LIVE</span>
    {% else %}
      <span class="saved-badge">◉ Saved</span>
    {% endif %}
    <button id="mapToggle" onclick="toggleMapLayer()">🌐 Map</button>
  </div>

  <div class="toolbar-sep"></div>

  <div class="btn-stack">
    <button class="btn btn-blue" onclick="recalibrate()">🎯 Re-zero</button>
    <button id="pauseBtn" class="btn btn-green" onclick="togglePause()">⏸ Pause</button>
  </div>

  <div>
    <div class="range-bar" id="rangeBar">
      <div class="zero-mark" id="zeroMark"></div>
      <div class="val-mark"  id="valMark"></div>
    </div>
    <div style="display:flex;gap:14px;margin-top:5px;align-items:flex-start;">
      <div class="sliders-col">
        <div class="slider-row">
          <label>− Range</label>
          <input type="range" id="minusSl" min="0.01" max="5" step="0.01"
                 value="0.04" oninput="onRangeSlider()">
          <span class="sv" id="minusV">0.04</span>
        </div>
        <div class="slider-row">
          <label>+ Range</label>
          <input type="range" id="plusSl" min="0.01" max="5" step="0.01"
                 value="0.04" oninput="onRangeSlider()">
          <span class="sv" id="plusV">0.04</span>
        </div>
        <div class="slider-row">
          <label>Offset</label>
          <input type="range" id="offSl" min="-2" max="2" step="0.005"
                 value="0.674" oninput="onOffset()">
          <span class="sv" id="offV">0.674</span>
        </div>
        <button class="btn btn-blue"
                style="font-size:11px;padding:2px 8px;margin-top:3px;align-self:flex-start;"
                onclick="saveSliders(this)">💾 Save View</button>
      </div>
      <div style="text-align:center;padding-top:4px;">
        <div style="font-size:10px;color:#888;">Last ADC</div>
        <div id="last-val" style="color:#fff;">—</div>
      </div>
    </div>
  </div>

  <div class="toolbar-sep"></div>

  <div class="info-col">
    <div>Points: <span id="ptCount">0</span></div>
    <div>Zero: <span id="zeroDisp">—</span></div>
    <div>Zoom: <span id="zoomDisp">—</span></div>
    <div id="fq-text">No Fix</div>
    <div id="motion-row" style="display:none;">
      Motion: <span id="motionVal" style="font-family:monospace;">—</span>
    </div>
    <div class="slider-row" style="margin-top:4px;">
      <label>Size</label>
      <input type="range" id="sizeSl" min="1" max="6" step="1"
             value="2" oninput="onSize()">
      <span class="sv" id="sizeV">2</span>
    </div>
  </div>

  <div id="timing-panel">
    <h4>Detector Timing</h4>
    <div class="tm-row">
      <span class="tm-lbl">Blanking</span>
      <span class="tm-val" id="blankVal">—</span>µs
      <button class="tm-btn" onclick="sendCmd('a')">+2</button>
      <button class="tm-btn" onclick="sendCmd('z')">−2</button>
    </div>
    <div class="tm-row">
      <span class="tm-lbl">RX Window</span>
      <span class="tm-val" id="winVal">—</span>µs
      <button class="tm-btn" onclick="sendCmd('s')">+1</button>
      <button class="tm-btn" onclick="sendCmd('x')">−1</button>
    </div>
    <button class="save-btn" onclick="sendCmd('S')">💾 Save Settings</button>
  </div>

</div>
<div id="map"></div>

<script>
// ── Constants injected from Flask ─────────────────────────────────────────────
const STUDY_ID      = {{ study_id      | tojson }};
const IS_LIVE       = {{ is_live       | tojson }};
const MAP_CENTER    = {{ map_center    | tojson }};
const GEOJSON_FILES = {{ geojson_files | tojson }};
const BAR_W      = 440;

// ── State ─────────────────────────────────────────────────────────────────────
let allPoints   = [];
let markers     = [];
let latestMkr   = null;
let polyline    = null;
let lastId      = 0;
let isPaused    = false;
let dotSize     = 2;
let zeroVal     = null;
let manualOff   = 0.674;
let plusRange   = 0.04;
let minusRange  = 0.04;
let lastADC     = null;

// ── Map setup ─────────────────────────────────────────────────────────────────
const mapObj = L.map('map', {maxZoom:25}).setView(MAP_CENTER, 20);

// ── Tile layer with manual online/offline toggle ──────────────────────────────
const BlankLayer = L.GridLayer.extend({
  createTile: function() {
    const s   = this.getTileSize();
    const c   = document.createElement('canvas');
    c.width   = s.x;  c.height = s.y;
    const ctx = c.getContext('2d');
    ctx.fillStyle   = '#d8d8d0';
    ctx.fillRect(0, 0, s.x, s.y);
    ctx.strokeStyle = '#c8c8c0';
    ctx.lineWidth   = 1;
    ctx.strokeRect(0, 0, s.x, s.y);
    return c;
  }
});
const blankLayer = new BlankLayer();
const osmLayer   = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom:25, maxNativeZoom:19,
  attribution:'© OpenStreetMap contributors'
});

function applyMapMode(mode) {
  const btn = document.getElementById('mapToggle');
  if (mode === 'offline') {
    osmLayer.remove();
    blankLayer.addTo(mapObj);
    btn.textContent = '✈ Offline';
    btn.classList.add('offline');
  } else {
    blankLayer.remove();
    osmLayer.addTo(mapObj);
    btn.textContent = '🌐 Map';
    btn.classList.remove('offline');
  }
  localStorage.setItem('mapMode', mode);
}

function toggleMapLayer() {
  applyMapMode(localStorage.getItem('mapMode') === 'offline' ? 'online' : 'offline');
}

applyMapMode(localStorage.getItem('mapMode') || 'online');

L.control.scale({imperial:true, metric:true, maxWidth:200}).addTo(mapObj);
mapObj.on('zoomend', () => {
  document.getElementById('zoomDisp').textContent = mapObj.getZoom();
});

// Load all .geojson files found alongside the server script
GEOJSON_FILES.forEach(name => {
  fetch('/geojson/' + name)
    .then(r => r.json())
    .then(d => L.geoJSON(d, {
      style: {color:'#FF8800', weight:2, opacity:0.6, fillOpacity:0},
      onEachFeature: (f, l) => { if (f.properties?.Layer) l.bindPopup(f.properties.Layer); }
    }).addTo(mapObj))
    .catch(() => {});
});

// ── Color mapping (same scheme as map_serial_stream.py) ───────────────────────
function getColor(val) {
  if (zeroVal === null) return 'rgb(128,128,128)';
  const z = zeroVal + manualOff;
  if (val <= z) {
    const n = minusRange > 0 ? Math.max(0, Math.min(1, (val - (z - minusRange)) / minusRange)) : 0;
    return `rgb(${Math.round(n*128)},${Math.round(255-n*127)},${Math.round(n*128)})`;
  } else {
    const n = plusRange  > 0 ? Math.max(0, Math.min(1, (val - z) / plusRange))  : 0;
    return `rgb(${Math.round(128+n*127)},${Math.round(128-n*128)},${Math.round(128-n*128)})`;
  }
}

// ── Range bar ─────────────────────────────────────────────────────────────────
function refreshBar() {
  if (zeroVal === null) return;
  document.getElementById('zeroMark').style.left = (BAR_W * 0.5) + 'px';
  document.getElementById('rangeBar').style.background =
    'linear-gradient(to right, #00ff00, #808080 50%, #ff0000)';
  refreshValMark();
}

function refreshValMark() {
  if (lastADC === null || zeroVal === null) return;
  const z = zeroVal + manualOff;
  let pct;
  if (lastADC <= z) {
    pct = minusRange > 0 ? (lastADC - (z - minusRange)) / minusRange * 50 : 50;
  } else {
    pct = plusRange > 0 ? 50 + (lastADC - z) / plusRange * 50 : 50;
  }
  document.getElementById('valMark').style.left =
    (Math.max(0, Math.min(100, pct)) / 100 * BAR_W) + 'px';
}

// ── Marker helpers ────────────────────────────────────────────────────────────
function markerStyle(point, isLatest) {
  const c = getColor(point.value);
  return {
    radius:      isLatest ? dotSize * 2 : dotSize,
    fillColor:   c,
    color:       isLatest ? '#fff' : c,
    weight:      isLatest ? 1.5 : 0.3,
    opacity:     1,
    fillOpacity: isLatest ? 1.0 : 0.7,
  };
}

function popupFor(p) {
  return `ADC raw: ${Math.round(p.value * 65535)}<br>` +
         `Lat: ${p.lat.toFixed(6)}<br>Lon: ${p.lon.toFixed(6)}`;
}

// Full re-render (called after range/offset/size/recalibrate change)
function renderAll() {
  markers.forEach(m => mapObj.removeLayer(m));
  markers = []; latestMkr = null;
  if (polyline) { mapObj.removeLayer(polyline); polyline = null; }
  if (!allPoints.length) return;

  polyline = L.polyline(allPoints.map(p => [p.lat, p.lon]),
    {color:'#4444ff', weight:1, opacity:0.4}).addTo(mapObj);

  allPoints.forEach((p, i) => {
    const isLast = (i === allPoints.length - 1);
    const m = L.circleMarker([p.lat, p.lon], markerStyle(p, isLast)).addTo(mapObj);
    if (i >= allPoints.length - 200) m.bindPopup(popupFor(p));
    markers.push(m);
    if (isLast) latestMkr = m;
  });
  document.getElementById('ptCount').textContent = allPoints.length;
}

// Incremental add (called during live streaming)
function addPoint(p) {
  // Downgrade previous latest marker to normal style
  if (latestMkr && allPoints.length > 0) {
    latestMkr.setStyle(markerStyle(allPoints[allPoints.length - 1], false));
  }
  allPoints.push(p);
  const m = L.circleMarker([p.lat, p.lon], markerStyle(p, true)).addTo(mapObj);
  m.bindPopup(popupFor(p));
  markers.push(m);
  latestMkr = m;

  if (polyline) {
    polyline.addLatLng([p.lat, p.lon]);
  } else {
    polyline = L.polyline([[p.lat, p.lon]],
      {color:'#4444ff', weight:1, opacity:0.4}).addTo(mapObj);
  }

  document.getElementById('ptCount').textContent = allPoints.length;
  if (!isPaused) mapObj.panTo([p.lat, p.lon], {animate:true, duration:0.25});

  lastADC = p.value;
  const c = getColor(p.value);
  const lv = document.getElementById('last-val');
  lv.textContent = Math.round(p.value * 65535);
  lv.style.color = c;
  refreshValMark();

  const fqLabels = ['No Fix','2D','3D','PPS','RTK Fixed','RTK Float'];
  const fqEl = document.getElementById('fq-text');
  fqEl.textContent = fqLabels[p.fix_quality] || 'No Fix';
  fqEl.style.color = p.fix_quality === 0 ? '#ff4444'
                   : p.fix_quality >= 4   ? '#44ff44' : '#ff0';
}

// ── Initial calibration from first 10 points ──────────────────────────────────
function initCalib(points) {
  if (!points.length) { zeroVal = 0.5; return; }
  const sample = points.slice(0, Math.min(10, points.length));
  zeroVal = sample.reduce((a, p) => a + p.value, 0) / sample.length;
  document.getElementById('zeroDisp').textContent = zeroVal.toFixed(4);
  refreshBar();
}

// ── Load all existing data then optionally open SSE ───────────────────────────
fetch('/data/' + STUDY_ID)
  .then(r => r.json())
  .then(d => {
    allPoints = d.points;
    lastId    = d.last_id;

    // Restore saved slider settings when present
    if (d.sl_minus_range != null) {
      minusRange = d.sl_minus_range;
      document.getElementById('minusSl').value = minusRange;
      document.getElementById('minusV').textContent = minusRange.toFixed(2);
    }
    if (d.sl_plus_range != null) {
      plusRange = d.sl_plus_range;
      document.getElementById('plusSl').value = plusRange;
      document.getElementById('plusV').textContent = plusRange.toFixed(2);
    }
    if (d.sl_dot_size != null) {
      dotSize = d.sl_dot_size;
      document.getElementById('sizeSl').value = dotSize;
      document.getElementById('sizeV').textContent = dotSize;
    }
    if (d.sl_offset != null) {
      manualOff = d.sl_offset;
      document.getElementById('offSl').value = manualOff;
    }
    if (d.sl_zero_value != null) {
      zeroVal = d.sl_zero_value;
      document.getElementById('zeroDisp').textContent = zeroVal.toFixed(4);
      document.getElementById('offV').textContent = (zeroVal + manualOff).toFixed(3);
      refreshBar();
    } else {
      initCalib(allPoints);
    }

    renderAll();

    if (allPoints.length) {
      const latest = allPoints[allPoints.length - 1];
      lastADC = latest.value;
      document.getElementById('last-val').textContent = Math.round(latest.value * 65535);
      document.getElementById('last-val').style.color = getColor(latest.value);
      refreshValMark();
      mapObj.setView([latest.lat, latest.lon], 20);
    }

    if (d.is_live) {
      document.getElementById('timing-panel').style.display = 'block';
      document.getElementById('motion-row').style.display   = 'block';
      if (d.blanking_us)  document.getElementById('blankVal').textContent = d.blanking_us;
      if (d.rx_window_us) document.getElementById('winVal').textContent   = d.rx_window_us;
      openSSE();
    }
  });

// ── SSE (live studies only) ───────────────────────────────────────────────────
function openSSE() {
  const es = new EventSource('/stream/' + STUDY_ID + '?since_id=' + lastId);
  es.onmessage = function(e) {
    const d = JSON.parse(e.data);
    if (d.type === 'complete') { es.close(); return; }
    if (d.blanking_us)  document.getElementById('blankVal').textContent = d.blanking_us;
    if (d.rx_window_us) document.getElementById('winVal').textContent   = d.rx_window_us;
    if (d.motion_mm != null) updateMotion(d.motion_mm, d.motion_paused);
    if (d.type === 'rows' && d.rows && d.rows.length) {
      lastId = d.rows[d.rows.length - 1].id;
      if (!isPaused) d.rows.forEach(addPoint);
    }
  };
  es.onerror = () => console.warn('[SSE] connection error');
}

// ── Toolbar controls ──────────────────────────────────────────────────────────
function togglePause() {
  isPaused = !isPaused;
  const btn = document.getElementById('pauseBtn');
  btn.textContent    = isPaused ? '▶ Resume' : '⏸ Pause';
  btn.style.background = isPaused ? '#c62828' : '';
}

function recalibrate() {
  fetch('/recalibrate/' + STUDY_ID, {method:'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.success) return;
      zeroVal = d.zero_value;
      manualOff = 0;
      document.getElementById('zeroDisp').textContent = zeroVal.toFixed(4);
      document.getElementById('offSl').value = 0;
      document.getElementById('offV').textContent  = '0.000';
      refreshBar();
      renderAll();
    });
}

function onRangeSlider() {
  minusRange = parseFloat(document.getElementById('minusSl').value);
  plusRange  = parseFloat(document.getElementById('plusSl').value);
  document.getElementById('minusV').textContent = minusRange.toFixed(2);
  document.getElementById('plusV').textContent  = plusRange.toFixed(2);
  refreshBar();
  renderAll();
}

function onOffset() {
  manualOff = parseFloat(document.getElementById('offSl').value);
  const adjZ = zeroVal !== null ? zeroVal + manualOff : manualOff;
  document.getElementById('offV').textContent = adjZ.toFixed(3);
  refreshBar();
  renderAll();
}

function onSize() {
  dotSize = parseInt(document.getElementById('sizeSl').value);
  document.getElementById('sizeV').textContent = dotSize;
  renderAll();
}

function updateMotion(mm, paused) {
  const el = document.getElementById('motionVal');
  if (!el) return;
  if (paused === '1') {
    el.textContent  = 'PAUSED';
    el.style.color  = '#ff8800';
  } else {
    el.textContent  = Math.round(parseFloat(mm)) + ' mm/pt';
    el.style.color  = '#a5d6a7';
  }
}

function saveSliders(btn) {
  fetch('/settings/' + STUDY_ID, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      plus_range:  plusRange,
      minus_range: minusRange,
      offset:      manualOff,
      dot_size:    dotSize,
      zero_value:  zeroVal,
    })
  }).then(r => r.json()).then(d => {
    if (d.ok && btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ Saved';
      setTimeout(() => { btn.textContent = orig; }, 1200);
    }
  }).catch(() => {});
}

function sendCmd(byte) {
  fetch('/cmd', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cmd: byte})
  })
  .then(r => r.json())
  .then(d => {
    if (d.blanking_us)  document.getElementById('blankVal').textContent = d.blanking_us;
    if (d.rx_window_us) document.getElementById('winVal').textContent   = d.rx_window_us;
  })
  .catch(() => {});
}
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    cfg = load_config()
    default_name = datetime.datetime.now().strftime('geo_%Y%m%d_%H%M%S')
    return render_template_string(INDEX_TMPL,
        studies=list_studies(),
        daemon_status=get_daemon_status(),
        error=request.args.get('error'),
        default_study_name=default_name,
        default_port=cfg.get('last_port', '/dev/cu.usbmodem1103'),
        git_hash=GIT_HASH,
    )


@app.route('/view/<study_id>')
def view_study(study_id):
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return redirect(url_for('index'))
    meta = read_meta(db_path)
    try:
        conn  = db_ro(db_path)
        first = conn.execute(
            'SELECT lat,lon FROM detections ORDER BY id LIMIT 1'
        ).fetchone()
        conn.close()
        center = [first[0], first[1]] if first else [37.7749, -122.4194]
    except Exception:
        center = [37.7749, -122.4194]
    geojson_files = sorted(p.name for p in Path(__file__).parent.glob('*.geojson'))
    return render_template_string(VIEW_TMPL,
        study_id=study_id,
        study_name=meta.get('study_name', study_id),
        is_live=is_live(db_path),
        map_center=center,
        geojson_files=geojson_files,
    )


@app.route('/data/<study_id>')
def get_data(study_id):
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    points = load_points(db_path)
    meta   = read_meta(db_path)
    calib  = get_calib(study_id)

    # Seed zero from first 10 points if not yet set
    if calib['zero_value'] is None and points:
        sample = [p['value'] for p in points[:10]]
        set_calib(study_id, zero_value=sum(sample) / len(sample))
        calib = get_calib(study_id)

    z = calib['zero_value'] or 0.5

    def _f(key):
        v = meta.get(key)
        return float(v) if v is not None else None

    return jsonify({
        'points':        points,
        'last_id':       points[-1]['id'] if points else 0,
        'is_live':       is_live(db_path),
        'zero_value':    z,
        'value_min':     z - calib['minus_range'],
        'value_max':     z + calib['plus_range'],
        'blanking_us':   meta.get('blanking_us'),
        'rx_window_us':  meta.get('rx_window_us'),
        'study_name':    meta.get('study_name', study_id),
        'sl_plus_range':  _f('sl_plus_range'),
        'sl_minus_range': _f('sl_minus_range'),
        'sl_offset':      _f('sl_offset'),
        'sl_dot_size':    int(_f('sl_dot_size')) if _f('sl_dot_size') is not None else None,
        'sl_zero_value':  _f('sl_zero_value'),
    })


@app.route('/stream/<study_id>')
def stream_study(study_id):
    db_path  = get_db_path(study_id)
    since_id = int(request.args.get('since_id', 0))

    def gen():
        nonlocal since_id
        while True:
            rows = load_points(db_path, since_id)
            meta = read_meta(db_path)
            motion = {
                'motion_mm':     meta.get('motion_mm'),
                'motion_paused': meta.get('motion_paused'),
            }
            if rows:
                since_id = rows[-1]['id']
                yield f"data: {json.dumps({'type': 'rows', 'rows': rows, 'blanking_us': meta.get('blanking_us'), 'rx_window_us': meta.get('rx_window_us'), **motion})}\n\n"
            elif not is_live(db_path):
                yield f"data: {json.dumps({'type': 'complete'})}\n\n"
                return
            else:
                yield f"data: {json.dumps({'type': 'heartbeat', **motion})}\n\n"
            time.sleep(0.5)

    return Response(
        stream_with_context(gen()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/start_daemon', methods=['POST'])
def start_daemon():
    if get_daemon_status()['running']:
        return redirect(url_for('index', error='Daemon is already running'))

    port     = request.form.get('port', '/dev/ttyACM0').strip()
    min_dist = request.form.get('min_dist', '20').strip()
    name     = request.form.get('study_name', '').strip()

    safe    = re.sub(r'[^\w-]', '_', name) if name else 'study'
    ts      = int(time.time())
    db_path = str(STUDIES_DIR / f'{safe}_{ts}.db')
    STUDIES_DIR.mkdir(parents=True, exist_ok=True)

    log_path = str(STUDIES_DIR / f'{safe}_{ts}.log')
    cmd = [sys.executable, str(DAEMON_SCRIPT),
           '--port', port, '--baud', '460800',
           '--db', db_path, '--study-name', name or safe,
           '--min-dist', min_dist]
    save_config(last_port=port)
    print(f'[web] launching daemon: {" ".join(cmd)}', flush=True)
    print(f'[web] daemon log: {log_path}', flush=True)
    subprocess.Popen(
        cmd,
        stdout=open(log_path, 'w'),
        stderr=subprocess.STDOUT,
        close_fds=True,
    )

    # Wait up to 3s for the daemon to write its PID file rather than a blind sleep.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if get_daemon_status()['running']:
            print(f'[web] daemon up (pid={get_daemon_status()["pid"]})', flush=True)
            break
        time.sleep(0.1)
    else:
        print(f'[web] WARNING: daemon did not appear in PID file — check {log_path}', flush=True)

    return redirect(url_for('view_study', study_id=f'{safe}_{ts}'))


@app.route('/stop_daemon', methods=['POST'])
def stop_daemon():
    status = get_daemon_status()
    if status['running']:
        try:
            os.kill(status['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return redirect(url_for('index'))


@app.route('/cmd', methods=['POST'])
def send_cmd():
    data = request.get_json(force=True)
    cmd  = str(data.get('cmd', ''))[:1]
    if not cmd:
        return jsonify({'error': 'No command'}), 400
    try:
        fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, cmd.encode())
        os.close(fd)
    except OSError:
        return jsonify({'error': 'Daemon not running'}), 503

    # Return whatever meta is currently in the db (daemon updates it asynchronously)
    status = get_daemon_status()
    meta   = read_meta(status['db_path']) if status['db_path'] else {}
    return jsonify({
        'ok':          True,
        'blanking_us':  meta.get('blanking_us'),
        'rx_window_us': meta.get('rx_window_us'),
    })


@app.route('/recalibrate/<study_id>', methods=['POST'])
def recalibrate(study_id):
    db_path = get_db_path(study_id)
    try:
        conn = db_ro(db_path)
        rows = conn.execute(
            'SELECT adc_raw FROM detections ORDER BY id DESC LIMIT 10'
        ).fetchall()
        conn.close()
    except Exception:
        return jsonify({'success': False, 'error': 'DB error'})
    if not rows:
        return jsonify({'success': False, 'error': 'No data yet'})

    vals = [(r[0] or 0) / 65535.0 for r in rows]
    zero = sum(vals) / len(vals)
    set_calib(study_id, zero_value=zero)
    calib = get_calib(study_id)
    return jsonify({
        'success':    True,
        'zero_value': zero,
        'value_min':  zero - calib['minus_range'],
        'value_max':  zero + calib['plus_range'],
    })


@app.route('/settings/<study_id>', methods=['POST'])
def save_settings(study_id):
    db_path = get_db_path(study_id)
    if not os.path.exists(db_path):
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(force=True)
    mapping = {
        'sl_plus_range':  data.get('plus_range'),
        'sl_minus_range': data.get('minus_range'),
        'sl_offset':      data.get('offset'),
        'sl_dot_size':    data.get('dot_size'),
        'sl_zero_value':  data.get('zero_value'),
    }
    try:
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA busy_timeout=1000')
        for key, val in mapping.items():
            if val is not None:
                conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(val)))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/geojson/<path:filename>')
def serve_geojson(filename):
    p = Path(__file__).parent / filename
    if p.exists() and p.suffix == '.geojson':
        return send_file(str(p), mimetype='application/json')
    return '', 404


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Metal Mapper web front-end')
    ap.add_argument('--web-port', type=int, default=5000)
    args = ap.parse_args()

    STUDIES_DIR.mkdir(parents=True, exist_ok=True)
    print(f'Metal Mapper starting on http://localhost:{args.web_port}')
    print(f'Studies directory: {STUDIES_DIR}')
    app.run(host='0.0.0.0', port=args.web_port, debug=False, threaded=True)
