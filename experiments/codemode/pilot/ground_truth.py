"""Ground truth computation for the code-mode pilot experiment.

Computes the true answers directly from the tool outputs with no model involved,
and prints them.
"""

from __future__ import annotations

import json
import re
from typing import Any
from brindle import mcp_server

EMPTY_TASK_SENTINELS = frozenset({
    "No pending or cancelled tasks.",
    "No queued or cancelled tasks.",
    "No tasks.",
})


def compute_q1_queued_tasks(tasks_raw: str | None = None) -> int:
    """Compute how many tasks are queued (pending or held)."""
    if tasks_raw is None:
        tasks_raw = mcp_server.list_tasks()
    queued = 0
    for raw_line in tasks_raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("##", "No ")):
            continue
        if line in EMPTY_TASK_SENTINELS:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("pending", "held"):
            queued += 1
    return queued


def compute_q2_unverified_milestones(progress_raw: str | None = None) -> list[dict[str, Any]]:
    """Compute which milestones are unverified and their checks."""
    if progress_raw is None:
        progress_raw = mcp_server.get_progress()
    unverified = []
    for line in progress_raw.splitlines():
        m = re.match(r"^\s*([✓✗○◐◌·])\s+(\d+)\.\s+(.*?)\s+\(check:\s*`([^`]+)`", line)
        if m:
            mark, num, title, check = m.groups()
            if mark != "✓":
                unverified.append({
                    "milestone": int(num),
                    "title": title.strip(),
                    "check": check.strip(),
                    "mark": mark,
                })
    return unverified


def compute_q3_active_agents(agents_raw: str | None = None) -> dict[str, Any]:
    """Compute how many agents are active."""
    if agents_raw is None:
        agents_raw = mcp_server.list_agents()
    active_agents = []
    paused_agents = []
    total = 0
    for raw_line in agents_raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("##") or line == "No agents.":
            continue
        parts = line.split()
        if not parts:
            continue
        total += 1
        agent_id = parts[0]
        status_idx = 3 if len(parts) > 3 and parts[1] == "(you)" else 2
        status = parts[status_idx] if len(parts) > status_idx else "unknown"
        if status in ("paused", "done"):
            paused_agents.append(agent_id)
        else:
            active_agents.append({"id": agent_id, "status": status})

    return {
        "active_count": len(active_agents),
        "paused_count": len(paused_agents),
        "total": total,
        "active_agents": active_agents,
    }


def compute_ground_truth() -> dict[str, Any]:
    tasks_raw = mcp_server.list_tasks()
    progress_raw = mcp_server.get_progress()
    agents_raw = mcp_server.list_agents()

    q1 = compute_q1_queued_tasks(tasks_raw)
    q2 = compute_q2_unverified_milestones(progress_raw)
    q3 = compute_q3_active_agents(agents_raw)

    return {
        "Q1_queued_tasks": q1,
        "Q2_unverified_milestones": q2,
        "Q3_active_agents": q3["active_count"],
        "details": {
            "Q3_details": q3,
        },
    }


def main() -> None:
    gt = compute_ground_truth()
    print("=== Ground Truth Answers (Computed Directly From Tool Outputs) ===")
    print(f"(Q1) How many tasks are queued? -> {gt['Q1_queued_tasks']}")
    if gt["Q2_unverified_milestones"]:
        for m in gt["Q2_unverified_milestones"]:
            print(f"(Q2) Which milestone is unverified, and what is its check? -> Milestone {m['milestone']} ({m['title']}), check: `{m['check']}`")
    else:
        print("(Q2) Which milestone is unverified, and what is its check? -> None (all verified)")
    print(f"(Q3) How many agents are active? -> {gt['Q3_active_agents']}")
    print()
    print("Structured ground truth JSON:")
    print(json.dumps(gt, indent=2))


if __name__ == "__main__":
    main()
