import time

import pytest

from brindle import db as db_module
from brindle.db import DB, Task, Workspace, _columns

# Columns of every table v0.5.12's schema and migrations declare (frozen from
# `git show v0.5.12:src/brindle/db.py`). A released brindle refuses its MCP
# tools when a table it declares has a column it doesn't (migrated_by_newer),
# so a new version may add tables but never columns to these. Never edit this
# to make a test pass.
V0_5_12 = {
    "agent_reported_spend": "agent_id reported_usd updated_at",
    "agents": "config_home created_at dismissed_at done_when failed_why headless hooks_off id inbox_socket inbox_token merge_failed mode name parent_id pipeline pipeline_rounds plan_first plan_state plan_text profile provider reopened_at result review_restarted review_sha review_slot reviewing_since session_ref status status_since stop_blocked stop_reminded_at stuck_noted task tmux_server tmux_window transcript_path unreported_noted weight workspace_id",
    "analysis_drafts": "branch doc_id drafter session state workspace_id",
    "autopilot": "audit_history audit_ok audit_sha auditor_id checking_since created_at detail enabled external_wait goal goal_sha goals_file note nudged_at nudged_time nudges progress proposal question root_id state usage_paused_ids usage_resets_at",
    "check_cache": "command created_at ok output sha workspace_id",
    "check_durations": "command repo_root seconds updated_at",
    "check_tree_cache": "command created_at ok output repo_root tree",
    "conflict_notices": "created_at files other_id workspace_id",
    "context_receipts": "content_hash item_id item_kind task_id",
    "decisions": "author_session body created_at id link rationale repo_key scope status supersedes topic",
    "doc_requests": "agent_id created_at doc_id id section title why",
    "events": "agent_id id kind payload repo_root root_id task_id ts workspace_id",
    "history": "agent_id branch id kind profile repo_root result task tokens ts",
    "history_usage_mark": "agent_id cache_creation_tokens cache_read_tokens input_tokens output_tokens transcript_path",
    "inbox": "agent_id body created_at delivered_at held id noticed_at sender_id supervisor_context",
    "intents": "paths repo_key session_root status summary updated_at",
    "kb_chunks": "doc_id heading_path id quarantined text",
    "kb_docs": "content_hash external_id id source_id title tombstoned updated_at url",
    "kb_embeddings": "chunk_id model vector",
    "kb_events": "actor id kind machine payload repo_key session ts",
    "kb_import_library": "added_at identity op op_token op_until pending source_id state",
    "kb_import_removals": "identity source_id",
    "kb_import_uploads": "content_hash doc_id external_id identity source_id",
    "kb_links": "chunk_id node_id symbol",
    "kb_sources": "added_at credential_name id kind mode repo_key retention_days scope sync_cursor visibility",
    "launch_health": "agent_id argv_task baseline delivered_at delivery_lead fingerprint launched_at outcome pane submitted_prompt token verifying",
    "learned_rules": "branches checks created_at decided_at findings key profile repo_root rule status title",
    "learned_rules_mark": "last_id repo_root",
    "login_state": "account agent_id checked_at delay expired_at interrupted next_check noticed refresh_cmd root_id state",
    "merges": "merged_at sha workspace_id",
    "milestones": "check_cmd checked_at checked_sha detail ever_passed id output passed_sha position profile repo root_id state state_why status title",
    "native_subagents": "agent_type ended_at id parent_id started_at",
    "permission_asks": "agent_id asked id kind outcome reason repo_root request_id resolved_by summary ts wait_seconds",
    "permission_requests": "agent_id answer channel created_at hold_until reason request suggestions tool_use_id",
    "pool_entries": "base_branch base_sha branch created_at fingerprint path port_base ready repo_root",
    "pool_failures": "base_branch failed_at repo_root",
    "review_claims": "agent_id claimed_at sha slot workspace_id",
    "review_edits": "sha workspace_id",
    "review_mcp_retries": "sha workspace_id",
    "reviews": "approved created_at id review_slot reviewer_created_at reviewer_id sha summary workspace_id",
    "routing_decisions": "agent_id baseline_profile budget_state demoted_from escalations id learned outcome prior profile repo_root review_rounds seeded_at task_id ts weight",
    "session_credentials": "key session_id updated_at value",
    "session_pr_prep": "branch check_cmd checks output prepared_at root_id sha state title",
    "session_prs": "auto branch merged_at opened_at pushed_sha root_id url",
    "session_repos": "added_at alias repo_root root_id",
    "sidebars": "pane root_id updated_at",
    "stage_open": "stage ts who workspace_id",
    "stage_timings": "day median_s n p90_s repo_root samples stage",
    "startup_prompts": "agent_id calls launched_at pane pending",
    "tasks": "agent_id analysis_doc branch caller_id caller_ws_id created_at depends_on done_when files forced id isolate mode pipeline plan_first profile repo_root started_at state task_text waiting waiting_since weight",
    "usage_cache": "cache_creation_tokens cache_read_tokens inode input_tokens last_message_id model output_tokens path size updated_at",
    "workspaces": "base_branch branch created_at id kind name path port_base repo_root session_root tmux_session",
}


@pytest.mark.parametrize("interleaved", [True])
def test_session_repo_branch_column_moves_out_and_no_released_table_gains_a_column(
        tmp_path, monkeypatch, interleaved):
    """A development database has the branch on session_repos: opening it
    copies the branch to its own table and drops the column, even when another
    process finishes that move between this one's check and its transaction.
    Afterwards no table v0.5.12 declares has an extra column."""
    path = str(tmp_path / "state.db")
    db = DB(path)
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute("ALTER TABLE autopilot ADD COLUMN summary TEXT")
    db.conn.execute("INSERT INTO autopilot (root_id, created_at, summary) VALUES ('root', 1, 'CLI polish')")
    db.conn.execute("DROP TABLE session_repo_branches")
    db.conn.execute("ALTER TABLE session_repos ADD COLUMN branch TEXT")
    db.conn.execute("INSERT INTO session_repos VALUES ('root','a','/a',1,'feat')")
    db.conn.execute("INSERT INTO session_repos VALUES ('root','b','/b',2,NULL)")
    db.conn.execute("CREATE TABLE session_repo_branches (root_id TEXT NOT NULL, alias TEXT NOT NULL, "
                    "branch TEXT NOT NULL, PRIMARY KEY (root_id, alias))")
    db.conn.close()

    real_tx = DB.tx
    moved = []

    def tx_after_other_process(self):
        # Another process opens, and so moves, after this one saw the column.
        if not moved:
            moved.append(1)
            DB(path).conn.close()
        return real_tx(self)

    if interleaved:
        monkeypatch.setattr(DB, "tx", tx_after_other_process)
    db = DB(path)
    assert bool(moved) == interleaved
    assert db.get_autopilot("root").summary == "CLI polish"
    assert [(r.alias, r.branch) for r in db.session_repos("root")] == [("a", "feat"), ("b", None)]
    now = _columns(db.conn)
    assert {t: sorted(now[t] - set(cols.split())) for t, cols in V0_5_12.items()
            if t in now and now[t] - set(cols.split())} == {}


@pytest.mark.parametrize("interleaved", [True])
def test_task_wait_noted_column_moves_to_task_notes_keeping_claims(tmp_path, monkeypatch, interleaved):
    """A development database has tasks.wait_noted: opening it copies the
    timestamps to task_notes and drops the column, even when another process
    finishes that move between this one's check and its transaction. A task
    already noted cannot claim its notice again; an unnoted one can."""
    path = str(tmp_path / "state.db")
    db = DB(path)
    db.conn.execute("ALTER TABLE tasks ADD COLUMN wait_noted REAL")
    for tid, noted in (("t1", 5.0), ("t2", None)):
        db.conn.execute("INSERT INTO tasks (id, repo_root, caller_ws_id, profile, task_text, mode, "
                        "isolate, state, created_at, wait_noted) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tid, "/r", "w", "p", "x", "m", 1, "pending", 1.0, noted))
    db.conn.close()

    real_tx = DB.tx
    moved = []

    def tx_after_other_process(self):
        if not moved:
            moved.append(1)
            DB(path).conn.close()
        return real_tx(self)

    if interleaved:
        monkeypatch.setattr(DB, "tx", tx_after_other_process)
    db = DB(path)
    assert bool(moved) == interleaved
    assert "wait_noted" not in _columns(db.conn)["tasks"]
    assert [tuple(r) for r in db.conn.execute("SELECT task_id, kind, noted FROM task_notes")] == [
        ("t1", "wait", 5.0)]
    assert not db.claim_wait_note("t1", 9.0)
    assert db.claim_wait_note("t2", 9.0)
    assert not db.claim_wait_note("t2", 10.0)


@pytest.mark.variant("live incident 2026-10-30: an app update added columns and open sessions' inserts failed")
def test_an_older_engine_writes_and_reads_rows_a_newer_engine_added_columns_to(tmp_path, monkeypatch):
    """A newer brindle migrates the shared database while an older one (a
    session's MCP server, say) keeps running against it: every write names its
    columns and every read goes by column name, so the extra columns are no
    concern of the older code's."""
    path = tmp_path / "brindle.db"
    newer = DB(path)
    for table in ("workspaces", "tasks", "review_claims"):
        newer.conn.execute(f"ALTER TABLE {table} ADD COLUMN from_a_newer_engine TEXT")
    newer.conn.close()

    # Helpers launched from a development worktree use the released engine.
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 1)
    db = DB(path)
    ws = Workspace("ws1", "/repo", "w", "worktree", "feat/x", "main", "/repo/w", None, "s", time.time())
    db.add_workspace(ws)
    db.add_task(Task("t1", "/repo", None, None, "ws1", "developer", "do it", "assign", 1, "feat/x",
                     None, None, None, "pending", time.time()))
    assert db.claim_review("ws1", "abc", "review") is not None

    assert db.get_workspace("ws1") == ws
    assert db.get_task("t1").task_text == "do it"
