"""GET /api/boards/{key}/tickets/{nn}/diff: get_diff on the board's daemon (a
fake one on /ws/daemon), its result returned to the caller."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import assert_valid, connect_daemon, connect_ui, read_until, snapshot

KEY = "dev-daniel/ticket-board/tk-hub"
DATA = {
    "base": "abc123",
    "diff_hash": "sha256:" + "f" * 64,
    "files": [
        {"path": "a.py", "status": "M", "added": 2, "removed": 1, "binary": False}
    ],
}


def diff_url(nn, key=KEY):
    return f"/api/boards/{key}/tickets/{nn}/diff"


def test_the_file_list_comes_back_to_the_caller_not_the_ui(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
        ThreadPoolExecutor(1) as pool,
    ):
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        pending = pool.submit(client.get, diff_url("27"))
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert rpc["method"] == "get_diff"
        assert rpc["params"] == {"board_key": KEY, "nn": "27"}
        daemon.send_json({"type": "rpc_result", "id": rpc["id"], "ok": True,
                          "exit_code": 0, "output_tail": "", "data": DATA})  # fmt: skip
        response = pending.result(10)

    assert response.status_code == 200
    assert response.json() == {"board_key": KEY, "nn": "27", **DATA}


def test_file_goes_to_the_daemon_and_a_refusal_is_a_502(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_daemon(client, token) as (daemon, _),
        ThreadPoolExecutor(1) as pool,
    ):
        daemon.send_json(snapshot())

        pending = pool.submit(client.get, diff_url("27"), params={"file": "a b.py"})
        rpc = daemon.receive_json()
        assert rpc["params"] == {"board_key": KEY, "nn": "27", "file": "a b.py"}
        daemon.send_json({"type": "rpc_result", "id": rpc["id"], "ok": False,
                          "exit_code": None, "output_tail": "",
                          "reason": "git diff failed (exit 128): boom"})  # fmt: skip
        response = pending.result(10)

    assert response.status_code == 502
    assert "git diff failed" in response.text


def test_unknown_ticket_is_404_and_an_offline_devbox_409(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        daemon.send_json(snapshot())
        assert client.get(diff_url("99")).status_code == 404
        assert client.get(diff_url("27", key="nope/x/y")).status_code == 404
    assert client.get(diff_url("27")).status_code == 409


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("a.py is not in ticket 27's diff", 404),
        ("a.py is ambiguous in ticket 27's diff: 2 files share that shown name", 409),
        ("a.py is gone from the worktree", 404),
        ("no ticket 27 on board alpha/b", 404),
        ("devbox disconnected", 409),
        ("ticket 27 has no worktree to diff", 409),
        ("ticket 27 has no base to diff against", 409),
        ("git diff failed (exit 128): boom", 502),
    ],
)
def test_daemon_refusals_map_to_their_status(client, add_token, reason, status):
    token = add_token("dev-daniel")
    with (
        connect_daemon(client, token) as (daemon, _),
        ThreadPoolExecutor(1) as pool,
    ):
        daemon.send_json(snapshot())
        pending = pool.submit(client.get, diff_url("27"), params={"file": "a.py"})
        rpc = daemon.receive_json()
        daemon.send_json({"type": "rpc_result", "id": rpc["id"], "ok": False,
                          "exit_code": None, "output_tail": "", "reason": reason})  # fmt: skip
        response = pending.result(10)

    assert response.status_code == status
    assert reason in response.text


def test_a_timeout_is_a_504_and_not_broadcast_to_the_ui(make_client, add_token):
    client = make_client(rpc_timeouts={"get_diff": 0.05})
    token = add_token("dev-daniel")
    with connect_ui(client) as ui, connect_daemon(client, token) as (daemon, _):
        read_until(ui, "devbox_status")
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        response = client.get(diff_url("27"))
        assert daemon.receive_json()["method"] == "get_diff"
        daemon.send_json(snapshot())  # a marker: anything broadcast before it shows
        seen = read_until(ui, "devbox_status")

    assert response.status_code == 504
    assert "timed out" in response.text
    assert all(m["type"] != "rpc_result" for m in seen)


def test_a_daemon_disconnect_is_a_409_and_not_broadcast_to_the_ui(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_ui(client) as ui,
        connect_daemon(client, token) as (daemon, _),
        ThreadPoolExecutor(1) as pool,
    ):
        read_until(ui, "devbox_status")
        daemon.send_json(snapshot())
        read_until(ui, "devbox_status")

        pending = pool.submit(client.get, diff_url("27"))
        daemon.receive_json()
        daemon.close()
        response = pending.result(10)
        seen = read_until(ui, "devbox_status")

    assert response.status_code == 409
    assert "disconnected" in response.text
    assert all(m["type"] != "rpc_result" for m in seen)
