"""Synthesis in the running daemon, against the fake hub of test_tk_daemon.py with fake
`tk.sh` and fake `agy` / `cursor-agent` / `claude`: runs on a ticket's move into
needs / human / pr and on refine_with_comment, lands in hub-meta.json (previous in
hub-meta.prev.json), reports synthesis_* events; set_task_done writes a tick."""

import asyncio
import importlib.util
import json
import logging
from pathlib import Path

import pytest
from conftest import row
from test_tk_daemon import DAEMON, FakeHub, daemon, run

KEY = "dev-test/alpha/b"


def setup_board(box, col="backlog", meta=None, branch="feat-01-export"):
    ticket = box.write(
        "work/alpha/.scratch/b/01-export.md", "# 01: Export\n\nDo the export.\n"
    )
    files = {"hub-meta.json": meta} if meta is not None else {}
    d = box.board("alpha", "b", [row("01", col, file=str(ticket), branch=branch,
                                     title="Export")], files)  # fmt: skip
    box.write(".config/tk-daemon/policy.json",
              {"synthesis": {"backends": ["agy", "claude"], "timeout": 2}})  # fmt: skip
    return d


def move(board, col):
    rows = json.loads((board / "rows.json").read_text())
    rows[0]["col"] = col
    (board / "rows.json").write_text(json.dumps(rows))


def meta(board, name="hub-meta.json"):
    return json.loads((board / name).read_text())


def rpc(id, method, **params):
    return {
        "type": "rpc",
        "id": id,
        "method": method,
        "params": {"board_key": KEY, **params},
    }


def test_a_move_into_needs_synthesizes_into_hub_meta_and_reports_events(box, cert):
    d = setup_board(box, meta={"archived": False, "summary_note": "kept"})
    box.write("work/alpha/docs/agents/project-memory.md", "1. Keep stdout pure JSON.\n")
    box.write(".local/state/ticket-skill/alpha/reports/feat-01-export.md",
              "# Coordinator report\n\nAll criteria met.\n")  # fmt: skip

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.3):
            await hub.next("snapshot")
            move(d, "needs")
            started = await hub.next("synthesis_started")
            finished = await hub.next("synthesis_finished")
            while True:
                snap = await hub.next("snapshot")
                t = snap["boards"][0]["tickets"][0] if snap["boards"] else {}
                if t.get("synthesis_status") == "ok":
                    return started, finished, t

    started, finished, ticket = run(scenario())

    assert (started["board_key"], started["nn"], started["backend"]) == (
        KEY,
        "01",
        "agy",
    )
    assert (finished["nn"], finished["backend"]) == ("01", "agy")
    assert ticket["summary"] == "Written by agy.\nSecond line."
    assert ticket["synthesis_backend"] == "agy"
    assert ticket["full_report_md"].startswith(
        "# Coordinator report\n\nAll criteria met.\n"
    )
    assert [t["text"] for t in ticket["manual_tasks"]] == [
        "Open the sheet on a phone",
        "Tick a box",
    ]
    m = meta(d)
    assert (
        m["summary_note"] == "kept" and m["tickets"]["01"]["synthesis_status"] == "ok"
    )
    assert meta(d, "hub-meta.prev.json") == {"archived": False, "summary_note": "kept"}
    [call] = box.synth_calls()
    assert call["cwd"] == str(box.work / "alpha")
    prompt = call["argv"][1]
    for part in ("Do the export.", "All criteria met.", "tk show 01: the agent pane tail",
                 "Keep stdout pure JSON."):  # fmt: skip
        assert part in prompt
    show = [c for c in box.tk_calls() if c["argv"][0] == "show"]
    assert [c["argv"] for c in show] == [["show", str(d), "01", "180"]]
    assert show[0]["cwd"] == str(box.work / "alpha")


def test_only_moves_into_needs_human_pr_trigger_after_the_catch_up(box, cert):
    d = setup_board(box, col="human")  # no synthesis yet: the catch-up runs it once

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.2):
            await hub.next("snapshot")
            await hub.next("synthesis_finished")
            for col in ("working", "review", "done", "backlog"):
                move(d, col)
                await hub.next("snapshot")
            calls = len(box.synth_calls())
            move(d, "pr")
            await hub.next("synthesis_finished")
            return calls

    assert run(scenario()) == 1
    assert len(box.synth_calls()) == 2


def rows_board(box, cols, meta=None):
    rows = []
    for i, col in enumerate(cols, 1):
        f = box.write(f"work/alpha/.scratch/b/{i:02d}-t.md", f"# {i:02d}: T\n")
        rows.append(row(f"{i:02d}", col, file=str(f), title=f"T{i}"))
    box.write(".config/tk-daemon/policy.json",
              {"synthesis": {"backends": ["agy"], "timeout": 2}})  # fmt: skip
    return box.board("alpha", "b", rows, {"hub-meta.json": meta} if meta else {})


def test_catch_up_skips_done_failed_ok_and_a_first_seen_backlog_ticket(box, cert):
    tickets = {"02": {"synthesis_status": "ok"}, "03": {"synthesis_status": "failed"}}
    d = rows_board(box, ["backlog", "human", "pr", "done", "needs"],
                   {"tickets": tickets})  # fmt: skip
    # 01 backlog (first sight), 02 ok, 03 failed, 04 done, 05 needs and none: only 05

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.2):
            await hub.next("snapshot")
            started = await hub.next("synthesis_started")
            await hub.next("synthesis_finished")
            await asyncio.sleep(1)  # more exports: nothing is retried
            return started

    assert run(scenario())["nn"] == "05"
    assert len(box.synth_calls()) == 1
    t = meta(d)["tickets"]
    assert t["03"]["synthesis_status"] == "failed" and "01" not in t


def test_a_ticket_new_on_the_board_is_synthesized_once(box, cert):
    d = rows_board(box, ["done"])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.2):
            await hub.next("snapshot")
            f = box.write("work/alpha/.scratch/b/02-t.md", "# 02: T\n")
            rows = json.loads((d / "rows.json").read_text())
            rows.append(row("02", "backlog", file=str(f), title="T2"))
            (d / "rows.json").write_text(json.dumps(rows))
            started = await hub.next("synthesis_started")
            await hub.next("synthesis_finished")
            await asyncio.sleep(1)  # more exports: not run again
            return started

    assert run(scenario())["nn"] == "02"
    assert len(box.synth_calls()) == 1


def test_a_ticket_that_lands_in_done_is_not_synthesized(box, cert):
    d = rows_board(box, ["done"])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.2):
            await hub.next("snapshot")
            f = box.write("work/alpha/.scratch/b/02-t.md", "# 02: T\n")
            rows = json.loads((d / "rows.json").read_text())
            rows.append(row("02", "done", file=str(f), title="T2"))
            (d / "rows.json").write_text(json.dumps(rows))
            await hub.next("snapshot")
            await asyncio.sleep(1)

    run(scenario())
    assert box.synth_calls() == []


def test_catch_ups_run_one_after_the_other_and_a_queued_one_rechecks(box, cert):
    d = rows_board(box, ["human", "human", "human"])
    box.env.update(FAKE_SYNTH_MODE_AGY="slow", FAKE_SYNTH_DELAY="0.6")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.2):
            await hub.next("snapshot")
            seen = [(await hub.next("synthesis_started"))["nn"]]
            rows = json.loads((d / "rows.json").read_text())
            rows[2]["col"] = "done"  # leaves while it waits in the queue
            (d / "rows.json").write_text(json.dumps(rows))
            while len(seen) < 4:
                m = await hub.next()
                if m["type"] in ("synthesis_started", "synthesis_finished"):
                    seen.append(m["type"])
            await asyncio.sleep(1.5)
            return seen

    seen = run(scenario())
    assert seen[1:] == ["synthesis_finished", "synthesis_started", "synthesis_finished"]
    assert len(box.synth_calls()) == 2
    assert "03" not in meta(d)["tickets"]


def test_a_queued_catch_up_is_dropped_when_a_click_synthesized_it_meanwhile(box, cert):
    # unit-level: still_wanted reads hub-meta now, not the export
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("tkd", DAEMON)
    tkd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tkd)
    d = rows_board(box, ["human"])
    t = {"nn": "01", "col": "human"}
    me = SimpleNamespace(sent={KEY: {"tickets": [t], "hub_meta": {},
                                     "board_path": str(d)}})  # fmt: skip
    assert tkd.Daemon.still_wanted(me, KEY, "01", True)
    (d / "hub-meta.json").write_text(json.dumps(
        {"tickets": {"01": {"synthesis_status": "ok"}}}))  # fmt: skip
    assert not tkd.Daemon.still_wanted(me, KEY, "01", True)
    assert tkd.Daemon.still_wanted(me, KEY, "01", False)  # a move still runs
    t["col"] = "done"
    assert not tkd.Daemon.still_wanted(me, KEY, "01", False)


def test_refine_with_comment_passes_the_comment_and_replies_when_done(box, cert):
    d = setup_board(box, col="working")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="01",
                               comment="Mention the total cap."))  # fmt: skip
            started = await hub.next("synthesis_started")
            result = await hub.next("rpc_result")
            snap = await hub.next("snapshot")
            return started, result, snap

    started, result, snap = run(scenario())

    assert started["nn"] == "01"
    assert (result["id"], result["ok"], result["exit_code"]) == ("r-1", True, 0)
    assert "agy" in result["output_tail"]
    assert "Mention the total cap." in box.synth_calls()[0]["argv"][1]
    assert snap["boards"][0]["tickets"][0]["synthesis_status"] == "ok"
    assert meta(d)["tickets"]["01"]["summary"] == "Written by agy.\nSecond line."


def test_a_failed_run_keeps_the_last_good_meta_and_marks_it_failed(box, cert):
    good = {"summary": "Old summary.", "reviewer_notes": "Old note.", "full_report_md": "Old",
            "manual_tasks": [{"text": "Old task", "done": True, "source": "synthesis"}],
            "synthesis_backend": "cursor", "synthesis_status": "ok"}  # fmt: skip
    d = setup_board(box, col="working", meta={"tickets": {"01": good}})
    box.env["FAKE_SYNTH_MODE_AGY"] = "hang"
    box.env["FAKE_SYNTH_MODE_CLAUDE"] = "garbage"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="01", comment=""))
            failed = await hub.next("synthesis_failed", timeout=20)
            result = await hub.next("rpc_result")
            return failed, result, await hub.next("snapshot")

    failed, result, snap = run(scenario())

    assert (failed["nn"], failed["backend"]) == ("01", "claude")
    assert (
        "agy: timed out after 2s" in failed["error"]
        and "claude: invalid output" in failed["error"]
    )
    assert result["ok"] is False and "agy: timed out" in result["reason"]
    entry = meta(d)["tickets"]["01"]
    assert entry == good | {
        "synthesis_status": "failed",
        "synthesis_error": failed["error"],
    }
    assert not (d / "hub-meta.prev.json").exists()
    assert snap["boards"][0]["tickets"][0]["summary"] == "Old summary."
    assert [c["bin"] for c in box.synth_calls()] == ["agy", "claude"]


def test_a_second_run_merges_the_checklist_keeping_ticks_and_developer_items(box, cert):
    current = [{"text": "Tick a box.", "done": True, "source": "synthesis"},
               {"text": "Dropped, unchecked", "done": False, "source": "synthesis"},
               {"text": "Dropped, checked", "done": True, "source": "synthesis"},
               {"text": "Mine", "done": False, "source": "developer"}]  # fmt: skip
    d = setup_board(
        box, col="working", meta={"tickets": {"01": {"manual_tasks": current}}}
    )

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="01", comment=""))
            return await hub.next("rpc_result")

    assert run(scenario())["ok"] is True
    assert meta(d)["tickets"]["01"]["manual_tasks"] == [
        {"text": "Tick a box", "done": True, "source": "synthesis"},
        {"text": "Dropped, checked", "done": True, "source": "synthesis"},
        {"text": "Mine", "done": False, "source": "developer"},
        {"text": "Open the sheet on a phone", "done": False, "source": "synthesis"},
    ]


def test_set_task_done_writes_the_tick_to_hub_meta(box, cert):
    tasks = [{"text": "Open on a phone", "done": False, "source": "synthesis"}]
    d = setup_board(
        box, meta={"archived": False, "tickets": {"01": {"manual_tasks": tasks}}}
    )

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(
                rpc("r-1", "set_task_done", nn="01", text="open on a phone", done=True)
            )
            ok = await hub.next("rpc_result")
            snap = await hub.next("snapshot")
            await hub.send(rpc("r-2", "set_task_done", nn="01", text="Nope", done=True))
            return ok, snap, await hub.next("rpc_result")

    ok, snap, unknown = run(scenario())

    assert (ok["ok"], ok["exit_code"]) == (True, 0)
    assert snap["boards"][0]["tickets"][0]["manual_tasks"][0]["done"] is True
    assert meta(d) == {"archived": False, "tickets": {"01": {"manual_tasks": [
        {"text": "Open on a phone", "done": True, "source": "synthesis"}]}}}  # fmt: skip
    assert unknown["ok"] is False and "Nope" in unknown["reason"]
    assert box.synth_calls() == []


def test_refine_refuses_an_unknown_ticket_without_running_a_backend(box, cert):
    setup_board(box)

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="99", comment=""))
            return await hub.next("rpc_result")

    result = run(scenario())

    assert result["ok"] is False and "99" in result["reason"]
    assert box.synth_calls() == []


def test_a_run_left_running_by_a_killed_daemon_is_marked_interrupted(box, cert):
    stale = {"summary": "Old summary.", "synthesis_status": "running"}
    d = setup_board(box, col="working", meta={"tickets": {"01": stale}})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            return await hub.next("snapshot")

    snap = run(scenario())

    ticket = snap["boards"][0]["tickets"][0]
    assert (ticket["synthesis_status"], ticket["synthesis_error"]) == (
        "failed",
        "interrupted",
    )
    assert meta(d)["tickets"]["01"] == stale | {
        "synthesis_status": "failed",
        "synthesis_error": "interrupted",
    }
    assert box.synth_calls() == []


def test_a_run_cut_by_the_rpc_timeout_reports_synthesis_failed(box, cert):
    d = setup_board(box, col="working")
    box.env["FAKE_SYNTH_MODE_AGY"] = "hang"

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, rpc_synthesis_timeout_s=1),
        ):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="01", comment=""))
            failed = await hub.next("synthesis_failed", timeout=10)
            return failed, await hub.next("rpc_result")

    failed, result = run(scenario())

    assert (failed["nn"], failed["error"]) == ("01", "interrupted")
    assert result["ok"] is False
    assert meta(d)["tickets"]["01"]["synthesis_status"] == "failed"


def test_tk_agy_timeout_overrides_the_policy_timeout(box, cert):
    setup_board(box, col="working")  # policy synthesis.timeout: 2
    box.env["TK_AGY_TIMEOUT"] = "0.5"
    box.env["FAKE_SYNTH_MODE_AGY"] = "hang"
    box.env["FAKE_SYNTH_MODE_CLAUDE"] = "hang"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="01", comment=""))
            return await hub.next("synthesis_failed", timeout=20)

    failed = run(scenario())

    assert "agy: timed out after 0.5s" in failed["error"]
    assert "claude: timed out after 0.5s" in failed["error"]


def test_the_coordinator_report_is_read_under_xdg_state_home(box, cert):
    setup_board(box, col="working")
    state = box.tmp / "state"
    box.env["XDG_STATE_HOME"] = str(state)
    report = state / "ticket-skill/alpha/reports/feat-01-export.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Coordinator report\n\nFrom XDG_STATE_HOME.\n")
    box.write(".local/state/ticket-skill/alpha/reports/feat-01-export.md",
              "# Coordinator report\n\nFrom the default.\n")  # fmt: skip

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(rpc("r-1", "refine_with_comment", nn="01", comment=""))
            return await hub.next("rpc_result")

    assert run(scenario())["ok"] is True
    prompt = box.synth_calls()[0]["argv"][1]
    assert "From XDG_STATE_HOME." in prompt
    assert "From the default." not in prompt


# ---- in process: boards the export would never hand over (they fail the schema) ----
class Local:
    """tk-daemon.py loaded in this process, its Daemon not connected to any hub."""

    def __init__(self, box, board: Path):
        spec = importlib.util.spec_from_file_location("tk_daemon", DAEMON)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.daemon = self.mod.Daemon(
            {**self.mod.DEFAULTS, "url": "wss://hub.test/ws/daemon"}
        )
        self.board_path = board
        self.root = box.work / "alpha"

    def board(self, policy) -> dict:
        return {"key": KEY, "repo": "alpha", "root": str(self.root),
                "board_path": str(self.board_path), "policy": policy}  # fmt: skip

    def ticket(self) -> dict:
        rows = json.loads((self.board_path / "rows.json").read_text())
        return {"nn": "01", "title": "Export", "file": rows[0]["file"],
                "branch": "feat-01-export"}  # fmt: skip

    def synthesize(self, policy):
        return asyncio.run(
            self.daemon.synthesize(self.board(policy), self.ticket(), "")
        )

    def context(self, policy):
        return asyncio.run(
            self.daemon.synthesis_context(self.board(policy), self.ticket(), "")
        )


@pytest.fixture
def local(box, monkeypatch):
    def make(**env) -> Local:
        for k, v in {**box.env, **env}.items():
            monkeypatch.setenv(k, v)
        for k in ("XDG_STATE_HOME", "TK_AGY_TIMEOUT"):
            if k not in env:
                monkeypatch.delenv(k, raising=False)
        return Local(box, setup_board(box, col="working"))

    return make


@pytest.mark.parametrize(
    "policy",
    [{"synthesis": None}, {"synthesis": "agy"}, None, ["synthesis"],
     {"synthesis": {"backends": None, "timeout": None, "commands": None}}],
)  # fmt: skip
def test_a_null_or_malformed_synthesis_policy_runs_the_default_backend(local, policy):
    loc = local()

    result = loc.synthesize(policy)

    assert result.backend == "agy"
    entry = meta(loc.board_path)["tickets"]["01"]
    assert (entry["synthesis_status"], entry["synthesis_backend"]) == ("ok", "agy")


def test_a_null_synthesis_policy_whose_run_fails_ends_failed_not_running(local):
    loc = local(FAKE_SYNTH_MODE="garbage")

    with pytest.raises(loc.mod.agy_runner.SynthesisFailed):
        loc.synthesize({"synthesis": None})

    entry = meta(loc.board_path)["tickets"]["01"]
    assert (entry["synthesis_status"], entry["synthesis_error"]) == (
        "failed",
        "agy: invalid output",
    )


def timeout_warnings(caplog, name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and name in r.getMessage()]  # fmt: skip


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "soon", " "])
def test_an_invalid_tk_agy_timeout_is_ignored_with_one_warning(local, caplog, value):
    loc = local(TK_AGY_TIMEOUT=value)
    policy = {"synthesis": {"timeout": 2}}

    timeouts = [loc.context(policy).timeout, loc.context(policy).timeout]

    assert timeouts == [2, 2]
    [warning] = timeout_warnings(caplog, "TK_AGY_TIMEOUT")
    assert repr(value) in warning


def test_a_valid_tk_agy_timeout_wins_over_the_policy(local, caplog):
    loc = local(TK_AGY_TIMEOUT="0.5")

    assert loc.context({"synthesis": {"timeout": 2}}).timeout == 0.5
    assert timeout_warnings(caplog, "timeout") == []


@pytest.mark.parametrize("value", [0, -5, "soon", True, [300]])
def test_an_invalid_policy_timeout_is_the_default_with_a_warning(local, caplog, value):
    loc = local()

    assert loc.context({"synthesis": {"timeout": value}}).timeout == 300
    [warning] = timeout_warnings(caplog, "synthesis.timeout")
    assert repr(value) in warning and KEY in warning


@pytest.mark.parametrize(
    "policy", [{"synthesis": {}}, {"synthesis": {"timeout": None}}]
)
def test_no_policy_timeout_is_the_default_without_a_warning(local, caplog, policy):
    loc = local()

    assert loc.context(policy).timeout == 300
    assert timeout_warnings(caplog, "timeout") == []
