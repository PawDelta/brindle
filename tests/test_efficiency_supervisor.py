"""The supervisor's operational memory (repo setting ``supervisor_state``):
brindle folds what it already sees (a merge, a decision, an open question,
a worker's result) into a compact state, and past the context threshold the
supervisor restarts as a fresh session seeded with that state. Runs the real
merge, decision, need_user, Stop-hook, delivery and resume code; only the CLI
launch, the process stop and the detached helper's start are faked."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from conftest import sh
from fast_repo import repo, repo_template  # noqa: F401
from brindle import agents, autopilot, cli, mcp_server, providers, supervisor_state, wake, workspaces
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
    path = tmp_path / f"chat-{context}.jsonl"
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
    # A worker that reports and is cleaned up before the state's first fold.
    gone = worktree(db, repo, "feat-wave")
    add(db, gone, "w2", "assign", parent="boss")
    db.set_result("w2", "Added a wave emoji.")
    db.delete_workspace(gone.id)
    assert db.get_agent("w2") is None

    state = supervisor_state.render(db, "boss")
    assert "feat-greet (w1): merged" in state
    assert "storage: storage lives in sqlite" in state
    assert "Ship on Friday or Monday?" in state.split("## Open questions")[1]
    assert "Added greet.py" in state and "[pending] Greeting works" in state
    assert "w2 (developer, feat-wave): Added a wave emoji." in state

    mcp_server.need_user(answered=True)
    state = supervisor_state.render(db, "boss")
    assert "## Open questions" not in state and "Ship on Friday or Monday?" in state   # kept as answered


@pytest.fixture
def restarting(db, session, tmp_path, monkeypatch):
    """``start(on)``: the supervisor with a decision, an open question and a
    queued message ends a turn past the threshold through the real Stop
    hook. Returns what was detached, stopped and launched; ``helper(argv)``
    runs a detached restart as its process would."""
    calls = SimpleNamespace(detached=[], stopped=[], launched=[])
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: calls.detached.append(list(argv)))
    monkeypatch.setattr(agents, "_stop", lambda db_, a: calls.stopped.append(a.id))
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: calls.launched.append(kw))

    def start(on: bool = True) -> SimpleNamespace:
        session(supervisor_state=on, supervisor_restart_tokens=50_000)
        decide_and_ask("storage", "Ship on Friday or Monday?")
        db.update_agent("boss", session_ref="old-long-chat")
        db.set_status("boss", "processing")
        db.enqueue("boss", "[brindle] w2 finished", None)
        big = transcript(tmp_path, 60_000, "Ship on Monday.")
        agents.handle_hook(db, "boss", "stop", {"transcript_path": str(big), "session_id": "old-long-chat"})
        return calls

    def helper(argv: list[str]) -> None:
        result = CliRunner().invoke(cli.app, argv[argv.index("_restart-supervisor"):], catch_exceptions=False)
        assert result.exit_code == 0, result.output
    calls.helper = helper
    return start


@pytest.mark.parametrize("on", [True, False])
def test_restart_seeds_a_fresh_session_and_loses_no_decision_or_question(db, tmp_path, restarting, on):
    small = transcript(tmp_path, 20_000, "Ship on Monday.")
    calls = restarting(on)
    if not on:
        assert calls.detached == [] and calls.launched == []
        return
    # Under the threshold, a turn ends as usual.
    assert supervisor_state.on_turn_end(db, db.get_agent("boss"), {"transcript_path": str(small)}) is False

    [argv] = calls.detached
    # Until the fresh session starts, the real waiter takes nothing: the mail is the fresh session's.
    assert wake.take(db, db.get_agent("boss")) is None and db.pending_count("boss") == 1
    calls.helper(argv)
    calls.helper(argv)   # a second helper with the same claim does nothing

    [launch] = calls.launched
    seed = launch["prompt"]
    assert calls.stopped == ["boss"] and launch["resume"] is None
    assert seed.startswith(supervisor_state.RESTART_HEAD)
    assert "storage lives in sqlite" in seed and "Ship on Friday or Monday?" in seed
    assert "- message: Ship on Monday." in seed and "a long tool output" not in seed
    assert "Make the app greet people" in seed   # the request it started from
    assert db.pop_pending("boss").body == "[brindle] w2 finished"   # the hold is gone with the restart

    # Nothing is lost across the restart: what came before and after both stay.
    mcp_server.record_decision("caching", "cache greetings", "speed")
    state = supervisor_state.render(db, "boss")
    assert "storage lives in sqlite" in state and "cache greetings" in state
    assert "Ship on Friday or Monday?" in state.split("## Open questions")[1]


@pytest.mark.parametrize("meanwhile", [
    "typed",
    pytest.param("paused", marks=pytest.mark.variant(
        "a late helper must not undo the person's pause of the session")),
    pytest.param("launch fails", marks=pytest.mark.variant(
        "a failed fresh launch must leave the old chat resumable, losing nothing")),
])
def test_a_restart_gives_way_to_what_happened_since_its_claim(db, restarting, monkeypatch, meanwhile):
    calls = restarting()
    [argv] = calls.detached
    if meanwhile == "paused":
        db.set_status("boss", "paused")   # the person paused the session (agents.pause records this first)
    elif meanwhile == "typed":
        agents.handle_hook(db, "boss", "prompt-submit", {"prompt": "one more thing", "session_id": "old-long-chat"})
    else:
        def refuse(*a, **kw):
            raise agents.AgentError("the launch failed")
        monkeypatch.setattr(agents, "_launch", refuse)

    if meanwhile == "launch fails":
        with pytest.raises(agents.AgentError):
            calls.helper(argv)
        # The long chat stays resumable, so ordinary recovery loses nothing.
        assert db.get_agent("boss").status == "paused" and db.get_agent("boss").session_ref == "old-long-chat"
        monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: calls.launched.append(kw))
        monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: True))
        agents.resume(db, "boss")
        assert [kw["resume"] for kw in calls.launched] == ["old-long-chat"]
    else:
        calls.helper(argv)
        assert calls.stopped == [] and calls.launched == []
        assert db.get_agent("boss").status == ("paused" if meanwhile == "paused" else "processing")
    assert db.pop_pending("boss") is not None   # the claim's hold on the mail is released
