"""SQLite state shared by the CLI, hook handlers, and every agent's MCP server.

Several processes touch the database at once (one MCP server per agent plus
hook invocations), so it runs in WAL mode and every write is a short
transaction.
"""

from __future__ import annotations

import functools
import json
import re
import signal
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, fields
from typing import Callable, Iterator

from brindle import bus
from brindle.config import db_path
from brindle.redact import mask

# Claude Code's own subagents (native_subagents table, see below). A crash
# can skip SubagentStop, so a subagent still "running" past this age is
# treated as crashed: view.py hides it from the sidebar, and
# start_native_subagent prunes it here. view.py's own NATIVE_SUBAGENT_LINGER
# (how long a *finished* one keeps showing "done") sits next to this concern
# but is a display-only choice, so it stays in view.py.
NATIVE_SUBAGENT_STALE = 2 * 3600
# Ended rows are dropped from the table entirely after this long.
NATIVE_SUBAGENT_PRUNE_AFTER = 3600

# SQLite user_version: 0 identifies databases written before version tracking.
# Bump for incompatible SCHEMA or _migrate changes so older engines refuse the result.
# Additive tables stay compatible with installed helpers launched from worktrees.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'worktree' (brindle-managed) or 'main' (existing checkout)
    branch TEXT NOT NULL,
    base_branch TEXT,
    path TEXT NOT NULL,
    port_base INTEGER,
    tmux_session TEXT NOT NULL,
    created_at REAL NOT NULL,
    session_root TEXT,
    UNIQUE (repo_root, name)
);
-- A suggested review cap is announced once for the branch's workspace,
-- including when a replacement worker starts immediately or from the queue.
CREATE TABLE IF NOT EXISTS review_cap_notices (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    provider TEXT NOT NULL,
    parent_id TEXT,
    mode TEXT NOT NULL,            -- 'interactive', 'handoff', or 'assign'
    status TEXT NOT NULL,
    tmux_window TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    status_since REAL,             -- when status last changed
    task TEXT,                     -- the prompt it was started with (for resuming)
    session_ref TEXT,              -- the CLI's own session id (claude --resume)
    stop_blocked INTEGER,          -- brindle's Stop hook just kept it going (for CLIs that don't say)
    headless INTEGER,              -- runs `claude -p` turn by turn (agents.run_headless)
    transcript_path TEXT,          -- Claude Code's own JSONL transcript for session_ref (brindle.usage)
    done_when TEXT,                -- the finish line it was given, if any (for review context)
    dismissed_at REAL,             -- closed from the sidebar (`brindle close`): hidden there for good
    inbox_socket TEXT,             -- Claude Code's inbox for this session (brindle.inbox)
    inbox_token TEXT,
    pipeline TEXT,                 -- a worker's branch in brindle's hands: 'reviewing' or 'fixing'
    pipeline_rounds INTEGER,
    stuck_noted REAL,              -- status_since of the 'waiting' spell its supervisor was told about (brindle.cull)
    review_sha TEXT,               -- the commit a reviewer was started on (agents.request_review)
    unreported_noted INTEGER,      -- its supervisor was told it stopped without reporting (agents.tell_parent_unreported)
    stop_reminded_at REAL,         -- a Stop last reminded it of unread mail (DB.claim_stop_reminder)
    tmux_server TEXT,              -- the private tmux server (BRINDLE_TMUX_SOCKET) its window is on; NULL: the default server
    reopened_at REAL,              -- when a dead reviewer's session was reopened once (pipeline.on_reviewer_unreported)
    hooks_off TEXT,                -- why its CLI runs none of brindle's hooks, shown on its sidebar row (agents.screen_status)
    reviewing_since REAL,          -- when its last report put its branch in review (pipeline.on_report)
    review_restarted REAL,         -- the wait in review whose dead check run was started again (pipeline.restart_review)
    merge_failed TEXT,             -- why the pipeline couldn't merge its approved branch (pipeline.review_state)
    config_home TEXT,              -- the CLI config home it runs with, decided once at spawn (provider_homes): '' the person's own
    review_slot TEXT,              -- the review claim slot a reviewer was started in (gates.standing_reviews)
    failed_why TEXT,               -- why its status is 'failed': auth | model | delivery (service.inbox)
    name TEXT                      -- a session root's name, given by the person (POST /sessions/{root}/rename)
);
-- The milestone current when a worker was assigned (agents.spawn). A table of
-- its own: released tables never gain columns (see session_repo_branches).
CREATE TABLE IF NOT EXISTS agent_milestones (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    title TEXT NOT NULL
);
-- The goal milestone a queued task's worker is assigned to (assign/handoff's milestone); a table of its
-- own so no released table gains a column.
CREATE TABLE IF NOT EXISTS task_milestones (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    sender_id TEXT,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL,
    held INTEGER,                  -- 1: a pull-mode message, read with read_messages instead of being delivered
    noticed_at REAL,               -- when a "new messages" notice covering it went out
    supervisor_context INTEGER    -- authenticated before a handover changed the recipient's ancestry
);
-- Autopilot: one row per session (keyed by its supervisor) that has it on.
CREATE TABLE IF NOT EXISTS autopilot (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    goal TEXT,                     -- NULL until the user says what we're building
    detail TEXT,
    state TEXT NOT NULL DEFAULT 'running',  -- running | blocked | stalled | usage_paused | auditing | done
    note TEXT,                     -- why it's blocked or stalled
    progress INTEGER NOT NULL DEFAULT 0,    -- bumped whenever real progress happens
    nudges INTEGER NOT NULL DEFAULT 0,      -- "keep going" nudges since the last progress
    nudged_at INTEGER,             -- the progress count at the last nudge
    checking_since REAL,           -- a milestone check is running in the background
    usage_resets_at REAL,          -- when state is usage_paused: the usage window's reset time
    usage_paused_ids TEXT,         -- JSON list of the workers stopped for usage
    created_at REAL NOT NULL,
    goals_file TEXT,               -- the goals.md the goal was loaded from; milestone status is synced back to it
    goal_sha TEXT,                 -- the checkout's HEAD when the goal was set: the completion audit reviews from here
    auditor_id TEXT,               -- the reviewer auditing the finished goal (state 'auditing')
    audit_sha TEXT,                -- the commit the last completion audit judged
    audit_ok INTEGER,              -- its verdict: 1 approved, 0 gaps found
    audit_history TEXT,            -- persistent audit notes and prior accepted decisions
    question TEXT,                 -- the supervisor's open need_user question (held while note matches)
    nudged_time REAL,              -- when the last nudge was sent
    proposal TEXT,                 -- JSON: checks awaiting the user's answer in brindle's dialog
    external_wait TEXT             -- JSON {pid, until, path, note}: an outside job
);
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id TEXT NOT NULL REFERENCES autopilot(root_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    check_cmd TEXT,                -- brindle runs this itself; exit 0 means done
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | passed | failed
    checked_at REAL,
    output TEXT,                   -- the tail of the last check's output
    checked_sha TEXT,              -- the checkout's HEAD when it was last checked
    passed_sha TEXT,               -- the checkout's HEAD when it last passed
    profile TEXT,                  -- default worker profile for assign
    repo TEXT,                     -- alias of the attached repo the check runs in (brindle.repos); NULL: the session's own
    state TEXT,                    -- last check: passed | not_done | regressed | failed (couldn't run); NULL: never checked
    state_why TEXT,                -- for failed: why the check couldn't run
    ever_passed INTEGER            -- 1 once any check passed (kept through later failures)
);
-- The phase a milestone sits in, when its goal names phases (goals.md `phase:`,
-- set_goal's "phase"). A table of its own, not a column of milestones: released
-- tables never gain columns (see session_repo_branches below).
CREATE TABLE IF NOT EXISTS milestone_phases (
    milestone_id INTEGER PRIMARY KEY REFERENCES milestones(id) ON DELETE CASCADE,
    phase TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase_landings (
    root_id TEXT NOT NULL REFERENCES autopilot(root_id) ON DELETE CASCADE,
    phase TEXT NOT NULL,           -- empty for a goal without phases
    notified_at REAL,
    pending INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (root_id, phase)
);
-- A completed goal phase brindle lands itself while later milestones remain
-- (autopilot.phase_landing; routes_actions.land_phase_prs opens the session PR
-- or pushes to the open one). sha: the commit the phase's milestone checks
-- passed on, the one pushed (never the branch's tip). state: pending | pushed |
-- refused | failed; why: what refused or failed it.
CREATE TABLE IF NOT EXISTS phase_prs (
    root_id TEXT NOT NULL REFERENCES autopilot(root_id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    sha TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    why TEXT,
    PRIMARY KEY (root_id, phase)
);
-- Short goal labels live outside the released autopilot table.
CREATE TABLE IF NOT EXISTS goal_summaries (
    root_id TEXT PRIMARY KEY REFERENCES autopilot(root_id) ON DELETE CASCADE,
    summary TEXT NOT NULL
);
-- brindle's per-goal offer to plan first, outside the released autopilot table
-- (requested: the retired analysis-doc request, kept for compatibility).
CREATE TABLE IF NOT EXISTS goal_doc_requests (
    root_id TEXT PRIMARY KEY REFERENCES autopilot(root_id) ON DELETE CASCADE,
    requested INTEGER NOT NULL,
    offer TEXT,                 -- brindle's one-time offer to plan first: plan:open:<id> | plan:accepted | plan:planned | plan:dismissed
                                -- (open:<id> | accepted | dismissed: the retired analysis-doc offer; inert)
    goal_at REAL                -- when this goal began: only workers assigned since count toward its offer
);
-- Repos attached to a session (brindle Pro multi_repo; see brindle.repos):
-- assign/handoff may put workers there by alias.
CREATE TABLE IF NOT EXISTS session_repos (
    root_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (root_id, alias)
);
-- The session's own branch in an attached repo, its workers' merge target. A
-- table of its own, not a column of session_repos: an older brindle refuses
-- its MCP tools when a table it declares has columns it doesn't (see
-- migrated_by_newer), so released tables never gain columns.
CREATE TABLE IF NOT EXISTS session_repo_branches (
    root_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    branch TEXT NOT NULL,
    PRIMARY KEY (root_id, alias),
    FOREIGN KEY (root_id, alias) REFERENCES session_repos(root_id, alias) ON DELETE CASCADE
);
-- One-shot notices about a task (kind 'wait': its supervisor was told it waits
-- long for room). A table of its own, not a column of tasks: see above.
CREATE TABLE IF NOT EXISTS task_notes (
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    noted REAL NOT NULL,
    PRIMARY KEY (task_id, kind)
);
-- Per-agent marks kept apart from agents (a released table gains no column):
-- kind 'commits' is what its supervisor was told had unreported commits
-- (cull.note_unreported_commits).
CREATE TABLE IF NOT EXISTS agent_marks (
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (agent_id, kind)
);
-- A worker's question or stall its supervisor hasn't dealt with (brindle.open_items):
-- one row per worker, deleted when it closes. level: the notices sent past the
-- first. retiring_at: a sweep is taking the worker over.
CREATE TABLE IF NOT EXISTS open_items (
    worker_id TEXT PRIMARY KEY,
    supervisor_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- question | stalled
    detail TEXT NOT NULL,
    opened_at REAL NOT NULL,
    stop_at REAL NOT NULL,         -- the status_since of the stop it is about
    level INTEGER NOT NULL DEFAULT 0,
    retiring_at REAL
);
CREATE INDEX IF NOT EXISTS open_items_supervisor ON open_items(supervisor_id);
-- Every report_result call, from receipt to delivery (agents.report_result):
-- state received -> accepted (promoted into agents.result) -> delivered (the
-- supervisor's inbox row or the review's claim written in that same
-- transaction), or rejected (agents.result never touched), or superseded (by a
-- newer delivered report, or folded into canonical_id). Every transition is a
-- compare-and-set on the state; rows are never deleted. A report is (agent, summary digest, branch head once
-- read): the same summary at a new head is a new report.
CREATE TABLE IF NOT EXISTS report_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    head TEXT,                     -- NULL until the branch head is read and bound (outbox_bind)
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    is_first INTEGER NOT NULL,
    state TEXT NOT NULL,
    delivery TEXT,                 -- the inbox message or review claim delivering it (outbox_deliver)
    fallback TEXT,                 -- failed review start, pending durable supervisor delivery
    canonical_id INTEGER,          -- the row a consolidated receipt was folded into (outbox_bind)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (agent_id, head, digest)
);
CREATE TABLE IF NOT EXISTS session_credentials (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, key)
);
-- A reviewer agent's verdict on a branch at one commit. A merge gate only
-- accepts an approval of the commit it is about to merge.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    reviewer_id TEXT,
    review_slot TEXT,
    reviewer_created_at REAL,
    approved INTEGER NOT NULL,
    summary TEXT,
    created_at REAL NOT NULL
);
-- The sidebar reads each workspace's latest review on every refresh (last_review).
CREATE INDEX IF NOT EXISTS reviews_workspace_id ON reviews(workspace_id, id);
-- The commit a workspace's branch was merged into its base at, so a repeated
-- merge of the same commit is a no-op (see pipeline.merge).
CREATE TABLE IF NOT EXISTS merges (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    merged_at REAL NOT NULL
);
-- A check command's PASSING result for one git tree in one repo, whichever
-- workspace (or the supervisor's checkout after a merge) it ran in: the same
-- tree is the same code. Written next to check_cache (see gates.run_checked).
CREATE TABLE IF NOT EXISTS check_tree_cache (
    repo_root TEXT NOT NULL,
    tree TEXT NOT NULL,
    command TEXT NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (repo_root, tree, command)
);
-- A check command's PASSING result at one commit, so gates.run and
-- request_review don't re-run the same command against the same tree. Only
-- written when the tree was clean before and after the run (see
-- gates.run_checked); failures are never cached, so a flaky or broken check
-- always gets a fresh run. "Clean" is `git status --porcelain`, which does
-- not see changes to gitignored files, so a check whose result depends on
-- one of those isn't fully captured by this key.
CREATE TABLE IF NOT EXISTS check_cache (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    command TEXT NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT,                   -- the tail of the command's output
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, sha, command)
);
-- The files two running branches were last reported to have both changed
-- (see tasks.conflict_forecast): the supervisor hears about a pair once, and
-- again only when more files join the overlap. Rows go with their workspaces.
CREATE TABLE IF NOT EXISTS conflict_notices (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    other_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    files TEXT NOT NULL,           -- JSON list
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, other_id)
);
-- How long a check command's last passing run took in a repo, so a run that
-- goes far past it can be reported to the supervisor (see gates.run_checked).
CREATE TABLE IF NOT EXISTS check_durations (
    repo_root TEXT NOT NULL,
    command TEXT NOT NULL,
    seconds REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (repo_root, command)
);
-- The same, per branch: a branch's own last passing run is the baseline for
-- its slow-run warning (a 1-file branch's run says little about a 47-file one).
CREATE TABLE IF NOT EXISTS branch_check_durations (
    repo_root TEXT NOT NULL,
    branch TEXT NOT NULL,
    command TEXT NOT NULL,
    seconds REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (repo_root, branch, command)
);
-- Claude Code's own built-in subagents (its Agent tool), reported by the
-- SubagentStart/SubagentStop hooks. Purely informational for the sidebar:
-- kept out of `agents` so they never affect message delivery, is_alive,
-- pause/resume, autopilot worker counts, list_agents, kill or retention.
CREATE TABLE IF NOT EXISTS native_subagents (
    id TEXT PRIMARY KEY,            -- Claude's agent_id
    parent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    agent_type TEXT,
    started_at REAL,
    ended_at REAL
);
-- Incremental token-usage cache for one transcript JSONL file (a session's own,
-- or one of its subagents'), keyed by path so repeated reads only parse new
-- bytes. See brindle.usage.
CREATE TABLE IF NOT EXISTS usage_cache (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,          -- bytes already parsed
    inode INTEGER,                  -- st_ino when parsed; a new one means the file was replaced
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    last_message_id TEXT,           -- dedupes a message streamed across several lines
    updated_at REAL NOT NULL
);
-- Durable, append-only record of what agents did. Deliberately has no foreign
-- keys: session pruning (sessions.py) deletes agents (and cascades reviews and
-- milestones with them), but history must survive that. Capped per repo_root
-- instead (see brindle.history).
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,             -- worker_result | review | merge | check | milestone | permission
    agent_id TEXT,
    branch TEXT,
    profile TEXT,
    task TEXT,                      -- first ~300 chars of the task/summary
    result TEXT,                    -- trimmed result/verdict text
    tokens TEXT                     -- JSON usage since the agent's previous row, or NULL
);
CREATE INDEX IF NOT EXISTS history_repo_root_id ON history(repo_root, id);
-- Review findings that recur across tasks, grouped per repo and worker
-- profile (brindle Pro learned_rules; see brindle.learned_rules). A group is
-- 'forming' until it has recurred often enough to be suggested ('pending'),
-- then 'accepted' (written to .brindle/rules/learned.md) or 'rejected' (and
-- never proposed again). No foreign keys, like history: it outlives sessions.
CREATE TABLE IF NOT EXISTS learned_rules (
    repo_root TEXT NOT NULL,
    key TEXT NOT NULL,
    profile TEXT NOT NULL,          -- the worker profile whose reviews found it
    title TEXT NOT NULL,
    rule TEXT NOT NULL,             -- the rule's text for the agent's prompt
    checks TEXT,                    -- JSON {deny_patterns, deny_deps, require_tests_for}
    findings TEXT NOT NULL,         -- JSON list of history row ids
    branches TEXT NOT NULL,         -- JSON list of the distinct branches (tasks) they came from
    status TEXT NOT NULL,           -- forming | pending | accepted | rejected
    created_at REAL NOT NULL,
    decided_at REAL,
    PRIMARY KEY (repo_root, key)
);
-- The last history row the grouping step has seen, per repo.
CREATE TABLE IF NOT EXISTS learned_rules_mark (
    repo_root TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
);
-- Each agent's cumulative usage as of its latest history row, so the next row
-- stores only the difference and summing rows never double counts. Separate
-- from history so capping history doesn't lose it. transcript_path is the
-- transcript the mark was taken from: a different one now (e.g. after
-- /clear starts a new transcript) means the mark doesn't apply any more, so
-- the next row starts a fresh baseline instead of computing a bogus delta.
CREATE TABLE IF NOT EXISTS history_usage_mark (
    agent_id TEXT PRIMARY KEY,
    transcript_path TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL
);
-- A coordination task declared through assign/handoff: the files it expects
-- to touch and any earlier tasks (by agent id or branch name) that must be
-- merged first. Most tasks start right away and just get a 'started' row
-- here (so overlap checks on later tasks can see their files); one with
-- unmet dependencies gets a 'pending' row instead, with no worker yet, until
-- brindle.tasks.on_merged starts it. See brindle.tasks.
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    agent_id TEXT,                 -- the worker's agent id, once started
    caller_id TEXT,
    caller_ws_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    task_text TEXT NOT NULL,
    mode TEXT NOT NULL,
    isolate INTEGER NOT NULL,
    branch TEXT,
    done_when TEXT,
    files TEXT,                    -- JSON list of globs this task expects to touch
    weight TEXT,                   -- light | medium | heavy, as the supervisor sized it
    depends_on TEXT,               -- JSON list of agent ids / branch names to wait on
    state TEXT NOT NULL,           -- pending | started | merged | cancelled
    created_at REAL NOT NULL,
    started_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_repo_root ON tasks(repo_root);
CREATE TABLE IF NOT EXISTS plan_tasks (
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (plan_id, worker_id)
);
-- Completed worker checks are outcomes, not reusable passing-cache entries.
-- Task ownership keeps their evidence after the worker workspace is removed.
CREATE TABLE IF NOT EXISTS task_check_outcomes (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    ok INTEGER NOT NULL,
    checked_at REAL NOT NULL,
    PRIMARY KEY (task_id, command)
);
-- A session's cloud login (brindle.login_pause), keyed by its root: when it
-- was last looked at, and while only the person can renew it (state
-- 'auth_paused'), what they run and how they were told. Back at 'ok', the
-- next lapse may tell them again.
CREATE TABLE IF NOT EXISTS login_state (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'ok',  -- ok | auth_paused
    checked_at REAL,               -- the last look ahead at its expiry
    agent_id TEXT,                 -- whose login lapsed: re-checked in its env
    refresh_cmd TEXT,
    expired_at REAL,
    noticed TEXT,                  -- how the person was told: chat | bar
    next_check REAL,               -- the next re-check while paused
    delay REAL,                    -- the backoff before it
    interrupted TEXT,              -- JSON list of agents whose turn brindle escaped while paused
    account TEXT                   -- who the lapsed login belonged to, when the CLI records it
);
-- The one `brindle watch --sidebar` pane per interactive session root (see
-- agents.sidebar_follow): its tmux pane id, so any session in that root's
-- tree can find and relocate it instead of starting a second one. Keyed by
-- root, not repo, so a second supervisor in the same repo gets its own.
CREATE TABLE IF NOT EXISTS sidebars (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    pane TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- Pre-built worktrees (see pool.py): checked out on a placeholder branch at
-- the base branch's tip, with `copy` files and `setup` already applied, so
-- `create` can claim one instead of doing that work live. A claim keeps the
-- entry's path and port_base forever (never moved); it just renames the
-- branch. Never exposed as a workspace: find_workspaces/view.snapshot don't
-- touch this table.
CREATE TABLE IF NOT EXISTS pool_entries (
    path TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    branch TEXT NOT NULL,           -- the placeholder branch, e.g. brindle-pool/<token>
    fingerprint TEXT NOT NULL,      -- setup commands + lockfile contents at base_sha
    port_base INTEGER,              -- reserved for this entry so setup's env matches claim
    ready INTEGER NOT NULL DEFAULT 0,  -- 0 while fill_one is still building it
    created_at REAL NOT NULL
);
-- A pool fill's most recent setup failure for a (repo, base), so fill()
-- backs off instead of retrying (and failing) every time something triggers
-- a refill. See pool.FAILURE_BACKOFF.
CREATE TABLE IF NOT EXISTS pool_failures (
    repo_root TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    failed_at REAL NOT NULL,
    PRIMARY KEY (repo_root, base_branch)
);
-- A tool-permission request left to the person (see brindle.permissions):
-- the structured request, so the supervisor can be told exactly what's
-- pending, and so the tool's PostToolUse (the same tool and input) can count
-- as the person approving it. Cleared when the turn ends. While the daemon
-- holds it for the person's answer (channel 'hook': for the inbox to show;
-- 'supervisor' while the worker's supervisor answers it first, see brindle.permission_routing;
-- the answer itself goes through the daemon, brindle.daemon.holds), then the
-- CLI's own prompt has it ('prompt') or the hook took an answer ('answered').
-- ``tool_use_id`` is the request's id: brindle's own, not the CLI's.
CREATE TABLE IF NOT EXISTS permission_requests (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    tool_use_id TEXT NOT NULL,
    request TEXT NOT NULL,          -- JSON of brindle.permissions.Request
    reason TEXT NOT NULL,           -- the policy's; '' when the policy is off
    created_at REAL NOT NULL,
    suggestions TEXT,               -- JSON: the CLI's own "always allow" updates
    channel TEXT,                   -- supervisor | hook | prompt | answered | terminal (CLI approval)
    -- what the person (or, supervisor_allow | supervisor_deny, the supervisor) answered:
    -- a record only, answers go through the daemon
    answer TEXT,
    hold_until REAL,                -- when the hook stops holding it for the app
    PRIMARY KEY (agent_id, tool_use_id)
);
-- The history permission_requests lacks (see brindle.permission_asks): one row
-- per permission hook decision, kept after it is answered. ``asked`` is 1 for
-- a request that was left to the person (or settled by a saved action), 0 for
-- one the policy decided itself. Like history it has no foreign keys, so it
-- survives session pruning.
CREATE TABLE IF NOT EXISTS permission_asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    repo_root TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL,
    request_id TEXT,                -- the permission_requests row's tool_use_id (asks only)
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    asked INTEGER NOT NULL,
    -- policy | person | supervisor | saved_action, or how the hold ended unanswered (timeout,
    -- terminal, agent_exit, daemon_lost, unheld); NULL while the ask is pending
    resolved_by TEXT,
    outcome TEXT NOT NULL,          -- allow | deny | prompt (left to the CLI's own prompt) | none | pending
    wait_seconds REAL               -- NULL when the end of the person's wait wasn't seen
);
CREATE INDEX IF NOT EXISTS permission_asks_ts ON permission_asks(ts);
-- One row per delegation: which profile brindle would have used without
-- learning, which it used, and how the task went (see brindle.savings). Local
-- only, and like history it has no foreign keys, so it survives session
-- pruning.
CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL,
    ts REAL NOT NULL,
    task_id TEXT,
    agent_id TEXT,                  -- the worker, once started
    weight TEXT,                    -- light | medium | heavy, or NULL
    baseline_profile TEXT NOT NULL, -- what brindle would have used without learning
    profile TEXT NOT NULL,          -- what it used
    learned INTEGER NOT NULL DEFAULT 0,        -- 1 when learning made the pick
    prior INTEGER NOT NULL DEFAULT 0,          -- 1 when that pick drew on other orgs' shared results
    review_rounds INTEGER NOT NULL DEFAULT 0,
    escalations INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,                   -- merged | removed_unmerged, once it's over
    demoted_from TEXT,              -- the profile a budget moved this task off (brindle.budget)
    budget_state TEXT,              -- warned | over | stopped (brindle.budget.sweep)
    seeded_at REAL                  -- when `brindle learning seed` sent it to the hosted learner
);
CREATE INDEX IF NOT EXISTS routing_decisions_repo_root ON routing_decisions(repo_root, ts);
CREATE INDEX IF NOT EXISTS routing_decisions_agent ON routing_decisions(agent_id);
CREATE TABLE IF NOT EXISTS launch_health (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    token TEXT NOT NULL DEFAULT '',
    pane TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    argv_task INTEGER NOT NULL DEFAULT 0,
    submitted_prompt TEXT NOT NULL DEFAULT '',
    delivered_at REAL,
    verifying INTEGER NOT NULL DEFAULT 0,
    delivery_lead TEXT,
    baseline TEXT NOT NULL DEFAULT '',
    outcome TEXT,
    launched_at REAL
);
-- A failed MCP connection gets one fresh reviewer per workspace/commit,
-- even after the original agent's record is removed.
CREATE TABLE IF NOT EXISTS review_mcp_retries (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    PRIMARY KEY (workspace_id, sha)
);
-- A dead process gets one replacement for this commit, even after its row is removed.
CREATE TABLE IF NOT EXISTS review_death_restarts (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    PRIMARY KEY (workspace_id, sha)
);
-- A conflict handoff belongs to the branch, including across replacement workers.
CREATE TABLE IF NOT EXISTS pipeline_conflicts (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    PRIMARY KEY (workspace_id, sha)
);
-- The commits the session review's edit route made (the person's own edits):
-- only the daemon writes them, so they never un-audit a session's work.
CREATE TABLE IF NOT EXISTS review_edits (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    PRIMARY KEY (workspace_id, sha)
);
-- The PR of a session's branch (POST /v1/sessions/{root}/pr: only the person
-- opens one): one per session and branch, so none is ever opened twice.
-- url is NULL while it is being opened; pushed_sha: the commit last pushed to it.
CREATE TABLE IF NOT EXISTS session_prs (
    root_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    url TEXT,
    pushed_sha TEXT,
    auto INTEGER NOT NULL DEFAULT 0,
    opened_at REAL NOT NULL,
    merged_at REAL,                 -- when gh first reported the PR merged (session_review.poll_merged)
    PRIMARY KEY (root_id, branch)
);
-- A durable claim prevents overlapping daemons from merging the same PR twice.
-- Keep failed attempts for the Inbox; only poll_merged records an actual merge.
CREATE TABLE IF NOT EXISTS session_pr_merges (
    url TEXT PRIMARY KEY,
    state TEXT NOT NULL,            -- merging | submitted | failed | pushing (a phase landing's push)
    owner TEXT,                    -- unique token whose session-pr-merge marker is held while acting
    error TEXT
);
-- What a phase landing's push (session_pr_merges state 'pushing') sends to the PR at url: kept with
-- the claim, so a push whose owner went away is reconciled against the PR's head before any merge.
CREATE TABLE IF NOT EXISTS session_pr_pushes (
    url TEXT PRIMARY KEY,
    sha TEXT NOT NULL
);
-- A session PR adopted from one opened with gh on another branch name: where it really lives,
-- so a Push of the session branch is refused instead of missing it.
CREATE TABLE IF NOT EXISTS session_pr_heads (
    root_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    head_ref TEXT NOT NULL,
    PRIMARY KEY (root_id, branch)
);
-- A ready session's PR as brindle prepared it for the person's approval
-- (routes_actions.prepare_pr): the pre-PR checks run on sha, nothing pushed.
-- state: checking | passed | failed (check_cmd and output: the failing check);
-- checks: the JSON list of commands that passed on sha (only those are reused).
CREATE TABLE IF NOT EXISTS session_pr_prep (
    root_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    sha TEXT NOT NULL,
    state TEXT NOT NULL,
    title TEXT,
    checks TEXT,
    check_cmd TEXT,
    output TEXT,
    prepared_at REAL NOT NULL,
    PRIMARY KEY (root_id, branch)
);
-- Inbox's Not now choice: only hides a ready PR at this branch head.
-- Separate from preparation so rechecking the same head preserves the choice.
CREATE TABLE IF NOT EXISTS session_pr_declined (
    root_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    sha TEXT NOT NULL,
    PRIMARY KEY (root_id, branch)
);
-- A session branch against origin/<default> as brindle last synced it
-- (brindle.session_sync). state: synced | behind (its checkout was dirty) |
-- conflict (files: the JSON list); upstream_sha: the origin commit looked at;
-- told_sha: the origin commit the supervisor was last told about.
CREATE TABLE IF NOT EXISTS session_sync (
    repo_root TEXT NOT NULL,
    branch TEXT NOT NULL,
    upstream_sha TEXT NOT NULL,
    state TEXT NOT NULL,
    files TEXT,
    told_sha TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (repo_root, branch)
);
-- One automatic resolution attempt per conflicting origin commit, retained
-- after failure so repeated syncs do not start another worker.
CREATE TABLE IF NOT EXISTS session_sync_resolvers (
    repo_root TEXT NOT NULL,
    branch TEXT NOT NULL,
    upstream_sha TEXT NOT NULL,
    task_id TEXT,
    PRIMARY KEY (repo_root, branch, upstream_sha)
);
-- Unlanded session work, independent of whether origin has been merged in.
CREATE TABLE IF NOT EXISTS session_drift (
    repo_root TEXT NOT NULL,
    branch TEXT NOT NULL,
    oldest_at REAL,
    file_count INTEGER NOT NULL,
    drifting INTEGER NOT NULL,
    PRIMARY KEY (repo_root, branch)
);
-- The supervisors told that a repo no longer refreshes on its own: origin wants
-- credentials (session_sync.tell_refused_fetches). Goes once origin answers again.
CREATE TABLE IF NOT EXISTS fetch_refused_notices (
    root_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    repo_root TEXT NOT NULL,
    told_at REAL NOT NULL,
    PRIMARY KEY (root_id, repo_root)
);
-- A session PR that landed on the default branch as a squash (or rebase):
-- squash_sha is that commit, covers_sha the session commit the PR carried,
-- so a later sync records it as merged instead of re-applying it.
CREATE TABLE IF NOT EXISTS session_squashes (
    repo_root TEXT NOT NULL,
    branch TEXT NOT NULL,
    squash_sha TEXT NOT NULL,
    covers_sha TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (repo_root, squash_sha)
);
-- A session PR that merged but whose landing commit (merge or squash) isn't
-- known yet: looked up at each sync until it is (brindle.session_sync).
CREATE TABLE IF NOT EXISTS session_pr_landings (
    root_id TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    branch TEXT NOT NULL,
    url TEXT NOT NULL,
    covers_sha TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_root, branch, url)
);
-- The one reviewer of a workspace's commit (agents.request_review): only the
-- dispatcher that inserts or takes over this row starts it. slot is '' for
-- the commit's review, or the profile of a configured second review.
CREATE TABLE IF NOT EXISTS review_claims (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    slot TEXT NOT NULL,
    agent_id TEXT,                  -- NULL while its dispatcher is still starting it
    claimed_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, sha, slot)
);
-- A launch whose CLI hasn't shown its input box yet, and the model calls
-- spent reading its startup prompts (brindle.prompt_answer).
CREATE TABLE IF NOT EXISTS startup_prompts (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    pane TEXT NOT NULL,
    launched_at REAL NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agent_reported_spend (
    agent_id TEXT PRIMARY KEY,
    reported_usd REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- The shared knowledge layer (brindle.knowledge owns these). kb_events is the
-- append-only source of truth; intents and decisions are projections rebuilt
-- from it.
CREATE TABLE IF NOT EXISTS kb_events (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    actor TEXT,
    session TEXT,
    machine TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS kb_events_repo ON kb_events(repo_key, ts);
-- The local event bus (brindle.bus owns this): append-only, short-lived.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    repo_root TEXT,
    root_id TEXT,
    agent_id TEXT,
    workspace_id TEXT,
    task_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
-- Per-stage pipeline timings rolled up from events as they are pruned
-- (brindle.bus.rollup_stage_timings): one row per UTC day, repo and stage.
-- ``samples`` (JSON seconds) lets a later prune of the same day merge exactly.
CREATE TABLE IF NOT EXISTS stage_timings (
    day TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    stage TEXT NOT NULL,
    n INTEGER NOT NULL,
    median_s REAL NOT NULL,
    p90_s REAL NOT NULL,
    samples TEXT NOT NULL,
    PRIMARY KEY (day, repo_root, stage)
);
-- Stages begun in events already pruned, waiting for their end event.
CREATE TABLE IF NOT EXISTS stage_open (
    workspace_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    who TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (workspace_id, stage, who)
);
CREATE TABLE IF NOT EXISTS intents (
    session_root TEXT NOT NULL,
    repo_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    paths TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_root, repo_key)
);
-- Shared decisions, a projection of decision events: scope is a JSON list of
-- path globs ([] for the whole repo); a superseded row keeps its text.
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    body TEXT NOT NULL,
    rationale TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'superseded')),
    author_session TEXT,
    link TEXT,
    supersedes TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_repo ON decisions(repo_key, status);
CREATE TABLE IF NOT EXISTS context_receipts (
    task_id TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (task_id, item_kind, item_id)
);
-- A session's drafts of analysis docs (brindle.pro.analysis_docs owns these):
-- kept apart from the agent and workspace rows, which may go first.
CREATE TABLE IF NOT EXISTS analysis_drafts (
    session TEXT NOT NULL,
    branch TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    workspace_id TEXT,
    drafter TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'clash', 'publish', 'drop')),
    PRIMARY KEY (session, branch, doc_id)
);
CREATE INDEX IF NOT EXISTS analysis_drafts_ws ON analysis_drafts(workspace_id, branch, state);
-- Knowledge sources and their indexed documents (brindle.kb owns these; the
-- kb_fts full-text table is made in DB._migrate, as SQLite may lack FTS5).
CREATE TABLE IF NOT EXISTS kb_sources (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'indexed' CHECK (mode IN ('indexed', 'federated')),
    visibility TEXT NOT NULL DEFAULT 'repo' CHECK (visibility IN ('repo', 'personal', 'org')),
    credential_name TEXT,
    retention_days INTEGER,
    sync_cursor TEXT,
    added_at REAL NOT NULL,
    UNIQUE (repo_key, kind, scope)
);
CREATE TABLE IF NOT EXISTS kb_docs (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES kb_sources(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    updated_at REAL,
    content_hash TEXT NOT NULL,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, external_id)
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id TEXT PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,
    heading_path TEXT NOT NULL,
    text TEXT NOT NULL,
    quarantined TEXT
);
-- Imported folders added to the personal library (brindle.pro.analysis_imports):
-- the folder, and a cache of each file sent (path to hash, plus the doc it became).
-- Every row names the identity that authorized it (backend, user, org, scope); a different
-- signed-in identity sees the folder as local and leaves these rows alone.
CREATE TABLE IF NOT EXISTS kb_import_library (
    source_id TEXT NOT NULL REFERENCES kb_sources(id) ON DELETE CASCADE,
    identity TEXT NOT NULL,
    added_at REAL NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0,   -- files the last upload could not send
    -- adding | in_library | removing (no row: local); removing is kept until the server
    -- confirms every delete.
    state TEXT NOT NULL DEFAULT 'in_library' CHECK (state IN ('adding', 'in_library', 'removing')),
    -- The one operation working on the folder (brindle.pro.analysis_imports): claimed in a
    -- transaction, recorded against only while op_token still matches, until op_until.
    op TEXT CHECK (op IN ('add', 'sync', 'remove')),
    op_token TEXT,
    op_until REAL,
    PRIMARY KEY (source_id, identity)
);
-- Folder removals the server has not confirmed for a source that was deleted meanwhile. Not
-- tied to the source, so they outlive it until the folder DELETE succeeds.
CREATE TABLE IF NOT EXISTS kb_import_removals (
    source_id TEXT NOT NULL,
    identity TEXT NOT NULL,
    PRIMARY KEY (source_id, identity)
);
CREATE TABLE IF NOT EXISTS kb_import_uploads (
    source_id TEXT NOT NULL REFERENCES kb_sources(id) ON DELETE CASCADE,
    identity TEXT NOT NULL,
    external_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    PRIMARY KEY (source_id, identity, external_id)
);
-- Only an explicit session request creates a document inbox item.
CREATE TABLE IF NOT EXISTS doc_requests (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    doc_id BLOB NOT NULL,          -- integer local id or opaque server string id, without coercion
    title TEXT,
    section TEXT,
    why TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- What the sweep reaped (brindle.reap): tmux sessions, stopped supervisors,
-- superseded workers, notices sent once. `brindle doctor` lists recent rows;
-- an 'idle_supervisor' row is an Inbox item while its root stays paused.
CREATE TABLE IF NOT EXISTS reaped (
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,          -- a tmux session name or an agent id
    root_id TEXT,                  -- the session root it belonged to, if known
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reaped_target ON reaped(kind, target);
CREATE INDEX IF NOT EXISTS kb_chunks_doc ON kb_chunks(doc_id);
CREATE TABLE IF NOT EXISTS kb_links (
    chunk_id TEXT NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY (chunk_id, node_id)
);
CREATE TABLE IF NOT EXISTS kb_embeddings (
    chunk_id TEXT PRIMARY KEY REFERENCES kb_chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL
);
-- Offers and one-use requests from the person (brindle.visual_offers).
CREATE TABLE IF NOT EXISTS visual_offers (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    question_hash TEXT NOT NULL,
    question TEXT NOT NULL,
    kind TEXT NOT NULL,
    counts TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS visual_grants (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    remaining INTEGER NOT NULL,
    explicit INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT 'visual'
);
CREATE TABLE IF NOT EXISTS visual_input_state (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    observer INTEGER NOT NULL,
    invalidated_at REAL NOT NULL DEFAULT 0
);
-- What the person typed lately, for publish_visual's `asked` to be checked
-- against. A row is spent by the one publication it allows; the rest go after
-- visual_offers.FOLD. source: 'grant' when this same message raised the
-- session's grant, which spending the row also spends. staged:
-- reported by the app, waiting for the CLI's hook to report that same prompt.
CREATE TABLE IF NOT EXISTS visual_person_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    words TEXT NOT NULL,
    source TEXT,
    staged INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
-- Visuals an agent published into a session (brindle.visuals owns these):
-- the current revision's question and options, the person's answer to it.
-- Deleted with their session; a kept copy is a kept_visuals row instead.
CREATE TABLE IF NOT EXISTS visuals (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,         -- the session (its root agent)
    repo_root TEXT NOT NULL,       -- the session's repo: pages are under its .brindle/visuals/
    by_agent TEXT NOT NULL,        -- who published the current revision
    kind TEXT NOT NULL,            -- visual | plan (analysis_doc: an older finished-doc page)
    title TEXT NOT NULL,
    rev INTEGER NOT NULL,
    question TEXT,
    options TEXT,                  -- JSON [{id, label, detail}] of the current revision
    answer TEXT,                   -- JSON {option, note, at, rev}
    read_rev INTEGER NOT NULL DEFAULT 0,
    dismissed_rev INTEGER NOT NULL DEFAULT 0,  -- the revision the person dismissed without answering
    consented_by TEXT,             -- the session supervisor that published it with the person's consent
    kept_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS visuals_root ON visuals(root_id, created_at);
CREATE TABLE IF NOT EXISTS kept_visuals (
    id TEXT PRIMARY KEY,           -- the visual's id; outlives the visual and its session
    title TEXT NOT NULL,
    from_session TEXT NOT NULL,
    from_session_name TEXT NOT NULL,
    repo TEXT NOT NULL,            -- its page is under this repo's .brindle/kept/visuals/
    rev INTEGER NOT NULL,
    answer TEXT,
    kept_at REAL NOT NULL
);
-- Planning docs (brindle.plans): a plan is a visuals row of kind 'plan' with
-- this row beside it. While one is open or redirected, the session's queued
-- tasks wait; the person's approval releases them. Deleted with the session.
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,           -- the plan's visual id
    root_id TEXT NOT NULL,         -- the session (its root agent)
    rev INTEGER NOT NULL,
    markdown TEXT NOT NULL,        -- the current revision, as the supervisor wrote it
    decisions TEXT NOT NULL,       -- JSON [{id, title, options, mine, challenge}]
    split TEXT NOT NULL,           -- JSON [{id, title, after, files, test}]
    state TEXT NOT NULL,           -- open | redirected | approved
    decision TEXT,                 -- JSON {approved, picks, comments, note, at, rev}: the person's
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ask TEXT                       -- the person's request restated by the supervisor, if given
);
CREATE INDEX IF NOT EXISTS plans_root ON plans(root_id, state);
-- A plan as a design doc (brindle.plan_docs): every revision, the supervisor's
-- and the person's own edits alike; the plans row holds the current one.
CREATE TABLE IF NOT EXISTS plan_revisions (
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    rev INTEGER NOT NULL,
    author TEXT NOT NULL,          -- supervisor | person | teammate:<sub> (from the Library)
    source TEXT NOT NULL,          -- proposal | edit | suggestion
    lede TEXT NOT NULL,
    sections TEXT NOT NULL,        -- JSON [{id, title, kind, markdown, caption}]
    markdown TEXT NOT NULL,
    decisions TEXT NOT NULL,
    split TEXT NOT NULL,
    ask TEXT,
    changed TEXT NOT NULL,         -- JSON: the section ids this revision changed
    created_at REAL NOT NULL,
    PRIMARY KEY (plan_id, rev)
);
-- Comment threads on a plan, anchored to a text range, a diagram, a decision
-- or a whole section; a suggestion is a thread with a replacement; replies
-- name their thread in parent_id.
CREATE TABLE IF NOT EXISTS plan_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES plan_threads(id) ON DELETE CASCADE,
    rev INTEGER NOT NULL,          -- the revision it was written on
    kind TEXT NOT NULL,            -- comment | suggestion | reply
    anchor TEXT,                   -- JSON, see brindle.plan_docs
    author TEXT NOT NULL,          -- person | supervisor | teammate:<sub> (from the Library)
    body TEXT NOT NULL,
    after TEXT,                    -- a suggestion's replacement for the anchored text
    attachments TEXT NOT NULL,     -- JSON [{name, ref}]
    status TEXT NOT NULL,          -- open | resolved | accepted | rejected
    sent_rev INTEGER,              -- the verdict that carried it to the supervisor
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_threads_plan ON plan_threads(plan_id, parent_id);
-- Exact Brindle account ownership, recorded at supervisor launch. Unknown is
-- deliberately absent, never a wildcard when routing a shared-doc question.
CREATE TABLE IF NOT EXISTS supervisor_accounts (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    owner TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_questions (
    doc_id TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (doc_id, thread_id, owner)
);
-- Votes on a plan's decisions (Confirm A): one per voter and decision.
CREATE TABLE IF NOT EXISTS plan_votes (
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    decision_id TEXT NOT NULL,
    voter TEXT NOT NULL,
    option_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (plan_id, decision_id, voter)
);
-- A plan shared through the Library (brindle.pro.shared_docs): its document,
-- which local revision the Library's head holds, and the last review state.
CREATE TABLE IF NOT EXISTS plan_shares (
    plan_id TEXT PRIMARY KEY REFERENCES plans(id) ON DELETE CASCADE,
    doc_id TEXT,                   -- NULL while the document is being created
    owner TEXT NOT NULL,           -- the account that shared it
    remote_rev INTEGER NOT NULL DEFAULT 0,
    local_rev INTEGER NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '{}',   -- JSON: reviewers, sign-offs, who is pending, ready
    claimed_at REAL,               -- set while a create or a push is under way
    org_id TEXT,                   -- the org it was shared from; NULL on a share older than this column
    lapsed_at REAL,                -- when the server last confirmed that account and org have no Team plan;
                                   -- NULL once a check fails. Honoured while recent (shared_docs.lapse_stands)
    unconfirmed TEXT,              -- why the Library couldn't confirm the review (plans.UNCONFIRMED); NULL once it answers
    approve_wanted TEXT,           -- JSON: the person's Approve, held until the Library answers (plans.settle_held_approval)
    approve_refused TEXT,          -- why a held Approve wasn't applied, for the person
    access_refused TEXT,           -- the Library refused this account access; NULL once it next gives a whole answer
    review_asked INTEGER NOT NULL DEFAULT 0   -- 1 once any review state seen here had required reviewers or org rules
);
-- This machine's replica of a shared doc's live draft (brindle.plan_drafts):
-- its Loro state and the peer id it writes under, in one row, so the id is
-- never used without the changes already written under it. seq goes up on
-- every write, so a holder that missed one reloads before writing.
CREATE TABLE IF NOT EXISTS plan_drafts (
    doc_id TEXT PRIMARY KEY,       -- the Library document
    peer_id INTEGER NOT NULL UNIQUE,
    state BLOB NOT NULL,           -- a Loro snapshot
    seq INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    supervisor_peer INTEGER        -- the peer id the session's supervisor writes under; NULL until it first does
);
-- The draft text handed to the supervisor (plans.read_draft), by the id its
-- next write names: its changes are worked out against this, never against
-- the draft as it is when they land. Gone with the replica.
CREATE TABLE IF NOT EXISTS plan_draft_bases (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES plan_drafts(doc_id) ON DELETE CASCADE,
    texts TEXT NOT NULL,           -- JSON: section id (or lede) -> the text handed over
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_draft_bases_plan ON plan_draft_bases(plan_id, created_at);
-- Updates a replica received before the changes they build on: a snapshot
-- leaves them out, so they wait here and are imported again on every load.
CREATE TABLE IF NOT EXISTS plan_draft_pending (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES plan_drafts(doc_id) ON DELETE CASCADE,
    data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_draft_pending_doc ON plan_draft_pending(doc_id, n);
-- Documents whose owner chose Stop sharing while the Library was out of reach:
-- their teams and org lose access at the next pass that reaches it.
CREATE TABLE IF NOT EXISTS plan_unshares (
    doc_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- Which local thread or reply each Library thread or reply became, so a pull
-- adds it once; status is the Library's as last seen. A negative remote_id is
-- a thread with no Library thread: status 'sending' for one sent whose answer
-- hasn't come back, otherwise one written before sharing, which stays here.
CREATE TABLE IF NOT EXISTS plan_share_threads (
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    remote_id INTEGER NOT NULL,
    local_id INTEGER NOT NULL REFERENCES plan_threads(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    PRIMARY KEY (plan_id, remote_id)
);
"""


@dataclass
class Workspace:
    id: str
    repo_root: str
    name: str
    kind: str
    branch: str
    base_branch: str | None
    path: str
    port_base: int | None
    tmux_session: str
    created_at: float
    session_root: str | None = None


@dataclass
class Agent:
    id: str
    workspace_id: str
    profile: str
    provider: str
    parent_id: str | None
    mode: str
    status: str
    tmux_window: str
    result: str | None
    created_at: float
    status_since: float | None = None
    task: str | None = None
    session_ref: str | None = None
    stop_blocked: int | None = None
    headless: int | None = None
    transcript_path: str | None = None
    done_when: str | None = None
    dismissed_at: float | None = None
    inbox_socket: str | None = None
    inbox_token: str | None = None
    pipeline: str | None = None
    pipeline_rounds: int | None = None
    stuck_noted: float | None = None
    review_sha: str | None = None
    unreported_noted: float | None = None
    plan_first: int | None = None      # must get its plan approved before editing (brindle.agents.submit_plan)
    plan_state: str | None = None      # proposed | approved | revise
    plan_text: str | None = None       # the plan awaiting a decision (agents.submit_plan), shown in the inbox
    tmux_server: str | None = None     # the private tmux server its window is on (see tmux.current_server)
    stop_reminded_at: float | None = None
    reopened_at: float | None = None
    hooks_off: str | None = None       # why its CLI runs none of brindle's hooks (agents.screen_status)
    reviewing_since: float | None = None   # when its last report put its branch in review (pipeline.on_report)
    review_restarted: float | None = None  # the wait in review whose review was started again (pipeline.restart_review)
    merge_failed: str | None = None        # why the pipeline couldn't merge its approved branch (pipeline.review_state)
    config_home: str | None = None         # its CLI's config home, decided once at spawn (provider_homes); '' the person's own
    weight: str | None = None              # the weight its task was routed as, known from launch (agents.spawn)
    review_slot: str | None = None         # the review claim slot it was started in (gates.standing_reviews)
    failed_why: str | None = None          # why it is 'failed': auth | model | delivery; None from an older brindle
    name: str | None = None                # a session root's name from the person; None: listed by its goal

@dataclass
class Autopilot:
    root_id: str
    enabled: int
    goal: str | None
    detail: str | None
    state: str
    note: str | None
    progress: int
    nudges: int
    nudged_at: int | None
    created_at: float
    checking_since: float | None = None
    usage_resets_at: float | None = None
    usage_paused_ids: str | None = None
    goals_file: str | None = None      # the goals.md this session's goal was loaded from (status is written back)
    goal_sha: str | None = None        # HEAD when the goal was set (the completion audit's diff base)
    auditor_id: str | None = None      # the reviewer auditing the finished goal
    audit_sha: str | None = None       # the commit the last completion audit judged
    audit_ok: int | None = None        # its verdict
    audit_history: str | None = None   # persistent audit notes and accepted decisions
    question: str | None = None        # the open need_user question the user hasn't answered
    nudged_time: float | None = None   # when the last nudge was sent; never cleared, the backoff ignores it once progress moves past nudged_at
    proposal: str | None = None        # JSON {checks, rules, at}: checks awaiting the user's answer
    external_wait: str | None = None   # JSON {pid, until, path, note}: an outside job
    offer_goal_at: float | None = None  # when the current goal began (workers since count toward its offer)
    plan_offer: str | None = None  # the offer to plan first, see brindle.plans.OFFER_* (None: never made)
    summary: str | None = None        # a short goal label for the sidebar


@dataclass
class LoginState:
    root_id: str
    state: str
    checked_at: float | None = None
    agent_id: str | None = None
    refresh_cmd: str | None = None
    expired_at: float | None = None
    noticed: str | None = None
    next_check: float | None = None
    delay: float | None = None
    interrupted: str | None = None
    account: str | None = None


@dataclass
class Milestone:
    id: int
    root_id: str
    position: int
    title: str
    check_cmd: str | None
    detail: str | None
    status: str
    checked_at: float | None
    output: str | None
    checked_sha: str | None = None
    passed_sha: str | None = None
    profile: str | None = None
    repo: str | None = None
    state: str | None = None
    state_why: str | None = None
    ever_passed: int | None = None   # 1 once any check passed, whatever sha it ran at
    phase_name: str | None = None    # the goal's phase this milestone sits in (goals.md `phase:`)


_EXIT = re.compile(r"\(exit (-?\d+)\)\s*$")


def check_state(passed: bool, output: str, had_passed: bool) -> tuple[str, str | None]:
    """A milestone check's state and, for ``failed`` (the check couldn't be
    done), why. A non-zero exit is ``not_done``, or ``regressed`` if the
    milestone had passed before; only exit 126/127 or a timeout couldn't run."""
    if passed:
        return "passed", None
    if "isn't attached to this session" in (output or ""):
        return "failed", "repo isn't attached"
    if m := _EXIT.search(output or ""):
        code = int(m.group(1))
        if code in (126, 127):
            return "failed", "command not found" if code == 127 else "command not runnable"
        # Negative: killed by that signal; 128+n: how a shell reports the same.
        if code < 0 or (code > 128 and code - 128 in signal.valid_signals()):
            return "failed", f"crashed: killed by signal {-code if code < 0 else code - 128}"
    if t := re.search(r"\(timed out after (\d+)s\)\s*$", output or ""):
        return "failed", f"timed out after {t.group(1)}s"
    return ("regressed" if had_passed else "not_done"), None


@dataclass
class SessionRepo:
    root_id: str
    alias: str
    repo_root: str
    added_at: float
    branch: str | None = None


@dataclass
class Review:
    id: int
    workspace_id: str
    sha: str
    reviewer_id: str | None
    approved: int
    summary: str | None
    created_at: float
    review_slot: str | None = None
    reviewer_created_at: float | None = None


@dataclass
class HistoryEntry:
    id: int
    repo_root: str
    ts: float
    kind: str
    agent_id: str | None
    branch: str | None
    profile: str | None
    task: str | None
    result: str | None
    tokens: str | None


@dataclass
class LearnedRule:
    repo_root: str
    key: str
    profile: str
    title: str
    rule: str
    checks: str | None
    findings: str
    branches: str
    status: str
    created_at: float
    decided_at: float | None = None


@dataclass
class RoutingDecision:
    id: int
    repo_root: str
    ts: float
    task_id: str | None
    agent_id: str | None
    weight: str | None
    baseline_profile: str
    profile: str
    learned: int
    prior: int = 0
    review_rounds: int = 0
    escalations: int = 0
    outcome: str | None = None
    demoted_from: str | None = None
    budget_state: str | None = None
    seeded_at: float | None = None


@dataclass
class CheckResult:
    workspace_id: str
    sha: str
    command: str
    ok: int
    output: str | None
    created_at: float


@dataclass
class NativeSubagent:
    id: str
    parent_id: str
    agent_type: str | None
    started_at: float
    ended_at: float | None


@dataclass
class PoolEntry:
    path: str
    repo_root: str
    base_branch: str
    base_sha: str
    branch: str
    fingerprint: str
    port_base: int | None
    ready: int
    created_at: float


@dataclass
class Message:
    id: int
    agent_id: str
    sender_id: str | None
    body: str
    created_at: float
    delivered_at: float | None
    held: int | None = None
    noticed_at: float | None = None


@dataclass
class OpenItem:
    worker_id: str
    supervisor_id: str
    kind: str
    detail: str
    opened_at: float
    stop_at: float
    level: int = 0
    retiring_at: float | None = None


@dataclass
class Task:
    id: str
    repo_root: str
    agent_id: str | None
    caller_id: str | None
    caller_ws_id: str
    profile: str
    task_text: str
    mode: str
    isolate: int
    branch: str | None
    done_when: str | None
    files: str | None
    depends_on: str | None
    state: str
    created_at: float
    started_at: float | None = None
    plan_first: int | None = None
    weight: str | None = None
    analysis_doc: int | None = None    # retired (sessions no longer ask for analysis docs); kept for old rows
    waiting: str | None = None         # pending only for room on the machine: why (brindle.tasks.wait_for_room)
    waiting_since: float | None = None
    pipeline: str | None = None        # the pipeline state its worker starts in (brindle.pipeline)
    forced: int | None = None          # 1: a supervisor started it despite the load (brindle.tasks.start_now)


def request_subagent(row) -> str | None:
    """The Agent-tool subagent a permission_requests row's request came from
    (Claude Code's agent_id, kept in its request JSON), or None: the main thread."""
    try:
        subagent = json.loads(row["request"]).get("subagent")
    except (ValueError, TypeError, AttributeError):
        return None
    return subagent if isinstance(subagent, str) and subagent else None


WAITER_CLAIM = "waiter-claim:"   # agent_marks kind prefix, then the waiter's pid (DB.pop_pending)


def _pane_delivery_message(c: sqlite3.Connection, agent_id: str) -> int | None:
    """The id of the message ``agent_id``'s pane-delivery mark is typing in,
    while that message is still there. A mark whose message is gone (a
    notice read_held dropped) holds nothing back: left blocking, it stopped
    every later message, the person's too, on every path."""
    row = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind='pane-delivery'",
                    (agent_id,)).fetchone()
    if row is None:
        return None
    try:
        message_id = json.loads(row["value"]).get("message_id")
    except (ValueError, AttributeError):
        return None
    found = c.execute("SELECT 1 FROM inbox WHERE id=? AND agent_id=?", (message_id, agent_id)).fetchone()
    return message_id if found else None


def _load(cls, row):
    """Build ``cls`` from a row, ignoring columns this version doesn't know.
    A newer brindle may have added columns; an older brindle reading the same
    ~/.brindle database must not crash on them."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


def _columns(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {t: frozenset(r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')) for t in tables}


@functools.cache
def _declared_columns() -> dict[str, frozenset[str]]:
    """The columns this version's schema and migrations give each table."""
    db = DB(":memory:")
    try:
        return _columns(db.conn)
    finally:
        db.conn.close()


def migrated_by_newer() -> bool:
    """Whether the on-disk schema is incompatible with this version's writes."""
    return migration_incompatibility() is not None


def migration_incompatibility() -> str | None:
    """Describe incompatible schema changes without running setup/migrations.

    Extra tables and columns that can be omitted on insert are harmless.
    Missing declared columns or extra required columns prevent correct writes.
    A newer user_version is an explicit compatibility boundary regardless of
    columns. No database yet means no incompatibility.
    """
    try:
        conn = sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        issues = []
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            issues.append(f"user_version {version} exceeds supported {SCHEMA_VERSION}")
        for table, declared in _declared_columns().items():
            quoted = table.replace('"', '""')
            columns = list(conn.execute(f'PRAGMA table_info("{quoted}")'))
            missing = declared - {r[1] for r in columns}
            required = {r[1] for r in columns if r[1] not in declared and r[3] and r[4] is None}
            if missing:
                issues.append(f"missing columns in {table}: {', '.join(sorted(missing))}")
            if required:
                issues.append(f"extra NOT NULL columns without defaults in {table}: {', '.join(sorted(required))}")
        return "; ".join(issues) or None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


class DB:
    def __init__(self, path: str | None = None) -> None:
        p = str(path) if path is not None else str(db_path())
        if p != ":memory:":
            from pathlib import Path

            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self.conn = sqlite3.connect(p, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        try:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    "This database was written by a newer brindle. Update brindle."
                )
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._enforce_permissions()
            self._migrate()
            # Mark the upgrade only after all required migrations succeed.
            if version < SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self.conn.close()
            raise

    def _enforce_permissions(self) -> None:
        if self.path == ":memory:":
            return
        import os
        base = str(self.path)
        for target in (base, base + "-wal", base + "-shm"):
            if os.path.exists(target):
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass


    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        thread_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_threads)")}
        for flag in ("question", "from_supervisor"):
            if flag not in thread_cols:
                self.conn.execute(f"ALTER TABLE plan_threads ADD COLUMN {flag} INTEGER NOT NULL DEFAULT 0")
        merge_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(session_pr_merges)")}
        if "owner" not in merge_cols:
            self.conn.execute("ALTER TABLE session_pr_merges ADD COLUMN owner TEXT")
        input_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(visual_input_state)")}
        if "observer" not in input_cols:
            self.conn.execute("ALTER TABLE visual_input_state ADD COLUMN observer INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE visual_input_state SET observer=generation")
        if "invalidated_at" not in input_cols:
            self.conn.execute("ALTER TABLE visual_input_state ADD COLUMN invalidated_at REAL NOT NULL DEFAULT 0")
        grant_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(visual_grants)")}
        if "scope" not in grant_cols:
            self.conn.execute("ALTER TABLE visual_grants ADD COLUMN scope TEXT NOT NULL DEFAULT 'visual'")
            # Earlier grants did not distinguish conditional requests or artifact scope.
            self.conn.execute("UPDATE visual_grants SET remaining=0")
        visual_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(visuals)")}
        if "dismissed_rev" not in visual_cols:
            self.conn.execute("ALTER TABLE visuals ADD COLUMN dismissed_rev INTEGER NOT NULL DEFAULT 0")
        if "consented_by" not in visual_cols:
            self.conn.execute("ALTER TABLE visuals ADD COLUMN consented_by TEXT")
        if "ask" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(plans)")}:
            self.conn.execute("ALTER TABLE plans ADD COLUMN ask TEXT")
        share_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_shares)")}
        for col, kind in (("org_id", "TEXT"), ("lapsed_at", "REAL"), ("unconfirmed", "TEXT"),
                          ("approve_wanted", "TEXT"), ("approve_refused", "TEXT"), ("access_refused", "TEXT")):
            if col not in share_cols:
                self.conn.execute(f"ALTER TABLE plan_shares ADD COLUMN {col} {kind}")
        if "review_asked" not in share_cols:
            self.conn.execute("ALTER TABLE plan_shares ADD COLUMN review_asked INTEGER NOT NULL DEFAULT 0")
            # What these shares saw before it was kept is unknown: nothing may be assumed unreviewed.
            self.conn.execute("UPDATE plan_shares SET review_asked=1")
        if "supervisor_peer" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_drafts)")}:
            self.conn.execute("ALTER TABLE plan_drafts ADD COLUMN supervisor_peer INTEGER")
        review_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(reviews)")}
        for col, kind in (("review_slot", "TEXT"), ("reviewer_created_at", "REAL")):
            if col not in review_cols:
                self.conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} {kind}")
        workspace_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(workspaces)")}
        if "session_root" not in workspace_cols:
            self.conn.execute("ALTER TABLE workspaces ADD COLUMN session_root TEXT")
        if any(r["table"] == "kb_docs" for r in self.conn.execute("PRAGMA foreign_key_list(doc_requests)")):
            # Early local-only requests referenced kb_docs. Keep their ids and
            # titles while allowing server-only references in the same store.
            with self.tx() as c:
                c.execute("ALTER TABLE doc_requests RENAME TO local_doc_requests")
                c.execute("CREATE TABLE doc_requests (id TEXT PRIMARY KEY,"
                          " agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,"
                          " doc_id BLOB NOT NULL, title TEXT, section TEXT, why TEXT NOT NULL,"
                          " created_at REAL NOT NULL)")
                c.execute("INSERT INTO doc_requests (id, agent_id, doc_id, title, section, why, created_at)"
                          " SELECT r.id, r.agent_id, r.doc_id, d.title,"
                          " r.section, r.why, r.created_at FROM local_doc_requests r"
                          " JOIN kb_docs d ON d.id = r.doc_id")
                c.execute("DROP TABLE local_doc_requests")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(agents)")}
        for col, kind in (("status_since", "REAL"), ("task", "TEXT"), ("session_ref", "TEXT"),
                          ("stop_blocked", "INTEGER"), ("headless", "INTEGER"),
                          ("transcript_path", "TEXT"), ("done_when", "TEXT"),
                          ("dismissed_at", "REAL"), ("stuck_noted", "REAL"),
                          ("inbox_socket", "TEXT"), ("inbox_token", "TEXT"),
                          ("pipeline", "TEXT"), ("pipeline_rounds", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
        for col, kind in (("review_sha", "TEXT"), ("unreported_noted", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
        for col, kind in (("plan_first", "INTEGER"), ("plan_state", "TEXT"),
                          ("tmux_server", "TEXT"), ("stop_reminded_at", "REAL"),
                          ("reopened_at", "REAL"), ("hooks_off", "TEXT"),
                          ("reviewing_since", "REAL"), ("review_restarted", "REAL"),
                          ("merge_failed", "TEXT"), ("plan_text", "TEXT"), ("config_home", "TEXT"),
                          ("weight", "TEXT"), ("review_slot", "TEXT"), ("failed_why", "TEXT"),
                          ("name", "TEXT")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
        if "failed_why" not in cols:
            # Agents failed before the cause was kept: a brief launch_health
            # couldn't deliver is known; the rest stay unknown (service.inbox).
            self.conn.execute("UPDATE agents SET failed_why='delivery' WHERE status='failed' AND id IN "
                              "(SELECT agent_id FROM launch_health WHERE outcome='delivery-failed')")
        if "review_slot" not in review_cols:
            # Preserve supersession metadata while the old reviewer still exists.
            self.conn.execute(
                "UPDATE reviews SET review_slot=(SELECT review_slot FROM agents WHERE id=reviewer_id), "
                "reviewer_created_at=(SELECT created_at FROM agents WHERE id=reviewer_id)"
            )
        task_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "plan_first" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN plan_first INTEGER")
        if "weight" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN weight TEXT")
        if "offer" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(goal_doc_requests)")}:
            self.conn.execute("ALTER TABLE goal_doc_requests ADD COLUMN offer TEXT")
        if "goal_at" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(goal_doc_requests)")}:
            self.conn.execute("ALTER TABLE goal_doc_requests ADD COLUMN goal_at REAL")
        if "analysis_doc" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN analysis_doc INTEGER")
        if "waiting" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN waiting TEXT")
            self.conn.execute("ALTER TABLE tasks ADD COLUMN waiting_since REAL")
        if "pipeline" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN pipeline TEXT")
        if "forced" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN forced INTEGER")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(autopilot)")}
        if "checking_since" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN checking_since REAL")
        if "goals_file" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN goals_file TEXT")
        if "usage_resets_at" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN usage_resets_at REAL")
        if "usage_paused_ids" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN usage_paused_ids TEXT")
        if "summary" in cols:
            # Move the development column out of the released table. Another
            # opener may finish the move before this transaction starts.
            with self.tx() as c:
                if "summary" in {r["name"] for r in c.execute("PRAGMA table_info(autopilot)")}:
                    c.execute("INSERT OR IGNORE INTO goal_summaries (root_id, summary) "
                              "SELECT root_id, summary FROM autopilot WHERE summary IS NOT NULL")
                    c.execute("ALTER TABLE autopilot DROP COLUMN summary")
        for col, kind in (("goal_sha", "TEXT"), ("auditor_id", "TEXT"), ("audit_sha", "TEXT"),
                          ("audit_ok", "INTEGER"), ("audit_history", "TEXT"), ("question", "TEXT"),
                          ("nudged_time", "REAL"), ("proposal", "TEXT"), ("external_wait", "TEXT")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE autopilot ADD COLUMN {col} {kind}")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(milestones)")}
        for col in ("checked_sha", "passed_sha", "profile", "repo"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE milestones ADD COLUMN {col} TEXT")
        for col in ("state", "state_why"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE milestones ADD COLUMN {col} TEXT")
        if "ever_passed" not in cols:
            self.conn.execute("ALTER TABLE milestones ADD COLUMN ever_passed INTEGER")
        self.conn.execute("UPDATE milestones SET ever_passed=1 WHERE ever_passed IS NULL "
                          "AND (status='passed' OR passed_sha IS NOT NULL)")
        # Rows checked before states were stored: work them out from what was recorded.
        for r in self.conn.execute("SELECT id, status, output, passed_sha FROM milestones "
                                   "WHERE state IS NULL AND status IN ('passed', 'failed')").fetchall():
            state, why = check_state(r["status"] == "passed", r["output"] or "",
                                     r["status"] == "passed" or bool(r["passed_sha"]))
            self.conn.execute("UPDATE milestones SET state=?, state_why=? WHERE id=?", (state, why, r["id"]))
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(login_state)")}
        for col in ("interrupted", "account"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE login_state ADD COLUMN {col} TEXT")
        cache_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usage_cache)")}
        if "inode" not in cache_cols:
            self.conn.execute("ALTER TABLE usage_cache ADD COLUMN inode INTEGER")
        mark_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(history_usage_mark)")}
        if "transcript_path" not in mark_cols:
            self.conn.execute("ALTER TABLE history_usage_mark ADD COLUMN transcript_path TEXT")
        inbox_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(inbox)")}
        for col, kind in (("held", "INTEGER"), ("noticed_at", "REAL"),
                          ("supervisor_context", "INTEGER")):
            if col not in inbox_cols:
                self.conn.execute(f"ALTER TABLE inbox ADD COLUMN {col} {kind}")
        tables = {r["name"] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sidebars" not in tables:
            self.conn.execute(
                """CREATE TABLE sidebars (
                    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    pane TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
        pool_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(pool_entries)")}
        for col, kind in (("port_base", "INTEGER"), ("ready", "INTEGER NOT NULL DEFAULT 1")):
            if col not in pool_cols:
                self.conn.execute(f"ALTER TABLE pool_entries ADD COLUMN {col} {kind}")
        routing_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(routing_decisions)")}
        if "prior" not in routing_cols:
            self.conn.execute("ALTER TABLE routing_decisions ADD COLUMN prior INTEGER NOT NULL DEFAULT 0")
        for col, kind in (("demoted_from", "TEXT"), ("budget_state", "TEXT"), ("seeded_at", "REAL")):
            if col not in routing_cols:
                self.conn.execute(f"ALTER TABLE routing_decisions ADD COLUMN {col} {kind}")
        request_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(permission_requests)")}
        for col, kind in (("suggestions", "TEXT"), ("channel", "TEXT"), ("answer", "TEXT"),
                          ("hold_until", "REAL")):
            if col not in request_cols:
                self.conn.execute(f"ALTER TABLE permission_requests ADD COLUMN {col} {kind}")
        self._move_session_repo_branches()
        self._move_task_wait_noted()
        if "merged_at" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(session_prs)")}:
            self.conn.execute("ALTER TABLE session_prs ADD COLUMN merged_at REAL")
        health_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(launch_health)")}
        if "token" not in health_cols:
            self.conn.execute("ALTER TABLE launch_health ADD COLUMN token TEXT NOT NULL DEFAULT ''")
        if "argv_task" not in health_cols:
            self.conn.execute("ALTER TABLE launch_health ADD COLUMN argv_task INTEGER NOT NULL DEFAULT 0")
        if "submitted_prompt" not in health_cols:
            self.conn.execute("ALTER TABLE launch_health ADD COLUMN submitted_prompt TEXT NOT NULL DEFAULT ''")
        if "launched_at" not in health_cols:
            self.conn.execute("ALTER TABLE launch_health ADD COLUMN launched_at REAL")
        if "verifying" not in health_cols:
            self.conn.execute("ALTER TABLE launch_health ADD COLUMN verifying INTEGER NOT NULL DEFAULT 0")
        if "delivery_lead" not in health_cols:
            self.conn.execute("ALTER TABLE launch_health ADD COLUMN delivery_lead TEXT")
        try:   # only brindle.kb needs FTS5: a SQLite built without it still opens
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5("
                              "text, heading_path, chunk_id UNINDEXED)")
        except sqlite3.OperationalError:
            pass

    def _move_session_repo_branches(self) -> None:
        """A development build kept the branch on session_repos itself: copy
        it to session_repo_branches and drop the column. In one transaction,
        rechecked inside it, so a second process opening at the same time
        finds the move done instead of failing on the vanished column."""
        def has_branch(c: sqlite3.Connection) -> bool:
            return "branch" in {r["name"] for r in c.execute("PRAGMA table_info(session_repos)")}

        if not has_branch(self.conn):
            return
        with self.tx() as c:
            if not has_branch(c):
                return
            c.execute("INSERT OR IGNORE INTO session_repo_branches (root_id, alias, branch) "
                      "SELECT root_id, alias, branch FROM session_repos WHERE branch IS NOT NULL")
            c.execute("ALTER TABLE session_repos DROP COLUMN branch")

    def _move_task_wait_noted(self) -> None:
        """A development build kept the long-wait note on tasks.wait_noted:
        copy it to task_notes and drop the column (same transaction pattern as
        _move_session_repo_branches)."""
        def has_col(c: sqlite3.Connection) -> bool:
            return "wait_noted" in {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}

        if not has_col(self.conn):
            return
        with self.tx() as c:
            if not has_col(c):
                return
            c.execute("INSERT OR IGNORE INTO task_notes (task_id, kind, noted) "
                      "SELECT id, 'wait', wait_noted FROM tasks WHERE wait_noted IS NOT NULL")
            c.execute("ALTER TABLE tasks DROP COLUMN wait_noted")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """Reads that see one moment of the database (a WAL read transaction
        from the first read on), whatever other connections commit meanwhile,
        without holding off their writes. Reads only; inside a transaction
        already, they share that one."""
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        finally:
            self.conn.execute("COMMIT")

    # -- workspaces --------------------------------------------------------

    def add_workspace(self, ws: Workspace) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO workspaces (id, repo_root, name, kind, branch, base_branch, path, port_base,"
                " tmux_session, created_at, session_root) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ws.id, ws.repo_root, ws.name, ws.kind, ws.branch, ws.base_branch,
                 ws.path, ws.port_base, ws.tmux_session, ws.created_at, ws.session_root),
            )
        bus.publish_ws(self, "workspace.created", ws, workspace_kind=ws.kind, branch=ws.branch)

    def set_workspace_session_root(self, workspace_id: str, session_root: str) -> None:
        """Remember which session owns work in this workspace, even after its worker exits."""
        with self.tx() as c:
            c.execute("UPDATE workspaces SET session_root = ? WHERE id = ?", (session_root, workspace_id))

    def get_workspace(self, ws_id: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return _load(Workspace, row) if row else None

    def claim_review_cap_notice(self, workspace_id: str) -> bool:
        """Claim a workspace's cap notice once, across worker replacements.

        Consult persisted state rather than a worker's round count or a
        caller's workspace snapshot; removal clears the claim by cascade.
        """
        with self.tx() as c:
            return c.execute("INSERT OR IGNORE INTO review_cap_notices (workspace_id) VALUES (?)",
                             (workspace_id,)).rowcount == 1

    def find_workspaces(self, repo_root: str | None = None) -> list[Workspace]:
        if repo_root:
            rows = self.conn.execute(
                "SELECT * FROM workspaces WHERE repo_root=? ORDER BY created_at", (repo_root,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM workspaces ORDER BY created_at")
        return [_load(Workspace, r) for r in rows]

    def workspace_by_path(self, path: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE path=?", (path,)).fetchone()
        return _load(Workspace, row) if row else None

    def workspaces_sharing(self, repo_root: str, *, path: str | None = None,
                           branch: str | None = None,
                           exclude_id: str | None = None) -> list[Workspace]:
        """Registered worktree workspaces of ``repo_root`` on ``path`` or on
        ``branch`` (either matches), other than ``exclude_id``."""
        rows = self.conn.execute(
            "SELECT * FROM workspaces WHERE repo_root=? AND kind='worktree' ORDER BY created_at",
            (repo_root,),
        )
        found = [_load(Workspace, r) for r in rows]
        return [w for w in found if w.id != exclude_id
                and ((path is not None and w.path == path)
                     or (branch is not None and w.branch == branch))]

    def workspace_by_tmux_session(self, session: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE tmux_session=?", (session,)
        ).fetchone()
        return _load(Workspace, row) if row else None

    def delete_workspace(self, ws_id: str) -> None:
        ws = self.get_workspace(ws_id)
        # The sessions this workspace is home to go with it, and their visuals.
        roots = [r[0] for r in self.conn.execute(
            "SELECT id FROM agents WHERE workspace_id=? AND parent_id IS NULL", (ws_id,))]
        with self.tx() as c:
            c.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        if ws:
            if roots:
                from brindle import visuals

                visuals.forget_sessions(self, ws.repo_root, roots)
            bus.publish_ws(self, "workspace.removed", ws, branch=ws.branch)

    # -- attached repos (brindle.repos) ---------------------------------------

    def add_session_repo(self, root_id: str, alias: str, repo_root: str, branch: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO session_repos (root_id, alias, repo_root, added_at) VALUES (?,?,?,?)",
                      (root_id, alias, repo_root, time.time()))
            if branch is not None:
                c.execute("INSERT INTO session_repo_branches (root_id, alias, branch) VALUES (?,?,?)",
                          (root_id, alias, branch))

    def remove_session_repo(self, root_id: str, alias: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM session_repos WHERE root_id=? AND alias=?", (root_id, alias))
            return cur.rowcount > 0

    def session_repos(self, root_id: str | None = None) -> list[SessionRepo]:
        """A session's attached repos in the order they were added; every
        session's when ``root_id`` is None."""
        sql = ("SELECT r.root_id, r.alias, r.repo_root, r.added_at, b.branch FROM session_repos r "
               "LEFT JOIN session_repo_branches b ON b.root_id=r.root_id AND b.alias=r.alias")
        if root_id:
            rows = self.conn.execute(sql + " WHERE r.root_id=? ORDER BY r.added_at", (root_id,))
        else:
            rows = self.conn.execute(sql + " ORDER BY r.added_at")
        return [_load(SessionRepo, r) for r in rows]

    def used_port_bases(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT port_base FROM workspaces WHERE port_base IS NOT NULL "
            "UNION SELECT port_base FROM pool_entries WHERE port_base IS NOT NULL"
        )
        return {r[0] for r in rows}

    # -- worktree pool -------------------------------------------------------

    def add_pool_entry(self, e: PoolEntry) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO pool_entries (path, repo_root, base_branch, base_sha, branch, "
                "fingerprint, port_base, ready, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (e.path, e.repo_root, e.base_branch, e.base_sha, e.branch,
                 e.fingerprint, e.port_base, e.ready, e.created_at),
            )

    def mark_pool_ready(self, path: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE pool_entries SET ready=1 WHERE path=?", (path,))

    def pool_entries(
        self, repo_root: str, base_branch: str | None = None, ready_only: bool = True
    ) -> list[PoolEntry]:
        clauses, params = ["repo_root=?"], [repo_root]
        if base_branch:
            clauses.append("base_branch=?")
            params.append(base_branch)
        if ready_only:
            clauses.append("ready=1")
        rows = self.conn.execute(
            f"SELECT * FROM pool_entries WHERE {' AND '.join(clauses)} ORDER BY created_at",
            params,
        )
        return [_load(PoolEntry, r) for r in rows]

    def count_pool_entries(self, repo_root: str, base_branch: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM pool_entries WHERE repo_root=? AND base_branch=? AND ready=1",
            (repo_root, base_branch),
        ).fetchone()
        return int(row[0])

    def take_pool_entry(self, repo_root: str, base_branch: str) -> PoolEntry | None:
        """Atomically claim (remove and return) the oldest matching ready
        entry, if any, so two concurrent creates never claim the same one."""
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM pool_entries WHERE repo_root=? AND base_branch=? AND ready=1 "
                "ORDER BY created_at LIMIT 1",
                (repo_root, base_branch),
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM pool_entries WHERE path=?", (row["path"],))
            return _load(PoolEntry, row)

    def delete_pool_entry(self, path: str) -> int:
        """Returns the number of rows deleted (0 or 1). A caller trimming or
        sweeping the pool must check this: a concurrent claim (take_pool_entry)
        may have already removed the row, in which case the worktree now
        belongs to a workspace and must not be discarded."""
        with self.tx() as c:
            cur = c.execute("DELETE FROM pool_entries WHERE path=?", (path,))
            return cur.rowcount

    def record_pool_failure(self, repo_root: str, base_branch: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO pool_failures (repo_root, base_branch, failed_at) VALUES (?,?,?) "
                "ON CONFLICT(repo_root, base_branch) DO UPDATE SET failed_at=excluded.failed_at",
                (repo_root, base_branch, time.time()),
            )

    def last_pool_failure(self, repo_root: str, base_branch: str) -> float | None:
        row = self.conn.execute(
            "SELECT failed_at FROM pool_failures WHERE repo_root=? AND base_branch=?",
            (repo_root, base_branch),
        ).fetchone()
        return float(row[0]) if row else None

    def clear_pool_failure(self, repo_root: str, base_branch: str) -> None:
        with self.tx() as c:
            c.execute(
                "DELETE FROM pool_failures WHERE repo_root=? AND base_branch=?",
                (repo_root, base_branch),
            )

    # -- session credentials -----------------------------------------------

    def set_session_credential(self, session_id: str, key: str, value: str, updated_at: float | None = None) -> None:
        self._enforce_permissions()
        ts = time.time() if updated_at is None else float(updated_at)
        with self.tx() as c:
            c.execute(
                "INSERT INTO session_credentials (session_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (session_id, key, value, ts),
            )
        self._enforce_permissions()

    def get_session_credentials(self, session_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT key, value FROM session_credentials WHERE session_id=?",
            (session_id,),
        ).fetchall()
        return {k: v for k, v in rows}

    def get_session_credentials_with_timestamps(self, session_id: str) -> dict[str, tuple[str, float]]:
        rows = self.conn.execute(
            "SELECT key, value, updated_at FROM session_credentials WHERE session_id=?",
            (session_id,),
        ).fetchall()
        return {k: (v, float(ts or 0.0)) for k, v, ts in rows}

    def clear_session_credentials(self, session_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM session_credentials WHERE session_id=?", (session_id,))


    # -- agents ------------------------------------------------------------

    def set_agent_milestone(self, agent_id: str, title: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO agent_milestones (agent_id, title) VALUES (?,?)", (agent_id, title))

    def set_task_milestone(self, task_id: str, title: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO task_milestones (task_id, title) VALUES (?,?)", (task_id, title))

    def task_milestone(self, task_id: str) -> str | None:
        row = self.conn.execute("SELECT title FROM task_milestones WHERE task_id=?", (task_id,)).fetchone()
        return row["title"] if row else None

    def agent_milestone(self, agent_id: str) -> str | None:
        row = self.conn.execute("SELECT title FROM agent_milestones WHERE agent_id=?", (agent_id,)).fetchone()
        return row["title"] if row else None

    def add_agent(self, a: Agent) -> None:
        if a.tmux_server is None:
            # An agent's window goes on the tmux server brindle is using when
            # its row is made (agents._open_window re-records it on launch,
            # for a resume on another server). Only rows an older brindle
            # made have no server, and count as the default server's.
            from brindle import tmux

            a.tmux_server = tmux.current_server()
        with self.tx() as c:
            c.execute(
                "INSERT INTO agents (id, workspace_id, profile, provider, parent_id, mode, "
                "status, tmux_window, result, created_at, status_since, task, session_ref, "
                "headless, transcript_path, done_when, inbox_socket, inbox_token, review_sha, "
                "tmux_server, config_home) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.workspace_id, a.profile, a.provider, a.parent_id, a.mode,
                 a.status, a.tmux_window, a.result, a.created_at,
                 a.status_since or a.created_at, a.task, a.session_ref, a.headless,
                 a.transcript_path, a.done_when, a.inbox_socket, a.inbox_token, a.review_sha,
                 a.tmux_server, a.config_home),
            )

    def data_version(self) -> int:
        """Changes whenever another connection commits to the database: a
        constant-time way to ask "did anything change since I last looked"."""
        return self.conn.execute("PRAGMA data_version").fetchone()[0]

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return _load(Agent, row) if row else None

    def list_agents(self, workspace_id: str | None = None) -> list[Agent]:
        if workspace_id:
            rows = self.conn.execute(
                "SELECT * FROM agents WHERE workspace_id=? ORDER BY created_at", (workspace_id,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY created_at")
        return [_load(Agent, r) for r in rows]

    def children(self, parent_id: str) -> list[Agent]:
        rows = self.conn.execute(
            "SELECT * FROM agents WHERE parent_id=? ORDER BY created_at", (parent_id,)
        )
        return [_load(Agent, r) for r in rows]

    def update_agent(self, agent_id: str, **fields: object) -> None:
        if "status" in fields:
            status = fields.pop("status")
            self.set_status(agent_id, str(status))
            if not fields:
                return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE agents SET {cols} WHERE id=?", (*fields.values(), agent_id))
        if "pipeline" in fields:   # the review / merge pipeline's step for this worker
            bus.publish_agent(self, "agent.pipeline", agent_id, pipeline=fields["pipeline"],
                              merge_failed=bool(fields.get("merge_failed")))

    def claim_note(self, agent_id: str, since: float, near: float = 0.0) -> tuple[bool, float | None]:
        """Atomically set stuck_noted to ``since`` unless it already is (or,
        with ``near``, is within that many seconds of it). Every
        brindle process sweeps (the daemon, each session's MCP server), so only
        the one that wins may send that note. Returns whether it won, and
        the value it replaced (for ``release_note``)."""
        with self.tx() as c:
            row = c.execute("SELECT stuck_noted FROM agents WHERE id=?", (agent_id,)).fetchone()
            cur = c.execute("UPDATE agents SET stuck_noted=? WHERE id=? AND stuck_noted IS NOT ?"
                            " AND (stuck_noted IS NULL OR ABS(stuck_noted - ?) >= ?)",
                            (since, agent_id, since, since, near))
        return cur.rowcount == 1, row["stuck_noted"] if row else None

    def claim_unreported(self, agent_id: str, since: float, previous: float | None,
                         *, reopened_at: float | None, status_since: float | None) -> bool:
        """Claim the stop read by the caller, without replacing a newer alert.

        Reviewer recovery clears the alert so a later stop can be handled.
        Its reopened_at changes in that same write: checking it prevents an
        old snapshot from reclaiming the cleared alert and escalating the
        reviewer just resumed. status_since also rejects a different turn.
        """
        with self.tx() as c:
            cur = c.execute("UPDATE agents SET unreported_noted=? WHERE id=? AND unreported_noted IS ?"
                            " AND reopened_at IS ? AND COALESCE(status_since, created_at) IS ?",
                            (since, agent_id, previous, reopened_at, status_since))
        return cur.rowcount == 1

    def _outbox_follow(self, c, row):
        """``row``, or the row it was folded into (canonical_id), however far."""
        while row is not None and row["state"] == "superseded" and row["canonical_id"]:
            row = c.execute("SELECT * FROM report_outbox WHERE id=?", (row["canonical_id"],)).fetchone()
        return row

    def outbox_get(self, row_id: int):
        """The row ``row_id`` now stands for (see ``_outbox_follow``)."""
        row = self.conn.execute("SELECT * FROM report_outbox WHERE id=?", (row_id,)).fetchone()
        return self._outbox_follow(self.conn, row)

    def outbox_receive(self, agent_id: str, digest: str, payload: str):
        """Record a received report, in one transaction, before anything slow
        (the branch head is bound later: ``outbox_bind``), or find the unbound
        row an earlier call of the same summary left. ``is_first``: the agent
        had no result yet."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT * FROM report_outbox WHERE agent_id=? AND digest=? AND head IS NULL"
                            " AND state IN ('received','rejected','accepted')", (agent_id, digest)).fetchone()
            if row is None:
                had = c.execute("SELECT result FROM agents WHERE id=?", (agent_id,)).fetchone()
                cur = c.execute("INSERT INTO report_outbox (agent_id, head, digest, payload, is_first,"
                                " state, created_at, updated_at) VALUES (?,NULL,?,?,?,'received',?,?)",
                                (agent_id, digest, payload, int(had is None or had["result"] is None), now, now))
                row = c.execute("SELECT * FROM report_outbox WHERE id=?", (cur.lastrowid,)).fetchone()
            return row

    def outbox_bind(self, row_id: int, head: str):
        """Bind ``head`` to a received report. If the same summary was already
        reported at that head, this row is folded into that one (superseded,
        canonical_id set; never deleted) and that one is returned. A row some
        other call already bound or folded gives the row it stands for."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM report_outbox WHERE id=?", (row_id,)).fetchone()
            if row["head"] is not None or row["state"] == "superseded":
                return self._outbox_follow(c, row)
            same = c.execute("SELECT * FROM report_outbox WHERE agent_id=? AND digest=? AND head=?",
                             (row["agent_id"], row["digest"], head)).fetchone()
            if same is not None:
                c.execute("UPDATE report_outbox SET state='superseded', canonical_id=?, updated_at=?"
                          " WHERE id=? AND head IS NULL AND state IN ('received','rejected','accepted')",
                          (same["id"], time.time(), row_id))
                return self._outbox_follow(c, same)
            c.execute("UPDATE report_outbox SET head=? WHERE id=? AND head IS NULL", (head, row_id))
            return c.execute("SELECT * FROM report_outbox WHERE id=?", (row_id,)).fetchone()

    def outbox_reject(self, row_id: int) -> bool:
        """A report that failed validation: received -> rejected. False if another
        call moved it on (the caller reloads and follows the row's state)."""
        with self.tx() as c:
            return c.execute("UPDATE report_outbox SET state='rejected', updated_at=? WHERE id=?"
                             " AND state IN ('received','rejected')", (time.time(), row_id)).rowcount == 1

    def outbox_accept(self, row_id: int, agent_id: str, payload: str) -> bool:
        """received/rejected -> accepted, and promote the report into the agent's
        result, in one transaction. Only the call that wins the transition
        writes ``agents.result``; the others get False."""
        with self.tx() as c:
            won = c.execute("UPDATE report_outbox SET state='accepted', updated_at=? WHERE id=?"
                            " AND state IN ('received','rejected')", (time.time(), row_id)).rowcount == 1
            if won:
                c.execute("UPDATE agents SET result=? WHERE id=?", (mask(payload), agent_id))
        if won:
            bus.publish_agent(self, "agent.result", agent_id)
        return won

    def outbox_deliver(self, row_id: int, *, message: tuple | None = None,
                       agent_fields: dict | None = None, nothing: bool = False) -> bool:
        """THE delivery of a report: accepted -> delivered, in ONE transaction
        with what delivers it, which must be stored: ``message`` (supervisor id,
        body, sender, held, line -> its inbox row, or with ``line`` (body -> one
        line) the full text kept as read plus the one line queued), and/or
        ``agent_fields`` (the pipeline's claim on the worker, e.g. a review),
        or ``nothing`` when there is no one to tell (the caller says so). It also
        supersedes the agent's older undelivered reports. False, writing nothing,
        unless this call won accepted -> delivered: only that call does the
        outside work (a notice, a reviewer)."""
        if message is None and not agent_fields and not nothing:
            raise ValueError("a delivery needs a message, a claim, or nothing to tell")
        from contextlib import nullcontext
        from brindle import agents, identity_lock

        # Preparation can outlive a sign-in change or a competing delivery.
        # Authorize the current recipient under the same lock as notification,
        # inside the transaction, immediately before writing any message.
        with agents.delivery_lock(message[0]) if message else nullcontext():
            return self._outbox_deliver(row_id, message, agent_fields, nothing, identity_lock)

    def _outbox_deliver(self, row_id, message, agent_fields, nothing, identity_lock) -> bool:
        message_id = None
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT * FROM report_outbox WHERE id=?", (row_id,)).fetchone()
            if row is None or row["state"] != "accepted":
                return False
            if row["fallback"] is not None:
                worker = self.get_agent(row["agent_id"])
                if (worker is None or worker.pipeline != "reviewing"
                        or row["delivery"] != f"review {worker.reviewing_since}"):
                    return False
            if message is not None:
                parent = self.get_agent(message[0])
                if parent is None:
                    return False
                identity_lock.ensure(parent)
            if c.execute("UPDATE report_outbox SET state='delivered', updated_at=? WHERE id=?"
                         " AND state='accepted'", (now, row_id)).rowcount != 1:
                return False
            ids = []
            if message is not None:
                to, body, sender, held, line = message
                cur = c.execute("INSERT INTO inbox (agent_id, sender_id, body, created_at, held, delivered_at)"
                                " VALUES (?,?,?,?,?,?)",
                                (to, sender, mask(body), now, int(held or line is not None),
                                 now if line is not None else None))
                message_id = int(cur.lastrowid)
                ids.append(f"message {message_id}")
                if line is not None:
                    c.execute("INSERT INTO inbox (agent_id, sender_id, body, created_at) VALUES (?,?,?,?)",
                              (to, sender, mask(line(message_id)), now))
            if agent_fields:
                cols = ", ".join(f"{k}=?" for k in agent_fields)
                c.execute(f"UPDATE agents SET {cols} WHERE id=?", (*agent_fields.values(), row["agent_id"]))
                if agent_fields.get("reviewing_since") is not None:
                    ids.append(f"review {agent_fields['reviewing_since']}")
                    if agent_fields.get("pipeline") == "reviewing":
                        # A new accepted review attempt replaces the previous
                        # escalation before its reviewer can escalate again.
                        c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind='pipeline-escalated'",
                                  (row["agent_id"],))
            c.execute("UPDATE report_outbox SET delivery=? WHERE id=?",
                      (", ".join(ids) or "nothing to tell", row_id))
            c.execute("UPDATE report_outbox SET state='superseded', updated_at=? WHERE agent_id=? AND id<?"
                      " AND state IN ('received','accepted')", (now, row["agent_id"], row_id))
        if message_id:
            bus.publish_agent(self, "message.queued", message[0], message_id=message_id,
                              sender_id=message[2], held=bool(message[3]))
        if agent_fields and "pipeline" in agent_fields:
            bus.publish_agent(self, "agent.pipeline", row["agent_id"], pipeline=agent_fields["pipeline"],
                              merge_failed=bool(agent_fields.get("merge_failed")))
        return True

    def outbox_review_failed(self, agent_id: str, report: float, reason: str):
        """Reopen only this review's receipt, retaining its claim until the
        fallback message is stored by outbox_deliver. A newer review wins."""
        with self.tx() as c:
            worker = self.get_agent(agent_id)
            if worker is None or worker.pipeline != "reviewing" or worker.reviewing_since != report:
                return None
            row = c.execute("SELECT * FROM report_outbox WHERE agent_id=? AND delivery=?"
                            " AND state IN ('delivered','accepted') ORDER BY id DESC LIMIT 1",
                            (agent_id, f"review {report}")).fetchone()
            if row is None:
                # A review claimed before the outbox existed still needs the
                # same durable, authorized fallback as a newly reported one.
                now = time.time()
                cur = c.execute("INSERT INTO report_outbox (agent_id, digest, payload, is_first, state,"
                                " delivery, fallback, created_at, updated_at)"
                                " VALUES (?,?,?,0,'accepted',?,?,?,?)",
                                (agent_id, f"review:{report}", worker.result or "", f"review {report}",
                                 mask(reason), now, now))
                return c.execute("SELECT * FROM report_outbox WHERE id=?", (cur.lastrowid,)).fetchone()
            c.execute("UPDATE report_outbox SET state='accepted', fallback=?, updated_at=?"
                      " WHERE id=? AND state='delivered' AND delivery=?",
                      (mask(reason), time.time(), row["id"], f"review {report}"))
            return c.execute("SELECT * FROM report_outbox WHERE id=?", (row["id"],)).fetchone()

    def outbox_stuck(self, before: float) -> list:
        """Reports received or accepted but not delivered since before ``before``."""
        return self.conn.execute("SELECT * FROM report_outbox WHERE state IN ('received','accepted')"
                                 " AND updated_at < ? ORDER BY id", (before,)).fetchall()

    def outbox_last_delivered_head(self, agent_id: str) -> str | None:
        row = self.conn.execute("SELECT head FROM report_outbox WHERE agent_id=? AND state='delivered'"
                                " AND head != '' ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
        return row["head"] if row else None

    def mark(self, agent_id: str, kind: str) -> str | None:
        """The value of ``agent_id``'s ``agent_marks`` row of ``kind``, if any."""
        row = self.conn.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                                (agent_id, kind)).fetchone()
        return row["value"] if row else None

    def set_mark(self, agent_id: str, kind: str, value: str | None) -> None:
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind=?", (agent_id, kind))
            else:
                c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,?,?) "
                          "ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                          (agent_id, kind, value))

    def claim_mark(self, agent_id: str, kind: str, value: str) -> tuple[bool, str | None]:
        """Like ``claim_note``, for a mark: atomically set it to ``value``
        unless it already is, so only one sweeping process sends that notice.
        Returns whether it won, and the value it replaced (for ``set_mark``
        to undo it when the notice couldn't be sent)."""
        with self.tx() as c:
            row = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                            (agent_id, kind)).fetchone()
            if row and row["value"] == value:
                return False, value
            c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,?,?) "
                      "ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                      (agent_id, kind, value))
        return True, row["value"] if row else None

    def add_mark(self, agent_id: str, kind: str, value: str) -> bool:
        """Create the mark unless one exists already. Returns whether it did."""
        with self.tx() as c:
            return bool(c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,?,?) "
                                  "ON CONFLICT(agent_id, kind) DO NOTHING", (agent_id, kind, value)).rowcount)

    def replace_mark(self, agent_id: str, kind: str, old: str, new: str) -> bool:
        """Set an existing mark from ``old`` to ``new``, never recreating one
        another process retired or changed meanwhile. Returns whether it did."""
        with self.tx() as c:
            return bool(c.execute("UPDATE agent_marks SET value=? WHERE agent_id=? AND kind=? AND value=?",
                                  (new, agent_id, kind, old)).rowcount)

    def retain_unresolved(self, message_id: int, agent_id: str) -> None:
        """A partial pane delivery that may or may not have gone in (agy says
        nothing of what it took in): kept, unresolved (held=2, in neither the
        queue nor the unread mail), so it is never typed in again, offered
        again or counted delivered; read_messages(message_id=...) still shows
        it. Its pane mark is retired, so the queue moves on."""
        with self.tx() as c:
            c.execute("UPDATE inbox SET held=2, delivered_at=NULL WHERE id=? AND agent_id=?",
                      (message_id, agent_id))
            c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind='pane-delivery'", (agent_id,))

    def claim_stop_mark(self, agent_id: str, kind: str, status_since: float,
                        reopened_at: float | None) -> tuple[bool, str | None]:
        """``claim_mark`` for a notice about one stop of an idle agent, the
        one its caller read (``status_since``, ``reopened_at``): won only if
        that stop is still the agent's current one, with no result, and not
        claimed yet. A stale read of an older stop neither sends its notice
        nor overwrites a newer stop's mark. Returns whether it won and the
        value it replaced (for ``release_stop_mark``)."""
        value = repr(status_since)
        with self.tx() as c:
            current = c.execute(
                "SELECT 1 FROM agents WHERE id=? AND status='idle' AND result IS NULL"
                " AND COALESCE(status_since, created_at) IS ? AND reopened_at IS ?",
                (agent_id, status_since, reopened_at)).fetchone()
            if current is None:
                return False, None
            row = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                            (agent_id, kind)).fetchone()
            if row and row["value"] == value:
                return False, value
            c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,?,?) "
                      "ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                      (agent_id, kind, value))
        return True, row["value"] if row else None

    def release_stop_mark(self, agent_id: str, kind: str, status_since: float,
                          previous: str | None) -> None:
        """Undo a ``claim_stop_mark`` whose notice couldn't be sent, only
        while the mark is still the one it claimed."""
        with self.tx() as c:
            if previous is None:
                c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind=? AND value=?",
                          (agent_id, kind, repr(status_since)))
            else:
                c.execute("UPDATE agent_marks SET value=? WHERE agent_id=? AND kind=? AND value=?",
                          (previous, agent_id, kind, repr(status_since)))

    def release_note(self, agent_id: str, since: float, previous: float | None,
                     *, field: str = "stuck_noted") -> None:
        """Undo ``claim_note`` when the note couldn't be sent, unless a later one replaced it."""
        if field not in ("stuck_noted", "unreported_noted"):
            raise ValueError(f"Not a note field: {field}")
        with self.tx() as c:
            c.execute(f"UPDATE agents SET {field}=? WHERE id=? AND {field} IS ?",
                      (previous, agent_id, since))

    def claim_idle(self, agent_id: str) -> tuple[float, float | None] | None:
        """Atomically flip idle -> processing. Only the caller that wins may type
        into the agent's terminal, so two senders never interleave keystrokes.
        Return (this claim's status_since, the one it replaced) for
        ``unclaim_idle``, or None if already busy."""
        with self.tx() as c:
            row = c.execute("SELECT status_since FROM agents WHERE id=? AND status='idle'", (agent_id,)).fetchone()
            if row is None:
                return None
            claimed_at = time.time()
            c.execute("UPDATE agents SET status='processing', status_since=? WHERE id=?", (claimed_at, agent_id))
        bus.publish_agent(self, "agent.status", agent_id, status="processing")
        return claimed_at, row["status_since"]

    def unclaim_idle(self, agent_id: str, claim: tuple[float, float | None]) -> None:
        """Undo ``claim_idle`` when nothing was delivered: idle again, on the
        same stop as before (no turn ran, and the notices about a stop go by
        its status_since), unless a newer hook or pause has moved the status."""
        with self.tx() as c:
            changed = self._unclaim_idle(c, agent_id, claim)
        if changed:
            bus.publish_agent(self, "agent.status", agent_id, status="idle")

    @staticmethod
    def _unclaim_idle(c: sqlite3.Connection, agent_id: str, claim: tuple[float, float | None]) -> int:
        return c.execute("UPDATE agents SET status='idle', status_since=? "
                         "WHERE id=? AND status='processing' AND status_since=?",
                         (claim[1], agent_id, claim[0])).rowcount

    _STAMP = "status_since = CASE WHEN status = ? THEN status_since ELSE ? END"

    def last_activity(self, root_id: str, before: float) -> float | None:
        """When session ``root_id`` last did anything before ``before``, by
        the event bus (short-lived: None once its events are pruned)."""
        return self.conn.execute("SELECT MAX(ts) FROM events WHERE root_id=? AND ts<?",
                                 (root_id, before)).fetchone()[0]

    def set_status(self, agent_id: str, status: str, only_if: str | None = None) -> None:
        guard, args = ("", ()) if only_if is None else (" AND status=?", (only_if,))
        # A row already in ``status`` is left as is (its status_since kept),
        # so only a real change is published.
        with self.tx() as c:
            changed = c.execute(
                f"UPDATE agents SET {self._STAMP}, status=? WHERE id=? AND status IS NOT ?{guard}",
                (status, time.time(), status, agent_id, status, *args),
            ).rowcount
        if changed:
            bus.publish_agent(self, "agent.status", agent_id, status=status)

    def claim_exited(self, agent_id: str, from_status: str, failed_why: str) -> str | None:
        """Atomically mark an agent whose process is gone stopped, if it is
        still in ``from_status`` and not dismissed: 'done' when it has a
        result (read in the same statement, so a report that just landed
        counts), else 'failed' with ``failed_why``. Returns the status set,
        or None when another caller changed the row first: only the caller
        that gets a status back may act on the change."""
        with self.tx() as c:
            changed = c.execute(
                "UPDATE agents SET status_since=?, "
                "failed_why = CASE WHEN result IS NULL THEN ? ELSE failed_why END, "
                "status = CASE WHEN result IS NULL THEN 'failed' ELSE 'done' END "
                "WHERE id=? AND status=? AND dismissed_at IS NULL",
                (time.time(), failed_why, agent_id, from_status),
            ).rowcount
            status = (c.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()[0]
                      if changed else None)
        if status:
            bus.publish_agent(self, "agent.status", agent_id, status=status)
        return status

    def set_result(self, agent_id: str, result: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE agents SET result=? WHERE id=?", (mask(result), agent_id))
        bus.publish_agent(self, "agent.result", agent_id)

    def delete_agent(self, agent_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            c.execute("DELETE FROM agent_marks WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM report_outbox WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM open_items WHERE worker_id=?", (agent_id,))

    # -- open items (brindle.open_items) -----------------------------------

    def add_open_item(self, worker_id: str, supervisor_id: str, kind: str, detail: str, at: float,
                      stop_at: float) -> bool:
        """Open the worker's item. False when it already has one."""
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO open_items (worker_id, supervisor_id, kind, detail, opened_at, stop_at)"
                            " VALUES (?,?,?,?,?,?)", (worker_id, supervisor_id, kind, detail, at, stop_at))
        return cur.rowcount == 1

    def get_open_item(self, worker_id: str) -> OpenItem | None:
        row = self.conn.execute("SELECT * FROM open_items WHERE worker_id=?", (worker_id,)).fetchone()
        return _load(OpenItem, row) if row else None

    def open_items(self, supervisor_id: str | None = None) -> list[OpenItem]:
        """Oldest first, every supervisor's unless one is named."""
        sql, args = "SELECT * FROM open_items", ()
        if supervisor_id is not None:
            sql, args = sql + " WHERE supervisor_id=?", (supervisor_id,)
        return [_load(OpenItem, r) for r in self.conn.execute(sql + " ORDER BY opened_at, worker_id", args)]

    def close_open_item(self, worker_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM open_items WHERE worker_id=?", (worker_id,))
        return cur.rowcount == 1

    def set_open_item_level(self, worker_id: str, expect: int, level: int) -> bool:
        """Compare-and-set, so of the processes sweeping only one sends a notice."""
        with self.tx() as c:
            cur = c.execute("UPDATE open_items SET level=? WHERE worker_id=? AND level=?",
                            (level, worker_id, expect))
        return cur.rowcount == 1

    def claim_open_item_retire(self, worker_id: str, now: float, expired_before: float) -> bool:
        """Claim taking the worker over, unless a claim newer than
        ``expired_before`` holds it (one that old is a sweep that died)."""
        with self.tx() as c:
            cur = c.execute("UPDATE open_items SET retiring_at=? WHERE worker_id=?"
                            " AND (retiring_at IS NULL OR retiring_at < ?)", (now, worker_id, expired_before))
        return cur.rowcount == 1

    def restart_open_item(self, worker_id: str, now: float) -> None:
        """A takeover held off: the hour starts over, and the claim is let go."""
        with self.tx() as c:
            c.execute("UPDATE open_items SET opened_at=?, retiring_at=NULL WHERE worker_id=?", (now, worker_id))

    def release_open_item_retire(self, worker_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE open_items SET retiring_at=NULL WHERE worker_id=?", (worker_id,))

    # -- inbox -------------------------------------------------------------

    def enqueue(self, agent_id: str, body: str, sender_id: str | None,
                notice: bool = False) -> int:
        """Queue ``body`` for delivery. ``notice``: it is a pull-mode "new
        messages" notice (noticed_at set), dropped once those are read (read_held)."""
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO inbox (agent_id, sender_id, body, created_at, noticed_at) VALUES (?,?,?,?,?)",
                (agent_id, sender_id, mask(body), now, now if notice else None),
            )
            message_id = int(cur.lastrowid)
        if not notice:
            bus.publish_agent(self, "message.queued", agent_id, message_id=message_id,
                              sender_id=sender_id)
        return message_id

    def mark_delivered(self, message_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE inbox SET delivered_at=? WHERE id=?", (time.time(), message_id))

    def claim_delivery(self, message_id: int) -> float | None:
        """Mark one queued message delivered before a transport sends it, so no
        other path (read_messages, the waiter, a pane delivery) takes it too.
        Returns the claim's time, or None if another path already had it: one
        that delivered it, or a pane delivery typing it (its row stays
        undelivered while its mark owns it)."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT agent_id FROM inbox WHERE id=? AND delivered_at IS NULL",
                            (message_id,)).fetchone()
            if row is None or _pane_delivery_message(c, row["agent_id"]) == message_id:
                return None
            c.execute("UPDATE inbox SET delivered_at=? WHERE id=?", (now, message_id))
        return now

    def release_delivery(self, message_id: int, claimed_at: float) -> None:
        """Put back a message whose send failed, only if the claim is still this one's."""
        with self.tx() as c:
            c.execute("UPDATE inbox SET delivered_at=NULL WHERE id=? AND delivered_at=?",
                      (message_id, claimed_at))

    # -- permission requests left to the person (brindle.permissions) --

    def add_permission_request(self, agent_id: str, tool_use_id: str, request: str, reason: str,
                               suggestions: str | None = None, channel: str | None = None,
                               hold_until: float | None = None) -> None:
        """Record a request (for the inbox and the supervisor; never an answer)."""
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO permission_requests (agent_id, tool_use_id, request, "
                      "reason, created_at, suggestions, channel, hold_until) VALUES (?,?,?,?,?,?,?,?)",
                      (agent_id, tool_use_id, request, reason, time.time(), suggestions, channel, hold_until))

    def permission_requests_of(self, agent_id: str) -> list[sqlite3.Row]:
        """The agent's recorded requests, oldest first."""
        return self.conn.execute("SELECT * FROM permission_requests WHERE agent_id=? ORDER BY created_at, rowid",
                                 (agent_id,)).fetchall()

    def hook_permission_request(self, agent_id: str) -> sqlite3.Row | None:
        """The request the agent's hook holds for the app's (or its supervisor's) answer, if any."""
        return self.conn.execute("SELECT * FROM permission_requests WHERE agent_id=? "
                                 "AND channel IN ('hook', 'supervisor') "
                                 "ORDER BY created_at DESC LIMIT 1", (agent_id,)).fetchone()

    def update_permission_request(self, agent_id: str, tool_use_id: str, **fields) -> None:
        """Set ``channel``, ``hold_until`` or ``answer`` (a record of what the
        person answered: brindle never reads it as one) on a request."""
        cols = [c for c in fields if c in ("channel", "hold_until", "answer")]
        with self.tx() as c:
            c.execute(f"UPDATE permission_requests SET {', '.join(f'{k}=?' for k in cols)} "
                      "WHERE agent_id=? AND tool_use_id=?", (*(fields[k] for k in cols), agent_id, tool_use_id))

    def pop_permission_request(self, agent_id: str, matches) -> sqlite3.Row | None:
        """Remove and return the agent's oldest recorded request ``matches(row)`` holds for."""
        with self.tx() as c:
            rows = c.execute("SELECT * FROM permission_requests WHERE agent_id=? "
                             "ORDER BY created_at, rowid", (agent_id,)).fetchall()
            row = next((r for r in rows if matches(r)), None)
            if row is not None:
                c.execute("DELETE FROM permission_requests WHERE agent_id=? AND tool_use_id=?",
                          (agent_id, row["tool_use_id"]))
        return row

    def latest_permission_request(self, agent_id: str) -> sqlite3.Row | None:
        """The newest recorded request still waiting on an answer."""
        return self.conn.execute("SELECT * FROM permission_requests WHERE agent_id=? "
                                 "AND channel IS NOT 'answered' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                 (agent_id,)).fetchone()

    def clear_permission_requests(self, agent_id: str, *, keep_terminal: bool = False) -> None:
        """Forget the agent's requests, but for a subagent's that a hook still
        holds: it runs on past its worker's turn, and a hold whose row goes reads
        that as its CLI's prompt having taken a request no prompt shows
        (agents._await_app_answer). One whose hook died is 'prompt' once the
        Inbox sees its hold gone (service.inbox.pending_permissions), and goes then.
        Normal turn completion keeps terminal approvals: the person executes
        the command after reading that turn's reply. The CLI consumes them;
        failure cleanup and agent deletion still remove them."""
        with self.tx() as c:
            for row in c.execute("SELECT tool_use_id, request, channel FROM permission_requests "
                                 "WHERE agent_id=?", (agent_id,)).fetchall():
                if keep_terminal and row["channel"] == "terminal":
                    continue
                if not (row["channel"] in ("hook", "supervisor") and request_subagent(row)):
                    c.execute("DELETE FROM permission_requests WHERE agent_id=? AND tool_use_id=?",
                              (agent_id, row["tool_use_id"]))

    def pop_pending(self, agent_id: str, pane_mark: Callable[[Message], str] | None = None,
                    waiter_pid: int | None = None) -> Message | None:
        """Atomically claim the oldest undelivered message, if any. None while
        a pane delivery holds the queue (agents._resume_pane_delivery goes first).
        ``pane_mark``: the claim is a pane delivery's, whose mark (what this
        returns for the message) owns the undelivered row from here on, so a
        process that dies before its first write loses nothing. ``waiter_pid``:
        the claim is that waiter's, recorded with it (see ``waiter_claims``)."""
        with self.tx() as c:
            if _pane_delivery_message(c, agent_id) is not None:
                return None
            row = c.execute(
                "SELECT * FROM inbox WHERE agent_id=? AND delivered_at IS NULL "
                "AND COALESCE(held,0)=0 ORDER BY id LIMIT 1",
                (agent_id,),
            ).fetchone()
            if not row:
                return None
            msg = _load(Message, row)
            if pane_mark is None:
                c.execute("UPDATE inbox SET delivered_at=? WHERE id=?", (time.time(), row["id"]))
            else:
                c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,'pane-delivery',?)"
                          " ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                          (agent_id, pane_mark(msg)))
            if waiter_pid is not None:
                kind = f"{WAITER_CLAIM}{waiter_pid}"
                held = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                                 (agent_id, kind)).fetchone()
                ids = (json.loads(held["value"])["ids"] if held else []) + [msg.id]
                c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,?,?)"
                          " ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                          (agent_id, kind, json.dumps({"pid": waiter_pid, "at": time.time(), "ids": ids})))
            return msg

    def waiter_claims(self, agent_id: str | None = None) -> list[sqlite3.Row]:
        """Mail a waiter (brindle.wake) has taken and not finished handing
        over: the rows read delivered, so nothing else offers them, and this
        record is what lets them be recovered if it dies holding them."""
        sql, args = f"SELECT agent_id, kind, value FROM agent_marks WHERE kind LIKE '{WAITER_CLAIM}%'", ()
        if agent_id is not None:
            sql, args = sql + " AND agent_id=?", (agent_id,)
        return self.conn.execute(sql, args).fetchall()

    def reclaim_waiter_claim(self, agent_id: str, kind: str, value: str) -> bool:
        """Put an abandoned waiter's mail back in the queue, the same rows, if
        its claim is still ``value``."""
        with self.tx() as c:
            if not c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind=? AND value=?",
                             (agent_id, kind, value)).rowcount:
                return False
            c.executemany("UPDATE inbox SET delivered_at=NULL WHERE id=? AND agent_id=?",
                          [(i, agent_id) for i in json.loads(value)["ids"]])
        return True

    def settle_pane_delivery(self, agent_id: str, saved: str) -> bool:
        """Retire a pane delivery whose window is gone, atomically and only
        if its mark is still ``saved``: sent counts as delivered, unknown is
        kept unresolved (see ``retain_unresolved``), any other row is queued."""
        state = json.loads(saved)
        with self.tx() as c:
            if not c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind='pane-delivery' AND value=?",
                             (agent_id, saved)).rowcount:
                return False
            if state.get("stage") == "submitted":
                c.execute("UPDATE inbox SET delivered_at=? WHERE id=? AND agent_id=?",
                          (time.time(), state.get("message_id"), agent_id))
            elif state.get("turn_started"):
                c.execute("UPDATE inbox SET held=2, delivered_at=NULL WHERE id=? AND agent_id=?",
                          (state.get("message_id"), agent_id))
        return True

    def marks_of_kind(self, kind: str) -> list[sqlite3.Row]:
        """Every ``agent_marks`` row of ``kind``."""
        return self.conn.execute("SELECT agent_id, value FROM agent_marks WHERE kind=?", (kind,)).fetchall()

    def pane_delivery_holds(self, agent_id: str) -> bool:
        """Whether a pane delivery's mark is holding ``agent_id``'s queue."""
        return _pane_delivery_message(self.conn, agent_id) is not None

    @staticmethod
    def _drop_unwritten_pane_mark(c: sqlite3.Connection, agent_id: str, message_id: int) -> None:
        """A claim given up before its first write has nothing left to resume."""
        row = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind='pane-delivery'",
                        (agent_id,)).fetchone()
        if row is not None:
            state = json.loads(row["value"])
            if state.get("message_id") == message_id and state.get("stage") is None:
                c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind='pane-delivery'", (agent_id,))

    def release_pending(self, message_id: int, agent_id: str) -> None:
        """Put a message pop_pending claimed back in the queue, as it was."""
        with self.tx() as c:
            c.execute("UPDATE inbox SET delivered_at=NULL WHERE id=? AND agent_id=?",
                      (message_id, agent_id))
            self._drop_unwritten_pane_mark(c, agent_id, message_id)

    PANE_STAGES = (None, "lead", "body", "submitted")

    def record_pane_delivery(self, message_id: int, agent_id: str, state: str,
                             create: bool = True) -> dict | None:
        """A partial external write is pending, owned by its durable pane mark.

        Publish both together so even a process exit before the caller unwinds
        leaves the message visible and unavailable to ordinary queue readers.
        ``state`` is merged into the delivery's current mark, which hooks add
        to meanwhile (an acknowledgement, a turn started): their fields kept,
        its stage never moved back. A mark that is gone is never recreated
        unless ``create`` (a delivery's first write), nor one for another
        message replaced. Returns the merged state, or None.
        """
        new = json.loads(state)
        with self.tx() as c:
            row = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind='pane-delivery'",
                            (agent_id,)).fetchone()
            if row is None and not create:
                return None
            merged = new
            if row is not None:
                old = json.loads(row["value"])
                if old.get("message_id") != message_id:
                    return None
                stage = max(old.get("stage"), new.get("stage"), key=self.PANE_STAGES.index)
                merged = {**old, **new, "stage": stage}
            if not c.execute("UPDATE inbox SET delivered_at=NULL WHERE id=? AND agent_id=?",
                             (message_id, agent_id)).rowcount:
                return None
            c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?,'pane-delivery',?)"
                      " ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                      (agent_id, json.dumps(merged)))
        return merged

    def requeue_claimed(self, message_id: int, agent_id: str, claim: tuple[float, float | None]) -> None:
        """Release the caller's failed delivery claim without creating a second message.

        Restore idle only if this delivery still owns the processing status
        (see ``unclaim_idle``); a newer hook or pause must keep its status.
        Both releases are atomic.
        """
        with self.tx() as c:
            c.execute("UPDATE inbox SET delivered_at=NULL WHERE id=? AND agent_id=?",
                      (message_id, agent_id))
            self._drop_unwritten_pane_mark(c, agent_id, message_id)
            changed = self._unclaim_idle(c, agent_id, claim)
        if changed:
            bus.publish_agent(self, "agent.status", agent_id, status="idle")

    def drop_pending(self, agent_id: str, sender_id: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM inbox WHERE agent_id=? AND sender_id=? AND delivered_at IS NULL",
                (agent_id, sender_id),
            )
            return cur.rowcount

    def recently_delivered(self, agent_id: str, body: str, within: float = 120.0) -> bool:
        """Whether ``body`` is a message brindle delivered to the agent just now,
        whole or as its one line (agents.one_line), which names its id."""
        want = body.strip()
        if not want:
            return False
        rows = self.conn.execute(
            "SELECT id, body FROM inbox WHERE agent_id=? AND delivered_at > ?",
            (agent_id, time.time() - within),
        )
        return any(r[1].strip() == want
                   or want.endswith(f"(full text: read_messages(message_id={r[0]}))") for r in rows)

    def delivered_since(self, agent_id: str, since: float) -> bool:
        """Whether brindle delivered any message to the agent after ``since``."""
        row = self.conn.execute(
            "SELECT 1 FROM inbox WHERE agent_id=? AND delivered_at > ? LIMIT 1", (agent_id, since),
        ).fetchone()
        return row is not None

    def pending_count(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND delivered_at IS NULL "
            "AND COALESCE(held,0)=0", (agent_id,)
        ).fetchone()
        return int(row[0])

    def oldest_pending(self, agent_id: str) -> float | None:
        """When the oldest message ``pending_count`` counts was queued."""
        row = self.conn.execute(
            "SELECT MIN(created_at) FROM inbox WHERE agent_id=? AND delivered_at IS NULL "
            "AND COALESCE(held,0)=0", (agent_id,)
        ).fetchone()
        return row[0]

    # Pull mode: messages held for the agent to read with read_messages.

    def enqueue_held(self, agent_id: str, body: str, sender_id: str | None,
                     publish: bool = True) -> int:
        """Keep ``body`` for read_messages. ``publish=False``: it is only the
        full text of a message delivered as another row, so no event."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO inbox (agent_id, sender_id, body, created_at, held) VALUES (?,?,?,?,1)",
                (agent_id, sender_id, mask(body), time.time()),
            )
            message_id = int(cur.lastrowid)
        if publish:
            bus.publish_agent(self, "message.queued", agent_id, message_id=message_id,
                              sender_id=sender_id, held=True)
        return message_id

    def get_message(self, agent_id: str, message_id: int) -> Message | None:
        row = self.conn.execute(
            "SELECT * FROM inbox WHERE id=? AND agent_id=?", (message_id, agent_id)
        ).fetchone()
        return _load(Message, row) if row else None

    def review_messages(self, agent_id: str, supervisor_ids: list[str]) -> list[Message]:
        """This worker's current and authenticated former supervisor updates.

        Include delivered and queued updates; delivery notices carry no spec.
        Handover marks only existing messages from the departing supervisor
        to its descendants, retaining their original sender and recipient.
        An empty current ancestry still admits those marked rows, never an
        unrelated sender's messages or later messages from a former supervisor.
        """
        placeholders = ",".join("?" for _ in supervisor_ids) or "NULL"
        rows = self.conn.execute(
            "SELECT * FROM inbox WHERE agent_id=? "
            f"AND (supervisor_context=1 OR sender_id IN ({placeholders})) "
            "AND (held=1 OR noticed_at IS NULL) ORDER BY id",
            (agent_id, *supervisor_ids),
        )
        return [_load(Message, row) for row in rows]

    def unread(self, agent_id: str) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM inbox WHERE agent_id=? AND held=1 AND delivered_at IS NULL ORDER BY id",
            (agent_id,),
        )
        return [_load(Message, r) for r in rows]

    def unread_count(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND held=1 AND delivered_at IS NULL",
            (agent_id,),
        ).fetchone()
        return int(row[0])

    def outstanding(self, agent_id: str, text: str) -> bool:
        """Whether a message containing ``text`` is still queued for the agent
        or held unread."""
        row = self.conn.execute(
            "SELECT 1 FROM inbox WHERE agent_id=? AND delivered_at IS NULL AND instr(body, ?) > 0 LIMIT 1",
            (agent_id, text),
        ).fetchone()
        return row is not None

    NOTICE_TTL = 120.0  # seconds a notice counts as outstanding before another may go

    def claim_notice(self, agent_id: str) -> list[Message] | None:
        """The unread messages a new notice should cover, marking them noticed;
        None when there are none, or a notice went out within ``NOTICE_TTL``
        (one covers everything unread, so a later message adds no second one;
        after that a lost notice is sent again)."""
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM inbox WHERE agent_id=? AND held=1 AND delivered_at IS NULL ORDER BY id",
                (agent_id,),
            ).fetchall()
            if not rows:
                return None
            last = max((r["noticed_at"] or 0.0) for r in rows)
            if last and time.time() - last < self.NOTICE_TTL:
                return None
            c.execute(
                "UPDATE inbox SET noticed_at=? WHERE agent_id=? AND held=1 AND delivered_at IS NULL",
                (time.time(), agent_id),
            )
            return [_load(Message, r) for r in rows]

    def claim_stop_reminder(self, agent_id: str) -> list[Message] | None:
        """The unread messages a Stop should remind the agent of, marking the
        reminder sent; None when there are none, or a Stop reminded it of this
        unread batch within ``NOTICE_TTL`` (read_held ends the batch). Separate from claim_notice: a notice that went out when
        the message arrived may have been lost, and the stop is the last chance
        to hand it over before the agent sits idle."""
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM inbox WHERE agent_id=? AND held=1 AND delivered_at IS NULL ORDER BY id",
                (agent_id,),
            ).fetchall()
            last = c.execute("SELECT stop_reminded_at FROM agents WHERE id=?",
                             (agent_id,)).fetchone()
            if not rows or (last and last[0] and time.time() - last[0] < self.NOTICE_TTL):
                return None
            c.execute("UPDATE agents SET stop_reminded_at=? WHERE id=?", (time.time(), agent_id))
            return [_load(Message, r) for r in rows]

    def reset_notice(self, agent_id: str) -> None:
        """Release a failed notice's claim unless its queued copy still owns it.

        A transport denial can leave the notice pending. Keep its claim so a
        later notification cannot duplicate it before flush delivers that row.
        """
        with self.tx() as c:
            c.execute(
                "UPDATE inbox SET noticed_at=NULL WHERE agent_id=? AND held=1 AND delivered_at IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM inbox WHERE agent_id=? AND COALESCE(held,0)=0"
                " AND noticed_at IS NOT NULL AND delivered_at IS NULL)",
                (agent_id, agent_id),
            )

    def read_held(self, agent_id: str) -> list[Message]:
        """Every unread held message, and every message still queued to be
        pushed (the person's, say) but the one a pane delivery is typing,
        marked read: whichever path runs first delivers each once. A Stop's
        reminder covered only the held ones (claim_stop_reminder): mail after
        them gets its own."""
        with self.tx() as c:
            typing = _pane_delivery_message(c, agent_id)
            rows = c.execute(
                "SELECT * FROM inbox WHERE agent_id=? AND delivered_at IS NULL AND (held=1 OR "
                "(COALESCE(held,0)=0 AND noticed_at IS NULL AND id IS NOT ?)) ORDER BY id",
                (agent_id, typing),
            ).fetchall()
            now = time.time()
            c.executemany("UPDATE inbox SET delivered_at=? WHERE id=?", [(now, r["id"]) for r in rows])
            c.execute("UPDATE agents SET stop_reminded_at=NULL WHERE id=?", (agent_id,))
            # A notice still queued announces only what was just read: it would wake the agent for nothing.
            c.execute("DELETE FROM inbox WHERE agent_id=? AND COALESCE(held,0)=0 "
                      "AND delivered_at IS NULL AND noticed_at IS NOT NULL", (agent_id,))
            if _pane_delivery_message(c, agent_id) is None:
                # Its message is gone (a notice just dropped, typed as far as
                # its lead): nothing is left to resume, and the queue moves on.
                c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind='pane-delivery'", (agent_id,))
            return [_load(Message, r) for r in rows]

    def message_delivered(self, message_id: int) -> bool:
        """Whether the specific message ``enqueue`` returned has since been
        delivered (by ``pop_pending`` or a reconcile-triggered flush)."""
        row = self.conn.execute(
            "SELECT delivered_at FROM inbox WHERE id=?", (message_id,)
        ).fetchone()
        return bool(row and row[0] is not None)

    # -- autopilot -----------------------------------------------------------

    def get_autopilot(self, root_id: str) -> Autopilot | None:
        row = self.conn.execute("SELECT * FROM autopilot WHERE root_id=?", (root_id,)).fetchone()
        if row is None:
            # Live sessions created before goal driving became unconditional
            # may have no row. Backfill at lookup so hooks and continuation agree.
            root = self.get_agent(root_id)
            if root and root.parent_id is None and root.mode == "interactive" \
                    and root.status not in ("paused", "done"):
                self.conn.execute(
                    "INSERT OR IGNORE INTO autopilot (root_id, enabled, created_at) "
                    "SELECT id, 1, ? FROM agents WHERE id=? AND parent_id IS NULL "
                    "AND mode='interactive' AND status NOT IN ('paused', 'done')",
                    (time.time(), root_id),
                )
                row = self.conn.execute("SELECT * FROM autopilot WHERE root_id=?", (root_id,)).fetchone()
        ap = _load(Autopilot, row) if row else None
        if ap:
            ap.enabled = 1  # Legacy off values no longer disable the goal driver.
            request = self.conn.execute("SELECT offer, goal_at FROM goal_doc_requests WHERE root_id=?",
                                        (root_id,)).fetchone()
            ap.plan_offer = request["offer"] if request else None
            ap.offer_goal_at = request["goal_at"] if request else None
            summary = self.conn.execute("SELECT summary FROM goal_summaries WHERE root_id=?", (root_id,)).fetchone()
            ap.summary = summary["summary"] if summary else None
        return ap

    def legacy_goal_owners(self) -> list[str]:
        """Sessions whose goal was loaded from a shared goals.md before each
        session had its own file (``goals_file`` set), newest first."""
        rows = self.conn.execute(
            "SELECT root_id FROM autopilot WHERE goals_file IS NOT NULL ORDER BY created_at DESC")
        return [r["root_id"] for r in rows]

    def add_autopilot(self, root_id: str, enabled: bool = True) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO autopilot (root_id, enabled, created_at) VALUES (?,?,?)",
                (root_id, int(enabled), time.time()),
            )

    def update_autopilot(self, root_id: str, **fields: object) -> None:
        """Update goal fields, joining a caller's goal-replacement transaction."""
        has_summary = "summary" in fields
        summary = fields.pop("summary", None)
        offer = fields.pop("plan_offer", "keep")
        goal_at = fields.pop("offer_goal_at", None)
        cols = ", ".join(f"{k}=?" for k in fields)
        with (nullcontext(self.conn) if self.conn.in_transaction else self.tx()) as c:
            if has_summary:
                if summary is None:
                    c.execute("DELETE FROM goal_summaries WHERE root_id=?", (root_id,))
                else:
                    c.execute("INSERT INTO goal_summaries (root_id, summary) VALUES (?,?) "
                              "ON CONFLICT(root_id) DO UPDATE SET summary=excluded.summary", (root_id, summary))
            if fields:
                c.execute(f"UPDATE autopilot SET {cols} WHERE root_id=?", (*fields.values(), root_id))
            if offer != "keep":
                c.execute("INSERT INTO goal_doc_requests (root_id, requested, offer) VALUES (?,0,?) "
                          "ON CONFLICT(root_id) DO UPDATE SET offer=excluded.offer", (root_id, offer))
            if goal_at is not None:
                c.execute("INSERT INTO goal_doc_requests (root_id, requested, goal_at) VALUES (?,0,?) "
                          "ON CONFLICT(root_id) DO UPDATE SET goal_at=excluded.goal_at", (root_id, goal_at))
        if "proposal" in fields:
            # Both the app and the terminal can settle proposed checks.
            bus.publish_agent(self, "checks.changed", root_id)

    def open_plan_offer(self, root_id: str, goal: str, offer: str) -> bool:
        """Open ``offer`` (to plan the goal first) only if ``goal`` is still the
        session's goal and it has never been offered or answered: one atomic
        write, so a delayed evaluation cannot overwrite an answer or a newer
        goal's state. (``requested`` is a column of the retired doc request.)"""
        with self.tx() as c:
            row = c.execute("SELECT goal FROM autopilot WHERE root_id=?", (root_id,)).fetchone()
            if row is None or row["goal"] != goal:
                return False
            c.execute("INSERT OR IGNORE INTO goal_doc_requests (root_id, requested) VALUES (?,0)", (root_id,))
            return c.execute("UPDATE goal_doc_requests SET offer=? WHERE root_id=? AND offer IS NULL",
                             (offer, root_id)).rowcount == 1

    def set_external_wait(self, root_id: str, wait: str) -> None:
        """A repeated declaration must leave the existing wait intact."""
        with self.tx() as c:
            c.execute("UPDATE autopilot SET external_wait=? WHERE root_id=? AND external_wait IS NULL",
                      (wait, root_id))

    def finish_external_wait(self, root_id: str, wait: str, now: float, message: str) -> bool:
        """Atomically replace a wait with its durable completion notice."""
        with self.tx() as c:
            changed = c.execute(
                "UPDATE autopilot SET external_wait=NULL, nudges=0, nudged_at=progress, nudged_time=? "
                "WHERE root_id=? AND external_wait=?", (now, root_id, wait)).rowcount
            if changed:
                c.execute("INSERT INTO inbox (agent_id, sender_id, body, created_at) VALUES (?,NULL,?,?)",
                          (root_id, mask(message), now))
            return bool(changed)

    def bump_progress(self, root_id: str) -> None:
        with (nullcontext(self.conn) if self.conn.in_transaction else self.tx()) as c:
            c.execute("UPDATE autopilot SET progress=progress+1, nudges=0 WHERE root_id=?", (root_id,))

    def set_milestones(self, root_id: str, items: list[tuple]) -> None:
        """Replace the session's milestones with ``(title, check_cmd, detail[, profile[, repo[, phase]]])`` items."""
        with (nullcontext(self.conn) if self.conn.in_transaction else self.tx()) as c:
            c.execute("DELETE FROM milestones WHERE root_id=?", (root_id,))
            for i, (title, check, detail, *rest) in enumerate(items, start=1):
                cur = c.execute(
                    "INSERT INTO milestones (root_id, position, title, check_cmd, detail, profile, repo) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (root_id, i, title, check, detail, rest[0] if rest else None,
                     rest[1] if len(rest) > 1 else None),
                )
                if len(rest) > 2 and rest[2]:
                    c.execute("INSERT INTO milestone_phases (milestone_id, phase) VALUES (?,?)",
                              (cur.lastrowid, rest[2]))

    def milestones(self, root_id: str) -> list[Milestone]:
        rows = self.conn.execute(
            "SELECT m.*, p.phase AS phase_name FROM milestones m "
            "LEFT JOIN milestone_phases p ON p.milestone_id=m.id WHERE m.root_id=? ORDER BY m.position",
            (root_id,),
        )
        return [_load(Milestone, r) for r in rows]

    def record_check(self, milestone_id: int, passed: bool, output: str,
                     sha: str | None = None, *, passed_sha: str | None = None,
                     state: str | None = None, state_why: str | None = None,
                     ever_passed: int | None = None) -> None:
        """``passed_sha`` defaults to ``sha`` on a pass and is kept on a fail.
        ``state`` (see ``check_state``) defaults to the one the output implies;
        ``status`` stays passed/failed for everything that only asks whether
        the milestone is done."""
        if state is None:
            state, state_why = check_state(passed, output, False)
        with (nullcontext(self.conn) if self.conn.in_transaction else self.tx()) as c:
            c.execute(
                "UPDATE milestones SET status=?, checked_at=?, output=?, checked_sha=?, "
                "passed_sha=COALESCE(?, passed_sha), state=?, state_why=?, "
                "ever_passed=COALESCE(?, ever_passed) WHERE id=?",
                ("passed" if passed else "failed", time.time(), output, sha,
                 passed_sha or (sha if passed else None), state, state_why,
                 1 if passed else ever_passed, milestone_id),
            )

    # -- reviews -------------------------------------------------------------

    def add_review(self, workspace_id: str, sha: str, reviewer_id: str | None,
                   approved: bool, summary: str) -> None:
        """Save the verdict and its review slot/start time together, so agent
        cleanup cannot change which earlier verdicts it supersedes.
        Missing reviewers leave the supersession metadata unknown."""
        with self.tx() as c:
            reviewer = c.execute("SELECT review_slot, created_at FROM agents WHERE id=?",
                                 (reviewer_id,)).fetchone()
            c.execute(
                "INSERT INTO reviews (workspace_id, sha, reviewer_id, approved, summary, created_at, "
                "review_slot, reviewer_created_at) VALUES (?,?,?,?,?,?,?,?)",
                (workspace_id, sha, reviewer_id, int(approved), mask(summary), time.time(),
                 reviewer["review_slot"] if reviewer else None,
                 reviewer["created_at"] if reviewer else None),
            )

    def latest_review(self, workspace_id: str, sha: str) -> Review | None:
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? AND sha=? ORDER BY id DESC LIMIT 1",
            (workspace_id, sha),
        ).fetchone()
        return _load(Review, row) if row else None

    def reviews_at(self, workspace_id: str, sha: str) -> list[Review]:
        """Each reviewer's latest review of ``sha``, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? AND sha=? ORDER BY id", (workspace_id, sha),
        ).fetchall()
        latest: dict[str | None, Review] = {}
        for row in rows:
            r = _load(Review, row)
            latest.pop(r.reviewer_id, None)
            latest[r.reviewer_id] = r
        return list(latest.values())

    def last_review(self, workspace_id: str) -> Review | None:
        """The most recent review of ``workspace_id`` at any sha, for incremental
        re-review: compare its sha against the current HEAD to see what's new."""
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        return _load(Review, row) if row else None

    def claim_review(self, workspace_id: str, sha: str, slot: str,
                     held: tuple[str | None, float] | None = None) -> float | None:
        """Claim the reviewer of ``workspace_id`` at ``sha`` (``slot``) for a
        dispatcher about to start it. Without ``held``, only an unclaimed
        commit; with it, only a claim still as ``review_claim`` read it. The
        claim's token for ``hold_review``, or None when another got it."""
        now = time.time()
        if held is None:
            cur = self.conn.execute("INSERT OR IGNORE INTO review_claims (workspace_id, sha, slot, agent_id,"
                                    " claimed_at) VALUES (?,?,?,NULL,?)",
                                    (workspace_id, sha, slot, now))
        else:
            cur = self.conn.execute(
                "UPDATE review_claims SET agent_id=NULL, claimed_at=? WHERE workspace_id=? AND sha=? "
                "AND slot=? AND agent_id IS ? AND claimed_at=?", (now, workspace_id, sha, slot, *held))
        return now if cur.rowcount == 1 else None

    def review_claim(self, workspace_id: str, sha: str, slot: str) -> tuple[str | None, float] | None:
        """``(agent_id, claimed_at)`` of the claim on that commit's reviewer, if any."""
        row = self.conn.execute("SELECT agent_id, claimed_at FROM review_claims WHERE workspace_id=? "
                                "AND sha=? AND slot=?", (workspace_id, sha, slot)).fetchone()
        return (row[0], row[1]) if row else None

    def review_slot(self, agent_id: str) -> str | None:
        """The slot of the claim ``agent_id`` was started under, if any."""
        row = self.conn.execute("SELECT slot FROM review_claims WHERE agent_id=?", (agent_id,)).fetchone()
        return row[0] if row else None

    def hold_review(self, workspace_id: str, sha: str, slot: str, token: float,
                    agent_id: str | None) -> None:
        """Name the reviewer started under the claim ``token``, or (None) give
        the claim up because none could start."""
        where = "workspace_id=? AND sha=? AND slot=? AND agent_id IS NULL AND claimed_at=?"
        if agent_id is None:
            self.conn.execute(f"DELETE FROM review_claims WHERE {where}", (workspace_id, sha, slot, token))
        else:
            self.conn.execute(f"UPDATE review_claims SET agent_id=? WHERE {where}",
                              (agent_id, workspace_id, sha, slot, token))

    def workspace_reviews(self, workspace_id: str) -> list[Review]:
        """Every review of ``workspace_id``, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? ORDER BY id", (workspace_id,))
        return [_load(Review, r) for r in rows]

    # -- merges --------------------------------------------------------------

    def set_merged(self, workspace_id: str, sha: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO merges (workspace_id, sha, merged_at) VALUES (?,?,?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET sha=excluded.sha, "
                "merged_at=excluded.merged_at",
                (workspace_id, sha, time.time()),
            )

    def merged_sha(self, workspace_id: str) -> str | None:
        """The commit ``workspace_id``'s branch was last merged at, if it was."""
        row = self.conn.execute(
            "SELECT sha FROM merges WHERE workspace_id=?", (workspace_id,)).fetchone()
        return row[0] if row else None

    # -- predicted conflicts ---------------------------------------------------

    def conflict_notice(self, workspace_id: str, other_id: str) -> list[str] | None:
        """The files the supervisor was last told these two branches both
        changed, or None if it hasn't been told about the pair."""
        import json

        a, b = sorted((workspace_id, other_id))
        row = self.conn.execute(
            "SELECT files FROM conflict_notices WHERE workspace_id=? AND other_id=?", (a, b),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_conflict_notice(self, workspace_id: str, other_id: str, files: list[str]) -> None:
        import json

        a, b = sorted((workspace_id, other_id))
        with self.tx() as c:
            c.execute(
                "INSERT INTO conflict_notices (workspace_id, other_id, files, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(workspace_id, other_id) DO UPDATE SET "
                "files=excluded.files, created_at=excluded.created_at",
                (a, b, json.dumps(list(files)), time.time()),
            )

    # -- check cache -----------------------------------------------------------

    def get_check(self, workspace_id: str, sha: str, command: str) -> CheckResult | None:
        row = self.conn.execute(
            "SELECT * FROM check_cache WHERE workspace_id=? AND sha=? AND command=?",
            (workspace_id, sha, command),
        ).fetchone()
        return _load(CheckResult, row) if row else None

    def record_task_check(self, workspace_id: str, command: str, ok: bool) -> None:
        rows = self.conn.execute(
            "SELECT t.id, t.agent_id FROM tasks t JOIN agents a ON a.id=t.agent_id "
            "WHERE a.workspace_id=? AND t.state='started'", (workspace_id,),
        ).fetchall()
        if not rows:
            return
        with self.tx() as c:
            c.executemany(
                "INSERT INTO task_check_outcomes (task_id, command, ok, checked_at) VALUES (?,?,?,?) "
                "ON CONFLICT(task_id, command) DO UPDATE SET ok=excluded.ok, checked_at=excluded.checked_at",
                [(r["id"], command, int(ok), time.time()) for r in rows],
            )
        for r in rows:
            bus.publish_agent(self, "task.checked", r["agent_id"], task_id=r["id"], command=command, ok=ok)

    def set_check(self, workspace_id: str, sha: str, command: str, ok: bool, output: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO check_cache (workspace_id, sha, command, ok, output, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id, sha, command) DO UPDATE SET "
                "ok=excluded.ok, output=excluded.output, created_at=excluded.created_at",
                (workspace_id, sha, command, int(ok), mask(output), time.time()),
            )

    def get_tree_check(self, repo_root: str, tree: str, command: str):
        return self.conn.execute(
            "SELECT ok, output FROM check_tree_cache WHERE repo_root=? AND tree=? AND command=?",
            (repo_root, tree, command),
        ).fetchone()

    def set_tree_check(self, repo_root: str, tree: str, command: str, ok: bool, output: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO check_tree_cache (repo_root, tree, command, ok, output, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(repo_root, tree, command) DO UPDATE SET "
                "ok=excluded.ok, output=excluded.output, created_at=excluded.created_at",
                (repo_root, tree, command, int(ok), mask(output), time.time()),
            )

    def check_duration(self, repo_root: str, command: str) -> float | None:
        """Seconds the last passing run of ``command`` took in ``repo_root``."""
        row = self.conn.execute(
            "SELECT seconds FROM check_durations WHERE repo_root=? AND command=?",
            (repo_root, command),
        ).fetchone()
        return row["seconds"] if row else None

    def branch_check_duration(self, repo_root: str, branch: str, command: str) -> float | None:
        """Seconds the last passing run of ``command`` took on ``branch``."""
        row = self.conn.execute(
            "SELECT seconds FROM branch_check_durations WHERE repo_root=? AND branch=? AND command=?",
            (repo_root, branch, command),
        ).fetchone()
        return row["seconds"] if row else None

    def set_check_duration(self, repo_root: str, command: str, seconds: float,
                           branch: str | None = None) -> None:
        with self.tx() as c:
            if branch is not None:
                c.execute(
                    "INSERT INTO branch_check_durations (repo_root, branch, command, seconds, updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(repo_root, branch, command) DO UPDATE SET "
                    "seconds=excluded.seconds, updated_at=excluded.updated_at",
                    (repo_root, branch, command, seconds, time.time()),
                )
            c.execute(
                "INSERT INTO check_durations (repo_root, command, seconds, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(repo_root, command) DO UPDATE SET "
                "seconds=excluded.seconds, updated_at=excluded.updated_at",
                (repo_root, command, seconds, time.time()),
            )

    # -- native subagents ----------------------------------------------------

    def start_native_subagent(self, sub_id: str, parent_id: str, agent_type: str | None) -> None:
        """Record a SubagentStart. One write: also prunes ``parent_id``'s own
        ended rows (older than NATIVE_SUBAGENT_PRUNE_AFTER) and abandoned
        still-"running" rows (older than NATIVE_SUBAGENT_STALE, e.g. a crash
        that skipped SubagentStop), so this table doesn't grow forever."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "DELETE FROM native_subagents WHERE parent_id=? AND "
                "((ended_at IS NOT NULL AND ended_at<?) OR (ended_at IS NULL AND started_at<?))",
                (parent_id, now - NATIVE_SUBAGENT_PRUNE_AFTER, now - NATIVE_SUBAGENT_STALE),
            )
            c.execute(
                "INSERT OR REPLACE INTO native_subagents (id, parent_id, agent_type, started_at, ended_at) "
                "VALUES (?,?,?,?,NULL)",
                (sub_id, parent_id, agent_type, now),
            )

    def stop_native_subagent(self, sub_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE native_subagents SET ended_at=? WHERE id=?", (time.time(), sub_id))

    def end_native_subagents(self, parent_id: str) -> None:
        """Mark every still-running native subagent of ``parent_id`` as ended:
        for when the parent itself stops (paused or killed), since a dead
        parent's own SubagentStop hooks will never fire."""
        with self.tx() as c:
            c.execute(
                "UPDATE native_subagents SET ended_at=? WHERE parent_id=? AND ended_at IS NULL",
                (time.time(), parent_id),
            )

    def native_subagents(self, parent_id: str) -> list[NativeSubagent]:
        rows = self.conn.execute(
            "SELECT * FROM native_subagents WHERE parent_id=? ORDER BY started_at", (parent_id,)
        )
        return [_load(NativeSubagent, r) for r in rows]

    # -- cloud login (brindle.login_pause) -------------------------------------

    def get_login_state(self, root_id: str) -> LoginState | None:
        row = self.conn.execute("SELECT * FROM login_state WHERE root_id=?", (root_id,)).fetchone()
        return _load(LoginState, row) if row else None

    def set_login_state(self, root_id: str, **fields: object) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        update = ", ".join(f"{k}=excluded.{k}" for k in fields)
        with self.tx() as c:
            c.execute(f"INSERT INTO login_state (root_id, {cols}) VALUES (?, {marks}) "
                      f"ON CONFLICT(root_id) DO UPDATE SET {update}", (root_id, *fields.values()))

    # -- sidebar (one `brindle watch --sidebar` pane per session root) --------

    def get_sidebar_pane(self, root_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT pane FROM sidebars WHERE root_id=?", (root_id,)
        ).fetchone()
        return row["pane"] if row else None

    def set_sidebar_pane(self, root_id: str, pane: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sidebars (root_id, pane, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(root_id) DO UPDATE SET pane=excluded.pane, updated_at=excluded.updated_at",
                (root_id, pane, time.time()),
            )

    def clear_sidebar_pane(self, root_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM sidebars WHERE root_id=?", (root_id,))

    def all_native_subagents(self) -> dict[str, list[NativeSubagent]]:
        """Every native subagent worth showing, grouped by parent id: one
        bounded query for a whole dashboard snapshot instead of one per agent."""
        now = time.time()
        rows = self.conn.execute(
            "SELECT * FROM native_subagents WHERE (ended_at IS NULL AND started_at>?) "
            "OR (ended_at IS NOT NULL AND ended_at>?) ORDER BY started_at",
            (now - NATIVE_SUBAGENT_STALE, now - NATIVE_SUBAGENT_PRUNE_AFTER),
        )
        out: dict[str, list[NativeSubagent]] = {}
        for r in rows:
            sub = _load(NativeSubagent, r)
            out.setdefault(sub.parent_id, []).append(sub)
        return out

    # -- usage cache (brindle.usage) --------------------------------------------

    def get_usage_cache(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM usage_cache WHERE path=?", (path,)).fetchone()

    def set_usage_cache(self, path: str, size: int, input_tokens: int, output_tokens: int,
                        cache_read_tokens: int, cache_creation_tokens: int,
                        model: str | None, last_message_id: str | None,
                        inode: int | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO usage_cache (path, size, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, model, last_message_id, updated_at, "
                "inode) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "cache_read_tokens=excluded.cache_read_tokens, "
                "cache_creation_tokens=excluded.cache_creation_tokens, model=excluded.model, "
                "last_message_id=excluded.last_message_id, updated_at=excluded.updated_at, "
                "inode=excluded.inode",
                (path, size, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                 model, last_message_id, time.time(), inode),
            )

    # -- history (brindle.history) ----------------------------------------------

    def add_history(self, repo_root: str, kind: str, *, agent_id: str | None = None,
                    branch: str | None = None, profile: str | None = None,
                    task: str | None = None, result: str | None = None,
                    tokens: str | None = None,
                    mark: tuple[str | None, int, int, int, int] | None = None) -> None:
        """Append a row, and (in the same transaction, so one never happens
        without the other) advance ``agent_id``'s usage mark to ``mark`` —
        ``(transcript_path, input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens)`` — if given."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO history (repo_root, ts, kind, agent_id, branch, profile, task, "
                "result, tokens) VALUES (?,?,?,?,?,?,?,?,?)",
                (repo_root, time.time(), kind, agent_id, branch, profile, mask(task), mask(result), tokens),
            )
            if mark is not None:
                transcript_path, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens = mark
                c.execute(
                    "INSERT OR REPLACE INTO history_usage_mark (agent_id, transcript_path, "
                    "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) "
                    "VALUES (?,?,?,?,?,?)",
                    (agent_id, transcript_path, input_tokens, output_tokens, cache_read_tokens,
                     cache_creation_tokens),
                )

    def history_after(self, repo_root: str, kind: str, after_id: int) -> list[HistoryEntry]:
        """``repo_root``'s ``kind`` rows newer than ``after_id``, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM history WHERE repo_root=? AND kind=? AND id>? ORDER BY id",
            (repo_root, kind, after_id))
        return [_load(HistoryEntry, r) for r in rows]

    def branch_worker_profile(self, repo_root: str, branch: str, before_id: int) -> str | None:
        """The profile of the worker that last reported on ``branch`` before
        history row ``before_id``."""
        row = self.conn.execute(
            "SELECT profile FROM history WHERE repo_root=? AND branch=? AND kind='worker_result' "
            "AND id<? AND profile IS NOT NULL ORDER BY id DESC LIMIT 1",
            (repo_root, branch, before_id)).fetchone()
        return row["profile"] if row else None

    # -- learned rules (brindle.learned_rules) -------------------------------------

    def learned_rules(self, repo_root: str, status: str | None = None) -> list[LearnedRule]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM learned_rules WHERE repo_root=? AND status=? ORDER BY created_at, key",
                (repo_root, status))
        else:
            rows = self.conn.execute(
                "SELECT * FROM learned_rules WHERE repo_root=? ORDER BY created_at, key", (repo_root,))
        return [_load(LearnedRule, r) for r in rows]

    def get_learned_rule(self, repo_root: str, key: str) -> LearnedRule | None:
        row = self.conn.execute(
            "SELECT * FROM learned_rules WHERE repo_root=? AND key=?", (repo_root, key)).fetchone()
        return _load(LearnedRule, row) if row else None

    def save_learned_rule(self, r: LearnedRule) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO learned_rules (repo_root, key, profile, title, rule, checks, "
                "findings, branches, status, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r.repo_root, r.key, r.profile, mask(r.title), mask(r.rule), r.checks, r.findings, r.branches,
                 r.status, r.created_at, r.decided_at))

    def learned_rules_mark(self, repo_root: str) -> int:
        row = self.conn.execute(
            "SELECT last_id FROM learned_rules_mark WHERE repo_root=?", (repo_root,)).fetchone()
        return int(row["last_id"]) if row else 0

    def set_learned_rules_mark(self, repo_root: str, last_id: int) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO learned_rules_mark (repo_root, last_id) VALUES (?,?)",
                      (repo_root, last_id))

    def get_usage_mark(self, agent_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM history_usage_mark WHERE agent_id=?", (agent_id,)
        ).fetchone()

    def list_history(self, repo_root: str | None = None, kind: str | None = None,
                     limit: int = 50) -> list[HistoryEntry]:
        clauses, args = [], []
        if repo_root:
            clauses.append("repo_root=?")
            args.append(repo_root)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ?", (*args, limit)
        )
        return [_load(HistoryEntry, r) for r in rows]

    @staticmethod
    def _prune_usage_marks(c: sqlite3.Connection) -> None:
        """Drop marks for agents that are gone from ``agents`` *and* have no
        surviving ``history`` row either: nothing will ever read them again."""
        c.execute(
            "DELETE FROM history_usage_mark WHERE agent_id NOT IN (SELECT id FROM agents) "
            "AND agent_id NOT IN (SELECT agent_id FROM history WHERE agent_id IS NOT NULL)"
        )

    def prune_history(self, repo_root: str, cap: int) -> int:
        """Keep only the ``cap`` newest rows for ``repo_root``. Returns how many
        were dropped."""
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM history WHERE repo_root=? AND id NOT IN "
                "(SELECT id FROM history WHERE repo_root=? ORDER BY id DESC LIMIT ?)",
                (repo_root, repo_root, cap),
            )
            self._prune_usage_marks(c)
            return cur.rowcount

    def prune_usage_marks(self) -> int:
        """Same cleanup as `prune_history`, for callers (like session pruning)
        that delete agents without touching history."""
        with self.tx() as c:
            before = c.execute("SELECT COUNT(*) FROM history_usage_mark").fetchone()[0]
            self._prune_usage_marks(c)
            after = c.execute("SELECT COUNT(*) FROM history_usage_mark").fetchone()[0]
            return before - after

    def history_tokens(self, repo_root: str, agent_ids: list[str]) -> dict[str, list[str]]:
        """The tokens JSON of every history row of each of ``agent_ids``."""
        out: dict[str, list[str]] = {}
        for i in range(0, len(agent_ids), 500):
            chunk = agent_ids[i:i + 500]
            rows = self.conn.execute(
                "SELECT agent_id, tokens FROM history WHERE repo_root=? AND tokens IS NOT NULL "
                f"AND agent_id IN ({','.join('?' * len(chunk))})", (repo_root, *chunk))
            for r in rows:
                out.setdefault(r["agent_id"], []).append(r["tokens"])
        return out

    # -- routing decisions (brindle.savings) --------------------------------------

    def add_routing_decision(self, repo_root: str, *, task_id: str | None, agent_id: str | None,
                             weight: str | None, baseline_profile: str, profile: str,
                             learned: bool, prior: bool = False,
                             ts: float | None = None, demoted_from: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO routing_decisions (repo_root, ts, task_id, agent_id, weight, "
                "baseline_profile, profile, learned, prior, demoted_from) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (repo_root, time.time() if ts is None else ts, task_id, agent_id, weight,
                 baseline_profile, profile, int(learned), int(learned and prior), demoted_from),
            )

    def set_budget_state(self, agent_id: str, state: str | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE routing_decisions SET budget_state=? WHERE agent_id=?", (state, agent_id))

    def set_routing_agent(self, task_id: str, agent_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE routing_decisions SET agent_id=? WHERE task_id=?", (agent_id, task_id))

    def note_routing_outcome(self, agent_id: str, *, review: bool = False, escalated: bool = False,
                             outcome: str | None = None) -> None:
        """Count a review or an escalation; the first outcome recorded sticks."""
        with self.tx() as c:
            c.execute(
                "UPDATE routing_decisions SET review_rounds=review_rounds+?, "
                "escalations=escalations+?, outcome=COALESCE(outcome, ?) WHERE agent_id=?",
                (int(review), int(escalated), outcome, agent_id),
            )

    def routing_decision_for_agent(self, agent_id: str) -> RoutingDecision | None:
        row = self.conn.execute("SELECT * FROM routing_decisions WHERE agent_id=? ORDER BY id DESC LIMIT 1",
                                (agent_id,)).fetchone()
        return _load(RoutingDecision, row) if row else None

    def list_routing_decisions(self, repo_root: str | None = None) -> list[RoutingDecision]:
        where, args = ("WHERE repo_root=?", (repo_root,)) if repo_root else ("", ())
        rows = self.conn.execute(f"SELECT * FROM routing_decisions {where} ORDER BY id", args)
        return [_load(RoutingDecision, r) for r in rows]

    def unseeded_finished_decisions(self, repo_root: str) -> list[tuple[RoutingDecision, str, str | None]]:
        """Finished (merged or removed_unmerged) decisions of ``repo_root`` not yet sent by
        `brindle learning seed`, each with its task's text and files JSON ('' / None when the
        decision has no task row)."""
        rows = self.conn.execute(
            "SELECT rd.*, t.task_text AS _text, t.files AS _files, t.weight AS _tweight "
            "FROM routing_decisions rd LEFT JOIN tasks t ON t.id = rd.task_id "
            "WHERE rd.repo_root=? AND rd.seeded_at IS NULL "
            "AND rd.outcome IN ('merged', 'removed_unmerged') ORDER BY rd.id", (repo_root,))
        out = []
        for r in rows:
            d = _load(RoutingDecision, r)
            if d.weight is None:
                d.weight = r["_tweight"]
            out.append((d, r["_text"] or "", r["_files"]))
        return out

    def mark_routing_seeded(self, ids: list[int]) -> None:
        now = time.time()
        with self.tx() as c:
            c.executemany("UPDATE routing_decisions SET seeded_at=? WHERE id=?", [(now, i) for i in ids])

    def mark_agent_seeded(self, agent_id: str) -> None:
        """The live learner path sent ``agent_id``'s terminal record: seeding skips it."""
        with self.tx() as c:
            c.execute("UPDATE routing_decisions SET seeded_at=? WHERE agent_id=? AND seeded_at IS NULL",
                      (time.time(), agent_id))

    def list_routing_decisions_for_live_agents(self) -> list[RoutingDecision]:
        """Return routing decisions for agents that are currently running (not dismissed or done)."""
        rows = self.conn.execute(
            "SELECT rd.* FROM routing_decisions rd "
            "JOIN agents a ON rd.agent_id = a.id "
            "WHERE a.dismissed_at IS NULL AND a.status != 'done' "
            "ORDER BY rd.id"
        )
        return [_load(RoutingDecision, r) for r in rows]

    # -- tasks (brindle.tasks) ---------------------------------------------------

    def add_task(self, t: Task) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO tasks (id, repo_root, agent_id, caller_id, caller_ws_id, profile, "
                "task_text, mode, isolate, branch, done_when, files, depends_on, state, "
                "created_at, started_at, weight) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.repo_root, t.agent_id, t.caller_id, t.caller_ws_id, t.profile,
                 t.task_text, t.mode, int(t.isolate), t.branch, t.done_when, t.files,
                 t.depends_on, t.state, t.created_at, t.started_at, t.weight),
            )

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _load(Task, row) if row else None

    def list_tasks(self, repo_root: str | None = None, state: str | None = None) -> list[Task]:
        clauses, args = [], []
        if repo_root:
            clauses.append("repo_root=?")
            args.append(repo_root)
        if state:
            clauses.append("state=?")
            args.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM tasks {where} ORDER BY created_at", args)
        return [_load(Task, r) for r in rows]

    def update_task(self, task_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def claim_task(self, task_id: str) -> bool:
        """Move a pending task to 'starting', atomically: True for the one
        caller that got it, so two sweeps or merges can't both start it."""
        with self.tx() as c:
            return c.execute(
                "UPDATE tasks SET state='starting' WHERE id=? AND state='pending'", (task_id,)
            ).rowcount == 1

    def claim_forced_task(self, task_id: str) -> bool:
        """Move a pending task to 'starting' and mark it ``forced`` (a
        supervisor starts it despite the load), unless another forced task is
        'starting': one statement, so of two processes racing for the single
        forced-start slot only one wins. ``forced`` is its own column: no
        write of the waiting reason can erase it."""
        with self.tx() as c:
            return c.execute(
                "UPDATE tasks SET state='starting', forced=1 WHERE id=? AND state='pending' "
                "AND NOT EXISTS (SELECT 1 FROM tasks WHERE state='starting' AND forced=1)",
                (task_id,),
            ).rowcount == 1

    def release_forced_task(self, task_id: str) -> None:
        """A forced start failed: back to pending, slot freed."""
        with self.tx() as c:
            c.execute("UPDATE tasks SET state='pending', forced=NULL WHERE id=? AND state='starting'", (task_id,))

    def count_forced_tasks(self) -> int:
        """How many workers a supervisor started despite the load."""
        return self.conn.execute("SELECT COUNT(*) FROM tasks WHERE forced=1").fetchone()[0]

    def set_task_waiting(self, task_id: str, why: str, since: float) -> bool:
        """Record why pending task ``task_id`` waits for room (and when it
        began, kept once set), only while it is still pending: False when it
        was claimed, cancelled or started meanwhile, so a stale writer changes
        nothing."""
        with self.tx() as c:
            return c.execute(
                "UPDATE tasks SET waiting=?, waiting_since=COALESCE(waiting_since, ?) "
                "WHERE id=? AND state='pending'", (why, since, task_id),
            ).rowcount == 1

    def claim_wait_note(self, task_id: str, at: float) -> bool:
        """Atomically mark that the long-wait note of pending task ``task_id``
        is being sent: only the one sweep that wins may send it, once."""
        with self.tx() as c:
            return c.execute(
                "INSERT OR IGNORE INTO task_notes (task_id, kind, noted) "
                "SELECT id, 'wait', ? FROM tasks WHERE id=? AND state='pending'",
                (at, task_id)).rowcount == 1

    def release_wait_note(self, task_id: str, at: float) -> None:
        """Undo ``claim_wait_note`` when the note couldn't be sent."""
        with self.tx() as c:
            c.execute("DELETE FROM task_notes WHERE task_id=? AND kind='wait' AND noted=?",
                      (task_id, at))

    def get_reported_spend(self, agent_id: str) -> float:
        row = self.conn.execute("SELECT reported_usd FROM agent_reported_spend WHERE agent_id=?", (agent_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_reported_spend(self, agent_id: str, reported_usd: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO agent_reported_spend (agent_id, reported_usd, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET reported_usd=excluded.reported_usd, updated_at=excluded.updated_at",
                (agent_id, reported_usd, time.time())
            )
