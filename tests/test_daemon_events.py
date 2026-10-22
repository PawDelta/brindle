import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import bus
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED

TOKEN = "t" * 43


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
