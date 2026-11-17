"""Cold snapshot queries and latency on an isolated copy, quiet or with four readers.

Run with the worktree's Python: scripts/bench_startup.py [--threads 4] [--base REF].
The temporary BRINDLE_HOME lives under this worktree and is removed on exit.
Copies DB/config/quota from the real home. Workspace paths are redirected to
this checkout, transcript paths cleared and panes disabled so timing isolates
the snapshot's database work. The four readers deserialize/serialize agent
rows, look up their workspaces, and inspect this checkout's HEAD.
With --base, only view.py is loaded from that ref; its dependencies use this
checkout. This isolates the batching change, not arbitrary historical engines.
"""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--source", type=Path, default=Path.home() / ".brindle")
    parser.add_argument("--base", help="also measure this ref's view.py on the identical DB backup")
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=".bench-startup-", dir=checkout) as home:
        os.environ["BRINDLE_HOME"] = home
        with sqlite3.connect((args.source.resolve() / "brindle.db").as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(Path(home) / "source.db") as target:
                source.backup(target)
        for name in ("config.json", "quota.json"):
            if (args.source / name).exists():
                shutil.copyfile(args.source / name, Path(home) / name)

        from brindle import view

        versions = [("current", view)]
        if args.base:
            code = subprocess.run(["git", "show", f"{args.base}:src/brindle/view.py"],
                                  cwd=checkout, check=True, capture_output=True, text=True).stdout
            baseline = ModuleType("brindle.benchmark_base_view")
            sys.modules[baseline.__name__] = baseline
            exec(compile(code, f"{args.base}:src/brindle/view.py", "exec"), baseline.__dict__)
            versions.insert(0, (args.base, baseline))
        for index, (version, rendering) in enumerate(versions):
            run_home = Path(home) / str(index)
            run_home.mkdir()
            os.environ["BRINDLE_HOME"] = str(run_home)
            shutil.copyfile(Path(home) / "source.db", run_home / "brindle.db")
            for name in ("config.json", "quota.json"):
                if (Path(home) / name).exists():
                    shutil.copyfile(Path(home) / name, run_home / name)
            _measure(rendering, args.threads, checkout, version)


def _measure(view, competitors, checkout, version):
    from brindle import status_cache, tmux
    from brindle.db import DB

    status_cache.clear()
    db = DB()
    workspaces = db.find_workspaces()
    # Preserve row cardinality, but read only this worker's checkout.
    for ws in workspaces:
        db.conn.execute("UPDATE workspaces SET path=? WHERE id=?", (str(checkout), ws.id))
    db.conn.execute("UPDATE agents SET transcript_path=NULL, tmux_window='' ")
    db.conn.commit()
    tmux.list_panes = lambda *a, **k: tmux.PaneSnapshot(None)
    stop = threading.Event()
    ready = threading.Barrier(competitors + 1)

    def contend():
        local = DB()
        ready.wait()
        try:
            while not stop.is_set():
                agents = local.list_agents()
                json.dumps([asdict(agent) for agent in agents])
                for agent in agents:
                    local.get_agent(agent.id)
                    local.get_workspace(agent.workspace_id)
                    status_cache.read_head(checkout)
                    if stop.is_set():
                        break
        finally:
            local.conn.close()

    threads = [threading.Thread(target=contend) for _ in range(competitors)]
    for thread in threads:
        thread.start()
    ready.wait()
    queries = []
    db.conn.set_trace_callback(queries.append)
    began = time.perf_counter()
    try:
        rows = view.snapshot(db, None, read_screens=False, known_git=view.KnownGit(db))
        elapsed = time.perf_counter() - began
    finally:
        db.conn.set_trace_callback(None)
        stop.set()
        for thread in threads:
            thread.join()
        db.conn.close()
    print(json.dumps({"version": version, "threads": competitors, "workspaces": len(workspaces), "shown": len(rows),
                      "queries": len(queries), "seconds": round(elapsed, 6)}))


if __name__ == "__main__":
    main()
