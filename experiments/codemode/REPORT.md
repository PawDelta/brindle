# Code-Mode Experiment Report (`exp/codemode-agy`)

## Overview

Ran [`experiments/codemode/codemode_real.py`](codemode_real.py) via `uv run python experiments/codemode/codemode_real.py` inside the Antigravity worker worktree (`BRINDLE_AGENT_ID=311d6ecb`), comparing:

1. **Traditional mode**: calling `brindle.mcp_server.list_tasks()`, `brindle.mcp_server.get_progress()`, and `brindle.mcp_server.list_agents()` separately and returning each full text payload to the model (also verified against direct `mcp_brindle_*` tool calls).
2. **Code-mode**: calling the same three functions in a single Python script, filtering the raw strings in-process, and printing only a compact summary.

## Characters Returned to the Model

| Mode / Tool | Initial Snapshot (chars) | Post-Review Snapshot (chars) |
| --- | ---: | ---: |
| Traditional: `list_tasks()` | 6,346 | 6,346 |
| Traditional: `get_progress()` | 1,028 | 1,531 |
| Traditional: `list_agents()` | 601 | 858 |
| **Traditional Total (3 tool responses)** | **7,975** | **8,735** |
| **Code-Mode Summary (single script output)** | **286** | **327** |
| **Net Reduction** | **7,689 (96.4%)** | **8,408 (96.3%)** |

- The largest contributor to traditional context bloat was `list_tasks()` (6,346 chars), which returned 27 historical cancelled tasks with full branch, dependency, and file lists when 0 tasks were actually queued (`0 pending, 0 held`).
- `get_progress()` (1,028–1,531 chars) included trailing quota and learned-rule advisory notes on top of the goal and milestone status.
- `list_agents()` (601–858 chars) included 3 paused supervisor sessions alongside the active agents.

## Summary Agreement and Renderer Validation

**Do the summaries agree?** **Yes (`summaries_agree: True`).**

- Traditional mode and code-mode use independent extraction paths (`_traditional_reference_summary` regex extraction vs. token/line parsers `parse_tasks`, `parse_progress`, `parse_agents`) and cross-check that the authoritative milestone count line (`<done> of <total> milestones verified.`) matches the number of rendered milestone rows.
- `validate_against_renderers()` in `codemode_real.py` also validates the parsers directly against `tasks.list_text` and `autopilot.progress` using an in-memory `DB(":memory:")` across:
  - Empty task output (`"No pending or cancelled tasks."`), both single-repo and multi-repo (`## <label> (<root>)`) sections.
  - Task state field parsing for `pending`, `held` (both counted in `queued`), and `cancelled`, including when branch names or `full=True` task text contain words like `pending` or `cancelled`.
  - All milestone state markers rendered by `autopilot.progress`: `○` (`pending` / `not_done`), `✓` (`passed`), `✗` (`failed` / `regressed`), `◐` (`working`), and `◌` (`waiting` / `checking`).

Code-mode output (327 chars):
```text
tasks: 0 queued (0 pending, 0 held), 27 cancelled (of 27)
progress: Autopilot on. Goal: Run a code-mode experiment on brindle's real read-only tools (agy worker), and report traditional vs code-mode output sizes. | verified=0/1 notes=5
agents: 3 active (1bb5847a:idle, 311d6ecb:processing, bd619bc9:processing), 3 paused (of 6)
```

## Failures and Observations

- **Failures**: None. Both direct MCP calls (`mcp_brindle_list_tasks`, `mcp_brindle_get_progress`, `mcp_brindle_list_agents`) and direct Python imports of `brindle.mcp_server` (`uv run python experiments/codemode/codemode_real.py`) succeeded with exit code 0 and without permission blocks.
- **First-run `uv` environment setup**: On the first `uv run python` invocation in the fresh worktree, `uv` created `.venv` and installed 49 wheels (~6 seconds, adding build progress lines to the command output). Subsequent runs with a warm `.venv` completed in ~0.3 seconds with clean output.
- **`BRINDLE_AGENT_ID` dependency**: `get_progress()` relies on `BRINDLE_AGENT_ID` being present in the environment to locate the caller's autopilot session (`311d6ecb` -> root `1bb5847a`). It was present in the worker environment; if invoked from a shell without `BRINDLE_AGENT_ID`, `get_progress()` returns `"Not running as a brindle agent."` (29 chars).
