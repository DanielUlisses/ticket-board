from conftest import assert_valid, connect_daemon, fixture

LAST_SYNC = {
    "started_at": "2026-10-23T14:00:00Z",
    "finished_at": "2026-10-23T14:01:00Z",
    "exit_code": 0,
    "log_path": "/home/d/.local/state/tk-daemon/sync-20261023T140000Z.log",
    "log_tail": "all good\n",
}


def test_sync_sends_sync_box_with_force_to_the_devbox(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        response = client.post("/api/devboxes/dev-daniel/sync", json={"force": True})
        assert response.status_code == 202
        rpc = daemon.receive_json()

    assert_valid(rpc)
    assert (rpc["method"], rpc["params"]) == ("sync_box", {"force": True})
    assert response.json() == {"rpcs": [{"id": rpc["id"], "devbox": "dev-daniel"}]}


def test_sync_refuses_while_the_box_is_offline(client):
    response = client.post("/api/devboxes/dev-daniel/sync", json={"force": False})

    assert response.status_code == 409


def test_the_last_sync_in_hello_is_shown_on_the_devbox(client, add_token):
    token = add_token("dev-daniel")
    hello = fixture("hello.json")
    hello["health"]["last_sync"] = LAST_SYNC
    with connect_daemon(client, token, hello):
        (box,) = client.get("/api/boards").json()["devboxes"]

    assert box["health"]["last_sync"] == LAST_SYNC


def test_an_interrupted_sync_in_hello_passes_the_schema_and_is_shown(client, add_token):
    token = add_token("dev-daniel")
    hello = fixture("hello-sync-interrupted.json")
    assert_valid(hello)
    with connect_daemon(client, token, hello):
        (box,) = client.get("/api/boards").json()["devboxes"]

    assert box["health"]["last_sync"] == hello["health"]["last_sync"]
    assert box["health"]["last_sync"]["interrupted"] is True
