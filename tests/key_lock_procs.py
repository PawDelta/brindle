"""Child-process bodies for the key-store lock test (spawned, so importable by name)."""

from __future__ import annotations

import fcntl

from brindle import keystore


def _without_flock(noflock: bool) -> None:
    if noflock:
        fcntl.flock = lambda fd, op: None


def update(name, value, noflock, loaded, go, done):
    """set_check, held between loading the record and saving it until ``go``."""
    _without_flock(noflock)
    store = type(keystore._key_store(name))
    load = store.load

    def load_then_hold(self):
        rec = load(self)
        loaded.set()
        go.wait(30)
        return rec

    store.load = load_then_hold
    keystore.set_check(name, "rejected", value)
    done.set()


def delete(name, noflock, trying, done):
    _without_flock(noflock)
    trying.set()
    keystore.unset_key(name)
    done.set()
