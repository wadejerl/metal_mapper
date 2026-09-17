#!/bin/bash
# sync_studies.sh — two-way studies sync between this head unit and the hub
# (the RTK base station Pi). Run by jlw_mm_sync.timer every 30 s; safe to
# run by hand. Reads studies_dir and sync_hub from ~/metal_mapper/config.json
# (sync_hub empty = disabled, exits 0).
#
#   ./sync_studies.sh           normal cycle
#   ./sync_studies.sh --seed    one-time bootstrap: also push legacy studies
#                               (ids without a '__<host>' tag) to the hub
#
# The whole scheme rests on one invariant, enforced here and in db_map.py:
# EVERY SYNCED FILE HAS EXACTLY ONE WRITING UNIT, so rsync never merges.
#   - '<name>_<epoch>__<host>.db'          written only by <host>
#   - '<study>.targets__<host>.json'       written only by <host>
#   - legacy files (no tag)                write-once: seeded to the hub,
#                                          pulled if absent, never updated
# Consequences:
#   - push: only files tagged with OUR host (plus a consistent snapshot of
#     the live study — never the hot WAL db itself)
#   - pull: everything not ours; files tagged with another host also update
#     in place (their owner is authoritative), legacy files never do
#   - no --delete anywhere: a deletion never propagates. Removing a study
#     for good = delete it on the OWNING unit first, then the hub (a hub-
#     only delete resurrects from the owner's next push), then peers;
#     local deletes of foreign/legacy files resurrect on the next pull.
#
# Test hooks: MM_CONFIG_FILE, MM_PID_FILE, MM_HOST_TAG (must match db_map's
# HOST_TAG derivation), and a local directory path works as sync_hub.
set -euo pipefail

CONFIG="${MM_CONFIG_FILE:-$HOME/metal_mapper/config.json}"
PIDFILE="${MM_PID_FILE:-/tmp/metal_detector_daemon.pid}"
SEED=0
[ "${1:-}" = "--seed" ] && SEED=1

note() { echo "[sync] $*"; }
FAIL=0
warn() { echo "[sync] WARN: $*" >&2; FAIL=1; }

# ── config ────────────────────────────────────────────────────────────────
# python3 is already a hard dependency of the head unit (the app itself);
# it also does the sqlite work below.
eval "$(python3 - "$CONFIG" <<'PY'
import json, os, shlex, sys
try:
    cfg = json.load(open(sys.argv[1]))
    if not isinstance(cfg, dict):
        cfg = {}
except Exception:
    cfg = {}
sdir = str(cfg.get('studies_dir') or '')
if sdir:
    # normalize like db_map does (Path/abspath): the db path in the pid
    # file is always absolute+normalized, and the live-study match below
    # compares against SDIR — a hand-edited trailing slash or ~ must not
    # silently disable live-snapshot publishing. NOTE: no apostrophes in
    # this heredoc — it sits inside $( ) and bash 3.2 (Mac) mis-parses.
    sdir = os.path.abspath(os.path.expanduser(sdir))
print('SDIR=%s' % shlex.quote(sdir))
print('HUB=%s' % shlex.quote(str(cfg.get('sync_hub') or '')))
PY
)"
[ -n "$SDIR" ] || SDIR="$HOME/metal_mapper/studies"
if [ -z "$HUB" ]; then
    note "sync_hub not configured in $CONFIG — nothing to do"
    exit 0
fi
[ -d "$SDIR" ] || { note "no studies dir at $SDIR — nothing to do"; exit 0; }

# Host tag: same derivation as db_map.py's HOST_TAG (keep in step!) —
# short hostname, every char outside [A-Za-z0-9-] becomes '-'.
H="${MM_HOST_TAG:-$( (hostname -s 2>/dev/null || true) \
                     | sed -E 's/[^A-Za-z0-9-]/-/g')}"
[ -n "$H" ] || H=unit

# Ownership parse, same rule as db_map.study_owner(): the token after the
# LAST '__' is a host tag only if it contains no '_' at all.
owner_of() {  # $1 = filename stem (no extension); prints tag or nothing
    local stem="$1" tail
    case "$stem" in *__*) tail="${stem##*__}" ;; *) return 0 ;; esac
    [[ "$tail" =~ ^[A-Za-z0-9-]+$ ]] && printf '%s' "$tail"
    return 0
}

# ── single instance ───────────────────────────────────────────────────────
# mkdir lock (no flock(1) on the Mac bench). A dead holder's lock is stolen;
# so is ANY lock older than an hour — after a power cut the recorded pid can
# get recycled onto some unrelated live process, and without the age cap
# that would silently stop sync until the next reboot.
LOCK="$SDIR/.sync_lock"
lock_age() {
    python3 -c 'import os, sys, time
try:
    print(int(time.time() - os.stat(sys.argv[1]).st_mtime))
except Exception:
    print(0)' "$LOCK" 2>/dev/null || echo 0
}
if ! mkdir "$LOCK" 2>/dev/null; then
    oldpid="$(cat "$LOCK/pid" 2>/dev/null || true)"
    age="$(lock_age)"
    if [ -z "$oldpid" ] && [ "$age" -lt 60 ]; then
        # between the holder's mkdir and its pid write — young, not stale
        note "lock exists with no pid yet (holder starting?) — skipping"
        exit 0
    fi
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null \
            && [ "$age" -lt 3600 ]; then
        note "another sync (pid $oldpid) is running — skipping"
        exit 0
    fi
    note "stealing stale lock (pid ${oldpid:-unknown}, ${age}s old)"
    rm -rf "$LOCK"
    if ! mkdir "$LOCK" 2>/dev/null; then
        note "lost the steal race to another sync — skipping"
        exit 0
    fi
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# --contimeout bounds the rsync:// TCP connect; --timeout alone only starts
# once the connection is up, so a blackholed hub (wedged rsyncd, AP proxy-
# arping for a dead Pi) would stall every cycle for the kernel's full
# SYN-retry window (~2 min). Not every rsync knows the flag (macOS ships
# openrsync), so probe for support.
CONTIMEOUT=()
if [[ "$HUB" == rsync://* ]] \
        && rsync --contimeout=10 --version >/dev/null 2>&1; then
    CONTIMEOUT=(--contimeout=10)
fi

# ── hub reachable? ────────────────────────────────────────────────────────
# The base Pi is off/away whenever the unit is home-benched — that must be
# a quiet no-op, not a red 'failed' unit every 30 s.
if ! rsync --timeout=10 ${CONTIMEOUT[@]+"${CONTIMEOUT[@]}"} "$HUB/" \
        >/dev/null 2>&1; then
    note "hub $HUB unreachable — skipping this cycle"
    exit 0
fi

# ── sqlite helpers ────────────────────────────────────────────────────────
# NOTE on -wal siblings: ANY read of a WAL-header db can materialize a
# 0-byte -wal and a -shm next to it (and some sqlite builds persist them
# after close), so every hot-journal gate below tests -s (non-empty), never
# mere existence — else a study the web UI ever viewed would sync-skip
# forever.
points_of() {  # $1 = db; prints its points row count, or -1 if unreadable
python3 - "$1" <<'PY'
import sqlite3, sys
def cnt(uri):
    conn = sqlite3.connect(uri, uri=True)
    try:
        return conn.execute('SELECT COUNT(*) FROM points').fetchone()[0]
    finally:
        conn.close()
p = sys.argv[1]
try:
    try:
        print(cnt(f'file:{p}?mode=ro'))
    except sqlite3.OperationalError:
        # a cleanly-closed WAL db on builds that refuse plain ro without a
        # -shm (Apple). immutable is safe here: no shm means no live writer.
        print(cnt(f'file:{p}?mode=ro&immutable=1'))
except Exception:
    print(-1)
PY
}

heal_db() {  # $1 = db with a stale -wal/-journal next to it: checkpoint /
             # roll back so the siblings resolve into the main file. Safe
             # against a genuinely live writer (sqlite locking) — then the
             # wal simply survives the attempt and the caller keeps skipping.
python3 - "$1" <<'PY' >/dev/null 2>&1
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=2)
try:
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
finally:
    conn.close()
PY
}

snapshot_db() {  # $1 = src db, $2 = dst: a consistent VACUUM INTO copy —
                 # transactional, reads through any wal, and never blocks
                 # (or tears against) a live writer.
python3 - "$1" "$2" <<'PY'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
if os.path.exists(dst):
    os.unlink(dst)
conn = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
try:
    conn.execute('VACUUM INTO ?', (dst,))
finally:
    conn.close()
PY
}

sidecar_ok() {  # $1 = sidecar json: parseable dict? A corrupt sidecar must
                # neither push (it would replace the hub's good copy) nor
                # sit in place (it would block its own restore).
python3 - "$1" <<'PY'
import json, sys
try:
    ok = isinstance(json.load(open(sys.argv[1])), dict)
except Exception:
    ok = False
sys.exit(0 if ok else 1)
PY
}

no_points() {  # $1 = db: no publishable points — 0 rows OR unreadable (-1:
               # schema not built yet, torn at creation, SD rot). Either is
               # the phantom class that must never reach the hub, where this
               # unit's purge can't follow it.
    local np
    np="$(points_of "$1")"
    [ "$np" = "0" ] || [ "$np" = "-1" ]
}

# ── snapshot the live study ───────────────────────────────────────────────
# The recording db is WAL-hot (db + -wal + -shm, ~20 commits/s): rsyncing
# it raw would ship a torn file. VACUUM INTO takes a consistent read
# snapshot WITHOUT blocking the writer, and its output is a plain single
# file — that is what the hub (and from there the other unit) sees grow.
STAGE="$SDIR/.sync_stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
shopt -s nullglob

# Validate every own sidecar ONCE per cycle, in ONE python: a fresh
# interpreter per file per pass (~0.2 s each on the Pi) would eat the 30 s
# cycle budget over a season of flagged studies. Both the push loop and the
# restore pass consult this list; the rare corrupt path re-checks fresh.
SC_BAD="$STAGE/.sc_bad"
python3 - "$SDIR"/*.targets__"$H".json > "$SC_BAD" <<'PY'
import json, os, sys
for p in sys.argv[1:]:
    try:
        ok = isinstance(json.load(open(p)), dict)
    except Exception:
        ok = False
    if not ok:
        print(os.path.basename(p))
PY
sidecar_bad() {  # $1 = sidecar basename: was it corrupt at cycle start?
    grep -qxF "$1" "$SC_BAD" 2>/dev/null
}

LIVE=""
if [ -r "$PIDFILE" ]; then
    pidline="$(cat "$PIDFILE" 2>/dev/null || true)"
    pid="${pidline%%:*}"
    livedb="${pidline#*:}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null \
            && [[ "$livedb" == "$SDIR"/*.db ]]; then   # raw db lives in /tmp
        LIVE="$(basename "$livedb")"
        np="$(points_of "$livedb")"
        if [ "$np" = "0" ] || [ "$np" = "-1" ]; then
            # 0 = RTK gate not passed yet (routinely minutes); -1 = the
            # daemon claimed the pidfile but has not built the schema yet
            # (ms window at start), or the db is unreadable. Either way,
            # don't publish: an aborted start would replicate to the hub
            # and peer, where THIS unit's purge can never reach it — an
            # immortal phantom study.
            note "live study $LIVE has no points yet — not publishing"
        elif snapshot_db "$livedb" "$STAGE/$LIVE"; then
            note "snapshotted live study $LIVE"
        else
            warn "live snapshot of $LIVE failed"
            rm -f "$STAGE/$LIVE"
        fi
    fi
fi

RSYNC=(rsync -a --timeout=30 ${CONTIMEOUT[@]+"${CONTIMEOUT[@]}"})

run_rsync() {
    # rc 23/24 (file vanished / partial transfer) are expected races on a
    # live tree — a purge unlinking mid-push, the hub's temp file of the
    # other unit's in-flight push vanishing mid-pull — not failures. But
    # rc 23 also covers persistent per-file errors (EACCES, I/O, full
    # disk); those must still turn the unit red, so sniff stderr for them.
    # Sniff AFTER dropping the tolerated missing/vanished lines: rsync
    # embeds filenames in every per-file message, and a study legally
    # named 'readonly_cal' inside a routine no-such-file line must not
    # paint the unit red every 30 s.
    local errf="$STAGE/.rsync_err" rc=0
    "${RSYNC[@]}" "$@" 2>"$errf" || rc=$?
    [ -s "$errf" ] && cat "$errf" >&2
    grep -viE 'no such file or directory|vanished' "$errf" \
        > "$errf.hard" 2>/dev/null || true
    if [ "$rc" != 0 ] && { [ "$rc" = 23 ] || [ "$rc" = 24 ]; } \
            && ! grep -qiE 'permission denied|read-?only file ?system|no space|i/o error|input/output error' \
                 "$errf.hard"; then
        rc=0
    fi
    rm -f "$errf" "$errf.hard"
    return "$rc"
}

# ── push: the live snapshot, then everything tagged with OUR host ─────────
if [ -n "$(ls -A "$STAGE" 2>/dev/null)" ]; then
    "${RSYNC[@]}" "$STAGE"/ "$HUB"/ || warn "push of live snapshot failed"
fi

push=()
for f in "$SDIR"/*__"$H".db; do
    base="$(basename "$f")"
    # the hot db itself never ships — its snapshot just did
    [ "$base" = "$LIVE" ] && continue
    # a NON-EMPTY -wal on a db with no live daemon claim = crashed session
    # (its wal may hold ALL the points; nothing else ever reopens an old
    # id) — or a live writer whose pidfile was lost. heal_db folds a
    # crashed wal into the main file so later cycles push plainly, but
    # NEVER raw-push this cycle: a secretly-live writer commits again
    # right after any emptiness re-test and an autocheckpoint would
    # rewrite pages mid-transfer. A VACUUM INTO snapshot is consistent
    # no matter who is writing. Notes, not warnings: these states can
    # linger and must not paint the timer unit red every 30 s.
    if [ -s "$f-wal" ]; then
        heal_db "$f" || true
        if snapshot_db "$f" "$STAGE/$base"; then
            if no_points "$STAGE/$base"; then
                # counted on the snapshot, which reads through the wal —
                # the raw main file could show 0 while the wal holds rows
                note "skipping 0-point wal-hot $base (purge fodder)"
                rm -f "$STAGE/$base"
            else
                note "pushing snapshot of wal-hot $base"
                push+=("$STAGE/$base")
            fi
        else
            note "skipping wal-hot $base (unreadable)"
            rm -f "$STAGE/$base"
        fi
        continue
    fi
    # own 0-point dbs stay local: purge takes them at the next boot, but a
    # copy already on the hub would outlive it there forever. Size-gated
    # so a season of big closed studies isn't re-counted every 30 s.
    if [ "$(wc -c < "$f")" -lt 262144 ] && no_points "$f"; then
        note "skipping 0-point $base (purge fodder)"
        continue
    fi
    push+=("$f")
done
for f in "$SDIR"/*.targets__"$H".json "$SDIR"/*__"$H".log; do
    base="$(basename "$f")"
    case "$base" in
        *.targets__"$H".json)
            stem="${base%.targets__"$H".json}"
            if sidecar_bad "$base"; then
                # never let a corrupt sidecar replace the hub's good copy;
                # the restore pass below sets it aside and re-pulls
                note "skipping corrupt sidecar $base"
                continue
            fi
            ;;
        *)  stem="${base%.log}" ;;
    esac
    # sidecars and logs obey the same phantom gate as their study db: a
    # 0-point study's files must not outlive the purge on the hub
    db="$SDIR/$stem.db"
    if [ ! -e "$db" ]; then
        note "skipping orphan $base (no local study db)"
        continue
    fi
    if [ "$(wc -c < "$db")" -lt 262144 ] && no_points "$db"; then
        note "skipping $base (0-point study)"
        continue
    fi
    push+=("$f")
done
if [ ${#push[@]} -gt 0 ]; then
    run_rsync "${push[@]}" "$HUB"/ || warn "push of own files failed"
fi

# --seed: bootstrap pre-sync history onto the hub. --ignore-existing means
# a re-seed (or a seed from the second unit) can never clobber anything.
if [ "$SEED" = 1 ]; then
    seedlist=()
    for f in "$SDIR"/*.db "$SDIR"/*.log; do
        base="$(basename "$f")"
        [ "$base" = "$LIVE" ] && continue
        [ -s "$f-wal" ] && [ "${f##*.}" = "db" ] && continue
        # same phantom logic as the push loop: purge fodder stays local
        if [ "${f##*.}" = "db" ] && [ "$(wc -c < "$f")" -lt 262144 ] \
                && no_points "$f"; then
            note "seed skipping 0-point $base"
            continue
        fi
        seedlist+=("$f")
    done
    if [ ${#seedlist[@]} -gt 0 ]; then
        note "seeding ${#seedlist[@]} file(s) to the hub"
        run_rsync --ignore-existing "${seedlist[@]}" "$HUB"/ \
            || warn "seed push failed"
    fi
fi

# ── pull pass 1: anything we don't have yet (never ours, never WAL) ───────
# --exclude='.*' keeps out the hub rsyncd's dot-temp files of the OTHER
# unit's in-flight push ('.<name>.db.XXXXXX') — pulling one would plant a
# hidden torn db that no glob ever sees again. If it vanishes mid-transfer
# instead, that's the rc 23/24 run_rsync tolerates.
run_rsync --ignore-existing \
    --exclude=".*" \
    --exclude="*__$H.db" --exclude="*.targets__$H.json" \
    --exclude="*__$H.log" \
    --exclude="*-wal" --exclude="*-shm" \
    "$HUB"/ "$SDIR"/ || warn "pull of new files failed"

# ── pull pass 1b: restore a LOST or CORRUPT own sidecar ───────────────────
# Own files are excluded above so a study deleted here stays deleted. But
# an own sidecar MISSING while its study db is still present was lost, not
# deleted (SD card restored from a clone, hand cleanup): left alone, this
# unit would restart seq at 1 and hand out 'host:N' keys that other units'
# stale found-marks still point at. Covers our sidecars on EVERY study we
# still hold — own, foreign (the whole point of cross-flagging), legacy. A
# present-but-unparseable sidecar is set aside first, since it would block
# its own repair (push already refused to ship it). --ignore-existing
# means a healthy sidecar is never touched; entries the hub doesn't have
# are the tolerated rc 23.
restore="$STAGE/.restorelist"
: > "$restore"
for f in "$SDIR"/*.db; do
    sc="$(basename "${f%.db}").targets__$H.json"
    if [ -e "$SDIR/$sc" ] && sidecar_bad "$sc"; then
        # re-check RIGHT before acting: db_map may have atomically swapped
        # in a fresh valid sidecar (an operator flag) since cycle start —
        # sweeping that aside would silently eat the flag
        if sidecar_ok "$SDIR/$sc"; then
            continue
        fi
        if [ -e "$SDIR/$sc.corrupt" ]; then
            # keep the FIRST set-aside: it holds the only bytes with any
            # chance of hand recovery; later corrupt arrivals are noise
            rm -f "$SDIR/$sc"
        else
            note "setting aside corrupt sidecar $sc"
            mv "$SDIR/$sc" "$SDIR/$sc.corrupt"
        fi
    fi
    [ -e "$SDIR/$sc" ] || echo "$sc" >> "$restore"
done
if [ -s "$restore" ]; then
    run_rsync --ignore-existing --files-from="$restore" "$HUB"/ "$SDIR"/ \
        || warn "own-sidecar restore failed"
    # a hub copy that is itself corrupt must not be accepted — left in
    # place it would quarantine-and-restore ping-pong every 30 s; only
    # files that actually arrived are re-checked (rarely any)
    while IFS= read -r sc; do
        if [ -e "$SDIR/$sc" ] && ! sidecar_ok "$SDIR/$sc"; then
            note "hub copy of $sc is corrupt too — dropping it (prune it on the hub)"
            rm -f "$SDIR/$sc"
        fi
    done < "$restore"
fi

# ── pull pass 2: updates to files another unit owns ───────────────────────
# Their owner is authoritative: a growing live snapshot, new flags in their
# targets sidecar, meta edits to their study. Explicit list (not patterns)
# so a LEGACY id that happens to contain '__' in its name part can never be
# mistaken for foreign-owned and get local edits clobbered.
pull2="$STAGE/.pull2list"
: > "$pull2"
for f in "$SDIR"/*.db "$SDIR"/*.json "$SDIR"/*.log; do
    base="$(basename "$f")"
    own="$(owner_of "${base%.*}")"
    if [ -n "$own" ] && [ "$own" != "$H" ]; then
        if [ "${f##*.}" = "db" ] \
                && { [ -s "$f-wal" ] || [ -s "$f-journal" ]; }; then
            # a crashed non-owner write (settings/rename ARE allowed on
            # foreign studies) left a hot journal beside our copy —
            # replacing the db file underneath it would corrupt the copy
            # on its next open. Resolve the journal first; if frames
            # survive (a writer or reader holds it right now) skip just
            # this file for this cycle.
            heal_db "$f" || true
            if [ -s "$f-wal" ] || [ -s "$f-journal" ]; then
                note "skipping journal-hot foreign $base this cycle"
                continue
            fi
        fi
        echo "$base" >> "$pull2"
    fi
done
if [ -s "$pull2" ]; then
    # files missing on the hub (a study the other unit recorded but hasn't
    # pushed yet, a hub prune) are the rc 23/24 run_rsync tolerates
    run_rsync --files-from="$pull2" "$HUB"/ "$SDIR"/ \
        || warn "pull of foreign updates failed"
fi

rm -rf "$STAGE"
if [ "$FAIL" = 1 ]; then
    note "cycle finished with warnings"
    exit 1
fi
note "cycle ok"
