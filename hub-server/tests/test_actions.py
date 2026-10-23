"""POST /api/boards/{key}/tickets/{nn}/action and /api/boards/{key}/frontier:
ticket RPCs routed to the board's daemon (a fake one on /ws/daemon)."""

from conftest import (
    assert_valid,
    board,
    connect_daemon,
    connect_ui,
    read_until,
    snapshot,
    ticket,
)

from hub_server.hub import DevboxOffline

KEY = "dev-daniel/ticket-board/tk-hub"


def action_url(nn, key=KEY):
    return f"/api/boards/{key}/tickets/{nn}/action"


LAUNCH = {
    "method": "launch_ticket",
    "params": {"type": "fix", "model": "opus", "effort": "high", "manual": True},
}


def test_a_launch_goes_to_the_boards_daemon_and_its_result_to_the_ui(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        response = client.post(action_url("27"), json=LAUNCH)
        assert response.status_code == 202
        [sent] = response.json()["rpcs"]

        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert rpc == {
            "type": "rpc",
            "id": sent["id"],
            "method": "launch_ticket",
            "params": {"board_key": KEY, "nn": "27", **LAUNCH["params"]},
        }
        assert sent == {"id": rpc["id"], "devbox": "dev-daniel",
                        "method": "launch_ticket", "board_key": KEY, "nn": "27"}  # fmt: skip
        daemon.send_json({"type": "rpc_result", "id": rpc["id"], "ok": True,
                          "exit_code": 0, "output_tail": "RECORDED 27",
                          "data": {"over_cap": True, "running": 5, "max_parallel": 5}})  # fmt: skip
        result = read_until(ui, "rpc_result")[-1]

    assert_valid(result)
    assert result == {
        "type": "rpc_result",
        "id": rpc["id"],
        "ok": True,
        "exit_code": 0,
        "output_tail": "RECORDED 27",
        "data": {"over_cap": True, "running": 5, "max_parallel": 5},
        "devbox": "dev-daniel",
        "method": "launch_ticket",
        "board_key": KEY,
        "nn": "27",
    }


def test_say_and_pane_tail_are_routed_with_the_ticket_added(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        say = {"method": "say_agent", "params": {"message": "Use the tokens."}}
        tail = {"method": "pane_tail", "params": {"lines": 180}}
        assert client.post(action_url("06"), json=say).status_code == 202
        assert client.post(action_url("06"), json=tail).status_code == 202
        rpcs = [daemon.receive_json(), daemon.receive_json()]

    for rpc in rpcs:
        assert_valid(rpc)
    assert [(r["method"], r["params"]) for r in rpcs] == [
        ("say_agent", {"board_key": KEY, "nn": "06", "message": "Use the tokens."}),
        ("pane_tail", {"board_key": KEY, "nn": "06", "lines": 180}),
    ]


def test_ship_verbs_are_routed_and_resolve_needs_a_reason(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        for method in ("open_pr", "ssh_push", "merge_pr", "verify_landed"):
            r = client.post(action_url("27"), json={"method": method, "params": {}})
            assert r.status_code == 202
        resolve = {"method": "resolve_nocode", "params": {"reason": "notes only"}}
        assert client.post(action_url("27"), json=resolve).status_code == 202
        no_reason = {"method": "resolve_nocode", "params": {}}
        assert client.post(action_url("27"), json=no_reason).status_code == 422
        blank = {"method": "resolve_nocode", "params": {"reason": ""}}
        assert client.post(action_url("27"), json=blank).status_code == 422
        plain = {"method": "resolve_ticket", "params": {}}
        assert client.post(action_url("27"), json=plain).status_code == 202
        rpcs = [daemon.receive_json() for _ in range(6)]

    for rpc in rpcs:
        assert_valid(rpc)
    assert [r["method"] for r in rpcs] == [
        "open_pr", "ssh_push", "merge_pr", "verify_landed", "resolve_nocode",
        "resolve_ticket",
    ]  # fmt: skip
    assert rpcs[4]["params"]["reason"] == "notes only"


def test_an_action_the_schema_rejects_or_outside_the_list_is_422_and_sends_nothing(
    client, add_token
):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        bad = [
            {"method": "say_agent", "params": {"message": ""}},
            {"method": "launch_ticket", "params": {"type": "feat"}},
            {"method": "rescan_boards", "params": {}},
            {"method": "rm -rf", "params": {}},
            {"method": "pane_tail", "params": {"lines": 180, "board_key": "a/b/c"}},
        ]
        codes = [client.post(action_url("27"), json=b).status_code for b in bad]
        client.post("/api/scan", json={"devbox": "dev-daniel"})
        first = daemon.receive_json()

    assert codes == [422] * len(bad)
    assert first["method"] == "rescan_boards"  # nothing was sent before it


def test_an_unknown_ticket_is_404_and_an_offline_devbox_409(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        daemon.send_json(snapshot())
        assert client.post(action_url("99"), json=LAUNCH).status_code == 404
        assert client.post(action_url("01", "a/b/c"), json=LAUNCH).status_code == 404
    offline = client.post(action_url("27"), json=LAUNCH)
    assert offline.status_code == 409
    assert offline.json()["detail"] == "devbox dev-daniel is not connected"


def test_frontier_launches_each_backlog_ticket_with_its_suggestions_within_caps(
    client, add_token
):
    b = board(KEY)
    ticket(b, "08").update(col="backlog", title="Fix the hub read path")
    ticket(b, "09").update(col="backlog", smodel="")  # no suggestion: skipped
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot(boards=[b]))
        response = client.post(f"/api/boards/{KEY}/frontier")
        assert response.status_code == 202
        body = response.json()
        rpcs = [daemon.receive_json() for _ in body["rpcs"]]

    for rpc in rpcs:
        assert_valid(rpc)
    assert [r["params"] for r in rpcs] == [
        {"board_key": KEY, "nn": "08", "type": "fix", "model": "opus",
         "effort": "medium", "manual": False},
        {"board_key": KEY, "nn": "27", "type": "feat", "model": "sonnet",
         "effort": "low", "manual": False},
    ]  # fmt: skip
    assert [(r["id"], r["nn"]) for r in body["rpcs"]] == [
        (rpcs[0]["id"], "08"),
        (rpcs[1]["id"], "27"),
    ]
    assert body["skipped"] == [{"nn": "09", "reason": "no suggested model/effort"}]


def test_frontier_of_an_unknown_board_is_404(client):
    assert client.post("/api/boards/a/b/c/frontier").status_code == 404


def test_a_devbox_dropping_midway_through_a_frontier_keeps_the_rpcs_sent(
    client, add_token, monkeypatch
):
    b = board(KEY)
    ticket(b, "08").update(col="backlog")
    hub = client.app.state.hub
    send = hub.ticket_action
    sent = []

    async def drops_after_one(devbox, *args):
        if sent:
            raise DevboxOffline(devbox)
        sent.append(await send(devbox, *args))
        return sent[-1]

    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot(boards=[b]))
        monkeypatch.setattr(hub, "ticket_action", drops_after_one)
        response = client.post(f"/api/boards/{KEY}/frontier")
        nothing = client.post(f"/api/boards/{KEY}/frontier")

    assert response.status_code == 202
    body = response.json()
    assert [r["nn"] for r in body["rpcs"]] == ["08"]
    assert body["rpcs"][0]["id"] == sent[0]["id"]
    assert {"nn": "27", "reason": "devbox disconnected"} in body["skipped"]
    assert nothing.status_code == 409  # nothing sent at all
