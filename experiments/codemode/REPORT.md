# Code-Mode Experiment Report (`exp/codemode-agy`)

## Overview

Ran [`experiments/codemode/codemode_real.py`](codemode_real.py) via `uv run python experiments/codemode/codemode_real.py` inside the Antigravity worker worktree (`BRINDLE_AGENT_ID=311d6ecb`), comparing:

1. **Traditional mode**: calling `brindle.mcp_server.list_tasks()`, `brindle.mcp_server.get_progress()`, and `brindle.mcp_server.list_agents()` separately and returning each full text payload to the model (also verified against direct `mcp_brindle_*` tool calls).
2. **Code-mode**: calling the same three functions in a single Python script, filtering the raw strings in-process, and printing only a compact summary.

## Characters Returned to the Model

| Mode / Tool | Characters | Share of Traditional |
| --- | ---: | ---: |
| Traditional: `list_tasks()` | 6,346 | 79.6% |
| Traditional: `get_progress()` | 1,028 | 12.9% |
| Traditional: `list_agents()` | 601 | 7.5% |
| **Traditional Total (3 tool responses)** | **7,975** | **100.0%** |
| **Code-Mode Summary (single script output)** | **286** | **3.6%** |

- **Net reduction**: **7,689 characters saved (96.4% reduction)**.
- The largest contributor to traditional context bloat was `list_tasks()` (6,346 chars), which returned 27 historical cancelled tasks with full branch, dependency, and file lists when 0 tasks were actually queued.
- `get_progress()` (1,028 chars) included 4 trailing quota/learning advisory notes on top of the goal and milestone status.
- `list_agents()` (601 chars) included 3 paused supervisor sessions alongside the 2 active agents.

## Summary Agreement

**Do the summaries agree?** **Yes (`summaries_agree: True`).**

Both modes report the exact same state:
- **Tasks**: 0 queued, 27 cancelled (27 total).
- **Progress**: `Autopilot on. Goal: Run a code-mode experiment on brindle's real read-only tools (agy worker), and report traditional vs code-mode output sizes.` with 1 milestone (`0 of 1 milestones verified`) and 4 notes.
- **Agents**: 2 active (`1bb5847a:idle`, `311d6ecb:processing`) and 3 paused (5 total).

Code-mode output (286 chars):
```text
tasks: 0 queued, 27 cancelled (of 27)
progress: Autopilot on. Goal: Run a code-mode experiment on brindle's real read-only tools (agy worker), and report traditional vs code-mode output sizes. | milestones=1 notes=4
agents: 2 active (1bb5847a:idle, 311d6ecb:processing), 3 paused (of 5)
```

## Failures and Observations

- **Failures**: None. Both direct MCP calls (`mcp_brindle_list_tasks`, `mcp_brindle_get_progress`, `mcp_brindle_list_agents`) and direct Python imports of `brindle.mcp_server` (`uv run python experiments/codemode/codemode_real.py`) succeeded with exit code 0 and without permission blocks.
- **First-run `uv` environment setup**: On the first `uv run python` invocation in the fresh worktree, `uv` created `.venv` and installed 49 wheels (~6 seconds, adding build progress lines to the command output). Subsequent runs with a warm `.venv` completed in ~0.3 seconds with clean output.
- **`BRINDLE_AGENT_ID` dependency**: `get_progress()` relies on `BRINDLE_AGENT_ID` being present in the environment to locate the caller's autopilot session (`311d6ecb` -> root `1bb5847a`). It was present in the worker environment; if invoked from a shell without `BRINDLE_AGENT_ID`, `get_progress()` returns `"Not running as a brindle agent."` (29 chars).
