# OS sandbox for Claude Code and Antigravity workers (parked)

Parked on 2026-11-06 by the owner's decision: no OS sandbox in 0.6.3, not even
opt-in. Nothing under `src/` imports this folder and pytest doesn't collect it
(`norecursedirs` in pyproject.toml). The product behaves as before the branch.

## What it does

`os_sandbox.py` makes a Claude Code or Antigravity worker's shell commands run
in the CLI's own command sandbox with every credential store denied, as Codex
workers already do under Codex's sandbox:

- policy: on for workers (never an interactive chat) when the repo's
  `permission_policy` is on, read from the main checkout's config; an
  owner-only switch `"os_sandbox": "off"` and per-service allowances
  `"os_sandbox_allow": ["ssh_agent", {"docker": "/path/proxy.sock"}, "host_tmux",
  "apple_events"]` in `~/.brindle/config.json`;
- protected paths: `permissions.credential_paths()` as the single source,
  expanded, with resolved aliases and stores moved by `GH_CONFIG_DIR` and the
  like; policy files write-protected; build caches, linked-worktree git dirs and
  profile `add_dirs` writable;
- Claude Code: a `sandbox` block for `--settings` (required, no unsandboxed
  second run, brindle's decisions kept, denyRead/denyWrite/allowWrite, the CLI's
  own credentials scrubbed from commands via `credentials.envVars`, network open,
  local binding, Unix sockets limited to brindle's daemon plus the allowances);
  managed settings that weaken it refuse the launch;
- Antigravity: `--sandbox` plus session `--settings` denying the same stores with
  `allow: []`; the hook denies agy's own `BypassSandbox: true` second run;
- a capability check per executable (platform, version floor, a real Seatbelt or
  bubblewrap probe on a synthetic store) that fails closed with a repair message
  and the off switch; `brindle doctor` lines.

`test_os_sandbox.py` executes the Seatbelt boundary on the machine (direct cat,
env var into `bash -c`, `os.environ`, case/array, dotglob, nested shells, a
symlink alias; a positive-control write) and covers the launches, the switch,
the refusals, the allowances and the agy bypass denial.

## Canary results (this Mac, 2026-11-06, synthetic data; details in docs/investigations/os-sandbox.md)

- Claude Code 2.1.319 via `claude -p --settings`: store read denied (also via
  `sh -c`), env var scrubbed, `allowedDomains: ["*"]` works, local bind works,
  nested `sandbox-exec` fails (rc 71), no unsandboxed second run offered.
  `allowUnixSockets` opens exactly the listed path (ssh agent, tmux socket).
- agy 1.5.2 via `agy --sandbox`: the person's relative `write_file(...)` allow
  entry broke the sandbox configuration; with `allow: []` the deny was enforced
  (incl. nested `sh`); no env scrubbing; the agent reran each blocked command
  with `BypassSandbox: true` unprompted, which the PreToolUse hook sees; no
  Unix socket, Mach or Apple Event settings; localhost proxied.
- The keychain (`security`) was reachable from both sandboxes with no allowance.

## Why it was parked

The owner judged Docker, host tmux, the ssh agent and the keychain core to
workers' commands; Claude Code can reopen sockets by exact path, Antigravity
can't reopen any, and the default-on boundary was not acceptable for 0.6.3.

## Wiring it back

1. `git mv dev/os_sandbox/os_sandbox.py src/brindle/os_sandbox.py` and
   `git mv dev/os_sandbox/test_os_sandbox.py tests/test_os_sandbox.py`.
2. `providers.ClaudeCode.command`: after the worker `permissions` deny block,
   `if os_sandbox.wanted(ctx.cwd, self.name, ctx.mode): settings["sandbox"] =
   os_sandbox.claude_settings(ctx.cwd, ctx.profile)`.
   `providers.Antigravity.command`: after the skip flag, `argv +=
   os_sandbox.agy_flags(ctx.cwd)` under the same `wanted` test.
3. `agents.py`: import `os_sandbox`; catch `os_sandbox.OsSandboxRefused` beside
   `providers.CodexSandboxRefused` in `spawn` (AgentError) and `run_headless`
   (print, code 127); in `_window_environment` extend `deny` with
   `os_sandbox.agy_env_denies()` for a sandboxed antigravity agent.
4. `antigravity.pre_tool_decision`: after the workspace check, `if
   os_sandbox.bypass_requested(payload) and os_sandbox.wanted(ws.path,
   agent.provider, agent.mode): return os_sandbox.bypass_denial()`.
5. `permissions.credential_paths`: add the CLIs' own sign-ins (Claude Code's
   `.credentials.json` in its effective home and `~/.claude`, agy's
   `~/.gemini/antigravity-cli/antigravity-oauth-token`, `~/.gemini/oauth_creds.json`).
6. `doctor._repo_checks`: `out.extend(os_sandbox_checks(repo_root))` mapping
   `os_sandbox.status(repo_root)` levels to Check; `cli.py`: `brindle permissions
   os-sandbox [on|off] --allow NAME[=PATH] --disallow NAME` over
   `set_user`/`forget_user`/`update_user` and `os_sandbox.parse_allowances`.
7. `tests/conftest.py` `brindle_home`: `monkeypatch.setattr(
   "brindle.os_sandbox.capability_problem", lambda provider, binary=None: None)`
   (the check spawns the CLI's `--version`, which conftest refuses); the test
   file restores the real one.
8. `docs/guide.md`: the "The OS sandbox" subsection under Permission policy
   (in this branch's history, commit 0d44a770).
