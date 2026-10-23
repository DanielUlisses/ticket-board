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
   "export_call_timeout_s" (30), "schema" (../protocol/schema.json)}
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
The box policy, ~/.config/tk-daemon/policy.json, is read by tk-export.sh.
SIGTERM / SIGINT stop the daemon (closing the connection with 1001) and kill a
running export.
"""

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import sys
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
}
# The board-override keys set_policy may write, mirroring tk.sh's POLICY_CHECK; box-level
# keys (max_parallel, usage_gate_pct, synthesis) are not among them.
POLICY_WRITABLE = {
    "autonomy": lambda v: v in ("assist", "auto"),
    "forge": lambda v: v in ("github", "azure", "none"),
    "require_human_before_pr": lambda v: isinstance(v, bool),
    "auto_merge": lambda v: isinstance(v, bool),
    "night_shift": lambda v: isinstance(v, bool),
}
SYNTHESIS_RPCS = frozenset({"refine_with_comment", "refresh_board_brief", "run_retro"})
FAILED = re.compile(r"^tk-export: failed (\S+)(?: (/.*))?$", re.MULTILINE)
RESUME = re.compile(r"^tk-export: resume ([0-9]+)$", re.MULTILINE)
TAIL = 4000  # characters of output_tail

log = logging.getLogger("tk-daemon")


class Refused(Exception):
    pass


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
        self.rpcs = {
            "rescan_boards": self.rpc_rescan_boards,
            "set_policy": self.rpc_set_policy,
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

    async def rpc_set_policy(self, ws, params: dict) -> dict:
        """`tk.sh policy-set <board> key=value…` in the repo's main checkout; the
        re-export after every RPC then carries the new resolved policy."""
        board = self.sent.get(params.get("board_key"))
        values = params.get("values")
        if board is None or not isinstance(values, dict) or not values:
            raise ValueError(f"no board {params.get('board_key')!r} on this box")
        for k, v in values.items():
            allowed = POLICY_WRITABLE.get(k)
            if allowed is None or not (v is None or allowed(v)):
                raise ValueError(
                    f"policy {k}={v!r} is not a writable board-override value"
                )
        tk = os.environ.get("TK_SH") or (
            "tk.sh"
            if shutil.which("tk.sh")
            else str(Path.home() / ".claude/skills/implement-tickets/scripts/tk.sh")
        )
        # Each value as JSON, so tk.sh reads back the type it was sent (quoting keeps a
        # string a string even when it looks like JSON); None is `key=`, which removes
        # the board override.
        args = [f"{k}={'' if v is None else json.dumps(v)}" for k, v in values.items()]
        code, out, err = await run_cmd(
            [tk, "policy-set", board["board_path"], *args], cwd=board["root"]
        )
        return {"ok": code == 0, "exit_code": code, "output_tail": out + err}

    async def handle_rpc(self, ws, msg: dict):
        rid, method = msg.get("id"), msg.get("method")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            log.warning("rpc without a usable id dropped (method %r)", method)
            return
        handler = self.rpcs.get(method)
        if handler is None:
            result = {
                "ok": False,
                "exit_code": None,
                "output_tail": "",
                "reason": f"unknown rpc {method!r}: this daemon runs {', '.join(sorted(self.rpcs))}",
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
        if handler is not None and method != "rescan_boards":
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
    processes are killed (run_cmd) before it exits."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(daemon.run_forever())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())
