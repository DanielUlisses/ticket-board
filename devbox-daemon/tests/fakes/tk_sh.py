#!/usr/bin/env python3
"""Fake ticket-skill tk.sh: `boards`, `export <path>`, `policy <path>`, `help`, with
the real verbs' output shape. Each call is appended to $FAKE_TK_LOG as
{"argv", "cwd", "pid"}. A board's rows come from <board>/rows.json; a board holding
`export-fails` fails its export, one holding `export-slow` (seconds) is slow to
export, one holding `export-stubborn` (seconds) is as slow and ignores SIGTERM.
`<repo>/.scratch/boards-out` replaces what `boards` prints. $FAKE_TK_HELP_FAILS
makes `help` fail; $FAKE_TK_HELP_PROSE adds prose naming policy files to it.
The `policy` verb exists (and `help` lists it) only when $FAKE_TK_POLICY is set:
then `resolved-policy.json` answers it and `policy-fails` makes it fail; `policy-set` then
writes `policy.json` (and the canned answer), `policy-set-fails` makes it fail. Without
it, `policy` fails like a tk.sh without that verb (usage on stdout, exit 1)."""

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

cwd = Path.cwd()
args = sys.argv[1:]
if log := os.environ.get("FAKE_TK_LOG"):
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "cwd": str(cwd), "pid": os.getpid()}) + "\n")


def rows(board: Path) -> list:
    f = board / "rows.json"
    return json.loads(f.read_text()) if f.exists() else []


verb = args[0] if args else ""
has_policy = bool(os.environ.get("FAKE_TK_POLICY"))
if verb == "help":
    if os.environ.get("FAKE_TK_HELP_FAILS"):
        sys.exit("ERROR: ticket-git-repo.sh not found")
    if os.environ.get("FAKE_TK_HELP_PROSE"):
        print("set the box policy in .scratch/policy.json; policy.json layers merge")
    print(
        "tk <verb> ...  (digest, view, export, boards"
        + (", policy" if has_policy else "")
        + ", help)"
    )
elif verb == "boards" and (cwd / ".scratch" / "boards-out").exists():
    print((cwd / ".scratch" / "boards-out").read_text())
elif verb == "boards":
    scratch = cwd / ".scratch"
    dirs = (
        sorted(d for d in scratch.iterdir() if d.is_dir()) if scratch.is_dir() else []
    )
    print(
        json.dumps(
            [{"slug": d.name, "path": str(d), "tickets": len(rows(d))} for d in dirs]
        )
    )
elif verb == "export":
    board = Path(args[1])
    if (board / "export-fails").exists():
        sys.exit("ERROR: board_rows failed")
    if (board / "export-slow").exists():
        time.sleep(float((board / "export-slow").read_text()))
    if (board / "export-stubborn").exists():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(float((board / "export-stubborn").read_text()))
    md = board / "board.md"
    print(
        json.dumps(
            {
                "board_id": board.name,
                "slug": re.sub(r"^[.-]*", "", board.name),  # as board-lib.sh makes it
                "repo": cwd.name,
                "root": str(cwd),
                "base": "main",
                "has_remote": not (board / "no-remote").exists(),
                "forge": "none",
                "board_path": str(board),
                "board_md": md.read_text() if md.exists() else None,
                "retro_results_md": None,
                "tickets": rows(board),
            }
        )
    )
elif verb == "policy" and has_policy:
    board = Path(args[1])
    if (board / "policy-fails").exists():
        sys.exit("ERROR: policy.json: parse error at line 3")
    print((board / "resolved-policy.json").read_text())
elif verb == "policy-set" and has_policy:
    # key=value... (value JSON when it parses) into <board>/policy.json; the
    # canned resolved-policy.json follows, standing in for the layer merge.
    board = Path(args[1])
    if (board / "policy-set-fails").exists():
        sys.exit("ERROR: not written - max_parallel is 0")
    for name in ("policy.json", "resolved-policy.json"):
        f = board / name
        if name == "resolved-policy.json" and not f.exists():
            continue
        obj = json.loads(f.read_text()) if f.exists() else {}
        for arg in args[2:]:
            key, _, val = arg.partition("=")
            try:
                obj[key] = json.loads(val)
            except ValueError:
                obj[key] = val
            if val == "":  # `key=` removes the key, as in the real tk.sh
                obj.pop(key, None)
        f.write_text(json.dumps(obj))
    print(f"WROTE {board / 'policy.json'}")
else:
    print("Usage: tk.sh ...")
    sys.exit(1)
