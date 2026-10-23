from conftest import assert_valid, board, connect_daemon, snapshot

KEY = "dev-daniel/ticket-board/tk-hub"


def seed(daemon):
    daemon.send_json(snapshot())


def test_archive_sends_set_archived_to_the_boards_box(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        seed(daemon)
        client.get("/api/boards")  # let the snapshot land before asking
        response = client.post(f"/api/boards/{KEY}/archive", json={"archived": True})
        assert response.status_code == 202
        rpc = daemon.receive_json()

    assert_valid(rpc)
    assert rpc["method"] == "set_archived"
    assert rpc["params"] == {"board_key": KEY, "archived": True}
    assert response.json() == {"rpcs": [{"id": rpc["id"], "devbox": "dev-daniel"}]}


def test_archive_refuses_while_the_box_is_offline(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        seed(daemon)
        client.get("/api/boards")
    response = client.post(f"/api/boards/{KEY}/archive", json={"archived": True})

    assert response.status_code == 409


def test_archive_of_an_unknown_board_is_404(client):
    response = client.post(
        "/api/boards/dev-daniel/x/y/archive", json={"archived": True}
    )

    assert response.status_code == 404


def test_an_archived_board_that_vanishes_from_its_box_is_removed(client, add_token):
    token = add_token("dev-daniel")
    archived = board(KEY)
    archived["hub_meta"] = {**archived.get("hub_meta", {}), "archived": True}
    with connect_daemon(client, token) as (daemon, _):
        daemon.send_json(snapshot(boards=[archived]))
        client.get("/api/boards")
        [kept] = client.get("/api/boards").json()["boards"]
        assert kept["hub_meta"]["archived"] is True
        daemon.send_json(snapshot(boards=[], removed=[]))
        client.get("/api/boards")
        boards = client.get("/api/boards").json()["boards"]

    assert boards == []
