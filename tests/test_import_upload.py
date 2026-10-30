"""Adding an imported Knowledge folder to the library: secret-looking files are
never sent, nothing is sent when the switch is off, and a changed file re-uploads."""
import pytest

from brindle import airgap, kb
from brindle.pro import analysis_imports as imports
from brindle.pro import auth, credentials
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as


@pytest.fixture
def remote(backend):
    backend.docs, backend.deleted, backend.switches = [], [], {"import_upload": "on"}

    def store(form, headers):
        backend.docs.append(dict(form))
        doc_id = f"d{len(backend.docs)}"
        backend.routes[f"DELETE /analysis-docs/{doc_id}"] = lambda f, h: (backend.deleted.append(doc_id), (200, {}))[1]
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


def test_secret_looking_files_are_never_sent(folder, remote, deps):
    db, source, _ = folder
    plan = imports.dry_run(db, source)
    assert [f["name"] for f in plan["files"]] == ["notes.md"]
    assert {s["name"] for s in plan["skipped"]} == {"prod-secrets.md", "setup.md"}
    imports.add_to_library(db, source, "/repo", **deps)
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
        imports.add_to_library(db, source, "/repo", **deps)
    assert remote.docs == [] and not imports.in_library(db, source)


def test_a_changed_file_reuploads_and_removal_deletes(folder, remote, deps):
    db, source, docs = folder
    imports.add_to_library(db, source, "/repo", **deps)
    assert imports.sync_upload(db, source, "/repo", **deps)["uploaded"] == 0   # unchanged: nothing
    (docs / "notes.md").write_text("# Notes\nthe TTL is now an hour")
    assert imports.sync_upload(db, source, "/repo", **deps)["uploaded"] == 1
    assert remote.docs[-1]["files"][0]["md"].endswith("an hour") and remote.deleted == ["d1"]
    imports.remove_from_library(db, source, client=deps["client"], store=deps["store"])
    assert remote.deleted == ["d1", "d2"] and not imports.in_library(db, source)
