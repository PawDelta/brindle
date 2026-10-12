"""The local brief-quality learner (brindle.pro.brief_learning)."""

from __future__ import annotations

import json
import time

import pytest

from brindle.db import Task
from brindle.pro import brief_learning, license

ROOT = "/repo"
LONG = "x" * 400      # a brief long enough not to be "short"


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == brief_learning.FEATURE)


def finish(db, n, *, outcome="merged", rounds=1, done_when="tests pass", files=("a/**",),
           weight="medium", text=LONG, deps=None, root=ROOT):
    tid = f"t{db.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]}-{n}"
    db.add_task(Task(id=tid, repo_root=root, agent_id=None, caller_id=None, caller_ws_id="ws",
                     profile="developer", task_text=text, mode="x", isolate=1, branch=tid,
                     done_when=done_when, files=json.dumps(list(files)),
                     depends_on=json.dumps(deps) if deps else None, state="merged",
                     created_at=time.time(), weight=weight))
    db.add_routing_decision(root, task_id=tid, agent_id=f"a-{tid}", weight=weight,
                            baseline_profile="developer", profile="developer", learned=False)
    db.note_routing_outcome(f"a-{tid}", outcome=outcome)
    for _ in range(rounds):
        db.note_routing_outcome(f"a-{tid}", review=True)


def seed(db, *, with_done=17, without_done_good=3, without_done_bad=6):
    """with_done clean merges that have a done_when; without it, 3 of 9 merge cleanly."""
    for i in range(with_done):
        finish(db, i)
    for i in range(without_done_good):
        finish(db, i, done_when=None)
    for i in range(without_done_bad):
        finish(db, i, done_when=None, rounds=3 if i % 2 else 1, outcome="merged" if i % 2 else "removed_unmerged")


def test_traits_of_a_brief():
    assert brief_learning.traits(LONG, "ok", ["a"], "heavy") == ["weight:heavy"]
    assert brief_learning.traits("hi", None, [], None, '["x"]') == [
        "no_done_when", "no_files", "short", "no_weight", "has_deps"]
    assert "many_files" in brief_learning.traits(LONG, "ok", list("abcd"), "light")
    assert "long" in brief_learning.traits("y" * 3001, "ok", ["a"], "light")


def test_no_warning_below_twenty_finished_tasks(db, pro):
    seed(db, with_done=10, without_done_good=3, without_done_bad=6)       # 19 finished
    assert brief_learning.rates(db, ROOT).overall.total == 19
    assert brief_learning.warning(db, ROOT, LONG, None, ["a/**"], "medium") is None
    finish(db, 99)                                                        # the 20th
    assert brief_learning.warning(db, ROOT, LONG, None, ["a/**"], "medium") is not None


def test_a_trait_with_too_few_samples_is_ignored(db, pro):
    for i in range(20):
        finish(db, i)
    for i in range(3):
        finish(db, i, done_when=None, rounds=3)      # 0 of 3: bad, but only 3 samples
    assert brief_learning.warning(db, ROOT, LONG, None, ["a/**"], "medium") is None


def test_without_the_entitlement_there_is_no_warning(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert brief_learning.warning(db, ROOT, LONG, None, ["a/**"], "medium") is None

    def broken(feature):
        raise RuntimeError("unreadable license")

    monkeypatch.setattr(license, "has", broken)
    assert brief_learning.warning(db, ROOT, LONG, None, ["a/**"], "medium") is None
