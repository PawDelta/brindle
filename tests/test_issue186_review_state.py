"""#186: where a reported branch is in review shows in list_agents and the sidebar."""
import json
from pathlib import Path

import pytest

from conftest import sh
from brindle import gates, mcp_server, pipeline, view, workspaces
from test_merge_order import add


@pytest.fixture
def branch(db, repo, monkeypatch):
    """A worker's committed branch under a supervisor, in the pipeline's hands."""
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"review": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="done", result="added new.py")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def test_issue186_review_state_tells_silence_apart(db, branch):
    ws = branch
    sha = gates.head(ws)[:8]
    assert pipeline.review_state(db, ws) is None

    db.update_agent("w1", pipeline="reviewing")
    assert "nothing" in pipeline.review_state(db, ws)
    with pipeline.review_run(ws.id):
        assert pipeline.review_state(db, ws) == "checks running"
    add(db, ws, "rev0", "review", "reviewer", parent="boss", status="processing")
    assert pipeline.review_state(db, ws) == "reviewer running (rev0)"

    db.set_status("rev0", "done")
    db.add_review(ws.id, gates.head(ws), "rev0", False, "no")
    db.update_agent("w1", pipeline=None)
    assert pipeline.review_state(db, ws) == f"changes requested at {sha}"
    db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
    assert pipeline.review_state(db, ws) == f"approved at {sha}"

    db.update_agent("w1", merge_failed="check `pytest` failed:\nboom\nsecond line")
    assert pipeline.review_state(db, ws) == "merge failed: check `pytest` failed: boom second line"


def test_issue186_review_state_shows_in_list_agents_and_the_sidebar(db, branch):
    ws = branch
    db.update_agent("w1", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer", parent="boss", status="processing")
    [line] = [x for x in mcp_server.list_agents().splitlines() if x.startswith("w1 ")]
    assert "review=reviewer running (rev0)" in line
    [entry] = [e for e in view.snapshot(db, ws.repo_root, panes={}) if e["id"] == ws.id]
    assert entry["review_state"] == "reviewer running (rev0)"
