"""Native profiles that name a Models.dev model (``model: <provider>/<model>``)."""

import json
import os

import pytest

from brindle import airgap
from brindle.native import catalog
from brindle.profiles import ProfileError, load_profile

CATALOG = {
    "nano-gpt": {
        "id": "nano-gpt", "env": ["NANO_GPT_API_KEY"], "npm": "@ai-sdk/openai-compatible",
        "api": "https://nano-gpt.com/api/v1",
        "models": {"deepseek/deepseek-v3": {"limit": {"context": 128000, "output": 8000}}},
    },
    "anthropic": {
        "id": "anthropic", "env": ["ANTHROPIC_API_KEY"], "npm": "@ai-sdk/anthropic",
        "models": {"claude-x": {"limit": {"context": 200000, "output": 150000}}},
    },
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(airgap.ENV, raising=False)
    airgap.reset()
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / catalog.CACHE_NAME).write_text(json.dumps(CATALOG))
    (tmp_path / ".brindle" / "agents").mkdir(parents=True)
    yield tmp_path
    airgap.reset()


def write_profile(repo, model, extra=""):
    (repo / ".brindle" / "agents" / "p.md").write_text(
        f"---\nname: p\nprovider: native\nmodel: {model}\n{extra}---\nwork\n")
    return load_profile("p", str(repo))


def test_the_entry_fills_in_the_endpoint(repo):
    p = write_profile(repo, "nano-gpt/deepseek/deepseek-v3")
    assert (p.model, p.base_url, p.api, p.api_key_env) == (
        "deepseek/deepseek-v3", "https://nano-gpt.com/api/v1", "openai", "NANO_GPT_API_KEY")
    assert p.context_tokens == 120000


def test_anthropic_style_and_a_provider_without_an_api_url(repo):
    p = write_profile(repo, "anthropic/claude-x")
    assert (p.base_url, p.api, p.context_tokens) == ("https://api.anthropic.com", "anthropic", 100000)


def test_fields_the_profile_sets_win(repo):
    p = write_profile(repo, "nano-gpt/deepseek/deepseek-v3",
                      "api: anthropic\napi_key_env: MY_KEY\ncontext_tokens: 32000\n")
    assert (p.api, p.api_key_env, p.context_tokens) == ("anthropic", "MY_KEY", 32000)


def test_an_explicit_base_url_keeps_the_slash_in_the_model(repo):
    p = write_profile(repo, "nano-gpt/deepseek/deepseek-v3", "base_url: https://openrouter.ai/api/v1\n")
    assert (p.model, p.base_url) == ("nano-gpt/deepseek/deepseek-v3", "https://openrouter.ai/api/v1")


def test_an_unlisted_or_malformed_model_is_an_error(repo):
    with pytest.raises(ProfileError, match="no model 'nope'"):
        write_profile(repo, "anthropic/nope")
    bad = {"p": {"api": "http://h", "env": "KEY", "models": {"m": {"limit": 5}}}}
    with pytest.raises(catalog.CatalogError, match="malformed"):
        catalog.resolve("p/m", bad)


def test_air_gap_never_fetches_and_uses_the_stale_cache(repo, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("fetched in air-gap mode")

    monkeypatch.setattr(catalog, "_download", no_network)
    monkeypatch.setenv(airgap.ENV, "1")
    cache = catalog.cache_path()
    os.utime(cache, (0, 0))                      # long past the TTL
    assert write_profile(repo, "anthropic/claude-x").api == "anthropic"
    assert catalog.refresh() is False
    cache.unlink()
    with pytest.raises(ProfileError, match="air-gap"):
        write_profile(repo, "anthropic/claude-x")


def test_loading_a_profile_never_downloads(repo, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("downloaded while loading a profile")

    monkeypatch.setattr(catalog, "_download", no_network)
    kicked = []
    monkeypatch.setattr(catalog, "refresh_async", lambda: kicked.append(1))
    os.utime(catalog.cache_path(), (0, 0))
    assert write_profile(repo, "anthropic/claude-x").api == "anthropic"
    catalog.cache_path().unlink()
    with pytest.raises(ProfileError, match="isn't cached yet"):
        write_profile(repo, "anthropic/claude-x")
    assert len(kicked) == 2


def test_refresh_rewrites_a_stale_cache(repo, monkeypatch):
    os.utime(catalog.cache_path(), (0, 0))
    monkeypatch.setattr(catalog, "_download", lambda: {"x": {"api": "http://h/v1", "models": {"m": {}}}})
    assert catalog.refresh() is True
    assert catalog.resolve("x/m").base_url == "http://h/v1"
