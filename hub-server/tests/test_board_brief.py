from conftest import assert_valid, connect_daemon, snapshot

KEY = "dev-daniel/ticket-board/tk-hub"


def test_brief_refresh_sends_refresh_board_brief_to_the_boards_box(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        daemon.send_json(snapshot())
        client.get("/api/boards")
        response = client.post(f"/api/boards/{KEY}/brief")
        assert response.status_code == 202
        rpc = daemon.receive_json()

    assert_valid(rpc)
    assert (rpc["method"], rpc["params"]) == ("refresh_board_brief", {"board_key": KEY})
    assert response.json() == {"rpcs": [{"id": rpc["id"], "devbox": "dev-daniel"}]}


def test_brief_refresh_refuses_offline_box_and_unknown_board(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        daemon.send_json(snapshot())
        client.get("/api/boards")
    assert client.post(f"/api/boards/{KEY}/brief").status_code == 409
    assert client.post("/api/boards/dev-daniel/x/y/brief").status_code == 404
