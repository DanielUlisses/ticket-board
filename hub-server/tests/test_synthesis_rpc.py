"""Refine with a comment, checklist ticks and synthesis events: the comment and tick
endpoints send refine_with_comment / set_task_done to the board's daemon, and the
daemon's synthesis_* events reach /ws/ui."""

from conftest import (
    assert_valid,
    connect_daemon,
    connect_ui,
    fixture,
    read_until,
    snapshot,
)

KEY = "dev-daniel/ticket-skill/tk-hub"


def test_a_comment_goes_to_the_boards_daemon_as_refine_with_comment(client, add_token):
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        response = client.post(
            f"/api/boards/{KEY}/tickets/01/comment",
            json={"comment": "Mention the total cap."},
        )
        assert response.status_code == 202
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert response.json() == {"rpc": {"id": rpc["id"], "devbox": "dev-daniel"}}
        assert (rpc["method"], rpc["params"]) == (
            "refine_with_comment",
            {"board_key": KEY, "nn": "01", "comment": "Mention the total cap."},
        )

        daemon.send_json(fixture("rpc-result-ok.json") | {"id": rpc["id"]})
        result = read_until(ui, "rpc_result")[-1]
    assert (result["method"], result["board_key"], result["nn"]) == (
        "refine_with_comment",
        KEY,
        "01",
    )


def test_an_empty_comment_is_a_plain_refresh(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        client.get("/api/boards")
        url = f"/api/boards/{KEY}/tickets/01/comment"
        assert client.post(url, json={"comment": ""}).status_code == 202
        assert daemon.receive_json()["params"]["comment"] == ""


def test_comment_refuses_bad_bodies_unknown_tickets_and_an_offline_box(
    client, add_token
):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        client.get("/api/boards")
        url = f"/api/boards/{KEY}/tickets/01/comment"
        assert client.post(url, json={}).status_code == 422
        assert client.post(url, json={"comment": 7}).status_code == 422
        unknown = f"/api/boards/{KEY}/tickets/99/comment"
        assert client.post(unknown, json={"comment": ""}).status_code == 404
        nowhere = "/api/boards/dev-daniel/x/y/tickets/01/comment"
        assert client.post(nowhere, json={"comment": ""}).status_code == 404
    offline = client.post(f"/api/boards/{KEY}/tickets/01/comment", json={"comment": ""})
    assert offline.status_code == 409


def test_a_tick_goes_to_the_boards_daemon_as_set_task_done(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        client.get("/api/boards")
        response = client.post(
            f"/api/boards/{KEY}/tickets/01/tasks",
            json={"text": "Check an unknown board exits non-zero", "done": True},
        )
        assert response.status_code == 202
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert response.json() == {"rpc": {"id": rpc["id"], "devbox": "dev-daniel"}}
        assert (rpc["method"], rpc["params"]) == (
            "set_task_done",
            {
                "board_key": KEY,
                "nn": "01",
                "text": "Check an unknown board exits non-zero",
                "done": True,
            },
        )
        url = f"/api/boards/{KEY}/tickets/01/tasks"
        assert client.post(url, json={"text": "", "done": True}).status_code == 422
        assert client.post(url, json={"text": "x", "done": "yes"}).status_code == 422
        assert client.post(url, json={"text": "x"}).status_code == 422
    offline = client.post(
        f"/api/boards/{KEY}/tickets/01/tasks", json={"text": "x", "done": True}
    )
    assert offline.status_code == 409


def test_synthesis_events_from_a_daemon_reach_the_ui(client, add_token):
    started = fixture("synthesis-started.json")
    failed = fixture("synthesis-failed.json")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        daemon.send_json(started)
        daemon.send_json(failed)
        got = [read_until(ui, "synthesis_started")[-1], ui.receive_json()]

    for event in got:
        assert_valid(event)
    assert got == [started, failed]


def test_synthesis_events_for_another_box_or_malformed_are_dropped(client, add_token):
    started = fixture("synthesis-started.json")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        daemon.send_json(started | {"board_key": "dev-acme/ticket-skill/tk-hub"})
        daemon.send_json(started | {"backend": "gemini"})
        daemon.send_json(started | {"nn": "02"})
        got = read_until(ui, "synthesis_started")
    assert [m for m in got if m["type"].startswith("synthesis_")] == [
        started | {"nn": "02"}
    ]
