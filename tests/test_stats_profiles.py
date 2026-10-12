"""`brindle cost report`'s per-profile section: finished tasks, merge rate, review rounds, and
tokens and dollars per merged branch, from seeded routing decisions and history rows."""
import json

import pytest

from brindle import cost

ROOT = "/repo"
SONNET = "claude-sonnet-5-5"   # $10 per million output tokens: 100k output = $1


def tokens(output):
    return json.dumps({"input": 0, "output": output, "cache_read": 0, "cache_creation": 0, "model": SONNET})


def task(db, agent, profile, weight, outcome, rounds=0, output=None, ts=None):
    db.add_routing_decision(ROOT, task_id=f"t-{agent}", agent_id=agent, weight=weight,
                            baseline_profile=profile, profile=profile, learned=False, ts=ts)
    db.note_routing_outcome(agent, outcome=outcome)
    for _ in range(rounds):
        db.note_routing_outcome(agent, review=True)
    if output:
        db.add_history(ROOT, "worker_result", agent_id=agent, branch=f"feat/{agent}", profile=profile,
                       tokens=tokens(output))


def test_empty_db(db):
    assert cost.profile_stats(db, ROOT, 0) == {}
    rep = cost.report(db, ROOT)
    assert rep.stats == {}
    text = cost.describe_report(rep, ROOT)
    assert "Worker profiles by weight" in text and "no finished tasks in this period" in text


def test_merge_rate_rounds_and_cost_per_merged_branch(db):
    task(db, "a1", "developer", "medium", "merged", rounds=1, output=100_000)       # $1
    task(db, "a2", "developer", "medium", "merged", rounds=3, output=300_000)       # $3
    task(db, "a3", "developer", "medium", "removed_unmerged", rounds=2, output=900_000)   # not counted
    task(db, "b1", "developer-codex", "heavy", "removed_unmerged")
    db.add_routing_decision(ROOT, task_id="t-live", agent_id="a4", weight="medium",
                            baseline_profile="developer", profile="developer", learned=False)   # unfinished
    stats = cost.report(db, ROOT).stats
    assert set(stats) == {("developer", "medium"), ("developer-codex", "heavy")}
    s = stats[("developer", "medium")]
    assert (s.finished, s.merged, s.unmerged) == (3, 2, 1)
    assert s.merge_rate == pytest.approx(2 / 3)
    assert s.avg_rounds == pytest.approx(2)
    assert s.tokens_per_merged == 200_000
    assert s.dollars_per_merged == pytest.approx(2)
    b = stats[("developer-codex", "heavy")]
    assert b.merge_rate == 0 and b.avg_rounds == 0
    assert b.tokens_per_merged is None and b.dollars_per_merged is None
    text = cost.describe_report(cost.report(db, ROOT), ROOT)
    assert "developer / medium" in text
    assert "3 finished, 67% merged, 2.0 review rounds avg, 200k tokens, $2.00 per merged branch" in text
    assert "developer-codex / heavy" in text and "0% merged" in text


def test_merged_branches_on_a_free_model_read_free():
    s = cost.ProfileStats(finished=1, merged=1, merged_counted=1)
    s.merged_spend.add(0.0, 5000, free=True)
    assert s.dollars_per_merged == 0.0
    text = "\n".join(cost.describe_stats({("developer-local", "light"): s}))
    assert "5k tokens, free per merged branch" in text
    unpriced = cost.ProfileStats(finished=1, merged=1, merged_counted=1)
    unpriced.merged_spend.add(None, 5000)
    assert unpriced.dollars_per_merged is None
