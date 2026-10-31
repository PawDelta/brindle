"""`brindle init`: detect a repo's setup and checks, write the config, check tools."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import click
import pytest
import typer.main
from typer.testing import CliRunner

from brindle import detect, doctor
from brindle.cli import app

from conftest import sh


def test_detect_uv_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("")
    d = detect.detect(tmp_path)
    assert d.stacks == ["Python (uv)"]
    assert d.setup == ["uv sync"] and d.checks == ["uv run pytest -q"]


def test_detect_node_picks_package_manager_and_skips_placeholder_test(tmp_path):
    pkg = {"scripts": {"test": 'echo "Error: no test specified" && exit 1', "typecheck": "tsc"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    d = detect.detect(tmp_path)
    assert d.stacks == ["Node (pnpm)"]
    assert d.setup == ["pnpm install --frozen-lockfile"]
    assert d.checks == ["pnpm run typecheck"]


def test_detect_go_and_make_fallback(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "Makefile").write_text("test:\n\tgo test ./...\n")
    d = detect.detect(tmp_path)
    assert d.checks == ["go test ./..."]          # make test only when nothing else gave a check

    other = tmp_path / "other"
    other.mkdir()
    (other / "Makefile").write_text("build:\n\ttrue\ntest: build\n\ttrue\n")
    assert detect.detect(other).checks == ["make test"]


def test_init_writes_detected_config_and_keeps_existing(repo, monkeypatch):
    (repo / "go.mod").write_text("module x\n")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(doctor, "checks", lambda root: [])
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    cfg = json.loads((repo / ".brindle" / "config.json").read_text())
    assert cfg["setup"] == ["go mod download"] and cfg["checks"] == ["go test ./..."]
    assert "Ready" in res.output

    (repo / ".brindle" / "config.json").write_text('{"checks": ["mine"]}\n')
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    assert "kept" in res.output
    assert json.loads((repo / ".brindle" / "config.json").read_text()) == {"checks": ["mine"]}


def test_init_fails_on_doctor_failure(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(doctor, "checks", lambda root: [doctor.Check(doctor.FAIL, "tmux", "not found")])
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 1
    assert "tmux" in res.output


def test_init_with_committed_config_says_nothing_about_committing(repo, monkeypatch):
    monkeypatch.chdir(repo)
    dirty = doctor.Check(doctor.WARN, "working tree", "uncommitted changes: workers branch from committed work only")
    monkeypatch.setattr(doctor, "checks", lambda root: [dirty])
    res = CliRunner().invoke(app, ["init"])
    # Its own .brindle/ output is a step to take, not an uncommitted-changes warning.
    assert res.exit_code == 0 and "Ready" in res.output, res.output
    assert "uncommitted changes" not in res.output and "git add .brindle" in res.output
    (repo / "app.py").write_text("print('changed')\n")   # other work still gets the warning
    res = CliRunner().invoke(app, ["init"])
    assert "uncommitted changes" in res.output and "commit .brindle/" in res.output
    sh("git checkout -q app.py && git add .brindle && git commit -qm config", repo)
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    assert "commit .brindle/" not in res.output


def test_init_outside_git_points_at_scratch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 1
    assert "scratch session" in res.output


def test_preflight_names_missing_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("BRINDLE_CLAUDE_BIN", "/nonexistent/claude")
    problems = doctor.preflight("claude")
    hint = next(p for p in problems if "Claude Code" in p)
    # The official installer first; npm is the alternative. install.sh says the same.
    assert hint.index("curl -fsSL https://claude.ai/install.sh | bash") < hint.index("npm install")
    assert not any("Claude Code" in p for p in doctor.preflight("shell"))
    install, out = _install_sh(tmp_path, on_path=False)
    assert out.index("curl -fsSL https://claude.ai/install.sh | bash") < out.index("npm install -g")
    # The PATH note is about the user's PATH, not the one install.sh extends for itself.
    assert out.strip().splitlines()[-1] == "Open a new terminal, or run: source ~/.local/bin/env"
    assert "uv added a line to ~/.bashrc, ~/.profile and ~/.zshrc" in out
    assert "isn't on" not in _install_sh(tmp_path / "again", on_path=True)[1]
    assert "BRINDLE_VERSION=<version>" in install and not re.search(r"BRINDLE_VERSION=\d", install)


def _install_sh(tmp_path, on_path):
    """scripts/install.sh's output on a box with no uv, brindle or claude (curl, uv: stubs)."""
    home, stubs = tmp_path / "home", tmp_path / "stubs"
    stubs.mkdir(parents=True)
    uv = ('case "$2" in dir) echo "$HOME/.local/bin" ;; install) printf \'#!/bin/sh\\necho brindle 0.5.5\\n\' '
          '> "$HOME/.local/bin/brindle"; chmod +x "$HOME/.local/bin/brindle" ;; esac')
    (stubs / "curl").write_text(f'#!/bin/sh\nmkdir -p "$HOME/.local/bin"; touch "$HOME/.local/bin/env"\n'
                               f"printf '%s\\n' '#!/bin/sh' '{uv}' > \"$HOME/.local/bin/uv\"\n"
                               'chmod +x "$HOME/.local/bin/uv"; echo "exit 0"\n')
    (stubs / "tmux").write_text("#!/bin/sh\n")
    for stub in ("curl", "tmux"):
        (stubs / stub).chmod(0o755)
    for tool in ("sh", "uname", "mkdir", "chmod", "touch", "cut", "printf"):
        if not (stubs / tool).exists():
            (stubs / tool).symlink_to(shutil.which(tool))
    script = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
    path = f"{home}/.local/bin:{stubs}" if on_path else str(stubs)
    res = subprocess.run(["sh", str(script)], env={"HOME": str(home), "PATH": path}, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return script.read_text(), res.stdout


def test_pr_footer_on_by_default_and_opt_out(repo):
    from brindle import workspaces

    assert workspaces.with_footer("Fixes #1", str(repo)).endswith(workspaces.PR_FOOTER)
    assert workspaces.with_footer("", str(repo)) == workspaces.PR_FOOTER
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text('{"pr_footer": false}')
    assert workspaces.with_footer("Fixes #1", str(repo)) == "Fixes #1"


def test_autopilot_guide_asks_for_checks_when_none(tmp_path):
    from brindle import autopilot
    from brindle.config import RepoConfig

    (tmp_path / "go.mod").write_text("module x\n")
    text = autopilot.guide(RepoConfig(), str(tmp_path))
    assert "no `checks`" in text and "`go test ./...`" in text
    assert "no `checks`" not in autopilot.guide(RepoConfig(checks=["make"]), str(tmp_path))


def test_history_share_card(db, repo):
    import time

    from brindle import history, workspaces
    from brindle.db import Agent, Task

    ws = workspaces.adopt_root(db, str(repo))
    now = time.time()
    db.add_agent(Agent("sup", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, now - 900))
    db.add_autopilot("sup")
    db.update_autopilot("sup", goal="Ship the settings page")
    db.set_milestones("sup", [("API", "true", ""), ("UI", "true", "")])
    for m in db.milestones("sup"):
        db.record_check(m.id, True, "")
    # Workers' and reviewers' agent rows are gone by now (removed with their
    # worktrees): the card reads the tasks and history.
    for tid, aid, branch, state, started in (("t1", "w1", "feat/api", "merged", now - 600),
                                              ("t2", "w2", "feat/ui", "started", now - 300)):
        db.add_task(Task(tid, str(repo), aid, "sup", ws.id, "developer", "x", "assign", 1, branch,
                         None, None, None, state, started, started))
    sh("git checkout -q -b feat/ui && git commit -q --allow-empty -m ui && git checkout -q main "
       "&& git merge -q --no-ff --no-edit feat/ui && git branch -q -D feat/ui", repo)
    for aid, kind, branch, profile in (("w1", "worker_result", "feat/api", "developer"),
                                       ("w2", "worker_result", "feat/ui", "developer"),
                                       ("r1", "review", "feat/api", "reviewer-codex"),
                                       ("sup", "merge", "feat/api", "supervisor")):
        db.add_history(str(repo), kind, agent_id=aid, branch=branch, profile=profile)

    card = history.share_card(db, "sup")
    assert "Goal: Ship the settings page" in card
    assert "✓ 2/2 milestones verified" in card
    assert "2 workers · 2 branches merged · 1 review (1 by a different model)" in card
    assert "1.5× parallel" in card
    assert card.endswith("https://pawdelta.com/brindle/")


def test_demo_repo_has_failing_goal(brindle_home):
    import subprocess

    from brindle import autopilot, demo
    from brindle.config import load_repo_config

    root = demo.create()
    plan = autopilot.load_goals_file(str(root))
    assert [m[1] for m in plan.milestones] == ["python3 -m unittest tests.test_slug -q",
                                               "python3 -m unittest tests.test_wrap -q"]
    # Each branch can pass the merge check alone; the milestone checks fail until the work is done.
    # Always fast: the demo shows parallel workers, never a supervisor doing it all.
    assert load_repo_config(root).delegation == "fast"
    check = load_repo_config(root).checks[0].split()
    assert subprocess.run(check, cwd=root, capture_output=True).returncode == 0
    assert subprocess.run(["python3", "-m", "unittest", "-q"], cwd=root, capture_output=True).returncode != 0
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout == ""
    assert demo.create() != root
    assert load_repo_config(demo.create(local=True)).default_agent == "developer-local"


def test_demo_says_nothing_about_the_autopilot_flag_nobody_passed(tmp_path, monkeypatch):
    from brindle import cli, demo

    monkeypatch.chdir(tmp_path)   # demo chdirs into its repo; this puts the cwd back afterwards
    monkeypatch.setattr(demo, "create", lambda local=False: tmp_path)
    started = []
    monkeypatch.setattr(cli, "start", lambda **kw: started.append(kw))
    res = CliRunner().invoke(app, ["demo"])
    assert res.exit_code == 0, res.output
    assert "deprecated" not in res.output
    assert started[0]["autopilot"] is None and started[0]["demo"] is True

    def walk(cmd, path):   # and no help text names a source file
        out = CliRunner().invoke(app, [*path, "--help"], terminal_width=200).output
        assert "src/brindle" not in out and ".py`" not in out, path
        for name, sub in (cmd.commands.items() if isinstance(cmd, click.Group) else ()):
            walk(sub, [*path, name])

    walk(typer.main.get_command(app), [])


@pytest.mark.parametrize("platform, managers, wanted", [
    ("darwin", ["apt-get"], "brew install tmux"),
    ("linux", ["apt-get"], "sudo apt install tmux"),
    ("linux", ["dnf"], "sudo dnf install tmux"),
    ("linux", [], "sudo apt install tmux (or your distro's package manager)"),
])
def test_tmux_hint_fits_the_platform(monkeypatch, platform, managers, wanted):
    monkeypatch.setattr(doctor.sys, "platform", platform)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}" if name in managers else None)
    assert doctor.tmux_install() == wanted
    # tmux itself is missing here too, so preflight names the same command.
    assert any(p.endswith(f"Install: {wanted}") for p in doctor.preflight("shell"))

