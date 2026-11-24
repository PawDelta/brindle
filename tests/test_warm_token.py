"""The daemon's warm brindle Pro sign-in: its Library requests take the sign-in
and access token from memory, so none reads the keychain or waits on another
brindle's refresh (2026-11-24, plan sharing); an expired token is refreshed
once for every caller, each waiting no longer than its deadline; and a refused
refresh signs out once."""

import fcntl
import os
import re
import threading
import time
from contextlib import contextmanager
from subprocess import CompletedProcess

import pytest

from brindle import keystore, plans, spawn
from brindle.daemon import app as daemon_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from brindle.pro import shared_docs as sd
from pro_fixtures import BASE, backend, claims, pro_env, sign, signing_key  # noqa: F401
from test_plans import session  # noqa: F401
from test_share_panel_access import panel  # noqa: F401
from test_share_people_regression import SlowLibrary, found
from test_pro_remote_learning import login_as
from test_shared_plan_docs import TEAM_LOGIN, parts  # noqa: F401

try:
    from brindle.pro import warm_token
except ImportError:     # the base before the warm sign-in: its requests read and refresh the store
    warm_token = None

REAL_SESSION, REAL_ENTITLEMENT = ad._session, ad.require_entitlement


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class Keychain:
    """The login keychain behind ``security``, counting reads and writes."""

    def __init__(self, monkeypatch):
        self.items, self.reads, self.writes = {}, 0, 0
        monkeypatch.setattr(keystore, "_keychain_reads", {})
        monkeypatch.setattr(keystore.KeychainStore, "_run", lambda store, args, stdin=None: self.run(store, args, stdin))

    def run(self, store, args, stdin):
        item = (store.service, store.account)
        if args[0] == "find-generic-password":
            self.reads += 1
            blob = self.items.get(item)
            return CompletedProcess(args, 44 if blob is None else 0, blob or "", "")
        if args[0] == "-i":
            self.writes += 1
            self.items[item] = re.search(r'-w "([^"]*)"', stdin).group(1)
        else:
            self.items.pop(item, None)
        return CompletedProcess(args, 0, "", "")

    def put(self, record):
        """Another process writes the sign-in (this one's cache doesn't see it)."""
        self.items[(credentials.SERVICE, credentials.ACCOUNT)] = keystore._encode(record)

    def expire_cache(self):
        for read in keystore._keychain_reads.values():
            read.invalidate()


def warm(monkeypatch, store, backend, clock):
    """Install the warm sign-in ``auth.authed`` uses, as the daemon does (no background thread)."""
    if warm_token is None:
        return None
    holder = warm_token.WarmToken(store, backend, clock=clock, jitter=lambda: 0.0)
    monkeypatch.setattr(auth, "_warm", holder, raising=False)
    return holder


def refreshes(backend):
    return backend.paths().count("POST /token/refresh")


def bearer(backend):
    return backend.calls[-1][2]["Authorization"].removeprefix("Bearer ")


@contextmanager
def lock_held():
    """Another brindle holds the refresh lock, its keychain reads queued behind a busy machine."""
    auth.lock_path().parent.mkdir(parents=True, exist_ok=True)
    held = os.open(auth.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


def landed(holder, flight):
    with holder._cond:
        assert holder._cond.wait_for(lambda: flight.done, 10)


@pytest.fixture
def quick_requests(monkeypatch):
    """Requests whose deadline ends soon: one that waits on the refresh lock fails fast."""
    monkeypatch.setattr(daemon_app, "REQUEST_DEADLINE", 0.5)


@pytest.fixture
def route(quick_requests, panel, parts, backend, monkeypatch):
    """The share panel's routes as the daemon runs them: the real session and entitlement
    check, the sign-in in a (fake) login keychain, and (after the change) a warm sign-in."""
    pid, _, app = panel
    SlowLibrary(backend)
    keychain = Keychain(monkeypatch)
    record = parts[2]["store"].load()
    keychain.put(record)
    monkeypatch.setattr(credentials, "default_store", lambda: credentials.KeychainStore())
    monkeypatch.setattr(auth, "UrllibTransport", lambda **kw: backend)
    monkeypatch.setattr(ad, "_session", REAL_SESSION)
    monkeypatch.setattr(ad, "require_entitlement", REAL_ENTITLEMENT)
    clock = Clock()
    holder = warm(monkeypatch, credentials.KeychainStore(), backend, clock)
    return pid, app, keychain, record, clock, holder


@pytest.mark.variant("the 2026-11-24 incident end to end: a share route behind another brindle's held refresh lock")
def test_a_people_search_near_expiry_reads_no_keychain_and_never_waits(route, backend):
    pid, app, keychain, record, clock, holder = route
    if holder is not None:
        keychain.put({**record, "access_expires_at": time.time() + 30})       # persisted near expiry
        holder.renew()              # the daemon's start: the token is rotated with one read and one write
        assert (keychain.reads, keychain.writes, refreshes(backend)) == (1, 1, 1)
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "comment"}).status_code == 200
    # Thirty seconds of the token are left, its renewal hasn't run yet and the keychain cache has lapsed...
    stored = keychain.items[(credentials.SERVICE, credentials.ACCOUNT)]
    clock.now = keystore._decode(stored)["access_expires_at"] - 30
    keychain.put({**keystore._decode(stored), "access_expires_at": time.time() + 30})
    keychain.expire_cache()
    before = (keychain.reads, refreshes(backend))
    with lock_held():                                    # ... and another brindle holds the refresh lock
        assert found(app, pid) == ["owner_1"]
    assert (keychain.reads, refreshes(backend)) == before


def test_a_lapsed_entitlement_is_reloaded_off_the_request_path(route, backend):
    pid, app, keychain, record, clock, holder = route
    now = int(time.time())
    lapsed = sign(backend.key, claims(features=list(TEAM_LOGIN), iat=now - 7200, exp=now - 60))
    keychain.put({**record, "entitlement": lapsed})
    holder.renew()                                   # the token is held with an entitlement that has lapsed
    renewed = {**keystore._decode(keychain.items[(credentials.SERVICE, credentials.ACCOUNT)]),
               "entitlement": record["entitlement"]}
    keychain.put(renewed)                            # another brindle renewed the entitlement since
    reads = keychain.reads
    with lock_held():
        assert app.get(f"/v1/plans/{pid}/people?q=own").status_code != 200     # answered at once, from memory
        reload = holder._flight                      # its reload waits for the lock, off the request
        assert reload is not None and keychain.reads == reads
    landed(holder, reload)
    assert found(app, pid) == ["owner_1"]
    assert keychain.reads == reads + 1 and refreshes(backend) == 0


def test_a_request_never_sends_for_a_sign_in_replaced_under_it(parts, backend, monkeypatch):
    keychain = Keychain(monkeypatch)
    store = credentials.KeychainStore()
    store.save(parts[2]["store"].load())
    holder = warm(monkeypatch, store, backend, Clock())
    holder.renew()

    def login_elsewhere(base=BASE):
        t = backend.issue()
        keychain.put({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                      "access_expires_at": time.time() + 900, "base_url": base,
                      "entitlement": sign(backend.key, claims(sub="user_2", features=list(TEAM_LOGIN)))})
        with auth.refresh_lock():
            auth._note_signin()
        return t["access_token"]

    # Another account signs in between the request's session (client, entitlement) and its token.
    view, record = auth.warm_pin(store)
    client = auth.Client(record["base_url"], backend)
    new = login_elsewhere()
    assert store.load()["access_token"] == record["access_token"]   # this process's keychain cache: the old one
    sent = len(backend.calls)
    with pytest.raises(auth.AuthError) as changed:
        auth.authed(client, view, "GET", "/me")
    assert changed.value.code == "record_changed" and len(backend.calls) == sent
    # A nested call made inside that operation keeps its pin rather than taking the new account...
    monkeypatch.setattr(auth, "UrllibTransport", lambda **kw: backend)
    with pytest.raises(auth.AuthError) as nested:
        ad._call("GET", "/me", store=view)
    assert nested.value.code == "record_changed"
    # ... and a refresh through it under the lock never reads the new account's record.
    with pytest.raises(keystore.CredentialError):
        auth.refresh(client, view)
    assert len(backend.calls) == sent
    # The next request is the new account's.
    view, _ = auth.warm_pin(store)
    assert auth.authed(client, view, "GET", "/me")[0] == 200 and bearer(backend) == new

    # A 401 that comes back once another account has been taken up is not sent again as that account.
    backend.calls.clear()
    me = backend.routes["GET /me"]

    def refused_meanwhile(form, headers):
        login_elsewhere()
        holder.grant(client)                          # a concurrent request takes up the new account
        return 401, {"error": "invalid_token"}

    backend.routes["GET /me"] = refused_meanwhile
    assert auth.authed(client, view, "GET", "/me")[0] == 401
    assert backend.paths().count("GET /me") == 1
    backend.routes["GET /me"] = me

    # The sign-in moves to another backend: a client for the old one gets nothing sent.
    login_elsewhere("https://other.test/api/brindle/v1")
    sent = len(backend.calls)
    with pytest.raises(auth.AuthError) as moved:
        auth.authed(client, store, "GET", "/me")
    assert moved.value.code == "record_changed" and len(backend.calls) == sent


def test_a_replacement_found_by_a_renewal_never_goes_out_with_the_old_bearer(parts, backend, monkeypatch):
    keychain = Keychain(monkeypatch)
    store, client = credentials.KeychainStore(), parts[2]["client"]
    store.save(parts[2]["store"].load())
    holder = warm(monkeypatch, store, backend, Clock())
    holder.renew()
    t = backend.issue()                         # another account signs in; its token needs rotating
    keychain.put({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                  "access_expires_at": time.time() + 10, "base_url": BASE,
                  "entitlement": sign(backend.key, claims(sub="user_2", features=list(TEAM_LOGIN)))})
    with auth.refresh_lock():
        auth._note_signin()
    reached, release = threading.Event(), threading.Event()
    held_refresh(backend, lambda limit: reached.set() or release.wait(limit))
    with holder._cond:                          # the background renewal finds it before any request does
        renewal = holder._launch(holder._token)
    assert reached.wait(10)                     # paused mid-rotation, the old bearer still unexpired
    with spawn.deadline(0.05), pytest.raises(auth.AuthError):
        auth.authed(client, store, "GET", "/me")
    assert "GET /me" not in backend.paths()
    release.set()
    landed(holder, renewal)
    assert auth.authed(client, store, "GET", "/me")[0] == 200
    assert bearer(backend) == store.load()["access_token"] != t["access_token"]


def test_the_no_entitlement_diagnostic_never_refreshes_on_the_request(parts, backend, monkeypatch):
    store = parts[2]["store"]
    holder = warm(monkeypatch, store, backend, Clock())
    holder.renew()
    with lock_held(), spawn.deadline(0.05):
        assert sd._no_entitlement_why(store) == plans.LIBRARY_ERROR
        reload = holder._flight                 # renewed in the background once the lock is free
        assert reload is not None
    landed(holder, reload)
    assert refreshes(backend) == 0


def test_a_missing_entitlement_is_reloaded_and_recovers_after_a_failed_reload(parts, backend, monkeypatch):
    store, clock = parts[2]["store"], Clock()
    store.save({k: v for k, v in store.load().items() if k != "entitlement"})
    monkeypatch.setattr(auth, "_signed_in_providers", lambda: [])
    issue, failures = backend.routes["GET /entitlement"], [1]
    backend.routes["GET /entitlement"] = lambda f, h: (503, {"error": "unavailable"}) if failures and failures.pop() \
        else issue(f, h)
    holder = warm(monkeypatch, store, backend, clock)
    holder.renew()
    assert holder.view.load().get("entitlement") is None
    reload = holder._flight                     # a request found none: its reload fails once
    assert reload is not None
    landed(holder, reload)
    holder.view.load()
    assert holder._flight is None               # requests don't start it over and over...
    clock.now += holder.due_in()
    holder.renew()                              # ... the next token renewal is followed by a reload
    holder.renew()
    assert holder.view.load().get("entitlement")
    assert backend.paths().count("GET /entitlement") == 2


def test_a_login_right_after_a_refused_refresh_is_the_sign_in_then(parts, backend, monkeypatch):
    client, store, _ = expired(parts, backend, monkeypatch)
    backend.revoked = True
    real_lock, logged_in = auth.refresh_lock, []

    @contextmanager
    def then_login():
        try:
            with real_lock():
                yield
        finally:
            if backend.revoked and not logged_in:   # once the refused refresh lets go of the lock
                logged_in.append(True)
                backend.revoked = False
                login_as(backend, store)
                with real_lock():
                    auth._note_signin()

    monkeypatch.setattr(auth, "refresh_lock", then_login)
    with pytest.raises(auth.AuthError) as refused:
        auth.authed(client, store, "GET", "/me")
    assert refused.value.revoked and logged_in
    assert auth.authed(client, store, "GET", "/me")[0] == 200


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


def held_refresh(backend, until):
    """The backend answers a refresh only once ``until`` says so."""
    spend = backend.routes["POST /token/refresh"]

    def refresh(form, headers):
        assert until(10)
        return spend(form, headers)

    backend.routes["POST /token/refresh"] = refresh


@pytest.mark.variant("race: concurrent callers after a suspend must share one rotation, never spend the token twice")
def test_callers_on_an_expired_token_share_one_refresh(parts, backend, monkeypatch):
    client, store, holder = expired(parts, backend, monkeypatch)
    callers = 4
    holder._cond = CountingCondition(callers)
    held_refresh(backend, holder._cond.all_waiting.wait)     # answered once every caller waits on it
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


def test_a_caller_on_an_expired_token_stops_at_its_deadline(parts, backend, monkeypatch):
    client, store, holder = expired(parts, backend, monkeypatch)
    answered = threading.Event()
    held_refresh(backend, answered.wait)
    with spawn.deadline(0.05), pytest.raises(auth.AuthError) as busy:
        auth.authed(client, store, "GET", "/me")
    assert busy.value.code == "lock_timeout"
    answered.set()                                  # the refresh it started still finishes, once
    assert holder.access(client) == store.load()["access_token"]
    assert refreshes(backend) == 1


def test_a_401_on_an_unexpired_token_never_waits_for_the_lock(parts, backend, monkeypatch):
    client, store = parts[2]["client"], parts[2]["store"]
    holder = warm(monkeypatch, store, backend, Clock())
    holder.renew()
    backend.access_tokens.discard(holder._token)        # the backend no longer takes it
    with lock_held():
        assert auth.authed(client, store, "GET", "/me")[0] == 401
        flight = holder._flight                          # the renewal it started waits for the lock
        assert flight is not None and refreshes(backend) == 0
    landed(holder, flight)
    assert auth.authed(client, store, "GET", "/me")[0] == 200
    assert refreshes(backend) == 1


def test_a_refused_refresh_signs_out_once(parts, backend, monkeypatch):
    keychain = Keychain(monkeypatch)
    store, client, clock = credentials.KeychainStore(), parts[2]["client"], Clock()
    store.save(parts[2]["store"].load())
    holder = warm(monkeypatch, store, backend, clock)
    holder.renew()
    clock.now += 1000                           # expired: the Mac was suspended past its renewal
    backend.revoked = True                      # the session was ended elsewhere
    reads = keychain.reads
    with pytest.raises(auth.AuthError) as refused:
        auth.authed(client, store, "GET", "/me")
    assert refused.value.revoked
    assert not keychain.items
    # The read under the lock, and the one that checks no newer sign-in was saved before deleting
    # it (the supervisor's call: a refusal is rare, deleting an older CLI's new login is not OK).
    assert keychain.reads - reads == 2
    io = (keychain.reads, keychain.writes)
    with pytest.raises(auth.AuthError) as again:
        auth.authed(client, store, "GET", "/me")
    assert again.value.code == "not_logged_in"
    assert refreshes(backend) == 1               # signed out once: nothing more is sent...
    assert (keychain.reads, keychain.writes) == io       # ... or read


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
