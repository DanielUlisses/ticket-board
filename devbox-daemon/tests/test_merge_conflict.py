"""A merge that hits a conflict (tk.sh merge exit 10) sends ticket-merger: the daemon
runs merge-conflict.sh start, then `claude -p --agent ticket-merger` in the ticket's
worktree, and brings the ticket back for review (or aborts the merge when the agent
fails). finish_conflict / abort_conflict are the developer's decision. Fake tk.sh,
merge-conflict.sh and claude on PATH; the fake hub of test_tk_daemon.py."""

import asyncio
import importlib.util
import json
import os

from conftest import DAEMON_DIR, row
from test_rpcs import call, ship
from test_tk_daemon import FakeHub, daemon, run

KEY = "dev-test/alpha/b"


def conflicted(box, **files):
    """Ticket 05 in pr, with a worktree, whose merge exits 10 (conflicts)."""
    wt = box.work / "alpha--feat-05-x"
    wt.mkdir(parents=True)
    ticket = box.write("work/alpha/.scratch/b/05-x.md", "# 05: X\n\nDo X.\n")
    board = box.board(
        "alpha", "b",
        [row("05", "pr", worktree=str(wt), branch="feat-05-x", title="X",
             file=str(ticket))],
        files,
    )  # fmt: skip
    (board / "merge-exit").write_text("10")
    (board / "merge-out").write_text(
        "GATE mergeable FAIL conflicts with main — send ticket-merger\n"
    )
    return board, wt


async def merge_state(hub, *states, timeout=15):
    """The ticket's merge_conflict from the first snapshot where its state is one of
    `states`."""
    while True:
        snap = await hub.next("snapshot", timeout)
        for b in snap["boards"]:
            for t in b["tickets"]:
                mc = t.get("merge_conflict") or {}
                if mc.get("state") in states:
                    return t


def agent_calls(box):
    return [c for c in box.synth_calls() if "--agent" in c["argv"]]


def mc_verbs(box):
    return [c["argv"][0] for c in box.mc_calls()]


def test_a_conflict_starts_ticket_merger_in_the_worktree_then_returns_for_review(
    box, cert
):
    board, wt = conflicted(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            result = await call(hub, "merge_pr", ship())
            ticket = await merge_state(hub, "review")
            return result, ticket

    result, ticket = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, 10)
    assert "ticket-merger" in result["reason"]
    [start, *_] = box.mc_calls()
    assert start == {
        "argv": ["start", str(board), "05"],
        "cwd": str(box.work / "alpha"),
    }
    [agent] = agent_calls(box)
    assert agent["bin"] == "claude"
    argv = agent["argv"]
    assert argv[0] == "-p"
    assert argv[2:6] == ["--agent", "ticket-merger", "--model", "sonnet"]
    assert agent["cwd"] == str(wt)
    prompt = argv[1]  # before the variadic tool lists, which would swallow it
    for text in (str(board), "05", "main", "src/a.py", "src/b.py"):
        assert text in prompt
    mc = ticket["merge_conflict"]
    assert mc["files"] == ["src/a.py", "src/b.py"]
    assert mc["summary"] == "src/a.py: kept both sides\nchecks: pytest passed\n"
    assert mc_verbs(box) == ["start", "status"]  # never finish (nor abort) on its own


def test_a_resolved_merge_queues_a_synthesis_run_that_covers_it(box, cert):
    conflicted(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            return await hub.next("synthesis_finished")

    finished = run(scenario())

    assert (finished["board_key"], finished["nn"]) == (KEY, "05")
    [synth] = [c for c in box.synth_calls() if c["bin"] == "agy"]
    prompt = " ".join(synth["argv"])
    assert "ticket-merger merged the base" in prompt
    assert "src/a.py: kept both sides" in prompt


def test_still_unresolved_after_the_agent_aborts_the_merge_and_says_why(box, cert):
    board, _ = conflicted(box)
    (board / "mc-status-out").write_text(
        "UNMERGED\n  src/b.py\nMARKERS\n  src/b.py:3\nDIFF vs b before the merge:\n"
    )
    box.env["FAKE_MERGER_OUT"] = "src/b.py: both sides changed the same logic; left it"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            return await merge_state(hub, "failed")

    ticket = run(scenario())

    mc = ticket["merge_conflict"]
    assert mc["reason"] == "the merge is still unresolved; the merge was aborted"
    assert "left it" in mc["output_tail"] and "src/b.py:3" in mc["output_tail"]
    assert mc_verbs(box) == ["start", "status", "abort"]
    assert [c for c in box.synth_calls() if c["bin"] == "agy"] == []


def test_a_failed_agent_aborts_the_merge_with_its_output_tail(box, cert):
    conflicted(box)
    box.env |= {"FAKE_MERGER_OUT": "API error: overloaded", "FAKE_MERGER_EXIT": "1"}

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            return await merge_state(hub, "failed")

    mc = run(scenario())["merge_conflict"]

    assert mc["reason"] == "ticket-merger failed (exit 1); the merge was aborted"
    assert "API error: overloaded" in mc["output_tail"]
    assert mc_verbs(box)[-1] == "abort"
    assert "finish" not in mc_verbs(box)


def test_a_timed_out_agent_is_killed_and_the_merge_aborted(box, cert):
    conflicted(box)
    box.env |= {"FAKE_SYNTH_MODE_CLAUDE": "hang", "FAKE_SYNTH_HANG": "30"}

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, rpc_synthesis_timeout_s=1),
        ):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            return await merge_state(hub, "failed")

    mc = run(scenario())["merge_conflict"]

    assert mc["reason"] == "ticket-merger timed out after 1s; the merge was aborted"
    assert mc_verbs(box) == ["start", "status", "abort"]


def test_a_second_merge_while_the_resolver_runs_is_refused(box, cert):
    conflicted(box)
    box.env |= {"FAKE_SYNTH_MODE_CLAUDE": "slow", "FAKE_SYNTH_DELAY": "3"}

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship(), "r-1")
            await merge_state(hub, "resolving")
            again = await call(hub, "merge_pr", ship(), "r-2")
            resolve = await call(hub, "resolve_conflict", ship(), "r-3")
            return again, resolve

    again, resolve = run(scenario())

    for r in (again, resolve):
        assert (r["ok"], r["exit_code"]) == (False, None)
        assert r["reason"] == "ticket 05 is resolving a merge conflict (ticket-merger)"
    assert len([c for c in box.tk_calls() if c["argv"][0] == "merge"]) == 1
    assert len(agent_calls(box)) == 1


def test_up_to_date_retries_the_merge_once_without_an_agent(box, cert):
    board, _ = conflicted(box)
    (board / "mc-start-out").write_text(
        "UP TO DATE — feat-05-x already contains origin/main\n"
    )
    (board / "mc-start-exit").write_text("0")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            return await merge_state(hub, "up_to_date")

    mc = run(scenario())["merge_conflict"]

    assert len([c for c in box.tk_calls() if c["argv"][0] == "merge"]) == 2
    assert agent_calls(box) == []
    assert mc_verbs(box) == ["start"]
    assert mc["reason"].endswith(
        "retried: conflicts: the PR conflicts with the base branch"
    )


def in_review(box):
    """Ticket 05 whose resolved merge waits for the developer."""
    board, _ = conflicted(box)
    meta = {"tickets": {"05": {"merge_conflict": {
        "state": "review", "started_at": "2026-10-23T10:00:00Z",
        "finished_at": "2026-10-23T10:05:00Z", "base": "main", "files": ["src/a.py"],
        "summary": "src/a.py: kept both", "reason": None, "output_tail": "",
    }}}}  # fmt: skip
    (board / "hub-meta.json").write_text(json.dumps(meta))
    return board


def test_commit_merge_runs_finish_from_the_main_checkout_and_marks_it_committed(
    box, cert
):
    board = in_review(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            result = await call(hub, "finish_conflict", ship(confirm=True))
            return result, await merge_state(hub, "committed")

    result, ticket = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    assert "PUSHED merge abc1234" in result["output_tail"]
    assert box.mc_calls() == [
        {"argv": ["finish", str(board), "05"], "cwd": str(box.work / "alpha")}
    ]
    assert ticket["merge_conflict"]["state"] == "committed"


def test_commit_merge_needs_the_explicit_confirm(box, cert):
    in_review(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "finish_conflict", ship())

    result = run(scenario())

    assert result["ok"] is False and result["reason"].startswith("invalid params")
    assert box.mc_calls() == []


def test_commit_merge_refused_finish_keeps_it_in_review(box, cert):
    board = in_review(box)
    (board / "mc-finish-out").write_text("STILL UNRESOLVED — not committing\n")
    (board / "mc-finish-exit").write_text("4")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            result = await call(hub, "finish_conflict", ship(confirm=True))
            return result, await merge_state(hub, "review")

    result, ticket = run(scenario())

    assert (result["ok"], result["exit_code"]) == (False, 4)
    assert result["reason"].startswith("not committed")
    assert ticket["merge_conflict"]["reason"] == result["reason"]


def test_abort_puts_the_worktree_back_and_notes_the_conflict(box, cert):
    board = in_review(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            result = await call(hub, "abort_conflict", ship())
            return result, await merge_state(hub, "aborted")

    result, ticket = run(scenario())

    assert result["ok"] is True
    assert box.mc_calls() == [
        {"argv": ["abort", str(board), "05"], "cwd": str(box.work / "alpha")}
    ]
    assert "still conflicts" in ticket["merge_conflict"]["reason"]


def test_finish_and_abort_without_a_resolved_merge_are_refused(box, cert):
    conflicted(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            finish = await call(hub, "finish_conflict", ship(confirm=True), "r-1")
            abort = await call(hub, "abort_conflict", ship(), "r-2")
            return finish, abort

    for r in run(scenario()):
        assert (r["ok"], r["exit_code"]) == (False, None)
        assert r["reason"] == "ticket 05 has no resolved merge waiting for review"
    assert box.mc_calls() == []


def test_a_merge_with_a_resolved_merge_waiting_is_refused(box, cert):
    in_review(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "merge_pr", ship())

    result = run(scenario())

    assert result["reason"].startswith("ticket 05 has a resolved merge to review")
    assert [c for c in box.tk_calls() if c["argv"][0] == "merge"] == []


def test_a_resolve_a_killed_daemon_left_is_aborted_and_failed_at_the_next_export(
    box, cert
):
    board, _ = conflicted(box)
    meta = {"tickets": {"05": {"merge_conflict": {"state": "resolving", "files": []}}}}
    (board / "hub-meta.json").write_text(json.dumps(meta))

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.3):
            return await merge_state(hub, "failed")

    mc = run(scenario())["merge_conflict"]

    assert mc["reason"].startswith("interrupted")
    assert mc_verbs(box) == ["abort"]


def test_stopping_the_daemon_mid_resolve_kills_the_agent_and_aborts_the_merge(
    box, cert
):
    board, _ = conflicted(box)
    box.env |= {"FAKE_SYNTH_MODE_CLAUDE": "hang", "FAKE_SYNTH_HANG": "30"}

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600) as proc,
        ):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            await merge_state(hub, "resolving")
            while not agent_calls(box):
                await asyncio.sleep(0.05)
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 10)

    run(scenario())

    assert mc_verbs(box) == ["start", "abort"]
    mc = json.loads((board / "hub-meta.json").read_text())["tickets"]["05"]
    assert mc["merge_conflict"]["state"] == "failed"
    assert mc["merge_conflict"]["reason"].startswith("interrupted")


def tool_list(argv, flag):
    """The values after `flag`, up to the next option."""
    i = argv.index(flag) + 1
    out = []
    while i < len(argv) and not argv[i].startswith("--"):
        out.append(argv[i])
        i += 1
    return out


def load_agy_runner():
    spec = importlib.util.spec_from_file_location(
        "agy_runner", DAEMON_DIR / "agy_runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ticket_merger_gets_a_scoped_allow_list_and_never_a_permission_skip(box, cert):
    _, wt = conflicted(box)
    script = os.path.realpath(box.bin / "merge-conflict.sh")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            await merge_state(hub, "review")

    run(scenario())

    [agent] = agent_calls(box)
    argv = agent["argv"]
    assert load_agy_runner().refused_flag(argv) is None
    assert "--permission-mode" not in argv
    allowed = tool_list(argv, "--allowedTools")
    for tool in ("Read", "Grep", "Glob", f"Edit(/{wt}/**)", f"Write(/{wt}/**)",
                 "Bash(git add *)", "Bash(git diff *)", "Bash(git status *)",
                 f"Bash({script} status *)"):  # fmt: skip
        assert tool in allowed
    assert not [t for t in allowed if any(
        w in t for w in ("commit", "push", "finish", "abort", "Bash(*)", "Bash(git *)")
    ) or t in ("Bash", "Edit", "Write")]  # fmt: skip
    denied = tool_list(argv, "--disallowedTools")
    for tool in ("Bash(git commit *)", "Bash(git push *)",
                 f"Bash({script} finish *)", f"Bash({script} abort *)"):  # fmt: skip
        assert tool in denied


def test_a_stop_while_start_runs_leaves_it_resolving_and_the_restart_aborts_it(
    box, cert
):
    board, _ = conflicted(box)
    (board / "mc-start-slow").write_text("1.5")

    async def first():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600) as proc,
        ):
            await hub.next("snapshot")
            await call(hub, "merge_pr", ship())
            while not box.mc_calls():  # start is running
                await asyncio.sleep(0.05)
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 20)

    run(first())
    mc = json.loads((board / "hub-meta.json").read_text())["tickets"]["05"]
    assert mc["merge_conflict"]["state"] == "resolving"  # left for the restart
    assert mc_verbs(box) == ["start"]

    (board / "mc-start-slow").unlink()

    async def second():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.3):
            return await merge_state(hub, "failed")

    mc = run(second())["merge_conflict"]

    assert mc_verbs(box) == ["start", "abort"]
    assert box.mc_calls()[-1]["cwd"] == str(box.work / "alpha")
    assert mc["reason"].startswith("interrupted")
    assert "aborted" in mc["reason"]


def test_a_failed_resolve_can_still_be_aborted_by_the_developer(box, cert):
    board, _ = conflicted(box)
    meta = {"tickets": {"05": {"merge_conflict": {
        "state": "failed", "reason": "interrupted: abort failed (exit 1)",
    }}}}  # fmt: skip
    (board / "hub-meta.json").write_text(json.dumps(meta))

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            result = await call(hub, "abort_conflict", ship())
            return result, await merge_state(hub, "aborted")

    result, _ = run(scenario())

    assert result["ok"] is True
    assert mc_verbs(box) == ["abort"]


def test_a_merge_that_outlives_its_rpc_still_sends_ticket_merger_on_exit_10(box, cert):
    board, _ = conflicted(box)
    (board / "merge-slow").write_text("2")

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=0.3, rpc_timeout_s=1),
        ):
            await hub.next("snapshot")
            result = await call(hub, "merge_pr", ship())
            return result, await merge_state(hub, "review")

    result, _ = run(scenario())

    assert result["reason"] == "timed out after 1s"
    assert mc_verbs(box) == ["start", "status"]
    assert len(agent_calls(box)) == 1
