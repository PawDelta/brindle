"""``WS /v1/events``: the bus, live.

After the guard has taken the token (the socket's first frame), the client gets
every bus event above ``?since=<id>`` (replayed, then tailed every 250 ms),
plus what no process publishes: a liveness sweep every 2 s, a git sweep every
5 s per workspace, and the Pro status poller's bar text and org notices. When
the client's ``since`` can't be honoured (events pruned, or from another
database) it is told to ``resnapshot``: fetch the snapshot and inbox afresh."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from concurrent.futures import ThreadPoolExecutor

from starlette.websockets import WebSocket, WebSocketDisconnect

from brindle import airgap, bus

log = logging.getLogger(__name__)

TAIL_SECONDS = 0.25
LIVENESS_SECONDS = 2.0
GIT_SECONDS = 5.0
PAGE = 500
connected = 0       # open /v1/events clients: with none, nobody is there to answer a held request


class Feed:
    """What one client is told, as plain dicts. Every method is synchronous and
    blocking (SQLite, tmux, git): the route runs them on one worker thread."""

    def __init__(self, db, since: int = 0, repo_root: str | None = None, poller=None,
                 panes=None, git_status=None) -> None:
        self.db, self.repo_root, self.poller = db, repo_root, poller
        self.last = since
        self._panes = panes
        self._git_status = git_status
        self._alive: dict[str, bool] | None = None
        self._git: dict[str, dict] = {}
        self._bar: object = False        # nothing sent yet (None is a real "no bar")
        self._due = {"live": 0.0, "git": 0.0}

    def start(self) -> list[dict]:
        """The replay: events above ``since``. A ``since`` that is ahead of the
        table, or behind its oldest row (pruned), means the client has lost
        events, so it resnapshots."""
        newest = self.db.conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        oldest = self.db.conn.execute("SELECT MIN(id) FROM events").fetchone()[0]
        gap = self.last > newest or (self.last > 0 and oldest is not None and oldest > self.last + 1)
        if self.last == 0 and oldest is not None and oldest > 1:
            gap = True      # asked for everything, but the start is pruned away
        if gap:
            self.last = newest
            return [{"type": "resnapshot", "reason": "events_lost"}]
        return self.events()

    def events(self) -> list[dict]:
        out: list[dict] = []
        while True:
            rows = bus.tail(self.db, self.last, repo_root=self.repo_root, limit=PAGE)
            if not rows:
                break
            self.last = rows[-1]["id"]
            out += [{"type": "event", **r} for r in rows]
            if len(rows) < PAGE:
                break
        if self.repo_root is not None:      # the filter hides rows: step over them
            row = self.db.conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
            self.last = max(self.last, row)
        return out

    def tick(self, now: float) -> list[dict]:
        out = self.events()
        if now >= self._due["live"]:
            self._due["live"] = now + LIVENESS_SECONDS
            out += self._liveness()
        if now >= self._due["git"]:
            self._due["git"] = now + GIT_SECONDS
            out += self._git_sweep()
        out += self._status()
        return out

    def _liveness(self) -> list[dict]:
        from brindle import agents, tmux

        panes = self._panes() if self._panes else tmux.list_panes()
        alive = {a.id: agents.is_alive(a, panes) for a in self.db.list_agents()
                 if a.status != "done"
                 and (self.repo_root is None or self._repo_of(a.workspace_id) == self.repo_root)}
        was, self._alive = self._alive, alive
        if was is None:
            return [{"type": "liveness", "agents": alive}]
        changed = {k: v for k, v in alive.items() if was.get(k) != v}
        gone = [k for k in was if k not in alive]
        return [{"type": "liveness", "agents": changed, "gone": gone}] if changed or gone else []

    def _repo_of(self, workspace_id: str) -> str | None:
        ws = self.db.get_workspace(workspace_id)
        return ws.repo_root if ws else None

    def _git_sweep(self) -> list[dict]:
        from brindle import status_cache

        status = self._git_status or status_cache.cached_status
        out = []
        for ws in self.db.find_workspaces():
            if self.repo_root is not None and ws.repo_root != self.repo_root:
                continue
            try:
                st = dataclasses.asdict(status(ws.path, ws.base_branch))
            except Exception:  # noqa: BLE001 - a vanished worktree is the liveness cull's business
                continue
            if self._git.get(ws.id) != st:
                self._git[ws.id] = st
                out.append({"type": "git", "workspace_id": ws.id, "status": st})
        return out

    def _status(self) -> list[dict]:
        saved = getattr(self.poller, "saved", None)
        if self.poller is None:
            return []
        from brindle.pro import status

        bar = status.bar_text(saved)
        notices = status.unread(saved)
        state = (bar, tuple(n.get("id") for n in notices))
        if state == self._bar:
            return []
        self._bar = state
        return [{"type": "status", "bar": list(bar) if bar else None, "notices": notices}]


def start_poller():
    """The daemon's one org status poller, started; None when brindle isn't
    signed in or is in air-gap mode (nothing to poll then)."""
    try:
        from brindle.pro import license, status

        if airgap.enabled():
            return None
        license.current(refresh=False)  # raises when not logged in: nothing to renew
        poller = status.Poller()
        return poller if poller.start() else None
    except Exception:  # noqa: BLE001 - no license, no Pro: no poll
        return None


async def events(ws: WebSocket) -> None:
    from brindle.db import DB
    from brindle.pro import status

    try:
        since = max(0, int(ws.query_params.get("since", "0")))
    except ValueError:
        since = 0
    global connected
    await ws.accept()
    connected += 1
    loop = asyncio.get_running_loop()
    # SQLite connections stay on their thread: one worker owns this client's.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brindle-events")
    db: DB | None = None
    disconnected: asyncio.Future | None = None

    def open_feed() -> Feed:
        nonlocal db
        db = DB()
        return Feed(db, since, ws.query_params.get("repo"), getattr(ws.app.state, "poller", None))

    def close_db() -> None:
        if db is not None:
            db.conn.close()

    try:
        feed = await loop.run_in_executor(pool, open_feed)
        disconnected = asyncio.ensure_future(_wait_for_disconnect(ws))
        for message in await loop.run_in_executor(pool, feed.start):
            await ws.send_json(message)
        while not disconnected.done():
            status.touch()      # an open app is activity: keeps the org poll brisk
            for message in await loop.run_in_executor(pool, feed.tick, loop.time()):
                await ws.send_json(message)
            await asyncio.wait({disconnected}, timeout=TAIL_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        connected -= 1
        if disconnected is not None:
            disconnected.cancel()
        try:
            # Queue cleanup on the owner thread even if cancellation arrived
            # while open_feed/tick was still running. Never close SQLite on
            # the event loop's thread or leave it to garbage collection.
            await asyncio.shield(loop.run_in_executor(pool, close_db))
        finally:
            pool.shutdown(wait=False)


async def _wait_for_disconnect(ws: WebSocket) -> None:
    """Returns when the client closes (frames it sends are ignored)."""
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return
