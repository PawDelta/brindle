"""Enterprise review policies on a shared plan (brindle.pro.review_policies),
against a fake of "Review policies" in pawdelta-web's docs/shared-designs-api.md."""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from brindle import plans
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import review_policies as rp
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_plans import AUTH, TOKEN, session  # noqa: F401 - fixture
from test_shared_plan_docs import DESIGN, TEAM, Library, parts  # noqa: F401 - fixture

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
        self.rules, self.read_status, self.reviewers_status, self.override_status = [], 200, 200, 200
        self.garbled = {}                   # fields the server answers with, whatever the rules say
        plain_set = self.set_reviewers

        def read(form, headers):
            if self.read_status != 200:
                return self.read_status, {"error": "key_unavailable", "error_description": "rules are sealed"}
            return 200, self.view()

        def put_reviewers(form, headers):
            if self.reviewers_status != 200:
                return self.reviewers_status, {"error": "policy_denied", "org_id": ORG, "policy": "security"}
            return plain_set(form, headers)

        remote.routes.update({f"GET {DESIGN}": read, f"PUT {DESIGN}/reviewers": put_reviewers,
                              f"POST {DESIGN}/policy-overrides": self.override})

    def view(self):
        return {**super().view(), "policy_reviewers": list(self.rules),
                "pending_policies": [{"org_id": r["org_id"], "policy": r["policy"]} for r in self.rules
                                     if r["status"] == "waiting"], **self.garbled}

    def override(self, form, headers):
        if self.override_status != 200:
            return self.override_status, {"error": "forbidden"}
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
    pytest.param({"policy_reviewers": None}, id="no-rules-list"),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"], approvals="1")]}, id="approvals-unreadable"),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"])], "pending_policies": "security"}, id="pending-unreadable"),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"])], "pending_policies": [{"org_id": ORG, "policy": "security"}]},
                 id="met-but-still-pending"),
    pytest.param({"policy_reviewers": [rule(status="met", met_by=["u_s"], approvals=2)]}, id="met-short-of-approvals"),
    pytest.param({"policy_reviewers": [rule(status="waived")]}, id="waived-with-no-waiver"),
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


@pytest.mark.parametrize("refuse", [
    pytest.param(lambda lib, db, pid, net: (setattr(lib, "read_status", 503), sd.sync(db, pid, **net)), id="rules-unreadable"),
    pytest.param(lambda lib, db, pid, net: (setattr(lib, "reviewers_status", 403), sd.set_reviewers(db, pid, [ME], **net)), id="rule-denies"),
])
def test_a_refusal_closes_a_ready_gate_until_the_library_answers_whole(db, shared, refuse):
    pid, lib, net = shared
    assert gate(db, pid)["state"] == "ready"
    with pytest.raises(RuntimeError):
        refuse(lib, db, pid, net)
    now = gate(db, pid)
    assert (now["state"], now["signed"]) == ("waiting", 0)

    lib.read_status, lib.reviewers_status = 200, 200
    sd.sync(db, pid, **net)
    assert gate(db, pid)["state"] == "ready"


def test_another_failure_says_nothing_about_the_rules(db, shared, backend):  # noqa: F811 - fixture
    pid, lib, net = shared
    backend.routes[f"GET {DESIGN}"] = lambda f, h: (500, {"error": "boom"})
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert rp.REFUSED not in sd.link(db, pid)["review"] and gate(db, pid)["state"] == "ready"


@pytest.mark.parametrize("role, offered", [("owner", ORG), ("member", None)])
def test_only_an_org_admin_is_offered_an_override(monkeypatch, role, offered):
    from brindle.daemon import routes_actions as actions

    monkeypatch.setattr(actions, "_docs_entitlement", lambda repo: SimpleNamespace(org_id=ORG, sub="me", role=role))
    monkeypatch.setattr(actions, "docs_api", lambda method, path: {"members": []})
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        assert app.get("/v1/library/people").json()["admin_org"] == offered


def test_an_admin_waives_a_rule_once_and_the_doc_shows_it_through_the_daemon(db, shared, backend):  # noqa: F811 - fixture
    pid, lib, _ = shared
    lib.rules = [rule()]
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        got = app.post(f"/v1/plans/{pid}/policy-override",
                       json={"rev": 1, "org_id": ORG, "policy": "security", "action": "waive", "reason": "Security is at an offsite."})
    assert got.status_code == 200, got.json()
    policy = got.json()["gate"]["policies"][0]
    assert (policy["status"], policy["waiver"]["by"]) == ("waived", "user_1")
    assert got.json()["gate"]["state"] == "ready"
    sent = [form for key, form, _ in backend.calls if key == f"POST {DESIGN}/policy-overrides"]
    assert [(s["rev"], s["action"]) for s in sent] == [(1, "waive")]


def test_a_stand_in_is_named_for_the_rule_and_clears_nothing_until_they_sign(db, shared):
    pid, lib, net = shared
    lib.rules = [rule(needs={"kind": "person", "id": "alex", "name": "alex"})]
    rp.override(db, pid, 1, ORG, "security", "stand_in", "Alex is away.", stand_in="sam", until=2_000, now=1_000, **net)
    now = gate(db, pid)
    assert now["state"] == "waiting" and now["policies"][0]["stand_ins"][0]["stand_in"] == "sam"


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

    lib.rules = []
    plans.decide(db, pid, 1, True, person=True)
    with pytest.raises(ServiceError) as e:
        rp.override(db, pid, 1, ORG, "security", "waive", "r", **net)
    assert e.value.code == "review_closed"
