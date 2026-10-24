import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from hub_server.app import create_app
from hub_server.cli import main as cli

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = json.loads((PROTOCOL / "schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
REGISTRY = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))

# REST route -> the schema $def its response body must match.
REST_DEFS = [
    ("GET", re.compile(r"^/api/boards$"), "rest_boards"),
    ("GET", re.compile(r"^/api/boards/.+/tickets/[^/]+/report$"), "rest_report"),
    ("GET", re.compile(r"^/api/boards/.+/tickets/[^/]+/activity$"), "rest_activity"),
    ("GET", re.compile(r"^/api/boards/.+/tickets/[^/]+/diff$"), "rest_diff"),
    ("POST", re.compile(r"^/api/scan$"), "rest_scan"),
    ("POST", re.compile(r"^/api/boards/.+/tickets/[^/]+/action$"), "rest_action"),
    ("POST", re.compile(r"^/api/boards/.+/frontier$"), "rest_action"),
    ("POST", re.compile(r"^/api/boards/.+/archive$"), "rest_archive"),
    ("POST", re.compile(r"^/api/devboxes/[^/]+/sync$"), "rest_sync_box"),
    ("POST", re.compile(r"^/api/boards/.+/brief$"), "rest_board_brief"),
    ("POST", re.compile(r"^/api/boards/.+/policy$"), "rest_set_policy"),
    ("POST", re.compile(r"^/api/boards/.+/tickets/[^/]+/comment$"), "rest_refine"),
    ("POST", re.compile(r"^/api/boards/.+/tickets/[^/]+/tasks$"), "rest_set_task_done"),
    ("POST", re.compile(r"^/api/boards/.+/memory$"), "rest_propose_memory"),
    ("POST", re.compile(r"^/api/boards/.+/retro(/apply)?$"), "rest_board_rpc"),
    ("POST", re.compile(r"^/api/boards/.+/sweep$"), "rest_board_rpc"),
    ("GET", re.compile(r"^/api/settings$"), "rest_settings"),
    ("POST", re.compile(r"^/api/settings$"), "rest_settings"),
    ("GET", re.compile(r"^/api/scheduler$"), "rest_scheduler"),
]


def assert_valid_def(instance, name):
    validator = Draft202012Validator(
        {"$ref": f"{SCHEMA['$id']}#/$defs/{name}"}, registry=REGISTRY
    )
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(instance)
    ]
    assert not errors, f"{name}: " + "\n".join(errors)


def check_rest_response(response):
    """httpx response hook: every /api response must match its REST $def, every
    error response (any path) rest_error."""
    request = response.request
    if response.status_code >= 400:
        if response.headers.get("content-type", "").startswith("application/json"):
            response.read()
            assert_valid_def(response.json(), "rest_error")
        return
    if not request.url.path.startswith("/api/"):
        return
    response.read()
    for method, path, name in REST_DEFS:
        if request.method == method and path.match(request.url.path):
            assert_valid_def(response.json(), name)
            return
    raise AssertionError(f"no REST $def for {request.method} {request.url.path}")


def fixture(name):
    return copy.deepcopy(json.loads((PROTOCOL / "fixtures" / name).read_text()))


def assert_valid(message):
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in VALIDATOR.iter_errors(message)
    ]
    assert not errors, "\n".join(errors)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "hub.db"


@pytest.fixture
def add_token(db, capsys):
    def add(devbox):
        assert cli(["token", "add", devbox, "--db", str(db)]) == 0
        return capsys.readouterr().out.strip()

    return add


@pytest.fixture
def make_client(db):
    clients = []

    def make(**kwargs):
        # The scheduler would launch the fixtures' frontier under every test;
        # the tests that want it ask for it.
        kwargs.setdefault("schedule", False)
        client = TestClient(create_app(db_path=db, **kwargs))
        client.event_hooks = {"request": [], "response": [check_rest_response]}
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


# TestClient's own origin; /ws/ui refuses any other.
ORIGIN = "http://testserver"


def connect_ui(client, headers=None):
    return client.websocket_connect(
        "/ws/ui", headers={"Origin": ORIGIN} | (headers or {})
    )


@contextmanager
def connect_daemon(client, token, hello=None):
    """Open /ws/daemon and send hello; yields (socket, the hub's first reply)."""
    with client.websocket_connect(
        "/ws/daemon", headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        ws.send_json(hello or fixture("hello.json"))
        yield ws, ws.receive_json()


def read_until(ws, message_type):
    """Messages from `ws` up to and including the first of `message_type`."""
    messages = []
    while not messages or messages[-1]["type"] != message_type:
        messages.append(ws.receive_json())
    return messages


def snapshot(full=True, boards=None, removed=()):
    """A snapshot from dev-daniel; by default the full three-board fixture."""
    message = fixture("snapshot-full-three-boards.json")
    message.update(full=full, removed=list(removed))
    if boards is not None:
        message["boards"] = list(boards)
    return message


def board(key):
    """One board of the three-board fixture, by key."""
    boards = fixture("snapshot-full-three-boards.json")["boards"]
    return next(b for b in boards if b["key"] == key)


def ticket(board, nn):
    return next(t for t in board["tickets"] if t["nn"] == nn)
