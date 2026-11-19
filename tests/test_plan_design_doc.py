"""A plan as a design doc: the fixed section spine, optional extras only when
they apply, decisions anchored to a section, and the worker split kept off
the person's doc."""

from brindle import plans, visual_offers
from test_plans import DECISIONS, session  # noqa: F401 - fixture

SPLIT = [{"id": "w1", "title": "Build it", "files": ["a.py"], "test": "uv run pytest a"}]


def _propose(db, boss, sections, decisions=None):
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    p = plans.propose(db, boss, "# Title\n\nLede.", decisions, SPLIT, sections=sections)
    return plans.to_dto(db, plans.get(db, p["id"]))


def test_extras_left_out_or_empty_are_not_present(db, session):
    boss, _ = session
    dto = _propose(db, boss, [
        {"id": "rollout", "title": "Rollout and tasks", "markdown": "Phase one."},
        {"id": "tech-stack", "title": "Tech stack", "markdown": ""},
        {"id": "context", "title": "Context", "markdown": "Why."},
        {"id": "open-questions", "title": "Open questions", "markdown": ""},
    ])
    assert [s["id"] for s in dto["sections"]] == ["context", "rollout", "open-questions"]
    assert dto["sections"][-1]["markdown"] == "None."
    assert "Tech stack" not in dto["markdown"]


def test_decision_attaches_to_its_section_else_open_questions(db, session):
    boss, _ = session
    anchored = {**DECISIONS[0], "section": "proposed-design"}
    elsewhere = {**DECISIONS[0], "id": "d2", "section": "no-such-section"}
    plain = {**DECISIONS[0], "id": "d3"}
    dto = _propose(db, boss, [{"id": "proposed-design", "title": "Proposed design", "markdown": "X."}],
                   [anchored, elsewhere, plain])
    assert {d["id"]: d["section"] for d in dto["decisions"]} == {
        "d1": "proposed-design", "d2": "open-questions", "d3": "open-questions"}


def test_split_is_off_the_persons_doc_but_still_links_workers(db, session):
    boss, _ = session
    dto = _propose(db, boss, [{"id": "rollout", "title": "Rollout", "markdown": "Phase one."}])
    assert dto["split"] == []
    assert "split" not in dto["markdown"].lower() and "uv run pytest a" not in dto["markdown"]
    assert "split" not in plans.anchors(plans.get(db, dto["id"]))
    assert plans.assignment(db, boss, f"{dto['id']}:w1") == (dto["id"], "w1")
