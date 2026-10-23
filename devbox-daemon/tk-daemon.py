#!/usr/bin/env python3
"""tk-daemon — reports every ticket-skill board on this devbox to the tk-hub, live,
and runs the hub's RPCs from a fixed list (board.md, Network & auth / Protocol).

Connects outbound to wss://<hub>/ws/daemon: to the hub's tailnet IP, with TLS
verified against its MagicDNS name, with a bearer token read by `op read
<token_ref>` and held in memory only (never written, never logged). Sends hello,
then a full snapshot; re-exports every `export_interval_s` and after each RPC,
sending only boards whose hash changed (and vanished boards as `removed`).
Reconnects with exponential backoff and jitter.

~/.config/tk-daemon/config.json:
  {"url": "wss://tk-hub.<tailnet>.ts.net/ws/daemon"   (https:// and no path also work)
   "ip": "100.x.y.z", "token_ref": "op://<vault>/<item>/<field>",
   optional: "devbox" (default: short hostname), "export_interval_s" (20),
   "backoff_initial_s" (1), "backoff_max_s" (60), "rpc_timeout_s" (120),
   "rpc_synthesis_timeout_s" (600), "hello_timeout_s" (30), "export_timeout_s" (60),
   "export_call_timeout_s" (30), "launch_lock_wait_s" (5), "launch_timeout_s" (900),
   "reservation_ttl_s" (120), "shutdown_grace_s" (60),
   "schema" (../protocol/schema.json)}
Health and every board are checked against the schema before sending: a board
that fails it is failed (keeps its last copy), never sent.
export_call_timeout_s bounds each tk.sh / herdr call inside tk-export.sh (a hung
board fails alone). tk-export.sh also gets a deadline a little under
export_timeout_s: boards not exported by then fail (keeping their last copies)
and the snapshot still arrives; export_timeout_s kills the whole export, as a
backstop. When the export before hello fails, the daemon connects anyway, with
the last health it knew (or herdr not ok, usage unknown), and sends the full
snapshot as soon as an export succeeds.
rescan_boards waits for an export already running at most rpc_timeout_s -
export_timeout_s, so it has time for its own.
The box policy, ~/.config/tk-daemon/policy.json, is read by tk-export.sh, and
here for its max_parallel (5).
RPCs: rescan_boards; launch_ticket, say_agent, pane_tail run tk.sh launch / say
/ show with the board's main checkout as cwd. An RPC whose params fail the
schema runs nothing. launch_ticket is refused while `herdr status server` is not
running, and, unless manual, when the box already runs max_parallel tickets
(working/review, plus launches not yet exported); a manual launch past the cap
goes, flagged in data.over_cap. The daemon's own launches on one board run one
after the other (an in-process lock per board: ▶ Frontier sends them all at
once), each in a task of its own that rpc_timeout_s does not cancel. In turn, a
launch takes the board's flock (<board>/.board.lock), waiting at most
launch_lock_wait_s for another process holding it (else refused "board busy",
nothing run), re-checks with tk.sh export (bounded by export_call_timeout_s)
that the ticket is still backlog, and runs tk.sh launch under launch_timeout_s
only (a launch killed midway leaves a worktree and agent with no run recorded).
A launch that outlives rpc_timeout_s, queued or running, is reported to the hub
as "timed out" yet carries on: its outcome is logged at warning level and the
re-export after the next RPC or tick shows it. ticket-skill's tk.sh does not
take the flock, so a board's own session is not kept out. A launch holds a slot
against max_parallel until an export shows the ticket out of backlog, or
reservation_ttl_s after tk.sh launch returned. say_agent's message goes through
a temp file; tk.sh exit 20 (at a dialog) and 21 (no agent) come back as reasons. pane_tail returns the lines in
data.pane and, being read-only, is not followed by a re-export.
SIGTERM / SIGINT stop the daemon (closing the connection with 1001) and kill a
running export; launches still queued then refuse ("daemon stopping") without
running tk.sh, and those already in it get shutdown_grace_s to finish before
they are killed, so systemd's TimeoutStopSec must exceed it (the default 90 s
does, for the default 60 s).
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
import typing
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

DAEMON_VERSION = "0.1.0"
PROTOCOL_VERSION = "1.0"
EXPORT = Path(__file__).resolve().with_name("tk-export.sh")
SCHEMA = Path(__file__).resolve().parents[1] / "protocol" / "schema.json"
DEFAULTS = {
    "export_interval_s": 20,
    "backoff_initial_s": 1,
    "backoff_max_s": 60,
    "rpc_timeout_s": 120,
    "rpc_synthesis_timeout_s": 600,
    "hello_timeout_s": 30,
    "export_timeout_s": 60,
    "export_call_timeout_s": 30,
    "launch_lock_wait_s": 5,
    "launch_timeout_s": 900,
    "reservation_ttl_s": 120,
    "shutdown_grace_s": 60,
}
SYNTHESIS_RPCS = frozenset({"refine_with_comment", "refresh_board_brief", "run_retro"})
FAILED = re.compile(r"^tk-export: failed (\S+)(?: (/.*))?$", re.MULTILINE)
RESUME = re.compile(r"^tk-export: resume ([0-9]+)$", re.MULTILINE)
TAIL = 4000  # characters of output_tail
PANE = 200_000  # characters of a pane_tail's data.pane
# RPCs that change nothing: no re-export after them (pane_tail is polled).
READ_ONLY_RPCS = frozenset({"rescan_boards", "pane_tail"})
MAX_PARALLEL = 5  # running tickets per box, unless the box policy says otherwise
RUNNING = ("working", "review")  # as tk-export.sh counts health.running
# tk.sh say / show exit codes, as the UI shows them.
AGENT_EXITS = {
    20: "dialog: the agent is at a dialog or not at its prompt; answer it in its herdr tab",
    21: "no agent: the ticket's agent isn't running",
}

log = logging.getLogger("tk-daemon")


class Refused(Exception):
    pass


class RpcRefused(Exception):
    """An RPC the daemon will not run: reported as ok false, exit_code null."""


def tk_sh() -> str:
    """tk.sh as tk-export.sh finds it: $TK_SH, tk.sh on PATH, the installed skill's."""
    if os.environ.get("TK_SH"):
        return os.environ["TK_SH"]
    if shutil.which("tk.sh"):
        return "tk.sh"
    return str(Path.home() / ".claude/skills/implement-tickets/scripts/tk.sh")


class Schema:
    """protocol/schema.json, checked with the keywords it uses (draft 2020-12:
    $ref, type, enum, const, required, properties, additionalProperties, items,
    allOf/anyOf/oneOf, if/then/else, min/max, minLength/maxLength, minItems,
    minProperties, pattern). Runtime dependencies stay websockets only
    (board.md, Shape); tests check it agrees with jsonschema on every fixture."""

    TYPES: typing.ClassVar[dict] = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "integer": lambda v: (
            (isinstance(v, int) and not isinstance(v, bool))
            or (isinstance(v, float) and v.is_integer())
        ),
    }

    def __init__(self, schema: dict):
        self.root = schema

    def errors(self, inst, ref: str | None = None) -> list[str]:
        """Errors of `inst` against the whole schema, or its `$defs/<ref>`."""
        out: list[str] = []
        self._check(inst, self.root["$defs"][ref] if ref else self.root, "", out)
        return out

    def _ok(self, inst, sch) -> bool:
        out: list[str] = []
        self._check(inst, sch, "", out)
        return not out

    @staticmethod
    def _same(a, b) -> bool:  # JSON equality: true is not 1
        if isinstance(a, bool) or isinstance(b, bool):
            return type(a) is type(b) and a == b
        return a == b

    def _check(self, inst, sch, at: str, out: list[str]):
        if sch is False:
            out.append(f"{at or '/'}: not allowed")
            return
        if not isinstance(sch, dict):  # true
            return
        if "$ref" in sch:
            node = self.root
            for part in sch["$ref"].removeprefix("#/").split("/"):
                node = node[part]
            self._check(inst, node, at, out)
        if "type" in sch:
            types = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
            if not any(self.TYPES[t](inst) for t in types):
                out.append(f"{at or '/'}: not {'/'.join(types)}")
                return
        if "enum" in sch and not any(self._same(inst, e) for e in sch["enum"]):
            out.append(f"{at or '/'}: not one of {sch['enum']}")
        if "const" in sch and not self._same(inst, sch["const"]):
            out.append(f"{at or '/'}: not {sch['const']!r}")
        if self.TYPES["number"](inst):
            if "minimum" in sch and inst < sch["minimum"]:
                out.append(f"{at}: below {sch['minimum']}")
            if "maximum" in sch and inst > sch["maximum"]:
                out.append(f"{at}: above {sch['maximum']}")
        if isinstance(inst, str):
            if len(inst) < sch.get("minLength", 0):
                out.append(f"{at}: shorter than {sch['minLength']}")
            if "maxLength" in sch and len(inst) > sch["maxLength"]:
                out.append(f"{at}: longer than {sch['maxLength']}")
            if "pattern" in sch and not re.search(sch["pattern"], inst):
                out.append(f"{at}: does not match {sch['pattern']}")
        if isinstance(inst, list):
            if len(inst) < sch.get("minItems", 0):
                out.append(f"{at}: fewer than {sch['minItems']} items")
            if "items" in sch:
                for i, v in enumerate(inst):
                    self._check(v, sch["items"], f"{at}/{i}", out)
        if isinstance(inst, dict):
            if len(inst) < sch.get("minProperties", 0):
                out.append(f"{at or '/'}: fewer than {sch['minProperties']} keys")
            for k in sch.get("required", []):
                if k not in inst:
                    out.append(f"{at or '/'}: missing {k}")
            props = sch.get("properties", {})
            extra = sch.get("additionalProperties", True)
            for k, v in inst.items():
                if k in props:
                    self._check(v, props[k], f"{at}/{k}", out)
                elif extra is False:
                    out.append(f"{at}/{k}: not allowed")
                elif isinstance(extra, dict):
                    self._check(v, extra, f"{at}/{k}", out)
        for sub in sch.get("allOf", []):
            self._check(inst, sub, at, out)
        if "anyOf" in sch and not any(self._ok(inst, x) for x in sch["anyOf"]):
            out.append(f"{at or '/'}: matches none of anyOf")
        if "oneOf" in sch and sum(self._ok(inst, x) for x in sch["oneOf"]) != 1:
            out.append(f"{at or '/'}: does not match exactly one of oneOf")
        if "if" in sch:
            branch = sch.get("then" if self._ok(inst, sch["if"]) else "else")
            if branch is not None:
                self._check(inst, branch, at, out)


def load_config(home: Path) -> dict:
    path = home / ".config" / "tk-daemon" / "config.json"
    cfg = {**DEFAULTS, **json.loads(path.read_text())}
    missing = [k for k in ("url", "ip", "token_ref") if not cfg.get(k)]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    cfg.setdefault("devbox", socket.gethostname().split(".")[0])
    return cfg


def endpoint(url: str) -> tuple[str, str, int]:
    """(wss URI, hub name, port) from the configured hub url."""
    u = urlsplit(url)
    if u.scheme not in ("wss", "https") or not u.hostname:
        raise ValueError(f"hub url must be wss:// or https://, got {url!r}")
    path = u.path if u.path not in ("", "/") else "/ws/daemon"
    port = u.port or 443
    return f"wss://{u.hostname}:{port}{path}", u.hostname, port


def kill_session(sid: int):
    """SIGKILL every process in session `sid`: its process group, and the groups
    `timeout` makes for the calls it runs (Linux /proc)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(sid, signal.SIGKILL)
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        with contextlib.suppress(OSError):  # gone meanwhile, or not ours
            if os.getsid(int(name)) == sid:
                os.kill(int(name), signal.SIGKILL)


async def board_lock(board_path: str, wait: float) -> typing.IO:
    """flock on <board>/.board.lock, taken within `wait` seconds or refused as
    board busy; closing the returned file releases it. Only drivers that take it
    are serialised: the daemon's own launches. ticket-skill's tk.sh never takes
    it, so a board's own session is not kept out (the re-check under the lock
    narrows that race, it does not close it)."""
    f = open(Path(board_path) / ".board.lock", "a")  # noqa: ASYNC230, SIM115
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RpcRefused(
                        f"board busy: another launch holds {f.name} past {wait:g}s"
                    ) from None
                await asyncio.sleep(0.1)
    except BaseException:
        f.close()
        raise


async def run_cmd(
    argv: list[str], env: dict | None = None, cwd: str | None = None
) -> tuple[int, str, str]:
    """Run a command in its own session; cancelling (a timeout, SIGTERM) kills
    everything in that session."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        kill_session(proc.pid)
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.uri, self.name, self.port = endpoint(cfg["url"])
        self.token: str | None = None
        self.sent: dict[str, dict] = {}  # board key -> the board last sent
        self.health: dict | None = None
        self.need_full = True  # the next snapshot must be full (a new connection)
        self.export_start = 0  # where the next export starts (tk-export: resume)
        self.schema = Schema(json.loads(Path(cfg.get("schema", SCHEMA)).read_text()))
        self.lock = asyncio.Lock()
        # Launches take a slot under launch_lock, before tk.sh runs; the slot is
        # held (reserved) until a snapshot shows the ticket out of backlog, or
        # reservation_ttl_s after tk.sh launch returned.
        self.launch_lock = asyncio.Lock()
        # (board key, nn) -> monotonic time tk.sh launch returned; None: running
        self.reserved: dict[tuple[str, str], float | None] = {}
        self.launches: set[asyncio.Task] = set()  # launches running, for shutdown
        self.board_locks: dict[str, asyncio.Lock] = {}  # one launch per board at once
        self.stopping = False  # set on SIGTERM / SIGINT: queued launches never start
        self.rpcs = {
            "rescan_boards": self.rpc_rescan_boards,
            "launch_ticket": self.rpc_launch_ticket,
            "say_agent": self.rpc_say_agent,
            "pane_tail": self.rpc_pane_tail,
        }

    # ---- export --------------------------------------------------------------------
    async def export(self) -> tuple[dict, set[str]]:
        env = {
            **os.environ,
            "TK_DEVBOX": self.cfg["devbox"],
            "TK_DAEMON_VERSION": DAEMON_VERSION,
            "TK_EXPORT_CALL_TIMEOUT": f"{self.cfg['export_call_timeout_s']:g}",
            "TK_EXPORT_START": str(self.export_start),
        }
        timeout = self.cfg["export_timeout_s"]
        # Boards left at the deadline fail alone, before the backstop kills it all.
        env["TK_EXPORT_DEADLINE"] = (
            f"{time.time() + timeout - min(5, timeout * 0.2):.3f}"
        )
        try:  # a hung gh/az/herdr must not hold the lock or block connecting
            rc, out, err = await asyncio.wait_for(
                run_cmd([str(EXPORT)], env=env), timeout
            )
        except TimeoutError:
            raise RuntimeError(f"tk-export.sh timed out after {timeout:g}s") from None
        for line in err.splitlines():
            log.warning("%s", line)
        if rc != 0:
            raise RuntimeError(f"tk-export.sh exited {rc}")
        resume = RESUME.findall(err)
        self.export_start = int(resume[-1]) if resume else 0
        # A failed line's key is tk-export's guess; its dir, when it matches the
        # board_path last sent, gives the real key.
        sent_dirs = {
            os.path.realpath(b["board_path"]): k
            for k, b in self.sent.items()
            if isinstance(b.get("board_path"), str)
        }
        failed = set()
        for key, path in FAILED.findall(err):
            failed.add(sent_dirs.get(os.path.realpath(path), key) if path else key)
        snap = json.loads(out)
        if errors := self.schema.errors(snap.get("health"), "health"):
            log.error("export: health fails the schema (%s)", "; ".join(errors[:3]))
            snap["health"] = self.fallback_health("health failed the schema")
        # A board the hub would reject fails alone: it keeps its last copy.
        boards = []
        for b in snap.get("boards") or []:
            if not (errors := self.schema.errors(b, "board")):
                boards.append(b)
                continue
            key = b.get("key") if isinstance(b, dict) else None
            log.error(
                "export: board %s fails the schema (%s)", key, "; ".join(errors[:3])
            )
            if isinstance(key, str):
                failed.add(key)
        snap["boards"] = boards
        return snap, failed

    async def sync(
        self, ws, full: bool, lock_wait: float | None = None
    ) -> tuple[int, list[str], list[str]]:
        """Export, then send a snapshot: every board when `full`, else the boards
        whose hash changed (nothing at all when neither boards nor health did).
        A board whose export failed keeps its last copy. One with no copy to keep
        (failed since this daemon started) would be missing from a full snapshot,
        and the hub would remove it: then a delta goes instead, and the next sync
        tries the full one again. Returns (boards, removed, held back: the failed
        boards that kept a full snapshot from going).
        `lock_wait`: at most this long waiting for an export already running."""
        if lock_wait is None:
            await self.lock.acquire()
        else:
            try:
                await asyncio.wait_for(self.lock.acquire(), lock_wait)
            except TimeoutError:
                raise RuntimeError(
                    f"an export already running held the lock past {lock_wait:g}s"
                ) from None
        try:
            snap, failed = await self.export()
            boards = {b["key"]: b for b in snap["boards"]}
            for key in failed & self.sent.keys():
                boards[key] = self.sent[key]
            removed = sorted(self.sent.keys() - boards.keys())
            held = sorted(failed - boards.keys())
            want_full = full or self.need_full
            full = want_full and not held
            changed = [
                b
                for k, b in boards.items()
                if full or self.sent.get(k, {}).get("hash") != b["hash"]
            ]
            if full or changed or removed or snap["health"] != self.health:
                msg = {
                    "type": "snapshot",
                    "protocol_version": PROTOCOL_VERSION,
                    "devbox": self.cfg["devbox"],
                    "full": full,
                    "health": snap["health"],
                    "boards": sorted(changed, key=lambda b: b["key"]),
                    "removed": removed,
                }
                if errors := self.schema.errors(msg):  # the hub would drop it all
                    raise RuntimeError(
                        f"snapshot fails the schema: {'; '.join(errors[:3])}"
                    )
                await ws.send(json.dumps(msg))
                log.info(
                    "sent %s snapshot: %d boards, %d removed",
                    "full" if full else "delta",
                    len(changed),
                    len(removed),
                )
            if held and want_full:
                log.warning(
                    "full snapshot held back: no copy of failed %s", " ".join(held)
                )
            self.sent, self.health = boards, snap["health"]
            self.expire_reservations()
            self.need_full = want_full and not full
            return len(boards), removed, held
        finally:
            self.lock.release()

    # ---- RPCs ----------------------------------------------------------------------
    async def rpc_rescan_boards(self, ws, params: dict) -> dict:
        budget = self.cfg["rpc_timeout_s"] - self.cfg["export_timeout_s"]
        # No budget (rpc_timeout_s <= export_timeout_s): the RPC timeout alone bounds it.
        n, removed, held = await self.sync(
            ws, full=True, lock_wait=budget if budget > 0 else None
        )
        tail = f"rescanned: {n} boards" + (
            f", removed {' '.join(removed)}" if removed else ""
        )
        if held:
            tail += (
                f"; sent as a delta, not full: no copy yet of failed {' '.join(held)}"
            )
        return {"ok": True, "exit_code": 0, "output_tail": tail}

    def board(self, params: dict) -> dict:
        """The board an RPC names, as last exported."""
        board = self.sent.get(params.get("board_key"))
        if board is None:
            raise RpcRefused(f"unknown board {params.get('board_key')!r} on this box")
        return board

    async def tk(self, board: dict, *args: str) -> tuple[int, str]:
        """tk.sh <verb> <board path> ... from the board's main checkout: (exit, output)."""
        verb, *rest = args
        rc, out, err = await run_cmd(
            [tk_sh(), verb, board["board_path"], *rest], cwd=board["root"]
        )
        return rc, out + err

    async def col(self, board: dict, nn: str) -> str:
        """The ticket's column now: `tk.sh export`, not the last snapshot; bounded
        by export_call_timeout_s, as tk-export.sh bounds it."""
        limit = self.cfg["export_call_timeout_s"]
        try:
            rc, out, err = await asyncio.wait_for(
                run_cmd([tk_sh(), "export", board["board_path"]], cwd=board["root"]),
                limit,
            )
        except TimeoutError:
            raise RpcRefused(f"tk.sh export timed out after {limit:g}s") from None
        if rc != 0:
            raise RpcRefused(f"tk.sh export failed (exit {rc}): {err.strip()[-200:]}")
        tickets = json.loads(out).get("tickets") or []
        found = next((t for t in tickets if t.get("nn") == nn), None)
        if found is None:
            raise RpcRefused(f"no ticket {nn} on board {board['key']}")
        return found.get("col")

    def col_sent(self, key: str, nn: str) -> str | None:
        tickets = self.sent.get(key, {}).get("tickets") or []
        return next((t.get("col") for t in tickets if t.get("nn") == nn), None)

    def expire_reservations(self):
        """Drop the slots whose ticket the last export shows out of backlog, and
        those whose tk.sh launch returned over reservation_ttl_s ago (a ticket
        that never left backlog must not hold a slot forever). A ticket missing
        from the export (its board failed, say) keeps its slot until the TTL."""
        now, ttl = time.monotonic(), self.cfg["reservation_ttl_s"]
        self.reserved = {
            slot: t
            for slot, t in self.reserved.items()
            if self.col_sent(*slot) in ("backlog", None)
            and (t is None or now - t < ttl)
        }

    def running(self) -> int:
        """Tickets running on the box: working or review in the last export, plus
        launches not yet seen out of backlog."""
        busy = {
            (key, t.get("nn"))
            for key, b in self.sent.items()
            for t in b.get("tickets") or []
            if t.get("col") in RUNNING
        }
        self.expire_reservations()
        return len(busy | self.reserved.keys())

    def max_parallel(self) -> int:
        """max_parallel from the box policy, ~/.config/tk-daemon/policy.json (5)."""
        path = Path.home() / ".config" / "tk-daemon" / "policy.json"
        try:
            cap = json.loads(path.read_text()).get("max_parallel", MAX_PARALLEL)
        except FileNotFoundError:
            return MAX_PARALLEL
        except (OSError, ValueError, AttributeError) as e:
            log.warning("ignoring %s: %s", path, e)
            return MAX_PARALLEL
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            log.warning("ignoring max_parallel %r in %s", cap, path)
            return MAX_PARALLEL
        return cap

    async def herdr_up(self):
        """`herdr status server`, asked now: no launches while herdr is down."""
        try:
            rc, out, err = await asyncio.wait_for(
                run_cmd(["herdr", "status", "server"]),
                self.cfg["export_call_timeout_s"],
            )
        except TimeoutError:
            raise RpcRefused("herdr is down: herdr status server timed out") from None
        except OSError as e:
            raise RpcRefused(f"herdr is down: {e}") from None
        if rc != 0 or not re.search(r"^status: running", out, re.MULTILINE):
            why = (err or out).strip().splitlines()
            raise RpcRefused(f"herdr is down: {why[-1] if why else f'exit {rc}'}")

    async def rpc_launch_ticket(self, ws, params: dict) -> dict:
        """tk.sh launch, refused while herdr is down and, unless `manual`, at the
        box's max_parallel; a manual launch past it goes, flagged as over_cap."""
        board, nn = self.board(params), params["nn"]
        slot = (board["key"], nn)
        await self.herdr_up()
        async with self.launch_lock:
            self.expire_reservations()
            if slot in self.reserved:
                raise RpcRefused(f"ticket {nn} is already launching")
            running, cap = self.running(), self.max_parallel()
            over = running >= cap
            if over and not params.get("manual"):
                raise RpcRefused(
                    f"the box is at max_parallel: {running} running, cap {cap}"
                )
            self.reserved[slot] = None
        # The queue, the locks, the re-check and tk.sh launch run in a task of
        # their own: the rpc timeout answers the hub but never cancels a launch
        # waiting its turn or running.
        task = asyncio.create_task(self.launch_locked(board, nn, params))
        self.launches.add(task)
        task.add_done_callback(self.launches.discard)
        try:
            rc, out = await asyncio.shield(task)
        except asyncio.CancelledError:
            log.warning("launch %s %s outlived its rpc; it carries on", *slot)
            task.add_done_callback(lambda t: self.log_orphan(slot, t))
            raise
        result = {"ok": rc in (0, 3), "exit_code": rc, "output_tail": out}
        if over:
            result["data"] = {"over_cap": True, "running": running, "max_parallel": cap}
        return result

    @staticmethod
    def log_orphan(slot: tuple[str, str], task: asyncio.Task):
        """The outcome of a launch whose rpc already answered (timed out)."""
        if task.cancelled():
            log.warning("launch %s %s cancelled after its rpc", *slot)
        elif (e := task.exception()) is not None:
            log.warning("launch %s %s failed after its rpc: %s", *slot, e)
        else:
            rc, out = task.result()
            tail = out.strip().splitlines()
            log.warning(
                "launch %s %s finished after its rpc: exit %s%s",
                *slot, rc, f" ({tail[-1]})" if tail else "",
            )  # fmt: skip

    async def launch_locked(
        self, board: dict, nn: str, params: dict
    ) -> tuple[int, str]:
        """After the board's earlier launches by this daemon, under its flock:
        re-check the ticket is backlog, tk.sh launch (bounded by
        launch_timeout_s). Frees the slot unless the launch went, else starts its
        reservation_ttl_s."""
        slot, launched = (board["key"], nn), False
        queue = self.board_locks.setdefault(board["key"], asyncio.Lock())
        lock, queued = None, False
        try:
            await queue.acquire()
            queued = True
            if self.stopping:  # its turn came during shutdown: don't start one
                raise RpcRefused("daemon stopping")
            lock = await board_lock(board["board_path"], self.cfg["launch_lock_wait_s"])
            if self.stopping:  # the stop came while it waited on an outside flock
                raise RpcRefused("daemon stopping")
            if (col := await self.col(board, nn)) != "backlog":
                raise RpcRefused(f"ticket {nn} is {col}, not backlog")
            limit = self.cfg["launch_timeout_s"]
            try:
                rc, out = await asyncio.wait_for(
                    self.tk(
                        board, "launch", nn, "--type", params["type"],
                        "--model", params["model"], "--effort", params["effort"],
                    ),
                    limit,
                )  # fmt: skip
            except TimeoutError:
                raise RpcRefused(f"tk.sh launch timed out after {limit:g}s") from None
            launched = rc in (0, 3)
            return rc, out
        finally:
            if lock is not None:
                lock.close()
            if queued:
                queue.release()
            if launched:
                self.reserved[slot] = time.monotonic()
            else:
                self.reserved.pop(slot, None)

    async def rpc_say_agent(self, ws, params: dict) -> dict:
        """tk.sh say, the message passed as a file (deleted after)."""
        board = self.board(params)
        fd, path = tempfile.mkstemp(prefix="tk-say-", suffix=".md")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(params["message"])
            rc, out = await self.tk(board, "say", params["nn"], path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
        result = {"ok": rc == 0, "exit_code": rc, "output_tail": out}
        if rc in AGENT_EXITS:
            result["reason"] = AGENT_EXITS[rc]
        return result

    async def rpc_pane_tail(self, ws, params: dict) -> dict:
        """tk.sh show <lines>: the agent's recent output (its stdout only) in
        data.pane; on failure, stdout and stderr in output_tail."""
        board = self.board(params)
        rc, out, err = await run_cmd(
            [tk_sh(), "show", board["board_path"], params["nn"],
             str(int(params["lines"]))],
            cwd=board["root"],
        )  # fmt: skip
        if rc == 0:
            return {"ok": True, "exit_code": 0, "output_tail": err,
                    "data": {"pane": out[-PANE:]}}  # fmt: skip
        result = {"ok": False, "exit_code": rc, "output_tail": out + err}
        if rc in AGENT_EXITS:
            result["reason"] = AGENT_EXITS[rc]
        return result

    async def handle_rpc(self, ws, msg: dict):
        rid, method = msg.get("id"), msg.get("method")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            log.warning("rpc without a usable id dropped (method %r)", method)
            return
        handler = self.rpcs.get(method)
        invalid = handler and self.schema.errors(
            {**msg, "params": msg.get("params") or {}}
        )
        if handler is None:
            result = {
                "ok": False,
                "exit_code": None,
                "output_tail": "",
                "reason": f"unknown rpc {method!r}: this daemon runs {', '.join(sorted(self.rpcs))}",
            }
        elif invalid:  # nothing runs on params the schema rejects (argv safety)
            handler = None  # and nothing to re-export after
            result = {
                "ok": False,
                "exit_code": None,
                "output_tail": "",
                "reason": f"invalid params: {'; '.join(invalid[:3])}",
            }
        else:
            timeout = self.cfg[
                "rpc_synthesis_timeout_s"
                if method in SYNTHESIS_RPCS
                else "rpc_timeout_s"
            ]
            try:
                result = await asyncio.wait_for(
                    handler(ws, msg.get("params") or {}), timeout
                )
            except TimeoutError:
                result = {
                    "ok": False,
                    "exit_code": None,
                    "output_tail": "",
                    "reason": f"timed out after {timeout:g}s",
                }
            except RpcRefused as e:
                result = {
                    "ok": False,
                    "exit_code": None,
                    "output_tail": "",
                    "reason": str(e),
                }
            except Exception as e:  # reported to the hub, not raised
                log.exception("rpc %s %s failed", rid, method)
                result = {
                    "ok": False,
                    "exit_code": None,
                    "output_tail": "",
                    "reason": str(e),
                }
        result["output_tail"] = result["output_tail"][-TAIL:]
        log.info("rpc %s %s -> ok=%s", rid, method, result["ok"])
        await ws.send(json.dumps({"type": "rpc_result", "id": rid, **result}))
        if handler is not None and method not in READ_ONLY_RPCS:
            await self.sync(ws, full=False)  # re-export right after each RPC

    async def guarded_rpc(self, ws, msg: dict):
        try:
            await self.handle_rpc(ws, msg)
        except Exception as e:  # noqa: BLE001 — e.g. the socket closed mid-RPC
            log.warning("rpc %s: %s: %s", msg.get("id"), type(e).__name__, e)

    # ---- connection ----------------------------------------------------------------
    async def read_token(self) -> str:
        if self.token is None:
            rc, out, _ = await run_cmd(["op", "read", self.cfg["token_ref"]])
            if rc != 0 or not out.strip():
                raise RuntimeError(
                    f"op read {self.cfg['token_ref']} failed (exit {rc})"
                )
            self.token = out.strip()
        return self.token

    def fallback_health(self, why: str) -> dict:
        """The last health sent, else a schema-valid one that launches nothing."""
        if self.health is not None:
            return self.health
        return {
            "daemon_version": DAEMON_VERSION,
            "herdr": {"ok": False, "status": f"unknown: {why}"},
            "usage": {"state": "unknown", "five_hour": None, "seven_day": None,
                      "updated_at": None},
            "running": 0,
            "last_sync": None,
        }  # fmt: skip

    async def session(self, on_welcome):
        token = await self.read_token()
        try:  # hello carries the health; done before connecting (the hub allows 10 s)
            health = (await self.export())[0]["health"]
        except Exception as e:  # noqa: BLE001 — connect anyway; the snapshot follows
            log.warning("export before hello failed: %s", e)
            health = self.fallback_health(str(e))
        async with connect(
            self.uri,
            host=self.cfg["ip"],
            port=self.port,
            server_hostname=self.name,
            additional_headers={"Authorization": f"Bearer {token}"},
            proxy=None,
            max_size=None,
            open_timeout=self.cfg["hello_timeout_s"],
        ) as ws:
            hello = {
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "devbox": self.cfg["devbox"],
                "health": health,
            }
            await ws.send(json.dumps(hello))
            reply = json.loads(
                await asyncio.wait_for(ws.recv(), self.cfg["hello_timeout_s"])
            )
            if reply.get("type") == "refused":
                self.token = None  # a rotated token is read again next time
                raise Refused(reply.get("reason", "no reason given"))
            if reply.get("type") != "welcome":
                raise RuntimeError(f"expected welcome, got {reply.get('type')!r}")
            log.info("connected to %s via %s", self.name, self.cfg["ip"])
            on_welcome()
            self.need_full = True
            try:
                await self.sync(ws, full=True)
            except (RuntimeError, ValueError, KeyError) as e:  # the next tick retries
                log.warning("full snapshot failed, retrying on the next tick: %s", e)
            try:
                await self.serve(ws)
            except asyncio.CancelledError:  # SIGTERM: say we are going away
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.close(1001, "daemon stopping"), 2)
                raise

    async def serve(self, ws):
        rpcs: set[asyncio.Task] = set()

        async def tick():
            while True:
                await asyncio.sleep(self.cfg["export_interval_s"])
                try:
                    await self.sync(ws, full=False)
                except Exception as e:  # noqa: BLE001 — next tick tries again
                    log.warning("periodic export failed: %s", e)

        ticker = asyncio.create_task(tick())
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
                if not isinstance(msg, dict):
                    log.warning("ignoring a hub message that is not a JSON object")
                    continue
                if msg.get("type") == "rpc":
                    task = asyncio.create_task(self.guarded_rpc(ws, msg))
                    rpcs.add(task)
                    task.add_done_callback(rpcs.discard)
                elif msg.get("type") == "refused":
                    raise Refused(msg.get("reason", "no reason given"))
                else:
                    log.warning("ignoring hub message of type %r", msg.get("type"))
        finally:
            ticker.cancel()
            for task in rpcs:
                task.cancel()

    async def run_forever(self):
        attempt = 0

        def reset():
            nonlocal attempt
            attempt = 0

        while True:
            try:
                await self.session(on_welcome=reset)
                log.warning("hub closed the connection")
            except Refused as e:
                log.error("hub refused this daemon: %s", e)
            except InvalidStatus as e:
                if e.response.status_code in (401, 403):
                    self.token = None
                log.error(
                    "hub rejected the connection: HTTP %s", e.response.status_code
                )
            except (OSError, TimeoutError, RuntimeError, ValueError) as e:
                log.error("connection failed: %s", e)
            except Exception as e:  # noqa: BLE001 — a dropped socket must not end the daemon
                log.error("connection lost: %s: %s", type(e).__name__, e)
            delay = min(
                self.cfg["backoff_max_s"], self.cfg["backoff_initial_s"] * 2**attempt
            )
            delay *= random.uniform(0.5, 1.0)
            attempt += 1
            log.info("reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # websockets' debug logs carry request headers — the bearer token.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        cfg = load_config(Path.home())
        daemon = Daemon(cfg)
    except (OSError, ValueError) as e:
        log.error("bad config: %s", e)
        return 1
    log.info(
        "tk-daemon %s for %s, hub %s via %s",
        DAEMON_VERSION,
        cfg["devbox"],
        daemon.name,
        cfg["ip"],
    )
    asyncio.run(run(daemon))
    return 0


async def run(daemon: Daemon):
    """Run until SIGTERM / SIGINT, which cancel the daemon so a running export's
    processes are killed (run_cmd) before it exits. They also set
    daemon.stopping at once, so launches still queued or waiting on the flock
    refuse ("daemon stopping") without running tk.sh; launches already in
    tk.sh launch get up to shutdown_grace_s to finish before the exit."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(daemon.run_forever())

    def stop():
        # At once: queued launches refuse, only those in tk.sh launch get waited for.
        daemon.stopping = True
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if daemon.launches:  # a launch killed midway leaves a half-made worktree
        grace = daemon.cfg["shutdown_grace_s"]
        log.info("waiting up to %gs for %d launches", grace, len(daemon.launches))
        await asyncio.wait(set(daemon.launches), timeout=grace)
    log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())
