"""agy_runner.synthesize over fake `agy` / `cursor-agent` / `claude` on PATH (tests/fakes/
synth_cli.py): backends tried in order, each under a hard timeout, output parsed from
the CLI's JSON envelope or a JSON block in its text."""

import asyncio
import json
import time

import pytest

import agy_runner
from agy_runner import SynthesisContext, SynthesisFailed, synthesize

# Flags that let a CLI act without asking: none may ever reach a backend.
PERMISSION_SKIP = {
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
    "--yolo", "-f", "--force", "--permission-mode", "auto", "accept-edits",
    "bypassPermissions",
}  # fmt: skip


@pytest.fixture
def ctx(box, monkeypatch):
    for k, v in box.env.items():
        monkeypatch.setenv(k, v)
    root = box.repo("alpha")

    def make(**kw) -> SynthesisContext:
        return SynthesisContext(
            **{
                "cwd": str(root),
                "ticket": "01: Export",
                "ticket_md": "# 01: Export\n\nDo the export.",
                "report_md": None,
                "pane": "pane tail",
                "project_memory": "1. tk.sh runs in the main checkout.",
                "comment": "",
                **kw,
            }
        )

    return make


def run(coro):
    return asyncio.run(coro)


def test_good_json_from_agy_with_the_prompt_as_argv_in_the_repo_root(box, ctx):
    c = ctx(comment="Mention the cap.")

    result = run(synthesize(c))

    assert result.backend == "agy"
    assert result.summary == "Written by agy.\nSecond line."
    assert result.reviewer_notes == "Check the edge case."
    assert result.manual_tasks == ["Open the sheet on a phone", "Tick a box"]
    assert result.new_memory_rule is None
    [call] = box.synth_calls()
    assert call["bin"] == "agy" and call["cwd"] == c.cwd
    assert call["stdin"] == ""  # the prompt is never piped (agy -p may hang on a pipe)
    assert call["argv"][0] == "-p" and "--output-format" in call["argv"]
    prompt = call["argv"][1]
    for part in ("01: Export", "Do the export.", "pane tail", "main checkout",
                 "Mention the cap."):  # fmt: skip
        assert part in prompt


def test_text_wrapped_json_and_bare_json_are_parsed(box, ctx, monkeypatch):
    monkeypatch.setenv("FAKE_SYNTH_MODE", "text")
    assert run(synthesize(ctx())).summary == "Written by agy.\nSecond line."
    monkeypatch.setenv("FAKE_SYNTH_MODE", "raw")
    assert run(synthesize(ctx())).reviewer_notes == "Check the edge case."


@pytest.mark.parametrize(
    "mode", ["garbage", "invalid", "is_error", "status_error", "agy_error"]
)
def test_bad_output_falls_through_to_the_next_backend_in_order(
    box, ctx, monkeypatch, mode
):
    monkeypatch.setenv("FAKE_SYNTH_MODE_AGY", mode)
    monkeypatch.setenv("FAKE_SYNTH_MODE_CURSOR_AGENT", "garbage")

    result = run(synthesize(ctx(backends=["agy", "cursor", "claude"])))

    assert result.backend == "claude"
    assert result.summary.startswith("Written by claude.")
    assert [c["bin"] for c in box.synth_calls()] == ["agy", "cursor-agent", "claude"]


# agy 1.4.6's `-p ... --output-format json` stdout, captured (conversation_id redacted).
REAL_AGY_JSON = (
    '{"conversation_id":"00000000-0000-0000-0000-000000000000","status":"SUCCESS",'
    '"response":"{\\"summary\\":\\"a\\\\nb\\",\\"reviewer_notes\\":\\"x\\",'
    '\\"full_report_md\\":\\"r\\",\\"manual_tasks\\":[\\"t1\\"]}\\n",'
    '"duration_seconds":3.814899722,"num_turns":1,"usage":{"input_tokens":6832,'
    '"output_tokens":158,"thinking_tokens":130,"cache_read_tokens":0,'
    '"total_tokens":6990}}\n'
)


def test_the_real_agy_envelope_is_parsed():
    assert agy_runner.parse_output(REAL_AGY_JSON) == {
        "summary": "a\nb",
        "reviewer_notes": "x",
        "full_report_md": "r",
        "manual_tasks": ["t1"],
    }


def test_an_agy_envelope_whose_status_is_not_success_never_counts():
    env = json.loads(REAL_AGY_JSON) | {"status": "ERROR"}

    assert agy_runner.parse_output(json.dumps(env)) is None


def test_a_good_first_backend_is_the_only_one_called(box, ctx):
    result = run(synthesize(ctx(backends=["cursor", "agy"])))

    assert result.backend == "cursor"
    assert [c["bin"] for c in box.synth_calls()] == ["cursor-agent"]


def test_a_hung_backend_is_killed_at_the_timeout_and_the_next_one_runs(
    box, ctx, monkeypatch
):
    monkeypatch.setenv("FAKE_SYNTH_MODE_AGY", "hang")
    start = time.monotonic()

    result = run(synthesize(ctx(backends=["agy", "claude"], timeout=0.5)))

    assert time.monotonic() - start < 5
    assert result.backend == "claude"


def gone(pid: int) -> bool:
    """pid has exited (no such process, or a zombie)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except FileNotFoundError:
        return True


def test_a_helper_left_holding_the_output_open_is_killed_and_the_run_succeeds(
    box, ctx, monkeypatch, tmp_path
):
    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("FAKE_SYNTH_MODE_AGY", "linger")
    monkeypatch.setenv("FAKE_SYNTH_PIDFILE", str(pidfile))
    start = time.monotonic()

    result = run(synthesize(ctx(backends=["agy"], timeout=10)))

    assert time.monotonic() - start < 5  # not held until the timeout
    assert result.backend == "agy"
    helper = int(pidfile.read_text())
    deadline = time.monotonic() + 2
    while not gone(helper) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert gone(helper)


def test_every_backend_failing_raises_with_each_ones_reason(box, ctx, monkeypatch):
    monkeypatch.setenv("FAKE_SYNTH_MODE_AGY", "hang")
    monkeypatch.setenv("FAKE_SYNTH_MODE_CLAUDE", "garbage")

    with pytest.raises(SynthesisFailed) as e:
        run(synthesize(ctx(backends=["agy", "claude"], timeout=0.5)))

    assert e.value.backend == "claude"
    assert "agy: timed out after 0.5s" in str(e.value)
    assert "claude: invalid output" in str(e.value)


def test_a_missing_binary_falls_through(box, ctx):
    cmds = {"agy": {"bin": "agy-not-installed"}}

    result = run(synthesize(ctx(backends=["agy", "claude"], commands=cmds)))

    assert result.backend == "claude"


def test_no_permission_skip_flag_reaches_any_backend_by_default(box, ctx, monkeypatch):
    monkeypatch.setenv("FAKE_SYNTH_MODE", "garbage")

    with pytest.raises(SynthesisFailed):
        run(synthesize(ctx(backends=["agy", "cursor", "claude"])))

    calls = box.synth_calls()
    assert [c["bin"] for c in calls] == ["agy", "cursor-agent", "claude"]
    for c in calls:
        assert PERMISSION_SKIP.isdisjoint(c["argv"]), c
    claude = calls[2]["argv"]
    assert claude[claude.index("--model") + 1] == "haiku"


def test_configured_binary_and_args_are_used_with_the_prompt_placeholder(box, ctx):
    cmds = {"agy": {"bin": "claude", "args": ["--print", "{prompt}", "--x"]}}

    run(synthesize(ctx(commands=cmds)))

    [call] = box.synth_calls()
    assert call["bin"] == "claude"
    assert call["argv"][0] == "--print" and call["argv"][2] == "--x"
    assert "01: Export" in call["argv"][1]


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "--dangerously-skip-permissions"],
        ["-p", "--yolo"],
        ["-p", "-f"],
        ["-p", "--force"],
        ["-p", "--mode", "auto"],
        ["-p", "--mode=accept-edits"],
        ["-p", "--permission-mode", "bypassPermissions"],
    ],
)
def test_configured_permission_skip_flags_are_refused_without_running(box, ctx, args):
    cmds = {"agy": {"args": args}}

    with pytest.raises(SynthesisFailed) as e:
        run(synthesize(ctx(commands=cmds)))

    assert box.synth_calls() == []
    assert "permission" in str(e.value)


@pytest.mark.parametrize(
    ("broken", "cmds", "reason"),
    [
        ("agy", {"agy": {"args": ["-p", "--yolo"]}}, "refused flag --yolo"),
        ("agny", {}, "unknown backend"),
        ("agy", {"agy": {"args": "-p {prompt}"}}, "args"),
        ("agy", {"agy": {"bin": ""}}, "binary"),
        ("agy", {"agy": "agy -p"}, "commands entry"),
    ],
)
def test_a_broken_backend_is_skipped_with_a_warning_and_the_next_one_runs(
    box, ctx, caplog, broken, cmds, reason
):
    result = run(synthesize(ctx(backends=[broken, "claude"], commands=cmds)))

    assert result.backend == "claude"
    assert [c["bin"] for c in box.synth_calls()] == ["claude"]
    assert reason in result.attempts[0] and result.attempts[0].startswith(broken)
    assert any(
        r.levelname == "WARNING"
        and broken in r.getMessage()
        and reason in r.getMessage()
        for r in caplog.records
    )


def test_a_broken_backend_after_a_good_one_is_never_looked_at(box, ctx):
    cmds = {"claude": {"args": ["-p", "--yolo"]}}

    result = run(synthesize(ctx(backends=["agy", "claude"], commands=cmds)))

    assert (result.backend, result.attempts) == ("agy", [])


def test_every_backend_broken_or_failing_raises_with_each_ones_reason(
    box, ctx, monkeypatch
):
    monkeypatch.setenv("FAKE_SYNTH_MODE_CURSOR_AGENT", "hang")
    cmds = {"agy": {"args": ["-p", "--yolo"]}}

    with pytest.raises(SynthesisFailed) as e:
        run(synthesize(ctx(backends=["agy", "cursor"], commands=cmds, timeout=0.5)))

    assert str(e.value).startswith("agy: refused flag --yolo")
    assert "; cursor: timed out after 0.5s" in str(e.value)
    assert e.value.backend == "cursor"
    calls = box.synth_calls()
    assert [c["bin"] for c in calls] == ["cursor-agent"]
    assert "--yolo" not in calls[0]["argv"]


def test_the_coordinator_report_comes_first_verbatim_then_the_reasoning(box, ctx):
    report = "# Coordinator report\n\nAll 5 criteria met.\n"

    result = run(synthesize(ctx(report_md=report)))

    assert result.full_report_md.startswith(report)
    assert result.full_report_md.endswith("agy thinks it is fine.")
    assert "All 5 criteria met." in box.synth_calls()[0]["argv"][1]


def test_summary_is_kept_to_two_lines_and_notes_to_one(box, ctx, monkeypatch):
    payload = {"summary": "one\n\ntwo\nthree", "reviewer_notes": "a\nb",
               "full_report_md": "r", "manual_tasks": [{"text": "t1"}, " t2 ", ""],
               "new_memory_rule": "Run shellcheck."}  # fmt: skip
    monkeypatch.setenv("FAKE_SYNTH_PAYLOAD", json.dumps(payload))

    result = run(synthesize(ctx()))

    assert (result.summary, result.reviewer_notes) == ("one\ntwo", "a")
    assert result.manual_tasks == ["t1", "t2"]
    assert result.new_memory_rule == "Run shellcheck."


def test_a_huge_context_is_cut_to_fit_one_argument(box, ctx):
    run(synthesize(ctx(pane="x" * 500_000, ticket_md="y" * 500_000)))

    prompt = box.synth_calls()[0]["argv"][1]
    assert len(prompt.encode()) < agy_runner.MAX_PROMPT_BYTES
