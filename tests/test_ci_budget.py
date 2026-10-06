"""``brindle ci run --budget`` and the run's token report (``brindle.ci_budget``):
parsing the budget, summing the supervisor and its workers from their
transcripts, stopping an over-budget run with status ``budget``, and the
tokens, models and profiles in the outcome, step summary and PR body."""

import functools
import json
import time

import pytest
from typer.testing import CliRunner

from brindle import ci, ci_budget
from brindle.cli import app
from brindle.db import Agent
from brindle.usage import Usage
from test_ci import FakeSession, finish_goal, run


def write_turn(path, msg_id, *, inp=0, out=0, cache_read=0, model="claude-sonnet-5-5"):
    """Append one assistant message to a Claude Code-shaped transcript."""
    entry = {"type": "assistant", "message": {
        "id": msg_id, "model": model,
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read}}}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def add_worker(db, ws_id, agent_id, transcript, profile="claude-light", provider="claude"):
    db.add_agent(Agent(agent_id, ws_id, profile, provider, "sup1", "assign", "processing", "%10",
                       None, time.time(), transcript_path=str(transcript) if transcript else None))


# -- parsing ----------------------------------------------------------------------


@pytest.mark.parametrize("text,tokens", [
    ("500000", 500_000), ("500k", 500_000), ("2m", 2_000_000), ("1.5M", 1_500_000),
    ("750k tokens", 750_000), ("1,000,000", 1_000_000), (None, None), (3000, 3000),
])
def test_parse_budget(text, tokens):
    assert ci_budget.parse_budget(text) == tokens


@pytest.mark.parametrize("text", ["", "lots", "-5k", "0", "$20", "20usd"])
def test_parse_budget_refuses(text):
    with pytest.raises(ci_budget.BudgetError):
        ci_budget.parse_budget(text)


def test_dollars_are_refused_with_the_reason():
    with pytest.raises(ci_budget.BudgetError, match="dollars"):
        ci_budget.parse_budget("$5")


# -- the tracker ------------------------------------------------------------------


def _agent(aid, profile="claude", provider="claude"):
    return Agent(aid, "ws", profile, provider, None, "assign", "processing", "", None, 0.0)


def test_tracker_sums_the_tree_and_remembers_agents_that_are_gone(db):
    tree = [_agent("sup", "supervisor"), _agent("w1", "claude-light"),
            _agent("cx", "codex", "codex")]
    used = {"sup": Usage(100, 10, 1000, 0, "claude-opus-5-5"),
            "w1": Usage(50, 5, 0, 0, "claude-haiku-4-5")}
    t = ci_budget.Tracker(db, "sup", budget=2000, tree_of=lambda db, root: list(tree),
                          usage_of=lambda db, a: used.get(a.id))
    assert t.update() == 1165
    assert t.over_budget() is None

    # w1's handoff finished and its row was deleted: it still counts.
    tree.remove(tree[1])
    used["sup"] = Usage(200, 20, 2000, 0, "claude-opus-5-5")
    assert t.update() == 2275
    assert "over the budget of 2k" in t.over_budget()

    s = t.summary()
    assert s["tokens"]["total"] == 2275 and s["tokens"]["output"] == 25
    assert s["budget"] == 2000 and s["estimated_cost_usd"] is None
    assert s["models"] == ["haiku", "opus"]
    assert s["profiles"] == ["claude-light", "codex", "supervisor"]
    assert s["untracked"] == ["cx"]


def test_tracker_never_breaks_the_run(db):
    def broken(db, root):
        raise RuntimeError("db locked")

    t = ci_budget.Tracker(db, "sup", budget=1, tree_of=broken)
    assert t.update() == 0 and ci_budget.check(t) is None


def test_no_budget_never_stops(db):
    t = ci_budget.Tracker(db, "sup", tree_of=lambda db, root: [_agent("sup")],
                          usage_of=lambda db, a: Usage(10**9))
    assert ci_budget.check(t) is None and t.total == 10**9
    assert ci_budget.check(None) is None


# -- the run ----------------------------------------------------------------------


def _with_transcripts(tmp_path, per_tick):
    """A supervisor script: on the first tick give sup1 a transcript and start
    a worker; every tick both spend ``per_tick`` more tokens."""
    sup_t, w_t = tmp_path / "sup.jsonl", tmp_path / "w1.jsonl"

    def script(session, root_id):
        db = session.db
        if session.ticks == 1:
            db.update_agent(root_id, transcript_path=str(sup_t))
            add_worker(db, db.get_agent(root_id).workspace_id, "w1", w_t)
        write_turn(sup_t, f"s{session.ticks}", inp=per_tick, out=10, model="claude-opus-5-5")
        write_turn(w_t, f"w{session.ticks}", inp=per_tick, cache_read=5, model="claude-sonnet-5-5")

    return script


def test_over_budget_run_stops_with_status_budget(db, repo, monkeypatch, tmp_path):
    s = FakeSession(db, monkeypatch, script=_with_transcripts(tmp_path, 1000))
    out = run(db, repo, ci.Goal("Add health"), s, budget=5000)
    assert out.status == "budget" and not out.ok
    assert "over the budget of 5k" in out.note
    assert s.stopped == ["sup1"] and s.pushed == [] and s.prs == []
    assert s.ticks == 3        # 2015 tokens a tick: over 5000 after the third
    assert out.usage["tokens"]["total"] > 5000
    assert out.usage["models"] == ["opus", "sonnet"]
    assert set(out.usage["profiles"]) == {"supervisor", "claude-light"}
    assert "tokens:" in out.describe() and "5k budget" in out.describe()


def test_a_finished_run_reports_tokens_in_the_pr_and_step_summary(db, repo, monkeypatch, tmp_path):
    spend = _with_transcripts(tmp_path, 1000)

    def script(session, root_id):
        spend(session, root_id)
        finish_goal(session, root_id, n=2)

    s = FakeSession(db, monkeypatch, script=script)
    out = run(db, repo, ci.Goal("Add health"), s, budget=1_000_000)
    assert out.status == "done" and out.ok
    body = s.prs[0][3]
    assert "## Usage" in body and "Models: opus, sonnet" in body and "Budget: 1000k" in body

    path = tmp_path / "summary.md"
    ci.write_step_summary(out, str(path))
    data = json.loads(path.read_text().split("```json\n")[1].split("\n```")[0])
    assert data["usage"]["tokens"]["total"] == out.usage["tokens"]["total"] > 0
    assert data["usage"]["models"] == ["opus", "sonnet"]
    assert "claude-light" in data["usage"]["profiles"]


def test_without_a_budget_tokens_are_still_reported(db, repo, monkeypatch, tmp_path):
    s = FakeSession(db, monkeypatch, script=_with_transcripts(tmp_path, 10**6))
    out = run(db, repo, ci.Goal("Add health"), s, timeout_min=1)
    assert out.status == "timeout"
    assert out.usage["budget"] is None and out.usage["tokens"]["total"] > 10**6


def test_cli_budget_stops_the_run(db, repo, monkeypatch, tmp_path):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci, "require_ci", lambda client=None: None)
    s = FakeSession(db, monkeypatch, script=_with_transcripts(tmp_path, 1000))
    monkeypatch.setattr(ci, "run", functools.partial(ci.run, clock=s.now, sleep=s.sleep))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Add health", "--budget", "5k"])
    assert res.exit_code == 1, res.output
    assert "brindle ci: budget" in res.output and "over the budget" in res.output
    text = summary.read_text()
    assert '"status": "budget"' in text and '"models"' in text and '"opus"' in text


def test_cli_refuses_a_bad_budget_before_starting(repo, monkeypatch):
    monkeypatch.chdir(repo)
    spawned = []
    monkeypatch.setattr(ci, "_spawn", lambda db, ws, prompt: spawned.append(prompt))
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Add health", "--budget", "$20"])
    assert res.exit_code == 1 and "dollars" in res.output and spawned == []
