"""Explicit requests to read knowledge documents, independent of indexing."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brindle import kb, mcp_server
from brindle.daemon.routes_actions import inbox_action
from brindle.db import Agent, Workspace
from brindle.service import inbox


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
    shown = [i for i in inbox.items(db, repo, panes={}) if i.kind == "doc"]
    assert [(i.doc, i.section, i.reason, i.agent_id, i.root_id) for i in shown] == [
        (doc, "Storage", "Check the proposed storage.", "sup1", "sup1"),
        (doc, None, "Read the whole plan.", "sup1", "sup1"),
    ]
    assert shown[0].request_id != shown[1].request_id
    inbox_action(db, {"id": shown[0].request_id, "action": "dismiss"}, {})
    assert [i.request_id for i in inbox.items(db, repo, panes={}) if i.kind == "doc"] == [shown[1].request_id]
