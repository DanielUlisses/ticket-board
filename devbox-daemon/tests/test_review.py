"""Human review on the hub (board.md, Human code review): draft line comments in
hub-meta, Request changes as one `tk.sh say`, Approve tied to the diff's hash, and
open_pr / ssh_push refused under require_human_before_pr without a matching
approval. Through the daemon process, over a real git worktree and fake tk.sh."""

import json
import re

import pytest
from conftest import row
from test_get_diff import wt  # noqa: F401 — the fixture
from test_rpcs import KEY, call, calls
from test_tk_daemon import FakeHub, daemon, run

COMMENTS = [
    {"file": "mod.txt", "line": 2, "text": "Keep this lower case."},
    {"file": "added.txt", "line": 1, "text": "Why a new file?"},
]


def meta(board):
    return json.loads((board / "hub-meta.json").read_text())


def review_board(box, wt, col="human", policy=None, hub_meta=None):  # noqa: F811
    files = {}
    if policy is not None:
        files["policy.json"] = policy
    if hub_meta is not None:
        files["hub-meta.json"] = hub_meta
    return box.board(
        "alpha",
        "b",
        [row("01", col, worktree=str(wt.path), base=wt.base)],
        files=files,
    )


def rpcs(box, cert, steps, snapshot=True):
    """Each step: (action or None, method, params sans board_key/nn) against one
    daemon; the rpc_results and (when it changed the board) the snapshot after the
    last one."""

    async def scenario():
        out = []
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            for i, (action, method, params) in enumerate(steps):
                if action:
                    action()
                out.append(
                    await call(
                        hub, method, {"board_key": KEY, "nn": "01", **params}, f"r-{i}"
                    )
                )
            return out, (await hub.next("snapshot")) if snapshot else None

    return run(scenario())


def session(box, cert, body):
    """body(rpc) against one daemon, rpc(method, **params) for ticket 01."""

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            n = 0

            async def rpc(method, **params):
                nonlocal n
                n += 1
                params = {"board_key": KEY, "nn": "01", **params}
                return await call(hub, method, params, f"r-{n}")

            return await body(rpc)

    return run(scenario())


def edit(wt):  # noqa: F811
    (wt.path / "mod.txt").write_text("one\nTWO\nthree\n")


def test_draft_comments_are_stored_in_hub_meta_and_ride_the_snapshot(box, cert, wt):  # noqa: F811
    board = review_board(box, wt, hub_meta={"tickets": {"01": {"summary": "s"}}})

    [result], snap = rpcs(
        box, cert, [(None, "set_review_comments", {"comments": COMMENTS})]
    )

    assert result["ok"] is True
    assert meta(board)["tickets"]["01"] == {"summary": "s", "review_comments": COMMENTS}
    assert snap["boards"][0]["tickets"][0]["review_comments"] == COMMENTS


def test_request_changes_sends_the_draft_as_one_formatted_say_then_clears_it(
    box,
    cert,
    wt,  # noqa: F811
):
    board = review_board(
        box,
        wt,
        col="review",
        hub_meta={"tickets": {"01": {"review_comments": COMMENTS}}},
    )

    [result], snap = rpcs(box, cert, [(None, "request_changes", {})])

    assert (result["ok"], result["exit_code"]) == (True, 0)
    assert result["data"] == {"comments": 2}
    [say] = calls(box, "say")
    assert say["argv"][:3] == ["say", str(board), "01"]
    assert say["message"] == (
        "Changes requested in the hub review (2 comments):\n"
        "\n"
        "added.txt:1 — Why a new file?\n"
        "mod.txt:2 — Keep this lower case.\n"
    )
    assert meta(board)["tickets"]["01"]["review_comments"] == []
    assert snap["boards"][0]["tickets"][0]["review_comments"] == []


def test_request_changes_keeps_the_draft_when_the_agent_did_not_get_it(
    box,
    cert,
    wt,  # noqa: F811
):
    board = review_board(
        box,
        wt,
        col="review",
        hub_meta={"tickets": {"01": {"review_comments": COMMENTS}}},
    )
    (board / "say-exit").write_text("21")

    [result], _ = rpcs(box, cert, [(None, "request_changes", {})], snapshot=False)

    assert (result["ok"], result["exit_code"]) == (False, 21)
    assert result["reason"].startswith("no agent")
    assert meta(board)["tickets"]["01"]["review_comments"] == COMMENTS


def test_request_changes_with_no_draft_sends_nothing(box, cert, wt):  # noqa: F811
    review_board(box, wt, col="review")

    [result], _ = rpcs(box, cert, [(None, "request_changes", {})], snapshot=False)

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert result["reason"] == "no draft comments to send on #01"
    assert calls(box, "say") == []


def test_approve_records_the_current_diff_hash(box, cert, wt):  # noqa: F811
    board = review_board(box, wt)
    edit(wt)

    async def body(rpc):
        h = (await rpc("get_diff"))["data"]["diff_hash"]
        return h, await rpc("approve_review", diff_hash=h)

    h, result = session(box, cert, body)

    assert result["ok"] is True
    review = meta(board)["tickets"]["01"]["human_review"]
    assert (review["approved"], review["diff_hash"]) == (True, h)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", review["at"])


def test_approve_refuses_a_hash_the_diff_no_longer_has(box, cert, wt):  # noqa: F811
    board = review_board(box, wt)
    edit(wt)

    async def body(rpc):
        h = (await rpc("get_diff"))["data"]["diff_hash"]
        (wt.path / "mod.txt").write_text("changed again\n")  # the agent moved on
        return await rpc("approve_review", diff_hash=h)

    result = session(box, cert, body)

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert "changed since" in result["reason"]
    assert not (board / "hub-meta.json").exists()


GATED = {"require_human_before_pr": True}


@pytest.mark.parametrize(("method", "verb"), [("open_pr", "pr"), ("ssh_push", "push")])
def test_a_gated_ship_needs_an_approval_of_the_current_diff(
    box,
    cert,
    wt,  # noqa: F811
    method,
    verb,
):
    review_board(box, wt, policy=GATED)
    edit(wt)

    async def body(rpc):
        unapproved = await rpc(method)
        h = (await rpc("get_diff"))["data"]["diff_hash"]
        await rpc("approve_review", diff_hash=h)
        approved = await rpc(method)
        return unapproved, approved

    unapproved, approved = session(box, cert, body)

    assert (unapproved["ok"], unapproved["exit_code"]) == (False, None)
    assert unapproved["reason"] == (
        "require_human_before_pr: approve #01's diff in the hub review first"
    )
    assert (approved["ok"], approved["exit_code"]) == (True, 0)
    assert len(calls(box, verb)) == 1


def test_a_diff_change_after_approval_voids_it_for_shipping(box, cert, wt):  # noqa: F811
    review_board(box, wt, policy=GATED)
    edit(wt)

    async def body(rpc):
        h = (await rpc("get_diff"))["data"]["diff_hash"]
        await rpc("approve_review", diff_hash=h)
        (wt.path / "fresh.txt").write_text("new\n")  # the agent kept going
        return await rpc("open_pr")

    result = session(box, cert, body)

    assert (result["ok"], result["exit_code"]) == (False, None)
    assert result["reason"] == (
        "require_human_before_pr: #01's diff changed since its approval;"
        " review it again"
    )
    assert calls(box, "pr") == []


def test_without_the_flag_review_is_optional(box, cert, wt):  # noqa: F811
    review_board(box, wt, policy={"require_human_before_pr": False})
    edit(wt)

    result = session(box, cert, lambda rpc: rpc("open_pr"))

    assert (result["ok"], result["exit_code"]) == (True, 0)
    assert len(calls(box, "pr")) == 1
