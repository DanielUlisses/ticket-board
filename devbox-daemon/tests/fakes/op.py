#!/usr/bin/env python3
"""Fake 1Password `op read <ref>`: prints $FAKE_OP_TOKEN, logs the ref to $FAKE_OP_LOG."""

import os
import sys

if sys.argv[1:2] != ["read"] or len(sys.argv) != 3:
    sys.exit(f"fake op: unexpected {sys.argv[1:]}")
if log := os.environ.get("FAKE_OP_LOG"):
    with open(log, "a") as f:
        f.write(sys.argv[2] + "\n")
print(os.environ["FAKE_OP_TOKEN"])
