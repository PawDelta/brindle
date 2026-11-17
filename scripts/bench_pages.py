"""Time the daemon reads each page of the desktop app makes when it opens.

Runs only against a throwaway brindle home: set BRINDLE_HOME to a folder that
is not ~/.brindle and holds a copy of the DB (`sqlite3 ~/.brindle/brindle.db
".backup $BRINDLE_HOME/brindle.db"`), config.json and quota.json. Nothing is
launched; routes are called in-process through the ASGI app.

    uv run python scripts/bench_pages.py --home=<throwaway home> [repo root] [rounds]

Cold is each route's first call in a fresh process (after one /meta call, so
the DB is open, as in a running daemon); warm is the best of `rounds` calls
after it. Prints one line per page and route, slowest flagged.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_new_session import _call, _throwaway_home  # noqa: E402


def _pages(repo: str, root: str | None, agent: str | None) -> list[tuple[str, str, dict]]:
    r = {"repo": repo}
    s = f"/v1/sessions/{quote(root, safe='')}" if root else None
    pages: list[tuple[str, str, dict]] = [
        ("Shell", "/v1/meta", {}),
        ("Shell", "/v1/repos", {}),
        ("Shell", "/v1/sessions", r),
        ("Shell", "/v1/snapshot", r),
        ("Shell", "/v1/usage", {}),
        ("Shell", "/v1/library/folders", {}),
        ("Inbox", "/v1/inbox", r),
        ("Inbox", "/v1/permissions/asks", r),
        # The Agents page reads routing twice: the rows at once (learned=0), then
        # the learned picks after the page is up.
        ("Agents", "/v1/routing", {**r, "scope": "user", "learned": "0"}),
        ("Agents*", "/v1/routing", {**r, "scope": "user"}),
        ("Session", "/v1/goal", {**r, **({"root": root} if root else {})}),
        ("Agents", "/v1/profiles", r),
        ("Agents", "/v1/rules", r),
        ("Library", "/v1/analysis-docs", r),
        ("Library", "/v1/library/people", {}),
        ("Knowledge", "/v1/kb/sources", r),
        ("Knowledge", "/v1/decisions", r),
        ("Knowledge", "/v1/kept-visuals", {}),
        ("Knowledge", "/v1/analysis-docs/scopes", r),
        ("Knowledge", "/v1/analysis-docs/results", r),
        ("Knowledge", "/v1/analysis-docs/policy", r),
        ("Knowledge", "/v1/analysis-docs/export-config", r),
        ("Settings", "/v1/config", r),
        ("Settings", "/v1/user-settings", {}),
        ("Settings", "/v1/permissions", {}),
        ("Settings", "/v1/permissions/report", r),
        ("Settings", "/v1/debug", {}),
        ("Settings", "/v1/doc-format", {}),
        ("Settings", "/v1/analysis-docs/switches", {}),
        ("Settings", "/v1/analysis-docs/destination", r),
        ("Settings", "/v1/review-policies", {}),
        ("Settings", "/v1/org-permissions", {}),
        ("Settings", "/v1/integrations/trackers", {}),
        ("Settings", "/v1/github/connection", {}),
        ("Settings", "/v1/providers/installed", {}),
        ("Settings", "/v1/cloud/options", {}),
        ("Settings", "/v1/storage", r),
        ("Account", "/v1/account", r),
        ("Account", "/v1/cost", r),
    ]
    if s:
        pages += [
            ("Session", f"{s}/visuals", {}),
            ("Session", f"{s}/visual-offer", {}),
            ("Session", f"{s}/pr/readiness", {}),
            ("Session", f"{s}/pr/draft", {}),
            ("Session", f"{s}/diff", {}),
            ("Session", f"{s}/tree", {}),
            ("Session", "/v1/analysis-docs/session-summary", {"session": root}),
        ]
    if agent:
        pages += [("Session", f"/v1/agents/{agent}", {}), ("Session", f"/v1/agents/{agent}/transcript", {})]
    return pages


def main(repo: str | None, rounds: int) -> None:
    _throwaway_home()
    from brindle.daemon.app import build_app

    app = build_app("t" * 43)

    async def timed(path: str, query: dict) -> tuple[int, float]:
        t = time.perf_counter()
        status, _ = await _call(app, path, urlencode(query))
        return status, (time.perf_counter() - t) * 1000

    async def run() -> None:
        await _call(app, "/v1/meta")
        _, repos = await _call(app, "/v1/repos")
        chosen = repo or next(x["repo_root"] for x in repos if not x.get("demo"))
        _, sessions = await _call(app, "/v1/sessions", urlencode({"repo": chosen}))
        root = sessions[0]["id"] if sessions else None
        _, snap = await _call(app, "/v1/snapshot", urlencode({"repo": chosen}))
        agent = next((a["id"] for w in snap or [] for a in w.get("agents", [])), None)
        print(f"repo={chosen} root={root} agent={agent} repos={len(repos)} sessions={len(sessions)}", flush=True)
        only = os.environ.get("BENCH_ONLY") or next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--profile=")), None)
        for page, path, query in _pages(chosen, root, agent):
            if only:
                if path != only:
                    continue
                import cProfile
                import pstats

                prof = cProfile.Profile()
                prof.enable()
                await timed(path, query)
                prof.disable()
                pstats.Stats(prof).sort_stats("cumulative").print_stats(45)
                return
            status, cold = await timed(path, query)
            warm = min([(await timed(path, query))[1] for _ in range(rounds)])
            flag = "  SLOW" if warm > 300 or cold > 1000 else ""
            print(f"{page:10} {path[:60]:60} {status} cold {cold:7.0f}ms  warm {warm:7.0f}ms{flag}", flush=True)

    asyncio.run(run())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("--home="):
        os.environ["BRINDLE_HOME"] = sys.argv.pop(1).split("=", 1)[1]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    sys.argv = [sys.argv[0], *[a for a in sys.argv[1:] if not a.startswith("--")]] + flags
    main(sys.argv[1] if len(sys.argv) > 1 else None, int(sys.argv[2]) if len(sys.argv) > 2 else 3)
