"""Milestone status is written back to the session's own goal file
(.brindle/goals/<session id>.md, claimed from .brindle/goals.md), and nothing else."""

import os
import re
import time

import pytest

from brindle import autopilot, workspaces
from brindle.db import Agent

GOALS = """# Settings page

Users can change their name.

## Settings API
check: `test -f api.txt`
profile: reviewer
The endpoint saves the name.

## Settings UI
check: test -f ui.txt

Some *user prose* to keep.
"""
SHA = "abc1234def5678abc1234def5678abc1234def56"


def add_agent(db, ws, agent_id, status="processing"):
    a = Agent(agent_id, ws.id, "supervisor", "claude", None, "interactive", status, "@0", None, time.time())
    db.add_agent(a)
    return a


def load(db, repo, agent_id="boss", text=GOALS, status="processing"):
    """A session whose goal was loaded from ``repo``'s goals.md."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "goals.md").write_text(text)
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, agent_id, status)
    autopilot.enable(db, agent_id, ws)
    return ws


def path(repo, agent_id="boss"):
    return repo / ".brindle" / "goals" / f"{agent_id}.md"


def goals(repo):
    return path(repo).read_text()


def pass_first(db, agent_id="boss", sha=SHA):
    db.record_check(db.milestones(agent_id)[0].id, True, "SECRET check output", sha)


def test_status_is_written_after_a_check_and_the_rest_is_preserved(db, repo):
    load(db, repo)
    assert goals(repo) == GOALS   # loading alone writes nothing
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    day = time.strftime("%Y-%m-%d")
    new = goals(repo)
    assert f"status: passed at abc1234 ({day})" in new
    assert "status: pending" in new
    assert [l for l in new.splitlines() if not l.startswith("status:")] == GOALS.splitlines()
    assert autopilot.parse_goals(new) == autopilot.parse_goals(GOALS)
    # The status goes after the check/profile lines, and rewriting is idempotent.
    assert new.index("profile: reviewer") < new.index("status: passed")
    before = os.stat(path(repo)).st_mtime_ns
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == new and os.stat(path(repo)).st_mtime_ns == before


def test_crlf_and_missing_final_newline_survive(db, repo):
    text = GOALS.replace("\n", "\r\n").rstrip()
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "goals.md").write_bytes(text.encode())
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "boss")
    autopilot.enable(db, "boss", ws)
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    raw = path(repo).read_bytes().decode()
    assert "\r\nstatus: passed" in raw and not re.search(r"(?<!\r)\n", raw)
    assert raw.replace("\r\nstatus: passed at abc1234 (" + time.strftime("%Y-%m-%d") + ")", "").count("status") == 1


@pytest.mark.parametrize("how", ["paused", "off"])
def test_a_stopped_session_stops_writing(db, repo, how):
    load(db, repo)
    pass_first(db)
    if how == "paused":
        db.set_status("boss", "paused")
    else:
        autopilot.set_enabled(db, "boss", False)
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == GOALS


def test_a_file_the_person_edited_is_left_for_resume(db, repo):
    load(db, repo)
    edited = "# Something else\n\n## Other\ncheck: true\n"
    path(repo).write_text(edited)
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == edited


def test_an_unedited_file_resumes_to_the_same_goal(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "boss")
    db.add_autopilot("boss")
    ms = [("Both", "true\nfalse", "## not a milestone\ncheck: not a check\n\\ kept"),
          ("Ticks", "echo `date`", None)]
    autopilot.set_goal(db, "boss", "Edge cases", ms, "# not the goal")
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    autopilot.reload_goals_file(db, "boss")
    assert [(m.title, m.check_cmd, m.detail) for m in db.milestones("boss")] == ms
    assert db.milestones("boss")[0].status == "passed" and db.get_autopilot("boss").detail == "# not the goal"
    # A hand-written fenced check (trailing space on its field, no status line yet).
    path(repo).write_text("# Hand written\n\n## Fenced\ncheck: \n```\ntrue\nfalse\n```\n")
    autopilot.reload_goals_file(db, "boss")
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    autopilot.reload_goals_file(db, "boss")
    assert [(m.check_cmd, m.status) for m in db.milestones("boss")] == [("true\nfalse", "passed")]


def test_a_sync_failure_is_logged_not_raised(db, repo, monkeypatch, caplog):
    ws = load(db, repo)
    monkeypatch.setattr(autopilot, "rewrite_goals", lambda *a: 1 / 0)
    (repo / "api.txt").write_text("x")
    autopilot.check_milestones(db, "boss", ws, cfg=autopilot.RepoConfig())
    assert "couldn't sync milestone status" in caplog.text
