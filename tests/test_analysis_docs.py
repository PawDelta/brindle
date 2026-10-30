"""The analysis-doc library client: what is uploaded after a merge, and the
cases where nothing is sent."""
import pytest

from brindle import airgap
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

MAIN = """---
title: Cache hit rate
question: Why is the cache slow?
---
## Results
- hit rate fell to 41% [verified]
- no change in eviction [not checked]

## Conclusion
- the TTL is too short [inference]
"""


@pytest.fixture
def deliv_repo(tmp_path):
    d = tmp_path / ".brindle" / "deliverables" / "cache"
    (d / "appendices").mkdir(parents=True)
    (d / "main.md").write_text(MAIN)
    (d / "appendices" / "a.md").write_text("raw numbers")
    return tmp_path


@pytest.fixture
def remote(backend):
    backend.docs, backend.switches = [], {}

    def store(form, headers):
        backend.docs.append(dict(form))
        return 200, {"id": f"d{len(backend.docs)}", "version": 1}

    backend.routes["POST /analysis-docs"] = store
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    return backend


def uploader(tmp_path, remote, notices, **kw):
    store = credentials.FileStore(tmp_path / "pro")
    keys = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    return ad.AnalysisDocs(str(tmp_path), client=auth.Client(None, remote), store=store, key_store=keys,
                           identity=lambda: "a" * 40, protected=kw.pop("protected", lambda files: []),
                           notice=notices.append, start_thread=False), store


def test_merged_deliverable_uploads_once_with_notice(deliv_repo, remote, fixed_identity):
    notices = []
    up, store = uploader(deliv_repo, remote, notices)
    login_as(remote, store, features=("analysis_docs",))
    docs = ad.collect(str(deliv_repo), [".brindle/deliverables/cache/main.md", ".brindle/deliverables/cache/appendices/a.md"])
    assert [d.slug for d in docs] == ["cache"]          # one doc per slug, however many files changed
    up.submit(docs + docs)
    assert up.flush()
    assert len(remote.docs) == 2 and len(notices) == 1
    sent = remote.docs[0]
    assert sent["scope"] == {"kind": "user", "id": "user_1"} and sent["title"] == "Cache hit rate"
    assert sent["appendices"] == [{"name": "a.md", "md": "raw numbers"}]
    assert sent["meta"]["metrics"] == ["hit rate fell to 41% [verified]"]
    assert sent["meta"]["labels"] == {"verified": 1, "inference": 1, "not_checked": 1}
    assert len(sent["repo_key"]) == 64


@pytest.mark.parametrize("case", ["airgap", "protected", "unentitled", "off"])
def test_nothing_sent(case, deliv_repo, remote, fixed_identity, monkeypatch):
    protected = (lambda files: files) if case == "protected" else (lambda files: [])
    up, store = uploader(deliv_repo, remote, [], protected=protected)
    login_as(remote, store, features=("learning",) if case == "unentitled" else ("analysis_docs",))
    if case == "airgap":
        monkeypatch.setattr(airgap, "enabled", lambda cfg=None: True)
    if case == "off":
        remote.switches = {"store": "off"}
    up.submit(ad.collect(str(deliv_repo), [".brindle/deliverables/cache/main.md"]))
    up.flush()
    assert remote.docs == []


def test_changed_files_finds_every_slug_and_skips_symlinks(tmp_path, monkeypatch):
    import os

    from brindle import git

    for slug in ("one", "two"):
        d = tmp_path / ".brindle" / "deliverables" / slug
        (d / "appendices").mkdir(parents=True)
        (d / "main.md").write_text(MAIN.replace("question: Why is the cache slow?",
                                                "question: >\n  Why is\n  the cache slow?"))
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected" / "secret.md").write_text("secret")
    os.symlink("../../../../protected/secret.md", tmp_path / ".brindle/deliverables/two/appendices/s.md")
    monkeypatch.setattr(git, "paths", lambda args, cwd: [
        "protected/secret.md", "src/x.py", *(f".brindle/deliverables/{s}/{f}" for s in ("one", "two")
                                             for f in ("main.md", "appendices/s.md"))])
    changed = ad.changed_files(str(tmp_path), "HEAD~1")
    docs = {d.slug: d for d in ad.collect(str(tmp_path), changed)}
    assert sorted(docs) == ["one", "two"]
    assert docs["two"].appendices == [] and "protected" not in "".join(docs["two"].files)
    assert docs["one"].meta["question"] == "Why is the cache slow?"


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


@pytest.mark.parametrize("gate_ok, uploaded", [(True, 2), (False, 0)])
def test_merge_uploads_every_changed_deliverable_only_when_it_merges(
        gate_ok, uploaded, db, remote, tmp_path, fixed_identity, monkeypatch):
    from brindle import codemap, codemap_docs, gates, git, pipeline, workspaces
    from brindle.db import Workspace

    wt = tmp_path / "wt"
    names = []
    for slug in ("one", "two"):
        d = wt / ".brindle" / "deliverables" / slug
        (d / "appendices").mkdir(parents=True)
        (d / "main.md").write_text(MAIN)
        (d / "appendices" / "a.md").write_text("raw")
        names.append(f".brindle/deliverables/{slug}/main.md")
    ws = Workspace("w1", str(tmp_path), "feat", "branch", "feat", "main", str(wt), None, "t", 0.0)
    db.add_workspace(ws)
    up, store = uploader(tmp_path, remote, [])
    login_as(remote, store, features=("analysis_docs",))
    monkeypatch.setitem(ad._instances, ws.repo_root, up)
    monkeypatch.setattr(git, "base_ref", lambda path, base: base)
    monkeypatch.setattr(git, "ahead_behind", lambda path, ref: (0, 1))
    monkeypatch.setattr(ad, "changed_files", lambda path, base: names)
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
