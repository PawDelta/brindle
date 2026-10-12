"""The org policy's ``agent_setup`` block, parsed by the client."""

import copy
import json
import logging

import pytest

from brindle.pro import license, team_policy
from brindle.pro.team_policy import AgentSetup, parse_policy

ORG = "org-1"
UUID = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
AWS = {"sso_start_url": "https://example.awsapps.com/start", "sso_region": "us-east-1",
       "account_id": "123456789012", "role_name": "BrindleWorker", "region": "us-west-2"}
GCP = {"project": "acme-brindle-prod", "region": "us-east5"}
AZURE = {"subscription_id": UUID, "resource": "acme-foundry"}
GATEWAY = {"base_url": "https://gateway.example.com/v1"}
MODELS = {"opus": "claude-opus-4-5", "sonnet": "claude-sonnet-4-5", "haiku": "claude-haiku-4-5"}


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


# -- each route parsed -------------------------------------------------------

@pytest.mark.parametrize("route", ["subscription", "console"])
def test_org_routes_keep_org_id(route, no_enterprise):
    s = parse({"claude": {"route": route, "org_id": UUID}})
    assert isinstance(s, AgentSetup)
    assert s.claude.route == route
    assert s.claude.org_id == UUID


def test_bedrock_route_keeps_aws_only(no_enterprise):
    s = parse({"claude": {"route": "bedrock", "aws": AWS}})
    c = s.claude
    assert c.route == "bedrock"
    assert c.aws.sso_start_url == AWS["sso_start_url"]
    assert c.aws.account_id == "123456789012"
    assert c.aws.role_name == "BrindleWorker"
    assert c.aws.region == "us-west-2"
    assert c.gcp is None and c.azure is None and c.gateway is None


def test_vertex_route_keeps_gcp_only(no_enterprise):
    c = parse({"claude": {"route": "vertex", "gcp": GCP}}).claude
    assert c.route == "vertex"
    assert c.gcp.project == "acme-brindle-prod"
    assert c.gcp.region == "us-east5"
    assert c.aws is None and c.azure is None


def test_foundry_route_keeps_azure_only(no_enterprise):
    c = parse({"claude": {"route": "foundry", "azure": AZURE}}).claude
    assert c.route == "foundry"
    assert c.azure.subscription_id == UUID
    assert c.azure.resource == "acme-foundry"
    assert c.aws is None and c.gcp is None


def test_gateway_route_keeps_gateway_only(no_enterprise):
    c = parse({"claude": {"route": "gateway", "gateway": GATEWAY}}).claude
    assert c.route == "gateway"
    assert c.gateway.base_url == "https://gateway.example.com/v1"
    assert c.aws is None and c.gcp is None and c.azure is None


@pytest.mark.parametrize("route", ["chatgpt", "api_key", "azure"])
def test_codex_routes(route, no_enterprise):
    s = parse({"codex": {"route": route}})
    assert s.codex.route == route
    assert s.claude is None


def test_models_kept_for_any_route(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "aws": AWS, "models": MODELS}}).claude
    assert c.models.opus == "claude-opus-4-5"
    assert c.models.sonnet == "claude-sonnet-4-5"
    assert c.models.haiku == "claude-haiku-4-5"


def test_bedrock_model_ids_with_colon_and_dots(no_enterprise):
    ids = {"opus": "us.anthropic.claude-opus-4-5-v1:0"}
    c = parse({"claude": {"route": "bedrock", "aws": AWS, "models": ids}}).claude
    assert c.models.opus == "us.anthropic.claude-opus-4-5-v1:0"
    assert c.models.sonnet is None


def test_absent_block_is_none(no_enterprise):
    assert parse_policy(ORG, body()).agent_setup is None


# -- the routing fields (model-routing phase 1) --------------------------------

OWN_AWS = {"region": "us-west-2"}
FOUNDRY_URL = "https://acme.services.ai.azure.com/anthropic"
IDS = ["us.anthropic.claude-opus-4-5-v1:0", "us.anthropic.claude-sonnet-4-5-v1:0"]


def test_bedrock_without_the_sso_group_keeps_the_region_only(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "aws": OWN_AWS}}).claude
    assert c.aws.region == "us-west-2" and c.aws.sso is False
    assert (c.aws.sso_start_url, c.aws.sso_region, c.aws.account_id, c.aws.role_name) == (None,) * 4
    assert parse({"claude": {"route": "bedrock", "aws": AWS}}).claude.aws.sso is True


@pytest.mark.parametrize("missing", ["sso_start_url", "sso_region", "account_id", "role_name"])
def test_a_partial_sso_group_is_malformed(missing, no_enterprise, caplog):
    aws = {k: v for k, v in AWS.items() if k != missing}
    with caplog.at_level(logging.WARNING, logger="brindle.pro.team_policy"):
        assert parse({"claude": {"route": "bedrock", "aws": aws}}) is None
    assert any("all four or none" in r.getMessage() and missing in r.getMessage() for r in caplog.records)


def test_bedrock_region_stays_required(no_enterprise):
    assert parse({"claude": {"route": "bedrock", "aws": {}}}) is None


def test_foundry_by_base_url(no_enterprise):
    c = parse({"claude": {"route": "foundry", "azure": {"base_url": FOUNDRY_URL}}}).claude
    assert c.azure.base_url == FOUNDRY_URL
    assert c.azure.resource is None and c.azure.subscription_id is None


def test_foundry_by_resource_may_also_name_the_base_url(no_enterprise):
    c = parse({"claude": {"route": "foundry", "azure": {**AZURE, "base_url": FOUNDRY_URL}}}).claude
    assert (c.azure.subscription_id, c.azure.resource, c.azure.base_url) == (UUID, "acme-foundry", FOUNDRY_URL)


@pytest.mark.parametrize("azure", [
    {}, {"subscription_id": UUID},                       # neither resource nor base_url
    {"resource": "acme-foundry"},                        # a resource needs its subscription
    {"base_url": "http://acme.services.ai.azure.com"},   # not https
    {"base_url": "https://u@acme.services.ai.azure.com"},
    {"base_url": FOUNDRY_URL + "?api-key=abc"},          # secret-shaped
])
def test_malformed_azure_blocks(azure, no_enterprise):
    assert parse({"claude": {"route": "foundry", "azure": azure}}) is None


def test_model_ids_and_default_model(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "aws": OWN_AWS, "model_ids": IDS,
                          "default_model": IDS[1]}}).claude
    assert c.model_ids == tuple(IDS) and c.default_model == IDS[1]


def test_model_ids_default_to_none(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "aws": OWN_AWS}}).claude
    assert c.model_ids == () and c.default_model is None
    c = parse({"claude": {"route": "bedrock", "aws": OWN_AWS, "model_ids": []}}).claude
    assert c.model_ids == () and c.default_model is None


def test_model_ids_are_deduplicated_in_order(no_enterprise):
    c = parse({"claude": {"route": "vertex", "gcp": GCP, "model_ids": [IDS[1], IDS[0], IDS[1]]}}).claude
    assert c.model_ids == (IDS[1], IDS[0])


def test_a_default_model_alone_is_fine(no_enterprise):
    c = parse({"claude": {"route": "gateway", "gateway": GATEWAY, "default_model": "claude-sonnet-4-5"}}).claude
    assert c.model_ids == () and c.default_model == "claude-sonnet-4-5"


@pytest.mark.parametrize("name,over", [
    ("default not in ids", {"model_ids": IDS, "default_model": "claude-haiku-4-5"}),
    ("too many ids", {"model_ids": [f"model-{i}" for i in range(33)]}),
    ("not a list", {"model_ids": IDS[0]}),
    ("id with a space", {"model_ids": ["claude opus"]}),
    ("id with a slash", {"model_ids": ["a/b"]}),
    ("id not a string", {"model_ids": [4]}),
    ("secret-shaped id", {"model_ids": ["sk-ant-api03-abcdefghijklmnop"]}),
    ("default not a string", {"default_model": 4}),
])
def test_malformed_model_ids(name, over, no_enterprise):
    assert parse({"claude": {"route": "bedrock", "aws": OWN_AWS, **over}}) is None, name


@pytest.mark.parametrize("route,block", [("console", {}), ("bedrock", {"aws": OWN_AWS}), ("vertex", {"gcp": GCP}),
                                         ("foundry", {"azure": AZURE}), ("gateway", {"gateway": GATEWAY})])
def test_model_ids_kept_for_every_route_but_subscription(route, block, no_enterprise):
    c = parse({"claude": {"route": route, "org_id": UUID, **block, "model_ids": IDS, "default_model": IDS[0]}}).claude
    assert c.model_ids == tuple(IDS) and c.default_model == IDS[0]


def test_model_ids_pruned_from_subscription_but_still_validated(no_enterprise):
    c = parse({"claude": {"route": "subscription", "org_id": UUID, "model_ids": IDS, "default_model": IDS[0]}}).claude
    assert c.model_ids == () and c.default_model is None
    assert parse({"claude": {"route": "subscription", "org_id": UUID, "model_ids": ["a b"]}}) is None


def test_codex_gateway_needs_its_base_url(no_enterprise):
    s = parse({"codex": {"route": "gateway", "base_url": "https://llm.acme.internal/v1"}})
    assert s.codex.route == "gateway" and s.codex.base_url == "https://llm.acme.internal/v1"
    assert parse({"codex": {"route": "gateway"}}) is None
    assert parse({"codex": {"route": "gateway", "base_url": "http://llm.acme.internal/v1"}}) is None


def test_codex_azure_base_url_is_optional(no_enterprise):
    assert parse({"codex": {"route": "azure"}}).codex.base_url is None    # policies older than the field
    s = parse({"codex": {"route": "azure", "base_url": "https://acme.openai.azure.com/v1"}})
    assert s.codex.base_url == "https://acme.openai.azure.com/v1"
    assert parse({"codex": {"route": "azure", "base_url": "ftp://x"}}) is None


@pytest.mark.parametrize("route", ["chatgpt", "api_key"])
def test_codex_login_routes_drop_a_base_url(route, no_enterprise):
    assert parse({"codex": {"route": route, "base_url": "https://llm.acme.internal/v1"}}).codex.base_url is None
    assert parse({"codex": {"route": route, "base_url": "nope"}}) is None


def test_deny_personal_keys_in_the_block(no_enterprise):
    assert parse({"codex": {"route": "chatgpt"}}).deny_personal_keys is None
    assert parse({"codex": {"route": "chatgpt"}, "deny_personal_keys": True}).deny_personal_keys is True
    assert parse({"codex": {"route": "chatgpt"}, "deny_personal_keys": False}).deny_personal_keys is False
    assert parse({"codex": {"route": "chatgpt"}, "deny_personal_keys": "yes"}) is None


def test_routed_names_the_routes_an_enforced_policy_manages(no_enterprise):
    assert parse({"claude": {"route": "bedrock", "aws": OWN_AWS}}).routed is True
    assert parse({"claude": {"route": "gateway", "gateway": GATEWAY}}).routed is True
    assert parse({"codex": {"route": "gateway", "base_url": "https://llm.acme.internal/v1"}}).routed is True
    assert parse({"codex": {"route": "azure", "base_url": "https://acme.openai.azure.com/v1"}}).routed is True
    assert parse({"codex": {"route": "azure"}}).routed is False
    assert parse({"claude": {"route": "subscription", "org_id": UUID}, "codex": {"route": "chatgpt"}}).routed is False
    assert parse({"enforce": True}).routed is False


def test_routing_fields_round_trip_through_the_cache(enterprise):
    raw = {"enforce": True, "deny_personal_keys": True,
           "claude": {"route": "foundry", "azure": {"base_url": FOUNDRY_URL}, "model_ids": IDS,
                      "default_model": IDS[0]},
           "codex": {"route": "gateway", "base_url": "https://llm.acme.internal/v1"}}
    p = parse_policy(ORG, body(raw))
    for again in (parse_policy(ORG, p.to_json()), parse_policy(ORG, json.loads(json.dumps(p.to_json())))):
        assert again.agent_setup == p.agent_setup
    assert p.agent_setup.claude.model_ids == tuple(IDS) and p.agent_setup.deny_personal_keys is True


def test_the_effective_policy_carries_the_block(no_enterprise):
    raw = body({"claude": {"route": "bedrock", "aws": OWN_AWS}})
    raw["effective"] = {"allowed_providers": ["claude"], "require_human_review": False}
    p = parse_policy(ORG, raw)
    assert p.enforced is p.effective and p.effective.agent_setup == p.agent_setup
    p = parse_policy(ORG, body({"claude": {"route": "bedrock", "aws": OWN_AWS}},
                               roles={"member": {"max_parallel_workers": 1}}))
    assert team_policy.with_role_overrides(p, "member", None).enforced.agent_setup == p.agent_setup


# -- pruning -----------------------------------------------------------------

@pytest.mark.parametrize("route,extra", [
    ("subscription", {"aws": AWS, "gcp": GCP, "azure": AZURE, "gateway": GATEWAY}),
    ("console", {"aws": AWS, "gcp": GCP, "azure": AZURE, "gateway": GATEWAY}),
    ("bedrock", {"gcp": GCP, "azure": AZURE, "gateway": GATEWAY}),
    ("vertex", {"aws": AWS, "azure": AZURE, "gateway": GATEWAY}),
    ("foundry", {"aws": AWS, "gcp": GCP, "gateway": GATEWAY}),
    ("gateway", {"aws": AWS, "gcp": GCP, "azure": AZURE}),
])
def test_unused_sub_blocks_are_pruned(route, extra, no_enterprise):
    needed = {"bedrock": "aws", "vertex": "gcp", "foundry": "azure", "gateway": "gateway"}
    block = {"route": route, "org_id": UUID, **extra,
             **({needed[route]: _block(route)} if route in needed else {})}
    c = parse({"claude": block}).claude
    for name in ("aws", "gcp", "azure", "gateway"):
        if name != needed.get(route):
            assert getattr(c, name) is None, name


def test_org_id_pruned_from_non_org_routes(no_enterprise):
    c = parse({"claude": {"route": "bedrock", "org_id": UUID, "aws": AWS}}).claude
    assert c.org_id is None


def _block(route):
    return {"bedrock": AWS, "vertex": GCP, "foundry": AZURE, "gateway": GATEWAY}[route]


def test_route_missing_its_sub_block_is_malformed(no_enterprise):
    assert parse({"claude": {"route": "bedrock"}}) is None


def test_org_route_missing_org_id_is_malformed(no_enterprise):
    assert parse({"claude": {"route": "subscription"}}) is None


# -- every invalid field -----------------------------------------------------

BAD_CASES = [
    ("claude route", {"claude": {"route": "openai", "org_id": UUID}}),
    ("claude route missing", {"claude": {"org_id": UUID}}),
    ("org_id not a UUID", {"claude": {"route": "console", "org_id": "not-a-uuid"}}),
    ("org_id short", {"claude": {"route": "console", "org_id": UUID[:-1]}}),
    ("org_id in bedrock still validated", {"claude": {"route": "bedrock", "org_id": "x", "aws": AWS}}),
    ("aws sso_start_url http", {"claude": {"route": "bedrock", "aws": {**AWS, "sso_start_url": "http://x.example.com/start"}}}),
    ("aws sso_start_url with userinfo", {"claude": {"route": "bedrock", "aws": {**AWS, "sso_start_url": "https://u:p@x.example.com/start"}}}),
    ("aws sso_start_url no host", {"claude": {"route": "bedrock", "aws": {**AWS, "sso_start_url": "https:///start"}}}),
    ("aws sso_region malformed", {"claude": {"route": "bedrock", "aws": {**AWS, "sso_region": "useast1"}}}),
    ("aws account_id 11 digits", {"claude": {"route": "bedrock", "aws": {**AWS, "account_id": "12345678901"}}}),
    ("aws account_id letters", {"claude": {"route": "bedrock", "aws": {**AWS, "account_id": "12345678901a"}}}),
    ("aws role_name with slash", {"claude": {"route": "bedrock", "aws": {**AWS, "role_name": "arn:aws:iam::1/role"}}}),
    ("aws role_name too long", {"claude": {"route": "bedrock", "aws": {**AWS, "role_name": "a" * 65}}}),
    ("aws region malformed", {"claude": {"route": "bedrock", "aws": {**AWS, "region": "us west 2"}}}),
    ("aws missing field", {"claude": {"route": "bedrock", "aws": {k: v for k, v in AWS.items() if k != "region"}}}),
    ("gcp project uppercase", {"claude": {"route": "vertex", "gcp": {**GCP, "project": "Acme-Prod"}}}),
    ("gcp project too short", {"claude": {"route": "vertex", "gcp": {**GCP, "project": "abc"}}}),
    ("gcp region malformed", {"claude": {"route": "vertex", "gcp": {**GCP, "region": "useast5"}}}),
    ("azure subscription not UUID", {"claude": {"route": "foundry", "azure": {**AZURE, "subscription_id": "sub-1"}}}),
    ("azure resource is a URL", {"claude": {"route": "foundry", "azure": {**AZURE, "resource": "https://acme.openai.azure.com"}}}),
    ("azure resource has a dot", {"claude": {"route": "foundry", "azure": {**AZURE, "resource": "acme.foundry"}}}),
    ("azure resource empty", {"claude": {"route": "foundry", "azure": {**AZURE, "resource": ""}}}),
    ("gateway base_url http", {"claude": {"route": "gateway", "gateway": {"base_url": "http://gw.example.com"}}}),
    ("gateway base_url userinfo", {"claude": {"route": "gateway", "gateway": {"base_url": "https://u@gw.example.com"}}}),
    ("gateway base_url with space", {"claude": {"route": "gateway", "gateway": {"base_url": "https://gw.example.com/a b"}}}),
    ("model id with space", {"claude": {"route": "bedrock", "aws": AWS, "models": {"opus": "claude opus"}}}),
    ("model id with slash", {"claude": {"route": "bedrock", "aws": AWS, "models": {"haiku": "a/b"}}}),
    ("model id not a string", {"claude": {"route": "bedrock", "aws": AWS, "models": {"sonnet": 4}}}),
    ("codex route", {"codex": {"route": "bing"}}),
    ("unknown field with a secret value", {"codex": {"route": "chatgpt", "extra": "AKIAIOSFODNN7EXAMPLE"}}),
    ("unknown nested field with a secret value", {"claude": {"route": "bedrock", "aws": AWS, "future": {"a": ["sk-ant-api03-abcdefghijklmnop"]}}}),
    ("unknown field named like a secret", {"codex": {"route": "chatgpt", "api_key=abc": 1}}),
    ("enforce not a bool", {"enforce": "yes"}),
    ("enforce zero", {"enforce": 0}),
    ("not an object", "claude"),
    ("claude not an object", {"claude": "subscription"}),
    ("secret-shaped api key in a name", {"claude": {"route": "bedrock", "aws": {**AWS, "role_name": "AKIAIOSFODNN7EXAMPLE"}}}),
    ("secret-shaped key in model id", {"claude": {"route": "bedrock", "aws": AWS, "models": {"opus": "sk-ant-api03-abcdefghijklmnop"}}}),
    ("secret in gateway url", {"claude": {"route": "gateway", "gateway": {"base_url": "https://gw.example.com/?token=abc"}}}),
    ("secret-shaped resource", {"claude": {"route": "foundry", "azure": {**AZURE, "resource": "ghp_abcdefghijklmnopqrstu"}}}),
]


@pytest.mark.parametrize("name,raw", BAD_CASES, ids=[c[0] for c in BAD_CASES])
def test_invalid_field_is_ignored(name, raw, no_enterprise, caplog):
    with caplog.at_level(logging.WARNING, logger="brindle.pro.team_policy"):
        p = parse_policy(ORG, body(raw))
    assert p.agent_setup is None
    assert [r for r in caplog.records if "agent_setup" in r.getMessage()], name


def test_warning_names_the_problem_not_the_value(no_enterprise, caplog):
    secret = "AKIAIOSFODNN7EXAMPLE"
    with caplog.at_level(logging.WARNING, logger="brindle.pro.team_policy"):
        parse_policy(ORG, body({"claude": {"route": "bedrock", "aws": {**AWS, "role_name": secret}}}))
    assert secret not in caplog.text


@pytest.mark.parametrize("url", [
    "https://d-1234.awsapps.com/start/#", "https://d-1234.awsapps.com/start#"])
def test_sso_start_url_trailing_hash_is_stripped(url, no_enterprise):
    s = parse({"claude": {"route": "bedrock", "aws": {**AWS, "sso_start_url": url}}})
    assert s.claude.aws.sso_start_url == url[:-1]


def test_sso_start_url_without_hash_unchanged(no_enterprise):
    assert parse({"claude": {"route": "bedrock", "aws": AWS}}).claude.aws.sso_start_url == AWS["sso_start_url"]


def test_sso_start_url_fragment_in_the_middle_is_still_invalid(no_enterprise):
    assert parse({"claude": {"route": "bedrock",
                             "aws": {**AWS, "sso_start_url": "https://x.awsapps.com/#/start"}}}) is None


@pytest.mark.parametrize("raw,where", [
    ({"claude": {"route": "subscription", "org_id": UUID}, "webhook": "x"}, "agent_setup"),
    ({"claude": {"route": "bedrock", "aws": {**AWS, "profile": "x"}}}, "aws"),
    ({"claude": {"route": "bedrock", "aws": AWS, "models": {"turbo": "x"}}}, "models"),
    ({"codex": {"route": "chatgpt", "api_key": "x"}}, "codex"),
    ({"claude": {"route": "subscription", "org_id": UUID, "future": {"a": 1}}}, "claude"),
])
def test_unknown_keys_are_ignored_not_fatal(raw, where, no_enterprise, caplog):
    with caplog.at_level(logging.DEBUG, logger="brindle.pro.team_policy"):
        s = parse(raw)
    assert isinstance(s, AgentSetup)
    assert (s.claude is not None) == ("claude" in raw)
    assert (s.codex is not None) == ("codex" in raw)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    notes = [r for r in caplog.records if "unknown" in r.getMessage() and where in r.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.DEBUG


def test_known_fields_survive_next_to_unknown_ones(no_enterprise):
    s = parse({"claude": {"route": "bedrock", "aws": {**AWS, "profile": "x"}, "models": {**MODELS, "turbo": "t"}},
               "future": 1})
    assert s.claude.aws.region == AWS["region"] and s.claude.models.opus == MODELS["opus"]


# -- enforce gating by entitlement ---------------------------------------------

def test_enforce_honoured_with_enterprise(enterprise):
    s = parse({"enforce": True, "claude": {"route": "subscription", "org_id": UUID}})
    assert s.enforce is True


def test_enforce_dropped_without_enterprise(no_enterprise):
    s = parse({"enforce": True, "claude": {"route": "subscription", "org_id": UUID}})
    assert s.enforce is False
    assert s.claude.route == "subscription"


def test_enforce_false_is_false_even_with_enterprise(enterprise):
    assert parse({"enforce": False, "claude": {"route": "subscription", "org_id": UUID}}).enforce is False


def test_enforce_defaults_false(enterprise):
    assert parse({"claude": {"route": "subscription", "org_id": UUID}}).enforce is False


def test_enforce_only_block_is_valid(enterprise):
    s = parse({"enforce": True})
    assert s.enforce is True and s.claude is None and s.codex is None


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


def test_malformed_block_does_not_raise(no_enterprise):
    for raw in ([], "x", 3, {"claude": None, "codex": []}):
        p = parse_policy(ORG, body(raw))
        assert p.agent_setup is None


def test_valid_block_alongside_other_policy_fields(no_enterprise):
    p = parse_policy(ORG, body({"codex": {"route": "chatgpt"}}, max_parallel_workers=2))
    assert p.agent_setup.codex.route == "chatgpt"
    assert p.max_parallel_workers == 2


def test_malformed_block_in_cache_is_ignored_on_reload(no_enterprise):
    p = parse_policy(ORG, body({"claude": {"route": "nope"}}, max_parallel_workers=5))
    again = parse_policy(ORG, copy.deepcopy(p.to_json()))
    assert again.agent_setup is None
    assert again.max_parallel_workers == 5


def test_block_round_trips_through_the_cache(enterprise):
    raw = {"enforce": True, "claude": {"route": "vertex", "gcp": GCP, "models": MODELS},
           "codex": {"route": "api_key"}}
    p = parse_policy(ORG, body(raw))
    again = parse_policy(ORG, p.to_json())
    assert again.agent_setup == p.agent_setup
    assert again.agent_setup.claude.gcp.project == "acme-brindle-prod"
    assert again.agent_setup.enforce is True
