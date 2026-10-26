"""Issue #49: an Antigravity worker's instructions, task and messages must reach
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
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    return pasted


@pytest.fixture
def helpers(monkeypatch):
    """brindle's detached helpers (_flush, _after-launch, _launch-health) aren't started; git
    and everything else runs as usual. Returns their command lines."""
    started = []
    real = subprocess.Popen

    def popen(argv, *a, **k):
        if isinstance(argv, list) and any(x in argv for x in ("_flush", "_after-launch", "_launch-health")):
            started.append(argv)
            return None
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


def add_agy(db, ws, agent_id="g1", status="idle", window="@9"):
    db.add_agent(Agent(agent_id, ws.id, "developer", "antigravity", "boss", "assign", status, window,
                       None, time.time()))
    return db.get_agent(agent_id)


def test_promptless_interactive_start_keeps_profile_tools_and_autopilot_context(
        db, ws, repo, launcher_inbox, fake_tmux, helpers, monkeypatch):
    from brindle import autopilot, cli, model_access
    monkeypatch.chdir(repo)
    monkeypatch.setattr(Antigravity, "command", lambda self, ctx: ["agy"])
    monkeypatch.setattr(Antigravity, "after_launch", lambda *a: True)
    monkeypatch.setattr(model_access, "probe", lambda *a, **k: pytest.fail("availability generation probe"))
    monkeypatch.setattr(autopilot, "guide", lambda *a: "\nAutopilot context must survive initialization.")
    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(cli, "_trust_codex_hook", lambda *a: None)
    monkeypatch.setattr(cli, "_cull_detached", lambda *a: None)
    monkeypatch.setattr(cli, "_local_models_detached", lambda *a: None)
    monkeypatch.setattr(cli, "_settings_sync_detached", lambda *a: None)
    cli.start(prompt=None, provider="antigravity", attach=False,
              watch=False, autopilot=True, branch=None, worktree=None)
    a, = db.list_agents(ws.id)
    assert a.mode == "interactive" and a.task is None
    expected = agents._profile_for(db, a, ws).prompt
    assert expected and db.pending_count(a.id) == 1
    agents.ready(db, a.id)   # delivers the warm-up itself
    delivered = fake_tmux[0][1]
    assert expected in delivered and antigravity.TOOLS_NOTE in delivered
    assert "Delegation rule:" in delivered
    assert "Autopilot context must survive initialization." in delivered
    assert antigravity.WARMUP_END in delivered
    assert db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone() is None
    assert not any("_launch-health" in argv for argv in helpers)
    assert db.pending_count(a.id) == 0
    db.set_status(a.id, "idle")
    assert not agents.flush(db, a.id) and len(fake_tmux) == 1 and launcher_inbox == []


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


def test_agy_warmup_queued_before_task(db, boss, launcher_inbox, fake_tmux, helpers, monkeypatch):
    # agy has no MCP tools in its first turn, so the task must be the second.
    out = asyncio.run(mcp_server.assign("developer-antigravity", "write the readme", branch="docs/readme"))
    m = re.search(r"Started worker (\S+) \(developer-antigravity\)", out)
    assert m, out
    worker = db.get_agent(m.group(1))
    assert worker.provider == "antigravity" and worker.tmux_window == "@9" and not worker.inbox_socket
    assert any("_after-launch" in argv and worker.id in argv for argv in helpers)
    assert db.pending_count(worker.id) == 2
    # What the detached _after-launch helper does once agy shows its prompt:
    # the warm-up goes in at once.
    agents.ready(db, worker.id)
    assert not db.get_agent(worker.id).inbox_socket
    assert db.pending_count(worker.id) == 1
    from brindle import launch_health
    monkeypatch.setattr(launch_health.time, "sleep", lambda _: pytest.fail("flush waited for delivery"))
    agents.handle_hook(db, worker.id, "prompt-submit", {})
    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (worker.id,)).fetchone()
    assert not row["verifying"] and row["delivered_at"] is None and not row["submitted_prompt"]
    db.set_status(worker.id, "idle")   # agy's Stop hook after the warm-up turn
    assert agents.flush(db, worker.id)
    assert [t for t, _ in fake_tmux] == ["@9", "@9"]
    warmup, task = (text for _, text in fake_tmux)
    assert "You are a developer agent" in warmup
    assert "When you run under brindle" in warmup and "call_mcp_tool" in warmup
    assert antigravity.WARMUP_END in warmup and "write the readme" not in warmup
    assert task.count("write the readme") == 1 and antigravity.WARMUP_END not in task
    assert db.pending_count(worker.id) == 0
    db.set_status(worker.id, "idle")
    assert not agents.flush(db, worker.id)
    assert len(fake_tmux) == 2
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


