"""A Claude worker whose finish line (its /goal) couldn't be met asked in its
own pane, nobody read it, and the goal sent it back until Claude Code's
stop-hook cap ended the turn: nine times, its supervisor never told."""

import time

from brindle import agents, cull, tmux
from brindle.db import Workspace
from brindle.providers import get_provider
from test_daemon_cull_sweep import a_session

LOOPED = """\
● I can't meet the finish line as written: tests/test_x.py doesn't exist on this branch.
  Reply with 1, 2 or 3:
  1. Create it  2. Use tests/test_y.py instead  3. Stop here
  Or run /goal clear
  ⎿  Goal not yet met… continuing
● Reply with 1, 2 or 3. Or run /goal clear
  ⎿  A hook blocked the turn from ending 9 consecutive times — overriding and ending turn.

> \
"""


def test_a_worker_looping_on_its_goal_gets_its_supervisor_told_once(db, tmp_path, monkeypatch):
    sent = a_session(db, tmp_path, monkeypatch, "idle", "claude")
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: LOOPED if target == "%w1" else "")
    cull.sweep(db)
    cull.sweep(db)
    told = [body for to, body in sent if to == "boss" and "w1" in body]
    assert len(told) == 1 and "Reply with 1, 2 or 3" in told[0]
    assert db.get_agent("w1").status == "waiting"


def test_the_goal_tells_the_worker_to_ask_its_supervisor_not_its_pane(tmp_path):
    ws = Workspace("ws1", str(tmp_path), "feature", "worktree", "feature", "main", str(tmp_path), None,
                   "brindle-ws1", time.time())
    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, "tests pass", get_provider("claude"),
                                           headless=False, supervisor_id="boss")
    assert prompt.startswith("/goal")
    escalation = prompt[prompt.index("Finish line: tests pass"):]
    assert "send_message" in escalation and "boss" in escalation
