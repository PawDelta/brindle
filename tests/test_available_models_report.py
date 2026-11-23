"""Model catalogs on the organization entitlement request, with no live probes."""

import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from brindle import doctor, model_availability as ma, providers, providers_install
from brindle.pro import auth, credentials
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures

ORG = "org_1"
REAL_REPORT = ma.reportable_models


@pytest.fixture
def report_client(backend, monkeypatch):
    monkeypatch.setattr(ma, "reportable_models", REAL_REPORT)
    queries = []

    class Transport:
        def request(self, method, url, form, headers):
            parsed = urlsplit(url)
            assert (method, parsed.path) == ("GET", "/api/brindle/v1/entitlement")
            queries.append(parse_qs(parsed.query))
            return backend._entitlement(form, headers)

    return auth.Client(transport=Transport()), queries


def test_org_entitlement_reports_only_listed_model_ids(report_client, backend, monkeypatch):
    client, queries = report_client
    monkeypatch.setattr(doctor, "signed_in_providers",
                        lambda: ["claude", "codex", "antigravity", "opencode", "unknown"])
    monkeypatch.setattr(providers, "which_cli", lambda name: "/bin/ollama" if name == "ollama" else None)
    codex_ids = {f"gpt-test-{i:02}" for i in range(65)}
    catalogs = {
        "codex": ma.Catalog(frozenset(codex_ids | {"/private/model", "C:/private/model", "a prompt"}), "codex"),
        "antigravity": ma.Catalog(frozenset({"gemini-test"}), "antigravity"),
        "native": ma.Catalog(frozenset({"qwen3-coder:30b"}), "ollama"),
    }
    monkeypatch.setattr(ma, "_cli_models", lambda binary: "opencode/beam-free\n/private/local\nconsole message\n")

    def discover(request, budget):
        if request.provider == "opencode":
            return ma._opencode_catalog()
        return catalogs[request.provider]

    monkeypatch.setattr(ma, "_isolated_discovery", discover)

    auth._fetch_entitlement(client, backend.issue()["access_token"], time.time(), ORG)

    [query] = queries
    assert query["providers"] == ["claude,codex,antigravity,opencode,unknown"]
    assert json.loads(query["available_models"][0]) == {
        "codex": sorted(codex_ids)[:64],
        "antigravity": ["gemini-test"],
        "opencode": ["opencode/beam-free"],
        "ollama": ["qwen3-coder:30b"],
    }


def test_personal_entitlement_omits_model_report(report_client, backend, monkeypatch):
    client, queries = report_client
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])

    def unexpected_probe(*args):
        pytest.fail("personal entitlement probed the model catalog")

    monkeypatch.setattr(ma, "_isolated_discovery", unexpected_probe)
    auth._fetch_entitlement(client, backend.issue()["access_token"], time.time())

    assert queries == [{"providers": ["codex"]}]


@pytest.mark.parametrize("available", [True, False])
def test_empty_catalog_clears_but_unreadable_catalog_preserves_report(
    report_client, backend, monkeypatch, available,
):
    client, queries = report_client
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    monkeypatch.setattr(providers, "which_cli", lambda name: None)

    def discover(request, budget):
        if not available:
            raise OSError("discovery failed")
        return ma.Catalog(frozenset(), "codex")

    monkeypatch.setattr(ma, "_isolated_discovery", discover)
    auth._fetch_entitlement(client, backend.issue()["access_token"], time.time(), ORG)

    [query] = queries
    if available:
        assert json.loads(query["available_models"][0]) == {}
    else:
        assert "available_models" not in query


def test_successful_provider_update_refreshes_model_report(report_client, backend, monkeypatch):
    client, queries = report_client
    record = auth._token_set(backend.issue(), time.time())
    credentials.default_store().save({**record, "org_id": ORG, "base_url": client.base})
    monkeypatch.setattr(auth, "Client", lambda base: client)
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    monkeypatch.setattr(providers, "which_cli", lambda name: None)
    models = {"gpt-old"}

    def finish_install():
        models.clear()
        models.add("gpt-new")
        return 0

    monkeypatch.setattr(ma, "_isolated_discovery",
                        lambda request, budget: ma.Catalog(frozenset(models), "codex"))
    monkeypatch.setattr(providers_install.spawn, "start",
                        lambda *args, **kwargs: SimpleNamespace(stdout=[], wait=finish_install, kill=lambda: None))
    monkeypatch.setattr(providers_install, "detect", lambda provider: ["/bin/codex"])
    monkeypatch.setattr(providers_install, "status", lambda provider: {"state": "installed"})

    providers_install.install("codex", lambda line: None)

    [query] = queries
    assert json.loads(query["available_models"][0]) == {"codex": ["gpt-new"]}
