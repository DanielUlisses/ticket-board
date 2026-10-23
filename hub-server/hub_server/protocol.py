"""protocol/schema.json at runtime: the hub validates what daemons send."""

import json
import os
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator

# A wheel ships the schema as hub_server/schema.json (pyproject force-include);
# a checkout (editable install, tests) reads it from <repo>/protocol.
REPO_SCHEMA = Path(__file__).resolve().parents[2] / "protocol" / "schema.json"


def find_schema() -> Path:
    """$TK_HUB_SCHEMA, else the schema shipped in the wheel, else the repo's."""
    if override := os.environ.get("TK_HUB_SCHEMA"):
        candidates = [Path(override)]
    else:
        candidates = [
            Path(str(resources.files("hub_server") / "schema.json")),
            REPO_SCHEMA,
        ]
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(
        "protocol schema not found (looked at "
        + ", ".join(map(str, candidates))
        + "); set TK_HUB_SCHEMA to the path of protocol/schema.json"
    )


def _open_objects(schema):
    """The schema without `additionalProperties: false`: a newer-minor daemon
    may add optional fields (MINOR is additive), which a strict check would
    refuse (hello) or drop (snapshot, rpc_result)."""
    if isinstance(schema, dict):
        return {
            key: _open_objects(value)
            for key, value in schema.items()
            if not (key == "additionalProperties" and value is False)
        }
    if isinstance(schema, list):
        return [_open_objects(item) for item in schema]
    return schema


SCHEMA = json.loads(find_schema().read_text())
VALIDATOR = Draft202012Validator(_open_objects(SCHEMA))


def problem(message) -> str | None:
    """The first way a daemon's `message` breaks the schema (unknown properties
    allowed), or None when it is valid."""
    error = next(iter(VALIDATOR.iter_errors(message)), None)
    if error is None:
        return None
    where = "".join(f"/{p}" for p in error.absolute_path) or "<root>"
    return f"{where}: {error.message}"[:300]  # messages can quote whole boards
