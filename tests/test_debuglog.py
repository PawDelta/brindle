"""Debug logs: only a staff account turns them on, entries are scrubbed, and
the uploader posts what was written."""

import json
import time
from pathlib import Path

import pytest
from test_daemon_actions import _send

from brindle import config, debuglog


class _Server:
    """A fake backend and credential store; ``me`` and ``creds`` can change mid-test."""

    def __init__(self, me: dict):
        self.me, self.posted, self.tokens = me, [], []
        self.by_token, self.asked = {}, []      # /me answers for particular tokens; each /me asked
        self.creds = {"refresh_token": "r-a", "access_token": "a", "access_expires_at": time.time() + 3600,
                      "base_url": "https://brindle.test/v1"}

    def load(self):
        return dict(self.creds)

    def save(self, creds):
        self.creds = dict(creds)

    def request(self, method, url, form, headers):
        if url.endswith("/me"):
            self.asked.append((url, headers.get("Authorization")))
            return 200, self.by_token.get(headers.get("Authorization"), self.me)
        if url.endswith("/token/refresh"):
            new = form["refresh_token"] + "2"
            return 200, {"access_token": new.removeprefix("r-"), "refresh_token": new, "expires_in": 900}
        self.posted.append((url, dict(form)))
        self.tokens.append(headers.get("Authorization"))
        return 202, {}


@pytest.fixture
def server(monkeypatch):
    def make(me: dict) -> _Server:
        s = _Server(me)
        monkeypatch.setattr(debuglog, "_account", lambda: (s, s))
        monkeypatch.setattr(debuglog, "_staff", {})
        return s
    return make


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
    assert _send("PUT", "/v1/debug", {"enabled": True})[0] == 403       # a non-staff account can't turn it on
    config.set_user(debuglog.SETTING, True)     # even if the setting is still on
    assert debuglog.upload_once() == 0 and s.posted == []
    assert not debuglog.enabled()


def test_debuglog_fresh_staff_check_sees_a_revocation_the_cache_hides(server):
    s = server({"sub": "u1", "staff": True})
    assert _send("GET", "/v1/debug")[1]["staff"] is True
    s.me = {"sub": "u1", "staff": False}        # revoked on the backend, the same account
    assert _send("GET", "/v1/debug")[1]["staff"] is True        # still cached
    # The app asks fresh right before installing an update: the backend's answer, now.
    assert _send("GET", "/v1/debug?fresh=1")[1]["staff"] is False
    assert len(s.asked) == 2


def test_debuglog_switch_mid_check_or_upload_uses_the_snapshot_account(server, monkeypatch):
    s = server({"sub": "u1", "staff": True})
    s.by_token["Bearer b"] = {"sub": "u2"}      # B is not staff
    a = dict(s.creds)
    b = {**a, "refresh_token": "r-b", "access_token": "b", "base_url": "https://other.test/v1"}
    real_snapshot, real_pending = debuglog._snapshot, debuglog._pending

    def snapshot_then_switch():         # B signs in on another backend right after the read
        got = real_snapshot()
        s.creds = dict(b)
        return got

    monkeypatch.setattr(debuglog, "_snapshot", snapshot_then_switch)
    assert debuglog.staff(refresh=True) is True         # asked as A, on A's backend
    monkeypatch.setattr(debuglog, "_snapshot", real_snapshot)
    assert debuglog.staff() is False        # A's cached answer isn't B's
    assert s.asked == [("https://brindle.test/v1/me", "Bearer a"), ("https://other.test/v1/me", "Bearer b")]

    s.creds = {**a, "access_expires_at": 0}     # A again, its access token due
    config.set_user(debuglog.SETTING, True)
    debuglog.record("app", "info", "private")

    def pending_then_switch():
        got = real_pending()
        s.creds = dict(b)
        return got

    monkeypatch.setattr(debuglog, "_pending", pending_then_switch)
    assert debuglog.upload_once() == 1
    # Posted as A (checked, refreshed from A's snapshot) to A's backend, never as B or to B's backend.
    assert s.tokens == ["Bearer a2"] and s.posted[0][0] == "https://brindle.test/v1/debug/logs"


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


def test_debuglog_disabled_writes_nothing_and_oversized_detail_is_replaced_by_a_marker():
    config.set_user(debuglog.SETTING, False)

    assert _send("POST", "/v1/debug/log", {"source": "app", "message": "hi"}) == (200, {"recorded": False})
    assert not debuglog.log_path().exists()

    config.set_user(debuglog.SETTING, True)
    assert debuglog.record("app", "error", "big", ["é" * 2000] * 600)
    assert debuglog.log_path().stat().st_size <= debuglog.MAX_ENTRY
    assert json.loads(debuglog.log_path().read_text())["detail"]["truncated"] is True
