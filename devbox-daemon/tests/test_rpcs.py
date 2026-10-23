"""The ticket RPCs (launch_ticket, say_agent, pane_tail) through the daemon
process, over fake tk.sh / herdr on PATH."""

import asyncio
import fcntl
import json
from pathlib import Path

import pytest
from conftest import row
from test_tk_daemon import FakeHub, daemon, run

KEY = "dev-test/alpha/b"


def launch(nn="02", manual=False, **over):
    return {"board_key": KEY, "nn": nn, "type": "fix", "model": "opus",
            "effort": "high", "manual": manual, **over}  # fmt: skip


async def call(hub, method, params, rid="r-1"):
    """Send one rpc; its rpc_result."""
    await hub.send({"type": "rpc", "id": rid, "method": method, "params": params})
    while True:
        msg = await hub.next("rpc_result")
        if msg["id"] == rid:
            return msg


def calls(box, verb):
    return [c for c in box.tk_calls() if c["argv"][:1] == [verb]]


def test_launch_runs_tk_sh_launch_from_the_main_checkout_with_the_given_settings(
    box, cert
):
    board = box.board("alpha", "b", [row("01", "working"), row("02")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "launch_ticket", launch())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    assert "RECORDED 02" in result["output_tail"]
    [c] = calls(box, "launch")
    assert c["argv"] == ["launch", str(board), "02", "--type", "fix",
                         "--model", "opus", "--effort", "high"]  # fmt: skip
    assert c["cwd"] == str(box.work / "alpha")


def test_launch_re_checks_the_ticket_is_still_backlog_right_before(box, cert):
    box.board("alpha", "b", [row("02")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")  # 02 in backlog
            box.board("alpha", "b", [row("02", "working")])  # another driver took it
            return await call(hub, "launch_ticket", launch())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert result["reason"] == "ticket 02 is working, not backlog"
    assert calls(box, "launch") == []


def test_launch_waits_for_the_boards_flock_held_by_another_driver(box, cert):
    board = box.board("alpha", "b", [row("02")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            with open(board / ".board.lock", "w") as held:  # noqa: ASYNC230
                fcntl.flock(held, fcntl.LOCK_EX)
                await hub.send({"type": "rpc", "id": "r-1",
                                "method": "launch_ticket", "params": launch()})  # fmt: skip
                await asyncio.sleep(0.8)
                waited = calls(box, "launch") == []
            result = await hub.next("rpc_result")
            return waited, result

    waited, result = run(scenario())

    assert waited
    assert result["ok"] is True
    assert len(calls(box, "launch")) == 1


def test_launch_is_refused_while_herdr_is_down(box, cert):
    box.board("alpha", "b", [row("02")])
    box.env["FAKE_HERDR_DOWN"] = "1"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "launch_ticket", launch(manual=True))

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert result["reason"].startswith("herdr is down")
    assert calls(box, "launch") == []


def test_launch_is_refused_at_the_boxs_max_parallel_unless_manual(box, cert):
    box.board("alpha", "b", [row("01", "working"), row("02"), row("03")])
    box.board("beta", "c", [row("01", "review")])
    box.write(".config/tk-daemon/policy.json", {"max_parallel": 2})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            auto = await call(hub, "launch_ticket", launch("02"), "r-1")
            manual = await call(hub, "launch_ticket", launch("03", manual=True), "r-2")
            return auto, manual

    auto, manual = run(scenario())

    assert (auto["ok"], auto["exit_code"]) == (False, None)
    assert auto["reason"] == "the box is at max_parallel: 2 running, cap 2"
    assert manual["ok"] is True
    assert manual["data"] == {"over_cap": True, "running": 2, "max_parallel": 2}
    assert [c["argv"][2] for c in calls(box, "launch")] == ["03"]


def test_launches_at_once_do_not_overrun_max_parallel(box, cert):
    box.board("alpha", "b", [row("02")])
    box.board("beta", "c", [row("03")])
    box.write(".config/tk-daemon/policy.json", {"max_parallel": 1})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            for rid, key, nn in [("r-1", KEY, "02"), ("r-2", "dev-test/beta/c", "03")]:
                await hub.send({"type": "rpc", "id": rid, "method": "launch_ticket",
                                "params": launch(nn, board_key=key)})  # fmt: skip
            return [await hub.next("rpc_result"), await hub.next("rpc_result")]

    results = run(scenario())

    assert sorted(r["ok"] for r in results) == [False, True]
    assert len(calls(box, "launch")) == 1


def test_a_launch_whose_boards_flock_another_process_holds_is_refused_busy(box, cert):
    board = box.board("alpha", "b", [row("02")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=600, launch_lock_wait_s=0.5
        ):  # fmt: skip
            await hub.next("snapshot")
            with open(board / ".board.lock", "w") as held:  # noqa: ASYNC230
                fcntl.flock(held, fcntl.LOCK_EX)  # another driver, past the wait
                return await call(hub, "launch_ticket", launch("02"))

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert result["reason"].startswith("board busy")
    assert calls(box, "launch") == []


def test_launches_on_one_board_at_once_all_go_one_after_the_other(box, cert):
    # ▶ Frontier sends every backlog ticket's launch at once; each tk.sh launch
    # takes longer than the flock wait, yet none is refused busy.
    board = box.board("alpha", "b", [row("02"), row("03"), row("04")],
                      files={"launch-slow": "0.8"})  # fmt: skip

    async def scenario():
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=600, launch_lock_wait_s=0.3
        ):  # fmt: skip
            await hub.next("snapshot")
            for nn in ("02", "03", "04"):
                await hub.send({"type": "rpc", "id": f"r-{nn}",
                                "method": "launch_ticket", "params": launch(nn)})  # fmt: skip
            return [await hub.next("rpc_result") for _ in range(3)]

    results = run(scenario())

    assert [r.get("reason") for r in results] == [None] * 3
    assert all(r["ok"] for r in results)
    assert sorted(c["argv"][2] for c in calls(box, "launch")) == ["02", "03", "04"]
    rows = {r["nn"]: r["col"] for r in json.loads((board / "rows.json").read_text())}
    assert rows == {"02": "working", "03": "working", "04": "working"}


def test_a_queued_launch_past_its_rpc_timeout_still_launches_and_logs_its_outcome(
    box, cert
):
    board = box.board("alpha", "b", [row("02"), row("03")], files={"launch-slow": "1"})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=600, rpc_timeout_s=1.5
        ):  # fmt: skip
            await hub.next("snapshot")
            for nn in ("02", "03"):
                await hub.send({"type": "rpc", "id": f"r-{nn}",
                                "method": "launch_ticket", "params": launch(nn)})  # fmt: skip
                await asyncio.sleep(0.3)  # 02 takes the board first
            results = {}
            while len(results) < 2:
                r = await hub.next("rpc_result")
                results[r["id"]] = r
            await asyncio.sleep(1.5)  # 03's tk.sh launch ends after its rpc did
            return results

    results = run(scenario())

    assert results["r-02"]["ok"] is True
    assert results["r-03"]["reason"] == "timed out after 1.5s"  # waited behind 02
    rows = {r["nn"]: r["col"] for r in json.loads((board / "rows.json").read_text())}
    assert rows == {"02": "working", "03": "working"}  # not killed by the timeout
    log = box.daemon_log.read_text()
    assert "launch dev-test/alpha/b 03 finished after its rpc: exit 0" in log


def test_a_launch_whose_ticket_never_leaves_backlog_frees_its_slot_after_a_while(
    box, cert
):
    # tk.sh launch exits 0 yet leaves the row in backlog: the slot it reserved
    # must not count against max_parallel forever.
    box.board("alpha", "b", [row("02"), row("03")], files={"launch-exit": "0"})
    box.write(".config/tk-daemon/policy.json", {"max_parallel": 1})

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=0.3, reservation_ttl_s=0.5),
        ):
            await hub.next("snapshot")
            first = await call(hub, "launch_ticket", launch("02"), "r-1")
            await asyncio.sleep(1.5)
            second = await call(hub, "launch_ticket", launch("03"), "r-2")
            return first, second

    first, second = run(scenario())

    assert first["ok"] is True
    assert second["ok"] is True, second.get("reason")


def test_a_launched_ticket_missing_from_the_export_keeps_its_slot(box, cert):
    board = box.board("alpha", "b", [row("02"), row("03")], files={"launch-exit": "0"})
    box.write(".config/tk-daemon/policy.json", {"max_parallel": 1})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.3):
            await hub.next("snapshot")
            first = await call(hub, "launch_ticket", launch("02"), "r-1")
            (board / "rows.json").write_text(json.dumps([row("03")]))  # 02 unseen
            await asyncio.sleep(1)  # a few exports without 02
            second = await call(hub, "launch_ticket", launch("03"), "r-2")
            return first, second

    first, second = run(scenario())

    assert first["ok"] is True
    assert second["reason"] == "the box is at max_parallel: 1 running, cap 1"


def test_a_stop_lets_the_running_launch_finish_and_starts_no_queued_one(box, cert):
    board = box.board(
        "alpha", "b", [row("02"), row("03")], files={"launch-slow": "1.5"}
    )

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600) as proc,
        ):
            await hub.next("snapshot")
            for nn in ("02", "03"):
                await hub.send({"type": "rpc", "id": f"r-{nn}",
                                "method": "launch_ticket", "params": launch(nn)})  # fmt: skip
                await asyncio.sleep(0.3)  # 02 takes the board first; 03 queues
            proc.terminate()
            await proc.wait()

    run(scenario())

    assert [c["argv"][2] for c in calls(box, "launch")] == ["02"]
    rows = {r["nn"]: r["col"] for r in json.loads((board / "rows.json").read_text())}
    assert rows == {"02": "working", "03": "backlog"}  # 02 finished within the grace


def test_a_stop_while_a_launch_waits_on_an_outside_flock_never_starts_it(box, cert):
    board = box.board("alpha", "b", [row("02")])

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, launch_lock_wait_s=5) as proc,
        ):
            await hub.next("snapshot")
            with open(board / ".board.lock", "w") as held:  # noqa: ASYNC230
                fcntl.flock(held, fcntl.LOCK_EX)  # another driver
                await hub.send({"type": "rpc", "id": "r-1", "method": "launch_ticket",
                                "params": launch("02")})  # fmt: skip
                await asyncio.sleep(0.5)  # the launch waits on the flock
                proc.terminate()
                await asyncio.sleep(0.5)
            # flock released: the waiting launch gets it, during the shutdown
            await asyncio.wait_for(proc.wait(), 10)

    run(scenario())

    assert calls(box, "launch") == []
    assert "daemon stopping" in box.daemon_log.read_text()


def say(message="Use the existing tokens.\nThen rerun the tests."):
    return {"board_key": KEY, "nn": "02", "message": message}


def test_say_relays_the_message_through_a_file_to_tk_sh_say(box, cert):
    board = box.board("alpha", "b", [row("02", "needs")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "say_agent", say())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    assert "SENT" in result["output_tail"]
    [c] = calls(box, "say")
    assert c["argv"][:3] == ["say", str(board), "02"]
    assert c["cwd"] == str(box.work / "alpha")
    assert c["message"] == "Use the existing tokens.\nThen rerun the tests."
    assert not Path(c["argv"][3]).exists()  # the temp file is gone


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (20, ("dialog: the agent is at a dialog or not at its prompt; "
              "answer it in its herdr tab")),
        (21, "no agent: the ticket's agent isn't running"),
    ],
)  # fmt: skip
def test_say_maps_tk_sh_exit_codes_to_reasons(box, cert, code, reason):
    box.board("alpha", "b", [row("02", "needs")], files={"say-exit": str(code)})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "say_agent", say())

    result = run(scenario())

    assert (result["ok"], result["exit_code"], result["reason"]) == (
        False,
        code,
        reason,
    )
    assert "NOT SENT" in result["output_tail"]


def test_pane_tail_returns_the_agents_recent_lines_without_a_re_export(box, cert):
    pane = "\n".join(f"line {i}" for i in range(1, 301))
    board = box.board("alpha", "b", [row("02", "working")], files={"pane.txt": pane})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            exports = len(calls(box, "export"))
            result = await call(
                hub, "pane_tail", {"board_key": KEY, "nn": "02", "lines": 180}
            )
            await asyncio.sleep(0.5)
            return result, len(calls(box, "export")) - exports

    result, exports = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    lines = result["data"]["pane"].splitlines()
    assert (len(lines), lines[0], lines[-1]) == (180, "line 121", "line 300")
    [c] = calls(box, "show")
    assert c["argv"] == ["show", str(board), "02", "180"]
    assert exports == 0  # read-only: no re-export after it


def test_pane_tail_keeps_tk_sh_stderr_out_of_the_pane_and_gives_it_on_failure(
    box, cert
):
    box.board("alpha", "b", [row("02", "working")],
              files={"pane.txt": "agent line", "show-stderr": "warning: slow herdr"})  # fmt: skip
    box.board("alpha", "c", [row("02", "needs")],
              files={"agent-gone": "", "show-stderr": "herdr: no such pane"})  # fmt: skip

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            ok = await call(
                hub, "pane_tail", {"board_key": KEY, "nn": "02", "lines": 60}, "r-1"
            )
            gone = await call(hub, "pane_tail",
                              {"board_key": "dev-test/alpha/c", "nn": "02", "lines": 60},
                              "r-2")  # fmt: skip
            return ok, gone

    ok, gone = run(scenario())

    assert ok["data"]["pane"].strip() == "agent line"
    assert "herdr: no such pane" in gone["output_tail"]


def test_pane_tail_of_a_ticket_without_a_live_agent_says_so(box, cert):
    box.board("alpha", "b", [row("02", "needs")], files={"agent-gone": ""})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(
                hub, "pane_tail", {"board_key": KEY, "nn": "02", "lines": 60}
            )

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, 21)
    assert result["reason"] == "no agent: the ticket's agent isn't running"


def test_an_rpc_whose_params_fail_the_schema_is_refused_without_running_anything(
    box, cert
):
    box.board("alpha", "b", [row("02")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            before = len(box.tk_calls())
            result = await call(hub, "launch_ticket", launch(nn="--help"))
            return result, len(box.tk_calls()) - before

    result, new_calls = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert result["reason"].startswith("invalid params")
    assert new_calls == 0
