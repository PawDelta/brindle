import contextvars
import os
import re
import shutil
import socket
import subprocess
import threading
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


def run_scenario(request, scenario, params=None):
    """Run one row of a scenario table: ``scenario``'s arguments come from
    ``params`` where named there, otherwise from the fixtures of that name."""
    import inspect

    params = params or {}
    return scenario(**{n: params[n] if n in params else request.getfixturevalue(n)
                       for n in inspect.signature(scenario).parameters})


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
_REAL_RUN = subprocess.run
_TMUX_PATH: list[str | None] = []
# Starts the running test's tmux server (and keeper) if it isn't yet.
_START_TEST_SERVER: list = []


def _tmux_path() -> str | None:
    """The machine's tmux, looked up once per run (a test that fakes it
    missing, or narrows PATH, does so after this is decided)."""
    from brindle import tmux

    if not _TMUX_PATH:
        _TMUX_PATH.append(shutil.which(tmux._bin()))
    return _TMUX_PATH[0]


# Routing, cloud, identity and credential-selection variables that brindle reads
# from the environment. An agent launched by brindle inherits some of them
# (BRINDLE_CLOUD_ROUTE="" among them) and a developer's shell may carry any, so
# a test starts without them and sets the ones it needs itself. BRINDLE_HOME,
# BRINDLE_TMUX_SOCKET and the rest a test's own fixtures set are left to those.
AMBIENT_BRINDLE_ENV = (
    # provider routing and the provider binaries / policies
    "BRINDLE_CLOUD_ROUTE", "BRINDLE_CLAUDE_BIN", "BRINDLE_CODEX_BIN", "BRINDLE_AGY_BIN",
    "BRINDLE_OPENCODE_BIN", "BRINDLE_OPENCODE_POLICY", "BRINDLE_CLAUDE_MANAGED_SETTINGS",
    "BRINDLE_AIRGAP",
    # cloud (CI and company login)
    "BRINDLE_CI_PREVIEW_CLOUDS", "BRINDLE_AWS_ID_TOKEN", "BRINDLE_GCP_ID_TOKEN",
    "BRINDLE_AZURE_ID_TOKEN", "BRINDLE_ID_TOKEN", "BRINDLE_GITLAB_TOKEN",
    # credential selection and the Pro account
    "BRINDLE_PRO_TOKEN", "BRINDLE_PRO_DEV", "BRINDLE_PRO_BASE_URL",
    "BRINDLE_PRO_CREDENTIAL_STORE", "BRINDLE_CODEX_AUTH",
    # identity of the agent (and its workspace) that launched this run
    "BRINDLE_AGENT_ID", "BRINDLE_WORKSPACE_ID", "BRINDLE_WORKSPACE_NAME",
    "BRINDLE_WORKSPACE_PATH", "BRINDLE_ROOT_PATH", "BRINDLE_BRANCH", "BRINDLE_BASE_BRANCH",
)


@pytest.fixture(autouse=True)
def ambient_brindle_env(monkeypatch):
    """Every test starts without the routing, cloud, identity and credential
    variables of the shell or agent that runs the suite (AMBIENT_BRINDLE_ENV);
    tests that need one set it themselves. brindle_home requests this fixture,
    so it runs first and its own overrides (the credential store, managed
    settings) are set after the cleanup, not wiped by it."""
    for name in AMBIENT_BRINDLE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_login_shell(monkeypatch):
    """No test runs the person's login shell (brindle.login_shell_env, at a
    daemon's start), and none sees what another read: a test that needs a
    shell gives ``read`` or ``load`` its own runner."""
    from brindle import login_shell_env

    def no_shell(argv, **kw):
        raise OSError("no login shell in tests")

    monkeypatch.setattr(login_shell_env, "_run", no_shell)
    monkeypatch.setattr(login_shell_env, "_values", {})
    monkeypatch.setattr(login_shell_env, "_loading", None)


@pytest.fixture
def real_process_scan():
    """Opts a test out of ``process_scan``: it runs the real ``procs.cwds``
    (against a fake /proc or lsof of its own)."""


@pytest.fixture(autouse=True)
def process_scan(request, monkeypatch):
    """``procs.cwds`` without the host's processes: just this one, in its
    folder, plus whatever the test adds to the dict returned (pid -> folder).
    The real scan reads every process of this user on the machine, so a
    removal or cleanup test would depend on what else runs there (a CI
    runner's ``systemd --user``, a developer's dev server). Tests of the scan
    itself ask for ``real_process_scan``."""
    from brindle import procs

    added: dict[int, str] = {}
    if "real_process_scan" not in request.fixturenames:
        monkeypatch.setattr(procs, "cwds", lambda ignore=(): {os.getpid(): os.getcwd(), **added})
    return added


@pytest.fixture(autouse=True)
def _fresh_google_token_cache():
    """The Google token-expiry cache is process-wide; a test never inherits one."""
    from brindle import cloud_login
    cloud_login._google_expiry_cache.clear()
    yield
    cloud_login._google_expiry_cache.clear()


@pytest.fixture(autouse=True)
def test_tmux_server(monkeypatch):
    """Each test gets its own tmux server (``brindle-test-<pid>-<n>``), not
    one shared by every test on the worker. A detached helper an earlier test
    started (``_launch-health``, ``_warm-checks``, ...) keeps that test's
    BRINDLE_TMUX_SOCKET, so it can only reach that test's dead server, never
    the one a later test is using. A keeper session also prevents closing the
    last test-owned session from taking down the server before new-window
    (#199). The teardown hook reaps the server after all fixtures finish.

    The server starts lazily, with the keeper, just before this process's
    first tmux command that could start it: most tests never touch tmux, and
    starting and reaping a server is most of an empty test's overhead. A
    brindle child process the test starts brings it up first too (see
    no_real_agent_cli)."""
    from brindle import tmux

    _TEST_COUNT[0] += 1
    name = f"brindle-test-{os.getpid()}-{_TEST_COUNT[0]}"
    _TEST_SERVER[:] = [name]
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", name)
    real_start = tmux._start
    binary = _tmux_path()
    started: list[bool] = [] if binary else [False]

    def keeper() -> None:
        if not started:
            started.append(True)
            # Use a plain shell and no user config so the keeper stays alive
            # independently of the agent panes and the user's tmux settings.
            # The real subprocess.run: a test faking tmux's runs sees only its own.
            _REAL_RUN([binary, "-u", "-L", name, "-f", os.devnull, "new-session", "-d", "-s",
                       "brindle-test-keeper", "/bin/sh"], capture_output=True)

    def start(base, args, input=None, **kwargs):
        if (not started and base[-2:] == ["-L", name] and not tmux._is_read(args)
                and args[:1] != ("kill-server",)):
            keeper()
        return real_start(base, args, input, **kwargs)

    monkeypatch.setattr(tmux, "_start", start)
    _START_TEST_SERVER[:] = [keeper]


@pytest.fixture(autouse=True)
def nobody_typing(monkeypatch):
    """No person pressed a key in any agent's pane lately (tmux.pane_input):
    a fake agent's pane is on the default server, which tests never ask."""
    from brindle import tmux

    monkeypatch.setattr(tmux, "pane_input", lambda *a, **k: None, raising=False)


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


# Every test in these files is a brindle CI test, except the refusal, fail-closed and
# secret-hygiene ones (@pytest.mark.brindle_ci_guard); tests elsewhere carry @pytest.mark.brindle_ci.
_BRINDLE_CI_FILES = re.compile(r"(^|/)(test_ci_[^/]*|e2e/test_ci|test_deploy_templates)\.py$")


def pytest_collection_modifyitems(config, items):
    """brindle CI's tests run only with BRINDLE_CI_TESTS=1; otherwise they are
    deselected, whatever -m the run passes. The guards always run: the
    commands stay callable, so their refusals must hold."""
    for item in items:
        if (_BRINDLE_CI_FILES.search(item.nodeid.split("::", 1)[0])
                and not item.get_closest_marker("brindle_ci_guard")):
            item.add_marker(pytest.mark.brindle_ci)
    if os.environ.get("BRINDLE_CI_TESTS") == "1":
        return
    skipped = [item for item in items if item.get_closest_marker("brindle_ci")]
    if skipped:
        config.hook.pytest_deselected(items=skipped)
        items[:] = [item for item in items if not item.get_closest_marker("brindle_ci")]


# A test's call phase must finish within BRINDLE_TEST_DURATION_LIMIT seconds
# (default 10; 0 turns the guard off) unless it is marked
# @pytest.mark.slow("why"). Slow tests are what make the suite take hours.
_SLOW_TESTS: list[tuple[str, float]] = []


def _duration_limit() -> float:
    return float(os.environ.get("BRINDLE_TEST_DURATION_LIMIT", "10"))


def _over_limit(report, limit: float) -> bool:
    return (limit > 0 and report.when == "call" and report.passed
            and report.duration > limit and "slow" not in report.keywords)


def pytest_runtest_logreport(report):
    if _over_limit(report, _duration_limit()):
        _SLOW_TESTS.append((report.nodeid, report.duration))


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput") or not _SLOW_TESTS:
        return
    limit = _duration_limit()
    lines = [f"{nodeid}: {secs:.1f}s" for nodeid, secs in sorted(_SLOW_TESTS, key=lambda t: -t[1])]
    session.config.get_terminal_writer().line(
        f"\n{len(lines)} test(s) ran longer than {limit:g}s: make them faster "
        "(fakes, DB rows, a fake clock) or mark them @pytest.mark.slow(\"why\"):\n  "
        + "\n  ".join(lines), red=True)
    if session.exitstatus == 0:
        session.exitstatus = 1


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Each test's sessions end with it, so none outlive the test that made
    them (or leak into the next one), and a private server or socket a test
    made and forgot is stopped and removed, with a warning naming the test.
    Runs after every fixture's teardown, so a test's monkeypatching (of
    subprocess, say) is undone by then."""
    from brindle import tmux

    yield
    _START_TEST_SERVER.clear()
    while _TEST_SERVER:
        name = _TEST_SERVER.pop()
        # Most tests never start their server (it starts lazily): no socket,
        # nothing to stop, and no tmux run to find that out.
        if not (tmux.socket_dir() / name).exists():
            continue
        try:
            tmux.reap_server(name)
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
        # A brindle child (the CLI, a detached helper) that may use tmux finds
        # the test's server already up, keeper and all (see test_tmux_server);
        # brindle.tmux's own runs in this process start it themselves.
        if _START_TEST_SERVER and not isinstance(args, (str, os.PathLike)):
            argv = [os.fspath(x) for x in args[:5]]
            if argv and (os.path.basename(executable(argv)) == "brindle" or "brindle" in argv):
                _START_TEST_SERVER[0]()
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
def no_reverse_dns(monkeypatch):
    """No reverse DNS lookups: http.server names every server it binds with
    socket.getfqdn(host), and on GitHub's macOS runners that lookup of
    127.0.0.1 waits ~35s for the resolver. A server keeps its address as its
    name instead."""
    monkeypatch.setattr(socket, "getfqdn", lambda name="": name or socket.gethostname())


@pytest.fixture(autouse=True)
def machine_has_room(monkeypatch):
    """Workers and check runs start at once, however loaded the machine
    running the tests is (spawn.no_room); a test that wants it busy sets
    spawn._load itself. No detached helper waits for room either, nor for
    disk (a test that wants it full sets disk.free_bytes): a task
    assign/handoff queued starts in-process when it replies, if there's room."""
    from brindle import disk, spawn, tasks

    def start_once(task_id: str) -> None:
        def start() -> None:
            db = DB()
            t = db.get_task(task_id)
            if t is not None:
                tasks.start_with_room_now(db, t)

        # Off the request's event loop, as the real helper process is.
        thread = threading.Thread(target=contextvars.copy_context().run, args=(start,))
        thread.start()
        thread.join()

    monkeypatch.setattr(disk, "free_bytes", lambda path: 1 << 50)
    monkeypatch.setattr(spawn, "_load", lambda: 0.0)
    monkeypatch.setattr(spawn, "pressure", lambda: 1)
    monkeypatch.setattr(tasks, "_wait_in_background", lambda task_id: None)
    monkeypatch.setattr(tasks, "start_in_background", start_once)


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
def brindle_home(tmp_path, monkeypatch, ambient_brindle_env):
    home = tmp_path / "brindle-home"
    monkeypatch.setenv("BRINDLE_HOME", str(home))
    monkeypatch.setenv("BRINDLE_CACHE_DIR", str(tmp_path / "brindle-cache"))   # not ~/Library/Caches
    # A fresh store is a new person's: the Autonomous preset, which runs what no
    # rule covers. The tests' policy is Standard (asks), so a test that wants the
    # default behaviour says so (tests/test_permission_presets.py).
    monkeypatch.setattr("brindle.permissions.DEFAULT_PRESET", "standard")
    # ... and nobody turned this home's policy on: a test of the real default
    # says so (tests/test_permission_policy.py).
    monkeypatch.setenv("BRINDLE_POLICY_DEFAULT_OFF", "1")
    # Never touch the real ~/.claude.json (providers.trust_folder writes there).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    # brindle's own config homes for the CLIs (provider_homes): the same folders,
    # so nothing is copied and nothing outside the test is written.
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(tmp_path / "claude-config"))
    monkeypatch.setenv("BRINDLE_CODEX_CONFIG_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("BRINDLE_OPENCODE_CONFIG_HOME", str(tmp_path / "opencode-config"))
    # Nor does a test find the machine's own CLIs in the makers' standard places
    # (Homebrew, ~/.local/bin ...): only PATH, as the test sets it.
    from brindle import providers_install

    monkeypatch.setattr(providers_install, "STANDARD", {p: [] for p in providers_install.PROVIDERS})
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
    # A test that forces the keychain backend takes the per-user refresh lock: in the test's dir.
    monkeypatch.setattr("brindle.pro.auth._user_home", lambda: tmp_path / "user-home")
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
def no_real_docker(tmp_path, monkeypatch):
    """No test asks the machine's docker about containers (every workspace
    removal would: ``services.down``): only a docker the test put on PATH
    under its tmp_path counts as installed."""
    from brindle import services

    root = tmp_path.resolve()
    real = services.docker_path

    def docker_path():
        found = real()
        return found if found and Path(found).resolve().is_relative_to(root) else None

    monkeypatch.setattr(services, "docker_path", docker_path)


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
def gh_sign_in_unknown(monkeypatch):
    """No test asks the machine's real gh whether it's signed in, or reads its
    real config: a pane's environment would depend on who runs the suite.
    tests/test_git_reads.py puts the real lookups back, with a fake gh."""
    from brindle import git

    monkeypatch.setattr(git, "gh_credential_helper", lambda: None)
    monkeypatch.setattr(git, "gh_token_in_file", lambda: False)


@pytest.fixture(autouse=True)
def cli_sign_in_unknown(monkeypatch):
    """No test asks the real claude or codex whether they're signed in (the
    temporary CLAUDE_CONFIG_DIR would say no); tests/test_signin.py fakes it."""
    from brindle import providers

    from brindle import antigravity, opencode

    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: None)
    monkeypatch.setattr(providers, "_SIGNED_IN", {})
    # Nor is a launch held for Claude Code's first-run screens (the temporary
    # CLAUDE_CONFIG_DIR has never seen them); tests/test_daemon_actions.py checks.
    monkeypatch.setattr(providers, "claude_onboarded", lambda: True)
    # Only what a test puts on PATH counts as installed, not an app's own copy.
    monkeypatch.setattr(providers, "CODEX_BUNDLED", "/nonexistent/codex")
    monkeypatch.setattr(antigravity, "BUNDLED", "/nonexistent/agy")
    monkeypatch.setattr(opencode, "INSTALLER", "/nonexistent/opencode")


@pytest.fixture(scope="session")
def _claude_stub_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("installed-clis")
    stub = path / "claude"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    # macOS's /usr/bin/git is a shim that looks up the developer tools on
    # every run (about 55ms a call, against 20ms for the git it runs): link
    # that same git here, first on the tests' PATH. Nothing changes elsewhere.
    if shutil.which("git") == "/usr/bin/git" and shutil.which("xcrun"):
        found = _REAL_RUN(["xcrun", "--find", "git"], capture_output=True, text=True)
        real = found.stdout.strip()
        if found.returncode == 0 and real and os.access(real, os.X_OK):
            (path / "git").symlink_to(real)
    return path


@pytest.fixture(autouse=True)
def claude_installed(_claude_stub_dir, monkeypatch):
    """claude is installed, whether or not the machine has it (CI has no
    provider CLIs): the baseline profiles run on it, so a reviewer or a
    profile on claude isn't refused as unusable. A test that wants it missing
    sets PATH itself. The stub is outside tmp_path, so no_real_agent_cli still
    refuses to launch it."""
    monkeypatch.setenv("PATH", f"{_claude_stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")


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


def _build_repo(base: Path, path_first: Path | None = None) -> Path:
    """``repo``'s repo (``base/proj``) and its bare origin (``base/origin.git``),
    made with git (about seven runs); ``path_first`` goes first on its PATH."""
    origin = base / "origin.git"
    work = base / "proj"
    path = os.environ.get("PATH", "")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "PATH": f"{path_first}{os.pathsep}{path}" if path_first else path}
    for k in ("GIT_DIR", "GIT_WORK_TREE"):
        env.pop(k, None)
    binary = shutil.which("git", path=env["PATH"]) or "git"

    def git(*args, cwd=work):
        _REAL_RUN([binary, *args], cwd=cwd, env=env, check=True, capture_output=True)

    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=base)
    work.mkdir()
    git("init", "-q", "-b", "main")
    (work / "app.py").write_text("print('hi')\n")
    (work / ".gitignore").write_text(".env\n")
    (work / ".env").write_text("SECRET=1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "-u", "origin", "main")
    git("remote", "set-head", "origin", "main")
    return work


# Where _repo_template built repo's template, once this process has.
_REPO_TEMPLATE: list[Path] = []


@pytest.fixture(scope="session", autouse=True)
def _repo_template(tmp_path_factory, _claude_stub_dir):
    """``repo``'s repo and origin, built once per test process. ``repo``
    finds it here rather than as a parameter, so it can still be called with
    just a path (tests/workspace_template.py builds its template that way)."""
    base = tmp_path_factory.mktemp("repo-template")
    _build_repo(base, _claude_stub_dir)
    _REPO_TEMPLATE[:] = [base]
    return base


@pytest.fixture
def repo(tmp_path):
    """A repo on ``main`` with one commit, pushed to a bare ``origin``: a
    copy of a template built once, with its origin's path made this test's
    (built in place when there is no template yet)."""
    if not _REPO_TEMPLATE:
        return _build_repo(tmp_path)
    template = _REPO_TEMPLATE[0]
    for name in ("origin.git", "proj"):
        shutil.copytree(template / name, tmp_path / name, symlinks=True)
    work = tmp_path / "proj"
    config = work / ".git" / "config"
    config.write_text(config.read_text().replace(
        f"url = {template / 'origin.git'}\n", f"url = {tmp_path / 'origin.git'}\n"))
    return work
