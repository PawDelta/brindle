"""brindle.provider_health: one answer to "can this provider and model take
work now", on a fixed clock (NOW), never the wall clock.

provider_health is imported inside the tests, so the reviewer test runs (and
fails on its assertion) against a tree without the module."""

import time

import pytest

from brindle import agents, policy, providers, quota, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent

NOW = 1_800_000_000.0
HOUR = 3600.0


def test_review_choice_passes_over_a_limited_providers_reviewer(db, repo, monkeypatch):
    (repo / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".brindle" / "agents" / "reviewer-sonnet-high.md").write_text(
        "---\nextends: reviewer\nprovider: codex\n---\n")
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("writer", ws.id, "developer-sonnet", "claude", None,
                       "assign", "done", "@0", "done", NOW))
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: None)
    monkeypatch.setattr(policy, "check_assign", lambda *a, **k: policy.Decision(allowed=True))
    monkeypatch.setattr(time, "time", lambda: NOW)   # the fake clock quota and provider_health read
    quota.record_limit("codex", now=NOW)

    chosen, note = agents._review_choice(db, ws, None, RepoConfig(review_sizing=False))

    assert chosen == "reviewer"
    assert note is not None and "reviewer-sonnet-high" in note


@pytest.fixture
def health(brindle_home, monkeypatch):
    from brindle import provider_health

    monkeypatch.setattr(provider_health, "_clock", lambda: NOW)
    return provider_health


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
