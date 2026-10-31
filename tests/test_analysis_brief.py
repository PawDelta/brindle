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


@pytest.mark.parametrize("weight, plan", [("medium", "pro"), ("heavy", "pro"), ("advanced", "free")])
def test_brief_only_for_large_work(weight, plan, db, backend, tmp_path):
    # Longer than any cap, with the usual fence markers inside: it must arrive whole and as is.
    block = "\n".join([*knowledge._FENCE, "x" * 20000, "final qualification "])
    backend.routes["POST /analysis-docs/brief"] = [(200, {"block": block, "cited": [{"doc_id": "d1", "version": 3}]})]
    login_as(backend, credentials.default_store(),
             features=("analysis_docs",) if plan == "pro" else ("learning",))
    repo = str(tmp_path / "repo")
    out = ab.brief_context(db, repo, "feat/x", "a1", "speed up the cache", ["src/cache.py"],
                           large=ab.large_work(weight), client=auth.Client(None, backend))
    sent = [p for p in backend.paths() if p == "POST /analysis-docs/brief"]
    sources = kb.sources(db, knowledge.repo_key(repo))
    receipt = [(k, i) for k, i, _ in knowledge.receipt(db, "a1")]
    if weight == "medium":
        assert (out, sent, sources, receipt) == ("", [], [], [])
    elif plan == "pro":
        assert f"\n{block}\n" in out and FOLDER in out
        assert [f["scopes"] for k, f, _ in backend.calls if k == "POST /analysis-docs/brief"] == [
            [{"kind": "user", "id": "user_1"}]]                     # scopes as the server takes them
        assert receipt == [(ab.RULE_ITEM, "a1"), (ab.BRIEF_ITEM, "a1"), (ab.DOC_ITEM, "d1"),
                           (ab.SESSION_DOC_ITEM, "d1")]
        assert sources == []
    else:
        assert sent == [] and FOLDER in out and receipt == [(ab.RULE_ITEM, "a1")]
        assert [s["scope"] for s in sources] == [str(ab.docs_dir(repo, "feat/x").parent.resolve())]


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


def test_fresh_resume_gives_the_rule_again(db, boss, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # no saved session
    db.add_agent(Agent("w1", boss.id, "developer", "claude", "boss", "assign", "paused", "", None,
                       time.time(), time.time(), "Add input validation.", "sess-w1"))
    knowledge.record_receipt(db, "w1", [(ab.RULE_ITEM, "w1", "")])
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw))
    agents.resume(db, "w1")
    assert FOLDER in launched[0]["prompt"]


def test_goal_is_briefed_once_and_told_to_leave_a_doc_when_audit_approves(db, boss, backend, monkeypatch):
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    backend.routes["POST /analysis-docs/brief"] = [(200, {"block": "known: the cache", "cited": []})]
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    login_as(backend, credentials.default_store(), features=("analysis_docs",))
    db.add_autopilot("boss")
    milestones = [("API", "true", None), ("UI", "true", None)]
    autopilot.set_goal(db, "boss", "Settings page", milestones)
    autopilot.set_goal(db, "boss", "Settings page", milestones)   # the same goal again: no second brief
    queued = [r[0] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss'")]
    assert len(queued) == 1 and "\nknown: the cache\n" in queued[0]
    db.update_autopilot("boss", audit_sha="abc123")
    out = autopilot.record_audit(db, "boss", True, "all there")
    assert f"{FOLDER}{ab.docs_dir(boss.repo_root, boss.branch)}" in out
