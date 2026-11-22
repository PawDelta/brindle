# README accuracy audit

Audited `README.md` claim by claim against this branch (`docs/readme-accuracy`, package version `0.6.5` in `pyproject.toml`), `scripts/install.sh`, `src/brindle/`, `app/`, and website routes in `/Users/hannahlerner/Projects/pawdelta-web/.brindle/worktrees/brindle/session-13/web/app`. The "Built with brindle" section was left untouched because its numbers are dated snapshots (`2026-10-05` to `2026-11-04`).

## Changes made to `README.md`

| Location | Before | After | Reason & evidence |
|---|---|---|---|
| Line 20 & line 122 | `Download the app` / `Download it from` linked to `https://pawdelta.com/brindle/getting-started/` | Linked to `https://pawdelta.com/brindle/downloads/` | In `pawdelta-web`, `/brindle/getting-started/` (`web/app/getting-started/page.tsx`, `backend/getting_started.py:41-43`) is the setup guide and links out to `/brindle/downloads/` for the desktop app; `/brindle/downloads/` (`web/app/downloads/page.tsx:93-119`) is the page that actually hosts the macOS and Linux app downloads. |
| Line 123 | `under **New session** choose **Watch brindle work on a practice repo**` | `under **New session** choose **Watch the practice run**` | In the desktop app's first-run **New session** view (`app/src/screens/New/FirstRun.tsx:80,91`), the card is **Practice run** and the button label is **Watch the practice run** (once projects exist, `app/src/screens/Onboarding/Demo.tsx:76-81` shows a **Practice run** row with button **Watch**). The old label existed only in a code comment (`Demo.tsx:1`). |
| Lines 189–191 | `It checks PyPI for updates (`BRINDLE_NO_UPDATE_CHECK` turns that off) and otherwise talks only to what you set up` | `The CLI checks PyPI for updates (`BRINDLE_NO_UPDATE_CHECK` turns that off) and the desktop app checks pawdelta.com for app updates (`"app_updates": false` turns that off); otherwise it talks only to what you set up` | The CLI checks PyPI (`src/brindle/updates.py:23,43`) and honors `BRINDLE_NO_UPDATE_CHECK` (and air-gap mode), whereas the desktop app separately checks `pawdelta.com/brindle/downloads/desktop/latest.json` (`app/src-tauri/src/updates.rs:1-32,63-72`) and does not read `BRINDLE_NO_UPDATE_CHECK`—turning it off requires `"app_updates": false` (or `"airgap": true` / `BRINDLE_AIRGAP=1`). |

---

## Claim-by-claim verification by README section

### 1. Header, badges, nav links and intro (`README.md:1-33`)

| Claim | Status | Code / repo evidence |
|---|---|---|
| Logo image `assets/brindle-mark.svg` | Verified | `assets/brindle-mark.svg` exists in repo. |
| PyPI package `brindle` | Verified | `pyproject.toml:2` (`name = "brindle"`), `.github/workflows/publish.yml`. |
| CI badge `.github/workflows/ci.yml` on `main` | Verified | `.github/workflows/ci.yml:1-14` exists and runs on `push` to `main`. |
| Python versions badge (`pypi/pyversions/brindle`) | Verified | `pyproject.toml:6,17-20` (`requires-python = ">=3.11"`, classifiers for 3.11, 3.12, 3.13; `scripts/install.sh:54` installs with `--python '>=3.11,<3.15'` until `loro` ships 3.15 wheels). |
| Platforms badge `macOS \| Linux \| WSL2` | Verified | `pyproject.toml:15-16` (MacOS, POSIX Linux), `scripts/install.sh:20-24` (macOS, Linux, WSL on Windows), `src/brindle/providers.py:1611-1646` (`is_wsl`, `which_cli`, `windows_browser`). |
| License badge (`LICENSE`, `source-available`) | Verified | `LICENSE` (Brindle License 1.0, source-available), `pyproject.toml:7` (`license = "LicenseRef-brindle-1.0"`). |
| Nav link `https://pawdelta.com/brindle/` | Verified | Maps to `web/app/page.tsx` (`web/next.config.ts:9` sets `basePath: "/brindle"`). |
| Nav link `https://pawdelta.com/brindle/docs/` | Verified | Maps to `web/app/docs/page.tsx`. |
| Nav link `Download the app` -> `https://pawdelta.com/brindle/downloads/` | **Fixed** (was `/getting-started/`) | Maps to `web/app/downloads/page.tsx` (hosts the Apple Silicon, Intel, AppImage and `.deb` download links); `web/app/getting-started/page.tsx` only links out to `/brindle/downloads/`. |
| Nav link `https://pawdelta.com/brindle/changelog/` | Verified | Maps to `web/app/changelog/page.tsx`. |
| Runs Claude Code, Codex, Google Antigravity, OpenCode and open-weight models side by side in git worktrees | Verified | `src/brindle/providers.py:2790-2792` (`ClaudeCode`, `Codex`, `Antigravity`, `OpenCode`, `Native`), `src/brindle/workspaces.py:1-11`. |
| Supervisor splits goal into milestones and hands work out; branch merges only after reviewer approves exact commit and checks pass; milestone done when check exits 0 | Verified | `src/brindle/builtin_agents/supervisor.md`, `src/brindle/autopilot.py:3-7,1676-1777`, `src/brindle/gates.py:1-15`. |
| Demo GIF `assets/brindle-app-demo.gif` | Verified | `assets/brindle-app-demo.gif` exists in repo. |

### 2. Why (`README.md:34-57`)

| Claim | Status | Code / repo evidence |
|---|---|---|
| Own worktree and branch cut from freshly fetched base, with own block of ports | Verified | `src/brindle/config.py:26-27,102` (`PORT_RANGE_START = 20000`, `PORT_BLOCK_SIZE = 10`, `fetch = True`), `src/brindle/workspaces.py:5-6,80-81`. |
| Up to `max_agents` at once, five by default | Verified | `src/brindle/config.py:124` (`max_agents: int = 5`). |
| Milestone check exits 0; brindle runs it itself and re-runs after later merges to catch regressions | Verified | `src/brindle/autopilot.py:1676-1681,1774-1777` (`check_milestones` re-runs previously passed milestones when `HEAD` moved). |
| Reviewer approves exact commit before merge; Codex can review Claude or a local model can review both | Verified | `src/brindle/gates.py:5-6`, `src/brindle/config.py:112-113` (`reviewer`, `review_profile`), `src/brindle/builtin_agents/review.gpt.md`, `review.qwen3-coder.md`. Cross-vendor review is optional/configurable (matches repo decision `4e5e8332`). |
| Pre-commit hooks and `checks` gate every merge | Verified | `src/brindle/config.py:106,123` (`checks`, `pre_commit = True`), `src/brindle/gates.py:7-14` (agent merges via `merge_workspace` are gated; human `brindle merge` is intentionally ungated). |
| Keeps going until every milestone passes, pauses at usage limit and resumes after reset | Verified | `src/brindle/autopilot.py:9-17,2676-2745` (`usage_paused`, `usage_resets_at`); `src/brindle/quota.py:1-10` (applies to CLI-reported subscription limits; API-key billing reports no subscription percentage). |
| Quit and `brindle continue` later; workers resume too | Verified | `src/brindle/cli.py:687-723`, `src/brindle/agents.py:2307-2316`; all five providers (`ClaudeCode`, `Codex`, `Antigravity`, `OpenCode`, `Native`) implement `can_resume` (`src/brindle/providers.py:1180,2358,2535,2673,2767`), falling back to restarting on the original task when no saved session exists. |
| Decisions, permission requests and finished work land in one inbox; supervisor can answer routine permission asks itself | Verified | `app/src/screens/Inbox/`, `src/brindle/permission_routing.py:1-19,153-225` (`answer_permission`, `permission_supervisor_hold_seconds = 120` in `config.py:195`; person-only rules bypass the supervisor). |
| Each agent's state comes from its lifecycle hooks, not screen scraping | Verified | `src/brindle/providers.py:1055,2278,2521,2669,2763`: all five providers set `uses_hooks = True` (Claude Code hooks, Codex `notify`/hooks, Antigravity hooks, OpenCode plugin, Native in-process status). `screen_state` is only a fallback/supplement for startup/approval prompts or stale hooks (`providers.py:385-387`). |
| Supervisor does small things itself rather than start workers | Verified | `src/brindle/config.py:163` (`delegation = "balanced"`), `src/brindle/builtin_agents/supervisor.md:11-13`. |
| Comparison links (`claude-squad`, `conductor`, `superset`, `claude-code-subagents`) | Verified | `web/app/compare/[slug]/page.tsx` builds all four from `web/content/compare/{claude-squad,conductor,superset,claude-code-subagents}.html`. |

### 3. How a goal gets done (`README.md:58-95`)

| Claim | Status | Code / repo evidence |
|---|---|---|
| Flowchart: Goal/milestones -> Supervisor -> Workers (own worktree + branch) -> Reviewer approves exact commit (changes requested -> Supervisor) -> Pre-commit hooks & checks -> Merge into session branch -> Milestone check exits 0? | Verified | `src/brindle/pipeline.py`, `src/brindle/gates.py:1-15`, `src/brindle/autopilot.py:3-17`. |
| Supervisor delegates through MCP tools (`assign` with `files` and `depends_on`); refuses overlapping work, queues dependent tasks until prerequisites merge, starts from updated base | Verified | `src/brindle/mcp_server.py:1009-1150`, `src/brindle/config.py:202-208` (`overlap = None` blocks within a session and queues cross-session), `src/brindle/tasks.py:1368-1406,1858-2160`. |
| `plan_first`: worker proposes plan and waits for approval before editing (enforced by hook for Claude Code; other CLIs told to wait) | Verified | `src/brindle/config.py:165` (`plan_first = False`), `src/brindle/agents.py:560-561` (`PLAN_FIRST_NOTE`), `src/brindle/providers.py:1037-1038` & `src/brindle/agents.py:5742-5751` (`PreToolUse` denial for `PLAN_GATED_TOOLS` is Claude Code only). |
| Every worker's worktree is snapshotted after each turn that changed files; can rewind to a good turn and restart with a note | Verified | `src/brindle/rewind.py:1-22,140-172` (`refs/brindle/turns/<agent>/<n>`), `src/brindle/cli.py:2998-3039` (`brindle agent turns`, `brindle agent rewind`). |
| `.brindle/goals.md` format (`# Goal`, `## Milestone`, `check: ...`) | Verified | `src/brindle/autopilot.py:23-36,253,273-328` (`parse_goals` parses this exact syntax; lines 26-35 use the same example). |

### 4. Built with brindle (`README.md:97-112`)

| Claim | Status | Notes |
|---|---|---|
| Dated historical stats (`2026-10-05` to `2026-11-04`) | Untouched by instruction | Dated snapshot; denominator is accurately stated as review events (`1,567`, of which `729` / `47%` requested changes) separately from merged branches (`529`). |

### 5. Get started (`README.md:114-144`)

| Claim | Status | Code / repo evidence |
|---|---|---|
| Need at least one agent CLI installed and signed in; Claude Code is default for supervisor, workers and reviewer (`npm install -g @anthropic-ai/claude-code`, then `claude auth login`) | Verified | `src/brindle/builtin_agents/supervisor.md:4` (`provider: claude`), `src/brindle/service/sessions.py:355` (`claude` default), `src/brindle/config.py:101,112` (`default_agent = "developer"`, `reviewer = "review.claude-sonnet.high"`, both `provider: claude`), `scripts/install.sh:70` & `src/brindle/doctor.py:141-142` (`npm install -g @anthropic-ai/claude-code`), `src/brindle/providers.py:2165` (`claude auth login`). Note: when Codex is also installed and signed in, weight routing (`src/brindle/worker_models.py:28-36`, `TIER_PREFERENCES`) tries `dev.gpt` first for `light` and `medium` tiers and falls back to Claude profiles when Codex isn't available. |
| Core workflow is free; desktop app needs a free brindle account with no payment details; CLI needs no account | Verified | `app/src/App.tsx:958-1044` (`SignInGate` gates the app behind a brindle account), `pyproject.toml:45` & `src/brindle/cli.py` (CLI core workflow runs with no account; Pro/Team/Enterprise features require entitlements), `web/app/downloads/page.tsx:118` & `backend/getting_started.py:273-274`. |
| **The app** (macOS and Linux) | Verified | `web/app/downloads/page.tsx:96-116` & `web/lib/routes.ts:37-42` publish macOS (Apple Silicon & Intel DMGs) and Linux (x86_64 AppImage & `.deb`). Windows thin-client plumbing exists in `.github/workflows/app-release.yml:28-37` and `src/brindle/cli.py:1161-1209`, but no Windows download is published yet. |
| Download link for the app | **Fixed** (was `getting-started/`) | Updated to `https://pawdelta.com/brindle/downloads/` (`web/app/downloads/page.tsx`). |
| Under **New session** choose **Watch the practice run** | **Fixed** (was `Watch brindle work on a practice repo`) | Matches `app/src/screens/New/FirstRun.tsx:80,91` (first-run **New session** screen; after projects exist, `app/src/screens/Onboarding/Demo.tsx:76-81` offers the **Practice run** row with **Watch**). |
| **The terminal** (macOS, Linux, or Windows through WSL2); install script installs `uv` if missing, then `brindle`, then runs `brindle doctor`; tmux installed with Homebrew on macOS and install command printed on Linux/WSL2 (`sudo apt install tmux` on Debian/Ubuntu) | Verified | `scripts/install.sh:20-24` (macOS, Linux, WSL), `26-32` (`uv` install if missing), `34-45` (`brew install tmux` on macOS if `brew` is present; prints `sudo apt install tmux` / `sudo dnf install -y tmux` / generic note elsewhere), `54` (`uv tool install --upgrade --python '>=3.11,<3.15' "$spec"`), `74` (`brindle doctor \|\| true`). |
| `curl -fsSL pawdelta.com/brindle/install \| sh` | Verified | `scripts/install.sh:2`, `pawdelta-web/backend/main.py:342-348` (`@app.api_route("/brindle/install", ...)` serves `install.sh`). |
| Quickstart shell commands (`brindle demo`, `brindle init`, `brindle`) | Verified | `src/brindle/cli.py:206-295,951-992`. |
| Terminal demo GIF `assets/brindle-demo.gif` | Verified | `assets/brindle-demo.gif` exists in repo. |
| App and CLI run on the same engine and work on the same sessions | Verified | `app/README.md:3-8`, `src/brindle/service/sessions.py:3`. |

### 6. Commands (`README.md:145-164`)

Every command and flag checked against `src/brindle/cli.py` (and `uv run python scripts/check_docs.py README.md`):

| Command / claim | Status | Evidence in `src/brindle/cli.py` |
|---|---|---|
| `brindle` and `-s codex` | Verified | `default()` (`cli.py:951-992`), `--supervisor`/`-s` (`cli.py:956-958`). |
| `brindle init` | Verified | `init()` (`cli.py:206-261`): detects `setup`, `checks`, `copy`, writes `.brindle/config.json`, runs doctor checks. |
| `brindle demo [--local]` | Verified | `demo()` (`cli.py:274-295`), `src/brindle/demo.py:113-115` (`--local` sets `dev.qwen3-coder` and `review.qwen3-coder` on Ollama). |
| `brindle doctor` | Verified | `doctor()` (`cli.py:1117-1144`), `src/brindle/doctor.py:504-552` (checks tmux, agent CLIs, sign-ins/credentials, repo config and checks, etc.). |
| `brindle continue [ID]` | Verified | `continue_cmd()` (`cli.py:687-723`), optional `session_id` (default most recent), resumes workers via `agents.resume`. |
| `brindle goal [check]` | Verified | `autopilot_cmd()` (`cli.py:1075-1100`), optional `action` (`check` runs milestone checks now). |
| `brindle history [--share]` | Verified | `history()` (`cli.py:1258-1293`), `--share` calls `history_mod.share_card`. |
| `brindle ls` | Verified | `list_cmd()` (`cli.py:1222-1255`). |
| `brindle new BRANCH -p PROMPT` | Verified | `new()` (`cli.py:298-368`), `branch` argument + `--prompt`/`-p`. |
| `brindle pr [WS]` | Verified | `pr()` (`cli.py:2848-2865`), optional `workspace` argument. |
| `brindle app` | Verified | `app_cmd()` (`cli.py:1184-1219`): opens desktop app or prints download URL. |
| `brindle web` | Verified | `web_cmd()` (`cli.py:2698-2721`): listens on `127.0.0.1` only. |
| With no `WS` argument, commands act on the current workspace | Verified | `_ws()` (`cli.py:121-131`) falls back to `workspaces.current(db)`. |
| Link `docs/guide.md#commands` | Verified | `docs/guide.md:401` (`## Commands`). |

### 7. Agents and models (`README.md:165-181`)

| Claim | Status | Code / repo evidence |
|---|---|---|
| Claude Code (`claude`): supervisor yes (default), worker/reviewer yes; signs in with its own login or `ANTHROPIC_API_KEY`; also Bedrock, Vertex and Foundry | Verified | `src/brindle/providers.py:1764-1788,1807-1808,2208`, `src/brindle/builtin_agents/supervisor.md:4`, `developer.md:4`, `reviewer.md:4`. |
| Codex (`codex`): supervisor yes, worker/reviewer yes; signs in with its own login or `OPENAI_API_KEY` | Verified | `src/brindle/providers.py:1809,1929-1945,2208`, `src/brindle/doctor.py:294-319`, `src/brindle/keys_cmds.py:116,140-147`. Caveat documented in `docs/guide.md:1762`: while signed in with ChatGPT, interactive Codex panes ignore env API keys and use the ChatGPT plan unless switched via `codex login --with-api-key`. |
| Google Antigravity (`agy`): supervisor yes, worker/reviewer yes; signs in with its own login or `GEMINI_API_KEY` | Verified | `src/brindle/providers.py:1801-1810,1952-1965,2208`, `src/brindle/keys_cmds.py:148-157`. Caveat documented in `docs/guide.md:2463-2468`: `agy` reads `GEMINI_API_KEY` only when `~/.gemini/antigravity-cli/settings.json` sets `"modelProvider": "gemini"` (`brindle keys set GEMINI_API_KEY` / `brindle doctor --fix` offers to set it). |
| OpenCode (`opencode`): supervisor yes, worker/reviewer yes; signs in with whatever the model's provider takes | Verified | `src/brindle/providers.py:2192-2196,2208`, `src/brindle/opencode.py:1-32`. |
| Open-weight models (brindle's own agent loop): supervisor no, worker/reviewer yes; any OpenAI- or Anthropic-style endpoint (Ollama, LM Studio, llama.cpp or hosted API) | Verified | `src/brindle/providers.py:2209,2752-2771` (`NOT_SUPERVISOR = ("native", "subagent")`), `src/brindle/native/client.py:104-120` (`api = "openai" | "anthropic"`), `docs/guide.md:2199-2208`. |
| Only offers profiles whose CLI is installed and signed in | Verified | `src/brindle/cli.py:2964-2974` & `src/brindle/providers.py:2212-2228` (`unusable`). |
| Built-in `dev.qwen3-coder` and `review.qwen3-coder` run Qwen3-Coder on Ollama; GLM, DeepSeek, Kimi and gpt-oss work the same way | Verified | `src/brindle/builtin_agents/dev.qwen3-coder.md:4-7` & `review.qwen3-coder.md:4-7` (`http://localhost:11434/v1`, `qwen3-coder:30b`), `src/brindle/worker_models.py:61-68`, `docs/guide.md:2123-2225`. |
| Claude Code itself can be pointed at another Anthropic-compatible backend from a profile | Verified | `src/brindle/profiles.py:136` (`env.NAME: value`), `docs/guide.md:2230-2252` (`env.ANTHROPIC_BASE_URL` / `env.ANTHROPIC_AUTH_TOKEN`). |
| Profiles are Markdown files with frontmatter (provider, model, permission mode, prompt); supervisor routes tasks by weight | Verified | `src/brindle/profiles.py:1-150`, `src/brindle/config.py:66,260`, `src/brindle/builtin_agents/supervisor.md:35-41`. |

### 8. FAQ (`README.md:182-201`)

| Question / claim | Status | Code / repo evidence |
|---|---|---|
| Extra cost on top of Claude/Codex plan? No; core workflow free; app needs free brindle account (no payment details); CLI needs none | Verified | `app/src/App.tsx:958-1044`, `pyproject.toml:45`, `web/app/downloads/page.tsx:118`, `backend/getting_started.py:273-274`. |
| Where does my code go? Local CLIs, state in `~/.brindle`, no telemetry | Verified | `src/brindle/config.py:30-35` (`~/.brindle`). No client telemetry/usage-count sender exists in `src/` or `app/` (repo decision `e0bf60fc` defers client pings; `git grep` confirms none in this checkout). |
| Update-check claim & opt-out | **Fixed** | CLI checks `https://pypi.org/pypi/brindle/json` (`src/brindle/updates.py:23,43`, disabled by `BRINDLE_NO_UPDATE_CHECK` or air-gap mode). Desktop app checks `pawdelta.com/brindle/downloads/desktop/latest.json` (`app/src-tauri/src/updates.rs:1-32,63-72`, disabled by `"app_updates": false` or air-gap mode, not `BRINDLE_NO_UPDATE_CHECK`). Updated README to state both accurately. Note: configuring a native profile with a Models.dev `<provider>/<model>` id and no `base_url` also fetches `https://models.dev/api.json` (`src/brindle/native/catalog.py:26,78-119`), covered by "model endpoints you configure". |
| Otherwise talks only to configured model endpoints, CI hosts, and brindle account (required by app, CLI only if signed in) | Verified | `app/src/App.tsx:958-1044`, `src/brindle/pro/auth.py`, `src/brindle/ci_client.py`. |
| Windows? CLI runs in WSL2; app is macOS and Linux for now | Verified | `scripts/install.sh:20-24`, `src/brindle/providers.py:1611-1646`, `web/app/downloads/page.tsx:96-116` (published downloads are macOS and Linux only). |
| Existing repo? Open folder in app or run `brindle init` once | Verified | `app/src/components/ProjectPicker.tsx`, `src/brindle/cli.py:206-261`. |
| Worker goes the wrong way? Review + checks gate merge, turn snapshots allow rewind | Verified | `src/brindle/gates.py:1-15`, `src/brindle/rewind.py:1-22,140-172`. |

### 9. Learn more, Contributing, License and disambiguation (`README.md:202-222`)

| Claim / link | Status | Code / repo evidence |
|---|---|---|
| `[The full guide](.../docs/guide.md)` | Verified | `docs/guide.md` exists and covers commands, config, providers, permission policy, paid features. |
| `[Website and docs](https://pawdelta.com/brindle/)` | Verified | Maps to `web/app/page.tsx`. |
| `[Contributor License Agreement](.../CONTRIBUTING.md)` and CLA bot on first PR | Verified | `CONTRIBUTING.md:1-41` contains Brindle Contributor License Agreement 1.0 (identical text to `CLA.md`); `.github/workflows/cla.yml:1-37` runs `contributor-assistant/github-action` on `pull_request_target` (referencing `CLA.md`). |
| Source-available under `[brindle License 1.0](.../LICENSE)` (free including at work, no modification beyond permitted changes, no redistribution, no competing service) | Verified | `LICENSE:3,11,13-52`. |
| `[guide](.../docs/guide.md#license)` | Verified | `docs/guide.md:2597` (`## License`). |
| Unrelated to Brindle desktop app at `brindle.dev` | Verified | Matches `docs/guide.md:2606`. |
