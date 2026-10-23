from conftest import (
    assert_valid,
    connect_daemon,
    connect_ui,
    fixture,
    read_until,
    snapshot,
)

KEY = "dev-daniel/ticket-board/tk-hub"


def test_policy_edit_goes_to_the_boards_daemon_as_set_policy(client, add_token):
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        response = client.post(
            f"/api/boards/{KEY}/policy",
            json={"values": {"autonomy": "assist", "auto_merge": True}},
        )
        assert response.status_code == 202
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert response.json() == {"rpc": {"id": rpc["id"], "devbox": "dev-daniel"}}
        assert rpc["method"] == "set_policy"
        assert rpc["params"] == {
            "board_key": KEY,
            "values": {"autonomy": "assist", "auto_merge": True},
        }

        daemon.send_json(fixture("rpc-result-ok.json") | {"id": rpc["id"]})
        result = read_until(ui, "rpc_result")[-1]
    assert (result["id"], result["board_key"], result["method"]) == (
        rpc["id"],
        KEY,
        "set_policy",
    )


def test_policy_edit_rejects_unknown_boards_box_keys_and_bad_values(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        url = f"/api/boards/{KEY}/policy"
        assert client.post(url, json={"values": {"autonomy": "x"}}).status_code == 422
        assert client.post(url, json={"values": {"max_parallel": 9}}).status_code == 422
        assert client.post(url, json={"values": {}}).status_code == 422
        missing = client.post(
            "/api/boards/dev-daniel/nope/none/policy",
            json={"values": {"autonomy": "auto"}},
        )
        assert missing.status_code == 404


def test_policy_edit_of_an_offline_devbox_is_404(client, add_token):
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        read_until(ui, "devbox_status")
    response = client.post(
        f"/api/boards/{KEY}/policy", json={"values": {"autonomy": "auto"}}
    )
    assert response.status_code == 404


def test_policy_edit_null_removes_the_board_override(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        response = client.post(
            f"/api/boards/{KEY}/policy",
            json={"values": {"autonomy": None, "night_shift": True}},
        )
        assert response.status_code == 202
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert rpc["params"]["values"] == {"autonomy": None, "night_shift": True}
