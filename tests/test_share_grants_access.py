"""Share routes grant usable roles against a Library enforcing live scope access."""

from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_shared_plan_docs import BASE, DESIGN, DOC, TEAM, Library, parts  # noqa: F401


class AccessLibrary(Library):
    def __init__(self, remote):
        super().__init__(remote)
        self.author = "user_1"
        self.actor = self.author
        self.revoke_before_grant = False
        self.revoked = False
        self.scopes = [{"kind": "user", "id": self.author}]
        self.members = {("group", "t_1"): {"reviewer", "viewer"}, ("org", "org_1"): {"reviewer", "viewer"}}
        remote.routes.update({
            f"GET {BASE}": lambda f, h: self.metadata(),
            f"GET {DESIGN}": lambda f, h: self.read(),
            f"PUT {DESIGN}/reviewers/reviewer": self.put_reviewer,
            f"PUT {DESIGN}/permissions/reviewer": self.put_role,
        })

    def readable(self):
        return self.actor == self.author or any(
            self.actor in self.members.get((s["kind"], s["id"]), set()) for s in self.scopes)

    def allowed(self, flag):
        return self.readable() and (self.actor == self.author or self.permissions.get(self.actor, {}).get("can_edit")
                                   or self.permissions.get(self.actor, {}).get(flag, False))

    def read(self):
        answer = (200, self.view()) if self.readable() else (404, {"error": "not_found"})
        # The client can receive a snapshot just before another editor revokes a grant.
        self.revoke_edit()
        return answer

    def revoke_edit(self):
        if self.revoke_before_grant:
            self.permissions.pop("viewer", None)
            self.revoke_before_grant = False
            self.revoked = True

    def metadata(self):
        return (200, {"id": DOC, "title": self.title, "scope": self.scopes[0], "scopes": list(self.scopes)})

    def share(self, form, headers):
        if form["scope"] not in self.scopes:
            self.scopes.append(form["scope"])
        return 200, {"id": DOC, "scopes": list(self.scopes)}

    def put_reviewer(self, form, headers):
        self.reviewers = [{"sub": "reviewer", "required": form["required"]}]
        return self.read()

    def put_role(self, form, headers):
        """``{sign_off}``: the access held is kept and a signer can comment and suggest."""
        self.revoke_edit()
        self.permissions["reviewer"] = {"can_edit": False, **self.permissions.get("reviewer", {}),
                                        "can_comment": True, "can_suggest": True}
        self.reviewers = ([{"sub": "reviewer", "required": form["sign_off"] == "required"}]
                          if form["sign_off"] != "none" else [])
        return self.read()

    def comment(self, form, headers):
        if not self.allowed("can_comment"):
            return 403, {"error": "forbidden"}
        return super().comment(form, headers)

    def sign_off(self, form, headers):
        if not self.readable() or not any(r["sub"] == self.actor for r in self.reviewers):
            return 403, {"error": "forbidden"}
        self.sign_offs.append({"rev": form["rev"], "sub": self.actor, "at": 9.0})
        return self.read()


@pytest.fixture
def access(parts, backend, monkeypatch):
    pid, _, net = parts
    library = AccessLibrary(backend)
    monkeypatch.setattr(ad, "_session", lambda *a: (net["client"], net["store"]))
    entitlement = ad.require_entitlement(net["store"])
    monkeypatch.setattr(ad, "require_entitlement", lambda store: replace(entitlement, sub=library.actor))
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        yield pid, library, app


@pytest.mark.parametrize("target", ["plan", "library"])
@pytest.mark.variant("the plan and Library reader routes each grant reviewer rights")
def test_share_panel_reviewer_can_comment_and_sign_off(access, target):
    pid, library, app = access
    assert app.post(f"/v1/plans/{pid}/share", json={"scope": TEAM}).status_code == 200
    base = f"/v1/plans/{pid}" if target == "plan" else f"/v1/library/designs/{DOC}"
    invited = app.put(f"{base}/reviewers/reviewer", json={"required": True})
    assert invited.status_code == 200, invited.json()
    library.actor = "reviewer"
    route = f"/v1/shared-designs/{DOC}"
    read = app.get(route)
    assert read.status_code == 200
    assert read.json()["can_comment"] and read.json()["can_sign_off"]
    commented = app.post(f"{route}/comments", json={"rev": 1, "anchor": {"kind": "section", "section": "summary"},
                                                        "text": "Looks good."})
    assert commented.status_code == 200, commented.json()
    signed = app.post(f"{route}/sign-offs", json={"rev": 1})
    assert signed.status_code == 200 and signed.json()["signed_off"]


@pytest.mark.variant("a downgrade to view must remove edit and comment")
def test_share_viewer_reads_but_cannot_edit_after_role_change(access):
    pid, library, app = access
    route = f"/v1/plans/{pid}/share"
    assert app.post(route, json={"scope": TEAM, "permissions": {"viewer": {"can_edit": True}}}).status_code == 200
    library.actor = "viewer"
    assert app.get(f"/v1/shared-designs/{DOC}").json()["can_edit"]
    library.actor = library.author
    assert app.post(route, json={"scope": TEAM, "permissions": {"viewer": {}}}).status_code == 200
    library.actor = "viewer"
    read = app.get(f"/v1/shared-designs/{DOC}")
    assert read.status_code == 200 and not read.json()["can_edit"]
    assert not read.json()["can_comment"] and not read.json()["can_suggest"]


@pytest.mark.parametrize("target", ["plan", "library"])
@pytest.mark.variant("another editor revokes edit after the old snapshot is read but before the reviewer grant commits")
def test_reviewer_invite_does_not_restore_an_interleaved_revoked_edit(access, target):
    pid, library, app = access
    assert app.post(f"/v1/plans/{pid}/share", json={"scope": TEAM}).status_code == 200
    library.permissions["viewer"] = {"can_edit": True}
    library.actor = "viewer"
    assert app.get(f"/v1/shared-designs/{DOC}").json()["can_edit"]
    library.actor = library.author
    library.revoke_before_grant = True
    base = f"/v1/plans/{pid}" if target == "plan" else f"/v1/library/designs/{DOC}"
    invited = app.put(f"{base}/reviewers/reviewer", json={"required": False})
    assert invited.status_code == 200, invited.json()
    assert library.revoked
    library.actor = "viewer"
    read = app.get(f"/v1/shared-designs/{DOC}")
    assert read.status_code == 200 and not read.json()["can_edit"]
    library.actor = "reviewer"
    reviewer = app.get(f"/v1/shared-designs/{DOC}").json()
    assert reviewer["can_comment"] and reviewer["can_sign_off"]
    assert not reviewer["gate"]["reviewers"][0]["required"]
