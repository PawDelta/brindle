import pytest

from brindle import autopilot
from brindle.config import RepoConfig


def test_max_weight_ceiling_caps_heavy_to_medium(db, repo, monkeypatch):
    cfg = RepoConfig(max_weight="medium")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(db, "caller-1", str(repo), weight="heavy", why=why)
    assert any("capped to medium" in w for w in why)
    # The resolved profile must be in medium routing, not developer-heavy
    assert profile in cfg.routing["medium"]


def test_docs_only_files_demotes_tier(db, repo, monkeypatch):
    cfg = RepoConfig()
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(
        db, "caller-2", str(repo), weight="heavy", files=["docs/guide.md", "docs/index.md"], why=why
    )
    assert any("demoted" in w for w in why)
    assert profile in cfg.routing["medium"]


def test_routing_explanation_includes_model_and_cost_signal(db, repo, monkeypatch):
    cfg = RepoConfig()
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(db, "caller-3", str(repo), weight="heavy", why=why)
    joined = " ".join(why)
    # Must show tier, profile, and cost indication
    assert "weight heavy ->" in joined
    assert "most expensive tier" in joined or "effort" in joined
