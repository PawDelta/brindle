"""The Share panel talks to the Library one person at a time."""

from starlette.testclient import TestClient

from brindle import plans, visual_offers
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, DECISIONS, TOKEN, session  # noqa: F401
from test_pro_remote_learning import login_as


class Library:
    def __init__(self, remote):
        self.reviewers = {}
        self.permissions = {}
        self.link = None
        self.fail = "b"
        self.people = [{"id": sub, "display_name": sub.upper(), "email": f"{sub}@example.com",
                        "teams": [{"id": "core", "name": "Core"}], "role": "member",
                        "on_plan": False, "on_plan_role": None} for sub in ("a", "b")]
        path = "/analysis-docs/doc_1/design"
        remote.routes[f"GET {path}"] = lambda f, h: (200, self.view())
        remote.routes[f"GET {path}/people"] = lambda f, h: (200, {"people": self.people})
        remote.routes[f"GET {path}/people?q=a&team=core&role=member&on_plan=false"] = lambda f, h: (200, {"people": self.people[:1]})
        for sub in ("a", "b"):
            remote.routes[f"PUT {path}/permissions/{sub}"] = lambda f, h, sub=sub: self.put(sub, f)
            remote.routes[f"DELETE {path}/reviewers/{sub}"] = lambda f, h, sub=sub: self.remove(sub)
        remote.routes[f"GET {path}/share-link"] = lambda f, h: (200, {"link": self.link})
        remote.routes[f"POST {path}/share-link"] = self.create_link
        remote.routes[f"PUT {path}/share-link"] = self.update_link

    def view(self):
        return {"rev": 1, "title": "Design", "lede": "", "ask": None,
                "sections": [{"id": "summary", "title": "Summary", "kind": "markdown", "markdown": "A design."}], "decisions": [], "split": [],
                "reviewers": [{"sub": sub, "required": required} for sub, required in self.reviewers.items()],
                "permissions": self.permissions,
                "sign_offs": [], "pending_reviewers": list(self.reviewers), "policy_reviewers": [], "ready": False}

    def put(self, sub, form):
        if self.fail == sub:
            return 409, {"error": "conflict"}
        self.reviewers[sub] = form["sign_off"] == "required"
        self.permissions[sub] = {"can_edit": False, **self.permissions.get(sub, {}), "can_comment": True, "can_suggest": True}
        return 200, self.view()

    def remove(self, sub):
        self.reviewers.pop(sub, None)
        return 200, self.view()

    def create_link(self, form, headers):
        self.link = {"url": "https://plans.pawdelta.com/b/dsl_one", "access": "anyone", "created_at": 1,
                     "permission": form.get("permission", "view")}
        return 200, {"link": self.link}

    def update_link(self, form, headers):
        self.link.update(form)
        return 200, {"link": self.link}


def test_share_panel_proxies_preserve_other_people_on_failure_and_update_link_access(
        db, session, tmp_path, backend, monkeypatch, pro_env):
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=("analysis_docs", "analysis_docs_team"))
    monkeypatch.setattr(ad, "_session", lambda *a: (auth.Client(None, backend), store))
    library = Library(backend)
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Design", DECISIONS)["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner, local_rev, remote_rev) VALUES (?, 'doc_1', 'user_1', 1, 1)", (pid,))
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        for base in ("/v1/library/designs/doc_1", f"/v1/plans/{pid}"):
            got = app.get(f"{base}/people?q=a&team=core&role=member&on_plan=false")
            assert got.status_code == 200 and got.json()["people"] == library.people[:1]
            added = app.put(f"{base}/reviewers/a", json={"required": True})
            assert added.status_code == 200, added.json()
            assert app.put(f"{base}/reviewers/a", json={"required": None}).status_code == 400
            assert library.reviewers == {"a": True}
            failed = app.put(f"{base}/reviewers/b", json={"required": False})
            assert failed.status_code == 409
            assert library.reviewers == {"a": True}
            library.fail = None
            assert app.put(f"{base}/reviewers/b", json={"required": False}).status_code == 200
            assert library.reviewers == {"a": True, "b": False}
            assert app.delete(f"{base}/reviewers/a").status_code == 200
            assert library.reviewers == {"b": False}
            assert app.delete(f"{base}/reviewers/b").status_code == 200
            library.fail = "b"
            link = app.post(f"{base}/share-link", json={"permission": "view"}).json()["link"]
            for permission in ("edit", "view"):
                changed = app.put(f"{base}/share-link", json={"permission": permission})
                assert changed.status_code == 200
                assert changed.json()["link"]["permission"] == permission
                assert changed.json()["link"]["url"] == link["url"]
