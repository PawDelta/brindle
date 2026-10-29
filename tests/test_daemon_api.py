import asyncio
import json
import os
import resource
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, Request, TextMessage

from brindle import cost, doctor, identity_lock, keystore, knowledge, procs, tmux, workspaces
from brindle import kb as kb_mod
from brindle.daemon import events, lifecycle, peer, terminal
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED, Guard
from brindle.db import Agent, Workspace
from brindle.pro import license

TOKEN = "t" * 43


def _get(path: str, headers: dict[str, str], query: str = "", peer_pid: int | None = None) -> tuple[int, dict]:
    """One HTTP request straight through the ASGI app (``peer_pid``: as if
    that process called on the Unix socket)."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    if peer_pid is not None:
        scope[peer.PEER_PID] = peer_pid
    asyncio.run(build_app(TOKEN, port=8123)(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(body)


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"}, 403),
    ({"Authorization": f"Bearer {TOKEN}", "Origin": "tauri://localhost"}, 200),
])
def test_requests_need_the_token_and_no_foreign_origin(headers, status):
    code, body = _get("/v1/meta", headers)
    assert code == status
    assert ("api_version" in body) == (status == 200)


@pytest.mark.variant("a token sent in the URL must never open the socket, only the first frame")
@pytest.mark.parametrize("query,frame,reached", [
    (f"token={TOKEN}".encode(), "wrong", False),    # a token in the URL counts for nothing
    (b"", TOKEN, True),
])
def test_websocket_token_is_its_first_frame(query, frame, reached):
    seen: list[dict] = []
    sent: list[dict] = []
    incoming = [{"type": "websocket.connect"},
                {"type": "websocket.receive", "text": frame},
                {"type": "websocket.receive", "text": "hello"}]

    async def route(scope, receive, send):
        seen.append(await receive())
        await send({"type": "websocket.accept"})
        seen.append(await receive())

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/v1/events", "query_string": query, "headers": []}
    asyncio.run(Guard(route, TOKEN)(scope, receive, send))
    assert [m["type"] for m in sent].count("websocket.accept") == 1
    if reached:
        assert seen == [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "hello"}]
    else:
        assert seen == [] and sent[-1] == {"type": "websocket.close", "code": WS_UNAUTHORIZED}


def test_tcp_start_binds_loopback_only_and_keeps_its_files_private(monkeypatch):
    # A short directory: a Unix socket's path is limited to ~104 bytes.
    d = Path(tempfile.mkdtemp(prefix="bd-"))
    try:
        os.chmod(d, 0o755)
        socks, info = lifecycle.prepare(TOKEN, unix=True, tcp=True, sock_path=d / "daemon.sock")
        try:
            assert socks[1].getsockname() == ("127.0.0.1", info["port"])
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
            assert stat.S_IMODE(os.stat(d / "daemon.sock").st_mode) == 0o600
            assert stat.S_IMODE(os.stat(lifecycle.info_path()).st_mode) == 0o600
            assert lifecycle.read_info() == info
        finally:
            for s in socks:
                s.close()
            lifecycle.cleanup(info)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    monkeypatch.setattr(lifecycle, "HOST", "0.0.0.0")
    with pytest.raises(lifecycle.DaemonError, match="loopback only"):
        lifecycle.bind_tcp()


def _ws_over_tcp(port: int, first: str) -> object:
    """Open WS /v1/events on the daemon's TCP port and send ``first`` as the
    first frame: the close code, or the type of the first message back."""
    ws = WSConnection(ConnectionType.CLIENT)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(ws.send(Request(host=f"127.0.0.1:{port}", target="/v1/events?since=999999")))
        while True:
            ws.receive_data(s.recv(4096) or None)
            for event in ws.events():
                if isinstance(event, AcceptConnection):
                    s.sendall(ws.send(TextMessage(first)))
                elif isinstance(event, CloseConnection):
                    return event.code
                elif isinstance(event, TextMessage):
                    return json.loads(event.data)["type"]


def test_tcp_daemon_needs_the_token_on_requests_and_as_the_ws_first_frame(tmp_path, monkeypatch):
    # The Windows app's way in: WSL2 forwards 127.0.0.1, and the token is read from daemon.json over \\wsl$.
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRINDLE_HOME", "relative-home")     # a UNC path needs it made absolute
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: {})
    socks, info = lifecycle.prepare(TOKEN, unix=False, tcp=True)
    conn = lifecycle.connection(lifecycle.read_info())
    assert conn["url"] == f"http://127.0.0.1:{info['port']}" and "token" not in conn
    expected = os.path.join(os.getcwd(), "relative-home", "daemon.json").replace("/", "\\")
    assert conn["windows_info_path"] == "\\\\wsl$\\Ubuntu" + expected
    socks[0].listen()   # connections made before the server is up wait in the backlog
    server = lifecycle.make_server(build_app(TOKEN, port=info["port"]))
    thread = threading.Thread(target=asyncio.run, args=(server.serve(sockets=socks),))
    thread.start()
    try:
        codes = []
        for headers in ({}, {"Authorization": "Bearer wrong"}, AUTH):
            http = HTTPConnection("127.0.0.1", info["port"], timeout=5)
            http.request("GET", "/v1/meta", headers=headers)
            codes.append(http.getresponse().status)
            http.close()
        assert codes == [401, 401, 200]
        assert _ws_over_tcp(info["port"], "wrong") == WS_UNAUTHORIZED
        assert _ws_over_tcp(info["port"], TOKEN) == "resnapshot"
    finally:
        server.should_exit = True
        thread.join(10)
        for s in socks:
            s.close()
        lifecycle.cleanup(info)


@pytest.mark.variant("real incident: a staff build's first start after an update waited on the old "
                     "daemon finishing requests its quit app had left")
def test_a_stopping_daemon_waits_only_briefly_for_a_request_still_running(monkeypatch):
    # An app that quits for its update leaves its slow requests (a provider's sign-in probe) running,
    # and the next app waits on this daemon's exit to start its own (daemon.rs stop_daemon).
    monkeypatch.setattr(lifecycle, "SHUTDOWN_GRACE", 0.05)
    asked = threading.Event()

    async def never_answers(scope, receive, send):
        asked.set()
        await asyncio.Event().wait()

    sock = lifecycle.bind_tcp()
    sock.listen()
    server = lifecycle.make_server(never_answers)
    thread = threading.Thread(target=asyncio.run, args=(server.serve(sockets=[sock]),))
    thread.start()
    try:
        with socket.create_connection(sock.getsockname()) as s:
            s.sendall(b"GET /v1/meta HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            assert asked.wait(10)
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "still waiting on the request"
    finally:
        server.force_exit = True
        thread.join(10)
        sock.close()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def seeded(db, repo, monkeypatch):
    """A repo with a session (root + worker), a decision, a KB source and a locked worker."""
    ws = workspaces.create(db, str(repo), "feat/read").workspace
    now = 1000.0
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, now))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "r1", "assign", "waiting", "@2", None, now))
    db.add_agent(Agent("w2", ws.id, "developer", "claude", "r1", "assign", "idle", "@3", None, now))
    db.add_autopilot("r1")
    db.update_autopilot("r1", goal="ship the thing")
    key = knowledge.repo_key(str(repo))
    knowledge.record_decision(db, key, "storage", "use sqlite", "simple")
    kb_mod.add_source(db, key, "folder", str(repo))
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: "other@example.com" if a == "w2" else None)
    monkeypatch.setattr(doctor, "checks", lambda r, db=None: [doctor.Check("ok", "python", "3.12")])
    monkeypatch.setattr(cost, "require_entitled", lambda: None)
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"@1": True, "@2": True, "@3": True})
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "window_alive", lambda w, server=None: True)

    def no_license(**kw):
        raise license.LicenseError("not logged in")
    current_license = license.current
    monkeypatch.setattr(license, "current", no_license)
    return {"repo": str(repo), "ws": ws.id, "current_license": current_license}


@pytest.mark.parametrize("path,check", [
    ("/v1/repos", lambda b, s: [r["repo_root"] for r in b] == [s["repo"]]),
    ("/v1/sessions", lambda b, s: [(x["id"], x["goal"]) for x in b] == [("r1", "ship the thing")]),
    ("/v1/snapshot", lambda b, s: [a["id"] for a in b[0]["agents"] if a["id"] == "w1"] == ["w1"]),
    ("/v1/inbox", lambda b, s: [(i["kind"], i["agent_id"]) for i in b] == [("waiting", "w1")]),
    ("/v1/goal", lambda b, s: b["root_id"] == "r1" and b["goal"] == "ship the thing"),
    ("/v1/agents/w1", lambda b, s: b["status"] == "waiting" and b["parent_id"] == "r1"),
    ("/v1/agents/w2", lambda b, s: b["locked"] == "other@example.com" and b["tokens"] is None),
    ("/v1/agents/nope", lambda b, s: b["error"]["code"] == "not_found"),
    ("/v1/workspaces/{ws}/diff", lambda b, s: b["base"] == "main" and b["diff"] == ""),
    ("/v1/decisions", lambda b, s: [d["topic"] for d in b] == ["storage"]),
    ("/v1/kb/sources", lambda b, s: [x["kind"] for x in b] == ["folder"]),
    ("/v1/profiles", lambda b, s: "developer" in [p["name"] for p in b] and "env" not in b[0]),
    ("/v1/config", lambda b, s: "repo" in b and b["repo"]["default_agent"] == "developer"),
    ("/v1/doctor", lambda b, s: b == [{"level": "ok", "name": "python", "detail": "3.12"}]),
    ("/v1/account", lambda b, s: b == {"signed_in": False, "name": None, "email": None, "plan": None, "orgs": []}),
    ("/v1/cost", lambda b, s: b["days"] == 30 and b["total"]["dollars"] == 0),
], ids=["repos", "sessions", "snapshot", "inbox", "goal", "agent", "agent-locked", "agent-missing", "diff",
        "knowledge-decisions", "knowledge-sources", "profiles", "config", "doctor", "account", "cost"])
def test_read_routes_answer_from_the_db(seeded, path, check):
    code, body = _get(path.format(ws=seeded["ws"]), AUTH, f"repo={seeded['repo']}")
    assert code in (200, 404)
    assert check(body, seeded)


def test_account_identity_is_cached_with_orgs_and_fails_soft_offline(brindle_home, monkeypatch):
    from brindle.pro import auth
    from brindle.pro.account import ProAccount

    ent = license.Entitlement("user", "org", "pro", "active", frozenset(), 1, 0, 9999999999, "key")
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    reads = []

    def me(*args):
        reads.append("me")
        if len(reads) > 1:
            raise auth.AuthError("offline", code="transport")
        return {"name": "Sam Rivera", "email": "sam@example.com"}

    def orgs(*args):
        reads.append("orgs")
        raise auth.AuthError("offline", code="transport")

    monkeypatch.setattr(auth, "me", me)
    monkeypatch.setattr(ProAccount, "orgs", orgs)
    for _ in range(2):
        code, body = _get("/v1/account", AUTH)
        assert code == 200 and (body["name"], body["email"]) == ("Sam Rivera", "sam@example.com")
    assert reads == ["me", "orgs"]
    license.clear_cache()
    for _ in range(2):
        code, body = _get("/v1/account", AUTH)
        assert code == 200 and body["signed_in"] and body["name"] is None and body["email"] is None
    assert reads == ["me", "orgs", "me", "orgs"]


def test_usage_lists_every_provider_and_flags_the_near_ones_worst_first(brindle_home, monkeypatch):
    import time

    from brindle import quota

    now = time.time()
    store = {
        # the fullest window resets tomorrow; the emptier one resets in ten minutes
        "claude": quota.Quota("claude", [quota.Window(40.0, now + 86400, 10080),
                                         quota.Window(20.0, now + 600, 300)], None, now - 5, "status line"),
        "codex": quota.Quota("codex", [quota.Window(85.0, now + 86400, 10080)], None, now - 9, "rollout"),
        "antigravity": quota.Quota("antigravity", [], now + 600, now - 1, "limit error"),
    }
    monkeypatch.setattr(quota, "get", store.get)
    code, body = _get("/v1/usage", AUTH)
    assert code == 200
    by = {p["provider"]: p for p in body["providers"]}
    assert list(by) == list(quota.PROVIDERS)
    assert by["claude"]["known"] and not by["claude"]["limited"]
    assert by["claude"]["windows"][0] == {"used": 40.0, "resets_at": now + 86400, "minutes": 10080,
                                          "name": "weekly", "short": "wk"}
    assert by["claude"]["updated_at"] == now - 5
    assert by["antigravity"]["limited"] and by["antigravity"]["limited_until"] == now + 600
    assert by["native"] == {"provider": "native", "known": False, "limited": False, "windows": [],
                            "limited_until": None, "updated_at": None}
    assert [(n["provider"], n["reason"]) for n in body["near"]] == [
        ("antigravity", "limited"), ("codex", "high"), ("claude", "resets_soon")]
    assert body["near"][2]["resets_at"] == now + 600


@pytest.mark.parametrize("kind", ["read", "idle", "peer", "terminal", "events", "doctor", "doctor-owned",
                                  "preflight", "spend-events", "slow-check"])
def test_daemon_operations_close_sqlite_connections(brindle_home, monkeypatch, kind):
    opened, closed = [], []
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.append(self)

    def tracked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs, factory=Connection)
        opened.append(conn)   # retain connections so GC cannot hide a leak
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(tmux, "list_panes", lambda *a: tmux.PaneSnapshot(tmux.current_server()))
    if kind == "read":
        for _ in range(30):
            assert _get("/v1/repos", AUTH) == (200, [])
            assert _get("/v1/agents/missing", AUTH)[0] == 404
    elif kind == "idle":
        assert lifecycle._agents_running() is False
    elif kind == "peer":
        assert peer._agent_panes() == {}
    elif kind == "terminal":
        monkeypatch.setattr(tmux, "tagged_pane_ids", lambda keys, server=None: [])
        assert terminal.reset_window_sizes() == 0
    elif kind == "preflight":
        from brindle import git, preflight, provider_homes

        repo = str(brindle_home.parent)
        monkeypatch.setattr(git, "toplevel", lambda path: repo)
        monkeypatch.setattr(git, "main_repo_root", lambda path: repo)
        monkeypatch.setattr(preflight, "check", lambda *a, **k: [])
        monkeypatch.setattr(provider_homes, "confirm_login", lambda provider: None)
        for _ in range(10):
            assert _get("/v1/providers/claude/preflight", AUTH, f"repo={repo}") == (200, [])
    elif kind == "spend-events":
        from brindle import budget, config
        from brindle import events as spend_events

        monkeypatch.setattr(license, "has", lambda feature: True)
        monkeypatch.setattr(budget, "worker_spend", lambda *a: 1.0)
        monkeypatch.setattr(spend_events, "_dispatch", lambda *a: None)
        ws = SimpleNamespace(id="ws", repo_root=str(brindle_home.parent), branch="main")
        worker = Agent("w", "ws", "", "", None, "assign", "done", "", None, 0.0)
        spend_events.emit(config.RepoConfig(), "remove", ws, worker)
        spend_events.emit_periodic_cost(config.RepoConfig(), ws.repo_root)
    elif kind == "slow-check":
        from brindle import gates

        ws = SimpleNamespace(id="missing")
        gates._report_slow(str(brindle_home / "brindle.db"), ws, "check", 1.0, 2.0, 10)
    elif kind.startswith("doctor"):
        from brindle import company_identity, legacy, signin_pause, updates

        # Keep the actual database probes; fake unrelated CLI/network diagnostics.
        monkeypatch.setattr(doctor, "_tool", lambda *a, **k: doctor.Check("ok", a[0], ""))
        for name in ("signin_checks", "credential_checks", "cloud_checks", "daemon_checks", "native_checks",
                     "override_checks", "quota_checks", "airgap_checks", "pro_checks", "managed_checks"):
            monkeypatch.setattr(doctor, name, lambda *a, **k: [])
        monkeypatch.setattr(company_identity, "checks", lambda: [])
        monkeypatch.setattr(signin_pause, "checks", lambda: [])
        monkeypatch.setattr(updates, "notice", lambda **k: None)
        monkeypatch.setattr(legacy, "findings", lambda repo: [])
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        monkeypatch.setattr(procs, "table", lambda: {})
        if kind == "doctor":
            for _ in range(10):
                assert _get("/v1/doctor", AUTH)[0] == 200
            assert len(opened) == 10    # each route's DB serves both database probes
        else:
            assert doctor.checks(None)
    else:
        incoming = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]

        async def receive():
            return incoming.pop(0)

        async def send(message):
            if message["type"] == "websocket.send":
                raise WebSocketDisconnect()   # failure sending the initial replay

        ws = WebSocket({"type": "websocket", "path": "/v1/events", "headers": [],
                        "query_string": b"since=999", "app": SimpleNamespace(state=SimpleNamespace())}, receive, send)
        asyncio.run(events.events(ws))
    assert opened and set(opened) == set(closed)


@pytest.mark.parametrize("soft,hard,refused", [
    (256, 4096, False), (256, resource.RLIM_INFINITY, False),
    (256, 4096, True), (100000, 100000, False),
])
def test_daemon_start_raises_file_limit_before_binding(monkeypatch, soft, hard, refused):
    limit = [soft, hard]

    def setlimit(which, value):
        if refused:
            raise OSError("not permitted")
        limit[:] = value

    class Started(Exception):
        pass

    def prepare(*args, **kwargs):
        if refused or soft == hard:
            assert limit[0] == soft
        else:
            assert soft < limit[0] <= (hard if hard != resource.RLIM_INFINITY else 65536)
        assert limit[1] == hard
        raise Started()

    monkeypatch.setattr(resource, "getrlimit", lambda which: tuple(limit))
    monkeypatch.setattr(resource, "setrlimit", setlimit)
    monkeypatch.setattr(lifecycle, "prepare", prepare)
    with pytest.raises(Started):
        lifecycle.serve()


@pytest.mark.parametrize("caller,refused_as,holds_for", [
    (310, "w1", {"w1"}),     # a command under w1's pane, however deep
    # BRINDLE_AGENT_ID in its own environment: refused, but it holds no request as w2,
    # since a process's word about itself is no proof (permission holds).
    (420, "w2", set()),
    (410, None, set()),     # a person's terminal
    (999, "can't be checked", set()),   # exited before the check: fire and forget
])
def test_peer_agent_callers_are_refused_on_the_unix_socket(seeded, monkeypatch, caller, refused_as, holds_for):
    table = {p.pid: p for p in [
        procs.Proc(1, 0, "/sbin/launchd"),
        procs.Proc(100, 1, "tmux new-session -d"),
        procs.Proc(200, 100, "claude --model opus"),
        procs.Proc(300, 200, "/bin/zsh -c pytest"),
        procs.Proc(310, 300, "curl --unix-socket daemon.sock HOME=/u"),
        procs.Proc(400, 1, "-zsh HOME=/u"),
        procs.Proc(410, 400, "brindle daemon status HOME=/u"),
        procs.Proc(420, 400, "curl --unix-socket daemon.sock HOME=/u BRINDLE_AGENT_ID=w2"),
    ]}
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(tmux, "window_pids", lambda w, server=None: {"@1": [150], "@2": [200]}.get(w, []))
    code, body = _get("/v1/meta", AUTH, peer_pid=caller)
    if refused_as:
        assert code == 403 and body["error"]["code"] == "agent_caller"
        assert refused_as in body["error"]["message"]
    else:
        assert code == 200
    assert peer.caller_agents(caller) == holds_for


def test_peer_pid_names_the_unix_socket_caller():
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        assert peer.peer_pid(a) == os.getpid()


def test_window_size_cleanup_unsets_manual_only_on_brindles_own_windows_once(db, monkeypatch):
    # The old streaming daemon left window-size manual on windows it sized.
    db.add_workspace(Workspace("ws", "/r", "root", "main", "main", None, "/r", None, "brindle-root", 1.0))
    db.add_agent(Agent("w1", "ws", "developer", "claude", None, "assign", "idle", "%1", None, 1.0))
    db.add_agent(Agent("w2", "ws", "developer", "claude", None, "assign", "idle", "%1", None, 1.0,
                       tmux_server="other"))
    db.add_agent(Agent("w3", "ws", "developer", "claude", None, "assign", "done", "%2", None, 1.0,
                       tmux_server="gone"))
    here = tmux.current_server()
    # list-panes rows: pane, its agent tag, its action-terminal tag (%4 a sidebar, %5 not brindle's)
    panes = {here: ["%1\tw1\t", "%4\t\t", "%5\t\t", "%9\t\tt-1"], "other": ["%1\tw2\t"]}
    manual = {(here, "%1"), ("other", "%1"), (here, "%4"), (here, "%5"), (here, "%9")}
    refused: dict = {}          # (server, command) tmux fails -> what it says
    ran = []

    def fake_tmux(*args, server=None, check=True, **kw):
        ran.append((server, *args))
        out, code, err = "", 0, ""
        if (server, args[0]) in refused:
            code, err = 1, refused[server, args[0]]
        elif args[0] == "list-panes":
            out, code, err = ("\n".join(panes[server]), 0, "") if server in panes \
                else ("", 1, "no server running on /tmp/tmux-1/gone")
        elif args[0] == "show-options":
            out = "manual\n" if (server, args[4]) in manual else ""
        elif args[0] == "set-option":
            manual.discard((server, args[4]))
        if check and code:
            raise tmux.TmuxError(err)
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    monkeypatch.setattr(tmux, "_tmux", fake_tmux)
    brindles = {(here, "%1"), ("other", "%1"), (here, "%9")}
    # A server that can't be asked (lost, or a socket it may not open), then a
    # refused unset: nothing is taken as done.
    for refused in ({("other", "list-panes"): "lost server"},
                    {("other", "list-panes"): "error connecting to /tmp/tmux-501/other (Permission denied)"},
                    {(here, "set-option"): "refused"}):
        terminal.reset_window_sizes()
        assert {(here, "%1"), (here, "%9")} <= manual
    refused = {}
    assert terminal.reset_window_sizes() >= 2
    assert manual == {(here, "%4"), (here, "%5")} and not brindles & manual
    assert not any("-g" in r for r in ran)      # never a global option
    ran.clear()
    assert terminal.reset_window_sizes() == 0 and ran == []     # done once, it never runs again


def test_committed_api_schema_matches_the_routes_and_dtos():
    # Drift: after changing a route or a DTO, run `brindle daemon schema` and commit the file.
    from brindle.daemon import schema

    assert schema.SCHEMA_PATH.read_text() == schema.render()


def test_rules_lists_every_pack_with_its_description_and_source(seeded):
    rules = Path(seeded["repo"]) / ".brindle" / "rules"
    rules.mkdir(parents=True)
    (rules / "mine.md").write_text("---\ndescription: My rule\n---\nBe kind.\n")
    code, body = _get("/v1/rules", AUTH, f"repo={seeded['repo']}")
    rows = {r["name"]: r for r in body}
    assert code == 200 and rows["mine"] == {"name": "mine", "description": "My rule", "source": str(rules / "mine.md")}
    assert rows["style/minimal-diff"]["source"].startswith("built-in") and rows["style/minimal-diff"]["description"]


ROUTE_ID = "us.anthropic.claude-opus-x-v1:0"     # the Bedrock id the org's route pins opus to


@pytest.mark.parametrize("allowed,heavy_ok", [
    (ROUTE_ID, True),                               # the pin is allowed: developer-heavy (opus) runs as it
    ("opus", True),                                 # the alias itself is allowed
    ("eu.anthropic.claude-opus-x-v1:0", False),     # a pin matches exactly, as check_assign matches it
], ids=["pinned-id", "alias", "other-region"])
def test_routing_policy_marks_what_the_org_refuses_and_locks(seeded, monkeypatch, allowed, heavy_ok):
    from brindle import providers
    from brindle.pro import managed_models, team_policy

    setup = team_policy.AgentSetup(claude=team_policy.ClaudeSetup(
        route="bedrock", models=team_policy.ModelsSetup(opus=ROUTE_ID)), enforce=True)
    acc = team_policy.Access(org_id="acme", allowed_providers=("claude",), allowed_models=(allowed,),
                             deny_personal_keys=True, agent_setup=setup)
    monkeypatch.setattr(team_policy, "access", lambda repo: acc)
    monkeypatch.setattr(managed_models, "current", lambda repo: None)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: False)
    _, body = _get("/v1/routing", AUTH, f"repo={seeded['repo']}")
    policy = body["policy"]
    assert (policy["applies"], policy["editable"], policy["allowed_providers"]) == (True, True, ["claude"])
    assert {(lock["kind"], lock["name"]) for lock in policy["locked"]} == {
        ("provider", "codex"), ("provider", "antigravity"), ("provider", "opencode"), ("keys", "personal_api_keys")}
    rows = {p["name"]: p for p in body["profiles"]}
    assert (rows["developer-heavy"]["allowed"], rows["developer-heavy"]["refusal"] is None) == (heavy_ok, heavy_ok)
    for refused in ("developer-light", "developer-codex"):      # a model, then a provider, the org doesn't allow
        assert not rows[refused]["allowed"] and rows[refused]["refusal"]
    assert ("developer-heavy" in policy["models"][allowed]) == heavy_ok
    assert "developer-light" not in policy["models"][allowed]
    heavy = [m for t in body["tiers"].values() for m in t["models"] if m["profile"] == "developer-heavy"]
    assert heavy and all((m["model"], m["provider_name"], m["allowed"]) == (ROUTE_ID, "Claude Code", heavy_ok)
                         for m in heavy)


@pytest.mark.parametrize("keychain_status", [44, 1])
def test_routing_user_view_runs_no_learner_and_no_sign_in_probe(seeded, monkeypatch, keychain_status):
    # The Agents tab opens on this read: it must not run the dispatch's learned
    # resolution (a keychain read per credential per candidate) nor any
    # provider's sign-in CLI (up to its timeout each, a signed-out answer never
    # cached), the picker's own readiness check in the config load included.
    from brindle import autopilot, config, providers, worker_models
    from brindle.pro import credentials

    repo = Path(seeded["repo"])
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "repo-only.md").write_text("---\nname: repo-only\nextends: developer\n---\n")
    config.set_user("routing", {"medium": ["developer-codex"]})
    config.set_user("routing_light", "claude-haiku")
    config.set_user("routing_heavy", "or-glm-4.6")      # a hosted pick: its key is in the keychain
    config.set_local(str(repo), "routing", {"medium": ["developer"]})
    routed, probed, pro_reads = [], [], []
    monkeypatch.setattr(license, "current", seeded["current_license"])
    monkeypatch.setattr(keystore, "backend_name", lambda: "keychain")
    monkeypatch.setattr(credentials.KeychainStore, "_run",
                        lambda self, args: pro_reads.append(args) or SimpleNamespace(returncode=keychain_status))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(autopilot, "_route_by_weight", lambda *a, **k: routed.append(a) or (None, False, ""))
    monkeypatch.setattr(doctor, "signin_providers", lambda: list(doctor.CLI_INSTALL))
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: probed.append(argv[0]))
    monkeypatch.setattr(keystore, "get_key", lambda name: probed.append(name))
    monkeypatch.setattr(worker_models, "_signed_in", None)
    code, body = _get("/v1/routing", AUTH, f"repo={repo}&scope=user&learned=0")
    assert code == 200 and routed == [] and probed == []
    assert len(pro_reads) <= 1
    # The person's own view: their settings and library, not this repo's.
    assert (body["tiers"]["medium"]["values"], body["tiers"]["medium"]["source"]) == (["developer-codex"], "user")
    assert "repo-only" not in {p["name"] for p in body["profiles"]}
    assert all(k["learned"] is None for k in body["kinds"].values())
    pro_reads.clear()
    assert _get("/v1/routing", AUTH, f"repo={repo}&scope=user&learned=0")[0] == 200
    assert len(pro_reads) <= 1       # at most one read; main's cross-process entitlement cache may serve it


def test_knowledge_search_returns_hits_for_an_indexed_doc(seeded, db):
    (Path(seeded["repo"]) / "NOTES.md").write_text("# Storage\n\nWe keep state in sqlite, never postgres.\n")
    src = kb_mod.sources(db, knowledge.repo_key(seeded["repo"]))[0]["id"]
    kb_mod.sync(db, src)
    _, hits = _get("/v1/kb/search", AUTH, f"repo={seeded['repo']}&q=sqlite")
    assert [h["heading_path"] for h in hits] == ["Storage"] and "sqlite" in hits[0]["text"]
    assert _get("/v1/kb/search", AUTH, f"repo={seeded['repo']}")[1] == []


def test_profiles_detail_lints_a_broken_profile(seeded):
    agents_dir = Path(seeded["repo"]) / ".brindle" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "orphan.md").write_text("---\nname: orphan\nextends: nobody\n---\nHi.\n")
    _, bad = _get("/v1/profiles/orphan", AUTH, f"repo={seeded['repo']}")
    assert bad["name"] == "orphan" and any("nobody" in e for e in bad["lint"]["errors"])
    _, listed = _get("/v1/profiles", AUTH, f"repo={seeded['repo']}")
    assert any("nobody" in e for p in listed if p["name"] == "orphan" for e in p["lint_errors"])
    (agents_dir / "torn.md").write_text("---\nname: torn\nauth: invalid\n")     # unclosed fence
    code, torn = _get("/v1/profiles/torn", AUTH, f"repo={seeded['repo']}")
    assert code == 200 and torn["name"] == "torn" and torn["lint"]["errors"] and "torn.md" in torn["source"]
    _, good = _get("/v1/profiles/developer", AUTH, f"repo={seeded['repo']}")
    assert good["lint"]["errors"] == [] and "env" not in good
    assert _get("/v1/profiles/nope", AUTH, f"repo={seeded['repo']}")[1]["error"]["code"] == "not_found"


@pytest.mark.variant("a secret is shown only as its name and last four characters, never whole")
def test_config_shows_secrets_only_as_names_and_last_four(seeded, monkeypatch):
    monkeypatch.setattr(keystore, "known_names", lambda repo=None: ["OPENAI_API_KEY", "UNSET_KEY"])
    reads: list[str] = []

    def record(name):
        reads.append(name)
        return {"value": "sk-live-1234567890WXYZ"} if name == "OPENAI_API_KEY" else {}

    monkeypatch.setattr(keystore, "get_record", record)
    _, body = _get("/v1/config", AUTH, f"repo={seeded['repo']}")
    assert [(k["name"], k["set"], k["last4"]) for k in body["keys"]] == [("OPENAI_API_KEY", True, "WXYZ"),
                                                                         ("UNSET_KEY", False, "")]
    assert "1234567890" not in json.dumps(body)
    # One read per key, and none at all when the page asks without the keys (the project page).
    assert sorted(reads) == ["OPENAI_API_KEY", "UNSET_KEY"]
    reads.clear()
    # A saved model choice is judged while the config loads: that must run no sign-in CLI
    # and read no key (a `security` call on macOS), or the quick read waits on them.
    from brindle import config, doctor, providers, worker_models

    config.set_user("routing_heavy", "or-glm-4.6")      # a hosted pick: its key is in the keychain
    probed: list = []
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(doctor, "signin_providers", lambda: list(doctor.CLI_INSTALL))
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: probed.append(argv[0]))
    monkeypatch.setattr(keystore, "get_key", lambda name: probed.append(name))
    monkeypatch.setattr(worker_models, "_signed_in", None)
    _, bare = _get("/v1/config", AUTH, f"repo={seeded['repo']}&keys=0")
    assert bare["keys"] == [] and "repo" in bare and reads == [] and probed == []
