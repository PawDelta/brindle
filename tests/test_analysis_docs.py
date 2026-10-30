"""The analysis-doc library client: what is uploaded after a merge, the local
copy deleted only after a 200, and the cases where nothing is sent."""
import pytest

from brindle import airgap
from brindle.pro import analysis_brief
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

SOURCE = {"kind": "task", "ref": "feat"}


def write_doc(repo_root, branch="feat"):
    d = analysis_brief.docs_dir(str(repo_root), branch)
    (d / "notes").mkdir(parents=True)
    (d / "summary.md").write_text("the TTL is too short")
    (d / "notes" / "raw.md").write_text("raw numbers")
    return d


@pytest.fixture
def remote(backend):
    backend.docs, backend.switches, backend.status = [], {}, 200

    def store(form, headers):
        backend.docs.append(dict(form))
        return backend.status, {"id": f"d{len(backend.docs)}", "version": 1}

    backend.routes["POST /analysis-docs"] = store
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    return backend


def uploader(tmp_path, remote, notices):
    store = credentials.FileStore(tmp_path / "pro")
    keys = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    return ad.AnalysisDocs(str(tmp_path), client=auth.Client(None, remote), store=store, key_store=keys,
                           identity=lambda: "a" * 40, notice=notices.append, start_thread=False), store


@pytest.mark.parametrize("status", [200, 500])
def test_merged_doc_uploads_and_local_copy_goes_only_after_a_200(status, tmp_path, remote, fixed_identity):
    notices = []
    folder = write_doc(tmp_path)
    (folder / "big.md").write_text("x" * (ad.MAX_FILE + 1))     # can't go whole: neither sent nor deleted
    up, store = uploader(tmp_path, remote, notices)
    login_as(remote, store, features=("analysis_docs",))
    remote.status = status
    up.submit(ad.collect(str(tmp_path), "feat", SOURCE))
    (folder / "summary.md").write_text("edited after it was read")  # not what was sent: kept
    assert up.flush()
    sent = remote.docs[0]
    assert sent["scope"] == {"kind": "user", "id": "user_1"} and sent["source"] == SOURCE
    assert sent["files"] == [{"name": "notes/raw.md", "md": "raw numbers"},
                             {"name": "summary.md", "md": "the TTL is too short"}]
    assert len(sent["repo_key"]) == 64 and len(notices) == 1
    assert sorted(p.name for p in folder.rglob("*.md")) == (
        ["big.md", "summary.md"] if status == 200 else ["big.md", "raw.md", "summary.md"])


@pytest.mark.parametrize("case", ["airgap", "unentitled", "off"])
def test_nothing_sent(case, tmp_path, remote, fixed_identity, monkeypatch):
    write_doc(tmp_path)
    up, store = uploader(tmp_path, remote, [])
    login_as(remote, store, features=("learning",) if case == "unentitled" else ("analysis_docs",))
    if case == "airgap":
        monkeypatch.setattr(airgap, "enabled", lambda cfg=None: True)
    if case == "off":
        remote.switches = {"store": "off"}
    up.submit(ad.collect(str(tmp_path), "feat", SOURCE))
    up.flush()
    assert remote.docs == []


@pytest.mark.parametrize("case", ["airgap", "unentitled"])
def test_docs_cli_sends_nothing(case, tmp_path, remote, monkeypatch):
    import json

    from typer.testing import CliRunner

    from brindle import git
    from brindle.cli import app

    sent = []
    monkeypatch.setattr(git, "main_repo_root", lambda path: str(tmp_path))
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, *a: sent.append(a) or (200, {}))
    monkeypatch.setattr(airgap, "_armed", False)
    login_as(remote, credentials.default_store(), features=("learning",))
    if case == "airgap":
        login_as(remote, credentials.default_store(), features=("analysis_docs",))
        (tmp_path / ".brindle").mkdir()
        (tmp_path / ".brindle" / "config.json").write_text(json.dumps({"airgap": True}))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["docs", "show", "doc1"])
    assert result.exit_code == 1 and sent == []


@pytest.mark.parametrize("gate_ok, uploaded", [(True, 1), (False, 0)])
def test_merge_uploads_the_branch_doc_only_when_it_merges(
        gate_ok, uploaded, db, remote, tmp_path, fixed_identity, monkeypatch):
    from brindle import codemap, codemap_docs, gates, git, pipeline, workspaces
    from brindle.db import Workspace

    write_doc(tmp_path, "feat/a")
    other = write_doc(tmp_path, "feat-a")      # a branch whose name differs only in "/": its own folder
    (tmp_path / "wt").mkdir()
    ws = Workspace("w1", str(tmp_path), "feat", "branch", "feat/a", "main", str(tmp_path / "wt"), None, "t", 0.0)
    db.add_workspace(ws)
    up, store = uploader(tmp_path, remote, [])
    login_as(remote, store, features=("analysis_docs",))
    monkeypatch.setitem(ad._instances, ws.repo_root, up)
    monkeypatch.setattr(git, "ahead_behind", lambda path, ref: (0, 1))
    monkeypatch.setattr(gates, "head", lambda w: "abc123")
    monkeypatch.setattr(workspaces, "sync_with_base", lambda w: workspaces.SyncResult("up_to_date"))
    monkeypatch.setattr(gates, "run", lambda *a, **k: gates.Report(sha="abc123") if gate_ok
                        else gates.Report().fail("checks failed"))
    monkeypatch.setattr(workspaces, "merge_back", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(codemap, "refresh_later", lambda *a, **k: None)
    monkeypatch.setattr(codemap_docs, "docs_refresh_note", lambda *a, **k: None)
    out = pipeline.merge(db, None, ws)
    up.flush()
    assert out.startswith("Merged" if gate_ok else "Not merged") and len(remote.docs) == uploaded
    assert len(list(other.rglob("*.md"))) == 2


@pytest.mark.parametrize("status", [200, 403])
def test_docs_export_writes_only_after_a_200(status, tmp_path, remote, monkeypatch):
    import base64

    from typer.testing import CliRunner

    from brindle import git
    from brindle.cli import app

    monkeypatch.setattr(git, "main_repo_root", lambda path: str(tmp_path))
    monkeypatch.setattr(auth.UrllibTransport, "request", lambda self, m, url, form, headers: (
        remote.request(m, url, form, headers)))
    login_as(remote, credentials.default_store(), features=("analysis_docs",))
    body = {"filename": "doc1.zip", "content_b64": base64.b64encode(b"PK").decode()}
    remote.routes["GET /analysis-docs/doc1/export?format=zip&destination=download"] = (
        [(200, body) if status == 200 else (403, {"error": "destination_not_allowed"})])
    out = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["docs", "export", "doc1", "--out", str(out)])
    assert (result.exit_code, out.exists() and (out / "doc1.zip").read_bytes() == b"PK") == (
        (0, True) if status == 200 else (1, False))
