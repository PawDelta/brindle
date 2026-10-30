"""Adding an imported Knowledge folder to the library: secret-looking files are
never sent, nothing is sent when the switch is off or the folder changed after
the preview, a changed file re-uploads, the server prunes what the folder no
longer has, and failures are surfaced and retried."""
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
    bad path, at most 20 files), POST .../imports/{folder_id}/prune and DELETE
    /analysis-docs/imports/{folder_id}. ``docs`` lists every stored version; ``posts`` the file
    lists of the POSTs; ``items`` the live docs, keyed by (folder, path)."""
    backend.docs, backend.deleted, backend.switches = [], [], {"import_upload": "on"}
    backend.posts, backend.items, backend.ids = [], {}, itertools.count(1)
    backend.reject = backend.fail_delete = backend.bad = False
    backend.during = backend.after_delete = None

    def drop(doc_ids):
        backend.deleted.extend(doc_ids)
        backend.items = {k: v for k, v in backend.items.items() if v["id"] not in doc_ids}

    def delete(folder):
        if backend.fail_delete:
            return 500, {}
        drop([v["id"] for k, v in backend.items.items() if k[0] == folder])
        if backend.after_delete:
            backend.after_delete()
        return 200, {}

    def prune(folder, form):
        gone = [v["id"] for k, v in backend.items.items() if k[0] == folder and k[1] not in form["keep_paths"]]
        drop(gone)
        return 200, {"deleted": len(gone), "kept": len([k for k in backend.items if k[0] == folder])}

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
                cur = cur or {"id": f"d{next(backend.ids)}", "version": 0}
                backend.items[key] = {**cur, "hash": f["hash"], "version": cur["version"] + 1}
                out.append({"path": f["path"], "status": "created" if cur["version"] == 0 else "updated",
                            "doc_id": cur["id"], "version": cur["version"] + 1})
        backend.routes[f"DELETE /analysis-docs/imports/{folder}?scope_kind=user&scope_id={scope['id']}"] = \
            lambda f, h: delete(folder)
        backend.routes[f"POST /analysis-docs/imports/{folder}/prune"] = lambda f, h: prune(folder, f)
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


def cached(db):
    """path -> doc id of the local cache."""
    return {r["external_id"]: r["doc_id"] for r in db.conn.execute("SELECT * FROM kb_import_uploads")}


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


def test_a_deleted_file_is_pruned_and_a_changed_one_updates_in_place(folder, remote, deps):
    db, source, docs = folder
    (docs / "more.md").write_text("# More")
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)             # d1 more, d2 notes
    (docs / "more.md").unlink()
    (docs / "notes.md").write_text("# Notes\nthe TTL is now an hour")
    assert imports.sync_upload(db, source, "/repo", **deps) == {
        "uploaded": 1, "deleted": 1, "skipped": 2, "sent": True, "failed": []}
    assert remote.deleted == ["d1"] and [v["id"] for v in remote.items.values()] == ["d2"]
    assert remote.items[(source, "notes.md")]["version"] == 2 and cached(db) == {"notes.md": "d2"}


def test_overlapping_syncs_with_changed_content_converge(folder, remote, deps):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "notes.md").write_text("# Notes\nv2")
    overlapped = []

    def overlap():      # during the old run's v2 content request: it is held, then its lease lapses
        (docs / "notes.md").write_text("# Notes\nv3")
        overlapped.append(imports.sync_upload(db, source, "/repo", **deps))      # held: does nothing
        try:
            imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
        except imports.Busy:
            overlapped.append("busy")
        with db.tx() as c:
            c.execute("UPDATE kb_import_library SET op_until = 1")
        overlapped.append(imports.sync_upload(db, source, "/repo", **deps))      # a real replacement uploads v3

    remote.during = overlap
    imports.sync_upload(db, source, "/repo", **deps)             # then the old v2 write lands over it
    assert overlapped[0]["sent"] is False and overlapped[1] == "busy" and overlapped[2]["sent"] is True
    assert [d["md"][-2:] for d in remote.docs[-2:]] == ["v3", "v2"]          # the server now holds stale v2
    imports.sync_upload(db, source, "/repo", **deps)             # every sync sends hashes: v3 is restored
    assert remote.docs[-1]["md"].endswith("v3") and remote.deleted == []
    assert list(remote.items) == [(source, "notes.md")] and cached(db) == {"notes.md": "d1"}


def test_a_folder_over_the_prune_limit_is_refused_before_anything_is_sent(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    (docs / "more.md").write_text("# More")
    monkeypatch.setattr(imports, "MAX_FILES", 1)
    with pytest.raises(imports.TooManyFiles, match="pick a smaller folder"):
        imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert remote.posts == [] and not imports.in_library(db, source)


def test_a_run_that_loses_its_claim_deletes_nothing_and_the_next_sync_converges(folder, remote, deps):
    db, source, docs = folder
    (docs / "more.md").write_text("# More")
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "more.md").unlink()
    (docs / "notes.md").write_text("# Notes\nchanged")

    def steal():        # another holder took the folder and its lease has lapsed again
        with db.tx() as c:
            c.execute("UPDATE kb_import_library SET op = 'sync', op_token = 'other', op_until = 1")

    remote.during = steal
    sent = len(remote.calls)
    assert imports.sync_upload(db, source, "/repo", **deps)["deleted"] == 0
    assert not [c for c in remote.calls[sent:] if c[0].endswith("/prune")] and remote.deleted == []
    assert len(remote.items) == 2 and sorted(cached(db)) == ["more.md", "notes.md"]     # the stale cache stays
    assert imports.sync_upload(db, source, "/repo", **deps)["deleted"] == 1              # the next sync converges
    assert [k[1] for k in remote.items] == ["notes.md"] and cached(db) == {"notes.md": "d2"}


def test_removal_is_retried_while_removing_and_survives_the_source(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    sync_upload = imports.sync_upload
    monkeypatch.setattr(imports, "sync_upload", lambda d, s, r: sync_upload(d, s, r, **deps))
    KEY = "github.com/acme/app"

    def shown(identity=None):
        return kb.sources(db, KEY, identity or imports.current_identity(deps["client"], deps["store"]))[0]

    remote.fail_delete = True
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 1}
    assert (shown()["library_state"], shown()["library_removing"]) == ("removing", 1) and remote.deleted == []
    kb.sync(db, source, None, "/repo")                  # an index at the boundary, the DELETE still failing
    assert (shown()["library_state"], shown()["library_removing"]) == ("removing", 1)   # "Finish removing" stays
    remote.fail_delete, remote.switches = False, {"import_upload": "off"}
    sent = len(remote.calls)
    kb.sync(db, source, None, "/repo")                  # switch off: not even the removal is retried
    assert remote.deleted == [] and len(remote.calls) == sent + 1       # (only the switch is read)
    # Removal is not switch-gated: with the switch off, finishing it still deletes.
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert remote.deleted == ["d1"] and not imports.in_library(db, source) and shown()["library_state"] == "local"
    # A crashed add leaves `adding` with a lapsed lease: an index leaves it be, a new add takes it over.
    remote.switches = {"import_upload": "on"}

    def crashed():
        with db.tx() as c:
            c.execute("UPDATE kb_import_library SET state = 'adding', op = 'add', op_token = 'dead', op_until = 1"
                      " WHERE source_id = ?", (source,))

    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    crashed()
    kb.sync(db, source, None, "/repo")
    assert shown()["library_state"] == "adding" and remote.deleted == ["d1"]
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert shown()["library_state"] == "in_library"
    # Removing the source while the DELETE fails keeps the removal; another source's index finishes it.
    other = kb.add_source(db, KEY, kb.FOLDER, str(docs.parent / "other"))
    (docs.parent / "other").mkdir()
    remote.fail_delete = True
    kb.remove_source(db, source)
    remote.fail_delete = False
    assert db.conn.execute("SELECT count(*) FROM kb_import_removals").fetchone()[0] == 1 and remote.items
    kb.sync(db, other, None, "/repo")
    assert not remote.items and db.conn.execute("SELECT count(*) FROM kb_import_removals").fetchone()[0] == 0


def test_account_changes_never_move_another_identitys_folder(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    sync_upload = imports.sync_upload
    monkeypatch.setattr(imports, "sync_upload", lambda d, s, r: sync_upload(d, s, r, **deps))
    KEY = "github.com/acme/app"
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    first = imports.current_identity(deps["client"], deps["store"])

    def shown(identity=None):
        return kb.sources(db, KEY, identity or imports.current_identity(deps["client"], deps["store"]))[0]

    def sign_in_as(sub, access_token=None, base=None):
        creds = deps["store"].load()
        creds["base_url"] = base = base or creds["base_url"]
        creds["entitlement"] = sign(remote.key, claims(features=["analysis_docs"], sub=sub, iss=base))
        creds["access_token"] = access_token or creds["access_token"]
        deps["store"].save(creds)
        license.clear_cache()

    # An account switch: the folder is local for the new identity, which uploads nothing and deletes
    # nothing; the first identity's unfinished removal stays untouched until it is signed in again.
    remote.fail_delete = True
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    remote.fail_delete = False
    sent, gone = len(remote.docs), len(remote.deleted)
    sign_in_as("user_2")
    kb.sync(db, source, None, "/repo")
    assert (len(remote.docs), len(remote.deleted)) == (sent, gone)
    assert shown()["library_state"] == "local" and shown(first)["library_state"] == "removing"
    sign_in_as("user_1")
    kb.sync(db, source, None, "/repo")
    assert len(remote.deleted) == gone + 1 and shown()["library_state"] == "local"
    # An account switch while the folder's DELETE is out: it was sent under the first account, so its
    # 200 is believed and the folder is dropped; nothing more is sent under the new one.
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
    sign_in_as("user_1")
    # Changed content while the first account is still in_library and the second is signed in: nothing uploads.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "notes.md").write_text("# Notes\nedited while another account is signed in")
    sent = len(remote.docs)
    sign_in_as("user_2")
    kb.sync(db, source, None, "/repo")
    assert len(remote.docs) == sent and shown(first)["library_state"] == "in_library"
    sign_in_as("user_1")
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    # The account switches while a POST succeeds: its docs are cached under the first account (the run
    # then stops, without a prune), so a later removal deletes every remote doc.
    first_new = len(remote.docs)
    remote.during = lambda: sign_in_as("user_2")
    prunes = len([c for c in remote.calls if c[0].endswith("/prune")])
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert len(remote.docs) == first_new + 2 and len(cached(db)) == 2
    assert len([c for c in remote.calls if c[0].endswith("/prune")]) == prunes
    sign_in_as("user_1")
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    assert not remote.items
    # The account switches while the snapshot is taken (the store changes right after its one
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
    # An expiring token while the store goes to another backend's record (same user and org) and,
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
    # A backend-only switch while the folder's DELETE is out: its 200 is believed, nothing more is sent.
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
