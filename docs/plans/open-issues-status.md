# Open issues: T4 status

This branch reports only T4; the supervisor owns the other tasks and phase checks.

| Task | Issue | Branch | Status (done / blocked / skipped) | Failing-before test | Full suite | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| T4 | #253 | `fix/mcp-connection-lifetime` | blocked (partial fix complete) | `tests/test_upgrade.py::test_mcp_transport_failure_delivers_recovery_without_mcp`: failed before, passed after | Not run; supervisor runs after phase | Fatal-exit recovery notice implemented. Original disconnect cause remains unverified; need incident logs to complete diagnosis. |

## Supported findings

The GitHub issue describes a mid-session disconnect but supplies no traceback or
client log. Inspection found no idle deadline in the stdio server path.
`providers.mcp_server_spec` launches `brindle mcp`; `cli.mcp` calls
`mcp_server.main`. The SDK catches ordinary tool exceptions and returns tool
errors. Its stdio writer can propagate a broken pipe out of the server loop.
Before this change, an exception escaping `mcp.run()` only stopped the Azure
refresher and propagated: no recovery message reached the agent. This establishes
the missing notification, not the cause of the original incident.

The fix logs the fatal exception and delivers reconnect instructions through
`agents._send_message`, which feeds the hook/runner delivery path. Using ordinary
`send_message` would hold the message for the unavailable MCP `read_messages`
tool. The original exception still propagates and the refresher still stops.
Delivery failures are logged without replacing the original exception.

## Evidence

- With `src/brindle/mcp_server.py` restored exactly to HEAD, ran
  `uv run pytest tests/test_upgrade.py::test_mcp_transport_failure_delivers_recovery_without_mcp -q`:
  **1 failed** at `db.pop_pending("boss") is not None` (actual value `None`).
  The test injects a fatal `BrokenPipeError` at the server runner boundary and
  exercises real database message delivery; it does not reproduce a physical
  pipe disconnect or assert message wording.
- Before the source fix, ran
  `uv run pytest tests/test_upgrade.py::test_an_upgrade_under_the_mcp_server_leaves_it_serving_the_version_it_started_with -q`:
  **1 passed**. Extended the existing real MCP client session to raise from a
  tool, receive an error result, then successfully serve two calls on the same
  connection. Survival already works; this is not failing-before fix evidence.
- After the source fix, `uv run pytest tests/test_upgrade.py -q`:
  **7 passed**. `git diff --check` passed.
- `tests/test_mcp_server.py` does not exist in this checkout. The existing MCP
  lifecycle tests and the new regression are in `tests/test_upgrade.py`.

## Remaining scope

The plan's failing-before tool-error survival test cannot be claimed: it passes
on the baseline. No speculative keepalive or reconnect loop was added. This
patch cannot report an uncatchable process termination or detect a client that
silently drops its connection. A separate investigation needs the incident's
client/server logs to establish that trigger and reproduce it deterministically.
Keep #253 open until that evidence or a confirmed matching fatal exception is
available. Shared file overlap was reported: T2 also touches `mcp_server.py`,
while this patch only adds the logging import and fatal-exit handling.
