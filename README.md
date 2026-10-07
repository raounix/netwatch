# Netwatch

See which application is using your network — download and upload, live, per app.

- **Per application, not per connection.** Chrome's 30 connections are one row named *Google Chrome*, with its icon.
  Background services get a plain-language line — *snapd: Snap store — installs and automatic snap refreshes* — because
  those are usually the answer to "something is downloading and I didn't start anything".
- **Commands keep their own name.** `npm install` typed into WebStorm's terminal shows as *npm · started from WebStorm*,
  not as WebStorm.
- **Short-lived programs are caught.** A `curl` or `git fetch` that runs for a fraction of a second is still named.
- **Totals for the session**, peak rates, bytes or bits per second, and a per-process view.
- **Your hotspot, too.** Every phone or laptop on this computer's Wi-Fi Hotspot: its name, IP and MAC, maker, signal,
  how long it has been on, what it is using right now and today — and controls to cut it off, cap its download and
  upload speed, or give it a daily data limit.

## The hotspot tab

Turn on *Wi-Fi Hotspot* in GNOME Settings and the devices that join appear in the **Hotspot** tab. Click one:

| control | what happens |
|---|---|
| Internet access off | it stays on the Wi-Fi but nothing it sends is forwarded |
| Download / upload speed limit (MB/s) | packets beyond the rate are dropped — TCP settles at the limit |
| Daily data limit (MB) | download + upload together; once used up, it is cut off until midnight |
| Reset today's usage | starts its count (and its daily limit) from zero |
| Disconnect | kicks it off the Wi-Fi now; it can rejoin unless its access is off |

The limits are an `nftables` table (`inet netwatch`) on the forward path, so they are **enforced by the kernel and
keep working with Netwatch closed**. The same table counts every device's use per day, also while Netwatch is closed.
Two systemd units installed with it keep this true across reboots: `netwatch-hotspot.service` puts the table back at
boot with the day's usage so far and saves the usage at shutdown; `netwatch-hotspot-day.timer` starts a new day at
midnight (the previous day is kept in `/var/lib/netwatch/hotspot.json`, 14 days back).

Where the information comes from: the Wi-Fi driver over nl80211 (who is associated, signal, link speed, exact byte
counters), NetworkManager's dnsmasq leases (IP address and the name the device gives itself) and the kernel's
neighbour table. Phones usually join with a *private*, per-network MAC address, so they show no maker — but that
address stays the same on your network, which is what the limits are tied to.

Limits apply to IPv4 internet traffic, which is all a GNOME hotspot gives its devices by default. A device that
changes its MAC address (some phones can be told to use a new random one) looks like a new device.

## Install

```bash
sudo ./install.sh
```

Then open **Netwatch** from the app grid. It asks for your password (once every few minutes at most) and starts.
Needs Python 3 with GTK 4 and libadwaita bindings — preinstalled on Ubuntu 24.04+ and Fedora Workstation.

In a terminal, or over SSH on a server (the helper alone is enough there — it only needs Python 3):

```bash
sudo netwatch-top
```

Remove with `sudo ./uninstall.sh` — it also takes the hotspot limits out of the kernel at once.

## How it works

Seeing other programs' traffic needs root, and a graphical app should not run as root. So there are two parts:

| | runs as | does |
|---|---|---|
| `netwatch` | you | the window |
| `netwatch-helper` | root, through `pkexec` | counts packets, prints one JSON report a second |

The helper keeps a packet ring per network card that holds **only the first ~440 bytes of each packet** — the headers.
Payload is never copied out of the kernel. For every packet it finds the connection, the connection's socket in
`/proc/net/tcp` / `udp`, the process holding that socket in `/proc/<pid>/fd`, and the application from the process's
systemd unit (`app-gnome-google\x2dchrome-….scope` → *Google Chrome*). It never writes a file and never starts another
program. Closing the window closes the helper's input, and it exits.

The totals in the two cards come from the kernel's own interface counters, so they are exact; the per-app numbers come
from the capture and agree with them to within a couple of percent (header bytes that the network card merges).

## What it cannot see

- **Docker containers** show up as *Unattributed*: NAT rewrites their packets, and this kernel does not expose the
  NAT table. `docker pull` itself is attributed — that is the `docker` service.
- **Behind a VPN**, traffic inside the tunnel is shown under each app *and* again, encrypted, under the VPN client
  (the window says so in a banner). With a **proxy** client (Throne, v2ray, sing-box…), apps that use the proxy are
  counted under the proxy, because that is the program actually on the network.
- A program that opens a connection, transfers and closes it within about a millisecond — only possible on a local
  network — can finish before anyone can ask the kernel who it was.
- Hotspot devices' traffic is shown per device in the Hotspot tab and as one *Hotspot devices* row among the
  applications (it crosses the uplink NAT-rewritten, owned by no program on this computer).

## Development

```bash
./netwatch --demo                     # the window with made-up traffic, no root needed
python3 tests/test_helper.py          # packet parsing, /proc decoding, labels
python3 tests/e2e_netns.py            # real traffic in throwaway network namespaces — no root needed
python3 tests/e2e_hotspot.py         # a simulated hotspot: counting, limits, blocking, reboot, midnight
python3 -W ignore tests/gui_smoke.py  # drives the window on a GTK Broadway display
```

`tests/e2e_netns.py` builds two network namespaces joined by a veth cable with 10 ms of latency, runs TCP (IPv4, IPv6,
dual-stack), UDP (connected and not) and a short-lived client across it from separate processes, and checks every byte
lands on the right process — and that loopback and Docker-style bridges are not counted. `tests/e2e_hotspot.py`
builds a router out of three namespaces (a "phone" on an interface named wlo1, this computer forwarding and NAT-ing,
an "internet"), runs the real helper and talks to it over stdin like the window does: it measures a 1 MB/s limit
holding, a 2 MB daily limit cutting off at 2 MB, a block, and the usage surviving a "reboot" and a "midnight".
