#!/usr/bin/env python3
"""End-to-end test of netwatch-helper — needs no root.

Re-runs itself inside an unprivileged user + network namespace ("A"), joins it
to a second namespace ("B") with a veth cable, drives traffic of known size
across the cable from separate processes, and checks that the helper put every
byte on the right process:

  TCP over IPv4 both ways · TCP to a dual-stack (::) listener over IPv4 ·
  TCP over IPv6 · UDP into an unconnected socket · UDP out of a connected one ·
  a short-lived client that exits right after its download ·
  loopback traffic (must NOT be counted) · a container-style bridge (must not be captured)

and that the helper exits on its own when its stdin is closed — that is how
the window stops it.

The cable gets 5 ms of delay each way (netem): a 10 ms round trip, still much
faster than a typical internet path. Without any delay a whole connection —
open, 300 kB, close — fits in under 3 ms, and a program that is gone before
its first packet is even processed cannot be named by anything short of eBPF.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
HELPER = pathlib.Path(os.environ.get("NETWATCH_HELPER", HERE.parent / "netwatch-helper"))
A4, B4, A6, B6 = "10.9.0.1", "10.9.0.2", "fd00::1", "fd00::2"
MB = 1_000_000

# What each process in A moves, in payload bytes (the wire adds headers).
EXPECT = {
    "tcp4":    {"tx": 4 * MB, "rx": 1.5 * MB},
    "dual":    {"tx": 1 * MB, "rx": 0},
    "tcp6":    {"tx": 1 * MB, "rx": 0},
    "udp_rx":  {"tx": 0, "rx": 800 * 1000},
    "udp_tx":  {"tx": 500 * 1200, "rx": 0},
    "short":   {"tx": 0, "rx": 300_000},
}

# ── the programs, each run as its own process ──────────────────────────────
PROGRAMS = {
    "tcp4": f"""
import socket
s = socket.create_server(("{A4}", 5001)); c, _ = s.accept()
c.sendall(bytes({4 * MB}))
got = 0
while got < {int(1.5 * MB)}:
    d = c.recv(65536)
    if not d: break
    got += len(d)
""",
    "dual": """
import socket
s = socket.create_server(("::", 5002), family=socket.AF_INET6, dualstack_ipv6=True); c, _ = s.accept()
c.sendall(bytes(1000000)); c.shutdown(socket.SHUT_WR); c.recv(1)
""",
    "tcp6": f"""
import socket
s = socket.create_server(("{A6}", 5003), family=socket.AF_INET6); c, _ = s.accept()
c.sendall(bytes(1000000)); c.shutdown(socket.SHUT_WR); c.recv(1)
""",
    "udp_rx": """
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", 7000)); s.settimeout(1.5)
try:
    while True: s.recv(2048)
except socket.timeout: pass
""",
    "udp_tx": f"""
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("{B4}", 7001))
for i in range(500):
    s.send(bytes(1200))
    if i % 50 == 49: time.sleep(0.02)
""",
    "short": f"""
import socket
c = socket.create_connection(("{B4}", 7100)); got = 0
while True:
    d = c.recv(65536)
    if not d: break
    got += len(d)
""",
    "lo_server": """
import socket
s = socket.create_server(("127.0.0.1", 5999)); c, _ = s.accept(); c.sendall(bytes(2000000)); c.close()
""",
    "lo_client": """
import socket
c = socket.create_connection(("127.0.0.1", 5999))
while c.recv(65536): pass
""",
}

PEER = f"""
import socket, threading, time, sys, os, subprocess
work = sys.argv[1]
open(work + "/peer-started", "w").close()
while "veth1" not in [n for _, n in socket.if_nameindex()]:
    time.sleep(0.02)
for cmd in (["ip", "link", "set", "lo", "up"],
            ["ip", "addr", "add", "{B4}/24", "dev", "veth1"],
            ["ip", "-6", "addr", "add", "{B6}/64", "dev", "veth1", "nodad"],
            ["ip", "link", "set", "veth1", "up"],
            ["tc", "qdisc", "add", "dev", "veth1", "root", "netem", "delay", "5ms", "limit", "100000"]):
    subprocess.run(cmd, check=True)

def absorb():
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("0.0.0.0", 7001)); u.settimeout(8)
    try:
        while True: u.recv(2048)
    except socket.timeout: pass

def serve_short():
    s = socket.create_server(("0.0.0.0", 7100)); c, _ = s.accept(); c.sendall(bytes(300000)); c.close()

threads = [threading.Thread(target=absorb), threading.Thread(target=serve_short)]
for t in threads: t.start()
open(work + "/peer-ready", "w").close()
while not os.path.exists(work + "/go"):
    time.sleep(0.02)

def tcp4():
    c = socket.create_connection(("{A4}", 5001)); got = 0
    while got < {4 * MB}:
        d = c.recv(65536)
        if not d: break
        got += len(d)
    c.sendall(bytes({int(1.5 * MB)})); c.close()

def drain(addr, family=socket.AF_INET):
    c = socket.socket(family, socket.SOCK_STREAM); c.connect(addr)
    while c.recv(65536): pass
    c.close()

def udp():
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(800):
        u.sendto(bytes(1000), ("{A4}", 7000))
        if i % 50 == 49: time.sleep(0.02)

work_threads = [threading.Thread(target=tcp4),
                threading.Thread(target=drain, args=(("{A4}", 5002),)),
                threading.Thread(target=drain, args=(("{A6}", 5003, 0, 0), socket.AF_INET6)),
                threading.Thread(target=udp)]
for t in work_threads: t.start()
for t in work_threads + threads: t.join()
"""


def sh(*cmd):
    subprocess.run(cmd, check=True)


def wait_for(path, timeout=10):
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > end:
            raise SystemExit(f"timed out waiting for {path.name}")
        time.sleep(0.02)


def inside(work):
    sh("mount", "-t", "sysfs", "sysfs", "/sys")       # so /sys/class/net shows this namespace
    sh("ip", "link", "set", "lo", "up")

    peer = subprocess.Popen(["unshare", "-n", sys.executable, "-c", PEER, str(work)])
    wait_for(work / "peer-started")
    sh("ip", "link", "add", "veth0", "type", "veth", "peer", "name", "veth1", "netns", str(peer.pid))
    sh("ip", "addr", "add", f"{A4}/24", "dev", "veth0")
    sh("ip", "-6", "addr", "add", f"{A6}/64", "dev", "veth0", "nodad")
    sh("ip", "link", "set", "veth0", "up")
    sh("tc", "qdisc", "add", "dev", "veth0", "root", "netem", "delay", "5ms", "limit", "100000")
    # a Docker-style bridge with one veth port: neither may be captured
    sh("ip", "link", "add", "br0", "type", "bridge")
    sh("ip", "link", "add", "vethb0", "type", "veth", "peer", "name", "vethb1")
    sh("ip", "link", "set", "vethb0", "master", "br0")
    for dev in ("br0", "vethb0", "vethb1"):
        sh("ip", "link", "set", dev, "up")
    wait_for(work / "peer-ready")

    procs = {name: subprocess.Popen([sys.executable, "-c", PROGRAMS[name]])
             for name in ("tcp4", "dual", "tcp6", "udp_rx", "lo_server")}
    time.sleep(0.3)                                     # listeners up

    helper = subprocess.Popen([str(HELPER), "--json", "--interval", "0.5"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    hello = json.loads(helper.stdout.readline())
    assert hello["type"] == "hello", hello

    (work / "go").touch()
    for name in ("udp_tx", "short", "lo_client"):
        procs[name] = subprocess.Popen([sys.executable, "-c", PROGRAMS[name]])
    for name, p in procs.items():
        if p.wait(timeout=30) != 0:
            raise SystemExit(f"traffic program {name} failed")
    peer.wait(timeout=30)
    time.sleep(1.2)                                     # let the last interval close

    started = time.monotonic()
    helper.stdin.close()                                # what the window does on quit
    lines = list(helper.stdout)
    ticks = [json.loads(line) for line in lines]
    if os.environ.get("NETWATCH_E2E_DUMP"):             # a real recording for tests/gui_smoke.py
        with open(os.environ["NETWATCH_E2E_DUMP"], "w") as f:
            f.write(json.dumps(hello) + "\n" + "".join(lines))
    code = helper.wait(timeout=5)
    exit_after = time.monotonic() - started
    return hello, ticks, code, exit_after, {n: p.pid for n, p in procs.items()}


def check(hello, ticks, code, exit_after, pids):
    failures = []

    def expect(cond, msg):
        print(("  ok    " if cond else "  FAIL  ") + msg)
        if not cond:
            failures.append(msg)

    counted = {i["name"] for i in hello["ifaces"]}
    print("interfaces captured:", sorted(counted))
    expect("veth0" in counted, "the veth uplink is captured")
    expect(not counted & {"lo", "br0", "vethb0"}, "loopback, the bridge and its port are not captured")

    per_pid, per_app, unattr, total = {}, {}, [0, 0], [0, 0]
    for t in ticks:
        assert t["type"] == "tick", t
        total[0] += t["rx"]
        total[1] += t["tx"]
        for p in t["procs"]:
            if p["pid"]:
                acc = per_pid.setdefault(p["pid"], [0, 0])
                acc[0] += p["rx"]
                acc[1] += p["tx"]
                per_app[p["pid"]] = (p["app"], t["apps"][p["app"]]["kind"], p["comm"])
            elif p["app"] == "other:unattributed":
                unattr[0] += p["rx"]
                unattr[1] += p["tx"]
    print(f"ticks: {len(ticks)}   drops: {sum(t['drops'] for t in ticks)}   "
          f"uplink counters ↓{total[0]:,} ↑{total[1]:,}   unattributed ↓{unattr[0]:,} ↑{unattr[1]:,}")

    for name, want in EXPECT.items():
        got = per_pid.get(pids[name], [0, 0])
        for d, i in (("rx", 0), ("tx", 1)):
            w = want[d]
            if w:
                ok = w <= got[i] <= w * 1.12             # payload + headers, never less than the payload
                expect(ok, f"{name:7} {d}: {got[i]:>10,} bytes (payload {int(w):,})")
            else:
                expect(got[i] < 60_000, f"{name:7} {d}: {got[i]:>10,} bytes (only ACKs expected)")
    for name in ("lo_server", "lo_client"):
        expect(pids[name] not in per_pid, f"{name} (loopback) is not counted")

    attributed = sum(sum(v) for v in per_pid.values())
    expect(sum(unattr) < 0.02 * attributed, f"unattributed is under 2% ({sum(unattr):,} of {attributed:,})")
    expect(sum(t["drops"] for t in ticks) == 0, "the ring dropped nothing")
    captured = sum(v[0] for v in per_pid.values()) + unattr[0]
    expect(abs(captured - total[0]) <= 0.05 * total[0],
           f"captured ↓ agrees with the kernel counters within 5% ({captured:,} vs {total[0]:,})")
    expect(code == 0 and exit_after < 2.5, f"helper exits on stdin EOF (code {code}, {exit_after:.2f}s)")
    sample = per_app.get(pids["tcp4"])
    print("app the tcp4 server was grouped under:", sample)
    return failures


def main():
    if os.environ.get("NETWATCH_E2E") != "inside":
        os.execvpe("unshare", ["unshare", "-rnm", "--propagation", "private",
                               sys.executable, __file__], dict(os.environ, NETWATCH_E2E="inside"))
    import tempfile
    with tempfile.TemporaryDirectory(prefix="netwatch-e2e-") as tmp:
        result = inside(pathlib.Path(tmp))
    failures = check(*result)
    print("\nPASS" if not failures else f"\nFAIL — {len(failures)} check(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
