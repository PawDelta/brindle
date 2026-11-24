"""The share panel's org directory and the plan's separate access list."""

from dataclasses import replace

import pytest

from brindle.daemon import routes_actions, routes_visuals
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import session  # noqa: F401
from test_share_panel_access import add_person, panel  # noqa: F401
from test_share_people_regression import MEMBERS
from test_shared_plan_docs import parts, sent  # noqa: F401


def test_plain_member_reuses_the_directory_across_the_first_share(panel, backend, monkeypatch):
    pid, _, app = panel
    backend.routes["GET /orgs/org_1/members"] = lambda f, h: (200, {"members": MEMBERS})
    require = routes_actions._docs_entitlement
    monkeypatch.setattr(routes_actions, "_docs_entitlement", lambda *args: replace(require(*args), role="member"))

    def directory():
        start = len(backend.calls)
        got = app.get("/v1/library/people")
        assert got.status_code == 200, got.json()
        assert got.json()["admin_org"] is None
        assert {path for path, _, _ in backend.calls[start:]} <= {"GET /orgs/org_1/members"}
        return got.json()["people"]

    before = directory()
    assert {p["sub"] for p in before} == {m["sub"] for m in MEMBERS} - {"user_1"}
    assert app.get(f"/v1/plans/{pid}/access").json()["people"] == []
    assert add_person(app, pid, {"sub": "owner_1", "access": "comment"}).status_code == 202
    assert directory() == before
    access = app.get(f"/v1/plans/{pid}/access").json()["people"]
    assert {p["sub"] for p in access} & {p["sub"] for p in before} == {"owner_1"}
    assert len(sent(backend, "GET /orgs/org_1/members")) == 1


def test_the_directory_refreshes_after_its_cache_expires(panel, backend, monkeypatch):
    _, _, app = panel
    now = 100.0
    monkeypatch.setattr(routes_visuals, "monotonic", lambda: now)
    members = list(MEMBERS)
    backend.routes["GET /orgs/org_1/members"] = lambda f, h: (200, {"members": members})

    def subs():
        got = app.get("/v1/library/people")
        assert got.status_code == 200, got.json()
        return {p["sub"] for p in got.json()["people"]}

    before = subs()
    members.pop()
    assert subs() == before
    now += 600
    assert subs() == before - {"ann"}


@pytest.mark.variant("A still-fresh directory must exclude the newly signed-in member instead of the previous member")
def test_the_directory_follows_an_account_change_within_the_same_org(panel, backend):
    _, library, app = panel
    backend.routes["GET /orgs/org_1/members"] = lambda f, h: (200, {"members": MEMBERS})

    def subs():
        got = app.get("/v1/library/people")
        assert got.status_code == 200, got.json()
        return {p["sub"] for p in got.json()["people"]}

    assert subs() == {"ann", "owner_1"}
    library.actor = "ann"
    assert subs() == {"user_1", "owner_1"}
