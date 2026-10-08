#!/usr/bin/env python3
"""A fixed benchmark of past tasks, for comparing worker profiles.

    python3 scripts/bench_profiles.py freeze            # pick the task set
    python3 scripts/bench_profiles.py show              # print the briefs to assign
    python3 scripts/bench_profiles.py report --since 2026-10-07

``freeze`` picks finished tasks of this repo from brindle's database (some
merged, some removed unmerged, every weight, each with a finish line) and
writes them to ``.brindle/bench.json`` (gitignored: briefs and paths stay on
this machine), so later runs use the same set. A
supervisor then replays each one with ``assign`` (``agent_profile`` set to the
profile under test, on a throwaway branch). ``report`` totals the replays
started since a date: per profile, how many merged, review rounds and tokens
per run, from the same ``routing_decisions`` and ``history`` rows that
``brindle cost report`` reads.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from brindle.config import brindle_home
from brindle.history import tokens_total

PER_WEIGHT = 4   # tasks per weight in the frozen set (up to 12 in all)


def repo_root() -> str:
    return subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                          text=True, check=True).stdout.strip()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(brindle_home() / "brindle.db")
    db.row_factory = sqlite3.Row
    return db


def bench_path(root: str) -> Path:
    return Path(root) / ".brindle" / "bench.json"


def freeze(db: sqlite3.Connection, root: str) -> list[dict]:
    picked = []
    for weight in ("light", "medium", "heavy"):
        rows = db.execute(
            "SELECT t.id, t.task_text, t.done_when, t.weight, t.files, r.outcome"
            " FROM tasks t JOIN routing_decisions r ON r.task_id = t.id"
            " WHERE t.repo_root = ? AND t.weight = ? AND t.done_when IS NOT NULL"
            " AND r.outcome IN ('merged', 'removed_unmerged')"
            " ORDER BY r.outcome, t.created_at DESC", (root, weight)).fetchall()
        # Half merged, half not, where there are enough of each.
        merged = [r for r in rows if r["outcome"] == "merged"]
        unmerged = [r for r in rows if r["outcome"] != "merged"]
        half = PER_WEIGHT // 2
        chosen = merged[:half] + unmerged[:half]
        chosen += [r for r in rows if r not in chosen][:PER_WEIGHT - len(chosen)]
        picked += [{"id": r["id"], "weight": r["weight"], "task": r["task_text"],
                    "done_when": r["done_when"], "files": json.loads(r["files"] or "[]"),
                    "first_outcome": r["outcome"]} for r in chosen]
    return picked


def report(db: sqlite3.Connection, root: str, since: float) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for r in db.execute(
            "SELECT profile, agent_id, outcome, review_rounds FROM routing_decisions"
            " WHERE repo_root = ? AND ts >= ? AND outcome IS NOT NULL", (root, since)):
        s = stats.setdefault(r["profile"], {"runs": 0, "merged": 0, "rounds": 0, "tokens": 0})
        s["runs"] += 1
        s["merged"] += r["outcome"] == "merged"
        s["rounds"] += r["review_rounds"] or 0
        for (tokens,) in db.execute("SELECT tokens FROM history WHERE agent_id = ?"
                                    " AND kind = 'worker_result'", (r["agent_id"],)):
            s["tokens"] += tokens_total(tokens)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("freeze")
    sub.add_parser("show")
    rep = sub.add_parser("report")
    rep.add_argument("--since", required=True, help="YYYY-MM-DD: replays started on or after")
    args = ap.parse_args(argv)
    root, db = repo_root(), connect()
    path = bench_path(root)

    if args.cmd == "freeze":
        tasks = freeze(db, root)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(tasks, indent=2) + "\n")
        print(f"froze {len(tasks)} tasks into {path}")
    elif args.cmd == "show":
        if not path.exists():
            print("no benchmark yet: run `freeze` first", file=sys.stderr)
            return 1
        for t in json.loads(path.read_text()):
            print(f"--- {t['id']} ({t['weight']}, first run {t['first_outcome']})\n"
                  f"{t['task']}\nFinish line: {t['done_when']}\n")
    else:
        since = datetime.fromisoformat(args.since).timestamp()
        stats = report(db, root, since)
        if not stats:
            print("no finished runs since then")
        for profile, s in sorted(stats.items()):
            print(f"{profile:<20} {s['merged']}/{s['runs']} merged, "
                  f"{s['rounds'] / s['runs']:.1f} review rounds, "
                  f"{s['tokens'] // s['runs']:,} tokens per run (cache reads included)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
