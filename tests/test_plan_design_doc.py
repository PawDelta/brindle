"""A plan as a design doc: the fixed section spine, optional extras only when
they apply, decisions anchored to a section, and the worker split kept off
the person's doc."""

import pytest

from brindle import plans, visual_offers
from test_plans import DECISIONS, session  # noqa: F401 - fixture

SPLIT = [{"id": "w1", "title": "Build it", "files": ["a.py"], "test": "uv run pytest a"}]


def _propose(db, boss, sections, decisions=None):
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    p = plans.propose(db, boss, "# Title\n\nLede.", decisions, SPLIT, sections=sections)
    return plans.to_dto(db, plans.get(db, p["id"]))


@pytest.mark.variant("an optional extra sent empty is never stored, and an empty Open questions reads None.")
def test_extras_left_out_or_empty_are_not_present(db, session):
    boss, _ = session
    dto = _propose(db, boss, [
        {"id": "rollout", "title": "Rollout and tasks", "markdown": "Phase one."},
        {"id": "tech-stack", "title": "Tech stack", "markdown": ""},
        {"id": "context", "title": "Context", "markdown": "Why."},
        {"id": "open-questions", "title": "Open questions", "markdown": ""},
    ])
    assert [s["id"] for s in dto["sections"]] == [
        "context", "goals", "proposed-design", "alternatives", "risks", "rollout", "open-questions"]
    assert dto["sections"][-1]["markdown"] == "None."
    assert "Tech stack" not in dto["markdown"]


@pytest.mark.parametrize("body, first", [
    pytest.param("## Context\n\nWhy.\n\n## Open questions\n", [], marks=pytest.mark.variant(
        "markdown-only spine headings, one left empty, land on the spine once each")),
    # No spine heading at all: a new plan still gets the whole spine, its own section first.
    pytest.param("## Summary\n\nA small change.", ["s1"], marks=pytest.mark.variant(
        "markdown with no spine heading keeps its own section first and still gets the whole spine")),
])
def test_markdown_only_proposal_maps_headings_to_the_spine(db, session, body, first):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    p = plans.propose(db, boss, f"# T\n\nLede.\n\n{body}",
                      [{**DECISIONS[0], "section": "context"}, {**DECISIONS[0], "id": "d2"}])
    dto = plans.to_dto(db, plans.get(db, p["id"]))
    ids = [s["id"] for s in dto["sections"]]
    assert ids == [*first, "context", "goals", "proposed-design", "alternatives", "risks", "rollout",
                   "open-questions"]
    assert [d["section"] for d in dto["decisions"]] == ["context", "open-questions"]
    assert {d["section"] for d in dto["decisions"]} <= set(ids)


@pytest.mark.variant("a decision naming a section that doesn't exist falls back to Open questions, never dangles")
def test_decision_attaches_to_its_section_else_open_questions(db, session):
    boss, _ = session
    anchored = {**DECISIONS[0], "section": "proposed-design"}
    elsewhere = {**DECISIONS[0], "id": "d2", "section": "no-such-section"}
    plain = {**DECISIONS[0], "id": "d3"}
    dto = _propose(db, boss, [{"id": "proposed-design", "title": "Proposed design", "markdown": "X."}],
                   [anchored, elsewhere, plain])
    assert {d["id"]: d["section"] for d in dto["decisions"]} == {
        "d1": "proposed-design", "d2": "open-questions", "d3": "open-questions"}
    assert {d["section"] for d in dto["decisions"]} <= {s["id"] for s in dto["sections"]}


@pytest.mark.variant("the worker split never shows on the person's doc, yet plan_worker ids still resolve")
def test_split_is_off_the_persons_doc_but_still_links_workers(db, session):
    boss, _ = session
    dto = _propose(db, boss, [{"id": "rollout", "title": "Rollout", "markdown": "Phase one."}])
    assert dto["split"] == []
    assert "split" not in dto["markdown"].lower() and "uv run pytest a" not in dto["markdown"]
    assert "split" not in plans.anchors(plans.get(db, dto["id"]))
    assert plans.assignment(db, boss, f"{dto['id']}:w1") == (dto["id"], "w1")
