"""Hub state and behaviour behind the sockets: admitting daemons, applying their
snapshots, broadcasting to the UI."""

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from hub_server import NAME, PROTOCOL_VERSION, protocol
from hub_server.store import Store, now

# board.md, Protocol: 10 min for synthesis RPCs, 2 min for everything else.
LONG_RPCS = {"refine_with_comment", "refresh_board_brief", "run_retro"}
LONG_TIMEOUT = 600.0
TIMEOUT = 120.0
UI_SEND_TIMEOUT = 5.0
RESULT_FIELDS = ("id", "ok", "exit_code", "output_tail", "reason", "data")
# The ticket RPCs POST .../tickets/{nn}/action routes to the board's daemon.
TICKET_ACTIONS = ("launch_ticket", "say_agent", "pane_tail")
# tk.sh launch --type values (ticket-skill's branch prefixes).
TICKET_TYPES = ("feat", "fix", "refactor", "chore", "docs", "test", "perf", "ci")

# What sending on a socket whose peer has gone can raise (Starlette, uvicorn).
SEND_ERRORS = (WebSocketDisconnect, RuntimeError, OSError)

VERSION = re.compile(r"^([0-9]+)\.[0-9]+$")
log = logging.getLogger("hub_server")


class DevboxOffline(Exception):
    pass


class BadAction(Exception):
    """An action the hub will not send: not a ticket RPC, or failing the schema."""


def ticket_type(ticket: dict) -> str:
    """The launch type: the branch's prefix, else the title's first word when it
    names a type ("Fix the …", "docs: …"), else feat (board.md, Scheduler)."""
    for text in (ticket.get("branch") or "", ticket.get("title") or ""):
        word = re.split(r"[-/:\s]", text.strip().lower(), maxsplit=1)[0]
        if word in TICKET_TYPES:
            return word
    return "feat"


def frontier_launches(board: dict) -> tuple[list[tuple[str, dict]], list[dict]]:
    """A board's frontier (its backlog tickets) as launch_ticket params with the
    suggested model/effort, manual false (the daemon keeps max_parallel), and
    the tickets skipped for want of a suggestion."""
    launches, skipped = [], []
    for t in board.get("tickets") or []:
        if t.get("col") != "backlog":
            continue
        if not t.get("smodel") or not t.get("seffort"):
            skipped.append({"nn": t["nn"], "reason": "no suggested model/effort"})
            continue
        launches.append(
            (t["nn"], {"type": ticket_type(t), "model": t["smodel"],
                       "effort": t["seffort"], "manual": False})
        )  # fmt: skip
    return launches, skipped


@dataclass
class PendingRpc:
    devbox: str
    method: str
    board_key: str | None
    nn: str | None
    ws: WebSocket  # the connection it was sent on; only that one can lose it
    timer: asyncio.TimerHandle | None = field(default=None)


class Hub:
    def __init__(
        self,
        db_path: Path | str,
        rpc_timeouts: dict[str, float] | None = None,
        ui_send_timeout: float = UI_SEND_TIMEOUT,
    ):
        self.store = Store(db_path)
        self.ui_send_timeout = ui_send_timeout
        self.rpc_timeouts = rpc_timeouts or {}
        self.pending: dict[str, PendingRpc] = {}
        self._tasks: set[asyncio.Task] = set()
        self.ui: set[WebSocket] = set()
        self.daemons: dict[str, WebSocket] = {}
        # A restarted hub has no daemons yet: every box is offline, every board stale.
        self.store.db.execute("UPDATE devboxes SET online = 0")
        self.store.boards.mark_stale()

    async def broadcast(self, message: dict | Callable[[], dict | None]):
        """Send to every UI. State (board_synced, devbox_status) is passed as a
        callable, read from the store right before each send: another
        connection's handler may change it during any await, and the UI must
        end on what the store says. A callable returning None sends nothing."""
        for ws in list(self.ui):
            current = message() if callable(message) else message
            if current is None:
                continue
            try:
                # A stalled UI (full TCP buffer) must not hold up the daemons.
                async with asyncio.timeout(self.ui_send_timeout):
                    await ws.send_json(current)
            except TimeoutError:
                log.warning(
                    "dropping a UI socket that stalled for %ss", self.ui_send_timeout
                )
                self.ui.discard(ws)
                self._spawn(_close_quietly(ws))
            except SEND_ERRORS:  # a UI that went away mid-send
                self.ui.discard(ws)

    async def admit(self, hello, token: str) -> str | None:
        """Check a daemon's hello; returns the refusal reason, or None."""
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return "expected a hello message first"
        devbox = hello.get("devbox")
        version = hello.get("protocol_version")
        if not isinstance(devbox, str) or not NAME.match(devbox):
            return "hello carries no valid devbox name"
        if not self.store.check_token(devbox, token):
            return f"bad or unknown token for devbox {devbox}"
        match = VERSION.match(version) if isinstance(version, str) else None
        if not match or match[1] != VERSION.match(PROTOCOL_VERSION)[1]:
            reason = (
                f"protocol_version {version} is not compatible with hub "
                f"{PROTOCOL_VERSION} (major differs)"
            )
            if devbox not in self.daemons:  # never mark a live connection offline
                self.store.update_devbox(
                    devbox,
                    online=False,
                    last_seen=now(),
                    protocol_version=version if match else None,
                    refused_reason=reason,
                )
                await self.broadcast(self._status(devbox))
            return reason
        if invalid := protocol.problem(hello):
            return f"invalid hello: {invalid}"
        return None

    # -- connected daemons ---------------------------------------------------

    # State changes happen synchronously before the first await, so another
    # connection's handler running during a broadcast never sees (or undoes)
    # half of them; state is broadcast as fresh reads (see broadcast).

    def _status(self, devbox: str) -> Callable[[], dict]:
        return lambda: self.store.devbox_status(devbox)

    def _removed(self, key: str) -> Callable[[], dict | None]:
        """board_removed, unless the board is back by the time it is sent."""
        return lambda: (
            None
            if self.store.boards.get(key)
            else {"type": "board_removed", "board_key": key}
        )

    def _synced(self, key: str) -> Callable[[], dict | None]:
        def read():
            board = self.store.boards.get(key)
            return board and {"type": "board_synced", "board": board}

        return read

    async def connected(self, devbox: str, ws: WebSocket, hello: dict):
        old = self.daemons.get(devbox)
        self.daemons[devbox] = ws
        self.store.update_devbox(
            devbox,
            online=True,
            last_seen=now(),
            health=hello.get("health"),
            protocol_version=hello["protocol_version"],
            refused_reason=None,
        )
        orphaned = self._take_pending(old, "devbox reconnected") if old else []
        if old is not None:  # a reconnect beat the old socket's close
            with contextlib.suppress(*SEND_ERRORS):
                await old.close()
        await self._finish_all(orphaned)
        await self.broadcast(self._status(devbox))

    async def disconnected(self, devbox: str, ws: WebSocket):
        if self.daemons.get(devbox) is not ws:
            return  # replaced by a newer connection
        del self.daemons[devbox]
        self.store.update_devbox(devbox, online=False, last_seen=now())
        self.store.boards.mark_stale(devbox)
        keys = self.store.boards.keys(devbox)
        failed = self._take_pending(ws, "devbox disconnected")
        await self._finish_all(failed)
        for key in keys:
            await self.broadcast(self._synced(key))
        await self.broadcast(self._status(devbox))

    async def received(self, devbox: str, ws: WebSocket, message):
        if self.daemons.get(devbox) is not ws:
            return  # a replaced connection's last words
        if invalid := protocol.problem(message):
            log.warning("ignoring an invalid message from %s: %s", devbox, invalid)
            return
        if message.get("type") == "snapshot":
            await self.apply_snapshot(devbox, message)
        elif message.get("type") == "rpc_result":
            rpc_id = message.get("id")
            rpc = self.pending.get(rpc_id) if isinstance(rpc_id, str) else None
            if rpc is not None and rpc.ws is ws:  # else late, foreign or garbage
                result = {k: message[k] for k in RESULT_FIELDS if k in message}
                await self._finish(rpc_id, {"type": "rpc_result", **result})

    # -- rpc -----------------------------------------------------------------

    def timeout(self, method: str) -> float:
        if method in self.rpc_timeouts:
            return self.rpc_timeouts[method]
        return LONG_TIMEOUT if method in LONG_RPCS else TIMEOUT

    async def send_rpc(
        self,
        devbox: str,
        method: str,
        params: dict,
        board_key: str | None = None,
        nn: str | None = None,
    ) -> str:
        """Send an rpc to the devbox's daemon; returns its id. The rpc_result
        (the daemon's, or a timeout/disconnect failure) is broadcast to the UI."""
        ws = self.daemons.get(devbox)
        if ws is None:
            raise DevboxOffline(devbox)
        rpc_id = f"r-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        rpc = PendingRpc(devbox, method, board_key, nn, ws)
        self.pending[rpc_id] = rpc
        seconds = self.timeout(method)
        timed_out = _failed(rpc_id, f"timed out after {seconds:g}s")
        rpc.timer = loop.call_later(
            seconds, lambda: self._spawn(self._finish(rpc_id, timed_out))
        )
        try:
            await ws.send_json(
                {"type": "rpc", "id": rpc_id, "method": method, "params": params}
            )
        except SEND_ERRORS:
            await self._finish(rpc_id, _failed(rpc_id, "devbox disconnected"))
        return rpc_id

    async def ticket_action(
        self, devbox: str, board_key: str, nn: str, method: str, params: dict
    ) -> dict:
        """One ticket RPC (TICKET_ACTIONS) to the board's daemon, with board_key
        and nn added; BadAction when the rpc would break the schema."""
        if method not in TICKET_ACTIONS:
            raise BadAction(f"{method!r} is not one of {', '.join(TICKET_ACTIONS)}")
        if "board_key" in params or "nn" in params:
            raise BadAction("board_key and nn come from the URL, not params")
        params = {"board_key": board_key, "nn": nn, **params}
        rpc = {"type": "rpc", "id": "r-check", "method": method, "params": params}
        if invalid := protocol.problem(rpc, strict=True):
            raise BadAction(invalid)
        rpc_id = await self.send_rpc(devbox, method, params, board_key, nn)
        return {"id": rpc_id, "devbox": devbox, "method": method,
                "board_key": board_key, "nn": nn}  # fmt: skip

    async def scan(self, devbox: str | None = None) -> list[dict]:
        """rescan_boards on one devbox (DevboxOffline if it isn't connected) or
        on every connected one, skipping any that drop out meanwhile."""
        if devbox is not None:
            return [
                {
                    "id": await self.send_rpc(devbox, "rescan_boards", {}),
                    "devbox": devbox,
                }
            ]
        sent = []
        for target in sorted(self.daemons):
            with contextlib.suppress(DevboxOffline):
                rpc_id = await self.send_rpc(target, "rescan_boards", {})
                sent.append({"id": rpc_id, "devbox": target})
        return sent

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _take_pending(self, ws: WebSocket, reason: str) -> list[tuple]:
        """Remove the rpcs sent on `ws` from pending; returns their failures."""
        taken = [rid for rid, rpc in self.pending.items() if rpc.ws is ws]
        return [(self.pending.pop(rid), _failed(rid, reason)) for rid in taken]

    async def _finish_all(self, finished: list[tuple]):
        for rpc, result in finished:
            await self._complete(rpc, result)

    async def _finish(self, rpc_id: str, result: dict):
        rpc = self.pending.pop(rpc_id, None)
        if rpc is not None:
            await self._complete(rpc, result)

    async def _complete(self, rpc: PendingRpc, result: dict):
        if rpc.timer is not None:
            rpc.timer.cancel()
        result = result | {"devbox": rpc.devbox, "method": rpc.method}
        if rpc.board_key is not None:
            result["board_key"] = rpc.board_key
        if rpc.nn is not None:
            result["nn"] = rpc.nn
        await self.broadcast(result)

    async def apply_snapshot(self, devbox: str, snapshot: dict):
        """Store the snapshot's boards, derive ticket_moved from `col`, remove
        boards absent from a full snapshot or listed in `removed`; then
        broadcast, with every write done first."""
        boards = self.store.boards
        at = now()
        self.store.update_devbox(devbox, last_seen=at, health=snapshot.get("health"))
        prefix = f"{devbox}/"
        events = []
        received = []
        for board in snapshot.get("boards") or []:
            key = board.get("key") if isinstance(board, dict) else None
            # A daemon only speaks for its own box.
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            if board.get("devbox") != devbox:
                continue
            received.append(key)
            moves = _moves(boards.get(key), board)
            boards.put(board)
            events.append(self._synced(key))
            for nn, from_col, to_col in moves:
                boards.log_move(key, nn, from_col, to_col, at)
                events.append(
                    {
                        "type": "ticket_moved",
                        "board_key": key,
                        "nn": nn,
                        "from": from_col,
                        "to": to_col,
                        "at": at,
                    }
                )
        if snapshot.get("full"):
            gone = [k for k in boards.keys(devbox) if k not in received]
        else:
            gone = [
                k
                for k in snapshot.get("removed") or []
                if isinstance(k, str) and k.startswith(prefix)
            ]
        events += [self._removed(k) for k in gone if boards.remove(k)]
        events.append(self._status(devbox))
        for event in events:
            await self.broadcast(event)


async def _close_quietly(ws: WebSocket):
    with contextlib.suppress(*SEND_ERRORS):
        await ws.close()


def _failed(rpc_id: str, reason: str) -> dict:
    """An rpc_result for an rpc whose command never ran."""
    return {
        "type": "rpc_result",
        "id": rpc_id,
        "ok": False,
        "exit_code": None,
        "output_tail": "",
        "reason": reason,
    }


def _moves(old: dict | None, new: dict) -> list[tuple[str, str, str]]:
    """(nn, from, to) for every ticket whose `col` changed; new tickets don't move."""
    if old is None:
        return []
    before = {t.get("nn"): t.get("col") for t in old.get("tickets") or []}
    return [
        (t["nn"], before[t["nn"]], t["col"])
        for t in new.get("tickets") or []
        if t.get("nn") in before and before[t["nn"]] != t.get("col")
    ]
