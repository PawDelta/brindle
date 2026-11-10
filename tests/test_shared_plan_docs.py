"""A plan shared through the Library (brindle.pro.shared_docs), against a fake
of the API in pawdelta-web's docs/shared-designs-api.md."""

import asyncio
import copy
import time

import pytest
from starlette.testclient import TestClient

from brindle import plan_docs, plans, visual_offers
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_plans import AUTH, DECISIONS, TOKEN, messages, session  # noqa: F401 - fixture
from test_pro_remote_learning import login_as

DOC = "doc_1"
BASE = f"/analysis-docs/{DOC}"
DESIGN = f"{BASE}/design"
LIST = "/analysis-docs?scope_kind=user&scope_id=user_1&session=boss&design=true"
TEAM ={"kind": "group", "id": "t_1"}
TEAM_LOGIN = ("analysis_docs", "analysis_docs_team")
SUMMARY = "Workers run on a machine the customer controls."


class Library:
    """The Library's one design: the head, its review state, and a compare
    on the revision on screen, as the server does."""

    def __init__(self, remote):
        self.rev, self.content, self.threads, self.votes = 0, None, [], {}
        self.permissions, self.reviewers, self.create_status, self.share_status = {}, [], 200, 200
        self.lose_answer, self.lose_create, self.list_status, self.plan = False, False, 200, None
        self.sign_offs = []
        remote.routes.update({
            f"POST {DESIGN}/sign-offs": self.sign_off,
            "POST /analysis-docs": self.create, f"POST {BASE}/share": self.share, f"GET {LIST}": self.list,
            f"GET {DESIGN}": lambda f, h: (200, self.view()), f"POST {DESIGN}/revisions": self.revise,
            f"PUT {DESIGN}/permissions": self.set_permissions, f"PUT {DESIGN}/reviewers": self.set_reviewers,
            f"PUT {DESIGN}/folder": lambda f, h: (200, self.view()),
        })

    def view(self):
        out = {**copy.deepcopy(self.content), "rev": self.rev, "threads": copy.deepcopy(self.threads),
               "permissions": dict(self.permissions), "reviewers": list(self.reviewers),
               "sign_offs": list(self.sign_offs), "policy_reviewers": [],
               "pending_reviewers": [r["sub"] for r in self.reviewers if r["required"] and not any(
                   s["sub"] == r["sub"] and s["rev"] == self.rev for s in self.sign_offs)]}
        for d in out["decisions"]:
            d["votes"] = self.votes.get(d["id"], {})
        return {**out, "ready": not out["pending_reviewers"]}

    def create(self, form, headers):
        if self.create_status != 200:
            return self.create_status, {"error": "switch_off"}
        self.rev, self.content, self.plan = 1, copy.deepcopy(dict(form["design"])), form["source"]["ref"]
        if self.lose_create:
            raise auth.AuthError("offline", code="network")
        return 200, {"id": DOC, "version": 1}

    def list(self, form, headers):
        if self.list_status != 200:
            return self.list_status, {"error": "unavailable"}
        return 200, {"docs": [{"doc_id": DOC, "created_at": 1, "source": {"kind": "goal", "ref": self.plan}}]
                     if self.plan else []}

    def share(self, form, headers):
        return self.share_status, {"id": DOC} if self.share_status == 200 else {"error": "too_many_scopes"}

    def revise(self, form, headers):
        if form["rev"] != self.rev:
            return 409, {"error": "stale", "rev": self.rev}
        self.rev, self.content = self.rev + 1, {k: copy.deepcopy(v) for k, v in form.items() if k != "rev"}
        self.votes = {}                                    # votes belong to the revision they were cast on
        if self.lose_answer:
            raise auth.AuthError("offline", code="network")
        return 200, self.view()

    def set_permissions(self, form, headers):
        self.permissions = dict(form["permissions"])
        return 200, self.view()

    def set_reviewers(self, form, headers):
        self.reviewers = list(form["reviewers"])
        return 200, self.view()

    def sign_off(self, form, headers):
        self.sign_offs.append({"rev": form["rev"], "sub": "user_1", "at": 9.0})
        return 200, self.view()


@pytest.fixture
def parts(db, session, tmp_path, backend):
    """A Team login, the fake Library and an open plan."""
    boss, _ = session
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=TEAM_LOGIN)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Cloud workers", DECISIONS,
                        sections=[{"id": "summary", "title": "Summary", "markdown": SUMMARY}])["id"]
    return pid, Library(backend), {"client": auth.Client(None, backend), "store": store}


def sent(backend, key):
    return [form for k, form, _ in backend.calls if k == key]


def test_required_reviewers_share_sign_off_and_build_through_the_daemon_routes(db, parts, monkeypatch):
    """Sharing, editing reviewers, a teammate's sign-off and the build, as the app drives them."""
    pid, lib, net = parts
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        post = lambda tail, **body: app.post(f"/v1/plans/{pid}/{tail}", json=body)  # noqa: E731
        got = post("share", scope=TEAM, reviewers=[{"sub": "u_a", "required": True}, {"sub": "user_1", "required": True}])
        assert got.status_code == 200, got.json()
        assert (got.json()["gate"]["state"], got.json()["gate"]["required"]) == ("waiting", 2)
        assert post("decide", rev=1, approved=True).json()["error"]["code"] == "gate_closed"

        # The owner signs from the doc; a teammate signs in the Library and the sync brings it back.
        assert post("sign-off", rev=1).json()["gate"]["signed"] == 1
        lib.sign_offs.append({"rev": 1, "sub": "u_a", "at": 9.0})
        assert post("sync").json()["gate"]["state"] == "ready"

        # A required reviewer added in the Library since the last sync: the approval finds out.
        lib.reviewers = [*lib.reviewers, {"sub": "u_b", "required": True}]
        assert post("decide", rev=1, approved=True).status_code == 409
        assert sd.sync_open(db, **net) == 1
        assert plans.get(db, pid)["state"] == "open"

        assert post("reviewers", reviewers=[{"sub": "u_a", "required": True}]).json()["gate"]["required"] == 1
        assert post("decide", rev=1, approved=True).json()["state"] == "approved"
        assert post("reviewers", reviewers=[]).json()["error"]["code"] == "review_closed"
        assert sd.sync_open(db, **net) == 0                         # review closed: nothing more to bring back


def test_required_reviewers_are_synced_in_the_background(monkeypatch):
    from brindle.daemon import lifecycle
    from brindle.daemon.session_review import ReadinessWatch

    now, synced = [0.0], []

    class Server:
        should_exit = False

    async def elapse(seconds):
        now[0] += lifecycle.SHARED_DOCS_EVERY + 1
        await asyncio.to_thread(int)            # lets the sync's own task run
        if synced:
            Server.should_exit = True

    monkeypatch.setattr(lifecycle, "_sync_shared_docs", lambda: synced.append(True))
    monkeypatch.setattr(ReadinessWatch, "sweep_safely", lambda self: None)
    monkeypatch.setattr(lifecycle, "_purge_typed_words", lambda: None)
    monkeypatch.setattr(lifecycle, "_keep_codex_hook", lambda settle=0.0: None)
    asyncio.run(lifecycle._watch(Server(), lifecycle.Activity(build_app(TOKEN, port=8123)), False, wait=elapse,
                                 clock=lambda: now[0]))
    assert synced


def test_a_shared_plan_goes_to_the_library_and_what_teammates_write_comes_back(db, parts, backend):
    pid, lib, net = parts
    grant = {"u_c": {"can_edit": False, "can_suggest": False, "can_comment": True}}
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a"}, {"sub": "u_b", "required": False}], permissions=grant, **net)
    [made] = sent(backend, "POST /analysis-docs")
    assert made["scope"] == {"kind": "user", "id": "user_1"} and "folder" not in made       # the default Designs folder
    assert made["design"]["sections"][0]["markdown"] == SUMMARY
    assert sent(backend, f"POST {BASE}/share")[0]["scope"] == TEAM
    assert lib.reviewers == [{"sub": "u_a", "required": True}, {"sub": "u_b", "required": False}]
    assert lib.permissions["u_a"]["can_comment"] and lib.permissions["u_c"] == grant["u_c"]
    assert sd.sync(db, pid, **net)["pushed"] is False and not sent(backend, f"POST {DESIGN}/revisions")

    # What the Library answers: anchors located, an insertion point's context left out; one row this can't read.
    lib.threads = [
        {"id": 1, "kind": "comment", "rev": 1, "author": "u_a", "text": "Does a rented VM count?", "at": 5.0,
         "status": "open", "replies": [{"id": 2, "author": "u_b", "text": "Yes.", "at": 6.0}],
         "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 7, "quote": "Workers", "state": "current"}},
        {"id": 3, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": "Today, ", "before": "",
         "status": "open", "replies": [],
         "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 0, "quote": "", "state": "current"}},
        {"id": 4, "kind": "comment", "rev": 1, "author": "u_a", "text": "?", "status": "open", "replies": [],
         "anchor": {"kind": "elsewhere"}}]
    lib.votes = {"d1": {"repo": ["u_c"]}}
    assert sd.sync(db, pid, **net)["pulled"] > 0
    assert sd.sync(db, pid, **net)["pulled"] == 0                  # once, however often it runs

    got = plan_docs.fields(db, plans.get(db, pid))
    comment, suggestion = got["threads"]
    assert (comment["author"], comment["anchor"]["state"]) == ("teammate:u_a", "current")
    assert [r["author"] for r in comment["replies"]] == ["teammate:u_b"]
    assert (got["decisions"][0]["votes"], got["decisions"][0]["confirmed"]) == ({"repo": ["teammate:u_c"]}, None)
    assert sd.link(db, pid)["review"]["pending_reviewers"] == ["u_a"]

    lib.threads[0]["status"] = "resolved"
    sd.sync(db, pid, **net)
    assert plan_docs.fields(db, plans.get(db, pid))["threads"][0]["status"] == "resolved"
    lib.threads[0]["status"] = "open"
    sd.sync(db, pid, **net)

    # The person takes a teammate's insertion; the new revision goes up on the Library's head,
    # and in that one sync the old revision's imported votes go, the person's own staying.
    plan_docs.confirm(db, pid, 1, "d1", "org")
    plan_docs.accept(db, pid, suggestion["id"], 1)
    assert sd.sync(db, pid, **net)["pushed"] is True
    [pushed] = sent(backend, f"POST {DESIGN}/revisions")
    assert pushed["rev"] == 1 and pushed["sections"][0]["markdown"] == "Today, " + SUMMARY
    assert sd.link(db, pid)["remote_rev"] == 2
    assert plan_docs.fields(db, plans.get(db, pid))["decisions"][0]["votes"] == {"org": ["person"]}

    # The Library moved on without this plan: the send is refused, the comments still arrive.
    lib.rev += 1
    lib.threads.append({"id": 5, "kind": "comment", "rev": 3, "author": "u_a", "text": "Name the VM rule.",
                        "status": "open", "replies": [], "anchor": {"kind": "section", "section": "summary"}})
    plan_docs.edit(db, pid, 2, "summary", 0, "Today", "Now")
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert sd.link(db, pid)["local_rev"] == 2
    assert plan_docs.fields(db, plans.get(db, pid))["threads"][-1]["text"] == "Name the VM rule."

    # Request changes carries teammates' open comments and replies to the supervisor.
    plans.decide(db, pid, 3, False, person=True)
    said = messages(db)[-1]
    assert "Name the VM rule." in said and "u_a" in said and "Yes." in said and "u_b" in said


def test_reviewers_change_after_sharing_and_a_folder_is_only_used_when_named(db, parts, backend):
    pid, lib, net = parts
    folder = {"scope": TEAM, "id": "f_1"}
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a"}], folder=folder, **net)
    assert sent(backend, f"PUT {DESIGN}/folder") == [{"folder": folder}]
    lib.permissions["u_x"] = {"can_edit": True, "can_suggest": True, "can_comment": True}

    sd.set_reviewers(db, pid, [{"sub": "u_b", "required": False}], **net)
    assert lib.reviewers == [{"sub": "u_b", "required": False}]
    assert lib.permissions["u_x"]["can_edit"] and lib.permissions["u_b"]["can_comment"]   # added to, not replaced
    assert sd.link(db, pid)["review"]["ready"] is True
    with pytest.raises(RuntimeError):
        sd.set_reviewers(db, pid, [{"required": True}], **net)
    assert lib.reviewers == [{"sub": "u_b", "required": False}]


def test_an_interrupted_share_or_send_keeps_one_document_and_one_revision(db, parts, backend):
    pid, lib, net = parts
    # A refused create leaves no claim behind.
    lib.create_status = 403
    with pytest.raises(RuntimeError, match="switch_off"):
        sd.share(db, pid, TEAM, **net)
    assert sd.link(db, pid) is None
    lib.create_status = 200

    # A claim that died before the Library got anything: nothing to find, so it creates.
    now = time.time()
    db.conn.execute("INSERT INTO plan_shares (plan_id, owner, claimed_at) VALUES (?, 'user_1', ?)", (pid, now))
    assert sd.share(db, pid, TEAM, now=now + sd.CLAIM_FOR + 1, **net)["doc_id"] == DOC
    assert len(sent(backend, "POST /analysis-docs")) == 2
    db.conn.execute("DELETE FROM plan_shares WHERE plan_id=?", (pid,))
    lib.plan = None

    # A create that commits but whose answer is lost: the outcome is kept, and a second share leaves it alone.
    lib.lose_create = True
    with pytest.raises(auth.AuthError):
        sd.share(db, pid, TEAM, now=now, **net)
    lib.lose_create = False
    assert sd.link(db, pid)["doc_id"] is None
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, now=now + 1, **net)

    # When the Library can't say whether it landed, nothing is created.
    lib.list_status = 500
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, now=now + sd.CLAIM_FOR + 1, **net)
    lib.list_status = 200
    assert len(sent(backend, "POST /analysis-docs")) == 3 and sd.link(db, pid)["doc_id"] is None

    # A later share finds that document; one that fails after it is known finishes on the same document.
    lib.share_status = 500
    with pytest.raises(RuntimeError, match="too_many_scopes"):
        sd.share(db, pid, TEAM, now=now + 2 * sd.CLAIM_FOR + 2, **net)
    lib.share_status = 200
    assert sd.share(db, pid, TEAM, **net)["doc_id"] == DOC
    assert len(sent(backend, "POST /analysis-docs")) == 3 and len(sent(backend, f"POST {BASE}/share")) == 3

    # A send in flight is left alone.
    plan_docs.edit(db, pid, 1, "summary", 0, "Workers", "Hosts")
    db.conn.execute("UPDATE plan_shares SET claimed_at=? WHERE plan_id=?", (now, pid))
    assert sd.sync(db, pid, now=now + 1, **net)["pushed"] is False
    assert not sent(backend, f"POST {DESIGN}/revisions")
    db.conn.execute("UPDATE plan_shares SET claimed_at=NULL WHERE plan_id=?", (pid,))

    # A send whose answer is lost isn't sent twice: the next sync sees it landed.
    lib.lose_answer = True
    with pytest.raises(auth.AuthError):
        sd.sync(db, pid, **net)
    lib.lose_answer = False
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert len(sent(backend, f"POST {DESIGN}/revisions")) == 1 and sd.link(db, pid)["remote_rev"] == 2


def test_an_insertion_the_library_cant_place_here_stays_detached(db, parts):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)

    def insertion(tid, state):
        return {"id": tid, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": "Today, ",
                "status": "open", "replies": [],
                "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 0, "quote": "", "state": state}}

    def refused_last():
        thread = plan_docs.fields(db, plans.get(db, pid))["threads"][-1]
        assert thread["anchor"]["state"] == "detached"
        with pytest.raises(ServiceError) as refused:
            plan_docs.accept(db, pid, thread["id"], 1)
        assert refused.value.code == "stale"

    lib.threads = [insertion(1, "detached")]                # the Library says it is gone
    sd.sync(db, pid, **net)
    refused_last()
    lib.rev += 1                                            # placed, but on text this plan doesn't have
    lib.threads.append(insertion(2, "current"))
    sd.sync(db, pid, **net)
    refused_last()


def test_nothing_is_sent_without_team_or_from_another_account(db, parts, backend):
    pid, lib, net = parts
    login_as(backend, net["store"], features=("analysis_docs",))
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, **net)
    login_as(backend, net["store"], features=TEAM_LOGIN)
    with pytest.raises(RuntimeError):
        sd.share(db, pid, {"kind": "user", "id": "u_a"}, **net)
    assert not sent(backend, "POST /analysis-docs") and sd.link(db, pid) is None

    sd.share(db, pid, TEAM, **net)
    db.conn.execute("UPDATE plan_shares SET owner='someone_else' WHERE plan_id=?", (pid,))
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert not sent(backend, f"GET {DESIGN}")


def test_a_plan_with_no_sections_is_refused_before_anything_is_created(db, session, tmp_path, backend):
    boss, _ = session
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=TEAM_LOGIN)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Just a title\n\nNo headings.")["id"]
    Library(backend)
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, client=auth.Client(None, backend), store=store)
    assert not sent(backend, "POST /analysis-docs") and sd.link(db, pid) is None
