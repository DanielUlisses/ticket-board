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
   "reservation_ttl_s" (120), "shutdown_grace_s" (60), "brief_debounce_s" (30),
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
a temp file; tk.sh exit 20 (at a dialog) and 21 (no agent) come back as reasons.
get_diff (read-only: git diff / ls-files in the ticket's worktree with
GIT_OPTIONAL_LOCKS=0 and no user git config, never add) returns the changed files
and diff_hash, or one file's diff capped at diff_cap_chars (a file that would be
stubbed is never pulled into memory; captured git output stops at the diff_max_*
limits). Untracked files are read and hashed directly in chunks, within a size and
total budget, past which they are fingerprinted by stat only; an untracked nested repo is listed as a `nested_repo`
stub whose hash carries its HEAD. pane_tail returns the lines in
data.pane and, being read-only, is not followed by a re-export.
Synthesis (agy_runner.py): a ticket that moves into needs, human or pr between two
exports (never one seen for the first time), and refine_with_comment, run the board's
`synthesis.backends` on the ticket; the result lands in the board's hub-meta.json
(`tickets.<NN>`; the file as it was before the run in hub-meta.prev.json), a failure
keeps the last good content with `synthesis_status: failed`. The hub hears
synthesis_started / synthesis_finished / synthesis_failed. One run per ticket at a time.
A `running` status with no run behind it (the daemon was killed) becomes failed,
"interrupted", at the next export. TK_AGY_TIMEOUT (seconds > 0, read at start)
overrides `synthesis.timeout`; an invalid value of either is ignored with a warning.
Merge conflicts: merge_pr's exit 10 sends ticket-merger (Daemon.resolve) through
merge-conflict.sh (see merge_conflict_sh); its state is the ticket's hub-meta
`merge_conflict`. finish_conflict (confirm: true) is the only commit and push.
SIGTERM / SIGINT stop the daemon (closing the connection with 1001) and kill a
running export; launches still queued then refuse ("daemon stopping") without
running tk.sh, and those already in it get shutdown_grace_s to finish before
they are killed, so systemd's TimeoutStopSec must exceed it (the default 90 s
does, for the default 60 s).
"""

import argparse
import asyncio
import collections
import contextlib
import errno
import fcntl
import functools
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import typing
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

import agy_runner

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
    "diff_cap_chars": 200_000,
    # get_diff memory bounds: any captured git output (names, one file's diff)
    "diff_max_output_bytes": 4 << 20,
    # the full tracked diff, streamed into diff_hash (refused past it)
    "diff_max_hash_bytes": 256 << 20,
    # an untracked file larger than this is stat-fingerprinted, not read
    "diff_max_untracked_file_bytes": 16 << 20,
    # untracked bytes read per request; the rest are stat-fingerprinted
    "diff_max_untracked_bytes": 256 << 20,
    # files listed (the hash still covers them all)
    "diff_max_files": 1000,
    # a file whose mtime/ctime is this close to the read is not cached (it could
    # still change within the timestamp's granularity: git's "racily clean" rule).
    # Floored at DIFF_RACY_FLOOR_S, whatever is configured: filesystems stamp
    # times in whole seconds (ext3, HFS+, some NFS) or 2 s (FAT).
    "diff_racy_window_s": 2.0,
    "brief_debounce_s": 30,  # a board's brief refreshes this long after its last move
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
SYNTHESIS_COLUMNS = frozenset({"needs", "human", "pr"})  # a move into one synthesizes
SHOW_LINES = "180"  # tk.sh show <board> <NN> 180, part of the synthesis context
FAILED = re.compile(r"^tk-export: failed (\S+)(?: (/.*))?$", re.MULTILINE)
RESUME = re.compile(r"^tk-export: resume ([0-9]+)$", re.MULTILINE)
TAIL = 4000  # characters of output_tail
PANE = 200_000  # characters of a pane_tail's data.pane
# RPCs that change nothing: no re-export after them (pane_tail is polled).
READ_ONLY_RPCS = frozenset({"rescan_boards", "pane_tail", "verify_landed", "get_diff"})
MAX_PARALLEL = 5  # running tickets per box, unless the box policy says otherwise
RUNNING = ("working", "review")  # as tk-export.sh counts health.running
# tk.sh say / show exit codes, as the UI shows them.
AGENT_EXITS = {
    20: "dialog: the agent is at a dialog or not at its prompt; answer it in its herdr tab",
    21: "no agent: the ticket's agent isn't running",
}

# The ship RPCs: tk.sh verb and the columns the ticket may be in when it runs.
SHIP_VERBS = {
    "open_pr": ("pr", ("human",)),
    "ssh_push": ("push", ("human",)),
    "merge_pr": ("merge", ("pr",)),
    "resolve_nocode": ("resolve", ("needs", "review", "human", "pr")),
    "resolve_ticket": ("resolve", ("done",)),  # landed: plain resolve, never --no-code
}  # fmt: skip
# tk.sh merge's gate exit codes (run_gates), as the UI shows them.
MERGE_EXITS = {
    10: "conflicts: the PR conflicts with the base branch",
    11: "checks pending: the PR's checks are still running",
    12: "gate failed: the PR is not ready to merge",
}
SHIP_EXITS = {5: "tk.sh wants the claude runner for this verb (exit 5)"}
MERGER_SUMMARY = 20_000  # characters of the agent's answer kept as the summary


def merger_argv(script: str, wt: str, prompt: str) -> list[str]:
    """ticket-merger, sent by the daemon when tk.sh merge exits 10 (Daemon.resolve).
    Headless (`claude -p`) nothing prompts, so it gets a scoped allow-list, never a
    permission-skip flag or mode: read anything; edit only in the ticket's worktree;
    stage, diff, status and log in git; merge-conflict.sh status. Committing, pushing,
    finish and abort are denied outright (finish is the developer's Commit merge).
    The prompt goes right after -p: the tool lists are variadic, and would swallow
    an argument after them."""
    wt = wt.rstrip("/")
    allowed = [
        "Read", "Grep", "Glob", f"Edit(/{wt}/**)", f"Write(/{wt}/**)",
        "Bash(git add *)", "Bash(git diff)", "Bash(git diff *)", "Bash(git status)",
        "Bash(git status *)", "Bash(git log *)", f"Bash({script} status *)",
    ]  # fmt: skip
    denied = [
        "Bash(git commit *)", "Bash(git push *)", "Bash(git merge *)",
        "Bash(git reset *)", "Bash(git rebase *)", "Bash(git stash *)",
        "Bash(git checkout *)", "Bash(git switch *)",
        f"Bash({script} start *)", f"Bash({script} finish *)", f"Bash({script} abort *)",
    ]  # fmt: skip
    return ["claude", "-p", prompt, "--agent", "ticket-merger", "--model", "sonnet",
            "--allowedTools", *allowed, "--disallowedTools", *denied]  # fmt: skip


GATE_LINE = re.compile(r"^GATE (\w+) (PASS|FAIL)\b ?(.*)$")

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


def merge_conflict_sh() -> str:
    """ticket-skill's merge-conflict.sh: $TK_MERGE_CONFLICT, else the one beside
    tk.sh (as tk_sh() finds it, symlinks followed), else the installed skill's.
    An absolute path: ticket-merger's prompt names it."""
    if os.environ.get("TK_MERGE_CONFLICT"):
        return os.environ["TK_MERGE_CONFLICT"]
    tk = shutil.which(tk_sh())
    if tk and (beside := Path(tk).resolve().with_name("merge-conflict.sh")).is_file():
        return str(beside)
    return str(
        Path.home() / ".claude/skills/implement-tickets/scripts/merge-conflict.sh"
    )


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


DIFF_RACY_FLOOR_S = 2.0
# Test-only: the hidden --diff-racy-window-test-s flag (see main) bypasses the
# floor, as a test has to let the stat cache engage on a file it just wrote. It is
# a flag, not a config key, so a user's config.json cannot set it.

# get_diff's git flags: everything that could vary with the dev's git config is
# pinned, so diff_hash changes only with the content: no system or global config,
# and the repo-local settings that could make git miss an edit are overridden (see
# Daemon.git_flags: trustctime, checkStat, fsmonitor), submodule changes are never
# ignored (--ignore-submodules=none beats diff.ignoreSubmodules and the
# .gitmodules "ignore" setting). The only things read from the dev's own config
# are core.excludesFile and core.attributesFile (see Daemon.user_path_config);
# filters (LFS, clean/smudge) are not applied, as the global config that declares
# them is not read. A repo-local config can still change what git calls binary or
# how it handles line endings (core.autocrlf, .gitattributes).
DIFF_FLAGS = (
    "--no-color", "--no-ext-diff", "--no-textconv", "--full-index",
    "--diff-algorithm=myers", "--no-indent-heuristic", "--src-prefix=a/",
    "--dst-prefix=b/", "-U3", "--inter-hunk-context=0", "--ignore-submodules=none",
)  # fmt: skip


GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    # paths are literal, not pathspecs: no globs ([id].tsx), no :magic
    "GIT_LITERAL_PATHSPECS": "1",
}


def stat_key(st: os.stat_result) -> tuple:
    """What an edit moves: inode, size, mtime, ctime (a same-size edit with the
    mtime put back still moves it) and mode."""
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)


def fingerprint(path: Path) -> tuple[dict | None, tuple | None, int]:
    """What get_diff needs of an untracked file, read in chunks, nothing written:
    a fingerprint of its content (and exec bit / link target), whether git would
    call it binary (a NUL in the first 8000 bytes), and its line count. Returns
    (info, stat key of what was read, time of the read in ns). info is None for
    anything but a file or a symlink; OSError when it cannot be read.

    The file is opened without following links or blocking, and checked to be the
    regular file that was lstat-ed (not swapped for a fifo or a symlink since); at
    most its fstat size is read, so one growing under us cannot run away."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(path)
        info = {"fingerprint": f"link:{target}", "binary": False, "added": 1, "size": 0}
        return info, None, 0
    if not stat.S_ISREG(st.st_mode):
        return None, None, 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fh:
        fst = os.fstat(fd)
        now = time.time_ns()
        if not stat.S_ISREG(fst.st_mode) or (fst.st_ino, fst.st_dev) != (
            st.st_ino, st.st_dev,
        ):  # fmt: skip
            raise OSError(errno.ESTALE, "changed while reading")
        sha, lines, last, size, binary = hashlib.sha256(), 0, b"", 0, False
        while size < fst.st_size and (
            chunk := fh.read(min(1 << 20, fst.st_size - size))
        ):
            if size < 8000 and b"\0" in chunk[: 8000 - size]:
                binary = True
            size += len(chunk)
            sha.update(chunk)
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if size and last != b"\n":
        lines += 1
    mode = "x" if fst.st_mode & 0o100 else "-"
    info = {"fingerprint": f"{mode}:{sha.hexdigest()}", "binary": binary,
            "added": 0 if binary else lines, "size": size}  # fmt: skip
    return info, stat_key(fst), now


def shown_path(path: str) -> str:
    """A path as the hub and the page see it. Paths are carried as os.fsdecode
    gives them (invalid UTF-8 bytes as lone surrogates), so they go back to the
    disk byte for byte; for JSON the bad bytes become U+FFFD. A file is asked
    for by that shown name (see rpc_get_diff), which stays stable across calls."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def shown_file(f: dict) -> dict:
    """A file entry with its paths made safe for JSON."""
    out = {**f, "path": shown_path(f["path"])}
    if "old_path" in f:
        out["old_path"] = shown_path(f["old_path"])
    return out


def fingerprint_retrying(path: Path) -> tuple[dict | None, tuple | None, int]:
    """fingerprint(), once more when the file was swapped under it (ESTALE): an
    editor's atomic save does that, and is no reason to refuse the request."""
    try:
        return fingerprint(path)
    except OSError as e:
        if e.errno != errno.ESTALE:
            raise
        return fingerprint(path)


def stat_fingerprint(st: os.stat_result) -> dict:
    """A file too big to read: identified by its stat key, which any edit moves
    (a touch too). Counted as unknown (0 lines), shown as over_cap."""
    key = ":".join(map(str, stat_key(st)))
    return {"fingerprint": f"stat:{key}", "binary": False, "added": 0,
            "size": st.st_size, "unread": True}  # fmt: skip


async def run_limited(
    argv: list[str],
    env: dict | None,
    limit: int,
    on_chunk=None,
    stdin: bytes | None = None,
) -> tuple[int, bytes, str, bool]:
    """run_cmd with a ceiling on stdout: chunks go to on_chunk (a hash update)
    or are buffered; past `limit` bytes the whole session is killed and
    overflow is True. stderr keeps its last 64 KiB. Returns (rc, out, err,
    overflow); out is empty with on_chunk. `stdin` is fed to the process, if given."""
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip
    buf, total, overflow = bytearray(), 0, False
    err = bytearray()

    async def drain_err():
        while chunk := await proc.stderr.read(1 << 16):
            err.extend(chunk)
            del err[: -(1 << 16)]

    async def feed():
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the process exited (or was killed) before reading it all

    err_task = asyncio.create_task(drain_err())
    feed_task = asyncio.create_task(feed()) if stdin is not None else None
    try:
        while chunk := await proc.stdout.read(1 << 16):
            total += len(chunk)
            if total > limit:
                overflow = True
                agy_runner.kill_session(proc.pid)
                break
            if on_chunk:
                on_chunk(chunk)
            else:
                buf.extend(chunk)
        await err_task
        await proc.wait()
    except BaseException:
        agy_runner.kill_session(proc.pid)
        err_task.cancel()
        await proc.wait()
        with contextlib.suppress(BaseException):  # reap it: no "never retrieved"
            await err_task
        raise
    finally:
        if feed_task:
            feed_task.cancel()
    return proc.returncode, bytes(buf), err.decode(errors="replace"), overflow


def sync_dir() -> Path:
    return Path.home() / ".local" / "state" / "tk-daemon"


def daemon_version() -> str:
    """What the install last put on this box: `install.sh daemon` writes the
    revision it installed to ~/.local/share/tk-hub/VERSION, so it changes when a
    sync installs a new daemon. DAEMON_VERSION when there is none."""
    try:
        text = (Path.home() / ".local" / "share" / "tk-hub" / "VERSION").read_text()
    except OSError:
        return DAEMON_VERSION
    return text.strip() or DAEMON_VERSION


SYNC_LOG = re.compile(r"sync-(\d{8})T(\d{6})(\d{3})?Z\.log")


def sync_logs() -> list[tuple[str, Path]]:
    """(stamp, log) of the logs this daemon named, oldest first; others are ignored."""
    found = []
    for path in sync_dir().glob("sync-*.log"):
        if m := SYNC_LOG.fullmatch(path.name):
            found.append((m.group(1) + m.group(2) + (m.group(3) or "000"), path))
    found.sort()
    return [(path.stem.removeprefix("sync-"), path) for _, path in found]


async def sync_unit_active(stamp: str) -> bool:
    rc, out, _ = await run_cmd(["systemctl", "--user", "is-active", f"tk-sync-{stamp}"])
    return rc == 0 and out.strip() in ("active", "activating")


async def last_sync(starting: set[str] = frozenset()) -> dict | None:
    """The newest sync-<stamp>.log under the state dir with its .exit file. No
    .exit file: the sync is still running (unit active, or just being started) or
    was interrupted (unit gone: killed, rebooted)."""
    logs = sync_logs()
    if not logs:
        return None
    stamp, log_path = logs[-1]
    started = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"
    code_path = log_path.with_suffix(".exit")
    interrupted = False
    try:
        exit_code = int(code_path.read_text().strip())
        finished = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(code_path.stat().st_mtime)
        )
    except (OSError, ValueError):
        exit_code, finished = None, None
        interrupted = stamp not in starting and not await sync_unit_active(stamp)
    try:
        tail = "".join(
            log_path.read_text(errors="replace").splitlines(keepends=True)[-20:]
        )
    except OSError:
        tail = ""
    return {"started_at": started, "finished_at": finished, "exit_code": exit_code,
            "interrupted": interrupted, "log_path": str(log_path),
            "log_tail": tail}  # fmt: skip


async def run_cmd(
    argv: list[str], env: dict | None = None, cwd: str | None = None,
    errors: str = "replace",
) -> tuple[int, str, str]:  # fmt: skip
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
        agy_runner.kill_session(proc.pid)
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors=errors), err.decode(errors="replace")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# hub-meta.json is read-modify-written by several RPCs and synthesis runs: each read,
# change and write happens with no await in between, so on the daemon's one event loop
# no two interleave.
def read_meta(board: Path, name: str = "hub-meta.json") -> dict:
    f = board / name
    meta = json.loads(f.read_text()) if f.exists() else {}
    if not isinstance(meta, dict):
        raise ValueError(f"{f} is not a JSON object")  # noqa: TRY004
    return meta


def write_meta(board: Path, meta: dict, name: str = "hub-meta.json"):
    f = board / name
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    tmp.replace(f)


def ticket_meta(meta: dict, nn: str) -> dict:
    """meta's `tickets.<nn>`, created when missing."""
    tickets = meta.setdefault("tickets", {})
    if not isinstance(tickets, dict):
        raise ValueError("hub-meta.json: tickets is not a JSON object")  # noqa: TRY004
    if not isinstance(tickets.get(nn), dict):
        tickets[nn] = {}
    return tickets[nn]


def update_ticket_meta(board: Path, nn: str, fields: dict):
    meta = read_meta(board)
    ticket_meta(meta, nn).update(fields)
    write_meta(board, meta)


def synthesis_policy(board: dict) -> dict:
    """The board's resolved `synthesis` policy; {} when it, or the policy, is missing,
    null or not an object."""
    policy = board.get("policy")
    synthesis = policy.get("synthesis") if isinstance(policy, dict) else None
    return synthesis if isinstance(synthesis, dict) else {}


def synthesis_backends(policy: dict) -> list:
    """`synthesis.backends`, ["agy"] when missing, empty or not a list."""
    backends = policy.get("backends")
    return list(backends) if isinstance(backends, list) and backends else ["agy"]


def seconds(value) -> float | None:
    """`value` (a number, or a string of one) as finite seconds > 0, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def env_agy_timeout() -> float | None:
    """$TK_AGY_TIMEOUT as seconds; None when unset, or invalid (said once, here)."""
    raw = os.environ.get("TK_AGY_TIMEOUT", "")
    if raw == "":
        return None
    if (value := seconds(raw)) is None:
        log.warning("ignoring TK_AGY_TIMEOUT=%r: not a number of seconds > 0", raw)
    return value


def read_file(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


class Daemon:
    racy_test_s: float | None = None  # see main

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.uri, self.name, self.port = endpoint(cfg["url"])
        self.token: str | None = None
        self.sent: dict[str, dict] = {}  # board key -> the board last sent
        self.health: dict | None = None
        # get_diff: untracked path -> (stat key, fingerprint info)
        # (LRU, 4096 entries; entries of a worktree not seen in a request are dropped)
        self.fp_lock = threading.Lock()  # fp_cache is used from to_thread workers
        self.fp_cache: collections.OrderedDict[str, tuple[tuple, dict]] = (
            collections.OrderedDict()
        )
        # stamps of syncs whose systemd-run is still starting
        self.sync_starting: set[str] = set()
        self.need_full = True  # the next snapshot must be full (a new connection)
        self.export_start = 0  # where the next export starts (tk-export: resume)
        self.schema = Schema(json.loads(Path(cfg.get("schema", SCHEMA)).read_text()))
        self.lock = asyncio.Lock()
        self.ws = None  # the hub connection, while there is one (synthesis events)
        self.synthesizing: set[tuple[str, str]] = set()  # (board key, nn) running now
        self.synth_tasks: set[asyncio.Task] = set()  # runs started by a column move
        self.brief_dirty: set[str] = set()  # moved while its brief was running
        self.briefing: set[str] = set()  # board keys with a brief running now
        self.brief_timers: dict[str, asyncio.Task] = {}  # debounce, per board key
        self.agy_timeout = env_agy_timeout()  # the box's, over every board's policy
        # Launches take a slot under launch_lock, before tk.sh runs; the slot is
        # held (reserved) until a snapshot shows the ticket out of backlog, or
        # reservation_ttl_s after tk.sh launch returned.
        self.launch_lock = asyncio.Lock()
        # (board key, nn) -> monotonic time tk.sh launch returned; None: running
        self.reserved: dict[tuple[str, str], float | None] = {}
        self.launches: set[asyncio.Task] = set()  # launches running, for shutdown
        self.board_locks: dict[str, asyncio.Lock] = {}  # one launch per board at once
        self.stopping = False  # set on SIGTERM / SIGINT: queued launches never start
        # (board key, nn) where ticket-merger runs now: one at a time per ticket
        self.resolving: set[tuple[str, str]] = set()
        self.resolvers: set[asyncio.Task] = set()  # their tasks, for shutdown
        self.rpcs = {
            "rescan_boards": self.rpc_rescan_boards,
            "launch_ticket": self.rpc_launch_ticket,
            "say_agent": self.rpc_say_agent,
            "pane_tail": self.rpc_pane_tail,
            "set_archived": self.rpc_set_archived,
            "set_policy": self.rpc_set_policy,
            "refine_with_comment": self.rpc_refine_with_comment,
            "refresh_board_brief": self.rpc_refresh_board_brief,
            "set_task_done": self.rpc_set_task_done,
            "propose_memory": self.rpc_propose_memory,
            "open_pr": functools.partial(self.rpc_ship, "open_pr"),
            "ssh_push": functools.partial(self.rpc_ship, "ssh_push"),
            "merge_pr": functools.partial(self.rpc_ship, "merge_pr"),
            "resolve_nocode": functools.partial(self.rpc_ship, "resolve_nocode"),
            "resolve_ticket": functools.partial(self.rpc_ship, "resolve_ticket"),
            "verify_landed": self.rpc_verify_landed,
            "get_diff": self.rpc_get_diff,
            "sync_box": self.rpc_sync_box,
            "resolve_conflict": self.rpc_resolve_conflict,
            "finish_conflict": self.rpc_finish_conflict,
            "abort_conflict": self.rpc_abort_conflict,
        }

    # ---- export --------------------------------------------------------------------
    async def export(self) -> tuple[dict, set[str]]:
        env = {
            **os.environ,
            "TK_DEVBOX": self.cfg["devbox"],
            "TK_DAEMON_VERSION": daemon_version(),
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
        if isinstance(snap.get("health"), dict):
            snap["health"]["last_sync"] = await last_sync(self.sync_starting)
            snap["health"]["daemon_version"] = daemon_version()
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
            if self.fail_interrupted(snap["boards"]):  # the hub sees them failed now
                snap, failed = await self.export()
            boards = {b["key"]: b for b in snap["boards"]}
            for key in failed & self.sent.keys():
                boards[key] = self.sent[key]
            removed = sorted(self.sent.keys() - boards.keys())
            held = sorted(failed - boards.keys())
            moved = self.moves_to_synthesize(boards)
            for key in self.boards_that_moved(boards):
                self.schedule_brief(key)
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
            for key, nn in moved:
                self.spawn_synthesis(key, nn)
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

    async def tk(
        self, board: dict, *args: str, script: str | None = None
    ) -> tuple[int, str]:
        """tk.sh (or `script`, merge-conflict.sh) <verb> <board path> ... from the
        board's main checkout: (exit, output)."""
        verb, *rest = args
        rc, out, err = await run_cmd(
            [script or tk_sh(), verb, board["board_path"], *rest], cwd=board["root"]
        )
        return rc, out + err

    async def col(self, board: dict, nn: str) -> str:
        return (await self.row(board, nn)).get("col")

    async def row(self, board: dict, nn: str) -> dict:
        """The ticket's row now: `tk.sh export`, not the last snapshot; bounded
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
            # the hub maps this text to a 404 (app.py diff_failure_status)
            raise RpcRefused(f"no ticket {nn} on board {board['key']}")
        return found

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
    def log_orphan(slot: tuple[str, str], task: asyncio.Task, what: str = "launch"):
        """The outcome of a launch (or ship verb) whose rpc already answered (timed out)."""
        if task.cancelled():
            log.warning("%s %s %s cancelled after its rpc", what, *slot)
        elif (e := task.exception()) is not None:
            log.warning("%s %s %s failed after its rpc: %s", what, *slot, e)
        else:
            rc, out = task.result()
            tail = out.strip().splitlines()
            log.warning(
                "%s %s %s finished after its rpc: exit %s%s",
                what, *slot, rc, f" ({tail[-1]})" if tail else "",
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

    async def locked_tk(
        self,
        board: dict,
        nn: str,
        cols: tuple[str, ...] | None,
        *args: str,
        landed: bool = False,
        script: str | None = None,
        check: typing.Callable[[], None] | None = None,
        after: typing.Callable[[int, str], object] | None = None,
    ) -> tuple[int, str]:
        """tk.sh (or `script`) <args> under the board's queue and flock, after
        re-checking the ticket is in one of `cols` (any when None), with `landed`
        that its landed verdict is yes:*, and calling `check` (it raises RpcRefused
        to refuse). The work runs in a task of
        its own: the rpc timeout answers the hub but never kills a merge midway.
        `after(exit, output)` runs in that task once the queue is free, so it also
        runs for work that outlived its rpc."""

        async def work() -> tuple[int, str]:
            # Ship verbs share the per-board queue with launches: one waits
            # behind the other, by design.
            queue = self.board_locks.setdefault(board["key"], asyncio.Lock())
            async with queue:
                if self.stopping:  # its turn came during shutdown: don't start
                    raise RpcRefused("daemon stopping")
                lock = await board_lock(
                    board["board_path"], self.cfg["launch_lock_wait_s"]
                )
                try:
                    if self.stopping:  # the stop came while it waited on the flock
                        raise RpcRefused("daemon stopping")
                    found = await self.row(board, nn)
                    if cols is not None and (col := found.get("col")) not in cols:
                        raise RpcRefused(
                            f"ticket {nn} is {col}, not {' or '.join(cols)}"
                        )
                    if landed and not str(found.get("landed")).startswith("yes:"):
                        # tk.sh would rewrite the Status date of a resolved ticket.
                        raise RpcRefused(
                            f"ticket {nn} is not landed (landed: {found.get('landed')})"
                        )
                    if check is not None:
                        check()
                    done = await self.tk(board, *args, script=script)
                finally:
                    lock.close()
            if after is not None:
                after(*done)
            return done

        task = asyncio.create_task(work())
        self.launches.add(task)
        task.add_done_callback(self.launches.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            slot = (board["key"], nn)
            log.warning("%s %s outlived its rpc; it carries on", args[0], nn)
            task.add_done_callback(lambda t: self.log_orphan(slot, t, args[0]))
            raise

    async def rpc_ship(self, method: str, ws, params: dict) -> dict:
        """open_pr / ssh_push / merge_pr / resolve_nocode / resolve_ticket: the matching tk.sh
        verb under the board flock, after a col re-check. Gate failures of merge
        (10/11/12) come back by exit code with the gates in data.gates."""
        board, nn = self.board(params), params["nn"]
        verb, cols = SHIP_VERBS[method]
        extra = []
        if method == "resolve_nocode":
            reason = " ".join(params["reason"].split())
            if not reason:
                raise RpcRefused("resolve without code needs a reason")
            extra = ["--no-code", reason]
        merge = method == "merge_pr"
        rc, out = await self.locked_tk(
            board, nn, cols, verb, nn, *extra, landed=method == "resolve_ticket",
            # no merge while ticket-merger runs, or its merge waits for review
            check=(lambda: self.refuse_while_conflicted(board, nn)) if merge else None,
            # exit 10 sends ticket-merger, even when the merge outlived this rpc
            after=(lambda rc, _: rc == 10 and self.start_resolver(board, nn))
            if merge else None,
        )  # fmt: skip
        result = {"ok": rc == 0, "exit_code": rc, "output_tail": out}
        if method == "merge_pr":
            merged = any(line.startswith("MERGED ") for line in out.splitlines())
            gates = [
                {"gate": m[1], "status": m[2].lower(), "detail": m[3]}
                for line in out.splitlines()
                if (m := GATE_LINE.match(line.strip()))
            ]
            result["data"] = {"gates": gates, "merged": merged}
            if rc == 10:  # ticket-merger resolves it (started by `after`)
                result["data"]["resolving"] = True
                result["reason"] = f"{MERGE_EXITS[rc]}; ticket-merger is resolving it"
            elif rc in MERGE_EXITS:
                result["reason"] = MERGE_EXITS[rc]
            elif merged and rc != 0:
                tail = out.strip().splitlines()
                result["reason"] = (
                    f"merged, but resolve failed: {tail[-1] if tail else f'exit {rc}'}"
                )
        elif rc in SHIP_EXITS:
            result["reason"] = SHIP_EXITS[rc]
        return result

    async def rpc_verify_landed(self, ws, params: dict) -> dict:
        """tk.sh why: data.landed (the LANDED verdict) and data.open_pr (the
        OPEN PR line's text after the label)."""
        board, nn = self.board(params), params["nn"]
        rc, out = await self.locked_tk(board, nn, ("pr",), "why", nn)
        data = {"landed": None, "open_pr": None}
        for line in out.splitlines():
            if line.startswith("LANDED "):
                data["landed"] = line[len("LANDED ") :].strip()
            elif line.startswith("OPEN PR "):
                data["open_pr"] = line[len("OPEN PR ") :].strip()
        return {"ok": rc == 0, "exit_code": rc, "output_tail": out, "data": data}

    # ---- merge conflicts: ticket-merger, then the developer's decision ---------------
    # The ticket's hub-meta `merge_conflict` says where it stands: resolving (the agent
    # runs), review (resolved and staged, waiting for Commit merge / Abort), failed
    # (the merge was aborted: reason, output_tail), committed, aborted, up_to_date.
    # Only finish_conflict, the developer's click, ever commits and pushes.
    def merge_conflict(self, board: dict, nn: str) -> dict:
        """The ticket's hub-meta merge_conflict, {} when none."""
        mc = ticket_meta(read_meta(Path(board["board_path"])), nn).get("merge_conflict")
        return mc if isinstance(mc, dict) else {}

    def record_conflict(self, board: dict, nn: str, fresh: bool = False, **fields):
        """Update (or, `fresh`, replace) the ticket's hub-meta merge_conflict."""
        path = Path(board["board_path"])
        meta = read_meta(path)
        entry = ticket_meta(meta, nn)
        old = entry.get("merge_conflict")
        entry["merge_conflict"] = {
            **(old if isinstance(old, dict) and not fresh else {}),
            **fields,
        }
        write_meta(path, meta)

    def refuse_while_conflicted(self, board: dict, nn: str):
        """No merge (nor a second resolver) while ticket-merger runs or its resolved
        merge waits for the developer."""
        if (board["key"], nn) in self.resolving:
            raise RpcRefused(
                f"ticket {nn} is resolving a merge conflict (ticket-merger)"
            )
        if self.merge_conflict(board, nn).get("state") == "review":
            raise RpcRefused(
                f"ticket {nn} has a resolved merge to review: Commit merge or Abort it"
            )

    def start_resolver(self, board: dict, nn: str) -> bool:
        """Resolve the ticket's conflict in a task of its own (it outlives the rpc);
        False when one already runs for it."""
        slot = (board["key"], nn)
        if slot in self.resolving or self.stopping:
            return False
        self.resolving.add(slot)
        try:
            self.record_conflict(
                board, nn, fresh=True, state="resolving", started_at=utc_now(),
                finished_at=None, base=board.get("base") or None, files=[],
                summary=None, reason=None, output_tail=None,
            )  # fmt: skip
        except BaseException:
            self.resolving.discard(slot)
            raise
        task = asyncio.create_task(self.resolve(board, nn))
        self.resolvers.add(task)
        task.add_done_callback(self.resolvers.discard)
        return True

    @staticmethod
    def merger_prompt(script: str, board: dict, ticket: dict, files: list[str]) -> str:
        """ticket-merger's brief (agents/ticket-merger.md): the script, board, NN, the
        ticket's intent, and that start already ran and nobody approved a commit."""
        nn, path, base = ticket["nn"], board["board_path"], board.get("base") or "?"
        listed = "\n".join(f"  {f}" for f in files)
        return (
            f"Resolve the merge conflict of ticket {nn} ({ticket.get('title', '')}) "
            f"on board {path}.\n"
            f"merge-conflict.sh: {script}\nBoard: {path}\nNN: {nn}\nBase: {base}\n"
            f"Ticket file (its intent): {ticket.get('file') or '?'}\n\n"
            f"The tk-hub daemon already ran `{script} start {path} {nn}`: {base} is "
            f"merged into this worktree, not committed, and these files conflict:\n"
            f"{listed}\n\n"
            "Do not run start again. Resolve them as your first call says (steps 2 to "
            f"5): `{script} status {path} {nn}` must end with UNMERGED none and "
            "MARKERS none. Return each file and how you resolved it, anything you "
            "refused to choose, and the checks you ran with what they printed.\n"
            "The developer has NOT approved a commit: never run finish or abort, never "
            "commit or push. They review the merge in the hub and commit it from there."
        )

    async def resolve(self, board: dict, nn: str):
        """merge-conflict.sh start; on conflicts ticket-merger in the ticket's worktree
        (bounded by rpc_synthesis_timeout_s), then status. Resolved: the ticket comes
        back for review (state review) and a synthesis run covers the merge. Anything
        else, after start began a merge: abort, state failed with the why. UP TO DATE
        (the forge's conflict flag was stale): the merge is retried once. Stopped while
        start runs (it carries on, shielded): left `resolving`, so the next daemon's
        first export aborts it (fail_interrupted_resolve)."""
        slot, script = (board["key"], nn), merge_conflict_sh()
        merging = False  # start began a merge: abort it unless it ends in review
        starting = False  # start is running: its outcome is unknown when stopped
        fields: dict | None = {}
        try:
            ticket = await self.row(board, nn)
            wt = ticket.get("worktree") or ""
            starting = True
            rc, out = await self.locked_tk(
                board, nn, ("pr",), "start", nn, script=script
            )
            starting = False
            if rc == 0 and out.lstrip().startswith("UP TO DATE"):
                rc2, out2 = await self.locked_tk(board, nn, ("pr",), "merge", nn)
                retried = "merged" if rc2 == 0 else MERGE_EXITS.get(rc2, f"exit {rc2}")
                fields = {"state": "up_to_date", "output_tail": out2[-TAIL:],
                          "reason": "the branch already has the base; the merge was "
                                    f"retried: {retried}"}  # fmt: skip
            elif rc not in (0, 2):
                fields = {"state": "failed", "output_tail": out[-TAIL:],
                          "reason": f"merge-conflict.sh start refused (exit {rc})"}  # fmt: skip
            else:
                merging = True
                files = [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]
                fields = {"files": files}
                summary, why = f"No conflicts to resolve:\n{out}", None
                said = summary  # what the agent printed, stderr too: the failure's tail
                if rc == 2:
                    limit = self.cfg["rpc_synthesis_timeout_s"]
                    prompt = self.merger_prompt(script, board, ticket, files)
                    head = await self.head(wt)
                    try:
                        arc, aout, aerr = await asyncio.wait_for(
                            run_cmd(merger_argv(script, wt, prompt), cwd=wt), limit
                        )
                        summary, said = aout[-MERGER_SUMMARY:], aout + aerr
                        if arc != 0:
                            why = f"ticket-merger failed (exit {arc})"
                        elif head and (now := await self.head(wt)) != head:
                            why = (
                                "ticket-merger committed against instructions (HEAD "
                                f"{head[:12]} -> {(now or '?')[:12]})"
                            )
                    except TimeoutError:
                        said, why = "", f"ticket-merger timed out after {limit:g}s"
                src, sout = await self.tk(board, "status", nn, script=script)
                if why is None and not (
                    src == 0
                    and re.search(r"^UNMERGED none$", sout, re.MULTILINE)
                    and re.search(r"^MARKERS none$", sout, re.MULTILINE)
                ):
                    why = "the merge is still unresolved"
                if why is None:
                    fields |= {"state": "review", "summary": summary,
                               "output_tail": sout[-TAIL:]}  # fmt: skip
                    merging = False
                else:
                    fields |= {"state": "failed", "reason": f"{why}; the merge was aborted",
                               "output_tail": f"{said}\n{sout}"[-TAIL:]}  # fmt: skip
        except asyncio.CancelledError:  # the daemon stopping
            fields = (
                None  # left resolving: the next daemon aborts whatever start did
                if starting
                else {"state": "failed",
                      "reason": "interrupted: the daemon stopped mid-resolve"}
            )  # fmt: skip
            raise
        except Exception as e:  # noqa: BLE001 — recorded, shown on the ticket
            log.warning("resolving %s %s: %s: %s", *slot, type(e).__name__, e)
            fields = {"state": "failed", "reason": f"resolver error: {e}"}
        finally:
            if merging:  # never leave a half-merged worktree
                with contextlib.suppress(Exception):
                    arc, aout = await self.tk(board, "abort", nn, script=script)
                    log.info("aborted the merge of %s %s: exit %s", *slot, arc)
                    if arc != 0:
                        fields["reason"] = (
                            f"{fields.get('reason')}; abort failed (exit {arc}): {aout.strip()[-200:]}"
                        )
            if fields is not None:
                with contextlib.suppress(Exception):
                    self.record_conflict(board, nn, **fields, finished_at=utc_now())
            self.resolving.discard(slot)
        if fields.get("state") == "review":
            self.spawn_synthesis(
                board["key"], nn,
                "ticket-merger merged the base into this branch to resolve a conflict; "
                "the merge is staged, not committed, and waits for the developer's "
                "review. Cover the merged result. Its answer:\n"
                + fields.get("summary", ""),
            )  # fmt: skip
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.sync(self.ws, full=False)

    async def head(self, wt: str) -> str | None:
        """The worktree's HEAD sha; None when git can't say."""
        with contextlib.suppress(OSError):
            rc, out, _ = await self.git_run(wt, "rev-parse", "--verify", "HEAD")
            if rc == 0:
                return out.strip()
        return None

    async def rpc_resolve_conflict(self, ws, params: dict) -> dict:
        """Send ticket-merger on a pr ticket's conflict (merge_pr does it on exit 10)."""
        board, nn = self.board(params), params["nn"]
        self.refuse_while_conflicted(board, nn)
        if (col := await self.col(board, nn)) != "pr":
            raise RpcRefused(f"ticket {nn} is {col}, not pr")
        self.start_resolver(board, nn)
        return {"ok": True, "exit_code": 0,
                "output_tail": f"ticket-merger is resolving {nn}'s conflict"}  # fmt: skip

    def conflict_in_review(self, board: dict, nn: str, also: tuple[str, ...] = ()):
        """Refuses unless the ticket's resolved merge waits for the developer (or its
        merge_conflict state is one of `also`)."""
        if (board["key"], nn) in self.resolving:
            raise RpcRefused(
                f"ticket {nn} is resolving a merge conflict (ticket-merger)"
            )
        if self.merge_conflict(board, nn).get("state") not in ("review", *also):
            raise RpcRefused(f"ticket {nn} has no resolved merge waiting for review")

    async def rpc_finish_conflict(self, ws, params: dict) -> dict:
        """Commit merge: merge-conflict.sh finish (commit the staged merge, push it,
        never force), the only commit and push the hub causes; params.confirm is the
        developer's explicit yes. Then the ticket is back to pr, its gates to re-run."""
        board, nn = self.board(params), params["nn"]
        rc, out = await self.locked_tk(
            board, nn, ("pr",), "finish", nn, script=merge_conflict_sh(),
            check=lambda: self.conflict_in_review(board, nn),
        )  # fmt: skip
        result = {"ok": rc == 0, "exit_code": rc, "output_tail": out}
        if rc == 0 or "merge commit is made" in out:  # committed, pushed or not
            reason = (
                None if rc == 0 else "committed, but the push failed: push it by hand"
            )
            self.record_conflict(board, nn, state="committed", reason=reason,
                                 output_tail=out[-TAIL:], finished_at=utc_now())  # fmt: skip
            if reason:
                result["reason"] = reason
        else:
            result["reason"] = (
                "not committed: still unresolved, or changes outside the merge"
                if rc == 4 else f"merge-conflict.sh finish failed (exit {rc})"
            )  # fmt: skip
            self.record_conflict(board, nn, reason=result["reason"],
                                 output_tail=out[-TAIL:])  # fmt: skip
        return result

    async def rpc_abort_conflict(self, ws, params: dict) -> dict:
        """Abort: merge-conflict.sh abort puts the worktree back; the ticket stays pr,
        the conflict noted. Also on a failed resolve, whose own abort may have failed
        (abort with no merge in progress changes nothing)."""
        board, nn = self.board(params), params["nn"]
        rc, out = await self.locked_tk(
            board, nn, None, "abort", nn, script=merge_conflict_sh(),
            check=lambda: self.conflict_in_review(board, nn, also=("failed",)),
        )  # fmt: skip
        if rc == 0:
            self.record_conflict(
                board, nn, state="aborted", finished_at=utc_now(), output_tail=out[-TAIL:],
                reason="aborted by the developer: the PR still conflicts with the base",
            )  # fmt: skip
        return {"ok": rc == 0, "exit_code": rc, "output_tail": out}

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

    async def git_run(self, wt: str, *args: str) -> tuple[int, str, str]:
        """Read-only git in the ticket's worktree. GIT_OPTIONAL_LOCKS=0 keeps
        git from refreshing (writing) the index on the way; no system or user
        config, so nothing of the dev's changes the output (or the hash). For
        output known to be small; anything that may be big goes through
        git_limited. Run get_diff's way (see git_flags)."""
        return await run_cmd(
            ["git", "-C", wt, *self.git_flags(), *args], env={**os.environ, **GIT_ENV}
        )

    def git_flags(self) -> list[str]:
        """`-c` flags of every get_diff git call. core.bigFileThreshold bounds what git
        loads whole (past it a file is binary to diff: "Binary files differ", with
        the full-index blob ids still moving the hash), the same on every call so
        the listings and the hash agree. The repo-local settings that make git skip
        reading a file it thinks unchanged are pinned to git's defaults (trustctime,
        checkStat) or off (fsmonitor), so a same-size edit with the mtime put back
        cannot hide."""
        n = self.cfg["diff_max_untracked_file_bytes"]
        return [
            "-c", f"core.bigFileThreshold={n}",
            "-c", "core.trustctime=true",
            "-c", "core.checkStat=default",
            "-c", "core.fsmonitor=false",
        ]  # fmt: skip

    async def user_path_config(self, wt: str, key: str) -> list[str]:
        """`-c <key>=<path>` for a git call, as the dev's own git config says
        (read-only `git config --get`; the calls run without that config): used
        for core.excludesFile (ls-files: what they gitignore globally stays hidden)
        and core.attributesFile (check-attr: their global -diff/binary rules still
        apply). Nothing when unset."""
        rc, out, _ = await run_cmd(
            ["git", "-C", wt, "config", "--type=path", "--get", key],
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            errors="surrogateescape",  # a non-UTF-8 path goes back byte for byte
        )
        path = out.strip()
        return ["-c", f"{key}={path}"] if rc == 0 and path else []

    async def git_limited(
        self, wt: str, args: tuple, limit: int, on_chunk=None,
        stdin: bytes | None = None, errors: str = "replace",
    ) -> tuple[int, str, str, bool]:  # fmt: skip
        """git with stdout capped at `limit` bytes (see run_limited): the last
        item is True when it was cut off and killed. Output is decoded with
        `errors`: "replace" for text that is shown, "surrogateescape" for file
        names, which must go back to the disk byte for byte."""
        rc, out, err, over = await run_limited(
            ["git", "-C", wt, *self.git_flags(), *args],
            {**os.environ, **GIT_ENV}, limit, on_chunk, stdin,
        )  # fmt: skip
        return rc, out.decode(errors=errors), err, over

    async def git_diff(
        self, wt: str, *args: str, stdin: bytes | None = None, config: tuple = ()
    ) -> str:
        """Small output (names, numstat, ls-files), bounded by diff_max_output_bytes:
        a ticket with more than that is refused, not buffered."""
        rc, out, err, over = await self.git_limited(
            wt, (*config, *args), self.cfg["diff_max_output_bytes"], stdin=stdin,
            errors="surrogateescape",
        )  # fmt: skip
        if over:
            raise RpcRefused(
                f"git {args[0]} output is too large (over "
                f"{self.cfg['diff_max_output_bytes']} bytes): too many changes"
            )
        if rc != 0:
            raise RpcRefused(f"git {args[0]} failed (exit {rc}): {err.strip()[-200:]}")
        return out

    def diff_limit(self) -> int:
        """Bytes of one file's diff worth reading: past diff_cap_chars chars (at most
        4 bytes each) it is over_cap anyway; never past diff_max_output_bytes."""
        return min(self.cfg["diff_cap_chars"] * 4, self.cfg["diff_max_output_bytes"])

    async def untracked_diff(self, wt: str, path: str) -> str | None:
        """One untracked file's diff as an addition, None when over the limit.
        --no-index exits 1 when the files differ; with anything on stderr that is a
        failure, not a diff."""
        rc, out, err, over = await self.git_limited(
            wt,
            ("diff", *DIFF_FLAGS, "--no-index", "--", "/dev/null", path),
            self.diff_limit(),
        )
        if over:
            return None
        if rc not in (0, 1) or (rc == 1 and err.strip()):
            raise RpcRefused(
                f"git diff of {shown_path(path)} failed (exit {rc}): {err.strip()[-200:]}"
            )
        return out

    def untracked_info(self, path: Path, budget: int) -> tuple[dict | None, int]:
        """fingerprint(path) within the budget of bytes left to read this request,
        and the bytes it is charged. Whether a file is read or stat-fingerprinted (over
        diff_max_untracked_file_bytes, or past the budget) depends on sizes alone,
        decided before the cache is asked, so diff_hash does not drift with cache
        state: the cache (same stat key, and not racily clean) only saves the
        read, a hit is charged like one. A touch on a stat-fingerprinted file moves
        its hash."""
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            return fingerprint_retrying(path)[0], 0
        if (
            st.st_size > self.cfg["diff_max_untracked_file_bytes"]
            or st.st_size > budget
        ):
            return stat_fingerprint(st), 0
        key = os.fspath(path)
        with self.fp_lock:
            hit = self.fp_cache.get(key)
            if hit and hit[0] == stat_key(st):
                self.fp_cache.move_to_end(key)
                return hit[1], st.st_size
        info, fkey, now = fingerprint_retrying(path)
        if info is not None and fkey and now - max(fkey[2], fkey[3]) > self.racy_ns():
            with self.fp_lock:
                self.fp_cache[key] = (fkey, info)
                self.fp_cache.move_to_end(key)
                while len(self.fp_cache) > 4096:
                    self.fp_cache.popitem(last=False)
        return info, st.st_size

    def racy_ns(self) -> float:
        """The racy window in ns: configured, never under DIFF_RACY_FLOOR_S (only
        the test key goes lower)."""
        if self.racy_test_s is not None:
            return self.racy_test_s * 1e9
        return max(self.cfg["diff_racy_window_s"], DIFF_RACY_FLOOR_S) * 1e9

    async def file_diff(
        self, wt: str, tracked: tuple, rev: tuple, f: dict, untracked: bool,
        over_cap: set[str],
    ) -> str | None:  # fmt: skip
        """One text file's diff, None when it is over the cap: decided from sizes and
        line counts before git runs where possible, else by the output limit."""
        path = f["path"]
        if untracked:
            if path in over_cap:
                return None
            try:
                return await self.untracked_diff(wt, path)
            except RpcRefused:
                if not (Path(wt) / path).exists():
                    # the hub maps this text to a 404 (app.py diff_failure_status)
                    raise RpcRefused(
                        f"{shown_path(path)} is gone from the worktree"
                    ) from None
                raise
        # a diff has at least two chars (a sign and a newline) per changed line
        if (f["added"] + f["removed"]) * 2 > self.cfg["diff_cap_chars"]:
            return None
        paths = [f["old_path"], path] if "old_path" in f else [path]
        rc, out, err, over = await self.git_limited(
            wt, (*tracked, *rev, *paths), self.diff_limit()
        )
        if over:
            return None
        if rc != 0:
            raise RpcRefused(f"git diff failed (exit {rc}): {err.strip()[-200:]}")
        return out

    async def apply_binary_attrs(self, wt: str, files: dict, untracked: list) -> None:
        """Untracked files git would treat as binary by .gitattributes (-diff, or the
        `binary` macro) are marked so: one read-only `git check-attr --stdin` over
        them (a tracked file's numstat already says). Their added count is 0."""
        paths = [p for p in untracked if p in files and not files[p].get("nested_repo")]
        if not paths:
            return
        out = await self.git_diff(
            wt, "check-attr", "-z", "--stdin", "diff", "binary",
            stdin=b"".join(os.fsencode(p) + b"\0" for p in paths),
            config=tuple(await self.user_path_config(wt, "core.attributesFile")),
        )  # fmt: skip
        parts = out.split("\0")
        for path, attr, value in zip(
            parts[0::3], parts[1::3], parts[2::3], strict=False
        ):
            f = files.get(path)
            if f and (
                (attr == "diff" and value == "unset")
                or (attr == "binary" and value == "set")
            ):
                f["binary"], f["added"] = True, 0

    async def rpc_get_diff(self, ws, params: dict) -> dict:
        """The ticket's change: its worktree's uncommitted diff against its base
        (with against=head: against its HEAD, data.base that sha) (untracked files
        as additions). Without `file`: data.files (path, status
        M/A/D/R, old_path on R, additions, deletions, binary; nested_repo on an
        untracked nested repo, listed with a trailing slash) and data.diff_hash;
        with it, that file's unified diff in data.diff, or a data.stub ("binary",
        "over_cap", "nested_repo") instead. Only reads: diff, ls-files and
        rev-parse, no add, no index write."""
        board, nn = self.board(params), params["nn"]
        ticket = await self.row(board, nn)
        wt, base = ticket.get("worktree"), ticket.get("base")
        if not wt or not Path(wt).is_dir():
            # the hub maps these texts to a 409 (app.py diff_failure_status)
            raise RpcRefused(f"ticket {nn} has no worktree to diff")
        if params.get("against") == "head":  # e.g. a merge in progress (ticket 34)
            rc, out, err = await self.git_run(wt, "rev-parse", "--verify", "HEAD")
            if rc != 0:
                raise RpcRefused(f"git rev-parse HEAD failed: {err.strip()[-200:]}")
            base = out.strip()
        if not base:
            raise RpcRefused(f"ticket {nn} has no base to diff against")
        if not re.fullmatch(r"[0-9a-f]{7,64}", base):
            raise RpcRefused(f"ticket {nn}'s base {base[:40]!r} is not a commit sha")
        tracked = ("diff", *DIFF_FLAGS, "-M")
        rev = ("--end-of-options", base, "--")
        # the listings take no -U/context flag: that would add the whole patch
        listing = (
            "diff", "--no-color", "--no-ext-diff", "--no-textconv",
            "--ignore-submodules=none", "-M",
        )  # fmt: skip
        names = (await self.git_diff(wt, *listing, "--name-status", "-z", *rev)).split(
            "\0"
        )
        nums = (await self.git_diff(wt, *listing, "--numstat", "-z", *rev)).split("\0")
        files: dict[str, dict] = {}
        i = 0
        while i < len(names) and names[i]:
            code = names[i][0]
            if code in "RC":
                old, path = names[i + 1], names[i + 2]
                i += 3
            else:
                old, path = None, names[i + 1]
                i += 2
            f = {"path": path, "status": {"T": "M", "C": "A", "U": "M"}.get(code, code)}
            if old and code == "R":
                f["old_path"] = old
            files[path] = f
        i = 0
        while i < len(nums) and nums[i]:
            add, rem, path = nums[i].split("\t", 2)
            i += 1
            if not path:  # a rename: old and new paths follow
                path = nums[i + 1]
                i += 2
            f = files.get(path)
            if f is not None:
                f["binary"] = add == "-"
                f["added"] = 0 if add == "-" else int(add)
                f["removed"] = 0 if rem == "-" else int(rem)
        tracked_digest = hashlib.sha256()
        rc, _, err, over = await self.git_limited(
            wt, (*tracked, *rev), self.cfg["diff_max_hash_bytes"],
            tracked_digest.update,
        )  # fmt: skip
        if over:
            raise RpcRefused(
                f"the tracked diff is too large to hash (over "
                f"{self.cfg['diff_max_hash_bytes']} bytes)"
            )
        if rc != 0:
            raise RpcRefused(f"git diff failed (exit {rc}): {err.strip()[-200:]}")
        # the tracked diff enters the hash as a fixed-size, tagged digest, so no
        # bytes of it can pass for (or merge with) the entries that follow
        digest = hashlib.sha256(b"tracked\0" + tracked_digest.digest())
        over_cap: set[str] = set()  # untracked files known to exceed the cap unread
        untracked = sorted(
            filter(
                None,
                (
                    await self.git_diff(
                        wt,
                        "ls-files",
                        "--others",
                        "--exclude-standard",
                        "-z",
                        config=tuple(
                            await self.user_path_config(wt, "core.excludesFile")
                        ),
                    )
                ).split("\0"),
            )
        )
        budget = self.cfg["diff_max_untracked_bytes"]
        for path in untracked:
            if path.endswith("/"):  # a nested repo: its HEAD stands for its content
                rc, head, _ = await self.git_run(wt, "-C", path, "rev-parse", "HEAD")
                digest.update(
                    b"nested\0" + os.fsencode(path)
                    + f"\0{head.strip() if rc == 0 else '?'}\0".encode()
                )  # fmt: skip
                files[path] = {"path": path, "status": "A", "binary": False,
                               "added": 0, "removed": 0, "nested_repo": True}  # fmt: skip
                continue
            try:
                info, read = await asyncio.to_thread(
                    self.untracked_info, Path(wt) / path, budget
                )
                budget -= read
            except FileNotFoundError:
                continue  # gone since ls-files: not part of the change any more
            except OSError as e:
                raise RpcRefused(
                    f"cannot read untracked {shown_path(path)}: {e.strerror}"
                ) from e
            if info is None:
                continue  # a fifo, socket...: nothing to show, nothing to read
            digest.update(
                b"untracked\0" + os.fsencode(path)
                + f"\0{info['fingerprint']}\0".encode()
            )  # fmt: skip
            files[path] = {"path": path, "status": "A", "binary": info["binary"],
                           "added": info["added"], "removed": 0}  # fmt: skip
            if info["size"] > self.cfg["diff_cap_chars"] or info.get("unread"):
                over_cap.add(path)
        seen = {os.fspath(Path(wt) / p) for p in untracked}
        prefix = os.fspath(Path(wt)) + os.sep
        with self.fp_lock:  # gone, or no longer untracked
            for key in [k for k in self.fp_cache if k.startswith(prefix)]:
                if key not in seen:
                    self.fp_cache.pop(key, None)
        await self.apply_binary_attrs(wt, files, untracked)
        data = {"base": base, "diff_hash": f"sha256:{digest.hexdigest()}"}
        want = params.get("file")
        if want is None:
            listed = sorted(files.values(), key=lambda f: f["path"])
            cap = self.cfg["diff_max_files"]
            data["files"] = [shown_file(f) for f in listed[:cap]]
            data["truncated"] = len(listed) > cap
            data["total_files"] = len(listed)
            return {"ok": True, "exit_code": 0, "output_tail": "", "data": data}
        # `want` is a name as listed (shown_path): the real path where it is valid
        # UTF-8; otherwise the file whose shown name it is, refused when several
        # share it (names differing only in invalid bytes)
        f = files.get(want)
        if f is None:
            named = [files[p] for p in sorted(files) if shown_path(p) == want]
            if len(named) > 1:
                # the hub maps this text to a 409 (app.py diff_failure_status)
                raise RpcRefused(
                    f"{want} is ambiguous in ticket {nn}'s diff: "
                    f"{len(named)} files share that shown name"
                )
            f = named[0] if named else None
        if f is None:
            # the hub maps this text to a 404 (app.py diff_failure_status)
            raise RpcRefused(f"{want} is not in ticket {nn}'s diff")
        path = f["path"]
        data |= shown_file(f)
        data["diff"], data["stub"] = "", None
        if f.get("nested_repo"):
            data["stub"] = "nested_repo"
        elif f["binary"]:
            data["stub"] = "binary"
        else:
            text = await self.file_diff(
                wt, tracked, rev, f, path in untracked, over_cap
            )
            if text is None or len(text) > self.cfg["diff_cap_chars"]:
                data["stub"] = "over_cap"
            else:
                data["diff"] = text
        return {"ok": True, "exit_code": 0, "output_tail": "", "data": data}

    async def rpc_set_archived(self, ws, params: dict) -> dict:
        key, flag = params.get("board_key"), params.get("archived")
        if not isinstance(flag, bool):
            raise ValueError(f"bad set_archived params: {params!r}")  # noqa: TRY004
        b = self.sent.get(key)
        if b is None:
            raise ValueError(f"no board {key} on this box")
        board = Path(b["board_path"])
        meta = read_meta(board)
        meta["archived"] = flag
        write_meta(board, meta)
        return {
            "ok": True,
            "exit_code": 0,
            "output_tail": f"{key} archived={str(flag).lower()}",
        }

    async def rpc_sync_box(self, ws, params: dict) -> dict:
        """`devbox sync`, started detached under `systemd-run --user` so it
        outlives this daemon (a sync reinstalls it); refused while a sync already
        runs, and while a ticket is working or in review unless `force`. Returns at
        once: last_sync reports the outcome."""
        last = await last_sync(self.sync_starting)
        if last and last["exit_code"] is None and not last["interrupted"]:
            raise RpcRefused(f"a sync is already running (log {last['log_path']})")
        busy = sum(
            1
            for b in self.sent.values()
            for t in b.get("tickets") or []
            if t.get("col") in RUNNING
        )
        if busy and not params.get("force"):
            raise RpcRefused(
                f"{busy} ticket(s) working or in review on this box: sync anyway to force it"
            )
        sync_dir().mkdir(parents=True, exist_ok=True)
        ms = int(time.time() * 1000)
        while True:  # never reuse a stamp: the log is created exclusively
            stamp = (
                time.strftime("%Y%m%dT%H%M%S", time.gmtime(ms / 1000))
                + f"{ms % 1000:03d}Z"
            )
            log_path = sync_dir() / f"sync-{stamp}.log"
            try:
                log_path.open("x").close()
                break
            except FileExistsError:
                ms += 1
        code_path = log_path.with_suffix(".exit")
        code_path.unlink(missing_ok=True)
        script = 'devbox sync >"$0" 2>&1; echo $? >"$1"'
        self.sync_starting.add(stamp)
        try:
            rc, out, err = await run_cmd(
                ["systemd-run", "--user", "--collect", f"--unit=tk-sync-{stamp}",
                 "sh", "-c", script, str(log_path), str(code_path)]
            )  # fmt: skip
        finally:
            self.sync_starting.discard(stamp)
        if rc != 0:
            log_path.unlink(missing_ok=True)
            raise RpcRefused(f"systemd-run failed: {(err or out).strip()[-300:]}")
        return {
            "ok": True,
            "exit_code": 0,
            "output_tail": f"devbox sync started, log {log_path}",
        }

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
        tk = tk_sh()
        # Each value as JSON, so tk.sh reads back the type it was sent (quoting keeps a
        # string a string even when it looks like JSON); None is `key=`, which removes
        # the board override.
        args = [f"{k}={'' if v is None else json.dumps(v)}" for k, v in values.items()]
        code, out, err = await run_cmd(
            [tk, "policy-set", board["board_path"], *args], cwd=board["root"]
        )
        return {"ok": code == 0, "exit_code": code, "output_tail": out + err}

    def board_ticket(self, params: dict) -> tuple[dict, dict]:
        """(board, ticket) named by params' board_key and nn, as last sent."""
        key, nn = params.get("board_key"), params.get("nn")
        board = self.sent.get(key) if isinstance(key, str) else None
        if board is None:
            raise ValueError(f"no board {key!r} on this box")
        ticket = next((t for t in board["tickets"] if t["nn"] == nn), None)
        if ticket is None:
            raise ValueError(f"no ticket {nn!r} on board {key}")
        return board, ticket

    async def rpc_refine_with_comment(self, ws, params: dict) -> dict:
        board, ticket = self.board_ticket(params)
        comment = params.get("comment", "")
        if not isinstance(comment, str):
            raise ValueError("comment must be a string")  # noqa: TRY004
        try:
            result = await self.synthesize(board, ticket, comment)
        except agy_runner.SynthesisFailed as e:
            return {"ok": False, "exit_code": None, "output_tail": "",
                    "reason": f"synthesis failed: {e}"}  # fmt: skip
        return {
            "ok": True,
            "exit_code": 0,
            "output_tail": f"{board['key']} #{ticket['nn']}: synthesized by {result.backend}",
        }

    async def rpc_refresh_board_brief(self, ws, params: dict) -> dict:
        board = self.board(params)
        if board["key"] in self.briefing:
            raise RpcRefused(f"the board brief of {board['key']} is already running")
        if (timer := self.brief_timers.pop(board["key"], None)) is not None:
            timer.cancel()  # this run covers what it was waiting for
        try:
            result = await self.refresh_brief(board)
        except agy_runner.SynthesisFailed as e:
            return {"ok": False, "exit_code": None, "output_tail": "",
                    "reason": f"synthesis failed: {e}"}  # fmt: skip
        return {
            "ok": True,
            "exit_code": 0,
            "output_tail": f"{board['key']}: board brief by {result.backend}",
        }

    async def rpc_set_task_done(self, ws, params: dict) -> dict:
        board, ticket = self.board_ticket(params)
        text, done = params.get("text"), params.get("done")
        if not isinstance(text, str) or not isinstance(done, bool):
            raise ValueError(f"bad set_task_done params: {params!r}")  # noqa: TRY004
        path = Path(board["board_path"])
        meta = read_meta(path)
        entry = ticket_meta(meta, ticket["nn"])
        try:
            entry["manual_tasks"] = agy_runner.set_task_done(
                entry.get("manual_tasks") or [], text, done
            )
        except KeyError:
            raise ValueError(f"no checklist item {text!r} on #{ticket['nn']}") from None
        write_meta(path, meta)
        tick = "done" if done else "not done"
        return {
            "ok": True,
            "exit_code": 0,
            "output_tail": f"#{ticket['nn']} {text}: {tick}",
        }

    # ---- synthesis -----------------------------------------------------------------
    def moves_to_synthesize(self, boards: dict) -> list[tuple[str, str]]:
        """(key, nn) of each ticket whose col moved into needs/human/pr since the last
        export; a board or ticket seen for the first time has not moved, an archived
        board is left alone."""
        moved = []
        for key, b in boards.items():
            old = self.sent.get(key)
            if old is None or b["hub_meta"].get("archived"):
                continue
            before = {t["nn"]: t["col"] for t in old["tickets"]}
            moved += [
                (key, t["nn"])
                for t in b["tickets"]
                if t["col"] in SYNTHESIS_COLUMNS
                and before.get(t["nn"], t["col"]) != t["col"]
            ]
        return moved

    def boards_that_moved(self, boards: dict) -> list[str]:
        """Keys of the boards where a known ticket changed column since the last
        export (any column, unlike moves_to_synthesize); archived boards are left alone."""
        moved = []
        for key, b in boards.items():
            old = self.sent.get(key)
            if old is None or b["hub_meta"].get("archived"):
                continue
            before = {t["nn"]: t["col"] for t in old["tickets"]}
            if any(before.get(t["nn"], t["col"]) != t["col"] for t in b["tickets"]):
                moved.append(key)
        return moved

    def schedule_brief(self, key: str):
        """Refresh the brief of board `key` once no ticket has moved for
        brief_debounce_s: each move restarts the wait."""
        if (timer := self.brief_timers.pop(key, None)) is not None:
            timer.cancel()

        async def later():
            try:
                await asyncio.sleep(self.cfg["brief_debounce_s"])
                self.brief_timers.pop(key, None)
                board = self.sent.get(key)
                if not board or board["hub_meta"].get("archived"):
                    return
                if key in self.briefing:
                    self.brief_dirty.add(key)  # the running one re-runs when it ends
                    return
                try:
                    await self.refresh_brief(board)
                except agy_runner.SynthesisFailed:
                    pass  # recorded, and told to the hub
                if self.ws is not None:
                    await self.sync(self.ws, full=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — logged; the next move tries again
                log.warning("board brief of %s: %s: %s", key, type(e).__name__, e)

        task = asyncio.create_task(later())
        self.brief_timers[key] = task
        self.synth_tasks.add(task)
        task.add_done_callback(self.synth_tasks.discard)

    def fail_interrupted(self, boards: list[dict]) -> bool:
        """Mark failed ("interrupted") each ticket of `boards` whose hub-meta still says
        `running` with no run of this daemon behind it: one a killed daemon left.
        True when any was."""
        found = False
        for b in boards:
            brief = b["hub_meta"].get("board_brief")
            if (isinstance(brief, dict) and brief.get("synthesis_status") == "running"
                    and b["key"] not in self.briefing):  # fmt: skip
                path = Path(b["board_path"])
                try:
                    meta = read_meta(path)
                    entry = meta.get("board_brief")
                    if (isinstance(entry, dict)
                            and entry.get("synthesis_status") == "running"):  # fmt: skip
                        entry.update(synthesis_status="failed",
                                     synthesis_error="interrupted")  # fmt: skip
                        write_meta(path, meta)
                        found = True
                        log.warning("board brief of %s was interrupted", b["key"])
                except (OSError, ValueError) as e:
                    log.warning("hub-meta of %s: %s", b["key"], e)
            for t in b["tickets"]:
                mc = t.get("merge_conflict")
                if (isinstance(mc, dict) and mc.get("state") == "resolving"
                        and (b["key"], t["nn"]) not in self.resolving):  # fmt: skip
                    found |= self.fail_interrupted_resolve(b, t["nn"])
                if (t.get("synthesis_status") != "running"
                        or (b["key"], t["nn"]) in self.synthesizing):  # fmt: skip
                    continue
                path = Path(b["board_path"])
                try:
                    meta = read_meta(path)
                    entry = ticket_meta(meta, t["nn"])
                    if entry.get("synthesis_status") != "running":
                        continue  # a run finished since the export read it
                    entry.update(synthesis_status="failed",
                                 synthesis_error="interrupted")  # fmt: skip
                    write_meta(path, meta)
                except (OSError, ValueError) as e:
                    log.warning("hub-meta of %s: %s", b["key"], e)
                    continue
                log.warning("synthesis of %s #%s was interrupted", b["key"], t["nn"])
                found = True
        return found

    def fail_interrupted_resolve(self, board: dict, nn: str) -> bool:
        """A `resolving` merge with no resolver behind it (the daemon stopped or was
        killed mid-resolve, maybe while start ran): its merge is aborted under the
        board's flock, in the background, then marked failed (never left half-merged).
        Held as resolving meanwhile: no merge, no second cleanup. Always False: the
        state changes when the abort is done, and that re-exports."""
        slot = (board["key"], nn)
        try:
            if self.merge_conflict(board, nn).get("state") != "resolving":
                return False  # a run finished since the export read it
        except (OSError, ValueError) as e:
            log.warning("hub-meta of %s: %s", board["key"], e)
            return False
        self.resolving.add(slot)
        log.warning("resolving %s #%s was interrupted; aborting its merge", *slot)

        async def cleanup():
            why = "interrupted: the daemon stopped mid-resolve"
            try:
                rc, out = await self.locked_tk(
                    board, nn, None, "abort", nn, script=merge_conflict_sh()
                )
                reason = (
                    f"{why}; the merge was aborted" if rc == 0
                    else f"{why}; abort failed (exit {rc}): {out.strip()[-200:]}"
                )  # fmt: skip
            except Exception as e:  # noqa: BLE001 — recorded, shown on the ticket
                reason = f"{why}; abort failed: {e}"
            try:
                self.record_conflict(board, nn, state="failed", reason=reason,
                                     finished_at=utc_now())  # fmt: skip
            except (OSError, ValueError) as e:
                log.warning("hub-meta of %s: %s", board["key"], e)
            finally:
                self.resolving.discard(slot)
            if self.ws is not None:
                with contextlib.suppress(Exception):
                    await self.sync(self.ws, full=False)

        task = asyncio.create_task(cleanup())
        self.resolvers.add(task)
        task.add_done_callback(self.resolvers.discard)
        return False

    def spawn_synthesis(self, key: str, nn: str, comment: str = ""):
        async def run():
            board, ticket = self.board_ticket({"board_key": key, "nn": nn})
            with contextlib.suppress(agy_runner.SynthesisFailed):
                await self.synthesize(board, ticket, comment)
            if self.ws is not None:  # the hub sees the result now, not next tick
                await self.sync(self.ws, full=False)

        async def guarded():
            try:
                await run()
            except Exception as e:  # noqa: BLE001 — logged; the next move tries again
                log.warning("synthesis of %s #%s: %s: %s", key, nn, type(e).__name__, e)

        task = asyncio.create_task(guarded())
        self.synth_tasks.add(task)
        task.add_done_callback(self.synth_tasks.discard)

    async def send_event(
        self, kind: str, key: str, nn: str | None, backend, error=None
    ):
        """synthesis_started / _finished / _failed to the hub, which relays it to the UI;
        dropped while there is no connection."""
        msg = {"type": f"synthesis_{kind}", "board_key": key, "nn": nn,
               "backend": backend if backend in agy_runner.DEFAULT_COMMANDS else None,
               "at": utc_now()}  # fmt: skip
        if error is not None:
            msg["error"] = error[-TAIL:]
        if errors := self.schema.errors(msg):
            log.error("synthesis event fails the schema: %s", "; ".join(errors[:3]))
            return
        if (ws := self.ws) is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(msg))

    async def synthesis_context(
        self, board: dict, ticket: dict, comment: str
    ) -> agy_runner.SynthesisContext:
        policy = synthesis_policy(board)
        root = board["root"]
        try:
            code, out, _ = await asyncio.wait_for(
                run_cmd([tk_sh(), "show", board["board_path"], ticket["nn"], SHOW_LINES],
                        cwd=root),
                self.cfg["export_call_timeout_s"],
            )  # fmt: skip
            pane = out if code == 0 else None
        except (TimeoutError, OSError):
            pane = None
        report = None
        branch = ticket.get("branch") or ""
        if branch and ".." not in branch.split("/"):
            state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
            reports = Path(state) / "ticket-skill" / board["repo"] / "reports"
            report = read_file(reports / f"{branch}.md")
        file = ticket.get("file") or ""
        return agy_runner.SynthesisContext(
            cwd=root,
            ticket=f"{ticket['nn']}: {ticket['title']}",
            ticket_md=read_file(Path(file)) if os.path.isabs(file) else None,
            report_md=report,
            pane=pane,
            project_memory=read_file(Path(root) / "docs/agents/project-memory.md"),
            comment=comment,
            backends=synthesis_backends(policy),
            timeout=self.agy_timeout or self.policy_timeout(board, policy),
            commands=policy.get("commands")
            if isinstance(policy.get("commands"), dict)
            else {},
        )

    @staticmethod
    def policy_timeout(board: dict, policy: dict) -> float:
        """`synthesis.timeout`; the default when missing, or invalid (with a warning)."""
        value = policy.get("timeout")
        if value is None:
            return agy_runner.DEFAULT_TIMEOUT
        if (timeout := seconds(value)) is None:
            log.warning("board %s: ignoring synthesis.timeout=%r: not a number of "
                        "seconds > 0; using %ss", board["key"], value,
                        agy_runner.DEFAULT_TIMEOUT)  # fmt: skip
            return agy_runner.DEFAULT_TIMEOUT
        return timeout

    async def synthesize(
        self, board: dict, ticket: dict, comment: str
    ) -> agy_runner.SynthesisResult:
        """One synthesis run on a ticket, into its board's hub-meta.json. Raises
        SynthesisFailed (after recording it) when every backend failed."""
        key, nn = board["key"], ticket["nn"]
        if (key, nn) in self.synthesizing:
            raise ValueError(f"synthesis of {key} #{nn} is already running")
        path = Path(board["board_path"])
        first = synthesis_backends(synthesis_policy(board))[0]
        self.synthesizing.add((key, nn))
        try:
            before = read_meta(path)
            update_ticket_meta(path, nn, {"synthesis_status": "running"})
            await self.send_event("started", key, nn, first)
            try:
                ctx = await self.synthesis_context(board, ticket, comment)
                result = await agy_runner.synthesize(ctx)
            except asyncio.CancelledError:  # timed out as an RPC, or stopping
                with contextlib.suppress(Exception):
                    update_ticket_meta(path, nn, {"synthesis_status": "failed",
                                                  "synthesis_error": "interrupted"})  # fmt: skip
                # Best effort: the UI stops waiting.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.send_event("failed", key, nn, None, "interrupted"), 5
                    )
                raise
            except Exception as e:
                error = str(e)
                update_ticket_meta(path, nn, {"synthesis_status": "failed",
                                              "synthesis_error": error})  # fmt: skip
                backend = (
                    e.backend if isinstance(e, agy_runner.SynthesisFailed) else None
                )
                await self.send_event("failed", key, nn, backend, error)
                raise
            meta = read_meta(path)  # ticks made meanwhile are kept
            entry = ticket_meta(meta, nn)
            entry.update(
                summary=result.summary,
                reviewer_notes=result.reviewer_notes,
                full_report_md=result.full_report_md,
                manual_tasks=agy_runner.merge_tasks(
                    [t for t in entry.get("manual_tasks") or []
                     if isinstance(t, dict) and isinstance(t.get("text"), str)],
                    result.manual_tasks,
                ),
                new_memory_rule=result.new_memory_rule,
                synthesis_backend=result.backend,
                synthesis_status="ok",
                synthesis_error=None,
            )  # fmt: skip
            write_meta(path, before, "hub-meta.prev.json")
            write_meta(path, meta)
            await self.send_event("finished", key, nn, result.backend)
            return result
        finally:
            self.synthesizing.discard((key, nn))

    async def refresh_brief(self, board: dict) -> agy_runner.BriefResult:
        """One board-brief run into the board's hub-meta.json `board_brief`. A failed
        run keeps the previous brief's lines and marks it failed; raises
        SynthesisFailed (after recording it). board.md is only read."""
        key = board["key"]
        if key in self.briefing:
            raise ValueError(f"the board brief of {key} is already running")
        path = Path(board["board_path"])
        policy = synthesis_policy(board)
        first = synthesis_backends(policy)[0]
        self.briefing.add(key)

        def record(**fields):
            meta = read_meta(path)
            old = meta.get("board_brief")
            brief = {"landed": [], "in_flight": [], "needs_you": [], "next": [],
                     "synthesis_backend": None, "updated_at": None}  # fmt: skip
            brief.update(old if isinstance(old, dict) else {})
            brief.update(fields)
            meta["board_brief"] = brief
            write_meta(path, meta)

        try:
            record(synthesis_status="running", synthesis_error=None)
            await self.send_event("started", key, None, first)
            try:
                ctx = agy_runner.SynthesisContext(
                    cwd=board["root"],
                    ticket=f"{board.get('slug') or key}",
                    project_memory=read_file(
                        Path(board["root"]) / "docs/agents/project-memory.md"
                    ),
                    backends=synthesis_backends(policy),
                    timeout=self.agy_timeout or self.policy_timeout(board, policy),
                    commands=policy.get("commands")
                    if isinstance(policy.get("commands"), dict)
                    else {},
                    board_md=board.get("board_md"),
                    board_state="\n".join(
                        f"{t['nn']} [{t['col']}] {t['title']}" for t in board["tickets"]
                    ),
                )
                result = await agy_runner.synthesize_brief(ctx)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    record(synthesis_status="failed", synthesis_error="interrupted")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.send_event("failed", key, None, None, "interrupted"), 5
                    )
                raise
            except Exception as e:
                error = str(e)
                record(synthesis_status="failed", synthesis_error=error)
                backend = (
                    e.backend if isinstance(e, agy_runner.SynthesisFailed) else None
                )
                await self.send_event("failed", key, None, backend, error)
                raise
            record(
                landed=result.landed,
                in_flight=result.in_flight,
                needs_you=result.needs_you,
                next=result.next,
                synthesis_backend=result.backend,
                synthesis_status="ok",
                synthesis_error=None,
                updated_at=utc_now(),
            )
            await self.send_event("finished", key, None, result.backend)
            return result
        finally:
            self.briefing.discard(key)
            if key in self.brief_dirty:
                self.brief_dirty.discard(key)
                self.schedule_brief(key)

    async def rpc_propose_memory(self, ws, params: dict) -> dict:
        """Project memory only changes through a ticket: append the next-numbered
        ticket to the board asking for the rule; it then flows through the normal
        scheduler and merge gate. Nothing here writes project-memory.md."""
        b = self.sent.get(params.get("board_key"))
        text = params.get("text")
        if b is None:
            raise ValueError(f"no board {params.get('board_key')!r} on this box")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty memory rule")
        text = text.strip()
        rule = " ".join(text.split())
        short = rule if len(rule) <= 72 else rule[:71].rstrip() + "…"
        board = Path(b["board_path"])
        if not board.is_dir():
            raise ValueError(
                f"{board} is not a directory: single-file boards are unsupported"
            )
        # NN is one sequence across every repo of the feature: the boards sharing
        # this board_id on this box, and each repo path listed in <board>/repos.
        dirs = {board / "issues"}
        nums: list[int] = []
        for other in self.sent.values():
            if other.get("board_id") == b["board_id"]:
                dirs.add(Path(other["board_path"]) / "issues")
                nums += [
                    int(t["nn"])
                    for t in other.get("tickets", [])
                    if str(t.get("nn")).isdigit()
                ]
        repos = board / "repos"
        if repos.is_file():
            for line in repos.read_text().splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    dirs.add(
                        Path(parts[1].strip()) / ".scratch" / b["board_id"] / "issues"
                    )
        for d in dirs:
            nums += [
                int(m.group(1))
                for f in d.glob("*.md")
                if (m := re.match(r"(\d+)", f.name))
            ]
        n = max(nums, default=0) + 1
        slug = re.sub(r"[^a-z0-9]+", "-", f"project memory {short}".lower())[:60]
        slug = slug.strip("-")
        issues = board / "issues"
        issues.mkdir(exist_ok=True)
        quoted = text.replace("\n", "\n> ")
        while True:
            nn = f"{n:02d}"
            path = issues / f"{nn}-{slug}.md"
            try:
                fh = path.open("x")  # exclusive: never overwrite a ticket
            except FileExistsError:
                n += 1
                continue
            break
        with fh:
            fh.write(
                f"# {nn}: Project memory — {short}\n\n"
                "**What to build:** Add this rule to `docs/agents/project-memory.md` in "
                "that file's voice (match its existing style and numbering), and prune "
                "any line this makes stale or redundant. Change nothing else.\n\n"
                f"> {quoted}\n\n"
                f"**Repo:** {b['repo']} — {b['root']}\n"
                "**Blocked by:** None\n"
                "**Seams under test:** None — docs only\n"
                "**Suggested model:** sonnet\n"
                "**Suggested effort:** low\n\n"
                "- [ ] the rule is in `docs/agents/project-memory.md`, in the file's voice\n"
                "- [ ] stale or duplicate lines are pruned\n"
            )
        return {"ok": True, "exit_code": 0, "output_tail": f"wrote {path}"}

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
            "daemon_version": daemon_version(),
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
            self.ws = ws  # cleared when serve() ends
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
            if self.ws is ws:
                self.ws = None
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    # Test-only (not in the README): the racy window, bypassing its floor.
    parser.add_argument("--diff-racy-window-test-s", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
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
        daemon.racy_test_s = args.diff_racy_window_test_s
    except (OSError, ValueError) as e:
        log.error("bad config: %s", e)
        return 1
    log.info(
        "tk-daemon %s for %s, hub %s via %s",
        daemon_version(),
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
    for resolver in daemon.resolvers:  # killed agents; their merges are aborted
        resolver.cancel()
    if daemon.resolvers:
        await asyncio.wait(
            set(daemon.resolvers), timeout=daemon.cfg["shutdown_grace_s"]
        )
    if daemon.launches:  # a launch killed midway leaves a half-made worktree
        grace = daemon.cfg["shutdown_grace_s"]
        log.info("waiting up to %gs for %d launches", grace, len(daemon.launches))
        await asyncio.wait(set(daemon.launches), timeout=grace)
    log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())
