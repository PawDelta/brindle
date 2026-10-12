from brindle.events import Event
from brindle.pro import team_events
from brindle.pro.orgkey import OrgKey

KEY = OrgKey("org_1", "k1", b"k" * 32)


def payload(by_model, **kw):
    ev = Event(kind="remove", repo_root="/r", agent_id="a1", cost_usd=1.5, by_model=by_model, **kw)
    return team_events.event_payload(KEY, "id", ev)


def test_absent_by_model_is_left_out():
    assert "by_model" not in payload(None)
    assert "by_model" not in payload({})


def test_at_most_32_entries_keeping_the_biggest():
    many = {f"model-{i:03d}": {"tokens": i + 1, "usd": 0.0} for i in range(40)}
    body = payload(many)
    assert len(body["by_model"]) == 32
    assert "model-039" in body["by_model"] and "model-000" not in body["by_model"]


def test_unpriced_tokens_count_with_zero_usd_and_bad_entries_are_dropped():
    body = payload({"unknown model": {"tokens": 99, "usd": None}, "neg": {"tokens": -1, "usd": 1.0},
                    "bad": "x", "": {"tokens": 1, "usd": 0.0}})
    assert body["by_model"] == {"unknown model": {"tokens": 99, "usd": 0.0}}


# -- events._by_model and cost.model_label ---------------------------------------------------------


def _row(agent_id, model, tokens, profile="dev"):
    import json

    from brindle.db import HistoryEntry

    t = json.dumps({"model": model, "input": tokens, "output": 0})
    return HistoryEntry(1, "/r", 1.0, "done", agent_id, "b", profile, None, None, t)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def list_history(self, repo_root, agent_id, limit):
        return self.rows


def _setup(monkeypatch, rows, has=True):
    from brindle import db
    from brindle.pro import license

    monkeypatch.setattr(db, "DB", lambda *a, **kw: FakeDB(rows))
    monkeypatch.setattr(license, "has", lambda feature: has)


def _worker(agent_id="a1"):
    from types import SimpleNamespace

    return SimpleNamespace(id=agent_id)


def test_remove_event_carries_per_model_tokens_and_usd_for_that_agent_only(monkeypatch):
    from brindle import events

    _setup(monkeypatch, [_row("a1", "claude-fable-5-1", 1_000_000), _row("a1", "claude-fable-5-1", 1_000_000),
                         _row("a1", "mystery-model", 500), _row("other", "claude-fable-5-1", 9_000_000)])
    out = events._by_model("remove", _worker(), "/r", 20.0)
    assert out == {"claude-fable-5-1": {"tokens": 2_000_000, "usd": 20.0},
                   "mystery-model": {"tokens": 500, "usd": 0.0}}


def test_empty_history_with_cost_is_a_single_unknown_entry(monkeypatch):
    from brindle import events

    _setup(monkeypatch, [])
    assert events._by_model("remove", _worker(), "/r", 3.0) == {"unknown model": {"tokens": 0, "usd": 3.0}}
    assert events._by_model("remove", _worker(), "/r", None) is None
