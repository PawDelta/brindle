"""The OS sandbox for Claude Code and Antigravity workers (brindle.os_sandbox):
the boundary itself, executed on this machine with a synthetic credential; the
launches that require it, the one owner switch, and what refuses them."""

import json
import os
import time
from pathlib import Path

import pytest

from brindle import antigravity, os_sandbox, permissions, providers
from brindle.config import set_local, set_user
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import Antigravity, ClaudeCode, LaunchContext
from workspace_template import copy_workspace, workspace_template  # noqa: F401 (a fixture)

# conftest takes the sandbox as available for every other test; this file checks the check.
REAL_CAPABILITY = os_sandbox.capability_problem


@pytest.fixture
def ws(db, workspace_template, tmp_path, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return copy_workspace(workspace_template, db, tmp_path)


@pytest.fixture
def capable(monkeypatch):
    """A machine on which both CLIs' sandboxes can be required (conftest's stub)
    with an agy to launch."""
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])


@pytest.fixture
def store(tmp_path):
    """A synthetic credential store (never a real one): fakehome/.sshbox/id_fake."""
    folder = tmp_path / "fakehome" / ".sshbox"
    folder.mkdir(parents=True)
    (folder / "id_fake").write_text("FAKE-CANARY-NOT-A-REAL-KEY\n")
    return folder


def claude_settings(ws, mode="assign"):
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "go", cwd=ws.path, mode=mode))
    return json.loads(argv[argv.index("--settings") + 1])


# -- the boundary, executed -----------------------------------------------------------------------

VARIANTS = [
    (["/bin/cat", "{target}"], {}),
    pytest.param(["/bin/bash", "-c", 'cat "$CRED"'], {"CRED": "{target}"},
                 marks=pytest.mark.variant("bypass: the path reaches the shell only through an env var")),
    pytest.param(["python3", "-c", "import os; print(open(os.environ['CRED_DIR'] + '/id_fake').read())"],
                 {"CRED_DIR": "{store}"},
                 marks=pytest.mark.variant("bypass: Python builds the path from os.environ")),
    pytest.param(["/bin/bash", "-c", 'a=("$CRED_DIR" id_fake); case x in x) cat "${a[0]}/${a[1]}";; esac'],
                 {"CRED_DIR": "{store}"}, marks=pytest.mark.variant("bypass: case and array indirection")),
    pytest.param(["/bin/bash", "-c", 'shopt -s dotglob; cat "$FAKEHOME"/.*/id_fake'], {"FAKEHOME": "{fakehome}"},
                 marks=pytest.mark.variant("bypass: shopt dotglob reaches the dot store")),
    pytest.param(["/bin/sh", "-c", "sh -c 'sh -c \"cat $CRED\"'"], {"CRED": "{target}"},
                 marks=pytest.mark.variant("bypass: nested children inherit the boundary")),
    pytest.param(["/bin/cat", "{alias}"], {},
                 marks=pytest.mark.variant("bypass: a symlink outside the store resolves into it")),
]


@pytest.mark.parametrize("argv,env", VARIANTS)
def test_sandbox_denies_credential_reads(tmp_path, store, argv, env):
    """The real OS boundary on this machine (the primitive both CLIs use, with
    brindle's deny list): a read of the synthetic store fails however it is
    spelled, and an allowed build-file write and read beside it works. Run
    inside a sandbox already (a sandboxed worker's own test run) Seatbelt can't
    nest: that is reported, never passed; rerun it on an unsandboxed runner."""
    target = str(store / "id_fake")
    alias = tmp_path / "work" / "alias_link"
    alias.parent.mkdir()
    alias.symlink_to(target)
    names = {"{target}": target, "{store}": str(store), "{fakehome}": str(store.parent), "{alias}": str(alias)}

    def fill(text):
        for key, value in names.items():
            text = text.replace(key, value)
        return text

    argv = [fill(a) for a in argv]
    env = {k: fill(v) for k, v in env.items()}
    result = os_sandbox.probe([str(store)], target, str(tmp_path / "work" / "build.txt"), argv=argv, env=env)
    if result.nested:
        pytest.skip(f"NOT VERIFIED: this test run is itself sandboxed and Seatbelt doesn't nest ({result.detail}); "
                    "run tests/test_os_sandbox.py on an unsandboxed runner")
    assert result.denied, result.detail
    assert result.control, result.detail
    assert (tmp_path / "work" / "build.txt").read_text() == "brindle-os-sandbox-control"


# -- the launches ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("policy,switch,mode,sandboxed", [
    ("on", None, "assign", True),
    ("on", None, "interactive", False),      # a supervisor's own chat keeps its policy
    ("on", "off", "assign", False),          # the owner's switch, in their own config
    ("off", None, "assign", False),
])
def test_claude_worker_commands_run_in_the_sandbox(ws, capable, policy, switch, mode, sandboxed):
    from brindle.daemon import lifecycle

    set_local(ws.repo_root, "permission_policy", policy)
    if switch:
        set_user(os_sandbox.USER_KEY, switch)
    settings = claude_settings(ws, mode)
    assert ("sandbox" in settings) == sandboxed
    if not sandboxed:
        return
    box = settings["sandbox"]
    assert (box["enabled"], box["failIfUnavailable"], box["allowUnsandboxedCommands"],
            box["autoAllowBashIfSandboxed"], box["filesystem"]["disabled"]) == (True, True, False, False, False)
    denied = set(box["filesystem"]["denyRead"])
    home = os.path.realpath(os.environ["BRINDLE_HOME"])
    assert {os.path.expanduser("~/.ssh"), os.path.expanduser("~/.aws"), os.path.join(home, "daemon.json"),
            os.path.join(home, "brindle-keys")} <= denied
    assert denied <= set(box["filesystem"]["denyWrite"])      # a store can't be planted either
    assert os.path.join(home, "config.json") in box["filesystem"]["denyWrite"]   # nor this switch flipped
    # The CLI keeps its credentials; its commands don't get them (env scrubbing).
    scrubbed = {e["name"] for e in box["credentials"]["envVars"] if e["mode"] == "deny"}
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_SECRET_ACCESS_KEY"} <= scrubbed
    assert "CLAUDE_CODE_USE_BEDROCK" not in scrubbed
    # Daemon scope: of the Unix sockets only brindle's (git's sign-in), whose
    # token stays unreadable; the daemon's own guard scopes the routes.
    assert box["network"]["allowUnixSockets"] == [str(lifecycle.socket_path())]
    assert box["network"]["allowedDomains"] == ["*"]


@pytest.mark.parametrize("provider,failure,said", [
    ("claude", "platform", "no sandbox here"),
    ("claude", "version", "older than"),
    ("claude", "managed", "managed-settings.json"),
    ("antigravity", "platform", "no sandbox here"),
])
def test_an_unavailable_or_conflicting_backend_refuses_the_launch(ws, tmp_path, monkeypatch, provider, failure, said):
    """Required means fail closed: the launch is refused with what to repair
    and the off switch, never started with its commands unsandboxed."""
    from brindle import providers_install

    set_local(ws.repo_root, "permission_policy", "on")
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/sh")
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])
    monkeypatch.setattr(providers, "claude_binary", lambda: "/bin/sh")
    monkeypatch.setattr(os_sandbox, "capability_problem", REAL_CAPABILITY)
    monkeypatch.setattr(os_sandbox, "_platform_problem", lambda: None)
    monkeypatch.setattr(os_sandbox, "_probe_problem", lambda: None)
    monkeypatch.setattr(providers_install, "probe_version", lambda path: "9.9.9")
    os_sandbox.forget_capability()
    if failure == "platform":
        monkeypatch.setattr(os_sandbox, "_platform_problem", lambda: ("no sandbox here", "Install one"))
    elif failure == "version":
        monkeypatch.setattr(providers_install, "probe_version", lambda path: "2.0.0")
    else:
        managed = tmp_path / "managed-settings.json"
        managed.write_text(json.dumps({"sandbox": {"enabled": False}}))
        monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(managed))
    launch = ClaudeCode() if provider == "claude" else Antigravity()
    with pytest.raises(os_sandbox.OsSandboxRefused) as refused:
        launch.command(LaunchContext("abc", load_profile("developer"), "go", cwd=ws.path, mode="assign"))
    assert said in str(refused.value) and '"os_sandbox": "off"' in str(refused.value)
    assert any(level == "fail" and said in detail for level, detail in os_sandbox.status(ws.repo_root))


def test_agy_worker_is_sandboxed_and_may_not_run_outside_it(db, ws, capable, monkeypatch):
    """agy gets --sandbox and session settings denying every store; its own
    second run outside the sandbox (BypassSandbox) is denied by the hook, and a
    Gemini key agy ignores is kept out of the pane, since agy's sandbox
    leaves the command environment alone."""
    set_local(ws.repo_root, "permission_policy", "on")
    monkeypatch.setattr(providers, "agy_uses_gemini_key", lambda: False)
    argv = Antigravity().command(LaunchContext("g1", load_profile("developer"), None, cwd=ws.path, mode="assign"))
    assert "--sandbox" in argv and antigravity.SKIP_FLAG in argv
    session = json.loads(argv[argv.index("--settings") + 1])
    perms = session["permissions"]
    assert f"read_file({os.path.expanduser('~/.ssh')})" in perms["deny"]
    assert not any(e.startswith("command(") for e in perms["allow"])   # none lets a bypass through unasked
    db.add_agent(Agent("g1", ws.id, "developer", "antigravity", "boss", "assign", "processing", "@0", None, time.time()))

    def hook(bypass):
        payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": "git status", "BypassSandbox": bypass}},
                   "conversationId": "c", "workspacePaths": [ws.path]}
        return antigravity.pre_tool_decision(db, "g1", payload, strict=True)

    assert hook(True) == os_sandbox.bypass_denial()
    assert hook(False)["reason"] != os_sandbox.BYPASS_DENIED
    assert os_sandbox.agy_env_denies() == ("GEMINI_API_KEY",)
    monkeypatch.setattr(providers, "agy_uses_gemini_key", lambda: True)
    assert os_sandbox.agy_env_denies() == ()       # agy itself needs it then


@pytest.mark.parametrize("allow,sockets,apple", [
    (None, [], False),
    (["ssh_agent", {"docker": "{proxy}"}, "apple_events", "keychain"], ["{agent}", "{proxy}"], True),
    ({"host_tmux": True, "docker": True}, ["{tmux}", "/var/run/docker.sock"], False),
    pytest.param(["dockr"], None, False, marks=pytest.mark.variant("a misspelt allowance fails closed")),
])
def test_allowances_open_exactly_the_named_host_services(ws, capable, tmp_path, monkeypatch, allow, sockets, apple):
    """Each host service is its own switch in the person's config; nothing is
    open until named, and only Claude Code's socket allowlist enforces it."""
    from brindle import tmux
    from brindle.daemon import lifecycle

    set_local(ws.repo_root, "permission_policy", "on")
    agent = tmp_path / "agent.sock"
    names = {"{proxy}": str(tmp_path / "docker-proxy.sock"), "{agent}": str(agent),
             "{tmux}": os_sandbox.tmux_socket(tmux.current_server())}
    fill = lambda v: names.get(v, v) if isinstance(v, str) else v  # noqa: E731
    if isinstance(allow, list):
        allow = [{k: fill(v) for k, v in e.items()} if isinstance(e, dict) else e for e in allow]
    if allow is not None:
        set_user(os_sandbox.ALLOW_KEY, allow)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    if sockets is None:
        with pytest.raises(os_sandbox.OsSandboxRefused, match="dockr"):
            claude_settings(ws)
        return
    box = claude_settings(ws)["sandbox"]
    expected = [str(lifecycle.socket_path()), *(fill(s) for s in sockets)]
    opened = set(box["network"]["allowUnixSockets"])
    assert set(expected) <= opened <= {a for p in expected for a in os_sandbox._aliases(p)}
    assert box["allowAppleEvents"] is apple


def test_protected_paths_cover_provider_sign_ins_relocated_stores_and_aliases(tmp_path, monkeypatch):
    """credential_paths is the one deny source: the CLIs' own sign-ins are in
    it, a store moved by an environment variable joins, and a store that is a
    symlink is denied under both spellings (the sandbox matches the path used)."""
    real = tmp_path / "real-aws"
    real.mkdir()
    link = tmp_path / "home" / ".aws"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-elsewhere"))
    paths = os_sandbox.protected_paths()
    assert {str(link), str(real.resolve()), str(tmp_path / "gh-elsewhere")} <= set(paths)
    assert os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token") in paths
    assert any(p.endswith("/.credentials.json") for p in permissions.credential_paths())
    assert len(paths) == len(set(paths))
