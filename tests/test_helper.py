#!/usr/bin/env python3
"""Unit tests for netwatch-helper's parsing — the cases a live capture rarely
produces: IPv6 extension headers, fragments, IPv4 options, truncated and
non-IP packets, the kernel's /proc/net address format, dual-stack lookups."""
import importlib.machinery
import pathlib
import socket
import struct
import unittest

HELPER = pathlib.Path(__file__).resolve().parent.parent / "netwatch-helper"
h = importlib.machinery.SourceFileLoader("netwatch_helper", str(HELPER)).load_module()

LOCAL4, REMOTE4 = socket.inet_aton("192.168.3.10"), socket.inet_aton("1.2.3.4")
LOCAL6, REMOTE6 = socket.inet_pton(socket.AF_INET6, "fd00::10"), socket.inet_pton(socket.AF_INET6, "2001:db8::4")


def ipv4(proto, src, dst, payload, frag_offset=0, options=b""):
    ihl = 5 + len(options) // 4
    total = ihl * 4 + len(payload)
    hdr = struct.pack("!BBHHHBBH4s4s", 0x40 | ihl, 0, total, 1, frag_offset & 0x1FFF, 64, proto, 0, src, dst)
    return hdr + options + payload


def ipv6(next_header, src, dst, payload):
    return struct.pack("!IHBB16s16s", 6 << 28, len(payload), next_header, 64, src, dst) + payload


def ports(sport, dport, rest=b"\0" * 16):
    return struct.pack("!HH", sport, dport) + rest


class Packets(unittest.TestCase):
    def setUp(self):
        self.m = h.Monitor(1.0, None, lambda msg: None)
        self.m.resolve = lambda flows, now: None        # keep lookups out of these tests

    def feed(self, pkt, outgoing, plen=None):
        self.m.packet(bytes(pkt), 0, len(pkt), plen or len(pkt), outgoing, 0.0)

    def placed(self, flow, pid=4242):
        self.m.flow_pid[flow] = pid
        return pid

    def test_ipv4_tcp_both_directions(self):
        pid = self.placed((6, LOCAL4, 50000, REMOTE4, 443))
        self.feed(ipv4(6, LOCAL4, REMOTE4, ports(50000, 443)), outgoing=True, plen=1500)
        self.feed(ipv4(6, REMOTE4, LOCAL4, ports(443, 50000)), outgoing=False, plen=900)
        self.assertEqual(self.m.pid_bytes[pid], [900, 1500])

    def test_ipv4_options_move_the_ports(self):
        pid = self.placed((17, LOCAL4, 5353, REMOTE4, 53))
        self.feed(ipv4(17, LOCAL4, REMOTE4, ports(5353, 53), options=b"\x01\x01\x01\x00"), outgoing=True, plen=100)
        self.assertEqual(self.m.pid_bytes[pid], [0, 100])

    def test_ipv4_later_fragment_has_no_ports(self):
        self.feed(ipv4(17, REMOTE4, LOCAL4, b"\xab" * 40, frag_offset=185), outgoing=False, plen=60)
        self.assertEqual(self.m.pid_bytes[h.OTHER_PROTOCOLS], [60, 0])
        self.assertFalse(self.m.pending)

    def test_ipv6_hop_by_hop_then_udp(self):
        pid = self.placed((17, LOCAL6, 40000, REMOTE6, 443))
        hbh = bytes([17, 0]) + b"\x01\x04\x00\x00\x00\x00"          # next = UDP, 8 bytes long
        self.feed(ipv6(0, REMOTE6, LOCAL6, hbh + ports(443, 40000)), outgoing=False, plen=1252)
        self.assertEqual(self.m.pid_bytes[pid], [1252, 0])

    def test_ipv6_first_fragment_keeps_ports_later_one_does_not(self):
        pid = self.placed((17, LOCAL6, 40000, REMOTE6, 443))
        first = struct.pack("!BBHI", 17, 0, 0 << 3 | 1, 7)          # offset 0, more fragments
        later = struct.pack("!BBHI", 17, 0, 185 << 3, 7)
        self.feed(ipv6(44, REMOTE6, LOCAL6, first + ports(443, 40000)), outgoing=False, plen=1280)
        self.feed(ipv6(44, REMOTE6, LOCAL6, later + b"\0" * 24), outgoing=False, plen=600)
        self.assertEqual(self.m.pid_bytes[pid], [1280, 0])
        self.assertEqual(self.m.pid_bytes[h.OTHER_PROTOCOLS], [600, 0])

    def test_non_tcp_udp_and_garbage_go_to_other_protocols(self):
        self.feed(ipv4(1, LOCAL4, REMOTE4, b"\x08\x00" + b"\0" * 30), outgoing=True, plen=98)    # ICMP echo
        self.feed(b"\x00\x01\x08\x00\x06\x04" + b"\0" * 22, outgoing=True, plen=42)              # ARP
        self.feed(ipv4(6, LOCAL4, REMOTE4, b"")[:12], outgoing=True, plen=12)                   # truncated
        self.assertEqual(self.m.pid_bytes[h.OTHER_PROTOCOLS], [0, 98 + 42 + 12])

    def test_tcp_header_cut_off_by_the_snap_length(self):
        pkt = ipv4(6, LOCAL4, REMOTE4, ports(50000, 443))
        self.m.packet(bytes(pkt), 0, 22, 1500, True, 0.0)       # only 2 bytes of TCP captured
        self.assertEqual(self.m.pid_bytes[h.OTHER_PROTOCOLS], [0, 1500])

    def test_unknown_flow_is_held_back_not_lost(self):
        self.feed(ipv4(6, REMOTE4, LOCAL4, ports(443, 50001)), outgoing=False, plen=700)
        flow = (6, LOCAL4, 50001, REMOTE4, 443)
        self.assertEqual(self.m.pending[flow], [700, 0])
        self.m.assign(flow, 77, 0.0)                                 # owner found later
        self.assertEqual(self.m.pid_bytes[77], [700, 0])
        self.assertNotIn(flow, self.m.pending)

    def test_pending_is_capped(self):
        self.m.pending = {("dummy", i): [0, 0] for i in range(h.MAX_PENDING)}
        self.feed(ipv4(6, REMOTE4, LOCAL4, ports(443, 50002)), outgoing=False, plen=321)
        self.assertEqual(self.m.pid_bytes[h.UNATTRIBUTED], [321, 0])
        self.assertEqual(len(self.m.pending), h.MAX_PENDING)


class Kernel(unittest.TestCase):
    def test_proc_net_addresses(self):
        cache = {}
        self.assertEqual(h._addr(b"0100007F:1F90", cache), (socket.inet_aton("127.0.0.1"), 8080))
        # tcp6 prints each 32-bit word in host order; ::ffff:10.9.0.1 must come back as plain IPv4
        self.assertEqual(h._addr(b"0000000000000000FFFF00000100090A:1389", cache),
                         (socket.inet_aton("10.9.0.1"), 5001))
        self.assertEqual(h._addr(b"000000FD000000000000000001000000:0050", cache),
                         (socket.inet_pton(socket.AF_INET6, "fd00::1"), 80))

    def test_dual_stack_listener_and_udp_lookups(self):
        t = h.SocketTable()
        t.bound[h.TCP6][(h.ANY6, 8080)] = 11               # [::]:8080 accepts IPv4 too
        t.conn[h.TCP4][(LOCAL4, 8080, REMOTE4, 5555)] = 12
        t.bound[h.UDP4][(h.ANY4, 5353)] = 13
        t.conn[h.UDP6][(LOCAL6, 40000, REMOTE6, 443)] = 14
        self.assertEqual(t.lookup((6, LOCAL4, 8080, REMOTE4, 5555)), 12)    # exact connection wins
        self.assertEqual(t.lookup((6, LOCAL4, 8080, REMOTE4, 6666)), 11)    # new SYN → the listener
        self.assertEqual(t.listener((6, LOCAL4, 8080, REMOTE4, 5555)), 11)
        self.assertEqual(t.lookup((17, LOCAL4, 5353, REMOTE4, 5353)), 13)
        self.assertEqual(t.lookup((17, LOCAL6, 40000, REMOTE6, 443)), 14)
        self.assertEqual(t.lookup((17, LOCAL6, 40001, REMOTE6, 443)), 0)
        self.assertEqual(t.files((6, LOCAL6, 1, REMOTE6, 2)), (h.TCP6,))   # IPv6 never reads the IPv4 table


class Names(unittest.TestCase):
    def test_program_labels(self):
        cases = [
            (("node", "/usr/bin/node", ["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "install"]), "npm"),
            (("python3", "/usr/bin/python3.14", ["python3", "-m", "pip", "install", "x"]), "pip"),
            (("python3", "/usr/bin/python3.14", ["python3", "-c", "print(1)"]), "python3.14"),
            (("python3", "/usr/bin/python3.14", ["python3", "-u", "/srv/app/worker.py"]), "worker"),
            (("curl", "/usr/bin/curl", ["curl", "-O", "https://x"]), "curl"),
            (("chrome", "/opt/google/chrome/chrome (deleted)", ["chrome"]), "chrome"),
        ]
        for (comm, exe, argv), want in cases:
            self.assertEqual(h.program_label(comm, exe, argv), want, argv)

    def test_control_characters_are_stripped(self):
        self.assertEqual(h.clean("evil\x1b[2Jname\n"), "evil?[2Jname?")

    def test_interval_and_interface_validation(self):
        for bad in (["--interval", "0.01"], ["--interfaces", "eth0;rm -rf /"], ["--interfaces", "a" * 16]):
            with self.assertRaises(SystemExit):
                h.main(bad + ["--json"])


class Hardening(unittest.TestCase):
    def test_undecodable_names_become_valid_utf8(self):
        name = "ev\udcffil"                       # how os.readlink() hands back a non-UTF-8 byte
        self.assertEqual(h.clean(name), "ev?il")
        h.clean(name).encode("utf-8")              # must not raise

    def test_app_cache_is_bounded(self):
        c = h.ProcCache()
        for i in range(3000):
            c.apps["proc:1000:job%05d" % i] = {"kind": "process"}
        c.prune()
        self.assertLessEqual(len(c.apps), 1024)

    def test_a_flood_of_new_flows_does_not_grow_the_maps(self):
        m = h.Monitor(1.0, None, lambda msg: None, hotspot=False)
        calls = []
        m.resolve = lambda flows, now: calls.append(flows)
        m.pending = {("dummy", i): [0, 0] for i in range(h.MAX_PENDING)}
        for port in range(1000, 3000):             # 2000 new flows after the cap
            pkt = bytes(ipv4(6, REMOTE4, LOCAL4, ports(443, port)))
            m.packet(pkt, 0, len(pkt), 60, False, 0.0)
        self.assertEqual(calls, [])
        self.assertEqual(len(m.retry_at), 0)
        self.assertEqual(len(m.unresolved), 0)
        self.assertEqual(m.pid_bytes[h.UNATTRIBUTED], [2000 * 60, 0])


def genl_msg(seq, family, attrs, kind=None):
    body = struct.pack("=BBH", 19, 1, 0) + attrs
    return struct.pack("=IHHII", 16 + len(body), kind or family, 2, seq, 0) + body


def ack(seq, err=0):
    return struct.pack("=IHHII", 36, 2, 0, seq, 0) + struct.pack("=i", err) + bytes(16)


class FakeSock:
    """Hands back pre-built datagrams, one per recv(), like a netlink socket."""
    def __init__(self, datagrams):
        self.datagrams = list(datagrams)
        self.sent = []
    def send(self, data):
        self.sent.append(data)
    def recv(self, n):
        return self.datagrams.pop(0)


class Wifi(unittest.TestCase):
    def station(self, seq):
        N = h.Nl80211
        rate = h._nla(N.RATE_BITRATE32, struct.pack("=I", 1444)) + h._nla(N.RATE_BITRATE, struct.pack("=H", 1444))
        info = (h._nla(N.STA_INACTIVE, struct.pack("=I", 46)) + h._nla(N.STA_SIGNAL, struct.pack("=b", -44)) +
                h._nla(N.STA_TX_BITRATE | 0x8000, rate) + h._nla(N.STA_CONNECTED, struct.pack("=I", 14014)) +
                h._nla(N.STA_RX64, struct.pack("=Q", 77489453)) + h._nla(N.STA_TX64, struct.pack("=Q", 614167093)))
        attrs = (h._nla(N.ATTR_IFINDEX, struct.pack("=I", 3)) + h._nla(N.ATTR_MAC, bytes.fromhex("a63f1220fb85")) +
                 h._nla(N.ATTR_STA_INFO | 0x8000, info))
        return genl_msg(seq, 41, attrs)

    def test_station_dump_as_the_kernel_sends_it(self):
        nl = h.Nl80211.__new__(h.Nl80211)
        nl.family, nl.seq = 41, 6
        done = struct.pack("=IHHII", 20, 3, 2, 7, 0) + bytes(4)
        # a stale ACK from an earlier request arrives first: it must be ignored
        nl.sock = FakeSock([ack(5), self.station(7), done])
        st = nl.stations(3)
        self.assertEqual(st, {"a6:3f:12:20:fb:85": {"rx": 77489453, "tx": 614167093, "signal": -44,
                                                    "connected_for": 14014, "inactive_ms": 46,
                                                    "link_mbps": 144.4}})

    def test_disconnect_reports_kernel_errors(self):
        nl = h.Nl80211.__new__(h.Nl80211)
        nl.family, nl.seq = 41, 0
        nl.sock = FakeSock([ack(1, err=-1)])          # EPERM
        with self.assertRaises(h.HotspotError):
            nl.disconnect(3, "a6:3f:12:20:fb:85")
        self.assertIn(bytes.fromhex("a63f1220fb85"), nl.sock.sent[0])


class Hotspot(unittest.TestCase):
    MAC = "a6:3f:12:20:fb:85"

    def test_ruleset(self):
        devices = {self.MAC: dict(h.Hotspot._sanitize({}), ip="10.42.0.85", down=1_000_000, quota=500_000_000),
                   "8e:11:aa:00:12:34": dict(h.Hotspot._sanitize({}), ip="10.42.0.40", blocked=True)}
        text = h.nft_ruleset(["wlo1"], devices, {self.MAC: 1234}, {"10.42.0.85": 5678}, {self.MAC: 9999})
        self.assertTrue(text.startswith("table inet netwatch {}\ndelete table inet netwatch\n"))
        for line in ('a6:3f:12:20:fb:85 counter packets 0 bytes 1234', '10.42.0.85 counter packets 0 bytes 5678',
                     'quota q_a63f1220fb85 { over 500000000 bytes used 9999 bytes; }',
                     'oifname { "wlo1" } ip daddr 10.42.0.85 limit rate over 1000000 bytes/second drop',
                     'iifname { "wlo1" } ether saddr a6:3f:12:20:fb:85 quota name "q_a63f1220fb85" drop',
                     'elements = { 8e:11:aa:00:12:34 }', 'elements = { 10.42.0.40 }'):
            self.assertIn(line, text)
        # drops come before the counting, so a dropped packet is never counted as used
        self.assertLess(text.index("@blocked drop"), text.index("update @dev_up"))
        self.assertLess(text.index("limit rate"), text.index("update @dev_up"))

    def test_commands_are_validated(self):
        hs = h.Hotspot.__new__(h.Hotspot)
        hs.lock, hs.error = 3, None
        hs.state = {"devices": {}}
        hs.apply = lambda zero=(): None
        hs.save = lambda: None
        hs.ifaces, hs.nl = {}, None
        bad = [dict(cmd="set_device", mac="AA:BB:CC:DD:EE:FF", blocked=False),     # upper case
               dict(cmd="set_device", mac=self.MAC, blocked=False, down=True),      # bool is not a number
               dict(cmd="set_device", mac=self.MAC, blocked=False, up=1.5e6),       # float
               dict(cmd="set_device", mac=self.MAC, blocked=False, quota=10 ** 20),
               dict(cmd="set_device", mac=self.MAC, blocked=None),
               dict(cmd="reset_today", mac=self.MAC),                               # unknown device
               dict(cmd="rm", mac=self.MAC)]
        for msg in bad:
            self.assertFalse(hs.command(dict(msg, id=1))["ok"], msg)
        self.assertEqual(hs.state["devices"], {})
        ok = hs.command(dict(cmd="set_device", mac=self.MAC, blocked=True, down=2_000_000, up=None, quota=None,
                             name="  Ali\x1b[31m  ", id=9))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["id"], 9)
        d = hs.state["devices"][self.MAC]
        self.assertEqual((d["blocked"], d["down"], d["name"]), (True, 2_000_000, "Ali?[31m"))

    def test_a_failed_apply_leaves_the_state_alone(self):
        hs = h.Hotspot.__new__(h.Hotspot)
        hs.lock, hs.error, hs.ifaces, hs.nl = 3, None, {}, None
        hs.state = {"devices": {}}
        hs.save = lambda: None

        def boom(zero=()):
            raise h.HotspotError("nft: syntax error")
        hs.apply = boom
        ack_ = hs.command(dict(cmd="set_device", mac=self.MAC, blocked=True, id=2))
        self.assertFalse(ack_["ok"])
        self.assertEqual(hs.state["devices"], {})

    def test_a_tampered_state_file_is_sanitized(self):
        d = h.Hotspot._sanitize({"name": "x" * 500, "ip": "10.42.0.85; flush ruleset", "down": "1000000",
                                 "quota": -5, "blocked": "yes", "used_up": 2 ** 70})
        self.assertEqual((d["ip"], d["down"], d["quota"], d["blocked"], d["used_up"]), (None, None, None, False, 0))
        self.assertLessEqual(len(d["name"]), 64)

    def test_lease_and_arp_parsing(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dnsmasq-%s.leases")
            with open(path % "wlo1", "w") as f:
                f.write("1791300000 a6:3f:12:20:fb:85 10.42.0.85 Alis-iPhone 01:a6:3f\n"
                        "0 8e:11:aa:00:12:34 10.42.0.40 * *\n"
                        "garbage line\n"
                        "1 zz:zz:zz:zz:zz:zz 10.42.0.9 x *\n"
                        "1 3c:22:fb:51:ae:e4 999.1.1.1 bad-ip *\n")
            saved, h.NM_LEASES = h.NM_LEASES, path
            try:
                leases = h.read_leases("wlo1")
            finally:
                h.NM_LEASES = saved
        self.assertEqual(sorted(leases), ["8e:11:aa:00:12:34", "a6:3f:12:20:fb:85"])
        self.assertEqual(leases["a6:3f:12:20:fb:85"]["hostname"], "Alis-iPhone")
        self.assertIsNone(leases["8e:11:aa:00:12:34"]["hostname"])
        self.assertEqual(leases["8e:11:aa:00:12:34"]["expires"], 0)       # never expires

    def test_private_addresses_have_no_maker(self):
        self.assertEqual(h.Oui().vendor("a6:3f:12:20:fb:85"), (None, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
