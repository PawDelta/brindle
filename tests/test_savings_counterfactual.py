"""`brindle cost report`'s savings section: cache-hit rate, what cheaper routing saved
against the top profile, and the spend budget demotions avoided."""
import json

import pytest
from typer.testing import CliRunner

from brindle import cost, git
from brindle.cli import app
from brindle.pro import license

PRICES = {"big-model": {"input": 10, "output": 50, "cache_write": 12, "cache_read": 1},
          "small-model": {"input": 1, "output": 5, "cache_write": 1.2, "cache_read": 0.1}}


def profile(repo, name, model):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: t\nprovider: claude\nmodel: {model}\n---\nGo.\n")


@pytest.fixture
def root(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    profile(repo, "big", "big-model")
    profile(repo, "small", "small-model")
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"pricing": PRICES, "routing": {"medium": ["big", "small"]}}))
    return git.main_repo_root(str(repo))


def task(db, root, n, profile, *, model, input=0, output=0, cache_read=0, demoted_from=None):
    agent = f"w{n}"
    db.add_routing_decision(root, task_id=f"t{n}", agent_id=agent, weight="medium",
                            baseline_profile=profile, profile=profile, learned=False,
                            demoted_from=demoted_from)
    db.add_history(root, "worker_result", agent_id=agent, profile=profile,
                   tokens=json.dumps({"model": model, "input": input, "output": output,
                                      "cache_read": cache_read}))


def test_cache_hit_rate_is_cache_reads_over_all_input(db, root):
    task(db, root, 1, "small", model="small-model", input=100_000, cache_read=300_000)
    s = cost.savings(db, root)
    assert s.cache_hit_rate == pytest.approx(0.75)


def test_no_usage_means_no_rate(db, root):
    assert cost.savings(db, root).cache_hit_rate is None


def test_cheaper_routing_is_the_same_tokens_at_the_top_profile_less_actual(db, root):
    # 1M output tokens: $5 on small, $50 on big (the most expensive profile in the routing)
    task(db, root, 1, "small", model="small-model", output=1_000_000)
    s = cost.savings(db, root)
    assert s.top_profile == "big" and s.routed == 1
    assert s.routing_saved == pytest.approx(45)


def test_tasks_on_the_top_profile_save_nothing(db, root):
    task(db, root, 1, "big", model="big-model", output=1_000_000)
    s = cost.savings(db, root)
    assert s.routed == 0 and s.routing_saved == 0


def test_demotions_count_what_the_skipped_profile_would_have_cost(db, root):
    task(db, root, 1, "small", model="small-model", output=1_000_000, demoted_from="big")
    task(db, root, 2, "small", model="small-model", output=1_000_000)
    s = cost.savings(db, root)
    assert s.demotions == 1 and s.demotion_saved == pytest.approx(45)
    assert s.routed == 2 and s.routing_saved == pytest.approx(90)


def test_a_row_of_an_unknown_model_is_priced_at_its_profiles_price(db, root):
    task(db, root, 1, "small", model="mystery-model", output=1_000_000, demoted_from="big")
    s = cost.savings(db, root)
    assert s.demotions == 1 and s.demotion_saved == pytest.approx(45)


def test_only_the_window_counts(db, root):
    import time

    task(db, root, 1, "small", model="small-model", output=1_000_000, demoted_from="big")
    db.conn.execute("UPDATE routing_decisions SET ts=?", (time.time() - 40 * 86400,))
    db.conn.commit()
    assert cost.savings(db, root).demotions == 0
    assert cost.savings(db, root, days=60).demotions == 1


def test_the_report_shows_the_savings(db, root):
    task(db, root, 1, "small", model="small-model", input=100_000, output=1_000_000,
         cache_read=300_000, demoted_from="big")
    res = CliRunner().invoke(app, ["cost", "report"])
    assert res.exit_code == 0, res.output
    assert "Savings, last 30 days" in res.output
    assert "cache-hit rate: 75%" in res.output
    assert "cheaper routing:" in res.output and "against running them all on big" in res.output
    assert "budget demotions:" in res.output and "1 task(s) moved to a cheaper profile" in res.output


def test_the_report_says_when_there_is_nothing_to_compare(db, root):
    res = CliRunner().invoke(app, ["cost", "report"])
    assert res.exit_code == 0, res.output
    assert "no token usage on record" in res.output
    assert "cheaper routing: nothing to compare yet" in res.output
    assert "budget demotions: none" in res.output
