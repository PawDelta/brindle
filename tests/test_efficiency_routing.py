"""Opt-in design-only Fable routing, including hosted learner suggestions."""

import pytest

from brindle import autopilot, learning
from test_routing import boss, clear_cache, config, everything_available, install


@pytest.mark.parametrize("enabled,task,expected", [
    (True, "Implement the storage engine", "developer-heavy"),
    pytest.param(True, "[design] Design the storage engine", "developer-fable",
                 marks=pytest.mark.variant("marked design work remains eligible for Fable")),
    pytest.param(False, "Implement the storage engine", "developer-fable",
                 marks=pytest.mark.variant("opting out preserves existing advanced routing")),
])
def test_advanced_design_routing(db, repo, boss, enabled, task, expected):
    config(repo, fable_for_design_only=enabled, learning="off")
    assert autopilot.choose_profile(db, "boss", str(repo), task=task,
                                    weight="advanced") == (expected, False)


@pytest.mark.parametrize("weight,default", [
    ("advanced", "developer-heavy"),
    (None, "developer-heavy"),
    (None, "developer-fable"),
])
def test_learner_cannot_restore_fable(db, repo, boss, monkeypatch, weight, default):
    offered = []

    class FablePicker(learning.LearningPlugin):
        def suggest(self, task, candidates, default=None):
            offered.append(candidates)
            return "developer-fable"

    install(monkeypatch, FablePicker())
    config(repo, fable_for_design_only=True, learning="cloud",
           default_agent=default,
           learning_candidates=["developer-fable", "developer-heavy", "developer-gemini"])
    assert autopilot.choose_profile(db, "boss", str(repo), task="Implement storage",
                                    weight=weight) == ("developer-heavy", False)
    assert offered and all("developer-fable" not in names for names in offered)
