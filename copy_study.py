#!/usr/bin/env python3
"""
copy_study.py — duplicate a study under a new name.

    copy_study.py SRC NEWNAME [--host TAG] [--studies-dir DIR] [--no-targets]

SRC is a study id (as shown in the map URL) or a path to its .db. The copy
gets a fresh id in the daemon's own convention, '<name>_<epoch>__<host>',
and its human label (meta 'study_name' — what the landing page lists) is set
to NEWNAME. Everything else in the db is left alone; created_at still says
when the data was surveyed, and meta 'copied_from' / 'copied_at' record the
provenance.

The copy is taken with SQLite's online backup, so a study whose -wal file
holds un-checkpointed pages — every study the daemon has ever written, and
one it is writing right now — comes across whole; a plain cp of the .db
would silently drop the WAL tail.

Dig-target sidecars ('<study>.targets__<tag>.json') travel with the study
unless --no-targets; their host tags are kept, so ownership on any unit is
exactly what it was for the original.

--host stamps another unit's tag as the owner: a copy made here and then
scp'd into that unit's studies/ is then that unit's own study (editable
there, pushed by its sync) rather than a read-only foreign one.
"""
import argparse
import json
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path

CONFIG_FILE = Path.home() / 'metal_mapper' / 'config.json'
DEFAULT_STUDIES_DIR = Path.home() / 'metal_mapper' / 'studies'
TAG_RE = re.compile(r'[A-Za-z0-9-]+')


def host_tag():
    """Same derivation as db_map.HOST_TAG and sync_studies.sh (keep in step)."""
    return (os.environ.get('MM_HOST_TAG')
            or re.sub(r'[^A-Za-z0-9-]', '-', socket.gethostname().split('.')[0])
            or 'unit')


def config_studies_dir():
    try:
        d = json.loads(CONFIG_FILE.read_text()).get('studies_dir')
        if isinstance(d, str) and d:
            return Path(d)
    except Exception:
        pass
    return DEFAULT_STUDIES_DIR


def new_study_id(name, host, now):
    """Mirror of db_map.new_study_id(): ASCII id that is also the filename."""
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', name).strip('_-')
    return f'{safe or "study"}_{int(now)}__{host}'


def resolve_src(src, studies_dir):
    p = Path(src)
    if p.suffix == '.db' or p.exists():
        if not p.is_file():
            sys.exit(f'source not found: {p}')
        return p.resolve()
    p = studies_dir / f'{src}.db'
    if not p.is_file():
        sys.exit(f'no study {src!r} in {studies_dir} (and {src!r} is not a file)')
    return p.resolve()


def copy_study(src_db, new_name, host, dst_dir, with_targets=True, now=None):
    now = time.time() if now is None else now
    src_db = Path(src_db)
    src_id = src_db.stem
    if not TAG_RE.fullmatch(host):
        raise SystemExit(f'host tag must match [A-Za-z0-9-]+, got {host!r}')
    if not new_name.strip():
        raise SystemExit('NEWNAME must not be empty')
    dst_id = new_study_id(new_name, host, now)
    dst_db = dst_dir / f'{dst_id}.db'
    if dst_db.exists() or dst_id == src_id:
        raise SystemExit(f'refusing to overwrite {dst_db}')
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Read-only URI open + online backup: a consistent snapshot that includes
    # the WAL, taken without ever writing to the source.
    src = sqlite3.connect(f'file:{src_db}?mode=ro', uri=True)
    src.execute('PRAGMA busy_timeout=5000')
    try:
        # sanity: it must be one of ours before we mint an id for it
        tables = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'points', 'meta'} <= tables:
            raise SystemExit(f'{src_db} is not a study db (no points/meta table)')
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    dst = sqlite3.connect(str(dst_db))
    try:
        dst.execute('PRAGMA journal_mode=WAL')      # what the daemon and viewer expect
        for k, v in (('study_name', new_name.strip()),
                     ('copied_from', src_id),
                     ('copied_at', f'{now:.6f}')):
            dst.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (k, v))
        dst.commit()
        n = dst.execute('SELECT COUNT(*) FROM points').fetchone()[0]
    finally:
        dst.close()

    copied = []
    if with_targets:
        for sc in sorted(src_db.parent.glob(f'{src_id}.targets__*.json')):
            tag = sc.name[len(src_id) + len('.targets__'):-len('.json')]
            if not TAG_RE.fullmatch(tag):
                continue
            out = dst_dir / f'{dst_id}.targets__{tag}.json'
            try:
                d = json.loads(sc.read_text())
            except Exception:
                print(f'skipping unreadable sidecar {sc.name}', file=sys.stderr)
                continue
            if isinstance(d, dict):
                d['study'] = dst_id          # the id it now belongs to
            out.write_text(json.dumps(d, indent=1))
            copied.append(out)
    return dst_id, dst_db, n, copied


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog='\n\n'.join(__doc__.split('\n\n')[2:]))
    ap.add_argument('src', metavar='SRC', help='study id or path to its .db')
    ap.add_argument('name', metavar='NEWNAME', help='new study name (the listed label)')
    ap.add_argument('--host', default=None, metavar='TAG',
                    help='owner tag for the copy (default: this machine, %(default)s)')
    ap.add_argument('--studies-dir', default=None, metavar='DIR',
                    help='where to put the copy (default: next to SRC when SRC is a '
                         'path, else the configured studies dir)')
    ap.add_argument('--no-targets', action='store_true',
                    help="don't copy the study's *.targets__*.json sidecars")
    ap.set_defaults(host=host_tag())
    a = ap.parse_args()

    cfg_dir = config_studies_dir()
    src_db = resolve_src(a.src, cfg_dir)
    dst_dir = Path(a.studies_dir).expanduser().resolve() if a.studies_dir else src_db.parent
    dst_id, dst_db, n, copied = copy_study(src_db, a.name, a.host, dst_dir,
                                           with_targets=not a.no_targets)
    print(f'{dst_id}\n  {dst_db}  ({n} points)')
    for p in copied:
        print(f'  {p}')


if __name__ == '__main__':
    main()
