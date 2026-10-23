import logging

import pytest
from conftest import (
    board,
    connect_daemon,
    connect_ui,
    fixture,
    read_until,
    snapshot,
)
from starlette.websockets import WebSocketDisconnect

from hub_server.cli import main as cli

KEY = "dev-daniel/ticket-skill/tk-hub"


def test_a_daemon_that_never_says_hello_is_closed(make_client):
    client = make_client(hello_timeout=0.05)
    with client.websocket_connect("/ws/daemon") as daemon:
        refused = daemon.receive_json()
        assert refused["type"] == "refused"
        assert "hello" in refused["reason"]
        with pytest.raises(WebSocketDisconnect) as closed:
            daemon.receive_json()
    assert closed.value.code == 1008


def test_garbage_and_invalid_messages_are_logged_and_ignored(client, add_token, caplog):
    token = add_token("dev-daniel")
    broken = board(KEY)
    del broken["tickets"]
    with (
        caplog.at_level(logging.WARNING, logger="hub_server"),
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        read_until(ui, "devbox_status")
        daemon.send_text("{not json")
        daemon.send_text("[" * 100_000)  # RecursionError in json.loads
        daemon.send_json(snapshot(full=False, boards=[broken]))
        daemon.send_json({"type": "snapshot"})
        # Still connected: a valid snapshot after the garbage goes through.
        daemon.send_json(snapshot(full=False, boards=[board(KEY)]))
        events = read_until(ui, "devbox_status")
    assert [e["type"] for e in events] == ["board_synced", "devbox_status"]
    assert events[0]["board"]["key"] == KEY
    assert len([r for r in caplog.records if r.name == "hub_server"]) == 4


def test_revoking_the_token_ends_the_live_connection(client, db, add_token):
    token = add_token("dev-daniel")
    with connect_ui(client) as ui, connect_daemon(client, token) as (daemon, _):
        read_until(ui, "devbox_status")
        assert cli(["token", "revoke", "dev-daniel", "--db", str(db)]) == 0
        daemon.send_json(snapshot())
        refused = daemon.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            daemon.receive_json()
        status = read_until(ui, "devbox_status")[-1]
    assert (refused["type"], refused["reason"]) == ("refused", "token revoked")
    assert closed.value.code == 1008
    assert status["online"] is False


def test_a_revoked_token_is_noticed_without_any_message(make_client, db, add_token):
    client = make_client(token_check_interval=0.05)
    token = add_token("dev-daniel")
    with connect_ui(client) as ui, connect_daemon(client, token) as (daemon, _):
        read_until(ui, "devbox_status")
        assert cli(["token", "revoke", "dev-daniel", "--db", str(db)]) == 0
        refused = daemon.receive_json()
        daemon.close()  # the daemon answers the hub's close
        status = read_until(ui, "devbox_status")[-1]
    assert refused["reason"] == "token revoked"
    assert status["online"] is False


def test_a_newer_minor_daemon_with_extra_fields_is_understood(client, add_token):
    token = add_token("dev-daniel")
    hello = fixture("hello.json") | {"protocol_version": "1.1", "features": ["x"]}
    hello["health"]["gpu"] = "none"
    newer = snapshot(full=False, boards=[board(KEY)]) | {
        "protocol_version": "1.1",
        "sent_by": "tk-daemon 0.2",
    }
    newer["health"]["gpu"] = "none"
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token, hello) as (daemon, reply),
    ):
        assert reply["type"] == "welcome"
        read_until(ui, "devbox_status")
        daemon.send_json(newer)
        events = read_until(ui, "devbox_status")
    assert [e["type"] for e in events] == ["board_synced", "devbox_status"]
