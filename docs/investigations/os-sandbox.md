# The OS sandbox for Claude Code and Antigravity workers

Status, 2026-11-06: built, then parked the same day by the owner's decision (no
OS sandbox in 0.6.3, not even opt-in). The code and its tests live in
`dev/os_sandbox/` with a README on wiring it back; nothing in the product
imports them. What follows describes the parked design and the evidence.

## Boundary

| | Claude Code | Antigravity (agy) |
|---|---|---|
| mechanism | its `sandbox` settings, passed with `--settings` (a layer only managed settings outrank) | `--sandbox` plus session-only `--settings` with `read_file`/`write_file` denies |
| required | `failIfUnavailable: true`: the CLI exits at start if it can't sandbox | brindle's own capability check; agy itself has no "fail if unavailable" (see gaps) |
| escape | `allowUnsandboxedCommands: false`: no unsandboxed second run; repository settings can't loosen it | agy reruns with `BypassSandbox: true` on its own; brindle's PreToolUse hook denies every such call |
| brindle's rules | kept: `autoAllowBashIfSandboxed: false` | kept: the strict hook decides every call as before |
| credential files | `filesystem.denyRead` + `denyWrite`: `permissions.credential_paths()` with resolved aliases | `permissions.deny`: `read_file(<path>)`, `write_file(<path>)` for the same list |
| env scrubbing | `credentials.envVars` deny for the CLI's own credentials (`secrets.provider_credentials`, secret-looking) | none in agy; brindle keeps a Gemini key agy ignores out of the pane |
| writes | worktree, temp, build caches, linked worktree git dirs, profile `add_dirs` | the same as `write_file(...)` allows |
| network | `allowedDomains: ["*"]`, `allowLocalBinding: true`, `allowUnixSockets: [daemon socket]` | agy's default: outbound open (seen), local binding worked (seen) |
| children | Bash descendants and Agent-tool subagents (docs) | nested `sh -c` denied (seen) |

Minimum versions: Claude Code 2.1.285 (`allowUnsandboxedCommands: false` from
`--settings` holds against a project `true`), agy 1.4.4 (the documented
unavailable-sandbox behaviour; verified on 1.5.2).

## Real-run evidence (this Mac, 2026-11-06)

Synthetic store only: `<scratch>/fakehome/.sshbox/id_fake` holding a canary string.

**Claude Code 2.1.319, `claude -p --model haiku --settings <brindle's block> --allowedTools Bash`:**

| step | result |
|---|---|
| `cat <store>/id_fake` | `Operation not permitted`, rc 1 |
| `printenv CANARY_TOKEN` (denied in `credentials.envVars`) | empty, rc 1 |
| `ls -la <store>` | `Operation not permitted` |
| `curl https://example.com` with `allowedDomains: ["*"]` | 200 |
| `python3` binding 127.0.0.1 with `allowLocalBinding` | bound |
| `sandbox-exec -p '(version 1)(allow default)' /bin/echo` | `sandbox_apply: Operation not permitted`, rc 71 |
| `sh -c 'cat <store>/id_fake'` | `Operation not permitted` |
| a second run outside the sandbox | not offered: "The sandbox config disables dangerouslyDisableSandbox for this session" |

**agy 1.5.2, `agy --sandbox -p ...` with `toolPermission: always-proceed` in the
person's settings (what `--dangerously-skip-permissions` selects):**

Run 1 (the person's settings as they are): every sandboxed command failed with
`sandbox configuration error: readwrite .brindle/config.json: non-absolute file
path` (a relative `write_file(...)` allow entry in their settings.json), and the
agent reran each with `BypassSandbox: true` without a prompt, reading the
store and the env var. The PreToolUse hook saw each rerun as
`toolCall.args.BypassSandbox: true`.

Run 2 (`--settings '{"permissions":{"allow":[],"deny":["read_file(<store>)"]}}'`):
the sandbox configured; `cat`, `ls` and `sh -c cat` on the store failed with
`Operation not permitted`; `printenv CANARY_TOKEN` printed it (no env
scrubbing); curl 200; local bind worked; nested `sandbox-exec` rc 71; the agent
again reran each failure with `BypassSandbox: true` and succeeded, since no
hook denied it there.

Consequences built in: brindle passes `allow: []` in agy's session settings (the
person's `command(...)` allows would let a bypass run unasked, and a relative
path among their file allows breaks the sandbox's mounts), and the hook denies
`BypassSandbox: true` (`os_sandbox.bypass_requested`). A sandbox configuration
error therefore fails every command rather than running it outside.

**Host services** (same day; `claude -p --model haiku` with and without the
allowances, `agy --sandbox` with `allow: []`; Docker Desktop wasn't running, so
its socket test is inconclusive):

| service | Claude, no allowance | Claude, allowance | agy `--sandbox` |
|---|---|---|---|
| ssh agent (`ssh-add -l`) | `Error connecting to agent: Operation not permitted` | `allowUnixSockets: [$SSH_AUTH_SOCK]`: `The agent has no identities` (reached) | `Operation not permitted`; no setting to open it |
| tmux server (`tmux -S /private/tmp/tmux-501/default ls`) | `Operation not permitted` | `allowUnixSockets: [that path]`: sessions listed | `Operation not permitted`; no setting |
| Docker (`docker version`) | daemon not running: `connect: no such file or directory` (socket path inconclusive) | same | `~/.docker/config.json: operation not permitted` (deny honoured), daemon not running |
| keychain (`security find-generic-password -s nope`) | reached: `could not be found in the keychain` (rc 44) | same | reached (rc 44) |
| `osascript -e 'return 1'` | `1` (no Apple Event sent) | `1` | `1` |
| `curl http://127.0.0.1:9` | connection refused (direct) | same | HTTP 400 from agy's proxy: localhost is proxied, not direct |

What each sandbox can narrow, from the docs and the runs above:

- Claude Code: exact Unix socket paths (`allowUnixSockets`, macOS; on Linux
  only with the optional seccomp filter, else all sockets are open), Mach
  service names (`allowMachLookup`, not needed for the keychain), Apple Events
  on or off, outbound domains (`allowedDomains` with `*.example.com`, `*`;
  `deniedDomains`; a proxy that refuses loopback/link-local resolution),
  localhost binding on or off, per-path reads and writes with narrower-wins
  and globs. It can't narrow an ssh agent to git hosts or Docker to a subset of
  the API: those need a brindle-side proxy socket (then allowed by path) or a
  localhost TCP proxy (reachable with `allowLocalBinding`).
- Antigravity: per-path reads and writes (`read_file`/`write_file` rules as
  mounts), and outbound domains only through `read_url` rules per the docs
  (the run above had outbound open regardless). No Unix socket, Mach or Apple
  Event settings: the ssh agent and tmux are blocked with no way to open them,
  the keychain is reachable with no way to close it, and localhost goes through
  its proxy.

**The probe** (`os_sandbox.probe`, what `tests/test_os_sandbox.py` executes):
`/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny file-read*
file-write* (subpath ...))'`, the primitive both CLIs use on macOS, with
brindle's deny list. Denied: direct `cat`, a path in an env var into `bash -c`,
`os.environ` in Python, case/array indirection, `shopt dotglob`, three nested
shells, a symlink outside the store resolving into it. Positive control: a write
and read beside the store. Inside a sandbox Seatbelt doesn't nest
(`sandbox_apply: Operation not permitted`): the probe reports `nested`, the
launch is refused, and the test skips with an explicit "NOT VERIFIED" reason, to
be rerun on an unsandboxed runner. The probe isn't Claude Code's generated
profile: that was exercised by the `claude -p` run above, which needs a model
turn and isn't in the test suite.

## Gaps and what only real runs prove

- agy: `--sandbox` has no effect where agy finds the sandbox unavailable, and
  nothing in its payloads says whether a command ran sandboxed. brindle's own
  capability probe (same primitive, same machine) and the version floor are the
  guard; a machine where agy's availability check differs from brindle's would
  run agy commands unsandboxed with the hook still denying bypasses and the
  rules still applying. An external execution broker would close this; not built.
- agy: no command-environment scrubbing. Under the Gemini-API route the key is
  visible to commands; under the default Google sign-in the token is a denied
  file.
- Linux: the bubblewrap probe and both CLIs' namespace sandboxes are untested
  here (macOS only). Ubuntu may need an AppArmor profile for bwrap; brindle
  never changes host security settings and refuses the launch instead.
- Not sandboxed: Claude Code's file tools, hooks, MCP servers, WebFetch; agy's
  `read_file`/`write_file` tools (they follow the deny rules); the interactive
  supervisor's own chat.
- Not measured: overhead on a loaded Mac; cloud and subscription auth under the
  sandbox for every route (the CLI authenticates outside it, so none is expected
  to change); hook and MCP asks and resume under a sandboxed session (hooks run
  outside; a resumed session gets the same `--settings`).
- The daemon's Unix socket is reachable from sandboxed commands (git's sign-in
  needs it). Its routes check the calling process and a token in `daemon.json`,
  which the sandbox keeps unreadable; the socket path can't scope routes, so
  that guard stays the boundary there.
