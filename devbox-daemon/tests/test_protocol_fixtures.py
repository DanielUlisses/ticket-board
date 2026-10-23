"""The protocol fixtures are the contract between daemon, hub and web: every one
must validate against protocol/schema.json, and every deliberately broken one
must be rejected at the point it was broken."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = json.loads((PROTOCOL / "schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

VALID = sorted((PROTOCOL / "fixtures").glob("*.json"))
INVALID = sorted((PROTOCOL / "fixtures-invalid").glob("*.json"))

# Where each broken fixture must fail: (JSON pointer of the instance, keyword).
# A fixture rejected for another reason is not testing what it claims to.
EXPECTED_ERROR = {
    "board-key-missing-devbox.json": ("/boards/1/key", "pattern"),
    "hello-bad-protocol-version.json": ("/protocol_version", "pattern"),
    "manual-task-bad-source.json": (
        "/boards/0/tickets/0/manual_tasks/0/source",
        "enum",
    ),
    "message-without-type.json": ("", "required"),
    "rpc-launch-missing-manual.json": ("/params", "required"),
    "rpc-resolve-nocode-empty-reason.json": ("/params/reason", "minLength"),
    "rpc-result-missing-output-tail.json": ("", "required"),
    "rpc-unknown-method.json": ("/method", "enum"),
    "synthesis-gemini-backend.json": ("/backend", "oneOf"),
    "ticket-missing-synthesis-status.json": ("/boards/0/tickets/0", "required"),
    "ticket-unknown-column.json": ("/boards/2/tickets/0/col", "enum"),
}


def test_schema_is_a_valid_json_schema():
    Draft202012Validator.check_schema(SCHEMA)


def test_there_are_fixtures_of_both_kinds():
    assert VALID and INVALID


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_fixture_validates(path):
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in VALIDATOR.iter_errors(json.loads(path.read_text()))
    ]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_broken_fixture_is_rejected_where_it_was_broken(path):
    assert path.name in EXPECTED_ERROR, f"add {path.name} to EXPECTED_ERROR"
    errors = {
        ("".join(f"/{p}" for p in e.absolute_path), e.validator)
        for e in VALIDATOR.iter_errors(json.loads(path.read_text()))
    }
    assert errors == {EXPECTED_ERROR[path.name]}


def test_every_message_type_and_rpc_method_has_a_fixture():
    types = set(SCHEMA["$defs"]["message_type"]["enum"])
    methods = set(SCHEMA["$defs"]["rpc_method"]["enum"])
    docs = [json.loads(p.read_text()) for p in VALID]
    assert types - {d["type"] for d in docs} == set()
    assert methods - {d["method"] for d in docs if d["type"] == "rpc"} == set()
