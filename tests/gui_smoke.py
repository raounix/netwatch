#!/usr/bin/env python3
"""Drives the Netwatch window without a screen and checks what it shows.

Runs the real window on a private GTK Broadway display, then:
  · demo data — both groupings, both unit systems, sorting, the idle filter,
    the details dialog, pause;
  · fake helpers — the password dialog dismissed (pkexec exit 126), a helper
    that reports an error, a helper from another protocol version;
  · a REAL helper session recorded by tests/e2e_netns.py, replayed through the
    same pipe the window reads — and stopped the way the window stops it.
"""
import argparse
import importlib.machinery
import os
import pathlib
import shlex
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
DISPLAY = ":19"
broadway = subprocess.Popen(["gtk4-broadwayd", DISPLAY, "--port", "8119", "--address", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.8)
os.environ.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=DISPLAY)

nw = importlib.machinery.SourceFileLoader("netwatch", str(HERE.parent / "netwatch")).load_module()
from gi.repository import GLib  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def rows(win):
    out, w = [], win.list.get_first_child()
    while w is not None:
        if isinstance(w, nw.AppRow):
            out.append(w)
        w = w.get_next_sibling()
    return out


def page(win):
    return win.stack.get_visible_child_name()


def walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from walk(child)
        child = child.get_next_sibling()


def find(widget, cls, title=None):
    for w in walk(widget):
        if isinstance(w, cls) and (title is None or getattr(w, "get_title", lambda: None)() == title):
            return w
    return None


def device_rows(win):
    out, w = {}, win.dev_list.get_first_child()
    while w is not None:
        if isinstance(w, nw.DeviceRow):
            out[w.mac] = w
        w = w.get_next_sibling()
    return out


def fake(code):
    return "%s -c %s" % (shlex.quote(sys.executable), shlex.quote(code))


REPLAY = HERE / "recorded-helper.jsonl"
STEPS = []                     # (seconds to wait first, function)


def step(delay):
    def deco(fn):
        STEPS.append((delay, fn))
        return fn
    return deco


# ── demo data ───────────────────────────────────────────────────────────────
@step(3.5)
def demo_running(win):
    print("demo data")
    check(page(win) == "main", "dashboard is shown")
    r = rows(win)
    check(len(r) >= 6, "%d application rows" % len(r))
    chrome = [x for x in r if x.entry.key == "app:google-chrome"]
    check(bool(chrome) and chrome[0].name.get_text() == "Google Chrome",
          "desktop name resolved: %r" % (chrome[0].name.get_text() if chrome else None))
    check(bool(chrome) and "pid 4021" in chrome[0].sub.get_text(), "single-process app shows its pid")
    for app_id, label, proxy in (("jetbrains-webstorm", "jetbrains-webstorm", False), (None, "throne", True),
                                 (None, "mentor", False), (None, "sing-box", True)):
        e = nw.Entry("test:" + label, "app" if app_id else "process", label, app_id)
        win._identify(e)
        says = "Proxy" in win.subtitle(e)
        check(says == proxy, "%s %s called a proxy client" % (e.display, "is" if proxy else "is not"))
    npm = [x for x in r if x.entry.key == "proc:1000:npm"]
    check(bool(npm) and "started from WebStorm" in npm[0].sub.get_text(), "npm shows where it was started from")
    check(win.down.rate.get_text().endswith("B/s"), "download card shows a rate: " + win.down.rate.get_text())
    win.activate_action("win.group", GLib.Variant("s", "process"))


@step(1.5)
def process_view(win):
    r = rows(win)
    pid_rows = [x for x in r if x.entry.kind == "pid"]
    check(len(pid_rows) >= 6 and all(x.sub.get_text().startswith("pid ") for x in pid_rows),
          "process view: %d rows, each with its pid" % len(pid_rows))
    check(not any(x.entry.kind == "app" for x in r), "process view has no application rows left")
    win.activate_action("win.units", GLib.Variant("s", "bits"))


@step(1.5)
def bits(win):
    cells = [x.cells[0].get_text() for x in rows(win) if x.cells[0].get_text() != "—"]
    check(bool(cells) and all("bit/s" in c for c in cells), "bits: %s" % cells[:3])
    check("bit/s" in win.down.rate.get_text(), "card in bits: " + win.down.rate.get_text())
    win.activate_action("win.units", GLib.Variant("s", "bytes"))
    win.activate_action("win.group", GLib.Variant("s", "app"))
    win.set_sort("name")


@step(1.5)
def sorted_by_name(win):
    names = [x.entry.display.lower() for x in rows(win)]
    check(names == sorted(names), "sorted by name: %s…" % names[:4])
    win.set_sort("name")                         # second click: back to activity
    check(win.sort_key == "activity", "clicking the sorted column again restores activity order")
    win.activate_action("win.show-idle", None)   # toggle off


@step(1.5)
def idle_filter(win):
    old = win.apps["svc:systemd-resolved"]
    old.last_active -= 1000                      # pretend it went quiet long ago
    win.list.invalidate_filter()
    r = {x.entry.key: x for x in rows(win)}
    check(not r["svc:systemd-resolved"].get_child_visible(), "an idle app is hidden when idle apps are off")
    check(r["app:google-chrome"].get_child_visible(), "an active app stays visible")
    win.activate_action("win.show-idle", None)
    win.list.invalidate_filter()
    check(r["svc:systemd-resolved"].get_child_visible(), "turning idle apps back on shows it again")
    old.last_active += 1000
    chrome = win.apps["app:google-chrome"]
    win.show_details(chrome)
    dialog = win.get_visible_dialog()
    check(dialog is not None and dialog.get_title() == "Google Chrome", "details dialog opens")
    if dialog:
        dialog.close()
    win.show_details(win.apps["other:unattributed"])
    dialog = win.get_visible_dialog()
    check(dialog is not None, "details dialog for the unattributed bucket opens")
    if dialog:
        dialog.close()
    win.activate_action("win.pause", None)
    win._frozen = win.down.rate.get_text(), [x.cells[2].get_text() for x in rows(win)]


@step(2.5)
def paused(win):
    check((win.down.rate.get_text(), [x.cells[2].get_text() for x in rows(win)]) == win._frozen,
          "pause freezes the view")
    check("paused" in win.title.get_subtitle(), "title says paused")
    win.activate_action("win.pause", None)
    win.views.set_visible_child_name("hotspot")


@step(1.5)
def hotspot_tab(win):
    print("hotspot tab (demo devices)")
    from gi.repository import Adw, Gtk
    rows = device_rows(win)
    check(len(rows) == 4 and win.hs_stack.get_visible_child_name() == "on", "4 devices listed (%d)" % len(rows))
    check(win.hs_page.get_badge_number() == 3, "the tab's badge counts the 3 connected (%d)" % win.hs_page.get_badge_number())
    check(win.hs_name.get_text() == "raounix2" and "2.4 GHz" in win.hs_info.get_text(),
          "network card: %r / %r" % (win.hs_name.get_text(), win.hs_info.get_text()))
    badges = lambda r: [b.get_text() for b in walk(r.badges) if isinstance(b, Gtk.Label)]
    guest = rows["8e:11:aa:00:12:34"]
    check(guest.name.get_text() == "Guest" and "No internet" in badges(guest) and "not connected" in guest.sub.get_text(),
          "the blocked guest: %r %s %r" % (guest.name.get_text(), badges(guest), guest.sub.get_text()))
    galaxy = rows["dc:a6:32:e4:51:8d"]
    check(any(b.startswith("↓ 1.0 MB/s") for b in badges(galaxy)) and "private address" in galaxy.sub.get_text(),
          "the limited phone: %s" % badges(galaxy))
    reza = rows["3c:22:fb:51:ae:e4"]
    check(any("Daily 500" in b for b in badges(reza)) and reza.quota.get_visible() and "Intel" in reza.sub.get_text(),
          "the laptop with a daily limit: %s, bar %.2f" % (badges(reza), reza.quota.get_value()))
    check(reza.quota.has_css_class("nw-warn") and not reza.quota.has_css_class("nw-full"),
          "an 80%+ used limit shows as a warning, not as 'healthy' green")
    check(device_rows(win) and list(device_rows(win))[-1] == "8e:11:aa:00:12:34",
          "the disconnected device sorts last")

    rows["a6:3f:12:20:fb:85"].activate()          # what a click does: the row's activate → row-activated
    dialog = win.get_visible_dialog()
    check(dialog is not None and dialog.get_title() == "Alis-iPhone", "clicking a device opens its dialog")
    groups = [g.get_title() for g in walk(dialog) if isinstance(g, Adw.PreferencesGroup)]
    check(groups[:3] == ["Access", "Today", "Device"], "the controls come first: %s" % groups[:3])
    save = find(dialog, Gtk.Button)
    save = next(w for w in walk(dialog) if isinstance(w, Gtk.Button) and w.get_label() == "Save")
    check(not save.get_sensitive(), "Save is off until something changes")
    find(dialog, Adw.EntryRow, "Your name for it").set_text("Ali's iPhone")
    find(dialog, Adw.SpinRow, "Download speed limit").set_value(1.0)
    find(dialog, Adw.SpinRow, "Daily data limit").set_value(800)
    find(dialog, Adw.SwitchRow, "Internet access").set_active(True)
    check(save.get_sensitive(), "Save turns on after a change")
    save.emit("clicked")
    win._dialog = dialog


@step(1.5)
def device_saved(win):
    check(win.get_visible_dialog() is None, "the dialog closes once the helper confirms")
    ali = device_rows(win).get("a6:3f:12:20:fb:85")
    r = ali.d["rules"] if ali else {}
    check(ali is not None and ali.name.get_text() == "Ali's iPhone" and r.get("down") == 1_000_000
          and r.get("quota") == 800_000_000 and r.get("up") is None and r.get("blocked") is False,
          "the device now reports the new name and limits: %s %s" % (ali and ali.name.get_text(), r))
    # a helper that cannot change rules (no nftables, or another window owns them)
    report = dict(win.hotspot, controls=False, error="nftables is not installed (sudo apt install nftables)")
    win.on_hotspot(report)
    check(win.hs_notice.get_visible() and "nftables" in win.hs_notice.get_text(), "unavailable controls are explained")
    win.show_device("a6:3f:12:20:fb:85")
    from gi.repository import Adw
    dialog = win.get_visible_dialog()
    access = find(dialog, Adw.SwitchRow, "Internet access")
    check(access is not None and not access.get_parent().get_sensitive() or not access.is_sensitive(),
          "…and the dialog's controls are disabled")
    dialog.close()
    off = dict(win.hotspot, active=False, devices=[])
    win.on_hotspot(off)
    check(win.hs_stack.get_visible_child_name() == "off", "no hotspot, no devices: the 'Hotspot is off' page")
    win.views.set_visible_child_name("apps")
    win.stop()
    win._stopped_at = win.total_rx


@step(2.0)
def demo_stopped(win):
    check(win.total_rx == win._stopped_at, "no reports arrive after stop()")
    win.options.demo = False
    win.options.helper_cmd = fake("raise SystemExit(126)")
    win.start()


# ── fake helpers ────────────────────────────────────────────────────────────
@step(1.5)
def dismissed(win):
    print("helper failures")
    check(page(win) == "start", "password dialog dismissed (exit 126) → back to the start page")
    win.options.helper_cmd = fake(
        "import json,sys; print(json.dumps({'type':'error','message':'cannot capture on any interface (eth9: No such device)'}),"
        " flush=True); sys.stderr.write('netwatch-helper: boom\\n'); raise SystemExit(1)")
    win.start()


@step(1.5)
def reported_error(win):
    check(page(win) == "error", "helper error → error page")
    check("No such device" in win.error_details.get_text(),
          "the helper's own message is shown: %r" % win.error_details.get_text())
    win.options.helper_cmd = fake(
        "import json,sys; print(json.dumps({'type':'hello','protocol':99}), flush=True); sys.stdin.read()")
    win.start()


@step(1.5)
def mismatch(win):
    check(page(win) == "error" and win.error_page.get_title() == "Version mismatch",
          "protocol mismatch is caught: %r" % win.error_page.get_title())
    check(win.client is None, "the mismatched helper was stopped")
    win.options.helper_cmd = "%s %s %s" % (shlex.quote(sys.executable), shlex.quote(str(HERE / "replay_helper.py")),
                                         shlex.quote(str(REPLAY)))
    win.apps.clear()
    win.pids.clear()
    win.list.remove_all()
    win.start()


# ── a real recorded session ─────────────────────────────────────────────────
@step(3.0)
def replay(win):
    print("recorded real helper session")
    check(page(win) == "main", "dashboard shown for the recorded session")
    procs = [x for x in rows(win) if x.entry.kind != "other"]
    total = sum(x.entry.rx_total + x.entry.tx_total for x in procs)
    check(total > 8_000_000, "the recorded traffic arrived: %s" % nw.fmt_bytes(total))
    check("veth0" in win.caption.get_text(), "the page names the interface: " + win.caption.get_text())
    win._proc = win.client.proc
    win.stop()


@step(2.0)
def replay_stopped(win):
    proc = win._proc
    check(proc.get_if_exited() and proc.get_exit_status() == 0,
          "closing the helper's stdin makes it exit (status %s)" % (proc.get_exit_status() if proc.get_if_exited() else "still running"))
    check(page(win) == "main", "a deliberate stop does not flash an error or start page")
    win.get_application().quit()


def main():
    opts = argparse.Namespace(demo=True, helper_cmd=None)
    app = nw.App(opts)

    def run_steps():
        win = app.window
        t = 0
        for delay, fn in STEPS:
            t += delay
            GLib.timeout_add(int(t * 1000), lambda fn=fn: (fn(win), False)[1])
        return False

    app.connect("activate", lambda *_: GLib.idle_add(run_steps))
    GLib.timeout_add_seconds(60, lambda: (failures.append("timed out"), app.quit()))
    try:
        app.run([sys.argv[0]])
    finally:
        broadway.terminate()
    print("\nPASS" if not failures else "\nFAIL — %d check(s)" % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
