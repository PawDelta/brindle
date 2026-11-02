"""Company identity: doctor lines, mismatch warnings, enforce at launch, --fix."""

from __future__ import annotations

import json
import pytest

from brindle import agents, antigravity, company_identity, company_login, doctor, providers, workspaces
from brindle.db import Agent
from brindle.pro.team_policy import (AgentSetup, AwsFederation, AwsSetup, AzureFederation, AzureSetup,
                                     ClaudeSetup, CodexSetup, GatewaySetup, GcpFederation, GcpSetup)

ORG = "acme"
CLAUDE_ORG = "11111111-2222-3333-4444-555555555555"
SUB = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
AWS = AwsSetup("us-west-2", "https://acme.awsapps.com/start", "us-east-1", "123456789012", "BrindleWorker")
OWN_AWS = AwsSetup("us-west-2")          # no SSO group: the machine's own AWS sign-in
GCP = GcpSetup("acme-brindle-prod", "us-east5")
AZURE = AzureSetup(SUB, "acme-foundry")
FOUNDRY_URL = AzureSetup(base_url="https://acme.services.ai.azure.com/anthropic")

CLAUDE_OK = (0, json.dumps({"loggedIn": True, "orgId": CLAUDE_ORG, "orgName": "Acme",
                            "subscriptionType": "enterprise"}))
AWS_OK = (0, json.dumps({"Account": "123456789012",
                         "Arn": "arn:aws:sts::123456789012:assumed-role/BrindleWorker/me"}))


class Fake:
    """Replaces company_login.run_cmd/which: ``rc`` maps an argv prefix to
    (returncode, output); anything unlisted succeeds with no output."""

    def __init__(self, monkeypatch, rc=None, missing=()):
        self.calls, self.rc, self.missing = [], rc or {}, set(missing)
        monkeypatch.setattr(company_login, "run_cmd", self.run)
        monkeypatch.setattr(company_login, "which", lambda n: None if n in self.missing else f"/bin/{n}")

    def run(self, argv, timeout=30.0, interactive=False, cwd=None, env=None, stdin=None):
        self.calls.append((tuple(argv), interactive, timeout))
        for prefix, result in self.rc.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return result
        return 0, ""

    def count(self, *prefix):
        return sum(c[0][:len(prefix)] == prefix for c in self.calls)


def policy(monkeypatch, claude=None, codex=None, enforce=False):
    setup = AgentSetup(claude=claude, codex=codex, enforce=enforce)
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, setup))


def claude_setup(route, **kw):
    return ClaudeSetup(route=route, **kw)


@pytest.fixture(autouse=True)
def installed(monkeypatch, tmp_path):
    company_identity.reset()
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude", "codex", "antigravity"])
    monkeypatch.setattr(providers, "signed_out", lambda p, env=None: None)
    monkeypatch.setattr(antigravity, "binary", lambda: "/bin/sh")


@pytest.fixture(autouse=True)
def launch_cache(monkeypatch, tmp_path):
    """The launch cache lives in a temp file, read against a clock the test moves."""
    clock = [1000.0]
    path = tmp_path / "identity-launch-cache.json"
    monkeypatch.setattr(company_identity, "_launch_cache_path", lambda: path)
    monkeypatch.setattr(company_identity, "_now", lambda: clock[0])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "CLOUDSDK_CONFIG", "CLOUDSDK_CORE_PROJECT",
                 "CLOUDSDK_ACTIVE_CONFIG_NAME", "AZURE_CONFIG_DIR", "AZURE_SUBSCRIPTION_ID"):
        monkeypatch.delenv(name, raising=False)
    return clock, path


def by_name(checks=None):
    return {c.name: c for c in (checks if checks is not None else company_identity.checks())}


# -- the identity lines --------------------------------------------------------------------


def test_gateway_line_is_the_url_and_runs_nothing(monkeypatch):
    fake = Fake(monkeypatch)
    policy(monkeypatch, claude_setup("gateway", gateway=GatewaySetup("https://gw.example.com/v1")))
    c = by_name()["claude identity"]
    assert c.level == doctor.OK and "https://gw.example.com/v1" in c.detail
    assert not fake.count("claude")


def test_codex_and_antigravity_are_informational(monkeypatch):
    Fake(monkeypatch, {("codex", "login", "status"): (0, "Logged in using ChatGPT\n"),
                       ("claude", "auth", "status"): (1, '{"loggedIn": false}')})
    policy(monkeypatch, codex=CodexSetup("chatgpt"))
    found = by_name()
    # Claude signed out with no company route: a warning, never a green check
    assert found["claude identity"].level == doctor.WARN and "signed out" in found["claude identity"].detail
    assert found["codex identity"].level == doctor.OK
    assert "Logged in using ChatGPT" in found["codex identity"].detail
    assert "can't be checked" in found["codex identity"].detail
    assert found["antigravity identity"].level == doctor.OK
    assert "can't be checked" in found["antigravity identity"].detail


def test_doctor_checks_include_the_identity_lines(monkeypatch, tmp_path):
    Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    names = [c.name for c in doctor.checks(None)]
    assert "claude identity" in names


def test_a_probe_is_cached_within_one_run(monkeypatch):
    fake = Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG))
    company_identity.identities()
    company_identity.identities()
    assert fake.count("claude", "auth", "status") == 1


# -- mismatches ----------------------------------------------------------------------------

def test_other_claude_org_warns_with_the_fix(monkeypatch):
    other = (0, json.dumps({"loggedIn": True, "orgId": "someone-else", "orgName": "Other",
                            "subscriptionType": "team"}))
    Fake(monkeypatch, {("claude", "auth", "status"): other})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN
    assert CLAUDE_ORG in c.detail and "brindle doctor --fix" in c.detail and "claude auth login --sso" in c.detail


def test_personal_claude_login_warns(monkeypatch):
    personal = (0, json.dumps({"loggedIn": True, "subscriptionType": "max"}))
    Fake(monkeypatch, {("claude", "auth", "status"): personal})
    policy(monkeypatch, claude_setup("subscription"))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and "personal" in c.detail and "brindle doctor --fix" in c.detail


def test_other_azure_subscription_warns(monkeypatch):
    Fake(monkeypatch, {("az", "account"): (0, json.dumps({"id": "other-sub", "name": "Mine"}))})
    policy(monkeypatch, claude_setup("foundry", azure=AZURE))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and "other-sub" in c.detail and "az account set" in c.detail


@pytest.mark.parametrize("rc, missing, said, not_said", [
    ({}, {"aws"}, "isn't installed", "couldn't run"),
    ({("aws", "sts"): (124, "timed out")}, set(), "couldn't run", "isn't installed"),
])
def test_missing_cli_warns_without_a_mismatch(monkeypatch, rc, missing, said, not_said):
    Fake(monkeypatch, rc, missing=missing)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and said in c.detail and not_said not in c.detail


# -- a route that leaves the cloud sign-in to the person ---------------------------------

OTHER_AWS = (0, json.dumps({"Account": "999999999999", "Arn": "arn:aws:sts::999999999999:assumed-role/Mine/me"}))


def test_bedrock_without_sso_checks_only_that_sts_answers(monkeypatch, launch_cache):
    from brindle import pane_auth

    clock, path = launch_cache
    fake = Fake(monkeypatch, {("aws", "sts"): OTHER_AWS})
    policy(monkeypatch, claude_setup("bedrock", aws=OWN_AWS), enforce=True)
    assert by_name()["claude identity"].level == doctor.OK            # any account: no match to make
    argv = next(a for a, _, _ in fake.calls if a[0] == "aws")
    assert "--profile" not in argv and argv[argv.index("--region") + 1] == "us-west-2"
    assert company_identity.expected_ident(claude_setup("bedrock", aws=OWN_AWS)) is None
    assert company_identity.launch_problem("claude") is None
    assert company_identity.launch_problem("claude") is None and fake.count("aws", "sts") == 2   # doctor, one launch
    assert company_identity.launch_check("claude") == (None, "identity_match", "bedrock")        # remembered
    assert fake.count("aws", "sts") == 2
    assert next(iter(json.loads(path.read_text()))).startswith("claude|bedrock|own|")
    # the person's own AWS sign-in reaches the pane: what was checked is what runs
    monkeypatch.setenv("AWS_PROFILE", "mine")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAMINE")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    deny = set(pane_auth.deny_names("claude", "auto", None))
    assert not {"AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"} & deny
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"} <= deny
    clock[0] += 301                                                    # past the launch cache
    Fake(monkeypatch, {("aws", "sts"): (255, "Unable to locate credentials")})
    assert by_name()["claude identity"].level == doctor.WARN
    assert company_identity.launch_problem("claude")                   # signed out is still a refusal
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert "AWS_ACCESS_KEY_ID" in pane_auth.deny_names("claude", "auto", None)    # with the SSO group: as before


def test_foundry_by_base_url_checks_only_that_az_answers(monkeypatch, launch_cache):
    from brindle import pane_auth

    clock, _ = launch_cache
    Fake(monkeypatch, {("az", "account"): (0, json.dumps({"id": "other-sub", "name": "Mine"}))})
    policy(monkeypatch, claude_setup("foundry", azure=FOUNDRY_URL), enforce=True)
    assert by_name()["claude identity"].level == doctor.OK            # any subscription
    assert company_identity.launch_problem("claude") is None
    assert company_identity.expected_ident(claude_setup("foundry", azure=FOUNDRY_URL)) is None
    monkeypatch.setenv("AZURE_CLIENT_ID", "mine")
    assert "AZURE_CLIENT_ID" not in pane_auth.deny_names("claude", "auto", None)   # the Azure sign-in is theirs
    clock[0] += 301
    Fake(monkeypatch, {("az", "account"): (1, "Please run 'az login'")})
    assert by_name()["claude identity"].level == doctor.WARN
    assert company_identity.launch_problem("claude")


# -- enforce at launch ---------------------------------------------------------------------

WRONG_AWS = {("aws", "sts"): (0, json.dumps({"Account": "999999999999", "Arn": "arn:x:y/R/s"}))}


def test_undetermined_identity_never_blocks(monkeypatch):
    Fake(monkeypatch, missing={"aws"})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_check("claude") == (None, "identity_undetermined", "bedrock")
    Fake(monkeypatch, {("aws", "sts"): (124, "timed out")})
    assert company_identity.launch_problem("claude") is None


@pytest.mark.variant("enforcement: a wrong identity must block the launch")
def test_enforce_blocks_with_the_enterprise_entitlement(monkeypatch):
    from brindle.pro import license, team_policy

    Fake(monkeypatch, WRONG_AWS)
    monkeypatch.setattr(license, "has", lambda feature: True)
    raw = {"claude": {"route": "bedrock",
                      "aws": {"sso_start_url": AWS.sso_start_url, "sso_region": AWS.sso_region,
                              "account_id": AWS.account_id, "role_name": AWS.role_name,
                              "region": AWS.region}},
           "enforce": True}
    setup = team_policy._parse_agent_setup(raw)
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, setup))
    assert setup.enforce
    assert company_identity.launch_problem("claude")


def test_a_mismatch_is_never_cached_and_is_probed_again(monkeypatch, launch_cache):
    _, path = launch_cache
    fake = Fake(monkeypatch, WRONG_AWS)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude")
    assert company_identity.launch_check("claude")[1:] == ("identity_mismatch", "bedrock")
    assert fake.count("aws", "sts") == 2
    assert not path.exists()


PERSONAL = (0, json.dumps({"loggedIn": True, "subscriptionType": "max"}))


def test_claude_login_probes_every_launch_and_catches_a_switch(monkeypatch, launch_cache):
    _, path = launch_cache
    fake = Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG), enforce=True)
    assert company_identity.launch_problem("claude") is None
    assert company_identity.launch_problem("claude") is None
    assert fake.count("claude", "auth", "status") == 2
    assert not path.exists()
    fake.rc[("claude", "auth", "status")] = PERSONAL
    assert company_identity.launch_problem("claude")
    assert fake.count("claude", "auth", "status") == 3


def cloud_cache_case(monkeypatch, route, **kw):
    fake = Fake(monkeypatch, {("aws", "sts"): AWS_OK,
                              ("gcloud", "config"): (0, GCP.project + "\n"),
                              ("az", "account"): (0, json.dumps({"id": SUB, "name": "x"}))})
    policy(monkeypatch, claude_setup(route, **kw), enforce=True)
    return fake


def touch(path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_aws_credential_env_vars_reprobe_without_being_stored(monkeypatch):
    name = "AWS_SESSION_TOKEN"
    monkeypatch.delenv(name, raising=False)
    fake = cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    monkeypatch.setenv(name, "tok")
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 2
    a = company_identity._fingerprint("bedrock")    # a secret: only that it is set, never its value
    monkeypatch.setenv(name, "another-secret")
    assert company_identity._fingerprint("bedrock") == a


def test_other_clouds_credential_env_vars_reprobe(monkeypatch):
    name = "GOOGLE_APPLICATION_CREDENTIALS"
    monkeypatch.delenv(name, raising=False)
    fake = cloud_cache_case(monkeypatch, "vertex", gcp=GCP)
    company_identity.launch_problem("claude")
    monkeypatch.setenv(name, "x")
    company_identity.launch_problem("claude")
    assert fake.count("gcloud", "config") == 2


def test_azure_profile_change_reprobes(monkeypatch, tmp_path):
    f = touch(tmp_path / "home" / ".azure" / "azureProfile.json", "{}")
    fake = cloud_cache_case(monkeypatch, "foundry", azure=AZURE)
    company_identity.launch_problem("claude")
    company_identity.launch_problem("claude")
    assert fake.count("az", "account") == 1
    f.write_text("{ changed }")
    company_identity.launch_problem("claude")
    assert fake.count("az", "account") == 2


def test_the_fingerprint_never_reads_file_contents(monkeypatch, tmp_path):
    import os

    f = touch(tmp_path / "home" / ".aws" / "credentials", "secret")
    f.chmod(0)
    if os.access(f, os.R_OK):
        pytest.skip("files are readable regardless of mode here")
    fake = cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 1
    f.chmod(0o600)


def test_codex_is_never_blocked(monkeypatch):
    Fake(monkeypatch)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), codex=CodexSetup("chatgpt"), enforce=True)
    assert company_identity.launch_problem("codex") is None


def test_launch_refuses_the_agent(db, repo, monkeypatch):
    Fake(monkeypatch, WRONG_AWS)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    ws = workspaces.create(db, str(repo), "feat").workspace
    agent = Agent("a1", ws.id, "developer", "claude", None, "assign", "starting", "", None, 0.0)
    with pytest.raises(agents.AgentError, match="brindle doctor --fix"):
        agents._launch(db, agent, ws, prompt=None, resume=None, watch_pane=False)


# -- what is checked is what runs ----------------------------------------------------------

def test_an_org_name_never_stands_in_for_the_org_id():
    # a personal org named after the company's id
    assert not company_identity.matches("org:org-1", "claude org Me (org-1) (org-9)")


def test_a_login_without_an_org_id_has_no_org(monkeypatch):
    Fake(monkeypatch, {("claude", "auth", "status"): (0, json.dumps(
        {"loggedIn": True, "orgName": f"Me ({CLAUDE_ORG})", "subscriptionType": "pro"}))})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG))
    ident = company_identity.claude_identity(ORG, claude_setup("subscription", org_id=CLAUDE_ORG))
    assert ident.ident == "claude org none"
    assert not company_identity.matches(f"org:{CLAUDE_ORG}", ident.ident)


def _env_of(db, repo, monkeypatch, profile_env):
    from types import SimpleNamespace
    from brindle.profiles import Profile

    monkeypatch.setattr(agents, "load_profile",
                        lambda name, root=None: Profile(name, "", "claude", "", env=profile_env))
    ws = workspaces.create(db, str(repo), "enf").workspace
    a = Agent("e1", ws.id, "developer", "claude", None, "assign", "starting", "", None, 0.0)
    db.add_agent(a)
    return agents.agent_env(ws, "e1", a, SimpleNamespace(config=None, denied_keys=()))


PROFILE_LINES = {"AWS_PROFILE": "personal", "AWS_ACCESS_KEY_ID": "AKIAPERSONAL", "ANTHROPIC_BASE_URL": "https://x",
                 "CLAUDE_CONFIG_DIR": "/home/me/.claude-personal", "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://x",
                 "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1", "https_proxy": "http://mitm", "EDITOR": "vi"}


def test_enforced_route_beats_a_profiles_env_lines(db, repo, monkeypatch):
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    env = _env_of(db, repo, monkeypatch, PROFILE_LINES)
    assert env["AWS_PROFILE"] == company_login.profile_name(ORG)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    for name in ("AWS_ACCESS_KEY_ID", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR",
                 "AWS_CONTAINER_CREDENTIALS_FULL_URI", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "https_proxy"):
        assert name not in env
    assert env["EDITOR"] == "vi"


def test_enforced_route_takes_inherited_and_stored_credentials_out_of_the_pane(monkeypatch):
    from brindle import pane_auth

    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://x")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("EDITOR", "vi")
    deny = pane_auth.deny_names("claude", "auto", None)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "AWS_CONTAINER_CREDENTIALS_FULL_URI", "CLAUDE_CODE_USE_VERTEX"):
        assert name in deny
    assert "EDITOR" not in deny
    assert "AWS_PROFILE" not in deny and "CLAUDE_CODE_USE_BEDROCK" not in deny   # the route's own
    assert "AWS_ACCESS_KEY_ID" not in pane_auth.deny_names("codex", "auto", None)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=False)
    assert "AWS_ACCESS_KEY_ID" not in pane_auth.deny_names("claude", "auto", None)

    # a federated route keeps its own hook variables and still strips what a profile adds
    route = company_login.claude_env(ORG, claude_setup("vertex", auth="federated", gcp=FED_GCP))
    assert {"GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"} <= set(route)
    fed_deny = company_identity.enforced_deny(route, {"AWS_WEB_IDENTITY_TOKEN_FILE"})
    assert "AWS_WEB_IDENTITY_TOKEN_FILE" in fed_deny and not set(route) & set(fed_deny)
    own = company_identity.enforced_deny({"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES" in own        # only a federated route may set it


# -- federated routes: the identity is the one brindle's own exchange assumed ---------------------------

FED_ROLE = "arn:aws:iam::123456789012:role/brindle-members-acme"
FED_AWS = AwsSetup(region="us-west-2", account_id="123456789012", role_name="brindle-members-acme",
                   federation=AwsFederation(FED_ROLE))
FED_GCP = GcpSetup("acme-brindle-prod", "us-east5", GcpFederation(
    "123456789", "brindle-members", "pawdelta", "bm@acme-brindle-prod.iam.gserviceaccount.com"))
FED_AZURE = AzureSetup(resource="acme-foundry",
                       federation=AzureFederation(SUB, "7c9e6679-7425-40de-944b-e07fc1f90ae7"))


def assumed(account):
    return {"arn": f"arn:aws:sts::{account}:assumed-role/brindle-members-acme/brindle-me",
            "issued_at": 1, "expires_at": 2}


@pytest.mark.parametrize("cached,level", [(assumed("123456789012"), doctor.OK),
                                          (assumed("999999999999"), doctor.WARN),
                                          (None, doctor.WARN)])
def test_federated_aws_identity_is_read_from_the_exchange_cache_without_the_cli(cached, level, monkeypatch):
    from brindle import cloud_credential

    fake = Fake(monkeypatch, missing={"aws"})
    claude = claude_setup("bedrock", auth="federated", aws=FED_AWS)
    policy(monkeypatch, claude)
    key = company_identity._launch_key(ORG, claude)
    if cached:
        cloud_credential._write(cloud_credential.org_dir(ORG, create=True) / cloud_credential.AWS_CACHE,
                                json.dumps(cached))
    assert (company_identity._launch_key(ORG, claude) != key) == bool(cached)   # the cache is fingerprinted
    c = by_name()["claude identity"]
    assert c.level == level and not fake.count("aws")
    expected = company_identity.expected_ident(claude)
    assert expected == "AWS account 123456789012, role brindle-members-acme"
    ident = company_identity.claude_identity(ORG, claude).ident
    assert company_identity.matches(expected, ident) == (level == doctor.OK)


@pytest.mark.parametrize("route,kw", [("vertex", {"gcp": FED_GCP}), ("foundry", {"azure": FED_AZURE})])
def test_federated_gcp_and_azure_identity_is_the_policys_and_matches_itself(route, kw, monkeypatch):
    fake = Fake(monkeypatch, missing={"gcloud", "az"})
    claude = claude_setup(route, auth="federated", **kw)
    ident = company_identity.claude_identity(ORG, claude)
    assert ident.determined and ident.problem is None and not fake.calls
    assert company_identity.matches(company_identity.expected_ident(claude), ident.ident)


