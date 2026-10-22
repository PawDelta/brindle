"""Debug logs: only a staff account turns them on, entries are scrubbed, and
the uploader posts what was written."""

import json
import time
from pathlib import Path

import pytest
from test_daemon_actions import _send

from brindle import config, debuglog
from brindle.pro import auth


class _Store:
    def load(self):
        return {"refresh_token": "r", "access_token": "a", "access_expires_at": time.time() + 3600}


class _Transport:
    def __init__(self, me: dict):
        self.me, self.posted = me, []

    def request(self, method, url, form, headers):
        if url.endswith("/me"):
            return 200, self.me
        self.posted.append((url, dict(form)))
        return 202, {}


@pytest.fixture
def server(monkeypatch):
    def make(me: dict) -> _Transport:
        t = _Transport(me)
        monkeypatch.setattr(debuglog, "_account",
                            lambda: (auth.Client("https://brindle.test/v1", transport=t), _Store()))
        monkeypatch.setattr(debuglog, "_staff", {})
        return t
    return make


def test_debuglog_non_staff_cannot_turn_it_on(server):
    server({"sub": "u1"})      # no ``staff`` field: not staff

    assert _send("PUT", "/v1/debug", {"enabled": True})[0] == 403
    assert _send("GET", "/v1/debug") == (200, {"enabled": False, "staff": False})


def test_debuglog_staff_entry_is_scrubbed_and_uploaded(server):
    t = server({"sub": "u1", "staff": True})
    assert _send("PUT", "/v1/debug", {"enabled": True}) == (200, {"enabled": True, "staff": True})
    secret = "sk-ant-" + "x" * 30

    assert debuglog.record("sidebar", "error", f"draw failed in {Path.home()}/proj",
                           {"env": f"key {secret}"})
    (line,) = debuglog.log_path().read_text().splitlines()
    assert str(Path.home()) not in line and secret not in line and "~/proj" in line

    assert debuglog.upload_once() == 1
    ((url, body),) = t.posted
    assert url == "https://brindle.test/v1/debug/logs"
    assert body["entries"] == [json.loads(line)] and body["client"]["install_id"]
    assert debuglog.upload_once() == 0      # nothing new since


def test_debuglog_disabled_writes_nothing():
    config.set_user(debuglog.SETTING, False)

    assert _send("POST", "/v1/debug/log", {"source": "app", "message": "hi"}) == (200, {"recorded": False})
    assert not debuglog.log_path().exists()
