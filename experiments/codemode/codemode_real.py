"""Code-mode experiment on real brindle read-only MCP tools."""

from __future__ import annotations

import os
from brindle import mcp_server


def parse_tasks(raw_tasks: str) -> dict[str, int]:
    if not raw_tasks or raw_tasks.strip() in ("No queued or cancelled tasks.", "No tasks."):
        return {"total": 0, "queued": 0, "cancelled": 0}
    lines = [line.strip() for line in raw_tasks.splitlines() if line.strip() and not line.startswith("## ")]
    queued = sum(1 for line in lines if " queued " in f" {line} " or " pending " in f" {line} ")
    cancelled = sum(1 for line in lines if " cancelled " in f" {line} ")
    return {"total": len(lines), "queued": queued, "cancelled": cancelled}


def parse_progress(raw_progress: str) -> dict[str, str | int]:
    lines = [line.strip() for line in raw_progress.splitlines() if line.strip()]
    if not lines:
        return {"header": "empty", "milestones": 0, "notes": 0}
    header = lines[0]
    milestone_lines = [line for line in lines if line.startswith(("○", "✓", "✗"))]
    note_lines = [line for line in lines if line.startswith("Note:")]
    return {
        "header": header,
        "milestones": len(milestone_lines),
        "notes": len(note_lines),
    }


def parse_agents(raw_agents: str) -> dict[str, int | list[str]]:
    if not raw_agents or raw_agents.strip() == "No agents.":
        return {"total": 0, "active": [], "paused": 0}
    lines = [line.strip() for line in raw_agents.splitlines() if line.strip() and not line.startswith("## ")]
    active: list[str] = []
    paused = 0
    for line in lines:
        parts = line.split()
        agent_id = parts[0]
        if " paused " in f" {line} ":
            paused += 1
        else:
            status = next((p for p in parts if p in ("processing", "idle", "starting", "done", "error", "unknown")), "active")
            active.append(f"{agent_id}:{status}")
    return {"total": len(lines), "active": active, "paused": paused}


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

    summary_data = {
        "tasks": parse_tasks(tasks_out),
        "progress": parse_progress(progress_out),
        "agents": parse_agents(agents_out),
    }
    return {
        "outputs": {
            "list_tasks": tasks_out,
            "get_progress": progress_out,
            "list_agents": agents_out,
        },
        "chars": chars,
        "summary_data": summary_data,
    }


def run_codemode() -> tuple[str, dict[str, object]]:
    """Call the three tools in one script, filter them, and return a short summary."""
    tasks_info = parse_tasks(mcp_server.list_tasks())
    progress_info = parse_progress(mcp_server.get_progress())
    agents_info = parse_agents(mcp_server.list_agents())

    active_str = ", ".join(agents_info["active"]) if agents_info["active"] else "none"
    summary_text = (
        f"tasks: {tasks_info['queued']} queued, {tasks_info['cancelled']} cancelled (of {tasks_info['total']})\n"
        f"progress: {progress_info['header']} | milestones={progress_info['milestones']} notes={progress_info['notes']}\n"
        f"agents: {len(agents_info['active'])} active ({active_str}), {agents_info['paused']} paused (of {agents_info['total']})"
    )
    summary_data = {
        "tasks": tasks_info,
        "progress": progress_info,
        "agents": agents_info,
    }
    return summary_text, summary_data


def main() -> None:
    agent_id = os.environ.get("BRINDLE_AGENT_ID")
    trad = run_traditional_mode()
    code_summary, code_data = run_codemode()

    agree = trad["summary_data"] == code_data
    trad_total = trad["chars"]["total"]
    code_chars = len(code_summary)
    reduction_pct = (1.0 - (code_chars / trad_total)) * 100.0 if trad_total else 0.0

    print("=== Environment ===")
    print(f"BRINDLE_AGENT_ID={agent_id!r}")
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


if __name__ == "__main__":
    main()
