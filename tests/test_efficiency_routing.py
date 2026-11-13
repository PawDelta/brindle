"""Opt-in design-heavy Fable eligibility through real routing."""

import asyncio

import pytest

from brindle import agents, autopilot, learning, mcp_server
from test_merge_order import fake_spawn
from test_routing import boss, clear_cache, config, everything_available, install


@pytest.mark.parametrize("enabled,design_heavy,expected", [
    (True, False, "developer-heavy"),
    pytest.param(True, True, "developer-fable",
                 marks=pytest.mark.variant("design-heavy work remains eligible for Fable")),
    pytest.param(False, False, "developer-fable",
                 marks=pytest.mark.variant("opting out preserves existing advanced routing")),
])
def test_advanced_design_routing(db, repo, boss, enabled, design_heavy, expected):
    config(repo, fable_for_design_only=enabled, learning="off")
    options = {"design_heavy": True} if design_heavy else {}
    assert autopilot.choose_profile(db, "boss", str(repo), task="Build the shared system",
                                    weight="advanced", **options) == (expected, False)


@pytest.mark.parametrize("weight,default,design_heavy", [
    ("advanced", "developer-heavy", False),
    (None, "developer-heavy", False),
    (None, "developer-fable", False),
    pytest.param("advanced", "developer-heavy", True,
                 marks=pytest.mark.variant("weighted routing must pass design eligibility to the learner")),
    pytest.param(None, "developer-heavy", True,
                 marks=pytest.mark.variant("unweighted learning must preserve design eligibility too")),
])
def test_learner_respects_design_eligibility(db, repo, boss, monkeypatch, weight, default, design_heavy):
    offered = []

    class FablePicker(learning.LearningPlugin):
        def record(self, task, outcome):
            pass

        def suggest(self, task, candidates, default=None):
            offered.append(candidates)
            return "developer-fable"

    install(monkeypatch, FablePicker())
    config(repo, fable_for_design_only=True, learning="cloud", default_agent=default,
           routing={"advanced": ["developer-heavy", "developer-fable", "developer-gemini"]},
           learning_candidates=["developer-fable", "developer-heavy", "developer-gemini"])
    expected = ("developer-fable", True) if design_heavy else ("developer-heavy", False)
    options = {"design_heavy": True} if design_heavy else {}
    assert autopilot.choose_profile(db, "boss", str(repo), task="Implement storage",
                                    weight=weight, **options) == expected
    assert offered and all(("developer-fable" in names) == design_heavy for names in offered)


def test_assign_marks_design_heavy_work(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    config(repo, fable_for_design_only=True, learning="off", pipeline=False)
    asyncio.run(mcp_server.assign(task="Design the shared interfaces", branch="shared-design",
                                 weight="advanced", design_heavy=True))
    [task] = db.list_tasks(str(repo), state="started")
    assert task.profile == "developer-fable"
