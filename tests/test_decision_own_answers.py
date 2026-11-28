"""Own answers on a planning doc's decision cards (brindle.plan_answers): a
row someone typed under the options, kept beside the revision's immutable
decisions, picked like an option, carried by the verdict, and shared through
the Library's /design/answers (a fake of the contract in pawdelta-web's
docs/shared-designs-api.md)."""

import copy

import pytest
from starlette.testclient import TestClient

from brindle import plan_docs, plans, visual_offers
from brindle.daemon.app import build_app
from brindle.pro import review_policies
from brindle.pro import shared_docs as sd
from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401
from test_plans import AUTH, DECISIONS, TOKEN, messages, session, work  # noqa: F401 - fixture
from test_shared_plan_docs import DESIGN, TEAM, Library, parts  # noqa: F401 - fixture

TEXT = "only on federal holidays"


class AnsweringLibrary:
    """The fake Library with own answers, as the contract has them: client-chosen
    ids, the author from the signed-in account, and per-answer PUT/DELETE."""

    def __init__(self, lib: Library, remote):
        self.lib, self.remote, self.answers = lib, remote, {}      # {decision: [answer]}
        view = lib.view
        lib.view = lambda: self._with_answers(view())
        remote.routes[f"POST {DESIGN}/answers"] = self.add

    def _with_answers(self, out):
        for d in out["decisions"]:
            d["answers"] = copy.deepcopy(self.answers.get(d["id"], []))
        return out

    def put(self, decision, author, answer_id, text):
        self.answers.setdefault(decision, []).append({"id": answer_id, "text": text, "author": author, "at": 5.0})
        self.remote.routes[f"PUT {DESIGN}/answers/{answer_id}"] = lambda f, h: self.edit(answer_id, f)
        self.remote.routes[f"DELETE {DESIGN}/answers/{answer_id}?rev={self.lib.rev}"] = lambda f, h: self.drop(answer_id)

    def _picked(self, answer_id):
        return any(sub != "user_1" for d in self.lib.votes.values() for sub in d.get(answer_id, []))

    def add(self, form, headers):
        self.put(form["decision"], "user_1", form["id"], form["text"])
        return self.lib.answer()

    def edit(self, answer_id, form):
        if self._picked(answer_id):
            return 409, {"error": "picked"}
        next(a for d in self.answers.values() for a in d if a["id"] == answer_id)["text"] = form["text"]
        return self.lib.answer()

    def drop(self, answer_id):
        if self._picked(answer_id):
            return 409, {"error": "picked"}
        for d in self.answers.values():
            d[:] = [a for a in d if a["id"] != answer_id]
        return self.lib.answer()


@pytest.fixture
def shared(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    return pid, AnsweringLibrary(lib, backend), net


def _decision(db, pid):
    return plan_docs.fields(db, plans.get(db, pid))["decisions"][0]


def _write(pid, tail, body):
    """One write through the app's route; the reply's status and body."""
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        r = client.post(f"/v1/plans/{pid}/{tail}", json=body)
    return r.status_code, r.json()


def _add(pid, text=TEXT):
    status, got = _write(pid, "answers", {"rev": 1, "decision": "d1", "text": text})
    assert status == 200, got
    return got["decisions"][0]["confirmed"]


def _plan(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    return plans.propose(db, boss, "# Cloud workers\n\n## Summary\n\nWorkers.", DECISIONS)["id"]


@pytest.mark.variant("an own answer must persist across a reopen, not just in memory")
def test_an_own_answer_survives_a_reopen(db, session):
    pid = _plan(db, session)
    _add(pid, f"  {TEXT} ")
    with TestClient(build_app(TOKEN), headers=AUTH) as client:       # the reader closed and opened again
        d = client.get(f"/v1/plans/{pid}").json()["decisions"][0]
    assert [(a["text"], a["author"], a["locked"]) for a in d["answers"]] == [(TEXT, "person", False)]
    assert d["confirmed"] == d["answers"][0]["id"]                    # adding it is the person's pick: decided


def test_the_verdict_carries_the_answers_text(db, session):
    pid = _plan(db, session)
    _add(pid)
    plans.decide(db, pid, 1, True, person=True)
    assert f"they chose their own answer, \"{TEXT}\"" in messages(db)[-1]


def test_a_teammates_answer_syncs_and_can_be_picked(db, shared, backend):
    pid, lib, net = shared
    lib.put("d1", "u_c", "own-david1", "only when the repo is idle")
    _add(pid)
    sd.sync(db, pid, **net)

    mine = next(a for a in _decision(db, pid)["answers"] if a["author"] == "person")
    assert [a["text"] for a in lib.answers["d1"]] == ["only when the repo is idle", TEXT]   # the person's went up
    assert [a["id"] for a in lib.answers["d1"]] == ["own-david1", mine["id"]]
    theirs = next(a for a in _decision(db, pid)["answers"] if a["author"] == "teammate:u_c")
    assert theirs["text"] == "only when the repo is idle"

    plan_docs.confirm(db, pid, 1, "d1", "own-david1")                 # picked like any option
    sd.sync(db, pid, **net)
    assert [f for k, f, _ in backend.calls if k == f"POST {DESIGN}/votes"][-1]["option"] == "own-david1"
    assert _decision(db, pid)["confirmed"] == "own-david1"


def test_an_older_library_keeps_own_answers_here_without_barring_the_review(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    backend.routes[f"POST {DESIGN}/answers"] = [(404, {"error": "not_found"})]      # no /answers there yet
    _add(pid)
    sd.sync(db, pid, **net)
    assert db.conn.execute("SELECT access_refused FROM plan_shares WHERE plan_id=?", (pid,)).fetchone()[0] is None
    assert [a["text"] for a in _decision(db, pid)["answers"]] == [TEXT]


@pytest.mark.parametrize("change", [
    pytest.param({"rev": 1, "text": "something else"}, id="edit"),
    pytest.param({"rev": 1, "remove": True}, id="remove"),
])
@pytest.mark.variant("each edit path (edit/remove) must respect another person's pick")
def test_an_answer_someone_else_picked_cant_be_changed(db, shared, change):
    pid, lib, net = shared
    answer = _add(pid)
    sd.sync(db, pid, **net)
    lib.lib.votes = {"d1": {answer: ["user_1", "u_c"]}}       # a teammate picks it before this machine hears of it
    assert _write(pid, f"answers/{answer}", change)[0] == 200
    sd.sync(db, pid, **net)

    assert [(a["text"], a["locked"]) for a in _decision(db, pid)["answers"]] == [(TEXT, True)]   # the Library's words stand
    assert [a["text"] for a in lib.answers["d1"]] == [TEXT]
    for again in ({"rev": 1, "remove": True}, {"rev": 1, "text": "something else"}):
        assert _write(pid, f"answers/{answer}", again)[0] == 409


def test_an_org_rule_refusing_an_answer_bars_the_review(db, shared, backend):
    pid, lib, net = shared
    answer = _add(pid)
    sd.sync(db, pid, **net)
    backend.routes[f"PUT {DESIGN}/answers/{answer}"] = [(403, {"error": "policy_denied"})]
    _write(pid, f"answers/{answer}", {"rev": 1, "text": "something else"})
    sd.sync(db, pid, **net)

    assert sd.link(db, pid)["review"][review_policies.REFUSED]["code"] == "policy_denied"
    assert plans.gate(db, plans.get(db, pid))["state"] != "ready"
    assert [a["text"] for a in _decision(db, pid)["answers"]] == ["something else"]   # kept for the next sync


def test_an_answer_changed_or_removed_elsewhere_comes_here(db, shared):
    pid, lib, net = shared
    answer = _add(pid)
    sd.sync(db, pid, **net)
    lib.answers["d1"][0]["text"] = "reworded on the laptop"           # its author, from another machine
    sd.sync(db, pid, **net)
    assert [a["text"] for a in _decision(db, pid)["answers"]] == ["reworded on the laptop"]
    assert [a["text"] for a in lib.answers["d1"]] == ["reworded on the laptop"]

    lib.drop(answer)
    sd.sync(db, pid, **net)
    assert (_decision(db, pid)["answers"], lib.answers["d1"]) == ([], [])
