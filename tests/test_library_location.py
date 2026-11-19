"""Settings › Library location: approved plans kept on this Mac (nothing
uploaded), moving them between here and the servers without losing one, and
an org's enforced location beating the person's choice."""
import pytest

from brindle import plans
from brindle.pro import analysis_docs as ad
from brindle.pro import analysis_policy, auth, credentials, local_library
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures
from test_daemon_api import AUTH, _get
from test_pro_remote_learning import login_as

MINE = "GET /analysis-docs?scope_kind=user&scope_id=user_1"


@pytest.fixture
def remote(backend, monkeypatch):
    """The fake Library behind every Pro call, signed in with the library feature."""
    backend.switches = {"store": "off"}
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    backend.routes["PUT /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": f["switches"]})
    backend.routes["POST /analysis-docs"] = lambda f, h: (200, {"id": "uploaded"})
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    login_as(backend, credentials.default_store(), features=("analysis_docs",))
    return backend


def test_local_mode_approval_is_listed_and_nothing_is_sent(tmp_path, remote, monkeypatch):
    remote.routes["GET /analysis-docs/scopes"] = [(200, {"scopes": [{"kind": "user", "id": "user_1"}]})]
    remote.routes[MINE] = [(200, {"docs": []})]
    up = ad.AnalysisDocs(str(tmp_path), identity=lambda: "a" * 40, start_thread=False)
    monkeypatch.setitem(ad._instances, str(tmp_path), up)
    p = {"id": "pl1", "root_id": "sup", "rev": 1, "markdown": "# Cache plan\n\nShorten the TTL.",
         "decisions": [], "split": [], "visual": {"repo_root": str(tmp_path)}}

    plans._to_library(p, {}, [], None, {})
    assert up.flush()

    code, body = _get("/v1/analysis-docs", AUTH)
    assert code == 200 and [d["title"] for d in body["docs"]] == ["Cache plan"]
    code, doc = _get(f"/v1/analysis-docs/{body['docs'][0]['doc_id']}", AUTH)
    assert code == 200 and "Shorten the TTL." in doc["files"][0]["md"]
    assert not [c for c in remote.paths() if not c.startswith("GET ")]       # nothing uploaded


@pytest.mark.parametrize("fails", ["delete", pytest.param("write", marks=pytest.mark.variant(
    "a server copy is deleted only once its local write has succeeded"))])
def test_move_to_local_keeps_every_plan(fails, remote, monkeypatch):
    remote.switches = {"store": "on"}
    remote.routes[MINE] = [(200, {"docs": [{"doc_id": "d1", "title": "One"}, {"doc_id": "d2", "title": "Two"}]})]
    for d in ("d1", "d2"):
        remote.routes[f"GET /analysis-docs/{d}"] = [(200, {"id": d, "files": [{"name": "plan.md", "md": d}],
                                                           "scopes": [{"kind": "user", "id": "user_1"}]})]
    remote.routes["DELETE /analysis-docs/d1"] = [(200, {})]
    remote.routes["DELETE /analysis-docs/d2"] = [(500, {"error": "boom"}) if fails == "delete" else (200, {})]
    add = local_library.add

    def flaky_add(files, source, **kw):
        if fails == "write" and kw.get("doc_id") == "d2":
            raise OSError("disk full")
        return add(files, source, **kw)

    monkeypatch.setattr(local_library, "add", flaky_add)

    got = local_library.set_location("local")

    assert (got["moved"], got["left"]) == (1, 1)
    assert local_library.choice() == "local"
    assert local_library.get("d1")["files"] == [{"name": "plan.md", "md": "d1"}]
    if fails == "delete":       # kept here, and on the server too
        assert local_library.get("d2")["files"] == [{"name": "plan.md", "md": "d2"}]
    else:                       # not written here: never deleted there
        assert local_library.get("d2") is None
        assert "DELETE /analysis-docs/d2" not in remote.paths()


def test_enforced_location_beats_the_persons_choice(remote):
    analysis_policy.save_cached("org_1", {"switches": {"store": "enforced_on"}, "groups": {}})
    local_library.remember("local")

    assert local_library.location() == "server"
    with pytest.raises(ad.Locked):
        local_library.set_location("local")
    assert not [c for c in remote.paths() if not c.startswith("GET ")]
