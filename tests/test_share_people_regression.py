"""Regressions from 2026-11-24: a teammate sharing a plan saw "Couldn't search
people", and adding the owner failed with a timeout.

- The Library's people search with ``include_unshared`` needs a Members-page
  permission an ordinary member doesn't have (403 forbidden), though the org's
  member list it would answer with is open to every member.
- A first add whose upload timed out leaves the plan claimed but not linked: it
  reads as shared, so the panel searched a Library doc the daemon didn't know
  (409 not_in_library), and Try again was refused while the claim stood.
"""

from brindle.pro import auth
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import session  # noqa: F401
from test_share_panel_access import PeopleLibrary, panel  # noqa: F401
from test_shared_plan_docs import BASE, DESIGN, parts, sent  # noqa: F401

OWNER = {"sub": "owner_1", "email": "owner@example.test", "role": "owner"}
MEMBERS = [{"sub": "user_1", "email": "me@example.test", "role": "member"}, OWNER,
           {"sub": "ann", "email": "ann@example.test", "role": "member"}]
SEARCH = "q=own&include_unshared=true"


class SlowLibrary(PeopleLibrary):
    """Its create can commit and then answer too late: the client times out."""

    def __init__(self, remote):
        super().__init__(remote)
        self.slow_create = False
        remote.routes["GET /orgs/org_1/members"] = lambda f, h: (200, {"org_id": "org_1", "members": MEMBERS})
        # An ordinary member may not list people the plan isn't shared with through the doc.
        remote.routes[f"GET {DESIGN}/people?{SEARCH}"] = lambda f, h: (403, {"error": "forbidden"})

    def create(self, form, headers):
        answer = super().create(form, headers)
        if self.slow_create:
            raise auth.TransportError("cannot reach brindle Pro backend: timed out")
        return answer


def found(app, pid):
    got = app.get(f"/v1/plans/{pid}/people?{SEARCH}")
    assert got.status_code == 200, got.json()
    return [p["id"] for p in got.json()["people"]]


def test_a_member_without_the_members_permission_can_still_find_the_owner(panel, backend):
    pid, _, app = panel
    SlowLibrary(backend)
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "comment"}).status_code == 200
    assert found(app, pid) == ["owner_1"]                 # the org's own list, never the person searching


def test_a_timed_out_first_add_still_searches_and_try_again_lands_on_the_one_doc(panel, backend):
    pid, library, app = panel
    library = SlowLibrary(backend)
    library.slow_create = True
    first = app.post(f"/v1/plans/{pid}/people", json={"sub": "owner_1", "access": "comment"})
    assert first.status_code == 502 and "timed out" in first.json()["error"]["message"]
    library.slow_create = False
    assert app.get(f"/v1/plans/{pid}").json()["gate"] is not None    # the panel now reads it as shared
    assert found(app, pid) == ["owner_1"]

    # Try again, straight away: the doc the timed-out create made is found, not refused and not made twice.
    again = app.post(f"/v1/plans/{pid}/people", json={"sub": "owner_1", "access": "comment"})
    assert again.status_code == 200, again.json()
    assert len(sent(backend, "POST /analysis-docs")) == 1
    assert sent(backend, f"POST {BASE}/share") == [{"scope": {"kind": "user", "id": "owner_1"}, "access": "comment",
                                                    "sign_off": "none"}]
