"""The scheduler's state across passes, driven on Hub with fake sockets and a
fake clock: what an automatic RPC holds while its outcome is unknown, what a
failure frees, what the log repeats, and when ▶ Run waves really stops."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest
from conftest import board, fixture, snapshot, ticket

from hub_server.hub import Hub
from hub_server.scheduler import Scheduler

KEY = "dev-daniel/ticket-board/tk-hub"
START = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


class FakeSocket:
    """Records what the hub sends; `on_send` runs once, mid-send; `broken`
    raises like a peer that has gone."""

    def __init__(self, on_send=None, broken=False):
        self.sent = []
        self.on_send = on_send
        self.broken = broken

    async def send_json(self, message):
        if self.broken:
            raise OSError("connection reset")
        self.sent.append(message)
        if self.on_send:
            hook, self.on_send = self.on_send, None
            await hook(message)

    async def close(self, code=1000):
        pass


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


def frontier(nn, col="backlog", **extra):
    t = copy.deepcopy(ticket(board(KEY), "27"))  # sonnet/low, on the frontier
    return t | {"nn": nn, "col": col, **extra}


def fleet(*tickets, running=4, **policy):
    """dev-daniel's snapshot with only ticket-board, holding `tickets`."""
    b = board(KEY)
    b["tickets"] = list(tickets)
    b["policy"] = b["policy"] | policy
    message = snapshot(boards=[b])
    message["health"]["running"] = running
    return message


def rpcs(ws, method="launch_ticket"):
    return [m for m in ws.sent if m.get("type") == "rpc" and m["method"] == method]


def nns(ws, method="launch_ticket"):
    return [m["params"]["nn"] for m in rpcs(ws, method)]


def entries(hub, action=None):
    """The scheduler log, oldest first."""
    rows = reversed(hub.store.decisions())
    return [(e["action"], e["nn"], e["reason"]) for e in rows
            if action in (None, e["action"])]  # fmt: skip


async def start(tmp_path, ws=None, clock=None):
    hub = Hub(tmp_path / "hub.db")
    scheduler = Scheduler(hub, clock=clock or Clock())
    hub.on_change = lambda: None  # the tests run every pass by hand
    ws = ws or FakeSocket()
    await hub.connected("dev-daniel", ws, fixture("hello.json"))
    return hub, scheduler, ws


async def confirmed(scheduler, clock):
    """Two passes an export cycle apart: a PR still "ready to merge" on the
    second is merged."""
    await scheduler.run_once()
    clock.now += timedelta(seconds=30)
    await scheduler.run_once()


async def reply(hub, ws, rpc, **result):
    message = {"type": "rpc_result", "id": rpc["id"], "output_tail": ""} | result
    await hub.received("dev-daniel", ws, message)


# -- automatic RPCs whose outcome is unknown, or known --------------------------


def test_a_launch_whose_rpc_timed_out_keeps_its_slot_until_the_board_shows_it(
    tmp_path,
):
    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), frontier("28")))
        await scheduler.run_once()  # 4 running, cap 5: 27 goes, 28 waits
        # The hub gave up after 120 s; the daemon's launch may still be running.
        await reply(hub, ws, rpcs(ws)[0], ok=False, exit_code=None,
                    reason="timed out after 120s")  # fmt: skip
        await scheduler.run_once()
        held = nns(ws)
        clock.now += timedelta(minutes=10)  # no snapshot ever showed it
        await scheduler.run_once()
        return hub, held, nns(ws)

    hub, held, later = asyncio.run(scenario())

    assert held == ["27"]  # 28 still waits on the cap, 27 is not relaunched
    assert later == ["27", "27"]  # released after the TTL and the cooldown
    [failed] = entries(hub, "failed")
    assert failed == ("failed", "27", "outcome unknown: timed out after 120s")


@pytest.mark.parametrize(
    ("result", "logged"),
    [
        ({"ok": False, "exit_code": None, "reason": "herdr is down"}, "herdr is down"),
        ({"ok": False, "exit_code": 1, "output_tail": "no such ticket\n"},
         "no such ticket"),
    ],
)  # fmt: skip
def test_a_refused_or_failed_launch_frees_its_slot_and_cools_down(
    tmp_path, result, logged
):
    async def scenario():
        hub, scheduler, ws = await start(tmp_path)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), frontier("28")))
        await scheduler.run_once()
        await reply(hub, ws, rpcs(ws)[0], **result)
        await asyncio.sleep(0)  # the failure is logged from a task
        await scheduler.run_once()
        return hub, nns(ws)

    hub, launched = asyncio.run(scenario())

    assert launched == ["27", "28"]
    assert ("failed", "27", logged) in entries(hub)
    assert entries(hub)[-1][2].startswith("the last launch_ticket failed")


def test_a_launch_cut_off_by_a_reconnect_keeps_its_slot(tmp_path):
    async def scenario():
        hub, scheduler, old = await start(tmp_path)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), frontier("28")))
        await scheduler.run_once()
        new = FakeSocket()
        await hub.connected("dev-daniel", new, fixture("hello.json"))
        await scheduler.run_once()
        return nns(old), nns(new)

    before, after = asyncio.run(scenario())

    assert (before, after) == (["27"], [])


def test_a_launch_that_never_left_the_hub_is_logged_failed_not_sent(tmp_path):
    async def scenario():
        hub, scheduler, _ = await start(tmp_path, ws=FakeSocket(broken=True))
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27")))
        await scheduler.run_once()
        return hub, scheduler

    hub, scheduler = asyncio.run(scenario())

    # The send broke midway: whether the daemon got it is unknown, so 27 keeps
    # its slot like any launch cut off by a disconnect.
    assert entries(hub) == [("failed", "27", "outcome unknown: devbox disconnected")]
    assert [i.nn for i in scheduler.inflight.values()] == ["27"]


def test_a_pushed_ticket_is_not_pushed_again_while_it_stays_in_human(tmp_path):
    passed = {"tests": "pass", "criteria": "2/2", "review": "ok"}
    tickets = (frontier("01", col="human", checks=passed),
               frontier("02", col="blocked"))  # fmt: skip
    policy = {"forge": "none", "night_shift": True, "require_human_before_pr": False}

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", fleet(*tickets, **policy))
        await scheduler.run_once()
        await reply(hub, ws, rpcs(ws, "ssh_push")[0], ok=True, exit_code=0)
        clock.now += timedelta(minutes=30)
        await hub.apply_snapshot("dev-daniel", fleet(*tickets, **policy))
        await scheduler.run_once()
        return nns(ws, "ssh_push")

    assert asyncio.run(scenario()) == ["01"]


PASSED = {"tests": "pass", "criteria": "2/2", "review": "ok"}
SHIPS = {"night_shift": True, "require_human_before_pr": False}


def test_a_pr_that_fails_twice_is_left_to_the_developer(tmp_path):
    def snap(col):  # 09 keeps waves on throughout
        return fleet(frontier("01", col=col, checks=PASSED),
                     frontier("09", col="working"), **SHIPS)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", snap("human"))

        async def attempt(**result):
            clock.now += timedelta(minutes=11)
            await scheduler.run_once()
            await reply(hub, ws, rpcs(ws, "open_pr")[-1], **result)
            await asyncio.sleep(0)

        await attempt(ok=False, exit_code=1, output_tail="gh: rate limited\n")
        await attempt(ok=False, exit_code=None, reason="timed out after 120s")
        await attempt(ok=False, exit_code=1, output_tail="gh: rate limited\n")
        clock.now += timedelta(minutes=11)
        await scheduler.run_once()
        given_up = nns(ws, "open_pr")
        # The developer takes it back to work: the count starts again.
        await hub.apply_snapshot("dev-daniel", snap("working"))
        await scheduler.run_once()
        await hub.apply_snapshot("dev-daniel", snap("human"))
        await scheduler.run_once()
        return hub, given_up, nns(ws, "open_pr")

    hub, given_up, again = asyncio.run(scenario())

    assert given_up == ["01", "01", "01"]  # the timeout is not an attempt
    assert ("skipped", "01", "PR failed twice — waiting for developer") in entries(hub)
    assert again == ["01", "01", "01", "01"]


def test_a_failed_merge_is_not_retried(tmp_path):
    ready = fleet(frontier("01", col="pr", note="#1 ready to merge"),
                  frontier("09", col="working"), auto_merge=True, **SHIPS)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", ready)
        await confirmed(scheduler, clock)
        await reply(hub, ws, rpcs(ws, "merge_pr")[0], ok=False, exit_code=12,
                    reason="gate failed: the PR is not ready to merge")  # fmt: skip
        await asyncio.sleep(0)
        clock.now += timedelta(hours=1)
        await scheduler.run_once()
        return hub, nns(ws, "merge_pr")

    hub, merges = asyncio.run(scenario())

    assert merges == ["01"]
    assert entries(hub)[-1] == ("skipped", "01", "merge failed — waiting for developer")


# -- the log -------------------------------------------------------------------


def test_a_skip_is_logged_when_its_cause_changes_not_its_numbers(tmp_path):
    over = fixture("hello.json")["health"]["usage"] | {
        "five_hour": {"used_percentage": 95, "resets_at": "2026-10-24T01:00:00Z"}
    }

    async def scenario():
        hub, scheduler, _ = await start(tmp_path)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=5))
        await scheduler.run_once()
        await scheduler.run_once()
        # A manual launch past the cap: 6 running, still the same cause.
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=6))
        await scheduler.run_once()
        gated = fleet(frontier("27"), running=6)
        gated["health"]["usage"] = over
        await hub.apply_snapshot("dev-daniel", gated)
        await scheduler.run_once()
        await scheduler.run_once()
        return hub

    hub = asyncio.run(scenario())

    gated = "dev-daniel's 5-hour usage is 95% (gate 90%) until 2026-10-24T01:00:00Z"
    assert entries(hub) == [
        ("skipped", "27", "dev-daniel is at max_parallel (5 running, cap 5)"),
        ("skipped", "27", gated),
    ]


def test_an_assist_board_is_logged_once_not_per_ticket(tmp_path):
    async def scenario():
        hub, scheduler, _ = await start(tmp_path)
        assist = fleet(frontier("27"), frontier("28"), autonomy="assist")
        await hub.apply_snapshot("dev-daniel", assist)
        await scheduler.run_once()
        await scheduler.run_once()
        return hub

    hub = asyncio.run(scenario())

    assert entries(hub) == [("skipped", None, "assist board: launches only on a click")]


# -- ▶ Run waves stopping --------------------------------------------------------


def test_waves_keep_running_when_the_settings_change_during_the_stopping_pass(
    tmp_path,
):
    later = "2026-10-24T06:00:00Z"

    def waiting(**policy):  # nothing to do: 01 waits for a human
        return fleet(frontier("01", col="human", checks={}),
                     night_shift=True, **policy)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub, scheduler, _ = await start(tmp_path, clock=clock)

        async def reschedule(_message):  # the developer, mid-pass
            hub.store.update_settings({"waves": True, "waves_start": later})

        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", waiting(require_human_before_pr=True))
        await scheduler.run_once()  # the stop condition, seen once
        clock.now += timedelta(seconds=30)
        # Still nothing to do, for another reason: logging it is the await
        # the developer's change lands in.
        await hub.apply_snapshot("dev-daniel", waiting(require_human_before_pr=False))
        hub.ui.add(FakeSocket(on_send=reschedule))
        await scheduler.run_once()
        return hub, scheduler

    hub, scheduler = asyncio.run(scenario())

    assert [e[2] for e in entries(hub)] == [
        "require_human_before_pr: a human opens the PR",
        "review not passed: no review reported",
    ]
    assert scheduler.settings().waves_start == later
    assert scheduler.settings().waves is True
    assert entries(hub, "waves_off") == []


def test_waves_stop_only_once_the_condition_held_across_an_export_cycle(tmp_path):
    # A blocker's board can show it done before the dependent's board shows
    # the dependent on the frontier: one pass seeing nothing left is not enough.
    done = fleet(frontier("01", col="done"))
    working = fleet(frontier("01", col="working"))

    async def scenario():
        clock = Clock()
        hub, scheduler, _ = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})

        async def at(seconds, snap):
            clock.now = START + timedelta(seconds=seconds)
            await hub.apply_snapshot("dev-daniel", snap)
            await scheduler.run_once()
            return scheduler.settings().waves

        return [
            await at(0, done),
            await at(10, done),  # too soon after the first sighting
            await at(20, working),  # the condition broke: start over
            await at(40, done),
            await at(60, done),
            await at(65, done),
        ]

    assert asyncio.run(scenario()) == [True, True, True, True, True, False]


# -- retry limits ----------------------------------------------------------------


def test_a_merge_that_fails_while_it_is_being_sent_is_not_retried(tmp_path):
    ready = fleet(frontier("01", col="pr", note="#1 ready to merge"),
                  frontier("09", col="working"), auto_merge=True, **SHIPS)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub = None

        async def fast_failure(rpc):  # the daemon answers before send returns
            await reply(hub, ws, rpc, ok=False, exit_code=10,
                        reason="conflicts: the PR conflicts with the base branch")  # fmt: skip

        ws = FakeSocket(on_send=fast_failure)
        hub, scheduler, _ = await start(tmp_path, ws=ws, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", ready)
        await confirmed(scheduler, clock)
        clock.now += timedelta(hours=1)
        await scheduler.run_once()
        return hub, nns(ws, "merge_pr")

    hub, merges = asyncio.run(scenario())

    assert merges == ["01"]
    assert entries(hub)[1:3] == [  # after "not ready: confirming on the next export"
        ("failed", "01", "conflicts: the PR conflicts with the base branch"),
        ("skipped", "01", "merge failed — waiting for developer"),
    ]


def test_a_launch_the_schema_refuses_twice_is_left_to_the_developer(tmp_path):
    bad = fleet(frontier("27", smodel="-rm"))  # not a model name: BadAction

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        await hub.apply_snapshot("dev-daniel", bad)
        for _ in range(3):
            await scheduler.run_once()
            clock.now += timedelta(minutes=11)
        return hub, ws

    hub, ws = asyncio.run(scenario())

    assert rpcs(ws) == []
    assert [e[0] for e in entries(hub)] == ["failed", "failed", "skipped"]
    assert entries(hub)[-1][2] == "launch failed twice — waiting for developer"


def test_a_failed_merge_stays_given_up_across_a_hub_restart(tmp_path):
    ready = fleet(frontier("01", col="pr", note="#1 ready to merge"),
                  frontier("09", col="working"), auto_merge=True, **SHIPS)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", ready)
        await confirmed(scheduler, clock)
        await reply(hub, ws, rpcs(ws, "merge_pr")[0], ok=False, exit_code=12,
                    reason="gate failed: the PR is not ready to merge")  # fmt: skip
        await asyncio.sleep(0)
        hub.store.close()

        clock.now += timedelta(hours=1)
        restarted, scheduler, ws = await start(tmp_path, clock=clock)
        await restarted.apply_snapshot("dev-daniel", ready)
        await confirmed(scheduler, clock)
        return nns(ws, "merge_pr")

    assert asyncio.run(scenario()) == []


def test_a_column_change_clears_a_stored_retry_and_a_failure_survives_a_restart(
    tmp_path,
):
    def snap(col):  # 09 keeps waves on throughout
        return fleet(frontier("01", col=col, checks=PASSED),
                     frontier("09", col="working"), **SHIPS)  # fmt: skip

    async def fail_once(hub, scheduler, ws, clock):
        clock.now += timedelta(minutes=11)
        await scheduler.run_once()
        await reply(hub, ws, rpcs(ws, "open_pr")[-1], ok=False, exit_code=1,
                    output_tail="gh: rate limited\n")  # fmt: skip
        await asyncio.sleep(0)

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", snap("human"))
        await fail_once(hub, scheduler, ws, clock)
        stored = hub.store.retries()
        await hub.apply_snapshot("dev-daniel", snap("working"))
        await scheduler.run_once()
        cleared = hub.store.retries()

        await hub.apply_snapshot("dev-daniel", snap("human"))
        await fail_once(hub, scheduler, ws, clock)  # 1 of 2 again
        hub.store.close()
        restarted, scheduler, ws = await start(tmp_path, clock=clock)
        await restarted.apply_snapshot("dev-daniel", snap("human"))
        await fail_once(restarted, scheduler, ws, clock)  # 2 of 2, after a restart
        return stored, cleared, restarted.store.retries()

    stored, cleared, after = asyncio.run(scenario())

    key = ("dev-daniel/ticket-board/tk-hub", "01")
    assert stored == {key: {"col": "human", "failed": 1, "given_up": None, "pr": None}}
    assert cleared == {}
    assert after[key] == {"col": "human", "failed": 2, "pr": None,
                          "given_up": "PR failed twice — waiting for developer"}  # fmt: skip


# -- boxes and PRs leaving the run ---------------------------------------------


def test_a_box_that_drops_out_stays_out_until_the_next_run(tmp_path):
    async def scenario():
        clock = Clock()
        hub, scheduler, old = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=0))
        await hub.disconnected("dev-daniel", old)
        await scheduler.run_once()  # offline just now: not judged yet
        clock.now += timedelta(minutes=3)
        await scheduler.run_once()
        await scheduler.run_once()
        new = FakeSocket()
        await hub.connected("dev-daniel", new, fixture("hello.json"))
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=0))
        await scheduler.run_once()
        during = nns(new)
        await scheduler.update_settings(waves=False)
        await scheduler.update_settings(waves=True)  # the next run
        await scheduler.run_once()
        return hub, during, nns(new)

    hub, during, next_run = asyncio.run(scenario())

    assert during == []
    assert next_run == ["27"]
    left = [e for e in entries(hub) if "left out" in e[2]]
    assert left == [("skipped", None, "box dev-daniel offline — left out of this run")]


def test_an_unmergeable_pr_is_held_while_that_pr_stands(tmp_path):
    def snap(col, note="", **extra):
        return fleet(frontier("01", col=col, note=note, **extra),
                     frontier("09", col="working"), auto_merge=True, **SHIPS)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})

        async def at(*args, **extra):
            await hub.apply_snapshot("dev-daniel", snap(*args, **extra))
            await confirmed(scheduler, clock)

        await at("pr", "#1 checks failed")
        # A forge outage: tk.sh can't see the PR and shows the ticket in human.
        await at("human", "agent finished", checks=PASSED)
        # CI was re-run and passed: still left to the human who looked at it.
        await at("pr", "#1 ready to merge")
        held = (nns(ws, "open_pr"), nns(ws, "merge_pr"))
        await at("pr", "#2 ready to merge")  # a new PR: the hold is off
        return hub, held, nns(ws, "merge_pr")

    hub, held, later = asyncio.run(scenario())

    assert (held, later) == (([], []), ["01"])
    assert (
        entries(hub).count(("skipped", "01", "PR not mergeable — waiting for human"))
        == 1
    )


def test_a_hold_on_a_pr_without_a_number_stands_through_human(tmp_path):
    # Notes without "#N": nothing tells two PRs apart, so only a column other
    # than pr or human lets the hold go. Merge-failed holds the same way.
    def snap(col, note="", **extra):
        return fleet(frontier("01", col=col, note=note, **extra),
                     frontier("02", col="pr", note="ready to merge"),
                     frontier("09", col="working"), auto_merge=True, **SHIPS)  # fmt: skip

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        hub.store.update_settings({"waves": True})

        async def at(*args, **extra):
            await hub.apply_snapshot("dev-daniel", snap(*args, **extra))
            await confirmed(scheduler, clock)

        await at("pr", "checks failed")
        for rpc in rpcs(ws, "merge_pr"):  # 02's merge fails: held too
            await reply(hub, ws, rpc, ok=False, exit_code=12, reason="gate failed")
        await asyncio.sleep(0)
        await at("human", "agent finished", checks=PASSED)
        await at("pr", "ready to merge")
        held = (nns(ws, "open_pr"), nns(ws, "merge_pr"))
        await at("working")
        await at("pr", "ready to merge")
        return held, nns(ws, "merge_pr")

    held, later = asyncio.run(scenario())

    assert held == ([], ["02"])
    assert later == ["02", "01"]


def test_a_box_that_never_sends_a_snapshot_is_left_out_after_the_grace(tmp_path):
    # A daemon whose snapshots the hub refuses (say a protocol mismatch), or
    # that keeps reconnecting: online, never known.
    async def scenario():
        clock = Clock()
        hub, scheduler, _ = await start(tmp_path, clock=clock)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=5))
        await scheduler.update_settings(waves=True)
        for _ in range(2):  # reconnecting doesn't restart the grace
            await hub.connected("dev-daniel", FakeSocket(), fixture("hello.json"))
            await scheduler.run_once()
            clock.now += timedelta(minutes=1)
        before_grace = set(scheduler.dropped)
        clock.now += timedelta(minutes=1)
        await scheduler.run_once()  # 3 min without a snapshot: left out
        before = scheduler.settings().waves
        clock.now += timedelta(seconds=30)
        await scheduler.run_once()
        return hub, scheduler, before, before_grace

    hub, scheduler, before, before_grace = asyncio.run(scenario())

    assert (before_grace, scheduler.dropped) == (set(), {"dev-daniel"})
    assert ("skipped", None, "box dev-daniel no snapshot — left out of this run") in (
        entries(hub)
    )
    assert (before, scheduler.settings().waves) == (True, False)


def test_waves_turned_on_again_while_on_keep_the_boxes_left_out(tmp_path):
    async def scenario():
        _, scheduler, _ = await start(tmp_path)
        await scheduler.update_settings(waves=True)
        scheduler.dropped = {"dev-old"}
        await scheduler.update_settings(waves=True, waves_start=None)
        kept = set(scheduler.dropped)
        await scheduler.update_settings(waves=False)
        await scheduler.update_settings(waves=True)
        return kept, scheduler.dropped

    assert asyncio.run(scenario()) == ({"dev-old"}, set())


def test_an_older_hub_database_gains_the_retry_pr_column(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "hub.db")
    db.execute("CREATE TABLE ticket_retries (board_key TEXT NOT NULL, nn TEXT NOT"
               " NULL, col TEXT NOT NULL, failed INTEGER NOT NULL, given_up TEXT,"
               " PRIMARY KEY (board_key, nn))")  # fmt: skip
    db.execute("INSERT INTO ticket_retries VALUES ('a/b/c', '01', 'pr', 1, 'x')")
    db.commit()
    db.close()

    hub = Hub(tmp_path / "hub.db")

    assert hub.store.retries() == {
        ("a/b/c", "01"): {"col": "pr", "failed": 1, "given_up": "x", "pr": None}
    }


def test_a_hub_restart_in_the_middle_of_a_run_drops_no_box(tmp_path):
    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        await scheduler.update_settings(waves=True)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=5))
        await hub.disconnected("dev-daniel", ws)  # the hub shutting down
        await scheduler.run_once()
        hub.store.close()

        clock.now += timedelta(seconds=40)
        hub = Hub(tmp_path / "hub.db")  # every box offline, every board stale
        scheduler = Scheduler(hub, clock=clock)
        hub.on_change = lambda: None
        await scheduler.run_once()  # the first tick, before any daemon is back
        new = FakeSocket()
        await hub.connected("dev-daniel", new, fixture("hello.json"))
        clock.now += timedelta(seconds=5)
        await scheduler.run_once()  # connected, no snapshot yet
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=0))
        await scheduler.run_once()
        return hub, scheduler, nns(new)

    hub, scheduler, launched = asyncio.run(scenario())

    assert (scheduler.dropped, scheduler.settings().waves) == (set(), True)
    assert launched == ["27"]
    assert [e for e in entries(hub) if "left out" in e[2]] == []


def test_a_box_left_out_stays_out_across_a_hub_restart(tmp_path):
    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        await scheduler.update_settings(waves=True)
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27"), running=0))
        await hub.disconnected("dev-daniel", ws)
        await scheduler.run_once()
        clock.now += timedelta(minutes=3)
        await scheduler.run_once()  # offline past the grace: left out
        hub.store.close()

        restarted, scheduler, new = await start(tmp_path, clock=clock)
        await restarted.apply_snapshot("dev-daniel", fleet(frontier("27"), running=0))
        await scheduler.run_once()
        return scheduler.dropped, nns(new)

    assert asyncio.run(scenario()) == ({"dev-daniel"}, [])


def test_waves_scheduled_through_the_settings_start_on_time(tmp_path):
    later = START + timedelta(hours=1)
    night_only = fleet(frontier("27"), running=0, autonomy="assist", night_shift=True)

    async def scenario():
        clock = Clock()
        hub, scheduler, ws = await start(tmp_path, clock=clock)
        await hub.apply_snapshot("dev-daniel", night_only)
        await scheduler.update_settings(
            waves=True, waves_start=later.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        await scheduler.run_once()
        before = nns(ws)
        clock.now = later
        await scheduler.run_once()
        return before, nns(ws), scheduler.settings().waves

    assert asyncio.run(scenario()) == ([], ["27"], True)


# -- the hub's hooks -------------------------------------------------------------


def test_a_failing_scheduler_hook_never_breaks_the_daemon_connection(tmp_path, caplog):
    async def scenario():
        hub, _, ws = await start(tmp_path)
        ui = FakeSocket()
        hub.ui.add(ui)

        def broken(*_):
            raise RuntimeError("database is locked")

        hub.on_change = hub.on_rpc_result = broken
        await hub.apply_snapshot("dev-daniel", fleet(frontier("27")))
        rpc_id = await hub.send_rpc("dev-daniel", "rescan_boards", {})
        await reply(hub, ws, {"id": rpc_id}, ok=True, exit_code=0)
        return ui

    ui = asyncio.run(scenario())

    assert [m["type"] for m in ui.sent][-1] == "rpc_result"
    assert "database is locked" in caplog.text
