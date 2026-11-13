"""The supervisor's operational memory (repo setting ``supervisor_state``):
brindle folds what it already sees (a merge, a decision, an open question,
a worker's result) into a compact state, and past the context threshold the
supervisor restarts as a fresh session seeded with that state. Runs the real
merge, decision, need_user, Stop-hook and resume code; only the CLI launch,
the process stop and the detached helper's start are faked."""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
from fast_repo import repo, repo_template  # noqa: F401
from brindle import agents, autopilot, cli, mcp_server, supervisor_state, workspaces
from test_merge_order import add, quiet_removal, worktree


@pytest.fixture
def session(db, repo, monkeypatch):
    """A goal-driven supervisor "boss" on ``repo``; ``setup(**settings)``
    commits the repo's settings."""
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing", task="Make the app greet people")
    db.add_autopilot("boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    quiet_removal(monkeypatch)

    def setup(**settings) -> None:
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps(
            {"review": False, "pipeline": False, **settings}))
        sh("git add .brindle/config.json && git commit -qm settings", repo)
        autopilot.set_goal(db, "boss", "Greet people", [("Greeting works", "true", None)])
    return setup


def transcript(tmp_path: Path, context: int, said: str) -> Path:
    """A Claude Code transcript whose last turn read ``context`` tokens,
    after the person said ``said``."""
    path = tmp_path / "chat.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": said}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "a long tool output"}]}},
        {"type": "assistant", "message": {
            "id": "m1", "role": "assistant", "content": [{"type": "text", "text": "Noted, Monday it is."}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": context - 10,
                      "cache_creation_input_tokens": 0, "output_tokens": 20}}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def decide_and_ask(topic: str, question: str) -> None:
    assert mcp_server.record_decision(topic, f"{topic} lives in sqlite", "one store").startswith("Recorded")
    assert mcp_server.need_user(question).startswith("Noted")


def test_state_reflects_a_merge_a_decision_and_an_open_question(db, repo, session):
    session(supervisor_state=True)
    ws = worktree(db, repo, "feat-greet", files={"greet.py": "print('hello')\n"})
    add(db, ws, "w1", "assign", parent="boss")
    db.set_result("w1", "Added greet.py; it prints hello.")
    reply = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert reply.startswith("Merged"), reply
    decide_and_ask("storage", "Ship on Friday or Monday?")

    state = supervisor_state.render(db, "boss")
    assert "feat-greet (w1): merged" in state
    assert "storage: storage lives in sqlite" in state
    assert "Ship on Friday or Monday?" in state.split("## Open questions")[1]
    assert "Added greet.py" in state and "[pending] Greeting works" in state

    mcp_server.need_user(answered=True)
    state = supervisor_state.render(db, "boss")
    assert "## Open questions" not in state and "Ship on Friday or Monday?" in state   # kept as answered


@pytest.mark.parametrize("on", [True, False])
def test_restart_seeds_a_fresh_session_and_loses_no_decision_or_question(db, repo, session, tmp_path,
                                                                         monkeypatch, on):
    session(supervisor_state=on, supervisor_restart_tokens=50_000)
    decide_and_ask("storage", "Ship on Friday or Monday?")
    detached, launched = [], []
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: detached.append(list(argv)))
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: launched.append(kw))
    db.update_agent("boss", session_ref="old-long-chat")

    # Under the threshold, the turn ends as usual.
    small = transcript(tmp_path, 20_000, "Ship on Monday.")
    assert supervisor_state.on_turn_end(db, db.get_agent("boss"), {"transcript_path": str(small)}) is False

    # Past it, the real Stop hook starts the restart instead of delivering anything.
    big = transcript(tmp_path, 60_000, "Ship on Monday.")
    db.enqueue("boss", "[brindle] w2 finished", None)
    agents.handle_hook(db, "boss", "stop", {"transcript_path": str(big)})
    if not on:
        assert detached == [] and launched == []
        return
    [argv] = detached
    assert db.pending_count("boss") == 1   # left for the fresh session
    result = CliRunner().invoke(cli.app, argv[argv.index("_restart-supervisor"):], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    [launch] = launched
    seed = launch["prompt"]
    assert launch["resume"] is None and db.get_agent("boss").session_ref is None
    assert seed.startswith(supervisor_state.RESTART_HEAD)
    assert "storage lives in sqlite" in seed and "Ship on Friday or Monday?" in seed
    assert "- message: Ship on Monday." in seed and "a long tool output" not in seed
    assert "Make the app greet people" in seed   # the request it started from

    # Nothing is lost across the restart: what came before and after both stay.
    mcp_server.record_decision("caching", "cache greetings", "speed")
    state = supervisor_state.render(db, "boss")
    assert "storage lives in sqlite" in state and "cache greetings" in state
    assert "Ship on Friday or Monday?" in state.split("## Open questions")[1]
