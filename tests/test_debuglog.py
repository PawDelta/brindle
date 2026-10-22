"""Debug logs: only a staff account turns them on, entries are scrubbed, and
the uploader posts what was written."""

import json
import time
from pathlib import Path

import pytest
from test_daemon_actions import _send

from brindle import config, debuglog
from brindle.pro import auth


class _Server:
    """A fake backend and credential store; ``me`` and ``creds`` can change mid-test."""

    def __init__(self, me: dict):
        self.me, self.posted, self.tokens = me, [], []
        self.creds = {"refresh_token": "r-a", "access_token": "a", "access_expires_at": time.time() + 3600}

    def load(self):
        return dict(self.creds)

    def request(self, method, url, form, headers):
        if url.endswith("/me"):
            return 200, self.me
        self.posted.append((url, dict(form)))
        self.tokens.append(headers.get("Authorization"))
        return 202, {}


@pytest.fixture
def server(monkeypatch):
    def make(me: dict) -> _Server:
        s = _Server(me)
        monkeypatch.setattr(debuglog, "_account",
                            lambda: (auth.Client("https://brindle.test/v1", transport=s), s))
        monkeypatch.setattr(debuglog, "_staff", {})
        return s
    return make


def test_debuglog_non_staff_cannot_turn_it_on(server):
    server({"sub": "u1"})      # no ``staff`` field: not staff

    assert _send("PUT", "/v1/debug", {"enabled": True})[0] == 403
    assert _send("GET", "/v1/debug") == (200, {"enabled": False, "staff": False})


def test_debuglog_staff_entry_is_scrubbed_and_uploaded(server):
    s = server({"sub": "u1", "staff": True})
    assert _send("PUT", "/v1/debug", {"enabled": True}) == (200, {"enabled": True, "staff": True})
    secrets = ["sk-ant-" + "x" * 30, "opaque-access-123456789", "example-password-123456789",
               "opaque-bearer-123456789"]

    assert debuglog.record("sidebar", "error", f"draw failed in {Path.home()}/proj",
                           {"env": f"key {secrets[0]}", "access_token": secrets[1], "password": secrets[2],
                            "headers": {"Authorization": f"Bearer {secrets[3]}"},
                            "said": f"sent Bearer {secrets[3]}"})
    (line,) = debuglog.log_path().read_text().splitlines()
    assert str(Path.home()) not in line and "~/proj" in line

    assert debuglog.upload_once() == 1
    ((url, body),) = s.posted
    assert url == "https://brindle.test/v1/debug/logs"
    assert body["entries"] == [json.loads(line)] and body["client"]["install_id"]
    assert not any(x in line or x in json.dumps(body) for x in secrets)
    assert debuglog.upload_once() == 0      # nothing new since


def test_debuglog_account_switch_to_non_staff_stops_uploads(server):
    s = server({"sub": "u1", "staff": True})
    assert _send("PUT", "/v1/debug", {"enabled": True})[0] == 200
    debuglog.record("app", "info", "before the switch")
    s.me, s.creds["refresh_token"] = {"sub": "u2"}, "r-b"       # another, non-staff account

    assert _send("GET", "/v1/debug") == (200, {"enabled": False, "staff": False})
    config.set_user(debuglog.SETTING, True)     # even if the setting is still on
    assert debuglog.upload_once() == 0 and s.posted == []
    assert not debuglog.enabled()


def test_debuglog_switch_during_an_upload_posts_as_the_checked_account(server, monkeypatch):
    s = server({"sub": "u1", "staff": True})
    config.set_user(debuglog.SETTING, True)
    debuglog.record("app", "info", "private")
    real = debuglog._pending

    def switch_then_pending():
        got = real()
        s.me, s.creds["access_token"] = {"sub": "u2"}, "b"      # non-staff B signs in meanwhile
        return got

    monkeypatch.setattr(debuglog, "_pending", switch_then_pending)
    assert debuglog.upload_once() == 1
    assert s.tokens == ["Bearer a"]       # A, whose staff status was checked, never B


def test_debuglog_rotation_keeps_pending_entries(server, monkeypatch):
    s = server({"sub": "u1", "staff": True})
    config.set_user(debuglog.SETTING, True)
    debuglog.record("app", "info", "a")
    size = debuglog.log_path().stat().st_size
    assert debuglog.upload_once() == 1
    # b fits; c rotates [a, b] away, and the new file is as large as the old cursor.
    monkeypatch.setattr(debuglog, "MAX_BYTES", size * 5 // 2)
    debuglog.record("app", "info", "b")
    debuglog.record("app", "info", "c")
    assert debuglog.rotated_path().exists()

    assert debuglog.upload_once() == 2
    assert [e["message"] for e in s.posted[-1][1]["entries"]] == ["b", "c"]
    assert debuglog.upload_once() == 0


def test_debuglog_rotation_while_the_uploader_has_the_files_open(server, monkeypatch):
    s = server({"sub": "u1", "staff": True})
    config.set_user(debuglog.SETTING, True)
    debuglog.record("app", "info", "a")
    monkeypatch.setattr(debuglog, "MAX_BYTES", debuglog.log_path().stat().st_size * 5 // 2)
    debuglog.record("app", "info", "b")
    real = debuglog._open_logs

    def open_then_rotate():
        got = real()
        monkeypatch.setattr(debuglog, "_open_logs", real)
        debuglog.record("app", "info", "c")       # rotates [a, b] away
        return got

    monkeypatch.setattr(debuglog, "_open_logs", open_then_rotate)
    assert debuglog.upload_once() == 2
    assert debuglog.upload_once() == 1
    assert [[e["message"] for e in body["entries"]] for _, body in s.posted] == [["a", "b"], ["c"]]


def test_debuglog_oversized_detail_is_replaced_by_a_marker():
    config.set_user(debuglog.SETTING, True)

    assert debuglog.record("app", "error", "big", ["é" * 2000] * 600)
    assert debuglog.log_path().stat().st_size <= debuglog.MAX_ENTRY
    assert json.loads(debuglog.log_path().read_text())["detail"]["truncated"] is True


def test_debuglog_disabled_writes_nothing():
    config.set_user(debuglog.SETTING, False)

    assert _send("POST", "/v1/debug/log", {"source": "app", "message": "hi"}) == (200, {"recorded": False})
    assert not debuglog.log_path().exists()
