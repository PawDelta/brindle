"""Explicit requests to read knowledge documents, independent of indexing."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brindle import identity_lock, kb, mcp_server, view
from brindle.daemon.routes_actions import inbox_action
from brindle.db import Agent, Workspace
from brindle.service import inbox
from brindle.pro import auth, credentials
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as


def test_request_doc_read_and_dismiss(db, tmp_path, monkeypatch):
    repo = str(tmp_path)
    db.add_workspace(Workspace("proj", repo, "proj", "root", "main", None, repo, None, "proj", 0))
    db.add_agent(Agent("sup1", "proj", "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "sup1")
    monkeypatch.setattr(inbox, "_setup_items", lambda: [])
    monkeypatch.setattr(inbox, "_org_notices", lambda: [])
    monkeypatch.setattr(mcp_server.knowledge, "repo_key", lambda _: repo)
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n## Storage\n\nKeep requests in the database.\n")
    source = kb.add_source(db, repo, "folder", repo)
    kb.sync(db, source)
    doc = db.conn.execute("SELECT id FROM kb_docs").fetchone()[0]
    path.write_text("# Plan\n\n## Storage\n\nKeep explicit requests in the database.\n")
    kb.sync(db, source)
    assert not [i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]

    with pytest.raises(ToolError):
        mcp_server.request_doc_read(doc + 1, "Read a missing doc.")
    with pytest.raises(ToolError):
        mcp_server.request_doc_read(doc, "First line.\nSecond line.")
    with pytest.raises(ToolError):
        mcp_server.request_doc_read(doc, " ")
    with db.tx() as c:
        c.execute("UPDATE kb_sources SET repo_key = 'other-repo' WHERE id = ?", (source,))
    with pytest.raises(ToolError):
        mcp_server.request_doc_read(doc, "Read another repo's doc.")
    with db.tx() as c:
        c.execute("UPDATE kb_sources SET repo_key = ? WHERE id = ?", (repo, source))

    mcp_server.request_doc_read(doc, "Check the proposed storage.", section="Storage")
    mcp_server.request_doc_read(doc, "Read the whole plan.")
    # Reopen the first implementation's local-only schema with pending rows.
    with db.tx() as c:
        c.execute("ALTER TABLE doc_requests RENAME TO new_doc_requests")
        c.execute("CREATE TABLE doc_requests (id TEXT PRIMARY KEY,"
                  " agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,"
                  " doc_id INTEGER NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,"
                  " section TEXT, why TEXT NOT NULL, created_at REAL NOT NULL)")
        c.execute("INSERT INTO doc_requests SELECT id, agent_id, doc_id, section, why, created_at"
                  " FROM new_doc_requests")
        c.execute("DROP TABLE new_doc_requests")
    db._migrate()
    shown = [i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]
    assert [(i.doc, i.section, i.reason, i.agent_id, i.root_id) for i in shown] == [
        (doc, "Storage", "Check the proposed storage.", "sup1", "sup1"),
        (doc, None, "Read the whole plan.", "sup1", "sup1"),
    ]
    assert shown[0].request_id != shown[1].request_id
    db.update_agent("sup1", status="paused", status_since=1, tmux_window="@1")
    monkeypatch.setattr(view.time, "time", lambda: 100)
    assert not [a for ws in view.snapshot(db, repo, panes={}) for a in ws["agents"]]
    assert [i.request_id for i in inbox.items(db, repo, panes={}) if i.kind == "doc"] == [i.request_id for i in shown]
    assert not [i for i in inbox.items(db, "other-repo", panes={}) if i.kind == "doc"]
    with monkeypatch.context() as locked:
        locked.setattr(identity_lock, "locked_by", lambda _: "another identity")
        assert not [i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]
    inbox_action(db, {"id": shown[0].request_id, "action": "dismiss"}, {})
    assert [i.request_id for i in inbox.items(db, repo, panes={}) if i.kind == "doc"] == [shown[1].request_id]


def test_request_server_doc(db, tmp_path, monkeypatch, backend):
    repo = str(tmp_path)
    db.add_workspace(Workspace("proj", repo, "proj", "root", "main", None, repo, None, "proj", 0))
    db.add_agent(Agent("sup1", "proj", "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "sup1")
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, m, url, form, headers:
                        backend.request(m, url, form, headers))
    login_as(backend, credentials.default_store(), features=("analysis_docs",))
    backend.routes["GET /analysis-docs/d1"] = [(200, {"id": "d1", "title": "Server plan", "body_md": "## Storage\nKeep it durable."})]
    backend.routes["GET /analysis-docs/denied"] = [(403, {"error": "forbidden"})]
    assert db.conn.execute("SELECT count(*) FROM kb_docs").fetchone()[0] == 0
    mcp_server.request_doc_read("d1", "Check server storage.", section="Storage")
    shown = [i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]
    assert [(i.doc, i.section, i.detail) for i in shown] == [("d1", "Storage", "Server plan")]
    with pytest.raises(ToolError):
        mcp_server.request_doc_read("denied", "Read an inaccessible doc.")
    assert len([i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]) == 1
    inbox_action(db, {"id": shown[0].request_id, "action": "dismiss"}, {})
    assert not [i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]
