"""End to end: the core CLI workspace lifecycle, driven through the real
commands in a throwaway git repo. No agent runs (``--agent none``), no network."""

import json
import os

import pytest
from typer.testing import CliRunner

from brindle import cull, git, history, scratch, tmux
from brindle.cli import app

from conftest import sh


def run(*args, ok=True):
    res = CliRunner().invoke(app, list(args))
    if ok:
        assert res.exit_code == 0, f"brindle {' '.join(args)} failed:\n{res.output}"
    return res


@pytest.fixture
def proj(repo, monkeypatch):
    """The throwaway repo (with an origin), as the working directory."""
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def ws_path(proj, db):
    run("new", "feat/e2e", "--agent", "none", "--no-setup")
    (ws,) = db.find_workspaces(str(proj.resolve())) or db.find_workspaces(str(proj))
    return ws


def test_init_writes_config_and_is_idempotent(proj):
    res = run("init", "--yes", ok=False)
    assert (proj / ".brindle" / "config.json").is_file(), res.output
    assert "detected:" in res.output
    first = (proj / ".brindle" / "config.json").read_text()
    again = run("init", "--yes", ok=False)
    assert "kept" in again.output
    assert (proj / ".brindle" / "config.json").read_text() == first


def test_init_outside_a_repo_fails(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    res = run("init", "--yes", ok=False)
    assert res.exit_code == 1 and "not in a git repo" in res.output


def test_doctor_runs_in_and_out_of_a_repo(proj, tmp_path, monkeypatch):
    def doctor():
        # Whether tools like claude are installed varies by machine; the exit
        # code must match what the report says either way.
        res = run("doctor", ok=False)
        assert res.exit_code == (1 if "problem(s) will stop brindle" in res.output else 0), res.output
        assert "python" in res.output and "brindle home" in res.output
        return res.output

    in_repo = doctor()
    assert "repo config" in in_repo and "routing light" in in_repo
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert "repo config" not in doctor()


def test_new_ls_status_diff_commit(proj, ws_path):
    ws = ws_path
    assert os.path.isdir(ws.path) and ws.branch == "feat/e2e"
    assert (ws.name, ws.base_branch) == ("feat-e2e", "main")

    out = run("ls").output
    assert ws.id in out and "feat/e2e" in out and "↑0 ↓0" in out
    data = json.loads(run("ls", "--json").stdout)
    assert [e["id"] for e in data] == [ws.id] and data[0]["agents"] == []

    st = run("status", ws.name).output
    assert "0 ahead, 0 behind" in st and "no upstream" in st

    assert run("diff", ws.name).output.strip() == "(no changes)"
    (open(os.path.join(ws.path, "new.txt"), "w")).write("hello\n")
    st = run("status", ws.name).output
    assert "new.txt" in st
    assert "new.txt" in run("diff", ws.name, "--stat").output

    assert run("commit", ws.name, "-m", "add new").output.startswith("✓")
    assert run("commit", ws.name, "-m", "again").output.strip() == "nothing to commit"
    st = run("status", ws.name).output
    assert "1 ahead, 0 behind" in st
    assert "new.txt" in run("diff", ws.name).output
    assert run("cd", ws.name).output.strip() == ws.path


def test_commands_resolve_the_workspace_from_inside_it(proj, ws_path, monkeypatch):
    monkeypatch.chdir(ws_path.path)
    assert ws_path.id in run("status").output


def test_unknown_workspaces_and_bad_arguments_are_clean_errors(proj):
    res = run("status", "nope", ok=False)
    assert res.exit_code == 1 and "Traceback" not in res.output
    assert run("rm", "nope", ok=False).exit_code == 1
    assert run("new", ok=False).exit_code == 1
    res = run("new", "x", "--pr", "1", ok=False)
    assert res.exit_code == 1 and "not both" in res.output


def test_sync_picks_up_new_base_commits(proj, ws_path):
    ws = ws_path
    sh("echo base >> app.py && git commit -qam 'base moves'", proj)
    assert "up to date" in run("sync", ws.name).output
    assert "base moves" in sh("git log --format=%s", ws.path)
    assert "0 ahead, 0 behind" in run("status", ws.name).output


def test_sync_refuses_uncommitted_changes(proj, ws_path):
    open(os.path.join(ws_path.path, "wip.txt"), "w").write("x")
    res = run("sync", ws_path.name, ok=False)
    assert res.exit_code == 1 and "uncommitted" in res.output


def test_merge_then_rm_deletes_the_merged_branch(proj, ws_path):
    ws = ws_path
    open(os.path.join(ws.path, "feature.txt"), "w").write("f\n")
    run("commit", ws.name, "-m", "feature")
    out = run("merge", ws.name).output
    assert "merged feat/e2e into main" in out
    assert (proj / "feature.txt").read_text() == "f\n"
    assert "feature" in sh("git log --format=%s main", proj)

    out = run("rm", ws.name).output
    assert f"removed {ws.id}" in out
    assert not os.path.isdir(ws.path)
    assert "feat/e2e" not in sh("git branch --list", proj)
    assert json.loads(run("ls", "--json").stdout) == []
    assert run("ls").output.strip() == "no workspaces"


def test_squash_merge(proj, ws_path):
    ws = ws_path
    for i in range(2):
        open(os.path.join(ws.path, f"f{i}.txt"), "w").write("x")
        run("commit", ws.name, "-m", f"c{i}")
    before = int(sh("git rev-list --count main", proj))
    run("merge", ws.name, "--squash")
    assert int(sh("git rev-list --count main", proj)) == before + 1
    assert (proj / "f0.txt").exists() and (proj / "f1.txt").exists()


def test_rm_keeps_an_unmerged_branch_and_needs_force_for_dirty(proj, ws_path):
    ws = ws_path
    open(os.path.join(ws.path, "a.txt"), "w").write("a")
    run("commit", ws.name, "-m", "unmerged work")
    open(os.path.join(ws.path, "dirty.txt"), "w").write("d")
    refused = run("rm", ws.name, ok=False)
    assert refused.exit_code == 1 and os.path.isdir(ws.path)
    out = run("rm", ws.name, "--force").output
    assert "removed" in out and not os.path.isdir(ws.path)
    assert "feat/e2e" in sh("git branch --list", proj)  # unmerged: kept


def test_new_reports_when_the_branch_is_taken(proj, ws_path):
    res = run("new", "feat/e2e", "--agent", "none", "--no-setup", ok=False)
    assert res.exit_code == 1 and "Traceback" not in res.output


def test_history_sessions_and_prune_on_a_quiet_repo(proj, db):
    assert run("history").output.strip() == "no history"
    assert run("sessions").output.strip() == "no paused sessions"
    out = run("prune").output
    assert "dropped 0 paused session(s), removed 0 old scratch session(s)" in out


def test_history_lists_rows_and_filters_by_kind(proj, db):
    root = git.main_repo_root(str(proj))
    history.record(db, root, "merge", branch="feat/a", result="merged")
    history.record(db, root, "check", branch="feat/b", result="ok")
    out = run("history").output
    assert "feat/a" in out and "feat/b" in out and "total tokens" in out
    only = run("history", "--kind", "merge").output
    assert "feat/a" in only and "feat/b" not in only
    assert "feat/a" in run("history", "--limit", "5").output


def test_prune_removes_a_finished_merged_worktree(proj, ws_path):
    ws = ws_path
    open(os.path.join(ws.path, "m.txt"), "w").write("m")
    run("commit", ws.name, "-m", "m")
    run("merge", ws.name)
    # Another live test run's private tmux server is not prune's to stop, nor
    # its socket to remove while that run lives (even once nothing listens).
    other = f"brindle-test-{os.getpid()}-other"
    tmux._on(other, "new-session", "-d", "-s", "x")
    try:
        out = run("prune").output
        assert "dropped" in out
        assert tmux.server_homes(other) == [None]
        tmux._on(other, "kill-server")
        assert not any(other in done for done in cull.orphan_servers())
        assert other in tmux.other_servers()
    finally:
        tmux._on(other, "kill-server")
        tmux.remove_socket(other)
    # prune never loses unmerged work and never crashes on a leftover worktree
    assert run("ls").exit_code == 0


def test_scratch_session_and_transfer(db, repo, tmp_path, monkeypatch):
    from brindle import cli

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.chdir(downloads)
    s = cli._here_or_scratch(db, reuse_scratch=False)
    assert scratch.is_scratch(s.path) and not (downloads / ".git").exists()

    assert run("transfer", str(repo), ok=False).exit_code == 1  # nothing to transfer yet
    assert "no scratch sessions" in run("transfer", str(repo), ok=False).output

    open(os.path.join(s.path, "notes.md"), "w").write("plan\n")
    git.commit_all(s.path, "Add notes")
    open(os.path.join(s.path, "todo.md"), "w").write("later\n")  # uncommitted

    out = run("transfer", str(repo)).output
    assert "moved 2 commit(s)" in out and "uncommitted work was committed first" in out
    moved = scratch.transferred_to(s.path)
    assert moved and moved.startswith(str(repo.resolve()))

    monkeypatch.chdir(repo)
    (ws,) = [w for w in db.find_workspaces(str(repo.resolve())) if w.kind == "worktree"]
    assert ws.branch.startswith("brindle/from-")
    assert open(os.path.join(ws.path, "notes.md")).read() == "plan\n"
    assert "brindle/from-" in run("ls").output
    run("merge", ws.name)
    assert (repo / "notes.md").read_text() == "plan\n"
    run("rm", ws.name)
    assert run("transfer", str(repo), ok=False).exit_code == 1  # already moved


def test_transfer_into_a_plain_folder_fails_cleanly(db, tmp_path, monkeypatch):
    elsewhere = tmp_path / "e"
    elsewhere.mkdir()
    s = scratch.create(db, str(elsewhere))
    open(os.path.join(s.path, "a.txt"), "w").write("a")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    res = run("transfer", str(plain), ok=False)
    assert res.exit_code == 1 and "isn't inside a git repository" in res.output
    res = run("transfer", s.path, ok=False)
    assert res.exit_code == 1 and "real repository" in res.output
