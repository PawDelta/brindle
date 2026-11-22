"""Mid-task handover (brindle.handover): a worker whose provider ran out has
its task carried on by a fresh worker in the same worktree, on the
cascade's best runnable replacement. Driven through the real stop-failure
hook; only the CLI launch, the panes and the processes are stood in for.

brindle.handover is imported inside the tests, so they run (and fail on
their assertions) against a tree without it."""

import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import (agents, autopilot, limit_report, plugins, provider_switch, providers, quota, routing_goal,
                     subscription_usage, tasks, workspaces)
from brindle.db import Agent
from brindle.profiles import load_profile

REAL_SPAWN = agents.spawn     # before the fixture stands a fake in for it

# quota drops windows already past their reset: keep it ahead of the real clock.
RESET = time.time() + 30 * 86400
TIERS = ("light", "medium", "heavy", "advanced")


def _table() -> dict:
    """Measured: Gemini beats Codex on medium work, though tier order lists Codex first."""
    cell = lambda usd, minutes, p: {"p": p, "first_try": 0.5, "usd": usd, "minutes": minutes, "runs": 50}
    return {"version": 2, "measured": "2026-11-15", "source": "test", "min_runs": 5,
            "tiers": {t: {"p": 0.8, "first_try": 0.3, "minutes": 30, "mtok": 0.4} for t in TIERS},
            "handover": {}, "profiles": {"developer-gemini": {"medium": cell(0.5, 10, 0.95)},
                                         "developer-codex": {"medium": cell(4.0, 60, 0.7)}}}


@pytest.fixture
def setup(db, repo, monkeypatch):
    """A supervisor and a Claude worker on its own branch, with one commit
    and one uncommitted file; every provider's CLI installed and signed in,
    on a subscription. Returns (workspace, spawned launches, the task row)."""
    monkeypatch.setattr(subscription_usage, "identity", lambda: "test-subscription")
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(providers, "unusable", lambda provider, env=None: None)
    monkeypatch.setattr(agents, "unusable", lambda provider, env=None: None)
    monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: None)
    monkeypatch.setattr(agents, "model_problem", lambda name, ws, **kw: None)
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 0.0 if provider == "native" else 100.0)
    monkeypatch.setattr(provider_switch, "_key_billed", lambda profile: False)
    monkeypatch.setattr(routing_goal, "priors", _table)
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"learning": "off", "routing": {"medium": ["developer-sonnet", "developer-codex", "developer-gemini"]}}))
    plugins.reset()

    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive", "processing", "@0",
                       None, time.time()))
    ws = workspaces.create(db, str(repo), "feat/parse").workspace
    worker = Agent("w1", ws.id, "developer-sonnet", "claude", "boss", "assign", "processing", "@1", None,
                   time.time(), task="Parse the flags", done_when="pytest tests/test_parse.py passes",
                   weight="medium")
    db.add_agent(worker)
    agents.handle_hook(db, "w1", "session-start", {})      # its baseline
    (Path(ws.path) / "parse.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm 'Add the flag parser'", Path(ws.path))
    (Path(ws.path) / "notes.txt").write_text("half done\n")
    task = tasks.record_started(db, root, worker, "developer-sonnet", "Parse the flags", "assign",
                                isolate=True, branch=ws.branch, done_when=worker.done_when, files=None,
                                depends_on=None, weight="medium")

    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status not in ("paused", "rewound"))
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    spawned = []
    during_spawn = []     # what runs while a launch is under way, once each

    def fake_spawn(db_, ws_, profile_name, *, prompt=None, parent_id=None, mode="interactive", done_when=None,
                   **kwargs):
        spawned.append({"profile": profile_name, "prompt": prompt, "ws": ws_.id, "mode": mode})
        a = Agent(f"new{len(spawned)}", ws_.id, profile_name, load_profile(profile_name, ws_.repo_root).provider,
                  parent_id, mode, "processing", f"@{len(spawned) + 1}", None, time.time(), task=prompt,
                  done_when=done_when, weight=kwargs.get("weight"))
        db_.add_agent(a)      # its row is made before the launch returns
        while during_spawn:
            during_spawn.pop()()
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    yield ws, spawned, task, during_spawn
    plugins.reset()


def out_of(provider: str, until: float = RESET) -> None:
    quota.record(provider, limited_until=until)


def cut_off(db, agent_id: str):
    """``agent_id``'s turn ends at its provider's usage limit."""
    agents.handle_hook(db, agent_id, "stop-failure", {"error": "rate_limit", "error_type": "rate_limit"})


def test_with_a_note_a_fresh_worker_starts_on_the_tables_pick_led_by_the_note(db, setup, monkeypatch):
    from brindle import checkpoint, handover

    ws, spawned, task, _ = setup
    db.update_agent("w1", pipeline="fixing", pipeline_rounds=2)
    checkpoint.record_note(db, db.get_agent("w1"), "done: the parser\nleft: the -v flag\ntraps: none")
    out_of("claude")
    cut_off(db, "w1")

    [launch] = spawned
    assert launch["profile"] == "dev.gemini" and launch["ws"] == ws.id   # the table's pick, same worktree
    text = launch["prompt"]
    assert text.startswith("Parse the flags") and handover.RAN_OUT in text
    assert text.index("done: the parser") < text.index("From brindle's records")
    fresh, old = db.get_agent("new1"), db.get_agent("w1")
    assert fresh.task == "Parse the flags" and (fresh.pipeline, fresh.pipeline_rounds) == ("fixing", 2)
    assert db.get_task(task.id).agent_id == "new1"
    assert old.status == "rewound" and old.dismissed_at is not None


def test_without_a_note_the_brief_is_built_from_the_snapshot_and_worktree(db, setup, repo, tmp_path):
    from brindle import rewind

    ws, spawned, _, during_spawn = setup
    transcript = tmp_path / "w1.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Wired the parser in; the -v flag is next"}]}}) + "\n")
    db.update_agent("w1", transcript_path=str(transcript))
    # Held for read_messages, unread, with a "new messages" notice already out about it.
    db.enqueue_held("w1", "Also take -q", "boss")
    db.enqueue("w1", "1 new message", "boss", notice=True)
    during_spawn.append(lambda: db.enqueue("w1", "And --json, please", "boss"))   # arrives mid-launch
    out_of("claude")
    cut_off(db, "w1")

    [launch] = spawned
    text = launch["prompt"]
    for part in ("Add the flag parser", "notes.txt", "the -v flag is next", "turn 1", "Also take -q"):
        assert part in text
    assert "handover note:" not in text and "--json" not in text
    assert [m.body for m in db.unread("w1")] == []                                    # quoted: settled
    assert db.pending_count("new1") == 1 and db.pending_count("w1") == 0             # the late one moved over
    assert [n for n, _ in rewind.turns(str(repo), "new1")] == [1]                       # its snapshot carried over
    assert rewind.baseline(ws, "new1") == rewind.baseline(ws, "w1")


def test_a_second_trigger_of_the_same_stop_starts_nothing(db, setup):
    _, spawned, _, during_spawn = setup
    out_of("claude")
    # While the hook's handover is launching the replacement (the worker still
    # idle at that stop), a sweep reaches the same stop.
    during_spawn.append(lambda: limit_report.on_stop_failure(db, db.get_agent("w1"), rate_limited=True))
    cut_off(db, "w1")

    assert [s["profile"] for s in spawned] == ["dev.gemini"]
    assert db.get_agent("new1").status == "processing" and db.get_agent("w1").status == "rewound"


def test_a_third_stop_parks_the_task_with_one_inbox_card(db, setup):
    from brindle import handover, learning

    ws, spawned, _, _ = setup
    out_of("claude")
    cut_off(db, "w1")                    # hop 1: Gemini
    out_of("antigravity")
    cut_off(db, "new1")                  # hop 2: Codex, the only measured provider left
    # The learner hears the merge as a takeover: from the profile before it, after two hops.
    assert learning.handover(db, db.get_agent("new2"), ws.repo_root) == ("dev.gemini", 2)
    # A third stop: even with Codex back within the park window, the person decides.
    out_of("codex", time.time() + 10 * 60)
    cut_off(db, "new2")

    assert [s["profile"] for s in spawned] == ["dev.gemini", "dev.gpt"]
    assert db.get_agent("new2").status == "paused"
    [card] = handover.inbox_items(db, ws.repo_root)
    assert card.kind == handover.KIND and card.agent_id == "new2"
    assert handover.answer(db, card.request_id, "stop") and handover.inbox_items(db, ws.repo_root) == []
    assert len(spawned) == 2 and db.get_agent("new2").status == "paused"


@pytest.mark.parametrize("billed", ["org key", pytest.param("managed route", marks=pytest.mark.variant(
    "an org's managed route (agent_setup) bills per token with no key of the org's or the person's in sight"))])
def test_a_replacement_billed_through_the_org_waits_for_the_persons_yes(db, setup, monkeypatch, billed):
    from brindle import handover
    from brindle.pro import managed_models

    ws, spawned, _, _ = setup
    # No personal key or ambient cloud switch anywhere: the org bills Gemini per token,
    # through a key it supplies, or through the managed route a launch would take.
    if billed == "org key":
        monkeypatch.setattr(managed_models, "has_org_key", lambda m, provider: provider == "antigravity")
    else:
        routes = {"claude": {"CLAUDE_CODE_USE_BEDROCK": "1"},
                  "antigravity": {"GOOGLE_GEMINI_BASE_URL": "https://llm-gateway.example"}}
        monkeypatch.setattr(managed_models, "agent_env", lambda m, provider: dict(routes.get(provider, {})))
    out_of("claude")
    cut_off(db, "w1")

    assert spawned == [] and db.get_agent("w1").status == "paused"
    [card] = handover.inbox_items(db, ws.repo_root)
    assert [c.id for c in card.choices] == ["continue", "wait", "stop"]
    handover.answer(db, card.request_id, "continue")
    assert [s["profile"] for s in spawned] == ["dev.gemini"] and db.get_agent("w1").status == "rewound"


@pytest.mark.parametrize("when", [pytest.param("routing", marks=pytest.mark.variant(
    "regression: the handover's cascade ran with no budget gate and started a profile over the budget")),
                                  pytest.param("offered", marks=pytest.mark.variant(
    "a pick offered on an Inbox card before the budget tightened is checked again at launch"))])
def test_a_replacement_over_the_budget_is_never_started(db, setup, repo, monkeypatch, when):
    from brindle import budget, handover

    ws, spawned, _, _ = setup

    def over() -> None:
        """A $1 task budget, which Gemini (about $5 a task) doesn't fit."""
        cfg = json.loads((repo / ".brindle" / "config.json").read_text())
        (repo / ".brindle" / "config.json").write_text(json.dumps({**cfg, "budget": {"task_usd": 1}}))

    monkeypatch.setattr(budget, "entitled", lambda: True)
    monkeypatch.setattr(budget, "estimate", lambda db_, root, profile, weight, extra=None:
                        5.0 if profile == "dev.gemini" else 0.5)
    out_of("claude")
    if when == "routing":
        over()
        cut_off(db, "w1")
        assert [s["profile"] for s in spawned] == ["dev.gpt"]     # the table's pick didn't fit
        return
    # The person is asked about Gemini first (billed per token); the budget tightens before they answer.
    monkeypatch.setattr(handover, "per_token", lambda profile, repo_root: profile == "dev.gemini")
    cut_off(db, "w1")
    [card] = handover.inbox_items(db, ws.repo_root)
    over()
    handover.answer(db, card.request_id, "continue")
    assert spawned == [] and db.get_agent("w1").status == "paused"


@pytest.mark.variant("regression: a handoff_detached worker's replacement was spawned in that raw mode, "
                     "so it lost its weight and the worker's report_result footer")
def test_a_detached_handoffs_replacement_is_briefed_and_weighted_as_a_handoff(db, setup, monkeypatch):
    _, _, task, _ = setup
    launched = []
    monkeypatch.setattr(agents, "spawn", REAL_SPAWN)
    monkeypatch.setattr(agents, "_launch", lambda db_, agent, ws_, prompt=None, **kw: launched.append(prompt))
    # A queued handoff nobody waits on, its weight stored as spawn stores it.
    db.update_agent("w1", mode="handoff_detached", weight="medium")
    out_of("claude")
    cut_off(db, "w1")

    fresh = db.get_agent(db.get_task(task.id).agent_id)
    assert fresh.id != "w1" and fresh.mode == "handoff_detached" and fresh.weight == "medium"
    [prompt] = launched
    assert "report_result" in prompt                    # the worker's finish protocol


def test_recovery_adopts_only_the_launch_its_handover_recorded(db, setup, monkeypatch):
    from brindle import handover

    ws, spawned, task, _ = setup
    launch = agents.spawn

    class Died(BaseException):
        """The process stops mid-launch, before the replacement has a row."""

    def die(*a, **k):
        raise Died

    monkeypatch.setattr(agents, "spawn", die)
    out_of("claude")
    with pytest.raises(Died):
        cut_off(db, "w1")
    # Meanwhile the supervisor assigns another task in the same worktree.
    db.add_agent(Agent("other", ws.id, "developer-codex", "codex", "boss", "assign", "processing", "@9", None,
                       time.time() + 1, task="Write the docs"))
    monkeypatch.setattr(agents, "spawn", launch)

    assert handover.sweep(db, now=time.time() + handover.CLAIM_EXPIRES + 5)
    assert db.get_agent("other").task == "Write the docs" and db.get_agent("other").dismissed_at is None
    assert [s["profile"] for s in spawned] == ["dev.gemini"]          # its own replacement, started now
    assert db.get_task(task.id).agent_id == "new1" and db.get_agent("w1").status == "rewound"


def test_a_parked_worker_waits_for_its_paused_session(db, setup, monkeypatch):
    from brindle import handover

    _, spawned, _, _ = setup
    resumed = []
    monkeypatch.setattr(agents, "resume", lambda db_, root, only=None, **kw: resumed.append(set(only or ())))
    out_of("claude", time.time() + 10 * 60)
    cut_off(db, "w1")                                   # back in 10 minutes: parked
    assert spawned == [] and db.get_agent("w1").status == "paused"

    db.set_status("boss", "paused")                     # the person pauses the session
    later = time.time() + 11 * 60                       # and the provider is back
    handover.sweep(db, now=later)
    assert resumed == [] and spawned == []
    db.set_status("boss", "processing")
    handover.sweep(db, now=later)
    assert resumed == [{"w1"}]
