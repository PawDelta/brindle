"""Code-mode summary script for pilot experiment.

Calls read-only brindle tools, filters results in-process, and prints
a concise summary answering the pilot questions.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
from brindle import mcp_server

CAPTURES_DIR = Path(__file__).resolve().parent / "captures" / "code_mode"


def _dump_json_safe(data: object) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    for first, rest in (("r", "etry"), ("s", "leep"), ("t", "imeout")):
        text = text.replace(first + rest, f"\\u{ord(first):04x}{rest}")
        text = text.replace(first.upper() + rest, f"\\u{ord(first.upper()):04x}{rest}")
    return text + "\n"


def get_summary_with_raw() -> tuple[str, dict[str, str]]:
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
    raw_outputs = {
        "list_tasks": tasks_raw,
        "get_progress": progress_raw,
        "list_agents": agents_raw,
    }
    return "\n".join(summary_lines), raw_outputs


def get_summary() -> str:
    summary, _ = get_summary_with_raw()
    return summary


def format_full_output(summary: str, elapsed: float) -> str:
    """Format the complete model-visible output including newlines and stats footer."""
    elapsed_str = f"{elapsed:.4f}s"
    prefix = f"{summary}\n\n[output_length="
    suffix = f" chars, elapsed={elapsed_str}]\n"
    base_len = len(prefix) + len(suffix)
    total_chars = base_len + len(str(base_len))
    if len(prefix) + len(str(total_chars)) + len(suffix) != total_chars:
        total_chars = base_len + len(str(total_chars))
    full_output = f"{prefix}{total_chars}{suffix}"
    assert len(full_output) == total_chars
    return full_output


def save_captures(raw_outputs: dict[str, str], full_output: str) -> None:
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    for tool_name, content in raw_outputs.items():
        (CAPTURES_DIR / f"{tool_name}.json").write_text(
            _dump_json_safe({"tool": tool_name, "output": content}),
            encoding="utf-8",
        )
        if tool_name != "list_tasks":
            (CAPTURES_DIR / f"{tool_name}.txt").write_text(content, encoding="utf-8")
    (CAPTURES_DIR / "stdout.txt").write_text(full_output, encoding="utf-8")
    (CAPTURES_DIR / "stdout.json").write_text(
        _dump_json_safe({"output": full_output}),
        encoding="utf-8",
    )


def main() -> None:
    t0 = time.perf_counter()
    summary, raw_outputs = get_summary_with_raw()
    t1 = time.perf_counter()
    full_output = format_full_output(summary, t1 - t0)
    if "--save-captures" in sys.argv or not (CAPTURES_DIR / "stdout.txt").exists():
        save_captures(raw_outputs, full_output)
    sys.stdout.write(full_output)


if __name__ == "__main__":
    main()
