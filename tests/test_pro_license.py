"""Offline entitlement verification, the pinned keys, the credential stores,
and the current entitlement (refresh, offline grace, failing closed)."""
import base64
import hashlib
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brindle import keystore
from brindle.pro import auth, credentials, keys, license
from brindle.pro.license import LicenseError
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, ISS, TEST_KID, b64, backend, claims, pro_env, sign, signing_key, token,
)

DAY = 86400


def verify(token, **kw):
    kw.setdefault("issuer", ISS)
    return license.verify(token, **kw)


# -- verification ------------------------------------------------------------------------


def test_token_use_must_be_entitlement(token):
    with pytest.raises(LicenseError, match="not an entitlement"):
        verify(token(token_use="access"))


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB"),       # signature
    lambda t: ".".join([t.split(".")[0], b64(json.dumps(claims(plan="enterprise")).encode()),
                        t.split(".")[2]]),                                     # payload swapped
    lambda t: t.rsplit(".", 1)[0],
    lambda t: "a" * (license.MAX_TOKEN_BYTES + 1),
])
def test_tampered_or_malformed_tokens_are_rejected(token, mangle):
    with pytest.raises(LicenseError):
        verify(mangle(token()))


@pytest.mark.parametrize("alg", ["none", ""])
def test_wrong_alg_is_rejected(signing_key, alg):
    with pytest.raises(LicenseError, match="algorithm"):
        verify(sign(signing_key, claims(), {"alg": alg}))


def test_unknown_kid_is_rejected(signing_key):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseError, match="unknown key"):
        verify(sign(other, claims(kid="rogue"), {"kid": "rogue"}))


def test_right_kid_wrong_key_is_rejected(signing_key):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseError, match="signature"):
        verify(sign(other, claims()))


def test_kid_header_and_claim_must_match(signing_key):
    with pytest.raises(LicenseError, match="kid"):
        verify(sign(signing_key, claims(kid="something-else"), {"kid": TEST_KID}))


def test_crit_header_is_rejected(signing_key):
    with pytest.raises(LicenseError, match="critical"):
        verify(sign(signing_key, claims(), {"crit": ["exp"]}))


@pytest.mark.parametrize("claim", ["jti", "token_use"])
def test_missing_claims_are_rejected(token, claim):
    with pytest.raises(LicenseError, match="missing"):
        verify(token(**{claim: ...}))


@pytest.mark.parametrize("over", [{"status": 3}, {"policy_role": ""}])
def test_malformed_claims_are_rejected(token, over):
    with pytest.raises(LicenseError):
        verify(token(**over))


def test_future_iat_is_rejected(token):
    now = time.time()
    with pytest.raises(LicenseError, match="not valid yet"):
        verify(token(iat=int(now + 3600), exp=int(now + 7200)))


# -- the test-only key hook ------------------------------------------------------------------


def test_test_hook_cannot_shadow_a_pinned_kid(monkeypatch):
    monkeypatch.setitem(keys.PINNED_KEYS, "prod", "00" * 32)
    with pytest.raises(RuntimeError):
        with license._test_signing_key("prod", Ed25519PrivateKey.generate().public_key()):
            pass


def test_pinned_keys_are_trusted(monkeypatch):
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setitem(license.PINNED_KEYS, "pinned-1", raw.hex())
    assert verify(sign(key, claims(kid="pinned-1"))).kid == "pinned-1"


def _thumbprint(raw_hex: str) -> str:
    x = base64.urlsafe_b64encode(bytes.fromhex(raw_hex)).rstrip(b"=").decode()
    jwk = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(jwk.encode()).digest()).rstrip(b"=").decode()


@pytest.mark.variant("integrity: a mispinned kid rejects every real token")
def test_every_pinned_kid_is_its_keys_thumbprint():
    """As the backend derives it (RFC 7638), or every token it signs is rejected."""
    assert keys.PINNED_KEYS, "no production key pinned"
    for kid, raw in keys.PINNED_KEYS.items():
        assert len(bytes.fromhex(raw)) == 32
        assert _thumbprint(raw) == kid


# -- the credential stores ---------------------------------------------------------------------


def test_pro_stores_use_their_own_service(monkeypatch):
    assert credentials.KeychainStore().service == "brindle-pro"
    assert credentials.SecretServiceStore().attrs[:2] == ["service", "brindle-pro"]
    assert issubclass(credentials.FileStore, keystore.FileStore)
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "keychain")
    assert isinstance(credentials.default_store(), credentials.KeychainStore)
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "bogus")
    with pytest.raises(credentials.CredentialError):
        credentials.default_store()


def test_file_store_refuses_a_symlink(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": "a"})
    target = tmp_path / "elsewhere"
    os.replace(store.path, target)
    os.symlink(target, store.path)
    with pytest.raises(credentials.CredentialError):
        store.load()


def test_file_store_rejects_corrupt_contents(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({})
    fd = os.open(store.path, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, b"not base64 !!")
    os.close(fd)
    with pytest.raises(credentials.CredentialError, match="corrupt"):
        store.load()


def test_keychain_writes_the_secret_on_stdin_not_argv(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(credentials.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or R())
    credentials.KeychainStore().save({"access_token": "SECRET"})
    argv, kw = calls[0]
    assert argv == ["security", "-i"]
    assert "SECRET" not in " ".join(argv)
    assert credentials._encode({"access_token": "SECRET"}) in kw["input"]


# -- features() / require() and the current entitlement ---------------------------------------------


def logged_in(backend, entitlement, *, access_valid=True):
    """Stored credentials as if logged in through ``backend``."""
    t = backend.issue()
    store = credentials.default_store()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + (900 if access_valid else -1),
                "entitlement": entitlement, "base_url": BASE})
    return store


def client(transport):
    return auth.Client(BASE, transport)


def test_features_fail_closed_without_credentials():
    assert license.features() == frozenset()
    assert not license.has("learning")
    with pytest.raises(LicenseError, match="not logged in"):
        license.require("learning")


def test_license_cached(backend, token, monkeypatch):
    """A second process reuses the entitlement without a keystore read, until
    brindle's own commands clear it. A read in flight across a logout can't
    put the old login back."""
    store = logged_in(backend, token())
    first = license.current()
    license._cache = None   # as in a new process
    monkeypatch.setattr(credentials, "default_store", lambda: pytest.fail("keystore read"))
    assert license.current() == first
    assert oct(license._disk_cache_path().stat().st_mode & 0o777) == "0o600"
    license.clear_cache()
    with pytest.raises(pytest.fail.Exception):
        license.current()

    class LogoutDuringRead:   # reads the old login, then the user logs out before it's cached
        def load(self):
            old = store.load()
            auth.logout(None, store)
            return old

    monkeypatch.setattr(credentials, "default_store", LogoutDuringRead)
    assert license.current() == first
    license._cache = None
    monkeypatch.setattr(credentials, "default_store", lambda: store)
    with pytest.raises(LicenseError, match="not logged in"):
        license.current()


def test_features_fail_closed_on_a_loose_credentials_file(backend, token):
    store = logged_in(backend, token())
    os.chmod(store.path, 0o644)
    assert license.features() == frozenset()


def test_features_fail_closed_on_a_forged_token(backend, token, monkeypatch):
    logged_in(backend, token()[:-6] + "AAAAAA")
    monkeypatch.setattr(auth, "UrllibTransport", lambda: backend)
    backend.routes["GET /entitlement"] = [(503, {"error": "unavailable"})]
    assert license.features() == frozenset()
    with pytest.raises(LicenseError):
        license.current(refresh=False)


def test_features_fail_closed_on_a_stolen_entitlement_from_another_issuer(backend, signing_key):
    logged_in(backend, sign(signing_key, claims(iss="https://evil.test")))
    backend.routes["GET /entitlement"] = [auth.TransportError("down")]
    assert license.features() == frozenset()


def test_revoked_session_fails_closed_and_forgets_credentials(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 2 * DAY), exp=int(now - DAY)), access_valid=False)
    backend.revoked = True
    with pytest.raises(LicenseError, match="brindle account login"):
        license.current(store=store, client=client(backend))
    assert store.load() is None


@pytest.mark.variant("paid vs free is one conditional expression; coverage cannot tell its sides apart")
@pytest.mark.parametrize("plan,says", [("pro", "brindle account login"),
                                       ("free", "no active brindle Pro subscription")])
def test_expired_past_grace_offline_asks_only_subscribers_to_log_in(backend, token, plan, says):
    now = time.time()
    store = logged_in(backend, token(plan=plan, iat=int(now - 30 * DAY), exp=int(now - 29 * DAY)),
                      access_valid=False)
    backend.routes["POST /token/refresh"] = [auth.TransportError("down")]
    with pytest.raises(LicenseError, match=says):
        license.current(store=store, client=client(backend))


def test_a_cancelled_subscription_is_announced_once(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 2 * DAY), exp=int(now - DAY)), access_valid=False)
    backend.plan = "free"
    assert license.current(store=store, client=client(backend)).plan == "free"
    assert license.ended_notice(store) == license.ENDED_NOTICE
    license.ended_notice_told(store)
    assert license.ended_notice(store) is None


# -- development keys (JWKS from a localhost backend) ------------------------------------------


def test_no_environment_makes_an_unpinned_key_trusted(monkeypatch):
    # Dev mode and a localhost issuer once let the client trust keys a local
    # server served; nothing outside keys.py may add a trusted key.
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")
    local = "http://localhost:8000/api/brindle/v1"
    with pytest.raises(LicenseError, match="unknown key"):
        license.verify(sign(key, claims(kid="unpinned", iss=local)), issuer=local)


def test_errors_never_contain_the_token(token, caplog):
    t = token(aud="wrong")
    with pytest.raises(LicenseError) as e:
        verify(t)
    assert t not in str(e.value) and t.split(".")[1] not in str(e.value)
    assert t not in caplog.text
