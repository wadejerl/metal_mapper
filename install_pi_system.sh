#!/bin/bash
# install_pi_system.sh — set up the system side of a fresh(ish) Pi from this
# repo checkout. Idempotent: safe to re-run after adding a new unit template
# or moving the checkout. Grows as the system does — add new setup sections
# at the bottom.
#
#   ./install_pi_system.sh
#
# Units are RENDERED from the *.service.in / *.timer.in templates in the
# repo root into /etc/systemd/system (real files, not symlinks): the
# @MM_*@ placeholders take the service user (whoever ran sudo), its home,
# this checkout's path, and the RTK base host — `northpole` unless the
# gitignored install.conf (see install.conf.example) says otherwise.
# So after a `git pull` that touches a template, re-run this script — it
# re-renders and daemon-reloads. (Pre-Sept-2026 installs symlinked the
# unit files; run this right after the pull that brought the templates —
# BEFORE any daemon-reload or reboot — and it replaces those links, which
# dangle now that the files they pointed at are *.in templates.)
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

# ── site config + the values the unit templates need ──────────────────────
# install.conf is gitignored and sourced as shell (KEY=value lines only).
if [ -f "$REPO/install.conf" ]; then
    # shellcheck disable=SC1091
    . "$REPO/install.conf"
fi
# The units run as the checkout's user; that user also owns the app dir so
# python can drop __pycache__ there and overlays can be added sans sudo.
SVC_USER="${MM_USER:-${SUDO_USER:-$(stat -c %U "$REPO/db_map.py")}}"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    echo "error: service user '$SVC_USER' does not exist on this box — check" \
         "MM_USER in install.conf, or run this from the account that owns the" \
         "checkout. (Nothing has been changed.)" >&2
    exit 1
fi
SVC_HOME="${MM_HOME:-$(getent passwd "$SVC_USER" | cut -d: -f6)}"
SVC_HOME="${SVC_HOME:-/home/$SVC_USER}"
# The RTK base station Pi's hostname (NTRIP caster on :2101), for the
# jlw_rover_rtk* units. The base is called northpole — a fixed beacon.
MM_BASE_HOST="${MM_BASE_HOST:-northpole}"
# The values are spliced into the templates by sed and land unquoted in
# ExecStart= lines: whitespace, sed's | & \ and systemd's $ % " ' would be
# mangled or split — a checkout at "~/Metal Mapper" would exec "~/Metal".
for kv in "MM_USER=$SVC_USER" "MM_HOME=$SVC_HOME" "MM_REPO=$REPO" "MM_BASE_HOST=$MM_BASE_HOST"; do
    case "${kv#*=}" in
        ''|*[[:space:]\|\&\\\$%\"\']*)
            echo "error: ${kv%%=*}='${kv#*=}' is empty or contains whitespace or one" \
                 "of | & \\ \$ % \" ' — that cannot be rendered into a systemd unit." \
                 "Use a plainer path/name. (Nothing has been changed.)" >&2
            exit 1;;
    esac
done
echo "service user: $SVC_USER ($SVC_HOME)   rtk base: $MM_BASE_HOST"

# ── systemd units: render + enable every template in the repo root ────────
# (rtkbase/ units are the base Pi's and are deployed there by hand — only
# the repo ROOT's units belong on a head unit.)
shopt -s nullglob
templates=("$REPO"/*.service.in "$REPO"/*.timer.in)
if [ ${#templates[@]} -eq 0 ]; then
    echo "error: no *.service.in / *.timer.in templates found in $REPO" >&2
    exit 1
fi

units=()
for tpl in "${templates[@]}"; do
    name="$(basename "${tpl%.in}")"
    dest="/etc/systemd/system/$name"
    rendered="$(sed -e "s|@MM_USER@|$SVC_USER|g" -e "s|@MM_HOME@|$SVC_HOME|g" \
                    -e "s|@MM_REPO@|$REPO|g" -e "s|@MM_BASE_HOST@|$MM_BASE_HOST|g" \
                    "$tpl")"
    left="$(printf '%s\n' "$rendered" | grep -o '@MM_[A-Z_]*@' | sort -u || true)"
    if [ -n "$left" ]; then
        echo "error: $name: unfilled placeholder(s): $(echo $left)" >&2
        exit 1
    fi
    # an older install symlinked the repo file here — that link now points
    # at a template full of @MM_*@, so it must become a real file
    if [ -L "$dest" ]; then
        rm -f "$dest"
        echo "unlinked $dest (was a symlink into the checkout)"
    fi
    if [ -f "$dest" ] && [ "$(cat "$dest")" = "$rendered" ]; then
        echo "current  $dest"
    else
        printf '%s\n' "$rendered" > "$dest.tmp"
        chmod 644 "$dest.tmp"
        mv -f "$dest.tmp" "$dest"
        echo "rendered $dest <- $(basename "$tpl")"
    fi
    units+=("$dest")
done

systemctl daemon-reload

for unit in "${units[@]}"; do
    name="$(basename "$unit")"
    # a unit with no [Install] is activated by something else (e.g.
    # jlw_mm_sync.service, which only its timer starts) — enable would fail
    if ! grep -q '^\[Install\]' "$unit"; then
        echo "skipped $name (no [Install] — activated by another unit)"
        continue
    fi
    systemctl enable "$name"
    echo "enabled $name"
done

# ── app dir: /opt/metal_mapper — what jlw_metalmap.service runs ───────────
# Symlinks, not copies: a `git pull` in the checkout updates the app in
# place (restart gunicorn after — templates are cached at start).
OPT=/opt/metal_mapper
# owned by the service user (see SVC_USER above)
install -d -o "$SVC_USER" -g "$SVC_USER" "$OPT"

app_files=(db_map.py serial_daemon.py nav_gps.py gunicorn_config.py
           sync_studies.sh static templates)
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
    # Gated units (ExecStartPre wait + TimeoutStartSec=infinity, i.e. the
    # RTK feeds) hold in 'activating' until their GPS + the base station
    # exist — a plain foreground start would sit on that wait forever and
    # wedge this paste block before the kiosk UI unit ever starts.
    flags=''
    grep -q '^TimeoutStartSec=infinity' "$unit" && flags='--no-block '
    echo "  sudo systemctl start ${flags}$(basename "$unit")"
done
