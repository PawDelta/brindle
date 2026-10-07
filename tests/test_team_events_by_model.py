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
