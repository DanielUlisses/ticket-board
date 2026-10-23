#!/usr/bin/env python3
"""Fake `herdr status server`: the real key: value lines, or a refusal when
$FAKE_HERDR_DOWN is set; hangs for $FAKE_HERDR_SLOW seconds first when set."""

import os
import sys
import time

if sys.argv[1:] != ["status", "server"]:
    sys.exit(f"fake herdr: unexpected {sys.argv[1:]}")
if slow := os.environ.get("FAKE_HERDR_SLOW"):
    time.sleep(float(slow))
if os.environ.get("FAKE_HERDR_DOWN"):
    sys.exit("error: cannot connect to herdr server: connection refused")
print("status: running\nversion: 0.9.3\nendpoint_compatible: yes\nprivate_protocol: 22")
