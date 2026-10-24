"""POST /api/boards/{key}/retro, /retro/apply and /sweep: the matching rpc on the
board's daemon (a fake one on /ws/daemon), its result returned to the caller."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import assert_valid, connect_daemon, snapshot

KEY = "dev-daniel/ticket-board/tk-hub"


def result(rpc, ok=True, **extra):
    return {"type": "rpc_result", "id": rpc["id"], "ok": ok, "exit_code": 0 if ok else 1,
            "output_tail": "", **extra}  # fmt: skip


@pytest.mark.parametrize(
    ("path", "body", "method", "params"),
    [
        ("retro", None, "run_retro", {"board_key": KEY}),
        ("sweep", None, "sweep_board", {"board_key": KEY}),
        (
            "retro/apply",
            {"mark": "approve", "ids": ["M1", "E2"]},
            "retro_apply",
            {"board_key": KEY, "mark": "approve", "ids": ["M1", "E2"]},
        ),
    ],
)
def test_each_route_sends_its_rpc_and_returns_the_daemons_data(
    client, add_token, path, body, method, params
):
    token = add_token("dev-daniel")
    data = {"proposals_md": "### M1 x"}
    with (
        connect_daemon(client, token) as (daemon, _),
        ThreadPoolExecutor(1) as pool,
    ):
        daemon.send_json(snapshot())
        pending = pool.submit(client.post, f"/api/boards/{KEY}/{path}", json=body)
        rpc = daemon.receive_json()
        assert_valid(rpc)
        assert (rpc["method"], rpc["params"]) == (method, params)
        daemon.send_json(result(rpc, data=data))
        response = pending.result(10)

    assert response.status_code == 200
    assert response.json()["data"] == data


def test_a_refusal_is_a_409_and_a_failure_a_502(client, add_token):
    token = add_token("dev-daniel")
    with (
        connect_daemon(client, token) as (daemon, _),
        ThreadPoolExecutor(1) as pool,
    ):
        daemon.send_json(snapshot())
        url = f"/api/boards/{KEY}/retro"
        pending = pool.submit(client.post, url)
        rpc = daemon.receive_json()
        daemon.send_json(result(rpc, ok=False, exit_code=None,
                                reason="retro needs every ticket done: 02 not done"))  # fmt: skip
        refused = pending.result(10)
        pending = pool.submit(client.post, url)
        rpc = daemon.receive_json()
        daemon.send_json(result(rpc, ok=False, output_tail="claude: boom"))
        failed = pending.result(10)

    assert refused.status_code == 409
    assert "not done" in refused.text
    assert failed.status_code == 502
    assert "claude: boom" in failed.text


def test_unknown_board_is_404_and_an_offline_devbox_409(client, add_token):
    token = add_token("dev-daniel")
    with connect_daemon(client, token) as (daemon, _):
        daemon.send_json(snapshot())
        assert client.post("/api/boards/nope/x/y/sweep").status_code == 404
    assert client.post(f"/api/boards/{KEY}/sweep").status_code == 409
