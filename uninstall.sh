#!/usr/bin/env bash
# Removes everything install.sh put in place.   sudo ./uninstall.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo:  sudo ./uninstall.sh" >&2; exit 1; }
PREFIX=/usr/local
UNITS=/etc/systemd/system

# Hotspot limits first: they are enforced by the kernel, so they would outlive
# the program until the next reboot.
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null; then
  systemctl disable --now netwatch-hotspot-day.timer netwatch-hotspot.service 2>/dev/null || true
fi
rm -f "$UNITS/netwatch-hotspot.service" "$UNITS/netwatch-hotspot-day.service" "$UNITS/netwatch-hotspot-day.timer"
[ -d /run/systemd/system ] && command -v systemctl >/dev/null && systemctl daemon-reload 2>/dev/null || true
if command -v nft >/dev/null && nft list table inet netwatch >/dev/null 2>&1; then
  nft delete table inet netwatch && echo "Hotspot limits removed from the kernel."
fi
# the limits you set and the usage history; a failure here must not leave the rest installed
rm -rf /var/lib/netwatch 2>/dev/null || echo "note: could not remove /var/lib/netwatch — delete it by hand" >&2

rm -f "$PREFIX/bin/netwatch" "$PREFIX/bin/netwatch-top" \
      "$PREFIX/lib/netwatch/netwatch-helper" \
      /usr/share/polkit-1/actions/local.netwatch.Netwatch.policy \
      "$PREFIX/share/applications/local.netwatch.Netwatch.desktop" \
      "$PREFIX/share/icons/hicolor/scalable/apps/local.netwatch.Netwatch.svg"
rmdir "$PREFIX/lib/netwatch" 2>/dev/null || true

# Refresh the icon cache only if one is already there: creating one would hide
# icons that other software later drops into this folder without refreshing it.
if [ -f "$PREFIX/share/icons/hicolor/icon-theme.cache" ]; then
  for cache in gtk4-update-icon-cache gtk-update-icon-cache; do
    if command -v "$cache" >/dev/null; then "$cache" -qtf "$PREFIX/share/icons/hicolor" || true; break; fi
  done
fi
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" || true
echo "Netwatch removed."
