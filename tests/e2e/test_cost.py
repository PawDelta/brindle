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

from brindle import autopilot, budget, cost, cost_estimate, git, history, mcp_server, pricing, quota
from brindle import usage as usage_mod
from brindle import watch, watch_costs, workspaces
from brindle.cli import app
from brindle.config import load_repo_config
from brindle.db import Agent, Task
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


def test_usage_cost_prices_a_live_transcript(db, tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(assistant("m1", MSG1))
    u = usage_mod.transcript_usage(db, str(transcript))
    assert usage_mod.usage_cost(u) == pytest.approx(67.0)
    assert usage_mod.summary_line(u, 67.0).endswith("· sonnet · ~$67.00")
    assert usage_mod.usage_cost(usage_mod.Usage(1, 1, model="mystery-1")) is None


# -- pricing --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["claude-sonnet-5-5-20260101", "anthropic/claude-sonnet-5-5",
                                  "us.anthropic.claude-sonnet-5-5", "claude-sonnet-5-5[1m]",
                                  "SONNET"])
def test_model_names_resolve_to_their_price(name):
    assert pricing.price_for(name) == pricing.PRICES["claude-sonnet-5-5"]


def test_overrides_add_and_replace_prices(db, repo, root, cli):
    config(repo, pricing={"my-model": {"input": 1, "output": 5},
                          "claude-sonnet-5-5": {"input": 100, "output": 100}})
    seed(db, root, tokens=row_tokens("my-model", input=1_000_000, output=1_000_000, cache_read=1_000_000))
    seed(db, root, tokens=row_tokens(input=1_000_000))
    out = cli.invoke(app, ["cost"]).output
    # a missing cache_read is charged at the input rate: 1 + 5 + 1
    assert "$107.00" in out
    assert "my-model" in out and "$7.00" in out and "$100.00" in out


def test_unpriced_models_are_shown_as_unpriced_tokens_not_guessed(db, root, cli):
    seed(db, root, tokens=row_tokens("sonnet", input=1_000_000))                 # alias: $2
    seed(db, root, tokens=row_tokens("totally-new-model", input=4_000_000))
    out = cli.invoke(app, ["cost"]).output
    assert "last 30 days: $2.00 (+4000k tokens unpriced)" in out
    assert "totally-new-model" in out and "unknown (4000k tokens unpriced)" in out
    only = cost.summary(db, root)
    only_unknown = cost.Bucket()
    only_unknown.add(None, 5)
    assert only_unknown.show() == "unknown (5 tokens unpriced)"
    assert only.total.unknown == 4_000_000 and only.total.dollars == pytest.approx(2.0)


def test_a_row_without_a_model_is_priced_at_its_profile(db, repo, root):
    profile(repo, "big", "claude-opus-5-5")
    profile(repo, "local", "qwen3", base_url="http://localhost:11434/v1")
    seed(db, root, profile="big", tokens=row_tokens(None, input=1_000_000))
    seed(db, root, profile="local", tokens=row_tokens("qwen3-coder", output=9_000_000))
    s = cost.summary(db, root)
    assert s.total.dollars == pytest.approx(4.0)         # opus-5-5 $4/MTok input; the local model is $0
    assert s.total.unknown == 0


def test_a_local_profile_row_reads_free_in_brindle_cost(db, repo, root, cli):
    profile(repo, "local", "qwen3", base_url="http://localhost:11434/v1")
    seed(db, root, profile="local", tokens=row_tokens(None, output=9_000_000))
    out = cli.invoke(app, ["cost"]).output
    assert "last 30 days: Free" in out and "$0.00" not in out
    assert "profile local" in out and "9000k tokens" in out        # its tokens still count


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


def test_an_all_zero_pricing_override_is_free_but_no_price_is_not(db, repo, root, cli):
    config(repo, pricing={"gift-model": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}})
    seed(db, root, tokens=row_tokens("gift-model", input=1_000_000))
    assert "last 30 days: Free" in cli.invoke(app, ["cost"]).output


def test_only_the_last_n_days_count(db, root, cli):
    seed(db, root, tokens=row_tokens(input=1_000_000), age_days=1)        # $2
    seed(db, root, tokens=row_tokens(input=1_000_000), age_days=45)       # $2, too old
    assert "last 30 days: $2.00" in cli.invoke(app, ["cost"]).output
    assert "last 60 days: $4.00" in cli.invoke(app, ["cost", "--days", "60"]).output


def test_a_cost_with_no_usage_says_so(db, root, cli):
    out = cli.invoke(app, ["cost"]).output
    assert "$0.00" in out and "no agent usage on record" in out


def test_tiny_amounts_never_read_as_zero():
    assert pricing.money(0.004) == "<$0.01" and pricing.money(0) == "$0.00"
    assert pricing.money(1234.5) == "$1,234.50"


def test_stale_prices_warn_after_90_days():
    assert pricing.stale_warning(pricing.AS_OF) is None
    assert "out of date" in pricing.stale_warning(pricing.AS_OF.replace(year=pricing.AS_OF.year + 1))


# -- `brindle cost report` (Pro) ------------------------------------------------------------------


def test_cost_report_needs_pro_and_fails_closed(root, cli, monkeypatch):
    res = cli.invoke(app, ["cost", "report"])
    assert res.exit_code != 0 and "brindle Pro" in res.output

    def broken(feature):
        raise RuntimeError("no store")

    monkeypatch.setattr(license, "has", broken)
    assert cli.invoke(app, ["cost", "report"]).exit_code != 0


def test_cost_report_breaks_spend_down(db, repo, root, cli, pro):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    db.update_autopilot("boss", goal="Ship the settings page\nmore detail", enabled=1, state="running")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "", None,
                       time.time()))
    seed(db, root, agent_id="w1", branch="feat/a", profile="developer",
         tokens=row_tokens(input=5_000_000, cache_read=10_000_000))                       # $10 + $2
    seed(db, root, "review", agent_id="r1", branch="feat/a", profile="reviewer",
         tokens=row_tokens(input=1_000_000), result="Review of feat/a: APPROVED")         # $2
    seed(db, root, "review", agent_id="r2", branch="feat/b", profile="reviewer",
         result="Review of feat/b: CHANGES REQUESTED")
    seed(db, root, "merge", agent_id="boss", branch="feat/a")
    db.conn.execute("UPDATE history SET profile='developer' WHERE agent_id='w1'")
    db.conn.commit()

    out = cli.invoke(app, ["cost", "report"]).output
    assert "Cost report for proj, last 30 days: $14.00" in out
    assert "Ship the settings page" in out and "more detail" not in out     # first line of the goal
    assert "developer" in out and "reviewer" in out
    assert "Merged branches: 1, $14.00 each on average" in out
    assert "approved (" in out
    assert "cache-hit rate: 62% of input tokens (10000k of 16000k)" in out
    assert "Prices as of" in out


def test_cost_report_cost_per_merged_branch_counts_rows_before_the_window(db, root, pro):
    seed(db, root, agent_id="w1", branch="feat/old", tokens=row_tokens(input=1_000_000), age_days=40)  # $2
    seed(db, root, "merge", agent_id="boss", branch="feat/old", tokens=row_tokens(input=1_000_000))   # $2
    rep = cost.report(db, root)
    assert rep.merged == 1 and rep.per_merged_branch == pytest.approx(4.0)


# -- estimates: set_goal / assign / CLI -----------------------------------------------------------


def _runs(db, root, n, model="claude-sonnet-5-5"):
    for i in range(n):
        w, r, branch = f"w{i}", f"r{i}", f"feat/{i}"
        db.add_task(Task(f"t{i}", root, w, "boss", "ws", "developer", "x", "assign", 1, branch, None,
                         None, None, "started", time.time(), weight="medium"))
        seed(db, root, agent_id=w, branch=branch, profile="developer",
             tokens=row_tokens(model, input=1_000_000 * (i + 1), output=100_000))
        seed(db, root, "review", agent_id=r, branch=branch, profile="reviewer",
             tokens=row_tokens(model, input=100_000, output=10_000))


def test_the_default_price_function_really_prices(db, repo, root):
    """A history of sonnet runs yields dollars, not 'no price for some models'."""
    _runs(db, root, 6)
    text = cost_estimate.describe([cost_estimate.TaskSpec("developer", "medium")], "reviewer",
                                  repo_root=root, db=db, candidates=[])
    assert "tokens (no price" not in text and "$" in text and "confident" not in text.split(":")[0]
    # median worker: 3.5M in ($7) + 100k out ($1); median review: 0.1M in + 10k out ($0.2 + $0.1)
    est = cost_estimate.estimate([cost_estimate.TaskSpec("developer", "medium")], "reviewer",
                                 dists=cost_estimate.distributions(cost_estimate.runs(db, root)),
                                 price=cost_estimate.default_price(root))
    assert est.priced and est.low == pytest.approx(8.3)


def test_default_price_honours_repo_overrides_and_unknown_models(repo, root):
    config(repo, pricing={"my-model": {"input": 1, "output": 2}})
    price = cost_estimate.default_price(root)
    assert price("my-model", 1_000_000, 1_000_000, 0, 0) == pytest.approx(3.0)
    assert price("claude-sonnet-5-5", 1_000_000, 0, 0, 0) == pytest.approx(2.0)
    assert price("never-heard-of-it", 1, 1, 1, 1) is None and price(None, 1, 1, 1, 1) is None


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


def test_cli_estimate_is_a_range_with_a_cheaper_review(db, repo, root, cli, pro, monkeypatch):
    profile(repo, "developer", "claude-sonnet-5-5")
    profile(repo, "reviewer", "claude-opus-5-5")
    profile(repo, "reviewer-local", "qwen3", base_url="http://localhost:11434/v1")
    config(repo, default_agent="developer", reviewer="reviewer")
    monkeypatch.setattr(cost_estimate, "available_reviewers", lambda r: ["reviewer", "reviewer-local"])
    out = cli.invoke(app, ["cost", "estimate", "--tasks", "2", "--weight", "light"]).output
    assert "2 tasks" in out and "low confidence" in out and "–$" in out
    assert "Cheaper: review with reviewer-local instead of reviewer" in out


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


def test_budget_does_nothing_without_the_cost_feature(db, repo, routed, monkeypatch):
    budget_config(repo, budget={"task_usd": 0.1})
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert choose(db, repo) == "big"


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


def test_nothing_fits_refuses_with_the_reason(db, repo, routed):
    budget_config(repo, budget={"task_usd": 0.1})
    with pytest.raises(autopilot.AutopilotError) as e:
        choose(db, repo)
    assert "over budget" in str(e.value) and "Raise `budget`" in str(e.value)


def test_estimates_prefer_finished_history_over_the_typical_mix(db, repo, root, routed):
    budget_config(repo, budget={"task_usd": 1})
    for n in range(3):                         # three finished big tasks that cost $0.50 each
        db.add_routing_decision(root, task_id=f"t{n}", agent_id=f"h{n}", weight="medium",
                                baseline_profile="big", profile="big", learned=False)
        db.note_routing_outcome(f"h{n}", outcome="merged")
        seed(db, root, agent_id=f"h{n}", profile="big", tokens=row_tokens("big-model", input=50_000))
    assert budget.estimate(db, root, "big", "medium") == pytest.approx(0.5)
    assert choose(db, repo) == "big"


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


def test_savings_fall_back_to_relative_cost_when_a_task_is_unpriced(db, repo, root, routed):
    from brindle import savings

    budget_config(repo)
    for n in range(5):
        db.add_routing_decision(root, task_id=f"t{n}", agent_id=f"s{n}", weight="medium",
                                baseline_profile="big", profile="mystery", learned=True)
        db.note_routing_outcome(f"s{n}", outcome="merged")
        seed(db, root, agent_id=f"s{n}", profile="mystery", tokens=row_tokens("never-heard-of-it", input=10))
    assert not savings.report(db, root)[-1].dollars


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
    assert [ln.text.rstrip()[-3:] for ln in lines[1:3]] == ["75%", "25%"]
    assert "$6.00 of $8.00" in lines[3].text and lines[3].style == "ok"   # 75% < WARN_AT
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
