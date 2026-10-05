"""``brindle ci``: goal sources, the headless run (with fakes for tmux, the
supervisor, git push and gh), the entitlement gate, ``brindle ci init``, and
the trust split (``entitle``, ``run --bundle``, ``publish``)."""

import functools
import json
import os
import re
import subprocess
import time
import types

from pathlib import Path

import pytest
from typer.testing import CliRunner

from brindle import autopilot as pilot
from brindle import ci, git, workspaces
from brindle.cli import app
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.pro import auth, credentials
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, backend, claims, pro_env, sign, signing_key,
)


class FakeSession:
    """Stands in for tmux, the supervisor, git push and gh: records what the
    run asks for, and ``script`` plays the supervisor on every poll."""

    def __init__(self, db, monkeypatch, script=None):
        self.db = db
        self.prompts, self.stopped, self.pushed, self.prs = [], [], [], []
        self.alive = True
        self.clock, self.ticks = 1_000.0, 0
        self.script = script or (lambda session, root_id: None)
        self.root_id = None
        monkeypatch.setattr(ci, "_spawn", self.spawn)
        monkeypatch.setattr(ci, "_alive", lambda db, root_id: self.alive)
        monkeypatch.setattr(ci, "_stop", self.stop)
        monkeypatch.setattr(ci, "_push", lambda ws, secrets=None, remote=None: self.pushed.append(ws.branch))
        monkeypatch.setattr(ci, "_create_pr", self.create_pr)

    def spawn(self, db, ws, prompt):
        self.prompts.append(prompt)
        a = Agent("sup1", ws.id, "supervisor", "claude", None, "interactive", "processing", "%9",
                  None, time.time())
        db.add_agent(a)
        db.add_autopilot(a.id)
        self.root_id = a.id
        return a

    def stop(self, db, root_id):
        self.stopped.append(root_id)

    def create_pr(self, ws, base, title, body, secrets=None, remote=None):
        self.prs.append((ws.branch, base, title, body))
        return "https://github.com/o/r/pull/7"

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds
        self.ticks += 1
        self.script(self, self.root_id)


def finish_goal(session, root_id, title="Add health", n=1):
    """The supervisor sets a goal and gets every milestone verified."""
    if session.ticks < n:
        return
    db = session.db
    if not db.milestones(root_id):
        pilot.set_goal(db, root_id, title, [("Endpoint", "uv run pytest -q", None),
                                            ("Docs", None, "README")])
    for m in db.milestones(root_id):
        db.record_check(m.id, True, "ok", "abc1234")


def run(db, repo, goal, session, **kw):
    kw.setdefault("timeout_min", 30)
    return ci.run(db, str(repo), goal, clock=session.now, sleep=session.sleep, **kw)


# -- goal sources ------------------------------------------------------------


def test_goal_from_issue_uses_gh(monkeypatch):
    seen = []

    def gh(args, cwd):
        seen.append(args)
        return {"number": 42, "title": "Add a /health endpoint", "body": "Return 200 with uptime."}

    monkeypatch.setattr(ci, "_gh_json", gh)
    g = ci.goal_from_issue(42, "/repo")
    assert seen == [["issue", "view", "42", "--json", "number,title,body"]]
    assert (g.title, g.detail, g.issue, g.source) == (
        "Add a /health endpoint", "Return 200 with uptime.", 42, "issue")
    assert g.branch == "brindle/ci-42"
    assert g.plan() is None


def test_goal_from_issue_failures(monkeypatch):
    monkeypatch.setattr(ci, "_gh_json", lambda args, cwd: {"number": 3, "title": "", "body": ""})
    with pytest.raises(ci.CIError, match="no title"):
        ci.goal_from_issue(3, "/repo")

    def broken(args, cwd):
        raise ci.CIError("gh issue view failed: not found")

    monkeypatch.setattr(ci, "_gh_json", broken)
    with pytest.raises(ci.CIError, match="not found"):
        ci.goal_from_issue(3, "/repo")


def test_goal_from_text_slug_and_plan():
    g = ci.goal_from_text("Add a /health endpoint\nIt returns uptime.")
    assert (g.title, g.detail, g.issue) == ("Add a /health endpoint", "It returns uptime.", None)
    assert g.branch == "brindle/ci-add-a-health-endpoint"
    assert g.plan() is None

    shaped = ci.goal_from_text("# Settings page\n\n## API\ncheck: uv run pytest tests/test_api.py -q\n")
    assert shaped.title == "Settings page"
    plan = shaped.plan()
    assert plan is not None and plan.milestones == [("API", "uv run pytest tests/test_api.py -q", None)]

    long = ci.goal_from_text("x" * 100)
    assert len(long.branch) <= len(ci.BRANCH_PREFIX) + ci.MAX_SLUG
    with pytest.raises(ci.CIError, match="empty"):
        ci.goal_from_text("  \n")


def test_goal_from_file(tmp_path):
    f = tmp_path / "goals.md"
    f.write_text("# Billing\n\n## Invoices\ncheck: make test-invoices\n", encoding="utf-8")
    g = ci.goal_from_file(f)
    assert (g.title, g.source) == ("Billing", "file")
    assert g.plan().milestones == [("Invoices", "make test-invoices", None)]
    with pytest.raises(ci.CIError, match="cannot read"):
        ci.goal_from_file(tmp_path / "missing.md")


def test_resolve_goal_wants_exactly_one_source(tmp_path):
    with pytest.raises(ci.CIError, match="exactly one"):
        ci.resolve_goal(None, None, None, str(tmp_path))
    with pytest.raises(ci.CIError, match="exactly one"):
        ci.resolve_goal("a", "b", None, str(tmp_path))
    assert ci.resolve_goal("Fix it", None, None, str(tmp_path)).title == "Fix it"


# -- the run --------------------------------------------------------------------


def test_completion_pushes_and_opens_the_pr(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch, script=finish_goal)
    goal = ci.Goal("Add a /health endpoint", "Return 200.", issue=42, source="issue")
    out = run(db, repo, goal, s)

    assert out.status == "done" and out.ok
    assert out.pr_url == "https://github.com/o/r/pull/7"
    assert s.pushed == ["brindle/ci-42"]
    branch, base, title, body = s.prs[0]
    assert (branch, base, title) == ("brindle/ci-42", "main", "Add a /health endpoint")
    assert "Return 200." in body
    assert "- [x] Endpoint (`uv run pytest -q`)" in body and "- [x] Docs" in body
    assert "Closes #42" in body
    assert s.stopped == ["sup1"]
    assert [m["status"] for m in out.milestones] == ["passed", "passed"]
    # The work happened in a worktree on the fresh branch, cut from the base.
    assert git.worktree_for_branch(str(repo), "brindle/ci-42")
    # The supervisor was told it runs unattended, and to derive the milestones.
    assert "unattended" in s.prompts[0] and "issue #42" in s.prompts[0]
    assert "set_goal" in s.prompts[0] and "Return 200." in s.prompts[0]


def test_a_rerun_reuses_the_branch_worktree(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch, script=finish_goal)
    goal = ci.Goal("Add health", issue=42)
    run(db, repo, goal, s)
    first = git.worktree_for_branch(str(repo), "brindle/ci-42")
    s2 = FakeSession(db, monkeypatch, script=finish_goal)
    db.delete_agent("sup1")
    run(db, repo, goal, s2)
    assert git.worktree_for_branch(str(repo), "brindle/ci-42") == first


def test_a_rerun_survives_a_deleted_worktree_folder(db, repo, monkeypatch):
    import shutil
    s = FakeSession(db, monkeypatch, script=finish_goal)
    goal = ci.Goal("Add health", issue=42)
    run(db, repo, goal, s)
    first = git.worktree_for_branch(str(repo), "brindle/ci-42")
    tip = git.out(["rev-parse", "brindle/ci-42"], str(repo))
    shutil.rmtree(first)                      # a wiped brindle home or a cleaned runner
    db.delete_agent("sup1")
    run(db, repo, goal, FakeSession(db, monkeypatch, script=finish_goal))
    again = git.worktree_for_branch(str(repo), "brindle/ci-42")
    assert again and Path(again).is_dir()
    assert git.ok(["merge-base", "--is-ancestor", tip, "brindle/ci-42"], str(repo))


def test_shaped_goal_records_the_milestones_up_front(db, repo, monkeypatch):
    def verify(session, root_id):
        for m in session.db.milestones(root_id):
            session.db.record_check(m.id, True, "ok")

    s = FakeSession(db, monkeypatch, script=verify)
    goal = ci.goal_from_text("# Settings\n\nUsers edit their name.\n\n## API\ncheck: make test-api\n"
                             "## UI\ncheck: make test-ui\n")
    out = run(db, repo, goal, s, pr=False)
    assert out.status == "done" and out.ok and out.pr_url is None
    assert [m["title"] for m in out.milestones] == ["API", "UI"]
    assert db.get_autopilot("sup1").goal == "Settings"
    assert "already recorded" in s.prompts[0] and "make test-api" not in s.prompts[0]
    assert s.pushed == [] and s.prs == []
    body = ci.pr_body(out)
    assert "- [x] API (`make test-api`)" in body and "## API" not in body


def test_need_user_fails_with_the_question(db, repo, monkeypatch):
    def ask(session, root_id):
        if session.ticks == 1:
            pilot.set_goal(session.db, root_id, "Add health", [("Endpoint", "make test", None)])
        if session.ticks == 2:
            pilot.need_user(session.db, root_id, "Postgres or SQLite for the store?")

    s = FakeSession(db, monkeypatch, script=ask)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "need_user" and not out.ok
    assert "Postgres or SQLite" in out.note
    assert s.pushed == [] and s.prs == []
    assert s.stopped == ["sup1"]
    assert out.milestones[0]["status"] == "pending"
    assert "Postgres or SQLite" in out.describe()


def test_stalled_and_usage_paused_fail(db, repo, monkeypatch):
    def stall(session, root_id):
        session.db.update_autopilot(root_id, state="stalled", note="no progress after 3 reminders")

    s = FakeSession(db, monkeypatch, script=stall)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert (out.status, out.note) == ("stalled", "no progress after 3 reminders")

    def paused(session, root_id):
        session.db.update_autopilot(root_id, state="usage_paused")

    db.delete_agent("sup1")
    s = FakeSession(db, monkeypatch, script=paused)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "stalled" and "usage" in out.note
    assert s.stopped == ["sup1"]


def test_timeout_is_non_zero_and_stops_the_session(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch)
    out = run(db, repo, ci.Goal("Add health"), s, timeout_min=1)
    assert out.status == "timeout" and not out.ok
    assert "1 minutes" in out.note
    assert out.elapsed == pytest.approx(60)
    assert s.pushed == [] and s.prs == [] and s.stopped == ["sup1"]
    assert s.ticks == 6   # polled every 10 s, never past the deadline


def test_supervisor_exit_fails(db, repo, monkeypatch):
    def die(session, root_id):
        session.alive = False

    s = FakeSession(db, monkeypatch, script=die)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "exited" and "exited" in out.note
    assert s.stopped == ["sup1"]


def test_cleanup_runs_when_the_run_itself_breaks(db, repo, monkeypatch):
    def boom(session, root_id):
        raise RuntimeError("db went away")

    s = FakeSession(db, monkeypatch, script=boom)
    with pytest.raises(RuntimeError, match="db went away"):
        run(db, repo, ci.Goal("Add health"), s)
    assert s.stopped == ["sup1"]


def test_pr_failure_is_reported_as_a_failure(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch, script=finish_goal)

    def refuse(ws, base, title, body, secrets=None, remote=None):
        raise ci.CIError("gh pr create failed: no commits between main and brindle/ci-add-health")

    monkeypatch.setattr(ci, "_create_pr", refuse)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "done" and not out.ok and out.pr_url is None
    assert "no commits" in out.note
    assert s.pushed == ["brindle/ci-add-health"]


def test_base_and_max_workers(db, repo, monkeypatch):
    from conftest import sh

    sh("git branch develop && git push -q origin develop", repo)
    s = FakeSession(db, monkeypatch, script=finish_goal)
    out = run(db, repo, ci.Goal("Add health"), s, base="develop", max_workers=2)
    assert out.ok and s.prs[0][1] == "develop"
    assert load_repo_config(str(repo)).max_agents == 2
    assert (repo / ".brindle" / ".gitignore").read_text() == "config.local.json\n"
    # Other local keys are kept, and the cap can change.
    local = repo / ".brindle" / "config.local.json"
    local.write_text(json.dumps({"max_agents": 2, "sidebar": "bottom"}))
    ci.set_max_workers(str(repo), 3)
    assert json.loads(local.read_text()) == {"max_agents": 3, "sidebar": "bottom"}


def test_step_summary_is_json(db, repo, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    s = FakeSession(db, monkeypatch, script=finish_goal)
    out = run(db, repo, ci.Goal("Add health", issue=5), s)
    ci.write_step_summary(out)
    text = summary.read_text()
    assert text.startswith("earlier step\n## brindle ci: done")
    data = json.loads(text.split("```json\n", 1)[1].split("```")[0])
    assert data["ok"] is True and data["status"] == "done"
    assert data["issue"] == 5 and data["branch"] == "brindle/ci-5"
    assert data["pr_url"] == "https://github.com/o/r/pull/7"
    assert [m["status"] for m in data["milestones"]] == ["passed", "passed"]
    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    assert ci.write_step_summary(out) is None


# -- the entitlement gate ----------------------------------------------------------


def test_ci_token_from_env_is_exchanged_in_memory(backend, monkeypatch, brindle_home):
    client = auth.Client(BASE, transport=backend)
    token = backend.issue_ci_token()
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", token)
    ent = ci.require_ci(client)
    assert "ci" in ent.features and ent.plan == "team" and ent.org_id == "org_team"
    assert ent.sub == "ci:ct_1" and ent.role == "member"
    assert backend.paths() == ["POST /ci/entitlement"]
    assert backend.calls[0][2]["Authorization"] == "Bearer " + token
    # Nothing was written: no credentials, no file under the home.
    assert credentials.default_store().load() is None
    assert not (brindle_home / "pro").exists()


def test_ci_token_works_on_every_run(backend, monkeypatch):
    """Unlike a refresh token, the CI token doesn't rotate: the same secret keeps working."""
    client = auth.Client(BASE, transport=backend)
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", backend.issue_ci_token())
    for _ in range(3):
        assert "ci" in ci.require_ci(client).features
    assert backend.paths() == ["POST /ci/entitlement"] * 3


def test_revoked_ci_token_is_refused_without_leaking_it(backend, monkeypatch):
    client = auth.Client(BASE, transport=backend)
    token = backend.issue_ci_token()
    backend.ci_tokens[token] = "revoked"
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", token)
    with pytest.raises(ci.CIError) as e:
        ci.require_ci(client)
    assert "invalid_token" in str(e.value) and token not in str(e.value)
    assert "ci-token create" in str(e.value)


def test_ci_token_without_ci_feature_is_refused(backend, monkeypatch):
    client = auth.Client(BASE, transport=backend)
    backend.ci_features = ["learning", "services"]
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", backend.issue_ci_token())
    with pytest.raises(ci.CIError, match="brindle Team"):
        ci.require_ci(client)


def test_plan_lapsed_is_refused(backend, monkeypatch):
    client = auth.Client(BASE, transport=backend)
    backend.routes["POST /ci/entitlement"] = [(403, {"error": "entitlement_required"})]
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", "cpc_" + "x" * 43)
    with pytest.raises(ci.CIError, match="entitlement_required"):
        ci.require_ci(client)


def test_forged_entitlement_is_refused(backend, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    client = auth.Client(BASE, transport=backend)
    forged = sign(Ed25519PrivateKey.generate(), claims(plan="team", features=["ci"]))
    backend.routes["POST /ci/entitlement"] = [(200, {"entitlement": forged})]
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", "cpc_" + "x" * 43)
    with pytest.raises(ci.CIError, match="signature"):
        ci.require_ci(client)


def test_refresh_token_in_env_is_refused_without_a_call(backend, monkeypatch):
    """A refresh token would work once (the backend rotates it), so it isn't sent at all."""
    client = auth.Client(BASE, transport=backend)
    refresh = backend.issue()["refresh_token"]
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", refresh)
    with pytest.raises(ci.CIError) as e:
        ci.require_ci(client)
    assert "not_a_ci_token" in str(e.value) and refresh not in str(e.value)
    assert backend.paths() == []


def test_not_logged_in_says_how(monkeypatch):
    monkeypatch.delenv("BRINDLE_PRO_TOKEN", raising=False)
    with pytest.raises(ci.CIError, match="BRINDLE_PRO_TOKEN"):
        ci.require_ci()


# -- the CLI -------------------------------------------------------------------------


def test_cli_run_is_refused_when_not_entitled(repo, monkeypatch):
    monkeypatch.delenv("BRINDLE_PRO_TOKEN", raising=False)
    monkeypatch.chdir(repo)
    spawned = []
    monkeypatch.setattr(ci, "_spawn", lambda db, ws, prompt: spawned.append(prompt))
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Add health"])
    assert res.exit_code == 1, res.output
    assert "brindle ci" in res.output and "BRINDLE_PRO_TOKEN" in res.output
    assert spawned == []


def test_cli_run_wants_one_goal_source(repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ci", "run"])
    assert res.exit_code == 1 and "exactly one" in res.output
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "a", "--issue", "1"])
    assert res.exit_code == 1 and "exactly one" in res.output


def test_cli_run_end_to_end(db, repo, monkeypatch, tmp_path):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci, "require_ci", lambda client=None: None)
    monkeypatch.setattr(ci, "_gh_json", lambda args, cwd: {"number": 9, "title": "Add health", "body": ""})
    s = FakeSession(db, monkeypatch, script=finish_goal)
    monkeypatch.setattr(ci, "run", functools.partial(ci.run, clock=s.now, sleep=s.sleep))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    res = CliRunner().invoke(app, ["ci", "run", "--issue", "9", "--timeout", "5"])
    assert res.exit_code == 0, res.output
    assert res.output.rstrip().endswith("https://github.com/o/r/pull/7")
    assert "2 of 2 milestones verified" in res.output
    assert s.prs[0][0] == "brindle/ci-9" and "Closes #9" in s.prs[0][3]
    assert '"status": "done"' in summary.read_text()

    # The failing path exits 1 and says why.
    db.delete_agent("sup1")
    s2 = FakeSession(db, monkeypatch)
    monkeypatch.setattr(ci, "run", functools.partial(ci.run, clock=s2.now, sleep=s2.sleep))
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Other thing", "--timeout", "1", "--no-pr"])
    assert res.exit_code == 1, res.output
    assert "timeout" in res.output and s2.stopped == ["sup1"]


# -- brindle ci init -------------------------------------------------------------------


def test_init_writes_a_workflow_and_respects_force(repo):
    path = ci.init(repo)
    assert path == repo / ".github" / "workflows" / "brindle.yml"
    text = path.read_text()
    assert "name: brindle" in text
    assert "  issues:\n    types: [labeled]" in text
    assert "  workflow_dispatch:" in text
    assert "github.event.label.name == 'brindle'" in text
    assert "apt-get install -yq tmux" in text
    assert "uv tool install brindle" in text
    assert "npm install -g @anthropic-ai/claude-code" in text
    assert "BRINDLE_PRO_TOKEN: ${{ secrets.BRINDLE_PRO_TOKEN }}" in text
    assert "brindle account org ci-token create" in text
    assert "trust with write access" in text and "GITHUB_TOKEN don't trigger" in text
    assert "brindle ci run --issue ${{ github.event.issue.number || inputs.issue }}" in text
    assert "{{{{" not in text and "}}}}" not in text
    # Every line is a YAML-shaped line: a comment, blank, or `key:` / `- item` text.
    for line in text.splitlines():
        assert not line.strip() or line.lstrip().startswith(("#", "-")) or ":" in line or \
            line.startswith("          "), line

    with pytest.raises(ci.CIError, match="--force"):
        ci.init(repo)
    assert "'brindle'" in path.read_text()
    ci.init(repo, label="agent", force=True)
    assert "github.event.label.name == 'agent'" in path.read_text()
    with pytest.raises(ci.CIError, match="label"):
        ci.init(repo, label="bad\nlabel", force=True)


def test_cli_init(repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ci", "init"])
    assert res.exit_code == 0, res.output
    assert "wrote" in res.output and (repo / ".github" / "workflows" / "brindle.yml").exists()
    res = CliRunner().invoke(app, ["ci", "init"])
    assert res.exit_code == 1 and "--force" in res.output
    res = CliRunner().invoke(app, ["ci", "init", "--force", "--label", "brindle-please"])
    assert res.exit_code == 0, res.output
    assert "brindle-please" in (repo / ".github" / "workflows" / "brindle.yml").read_text()


def test_real_seams_exist():
    """The fakes replace real functions with these names and shapes."""
    for name in ("_spawn", "_alive", "_stop", "_push", "_create_pr", "_gh_json", "_checkout"):
        assert callable(getattr(ci, name)), name
    assert workspaces.create and pilot.set_goal


# -- secrets are withheld from agents ------------------------------------------


def test_withhold_secrets_removes_tokens_and_keeps_the_model_key():
    env = {"BRINDLE_PRO_TOKEN": "cpc_x", "GH_TOKEN": "ghs_x", "GITHUB_TOKEN": "ghs_y",
           "ANTHROPIC_API_KEY": "sk-ant-x", "PATH": "/bin"}
    taken = ci.withhold_secrets(env)
    assert taken == {"BRINDLE_PRO_TOKEN": "cpc_x", "GH_TOKEN": "ghs_x", "GITHUB_TOKEN": "ghs_y"}
    assert env == {"ANTHROPIC_API_KEY": "sk-ant-x", "PATH": "/bin"}


def _recording_run(seen, real=subprocess.run):
    def fake(cmd, **kw):
        seen.append((cmd, kw.get("env") or {}, kw.get("cwd")))
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r/pull/1\n", "")
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, **kw)
    return fake


def test_push_goes_through_a_fresh_bare_repo_not_the_agents_worktree(monkeypatch, repo):
    # The agents' worktree plants a pre-push hook and repoints origin.
    hooks = Path(repo) / ".git" / "hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\necho stolen > /tmp/brindle-test-stolen\n")
    (hooks / "pre-push").chmod(0o755)
    subprocess.run(["git", "remote", "add", "origin", "https://evil.example/x.git"], cwd=repo, check=False)
    subprocess.run(["git", "checkout", "-q", "-b", "brindle/ci-x"], cwd=repo, check=True)
    seen = []
    monkeypatch.setattr(ci.subprocess, "run", _recording_run(seen))
    ws = types.SimpleNamespace(path=str(repo), branch="brindle/ci-x", repo_root=str(repo))
    ci._push(ws, {"GH_TOKEN": "ghs_secret", "BRINDLE_PRO_TOKEN": "cpc_secret"},
             "https://github.com/acme/app")
    push = next(cmd for cmd, _, _ in seen if "push" in cmd)
    assert push[-2] == "https://github.com/acme/app.git" and "--no-verify" in push
    assert "core.hooksPath=/dev/null" in push and str(repo) not in push[2]
    fetch = next(cmd for cmd, _, _ in seen if "fetch" in cmd)
    assert fetch[2].endswith("push.git")   # a bare repo brindle made, not the worktree
    for _, env, _ in seen:
        assert env.get("GH_TOKEN") == "ghs_secret" and "BRINDLE_PRO_TOKEN" not in env


def test_pr_names_the_repo_and_never_runs_in_the_worktree(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ci.subprocess, "run", _recording_run(seen))
    ws = types.SimpleNamespace(path=str(tmp_path), branch="brindle/ci-x", repo_root=str(tmp_path))
    url = ci._create_pr(ws, "main", "t", "b", {"GH_TOKEN": "ghs_secret", "BRINDLE_PRO_TOKEN": "cpc_s"},
                        "https://github.com/acme/app")
    cmd, env, cwd = seen[0]
    assert url.endswith("/pull/1") and cmd[cmd.index("--repo") + 1] == "acme/app"
    assert cwd != str(tmp_path) and "BRINDLE_PRO_TOKEN" not in env
    assert all("ghs_secret" not in " ".join(c) for c, _, _ in seen)


def test_no_push_or_pr_without_a_github_origin(tmp_path):
    ws = types.SimpleNamespace(path=str(tmp_path), branch="b", repo_root=str(tmp_path))
    with pytest.raises(ci.CIError):
        ci._push(ws, {"GH_TOKEN": "x"}, "https://evil.example/acme/app")
    with pytest.raises(ci.CIError):
        ci._create_pr(ws, "main", "t", "b", {"GH_TOKEN": "x"}, None)


def test_run_withholds_secrets_even_when_called_directly(db, repo, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghs_direct")
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", "cpc_direct")
    seen_env = {}

    def spawn(db_, ws, prompt):
        seen_env.update(os.environ)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ci, "_spawn", spawn)
    with pytest.raises(RuntimeError):
        ci.run(db, str(repo), ci.goal_from_text("x"))
    assert "GH_TOKEN" not in seen_env and "BRINDLE_PRO_TOKEN" not in seen_env


def test_the_workflow_does_not_persist_checkout_credentials(tmp_path):
    assert "persist-credentials: false" in ci.workflow_text()


# -- the trust split: entitle, run --bundle, publish ---------------------------


def test_entitle_writes_the_entitlement_and_run_needs_no_token(backend, monkeypatch, tmp_path):
    client = auth.Client(BASE, transport=backend)
    token = backend.issue_ci_token()
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", token)
    out = ci.entitle(tmp_path / "sub" / "ent.jwt", client)
    assert out.stat().st_mode & 0o777 == 0o600
    assert token not in out.read_text() and out.read_text().count(".") == 2
    # An existing, world-readable file is replaced, not written through.
    out.chmod(0o644)
    ci.entitle(out, client)
    assert out.stat().st_mode & 0o777 == 0o600

    # The run step: no token in the environment, no call to the backend.
    monkeypatch.delenv("BRINDLE_PRO_TOKEN")
    calls = len(backend.calls)
    ent = ci.require_ci(client, entitlement_file=out)
    assert "ci" in ent.features and ent.plan == "team"
    assert len(backend.calls) == calls


def test_entitle_failures(backend, monkeypatch, tmp_path):
    client = auth.Client(BASE, transport=backend)
    monkeypatch.delenv("BRINDLE_PRO_TOKEN", raising=False)
    with pytest.raises(ci.CIError, match="BRINDLE_PRO_TOKEN"):
        ci.entitle(tmp_path / "ent.jwt", client)
    token = backend.issue_ci_token()
    backend.ci_tokens[token] = "revoked"
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", token)
    with pytest.raises(ci.CIError) as e:
        ci.entitle(tmp_path / "ent.jwt", client)
    assert "invalid_token" in str(e.value) and token not in str(e.value)
    backend.ci_features = ["learning"]
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", backend.issue_ci_token())
    with pytest.raises(ci.CIError, match="brindle Team"):
        ci.entitle(tmp_path / "ent.jwt", client)
    assert not (tmp_path / "ent.jwt").exists()


def test_entitlement_file_is_verified_like_a_fresh_one(signing_key, tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    f = tmp_path / "ent.jwt"
    with pytest.raises(ci.CIError, match="cannot read"):
        ci.require_ci(entitlement_file=f)
    f.write_text(sign(Ed25519PrivateKey.generate(), claims(plan="team", features=["ci"])))
    with pytest.raises(ci.CIError, match="signature"):
        ci.require_ci(entitlement_file=f)
    now = int(time.time())
    f.write_text(sign(signing_key, claims(plan="team", features=["ci"], iat=now - 7200, exp=now - 3600)))
    with pytest.raises(ci.CIError, match="refused"):   # expired: no offline grace in CI
        ci.require_ci(entitlement_file=f)
    f.write_text(sign(signing_key, claims(plan="pro", features=["learning"])))
    with pytest.raises(ci.CIError, match="brindle Team"):
        ci.require_ci(entitlement_file=f)
    f.write_text(sign(signing_key, claims(plan="team", features=["ci"])) + "\n")
    assert "ci" in ci.require_ci(entitlement_file=f).features


def _working_supervisor(repo, branch, text="def health():\n    return 200\n"):
    """A supervisor that commits to the run's branch, then gets the goal verified."""
    from conftest import sh

    def script(session, root_id):
        wt = Path(git.worktree_for_branch(str(repo), branch))
        if not (wt / "health.py").exists():
            (wt / "health.py").write_text(text)
            sh("git add -A && git commit -qm 'Add health'", wt)
        finish_goal(session, root_id)

    return script


def _bundled_run(db, repo, monkeypatch, tmp_path, goal=None):
    goal = goal or ci.Goal("Add a /health endpoint", "Return 200.", issue=42, source="issue")
    s = FakeSession(db, monkeypatch, script=_working_supervisor(repo, goal.branch))
    bundle = tmp_path / "out" / "brindle.bundle"
    out = run(db, repo, goal, s, bundle=bundle)
    return s, out, bundle


def test_bundle_round_trip_into_a_bare_origin(db, repo, monkeypatch, tmp_path):
    from conftest import sh

    origin = tmp_path / "origin.git"
    s, out, bundle = _bundled_run(db, repo, monkeypatch, tmp_path)

    # The run pushed nothing and opened nothing: it only wrote the two files.
    assert out.ok and out.pr_url is None and out.bundle == str(bundle.resolve())
    assert s.pushed == [] and s.prs == []
    assert "brindle/ci-42" not in sh("git branch --list 'brindle/*'", origin)
    assert out.summary()["bundle"] == out.bundle and "brindle ci publish" in out.describe()
    meta = json.loads(Path(str(bundle) + ".json").read_text())
    assert set(meta) == {"branch", "base", "title", "body", "status"}
    assert (meta["branch"], meta["base"], meta["title"], meta["status"]) == (
        "brindle/ci-42", "main", "Add a /health endpoint", "done")
    assert "Closes #42" in meta["body"] and "- [x] Endpoint" in meta["body"]
    assert "Built in parallel" in meta["body"]     # the footer, from the repo's config

    # Publishing, somewhere else: only the bundle and its JSON are needed.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for f in (bundle, Path(str(bundle) + ".json")):
        (elsewhere / f.name).write_bytes(f.read_bytes())
    prs = []

    def open_pr(slug, base, branch, title, body, env):
        prs.append((slug, base, branch, title, body))
        return "https://github.com/acme/app/pull/3"

    monkeypatch.setattr(ci, "_open_pr", open_pr)
    main_before = sh("git rev-parse refs/heads/main", origin)
    url = ci.publish(elsewhere / "brindle.bundle", "acme/app", remote=str(origin))
    assert url == "https://github.com/acme/app/pull/3"
    assert prs == [("acme/app", "main", "brindle/ci-42", "Add a /health endpoint", meta["body"])]
    wt = git.worktree_for_branch(str(repo), "brindle/ci-42")
    assert sh("git rev-parse refs/heads/brindle/ci-42", origin) == sh("git rev-parse HEAD", wt)
    assert "return 200" in sh("git show refs/heads/brindle/ci-42:health.py", origin)
    assert sh("git rev-parse refs/heads/main", origin) == main_before


def test_publish_runs_git_outside_any_worktree_and_takes_the_repo_from_the_environment(
        db, repo, monkeypatch, tmp_path):
    _, _, bundle = _bundled_run(db, repo, monkeypatch, tmp_path)
    wt = git.worktree_for_branch(str(repo), "brindle/ci-42")
    seen = []
    record, real = _recording_run(seen), subprocess.run
    github, origin = "https://github.com/acme/app.git", str(tmp_path / "origin.git")

    def no_network(cmd, **kw):
        """Records the command as written; the base comes from the local origin instead."""
        if github in cmd and "fetch" in cmd:
            seen.append((cmd, kw.get("env") or {}, kw.get("cwd")))
            return real([origin if a == github else a for a in cmd], **kw)
        return record(cmd, **kw)

    monkeypatch.setattr(ci.subprocess, "run", no_network)
    url = ci.publish(bundle, base="main", environ={**os.environ, "GITHUB_REPOSITORY": "acme/app",
                                                   "GH_TOKEN": "ghs_write"})
    assert url.endswith("/pull/1")
    push = next(cmd for cmd, _, _ in seen if "push" in cmd)
    assert push[-2] == "https://github.com/acme/app.git"
    assert push[-1] == "refs/heads/brindle/ci-42:refs/heads/brindle/ci-42"   # not forced
    assert "--no-verify" in push and "core.hooksPath=/dev/null" in push
    gh = next(cmd for cmd, _, _ in seen if cmd[:2] == ["gh", "pr"])
    assert gh[gh.index("--repo") + 1] == "acme/app" and gh[gh.index("--head") + 1] == "brindle/ci-42"
    for cmd, env, cwd in seen:
        assert cwd and not str(cwd).startswith((str(repo), wt)), cmd
        assert "ghs_write" not in " ".join(cmd)
    # The bundle is verified before anything is taken from it.
    names = [next(w for w in ("init", "verify", "fetch", "push", "pr") if w in cmd) for cmd, _, _ in seen]
    assert names == ["init", "fetch", "verify", "fetch", "push", "pr"]


def test_publish_refuses_what_it_should_not_push(db, repo, monkeypatch, tmp_path):
    origin = tmp_path / "origin.git"
    _, _, bundle = _bundled_run(db, repo, monkeypatch, tmp_path)
    meta_path = Path(str(bundle) + ".json")
    good = json.loads(meta_path.read_text())
    opened = []
    monkeypatch.setattr(ci, "_open_pr", lambda *a: opened.append(a) or "url")

    def publish_with(**over):
        meta_path.write_text(json.dumps({**good, **over}))
        return ci.publish(bundle, "acme/app", remote=str(origin))

    for over, why in [({"branch": "main"}, "brindle/ci-"),
                      ({"branch": "brindle/ci-42:refs/heads/main"}, "brindle/ci-"),
                      ({"branch": "brindle/ci-../../main"}, "brindle/ci-"),
                      ({"branch": "--force"}, "brindle/ci-"),
                      ({"base": "--upload-pack=evil"}, "base"),
                      ({"base": "brindle/ci-42"}, "base"),
                      ({"status": "timeout"}, "status"),
                      ({"title": ""}, "title"),
                      ({"body": None}, "body")]:
        with pytest.raises(ci.CIError, match=why):
            publish_with(**over)
    meta_path.write_text("[]")
    with pytest.raises(ci.CIError, match="JSON object"):
        ci.publish(bundle, "acme/app", remote=str(origin))
    meta_path.write_text(json.dumps(good))

    with pytest.raises(ci.CIError, match="--repo"):
        ci.publish(bundle, None, remote=str(origin), environ={})
    with pytest.raises(ci.CIError, match="--repo"):
        ci.publish(bundle, "https://evil.example/x", remote=str(origin))
    with pytest.raises(ci.CIError, match="no bundle"):
        ci.publish(tmp_path / "missing.bundle", "acme/app", remote=str(origin))

    # A bundle that isn't one, and one whose base the repository doesn't have.
    real = bundle.read_bytes()
    bundle.write_bytes(b"not a bundle\n")
    with pytest.raises(ci.CIError, match="bundle verify"):
        ci.publish(bundle, "acme/app", remote=str(origin))
    bundle.write_bytes(real)
    with pytest.raises(ci.CIError, match="base branch"):
        ci.publish(bundle, "acme/app", base="release", remote=str(origin))
    with pytest.raises(ci.CIError, match="--base"):
        ci.publish(bundle, "acme/app", base="--upload-pack=evil", remote=str(origin))
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(empty)], check=True)
    other = subprocess.run(
        ["git", "--git-dir", str(empty), "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
         "-m", "other", "4b825dc642cb6eb9a060e54bf8d69288fbee4904"],
        check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "--git-dir", str(empty), "update-ref", "refs/heads/main", other], check=True)
    meta_path.write_text(json.dumps(good))
    with pytest.raises(ci.CIError, match="bundle verify"):
        ci.publish(bundle, "acme/app", remote=str(empty))

    assert opened == []
    assert "brindle/ci-42" not in subprocess.run(["git", "--git-dir", str(origin), "branch", "--list"],
                                               capture_output=True, text=True).stdout


def test_bundle_failure_is_a_failed_run(db, repo, monkeypatch, tmp_path):
    """Nothing committed past the base: there is nothing to bundle, and the run says so."""
    s = FakeSession(db, monkeypatch, script=finish_goal)
    bundle = tmp_path / "brindle.bundle"
    out = run(db, repo, ci.Goal("Add health"), s, bundle=bundle)
    assert out.status == "done" and not out.ok and out.bundle is None and out.note
    assert not Path(str(bundle) + ".json").exists()
    assert s.pushed == [] and s.prs == []


def test_cli_run_with_entitlement_and_bundle_then_publish(db, repo, monkeypatch, tmp_path, signing_key):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("BRINDLE_PRO_TOKEN", raising=False)
    ent = tmp_path / "ent.jwt"
    ent.write_text(sign(signing_key, claims(plan="team", features=["ci"])))
    s = FakeSession(db, monkeypatch, script=_working_supervisor(repo, "brindle/ci-add-health"))
    monkeypatch.setattr(ci, "run", functools.partial(ci.run, clock=s.now, sleep=s.sleep))
    bundle = tmp_path / "out" / "brindle.bundle"

    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Add health", "--timeout", "5",
                                   "--entitlement", str(ent), "--bundle", str(bundle)])
    assert res.exit_code == 0, res.output
    assert bundle.exists() and "brindle ci publish" in res.output
    assert s.pushed == [] and s.prs == []      # --bundle implies --no-pr

    # A refused entitlement stops the run before anything is spawned.
    ent.write_text("not.a.jwt")
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Other", "--entitlement", str(ent)])
    assert res.exit_code == 1 and "refused" in res.output and len(s.prompts) == 1

    seen = []
    monkeypatch.setattr(ci, "publish", lambda path, repo=None, base=None: seen.append((path, repo, base))
                        or "https://github.com/o/r/pull/9")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    res = CliRunner().invoke(app, ["ci", "publish", str(bundle), "--repo", "o/r", "--base", "main"])
    assert res.exit_code == 0 and res.output.strip() == "https://github.com/o/r/pull/9"
    assert seen == [(str(bundle), "o/r", "main")] and "pull/9" in summary.read_text()

    def refuse(path, repo=None, base=None):
        raise ci.CIError("brindle ci publish: git bundle verify failed: bad")

    monkeypatch.setattr(ci, "publish", refuse)
    res = CliRunner().invoke(app, ["ci", "publish", str(bundle)])
    assert res.exit_code == 1 and "bundle verify" in res.output


def test_cli_entitle(backend, monkeypatch, tmp_path):
    client = auth.Client(BASE, transport=backend)
    monkeypatch.setattr(auth, "Client", lambda *a, **k: client)
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", backend.issue_ci_token())
    out = tmp_path / "ent.jwt"
    res = CliRunner().invoke(app, ["ci", "entitle", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "wrote" in res.output and "cpc_" not in res.output
    assert "ci" in ci.require_ci(entitlement_file=out).features
    monkeypatch.delenv("BRINDLE_PRO_TOKEN")
    res = CliRunner().invoke(app, ["ci", "entitle", "--out", str(out)])
    assert res.exit_code == 1 and "BRINDLE_PRO_TOKEN" in res.output


def _job(text, name):
    """The lines of one job in the workflow."""
    jobs = text.split("\njobs:\n", 1)[1]
    start = jobs.index(f"  {name}:\n")
    nxt = [m.start() for m in re.finditer(r"(?m)^  \w+:\n", jobs) if m.start() > start]
    return jobs[start:nxt[0] if nxt else len(jobs)]


def test_the_workflow_keeps_the_push_token_away_from_the_agents():
    text = ci.workflow_text()
    assert "\npermissions: {}\n" in text       # nothing unless a job asks for it
    entitle_job, run_job, publish_job = _job(text, "entitle"), _job(text, "run"), _job(text, "publish")

    # The CI token: only in its own job, which checks out and runs nothing from the repo.
    assert "BRINDLE_PRO_TOKEN" in entitle_job and "brindle ci entitle --out" in entitle_job
    assert "actions/checkout" not in entitle_job and "claude-code" not in entitle_job
    assert "permissions: {}" in entitle_job and "retention-days: 1" in entitle_job
    assert text.count("secrets.BRINDLE_PRO_TOKEN") == 1

    # The job where agents run: read-only, the model key, and the entitlement file only.
    assert "needs: entitle" in run_job
    assert "permissions:\n      contents: read\n      issues: read\n" in run_job
    assert "write" not in run_job.replace("write access", "")
    assert "secrets.GITHUB_TOKEN" not in run_job and "BRINDLE_PRO_TOKEN" not in run_job
    download = run_job.index("actions/download-artifact")
    work = run_job.index("brindle ci run --issue")
    upload = run_job.index("actions/upload-artifact")
    assert download < work < upload
    run_step = next(s for s in run_job.split("\n      - ") if "brindle ci run --issue" in s)
    assert "ANTHROPIC_API_KEY" in run_step and "GH_TOKEN: ${{ github.token }}" in run_step
    assert "--entitlement" in run_step and "--bundle" in run_step

    # The job that can push: after the run, on its own machine, with no checkout,
    # and the pull request's base comes from the repository, not the bundle.
    assert "needs: run" in publish_job
    assert "permissions:\n      contents: write\n      pull-requests: write\n" in publish_job
    assert "actions/checkout" not in publish_job and "claude-code" not in publish_job
    assert "actions/download-artifact" in publish_job and "brindle ci publish" in publish_job
    assert "--base \"${{ github.event.repository.default_branch }}\"" in publish_job
    assert "BRINDLE_PRO_TOKEN" not in publish_job and "ANTHROPIC_API_KEY" not in publish_job


def test_the_workflow_pins_the_brindle_that_wrote_it(monkeypatch):
    import brindle
    monkeypatch.setattr(brindle, "__version__", "1.2.3")
    text = ci.workflow_text()
    assert text.count("uv tool install brindle==1.2.3") == 3 and "install brindle\n" not in text


def test_publish_takes_the_base_from_the_repository_not_the_bundle(db, repo, monkeypatch, tmp_path):
    origin = tmp_path / "origin.git"
    _, _, bundle = _bundled_run(db, repo, monkeypatch, tmp_path)
    meta_path = Path(str(bundle) + ".json")
    meta = json.loads(meta_path.read_text())
    meta_path.write_text(json.dumps({**meta, "base": "release"}))   # the run asks for another base
    opened = []
    monkeypatch.setattr(ci, "_open_pr", lambda slug, base, *a: opened.append(base) or "url")
    ci.publish(bundle, "acme/app", remote=str(origin))
    assert opened == ["main"]


def test_the_entitlement_file_is_gone_before_agents_start(tmp_path, signing_key):
    ent = tmp_path / "ent.jwt"
    ent.write_text(sign(signing_key, claims(plan="team", features=["ci"])))
    ci.require_ci(entitlement_file=ent)
    assert not ent.exists()

def test_file_remotes_are_for_tests_only():
    """`remote` is a keyword of ci.publish alone: not a CLI option, not a config key."""
    import inspect

    from brindle import cli, config

    assert "remote" not in inspect.signature(ci.publish_cli).parameters
    assert "remote" not in inspect.signature(cli.ci_publish).parameters
    assert "ci.publish" not in inspect.getsource(config)
