"""Planning governance is applied before presenting or copying a link."""
import pytest

from brindle.pro import analysis_docs as ad
from brindle.pro import analysis_policy, local_library, share_links
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_library_location import remote, sign_in  # noqa: F401
from test_plan_share_link import DOC, LINK, Library, parts  # noqa: F401
from test_pro_team import ORG, run, team  # noqa: F401


@pytest.mark.parametrize("location,forced", [("local", "enforced_on"), pytest.param(
    "server", "enforced_off", marks=pytest.mark.variant("forced local storage prevents an opted-in upload"))])
def test_forced_location_wins(location, forced, remote):
    sign_in(remote, features=("analysis_docs", "analysis_docs_team", "analysis_docs_enterprise"))
    policy = {"switches": {"store": forced}, "groups": {}}
    remote.routes["GET /orgs/org_1/analysis-docs/policy"] = lambda f, h: (200, policy)
    remote.routes["GET /analysis-docs/scopes"] = lambda f, h: (200, {"scopes": []})
    local_library.remember(location)
    state = next(s for s in ad.switch_states() if s["name"] == "store")
    assert state["locked"] and state["on"] == (forced == "enforced_on")
    assert local_library.location() != location
    with pytest.raises(ad.Locked):
        local_library.set_location(location)


def test_off_prevents_copy_even_without_an_existing_link(parts, backend):
    lib = Library(backend)
    kw = parts(("analysis_docs", "analysis_docs_team", "analysis_docs_enterprise"))
    policy = {"switches": {}, "groups": {}, "share_links": "off"}
    backend.routes["GET /orgs/org_1/analysis-docs/policy"] = lambda f, h: (200, policy)
    state = share_links.read(DOC, **kw)
    assert state["access"] == "off" and state["link"] is None
    with pytest.raises(ServiceError) as refused:
        share_links.copy(DOC, **kw)
    assert refused.value.code == "share_links_off" and lib.made == 0
    assert not any(p.endswith(LINK) for p in backend.paths())
    assert share_links.revoke(DOC, **kw)["link"] is None


def test_holding_org_off_prevents_copy_of_existing_link(parts, backend):
    lib = Library(backend)
    kw = parts(("analysis_docs",))
    lib.create({}, {})
    lib.link["access"] = "off"
    assert share_links.read(DOC, **kw)["access"] == "off"
    with pytest.raises(ServiceError) as refused:
        share_links.copy(DOC, **kw)
    assert refused.value.code == "share_links_off"
    assert lib.made == 1


def test_admin_sets_links_without_replacing_other_controls(team):
    policy = {"switches": {"store": "enforced_off"}, "groups": {}, "share_links": "org"}
    path = f"/orgs/{ORG}/analysis-docs/policy"
    team.routes[f"GET {path}"] = lambda f, h: (200, policy)
    team.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": {}})

    def put(form, headers):
        policy.clear()
        policy.update(form)
        return 200, policy

    team.routes[f"PUT {path}"] = put
    code, _, err = run(team, "org", "docs", "share_links", "off", "--org", ORG)
    assert code == 0, err
    assert analysis_policy.load_cached(ORG)["share_links"] == "off"
    assert not analysis_policy.resolve_switch("store", {"store": "on"}, store=team.store)
    code, _, err = run(team, "org", "docs", "store", "enforced_on", "--org", ORG)
    assert code == 0, err
    assert policy["share_links"] == "off"
