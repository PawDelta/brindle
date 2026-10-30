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


@pytest.fixture
def remote(backend, monkeypatch):
    backend.routes.update({
        "GET /analysis-docs/d1": lambda f, h: (200, {"id": "d1", "version": backend.version, "kind": "living"}),
        "PUT /analysis-docs/d1/drafts": [(200, {})],
        "POST /analysis-docs/d1/drafts/publish": [(200, {"version": 2})],
        "POST /analysis-docs/d1/drafts/drop": [(200, {})],
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


def session(db, tmp_path):
    """A supervisor and its worker on branch ``feat/a``, which read ``d1`` at version 1."""
    sup = add(db, workspace(db, tmp_path, "main", "main"), "sup", mode="interactive")
    ws = workspace(db, tmp_path, "w1", "feat/a")
    worker = add(db, ws, "w1a", parent=sup.id)
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


@pytest.mark.parametrize("outcome", ["merged", "clash", "removed"])
def test_draft_publishes_on_merge_is_dropped_unmerged_and_a_clash_goes_to_the_supervisor(
        outcome, db, remote, told, tmp_path, monkeypatch):
    sup, worker, ws = session(db, tmp_path)
    ad.save_draft(db, worker, ws.branch, "d1", FILES)
    assert calls(remote, "PUT /analysis-docs/d1/drafts") == [
        {"session": "sup", "branch": "feat/a", "base_version": 1, "files": FILES}]
    if outcome == "clash":
        remote.routes["POST /analysis-docs/d1/drafts/publish"] = [
            (409, {"error": "clash", "clashes": [{"section": "Storage", "version": 3}]}), (200, {"version": 4})]
    if outcome == "removed":
        pipeline.on_workspace_cleanup(db, ws)
    else:
        merge(db, ws, tmp_path, monkeypatch)
        pipeline.on_workspace_cleanup(db, ws)      # removing a merged branch drops nothing
    published = calls(remote, "POST /analysis-docs/d1/drafts/publish")
    dropped = calls(remote, "POST /analysis-docs/d1/drafts/drop")
    assert (published, dropped) == (([], [{"session": "sup", "branch": "feat/a"}]) if outcome == "removed"
                                    else ([{"session": "sup", "branch": "feat/a"}], []))
    assert [to for to, _ in told] == (["sup"] if outcome == "clash" else [])
    if outcome == "clash":
        assert '"Storage"' in told[0][1]
        remote.version = 3                          # the supervisor reconciles on the newest version
        ad.read_doc(db, sup.id, "d1")
        assert ad.save_draft(db, sup, "main", "d1", FILES) == "Reconciled d1: published."
        assert calls(remote, "PUT /analysis-docs/d1/drafts")[-1]["branch"] == "feat/a"
        assert calls(remote, "POST /analysis-docs/d1/drafts/publish")[-1] == {"session": "sup", "branch": "feat/a"}


@pytest.mark.parametrize("how", ["published here", "changed elsewhere"])
def test_a_changed_doc_tells_only_the_session_that_read_it(how, db, remote, told, tmp_path):
    sup, reader, _ = session(db, tmp_path)
    if how == "published here":
        other_ws = workspace(db, tmp_path, "w2", "feat/b")
        other = add(db, other_ws, "w2a")
        ad.read_doc(db, other.id, "d1")
        ad.save_draft(db, other, other_ws.branch, "d1", FILES)
        ad.publish_drafts(db, other_ws)
        ad.tell_readers(db, "d1", 2)                # the same version again: told once
    else:
        remote.routes["POST /analysis-docs/versions"] = [(200, {"versions": {"d1": 2}})]
        ad.check_reads(db, reader.id)
        ad.check_reads(db, reader.id)
    assert [to for to, _ in told] == ["w1a"] and "version 2" in told[0][1]
    assert db.conn.execute("SELECT COUNT(*) FROM doc_requests").fetchone()[0] == 0   # nothing for the person


def test_reviewer_brief_has_the_docs_and_decisions_for_its_paths(db, repo, remote, monkeypatch):
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
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, profile, **kw: prompts.append(kw["prompt"]) or
                        Agent("rev1", ws_.id, profile, "claude", None, "review", "starting", "@0", None, 0.0))
    agents.request_review(db, None, ws)
    [prompt] = prompts
    assert "Keep the TTL at 60s" in prompt and "Plain prose" not in prompt
    assert "The cache design doc says TTL 60s." in prompt and "contradicts" in prompt
    assert calls(remote, "POST /analysis-docs/brief")[0]["paths"] == ["src/auth.py"]
