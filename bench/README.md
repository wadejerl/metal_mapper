# Bench rig — test db_map without hardware

Everything here runs on a laptop with no detector and no GPS attached.
Studies land in `bench/studies/` (gitignored); point db_map at it with
`--studies-dir`.

## Synthetic studies (one-shot generators)

```sh
python3 bench/gen_study.py     # serpentine_demo (630-pt survey, 2 targets)
                               # + warn_demo (drift flags, identity failure)
python3 bench/gen_drift.py     # drift_demo (300 heading-NULL points — the
                               # no-RTK "GPS wander fakes movement" case)
```

Both write through `serial_daemon.db_open()` so the schema is exactly what
the real daemon produces, never a hand-rolled copy.

## Live simulator (zero mocks below the pty)

```sh
python3 bench/live_sim.py
```

Opens a pty "firmware" that speaks the detector3 protocol — `I` handshake,
sample lines at 50 Hz plus GPS/heading anchor lines at 10 Hz, tuning-key
echoes (`a/z s/x f/v g/b`) and the word-gated `SAVE`/`CLEAR` — and launches the **real**
`serial_daemon.py` against it, writing `bench/studies/live_demo.db`.
Cycles every 60 s: RTK-fixed recording → RTK-float degraded → idle/no-fix,
so every banner state and the timing round-trip can be exercised from the
browser. Daemon log: `bench/live_daemon.log`. Ctrl-C stops both.

## Fake nav GPS

```sh
python3 bench/fake_nav.py      # NMEA GGA+RMC over TCP on 127.0.0.1:50310
```

Simulates the nav receiver driving circles around the demo field at ~3 m/s.

## Putting it together

```sh
python3 bench/gen_study.py && python3 bench/gen_drift.py
python3 bench/fake_nav.py &
python3 bench/live_sim.py &
python3 db_map.py --studies-dir bench/studies \
                  --nav-host 127.0.0.1 --nav-port 50310 --web-port 5057
```

Then open http://localhost:5057 — live_demo shows the full live pipeline,
serpentine_demo the slider/zero/channel controls, drift_demo the
heading-less rendering, warn_demo the warning pills.
