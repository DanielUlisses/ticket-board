"""The scheduler wired into the hub: a decision becomes an RPC to the board's
daemon and an entry in the Scheduler log; the hub settings it reads (total cap,
▶ Run waves) live in SQLite and are edited over REST."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from conftest import assert_valid, connect_daemon, connect_ui, read_until, snapshot

from hub_server.cli import main as cli

KEY = "dev-daniel/ticket-board/tk-hub"
START = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


def test_a_decision_sends_its_rpc_and_is_logged(make_client, add_token):
    client = make_client(schedule=True)
    token = add_token("dev-daniel")
    with connect_ui(client) as ui, connect_daemon(client, token) as (daemon, _):
        # ticket-board is an auto board with 27 on its frontier; the box runs
        # 4 of its 5.
        daemon.send_json(snapshot())
        rpc = daemon.receive_json()
        logged = read_until(ui, "scheduler_log")[-1]
        daemon.send_json({"type": "rpc_result", "id": rpc["id"], "ok": True,
                          "exit_code": 0, "output_tail": "LAUNCHED 27"})  # fmt: skip
        read_until(ui, "rpc_result")

    assert_valid(rpc)
    assert (rpc["method"], rpc["params"]) == ("launch_ticket", {
        "board_key": KEY, "nn": "27", "type": "feat", "model": "sonnet",
        "effort": "low", "manual": False})  # fmt: skip
    assert_valid(logged)
    entry = {k: logged["entry"][k] for k in ("action", "method", "board_key", "nn")}
    assert entry == {"action": "sent", "method": "launch_ticket",
                     "board_key": KEY, "nn": "27"}  # fmt: skip
    [latest, *_] = client.get("/api/scheduler").json()["entries"]
    assert latest == logged["entry"]


def test_the_total_cap_is_a_hub_setting_kept_in_sqlite(make_client):
    client = make_client()
    assert client.get("/api/settings").json()["total_cap"] == 8

    with connect_ui(client) as ui:
        saved = client.post("/api/settings", json={"total_cap": 3})
        event = read_until(ui, "hub_settings")[-1]

    assert saved.status_code == 200
    assert saved.json()["total_cap"] == 3
    assert_valid(event)
    assert event["settings"] == saved.json()
    assert make_client().get("/api/settings").json()["total_cap"] == 3
    assert client.post("/api/settings", json={"total_cap": 0}).status_code == 422


def test_waves_with_nothing_left_turn_themselves_off_and_say_why(make_client):
    client = make_client(schedule=True)
    # Every pass 30 s after the last: the stop must hold across an export cycle.
    ticks = itertools.count()
    client.app.state.scheduler.clock = lambda: (
        START + timedelta(seconds=30 * next(ticks))
    )

    with connect_ui(client) as ui:
        client.post("/api/settings", json={"waves": True, "waves_start": None})
        client.post("/api/settings", json={"total_cap": 8})  # one more pass
        logged = read_until(ui, "scheduler_log")[-1]
        off = read_until(ui, "hub_settings")[-1]

    assert_valid(logged)
    assert (logged["entry"]["action"], logged["entry"]["reason"]) == (
        "waves_off", "nothing left to launch, ship or merge")  # fmt: skip
    assert off["settings"]["waves"] is False
    assert client.get("/api/settings").json()["waves"] is False


def test_a_start_time_comes_only_with_turning_waves_on(make_client):
    client = make_client()
    start = "2026-10-24T06:00:00Z"

    alone = client.post("/api/settings", json={"waves_start": start})
    off = client.post("/api/settings", json={"waves": False, "waves_start": start})
    on = client.post("/api/settings", json={"waves": True, "waves_start": start})

    assert (alone.status_code, off.status_code) == (422, 422)
    assert on.json() | {"total_cap": 8} == {"total_cap": 8, "waves": True,
                                            "waves_start": start}  # fmt: skip


@pytest.mark.parametrize(
    ("flags", "schedule"), [([], True), (["--no-schedule"], False)]
)
def test_serve_runs_the_scheduler_unless_told_not_to(monkeypatch, tmp_path, flags,
                                                      schedule):  # fmt: skip
    made = {}
    monkeypatch.setattr("hub_server.app.create_app", lambda **kw: made.update(kw))
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)

    assert cli(["serve", "--db", str(tmp_path / "hub.db"), *flags]) == 0
    assert made["schedule"] is schedule
