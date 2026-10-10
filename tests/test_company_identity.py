"""Company identity: doctor lines, mismatch warnings, enforce at launch, --fix."""

from __future__ import annotations

import json
import pytest

from brindle import agents, antigravity, company_identity, company_login, doctor, providers, workspaces
from brindle.db import Agent
from brindle.pro.team_policy import (AgentSetup, AwsSetup, AzureSetup, ClaudeSetup, CodexSetup,
                                     GatewaySetup, GcpSetup)

ORG = "acme"
CLAUDE_ORG = "11111111-2222-3333-4444-555555555555"
SUB = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
AWS = AwsSetup("https://acme.awsapps.com/start", "us-east-1", "123456789012", "BrindleWorker", "us-west-2")
GCP = GcpSetup("acme-brindle-prod", "us-east5")
AZURE = AzureSetup(SUB, "acme-foundry")

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

def test_claude_org_line(monkeypatch):
    Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG))
    c = by_name()["claude identity"]
    assert c.level == doctor.OK
    assert "Acme" in c.detail and CLAUDE_ORG in c.detail and "enterprise" in c.detail


def test_aws_line_uses_the_stored_profile(monkeypatch):
    fake = Fake(monkeypatch, {("aws", "sts"): AWS_OK})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS))
    c = by_name()["claude identity"]
    assert c.level == doctor.OK
    assert "123456789012" in c.detail and "BrindleWorker" in c.detail and "us-west-2" in c.detail
    argv = next(a for a, _, _ in fake.calls if a[0] == "aws")
    assert argv[argv.index("--profile") + 1] == "brindle-acme"


def test_gcloud_project_line(monkeypatch):
    Fake(monkeypatch, {("gcloud", "config"): (0, "acme-brindle-prod\n")})
    policy(monkeypatch, claude_setup("vertex", gcp=GCP))
    c = by_name()["claude identity"]
    assert c.level == doctor.OK and "acme-brindle-prod" in c.detail


def test_azure_subscription_line(monkeypatch):
    Fake(monkeypatch, {("az", "account"): (0, json.dumps({"id": SUB, "name": "Acme Prod"}))})
    policy(monkeypatch, claude_setup("foundry", azure=AZURE))
    c = by_name()["claude identity"]
    assert c.level == doctor.OK and SUB in c.detail and "Acme Prod" in c.detail


def test_gateway_line_is_the_url_and_runs_nothing(monkeypatch):
    fake = Fake(monkeypatch)
    policy(monkeypatch, claude_setup("gateway", gateway=GatewaySetup("https://gw.example.com/v1")))
    c = by_name()["claude identity"]
    assert c.level == doctor.OK and "https://gw.example.com/v1" in c.detail
    assert not fake.count("claude")


def test_codex_and_antigravity_are_informational(monkeypatch):
    Fake(monkeypatch, {("codex", "login", "status"): (0, "Logged in using ChatGPT\n")})
    policy(monkeypatch, codex=CodexSetup("chatgpt"))
    found = by_name()
    assert found["codex identity"].level == doctor.OK
    assert "Logged in using ChatGPT" in found["codex identity"].detail
    assert "can't be checked" in found["codex identity"].detail
    assert found["antigravity identity"].level == doctor.OK
    assert "can't be checked" in found["antigravity identity"].detail


def test_no_policy_still_shows_the_claude_login(monkeypatch):
    Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    c = by_name()["claude identity"]
    assert c.level == doctor.OK and "Acme" in c.detail


def test_doctor_checks_include_the_identity_lines(monkeypatch, tmp_path):
    Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    names = [c.name for c in doctor.checks(None)]
    assert "claude identity" in names


def test_commands_have_timeouts_and_argv(monkeypatch):
    fake = Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG))
    company_identity.checks()
    assert fake.calls and all(isinstance(a, tuple) and t for a, _, t in fake.calls)


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


def test_other_aws_account_warns(monkeypatch):
    wrong = (0, json.dumps({"Account": "999999999999", "Arn": "arn:aws:sts::999999999999:assumed-role/X/me"}))
    Fake(monkeypatch, {("aws", "sts"): wrong})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and "999999999999" in c.detail and "aws sso login --profile brindle-acme" in c.detail


def test_other_gcp_project_warns(monkeypatch):
    Fake(monkeypatch, {("gcloud", "config"): (0, "personal-project\n")})
    policy(monkeypatch, claude_setup("vertex", gcp=GCP))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and "personal-project" in c.detail
    assert "gcloud config set project acme-brindle-prod" in c.detail


def test_other_azure_subscription_warns(monkeypatch):
    Fake(monkeypatch, {("az", "account"): (0, json.dumps({"id": "other-sub", "name": "Mine"}))})
    policy(monkeypatch, claude_setup("foundry", azure=AZURE))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and "other-sub" in c.detail and "az account set" in c.detail


def test_missing_cli_warns_without_a_mismatch(monkeypatch):
    Fake(monkeypatch, missing={"aws"})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS))
    c = by_name()["claude identity"]
    assert c.level == doctor.WARN and "isn't installed" in c.detail


# -- enforce at launch ---------------------------------------------------------------------

WRONG_AWS = {("aws", "sts"): (0, json.dumps({"Account": "999999999999", "Arn": "arn:x:y/R/s"}))}


def test_enforce_refuses_on_a_mismatch_with_the_fix(monkeypatch):
    Fake(monkeypatch, WRONG_AWS)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    why = company_identity.launch_problem("claude")
    assert why and "999999999999" in why and "brindle doctor --fix" in why


def test_without_enforce_a_mismatch_only_warns(monkeypatch):
    Fake(monkeypatch, WRONG_AWS)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=False)
    assert company_identity.launch_problem("claude") is None
    assert by_name()["claude identity"].level == doctor.WARN


def test_enforce_passes_a_matching_identity(monkeypatch):
    Fake(monkeypatch, {("aws", "sts"): AWS_OK})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude") is None


def test_undetermined_identity_never_blocks(monkeypatch):
    Fake(monkeypatch, missing={"aws"})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude") is None
    Fake(monkeypatch, {("aws", "sts"): (124, "timed out")})
    assert company_identity.launch_problem("claude") is None


def test_no_policy_or_unavailable_policy_never_blocks(monkeypatch):
    Fake(monkeypatch, WRONG_AWS)
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    assert company_identity.launch_problem("claude") is None


@pytest.mark.parametrize("enterprise", [True, False])
def test_enforce_blocks_only_with_the_enterprise_entitlement(enterprise, monkeypatch):
    from brindle.pro import license, team_policy

    Fake(monkeypatch, WRONG_AWS)
    monkeypatch.setattr(license, "has", lambda feature: enterprise)
    raw = {"claude": {"route": "bedrock",
                      "aws": {"sso_start_url": AWS.sso_start_url, "sso_region": AWS.sso_region,
                              "account_id": AWS.account_id, "role_name": AWS.role_name,
                              "region": AWS.region}},
           "enforce": True}
    setup = team_policy._parse_agent_setup(raw)
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, setup))
    assert setup.enforce is enterprise
    assert bool(company_identity.launch_problem("claude")) is enterprise


def test_a_second_launch_within_five_minutes_runs_no_probe(monkeypatch, launch_cache):
    clock, _ = launch_cache
    fake = Fake(monkeypatch, {("aws", "sts"): AWS_OK})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude") is None
    clock[0] += 299
    assert company_identity.launch_problem("claude") is None
    assert fake.count("aws", "sts") == 1


def test_a_probe_runs_again_after_five_minutes(monkeypatch, launch_cache):
    clock, _ = launch_cache
    fake = Fake(monkeypatch, {("aws", "sts"): AWS_OK})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    company_identity.launch_problem("claude")
    clock[0] += 301
    assert company_identity.launch_problem("claude") is None
    assert fake.count("aws", "sts") == 2


def test_the_launch_cache_is_a_private_file_with_the_timestamp(monkeypatch, launch_cache):
    import os
    import stat

    clock, path = launch_cache
    Fake(monkeypatch, {("aws", "sts"): AWS_OK})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    company_identity.launch_problem("claude")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    [(key, at)] = json.loads(path.read_text()).items()
    assert key.startswith("claude|bedrock|brindle-acme|") and at == clock[0]


def test_a_mismatch_is_never_cached_and_is_probed_again(monkeypatch, launch_cache):
    _, path = launch_cache
    fake = Fake(monkeypatch, WRONG_AWS)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude")
    assert company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 2
    assert not path.exists()


@pytest.mark.parametrize("answer", [{("aws", "sts"): (124, "timed out")}, {}])
def test_an_undetermined_result_is_never_cached(answer, monkeypatch, launch_cache):
    _, path = launch_cache
    fake = Fake(monkeypatch, answer, missing=() if answer else {"aws"})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude") is None
    assert company_identity.launch_problem("claude") is None
    assert not path.exists()
    if answer:
        assert fake.count("aws", "sts") == 2


def test_doctor_ignores_the_launch_cache(monkeypatch, launch_cache):
    fake = Fake(monkeypatch, {("aws", "sts"): AWS_OK})
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    assert company_identity.launch_problem("claude") is None
    assert fake.count("aws", "sts") == 1
    assert by_name()["claude identity"].level == doctor.OK
    assert fake.count("aws", "sts") == 2


PERSONAL = (0, json.dumps({"loggedIn": True, "subscriptionType": "max"}))


@pytest.mark.parametrize("route", ["subscription", "console"])
def test_claude_login_probes_every_launch_and_catches_a_switch(route, monkeypatch, launch_cache):
    _, path = launch_cache
    fake = Fake(monkeypatch, {("claude", "auth", "status"): CLAUDE_OK})
    policy(monkeypatch, claude_setup(route, org_id=CLAUDE_ORG), enforce=True)
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


def bump(path):
    import os

    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_a_cloud_route_reuses_the_cache_when_nothing_changed(monkeypatch, tmp_path):
    touch(tmp_path / "home" / ".aws" / "sso" / "cache" / "a.json")
    fake = cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 1


def test_aws_sso_cache_change_reprobes(monkeypatch, tmp_path):
    f = touch(tmp_path / "home" / ".aws" / "sso" / "cache" / "a.json")
    fake = cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    bump(f)
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 2
    touch(tmp_path / "home" / ".aws" / "sso" / "cache" / "new.json")
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 3


def test_aws_profile_change_reprobes(monkeypatch):
    fake = cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    monkeypatch.setenv("AWS_PROFILE", "personal")
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 2


@pytest.mark.parametrize("name,value", [
    ("AWS_SESSION_TOKEN", "tok"), ("AWS_SECRET_ACCESS_KEY", "s"), ("AWS_WEB_IDENTITY_TOKEN_FILE", "/t"),
    ("AWS_DEFAULT_PROFILE", "personal"), ("AWS_REGION", "eu-west-1")])
def test_aws_credential_env_vars_reprobe_without_being_stored(name, value, monkeypatch):
    monkeypatch.delenv(name, raising=False)
    fake = cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    monkeypatch.setenv(name, value)
    company_identity.launch_problem("claude")
    assert fake.count("aws", "sts") == 2
    if name.endswith(("TOKEN", "KEY")):      # a secret: only that it is set, never its value
        a = company_identity._fingerprint("bedrock")
        monkeypatch.setenv(name, "another-secret")
        assert company_identity._fingerprint("bedrock") == a


@pytest.mark.parametrize("route,name", [("vertex", "GOOGLE_APPLICATION_CREDENTIALS"),
                                        ("foundry", "AZURE_CLIENT_ID")])
def test_other_clouds_credential_env_vars_reprobe(route, name, monkeypatch):
    monkeypatch.delenv(name, raising=False)
    fake = cloud_cache_case(monkeypatch, route, **{"vertex": {"gcp": GCP}, "foundry": {"azure": AZURE}}[route])
    company_identity.launch_problem("claude")
    monkeypatch.setenv(name, "x")
    company_identity.launch_problem("claude")
    assert fake.count(*(("gcloud", "config") if route == "vertex" else ("az", "account"))) == 2


def test_gcloud_active_config_change_reprobes(monkeypatch, tmp_path):
    cfg = tmp_path / "gcloud"
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(cfg))
    f = touch(cfg / "active_config", "default")
    fake = cloud_cache_case(monkeypatch, "vertex", gcp=GCP)
    company_identity.launch_problem("claude")
    company_identity.launch_problem("claude")
    assert fake.count("gcloud", "config") == 1
    bump(f)
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


def test_the_cache_temp_file_is_per_process(monkeypatch, tmp_path, launch_cache):
    import os

    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append(str(a)), real(a, b))[1])
    cloud_cache_case(monkeypatch, "bedrock", aws=AWS)
    company_identity.launch_problem("claude")
    assert seen and f".{os.getpid()}." in seen[0]


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


# -- doctor --fix --------------------------------------------------------------------------

def test_fix_runs_company_login(monkeypatch):
    called = []
    monkeypatch.setattr(company_login, "apply_current", lambda say, *a, **k: called.append(say))
    doctor.fix(lambda prompt: False)
    assert called


# -- what is checked is what runs ----------------------------------------------------------

@pytest.mark.parametrize("current", [
    "claude org Me (org-1) (org-9)",    # a personal org named after the company's id
    "claude org x org-1 (org-9)",
    "claude org none",
    "claude org org-9",
])
def test_an_org_name_never_stands_in_for_the_org_id(current):
    assert not company_identity.matches("org:org-1", current)


def test_the_org_id_matches_with_or_without_a_name():
    assert company_identity.matches("org:ORG-1", "claude org Acme (org-1)")
    assert company_identity.matches("org:org-1", "claude org org-1")


def test_a_login_without_an_org_id_has_no_org(monkeypatch):
    Fake(monkeypatch, {("claude", "auth", "status"): (0, json.dumps(
        {"loggedIn": True, "orgName": f"Me ({CLAUDE_ORG})", "subscriptionType": "pro"}))})
    policy(monkeypatch, claude_setup("subscription", org_id=CLAUDE_ORG))
    ident = company_identity.claude_identity(ORG, claude_setup("subscription", org_id=CLAUDE_ORG))
    assert ident.ident == "claude org none"
    assert not company_identity.matches(f"org:{CLAUDE_ORG}", ident.ident)


def _env_of(db, repo, monkeypatch, profile_env):
    from types import SimpleNamespace

    monkeypatch.setattr(agents, "load_profile",
                        lambda name, root=None: SimpleNamespace(env=profile_env, tool_search=None))
    ws = workspaces.create(db, str(repo), "enf").workspace
    a = Agent("e1", ws.id, "developer", "claude", None, "assign", "starting", "", None, 0.0)
    db.add_agent(a)
    return agents.agent_env(ws, "e1", a, SimpleNamespace(config=None, denied_keys=()))


PROFILE_LINES = {"AWS_PROFILE": "personal", "AWS_ACCESS_KEY_ID": "AKIAPERSONAL", "ANTHROPIC_BASE_URL": "https://x",
            "CLAUDE_CONFIG_DIR": "/home/me/.claude-personal", "EDITOR": "vi"}


def test_enforced_route_beats_a_profiles_env_lines(db, repo, monkeypatch):
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    env = _env_of(db, repo, monkeypatch, PROFILE_LINES)
    assert env["AWS_PROFILE"] == company_login.profile_name(ORG)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    for name in ("AWS_ACCESS_KEY_ID", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"):
        assert name not in env
    assert env["EDITOR"] == "vi"


def test_without_enforce_a_profiles_env_lines_still_apply(db, repo, monkeypatch):
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=False)
    env = _env_of(db, repo, monkeypatch, PROFILE_LINES)
    assert env["AWS_PROFILE"] == "personal" and env["AWS_ACCESS_KEY_ID"] == "AKIAPERSONAL"


def test_enforced_route_takes_inherited_and_stored_credentials_out_of_the_pane(monkeypatch):
    from brindle import pane_auth

    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=True)
    deny = pane_auth.deny_names("claude", "auto", None)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_VERTEX"):
        assert name in deny
    assert "AWS_PROFILE" not in deny and "CLAUDE_CODE_USE_BEDROCK" not in deny   # the route's own
    assert "AWS_ACCESS_KEY_ID" not in pane_auth.deny_names("codex", "auto", None)
    policy(monkeypatch, claude_setup("bedrock", aws=AWS), enforce=False)
    assert "AWS_ACCESS_KEY_ID" not in pane_auth.deny_names("claude", "auto", None)
