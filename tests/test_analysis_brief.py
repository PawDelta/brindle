"""What large work gets from the analysis-doc library: nothing for small work;
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
    ({"requested": True}, True), ({"milestones": 2}, False), ({"milestones": 3}, True),
    ({"requested": True, "branch": "merge/x"}, False), ({"milestones": 3, "branch": "sync-main"}, False),
    pytest.param({"requested": True, "branch": "team/alice/merge/main"}, False,
                 marks=pytest.mark.variant("branch_prefix puts the namespace in front of the chore name")),
])
@pytest.mark.variant("large_work is one expression, so line coverage cannot see these cases: weight no longer asks, 2 vs 3 milestones, chore branches at any prefix depth")
def test_large_work_needs_a_request_or_a_three_milestone_goal(kwargs, large):
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


def test_goal_is_briefed_once_and_told_to_leave_a_doc_when_audit_approves(db, boss, backend, monkeypatch):
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    backend.routes["POST /analysis-docs/brief"] = [(200, {"block": "known: the cache", "cited": []})]
    backend.routes["GET /me/analysis-docs/settings"] = [(200, {"switches": {"assist": "on"}})]
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    login_as(backend, credentials.default_store(), features=("analysis_docs",))
    db.add_autopilot("boss")
    milestones = [("API", "true", None), ("UI", "true", None), ("Docs", "true", None)]
    autopilot.set_goal(db, "boss", "Settings page", milestones)
    autopilot.set_goal(db, "boss", "Settings page", milestones)   # the same goal again: no second brief
    queued = [r[0] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss'")]
    assert len(queued) == 1 and "\nknown: the cache\n" in queued[0]
    db.update_autopilot("boss", audit_sha="abc123")
    out = autopilot.record_audit(db, "boss", True, "all there")
    assert f"{FOLDER}{ab.docs_dir(boss.repo_root, boss.branch)}" in out


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
        autopilot.set_goal(db, "boss", "Settings page", [("API", "true", None), ("UI", "true", None)])
    elif entry == "review":
        assert ab.review_context(db, boss.repo_root, "speed up the cache", ["a.py"], client=client) == ("", [])
    else:
        ab.brief_context(db, boss.repo_root, "feat/x", "a1", "speed up the cache", [], large=True, client=client)
    assert "POST /analysis-docs/brief" not in backend.paths()
    assert backend.calls == [] if why == "airgap" else True
