import pytest

from brindle import codemap, gates, git, pipeline, policy, workspaces


@pytest.mark.parametrize("changed,doc_touched", [
    (["docs/readme.md", "src/app.py"], True),
    (["src/app.py"], False),
])
def test_merge_docs_refresh_note(db, repo, monkeypatch, changed, doc_touched):
    (repo / "graphify-out").mkdir(exist_ok=True)
    (repo / "graphify-out" / "graph.json").write_text("{}")
    monkeypatch.setattr(codemap.shutil, "which", lambda cmd: f"/bin/{cmd}")

    ws = workspaces.create(db, str(repo), "feat-refresh").workspace
    monkeypatch.setattr(policy, "check_merge", lambda *a, **k: policy.Decision(True))
    monkeypatch.setattr(git, "ahead_behind", lambda *a, **k: (0, 0))
    monkeypatch.setattr(workspaces, "sync_with_base", lambda ws_: workspaces.SyncResult("up_to_date"))
    monkeypatch.setattr(gates, "run", lambda *a, **k: gates.Report(ok=True, sha="h1", passed=["ok"]))
    monkeypatch.setattr(gates, "head", lambda ws_: "h1")
    monkeypatch.setattr(workspaces, "merge_back", lambda *a, **k: str(repo))
    monkeypatch.setattr(codemap, "refresh_later", lambda *a, **k: None)
    monkeypatch.setattr(git, "paths", lambda *a, **k: changed)

    res = pipeline._merge(db, None, ws, squash=False)
    if doc_touched:
        assert "docs/readme.md" in res and "assign" in res
        assert "src/app.py" not in res.partition("Docs changed")[2]
    else:
        assert "assign" not in res
