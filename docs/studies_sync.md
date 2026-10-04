# Studies sync — two head units, one hub

Both head units carry the full studies library. They never talk to each
other directly: each runs a pair of rsync passes against a **hub** — an
rsync daemon on the RTK base station Pi, which is already the field LAN's
DHCP/NTP anchor and is always powered when the units are.

```
 head unit A  ──rsync──▶            ◀──rsync──  head unit B
 (studies/)              base Pi                 (studies/)
                      [mm_studies]
```

## The one invariant

**Every synced file has exactly one writing unit**, so rsync never has to
merge anything:

| file                              | writer            | others see it as |
|-----------------------------------|-------------------|------------------|
| `<name>_<epoch>__<host>.db`       | `<host>` only     | read-only        |
| `<study>.targets__<host>.json`    | `<host>` only     | read-only        |
| `<name>_<epoch>__<host>.log`      | `<host>` only     | read-only        |

New study ids get the recording unit's host tag appended by db_map
(`new_study_id`), and dig targets flagged anywhere go into the flagging
unit's own **sidecar json**, never into the study db. That is why person B
can flag targets on person A's still-growing study while A also flags —
each unit's flags live in its own file, and every map view renders the
union of all sidecars (plus the legacy in-db `targets` table). Marking a
target *found* works the same way: the annotation is written into the
marking unit's own sidecar, keyed by the target's `src:id`, so either crew
can retire any pin. **Un-marking**, though, only works on the unit that
set the mark — the mark lives in that unit's file, and popping your own
sidecar can't undo it. The three status boxes (visited / found / skip)
work the same way: a flag another unit set shows as a dimmed, disabled
checkbox, and `POST /targets/<id>/status` refuses to clear it with a 403
(`<flag> was set on another unit — clear it there`). Sidecar format
version 2 adds `marks` (visited/skip) and `sets` (named, ordered pin
lists) to the same one-writer file; a pin copied from another unit's pin
by *save as set* carries `from`, and status marks for it are written
under the SOURCE key, so even a v1 reader sees the find.

The host tag is the short hostname with everything outside `[A-Za-z0-9-]`
replaced by `-` (`db_map.HOST_TAG`; `sync_studies.sh` derives the same
value — keep them in step). Since the tag can never contain `_`, the
trailing `__<tag>` token is unambiguous even for ids whose name part
contains `__`.

## What a sync cycle does (`sync_studies.sh`, every 30 s via timer)

1. **Snapshot the live study.** The recording db is WAL-hot (db + `-wal` +
   `-shm`, ~20 commits/s); rsyncing it raw would ship a torn file. The
   script takes a consistent snapshot with SQLite's `VACUUM INTO` (which
   never blocks the writer) and pushes *that* under the study's name. The
   other unit's copy therefore grows at sync cadence — its open map view
   fills in live, because `/stream` re-reads the db every tick and rsync
   swaps files in atomically (temp file + rename). A live study with **0
   points** (RTK gate not passed yet) is not published: an aborted start
   must never reach the hub, where this unit's purge can't follow it.
2. **Push own files**: closed studies, sidecars, and logs tagged with this
   host. The hot db itself never ships. A closed db that still has a
   non-empty `-wal` (crashed session — or a live writer whose pidfile was
   lost) gets a checkpoint attempt so later cycles converge, but *this*
   cycle it ships as a `VACUUM INTO` snapshot, never the raw file: a
   secretly-live writer can't tear a snapshot. Own 0-point dbs — and
   unreadable ones (no schema yet, torn at creation, SD rot) — are
   skipped, along with their sidecars and logs (they're purge fodder, and
   a hub copy would outlive the purge). A sidecar that no longer parses
   is never pushed — it would replace the hub's good copy.
3. **Pull pass 1** (`--ignore-existing`): everything we don't have yet —
   excluding our own files (we are authoritative for those; a study
   deleted here must not resurrect from the hub), any `-wal`/`-shm`, and
   all dotfiles (the hub rsyncd stages the other unit's in-flight push as
   a `.name.db.XXXXXX` temp — pulling one would plant hidden torn junk).
   One exception pass follows: an **own sidecar that is missing (or no
   longer parses) while its study db is present** is restored from the hub
   — losing it would reset the target numbering and let stale found-marks
   strike new targets. This covers our sidecars on every study we hold,
   own or foreign; a corrupt one is set aside as `*.corrupt`
   first (only the first set-aside is kept — it holds the recoverable
   bytes), and a restored copy that is itself corrupt is dropped rather
   than accepted (fix or prune that copy on the hub).
4. **Pull pass 2**: in-place updates for files **another unit owns** (its
   growing live snapshot, its updated sidecars, its meta edits). Built as
   an explicit file list with a real ownership parse, so an untagged id
   (a file copied onto the hub by hand) that merely contains `__` can never
   be mistaken for foreign and get local edits clobbered. A foreign db
   sitting under a stale local `-wal` or
   `-journal` (a crashed non-owner settings write) is checkpointed/rolled
   back first — swapping the db file under a hot journal corrupts the
   copy — and skipped for the cycle if the journal won't resolve.

No `--delete`, anywhere, ever. Deleting a study for good is manual, **in
this order**: on the owning unit first, then the hub, then any peer that
pulled it. A hub-only delete resurrects from the owner's next push within
30 s; a peer-only delete resurrects from the hub. A local delete of your
own file stays deleted locally (and the restore pass above only revives
sidecars whose study still exists) but the hub archive keeps its copy
until pruned there.

Consequences worth knowing:

- **Non-owner in-db writes are ephemeral.** Saving view settings or
  re-zeroing on a study the *other* unit recorded writes into your local
  copy, and the next pull overwrites it. Targets are exempt — they live in
  sidecars.
- The empty-study purge (`purge_empty_studies`) skips foreign studies — a
  freshly-started study legitimately syncs in with 0 points — and skips
  legacy ones while `sync_hub` is configured, since a purge would just
  churn against the pull. The sync side holds up its end by never pushing
  an own 0-point db, so what purge deletes was never on the hub.
- The web UI badges foreign studies (`⇄ <host>`) on the landing page, and
  foreign target pins render purple; found pins go gray with a struck
  number.
- If the hub is unreachable (unit at home, base Pi off) the cycle is a
  quiet no-op — the timer keeps ticking, nothing turns red.
- **In-db pins don't travel.** Studies recorded before sidecars keep
  their dig pins in the db's own `targets` table; the map still draws them
  (keys `db:N`, bare-number labels), but only sidecars carry pins between
  units, so a `db:N` pin deleted here stays on the peer.
  `migrate_legacy_targets.py` moves such rows into a sidecar. (Pins flagged
  since the sidecar change go to sidecars and sync like any other — this
  is about the old rows only.)
- **Hub misses are routine, and quiet on the Pis.** The own-sidecar
  restore pass and pass 2 ask the hub for explicit file lists, and an
  entry the hub lacks is normal: our sidecar for a study nobody ever
  flagged or found-marked on from this unit (a peer's sidecar doesn't
  count), a foreign file its owner hasn't pushed yet, a hub prune. GNU
  rsync ≥ 3.1 (both Pis) is given `--ignore-missing-args` and skips them
  silently, so a steady-state journal shows only the script's own lines.
  openrsync (the Mac bench) lacks the flag; there the first miss aborts
  that file-list pull with one `No such file` line and rsync exits 23,
  which `run_rsync` tolerates (`cycle ok`) — so on the bench the restore
  and pass-2 legs only do work when nothing in the list is missing.

## Setup

**Head units** (both): **re-run `install_pi_system.sh` once** — it renders
and enables the new timer and links `/opt/metal_mapper/sync_studies.sh`,
which a unit deployed before this feature doesn't have; a plain
service-restart deploy never creates them. The installer enables but does
not start units, so on an already-running unit also do:

```bash
sudo systemctl start jlw_mm_sync.timer
```

(or reboot). Then point each unit at the hub once:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / 'metal_mapper' / 'config.json'
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg['sync_hub'] = 'rsync://<base-pi-host>:8730/mm_studies'
p.write_text(json.dumps(cfg, indent=2))
PY
```

**Hub** (base Pi, as user `rtk` — same manual deploy as the RTK units):

```bash
scp rtkbase/mm_hub_rsyncd.conf rtk@<base-pi>:/home/rtk/
scp rtkbase/jlw_mm_hub.service rtk@<base-pi>:/tmp/
ssh rtk@<base-pi> 'sudo mv /tmp/jlw_mm_hub.service /etc/systemd/system/ &&
                   sudo systemctl daemon-reload &&
                   sudo systemctl enable --now jlw_mm_hub.service'
```

The rsync daemon runs unprivileged as `rtk` on port 8730 (no chroot, no
creds — field-LAN trust, same as the NTRIP caster). Files land flat in
`/home/rtk/mm_studies`; connection logging goes to journald (no log file —
it would grow without bound on the SD card). A transfer killed mid-push
can leave a stale `.name.XXXXXX` temp file in the module dir; the units
never pull dotfiles, so it's harmless — prune them whenever you're on the
hub anyway.

## Testing

`python3 test_sync_studies.py` simulates both units and the hub in a temp
tree (a local directory stands in for the rsync module, plus a real
`rsync --daemon` smoke test) and covers the push/pull matrix: live
snapshot + growth propagation + the 0-point gates, cross-unit sidecar
flags, crashed-session checkpoint healing (own and foreign), delete
non-propagation and the lost/corrupt-sidecar restore, corrupted-copy healing,
hub temp-file exclusion, untagged files and the `__`-in-name trap, and
the lock (including staleness and pid recycling). The web-side
sidecar/merge/permission logic is covered in `test_db_map.py`.
