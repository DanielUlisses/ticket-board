"""Hub review actions (board.md, Human code review): set_review_comments,
approve_review and request_changes routed to the board's daemon; a request_changes
the agent got is a round in the ticket's activity log."""

from conftest import assert_valid, connect_daemon, connect_ui, read_until, snapshot

KEY = "dev-daniel/ticket-board/tk-hub"
URL = f"/api/boards/{KEY}/tickets/27/action"
COMMENT = {"file": "hub-web/src/Settings.tsx", "line": 12, "text": "Label this input."}


def test_review_actions_are_routed_with_the_ticket_added(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        for body in (
            {"method": "set_review_comments", "params": {"comments": [COMMENT]}},
            {"method": "approve_review", "params": {"diff_hash": "sha256:41aa07"}},
            {"method": "request_changes", "params": {}},
        ):
            assert client.post(URL, json=body).status_code == 202
        rpcs = [daemon.receive_json() for _ in range(3)]

    for rpc in rpcs:
        assert_valid(rpc)
    ticket = {"board_key": KEY, "nn": "27"}
    assert [(r["method"], r["params"]) for r in rpcs] == [
        ("set_review_comments", {**ticket, "comments": [COMMENT]}),
        ("approve_review", {**ticket, "diff_hash": "sha256:41aa07"}),
        ("request_changes", ticket),
    ]


def test_a_comment_the_schema_refuses_is_a_422(client, add_token):
    with connect_daemon(client, add_token("dev-daniel")) as (daemon, _):
        daemon.send_json(snapshot())
        bad = {**COMMENT, "line": 0}
        body = {"method": "set_review_comments", "params": {"comments": [bad]}}
        assert client.post(URL, json=body).status_code == 422


def request_changes(client, ui, daemon, ok):
    body = {"method": "request_changes", "params": {}}
    assert client.post(URL, json=body).status_code == 202
    rpc = daemon.receive_json()
    result = {"type": "rpc_result", "id": rpc["id"], "ok": ok,
              "exit_code": 0 if ok else 21, "output_tail": ""}  # fmt: skip
    if ok:
        result["data"] = {"comments": 2}
    daemon.send_json(result)
    read_until(ui, "rpc_result")


def test_a_delivered_request_changes_is_a_round_in_the_activity_log(client, add_token):
    with (
        connect_ui(client) as ui,
        connect_daemon(client, add_token("dev-daniel")) as (daemon, _),
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")
        request_changes(client, ui, daemon, ok=False)  # not sent: no round
        request_changes(client, ui, daemon, ok=True)

    body = client.get(f"/api/boards/{KEY}/tickets/27/activity").json()
    [round_] = body["events"]
    assert {k: v for k, v in round_.items() if k != "at"} == {
        "type": "changes_requested",
        "board_key": KEY,
        "nn": "27",
        "comments": 2,
    }
