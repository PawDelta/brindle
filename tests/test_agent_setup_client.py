"""The org policy's ``agent_setup`` block, parsed by the client."""

import copy
import json
import logging

import pytest

from brindle.pro import license, team_policy
from brindle.pro.team_policy import parse_policy

ORG = "org-1"
UUID = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
AWS = {"sso_start_url": "https://example.awsapps.com/start", "sso_region": "us-east-1",
       "account_id": "123456789012", "role_name": "BrindleWorker", "region": "us-west-2"}
GCP = {"project": "acme-brindle-prod", "region": "us-east5"}
AZURE = {"subscription_id": UUID, "resource": "acme-foundry"}
GATEWAY = {"base_url": "https://gateway.example.com/v1"}
MODELS = {"opus": "claude-opus-4-5", "sonnet": "claude-sonnet-4-5", "haiku": "claude-haiku-4-5"}
OWN_AWS = {"region": "us-west-2"}           # no SSO group: the machine's own AWS sign-in applies
FOUNDRY_URL = "https://acme.services.ai.azure.com/anthropic"
IDS = ["us.anthropic.claude-opus-4-5-v1:0", "us.anthropic.claude-sonnet-4-5-v1:0"]
FED_AWS = {"role_arn": "arn:aws:iam::123456789012:role/brindle-members-acme"}
FED_GCP = {"project_number": "123456789", "pool_id": "brindle-members", "provider_id": "pawdelta",
           "service_account": "brindle-members@acme-brindle-prod.iam.gserviceaccount.com",
           "max_session_age": 7200}
FED_AZURE = {"tenant_id": UUID, "client_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"}


def body(agent_setup=None, **policy):
    """A policy answer; ``agent_setup`` is added under the policy when given."""
    p = {"allowed_providers": ["anthropic"], "require_human_review": True, **policy}
    if agent_setup is not None:
        p["agent_setup"] = agent_setup
    return {"version": 3, "policy": p}


def parse(agent_setup):
    return parse_policy(ORG, body(agent_setup)).agent_setup


def claude(**over):
    return {"claude": {"route": "subscription", "org_id": UUID, **over}}


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == team_policy.AGENT_ENFORCE_FEATURE)


@pytest.fixture
def no_enterprise(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)


# -- route-specific fields ---------------------------------------------------

@pytest.mark.variant("Subscription parsing must retain its organization identity")
@pytest.mark.parametrize("route", ["subscription"])
def test_org_routes_keep_org_id(route, no_enterprise):
    c = parse({"claude": {"route": route, "org_id": UUID}}).claude
    assert c.route == route and c.org_id == UUID


@pytest.mark.parametrize("route,base_url,kept", [
    ("api_key", "https://llm.acme.internal/v1", None),     # a login route drops it
    ("azure", None, None),                                                         # older than the field
    ("azure", "https://acme.openai.azure.com/v1", "https://acme.openai.azure.com/v1"),
    ("gateway", "https://llm.acme.internal/v1", "https://llm.acme.internal/v1")])
@pytest.mark.variant("Codex login routes discard endpoints while Azure and gateway routes retain them, including older Azure policies")
def test_codex_routes(route, base_url, kept, no_enterprise):
    s = parse({"codex": {"route": route, **({"base_url": base_url} if base_url else {})}})
    assert s.codex.route == route and s.codex.base_url == kept
    assert s.claude is None


@pytest.mark.variant("Repeated model IDs deduplicate in order; an empty allowlist permits a default")
def test_models_kept_for_any_route(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "aws": AWS, "models": MODELS}}).claude
    assert c.models.opus == "claude-opus-4-5"
    assert c.models.sonnet == "claude-sonnet-4-5"
    assert c.models.haiku == "claude-haiku-4-5"
    assert c.model_ids == () and c.default_model is None
    c = parse({"claude": {"route": "console", "org_id": UUID, "model_ids": [*IDS, IDS[0]], "default_model": IDS[1]}}).claude
    assert c.model_ids == tuple(IDS) and c.default_model == IDS[1]
    c = parse({"claude": {"route": "gateway", "gateway": GATEWAY, "model_ids": [], "default_model": IDS[0]}}).claude
    assert c.model_ids == () and c.default_model == IDS[0]


# -- pruning -----------------------------------------------------------------

@pytest.mark.variant("A subscription policy carrying stale cloud credential blocks must discard them")
def test_unused_sub_blocks_are_pruned(no_enterprise):
    block = {"route": "subscription", "org_id": UUID, "aws": AWS, "gcp": GCP, "azure": AZURE, "gateway": GATEWAY}
    c = parse({"claude": block}).claude
    assert (c.aws, c.gcp, c.azure, c.gateway) == (None, None, None, None)


@pytest.mark.variant("Non-org routes must discard org identity and subscriptions must discard pinned model IDs")
def test_org_id_pruned_from_non_org_routes(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "org_id": UUID, "aws": AWS}}).claude
    assert c.org_id is None
    c = parse({"claude": {"route": "subscription", "org_id": UUID, "model_ids": IDS, "default_model": IDS[0]}}).claude
    assert c.model_ids == () and c.default_model is None        # a subscription serves no ids


def test_route_missing_its_sub_block_is_malformed(no_enterprise):
    assert parse({"claude": {"route": "bedrock"}}) is None


# -- every invalid field -----------------------------------------------------

BAD_CASES = [
    pytest.param("aws sso_start_url with userinfo", {"claude": {"route": "bedrock", "aws": {**AWS, "sso_start_url": "https://u:p@x.example.com/start"}}}, marks=pytest.mark.variant("Reject credentials embedded in an SSO URL")),
    ("codex route", {"codex": {"route": "bing"}}),
    ("unknown nested field with a secret value", {"claude": {"route": "bedrock", "aws": AWS, "future": {"a": ["sk-ant-api03-abcdefghijklmnop"]}}}),
    ("enforce not a bool", {"enforce": "yes"}),
    ("not an object", "claude"),
    ("secret in gateway url", {"claude": {"route": "gateway", "gateway": {"base_url": "https://gw.example.com/?token=abc"}}}),
    ("secret-shaped resource", {"claude": {"route": "foundry", "azure": {**AZURE, "resource": "ghp_abcdefghijklmnopqrstu"}}}),
    ("aws SSO group incomplete", {"claude": {"route": "bedrock", "aws": {k: v for k, v in AWS.items() if k != "role_name"}}}),
    ("aws region missing without SSO", {"claude": {"route": "bedrock", "aws": {}}}),
    ("azure neither resource nor base_url", {"claude": {"route": "foundry", "azure": {"subscription_id": UUID}}}),
    ("azure resource without subscription", {"claude": {"route": "foundry", "azure": {"resource": "acme-foundry"}}}),
    ("azure base_url secret-shaped", {"claude": {"route": "foundry", "azure": {"base_url": FOUNDRY_URL + "?api-key=abc"}}}),
    ("default_model not in model_ids", {"claude": {"route": "bedrock", "aws": OWN_AWS, "model_ids": IDS, "default_model": "x"}}),
    ("too many model_ids", {"claude": {"route": "bedrock", "aws": OWN_AWS, "model_ids": [f"m-{i}" for i in range(33)]}}),
    pytest.param("model_ids not a list", {"claude": {"route": "bedrock", "aws": OWN_AWS, "model_ids": IDS[0]}}, marks=pytest.mark.variant("A scalar model ID must not be accepted as an iterable allowlist")),
    ("model id secret-shaped", {"claude": {"route": "bedrock", "aws": OWN_AWS, "model_ids": ["sk-ant-api03-abcdefghijklmnop"]}}),
    pytest.param("model_ids validated for subscription too", {"claude": {"route": "subscription", "org_id": UUID, "model_ids": ["a b"]}}, marks=pytest.mark.variant("Malformed model restrictions must be rejected even on routes that prune them")),
    pytest.param("codex gateway without base_url", {"codex": {"route": "gateway"}}, marks=pytest.mark.variant("A gateway without an endpoint must not become a personal login route")),
    ("antigravity route", {"antigravity": {"route": "bing"}}),
    ("antigravity gateway without base_url", {"antigravity": {"route": "gateway"}}),
    pytest.param("antigravity base_url http", {"antigravity": {"route": "gateway", "base_url": "http://llm.acme.internal/v1"}}, marks=pytest.mark.variant("Antigravity gateways must reject cleartext credential transport")),
    ("codex login route with a bad base_url", {"codex": {"route": "chatgpt", "base_url": "nope"}}),
    ("deny_personal_keys not a bool", {"codex": {"route": "chatgpt"}, "deny_personal_keys": "yes"}),
    ("exclusive not a bool", {"codex": {"route": "chatgpt"}, "exclusive": 1}),
    ("federated without its federation block", {"claude": {"route": "vertex", "auth": "federated", "gcp": GCP}}),
    ("auth is not a known mode", {"claude": {"route": "vertex", "auth": "magic", "gcp": GCP}}),
    ("federation role in another account than account_id", {"claude": {
        "route": "bedrock", "auth": "federated",
        "aws": {"region": "us-west-2", "account_id": "999999999999", "federation": FED_AWS}}}),
    ("federation max_session_age under an hour", {"claude": {
        "route": "bedrock", "auth": "federated",
        "aws": {"region": "us-west-2", "federation": {**FED_AWS, "max_session_age": 60}}}}),
    ("federation value shaped like a secret", {"claude": {
        "route": "foundry", "auth": "federated",
        "azure": {"resource": "acme-foundry", "federation": {**FED_AZURE, "client_id": "AKIAIOSFODNN7EXAMPLE"}}}}),
]


@pytest.mark.parametrize("name,raw", BAD_CASES, ids=[c.values[0] if hasattr(c, "values") else c[0] for c in BAD_CASES])
def test_invalid_field_is_ignored(name, raw, no_enterprise, caplog):
    with caplog.at_level(logging.WARNING, logger="brindle.pro.team_policy"):
        p = parse_policy(ORG, body(raw))
    assert p.agent_setup is None
    assert [r for r in caplog.records if "agent_setup" in r.getMessage()], name


def test_sso_start_url_trailing_hash_is_stripped(no_enterprise):
    url = "https://d-1234.awsapps.com/start#"
    s = parse({"claude": {"route": "bedrock", "aws": {**AWS, "sso_start_url": url}}})
    assert s.claude.aws.sso_start_url == url[:-1]


def test_known_fields_survive_next_to_unknown_ones(no_enterprise):
    s = parse({"claude": {"route": "bedrock", "aws": {**AWS, "profile": "x"}, "models": {**MODELS, "turbo": "t"}},
               "future": 1})
    assert s.claude.aws.region == AWS["region"] and s.claude.models.opus == MODELS["opus"]


# -- enforce gating by entitlement ---------------------------------------------

@pytest.mark.variant("A server enforcement request must not grant an unlicensed client enforcement")
def test_enforce_dropped_without_enterprise(no_enterprise):
    s = parse({"enforce": True, "claude": {"route": "subscription", "org_id": UUID}})
    assert s.enforce is False
    assert s.claude.route == "subscription"


def test_enforce_entitlement_error_fails_closed(monkeypatch):
    def boom(feature):
        raise RuntimeError("no license store")
    monkeypatch.setattr(license, "has", boom)
    assert parse({"enforce": True}).enforce is False


# -- a malformed block is ignored; the rest of the policy applies ---------------

def test_malformed_block_leaves_rest_of_policy(no_enterprise, caplog):
    raw = body({"claude": {"route": "bedrock", "aws": {**AWS, "account_id": "bad"}}},
               require_human_review=True, max_parallel_workers=4,
               allowed_models=["claude-opus-4-5"], provider_config={"provider": "bedrock",
                                                                    "region": "us-west-2"})
    with caplog.at_level(logging.WARNING, logger="brindle.pro.team_policy"):
        p = parse_policy(ORG, raw)
    assert p.agent_setup is None
    assert p.require_human_review is True
    assert p.max_parallel_workers == 4
    assert p.allowed_models == ("claude-opus-4-5",)
    assert p.provider_config.provider == "bedrock"
    assert p.allowed_providers == ("anthropic",)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and "agent_setup" in r.getMessage()]
    assert len(warnings) == 1


def test_malformed_block_in_cache_is_ignored_on_reload(no_enterprise):
    p = parse_policy(ORG, body({"claude": {"route": "nope"}}, max_parallel_workers=5))
    again = parse_policy(ORG, copy.deepcopy(p.to_json()))
    assert again.agent_setup is None
    assert again.max_parallel_workers == 5


@pytest.mark.parametrize("route,block,fed,field,want", [
    ("bedrock", "aws", FED_AWS, "role_arn", FED_AWS["role_arn"]),
    ("vertex", "gcp", FED_GCP, "service_account", FED_GCP["service_account"]),
    ("foundry", "azure", FED_AZURE, "client_id", FED_AZURE["client_id"]),
])
def test_federated_auth_parses_the_federation_block(route, block, fed, field, want, no_enterprise):
    base = {"bedrock": {"region": "us-west-2"}, "vertex": GCP, "foundry": {"resource": "acme-foundry"}}[route]
    raw = {"claude": {"route": route, "auth": "federated", block: {**base, "federation": fed}}}
    p = parse_policy(ORG, body(raw))
    c = p.agent_setup.claude
    f = getattr(c, block).federation
    assert c.federated and getattr(f, field) == want
    assert f.max_session_age == fed.get("max_session_age", 86400)
    if route == "bedrock":      # no SSO group: the account and role come from the ARN
        assert c.aws.account_id == "123456789012" and c.aws.role_name == "brindle-members-acme"
    assert parse_policy(ORG, p.to_json()).agent_setup == p.agent_setup
    own = {"bedrock": AWS, "vertex": GCP, "foundry": AZURE}[route]
    without = parse({"claude": {"route": route, block: {**own, "federation": fed}}}).claude
    assert without.auth == "own_login" and not without.federated    # absent auth: today's behaviour
    assert getattr(without, block).federation is None


@pytest.mark.variant("JSON cache reload must preserve exclusive routing and tuple model IDs across all client providers")
def test_block_round_trips_through_the_cache(enterprise):
    raw = {"enforce": True, "exclusive": True, "deny_personal_keys": True,
           "claude": {"route": "foundry", "azure": {"base_url": FOUNDRY_URL}, "models": MODELS,
                      "model_ids": IDS, "default_model": IDS[0]},
           "codex": {"route": "gateway", "base_url": "https://llm.acme.internal/v1"},
           "antigravity": {"route": "gateway", "base_url": "https://llm.acme.internal/v1"}}
    p = parse_policy(ORG, body(raw))
    again = parse_policy(ORG, p.to_json())                      # asdict: model_ids come back as a tuple
    assert again.agent_setup == p.agent_setup
    assert again.agent_setup.claude.model_ids == tuple(IDS) and again.agent_setup.exclusive is True
    assert parse_policy(ORG, json.loads(json.dumps(p.to_json()))).agent_setup == p.agent_setup
