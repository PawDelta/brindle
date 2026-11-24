"""Sharing a plan with one person, in every situation the two people and the
Library can be in (after the plan-sharing fix of 2026-11-24):

1. both people online;
2. the person invited has never opened brindle: the share lands in the
   Library and reaches them, through their Inbox, when they first do;
3. the sharer drops offline partway: nothing half-shared reads as reviewed,
   and Try again neither shares twice nor loses the sign-off;
4. a Library of another version: rows and answers with fields this brindle
   doesn't know, or without ones it expects.

The Library is a fake on the transport (no network): the per-person routes of
test_share_panel_access, plus the per-person Inbox feed the real server fills
when a share reaches someone (``notify_shared``).
"""

from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from brindle import inbox_team
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, review_queue
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_share_panel_access import PeopleLibrary, as_, person
from test_shared_plan_docs import DESIGN, DOC, parts, sent  # noqa: F401

ANN = {"kind": "user", "id": "ann"}
ANN_SIGNS = {"sub": "ann", "access": "comment", "sign_off": True}


class CompatLibrary(PeopleLibrary):
    """The per-person Library, which can drop off the network around a share
    (``cut``: before it commits, or after, losing only the answer) and answer
    a share the way another version does (``share_answer``)."""

    def __init__(self, remote):
        super().__init__(remote)
        self.inbox: dict[str, list[dict]] = {}
        self.cut = self.share_answer = None
        remote.routes["GET /me/inbox"] = lambda f, h: (200, {"items": list(self.inbox.get(
            h.get("Authorization", "").removeprefix("Bearer "), []))})

    def share(self, form, headers):
        if self.cut == "before":
            raise auth.TransportError("cannot reach brindle Pro backend: network is unreachable")
        scope = form["scope"]
        new = scope["kind"] == "user" and scope not in self.scopes
        status, answer = super().share(form, headers)
        if new and status == 200:          # as the server: the person's Inbox hears once, whether or not they're online
            self.inbox.setdefault(scope["id"], []).append({
                "kind": "shared_with_you", "item_key": f"shared:{DOC}:{scope['id']}", "doc_id": DOC,
                "title": self.title, "sender": {"sub": self.author}})
        if self.cut == "after":
            raise auth.TransportError("cannot reach brindle Pro backend: timed out")
        if self.share_answer == "older":
            answer = {k: v for k, v in answer.items() if k != "design"}
        elif self.share_answer == "newer":
            answer = {**answer, "notified": [scope["id"]],
                      "design": {**answer["design"], "access_levels": {"ann": "comment"}, "can_edit": True,
                                 "access_request": None, "reviewers": [{**r, "added_by": self.author}
                                                                       for r in answer["design"]["reviewers"]]}}
        return status, answer


@pytest.fixture
def compat(parts, backend, monkeypatch):
    pid, _, net = parts
    library = CompatLibrary(backend)
    monkeypatch.setattr(ad, "_session", lambda *a: (net["client"], net["store"]))
    entitlement = ad.require_entitlement(net["store"])
    monkeypatch.setattr(ad, "require_entitlement", lambda store: replace(entitlement, sub=library.actor))
    inbox_team.clear()
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        yield pid, library, app, net
    inbox_team.clear()


def signers(plan):
    return [r["sub"] for r in plan["gate"]["reviewers"] if r.get("sub")]


@pytest.mark.parametrize("invitee", [
    pytest.param("never_opened", marks=pytest.mark.variant("an invitee with no brindle run yet: the Library holds it")),
    pytest.param("online", marks=pytest.mark.variant("an Inbox already read must still take the new share")),
])
def test_the_share_reaches_the_invitee_online_or_the_first_time_they_open_brindle(db, compat, monkeypatch, invitee):
    pid, library, app, net = compat
    # Ann's brindle, signed in as ann: no device of hers is ever told; it asks the Library when it runs.
    monkeypatch.setattr(review_queue, "account", lambda *a: (net["client"], "ann", "ann"))
    if invitee == "online":
        inbox_team.poll(db)
        assert inbox_team.items("ann") == [] and as_(library, app, "ann") is None
    added = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert added.status_code == 200, added.json()
    assert ANN in library.scopes and library.reviewers == [{"sub": "ann", "required": True}]   # it is in the Library
    assert signers(added.json()) == ["ann"]

    inbox_team.poll(db)                     # ann's brindle runs: now, or for the first time
    shared, = inbox_team.items("ann")
    assert (shared.kind, shared.doc, shared.from_person.sub) == ("shared_with_you", DOC, library.author)
    opened = as_(library, app, "ann")
    assert opened is not None and opened["can_comment"] and opened["can_sign_off"]


@pytest.mark.parametrize("cut", [
    pytest.param("before", marks=pytest.mark.variant("the sharer went offline after the upload, before the share")),
    pytest.param("after", marks=pytest.mark.variant("the share committed and only its answer was lost")),
])
def test_a_share_cut_off_midway_is_never_half_done_and_try_again_lands_once(compat, backend, cut):
    pid, library, app, _ = compat
    library.cut = cut
    first = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert first.status_code == 502, first.json()
    gate = app.get(f"/v1/plans/{pid}").json()["gate"]
    assert gate is not None and gate["state"] == "waiting"      # never reads as reviewed without the Library's word

    library.cut = None
    again = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert again.status_code == 200, again.json()
    assert len(sent(backend, "POST /analysis-docs")) == 1                # the one doc
    assert library.scopes.count(ANN) == 1 and len(library.inbox["ann"]) == 1   # shared once, told once
    assert person(app, pid, "ann") == {"sub": "ann", "name": None, "access": "comment", "sign_off": "required"}
    assert signers(again.json()) == ["ann"] and again.json()["gate"]["state"] == "waiting"


@pytest.mark.parametrize("answer", [
    pytest.param("older", marks=pytest.mark.variant("a share answer without its design view: fails on base")),
    pytest.param("newer", marks=pytest.mark.variant("fields a newer Library adds to the share answer are ignored")),
])
def test_a_share_answer_from_another_library_version_still_shows_the_sign_off(compat, answer):
    pid, library, app, _ = compat
    library.share_answer = answer
    added = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert added.status_code == 200, added.json()
    assert signers(added.json()) == ["ann"]          # the build waits for ann from the moment she is added
    assert added.json()["gate"]["state"] == "waiting"


ROW = {"id": "ann", "display_name": "Ann Lee", "email": "ann@example.test", "role": "member",
       "teams": [{"id": "t_1", "name": "Core"}], "on_plan": True, "on_plan_role": "commenter"}


@pytest.mark.parametrize(("library_rows", "panel_rows"), [
    # An older Library: no role, teams or plan role; no display name.
    pytest.param({"people": [{"id": "bo", "email": "bo@example.test"}]},
                 [{"id": "bo", "display_name": "bo@example.test", "email": "bo@example.test", "teams": [], "role": "",
                   "on_plan": False, "on_plan_role": None}],
                 marks=pytest.mark.variant("missing fields: a 400 on base, apart from the other rows' causes")),
    # A newer one: fields this brindle doesn't know, and a plan role it can't name.
    pytest.param({"people": [{**ROW, "on_plan_role": "approver", "avatar": "a.png",
                              "teams": [{"id": "t_1", "name": "Core", "color": "teal"}]}], "next_cursor": "c_2"},
                 [{**ROW, "on_plan_role": None}],
                 marks=pytest.mark.variant("an unknown plan role: a 400 on base, apart from the other rows' causes")),
    # A row nobody can be added by (no account id) beside one that can.
    ({"people": [{"display_name": "Invited by email", "email": "new@example.test"}, ROW]}, [ROW]),
])
def test_people_search_reads_rows_from_another_library_version(compat, backend, library_rows, panel_rows):
    pid, _, app, _ = compat
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "comment"}).status_code == 200
    backend.routes[f"GET {DESIGN}/people"] = lambda f, h: (200, library_rows)
    got = app.get(f"/v1/plans/{pid}/people")
    assert got.status_code == 200, got.json()
    assert got.json()["people"] == panel_rows
