"""The scheduler's decision function over a fake fleet and a fake clock: which
frontier tickets launch where (caps, ordering, usage gate), which boards and
boxes are never scheduled, and what ▶ Run waves ships and when it stops. No sockets: `decide` only
reads boards, devbox statuses, hub settings and what is already in flight."""

from datetime import UTC, datetime, timedelta

from hub_server.scheduler import Inflight, Settings, decide

NOW = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def window(pct, resets_in=timedelta(hours=2)):
    return {"used_percentage": pct, "resets_at": stamp(NOW + resets_in)}


def usage(five=None, seven=None, state="ok"):
    return {"state": state, "five_hour": five, "seven_day": seven,
            "updated_at": stamp(NOW)}  # fmt: skip


UNKNOWN = {"state": "unknown", "five_hour": None, "seven_day": None, "updated_at": None}


def box(name="box-a", running=0, online=True, herdr=True, use=None):
    return {
        "type": "devbox_status",
        "devbox": name,
        "online": online,
        "last_seen": stamp(NOW),
        "health": {
            "daemon_version": "0.1.0",
            "herdr": {"ok": herdr, "status": "ok" if herdr else "down"},
            "usage": use or usage(window(10), window(20)),
            "running": running,
        },
    }


def ticket(nn, col="backlog", effort="medium", model="sonnet", note="", **extra):
    return {"nn": nn, "title": f"Ticket {nn}", "branch": "", "col": col,
            "smodel": model, "seffort": effort, "note": note, **extra}  # fmt: skip


PASSED = {"tests": "pass", "criteria": "4/4", "review": "ok"}


def reviewed(nn, checks=PASSED, **extra):
    """A ticket in human whose coordinator reported review ok, tests pass and
    every criterion met (phase file line 2: tests=… criteria=m/n review=…)."""
    return ticket(nn, col="human", checks=checks, **extra)


def board(repo="alpha", slug="b", devbox="box-a", tickets=(), archived=False,
          stale=False, has_remote=True, **policy):  # fmt: skip
    return {
        "key": f"{devbox}/{repo}/{slug}",
        "devbox": devbox,
        "repo": repo,
        "slug": slug,
        "has_remote": has_remote,
        "stale": stale,
        "hub_meta": {"archived": archived, "board_brief": None},
        "policy": {
            "forge": "github", "require_human_before_pr": False, "auto_merge": False,
            "night_shift": False, "autonomy": "auto", "max_parallel": 5,
            "usage_gate_pct": 90, "synthesis": {"backends": ["agy"]},
        } | policy,
        "tickets": list(tickets),
    }  # fmt: skip


def run(boards, boxes=None, settings=None, inflight=(), cooldown=None, now=NOW,
        given_up=None, dropped=(), pending=(), ready_since=None,
        unseen=()):  # fmt: skip
    # By default every PR has been "ready to merge" for an hour: confirmed.
    if ready_since is None:
        ready_since = {(b["key"], t["nn"], t["pr"]["number"]): NOW - timedelta(hours=1)
                       for b in boards for t in b["tickets"] if t["col"] == "pr"}  # fmt: skip
    return decide(
        boards,
        boxes if boxes is not None else [box()],
        settings or Settings(),
        list(inflight),
        cooldown or {},
        now,
        given_up or {},
        set(dropped),
        set(pending),
        ready_since,
        set(unseen),
    )


def sent(plan):
    return [(a.method, a.board_key.split("/")[1], a.nn) for a in plan.actions]


def why(plan, nn):
    return next(s.reason for s in plan.skipped if s.nn == nn)


# -- launching the frontier ----------------------------------------------------


def test_an_auto_board_launches_its_frontier_with_the_suggested_model_and_effort():
    plan = run([board(tickets=[ticket("01", effort="low", model="haiku"),
                               ticket("02", col="blocked"), ticket("03", col="working")])])  # fmt: skip

    [launch] = plan.actions
    assert (launch.method, launch.devbox, launch.board_key, launch.nn) == (
        "launch_ticket", "box-a", "box-a/alpha/b", "01")  # fmt: skip
    assert launch.params == {"type": "feat", "model": "haiku", "effort": "low",
                             "manual": False}  # fmt: skip


def test_a_box_launches_no_more_than_its_max_parallel():
    plan = run(
        [board(tickets=[ticket("01"), ticket("02"), ticket("03")], max_parallel=5)],
        [box(running=4)],
    )

    assert sent(plan) == [("launch_ticket", "alpha", "01")]
    assert why(plan, "02") == "box-a is at max_parallel (5 running, cap 5)"


def test_launches_already_in_flight_count_against_the_box_cap():
    earlier = Inflight("box-a", "box-a/alpha/b", "09", "launch_ticket", "backlog", NOW)
    plan = run(
        [board(tickets=[ticket("01"), ticket("09")], max_parallel=2)],
        [box(running=1)],
        inflight=[earlier],
    )

    assert sent(plan) == []
    assert why(plan, "01") == "box-a is at max_parallel (2 running, cap 2)"
    assert [s.nn for s in plan.skipped] == ["01"]  # 09 is in flight, not skipped


def test_the_total_cap_holds_across_boxes():
    plan = run(
        [board(devbox="box-a", tickets=[ticket("01")]),
         board(repo="beta", devbox="box-b", tickets=[ticket("02"), ticket("03")])],
        [box("box-a", running=3), box("box-b", running=3)],
        Settings(total_cap=8),
    )  # fmt: skip

    assert sent(plan) == [
        ("launch_ticket", "alpha", "01"),
        ("launch_ticket", "beta", "02"),
    ]
    assert why(plan, "03") == "the total cap is reached (8 running, cap 8)"


def test_an_over_full_wave_goes_round_robin_across_repos_then_effort_board_nn():
    cap = {"max_parallel": 10}
    plan = run(
        [
            # alpha already has a ticket running: its first frontier ticket
            # waits for the second round.
            board("alpha", "b", tickets=[ticket("01", effort="low"),
                                         ticket("02", effort="low"),
                                         ticket("05", col="working")], **cap),
            board("beta", "b", tickets=[ticket("07", effort="high"),
                                        ticket("03", effort="medium")], **cap),
            board("gamma", "y", tickets=[ticket("04", effort="medium")], **cap),
            board("gamma", "x", tickets=[ticket("09", effort="medium")], **cap),
        ],
        [box(running=1)],
        Settings(total_cap=6),
    )  # fmt: skip

    # Round 1: beta's and gamma's best (equal effort: board order). Round 2:
    # alpha, gamma, beta by effort. alpha's second would open round 3.
    assert sent(plan) == [
        ("launch_ticket", "beta", "03"),
        ("launch_ticket", "gamma", "09"),
        ("launch_ticket", "alpha", "01"),
        ("launch_ticket", "gamma", "04"),
        ("launch_ticket", "beta", "07"),
    ]
    assert why(plan, "02").startswith("the total cap is reached")


# -- the usage gate ------------------------------------------------------------


def test_a_window_at_the_gate_stops_automatic_launches_until_it_resets():
    resets = timedelta(minutes=40)
    fleet = [board(tickets=[ticket("01")], usage_gate_pct=90)]
    gated = [box(use=usage(window(30), window(90, resets_in=resets)))]

    plan = run(fleet, gated)
    later = run(fleet, gated, now=NOW + resets + timedelta(seconds=1))

    assert sent(plan) == []
    assert why(plan, "01") == (
        "box-a's 7-day usage is 90% (gate 90%) until 2026-10-23T22:40:00Z"
    )
    assert sent(later) == [("launch_ticket", "alpha", "01")]


def test_stale_usage_still_gates():
    plan = run(
        [board(tickets=[ticket("01")], usage_gate_pct=80)],
        [box(use=usage(window(85), window(10), state="stale"))],
    )

    assert why(plan, "01") == (
        "box-a's 5-hour usage is 85% (gate 80%) until 2026-10-24T00:00:00Z"
    )


def test_unknown_usage_allows_one_automatic_launch_at_a_time():
    fleet = [board(tickets=[ticket("01"), ticket("02")])]
    unknown = [box(use=UNKNOWN)]

    first = run(fleet, unknown)
    launched = Inflight("box-a", "box-a/alpha/b", "01", "launch_ticket", "backlog", NOW)
    second = run(fleet, unknown, inflight=[launched])

    assert sent(first) == [("launch_ticket", "alpha", "01")]
    assert why(first, "02") == (
        "box-a's usage is unknown: one automatic launch at a time"
    )
    assert sent(second) == []
    assert why(second, "02") == why(first, "02")


def test_the_wave_prefers_the_box_with_more_headroom():
    plan = run(
        [board("alpha", devbox="box-a", tickets=[ticket("01")]),
         board("beta", devbox="box-b", tickets=[ticket("02")])],
        [box("box-a", running=3, use=usage(window(70), window(80))),
         box("box-b", running=4, use=usage(window(10), window(20)))],
        Settings(total_cap=8),
    )  # fmt: skip

    assert sent(plan) == [("launch_ticket", "beta", "02")]


# -- never scheduled -----------------------------------------------------------


def test_assist_archived_offline_and_herdr_down_are_never_scheduled():
    plan = run(
        [
            board("assist", tickets=[ticket("01"), ticket("11")], autonomy="assist"),
            board("archived", tickets=[ticket("02")], archived=True),
            board("offline", devbox="box-off", tickets=[ticket("03")], stale=True),
            board("herdr", devbox="box-herdr", tickets=[ticket("04"), ticket("14")]),
            board("gone", devbox="box-unknown", tickets=[ticket("05")]),
        ],
        [box(), box("box-off", online=False), box("box-herdr", herdr=False)],
    )

    assert plan.actions == []
    # Once per board, not per ticket.
    assert [(s.board_key.split("/")[1], s.nn, s.reason) for s in plan.skipped] == [
        ("assist", None, "assist board: launches only on a click"),
        ("archived", None, "archived board"),
        ("offline", None, "box-off is offline"),
        ("herdr", None, "herdr is down on box-herdr"),
        ("gone", None, "box-unknown is offline"),
    ]


def test_a_box_offline_or_herdr_down_during_waves_leaves_the_run():
    fleet = [
        board("alpha", tickets=[ticket("01"), ticket("02", col="done")], **SHIPS),
        board("beta", devbox="box-off", tickets=[ticket("03"), ticket("04", col="working"),
                                                 reviewed("05")], **SHIPS),
        board("gamma", devbox="box-herdr", tickets=[ticket("06")], **SHIPS),
    ]  # fmt: skip
    boxes = [
        box(),
        box("box-off", online=False, running=1),
        box("box-herdr", herdr=False),
    ]

    plan = run(fleet, boxes, WAVES)

    assert plan.left_out == [
        ("box-herdr", "box box-herdr herdr down — left out of this run"),
        ("box-off", "box box-off offline — left out of this run"),
    ]
    assert sent(plan) == [("launch_ticket", "alpha", "01")]
    assert [
        s.board_key for s in plan.skipped
    ] == []  # the box is logged, not its boards


def test_a_box_is_judged_from_its_status_not_its_boards_staleness():
    # Online with no snapshot past the grace ("unseen"): left out as such. A
    # stale board on an online box doesn't make the box offline.
    fleet = [
        board("alpha", tickets=[ticket("02", col="done")], **SHIPS),
        board("beta", devbox="box-mute", tickets=[ticket("03")], stale=True, **SHIPS),
        board("gamma", devbox="box-b", tickets=[ticket("04")], stale=True, **SHIPS),
    ]  # fmt: skip
    boxes = [box(), box("box-mute"), box("box-b")]

    plan = run(fleet, boxes, WAVES, unseen={"box-mute"}, pending={"box-b"})

    assert plan.left_out == [
        ("box-mute", "box box-mute no snapshot — left out of this run"),
    ]
    assert plan.stop is None  # box-b is still not known


def test_a_box_left_out_stays_out_of_the_run_and_out_of_the_stop():
    fleet = [
        board("alpha", tickets=[ticket("02", col="done")], **SHIPS),
        board("beta", devbox="box-off", tickets=[ticket("03"), ticket("04", col="working"),
                                                 reviewed("05")], **SHIPS),
    ]  # fmt: skip
    back = [box(), box("box-off")]  # reconnected mid-run

    plan = run(fleet, back, WAVES, dropped={"box-off"})

    assert (sent(plan), plan.left_out, plan.stop) == ([], [], DONE)
    assert sent(run(fleet, back)) == [("launch_ticket", "beta", "03")]  # waves off


def test_a_box_not_known_yet_is_neither_judged_nor_lets_waves_stop():
    # pending: online without a snapshot since it connected, or offline for
    # less than the scheduler's grace (a hub restart, a blip).
    fleet = [
        board("alpha", tickets=[ticket("02", col="done")], **SHIPS),
        board("beta", devbox="box-new", tickets=[ticket("03")], stale=True, **SHIPS),
        board("gamma", devbox="box-off", tickets=[ticket("04")], **SHIPS),
    ]  # fmt: skip
    boxes = [box(), box("box-new"), box("box-off", online=False)]

    plan = run(fleet, boxes, WAVES, pending={"box-new", "box-off"})

    assert (sent(plan), plan.left_out, plan.stop) == ([], [], None)
    assert [(s.board_key.split("/")[1], s.reason) for s in plan.skipped] == [
        ("beta", "box-new is not known yet"),
        ("gamma", "box-off is not known yet"),
    ]


def test_an_rpc_in_flight_on_a_box_left_out_does_not_hold_waves_on():
    lost = Inflight("box-off", "box-off/beta/b", "03", "launch_ticket", "backlog", NOW)
    fleet = [board("alpha", tickets=[ticket("02", col="done")], **SHIPS),
             board("beta", devbox="box-off", tickets=[ticket("03")], **SHIPS)]  # fmt: skip
    boxes = [box(), box("box-off", online=False)]

    plan = run(fleet, boxes, WAVES, inflight=[lost], dropped={"box-off"})

    assert plan.stop == DONE


def test_offline_boxes_last_known_running_counts_toward_the_total_cap():
    plan = run(
        [board(tickets=[ticket("01"), ticket("02")])],
        [box(running=1), box("box-off", running=6, online=False)],
        Settings(total_cap=8),
    )

    assert sent(plan) == [("launch_ticket", "alpha", "01")]
    assert why(plan, "02") == (
        "the total cap is reached (8 running, 6 on offline boxes, cap 8)"
    )


# -- ▶ Run waves ---------------------------------------------------------------

WAVES = Settings(waves=True, waves_start=stamp(NOW - timedelta(minutes=5)))


def test_waves_open_a_pr_or_push_for_tickets_that_passed_review():
    fleet = [
        board("hub", tickets=[reviewed("01", note="agent finished")],
              night_shift=True),
        board("plain", tickets=[reviewed("02", note="agent finished")],
              night_shift=True, forge="none"),
    ]  # fmt: skip

    assert sent(run(fleet, settings=WAVES)) == [
        ("open_pr", "hub", "01"),
        ("ssh_push", "plain", "02"),
    ]
    assert sent(run(fleet)) == []  # waves off: nothing ships


def test_policy_wins_over_waves():
    plan = run(
        [
            board("human", tickets=[reviewed("01")],
                  night_shift=True, require_human_before_pr=True),
            board("optout", tickets=[reviewed("02")]),
            board("pushed", tickets=[reviewed("03", note="pushed 2m ago")],
                  night_shift=True, forge="none"),
            board("local", tickets=[reviewed("04")],
                  night_shift=True, forge="none", has_remote=False),
        ],
        settings=WAVES,
    )  # fmt: skip

    assert plan.actions == []
    assert {s.nn: s.reason for s in plan.skipped} == {
        "01": "require_human_before_pr: a human opens the PR",
        "02": "night_shift is off for this board",
        "03": "already pushed: merge it on the forge",
        "04": "the repo has no remote: merge the branch locally",
    }


def test_waves_ship_only_what_passed_review():
    # human only says the agent stopped (idle or done): its checks say whether
    # the review passed.
    plan = run(
        [board(tickets=[
            reviewed("01"),
            reviewed("02", checks={"tests": "none", "criteria": "3/3", "review": "ok"}),
            reviewed("03", checks={}),
            reviewed("04", checks={"tests": "pass", "criteria": "4/4", "review": "2-blocking"}),
            reviewed("05", checks={"tests": "pass", "criteria": "3/4", "review": "ok"}),
            reviewed("06", checks={"tests": "fail", "criteria": "4/4", "review": "ok"}),
            reviewed("07", checks={"tests": "pass", "review": "ok"}),
            reviewed("08", checks={"criteria": "3/3", "review": "ok"}),
            reviewed("09", checks={"tests": "pass", "criteria": "0/0", "review": "ok"}),
            reviewed("10", checks={"tests": "pass", "criteria": "all", "review": "ok"}),
            reviewed("11", checks={"tests": "pass", "criteria": "²/²", "review": "ok"}),
            reviewed("12", checks={"tests": "pass", "criteria": "3/03", "review": "ok"}),
        ], night_shift=True)],
        settings=WAVES,
    )  # fmt: skip

    assert sent(plan) == [
        ("open_pr", "alpha", "01"),
        ("open_pr", "alpha", "02"),
        ("open_pr", "alpha", "12"),
    ]
    assert {s.nn: s.reason for s in plan.skipped} == {
        "03": "review not passed: no review reported",
        "04": "review not passed: review 2-blocking",
        "05": "review not passed: criteria 3/4",
        "06": "review not passed: tests fail",
        "07": "review not passed: no criteria reported",
        "08": "review not passed: no tests reported",
        "09": "review not passed: criteria 0/0",
        "10": "review not passed: criteria all",
        "11": "review not passed: criteria ²/²",
    }


def test_waves_arm_an_assist_board_whose_policy_says_night_shift():
    fleet = [board(tickets=[ticket("01")], autonomy="assist", night_shift=True)]

    assert sent(run(fleet, settings=WAVES)) == [("launch_ticket", "alpha", "01")]
    assert sent(run(fleet)) == []


def pr(nn, note, mergeable="MERGEABLE"):
    return ticket(nn, col="pr", note=note,
                  pr={"number": int(nn), "isDraft": False, "mergeable": mergeable,
                      "review": "", "checks": []})  # fmt: skip


def test_waves_merge_ready_prs_one_per_board_when_auto_merge():
    fleet = [
        board("merge", tickets=[pr("03", "#3 ready to merge"), pr("02", "#2 ready to merge"),
                                pr("04", "#4 checks running")],
              night_shift=True, auto_merge=True),
        board("nomerge", tickets=[pr("05", "#5 ready to merge")], night_shift=True),
    ]  # fmt: skip

    plan = run(fleet, settings=WAVES)

    # tk.sh ready's verdict is the board's own `pr` note ("ready to merge"),
    # re-exported every 20 s; merge_pr re-runs every gate before merging. One
    # merge per board at a time: each one moves the base under the others.
    assert sent(plan) == [("merge_pr", "merge", "02")]
    assert {s.nn: s.reason for s in plan.skipped} == {
        "03": "another merge on this board goes first",
        "04": "not ready: #4 checks running",
        "05": "auto_merge is off for this board",
    }
    assert sent(run(fleet)) == []


def test_a_ready_pr_in_cooldown_does_not_hold_the_boards_other_merges():
    cooldown = {("box-a/alpha/b", "02", "merge_pr"): NOW + timedelta(minutes=5)}
    plan = run(
        [board(tickets=[pr("02", "#2 ready to merge"), pr("03", "#3 ready to merge")],
               night_shift=True, auto_merge=True)],
        settings=WAVES,
        cooldown=cooldown,
    )  # fmt: skip

    assert sent(plan) == [("merge_pr", "alpha", "03")]
    assert why(plan, "02").startswith("the last merge_pr failed")


def test_a_pr_that_needs_a_person_leaves_the_wave():
    # tk.sh's pr note is "#N <state>"; a state a person has to act on (or one
    # this hub doesn't know) drops the PR from the wave.
    states = ["draft", "conflict", "changes requested", "checks failed",
              "awaiting review", "something new"]  # fmt: skip
    tickets = [pr(f"{i:02}", f"#{i} {state}") for i, state in enumerate(states, 1)]

    plan = run([board(tickets=tickets, auto_merge=True, **SHIPS)], settings=WAVES)

    assert sent(plan) == []
    assert {s.nn: s.reason for s in plan.skipped} == {
        t["nn"]: "PR not mergeable — waiting for human" for t in tickets
    }
    assert plan.unmergeable == [
        ("box-a/alpha/b", t["nn"], int(t["nn"])) for t in tickets
    ]
    assert plan.stop == DONE


def test_a_pr_only_pending_keeps_waiting():
    # CI still running, or the forge not having computed mergeability yet,
    # resolve without a person: waves wait for them.
    plan = run(
        [board(tickets=[pr("01", "#1 checks running"),
                        pr("02", "#2 ready to merge", mergeable="UNKNOWN")],
               auto_merge=True, **SHIPS)],
        settings=WAVES,
    )  # fmt: skip

    assert sent(plan) == []
    assert {s.nn: s.reason for s in plan.skipped} == {
        "01": "not ready: #1 checks running",
        "02": "not ready: #2 mergeability not computed yet",
    }
    assert (plan.unmergeable, plan.stop) == ([], None)


def test_a_ready_pr_is_merged_once_ready_held_across_an_export_cycle():
    # CI may not have registered yet on the first export that says ready.
    fleet = [board(tickets=[pr("01", "#1 ready to merge")], auto_merge=True, **SHIPS)]
    key = ("box-a/alpha/b", "01", 1)  # the PR number too
    older = ("box-a/alpha/b", "01", 7)  # a PR this ticket had before

    first = run(fleet, settings=WAVES, ready_since={})
    soon = run(fleet, settings=WAVES, ready_since={key: NOW - timedelta(seconds=10)})
    later = run(fleet, settings=WAVES, ready_since={key: NOW - timedelta(seconds=30)})
    other = run(fleet, settings=WAVES, ready_since={older: NOW - timedelta(hours=1)})

    assert (sent(first), first.ready, first.stop) == ([], [key], None)
    assert why(first, "01") == "not ready: confirming on the next export"
    assert sent(soon) == []
    assert sent(later) == [("merge_pr", "alpha", "01")]
    assert sent(other) == []  # a new PR starts the confirmation again


def test_a_merge_in_flight_holds_the_boards_next_merge():
    merging = Inflight("box-a", "box-a/alpha/b", "02", "merge_pr", "pr", NOW)
    plan = run(
        [board(tickets=[pr("02", "#2 ready to merge"), pr("03", "#3 ready to merge")],
               night_shift=True, auto_merge=True)],
        settings=WAVES,
        inflight=[merging],
    )  # fmt: skip

    assert sent(plan) == []
    assert why(plan, "03") == "another merge on this board goes first"


def test_waves_wait_for_their_start_time():
    fleet = [board(tickets=[ticket("01"), reviewed("02")],
                   autonomy="assist", night_shift=True)]  # fmt: skip
    later = Settings(waves=True, waves_start=stamp(NOW + timedelta(hours=2)))

    before = run(fleet, settings=later)
    at_start = run(fleet, settings=later, now=NOW + timedelta(hours=2))

    assert sent(before) == []
    assert before.stop is None
    assert sent(at_start) == [
        ("launch_ticket", "alpha", "01"),
        ("open_pr", "alpha", "02"),
    ]


DONE = "nothing left to launch, ship or merge"
SHIPS = {"night_shift": True, "require_human_before_pr": False}


def test_waves_stay_on_while_started_tickets_are_still_working():
    working = [board(tickets=[ticket("01", col="working"), ticket("02", col="review"),
                              ticket("03", col="done")], **SHIPS)]  # fmt: skip
    finished = [board(tickets=[ticket("03", col="done"), ticket("04", col="needs")])]
    launched = Inflight("box-a", "box-a/alpha/b", "03", "launch_ticket", "backlog", NOW)

    assert run(working, settings=WAVES).stop is None
    assert run(finished, settings=WAVES).stop == DONE
    assert run(finished, settings=WAVES, inflight=[launched]).stop is None
    assert run(finished).stop is None  # waves off: nothing to stop


def test_waves_ship_what_passed_review_before_stopping():
    plan = run([board(tickets=[reviewed("01")], **SHIPS)], settings=WAVES)

    assert sent(plan) == [("open_pr", "alpha", "01")]
    assert plan.stop is None


def test_human_tickets_policy_wont_ship_let_waves_stop():
    for fleet in (
        [
            board(
                tickets=[reviewed("01")], night_shift=True, require_human_before_pr=True
            )
        ],
        [board(tickets=[reviewed("01", checks={})], **SHIPS)],  # review not passed
        [board(tickets=[reviewed("01")])],  # night_shift off
        [board(tickets=[reviewed("01", note="pushed 3m ago")], forge="none", **SHIPS)],
    ):
        assert run(fleet, settings=WAVES).stop == DONE, fleet[0]["policy"]


def test_a_pr_policy_wont_merge_lets_waves_stop_once_it_is_open():
    fleet = [board(tickets=[pr("01", "#1 ready to merge")], **SHIPS)]  # no auto_merge

    assert run(fleet, settings=WAVES).stop == DONE


def test_under_auto_merge_waves_merge_a_ready_pr_and_wait_for_one_not_ready():
    merges = SHIPS | {"auto_merge": True}
    ready = run([board(tickets=[pr("01", "#1 ready to merge")], **merges)],
                settings=WAVES)  # fmt: skip
    pending = run([board(tickets=[pr("01", "#1 checks running")], **merges)],
                  settings=WAVES)  # fmt: skip

    assert (sent(ready), ready.stop) == ([("merge_pr", "alpha", "01")], None)
    assert (sent(pending), pending.stop) == ([], None)


def test_a_blocked_ticket_keeps_waves_on_only_while_a_blocker_is_still_moving():
    merges = SHIPS | {"auto_merge": True}
    by_merge = [board(tickets=[pr("01", "#1 checks running"),
                               ticket("02", col="blocked", open_blockers=["01"])],
                      **merges)]  # fmt: skip
    by_human = [board(tickets=[reviewed("01", checks={}),
                               ticket("02", col="blocked", open_blockers=["01"])],
                      **merges)]  # fmt: skip
    chain = [board(tickets=[pr("01", "#1 checks running"),
                            ticket("02", col="blocked", open_blockers=["01"]),
                            ticket("03", col="blocked", open_blockers=["02"])],
                   **merges)]  # fmt: skip
    chain[0]["tickets"] = chain[0]["tickets"][::-1]  # 03 is looked at before 02

    assert run(by_merge, settings=WAVES).stop is None
    assert run(by_human, settings=WAVES).stop == DONE
    assert run(chain, settings=WAVES).stop is None


def test_a_blocked_ticket_never_holds_waves_on_by_itself():
    # Only a moving blocker can unblock it, and that blocker holds waves on by
    # itself, on whichever scheduled board; a blocker on an unscheduled or
    # archived board finishes only through a human.
    def waiting(blocker):
        return board("alpha", tickets=[ticket("02", col="blocked",
                                              open_blockers=[blocker])], **SHIPS)  # fmt: skip

    finished = board("beta", tickets=[ticket("05", col="needs")], **SHIPS)
    working = board("beta", tickets=[ticket("05", col="working")], **SHIPS)

    assert run([waiting("beta:5"), finished], settings=WAVES).stop == DONE
    assert run([waiting("gamma:5 (not found)")], settings=WAVES).stop == DONE
    assert run([waiting("beta:5"), working], settings=WAVES).stop is None
    # beta is not scheduled (assist, night_shift off): its 05 moves for a human.
    unscheduled = board("beta", tickets=[ticket("05", col="working")],
                        autonomy="assist", night_shift=False)  # fmt: skip
    assert run([waiting("beta:5"), unscheduled], settings=WAVES).stop == DONE


def test_a_ticket_blocked_on_a_settled_push_does_not_hold_waves_on():
    pushed = Inflight("box-a", "box-a/alpha/b", "01", "ssh_push", "human", NOW,
                      settled=True)  # fmt: skip
    fleet = [board(tickets=[reviewed("01"),
                            ticket("02", col="blocked", open_blockers=["01"])],
                   forge="none", **SHIPS)]  # fmt: skip

    plan = run(fleet, settings=WAVES, inflight=[pushed])

    assert (sent(plan), plan.stop) == ([], DONE)


def test_an_archived_board_does_not_hold_waves_on():
    fleet = [board(tickets=[ticket("01", col="working"), ticket("02")],
                   archived=True, **SHIPS)]  # fmt: skip

    assert run(fleet, settings=WAVES).stop == DONE


def test_a_backlog_ticket_without_a_suggestion_is_not_frontier():
    plan = run([board(tickets=[ticket("01", model="")])], settings=WAVES)

    assert sent(plan) == []
    assert why(plan, "01") == "no suggested model/effort"
    assert plan.stop == DONE


def test_a_ticket_the_scheduler_gave_up_on_waits_for_the_developer():
    fleet = [board(tickets=[reviewed("01"), pr("02", "#2 ready to merge"),
                            ticket("03")], auto_merge=True, **SHIPS)]  # fmt: skip
    given_up = {
        ("box-a/alpha/b", "01"): "PR failed twice — waiting for developer",
        ("box-a/alpha/b", "02"): "merge failed — waiting for developer",
        ("box-a/alpha/b", "03"): "launch failed twice — waiting for developer",
    }

    plan = run(fleet, settings=WAVES, given_up=given_up)

    assert sent(plan) == []
    assert {s.nn: s.reason for s in plan.skipped} == {
        "01": "PR failed twice — waiting for developer",
        "02": "merge failed — waiting for developer",
        "03": "launch failed twice — waiting for developer",
    }
    assert plan.stop == DONE  # 03 given up: not frontier


def test_a_push_that_went_through_does_not_hold_waves_on():
    pushed = Inflight("box-a", "box-a/alpha/b", "01", "ssh_push", "human", NOW,
                      settled=True)  # fmt: skip
    fleet = [board(tickets=[reviewed("01")], forge="none", **SHIPS)]

    assert run(fleet, settings=WAVES, inflight=[pushed]).stop == DONE


def test_waves_stop_when_every_remaining_launch_is_held_by_the_usage_gate():
    gated = box("box-a", use=usage(window(95), window(20)))
    fleet = [board(tickets=[ticket("01"), ticket("02", col="blocked")])]
    elsewhere = fleet + [board("beta", devbox="box-b", tickets=[ticket("03")])]
    working = [board(tickets=[ticket("01"), ticket("02", col="working")])]

    plan = run(fleet, [gated], settings=WAVES)
    full = run(elsewhere, [gated, box("box-b", running=5)], settings=WAVES)

    assert plan.stop == "every remaining launch is held by the usage gate"
    assert full.stop is None  # box-b's frontier waits for a cap, not the gate
    # Still working, or a PR it will merge once ready: keep on, so it ships.
    assert run(working, [gated], settings=WAVES).stop is None
    merging = [board(tickets=[ticket("01"), pr("02", "#2 checks running")],
                     auto_merge=True, **SHIPS)]  # fmt: skip
    assert run(merging, [gated], settings=WAVES).stop is None


def test_a_failed_automatic_rpc_waits_out_its_cooldown():
    fleet = [board(tickets=[ticket("01"), reviewed("02")], night_shift=True)]
    until = NOW + timedelta(minutes=10)
    cooldown = {("box-a/alpha/b", "01", "launch_ticket"): until,
                ("box-a/alpha/b", "02", "open_pr"): until}  # fmt: skip

    plan = run(fleet, settings=WAVES, cooldown=cooldown)
    later = run(fleet, settings=WAVES, cooldown=cooldown, now=until)

    assert sent(plan) == []
    assert (
        why(plan, "01")
        == "the last launch_ticket failed: retry after 2026-10-23T22:10:00Z"
    )
    assert (
        why(plan, "02") == "the last open_pr failed: retry after 2026-10-23T22:10:00Z"
    )
    assert sent(later) == [("launch_ticket", "alpha", "01"), ("open_pr", "alpha", "02")]
