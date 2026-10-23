"""The manual checklist rules (board.md, Synthesis): developer items are never removed by
a synthesis run; an item a new run repeats (by normalised text) keeps its tick; a
synthesis item a new run drops is removed only if unchecked."""

import pytest

from agy_runner import merge_tasks, set_task_done


def syn(text, done=False):
    return {"text": text, "done": done, "source": "synthesis"}


def dev(text, done=False):
    return {"text": text, "done": done, "source": "developer"}


def test_a_first_run_makes_unchecked_synthesis_items():
    assert merge_tasks([], ["Open on a phone", "Tick a box"]) == [
        syn("Open on a phone"),
        syn("Tick a box"),
    ]


def test_a_repeated_item_keeps_its_tick_matched_by_normalised_text():
    old = [syn("Open the sheet on a phone.", done=True), syn("Tick a box")]

    merged = merge_tasks(old, ["  open the SHEET   on a phone ", "Tick a box"])

    assert merged == [
        syn("  open the SHEET   on a phone ".strip(), done=True),
        syn("Tick a box"),
    ]


def test_dropped_synthesis_items_go_only_when_unchecked():
    old = [syn("Checked and dropped", done=True), syn("Unchecked and dropped")]

    assert merge_tasks(old, ["New one"]) == [
        syn("Checked and dropped", done=True),
        syn("New one"),
    ]


def test_developer_items_are_never_removed_and_keep_their_text_and_source():
    old = [dev("My own check"), dev("Also mine", done=True)]

    merged = merge_tasks(old, ["also MINE", "Fresh"])

    assert merged == [dev("My own check"), dev("Also mine", done=True), syn("Fresh")]


def test_duplicates_in_a_run_are_kept_once():
    assert merge_tasks([], ["A", "a.", "B"]) == [syn("A"), syn("B")]


def test_set_task_done_ticks_and_unticks_by_normalised_text():
    tasks = [syn("Open on a phone"), dev("Mine")]

    ticked = set_task_done(tasks, "open on a PHONE.", True)
    assert ticked == [syn("Open on a phone", done=True), dev("Mine")]
    assert set_task_done(ticked, "Open on a phone", False) == tasks
    assert tasks == [syn("Open on a phone"), dev("Mine")]  # not modified in place


def test_set_task_done_on_an_unknown_item_raises():
    with pytest.raises(KeyError):
        set_task_done([syn("A")], "B", True)


def test_a_developer_item_wins_over_an_earlier_synthesis_item_with_the_same_text():
    old = [syn("Open on a phone", done=True), dev("open on a PHONE."), dev("Mine")]

    merged = merge_tasks(old, ["Open on a phone", "Fresh"])

    assert merged == [dev("open on a PHONE.", done=True), dev("Mine"), syn("Fresh")]


def test_developer_items_with_the_same_text_are_all_kept():
    old = [dev("Mine"), dev("mine.", done=True)]

    assert merge_tasks(old, []) == old
