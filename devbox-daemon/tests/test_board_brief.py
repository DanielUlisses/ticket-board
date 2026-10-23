"""The board brief in the running daemon (fake hub, fake tk.sh, fake agy): refreshed,
debounced, when any ticket of the board changes column, and on refresh_board_brief; a
failed run keeps the previous brief; board.md is never written."""

import asyncio

from test_synthesis import KEY, meta, move, setup_board
from test_tk_daemon import FakeHub, daemon, run

BRIEF = {"landed": ["Export landed (agy)"], "in_flight": ["02 is in review"],
         "needs_you": ["01 waits for you"], "next": ["Launch 03"]}  # fmt: skip


def brief_calls(box):
    return [c for c in box.synth_calls() if "BOARD BRIEF" in c["argv"][1]]


def test_column_changes_refresh_the_brief_once_after_the_debounce(box, cert):
    d = setup_board(box, meta={"archived": False})
    box.write("work/alpha/.scratch/b/board.md", "# b\n\nWhy this board exists.\n")

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=0.2, brief_debounce_s=1.0),
        ):
            await hub.next("snapshot")
            assert not brief_calls(box)  # first sight is not a change
            for col in ("working", "review"):  # not columns that synthesize a ticket
                move(d, col)
                await hub.next("snapshot")
            assert not brief_calls(box)  # still inside the debounce
            started = await hub.next("synthesis_started", timeout=10)
            finished = await hub.next("synthesis_finished")
            await asyncio.sleep(1.5)
            return started, finished

    started, finished = run(scenario())

    assert (started["board_key"], started["nn"], started["backend"]) == (
        KEY,
        None,
        "agy",
    )
    assert (finished["nn"], finished["backend"]) == (None, "agy")
    assert len(brief_calls(box)) == 1
    prompt = brief_calls(box)[0]["argv"][1]
    assert "Why this board exists." in prompt and "01" in prompt
    brief = meta(d)["board_brief"]
    assert brief | {"updated_at": None} == BRIEF | {
        "synthesis_backend": "agy",
        "synthesis_status": "ok",
        "synthesis_error": None,
        "updated_at": None,
    }
    assert brief["updated_at"]
    assert (d / "board.md").read_text() == "# b\n\nWhy this board exists.\n"


def test_refresh_board_brief_runs_now_and_replies(box, cert):
    d = setup_board(box, col="working")

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send({"type": "rpc", "id": "r-1", "method": "refresh_board_brief",
                            "params": {"board_key": KEY}})  # fmt: skip
            result = await hub.next("rpc_result")
            return result, await hub.next("snapshot")

    result, snap = run(scenario())

    assert (result["id"], result["ok"]) == ("r-1", True) and "agy" in result[
        "output_tail"
    ]
    assert meta(d)["board_brief"]["landed"] == BRIEF["landed"]
    assert snap["boards"][0]["hub_meta"]["board_brief"]["synthesis_status"] == "ok"


def test_a_failed_brief_keeps_the_previous_one_and_says_it_failed(box, cert):
    old = {**BRIEF, "synthesis_backend": "cursor", "synthesis_status": "ok",
           "updated_at": "2026-01-01T00:00:00Z"}  # fmt: skip
    d = setup_board(box, col="working", meta={"archived": False, "board_brief": old})
    box.env["FAKE_SYNTH_MODE_AGY"] = "garbage"
    box.env["FAKE_SYNTH_MODE_CLAUDE"] = "hang"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send({"type": "rpc", "id": "r-1", "method": "refresh_board_brief",
                            "params": {"board_key": KEY}})  # fmt: skip
            failed = await hub.next("synthesis_failed", timeout=20)
            return failed, await hub.next("rpc_result")

    failed, result = run(scenario())

    assert failed["nn"] is None and "agy: invalid output" in failed["error"]
    assert result["ok"] is False
    assert meta(d)["board_brief"] == old | {"synthesis_status": "failed",
                                           "synthesis_error": failed["error"]}  # fmt: skip


def test_a_move_during_a_running_brief_gets_a_follow_up_refresh(box, cert):
    d = setup_board(box, meta={"archived": False})
    box.env["FAKE_SYNTH_MODE_AGY"] = "slow"
    box.env["FAKE_SYNTH_DELAY"] = "2"

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=0.2, brief_debounce_s=0.3),
        ):
            await hub.next("snapshot")
            move(d, "working")
            await hub.next("synthesis_started", timeout=10)
            move(d, "review")  # its debounce fires while the first run is in flight
            await hub.next("synthesis_finished", timeout=10)
            await hub.next("synthesis_started", timeout=10)
            await hub.next("synthesis_finished", timeout=10)

    run(scenario())

    assert len(brief_calls(box)) == 2
    assert meta(d)["board_brief"]["synthesis_status"] == "ok"


def test_refresh_board_brief_while_running_is_refused(box, cert):
    setup_board(box, col="working", meta={"archived": False})
    box.env["FAKE_SYNTH_MODE_AGY"] = "slow"
    box.env["FAKE_SYNTH_DELAY"] = "2"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            rpc = {"type": "rpc", "method": "refresh_board_brief",
                   "params": {"board_key": KEY}}  # fmt: skip
            await hub.send(rpc | {"id": "r-1"})
            await hub.next("synthesis_started", timeout=10)
            await hub.send(rpc | {"id": "r-2"})
            refused = await hub.next("rpc_result")
            first = await hub.next("rpc_result", timeout=10)
            return refused, first

    refused, first = run(scenario())

    assert (refused["id"], refused["ok"]) == ("r-2", False)
    assert "already running" in refused["reason"]
    assert (first["id"], first["ok"]) == ("r-1", True)


def test_a_brief_left_running_by_a_killed_daemon_is_marked_interrupted(box, cert):
    stale = {**BRIEF, "synthesis_backend": "agy", "synthesis_status": "running",
             "updated_at": None}  # fmt: skip
    d = setup_board(box, col="working", meta={"archived": False, "board_brief": stale})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            return await hub.next("snapshot")

    snap = run(scenario())

    expected = stale | {"synthesis_status": "failed", "synthesis_error": "interrupted"}
    assert meta(d)["board_brief"] == expected
    assert snap["boards"][0]["hub_meta"]["board_brief"] == expected
    assert brief_calls(box) == []


def test_a_running_brief_clears_the_previous_error(box, cert):
    old = {**BRIEF, "synthesis_backend": "agy", "synthesis_status": "failed",
           "synthesis_error": "boom", "updated_at": None}  # fmt: skip
    d = setup_board(box, col="working", meta={"archived": False, "board_brief": old})
    box.env["FAKE_SYNTH_MODE_AGY"] = "slow"
    box.env["FAKE_SYNTH_DELAY"] = "2"
    seen = []

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send({"type": "rpc", "id": "r-1", "method": "refresh_board_brief",
                            "params": {"board_key": KEY}})  # fmt: skip
            await hub.next("synthesis_started", timeout=10)
            seen.append(meta(d)["board_brief"])
            await hub.next("rpc_result", timeout=10)

    run(scenario())

    assert seen[0]["synthesis_status"] == "running"
    assert seen[0].get("synthesis_error") is None
