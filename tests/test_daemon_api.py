import asyncio
import json
import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from brindle import cost, doctor, identity_lock, keystore, knowledge, procs, tmux, workspaces
from brindle import kb as kb_mod
from brindle.daemon import lifecycle, peer
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED, Guard
from brindle.db import Agent
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


def test_start_binds_loopback_and_keeps_its_files_private():
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
    monkeypatch.setattr(doctor, "checks", lambda r: [doctor.Check("ok", "python", "3.12")])
    monkeypatch.setattr(cost, "require_entitled", lambda: None)
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"@1": True, "@2": True, "@3": True})
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "window_alive", lambda w, server=None: True)

    def no_license(**kw):
        raise license.LicenseError("not logged in")
    monkeypatch.setattr(license, "current", no_license)
    return {"repo": str(repo), "ws": ws.id}


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
    ("/v1/account", lambda b, s: b == {"signed_in": False, "name": None, "plan": None}),
    ("/v1/cost", lambda b, s: b["days"] == 30 and b["total"]["dollars"] == 0),
], ids=["repos", "sessions", "snapshot", "inbox", "goal", "agent", "agent-locked", "agent-missing", "diff",
        "knowledge-decisions", "knowledge-sources", "profiles", "config", "doctor", "account", "cost"])
def test_read_routes_answer_from_the_db(seeded, path, check):
    code, body = _get(path.format(ws=seeded["ws"]), AUTH, f"repo={seeded['repo']}")
    assert code in (200, 404)
    assert check(body, seeded)


@pytest.mark.parametrize("caller,refused_as", [
    (310, "w1"),     # a command under w1's pane, however deep
    (420, "w2"),     # BRINDLE_AGENT_ID in its own environment
    (410, None),     # a person's terminal
    (999, "can't be checked"),   # exited before the check: fire and forget
])
def test_peer_agent_callers_are_refused_on_the_unix_socket(seeded, monkeypatch, caller, refused_as):
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


def test_peer_pid_names_the_unix_socket_caller():
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        assert peer.peer_pid(a) == os.getpid()


def test_committed_api_schema_matches_the_routes_and_dtos():
    # Drift: after changing a route or a DTO, run `brindle daemon schema` and commit the file.
    from brindle.daemon import schema

    assert schema.SCHEMA_PATH.read_text() == schema.render()


def test_routing_shows_each_tier_and_setting_with_its_source(seeded, monkeypatch):
    from brindle import config, providers

    config.set_local(seeded["repo"], "routing", {"light": ["developer"]})
    config.set_repo(seeded["repo"], "exclude_providers", ["antigravity"])
    config.set_user("reviewer", "reviewer")
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude"])
    monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: None)
    _, body = _get("/v1/routing", AUTH, f"repo={seeded['repo']}")
    assert body["tiers"]["light"] == {"values": ["developer"], "source": "local"}
    assert body["tiers"]["medium"] == {"values": config.DEFAULT_ROUTING["medium"], "source": "default"}
    assert body["reviewer"] == {"values": ["reviewer"], "source": "user"}
    assert body["exclude_providers"] == {"values": ["antigravity"], "source": "repo"}
    rows = {p["name"]: p for p in body["profiles"]}
    assert (rows["developer"]["installed"], rows["developer"]["signed_in"]) == (True, True)
    assert (rows["developer-codex"]["installed"], rows["developer-codex"]["signed_in"]) == (False, False)
    assert rows["developer-gemini"]["excluded"] and not rows["developer"]["excluded"]
    assert (rows["developer-light"]["cost_tier"], rows["developer-heavy"]["cost_tier"]) == ("low", "high")


def test_config_shows_secrets_only_as_names_and_last_four(seeded, monkeypatch):
    monkeypatch.setattr(keystore, "known_names", lambda repo=None: ["OPENAI_API_KEY", "UNSET_KEY"])
    monkeypatch.setattr(keystore, "get_key", lambda n: "sk-live-1234567890WXYZ" if n == "OPENAI_API_KEY" else None)
    _, body = _get("/v1/config", AUTH, f"repo={seeded['repo']}")
    assert body["keys"] == [{"name": "OPENAI_API_KEY", "set": True, "last4": "WXYZ"},
                            {"name": "UNSET_KEY", "set": False, "last4": ""}]
    assert "1234567890" not in json.dumps(body)
