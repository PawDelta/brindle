"""`brindle doctor` shows, per installed provider CLI, how it's signed in
(its own login, an environment key, or signed out) and whether a quota limit
is in effect."""

import json
import os
import time

import pytest

from brindle import antigravity, doctor, providers, quota


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """claude, codex and agy all installed; no environment keys set, and no
    Codex rollouts to read quota from."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(providers, "claude_binary", lambda: "/bin/sh")
    monkeypatch.setattr(providers, "codex_binary", lambda: "/bin/sh")
    monkeypatch.setattr(antigravity, "binary", lambda: "/bin/sh")
    for keys in providers._ENV_AUTH.values():
        for k in keys:
            monkeypatch.delenv(k, raising=False)


def lines(repo_root=None):
    return {c.name: c for c in doctor.signin_checks(repo_root)}


SIGNED_IN = {"auth": (0, json.dumps({"loggedIn": True})), "login": (0, "Logged in using ChatGPT\n"),
             "models": (0, "Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\n")}


def test_doctor_names_profile_env_keys(installed, repo, monkeypatch):
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: SIGNED_IN.get(argv[1]))
    agents = repo / ".brindle" / "agents"
    agents.mkdir(parents=True)
    (agents / "keyed.md").write_text(
        "---\nname: keyed\nprovider: codex\nenv.CODEX_API_KEY: sk-profile-secret\n---\nWork.\n")
    (agents / "cleared.md").write_text(
        "---\nname: cleared\nprovider: claude\nenv.ANTHROPIC_API_KEY:\n---\nWork.\n")
    got = lines(str(repo))
    assert got["Codex sign-in"].detail.startswith(
        "its own login; profile keyed: environment key CODEX_API_KEY;")
    assert "profile" not in got["Claude Code sign-in"].detail   # an empty value clears the key
    assert "sk-profile-secret" not in doctor.render(doctor.signin_checks(str(repo)))


def test_bedrock_bearer_bypasses_unrelated_sso_login(installed, repo, monkeypatch):
    from brindle import cloud_login

    # This compares bearer and SSO routes, independent of the runner's cloud identity.
    for name in ("BRINDLE_CLOUD_ROUTE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SESSION_TOKEN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(name, raising=False)
    config = repo / "aws-config"
    config.write_text("[profile company]\nsso_session = company\n")
    env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "company", "AWS_CONFIG_FILE": str(config),
           "AWS_BEARER_TOKEN_BEDROCK": "FAKE_BEARER"}
    directory = repo / ".brindle" / "agents"
    directory.mkdir(parents=True)
    profile = directory / "bedrock.md"
    profile.write_text("---\nprovider: claude\n" +
                       "".join(f"env.{k}: {v}\n" for k, v in env.items()) + "---\nWork.\n")
    assert cloud_login.configured_status(env) is None
    assert cloud_login.login_command(env) is None
    assert doctor.cloud_checks(str(repo)) == []
    # A second profile sharing the config really does need its missing SSO login.
    env["AWS_BEARER_TOKEN_BEDROCK"] = ""
    (directory / "sso.md").write_text(profile.read_text().replace("FAKE_BEARER", ""))
    assert not cloud_login.configured_status(env).ok
    assert cloud_login.login_command(env)
    assert any(c.level == doctor.WARN for c in doctor.cloud_checks(str(repo)))


def test_doctor_shows_signed_out(installed, monkeypatch):
    replies = {"auth": (1, json.dumps({"loggedIn": False})), "login": (1, "Not logged in\n"),
               "models": (1, "Error: Please sign in to view available models. Launch the CLI "
                             "without arguments to sign in.\n")}
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: replies.get(argv[1]))
    got = lines()
    assert got["Claude Code sign-in"].level == doctor.FAIL
    assert got["Claude Code sign-in"].detail.startswith("signed out: ")
    assert "claude auth login" in got["Claude Code sign-in"].detail
    assert got["Codex sign-in"].level == doctor.WARN
    assert "codex login" in got["Codex sign-in"].detail
    assert got["Google Antigravity sign-in"].level == doctor.WARN
    assert "run `agy`" in got["Google Antigravity sign-in"].detail
    # and no credential line says all is well
    assert {c.name: c for c in doctor.credential_checks()}["claude"].level == doctor.WARN


def test_doctor_shows_quota_usage_and_not_twice(installed):
    quota.record("codex", [quota.Window(42, time.time() + 3600, 10080)])
    c = lines()["Codex sign-in"]
    assert c.level == doctor.OK
    assert "quota: Codex at 42% of its weekly limit" in c.detail
    # An installed CLI's quota is on its sign-in line, not a second one.
    assert not any(q.name == "codex quota" for q in doctor.quota_checks(None))


# -- which credential wins -----------------------------------------------------------------------

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE-0123456789abcdefXYZ"


def credentials():
    return {c.name: c for c in doctor.credential_checks()}


def test_doctor_names_an_exported_key_and_warns_next_to_a_subscription_login(installed, monkeypatch):
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: SIGNED_IN.get(argv[1]))
    assert credentials()["claude"].level == doctor.OK      # signed in, no key: all is well
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    c = credentials()["claude"]
    assert c.level == doctor.WARN
    assert c.detail.startswith("API key (ANTHROPIC_API_KEY, from environment)")
    assert "bills the key" in c.detail
    assert FAKE_KEY not in doctor.render(doctor.credential_checks())


def test_doctor_says_org_managed(installed, tmp_path, monkeypatch):
    path = tmp_path / "managed-settings.json"
    path.write_text(json.dumps({"apiKeyHelper": "/opt/bin/get-key"}))
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)   # the org's settings win over it
    c = credentials()["claude"]
    assert (c.level, c.detail) == (doctor.OK, "org-managed (apiKeyHelper)")


BLOCKED_DOCTOR = """
import sys, threading
import typer
from brindle import cli, disk, doctor, git

stuck = threading.Event()
echoed = []
real_echo = typer.echo
typer.echo = lambda msg="", **k: (echoed.append(str(msg)), real_echo(msg, **k))[1]
doctor.CHECK_SECONDS, doctor.GRACE_SECONDS = 0.3, 0.1
ok = lambda n: (lambda: [doctor.Check(doctor.OK, n, "fine")])

def hang(*a, **k):
    stuck.wait()

def inner_probe():
    # a check whose own worker threads block, as a stuck git probe would
    git.default_branch = lambda r: "main"
    git.out = lambda a, c: "a\tsha\t0"
    disk._in_use = lambda *a: set()
    disk.ancestors = lambda *a: frozenset()
    disk.merged = hang
    print("EARLIER:" + ",".join(echoed), file=sys.stderr, flush=True)
    disk.dead_branches(None, "r")
    return []

git.main_repo_root = hang
doctor._steps = lambda r, db: [("python", ok("python"), 5), ("first", ok("first"), 5),
                               ("stuck", inner_probe, 0.3), ("last", ok("last"), 5)]
doctor.disk_checks = lambda r, db: []
cli.doctor(fix=False)
"""


def test_doctor_prints_as_it_goes_and_exits_though_checks_and_discovery_block(tmp_path):
    """Run the real `brindle doctor` command with the repo lookup and one check's
    worker threads blocked forever: earlier results are already printed when
    the stuck check starts, each blocked part is reported, the rest still
    run, and the process exits."""
    import sys

    from brindle import spawn

    LIMIT = 60
    env = {**os.environ, "BRINDLE_HOME": str(tmp_path)}
    proc = spawn.read([sys.executable, "-c", BLOCKED_DOCTOR], env=env, timeout=LIMIT)
    assert proc.returncode == 0, proc.stderr
    assert "EARLIER:" in proc.stderr and "python" in proc.stderr.split("EARLIER:")[1]
    names = [line.split()[1] for line in proc.stdout.splitlines() if line and line[0] in "✓!"]
    assert names == ["python", "repository", "first", "stuck", "last"]
    assert proc.stdout.count("took too long") == 2
