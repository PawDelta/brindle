"""brindle.reap: what outlived its work is found and stopped by the sweep.
tmux is faked (its session listing, kills); the DB and git are real."""

import fcntl
import os
import time
from types import SimpleNamespace

import pytest
from conftest import sh

from brindle import agents, cull, doctor, git, procs, reap, tmux, workspaces
from brindle.config import brindle_home
from brindle.db import Agent, Task, Workspace
from brindle.service import inbox

NOW = time.time()
OLD = NOW - 30 * 24 * 3600
SESSION_INFOS = tmux.session_infos      # the real listing, for a test that fakes tmux under it


@pytest.fixture
def fake_tmux(monkeypatch):
    t = SimpleNamespace(sessions=[], panes=tmux.PaneSnapshot(tmux.current_server()), killed=[], stopped=[])
    monkeypatch.setattr(tmux, "session_infos", lambda prefix="brindle_", fresh=False: t.sessions)
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: t.panes)
    monkeypatch.setattr(tmux, "kill_session", t.killed.append)
    monkeypatch.setattr(tmux, "kill_window", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "window_pids", lambda *a, **k: [])
    monkeypatch.setattr(procs, "descendants", lambda pids, procs=None: set(pids))
    monkeypatch.setattr(procs, "terminate", lambda pids, grace=3.0: t.stopped.extend(pids) or len(pids))
    return t


def test_session_listing_is_parsed(monkeypatch):
    """The one listing every other test fakes, from tmux's own output."""
    rows = ["brindle_p_a\t1\t100\t200\t%1\t11\t0\t300\tzsh\t/w/a\t", "other\t0\t1\t1\t%2\t12\t0\t1\tzsh\t/x\t",
            "brindle_p_a\t1\t100\t200\t%3\t13\t1\t300\tclaude\t\tclaude --resume"]
    out = {"list-panes": "\n".join(rows) + "\n", "show-environment": "BRINDLE_HOME=/some/home\n"}
    asked = []
    monkeypatch.setattr(tmux, "_tmux", lambda *args, **k: asked.append(args[0]) or SimpleNamespace(
        returncode=0, stdout=out.get(args[0], "")))
    monkeypatch.setattr(tmux, "_SESSION_HOMES", {})

    tmux.session_infos()
    [s] = tmux.session_infos()        # the cull lists them every minute: the home is read once

    assert asked.count("show-environment") == 1
    assert (s.name, s.attached, s.created, s.activity, s.home) == ("brindle_p_a", True, 100, 200, "/some/home")
    assert [(p.id, p.pid, p.path, p.dead, p.idle()) for p in s.panes] == [
        ("%1", 11, "/w/a", False, True), ("%3", 13, "", True, True)]

    out["show-environment"] = "BRINDLE_HOME=/another/home\n"
    tmux.ensure_session(s.name, "/w/a", {"BRINDLE_HOME": "/another/home"}, size=(171, 54))
    [s] = tmux.session_infos()
    assert s.home == "/another/home"


def session(name, *panes, attached=False, home=None, activity=OLD):
    return tmux.SessionInfo(name, attached, OLD, activity, home or str(brindle_home()), list(panes))


def pane(pane_id, path, pid=4242, cmd="zsh", activity=OLD):
    return tmux.PaneInfo(pane_id, pid, str(path), cmd, False, "", activity)


def add(db, ws, agent_id, **kw):
    fields = dict(profile="developer", provider="claude", parent_id=None, mode="assign", status="idle",
                  tmux_window="", result=None, created_at=OLD, status_since=OLD)
    db.add_agent(Agent(agent_id, ws.id, **{**fields, **kw}))


def live(t, *pane_ids):
    t.panes.update({p: True for p in pane_ids})


@pytest.mark.parametrize("case,reaped", [
    ("no row", True),
    ("worktree gone, worker left in it", True),
    ("worktree gone, another workspace's worker in it", False),
    ("attached", False),
    ("recent input", False),
    ("recent output", False),
    ("uncommitted work in its folder", False),
    ("git status fails", False),   # unknown is never clean
    pytest.param("another brindle home", False,
                 marks=pytest.mark.variant("a feature CLI's temp BRINDLE_HOME shares the tmux server")),
])
def test_orphan_sessions_are_reaped_by_the_sweep(db, repo, tmp_path, fake_tmux, monkeypatch, case, reaped):
    folder = tmp_path / "plain"
    folder.mkdir()
    name = "brindle_proj_old"
    if case.startswith("worktree gone"):
        ws = Workspace("proj/old", str(repo), "old", "worktree", "old", "main", str(tmp_path / "gone"),
                       None, name, OLD)
        db.add_workspace(ws)
        if "another workspace" in case:   # its pane moved into this session
            ws = workspaces.adopt_root(db, str(repo))
        add(db, ws, "agy1", provider="antigravity", tmux_window="%7")
        live(fake_tmux, "%7")
        folder = tmp_path / "gone"
    if case == "uncommitted work in its folder":
        (repo / "wip.py").write_text("x = 1\n")
        folder = repo
    if case == "git status fails":
        def refused(path, tracked_only=False):
            raise git.GitBusy("git status: no room")
        monkeypatch.setattr(git, "dirty_files", refused)
        folder = repo
    # Recent as of this test, not of the module's import: a long suite runs it
    # well after collection, past the reaper's SESSION_GRACE.
    recent = time.time()
    fake_tmux.sessions = [session(name, pane("%7", folder, activity=recent if case == "recent output" else OLD),
                                  attached=case == "attached", activity=recent if case == "recent input" else OLD,
                                  home="/elsewhere" if case == "another brindle home" else None)]

    before = doctor.reap_checks(db)
    lines = cull.sweep(db)

    assert (name in fake_tmux.killed) == reaped
    assert any(name in line for line in lines) == reaped
    assert (4242 in fake_tmux.stopped) == reaped   # its processes first
    if case.startswith("worktree gone"):
        assert (db.get_agent("agy1").dismissed_at is not None) == reaped
    # doctor: what it would reap before, what it reaped after
    assert (name in before[1].detail) == reaped
    assert (name in doctor.reap_checks(db)[0].detail) == reaped


@pytest.mark.parametrize("case,killed", [
    ("merged, worker reported", True),
    ("merged, worker still working", False),
    ("closed worker left a shell", True),
    ("unreported worker with uncommitted work", False),
    # The leftover session goes, but a worker running elsewhere is no occupant of it.
    ("merged, reported worker on another tmux server", True),
    pytest.param("merged, reported worker in an attached session", True, marks=pytest.mark.variant("Reap the old session, spare the worker attached elsewhere")),
    ("merged, another workspace's live worker in its session", False),
])
def test_finished_workers_sessions_are_reaped(db, repo, fake_tmux, monkeypatch, case, killed):
    ws = workspaces.create(db, str(repo), "feat-done").workspace
    db.conn.execute("UPDATE workspaces SET created_at=? WHERE id=?", (OLD, ws.id))
    reported = "did it" if case in ("merged, worker reported", "closed worker left a shell") \
        or "another workspace" in case else None
    elsewhere = "reported worker" in case
    if case == "merged, worker still working":
        add(db, ws, "w1", tmux_window="%3", status="processing")
    elif elsewhere:
        foreign = "another tmux server" in case
        add(db, ws, "w1", result="did it", tmux_window="%3" if foreign else "%9",
            tmux_server="brindle-elsewhere" if foreign else None)
        monkeypatch.setattr(tmux, "window_alive", lambda *a, **k: True)   # alive on its own server
    else:
        add(db, ws, "w1", tmux_window="%3", result=reported)
    if case in ("closed worker left a shell", "unreported worker with uncommitted work"):
        sh("echo more >> app.py && git commit -qam more", ws.path)   # not merged
        sh("echo wip > wip.py", ws.path)   # uncommitted: only an unfinished worker's keeps its session
    if case == "closed worker left a shell":
        db.update_agent("w1", dismissed_at=OLD)
    else:
        live(fake_tmux, "%3")
    fake_tmux.sessions = [session(ws.tmux_session, pane("%3", ws.path, cmd="zsh" if case ==
                                                         "closed worker left a shell" else "claude"))]
    if "another workspace" in case:
        add(db, workspaces.adopt_root(db, str(repo)), "x1", tmux_window="%4", status="processing")
        live(fake_tmux, "%4")
        fake_tmux.sessions[0].panes.append(pane("%4", repo, cmd="claude"))
    if case == "merged, reported worker in an attached session":
        live(fake_tmux, "%9")
        fake_tmux.sessions.append(session("brindle_proj_alt", pane("%9", ws.path, cmd="claude"), attached=True))

    reap.run(db)

    assert (ws.tmux_session in fake_tmux.killed) == killed
    assert "brindle_proj_alt" not in fake_tmux.killed
    if "another workspace" in case:
        assert db.get_agent("x1").dismissed_at is None
    if case == "merged, worker reported":
        assert db.get_agent("w1").dismissed_at is not None
    if elsewhere:
        assert db.get_agent("w1").dismissed_at is None
    assert os.path.isdir(ws.path)   # the worktree itself is never touched here


@pytest.mark.parametrize("case", ["idle",
    pytest.param("attached", marks=pytest.mark.variant("An attached idle supervisor must not be reaped")),
    pytest.param("recent input", marks=pytest.mark.variant("Recent client input must keep an otherwise idle session alive")),
    "running worker", "uncommitted work", "attached worker shell", "recent output",
    pytest.param("a job in a second pane", marks=pytest.mark.variant("A job in a sibling pane must protect the whole session")),
    pytest.param("a job on a done worker's old pane id", marks=pytest.mark.variant("Reused pane IDs must not hide an unrelated live job")),
])
def test_idle_supervisors_are_stopped_but_resumable(db, repo, fake_tmux, case):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "boss", mode="interactive", profile="supervisor", tmux_window="%1")
    live(fake_tmux, "%1")
    fake_tmux.panes.tags["%1"] = {agents.AGENT_TAG: "boss"}   # as its launch tags it
    if case == "uncommitted work":
        (repo / "wip.py").write_text("x = 1\n")
    if case == "running worker":
        add(db, ws, "w1", parent_id="boss", status="processing", tmux_window="%2")
        live(fake_tmux, "%2")
    # A second pane the pause would close: logging in a shell, or a job brindle didn't start.
    job = {"recent output": [pane("%8", repo, activity=NOW - 3600)],
           "a job in a second pane": [pane("%8", repo, cmd="python")],
           "a job on a done worker's old pane id": [pane("%8", repo, cmd="node")]}.get(case, [])
    if case == "a job on a done worker's old pane id":   # tmux restarted and reused %8
        add(db, ws, "w1", parent_id="boss", result="did it", status="done", tmux_window="%8")
        live(fake_tmux, "%8")   # alive, and untagged: the DB still maps it to w1
    fake_tmux.sessions = [session("brindle_proj_main", pane("%1", repo, cmd="claude"), *job,
                                  attached=case == "attached",
                                  activity=NOW - 3600 if case == "recent input" else OLD)]
    if case == "attached worker shell":   # pause closes a reported worker's shell-only session
        wws = workspaces.create(db, str(repo), "feat-shell").workspace
        add(db, wws, "w1", parent_id="boss", result="did it", status="done")
        fake_tmux.sessions.append(session(wws.tmux_session, pane("%2", wws.path), attached=True))

    reap.run(db)
    reap.run(db)

    stopped = case == "idle"
    assert (db.get_agent("boss").status == "paused") == stopped
    assert (agents.latest_paused(db, ws) is not None) == stopped   # `brindle continue` finds it
    items = inbox._idle_items(db, str(repo))
    assert [i.root_id for i in items] == (["boss"] if stopped else [])
    if stopped:
        db.set_status("boss", "idle")   # continued: the Inbox item goes
        assert inbox._idle_items(db, str(repo)) == []


@pytest.mark.parametrize("case,gone", [
    ("reviewer with its verdict", True),
    ("reviewer in an attached session", False),
    ("coordinator paused two days", True),
    ("coordinator paused an hour", False),
])
def test_leftover_reviewers_and_coordinator_rows(db, repo, fake_tmux, case, gone):
    ws = workspaces.adopt_root(db, str(repo))
    if case.startswith("reviewer"):
        add(db, ws, "x1", mode="review", result="APPROVED", tmux_window="%5")
        live(fake_tmux, "%5")
        fake_tmux.sessions = [session("brindle_proj_rev", pane("%5", repo, cmd="claude"),
                                      attached="attached" in case)]
    else:
        since = NOW - (2 * 86400 if "two days" in case else 3600)
        add(db, ws, "x1", mode="coordinator", profile="coordinator", status="paused",
            created_at=since, status_since=since)

    reap.run(db)

    a = db.get_agent("x1")
    assert (a is None or a.dismissed_at is not None) == gone


@pytest.mark.variant("race: a client, uncommitted work or input arriving between planning and the stop keeps the session")
@pytest.mark.parametrize("change", ["a client attaches", "uncommitted work appears", "input arrives",
                                    "another home takes it"])
def test_what_changed_since_planning_is_not_reaped(db, repo, tmp_path, fake_tmux, monkeypatch, change):
    """Planning (git probes and all) takes a while: a stop acts on the state
    just before it, not on the plan's snapshot (a session's home included,
    which session_infos otherwise reads once per session)."""
    home = [str(brindle_home())]
    if change == "another home takes it":
        row = f"brindle_proj_old\t0\t{OLD:.0f}\t{OLD:.0f}\t%7\t4242\t0\t{OLD:.0f}\tzsh\t{repo}\t"
        monkeypatch.setattr(tmux, "_tmux", lambda *args, **k: SimpleNamespace(
            returncode=0, stdout=row + "\n" if args[0] == "list-panes" else f"BRINDLE_HOME={home[0]}\n"))
        monkeypatch.setattr(tmux, "_SESSION_HOMES", {})
        monkeypatch.setattr(tmux, "session_infos", SESSION_INFOS)
    elif change == "input arrives":
        ws = workspaces.adopt_root(db, str(repo))
        add(db, ws, "boss", mode="interactive", profile="supervisor", tmux_window="%1")
        live(fake_tmux, "%1")
        fake_tmux.sessions = [session("brindle_proj_main", pane("%1", repo, cmd="claude"))]
    else:
        fake_tmux.sessions = [session("brindle_proj_old", pane("%7", repo))]
    planned = reap.plan

    def then_changed(*a, **k):
        out = planned(*a, **k)
        if change == "another home takes it":     # another brindle's ensure_session, in its own process
            home[0] = "/another/home"
            return out
        [s] = fake_tmux.sessions
        if change == "a client attaches":
            s.attached = True
        elif change == "uncommitted work appears":
            (repo / "wip.py").write_text("x = 1\n")
        else:
            s.activity = time.time()   # input as the stop runs, not at the module's import
        return out
    monkeypatch.setattr(reap, "plan", then_changed)

    assert reap.run(db) == []
    assert fake_tmux.killed == [] and fake_tmux.stopped == []
    if change == "input arrives":
        assert db.get_agent("boss").status != "paused"


@pytest.mark.variant("race: state that changes during the re-plan's own git probes keeps the session")
@pytest.mark.parametrize("probe", ["workspaces_merged", "contained"])
def test_what_changes_during_the_revalidation_probe_is_not_reaped(db, repo, fake_tmux, monkeypatch, probe):
    """The re-plan's own git probes take a while too: the stop acts on safety
    state read after them, not on the listing taken before them."""
    from pathlib import Path

    ws = workspaces.create(db, str(repo), "feat-race").workspace
    db.conn.execute("UPDATE workspaces SET created_at=? WHERE id=?", (OLD, ws.id))
    monkeypatch.setattr(tmux, "window_alive", lambda *a, **k: True)
    if probe == "workspaces_merged":   # a finished worker's session: a client attaches
        add(db, ws, "w1", tmux_window="%3", result="did it")
    else:                              # a superseded worker: input arrives in its pane
        add(db, ws, "boss", mode="interactive", profile="supervisor", tmux_window="%1", status_since=NOW)
        add(db, ws, "w1", parent_id="boss", tmux_window="%3", task="Add a greeting")
        monkeypatch.setattr(agents, "send_message", lambda *a, **k: None)
        _commit(Path(ws.path), "def greet():\n    return 'hi'\n")
        _commit(repo, "def greet():\n    return 'hi'\n", when=NOW, msg="squashed feat-race")
        live(fake_tmux, "%1")
    live(fake_tmux, "%3")
    fake_tmux.sessions = [session(ws.tmux_session, pane("%3", ws.path, cmd="claude"))]
    real = getattr(cull if probe == "workspaces_merged" else reap, probe)
    calls = []

    def slow_probe(w):
        calls.append(w)
        if len(calls) == 2:   # the revalidation pass, after its fresh listing
            if probe == "workspaces_merged":
                fake_tmux.sessions[0].attached = True
            else:
                fake_tmux.sessions[0].panes[0].activity = time.time()
        return real(w)
    monkeypatch.setattr(cull if probe == "workspaces_merged" else reap, probe, slow_probe)

    reap.run(db)

    assert len(calls) >= 2
    assert fake_tmux.killed == [] and fake_tmux.stopped == []
    assert db.get_agent("w1").dismissed_at is None


def _commit(cwd, text, when=NOW - 2 * 86400, msg="change"):
    (cwd / "app.py").write_text(text)
    sh(f"GIT_AUTHOR_DATE='@{int(when)} +0000' GIT_COMMITTER_DATE='@{int(when)} +0000' git commit -qam '{msg}'", cwd)


@pytest.mark.parametrize("case", [
    "contained",
    pytest.param("recently active", marks=pytest.mark.variant("A superseded worker with recent activity must not be stopped")),
    "uncommitted changes", "similar",
    pytest.param("worker in an attached session", marks=pytest.mark.variant("A superseded worker still attached by the person must not be stopped")),
    pytest.param("worker on another tmux server", marks=pytest.mark.variant(
        "its pane id names some other pane in this server's listing")),
])
def test_superseded_workers(db, repo, fake_tmux, monkeypatch, case):
    from pathlib import Path

    ws = workspaces.create(db, str(repo), "feat-dup").workspace
    db.conn.execute("UPDATE workspaces SET created_at=? WHERE id=?", (OLD, ws.id))
    add(db, ws, "boss", mode="interactive", profile="supervisor", tmux_window="%1", status_since=NOW)
    add(db, ws, "w1", parent_id="boss", tmux_window="%2", task="Add a greeting function to app.py",
        status_since=NOW - 3600 if case == "recently active" else OLD,
        tmux_server="brindle-elsewhere" if "another tmux server" in case else None)
    live(fake_tmux, "%1", "%2")
    monkeypatch.setattr(tmux, "window_alive", lambda *a, **k: True)   # alive on its own server
    fake_tmux.sessions = [session("brindle_proj_main", pane("%1", repo), attached=True),
                          session(ws.tmux_session, pane("%2", ws.path, cmd="claude"),
                                  attached="attached session" in case)]
    told = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: told.append((to, body)))
    _commit(Path(ws.path), "def greet():\n    return 'hi'\n")
    if case == "similar":
        sh("git checkout -qb feat-other", repo)
        _commit(repo, "def greeting(name):\n    return name\n", when=NOW)
        sh("git checkout -q main && git merge -q --no-ff feat-other -m \"Merge branch 'feat-other'\"", repo)
        db.add_task(Task("t2", str(repo), None, "boss", ws.id, "developer",
                         "Add greeting function in app.py for users", "assign", 1, "feat-other",
                         None, None, None, "merged", NOW))
    else:   # squash-merged into main under another commit
        _commit(repo, "def greet():\n    return 'hi'\n", when=NOW, msg="squashed feat-dup")
    if case == "uncommitted changes":
        (Path(ws.path) / "notes.txt").write_text("todo\n")
        os.utime(Path(ws.path) / "notes.txt", (OLD, OLD))

    reap.run(db)
    reap.run(db)

    w = db.get_agent("w1")
    assert (w.dismissed_at is not None) == (case == "contained")
    head = sh("git rev-parse main", repo)
    if case == "contained":
        assert w.result.startswith("Superseded") and head[:10] in told[0][1]
    if case == "similar":
        assert "feat-other" in told[0][1] and "1 of 1" in told[0][1]
    quiet = case in ("recently active", "worker in an attached session", "worker on another tmux server")
    assert len(told) == (0 if quiet else 1) and all(to == "boss" for to, _ in told)


def test_a_sweep_already_reaping_is_left_to_it(db, tmp_path, fake_tmux):
    """Every sidebar sweeps: a second reaper waits its turn instead of acting on
    the same plan (a once-only notice sent twice, a stop made twice)."""
    fake_tmux.sessions = [session("brindle_proj_old", pane("%7", tmp_path))]
    lock = brindle_home() / "locks" / "reap.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)   # another sweep mid-reap
        assert reap.run(db) == [] and fake_tmux.killed == []
    assert reap.run(db) and fake_tmux.killed == ["brindle_proj_old"]
