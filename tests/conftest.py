import os
import re
import subprocess
from pathlib import Path

import pytest

from brindle.db import DB


def sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(
        cmd, shell=True, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _reap_dead_test_servers() -> None:
    """Servers (and socket files) of earlier runs that died before their
    teardown, e.g. killed by a command timeout."""
    from brindle import procs, tmux

    for name in tmux.other_servers("brindle-test-"):
        pid = name.removeprefix("brindle-test-")
        if pid.isdigit() and not procs.alive(int(pid)):
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
         'tmux -L "$2" kill-server 2>/dev/null; rm -f "$3"',
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
    import os

    from brindle import tmux

    yield
    if os.environ.get("BRINDLE_TMUX_SOCKET") == f"brindle-test-{os.getpid()}":
        try:
            tmux.kill_server()
        except tmux.TmuxError:
            pass
    leaked = _reap_run_leftovers(_KNOWN_SOCKETS)
    if leaked:
        item.warn(pytest.PytestWarning(
            f"{item.nodeid} left tmux server(s) or socket(s) behind, now removed: {', '.join(leaked)}"))
    _KNOWN_SOCKETS.update(_socket_names())


@pytest.fixture(autouse=True)
def no_signed_in_providers(monkeypatch):
    """No sign-in probes in tests, and no `providers` param on /entitlement
    (tests/test_entitlement_providers.py sets its own)."""
    from brindle import doctor

    monkeypatch.setattr(doctor, "signed_in_providers", lambda: [])


@pytest.fixture(autouse=True)
def brindle_home(tmp_path, monkeypatch):
    home = tmp_path / "brindle-home"
    monkeypatch.setenv("BRINDLE_HOME", str(home))
    # Never touch the real ~/.claude.json (providers.trust_folder writes there).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
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
    from brindle.pro import license

    license.clear_cache()
    yield home
    license.clear_cache()


@pytest.fixture(autouse=True)
def push_messages(monkeypatch):
    """Most tests read the messages brindle queues for a supervisor directly;
    tests/test_message_pull.py turns pull mode (the real default) back on."""
    from brindle import agents

    monkeypatch.setattr(agents, "pulls_messages", lambda db, agent: False)


@pytest.fixture(autouse=True)
def cli_sign_in_unknown(monkeypatch):
    """No test asks the real claude or codex whether they're signed in (the
    temporary CLAUDE_CONFIG_DIR would say no); tests/test_signin.py fakes it."""
    from brindle import providers

    from brindle import antigravity

    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: None)
    monkeypatch.setattr(providers, "_SIGNED_IN", {})
    # Only what a test puts on PATH counts as installed, not an app's own copy.
    monkeypatch.setattr(providers, "CODEX_BUNDLED", "/nonexistent/codex")
    monkeypatch.setattr(antigravity, "BUNDLED", "/nonexistent/agy")


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
