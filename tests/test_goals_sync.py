"""Milestone status is written back to the session's own goal file
(.brindle/goals/<session id>.md, claimed from .brindle/goals.md), and nothing else."""

import asyncio
import os
import re
import time

import pytest

from brindle import autopilot, mcp_server, workspaces
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


@pytest.mark.variant("A milestone status write must preserve handwritten prose and be idempotent on subsequent syncs")
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



def test_a_stopped_session_stops_writing(db, repo):
    load(db, repo)
    pass_first(db)
    db.set_status("boss", "paused")
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


def test_goal_summary_and_milestone_phases_round_trip_through_the_goal_file(db, repo, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    from brindle.service.dto import GoalDTO, MilestoneDTO

    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "boss")
    db.add_autopilot("boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    asyncio.run(mcp_server.mcp.call_tool("set_goal", {"goal": "Phased", "summary": " CLI polish ", "milestones": [
        {"title": "API", "check": "true", "phase": " Build  it "},
        {"title": "UI", "check": "true", "detail": "phase: not a phase"},
        {"title": "Ship", "check": "true", "phase": "Release"}]}))
    autopilot.write_goals_file(db, "boss")
    assert "## API\nstatus: pending\nphase: Build it\n" in goals(repo)
    autopilot.reload_goals_file(db, "boss")
    ms = db.milestones("boss")
    assert [(m.phase_name, m.detail) for m in ms] == [("Build it", None), (None, "phase: not a phase"), ("Release", None)]
    assert MilestoneDTO.from_row(ms[0]).phase_name == "Build it"
    ap = db.get_autopilot("boss")
    assert ap.summary == "CLI polish"
    assert GoalDTO.from_autopilot(ap, ms).summary == "CLI polish"
    args = {"goal": "Phased", "milestones": [{"title": "API", "check": "true"}]}
    asyncio.run(mcp_server.mcp.call_tool("set_goal", args))
    assert db.get_autopilot("boss").summary == "CLI polish"
    asyncio.run(mcp_server.mcp.call_tool("set_goal", {**args, "goal": "Another goal"}))
    assert db.get_autopilot("boss").summary == "Another goal"

    # A later failure must retain its cause through the SDK's error wrapper.
    def broken_note(*args):
        raise RuntimeError("cost estimate unavailable")

    monkeypatch.setattr(mcp_server.cost_estimate, "reply_note", broken_note)
    with pytest.raises(ToolError, match="cost estimate unavailable"):
        asyncio.run(mcp_server.mcp.call_tool("set_goal", {
            "goal": "Phased", "milestones": [{"title": "API", "check": "true"}]}))


def test_a_sync_failure_is_logged_not_raised(db, repo, monkeypatch, caplog):
    ws = load(db, repo)
    monkeypatch.setattr(autopilot, "rewrite_goals", lambda *a: 1 / 0)
    (repo / "api.txt").write_text("x")
    autopilot.check_milestones(db, "boss", ws, cfg=autopilot.RepoConfig())
    assert "couldn't sync milestone status" in caplog.text
