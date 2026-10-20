import asyncio
import json
import subprocess
import threading
from unittest.mock import ANY

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import identity_lock, tmux, workspaces
from brindle.daemon import terminal
from brindle.daemon.app import build_app
from brindle.daemon.terminal import WS_NOT_FOUND
from brindle.db import Agent

TOKEN = "t" * 43


class FakeTmux:
    """A tmux server with one live pane, %1, in session ``sess`` (every other
    target is gone), reached both by tmux processes (``calls``) and by the
    daemon's control client (``lines``). ``screen`` is what the pane shows;
    ``print`` makes it print more, as %output lines on the control client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.lines: list[str] = []
        self.procs: list[FakeControl] = []
        self.screen = b"hello\nworld"
        self.attached = 0
        self.print_during_capture = b""
        self.cond = threading.Condition()

    def __call__(self, *args, input=None, check=True, server=None):
        target = args[args.index("-t") + 1] if "-t" in args else None
        with self.cond:
            self.calls.append(args)
            self.cond.notify_all()
        if target not in (None, "%1"):
            if check:
                raise tmux.TmuxError(f"can't find pane: {target}")
            return subprocess.CompletedProcess(args, 1, "", "")
        out = ""
        if args[0] == "display-message":
            out = {"#{session_name}": "sess", tmux.CURSOR_FORMAT: "3 1"}.get(args[-1], "0")
        elif args[0] == "capture-pane":
            out = self.screen.decode() + "\n"
        elif args[0] == "list-clients":
            out = "".join(f"{f}\n" for f in self.flags())
        return subprocess.CompletedProcess(args, 0, out, "")

    def flags(self) -> list[str]:
        return ["attached,focused,control-mode,UTF-8"] * bool(self.procs) + \
            ["attached,UTF-8"] * self.attached

    async def spawn(self, argv: list[str]) -> "FakeControl":
        proc = FakeControl(self, argv)
        self.procs.append(proc)
        proc.reply([], flags=0)         # attach-session's own reply
        return proc

    def print(self, *chunks: bytes) -> None:
        """The pane prints ``chunks``, which tmux hands over together."""
        self.screen += b"".join(chunks)
        self.procs[-1].feed(b"".join(_output(c) for c in chunks))

    def command(self, proc: "FakeControl", line: str) -> None:
        with self.cond:
            self.lines.append(line)
            self.cond.notify_all()
        words = line.split()
        target = words[words.index("-t") + 1] if "-t" in words else None
        if target not in (None, "%1") or words[0] == "nonsense":
            proc.reply([f"can't find pane: {target}".encode()], error=True)
        elif words[0] == "capture-pane":
            if self.print_during_capture:
                self.print(self.print_during_capture)
                self.print_during_capture = b""
            proc.reply(self.screen.split(b"\n"))
        elif words[0] == "display-message":
            proc.reply([b"3 1"])
        elif words[0] == "list-clients":
            proc.reply([f.encode() for f in self.flags()])
        else:
            proc.reply([])

    def wait_for(self, pred) -> None:
        with self.cond:
            assert self.cond.wait_for(pred, timeout=5)


def _output(chunk: bytes) -> bytes:
    escaped = b"".join(b"\\%03o" % b if b < 32 or b == 92 else bytes([b]) for b in chunk)
    return b"%output %1 " + escaped + b"\n"


class FakeControl:
    """The ``tmux -C`` process: commands written to it are answered on its
    stdout, each with its own %begin/%end block, in order with the output."""

    def __init__(self, fake: FakeTmux, argv: list[str]) -> None:
        self.fake, self.argv, self.closed = fake, argv, False
        self.loop = asyncio.get_running_loop()
        self.thread = threading.get_ident()
        self.stdout = asyncio.StreamReader(limit=1 << 24)
        self.stdin = self
        self.exited = self.loop.create_future()
        self.n = 0

    def feed(self, data: bytes) -> None:
        if threading.get_ident() == self.thread:    # from a command: before its reply
            self.stdout.feed_data(data)
        else:
            self.loop.call_soon_threadsafe(self.stdout.feed_data, data)

    def reply(self, lines: list[bytes], error: bool = False, flags: int = 1) -> None:
        self.n += 1
        guard = b"1792476213 %d %d" % (self.n, flags)
        end = b"%error " if error else b"%end "
        self.stdout.feed_data(b"%begin " + guard + b"\n" + b"".join(l + b"\n" for l in lines)
                              + end + guard + b"\n")

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            self.fake.command(self, line)

    def close(self) -> None:
        self.closed = True
        self.stdout.feed_data(b"%exit\n")
        self.stdout.feed_eof()
        self.exited.set_result(None)
        with self.fake.cond:
            self.fake.cond.notify_all()

    async def wait(self) -> int:
        await self.exited
        return 0


@pytest.fixture
def fake(db, repo, tmp_path, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/term").workspace
    said = tmp_path / "w9.jsonl"
    said.write_text(json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "all done\nbye"}]}}) + "\n")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "%1", None, 1000.0))
    db.add_agent(Agent("w9", ws.id, "developer", "claude", None, "assign", "idle", "%9", None, 1000.0,
                       transcript_path=str(said)))
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    fake = FakeTmux()
    monkeypatch.setattr(tmux, "_tmux", fake)
    monkeypatch.setattr(terminal, "spawn", fake.spawn)
    return fake


def connect(client, agent):
    sock = client.websocket_connect(f"/v1/terminal/{agent}").__enter__()
    sock.send_text(TOKEN)
    return sock


def test_viewers_share_one_control_client_whose_seeds_are_ordered_with_its_stream(fake):
    with TestClient(build_app(TOKEN)) as client:
        a = connect(client, "w1")
        assert a.receive_json() == {"type": "terminal", "agent": "w1", "shared": False}
        assert a.receive_bytes() == b"\x1bchello\r\nworld\x1b[0m\x1b[2;4H"
        fake.print(b"\x1b[31mli", b"ve\\")       # handed over together: one frame
        assert a.receive_bytes() == b"\x1b[31mlive\\"
        # b joins while the pane prints "two": the capture answers after it,
        # so it is in b's seed and not in b's stream, and streamed to a.
        fake.print_during_capture = b"two"
        b = connect(client, "w1")
        b.receive_json()
        assert b.receive_bytes() == b"\x1bchello\r\nworld\x1b[31mlive\\two\x1b[0m\x1b[2;4H"
        assert a.receive_bytes() == b"two"
        fake.print(b"more")
        assert a.receive_bytes() == b.receive_bytes() == b"more"
        b.__exit__(None, None, None)
        fake.print(b"still")                     # still streamed for the one viewer left
        assert a.receive_bytes() == b"still"
        a.__exit__(None, None, None)
        fake.wait_for(lambda: fake.procs[-1].closed)
    assert [p.argv[-4:] for p in fake.procs] == [["-C", "attach-session", "-t", "=sess"]]
    assert fake.lines[0] == "refresh-client -f ignore-size"
    assert sum(line.startswith("capture-pane") for line in fake.lines) == 2


@pytest.mark.parametrize("attached", [0, 1])
def test_control_frames_reach_the_pane(fake, attached):
    fake.attached = attached
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, "w1")
        assert sock.receive_json()["shared"] is bool(attached)
        sock.receive_bytes()
        sock.send_text("not json")
        sock.send_json({"type": "key", "text": '-t %2 "$x\\\n'})
        sock.send_json({"type": "key", "keys": ["C-c", "Enter"]})
        sock.send_json({"type": "paste", "text": "-a\nb"})   # "-a": data, not a set-buffer option
        for cols in (80, 100, 120):
            sock.send_json({"type": "resize", "cols": cols, "rows": 40})
        if attached:    # a terminal is attached: its size rules, nothing is resized
            fake.wait_for(lambda: sum(l.startswith("list-clients") for l in fake.lines) >= 2)
        else:       # the first size at once, the last after the throttle, then viewers reseeded
            fake.wait_for(lambda: ("resize-window", "-t", "%1", "-x", "120", "-y", "40") in fake.calls)
            fake.wait_for(lambda: sum(l.startswith("capture-pane") for l in fake.lines) >= 2)
            assert sock.receive_json() == {"type": "resync"}
            assert sock.receive_bytes().startswith(b"\x1bc")
        sent = [l for l in fake.lines if l.split()[0] in ("send-keys", "set-buffer", "paste-buffer")]
        name = sent[2].split()[2]
        assert sent == [
            'send-keys -t %1 -l -- "-t %2 \\"\\$x\\\\\\012"',
            'send-keys -t %1 -- "C-c" "Enter"',
            f'set-buffer -b {name} -- "-a\\012b"',
            f"paste-buffer -p -d -b {name} -t %1",
        ]
        assert [c for c in fake.calls if c[0] in ("set-option", "resize-window")] == (
            [] if attached else [("set-option", "-w", "-t", "%1", "window-size", "manual"),
                                 ("resize-window", "-t", "%1", "-x", "80", "-y", "40"),
                                 ("resize-window", "-t", "%1", "-x", "120", "-y", "40")])
        sock.__exit__(None, None, None)


def test_resize_is_throttled_on_the_leading_edge_and_the_last_size_lands():
    class Clock:
        now = 0.0
        timers: list = []

        def time(self):
            return self.now

        def call_later(self, wait, fn):
            self.timers.append((self.now + wait, fn))

            class Handle:
                cancel = staticmethod(lambda: None)
            return Handle()

    async def go():
        pane, clock, sized = terminal.Pane("%1", None, None), Clock(), []
        pane._loop = clock

        gated, state = {}, {"attached": 0}

        async def run(fn, target, *args):
            if fn is tmux.attached_terminals:
                return state["attached"]
            if fn is tmux.resize_window:
                sized.append(args)
                if "gate" in gated:
                    await gated["gate"].wait()
            return 0
        pane.run = run
        viewer = pane.add()

        async def settle():
            for _ in range(8):
                await asyncio.sleep(0)

        for size in ((80, 24), (90, 24), (100, 24)):
            pane.resize(*size)
        await settle()
        assert sized == [(80, 24)]              # first at once, the rest wait
        assert not viewer.reseed                # a resize is still waiting
        (at, fire), = clock.timers
        assert at == terminal.RESIZE_SECONDS
        clock.now = at
        fire()
        await settle()
        assert sized == [(80, 24), (100, 24)]
        assert viewer.reseed

        # A slow tmux call: later requests wait for it, one at a time, and the
        # last size wins; a repeat of the final size still reseeds.
        viewer.reseed = False
        clock.now += 5
        gate = gated["gate"] = asyncio.Event()
        pane.resize(120, 24)
        await settle()
        pane.resize(130, 24)
        clock.now += 5
        pane.resize(130, 24)
        await settle()
        assert sized[2:] == [(120, 24)] and not viewer.reseed
        gate.set()
        gated.clear()
        await settle()
        assert sized[2:] == [(120, 24), (130, 24)]
        assert viewer.reseed

        # A terminal attaches and leaves: sizing was handed back, so the same
        # size is applied again.
        clock.now += 5
        state["attached"] = 1
        await pane._check_shared()
        state["attached"] = 0
        await pane._check_shared()
        pane.resize(130, 24)
        await settle()
        assert sized[-2:] == [(130, 24), (130, 24)]
        pane._closed = True

    asyncio.run(go())


def test_without_control_mode_the_pane_is_polled_and_typed_into_by_tmux_processes(fake, monkeypatch):
    async def refused(argv):
        raise OSError("no control mode")

    monkeypatch.setattr(terminal, "spawn", refused)
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, "w1")
        sock.receive_json()
        assert sock.receive_bytes() == b"\x1bchello\r\nworld\x1b[0m\x1b[2;4H"
        fake.screen = b"hello\nthere"
        assert sock.receive_bytes() == b"\x1b[?25l\x1b[2;1H\x1b[0m\x1b[2Kthere\x1b[0m\x1b[2;4H\x1b[?25h"
        sock.send_json({"type": "key", "text": "x"})
        fake.wait_for(lambda: ("send-keys", "-t", "%1", "-l", "--", "x") in fake.calls)
        sock.__exit__(None, None, None)
    assert fake.procs == []


def test_control_quote_is_one_argument_tmux_reads_back_as_the_text():
    # tmux's parser undoes \\ \" and \$ and turns \ooo into the byte, so
    # control characters can cross a line-based channel; a NUL can't.
    assert tmux.control_quote('-t %2 "$x\\' + "\n\x7f\x00é") == '"-t %2 \\"\\$x\\\\\\012\\177é"'


def test_coalescing_ends_at_the_byte_threshold_before_the_timer(monkeypatch):
    monkeypatch.setattr(terminal, "COALESCE_SECONDS", 30.0)

    async def run():
        pane = terminal.Pane("%1", None, None)
        viewer = pane.add()
        pane.printed(b"a")
        gathering = asyncio.ensure_future(terminal._coalesce(pane, viewer))
        await asyncio.sleep(0.01)
        assert not gathering.done()                 # a small chunk: still gathering
        pane.printed(b"b" * terminal.COALESCE_BYTES)  # over the threshold: done now
        await asyncio.wait_for(gathering, 1)
        assert pane.ring.since(viewer.pos) == b"a" + b"b" * terminal.COALESCE_BYTES

    asyncio.run(run())


@pytest.mark.parametrize("agent,locked,hello,shown,code", [
    ("w9", None, {"type": "no_pane", "agent": "w9", "reason": "exited", "source": "transcript"},
     b"\x1bcall done\r\nbye", 1000),
    ("w9", "boss@example.com", {"type": "no_pane", "agent": "w9", "reason": "locked",
                                "locked_by": "boss@example.com"}, None, 1000),
    ("nope", None, {"type": "error", "code": "not_found", "message": ANY}, None, WS_NOT_FOUND),
])
def test_without_a_pane_says_so_and_shows_what_is_left(fake, monkeypatch, agent, locked, hello,
                                                       shown, code):
    # locked_by takes the agent's id: handed anything else it answers None (fail-open)
    monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: locked if agent_id == agent else None)
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, agent)
        assert sock.receive_json() == hello
        if shown:
            assert sock.receive_bytes() == shown
        with pytest.raises(WebSocketDisconnect) as closed:
            sock.receive_bytes()
        assert closed.value.code == code
        sock.__exit__(None, None, None)
    assert fake.procs == []


@pytest.mark.serial
def test_a_real_pane_streams_what_it_prints(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/term").workspace
    pane = tmux._tmux("new-session", "-d", "-s", "term", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", pane, None, 1000.0,
                       tmux_server=tmux.current_server()))
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, "w1")
        sock.receive_json()
        sock.receive_bytes()
        # a paste starting with "-" (an option, to tmux's parser) with the
        # quoting control_quote must undo: quotes, $, a backslash, a newline
        sock.send_json({"type": "paste",
                        "text": '--help 2>/dev/null; x="a\\$b"\necho "brindle-$((40+2)) $x"'})
        sock.send_json({"type": "key", "keys": ["Enter"]})
        seen = b""
        while b"brindle-42 a$b" not in seen:
            seen += sock.receive_bytes()
        sock.__exit__(None, None, None)
