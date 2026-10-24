"""run_retro, retro_apply and sweep_board through the daemon process, over fake
tk.sh, claude and sweep.sh on PATH."""

import json

from conftest import row
from test_rpcs import KEY, call, calls
from test_tk_daemon import FakeHub, daemon, run

DONE = [row("01", "done", branch="fix-01-a"), row("02", "done", branch="fix-02-b")]


def test_run_retro_feeds_tk_retro_to_claude_and_saves_what_it_prints(box, cert):
    board = box.board("alpha", "b", DONE, {"retro-out": "RETRO board b\nTICKET 01"})
    box.env["FAKE_RETRO_OUT"] = "### M1 rule\n\n### E1 edit"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "run_retro", {"board_key": KEY})

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    assert result["data"]["proposals_md"] == "### M1 rule\n\n### E1 edit\n"
    [retro] = [c for c in box.synth_calls() if "--agent" in c["argv"]]
    assert retro["bin"] == "claude"
    assert retro["argv"] == ["-p", "--agent", "ticket-retro", "--model", "sonnet"]
    assert retro["stdin"].startswith("RETRO board b\nTICKET 01")
    assert retro["cwd"] == str(box.work / "alpha")
    assert [c["argv"] for c in calls(box, "retro")] == [["retro", str(board)]]
    [save] = calls(box, "retro-save")
    assert save["argv"] == ["retro-save", str(board)]
    assert save["stdin"] == "### M1 rule\n\n### E1 edit\n"


def test_run_retro_saves_nothing_when_claude_fails(box, cert):
    box.board("alpha", "b", DONE)
    box.env["FAKE_RETRO_EXIT"] = "3"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "run_retro", {"board_key": KEY})

    result = run(scenario())

    assert result["ok"] is False
    assert calls(box, "retro-save") == []


def test_run_retro_is_refused_while_a_ticket_is_not_done(box, cert):
    box.board("alpha", "b", [*DONE, row("03", "working", branch="fix-03-c")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "run_retro", {"board_key": KEY})

    result = run(scenario())

    assert result["ok"] is False
    assert "not done" in result["reason"]
    assert calls(box, "retro") == []


def test_retro_apply_marks_through_tk_sh_and_returns_the_regenerated_results(box, cert):
    board = box.board("alpha", "b", DONE)
    (board / "retro").mkdir()
    (board / "retro" / "proposals.md").write_text("### M1 rule\n")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(
                hub, "retro_apply",
                {"board_key": KEY, "mark": "approve", "ids": ["M1", "E2"]},
            )  # fmt: skip

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    [c] = calls(box, "retro-apply")
    assert c["argv"] == ["retro-apply", str(board), "approve", "M1", "E2"]
    assert "approve M1" in result["data"]["results_md"]
    assert result["data"]["proposals_md"] == "### M1 rule\n"


LIST = "\n".join(  # noqa: FLY002
    [
        "# root=/x base=main",
        "# kind\tbranch\tpath\tstate\treason\tagent\tdirty\tworkspace\tboard\tflags",
        "worktree\tfix-01-a\t{wt}/alpha--fix-01-a\tmerged\t-\t-\t0\t-\tb\tmerged-branch,removable",
        "worktree\tfix-02-b\t{wt}/alpha--fix-02-b\tmerged\t-\tidle\t3\t-\tb\tdirty,keep",
        "worktree\tother-9\t{wt}/alpha--other-9\tmerged\t-\t-\t0\t-\tzig\tremovable",
        "branch\tfix-04-d\t-\tmerged\t-\t-\t-\t-\tb\tremovable",
        "branch\tfix-05-e\t-\tunmerged\t-\t-\t-\t-\tb\tkeep",
        "branch\tother-8\t-\tmerged\t-\t-\t-\t-\t-\tremovable",
        "workspace\t-\t{wt}/alpha--fix-03-c\tgone\tcheckout-missing\t-\t-\tw9\t-\torphan-workspace,removable",
        "workspace\t-\t{wt}/alpha--fix-06-f\tgone\tcheckout-missing\tbusy\t-\tw7\t-\tagent-busy,keep",
        "branch\tfix-07-g\t-\tmerged\t-\t-\t-\t-\tb\tremovable",
        "branch\tfix-08-h\t-\tunmerged\tno-agent\t-\t-\t-\tb\tno-agent,keep",
        "workspace\t-\t{wt}/elsewhere\tgone\tcheckout-missing\t-\t-\tw1\t-\tremovable",
        "",
    ]
)


def test_sweep_removes_only_this_boards_removable_rows_and_reports_the_rest(box, cert):
    tickets = [
        row(f"0{i}", "done", branch=f"fix-0{i}-{c}") for i, c in enumerate("abcdef", 1)
    ]
    tickets.append(row("07", "working", branch="fix-07-g"))
    board = box.board("alpha", "b", tickets, {"retro-results.md": "# Retro results\n"})
    other = box.board("zig", "z", [row("01", "done", branch="other-9")])
    del other
    listing = box.tmp / "sweep-list.tsv"
    listing.write_text(LIST.format(wt=box.work))
    box.env["FAKE_SWEEP_LIST"] = str(listing)
    box.env["FAKE_SWEEP_EXITS"] = json.dumps({"fix-04-d": 4})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "sweep_board", {"board_key": KEY})

    result = run(scenario())

    assert (result["ok"], result["exit_code"]) == (True, 0)
    del board
    argvs = [c["argv"] for c in box.sweep_calls()]
    assert argvs[0][0] == "list"
    assert argvs[1:] == [
        ["remove", "--worktree", f"{box.work}/alpha--fix-01-a"],
        ["remove", "--branch", "fix-04-d"],
        ["remove", "--workspace", "w9"],
    ]
    assert all(c["cwd"] == str(box.work / "alpha") for c in box.sweep_calls())
    data = result["data"]
    assert data["removed"] == [
        {"kind": "worktree", "ref": f"{box.work}/alpha--fix-01-a"},
        {"kind": "workspace", "ref": "w9"},
    ]
    skipped = {(s["kind"], s["ref"]): s["reason"] for s in data["skipped"]}
    assert set(skipped) == {
        ("worktree", f"{box.work}/alpha--fix-02-b"),
        ("branch", "fix-04-d"),
        ("branch", "fix-05-e"),
        ("workspace", "w7"),
    }
    assert "dirty" in skipped[("worktree", f"{box.work}/alpha--fix-02-b")]
    assert "agent-busy" in skipped[("workspace", "w7")]
    assert "unmerged" in skipped[("branch", "fix-04-d")]


def test_sweep_leaves_a_ticket_that_is_not_done_alone_and_says_why_it_kept_a_row(
    box, cert
):
    tickets = [row("04", "done", branch="fix-04-d"), row("08", "done", branch="fix-08-h"),
               row("07", "working", branch="fix-07-g")]  # fmt: skip
    box.board("alpha", "b", tickets, {"retro-results.md": "# r\n"})
    listing = box.tmp / "sweep-list.tsv"
    listing.write_text(LIST.format(wt=box.work))
    box.env["FAKE_SWEEP_LIST"] = str(listing)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "sweep_board", {"board_key": KEY})

    data = run(scenario())["data"]

    removes = [c["argv"] for c in box.sweep_calls() if c["argv"][0] == "remove"]
    assert ["remove", "--branch", "fix-07-g"] not in removes
    assert ["remove", "--branch", "fix-04-d"] in removes
    skipped = {s["ref"]: s["reason"] for s in data["skipped"]}
    assert "fix-07-g" not in skipped
    assert skipped["fix-08-h"] == "unmerged (no-agent)"


def test_sweep_is_refused_before_a_retro_has_results(box, cert):
    box.board("alpha", "b", DONE)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "sweep_board", {"board_key": KEY})

    result = run(scenario())

    assert result["ok"] is False
    assert "retro" in result["reason"]
    assert box.sweep_calls() == []


def test_run_retro_hands_claude_only_tk_retros_stdout(box, cert):
    box.board("alpha", "b", DONE, {"retro-out": "RETRO board b"})
    box.env["FAKE_TK_STDERR"] = "WARN noise"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            await hub.next("snapshot")
            return await call(hub, "run_retro", {"board_key": KEY})

    run(scenario())

    [retro] = [c for c in box.synth_calls() if "--agent" in c["argv"]]
    assert "noise" not in retro["stdin"]
