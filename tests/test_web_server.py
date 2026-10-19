import http.client
import json
import threading
import time

import pytest

from brindle import tmux, workspaces
from brindle.db import Agent
from brindle.web import server as web


@pytest.fixture
def served(db, repo, monkeypatch):
    monkeypatch.setattr(tmux, "list_panes", lambda: tmux.PaneSnapshot())
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    db.update_autopilot("boss", goal="Settings page")
    db.set_milestones("boss", [("Settings API", "test -f api.txt", None)])
    db.record_check(db.milestones("boss")[0].id, True, "1 passed", "abc123")
    srv = web.make_server(ws.repo_root, "boss")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.app.port, timeout=10)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{srv.app.port}", **(headers or {})})
    resp = conn.getresponse()
    return resp, resp.read()


@pytest.mark.parametrize("method, path, headers", [
    ("GET", "/api/snapshot", {}),                                       # no token
    ("GET", "/api/snapshot?token={token}", {"Host": "evil.example:{port}"}),  # DNS rebinding
    ("POST", "/api/snapshot?token={token}", {"Origin": "http://evil.example"}),  # CSRF
])
def test_rejects_unauthorized_requests(served, method, path, headers):
    fill = {"token": served.app.token, "port": served.app.port}
    resp, _ = call(served, method, path.format(**fill),
                   {k: v.format(**fill) for k, v in headers.items()})
    assert resp.status == 403


def test_snapshot_round_trip_with_cookie(served):
    resp, _ = call(served, "GET", f"/?token={served.app.token}")
    assert resp.status == 200
    cookie = resp.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    resp, body = call(served, "GET", "/api/snapshot", {"Cookie": cookie.split(";")[0]})
    snap = json.loads(body)
    assert [a["id"] for ws in snap["workspaces"] for a in ws["agents"]] == ["boss"]
    m = snap["autopilot"]["milestones"][0]
    assert (m["title"], m["status"], m["output"], m["checked_sha"]) == \
        ("Settings API", "passed", "1 passed", "abc123")
