"""What a reviewer gets from the analysis-doc library (the server's block, only
when it names a changed file, and nothing without assist), and brindle's
one-time offer to plan a big goal first (brindle.plans): when a goal
qualifies, and that an answer settles only the offer it saw. On database rows
with git faked."""
import time

import pytest

from brindle import agents, autopilot, knowledge, plans
from brindle.db import Agent, Workspace
from brindle.pro import analysis_brief as ab
from brindle.pro import auth, credentials
from brindle.service.errors import ServiceError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    monkeypatch.setattr(knowledge, "repo_key", lambda root: str(root))


@pytest.fixture
def boss(db, tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    ws = Workspace("r/main", str(tmp_path / "repo"), "main", "main", "main", None, str(tmp_path / "repo"),
                   None, "brindle_r_main", time.time())
    db.add_workspace(ws)
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


@pytest.mark.parametrize("kwargs, large", [
    ({"weight": "heavy", "milestones": 3}, False),   # neither weight nor milestone count asks
    ({"requested": True, "branch": "merge/x"}, False),
    pytest.param({"requested": True, "branch": "team/alice/merge/main"}, False,
                 marks=pytest.mark.variant("branch_prefix puts the namespace in front of the chore name")),
])
@pytest.mark.variant("large_work is one expression: weight and milestone counts never ask, chore branches at any prefix depth")
def test_large_work_needs_an_explicit_request(kwargs, large):
    assert ab.large_work(**kwargs) is large


@pytest.mark.parametrize("block, shown", [("the cache design: src/cache.py is hot.", True),
                                          ("unrelated export test doc", False),
                                          ("backup in src/cache.py.bak and old/src/cache.py", False)])
@pytest.mark.variant("whole-path match: lookalike paths such as cache.py.bak or old/src/cache.py must not count")
def test_reviewer_gets_the_doc_block_only_when_it_names_a_changed_file(block, shown, db, tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_brief", lambda *a, **kw: (block, [(ab.DOC_ITEM, "d1", "3")]))
    out, cited = ab.review_context(db, str(tmp_path), "speed up the cache", ["src/cache.py"])
    assert ("\n" + block + "\n" in out, bool(cited)) == (shown, shown)
    assert "Flag as a blocker" not in out


def _offer(db):
    from brindle.service.dto import GoalDTO

    return GoalDTO.from_autopilot(db.get_autopilot("boss"), []).plan_offer


def _plan(count, repo=None):
    return [(f"M{i}", "true", None, None, repo) for i in range(count)]


@pytest.fixture
def goal_env(db, boss, monkeypatch):
    monkeypatch.setattr("brindle.daemon.holds.app_connected", lambda: True)
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    monkeypatch.setattr(agents, "flush", lambda db_, agent_id: None)   # no terminal to push the answer to
    db.add_autopilot("boss")


@pytest.mark.parametrize("milestones, goal, offered", [
    pytest.param(_plan(3), "Redesign the sync architecture", True,
                 marks=pytest.mark.variant("a multi-milestone goal offers planning without requiring multiple repos")),
    # An omitted repo is the root checkout: with one attached repo that is two repos.
    pytest.param(_plan(1) + _plan(1, "web"), "Update the docs", True,
                 marks=pytest.mark.variant("an omitted repo and an attached repo count as two repositories")),
    pytest.param(_plan(2, "web"), "Update the docs", False,
                 marks=pytest.mark.variant("multiple milestones in one attached repo do not imply multiple repos")),
    # A bug fix is never offered, however many milestones or repos, or design words.
    (_plan(5) + _plan(1, "web"), "Fix the crash by refactoring sync", False)])
def test_large_goal_is_offered_planning_first_once(milestones, goal, offered, db, goal_env):
    from brindle.daemon import routes_actions

    autopilot.set_goal(db, "boss", goal, milestones)
    assert (_offer(db) is not None) is offered
    if not offered:
        return
    first = _offer(db)
    autopilot.set_goal(db, "boss", goal, milestones)    # the same goal again keeps the one offer
    assert _offer(db) == first
    from brindle.service import inbox

    asks = [i for i in inbox.items(db, None) if i.kind == "plan_offer"]    # the Inbox carries the ask
    assert [(i.request_id, i.root_id) for i in asks] == [(first, "boss")]
    with pytest.MonkeyPatch.context() as mp:    # a paused session the live snapshot no longer lists
        mp.setattr(inbox.view, "snapshot", lambda *a, **k: [])
        assert [i.request_id for i in inbox.items(db, None) if i.kind == "plan_offer"] == [first]
    routes_actions.goal_plan_offer(db, {}, {"root": "boss", "accept": True, "offer": first})
    assert (_offer(db), db.get_autopilot("boss").plan_offer) == (None, plans.OFFER_ACCEPTED)
    assert not [i for i in inbox.items(db, None) if i.kind == "plan_offer"]
    autopilot.set_goal(db, "boss", goal, milestones)    # answered: never asked again for this goal
    assert _offer(db) is None
    with pytest.raises(ServiceError, match="no longer open"):
        plans.answer_offer(db, "boss", first, True)


@pytest.mark.variant("race: the goal grows between showing the offer and the answer")
def test_an_answer_belongs_to_the_offer_it_saw_and_a_grown_plan_is_offered(db, goal_env):
    autopilot.set_goal(db, "boss", "Goal A", _plan(2))
    assert _offer(db) is None
    autopilot.set_goal(db, "boss", "Goal A", _plan(5))        # the plan grew: now it deserves one
    a = _offer(db)
    assert a
    autopilot.set_goal(db, "boss", "Goal B", _plan(5), replace=True)        # replaced before A's answer arrived
    b = _offer(db)
    assert b and b != a
    with pytest.raises(ServiceError):
        plans.answer_offer(db, "boss", a, True)
    assert _offer(db) == b
    plans.answer_offer(db, "boss", b, False)                  # Not now
    assert (_offer(db), db.get_autopilot("boss").plan_offer) == (None, plans.OFFER_DISMISSED)


@pytest.mark.variant("real incident: a 3rd parallel worker opened the offer and held urgent work; "
                      "the audit then found a queued assignment did the same")
@pytest.mark.parametrize("assigned", ["started", "queued"])
def test_assigned_work_never_makes_a_goal_deserve_an_offer(assigned, db, boss, goal_env):
    """The reported bug: a 3rd parallel worker opened the offer and held urgent work.
    Neither workers nor queued assignments open one, even when the goal then grows."""
    autopilot.set_goal(db, "boss", "Small goal", _plan(1))
    for i in range(4):
        if assigned == "started":
            db.add_agent(Agent(f"w{i}", boss.id, "developer", "claude", "boss", "assign", "working", "",
                               None, time.time()))
        else:
            db.conn.execute("INSERT INTO tasks (id, repo_root, caller_id, caller_ws_id, profile, task_text, "
                            "mode, isolate, state, created_at) VALUES (?, '/r', 'boss', 'ws', 'developer', "
                            "'t', 'assign', 1, 'pending', ?)", (f"t{i}", time.time()))
        autopilot.consider_plan_offer(db, "boss")
    assert _offer(db) is None
    autopilot.set_goal(db, "boss", "Small goal", _plan(6))     # grown after work was assigned: still not held
    assert _offer(db) is None


@pytest.mark.parametrize("why", [
    pytest.param("on", marks=pytest.mark.variant("a user-only entitlement sends only the user's brief scope")),
    pytest.param("own", marks=pytest.mark.variant("the person's assist opt-out suppresses brief requests")),
    "group",
    pytest.param("airgap", marks=pytest.mark.variant("airgap must suppress even settings and scope requests")),
])
def test_no_brief_request_when_assist_is_off_by_policy_or_air_gap(why, db, boss, backend, monkeypatch):
    from brindle import airgap

    groups = {"g": {"switches": {"assist": "enforced_off"}}} if why == "group" else {}
    backend.routes.update({"POST /analysis-docs/brief": [(200, {"block": "known: the cache", "cited": []})],
                           "GET /analysis-docs/scopes": [(200, {"scopes": [{"kind": "group", "id": "g"}]})],
                           "GET /me/analysis-docs/settings": [(200, {"switches": {"assist": "off" if why == "own" else "on"}})]})
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    monkeypatch.setattr("brindle.pro.analysis_policy.fetch", lambda *a: {"switches": {}, "groups": groups})
    login_as(backend, credentials.default_store(),
             features=("analysis_docs", "analysis_docs_team") if why == "group" else ("analysis_docs",))
    monkeypatch.setattr(airgap, "enabled", lambda cfg=None: why == "airgap")
    backend.calls.clear()
    client = auth.Client(None, backend)
    assert ab.review_context(db, boss.repo_root, "speed up the cache", ["a.py"], client=client) == ("", [])
    sent = [f["scopes"] for k, f, _ in backend.calls if k == "POST /analysis-docs/brief"]
    assert sent == ([[{"kind": "user", "id": "user_1"}]] if why == "on" else [])   # scopes as the server takes them
    assert backend.calls == [] if why == "airgap" else True


def test_alias_and_path_of_one_attached_repo_are_one_repository(db, boss, goal_env, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    db.add_session_repo("boss", "web", str(web))
    autopilot.set_goal(db, "boss", "Small goal", _plan(1, "web") + _plan(1, str(web)))
    assert _offer(db) is None


@pytest.mark.variant("identity boundary: workers of a replaced goal must not count for the new one")
def test_a_stale_evaluation_cannot_reopen_a_dismissed_offer(db, goal_env):
    autopilot.set_goal(db, "boss", "Goal A", _plan(5))
    first = _offer(db)
    plans.answer_offer(db, "boss", first, False)
    assert db.open_plan_offer("boss", "Goal A", "open:late") is False    # a delayed second evaluation
    assert db.open_plan_offer("boss", "Goal OLD", "open:late") is False   # or one for a replaced goal
    assert _offer(db) is None and db.get_autopilot("boss").plan_offer == plans.OFFER_DISMISSED
