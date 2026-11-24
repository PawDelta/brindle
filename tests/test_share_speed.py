"""Call counts for the plan sharing paths."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Event

import pytest
from starlette.testclient import TestClient

from brindle import plan_docs, plans, spawn, visual_offers
from brindle.daemon.app import build_app
from brindle.db import DB
from brindle.pro import auth, credentials, shared_docs, shared_reader
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_plans import AUTH, TOKEN, session  # noqa: F401 - fixture
from test_shared_plan_docs import BASE, DESIGN, DOC, TEAM, parts  # noqa: F401 - fixture


@pytest.mark.variant("call-count budget: the speed audit's one-draft-lookup fix stays fixed")
def test_plan_dto_looks_up_the_draft_once_when_no_replica_exists(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    plan = plans.propose(db, boss, "# Cloud workers", [])
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        dto = plans.to_dto(db, plans.get(db, plan["id"]))
    finally:
        db.conn.set_trace_callback(None)

    assert dto["id"] == plan["id"]
    assert sum("FROM plan_shares WHERE plan_id=" in sql and "doc_id IS NOT NULL" in sql
               for sql in statements) == 1


@pytest.fixture
def shared(parts, backend, db, monkeypatch):
    pid, library, net = parts
    shared_docs.share(db, pid, TEAM, **net)
    monkeypatch.setattr(credentials, "default_store", lambda: net["store"])
    monkeypatch.setattr(auth, "UrllibTransport", lambda **kw: backend)
    backend.calls.clear()
    return pid, library, net


def _library_wave(backend, *paths):
    """One 150 ms wave of Library latency if every call starts before any can finish."""
    together = Barrier(len(paths))
    spans = {}

    def track(path, serve):
        def reply(form, headers):
            try:
                together.wait(0.5)
            except BrokenBarrierError:
                # The guard also lets the original sequential implementation finish.
                spans[path] = len(spans) * 150
            else:
                spans[path] = 0
            return serve(form, headers)
        return reply

    for path in paths:
        backend.routes[path] = track(path, backend.routes[path])
    return spans


@pytest.mark.variant("perf: catches access reads going sequential again")
def test_access_overlaps_all_three_library_reads(shared, backend):
    pid, _, _ = shared
    paths = [f"GET {BASE}", f"GET {DESIGN}", f"GET {DESIGN}/people"]
    deadlines = []
    request = backend.request

    def handled(*args):
        deadlines.append(spawn.current())
        return request(*args)

    backend.request = handled
    spans = _library_wave(backend, *paths)
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        response = app.get(f"/v1/plans/{pid}/access")
    assert response.status_code == 200, response.text
    assert sorted(backend.paths()) == sorted(paths)
    assert max(spans.values()) + 150 == 150  # simulated wall time
    assert deadlines[0] is not None and all(deadline is deadlines[0] for deadline in deadlines)


def _add_teammate_comment(library, thread=1, author="u_e"):
    library.threads.append({"id": thread, "kind": "comment", "rev": 1, "author": author,
                            "text": "Which VM?", "at": 5.0, "status": "open", "replies": [],
                            "anchor": {"kind": "section", "section": "summary"}})


def _show_teammate_name(backend):
    backend.routes[f"GET {DESIGN}/people"] = lambda f, h: (
        200, {"people": [{"id": "u_e", "display_name": "Dana"}]})


@pytest.mark.variant("perf: catches an idle sync re-reading the people directory")
def test_idle_sync_skips_the_directory(shared, backend, db):
    pid, library, net = shared
    _add_teammate_comment(library)
    _show_teammate_name(backend)
    assert shared_docs.sync(db, pid, **net)["pulled"]
    assert f"GET {DESIGN}/people" in backend.paths()
    assert plan_docs.fields(db, plans.get(db, pid))["threads"][0]["author_name"] == "Dana"
    backend.calls.clear()
    assert shared_docs.sync(db, pid, **net)["pulled"] == 0
    assert backend.paths() == [f"GET {DESIGN}"]


@pytest.mark.variant("perf: catches the idle-sync skip never re-reading names after an outage")
def test_idle_sync_recovers_names_after_a_directory_outage(shared, backend, db):
    pid, library, net = shared
    _add_teammate_comment(library)
    backend.routes[f"GET {DESIGN}/people"] = lambda f, h: (503, {"error": "unavailable"})
    assert shared_docs.sync(db, pid, **net)["pulled"]
    assert len(plan_docs.fields(db, plans.get(db, pid))["threads"]) == 1

    _show_teammate_name(backend)
    backend.calls.clear()
    assert shared_docs.sync(db, pid, **net)["pulled"]
    assert backend.paths() == [f"GET {DESIGN}", f"GET {DESIGN}/people"]
    assert plan_docs.fields(db, plans.get(db, pid))["threads"][0]["author_name"] == "Dana"
    backend.calls.clear()
    shared_docs.sync(db, pid, **net)
    assert backend.paths() == [f"GET {DESIGN}"]


@pytest.mark.variant("perf: catches the first share repeating the access reads")
def test_first_share_sync_can_return_access_with_one_set_of_reads(shared, backend):
    pid, _, _ = shared
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        response = app.post(f"/v1/plans/{pid}/sync", json={"include_access": True})
    assert response.status_code == 200, response.text
    assert response.json()["access"]["shared"]
    assert sorted(backend.paths()) == sorted([f"GET {BASE}", f"GET {DESIGN}", f"GET {DESIGN}/people"])


def test_overlapping_sync_recovers_names_for_a_newly_pulled_thread(shared, backend, db):
    pid, library, net = shared
    _add_teammate_comment(library)
    directory = f"GET {DESIGN}/people"
    backend.routes[directory] = lambda f, h: (503, {"error": "unavailable"})
    assert shared_docs.sync(db, pid, **net)["pulled"]

    older_is_waiting, release_older = Event(), Event()

    def older_names(form, headers):
        older_is_waiting.set()
        assert release_older.wait(3)
        return 200, {"people": [{"id": "u_e", "display_name": "Dana"}]}

    def background_sync():
        worker_db = DB()
        try:
            return shared_docs.sync(worker_db, pid, **net)
        finally:
            worker_db.conn.close()

    backend.routes[directory] = older_names
    with ThreadPoolExecutor(max_workers=1) as pool:
        older = pool.submit(background_sync)
        try:
            assert older_is_waiting.wait(3)
            _add_teammate_comment(library, thread=2, author="u_f")
            backend.routes[directory] = lambda f, h: (
                200, {"people": [{"id": "u_f", "display_name": "Alex"}]})

            def newer_design(form, headers):
                release_older.set()
                older.result(3)
                return 200, library.view()

            backend.routes[f"GET {DESIGN}"] = newer_design
            with TestClient(build_app(TOKEN), headers=AUTH) as app:
                response = app.post(f"/v1/plans/{pid}/sync", json={"include_access": True})
            assert response.status_code == 200, response.text
            assert len(response.json()["threads"]) == 2
            assert "Alex" not in [row["author_name"] for row in response.json()["threads"]]
        finally:
            release_older.set()

    backend.calls.clear()
    shared_docs.sync(db, pid, **net)
    assert backend.paths() == [f"GET {DESIGN}", directory]
    assert "Alex" in [row["author_name"] for row in plan_docs.fields(db, plans.get(db, pid))["threads"]]


def test_first_share_sync_applies_design_when_metadata_is_unavailable(shared, backend, db):
    pid, library, _ = shared
    _add_teammate_comment(library)
    plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Use a dedicated host.")
    backend.routes[f"GET {BASE}"] = lambda f, h: (503, {"error": "unavailable"})
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        response = app.post(f"/v1/plans/{pid}/sync", json={"include_access": True})
        assert response.status_code == 200, response.text
        assert "access" not in response.json()
        assert len(response.json()["threads"]) == 2
        assert f"GET {BASE}" in backend.paths()
        assert f"POST {DESIGN}/comments" in backend.paths()

        backend.routes[f"GET {DESIGN}"] = lambda f, h: (403, {"error": "forbidden"})
        refused = app.post(f"/v1/plans/{pid}/sync", json={"include_access": True})
    assert refused.status_code == 403
    assert shared_docs.link(db, pid)["access_refused"] == "forbidden"


@pytest.mark.variant("perf: catches a commenter waiting out a names call that always refuses")
def test_commenter_open_overlaps_title_and_design_without_asking_for_names(shared, backend):
    _, library, net = shared
    library.author = "u_author"
    library.permissions["user_1"] = {"can_comment": True, "can_edit": False}
    backend.routes[f"GET {DESIGN}/people"] = lambda f, h: (403, {"error": "forbidden"})
    paths = [f"GET {BASE}", f"GET {DESIGN}"]
    spans = _library_wave(backend, *paths)
    got = shared_reader.read(DOC, **net)
    assert got["title"] == library.title and got["can_comment"] and not got["can_edit"]
    assert sorted(backend.paths()) == sorted(paths)
    assert max(spans.values()) + 150 == 150  # simulated wall time


@pytest.mark.variant("editors still receive names and the directory overlaps title enrichment")
def test_editor_open_overlaps_names_with_an_outstanding_title(shared, backend):
    _, library, net = shared
    library.permissions["user_1"] = {"can_edit": True}
    backend.routes[f"GET {DESIGN}/people"] = lambda f, h: (
        200, {"people": [{"id": "u_e", "display_name": "Dana"}]})
    spans = _library_wave(backend, f"GET {BASE}", f"GET {DESIGN}/people")
    got = shared_reader.read(DOC, **net)
    assert got["can_edit"] and got["teammate_names"] == {"u_e": "Dana"}
    assert max(spans.values()) + 150 == 150


@pytest.mark.variant("perf: catches credentials loaded once per layer again")
def test_shared_access_loads_credentials_once_per_request(shared, monkeypatch):
    pid, _, net = shared
    reads = []
    load = net["store"].load

    def counted():
        reads.append(None)
        return load()

    monkeypatch.setattr(net["store"], "load", counted)
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        for expected in (1, 2):
            response = app.get(f"/v1/plans/{pid}/access")
            assert response.status_code == 200, response.text
            assert len(reads) == expected
