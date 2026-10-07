from brindle.events import Event
from brindle.pro import team_events
from brindle.pro.orgkey import OrgKey

KEY = OrgKey("org_1", "k1", b"k" * 32)


def payload(by_model, **kw):
    ev = Event(kind="remove", repo_root="/r", agent_id="a1", cost_usd=1.5, by_model=by_model, **kw)
    return team_events.event_payload(KEY, "id", ev)


def test_by_model_rides_next_to_cost_usd():
    body = payload({"claude-opus-4": {"tokens": 1200, "usd": 1.25}, "local": {"tokens": 50, "usd": 0}})
    assert body["cost_usd"] == 1.5
    assert body["by_model"] == {"claude-opus-4": {"tokens": 1200, "usd": 1.25},
                                "local": {"tokens": 50, "usd": 0.0}}
    assert "by_model" in team_events.PAYLOAD_KEYS


def test_absent_by_model_is_none():
    assert payload(None)["by_model"] is None
    assert payload({})["by_model"] is None


def test_names_are_cut_to_128_chars():
    body = payload({"m" * 300: {"tokens": 7, "usd": 0.5}})
    assert list(body["by_model"]) == ["m" * 128]


def test_at_most_32_entries_keeping_the_biggest():
    many = {f"model-{i:03d}": {"tokens": i + 1, "usd": 0.0} for i in range(40)}
    body = payload(many)
    assert len(body["by_model"]) == 32
    assert "model-039" in body["by_model"] and "model-000" not in body["by_model"]


def test_truncated_names_that_collide_are_summed():
    body = payload({"x" * 128 + "a": {"tokens": 1, "usd": 1.0}, "x" * 128 + "b": {"tokens": 2, "usd": 2.0}})
    assert body["by_model"] == {"x" * 128: {"tokens": 3, "usd": 3.0}}


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


def test_remainder_of_cost_usd_becomes_an_unknown_model_entry(monkeypatch):
    from brindle import events

    _setup(monkeypatch, [_row("a1", "claude-fable-5-1", 1_000_000)])
    out = events._by_model("remove", _worker(), "/r", 12.5)
    assert out["claude-fable-5-1"] == {"tokens": 1_000_000, "usd": 10.0}
    assert out["unknown model"] == {"tokens": 0, "usd": 2.5}


def test_empty_history_with_cost_is_a_single_unknown_entry(monkeypatch):
    from brindle import events

    _setup(monkeypatch, [])
    assert events._by_model("remove", _worker(), "/r", 3.0) == {"unknown model": {"tokens": 0, "usd": 3.0}}
    assert events._by_model("remove", _worker(), "/r", None) is None


def test_by_model_only_for_remove_events_under_org_budgets(monkeypatch):
    from brindle import events

    _setup(monkeypatch, [_row("a1", "claude-fable-5-1", 1000)], has=False)
    assert events._by_model("remove", _worker(), "/r", 1.0) is None
    _setup(monkeypatch, [_row("a1", "claude-fable-5-1", 1000)])
    assert events._by_model("review", _worker(), "/r", 1.0) is None
    assert events._by_model("remove", None, "/r", 1.0) is None


def test_model_label_falls_back_to_profile_then_unknown():
    from brindle import cost

    assert cost.model_label(cost.Priced(_row("a", "m1", 1), "m1", 1, None)) == "m1"
    assert cost.model_label(cost.Priced(_row("a", None, 1, profile="dev"), None, 1, None)) == "profile dev"
    assert cost.model_label(cost.Priced(_row("a", None, 1, profile=None), None, 1, None)) == "unknown model"


def test_tokens_and_usd_are_capped_at_the_backend_limits():
    cap, tok = team_events.MAX_EVENT_COST_USD, 10**12
    body = payload({"big": {"tokens": 10**15, "usd": 10**9},
                    "x" * 128 + "a": {"tokens": tok, "usd": cap}, "x" * 128 + "b": {"tokens": tok, "usd": cap}})
    assert body["by_model"]["big"] == {"tokens": tok, "usd": float(cap)}
    assert body["by_model"]["x" * 128] == {"tokens": tok, "usd": float(cap)}
