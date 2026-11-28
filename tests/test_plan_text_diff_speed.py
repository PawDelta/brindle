"""Plan text diffs preserve offsets without comparing whole large texts by character."""

from difflib import SequenceMatcher

import pytest

from brindle import plan_docs, plan_drafts, plan_rebase
from brindle.crdt import SharedDoc


@pytest.fixture
def matching_budget(monkeypatch):
    remaining = 1_000_000
    find = SequenceMatcher.find_longest_match

    def bounded(self, alo, ahi, blo, bhi):
        nonlocal remaining
        remaining -= (ahi - alo) * (bhi - blo)
        assert remaining >= 0, "sequence matching exceeded the work budget"
        return find(self, alo, ahi, blo, bhi)

    monkeypatch.setattr(SequenceMatcher, "find_longest_match", bounded)


def test_rebase_skips_unchanged_paragraphs(matching_budget):
    paragraphs = [f"Paragraph {i:03}: leave the rest of this paragraph intact." for i in range(160)]
    base = "\n\n".join(paragraphs)
    local = base.replace("Paragraph 000:", "First paragraph:").replace("Paragraph 153:", "Section 153:")
    local = local.replace("Paragraph 080:", "New paragraph.\n\nParagraph 080:")
    remote = base.replace("Paragraph 006:", "Section 006:").replace("Paragraph 159:", "Last paragraph:")

    result = plan_rebase.rebase(base, local, remote)

    assert result.text == local.replace("Paragraph 006:", "Section 006:").replace("Paragraph 159:", "Last paragraph:")
    assert not result.collisions
    replayed = remote
    for h in reversed(result.applied):
        replayed = replayed[:h.start] + h.text + replayed[h.end:]
    assert replayed == result.text


def test_large_rebase_keeps_repeated_paragraphs_at_their_positions(matching_budget):
    paragraphs = ["Budget: 100."] * 160
    local, remote = paragraphs.copy(), paragraphs.copy()
    local[0], local[-2] = "Budget: 200.", "Budget: 400."
    remote[1], remote[-1] = "Budget: 300.", "Budget: 500."

    result = plan_rebase.rebase("\n\n".join(paragraphs), "\n\n".join(local), "\n\n".join(remote))

    assert not result.collisions
    assert result.text.split("\n\n") == [local[0], remote[1], *paragraphs[2:-2], local[-2], remote[-1]]


def test_rewritten_paragraph_stays_out_of_large_myers_searches(monkeypatch):
    split = plan_rebase._split

    def bounded(before, after):
        assert len(before) + len(after) <= 2_000, "Myers search exceeded the size budget"
        return split(before, after)

    monkeypatch.setattr(plan_rebase, "_split", bounded)
    base = "alpha " * 1280 + " Tail."
    local = "omega " * 1280 + " Tail."

    edits = plan_rebase._hunks(base, local)

    assert plan_rebase._render(base, 0, len(base), edits) == local
    assert edits[-1].end <= len(base) - len(" Tail.")


def _compare(left, right):
    current = {"rev": 2, "lede": "", "sections": [{"id": "s", "markdown": right}], "decisions": []}
    previous = {**current, "rev": 1, "sections": [{"id": "s", "markdown": left}]}
    change, = plan_docs.compare_revisions(current, previous)["changes"]
    return change["spans"]


def test_large_history_section_keeps_unchanged_lines_between_edits(matching_budget):
    middle = "".join(f"{i:03}: " + "stable content " * 8 + "\n" for i in range(472))
    spans = _compare("Left\n" + middle + "Up", "left\n" + middle + "up")

    assert [(span["before_start"], span["before"], span["after"]) for span in spans] == [
        (0, "L", "l"), (5 + len(middle), "U", "u"),
    ]


def test_large_draft_mapping_keeps_offsets_between_edits(matching_budget):
    middle = "".join(f"{i:03}: " + "stable content " * 6 + "\n" for i in range(270))
    at, changed = plan_drafts.mapping("Left\n" + middle + "Up", "leftward\n" + middle + "up")

    assert changed == [(0, 1), (4, 4), (5 + len(middle), 6 + len(middle))]
    assert at(25) == 29


@pytest.mark.parametrize("prefix, suffix, remove_word", [
    pytest.param("! ", " ?", False, id="short-edge-insertions"),
    pytest.param("! ", " ?", True, id="interior-deletion",
                 marks=pytest.mark.variant("An interior deletion splits the unchanged run into two anchors")),
    pytest.param("[" * 30, "]" * 30, False, id="long-edge-insertions"),
])
def test_retype_keeps_a_middle_replacement_in_place_after_relay_insertions(matching_budget, prefix, suffix,
                                                                         remove_word):
    words = [f"word{i:03}" for i in range(300)]
    base = " ".join(words)
    local = base.replace("word150", "replacement")

    def relay_text(text):
        if remove_word:
            text = text.replace("word100 ", "")
        return prefix + text + suffix

    doc = SharedDoc.new({"s": relay_text(base)})
    plan_drafts.retype(doc, {"s": base}, {"s": local})

    assert doc.text("s") == relay_text(local)


def test_retype_keeps_a_relay_replacement_when_local_inserts_long_edges(matching_budget):
    base = " ".join(f"word{i:03}" for i in range(300))
    relay = base.replace("word150", "replacement")
    doc = SharedDoc.new({"s": relay})
    prefix, suffix = "[" * 30, "]" * 30

    plan_drafts.retype(doc, {"s": base}, {"s": prefix + base + suffix})

    assert doc.text("s") == prefix + relay + suffix


def test_small_diffs_keep_legacy_offsets():
    # A repeated character can be either the first match or a common suffix.
    # The old matcher picked the first match, which matters for draft cursors.
    at, changed = plan_drafts.mapping("ba", "aa")
    assert changed == [(0, 1), (2, 2)]
    assert [at(pos) for pos in range(3)] == [0, 0, 1]
    assert [(s["kind"], s["before_start"], s["start"]) for s in _compare("ba", "aa")] == [
        ("delete", 0, 0), ("insert", 2, 1),
    ]
    result = plan_rebase.rebase("Budget: 100. Due Monday.", "Budget: 200. Due Monday.", "Budget: 100. Due Friday.")
    assert result.text == "Budget: 200. Due Friday."
    assert result.applied == (plan_rebase.Hunk(8, 9, "2"),)
