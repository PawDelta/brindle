import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from brindle import git, workspaces
from brindle.web.server import App, Request, load_routes, match


def request(db, repo, workspace_id):
    load_routes()
    path = f"/api/workspaces/{quote(workspace_id, safe='')}/diff"
    found = match("GET", path)
    assert found is not None
    handler, params = found
    return handler(Request(App(str(repo), None), "GET", path, {}, params, {}, _db=db))


def test_diff_from_worktree_includes_commits_edits_and_is_capped(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feature", fetch=False, run_setup=False).workspace
    source = Path(ws.path) / "app.py"
    source.write_text("committed\n")
    git.commit_all(ws.path, "change")
    source.write_text("committed\nworking\n")
    response = request(db, repo, ws.id)
    assert response.status == 200
    data = json.loads(response.body)
    assert "app.py" in data["stat"] and "2 insertions(+)" in data["stat"]
    assert "+committed\n+working" in data["diff"]
    assert not data["truncated"]
    source.write_text("large\n" * 20000)
    capped = json.loads(request(db, repo, ws.id).body)
    assert capped["truncated"] and len(capped["diff"]) <= 60000
    monkeypatch.setattr("brindle.web.diff.MAX_DIFF_CHARS", 20)
    capped = json.loads(request(db, repo, ws.id).body)
    assert capped["stat_truncated"] and len(capped["stat"]) == 20


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


def test_workspace_click_renders_diff_reviews_and_checks():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the browser client")
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; this.textContent = ''; }
  addEventListener(name, fn) { this[name] = fn; }
  querySelector() { return this.children[0].children.find(c => c.attrs && 'data-diff-patch' in c.attrs); }
}
global.Node = Element;
const panel = new Element('div');
global.document = {createElement: tag => new Element(tag),
  getElementById: () => panel, querySelectorAll: () => []};
global.location = {search: ''};
const pending = [];
global.fetch = url => new Promise(resolve => pending.push({url, resolve}));
global.setTimeout = () => {};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8').replace(/poll\(\);\s*$/, ''));
const data = {name: 'feature', base: 'main', sha: 'abc', stat: 'file | 1 +',
  diff: '+<script>unsafe</script>', reviews: [{sha: 'abc', approved: 1, summary: 'ready'}],
  checks: [{sha: 'old', ok: 0, command: 'lint', output: 'failure'}]};
function text(e) { return typeof e === 'string' ? e : e.children.map(text).join(' '); }
(async () => {
  workspaceBlock({id: 'repo/feature', name: 'feature', agents: []}).children[0].children[0].click();
  assert.equal(pending[0].url, 'api/workspaces/repo%2Ffeature/diff');
  const current = loadDiff();
  pending[1].resolve({ok: true, json: async () => data});
  await current;
  assert(text(panel).includes(data.stat) && text(panel).includes(data.diff));
  assert(text(panel).includes('ready') && text(panel).includes('failure'));
  const patch = panel.children[0].children.find(c => c.attrs && 'data-diff-patch' in c.attrs);
  assert.equal(patch.tag, 'details');
  assert(!('open' in patch.attrs));
  pending[0].resolve({ok: true, json: async () => ({...data, name: 'stale'})});
  await new Promise(resolve => setImmediate(resolve));
  assert(!text(panel).includes('stale'));
  patch.open = true;
  const refreshed = loadDiff(true);
  pending[2].resolve({ok: true, json: async () => ({...data, diff: '+updated'})});
  await refreshed;
  assert(text(panel).includes('+updated') && panel.querySelector().open);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    client = Path(__file__).parents[1] / "src/brindle/web/static/app.js"
    subprocess.run([node, "-e", script, str(client)], check=True, capture_output=True, text=True)
