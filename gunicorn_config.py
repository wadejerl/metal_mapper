import os
import threading

bind          = "127.0.0.1:8000"
# Where db_map lives. gunicorn only puts its *current directory* on
# sys.path, so a hand-launched `gunicorn --config /opt/metal_mapper/
# gunicorn_config.py db_map:app` from any other directory died with
# "No module named db_map" (the unit sets WorkingDirectory, a shell does
# not). Pin both so the config works from anywhere.
chdir         = "/opt/metal_mapper"
pythonpath    = "/opt/metal_mapper"
workers       = 1          # must be 1 — daemon state and _calib dict are process-local
worker_class  = "gthread"  # threads let SSE hold a connection while other requests flow
threads       = 8          # every open page holds one SSE stream (map/raw
                           # ticks, landing-page nav chip) — kiosk plus a
                           # couple of laptops/phones must not starve the
                           # pool and block start/stop/cmd/data requests
timeout       = 0          # disable worker heartbeat timeout — SSE is long-lived
worker_tmp_dir = "/dev/shm"  # spare the SD card
# Drain window when the worker stops accepting (SIGTERM from systemctl
# restart, or the RSS recycle below). The kiosk always holds an SSE stream
# and those never end on their own, so the drain ALWAYS runs in full: this
# value IS the restart downtime (the default 30 s was 30 s of 502s). It
# also bounds the longest request that survives a restart — a 950k-point
# /points is ~1.3 s of server work on a fast Mac, several times that on a
# Pi 4, plus the 45 MB download to a laptop over WiFi. 10 s covers the
# kiosk's own load; a laptop download cut short is re-fetched by the page
# (view.html retries /points), which is cheaper than a longer outage on
# every restart. SSE clients reconnect on their own.
graceful_timeout = 10

errorlog  = "/var/log/metal_mapper/error.log"
accesslog = "/var/log/metal_mapper/access.log"
loglevel  = "info"

# ── Worker memory guard ──────────────────────────────────────────────────────
# A 2 GB Pi 4 shares its RAM with the chromium kiosk; the kernel's answer to
# a worker that outgrows its share is an OOM kill with the whole box
# thrashing first (seen 2026-09: anon-rss 1.3 GB after JSON-serving a
# 950k-point study). Points now travel as packed columns and the SSE replay
# is capped, so the worker should idle near 100 MB — this is the backstop:
# past the threshold, stop taking requests and let the arbiter fork a fresh
# worker. Set MM_WORKER_RSS_MAX_MB=0 to disable.
WORKER_RSS_MAX_MB = int(os.environ.get('MM_WORKER_RSS_MAX_MB', '512'))


def worker_rss_mb():
    """Resident set of this process in MB, or None where /proc is absent
    (a Mac bench run) — the guard is then a no-op."""
    try:
        with open('/proc/self/statm') as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE') / 2 ** 20
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def recycle_worker(worker, why):
    """The max_requests lever: alive=False stops accepting, in-flight
    requests get graceful_timeout, then the worker exits and the arbiter
    respawns. Backstop for what max_requests trips over here: an SSE
    thread whose client never disconnects is a non-daemon executor thread,
    which the interpreter joins at exit — sys.exit(0) would hang, and with
    timeout=0 the arbiter never murders a silent worker. So a hard exit
    follows the drain window if the process is still around."""
    worker.log.warning('recycling worker: %s', why)
    worker.alive = False
    t = threading.Timer(graceful_timeout + 2, os._exit, (0,))
    t.daemon = True
    t.start()


def post_request(worker, req, environ, resp):
    if not WORKER_RSS_MAX_MB or not worker.alive:
        return
    rss = worker_rss_mb()
    if rss is not None and rss >= WORKER_RSS_MAX_MB:
        recycle_worker(worker, 'rss %.0f MB >= %d MB after %s %s' % (
            rss, WORKER_RSS_MAX_MB, environ.get('REQUEST_METHOD', '?'),
            environ.get('PATH_INFO', '?')))
