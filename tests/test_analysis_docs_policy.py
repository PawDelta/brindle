"""Enterprise and group analysis-document controls."""
import json

import pytest

from brindle.pro import analysis_docs as ad
from brindle.pro import analysis_policy as ap
from brindle.pro._files import private_dir, write_private
from pro_fixtures import backend, claims, fixed_identity, pro_env, signing_key  # noqa: F401
from test_analysis_docs import SOURCE, remote, uploader, write_doc  # noqa: F401
from test_pro_remote_learning import login_as
from test_pro_team import ORG, login, run, team, team_claims  # noqa: F401


@pytest.mark.parametrize("enterprise,group_state,user,group,name,expected", [
    ("enforced_off", "enforced_on", "on", "g", "store", False),
    ("default_on", "enforced_on", "off", "g", "store", True),
    ("default_on", "default_on", "off", "g", "store", False),
    ("off", "default_on", None, "g", "store", True),
    ("off", None, None, "g", "store", False),
    (None, None, None, "g", "store", True),
    (None, None, None, "g", "prompt_analysis", False),
    pytest.param("off", "enforced_on", None, None, "store", False,
                 marks=pytest.mark.variant("missing group must not select an existing group")),
    pytest.param("off", "enforced_on", None, "unknown", "store", False,
                 marks=pytest.mark.variant("unknown group must not select an existing group")),
])
def test_precedence(enterprise, group_state, user, group, name, expected):
    policy = {"switches": {name: enterprise}, "groups": {"g": {"switches": {name: group_state}}}}
    assert ap.resolve_switch(name, {name: user}, group=group, policy=policy,
                             org=ORG, plan="pro") is expected


def test_enforced_off_keeps_upload_local(tmp_path, remote, fixed_identity):
    folder = write_doc(tmp_path)
    up, store = uploader(tmp_path, remote, [])
    login_as(remote, store, features=("analysis_docs",))
    from brindle.pro import license

    org = license.current(refresh=False, store=store).org_id
    write_private(private_dir() / f"analysis-policy-{org}.json",
                  json.dumps({"switches": {"store": "enforced_off"}, "groups": {}}).encode())
    remote.switches = {"store": "on"}
    up.submit(ad.collect(str(tmp_path), "feat", SOURCE))
    up.flush()
    assert remote.docs == [] and (folder / "summary.md").exists()


def test_org_docs_shows_caches_and_updates_policy(team, monkeypatch):
    path = f"/orgs/{ORG}/analysis-docs/policy"
    policy = {"switches": {"store": "enforced_off", "assist": "default_on"},
              "groups": {"g": {"switches": {"share": "off"}}}}
    team.routes[f"GET {path}"] = lambda f, h: (200, policy)
    team.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": {"store": "on"}})

    def put(form, headers):
        policy.clear()
        policy.update(form)
        return 200, policy

    team.routes[f"PUT {path}"] = put
    code, out, err = run(team, "org", "docs")
    assert code == 0 and not err and "store: off" in out and "assist: on" in out
    assert not ap.resolve_switch("store", {"store": "on"}, store=team.store)
    code, _, _ = run(team, "org", "docs", "import_upload", "default_on", "--org", ORG)
    assert code == 0 and ap.load_cached(ORG)["switches"]["import_upload"] == "default_on"
    code, out, _ = run(team, "org", "docs", "share", "enforced_on", "--group", "g", "--org", ORG)
    assert code == 0 and "share: on" in out
    assert policy == {"switches": {"store": "enforced_off", "assist": "default_on",
                                   "import_upload": "default_on"},
                      "groups": {"g": {"switches": {"share": "enforced_on"}}}}

    from typer.testing import CliRunner

    from brindle.cli import app
    from brindle.pro import auth

    put = []                                        # a person's own switches: brindle docs on|off --switch
    team.routes["PUT /me/analysis-docs/settings"] = lambda f, h: put.append(f) or (200, f)
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, m, url, form, headers:
                        team.request(m, url, form, headers))
    team.routes["GET /analysis-docs/scopes"] = lambda f, h: (200, {"scopes": [
        {"kind": "user", "id": "user_1"}, {"kind": "group", "id": "g"}, {"kind": "group", "id": "elsewhere"}]})
    login(team, team_claims(features=["analysis_docs", "analysis_docs_team", "analysis_docs_enterprise"]))
    done = CliRunner().invoke(app, ["docs", "off", "--switch", "assist"])
    assert done.exit_code == 0 and put == [{"switches": {"store": "on", "assist": "off"}}], done.output
    for args, why in ((["docs", "on"], "store off"),                    # the org enforces store off
                      (["docs", "off", "--switch", "share"], "share on")):   # and the person's group share on
        refused = CliRunner().invoke(app, args)
        assert refused.exit_code == 1 and f"organisation has {why}" in refused.output and len(put) == 1
    login(team, claims(org_id="org_personal", plan="free", features=[]))
    policy["switches"].pop("store")
    team.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": {}})
    code, out, err = run(team, "org", "docs", "--org", ORG)
    assert code == 0 and not err and "store: on" in out


def test_non_admin_put_refusal_is_shown(team):
    path = f"/orgs/{ORG}/analysis-docs/policy"
    team.routes[f"GET {path}"] = [(200, {"switches": {}, "groups": {}})]
    team.routes[f"PUT {path}"] = [(403, {"error": "forbidden"})]
    code, _, err = run(team, "org", "docs", "store", "enforced_off", "--org", ORG)
    assert code == 1 and "forbidden" in err
    assert ap.load_cached(ORG) == {"switches": {}, "groups": {}}
