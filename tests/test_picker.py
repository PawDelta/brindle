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
    (allow(), None, ["claude", "codex", "antigravity"], True),
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
    assert acc.clis() == clis
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


@pytest.mark.parametrize("opener, shown", [("wslview", {"BROWSER": "wslview"}), (None, None)])
def test_wsl_browser_sign_in_points_the_login_at_the_windows_browser(monkeypatch, opener, shown):
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("brindle.providers.windows_browser", lambda which=None: opener)
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["codex"])
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    seen = []
    assert picker.sign_in("codex", run=lambda argv, **kw: seen.append(kw.get("env")) or (0, ""),
                          confirm=lambda *a, **kw: True, echo=lambda s: None)
    assert (seen[0] and {"BROWSER": seen[0]["BROWSER"]}) == shown


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
    monkeypatch.setattr(picker, "sign_in", lambda p: asked.append(f"sign_in:{p}") or True)
    try:
        got = cli._resolve_supervisor(None, provider)
    except (typer.Exit, SystemExit):
        got = "refused"
    return got, asked, supervisor_pick()


@pytest.mark.parametrize("kw, got, asked, remembered", [
    (dict(provider="codex", signed_in=["codex"]), "codex", [], None),               # -s CLI is this session only
    (dict(provider="codex"), "codex", ["sign_in:codex"], None),                     # -s CLI not signed in: sign in first
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
    (["-s=codex"], "codex"),
    (["-s", "codex", "start", "--no-attach"], "codex"),
    (["-s", "codex", "supervisor", "--no-attach"], "codex"),
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


@pytest.mark.parametrize("wanted, default", [
    (None, "o"), ("claude", "o"), (picker.PICK, "o"),
    ("codex", "p"),                       # the running session is on another CLI
])
def test_running_session_on_another_cli_is_not_the_default(monkeypatch, wanted, default):
    monkeypatch.setattr(picker, "pick_row", lambda *a, **kw: ("enter", 0))
    live = SimpleNamespace(root=SimpleNamespace(id="a1", provider="claude"), goal="", branch="main",
                           age=60, workers=0)
    assert cli._ask_about_running([live], wanted) == (default, live)


def test_supervisor_flag_reaches_the_running_session_question(db, repo, monkeypatch):
    from brindle import sessions

    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))
    monkeypatch.setattr(sessions, "live", lambda db, root: [SimpleNamespace(
        root=SimpleNamespace(id="a1", provider="claude"))])
    asked = []

    def ask(live, wanted=None):
        asked.append(wanted)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_ask_about_running", ask)
    CliRunner().invoke(cli.app, ["-s", "codex"])
    assert asked == ["codex"]


@pytest.mark.parametrize("argv", [["-s", "gemini"], ["start", "-s", "gemini"], ["-c", "-s", "codex"]])
def test_supervisor_flag_that_cannot_be_honoured_fails_cleanly(db, repo, monkeypatch, argv):
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output


def _workers(acc, keys, **kw):
    from brindle import worker_models

    it = iter(keys)
    out = []
    picker.workers_screen(acc, key=lambda: next(it), echo=lambda s="", nl=True: out.append(s), **kw)
    return out, worker_models


def test_workers_screen_offers_only_allowed_models_and_sets_one_up(monkeypatch):
    from brindle import local_setup
    from brindle.config import user_settings

    acc = team_policy.Access(org_id=ORG, allowed_providers=("claude", "native"), deny_personal_keys=True)
    steps = iter([local_setup.SetupResult(ready=False, reason="needs 24 GB", fallback=True),
                  local_setup.SetupResult(ready=True)])
    signed = []
    out, wm = _workers(acc, ["1", "3", "1", "3", "2", "1", "q"], is_ready=lambda m: False,
                       local_setup=lambda model: next(steps),
                       cli_setup=lambda p: signed.append(p) or True)
    assert [m.id for m in wm.offered(acc, "medium")] == ["claude-sonnet", "claude-opus", "local-qwen3-coder"]
    text = "\n".join(out)
    assert "Codex" not in text and "OpenRouter" not in text and "✗ needs 24 GB" in text
    assert user_settings()["routing_light"] == "local-qwen3-coder"      # the failed setup saved nothing
    assert user_settings()["routing_medium"] == "claude-sonnet" and signed == ["claude"]


def test_workers_screen_checks_and_stores_a_hosted_key(monkeypatch):
    from brindle import key_check, keystore
    from brindle.config import user_profiles_dir, user_settings

    answers = iter(["bad", "good"])
    stored = {}
    monkeypatch.setattr(key_check, "verify", lambda name, value: "rejected" if value == "bad" else None)
    monkeypatch.setattr(keystore, "set_key", lambda name, value, repo_root=None: stored.update({name: value}))
    out, wm = _workers(team_policy.Access(), ["1", "5", "1", "5", "q"], is_ready=lambda m: False,
                       read_secret=lambda name: next(answers))
    assert "✗ rejected" in "\n".join(out) and stored == {"OPENROUTER_API_KEY": "good"}
    assert user_settings()["routing_light"] == "or-qwen3-coder"
    assert "api_key_env: OPENROUTER_API_KEY" in (user_profiles_dir() / "developer-or-qwen3-coder.md").read_text()


@pytest.mark.variant("the launch-time access re-check is the user-mandated guard that a saved worker pick the org no longer allows is never used")
@pytest.mark.parametrize("pick, signed_in, access, light", [
    ("codex", ["codex"], {}, ["developer-codex"]),
    ("codex", [], {}, None),
    ("codex", ["codex"], dict(allowed_providers=("claude",)), None),       # the org narrowed since the pick
    ("codex", ["codex"], dict(allowed_models=("claude-sonnet-4-6",)), None),  # a CLI whose profile names no model
    ("claude-haiku", ["claude"], dict(allowed_models=("claude-haiku-5-5",)), ["developer-light"]),  # the profile's haiku
    ("claude-sonnet", ["claude"], dict(allowed_models=("claude-sonnet-4-5",)), None),   # developer runs 4-6
    ("claude-sonnet", ["claude"], dict(allowed_profiles=("developer-light",)), None),
    ("local-qwen3-coder", [], {}, ["developer-local"]),                    # Ollama is probed once when ready
    ("local-qwen3-coder", [], {}, None),                                   # ...and again when it wasn't
])
def test_workers_launch_falls_back_when_a_pick_is_not_ready(db, repo, monkeypatch, capsys, pick, signed_in, access,
                                                            light):
    from brindle import local_setup, worker_models
    from brindle.config import DEFAULT_ROUTING, load_repo_config
    from brindle.native import serve

    for var in ("ANTHROPIC_MODEL", "CLAUDE_MODEL", "CODEX_MODEL", "OPENAI_MODEL", "MODEL"):
        monkeypatch.delenv(var, raising=False)
    probes, signin_probes = [], []
    monkeypatch.setattr(serve, "ollama_available", lambda: probes.append(1) or True)
    monkeypatch.setattr(local_setup, "verify_tags", lambda host, model: light is not None)
    monkeypatch.setattr(worker_models, "_warned", set())
    monkeypatch.setattr(worker_models, "_local_ready", {})
    monkeypatch.setattr(worker_models, "_signed_in", None, raising=False)
    monkeypatch.setattr(team_policy, "access", lambda root: team_policy.Access(org_id=ORG, **access))
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: signin_probes.append(1) or signed_in)
    set_user("routing_light", pick)
    load_repo_config(str(repo))
    cfg = load_repo_config(str(repo))
    assert cfg.routing["light"] == (light or DEFAULT_ROUTING["light"])
    assert ("isn't ready for light tasks" in capsys.readouterr().err) is (light is None)
    assert len(probes) == (0 if pick != "local-qwen3-coder" else 1 if light else 2)
    assert len(signin_probes) == 1
