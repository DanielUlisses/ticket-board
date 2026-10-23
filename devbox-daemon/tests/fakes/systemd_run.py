#!/usr/bin/env python3
"""Fake `systemd-run --user …`: logs its argv to $FAKE_SYSTEMD_LOG, then runs the
command after the options inline (the real one detaches it). With
$FAKE_SYSTEMD_FAIL set it refuses: that text on stderr, exit 1."""

import json
import os
import subprocess
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_SYSTEMD_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
if fail := os.environ.get("FAKE_SYSTEMD_FAIL"):
    sys.exit(fail)
cmd = [a for a in argv if not a.startswith("--")]
sys.exit(subprocess.run(cmd, check=False).returncode)
