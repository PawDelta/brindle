import json
from urllib.parse import quote

from brindle import git, workspaces
from brindle.web.server import App, Request, load_routes, match


def request(db, repo, workspace_id):
    load_routes()
    path = f"/api/workspaces/{quote(workspace_id, safe='')}/diff"
    found = match("GET", path)
    assert found is not None
    handler, params = found
    return handler(Request(App(str(repo), None), "GET", path, {}, params, {}, _db=db))


def test_diff_returns_workspace_review_and_check_history(db, repo):
    ws = workspaces.create(db, str(repo), "feature", fetch=False, run_setup=False).workspace
    sha = git.out(["rev-parse", "HEAD"], repo)
    db.add_review(ws.id, "older", None, False, "needs work")
    db.add_review(ws.id, sha, None, True, "ready")
    db.set_check(ws.id, sha, "pytest", True, "passed")
    db.set_check(ws.id, "older", "lint", False, "x" * 5000)
    other = workspaces.create(db, str(repo), "other", fetch=False, run_setup=False).workspace
    db.add_review(other.id, sha, None, False, "unrelated")
    db.set_check(other.id, sha, "unrelated", False, "failed")
    data = json.loads(request(db, repo, ws.id).body)
    assert data["sha"] == sha
    assert [(r["sha"], r["approved"], r["summary"]) for r in data["reviews"]] == [
        ("older", 0, "needs work"), (sha, 1, "ready")]
    checks = {c["command"]: c for c in data["checks"]}
    assert set(checks) == {"pytest", "lint"}
    assert checks["pytest"]["ok"] and checks["pytest"]["output"] == "passed"
    assert not checks["lint"]["ok"] and len(checks["lint"]["output"]) == 4000


def test_diff_handles_unknown_out_of_scope_and_unavailable_workspaces(db, repo):
    assert request(db, repo, "missing").status == 404
    ws = workspaces.adopt_root(db, str(repo))
    assert request(db, repo / "elsewhere", ws.id).status == 404
    with db.tx() as c:
        c.execute("UPDATE workspaces SET base_branch=NULL WHERE id=?", (ws.id,))
    assert request(db, repo, ws.id).status == 409
    with db.tx() as c:
        c.execute("UPDATE workspaces SET base_branch='missing-branch' WHERE id=?", (ws.id,))
    assert request(db, repo, ws.id).status == 409

