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


@pytest.fixture(scope="session", autouse=True)
def private_tmux_server():
    """Run every test's tmux sessions on a private server, so parallel test
    runs (e.g. two brindle workers testing at once) can't collide. Nothing of
    it survives the run: a watchdog stops the server and removes its socket
    once this process is gone, even if it was killed before teardown."""
    import os

    from brindle import tmux

    _reap_dead_test_servers()
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
    them (or leak into the next one). Runs after every fixture's teardown,
    so a test's monkeypatching (of subprocess, say) is undone by then."""
    import os

    from brindle import tmux

    yield
    if os.environ.get("BRINDLE_TMUX_SOCKET") == f"brindle-test-{os.getpid()}":
        try:
            tmux.kill_server()
        except tmux.TmuxError:
            pass


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

    monkeypatch.setattr(providers, "_auth_probe", lambda argv: None)
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
