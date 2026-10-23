"""tk-hub server: receives board snapshots from devbox daemons, stores them and
broadcasts them to the web UI."""

import re

# The protocol major.minor this hub speaks; must equal protocol/schema.json's
# `x-protocol-version` (tests/test_protocol_version.py checks it).
PROTOCOL_VERSION = "1.0"

# A devbox, repo or board name: one path segment (schema.json $defs/name).
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
