"""End to end: usage recording -> pricing -> dollar spend, as users hit it.

Transcripts are written to disk and read back through ``usage``/``history``;
history rows are seeded into a temporary db; then the CLI (`brindle history`,
`brindle cost`, `brindle cost report`, `brindle cost estimate`), the budget
gate, the MCP replies and the sidebar's Costs section are driven. No network.
"""
import asyncio
import json
import time

import pytest
from typer.testing import CliRunner

from brindle import autopilot, budget, cost, cost_estimate, git, history, mcp_server, quota
from brindle import usage as usage_mod
from brindle import watch, watch_costs, workspaces
from brindle.cli import app
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.pro import license

DAY = 86400

# claude-sonnet-5-5: $2 in, $10 out, $2.50 cache write, $0.20 cache read per MTok
MSG1 = dict(input=1_000_000, output=2_000_000, cache_read=100_000_000, cache_creation=10_000_000)  # $67
MSG2 = dict(input=500_000, output=1_000_000)                                                      # $11


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")


@pytest.fixture(autouse=True)
def no_pro_by_default(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)


@pytest.fixture
def root(repo, monkeypatch):
    monkeypatch.chdir(repo)
    return git.main_repo_root(str(repo))


@pytest.fixture
def cli():
    return CliRunner()


def assistant(msg_id, u, model="claude-sonnet-5-5"):
    return json.dumps({"type": "assistant", "message": {
        "id": msg_id, "model": model,
        "usage": {"input_tokens": u.get("input", 0), "output_tokens": u.get("output", 0),
                  "cache_read_input_tokens": u.get("cache_read", 0),
                  "cache_creation_input_tokens": u.get("cache_creation", 0)}}}) + "\n"


def row_tokens(model="claude-sonnet-5-5", **u):
    return json.dumps({"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, **u, "model": model})


def seed(db, root, kind="worker_result", *, tokens=None, age_days=0.0, **kw):
    db.add_history(root, kind, tokens=tokens, **kw)
    db.conn.execute("UPDATE history SET ts=? WHERE id=(SELECT MAX(id) FROM history)",
                    (time.time() - age_days * DAY,))
    db.conn.commit()


def profile(repo, name, model=None, base_url=None):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    extra = (f"model: {model}\n" if model else "") + (f"base_url: {base_url}\n" if base_url else "")
    provider = "native" if base_url else "claude"
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: t\nprovider: {provider}\n{extra}---\nGo.\n")


def config(repo, **kw):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(kw))


# -- recording: transcript -> history rows, never double counted ----------------------------------


def test_transcript_usage_becomes_history_rows_without_double_counting(db, repo, root, tmp_path, cli):
    ws = workspaces.adopt_root(db, str(repo))
    transcript = tmp_path / "session.jsonl"
    # a streamed message appears on two lines with the same id: counted once
    transcript.write_text(assistant("m1", MSG1) + assistant("m1", MSG1))
    agent = Agent("w1", ws.id, "developer", "claude", None, "assign", "processing", "", None,
                  time.time(), transcript_path=str(transcript))
    db.add_agent(agent)

    history.record_safely(db, root, "worker_result", agent=agent, with_usage=True,
                          branch="feat/a", result="done")
    with transcript.open("a") as f:
        f.write(assistant("m2", MSG2))
    history.record_safely(db, root, "review", agent=agent, with_usage=True, branch="feat/a",
                          result="Review of feat/a: APPROVED")

    rows = db.list_history(root, None, 10)
    first, second = json.loads(rows[1].tokens), json.loads(rows[0].tokens)
    assert first["input"] == 1_000_000 and first["cache_read"] == 100_000_000
    assert second["input"] == 500_000 and second["output"] == 1_000_000 and second["cache_read"] == 0
    assert second["model"] == "claude-sonnet-5-5"

    out = cli.invoke(app, ["history"]).output
    assert "worker_result" in out and "review" in out
    # msg1: 1M + 2M + 100M + 10M = 113M; msg2: 0.5M + 1M = 1.5M. Counted once, never doubled.
    total = sum(history.tokens_total(r.tokens) for r in rows)
    assert f"total tokens: {usage_mod.format_tokens(total)}" in out
    assert total == 114_500_000

    spent = cost.summary(db, root)
    assert spent.total.dollars == pytest.approx(78.0)
    assert spent.by_model["claude-sonnet-5-5"].dollars == pytest.approx(78.0)
    assert "$78.00" in cli.invoke(app, ["cost"]).output


# -- pricing --------------------------------------------------------------------------------------


def test_a_mixed_total_says_how_many_tokens_were_free(db, repo, root, cli):
    profile(repo, "local", "qwen3", base_url="http://localhost:11434/v1")
    seed(db, root, tokens=row_tokens(input=1_000_000))                                # $2
    seed(db, root, profile="local", tokens=row_tokens(None, output=9_000_000))        # free
    seed(db, root, tokens=row_tokens("totally-new-model", input=4_000_000))           # unpriced
    out = cli.invoke(app, ["cost"]).output
    assert "last 30 days: $2.00 (+9000k tokens free) (+4000k tokens unpriced)" in out
    b = cost.Bucket()
    b.add(0.0, 5, free=True)
    assert b.show() == "Free" and b.all_tokens == 5
    b.add(None, 7)
    assert b.show() == "Free (+7 tokens unpriced)"


def test_a_cost_with_no_usage_says_so(db, root, cli):
    out = cli.invoke(app, ["cost"]).output
    assert "$0.00" in out and "no agent usage on record" in out


# -- estimates: set_goal / assign / CLI -----------------------------------------------------------


@pytest.fixture
def boss(db, repo, monkeypatch):
    config(repo, pipeline=False)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(cost_estimate, "available_reviewers", lambda r: [])
    return ws


def test_set_goal_shows_a_priced_estimate_with_pro(db, repo, boss, pro):
    profile(repo, "developer", "claude-sonnet-5-5")
    config(repo, pipeline=False, default_agent="developer")
    out = mcp_server.set_goal("Settings", [{"title": "API", "check": "true"},
                                           {"title": "UI", "check": "true"}])
    assert "Cost estimate" in out and "2 tasks" in out and "$" in out and "tokens (no price" not in out


def test_set_goal_and_assign_have_no_estimate_without_pro(db, boss, monkeypatch):
    from brindle import agents

    out = mcp_server.set_goal("Settings", [{"title": "API", "check": "true"}])
    assert "Cost estimate" not in out

    def spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
        a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "", None,
                  time.time(), task=prompt)
        db.add_agent(a)
        return a

    monkeypatch.setattr(agents, "spawn", spawn)
    assert "Cost estimate" not in asyncio.run(mcp_server.assign(task="do A", branch="feat-a"))


# -- budgets --------------------------------------------------------------------------------------

PRICES = {"big-model": {"input": 10, "output": 50, "cache_write": 12, "cache_read": 1},
          "small-model": {"input": 1, "output": 5, "cache_write": 1.2, "cache_read": 0.1}}


@pytest.fixture
def routed(db, repo, monkeypatch, pro):
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)
    profile(repo, "big", "big-model")
    profile(repo, "small", "small-model")
    profile(repo, "mystery", "no-such-model")
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    db.update_autopilot("boss", goal="Ship it", enabled=1, state="running")
    return ws


def budget_config(repo, **kw):
    config(repo, pricing=PRICES, routing={"medium": ["big", "small"]}, **kw)


def choose(db, repo):
    name, _ = autopilot.choose_profile(db, "boss", str(repo), weight="medium", why=[], decision={})
    return name


def test_month_budget_counts_spend_recorded_from_a_real_transcript(db, repo, root, routed, tmp_path):
    budget_config(repo, budget={"month_usd": 6})
    assert choose(db, repo) == "big"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(assistant("m1", {"input": 300_000}, model="big-model"))          # $3
    ws = workspaces.adopt_root(db, str(repo))
    agent = Agent("w1", ws.id, "big", "claude", "boss", "assign", "processing", "", None, time.time(),
                  transcript_path=str(transcript))
    db.add_agent(agent)
    history.record_safely(db, root, "worker_result", agent=agent, with_usage=True)
    assert choose(db, repo) == "small"          # 3 + ~4.6 > 6; 3 + ~0.46 fits


def test_unpriced_profile_is_not_skipped_by_a_repo_budget_but_is_by_an_org_one(db, repo, routed, monkeypatch):
    from brindle.pro import team_policy

    budget_config(repo, budget={"task_usd": 1})
    gate = budget.Gate(db, load_repo_config(repo), str(repo), "boss")
    assert gate.why_not("mystery", "medium") is None
    monkeypatch.setattr(team_policy, "org_budgets", lambda r: team_policy.OrgPolicy(
        org_id="org_acme", version=1, budget_seat_month_usd=1000.0))
    gate = budget.Gate(db, load_repo_config(repo), str(repo), "boss")
    assert "no known price" in gate.why_not("mystery", "medium")


def test_an_unreadable_org_budget_exhausts_the_month(db, repo, routed, monkeypatch):
    from brindle.pro import team_policy

    def down(root):
        raise RuntimeError("policy server down")

    budget_config(repo)
    monkeypatch.setattr(team_policy, "org_budgets", down)
    lim = budget.limits(load_repo_config(repo), str(repo))
    assert lim.month_usd == 0.0 and lim.org
    assert budget.Gate(db, load_repo_config(repo), str(repo), "boss").why_not("small", "medium")


# -- savings --------------------------------------------------------------------------------------


def test_savings_in_dollars_from_routing_decisions(db, repo, root, routed):
    from brindle import savings

    budget_config(repo)
    for n in range(5):                         # learning moved 5 tasks from big to small
        db.add_routing_decision(root, task_id=f"t{n}", agent_id=f"s{n}", weight="medium",
                                baseline_profile="big", profile="small", learned=True)
        db.note_routing_outcome(f"s{n}", outcome="merged")
        seed(db, root, agent_id=f"s{n}", profile="small", tokens=row_tokens("small-model", input=1_000_000))
    periods = savings.report(db, root)
    p = periods[-1]
    assert p.dollars and p.compared == 5
    assert p.actual_cost == pytest.approx(5.0) and p.baseline_cost == pytest.approx(50.0)
    text = savings.describe(periods, root)
    assert "$5.00 against an estimated $50.00" in text and "90% lower cost" in text

    rep = cost.savings(db, root)
    assert rep.top_profile == "big" and rep.routed == 5
    assert rep.routing_saved == pytest.approx(45.0)


# -- the sidebar's Costs section ------------------------------------------------------------------


def test_costs_section_from_real_history(db, repo, root, pro):
    now = time.time()
    seed(db, root, tokens=row_tokens(input=2_000_000))                                # $4
    seed(db, root, tokens=row_tokens(input=1_000_000))                                # $2
    seed(db, root, tokens=row_tokens("never-heard-of-it", input=1_000_000))           # unpriced
    seed(db, root, tokens=row_tokens(input=9_000_000), age_days=40)                   # last month
    local = watch_costs.local(db, root, [], now=now)
    assert local.month == pytest.approx(6.0)
    assert sorted(t for _, t in local.models) == [1_000_000, 3_000_000]
    lines = watch_costs.render(local, watch_costs.Remote(limits=budget.Limits(month_usd=8.0), at=now),
                               now, 70, False)
    assert lines[0].text.startswith("▾ Costs")
    assert local.providers == []     # no provider has reported its usage: none is shown
    assert any("$6.00 of $8.00" in ln.text and ln.style == "ok" for ln in lines[1:])
    folded = watch_costs.render(local, None, now, 70, True)
    assert len(folded) == 1 and "75%)" in folded[0].text


def test_costs_section_shows_a_running_workers_dollars(db, repo, root, pro):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "", None, time.time()))
    seed(db, root, agent_id="w1", profile="developer", tokens=row_tokens(input=1_000_000))
    snap = [{"id": ws.id, "agents": [{"id": "w1", "mode": "assign", "status": "processing"},
                                     {"id": "gone", "mode": "assign", "status": "done"}]}]
    local = watch_costs.local(db, root, snap)
    assert local.workers == {"w1": pytest.approx(2.0)}


def test_costs_section_shows_free_for_a_local_workers_figure(db, repo, root, pro):
    profile(repo, "local", "qwen3", base_url="http://localhost:11434/v1")
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("w1", ws.id, "local", "native", "boss", "assign", "processing", "", None, time.time()))
    db.add_agent(Agent("w2", ws.id, "developer", "claude", "boss", "assign", "processing", "", None, time.time()))
    seed(db, root, agent_id="w1", profile="local", tokens=row_tokens(None, output=1_000_000))
    seed(db, root, agent_id="w2", profile="developer", tokens=row_tokens(input=1_000_000))
    snap = [{"id": ws.id, "agents": [{"id": "w1", "mode": "assign", "status": "processing"},
                                     {"id": "w2", "mode": "assign", "status": "processing"}]}]
    local = watch_costs.local(db, root, snap)
    assert local.workers == {"w1": "Free", "w2": pytest.approx(2.0)}
    assert local.month == pytest.approx(2.0)          # free usage still counts as $0 toward budgets
    a = {"id": "w1", "profile": "local", "provider": "native", "status": "processing"}
    text = " ".join(ln.text for ln in watch.render_agent(a, {}, time.time(), 70, local.workers["w1"]))
    assert "· Free ·" in text and "$0.00" not in text


def test_costs_section_survives_a_broken_history(db, root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(cost, "priced_rows", boom)
    local = watch_costs.local(db, root, [])
    assert local.month == 0.0 and local.models == []
    assert "no usage this month" in watch_costs.render(local, None, time.time(), 70, False)[1].text


def test_the_dashboard_renders_the_costs_section(db, repo, root):
    local = watch_costs.Local(month=3.0, models=[("sonnet", 300), ("haiku", 100)], workers={"a1": 0.5})
    ws = {"id": "repo/feat", "name": "feat", "branch": "feat", "base_branch": "main", "path": "/",
          "ahead": 0, "behind": 0, "dirty": 0,
          "agents": [{"id": "a1", "profile": "developer", "provider": "claude", "status": "processing",
                      "mode": "assign", "status_since": 1000.0, "pending": 0, "reported": False,
                      "window": "@1", "tokens": "12k tokens"}]}
    texts = [ln.text for ln in watch.render([ws], 1090, 80, costs=(local, None))]
    assert any(t.strip().startswith("sonnet") and t.rstrip().endswith("75%") for t in texts)
    assert any("12k tokens · $0.50" in t for t in texts)
