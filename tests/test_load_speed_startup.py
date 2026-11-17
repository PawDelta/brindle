"""The app's first snapshot after the daemon starts answers from what is known:
no git status per workspace on the request path (with recovery competing for
the daemon, that took 30 s), counts stored by the last daemon while their
commits still stand, and the rest counted afterwards and published."""

import json
import time
from types import SimpleNamespace

import pytest

from brindle import bus, git, status_cache, tmux, view
from brindle.daemon import routes_read
from brindle.db import Agent, Workspace

MAIN, OLD, NEW = "a" * 40, "b" * 40, "c" * 40


def _worktree(tmp_path, db, name, head):
    """A worktree whose git files say HEAD is ``feat-<name>`` at ``head`` and
    main is at MAIN (plain files: nothing here can run git), with one worker
    that has reported."""
    path = tmp_path / name
    (path / ".git" / "refs" / "heads").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text(f"ref: refs/heads/feat-{name}\n")
    (path / ".git" / "refs" / "heads" / f"feat-{name}").write_text(head + "\n")
    (path / ".git" / "refs" / "heads" / "main").write_text(MAIN + "\n")
    ws = Workspace(id=f"ws-{name}", repo_root=str(tmp_path), name=name, kind="worktree",
                   branch=f"feat-{name}", base_branch="main", path=str(path), port_base=None,
                   tmux_session="s", created_at=time.time() - 3600)
    db.add_workspace(ws)
    db.add_agent(Agent(id=f"ag-{name}", workspace_id=ws.id, profile="developer", provider="shell",
                       parent_id=None, mode="assign", status="done", tmux_window="", result="done",
                       created_at=time.time() - 3600))
    return ws


@pytest.fixture
def cold(monkeypatch):
    """A fresh daemon's caches, with every git status counted by the test."""
    status_cache.clear()
    asked = []

    def counting(path, base, now=None):
        asked.append(str(path))
        return git.Status("feat", base, 0, 0, [], None)

    monkeypatch.setattr(status_cache, "cached_status", counting)
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot(None))
    return asked


def _route_snapshot(monkeypatch):
    """GET /v1/snapshot's handler, with the background count held back."""
    later = []
    monkeypatch.setattr(view, "warm_later", lambda pending: later.append(list(pending)), raising=False)
    request = SimpleNamespace(query_params={}, app=SimpleNamespace(state=SimpleNamespace()))
    rows = routes_read.snapshot(request)
    return {w["id"]: w for w in json.loads(rows.body)}, later


UNKNOWN = {"ahead": None, "behind": None, "dirty": None}


@pytest.mark.parametrize("stored_head, shown", [
    (None, UNKNOWN),    # nothing stored: unknown until counted, never counted on the request path
    (OLD, None),        # stored for these commits, ahead 0: merged, so hidden at once
    (NEW, UNKNOWN),     # HEAD moved since: the stored counts no longer apply
])
def test_cold_snapshot_runs_no_git_and_uses_stored_counts(db, tmp_path, monkeypatch, cold, stored_head, shown):
    ws = _worktree(tmp_path, db, "one", OLD)
    if stored_head:
        db.set_git_counts(ws.id, "main", (stored_head, MAIN, None), 0, 3, 1)

    rows, later = _route_snapshot(monkeypatch)

    assert cold == []                    # nothing counted on the request path
    got = rows.get(ws.id)
    assert (got and {k: got[k] for k in ("ahead", "behind", "dirty")}) == shown
    assert [w.id for w, _ in later[0]] == [ws.id]    # counted afterwards


@pytest.mark.parametrize("live, told", [
    (git.Status("feat", "main", 2, 0, [], None), True),     # not what was shown: the app reads again
    (git.Status("feat", "main", 0, 3, ["x"], None), False),  # as shown: nothing to read again
])
def test_background_count_publishes_when_it_changes_the_view(db, tmp_path, monkeypatch, live, told):
    ws = _worktree(tmp_path, db, "two", OLD)
    monkeypatch.setattr(status_cache, "cached_status", lambda path, base, now=None: live)

    thread = view.warm_later([(ws, view.GitCounts(0, 3, 1))])
    thread.join()

    kinds = [e["kind"] for e in bus.tail(db, repo_root=ws.repo_root)]
    assert (view.GIT_KNOWN_EVENT in kinds) is told
