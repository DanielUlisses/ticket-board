#!/usr/bin/env python3
"""Fake `systemctl --user is-active <unit>`: `active` (exit 0) when the unit is in
the comma-separated $FAKE_SYSTEMCTL_ACTIVE, else `inactive` (exit 3)."""

import os
import sys

if sys.argv[1:3] != ["--user", "is-active"] or len(sys.argv) != 4:
    sys.exit(f"fake systemctl: unexpected {sys.argv[1:]}")
if sys.argv[3] in os.environ.get("FAKE_SYSTEMCTL_ACTIVE", "").split(","):
    print("active")
    sys.exit(0)
print("inactive")
sys.exit(3)
