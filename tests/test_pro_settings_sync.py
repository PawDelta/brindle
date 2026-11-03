"""Settings sync: ~/.brindle/config.json's user-wide keys follow a Pro person
across machines through /me/settings, allowlisted keys only."""

import json
import time

import pytest

from brindle import config
from brindle.pro import auth, credentials, settings_sync
from pro_fixtures import (  # noqa: F401 -- fixtures
    BASE, backend, claims, pro_env, sign, signing_key, token,
)


@pytest.fixture
def store(backend, brindle_home):
    s = credentials.FileStore(brindle_home / "pro")
    t = backend.issue()
    s.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
            "access_expires_at": time.time() + 900, "base_url": BASE,
            "entitlement": sign(backend.key, claims(features=["settings_sync"]))})
    return s


def test_only_allowlisted_keys_travel_in_either_direction(backend, store):
    p = config.user_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(delegation="balanced", pr_footer=False, max_agents=3, learning="off",
                                 plugins={"x": "y"}, sidebar="x" * 33, secret_token="hunter2")))
    c = auth.Client(BASE, backend)
    assert settings_sync.push(c, store).action == "pushed"
    (_, form, _), = [c_ for c_ in backend.calls if c_[0] == "PUT /me/settings"]
    assert form["settings"] == {"delegation": "balanced", "pr_footer": False, "max_agents": 3}
    assert backend.settings == {"delegation": "balanced", "pr_footer": False, "max_agents": 3}
    backend.settings = {"delegation": "fast", "sidebar": "bottom", "evil": "x"}
    backend.settings_at += 10
    r = settings_sync.pull(c, store)
    assert r.action == "pulled" and r.changes["delegation"] == ("balanced", "fast")
    local = json.loads(p.read_text())
    assert local["delegation"] == "fast" and local["sidebar"] == "bottom" and "evil" not in local
    assert local["secret_token"] == "hunter2" and local["learning"] == "off"
