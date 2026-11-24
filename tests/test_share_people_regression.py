"""Regressions from 2026-11-24: a teammate sharing a plan saw "Couldn't search
people", and adding the owner failed with a timeout.

- The Library's people search with ``include_unshared`` needs a Members-page
  permission an ordinary member doesn't have (403 forbidden), though the org's
  member list it would answer with is open to every member.
- A first add whose upload timed out leaves the plan claimed but not linked: it
  reads as shared, so the panel searched a Library doc the daemon didn't know
  (409 not_in_library), and Try again was refused while the claim stood.
"""

import threading
from types import SimpleNamespace

import pytest

from brindle import db as db_mod
from brindle import spawn
from brindle.pro import auth
from brindle.pro import share_access as sa
from brindle.pro import shared_docs as sd
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import session  # noqa: F401
from test_share_panel_access import PeopleLibrary, panel  # noqa: F401
from test_shared_plan_docs import BASE, DESIGN, DOC, parts, sent  # noqa: F401

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


def invite(db, parts, who):
    pid, _, net = parts
    return sa.plan_write(db, pid, lambda call: sa.invite(call, who, "comment", False), first=True, **net)


def held_create(parts, backend, meanwhile):
    """Ann's invite, whose create has committed in the Library and whose answer
    is held while ``meanwhile`` runs; then the errors it ended with."""
    lib = parts[1]
    created, release, failed = threading.Event(), threading.Event(), []
    commit = lib.create

    def held(form, headers):
        answer = commit(form, headers)
        created.set()
        release.wait()
        return answer

    backend.routes["POST /analysis-docs"] = held

    def first():
        own = db_mod.DB()
        try:
            invite(own, parts, "ann")
        except Exception as e:  # noqa: BLE001 - returned to the test
            failed.append(e)
        finally:
            own.conn.close()
            created.set()

    out = threading.Thread(target=first)
    out.start()
    try:
        created.wait()
        meanwhile()
    finally:
        release.set()
        out.join()
    return failed


def as_another_brindle(db, parts):
    """Bo's invite from a brindle with no create of its own out (another process)."""
    getattr(sd, "_creating", set()).discard(parts[0])     # the first create is parked in release.wait()
    invite(db, parts, "bo")


def test_a_second_invite_never_overlaps_a_create_still_out(db, parts, backend):
    pid = parts[0]

    def meanwhile():
        # The claim has aged out, but this process's create is still out: a second invite waits for its
        # answer, and its refusal leaves the claim as it was.
        db.conn.execute("UPDATE plan_shares SET claimed_at=claimed_at-? WHERE plan_id=?", (sd.CLAIM_FOR + 1, pid))
        claimed = sd.link(db, pid)["claimed_at"]
        with pytest.raises(RuntimeError, match="already being shared"):
            invite(db, parts, "bo")
        assert sd.link(db, pid)["claimed_at"] == claimed
        # Another brindle finds the committed doc and links it; a sync moves it on.
        as_another_brindle(db, parts)
        db.conn.execute("UPDATE plan_shares SET remote_rev=2 WHERE plan_id=?", (pid,))

    assert held_create(parts, backend, meanwhile) == []
    share = sd.link(db, pid)
    assert (share["doc_id"], share["remote_rev"]) == (DOC, 2)       # the late answer kept the link and the sync
    assert len(sent(backend, "POST /analysis-docs")) == 1


def test_a_late_create_answer_grants_nothing_once_its_claim_is_gone(db, parts, backend):
    pid = parts[0]

    def meanwhile():
        as_another_brindle(db, parts)
        db.conn.execute("DELETE FROM plan_shares WHERE plan_id=?", (pid,))   # e.g. the doc was deleted since

    failed = held_create(parts, backend, meanwhile)
    assert [type(e) for e in failed] == [RuntimeError]
    assert [f["scope"]["id"] for f in sent(backend, f"POST {BASE}/share")] == ["bo"]     # never ann


def test_an_air_gapped_search_reads_no_credential(parts, monkeypatch):
    """An unlinked plan's search in an air-gapped repo: refused before the sign-in is read."""
    from starlette.testclient import TestClient

    from brindle import airgap
    from brindle.daemon.app import build_app
    from brindle.pro import analysis_docs as ad
    from test_plans import AUTH, TOKEN

    def opened(*a):
        raise AssertionError("the credential store was opened")

    monkeypatch.setattr(airgap, "enabled", lambda: True)
    monkeypatch.setattr(ad, "_session", opened)
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        got = app.get(f"/v1/plans/{parts[0]}/people?{SEARCH}")
    assert got.status_code == 402, got.json()


@pytest.mark.variant("the root cause: a refresh-lock holder inside background work must not queue for CPU room")
def test_the_refresh_lock_holder_never_waits_for_room(tmp_path, monkeypatch):
    """Background work on a busy machine holds the sign-in's refresh lock while its keychain read
    queues for room: every other brindle's request then waits on the lock until lock_timeout."""
    class NoRoom(threading.Condition):
        def wait(self, *a):
            raise AssertionError("a read waited for room while holding the refresh lock")

    started = []
    monkeypatch.setattr(auth, "lock_path", lambda: tmp_path / "refresh.lock")
    monkeypatch.setattr(spawn, "_gated", True)
    monkeypatch.setattr(spawn, "_running", 1)
    monkeypatch.setattr(spawn, "room", lambda: 0.0)          # the machine is busy: nothing that waits may start
    monkeypatch.setattr(spawn, "_cond", NoRoom())
    monkeypatch.setattr(spawn, "_run_read", lambda argv, *a: started.append(spawn._background(argv)) or SimpleNamespace(returncode=0))
    with spawn.waits_for_room(), auth.refresh_lock():
        spawn.read(["security", "find-generic-password"])
    assert started == [["security", "find-generic-password"]]   # at once, and at normal priority
