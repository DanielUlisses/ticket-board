#!/usr/bin/env python3
"""Fake `devbox sync`: prints a line, exits $FAKE_DEVBOX_EXIT (0)."""

import os
import sys

if sys.argv[1:] != ["sync"]:
    sys.exit(f"fake devbox: unexpected {sys.argv[1:]}")
print("devbox sync: done")
sys.exit(int(os.environ.get("FAKE_DEVBOX_EXIT", "0")))
