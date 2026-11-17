"""Enterprise review policies on a shared plan (brindle.pro.review_policies),
against a fake of "Review policies" in pawdelta-web's docs/shared-designs-api.md."""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from brindle import plan_docs, plans
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import review_policies as rp
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_plans import AUTH, TOKEN, session  # noqa: F401 - fixture
from test_shared_plan_docs import BASE, DESIGN, TEAM, Library, parts  # noqa: F401 - fixture

ME = {"sub": "user_1", "required": True}
ORG = "org_1"


def rule(policy="security", **more):
    return {"org_id": ORG, "policy": policy, "name": policy.title(), "note": None,
            "when": {"kind": "paths", "paths": ["billing/**"], "label": None}, "matched": ["billing/meter.py"],
            "needs": {"kind": "team", "id": "t_9", "name": "Security"}, "approvals": 1, "eligible": 6, "on_list": [],
            "met_by": [], "status": "waiting", "waiver": None, "stand_ins": [], "set_by": "dana", "set_at": 7, **more}


class Governed(Library):
    """The Library with an org's rules applied, as its view derives them."""

    def __init__(self, remote):
        super().__init__(remote)
        self.rules, self.override_status = [], 200
        self.garbled = {}                   # fields the server answers with, whatever the rules say
        self.refusing = {}                  # route -> (status, error) while an org rule refuses it

        def guarded(route):
            handler = remote.routes[route]

            def call(form, headers):
                if route in self.refusing:
                    status, error = self.refusing[route]
                    return status, {"error": error, "org_id": ORG, "policy": "security"}
                return handler(form, headers)
            return call

        remote.routes.update({r: guarded(r) for r in (f"GET {DESIGN}", f"PUT {DESIGN}/permissions", f"PUT {DESIGN}/reviewers",
                                                      f"POST {BASE}/share", f"PUT {DESIGN}/folder")})
        remote.routes[f"POST {DESIGN}/policy-overrides"] = self.override
        remote.routes[f"POST {DESIGN}/policy-asks"] = self.ask
        # The org's list, as the Library keeps it: a save at an old version is a 409.
        self.version, self.saved, self.org_rules = 2, [], [{"id": "security"}]
        org = f"/orgs/{ORG}/analysis-docs/review-policies"
        remote.routes.update({
            f"GET {org}": lambda f, h: (200, {"org_id": ORG, "version": self.version, "policies": self.org_rules}),
            f"GET {org}/directory": lambda f, h: (200, {"teams": [{"id": "t_9"}], "roles": []}),
            f"GET {org}/in-review": lambda f, h: (200, {"truncated": False, "policies": [{"policy": "security", "count": 3}]}),
            f"PUT {org}": self.save,
            f"POST {org}/draft-impact": lambda f, h: (200, {"org_id": ORG, "truncated": False, "count": len(f["policies"]),
                                                            "designs": [], "reopens_gates": False, "reopened": 0}),
            f"POST {org}/preview": lambda f, h: (200, {"policy_reviewers": [rule(), {"policy": "broken"}]}),
        })

    def save(self, form, headers):
        if form["version"] != self.version:
            return 409, {"error": "conflict"}
        self.version, self.saved = self.version + 1, form["policies"]
        return 200, {"version": self.version, "policies": form["policies"]}

    def ask(self, form, headers):
        if form["rev"] != self.rev:
            return 409, {"error": "stale", "rev": self.rev}
        self.rules = [{**r, "asked_at": 9, "opened_by": [{"sub": "u_s", "at": 8}]} if r["policy"] == form["policy"] else r
                      for r in self.rules]
        return 200, {**self.view(), "notified": ["u_s"]}

    def applied(self, r):
        """A stand-in's sign-off on this revision, made in time, meets their rule."""
        met = [s["sub"] for s in self.sign_offs if s["rev"] == self.rev
               and any(o["stand_in"] == s["sub"] and s["at"] <= o["until"] for o in r["stand_ins"])]
        return {**r, "status": "met", "met_by": met} if met and r["status"] == "waiting" else r

    def view(self):
        rules = [self.applied(r) for r in self.rules]
        return {**super().view(), "policy_reviewers": rules,
                "pending_policies": [{"org_id": r["org_id"], "policy": r["policy"]} for r in rules
                                     if r["status"] == "waiting"], **self.garbled}

    def override(self, form, headers):
        if self.override_status != 200:
            return self.override_status, {"error": "forbidden"}
        if form["rev"] != self.rev:
            return 409, {"error": "stale", "rev": self.rev}
        for i, r in enumerate(self.rules):
            if r["policy"] == form["policy"] and form["action"] == "waive":
                self.rules[i] = {**r, "status": "waived", "waiver": {"by": "user_1", "at": 9, "reason": form["reason"]}}
            elif r["policy"] == form["policy"]:
                self.rules[i] = {**r, "stand_ins": [{"stand_in": form["stand_in"], "until": form["until"], "by": "user_1", "at": 9}]}
        return 200, self.view()


@pytest.fixture
def shared(db, parts, backend, monkeypatch):  # noqa: F811 - fixture
    """A plan shared with one required reviewer who has signed: ready, but for its org's rules."""
    pid, _, net = parts
    lib = Governed(backend)
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    sd.share(db, pid, TEAM, reviewers=[ME], **net)
    sd.sign_off(db, pid, 1, **net)
    return pid, lib, net


def gate(db, pid):
    return plans.to_dto(db, plans.get(db, pid))["gate"]


@pytest.mark.variant("the whole journey a rule puts a ready plan through: held, explained, then released when one sign-off meets every rule")
def test_a_rule_holds_the_build_and_shows_why_until_one_sign_off_meets_all_of_them(db, shared):
    pid, lib, net = shared
    assert gate(db, pid)["state"] == "ready"
    lib.rules = [rule(), rule("staff", when={"kind": "any"}, matched=[])]
    sd.sync(db, pid, **net)
    now = gate(db, pid)
    assert (now["state"], now["signed"], now["required"]) == ("waiting", 1, 3)
    security = now["policies"][0]
    assert (security["needs_name"], security["eligible"], security["matched"]) == ("Security", 6, ["billing/meter.py"])
    with pytest.raises(ServiceError) as e:
        plans.decide(db, pid, 1, True, person=True)
    assert e.value.code == "gate_closed"

    lib.rules = [{**r, "status": "met", "met_by": ["u_s"]} for r in lib.rules]
    sd.sync(db, pid, **net)
    assert gate(db, pid)["state"] == "ready"


@pytest.mark.parametrize("review", [
    pytest.param({"policy_reviewers": None}, id="no-rules-list", marks=pytest.mark.variant(
        "a Library reply with no rules list at all must hold the gate, not read as no rules apply")),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"], approvals="1")]}, id="approvals-unreadable"),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"])], "pending_policies": "security"}, id="pending-unreadable"),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"])], "pending_policies": [{"org_id": ORG, "policy": "security"}]},
                 id="met-but-still-pending", marks=pytest.mark.variant("the Library contradicting itself must not open the gate")),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"], approvals=2)]}, id="met-short-of-approvals",
                 marks=pytest.mark.variant("a rule needing two approvals with one signer must not read as met")),
    pytest.param({"policy_reviewers": [rule(status="waived")]}, id="waived-with-no-waiver",
                 marks=pytest.mark.variant("a waiver nobody can be shown for must not clear the rule")),
    pytest.param({"policy_reviewers": [rule(status="denied")]}, id="status-unknown", marks=pytest.mark.variant(
        "a status this brindle doesn't know, a denial above all, must hold the gate rather than read as met")),
])
def test_a_rule_that_cant_be_believed_closes_the_gate(db, shared, review):
    pid, lib, net = shared
    lib.garbled = review
    sd.sync(db, pid, **net)
    assert gate(db, pid)["state"] == "waiting"
    with pytest.raises(ServiceError) as e:
        plans.decide(db, pid, 1, True, person=True)
    assert e.value.code == "gate_closed" and plans.get(db, pid)["state"] == "open"


UNREADABLE = (503, "key_unavailable")


@pytest.mark.parametrize("route, answer, act", [
    pytest.param(f"GET {DESIGN}", UNREADABLE, lambda db, pid, net: sd.sync(db, pid, **net), id="rules-unreadable",
                 marks=pytest.mark.variant("the rules read failing during a plain sync must close a gate that was ready")),
    pytest.param(f"GET {DESIGN}", UNREADABLE, lambda db, pid, net: sd.set_reviewers(db, pid, [ME], **net), id="unreadable-before-a-reviewer-edit"),
    pytest.param(f"PUT {DESIGN}/permissions", UNREADABLE, lambda db, pid, net: sd.set_reviewers(db, pid, [ME, {"sub": "u_new", "required": True}], **net),
                 id="unreadable-while-granting", marks=pytest.mark.variant(
                     "the rules turning unreadable while access is being granted must close the gate, not leave it ready")),
    pytest.param(f"PUT {DESIGN}/reviewers", (403, "policy_denied"), lambda db, pid, net: sd.set_reviewers(db, pid, [ME], **net), id="rule-denies"),
    pytest.param(f"POST {BASE}/share", UNREADABLE, lambda db, pid, net: sd.share(db, pid, TEAM, **net), id="unreadable-while-sharing",
                 marks=pytest.mark.variant("the rules turning unreadable while sharing a plan must close the gate, not leave it ready")),
    pytest.param(f"PUT {DESIGN}/folder", UNREADABLE, lambda db, pid, net: sd.share(db, pid, TEAM, folder={"scope": TEAM, "id": "f_1"}, **net),
                 id="unreadable-while-filing"),
])
def test_a_refusal_closes_a_ready_gate_until_the_library_answers_whole(db, shared, route, answer, act):
    pid, lib, net = shared
    assert gate(db, pid)["state"] == "ready"
    lib.refusing[route] = answer
    with pytest.raises(RuntimeError):
        act(db, pid, net)
    now = gate(db, pid)
    assert (now["state"], now["signed"]) == ("waiting", 0)

    lib.refusing.clear()
    sd.sync(db, pid, **net)
    assert gate(db, pid)["state"] == "ready"


@pytest.mark.parametrize("role, offered", [
    pytest.param("owner", ORG, marks=pytest.mark.variant("an org owner is offered the override for their own org")),
    pytest.param("member", None, marks=pytest.mark.variant("a member must not be offered what only an admin can do")),
])
def test_only_an_org_admin_is_offered_an_override(monkeypatch, role, offered):
    from brindle.daemon import routes_actions as actions

    monkeypatch.setattr(actions, "_docs_entitlement", lambda repo: SimpleNamespace(org_id=ORG, sub="u_me", role=role))
    monkeypatch.setattr(actions, "docs_api", lambda method, path: {"members": []})
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        people = app.get("/v1/library/people").json()
    assert (people["admin_org"], people["me"]) == (offered, "u_me")


def test_an_admin_waives_the_revision_on_screen_through_the_daemon_under_the_librarys_number(db, shared):
    """Two local edits reach the Library as one revision, so the numbers differ, and the fake refuses the wrong one."""
    pid, lib, _ = shared
    lib.rules = [rule()]
    plan_docs.edit(db, pid, 1, "summary", 0, "Workers", "People")
    plan_docs.edit(db, pid, 2, "summary", 0, "People", "Agents")
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        got = app.post(f"/v1/plans/{pid}/policy-override",
                       json={"rev": 3, "org_id": ORG, "policy": "security", "action": "waive", "reason": "Security is at an offsite."})
        assert got.status_code == 200, got.json()
        policy = got.json()["gate"]["policies"][0]
        assert (policy["status"], policy["waiver"]["by"]) == ("waived", "user_1")
        assert app.post(f"/v1/plans/{pid}/sign-off", json={"rev": 3}).json()["gate"]["state"] == "ready"


def test_an_admin_names_themself_and_the_rule_waits_until_they_sign(db, shared):
    pid, lib, net = shared
    lib.sign_offs.clear()
    lib.rules = [rule(needs={"kind": "person", "id": "alex", "name": "alex"})]
    rp.override(db, pid, 1, ORG, "security", "stand_in", "Alex is away.", stand_in="user_1", until=2_000, now=1_000, **net)
    now = gate(db, pid)
    assert (now["state"], now["policies"][0]["status"]) == ("waiting", "waiting")
    assert now["policies"][0]["stand_ins"][0]["stand_in"] == "user_1"

    sd.sign_off(db, pid, 1, **net)
    now = gate(db, pid)
    assert (now["state"], now["policies"][0]["met_by"]) == ("ready", ["user_1"])


@pytest.mark.parametrize("args", [
    pytest.param(dict(action="skip", reason="r"), id="unknown-action"),
    pytest.param(dict(action="waive", reason="  "), id="no-reason"),
    pytest.param(dict(action="stand_in", reason="r", until=2_000), id="stand-in-names-nobody"),
    pytest.param(dict(action="stand_in", reason="r", stand_in="sam", until=500), id="stand-in-already-over"),
    pytest.param(dict(action="waive", reason="r", org_id=""), id="no-rule-named"),
])
def test_a_malformed_override_is_refused_before_it_is_sent(db, shared, backend, args):  # noqa: F811 - fixture
    pid, _, net = shared
    org = args.pop("org_id", ORG)
    action, reason = args.pop("action"), args.pop("reason")
    with pytest.raises(ServiceError) as e:
        rp.override(db, pid, 1, org, "security", action, reason, now=1_000, **args, **net)
    assert e.value.code == "invalid"
    assert not [k for k, _, _ in backend.calls if k.endswith("/policy-overrides")]


def test_a_refused_override_changes_nothing_and_a_closed_review_takes_none(db, shared):
    pid, lib, net = shared
    lib.rules = [rule()]
    lib.override_status = 403
    with pytest.raises(RuntimeError):
        rp.override(db, pid, 1, ORG, "security", "waive", "r", **net)
    assert gate(db, pid)["state"] == "waiting" and gate(db, pid)["policies"][0]["waiver"] is None
    assert rp.REFUSED not in sd.link(db, pid)["review"]             # an admin check says nothing about the rules

    lib.rules = []
    plans.decide(db, pid, 1, True, person=True)
    with pytest.raises(ServiceError) as e:
        rp.override(db, pid, 1, ORG, "security", "waive", "r", **net)
    assert e.value.code == "review_closed"


def test_an_admin_asks_a_waiting_rule_again_and_the_gate_shows_when_and_who_opened_it(db, shared):
    pid, lib, _ = shared
    lib.rules = [rule()]
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        got = app.post(f"/v1/plans/{pid}/policy-ask", json={"rev": 1, "org_id": ORG, "policy": "security"})
        assert got.status_code == 200, got.json()
        policy = got.json()["gate"]["policies"][0]
        assert (policy["asked_at"], policy["opened_by"]) == (9, [{"sub": "u_s", "at": 8}])
        assert app.post(f"/v1/plans/{pid}/policy-ask", json={"rev": 1, "org_id": "", "policy": "security"}).status_code == 400


def test_the_settings_routes_read_and_save_the_orgs_rules_and_preview_a_plans(db, shared, monkeypatch):
    """Through the real Library calls, for a login with no owner/admin role (a custom role with
    the control permission): the Library, not the daemon, decides who may."""
    pid, lib, net = shared
    entitled = ad.require_entitlement
    monkeypatch.setattr(ad, "require_entitlement", lambda store=None: entitled(store or net["store"]))
    monkeypatch.setattr(plan_docs, "revision", lambda db, plan_id: {
        "split": [{"files": ["billing/a.py", "billing/a.py"]}], "decisions": [{"options": [{"touches": ["auth/b.py"]}]}]})
    rules = [{"id": "security", "eligible": 6, "set_by": "dana", "needs": {"kind": "team", "id": "t_9", "name": "Security"}}]
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        read = app.get("/v1/review-policies").json()
        assert "error" not in read, read
        assert (read["version"], read["directory"]["teams"][0]["id"], read["in_review"]["policies"][0]["count"]) == (2, "t_9", 3)
        assert app.put("/v1/review-policies", json={"org_id": ORG, "version": 2, "policies": rules}).status_code == 200
        assert lib.saved == [{"id": "security", "needs": {"kind": "team", "id": "t_9"}}]
        # Another admin saved first: the 409 reaches the app as a 409, and nothing is written.
        stale = app.put("/v1/review-policies", json={"org_id": ORG, "version": 2, "policies": []})
        assert (stale.status_code, lib.version) == (409, 3)
        # The active org changed since the form loaded: refused before anything is sent.
        other = app.put("/v1/review-policies", json={"org_id": "org_2", "version": 3, "policies": []})
        assert (other.status_code, other.json()["error"]["code"], lib.version) == (409, "org_changed", 3)
        assert app.put("/v1/review-policies", json={"org_id": ORG, "version": "2", "policies": []}).status_code == 400
        impact = app.post("/v1/review-policies/draft-impact", json={"org_id": ORG, "policies": rules}).json()
        assert impact["count"] == 1
        preview = app.post(f"/v1/plans/{pid}/policy-preview", json={}).json()["policies"]
    assert [p["policy"] for p in preview] == ["security"] and preview[0]["needs_name"] == "Security"
