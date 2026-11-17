"""The hourly sweep of leftover worktrees (brindle.worktree_cleanup): finished
ones go once nothing is in them, their changes saved as a patch and their
branch kept; the person's own checkouts are never touched."""

import dataclasses
import os
import time
from pathlib import Path

import pytest

from brindle import agents, config, disk, git, history, services, workspaces, worktree_cleanup
from brindle.db import Agent

LATER = 2 * disk.FRESH   # past the quiet time a finished worktree is left alone for


def _patches() -> list[Path]:
    return sorted((config.brindle_home() / worktree_cleanup.PATCHES_DIR).rglob("*.patch"))


def _agent(db, ws_id: str, **fields) -> None:
    db.add_agent(Agent(fields.pop("id", "w1"), ws_id, "developer", "claude", None, "assign",
                       fields.pop("status", "done"), "%1", fields.pop("result", "finished"),
                       time.time(), **fields))


@pytest.mark.parametrize("holder", [None, "process", "process-during-save",
                                    pytest.param("edit-during-save", marks=pytest.mark.variant(
                                        "an edit after saving the patch must prevent teardown and worktree deletion")),
                                    "teardown-deletes"])
def test_kept_merged_workspace_goes_once_nothing_is_in_it(db, repo, tmp_path, process_scan, monkeypatch,
                                                          holder):
    """A worker's workspace kept at merge (its branch merged, uncommitted
    changes left) is removed by a later sweep with its changes saved as a
    patch and its branch kept; a process whose folder is in it keeps it,
    and so does one that starts there, or an edit made, while its changes
    are being saved (after the earlier checks passed): then neither its
    teardown nor its services are run. A teardown that deletes saved
    changes keeps the worktree, and the patch, the only copy left."""
    ws = workspaces.create(db, str(repo), "kept-at-merge").workspace
    marker = tmp_path / "torn-down"
    teardown = "rm notes.txt" if holder == "teardown-deletes" else f"touch {marker}"
    configured = workspaces.config_for
    monkeypatch.setattr(workspaces, "config_for",
                        lambda *a: dataclasses.replace(configured(*a), teardown=[teardown]))
    downed = []
    monkeypatch.setattr(services, "down", lambda w, cfg: downed.append(w.id))
    path = Path(ws.path)
    (path / "feature.py").write_text("done = True\n")
    git.run(["add", "feature.py"], path)
    git.run(["commit", "-qm", "feature"], path)
    git.run(["merge", "-q", "--no-edit", ws.branch], repo)
    (path / "app.py").write_text("print('edited after the merge')\n")
    (path / "notes.txt").write_text("left behind\n")
    _agent(db, ws.id)
    monkeypatch.setattr(agents, "is_alive", lambda *a, **kw: False)
    if holder == "process":   # a dev server started by hand, still running there
        process_scan[4242] = os.path.realpath(path / "web")
    elif holder in ("process-during-save", "edit-during-save"):
        save = worktree_cleanup.save_patch

        def busy_meanwhile(*args, **kwargs):
            saved = save(*args, **kwargs)
            if holder == "process-during-save":
                process_scan[4242] = os.path.realpath(path)
            else:
                (path / "notes.txt").write_text("written after the patch was saved\n")
            return saved
        monkeypatch.setattr(worktree_cleanup, "save_patch", busy_meanwhile)

    worktree_cleanup.sweep(db, time.time() + LATER)

    if holder == "teardown-deletes":
        assert path.is_dir() and not (path / "notes.txt").exists()
        [patch] = _patches()
        assert "left behind" in patch.read_text()
        return
    if holder:
        assert path.is_dir() and (path / "notes.txt").is_file() and db.get_workspace(ws.id) is not None
        assert not marker.exists() and not downed
        if holder != "edit-during-save":   # (that one's patch has what the edit replaced)
            assert not _patches()
        return
    assert not path.exists() and db.get_workspace(ws.id) is None
    assert git.branch_exists(str(repo), ws.branch) and marker.exists() and downed == [ws.id]
    [patch] = _patches()
    git.run(["apply", "--check", "--binary", str(patch)], repo)   # applies to the merged commit
    assert "left behind" in patch.read_text() and "edited after the merge" in patch.read_text()


@pytest.mark.variant("cleanup must never remove an adopted personal checkout even when merged and idle")
def test_the_persons_own_checkout_is_never_touched(db, repo, tmp_path, monkeypatch):
    """A worktree the person made outside brindle's folders stays, however
    finished and idle it looks: merged, uncommitted changes, nothing in it,
    its workspace row's agents all closed."""
    mine = tmp_path / "my-checkout"
    git.run(["worktree", "add", "-q", "-b", "mine", str(mine), "main"], repo)
    (mine / "scratch.txt").write_text("my own notes\n")
    ws = workspaces.adopt_root(db, str(mine))
    _agent(db, ws.id, dismissed_at=time.time())
    monkeypatch.setattr(agents, "is_alive", lambda *a, **kw: False)

    worktree_cleanup.sweep(db, time.time() + 30 * disk.DAY)

    assert (mine / "scratch.txt").read_text() == "my own notes\n"
    assert git.worktree_for_branch(str(repo), "mine") == str(mine) and not _patches()


def test_scratchpad_worktree_goes_only_after_its_session_ends(db, repo, tmp_path, monkeypatch):
    """A worktree an agent made by hand in its session's scratchpad stays
    while that session runs, and goes (changes saved) once it has ended."""
    base = tmp_path / "cc-tmp"
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(base))
    pad = base / f"claude-{os.getuid()}" / "-Users-someone-proj" / "sess-123" / "scratchpad"
    pad.mkdir(parents=True)
    probe = pad / "probe"
    git.run(["worktree", "add", "-q", "--detach", str(probe), "HEAD"], repo)
    (probe / "trial.txt").write_text("an experiment\n")
    ws = workspaces.adopt_root(db, str(repo))
    _agent(db, ws.id, status="processing", result=None, session_ref="sess-123")
    running = [True]
    monkeypatch.setattr(agents, "is_alive", lambda *a, **kw: running[0])

    worktree_cleanup.sweep(db)
    assert (probe / "trial.txt").is_file() and not _patches()

    running[0] = False
    db.update_agent("w1", status="done", result="finished")
    worktree_cleanup.sweep(db)

    assert not probe.exists()
    [patch] = _patches()
    assert "an experiment" in patch.read_text()


@pytest.mark.parametrize("record", ["none", "archived"])
def test_scratchpad_worktree_of_a_session_without_rows(db, repo, tmp_path, monkeypatch, record):
    """A session whose agent rows are gone counts as ended only when brindle
    archived them with their removed workspace: a session it has no record
    of (here one started in a folder of a workspace that still exists)
    keeps its scratchpad worktree."""
    base = tmp_path / "cc-tmp"
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(base))
    ws = workspaces.create(db, str(repo), "still-here").workspace
    project = "".join(c if c.isalnum() else "-" for c in os.path.join(ws.path, "sub"))
    pad = base / f"claude-{os.getuid()}" / project / "sess-456" / "scratchpad"
    pad.mkdir(parents=True)
    probe = pad / "probe"
    git.run(["worktree", "add", "-q", "--detach", str(probe), "HEAD"], repo)
    monkeypatch.setattr(agents, "is_alive", lambda *a, **kw: False)
    if record == "archived":
        gone = workspaces.adopt_root(db, str(repo))
        _agent(db, gone.id, id="w9", session_ref="sess-456")
        assert history.archive(db, gone, db.list_agents(gone.id), "workspace removed")
        db.delete_workspace(gone.id)
        assert not [a for a in db.list_agents() if a.session_ref == "sess-456"]

    worktree_cleanup.sweep(db)

    assert probe.exists() == (record == "none")
