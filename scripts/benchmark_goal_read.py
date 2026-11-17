"""Measure goal reads on an isolated backup of the owner's database.

Run with the worktree's Python environment and the source tree on sys.path.
Checkout paths are mapped to this worktree (one spelling per workspace) so
the benchmark never reads another session's checkout or mutates live data.
"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brindle import autopilot, view
from brindle.db import DB


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("copy", type=Path)
    parser.add_argument("root")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--discard-copy", action="store_true")
    args = parser.parse_args()
    if args.copy.resolve() == args.database.resolve():
        parser.error("the benchmark copy must differ from the source database")
    if not args.reuse:
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(args.copy) as target:
                source.backup(target)
    db = DB(args.copy)
    checkout = str(Path.cwd())
    for i, ws in enumerate(db.find_workspaces()):
        db.conn.execute("UPDATE workspaces SET path=? WHERE id=?", (checkout + "/." * (i + 1), ws.id))
    db.conn.commit()
    root = db.get_agent(args.root)
    repo = db.get_workspace(root.workspace_id).repo_root
    print(json.dumps({"agents": len(db.list_agents()), "workspaces": len(db.find_workspaces()),
                      "events": db.conn.execute("SELECT count(*) FROM events").fetchone()[0]}))
    for name, read in (
        ("goal_activity", lambda: autopilot.goal_activity(db, args.root)),
        ("autopilot_entry", lambda: view.autopilot_entry(db, repo, root_id=args.root)),
        ("goal_session_selection", lambda: view.autopilot_root(db, repo, root_id=args.root)),
    ):
        samples = []
        for _ in range(5):
            began = perf_counter()
            read()
            samples.append(round(perf_counter() - began, 6))
        print(json.dumps({"part": name, "seconds": samples}))
    db.conn.close()
    if args.discard_copy:
        args.copy.unlink()


if __name__ == "__main__":
    main()
