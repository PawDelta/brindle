"""scripts/bench_recovery.py times a restart's recovery on a copy of brindle's
home, with a stand-in for Claude Code only: no session it resumes may launch
another CLI for real, nor write where the real one keeps its work."""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from brindle.db import DB, Agent, Workspace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_recovery  # noqa: E402


@pytest.mark.parametrize("pipeline, refused", [
    ("fixing", True),       # sent back to work, not reported since: the resume launches it again
    (None, False),          # reported and done: nothing to launch, so the session can be timed
])
def test_a_session_with_another_cli_the_resume_would_launch_is_refused(db, brindle_home, tmp_path,
                                                                       pipeline, refused):
    repo, worktree = tmp_path / "repo", tmp_path / "wt"
    db.add_workspace(Workspace("ws-root", str(repo), "repo", "checkout", "main", None, str(repo), None,
                               "t-root", time.time()))
    db.add_workspace(Workspace("ws-w", str(repo), "w", "worktree", "feat", "main", str(worktree), None,
                               "t-w", time.time()))
    db.add_agent(Agent("root", "ws-root", "supervisor", "claude", None, "interactive", "idle", "%1", None,
                       time.time()))
    db.add_agent(Agent("w", "ws-w", "developer", "codex", "root", "assign", "done", "%2", "report", time.time()))
    db.update_agent("w", pipeline=pipeline, config_home="/real/codex-home")
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as target:
        db.conn.backup(target)
    db.conn.close()
    real = tmp_path / "real-home"
    real.mkdir()

    def prepare():
        bench_recovery.prepare(brindle_home, source, real, ["root"], "bench-test", None)

    if refused:
        with pytest.raises(SystemExit, match="w \\(codex\\)"):
            prepare()
        return
    prepare()
    copy = DB()
    try:
        worker, where = copy.get_agent("w"), copy.get_workspace("ws-w")
        # Nothing of the session points outside the copy, done agents' homes and workspaces included.
        assert Path(worker.config_home).is_relative_to(brindle_home)
        assert Path(where.path).is_relative_to(brindle_home) and Path(where.repo_root).is_relative_to(brindle_home)
    finally:
        copy.conn.close()
