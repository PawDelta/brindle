"""The share panel's access list: each person's level reaches the Library as
that role, sign-off is kept apart from access, and Stop sharing revokes."""

from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_share_grants_access import AccessLibrary
from test_shared_plan_docs import BASE, DESIGN, DOC, TEAM, parts, sent  # noqa: F401

PEOPLE = ("ann", "bo")
ROLES = {"view": {}, "comment": {"can_comment": True},
         "edit": {"can_edit": True, "can_comment": True, "can_suggest": True}}


class PeopleLibrary(AccessLibrary):
    """The Library's per-person routes: one person's share, role and sign-off
    each change alone, as the server's atomic operations do."""

    def __init__(self, remote):
        super().__init__(remote)
        self.members[("group", "t_1")] = {"teammate"}
        for sub in PEOPLE:
            remote.routes.update({
                f"PUT {DESIGN}/permissions/{sub}": lambda f, h, sub=sub: self.role(sub, f),
                f"PUT {DESIGN}/reviewers/{sub}": lambda f, h, sub=sub: self.sign(sub, f.get("required", True)),
                f"DELETE {DESIGN}/reviewers/{sub}": lambda f, h, sub=sub: self.sign(sub, None),
                f"DELETE {BASE}/share/user/{sub}": lambda f, h, sub=sub: self.revoke(sub),
            })
        remote.routes[f"DELETE {BASE}/share/group/t_1"] = lambda f, h: self.drop(TEAM)
        remote.routes[f"DELETE {DESIGN}/share-link"] = self.unlink
        self.link, self.unlink_status = None, 200

    def unlink(self, form, headers):
        if self.link is None:
            return 404, {"error": "not_found"}
        if self.unlink_status != 200:
            return self.unlink_status, {"error": "unavailable"}
        self.link = None
        return 200, {"link": None}

    def readable(self):
        # "stranger" holds the plan's share link and nothing else.
        return (super().readable() or {"kind": "user", "id": self.actor} in self.scopes
                or (self.actor == "stranger" and self.link is not None))

    def role(self, sub, form):
        self.permissions[sub] = dict(ROLES[form["role"]])
        self.reviewers = [r for r in self.reviewers if r["sub"] != sub]
        return 200, self.view()

    def sign(self, sub, required):
        self.reviewers = [r for r in self.reviewers if r["sub"] != sub]
        if required is not None:
            self.reviewers.append({"sub": sub, "required": required})
        return 200, self.view()

    def share(self, form, headers):
        scope = form["scope"]
        if scope not in self.scopes:
            self.scopes.append(scope)
        if scope["kind"] != "user":
            return 200, {"id": DOC, "scopes": list(self.scopes)}
        self.role(scope["id"], {"role": form.get("role", "view")})
        return 200, {"id": DOC, "scopes": list(self.scopes), "design": self.view()}

    def revoke(self, sub):
        self.drop({"kind": "user", "id": sub})
        self.permissions.pop(sub, None)
        self.sign(sub, None)
        return 200, {"id": DOC, "scopes": list(self.scopes), "design": self.view()}

    def drop(self, scope):
        self.scopes = [s for s in self.scopes if s != scope]
        return 200, {"id": DOC, "scopes": list(self.scopes)}


@pytest.fixture
def panel(parts, backend, monkeypatch):
    pid, _, net = parts
    library = PeopleLibrary(backend)
    monkeypatch.setattr(ad, "_session", lambda *a: (net["client"], net["store"]))
    entitlement = ad.require_entitlement(net["store"])
    monkeypatch.setattr(ad, "require_entitlement", lambda store: replace(entitlement, sub=library.actor))
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        yield pid, library, app


def as_(library, app, who):
    """The design as ``who`` reads it, or None when the Library won't show it."""
    library.actor, before = who, library.actor
    try:
        got = app.get(f"/v1/shared-designs/{DOC}")
        return got.json() if got.status_code == 200 else None
    finally:
        library.actor = before


def person(app, pid, sub):
    return next((p for p in app.get(f"/v1/plans/{pid}/access").json()["people"] if p["sub"] == sub), None)


@pytest.mark.parametrize("access", ["view", "comment", "edit"])
def test_first_person_shares_the_plan_at_their_access_level(panel, backend, access):
    pid, library, app = panel
    before = app.get(f"/v1/plans/{pid}/access").json()
    assert not before["shared"] and before["people"] == []
    added = app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": access})
    assert added.status_code == 200, added.json()
    assert added.json()["gate"] is not None                     # adding the first person shared it
    assert sent(backend, f"POST {BASE}/share") == [{"scope": {"kind": "user", "id": "ann"}, "role": access}]
    assert not sent(backend, f"PUT {DESIGN}/permissions")       # never the whole map
    ann = as_(library, app, "ann")
    assert ann is not None
    assert (ann["can_edit"], ann["can_comment"]) == (access == "edit", access != "view")
    assert person(app, pid, "ann") == {"sub": "ann", "name": None, "access": access, "sign_off": None}


def test_sign_off_is_kept_apart_from_access(panel):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "edit", "sign_off": True}).status_code == 200
    assert person(app, pid, "ann")["sign_off"] == "required"
    assert as_(library, app, "ann")["can_edit"]
    # Turning sign-off off leaves the editor an editor.
    assert app.put(f"/v1/plans/{pid}/people/ann/sign-off", json={"must": False}).status_code == 200
    assert person(app, pid, "ann") == {"sub": "ann", "name": None, "access": "edit", "sign_off": None}
    assert app.put(f"/v1/plans/{pid}/people/ann/sign-off", json={"must": True}).status_code == 200
    # A new access level keeps the sign-off the person had.
    changed = app.put(f"/v1/plans/{pid}/people/ann", json={"access": "view", "sign_off": True})
    assert changed.status_code == 200, changed.json()
    assert person(app, pid, "ann")["access"] == "view" and person(app, pid, "ann")["sign_off"] == "required"
    assert [r["sub"] for r in changed.json()["gate"]["reviewers"] if r.get("sub")] == ["ann"]
    ann = as_(library, app, "ann")
    assert not ann["can_edit"] and ann["can_sign_off"]


def test_stop_sharing_revokes_people_teams_and_the_whole_plan(panel):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "comment"}).status_code == 200
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view", "sign_off": True}).status_code == 200
    assert app.post(f"/v1/plans/{pid}/share", json={"scope": TEAM}).status_code == 200
    assert app.get(f"/v1/plans/{pid}/access").json()["scopes"] == [TEAM]
    # One person: the others keep it.
    assert app.delete(f"/v1/plans/{pid}/people/ann").status_code == 200
    assert as_(library, app, "ann") is None and as_(library, app, "bo") is not None
    # One team.
    assert as_(library, app, "teammate") is not None
    assert app.delete(f"/v1/plans/{pid}/scopes/group/t_1").status_code == 200
    assert as_(library, app, "teammate") is None
    # Everyone: bo, the team shared again, and bo's sign-off goes too.
    assert app.post(f"/v1/plans/{pid}/share", json={"scope": TEAM}).status_code == 200
    stopped = app.delete(f"/v1/plans/{pid}/access")
    assert stopped.status_code == 200, stopped.json()
    assert as_(library, app, "bo") is None and as_(library, app, "teammate") is None
    left = app.get(f"/v1/plans/{pid}/access").json()
    assert left["shared"] and left["people"] == [] and left["scopes"] == []
    assert library.scopes == [{"kind": "user", "id": library.author}]


@pytest.mark.parametrize("unlink", [200, 503])
def test_stop_sharing_turns_the_link_off_or_fails_without_revoking_anyone(panel, unlink):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "view"}).status_code == 200
    library.link, library.unlink_status = {"url": "https://plans.example/dsl_1"}, unlink
    assert as_(library, app, "stranger") is not None
    stopped = app.delete(f"/v1/plans/{pid}/access")
    if unlink == 200:
        assert stopped.status_code == 200, stopped.json()
        assert as_(library, app, "stranger") is None and as_(library, app, "ann") is None
    else:
        # The refusal reaches the panel, and nobody was told it stopped while the link still opens it.
        assert stopped.status_code >= 500
        assert as_(library, app, "stranger") is not None and as_(library, app, "ann") is not None
