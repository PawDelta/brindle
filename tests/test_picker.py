"""The supervisor picker, the access helper behind it, and how a launch
resolves which CLI the supervisor runs on."""
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from brindle import cli, doctor, picker, providers
from brindle.config import supervisor_pick, set_user
from brindle.policy import deny
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
    ({"allowed_providers": ["codex", "claude"], "allowed_models": ["gpt-5"]}, None, ["claude", "codex"], False),
    ({}, "exclusive", ["codex"], True),                       # the managed routing serves only Codex
    ({}, _unavailable, [], False),                            # can't read it: nothing
    pytest.param(deny("never fetched"), None, [], False,
                 marks=pytest.mark.variant("a denied organization policy cannot fall back to personal supervisor access")),
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
    # Claude Code's official installer is a pipe, so it runs through a shell.
    ran = []
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["claude"])
    picker.sign_in("claude", run=lambda argv, **kw: ran.append(argv) or (0, ""),
                   confirm=lambda *a, **kw: True, echo=lambda s: None)
    assert ran[0][:2] == ["sh", "-c"] and "|" in ran[0][2]


@pytest.mark.parametrize("opener, shown", [("wslview", {"BROWSER": "wslview"}), pytest.param(None, None,
    marks=pytest.mark.variant("WSL sign-in without a Windows opener must not pass a bogus browser override"))])
def test_wsl_browser_sign_in_points_the_login_at_the_windows_browser(monkeypatch, opener, shown):
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("brindle.providers.windows_browser", lambda which=None: opener)
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["codex"])
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    seen = []
    assert picker.sign_in("codex", run=lambda argv, **kw: seen.append(kw.get("env")) or (0, ""),
                          confirm=lambda *a, **kw: True, echo=lambda s: None)
    # The login env also carries brindle's config home (provider_homes): BROWSER is checked on its own.
    assert ({"BROWSER": seen[0]["BROWSER"]} if "BROWSER" in (seen[0] or {}) else None) == shown


def _resolve(monkeypatch, *, provider=None, pick=None, tty=True, signed_in=(), allowed=None, ci=False, off=None,
             typed=None):
    """``typed``: the real picker runs, answered with that; else a stand-in picks
    antigravity, set up as the real one sets up its choice."""
    from brindle import providers_install

    for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "TF_BUILD", "JENKINS_URL", "BRINDLE_CI"):
        monkeypatch.delenv(var, raising=False)
    if ci:
        monkeypatch.setenv("CI", "true")
    if pick:
        set_user("supervisor", pick)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: tty)))
    signed = list(signed_in)
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: list(signed))
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude", "codex", "antigravity", "opencode"])
    monkeypatch.setattr(providers, "unusable", lambda p, env=None: None if p in signed else f"{p} is signed out")
    monkeypatch.setattr(providers_install, "turned_off", lambda p: p == off)
    monkeypatch.setattr(team_policy, "access", lambda root: team_policy.Access(org_id=ORG, allowed_providers=allowed))
    asked = []
    if typed is None:
        monkeypatch.setattr(picker, "pick_supervisor", lambda acc, cur: asked.append(cur) or signed.append(
            "antigravity") or "antigravity")
    else:
        monkeypatch.setattr(typer, "prompt", lambda *a, **k: typed)
    monkeypatch.setattr(picker, "sign_in", lambda p: asked.append(f"sign_in:{p}") or signed.append(p) or True)
    try:
        got = cli._resolve_supervisor(None, provider)
    except (typer.Exit, SystemExit):
        got = "refused"
    return got, asked, supervisor_pick()


@pytest.mark.parametrize("kw, got, asked, remembered", [
    pytest.param(dict(provider="codex", signed_in=["codex"]), "codex", [], None,
                 marks=pytest.mark.variant("an explicit supervisor CLI choice must not replace the saved preference")),
    pytest.param(dict(provider="codex"), "codex", ["sign_in:codex"], None,
                 marks=pytest.mark.variant("an explicitly selected signed-out CLI must sign in before launch")),
    (dict(provider="codex", allowed=("claude",)), "refused", [], None),             # -s refuses what the org doesn't allow
    (dict(provider=picker.PICK), "antigravity", [None], "antigravity"),             # bare -s opens the picker
    (dict(provider=picker.PICK, tty=False), "refused", [], None),
    pytest.param(dict(pick="codex", signed_in=["codex"]), "codex", [], "codex",
                 marks=pytest.mark.variant("a usable saved preference must avoid unnecessary sign-in or repicking")),
    pytest.param(dict(pick="codex"), "antigravity", ["codex"], "antigravity",
                 marks=pytest.mark.variant("a saved but signed-out CLI cannot be launched without a new choice")),
    pytest.param(dict(pick="codex", signed_in=["codex"], allowed=("antigravity",)), "antigravity", ["codex"], "antigravity",
                 marks=pytest.mark.variant("a saved signed-in CLI must still respect a narrowed organization policy")),
    # No terminal: never ask; the default CLI, and with none usable nothing (not the profile's Claude).
    pytest.param(dict(tty=False, signed_in=["codex"]), "codex", [], None,
                 marks=pytest.mark.variant("a noninteractive launch must use an available signed-in CLI without prompting")),
    (dict(ci=True), "refused", [], None),
    pytest.param(dict(provider="codex", tty=False), "refused", [], None,
                 marks=pytest.mark.variant("an unusable explicit CLI must fail when no terminal can complete sign-in")),
    # The real picker never offers a CLI turned off in brindle (its second row is then Antigravity) ...
    (dict(off="codex", typed="2"), "antigravity", ["sign_in:antigravity"], "antigravity"),
    # ... and whatever a pick returns runs only if usable: never remembered otherwise.
    (dict(off="antigravity"), "refused", [None], None),
])
def test_supervisor_launch_resolution(monkeypatch, kw, got, asked, remembered):
    assert _resolve(monkeypatch, **kw) == (got, asked, remembered)


@pytest.mark.parametrize("argv, value", [
    (["-s"], picker.PICK),
    pytest.param(["start", "-s", "--no-attach"], picker.PICK,
                 marks=pytest.mark.variant("issue 196: a bare supervisor flag on start must not consume the next option")),
    pytest.param(["-s", "codex"], "codex",
                 marks=pytest.mark.variant("issue 196: the root command accepts an explicit supervisor CLI")),
    pytest.param(["-s=codex"], "codex",
                 marks=pytest.mark.variant("issue 196: the equals form of the supervisor flag must parse")),
    pytest.param(["-s", "codex", "start", "--no-attach"], "codex",
                 marks=pytest.mark.variant("issue 196: a root supervisor option must survive explicit start dispatch")),
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
    pytest.param(None, "o", marks=pytest.mark.variant("without an override, an existing session remains the default")),
    pytest.param("claude", "o", marks=pytest.mark.variant("selecting the existing session's CLI must preserve the resume default")),
    pytest.param(picker.PICK, "o", marks=pytest.mark.variant("a bare picker flag must not force a second session before choosing a CLI")),
    ("codex", "a"),                       # the running session is on another CLI: start alongside
])
def test_running_session_on_another_cli_is_not_the_default(monkeypatch, wanted, default):
    monkeypatch.setattr(picker, "pick_row", lambda *a, **kw: ("enter", 0))
    live = SimpleNamespace(root=SimpleNamespace(id="a1", provider="claude"), goal="", branch="main",
                           age=60, workers=0, workspace=SimpleNamespace(id="w1"))
    assert cli._ask_about_running([live], wanted) == (default, live)


def test_supervisor_flag_reaches_the_running_session_question(db, repo, monkeypatch):
    from brindle import sessions

    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))
    monkeypatch.setattr(sessions, "live", lambda db, root: [SimpleNamespace(
        root=SimpleNamespace(id="a1", provider="claude"))])
    asked = []

    def ask(live, wanted=None, **kw):
        asked.append(wanted)
        raise typer.Exit(0)

    monkeypatch.setattr(cli, "_ask_about_running", ask)
    CliRunner().invoke(cli.app, ["-s", "codex"])
    assert asked == ["codex"]


@pytest.mark.parametrize("argv", [["-s", "gemini"], pytest.param(["start", "-s", "gemini"],
    marks=pytest.mark.variant("explicit start must reject an unsupported supervisor CLI cleanly too")),
    ["-c", "-s", "codex"]])
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
