"""The hub's scheduler (board.md, Scheduler): runs frontier waves on its own
and, while ▶ Run waves is on, ships what passed review.

▶ Run waves is board.md's ☾ Night, reworked by the developer (2026-10-23): no
time of day and no auto-off time. It starts now or at a chosen time, arms the
boards whose policy says night_shift as auto, opens PRs / pushes and merges as
Night did, and turns itself off once nothing is left to launch, ship or
merge, or (nothing still moving) every remaining launch is held by the usage
gate.

`decide` is the whole policy, a pure function of the fleet (boards, devbox
statuses), the hub settings, what the scheduler already has in flight and the
clock. `Scheduler` runs it on every state change and on a tick, sends the RPCs
it asks for and logs every decision."""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta

from hub_server.hub import BadAction, DevboxOffline, Hub, frontier_launches

TICK = 30.0  # seconds between passes besides those state changes trigger
# How long an automatic RPC holds its ticket when no snapshot shows it moving
# (ssh_push leaves it in human), and how long a failed one waits to retry.
INFLIGHT_TTL = timedelta(minutes=10)
COOLDOWN = timedelta(minutes=10)
MAX_ATTEMPTS = 2  # launch_ticket / open_pr / ssh_push; a failed merge_pr: none
WHAT = {"launch_ticket": "launch", "open_pr": "PR", "ssh_push": "push"}
# ▶ Run waves stops only on a condition that held through two passes this far
# apart: more than one export cycle (20 s), so a blocker's board showing it
# done and the dependent's board showing it on the frontier both arrive.
STOP_CONFIRM = timedelta(seconds=25)
# A box offline for less than this is not known yet, not offline: a hub
# restart, or a daemon reconnecting with backoff.
DROP_GRACE = timedelta(minutes=2)
# The column each automatic RPC moves its ticket out of.
FROM_COL = {"launch_ticket": "backlog", "open_pr": "human", "ssh_push": "human",
            "merge_pr": "pr"}  # fmt: skip

# What the hub (or the daemon) reports for an rpc whose outcome it doesn't
# know: the command may still be running on the box.
UNKNOWN_OUTCOME = re.compile(
    r"^(timed out after [0-9.]+s|devbox disconnected|devbox reconnected)$"
)

# tk.sh's note on a ticket in pr, "#N <state>".
PR_NOTE = re.compile(r"(#[0-9]+) (.+)")
PR_PENDING = {"checks running"}  # resolves without a person
UNMERGEABLE = "PR not mergeable — waiting for human"
# checks.criteria, met/total ("3/4"); ASCII digits only.
CRITERIA = re.compile(r"([0-9]+)/([0-9]+)")

log = logging.getLogger("hub_server.scheduler")


def failure(result: dict) -> str:
    """Why an rpc failed: its reason, else its output's last line, else the exit."""
    tail = (result.get("output_tail") or "").strip().splitlines()
    return (
        result.get("reason")
        or (tail[-1] if tail else "")
        or (f"exit {result.get('exit_code')}")
    )


def unknown_outcome(result: dict) -> bool:
    return result.get("exit_code") is None and bool(
        UNKNOWN_OUTCOME.match(result.get("reason") or "")
    )


@dataclass(frozen=True)
class Settings:
    """Hub settings (SQLite): the total cap and ▶ Run waves."""

    total_cap: int = 8
    waves: bool = False
    waves_start: str | None = None  # timestamp; None: from when it was turned on


@dataclass(frozen=True)
class Inflight:
    """An automatic RPC sent and not yet seen in a snapshot (its ticket still
    in `from_col`)."""

    devbox: str
    board_key: str
    nn: str
    method: str
    from_col: str
    at: datetime
    settled: bool = False  # it went through (a push: held while in human)


@dataclass(frozen=True)
class Action:
    method: str
    devbox: str
    board_key: str
    nn: str
    params: dict
    reason: str


@dataclass(frozen=True)
class Skip:
    """A candidate that waits, and why. nn None: the whole board (assist,
    archived, box offline, herdr down). `cause` is the reason without its live
    numbers (running counts, usage %), what the log dedups on."""

    board_key: str
    nn: str | None
    method: str | None
    reason: str
    cause: str = ""


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    stop: str | None = None  # why ▶ Run waves turns itself off now
    # Boxes this pass leaves out of the run (offline, herdr down), and why.
    left_out: list[tuple[str, str]] = field(default_factory=list)
    # PRs this pass found needing a person: (board_key, nn, PR number), held
    # while that PR stands.
    unmergeable: list[tuple[str, str, int | None]] = field(default_factory=list)
    # PRs "ready to merge" this pass, (board_key, nn, PR number): merged once
    # that held for STOP_CONFIRM.
    ready: list[tuple[str, str, int | None]] = field(default_factory=list)


def stamp(at: datetime) -> str:
    """A protocol timestamp: RFC 3339 UTC with a `Z` suffix."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


MAX_PARALLEL = 5  # per box, when no board says otherwise (box policy.json)
RUNNING = ("working", "review")  # as the daemon counts health.running
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def effort_rank(effort: str) -> int:
    """low before high; an effort the hub doesn't know goes last."""
    return EFFORTS.index(effort) if effort in EFFORTS else len(EFFORTS)


USAGE_GATE_PCT = 90
WINDOWS = (("five_hour", "5-hour"), ("seven_day", "7-day"))


@dataclass
class Box:
    """One devbox's room for automatic launches in this pass."""

    name: str
    running: int
    cap: int
    gated: str | None = None  # why the usage gate holds automatic launches
    gated_cause: str = ""  # the same without the usage %
    unknown: bool = False  # no usage data: one automatic launch at a time
    launching: int = 0  # automatic launches in flight or sent this pass
    used: float = 0.0  # the fuller window's %, for preferring headroom


def usage_gate(box: Box, usage: dict | None, gate: float, now: datetime):
    """Fill in the box's usage gate: any window at or above `gate` holds
    automatic launches until it resets; an expired window counts 0 %; no data
    is unknown. Stale usage is still used (board.md, Scheduler)."""
    usage = usage or {}
    windows = [(label, usage.get(key)) for key, label in WINDOWS]
    if usage.get("state") not in ("ok", "stale") or not any(w for _, w in windows):
        box.unknown, box.used = True, gate
        return
    for label, w in windows:
        if not w:
            continue
        resets = datetime.fromisoformat(w["resets_at"])
        pct = w["used_percentage"] if resets > now else 0
        box.used = max(box.used, pct)
        if pct >= gate and box.gated is None:
            until = f"until {w['resets_at']}"
            box.gated = (
                f"{box.name}'s {label} usage is {pct:g}% (gate {gate:g}%) {until}"
            )
            box.gated_cause = f"{box.name}'s {label} usage gate {until}"


def decide(
    boards: list[dict],
    devboxes: list[dict],
    settings: Settings,
    inflight: list[Inflight],
    cooldown: dict[tuple[str, str, str], datetime],
    now: datetime,
    given_up: dict[tuple[str, str], str] | None = None,
    dropped: set[str] | None = None,
    pending: set[str] | None = None,
    ready_since: dict[tuple[str, str, int | None], datetime] | None = None,
    unseen: set[str] | None = None,
) -> Plan:
    """What to send now, and why every other candidate waits. given_up: the
    tickets the scheduler stopped retrying (or found unmergeable), and why;
    they wait for the developer. dropped: the boxes left out of this ▶ Run
    waves run so far; a box offline or herdr down while it runs joins them
    (plan.left_out) and stays out, reconnected or not, until the next run.
    pending: boxes not known yet (online without a snapshot since they
    connected, or offline for less than the scheduler's grace): never
    dropped, never acted on, and waves don't stop while one is. ready_since:
    when each PR (board_key, nn, PR number) was first seen "ready to merge".
    unseen: online boxes that sent no snapshot within the grace: judged, as
    "no snapshot"."""
    given_up = given_up or {}
    pending = pending or set()
    ready_since = ready_since or {}
    plan = Plan()

    def cooling(key: str, nn: str, method: str) -> str | None:
        until = cooldown.get((key, nn, method))
        if until is None or until <= now:
            return None
        return f"the last {method} failed: retry after {stamp(until)}"

    statuses = {d["devbox"]: d for d in devboxes}
    busy = {(i.board_key, i.nn) for i in inflight}
    boxes: dict[str, Box] = {}
    offline_running = 0  # last known: an offline box's agents may still run
    for name, status in statuses.items():
        launching = sum(
            1 for i in inflight if i.devbox == name and i.method == "launch_ticket"
        )
        health = status.get("health") or {}
        if not status.get("online"):
            offline_running += health.get("running", 0) + launching
            continue
        policies = [b["policy"] for b in boards if b["devbox"] == name]
        caps = [p.get("max_parallel", MAX_PARALLEL) for p in policies]
        gates = [p.get("usage_gate_pct", USAGE_GATE_PCT) for p in policies]
        running = health.get("running", 0) + launching
        room = Box(name, running, min(caps, default=MAX_PARALLEL), launching=launching)
        usage_gate(room, health.get("usage"), min(gates, default=USAGE_GATE_PCT), now)
        boxes[name] = room
    total = sum(b.running for b in boxes.values()) + offline_running
    waves = waves_on(settings, now)
    held_by_gate = set()  # frontier tickets the usage gate holds
    out = set()
    if waves:
        out = left_out(
            boards,
            statuses,
            set(dropped or ()) - pending,
            pending,
            unseen or set(),
            plan,
        )
        for b in boards:
            if b["devbox"] in pending and not b["hub_meta"].get("archived"):
                plan.skipped.append(
                    Skip(b["key"], None, None, f"{b['devbox']} is not known yet")
                )
        out |= pending  # acted on by nothing, like the boxes left out

    candidates = []
    noted = set()  # boards skipped as a whole
    for b in boards:
        if b["devbox"] in out:
            continue  # logged once, as the box
        launches, unsuggested = frontier_launches(b)
        launches = [(nn, p) for nn, p in launches if (b["key"], nn) not in busy]
        if blocked := launch_block(b, statuses, waves):
            if launches or unsuggested:
                plan.skipped.append(Skip(b["key"], None, "launch_ticket", blocked))
                noted.add(b["key"])
            continue
        for nn, params in launches:
            why = given_up.get((b["key"], nn)) or cooling(b["key"], nn, "launch_ticket")
            if why:
                plan.skipped.append(Skip(b["key"], nn, "launch_ticket", why))
            else:
                candidates.append((b, nn, params))
        plan.skipped += [
            Skip(b["key"], s["nn"], "launch_ticket", s["reason"]) for s in unsuggested
        ]

    for b, nn, params in wave_order(boards, candidates, inflight, boxes):
        room = boxes[b["devbox"]]
        cause = ""
        if room.gated:
            reason, cause = room.gated, room.gated_cause
            held_by_gate.add((b["key"], nn))
        elif room.unknown and room.launching:
            reason = f"{room.name}'s usage is unknown: one automatic launch at a time"
        elif room.running >= room.cap:
            cause = f"{room.name} is at max_parallel"
            reason = f"{cause} ({room.running} running, cap {room.cap})"
        elif total >= settings.total_cap:
            cause = "the total cap is reached"
            offline = f", {offline_running} on offline boxes" if offline_running else ""
            reason = f"{cause} ({total} running{offline}, cap {settings.total_cap})"
        else:
            plan.actions.append(
                Action("launch_ticket", room.name, b["key"], nn, params, "frontier")
            )
            room.running += 1
            room.launching += 1
            total += 1
            continue
        plan.skipped.append(Skip(b["key"], nn, "launch_ticket", reason, cause))

    if waves:
        for b in boards:
            if b["devbox"] in out:
                continue
            merging = any(
                i.board_key == b["key"] and i.method == "merge_pr" for i in inflight
            )
            shippable = [
                t
                for t in sorted(b.get("tickets") or [], key=lambda t: int(t["nn"]))
                if (b["key"], t["nn"]) not in busy and t.get("col") in ("human", "pr")
            ]
            if shippable and (blocked := box_block(b, statuses)):
                if b["key"] not in noted:
                    plan.skipped.append(Skip(b["key"], None, None, blocked))
                continue
            for t in shippable:
                gave_up = given_up.get((b["key"], t["nn"]))
                if t["col"] == "human":
                    method, reason = waves_ship(b, t)
                    reason = gave_up or reason or cooling(b["key"], t["nn"], method)
                else:
                    method, reason = "merge_pr", gave_up or waves_merge(b, t)
                    if pr_state(t)[0] == "ready" and not gave_up:
                        ready = (b["key"], t["nn"], pr_number(t))
                        plan.ready.append(ready)
                        since = ready_since.get(ready)
                        if not reason and (since is None or now - since < STOP_CONFIRM):
                            reason = "not ready: confirming on the next export"
                    # A PR waiting out a failure doesn't hold the board's others.
                    reason = reason or cooling(b["key"], t["nn"], method)
                    if not reason and merging:
                        reason = "another merge on this board goes first"
                    merging = merging or not reason
                if reason == UNMERGEABLE and not gave_up:
                    plan.unmergeable.append((b["key"], t["nn"], pr_number(t)))
                if reason:
                    plan.skipped.append(Skip(b["key"], t["nn"], method, reason))
                else:
                    plan.actions.append(
                        Action(method, b["devbox"], b["key"], t["nn"], {}, "waves")
                    )
        unmergeable = {(k, nn): UNMERGEABLE for k, nn, _ in plan.unmergeable}
        in_run = [b for b in boards if b["devbox"] not in out]
        waiting = any(  # a box not known yet may still have work for this run
            b["devbox"] in pending
            for b in boards
            if not b["hub_meta"].get("archived") and armed(b["policy"], True)
        )
        if not waiting:
            ours = [i for i in inflight if i.devbox not in out]
            plan.stop = waves_stop(
                in_run, plan, ours, held_by_gate, given_up | unmergeable
            )
    return plan


def pr_number(ticket: dict) -> int | None:
    """The ticket's PR number: its pr's, else the "#N" of its note."""
    number = (ticket.get("pr") or {}).get("number")
    if isinstance(number, int):
        return number
    m = PR_NOTE.fullmatch((ticket.get("note") or "").strip())
    return int(m[1][1:]) if m else None


def left_out(boards, statuses, dropped, pending, unseen, plan: Plan) -> set[str]:
    """The boxes out of this run: those already dropped, and any box of a
    board the run schedules that is offline, sent no snapshot (unseen) or has
    herdr down now, judged from the box's status (a box not known yet is not
    judged)."""
    new = {}
    for b in boards:
        name = b["devbox"]
        if name in dropped or name in pending or b["hub_meta"].get("archived"):
            continue
        if not armed(b["policy"], True):
            continue
        status = statuses.get(name)
        if not status or not status.get("online"):
            new[name] = "offline"
        elif name in unseen:
            new[name] = "no snapshot"
        elif not ((status.get("health") or {}).get("herdr") or {}).get("ok"):
            new.setdefault(name, "herdr down")
    plan.left_out = [
        (name, f"box {name} {what} — left out of this run")
        for name, what in sorted(new.items())
    ]
    return dropped | set(new)


def waves_stop(boards, plan: Plan, inflight, held_by_gate, given_up) -> str | None:
    """Why ▶ Run waves is done, if it is. Never while it sends anything or
    has anything in flight (a push that went through excepted), nor while a
    started ticket on a board it schedules is still moving (see `moving`).
    Then: (a) no frontier left (a backlog ticket without a suggested
    model/effort, or one whose launch the scheduler gave up on, never launches:
    not frontier); (b) every frontier ticket is held by the usage gate."""
    if plan.actions or any(not i.settled for i in inflight):
        return None
    scheduled = [
        b
        for b in boards
        if not b["hub_meta"].get("archived") and armed(b["policy"], True)
    ]
    finished = {(i.board_key, i.nn) for i in inflight if i.settled} | set(given_up)
    if moving(scheduled, finished):
        return None
    frontier = {
        (b["key"], nn) for b in scheduled for nn, _ in frontier_launches(b)[0]
    } - set(given_up)
    if not frontier:
        return "nothing left to launch, ship or merge"
    if frontier <= held_by_gate:
        return "every remaining launch is held by the usage gate"
    return None


def moving(boards: list[dict], finished: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The started tickets ▶ Run waves still waits on: working or in review;
    in human with a PR or push it will send; a PR it will merge once ready
    (auto_merge). A ticket in human that policy won't ship, a PR that policy
    won't merge, needs and done have finished: they wait for a human. So have
    the `finished` ones: a push that went through, whatever its note says, and
    those the scheduler gave up on.

    A blocked ticket is not here: it can only be unblocked by a blocker that
    is moving, and that blocker keeps waves on by itself, on whichever
    scheduled board; a blocker on an unscheduled or archived board finishes
    only through a human. So no blocker (`NN`, or tk.sh's `<repo>:<NN>`) is
    ever looked up."""

    def going(board, t) -> bool:
        if (board["key"], t["nn"]) in finished:
            return False
        col, policy = t.get("col"), board["policy"]
        if col in RUNNING:
            return True
        if col == "human":
            return waves_ship(board, t)[1] is None
        if col == "pr":
            return bool(policy.get("night_shift") and policy.get("auto_merge")
                        and policy.get("forge") != "none"
                        and pr_state(t)[0] != "unmergeable")  # fmt: skip
        return False

    return {
        (b["key"], t["nn"])
        for b in boards
        for t in b.get("tickets") or []
        if going(b, t)
    }


def armed(policy: dict, waves: bool) -> bool:
    """Scheduled automatically: autonomy auto, or night_shift while ▶ Run waves."""
    return policy.get("autonomy") == "auto" or (
        waves and bool(policy.get("night_shift"))
    )


def waves_merge(board: dict, ticket: dict) -> str | None:
    """Why a ticket with a PR is not merged by ▶ Run waves. Ready is tk.sh's
    own verdict on the PR, the board's `pr` note "ready to merge" (the same
    gates tk.sh ready runs, re-exported every 20 s); merge_pr re-runs them."""
    policy = board["policy"]
    if not policy.get("night_shift"):
        return "night_shift is off for this board"
    if not policy.get("auto_merge"):
        return "auto_merge is off for this board"
    if policy.get("forge") == "none":
        return "forge is none: merge it on the forge"
    state, detail = pr_state(ticket)
    if state == "unmergeable":
        return UNMERGEABLE
    if state == "pending":
        return f"not ready: {detail}"
    return None


def pr_state(ticket: dict) -> tuple[str, str]:
    """("ready" | "pending" | "unmergeable", detail) from tk.sh's pr note,
    "#N <state>" (tk.sh board_rows: the first of draft, conflict, changes
    requested, checks failed, checks running, awaiting review, ready to
    merge). Pending resolves without a person: checks running, and a "ready
    to merge" whose PR's mergeable the forge hasn't computed yet (UNKNOWN,
    which tk.sh's gates fail as "ask again in a minute"). Anything else needs
    a person, a state this hub doesn't know included."""
    note = (ticket.get("note") or "").strip()
    m = PR_NOTE.fullmatch(note)
    number, state = (m[1], m[2]) if m else ("", note)
    if state == "ready to merge":
        if (ticket.get("pr") or {}).get("mergeable") == "UNKNOWN":
            return "pending", f"{number} mergeability not computed yet".strip()
        return "ready", note
    if state in PR_PENDING:
        return "pending", note
    return "unmergeable", note


def waves_ship(board: dict, ticket: dict):
    """(method, why not) for a ticket in human, by ▶ Run waves: a PR, or a
    push when forge is none, once its review passed. Client policy always
    wins."""
    policy = board["policy"]
    method = "ssh_push" if policy.get("forge") == "none" else "open_pr"
    if not policy.get("night_shift"):
        return method, "night_shift is off for this board"
    if policy.get("require_human_before_pr"):
        return method, "require_human_before_pr: a human opens the PR"
    if problem := review_problem(ticket.get("checks") or {}):
        return method, f"review not passed: {problem}"
    if method == "ssh_push":
        if not board.get("has_remote"):
            return method, "the repo has no remote: merge the branch locally"
        if (ticket.get("note") or "").startswith("pushed"):
            return method, "already pushed: merge it on the forge"
    return method, None


def review_problem(checks: dict) -> str | None:
    """What keeps a ticket in human from having passed review. A ticket
    reaches human whenever its agent is idle or done; its coordinator's checks
    (phase file line 2: tests=pass|fail|none criteria=m/n review=ok|N-blocking)
    say whether the review passed: review ok, tests pass (or none: no suite),
    every criterion met (m/m, m > 0). A check not reported has not passed."""
    review = checks.get("review")
    if not review:
        return "no review reported"
    if review != "ok":
        return f"review {review}"
    tests = checks.get("tests")
    if not tests:
        return "no tests reported"
    if tests not in ("pass", "none"):
        return f"tests {tests}"
    criteria = checks.get("criteria")
    if not criteria:
        return "no criteria reported"
    m = CRITERIA.fullmatch(criteria)
    if not m or int(m[2]) == 0 or int(m[1]) != int(m[2]):
        return f"criteria {criteria}"
    return None


def waves_on(settings: Settings, now: datetime) -> bool:
    """▶ Run waves is on and its start time has come."""
    if not settings.waves:
        return False
    return settings.waves_start is None or now >= datetime.fromisoformat(
        settings.waves_start
    )


def box_block(board: dict, statuses: dict[str, dict]) -> str | None:
    """Why nothing at all is sent to the board: archived, or its box offline."""
    status = statuses.get(board["devbox"])
    if board["hub_meta"].get("archived"):
        return "archived board"
    if board.get("stale") or not status or not status.get("online"):
        return f"{board['devbox']} is offline"
    return None


def launch_block(board: dict, statuses: dict[str, dict], waves: bool) -> str | None:
    """Why the board's frontier is not launched automatically. ▶ Run waves
    arms a board as auto only where its policy says night_shift."""
    if reason := box_block(board, statuses):
        return reason
    health = statuses[board["devbox"]].get("health") or {}
    if not (health.get("herdr") or {}).get("ok"):
        return f"herdr is down on {board['devbox']}"
    if not armed(board["policy"], waves):
        return "assist board: launches only on a click"
    return None


def wave_order(boards, candidates, inflight, boxes):
    """Round-robin across repos, so most projects are in flight: a repo's
    k-th candidate goes in round k + its tickets already running. Within a
    round: effort low to high, the box with more usage headroom, then board,
    then NN."""
    in_flight: dict[str, int] = {}
    keys = {b["key"]: b for b in boards}
    for b in boards:
        for t in b.get("tickets") or []:
            if t.get("col") in RUNNING:
                in_flight[b["repo"]] = in_flight.get(b["repo"], 0) + 1
    for i in inflight:
        if i.method == "launch_ticket" and i.board_key in keys:
            repo = keys[i.board_key]["repo"]
            in_flight[repo] = in_flight.get(repo, 0) + 1

    def within(c):
        b, nn, params = c
        used = boxes[b["devbox"]].used
        return (effort_rank(params["effort"]), used, b["key"], int(nn))

    by_repo: dict[str, list] = {}
    for c in sorted(candidates, key=within):
        by_repo.setdefault(c[0]["repo"], []).append(c)
    ranked = [
        (in_flight.get(repo, 0) + k, within(c), c)
        for repo, cs in by_repo.items()
        for k, c in enumerate(cs)
    ]
    return [c for *_, c in sorted(ranked, key=lambda r: r[:2])]


class Scheduler:
    """Runs `decide` on every state change the hub reports and on a tick,
    sends the RPCs it asks for through the hub, and logs every decision (to
    SQLite, the hub log and /ws/ui as scheduler_log). A skip is logged when its
    reason changes, not on every pass. Disabled, it still owns the settings."""

    def __init__(
        self,
        hub: Hub,
        enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.hub = hub
        self.enabled = enabled
        self.clock = clock
        self.inflight: dict[str, Inflight] = {}  # by rpc id
        self.cooldown: dict[tuple[str, str, str], datetime] = {}
        self.reasons: dict[tuple[str, str, str], str] = {}  # last skip logged
        # Per ticket, while it stays in the column they were counted in (kept
        # in SQLite: a restarted hub doesn't retry a merge that failed).
        self.retries = hub.store.retries()
        self._stopping: datetime | None = None  # first pass that saw plan.stop
        # Boxes left out of this waves run, kept in SQLite with the settings:
        # a restart neither clears them nor rebuilds them from offline flags.
        self.dropped: set[str] = set(hub.store.settings().get("waves_dropped", []))
        # When this scheduler first saw each box not known (offline, or
        # online without a snapshot or with a stale board): judged only after
        # DROP_GRACE (a hub restart, a daemon blip); a reconnect doesn't reset it.
        self.unknown_since: dict[str, datetime] = {}
        self.ready_since: dict[tuple[str, str, int | None], datetime] = {}
        self._dirty = False
        self._task: asyncio.Task | None = None
        self._early: dict[str, dict] | None = None
        if enabled:
            hub.on_change = self.kick
            hub.on_rpc_result = self.rpc_finished

    # -- settings ------------------------------------------------------------

    def unknown(self, boards: list[dict], statuses: list[dict], now):
        """(pending, unseen). A box is known while online, with a snapshot
        since it connected (or since the hub started) and no stale board.
        pending: not known for less than DROP_GRACE, as this scheduler saw it;
        unseen: online but not known for longer (a daemon whose snapshots the
        hub refuses, one that keeps reconnecting)."""
        online = {d["devbox"] for d in statuses if d.get("online")}
        stale = {b["devbox"] for b in boards if b.get("stale")}
        known = (online & self.hub.snapshotted) - stale
        for name in {d["devbox"] for d in statuses} | {b["devbox"] for b in boards}:
            if name in known:
                self.unknown_since.pop(name, None)
            else:
                self.unknown_since.setdefault(name, now)
        pending = {
            name
            for name, since in self.unknown_since.items()
            if now - since < DROP_GRACE
        }
        return pending, (online - known) - pending

    def settings(self) -> Settings:
        known = Settings.__dataclass_fields__
        stored = self.hub.store.settings()
        return Settings(**{k: v for k, v in stored.items() if k in known})

    async def update_settings(self, **values) -> dict:
        """Store, broadcast hub_settings, and run a pass on the new settings."""
        if values.get("waves") is False:
            values["waves_start"] = None
        if values.get("waves") is True and not self.settings().waves:
            self.dropped = set()  # a new run: every box is back in
            values["waves_dropped"] = []
        self.hub.store.update_settings(values)
        current = asdict(self.settings())
        await self.hub.broadcast({"type": "hub_settings", "settings": current})
        self.kick()
        return current

    # -- passes --------------------------------------------------------------

    def kick(self):
        """Run a pass soon; one at a time, and one more if kicked meanwhile."""
        if not self.enabled:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._drain())

    async def _drain(self):
        while self._dirty:
            self._dirty = False
            try:
                await self.run_once()
            except Exception:
                log.exception("scheduler pass failed")

    async def ticking(self, seconds: float = TICK):
        while True:
            self.kick()
            await asyncio.sleep(seconds)

    async def run_once(self):
        now = self.clock()
        boards = self.hub.store.boards.all()
        self._prune(boards, now)
        settings = self.settings()
        statuses = self.hub.store.devbox_statuses()
        pending, unseen = self.unknown(boards, statuses, now)
        plan = decide(
            boards,
            statuses,
            settings,
            list(self.inflight.values()),
            self.cooldown,
            now,
            {k: r["given_up"] for k, r in self.retries.items() if r["given_up"]},
            self.dropped,
            pending,
            self.ready_since,
            unseen,
        )
        self.ready_since = {k: self.ready_since.get(k, now) for k in plan.ready}
        for name, why in plan.left_out:
            self.dropped.add(name)
            self.hub.store.update_settings({"waves_dropped": sorted(self.dropped)})
            await self.log("skipped", why, devbox=name)
        for board_key, nn, number in plan.unmergeable:  # held while that PR stands
            retry = {"col": "pr", "failed": 0, "given_up": UNMERGEABLE, "pr": number}
            self.hub.store.put_retry(board_key, nn, retry)
            self.retries[(board_key, nn)] = retry
        for action in plan.actions:
            await self._send(action, now)
        seen = set()
        for skip in plan.skipped:
            key = (skip.board_key, skip.nn, skip.method)
            retry = self.retries.get((skip.board_key, skip.nn))
            if retry and skip.reason == retry["given_up"]:
                key = (skip.board_key, skip.nn, None)  # held: once, whatever the column
            seen.add(key)
            cause = skip.cause or skip.reason
            if self.reasons.get(key) != cause:
                self.reasons[key] = cause
                await self.log("skipped", skip.reason, skip.method,
                               skip.board_key.split("/")[0], skip.board_key, skip.nn)  # fmt: skip
        self.reasons = {k: v for k, v in self.reasons.items() if k in seen}
        if not plan.stop:
            self._stopping = None
            return
        if self._stopping is None:
            self._stopping = now
        # Stop on a condition that held across an export cycle, and only on
        # the settings this pass decided on: a developer who changed them
        # meanwhile (a new start, say) wins.
        if now - self._stopping < STOP_CONFIRM:
            return
        self._stopping = None
        if self.settings() == settings:
            await self.log("waves_off", plan.stop)
            await self.update_settings(waves=False)

    def _prune(self, boards: list[dict], now: datetime):
        """Release automatic RPCs whose ticket left the column they act on, or
        that outlived INFLIGHT_TTL; a push that went through holds its ticket
        for as long as it stays in human."""
        cols = {
            (b["key"], t["nn"]): t.get("col")
            for b in boards
            for t in b.get("tickets") or []
        }
        self.inflight = {
            rpc_id: i
            for rpc_id, i in self.inflight.items()
            if cols.get((i.board_key, i.nn)) == i.from_col
            and (i.settled or now - i.at < INFLIGHT_TTL)
        }
        self.cooldown = {k: until for k, until in self.cooldown.items() if until > now}
        numbers = {
            (b["key"], t["nn"]): pr_number(t)
            for b in boards
            for t in b.get("tickets") or []
        }
        # A ticket that changed column starts over. A hold on a PR stands
        # while that PR does: through pr -> human -> pr (tk.sh shows human
        # when the forge can't be read), until another column or PR number.
        for key, retry in list(self.retries.items()):
            col = cols.get(key)
            if retry["col"] == "pr":
                now_pr, held_pr = numbers.get(key), retry.get("pr")
                differs = None not in (now_pr, held_pr) and now_pr != held_pr
                keep = col == "human" or (col == "pr" and not differs)
            else:
                keep = col == retry["col"]
            if not keep:
                self.hub.store.drop_retry(*key)  # the store first: it can fail
                del self.retries[key]

    async def _send(self, a: Action, now: datetime):
        self._early = {}  # results that beat ticket_action's return
        try:
            sent = await self.hub.ticket_action(
                a.devbox, a.board_key, a.nn, a.method, a.params
            )
        except DevboxOffline:
            await self.log("skipped", f"{a.devbox} went offline", a.method,
                           a.devbox, a.board_key, a.nn)  # fmt: skip
            return
        except BadAction as e:  # a suggestion the schema refuses: definite
            inflight = Inflight(
                a.devbox, a.board_key, a.nn, a.method, FROM_COL[a.method], now
            )
            self.cooldown[(a.board_key, a.nn, a.method)] = now + COOLDOWN
            try:  # the record first: a store error mustn't lose it
                await self.log("failed", str(e), a.method, a.devbox, a.board_key, a.nn)
            finally:
                self._count_failure(inflight)
            return
        finally:
            early, self._early = self._early, None
        self.inflight[sent["id"]] = Inflight(
            a.devbox, a.board_key, a.nn, a.method, FROM_COL[a.method], now
        )
        # A result can beat ticket_action's return (a send that broke, a fast
        # daemon): it is settled like any other, and a failure is not "sent".
        result = early.get(sent["id"])
        if result is None or result.get("ok"):
            await self.log("sent", a.reason, a.method, a.devbox, a.board_key, a.nn)
        if result is not None and (logging := self._settle(sent["id"], result)):
            await logging

    def rpc_finished(self, result: dict):
        """A push that went through holds its ticket while it stays in human.
        A failed automatic RPC cools down; a definite failure (a refusal, a
        non-zero exit) frees its ticket now, an unknown outcome (timed out,
        cut off by a disconnect or reconnect: a launch may still be running)
        keeps holding it until a snapshot shows it moving or INFLIGHT_TTL, and
        is not counted as an attempt. A definite failure of a merge leaves the
        ticket to the developer at once, of a PR or push the second time."""
        rpc_id = result.get("id")
        if rpc_id not in self.inflight:
            if self._early is not None:
                self._early[rpc_id] = result
            return
        if logging := self._settle(rpc_id, result):
            self.hub.spawn(logging)

    def _settle(self, rpc_id: str, result: dict):
        """Apply an automatic RPC's result; returns the log of a failure, to
        await or spawn."""
        inflight = self.inflight[rpc_id]
        if result.get("ok"):
            if inflight.method == "ssh_push":
                self.inflight[rpc_id] = replace(inflight, settled=True)
            return None
        key = (inflight.board_key, inflight.nn, inflight.method)
        self.cooldown[key] = self.clock() + COOLDOWN
        why = failure(result)
        definite = not unknown_outcome(result)
        if definite:
            del self.inflight[rpc_id]
        else:
            why = f"outcome unknown: {why}"

        async def record():  # the log entry first: a store error mustn't lose it
            try:
                await self.log("failed", why, inflight.method, inflight.devbox,
                               inflight.board_key, inflight.nn)  # fmt: skip
            finally:
                if definite:
                    self._count_failure(inflight)

        return record()

    def _count_failure(self, i: Inflight):
        """A definite failure: a merge is not retried; a launch, PR or push
        once. Kept per ticket, in SQLite, while it stays in that column."""
        ticket = (i.board_key, i.nn)
        retry = self.retries.get(ticket)
        failed = (retry["failed"] if retry else 0) + 1
        if i.method == "merge_pr":
            given_up = "merge failed — waiting for developer"
        elif failed >= MAX_ATTEMPTS:
            given_up = f"{WHAT[i.method]} failed twice — waiting for developer"
        else:
            given_up = None
        retry = {"col": i.from_col, "failed": failed, "given_up": given_up}
        if i.from_col == "pr":  # held while that PR stands
            board = self.hub.store.boards.get(i.board_key) or {}
            t = next((t for t in board.get("tickets") or [] if t["nn"] == i.nn), {})
            retry["pr"] = pr_number(t)
        self.hub.store.put_retry(*ticket, retry)
        self.retries[ticket] = retry

    async def log(self, action, reason, method=None, devbox=None, board_key=None,
                  nn=None):  # fmt: skip
        entry = self.hub.store.log_decision({
            "at": stamp(self.clock()), "action": action, "method": method,
            "devbox": devbox, "board_key": board_key, "nn": nn, "reason": reason,
        })  # fmt: skip
        log.info("%s %s %s %s: %s", action, method or "-", board_key or "-",
                 nn or "-", reason)  # fmt: skip
        await self.hub.broadcast({"type": "scheduler_log", "entry": entry})
