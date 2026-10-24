#!/usr/bin/env python3
"""Fake ticket-skill merge-conflict.sh: `start|status|finish|abort <board> <NN>`.
Each call is appended to $FAKE_MC_LOG as {"argv", "cwd"}. A verb prints
`<board>/mc-<verb>-out` and exits with `<board>/mc-<verb>-exit` when they exist,
else the real script's happy path: start lists two conflicted files (exit 2),
status says nothing is unresolved, finish pushes, abort aborts (all exit 0).
`<board>/mc-start-slow` (seconds) makes start slow."""

import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if log := os.environ.get("FAKE_MC_LOG"):
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")

if len(args) != 3 or args[0] not in ("start", "status", "finish", "abort"):
    print("Usage: merge-conflict.sh start|status|finish|abort <board> <NN>")
    sys.exit(1)
verb, board = args[0], Path(args[1])
if verb == "start" and (board / "mc-start-slow").exists():
    time.sleep(float((board / "mc-start-slow").read_text()))
DEFAULTS = {
    "start": ("CONFLICTS in /wt:\n  src/a.py\n  src/b.py\n", 2),
    "status": ("UNMERGED none\nMARKERS none\nDIFF vs b before the merge:\n src/a.py | 2 +-\n", 0),
    "finish": ("PUSHED merge abc1234 to origin/b — CI re-runs\n", 0),
    "abort": ("ABORTED — /wt is back where it was\n", 0),
}  # fmt: skip
out, code = DEFAULTS[verb]
if (f := board / f"mc-{verb}-out").exists():
    out = f.read_text()
if (f := board / f"mc-{verb}-exit").exists():
    code = int(f.read_text())
print(out, end="")
sys.exit(code)
