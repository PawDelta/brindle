"""A released brindle refuses its MCP tools when a table it declares has a
column it doesn't (db.migrated_by_newer), so a new version may add tables but
never columns to the ones v0.5.12 declares."""

from brindle.db import DB, _columns

# Columns of every table v0.5.12's schema and migrations declare (frozen from
# `git show v0.5.12:src/brindle/db.py`). Never edit this to make the test pass.
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


def test_no_released_table_gains_a_column():
    db = DB(":memory:")
    now = _columns(db.conn)
    grown = {t: sorted(now[t] - set(cols.split())) for t, cols in V0_5_12.items()
             if t in now and now[t] - set(cols.split())}
    assert grown == {}


def test_branch_column_migrates_to_its_own_table(tmp_path):
    path = str(tmp_path / "state.db")
    db = DB(path)
    db.conn.execute("PRAGMA foreign_keys=OFF")
    # A development database as main left it: the branch on session_repos.
    db.conn.execute("DROP TABLE session_repo_branches")
    db.conn.execute("ALTER TABLE session_repos ADD COLUMN branch TEXT")
    db.conn.execute("INSERT INTO session_repos VALUES ('root','a','/a',1,'feat')")
    db.conn.execute("INSERT INTO session_repos VALUES ('root','b','/b',2,NULL)")
    db.conn.close()

    db = DB(path)
    assert "branch" not in _columns(db.conn)["session_repos"]
    assert [(r.alias, r.branch) for r in db.session_repos("root")] == [("a", "feat"), ("b", None)]
