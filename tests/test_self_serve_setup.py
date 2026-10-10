"""Self-serve company setup: detect, propose (default no), save, apply, label."""

import json

import pytest

from brindle import company_identity, company_login, config, doctor, providers, self_serve
from brindle.pro import team_policy
from brindle.pro.team_policy import AgentSetup, AwsSetup, ClaudeSetup

CLAUDE_ORG = "11111111-2222-3333-4444-555555555555"
SUB = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"

TEAM_STATUS = (0, json.dumps({"loggedIn": True, "orgId": CLAUDE_ORG, "orgName": "Acme Corp",
                              "subscriptionType": "enterprise"}))
PERSONAL_STATUS = (0, json.dumps({"loggedIn": True, "orgId": None, "orgName": None,
                                  "subscriptionType": "max"}))
AWS_OK = (0, json.dumps({"Account": "123456789012",
                         "Arn": "arn:aws:sts::123456789012:assumed-role/Dev/me"}))

AWS_CONFIG = """\
[sso-session corp]
sso_start_url = https://acme.awsapps.com/start
sso_region = us-east-1

[profile dev]
sso_session = corp
sso_account_id = 123456789012
sso_role_name = Dev
region = us-west-2

[profile broken]
region = us-west-2

[profile brindle-acme]
sso_start_url = https://other.awsapps.com/start
sso_region = us-east-1
sso_account_id = 999999999999
sso_role_name = Org
region = us-east-1
"""

ALLOWED = [
    ("claude", "auth", "status"),
    ("aws", "sts", "get-caller-identity"),
    ("gcloud", "config", "get-value"),
    ("az", "account", "show"),
    ("codex", "login", "status"),
]


class Fake:
    def __init__(self, monkeypatch, rc=None, missing=()):
        self.calls, self.rc, self.missing = [], rc or {}, set(missing)
        monkeypatch.setattr(company_login, "run_cmd", self.run)
        monkeypatch.setattr(company_login, "which", lambda n: None if n in self.missing else f"/bin/{n}")

    def run(self, argv, timeout=30.0, interactive=False, cwd=None, env=None, stdin=None):
        self.calls.append((tuple(argv), interactive, timeout))
        for prefix, result in self.rc.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return result
        return 1, ""

    def ran(self, *prefix):
        return any(c[0][:len(prefix)] == prefix for c in self.calls)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    company_identity.reset()
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_RESOURCE", raising=False)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: None)
    cfg = tmp_path / "aws-config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    return cfg


def answers(*given):
    seen = []
    it = iter(given)

    def ask(prompt):
        seen.append(prompt)
        return next(it, "")
    ask.seen = seen
    return ask


# -- detection -----------------------------------------------------------------------------

def test_detects_a_company_claude_plan(monkeypatch):
    Fake(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS})
    d = self_serve.detect()
    assert [c.claude.route for c in d.choices] == ["subscription"]
    assert d.choices[0].claude.org_id == CLAUDE_ORG
    assert d.choices[0].billed == "Claude: Acme Corp's enterprise plan"


def test_a_personal_plan_proposes_nothing(monkeypatch):
    Fake(monkeypatch, {("claude", "auth", "status"): PERSONAL_STATUS})
    d = self_serve.detect()
    assert d.choices == [] and any("max" in f for f in d.found)


def test_detects_aws_sso_profiles_and_whether_they_work(env, monkeypatch):
    env.write_text(AWS_CONFIG)
    fake = Fake(monkeypatch, {("aws", "sts", "get-caller-identity", "--profile", "dev"): AWS_OK})
    d = self_serve.detect()
    assert len(d.choices) == 1      # no half-profile, no brindle-* profile
    aws = d.choices[0].claude.aws
    assert (aws.account_id, aws.role_name, aws.region, aws.sso_region, aws.sso_start_url) == \
        ("123456789012", "Dev", "us-west-2", "us-east-1", "https://acme.awsapps.com/start")
    assert "AWS 1234…" not in d.choices[0].label and "1234…" in d.choices[0].label
    assert "123456789012" not in d.choices[0].label
    assert any("(signed in)" in f for f in d.found)
    assert fake.ran("aws", "sts", "get-caller-identity", "--profile", "dev")


def test_aws_profile_not_signed_in_is_reported(env, monkeypatch):
    env.write_text(AWS_CONFIG)
    Fake(monkeypatch)
    assert any("not signed in" in f for f in self_serve.detect().found)


def test_detects_the_active_gcloud_project(monkeypatch):
    fake = Fake(monkeypatch, {("gcloud", "config", "get-value", "project"): (0, "acme-prod\n"),
                              ("gcloud", "config", "get-value", "compute/region"): (0, "europe-west4\n")})
    d = self_serve.detect()
    c = d.choices[0].claude
    assert (c.route, c.gcp.project, c.gcp.region) == ("vertex", "acme-prod", "europe-west4")
    assert not fake.ran("gcloud", "auth")       # no token command is ever run


def test_gcloud_with_no_project_is_nothing(monkeypatch):
    Fake(monkeypatch, {("gcloud", "config", "get-value", "project"): (0, "(unset)\n")})
    assert self_serve.detect().choices == []


def test_detects_the_active_az_subscription(monkeypatch):
    Fake(monkeypatch, {("az", "account", "show"): (0, json.dumps({"id": SUB, "name": "Acme Prod"}))})
    d = self_serve.detect()
    assert any("Acme Prod" in f for f in d.found)
    assert d.choices == []      # the Foundry resource can't be detected...
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "acme-foundry")
    c = self_serve.detect().choices[0].claude
    assert (c.route, c.azure.subscription_id, c.azure.resource) == ("foundry", SUB, "acme-foundry")


def test_detects_codex_chatgpt_login(monkeypatch):
    Fake(monkeypatch, {("codex", "login", "status"): (0, "Logged in using ChatGPT\n")})
    d = self_serve.detect()
    assert d.codex == team_policy.CodexSetup("chatgpt")


def test_managed_claude_code_settings_are_used(monkeypatch):
    fake = Fake(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS})
    monkeypatch.setattr(providers, "claude_org_managed", lambda: "apiKeyHelper")
    out = []
    ask = answers("y")
    assert self_serve.offer(out.append, None, ask) is None
    assert "your IT already configures Claude Code; brindle will use it" in " ".join(out)
    assert ask.seen == [] and fake.calls == [] and self_serve.local_setup() is None


def test_missing_clis_are_skipped(monkeypatch, env):
    fake = Fake(monkeypatch, missing=("claude", "aws", "gcloud", "az", "codex"))
    env.write_text(AWS_CONFIG)
    d = self_serve.detect()
    assert fake.calls == [] and [c.claude.route for c in d.choices] == ["bedrock"]   # ~/.aws/config alone


def test_nothing_beyond_the_status_commands_is_run_or_read(env, monkeypatch, tmp_path):
    env.write_text(AWS_CONFIG)
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("aws_secret_access_key = SECRET")
    fake = Fake(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS,
                              ("gcloud", "config", "get-value", "project"): (0, "p\n"),
                              ("az", "account", "show"): (0, json.dumps({"id": SUB})),
                              ("codex", "login", "status"): (0, "ChatGPT")})
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    self_serve.detect()
    for argv, interactive, timeout in fake.calls:
        assert not interactive and timeout > 0
        assert any(argv[:len(a)] == a for a in ALLOWED), argv
    assert not any("credentials" in o or ".json" in o and "gcloud" in o for o in opened)


# -- the proposal --------------------------------------------------------------------------

def aws_world(env, monkeypatch):
    env.write_text(AWS_CONFIG)
    return Fake(monkeypatch, {("aws", "sts", "get-caller-identity", "--profile", "dev"): AWS_OK,
                              ("aws", "sts", "get-caller-identity", "--profile", "brindle-personal"): AWS_OK})


@pytest.mark.parametrize("reply", ["", "n", "no", "maybe", "0"])
def test_default_is_no_and_changes_nothing(reply, env, monkeypatch):
    fake = aws_world(env, monkeypatch)
    out = []
    assert self_serve.offer(out.append, None, answers(reply)) is None
    assert self_serve.local_setup() is None
    assert config.user_settings().get("agent_setup") is None
    assert not fake.ran("aws", "sso")
    assert not (env.read_text() != AWS_CONFIG)
    assert "nothing changed" in " ".join(out)


def test_the_proposal_names_the_choice(env, monkeypatch):
    aws_world(env, monkeypatch)
    ask = answers("")
    self_serve.offer(lambda m: None, None, ask)
    assert ask.seen == ["Use Bedrock (AWS account 1234…, role Dev, us-west-2)? [y/N] "]


def test_eof_and_no_tty_mean_no(env, monkeypatch):
    fake = aws_world(env, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert self_serve.tty_ask("?") == ""
    assert self_serve.run(lambda m: None) is None       # no ask, no tty: not even detected
    assert fake.calls == []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda p: (_ for _ in ()).throw(EOFError()))
    assert self_serve.tty_ask("?") == ""


def test_several_choices_pick_by_number(env, monkeypatch):
    aws_world(env, monkeypatch)
    fake = Fake(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS,
                              ("aws", "sts"): AWS_OK, ("gcloud", "config", "get-value", "project"): (0, "p\n")})
    out = []
    assert self_serve.offer(out.append, None, answers("")) is None
    text = "\n".join(out)
    assert "1. Use Claude on Acme Corp's enterprise plan" in text and "2. Use Bedrock" in text
    assert "0. Keep my personal setup" in text
    assert self_serve.local_setup() is None
    assert not fake.ran("claude", "auth", "login")


def test_yes_saves_applies_and_nudges(env, monkeypatch):
    fake = aws_world(env, monkeypatch)
    out = []
    assert self_serve.offer(out.append, None, answers("y")) is True
    saved = config.user_settings()["agent_setup"]
    assert saved["enforce"] is False and saved["claude"]["route"] == "bedrock"
    assert saved["claude"]["aws"]["account_id"] == "123456789012"
    assert "Ready: Claude on your bedrock (us-west-2)" in " ".join(out)
    assert out[-1] == ("Setting this up for your team? Your admin can do it once for everyone "
                       "with brindle Team: pawdelta.com/brindle#pricing")
    assert "[profile brindle-personal]" in env.read_text() and "[profile dev]" in env.read_text()
    env_stored = config.user_settings()["company_claude_env"]
    assert env_stored["self_serve"] is True and env_stored["env"]["AWS_PROFILE"] == "brindle-personal"
    assert fake.ran("aws", "sts", "get-caller-identity", "--profile", "brindle-personal")


def test_no_nudge_when_the_setup_isnt_ready(env, monkeypatch):
    aws_world(env, monkeypatch)
    out = []
    fake = Fake(monkeypatch, {("aws", "sts", "get-caller-identity", "--profile", "dev"): AWS_OK,
                              ("aws", "sts", "get-caller-identity", "--profile", "brindle-personal"): (1, ""),
                              ("aws", "sso", "login"): (1, "")})
    assert self_serve.offer(out.append, None, answers("y")) is False
    assert not any("Setting this up for your team" in m for m in out)


# -- the saved setup -----------------------------------------------------------------------

def test_saved_setup_round_trips_through_the_org_parser():
    setup = AgentSetup(
        claude=ClaudeSetup(route="bedrock", aws=AwsSetup("https://a.awsapps.com/start", "us-east-1",
                                                         "123456789012", "Dev", "us-west-2")),
        codex=team_policy.CodexSetup("chatgpt"), enforce=False)
    self_serve.save_local(setup)
    assert self_serve.local_setup() == setup
    assert team_policy._parse_agent_setup(config.user_settings()["agent_setup"]) == setup


def test_invalid_local_setup_is_ignored(caplog):
    config.set_user("agent_setup", {"claude": {"route": "bedrock", "aws": {"account_id": "x"}}})
    assert self_serve.local_setup() is None
    config.set_user("agent_setup", {"claude": {"route": "nonsense"}})
    assert self_serve.local_setup() is None


def test_enforce_never_applies_to_a_local_setup(monkeypatch):
    monkeypatch.setattr(team_policy, "_enforce_entitled", lambda: True)
    config.set_user("agent_setup", {"claude": {"route": "gateway",
                                               "gateway": {"base_url": "https://gw.example.com"}},
                                    "enforce": True})
    assert self_serve.local_setup().enforce is False
    assert team_policy._parse_agent_setup(config.user_settings()["agent_setup"]).enforce is True
    assert company_identity.launch_problem("claude") is None
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude"])
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    Fake(monkeypatch)
    assert company_identity.identities()[1] is False


# -- org setup wins over local ---------------------------------------------------------------

def org_world(monkeypatch, got):
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: got)


def test_org_setup_wins_over_the_local_one(env, monkeypatch):
    local = AgentSetup(claude=ClaudeSetup(route="gateway", gateway=team_policy.GatewaySetup("https://me.example.com")))
    self_serve.save_local(local)
    Fake(monkeypatch, {("claude", "auth", "status"): (0, "")})
    org = AgentSetup(claude=ClaudeSetup(route="gateway", gateway=team_policy.GatewaySetup("https://org.example.com")))
    org_world(monkeypatch, ("acme", org))
    out = []
    assert company_login.apply_current(out.append) is True
    stored = config.user_settings()["company_claude_env"]
    assert stored["env"]["ANTHROPIC_BASE_URL"] == "https://org.example.com"
    assert "self_serve" not in stored and stored["org"] == "acme"
    assert not any("Setting this up" in m for m in out)


def test_without_an_org_setup_the_local_one_applies_again(monkeypatch):
    local = AgentSetup(claude=ClaudeSetup(route="gateway", gateway=team_policy.GatewaySetup("https://me.example.com")))
    self_serve.save_local(local)
    Fake(monkeypatch)
    org_world(monkeypatch, None)
    assert company_login.apply_current(lambda m: None) is True
    assert company_login.stored_env()["ANTHROPIC_BASE_URL"] == "https://me.example.com"
    # and the org-less clear_env of an org's env leaves the person's own env alone
    company_login.clear_env()
    assert company_login.stored_env()["ANTHROPIC_BASE_URL"] == "https://me.example.com"


def test_a_failing_policy_for_a_signed_in_org_proposes_nothing(env, monkeypatch):
    fake = aws_world(env, monkeypatch)
    org_world(monkeypatch, company_login.UNAVAILABLE)
    monkeypatch.setattr(company_login, "_logged_in", lambda *a: True)
    ask = answers("y")
    assert company_login.apply_current(lambda m: None, ask=ask) is None
    assert ask.seen == [] and fake.calls == []


def test_apply_current_offers_when_the_org_has_no_setup(env, monkeypatch):
    aws_world(env, monkeypatch)
    org_world(monkeypatch, None)
    out = []
    assert company_login.apply_current(out.append, ask=answers("y")) is True
    assert self_serve.local_setup().claude.route == "bedrock"


def test_apply_current_offers_when_logged_out(env, monkeypatch):
    aws_world(env, monkeypatch)
    org_world(monkeypatch, company_login.UNAVAILABLE)
    monkeypatch.setattr(company_login, "_logged_in", lambda *a: False)
    assert company_login.apply_current(lambda m: None, ask=answers("y")) is True


def test_doctor_fix_shows_the_menu_before_the_prompt(env, monkeypatch, capsys):
    aws_world(env, monkeypatch)
    Fake(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS,
                       ("aws", "sts"): AWS_OK})
    org_world(monkeypatch, None)
    monkeypatch.setattr(doctor, "signin_providers", lambda: [])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)

    def tty(prompt):
        shown = capsys.readouterr().out
        assert "Found on this machine:" in shown and "1. Use Claude on" in shown
        assert "0. Keep my personal setup" in shown
        return "2"
    monkeypatch.setattr(self_serve, "tty_ask", tty)
    lines = doctor.fix(lambda p: False)
    assert any("Ready: Claude on your bedrock" in ln for ln in lines)
    assert not any("Found on this machine" in ln for ln in lines)       # not printed twice


def test_doctor_fix_without_a_tty_detects_nothing(env, monkeypatch):
    fake = aws_world(env, monkeypatch)
    org_world(monkeypatch, None)
    monkeypatch.setattr(doctor, "signin_providers", lambda: [])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert doctor.fix(lambda p: False) == ["nothing to fix"]
    assert fake.calls == []


def test_doctor_fix_adds_nothing_when_the_personal_setup_is_kept(env, monkeypatch):
    aws_world(env, monkeypatch)
    org_world(monkeypatch, None)
    monkeypatch.setattr(doctor, "signin_providers", lambda: [])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(self_serve, "tty_ask", lambda prompt: "")
    assert doctor.fix(lambda p: False) == ["nothing to fix"]
    assert self_serve.local_setup() is None


def test_removing_the_local_setup_drops_its_env(monkeypatch):
    local = AgentSetup(claude=ClaudeSetup(route="gateway", gateway=team_policy.GatewaySetup("https://me.example.com")))
    self_serve.save_local(local)
    Fake(monkeypatch)
    assert self_serve.run(lambda m: None) is True
    assert company_login.stored_env()
    config.set_user("agent_setup", None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert self_serve.run(lambda m: None) is None
    assert company_login.stored_env() == {}


@pytest.mark.parametrize("entry", ["account", "doctor"])
def test_login_and_doctor_fix_offer_it(entry, env, monkeypatch):
    aws_world(env, monkeypatch)
    org_world(monkeypatch, None)
    asked = answers("y")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(self_serve, "tty_ask", asked)
    monkeypatch.setattr(doctor, "signin_providers", lambda: [])
    if entry == "doctor":
        lines = doctor.fix(lambda p: False)
        assert any("Ready: Claude on your bedrock" in ln for ln in lines)
    else:
        from brindle.pro import account as pro_account

        class Ent:
            sub, org_id, plan, features = "me", "acme", "pro", frozenset()

        monkeypatch.setattr(pro_account.auth, "login", lambda *a, **k: Ent())
        monkeypatch.setattr(pro_account.auth, "me", lambda *a, **k: {})
        monkeypatch.setattr(pro_account.loopback, "can_open_browser", lambda out: False)
        monkeypatch.setattr(pro_account.ProAccount, "_client", lambda self, base=None: None, raising=False)
        assert pro_account.ProAccount(store=object()).run(["login"]) == 0
    assert len(asked.seen) == 1
    assert self_serve.local_setup().claude.route == "bedrock"


# -- doctor labels ---------------------------------------------------------------------------

def doctor_lines(monkeypatch, rc, local):
    self_serve.save_local(local)
    Fake(monkeypatch, rc)
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude"])
    return {c.name: c for c in company_identity.checks()}


def test_doctor_labels_a_self_serve_claude_plan(monkeypatch):
    local = AgentSetup(claude=ClaudeSetup(route="subscription", org_id=CLAUDE_ORG))
    c = doctor_lines(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS}, local)["claude identity"]
    assert c.level == doctor.OK
    assert "self-serve" in c.detail and "billed to Claude: Acme Corp's enterprise plan" in c.detail


def test_doctor_labels_a_self_serve_bedrock_account(monkeypatch):
    local = AgentSetup(claude=ClaudeSetup(route="bedrock", aws=AwsSetup(
        "https://a.awsapps.com/start", "us-east-1", "123456789012", "Dev", "us-west-2")))
    c = doctor_lines(monkeypatch, {("aws", "sts"): AWS_OK}, local)["claude identity"]
    assert "self-serve" in c.detail and "billed to Bedrock: AWS account 1234…" in c.detail


def test_doctor_does_not_label_an_org_setup(monkeypatch):
    self_serve.save_local(AgentSetup(claude=ClaudeSetup(route="gateway",
                                                         gateway=team_policy.GatewaySetup("https://x.example.com"))))
    Fake(monkeypatch, {("claude", "auth", "status"): TEAM_STATUS})
    org = AgentSetup(claude=ClaudeSetup(route="subscription", org_id=CLAUDE_ORG))
    monkeypatch.setattr(company_identity, "_setup", lambda: ("acme", org))
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude"])
    c = {c.name: c for c in company_identity.checks()}["claude identity"]
    assert "self-serve" not in c.detail
