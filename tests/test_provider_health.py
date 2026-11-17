"""brindle.provider_health: one answer to "can this provider and model take
work now", on a fixed clock (NOW), never the wall clock.

provider_health is imported inside the tests, so the caller tests run (and
fail on their assertions) against a tree without the module."""

import time

import pytest

from brindle import agents, autopilot, policy, providers, quota, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR


@pytest.fixture
def clock(monkeypatch):
    """The fake clock quota, provider_health and the hooks all read."""
    monkeypatch.setattr(time, "time", lambda: NOW)


@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: None)
    monkeypatch.setattr(policy, "check_assign", lambda *a, **k: policy.Decision(allowed=True))


def _profile(repo, name, text):
    (repo / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".brindle" / "agents" / f"{name}.md").write_text(text)


def test_review_choice_passes_over_a_limited_providers_reviewer(db, repo, clock, available):
    _profile(repo, "reviewer-sonnet-high", "---\nextends: reviewer\nprovider: codex\n---\n")
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("writer", ws.id, "developer-sonnet", "claude", None,
                       "assign", "done", "@0", "done", NOW))
    quota.record_limit("codex", now=NOW)

    chosen, note = agents._review_choice(db, ws, None, RepoConfig(review_sizing=False))

    assert chosen == "reviewer"
    assert note is not None and "reviewer-sonnet-high" in note


def test_routing_checks_the_cap_on_the_effective_model(repo, brindle_home, clock, available, monkeypatch):
    # No model field: the model comes from the profile's GEMINI_MODEL.
    for name, model in (("gemini-pro", "gemini-3-pro"), ("gemini-flash", "gemini-3-flash")):
        _profile(repo, name, f"---\nextends: developer-gemini\nenv.GEMINI_MODEL: {model}\n---\n")
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    quota.record("antigravity", [quota.Window(100, NOW + HOUR, 300, "Gemini Pro 5h")])

    assert "limit reached" in autopilot._unavailable("gemini-pro", RepoConfig(), str(repo))
    assert autopilot._unavailable("gemini-flash", RepoConfig(), str(repo)) is None


@pytest.fixture
def health(brindle_home, clock):
    from brindle import provider_health

    return provider_health


def _hook(event, payload):
    return lambda db: agents.handle_hook(db, "w", event, payload)


def _agy_done_with_mail(db):
    # agy's own turn end with a message queued: it types the message in as a
    # new turn and never runs the Stop hook.
    from brindle import antigravity

    db.enqueue("w", "next", "brindle")
    antigravity.end_turn(db, db.get_agent("w"), {}, "")


FAIL = _hook("stop-failure", {"error": "api_error 500"})


@pytest.mark.parametrize("steps,expected", [
    pytest.param([_hook("stop-failure", {"error": "rate_limit_error: 429"})], "rate_limited", id="429"),
    pytest.param([FAIL] * 3, "erroring", id="three failed turns"),
    pytest.param([FAIL, FAIL, _hook("stop", {}), FAIL], "ok", id="a finished turn breaks the run"),
    pytest.param([FAIL, FAIL, _agy_done_with_mail, FAIL], "ok",
                 id="a finished agy turn with queued mail breaks the run"),
    pytest.param([FAIL, FAIL, _hook("codex-notify", {"type": "agent-turn-complete"}), FAIL], "ok",
                 id="a finished Codex turn breaks the run"),
    pytest.param([_hook("notification", {"message": "Approaching usage limit"})], "closing",
                 id="the CLI's approaching-limit notice"),
])
def test_hooks_record_provider_health(db, repo, health, monkeypatch, steps, expected):
    from brindle import antigravity

    monkeypatch.setattr(antigravity, "_flush_soon", lambda agent_id: None)
    quota.record("antigravity", [quota.Window(50, NOW + DAY, 10080)])
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("w", ws.id, "developer-gemini", "antigravity", None,
                       "interactive", "processing", "@1", None, NOW))
    for step in steps:
        step(db)
    assert health.limit("antigravity").state == expected


def _windows(*windows):
    return lambda ph: quota.record("antigravity", list(windows))


def _errors(*times, success_after=None):
    def setup(ph):
        for t in times:
            ph.record_error("antigravity", now=t)
            if t == success_after:
                ph.record_success("antigravity", now=t)
    return setup


def _readings(ph):
    quota.record("antigravity", [quota.Window(92, NOW + HOUR, 300)])
    ph.record_reading("antigravity", 80, NOW + HOUR, now=NOW - 300)
    ph.record_reading("antigravity", 92, NOW + HOUR, now=NOW - 60)


@pytest.mark.parametrize("setup,model,expected", [
    pytest.param(_windows(quota.Window(100, NOW + HOUR, 300, "Gemini Pro 5h")), "gemini-3-pro",
                 "limited", id="one-model cap limits its model"),
    pytest.param(_windows(quota.Window(100, NOW + HOUR, 300, "Gemini Pro 5h")), "gemini-3-flash",
                 "ok", id="one-model cap leaves other models"),
    pytest.param(_windows(quota.Window(97, NOW + HOUR, 300)), None,
                 "closing", id="97 percent with the reset far off"),
    pytest.param(_windows(quota.Window(98, NOW + 600, 300)), None,
                 "ok", id="reset within 20 minutes is waited for"),
    pytest.param(_windows(quota.Window(99, NOW + 600, 300), quota.Window(98, NOW + DAY, 10080)), None,
                 "closing", id="a near reset does not hide another closing window"),
    pytest.param(_windows(quota.Window(97, None, 300)), None,
                 "ok", id="an unknown reset is not closing"),
    pytest.param(_readings, None, "closing", id="readings project 100 percent"),
    pytest.param(lambda ph: ph.record_rate_limited("antigravity", now=NOW - 60), None,
                 "rate_limited", id="429"),
    pytest.param(_errors(NOW - 500, NOW - 300, NOW - 100), None,
                 "erroring", id="three errors in ten minutes"),
    pytest.param(_errors(NOW - 500, NOW - 300, NOW - 100, success_after=NOW - 300), None,
                 "ok", id="a success breaks the error run"),
])
def test_limit_states(health, setup, model, expected):
    setup(health)
    assert health.limit("antigravity", model).state == expected
