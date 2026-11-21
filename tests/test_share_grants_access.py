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
        self.scopes = [{"kind": "user", "id": self.author}]
        self.members = {("group", "t_1"): {"reviewer", "viewer"}, ("org", "org_1"): {"reviewer", "viewer"}}
        remote.routes.update({
            f"GET {BASE}": lambda f, h: self.metadata(),
            f"GET {DESIGN}": lambda f, h: self.read(),
            f"PUT {DESIGN}/reviewers/reviewer": self.put_reviewer,
        })

    def readable(self):
        return self.actor == self.author or any(
            self.actor in self.members.get((s["kind"], s["id"]), set()) for s in self.scopes)

    def allowed(self, flag):
        return self.readable() and (self.actor == self.author or self.permissions.get(self.actor, {}).get("can_edit")
                                   or self.permissions.get(self.actor, {}).get(flag, False))

    def read(self):
        return (200, self.view()) if self.readable() else (404, {"error": "not_found"})

    def metadata(self):
        return (200, {"id": DOC, "title": self.title, "scope": self.scopes[0], "scopes": list(self.scopes)})

    def share(self, form, headers):
        if form["scope"] not in self.scopes:
            self.scopes.append(form["scope"])
        return 200, {"id": DOC, "scopes": list(self.scopes)}

    def put_reviewer(self, form, headers):
        self.reviewers = [{"sub": "reviewer", "required": form["required"]}]
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
