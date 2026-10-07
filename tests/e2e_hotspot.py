#!/usr/bin/env python3
"""End-to-end test of the hotspot side of netwatch-helper — needs no root.

Inside unprivileged user + network + mount namespaces it builds a small
router: a "phone" namespace on an interface named wlo1 (with a dnsmasq-style
lease, as NetworkManager's hotspot writes it), this namespace forwarding and
NAT-ing to an "internet" namespace on wan0. Then it runs the real helper, talks
to it over stdin exactly like the window does, moves real traffic, and checks:

  the device is found, named and counted (kernel counters, per direction) ·
  the hotspot interface is kept out of the per-application capture ·
  its forwarded bytes leave "Unattributed" for the "Hotspot devices" row ·
  a 1 MB/s download limit holds · a 2 MB daily limit cuts off at 2 MB ·
  blocking cuts it off completely · bad commands are refused ·
  usage survives the helper exiting and a "reboot" (--restore) ·
  --new-day files the day away and starts from zero · and the systemd jobs
  keep their hands off while a window's helper owns the table.
"""
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
HELPER = str(HERE.parent / "netwatch-helper")
PHONE_IP = "10.42.0.85"
MB = 1_000_000

SERVER = r'''
import socket, threading
s = socket.create_server(("192.0.2.2", 8000))
def serve(c):
    f = c.makefile("rb"); line = f.readline().split()
    if not line: return
    n = int(line[1])
    try:
        if line[0] == b"GET":
            c.sendall(bytes(n))
        else:
            got = 0
            while got < n:
                d = f.read1(65536)
                if not d: break
                got += len(d)
            c.sendall(b"OK\n")
    except OSError: pass
    c.close()
while True:
    c, _ = s.accept(); threading.Thread(target=serve, args=(c,), daemon=True).start()
'''

CLIENT = r'''
import socket, sys, time, json
op, n, limit = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
t = time.monotonic(); moved = 0; ok = True
try:
    c = socket.create_connection(("192.0.2.2", 8000), timeout=3); c.settimeout(3)
    c.sendall(b"%s %d\n" % (op.encode(), n))
    if op == "GET":
        while moved < n and time.monotonic() - t < limit:
            d = c.recv(65536)
            if not d: break
            moved += len(d)
    else:
        chunk = bytes(65536)
        while moved < n:
            moved += c.send(chunk[:min(65536, n - moved)])
        c.settimeout(limit); c.recv(16)
except OSError:
    ok = False
print(json.dumps({"bytes": moved, "seconds": round(time.monotonic() - t, 2), "complete": ok and moved >= n}))
'''


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


class Helper:
    """The helper as the window runs it: JSON on stdout, commands on stdin."""

    def __init__(self):
        self.p = subprocess.Popen([HELPER, "--json", "--interval", "0.5"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.q = queue.Queue()
        threading.Thread(target=lambda: [self.q.put(json.loads(l)) for l in self.p.stdout], daemon=True).start()
        self.next_id = 0
        self.last_tick = None
        self.hello = self.wait(lambda m: m["type"] == "hello")

    def wait(self, pred, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                m = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if m["type"] == "tick":
                self.last_tick = m
            if pred(m):
                return m
        raise SystemExit("timed out waiting for the helper (stderr: %s)" % self.p.stderr.read() if self.p.poll() is not None else "")

    def tick(self):
        return self.wait(lambda m: m["type"] == "tick")

    def send(self, **cmd):
        self.next_id += 1
        cmd["id"] = self.next_id
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()
        return self.wait(lambda m: m["type"] == "ack" and m.get("id") == cmd["id"])

    def device(self, mac):
        hs = self.tick()["hotspot"]
        return next((d for d in hs["devices"] if d["mac"] == mac), None), hs

    def close(self):
        self.p.stdin.close()
        return self.p.wait(timeout=10)


def inside(work):
    failures = []

    def expect(cond, msg):
        print(("  ok    " if cond else "  FAIL  ") + msg)
        if not cond:
            failures.append(msg)

    # a private /var/lib (dnsmasq's leases, the helper's state) and /sys for this namespace
    varlib = work / "varlib"
    (varlib / "NetworkManager").mkdir(parents=True)
    sh("mount", "--bind", str(varlib), "/var/lib")
    sh("mount", "-t", "sysfs", "sysfs", "/sys")
    sh("ip", "link", "set", "lo", "up")

    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)     # never hold the caller's pipe open
    phone = subprocess.Popen(["unshare", "-n", "sleep", "600"], **quiet)
    net = subprocess.Popen(["unshare", "-n", "sleep", "600"], **quiet)
    CHILDREN.extend([phone, net])
    time.sleep(0.3)
    sh("ip", "link", "add", "wlo1", "type", "veth", "peer", "name", "cl0", "netns", str(phone.pid))
    sh("ip", "link", "add", "wan0", "type", "veth", "peer", "name", "inet0", "netns", str(net.pid))
    sh("ip", "addr", "add", "10.42.0.1/24", "dev", "wlo1")
    sh("ip", "addr", "add", "192.0.2.1/24", "dev", "wan0")
    for dev in ("wlo1", "wan0"):
        sh("ip", "link", "set", dev, "up")
    sh("ip", "route", "add", "default", "via", "192.0.2.2")
    pathlib.Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
    sh("nsenter", "-t", str(phone.pid), "-n", "sh", "-c",
       "ip link set lo up; ip addr add %s/24 dev cl0; ip link set cl0 up; ip route add default via 10.42.0.1" % PHONE_IP)
    sh("nsenter", "-t", str(net.pid), "-n", "sh", "-c",
       "ip link set lo up; ip addr add 192.0.2.2/24 dev inet0; ip link set inet0 up")
    mac = subprocess.run(["nsenter", "-t", str(phone.pid), "-n", "cat", "/sys/class/net/cl0/address"],
                         capture_output=True, text=True).stdout.strip()
    if not mac:     # /sys is this namespace's; ask the phone's namespace through ip(8)
        mac = subprocess.run(["nsenter", "-t", str(phone.pid), "-n", "ip", "-br", "link", "show", "cl0"],
                             capture_output=True, text=True).stdout.split()[2]
    (varlib / "NetworkManager" / "dnsmasq-wlo1.leases").write_text(
        "%d %s %s ali-phone 01:%s\n" % (int(time.time()) + 3600, mac, PHONE_IP, mac))
    sh("nft", "-f", "-", input=b"table ip lab_nat {\n chain post {\n  type nat hook postrouting priority srcnat;\n"
                                b"  oifname \"wan0\" masquerade\n }\n}\n")
    server = subprocess.Popen(["nsenter", "-t", str(net.pid), "-n", sys.executable, "-c", SERVER], **quiet)
    CHILDREN.append(server)
    time.sleep(0.4)

    def transfer(op, n, limit=15.0):
        out = subprocess.run(["nsenter", "-t", str(phone.pid), "-n", sys.executable, "-c", CLIENT, op, str(n), str(limit)],
                             capture_output=True, text=True, timeout=limit + 10).stdout
        return json.loads(out)

    print("helper on a simulated hotspot (phone %s, %s)" % (mac, PHONE_IP))
    h = Helper()
    CHILDREN.append(h.p)
    counted = {i["name"] for i in h.hello["ifaces"]}
    expect("wlo1" not in counted and "wan0" in counted,
           "the hotspot interface is kept out of the per-app capture (captured: %s)" % sorted(counted))
    h.tick()
    d, hs = h.device(mac)
    expect(hs["active"] and [i["name"] for i in hs["ifaces"]] == ["wlo1"], "hotspot wlo1 detected")
    expect(hs["controls"], "controls available (error: %s)" % hs.get("error"))
    expect(d is not None and d["ip"] == PHONE_IP and d["hostname"] == "ali-phone",
           "the phone is listed with its IP and lease name: %s" % ((d and (d["ip"], d["hostname"])),))

    print("counting")
    transfer("GET", 3 * MB)
    transfer("PUT", 1 * MB)
    hotspot_row = [0, 0]
    unattributed = [0, 0]
    for _ in range(4):
        t = h.tick()
        for p in t["procs"]:
            if p["app"] == "other:hotspot":
                hotspot_row[0] += p["rx"]
                hotspot_row[1] += p["tx"]
            elif p["app"] == "other:unattributed":
                unattributed[0] += p["rx"]
                unattributed[1] += p["tx"]
    d, hs = h.device(mac)
    expect(3 * MB <= d["today_down"] <= 3.1 * MB, "today ↓ %s bytes (3 MB moved)" % f"{d['today_down']:,}")
    expect(1 * MB <= d["today_up"] <= 1.1 * MB, "today ↑ %s bytes (1 MB moved)" % f"{d['today_up']:,}")
    expect(3 * MB <= hotspot_row[0] <= 3.2 * MB and 1 * MB <= hotspot_row[1] <= 1.2 * MB,
           "\"Hotspot devices\" row: ↓%s ↑%s" % (f"{hotspot_row[0]:,}", f"{hotspot_row[1]:,}"))
    expect(sum(unattributed) < 0.03 * sum(hotspot_row),
           "the forwarded bytes left \"Unattributed\" (left over: %s)" % f"{sum(unattributed):,}")

    print("limits")
    for bad, why in (({"cmd": "set_device", "mac": "zz:zz:zz:zz:zz:zz", "blocked": False}, "bad MAC"),
                     ({"cmd": "set_device", "mac": mac, "blocked": False, "down": -5}, "negative limit"),
                     ({"cmd": "set_device", "mac": mac, "blocked": False, "quota": 10}, "quota below 1 MB"),
                     ({"cmd": "set_device", "mac": mac, "blocked": "yes"}, "blocked not a boolean"),
                     ({"cmd": "set_device", "mac": mac, "blocked": False, "name": "x" * 200}, "name too long"),
                     ({"cmd": "format_disk", "mac": mac}, "unknown command")):
        ack = h.send(**bad)
        expect(not ack["ok"], "refused (%s): %s" % (why, ack.get("error")))
    ack = h.send(cmd="set_device", mac=mac, blocked=False, down=1 * MB, up=None, quota=None, name="Ali's phone")
    expect(ack["ok"], "1 MB/s download limit accepted")
    r = transfer("GET", 5 * MB, limit=20)
    rate = r["bytes"] / r["seconds"] / MB
    expect(r["complete"] and 0.8 <= rate <= 1.35, "5 MB took %.1f s = %.2f MB/s" % (r["seconds"], rate))
    d, _ = h.device(mac)
    expect(d["name"] == "Ali's phone" and d["rules"]["down"] == MB, "the name and the limit are reported back")

    ack = h.send(cmd="set_device", mac=mac, blocked=False, down=None, up=None, quota=2 * MB, name="Ali's phone")
    ack2 = h.send(cmd="reset_today", mac=mac)
    expect(ack["ok"] and ack2["ok"], "2 MB daily limit set, today's usage reset")
    r = transfer("GET", 5 * MB, limit=6)
    expect(1.6 * MB <= r["bytes"] <= 2.05 * MB and not r["complete"],
           "a 5 MB download stopped after %s bytes" % f"{r['bytes']:,}")
    d, _ = h.device(mac)
    expect(d["quota_used"] is not None and d["quota_used"] >= 2 * MB, "quota reported used: %s" % f"{d['quota_used']:,}")

    ack = h.send(cmd="set_device", mac=mac, blocked=True, down=None, up=None, quota=None, name="Ali's phone")
    r = transfer("GET", 100_000, limit=4)
    expect(ack["ok"] and r["bytes"] == 0, "blocked: %s bytes got through" % r["bytes"])

    print("persistence")
    h.send(cmd="set_device", mac=mac, blocked=False, down=None, up=None, quota=2 * MB, name="Ali's phone")
    h.send(cmd="reset_today", mac=mac)
    transfer("GET", 1 * MB)
    h.tick()
    rival = subprocess.run([HELPER, "--new-day"], capture_output=True, text=True)
    state_before = json.loads((varlib / "netwatch" / "hotspot.json").read_text())
    expect(rival.returncode == 0 and "history" in state_before,
           "--new-day steps aside while a window's helper owns the table")
    code = h.close()
    state = json.loads((varlib / "netwatch" / "hotspot.json").read_text())
    saved = state["devices"].get(mac, {})
    expect(code == 0 and saved.get("quota") == 2 * MB and saved.get("used_down", 0) >= MB,
           "state saved on exit (used ↓ %s, quota %s)" % (f"{saved.get('used_down', 0):,}", saved.get("quota")))
    mode = oct((varlib / "netwatch" / "hotspot.json").stat().st_mode & 0o777)
    expect(mode == "0o600", "state file is private (%s)" % mode)

    sh("nft", "delete", "table", "inet", "netwatch")            # "reboot": the kernel forgot everything
    restore = subprocess.run([HELPER, "--restore"], capture_output=True, text=True)
    r = transfer("GET", 5 * MB, limit=6)
    expect(restore.returncode == 0 and 0.6 * MB <= r["bytes"] <= 1.1 * MB,
           "after --restore the day's 1 MB still counts: only %s more bytes allowed" % f"{r['bytes']:,}")

    state = json.loads((varlib / "netwatch" / "hotspot.json").read_text())
    state["day"] = "2001-01-01"                                  # pretend midnight passed
    (varlib / "netwatch" / "hotspot.json").write_text(json.dumps(state))
    newday = subprocess.run([HELPER, "--new-day"], capture_output=True, text=True)
    state = json.loads((varlib / "netwatch" / "hotspot.json").read_text())
    expect(newday.returncode == 0 and "2001-01-01" in state["history"] and state["day"] != "2001-01-01",
           "--new-day filed yesterday under its date (%s)" % sorted(state["history"]))
    r = transfer("GET", 1 * MB, limit=6)
    expect(r["complete"], "the new day starts from zero: 1 MB allowed again")

    return failures


CHILDREN = []


def main():
    if os.environ.get("NETWATCH_E2E") != "inside":
        os.execvpe("unshare", ["unshare", "-rnm", "--propagation", "private", sys.executable, __file__],
                   dict(os.environ, NETWATCH_E2E="inside"))
    work = pathlib.Path(tempfile.mkdtemp(prefix="netwatch-hotspot-"))
    try:
        failures = inside(work)
    finally:
        for child in CHILDREN:
            if child.poll() is None:
                child.kill()
        shutil.rmtree(work, ignore_errors=True)
    print("\nPASS" if not failures else "\nFAIL — %d check(s)" % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
