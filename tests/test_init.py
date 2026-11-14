"""`brindle init`: detect a repo's setup and checks, write the config, check tools."""

import json
import shutil
import subprocess
from pathlib import Path

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


def test_detect_python_check_runs_without_python_on_path(tmp_path, monkeypatch):
    """A uv project with a .venv but no `python` on PATH must not get a bare `python`."""
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n[dependency-groups]\ndev = ["pytest"]\n')
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert detect.detect(repo).checks == [".venv/bin/python -m pytest -q"]
    # no .venv, no uv: python3 stands in for the missing python
    (repo / ".venv" / "bin" / "python").unlink()
    (repo / ".venv" / "bin").rmdir()
    (repo / ".venv").rmdir()
    (empty / "python3").write_text("")
    (empty / "python3").chmod(0o755)
    assert detect.detect(repo).checks == ["python3 -m pytest -q"]
    # lookalikes and malformed manifests are not uv
    for text in ('# [tool.uv]\n[tool.pytest]\n', '[tool.uvicorn]\npytest = 1\n', '[tool.uv\npytest'):
        (repo / "pyproject.toml").write_text(text)
        assert detect.detect(repo).checks == ["python3 -m pytest -q"]
    # pyproject that uses uv, no lock (whitespace and nested tables are still the uv table)
    for text in ('[tool.uv]\ndev-dependencies = ["pytest"]\n', '[ tool.uv ]\npytest = 1\n', '[tool.uv.sources]\npytest = 1\n'):
        (repo / "pyproject.toml").write_text(text)
        assert detect.detect(repo).checks == ["uv run pytest -q"]


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


def test_preflight_names_missing_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("BRINDLE_CLAUDE_BIN", "/nonexistent/claude")
    assert any("Claude Code" in p for p in doctor.preflight("claude"))
    assert not any("Claude Code" in p for p in doctor.preflight("shell"))
    # install.sh installs brindle (uv tool install), and says its folder is missing only
    # from the user's own PATH, not from the one install.sh extends for itself.
    for on_path in (False, True):
        bin_dir, out = _install_sh(tmp_path / str(on_path), on_path)
        assert (bin_dir / "brindle").exists()
        assert (str(bin_dir) in out) is not on_path


def _install_sh(tmp_path, on_path):
    """scripts/install.sh's bin folder and output on a box with no uv, brindle
    or claude (curl, uv: stubs)."""
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
    return home / ".local" / "bin", res.stdout


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



