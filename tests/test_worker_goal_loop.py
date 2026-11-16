"""A Claude worker whose finish line (its /goal) couldn't be met asked in its
own pane, nobody read it, and the goal sent it back until Claude Code's
stop-hook cap ended the turn: nine times, its supervisor never told."""

from brindle import agents, cull, tmux
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


def test_a_stop_another_hook_sent_back_is_not_blocked_again_for_queued_mail(db, tmp_path, monkeypatch):
    a_session(db, tmp_path, monkeypatch, "processing", "claude")
    db.enqueue("w1", "continue working", "boss")
    assert agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True}) is None
    assert db.pending_count("w1") == 1    # kept for the sweep's flush, not lost
