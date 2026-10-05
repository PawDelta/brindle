"""Issue #49: an Antigravity worker's warm-up, task and messages must reach
its own pane, never the Claude Code inbox of the session that launched it;
and assign/handoff say why a worker couldn't be started."""

import asyncio
import re
import subprocess
import time

import pytest

from brindle import agents, antigravity, inbox, mcp_server, workspaces
from brindle.db import Agent
from brindle.providers import Antigravity

IDLE_SCREEN = "Accept-edits mode\n> \n? for shortcuts\n"
PROFILE = "---\nname: developer-antigravity\nprovider: antigravity\npermission_mode: acceptEdits\n---\nYou are a developer agent.\n"


@pytest.fixture
def ws(db, repo):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false}')
    return workspaces.adopt_root(db, str(repo))


@pytest.fixture
def launcher_inbox(tmp_path, monkeypatch):
    """The launcher is a Claude Code session: its inbox is in the environment
    of everything it runs (the brindle CLI, brindle's MCP server). Returns what
    was sent to any inbox."""
    sock = tmp_path / "supervisor.sock"
    sock.write_text("")
    monkeypatch.setenv(inbox.SOCKET_VAR, str(sock))
    monkeypatch.setenv(inbox.TOKEN_VAR, "supervisor-token")
    sent = []
    monkeypatch.setattr(inbox, "send",
                        lambda agent, text, sender="brindle", timeout=3.0: sent.append((agent.id, text)) or True)
    return sent


@pytest.fixture
def fake_tmux(monkeypatch):
    """No real panes: records what's pasted where; every pane shows idle agy."""
    pasted = []
    monkeypatch.setattr(agents.tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(agents.tmux, "new_window", lambda *a, **k: "@9")
    monkeypatch.setattr(agents.tmux, "apply_theme", lambda *a: None)
    monkeypatch.setattr(agents.tmux, "capture", lambda *a, **k: IDLE_SCREEN)
    monkeypatch.setattr(agents.tmux, "paste", lambda target, text, **k: pasted.append((target, text)))
    monkeypatch.setattr(agents, "is_alive", lambda agent: True)
    return pasted


@pytest.fixture
def helpers(monkeypatch):
    """brindle's detached helpers (_flush, _after-launch) aren't started; git
    and everything else runs as usual. Returns their command lines."""
    started = []
    real = subprocess.Popen

    def popen(argv, *a, **k):
        if isinstance(argv, list) and any(x in argv for x in ("_flush", "_after-launch")):
            started.append(argv)
            return None
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


def add_agy(db, ws, agent_id="g1", status="idle", window="@9"):
    db.add_agent(Agent(agent_id, ws.id, "developer", "antigravity", "boss", "assign", status, window,
                       None, time.time()))
    return db.get_agent(agent_id)


def test_launch_does_not_take_the_launchers_inbox(db, ws, launcher_inbox, fake_tmux, helpers, monkeypatch):
    monkeypatch.setattr(Antigravity, "command", lambda self, ctx: ["agy"])
    monkeypatch.setattr(Antigravity, "after_launch", lambda self, t: None)
    a = agents.spawn(db, ws, "developer", prompt="fix it", provider_name="antigravity", mode="assign")
    a = db.get_agent(a.id)
    # agy has no start hook, so brindle marks it ready from the launcher's own process.
    assert a.status == "idle" and not a.inbox_socket and not a.inbox_token
    assert any("_flush" in argv and a.id in argv for argv in helpers)
    # The warm-up (its profile) and then the task are typed into its pane.
    assert agents.flush(db, a.id)
    db.set_status(a.id, "idle")
    assert agents.flush(db, a.id)
    assert [t for t, _ in fake_tmux] == ["@9", "@9"]
    assert fake_tmux[0][1].endswith(antigravity.WARMUP_END) and "You are a developer agent" in fake_tmux[0][1]
    assert fake_tmux[1][1].startswith("fix it")
    assert launcher_inbox == []


def test_a_claude_agent_still_records_its_own_inbox(db, ws, launcher_inbox):
    db.add_agent(Agent("c1", ws.id, "developer", "claude", None, "assign", "starting", "@1", None, time.time()))
    agents.handle_hook(db, "c1", "session-start", {})
    assert db.get_agent("c1").inbox_token == "supervisor-token"


def test_send_message_is_typed_into_the_agy_pane(db, ws, launcher_inbox, fake_tmux):
    add_agy(db, ws)
    assert agents.send_message(db, "g1", "also add tests") == "delivered"
    assert len(fake_tmux) == 1 and fake_tmux[0][0] == "@9" and "also add tests" in fake_tmux[0][1]
    assert launcher_inbox == []


def test_an_inbox_recorded_by_an_older_brindle_is_ignored(db, ws, launcher_inbox, fake_tmux, tmp_path,
                                                        monkeypatch):
    add_agy(db, ws)
    db.update_agent("g1", inbox_socket=str(tmp_path / "supervisor.sock"), inbox_token="supervisor-token")
    assert not inbox.usable(db.get_agent("g1"))
    assert agents.send_message(db, "g1", "hello") == "delivered"
    assert [t for t, _ in fake_tmux] == ["@9"]
    # A busy agy agent keeps it queued for its Stop hook rather than using the inbox.
    db.set_status("g1", "processing")
    monkeypatch.setattr(agents.tmux, "capture", lambda *a, **k: "Working\nesc to cancel\n")
    assert agents.send_message(db, "g1", "later") == "queued"
    assert db.pending_count("g1") == 1 and launcher_inbox == []


def test_a_codex_worker_does_not_take_the_launchers_inbox(db, ws, launcher_inbox, fake_tmux):
    db.add_agent(Agent("x1", ws.id, "developer", "codex", "boss", "assign", "starting", "@9", None,
                       time.time()))
    agents.ready(db, "x1")   # Codex has no start hook either
    assert db.get_agent("x1").status == "idle" and not db.get_agent("x1").inbox_socket
    assert agents.send_message(db, "x1", "also add tests") == "delivered"
    assert [t for t, _ in fake_tmux] == ["@9"] and launcher_inbox == []


def test_agents_are_launched_without_the_launchers_inbox(db, ws, launcher_inbox, monkeypatch):
    windows = []
    monkeypatch.setattr(agents.tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(agents.tmux, "apply_theme", lambda *a: None)
    monkeypatch.setattr(agents.tmux, "new_window",
                        lambda session, name, cwd, argv, env, **k: windows.append(argv) or "@9")
    a = add_agy(db, ws, status="starting", window="")
    agents._open_window(db, a, ws, "w", ["/usr/bin/env"], False)
    # What the pane runs, started from inside the supervisor's Claude Code session.
    out = subprocess.run(windows[0], capture_output=True, text=True, check=True).stdout
    assert "PATH=" in out
    assert inbox.SOCKET_VAR not in out and inbox.TOKEN_VAR not in out


def test_the_sweep_forgets_a_launchers_inbox(db, ws, tmp_path):
    from brindle import cull

    for aid, provider in (("g1", "antigravity"), ("x1", "codex"), ("c1", "claude")):
        db.add_agent(Agent(aid, ws.id, "developer", provider, "boss", "assign", "done", "@9", "ok",
                           time.time()))
        db.update_agent(aid, inbox_socket=str(tmp_path / "supervisor.sock"), inbox_token="t")
    assert any("inbox" in line for line in cull.sweep(db))
    assert not db.get_agent("g1").inbox_socket and not db.get_agent("g1").inbox_token
    assert not db.get_agent("x1").inbox_socket
    assert db.get_agent("c1").inbox_token == "t"   # a Claude Code agent's inbox is its own
    assert not any("inbox" in line for line in cull.sweep(db))


# -- assign over MCP ------------------------------------------------------------------


@pytest.fixture
def boss(db, ws, repo, monkeypatch, tmp_path):
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "developer-antigravity.md").write_text(PROFILE)
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\n")
    agy.chmod(0o755)
    monkeypatch.setenv("BRINDLE_AGY_BIN", str(agy))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def test_assign_starts_an_antigravity_worker(db, boss, launcher_inbox, fake_tmux, helpers):
    out = asyncio.run(mcp_server.assign("developer-antigravity", "write the readme", branch="docs/readme"))
    m = re.search(r"Started worker (\S+) \(developer-antigravity\)", out)
    assert m, out
    worker = db.get_agent(m.group(1))
    assert worker.provider == "antigravity" and worker.tmux_window == "@9" and not worker.inbox_socket
    assert any("_after-launch" in argv and worker.id in argv for argv in helpers)
    # What the detached _after-launch helper does once agy shows its prompt.
    agents.ready(db, worker.id)
    assert not db.get_agent(worker.id).inbox_socket
    assert agents.flush(db, worker.id)
    assert fake_tmux[0][0] == "@9" and fake_tmux[0][1].endswith(antigravity.WARMUP_END)
    assert db.pending_count(worker.id) == 1   # the task, typed in as the next turn
    assert launcher_inbox == []


@pytest.mark.parametrize("tool", ["assign", "handoff"])
def test_a_failed_start_says_why(db, boss, monkeypatch, tool):
    def tracked(*a, **k):
        raise antigravity.AntigravityError(".agents/hooks.json is committed in this repo")

    monkeypatch.setattr(Antigravity, "command", tracked)
    out = asyncio.run(getattr(mcp_server, tool)("developer-antigravity", "write the readme"))
    assert out.startswith("Not started") and "developer-antigravity" in out
    assert "AntigravityError: .agents/hooks.json is committed" in out
    assert [a.id for a in db.list_agents()] == ["boss"]


def test_a_failure_with_no_message_names_the_error(db, boss, monkeypatch):
    def silent(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(agents, "delegate", silent)
    out = asyncio.run(mcp_server.assign("developer-antigravity", "write the readme"))
    assert out.startswith("Not started") and out.endswith("TimeoutError")
