"""`brindle cost report`'s savings section: cache-hit rate, what cheaper routing saved
against the top profile, and the spend budget demotions avoided."""
import json

import pytest
from typer.testing import CliRunner

from brindle import git
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
