#!/bin/bash
# start_local_ui.sh — the kiosk: chromium fullscreen on the Pi's own display.
# jlw_ui_mm.service runs this as the service user once the display manager
# has logged that user into the :0 session; install_pi_system.sh sets both
# up, and the unit's Restart=always retries every 5 s until the session is
# there (chromium exits at once without a display).
export DISPLAY=:0

# Trixie/Bookworm ship the browser as `chromium`, older images as
# `chromium-browser`.
BROWSER="$(command -v chromium || command -v chromium-browser)" || {
    echo "start_local_ui: no chromium here — sudo ./install_pi_system.sh installs it" >&2
    exit 1
}

# Wait for the web UI to answer before chromium loads it. The unit orders
# this After= jlw_metalmap, whose ExecStartPost probe holds it in
# "activating" until gunicorn's worker answers /healthz — so the backend
# answers by now; what this waits out is its FIRST landing-page render
# (the study index: cached in <studies_dir>/.study_index.json, so normally
# instant, but new or changed studies get counted), which on a cold SD
# card — with chromium and the desktop loading off the same card — can
# take a while. Probing http://localhost/ rather than /healthz warms that
# index, so chromium's own first load is served from it. One probe at a
# time, each allowed the rest
# of the window: a short per-probe timeout would abandon that render
# mid-scan and start another every few seconds, stacking scans until the
# worker's thread pool is full — the very hang this is meant to avoid.
# Bounded well under the unit's 90 s start timeout; a backend still not
# answering when chromium loads gets nginx's own retrying page
# (nginx/metal-mapper), so the kiosk never parks on a dead 502. curl and
# wget both ship on Pi OS; with neither, the ordering alone has to do.
URL=http://localhost/
WAIT_S=60
if command -v curl >/dev/null 2>&1; then
    probe() { curl -sf -o /dev/null --max-time "$1" "$URL"; }
elif command -v wget >/dev/null 2>&1; then
    probe() { wget -q -O /dev/null -T "$1" -t 1 "$URL"; }
else
    probe() { return 0; }
fi
# the remaining window is read ONCE per pass and never passed as 0: for
# both curl --max-time and wget -T, 0 means "no timeout at all"
deadline=$((SECONDS + WAIT_S))
while :; do
    left=$((deadline - SECONDS))
    [ "$left" -gt 0 ] || break
    [ "$left" -gt "$WAIT_S" ] && left=$WAIT_S     # clock stepped backwards
    probe "$left" && break
    sleep 1
done

# Keep the screen on: a bare X session blanks after 10 min, and on a
# Desktop image light-locker then parks the greeter over the kiosk. Asked
# right before chromium, AFTER the wait above: lightdm 'active' is the
# daemon, not the session, so this script can start before X is up — X
# arriving during the wait must not leave a chromium on a screen that
# still blanks. Fails quietly with no X yet; chromium then exits at once
# and Restart=always re-runs the whole script. (A Desktop session has its
# own setting — asking twice is harmless.)
if command -v xset >/dev/null 2>&1; then
    xset s off -dpms s noblank 2>/dev/null || true
fi

"$BROWSER" --password-store=basic --kiosk --noerrdialogs --disable-infobars "$URL" &
