"""The shared merge path holds only collisions and resolves by position."""

import pytest

from brindle.plan_rebase import apply_resolution, rebase


@pytest.mark.parametrize("choice", ["theirs", "mine", "merged", "suggestion"])
def test_overlap_is_held_while_independent_edits_apply(choice):
    base = "Title\n\nBudget: 100 credits. Keep notes.\n\nRemove this.\n\nEnd"
    local = "New title\n\nBudget: 200 credits. Keep my notes.\n\nEnd\n\nAdded"
    remote = "Title\n\nBudget: 300 credits. Keep notes. Extra detail.\n\nRemove this.\n\nEnd"
    result = rebase(base, local, remote)
    expected = "New title\n\nBudget: 300 credits. Keep my notes. Extra detail.\n\nEnd\n\nAdded"
    assert result.text == expected
    collision, = result.collisions
    assert collision.base == "Budget: 100 credits. Keep notes.\n\n"
    assert collision.mine == "Budget: 200 credits. Keep my notes. Extra detail.\n\n"
    assert collision.theirs == "Budget: 300 credits. Keep my notes. Extra detail.\n\n"
    assert result.text[collision.start:collision.end] == collision.theirs
    combined = "Budget agreed together.\n\n"
    resolved = apply_resolution(result, collision.id, choice, combined if choice == "merged" else None)
    replacement = collision.mine if choice == "mine" else combined if choice == "merged" else collision.theirs
    assert resolved.text == "New title\n\n" + replacement + "End\n\nAdded"
    assert not resolved.collisions
    if choice == "suggestion":
        suggestion, = resolved.suggestions
        assert resolved.text[suggestion.start:suggestion.end] == suggestion.before == collision.theirs
        assert suggestion.after == collision.mine
    else:
        assert not resolved.suggestions


def test_repeated_paragraphs_resolve_at_their_positions():
    base = "Budget: 100.\n\nBudget: 100.\n\nBudget: 100."
    local = "Budget: 200.\n\nBudget: 100.\n\nBudget: 200."
    remote = "Budget: 300.\n\nBudget: 100.\n\nBudget: 300."
    result = rebase(base, local, remote)
    first, last = result.collisions
    result = apply_resolution(result, last.id, "suggestion")
    result = apply_resolution(result, first.id, "merged", "A longer first paragraph.\n\n")
    assert result.text == "A longer first paragraph.\n\nBudget: 100.\n\nBudget: 300."
    suggestion, = result.suggestions
    assert result.text[suggestion.start:suggestion.end] == "Budget: 300."
    assert suggestion.after == "Budget: 200."
    # Resolving an earlier box also moves a still-pending later box.
    result = rebase(base, local, remote)
    result = apply_resolution(result, first.id, "merged", "Short.\n\n")
    result = apply_resolution(result, last.id, "mine")
    assert result.text == "Short.\n\nBudget: 100.\n\nBudget: 200."
    # A change to just the final identical paragraph must keep the equal prefix.
    result = rebase(base, "Budget: 100.\n\nBudget: 100.\n\nBudget: 200.", base)
    assert result.text == "Budget: 100.\n\nBudget: 100.\n\nBudget: 200."
    assert not result.collisions
    # Concurrent insertions preserve both writers, with remote first (B4.5).
    inserted = rebase("ab", "aMineb", "aTheirsb")
    assert inserted.text == "aTheirsMineb"
    assert not inserted.collisions
    result = rebase(base, "Budget: 100.\n\nBudget: 100.\n\nBudget: 200.",
                    "Budget: 300.\n\nBudget: 100.\n\nBudget: 100.")
    assert result.text == "Budget: 300.\n\nBudget: 100.\n\nBudget: 200."
    assert not result.collisions
    base = "\n\n".join(["Budget: 100."] * 5)
    local = "\n\n".join(f"Budget: {n}." for n in [200, 200, 100, 200, 200])
    remote = "\n\n".join(f"Budget: {n}." for n in [100, 100, 300, 100, 100])
    result = rebase(base, local, remote)
    assert result.text == "\n\n".join(f"Budget: {n}." for n in [200, 200, 300, 200, 200])
    assert not result.collisions


def test_held_insertion_at_deleted_paragraph_boundary_survives_resolution():
    result = rebase("A\n\nB", "A\n\nMineB", "B")
    assert result.text == "B"
    collision, = result.collisions
    assert "Mine" in collision.mine
    result = apply_resolution(result, collision.id, "mine")
    assert result.text == "A\n\nMineB"
