#!/usr/bin/env bash
# StormWatch installer for Fedora (KDE Plasma or any other desktop).
#
#   packaging/install.sh                 install for the current user
#   packaging/install.sh --realtime      also allow real-time scheduling (re-login after)
#   packaging/install.sh --no-gvfs       stop GNOME's gvfs from grabbing the camera on plug-in
#   packaging/install.sh --no-dnf        do not install system packages
#
# The app goes to ~/.local/share/stormwatch, the launcher to ~/.local/bin.
# sudo is used for: dnf packages, udev rules, and (with --realtime) limits.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/stormwatch"
BINDIR="$HOME/.local/bin"
REALTIME=0
NO_GVFS=0
DNF=1

for arg in "$@"; do
  case "$arg" in
    --realtime) REALTIME=1 ;;
    --no-gvfs) NO_GVFS=1 ;;
    --no-dnf) DNF=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null; then SUDO="sudo"; else warn "no sudo: skipping system steps"; DNF=0; fi
fi

# 1. Python and libraries from Fedora (PySide6 from the system integrates with
#    the Plasma theme; python3-gphoto2 links the system libgphoto2 + udev rules).
if [ "$DNF" -eq 1 ] && command -v dnf >/dev/null; then
  say "Installing system packages (dnf)"
  $SUDO dnf install -y python3 python3-numpy python3-pillow python3-pyside6 python3-gphoto2 \
      libgphoto2 || warn "some packages failed; missing ones are installed with pip below"
fi

PY="$(command -v python3)"
missing=()
for mod in numpy PIL PySide6 gphoto2; do
  "$PY" -c "import $mod" 2>/dev/null || missing+=("$mod")
done
if [ "${#missing[@]}" -gt 0 ]; then
  say "Installing missing Python modules in a private environment: ${missing[*]}"
  "$PY" -m venv --system-site-packages "$APPDIR/venv"
  pip_names=()
  for m in "${missing[@]}"; do
    case "$m" in PIL) pip_names+=(pillow) ;; *) pip_names+=("$m") ;; esac
  done
  "$APPDIR/venv/bin/pip" install --quiet --upgrade "${pip_names[@]}"
  PY="$APPDIR/venv/bin/python"
fi

# 2. The app itself.
say "Installing StormWatch to $APPDIR"
mkdir -p "$APPDIR/app" "$BINDIR"
rm -rf "$APPDIR/app/stormwatch"
cp -r "$HERE/stormwatch" "$APPDIR/app/"
find "$APPDIR/app" -name '__pycache__' -prune -exec rm -rf {} +
cp -r "$HERE/firmware" "$APPDIR/" 2>/dev/null || true
cat > "$BINDIR/stormwatch" <<EOF
#!/bin/sh
PYTHONPATH="$APPDIR/app\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PY" -m stormwatch "\$@"
EOF
chmod +x "$BINDIR/stormwatch"

# 3. Menu entry and icon.
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$APPS" "$ICONS"
sed "s|@BIN@|$BINDIR/stormwatch|" "$HERE/packaging/stormwatch.desktop" > "$APPS/stormwatch.desktop"
cp "$HERE/stormwatch/ui/stormwatch.svg" "$ICONS/stormwatch.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

# 4. udev: camera never autosuspends, trigger cables usable without dialout,
#    ModemManager keeps its hands off them, FTDI latency 1 ms.
if { [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; } && command -v udevadm >/dev/null; then
  say "Installing udev rules"
  $SUDO install -m 0644 "$HERE/packaging/70-stormwatch.rules" /etc/udev/rules.d/70-stormwatch.rules
  if [ "$REALTIME" -eq 1 ]; then
    $SUDO install -m 0644 "$HERE/packaging/71-stormwatch-realtime.rules" /etc/udev/rules.d/71-stormwatch-realtime.rules
  fi
  $SUDO udevadm control --reload-rules
  $SUDO udevadm trigger --subsystem-match=usb --subsystem-match=tty --subsystem-match=usb-serial || true
  [ "$REALTIME" -eq 1 ] && $SUDO udevadm trigger --subsystem-match=misc || true
fi

# 5. Optional: real-time priority without rtkit's limits.
if [ "$REALTIME" -eq 1 ] && { [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; }; then
  say "Allowing real-time scheduling for $USER (takes effect after you log in again)"
  printf '# StormWatch: real-time priority for the detection thread\n%s - rtprio 60\n' "$USER" \
    | $SUDO tee /etc/security/limits.d/95-stormwatch.conf >/dev/null
fi

# 6. Optional: GNOME's gvfs grabs PTP cameras on plug-in (KDE apps may pull it
#    in too). StormWatch can release it from its banner, this stops it for good.
if [ "$NO_GVFS" -eq 1 ] && systemctl --user list-unit-files 2>/dev/null | grep -q gvfs-gphoto2-volume-monitor; then
  say "Stopping gvfs from grabbing cameras"
  systemctl --user mask --now gvfs-gphoto2-volume-monitor.service || true
fi

say "Done."
cat <<EOF

  Start it from the application menu (StormWatch) or run:  stormwatch
  Try it without hardware:                                 stormwatch --demo
  See what it finds:                                       stormwatch --list-devices

  Plug in the D3300 (USB, switched on), set the lens to M and the dial to M.
  Unplug and replug it once now so the new udev rules apply.
EOF
