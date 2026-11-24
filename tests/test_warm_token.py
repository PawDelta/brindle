"""The daemon's warm brindle Pro sign-in: its Library requests take the access
token from memory, so none waits on another brindle's refresh (2026-11-24,
plan sharing), an expired token is refreshed once for every caller, and a
refused refresh signs out once."""

import fcntl
import os
import threading
import time

import pytest

from brindle.daemon import app as daemon_app
from brindle.pro import auth
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import session  # noqa: F401
from test_share_panel_access import panel  # noqa: F401
from test_share_people_regression import SlowLibrary, found
from test_shared_plan_docs import parts  # noqa: F401

try:
    from brindle.pro import warm_token
except ImportError:     # the base before the warm sign-in: its requests refresh under the lock
    warm_token = None


@pytest.fixture
def quick_requests(monkeypatch):
    """Requests whose deadline ends soon: one that waits on the refresh lock fails fast."""
    monkeypatch.setattr(daemon_app, "REQUEST_DEADLINE", 0.5)


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def warm(monkeypatch, store, backend, clock):
    """Install the warm sign-in ``auth.authed`` uses, as the daemon does (no background thread)."""
    if warm_token is None:
        return None
    holder = warm_token.WarmToken(store, backend, clock=clock, jitter=lambda: 0.0)
    monkeypatch.setattr(auth, "_warm", holder, raising=False)
    return holder


def refreshes(backend):
    return backend.paths().count("POST /token/refresh")


def test_a_people_search_near_expiry_never_waits_for_another_brindles_refresh(
        quick_requests, panel, parts, backend, monkeypatch):
    pid, _, app = panel
    SlowLibrary(backend)
    store, clock = parts[2]["store"], Clock()
    warm(monkeypatch, store, backend, clock)
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "comment"}).status_code == 200
    # Thirty seconds of the token are left and its renewal hasn't run yet...
    record = store.load()
    clock.now = record["access_expires_at"] - 30
    store.save({**record, "access_expires_at": time.time() + 30})
    # ... while another brindle holds the refresh lock, its keychain reads queued behind a busy machine.
    auth.lock_path().parent.mkdir(parents=True, exist_ok=True)
    held = os.open(auth.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        assert found(app, pid) == ["owner_1"]
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert refreshes(backend) == 0


class CountingCondition(threading.Condition):
    """Says when ``expected`` callers are waiting on it."""

    def __init__(self, expected):
        super().__init__()
        self.waiting, self.expected, self.all_waiting = 0, expected, threading.Event()

    def wait(self, *args):
        self.waiting += 1
        if self.waiting >= self.expected:
            self.all_waiting.set()
        return super().wait(*args)


def expired(parts, backend, monkeypatch):
    """A warm sign-in whose token has expired: the Mac was suspended past its renewal."""
    client, store = parts[2]["client"], parts[2]["store"]
    clock = Clock()
    holder = warm(monkeypatch, store, backend, clock)
    assert auth.authed(client, store, "GET", "/me")[0] == 200       # taken up from the store
    clock.now += 1000
    return client, store, holder


def test_callers_on_an_expired_token_share_one_refresh(parts, backend, monkeypatch):
    client, store, holder = expired(parts, backend, monkeypatch)
    callers = 4
    holder._cond = CountingCondition(callers - 1)
    spend = backend.routes["POST /token/refresh"]

    def refresh(form, headers):         # answers once every other caller is waiting on it
        assert holder._cond.all_waiting.wait(10)
        return spend(form, headers)

    backend.routes["POST /token/refresh"] = refresh
    answers, errors = [], []

    def call():
        try:
            answers.append(auth.authed(client, store, "GET", "/me")[0])
        except Exception as e:  # noqa: BLE001 - returned to the test
            errors.append(e)

    threads = [threading.Thread(target=call) for _ in range(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and answers == [200] * callers
    assert refreshes(backend) == 1
    assert store.load()["access_token"] == holder._token          # saved for the other brindles


def test_a_refused_refresh_signs_out_once(parts, backend, monkeypatch):
    client, store, _ = expired(parts, backend, monkeypatch)
    backend.revoked = True                      # the session was ended elsewhere
    with pytest.raises(auth.AuthError) as refused:
        auth.authed(client, store, "GET", "/me")
    assert refused.value.revoked
    assert store.load() is None
    with pytest.raises(auth.AuthError) as again:
        auth.authed(client, store, "GET", "/me")
    assert again.value.code == "not_logged_in"
    assert refreshes(backend) == 1               # signed out once: nothing more is sent


def test_the_background_renewal_rotates_before_expiry_for_every_brindle(parts, backend, monkeypatch):
    client, store = parts[2]["client"], parts[2]["store"]
    clock = Clock()
    holder = warm(monkeypatch, store, backend, clock)
    holder.renew()                                   # the daemon's start takes up the stored sign-in
    first = holder._token
    assert refreshes(backend) == 0 and holder.due_in() > 0
    clock.now += holder.due_in()                    # two thirds of its life
    holder.renew()
    assert refreshes(backend) == 1 and holder._token != first
    assert store.load()["access_token"] == holder._token          # what the CLI and hooks read
    assert holder.due_in() > 0
    clock.now += 1
    assert auth.authed(client, store, "GET", "/me")[0] == 200
    assert refreshes(backend) == 1
