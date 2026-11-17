"""Worktrees share build caches instead of building their own (brindle.build_caches)."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import sh

from brindle import agents, build_caches, gates, providers, workspaces
from brindle.config import RepoConfig
from brindle.profiles import Profile, load_profile
from brindle.providers import ClaudeCode, Codex, LaunchContext


@pytest.fixture(autouse=True)
def global_caches(tmp_path, monkeypatch):
    for key in ("UV_CACHE_DIR", "CARGO_TARGET_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))


@pytest.mark.parametrize("stored, profile_home, enforced, variable, flagged", [
    pytest.param(True, None, False, None, False,
                 marks=pytest.mark.variant("a stored /chrome choice must survive worker launch")),
    (False, None, False, "1", False),    # turned on by its own variable
    pytest.param(True, False, False, None, True,
                 marks=pytest.mark.variant("the pane's profile home overrides a different stored home")),
    pytest.param(False, True, False, None, False,
                 marks=pytest.mark.variant("a profile home's explicit Chrome choice is respected")),
    (False, True, True, None, True),     # an enforced identity drops the profile's home
])
@pytest.mark.usefixtures("fake_agent_cli")
def test_claude_starts_without_chrome_unless_the_panes_home_chose_it(db, repo, tmp_path, monkeypatch, stored,
                                                                     profile_home, enforced, variable, flagged):
    def home(name, on):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".claude.json").write_text(json.dumps({"claudeInChromeDefaultEnabled": on}))
        return str(tmp_path / name)

    ws = workspaces.create(db, str(repo), "feat/chrome").workspace
    env = {"CLAUDE_CONFIG_DIR": home("profile", profile_home)} if profile_home is not None else {}
    profile = Profile("developer", "developer", "claude", "Prompt", env=env)
    monkeypatch.setattr(agents, "load_profile", lambda *a, **k: profile)
    monkeypatch.setattr(agents, "_profile_for", lambda *a, **k: profile)
    monkeypatch.setattr(agents, "launch_blockers", lambda *a, **k: [])
    monkeypatch.setattr(agents.company_identity, "enforced_route_env", lambda: {} if enforced else None)
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_CFC", raising=False)
    if variable:
        monkeypatch.setenv("CLAUDE_CODE_ENABLE_CFC", variable)
    with monkeypatch.context() as held:
        held.setattr(agents, "_launch", lambda db_, a, ws_, **kw: None)
        worker = agents.spawn(db, ws, "developer", mode="assign")
    worker.config_home = home("stored", stored)
    opened = []
    monkeypatch.setattr(agents, "_open_window", lambda _db, _a, _ws, _name, argv, *a, **k: opened.append(argv))
    monkeypatch.setattr(type(agents.get_provider("claude")), "after_launch", lambda self, target: None)
    agents._launch(db, worker, ws, prompt="Task", resume=None, watch_pane=False)
    assert ("--no-chrome" in opened[0]) is flagged


@pytest.mark.parametrize("who, shared_target", [
    pytest.param("worker", True, marks=pytest.mark.variant("Claude settings must preserve the shared build cache environment")),
    pytest.param("codex", True, marks=pytest.mark.variant("Codex must grant sandbox write access to shared cache directories")),
    pytest.param("check", True, marks=pytest.mark.variant("check dispatch must inherit the same shared cache as workers")),
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
        git_dirs = set(providers._worktree_git_dirs(ws.path))   # so the worker can commit
        assert set(json.loads(roots)) == {env["UV_CACHE_DIR"], env["CARGO_TARGET_DIR"]} | git_dirs
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


NPM_CI = "cd app && npm ci --no-audit"


@pytest.mark.parametrize("setup, base_lock, platform_clones, events", [
    pytest.param([NPM_CI, "echo done"], "unchanged", True, ["clone", "echo done"],
                 marks=pytest.mark.variant("a successful clone must still run the following setup command")),
    ([NPM_CI, "echo done"], "changed", True, [NPM_CI, "echo done"]),
    ([NPM_CI, "echo done"], "unchanged", False, [NPM_CI, "echo done"]),
    # Each command in order: the clone happens when its install is reached.
    (["cd app && rm -rf node_modules", NPM_CI], "unchanged", True, ["cd app && rm -rf node_modules", "clone"]),
    pytest.param(["exit 1", NPM_CI], "unchanged", True, ["exit 1"],
                 marks=pytest.mark.variant("a failed setup step must prevent the later node_modules clone")),
    # A flag that changes what or where it installs runs as configured.
    (["cd app && npm ci --prefix=web"], "unchanged", True, ["cd app && npm ci --prefix=web"]),
])
def test_new_worktree_clones_node_modules_when_the_lockfile_matches(db, repo, monkeypatch,
                                                                   setup, base_lock, platform_clones, events):
    app = repo / "app"
    app.mkdir()
    (app / "package.json").write_text('{"name": "app", "dependencies": {"left-pad": "1.3.0"}}\n')
    (app / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"setup": setup}))
    sh("git add -A && git commit -qm app && git push -q", repo)
    (app / "node_modules" / "left-pad").mkdir(parents=True)
    if base_lock == "changed":
        (app / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {}}\n')

    seen, clones = [], []

    def fake_clone(src, dst):
        seen.append("clone")
        clones.append((src, dst))
        dst.mkdir()
        return True

    monkeypatch.setattr(build_caches, "can_clone", lambda: platform_clones)
    monkeypatch.setattr(build_caches, "clone_tree", fake_clone)
    monkeypatch.setattr(workspaces, "run_commands",
                        lambda cmds, cwd, env: (seen.extend(cmds), workspaces.SetupResult("exit 1" not in cmds, ""))[1])

    created = workspaces.create(db, str(repo), "feat/node")
    assert seen == events
    assert created.setup.ok == ("exit 1" not in setup)
    if clones:
        assert clones == [(app / "node_modules", Path(created.workspace.path) / "app" / "node_modules")]


@pytest.mark.variant("race: two worktrees' engine builds sharing a CARGO_TARGET_DIR must not delete each other's staging dirs (shell code coverage can't see)")
def test_engine_builds_of_two_worktrees_sharing_a_cargo_target_keep_separate_work_dirs(tmp_path):
    """build_engine.sh deletes and recreates its staging dirs outside Cargo's
    lock, so a shared CARGO_TARGET_DIR must not make two worktrees share them.
    Stubbed tools stop the build at its first download."""
    script = Path(__file__).resolve().parents[1] / "app" / "scripts" / "build_engine.sh"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in {"uname": 'case "$1" in -m) echo arm64;; *) echo Darwin;; esac',
                       "git": "echo 0123456789abcdef0123456789abcdef01234567",
                       "uv": "exit 0", "curl": "exit 1", "sysctl": "echo 1"}.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    shared = tmp_path / "shared-target"
    for wt in ("wt1", "wt2"):
        app = tmp_path / wt / "app"
        (app / "scripts" / "engine" / "helper").mkdir(parents=True)
        (app / "src-tauri" / "icons").mkdir(parents=True)
        (app / "scripts" / "build_engine.sh").write_text(script.read_text())
        # The build's other inputs, which its stamp reads.
        for name in ("scripts/engine/brindle", "scripts/engine/engine.entitlements", "scripts/engine/helper/main.c",
                     "scripts/engine/helper/Info.plist", "scripts/engine/helper/helper.entitlements",
                     "src-tauri/icons/icon.icns"):
            (app / name).write_text("")
        env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path), "BRINDLE_SRC": str(tmp_path),
               "CARGO_TARGET_DIR": str(shared)}
        proc = subprocess.run(["bash", str(app / "scripts" / "build_engine.sh")], env=env,
                              capture_output=True, text=True)
        assert proc.returncode != 0 and (app / "src-tauri" / "target" / "engine-build" / "downloads").is_dir(), proc.stderr
    assert not shared.exists()
