from conftest import (
    assert_valid,
    board,
    connect_daemon,
    connect_ui,
    fixture,
    read_until,
    snapshot,
    ticket,
)

THREE_BOARDS = [
    "dev-daniel/devboxes/tk-hub",
    "dev-daniel/ticket-board/tk-hub",
    "dev-daniel/ticket-skill/tk-hub",
]


def test_full_snapshot_is_stored_and_broadcast(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        online = ui.receive_json()
        assert_valid(online)
        assert online["type"] == "devbox_status" and online["online"] is True

        daemon.send_json(fixture("snapshot-full-three-boards.json"))
        events = read_until(ui, "devbox_status")
        for event in events:
            assert_valid(event)
        synced = sorted(
            e["board"]["key"] for e in events if e["type"] == "board_synced"
        )
        assert synced == THREE_BOARDS

        body = client.get("/api/boards").json()
        assert sorted(b["key"] for b in body["boards"]) == THREE_BOARDS
        for b in body["boards"]:
            assert_valid({"type": "board_synced", "board": b})
            assert b["stale"] is False
        assert [d["devbox"] for d in body["devboxes"]] == ["dev-daniel"]
        for d in body["devboxes"]:
            assert_valid(d)
            assert d["online"] is True


def test_a_column_change_is_one_ticket_moved_and_one_activity_row(client, add_token):
    token = add_token("dev-daniel")
    key = "dev-daniel/ticket-skill/tk-hub"
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")  # online
        read_until(ui, "devbox_status")  # first snapshot

        moved = board(key)
        ticket(moved, "02")["col"] = "human"  # was review
        daemon.send_json(snapshot(full=False, boards=[moved]))
        events = read_until(ui, "devbox_status")

    for event in events:
        assert_valid(event)
    moves = [e for e in events if e["type"] == "ticket_moved"]
    assert [(m["board_key"], m["nn"], m["from"], m["to"]) for m in moves] == [
        (key, "02", "review", "human")
    ]

    activity = client.get(f"/api/boards/{key}/tickets/02/activity")
    assert activity.status_code == 200
    rows = activity.json()["events"]
    assert rows == moves
    assert client.get(f"/api/boards/{key}/tickets/01/activity").json()["events"] == []


def test_disconnect_marks_the_boxs_boards_stale(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")
        # Close explicitly: leaving the TestClient context cancels the
        # server task before its disconnect handling runs.
        daemon.close()
        events = read_until(ui, "devbox_status")

    for event in events:
        assert_valid(event)
    stale = sorted(e["board"]["key"] for e in events if e["type"] == "board_synced")
    assert stale == THREE_BOARDS
    assert all(e["board"]["stale"] for e in events if e["type"] == "board_synced")
    assert events[-1]["online"] is False

    body = client.get("/api/boards").json()
    assert [b["stale"] for b in body["boards"]] == [True, True, True]
    assert body["devboxes"][0]["online"] is False


def test_reconnecting_daemon_clears_stale(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")
        daemon.close()
        read_until(ui, "devbox_status")
        assert all(b["stale"] for b in client.get("/api/boards").json()["boards"])

        with connect_daemon(client, token) as (daemon, _):
            daemon.send_json(snapshot())
            read_until(ui, "devbox_status")
            read_until(ui, "devbox_status")
            boards = client.get("/api/boards").json()["boards"]
    assert [b["stale"] for b in boards] == [False, False, False]


def test_boards_missing_from_a_full_snapshot_are_removed(client, add_token):
    token = add_token("dev-daniel")
    kept = "dev-daniel/ticket-board/tk-hub"
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")
        daemon.send_json(snapshot(boards=[board(kept)]))
        events = read_until(ui, "devbox_status")

    removed = sorted(e["board_key"] for e in events if e["type"] == "board_removed")
    assert removed == [k for k in THREE_BOARDS if k != kept]
    for event in events:
        assert_valid(event)
    assert [b["key"] for b in client.get("/api/boards").json()["boards"]] == [kept]


def test_a_partial_snapshot_removes_only_what_it_lists(client, add_token):
    token = add_token("dev-daniel")
    gone = "dev-daniel/devboxes/tk-hub"
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")
        daemon.send_json(snapshot(full=False, boards=[], removed=[gone]))
        events = read_until(ui, "devbox_status")

    assert [e for e in events if e["type"] == "board_removed"] == [
        {"type": "board_removed", "board_key": gone}
    ]
    keys = [b["key"] for b in client.get("/api/boards").json()["boards"]]
    assert keys == [k for k in THREE_BOARDS if k != gone]


def test_a_daemon_cannot_write_another_boxs_boards(client, add_token):
    add_token("dev-daniel")
    token = add_token("dev-acme")
    hello = fixture("hello.json") | {"devbox": "dev-acme"}
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token, hello) as (daemon, _),
    ):
        daemon.send_json(snapshot() | {"devbox": "dev-acme"})
        read_until(ui, "devbox_status")
        events = read_until(ui, "devbox_status")
    assert [e["type"] for e in events] == ["devbox_status"]
    assert client.get("/api/boards").json()["boards"] == []
