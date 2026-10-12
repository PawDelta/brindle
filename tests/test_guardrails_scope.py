"""Guardrails (brindle Pro, feature ``guardrails``): a profile's write scope,
read scope and env allowlist, and what happens without the feature."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import sh
from brindle import gates, guardrails, rule_checks, secrets, tmux, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.native import Toolbox, core_tools
from brindle.pro import license
from brindle.profiles import load_profile

SCOPED = ("---\nname: scoped\nextends: developer\nwrite_scope: [src/api/**, tests/api/]\n"
          "read_scope:\n  - src/**\n  - tests/**\nenv_allow: [NPM_TOKEN, AWS_*]\n---\nScoped.\n")


@pytest.fixture
def entitled(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "guardrails")
    monkeypatch.setattr(guardrails, "_warned", set())


@pytest.fixture
def profiles(repo):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "scoped.md").write_text(SCOPED)
    return d


def add_worker(db, ws, profile, agent_id="w1", provider="claude"):
    a = Agent(agent_id, ws.id, profile, provider, "boss", "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    return a


# -- the profile keys --------------------------------------------------------------------------


def test_a_broken_license_check_fails_closed(repo, profiles, monkeypatch):
    def boom(feature):
        raise RuntimeError("keychain")
    monkeypatch.setattr(license, "has", boom)
    monkeypatch.setattr(guardrails, "_warned", set())
    assert guardrails.effective(load_profile("scoped", str(repo))).write_scope is None


# -- matching ----------------------------------------------------------------------------------


def test_a_relative_glob_never_matches_a_path_outside_the_worktree(tmp_path):
    """``*`` crosses ``/``, so ``*.md`` would match /etc/x.md; outside the
    worktree only an absolute glob applies."""
    assert not guardrails.in_scope("/etc/x.md", ["*.md"])
    assert not guardrails.in_scope("/etc/passwd", ["*", "**"])
    assert guardrails.in_scope("/opt/cache/a.txt", ["/opt/cache/**"])
    assert not guardrails.in_scope("opt/cache/a.txt", ["/opt/cache/**"])
    assert not guardrails.dir_covered("/", ["**"]) and not guardrails.dir_covered("/etc", ["etc/**"])
    assert guardrails.dir_covered("/opt/cache", ["/opt/cache/**"])
    root = str(tmp_path / "wt")
    p = SimpleNamespace(write_scope=["*.md"], read_scope=["**"])
    assert guardrails.tool_denial(p, "Write", {"file_path": "/etc/x.md"}, root)
    assert guardrails.tool_denial(p, "Write", {"file_path": f"{root}/README.md"}, root) is None
    assert guardrails.tool_denial(p, "Read", {"file_path": "/etc/passwd"}, root)
    assert guardrails.tool_denial(p, "Grep", {"pattern": "x", "path": "/"}, root)


def test_dir_covered_means_everything_under_it_is_in_scope():
    scope = ["src/api/**", "*.md", "docs", "lib/"]
    assert not guardrails.dir_covered("", scope)       # *.md is in scope, the rest of the root isn't
    assert not guardrails.dir_covered("src", scope)    # holds src/api, and src/core.py too
    assert guardrails.dir_covered("src/api", scope)
    assert guardrails.dir_covered("src/api/sub", scope)
    assert guardrails.dir_covered("docs", scope) and guardrails.dir_covered("docs/x", scope)
    assert guardrails.dir_covered("lib", scope)
    assert not guardrails.dir_covered("src/apix", scope)
    assert not guardrails.dir_covered("src/api", ["src/api/*.py"])
    assert guardrails.dir_covered("", ["**"])


# -- 1. the hard check: the branch's diff --------------------------------------------------------

@pytest.fixture
def branch(db, repo, profiles):
    ws = workspaces.create(db, str(repo), "feat").workspace
    root = Path(ws.path)
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "api" / "h.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm inscope", root)
    return ws


def test_a_rename_out_of_scope_and_a_mode_change_count(db, branch, entitled):
    root = Path(branch.path)
    sh("git mv src/api/h.py moved.py && git commit -qm mv", root)
    sh("git update-index --chmod=+x app.py && git commit -qm chmod", root)
    add_worker(db, branch, "scoped")
    found = {v.detail.split(" ", 1)[0] for v in rule_checks.run(db, branch).violations}
    assert found == {"moved.py", "app.py"}     # src/api/h.py's deletion is in scope


def test_the_merge_gate_refuses_a_branch_outside_its_scope(db, branch, repo, entitled):
    (Path(branch.path) / "core.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm out", Path(branch.path))
    add_worker(db, branch, "scoped")
    report = gates.run(db, branch, load_repo_config(str(repo)), review_required=False)
    assert not report.ok
    assert "core.py is outside the profile's write_scope" in report.problem


def test_check_write_scope_caps_the_list():
    files = [f"f{i}.py" for i in range(15)]
    found = rule_checks.check_write_scope(["src/**"], files)
    assert len(found) == rule_checks.MAX_SHOWN_PER_RULE + 1
    assert found[-1].detail == "... and 5 more files"
    assert rule_checks.check_write_scope(None, files) == []


# -- 2. best effort: Claude Code's hook and the native tools ---------------------------------------


def test_an_in_scope_decoy_path_does_not_hide_the_real_one(repo, profiles, entitled, tmp_path):
    p = guardrails.effective(load_profile("scoped", str(repo)))
    root = str(tmp_path)
    decoy = {"file_path": f"{root}/src/api/ok.py", "notebook_path": f"{root}/core.ipynb"}
    assert guardrails.tool_denial(p, "NotebookEdit", decoy, root)
    assert guardrails.tool_denial(p, "Read", {"file_path": f"{root}/src/a.py", "path": f"{root}/x"}, root)
    assert guardrails.tool_denial(p, "Grep", {"path": f"{root}/src", "file_path": root}, root)
    assert guardrails.tool_denial(p, "Write", {"file_path": f"{root}/src/api/ok.py"}, root) is None
    # A search pattern can't climb out of the directory that was checked.
    src = f"{root}/src"
    for pat in ("../../etc/*", "/etc/*", "~/.ssh/*", "a/../../x"):
        assert guardrails.tool_denial(p, "Glob", {"pattern": pat, "path": src}, root), pat
    assert guardrails.tool_denial(p, "Grep", {"pattern": "x", "path": src, "glob": "../*"}, root)
    assert guardrails.tool_denial(p, "Glob", {"pattern": "**/*.py", "path": src}, root) is None
    assert guardrails.tool_denial(p, "Grep", {"pattern": "../x", "path": src}, root) is None   # a regex


def test_a_search_of_a_directory_partly_in_scope_is_denied(tmp_path):
    """Claude Code's Grep reads the whole directory it is given: one that only
    holds something in scope would expose the rest of it."""
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "h.py").write_text("x\n")
    (tmp_path / "src" / "core.py").write_text("secret\n")
    p = SimpleNamespace(write_scope=None, read_scope=["src/api/**", "*.md"])
    root = str(tmp_path)
    for tool in ("Grep", "Glob"):
        assert guardrails.tool_denial(p, tool, {"pattern": "x", "path": f"{root}/src"}, root), tool
        assert guardrails.tool_denial(p, tool, {"pattern": "x"}, root), tool          # the root, via *.md
        assert guardrails.tool_denial(p, tool, {"pattern": "x", "path": f"{root}/src/api"}, root) is None
    # Grep of one file: that file's own scope decides.
    assert guardrails.tool_denial(p, "Grep", {"pattern": "x", "path": f"{root}/src/api/h.py"}, root) is None
    assert guardrails.tool_denial(p, "Grep", {"pattern": "x", "path": f"{root}/src/core.py"}, root)


def test_native_file_tools_are_confined_to_the_scope(tmp_path):
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "h.py").write_text("hit = 1\n")
    (tmp_path / "src" / "core.py").write_text("hit = 2\n")
    (tmp_path / "secret.txt").write_text("hit = 3\n")
    box = Toolbox().add(*core_tools(str(tmp_path), bash_timeout=5,
                                    write_scope=["src/api/**"], read_scope=["src/**"]))
    assert not box.call("Write", {"path": "src/api/new.py", "content": "x\n"}).is_error
    r = box.call("Write", {"path": "src/core2.py", "content": "x\n"})
    assert r.is_error and "outside your write scope" in r.content
    assert not (tmp_path / "src" / "core2.py").exists()
    box.call("Read", {"path": "src/core.py"})
    r = box.call("Edit", {"path": "src/core.py", "old_string": "hit", "new_string": "miss"})
    assert r.is_error and "outside your write scope" in r.content
    r = box.call("Read", {"path": "secret.txt"})
    assert r.is_error and "outside your read scope" in r.content
    globbed = box.call("Glob", {"pattern": "**/*"}).content.splitlines()
    assert "secret.txt" not in globbed and "src/core.py" in globbed
    grepped = box.call("Grep", {"pattern": "hit"}).content
    assert "secret.txt" not in grepped and "src/core.py" in grepped
    # A symlink in scope pointing outside it doesn't let Grep or Read through.
    (tmp_path / "src" / "link.txt").symlink_to(tmp_path / "secret.txt")
    assert "secret" not in box.call("Grep", {"pattern": "hit = 3"}).content
    assert box.call("Grep", {"pattern": "hit = 3"}).content == "no matches"
    assert box.call("Read", {"path": "src/link.txt"}).is_error
    assert "src/link.txt" not in box.call("Glob", {"pattern": "**/*"}).content.splitlines()
    # Without scopes nothing changes.
    free = Toolbox().add(*core_tools(str(tmp_path), bash_timeout=5))
    assert not free.call("Read", {"path": "secret.txt"}).is_error


# -- env_allow --------------------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    name = "brindle_envallow"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_a_pane_with_env_allow_really_starts_without_the_rest(session, tmp_path):
    tmux._tmux("set-environment", "-g", "DATABASE_URL", "postgres://secret")
    tmux._tmux("set-environment", "-g", "NPM_TOKEN", "npm-ok")
    tmux._tmux("set-environment", "-g", "ANTHROPIC_API_KEY", "sk-ant-ok")
    try:
        out = tmp_path / "pane.env"
        tmux.new_window(session, "w", str(tmp_path), ["/bin/sh", "-c", f'env > "{out}"'],
                        {"BRINDLE_AGENT_ID": "a1", "FROM_PROFILE": "p"},
                        keep=secrets.provider_credentials("claude"), allow=["NPM_TOKEN"])
        deadline = time.time() + 10
        while time.time() < deadline and not out.exists():
            time.sleep(0.05)
        time.sleep(0.2)
        got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        assert "DATABASE_URL" not in got
        assert got["NPM_TOKEN"] == "npm-ok" and got["ANTHROPIC_API_KEY"] == "sk-ant-ok"
        assert got["BRINDLE_AGENT_ID"] == "a1" and got["FROM_PROFILE"] == "p" and "PATH" in got
    finally:
        for name in ("DATABASE_URL", "NPM_TOKEN", "ANTHROPIC_API_KEY"):
            tmux._tmux("set-environment", "-g", "-r", name, check=False)
