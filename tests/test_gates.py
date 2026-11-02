from brindle import gates, git, workspaces
from brindle.config import RepoConfig


def test_check_summary_fail_fast(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    cfg = RepoConfig(checks=["cheap", "expensive"])
    calls = []

    def run(db, ws, cmd, *args):
        calls.append(cmd)
        return cmd != "cheap", "failed" if cmd == "cheap" else "ok"

    monkeypatch.setattr(gates, "_run_queued", run)
    summary = gates.check_summary(db, ws, cfg)
    assert calls == ["cheap"]
    assert gates.summary_failed(summary)
    assert "`expensive` (skipped: earlier check failed)" in summary
    assert db.get_check(ws.id, gates.head(ws), "expensive") is None
    tree = git.out(["rev-parse", "HEAD^{tree}"], ws.path)
    assert db.get_tree_check(ws.repo_root, tree, "expensive") is None
    assert gates.run_checked(db, ws, "expensive", {}, cfg.check_timeout)[0]
    assert calls == ["cheap", "expensive"]
