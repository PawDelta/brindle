# Process count: leaks and idle spawn rate

Report (2026-11-11): a user on 0.6.3 stable (macOS, Microsoft Defender) saw over
100 Claude-related processes and suspended `zsh`/`eval` subshells (Claude Code's
Bash-tool shells: shell snapshots, cwd syncs, background timers). Defender
inspects every new process, so frequent spawning makes the machine sluggish.
brindle never sends SIGSTOP (grep is clean); the suspended shells are Claude
Code's, kept alive by CLIs brindle started.

Two questions: which brindle-started processes can outlive their purpose
(leaks), and how many processes brindle starts per minute at idle (spawn rate).

## How it was measured

All measurement was read-only: nothing was killed that this investigation
didn't start.

- **Live census** (this machine, installed app = a main build with idle sync):
  `ps -axo pid,ppid,stat,etime,command` plus `ps xeww` for the environment (only
  `BRINDLE_AGENT_ID` and `BRINDLE_HOME` were read out of it, never printed),
  joined with the agents table read with `sqlite3 file:…?mode=ro`. Each
  process is attributed to the nearest ancestor whose command line carries
  brindle's MCP config (an agent CLI), or to the daemon, a sidebar, another
  brindle process, or nothing of brindle's.
- **Live spawn sample**: `ps` once a second for 3 minutes; each pid seen for
  the first time is attributed by its parent chain. A git run of a few
  milliseconds is mostly missed by a 1 s sample, so these numbers are lower
  bounds. (macOS hands out pids at random, so the pid counter can't give a
  machine-wide total.)
- **Per-sweep count** (deterministic): a throwaway test ran the daemon's
  `ReadinessWatch.sweep` against real git checkouts (4 session checkouts: 3
  idle, 1 paused; origin a local bare repo, so no network) with
  `subprocess.Popen.__init__` wrapped to count every process started, after
  one warm-up sweep. The `v0.6.3` column is the same sweep with the two
  functions v0.6.3 doesn't have (`refresh_idle_sessions`,
  `refresh_drift_flags`) turned off; the rest of the sweep is the same in both.
  The harness was not committed; the committed regression test
  (`test_a_sweep_starts_no_git_for_a_session_whose_refs_have_not_moved`) checks
  the same thing for one session.

## Leaks

### L1. Closing an agent's window before stopping its processes (fixed)

`agents.pause` (a session's pause, `stop_procs=True`), `agents._stop` (used by
`kill`, `close`, `pause_worker`, the reviewer's and auditor's `close_later`) and
the kill path of `resume` closed the agent's tmux window first and only then
read the process table to find the agent's processes (`procs.stop`).

Closing the window hangs up the CLI in it, and the CLI exits. What it started
in a session of its own (Claude Code's Bash-tool shells and background jobs,
which leave the pane's process group) survives the hangup and is reparented to
launchd (ppid 1). `procs.agent_pids` finds an agent's processes as the
descendants of its pane and of the processes carrying its MCP config; once the
CLI is gone, those shells descend from neither, and their own command lines
(`zsh -c source …snapshot… && eval '…'`) name no agent. `_stop` captured the
pane's pids before the close, but walked their descendants after it, when
the tree was already broken. The cull's sweep can't find them either (step 1
starts from `procs.all_agent_ids`, which reads agent ids off the CLIs that are
now gone). Nothing ever reaped them; a shell that had stopped (state `T`)
stays stopped forever, since only SIGKILL or SIGCONT reaches it.

Fix: stop the processes first, while the tree is whole, then close the window.
`procs.stop` already skips the caller and its ancestors, so a pause running in
the chat's own pane (`_ended`) is still safe. The cull's own pause of a session
whose chat is gone (`stop_procs=False`) was already safe: it stops the
processes from the table it read before anything closed.

Test: `tests/test_procs.py::test_a_stop_leaves_nothing_the_agents_cli_started`
(rows: session pause, worker stop). A fake process table in which closing the
window ends the CLI and reparents its Bash shell (and the test run under it) to
launchd. On the unmodified code nothing is stopped (`set() >= {5002, 5003}`
fails); with the fix both are.

The resume path's kill (`agents.resume`, for a failed or settling agent) had the
same order and is reordered the same way; it has no test of its own (it is the
same `procs.stop` call, and the resume tests in `test_sessions.py` and
`test_auth_failure_handling.py` pass).

### L2. The learned-rules `claude -p` call left its helpers on a timeout (fixed)

`learned_rules.default_call`'s Claude path ran `claude -p` with
`subprocess.run` (with `CALL_TIMEOUT`, five minutes), in the caller's own session and with every
tool and every MCP server the person has configured. On a timeout
`subprocess.run` kills only the CLI itself: the MCP servers and shells it
started kept running (and kept the output pipes open).

Fix: the call goes through `spawn.read` (its own session; a timeout stops the
whole group; `reap=True` stops whatever it leaves in its group once it has
answered), with `--tools "" --strict-mcp-config --no-session-persistence`
(`model_access.NO_TOOLS`): grouping review findings needs no tools, and the
findings are text brindle doesn't control.

Test: `tests/test_learned_rules.py::test_a_claude_call_that_times_out_leaves_nothing_it_started_running`.
A fake `claude` forks a helper and hangs. On the unmodified code the helper is
still running after the call (`ps` state `S`); with the fix it is gone.

### Checked and found sound

- **Reviewers and auditors**: a reviewer's `submit_review` and the pipeline
  close it with `close_later` → `_close` → `kill` → `_stop`; an auditor is closed
  with `agents.close` (or `close_later` as a review-mode agent). Both go through
  `_stop`, so L1's fix covers them. Their processes are found by the MCP config
  on their command lines, Codex's TOML form included.
- **Done, paused, closed and culled agents**: the cull (every minute, from each
  sidebar, and `brindle prune`) stops every process of an agent whose row is
  paused, done, dismissed, failed with a dead pane, or gone (`cull.sweep` step 1).
  On this machine none of the 22 paused coordinators, 13 paused chats, 2 paused
  reviewers or 12 done agents had a process left.
- **`brindle mcp` servers of ended sessions**: found by their own environment
  (`BRINDLE_AGENT_ID=` with a matching `BRINDLE_HOME`, set by brindle itself),
  whatever their parent, so an orphaned one is still a root for step 1. The
  only orphaned `brindle mcp` on this machine (pid 84333, 45 hours old) was
  started by a test run (`tests/test_mcp_stale_engine.py::test_an_app_update_is_served_by_the_new_install_on_the_same_connection`,
  under pytest's tmp dir): a test-suite leak on developer machines, not a user's.
- **tmux panes left with live processes**: `reap.py` and `cull.prune_retired`
  terminate the descendants of an orphan session's panes before killing it.
- **Reads through `spawn.read`** run in their own session and stop the whole
  group on a deadline or timeout; `_stop_reads_at_exit` stops them when the
  process exits.
- **Reported workers** keep their CLI until `stale_after` (30 minutes by
  default) so their supervisor can send them back for fixes. That is by
  design, not a leak, but each one holds a CLI, its MCP servers and any shells
  for that long.

## Spawn rate

### S1. Idle sync and drift flags: git for every session, every 5 seconds (main only; fixed)

The daemon's watch runs `ReadinessWatch.sweep` every 5 seconds
(`lifecycle.WATCH_INTERVAL`). On main (not in v0.6.3) the sweep starts with
`session_sync.refresh_idle_sessions` and `refresh_drift_flags`, which for
every session that isn't done (paused ones included) ran, each sweep:
`git remote` and `git symbolic-ref` (twice each: `upstream`), `git status`
(`_quiet`), `git show-ref`, `git rev-parse`, `git rev-list --count` (`drift`, via
`sync`), and `git show-ref`, `git log`, `git diff --name-only` (drift flags):
14 git processes per session per sweep, whether anything had changed or not.

| per-sweep count (4 sessions: 3 idle, 1 paused) | warm-up sweep | each later sweep | per minute |
|---|---|---|---|
| v0.6.3 | 0 | 0 | 0 |
| main, before | 58 | 56 | 672 |
| main, after | 46 | 0 | 0 (+1 fetch per repo per 5 minutes) |

That is 168 processes a minute for every open session, on top of what agents
do, each one inspected by an antivirus.

Fix (`session_sync.Seen`, kept by the `ReadinessWatch` between sweeps):

- a session that an idle sync found synced (`up_to_date` or `synced`) is left
  alone while its HEAD and `origin/<default>` are as they were, read from the
  checkout's git files (`session_review._fingerprint`, no git run);
- drift measurements (oldest unlanded commit, files that differ) are reused
  while HEAD, `origin/<default>` and the session's recorded squash landings are
  unchanged; whether the oldest commit is past 48 hours is still judged every
  sweep, and the row is written only when it changes;
- `upstream()` (the repo's remote and default branch) is believed for 60 s;
- a session that isn't quiet, or is behind, conflicting or busy, is checked
  every sweep as before; a checkout whose refs can't be read from its files
  (reftable, say) is asked of git every time, as before.

Test: `tests/test_session_sync.py::test_a_sweep_starts_no_git_for_a_session_whose_refs_have_not_moved`
(14 git processes on the second sweep on the unmodified code, none with the
fix; a commit pushed to origin is still merged in after the next fetch).

### S2. `claude -p /usage`: one CLI per sidebar every 5 minutes (fixed)

Each sidebar (`brindle watch`) refreshes the Claude plan's usage with `claude -p
/usage` at most every 5 minutes, on a clock of its own process
(`quota._claude_usage_at`): N sidebars started N Claude CLIs every 5 minutes.
Each CLI start also starts every MCP server the person has configured and
Claude Code's login-shell snapshot (`zsh -l`, which sources the person's
`.zprofile` and `.zshrc`).

Fix: the clock is shared across processes (the mtime of
`<brindle home>/claude-usage.stamp`; the answer was already shared, in
`quota.json`), and the probe runs with `--strict-mcp-config` (no MCP servers)
and `reap=True`. Test:
`tests/test_quota.py::test_sidebars_share_one_claude_usage_run_per_refresh`
(three fresh sidebar processes: three runs before, one after).

### Other sources, measured and left as they are

- **gh**: an open session PR costs one `gh pr view` per 90 s
  (`PR_STATE_TTL`) and one merged-check per 60 s (`MERGE_POLL_SECONDS`), the same
  in v0.6.3 and main: under 2 a minute per open PR.
- **Fetches**: idle sync fetches `origin/<default>` once per repo per 5 minutes
  (`IDLE_FETCH_SECONDS`); other fetches happen on events (a merge, a resume, a
  new session), not on a clock.
- **The cull**: once a minute per sidebar process (two `ps` for the process
  table, `tmux list-panes`, and git only for workers it judges). With several
  sidebars there are several sweeps a minute; each is cheap next to S1.
- **The readiness check** for autopilot sessions whose goal is reached already
  reuses its answer while the checkout's fingerprint is unchanged.
- **`_quiet`** asks tmux whether each worker's window is alive
  (`window_alive`, one `tmux display` each) for an idle supervisor with
  workers; one call a sweep while a worker is alive.
- **Developer machines only**: every pytest process (each xdist worker) runs a
  watchdog shell (`tests/conftest.py::private_tmux_server`) that starts a one-second wait process
  every second while the run lasts: about 60 processes a minute per worker.

## Live numbers on this machine

Process census (one snapshot, installed app = a main build, before this
branch's fixes): 723 processes of this user, about 50 of them under the 8 live
agent CLIs (3 to 16 each: the CLI, its `brindle mcp`, `caffeinate`, the shell
running a command and what it runs). No process belonged to any of the 49
paused or done agents, and no Claude Bash-tool shell was orphaned. So L1 isn't
showing here now. It needs a shell that outlives the CLI's hangup; this machine's
shells are bash with no long background jobs, while the reporter's are zsh
login shells, some stopped.

Spawn sample, 3 minutes, with the machine's sessions as they were (3 idle
chats, 5 workers at work; lower bounds, see above):

| started by | seen per minute | mostly |
|---|---|---|
| brindle daemon | 97 | git: status, rev-parse, symbolic-ref, rev-list, remote, show-ref (S1's set) |
| brindle sidebars | 76 | git (178 of 227) and tmux |
| other brindle (hooks, status line) | 19 | `brindle _hook`, `_statusline` |
| agent CLI trees | 96 | mostly this investigation's own `ps` sampling (167) and test runs |
| nothing of brindle's | 11 | |

The daemon's share is S1: with this branch the per-sweep count goes from 14
per session to 0 at idle (table above). The live "after" can't be sampled
here: the installed app runs main, not this branch.

The sidebars' git (about 60 a minute that the sample caught, so more in
fact) is **not addressed here**. It comes from each `brindle watch` process's
own refresh and cull, once per sidebar. It is the next spawn source to cut
(see What is left).

## What is left

- Sidebar git: each `brindle watch` process runs its own git for its view
  and its own cull every minute; measured above at 60+ a minute here. Next:
  find which refresh starts it (status_cache, the cull's reap) and share or
  fingerprint it as S1 does.
- Orphans that already exist (from before this fix) are not reaped: a shell
  reparented to launchd names no agent, and its environment can't be trusted
  to say whose it is (Claude Code's daemon passes the environment of whichever
  agent first started it to every session it hosts). Restarting the machine,
  or ending them by hand, clears them once.
- Claude Code's own processes per CLI (its MCP servers, its shell snapshot, a
  `caffeinate`) scale with the number of live agents; reported workers keep
  theirs for `stale_after`.
