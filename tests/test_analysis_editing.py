"""Shared editing of analysis docs (phase "Then"): a session's draft of a
living doc publishes when its branch merges and is dropped when it is removed
unmerged, a clash goes to the session's supervisor, a session is told when a
doc it read changes, and a reviewer gets the docs and decisions for its paths."""
import time

import pytest

from brindle import agents, knowledge, pipeline
from brindle.db import Agent, Task, Workspace
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

FILES = [{"name": "design.md", "md": "# Storage\nTTL 60s\n"}]
DROP = "DELETE /analysis-docs/d1/draft?session=sup&branch=feat%2Fa"


@pytest.fixture
def remote(backend, monkeypatch):
    backend.routes.update({
        "GET /analysis-docs/d1": lambda f, h: (200, {"id": "d1", "version": backend.version, "kind": "living"}),
        "PUT /analysis-docs/d1/draft": [(200, {})],
        "POST /analysis-docs/d1/draft/publish": [(200, {"version": 2})],
        DROP: [(200, {})],
    })
    backend.version = 1
    monkeypatch.setattr(auth.UrllibTransport, "request",
                        lambda self, m, url, form, headers: backend.request(m, url, form, headers))
    login_as(backend, credentials.default_store(), features=("analysis_docs",))
    return backend


@pytest.fixture
def told(monkeypatch):
    out = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, text, **kw: out.append((to, text)))
    return out


def add(db, ws, agent_id, mode="assign", parent=None):
    db.add_agent(Agent(agent_id, ws.id, "developer", "claude", parent, mode, "idle", "@0", None, time.time()))
    return db.get_agent(agent_id)


def workspace(db, tmp_path, ws_id, branch):
    (tmp_path / ws_id).mkdir()
    ws = Workspace(ws_id, str(tmp_path), branch, "branch", branch, "main", str(tmp_path / ws_id), None, "t", 0.0)
    db.add_workspace(ws)
    return ws


def session(db, tmp_path, mode="assign"):
    """A supervisor and its worker on branch ``feat/a``, which read ``d1`` at version 1."""
    sup = add(db, workspace(db, tmp_path, "main", "main"), "sup", mode="interactive")
    ws = workspace(db, tmp_path, "w1", "feat/a")
    worker = add(db, ws, "w1a", mode=mode, parent=sup.id)
    ad.read_doc(db, worker.id, "d1")
    return sup, worker, ws


def merge(db, ws, tmp_path, monkeypatch):
    from brindle import codemap, codemap_docs, gates, git, workspaces

    monkeypatch.setattr(git, "ahead_behind", lambda path, ref: (0, 1))
    monkeypatch.setattr(gates, "head", lambda w: "abc123")
    monkeypatch.setattr(workspaces, "sync_with_base", lambda w: workspaces.SyncResult("up_to_date"))
    monkeypatch.setattr(gates, "run", lambda *a, **k: gates.Report(sha="abc123"))
    monkeypatch.setattr(workspaces, "merge_back", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(codemap, "refresh_later", lambda *a, **k: None)
    monkeypatch.setattr(codemap_docs, "docs_refresh_note", lambda *a, **k: None)
    assert pipeline.merge(db, None, ws).startswith("Merged")


def calls(remote, key):
    return [form for k, form, _ in remote.calls if k == key]


def states(db, session_id="sup"):
    return {key: d["state"] for key, d in ad.drafts(db, session_id).items()}


GONE = "server down, then the session is gone"
GONE_CLASH = "server down, then the session is gone and the draft clashes"


@pytest.mark.parametrize("outcome", ["merged", "removed", GONE, GONE_CLASH])
def test_draft_publishes_on_merge_and_is_dropped_unmerged(outcome, db, remote, told, tmp_path, monkeypatch):
    from brindle import mcp_server

    sup, worker, ws = session(db, tmp_path, mode="handoff")
    ad.save_draft(db, worker, ws.branch, "d1", FILES)
    assert calls(remote, "PUT /analysis-docs/d1/draft") == [
        {"session": "sup", "branch": "feat/a", "base_version": 1, "files": FILES}]
    db.set_result(worker.id, "done")                # the handoff completes: its worker is removed
    mcp_server._await_worker(db, worker.id, 1)
    assert db.get_agent(worker.id) is None
    if outcome in (GONE, GONE_CLASH):
        remote.routes["POST /analysis-docs/d1/draft/publish"] = [(503, {}), (200, {"version": 2}) if outcome == GONE
                                                                  else (409, {"error": "section_clash", "clashes": []})]
    if outcome == "removed":
        pipeline.on_workspace_cleanup(db, ws)
    else:
        merge(db, ws, tmp_path, monkeypatch)
        pipeline.on_workspace_cleanup(db, ws)      # removing a merged branch drops nothing
    if outcome in (GONE, GONE_CLASH):
        assert ad.owed(db) == {("sup", "feat/a", "d1"): {"op": "publish", "by": "w1a"}}
        db.delete_workspace(ws.id)
        db.delete_workspace(sup.workspace_id)       # its agents go with it
        other = add(db, workspace(db, tmp_path, "w9", "feat/z"), "other")
        agents.handle_hook(db, other.id, "stop", {})   # any session's turn ends: the queue is sent
    published = calls(remote, "POST /analysis-docs/d1/draft/publish")
    draft = {"session": "sup", "branch": "feat/a"}
    dropped = [draft for _ in calls(remote, DROP)]         # its session and branch are in the query
    assert (published, dropped) == {"merged": ([draft], []), "removed": ([], [draft]), GONE: ([draft, draft], []),
                                    GONE_CLASH: ([draft, draft], [draft])}[outcome]   # no one left to reconcile
    assert states(db) == {} and ad.owed(db) == {} and told == []


def test_each_branchs_clash_goes_to_the_supervisor_to_reconcile(db, remote, told, tmp_path, monkeypatch):
    sup, worker_a, ws_a = session(db, tmp_path)
    ws_b = workspace(db, tmp_path, "w2", "feat/b")
    worker_b = add(db, ws_b, "w2a", parent=sup.id)
    ad.read_doc(db, worker_b.id, "d1")
    clash = (409, {"error": "section_clash", "clashes": ["Storage"], "base_version": 1, "head_version": 3})
    remote.routes["POST /analysis-docs/d1/draft/publish"] = [clash, clash, (200, {"version": 4})]
    for worker, ws in ((worker_a, ws_a), (worker_b, ws_b)):
        ad.save_draft(db, worker, ws.branch, "d1", FILES)
        merge(db, ws, tmp_path, monkeypatch)
    assert [to for to, _ in told] == ["sup", "sup"] and all('"Storage"' in text for _, text in told)
    assert states(db) == {("feat/a", "d1"): "clash", ("feat/b", "d1"): "clash"}
    remote.version = 3                              # the supervisor reconciles on the newest version
    ad.read_doc(db, sup.id, "d1")
    with pytest.raises(RuntimeError):               # two drafts clash: it must say which
        ad.save_draft(db, sup, "main", "d1", FILES)
    ad.save_draft(db, sup, "main", "d1", FILES, clashed_branch="feat/b")
    assert calls(remote, "PUT /analysis-docs/d1/draft")[-1] == {
        "session": "sup", "branch": "feat/b", "base_version": 3, "files": FILES}
    assert calls(remote, "POST /analysis-docs/d1/draft/publish")[-1] == {"session": "sup", "branch": "feat/b"}
    assert (ad.UPDATED_ITEM, "d1", "published") in knowledge.receipt(db, "sup")
    assert states(db) == {("feat/a", "d1"): "clash"}


@pytest.mark.parametrize("how", ["published here", "changed elsewhere"])
def test_a_changed_doc_tells_only_the_session_that_read_it(how, db, remote, told, tmp_path, monkeypatch):
    sup, reader, ws = session(db, tmp_path)
    if how == "published here":
        other_ws = workspace(db, tmp_path, "w2", "feat/b")
        other = add(db, other_ws, "w2a")
        ad.read_doc(db, other.id, "d1")
        ad.save_draft(db, other, other_ws.branch, "d1", FILES)
        merge(db, other_ws, tmp_path, monkeypatch)
        ad.tell_readers(db, "d1", 2)                # the same version again: told once
    else:
        remote.routes["POST /analysis-docs/versions"] = [(200, {"versions": {"d1": 2}})]
        agents.handle_hook(db, reader.id, "stop", {})   # its turn ends: the server is asked
        agents.handle_hook(db, reader.id, "stop", {})
    about_d1 = [(to, text) for to, text in told if "d1" in text]
    assert [to for to, _ in about_d1] == ["w1a"] and "version 2" in about_d1[0][1]
    assert db.conn.execute("SELECT COUNT(*) FROM doc_requests").fetchone()[0] == 0   # nothing for the person
    if how == "changed elsewhere":                  # a native worker's report waits until it reads it again
        from brindle.native import NativeAgent, Permissions, Toolbox, runner
        from brindle.native.client import Reply, ToolCall

        remote.version = 2
        calls_ = [("report_result", {"result": "done"}), ("read_analysis_doc", {"doc": "d1"}),
                  ("report_result", {"result": "done"})]
        replies = [Reply("", [ToolCall(str(i), name, args)], "tool_calls") for i, (name, args) in enumerate(calls_)]
        held = []

        class Model:
            def complete(self, system, messages, specs, **kw):
                held.append(db.get_agent(reader.id).result)
                return replies.pop(0) if replies else Reply("finished", [], "stop")

        toolbox = Toolbox().add(*runner.brindle_tools(db, reader.id, ws, "assign"))
        NativeAgent(Model(), toolbox, Permissions("default", []), "").run("go")
        assert held == [None, None, None, "done"]   # unset after the first report, set once it read d1 again


def test_reviewer_brief_has_the_docs_and_decisions_and_a_stale_doc_holds_the_verdict(
        db, repo, remote, told, monkeypatch):
    key = knowledge.repo_key(str(repo))
    knowledge.record_decision(db, key, "Cache TTL", "Keep the TTL at 60s", paths=["src/auth.py"])
    knowledge.record_decision(db, key, "Docs style", "Plain prose", paths=["docs/**"])
    remote.routes["POST /analysis-docs/brief"] = [(200, {"block": "The cache design doc says TTL 60s.",
                                                        "cited": [{"doc_id": "d1", "version": 1}]})]
    ws = Workspace("ws-rev", str(repo), "feat/rev", "branch", "feat/rev", "main", str(repo), None, "t", 0.0)
    db.add_workspace(ws)
    worker = add(db, ws, "w1")
    db.add_task(Task("t-rev", str(repo), worker.id, None, ws.id, "developer", "fix auth", "assign", 1,
                     "feat/rev", None, '["src/auth.py"]', None, "started", 0.0))
    prompts = []

    def spawn(db_, ws_, profile, **kw):
        prompts.append(kw["prompt"])
        return add(db, ws_, "rev1", mode="review")

    monkeypatch.setattr(agents, "spawn", spawn)
    agents.request_review(db, None, ws)
    [prompt] = prompts
    assert "Keep the TTL at 60s" in prompt and "Plain prose" not in prompt
    assert "The cache design doc says TTL 60s." in prompt
    assert calls(remote, "POST /analysis-docs/brief")[0]["paths"] == ["src/auth.py"]
    remote.version = 2                              # d1 changes before the verdict
    remote.routes["POST /analysis-docs/versions"] = lambda f, h: (200, {"versions": {"d1": remote.version}})
    monkeypatch.setattr(agents, "finish_review", lambda *a, **k: None)
    agents.submit_review(db, "rev1", True, "looks right")
    assert db.get_agent("rev1").result is None      # held: it read version 1
    assert calls(remote, "POST /analysis-docs/versions")[-1] == {"doc_ids": ["d1"]}
    ad.read_doc(db, "rev1", "d1")
    agents.submit_review(db, "rev1", True, "still right")
    assert db.get_agent("rev1").result is not None
