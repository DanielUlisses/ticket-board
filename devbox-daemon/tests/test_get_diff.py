"""get_diff through the daemon process, against a real git repo + worktree
(git is never faked). The ticket's change is the worktree's uncommitted diff
against its base sha."""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest
from conftest import row
from test_rpcs import KEY, call
from test_tk_daemon import FakeHub, daemon, run


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def wt(box):
    """A real repo with a committed base, and a worktree of it (the ticket's)."""
    main = box.repo("alpha")
    (main / ".git").rmdir()  # box.repo made a fake .git dir
    git(main, "init", "-q", "-b", "main")
    git(main, "config", "user.email", "t@t")
    git(main, "config", "user.name", "t")
    (main / "mod.txt").write_text("one\ntwo\nthree\n")
    (main / "gone.txt").write_text("bye\n")
    (main / "old.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    (main / "bin.dat").write_bytes(b"\x00\x01\x02")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "base")
    base = git(main, "rev-parse", "HEAD")
    tree = box.tmp / "wt"
    git(main, "worktree", "add", "-q", "-b", "tk-01", str(tree), base)
    wt = type("WT", (), {})()
    wt.path, wt.base = tree, base
    return wt


def edit_worktree(w: Path):
    (w / "mod.txt").write_text("one\nTWO\nthree\nfour\n")
    (w / "gone.txt").unlink()
    git(w, "mv", "old.txt", "new.txt")  # staged rename: the index is the dev's
    (w / "added.txt").write_text("a\nb\n")
    git(w, "add", "added.txt")
    (w / "fresh.txt").write_text("x\ny\nz\n")  # untracked
    (w / "bin.dat").write_bytes(b"\x00\x09\x09\x09")


def snapshot(w: Path):
    idx = Path(git(w, "rev-parse", "--git-path", "index"))
    idx = idx if idx.is_absolute() else w / idx
    files = {
        str(p.relative_to(w)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(w.rglob("*"))
        if p.is_file() and ".git" not in p.parts and p.name != ".git"
    }
    return idx.read_bytes(), idx.stat().st_mtime_ns, files


def get_diff(box, cert, wt, file=None, **config):
    box.board("alpha", "b", [row("01", "review", worktree=str(wt.path), base=wt.base)])
    params = {"board_key": KEY, "nn": "01"}
    if file:
        params["file"] = file

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, **config),
        ):
            await hub.next("snapshot")
            return await call(hub, "get_diff", params)

    return run(scenario())


def get_diffs(box, cert, wt, steps, **config):
    """Several get_diff calls against ONE daemon (its caches persist); each step is
    (action or None, file or None): the action runs on the worktree first."""
    box.board("alpha", "b", [row("01", "review", worktree=str(wt.path), base=wt.base)])

    async def scenario():
        out = []
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, **config),
        ):
            await hub.next("snapshot")
            for action, file in steps:
                if action:
                    action()
                params = {"board_key": KEY, "nn": "01"}
                if file:
                    params["file"] = file
                out.append(await call(hub, "get_diff", params))
        return out

    return run(scenario())


def by_path(result):
    return {f["path"]: f for f in result["data"]["files"]}


def test_lists_changed_files_with_status_and_stats_untracked_as_added(box, cert, wt):
    edit_worktree(wt.path)

    result = get_diff(box, cert, wt)

    assert result["ok"] is True
    files = by_path(result)
    assert {p: f["status"] for p, f in files.items()} == {
        "mod.txt": "M",
        "gone.txt": "D",
        "new.txt": "R",
        "added.txt": "A",
        "fresh.txt": "A",
        "bin.dat": "M",
    }
    assert (files["mod.txt"]["added"], files["mod.txt"]["removed"]) == (2, 1)
    assert (files["gone.txt"]["added"], files["gone.txt"]["removed"]) == (0, 1)
    assert (files["fresh.txt"]["added"], files["fresh.txt"]["removed"]) == (3, 0)
    assert files["new.txt"]["old_path"] == "old.txt"
    assert files["bin.dat"]["binary"] is True
    assert files["mod.txt"]["binary"] is False
    assert result["data"]["base"] == wt.base


def test_a_file_returns_its_unified_diff(box, cert, wt):
    edit_worktree(wt.path)

    mod = get_diff(box, cert, wt, "mod.txt")["data"]
    fresh = get_diff(box, cert, wt, "fresh.txt")["data"]
    renamed = get_diff(box, cert, wt, "new.txt")["data"]

    assert mod["stub"] is None
    assert "-two\n+TWO\n" in mod["diff"] and "+four" in mod["diff"]
    assert fresh["diff"].count("\n+") >= 3 and "+x\n+y\n+z\n" in fresh["diff"]
    assert "rename from old.txt" in renamed["diff"]
    assert fresh["status"] == "A" and fresh["added"] == 3


def test_binary_and_over_cap_files_return_a_stub_with_stats(box, cert, wt):
    edit_worktree(wt.path)
    (wt.path / "big.txt").write_text("".join(f"row {i}\n" for i in range(500)))

    binary = get_diff(box, cert, wt, "bin.dat")["data"]
    big = get_diff(box, cert, wt, "big.txt", diff_cap_chars=1000)["data"]
    small = get_diff(box, cert, wt, "mod.txt", diff_cap_chars=1000)["data"]

    assert binary["stub"] == "binary" and binary["diff"] == ""
    assert big["stub"] == "over_cap" and big["diff"] == ""
    assert big["added"] == 500
    assert small["stub"] is None and small["diff"]


def test_unknown_file_is_refused(box, cert, wt):
    result = get_diff(box, cert, wt, "nope.txt")

    assert result["ok"] is False and "nope.txt" in result["reason"]


def test_a_ticket_without_a_worktree_is_refused(box, cert):
    box.board("alpha", "b", [row("01", "review")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            return await call(hub, "get_diff", {"board_key": KEY, "nn": "01"})

    result = run(scenario())

    assert result["ok"] is False and "worktree" in result["reason"]


def test_the_index_and_worktree_are_untouched(box, cert, wt):
    edit_worktree(wt.path)
    before = snapshot(wt.path)

    get_diff(box, cert, wt)
    get_diff(box, cert, wt, "fresh.txt")
    get_diff(box, cert, wt, "mod.txt")

    assert snapshot(wt.path) == before
    assert "fresh.txt" not in git(wt.path, "ls-files")  # still untracked


def test_diff_hash_is_stable_and_changes_exactly_with_the_content(box, cert, wt):
    edit_worktree(wt.path)

    first = get_diff(box, cert, wt)["data"]["diff_hash"]
    again = get_diff(box, cert, wt)["data"]["diff_hash"]
    per_file = get_diff(box, cert, wt, "mod.txt")["data"]["diff_hash"]
    (wt.path / "fresh.txt").write_text("x\ny\nCHANGED\n")  # untracked content
    untracked = get_diff(box, cert, wt)["data"]["diff_hash"]
    (wt.path / "mod.txt").write_text("one\ntwo\nthree\n")  # back to base
    tracked = get_diff(box, cert, wt)["data"]["diff_hash"]

    assert first == again == per_file
    assert len({first, untracked, tracked}) == 3
    assert first.startswith("sha256:") and len(first) == 7 + 64
    int(first[7:], 16)


def test_paths_with_spaces_and_unicode_and_a_rename_with_a_space(box, cert, wt):
    (wt.path / "my file.txt").write_text("a\n")
    (wt.path / "café ü.txt").write_text("b\nc\n")  # untracked
    git(wt.path, "mv", "old.txt", "new name.txt")

    listing = get_diff(box, cert, wt)
    files = by_path(listing)
    spaced = get_diff(box, cert, wt, "my file.txt")["data"]
    uni = get_diff(box, cert, wt, "café ü.txt")["data"]
    renamed = get_diff(box, cert, wt, "new name.txt")["data"]

    assert files["my file.txt"]["status"] == "A"
    assert files["café ü.txt"]["added"] == 2
    assert files["new name.txt"]["status"] == "R"
    assert files["new name.txt"]["old_path"] == "old.txt"
    assert "+a\n" in spaced["diff"] and "+b\n+c\n" in uni["diff"]
    assert "rename to new name.txt" in renamed["diff"]


def test_a_tracked_binary_edited_twice_changes_the_hash(box, cert, wt):
    (wt.path / "bin.dat").write_bytes(b"\x00\x09\x09\x09")
    first = get_diff(box, cert, wt)["data"]["diff_hash"]
    (wt.path / "bin.dat").write_bytes(b"\x00\x08\x08\x08")
    second = get_diff(box, cert, wt)["data"]["diff_hash"]

    assert first != second


def test_a_base_that_is_not_a_sha_is_refused_before_git_runs(box, cert, wt):
    wt.base = "--output=/tmp/pwned"

    result = get_diff(box, cert, wt)

    assert result["ok"] is False and "base" in result["reason"]


def test_diff_output_ignores_user_diff_config(box, cert, wt):
    edit_worktree(wt.path)
    plain = get_diff(box, cert, wt)
    git(wt.path, "config", "diff.noprefix", "true")
    git(wt.path, "config", "diff.algorithm", "patience")
    git(wt.path, "config", "diff.suppressBlankEmpty", "true")

    configured = get_diff(box, cert, wt)
    mod = get_diff(box, cert, wt, "mod.txt")["data"]

    assert configured["data"]["diff_hash"] == plain["data"]["diff_hash"]
    assert "--- a/mod.txt" in mod["diff"]
    assert re.search(r"^index [0-9a-f]{40}\.\.[0-9a-f]{40} ", mod["diff"], re.MULTILINE)


def test_untracked_lines_starting_with_plus_plus_are_counted(box, cert, wt):
    (wt.path / "pp.txt").write_text("++ one\n+two\nthree")  # no final newline

    files = by_path(get_diff(box, cert, wt))
    data = get_diff(box, cert, wt, "pp.txt")["data"]

    assert files["pp.txt"]["added"] == 3 and data["added"] == 3
    assert "++ one" in data["diff"]


def test_an_untracked_binary_is_a_stub(box, cert, wt):
    (wt.path / "blob.bin").write_bytes(b"ab\x00cd")

    data = get_diff(box, cert, wt, "blob.bin")["data"]

    assert data["binary"] is True and data["stub"] == "binary" and data["added"] == 0


def test_an_untracked_nested_repo_is_marked_and_its_head_is_in_the_hash(box, cert, wt):
    nested = wt.path / "nested"
    nested.mkdir()
    git(nested, "init", "-q", "-b", "main")
    git(nested, "config", "user.email", "t@t")
    git(nested, "config", "user.name", "t")
    (nested / "f").write_text("1\n")
    git(nested, "add", "-A")
    git(nested, "commit", "-qm", "one")

    listing = get_diff(box, cert, wt)
    entry = by_path(listing)["nested/"]
    data = get_diff(box, cert, wt, "nested/")["data"]
    (nested / "f").write_text("2\n")
    git(nested, "commit", "-aqm", "two")
    after = get_diff(box, cert, wt)["data"]["diff_hash"]

    assert entry["status"] == "A" and entry["nested_repo"] is True
    assert data["stub"] == "nested_repo" and data["diff"] == ""
    assert after != listing["data"]["diff_hash"]


def test_an_unreadable_untracked_file_is_surfaced_not_hashed_as_empty(box, cert, wt):
    secret = wt.path / "secret.txt"
    secret.write_text("x\n")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("running as a user that reads anything")
        result = get_diff(box, cert, wt)
    finally:
        secret.chmod(0o644)

    assert result["ok"] is False and "secret.txt" in result["reason"]


def test_an_unmerged_path_is_listed_as_modified(box, cert, wt):
    blobs = []
    for stage, text in (("1", "one\n"), ("2", "ours\n"), ("3", "theirs\n")):
        blob = subprocess.run(
            ["git", "-C", str(wt.path), "hash-object", "-w", "--stdin"],
            input=text, check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip
        blobs.append((stage, blob))
    index_info = "".join(f"100644 {b} {s}\tmod.txt\n" for s, b in blobs)
    subprocess.run(
        ["git", "-C", str(wt.path), "update-index", "--index-info"],
        input="0 " + "0" * 40 + "\tmod.txt\n" + index_info,
        check=True, capture_output=True, text=True,
    )  # fmt: skip

    (wt.path / "mod.txt").write_text("<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>>\n")
    r = get_diff(box, cert, wt)
    files = by_path(r)

    assert files["mod.txt"]["status"] == "M"


@pytest.fixture
def git_log(box):
    """A `git` ahead of the real one on PATH that logs each invocation's args."""
    log = box.tmp / "git.log"
    shim = box.bin / "git"
    shim.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexec /usr/bin/git "$@"\n')
    shim.chmod(0o755)
    log.touch()
    return lambda: log.read_text().splitlines()


def test_a_huge_untracked_file_is_stubbed_without_git_diffing_it(
    box, cert, wt, git_log
):
    (wt.path / "huge.txt").write_text("".join(f"row {i}\n" for i in range(5000)))

    listing = get_diff(box, cert, wt, diff_cap_chars=1000)
    data = get_diff(box, cert, wt, "huge.txt", diff_cap_chars=1000)["data"]

    assert data["stub"] == "over_cap" and data["diff"] == ""
    assert by_path(listing)["huge.txt"]["added"] == 5000
    assert not [c for c in git_log() if "--no-index" in c]


def test_an_untracked_file_past_the_read_limit_is_not_read_but_still_hashed(
    box, cert, wt
):
    (wt.path / "huge.bin").write_bytes(b"a" * 5000)
    cfg = {"diff_max_untracked_file_bytes": 1000}

    first = get_diff(box, cert, wt, **cfg)
    entry = by_path(first)["huge.bin"]
    data = get_diff(box, cert, wt, "huge.bin", **cfg)["data"]
    (wt.path / "huge.bin").write_bytes(b"a" * 5001)
    second = get_diff(box, cert, wt, **cfg)

    assert entry["status"] == "A" and entry["added"] == 0
    assert data["stub"] == "over_cap"
    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]


def test_the_untracked_byte_budget_stops_reading_files(box, cert, wt):
    for n in "abc":
        (wt.path / f"{n}.txt").write_text("x\n" * 600)  # 1200 bytes each
    cfg = {"diff_max_untracked_bytes": 2000}

    files = by_path(get_diff(box, cert, wt, **cfg))

    assert [files[f"{n}.txt"]["added"] for n in "abc"] == [600, 0, 0]
    assert get_diff(box, cert, wt, "c.txt", **cfg)["data"]["stub"] == "over_cap"


def test_a_huge_tracked_change_is_stubbed_without_running_its_diff(
    box, cert, wt, git_log
):
    (wt.path / "mod.txt").write_text("".join(f"n{i}\n" for i in range(5000)))

    data = get_diff(box, cert, wt, "mod.txt", diff_cap_chars=1000)["data"]

    assert data["stub"] == "over_cap" and data["diff"] == ""
    assert data["added"] == 5000
    per_file = [c for c in git_log() if c.endswith("-- mod.txt") and "numstat" not in c]
    assert not per_file


def test_output_past_the_ceiling_is_a_clean_stub(box, cert, wt):
    # under diff_cap_chars (so no precheck) but past the byte ceiling
    (wt.path / "mod.txt").write_text("".join(f"{'y' * 30}{i}\n" for i in range(100)))
    cfg = {"diff_cap_chars": 100_000, "diff_max_output_bytes": 1500}

    r = get_diff(box, cert, wt, "mod.txt", **cfg)
    data = r["data"]
    ok = get_diff(box, cert, wt, "mod.txt", diff_cap_chars=100_000)["data"]

    assert data["stub"] == "over_cap" and data["diff"] == ""
    assert ok["stub"] is None and len(ok["diff"]) > 1500


def test_a_tracked_diff_past_the_hash_ceiling_is_refused(box, cert, wt):
    (wt.path / "mod.txt").write_text("z\n" * 3000)

    result = get_diff(box, cert, wt, diff_max_hash_bytes=1000)

    assert result["ok"] is False and "too large" in result["reason"]


def test_the_file_list_is_capped_with_a_truncated_flag(box, cert, wt):
    for i in range(5):
        (wt.path / f"u{i}.txt").write_text("x\n")
    (wt.path / "mod.txt").write_text("changed\n")

    capped = get_diff(box, cert, wt, diff_max_files=3)["data"]
    full = get_diff(box, cert, wt)["data"]
    one = get_diff(box, cert, wt, "u4.txt", diff_max_files=3)["data"]

    assert len(capped["files"]) == 3 and capped["truncated"] is True
    assert capped["total_files"] == 6
    assert full["truncated"] is False and len(full["files"]) == 6
    assert capped["diff_hash"] == full["diff_hash"]  # the hash still covers all
    assert "+x" in one["diff"]  # a file past the list cap is still reachable


NO_RACE = {
    "diff_racy_window_test_s": 0
}  # let the stat cache engage on just-written files


@pytest.mark.parametrize("cfg", [{}, NO_RACE], ids=["racy_window", "ctime_key"])
def test_a_same_size_edit_with_the_mtime_restored_changes_the_hash(box, cert, wt, cfg):
    f = wt.path / "fresh.txt"
    f.write_text("aaaa\n")
    orig = f.stat().st_mtime_ns

    def edit():
        f.write_text("bbbb\n")
        os.utime(f, ns=(orig, orig))

    first, second = get_diffs(box, cert, wt, [(None, None), (edit, None)], **cfg)

    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]


def test_the_hash_does_not_depend_on_the_cache_once_over_the_untracked_budget(
    box, cert, wt
):
    for n in "abc":
        (wt.path / f"{n}.txt").write_text("x\n" * 600)  # 1200 bytes each
    cfg = {"diff_max_untracked_bytes": 2000, **NO_RACE}

    first, second = get_diffs(box, cert, wt, [(None, None)] * 2, **cfg)

    assert first["data"]["diff_hash"] == second["data"]["diff_hash"]
    assert by_path(first) == by_path(second)


def test_a_touch_on_an_over_limit_untracked_file_changes_the_hash(box, cert, wt):
    f = wt.path / "huge.bin"
    f.write_bytes(b"a" * 5000)
    cfg = {"diff_max_untracked_file_bytes": 1000}

    first, second = get_diffs(
        box, cert, wt, [(None, None), (lambda: os.utime(f, (1, 1)), None)], **cfg
    )

    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]


def test_a_big_tracked_file_is_not_loaded_by_git_yet_its_change_moves_the_hash(
    box, cert, wt, git_log
):
    (wt.path / "mod.txt").write_text("a" * 3000 + "\n")
    cfg = {"diff_max_untracked_file_bytes": 1000}

    first = get_diff(box, cert, wt, **cfg)
    (wt.path / "mod.txt").write_text("b" * 3000 + "\n")
    second = get_diff(box, cert, wt, **cfg)

    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]
    assert by_path(first)["mod.txt"]["binary"]  # git itself treats it as binary
    calls = [c for c in git_log() if " diff " in f" {c} " or " ls-files " in f" {c} "]
    assert calls and all("core.bigFileThreshold=1000" in c for c in calls)


def test_the_users_global_excludes_file_still_hides_untracked_files(box, cert, wt):
    ignore = box.home / "my-ignore"
    ignore.write_text("*.secret\n")
    (box.home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {ignore}\n")
    (wt.path / "keys.secret").write_text("k\n")
    (wt.path / "keep.txt").write_text("k\n")

    files = by_path(get_diff(box, cert, wt))

    assert "keep.txt" in files and "keys.secret" not in files


@pytest.fixture
def git_pids(box):
    """A `git` ahead of the real one on PATH that logs its own pid per call."""
    log = box.tmp / "git.pids"
    shim = box.bin / "git"
    shim.write_text(f'#!/bin/sh\necho "$$" >> {log}\nexec /usr/bin/git "$@"\n')
    shim.chmod(0o755)
    log.touch()
    return lambda: [int(p) for p in log.read_text().split()]


def test_no_git_process_is_left_after_its_output_overflowed(box, cert, wt, git_pids):
    (wt.path / "mod.txt").write_text("".join(f"{'y' * 30}{i}\n" for i in range(100)))
    cfg = {"diff_cap_chars": 100_000, "diff_max_output_bytes": 1500}

    data = get_diff(box, cert, wt, "mod.txt", **cfg)["data"]

    assert data["stub"] == "over_cap"
    assert git_pids()
    assert not [p for p in git_pids() if Path(f"/proc/{p}").exists()]


def test_gitattributes_mark_an_untracked_file_binary(box, cert, wt):
    (wt.path / ".gitattributes").write_text("*.dat -diff\n*.bin binary\n")
    (wt.path / "t.dat").write_text("text\n")
    (wt.path / "t.bin").write_text("text\n")
    (wt.path / "t.txt").write_text("text\n")

    files = by_path(get_diff(box, cert, wt))
    stub = get_diff(box, cert, wt, "t.dat")["data"]

    assert files["t.dat"]["binary"] and files["t.bin"]["binary"]
    assert files["t.dat"]["added"] == 0
    assert not files["t.txt"]["binary"]
    assert stub["stub"] == "binary"


def test_an_untracked_symlink_is_listed_diffed_and_hashed_by_its_target(box, cert, wt):
    link = wt.path / "ln"
    link.symlink_to("mod.txt")

    def retarget():
        link.unlink()
        link.symlink_to("gone.txt")

    first, one, second = get_diffs(
        box, cert, wt, [(None, None), (None, "ln"), (retarget, None)]
    )

    assert by_path(first)["ln"]["status"] == "A"
    assert (
        "new file mode 120000" in one["data"]["diff"]
        and "+mod.txt" in one["data"]["diff"]
    )
    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]


def test_flipping_the_exec_bit_of_an_untracked_file_changes_the_hash(box, cert, wt):
    f = wt.path / "run.sh"
    f.write_text("echo\n")

    first, second = get_diffs(
        box, cert, wt, [(None, None), (lambda: f.chmod(0o755), None)]
    )

    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]


@pytest.fixture
def git_hook(box):
    """A `git` ahead of the real one on PATH: `hook` (a shell snippet, $1 the
    subcommand after any -c pairs, $@ all args) runs before the real git."""

    def install(hook: str):
        shim = box.bin / "git"
        shim.write_text(f'#!/bin/sh\n{hook}\nexec /usr/bin/git "$@"\n')
        shim.chmod(0o755)

    return install


def test_a_file_deleted_between_the_listing_and_the_read_is_skipped(
    box, cert, wt, git_hook
):
    (wt.path / "vanish.txt").write_text("x\n")
    (wt.path / "stay.txt").write_text("x\n")
    git_hook(
        f'case "$*" in *ls-files*) /usr/bin/git "$@"; rm -f {wt.path}/vanish.txt; exit 0;; esac'
    )

    files = by_path(get_diff(box, cert, wt))

    assert "stay.txt" in files and "vanish.txt" not in files


def test_a_file_gone_before_its_diff_is_a_refusal_saying_so(box, cert, wt, git_hook):
    (wt.path / "vanish.txt").write_text("x\n")
    git_hook(f'case "$*" in *--no-index*) rm -f {wt.path}/vanish.txt;; esac')

    result = get_diff(box, cert, wt, "vanish.txt")

    assert result["ok"] is False and "gone from the worktree" in result["reason"]


def test_a_fifo_is_skipped(box, cert, wt):
    os.mkfifo(wt.path / "pipe")
    (wt.path / "real.txt").write_text("x\n")

    result = get_diff(box, cert, wt)

    assert result["ok"] is True
    assert "pipe" not in by_path(result) and "real.txt" in by_path(result)


def test_crlf_and_unterminated_last_lines_are_counted(box, cert, wt):
    (wt.path / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (wt.path / "tail.txt").write_bytes(b"a\r\nb")

    files = by_path(get_diff(box, cert, wt))

    assert files["crlf.txt"]["added"] == 2 and files["tail.txt"]["added"] == 2


def test_an_untracked_name_that_is_not_utf8_is_listed_and_moves_the_hash(box, cert, wt):
    f = wt.path / os.fsdecode(b"bad\xff.txt")
    f.write_text("one\n")

    first, second = get_diffs(
        box, cert, wt, [(None, None), (lambda: f.write_text("two\n"), None)]
    )

    assert "bad\ufffd.txt" in by_path(first)  # shown with the bad byte replaced
    assert first["data"]["diff_hash"] != second["data"]["diff_hash"]


def test_a_non_utf8_name_is_diffable_by_its_shown_name(box, cert, wt):
    (wt.path / os.fsdecode(b"bad\xff.txt")).write_text("one\n")

    data = get_diff(box, cert, wt, "bad\ufffd.txt")["data"]

    assert "+one" in data["diff"]


def test_a_non_utf8_name_gets_its_gitattributes(box, cert, wt):
    (wt.path / ".gitattributes").write_bytes(b"bad* -diff\n")
    (wt.path / os.fsdecode(b"bad\xff.txt")).write_text("one\n")

    files = by_path(get_diff(box, cert, wt))

    assert files["bad\ufffd.txt"]["binary"]


def test_repo_config_cannot_hide_a_same_size_tracked_edit(box, cert, wt):
    git(wt.path, "config", "core.trustctime", "false")
    git(wt.path, "config", "core.checkStat", "minimal")
    f = wt.path / "mod.txt"
    os.utime(f, (1_000_000, 1_000_000))
    git(wt.path, "update-index", "--refresh")  # the index now has that old mtime

    def edit():
        time.sleep(1.1)  # git compares ctime in whole seconds
        f.write_text("one\nTWO\nthree\n")  # same size
        os.utime(f, (1_000_000, 1_000_000))

    first, second = get_diffs(box, cert, wt, [(None, None), (edit, None)])

    assert "mod.txt" not in by_path(first)
    assert "mod.txt" in by_path(second)


def test_the_users_global_attributes_file_marks_untracked_files_binary(box, cert, wt):
    attrs = box.home / "my-attrs"
    attrs.write_text("*.dat -diff\n")
    (box.home / ".gitconfig").write_text(f"[core]\n\tattributesFile = {attrs}\n")
    (wt.path / "t.dat").write_text("text\n")

    files = by_path(get_diff(box, cert, wt))

    assert files["t.dat"]["binary"]


def test_a_file_is_asked_for_literally_not_as_a_glob(box, cert, wt):
    (wt.path / "pages").mkdir()
    for n in ("[id].tsx", "i.tsx", "d.tsx"):
        (wt.path / "pages" / n).write_text("one\n")
    git(wt.path, "add", "pages")
    git(wt.path, "-c", "commit.gpgsign=false", "commit", "-qm", "pages")
    base = git(wt.path, "rev-parse", "HEAD")
    wt.base = base
    for n in ("[id].tsx", "i.tsx", "d.tsx"):
        (wt.path / "pages" / n).write_text("two\n")

    diff = get_diff(box, cert, wt, "pages/[id].tsx")["data"]["diff"]

    assert "pages/[id].tsx" in diff
    assert "pages/i.tsx" not in diff and "pages/d.tsx" not in diff


@pytest.mark.parametrize("name", [":!foo", "*.txt"])
def test_an_untracked_name_with_pathspec_magic_diffs_only_itself(box, cert, wt, name):
    (wt.path / name).write_text("magic\n")
    (wt.path / "other.txt").write_text("other\n")
    (wt.path / "foo").write_text("foo\n")

    data = get_diff(box, cert, wt, name)["data"]

    assert "+magic" in data["diff"]
    assert "+other" not in data["diff"] and "+foo" not in data["diff"]


def test_a_tracked_name_with_pathspec_magic_diffs_only_itself(box, cert, wt):
    (wt.path / "mod.txt").write_text("changed\n")
    (wt.path / "m*.txt").write_text("star\n")
    git(wt.path, "add", "m*.txt")

    data = get_diff(box, cert, wt, "m*.txt")["data"]

    assert "+star" in data["diff"] and "changed" not in data["diff"]


def test_a_shown_name_two_files_share_is_refused_as_ambiguous(box, cert, wt):
    for b in (b"a\xff.txt", b"a\xfe.txt"):
        (wt.path / os.fsdecode(b)).write_text("x\n")

    listed = get_diff(box, cert, wt)["data"]["files"]
    result = get_diff(box, cert, wt, "a�.txt")

    assert [f["path"] for f in listed].count("a�.txt") == 2
    assert result["ok"] is False and "ambiguous" in result["reason"]


def test_the_test_only_racy_key_in_config_json_does_not_lower_the_floor():
    import importlib.util

    from test_tk_daemon import DAEMON

    spec = importlib.util.spec_from_file_location("tk_daemon", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = {**mod.DEFAULTS, "url": "wss://h", "ip": "1.1.1.1", "token_ref": "t",
           "diff_racy_window_test_s": 0, "diff_racy_window_s": 0}  # fmt: skip

    assert mod.Daemon(cfg).racy_ns() == mod.DIFF_RACY_FLOOR_S * 1e9


def test_a_non_utf8_global_excludes_path_still_applies(box, cert, wt):
    ignore = box.home / os.fsdecode(b"ign\xffore")
    ignore.write_text("*.secret\n")
    (box.home / ".gitconfig").write_bytes(
        b"[core]\n\texcludesFile = " + os.fsencode(ignore) + b"\n"
    )
    (wt.path / "keys.secret").write_text("k\n")
    (wt.path / "keep.txt").write_text("k\n")

    files = by_path(get_diff(box, cert, wt))

    assert "keep.txt" in files and "keys.secret" not in files


def test_against_head_diffs_the_worktree_against_its_head_not_the_base(box, cert, wt):
    """A merge in progress, as ticket-merger leaves it, shows against the branch's
    previous head: what the merge brings, not the ticket's whole change."""
    (wt.path / "mod.txt").write_text("one\nTWO\nthree\n")
    git(wt.path, "commit", "-qam", "the ticket's own work")
    head = git(wt.path, "rev-parse", "HEAD")
    (wt.path / "gone.txt").write_text("merged in\n")
    git(wt.path, "add", "gone.txt")  # staged, as a resolved merge is
    box.board("alpha", "b", [row("01", "pr", worktree=str(wt.path), base=wt.base)])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            params = {"board_key": KEY, "nn": "01", "against": "head"}
            listing = await call(hub, "get_diff", params, "r-1")
            one = await call(hub, "get_diff", {**params, "file": "gone.txt"}, "r-2")
            return listing, one

    listing, one = run(scenario())

    assert listing["ok"], listing
    assert listing["data"]["base"] == head
    assert [f["path"] for f in listing["data"]["files"]] == ["gone.txt"]
    assert "+merged in" in one["data"]["diff"]
