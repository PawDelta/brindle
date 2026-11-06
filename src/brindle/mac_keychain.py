"""A keychain item of this program's own, through Security.framework (macOS).

Unlike an item made with ``/usr/bin/security`` (brindle.keystore), which any
process can read back by running ``security``, an item made here carries the
identity of the program that made it: macOS lets that program read it silently
and asks before giving it to any other (``security``, another app, a script on
another interpreter). So it only means something when the program has a stable
signed identity, which is the engine's helper app (app/docs/bundled-engine.md):
see brindle.github_connect for who may use this and who asks the daemon instead.

Every call says whether macOS may show a dialog (``ask``). With it off, a locked
keychain, or an item macOS would have asked about, fails at once with Refused:
nothing a poll does can raise a dialog. The setting is the process's, so calls
are serialized.

Reached with ctypes: the functions are Security's file-keychain C API, which
needs no native code of brindle's.
"""

from __future__ import annotations

import ctypes
import sys
import threading

SECURITY = "/System/Library/Frameworks/Security.framework/Security"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
NOT_FOUND = -25300
UNLOCKED = 1                 # SecKeychainStatus: kSecUnlockStateStatus
READ_WAIT = 5.0              # seconds a read waits behind a write that has a dialog up

_lock = threading.Lock()
_libs: tuple[ctypes.CDLL, ctypes.CDLL] | None = None


class Refused(Exception):
    """macOS didn't hand the item over (or take it) without asking, and wasn't allowed to
    ask. ``locked``: the keychain is locked, which passes; otherwise the item isn't this
    program's to read any more. ``status``: Security's OSStatus. Never carries a secret."""

    def __init__(self, status: int, locked: bool) -> None:
        super().__init__(f"keychain refused (OSStatus {status}{', locked' if locked else ''})")
        self.status, self.locked = status, locked


def available() -> bool:
    return sys.platform == "darwin"


def security() -> ctypes.CDLL:
    """Security.framework, loaded once."""
    global _libs
    if _libs is None:
        _libs = (ctypes.CDLL(SECURITY), ctypes.CDLL(CORE_FOUNDATION))
        _libs[1].CFRelease.argtypes = [ctypes.c_void_p]
    return _libs[0]


class Item:
    """One generic-password item, ``account`` under ``service``, in ``keychain`` (a
    SecKeychainRef; None: the person's default keychain, their login one)."""

    def __init__(self, service: str, account: str, keychain: ctypes.c_void_p | None = None) -> None:
        self.service, self.account, self.keychain = service.encode(), account.encode(), keychain

    def _locked(self) -> bool:
        status = ctypes.c_uint32()
        if security().SecKeychainGetStatus(self.keychain, ctypes.byref(status)) != 0:
            return True
        return not status.value & UNLOCKED

    def _find(self, want_data: bool) -> tuple[int, bytes | None, ctypes.c_void_p]:
        length, data, ref = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = security().SecKeychainFindGenericPassword(
            self.keychain, len(self.service), self.service, len(self.account), self.account,
            ctypes.byref(length) if want_data else None, ctypes.byref(data) if want_data else None, ctypes.byref(ref))
        secret = None
        if status == 0 and want_data:
            secret = ctypes.string_at(data, length.value)
            security().SecKeychainItemFreeContent(None, data)
        return status, secret, ref

    def _release(self, ref: ctypes.c_void_p) -> None:
        if ref:
            _libs[1].CFRelease(ref)

    def load(self) -> bytes | None:
        """The item's secret, None when there is no such item. Never shows a dialog:
        Refused when macOS would have."""
        if not _lock.acquire(timeout=READ_WAIT):
            raise Refused(0, True)      # someone is at a dialog: as good as locked, for now
        try:
            security().SecKeychainSetUserInteractionAllowed(False)
            status, secret, ref = self._find(want_data=True)
            self._release(ref)
            if status == NOT_FOUND:
                return None
            if status != 0:
                raise Refused(status, self._locked())
            return secret
        finally:
            _lock.release()

    def save(self, secret: bytes, ask: bool) -> None:
        """Put ``secret`` in the item, replacing what's there. ``ask``: the person is
        here and asked for this, so macOS may show its unlock dialog."""
        with _lock:
            sec = security()
            sec.SecKeychainSetUserInteractionAllowed(bool(ask))
            try:
                status, _, ref = self._find(want_data=False)
                if status == 0:
                    status = sec.SecKeychainItemModifyAttributesAndData(ref, None, len(secret), secret)
                    self._release(ref)
                elif status == NOT_FOUND:
                    status = sec.SecKeychainAddGenericPassword(
                        self.keychain, len(self.service), self.service, len(self.account), self.account,
                        len(secret), secret, None)
                if status != 0:
                    raise Refused(status, self._locked())
            finally:
                sec.SecKeychainSetUserInteractionAllowed(False)

    def delete(self) -> None:
        """Remove the item, if it's there and this program may. Never shows a dialog."""
        with _lock:
            sec = security()
            sec.SecKeychainSetUserInteractionAllowed(False)
            status, _, ref = self._find(want_data=False)
            if status == 0:
                sec.SecKeychainItemDelete(ref)
                self._release(ref)
