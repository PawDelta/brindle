"""The supervisor's operational memory (repo setting ``supervisor_state``):
brindle folds what it already sees (a merge, a decision, an open question,
a worker's result) into a compact state, and past the context threshold the
supervisor restarts as a fresh session seeded with that state. Runs the real
merge, decision, need_user, Stop-hook, delivery and resume code; only the CLI
launch, the process stop and the detached helper's start are faked."""

import asyncio
import json
from contextlib import ExitStack
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
    """``start(on, before_stop)``: the supervisor with a decision, an open
    question and a queued message (``calls.mail``) ends a turn past the
    threshold through the real Stop hook. Returns what was detached, stopped
    and launched; ``helper(argv)`` runs a detached restart as its process
    would. The launch is faked from its preflight (``calls.preflight``, run
    first) to its commit, which holds the resume's gate as the real one does."""
    calls = SimpleNamespace(detached=[], stopped=[], launched=[], preflight=lambda: None)
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: calls.detached.append(list(argv)))
    monkeypatch.setattr(agents, "_stop", lambda db_, a: calls.stopped.append(a.id))
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)

    def launch(db_, a, ws, **kw):
        calls.preflight()
        with ExitStack() as hold:
            agents._gate_commit(hold)
            calls.launched.append(kw)
            db_.set_status(a.id, "processing")
    monkeypatch.setattr(agents, "_launch", launch)

    def start(on: bool = True, around_stop=lambda end_turn: end_turn()) -> SimpleNamespace:
        """``around_stop(end_turn)``: what else happens as the turn ends."""
        session(supervisor_state=on, supervisor_restart_tokens=50_000)
        decide_and_ask("storage", "Ship on Friday or Monday?")
        db.update_agent("boss", session_ref="old-long-chat")
        db.set_status("boss", "processing")
        calls.mail = db.enqueue("boss", "[brindle] w2 finished", None)
        big = transcript(tmp_path, 60_000, "Ship on Monday.")
        around_stop(lambda: agents.handle_hook(
            db, "boss", "stop", {"transcript_path": str(big), "session_id": "old-long-chat"}))
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
    # Until the fresh session starts, no transport takes the mail: it is the fresh session's.
    boss = db.get_agent("boss")
    assert wake.take(db, boss) is None                     # the waiter
    assert db.claim_delivery(calls.mail) is None           # the inbox transport
    partial = json.dumps({"message_id": calls.mail, "pane": boss.tmux_window, "server": agents.server_of(boss),
                          "provider": "claude", "body": "[brindle] w2 finished", "lead": "", "stage": "lead"})
    db.set_mark("boss", "pane-delivery", partial)
    assert agents.flush(db, "boss") is False               # a pane delivery part way, resumed
    db.set_mark("boss", "pane-delivery", None)
    assert db.pending_count("boss") == 1
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


def typed(db) -> None:
    """The person sends a prompt in the claimed (old) CLI session."""
    agents.handle_hook(db, "boss", "prompt-submit", {"prompt": "one more thing", "session_id": "old-long-chat"})


def turn_completed(db, tmp_path) -> None:
    """New turns in the same CLI session: they start and end again, and the
    last leaves it idle as the claimed one did."""
    typed(db)
    small = transcript(tmp_path, 20_000, "one more thing")
    agents.handle_hook(db, "boss", "stop", {"transcript_path": str(small), "session_id": "old-long-chat"})
    assert db.pending_count("boss") == 0   # its Stop delivered the queued mail: no longer held
    db.set_status("boss", "idle")          # the turn that mail started ends with nothing more to say


@pytest.mark.parametrize("meanwhile, stopped, status", [
    # Looks like the claimed turn by status and session: only the claim's cancel tells them apart.
    pytest.param("turn completed", [], "idle", marks=pytest.mark.variant("a turn completed since the claim cancels the restart")),
    pytest.param("paused", [], "paused", marks=pytest.mark.variant(
        "a pause before the helper starts must not be undone by it")),
    pytest.param("paused during launch", ["boss"], "paused", marks=pytest.mark.variant(
        "the person's own pause() during the launch's preflight, beside the app exit's batch pause")),
    pytest.param("app quit during launch", ["boss"], "paused", marks=pytest.mark.variant(
        "the app's exit pauses through record_pauses, a second entry point that once skipped the cancel")),
])
def test_a_restart_gives_way_to_what_happened_since_its_claim(db, tmp_path, restarting, meanwhile,
                                                              stopped, status):
    calls = restarting()
    [argv] = calls.detached
    if meanwhile == "turn completed":
        turn_completed(db, tmp_path)
    elif meanwhile == "paused":
        agents.pause(db, "boss")
    elif meanwhile == "paused during launch":   # while the fresh launch is in its preflight, before it commits
        calls.preflight = lambda: agents.pause(db, "boss")
    else:   # the app's daemon on its way out records every session's pause at once
        calls.preflight = lambda: agents.record_pauses(db, ["boss"])

    calls.helper(argv)
    assert calls.stopped == stopped and calls.launched == []
    assert db.get_agent("boss").status == status
    assert not db.restart_holding("boss")   # the mail is delivered again


@pytest.mark.parametrize("transport", ["pane", "inbox"])
def test_no_restart_is_claimed_while_mail_is_part_way_to_the_supervisor(db, restarting, monkeypatch, transport):
    """The turn ends while a message is part way in: typed into the pane, or
    sent through the real direct inbox hand-over (a supervisor without brindle's
    waiter), its send faked at the socket."""
    from brindle import inbox

    def typing_into_the_pane(end_turn) -> None:
        mail = db.enqueue("boss", "[brindle] review approved", None)
        db.set_mark("boss", "pane-delivery", json.dumps({"message_id": mail, "stage": "lead"}))
        end_turn()

    def sending_to_its_inbox(end_turn) -> None:
        monkeypatch.setattr(inbox, "usable", lambda agent: True)
        sends = []

        def send(agent, text, **kw):
            sends.append(text)
            if len(sends) == 1:
                end_turn()   # the turn ends while the hand-over's send is under way
            return True
        monkeypatch.setattr(inbox, "send", send)
        mail = db.enqueue("boss", "[brindle] review approved", None)
        assert agents._hand_over(db, db.get_agent("boss"), mail, "[brindle] review approved", None) == "delivered"

    calls = restarting(around_stop=typing_into_the_pane if transport == "pane" else sending_to_its_inbox)
    assert calls.detached == [] and not db.restart_holding("boss")
    assert not db.deliveries_in_flight("boss") or transport == "pane"   # a finished send is in flight no more


def test_a_failed_fresh_launch_leaves_the_old_chat_resumable(db, restarting, monkeypatch):
    calls = restarting()
    [argv] = calls.detached
    calls.preflight = lambda: (_ for _ in ()).throw(agents.AgentError("the launch failed"))
    with pytest.raises(agents.AgentError):
        calls.helper(argv)
    # The long chat stays resumable, so ordinary recovery loses nothing.
    assert db.get_agent("boss").status == "paused" and db.get_agent("boss").session_ref == "old-long-chat"
    assert not db.restart_holding("boss")
    calls.preflight = lambda: None
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: True))
    agents.resume(db, "boss")
    assert [kw["resume"] for kw in calls.launched] == ["old-long-chat"]
