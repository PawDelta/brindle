"""Sharing a plan with one person, in every situation the two people and the
Library can be in (after the plan-sharing fix of 2026-11-24):

1. both people online;
2. the person invited has never opened brindle: the share lands in the
   Library and reaches them, through their Inbox, when they first sign in;
3. the sharer drops offline partway: nothing half-shared reads as reviewed,
   and Try again neither shares twice nor loses the sign-off;
4. another version on the other side: a request body an older client sent,
   and Library answers with fields this brindle doesn't know or without ones
   it expects.

The Library is a fake on the transport (no network), modelled on the current
server (pawdelta-web backend/pro_api/analysis_designs.py, docs/shared-designs-api.md):

- ``share_to``: a person's share adds their user scope once; sharing again with
  someone who has it changes only their grant, in the same transaction;
- ``notify_shared``: everyone a share newly lets read the design gets a
  ``shared_with_you`` row in their account's Inbox feed (``GET /me/inbox``),
  written on the server whether or not any brindle of theirs is running. brindle
  registers no device: the feed is the account's, read by whichever signed-in
  brindle asks;
- ``_set_person_grant``: ``PUT /design/permissions/{sub}`` takes ``{access?,
  sign_off?}``, keeping what isn't sent, or a legacy ``{role, required}``, which
  replaces both (a ``reviewer`` at comment); mixing the two shapes, or sending
  either new field as null, is refused.
"""

from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from brindle import inbox_team
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from pro_fixtures import backend, claims, pro_env, sign, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_share_panel_access import PeopleLibrary, as_, person
from test_shared_plan_docs import DESIGN, DOC, TEAM_LOGIN, parts, sent  # noqa: F401

ANN = {"kind": "user", "id": "ann"}
ANN_SIGNS = {"sub": "ann", "access": "comment", "sign_off": True}
OLD_BODY, NEW_BODY = {"role", "required"}, {"access", "sign_off"}


class CompatLibrary(PeopleLibrary):
    """The per-person Library, which can drop off the network around a share
    (``cut``: before it commits, or after, losing only the answer), answer a
    share or unshare as another version does (``answers``), and fail a read of
    the design once unsharing has begun while people still have it (``reads_fail``).
    ``older``: ``PUT /permissions/{sub}`` as a Library before #328 takes it."""

    def __init__(self, remote):
        super().__init__(remote)
        self.inbox: dict[str, list[dict]] = {}
        self.accounts: dict[str, str] = {}          # access token -> the account it signs in
        self.cut = self.answers = None
        self.reads_fail = self.unsharing = self.older = False
        remote.routes["GET /me/inbox"] = lambda f, h: (200, {"items": list(self.inbox.get(
            self.accounts.get(h.get("Authorization", "").removeprefix("Bearer ")), []))})

    def people_with_it(self):
        return {s["id"] for s in self.scopes if s["kind"] == "user"}

    def share(self, form, headers):
        if self.cut == "before":
            raise auth.TransportError("cannot reach brindle Pro backend: network is unreachable")
        before = self.people_with_it()
        status, answer = super().share(form, headers)
        if status == 200:
            for sub in sorted(self.people_with_it() - before - {self.author}):
                self.inbox.setdefault(sub, []).append({
                    "kind": "shared_with_you", "item_key": f"shared:{DOC}:{sub}", "doc_id": DOC,
                    "title": self.title, "sender": {"sub": self.author}})
        if self.cut == "after":
            raise auth.TransportError("cannot reach brindle Pro backend: timed out")
        return status, self.version(answer)

    def revoke(self, sub):
        status, answer = super().revoke(sub)
        self.unsharing = True
        return status, self.version(answer)

    def version(self, answer):
        if self.answers == "older":
            return {k: v for k, v in answer.items() if k != "design"}
        if self.answers == "newer":
            design = answer["design"]
            return {**answer, "notified": [], "design": {
                **design, "access_levels": {}, "can_edit": True, "access_request": None,
                "reviewers": [{**r, "added_by": self.author} for r in design["reviewers"]]}}
        return answer

    def read(self):
        if self.reads_fail and self.unsharing and self.people_with_it() - {self.author}:
            return 503, {"error": "unavailable"}
        return super().read()

    def role(self, sub, form):
        new, old = NEW_BODY & form.keys(), OLD_BODY & form.keys()
        if self.older:        # pawdelta-web 85bd1cb9: PersonGrant is {role, required}, extra fields refused
            if new:
                return 422, {"error": "invalid_request", "fields": ["<unknown>"]}
            role = form.get("role", "view")
            self.permissions.pop(sub, None)
            if role != "view":
                self.permissions[sub] = {"can_edit": role == "edit", "can_suggest": role in ("edit", "reviewer"),
                                         "can_comment": True}
            self.reviewers = [r for r in self.reviewers if r["sub"] != sub] + (
                [{"sub": sub, "required": form.get("required", True)}] if role == "reviewer" else [])
            return 200, self.view()
        if (new and old) or any(form[k] is None for k in new):
            return 422, {"error": "invalid_request"}
        if not new:
            role = form.get("role", "view")
            form = {"access": "comment" if role == "reviewer" else role,
                    "sign_off": ("required" if form.get("required", True) else "optional") if role == "reviewer"
                    else "none"}
        return super().role(sub, form)


@pytest.fixture
def compat(parts, backend, monkeypatch):
    pid, _, net = parts
    library = CompatLibrary(backend)
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (client or net["client"], store or net["store"]))
    entitlement = ad.require_entitlement(net["store"])
    monkeypatch.setattr(ad, "require_entitlement", lambda store: replace(entitlement, sub=library.actor))
    inbox_team.clear()
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        yield pid, library, app, net
    inbox_team.clear()


def signers(plan):
    return [r["sub"] for r in plan["gate"]["reviewers"] if r.get("sub")]


def signs_in(tmp_path, backend, library, net, sub):
    """``sub`` signs in to brindle on their own machine: their own credential store."""
    store = credentials.FileStore(tmp_path / sub)
    store.save({**net["store"].load(), "access_token": f"{sub}_access", "refresh_token": f"{sub}_refresh",
                "entitlement": sign(backend.key, claims(sub=sub, features=list(TEAM_LOGIN)))})
    library.accounts[f"{sub}_access"] = sub
    return store


@pytest.mark.parametrize("invitee", [
    pytest.param("never_opened", marks=pytest.mark.variant("no brindle of ann's at share time: her first sign-in finds it")),
    pytest.param("online", marks=pytest.mark.variant("an Inbox already read must still take the new share")),
])
def test_the_share_reaches_the_invitee_online_or_the_first_time_they_sign_in(db, compat, backend, tmp_path, invitee):
    pid, library, app, net = compat
    ann = None
    if invitee == "online":
        ann = signs_in(tmp_path, backend, library, net, "ann")
        inbox_team.poll(db, client=net["client"], store=ann)
        assert inbox_team.items("ann") == [] and as_(library, app, "ann") is None
    else:
        assert not (tmp_path / "ann").exists()          # ann has never signed in to brindle anywhere

    added = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert added.status_code == 200, added.json()
    assert ANN in library.scopes and library.reviewers == [{"sub": "ann", "required": True}]   # it is in the Library
    assert [i["doc_id"] for i in library.inbox["ann"]] == [DOC]                                # waiting on her account
    assert signers(added.json()) == ["ann"]

    ann = ann or signs_in(tmp_path, backend, library, net, "ann")
    inbox_team.poll(db, client=net["client"], store=ann)          # her brindle's sync, signed in as her
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
    seen = as_(library, app, "ann")
    if cut == "before":           # nothing of it landed: no access, no sign-off
        assert seen is None and library.reviewers == [] and "ann" not in library.permissions
    else:                         # all of it landed, in one transaction
        assert seen is not None and seen["can_comment"] and seen["can_sign_off"]
        assert library.reviewers == [{"sub": "ann", "required": True}]

    library.cut = None
    again = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert again.status_code == 200, again.json()
    assert len(sent(backend, "POST /analysis-docs")) == 1                # the one doc
    assert library.scopes.count(ANN) == 1 and len(library.inbox["ann"]) == 1   # shared once, told once
    assert person(app, pid, "ann") == {"sub": "ann", "name": None, "access": "comment", "sign_off": "required"}
    assert signers(again.json()) == ["ann"] and again.json()["gate"]["state"] == "waiting"


@pytest.mark.parametrize("answers", [
    pytest.param("older", marks=pytest.mark.variant("a share answer without its design view: fails on base")),
    pytest.param("newer", marks=pytest.mark.variant("fields a newer Library adds to the share answer are ignored")),
])
def test_a_share_answer_from_another_library_version_still_shows_the_sign_off(compat, answers):
    pid, library, app, _ = compat
    library.answers = answers
    added = app.post(f"/v1/plans/{pid}/people", json=ANN_SIGNS)
    assert added.status_code == 200, added.json()
    assert signers(added.json()) == ["ann"]          # the build waits for ann from the moment she is added
    assert added.json()["gate"]["state"] == "waiting"


@pytest.mark.parametrize("who", [
    pytest.param("one", marks=pytest.mark.variant("an unshare answer without its design view: fails on base")),
    pytest.param("everyone", marks=pytest.mark.variant("a read failing mid-way must not stop Stop sharing")),
])
def test_an_unshare_answered_without_the_design_view(compat, who):
    pid, library, app, _ = compat
    for sub in ("ann", "bo"):
        assert app.post(f"/v1/plans/{pid}/people", json={**ANN_SIGNS, "sub": sub}).status_code == 200
    library.answers = "older"
    if who == "one":
        removed = app.delete(f"/v1/plans/{pid}/people/ann")
        assert removed.status_code == 200, removed.json()
        assert signers(removed.json()) == ["bo"]              # ann's sign-off left the gate with her
        return
    library.reads_fail = True             # the design can't be read while anyone still has it
    stopped = app.delete(f"/v1/plans/{pid}/access")
    assert stopped.status_code == 200, stopped.json()
    assert library.scopes == [{"kind": "user", "id": library.author}] and library.reviewers == []


@pytest.mark.parametrize(("target", "older", "access"), [
    pytest.param("plan", False, "edit", marks=pytest.mark.variant("the plan's route sent the legacy body: demoted")),
    pytest.param("library", False, "edit", marks=pytest.mark.variant("the Library reader's route did too")),
    pytest.param("plan", True, "edit", marks=pytest.mark.variant("an older Library refuses {sign_off}: plan route")),
    pytest.param("library", True, "edit", marks=pytest.mark.variant("an older Library refuses it: reader route")),
    pytest.param("plan", True, "comment", marks=pytest.mark.variant("an older Library's signer still gets comment")),
])
def test_asking_someone_to_sign_off_keeps_their_access_on_either_library(compat, target, older, access):
    """The legacy ``{role: "reviewer"}`` body makes the person a commenter, on
    the current Library and on one before #328, which also refuses the new
    ``{sign_off}`` (422): neither may cost an editor their edit."""
    pid, library, app, _ = compat
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": access}).status_code == 200
    library.older = older
    base = f"/v1/plans/{pid}" if target == "plan" else f"/v1/library/designs/{DOC}"
    asked = app.put(f"{base}/reviewers/ann", json={"required": True})
    assert asked.status_code == 200, asked.json()
    assert person(app, pid, "ann") == {"sub": "ann", "name": None, "access": access, "sign_off": "required"}
    seen = as_(library, app, "ann")
    assert seen["can_edit"] is (access == "edit") and seen["can_comment"] and seen["can_sign_off"]


@pytest.mark.variant("a pinned older client's request and reader against the current Library's wire shapes")
def test_an_older_client_still_gets_access_and_sign_off_from_the_current_library(compat):
    """brindle before this branch (shared_docs.set_reviewer at 851fb558): its
    request, pinned here, against the current Library's per-person route, and
    its reader (share_access.summary) of the answer."""
    from brindle.pro import share_access

    pid, library, app, net = compat
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "ann", "access": "view"}).status_code == 200
    status, view = net["client"].call("PUT", f"{DESIGN}/permissions/ann", {"role": "reviewer", "required": True},
                                      token=net["store"].load()["access_token"])
    assert status == 200, view
    ann, = share_access.summary({"scope": library.scopes[0], "scopes": library.scopes}, view)["people"]
    assert ann == {"sub": "ann", "name": None, "access": "comment", "sign_off": "required"}


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
