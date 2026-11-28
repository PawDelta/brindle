"""The Library's refusals on a shared plan: a 404 on something under the plan
(a person already gone) never reads as the plan being gone, and a background
invite that can never land is shown as failed at once instead of retried."""

import pytest

from brindle import plans
from brindle.pro import share_invites
from brindle.pro import shared_docs as sd
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_share_panel_access import add_person, panel  # noqa: F401
from test_shared_plan_docs import BASE, DOC, parts  # noqa: F401


def _removed_someone_gone(db, pid, backend, app):
    """bo never had it: the Library's 404 is about bo, not about the plan,
    so no access refusal is kept. Returns the refusal the plan should hold."""
    backend.routes[f"DELETE {BASE}/share/user/bo"] = lambda f, h: (404, {"error": "not_found"})
    app.delete(f"/v1/plans/{pid}/people/bo")
    return None


def _kept_by_an_older_client(db, pid, backend, app):
    """An older brindle kept "not_found" for any 404, a removed person's included."""
    with db.tx() as c:
        c.execute("UPDATE plan_shares SET access_refused='not_found' WHERE plan_id=?", (pid,))
    return "not_found"


def _entry_read_404(db, pid, backend, app):
    """Only the doc's entry answers 404: kept as a refusal of access, never as its design being gone."""
    backend.routes[f"GET {BASE}"] = lambda f, h: (404, {"error": "not_found"})
    app.get(f"/v1/plans/{pid}/access")
    return "not_found"


@pytest.mark.parametrize("arrive", [
    pytest.param(_removed_someone_gone,
                 marks=pytest.mark.variant("Removing an absent person must leave the plan's access refusal unset")),
    pytest.param(_kept_by_an_older_client,
                 marks=pytest.mark.variant("A refusal persisted by an older client must keep the stop-sharing gate closed")),
    _entry_read_404,
])
def test_a_404_not_on_the_design_never_opens_stop_sharing(panel, backend, db, monkeypatch, arrive):
    pid, library, app = panel
    monkeypatch.setattr(sd, "account", lambda store=None: library.author)    # the owner asks to stop
    assert add_person(app, pid, {"sub": "ann", "access": "comment", "sign_off": True}).status_code == 202
    before = sd.link(db, pid)
    assert before["access_refused"] is None and plans.demands_review(before["review"])
    kept = arrive(db, pid, backend, app)

    after = sd.link(db, pid)
    assert (after["access_refused"], after["review"]) == (kept, before["review"])
    stopped = app.post(f"/v1/plans/{pid}/stop-sharing", json={"rev": plans.get(db, pid)["rev"]})
    assert stopped.status_code == 409 and stopped.json()["error"]["code"] == "stop_sharing_refused"
    assert sd.link(db, pid)["doc_id"] is not None          # still shared, ann still asked to sign


def _firewall(db, pid, backend, library):
    """The firewall in front of the Library refuses the upload (a 403 with no Library code)."""
    backend.routes["POST /analysis-docs"] = lambda f, h: (403, {})


def _other_org(db, pid, backend, library):
    """This account shared the plan from another org: nothing sent from this one can land there."""
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, owner, org_id, doc_id) VALUES (?,?,?,?)",
                  (pid, library.author, "o_elsewhere", DOC))


@pytest.mark.parametrize("refuse", [
    pytest.param(_firewall,
                 marks=pytest.mark.variant("A background invite must fail immediately on an uncoded HTTP 403")),
    _other_org,
])
def test_an_invite_that_can_never_land_fails_on_its_first_try(panel, backend, db, refuse):
    pid, library, app = panel
    refuse(db, pid, backend, library)
    assert add_person(app, pid, {"sub": "ann", "access": "comment"}).status_code == 202
    ann = share_invites.pending(db, pid)
    assert ann and ann[0]["failed"]                            # shown as failed, never due again
    assert library.permissions.get("ann") is None             # nothing reached a doc of another org
