#!/bin/sh
# Optional RGB keyboard support for Nitro Control.
#
# Builds the Linuwu-Sense kernel driver (4-zone RGB, battery limiter, LCD overdrive, ...)
# from a pinned, hash-verified upstream commit, registers it with DKMS so it is rebuilt
# on every kernel update, and switches from acer_wmi to it, rolling back automatically
# if anything fails.
#
#   sudo sh setup-rgb.sh install     build + switch to Linuwu-Sense
#   sudo sh setup-rgb.sh remove      go back to the stock acer_wmi driver
#   sh setup-rgb.sh status           show what is active
#
# ASSUME_YES=1 installs missing build tools without asking.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=linuwu.env
. "$HERE/linuwu.env"

PKG=linuwu-sense
SRC_DIR=/usr/src/$PKG-$LINUWU_VERSION
MODPROBE_CONF=/etc/modprobe.d/nitro-control-rgb.conf
SYSFS_BASE=/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi
KVER=$(uname -r)
KDIR=/lib/modules/$KVER/build
ASSUME_YES=${ASSUME_YES:-0}

red() { printf '\033[91m✘ %s\033[0m\n' "$*" >&2; }
ok() { printf '\033[92m✔\033[0m %s\n' "$*"; }
info() { printf '\033[96m›\033[0m %s\n' "$*"; }
warn() { printf '\033[93m!\033[0m %s\n' "$*"; }

ask() {
    [ "$ASSUME_YES" = 1 ] && return 0
    printf '%s [Y/n] ' "$1"
    read -r reply || reply=n
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

need_root() {
    [ "$(id -u)" -eq 0 ] || { red "Run as root (sudo)."; exit 1; }
}

sha256() {
    python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
}

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
    else python3 -c 'import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
    fi
}

loaded() { grep -q "^$1 " /proc/modules 2>/dev/null; }

dkms_versions() {
    command -v dkms >/dev/null 2>&1 || return 0
    dkms status -m "$PKG" 2>/dev/null | sed -n "s#^$PKG[/, ]\{1,2\}\([^,:]*\).*#\1#p" | sort -u
}

secure_boot_on() {
    if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -q 'SecureBoot enabled'; then
        return 0
    fi
    for f in /sys/firmware/efi/efivars/SecureBoot-*; do
        [ -r "$f" ] || continue
        [ "$(od -An -t u1 -j4 -N1 "$f" | tr -d ' ')" = 1 ] && return 0
    done
    return 1
}

# --------------------------------------------------------------- services
SERVICE=""
detect_service() {
    if [ -d /run/systemd/system ] && systemctl cat nitro-controld.service >/dev/null 2>&1; then SERVICE=systemd
    elif [ -x /etc/init.d/nitro-controld ] && command -v rc-service >/dev/null 2>&1; then SERVICE=openrc
    fi
}
stop_service() {
    case "$SERVICE" in
        systemd) systemctl stop nitro-controld.service 2>/dev/null || true ;;
        openrc) rc-service nitro-controld stop >/dev/null 2>&1 || true ;;
    esac
}
start_service() {
    case "$SERVICE" in
        systemd) systemctl start nitro-controld.service 2>/dev/null || warn "could not start nitro-controld" ;;
        openrc) rc-service nitro-controld start >/dev/null 2>&1 || warn "could not start nitro-controld" ;;
    esac
}

# power-profiles-daemon (the desktop's power widget) reads the available thermal
# modes only at start-up, so it must be restarted whenever the driver changes.
restart_power_profiles() {
    if [ -d /run/systemd/system ]; then
        systemctl try-restart power-profiles-daemon.service tuned-ppd.service 2>/dev/null || true
    elif command -v rc-service >/dev/null 2>&1 && [ -x /etc/init.d/power-profiles-daemon ]; then
        rc-service power-profiles-daemon restart >/dev/null 2>&1 || true
    fi
}

# ------------------------------------------------------------ build tools
pkg_install() {
    CMD=""
    if command -v pacman >/dev/null 2>&1; then CMD="pacman -S --needed --noconfirm"
    elif command -v apt-get >/dev/null 2>&1; then CMD="apt-get install -y"
    elif command -v dnf >/dev/null 2>&1; then CMD="dnf install -y"
    elif command -v zypper >/dev/null 2>&1; then CMD="zypper install -y"
    elif command -v xbps-install >/dev/null 2>&1; then CMD="xbps-install -y"
    elif command -v emerge >/dev/null 2>&1; then CMD="emerge --noreplace"
    elif command -v eopkg >/dev/null 2>&1; then CMD="eopkg install -y"
    fi
    if [ -z "$CMD" ]; then
        red "Unknown package manager: please install $* yourself and re-run."
        return 1
    fi
    ask "Install build tools with: $CMD $* ?" || return 1
    # shellcheck disable=SC2086
    $CMD "$@"
}

headers_package() {
    if command -v pacman >/dev/null 2>&1; then
        kpkg=$(pacman -Qqo "/usr/lib/modules/$KVER/vmlinuz" 2>/dev/null || true)
        [ -n "$kpkg" ] && echo "$kpkg-headers"
    elif command -v apt-get >/dev/null 2>&1; then echo "linux-headers-$KVER"
    elif command -v dnf >/dev/null 2>&1; then echo "kernel-devel-$KVER"
    elif command -v zypper >/dev/null 2>&1; then echo "kernel-${KVER##*-}-devel"
    fi
}

ensure_tools() {
    missing=""
    command -v dkms >/dev/null 2>&1 || missing="$missing dkms"
    command -v make >/dev/null 2>&1 || missing="$missing make"
    if [ ! -f "$KDIR/Makefile" ]; then
        hp=$(headers_package)
        if [ -z "$hp" ]; then
            red "Kernel headers for $KVER are missing and I can't tell which package provides them."
            red "Install the headers for your running kernel, then re-run."
            exit 1
        fi
        missing="$missing $hp"
    fi
    if grep -qs '^CONFIG_CC_IS_CLANG=y' "$KDIR/.config"; then
        command -v clang >/dev/null 2>&1 || missing="$missing clang"
        command -v ld.lld >/dev/null 2>&1 || missing="$missing lld"
        command -v llvm-ar >/dev/null 2>&1 || missing="$missing llvm"
    else
        command -v gcc >/dev/null 2>&1 || missing="$missing gcc"
    fi
    if [ -n "$missing" ]; then
        info "Building the driver needs:$missing"
        # shellcheck disable=SC2086
        pkg_install $missing || { red "Build tools missing, RGB setup skipped."; exit 1; }
    fi
    [ -f "$KDIR/Makefile" ] || { red "Kernel headers for $KVER still missing (reboot into the latest kernel?)."; exit 1; }
}

# ---------------------------------------------------------------- install
dkms_cleanup() {
    for v in $(dkms_versions); do
        dkms remove "$PKG/$v" --all >/dev/null 2>&1 || true
    done
    rm -rf /usr/src/$PKG-*
}

wait_for_driver() {
    i=0
    while [ $i -lt 8 ]; do
        if [ -d "$SYSFS_BASE/four_zoned_kb" ] || [ -d "$SYSFS_BASE/nitro_sense" ] || [ -d "$SYSFS_BASE/predator_sense" ]; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

restore_stock() {
    rm -f "$MODPROBE_CONF"
    loaded linuwu_sense && modprobe -r linuwu_sense 2>/dev/null || true
    modprobe acer_wmi 2>/dev/null || warn "could not reload acer_wmi — a reboot will restore it"
}

do_install() {
    need_root
    detect_service
    vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo unknown)
    case "$vendor" in *Acer*) ;; *) red "Not an Acer laptop ($vendor): Linuwu-Sense is Acer-only."; exit 1 ;; esac

    if [ -f /etc/modprobe.d/blacklist-acer_wmi.conf ] || [ -f /etc/systemd/system/linuwu_sense.service ] \
        || [ -f "/lib/modules/$KVER/kernel/drivers/platform/x86/linuwu_sense.ko" ]; then
        warn "Linuwu-Sense is already installed manually (with its own 'make install')."
        warn "Nitro Control already works with it. To let Nitro Control manage it instead"
        warn "(automatic rebuilds on kernel updates, safe fallback), run 'sudo make uninstall'"
        warn "in your Linuwu-Sense folder first, then re-run this."
        exit 0
    fi

    if loaded linuwu_sense && [ -f "$MODPROBE_CONF" ] && dkms_versions | grep -qx "$LINUWU_VERSION"; then
        ok "RGB keyboard driver (Linuwu-Sense $LINUWU_VERSION) already installed and active"
        exit 0
    fi

    if secure_boot_on; then
        red "Secure Boot is enabled: the kernel will refuse an unsigned driver."
        red "Either disable Secure Boot in the BIOS or set up DKMS module signing (MOK), then re-run."
        exit 1
    fi

    ensure_tools

    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    base="https://raw.githubusercontent.com/$LINUWU_REPO/$LINUWU_COMMIT"
    info "Downloading Linuwu-Sense ${LINUWU_COMMIT%"${LINUWU_COMMIT#???????}"}…"
    fetch "$base/src/linuwu_sense.c" "$work/linuwu_sense.c" || { red "Download failed (are you online?)"; exit 1; }
    fetch "$base/LICENSE" "$work/LICENSE" || { red "Download failed (are you online?)"; exit 1; }
    [ "$(sha256 "$work/linuwu_sense.c")" = "$LINUWU_SRC_SHA256" ] || { red "Source checksum mismatch — refusing to build."; exit 1; }
    [ "$(sha256 "$work/LICENSE")" = "$LINUWU_LICENSE_SHA256" ] || { red "License checksum mismatch — refusing to build."; exit 1; }
    python3 "$HERE/patch_linuwu.py" "$work/linuwu_sense.c"
    [ "$(sha256 "$work/linuwu_sense.c")" = "$LINUWU_PATCHED_SHA256" ] || { red "Patched source checksum mismatch — refusing to build."; exit 1; }
    ok "Source verified and patched (kernel 7.x compatibility + safety fixes)"

    dkms_cleanup
    mkdir -p "$SRC_DIR/src"
    install -m 644 "$work/linuwu_sense.c" "$SRC_DIR/src/linuwu_sense.c"
    install -m 644 "$work/LICENSE" "$SRC_DIR/LICENSE"
    install -m 644 "$HERE/Kbuild.mk" "$SRC_DIR/Makefile"
    sed "s#@VERSION@#$LINUWU_VERSION#" "$HERE/dkms.conf.in" > "$SRC_DIR/dkms.conf"

    info "Building with DKMS for kernel $KVER (takes a minute)…"
    if ! dkms add "$PKG/$LINUWU_VERSION" >/dev/null || ! dkms build "$PKG/$LINUWU_VERSION" -k "$KVER" \
        || ! dkms install "$PKG/$LINUWU_VERSION" -k "$KVER"; then
        red "Build failed — your system is unchanged. Log: /var/lib/dkms/$PKG/$LINUWU_VERSION/build/make.log"
        dkms_cleanup
        exit 1
    fi
    ok "Driver built and registered with DKMS (rebuilds automatically on kernel updates)"

    info "Switching from acer_wmi to Linuwu-Sense…"
    stop_service  # the service hands the fans back to the firmware as it stops
    if loaded acer_wmi && ! modprobe -r acer_wmi; then
        red "Could not unload acer_wmi (in use?). Nothing changed; reboot and try again."
        start_service
        dkms_cleanup
        exit 1
    fi
    if modprobe linuwu_sense && wait_for_driver; then
        modprobe_bin=$(command -v modprobe)
        cat > "$MODPROBE_CONF" <<EOF
# Written by Nitro Control (packaging/linuwu/setup-rgb.sh).
# Load Linuwu-Sense in place of acer_wmi; if it is not built for the running kernel,
# fall back to the stock acer_wmi driver so fans and thermal profiles keep working.
install acer_wmi $modprobe_bin linuwu_sense 2>/dev/null || $modprobe_bin --ignore-install acer_wmi
EOF
        restart_power_profiles
        start_service
        if [ -d "$SYSFS_BASE/four_zoned_kb" ]; then
            ok "RGB keyboard ready — open the Lighting page in Nitro Control"
        else
            warn "Driver active, but this model has no 4-zone keyboard; the other extras are available."
        fi
    else
        red "Linuwu-Sense did not start on this laptop — restoring acer_wmi."
        restore_stock
        restart_power_profiles
        start_service
        dkms_cleanup
        exit 1
    fi
}

do_remove() {
    need_root
    detect_service
    if [ ! -f "$MODPROBE_CONF" ] && [ -z "$(dkms_versions)" ]; then
        ok "Nitro Control's RGB driver is not installed"
        return 0
    fi
    info "Switching back to the stock acer_wmi driver…"
    stop_service
    restore_stock
    rm -f /etc/predator_state /etc/four_zone_kb_state
    dkms_cleanup
    restart_power_profiles
    start_service
    ok "Linuwu-Sense removed; acer_wmi is back"
}

do_status() {
    if loaded linuwu_sense; then ok "Active driver: linuwu_sense"
    elif loaded acer_wmi; then info "Active driver: acer_wmi (stock)"
    else warn "No Acer driver loaded"; fi
    v=$(dkms_versions | tr '\n' ' ')
    [ -n "$v" ] && info "DKMS: $PKG $v" || info "DKMS: not installed"
    [ -f "$MODPROBE_CONF" ] && info "Boot: Linuwu-Sense preferred, acer_wmi fallback" || info "Boot: stock acer_wmi"
    [ -d "$SYSFS_BASE/four_zoned_kb" ] && ok "4-zone RGB keyboard available"
    return 0
}

case "${1:-}" in
    install) do_install ;;
    remove|uninstall) do_remove ;;
    status) do_status ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
