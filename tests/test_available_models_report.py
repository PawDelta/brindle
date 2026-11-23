"""Model catalogs on the organization entitlement request, with no live probes."""

import json
import time
from contextlib import contextmanager
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


@pytest.mark.parametrize("available,ollama", [
    (True, False),
    (False, False),
    pytest.param(False, True, marks=pytest.mark.variant(
        "an empty Ollama catalog cannot clear a previous Codex report after a failed Codex probe")),
])
def test_empty_catalog_clears_but_unreadable_catalog_preserves_report(
    report_client, backend, monkeypatch, available, ollama,
):
    client, queries = report_client
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    monkeypatch.setattr(providers, "which_cli", lambda name: "/bin/ollama" if ollama and name == "ollama" else None)

    def discover(request, budget):
        if request.provider == "native":
            return ma.Catalog(frozenset(), "ollama", authoritative=True)
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


@pytest.mark.parametrize("issuer_changed", [
    False,
    pytest.param(True, marks=pytest.mark.variant(
        "an account switch before the lock must not send the new credentials to the previous issuer")),
])
def test_provider_update_refreshes_the_signin_loaded_under_the_lock(
    report_client, backend, monkeypatch, issuer_changed,
):
    client, queries = report_client
    store = credentials.default_store()
    now = time.time()
    old = auth._token_set(backend.issue(), now)
    old_issuer = "https://previous.example.test/api/brindle/v1" if issuer_changed else client.base
    store.save({**old, "org_id": "old_org" if issuer_changed else ORG, "base_url": old_issuer})
    issued = backend.issue()
    _, body = backend._entitlement({}, {"Authorization": "Bearer " + issued["access_token"]})
    new = {**auth._token_set(issued, now), "org_id": ORG, "base_url": client.base,
           "entitlement": body["entitlement"]}
    lock = auth.refresh_lock

    @contextmanager
    def after_other_refresh():
        store.save(new)
        with lock():
            yield

    def previous_issuer_request(*args, **kwargs):
        pytest.fail("provider update sent an entitlement request to the previous issuer")

    previous_client = SimpleNamespace(base=old_issuer, get=previous_issuer_request)
    monkeypatch.setattr(auth, "refresh_lock", after_other_refresh)
    monkeypatch.setattr(auth, "Client", lambda base: client if base == client.base else previous_client)
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    monkeypatch.setattr(providers, "which_cli", lambda name: None)
    monkeypatch.setattr(ma, "_isolated_discovery",
                        lambda request, budget: ma.Catalog(frozenset({"gpt-new"}), "codex"))

    auth.refresh_after_provider_update()

    [query] = queries
    assert query["org_id"] == [ORG]
    assert json.loads(query["available_models"][0]) == {"codex": ["gpt-new"]}
    assert store.load()["refresh_token"] == new["refresh_token"]
