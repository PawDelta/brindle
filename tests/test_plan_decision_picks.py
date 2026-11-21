"""The person's picks on a shared plan's decisions are saved as they are made
(POST /v1/plans/{id}/confirm, brindle.plan_docs.confirm) and reach everyone on
the plan through the Library. That a pick survives a reopen and seeds the
verdict is held by tests/test_plan_docs.py's design-doc test; the reader's side
(it saves each pick, restores it, counts it) by the app's PlanReader tests."""

import pytest
from starlette.testclient import TestClient

from brindle.daemon.app import build_app
from brindle.pro import shared_docs as sd
from brindle.pro import shared_reader
from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session, work  # noqa: F401 - fixture
from test_shared_plan_docs import DOC, TEAM, parts  # noqa: F401 - fixture


@pytest.mark.variant("the whole way from the author's Confirm to a teammate's reader: each half is tested alone, "
                     "the chain (the author's sub named, the teammate's own pick kept apart) only here")
def test_a_shared_plans_pick_reaches_the_teammate_reader(db, parts):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.votes = {"d1": {"org": ["u_c"]}}
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        client.post(f"/v1/plans/{pid}/confirm", json={"rev": 1, "decision": "d1", "option": "repo"})
    sd.sync(db, pid, **net)

    teammate = shared_reader._dto(DOC, lib.view(), "u_c")
    assert teammate["decisions"][0]["votes"] == {"org": ["person"], "repo": ["user_1"]}
    assert teammate["decisions"][0]["confirmed"] == "org"
