"""Ground truth computation for the code-mode pilot experiment.

Computes the reference answers directly from the saved read-only tool captures
under experiments/codemode/pilot/captures/ (no live tool calls or model involved)
and prints them.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

CAPTURES_DIR = Path(__file__).resolve().parent / "captures"

EMPTY_TASK_SENTINELS = frozenset({
    "No pending or cancelled tasks.",
    "No queued or cancelled tasks.",
    "No tasks.",
})


def load_run_captures(run_name: str) -> dict[str, str]:
    """Load saved read-only tool outputs for a measured run from captures/."""
    run_dir = CAPTURES_DIR / run_name
    loaded: dict[str, str] = {}
    for tool_name in ("list_tasks", "get_progress", "list_agents"):
        json_path = run_dir / f"{tool_name}.json"
        txt_path = run_dir / f"{tool_name}.txt"
        if json_path.is_file():
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            loaded[tool_name] = payload["output"] if isinstance(payload, dict) else str(payload)
        elif txt_path.is_file():
            loaded[tool_name] = txt_path.read_text(encoding="utf-8")
        else:
            raise FileNotFoundError(f"Missing saved capture file: {json_path}")
    stdout_json = run_dir / "stdout.json"
    stdout_txt = run_dir / "stdout.txt"
    if stdout_json.is_file():
        payload = json.loads(stdout_json.read_text(encoding="utf-8"))
        loaded["stdout"] = payload["output"] if isinstance(payload, dict) else str(payload)
    elif stdout_txt.is_file():
        loaded["stdout"] = stdout_txt.read_text(encoding="utf-8")
    return loaded


def compute_q1_queued_tasks(tasks_raw: str) -> int:
    """Compute how many tasks are queued (pending or held)."""
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


def compute_q2_unverified_milestones(progress_raw: str) -> list[dict[str, Any]]:
    """Compute which milestones are unverified and their checks."""
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


def compute_q3_active_agents(agents_raw: str) -> dict[str, Any]:
    """Compute how many agents are active."""
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


def compute_run_reference(run_name: str) -> dict[str, Any]:
    """Compute reference answers and character counts for a single measured run."""
    captures = load_run_captures(run_name)
    tasks_raw = captures["list_tasks"]
    progress_raw = captures["get_progress"]
    agents_raw = captures["list_agents"]

    q1 = compute_q1_queued_tasks(tasks_raw)
    q2 = compute_q2_unverified_milestones(progress_raw)
    q3 = compute_q3_active_agents(agents_raw)

    tool_chars = {
        "list_tasks": len(tasks_raw),
        "get_progress": len(progress_raw),
        "list_agents": len(agents_raw),
    }
    result: dict[str, Any] = {
        "Q1_queued_tasks": q1,
        "Q2_unverified_milestones": q2,
        "Q3_active_agents": q3["active_count"],
        "Q3_details": q3,
        "tool_output_characters": tool_chars,
        "raw_tool_total_characters": sum(tool_chars.values()),
    }
    if "stdout" in captures:
        stdout_raw = captures["stdout"]
        footer_match = re.search(r"\[output_length=(\d+)\s+chars,", stdout_raw)
        if footer_match:
            reported_len = int(footer_match.group(1))
            assert reported_len == len(stdout_raw), (
                f"Footer output_length={reported_len} != actual stdout length {len(stdout_raw)}"
            )
        result["model_visible_output_characters"] = len(stdout_raw)
    else:
        result["model_visible_output_characters"] = sum(tool_chars.values())
    return result


def compute_ground_truth() -> dict[str, Any]:
    traditional = compute_run_reference("traditional")
    code_mode = compute_run_reference("code_mode")

    assert traditional["Q1_queued_tasks"] == code_mode["Q1_queued_tasks"]
    assert traditional["Q2_unverified_milestones"] == code_mode["Q2_unverified_milestones"]
    assert traditional["Q3_active_agents"] == code_mode["Q3_active_agents"]

    trad_chars = traditional["model_visible_output_characters"]
    code_chars = code_mode["model_visible_output_characters"]
    reduction_pct = round((1.0 - (code_chars / trad_chars)) * 100.0, 2) if trad_chars else 0.0

    return {
        "Q1_queued_tasks": traditional["Q1_queued_tasks"],
        "Q2_unverified_milestones": traditional["Q2_unverified_milestones"],
        "Q3_active_agents": traditional["Q3_active_agents"],
        "runs": {
            "traditional": traditional,
            "code_mode": code_mode,
        },
        "character_counts": {
            "traditional_total": trad_chars,
            "code_mode_total": code_chars,
            "character_reduction_pct": reduction_pct,
        },
    }


def main() -> None:
    gt = compute_ground_truth()
    print("=== Ground Truth Answers (Computed From Saved Captures) ===")
    print(f"(Q1) How many tasks are queued? -> {gt['Q1_queued_tasks']}")
    if gt["Q2_unverified_milestones"]:
        for m in gt["Q2_unverified_milestones"]:
            print(
                f"(Q2) Which milestone is unverified, and what is its check? -> "
                f"Milestone {m['milestone']} ({m['title']}), check: `{m['check']}`"
            )
    else:
        print("(Q2) Which milestone is unverified, and what is its check? -> None (all verified)")
    print(f"(Q3) How many agents are active? -> {gt['Q3_active_agents']}")
    print()
    print("Structured ground truth JSON:")
    print(json.dumps(gt, indent=2))


if __name__ == "__main__":
    main()
