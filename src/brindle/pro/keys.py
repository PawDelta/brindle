"""Pinned Ed25519 public keys that may sign brindle documents (entitlements,
policies, revocations; see :mod:`brindle.pro.signed`), by ``kid``.

Only keys listed here are trusted; a token naming any other ``kid`` is
rejected. There is deliberately no way to add a key at runtime from the
environment, a config file or the network.

Two slots: the *active* key signs everything the backend issues today; the
*standby* key is pinned ahead of need and kept offline. If the active key is
compromised, the standby key signs a revocation of the active ``kid``
(``brindle-revocation+jwt``, claim ``revoked_kid``). Clients that fetch it
refuse every token the active key signed from then on, and the backend
switches to signing with the standby key, which every client already trusts.
A revocation counts only when a *different* pinned key signed it.

Rotation: add the next key here and release *before* the backend signs with
it; switch the backend to the new ``kid``; drop the old ``kid`` in a later
release, once every token it signed is past ``exp`` + ``license.MAX_GRACE``.

Values are the raw 32-byte public keys, hex-encoded; each ``kid`` is its key's
RFC 7638 thumbprint.

Pinning the standby key (an operator, before any release that needs it):

1. Create a second Ed25519 key in KMS, beside the active one.
2. Set ``BRINDLE_KMS_STANDBY_KEY_ID`` on the server to that key's id.
3. Take the key's public key as raw 32 bytes, base64url-encoded. Its RFC 7638
   thumbprint of that value is the ``kid``. Set ``STANDBY_KID`` to the
   thumbprint and ``STANDBY_KEY`` to the same 32 bytes hex-encoded, as the
   verifier reads them.
4. Ship that in a release before the key is ever needed.

Revoking the active key: the standby key signs a ``brindle-revocation+jwt``
token naming the active ``kid`` in ``revoked_kid``. The backend serves
revocations at ``GET /keys/revocations``, and clients pick them up on their
next refresh. The revocation counts only when a different pinned key signed
it, so the standby key must sign it, not the active one.
"""

from __future__ import annotations

# Production signing key, created 2026-09-30.
ACTIVE_KID = "sQ88-LFjWYgISL3A6CcjMe9BEzThVtgGgKpbXH9vCIk"
ACTIVE_KEY = "370e58a97ce3c85fcacf18693ed1607addefbcb63794e0fea4e615eee310fba0"

# The standby key: empty until an operator pins it (see the module docstring).
# Fill both together, in a release made before the key is needed.
STANDBY_KID: str | None = None
STANDBY_KEY: str | None = None

PINNED_KEYS: dict[str, str] = {
    ACTIVE_KID: ACTIVE_KEY,
    **({STANDBY_KID: STANDBY_KEY} if STANDBY_KID and STANDBY_KEY else {}),
}
