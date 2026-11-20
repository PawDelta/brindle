"""Settings › Library location: approved plans kept on this Mac (nothing
uploaded, and per account), moving them between here and the servers without
losing or doubling one, and an org's or team's enforced location beating the
person's choice."""
import time

import pytest

from brindle import plans
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials, license, local_library
from pro_fixtures import BASE, backend, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures
from test_daemon_api import AUTH, _get

MINE = "GET /analysis-docs?scope_kind=user&scope_id=user_1"
SHARED = [{"kind": "user", "id": "user_1"}, {"kind": "group", "id": "team_1"}]


def sign_in(backend, sub="user_1", features=("analysis_docs",)):
    t = backend.issue()
    credentials.default_store().save({
        "access_token": t["access_token"], "refresh_token": t["refresh_token"], "access_expires_at": time.time() + 900,
        "base_url": BASE, "entitlement": sign(backend.key, claims(sub=sub, features=list(features)))})
    license.clear_cache()


@pytest.fixture
def remote(backend, monkeypatch):
    """The fake Library behind every Pro call, signed in with the library feature."""
    backend.switches, backend.uploads = {"store": "off"}, []
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    backend.routes["PUT /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": f["switches"]})
    backend.routes["POST /analysis-docs"] = lambda f, h: backend.uploads.append(f) or (200, {"id": "uploaded"})
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: backend.request(*a))
    sign_in(backend)
    return backend


def sent(remote):
    return [c for c in remote.paths() if not c.startswith("GET ")]


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
    assert sent(remote) == []       # nothing uploaded


def test_another_account_never_lists_or_uploads_the_plans(remote):
    remote.routes["GET /analysis-docs/scopes"] = [(200, {"scopes": [{"kind": "user", "id": "user_2"}]})]
    remote.routes["GET /analysis-docs?scope_kind=user&scope_id=user_2"] = [(200, {"docs": []})]
    mine = local_library.add([{"name": "plan.md", "md": "# Mine"}], {}, identity="a" * 40)["doc_id"]

    sign_in(remote, sub="user_2")
    assert _get("/v1/analysis-docs", AUTH)[1]["docs"] == []
    assert _get(f"/v1/analysis-docs/{mine}", AUTH)[0] != 200
    assert local_library.set_location("server")["moved"] == 0 and remote.uploads == []

    sign_in(remote)
    assert [r["doc_id"] for r in local_library.records()] == [mine]


@pytest.mark.parametrize("fails", ["delete", pytest.param("write", marks=pytest.mark.variant(
    "a server copy is deleted only once its local write has succeeded"))])
def test_move_to_local_keeps_every_plan(fails, remote, monkeypatch):
    remote.switches = {"store": "on"}
    remote.routes[MINE] = [(200, {"docs": [{"doc_id": "d1", "title": "One"}, {"doc_id": "d2", "title": "Two"}]})]
    for d in ("d1", "d2"):
        remote.routes[f"GET /analysis-docs/{d}"] = [(200, {"id": d, "files": [{"name": "plan.md", "md": d}],
                                                           "scopes": SHARED[:1]})]
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


def test_round_trip_keeps_one_record_per_plan_and_its_repo(remote):
    """A plan kept here and shared, and a downloaded one, survive repeated
    moves to this Mac as one record each, and go back under their own repo."""
    kept = local_library.add([{"name": "plan.md", "md": "# Kept"}], {}, identity="a" * 40)["doc_id"]
    assert local_library.server_copy(kept) == "uploaded"            # shared: its server copy exists
    remote.routes[MINE] = [(200, {"docs": [{"doc_id": "uploaded"}, {"doc_id": "d1"}]})]
    remote.routes["GET /analysis-docs/uploaded"] = [(200, {"id": "uploaded", "files": [], "scopes": SHARED})]
    remote.routes["GET /analysis-docs/d1"] = [(200, {"id": "d1", "files": [{"name": "p.md", "md": "d1"}],
                                                     "repo_key": "rk1", "scopes": SHARED[:1]})]
    remote.routes["DELETE /analysis-docs/d1"] = [(200, {})]

    for _ in range(2):
        local_library.set_location("local")
        assert sorted(r["doc_id"] for r in local_library.records()) == sorted([kept, "d1"])

    remote.uploads.clear()
    assert local_library.set_location("server")["left"] == 0
    assert [u["repo_key"] for u in remote.uploads] == ["rk1"]       # d1 only: kept's shared copy stands
    assert local_library.records() == []


@pytest.mark.parametrize("status", [503, pytest.param(404, marks=pytest.mark.variant(
    "a server copy that is gone is uploaded again rather than assumed"))])
def test_move_to_servers_keeps_a_plan_whose_server_copy_is_unconfirmed(status, remote):
    kept = local_library.add([{"name": "plan.md", "md": "# Kept"}], {}, identity="a" * 40)["doc_id"]
    local_library.server_copy(kept)
    remote.uploads.clear()
    remote.routes["GET /analysis-docs/uploaded"] = [(status, {"error": "unavailable"})]

    got = local_library.set_location("server")

    if status == 503:           # not confirmed: the local copy stays, counted as left
        assert (got["left"], len(remote.uploads)) == (1, 0) and local_library.get(kept) is not None
    else:                       # gone: uploaded again before the local copy goes
        assert (got["left"], len(remote.uploads)) == (0, 1) and local_library.get(kept) is None


@pytest.mark.parametrize("level", [pytest.param("team", marks=pytest.mark.variant(
    "a team's enforced store beats the person's saved choice, learned by the daemon's own read")),
                                   pytest.param("org", marks=pytest.mark.variant(
    "an Enterprise org's own enforced value applies when its team's switches are empty"))])
def test_enforced_location_beats_the_persons_choice(level, tmp_path, remote):
    from brindle.daemon import routes_actions

    sign_in(remote, features=("analysis_docs", "analysis_docs_team", "analysis_docs_enterprise"))
    enforced = {"store": "enforced_on"}
    remote.routes["GET /orgs/org_1/analysis-docs/policy"] = [(200, {
        "switches": enforced if level == "org" else {}, "groups": {"team_1": {"switches": enforced if level == "team" else {}}}})]
    remote.routes["GET /analysis-docs/scopes"] = [(200, {"scopes": SHARED})]
    local_library.remember("local")

    assert routes_actions.library_on_this_mac() is False        # the daemon's own read learns the team
    state = next(s for s in ad.switch_states() if s["name"] == "store")
    assert state["locked"] and state["on"]                       # shown as locked on, not the person's choice
    with pytest.raises(ad.Locked):
        local_library.set_location("local")
    assert sent(remote) == []
    up = ad.AnalysisDocs(str(tmp_path), identity=lambda: "a" * 40, start_thread=False)
    up.submit([ad.Doc(files=[{"name": "plan.md", "md": "# Plan"}], source={"kind": "goal"}, plan=True)])
    assert up.flush()
    assert len(remote.uploads) == 1 and local_library.records() == []  # stored on the servers
