"""Adding an imported Knowledge folder to the library: secret-looking files are
never sent, nothing is sent when the switch is off or the folder changed after
the preview, a changed file re-uploads, and failures are surfaced and retried."""
import pytest

from brindle import airgap, kb
from brindle.pro import analysis_imports as imports
from brindle.pro import auth, credentials, license
from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as


@pytest.fixture
def remote(backend):
    backend.docs, backend.deleted, backend.switches = [], [], {"import_upload": "on"}
    backend.reject = backend.fail_delete = False
    backend.during = backend.after_delete = None

    def delete(doc_id):
        if backend.fail_delete:
            return 500, {}
        backend.deleted.append(doc_id)
        if backend.after_delete:
            backend.after_delete()
        return 200, {}

    def store(form, headers):
        if backend.reject:
            return 500, {}
        if backend.during:
            during, backend.during = backend.during, None
            during()
        backend.docs.append(dict(form))
        doc_id = f"d{len(backend.docs)}"
        backend.routes[f"DELETE /analysis-docs/{doc_id}"] = lambda f, h: delete(doc_id)
        return 200, {"id": doc_id, "version": 1}

    backend.routes["POST /analysis-docs"] = store
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
    assert [d["files"][0]["name"] for d in remote.docs] == ["notes.md"]
    assert remote.docs[0]["source"] == {"kind": "import", "ref": "imported"}


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


def test_a_changed_file_reuploads_and_removal_survives_a_failed_delete(folder, remote, deps, monkeypatch):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    sync_upload = imports.sync_upload
    monkeypatch.setattr(imports, "sync_upload", lambda d, s, r: sync_upload(d, s, r, **deps))
    assert kb.sync(db, source, None, "/repo").get("uploaded") is None      # unchanged: nothing
    (docs / "notes.md").write_text("# Notes\nthe TTL is now an hour")
    remote.fail_delete = True
    assert kb.sync(db, source, None, "/repo")["uploaded"] == 1             # the real indexing path
    assert remote.docs[-1]["files"][0]["md"].endswith("an hour") and remote.deleted == []
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 2}
    KEY = "github.com/acme/app"

    def shown(identity=None):
        return kb.sources(db, KEY, identity or imports.current_identity(deps["client"], deps["store"]))[0]

    assert (shown()["library_state"], shown()["library_removing"]) == ("removing", 2) and remote.deleted == []
    kb.sync(db, source, None, "/repo")                  # an index at the boundary, the DELETE still failing
    assert (shown()["library_state"], shown()["library_removing"]) == ("removing", 2)   # "Finish removing" stays
    remote.fail_delete, remote.switches = False, {"import_upload": "off"}
    kb.sync(db, source, None, "/repo")                  # switch off: not even the queued deletes go
    assert remote.deleted == [] and shown()["library_removing"] == 2
    # Removal is not switch-gated: with the switch off, finishing it still deletes (uploads sent nothing).
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert sorted(remote.deleted) == ["d1", "d2"] and not imports.in_library(db, source) and len(remote.docs) == 2
    remote.switches = {"import_upload": "on"}
    assert shown()["library_state"] == "local"
    # Overlapping syncs with changed content, via the real sync: the second finds the first holding
    # the folder and does nothing (a removal then is refused too); the newer content wins next time.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)             # d3: "an hour"
    (docs / "notes.md").write_text("# Notes\nv2")
    remote.deleted.clear()
    overlapped = []

    def overlap():
        (docs / "notes.md").write_text("# Notes\nv3")
        overlapped.append(kb.sync(db, source, None, "/repo"))
        try:
            imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
        except imports.Busy:
            overlapped.append("busy")

    remote.during = overlap
    kb.sync(db, source, None, "/repo")                  # uploads v2 (d4), replacing d3
    assert "uploaded" not in overlapped[0] and overlapped[1] == "busy" and remote.deleted == ["d3"]
    kb.sync(db, source, None, "/repo")                  # the newer v3 (d5) replaces v2
    assert remote.docs[-1]["files"][0]["md"].endswith("v3") and remote.deleted == ["d3", "d4"]
    assert db.conn.execute("SELECT doc_id FROM kb_import_uploads").fetchall()[0]["doc_id"] == "d5"
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
    assert shown()["library_state"] == "adding" and tracked() == ["d5"]
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    assert shown()["library_state"] == "in_library"
    crashed()
    kb.sync(db, source, None, "/repo")
    assert shown()["library_state"] == "adding" and tracked() == ["d5"] and "d5" not in remote.deleted
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert shown()["library_state"] == "local" and remote.deleted[-1] == "d5"
    # Another source's sync drains this folder's queue; the folder, held at that moment, stays
    # "removing" with nothing queued, so Finish removing is still there and completes it.
    other = kb.add_source(db, KEY, kb.FOLDER, str(docs.parent / "other"))
    (docs.parent / "other").mkdir()
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # d6
    remote.fail_delete = True
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 1}
    remote.fail_delete = False
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET op = 'sync', op_token = 'held', op_until = 9e12")
    kb.sync(db, other, None, "/repo")
    assert remote.deleted[-1] == "d6" and (shown()["library_state"], shown()["library_removing"]) == ("removing", 0)
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET op = NULL, op_token = NULL, op_until = NULL")
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert shown()["library_state"] == "local"
    # An account switch: the folder is local for the new identity, which uploads nothing; the first
    # identity's queued deletion stays untouched until it is signed in again.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # d7
    first = imports.current_identity(deps["client"], deps["store"])
    remote.fail_delete = True
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    remote.fail_delete = False
    sent, gone = len(remote.docs), len(remote.deleted)

    def sign_in_as(sub):
        creds = deps["store"].load()
        creds["entitlement"] = sign(remote.key, claims(features=["analysis_docs"], sub=sub))
        deps["store"].save(creds)
        license.clear_cache()

    sign_in_as("user_2")
    kb.sync(db, source, None, "/repo")
    assert (len(remote.docs), len(remote.deleted)) == (sent, gone)
    assert shown()["library_state"] == "local" and shown(first)["library_state"] == "removing"
    sign_in_as("user_1")
    kb.sync(db, source, None, "/repo")
    assert remote.deleted[-1] == "d7" and shown()["library_state"] == "local"
    # (a) An account switch in the middle of a batch of deletes: the run stops, the rest stays queued
    # (a request under the new account would be believed, so none is sent).
    (docs / "more.md").write_text("# More\nsecond file")
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)               # two docs
    remote.fail_delete = True
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 2}
    remote.fail_delete, remote.after_delete = False, lambda: sign_in_as("user_2")
    gone = len(remote.deleted)
    kb.sync(db, source, None, "/repo")
    remote.after_delete = None
    # one DELETE went out; its answer raced the switch and is not believed, so both stay queued
    assert len(remote.deleted) == gone + 1 and shown(first)["library_removing"] == 2
    assert shown()["library_state"] == "local"
    sign_in_as("user_1")
    kb.sync(db, source, None, "/repo")      # the first DELETE is repeated (harmless), then the second
    assert len(remote.deleted) == gone + 3 and shown()["library_state"] == "local"
    # (b) Changed content while the first account is still in_library and the second is signed in:
    # nothing uploads.
    imports.add_to_library(db, source, "/repo", token(db, source), **deps)
    (docs / "notes.md").write_text("# Notes\nedited while another account is signed in")
    sent = len(remote.docs)
    sign_in_as("user_2")
    kb.sync(db, source, None, "/repo")
    assert len(remote.docs) == sent and shown(first)["library_state"] == "in_library"
