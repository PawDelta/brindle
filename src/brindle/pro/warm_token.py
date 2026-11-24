"""The daemon keeps its brindle Pro sign-in warm, so a Library request never
waits on a sign-in refresh.

Every brindle of the OS user refreshes the 15-minute access token under one
cross-process lock (:func:`brindle.pro.auth.refresh_lock`), with keychain reads
and writes inside it. A request that found its token near expiry used to wait
there for whichever brindle held the lock: on a busy machine, past its own
deadline (2026-11-24, plan sharing). Here the daemon holds the current access
token in memory and renews it in the background at about two thirds of its
life, so its requests (:func:`brindle.pro.auth.authed`) take the token from
memory and never read the store or take the lock while it is unexpired.

Only a token that has actually expired (the Mac was suspended, say, and the
renewal is late) is refreshed on a request's path, and then once for all the
requests waiting on it (single-flight in this process), each waiting no longer
than its own deadline. A refresh reads the store once and writes it once, so
other processes (the CLI, hooks, workers) see the renewed sign-in as before.
A refused refresh signs out once (:func:`auth._rotate` deletes the stored
sign-in); later requests find nothing stored and send nothing. A login or
logout anywhere replaces the marker file :func:`auth.signin_marker`, and the
token held for the sign-in it replaced is dropped at the next request.
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


class _Flight:
    """One refresh in progress, which every caller needing a token waits on."""

    def __init__(self) -> None:
        self.done = False
        self.error: BaseException | None = None


class WarmToken:
    """The access token of ``store``'s sign-in, held in memory and renewed
    before it expires. ``clock`` is wall-clock time (a token's expiry is wall
    time: monotonic time stands still while the Mac is suspended)."""

    def __init__(self, store, transport: auth.Transport | None = None, *,
                 clock: Callable[[], float] = time.time,
                 jitter: Callable[[], float] | None = None) -> None:
        self.store, self.key = store, store_key(store)
        self.transport = transport
        self.clock = clock
        self.jitter = jitter or (lambda: random.uniform(-JITTER, JITTER))
        self._marker = auth.signin_marker()
        self._cond = threading.Condition()
        self._token: str | None = None
        self._expires = 0.0
        self._renew_at: float | None = None
        self._base: str | None = None
        self._signin = ""              # the marker as it was when the token was taken
        self._flight: _Flight | None = None
        self._changed = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    # -- requests ---------------------------------------------------------------------------

    def serves(self, store) -> bool:
        return self.key is not None and store_key(store) == self.key

    def access(self, client: auth.Client, stale: str | None = None) -> str | None:
        """An unexpired access token for ``client``'s backend, never ``stale`` (one
        the backend just refused). None when this sign-in is held for another
        backend. A token that has expired is refreshed once for every caller."""
        with self._cond:
            while True:
                self._check_signin()
                if self._base is not None and client.base != self._base:
                    return None
                if self._token and self._token != stale and self._expires > self.clock():
                    return self._token
                flight = self._flight
                if flight is None:
                    flight = self._flight = _Flight()
                    break
                self._await(flight)
                if flight.error is not None:
                    raise flight.error
        self._fly(flight, client.base, stale)
        with self._cond:
            return self._token if client.base == self._base else None

    def _await(self, flight: _Flight) -> None:
        """Wait (holding the condition) for ``flight``, no longer than the request's deadline."""
        request = spawn.current()
        while not flight.done:
            if request is None:
                self._cond.wait()
                continue
            left = request.left()
            if request.stopped or left <= 0:
                raise auth.AuthError("the request ended while waiting for the sign-in refresh",
                                     code="lock_timeout")
            self._cond.wait(left)

    def _check_signin(self) -> None:
        """Drop a token held for a sign-in a login or logout has since replaced."""
        if self._token is not None and self._read_marker() != self._signin:
            self._clear()

    def _read_marker(self) -> str:
        try:
            return self._marker.read_text()
        except OSError:
            return ""

    def _clear(self) -> None:
        self._token, self._expires, self._renew_at, self._base = None, 0.0, None, None

    # -- refreshing --------------------------------------------------------------------------

    def _fly(self, flight: _Flight, base: str | None, stale: str | None) -> None:
        """Run ``flight``: the one refresh its waiters share."""
        try:
            base, record, signin = self._fetch(base, stale)
        except BaseException as e:
            with self._cond:
                flight.error = e
                if isinstance(e, auth.AuthError) and (e.revoked or e.code == "not_logged_in"):
                    self._clear()
                self._land(flight)
            raise
        with self._cond:
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

    def _fetch(self, base: str | None, stale: str | None) -> tuple[str, dict, str]:
        """(backend, stored sign-in with a usable access token, marker it was read under).
        A first read adopts a fresh stored token without the lock; otherwise, under the
        refresh lock, a token another brindle renewed meanwhile is adopted, and only a
        stale one is rotated: one store read and one write."""
        signin = self._read_marker()
        if stale is None and self._token is None:
            cur = self.store.load() or {}
            if not cur.get("refresh_token"):
                raise auth.AuthError("not logged in to brindle Pro (run `brindle account login`)",
                                     code="not_logged_in")
            base = base or auth.base_url(cur.get("base_url"))
            if self._fresh(cur, None):
                return base, cur, signin
        with auth.refresh_lock():
            signin = self._read_marker()
            cur = self.store.load() or {}
            if not cur.get("refresh_token"):
                raise auth.AuthError("not logged in to brindle Pro (run `brindle account login`)",
                                     code="not_logged_in")
            base = base or auth.base_url(cur.get("base_url"))
            if self._fresh(cur, stale):
                return base, cur, signin
            client = auth.Client(base, self.transport)
            return base, auth._rotate(client, self.store, cur, self.clock(), adopt=True), signin

    def _take(self, base: str, record: dict, signin: str) -> None:
        now = self.clock()
        self._token = record["access_token"]
        self._expires = float(record.get("access_expires_at") or 0)
        life = self._expires - now
        # A token issued already expired has nothing to renew ahead of: requests refresh it.
        self._renew_at = now + life * (RENEW_AT + self.jitter()) if life > 0 else None
        self._base, self._signin = base, signin

    # -- the background renewal ----------------------------------------------------------------

    def due_in(self) -> float | None:
        """Seconds until the next renewal (<= 0: now), or None: nothing to renew
        until a request or a sign-in gives this holder a token again."""
        with self._cond:
            return None if self._renew_at is None else self._renew_at - self.clock()

    def renew(self) -> None:
        """Renew (or, holding nothing yet, take up) the access token now. A failed
        renewal is tried once more when the token expires, then waits for a change."""
        with self._cond:
            self._check_signin()
            if self._flight is not None:
                return      # a request's refresh is out: its result wakes the loop
            flight = self._flight = _Flight()
            stale, base = self._token, self._base
        try:
            self._fly(flight, base, stale)
        except Exception as e:  # noqa: BLE001 - requests refresh on demand; say why and wait
            log.info("couldn't renew the brindle Pro sign-in in the background (%s)",
                     getattr(e, "code", type(e).__name__))
            with self._cond:
                now = self.clock()
                self._renew_at = self._expires if self._token and self._expires > now else None

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
            # A request's refresh, a renewal or stop() wakes this early; a Mac that
            # was suspended may wake it late, and the first request then refreshes.
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
