"""Doc format (brindle.doc_templates): a new plan starts from the default
template, its reference is numbered from the naming pattern, and an empty
required section stops what the format says it stops."""

import pytest

from brindle import doc_templates, mcp_server, plan_docs, plans, visual_offers
from brindle.service.errors import ServiceError
from test_plans import session  # noqa: F401 - fixture

DESIGN = {"id": "design", "name": "Design doc", "default": True, "sections": [
    {"title": "Summary", "kind": "text", "required": True, "hint": "What changes for the user."},
    {"title": "Architecture", "kind": "diagram", "required": False},
    {"title": "Rollout", "kind": "text", "required": True, "hint": "Numbered steps, and how to turn it off."},
]}


@pytest.fixture(autouse=True)
def as_the_person(monkeypatch):
    """The person saves the format (from the app): an agent's process may not change their settings."""
    real = doc_templates.save

    def save(level, raw):
        with monkeypatch.context() as m:
            m.delenv("BRINDLE_AGENT_ID", raising=False)
            return real(level, raw)

    monkeypatch.setattr(doc_templates, "save", save)


def _format(blocks=("sign_off",), reference="DES-{number}"):
    doc_templates.save(doc_templates.ORG, {"templates": [DESIGN], "naming": {"reference": reference},
                                           "blocks": list(blocks)})


def _propose(db, boss, markdown="# Presets\n\n## Summary\n\nPresets per org.", plan_id=None):
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    reply = mcp_server.propose_plan(markdown, plan_id=plan_id)
    return reply.split()[2], reply


def test_new_plan_starts_from_the_template_and_gets_the_next_reference(db, session):
    boss, _ = session
    _format()
    pid, reply = _propose(db, boss, "# Presets\n\n## Risks\n\nNone.\n\n## Summary\n\nPresets per org.")
    titles = [s["title"] for s in plan_docs.revision(db, pid)["sections"]]
    # The supervisor's own sections and order stay; the template's missing headings come in after the one before.
    assert titles == ["Risks", "Summary", "Architecture", "Rollout"]
    assert "## Rollout" in plans.get(db, pid)["markdown"]
    assert "DES-0001" in reply and "Rollout (Numbered steps" in reply and "Summary (" not in reply
    assert doc_templates.view(db)["next_reference"] == "DES-0002"

    # The plan keeps the template it was written with: a later change doesn't reach it.
    doc_templates.save(doc_templates.ORG, {"templates": [{**DESIGN, "sections": DESIGN["sections"][:1]}]})
    plans.decide(db, pid, 1, False, note="Fill the rollout.", person=True)
    pid2, _ = _propose(db, boss, "# Presets\n\n## Summary\n\nPresets per org.", plan_id=pid)
    assert "Rollout" in [s["title"] for s in plan_docs.revision(db, pid2)["sections"]]
    assert doc_templates.of(db, pid)["reference"] == "DES-0001"


@pytest.mark.parametrize("blocks, refused", [
    pytest.param(["sign_off"], True, id="blocks-sign-off"),
    pytest.param(["share"], False, id="blocks-share-only"),
])
def test_empty_required_section_blocks_sign_off(db, session, blocks, refused):
    boss, _ = session
    _format(blocks)
    pid, _ = _propose(db, boss)
    if refused:
        with pytest.raises(ServiceError) as e:
            plans.decide(db, pid, 1, True, person=True)
        assert e.value.code == "required_sections_missing" and e.value.extra["missing"] == ["Rollout"]
        assert plans.get(db, pid)["state"] == "open"
        # Filled, it goes.
        plan_docs.edit(db, pid, 1, plan_docs.revision(db, pid)["sections"][-1]["id"], 0, "", "1. Ship it.")
        plans.decide(db, pid, 2, True, person=True)
    else:
        plans.decide(db, pid, 1, True, person=True)
    assert plans.get(db, pid)["state"] == "approved"


def test_empty_required_section_blocks_sharing(db, session):
    from brindle.pro import shared_docs

    boss, _ = session
    _format(["share"])
    pid, _ = _propose(db, boss)
    with pytest.raises(ServiceError) as e:
        shared_docs.share(db, pid, {"kind": "org"})
    assert e.value.code == "required_sections_missing"


def test_reference_pattern_and_levels(db, tmp_path):
    with pytest.raises(ServiceError):
        doc_templates.save(doc_templates.ORG, {"naming": {"reference": "DES-"}})     # no {number}
    _format(reference="SEC-{number}/r")
    doc_templates.save("team:t1", {"naming": {"reference": "TEAM-{number}"}})
    assert doc_templates.resolve("t1")["naming"]["reference"] == "TEAM-{number}"
    assert doc_templates.resolve("t1")["templates"][0]["id"] == "design"           # inherited from the org
    with db.tx() as c:
        assert [doc_templates._take(c, "SEC-{number}/r") for _ in range(2)] == ["SEC-0001/r", "SEC-0002/r"]
        assert doc_templates._take(c, "TEAM-{number}") == "TEAM-0001"                # numbered per pattern


def test_without_a_format_nothing_changes(db, session):
    boss, _ = session
    pid, reply = _propose(db, boss, "# Presets\n\nOne page.\n\n## Goal\n\nPresets.")
    assert plans.get(db, pid)["markdown"] == "# Presets\n\nOne page.\n\n## Goal\n\nPresets."
    assert doc_templates.of(db, pid) is None and "Reference" not in reply
