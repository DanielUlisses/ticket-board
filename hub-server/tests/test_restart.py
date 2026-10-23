"""SQLite holds hub-only data (tokens, activity) across restarts; a restarted hub
has no daemons, so every box is offline and every board stale until it reports."""

from conftest import (
    assert_valid,
    board,
    connect_daemon,
    connect_ui,
    read_until,
    snapshot,
    ticket,
)

KEY = "dev-daniel/ticket-skill/tk-hub"


def test_a_restarted_hub_keeps_its_data_and_marks_everything_stale(
    make_client, add_token
):
    token = add_token("dev-daniel")
    first = make_client()
    with connect_ui(first) as ui, connect_daemon(first, token) as (daemon, _):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")
        moved = board(KEY)
        ticket(moved, "02")["col"] = "human"
        daemon.send_json(snapshot(full=False, boards=[moved]))
        read_until(ui, "devbox_status")
        activity = first.get(f"/api/boards/{KEY}/tickets/02/activity").json()

    second = make_client()
    body = second.get("/api/boards").json()
    assert len(body["boards"]) == 3
    assert all(b["stale"] for b in body["boards"])
    for b in body["boards"]:
        assert_valid({"type": "board_synced", "board": b})
    assert [(d["devbox"], d["online"]) for d in body["devboxes"]] == [
        ("dev-daniel", False)
    ]
    assert second.get(f"/api/boards/{KEY}/tickets/02/activity").json() == activity
    assert len(activity["events"]) == 1

    with connect_daemon(second, token) as (_, reply):
        assert reply["type"] == "welcome"
