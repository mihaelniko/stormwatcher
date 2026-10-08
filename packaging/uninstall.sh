#!/usr/bin/env bash
# Remove what packaging/install.sh installed (settings and photos are kept).
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf "$DATA/stormwatch"
rm -f "$HOME/.local/bin/stormwatch" "$DATA/applications/stormwatch.desktop" \
      "$DATA/icons/hicolor/scalable/apps/stormwatch.svg"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
$SUDO rm -f /etc/udev/rules.d/70-stormwatch.rules /etc/udev/rules.d/71-stormwatch-realtime.rules \
            /etc/security/limits.d/95-stormwatch.conf
$SUDO udevadm control --reload-rules || true
systemctl --user unmask gvfs-gphoto2-volume-monitor.service 2>/dev/null || true
echo "StormWatch removed. Settings remain in ~/.config/stormwatch, photos in your Pictures folder."
