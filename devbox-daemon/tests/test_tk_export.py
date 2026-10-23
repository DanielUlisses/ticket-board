"""tk-export.sh against fixture repos, with fake tk.sh / herdr on PATH: its
stdout must be one schema-valid full `snapshot` of every board on the box."""

import json
import subprocess
import time

from conftest import DAEMON_DIR, row, schema_errors

EXPORT = DAEMON_DIR / "tk-export.sh"


def export(box, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(EXPORT)],
        check=False,
        env={**box.env, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def snapshot(box, **env) -> dict:
    r = export(box, **env)
    assert r.returncode == 0, r.stderr
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    return msg


def failed_keys(r) -> list[str]:
    """Board keys of the `tk-export: failed <key> <path>` lines."""
    return [
        ln.split(" ", 3)[2]
        for ln in r.stderr.splitlines()
        if ln.startswith("tk-export: failed ")
    ]


def boards_by_key(msg) -> dict:
    return {b["key"]: b for b in msg["boards"]}


def test_finds_every_board_under_the_glob_and_merges_memory_hub_meta_and_usage(box):
    box.board("alpha", "tk-hub", [row("01", "working"), row("02")],
              {"hub-meta.json": {"archived": False, "summary_note": "kept",
                                 "tickets": {"01": {"summary": "Two lines.", "synthesis_status": "ok"}}},
               "board.md": "# tk-hub\n"})  # fmt: skip
    box.board("alpha", "other", [row("01", "review")])
    box.board("beta", "zig", [row("03", "done")])
    box.write("work/alpha/docs/agents/project-memory.md", "# Project memory\n1. rule\n")
    box.write(".local/state/tk-daemon/usage.json", {
        "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": "2099-01-01T00:00:00Z"},
                        "seven_day": {"used_percentage": 71.5, "resets_at": 4070908800}},
        "updated_at": "2099-01-01T00:00:00Z"})  # fmt: skip

    msg = snapshot(box)

    assert (msg["type"], msg["full"], msg["devbox"], msg["removed"]) == (
        "snapshot", True, "dev-test", [])  # fmt: skip
    boards = boards_by_key(msg)
    assert sorted(boards) == [
        "dev-test/alpha/other",
        "dev-test/alpha/tk-hub",
        "dev-test/beta/zig",
    ]
    hub = boards["dev-test/alpha/tk-hub"]
    assert hub["project_memory"] == "# Project memory\n1. rule\n"
    assert boards["dev-test/beta/zig"]["project_memory"] is None
    assert hub["board_md"] == "# tk-hub\n"
    assert hub["hub_meta"] == {
        "archived": False,
        "board_brief": None,
        "summary_note": "kept",
    }
    t01, t02 = hub["tickets"]
    assert (t01["summary"], t01["synthesis_status"], t01["col"]) == (
        "Two lines.",
        "ok",
        "working",
    )
    assert (t02["summary"], t02["manual_tasks"], t02["synthesis_status"]) == (
        None,
        [],
        "none",
    )
    usage = msg["health"]["usage"]
    assert usage["state"] == "ok"
    assert usage["five_hour"] == {
        "used_percentage": 42,
        "resets_at": "2099-01-01T00:00:00Z",
    }
    assert usage["seven_day"] == {
        "used_percentage": 71.5,
        "resets_at": "2099-01-01T00:00:00Z",
    }
    assert msg["health"]["running"] == 2  # one working + one review
    assert msg["health"]["herdr"]["ok"] is True
    assert "status: running" in msg["health"]["herdr"]["status"]
    # tk.sh runs from each repo's main checkout.
    calls = box.tk_calls()
    assert {(c["argv"][0], c["cwd"]) for c in calls if c["argv"][0] == "boards"} == {
        ("boards", str(box.work / "alpha")), ("boards", str(box.work / "beta"))}  # fmt: skip
    assert all(
        c["cwd"] in (str(box.work / "alpha"), str(box.work / "beta")) for c in calls
    )


def test_a_board_with_nothing_but_tickets_gets_defaults_and_herdr_down_is_reported(box):
    box.board("alpha", "bare", [row("01")])

    msg = snapshot(box, FAKE_HERDR_DOWN="1")

    (b,) = msg["boards"]
    assert (b["project_memory"], b["hub_meta"]) == (
        None,
        {"archived": False, "board_brief": None},
    )
    assert msg["health"]["usage"] == {
        "state": "unknown", "five_hour": None, "seven_day": None, "updated_at": None}  # fmt: skip
    assert msg["health"]["herdr"]["ok"] is False
    assert "connection refused" in msg["health"]["herdr"]["status"]


RESOLVED = {"forge": "github", "require_human_before_pr": False, "auto_merge": False,
            "night_shift": True, "autonomy": "assist", "max_parallel": 2,
            "usage_gate_pct": 70, "synthesis": {"backends": ["cursor"], "timeout": 60}}  # fmt: skip


def test_policy_is_tk_sh_policy_when_tk_sh_has_that_verb(box):
    box.board("alpha", "resolved", [row("01")], {"resolved-policy.json": RESOLVED,
                                                 "policy.json": {"auto_merge": True}})  # fmt: skip

    boards = boards_by_key(snapshot(box, FAKE_TK_POLICY="1"))

    assert boards["dev-test/alpha/resolved"]["policy"] == RESOLVED


def test_without_the_policy_verb_policy_is_the_box_repo_board_layers(box):
    box.write(
        ".config/tk-daemon/policy.json", {"max_parallel": 3, "autonomy": "assist"}
    )
    box.write(
        "work/alpha/.scratch/policy.json", {"autonomy": "auto", "usage_gate_pct": 80}
    )
    box.board("alpha", "layered", [row("01")], {"policy.json": {"auto_merge": True}})

    boards = boards_by_key(snapshot(box))

    assert boards["dev-test/alpha/layered"]["policy"] == {
        "forge": "none", "require_human_before_pr": True, "auto_merge": True,
        "night_shift": False, "autonomy": "auto", "max_parallel": 3, "usage_gate_pct": 80,
        "synthesis": {"backends": ["agy"], "timeout": 300}}  # fmt: skip
    assert [c["argv"][0] for c in box.tk_calls()].count("policy") == 0


def test_policy_sources_name_the_layer_each_value_comes_from(box):
    box.write(
        ".config/tk-daemon/policy.json", {"max_parallel": 3, "autonomy": "assist"}
    )
    box.write(
        "work/alpha/.scratch/policy.json", {"autonomy": "auto", "night_shift": True}
    )
    box.board(
        "alpha",
        "b",
        [row("01")],
        {"policy.json": {"auto_merge": True, "night_shift": False}},
    )

    b = boards_by_key(snapshot(box))["dev-test/alpha/b"]
    env = boards_by_key(snapshot(box, TICKET_FORGE="github"))["dev-test/alpha/b"]

    assert b["policy_sources"] == {
        "forge": "detected", "require_human_before_pr": "default", "auto_merge": "board",
        "night_shift": "board", "autonomy": "repo", "max_parallel": "box",
        "usage_gate_pct": "default", "synthesis": "default"}  # fmt: skip
    assert env["policy_sources"]["forge"] == "env"


def test_forge_of_a_repo_without_a_remote_is_sourced_no_remote_even_with_the_env(box):
    box.board("alpha", "b", [row("01")], {"no-remote": ""})

    plain = boards_by_key(snapshot(box))["dev-test/alpha/b"]
    env = boards_by_key(snapshot(box, TICKET_FORGE="github"))["dev-test/alpha/b"]

    assert plain["policy_sources"]["forge"] == "no-remote"
    assert env["policy_sources"]["forge"] == "no-remote"
    assert plain["policy_sources"]["autonomy"] == "default"


def test_a_tk_sh_policy_error_fails_the_board_and_reaches_stderr(box):
    box.board("alpha", "good", [row("01")], {"resolved-policy.json": RESOLVED})
    box.board("alpha", "bad", [row("01")], {"policy-fails": ""})

    r = export(box, FAKE_TK_POLICY="1")

    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert [b["key"] for b in msg["boards"]] == ["dev-test/alpha/good"]
    assert "dev-test/alpha/bad" in failed_keys(r)
    assert "ERROR: policy.json: parse error at line 3" in r.stderr


def test_only_main_checkouts_and_glob_matches_are_scanned(box):
    box.board("alpha", "one", [row("01")])
    box.board("alpha", "two", [row("01")])
    wt = box.work / "alpha--feat-01" / ".scratch" / "one"
    wt.mkdir(parents=True)
    (box.work / "alpha--feat-01" / ".git").write_text("gitdir: /x\n")

    everything = snapshot(box)
    only_one = snapshot(box, TK_SCAN_GLOB=str(box.work / "*" / ".scratch" / "one"))

    assert [b["key"] for b in everything["boards"]] == [
        "dev-test/alpha/one",
        "dev-test/alpha/two",
    ]
    assert [b["key"] for b in only_one["boards"]] == ["dev-test/alpha/one"]
    assert all(c["cwd"] == str(box.work / "alpha") for c in box.tk_calls())


def test_a_failing_board_is_named_on_stderr_and_the_others_still_export(box):
    box.board("alpha", "good", [row("01")])
    box.board("alpha", "bad", [row("01")], {"export-fails": ""})

    r = export(box)

    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert [b["key"] for b in msg["boards"]] == ["dev-test/alpha/good"]
    assert "dev-test/alpha/bad" in failed_keys(r)


def test_a_board_whose_hub_meta_cannot_be_merged_is_named_failed_not_dropped(box):
    box.board("alpha", "good", [row("01")])
    box.board("alpha", "list", [row("01")], {"hub-meta.json": {"tickets": []}})
    box.board("alpha", "str", [row("01")], {"hub-meta.json": {"tickets": {"01": "x"}}})

    r = export(box)

    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert [b["key"] for b in msg["boards"]] == ["dev-test/alpha/good"]
    assert "dev-test/alpha/list" in failed_keys(r)
    assert "dev-test/alpha/str" in failed_keys(r)


def test_usage_expired_window_is_zero_and_old_data_is_stale_while_agents_run(box):
    box.board("alpha", "b", [row("01", "working")])
    box.write(".local/state/tk-daemon/usage.json", {
        "five_hour": {"used_percentage": 95, "resets_at": "2020-01-01T00:00:00Z"},
        "seven_day": {"used_percentage": 50, "resets_at": "2099-01-01T00:00:00.123Z"},
        "updated_at": "2020-01-01T00:00:00Z"})  # fmt: skip

    usage = snapshot(box)["health"]["usage"]

    assert usage == {
        "state": "stale",
        "five_hour": {"used_percentage": 0, "resets_at": "2020-01-01T00:00:00Z"},
        "seven_day": {"used_percentage": 50, "resets_at": "2099-01-01T00:00:00Z"},
        "updated_at": "2020-01-01T00:00:00Z",
    }


def test_old_data_is_stale_while_agents_run_even_when_no_window_has_expired(box):
    box.board("alpha", "b", [row("01", "working")])
    box.write(".local/state/tk-daemon/usage.json", {
        "five_hour": {"used_percentage": 40, "resets_at": "2099-01-01T00:00:00Z"},
        "seven_day": {"used_percentage": 50, "resets_at": "2099-01-02T00:00:00Z"},
        "updated_at": "2020-01-01T00:00:00Z"})  # fmt: skip

    usage = snapshot(box)["health"]["usage"]

    assert usage["state"] == "stale"
    assert usage["five_hour"]["used_percentage"] == 40
    assert usage["seven_day"]["used_percentage"] == 50


def test_board_hash_is_stable_and_changes_with_content(box):
    d = box.board("alpha", "b", [row("01")])
    first = snapshot(box)["boards"][0]["hash"]
    again = snapshot(box)["boards"][0]["hash"]
    (d / "hub-meta.json").write_text(json.dumps({"archived": True}))
    changed = snapshot(box)["boards"][0]["hash"]

    assert first == again != changed


def test_a_hub_meta_that_is_not_a_json_object_fails_the_board(box):
    box.board("alpha", "good", [row("01")], {"hub-meta.json": {"archived": True}})
    box.board("alpha", "broken", [row("01")], {"hub-meta.json": '{"archived": tru'})
    box.board("alpha", "array", [row("01")], {"hub-meta.json": "[]"})

    r = export(box)

    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert [b["key"] for b in msg["boards"]] == ["dev-test/alpha/good"]
    assert "dev-test/alpha/broken" in failed_keys(r)
    assert "dev-test/alpha/array" in failed_keys(r)


def test_tk_sh_boards_output_that_is_not_a_json_array_fails_that_repos_boards(box):
    box.board("alpha", "one", [row("01")])
    box.board("alpha", "two", [row("01")])
    box.board("beta", "fine", [row("01")])
    (box.work / "alpha" / ".scratch" / "boards-out").write_text('{"oops": 1}')

    r = export(box)

    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert [b["key"] for b in msg["boards"]] == ["dev-test/beta/fine"]
    assert "dev-test/alpha/one" in failed_keys(r)
    assert "dev-test/alpha/two" in failed_keys(r)


def test_bad_usage_json_is_reported_unknown_and_never_breaks_the_export(box):
    box.board("alpha", "b", [row("01")])
    unknown = {"state": "unknown", "five_hour": None, "seven_day": None}
    for content in ["", "{} {}", "{not json", "[1, 2]", '"text"', '{"rate_limits": "full"}',
                    '{"rate_limits": [1]}', '{"rate_limits": {"five_hour": 7}}']:  # fmt: skip
        box.write(".local/state/tk-daemon/usage.json", content)

        usage = snapshot(box)["health"]["usage"]

        assert {k: usage[k] for k in unknown} == unknown, content


def test_usage_percentages_are_clamped_to_0_100(box):
    box.board("alpha", "b", [row("01")])
    box.write(".local/state/tk-daemon/usage.json", {
        "rate_limits": {"five_hour": {"used_percentage": 140, "resets_at": "2099-01-01T00:00:00Z"},
                        "seven_day": {"used_percentage": -3, "resets_at": "2099-01-01T00:00:00Z"}},
        "updated_at": "2099-01-01T00:00:00Z"})  # fmt: skip

    usage = snapshot(box)["health"]["usage"]

    assert usage["five_hour"]["used_percentage"] == 100
    assert usage["seven_day"]["used_percentage"] == 0


def test_a_hung_herdr_is_timed_out_and_reported_not_ok(box):
    box.board("alpha", "b", [row("01")])

    t0 = time.monotonic()
    msg = snapshot(box, FAKE_HERDR_SLOW="30", TK_EXPORT_CALL_TIMEOUT="1")

    assert time.monotonic() - t0 < 15
    assert msg["health"]["herdr"]["ok"] is False
    assert "timed out" in msg["health"]["herdr"]["status"]
    assert [b["key"] for b in msg["boards"]] == ["dev-test/alpha/b"]


def test_one_hung_board_is_timed_out_and_failed_while_the_others_export(box):
    box.board("alpha", "good", [row("01")])
    box.board("alpha", "hung", [row("01")], {"export-slow": "30"})
    box.board("beta", "also", [row("01")])

    t0 = time.monotonic()
    r = export(box, TK_EXPORT_CALL_TIMEOUT="1")

    assert time.monotonic() - t0 < 15
    assert r.returncode == 0, r.stderr
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert [b["key"] for b in msg["boards"]] == [
        "dev-test/alpha/good",
        "dev-test/beta/also",
    ]
    assert "dev-test/alpha/hung" in failed_keys(r)
    assert "timed out" in r.stderr


def test_past_the_deadline_the_remaining_boards_fail_and_the_snapshot_still_prints(box):
    for slug in ("a", "b", "c", "d", "e"):
        box.board("alpha", slug, [row("01")], {"export-slow": "0.35"})

    t0 = time.monotonic()
    r = export(box, TK_EXPORT_DEADLINE=f"{time.time() + 0.85:.3f}")
    took = time.monotonic() - t0

    assert r.returncode == 0, r.stderr
    assert took < 1.5
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    exported = [b["key"] for b in msg["boards"]]
    failed = failed_keys(r)
    assert 1 <= len(exported) <= 3 and failed
    assert sorted(exported + failed) == [f"dev-test/alpha/{s}" for s in "abcde"]


def test_when_tk_sh_help_fails_policy_boards_fail_loudly(box):
    box.board("alpha", "b", [row("01")])

    r = export(box, FAKE_TK_HELP_FAILS="1")

    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert msg["boards"] == []
    assert "dev-test/alpha/b" in failed_keys(r)
    assert "tk.sh help failed" in r.stderr


def test_policy_prose_in_help_is_not_taken_for_the_verb(box):
    box.board("alpha", "b", [row("01")], {"policy.json": {"max_parallel": 4}})

    boards = boards_by_key(snapshot(box, FAKE_TK_HELP_PROSE="1"))

    assert boards["dev-test/alpha/b"]["policy"]["max_parallel"] == 4
    assert "policy" not in [c["argv"][0] for c in box.tk_calls()]


def test_relative_board_paths_from_tk_sh_boards_are_taken_from_the_repo_root(box):
    box.board("alpha", "rel", [row("01")])
    (box.work / "alpha" / ".scratch" / "boards-out").write_text(
        json.dumps([{"slug": "rel", "path": ".scratch/rel"}])
    )

    assert [b["key"] for b in snapshot(box)["boards"]] == ["dev-test/alpha/rel"]


def test_failed_lines_carry_the_key_as_tk_sh_makes_it_and_the_board_path(box):
    d = box.board("alpha", "-hidden", [row("01")], {"export-fails": ""})

    r = export(box)

    line = next(
        ln for ln in r.stderr.splitlines() if ln.startswith("tk-export: failed ")
    )
    assert line == f"tk-export: failed dev-test/alpha/hidden {d.resolve()}"


def test_herdr_and_usage_are_read_before_the_boards_so_a_deadline_never_skips_them(box):
    for slug in ("a", "b", "c"):
        box.board("alpha", slug, [row("01")], {"export-slow": "0.35"})

    r = export(box, TK_EXPORT_DEADLINE=f"{time.time() + 0.5:.3f}")

    msg = json.loads(r.stdout)
    assert schema_errors(msg) == []
    assert failed_keys(r)
    assert msg["health"]["herdr"]["ok"] is True


def test_consecutive_runs_past_their_deadline_together_export_every_board(box):
    slugs = ["a", "b", "c", "d", "e"]
    for slug in slugs:
        box.board("alpha", slug, [row("01")], {"export-slow": "0.35"})

    seen, start = set(), "0"
    for _ in range(8):
        r = export(
            box, TK_EXPORT_DEADLINE=f"{time.time() + 0.85:.3f}", TK_EXPORT_START=start
        )
        msg = json.loads(r.stdout)
        assert schema_errors(msg) == []
        got = [b["key"] for b in msg["boards"]]
        assert got and sorted(got + failed_keys(r)) == [
            f"dev-test/alpha/{s}" for s in slugs
        ]
        seen.update(got)
        resume = [
            ln.split()[-1]
            for ln in r.stderr.splitlines()
            if ln.startswith("tk-export: resume ")
        ]
        if not resume:
            break
        start = resume[0]
    assert seen == {f"dev-test/alpha/{s}" for s in slugs}


def test_a_board_that_alone_outlasts_the_deadline_does_not_starve_the_others(box):
    box.board("alpha", "a", [row("01")], {"export-slow": "3"})
    box.board("alpha", "b", [row("01")])
    box.board("alpha", "c", [row("01")])

    seen, start = set(), "0"
    for _ in range(2):
        r = export(
            box, TK_EXPORT_DEADLINE=f"{time.time() + 0.6:.3f}", TK_EXPORT_START=start
        )
        seen.update(b["key"] for b in json.loads(r.stdout)["boards"])
        resume = [
            ln.split()[-1]
            for ln in r.stderr.splitlines()
            if ln.startswith("tk-export: resume ")
        ]
        start = resume[0] if resume else "0"

    assert {"dev-test/alpha/b", "dev-test/alpha/c"} <= seen


def test_a_zero_padded_start_index_is_decimal(box):
    for slug in "abcdefghij":
        box.board("alpha", slug, [row("01")])

    r = export(box, TK_EXPORT_START="09")

    assert r.returncode == 0, r.stderr
    calls = [c["argv"][1] for c in box.tk_calls() if c["argv"][0] == "export"]
    assert calls[0].endswith("/j")
