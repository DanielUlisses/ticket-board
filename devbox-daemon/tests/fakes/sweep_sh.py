#!/usr/bin/env python3
"""Fake sweep-tickets sweep.sh. Each call is appended to $FAKE_SWEEP_LOG as
{"argv", "cwd"}. `list` prints $FAKE_SWEEP_LIST (the file's text, TSV rows).
`remove --<kind> <value>` exits with $FAKE_SWEEP_EXITS[<value>] (a JSON object),
else 0."""

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if log := os.environ.get("FAKE_SWEEP_LOG"):
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
if args[:1] == ["list"]:
    print(Path(os.environ["FAKE_SWEEP_LIST"]).read_text(), end="")
elif args[:1] == ["remove"]:
    code = json.loads(os.environ.get("FAKE_SWEEP_EXITS", "{}")).get(args[2], 0)
    print(f"REMOVED {args[1]} {args[2]}" if code == 0 else f"SKIPPED {args[2]}")
    sys.exit(code)
else:
    sys.exit(1)
