"""The supervisor picker, the access helper behind it, and how a launch
resolves which CLI the supervisor runs on."""
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from brindle import cli, doctor, picker
from brindle.config import supervisor_pick, set_user
from brindle.policy import allow, deny
from brindle.pro import license, managed_models, team_policy
from brindle.pro.team_policy import parse_policy

ORG = "acme"
CODEX_ONLY = {"enforce": True, "exclusive": True,
              "codex": {"route": "azure", "base_url": "https://acme.openai.azure.com/v1"}}


def _exclusive(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: True)
    return managed_models.Managed(ORG, None, True, setup=team_policy._parse_agent_setup(CODEX_ONLY))


def _unavailable(root):
    raise managed_models.ManagedUnavailable("couldn't read the org's managed-models policy")


@pytest.mark.parametrize("policy, managed, clis, model_ok", [
    (allow(), None, ["claude", "antigravity"], True),        # no team org: everything installed
    ({"allowed_providers": ["codex", "claude"], "allowed_models": ["gpt-5"]}, None, ["claude", "codex"], False),
    ({}, "exclusive", ["codex"], True),                       # the managed routing serves only Codex
    ({}, _unavailable, [], False),                            # can't read it: nothing
    (deny("never fetched"), None, [], False),
])
def test_supervisor_access_narrows_to_the_org(monkeypatch, policy, managed, clis, model_ok):
    p = policy if not isinstance(policy, dict) else parse_policy(ORG, {"version": 1, "policy": policy})
    monkeypatch.setattr(team_policy.ProPolicy, "policy", lambda self: p)
    m = _exclusive(monkeypatch) if managed == "exclusive" else managed
    monkeypatch.setattr(managed_models, "current", m if callable(m) else lambda root: m)
    acc = team_policy.access("/repo")
    assert acc.clis(installed=["claude", "antigravity"]) == clis
    assert acc.allows_model("claude-sonnet-4-5") is model_ok
    assert acc.deny_personal_keys is (managed == "exclusive")


def test_supervisor_picker_lists_only_allowed_clis_and_sets_up_one(monkeypatch):
    acc = team_policy.Access(org_id=ORG, allowed_providers=("claude", "codex"))
    out, set_up = [], []
    keys = iter(["9", "2"])           # out of range, then Codex
    got = picker.pick_supervisor(acc, "claude", signed_in=["claude"], installed=["claude"],
                                 key=lambda: next(keys), setup=lambda p: set_up.append(p) or True,
                                 echo=lambda s="", nl=True: out.append(s))
    assert got == "codex" and set_up == ["codex"]
    rows = picker.supervisor_rows(acc, "claude", ["claude"], ["claude"])
    assert [(r.provider, r.tags) for r in rows] == [("claude", ["current"]), ("codex", ["not installed"])]
    assert "antigravity" not in "\n".join(out)


def test_supervisor_sign_in_installs_opens_login_and_verifies(monkeypatch):
    ran = []
    monkeypatch.setattr(doctor, "signin_providers", lambda: [])
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    ok = picker.sign_in("codex", run=lambda argv, **kw: ran.append(argv[:2]) or (0, ""),
                        confirm=lambda *a, **kw: True, echo=lambda s: None)
    assert ok and ran == [["npm", "install"], [ran[1][0], "login"]]


def _resolve(monkeypatch, *, provider=None, pick=None, tty=True, signed_in=(), allowed=None, ci=False):
    for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "TF_BUILD", "JENKINS_URL", "BRINDLE_CI"):
        monkeypatch.delenv(var, raising=False)
    if ci:
        monkeypatch.setenv("CI", "true")
    if pick:
        set_user("supervisor", pick)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: tty)))
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: list(signed_in))
    monkeypatch.setattr(team_policy, "access", lambda root: team_policy.Access(org_id=ORG, allowed_providers=allowed))
    asked = []
    monkeypatch.setattr(picker, "pick_supervisor", lambda acc, cur: asked.append(cur) or "antigravity")
    try:
        got = cli._resolve_supervisor(None, provider)
    except (typer.Exit, SystemExit):
        got = "refused"
    return got, asked, supervisor_pick()


@pytest.mark.parametrize("kw, got, asked, remembered", [
    (dict(provider="codex"), "codex", [], "codex"),                                 # -s CLI is remembered
    (dict(provider="codex", allowed=("claude",)), "refused", [], None),             # -s refuses what the org doesn't allow
    (dict(provider=picker.PICK), "antigravity", [None], "antigravity"),             # bare -s opens the picker
    (dict(provider=picker.PICK, tty=False), "refused", [], None),
    (dict(pick="codex", signed_in=["codex"]), "codex", [], "codex"),                # usable pick: no question
    (dict(pick="codex"), "antigravity", ["codex"], "antigravity"),                  # pick not signed in: ask
    (dict(pick="codex", signed_in=["codex"], allowed=("antigravity",)), "antigravity", ["codex"], "antigravity"),
    (dict(), "antigravity", [None], "antigravity"),                                 # first launch: ask
    (dict(tty=False), None, [], None),                                              # no terminal: never ask
    (dict(ci=True), None, [], None),
    (dict(pick="codex", tty=False, allowed=("codex",)), "codex", [], "codex"),
    (dict(tty=False, allowed=("codex",)), "refused", [], None),                     # the profile's claude isn't allowed
])
def test_supervisor_launch_resolution(monkeypatch, kw, got, asked, remembered):
    assert _resolve(monkeypatch, **kw) == (got, asked, remembered)


@pytest.mark.parametrize("argv, value", [
    (["-s"], picker.PICK),
    (["start", "-s", "--no-attach"], picker.PICK),
    (["-s", "codex"], "codex"),
])
def test_supervisor_bare_flag_reaches_the_picker(db, repo, monkeypatch, argv, value):
    monkeypatch.chdir(repo)
    seen = []

    def stop(root, provider, **kw):
        seen.append(provider)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_resolve_supervisor", stop)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert seen == [value]
