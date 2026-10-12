"""Route health: what a launch or a run found out about an agent's model route
is counted per route and model and sent as one ``route_health`` event per
window, by signed-in Team and Enterprise members only (the Team feed carries it; see
test_pro_team for the wire form and that no provider text ever rides along)."""

import pytest

from brindle import events
from brindle.pro import license, route_health
from brindle.pro.license import Entitlement


@pytest.fixture(autouse=True)
def window():
    route_health.reset()
    yield
    route_health.reset()


class Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def entitlement(plan):
    return Entitlement("user_1", "org_1", plan, "active", frozenset(), 1, 0, 10_000_000_000, "k")


@pytest.mark.parametrize("plan,sent", [("team", 2), ("pro", 0)])   # the backend takes events from team orgs only
def test_repeats_are_one_event_with_a_count_for_a_team_org(plan, sent, monkeypatch):
    monkeypatch.setattr(license, "current", lambda **kw: entitlement(plan))
    recorder = Recorder()
    monkeypatch.setattr(events, "plugins_for", lambda cfg, root: [recorder])
    for _ in range(50):
        route_health.record("/r", agent="claude", route="bedrock", model="us.anthropic.claude-opus-5-5",
                            outcome="provider_403")
    route_health.record("/r", agent="codex", route="chatgpt", model=None, outcome="model_unavailable")
    route_health.record("/r", agent="shell", route="default", model="x", outcome="provider_401")  # no such agent on the feed
    assert route_health.flush() == sent
    assert [(e.kind, e.provider, e.route, e.model, e.outcome, e.count) for e in recorder.events] == ([
        ("route_health", "claude", "bedrock", "us.anthropic.claude-opus-5-5", "provider_403", 50),
        ("route_health", "codex", "chatgpt", "default", "model_unavailable", 1)] if sent else [])
    assert route_health.pending() == {}
