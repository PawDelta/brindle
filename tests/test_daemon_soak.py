"""The daemon under repeated use: the read routes and the events feed, on a
real tmux server, must hand back every fd and SQLite connection they open."""

import asyncio
import contextlib
import faulthandler
import json
import os
import signal
import sqlite3
import socket
import threading
import time

import anyio
import httpx2 as httpx
import pytest
from starlette.testclient import TestClient

from brindle import git, identity_lock, spawn, tmux, workspaces
from brindle.daemon import lifecycle, session_review
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace

TOKEN = "t" * 43
ROUNDS = 5
EVENT_CLIENTS = 60

DEADLINE = 50.0     # seconds the whole test may wait on the daemon before it fails
pause = threading.Event()   # never set: pause.wait(n) is a plain timed wait

# path -> the status it must answer; the 404s and the 402 are deliberate error paths
READ_PATHS = {
    "/v1/meta": 200, "/v1/repos": 200, "/v1/sessions?repo={repo}": 200, "/v1/snapshot": 200,
    "/v1/goal?repo={repo}": 200, "/v1/inbox": 200, "/v1/agents/w1": 200, "/v1/agents/missing": 404,
    "/v1/workspaces/{ws}/diff": 200, "/v1/workspaces/{ws}/file?path=app.py": 200,
    "/v1/workspaces/{ws}/file?path=nope.py": 404, "/v1/decisions?repo={repo}": 200,
    "/v1/kb/sources?repo={repo}": 200, "/v1/kb/search?repo={repo}&q=x": 200, "/v1/profiles": 200,
    "/v1/profiles/developer": 200, "/v1/config?repo={repo}": 200, "/v1/routing?repo={repo}": 200,
    "/v1/doctor": 200, "/v1/providers/claude/preflight?repo={repo}": 200, "/v1/account": 200,
    "/v1/cost": 402,    # Pro-gated: refused after the route opened its DB
}


@contextlib.contextmanager
def wall_clock(seconds: float):
    """Interrupt the test thread after ``seconds`` wherever it is waiting (a
    handshake, an HTTP call, a receive, cleanup): the client's own waits have
    no timeout. The serial pass runs tests on the main thread, where alarms land."""
    def expire(signum, frame):
        raise TimeoutError(f"the daemon soak took over {seconds:.0f}s: it is stalled")

    old = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    # Should the raise itself be swallowed or cleanup block, print where and kill the run.
    faulthandler.dump_traceback_later(seconds + 5, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


class Tracked(sqlite3.Connection):
    """A connection that says whether it is still open."""

    live: set = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Tracked.live.add(id(self))

    def close(self):
        super().close()
        Tracked.live.discard(id(self))


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def settled(measure, limit: float = 5.0) -> int:
    """``measure()`` once it has stopped changing for a second (worker threads
    are still winding down after a socket closes)."""
    deadline, last, same = time.monotonic() + limit, None, 0
    while time.monotonic() < deadline:
        now = measure()
        same = same + 1 if now == last else 0
        if same >= 5:
            break
        last = now
        pause.wait(0.2)
    return measure()


class Sockets:
    """WebSockets whose receives give up at ``deadline`` (a monotonic time):
    a daemon that never answers fails the test instead of hanging it."""

    def __init__(self, client, deadline: float) -> None:
        self.client, self.deadline = client, deadline

    @contextlib.contextmanager
    def connect(self, path):
        with self.client.websocket_connect(path) as sock:
            sock.send_text(TOKEN)
            yield sock

    def receive(self, sock):
        """The next frame's payload (text parsed as JSON, else bytes)."""
        async def next_message():
            with anyio.fail_after(max(0.0, self.deadline - time.monotonic())):
                return await sock._send_rx.receive()    # what sock.receive() waits on, with a timeout

        try:
            message = sock.portal.call(next_message)
        except TimeoutError:
            pytest.fail("no frame from the daemon before the deadline")
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"]) if message.get("text") is not None else message["bytes"]


@pytest.mark.serial
def test_daemon_leaks_nothing_under_load(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/soak").workspace
    pane = tmux._tmux("new-session", "-d", "-s", "soak", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", pane, None, 1000.0,
                       tmux_server=tmux.current_server()))
    # The daemon is the file's only user, as in production: SQLite keeps the fd
    # of a connection closed beside another open one, which is not a leak.
    db.conn.close()
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=Tracked, **kw))
    paths = {p.format(repo=repo, ws=ws.id): status for p, status in READ_PATHS.items()}
    headers = {"Authorization": f"Bearer {TOKEN}"}

    def hammer(client, socks):
        for _ in range(ROUNDS):
            for path, status in paths.items():
                got = client.get(path, headers=headers).status_code
                assert got == status, f"{path}: {got}, expected {status}"
        for _ in range(EVENT_CLIENTS):
            with socks.connect("/v1/events") as sock:
                socks.receive(sock)     # the replay: the feed has its DB open

    deadline = time.monotonic() + DEADLINE
    with wall_clock(DEADLINE), TestClient(build_app(TOKEN)) as client:
        socks = Sockets(client, deadline)
        hammer(client, socks)   # warm-up: lazily opened files and thread pools are not leaks
        fds = settled(open_fds)
        conns = settled(lambda: len(Tracked.live))
        hammer(client, socks)
        assert settled(open_fds) <= fds
        assert settled(lambda: len(Tracked.live)) <= conns


@pytest.mark.serial
@pytest.mark.variant("Long supervisor history with clients abandoning and re-asking for attributed diffs")
def test_session_diff_overload(db, repo, monkeypatch):
    # Import 204 worker commits and 12 genuine two-parent supervisor merges.
    # The final merge deletes a base file, making attribution walk the whole history.
    base, stream = git.out(["rev-parse", "HEAD"], repo), []
    parent = base
    for step in range(12):
        for part in range(18):
            mark = step * 18 + part + 1
            branch = "session" if part == 17 else f"worker-{step}"
            subject = f"Merge branch 'worker-{step}'" if part == 17 else f"edit {mark}"
            content = f"worker {step}, revision {min(part, 16)}\n"
            first = f":{mark - 1}" if 0 < part < 17 else parent
            stream += [f"commit refs/heads/{branch}\nmark :{mark}\n",
                       f"committer Test <test@example.com> {1700000000 + mark} +0000\n",
                       f"data {len(subject)}\n{subject}\nfrom {first}\n"]
            if part == 17:
                stream.append(f"merge :{mark - 1}\n")
            for file in range(2):
                stream.append(f"M 100644 inline file-{step}-{file}.txt\ndata {len(content)}\n{content}")
            if step == 11:
                stream.append("D app.py\n")
            stream.append("\n")
        parent = f":{mark}"
    git.run(["fast-import", "--quiet"], repo, input="".join(stream))
    git.run(["checkout", "-q", "session"], repo)
    db.add_workspace(Workspace("overload", str(repo), "session", "root", "session", "main",
                               str(repo), None, "unused", 1000.0))
    db.add_agent(Agent("overload", "overload", "supervisor", "claude", None, "interactive",
                       "idle", "", None, 1000.0))
    db.conn.close()
    local, started, completed, stopped = threading.local(), [], [], []
    real_git, real_diff = git.run, session_review.diff

    def counted(*args, **kwargs):
        if hasattr(local, "count"):
            local.count += 1
        return real_git(*args, **kwargs)

    def measured(*args):
        start, local.count = time.monotonic(), 0
        started.append(start)
        try:
            result = real_diff(*args)
            completed.append((local.count, time.monotonic() - start, len(result["files"])))
            return result
        except spawn.Busy:          # its client left: brindle.spawn stopped its reads
            stopped.append(start)
            raise
        finally:
            del local.count

    monkeypatch.setattr(git, "run", counted)
    monkeypatch.setattr(session_review, "diff", measured)

    async def exercise():
        short, cheap_bound = 0.075, 0.1
        limiter = anyio.to_thread.current_default_thread_limiter()
        samples, abandoned, shed, answered, latencies = [], [], [], [], {}
        loop = asyncio.get_running_loop()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        app = build_app(TOKEN)
        lane = app.app.state.heavy_read_lane
        server = lifecycle.make_server(app)
        server.config.load()
        server.lifespan = server.config.lifespan_class(server.config)
        await server.startup(sockets=[sock])
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        try:
            async with httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {TOKEN}"},
                                         timeout=DEADLINE, trust_env=False) as client:
                async def read(path, key):
                    start = time.monotonic()
                    response = await client.get(path)
                    response.raise_for_status()
                    latencies[key] = time.monotonic() - start
                    return response

                response = await read("/v1/sessions/overload/diff", "diff_baseline")
                assert len(response.json()["files"]) == 25
                await read("/v1/meta", "meta_baseline")
                await read("/v1/repos", "repos_baseline")

                async def poll():
                    for _ in range(5):
                        try:
                            response = await client.get("/v1/sessions/overload/diff",
                                                        timeout=httpx.Timeout(DEADLINE, read=short))
                            if response.status_code == 503 and response.json().get("error", {}).get("code") == "busy":
                                shed.append(time.monotonic())
                            else:
                                response.raise_for_status()
                                answered.append(time.monotonic())
                        except httpx.ReadTimeout:
                            abandoned.append(time.monotonic())
                        stats = limiter.statistics()
                        samples.append((stats.borrowed_tokens, stats.tasks_waiting, lane.workers.borrowed_tokens))

                async def sample_cheap():
                    # Cheap reads while the pollers' diffs are really running.
                    while not pollers_done.is_set():
                        for path in ("/v1/meta", "/v1/repos"):
                            busy = lane.workers.borrowed_tokens > 0
                            start = time.monotonic()
                            (await client.get(path)).raise_for_status()
                            # Busy if the heavy lane held a worker when the request began or ended.
                            during.append((time.monotonic() - start,
                                           busy or lane.workers.borrowed_tokens > 0))

                # Drop the cached diff: the first poll recomputes the full history.
                session_review._forget()
                pollers_done, during = asyncio.Event(), []
                sampler = asyncio.ensure_future(sample_cheap())
                outcomes = await asyncio.gather(*(poll() for _ in range(8)), return_exceptions=True)
                pollers_done.set()
                await sampler
                assert all(outcome is None for outcome in outcomes), outcomes
                overlapped = [seconds for seconds, busy in during if busy]
                assert overlapped, "no cheap read overlapped heavy work"
                window = [seconds for seconds, _ in during]
                executor = loop._default_executor
                executor_at_probe = [len(executor._threads), executor._work_queue.qsize()] if executor else [0, 0]
                executor_start = time.monotonic()
                await asyncio.to_thread(lambda: None)
                executor_latency = time.monotonic() - executor_start
                occupancy = limiter.statistics()
                heavy_at_probe = [lane.admission.borrowed_tokens, lane.workers.borrowed_tokens]
                await asyncio.gather(read("/v1/meta", "meta_loaded"), read("/v1/repos", "repos_loaded"))
        finally:
            # Disconnected requests still own real workers; drain before closing the loop.
            await asyncio.gather(*list(server.server_state.tasks))
            await server.shutdown(sockets=[sock])
            sock.close()
        print("OVERLOAD " + json.dumps({
            "latencies_s": latencies, "cheap_during_diffs": [len(overlapped), len(window), max(window)],
            "abandoned": len(abandoned), "shed": len(shed), "answered": len(answered),
            "diffs_completed": len(completed), "git_per_diff": sorted({c[0] for c in completed}),
            "diffs_stopped": len(stopped),
            "diff_seconds_min_max": [min(c[1] for c in completed[1:] or completed),
                                     max(c[1] for c in completed[1:] or completed)],
            "anyio_capacity": limiter.total_tokens, "anyio_peak_active": max(s[0] for s in samples),
            "anyio_peak_queued": max(s[1] for s in samples),
            "anyio_at_probe": [occupancy.borrowed_tokens, occupancy.tasks_waiting],
            "heavy_capacity": lane.workers.total_tokens, "heavy_peak_active": max(s[2] for s in samples),
            "heavy_admission_workers_at_probe": heavy_at_probe,
            "executor_probe_s": executor_latency, "executor_threads_queued_at_probe": executor_at_probe,
        }, sort_keys=True))
        # A busy response can itself arrive after the client's short bound.
        assert len(answered) + len(abandoned) + len(shed) == 40
        # Every diff either finished in full or was stopped with its client; none is lost.
        assert len(completed) + len(stopped) == len(started) > 1
        assert all(c[2] == 25 for c in completed)
        assert limiter.borrowed_tokens == 0
        assert lane.admission.borrowed_tokens == lane.workers.borrowed_tokens == 0
        assert occupancy.tasks_waiting == 0
        assert occupancy.borrowed_tokens < occupancy.total_tokens
        assert max(latencies["meta_loaded"], latencies["repos_loaded"]) < cheap_bound, latencies
        assert max(window) < cheap_bound, sorted(window)[-5:]

    with wall_clock(DEADLINE):
        asyncio.run(exercise())
