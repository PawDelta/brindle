"""Worktrees share build caches instead of building their own (brindle.build_caches)."""

import json
from pathlib import Path

import pytest
from conftest import sh

from brindle import agents, build_caches, gates, workspaces
from brindle.config import RepoConfig
from brindle.profiles import load_profile
from brindle.providers import ClaudeCode, Codex, LaunchContext


@pytest.fixture(autouse=True)
def global_caches(tmp_path, monkeypatch):
    for key in ("UV_CACHE_DIR", "CARGO_TARGET_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))


@pytest.mark.parametrize("who, shared_target", [("worker", True), ("codex", True), ("check", True),
                                                ("check", False)])
def test_worker_and_check_env_share_build_caches(db, repo, tmp_path, monkeypatch, who, shared_target):
    ws = workspaces.create(db, str(repo), "feat/caches").workspace
    cfg = RepoConfig(checks=["cargo test"], shared_cargo_target=shared_target)
    if who == "worker":
        # A Claude worker's Bash env: its process env, then its --settings env.
        argv = ClaudeCode().command(LaunchContext("w1", load_profile("developer"), None, cwd=ws.path))
        settings = json.loads(argv[argv.index("--settings") + 1])
        env = {**agents.agent_env(ws, "w1"), **settings["env"]}
    elif who == "codex":
        # A Codex worker's shell env, and the sandbox may write the shared caches.
        argv = Codex().command(LaunchContext("w1", load_profile("developer"), None, cwd=ws.path))
        sets = dict(a.removeprefix("shell_environment_policy.set.").split("=", 1) for a in argv
                    if a.startswith("shell_environment_policy.set."))
        env = {**agents.agent_env(ws, "w1"), **{k: json.loads(v) for k, v in sets.items()}}
        [roots] = [a.split("=", 1)[1] for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")]
        assert set(json.loads(roots)) == {env["UV_CACHE_DIR"], env["CARGO_TARGET_DIR"]}
    else:
        seen = []
        monkeypatch.setattr(gates, "_run_queued", lambda db, ws, cmd, env, *a, **kw: (seen.append(env), (True, "ok"))[1])
        gates.check_summary(db, ws, cfg)
        [env] = seen
    assert env["UV_CACHE_DIR"] == str(tmp_path / "xdg" / "uv")
    if shared_target:
        assert env["CARGO_TARGET_DIR"] == str(build_caches.cargo_target_dir(repo))
        assert not env["CARGO_TARGET_DIR"].startswith(ws.path)
    else:
        assert "CARGO_TARGET_DIR" not in env


@pytest.mark.parametrize("base_lock, platform_clones, cloned", [
    ("unchanged", True, True),
    ("changed", True, False),
    ("unchanged", False, False),
])
def test_new_worktree_clones_node_modules_when_the_lockfile_matches(db, repo, monkeypatch,
                                                                   base_lock, platform_clones, cloned):
    app = repo / "app"
    app.mkdir()
    (app / "package.json").write_text('{"name": "app", "dependencies": {"left-pad": "1.3.0"}}\n')
    (app / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"setup": ["cd app && npm ci --no-audit", "echo done"]}))
    sh("git add -A && git commit -qm app && git push -q", repo)
    (app / "node_modules" / "left-pad").mkdir(parents=True)
    if base_lock == "changed":
        (app / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {}}\n')

    clones, ran = [], []

    def fake_clone(src, dst):
        clones.append((src, dst))
        dst.mkdir()
        return True

    monkeypatch.setattr(build_caches, "can_clone", lambda: platform_clones)
    monkeypatch.setattr(build_caches, "clone_tree", fake_clone)
    monkeypatch.setattr(workspaces, "run_commands",
                        lambda cmds, cwd, env: (ran.extend(cmds), workspaces.SetupResult(True, ""))[1])

    created = workspaces.create(db, str(repo), "feat/node")
    target = Path(created.workspace.path) / "app" / "node_modules"
    if cloned:
        assert clones == [(app / "node_modules", target)]
        assert ran == ["echo done"] and created.setup.ok
    else:
        assert clones == []
        assert ran == ["cd app && npm ci --no-audit", "echo done"]
