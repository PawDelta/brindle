import time
import pytest

from brindle import cull, events
from brindle.config import RepoConfig
from brindle.db import Agent, Workspace
from brindle.events import Event


class MockPlugin(events.EventsPlugin):
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


def _ws(repo_root: str, ws_id: str = "ws1") -> Workspace:
    return Workspace(
        id=ws_id,
        repo_root=repo_root,
        name="main",
        kind="main",
        branch="main",
        base_branch="main",
        path=repo_root,
        port_base=9000,
        tmux_session="session",
        created_at=time.time(),
    )


def test_emit_periodic_cost_tracks_delta_and_updates_reported_spend(db, monkeypatch, tmp_path):
    repo_root = str(tmp_path / "repo")
    cfg = RepoConfig()
    ws = _ws(repo_root)
    db.add_workspace(ws)
    agent = Agent("a1", ws.id, "developer", "claude", None, "assign", "processing", "@1", None, time.time())
    db.add_agent(agent)

    plugin = MockPlugin()
    monkeypatch.setattr(events, "plugins_for", lambda cfg, root: [plugin])

    from brindle.pro import license
    monkeypatch.setattr(license, "has", lambda feat: feat == "org_budgets")

    from brindle import budget
    spend_val = 0.50
    monkeypatch.setattr(budget, "worker_spend", lambda db, a, root: spend_val)

    # First pass: delta is $0.50 >= 0.01
    done = events.emit_periodic_cost(cfg, repo_root)
    assert len(done) == 1
    assert "emitted periodic cost $0.50 for agent a1" in done[0]
    assert len(plugin.events) == 1
    assert plugin.events[0].kind == "cost"
    assert plugin.events[0].cost_usd == 0.50
    assert db.get_reported_spend("a1") == pytest.approx(0.50)

    # Second pass with no new spend: delta is 0
    done2 = events.emit_periodic_cost(cfg, repo_root)
    assert done2 == []
    assert len(plugin.events) == 1

    # Third pass with new spend ($0.85 total, delta $0.35)
    spend_val = 0.85
    done3 = events.emit_periodic_cost(cfg, repo_root)
    assert len(done3) == 1
    assert "emitted periodic cost $0.35 for agent a1" in done3[0]
    assert len(plugin.events) == 2
    assert plugin.events[1].kind == "cost"
    assert plugin.events[1].cost_usd == pytest.approx(0.35)
    assert db.get_reported_spend("a1") == pytest.approx(0.85)

    # Fourth pass: worker remove event only reports the unrecorded delta
    spend_val = 1.00
    events.emit(cfg, "remove", ws, worker=agent)
    assert len(plugin.events) == 3
    assert plugin.events[2].kind == "remove"
    assert plugin.events[2].cost_usd == pytest.approx(0.15)
    assert db.get_reported_spend("a1") == pytest.approx(1.00)


def test_cull_sweep_calls_emit_periodic_cost(db, monkeypatch, tmp_path):
    repo_root = str(tmp_path / "repo")
    ws = _ws(repo_root)
    db.add_workspace(ws)

    called_roots = []
    monkeypatch.setattr(events, "emit_periodic_cost", lambda cfg, root: called_roots.append(root) or ["cost-done"])

    # Mock other sweep steps so tmux/procs don't interfere
    from brindle import tmux, procs
    snap = tmux.PaneSnapshot()
    snap.tags = {}
    monkeypatch.setattr(tmux, "list_panes", lambda: snap)
    monkeypatch.setattr(procs, "table", lambda: {})

    res = cull.sweep(db)
    assert repo_root in called_roots
    assert "cost-done" in res
