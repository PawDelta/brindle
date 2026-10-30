# Open issues: execution plan

This plan covers the 18 open GitHub issues on `brindle` as of 2026-10-23. It is written so a cheaper agent (for example Antigravity) can carry it out without the supervisor. Work through it one task at a time, in the order given.

## Who does what

- **Supervisor: Codex.** Chosen because Codex has documented MCP client support and a non-interactive `codex exec --json` mode, and brindle's own profiles and rules are built around Codex. Antigravity's MCP and long-running support came only from third-party wrappers, so it is the riskier supervisor. Start brindle with the Codex supervisor (`brindle -s codex`).
- **Subtle tasks: Codex worker, heavy tier.** T1, T4, T5, T6, T7, T8, T12, T13.
- **Mechanical tasks: Antigravity worker, light tier.** T2, T3, T9, T11, T14. Each has a clear failing test, so a light worker can finish it on its own.
- **Blocked: no worker.** T15, T16.
- If an Antigravity worker fails its test twice, reassign it to Codex heavy. Do not give up on the task.

## How to work (read first)

1. **Source of truth.** The issue body on GitHub is the full spec. Read it with `gh issue view <N>`. This file gives the order, the file areas and the acceptance test. If the two disagree, the issue wins, and you should note the mismatch in your report.
2. **Find the code before you read it.** Use the code map instead of listing folders:
   ```
   graphify query "<question>" --graph graphify-out/graph.json --budget 1500
   graphify explain "<symbol>" --graph graphify-out/graph.json
   graphify affected "<symbol>" --graph graphify-out/graph.json
   ```
   Paths it prints are relative to the repo root. The map can lag, so trust the files when they differ.
3. **One issue, one branch.** Branch from `main` as `fix/<short-name>` or `feat/<short-name>`. Do not mix issues on one branch.
4. **Test first for bugs.** For every issue marked BUG, write a test that reproduces the reported symptom and **fails on the unmodified code**. Run it, confirm it fails, then fix the root cause, then confirm it passes. A test that passes before your change is not evidence of a fix.
5. **Tests must earn their place** (repo rule). Add the fewest tests that would fail without your change: usually one regression test. Extend an existing test or parametrize table before adding a new test function. Do not test wording, help text, constants, defaults or enum values. Do not assert that a mock was called with what the test just gave it. Use database rows and fakes, not real tmux sessions, subprocesses or timed waits, unless timing is the thing being tested.
6. **Mock every new call.** If your change makes a function call a new function, add mocks for that new call in every existing test that patches the original function.
7. **Verify.** Run the targeted tests with `uv run pytest <file> -q`. Run the full suite once at the end of a phase with `uv run pytest -q`.
8. **Commit** on your branch with a clear message. Do not push or open a PR unless the supervisor says to. Add this trailer:
   ```
   Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>
   ```
9. **Stop and ask** (do not guess) when the task is marked **NEEDS USER**, or when the code contradicts the plan in a way the plan does not cover. Write the question in your report and keep going on the other tasks.
10. **Report** for each task: branch name, files changed, the test you added and its before/after result, the full-suite result, and anything you could not verify.

## Dependency order

```
Phase 1 (independent, any order):
  T1 #199   T2 #244   T3 #249   T4 #253   T5 #261

Phase 2 (reviewer/pipeline; run in this order):
  T6 #262 + #250 + #251   (one branch: prompt detection)
  T7 #252                 (after T6)
  T8 #186                 (after T7)

Phase 3 (providers; run in this order, shared files):
  T9 #213  ->  T10 #218  ->  T11 #214  ->  T12 #212  ->  T13 #215

Phase 4:
  T14 #221                (after T8: both touch the merge path)

Blocked:
  T15 #260  NEEDS USER (signing certificate, secrets, Windows test machine)
  T16 #194  NEEDS USER (roadmap item; confirm whether to keep open)
  T17 #261 follow-up: version-mismatch warning in the app (after T5)
  T18 #212 stretch: none. Reserved.
```

---

## Phase 1

### T1. #199: flaky test, isolated tmux server gone before `new-window` (BUG)

- **Symptom.** `tests/test_auth_failure_handling.py::test_stop_failure_payloads_with_enums_and_last_assistant_message` fails on macOS with xdist: `TmuxError: ... no server running on /private/tmp/tmux-501/brindle-test-<pid>`.
- **Likely cause.** tmux exits when its last session closes. A detached helper from an earlier test on the same xdist worker, or teardown racing a background spawn, kills the server.
- **Find.** `graphify query "isolated tmux server test fixture" --graph graphify-out/graph.json`. Look in `tests/conftest.py` (the `sh()` helper is at line 22) and `src/brindle/tmux.py`.
- **Fix.** Make each test's tmux server own its lifetime: keep a dummy session alive for the server's lifetime, or wait for teardown of the previous test before the next one creates a server. Do not add timed waits or try-again loops (the repo's lint check rejects them).
- **Test.** Write a deterministic test that creates the server, closes the first session, and then adds a window. On unmodified code it must fail. Do not rely on timing.
- **Done when.** The new test fails before the fix and passes after. `uv run pytest tests/test_auth_failure_handling.py -q` passes.

### T2. #244: autopilot nudges fire while a `need_user` question is open (BUG)

- **Symptom.** While a `need_user` question is open, brindle sends "The goal isn't reached yet, and no workers are running" about every few minutes.
- **Find.** `graphify query "autopilot nudge no workers running" --graph graphify-out/graph.json`. Look in `src/brindle/autopilot.py` (134 symbols in its community). Also check `need_user` in `src/brindle/mcp_server.py`.
- **Fix.**
  - While a `need_user` question is open (not `answered=true`), suppress the no-workers nudge.
  - Send at most one reminder, after 60 minutes, saying the question is still open.
  - When `need_user(answered=true)` is called, nudges resume.
- **Test.** Use a fake clock and the real autopilot loop code path, not a mock of the nudge function. On unmodified code the test must show repeated nudges while the question is open.
- **Done when.** The new test fails before and passes after. Existing autopilot tests still pass: `uv run pytest tests/ -q -k autopilot`.

### T3. #249: `depends_on` across attached repos never releases (BUG)

- **Symptom.** A task whose `depends_on` names a branch in another attached repo stays pending forever after that branch merges.
- **Find.** `graphify query "depends_on queued task release on merge" --graph graphify-out/graph.json`. Look in `src/brindle/tasks.py` (128 symbols) and the merge path in `src/brindle/cli.py` or `src/brindle/workspaces.py`.
- **Fix.** When a merge happens in any repo of the session, release every queued task whose `depends_on` names that branch, regardless of which repo the task lives in. Probably the release lookup filters by the task's own repo. Remove that filter.
- **Test.** Two attached repos (use the `sh()` helper and temp git repos from `tests/conftest.py`). Queue a task in repo B depending on a branch in repo A, merge A, and assert the task starts. Use the real release code.
- **Done when.** The new test fails before and passes after.

### T4. #253: supervisor's brindle MCP connection drops mid-session (BUG, investigate first)

- **Symptom.** During a long autopilot session the brindle MCP server disconnected and its tools disappeared until the user ran `/mcp`.
- **Find.** `graphify query "mcp server stdio connection lifetime" --graph graphify-out/graph.json`. Read `src/brindle/mcp_server.py` (181 symbols) and how it is launched.
- **Steps.**
  1. Reproduce the disconnect with a long-running fake call, or read the code for an idle timeout, a broken pipe, or an exception that ends the server loop.
  2. Record the cause in the report before you change anything.
  3. Fix: keep the server alive on handled errors, and when it truly exits, make the supervisor's next message clearly say the MCP server is down and how to reconnect.
- **Test.** A test that makes one tool call raise and checks the server keeps serving the next call. Must fail before the fix.
- **Done when.** The test fails before and passes after. Report the root cause even if the fix is partial.

### T5. #261: bundled engine and installed CLI share `~/.brindle` (BUG)

- **Symptom.** Different brindle versions open the same database and replace each other's daemon. The schema has no version, so an older engine can open a newer database.
- **Find.** `src/brindle/db.py`. The schema is built with `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE` (see the `DB` class, line 867). Also read `app/docs/bundled-engine.md` for "Which running daemon it uses".
- **Steps.**
  1. Add a `schema_version` value in the database (a small `meta` table or a `PRAGMA user_version`).
  2. On open: if the database version is **newer** than the code's version, raise a clear error: "This database was written by a newer brindle. Update brindle." If it is older, run the migrations and set the version.
  3. Do not change the daemon-replacement behavior in this task. That is a follow-up (T17).
- **Test.** One test: create a database with a higher version than the code knows, open it, and assert the clear error. Must fail before the fix (today it opens silently).
- **Done when.** The test passes, and `uv run pytest tests/ -q -k db` still passes.

---

## Phase 2

### T6. #262, #250, #251: reviewers and workers stall on prompts (BUG, one branch)

All three are about prompts that block a reviewer or worker. Do them on one branch because they share prompt-detection code.

- **#262 (chained read-only commands).**
  - Find the permission policy: `graphify query "permission policy allow simple command" --graph graphify-out/graph.json`.
  - Auto-allow a chain (`&&`, `;`, `|`) when every part would be allowed on its own: read-only git (`log`, `diff`, `show`, `status`), `grep`, `ls`, `cat`, `head`, `tail`, and `curl` without data or upload flags.
  - Honor `profile` passed to `request_review`. Today `request_review(profile="reviewer-codex")` can start a `reviewer-gemini-low`. Find the reviewer selection in the review code and use the passed profile.
  - When a reviewer waits on a prompt, send the supervisor a message at once so it can reassign.
  - Tests: parametrize the policy test with one row per branch (a chain of allowed commands is allowed; a chain with `rm` is not; `curl -d` is not). The profile test must use the real request path and fail before the fix.
- **#250 (Codex startup and approval prompts).**
  - Find the Codex provider: `graphify query "codex reviewer startup prompt hooks need review" --graph graphify-out/graph.json`.
  - Surface "Would you like to run the following command?" and "Hooks need review" as **waiting on input**, with the prompt text. Do not mark the reviewer failed.
  - Auto-approve read-only probes and local test runs, only where the policy already allows them.
  - Test: a pane fixture containing the prompt text is classified as waiting, not failed. Must fail before the fix.
- **#251 (workers ask questions in their own pane).**
  - Detect a numbered-choice question in a worker pane and forward it to the supervisor as a message, including the question and options.
  - Do this with a pane-text classifier, not timing. Fixture-based test with a captured pane string.
  - Test must fail before the fix.
- **Done when.** Each of the three has its test failing before and passing after. `uv run pytest tests/ -q` passes.

### T7. #252: reviewers re-raise findings the supervisor already decided (BUG)

- **Find.** `graphify query "review prompt brief design decisions" --graph graphify-out/graph.json`. Look at where the reviewer prompt is built, and where `record_audit_decision` and `record_decision` are stored (`mcp_server.py`, `db.py`).
- **Fix.** Include in every reviewer prompt, for the goal: (a) the recorded decisions, and (b) any spec changes the supervisor sent the worker. Tell the reviewer not to raise a finding that contradicts a recorded decision.
- **Test.** Build a reviewer prompt for a goal with one recorded decision and assert the decision text is in it. Use the real prompt builder. Must fail before the fix. Do not test the exact wording; test that the decision text appears.
- **Done when.** The test passes and the review tests still pass.

### T8. #186: review pipeline unreliable (umbrella; split into sub-tasks)

Do these in order, one commit each, on one branch `feat/review-pipeline`.

- **8a. Auto-start review after a report.** When a worker reports and no review is running after a configurable short delay (default 60 seconds; add the setting to the config), start the review without the supervisor calling `request_review`. Find `report_result` and `request_review` in `mcp_server.py`. Test with the real report path and a fake clock.
- **8b. Expose review state.** Add a per-branch state to `list_agents` and `list_tasks`: `waiting_for_checks`, `checks_running`, `reviewer_running`, `verdict_recorded`, `merge_failed` (with the reason). Test the state transitions with database rows.
- **8c. Attempt a merge a second time on transient failure.** When an approved branch fails a merge check, attempt the merge once more. Send the full failing check output to the supervisor, not just the first line. Test with a fake check that fails once, then passes.
- **8d. Ignore untracked scratch files.** When deciding whether a worker "left uncommitted changes", ignore untracked files, or name the file in the message. Find the check in `workspaces.py` or `cli.py`. Test with one untracked file present and the work committed: the branch must be reviewable.
- **8e. Run review in parallel with checks (optional, only if 8a–8d are done and green).** Start the reviewer as soon as the diff is ready and hand it the check results when they finish. This changes the pipeline's ordering, so skip it if you are unsure, and say so in your report.
- **Done when.** Each sub-task's test fails before and passes after. Run the full suite once at the end.

---

## Phase 3: providers and models (run in order)

### T9. #213: make OpenCode a routing option

- **Find.** `src/brindle/opencode.py`, `src/brindle/worker_models.py`, and the `routing` defaults in `src/brindle/config.py` (`RepoConfig`, line 96; `load_repo_config` at line 332).
- **Steps.**
  1. Add `developer-opencode` as a non-baseline candidate in the default routing lists, so it is usable without config.
  2. Add OpenCode as a signed-in CLI option for each size on the workers screen.
  3. `brindle doctor` lists OpenCode in the effective routing when it is installed.
- **Test.** The routing default includes the OpenCode profile for a size when OpenCode is detected (fake the detection). Must fail before the change. One row per size is enough; do not write one test per size.
- **Done when.** Test passes, and `uv run pytest tests/ -q -k routing` passes.

### T10. #218: OpenCode token and cost tracking, plus effort

- **Find.** `src/brindle/opencode.py`. Look at how other providers record usage (search for `record_usage` or similar with `graphify query "record token usage provider" --graph graphify-out/graph.json`).
- **Steps.**
  1. Read usage from OpenCode's plugin events or its session storage. First find out which one exists on this machine: look in OpenCode's data directory (`~/.local/share/opencode` on macOS/Linux) and note the format in your report.
  2. Record tokens the same way the other providers do, so they count toward budgets.
  3. Map `effort` to a model variant or reasoning setting if OpenCode supports one. If it does not, document in `opencode.py` that effort is ignored.
- **Test.** Parse a saved usage fixture file (a small JSON you add under `tests/fixtures/`) and assert the token counts are recorded. Must fail before the change.
- **Done when.** Test passes. If the storage format cannot be found, stop and report it (do not guess the format).

### T11. #214: share one `provider/model` choice between native and OpenCode

- **Steps.**
  1. When an OpenCode profile names a Models.dev `provider/model`, pass it through unchanged.
  2. Before launch, check that OpenCode knows the provider. If not, fail with a readable error naming the provider.
- **Find.** `src/brindle/native/catalog.py` (Models.dev catalog) and the OpenCode launch code in `opencode.py`.
- **Test.** An unknown provider raises the readable error before launch (fake OpenCode's provider list). A known one passes the string through unchanged. Both must be tested through the real launch code.
- **Done when.** Tests pass.

### T12. #212: workers screen offers Models.dev models

- **Find.** `src/brindle/worker_models.py` (the fixed list) and `src/brindle/native/catalog.py`.
- **Steps.**
  1. On the workers screen, search the cached Models.dev catalog. Offer models whose provider has an OpenAI- or Anthropic-style endpoint.
  2. A pick writes a native profile with `model: <provider>/<model>`, asks for the provider's key variable the same way OpenRouter picks do, and checks the key before saving.
  3. Respect org policy (allowed providers and models) and air-gap mode: in air-gap mode the catalog is never fetched.
- **Test.** The picker's model list includes a Models.dev model and excludes one from a disallowed provider. Use fakes for the catalog. Must fail before the change.
- **Done when.** Tests pass. A non-fixed model can be picked and routed to, in a test.

### T13. #215: "no Anthropic" preset

- **Steps.**
  1. Add a setting `"providers_exclude": ["claude"]` (or a "No Anthropic" preset) that:
     - sets the supervisor to OpenCode, Codex or Antigravity, whichever is signed in;
     - filters Claude profiles out of every routing tier and out of the reviewer choice;
     - refuses to launch, with a clear message, if nothing usable remains.
  2. `brindle doctor` shows that the preset is active and what it resolved to.
- **Find.** `config.py` (`load_repo_config`, `RepoConfig`), routing in `tasks.py` or `agents.py`, and the reviewer selection.
- **Test.** With the setting on and only Claude profiles available, launch refuses with the message. With the setting on and Codex available, the resolved supervisor is Codex and no tier contains a Claude profile. Use the real resolution code with fake installed-CLI detection.
- **Done when.** Tests pass.

---

## Phase 4

### T14. #221: refresh the doc part of the code graph after merges

- **Find.** The merge message in `src/brindle/pipeline.py` or the merge code (search `graphify update`).
- **Steps.**
  1. When a merge changes Markdown files, run graphify's semantic update for only those files in the background. Use the repo's configured model or a cheap default. Respect air-gap mode. If no model is available, print a one-line note and do not create a supervisor task.
  2. On session start, if the default branch has moved since the graph was built, run the code-graph refresh. (Alternatively, offer `graphify hook install` from `brindle init`.)
- **Test.** A merge that changes a `.md` file calls the semantic update for that file only (use a fake runner). Must fail before the change.
- **Done when.** Test passes. Run after T8 has merged.

---

## Blocked tasks: need the user

### T15. #260: Windows desktop release (NEEDS USER)

Do not start. You need from the user:
- a Windows code-signing certificate, and the `desktop-release` environment secrets;
- a real Windows machine with WSL to test the installer.

What you can do now, without those: prepare the CI change in `app-release.yml` so the `windows` job signs the installer when a certificate secret is present, and leaves it unsigned otherwise. Write it as a separate branch and say it is untested.

### T16. #194: native Windows support (NEEDS USER)

The issue is marked "on the roadmap, not planned for now". Ask the user whether to keep it open or close it. Do not implement it.

### T17. #261 follow-up: version-mismatch warning (after T5)

When the app finds a daemon from a different install, warn the user about the version difference. Do not stop or replace the daemon in this task. Find the daemon handshake in `app/src-tauri/` and `src/brindle/daemon.py`. Test the warning path with a fake version string.

---

## Report format

At the end, write `docs/plans/open-issues-status.md` with one row per task:

| Task | Issue | Branch | Status (done / blocked / skipped) | Failing-before test | Full suite | Notes |

Keep notes short. Name any issue where the code did not match this plan.


## Added scope: David’s new issues (2026-10-23)

The owner explicitly prioritizes **#263 as CRITICAL: login blocks launching the app**. This overrides the original phase order for these launch bugs. Existing phases remain in scope.

### T19. #263: Claude / agy login does not complete — CRITICAL

- Source: https://github.com/PawDelta/brindle/issues/263
- Worker: Codex heavy, `fix/critical-provider-login`, starts immediately.
- Trace desktop and CLI sign-in, credential-home consistency, completion detection and launch unblocking for Claude and Antigravity. Reproduce with minimal deterministic failing-before regression coverage before source fixes. Never expose credentials or automate a real sign-in.
- Verify the actual login/completion path and successful launch readiness with fake provider results. Record exact acceptance selectors in the status report and milestone check when identified.

### T20. #264: black screen when resuming a session

- Source: https://github.com/PawDelta/brindle/issues/264
- Worker: Codex heavy, `fix/resume-black-screen`, independent investigation in parallel with T19.
- Reproduce resuming after `/exit` and verify the supervisor conversation and sidebar are restored. Trace session continuation, pane selection and sidebar attachment; coordinate overlaps before edits.
- Require failing-before regression evidence and targeted session/sidebar checks. Preserve shared credential recovery behavior.

### T21. #265: company cloud identity logins

- Source: https://github.com/PawDelta/brindle/issues/265
- Worker: Codex heavy, `feat/company-cloud-logins`, queued until T19 merges; plan first.
- Cover Claude through Bedrock/Vertex, Gemini through Vertex, and Azure OpenAI using cloud identities; profiles carry account/project/region; doctor checks credential validity; worker identities propagate and expired credentials pause consistently.
- Inspect existing cloud-login support first and implement only demonstrated gaps. Verify resolution, propagation, expiry and recovery with fakes, without printing credentials or modifying real cloud accounts.

The final status report must include T19–T21. Issue #253 remains open: tested fatal-exit notification improvements are partial; the historical disconnect trigger is unconfirmed and the owner has no logs or reproduction details.
