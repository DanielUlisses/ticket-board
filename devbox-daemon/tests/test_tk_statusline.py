"""tk-statusline (Claude Code statusLine command) and its installer, run as
subprocesses against a fake HOME."""

import json
import os
import shlex
import stat
import subprocess
import sys
import time

from conftest import DAEMON_DIR, schema_errors

STATUSLINE = DAEMON_DIR / "tk-statusline"
INSTALL = DAEMON_DIR / "tk-statusline-install"
USAGE = ".local/state/tk-daemon/usage.json"
PREV = ".config/tk-daemon/statusline-prev"


def run(box, script, stdin="", *args):
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin, check=False, capture_output=True, text=True, env=box.env, timeout=30,
    )  # fmt: skip


def statusline(box, payload):
    return run(
        box, STATUSLINE, payload if isinstance(payload, str) else json.dumps(payload)
    )


def usage_file(box):
    return json.loads((box.home / USAGE).read_text())


FULL = {
    "model": {"id": "x"},
    "rate_limits": {
        "five_hour": {"used_percentage": 42.5, "resets_at": 4102444800},
        "seven_day": {"used_percentage": 71, "resets_at": 4102444900},
    },
}


def test_writes_rate_limits_with_a_timestamp_and_prints_a_minimal_line(box):
    before = int(time.time())
    r = statusline(box, FULL)

    assert r.returncode == 0
    assert r.stdout.strip() != ""
    data = usage_file(box)
    assert data["rate_limits"] == FULL["rate_limits"]
    assert before <= data["updated_at"] <= int(time.time())
    assert [p.name for p in (box.home / USAGE).parent.iterdir()] == [
        "usage.json"
    ]  # no temp left


def test_a_missing_window_is_written_as_is_and_no_rate_limits_keeps_the_old_file(box):
    statusline(box, {"rate_limits": {"seven_day": FULL["rate_limits"]["seven_day"]}})
    assert usage_file(box)["rate_limits"] == {
        "seven_day": FULL["rate_limits"]["seven_day"]
    }

    r = statusline(box, {"model": {"id": "x"}})
    assert r.returncode == 0 and r.stdout.strip() != ""
    assert list(usage_file(box)["rate_limits"]) == ["seven_day"]


def test_bad_stdin_still_prints_a_line_and_exits_zero(box):
    for junk in [
        "",
        "not json",
        "[1,2]",
        '{"rate_limits": 7}',
        '{"rate_limits": {"five_hour": "x"}}',
    ]:
        r = statusline(box, junk)
        assert r.returncode == 0, junk
        assert r.stdout.strip() != "", junk
    assert not (box.home / USAGE).exists()


def test_write_failure_never_fails_the_statusline(box):
    state = box.home / ".local/state"
    state.mkdir(parents=True)
    (state / "tk-daemon").write_text("a file where the state dir should be")

    r = statusline(box, FULL)

    assert r.returncode == 0
    assert r.stdout.strip() != ""


def test_chains_the_previous_command_with_the_same_stdin(box):
    box.write(PREV, "cat | tr a-z A-Z")

    r = statusline(
        box,
        '{"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": 4102444800}}, "x": "abc"}',
    )

    assert r.returncode == 0
    assert '"X": "ABC"' in r.stdout
    assert usage_file(box)["rate_limits"]["five_hour"]["used_percentage"] == 1


def test_a_failing_or_silent_previous_command_falls_back_to_the_minimal_line(box):
    for cmd in ["exit 3", "true", "echo boom >&2; exit 1"]:
        box.write(PREV, cmd)
        r = statusline(box, FULL)
        assert r.returncode == 0, cmd
        assert r.stdout.strip() != "", cmd
        assert "boom" not in r.stdout


def test_a_prev_that_leads_back_to_tk_statusline_does_not_recurse(box):
    box.write(PREV, f"{sys.executable} {STATUSLINE}")

    r = statusline(box, FULL)

    assert r.returncode == 0
    assert r.stdout.strip() == "5h 42% · wk 71%"


def test_a_hanging_previous_command_times_out_and_its_group_is_killed(box):
    marker = box.home / "grandchild-alive"
    box.write(PREV, f"(sleep 8; touch {marker}) & sleep 60")
    t0 = time.time()

    r = statusline(box, FULL)

    assert r.returncode == 0
    assert r.stdout.strip() == "5h 42% · wk 71%"
    assert time.time() - t0 < 7
    time.sleep(4)
    assert not marker.exists()


def test_previous_stdout_counts_on_non_zero_exit_and_keeps_leading_padding(box):
    box.write(PREV, "printf '  padded\\n\\n'; exit 2")

    r = statusline(box, FULL)

    assert r.stdout == "  padded\n"


def test_non_utf8_stdin_reaches_the_child_unchanged(box):
    seen = box.home / "seen"
    box.write(PREV, f"tee {seen}")
    payload = b'{"x": "\xff\xfe"}'

    r = subprocess.run(
        [sys.executable, str(STATUSLINE)],
        input=payload, check=False, capture_output=True, env=box.env, timeout=30,
    )  # fmt: skip

    assert r.returncode == 0
    assert seen.read_bytes() == payload
    assert r.stdout.endswith(b"\n")


def test_the_daemon_reads_what_the_statusline_wrote(box):
    box.board("alpha", "b", [])
    statusline(box, FULL)
    r = subprocess.run(
        ["bash", str(DAEMON_DIR / "tk-export.sh")],
        check=False, capture_output=True, text=True, env=box.env, timeout=60,
    )  # fmt: skip
    msg = json.loads(r.stdout.strip().splitlines()[-1])
    usage = msg["health"]["usage"]
    assert usage["state"] == "ok"
    assert usage["five_hour"] == {
        "used_percentage": 42.5,
        "resets_at": "2100-01-01T00:00:00Z",
    }
    assert schema_errors(msg) == []


# ---- installer -------------------------------------------------------------------


def settings(box):
    return json.loads((box.home / ".claude/settings.json").read_text())


def install(box):
    return run(box, INSTALL, "", "--bin", "/opt/tk/tk-statusline")


def test_install_creates_settings_when_there_is_none(box):
    assert install(box).returncode == 0
    assert settings(box)["statusLine"] == {
        "type": "command",
        "command": "/opt/tk/tk-statusline",
    }
    assert not (box.home / PREV).exists()


def test_install_wraps_an_existing_statusline_keeping_other_keys(box):
    box.write(".claude/settings.json", {
        "theme": "dark",
        "statusLine": {"type": "command", "command": "my-line --fancy", "padding": 2}})  # fmt: skip

    assert install(box).returncode == 0

    s = settings(box)
    assert s["theme"] == "dark"
    assert s["statusLine"] == {
        "type": "command",
        "command": "/opt/tk/tk-statusline",
        "padding": 2,
    }
    assert (box.home / PREV).read_text().strip() == "my-line --fancy"


def test_install_is_idempotent_and_never_wraps_itself(box):
    box.write(
        ".claude/settings.json",
        {"statusLine": {"type": "command", "command": "my-line"}},
    )
    install(box)
    first = (box.home / ".claude/settings.json").read_text()

    assert install(box).returncode == 0

    assert (box.home / ".claude/settings.json").read_text() == first
    assert (box.home / PREV).read_text().strip() == "my-line"


def test_install_refuses_unreadable_settings_without_touching_them(box):
    p = box.write(".claude/settings.json", "{ not json")

    r = install(box)

    assert r.returncode != 0
    assert p.read_text() == "{ not json"
    assert not os.path.exists(box.home / PREV)


def test_install_quotes_a_bin_path_with_spaces_and_rerun_keeps_the_original(box):
    spaced = box.tmp / "my bin" / "tk-statusline"
    spaced.parent.mkdir()
    box.write(
        ".claude/settings.json",
        {"statusLine": {"type": "command", "command": "my-line"}},
    )

    assert run(box, INSTALL, "", "--bin", str(spaced)).returncode == 0
    first = (box.home / ".claude/settings.json").read_text()
    command = settings(box)["statusLine"]["command"]
    assert shlex.split(command) == [str(spaced)]
    assert run(box, INSTALL, "", "--bin", str(spaced)).returncode == 0

    assert (box.home / ".claude/settings.json").read_text() == first
    assert (box.home / PREV).read_text().strip() == "my-line"


def test_install_leaves_an_interpreter_wired_command_alone(box):
    cmd = "python3 /x/tk-statusline"
    box.write(".claude/settings.json", {"statusLine": {"type": "command", "command": cmd}})  # fmt: skip
    box.write(PREV, "my-line\n")

    assert run(box, INSTALL, "", "--bin", "/x/tk-statusline").returncode == 0

    assert settings(box)["statusLine"]["command"] == cmd
    assert (box.home / PREV).read_text() == "my-line\n"


def test_install_with_a_bad_bin_is_a_clean_error(box):
    r = run(box, INSTALL, "", "--bin")

    assert r.returncode != 0
    assert "Traceback" not in r.stderr


def test_install_keeps_the_mode_and_a_symlinked_settings_file(box):
    real = box.write("dotfiles/settings.json", {"theme": "dark"})
    real.chmod(0o600)
    link = box.home / ".claude/settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    assert install(box).returncode == 0

    assert link.is_symlink()
    assert json.loads(real.read_text())["statusLine"]["command"].endswith(
        "tk-statusline"
    )
    assert stat.S_IMODE(real.stat().st_mode) == 0o600


def test_install_removes_a_leftover_prev_when_there_is_no_statusline(box):
    box.write(PREV, "stale-line\n")
    box.write(".claude/settings.json", {"theme": "dark"})

    assert install(box).returncode == 0

    assert not (box.home / PREV).exists()
