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

    def save(*args, **kw):
        with monkeypatch.context() as m:
            m.delenv("BRINDLE_AGENT_ID", raising=False)
            return real(*args, **kw)

    monkeypatch.setattr(doc_templates, "save", save)


def _format(blocks=("sign_off",), reference="DES-{number}"):
    """The person's own (local) format."""
    doc_templates.save(doc_templates.ORG, {"templates": [DESIGN], "blocks": list(blocks),
                                           "naming": {"reference": reference, "title_hint": "What it does"}},
                       doc_templates.LocalStore())


def _propose(db, boss, markdown="# Presets\n\n## Summary\n\nPresets per org.", plan_id=None):
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    reply = mcp_server.propose_plan(markdown, plan_id=plan_id)
    return reply.split()[2], reply


def test_new_plan_starts_from_the_template_and_gets_the_next_reference(db, session):
    boss, _ = session
    _format()
    pid, reply = _propose(db, boss, "# Presets\n\n## Risks\n\nNone.\n\n## Architecture\n\nOne box.\n\n"
                                    "## Notes\n\nSee chat.\n\n## Summary\n\nPresets per org.")
    titles = [s["title"] for s in plan_docs.revision(db, pid)["sections"]]
    # The template's order wins; the supervisor's own sections stay after the one they followed
    # (Risks, before any, stays first), and a missing template heading comes in empty.
    assert titles == ["Risks", "Summary", "Architecture", "Notes", "Rollout"]
    assert "## Rollout" in plans.get(db, pid)["markdown"]
    # The supervisor is told the whole template: every section's kind and hint, and the title hint.
    assert "DES-0001" in reply and "Title: What it does" in reply and "Architecture (diagram)" in reply
    assert "Summary (text, required): What changes for the user." in reply and "still empty: Rollout" in reply
    assert doc_templates.view(db)["next_reference"] == "DES-0002"
    assert plans.to_dto(db, plans.get(db, pid))["reference"] == "DES-0001"

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


def test_a_doc_follows_its_teams_format(db, session):
    """Sharing to a team checks that team's format, and the doc's sign-off follows it from then on."""
    from brindle.pro import shared_docs

    boss, _ = session
    _format([])                                               # the org's format stops nothing
    doc_templates.save("team:t1", {"blocks": ["share", "sign_off"]})
    pid, _ = _propose(db, boss)
    with pytest.raises(RuntimeError, match="not logged in"):   # past the check: the org's format lets it through
        shared_docs.share(db, pid, {"kind": "org", "id": "o1"})
    with pytest.raises(ServiceError) as e:
        shared_docs.share(db, pid, {"kind": "group", "id": "t1"})
    assert e.value.code == "required_sections_missing"
    doc_templates.set_team(db, pid, "t1")                     # as a share to t1 records it
    with pytest.raises(ServiceError, match="Rollout"):
        plans.decide(db, pid, 1, True, person=True)


class FakeLibrary:
    """pawdelta-web's doc format routes (backend/pro_api/doc_formats.py) for org o1, in memory: levels
    org > eng (a structure node), a design filed under eng, the template setting locked at ``locked``."""

    BASE = "/orgs/o1/analysis-docs/format/"

    def __init__(self):
        self.levels, self.version, self.counters = {}, 0, {}
        self.designs, self.parents, self.locked = {"doc-1": "eng"}, {"eng": "org"}, set()
        self.folders = {("group", "t1", "f1"): "eng"}      # a folder the eng level holds; any other is the org's
        self.admin, self.down, self.calls = True, False, []

    def _chain(self, node):
        return ([] if node == "org" else self._chain(self.parents[node])) + [node]

    def _effective(self, node):
        out = {"templates": [], "naming": {"title_hint": None, "reference": None}, "blocks": ["sign_off"]}
        sources = dict.fromkeys(out)
        for n in self._chain(node):
            for k, v in self.levels.get(n, {}).items():
                out[k], sources[k] = v, n
        return {**out, "from": sources, "version": self.version, "node": node}

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if self.down:
            raise RuntimeError("the Library couldn't be reached")
        assert path.startswith(self.BASE), path
        parts = path[len(self.BASE):].split("/")
        if parts[0] == "designs":
            return 200, self._effective(self.designs[parts[1]])
        if parts[0] == "folders":
            return 200, self._effective(self.folders.get(tuple(parts[1:4]), "org"))
        node = parts[1]
        if method == "GET":
            return 200, self._effective(node)
        if method == "POST":
            pattern = self._effective(node)["naming"]["reference"]
            number = self.counters.get(pattern, 1)
            self.counters[pattern] = number + 1
            return 200, {"reference": doc_templates.reference(pattern, number)}
        if not self.admin:
            return 403, {"error": "forbidden"}
        if any(n in self.locked for n in self._chain(node)[:-1]):
            return 422, {"error": "locked", "locked_by": "org"}
        if body["version"] != self.version:
            return 409, {"error": "conflict"}
        self.levels[node] = {k: v for k, v in body["format"].items() if v is not None}
        self.version += 1
        return 200, self._effective(node)


@pytest.fixture
def library(monkeypatch):
    """Signed in to a Team-plan org whose formats the Library keeps."""
    from types import SimpleNamespace

    from brindle.pro import license, shared_docs

    fake = FakeLibrary()
    monkeypatch.setattr(license, "current", lambda **kw: SimpleNamespace(
        org_id="o1", features=frozenset({shared_docs.TEAM_FEATURE})))
    monkeypatch.setattr(doc_templates.ProStore, "_call", lambda self, method, path, body=None: fake(method, path, body))
    return fake


def test_an_orgs_format_lives_in_the_library(db, library):
    """Saving names the version read; the Library refuses a stale one, a non-admin and a locked level."""
    view = doc_templates.view(db)
    assert view["source"] == "org" and view["format"] == {} and view["version"] == 0
    doc_templates.save("org", {"templates": [DESIGN], "naming": {"reference": "DES-{number}"}}, version=0)
    doc_templates.save("eng", {"blocks": ["share"]}, version=1)
    eng = doc_templates.view(db, "eng")
    # A level shows only what it sets itself; what's in force there comes down from the org.
    assert eng["format"] == {"blocks": ["share"]} and eng["resolved"]["templates"][0]["id"] == "design"
    for arrange, code in [(lambda: None, "conflict"), (lambda: setattr(library, "admin", False), "forbidden"),
                          (lambda: library.locked.add("org"), "locked")]:
        library.admin = True
        arrange()
        with pytest.raises(ServiceError) as e:
            doc_templates.save("eng", {"blocks": []}, version=0 if code == "conflict" else 2)
        assert e.value.code == code
    assert library.levels["eng"] == {"blocks": ["share"]}


def test_an_org_plan_takes_the_librarys_template_reference_and_gates(db, session, library):
    from brindle.pro import shared_docs

    boss, _ = session
    library.levels = {"org": {"templates": [DESIGN], "naming": {"reference": "DES-{number}"}, "blocks": []},
                      "eng": {"blocks": ["sign_off", "share"]}}
    library.counters["DES-{number}"] = 41
    _format(reference="LOCAL-{number}")                        # the person's own format is never used for an org
    pid, reply = _propose(db, boss)
    assert "## Rollout" in plans.get(db, pid)["markdown"]
    assert doc_templates.reference_of(db, pid) == "DES-0041" and "DES-0041" in reply
    sections = plan_docs.revision(db, pid)["sections"]
    doc_templates.require(db, pid, doc_templates.SIGN_OFF, sections)     # the org's level stops nothing
    # Shared into a folder, the folder's level decides: eng's stops the share, the org's doesn't.
    scope = {"kind": "group", "id": "t1"}
    with pytest.raises(ServiceError, match="Rollout"):
        shared_docs.share(db, pid, scope, folder={"scope": scope, "id": "f1"})
    with pytest.raises(RuntimeError):                                     # past the check: on to the Library
        shared_docs.share(db, pid, scope, folder={"scope": scope, "id": "f2"})
    # Shared, it follows its design's format in the Library: filed under eng, sign-off stops.
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner) VALUES (?, 'doc-1', 'me')", (pid,))
    with pytest.raises(ServiceError, match="Rollout"):
        doc_templates.require(db, pid, doc_templates.SIGN_OFF, sections)
    assert ("GET", FakeLibrary.BASE + "designs/doc-1") in library.calls


def test_an_orgs_format_never_comes_from_this_machine(db, session, library):
    """While the Library can't be read an org has no format: not the person's local one, which
    another org may have set. A plan written then has no template and no reference."""
    boss, _ = session
    _format()
    library.down = True
    assert doc_templates.resolve()["templates"] == []
    with pytest.raises(ServiceError) as e:
        doc_templates.save(doc_templates.ORG, {"blocks": []}, version=0)
    assert e.value.code == "upstream"
    pid, _ = _propose(db, boss)
    assert doc_templates.of(db, pid) is None and "Rollout" not in plans.get(db, pid)["markdown"]


def test_reference_pattern_and_levels(db, tmp_path):
    local = doc_templates.LocalStore()
    with pytest.raises(ServiceError):
        doc_templates.save(doc_templates.ORG, {"naming": {"reference": "DES-"}})     # no {number}
    _format(reference="SEC-{number}/r")
    doc_templates.save("team:t1", {"naming": {"reference": "TEAM-{number}"}})
    assert doc_templates.resolve("team:t1")["naming"]["reference"] == "TEAM-{number}"
    assert doc_templates.resolve("team:t1")["templates"][0]["id"] == "design"     # inherited from the org
    assert [local.take(db, "org") for _ in range(2)] == ["SEC-0001/r", "SEC-0002/r"]
    assert local.take(db, "team:t1") == "TEAM-0001"                                # numbered per pattern


def test_without_a_format_nothing_changes(db, session):
    boss, _ = session
    pid, reply = _propose(db, boss, "# Presets\n\nOne page.\n\n## Goal\n\nPresets.")
    assert plans.get(db, pid)["markdown"] == "# Presets\n\nOne page.\n\n## Goal\n\nPresets."
    assert doc_templates.of(db, pid) is None and "Reference" not in reply
