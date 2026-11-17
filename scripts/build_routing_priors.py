"""Build routing's measured table (src/brindle/data/routing_priors.json,
version 2) from benchmark records.

    uv run python scripts/build_routing_priors.py RECORDS.jsonl [...] [--tier medium] [--out PATH]

Each record is one run of one profile (bench_workers.py's worker-records.jsonl
and the like). A record counts when ``valid`` isn't false, and gives:

- the profile: ``worker``, ``profile`` or ``reviewer`` (a reviewer row's role
  is "reviewer");
- the role: ``role``, ``tier`` or ``weight``, else ``--tier``; and an optional
  ``kind``, which adds a ``role/kind`` cell beside the role's;
- success: ``merged`` when present, else ``pass_rate`` >= ``--pass``;
- first try: ``first_try`` when present, else success within one review
  round (``rounds`` <= 1, when the row has rounds);
- ``cost_usd``, and ``minutes`` (else ``seconds`` / 60);
- ``from``: the profile this run took over from, for the ``handover`` pairs.

A cell is p (success rate), first_try, usd and minutes (medians), and runs.
Everything else (tiers, token mix, cells the records don't cover) is kept
from ``--base`` (default: the shipped table). The cascade only uses a cell
with at least ``min_runs`` runs.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
from collections import defaultdict
from importlib import resources
from pathlib import Path

from brindle import cascade


def rows(paths: list[str]) -> list[dict]:
    out = []
    for path in paths:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("valid", True) is not False:
                    out.append(row)
    return out


def outcome(row: dict, tier: str | None, pass_at: float) -> tuple | None:
    """(profile, role, kind, success, first_try, usd, minutes, from) of a record, None when it can't say."""
    profile = row.get("worker") or row.get("profile") or row.get("reviewer")
    role = row.get("role") or row.get("tier") or row.get("weight") or ("reviewer" if row.get("reviewer") else tier)
    if not profile or role not in cascade.ROLES:
        return None
    if "merged" in row:
        ok = bool(row["merged"])
    elif isinstance(row.get("pass_rate"), (int, float)):
        ok = row["pass_rate"] >= pass_at
    else:
        return None
    first = bool(row["first_try"]) if "first_try" in row else ok and (row.get("rounds") or 0) <= 1
    usd = row.get("cost_usd")
    minutes = row.get("minutes") if row.get("minutes") is not None else (
        row["seconds"] / 60 if isinstance(row.get("seconds"), (int, float)) else None)
    return profile, role, row.get("kind"), ok, first, usd, minutes, row.get("from")


def cell(runs: list[tuple]) -> dict | None:
    usd = [r[5] for r in runs if isinstance(r[5], (int, float))]
    minutes = [r[6] for r in runs if isinstance(r[6], (int, float))]
    if not usd or not minutes:
        return None
    n = len(runs)
    return {"p": round(sum(r[3] for r in runs) / n, 3), "first_try": round(sum(r[4] for r in runs) / n, 3),
            "usd": round(statistics.median(usd), 4), "minutes": round(statistics.median(minutes), 1), "runs": n}


def build(records: list[dict], base: dict, *, tier: str | None = None, pass_at: float = 1.0,
          measured: str | None = None, source: str = "") -> dict:
    groups: dict[tuple[str, str], list] = defaultdict(list)
    pairs: dict[str, list] = defaultdict(list)
    for row in records:
        o = outcome(row, tier, pass_at)
        if o is None:
            continue
        profile, role, kind, ok = o[0], o[1], o[2], o[3]
        groups[profile, role].append(o)
        if kind:
            groups[profile, f"{role}/{kind}"].append(o)
        if o[7]:
            pairs[f"{o[7]}>{profile}"].append(ok)
    profiles = {name: dict(cells) for name, cells in base.get("profiles", {}).items()}
    for (profile, key), runs in sorted(groups.items()):
        if (c := cell(runs)) is not None:
            profiles.setdefault(profile, {})[key] = c
    handover = dict(base.get("handover", {}))
    handover.update({k: {"p": round(sum(v) / len(v), 3), "runs": len(v)} for k, v in sorted(pairs.items())})
    table = {**base, "version": cascade.VERSION, "measured": measured or datetime.date.today().isoformat(),
             "source": source or base.get("source", ""), "min_runs": base.get("min_runs", 5),
             "profiles": profiles, "handover": handover}
    cascade.parse(table)   # never write a table routing would refuse
    return table


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", nargs="+", help="benchmark records (.jsonl)")
    ap.add_argument("--tier", choices=cascade.TIERS, help="the role of rows that name none")
    ap.add_argument("--pass", dest="pass_at", type=float, default=1.0,
                    help="pass_rate that counts as a success (default 1.0: every hidden test)")
    ap.add_argument("--base", help="the table to start from (default: the shipped one)")
    ap.add_argument("--measured", help="the date the records were measured (default today)")
    ap.add_argument("--out", help="where to write (default: print)")
    args = ap.parse_args(argv)
    base_text = Path(args.base).read_text() if args.base else \
        resources.files("brindle").joinpath("data", "routing_priors.json").read_text(encoding="utf-8")
    table = build(rows(args.records), json.loads(base_text), tier=args.tier, pass_at=args.pass_at,
                  measured=args.measured, source="benchmark records: " + ", ".join(Path(r).name for r in args.records))
    text = json.dumps(table, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
