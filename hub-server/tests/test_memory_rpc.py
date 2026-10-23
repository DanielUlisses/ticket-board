from conftest import assert_valid, connect_daemon, connect_ui, read_until, snapshot

KEY = "dev-daniel/ticket-board/tk-hub"


def test_memory_proposal_goes_to_the_boards_daemon_as_propose_memory(client, add_token):
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        response = client.post(
            f"/api/boards/{KEY}/memory", json={"text": "  Export prints JSON only.  "}
        )
        assert response.status_code == 202
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert response.json() == {"rpc": {"id": rpc["id"], "devbox": "dev-daniel"}}
        assert rpc["method"] == "propose_memory"
        assert rpc["params"] == {"board_key": KEY, "text": "Export prints JSON only."}


def test_memory_proposal_rejects_empty_text_and_unknown_boards(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        assert (
            client.post(f"/api/boards/{KEY}/memory", json={"text": " "}).status_code
            == 422
        )
        missing = client.post(
            "/api/boards/dev-daniel/nope/none/memory", json={"text": "x"}
        )
        assert missing.status_code == 404
