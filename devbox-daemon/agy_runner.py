"""agy_runner — card summaries, full reports and manual checklists written by a cheap
CLI, never by the coding agents (board.md, Synthesis).

One interface: `await synthesize(context) -> SynthesisResult`. The backends are tried
in the context's order (policy `synthesis.backends`, default ["agy"]); the next one runs
only when one fails: timed out, not installed, exited non-zero, printed no valid
synthesis, or is misconfigured (unknown backend, bad bin or args, a permission-skip
flag), found only when its turn comes, logged and never run. When every backend
fails, SynthesisFailed lists each one's reason. Each runs with cwd = the repo root,
stdin = /dev/null, the prompt as one argv item (agy -p may hang on a pipe), in its
own session, killed at the hard timeout (policy `synthesis.timeout`, 300 s). No
permission-skip flag is ever passed: a backend configured with one is refused.

Default commands (override per backend with policy `synthesis.commands.<backend>:
{"bin": ..., "args": [...]}`; `{prompt}` in args is replaced by the prompt, else the
prompt is appended):
  agy     agy -p {prompt} --output-format json
  cursor  cursor-agent -p --output-format json --mode ask {prompt}
  claude  claude -p --output-format json --model haiku {prompt}

Output: one JSON object {summary, reviewer_notes, full_report_md, manual_tasks,
new_memory_rule?}, found as the CLI's JSON envelope's text (`response`, `result`,
`text`, …), the bare object, or a JSON block in text. agy's envelope (verified on agy
1.4.6) is one JSON line {conversation_id, status: "SUCCESS", response: "<model text>",
duration_seconds, num_turns, usage}; an envelope whose `status` is anything else, or
flagged is_error (claude, cursor-agent), is a failed backend.

A board brief (`synthesize_brief`) is the same machinery with another prompt and another
object: {landed, in_flight, needs_you, next}, each a list of short lines.

Also the checklist rules, as pure functions: `merge_tasks` (a new run's items over the
current checklist) and `set_task_done` (a tick).
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 300
DEFAULT_COMMANDS = {
    "agy": {"bin": "agy", "args": ["-p", "{prompt}", "--output-format", "json"]},
    "cursor": {
        "bin": "cursor-agent",
        "args": ["-p", "--output-format", "json", "--mode", "ask", "{prompt}"],
    },
    "claude": {
        "bin": "claude",
        "args": ["-p", "--output-format", "json", "--model", "haiku", "{prompt}"],
    },
}
# Flags that let a CLI act without asking, and the mode values that do the same.
SKIP_FLAGS = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--yolo",
    "-y",
    "-f",
    "--force",
}
MODE_FLAGS = {"--mode", "--permission-mode", "--approval-mode"}
SKIP_MODES = {"auto", "accept-edits", "acceptedits", "bypasspermissions", "yolo",
              "auto_edit", "auto-edit", "dontask"}  # fmt: skip
# One argv item is at most 128 KiB on Linux (MAX_ARG_STRLEN): the prompt stays under.
MAX_PROMPT_BYTES = 120_000
SECTION_BYTES = 20_000
MAX_OUTPUT_CHARS = 2_000_000
MAX_OUTPUT_BYTES = 4 * MAX_OUTPUT_CHARS  # read from a backend's stdout / stderr
DRAIN_TIMEOUT = 5  # seconds to read what is left once the backend's session is killed
EXIT_POLL = 0.05  # seconds between looks at whether the backend has exited
TEXT_KEYS = ("response", "result", "text", "output", "content", "message")
TAIL = 300  # characters of stderr quoted in a failure

log = logging.getLogger("tk-daemon.agy_runner")

INSTRUCTIONS = """\
You write the tk-hub card for one ticket of a ticket-skill board, from the material \
below. Only read: do not edit files or run commands.
Answer with ONE JSON object and nothing else:
{"summary": "<two short lines: what the ticket does and where it stands>",
 "reviewer_notes": "<one line: what a reviewer should look at first>",
 "full_report_md": "<markdown: your reasoning about the work, its risks and what is left>",
 "manual_tasks": ["<a check only a human can do, e.g. on a real device>", ...],
 "new_memory_rule": "<one durable rule for the project memory, or null>"}
Do not copy the coordinator report into full_report_md: it is put first, verbatim, \
for you. Keep manual_tasks short and concrete; [] when there is nothing to check by \
hand. Follow the developer comment when there is one."""


@dataclass
class SynthesisContext:
    cwd: str  # the repo's main checkout
    ticket: str  # "NN: title"
    ticket_md: str | None = None  # the ticket file
    report_md: str | None = None  # reports/<branch>.md, the coordinator report
    pane: str | None = None  # tk.sh show <board> <NN> 180
    project_memory: str | None = None
    comment: str = ""  # the developer's comment (refine_with_comment)
    backends: list[str] = field(default_factory=lambda: ["agy"])
    timeout: float = DEFAULT_TIMEOUT  # per backend
    commands: dict = field(default_factory=dict)  # backend -> {bin?, args?}
    board_md: str | None = None  # a board brief: board.md, the board's purpose
    board_state: str | None = None  # a board brief: one line per ticket


@dataclass
class SynthesisResult:
    backend: str
    summary: str
    reviewer_notes: str
    full_report_md: str
    manual_tasks: list[str]
    new_memory_rule: str | None
    attempts: list[str] = field(default_factory=list)  # backends that failed first


@dataclass
class BriefResult:
    backend: str
    landed: list[str]
    in_flight: list[str]
    needs_you: list[str]
    next: list[str]
    attempts: list[str] = field(default_factory=list)


class SynthesisFailed(Exception):
    """Every backend failed; `backend` is the last one tried (None: none was)."""

    def __init__(self, attempts: list[str], backend: str | None):
        super().__init__("; ".join(attempts) or "no synthesis backend configured")
        self.attempts = attempts
        self.backend = backend


class BackendFailed(Exception):
    pass


class ConfigError(Exception):
    """A backend's configuration is wrong: it is skipped, never run."""


async def synthesize(ctx: SynthesisContext) -> SynthesisResult:
    backend, obj, attempts = await _first_good(ctx, build_prompt(ctx), _is_synthesis)
    return _result(backend, obj, ctx.report_md, attempts)


async def synthesize_brief(ctx: SynthesisContext) -> BriefResult:
    """The board brief of `ctx` (board_md, board_state); same backends and failures."""
    backend, obj, attempts = await _first_good(ctx, build_brief_prompt(ctx), _is_brief)
    return BriefResult(
        backend,
        *(_strings(obj[k]) for k in ("landed", "in_flight", "needs_you", "next")),
        attempts=attempts,
    )


async def _first_good(
    ctx: SynthesisContext, prompt: str, valid
) -> tuple[str, dict, list]:
    """(backend, its output object, failed attempts) of the first backend that works."""
    attempts: list[str] = []
    for backend in ctx.backends:
        try:
            argv = command(backend, ctx, prompt)
        except ConfigError as e:
            log.warning("synthesis backend %s skipped, misconfigured: %s", backend, e)
            attempts.append(f"{backend}: {e}")
            continue
        try:
            out = await _run_backend(argv, ctx, valid)
        except BackendFailed as e:
            attempts.append(f"{backend}: {e}")
            continue
        return backend, out, attempts
    raise SynthesisFailed(attempts, ctx.backends[-1] if ctx.backends else None)


BRIEF_INSTRUCTIONS = """\
You write the BOARD BRIEF of a ticket-skill board, for the developer coming back after \
a break, from the material below. Only read: do not edit files or run commands.
Answer with ONE JSON object and nothing else:
{"landed": ["<short line: what is done and merged>", ...],
 "in_flight": ["<short line: what is being worked on or reviewed>", ...],
 "needs_you": ["<short line: what waits for the developer, with the ticket number>", ...],
 "next": ["<short line: what to launch or decide next>", ...]}
Each list holds at most 6 lines; [] when there is nothing. Never rewrite board.md."""


def build_brief_prompt(ctx: SynthesisContext) -> str:
    parts = [BRIEF_INSTRUCTIONS]
    for title, text in [
        (f"Board {ctx.ticket}", ctx.board_md),
        ("Tickets", ctx.board_state),
        ("Project memory", ctx.project_memory),
    ]:
        body = _cut(text, SECTION_BYTES, False) if text and text.strip() else "(none)"
        parts.append(f"## {title}\n\n{body}")
    return _cut("\n\n".join(parts), MAX_PROMPT_BYTES - 1, False)


def build_prompt(ctx: SynthesisContext) -> str:
    sections = [
        (f"Ticket {ctx.ticket}", ctx.ticket_md, False),
        ("Coordinator report", ctx.report_md, False),
        ("Board view (tk.sh show)", ctx.pane, True),
        ("Project memory", ctx.project_memory, False),
        ("Developer comment", ctx.comment, False),
    ]
    parts = [INSTRUCTIONS]
    for title, text, keep_tail in sections:
        body = (
            _cut(text, SECTION_BYTES, keep_tail) if text and text.strip() else "(none)"
        )
        parts.append(f"## {title}\n\n{body}")
    return _cut("\n\n".join(parts), MAX_PROMPT_BYTES - 1, False)


def _cut(text: str, limit: int, keep_tail: bool) -> str:
    raw = text.encode()
    if len(raw) <= limit:
        return text
    mark = "[...cut...]"
    room = limit - len(mark) - 1
    if keep_tail:
        return mark + "\n" + raw[-room:].decode(errors="ignore")
    return raw[:room].decode(errors="ignore") + "\n" + mark


def refused_flag(args: list[str]) -> str | None:
    """The first permission-skip flag in `args`, or None."""
    for i, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        if flag in SKIP_FLAGS:
            return arg
        if flag in MODE_FLAGS:
            value = value if eq else (args[i + 1] if i + 1 < len(args) else "")
            if value.lower() in SKIP_MODES:
                return f"{flag} {value}"
    return None


def command(backend: str, ctx: SynthesisContext, prompt: str) -> list[str]:
    """The backend's argv; ConfigError when its configuration is wrong."""
    if backend not in DEFAULT_COMMANDS:
        raise ConfigError(f"unknown backend {backend!r}")
    override = ctx.commands.get(backend) or {}
    if not isinstance(override, dict):
        raise ConfigError("commands entry must be a {bin, args} object")
    cfg = {**DEFAULT_COMMANDS[backend], **override}
    args = cfg["args"]
    if not isinstance(cfg["bin"], str) or not cfg["bin"]:
        raise ConfigError("no binary configured")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError("args must be a list of strings")
    if flag := refused_flag([a for a in args if a != "{prompt}"]):
        raise ConfigError(f"refused flag {flag}: it would skip permission prompts")
    if "{prompt}" in args:
        return [cfg["bin"], *(prompt if a == "{prompt}" else a for a in args)]
    return [cfg["bin"], *args, prompt]


async def _run_backend(argv: list[str], ctx: SynthesisContext, valid) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=ctx.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:  # not installed, not executable
        raise BackendFailed(f"{argv[0]} not runnable ({e.strerror})") from None
    out, err = bytearray(), bytearray()
    readers = [
        asyncio.ensure_future(_drain(proc.stdout, out)),
        asyncio.ensure_future(_drain(proc.stderr, err)),
    ]
    # Until the backend itself exits, not until its pipes close: a helper it left
    # behind may hold them open. Then its whole session goes, on every path.
    try:
        await asyncio.wait_for(_exited(proc), ctx.timeout)
    except TimeoutError:
        raise BackendFailed(f"timed out after {ctx.timeout:g}s") from None
    finally:
        kill_session(proc.pid)
        await asyncio.wait(readers, timeout=DRAIN_TIMEOUT)
        for reader in readers:
            reader.cancel()
    stderr = err.decode(errors="replace")
    if proc.returncode != 0:
        raise BackendFailed(f"exited {proc.returncode}: {stderr.strip()[-TAIL:]}")
    if "AGY_ERROR" in stderr:
        raise BackendFailed(stderr.strip()[-TAIL:])
    found = parse_output(out.decode(errors="replace")[:MAX_OUTPUT_CHARS], valid)
    if found is None:
        raise BackendFailed("invalid output")
    return found


async def _exited(proc: asyncio.subprocess.Process):
    """Until `proc` exits. Not proc.wait(): before Python 3.13 that also waits for
    every pipe to close, which a helper left holding them delays until the timeout;
    the returncode is set at the exit on every version."""
    while proc.returncode is None:
        await asyncio.sleep(EXIT_POLL)


async def _drain(stream: asyncio.StreamReader, into: bytearray):
    """Read `stream` to its end into `into`, keeping at most MAX_OUTPUT_BYTES."""
    while chunk := await stream.read(65536):
        if len(into) < MAX_OUTPUT_BYTES:
            into += chunk[: MAX_OUTPUT_BYTES - len(into)]


def kill_session(sid: int):
    """SIGKILL every process in session `sid`: its process group, and the groups its
    processes made (Linux /proc). A process that left the session is out of reach."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(sid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        for name in os.listdir("/proc"):
            if name.isdigit():
                with contextlib.suppress(OSError):  # gone meanwhile, or not ours
                    if os.getsid(int(name)) == sid:
                        os.kill(int(name), signal.SIGKILL)


# ---- output -------------------------------------------------------------------------
def parse_output(text: str, valid=None) -> dict | None:
    """The synthesis object in a CLI's output: the bare object, inside its JSON
    envelope's text, or a JSON block anywhere in text; None when there is none. An
    envelope flagged is_error, or with a `status` other than SUCCESS, never counts."""
    for obj in _objects(text):
        if (found := _synthesis_in(obj, 0, valid or _is_synthesis)) is not None:
            return found
    return None


def _objects(text: str):
    """The JSON values in `text`: all of it, then each {...} that decodes, in order."""
    text = text.strip()
    with contextlib.suppress(ValueError, RecursionError):
        yield json.loads(text)
    decoder = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except (ValueError, RecursionError):
            i = text.find("{", i + 1)
            continue
        yield obj
        i = text.find("{", end)


def _synthesis_in(obj, depth: int, valid) -> dict | None:
    if not isinstance(obj, dict) or depth > 3 or obj.get("is_error") is True:
        return None
    if valid(obj):
        return obj
    if depth == 0 and obj.get("status", "SUCCESS") != "SUCCESS":
        return None
    for key in TEXT_KEYS:
        value = obj.get(key)
        inner = _objects(value) if isinstance(value, str) else [value]
        for candidate in inner:
            if (found := _synthesis_in(candidate, depth + 1, valid)) is not None:
                return found
    return None


def _is_synthesis(obj: dict) -> bool:
    summary = obj.get("summary")
    return (
        isinstance(summary, str)
        and bool(summary.strip())
        and isinstance(obj.get("reviewer_notes"), str)
        and isinstance(obj.get("full_report_md"), str)
        and isinstance(obj.get("manual_tasks"), list)
        and isinstance(obj.get("new_memory_rule"), str | None)
    )


def _is_brief(obj: dict) -> bool:
    return all(
        isinstance(obj.get(k), list) and all(isinstance(x, str) for x in obj[k])
        for k in ("landed", "in_flight", "needs_you", "next")
    )


def _strings(items: list[str]) -> list[str]:
    return [t.strip() for t in items if t.strip()]


def _lines(text: str, n: int) -> str:
    return "\n".join([ln.strip() for ln in text.splitlines() if ln.strip()][:n])


def _result(backend: str, obj: dict, report: str | None, attempts: list[str]):
    tasks = []
    for t in obj["manual_tasks"]:
        text = t.get("text") if isinstance(t, dict) else t
        if isinstance(text, str) and text.strip() and text.strip() not in tasks:
            tasks.append(text.strip())
    reasoning = obj["full_report_md"].strip()
    full = reasoning
    if report and report.strip() and not reasoning.startswith(report.strip()):
        full = report + ("" if report.endswith("\n") else "\n") + "\n" + reasoning
    rule = obj.get("new_memory_rule")
    return SynthesisResult(
        backend=backend,
        summary=_lines(obj["summary"], 2),
        reviewer_notes=_lines(obj["reviewer_notes"], 1),
        full_report_md=full,
        manual_tasks=tasks,
        new_memory_rule=rule.strip()
        if isinstance(rule, str) and rule.strip()
        else None,
        attempts=attempts,
    )


# ---- checklist ----------------------------------------------------------------------
def normalise(text: str) -> str:
    """What two checklist items are compared by: case, spacing and a final full stop
    don't count."""
    return " ".join(text.split()).casefold().rstrip(".!;: ")


def merge_tasks(current: list[dict], new: list[str]) -> list[dict]:
    """A new run's items over the current checklist: developer items always stay (with
    their text); an item the run repeats keeps its tick and takes the run's text; a
    synthesis item the run dropped stays only if it is checked. A synthesis item with a
    developer item's text gives way to it, its tick kept on the developer item. Current
    items keep their order; the run's new items follow, deduplicated."""
    wanted: dict[str, str] = {}
    for text in new:
        if text.strip():
            wanted.setdefault(normalise(text), text.strip())
    developer = {
        normalise(t["text"]) for t in current if t.get("source") == "developer"
    }
    ticked = {
        normalise(t["text"])
        for t in current
        if t.get("source") != "developer" and t.get("done")
    }
    merged, seen = [], set()
    for task in current:
        key = normalise(task["text"])
        if task.get("source") == "developer":
            merged.append({**task, "done": bool(task.get("done")) or key in ticked})
        elif key in seen or key in developer:
            continue
        elif key in wanted:
            merged.append({**task, "text": wanted[key]})
        elif task.get("done"):
            merged.append(dict(task))
        else:
            continue
        seen.add(key)
    merged += [
        {"text": text, "done": False, "source": "synthesis"}
        for key, text in wanted.items()
        if key not in seen
    ]
    return merged


def set_task_done(tasks: list[dict], text: str, done: bool) -> list[dict]:
    """The checklist with the item matching `text` (normalised) ticked or unticked;
    KeyError when there is none."""
    key = normalise(text)
    if not any(normalise(t["text"]) == key for t in tasks):
        raise KeyError(text)
    return [
        {**t, "done": done} if normalise(t["text"]) == key else dict(t) for t in tasks
    ]
