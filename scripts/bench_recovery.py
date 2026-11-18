"""Time a daemon start's session recovery (lifecycle.resume_after_start) on an
isolated copy of brindle's home.

Run with the worktree's Python: scripts/bench_recovery.py [--sessions 6] [--runs 3]
[--roots ID ...] [--viewed ID] [--slow-worker SECONDS --slow-at checks|window]. To compare with another engine, check out its
src/brindle/daemon/lifecycle.py, routes_read.py and src/brindle/agents.py and run it again
(--app-reads gate for an app that releases panes from its gate's own reads only).

The temporary BRINDLE_HOME lives under this worktree and is removed on exit;
the real home is only read (its DB through SQLite's backup, read-only). The
copy's sessions are the real home's resume record if one is waiting, else its
most recently active chats, marked paused as a restart leaves them. They come
back on a private tmux server with a stand-in for Claude Code that draws its
input box and waits, so brindle's own work is what's timed, not the CLI's.

Only sessions whose every agent to resume runs Claude Code are timed (any
other CLI would be the real one): it refuses the rest before anything runs.
Every workspace and repo of theirs is redirected into the copy (an empty git
repo each, none of the real files: repo settings and code maps are defaults),
and so is Claude Code's config home (CLAUDE_CONFIG_DIR, each agent's
config_home), with an empty transcript per conversation so they resume.
Kept off everything else outside the copy: signals to the processes
procs.stop finds (it still looks for them, as a restart does; the real
sessions share the copy's agent ids), the queued tasks and branches a resume
picks back up (they would start real workers), Claude Code's folder trust
(the real ~/.claude.json), keychain reads (a dialog for this Python), and the
launch helpers that outlive a run (a process is started in their place).
Each run prints recovery's timing log: when each session started, had its chat
up and ended, from the daemon's start.
"""

import argparse
import atexit
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FAKE_CLAUDE = """#!/bin/sh
# Claude Code's stand-in: a version when asked, else its input box, then waits.
case " $* " in *" --version "*) echo "2.1.0 (Claude Code)"; exit 0;; esac
[ -t 1 ] || exit 0
printf '\\n\\342\\235\\257 \\n? for shortcuts\\n'
exec tail -f /dev/null
"""


def pick(source: sqlite3.Connection, real_home: Path, count: int) -> list[str]:
    """The sessions to bring back: the real home's resume record, else its last active chats."""
    record = real_home / "resume-on-start.json"
    if record.exists():
        return [e["id"] for e in json.loads(record.read_text())][:count]
    rows = source.execute(
        "SELECT a.id FROM agents a LEFT JOIN (SELECT agent_id, MAX(ts) AS last FROM events GROUP BY agent_id) e"
        " ON e.agent_id = a.id WHERE a.parent_id IS NULL AND a.mode = 'interactive' AND a.provider = 'claude'"
        " AND a.status != 'done' AND a.dismissed_at IS NULL ORDER BY COALESCE(e.last, a.created_at) DESC LIMIT ?",
        (count,)).fetchall()
    return [r[0] for r in rows]


def prepare(home: Path, source_db: Path, real_home: Path, roots: list[str], server: str, viewed: str | None) -> None:
    """The copy as the last daemon's pause leaves it: ``roots`` and their running agents paused, on ``server``."""
    from brindle.db import DB
    from brindle import agents, providers
    from brindle.daemon import lifecycle

    shutil.copyfile(source_db, home / "brindle.db")
    for name in ("config.json", "quota.json", "agent-window-size"):
        if (real_home / name).exists():
            shutil.copyfile(real_home / name, home / name)
    db = DB()
    try:
        everyone = [a for root in roots for a in agents.tree(db, root)]
        # What a resume launches (agents._resume's restartable): all but the done, and of those a
        # worker the pipeline sent back to work that hasn't reported since.
        trees = [a for a in everyone if a.status != "done"
                 or (a.pipeline in agents.BACK_AT_WORK and not agents.finished_reporting(a))]
        # Only Claude Code has a stand-in here: any other CLI a resume would launch is the real one.
        other = [f"{a.id} ({a.provider})" for a in trees
                 if a.provider != "claude" and providers.get_provider(a.provider).launches_process]
        if other:
            raise SystemExit(f"bench_recovery: only Claude Code sessions can be timed; these run other CLIs: "
                             f"{', '.join(other)}")
        # Every place a launch may write, in the copy: each workspace (a repo of its own, with
        # git but none of the real one's files) and Claude Code's config home, where each
        # conversation the sessions resume is an empty transcript (so they resume, not start fresh).
        claude = home / "claude"
        claude.mkdir()
        # Past its first-run notes, as the person's own is (else the preflight holds every launch).
        (claude / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
        repos: dict[str, Path] = {}
        for ws_id in sorted({a.workspace_id for a in everyone}):
            ws = db.get_workspace(ws_id)
            if ws is None:
                continue
            repo = repos.setdefault(ws.repo_root, home / "repos" / str(len(repos)))
            path = repo if os.path.realpath(ws.path) == os.path.realpath(ws.repo_root) else home / "ws" / ws.id
            for folder in (repo, path):
                if not folder.exists():
                    folder.mkdir(parents=True)
                    subprocess.run(["git", "init", "-q", str(folder)], check=True)
            db.conn.execute("UPDATE workspaces SET repo_root=?, path=? WHERE id=?", (str(repo), str(path), ws.id))
        # Every other agent's window was on the last daemon's server, as after a restart: none is
        # alive on this one (left on the real one, each read would ask it about each of them).
        db.conn.execute("UPDATE agents SET tmux_window='', tmux_server=?", (server,))
        for a in everyone:
            db.update_agent(a.id, config_home=str(claude))
        for a in trees:
            if a.status != "done":      # a done one sent back to work is the resume's to reopen
                db.set_status(a.id, "paused")
            if a.session_ref:
                (claude / "projects" / "bench").mkdir(parents=True, exist_ok=True)
                (claude / "projects" / "bench" / f"{a.session_ref}.jsonl").touch()
        db.conn.commit()
    finally:
        db.conn.close()
    lifecycle._record_resume([{"id": root, "server": server} for root in roots])
    if viewed:
        lifecycle.record_viewed(viewed)


def isolate() -> None:
    """Keep the run's effects inside the copy (see the module's docstring)."""
    from brindle import agents, debuglog, keystore, pipeline, procs, providers, tasks
    from brindle import spawn as processes

    # A launch's helpers outlive the run and would write to its home once it's gone: a process is
    # started in their place, so what the launch spends starting them is still counted.
    real_start, real_detach = processes.start, processes.detach

    def helper(args) -> bool:
        return any(str(a).startswith(("_after-launch", "_launch-health")) for a in args)

    processes.start = lambda args, **kw: real_start(["/usr/bin/true"] if helper(args) else args, **kw)
    processes.detach = lambda args, **kw: real_detach(["/usr/bin/true"] if helper(args) else args, **kw)
    procs.terminate = lambda pids, *a, **kw: 0
    pipeline.on_resumed = lambda *a, **kw: None
    tasks.on_resumed = lambda *a, **kw: None
    providers.trust_folder = lambda *a, **kw: None
    keystore.known_names = lambda *a, **kw: []
    keystore.get_key_info = lambda *a, **kw: (None, 0.0)
    agents.flush = lambda *a, **kw: None
    debuglog.staff = lambda *a, **kw: True
    debuglog._publish_timings = debuglog._write_timings


def slow_workers(seconds: float, at: str) -> None:
    """Each worker's launch takes ``seconds`` longer, ``at`` its checks (before its status
    changes, as a loaded machine's did: 7b132633's profile and model check took 7.1 s) or
    its window (after its status says starting: folder trust, launch health, the window)."""
    from brindle import agents, providers

    real, trust = agents._launch_resolved, providers.trust_folder
    worker = threading.local()

    def launch(db, agent, ws, **kw):
        worker.on = agent.parent_id is not None
        if worker.on and at == "checks":
            threading.Event().wait(seconds)
        return real(db, agent, ws, **kw)

    def trusted(*a, **kw):
        if getattr(worker, "on", False) and at == "window":
            threading.Event().wait(seconds)
        return trust(*a, **kw)

    agents._launch_resolved, providers.trust_folder = launch, trusted


def app(recovery, lead: str, stop: threading.Event, marks: dict, any_read: bool,
        chat_read: bool = False) -> dict[str, threading.Thread]:
    """The app's reads while recovery runs, as the daemon serves them (one shared
    SnapshotView): its pane gate (App.tsx usePanesReady) looks at how recovery
    stands every STARTUP_CHECK and reads the snapshot in turn, and its sync reads
    it again on each new event, one read at a time. ``mark``: the first look that
    found the open session ``lead`` back. ``open``: when the first read begun
    after it came back with ``lead``'s window, the gate's own only, unless
    ``any_read``: the app that also looks on each event (its answer comes after
    the read the event began), refreshes once it marks one back, and releases
    from any read; ``all``: when a gate read begun after recovery ended came back. ``chat``,
    with ``chat_read``: when the read of ``lead``'s chat alone that the app makes once it marks
    it back (App.tsx showChat, GET /v1/sessions/{root}/view) came back with its window."""
    from types import SimpleNamespace

    from brindle.daemon import routes_read
    from brindle.daemon.routes_read import SnapshotView
    from brindle.db import DB

    view = SnapshotView()
    born = time.monotonic()     # the daemon's start: recovery's log counts from its Recovery, this from here

    def at() -> float:
        return time.monotonic() - born

    def shows(rows) -> bool:
        return any(a["id"] == lead and a.get("window") for ws in rows for a in ws["agents"])

    def whole(rows) -> bool:
        """``lead``'s every agent with its window, as the app before 0.6.4+release.217 released a
        session (App.tsx waitingForPanes over its whole tree): its workers' launches held its chat."""
        found = {a["id"]: a for ws in rows for a in ws["agents"]}
        mine, grew = {lead}, True
        while grew:
            more = {i for i, a in found.items() if a.get("parent_id") in mine} - mine
            mine, grew = mine | more, bool(more)
        return lead in found and not any(
            a.get("provider") != "subagent" and not a.get("headless") and not a.get("locked")
            and a.get("status") not in ("paused", "done", "failed", "closed", "rewound", "exited") and not a.get("window")
            for i, a in found.items() if i in mine)

    def read(db, counted: bool) -> None:
        began = at()
        state = SimpleNamespace(recovery=recovery)
        if hasattr(routes_read, "recovering"):      # as GET /v1/snapshot reads it (an engine of before that didn't)
            rows = view.read(db, None, not routes_read.recovering(state))
        else:
            rows = view.read(db, None)
        marks["renders"].append(round(at() - began, 2))
        if counted and "mark" in marks and began > marks["mark"]:
            if "open" not in marks and shows(rows):
                marks["open"] = at()
            if "whole" not in marks and whole(rows):
                marks["whole"] = at()

    again = threading.Event()   # sync is asked for one more read (the app's refresh once it marks one back)

    def chat() -> None:
        """The open session's chat alone, as the daemon serves it (routes_read.session_view)."""
        began = at()
        response = routes_read.session_view(SimpleNamespace(
            path_params={"root": lead}, query_params={}, app=SimpleNamespace(state=SimpleNamespace(recovery=recovery))))
        marks["chat_read"] = round(at() - began, 2)
        if response.status_code == 200 and shows([json.loads(response.body)]):
            marks["chat"] = at()

    def look() -> None:
        """What /v1/meta says (Recovery.progress): ``lead`` back, no longer pending, or recovery over.
        The first look that finds it so is the mark; the app then refreshes (any_read) and reads
        its chat alone (chat_read)."""
        progress = recovery.progress()
        back = (lead not in progress["pending"]) if progress else bool(recovery.total)
        if back and "mark" not in marks:
            marks["mark"] = at()
            if any_read:
                again.set()
            if chat_read:
                threading.Thread(target=chat, daemon=True).start()

    def gate() -> None:
        """Looks every STARTUP_CHECK, reading after each, and once more at once when ``lead``
        came back while its read was out (App.tsx's watch)."""
        db = DB()
        try:
            while not stop.is_set():
                over = recovery.idle.is_set() and recovery.total
                look()
                began = at()
                read(db, True)
                if over:
                    marks["all"] = at()
                    return
                look()      # what its watch saw while the read was out
                if not began < marks.get("mark", -1):
                    stop.wait(STARTUP_CHECK)
        finally:
            db.conn.close()

    def lookout(event: threading.Event) -> None:
        """The app's look on an event (its meta's round trip): the read the event began
        is already out by the time it answers."""
        if not event.wait(EVENT_LOOK):
            look()

    def sync() -> None:
        """A read on each new event, one at a time, and a look on it (any_read: the
        app has one on each event while sessions come back)."""
        db, seen = DB(), None
        try:
            while not stop.is_set():
                newest = db.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
                if newest != seen or again.is_set():
                    if newest != seen and any_read and "mark" not in marks:
                        threading.Thread(target=lookout, args=(stop,), daemon=True).start()
                    seen = newest
                    again.clear()
                    read(db, any_read)
                else:
                    stop.wait(0.05)     # the stream's next event
        finally:
            db.conn.close()

    threads = {"gate": threading.Thread(target=gate, daemon=True), "sync": threading.Thread(target=sync, daemon=True)}
    for thread in threads.values():
        thread.start()
    return threads


STARTUP_CHECK = 0.5     # seconds between the app gate's looks (App.tsx)
EVENT_LOOK = 0.05       # what a look on the event of a session's return takes to come (its meta's round trip)


def run_once(args, checkout: Path, fake: Path, n: int) -> float:
    # Removed by hand too, once its tmux server is gone (a session's pane may still write to it till then).
    with tempfile.TemporaryDirectory(prefix=".bench-recovery-", dir=checkout, ignore_cleanup_errors=True) as tmp:
        home = Path(tmp)
        HOMES.append(home)
        server = f"brindle-bench-recovery-{os.getpid()}-{n}"
        os.environ.update(BRINDLE_HOME=str(home), BRINDLE_TMUX_SOCKET=server, BRINDLE_CLAUDE_BIN=str(fake),
                          CLAUDE_CONFIG_DIR=str(home / "claude"))
        backup = home / "source.db"
        with sqlite3.connect((args.source.resolve() / "brindle.db").as_uri() + "?mode=ro", uri=True) as source:
            roots = args.roots or pick(source, args.source, args.sessions)
            with sqlite3.connect(backup) as target:
                source.backup(target)
        viewed = (args.source / "viewed-session").read_text().strip() if (args.source / "viewed-session").exists() else None
        lead = args.viewed or (viewed if viewed in roots else roots[0])
        prepare(home, backup, args.source, roots, server, lead)
        from brindle.daemon import lifecycle

        stop = threading.Event()
        try:
            recovery = lifecycle.Recovery()
            marks: dict = {"renders": []}
            readers = app(recovery, lead, stop, marks, args.app_reads in ("any", "chat"), args.app_reads == "chat")
            began = time.monotonic()
            back = lifecycle.resume_after_start(recovery)
            took = time.monotonic() - began
            readers["gate"].join(30)    # bounded: its read after recovery's end, which the app waits for
            nan = float("nan")
            print(f"run {n}: {len(back)} of {len(roots)} back in {took:.2f}s; open session's chat up at "
                  f"+{marks.get('mark', nan):.2f}s (the app's first look that saw it), its pane showed from its chat's own row at "
                  f"+{marks.get('chat', nan):.2f}s (that read took {marks.get('chat_read', nan):.2f}s), "
                  f"from a whole snapshot at +{marks.get('open', nan):.2f}s "
                  f"(+{marks.get('whole', nan):.2f}s waiting for its workers' panes too, as before), every pane at +{marks.get('all', nan):.2f}s ({len(marks['renders'])} snapshot renders, "
                  f"longest {max(marks['renders'], default=0):.2f}s)", flush=True)
            return took
        finally:
            stop.set()
            subprocess.run(["tmux", "-L", server, "kill-server"], capture_output=True)
            # A pane's process can outlive its server (a session's sidebar did, writing on to the copy).
            held = subprocess.run(["lsof", "-t", "+D", str(home)], capture_output=True, text=True).stdout.split()
            for pid in {int(p) for p in held} - {os.getpid()}:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass    # gone meanwhile
            shutil.rmtree(home, ignore_errors=True)


HOMES: list[Path] = []      # each run's copy: removed again at exit, after brindle's own exit hooks (Pro's spool) wrote to it


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=Path.home() / ".brindle")
    parser.add_argument("--sessions", type=int, default=6)
    parser.add_argument("--roots", nargs="*", help="these sessions instead of the last active ones")
    parser.add_argument("--viewed", help="the session the app showed last (default: the real home's, if among them)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--app-reads", choices=("gate", "any", "chat"), default="chat",
                        help="which of the app's reads may show the open session's pane: only the gate's own "
                             "(the app before it released from sync's reads), any begun after it was back "
                             "(before it read the chat alone), or also its chat's own row read once it was back")
    parser.add_argument("--slow-worker", type=float, default=0.0,
                        help="seconds added to each worker's launch (a loaded machine)")
    parser.add_argument("--slow-at", choices=("checks", "window"), default="checks",
                        help="where: its checks, before its status changes, or its window, after")
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[1]
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)
    isolate()
    if args.slow_worker:
        slow_workers(args.slow_worker, args.slow_at)
    with tempfile.TemporaryDirectory(prefix=".bench-recovery-bin-", dir=checkout) as bin_dir:
        fake = Path(bin_dir) / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        took = [run_once(args, checkout, fake, n) for n in range(args.runs)]
    print(json.dumps({"runs": [round(t, 2) for t in took], "best": round(min(took), 2)}), flush=True)
    # brindle's own exit hooks (Pro's event spool) write to the last copy: they run first, then the copies go.
    atexit._run_exitfuncs()
    for home in HOMES:
        shutil.rmtree(home, ignore_errors=True)
    os._exit(0)     # nothing left to run at exit, and the resumes' follow-up threads aren't waited for


if __name__ == "__main__":
    main()
