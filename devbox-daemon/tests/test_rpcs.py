"""The ticket RPCs (launch_ticket, say_agent, pane_tail) through the daemon
process, over fake tk.sh / herdr on PATH."""

import asyncio
import fcntl
import json
import re
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


def ship(nn="05", **extra):
    return {"board_key": KEY, "nn": nn, **extra}


# Review optional: open_pr / ssh_push without a human approval (test_review.py
# has the require_human_before_pr gate).
UNGATED = {"policy.json": {"require_human_before_pr": False}}


@pytest.mark.parametrize(
    ("method", "col", "params", "argv"),
    [
        ("open_pr", "human", {}, ["pr", "{b}", "05"]),
        ("ssh_push", "human", {}, ["push", "{b}", "05"]),
        ("merge_pr", "pr", {}, ["merge", "{b}", "05"]),
        ("resolve_nocode", "human", {"reason": "notes only"},
         ["resolve", "{b}", "05", "--no-code", "notes only"]),
        ("resolve_ticket", "done", {}, ["resolve", "{b}", "05"]),
    ],
)  # fmt: skip
def test_each_ship_verb_runs_its_tk_sh_call_from_the_main_checkout(
    box, cert, method, col, params, argv
):
    board = box.board(
        "alpha", "b", [row("05", col, landed="yes:ancestry")], files=UNGATED
    )

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, method, ship(**params))

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    [c] = calls(box, argv[0])
    assert c["argv"] == [a.replace("{b}", str(board)) for a in argv]
    assert c["cwd"] == str(box.work / "alpha")


@pytest.mark.parametrize(
    ("method", "col"),
    [("open_pr", "pr"), ("ssh_push", "working"), ("merge_pr", "human")],
)
def test_a_ship_verb_re_checks_the_column_right_before(box, cert, method, col):
    box.board("alpha", "b", [row("05", "human" if col != "human" else "pr")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            box.board("alpha", "b", [row("05", col)])  # another driver moved it
            return await call(hub, method, ship())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert f"ticket 05 is {col}, not" in result["reason"]
    assert not [c for c in box.tk_calls() if c["argv"][0] in ("pr", "push", "merge")]


def test_a_ship_verb_waits_for_the_boards_flock(box, cert):
    board = box.board("alpha", "b", [row("05", "human")], files=UNGATED)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            with open(board / ".board.lock", "w") as held:  # noqa: ASYNC230
                fcntl.flock(held, fcntl.LOCK_EX)
                await hub.send({"type": "rpc", "id": "r-1",
                                "method": "ssh_push", "params": ship()})  # fmt: skip
                await asyncio.sleep(0.8)
                waited = calls(box, "push") == []
            return waited, await hub.next("rpc_result")

    waited, result = run(scenario())

    assert waited
    assert result["ok"] is True


def test_merge_gate_failures_come_back_by_exit_code_with_each_gate(box, cert):
    board = box.board("alpha", "b", [row("05", "pr")])
    (board / "merge-exit").write_text("12")
    (board / "merge-out").write_text(
        "GATE pr PASS #7\nGATE ready PASS\nGATE mergeable PASS\n"
        "GATE checks FAIL failed: build\nGATE review PASS\nNOT MERGED — a gate failed\n"
    )

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "merge_pr", ship())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, 12)
    assert result["reason"].startswith("gate failed")
    assert result["data"]["gates"] == [
        {"gate": "pr", "status": "pass", "detail": "#7"},
        {"gate": "ready", "status": "pass", "detail": ""},
        {"gate": "mergeable", "status": "pass", "detail": ""},
        {"gate": "checks", "status": "fail", "detail": "failed: build"},
        {"gate": "review", "status": "pass", "detail": ""},
    ]


def test_verify_landed_returns_the_verdict_and_the_open_pr(box, cert):
    board = box.board("alpha", "b", [row("05", "pr")])
    (board / "why-out").write_text(
        "ticket 05: in-progress, forge github\nLANDED no\n"
        'OPEN PR {"number":7,"checks":["build=ok"]}\n'
    )

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "verify_landed", ship())

    result = run(scenario())

    assert result["ok"] is True
    assert result["data"] == {
        "landed": "no",
        "open_pr": '{"number":7,"checks":["build=ok"]}',
    }
    assert calls(box, "why")[0]["argv"][2] == "05"


def test_resolve_without_a_reason_is_refused_and_runs_nothing(box, cert):
    box.board("alpha", "b", [row("05", "done")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            blank = await call(hub, "resolve_nocode", ship(reason="   "), "r-1")
            empty = await call(hub, "resolve_nocode", ship(reason=""), "r-2")
            return blank, empty

    blank, empty = run(scenario())

    assert blank["ok"] is False
    assert "needs a reason" in blank["reason"]
    assert empty["ok"] is False
    assert calls(box, "resolve") == []


def test_plain_resolve_of_a_landed_ticket_succeeds_where_no_code_is_refused(box, cert):
    board = box.board("alpha", "b", [row("05", "done", landed="yes:ancestry")])
    (board / "landed").write_text("")  # tk.sh refuses resolve --no-code on it

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            plain = await call(hub, "resolve_ticket", ship(), "r-1")
            # resolve_nocode is not even offered for done: the col re-check refuses it.
            nocode = await call(hub, "resolve_nocode", ship(reason="x"), "r-2")
            box.board("alpha", "b", [row("05", "human")])
            refused = await call(hub, "resolve_nocode", ship(reason="x"), "r-3")
            return plain, nocode, refused

    plain, nocode, refused = run(scenario())

    assert (plain["ok"], plain["exit_code"]) == (True, 0)
    assert nocode["ok"] is False and "ticket 05 is done, not" in nocode["reason"]
    assert (refused["ok"], refused["exit_code"]) == (False, 1)
    assert "use plain resolve" in refused["output_tail"]
    assert next(c["argv"] for c in calls(box, "resolve")) == [
        "resolve",
        str(board),
        "05",
    ]


@pytest.mark.parametrize(
    ("method", "col", "params"),
    [("resolve_nocode", "working", {"reason": "x"}), ("resolve_nocode", "backlog", {"reason": "x"}),
     ("resolve_ticket", "pr", {}), ("verify_landed", "human", {})],
)  # fmt: skip
def test_resolve_and_verify_re_check_the_column_right_before(
    box, cert, method, col, params
):
    box.board("alpha", "b", [row("05", "pr" if col != "pr" else "done")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            box.board("alpha", "b", [row("05", col)])
            return await call(hub, method, ship(**params))

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert f"ticket 05 is {col}, not" in result["reason"]
    assert not [c for c in box.tk_calls() if c["argv"][0] in ("resolve", "why")]


def test_resolve_of_an_already_resolved_ticket_is_refused_before_tk_sh_runs(box, cert):
    box.board("alpha", "b", [row("05", "done", landed="-")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "resolve_ticket", ship())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert "ticket 05 is not landed" in result["reason"]
    assert calls(box, "resolve") == []


def test_a_stop_while_ship_verbs_wait_never_runs_them(box, cert):
    board = box.board("alpha", "b", [row("05", "human")])

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, launch_lock_wait_s=5) as proc,
        ):
            await hub.next("snapshot")
            with open(board / ".board.lock", "w") as held:  # noqa: ASYNC230
                fcntl.flock(held, fcntl.LOCK_EX)
                for rid in (
                    "r-1",
                    "r-2",
                ):  # the first waits on the flock, the second on the queue
                    await hub.send({"type": "rpc", "id": rid,
                                    "method": "ssh_push", "params": ship()})  # fmt: skip
                await asyncio.sleep(0.5)
                proc.terminate()
                await asyncio.sleep(0.5)
            await asyncio.wait_for(proc.wait(), 10)

    run(scenario())

    assert calls(box, "push") == []
    assert box.daemon_log.read_text().count("daemon stopping") >= 2


def test_exit_5_on_a_pr_reaches_the_reason(box, cert):
    board = box.board("alpha", "b", [row("05", "human")], files=UNGATED)
    (board / "pr-exit").write_text("5")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "open_pr", ship())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, 5)
    assert "claude runner" in result["reason"]


@pytest.mark.parametrize("code", [10, 11])
def test_merge_exits_10_and_11_come_back_with_their_reason(box, cert, code):
    board = box.board("alpha", "b", [row("05", "pr")])
    (board / "merge-exit").write_text(str(code))
    (board / "merge-out").write_text("GATE pr PASS #7\nNOT MERGED\n")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "merge_pr", ship())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, code)
    assert result["reason"] and result["data"]["merged"] is False


def test_a_merge_that_went_through_but_whose_resolve_failed_says_so(box, cert):
    board = box.board("alpha", "b", [row("05", "pr")])
    (board / "merge-exit").write_text("1")
    (board / "merge-out").write_text(
        "GATE pr PASS #7\nMERGED 05 #7 (squash)\nERROR: resolve failed: boom\n"
    )

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "merge_pr", ship())

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, 1)
    assert result["data"]["merged"] is True
    assert result["reason"] == "merged, but resolve failed: ERROR: resolve failed: boom"


def test_a_clean_merge_is_flagged_merged(box, cert):
    board = box.board("alpha", "b", [row("05", "pr")])
    (board / "merge-out").write_text("GATE pr PASS #7\nMERGED 05 #7 (squash)\n")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "merge_pr", ship())

    result = run(scenario())

    assert result["ok"] is True
    assert result["data"]["merged"] is True


def sync_logs(box):
    return sorted((box.home / ".local/state/tk-daemon").glob("sync-*.log"))


@pytest.mark.parametrize("col", ["working", "review"])
def test_sync_box_is_refused_while_a_ticket_is_working_or_in_review_unless_forced(
    box, cert, col
):
    box.board("alpha", "b", [row("01", col)])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            refused = await call(hub, "sync_box", {"force": False}, "r-1")
            before = sync_logs(box)
            forced = await call(hub, "sync_box", {"force": True}, "r-2")
            return refused, before, forced

    refused, before, forced = run(scenario())

    assert (refused["ok"], refused["exit_code"]) == (False, None)
    assert "working or in review" in refused["reason"] and before == []
    assert forced["ok"] is True and len(sync_logs(box)) == 1


def test_sync_box_runs_devbox_sync_detached_via_systemd_run_logging_to_state(box, cert):
    box.board("alpha", "b", [row("01", "backlog")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "sync_box", {"force": False})

    result = run(scenario())

    assert result["ok"] is True
    (argv,) = [
        json.loads(x) for x in (box.tmp / "systemd.log").read_text().splitlines()
    ]
    (log,) = sync_logs(box)
    assert argv[:2] == ["--user", "--collect"]
    assert any("devbox sync" in a for a in argv) and str(log) in argv
    assert log.read_text() == "devbox sync: done\n"
    assert log.name.startswith("sync-") and str(log) in result["output_tail"]


def test_the_last_sync_result_and_version_are_reported_after_the_daemon_restarts(
    box, cert
):
    box.board("alpha", "b", [row("01", "backlog")])
    box.env["FAKE_DEVBOX_EXIT"] = "3"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            await call(hub, "sync_box", {"force": False})
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            return await hub.next("hello")

    health = run(scenario())["health"]

    last = health["last_sync"]
    assert (last["exit_code"], last["log_tail"]) == (3, "devbox sync: done\n")
    assert last["log_path"] == str(sync_logs(box)[0])
    assert health["daemon_version"] == "0.1.0"


def stale_log(box, stamp, exit_code=None, text="partial\n"):
    d = box.home / ".local/state/tk-daemon"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"sync-{stamp}.log").write_text(text)
    if exit_code is not None:
        (d / f"sync-{stamp}.exit").write_text(f"{exit_code}\n")


def hello_health(box, cert):
    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            return await hub.next("hello")

    return run(scenario())["health"]


def test_a_sync_with_no_exit_file_and_no_active_unit_is_reported_interrupted(box, cert):
    stale_log(box, "20261023T120005123Z")

    last = hello_health(box, cert)["last_sync"]

    assert (last["exit_code"], last["interrupted"]) == (None, True)
    assert last["started_at"] == "2026-10-23T12:00:05Z"
    assert last["log_tail"] == "partial\n"


def test_a_sync_whose_unit_is_still_active_is_running_not_interrupted(box, cert):
    stale_log(box, "20261023T120005123Z")
    box.env["FAKE_SYSTEMCTL_ACTIVE"] = "tk-sync-20261023T120005123Z"

    last = hello_health(box, cert)["last_sync"]

    assert (last["exit_code"], last["interrupted"]) == (None, False)


def test_sync_box_is_refused_even_forced_while_a_sync_is_running(box, cert):
    box.board("alpha", "b", [row("01", "backlog")])
    stale_log(box, "20261023T120005123Z")
    box.env["FAKE_SYSTEMCTL_ACTIVE"] = "tk-sync-20261023T120005123Z"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "sync_box", {"force": True})

    refused = run(scenario())

    assert refused["ok"] is False and "already running" in refused["reason"]
    assert len(sync_logs(box)) == 1


def test_logs_not_named_by_the_daemon_are_ignored(box, cert):
    stale_log(box, "20261023T120005Z", exit_code=0, text="the real one\n")
    stale_log(box, "zzz", text="stray\n")
    stale_log(box, "2026-10-23", text="stray too\n")

    last = hello_health(box, cert)["last_sync"]

    assert last is not None and last["log_tail"] == "the real one\n"
    assert last["started_at"] == "2026-10-23T12:00:05Z"


def test_back_to_back_syncs_get_distinct_logs_and_neither_is_overwritten(box, cert):
    box.board("alpha", "b", [row("01", "backlog")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            await call(hub, "sync_box", {"force": False}, "r-1")
            await call(hub, "sync_box", {"force": False}, "r-2")

    run(scenario())

    logs = sync_logs(box)
    assert len(logs) == 2
    assert all(p.with_suffix(".exit").exists() for p in logs)
    assert all(re.fullmatch(r"sync-\d{8}T\d{6}\d{3}Z\.log", p.name) for p in logs)


def test_a_systemd_run_failure_is_refused_with_its_stderr_and_leaves_no_log(box, cert):
    box.board("alpha", "b", [row("01", "backlog")])
    box.env["FAKE_SYSTEMD_FAIL"] = "Failed to connect to user bus"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "sync_box", {"force": False})

    refused = run(scenario())

    assert refused["ok"] is False
    assert "Failed to connect to user bus" in refused["reason"]
    assert sync_logs(box) == []


def test_the_reported_version_follows_the_installed_version_file(box, cert):
    assert hello_health(box, cert)["daemon_version"] == "0.1.0"

    box.write(".local/share/tk-hub/VERSION", "abc1234\n")

    assert hello_health(box, cert)["daemon_version"] == "abc1234"
