# Permission prompts end to end, on real provider sessions

One run, 2026-10-20, of brindle's permission hold on real signed-in CLIs in
brindle-managed tmux panes, driven through the daemon API with the same
routes and bodies the app uses: `GET /v1/inbox`, then
`POST /v1/inbox/{agent}/permission` with `{request, digest, choice}`.

Engine: brindle `dev/desktop-app` at `7993a42` (permission-items merged),
installed into a scratch venv. CLIs: Claude Code 2.1.305, Codex 0.162.1,
Antigravity `agy` 1.4.3, OpenCode 1.19.3. Policy: `permission_policy: "on"`
for each scratch repo (set with `PATCH /v1/config`, scope repo), and
`permission_hold_seconds` left at its default (86400).

## Results

| | Claude Code | Codex | agy, `agy_approvals: brindle` | agy, `agy_approvals: prompt` | OpenCode |
|---|---|---|---|---|---|
| Request shown in `GET /v1/inbox` (`kind: permission`, right agent, command, choices) | PASS | PASS | PASS | not reached (see below) | PASS, as a terminal-only item |
| Allow once runs the tool | PASS | PASS | PASS | — | n/a (by design) |
| Deny refuses it | PASS | PASS | PASS | — | n/a (by design) |
| Allow always writes the rule shown | PASS (Claude's own rule) | PASS (brindle rule) | PASS (brindle rule) | — | n/a (by design) |
| `{choice: "terminal"}` releases to the CLI's prompt | PASS | PASS | PASS (refused, since agy has no prompt here) | — | n/a |
| App answer refused once at the CLI's prompt (409, no keys sent) | PASS | PASS | n/a | — | PASS |
| Forged DB answer ignored | PASS | PASS | PASS | — | n/a |
| Item clears after the answer | PASS | PASS | PASS | — | PASS |
| POST round trip | 0.23–0.24 s | 0.21 s | 0.20–0.21 s | — | — |
| Answer → tool ran | 0.14–0.52 s | 0.25–0.38 s | 0.09–0.12 s | — | — |
| CLI's own prompt visible while held | yes | **no** ("Running hook") | no (no prompt at all) | — | — |
| Long hold survives the CLI's hook timeout | not tested (single-digit-minute holds only) | hook not killed in 7 min, but **the model gave up** (finding 3) | PASS: held 10.9 min, then answered | — | — |
| Policy deny from the plugin | — | — | — | — | PASS |

None of the four CLIs needed a sign-in. OpenCode has no credentials
(`opencode auth list`: 0), but its free `opencode/*` models work without
any. agy prompt mode could not be tested on this machine: see finding 6.

## Findings

1. **Claude Code started in auto mode, which skips brindle completely.** The
   first Claude session (profile with no `permission_mode`) came up with
   "auto mode on". Claude's classifier approved all five commands itself,
   no `PermissionRequest` ever fired, and the app had nothing to show. With
   `permission_mode: default` in the profile it asked as expected. Anyone
   whose Claude Code defaults to auto mode will see no permission items
   in the app.
2. **Claude's "Allow always" can mean "accept edits for this session".** For
   `touch e2e-once`, Claude's `permission_suggestions` were
   `addDirectories` (session) plus `setMode acceptEdits` (session). The app
   labels that "Allow always" and describes the rule correctly
   ("…permission mode acceptEdits (session)"). Answering it switched the
   whole session to accept-edits, so the next two `touch` commands ran with
   no prompt. Claude's own terminal prompt offered something narrower for
   the same request ("don't ask again for: `touch e2e-once *`"). For
   `git tag e2e-always2`, the suggestion was a real persistent rule,
   `Bash(git tag *)` in localSettings, which was written to
   `.claude/settings.local.json` and applied (the next `git tag` ran with
   no hold).
3. **Codex: a long hold costs tokens, and the model can give up.** While the
   request was held, Codex's model kept polling its exec cell. That was one
   `wait` call every ~31 s, each a model call (~20.6k input tokens, mostly
   cached). After about 7 minutes it terminated the cell itself, said
   "done", and skipped the remaining commands. The hook was not killed by
   a timeout; the model cancelled the call. So with Codex, `hold_until` is
   not what bounds a hold. Also, the hook's row stayed in
   `permission_requests` afterwards (the hook was killed before its
   cleanup). It is not shown in the inbox, which is correct, but it is
   never removed.
4. **Codex hook trust is one slot shared by every brindle install.** The
   trust key is always `/<session-flags>/config.toml:permission_request:0:0`.
   The scratch install (a different BRINDLE_HOME and interpreter, so a
   different command) overwrote the owner's `trusted_hash` in
   `~/.codex/config.toml`. Per `codex_hook.status`, the owner's own brindle
   then reads that as REMOVED and won't trust it again on its own. This
   will happen to anyone who runs a dev checkout of brindle next to the
   installed one. Restored from backup after the run.
5. **agy rule sync writes the scratch home's rules into the owner's real agy
   settings.** `sync_permissions` writes to `~/.gemini/antigravity-cli/settings.json`
   whatever the BRINDLE_HOME. The scratch home's seeded rules (aws, uv,
   python, npm, docker, gh, …), the e2e allow rules, a
   `daemon.json` deny and a `touch e2e-denied` deny all landed in the
   owner's file, and both scratch repos were added as trusted workspaces.
   Restored from backup after the run.
6. **agy prompt mode never prompted here.** The owner's agy settings have
   `agentMode: "accept-edits"` and a broad allow list. `touch`, `git tag`,
   `uname -s` and `rm` all ran without asking. agy has no CLI flag for an
   asking mode (`--mode` takes only accept-edits, auto, plan), so the
   "Go to session" path for agy can't be reached without editing the
   owner's agy settings, which I didn't do. That path is the same
   screen-read item as OpenCode's, which did pass.
7. **Codex deny reason reads as an allow.** Denied in the app, Codex shows
   "Blocked by hook — brindle: the person answered in the brindle app"
   (the Codex branch of `permission_request_decision`). Claude and agy say
   "…denied this in the brindle app". This is wording only; the call was
   refused.
8. **Three orphaned `codex app-server` processes.** They were started with
   brindle's hook flags at the first Codex launch, by the trust step's
   app-server, and ran with ppid 1 for ~20 minutes. They exited by
   themselves while the run was being cleaned up.
9. **The answer POST has a ~0.2 s floor.** The tool ran 0.09 s after the POST
   on agy, but the POST returned at 0.207 s: `answer_permission` checks
   the hold's outcome every `STEP_SECONDS` (0.2 s).
10. **Outside this test:** the app's `POST /v1/agents/{id}/message` reaches
    Claude Code as "Another Claude session sent a message…". Haiku treated
    it as a peer's request and declined to run a command from it. For
    those follow-ups I typed into the pane instead.

## Per-provider run

Commands were harmless and ran in throwaway repos: `touch e2e-*`,
`git tag e2e-*`, and for Codex `curl -s -o e2e-* https://example.com`. A
plain `touch` needs no approval in Codex's workspace sandbox, and
`approval_policy = "untrusted"` is rejected by Codex 0.162.1 ("no longer
supported"). The network call needs sandbox escalation instead.

### Claude Code (Haiku 5.5, low effort, `--strict-mcp-config`, `setting_sources: project,local`, `permission_mode: default`)

Prompt: run `touch e2e-once`, `e2e-deny`, `e2e-always`, `e2e-term`,
`e2e-forged`, one call each.

- `touch e2e-once`: the item showed `via: hook`, `tool: Bash`,
  `target: touch e2e-once`, choices allow_once / allow_always (with
  Claude's rule) / deny, `terminal_prompt_visible: true`, and Claude's own
  prompt was on screen. `allow_once` → 200 in 0.244 s, file at 0.518 s,
  pane "Allowed by PermissionRequest hook", item gone.
- `touch e2e-deny`: first forged:
  `UPDATE permission_requests SET answer='allow_once', channel='answered'`.
  21 s later there was no file and the item was still held with the same
  id and digest. Then `deny` → 200 in 0.235 s. No file appeared; pane
  "Error: brindle: the person denied this in the brindle app".
- `touch e2e-always`: `allow_always` → 200 in 0.233 s, tool at 0.142 s.
  The rule was session acceptEdits (finding 2), so the remaining two
  `touch` commands ran unasked. brindle wrote no rule of its own (correct
  when Claude supplies suggestions).
- `git tag e2e-term` (typed in the pane): `{choice: "terminal"}` → 200. The
  item turned into `via: prompt` with the prompt's wording and no choices.
  `allow_once` with the old id and digest → 409 "at the CLI's own prompt
  now". No tag yet. Answering "1" in the pane created the tag, and the
  inbox was empty.
- `git tag e2e-always2` then `git tag e2e-after`: `allow_always` → 200 in
  0.234 s, tag at 0.201 s. `.claude/settings.local.json` got
  `"allow": ["Bash(git tag *)"]`, and `e2e-after` then ran with no hold.

### Codex (`gpt-6-luna`, low effort)

Prompt: six `curl -s -o e2e-… https://example.com`, each with escalation.

- once: item `via: hook`, `tool: Bash`, allow_always offered "brindle's
  permission rule: allow bash exact '…e2e-once…'", and
  `terminal_prompt_visible: null` (not known). On screen: "Running hook",
  no Codex prompt. `allow_once` → 200 in 0.214 s, file at 0.381 s.
- deny → 200 in 0.214 s. No file; pane "Blocked by hook" (finding 7).
- always → 200 in 0.215 s, file at 0.253 s. permissions.json got
  `allow bash exact 'curl -s -o e2e-always https://example.com'`. The
  repeat of the same command later ran with no hold; history says
  "allowed by user rule b128b21f".
- forged: same SQL as for Claude. Then I left it held to test the hook
  timeout. Nothing ran, and the item stayed in the inbox with its id. At
  ~7 min the model cancelled the call itself (finding 3).
- term (typed in the pane): `terminal` → 200. Codex's own prompt then
  appeared ("Would you like to run the following command?"), and the
  inbox showed it as `via: prompt`. `allow_once` → 409. I answered `y` in
  the pane and it ran.

### agy, `agy_approvals: brindle` (`gemini-3.8-flash-low`)

Started with `--dangerously-skip-permissions`, and brindle's PreToolUse
hook gates every call. Prompt: six `touch` commands.

- once: item `tool: run_command`, `terminal_prompt_visible: false`. I left
  it held 652 s (10.9 min) to test agy's hook timeout (brindle writes
  hold + 30 s, 86430 s). It was still held, and `allow_once` → 200 in
  0.207 s, file at 0.093 s.
- deny → 200 in 0.206 s, no file.
- always → 200 in 0.207 s, file at 0.115 s, brindle rule
  `allow bash exact 'touch e2e-always'`. The repeat ran with no hold.
- forged: same SQL. 20 s later there was no file and it was still held;
  then `deny` → 200 in 0.214 s.
- term → 200. With no prompt of its own, agy refused the call (history:
  "brindle runs this agent without approval prompts, and no rule allows
  this").

### OpenCode (`opencode/glm-5.3-flash-free`)

I added `brindle permissions deny bash "touch e2e-denied"`. Prompt:
`touch e2e-denied`, then `touch e2e-prompt`.

- `touch e2e-denied`: denied by the plugin's pre-tool hook. No file;
  history "deny: denied by user rule 2f2e32d9".
- `touch e2e-prompt`: OpenCode's own "Permission required" box. The inbox
  showed `via: prompt`, the box's wording, no choices, and
  `terminal: "…answer it in the agent's terminal"`.
  `POST …/permission {choice: allow_once}` and `{choice: terminal}` both
  → 409. The pane was unchanged and no keys were sent. Enter in the pane
  (Allow once) ran it, and the item cleared.

## How it was run

- Scratch venv: `uv venv <scratch>/venv --python 3.12`, then
  `uv pip install <this checkout>` (the engine at the repo's root).
  The person's installed brindle and `~/.brindle` were not touched.
- `BRINDLE_HOME` was a symlink `~/.be2e` → `<scratch>/home`, because
  `daemon.sock` under the scratch path is longer than macOS's 104-byte
  socket path limit. `BRINDLE_TMUX_SOCKET=be2e` gave a private tmux server.
  `BRINDLE_*`, `CLAUDE*` and `TMUX*` were stripped from the environment
  of everything started.
- `brindle daemon --tcp`. The driver called the daemon over TCP with the
  token from `daemon.json`, the same routes and bodies as the app. The
  driver runs under a brindle agent, so the daemon's Unix-socket guard
  would rightly refuse it. The hooks used the Unix socket as normal.
- Each session: `POST /v1/sessions {repo, prompt, watch: false, autopilot: false}`.
  The supervisor profile was overridden (user or repo `agents/supervisor.md`)
  with a two-line prompt and the cheapest model for each provider.
- Forged answer: a direct `UPDATE` of the hook's `permission_requests` row
  in `brindle.db` while the request was held.
- Latency: `t0` just before the POST, then the target file is polled every
  5 ms.
- Cleanup: every agent closed (`POST /v1/agents/{id}/kill`), daemon
  stopped, tmux server killed, `~/.be2e` removed, scratch repos, home and
  venv deleted. `~/.codex/config.toml` and
  `~/.gemini/antigravity-cli/settings.json` were backed up before the run
  and restored after it; their diffs held only this run's additions.
  Claude Code and Codex may have kept project-trust and session-history
  entries for the deleted scratch paths in their own stores.
