#!/bin/bash
# install_pi_system.sh — set up the system side of a fresh(ish) Pi from this
# repo checkout. Idempotent: safe to re-run after adding a new .service file
# or moving the checkout. Grows as the system does — add new setup sections
# at the bottom.
#
#   ./install_pi_system.sh
#
# Units are SYMLINKED into /etc/systemd/system (not copied), so a `git pull`
# updates them in place — run `sudo systemctl daemon-reload` after a pull
# that touches unit files.
set -euo pipefail

command -v systemctl >/dev/null 2>&1 || {
    echo "error: no systemctl here — this runs on the Pi, not the bench Mac" >&2
    exit 1
}

# ── root ──────────────────────────────────────────────────────────────────
# Re-exec under sudo (prompts for a password only if the user needs one).
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

REPO="$(cd "$(dirname "$0")" && pwd -P)"
echo "repo: $REPO"

# /opt/metal_mapper is BUILT from this checkout — a checkout living there
# would symlink files onto themselves (ln refuses, set -e aborts mid-run).
if [ "$REPO" = "/opt/metal_mapper" ]; then
    echo "error: the checkout must not live at /opt/metal_mapper — that is" \
         "the app dir this script builds. Clone elsewhere (e.g." \
         "~/metal_mapper) and re-run." >&2
    exit 1
fi

# ── systemd units: symlink + enable every .service in the repo root ───────
shopt -s nullglob
units=("$REPO"/*.service)
if [ ${#units[@]} -eq 0 ]; then
    echo "error: no .service files found in $REPO" >&2
    exit 1
fi

for unit in "${units[@]}"; do
    name="$(basename "$unit")"
    ln -sfn "$unit" "/etc/systemd/system/$name"
    echo "linked  /etc/systemd/system/$name -> $unit"
done

systemctl daemon-reload

for unit in "${units[@]}"; do
    name="$(basename "$unit")"
    systemctl enable "$name"
    echo "enabled $name"
done

# ── app dir: /opt/metal_mapper — what jlw_metalmap.service runs ───────────
# Symlinks, not copies: a `git pull` in the checkout updates the app in
# place (restart gunicorn after — templates are cached at start).
OPT=/opt/metal_mapper
# The unit runs as the checkout's user; that user owns the app dir so
# python can drop __pycache__ there and overlays can be added sans sudo.
SVC_USER="${SUDO_USER:-$(stat -c %U "$REPO/db_map.py")}"
install -d -o "$SVC_USER" -g "$SVC_USER" "$OPT"

app_files=(db_map.py serial_daemon.py nav_gps.py gunicorn_config.py
           static templates)
for f in "${app_files[@]}"; do
    # Legacy copy-deployments left REAL dirs here (old README said to cp
    # static/ and templates/); ln -sfn onto a real dir would silently NEST
    # the link inside it and freeze the copies forever — migrate them.
    if [ -d "$OPT/$f" ] && [ ! -L "$OPT/$f" ]; then
        echo "migrating: removing copied $OPT/$f (replaced by symlink)"
        rm -rf -- "${OPT:?}/$f"
    fi
    ln -sfn "$REPO/$f" "$OPT/$f"
    echo "linked  $OPT/$f -> $REPO/$f"
done
# map overlays: whatever GeoJSON the repo carries rides along
for gj in "$REPO"/*.geojson; do
    name="$(basename "$gj")"
    # a real file here is an operator's field-added overlay — never clobber
    if [ -e "$OPT/$name" ] && [ ! -L "$OPT/$name" ]; then
        echo "skip    $OPT/$name: real file (operator overlay?) — resolve manually" >&2
        continue
    fi
    ln -sfn "$gj" "$OPT/$name"
    echo "linked  $OPT/$name -> $gj"
done
# prune broken links (repo file renamed/removed, or the checkout moved) —
# a dangling symlink serves nobody and TemplateNotFound debugging is misery
find "$OPT" -maxdepth 1 -xtype l -print -delete | sed 's/^/pruned  /'

# ── the rest of what jlw_metalmap.service needs to start ──────────────────
# Network-free, so it runs BEFORE the pip sync: an offline field install
# must still come up even when the index is unreachable.
# gunicorn_config.py logs here; gunicorn refuses to boot without the dir.
install -d -o "$SVC_USER" -g "$SVC_USER" /var/log/metal_mapper
# serial_daemon.py opens the detector's USB CDC port.
usermod -a -G dialout "$SVC_USER"

# venv: created once; deps re-synced every run (a no-op offline when
# nothing changed). Non-fatal: an unreachable index must not abort the
# rest of the setup in the field.
if [ ! -x "$OPT/venv/bin/pip" ]; then
    echo "creating venv in $OPT/venv"
    python3 -m venv "$OPT/venv"
fi
"$OPT/venv/bin/pip" install --quiet -r "$REPO/requirements.txt" \
    || echo "warn: pip sync failed (offline?) — venv deps not updated" >&2
echo "venv ok ($("$OPT/venv/bin/python" --version 2>&1))"

# ── nginx: :80 reverse proxy, SSE-aware (nginx/metal-mapper) ──────────────
if [ -d /etc/nginx/sites-enabled ]; then
    ln -sfn "$REPO/nginx/metal-mapper" /etc/nginx/sites-enabled/metal-mapper
    echo "linked  /etc/nginx/sites-enabled/metal-mapper"
    # The stock Debian default site is default_server on :80 — left in
    # place it wins every request and serves the nginx welcome page
    # instead of the app. sites-available/default survives for reference.
    if [ -e /etc/nginx/sites-enabled/default ]; then
        rm -f /etc/nginx/sites-enabled/default
        echo "removed stock default site (was shadowing :80)"
    fi
    # Non-fatal like the pip sync: a bad config must not abort the run —
    # and -t failing leaves the OLD config serving, nothing breaks live.
    nginx -t && systemctl reload-or-restart nginx \
        || echo "warn: nginx config test/reload failed — fix and re-run" >&2
else
    echo "warn: nginx not installed — skipping proxy setup" \
         "(sudo apt install nginx, then re-run)" >&2
fi

# ── future setup sections go here ──────────────────────────────────────────

echo
echo "done. Units are enabled but not started — reboot, or start them now:"
for unit in "${units[@]}"; do
    echo "  sudo systemctl start $(basename "$unit")"
done
