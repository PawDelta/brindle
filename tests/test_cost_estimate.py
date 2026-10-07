import asyncio
import json
import re
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, cost_estimate as ce, mcp_server, workspaces
from brindle.cli import app
from brindle.db import Agent, Task
from brindle.pro import license

RATES = {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6}


def price(model, input, output, cache_write, cache_read):
    """A stand-in for brindle.pricing.price: one flat rate, None for an unknown model."""
    if model == "mystery":
        return None
    return (input * RATES["input"] + output * RATES["output"]
            + cache_write * RATES["cache_write"] + cache_read * RATES["cache_read"])


def tokens(i, o=0, cw=0, cr=0, model="claude-sonnet"):
    return json.dumps({"input": i, "output": o, "cache_creation": cw, "cache_read": cr,
                       "model": model})


def add_task(db, repo_root, agent_id, branch, weight, profile="developer"):
    db.add_task(Task(f"t-{agent_id}", repo_root, agent_id, "boss", "ws", profile, "do it",
                     "assign", 1, branch, None, None, None, "started", time.time(),
                     weight=weight))


def seed(db, repo_root, n, *, profile="developer", reviewer="reviewer", weight="medium",
         base=100_000, model="claude-sonnet"):
    """n workers, each reviewed once; worker i used base*(i+1) input tokens."""
    for i in range(n):
        w, r, branch = f"w{weight}{i}", f"r{weight}{i}", f"feat/{weight}{i}"
        add_task(db, repo_root, w, branch, weight, profile)
        # two rows for one worker: their deltas add up to the run
        db.add_history(repo_root, "worker_result", agent_id=w, branch=branch, profile=profile,
                       tokens=tokens(base * (i + 1) // 2, 1000 * (i + 1), model=model))
        db.add_history(repo_root, "worker_result", agent_id=w, branch=branch, profile=profile,
                       tokens=tokens(base * (i + 1) // 2, 1000 * (i + 1), model=model))
        db.add_history(repo_root, "review", agent_id=r, branch=branch, profile=reviewer,
                       tokens=tokens(10_000, 500, model=model))
        db.add_history(repo_root, "merge", agent_id="boss", branch=branch,
                       tokens=tokens(999_999, model=model))


def dollars(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\$([\d,]+\.?\d*)", text)]


# -- history -> runs -> distributions ---------------------------------------------------


def test_runs_sum_an_agents_rows_and_find_role_and_weight(db):
    seed(db, "/repo", 2)
    runs = sorted(ce.runs(db, "/repo"), key=lambda r: (r.role, r.tokens.input))
    workers = [r for r in runs if r.role == "worker"]
    reviews = [r for r in runs if r.role == "review"]
    assert [r.tokens.input for r in workers] == [100_000, 200_000]
    assert [r.tokens.output for r in workers] == [2000, 4000]
    assert all(r.weight == "medium" and r.profile == "developer" for r in workers)
    # a reviewer's weight comes from the task of the branch it reviewed
    assert len(reviews) == 2 and all(r.weight == "medium" for r in reviews)
    assert all(r.profile == "reviewer" for r in reviews)
    # supervisor merge rows aren't anyone's run
    assert all(r.tokens.input < 999_999 for r in runs)


def test_weight_from_routing_decisions(db):
    db.add_history("/repo", "worker_result", agent_id="a1", profile="developer",
                   tokens=tokens(5000))
    db.add_routing_decision("/repo", task_id=None, agent_id="a1", weight="heavy",
                            baseline_profile="developer", profile="developer", learned=False)
    (run,) = ce.runs(db, "/repo")
    assert run.weight == "heavy"


def test_distribution_median_and_p80():
    runs = [ce.Run("worker", "developer", "medium", ce.Tokens(i * 100, i * 10)) for i in range(1, 6)]
    d = ce.distribution(runs)
    assert d.n == 5
    assert d.p50 == ce.Tokens(300, 30)
    assert d.p80 == ce.Tokens(420, 42)


# -- estimates ----------------------------------------------------------------------------


def test_enough_history_gives_a_confident_priced_range(db):
    seed(db, "/repo", 6)
    dists = ce.distributions(ce.runs(db, "/repo"))
    est = ce.estimate([ce.TaskSpec("developer", "medium")] * 3, "reviewer", dists=dists, price=price)
    assert est.confident and est.priced
    assert est.tasks == 3 and len(est.parts) == 6
    worker = est.parts[0]
    d = dists[("worker", "developer", "medium")]
    assert worker.low == pytest.approx(price(None, d.p50.input, d.p50.output, 0, 0))
    assert worker.high == pytest.approx(price(None, d.p80.input, d.p80.output, 0, 0))
    assert est.low < est.high
    assert est.low == pytest.approx(sum(p.low for p in est.parts))


def test_other_weights_pool_when_the_exact_one_is_thin(db):
    seed(db, "/repo", 6, weight="medium")
    dists = ce.distributions(ce.runs(db, "/repo"))
    p = ce.part(dists, "worker", "developer", "light", price, None)
    assert p.confident and p.runs == 6


def test_thin_history_says_low_confidence_and_still_gives_a_range(db):
    seed(db, "/repo", 2)
    text = ce.describe([ce.TaskSpec("developer", "medium")], "reviewer", repo_root="/repo", db=db,
                       price=price, candidates=[])
    assert "low confidence" in text
    low, high = dollars(text)[:2]
    assert low < high


def test_never_a_single_number_even_when_percentiles_match(db):
    for i in range(6):
        db.add_history("/repo", "worker_result", agent_id=f"w{i}", profile="developer",
                       tokens=tokens(100_000))
        db.add_history("/repo", "review", agent_id=f"r{i}", profile="reviewer", tokens=tokens(1000))
    text = ce.describe([ce.TaskSpec("developer")], "reviewer", repo_root="/repo", db=db,
                       price=price, candidates=[])
    assert "low confidence" not in text
    low, high = dollars(text)[:2]
    assert low < high


def test_no_price_falls_back_to_a_token_range(db):
    seed(db, "/repo", 6, model="mystery")
    text = ce.describe([ce.TaskSpec("developer", "medium")], "reviewer", repo_root="/repo", db=db,
                       price=price, candidates=[])
    assert "tokens" in text and "$" not in text


def test_cheaper_alternative_prefers_a_local_reviewer(db):
    seed(db, "/repo", 6)
    seed(db, "/repo", 6, reviewer="reviewer-codex", weight="light", base=50_000)
    text = ce.describe([ce.TaskSpec("developer", "medium")] * 2, "reviewer", repo_root="/repo",
                       db=db, price=price, candidates=["reviewer", "reviewer-codex", "reviewer-local"])
    first, cheaper = text.splitlines()
    assert "Cheaper: review with reviewer-local instead of reviewer" in cheaper
    assert dollars(cheaper)[1] < dollars(first)[1]


def test_cheaper_alternative_without_local_takes_the_cheapest(db):
    seed(db, "/repo", 6)
    dists = ce.distributions(ce.runs(db, "/repo"))
    tasks = [ce.TaskSpec("developer", "medium")]
    # no history for the others: priced from the prior, which costs more than reviewer's runs here
    assert ce.cheaper(tasks, "reviewer", ["reviewer-codex"], dists=dists, price=price) is None
    # a candidate that is genuinely cheaper wins
    cheap = lambda model, i, o, cw, cr: 0.0 if model == "free" else price(model, i, o, cw, cr)
    for i in range(6):
        db.add_history("/repo", "review", agent_id=f"cx{i}", profile="reviewer-codex",
                       tokens=tokens(1000, model="free"))
    dists = ce.distributions(ce.runs(db, "/repo"))
    name, est = ce.cheaper(tasks, "reviewer", ["reviewer", "reviewer-codex"], dists=dists, price=cheap)
    assert name == "reviewer-codex"


def test_a_failing_price_function_is_unpriced_not_an_error(db):
    seed(db, "/repo", 6)

    def broken(*a):
        raise RuntimeError("no table")

    text = ce.describe([ce.TaskSpec("developer", "medium")], "reviewer", repo_root="/repo", db=db,
                       price=broken, candidates=[])
    assert "tokens" in text


# -- the Pro gate --------------------------------------------------------------------------


def test_reply_note_fails_closed(db, monkeypatch):
    seed(db, "/repo", 6)
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert ce.reply_note(db, "/repo", [ce.TaskSpec("developer")]) is None

    def explode(feature):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(license, "has", explode)
    assert ce.reply_note(db, "/repo", [ce.TaskSpec("developer")]) is None


def test_reply_note_when_entitled(db, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    monkeypatch.setattr(ce, "default_price", lambda *_: price)
    monkeypatch.setattr(ce, "available_reviewers", lambda root: [])
    note = ce.reply_note(db, "/repo", [ce.TaskSpec("developer")])
    assert note.startswith("Cost estimate") and "low confidence" in note


# -- MCP replies and the CLI ------------------------------------------------------------------


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"pipeline": False}))
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(ce, "default_price", lambda *_: price)
    monkeypatch.setattr(ce, "available_reviewers", lambda root: [])
    return ws


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


MILESTONES = [{"title": "API", "check": "true"}, {"title": "UI", "check": "true"}]


def test_set_goal_reply_has_an_estimate_with_pro(db, boss, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    out = mcp_server.set_goal("Settings page", MILESTONES)
    assert "Cost estimate" in out and "2 tasks" in out


def test_set_goal_reply_has_no_estimate_without_pro(db, boss, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)
    out = mcp_server.set_goal("Settings page", MILESTONES)
    assert "Cost estimate" not in out


def test_assign_reply_has_an_estimate_with_pro(db, boss, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a"))
    assert "Started worker" in out and "Cost estimate" in out and "1 task," in out


def test_cli_cost_estimate(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ce, "default_price", lambda *_: price)
    monkeypatch.setattr(ce, "available_reviewers", lambda root: [])
    monkeypatch.setattr(license, "has", lambda feature: False)
    res = CliRunner().invoke(app, ["cost", "estimate", "--tasks", "2"])
    assert res.exit_code == 1 and "brindle Pro" in res.output

    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    res = CliRunner().invoke(app, ["cost", "estimate", "--tasks", "2", "--weight", "light"])
    assert res.exit_code == 0, res.output
    assert "2 tasks" in res.output and "low confidence" in res.output
    assert len(dollars(res.output)) >= 2
