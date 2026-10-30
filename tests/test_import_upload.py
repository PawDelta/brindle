"""Adding an imported Knowledge folder to the library: secret-looking files are
never sent, nothing is sent when the switch is off or the folder changed after
the preview, a changed file re-uploads, and failures are surfaced and retried."""
import hashlib
import itertools
import time

import pytest

from brindle import airgap, kb
from brindle.pro import analysis_imports as imports
from brindle.pro import auth, credentials, license
from pro_fixtures import BASE, backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as


@pytest.fixture
def remote(backend):
    """The backend's import contract: POST /analysis-docs/imports (per file created / updated /
    unchanged by hash, needs_content when a new file comes without text, 422 hash_mismatch or
    bad path, at most 20 files) and DELETE /analysis-docs/imports/{folder_id}. ``docs`` lists
    every stored version; ``posts`` the file lists of the POSTs; ``items`` the live docs."""
    backend.docs, backend.deleted, backend.switches = [], [], {"import_upload": "on"}
    backend.posts, backend.items, backend.ids = [], {}, itertools.count(1)
    backend.reject = backend.fail_delete = backend.bad = False
    backend.during = backend.after_delete = None

    def delete(doc_ids):
        if backend.fail_delete:
            return 500, {}
        backend.deleted.extend(doc_ids)
        backend.items = {k: v for k, v in backend.items.items() if v["id"] not in doc_ids}
        if backend.after_delete:
            backend.after_delete()
        return 200, {}

    def store(form, headers):
        if backend.reject:
            return 500, {}
        files, folder, scope = form["files"], form["folder_id"], form["scope"]
        backend.posts.append(files)
        if len(files) > 20 or len({f["path"] for f in files}) != len(files) or \
                any(f["path"] == backend.bad for f in files) or \
                any(f["path"].startswith("/") or ".." in f["path"].split("/") for f in files):
            return 422, {"error": "validation"}
        if any("md" in f and hashlib.sha256(f["md"].encode()).hexdigest() != f["hash"] for f in files):
            return 422, {"error": "hash_mismatch"}
        if any("md" in f for f in files) and backend.during:
            during, backend.during = backend.during, None
            during()
        out = []
        for f in files:
            key, cur = (folder, f["path"]), backend.items.get((folder, f["path"]))
            if cur and cur["hash"] == f["hash"]:
                out.append({"path": f["path"], "status": "unchanged", "doc_id": cur["id"], "version": cur["version"]})
            elif "md" not in f:
                out.append({"path": f["path"], "status": "needs_content", "doc_id": cur and cur["id"],
                            "version": None})
            else:
                backend.docs.append({"folder": folder, "path": f["path"], "md": f["md"]})
                if cur is None:
                    cur = {"id": f"d{next(backend.ids)}", "version": 0}
                    doc_id = cur["id"]
                    backend.routes[f"DELETE /analysis-docs/{doc_id}"] = lambda f, h, i=doc_id: delete([i])
                backend.items[key] = {**cur, "hash": f["hash"], "version": cur["version"] + 1}
                out.append({"path": f["path"], "status": "created" if cur["version"] == 0 else "updated",
                            "doc_id": cur["id"], "version": cur["version"] + 1})
        backend.routes[f"DELETE /analysis-docs/imports/{folder}?scope_kind=user&scope_id={scope['id']}"] = \
            lambda f, h: delete([v["id"] for k, v in backend.items.items() if k[0] == folder])
        return 200, {"folder_id": folder, "files": out}

    backend.routes["POST /analysis-docs/imports"] = store
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    return backend


@pytest.fixture
def deps(tmp_path, remote, fixed_identity):
    store = credentials.FileStore(tmp_path / "pro")
    login_as(remote, store, features=("analysis_docs",))
    return {"client": auth.Client(None, remote), "store": store,
            "key_store": credentials.FileStore(tmp_path / "pro", account="learning-keys"),
            "identity": lambda: "a" * 40}


@pytest.fixture
def folder(tmp_path, db):
    docs = tmp_path / "imported"
    docs.mkdir()
    (docs / "notes.md").write_text("# Notes\nthe TTL is too short")
    (docs / "prod-secrets.md").write_text("# Not for sending")
    (docs / "setup.md").write_text("export API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz0123")
    return db, kb.add_source(db, "github.com/acme/app", kb.FOLDER, str(docs)), docs


def token(db, source):
    return imports.dry_run(db, source)["token"]


def test_secret_looking_files_are_never_sent(folder, remote, deps):
    db, source, _ = folder
    plan = imports.dry_run(db, source)
    assert [f["name"] for f in plan["files"]] == ["notes.md"]
    assert {s["name"] for s in plan["skipped"]} == {"prod-secrets.md", "setup.md"}
    overlapping = []      # an index starting while the add is mid-upload leaves it be
    remote.during = lambda: overlapping.append(imports.sync_upload(db, source, "/repo", **deps))
    imports.add_to_library(db, source, "/repo", plan["token"], **deps)
    assert overlapping[0]["sent"] is False and len(overlapping) == 1
    assert [(d["folder"], d["path"]) for d in remote.docs] == [(source, "notes.md")]
    assert [["md" in f for f in files] for files in remote.posts] == [[False], [True]]   # hashes first, then text


@pytest.mark.parametrize("off", ["switch", "airgap"])
def test_nothing_is_sent_when_uploading_is_off(off, folder, remote, deps, monkeypatch):
    db, source, _ = folder
    if off == "switch":
        remote.switches = {"import_upload": "off"}
    else:
        monkeypatch.setattr(airgap, "enabled", lambda cfg=None: True)
    with pytest.raises(imports.ImportUploadOff):
        imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert remote.docs == [] and not imports.in_library(db, source)


def test_only_the_confirmed_files_go_and_a_rejected_upload_is_surfaced(folder, remote, deps):
    db, source, docs = folder
    confirmed = token(db, source)
    (docs / "unconfirmed.md").write_text("# Added after the preview")
    with pytest.raises(imports.ManifestChanged) as changed:
        imports.add_to_library(db, source, "/repo", confirmed, **deps)
    assert [f["name"] for f in changed.value.plan["files"]] == ["notes.md", "unconfirmed.md"]
    assert remote.docs == [] and not imports.in_library(db, source)
    remote.reject = True
    with pytest.raises(imports.UploadFailed):
        imports.add_to_library(db, source, "/repo", changed.value.plan["token"], **deps)
    assert not imports.in_library(db, source)      # nothing went up: not a member
    remote.reject = False
    imports.add_to_library(db, source, "/repo", changed.value.plan["token"], **deps)
    assert len(remote.docs) == 2 and imports.in_library(db, source)


def test_files_go_in_batches_of_20_and_a_rejected_file_does_not_hold_back_the_rest(folder, remote, deps):
    db, source, docs = folder
    for n in range(21):
        (docs / f"n{n:02}.md").write_text(f"# Note {n}")
    remote.bad = "n20.md"
    with pytest.raises(imports.UploadFailed, match="n20.md"):
        imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert max(len(files) for files in remote.posts) == 20
    assert len(remote.items) == 21 and imports.in_library(db, source)       # notes.md + n00..n19; only n20 failed


def test_a_lost_claim_queues_every_doc_of_the_batch_that_was_stored(folder, remote, deps):
    db, source, docs = folder
    (docs / "more.md").write_text("# More")

    def steal():
        with db.tx() as c:
            c.execute("UPDATE kb_import_library SET op_token = 'other'")

    remote.during = steal
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    queued = lambda: sorted(r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM kb_import_deletions"))  # noqa: E731
    assert queued() == ["d1", "d2"] and remote.deleted == []      # both docs are accounted for, left to the holder
    assert db.conn.execute("SELECT count(*) FROM kb_import_uploads").fetchone()[0] == 0


def test_a_run_that_lost_its_claim_deletes_nothing_the_replacement_adopted(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)             # d1
    (docs / "more.md").write_text("# More")
    send, replaced = imports._send, []

    def send_then_replace(*args):      # the lease lapses after the answer and a real sync takes the folder over
        got = send(*args)
        if not replaced:
            replaced.append(True)
            with db.tx() as c:
                c.execute("UPDATE kb_import_library SET op_until = 1")
            imports.sync_upload(db, source, "/repo", **deps)          # adopts d2 (the answer was "unchanged")
        return got

    monkeypatch.setattr(imports, "_send", send_then_replace)
    imports.sync_upload(db, source, "/repo", **deps)
    assert remote.deleted == [] and sorted(v["id"] for v in remote.items.values()) == ["d1", "d2"]
    assert sorted(r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM kb_import_uploads")) == ["d1", "d2"]
    assert db.conn.execute("SELECT count(*) FROM kb_import_deletions").fetchone()[0] == 0


def test_a_restored_file_is_not_adopted_while_its_old_doc_waits_for_deletion(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "notes.md").unlink()
    remote.fail_delete = True
    imports.sync_upload(db, source, "/repo", **deps)                          # d1 queued, its delete fails
    (docs / "notes.md").write_text("# Notes\nthe TTL is too short")
    exchange = imports._exchange

    def exchange_then_drain(*args):     # the deletes recover and another sync drains right after the answer
        got = exchange(*args)
        remote.fail_delete = False
        imports.drain_pending(db, client=deps["client"], store=deps["store"])
        live.extend(v["id"] for v in remote.items.values())
        return got

    live = []
    monkeypatch.setattr(imports, "_exchange", exchange_then_drain)
    assert imports.sync_upload(db, source, "/repo", **deps)["failed"] == ["notes.md"]     # d1 comes back: not adopted
    assert live == ["d1"]       # the other drain left it alone while this sync held the folder
    monkeypatch.setattr(imports, "_exchange", exchange)
    imports.sync_upload(db, source, "/repo", **deps)                          # d1 deleted, the file sent again
    assert [v["id"] for v in remote.items.values()] == ["d2"]
    assert [r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM kb_import_uploads")] == ["d2"]


def test_a_drain_sends_no_folder_delete_while_another_operation_holds_the_folder(folder, remote, deps):
    db, source, _ = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    remote.fail_delete = True
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    remote.fail_delete = False
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET op = 'add', op_token = 'held', op_until = 9e12")
    sent = len(remote.calls)
    imports.drain_pending(db, client=deps["client"], store=deps["store"])
    assert not [c for c in remote.calls[sent:] if c[0].startswith("DELETE /analysis-docs/imports/")]
    assert imports.state(db, source, imports.current_identity(deps["client"], deps["store"])) == imports.REMOVING


def test_a_changed_file_reuploads_and_removal_survives_a_failed_delete(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    sync_upload = imports.sync_upload
    monkeypatch.setattr(imports, "sync_upload", lambda d, s, r: sync_upload(d, s, r, **deps))
    assert kb.sync(db, source, None, "/repo").get("uploaded") is None      # unchanged: nothing
    (docs / "notes.md").write_text("# Notes\nthe TTL is now an hour")
    remote.fail_delete = True
    assert kb.sync(db, source, None, "/repo")["uploaded"] == 1             # the real indexing path
    assert remote.docs[-1]["md"].endswith("an hour") and remote.deleted == []
    assert remote.items[(source, "notes.md")]["version"] == 2         # updated in place: one doc, tracked by id
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 1}
    KEY = "github.com/acme/app"

    def shown(identity=None):
        return kb.sources(db, KEY, identity or imports.current_identity(deps["client"], deps["store"]))[0]

    assert (shown()["library_state"], shown()["library_removing"]) == ("removing", 1) and remote.deleted == []
    kb.sync(db, source, None, "/repo")                  # an index at the boundary, the DELETE still failing
    assert (shown()["library_state"], shown()["library_removing"]) == ("removing", 1)   # "Finish removing" stays
    remote.fail_delete, remote.switches = False, {"import_upload": "off"}
    kb.sync(db, source, None, "/repo")                  # switch off: not even the queued deletes go
    assert remote.deleted == [] and shown()["library_removing"] == 1
    # Removal is not switch-gated: with the switch off, finishing it still deletes (uploads sent nothing).
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert remote.deleted == ["d1"] and not imports.in_library(db, source) and len(remote.docs) == 2
    remote.switches = {"import_upload": "on"}
    assert shown()["library_state"] == "local"
    # Overlapping syncs with changed content, via the real sync: the second finds the first holding
    # the folder and does nothing (a removal then is refused too); the newer content wins next time.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)             # d2: "an hour"
    (docs / "notes.md").write_text("# Notes\nv2")
    overlapped = []

    def overlap():
        (docs / "notes.md").write_text("# Notes\nv3")
        overlapped.append(kb.sync(db, source, None, "/repo"))
        try:
            imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
        except imports.Busy:
            overlapped.append("busy")

    remote.during = overlap
    kb.sync(db, source, None, "/repo")                  # uploads v2 into d2
    assert "uploaded" not in overlapped[0] and overlapped[1] == "busy" and remote.deleted == ["d1"]
    kb.sync(db, source, None, "/repo")                  # the newer v3 replaces v2 in d2
    assert remote.docs[-1]["md"].endswith("v3") and remote.deleted == ["d1"]
    assert db.conn.execute("SELECT doc_id FROM kb_import_uploads").fetchall()[0]["doc_id"] == "d2"
    # A crashed add leaves `adding` with a lapsed lease: a new add and a removal take it over
    # (a live lease would not be); no waiting, the lease is set in the past.
    def crashed():
        with db.tx() as c:
            c.execute("UPDATE kb_import_library SET state = 'adding', op = 'add', op_token = 'dead', op_until = 1"
                      " WHERE source_id = ?", (source,))

    def tracked():
        return [r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM kb_import_uploads")]

    crashed()
    kb.sync(db, source, None, "/repo")      # an index must not finish a removal of an adding folder
    assert shown()["library_state"] == "adding" and tracked() == ["d2"]
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert shown()["library_state"] == "in_library"
    crashed()
    kb.sync(db, source, None, "/repo")
    assert shown()["library_state"] == "adding" and tracked() == ["d2"] and "d2" not in remote.deleted
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert shown()["library_state"] == "local" and remote.deleted[-1] == "d2"
    # Another source's sync leaves a folder another operation holds alone (its queued delete could
    # overlap that operation's upload); once the hold ends, a sync drains it and the folder is done.
    other = kb.add_source(db, KEY, kb.FOLDER, str(docs.parent / "other"))
    (docs.parent / "other").mkdir()
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # d3
    remote.fail_delete = True
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 1}
    remote.fail_delete = False
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET op = 'sync', op_token = 'held', op_until = 9e12")
    kb.sync(db, other, None, "/repo")
    assert remote.deleted[-1] == "d2" and (shown()["library_state"], shown()["library_removing"]) == ("removing", 1)
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET op = NULL, op_token = NULL, op_until = NULL")
    kb.sync(db, other, None, "/repo")
    assert remote.deleted[-1] == "d3" and shown()["library_state"] == "local"
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # d4
    remote.fail_delete = True
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    remote.fail_delete = False
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert shown()["library_state"] == "local"
    # An account switch: the folder is local for the new identity, which uploads nothing; the first
    # identity's queued deletion stays untouched until it is signed in again.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # d5
    first = imports.current_identity(deps["client"], deps["store"])
    remote.fail_delete = True
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    remote.fail_delete = False
    sent, gone = len(remote.docs), len(remote.deleted)

    def sign_in_as(sub, access_token=None, base=None):
        creds = deps["store"].load()
        creds["base_url"] = base = base or creds["base_url"]
        creds["entitlement"] = sign(remote.key, claims(features=["analysis_docs"], sub=sub, iss=base))
        creds["access_token"] = access_token or creds["access_token"]
        deps["store"].save(creds)
        license.clear_cache()

    sign_in_as("user_2")
    kb.sync(db, source, None, "/repo")
    assert (len(remote.docs), len(remote.deleted)) == (sent, gone)
    assert shown()["library_state"] == "local" and shown(first)["library_state"] == "removing"
    sign_in_as("user_1")
    kb.sync(db, source, None, "/repo")
    assert remote.deleted[-1] == "d5" and shown()["library_state"] == "local"
    # (a) An account switch while the folder's DELETE is out: it was sent under the first account, so its
    # 200 is believed and the first account's queue is cleared; nothing more is sent under the new one.
    (docs / "more.md").write_text("# More\nsecond file")
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # two docs
    remote.fail_delete = True
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 2}
    remote.fail_delete, remote.after_delete = False, lambda: sign_in_as("user_2")
    gone = len(remote.deleted)
    kb.sync(db, source, None, "/repo")
    remote.after_delete = None
    assert len(remote.deleted) == gone + 2 and shown(first)["library_state"] == "local"
    assert remote.calls[-1][0].startswith("DELETE /analysis-docs/imports/")      # the last request sent
    assert shown()["library_state"] == "local"
    sign_in_as("user_1")
    kb.sync(db, source, None, "/repo")
    assert len(remote.deleted) == gone + 2 and shown()["library_state"] == "local"
    # (b) Changed content while the first account is still in_library and the second is signed in:
    # nothing uploads.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "notes.md").write_text("# Notes\nedited while another account is signed in")
    sent = len(remote.docs)
    sign_in_as("user_2")
    kb.sync(db, source, None, "/repo")
    assert len(remote.docs) == sent and shown(first)["library_state"] == "in_library"
    sign_in_as("user_1")
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    # (c) The account switches while a POST succeeds: its docs are recorded under the first account
    # (the run then stops), so a later removal deletes every remote doc.
    first_new = len(remote.docs)
    remote.during = lambda: sign_in_as("user_2")
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert len(remote.docs) == first_new + 2 and len(tracked()) == 2       # the batch's answer is believed
    mine = set(tracked())
    sign_in_as("user_1")
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    assert mine <= set(remote.deleted)
    # (d) The account switches while the snapshot is taken (the store changes right after its one
    # read, tokens differ): the snapshot is wholly the first account's, and its first request is refused.
    sign_in_as("user_1", "token-1")
    real_load, armed = deps["store"].load, []

    def load_then_switch():
        record = real_load()
        if not armed:
            armed.append(True)
            sign_in_as("user_2", "token-2")
        return record

    deps["store"].load = load_then_switch
    snap = imports._snapshot(deps["client"], deps["store"])
    del deps["store"].load
    assert (snap.who, snap.token) == (first, "token-1")
    calls = len(remote.calls)
    with pytest.raises(imports.AccountChanged):
        imports._call(snap, "GET", "/me/analysis-docs/settings")
    assert len(remote.calls) == calls
    # (e) An expiring token while the store goes to another backend's record (same user and org) and,
    # in the A/B/A case, back: the refresh sends only the record's own token to the record's own host,
    # never the other record's, and the new tokens are kept only if the store holds the record they came from.
    other = "https://other.pawdelta.test/api/brindle/v1"
    real_request, entry = remote.request, {}

    def expiring_login():
        login_as(remote, deps["store"], features=("analysis_docs",))
        creds = deps["store"].load()
        creds["access_expires_at"] = 1
        deps["store"].save(creds)
        entry["a"] = deps["store"].load()
        entry["b"] = {**entry["a"], "base_url": other, "refresh_token": "rt-b", "access_token": "token-b",
                      "entitlement": sign(remote.key, claims(features=["analysis_docs"], sub="user_1", iss=other))}
        return len(remote.calls)

    def refresh_bodies(since):
        return [c[1]["refresh_token"] for c in remote.calls[since:] if c[0] == "POST /token/refresh"]

    def entitlements(status=200):      # the fake backend's entitlement has no library feature; 503 on request
        def request(method, url, form, headers):
            got = real_request(method, url, form, headers)
            if "/entitlement" in url:
                got = (status, {**got[1], "entitlement": sign(remote.key, claims(features=["analysis_docs"]))}
                       if status == 200 else {})
            return got
        remote.request = request

    def scripted_loads(*script):       # the n-th store read first saves a record ("before") or after it ("after")
        real, reads = deps["store"].load, []

        def load():
            when, act = script[len(reads)] if len(reads) < len(script) else (None, None)
            reads.append(1)
            if when == "before":
                deps["store"].save(act)
            got = real()
            if when == "after":
                act() if callable(act) else deps["store"].save(act)
            return got
        deps["store"].load = load

    def live(since):                   # nothing was revoked: the current sign-in still gets answers
        return auth.authed(deps["client"], deps["store"], "GET", "/me/analysis-docs/settings")[0] == 200

    since = expiring_login()           # A is read, then the store holds B: nothing at all is sent
    entitlements()
    scripted_loads(("after", entry["b"]))
    with pytest.raises(imports.AccountChanged):
        imports._snapshot(deps["client"], deps["store"])
    del deps["store"].load
    assert refresh_bodies(since) == [] and deps["store"].load() == entry["b"]
    since = expiring_login()           # A, B, A: when the lock is taken A is back, so only A's token goes out
    scripted_loads(("after", entry["b"]), ("before", entry["a"]))
    snap = imports._snapshot(deps["client"], deps["store"])
    del deps["store"].load
    assert refresh_bodies(since) == [entry["a"]["refresh_token"]] and snap.who == first
    assert snap.token != entry["a"]["access_token"] and deps["store"].load()["access_token"] == snap.token
    since = expiring_login()           # (a) auth.refresh rotates between the snapshot's read and its lock: one rotation
    scripted_loads(("after", lambda: auth.refresh(deps["client"], deps["store"], entry["a"])))
    with pytest.raises(imports.AccountChanged):
        imports._snapshot(deps["client"], deps["store"])
    del deps["store"].load
    assert len(refresh_bodies(since)) == 1 and live(since)
    since = expiring_login()           # (a) the other way round: the snapshot rotates, a stale auth.refresh reuses it
    imports._snapshot(deps["client"], deps["store"])
    auth.refresh(deps["client"], deps["store"], entry["a"])
    assert len(refresh_bodies(since)) == 1 and live(since)
    since = expiring_login()           # (b) the entitlement fetch fails after the rotation: the saved pair is kept
    entitlements(503)
    with pytest.raises(auth.AuthError):
        imports._snapshot(deps["client"], deps["store"])
    saved = deps["store"].load()
    assert saved["refresh_token"] != entry["a"]["refresh_token"] and saved["access_expires_at"] > time.time()
    entitlements()
    assert imports._snapshot(deps["client"], deps["store"]).token == saved["access_token"]
    assert len(refresh_bodies(since)) == 1
    del remote.request
    creds = deps["store"].load()
    creds["access_expires_at"] = time.time() + 900
    deps["store"].save(creds)
    sign_in_as("user_1", "token-1", BASE)
    # (f) A backend-only switch in the middle of a batch of deletes: nothing more is sent, rows stay queued.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # two docs
    remote.fail_delete = True
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 2}
    remote.fail_delete, at_switch, gone = False, [], len(remote.deleted)

    def switch_backend():
        at_switch.append(len(remote.calls))
        sign_in_as("user_1", None, other)

    remote.after_delete = switch_backend
    kb.sync(db, source, None, "/repo")
    remote.after_delete = None
    assert len(remote.deleted) == gone + 2 and len(remote.calls) == at_switch[0]     # nothing sent after it
    assert shown(first)["library_state"] == "local"
    sign_in_as("user_1", None, BASE)
    kb.sync(db, source, None, "/repo")
    assert shown()["library_state"] == "local" and len(remote.deleted) == gone + 2
    # A file gone from the folder: its doc is deleted by id (the import API has no per-file removal).
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "more.md").unlink()
    gone = len(remote.deleted)
    kb.sync(db, source, None, "/repo")
    assert len(remote.deleted) == gone + 1 and len(tracked()) == 1 and len(remote.items) == 1
