"""Time the New session page's reads and a new session's worktree setup.

Runs only against a throwaway brindle home: set BRINDLE_HOME to a folder that
is not ~/.brindle and holds a copy of the DB (`sqlite3 ~/.brindle/brindle.db
".backup $BRINDLE_HOME/brindle.db"`). Nothing is launched.

    BRINDLE_HOME=... uv run python scripts/bench_new_session.py page [rounds]
        GET /repos, GET /sessions for every repo at once (as the app reads
        them), GET /providers/usable: cold, then warm rounds.
    BRINDLE_HOME=... uv run python scripts/bench_new_session.py create <scratch dir> <source repo>
        In a local clone of <source repo> (its own setup commands, e.g. npm ci):
        a session worktree's branch lookup, worktree and setup, with the pool
        filled first, as a session start makes them.
    BRINDLE_HOME=... uv run python scripts/bench_new_session.py start <scratch dir> <source repo>
        The whole POST /sessions path in such a clone, to its reply, with each
        step's time (create detail), and the setup after launch apart.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

TOKEN = "t" * 43


def _throwaway_home() -> None:
    home = os.environ.get("BRINDLE_HOME")
    if not home or Path(home).resolve() == (Path.home() / ".brindle").resolve():
        sys.exit("set BRINDLE_HOME to a throwaway home holding a copy of the DB (never ~/.brindle)")


async def _call(app, path: str, query: str = "") -> tuple[int, object]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode())]}
    await app(scope, receive, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(body or b"null")


def page(rounds: int) -> None:
    from brindle.daemon.app import build_app

    app = build_app(TOKEN)

    def once(label: str) -> None:
        t0 = time.perf_counter()
        _, repos = asyncio.run(_call(app, "/v1/repos"))
        t_repos = time.perf_counter() - t0

        async def burst():
            return await asyncio.gather(*(_call(app, "/v1/sessions", urlencode({"repo": r["repo_root"]}))
                                          for r in repos))
        t1 = time.perf_counter()
        asyncio.run(burst())
        t_sessions = time.perf_counter() - t1
        first = next((r["repo_root"] for r in repos if not r.get("demo")), None)
        t2 = time.perf_counter()
        if first:
            asyncio.run(_call(app, "/v1/providers/usable", urlencode({"repo": first})))
        t_usable = time.perf_counter() - t2
        # The app's sessions feed waits for every repo's list before it shows any repo, and the
        # page's composer waits for the repos: the whole sequence is the page's critical path.
        print(f"{label}: repos={len(repos)} /repos {t_repos * 1000:.0f}ms  "
              f"/sessions x{len(repos)} at once {t_sessions * 1000:.0f}ms  "
              f"/providers/usable {t_usable * 1000:.0f}ms  "
              f"page total {(time.perf_counter() - t0) * 1000:.0f}ms", flush=True)

    once("cold")
    for i in range(rounds):
        once(f"warm{i + 1}")


def start(scratch: Path, source: str) -> None:
    """The whole POST /sessions path (routes_actions.start_session, a new
    worktree): checks, fetch, worktree, provider, preflight, and the launch up
    to its window. Only what needs a live tmux and CLI is stubbed: the window
    itself, its tmux follow-ups, the detached helpers and the cull. The setup
    after launch runs inline, timed apart."""
    from brindle import agents, tmux
    from brindle import spawn as processes
    from brindle.daemon import routes_actions
    from brindle.db import DB
    from brindle.service import sessions as svc

    work = _clone(scratch, source)
    db = DB()
    from brindle import pool, workspaces

    workspaces.adopt_root(db, str(work))
    pool.fill(db, str(work))
    logged: list[tuple[str, dict]] = []
    svc._log_steps = lambda what, steps: logged.append((what, dict(steps)))
    agents._open_window = lambda *a, **k: "%999999"
    tmux.mark_launched = lambda target: None
    processes.start = lambda *a, **k: None
    agents._detach = lambda *a, **k: None
    svc.cull_detached = lambda *a, **k: None
    after: list[float] = []

    def inline(work_fn):
        t = time.perf_counter()
        work_fn()
        after.append(time.perf_counter() - t)

    svc.after_launch = inline
    t = time.perf_counter()
    got = routes_actions.start_session(db, {}, {"repo": str(work), "choice": "n", "provider": "claude",
                                                "prompt": "Say hello."})
    total = time.perf_counter() - t - sum(after)
    started = isinstance(got, dict) and "error" not in got and "preflight" not in got
    print(f"POST /sessions (to its reply): {total:.2f}s  started={started}", flush=True)
    for what, steps in logged:
        print(f"  {what}: {steps}", flush=True)
    print(f"setup after launch (off the reply path): {sum(after):.2f}s", flush=True)
    if not started:
        print(f"  reply: {got}", flush=True)


def _clone(scratch: Path, source: str) -> Path:
    work, origin = scratch / "proj", scratch / "origin.git"
    if not work.exists():
        subprocess.run(["git", "clone", "-q", "--bare", "--local", "--single-branch", "--branch", "main",
                        source, str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
        for n in range(2, 26):     # a repo many sessions old
            subprocess.run(["git", "branch", f"brindle/session-{n}"], cwd=work, check=True)
        # Origin a commit ahead of the local main, as a checkout's usually is: a new
        # session starts from origin's tip, not the local main the pool is built from.
        subprocess.run("git -c user.name=b -c user.email=b@example.com commit -q --allow-empty -m ahead"
                       " && git push -q origin main && git reset -q --hard HEAD~1", shell=True, cwd=work,
                       check=True)
    return work


def create(scratch: Path, source: str) -> None:
    from brindle import pool, workspaces
    from brindle.db import DB
    from brindle.service import sessions as svc

    work = _clone(scratch, source)
    db = DB()
    ws = workspaces.adopt_root(db, str(work))
    t = time.perf_counter()
    pool.fill(db, str(work))
    print(f"pool fill (once, in the background in real use): {time.perf_counter() - t:.2f}s", flush=True)
    with svc.create_timing() as steps:
        t = time.perf_counter()
        made = svc._new_session_worktree(db, ws, defer_setup=True)
        placed = time.perf_counter() - t
    t = time.perf_counter()
    made.finish()
    setup = time.perf_counter() - t
    detail = {k: round(v, 3) for k, v in steps.items() if isinstance(v, float)}
    print(f"session worktree: {placed:.2f}s {detail} (how={made.how})  setup after launch: {setup:.2f}s",
          flush=True)


if __name__ == "__main__":
    _throwaway_home()
    if len(sys.argv) >= 2 and sys.argv[1] == "page":
        page(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif len(sys.argv) == 4 and sys.argv[1] == "create":
        create(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "start":
        start(Path(sys.argv[2]), sys.argv[3])
    else:
        sys.exit(__doc__)
