"""Code-mode summary script for pilot experiment.

Calls read-only brindle tools, filters results in-process, and prints
a concise summary answering the pilot questions.
"""

from __future__ import annotations

import re
import time
from brindle import mcp_server


def get_summary() -> str:
    tasks_raw = mcp_server.list_tasks()
    progress_raw = mcp_server.get_progress()
    agents_raw = mcp_server.list_agents()

    # Q1: Queued tasks (pending + held)
    queued = 0
    total_tasks = 0
    for line in tasks_raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("##", "No pending", "No queued", "No tasks")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            total_tasks += 1
            if parts[1] in ("pending", "held"):
                queued += 1

    # Q2: Unverified milestones
    unverified = []
    for line in progress_raw.splitlines():
        m = re.match(r"^\s*([✓✗○◐◌·])\s+(\d+)\.\s+(.*?)\s+\(check:\s*`([^`]+)`", line)
        if m:
            mark, num, title, check = m.groups()
            if mark != "✓":
                unverified.append(f"Milestone {num} ({title}): check=`{check}`")

    # Q3: Active agents
    active = []
    paused = 0
    for line in agents_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("##") or line == "No agents.":
            continue
        parts = line.split()
        if not parts:
            continue
        agent_id = parts[0]
        status_idx = 3 if len(parts) > 3 and parts[1] == "(you)" else 2
        status = parts[status_idx] if len(parts) > status_idx else "unknown"
        if status in ("paused", "done"):
            paused += 1
        else:
            active.append(f"{agent_id}:{status}")

    summary_lines = [
        f"Q1: queued_tasks={queued} (total={total_tasks})",
        f"Q2: unverified_milestones=[{'; '.join(unverified)}]",
        f"Q3: active_agents={len(active)} (paused={paused}, active_ids={', '.join(active)})",
    ]
    return "\n".join(summary_lines)


def main() -> None:
    t0 = time.perf_counter()
    summary = get_summary()
    t1 = time.perf_counter()
    print(summary)
    # Output stats
    print(f"\n[summary_length={len(summary)} chars, elapsed={t1 - t0:.4f}s]")


if __name__ == "__main__":
    main()
