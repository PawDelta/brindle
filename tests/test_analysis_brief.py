"""What requested docs get from the analysis-doc library: nothing without a request;
otherwise the rule and a docs folder, plus the server's block, verbatim and
fenced, recorded in the task's receipt (Pro), or the folder as a Knowledge
source (free tier). Reached through assign, requeue, a fresh resume and a goal,
on database rows with git faked."""
import asyncio
import time

import pytest

from brindle import agents, autopilot, kb, knowledge, mcp_server, tasks
from brindle.db import Agent, Task, Workspace
from brindle.pro import analysis_brief as ab
from brindle.pro import auth, credentials
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

FOLDER = "Docs folder: "


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
    ({"weight": "heavy"}, False),   # weight alone never asks
    ({"requested": True}, True), ({"milestones": 2}, False), ({"milestones": 3}, False),
    ({"requested": True, "branch": "merge/x"}, False), ({"milestones": 3, "branch": "sync-main"}, False),
    pytest.param({"requested": True, "branch": "team/alice/merge/main"}, False,
                 marks=pytest.mark.variant("branch_prefix puts the namespace in front of the chore name")),
])
@pytest.mark.variant("large_work is one expression: weight and milestone counts never ask, chore branches at any prefix depth")
def test_large_work_needs_an_explicit_request(kwargs, large):
    assert ab.large_work(**kwargs) is large


@pytest.mark.parametrize("weight, plan", [("medium", "pro"), ("heavy", "pro"), ("heavy", "off"),
                                          ("advanced", "free")])
def test_brief_only_for_large_work(weight, plan, db, backend, tmp_path):
    # Longer than any cap, with the usual fence markers inside: it must arrive whole and as is.
    block = "\n".join([*knowledge._FENCE, "x" * 20000, "final qualification "])
    backend.routes["POST /analysis-docs/brief"] = [(200, {"block": block, "cited": [{"doc_id": "d1", "version": 3}]})]
    switches = {"assist": "on"} if plan == "pro" else {"assist": "off"} if plan == "off" else {}
    backend.routes["GET /me/analysis-docs/settings"] = [(200, {"switches": switches})]
    login_as(backend, credentials.default_store(),
             features=("learning",) if plan == "free" else ("analysis_docs",))
    repo = str(tmp_path / "repo")
    out = ab.brief_context(db, repo, "feat/x", "a1", "speed up the cache", ["src/cache.py"],
                           large=ab.large_work(requested=weight != "medium"), client=auth.Client(None, backend))
    assert plan != "off" or ab.review_context(db, repo, "x", ["a.py"], client=auth.Client(None, backend)) == ("", [])
    sent = [p for p in backend.paths() if p == "POST /analysis-docs/brief"]
    sources = kb.sources(db, knowledge.repo_key(repo))
    receipt = [(k, i) for k, i, _ in knowledge.receipt(db, "a1")]
    if weight == "medium":
        assert (out, sent, sources, receipt) == ("", [], [], [])
    elif plan == "pro":
        assert f"\n{block}\n" in out and FOLDER in out
        assert [f["scopes"] for k, f, _ in backend.calls if k == "POST /analysis-docs/brief"] == [
            [{"kind": "user", "id": "user_1"}]]                     # scopes as the server takes them
        assert receipt == [(ab.RULE_ITEM, "a1"), (ab.BRIEF_ITEM, "a1"), (ab.BRIEF_TEXT_ITEM, "a1"),
                           (ab.DOC_ITEM, "d1"),
                           (ab.SESSION_DOC_ITEM, "d1")]
        assert sources == []
    else:
        assert sent == [] and FOLDER in out and receipt == [(ab.RULE_ITEM, "a1")]
        assert [s["scope"] for s in sources] == ([str(ab.docs_dir(repo, "feat/x").parent.resolve())]
                                                 if plan == "free" else [])


@pytest.mark.parametrize("via", ["assign", "requeue"])
def test_explicitly_requested_subagent_task_gets_the_rule(via, db, boss):
    if via == "assign":
        asyncio.run(mcp_server.assign("subagent", "Fix a typo", isolate=False, analysis_doc=True))
    else:
        db.add_task(Task("t1", boss.repo_root, None, "boss", boss.id, "subagent", "Fix a typo", "assign",
                         0, None, None, None, None, "cancelled", time.time()))
        db.update_task("t1", analysis_doc=1)
        tasks.requeue(db, db.get_agent("boss"), boss, "t1", depends_on=[])   # starts at once
    [worker] = [a for a in db.list_agents() if a.provider == "subagent"]
    assert f"{FOLDER}{ab.docs_dir(boss.repo_root, boss.branch)}" in worker.task


def test_fresh_resume_replays_the_rule_and_brief_without_fetching(db, boss, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # no saved session
    db.add_agent(Agent("w1", boss.id, "developer", "claude", "boss", "assign", "paused", "", None,
                       time.time(), time.time(), "Add input validation.", "sess-w1"))
    knowledge.record_receipt(db, "w1", [(ab.RULE_ITEM, "w1", ""), (ab.BRIEF_TEXT_ITEM, "w1", "known: the cache")])
    fetched = []
    monkeypatch.setattr(ab, "_brief", lambda *a, **kw: fetched.append(a) or ("new brief", []))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw))
    agents.resume(db, "w1")
    assert FOLDER in launched[0]["prompt"] and "\nknown: the cache\n" in launched[0]["prompt"]
    assert fetched == []


@pytest.mark.parametrize("block, shown", [("the cache design: src/cache.py is hot.", True),
                                          ("unrelated export test doc", False),
                                          ("backup in src/cache.py.bak and old/src/cache.py", False)])
@pytest.mark.variant("whole-path match: lookalike paths such as cache.py.bak or old/src/cache.py must not count")
def test_reviewer_gets_the_doc_block_only_when_it_names_a_changed_file(block, shown, db, tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_brief", lambda *a, **kw: (block, [(ab.DOC_ITEM, "d1", "3")]))
    out, cited = ab.review_context(db, str(tmp_path), "speed up the cache", ["src/cache.py"])
    assert ("\n" + block + "\n" in out, bool(cited)) == (shown, shown)
    assert "Flag as a blocker" not in out


@pytest.mark.parametrize("requested", [False, True])
def test_goal_doc_requires_a_request_and_is_briefed_once(requested, db, boss, backend, monkeypatch):
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    backend.routes["POST /analysis-docs/brief"] = [(200, {"block": "known: the cache", "cited": []})]
    backend.routes["GET /me/analysis-docs/settings"] = [(200, {"switches": {"assist": "on"}})]
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    login_as(backend, credentials.default_store(), features=("analysis_docs",))
    db.add_autopilot("boss")
    milestones = [("API", "true", None), ("UI", "true", None), ("Docs", "true", None)]
    autopilot.set_goal(db, "boss", "Settings page", milestones, analysis_doc=requested)
    autopilot.set_goal(db, "boss", "Settings page", milestones)   # the same goal again: no second brief
    autopilot.set_goal(db, "boss", "Settings page", milestones, analysis_doc=requested)
    queued = [r[0] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss'")]
    assert len(queued) == int(requested)
    if requested:
        assert "\nknown: the cache\n" in queued[0]
    db.update_autopilot("boss", audit_sha="abc123")
    out = autopilot.record_audit(db, "boss", True, "all there")
    assert (f"{FOLDER}{ab.docs_dir(boss.repo_root, boss.branch)}" in out) is requested
    if not requested:
        from brindle.daemon import routes_actions

        monkeypatch.setattr(ab, "_entitled", lambda store=None: None)  # free tier: no reference brief
        for _ in range(2):
            routes_actions.goal_analysis_doc(db, {}, {"root": "boss", "analysis_doc": True})
        queued = [r[0] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss'")]
        assert len(queued) == 1 and f"{FOLDER}{ab.docs_dir(boss.repo_root, boss.branch)}" in queued[0]
        assert db.get_autopilot("boss").state == "done"
    autopilot.set_goal(db, "boss", "Another goal", milestones)
    assert autopilot._goal_doc_rule(db, "boss") == ""


def _offer(db):
    from brindle.service.dto import GoalDTO

    return GoalDTO.from_autopilot(db.get_autopilot("boss"), []).analysis_offer


def _plan(count, repo=None):
    return [(f"M{i}", "true", None, None, repo) for i in range(count)]


@pytest.fixture
def goal_env(db, boss, monkeypatch):
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    monkeypatch.setattr(ab, "_entitled", lambda store=None: None)
    db.add_autopilot("boss")


@pytest.mark.parametrize("milestones, goal, offered", [
    (_plan(2), "Fix the typo", False), (_plan(4), "Fix the typo", True),
    (_plan(3), "Redesign the sync architecture", True),
    # An omitted repo is the root checkout: with one attached repo that is two repos.
    (_plan(1) + _plan(1, "web"), "Fix the typo", True), (_plan(2, "web"), "Fix the typo", False)])
def test_large_goal_is_offered_a_doc_once_and_yes_turns_it_on(milestones, goal, offered, db, goal_env):
    from brindle.daemon import routes_actions

    autopilot.set_goal(db, "boss", goal, milestones)
    assert (_offer(db) is not None) is offered
    if not offered:
        return
    first = _offer(db)
    autopilot.set_goal(db, "boss", goal, milestones)    # the same goal again keeps the one offer
    assert _offer(db) == first
    routes_actions.goal_analysis_offer(db, {}, {"root": "boss", "accept": True, "offer": first})
    assert (_offer(db), db.get_autopilot("boss").analysis_doc) == (None, 1)
    autopilot.set_goal(db, "boss", goal, milestones)    # answered: never asked again for this goal
    assert _offer(db) is None
    with pytest.raises(autopilot.AutopilotError):
        autopilot.resolve_analysis_offer(db, "boss", True, first)


@pytest.mark.variant("race: the goal grows between showing the offer and the answer")
def test_an_answer_belongs_to_the_offer_it_saw_and_a_grown_plan_is_offered(db, goal_env):
    autopilot.set_goal(db, "boss", "Goal A", _plan(2))
    assert _offer(db) is None
    autopilot.set_goal(db, "boss", "Goal A", _plan(4))        # the plan grew: now it deserves one
    a = _offer(db)
    assert a
    autopilot.set_goal(db, "boss", "Goal B", _plan(4))        # replaced before A's answer arrived
    b = _offer(db)
    assert b and b != a
    with pytest.raises(autopilot.AutopilotError):
        autopilot.resolve_analysis_offer(db, "boss", True, a)
    assert (_offer(db), db.get_autopilot("boss").analysis_doc) == (b, 0)
    autopilot.resolve_analysis_offer(db, "boss", False, b)    # Not now: off, and a chat request still wins
    assert (_offer(db), db.get_autopilot("boss").analysis_doc) == (None, 0)
    autopilot.set_goal(db, "boss", "Goal B", _plan(4), analysis_doc=True)
    assert db.get_autopilot("boss").analysis_doc == 1


@pytest.mark.variant("input variant: a goal with few milestones that grows through its workers")
def test_several_workers_make_a_small_goal_deserve_an_offer(db, boss, goal_env):
    autopilot.set_goal(db, "boss", "Small goal", _plan(1))
    for i in range(3):
        assert _offer(db) is None
        db.add_agent(Agent(f"w{i}", boss.id, "developer", "claude", "boss", "assign", "working", "", None, time.time()))
        autopilot.consider_analysis_offer(db, "boss")
    offer = _offer(db)
    assert offer
    db.add_agent(Agent("w3", boss.id, "developer", "claude", "boss", "assign", "working", "", None, time.time()))
    autopilot.consider_analysis_offer(db, "boss")
    assert _offer(db) == offer


@pytest.mark.parametrize("via", ["app", "cli", "tool"])
def test_person_can_request_and_cancel_a_goal_doc(via, db, boss, monkeypatch):
    from typer.testing import CliRunner
    from brindle import cli
    from brindle.daemon import routes_actions

    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    monkeypatch.setattr(cli, "_session_root", lambda db: "boss")
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Settings", [("API", "true", None)])
    for requested in (True, False):
        if via == "app":
            routes_actions.goal_analysis_doc(db, {}, {"root": "boss", "analysis_doc": requested})
        elif via == "cli":
            result = CliRunner().invoke(cli.app, ["goal", "--analysis-doc" if requested else "--no-analysis-doc"])
            assert result.exit_code == 0, result.output
        else:
            mcp_server.set_goal("Settings", [{"title": "API", "check": "true"}], analysis_doc=requested)
        assert bool(autopilot._goal_doc_rule(db, "boss")) is requested


@pytest.mark.parametrize("entry, why", [("goal", "own"), ("task", "group"),
                                         ("goal", "airgap"), ("review", "airgap")])
def test_no_brief_request_when_assist_is_off_by_policy_or_air_gap(entry, why, db, boss, backend, monkeypatch):
    from brindle import airgap

    policy = {"switches": {}, "groups": {"g": {"switches": {"assist": "enforced_off"}}}}
    backend.routes.update({"POST /analysis-docs/brief": [(200, {"block": "known: the cache", "cited": []})],
                           "GET /analysis-docs/scopes": [(200, {"scopes": [{"kind": "group", "id": "g"}]})],
                           "GET /me/analysis-docs/settings": [(200, {"switches": {"assist": "off"} if why == "own" else {}})]})
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    monkeypatch.setattr("brindle.pro.analysis_policy.fetch", lambda *a: policy)
    login_as(backend, credentials.default_store(),
             features=("analysis_docs", "analysis_docs_team") if why == "group" else ("analysis_docs",))
    monkeypatch.setattr(airgap, "enabled", lambda cfg=None: why == "airgap")
    backend.calls.clear()
    client = auth.Client(None, backend)
    if entry == "goal":
        monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
        db.add_autopilot("boss")
        autopilot.set_goal(db, "boss", "Settings page", [("API", "true", None), ("UI", "true", None)],
                           analysis_doc=True)
    elif entry == "review":
        assert ab.review_context(db, boss.repo_root, "speed up the cache", ["a.py"], client=client) == ("", [])
    else:
        ab.brief_context(db, boss.repo_root, "feat/x", "a1", "speed up the cache", [], large=True, client=client)
    assert "POST /analysis-docs/brief" not in backend.paths()
    assert backend.calls == [] if why == "airgap" else True


def test_alias_and_path_of_one_attached_repo_are_one_repository(db, boss, goal_env, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    db.add_session_repo("boss", "web", str(web))
    autopilot.set_goal(db, "boss", "Small goal", _plan(1, "web") + _plan(1, str(web)))
    assert _offer(db) is None


@pytest.mark.variant("identity boundary: workers of a replaced goal must not count for the new one")
def test_workers_of_an_earlier_goal_do_not_count_for_a_new_goal(db, boss, goal_env):
    autopilot.set_goal(db, "boss", "Big goal", _plan(1))
    for i in range(3):
        db.add_agent(Agent(f"w{i}", boss.id, "developer", "claude", "boss", "assign", "done", "", None,
                           time.time()))
    autopilot.consider_analysis_offer(db, "boss")
    assert _offer(db)
    autopilot.set_goal(db, "boss", "Fix a typo", _plan(1))
    assert _offer(db) is None


@pytest.mark.variant("race: the goal is replaced after the offer was accepted")
def test_goal_replaced_after_acceptance_still_gets_its_own_doc_instruction(db, boss, goal_env, monkeypatch):
    autopilot.set_goal(db, "boss", "Goal A", _plan(4))
    a = _offer(db)
    db.update_autopilot("boss", state="done")
    real = autopilot._commission_doc

    def replaced_first(*args):    # B replaces A after the answer committed, before the briefing
        autopilot.set_goal(db, "boss", "Goal B", _plan(1))
        return real(*args)

    monkeypatch.setattr(autopilot, "_commission_doc", replaced_first)
    autopilot.resolve_analysis_offer(db, "boss", True, a)
    assert db.get_autopilot("boss").analysis_doc == 0       # B stays unrequested
    [queued] = [r[0] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss'")]
    assert 'the goal "Goal A"' in queued and FOLDER in queued


def test_a_stale_evaluation_cannot_reopen_a_dismissed_offer(db, goal_env):
    autopilot.set_goal(db, "boss", "Goal A", _plan(4))
    first = _offer(db)
    autopilot.resolve_analysis_offer(db, "boss", False, first)
    assert db.open_analysis_offer("boss", "Goal A", "open:late") is False    # a delayed second evaluation
    assert db.open_analysis_offer("boss", "Goal OLD", "open:late") is False   # or one for a replaced goal
    assert _offer(db) is None and db.get_autopilot("boss").analysis_offer == "dismissed"
