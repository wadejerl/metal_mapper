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
# @MM_*@ placeholders take the service user (whoever ran sudo), this
# checkout's path, and the RTK base host — `northpole` unless the
# gitignored install.conf (see install.conf.example) says otherwise.
# Needs a few packages a stock Raspberry Pi OS image lacks — nginx (the
# :80 reverse proxy in front of gunicorn), chromium (the kiosk), rsync,
# python3-venv, git, and on a Lite image a bare X login for the kiosk —
# whatever is missing is offered as ONE apt-get up front; the script stops
# if that is declined. So after a `git pull` that touches a template, re-run this script — it
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
# The RTK base station Pi's hostname (NTRIP caster on :2101), for the
# jlw_rover_rtk* units. The base is called northpole — a fixed beacon.
MM_BASE_HOST="${MM_BASE_HOST:-northpole}"
# The values are spliced into the templates by sed and land unquoted in
# ExecStart= lines: whitespace, sed's | & \ and systemd's $ % " ' would be
# mangled or split — a checkout at "~/Metal Mapper" would exec "~/Metal".
for kv in "MM_USER=$SVC_USER" "MM_REPO=$REPO" "MM_BASE_HOST=$MM_BASE_HOST"; do
    case "${kv#*=}" in
        ''|*[[:space:]\|\&\\\$%\"\']*)
            echo "error: ${kv%%=*}='${kv#*=}' is empty or contains whitespace or one" \
                 "of | & \\ \$ % \" ' — that cannot be rendered into a systemd unit." \
                 "Use a plainer path/name. (Nothing has been changed.)" >&2
            exit 1;;
    esac
done
echo "service user: $SVC_USER   rtk base: $MM_BASE_HOST"

# ── packages a stock Raspberry Pi OS image lacks ──────────────────────────
# Settled here, before anything on the box is touched, as ONE apt-get so a
# fresh Pi (Trixie Lite or Desktop) is done in a single prompt:
#   nginx         the only thing listening on :80 — gunicorn sits behind it
#                 on 127.0.0.1:8000 (nginx/metal-mapper); without it the
#                 kiosk and every laptop get nothing
#   chromium      the kiosk browser jlw_ui_mm runs on the Pi's own screen
#                 (start_local_ui.sh); `chromium` on Trixie/Bookworm, older
#                 images only know `chromium-browser` — resolved after the
#                 index update
#   rsync         the studies sync (sync_studies.sh)
#   python3-venv  Debian splits venv/ensurepip out of python3
#   git           the deploy path is `git pull`
#   curl          start_local_ui.sh polls the landing page before chromium
#                 loads it (wget does as well; stock images have both)
#   lightdm xserver-xorg x11-xserver-utils openbox
#                 only when there is no display manager at all (a Lite
#                 image): a bare X login that puts the service user on :0 at
#                 boot for the kiosk. A Desktop image has its own — left alone.
# Declined, or no terminal to ask on: stop, nothing changed.
missing=()
command -v nginx >/dev/null 2>&1 || missing+=(nginx)
command -v rsync >/dev/null 2>&1 || missing+=(rsync)
command -v git   >/dev/null 2>&1 || missing+=(git)
command -v curl  >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || missing+=(curl)
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || missing+=(python3-venv)
command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 \
    || missing+=(chromium)
new_dm=
if ! command -v lightdm >/dev/null 2>&1 && [ ! -s /etc/X11/default-display-manager ]; then
    missing+=(lightdm xserver-xorg x11-xserver-utils openbox)
    new_dm=1
fi
if [ ${#missing[@]} -gt 0 ]; then
    echo "not installed: ${missing[*]}"
    if [ -t 0 ]; then
        read -r -p "Install with apt-get now? [Y/n] " ans || ans=n
    else
        ans=n
        echo "(no terminal to ask on)"
    fi
    case "${ans:-Y}" in
        [Yy]*)
            apt-get update || {
                echo "error: apt-get update failed — is the Pi online?" \
                     "(Nothing has been changed.)" >&2
                exit 1
            }
            if [[ " ${missing[*]} " == *" chromium "* ]] \
                    && ! apt-cache show chromium >/dev/null 2>&1; then
                missing=("${missing[@]/chromium/chromium-browser}")
            fi
            DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}" || {
                echo "error: apt-get could not install: ${missing[*]}." \
                     "(Nothing else has been changed.)" >&2
                exit 1
            };;
        *)
            echo "error: required: ${missing[*]}. Install them" \
                 "(sudo apt-get install ${missing[*]}) and re-run." \
                 "(Nothing has been changed.)" >&2
            exit 1;;
    esac
fi

# ── kiosk login: the service user on :0 at boot ───────────────────────────
# jlw_ui_mm.service runs start_local_ui.sh as the service user and expects
# that user's X session on DISPLAY=:0 (Restart=always: it retries every 5 s
# until the session is there). On a Desktop image raspi-config's Desktop
# Autologin provides it — checked here, never changed. When the display
# manager was installed just now (Lite image) it gets pointed at the
# service user: a lightdm.conf.d drop-in (anything raspi-config later
# writes to lightdm.conf itself wins) plus the graphical default target a
# Lite image does not have.
if [ -n "$new_dm" ]; then
    install -d /etc/lightdm/lightdm.conf.d
    cat > /etc/lightdm/lightdm.conf.d/50-metal-mapper.conf <<EOF
# written by install_pi_system.sh — the kiosk's login: the service user
# straight into a bare openbox session on :0; jlw_ui_mm.service puts
# chromium --kiosk on it. Settings in /etc/lightdm/lightdm.conf override these.
[Seat:*]
autologin-user=$SVC_USER
autologin-user-timeout=0
autologin-session=openbox
EOF
    systemctl set-default graphical.target
    echo "kiosk: lightdm logs $SVC_USER into openbox on :0 at boot; default target -> graphical"
else
    al="$(grep -hs '^autologin-user=' /etc/lightdm/lightdm.conf.d/*.conf \
              /etc/lightdm/lightdm.conf 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    if [ "$al" != "$SVC_USER" ]; then
        echo "warn: display autologin user is '${al:-unset}', not $SVC_USER —" \
             "jlw_ui_mm needs $SVC_USER's session on :0 (raspi-config: System" \
             "Options -> Boot / Auto Login -> Desktop Autologin, as $SVC_USER)" >&2
    fi
    if [ "$(systemctl get-default)" != "graphical.target" ]; then
        echo "warn: default target is $(systemctl get-default), not graphical.target —" \
             "jlw_ui_mm.service (WantedBy=graphical.target) will not start at boot" >&2
    fi
fi

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
    rendered="$(sed -e "s|@MM_USER@|$SVC_USER|g" -e "s|@MM_REPO@|$REPO|g" \
                    -e "s|@MM_BASE_HOST@|$MM_BASE_HOST|g" \
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
# A hand-launched `sudo gunicorn ...` leaves root-owned log files behind,
# and the unit (running as the service user) then fails to open them at
# boot with nothing useful in the journal — re-running this hands them back.
find /var/log/metal_mapper -maxdepth 1 -type f ! -user "$SVC_USER" \
     -exec chown "$SVC_USER:$SVC_USER" {} + -print | sed 's/^/chowned /'
# serial_daemon.py opens the detector's USB CDC port.
usermod -a -G dialout "$SVC_USER"

# venv: created once; deps re-synced every run (a no-op offline when
# nothing changed). Non-fatal: an unreachable index must not abort the
# rest of the setup in the field. Besides the web app this venv holds
# pygnssutils — the jlw_rover_* units run its gnssserver/gnssntripclient
# from here (nothing is expected in the service user's home).
if [ ! -x "$OPT/venv/bin/pip" ]; then
    echo "creating venv in $OPT/venv"
    python3 -m venv "$OPT/venv"
fi
"$OPT/venv/bin/pip" install --quiet -r "$REPO/requirements.txt" \
    || echo "warn: pip sync failed (offline?) — venv deps not updated" >&2
echo "venv ok ($("$OPT/venv/bin/python" --version 2>&1))"
for tool in gnssserver gnssntripclient; do
    [ -x "$OPT/venv/bin/$tool" ] \
        || echo "warn: $OPT/venv/bin/$tool missing — the jlw_rover_* units run it;" \
                "re-run this script with the index reachable so pip can install pygnssutils" >&2
done

# ── nginx site: :80 reverse proxy, SSE-aware (nginx/metal-mapper) ─────────
# nginx itself was settled by the package gate up top; Debian's package always has sites-enabled
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
    echo "error: nginx is installed but /etc/nginx/sites-enabled is missing — not" \
         "Debian's nginx layout; link $REPO/nginx/metal-mapper into its config by hand" >&2
    exit 1
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
