#!/usr/bin/env python3
"""Live co-editing end to end on one Mac: two brindle daemons, one local Library.

Starts pawdelta-web's local server and live relay (its ``scripts/live_local.sh``,
bound to 127.0.0.1, accounts alice and bob), then two daemons from this
checkout, each with its own temporary BRINDLE_HOME (the ones the script seeds,
under ``--work``; never ~/.brindle, and the desktop app is never started).
Alice shares a design from her daemon and gives bob edit rights; bob opens it
by its Library doc id, as the app's shared-design reader does. Every edit goes
through a daemon's ``/v1/visuals/{id}/live`` socket the way an app window
sends it (Loro updates under the number the daemon lends the window).

    uv run python scripts/live_two_daemons.py \\
        --server-script ~/Projects/pawdelta-web/scripts/live_local.sh

Checks, each printed PASS / FAIL / SKIP with why:

1. alice's edit reaches bob, and bob's reaches alice
2. concurrent edits in one section converge (both windows and the relay)
3. a comment's anchor moves with text inserted before it
4. bob's edits made offline go up when his daemon is back
5. with alice (the author) dropped mid-edit, bob can only comment and suggest
6. a frozen version leaves bob read-only
7. the relay restarted keeps the draft's history (local blob dir)
8. an account not on the live allowlist is refused

The local server signs entitlements with a key it makes at each start, and a
daemon trusts only keys pinned in brindle/pro/keys.py: each test daemon pins
that run's key in its own process, in memory (never written anywhere).

Bob "offline" (4): the Library URL must be the entitlement's issuer, which both
daemons share, so bob's relay socket alone is routed through a TCP gate in this
script (his daemon's process patches where that one socket connects; REST goes
direct). Cutting the gate drops it as a network would; his daemon keeps running,
his window keeps typing into the live socket, and restoring the gate lets his
link reconnect and send its outbox.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATIENCE = 45.0         # seconds a check waits for something to arrive
STEP = 0.2              # between looks
SWEEP_WAIT = 60.0       # a daemon's outbox sweep runs every 15 s; a relay link backs off up to 30 s
LOCAL = "127.0.0.1"
LEDE = "# Live two daemons\n\nShared from alice's daemon."
SECTIONS = [
    {"id": "overview", "title": "Overview", "markdown": "Edit this section from either daemon."},
    {"id": "rollout", "title": "Rollout", "markdown": "The allowlist controls who can join."},
    {"id": "notes", "title": "Notes", "markdown": "Offline notes."},
]
CLOSE_RELOAD = 4205     # brindle.pro.live_docs.RELOAD


class Failed(AssertionError):
    pass


class Skipped(Exception):
    pass


async def pause(seconds: float) -> None:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    handle = loop.call_later(seconds, lambda: done.done() or done.set_result(None))
    try:
        await done
    finally:
        handle.cancel()


async def until(what: str, check, within: float = PATIENCE):
    """``check()`` (sync or async) until it returns something true."""
    end = time.monotonic() + within
    last = None
    while True:
        try:
            got = check()
            if asyncio.iscoroutine(got):
                got = await got
        except (OSError, Failed, urllib.error.URLError) as e:
            got, last = None, e
        if got:
            return got
        if time.monotonic() > end:
            raise Failed(f"{what}: not within {within:.0f}s" + (f" ({last})" if last else ""))
        await pause(STEP)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((LOCAL, port)) == 0


def http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=PATIENCE) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")[:300]}


# -- the Library (pawdelta-web) -------------------------------------------------------------------


class Server:
    def __init__(self, script: Path, work: Path, port: int) -> None:
        self.script, self.work, self.port = script, work, port
        self.out, self.blobs = work / "out", work / "blobs"
        self.proc: subprocess.Popen | None = None
        self.seed: dict = {}
        self.starts = 0

    def start(self) -> None:
        seed = self.out / "seed.json"
        if seed.exists():
            seed.unlink()
        self.starts += 1
        log = open(self.work / f"server-{self.starts}.log", "w")
        self.proc = subprocess.Popen(
            ["bash", str(self.script), "--host", LOCAL, "--bind", LOCAL, "--port", str(self.port),
             "--blob-dir", str(self.blobs), "--out-dir", str(self.out)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    async def ready(self) -> None:
        seed = self.out / "seed.json"
        await until("the local Library to start", lambda: (self.proc.poll() is None or self._died())
                    and seed.exists() and listening(self.port), 90.0)
        self.seed = json.loads(seed.read_text())

    def _died(self):
        raise Failed(f"the local Library exited ({self.proc.returncode}); see {self.work}/server-{self.starts}.log")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(30)
        self.proc = None

    @property
    def base(self) -> str:
        return self.seed["base_url"]

    def account(self, n: int) -> dict:
        return self.seed["accounts"][f"account_{n}"]

    def api(self, method: str, path: str, who: int, body: dict | None = None) -> tuple[int, dict]:
        return http(method, self.base + path, self.account(who)["access_token"], body)

    def draft(self, doc_id: str) -> dict:
        status, got = self.api("GET", f"/analysis-docs/{doc_id}/design/draft", 1)
        if status != 200:
            raise Failed(f"reading the relay's draft: HTTP {status} {got}")
        return got


# -- a brindle daemon -----------------------------------------------------------------------------

PINNED_DAEMON = (
    "import os, sys\n"
    "from brindle.pro import keys\n"
    "keys.PINNED_KEYS[sys.argv[1]] = sys.argv[2]   # this run's local Library key, in this process only\n"
    "gate = os.environ.get('LIVE_RELAY_GATE')\n"
    "if gate:\n"
    "    import asyncio\n"
    "    from brindle.pro import live_docs\n"
    "    class Gated:                               # the relay socket only, through the driver's gate\n"
    "        def __getattr__(self, name):\n"
    "            return getattr(asyncio, name)\n"
    "        async def open_connection(self, host, port, **kw):\n"
    "            return await asyncio.open_connection('127.0.0.1', int(gate), **kw)\n"
    "    live_docs.asyncio = Gated()\n"
    "from brindle.cli import app\n"
    "app(sys.argv[3:], prog_name='brindle')\n")


class Gate:
    """A TCP door between one daemon and the relay: its relay socket goes
    through here (REST calls don't), so the driver can take that machine
    off the relay and bring it back while the daemon keeps running."""

    def __init__(self, target: int) -> None:
        self.target = target
        self.open = True
        self.port = 0
        self.pairs: set[tuple] = set()
        self.server = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, LOCAL, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer) -> None:
        if not self.open:
            writer.transport.abort()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(LOCAL, self.target)
        except OSError:
            writer.transport.abort()
            return
        pair = (writer, up_writer)
        self.pairs.add(pair)

        async def pipe(r, w) -> None:
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            except (OSError, ConnectionError):
                pass
            finally:
                w.close()

        try:
            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        finally:
            self.pairs.discard(pair)

    def cut(self) -> None:
        """The network goes: every connection through here drops (no clean close), new ones fail."""
        self.open = False
        for pair in list(self.pairs):
            for w in pair:
                w.transport.abort()

    def restore(self) -> None:
        self.open = True

    def stop(self) -> None:
        self.cut()
        if self.server is not None:
            self.server.close()


class Daemon:
    def __init__(self, name: str, home: Path, server: Server, work: Path) -> None:
        self.name, self.home, self.server, self.work = name, home, server, work
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.token = ""
        self.starts = 0
        self.gate: Gate | None = None

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BRINDLE_", "TMUX", "CLAUDE", "LIVE_RELAY_GATE"))}
        env.update({"BRINDLE_HOME": str(self.home), "BRINDLE_PRO_BASE_URL": self.server.base,
                    "BRINDLE_PRO_CREDENTIAL_STORE": "file", "BRINDLE_PRO_DEV": "1", "BRINDLE_VISUALS": "1",
                    "PYTHONPATH": str(ROOT / "src")})
        if self.gate is not None:
            env["LIVE_RELAY_GATE"] = str(self.gate.port)
        return env

    def run(self, *args: str) -> str:
        """This script again, in this daemon's home (for what has no route: seeding a plan)."""
        got = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args], env=self.env(),
                             capture_output=True, text=True, check=False)
        if got.returncode != 0:
            raise Failed(f"{self.name}: {' '.join(args)} failed:\n{got.stderr[-2000:]}")
        return got.stdout.strip()

    def start(self) -> None:
        info = self.home / "daemon.json"
        if info.exists():
            info.unlink()
        self.starts += 1
        log = open(self.work / f"daemon-{self.name}-{self.starts}.log", "w")
        seed = self.server.seed
        self.proc = subprocess.Popen(
            [sys.executable, "-c", PINNED_DAEMON, seed["kid"], seed["public_key_hex"], "daemon", "--no-socket", "--tcp"],
            env=self.env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(self.home))

    async def ready(self) -> None:
        info = self.home / "daemon.json"

        def up():
            if self.proc.poll() is not None:
                raise Failed(f"{self.name}'s daemon exited ({self.proc.returncode}); "
                             f"see {self.work}/daemon-{self.name}-{self.starts}.log")
            if not info.exists():
                return None
            got = json.loads(info.read_text())
            return got if got.get("port") and got.get("pid") == self.proc.pid and listening(got["port"]) else None

        got = await until(f"{self.name}'s daemon to start", up, 60.0)
        self.port, self.token = got["port"], got["token"]

    def stop(self, kill: bool = False) -> None:
        """SIGTERM: the daemon closes its relay links cleanly. ``kill``: the
        machine drops off mid-edit (no clean close)."""
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
            self.proc.wait(60)
        self.proc = None

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        status, got = http(method, f"http://{LOCAL}:{self.port}/v1{path}", self.token, body)
        if status != 200:
            raise Failed(f"{self.name} {method} {path}: HTTP {status} {got}")
        return got

    async def call(self, method: str, path: str, body: dict | None = None) -> dict:
        return await asyncio.to_thread(self.api, method, path, body)


# -- an app window on a daemon's live socket ------------------------------------------------------


class Window:
    """What the app's DocEditor does with ``/v1/visuals/{id}/live``: send the
    token, take {"peer"}, {"state"} and the snapshot, write under the lent
    number, open again on RELOAD."""

    def __init__(self, daemon: Daemon, visual: str) -> None:
        self.daemon, self.visual = daemon, visual
        self.conn = None
        self.doc = None
        self.peer: int | None = None
        self.state: dict | None = None
        self.closed: int | None = None
        self.reader: asyncio.Task | None = None
        self.reloads = 0

    async def _open_once(self) -> None:
        from brindle.pro.live_docs import WsConnection

        reader, writer = await asyncio.open_connection(LOCAL, self.daemon.port)
        self.conn = await WsConnection.open(reader, writer, f"{LOCAL}:{self.daemon.port}",
                                            f"/v1/visuals/{self.visual}/live", "")
        self.doc, self.peer, self.state, self.closed = None, None, None, None
        await self.conn.send(self.daemon.token)
        self.reader = asyncio.get_running_loop().create_task(self._read(self.conn))

    async def _read(self, conn) -> None:
        from brindle.crdt import SharedDoc

        while True:
            text = await conn.recv()
            if conn is not self.conn:
                return
            if text is None:
                self.closed = conn.closed_code or 1006
                if self.closed == CLOSE_RELOAD:          # the daemon replaced the text: open again, as the app does
                    self.reloads += 1
                    asyncio.get_running_loop().create_task(self._open_once())
                return
            frame = json.loads(text)
            if "peer" in frame:
                self.peer = None if frame["peer"] is None else int(frame["peer"])
                if self.doc is not None and self.peer is not None:
                    self.doc.set_peer_id(self.peer)
            elif "state" in frame:
                self.state = frame["state"]
            elif "update" in frame:
                data = base64.b64decode(frame["update"])
                if self.doc is None:
                    self.doc = SharedDoc.load(data)
                    if self.peer is not None:
                        self.doc.set_peer_id(self.peer)
                else:
                    self.doc.import_updates(data)

    async def open(self, writer: bool = True) -> Window:
        """Open until the text is in (and, for a writer, a number lent)."""
        await self._open_once()

        async def loaded():
            if self.closed not in (None, CLOSE_RELOAD):
                raise Failed(f"{self.daemon.name}'s live socket closed with {self.closed}")
            return self.doc is not None and (self.peer is not None or not writer)

        await until(f"{self.daemon.name}'s window to load {self.visual}", loaded, SWEEP_WAIT)
        return self

    def text(self, section: str) -> str:
        return self.doc.text(section) if self.doc is not None else ""

    async def edit(self, section: str, offset: int, text: str, delete: int = 0) -> None:
        if self.doc is None or self.peer is None:
            raise Failed(f"{self.daemon.name}'s window can't write: no text or no number")
        before = self.doc.version()
        if delete:
            self.doc.delete(section, offset, delete)
        if text:
            self.doc.insert(section, offset, text)
        await self.conn.send(json.dumps({"update": base64.b64encode(self.doc.export_updates(before)).decode()}))

    async def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                await conn.close(1000)
            except OSError:
                pass
        if self.reader is not None:
            self.reader.cancel()


# -- seeding (run in a daemon's home) -------------------------------------------------------------


def seed_plan() -> None:
    """A session with a supervisor, and a design it proposed: what alice shares."""
    from brindle import plans, visual_offers
    from brindle.db import DB, Agent, Workspace

    db = DB()
    repo = tempfile.mkdtemp(prefix="live-two-daemons-repo-")
    ws = Workspace("ws_live", repo, "main", "main", "main", None, repo, None, "brindle-ws_live", time.time())
    db.add_workspace(ws)
    boss = Agent("boss_live", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, time.time(),
                 headless=1)
    db.add_agent(boss)
    visual_offers.person_spoke(db, boss, "Make a planning doc for the live test.")
    print(plans.propose(db, boss, LEDE, sections=SECTIONS)["id"])


def store_credentials() -> None:
    """live_local.sh writes credentials.json as plain JSON; brindle's file store
    holds base64 of it (keystore._encode). Save what it wrote through that store."""
    from brindle.pro.credentials import FileStore, file_store_dir

    path = file_store_dir() / "credentials.json"
    try:
        creds = json.loads(path.read_text())
    except ValueError:
        return                                          # already brindle's own format
    path.unlink()
    FileStore().save(creds)


def plan_doc(plan_id: str) -> None:
    from brindle.db import DB

    row = DB().conn.execute("SELECT doc_id FROM plan_shares WHERE plan_id=?", (plan_id,)).fetchone()
    print(row["doc_id"] if row else "")


# -- the checks -----------------------------------------------------------------------------------


class Run:
    def __init__(self, server: Server, alice: Daemon, bob: Daemon) -> None:
        self.server, self.alice, self.bob = server, alice, bob
        self.plan = self.doc = ""
        self.a: Window | None = None
        self.b: Window | None = None
        self.results: list[tuple[int, str, str, str]] = []

    async def setup(self) -> None:
        self.server.start()
        await self.server.ready()
        self.plan = self.alice.run("--seed-plan")
        self.bob.gate = Gate(self.server.port)
        await self.bob.gate.start()
        for d in (self.alice, self.bob):
            d.run("--store-credentials")
            d.start()
        for d in (self.alice, self.bob):
            await d.ready()
        org = self.server.seed["org_id"]
        await self.alice.call("POST", f"/plans/{self.plan}/share", {"scope": {"kind": "org", "id": org}})
        self.doc = self.alice.run("--plan-doc", self.plan)
        if not self.doc:
            raise Failed("alice's share left no Library doc")
        # The app has no way to make a teammate a co-editor: grant it as the Library's owner would.
        bob = self.server.account(2)["sub"]
        status, got = await asyncio.to_thread(self.server.api, "PUT", f"/analysis-docs/{self.doc}/design/permissions", 1,
                                              {"permissions": {bob: {"can_edit": True, "can_comment": True,
                                                                     "can_suggest": True}}})
        if status != 200:
            raise Failed(f"granting bob edit rights: HTTP {status} {got}")
        self.a = await Window(self.alice, self.plan).open()
        self.b = await Window(self.bob, self.doc).open()

    async def converged(self, section: str, *needles: str) -> str:
        def same():
            texts = {self.a.text(section), self.b.text(section)}
            return len(texts) == 1 and all(n in self.a.text(section) for n in needles) and self.a.text(section)
        return await until(f"both windows to hold {needles!r} in {section}", same)

    async def relay_holds(self, section: str, text: str) -> None:
        await until(f"the relay's draft of {section} to read {text!r}",
                    lambda: self.server.draft(self.doc)["texts"].get(section) == text)

    # 1
    async def check_round_trip(self) -> str:
        await self.a.edit("overview", 0, "[alice] ")
        await until("bob to see alice's edit", lambda: self.b.text("overview").startswith("[alice] "))
        await self.b.edit("overview", len(self.b.text("overview")), " [bob]")
        await until("alice to see bob's edit", lambda: self.a.text("overview").endswith(" [bob]"))
        text = await self.converged("overview", "[alice]", "[bob]")
        await self.relay_holds("overview", text)
        return repr(text)

    # 2
    async def check_concurrent(self) -> str:
        await asyncio.gather(self.a.edit("rollout", 4, "ALICE "), self.b.edit("rollout", 4, "BOB "))
        await asyncio.gather(self.a.edit("rollout", 0, "<"), self.b.edit("rollout", len(self.b.text("rollout")), ">"))
        text = await self.converged("rollout", "ALICE ", "BOB ", "<", ">")
        await self.relay_holds("rollout", text)
        return repr(text)

    # 3
    async def check_anchor(self) -> str:
        quote = "either daemon"
        body = self.a.text("overview")
        start = body.index(quote)
        plan = await self.alice.call("GET", f"/plans/{self.plan}")
        await self.alice.call("POST", f"/plans/{self.plan}/comments", {
            "rev": plan["rev"], "text": "Which daemons?",
            "anchor": {"kind": "text", "section": "overview", "start": start, "quote": quote}})
        inserted = "Inserted before it. "
        await self.b.edit("overview", 0, inserted)
        await until("alice to see bob's insert", lambda: self.a.text("overview").startswith(inserted))

        async def moved():
            got = await self.alice.call("GET", f"/plans/{self.plan}")
            for t in got.get("threads") or []:
                anchor = t.get("anchor") or {}
                if t.get("text") == "Which daemons?" or anchor.get("quote") == quote:
                    return anchor if anchor.get("start") == start + len(inserted) else None
            raise Failed(f"alice's comment isn't on her plan: {got.get('threads')}")
        anchor = await until("the comment's anchor to move with the insert", moved)
        return f"start {start} -> {anchor['start']}, state {anchor.get('state')}"

    # 4
    async def check_offline(self) -> str:
        """Bob's daemon keeps running and his window stays open; only his relay socket is cut."""
        self.bob.gate.cut()
        typed, first = " (typed by bob, offline)", "Bob: "
        await self.b.edit("notes", len(self.b.text("notes")), typed)
        await self.b.edit("notes", 0, first)
        await pause(3.0)
        relay = self.server.draft(self.doc)["texts"]["notes"]
        if typed in self.a.text("notes") or typed in relay:
            raise Failed("bob's edit got past the cut relay connection")
        self.bob.gate.restore()
        await until("bob's queued edits to reach alice",
                    lambda: typed in self.a.text("notes") and self.a.text("notes").startswith(first), SWEEP_WAIT)
        text = await self.converged("notes", typed, first)
        await self.relay_holds("notes", text)
        return f"held while cut (relay had {relay!r}); after: {text!r}"

    # 5
    async def check_author_offline(self) -> str:
        await self.a.edit("notes", 0, "Alice was here. ")
        await until("bob to see alice's last edit", lambda: self.b.text("notes").startswith("Alice was here. "))
        await self.a.close()
        self.alice.stop(kill=True)                      # dropped mid-edit: no clean close
        await until("bob's window to show the author-offline lock",
                    lambda: (self.b.state or {}).get("lock") == "author_offline")
        before = self.server.draft(self.doc)["texts"]["notes"]
        await self.b.edit("notes", 0, "BOB-LOCKED ")
        await pause(3.0)
        if "BOB-LOCKED" in self.server.draft(self.doc)["texts"]["notes"]:
            raise Failed("the relay took bob's text edit while the author was offline")
        design = await self.bob.call("GET", f"/shared-designs/{self.doc}")
        commented = await self.bob.call("POST", f"/shared-designs/{self.doc}/comments", {
            "rev": design["rev"], "anchor": {"kind": "section", "section": "notes"}, "text": "Comment while locked."})
        suggested = await self.bob.call("POST", f"/shared-designs/{self.doc}/suggestions", {
            "rev": design["rev"], "section": "rollout", "start": 0, "before": "", "after": "Suggested. ",
            "text": "Suggestion while locked."})
        texts = [t.get("text") for t in suggested.get("threads") or []]
        if "Comment while locked." not in json.dumps(commented) or "Suggestion while locked." not in texts:
            raise Failed(f"bob's comment or suggestion isn't on the design: {texts}")
        # The author comes back: the lock ends, and bob's held edit goes up after all.
        self.alice.start()
        await self.alice.ready()
        self.a = await Window(self.alice, self.plan).open()
        await self.a.edit("overview", 0, "Back. ")
        await until("the lock to end on bob's window", lambda: (self.b.state or {}).get("lock") is None)
        await self.converged("notes", "BOB-LOCKED")
        return f"relay notes stayed {before!r}; comment and suggestion saved"

    # 6
    async def check_freeze(self) -> str:
        await self.a.conn.send(json.dumps({"freeze": True}))       # the author's Freeze button (DocPresence)
        await until("alice's window to show the sign-off lock", lambda: (self.a.state or {}).get("lock") == "sign_off")
        frozen = self.server.draft(self.doc)["frozen"]
        if not frozen:
            raise Failed("alice's Freeze left the Library's draft open")
        before = self.server.draft(self.doc)["texts"]["overview"]
        await self.b.edit("overview", 0, "BOB-FROZEN ")
        await until("bob's window to show the sign-off lock", lambda: (self.b.state or {}).get("lock") == "sign_off")
        if "BOB-FROZEN" in self.server.draft(self.doc)["texts"]["overview"] or "BOB-FROZEN" in self.a.text("overview"):
            raise Failed("bob's edit went in while a version was frozen")
        await self.a.conn.send(json.dumps({"reopen": True}))       # and her Reopen
        await until("alice's window to show the draft open", lambda: (self.a.state or {}).get("lock") is None)
        if self.server.draft(self.doc)["frozen"]:
            raise Failed("alice's Reopen left the version frozen")
        await until("bob's held edit to go once reopened", lambda: "BOB-FROZEN" in self.a.text("overview"),
                    SWEEP_WAIT)
        return f"froze r{frozen}; relay overview stayed {before[:30]!r}...; reopened, then delivered"

    # 7
    async def check_relay_restart(self) -> str:
        texts = self.server.draft(self.doc)["texts"]
        old = self.server.seed
        await self.a.close()
        await self.b.close()
        self.alice.stop()
        self.bob.stop()
        self.server.stop()
        self.server.start()
        await self.server.ready()
        status, got = await asyncio.to_thread(self.server.api, "GET", f"/analysis-docs/{self.doc}/design/draft", 1)
        if status != 200:
            same_key = self.server.seed["kid"] == old["kid"]
            raise Failed(f"after the restart the doc is gone: HTTP {status} {got.get('error')} "
                         f"(new doc {self.server.seed['doc_id']}, signing key {'kept' if same_key else 'new'}; "
                         f"blobs kept in {self.server.blobs})")
        if got["texts"] != texts:
            raise Failed(f"the draft changed across the restart: {got['texts']} != {texts}")
        return "draft text intact"

    # 8
    async def check_allowlist(self) -> str:
        allow = set(self.server.seed.get("allowlist") or [])
        seeded = {a["email"] for a in self.server.seed["accounts"].values()}
        if seeded <= allow:
            raise Skipped("live_local.sh allowlists exactly the accounts it seeds: there is no signed-in "
                          "account off the allowlist to try")
        raise Skipped("no non-allowlisted account in the seed")

    async def run(self, only: set[int]) -> None:
        checks = [(1, "alice's edit reaches bob and back", self.check_round_trip),
                  (2, "concurrent edits in one section converge", self.check_concurrent),
                  (3, "a comment anchor moves with an insert before it", self.check_anchor),
                  (4, "bob offline, then back: his queued edits arrive", self.check_offline),
                  (5, "author offline: bob can only comment/suggest", self.check_author_offline),
                  (6, "frozen version: bob read-only", self.check_freeze),
                  (7, "relay restart keeps the history", self.check_relay_restart),
                  (8, "a non-allowlisted account is refused", self.check_allowlist)]
        try:
            await self.setup()
        except Exception as e:  # noqa: BLE001 - every check fails with the setup's reason
            traceback.print_exc()
            self.results = [(n, name, "FAIL", f"setup: {e}") for n, name, _ in checks if n in only]
            return
        for n, name, check in checks:
            if n not in only:
                continue
            try:
                detail = await check()
                self.results.append((n, name, "PASS", detail))
            except Skipped as e:
                self.results.append((n, name, "SKIP", str(e)))
            except Exception as e:  # noqa: BLE001 - one failing check must not hide the rest
                traceback.print_exc()
                self.results.append((n, name, "FAIL", str(e)))
            print(f"[{self.results[-1][2]}] {n}. {name}: {self.results[-1][3]}", flush=True)

    def table(self) -> str:
        rows = ["| # | check | result | detail |", "|---|---|---|---|"]
        rows += [f"| {n} | {name} | {verdict} | {detail.replace('|', '/')} |" for n, name, verdict, detail in self.results]
        return "\n".join(rows)


async def main(args) -> int:
    work = Path(args.work).resolve() if args.work else Path(tempfile.mkdtemp(prefix="live-two-daemons-"))
    if work.exists() and args.fresh:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    server = Server(Path(args.server_script).expanduser().resolve(), work, args.port or free_port())
    alice = Daemon("alice", server.out / "account-1", server, work)
    bob = Daemon("bob", server.out / "account-2", server, work)
    run = Run(server, alice, bob)
    only = {int(n) for n in args.only.split(",")} if args.only else set(range(1, 9))
    try:
        await run.run(only)
    finally:
        for window in (run.a, run.b):
            if window is not None:
                await window.close()
        for d in (alice, bob):
            d.stop()
        if bob.gate is not None:
            bob.gate.stop()
        server.stop()
    print()
    print(run.table())
    print(f"\nlogs and homes: {work}")
    return 0 if all(v != "FAIL" for _, _, v, _ in run.results) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--seed-plan"]:
        seed_plan()
        sys.exit(0)
    if sys.argv[1:2] == ["--store-credentials"]:
        store_credentials()
        sys.exit(0)
    if sys.argv[1:2] == ["--plan-doc"]:
        plan_doc(sys.argv[2])
        sys.exit(0)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server-script", required=True, help="pawdelta-web's scripts/live_local.sh")
    parser.add_argument("--work", help="where homes, blobs and logs go (default: a new temp dir)")
    parser.add_argument("--fresh", action="store_true", help="empty --work first")
    parser.add_argument("--port", type=int, help="the local Library's port (default: a free one)")
    parser.add_argument("--only", help="comma-separated check numbers")
    sys.exit(asyncio.run(main(parser.parse_args())))
