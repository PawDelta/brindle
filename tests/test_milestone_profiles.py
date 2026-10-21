import pytest

from brindle import autopilot
from brindle.config import RepoConfig
from test_autopilot import add_agent, root  # noqa: F401  (the `root` fixture)


def set_goal(db, items):
    autopilot.set_goal(db, "boss", "Goal", items)


def test_resolution_prefers_current_milestone_then_default(db, root):
    agent, ws = root
    set_goal(db, [("A", "true", None, "reviewer"), ("B", "true", None, "supervisor"), ("C", "true", None)])
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == "reviewer"
    assert autopilot.resolve_profile(db, "boss", ws.repo_root, "developer") == "developer"
    first = db.milestones("boss")[0]
    db.record_check(first.id, True, "", "sha")
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == "supervisor"
    for m in db.milestones("boss")[1:2]:
        db.record_check(m.id, True, "", "sha")
    # the current milestone has no profile: fall back to the repo default
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == RepoConfig().default_agent


def test_weight_overrides_milestone_profile(db, root):
    agent, ws = root
    set_goal(db, [("A", "true", None, "developer-heavy")])
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == "developer-heavy"
    name, _ = autopilot.choose_profile(db, "boss", ws.repo_root, weight="medium")
    assert name == "developer-sonnet"


def test_unknown_profile_errors_clearly(db, root):
    agent, ws = root
    set_goal(db, [("A", "true", None, "no-such-profile")])
    with pytest.raises(autopilot.AutopilotError, match="no agent profile named 'no-such-profile'"):
        autopilot.resolve_profile(db, "boss", ws.repo_root)
    with pytest.raises(autopilot.AutopilotError, match="'nope'"):
        autopilot.resolve_profile(db, "boss", ws.repo_root, "nope")
