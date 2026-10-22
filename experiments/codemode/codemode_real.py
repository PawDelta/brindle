"""Code-mode experiment on real brindle read-only MCP tools."""

from __future__ import annotations

import os
import re
import time
from brindle import autopilot, mcp_server, tasks
from brindle.db import Agent, DB, Task, Workspace

EMPTY_TASK_SENTINELS = frozenset({
    "No pending or cancelled tasks.",
    "No queued or cancelled tasks.",
    "No tasks.",
})

MILESTONE_ROW_RE = re.compile(r"^\s*([✓✗○◐◌·])\s+(\d+)\.\s+")
PROGRESS_COUNT_RE = re.compile(r"^(\d+)\s+of\s+(\d+)\s+milestones\s+verified\.$")


def parse_tasks(raw_tasks: str) -> dict[str, int]:
    """Parse tasks.list_text / mcp_server.list_tasks output by task state field."""
    pending = 0
    held = 0
    cancelled = 0
    for raw_line in raw_tasks.splitlines():
        if not raw_line.strip() or raw_line.startswith((" ", "\t", "## ")):
            continue
        line = raw_line.strip()
        if line in EMPTY_TASK_SENTINELS:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        state = parts[1]
        if state == "pending":
            pending += 1
        elif state == "held":
            held += 1
        elif state == "cancelled":
            cancelled += 1
    queued = pending + held
    return {
        "total": queued + cancelled,
        "queued": queued,
        "pending": pending,
        "held": held,
        "cancelled": cancelled,
    }


def parse_progress(raw_progress: str) -> dict[str, str | int]:
    """Parse autopilot.progress / mcp_server.get_progress output."""
    raw_lines = [line for line in raw_progress.splitlines() if line.strip()]
    if not raw_lines:
        return {"header": "empty", "verified": 0, "milestones": 0, "row_count": 0, "notes": 0}
    header = raw_lines[0].strip()
    verified = 0
    count_total: int | None = None
    row_count = 0
    notes = 0
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if m := PROGRESS_COUNT_RE.match(stripped):
            verified = int(m.group(1))
            count_total = int(m.group(2))
        elif MILESTONE_ROW_RE.match(raw_line):
            row_count += 1
        elif stripped.startswith("Note:"):
            notes += 1
    milestones = count_total if count_total is not None else row_count
    return {
        "header": header,
        "verified": verified,
        "milestones": milestones,
        "row_count": row_count,
        "notes": notes,
    }


def parse_agents(raw_agents: str) -> dict[str, int | list[str]]:
    """Parse mcp_server.list_agents output."""
    if not raw_agents or raw_agents.strip() == "No agents.":
        return {"total": 0, "active": [], "paused": 0}
    active: list[str] = []
    paused = 0
    total = 0
    for raw_line in raw_agents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("## ") or line == "No agents.":
            continue
        parts = line.split()
        if not parts:
            continue
        total += 1
        agent_id = parts[0]
        # Format: <id> [(you)] <profile>/<provider> <status> mode=<mode> ...
        status_idx = 3 if len(parts) > 3 and parts[1] == "(you)" else 2
        status = parts[status_idx] if len(parts) > status_idx else "unknown"
        if status == "paused":
            paused += 1
        else:
            active.append(f"{agent_id}:{status}")
    return {"total": total, "active": active, "paused": paused}


def _traditional_reference_summary(tasks_out: str, progress_out: str, agents_out: str) -> dict[str, object]:
    """Independent regex-based extraction from the three raw tool outputs."""
    task_states = re.findall(
        r"^[0-9a-f]+\s+(pending|held|cancelled)\s+\S+\s+branch=",
        tasks_out,
        flags=re.MULTILINE,
    )
    pending = task_states.count("pending")
    held = task_states.count("held")
    cancelled = task_states.count("cancelled")
    queued = pending + held

    non_empty = [ln.strip() for ln in progress_out.splitlines() if ln.strip()]
    header = non_empty[0] if non_empty else "empty"
    m_count = re.search(r"^(\d+)\s+of\s+(\d+)\s+milestones\s+verified\.$", progress_out, flags=re.MULTILINE)
    row_matches = re.findall(r"^\s*[✓✗○◐◌·]\s+\d+\.\s+", progress_out, flags=re.MULTILINE)
    verified = int(m_count.group(1)) if m_count else 0
    milestones = int(m_count.group(2)) if m_count else len(row_matches)
    notes = len(re.findall(r"^Note:", progress_out, flags=re.MULTILINE))

    agent_rows = re.findall(
        r"^(\S+)(?:\s+\(you\))?\s+\S+/\S+\s+(\S+)\s+mode=",
        agents_out,
        flags=re.MULTILINE,
    )
    active = [f"{aid}:{st}" for aid, st in agent_rows if st != "paused"]
    paused = sum(1 for _, st in agent_rows if st == "paused")

    return {
        "tasks": {
            "total": len(task_states),
            "queued": queued,
            "pending": pending,
            "held": held,
            "cancelled": cancelled,
        },
        "progress": {
            "header": header,
            "verified": verified,
            "milestones": milestones,
            "row_count": len(row_matches),
            "notes": notes,
        },
        "agents": {
            "total": len(agent_rows),
            "active": active,
            "paused": paused,
        },
    }


def validate_against_renderers() -> None:
    """Validate task and progress parsers against real brindle renderers on in-memory DB."""
    db = DB(":memory:")
    now = time.time()
    repo_a = "/repo/a"
    repo_b = "/repo/b"
    ws = Workspace(
        id="ws-root",
        repo_root=repo_a,
        name="root",
        kind="main",
        branch="main",
        base_branch=None,
        path=repo_a,
        port_base=None,
        tmux_session="s",
        created_at=now,
    )
    db.add_workspace(ws)
    root = Agent(
        id="root0001",
        workspace_id=ws.id,
        profile="supervisor",
        provider="claude",
        parent_id=None,
        mode="interactive",
        status="idle",
        tmux_window="0",
        result=None,
        created_at=now,
    )
    db.add_agent(root)

    # 1. Empty tasks sentinel (single repo and multi-repo sections)
    empty_single = tasks.list_text(db, repo_a)
    assert empty_single == "No pending or cancelled tasks.", empty_single
    assert parse_tasks(empty_single) == {"total": 0, "queued": 0, "pending": 0, "held": 0, "cancelled": 0}
    multi_empty = f"## this repo ({repo_a})\n{empty_single}\n\n## other ({repo_b})\n{tasks.list_text(db, repo_b)}"
    assert parse_tasks(multi_empty) == {"total": 0, "queued": 0, "pending": 0, "held": 0, "cancelled": 0}

    # 2. Pending, held, and cancelled tasks (including misleading words in branch/files)
    for tid, state, branch in (
        ("aaaa1111", "pending", "fix/cancelled-word-in-branch"),
        ("bbbb2222", "held", "feat/held-task"),
        ("cccc3333", "cancelled", "feat/pending-word-in-branch"),
    ):
        db.add_task(
            Task(
                id=tid,
                repo_root=repo_a,
                agent_id=None,
                caller_id=root.id,
                caller_ws_id=ws.id,
                profile="developer",
                task_text=" touches pending and cancelled words ",
                mode="assign",
                isolate=1,
                branch=branch,
                done_when="done",
                files='["src/pending.py"]',
                depends_on='["dep"]',
                state=state,
                created_at=now,
            )
        )
    rendered_tasks = tasks.list_text(db, repo_a, full=True)
    parsed_t = parse_tasks(rendered_tasks)
    assert parsed_t == {"total": 3, "queued": 2, "pending": 1, "held": 1, "cancelled": 1}, parsed_t

    # 3. Progress renderer with ○ (pending), ✓ (passed), ✗ (failed), ◐ (working), and ◌ (waiting/checking)
    db.add_autopilot(root.id, enabled=True)
    db.update_autopilot(root.id, goal="Test all milestone markers")
    db.set_milestones(root.id, [("M1", "true", None, None, None)])
    ms = db.milestones(root.id)
    assert len(ms) == 1
    m_id = ms[0].id

    # 3a. Pending (○)
    prog_pending = autopilot.progress(db, root.id)
    assert "  ○ 1. M1" in prog_pending, prog_pending
    assert parse_progress(prog_pending)["milestones"] == 1
    assert parse_progress(prog_pending)["row_count"] == 1

    # 3b. Passed (✓)
    db.record_check(m_id, True, "ok", "sha1", passed_sha="sha1", state="passed", ever_passed=1)
    prog_passed = autopilot.progress(db, root.id)
    assert "  ✓ 1. M1" in prog_passed, prog_passed
    assert parse_progress(prog_passed) == {
        "header": "Autopilot on. Goal: Test all milestone markers",
        "verified": 1,
        "milestones": 1,
        "row_count": 1,
        "notes": 0,
    }

    # 3c. Failed while pending task exists -> ◌ (waiting on queued task)
    db.record_check(m_id, False, "fail", "sha1", state="not_done")
    prog_waiting_task = autopilot.progress(db, root.id)
    assert "  ◌ 1. M1" in prog_waiting_task, prog_waiting_task
    assert parse_progress(prog_waiting_task)["milestones"] == 1
    assert parse_progress(prog_waiting_task)["row_count"] == 1

    # 3d. Regressed/failed without pending task or active worker -> ✗
    db.update_task("aaaa1111", state="cancelled")
    db.record_check(m_id, False, "fail", "sha1", state="regressed", ever_passed=1)
    prog_failed = autopilot.progress(db, root.id)
    assert "  ✗ 1. M1" in prog_failed, prog_failed
    assert parse_progress(prog_failed)["milestones"] == 1
    assert parse_progress(prog_failed)["row_count"] == 1

    # 3d. Failed with active working worker -> ◐
    worker = Agent(
        id="work0001",
        workspace_id=ws.id,
        profile="developer",
        provider="claude",
        parent_id=root.id,
        mode="assign",
        status="processing",
        tmux_window="1",
        result=None,
        created_at=now,
    )
    db.add_agent(worker)
    prog_working = autopilot.progress(db, root.id)
    assert "  ◐ 1. M1" in prog_working, prog_working
    assert parse_progress(prog_working)["milestones"] == 1
    assert parse_progress(prog_working)["row_count"] == 1

    # 3e. Failed with waiting worker -> ◌
    db.update_agent(worker.id, status="waiting")
    prog_waiting = autopilot.progress(db, root.id)
    assert "  ◌ 1. M1" in prog_waiting, prog_waiting
    assert parse_progress(prog_waiting)["milestones"] == 1
    assert parse_progress(prog_waiting)["row_count"] == 1

    # 3f. Failed with background checking -> ◌
    db.update_autopilot(root.id, checking_since=time.time())
    prog_checking = autopilot.progress(db, root.id)
    assert "  ◌ 1. M1" in prog_checking, prog_checking
    assert parse_progress(prog_checking)["milestones"] == 1
    assert parse_progress(prog_checking)["row_count"] == 1


def run_traditional_mode() -> dict[str, object]:
    """Call list_tasks, get_progress, and list_agents separately and record chars."""
    tasks_out = mcp_server.list_tasks()
    progress_out = mcp_server.get_progress()
    agents_out = mcp_server.list_agents()

    chars = {
        "list_tasks": len(tasks_out),
        "get_progress": len(progress_out),
        "list_agents": len(agents_out),
    }
    chars["total"] = sum(chars.values())

    return {
        "outputs": {
            "list_tasks": tasks_out,
            "get_progress": progress_out,
            "list_agents": agents_out,
        },
        "chars": chars,
        "summary_data": _traditional_reference_summary(tasks_out, progress_out, agents_out),
    }


def run_codemode() -> tuple[str, dict[str, object]]:
    """Call the three tools in one script, filter them, and return a short summary."""
    tasks_info = parse_tasks(mcp_server.list_tasks())
    progress_info = parse_progress(mcp_server.get_progress())
    agents_info = parse_agents(mcp_server.list_agents())

    active_str = ", ".join(agents_info["active"]) if agents_info["active"] else "none"
    summary_text = (
        f"tasks: {tasks_info['queued']} queued ({tasks_info['pending']} pending, {tasks_info['held']} held), "
        f"{tasks_info['cancelled']} cancelled (of {tasks_info['total']})\n"
        f"progress: {progress_info['header']} | verified={progress_info['verified']}/{progress_info['milestones']} "
        f"notes={progress_info['notes']}\n"
        f"agents: {len(agents_info['active'])} active ({active_str}), "
        f"{agents_info['paused']} paused (of {agents_info['total']})"
    )
    summary_data = {
        "tasks": tasks_info,
        "progress": progress_info,
        "agents": agents_info,
    }
    return summary_text, summary_data


def main() -> None:
    validate_against_renderers()
    agent_id = os.environ.get("BRINDLE_AGENT_ID")
    trad = run_traditional_mode()
    code_summary, code_data = run_codemode()

    agree = (
        trad["summary_data"] == code_data
        and code_data["progress"]["milestones"] == code_data["progress"]["row_count"]
    )
    trad_total = trad["chars"]["total"]
    code_chars = len(code_summary)
    reduction_pct = (1.0 - (code_chars / trad_total)) * 100.0 if trad_total else 0.0

    print("=== Environment ===")
    print(f"BRINDLE_AGENT_ID={agent_id!r}")
    print("renderer_validation: passed")
    print()
    print("=== Traditional Mode Character Counts ===")
    print(f"list_tasks:   {trad['chars']['list_tasks']} chars")
    print(f"get_progress: {trad['chars']['get_progress']} chars")
    print(f"list_agents:  {trad['chars']['list_agents']} chars")
    print(f"total:        {trad_total} chars")
    print()
    print("=== Code-Mode Summary Output ===")
    print(code_summary)
    print()
    print("=== Comparison ===")
    print(f"code_mode_chars: {code_chars} chars")
    print(f"reduction:       {reduction_pct:.1f}%")
    print(f"summaries_agree: {agree}")
    assert agree, f"Mismatch between traditional {trad['summary_data']} and code-mode {code_data}"


if __name__ == "__main__":
    main()
