#!/usr/bin/env bash
# Installs Netwatch for every user on this machine.   Run from this folder:  sudo ./install.sh
#
# The helper runs as root (through pkexec), so it is installed where only root
# can write. Left in a home folder, any program running as you could rewrite
# it and wait for the next time you type your password.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo:  sudo ./install.sh" >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/usr/local
LIB="$PREFIX/lib/netwatch"
POLKIT=/usr/share/polkit-1/actions        # polkit reads actions from here only, not /usr/local
UNITS=/etc/systemd/system
HOTSPOT_UNITS="netwatch-hotspot.service netwatch-hotspot-day.service netwatch-hotspot-day.timer"

for f in netwatch netwatch-helper data/local.netwatch.Netwatch.policy \
         data/local.netwatch.Netwatch.desktop data/local.netwatch.Netwatch.svg \
         data/netwatch-hotspot.service data/netwatch-hotspot-day.service data/netwatch-hotspot-day.timer; do
  [ -f "$SRC/$f" ] || { echo "missing $SRC/$f" >&2; exit 1; }
done
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null \
  || echo "warning: GTK 4 / libadwaita Python bindings not found — sudo apt install python3-gi gir1.2-adw-1" >&2
command -v nft >/dev/null \
  || echo "warning: nft not found — hotspot limits need it: sudo apt install nftables" >&2

install -d -o root -g root -m 0755 "$LIB"
install -o root -g root -m 0755 "$SRC/netwatch-helper" "$LIB/netwatch-helper"
install -o root -g root -m 0755 "$SRC/netwatch" "$PREFIX/bin/netwatch"
ln -sfn "$LIB/netwatch-helper" "$PREFIX/bin/netwatch-top"          # terminal view: sudo netwatch-top
install -D -o root -g root -m 0644 "$SRC/data/local.netwatch.Netwatch.policy" \
  "$POLKIT/local.netwatch.Netwatch.policy"
install -D -o root -g root -m 0644 "$SRC/data/local.netwatch.Netwatch.desktop" \
  "$PREFIX/share/applications/local.netwatch.Netwatch.desktop"
install -D -o root -g root -m 0644 "$SRC/data/local.netwatch.Netwatch.svg" \
  "$PREFIX/share/icons/hicolor/scalable/apps/local.netwatch.Netwatch.svg"

# Hotspot limits live in the kernel (nftables). These put them back after a
# reboot and start each day's usage from zero at midnight.
install -d -o root -g root -m 0700 /var/lib/netwatch
for u in $HOTSPOT_UNITS; do
  install -o root -g root -m 0644 "$SRC/data/$u" "$UNITS/$u"
done
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null; then
  { systemctl daemon-reload && systemctl enable --now netwatch-hotspot.service netwatch-hotspot-day.timer; } \
    || echo "warning: could not enable the hotspot service and timer — limits would not survive a reboot" >&2
fi

# Refresh the icon cache only if one is already there: creating one would hide
# icons that other software later drops into this folder without refreshing it.
if [ -f "$PREFIX/share/icons/hicolor/icon-theme.cache" ]; then
  for cache in gtk4-update-icon-cache gtk-update-icon-cache; do
    if command -v "$cache" >/dev/null; then "$cache" -qtf "$PREFIX/share/icons/hicolor" || true; break; fi
  done
fi
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" || true

echo "Netwatch installed."
echo "  Window:   open \"Netwatch\" from the app grid, or run: netwatch"
echo "  Terminal: sudo netwatch-top"
echo "  Remove:   sudo ./uninstall.sh"
