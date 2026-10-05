"""The coarse savings fields a terminal record carries to the server, and
nothing else new: no repo names, paths or task text."""
import json
import time
from types import SimpleNamespace

import pytest

from copse.learning import Outcome, TaskInfo
from copse.pro import learning as cloud
from copse.pro.learning import CloudLearner, record_payload
from copse.pro.orgkey import OrgKey

REPO = "/work/secret-client-project"
TEXT = "Fix the crash in src/zebra_module.py on branch feat/zebra-hotfix"
AGENT = "worker-zebra-7"
COSTS = {"developer": 2, "cheap": 0, "fancy": 20}
FIELDS = {"baseline_profile", "learned", "cost_rank", "baseline_cost_rank"}
FORBIDDEN = ("zebra", "src/", "secret-client-project", "/work", "feat/", "Fix the crash")


def task():
    return TaskInfo(repo_root=REPO, task=TEXT, files=("src/zebra_module.py",), weight="medium",
                    agent_id=AGENT, profile="cheap", provider="claude", model="m",
                    started_at=time.time() - 60)


def key():
    return OrgKey("org1", "k1", b"k" * 32)


def decision(baseline="developer", learned=True):
    return SimpleNamespace(baseline_profile=baseline, learned=learned)


def payload(event, dec=None, cost=COSTS.get):
    return record_payload(key(), "identity", task(), Outcome(event=event, checks_passed=True),
                          decision=dec, cost=cost)


@pytest.mark.parametrize("event", ["merged", "removed_unmerged"])
def test_terminal_events_carry_the_four_fields(event):
    body = payload(event, decision())
    assert body["baseline_profile"] == "developer" and body["learned"] is True
    assert body["cost_rank"] == 0 and body["baseline_cost_rank"] == 2


def test_ranks_are_clamped_to_0_16():
    body = payload("merged", decision("fancy"))
    assert body["baseline_cost_rank"] == 16


@pytest.mark.parametrize("event", ["review", "escalated"])
def test_other_events_never_carry_them(event):
    assert not FIELDS & set(payload(event, decision()))


def test_no_decision_omits_all_four():
    assert not FIELDS & set(payload("merged", None))


def test_a_cost_failure_omits_them_but_keeps_the_record():
    def boom(name):
        raise RuntimeError

    body = payload("merged", decision(), cost=boom)
    assert body is not None and not FIELDS & set(body)


def test_a_baseline_that_is_not_a_plain_name_is_omitted():
    assert not FIELDS & set(payload("merged", decision("../etc/passwd")))


def test_a_payload_never_contains_repo_names_or_paths():
    text = json.dumps(payload("merged", decision()))
    for bad in FORBIDDEN + (AGENT,):
        assert bad not in text


def test_every_key_is_allowed():
    assert set(payload("merged", decision())) <= cloud.RECORD_KEYS


def test_the_learner_looks_up_the_decision_by_agent_and_sends_it():
    sent, asked = [], []

    def lookup(agent_id):
        asked.append(agent_id)
        return decision()

    learner = CloudLearner(REPO, org=lambda: "org1", cost=COSTS.get, start_thread=False,
                           decision_for=lookup)
    learner.identity = lambda: "identity"
    learner.key = lambda org: key()
    learner._post = lambda org, path, body: sent.append(body)
    learner._send_one((task(), Outcome(event="merged"), 0, 0))
    learner._send_one((task(), Outcome(event="review"), 1, 0))
    assert asked == [AGENT]
    assert FIELDS <= set(sent[0]) and not FIELDS & set(sent[1])


def test_the_learner_omits_them_when_the_lookup_fails():
    sent = []

    def lookup(agent_id):
        raise RuntimeError("no db")

    learner = CloudLearner(REPO, org=lambda: "org1", cost=COSTS.get, start_thread=False,
                           decision_for=lookup)
    learner.identity = lambda: "identity"
    learner.key = lambda org: key()
    learner._post = lambda org, path, body: sent.append(body)
    learner._send_one((task(), Outcome(event="merged"), 0, 0))
    assert len(sent) == 1 and not FIELDS & set(sent[0])


def test_the_db_finds_a_decision_by_agent(db):
    db.add_routing_decision(REPO, task_id="t", agent_id="a1", weight="medium",
                            baseline_profile="developer", profile="cheap", learned=True)
    found = db.routing_decision_for_agent("a1")
    assert found.baseline_profile == "developer" and found.learned == 1
    assert db.routing_decision_for_agent("nope") is None
