from conftest import assert_valid, connect_daemon, connect_ui, fixture, read_until


def hello(devbox):
    return fixture("hello.json") | {"devbox": devbox}


def test_scan_one_devbox_round_trips_to_the_ui(client, add_token):
    token = add_token("dev-daniel")
    add_token("dev-acme")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        read_until(ui, "devbox_status")

        response = client.post("/api/scan", json={"devbox": "dev-daniel"})
        assert response.status_code == 202
        [sent] = response.json()["rpcs"]
        assert sent["devbox"] == "dev-daniel"

        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert rpc == {
            "type": "rpc",
            "id": sent["id"],
            "method": "rescan_boards",
            "params": {},
        }
        daemon.send_json(
            {
                "type": "rpc_result",
                "id": rpc["id"],
                "ok": True,
                "exit_code": 0,
                "output_tail": "rescanned 3 boards",
            }
        )
        result = ui.receive_json()

    assert_valid(result)
    assert result == {
        "type": "rpc_result",
        "id": rpc["id"],
        "ok": True,
        "exit_code": 0,
        "output_tail": "rescanned 3 boards",
        "devbox": "dev-daniel",
        "method": "rescan_boards",
    }


def test_scan_all_reaches_every_connected_daemon(client, add_token):
    daniel = add_token("dev-daniel")
    acme = add_token("dev-acme")
    with (
        connect_daemon(client, daniel, hello("dev-daniel")) as (d1, _),
        connect_daemon(client, acme, hello("dev-acme")) as (d2, _),
    ):
        response = client.post("/api/scan", json={})
        assert response.status_code == 202
        sent = {r["devbox"]: r["id"] for r in response.json()["rpcs"]}
        assert set(sent) == {"dev-daniel", "dev-acme"}
        assert d1.receive_json()["id"] == sent["dev-daniel"]
        assert d2.receive_json()["id"] == sent["dev-acme"]


def test_scan_of_an_offline_devbox_is_404(client, add_token):
    add_token("dev-daniel")
    assert client.post("/api/scan", json={"devbox": "dev-daniel"}).status_code == 404


def test_an_unanswered_rpc_times_out_to_the_ui(make_client, add_token):
    client = make_client(rpc_timeouts={"rescan_boards": 0.05})
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        read_until(ui, "devbox_status")
        [sent] = client.post("/api/scan", json={"devbox": "dev-daniel"}).json()["rpcs"]
        result = ui.receive_json()
        # A late answer for a finished rpc is dropped.
        daemon.send_json(fixture("rpc-result-ok.json") | {"id": sent["id"]})
    assert_valid(result)
    assert result["id"] == sent["id"]
    assert (result["ok"], result["exit_code"]) == (False, None)
    assert "timed out" in result["reason"]


def test_disconnect_fails_the_daemons_pending_rpcs(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        read_until(ui, "devbox_status")
        [sent] = client.post("/api/scan", json={}).json()["rpcs"]
        daemon.close()
        result = ui.receive_json()
    assert_valid(result)
    assert (result["id"], result["ok"]) == (sent["id"], False)
    assert "disconnected" in result["reason"]
