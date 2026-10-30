"""Adding an imported Knowledge folder to the library: secret-looking files are
never sent, nothing is sent when the switch is off or the folder changed after
the preview, a changed file re-uploads, and failures are surfaced and retried."""
import pytest

from brindle import airgap, kb
from brindle.pro import analysis_imports as imports
from brindle.pro import auth, credentials
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as


@pytest.fixture
def remote(backend):
    backend.docs, backend.deleted, backend.switches = [], [], {"import_upload": "on"}
    backend.reject = backend.fail_delete = False

    def delete(doc_id):
        if backend.fail_delete:
            return 500, {}
        backend.deleted.append(doc_id)
        return 200, {}

    def store(form, headers):
        if backend.reject:
            return 500, {}
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
    imports.add_to_library(db, source, "/repo", plan["token"], **deps)
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
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    assert not imports.in_library(db, source) and remote.deleted == []     # both docs still queued
    remote.fail_delete = False
    assert imports.remove_from_library(db, source, client=deps["client"], store=deps["store"]) == {"left": 0}
    assert sorted(remote.deleted) == ["d1", "d2"]
