import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import bus
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED

TOKEN = "t" * 43


def test_session_changed_tracks_head_and_index_without_status_changes(db, tmp_path):
    from brindle import git
    from brindle.daemon.events import Feed
    from brindle.db import Agent, Workspace

    private = tmp_path / ".git"
    private.mkdir()
    head = private / "HEAD"
    head.write_text("a" * 40)
    index = private / "index"
    index.write_bytes(b"index")
    db.add_workspace(Workspace("w", "/repo", "s", "worktree", "s", "main", str(tmp_path), None, "s", 0))
    db.add_agent(Agent("s", "w", "supervisor", "claude", None, "interactive", "paused", "", None, 0))
    feed = Feed(db, panes=lambda: [], git_status=lambda *_: git.Status("s", "main", 1, 0, [], 0))
    feed.tick(0)
    assert not bus.tail(db, kinds=("session.changed",))
    for n, update in enumerate((lambda: head.write_text("b" * 40), lambda: index.write_bytes(b"new index"), index.unlink), 1):
        update()
        feed.tick(n * 5)
        events = [m for m in feed.tick(n * 5 + 1) if m.get("kind") == "session.changed"]
        assert len(events) == 1
        assert events[0]["root_id"] == "s"
        assert events[0]["workspace_id"] == "w"
        assert not [m for m in feed.tick(n * 5 + 2) if m.get("kind") == "session.changed"]


@pytest.mark.variant("a wrong first-frame token must close the socket with WS_UNAUTHORIZED")
def test_missing_token_is_rejected(db):
    with TestClient(build_app(TOKEN)) as client, \
            client.websocket_connect("/v1/events") as ws:
        ws.send_text("wrong")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == WS_UNAUTHORIZED


@pytest.mark.parametrize("since,first", [(1, ["event", "event"]), (99, ["resnapshot"])])
def test_since_replays_then_tails(db, since, first):
    for n in range(3):
        bus.publish(db, "note", n=n)
    with TestClient(build_app(TOKEN)) as client, \
            client.websocket_connect(f"/v1/events?since={since}") as ws:
        ws.send_text(TOKEN)
        got = [ws.receive_json() for _ in first]
        assert [m["type"] for m in got] == first
        if since == 1:
            assert [m["payload"]["n"] for m in got] == [1, 2]
            bus.publish(db, "note", n=3)
            live = next(m for m in iter(ws.receive_json, None) if m["type"] == "event")
            assert live["payload"] == {"n": 3}
