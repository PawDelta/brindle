"""brindle.wake for workers: a Claude worker in a tmux window gets its mail
from the same asyncRewake waiter a supervisor has, framed the same way,
instead of keys typed into its pane (no real CLIs, no real tmux)."""

import json
import re
import time

import pytest

from brindle import agents, launch_health, wake, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import ClaudeCode, LaunchContext


@pytest.fixture
def worker(db, repo, monkeypatch):
    """A supervisor and its Claude worker, idle in its window; typing into the
    pane is recorded instead of done."""
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "idle",
                       "@1", None, time.time()))
    pasted = []
    monkeypatch.setattr(agents, "is_alive", lambda a, *_: True)
    monkeypatch.setattr(agents, "reconcile", lambda db, agent, *a, **k: agent)
    monkeypatch.setattr(agents, "_paste_blocked", lambda agent: False)
    monkeypatch.setattr(agents, "_paste_message", lambda db, agent, body, *a, **k: pasted.append(body))
    monkeypatch.setattr(launch_health, "before_delivery", lambda *a, **k: "")
    monkeypatch.setattr(launch_health, "verify_delivery", lambda *a, **k: None)
    return pasted


def _hooks(headless=False):
    profile = load_profile("developer")
    profile.headless = headless
    argv = ClaudeCode().command(LaunchContext("w1", profile, "the task", mode="assign"))
    return json.loads(argv[argv.index("--settings") + 1])["hooks"]


def _commands(group):
    return [h["command"] for g in group for h in g["hooks"]]


@pytest.mark.variant("catches a worker launched without the waiter, or losing its report_result Stop hook beside it")
def test_a_claude_worker_launches_with_the_waiter_beside_its_stop_hook():
    hooks = _hooks()
    for event in ("SessionStart", "Stop"):
        [waiter] = [h for g in hooks[event] for h in g["hooks"] if wake.EVENT in h["command"]]
        assert waiter["asyncRewake"] is True and "--agent w1" in waiter["command"]
    # The Stop hook that keeps it going until report_result is still there.
    assert any(re.search(r"_hook stop --agent w1$", c) for c in _commands(hooks["Stop"]))
    # A headless worker's runner takes its mail itself: no waiter.
    headless = _hooks(headless=True)
    assert not any(wake.EVENT in c for e in ("SessionStart", "Stop") for c in _commands(headless[e]))


def test_a_workers_mail_goes_to_its_live_waiter_not_its_pane(db, worker):
    wake._register("w1")   # its waiter is watching
    assert agents.send_message(db, "w1", "please fix the test", sender_id="boss") == "delivered"
    assert agents.flush(db, "w1") is False
    assert worker == [] and db.pending_count("w1") == 1   # for the waiter

    text, _ = wake.take(db, db.get_agent("w1"))
    nonce = re.match(r'brindle delivered 1 message \(nonce "([0-9a-f]{16})"\):\n', text).group(1)
    [(sender, body)] = re.findall(rf'<brindle-msg nonce="{nonce}" from="([^"]*)">\n(.*?)\n</brindle-msg nonce="{nonce}">',
                                  text, re.S)
    assert sender == "supervisor agent boss" and "please fix the test" in body

    # Its Stop leaves queued mail to the waiter it just started, and still
    # blocks a stop with no report.
    db.enqueue("w1", "one more thing", "boss")
    assert agents.handle_hook(db, "w1", "stop", {}) is None
    assert db.pending_count("w1") == 1 and worker == []
    wake.take(db, db.get_agent("w1"))
    assert agents.handle_hook(db, "w1", "stop", {})["decision"] == "block"
