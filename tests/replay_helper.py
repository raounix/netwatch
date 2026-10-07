#!/usr/bin/env python3
"""Stands in for netwatch-helper: replays a recorded session (one JSON report
per line) and then, like the real helper, runs until its stdin is closed."""
import sys
import time

with open(sys.argv[1]) as f:
    for line in f:
        sys.stdout.write(line)
        sys.stdout.flush()
        time.sleep(0.05)
sys.stdin.read()
