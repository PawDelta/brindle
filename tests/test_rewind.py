"""Rewind: per-turn worktree snapshots, and restarting a worker from one."""

import asyncio
import json
import os
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, git, rewind, workspaces
from brindle.cli import app
from brindle.db import Agent

from conftest import sh


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def worker(db, ws, *, agent_id="w1", mode="assign", provider="claude", parent=None,
           task="add a login page", **extra):
    a = Agent(agent_id, ws.id, "developer", provider, parent, mode, "processing", "", None,
              time.time(), task=task, **extra)
    db.add_agent(a)
    return a


def write(ws, name, text):
    with open(os.path.join(ws.path, name), "w") as f:
        f.write(text)


def read(ws, name):
    with open(os.path.join(ws.path, name)) as f:
        return f.read()


def stop(db, agent_id):
    """The end of a turn, as Claude Code's Stop hook reports it (stop_hook_active
    keeps the report_result nudge from starting another turn)."""
    return agents.handle_hook(db, agent_id, "stop", {"stop_hook_active": True})


def refs(repo, agent_id):
    return sh(f"git for-each-ref --format='%(refname)' refs/brindle/turns/{agent_id}/", repo).splitlines()


# -- snapshots -----------------------------------------------------------------------


def test_stop_hook_snapshots_a_turn_that_changed_files(db, repo, ws):
    worker(db, ws)
    head = git.out(["rev-parse", "HEAD"], ws.path)
    write(ws, "app.py", "print('login')\n")
    write(ws, "new.txt", "untracked\n")
    stop(db, "w1")

    assert refs(repo, "w1") == ["refs/brindle/turns/w1/1"]
    [(n, sha)] = rewind.turns(str(repo), "w1")
    assert n == 1
    # A commit object off to the side: the branch, HEAD and the index are untouched.
    assert git.out(["rev-parse", "HEAD"], ws.path) == head
    assert git.out(["rev-parse", f"{sha}^"], str(repo)) == head
    assert sh("git diff --cached --name-only", ws.path) == ""
    assert sorted(git.dirty_files(ws.path)) == ["app.py", "new.txt"]
    # The snapshot has both the edit and the untracked file, but not ignored ones.
    assert sh(f"git show {sha}:app.py", repo) == "print('login')"
    assert sh(f"git show {sha}:new.txt", repo) == "untracked"
    assert sh(f"git ls-tree --name-only {sha}", repo).splitlines() == [".gitignore", "app.py", "new.txt"]


def test_no_snapshot_when_nothing_changed(db, repo, ws):
    worker(db, ws)
    stop(db, "w1")
    assert refs(repo, "w1") == []  # a turn that only read code
    write(ws, "app.py", "print('login')\n")
    stop(db, "w1")
    stop(db, "w1")  # a second idle stop (or a duplicate hook) adds nothing
    assert refs(repo, "w1") == ["refs/brindle/turns/w1/1"]


def test_first_turn_that_commits_before_the_hook_is_snapshotted(db, repo, ws):
    worker(db, ws)
    agents.handle_hook(db, "w1", "session-start", {})  # records where it started
    start = git.out(["rev-parse", "HEAD"], ws.path)
    assert sh("git rev-parse refs/brindle/turns/w1/base", repo) == start
    write(ws, "app.py", "print('login')\n")
    sh("git commit -qam login", ws.path)
    stop(db, "w1")
    [row] = rewind.describe(str(repo), "w1")
    assert row["turn"] == 1 and row["files"] == ["app.py"]
    assert row["head"] == git.out(["rev-parse", "HEAD"], ws.path) != start
    # The listing shows only turns, and the rewind goes back to the committed state.
    assert "base" not in rewind.format_turns(str(repo), "w1")
    sh("git commit -q --allow-empty -m later", ws.path)
    rewind.restore(ws, row["sha"])
    assert git.out(["rev-parse", "HEAD"], ws.path) == row["head"]
    assert git.dirty_files(ws.path) == []


def test_without_a_recorded_start_the_fork_from_the_base_branch_is_the_baseline(db, repo, ws):
    worker(db, ws)  # no session-start hook ran (an older launch, say)
    write(ws, "app.py", "print('login')\n")
    sh("git commit -qam login", ws.path)
    stop(db, "w1")
    assert [n for n, _ in rewind.turns(str(repo), "w1")] == [1]
    assert rewind.describe(str(repo), "w1")[0]["files"] == ["app.py"]


def test_a_commit_is_a_change_too(db, repo, ws):
    worker(db, ws)
    write(ws, "app.py", "print('login')\n")
    stop(db, "w1")
    sh("git commit -qam login", ws.path)
    stop(db, "w1")
    rows = rewind.describe(str(repo), "w1")
    assert [r["turn"] for r in rows] == [1, 2]
    assert rows[1]["head"] == git.out(["rev-parse", "HEAD"], ws.path)
    assert rows[1]["files"] == []  # same files as turn 1, now committed


def test_snapshot_summary_comes_from_the_transcript(db, repo, ws, tmp_path):
    t = tmp_path / "w1.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "add a login page"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {}}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Added the login view in app.py; tests next."}]}},
    ]
    t.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    worker(db, ws, transcript_path=str(t))
    write(ws, "app.py", "print('login')\n")
    stop(db, "w1")
    [row] = rewind.describe(str(repo), "w1")
    assert row["files"] == ["app.py"]
    assert row["said"] == "Added the login view in app.py; tests next."
    assert "Added the login view" in rewind.format_turns(str(repo), "w1")


def test_only_workers_in_their_own_worktree_get_snapshots(db, repo, ws):
    worker(db, ws, agent_id="boss", mode="interactive")
    worker(db, ws, agent_id="rev", mode="review")
    write(ws, "app.py", "print('login')\n")
    stop(db, "boss")
    stop(db, "rev")
    assert refs(repo, "boss") == [] and refs(repo, "rev") == []
    main = workspaces.adopt_root(db, str(repo))
    worker(db, main, agent_id="w2")
    write(main, "app.py", "print('main')\n")
    stop(db, "w2")
    assert refs(repo, "w2") == []


def test_codex_turn_complete_snapshots_too(db, repo, ws):
    worker(db, ws, provider="codex")
    write(ws, "app.py", "print('login')\n")
    agents.handle_hook(db, "w1", "codex-notify", {"type": "agent-turn-complete"})
    assert refs(repo, "w1") == ["refs/brindle/turns/w1/1"]


def test_a_snapshot_failure_never_breaks_the_hook(db, repo, ws, monkeypatch):
    worker(db, ws)
    db.enqueue("w1", "next message", None)
    monkeypatch.setattr(rewind, "snapshot", lambda *a, **k: 1 / 0)
    out = agents.handle_hook(db, "w1", "stop", {})
    assert out == {"decision": "block", "reason": "next message"}


# -- restoring --------------------------------------------------------------------------


def two_turns(db, repo, ws):
    """Turn 1: an edit and a new file. Turn 2: committed, plus another edit
    and another new file."""
    worker(db, ws)
    agents.handle_hook(db, "w1", "session-start", {})
    start = git.out(["rev-parse", "HEAD"], ws.path)
    write(ws, "app.py", "print('turn 1')\n")
    write(ws, "one.txt", "1\n")
    stop(db, "w1")
    sh("git add -A && git commit -qm 'turn 1 work'", ws.path)
    write(ws, "app.py", "print('turn 2')\n")
    write(ws, "two.txt", "2\n")
    stop(db, "w1")
    assert [n for n, _ in rewind.turns(str(repo), "w1")] == [1, 2]
    return start


def test_restore_puts_branch_and_files_back(db, repo, ws):
    start = two_turns(db, repo, ws)
    snaps = dict(rewind.turns(str(repo), "w1"))
    rewind.restore(ws, snaps[1])
    assert git.out(["rev-parse", "HEAD"], ws.path) == start
    assert git.current_branch(ws.path) == "feature"
    assert read(ws, "app.py") == "print('turn 1')\n"
    assert read(ws, "one.txt") == "1\n"
    assert not os.path.exists(os.path.join(ws.path, "two.txt"))
    # Exactly the worker's state then: an edit and an untracked file, a clean index.
    assert sorted(git.dirty_files(ws.path)) == ["app.py", "one.txt"]
    assert sh("git status --porcelain", ws.path).splitlines() == ["M app.py", "?? one.txt"]  # sh strips
    assert sh("git diff --cached --name-only", ws.path) == ""
    # Forward again works just as well.
    rewind.restore(ws, snaps[2])
    assert git.out(["log", "-1", "--format=%s"], ws.path) == "turn 1 work"
    assert read(ws, "app.py") == "print('turn 2')\n" and read(ws, "two.txt") == "2\n"


# -- the rewind -------------------------------------------------------------------------


@pytest.fixture
def spawned(db, monkeypatch):
    """agents.spawn stands in: no CLI is started, the call is recorded and a
    new agent row is made as the real one would."""
    calls = []

    def fake_spawn(db_, ws, profile_name, *, prompt=None, parent_id=None, mode="interactive",
                   done_when=None, **kwargs):
        calls.append({"ws": ws, "profile": profile_name, "prompt": prompt, "parent_id": parent_id,
                      "mode": mode, "done_when": done_when, **kwargs})
        a = Agent(f"new{len(calls)}", ws.id, profile_name, "claude", parent_id, mode, "processing",
                  "", None, time.time(), task=prompt, done_when=done_when)
        db_.add_agent(a)
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return calls


def test_rewind_resets_the_worktree_and_starts_a_fresh_briefed_worker(db, repo, ws, spawned):
    start = two_turns(db, repo, ws)
    db.update_agent("w1", parent_id="boss", done_when="pytest passes", mode="handoff")

    fresh = rewind.rewind(db, "w1", 1, profile="developer-heavy", note="use the existing auth module")

    assert git.out(["rev-parse", "HEAD"], ws.path) == start
    assert read(ws, "app.py") == "print('turn 1')\n"
    assert not os.path.exists(os.path.join(ws.path, "two.txt"))
    [call] = spawned
    assert call["ws"].id == ws.id and call["profile"] == "developer-heavy"
    assert call["parent_id"] == "boss" and call["done_when"] == "pytest passes"
    assert call["mode"] == "assign"  # the result goes to the parent as a message
    brief = call["prompt"]
    assert brief.startswith("add a login page")
    assert "turn 1" in brief and "Turn 1 (changed: app.py, one.txt)" in brief
    assert "Turn 2" not in brief  # rewound past it
    assert "use the existing auth module" in brief
    # The record keeps the original task, not the brief, so a later rewind
    # (or a reviewer) sees what was really asked.
    assert fresh.task == "add a login page" and db.get_agent(fresh.id).task == "add a login page"
    old = db.get_agent("w1")
    assert old.status == "rewound" and old.dismissed_at
    # Turn 1 (and the starting point) is now the fresh worker's too, so it can
    # be rewound further back.
    assert [n for n, _ in rewind.turns(str(repo), fresh.id)] == [1]
    assert sh(f"git rev-parse refs/brindle/turns/{fresh.id}/base", repo) == start
    assert dict(rewind.turns(str(repo), fresh.id))[1] == dict(rewind.turns(str(repo), "w1"))[1]


def test_rewind_keeps_the_profile_by_default_and_the_brief_quotes_the_turns(db, repo, ws, spawned, tmp_path):
    t = tmp_path / "w1.jsonl"
    worker(db, ws, transcript_path=str(t))
    for k in (1, 2):
        t.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"I finished step {k}."}]}}) + "\n")
        write(ws, "app.py", f"print({k})\n")
        stop(db, "w1")
    fresh = rewind.rewind(db, "w1", 2)
    assert fresh.profile == "developer"
    brief = spawned[0]["prompt"]
    assert "I finished step 1." in brief and "I finished step 2." in brief
    assert "From the person rewinding you" not in brief


def test_rewind_refuses_what_it_cannot_do(db, repo, ws, spawned):
    worker(db, ws)
    with pytest.raises(rewind.RewindError, match="no turn snapshots"):
        rewind.rewind(db, "w1", 1)
    write(ws, "app.py", "x\n")
    stop(db, "w1")
    with pytest.raises(rewind.RewindError, match="no turn 3; its snapshots: 1"):
        rewind.rewind(db, "w1", 3)
    with pytest.raises(rewind.RewindError, match="no agent profile named 'no-such-profile'"):
        rewind.rewind(db, "w1", 1, profile="no-such-profile")
    worker(db, ws, agent_id="rev", mode="review")
    with pytest.raises(rewind.RewindError, match="only workers"):
        rewind.rewind(db, "rev", 1)
    with pytest.raises(agents.AgentError):
        rewind.rewind(db, "nobody", 1)
    assert spawned == [] and read(ws, "app.py") == "x\n"  # nothing was touched


def test_rewind_stops_a_running_worker_first(db, repo, ws, spawned, monkeypatch):
    worker(db, ws)
    write(ws, "app.py", "x\n")
    stop(db, "w1")
    stopped = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "w1")
    monkeypatch.setattr(agents, "_stop", lambda db_, a: stopped.append(a.id))
    rewind.rewind(db, "w1", 1)
    assert stopped == ["w1"]


# -- cleanup ----------------------------------------------------------------------------


def test_removing_the_workspace_deletes_the_refs(db, repo, ws):
    worker(db, ws)
    worker(db, ws, agent_id="w2")
    agents.handle_hook(db, "w1", "session-start", {})
    write(ws, "app.py", "x\n")
    stop(db, "w1")
    stop(db, "w2")
    assert len(refs(repo, "w1")) == 2 and len(refs(repo, "w2")) == 1  # w1 has its baseline too
    workspaces.remove(db, ws, force=True)
    assert refs(repo, "w1") == [] and refs(repo, "w2") == []
    assert sh("git for-each-ref refs/brindle/", repo) == ""


# -- the supervisor's tools and the command ----------------------------------------------


def test_mcp_tools(db, repo, ws, spawned, monkeypatch):
    from brindle import mcp_server

    assert "No agent" in mcp_server.agent_turns("nobody")
    worker(db, ws)
    assert "no turn snapshots" in mcp_server.agent_turns("w1")
    write(ws, "app.py", "x\n")
    stop(db, "w1")
    assert "turn   1" in mcp_server.agent_turns("w1")
    out = asyncio.run(mcp_server.rewind_agent("w1", 5))
    assert out.startswith("Not rewound: w1 has no turn 5")
    # Only the worker's own supervisor (or the session root) may rewind it.
    worker(db, ws, agent_id="boss", mode="interactive")
    worker(db, ws, agent_id="other", mode="interactive")
    db.update_agent("w1", parent_id="boss")
    for who in ("other", "w1"):
        monkeypatch.setenv("BRINDLE_AGENT_ID", who)
        assert "isn't yours to rewind" in asyncio.run(mcp_server.rewind_agent("w1", 1))
    assert spawned == []
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    out = asyncio.run(mcp_server.rewind_agent("w1", 1, agent_profile="developer-heavy", note="try again"))
    assert "Rewound w1 to turn 1" in out and "new1 (developer-heavy/claude)" in out
    assert "try again" in spawned[0]["prompt"]


def test_mcp_server_lists_the_tools():
    from brindle import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"rewind_agent", "agent_turns"} <= names


def test_cli(db, repo, ws, spawned):
    worker(db, ws)
    res = CliRunner().invoke(app, ["agent", "turns", "w1"])
    assert res.exit_code == 0 and "no turn snapshots" in res.output
    write(ws, "app.py", "x\n")
    stop(db, "w1")
    res = CliRunner().invoke(app, ["agent", "turns", "w1"])
    assert res.exit_code == 0 and "turn   1" in res.output and "app.py" in res.output
    res = CliRunner().invoke(app, ["agent", "rewind", "w1", "--to", "2"])
    assert res.exit_code == 1 and "no turn 2" in res.output
    res = CliRunner().invoke(app, ["agent", "rewind", "w1", "--to", "1", "--profile", "developer-heavy",
                                   "--note", "smaller steps"])
    assert res.exit_code == 0, res.output
    assert "rewound w1 to turn 1" in res.output and "new1 (developer-heavy/claude)" in res.output
    assert "smaller steps" in spawned[0]["prompt"]
