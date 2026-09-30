#!/bin/sh
# Nitro Control installer — works on any distro with Python 3.8+, GTK 4 and libadwaita.
#
#   sudo ./install.sh              install to /usr/local and start the service
#   sudo ./install.sh --uninstall  remove it (fans are handed back to the firmware)
#   sudo ./install.sh --uninstall --purge   also delete saved settings
#   sudo ./install.sh --with-rgb   also set up the 4-zone RGB keyboard driver (Linuwu-Sense)
#
# Options: --prefix DIR (default /usr/local), --no-deps, --no-service, --no-rgb, -y (don't ask)

set -eu

PREFIX=/usr/local
DEPS=1
SERVICE=1
ASSUME_YES=0
RGB=ask
ACTION=install
PURGE=0
HERE=$(cd "$(dirname "$0")" && pwd)

case " $* " in *" -h "*|*" --help "*) ;; *)
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then exec sudo sh "$0" "$@"; fi
        echo "Please run as root." >&2; exit 1
    fi ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX=$2; shift ;;
        --prefix=*) PREFIX=${1#*=} ;;
        --no-deps) DEPS=0 ;;
        --no-service) SERVICE=0 ;;
        --with-rgb) RGB=yes ;;
        --no-rgb) RGB=no ;;
        --uninstall) ACTION=uninstall ;;
        --purge) PURGE=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

LIBDIR=$PREFIX/lib/nitro-control
APP_ID=io.github.thomasmartinoa.NitroControl
BINDIR=$PREFIX/bin
SHAREDIR=$PREFIX/share

red() { printf '\033[91m%s\033[0m\n' "$*"; }
ok() { printf '\033[92m✔\033[0m %s\n' "$*"; }
info() { printf '\033[96m›\033[0m %s\n' "$*"; }
warn() { printf '\033[93m!\033[0m %s\n' "$*"; }

ask() {
    [ "$ASSUME_YES" = 1 ] && return 0
    printf '%s [Y/n] ' "$1"
    read -r reply || reply=n
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

INIT=none
if [ -d /run/systemd/system ]; then INIT=systemd
elif command -v openrc-run >/dev/null 2>&1 || [ -x /sbin/openrc-run ]; then INIT=openrc
fi

# --------------------------------------------------------------- uninstall
RGB_SETUP="$HERE/packaging/linuwu/setup-rgb.sh"

if [ "$ACTION" = uninstall ]; then
    if [ -f /etc/modprobe.d/nitro-control-rgb.conf ] || ls -d /usr/src/linuwu-sense-* >/dev/null 2>&1; then
        info "Removing the RGB keyboard driver and restoring acer_wmi…"
        sh "$RGB_SETUP" remove || warn "RGB driver removal reported a problem: sh $RGB_SETUP status"
    fi
    info "Stopping service (fans go back to automatic)…"
    if [ "$INIT" = systemd ]; then
        systemctl disable --now nitro-controld.service 2>/dev/null || true
        rm -f /etc/systemd/system/nitro-controld.service
        systemctl daemon-reload
    elif [ "$INIT" = openrc ]; then
        rc-service nitro-controld stop 2>/dev/null || true
        rc-update del nitro-controld default 2>/dev/null || true
        rm -f /etc/init.d/nitro-controld
    fi
    if [ -d "$LIBDIR" ]; then
        PYTHONPATH=$LIBDIR python3 -m nitrocontrol.daemon --restore-auto 2>/dev/null || true
    fi
    rm -rf "$LIBDIR"
    rm -f "$BINDIR/nitro-control" "$BINDIR/nitroctl" "$BINDIR/nitro-controld"
    rm -f "$SHAREDIR/applications/$APP_ID.desktop" "$SHAREDIR/icons/hicolor/scalable/apps/$APP_ID.svg"
    rm -f "$SHAREDIR/applications/nitro-control.desktop" "$SHAREDIR/icons/hicolor/scalable/apps/nitro-control.svg"
    [ "$PURGE" = 1 ] && rm -rf /var/lib/nitro-control && ok "Removed saved settings"
    ok "Nitro Control uninstalled"
    exit 0
fi

# ----------------------------------------------------------------- checks
printf '\n\033[1m  NITRO\033[91m·\033[0m\033[1mCONTROL\033[0m installer\n\n'

VENDOR=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo unknown)
MODEL=$(cat /sys/class/dmi/id/product_name 2>/dev/null || echo unknown)
info "Machine: $VENDOR $MODEL"
case "$VENDOR" in
    *Acer*) ;;
    *) warn "This does not look like an Acer laptop. Nitro Control only uses standard kernel"
       warn "interfaces, so it is harmless, but most controls will be unavailable." ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    red "python3 is required."; exit 1
fi
PYTHON=$(command -v python3)
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    red "Python 3.8 or newer is required."; exit 1
fi

have_gui_deps() {
    "$PYTHON" - <<'EOF' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import cairo
EOF
}

if [ "$DEPS" = 1 ] && ! have_gui_deps; then
    PKGS="" ; CMD=""
    if command -v pacman >/dev/null 2>&1; then CMD="pacman -S --needed"; PKGS="python-gobject python-cairo gtk4 libadwaita"
    elif command -v apt-get >/dev/null 2>&1; then CMD="apt-get install -y"; PKGS="python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1"
    elif command -v dnf >/dev/null 2>&1; then CMD="dnf install -y"; PKGS="python3-gobject python3-cairo gtk4 libadwaita"
    elif command -v zypper >/dev/null 2>&1; then CMD="zypper install -y"; PKGS="python3-gobject python3-gobject-cairo typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1"
    elif command -v xbps-install >/dev/null 2>&1; then CMD="xbps-install -y"; PKGS="python3-gobject gtk4 libadwaita"
    elif command -v apk >/dev/null 2>&1; then CMD="apk add"; PKGS="py3-gobject3 py3-cairo gtk4.0 libadwaita"
    elif command -v emerge >/dev/null 2>&1; then CMD="emerge --noreplace"; PKGS="dev-python/pygobject dev-python/pycairo gui-libs/gtk gui-libs/libadwaita"
    elif command -v eopkg >/dev/null 2>&1; then CMD="eopkg install -y"; PKGS="python-gobject python-cairo libgtk-4 libadwaita"
    fi
    if [ -n "$CMD" ]; then
        info "The GUI needs PyGObject, pycairo, GTK 4 and libadwaita."
        if ask "Install them now with: $CMD $PKGS ?"; then
            # shellcheck disable=SC2086
            $CMD $PKGS || warn "Package install failed — install the GUI dependencies manually."
        fi
    else
        warn "Unknown package manager. Install PyGObject, pycairo, GTK 4 and libadwaita manually."
    fi
    have_gui_deps && ok "GUI dependencies present" || warn "GUI dependencies missing: the CLI and service will still work."
fi

if ! lsmod 2>/dev/null | grep -qE '^(acer_wmi|linuwu_sense) '; then
    warn "Neither acer_wmi nor linuwu_sense is loaded. Trying: modprobe acer_wmi"
    modprobe acer_wmi 2>/dev/null || warn "Could not load acer_wmi — hardware controls will be limited."
fi

# ---------------------------------------------------------------- install
info "Installing to $PREFIX"
rm -rf "$LIBDIR"
mkdir -p "$LIBDIR" "$BINDIR" "$SHAREDIR/applications" "$SHAREDIR/icons/hicolor/scalable/apps"
cp -r "$HERE/nitrocontrol" "$LIBDIR/"
find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod -R u=rwX,go=rX "$LIBDIR"

wrapper() {
    cat > "$BINDIR/$1" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PYTHON" -m $2 "\$@"
EOF
    chmod 755 "$BINDIR/$1"
}
wrapper nitro-control nitrocontrol
wrapper nitroctl nitrocontrol.cli
wrapper nitro-controld nitrocontrol.daemon

# Named after the application ID so docks/taskbars match the running window to its icon
rm -f "$SHAREDIR/applications/nitro-control.desktop" "$SHAREDIR/icons/hicolor/scalable/apps/nitro-control.svg"
install -m 644 "$HERE/data/$APP_ID.desktop" "$SHAREDIR/applications/$APP_ID.desktop"
sed -i "s#^Exec=.*#Exec=$BINDIR/nitro-control#" "$SHAREDIR/applications/$APP_ID.desktop"
install -m 644 "$HERE/data/$APP_ID.svg" "$SHAREDIR/icons/hicolor/scalable/apps/$APP_ID.svg"
touch "$SHAREDIR/icons/hicolor" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$SHAREDIR/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$SHAREDIR/applications" 2>/dev/null || true
ok "Files installed"

# ---------------------------------------------------------------- service
if [ "$SERVICE" = 1 ]; then
    if [ "$INIT" = systemd ]; then
        sed -e "s#@LIBDIR@#$LIBDIR#g" -e "s#@PYTHON@#$PYTHON#g" \
            "$HERE/packaging/systemd/nitro-controld.service" > /etc/systemd/system/nitro-controld.service
        systemctl daemon-reload
        systemctl enable nitro-controld.service >/dev/null 2>&1
        systemctl restart nitro-controld.service
        sleep 1
        if systemctl is-active --quiet nitro-controld.service; then ok "Service nitro-controld running"
        else warn "Service failed to start — see: journalctl -u nitro-controld"; fi
    elif [ "$INIT" = openrc ]; then
        sed -e "s#@LIBDIR@#$LIBDIR#g" -e "s#@PYTHON@#$PYTHON#g" \
            "$HERE/packaging/openrc/nitro-controld" > /etc/init.d/nitro-controld
        chmod 755 /etc/init.d/nitro-controld
        rc-update add nitro-controld default >/dev/null 2>&1 || true
        rc-service nitro-controld restart && ok "Service nitro-controld running"
    else
        warn "No systemd or OpenRC found. Start the service at boot with your init system:"
        warn "    $BINDIR/nitro-controld"
    fi
fi

# ------------------------------------------------------------ RGB driver
rgb_prompt() {
    [ "$ASSUME_YES" = 1 ] && return 1          # never add a kernel driver without being asked
    [ -t 0 ] || return 1
    printf '\n'
    info "Optional: 4-zone RGB keyboard, 80% charge limiter, LCD overdrive and more need the"
    info "Linuwu-Sense kernel driver. It replaces acer_wmi (with automatic fallback), is built"
    info "with DKMS so it survives kernel updates, and can be removed with --uninstall."
    printf 'Set up the RGB keyboard driver now? [y/N] '
    read -r reply || reply=n
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

case "$VENDOR" in *Acer*) IS_ACER=1 ;; *) IS_ACER=0 ;; esac
# Already managed by Nitro Control: re-run the setup so driver fixes get applied
# (it does nothing when the installed build is current).
[ "$RGB" != no ] && [ -f /etc/modprobe.d/nitro-control-rgb.conf ] && RGB=yes
if [ "$RGB" = yes ] || { [ "$RGB" = ask ] && [ "$IS_ACER" = 1 ] && ! grep -q '^linuwu_sense ' /proc/modules && rgb_prompt; }; then
    if ASSUME_YES=$ASSUME_YES sh "$RGB_SETUP" install; then
        :
    else
        warn "RGB driver setup did not complete. Everything else works; retry later with:"
        warn "    sudo sh $RGB_SETUP install"
    fi
fi

# The desktop power widget (power-profiles-daemon) only reads the available thermal
# modes at start; refresh it in case the Acer driver changed since it started.
if grep -q '^linuwu_sense ' /proc/modules && [ -d /run/systemd/system ]; then
    systemctl try-restart power-profiles-daemon.service tuned-ppd.service 2>/dev/null || true
fi

# ------------------------------------------------------------ permissions
USER_NAME=${SUDO_USER:-${DOAS_USER:-}}
if [ -n "$USER_NAME" ] && [ "$USER_NAME" != root ]; then
    if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qxE 'wheel|sudo|admin|nitro'; then
        ok "$USER_NAME can change settings (admin group member)"
    else
        getent group nitro >/dev/null 2>&1 || groupadd -r nitro
        usermod -aG nitro "$USER_NAME"
        ok "Added $USER_NAME to group 'nitro' — log out and back in to change settings from the GUI"
    fi
fi

printf '\n'
ok "Done! Launch Nitro Control from your app menu, or run: nitro-control"
info "Command line: nitroctl status | nitroctl profile turbo | nitroctl fan curve"
