import time

from brindle.db import DB, Task, Workspace


def test_an_older_engine_writes_and_reads_rows_a_newer_engine_added_columns_to(tmp_path):
    """A newer brindle migrates the shared database while an older one (a
    session's MCP server, say) keeps running against it: every write names its
    columns and every read goes by column name, so the extra columns are no
    concern of the older code's."""
    path = tmp_path / "brindle.db"
    newer = DB(path)
    for table in ("workspaces", "tasks", "review_claims"):
        newer.conn.execute(f"ALTER TABLE {table} ADD COLUMN from_a_newer_engine TEXT")
    newer.conn.close()

    db = DB(path)
    ws = Workspace("ws1", "/repo", "w", "worktree", "feat/x", "main", "/repo/w", None, "s", time.time())
    db.add_workspace(ws)
    db.add_task(Task("t1", "/repo", None, None, "ws1", "developer", "do it", "assign", 1, "feat/x",
                     None, None, None, "pending", time.time()))
    assert db.claim_review("ws1", "abc", "review") is not None

    assert db.get_workspace("ws1") == ws
    assert db.get_task("t1").task_text == "do it"
