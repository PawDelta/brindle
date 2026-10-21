import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from brindle.db import DB


def full_text(db, agent_id, body):
    """A supervisor is pushed a one-line pointer; this gives the full message it names."""
    from brindle import agents

    marker = "read_messages(message_id="
    if marker not in body:
        return body
    return agents.full_message(db, agent_id, int(body.rsplit(marker, 1)[1].rstrip(")")))


def sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(
        cmd, shell=True, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _reap_dead_test_servers() -> None:
    """Servers (and socket files) of earlier runs that died before their
    teardown, e.g. killed by a command timeout."""
    from brindle import procs, tmux

    for name in tmux.other_servers("brindle-test-"):
        m = re.fullmatch(r"brindle-test-(\d+)(?:[-.].*)?", name)
        if m and not procs.alive(int(m.group(1))):
            tmux.reap_server(name)


def _socket_names() -> set[str]:
    from brindle import tmux

    try:
        return {p.name for p in tmux.socket_dir().iterdir()}
    except OSError:
        return set()


# Every socket seen in tmux's socket directory so far: at the start of the
# run, then after each test. A test's leftovers are what is new since.
_KNOWN_SOCKETS: set[str] = set()


def _reap_run_leftovers(known: set[str]) -> list[str]:
    """Stop and remove the tmux servers and sockets a test left behind: any
    ``brindle-*`` socket that wasn't there before and is dead, or is a test
    server's (``brindle-test-*`` of a run that is over, ``brindle-e2e-*``).
    A live server of another kind (a demo recording's, say) and another
    run's live ``brindle-test-<pid>`` are left alone. Returns the names."""
    from brindle import procs, tmux

    found = []
    for name in sorted(_socket_names() - known):
        if not name.startswith("brindle-"):
            continue
        # A test server is named after the pytest process that made it
        # (``brindle-test-<pid>``, ``-other``, tmux's ``.lock`` file while it
        # starts, ``brindle-e2e-<what>-<pid>``). Another live run's is never ours to
        # reap, or to warn about: concurrent runs (brindle workers) share the
        # socket directory, and these appear there at any moment.
        m = (re.fullmatch(r"brindle-test-(\d+)(?:[-.].*)?", name)
             or re.fullmatch(r"brindle-e2e-.*-(\d+)", name))
        if m and int(m.group(1)) != os.getpid() and procs.alive(int(m.group(1))):
            continue
        try:
            listening = tmux.server_homes(name) is not None
        except tmux.TmuxError:
            continue  # not answering: nothing to be done with it from here
        if listening and not name.startswith(("brindle-test-", "brindle-e2e-")):
            continue
        try:
            tmux.reap_server(name)
        except tmux.TmuxError:
            continue
        found.append(name)
    return found


# The tmux server socket of the test running now (see test_tmux_server).
_TEST_SERVER: list[str] = []
_TEST_COUNT = [0]


@pytest.fixture(autouse=True)
def test_tmux_server(monkeypatch):
    """Each test gets its own tmux server (``brindle-test-<pid>-<n>``), not
    one shared by every test on the worker. A detached helper an earlier test
    started (``_launch-health``, ``_warm-checks``, ...) keeps that test's
    BRINDLE_TMUX_SOCKET, so it can only reach that test's dead server, never
    the one a later test is using. A shared server is the suspected route for
    a leftover helper's tmux commands, or the previous test's teardown, to
    take the server out from under a later test between its new-session and
    new-window (#199); the failure was not reproduced."""
    _TEST_COUNT[0] += 1
    name = f"brindle-test-{os.getpid()}-{_TEST_COUNT[0]}"
    _TEST_SERVER[:] = [name]
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", name)


@pytest.fixture(scope="session", autouse=True)
def private_tmux_server():
    """Run every test's tmux sessions on a private server, so parallel test
    runs (e.g. two brindle workers testing at once) can't collide. Nothing of
    it survives the run: a watchdog stops the server and removes its socket
    once this process is gone, even if it was killed before teardown."""
    import os

    from brindle import tmux

    _reap_dead_test_servers()
    _KNOWN_SOCKETS.update(_socket_names())
    old = os.environ.get("BRINDLE_TMUX_SOCKET")
    name = f"brindle-test-{os.getpid()}"
    os.environ["BRINDLE_TMUX_SOCKET"] = name
    watchdog = subprocess.Popen(
        ["/bin/sh", "-c",
         'while kill -0 "$1" 2>/dev/null; do sleep 1; done; '
         'for s in "$3" "$3"-* "$3".*; do tmux -L "${s##*/}" kill-server 2>/dev/null; rm -f "$s"; done',
         "brindle-test-watchdog", str(os.getpid()), name, str(tmux.socket_dir() / name)],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield
    try:
        tmux.kill_server()
    except tmux.TmuxError:
        pass
    watchdog.kill()
    watchdog.wait()
    if old is None:
        os.environ.pop("BRINDLE_TMUX_SOCKET", None)
    else:
        os.environ["BRINDLE_TMUX_SOCKET"] = old


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Each test's sessions end with it, so none outlive the test that made
    them (or leak into the next one), and a private server or socket a test
    made and forgot is stopped and removed, with a warning naming the test.
    Runs after every fixture's teardown, so a test's monkeypatching (of
    subprocess, say) is undone by then."""
    from brindle import tmux

    yield
    while _TEST_SERVER:
        try:
            tmux.reap_server(_TEST_SERVER.pop())
        except tmux.TmuxError:
            pass
    leaked = _reap_run_leftovers(_KNOWN_SOCKETS)
    if leaked:
        item.warn(pytest.PytestWarning(
            f"{item.nodeid} left tmux server(s) or socket(s) behind, now removed: {', '.join(leaked)}"))
    _KNOWN_SOCKETS.update(_socket_names())


@pytest.fixture(autouse=True)
def no_real_agent_cli(tmp_path, monkeypatch):
    """A test must never start the real claude/codex, in a tmux pane (it would
    outlive the run as an orphan) or as a subprocess: any process launch whose
    argv names one that doesn't resolve under the test's tmp_path raises. A test
    that needs a live agent pane uses the fake_agent_cli fixture."""
    from brindle import providers, tmux

    # A test's env must not point at a real binary unless it sets that itself.
    monkeypatch.delenv("BRINDLE_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("BRINDLE_CODEX_BIN", raising=False)
    root = tmp_path.resolve()

    def executable(argv):
        """The program an argv runs, past env's -u VAR, KEY=VAL and --."""
        i = 0
        if argv and os.path.basename(argv[0]) == "env":
            i = 1
            while i < len(argv):
                if argv[i] == "-u":
                    i += 2
                elif argv[i] == "--" or "=" in argv[i]:
                    i += 1
                else:
                    break
        return argv[i] if i < len(argv) else ""

    def refuse(exe, argv):
        found = shutil.which(exe) or exe
        if not Path(found).resolve().is_relative_to(root):
            raise AssertionError(
                f"{os.environ.get('PYTEST_CURRENT_TEST')} would launch the real {exe!r}: {argv[:6]}; "
                "use the fake_agent_cli fixture")

    def check(args):
        argv = [os.fspath(a) for a in ([args] if isinstance(args, (str, os.PathLike)) else args)]
        exe = executable(argv)
        overrides = (os.environ.get("BRINDLE_CLAUDE_BIN"), os.environ.get("BRINDLE_CODEX_BIN"))
        if os.path.basename(exe) in ("claude", "codex") or exe in overrides:
            refuse(exe, argv)
        # The headless runner is a child process that resolves the binary itself.
        # Only the binary of the provider the launch uses matters: a test that
        # fakes one provider's leaves the other's unused.
        if "_headless" in argv:
            i = argv.index("_headless") + 1
            agent = None
            try:
                from brindle.db import DB
                agent = DB().get_agent(argv[i]) if i < len(argv) else None
            except Exception:
                pass  # no such database: assume either provider
            by_provider = {"claude": (os.environ.get("BRINDLE_CLAUDE_BIN"), providers.claude_binary),
                           "codex": (os.environ.get("BRINDLE_CODEX_BIN"), providers.codex_binary)}
            used = [by_provider[agent.provider]] if agent and agent.provider in by_provider \
                else list(by_provider.values())
            binaries = [override for override, _ in used if override] or [find() for _, find in used]
            for binary in binaries:
                refuse(binary, argv)

    real_init, real_window = subprocess.Popen.__init__, tmux.new_window

    def guarded_init(self, args, *a, **kw):
        check(args)
        real_init(self, args, *a, **kw)

    # tmux windows run their command from a script, so its argv is checked here.
    def guarded_window(session, name, cwd, command, *a, **kw):
        check(command)
        return real_window(session, name, cwd, command, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    monkeypatch.setattr(tmux, "new_window", guarded_window)


@pytest.fixture
def fake_agent_cli(tmp_path, monkeypatch):
    """BRINDLE_CLAUDE_BIN and BRINDLE_CODEX_BIN point at a stub that draws an
    idle input box (Claude Code's, then Codex's) and sleeps, for tests that
    need an agent's pane to be live; the private tmux server's teardown kills
    it. Without the box each launch's after_launch waited out its 30 seconds,
    and tests that spawn twice took a minute. Echo is off so a pasted message
    doesn't read as a draft left in the box."""
    stub = tmp_path / "agent-cli-stub"
    stub.write_text("#!/bin/sh\nstty -echo 2>/dev/null\n"
                    "printf '❯ \\n? for shortcuts\\n› \\n  100%% context left\\n'\nexec sleep 600\n")
    stub.chmod(0o755)
    monkeypatch.setenv("BRINDLE_CLAUDE_BIN", str(stub))
    monkeypatch.setenv("BRINDLE_CODEX_BIN", str(stub))


@pytest.fixture(autouse=True)
def no_signed_in_providers(monkeypatch):
    """No sign-in probes in tests, and no `providers` param on /entitlement
    (tests/test_entitlement_providers.py sets its own)."""
    from brindle import doctor

    monkeypatch.setattr(doctor, "signed_in_providers", lambda: [])
    # And no `<cli> --version` run of whatever claude/codex the machine has.
    monkeypatch.setattr(doctor, "_version", lambda argv: "")


@pytest.fixture(autouse=True)
def no_claude_usage_command(monkeypatch):
    """The sidebar never runs the real `claude -p /usage`: it reads as just
    asked (a test calls refresh_claude_usage(force=True) and fakes the run)."""
    from brindle import quota

    monkeypatch.setattr(quota, "_claude_usage_at", float("inf"))


@pytest.fixture(autouse=True)
def no_signin_pause(monkeypatch):
    """The cull sweep and agent launches don't ask the real CLIs who is signed
    in: every provider reads as unverifiable (tests/test_signin_pause.py
    brings the real check back and fakes the probes under it)."""
    from brindle import signin_pause

    signin_pause.reset()
    monkeypatch.setattr(signin_pause, "check", lambda provider, cache=False: None)


@pytest.fixture(autouse=True)
def no_launch_catalog_network(monkeypatch):
    """Integration tests never discover real model accounts or endpoints.
    Catalog protocol tests instantiate their own checker; integration tests
    that exercise decisions replace agents' checker explicitly."""
    from brindle import agents, model_availability as ma

    def unknown(request, timeout):
        raise ma._DiscoveryError("unsupported")

    monkeypatch.setattr(agents, "_model_checker", ma.ModelAvailabilityChecker(discover=unknown))


@pytest.fixture(autouse=True)
def provider_http(monkeypatch):
    """No network for `brindle keys set`: the provider answers 200 unless a test
    changes ``status``/``body``; ``calls`` lists the URLs asked."""
    from brindle import key_check

    reply = {"status": 200, "body": {}, "calls": []}

    def fake_get(url, headers):
        reply["calls"].append(url)
        return reply["status"], reply["body"]

    monkeypatch.setattr(key_check, "_get", fake_get)
    return reply


@pytest.fixture(autouse=True)
def brindle_home(tmp_path, monkeypatch):
    home = tmp_path / "brindle-home"
    monkeypatch.setenv("BRINDLE_HOME", str(home))
    # Never touch the real ~/.claude.json (providers.trust_folder writes there).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    # ... nor agy's real settings.
    monkeypatch.setenv("BRINDLE_AGY_SETTINGS", str(tmp_path / "agy-home" / "settings.json"))
    # The shell provider runs $SHELL. The person's own shell reads their dotfiles,
    # so a slow or stuck one (a stale pyenv rehash lock waits 60s) would fail
    # tests that give the shell a few seconds.
    monkeypatch.setenv("SHELL", "/bin/sh")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "BRINDLE_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    # brindle's own Pro plugins are always installed: keep them off the real
    # keychain and network (a file store under the temporary home, no login).
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "file")
    monkeypatch.delenv("BRINDLE_PRO_DEV", raising=False)
    # No machine-wide Claude Code managed settings, and no ambient Claude credential.
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", "")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    from brindle.pro import license

    license.clear_cache()
    yield home
    license.clear_cache()


@pytest.fixture(autouse=True)
def no_machine_cloud_state(tmp_path, monkeypatch):
    """Self-serve detection (and company login) never reads this machine's real
    AWS/gcloud/az config or runs its CLIs: they point at empty temporary
    directories and count as not installed, unless a test fakes
    company_login.which/run_cmd itself."""
    import shutil

    from brindle import company_login

    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws" / "credentials"))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "no-gcloud"))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "no-azure"))
    cloud = {"aws", "gcloud", "az", "claude", "codex"}
    monkeypatch.setattr(company_login, "which",
                        lambda name: None if name in cloud else shutil.which(name))


@pytest.fixture(autouse=True)
def push_messages(monkeypatch):
    """Most tests read the messages brindle queues for a supervisor directly;
    tests/test_message_pull.py turns pull mode (the real default) back on."""
    from brindle import agents

    monkeypatch.setattr(agents, "pulls_messages", lambda db, agent: False)


@pytest.fixture(autouse=True)
def finish_review_inline(monkeypatch):
    """A verdict's pipeline work runs in a detached brindle process, which
    tests can't wait on: run it before submit_review returns instead."""
    from brindle import agents

    monkeypatch.setattr(agents, "finish_review_later",
                        lambda reviewer_id, ws_id: agents.finish_review(DB(), reviewer_id, ws_id))


@pytest.fixture(autouse=True)
def cli_sign_in_unknown(monkeypatch):
    """No test asks the real claude or codex whether they're signed in (the
    temporary CLAUDE_CONFIG_DIR would say no); tests/test_signin.py fakes it."""
    from brindle import providers

    from brindle import antigravity, opencode

    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: None)
    monkeypatch.setattr(providers, "_SIGNED_IN", {})
    # Only what a test puts on PATH counts as installed, not an app's own copy.
    monkeypatch.setattr(providers, "CODEX_BUNDLED", "/nonexistent/codex")
    monkeypatch.setattr(antigravity, "BUNDLED", "/nonexistent/agy")
    monkeypatch.setattr(opencode, "INSTALLER", "/nonexistent/opencode")


@pytest.fixture(autouse=True)
def agy_settings(tmp_path, monkeypatch):
    """agy's settings.json (which brindle's permission policy mirrors rules
    into) lives under the temporary directory, never the real home."""
    from brindle import antigravity

    path = tmp_path / "gemini" / "antigravity-cli" / "settings.json"
    monkeypatch.setattr(antigravity, "settings_path", lambda: path)
    return path


@pytest.fixture
def db(brindle_home):
    return DB()


@pytest.fixture
def repo(tmp_path):
    """A repo on ``main`` with one commit, pushed to a bare ``origin``."""
    origin = tmp_path / "origin.git"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "app.py").write_text("print('hi')\n")
    (work / ".gitignore").write_text(".env\n")
    (work / ".env").write_text("SECRET=1\n")
    sh("git add -A && git commit -qm init", work)
    sh(f"git remote add origin {origin} && git push -q -u origin main", work)
    sh("git remote set-head origin main", work)
    return work
