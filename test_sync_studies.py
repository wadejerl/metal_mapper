#!/usr/bin/env python3
"""
test_sync_studies.py — regression battery for sync_studies.sh.

Simulates BOTH head units and the hub in one temp tree: two studies dirs,
two config.json files, and a plain local directory standing in for the
hub's rsync module (rsync treats a local path and rsync:// identically for
everything the script does). Study dbs are built with serial_daemon's own
db_open/insert_rows so the WAL behavior is the real thing. Run before
deploying:

    python3 test_sync_studies.py
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import serial_daemon as sd  # noqa: E402

SCRIPT = os.path.join(REPO, 'sync_studies.sh')

# GNU rsync >= 3.1 (the Pis) skips a --files-from entry the hub lacks
# silently when told --ignore-missing-args; openrsync (Mac bench) cannot,
# so a few checks below relax to "tolerated" there. To run the battery the
# way the field runs it, put a GNU rsync first on PATH.
HAVE_IMA = subprocess.run(['rsync', '--ignore-missing-args', '--version'],
                          capture_output=True).returncode == 0
RSYNC_FLAVOR = subprocess.run(['rsync', '--version'], capture_output=True,
                              text=True).stdout.splitlines()[0].strip()

FAILED = []


def check(name, ok, detail=''):
    print(f'  {"ok  " if ok else "FAIL"} {name}'
          + (f'  {detail}' if not ok and detail else ''))
    if not ok:
        FAILED.append(name)


SCRATCH = tempfile.mkdtemp(prefix='sync_test_')
HUB = os.path.join(SCRATCH, 'hub')
os.makedirs(HUB)

UNITS = {}
for tag in ('unitA', 'unitB'):
    sdir = os.path.join(SCRATCH, tag, 'studies')
    os.makedirs(sdir)
    cfg = os.path.join(SCRATCH, tag, 'config.json')
    Path(cfg).write_text(json.dumps({'studies_dir': sdir, 'sync_hub': HUB}))
    UNITS[tag] = {'sdir': sdir, 'cfg': cfg,
                  'pid': os.path.join(SCRATCH, tag, 'daemon.pid')}


def run_sync(tag, *args):
    """One sync cycle as head unit <tag>. MM_PID_FILE is ALWAYS overridden:
    without it a real daemon on this bench would leak into the test."""
    u = UNITS[tag]
    env = dict(os.environ, MM_CONFIG_FILE=u['cfg'], MM_PID_FILE=u['pid'],
               MM_HOST_TAG=tag)
    return subprocess.run(['bash', SCRIPT, *args], env=env,
                          capture_output=True, text=True, timeout=120)


def make_study(tag, stem, n_points=3):
    """Schema-true study db, cleanly closed (WAL checkpointed away)."""
    path = os.path.join(UNITS[tag]['sdir'], f'{stem}.db')
    conn = sd.db_open(path)
    rows = [{'lat': 40.1 + i * 1e-6, 'lon': -119.1, 'heading': 90.0,
             'fix': 4, 'adc': [1000 + i] * 8, 'gps_ts': '%.3f' % (100.0 + i)}
            for i in range(n_points)]
    if rows:
        sd.insert_rows(conn, rows, 12.4, 25.0)
    sd.meta_set(conn, 'study_name', stem)
    conn.close()
    return path


def points_in(path):
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            return conn.execute('SELECT COUNT(*) FROM points').fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


def wal_gone(wal_path):
    """Resolved wal = absent OR empty. Some sqlite builds (Apple) persist a
    0-byte -wal after the last close; readers materialize them too. The
    script's hot-journal gates use -s (non-empty) for the same reason."""
    return not os.path.exists(wal_path) or os.path.getsize(wal_path) == 0


def sidecar(tag, study_stem, owner, targets):
    p = os.path.join(UNITS[tag]['sdir'],
                     f'{study_stem}.targets__{owner}.json')
    Path(p).write_text(json.dumps({
        'version': 1, 'study': study_stem, 'host': owner,
        'seq': len(targets),
        'targets': targets, 'cleared': {}}))
    return p


print(f'sync_studies.sh:  (rsync on PATH: {RSYNC_FLAVOR}; '
      f'--ignore-missing-args {"yes" if HAVE_IMA else "no"})')

# ── disabled / unreachable are quiet no-ops ──────────────────────────────────
nocfg = os.path.join(SCRATCH, 'nohub.json')
Path(nocfg).write_text(json.dumps({'studies_dir': UNITS['unitA']['sdir']}))
r = subprocess.run(['bash', SCRIPT],
                   env=dict(os.environ, MM_CONFIG_FILE=nocfg,
                            MM_PID_FILE=UNITS['unitA']['pid'],
                            MM_HOST_TAG='unitA'),
                   capture_output=True, text=True)
check('no sync_hub: exit 0 and says so', r.returncode == 0
      and 'not configured' in r.stdout, (r.returncode, r.stdout))

badcfg = os.path.join(SCRATCH, 'badhub.json')
Path(badcfg).write_text(json.dumps({'studies_dir': UNITS['unitA']['sdir'],
                                    'sync_hub': os.path.join(SCRATCH, 'gone')}))
r = subprocess.run(['bash', SCRIPT],
                   env=dict(os.environ, MM_CONFIG_FILE=badcfg,
                            MM_PID_FILE=UNITS['unitA']['pid'],
                            MM_HOST_TAG='unitA'),
                   capture_output=True, text=True)
check('hub unreachable: exit 0 and says so', r.returncode == 0
      and 'unreachable' in r.stdout, (r.returncode, r.stdout))

# ── own closed study + sidecar + log push, other unit pulls ──────────────────
a1 = make_study('unitA', 'a1_1758000001__unitA')
Path(a1[:-3] + '.log').write_text('log A')
sidecar('unitA', 'a1_1758000001__unitA', 'unitA',
        [{'id': 1, 'lat': 40.2, 'lon': -119.2, 'created_at': 1.0}])
r = run_sync('unitA')
check('own push cycle ok', r.returncode == 0, (r.returncode, r.stdout, r.stderr))
check('hub got study + sidecar + log',
      points_in(os.path.join(HUB, 'a1_1758000001__unitA.db')) == 3
      and os.path.exists(os.path.join(
          HUB, 'a1_1758000001__unitA.targets__unitA.json'))
      and os.path.exists(os.path.join(HUB, 'a1_1758000001__unitA.log')),
      sorted(os.listdir(HUB)))
# no live study this cycle: the stage's own work files (the sidecar
# validity list, rsync lists) must not ride the snapshot push to the hub
check('no stage work file leaks to the hub (no dotfiles)',
      not [n for n in os.listdir(HUB) if n.startswith('.')],
      sorted(os.listdir(HUB)))

r = run_sync('unitB')
check('other unit pulled study + sidecar',
      points_in(os.path.join(UNITS['unitB']['sdir'],
                             'a1_1758000001__unitA.db')) == 3
      and os.path.exists(os.path.join(
          UNITS['unitB']['sdir'],
          'a1_1758000001__unitA.targets__unitA.json')),
      sorted(os.listdir(UNITS['unitB']['sdir'])))

# ── cross-flagging: B's sidecar on A's study reaches A ───────────────────────
sidecar('unitB', 'a1_1758000001__unitA', 'unitB',
        [{'id': 1, 'lat': 40.3, 'lon': -119.3, 'created_at': 2.0}])
run_sync('unitB')
run_sync('unitA')
p = os.path.join(UNITS['unitA']['sdir'],
                 'a1_1758000001__unitA.targets__unitB.json')
check("B's flags on A's study reach A", os.path.exists(p)
      and json.loads(Path(p).read_text())['host'] == 'unitB',
      sorted(os.listdir(UNITS['unitA']['sdir'])))

# B updates its sidecar (new flag) → the update propagates (pull pass 2).
sidecar('unitB', 'a1_1758000001__unitA', 'unitB',
        [{'id': 1, 'lat': 40.3, 'lon': -119.3, 'created_at': 2.0},
         {'id': 2, 'lat': 40.4, 'lon': -119.4, 'created_at': 3.0}])
run_sync('unitB')
run_sync('unitA')
check("B's sidecar UPDATE reaches A",
      len(json.loads(Path(p).read_text())['targets']) == 2,
      Path(p).read_text())

# ── live study: snapshot ships, hot files never do, growth propagates ────────
live = make_study('unitA', 'live_1758000002__unitA', n_points=2)
conn_live = sd.db_open(live)          # held open: -wal/-shm exist, WAL hot
sd.insert_rows(conn_live, [{'lat': 40.5, 'lon': -119.5, 'heading': 90.0,
                            'fix': 4, 'adc': [1] * 8, 'gps_ts': '1'}],
               12.4, 25.0)
Path(UNITS['unitA']['pid']).write_text(f'{os.getpid()}:{live}')
r = run_sync('unitA')
hub_live = os.path.join(HUB, 'live_1758000002__unitA.db')
check('live cycle ok + snapshotted', r.returncode == 0
      and 'snapshotted live study' in r.stdout,
      (r.returncode, r.stdout, r.stderr))
check('hub got a consistent live snapshot', points_in(hub_live) == 3,
      points_in(hub_live))
check('hot -wal/-shm never reach the hub',
      not os.path.exists(hub_live + '-wal')
      and not os.path.exists(hub_live + '-shm'), sorted(os.listdir(HUB)))

run_sync('unitB')
b_live = os.path.join(UNITS['unitB']['sdir'], 'live_1758000002__unitA.db')
check('other unit sees the live study', points_in(b_live) == 3,
      points_in(b_live))

# it grows on A → the next two cycles grow it on B (pull pass 2 updates)
sd.insert_rows(conn_live, [{'lat': 40.6, 'lon': -119.6, 'heading': 90.0,
                            'fix': 4, 'adc': [2] * 8, 'gps_ts': '2'}] * 2,
               12.4, 25.0)
run_sync('unitA')
run_sync('unitB')
check('live growth propagates A → hub → B', points_in(b_live) == 5,
      points_in(b_live))

# stop 'recording': close + drop the pid file; the real db replaces the
# snapshot on the hub (same name, owner push)
conn_live.close()
os.unlink(UNITS['unitA']['pid'])
run_sync('unitA')
check('closed study replaces its snapshot on the hub',
      points_in(hub_live) == 5, points_in(hub_live))

# ── crashed session (wal-hot, no daemon): healed and pushed ──────────────────
# A real crash leaves a valid -wal that may hold ALL the committed points;
# nothing else ever checkpoints an old id, so the sync script must. Simulate
# with a child process that commits rows and dies without closing.
crashed = make_study('unitA', 'crash_1758000003__unitA', n_points=2)
crash_code = f'''
import os, sys
sys.path.insert(0, {REPO!r})
import serial_daemon as sd
conn = sd.db_open({crashed!r})
sd.insert_rows(conn, [{{'lat': 40.7, 'lon': -119.7, 'heading': 90.0,
                        'fix': 4, 'adc': [3] * 8, 'gps_ts': '3'}}] * 4,
               12.4, 25.0)
os._exit(0)   # no close: -wal/-shm stay behind, holding the 4 rows
'''
subprocess.run([sys.executable, '-c', crash_code], check=True)
assert os.path.getsize(crashed + '-wal') > 0, 'crash sim left no hot wal?'
r = run_sync('unitA')
check('crashed session: consistent snapshot pushed WITH its wal rows',
      r.returncode == 0 and 'pushing snapshot of wal-hot' in r.stdout
      and wal_gone(crashed + '-wal')      # heal folded the wal for later cycles
      and points_in(os.path.join(HUB, 'crash_1758000003__unitA.db')) == 6,
      (r.returncode, r.stdout,
       points_in(os.path.join(HUB, 'crash_1758000003__unitA.db'))))
r = run_sync('unitA')
check('healed session converges to a plain push next cycle',
      r.returncode == 0 and 'wal-hot' not in r.stdout
      and points_in(os.path.join(HUB, 'crash_1758000003__unitA.db')) == 6,
      (r.returncode, r.stdout))

# a db whose wal frames a live connection still pins ships as a snapshot
# too — never the raw file (VACUUM INTO reads through the pinned wal)
held = make_study('unitA', 'held_1758000004__unitA', n_points=1)
conn_held = sd.db_open(held)
sd.insert_rows(conn_held, [{'lat': 40.8, 'lon': -119.8, 'heading': 90.0,
                            'fix': 4, 'adc': [4] * 8, 'gps_ts': '4'}],
               12.4, 25.0)
pin = sqlite3.connect(held, isolation_level=None)
pin.execute('BEGIN')
pin.execute('SELECT COUNT(*) FROM points').fetchone()   # pins the wal
r = run_sync('unitA')
check('wal pinned by a live connection: snapshot pushed, exit 0',
      r.returncode == 0 and 'pushing snapshot of wal-hot' in r.stdout
      and points_in(os.path.join(HUB, 'held_1758000004__unitA.db')) == 2,
      (r.returncode, r.stdout,
       points_in(os.path.join(HUB, 'held_1758000004__unitA.db'))))
pin.execute('COMMIT')
pin.close()
conn_held.close()
os.unlink(held)
os.unlink(os.path.join(HUB, 'held_1758000004__unitA.db'))

# ── deletes never propagate; own files never come back ──────────────────────
os.unlink(a1)
run_sync('unitA')
check('local delete of OWN study does not resurrect from hub',
      not os.path.exists(a1)
      and points_in(os.path.join(HUB, 'a1_1758000001__unitA.db')) == 3)

# a corrupted local copy of the OTHER unit's file heals from the hub
Path(b_live).write_bytes(b'garbage')
run_sync('unitB')
check("corrupted copy of other unit's study heals from hub",
      points_in(b_live) == 5, points_in(b_live))

# ── legacy studies: seed once, never updated, never parsed as foreign ────────
leg = make_study('unitA', 'legacy_run', n_points=2)
trap = make_study('unitA', 'my__probe_1758', n_points=2)  # '__' in NAME part
r = run_sync('unitA')
check('legacy not pushed without --seed',
      not os.path.exists(os.path.join(HUB, 'legacy_run.db'))
      and not os.path.exists(os.path.join(HUB, 'my__probe_1758.db')),
      sorted(os.listdir(HUB)))
r = run_sync('unitA', '--seed')
check('--seed pushes legacy', r.returncode == 0
      and points_in(os.path.join(HUB, 'legacy_run.db')) == 2
      and points_in(os.path.join(HUB, 'my__probe_1758.db')) == 2,
      (r.returncode, r.stdout, r.stderr))
run_sync('unitB')
bleg = os.path.join(UNITS['unitB']['sdir'], 'legacy_run.db')
btrap = os.path.join(UNITS['unitB']['sdir'], 'my__probe_1758.db')
check('legacy pulled where absent',
      points_in(bleg) == 2 and points_in(btrap) == 2)

# steady state with studies nobody ever flagged on (every legacy one, the
# closed own ones): the restore pass asks the hub for sidecars it does not
# have. GNU rsync is told to skip those silently — the journal shows only
# the script's own lines; openrsync still logs one line per cycle, which
# run_rsync tolerates
r = run_sync('unitA')
if HAVE_IMA:
    check('un-flagged studies: quiet cycle, empty stderr (GNU rsync)',
          r.returncode == 0 and 'cycle ok' in r.stdout and r.stderr == '',
          (r.returncode, r.stdout, r.stderr))
else:
    check('un-flagged studies: cycle ok (openrsync: hub misses tolerated)',
          r.returncode == 0 and 'cycle ok' in r.stdout,
          (r.returncode, r.stdout, r.stderr))

# a pre-sync session that crashed with its wal hot: --seed used to skip it
# without a word. Now it ships a consistent snapshot WITH the wal's rows
# and folds the wal in for later cycles, like the push loop does
wleg = make_study('unitA', 'legacy_walhot', n_points=1)
crash_code = f'''
import os, sys
sys.path.insert(0, {REPO!r})
import serial_daemon as sd
conn = sd.db_open({wleg!r})
sd.insert_rows(conn, [{{'lat': 40.9, 'lon': -119.9, 'heading': 90.0,
                        'fix': 4, 'adc': [5] * 8, 'gps_ts': '5'}}] * 3,
               12.4, 25.0)
os._exit(0)   # no close: -wal holds the 3 rows
'''
subprocess.run([sys.executable, '-c', crash_code], check=True)
assert os.path.getsize(wleg + '-wal') > 0, 'legacy crash sim left no hot wal?'
r = run_sync('unitA', '--seed')
check('--seed ships a wal-hot legacy db as a snapshot WITH its wal rows',
      r.returncode == 0
      and 'seeding snapshot of wal-hot legacy_walhot.db' in r.stdout
      and points_in(os.path.join(HUB, 'legacy_walhot.db')) == 4
      and wal_gone(wleg + '-wal'),
      (r.returncode, r.stdout, r.stderr,
       points_in(os.path.join(HUB, 'legacy_walhot.db'))))
r = run_sync('unitA', '--seed')
check('re-seed after the heal: plain file, nothing re-shipped, no dotfiles on hub',
      r.returncode == 0 and 'wal-hot' not in r.stdout
      and points_in(os.path.join(HUB, 'legacy_walhot.db')) == 4
      and not [n for n in os.listdir(HUB) if n.startswith('.')],
      (r.returncode, r.stdout, sorted(os.listdir(HUB))))

# B tweaks its local copy (view settings); hub's copy also changes. Neither
# side may clobber B's local edit — legacy files are write-once in transit.
conn = sqlite3.connect(bleg)
conn.execute("INSERT OR REPLACE INTO meta VALUES ('sl_offset', '42')")
conn.commit()
conn.close()
conn = sqlite3.connect(os.path.join(HUB, 'my__probe_1758.db'))
conn.execute("INSERT OR REPLACE INTO meta VALUES ('sl_offset', '99')")
conn.commit()
conn.close()
run_sync('unitB')
conn = sqlite3.connect(f'file:{bleg}?mode=ro', uri=True)
v1 = dict(conn.execute('SELECT key,value FROM meta')).get('sl_offset')
conn.close()
conn = sqlite3.connect(f'file:{btrap}?mode=ro', uri=True)
v2 = dict(conn.execute('SELECT key,value FROM meta')).get('sl_offset')
conn.close()
check('legacy local edits survive the pull (no pass-2 update)',
      v1 == '42' and v2 is None, (v1, v2))

# ── locking ──────────────────────────────────────────────────────────────────
lock = os.path.join(UNITS['unitA']['sdir'], '.sync_lock')
os.makedirs(lock)
Path(lock, 'pid').write_text(str(os.getpid()))       # alive → skip
r = run_sync('unitA')
check('live lock respected', r.returncode == 0
      and 'another sync' in r.stdout, (r.returncode, r.stdout))
dead = subprocess.run(['sh', '-c', 'echo $$'],
                      capture_output=True, text=True).stdout.strip()
Path(lock, 'pid').write_text(dead)                   # reaped → steal
r = run_sync('unitA')
check('stale lock stolen', r.returncode == 0
      and 'stealing stale lock' in r.stdout
      and not os.path.exists(lock), (r.returncode, r.stdout))

# lock older than an hour is stolen even if its pid is alive (pid recycling
# after a power cut must not stop sync forever); a young pid-less lock is a
# holder mid-startup, not stale
os.makedirs(lock)
Path(lock, 'pid').write_text(str(os.getpid()))
old = time.time() - 7200
os.utime(lock, (old, old))
r = run_sync('unitA')
check('hour-old lock stolen despite live pid', r.returncode == 0
      and 'stealing stale lock' in r.stdout, (r.returncode, r.stdout))
os.makedirs(lock)                                    # fresh mtime, no pid
r = run_sync('unitA')
check('young pid-less lock respected', r.returncode == 0
      and 'no pid yet' in r.stdout and os.path.exists(lock),
      (r.returncode, r.stdout))
os.rmdir(lock)

# ── 0-point studies stay local (purge fodder must never reach the hub) ───────
zero = make_study('unitA', 'zero_1758000005__unitA', n_points=0)
r = run_sync('unitA')
check('own 0-point closed db not pushed', r.returncode == 0
      and 'skipping 0-point' in r.stdout
      and not os.path.exists(os.path.join(HUB, 'zero_1758000005__unitA.db')),
      (r.returncode, r.stdout))
conn = sd.db_open(zero)
sd.insert_rows(conn, [{'lat': 40.9, 'lon': -119.9, 'heading': 90.0,
                       'fix': 4, 'adc': [5] * 8, 'gps_ts': '5'}] * 2,
               12.4, 25.0)
conn.close()
run_sync('unitA')
check('once it has points it ships',
      points_in(os.path.join(HUB, 'zero_1758000005__unitA.db')) == 2)

zl = make_study('unitA', 'zerolive_1758000006__unitA', n_points=0)
conn_zl = sd.db_open(zl)
Path(UNITS['unitA']['pid']).write_text(f'{os.getpid()}:{zl}')
r = run_sync('unitA')
check('0-point LIVE study not published', r.returncode == 0
      and 'no points yet' in r.stdout and 'not publishing' in r.stdout
      and 'snapshotted' not in r.stdout
      and not os.path.exists(os.path.join(HUB,
                                          'zerolive_1758000006__unitA.db')),
      (r.returncode, r.stdout))
sd.insert_rows(conn_zl, [{'lat': 41.0, 'lon': -120.0, 'heading': 90.0,
                          'fix': 4, 'adc': [6] * 8, 'gps_ts': '6'}],
               12.4, 25.0)
r = run_sync('unitA')
check('first point publishes the live snapshot',
      'snapshotted live study' in r.stdout
      and points_in(os.path.join(HUB, 'zerolive_1758000006__unitA.db')) == 1,
      (r.stdout,))
conn_zl.close()
os.unlink(UNITS['unitA']['pid'])

# a pidfile can exist before the daemon creates the points schema; that
# window reads as points_of = -1 and must be treated like 0, not published
sw = os.path.join(UNITS['unitA']['sdir'], 'sw_1758000011__unitA.db')
conn = sqlite3.connect(sw)
conn.execute('PRAGMA journal_mode=WAL')     # WAL header, no schema yet
conn.close()
Path(UNITS['unitA']['pid']).write_text(f'{os.getpid()}:{sw}')
r = run_sync('unitA')
check('pre-schema live db (unreadable points) not published',
      r.returncode == 0 and 'not publishing' in r.stdout
      and 'snapshotted' not in r.stdout
      and not os.path.exists(os.path.join(HUB, 'sw_1758000011__unitA.db')),
      (r.returncode, r.stdout, r.stderr))
os.unlink(UNITS['unitA']['pid'])
for leftover in Path(UNITS['unitA']['sdir']).glob('sw_1758000011__unitA.db*'):
    leftover.unlink()

# a crash BEFORE the points schema exists (wal-hot, no live pid) must not
# publish either — not the wal-hot snapshot this cycle, and not the plain
# push after heal folds the wal (points_of = -1 both times)
pre = os.path.join(UNITS['unitA']['sdir'], 'pre_1758000012__unitA.db')
Path(pre[:-3] + '.log').write_text('log pre')
crash_code = f'''
import os, sqlite3
conn = sqlite3.connect({pre!r})
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
os._exit(0)   # hot wal, no points table: points_of reads -1
'''
subprocess.run([sys.executable, '-c', crash_code], check=True)
assert os.path.getsize(pre + '-wal') > 0, 'pre-schema sim left no hot wal?'
r = run_sync('unitA')
check('pre-schema wal-hot crash: nothing published (snapshot gate)',
      r.returncode == 0 and 'pushing snapshot' not in r.stdout
      and not os.path.exists(os.path.join(HUB, 'pre_1758000012__unitA.db'))
      and not os.path.exists(os.path.join(HUB, 'pre_1758000012__unitA.log')),
      (r.returncode, r.stdout, sorted(os.listdir(HUB))))
r = run_sync('unitA')
check('pre-schema db stays unpublished after heal folds the wal',
      r.returncode == 0
      and not os.path.exists(os.path.join(HUB, 'pre_1758000012__unitA.db')),
      (r.returncode, r.stdout))
for leftover in Path(UNITS['unitA']['sdir']).glob('pre_1758000012__unitA.*'):
    leftover.unlink()

# a 0-point study's sidecar and log are held back with it, then ship together
zs = make_study('unitA', 'zs_1758000010__unitA', n_points=0)
Path(zs[:-3] + '.log').write_text('log zs')
sidecar('unitA', 'zs_1758000010__unitA', 'unitA',
        [{'id': 1, 'lat': 41.1, 'lon': -120.1, 'created_at': 7.0}])
r = run_sync('unitA')
check('0-point study: sidecar and log held back too', r.returncode == 0
      and '(0-point study)' in r.stdout
      and not os.path.exists(os.path.join(HUB, 'zs_1758000010__unitA.db'))
      and not os.path.exists(os.path.join(
          HUB, 'zs_1758000010__unitA.targets__unitA.json'))
      and not os.path.exists(os.path.join(HUB, 'zs_1758000010__unitA.log')),
      (r.returncode, r.stdout, sorted(os.listdir(HUB))))
r = run_sync('unitA', '--seed')
check("--seed holds back a 0-point study's log too", r.returncode == 0
      and 'seed skipping zs_1758000010__unitA.log (0-point study)' in r.stdout
      and not os.path.exists(os.path.join(HUB, 'zs_1758000010__unitA.log')),
      (r.returncode, r.stdout, sorted(os.listdir(HUB))))
conn = sd.db_open(zs)
sd.insert_rows(conn, [{'lat': 41.2, 'lon': -120.2, 'heading': 90.0,
                       'fix': 4, 'adc': [7] * 8, 'gps_ts': '7'}],
               12.4, 25.0)
conn.close()
run_sync('unitA')
check('with points, db + sidecar + log all ship',
      points_in(os.path.join(HUB, 'zs_1758000010__unitA.db')) == 1
      and os.path.exists(os.path.join(
          HUB, 'zs_1758000010__unitA.targets__unitA.json'))
      and os.path.exists(os.path.join(HUB, 'zs_1758000010__unitA.log')),
      sorted(os.listdir(HUB)))

# ── hub dot-temp files (the other unit's in-flight push) never pulled ────────
tmpf = os.path.join(HUB, '.big_1758000007__unitA.db.Xa12Bc')
Path(tmpf).write_bytes(b'partial transfer junk')
r = run_sync('unitB')
check('hub dot-temp file not pulled', r.returncode == 0
      and not os.path.exists(os.path.join(
          UNITS['unitB']['sdir'], '.big_1758000007__unitA.db.Xa12Bc')),
      (r.returncode, sorted(os.listdir(UNITS['unitB']['sdir']))))
os.unlink(tmpf)

# ── trailing-slash studies_dir still publishes the live snapshot ─────────────
Path(UNITS['unitA']['cfg']).write_text(json.dumps(
    {'studies_dir': UNITS['unitA']['sdir'] + '/', 'sync_hub': HUB}))
sl = make_study('unitA', 'slash_1758000008__unitA', n_points=1)
conn_sl = sd.db_open(sl)
Path(UNITS['unitA']['pid']).write_text(f'{os.getpid()}:{sl}')
r = run_sync('unitA')
check('trailing-slash studies_dir: live snapshot still publishes',
      'snapshotted live study' in r.stdout
      and points_in(os.path.join(HUB, 'slash_1758000008__unitA.db')) == 1,
      (r.returncode, r.stdout))
conn_sl.close()
os.unlink(UNITS['unitA']['pid'])
Path(UNITS['unitA']['cfg']).write_text(json.dumps(
    {'studies_dir': UNITS['unitA']['sdir'], 'sync_hub': HUB}))

# ── stale journal beside a FOREIGN db: healed, then the pull proceeds ────────
# (a crashed non-owner settings write must not let pass 2 swap the db file
# out from under a hot -wal — that corrupts the copy)
b_a1 = os.path.join(UNITS['unitB']['sdir'], 'a1_1758000001__unitA.db')
crash_code = f'''
import os, sys
sys.path.insert(0, {REPO!r})
import serial_daemon as sd
conn = sd.db_open({b_a1!r})
sd.meta_set(conn, 'sl_offset', '77')
os._exit(0)   # hot -wal left beside the FOREIGN copy
'''
subprocess.run([sys.executable, '-c', crash_code], check=True)
assert os.path.getsize(b_a1 + '-wal') > 0, 'foreign crash sim left no wal?'
conn = sd.db_open(os.path.join(HUB, 'a1_1758000001__unitA.db'))
sd.meta_set(conn, 'study_name', 'renamed by owner')    # owner-side update
conn.close()
r = run_sync('unitB')
conn = sqlite3.connect(f'file:{b_a1}?mode=ro', uri=True)
name = dict(conn.execute('SELECT key,value FROM meta')).get('study_name')
conn.close()
check('stale foreign -wal healed, owner update pulled clean',
      r.returncode == 0 and wal_gone(b_a1 + '-wal')
      and points_in(b_a1) == 3 and name == 'renamed by owner',
      (r.returncode, r.stdout, name, points_in(b_a1)))

# a foreign db whose wal a live local connection pins: skipped, quietly
conn_hold = sd.db_open(b_a1)
sd.meta_set(conn_hold, 'sl_offset', '88')             # frames in the wal
pin = sqlite3.connect(b_a1, isolation_level=None)
pin.execute('BEGIN')
pin.execute('SELECT COUNT(*) FROM meta').fetchone()   # pins them
conn = sd.db_open(os.path.join(HUB, 'a1_1758000001__unitA.db'))
sd.meta_set(conn, 'study_name', 'renamed again')
conn.close()
r = run_sync('unitB')
conn = sqlite3.connect(f'file:{b_a1}?mode=ro', uri=True)
name = dict(conn.execute('SELECT key,value FROM meta')).get('study_name')
conn.close()
check('journal-hot foreign db skipped this cycle', r.returncode == 0
      and 'journal-hot foreign' in r.stdout and name != 'renamed again',
      (r.returncode, r.stdout, name))
pin.execute('COMMIT')
pin.close()
conn_hold.close()
run_sync('unitB')                                     # then it heals

# ── lost own sidecar restored from hub; a present one never clobbered ────────
own_sc = os.path.join(UNITS['unitA']['sdir'],
                      'crash_1758000003__unitA.targets__unitA.json')
sidecar('unitA', 'crash_1758000003__unitA', 'unitA',
        [{'id': 1, 'lat': 40.2, 'lon': -119.2, 'created_at': 1.0},
         {'id': 2, 'lat': 40.3, 'lon': -119.3, 'created_at': 2.0}])
run_sync('unitA')                                     # hub has the 2-target copy
os.unlink(own_sc)
r = run_sync('unitA')
check('LOST own sidecar (study still present) restored from hub',
      os.path.exists(own_sc)
      and len(json.loads(Path(own_sc).read_text())['targets']) == 2,
      (r.returncode, r.stdout, sorted(os.listdir(UNITS['unitA']['sdir']))))
Path(os.path.join(HUB, 'crash_1758000003__unitA.targets__unitA.json')) \
    .write_text(json.dumps({'version': 1, 'study': 'crash_1758000003__unitA',
                            'host': 'unitA', 'seq': 9,
                            'targets': [], 'cleared': {}}))
run_sync('unitA')
check('present own sidecar never clobbered by a divergent hub copy',
      len(json.loads(Path(own_sc).read_text())['targets']) == 2
      and len(json.loads(Path(os.path.join(
          HUB, 'crash_1758000003__unitA.targets__unitA.json'))
          .read_text())['targets']) == 2,          # own push re-asserted it
      Path(own_sc).read_text())

# ── own log / sidecar deletes stay deleted (pass-1 excludes + restore gate) ──
alog = os.path.join(UNITS['unitA']['sdir'], 'a1_1758000001__unitA.log')
a1_sc = os.path.join(UNITS['unitA']['sdir'],
                     'a1_1758000001__unitA.targets__unitA.json')
os.unlink(alog)
os.unlink(a1_sc)      # its study db is ALSO gone — restore pass must not act
run_sync('unitA')
check('own log delete does not resurrect from hub',
      not os.path.exists(alog)
      and os.path.exists(os.path.join(HUB, 'a1_1758000001__unitA.log')))
check('own sidecar of a DELETED study does not resurrect',
      not os.path.exists(a1_sc)
      and os.path.exists(os.path.join(
          HUB, 'a1_1758000001__unitA.targets__unitA.json')))

# ── files listed for pass 2 but pruned from the hub: tolerated, exit 0 ───────
os.unlink(os.path.join(HUB, 'a1_1758000001__unitA.targets__unitB.json'))
r = run_sync('unitA')       # A still holds B's sidecar → pass 2 lists it
check('hub-pruned foreign file: cycle still exits 0', r.returncode == 0
      and 'cycle ok' in r.stdout, (r.returncode, r.stdout, r.stderr))

# ── own sidecar on a FOREIGN study: restored if lost, quarantined if torn ────
run_sync('unitB')            # B re-pushes its sidecar after the prune above
b_sc = os.path.join(UNITS['unitB']['sdir'],
                    'a1_1758000001__unitA.targets__unitB.json')
hub_sc = os.path.join(HUB, 'a1_1758000001__unitA.targets__unitB.json')
check("B's sidecar re-pushed after a hub prune",
      len(json.loads(Path(hub_sc).read_text())['targets']) == 2,
      sorted(os.listdir(HUB)))
os.unlink(b_sc)
r = run_sync('unitB')
check('own sidecar on a FOREIGN study restored from hub',
      os.path.exists(b_sc)
      and len(json.loads(Path(b_sc).read_text())['targets']) == 2,
      (r.returncode, r.stdout, sorted(os.listdir(UNITS['unitB']['sdir']))))
Path(b_sc).write_text('{"torn')                       # corrupt, but present
r = run_sync('unitB')
check('corrupt own sidecar: quarantined, never pushed, restored',
      r.returncode == 0 and 'skipping corrupt sidecar' in r.stdout
      and os.path.exists(b_sc + '.corrupt')
      and json.loads(Path(b_sc).read_text())['host'] == 'unitB'
      and len(json.loads(Path(hub_sc).read_text())['targets']) == 2,
      (r.returncode, r.stdout, sorted(os.listdir(UNITS['unitB']['sdir']))))
os.unlink(b_sc + '.corrupt')

# hub copy corrupt TOO: never accepted (else it ping-pongs through
# quarantine every 30 s), and the FIRST .corrupt — the only bytes with any
# chance of hand recovery — is never clobbered by later corrupt arrivals
Path(b_sc).write_text('{"torn A')
hub_good = Path(hub_sc).read_text()
Path(hub_sc).write_text('{"torn hub')
r = run_sync('unitB')
check('corrupt hub copy dropped, not accepted', r.returncode == 0
      and 'corrupt too' in r.stdout and not os.path.exists(b_sc)
      and Path(b_sc + '.corrupt').read_text() == '{"torn A',
      (r.returncode, r.stdout))
r = run_sync('unitB')
check('no quarantine ping-pong: forensic copy survives later cycles',
      r.returncode == 0 and not os.path.exists(b_sc)
      and Path(b_sc + '.corrupt').read_text() == '{"torn A',
      (r.returncode, r.stdout))
Path(hub_sc).write_text(hub_good)             # hand-fix on the hub
run_sync('unitB')
check('hand-fixed hub copy restores normally',
      os.path.exists(b_sc)
      and len(json.loads(Path(b_sc).read_text())['targets']) == 2,
      sorted(os.listdir(UNITS['unitB']['sdir'])))
os.unlink(b_sc + '.corrupt')

# a study legally named 'readonly...' must not un-tolerate the routine
# missing-hub-file rc 23: rsync embeds the filename in its no-such-file
# stderr lines, and the error sniff must not read it as a real failure
ro = make_study('unitB', 'readonly-cal_1758000013__mm9', n_points=1)
r = run_sync('unitB')
check("study named 'readonly...': missing-hub-file rc 23 still tolerated",
      r.returncode == 0 and 'cycle ok' in r.stdout,
      (r.returncode, r.stdout, r.stderr))
for leftover in Path(UNITS['unitB']['sdir']).glob(
        'readonly-cal_1758000013__mm9.db*'):
    leftover.unlink()

# ── real rsync:// daemon transport (the field configuration) ─────────────────
port = 18000 + os.getpid() % 2000
mod_dir = os.path.join(SCRATCH, 'hub_daemon')
os.makedirs(mod_dir)
dconf = os.path.join(SCRATCH, 'rsyncd.conf')
Path(dconf).write_text(f'port = {port}\nuse chroot = no\n'
                       f'pid file = {os.path.join(SCRATCH, "rsyncd.pid")}\n'
                       f'\n[mm]\n    path = {mod_dir}\n'
                       f'    read only = false\n')
daemon = subprocess.Popen(['rsync', '--daemon', '--no-detach',
                           f'--config={dconf}'],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
try:
    import socket
    up = False
    for _ in range(50):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.2).close()
            up = True
            break
        except OSError:
            time.sleep(0.1)
    if not up:
        print('  skip daemon-transport checks (no rsync daemon on this host)')
    else:
        # fresh unit so the daemon test is self-contained: one own study
        # with its sidecar, one foreign study pre-placed in the module
        dsdir = os.path.join(SCRATCH, 'unitD', 'studies')
        os.makedirs(dsdir)
        UNITS['unitD'] = {'sdir': dsdir,
                          'cfg': os.path.join(SCRATCH, 'unitD',
                                              'config.json'),
                          'pid': os.path.join(SCRATCH, 'unitD',
                                              'daemon.pid')}
        Path(UNITS['unitD']['cfg']).write_text(json.dumps(
            {'studies_dir': dsdir,
             'sync_hub': f'rsync://127.0.0.1:{port}/mm'}))
        make_study('unitD', 'd1_1758000009__unitD', n_points=2)
        sidecar('unitD', 'd1_1758000009__unitD', 'unitD',
                [{'id': 1, 'lat': 40.0, 'lon': -119.0, 'created_at': 1.0}])
        # a second own study nobody flagged on: its sidecar is a hub miss
        # for the restore pass over the daemon — the field's noise case
        make_study('unitD', 'd2_1758000014__unitD', n_points=1)
        shutil.copy(os.path.join(HUB, 'a1_1758000001__unitA.db'),
                    os.path.join(mod_dir, 'a1_1758000001__unitA.db'))
        r = run_sync('unitD')
        pushed = (points_in(os.path.join(
                      mod_dir, 'd1_1758000009__unitD.db')) == 2
                  and os.path.exists(os.path.join(
                      mod_dir,
                      'd1_1758000009__unitD.targets__unitD.json')))
        pulled = points_in(os.path.join(
            dsdir, 'a1_1758000001__unitA.db')) == 3
        openrsync = 'openrsync' in subprocess.run(
            ['rsync', '--version'],
            capture_output=True, text=True).stdout.lower()
        if openrsync:
            # openrsync's daemon (Mac bench) can't serve --files-from, so
            # pass 2 / the restore pass warn here; the field hub is GNU
            # rsyncd on the Pi. Verify the legs it can do.
            check('rsync:// daemon transport: push + pull pass 1 work '
                  '(openrsync hub: files-from legs untestable)',
                  pushed and pulled,
                  (r.returncode, r.stdout, r.stderr,
                   sorted(os.listdir(mod_dir))))
        else:
            # GNU daemon (the field): the missing sidecar must not print a
            # 'link_stat ... No such file' line — that was the per-cycle
            # journal noise on the rover
            check('rsync:// daemon transport: full cycle ok, empty stderr',
                  r.returncode == 0 and 'cycle ok' in r.stdout
                  and pushed and pulled and r.stderr == '',
                  (r.returncode, r.stdout, r.stderr,
                   sorted(os.listdir(mod_dir))))
finally:
    daemon.terminate()
    daemon.wait(timeout=10)

# ── verdict ──────────────────────────────────────────────────────────────────
print()
if FAILED:
    print(f'{len(FAILED)} FAILURE(S): {FAILED}')
    print(f'scratch tree kept for inspection: {SCRATCH}')
    sys.exit(1)
shutil.rmtree(SCRATCH, ignore_errors=True)
print('ALL PASS')
