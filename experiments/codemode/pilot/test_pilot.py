"""Regression test for code-mode pilot output character counting."""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import re
import sys
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "code_mode_summary", Path(__file__).resolve().parent / "code_mode_summary.py"
)
assert _SPEC is not None and _SPEC.loader is not None
code_mode_summary = importlib.util.module_from_spec(_SPEC)
sys.modules["code_mode_summary"] = code_mode_summary
_SPEC.loader.exec_module(code_mode_summary)

FAKE_TASKS = "1013ef62 pending developer branch=feat/a files=a.py"
FAKE_PROGRESS = (
    "Autopilot on. Goal: Pilot\n"
    "0 of 1 milestones verified.\n"
    "  ○ 1. Pilot check (check: `uv run python experiments/codemode/pilot/ground_truth.py`)"
)
FAKE_AGENTS = (
    "1bb5847a supervisor/claude idle mode=interactive ws=brindle/root branch=dev/desktop-app\n"
    "44f72646 supervisor/claude paused mode=interactive ws=brindle/root branch=dev/desktop-app"
)


def test_main_advertised_char_count_matches_complete_stdout() -> None:
    """main() must advertise the exact character count of the full emitted stdout."""
    buf = io.StringIO()
    with (
        patch.object(code_mode_summary.mcp_server, "list_tasks", return_value=FAKE_TASKS),
        patch.object(code_mode_summary.mcp_server, "get_progress", return_value=FAKE_PROGRESS),
        patch.object(code_mode_summary.mcp_server, "list_agents", return_value=FAKE_AGENTS),
        patch.object(sys, "argv", ["code_mode_summary.py"]),
        redirect_stdout(buf),
    ):
        code_mode_summary.main()

    emitted = buf.getvalue()
    match = re.search(r"\[(?:output_length|summary_length)=(\d+)\s+chars,\s*elapsed=[0-9.]+s\]\n\Z", emitted)
    assert match is not None, f"Missing stats footer in emitted stdout: {emitted!r}"
    advertised_chars = int(match.group(1))
    assert advertised_chars == len(emitted), (
        f"Advertised character count {advertised_chars} != complete emitted stdout length {len(emitted)}"
    )


if __name__ == "__main__":
    test_main_advertised_char_count_matches_complete_stdout()

