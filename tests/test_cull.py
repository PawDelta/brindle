import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import run_scenario

from brindle import agents, cull, procs, status_cache, tmux, view, workspaces
from brindle.db import Agent, Workspace

SLEEPER = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"


def launch_as(agent_id, *, argv_config=True, env_only=False):
    """A stand-in for an agent's CLI: named in its command line the way brindle
    launches Claude Code (unless env_only), with a child process of its own."""
    args = [sys.executable, "-c", SLEEPER]
    if argv_config:
        env_block = {"BRINDLE_AGENT_ID": agent_id}
        if os.environ.get("BRINDLE_HOME"):
            env_block["BRINDLE_HOME"] = os.environ["BRINDLE_HOME"]
        args.append(json.dumps({"mcpServers": {"brindle": {"env": env_block}}}))
    env = {**os.environ}
    if env_only:
        env["BRINDLE_AGENT_ID"] = agent_id
    proc = subprocess.Popen(args, env=env, start_new_session=True)
    # Reaped the moment it exits: on macOS an unreaped child still answers
    # kill(0), so procs.terminate would wait out its whole grace period.
    threading.Thread(target=proc.wait, daemon=True).start()
    # Wait for its child: a fixed sleep loses the race on a loaded machine
    # (a parallel test run).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True).stdout.strip():
            break
        time.sleep(0.05)
    return proc


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check it's really running.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(out.strip()) and not out.strip().startswith("Z")


@pytest.fixture
def proc_cleanup():
    started = []
    yield started
    import signal

    for p in started:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # the stand-in and the child it started
        except (ProcessLookupError, PermissionError):
            # Already gone, or (macOS) a group whose exited leader isn't reaped yet.
            p.kill()
        p.wait(timeout=5)


@pytest.fixture(autouse=True)
def no_process_table(request, monkeypatch):
    """Each sweep lists every process on the machine with ``ps`` (seconds on a
    loaded one). No agent process runs here unless the test starts one with
    launch_as (and proc_cleanup), so the others sweep an empty table."""
    if "proc_cleanup" not in request.fixturenames:
        monkeypatch.setattr(procs, "table", lambda: {})


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def add(db, ws, agent_id, **kw):
    fields = dict(profile="developer", provider="claude", parent_id=None, mode="assign",
                  status="idle", tmux_window="@999", result=None, created_at=time.time() - 7200)
    fields.update(kw)
    db.add_agent(Agent(agent_id, ws.id, **fields))




def test_sweep_stops_processes_of_paused_agents(db, ws, proc_cleanup):
    add(db, ws, "abc12345", status="paused")
    p = launch_as("abc12345")
    proc_cleanup.append(p)
    notes = cull.sweep(db)
    p.wait(timeout=5)
    assert any("leftover" in n for n in notes)


@pytest.mark.variant("the only assertion that procs.terminate escalates to SIGKILL after the grace period; the sweep tests run these lines without checking the signal order")
def test_a_process_ignoring_sigterm_is_killed_after_the_grace_period(monkeypatch):
    import signal

    sent = []
    monkeypatch.setattr(procs, "os", SimpleNamespace(kill=lambda pid, sig: sent.append((pid, sig))))
    monkeypatch.setattr(procs, "alive", lambda pid: True)   # it ignores SIGTERM: still running
    assert procs.terminate([4242], grace=0) == 1
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_sweep_pauses_agents_whose_window_is_gone(db, ws, proc_cleanup, monkeypatch):
    """Killing the chat's tmux session also kills the wrapper that would have
    paused its session (agents._pause_when_done): the sweep must stop every
    agent the chat started, and nothing of another session."""
    add(db, ws, "abc12345", mode="interactive", status="processing", profile="supervisor", tmux_window="%1")
    add(db, ws, "w1", parent_id="abc12345", status="processing", tmux_window="%2", status_since=time.time())
    add(db, ws, "r1", parent_id="w1", mode="review", status="processing", tmux_window="%3",
        status_since=time.time())
    add(db, ws, "other", mode="interactive", status="processing", profile="supervisor", tmux_window="%4")
    add(db, ws, "w2", parent_id="other", status="processing", tmux_window="%5", status_since=time.time())
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"%2": True, "%3": True, "%4": True, "%5": True})  # the chat's pane %1 is gone
    killed = []
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "kill_window", killed.append)
    monkeypatch.setattr(tmux, "kill_session", lambda *a, **k: None)
    # The chat's CLI and its child, as procs.table lists them (stopping a real
    # one is test_sweep_stops_processes_of_paused_agents).
    config = json.dumps({"mcpServers": {"brindle": {"env": {
        "BRINDLE_AGENT_ID": "abc12345", "BRINDLE_HOME": os.environ["BRINDLE_HOME"]}}}})
    table = {900001: procs.Proc(900001, 1, f"claude --mcp-config {config}"),
             900002: procs.Proc(900002, 900001, "python -m pytest")}
    stopped = []
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(procs, "terminate", lambda pids: stopped.extend(pids) or len(pids))
    cull.sweep(db)
    assert sorted(stopped) == [900001, 900002]
    a = db.get_agent("abc12345")
    assert a.status == "paused" and a.dismissed_at is None  # still resumable, still shown
    assert {a: db.get_agent(a).status for a in ("w1", "r1", "other", "w2")} == {
        "w1": "paused", "r1": "paused", "other": "processing", "w2": "processing"}
    assert set(killed) == {"%1", "%2", "%3"}


def test_idle_unreported_worker_noticed_without_goal(db, ws, tmp_path, monkeypatch):
    """A sweep distinguishes idle, busy, covered and undelivered workers in one session."""
    from brindle import gates, git, identity_lock, pipeline

    now = time.time()
    panes = tmux.PaneSnapshot(tmux.current_server())
    workers = [
        ("idle-claude", "claude", "assign", False),
        ("idle-codex", "codex", "assign", False),
        ("idle-review", "claude", "review", False),
        ("idle-fixing", "claude", "assign", True),
    ]
    for name, provider, mode, fixing in workers:
        parent = "boss-" + name
        add(db, ws, parent, mode="interactive", status="processing", profile="supervisor",
            tmux_window="%" + parent)
        db.add_autopilot(parent)
        add(db, ws, name, parent_id=parent, provider=provider, mode=mode,
            status_since=now - 600, tmux_window="%" + name)
        db.update_agent(name, unreported_noted=1)
        panes["%" + name] = panes["%" + parent] = True
        if fixing:
            db.update_agent(name, result="done", pipeline="fixing", pipeline_rounds=1)
            db.add_review(ws.id, "head", "r1", False, "fix the bug")
            db.conn.execute("UPDATE reviews SET created_at=?", (now - 900,))
    sent, original_send = [], agents.send_message
    activity = now
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "window_activity", lambda *a, **k: activity)
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "command_running", lambda *a: False)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append(to))
    monkeypatch.setattr(pipeline, "on_reviewer_unreported",
                        lambda db_, r, ws_: sent.append(r.parent_id) or True)
    monkeypatch.setattr(gates, "head", lambda *a: "head")
    cull.sweep(db)
    expected = {"boss-idle-claude", "boss-idle-review", "boss-idle-fixing"}
    assert len(sent) == 3 and set(sent) == expected  # Codex's fresh output keeps it active
    activity = now - 600
    cull.sweep(db)
    cull.sweep(db)
    assert len(sent) == 4 and set(sent) == expected | {"boss-idle-codex"}

    # Those workers finish; now distinguish lost receipts from unreported commits.
    for name, _, _, _ in workers:
        db.update_agent(name, status="done")
    notice_ws = Workspace("notices", str(tmp_path), "work", "worktree", "work", "main",
                          str(tmp_path), None, "fake", 1)
    db.add_workspace(notice_ws)
    cases = ("received", "fresh", "commits", "more-commits", "covered", "active", "command", "locked")
    for case in cases:
        parent = "notice-boss-" + case
        add(db, notice_ws, parent, mode="interactive", status="processing", headless=1,
            tmux_window="%" + parent)
        add(db, notice_ws, case, parent_id=parent, status_since=1, tmux_window="%" + case,
            provider="codex" if case == "active" else "claude")
        panes["%" + case] = panes["%" + parent] = True
        if case in ("received", "fresh", "covered", "more-commits", "locked"):
            row = db.outbox_receive(case, "digest", "done")
            if case in ("covered", "more-commits"):
                db.outbox_bind(row["id"], "head" if case == "covered" else "previous")
                db.outbox_accept(row["id"], case, "done")
                db.outbox_deliver(row["id"], nothing=True)
            else:
                db.set_status(case, "processing")  # receipt notices don't depend on idle panes
                if case != "fresh":
                    db.conn.execute("UPDATE report_outbox SET updated_at=1 WHERE id=?", (row["id"],))
    monkeypatch.setattr(agents, "send_message", original_send)
    monkeypatch.setattr(git, "ok", lambda *a: True)
    monkeypatch.setattr(git, "run", lambda argv, *a: SimpleNamespace(stdout="a b" if argv[0] == "rev-list" else ""))
    monkeypatch.setattr(status_cache, "cached_any_status", lambda path, base: git.Status(None, base, 0, 0, [], None))
    monkeypatch.setattr(agents, "command_running", lambda db_, a: a.id == "command")
    activity = now
    locked = [True]
    monkeypatch.setattr(identity_lock, "locked_by",
                        lambda aid: "other" if aid == "notice-boss-locked" and locked else None)

    def counts():
        return {case: db.pending_count("notice-boss-" + case) for case in cases}

    cull.note_unreported_commits(db, now, panes)
    expected = dict(received=1, fresh=0, commits=1, covered=0, active=0, command=0, locked=0)
    expected["more-commits"] = 1
    assert counts() == expected
    locked.clear()
    cull.note_unreported_commits(db, now, panes)
    cull.note_unreported_commits(db, now, panes)
    expected["locked"] = 1
    assert counts() == expected
    row = db.outbox_receive("received", "next", "new report")
    db.conn.execute("UPDATE report_outbox SET updated_at=1 WHERE id=?", (row["id"],))
    cull.note_unreported_commits(db, now, panes)
    expected["received"] = 2  # each undelivered receipt has its own notice
    assert counts() == expected


def _live_panes():
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"@998": True, "@999": True})
    return panes


def test_idle_worker_changing_files_is_not_alerted(db, ws, monkeypatch, name="café.py"):
    """Idle by its hooks, but something it started is still editing its worktree."""
    add(db, ws, "boss", mode="interactive", status="processing", profile="supervisor", tmux_window="@998")
    add(db, ws, "w1", parent_id="boss", status_since=time.time() - 600)
    wip = os.path.join(ws.path, name)
    with open(wip, "w") as f:
        f.write("x = 1\n")
    sent = []
    monkeypatch.setattr(tmux, "window_activity", lambda target, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append(to))
    cull.note_idle_unreported(db, time.time(), _live_panes())
    assert sent == []
    os.utime(wip, (time.time() - 600, time.time() - 600))
    cull.note_idle_unreported(db, time.time(), _live_panes())
    assert sent == ["boss"]


@pytest.mark.parametrize("problem", ["unpicked", "inbox"])
def test_reported_branch_problems_reach_the_supervisor(db, ws, monkeypatch, problem):
    """#135: a worker reported, then its work went nowhere: its branch waited
    for a review nobody started, or messages to it sat undelivered. The
    supervisor hears once, with what to do."""
    add(db, ws, "boss", mode="interactive", status="processing", profile="supervisor", tmux_window="@998")
    add(db, ws, "w1", parent_id="boss", result="done")
    sent = []
    monkeypatch.setattr(tmux, "window_activity", lambda target, **k: None)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append((to, body)))
    later = time.time()
    if problem == "unpicked":
        # Reported long after its last commit: the wait starts at the report.
        reported = time.time() + cull.UNPICKED_AFTER
        db.update_agent("w1", pipeline="reviewing", reviewing_since=reported)
        cull.note_reported_stuck(db, reported + 60, _live_panes())
        assert sent == []
        later = reported + cull.UNPICKED_AFTER + 60
    else:
        db.enqueue("w1", "changes requested", "r1")
        db.conn.execute("UPDATE inbox SET created_at=?", (time.time() - 600,))
    cull.note_reported_stuck(db, later, _live_panes())
    cull.note_reported_stuck(db, later, _live_panes())
    assert len(sent) == 1 and sent[0][0] == "boss" and ws.id in sent[0][1]


@pytest.mark.parametrize("elapsed,status,expected", [
    ("12m 4s", "processing", 0),   # under the limit: nothing yet
    ("41m 9s", "processing", 1),   # long: told once, however many sweeps read it
    ("41m 9s", "waiting", 1),      # a prompt answered in the terminal leaves it 'waiting' meanwhile
])
def test_long_running_command_reaches_the_supervisor_once(db, ws, monkeypatch, elapsed, status, expected):
    add(db, ws, "boss", mode="interactive", status="processing", profile="supervisor", tmux_window="@998")
    add(db, ws, "w1", parent_id="boss", status=status)
    sent = []
    screen = ("⏺ Bash(./slow.sh --token sk-ant-api03-abcdefghijklmnopqrstuv\n  more)\n"
              f"  ⎿  Running… ({elapsed} · esc to interrupt)\n")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append((to, body)))
    # Two overlapping sweeps, each from its own stale read of the worker, whose
    # estimates of the command's start fall either side of a minute boundary.
    snapshot = db.list_agents()
    monkeypatch.setattr(db, "list_agents", lambda *a, **k: snapshot)
    now = 6000.0 + 2469
    cull.note_long_commands(db, now, _live_panes())
    cull.note_long_commands(db, now - 1, _live_panes())
    assert len(sent) == expected
    if expected:
        assert sent[0][0] == "boss" and "./slow.sh" in sent[0][1] and "sk-ant" not in sent[0][1]
        assert "41 min" in sent[0][1]


@pytest.mark.parametrize("case,exit_,cause", [
    ("sweep", (143, None), "status 143 (SIGTERM)"),     # the CLI's wrapper shell exited 128 + 15
    ("leftover", (None, 15), "signal 15 (SIGTERM)"),    # the CLI itself was the pane's program
    ("overlap", (143, None), "status 143 (SIGTERM)"),   # two sweeps read it 'processing'
    ("report", (143, None), None),                      # its report lands after the read
])
def test_dead_pane_marks_agent_stopped(db, ws, monkeypatch, case, exit_, cause):
    """A worker's CLI killed outside brindle leaves its pane dead ("Pane is
    dead (status 143)") with no process left for step 1 to find: it stayed
    'processing' forever and its supervisor never heard. Now it's failed and
    its supervisor told once, however many sweeps saw it; one that had
    reported first, even a moment before the claim, is just done. A helper
    process it left behind is stopped without step 1 turning 'failed' back
    into 'paused'."""
    add(db, ws, "boss", mode="interactive", status="processing", profile="supervisor", tmux_window="%1")
    add(db, ws, "w1", parent_id="boss", status="processing", tmux_window="%2",
        status_since=time.time() - 120)
    add(db, ws, "w2", parent_id="boss", status="idle", tmux_window="%3", result="done",
        status_since=time.time() - 120)
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"%1": True, "%2": False, "%3": False})
    panes.exits = {"%2": exit_, "%3": (143, None)}
    sent = []
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "kill_window", lambda *a, **k: None)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append((to, body)))
    stopped = []
    monkeypatch.setattr(procs, "all_agent_ids", lambda table: ["w1"] if case == "leftover" else [])
    monkeypatch.setattr(procs, "agent_pids", lambda ids, procs=None: {i: {4242} for i in ids})
    monkeypatch.setattr(procs, "terminate", lambda pids: stopped.extend(pids) or len(pids))
    if case in ("overlap", "report"):
        stale = db.list_agents()   # what both sweeps read before either acted
        monkeypatch.setattr(db, "list_agents", lambda *a, **k: stale)
        if case == "report":
            db.set_result("w1", "done")
        cull.note_dead_panes(db, time.time(), panes)
        cull.note_dead_panes(db, time.time(), panes)
    else:
        cull.sweep(db)
        cull.sweep(db)
    assert stopped == ([4242, 4242] if case == "leftover" else [])
    assert db.get_agent("w2").status == "done"
    w1 = db.get_agent("w1")
    if cause is None:
        assert w1.status == "done" and sent == []
        return
    assert w1.status == "failed" and w1.failed_why == f"exited: {cause}"
    assert [to for to, _ in sent] == ["boss"]
    assert all(s in sent[0][1] for s in ("w1", ws.branch, cause, "rewind"))


# -- finished work leaves the live view ---------------------------------------


def worktree_row(db, repo, name, path, created_at=None):
    ws = Workspace(f"proj/{name}", str(repo), name, "worktree", name, "main", str(path), None,
                   f"brindle_proj_{name}", time.time() - 7200 if created_at is None else created_at)
    db.add_workspace(ws)
    return ws


@pytest.fixture
def merged(monkeypatch):
    """Every worktree reads as merged and clean; removals are recorded, not done."""
    removed = []
    monkeypatch.setattr(view.status_cache, "cached_status", lambda path, base, now=None:
                        SimpleNamespace(ahead=0))
    monkeypatch.setattr(cull.git, "dirty_files", lambda path: [])
    monkeypatch.setattr(workspaces, "remove", lambda db, ws, **kw:
                        removed.append(ws.id) or SimpleNamespace(branch_deleted=True))
    return removed


def test_sweep_forgets_a_workspace_whose_worktree_is_gone(db, repo, tmp_path):
    """Only `brindle prune` used to: the rows of worktrees deleted outside
    brindle (or by a removal that crashed half way) stayed listed for good."""
    gone = worktree_row(db, repo, "feat-gone", tmp_path / "gone")
    add(db, gone, "w1", status="done", result="did it")
    # One made a moment ago may not have its folder yet: not forgotten.
    fresh = worktree_row(db, repo, "feat-fresh", tmp_path / "fresh", created_at=time.time())

    cull.sweep(db)

    assert db.get_workspace(gone.id) is None and db.get_agent("w1") is None
    assert db.get_workspace(fresh.id) is not None


def test_a_failing_sweep_step_does_not_keep_an_unused_worktree(db, repo, tmp_path, merged, monkeypatch):
    """#167: one step raising ended the quiet sweep before it retired anything."""
    from brindle.native import serve

    monkeypatch.setattr(serve, "stop_unused", lambda db: 1 / 0)
    ws = worktree_row(db, repo, "feat-unused", tmp_path)

    cull.sweep_quietly(db)

    assert merged == [ws.id]


def test_a_paused_reviewer_does_not_keep_a_merged_worktree(db, repo, tmp_path, merged):
    """A reviewer left paused without a report (its session paused, or a newer
    round replaced it) counted as unfinished, so the merged worktree stayed."""
    ws = worktree_row(db, repo, "feat-merged", tmp_path)
    add(db, ws, "w1", status="done", result="did it")
    add(db, ws, "r1", mode="review", status="paused")

    cull.prune_retired(db)

    assert merged == [ws.id]


def test_a_running_reviewer_on_another_server_keeps_its_worktree(db, repo, tmp_path, merged,
                                                                monkeypatch):
    """The same pane id, live on both servers: this server's owner of it is
    no claim on the other server's reviewer, which is still at work."""
    here = tmux.PaneSnapshot(tmux.current_server())
    there = tmux.PaneSnapshot("other")
    for panes, owner in ((here, "mine"), (there, "r1")):
        panes["%1"] = True
        panes.tags["%1"] = {agents.AGENT_TAG: owner}
    monkeypatch.setattr(tmux, "list_panes", lambda server=None: there if server == "other" else here)
    monkeypatch.setattr(tmux, "window_alive", lambda pane, server=None: server == "other")
    ws = worktree_row(db, repo, "feat-merged", tmp_path)
    add(db, ws, "w1", status="done", result="did it")
    add(db, ws, "r1", mode="review", status="processing", tmux_window="%1", tmux_server="other")

    cull.prune_retired(db, cautious=True)

    assert merged == []


# -- another tmux server's pane list says nothing about this agent -----------
#
# The incident: `brindle demo`, recorded on a private tmux server
# (BRINDLE_TMUX_SOCKET) but against the person's real BRINDLE_HOME, ran a
# sweep. Its pane list had none of the real agents' panes, so every real
# worker past the launch grace period looked stopped: the sweep marked them
# paused and SIGTERMed their processes.


@pytest.fixture
def other_server():
    """A second private tmux server, as a demo recording would use, for the
    cull to run on; the agents under test stay on the test run's server."""
    name = f"brindle-test-{os.getpid()}-other"
    yield name
    tmux.reap_server(name)


def live_pane(ws, agent_id):
    """A real, running pane on the current server, tagged as ``agent_id``'s."""
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    return tmux.new_window(ws.tmux_session, agent_id, ws.path, ["sleep", "60"], {},
                           tag=(agents.AGENT_TAG, agent_id))


def test_sweep_on_another_server_leaves_a_live_agent_alone(db, ws, monkeypatch):
    here = tmux.current_server()
    pane = "%1"
    add(db, ws, "abc12345", status="processing", tmux_window=pane, tmux_server=here)
    config = json.dumps({"BRINDLE_AGENT_ID": "abc12345", "BRINDLE_HOME": os.environ["BRINDLE_HOME"]})
    table = {1: procs.Proc(1, 0, "init"), 11: procs.Proc(11, 1, f"claude --mcp-config {config}")}
    stopped = []
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(procs, "terminate", lambda pids, **k: stopped.append(set(pids)) or len(pids))

    def panes(server=None):
        snapshot = tmux.PaneSnapshot(server or tmux.current_server())
        if snapshot.server == here:
            snapshot[pane] = True
        return snapshot

    monkeypatch.setattr(tmux, "list_panes", panes)
    monkeypatch.setattr(tmux, "window_alive", lambda target, server=None:
                        target == pane and (server or tmux.current_server()) == here)
    a = db.get_agent("abc12345")
    assert a.tmux_server == here and agents.is_alive(a)

    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", "brindle-test-other")
    assert not tmux.list_panes()  # that server knows nothing of the agent's pane
    notes = cull.sweep(db)

    assert notes == []
    assert stopped == [], "its process was stopped"
    a = db.get_agent("abc12345")
    assert a.status == "processing" and a.dismissed_at is None
    # Asked from the other server, it is still known to be running: the
    # answer comes from its own server, not the other one's pane list.
    assert agents.is_alive(a) and agents.is_alive(a, tmux.list_panes())


def test_sweep_on_another_server_leaves_a_stale_looking_worker_alone(db, ws, other_server,
                                                                     monkeypatch):
    # A reported, long-idle worker (closed after stale_after on its own
    # server) and one that would look stopped: both are the other server's
    # to judge. A pane id that happens to exist on both servers must not
    # make the other server's agent look like this one's.
    add(db, ws, "w1", result="done", tmux_window="%0")
    add(db, ws, "w2", status="processing", tmux_window="%1")
    # A row an older brindle made (no tmux_server) counts as the default server's.
    add(db, ws, "abc12345", status="processing", tmux_window="%2")
    with db.tx() as c:
        c.execute("UPDATE agents SET tmux_server=NULL WHERE id='abc12345'")
    config = json.dumps({"BRINDLE_AGENT_ID": "abc12345", "BRINDLE_HOME": os.environ["BRINDLE_HOME"]})
    table = {1: procs.Proc(1, 0, "init"), 11: procs.Proc(11, 1, f"claude --mcp-config {config}")}
    stopped = []
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(procs, "terminate", lambda pids, **k: stopped.append(set(pids)) or len(pids))
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", other_server)
    # Its own (default) server would be the user's real tmux: stand in for it
    # and record which server was asked.
    asked = []
    monkeypatch.setattr(tmux, "window_alive", lambda target, server=None: asked.append(server) or False)
    assert cull.sweep(db) == []
    assert cull.prune_stale_agents(db) == []
    assert None in asked and other_server not in asked
    assert stopped == [], "the default server's agent was judged from another server's panes"
    for aid in ("w1", "w2", "abc12345"):
        a = db.get_agent(aid)
        assert a.dismissed_at is None and a.status == ("idle" if aid == "w1" else "processing")


def test_sweep_retries_a_flush_the_pane_blocked(db, ws, monkeypatch):
    """A flush that found the person typing leaves the message queued; with no
    turn-end hook (agy) nothing else retries it, so the next sweep must once
    the input box is clear, and never while it still holds typed text."""
    add(db, ws, "boss", mode="interactive", provider="antigravity", profile="supervisor",
        tmux_window="@998")
    db.enqueue("boss", "first task", None)
    screen = ["> half typed\n───\n? for shortcuts"]
    pasted = []
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: _live_panes())
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen[0])
    monkeypatch.setattr(tmux, "paste", lambda target, text, **k: pasted.append(text))
    later = time.time() + 600
    assert agents.flush(db, "boss") is False
    cull.sweep(db, now=later)
    assert pasted == [] and db.pending_count("boss") == 1
    screen[0] = "> \n───\n? for shortcuts"
    cull.sweep(db, now=later)
    assert len(pasted) == 1 and "first task" in pasted[0]
    assert db.pending_count("boss") == 0
    cull.sweep(db, now=later)
    assert len(pasted) == 1


def _keeps_held_and_status_read_creates_nothing(db, brindle_home):
    import fcntl
    from brindle import gates

    assert gates.marked("review", "w1") is False
    assert list((brindle_home / "locks").glob("*")) == []
    old = time.time() - cull.LOCK_MAX_AGE - 60
    held, free, slot = (brindle_home / "locks" / n for n in (
        "check-" + "a" * 16 + ".lock", "review-run-" + "b" * 16 + ".lock", "check-slot-0.lock"))
    for p in (held, free, slot):
        p.write_text("")
        os.utime(p, (old, old))
    with open(held) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        assert cull.clean_locks(db) == 1
    assert held.exists() and slot.exists() and not free.exists()


def _race_waiter_relocks_the_new_file(brindle_home, monkeypatch):
    import fcntl
    from brindle import gates

    path = brindle_home / "locks" / "review-run-x.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    real = fcntl.flock
    calls = []

    def flock(fd, op):
        if not calls:   # cleanup unlinks the file the waiter just opened
            calls.append(1)
            path.unlink()
            path.write_text("")
            path.unlink()
        return real(fd, op)

    monkeypatch.setattr(gates.fcntl, "flock", flock)
    with gates._open_locked(path, fcntl.LOCK_SH) as f:
        assert os.path.samestat(os.stat(path), os.fstat(f.fileno()))


def _race_check_lock_waiter_relocks(brindle_home, monkeypatch):
    import fcntl
    from brindle import gates

    real = fcntl.flock
    calls = []

    def flock(fd, op):
        if not calls:   # cleanup unlinks the check lock the waiter just opened
            calls.append(1)
            for p in (brindle_home / "locks").glob("check-*.lock"):
                p.unlink()
        return real(fd, op)

    monkeypatch.setattr(gates.fcntl, "flock", flock)
    with gates._check_lock("/r", "t", "cmd") as result:
        (lock,) = (brindle_home / "locks").glob("check-*.lock")
        assert result.name == lock.name.replace(".lock", ".result")


@pytest.mark.parametrize("scenario", [
    pytest.param(_keeps_held_and_status_read_creates_nothing, id="held"),
    pytest.param(_race_waiter_relocks_the_new_file, id="race-waiter"),
    pytest.param(_race_check_lock_waiter_relocks, id="race-check-lock"),
])
def test_lock_cleanup(request, scenario):
    run_scenario(request, scenario)


def test_stopped_workers_of_a_paused_session_stay_resumable(db, ws):
    add(db, ws, "boss", mode="interactive", status="paused", profile="supervisor")
    add(db, ws, "w1", parent_id="boss", status="paused")
    add(db, ws, "w2", parent_id=None, status="processing")   # its session went on without it
    cull.sweep(db)
    assert db.get_agent("w1").dismissed_at is None
    assert db.get_agent("w2").dismissed_at is not None


def test_pane_ownership_is_per_server(db, ws, other_server, monkeypatch):
    # The same pane id on two servers: neither agent is a claim on the
    # other's pane, and stopping one never touches the other's window.
    here = tmux.current_server()
    pane = live_pane(ws, "mine")
    add(db, ws, "mine", status="processing", tmux_window=pane, created_at=time.time())
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", other_server)
    add(db, ws, "theirs", status="processing", tmux_window=pane, created_at=time.time())
    assert db.get_agent("theirs").tmux_server == other_server
    assert agents.owns_pane(db, db.get_agent("mine"))
    assert agents.owns_pane(db, db.get_agent("theirs"))
    assert not agents.is_alive(db.get_agent("theirs"))  # nothing on its own server
    agents.close(db, "theirs")
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", here)
    assert tmux.window_alive(pane), "closing the other server's agent killed this server's pane"
    assert agents.is_alive(db.get_agent("mine"))


def test_pane_reads_and_keys_go_to_the_agents_own_server(db, ws, other_server, monkeypatch):
    # Reading an agent's screen or typing into its pane goes to the server
    # its window is on, whatever server brindle is using at the time: on any
    # other server the same pane id would be some other pane (or nothing).
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    pane = tmux.new_window(ws.tmux_session, "abc12345", ws.path, ["cat"], {},
                           tag=(agents.AGENT_TAG, "abc12345"))
    add(db, ws, "abc12345", status="processing", tmux_window=pane)
    a = db.get_agent("abc12345")
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", other_server)
    with pytest.raises(tmux.TmuxError):
        tmux.capture(pane, lines=5)  # nothing of the sort on this server
    # The calls the agent's own server is named for reach that server, not the one in use.
    asked, real = [], tmux._tmux
    monkeypatch.setattr(tmux, "_tmux", lambda *args, server=None, **kw: asked.append((args[0], server))
                        or real(*args, server=server, **kw))
    own = agents.server_of(a)
    tmux.send_keys(pane, "-l", "hello from brindle", server=own)
    tmux.capture(pane, lines=5, server=own)
    assert tmux.window_activity(pane, server=own) is not None
    assert asked and {server for _, server in asked} == {own} and own != other_server
    assert tmux.window_activity(pane) is None


def test_idle_sweep_honors_the_unreported_alert_cooldown(db, ws, monkeypatch):
    add(db, ws, "boss", mode="interactive", status="processing", profile="supervisor", tmux_window="@998")
    add(db, ws, "w1", parent_id="boss", mode="assign", status_since=time.time() - 600)
    db.update_agent("w1", unreported_noted=int(time.time()))   # alerted a moment ago
    sent = []
    monkeypatch.setattr(tmux, "window_activity", lambda target, **k: time.time() - 600)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append(to))
    cull.sweep(db)
    assert sent == []
    db.update_agent("w1", unreported_noted=int(time.time()) - agents.UNREPORTED_REALERT_SECONDS - 1)
    cull.sweep(db)
    assert sent == ["boss"]


def test_stale_after_zero_keeps_workers(db, ws, repo, monkeypatch):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text('{"stale_after": 0}')
    add(db, ws, "w1", result="done")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    cull.sweep(db)
    assert db.get_agent("w1").dismissed_at is None


def test_the_daemon_is_never_included():
    config = json.dumps({"BRINDLE_AGENT_ID": "abc12345", "BRINDLE_HOME": os.environ["BRINDLE_HOME"]})
    t = {1: procs.Proc(1, 0, "init"),
         10: procs.Proc(10, 1, f"/usr/bin/claude daemon run --spawned-by {config}"),
         11: procs.Proc(11, 10, f"claude --mcp-config {config}")}
    assert procs.agent_pids(["abc12345"], procs=t)["abc12345"] == {11}
