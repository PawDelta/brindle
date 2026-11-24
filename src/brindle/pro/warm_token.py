"""The daemon keeps its brindle Pro sign-in warm, so a Library request never
waits on a sign-in refresh.

Every brindle of the OS user refreshes the 15-minute access token under one
cross-process lock (:func:`brindle.pro.auth.refresh_lock`), with keychain reads
and writes inside it. A request that found its token near expiry used to wait
there for whichever brindle held the lock: on a busy machine, past its own
deadline (2026-11-24, plan sharing). Here the daemon holds the stored sign-in
and its access token in memory and renews the token in the background at about
two thirds of its life. Its requests read both from memory (:attr:`WarmToken.view`,
which :func:`brindle.pro.analysis_docs._session` hands out, and
:func:`brindle.pro.auth.authed`): no keychain read and no refresh lock while the
token is unexpired.

Every refresh runs on its own thread (never a request's, so a rotation that has
started always finishes and is saved), once for all the callers that need it;
each caller waits for it no longer than its own deadline. Only a token that has
actually expired (the Mac was suspended, say, and the renewal is late) is waited
for. A Library 401 on an unexpired token uses a newer token if one is ready, or
starts a renewal and returns the 401. A refresh reads the store once (under the
lock, uncached) and writes it once, so other processes (the CLI, hooks, workers)
see the renewed sign-in as before. A refused refresh signs out once
(:func:`auth._rotate` deletes the stored sign-in); later requests send nothing
until a login. A login, logout or org switch anywhere replaces the marker file
:func:`auth.signin_marker`: what was held for the sign-in it replaced is dropped
at the next request, and the next refresh reads the store afresh.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

from brindle import keystore, spawn
from brindle.pro import auth

log = logging.getLogger(__name__)

RENEW_AT = 2 / 3     # renew once this share of the access token's life has passed
JITTER = 0.05        # +/- this share of its life, so brindles started together renew apart


def store_key(store) -> tuple | None:
    """Which stored sign-in ``store`` reads, or None for a store of another
    kind: no warm sign-in serves that one, it reads its own."""
    if isinstance(store, keystore.KeychainStore):
        return ("keychain", store.service, store.account)
    if isinstance(store, keystore.SecretServiceStore):
        return ("secret-service", *store.attrs)
    if isinstance(store, keystore.FileStore):
        return ("file", str(store.path))
    return None


class SignInRefreshing(keystore.KeychainReadBlocked):
    """The request's deadline came while the sign-in was still being read or refreshed."""

    def __init__(self) -> None:
        super().__init__("Another brindle process is refreshing your sign-in.")


class _Flight:
    """One refresh in progress, which every caller needing it waits on."""

    def __init__(self) -> None:
        self.done = False
        self.error: BaseException | None = None


class _View:
    """The holder's store as the daemon's requests read it: outside a refresh lock
    from memory, under one (an uncached read) from the store itself. Saves and
    deletes go to the store and update what is held."""

    def __init__(self, holder: WarmToken) -> None:
        self._holder = holder
        self.name = getattr(holder.store, "name", "")

    def load(self) -> dict | None:
        if keystore.reading_uncached():
            return self._holder.store.load()
        return self._holder.snapshot()

    def save(self, creds: dict) -> None:
        self._holder.store.save(creds)
        self._holder._saved(creds)

    def delete(self) -> None:
        self._holder.store.delete()
        self._holder._deleted()


class WarmToken:
    """``store``'s sign-in and access token, held in memory and renewed before
    the token expires. ``clock`` is wall-clock time (a token's expiry is wall
    time: monotonic time stands still while the Mac is suspended)."""

    def __init__(self, store, transport: auth.Transport | None = None, *,
                 clock: Callable[[], float] = time.time,
                 jitter: Callable[[], float] | None = None) -> None:
        self.store, self.key = store, store_key(store)
        self.transport = transport
        self.clock = clock
        self.jitter = jitter or (lambda: random.uniform(-JITTER, JITTER))
        self.view = _View(self)
        self._marker = auth.signin_marker()
        self._cond = threading.Condition()
        self._record: dict | None = None      # the stored sign-in, as last read or written
        self._base: str | None = None         # its backend: the token is only ever sent there
        self._token: str | None = None
        self._expires = 0.0
        self._renew_at: float | None = None
        self._signin = ""                     # the marker as it was when they were read
        self._signed_out = False              # nothing stored: nothing to send until a login
        self._flight: _Flight | None = None
        self._changed = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def serves(self, store) -> bool:
        return store is self.view or (self.key is not None and store_key(store) == self.key)

    # -- requests ---------------------------------------------------------------------------

    def access(self, client: auth.Client) -> str | None:
        """An unexpired access token for ``client``'s backend, or None when the
        sign-in held is for another backend. An expired token is refreshed once
        for every caller, each waiting no longer than its deadline."""
        with self._cond:
            while True:
                self._check_signin()
                if self._signed_out:
                    raise auth.AuthError("not logged in to brindle Pro (run `brindle account login`)",
                                         code="not_logged_in")
                if self._base is not None and client.base != self._base:
                    return None
                if self._token and self._expires > self.clock():
                    return self._token
                flight = self._flight or self._launch(self._token)
                self._await(flight, lambda: auth.AuthError(
                    "the request ended while waiting for the sign-in refresh", code="lock_timeout"))
                if flight.error is not None:
                    raise flight.error

    def refused(self, client: auth.Client, stale: str) -> str | None:
        """The backend refused ``stale`` (a 401): a newer unexpired token if one is
        ready, else None, having started a renewal for the next request. Never waits."""
        with self._cond:
            self._check_signin()
            if self._signed_out or client.base != self._base:
                return None
            if self._token and self._token != stale and self._expires > self.clock():
                return self._token
            if self._flight is None:
                self._launch(stale)
            return None

    def snapshot(self) -> dict | None:
        """The stored sign-in as held (a copy), read once if nothing is held yet."""
        with self._cond:
            while True:
                self._check_signin()
                if self._signed_out:
                    return None
                if self._record is not None:
                    record = dict(self._record)
                    break
                flight = self._flight or self._launch(self._token)
                self._await(flight, SignInRefreshing)
                if self._record is None and flight.error is not None and not self._signed_out:
                    raise keystore.CredentialError(
                        f"couldn't read the brindle Pro sign-in ({getattr(flight.error, 'code', 'error')})")
        if self._entitled(record):
            return record
        # Its entitlement has lapsed: the store may hold the one another process renewed since.
        stored = self.store.load()
        if stored and stored.get("refresh_token") == record.get("refresh_token"):
            with self._cond:
                if self._record is not None and self._record.get("refresh_token") == stored["refresh_token"]:
                    self._record = dict(stored)
        return stored

    def _entitled(self, record: dict) -> bool:
        from brindle.pro import license

        try:
            license.verify(record.get("entitlement") or "", issuer=auth.base_url(record.get("base_url")),
                           now=time.time(), grace=0)
        except (license.LicenseError, auth.AuthError):
            return False
        return True

    def _await(self, flight: _Flight, ended: Callable[[], BaseException]) -> None:
        """Wait (holding the condition) for ``flight``, no longer than the request's deadline."""
        request = spawn.current()
        while not flight.done:
            if request is None:
                self._cond.wait()
                continue
            left = request.left()
            if request.stopped or left <= 0:
                raise ended()
            self._cond.wait(left)

    def _check_signin(self) -> None:
        """Drop what was held for a sign-in a login, logout or org switch has since replaced."""
        if (self._record is not None or self._token is not None or self._signed_out) \
                and self._read_marker() != self._signin:
            self._clear()

    def _read_marker(self) -> str:
        try:
            return self._marker.read_text()
        except OSError:
            return ""

    def _clear(self, signed_out: bool = False) -> None:
        self._record, self._base, self._token = None, None, None
        self._expires, self._renew_at = 0.0, None
        self._signed_out = signed_out

    # -- refreshing --------------------------------------------------------------------------

    def _launch(self, stale: str | None) -> _Flight:
        """Start the one refresh (holding the condition), on a thread of its own: no
        request's deadline stops it, so a token it spends is always saved."""
        flight = self._flight = _Flight()
        threading.Thread(target=self._fly, args=(flight, stale), name="brindle-sign-in-refresh",
                         daemon=True).start()
        return flight

    def _fly(self, flight: _Flight, stale: str | None) -> None:
        try:
            base, record, signin = self._fetch(stale)
        except BaseException as e:  # noqa: BLE001 - handed to every caller waiting on it
            with self._cond:
                flight.error = e
                if isinstance(e, auth.AuthError) and (e.revoked or e.code == "not_logged_in"):
                    self._clear(signed_out=True)
                    self._signin = self._read_marker()
                self._land(flight)
            return
        with self._cond:
            if self._read_marker() == signin:      # else a login replaced it meanwhile: read again
                self._take(base, record, signin)
            self._land(flight)

    def _land(self, flight: _Flight) -> None:
        flight.done = True
        self._flight = None
        self._cond.notify_all()
        self._changed.set()

    def _fresh(self, record: dict, stale: str | None) -> bool:
        token = record.get("access_token")
        return bool(token) and token != stale and \
            float(record.get("access_expires_at") or 0) > self.clock() + auth.ACCESS_SLACK

    def _fetch(self, stale: str | None) -> tuple[str, dict, str]:
        """(backend, stored sign-in with a usable access token, marker it was read under),
        all read under the refresh lock: one uncached store read, and one write if the
        token has to be rotated. A token other than ``stale`` that is still fresh (another
        brindle renewed it) is taken up as it is. The backend is always the record's own."""
        with auth.refresh_lock():
            signin = self._read_marker()
            cur = self.store.load() or {}
            if not cur.get("refresh_token"):
                raise auth.AuthError("not logged in to brindle Pro (run `brindle account login`)",
                                     code="not_logged_in")
            base = auth.base_url(cur.get("base_url"))
            if self._fresh(cur, stale):
                return base, cur, signin
            with self._cond:      # what the store holds, for requests, while the token rotates
                self._record, self._base, self._signin = dict(cur), base, signin
                self._signed_out = False
            rotated = auth._rotate(auth.Client(base, self.transport), self.store, cur, self.clock(),
                                   adopt=True)
            return base, rotated, signin

    def _take(self, base: str, record: dict, signin: str) -> None:
        now = self.clock()
        self._record, self._base, self._signin, self._signed_out = dict(record), base, signin, False
        self._token = record.get("access_token") or None
        self._expires = float(record.get("access_expires_at") or 0)
        life = self._expires - now
        # A token issued already expired has nothing to renew ahead of: requests refresh it.
        self._renew_at = now + life * (RENEW_AT + self.jitter()) if self._token and life > 0 else None

    def _saved(self, creds: dict) -> None:
        """This process saved ``creds`` through :attr:`view` (under the refresh lock)."""
        try:
            base = auth.base_url(creds.get("base_url"))
        except auth.AuthError:
            with self._cond:
                self._clear()
            return
        with self._cond:
            self._take(base, creds, self._read_marker())
            self._changed.set()

    def _deleted(self) -> None:
        with self._cond:
            self._clear(signed_out=True)
            self._signin = self._read_marker()

    # -- the background renewal ----------------------------------------------------------------

    def due_in(self) -> float | None:
        """Seconds until the next renewal (<= 0: now), or None: nothing to renew
        until a request or a sign-in gives this holder a token again."""
        with self._cond:
            return None if self._renew_at is None else self._renew_at - self.clock()

    def renew(self) -> None:
        """Renew (or, holding nothing yet, take up) the access token now, and wait for
        it. A failed renewal is tried once more when the token expires, then waits
        for a change (requests refresh an expired token themselves)."""
        with self._cond:
            self._check_signin()
            if self._signed_out:
                return
            flight = self._flight or self._launch(self._token)
            self._await(flight, SignInRefreshing)
            error = flight.error
            if error is not None:
                now = self.clock()
                self._renew_at = self._expires if self._token and self._expires > now else None
        if error is not None:
            log.info("couldn't renew the brindle Pro sign-in in the background (%s)",
                     getattr(error, "code", type(error).__name__))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="brindle-warm-sign-in", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._changed.set()

    def _run(self) -> None:
        self.renew()        # take up the stored sign-in before the first request needs it
        while not self._stopped.is_set():
            self._changed.clear()
            due = self.due_in()
            if due is not None and due <= 0:
                self.renew()
                continue
            # A refresh, a save or stop() wakes this early; a Mac that was
            # suspended may wake it late, and the first request then refreshes.
            self._changed.wait(due)


def install(holder: WarmToken | None) -> None:
    """Make ``holder`` the warm sign-in :func:`auth.authed` uses in this process."""
    auth._warm = holder


def start() -> WarmToken | None:
    """The daemon's warm sign-in, installed and renewing; None in air-gap mode."""
    from brindle import airgap
    from brindle.pro import credentials

    if airgap.enabled():
        return None
    try:
        holder = WarmToken(credentials.default_store())
    except Exception:  # noqa: BLE001 - no usable store: requests read their own
        log.info("brindle Pro sign-in isn't kept warm: no credential store")
        return None
    install(holder)
    holder.start()
    return holder


def stop(holder: WarmToken | None) -> None:
    """Stop ``holder``'s renewal and stop serving requests from it."""
    if holder is None:
        return
    holder.stop()
    if auth._warm is holder:
        install(None)
