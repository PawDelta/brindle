"""Workspace changes and the recorded reviews and checks for their commits."""

from dataclasses import asdict
from urllib.parse import unquote

from brindle import git, workspaces
from brindle.web.server import Request, Response, json_response, route

MAX_DIFF_CHARS = 60_000
OUTPUT_TAIL = 4000


@route("GET", "/api/workspaces/{id}/diff")
def workspace_diff(req: Request) -> Response:
    ws = req.db.get_workspace(unquote(req.params["id"]))
    if ws is None or (req.app.repo_root is not None and ws.repo_root != req.app.repo_root):
        return json_response({"error": "Workspace not found"}, 404)
    try:
        base = workspaces.require_base(ws)
        # The same helper as the MCP workspace_diff: merge-base through the
        # working tree, including its summary of untracked files.
        stat = git.diff(ws.path, base, stat=True)
        patch = git.diff(ws.path, base)
        sha = git.out(["rev-parse", "HEAD"], ws.path)
    except (git.GitError, workspaces.WorkspaceError) as e:
        return json_response({"error": str(e)}, 409)
    reviews = [asdict(r) for r in req.db.workspace_reviews(ws.id)]
    checks = []
    for row in req.db.conn.execute(
        "SELECT * FROM check_cache WHERE workspace_id=? ORDER BY created_at", (ws.id,)
    ):
        check = dict(row)
        check["output"] = (check["output"] or "")[-OUTPUT_TAIL:]
        checks.append(check)
    return json_response({
        "workspace_id": ws.id, "name": ws.name, "base": base, "sha": sha,
        "stat": stat[:MAX_DIFF_CHARS], "diff": patch[:MAX_DIFF_CHARS],
        "truncated": len(patch) > MAX_DIFF_CHARS,
        "stat_truncated": len(stat) > MAX_DIFF_CHARS,
        "reviews": reviews, "checks": checks,
    })
