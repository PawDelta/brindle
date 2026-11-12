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
  `ReadinessWatch.sweep` against real git checkouts (4 session checkouts in
  one repo: 3 idle, 1 paused; origin a local bare repo, so no network) with
  `subprocess.Popen.__init__` wrapped to count every process brindle started
  (not the helpers git itself starts, such as a fetch's transport). "Before"
  counted steady sweeps after one warm-up. "After" ran a full fetch interval:
  60 sweeps on a fake clock, one every 5 s, so the fetch and the 5-minute
  renewal of each session's upstream both fall inside it. The `v0.6.3` column
  is the same sweep with the two functions v0.6.3 doesn't have
  (`refresh_idle_sessions`, `refresh_drift_flags`) turned off; the rest of the
  sweep is the same in both. The harness was not committed; the committed
  regression test
  (`test_a_sweep_starts_no_git_for_a_session_whose_refs_have_not_moved`) checks
  the unchanged-sweep case for one session.

## Leaks

### L1. Closing an agent's window before stopping its processes (fixed)

Every path that closes an agent's tmux window closed it before its processes
were found:

- synchronous stops: `agents.pause` (a session's pause, `stop_procs=True`),
  `agents._stop` (used by `kill`, `close`, `pause_worker`, the reviewer's and
  auditor's `close_later`) and the kill path of `resume`, which read the
  process table (`procs.stop`) only after the close;
- closes that leave the processes to a later cull: `pause(stop_procs=False)`
  (the cull's own pause of a session whose chat is gone, and
  `service.sessions.place` pausing a running session for a new one),
  `close_paused` (the daemon pausing every session on its way out, followed by
  a detached cull) and `settle_panes` (stale panes closed as a session resumes).

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

Fix, synchronous stops: stop the processes first, while the tree is whole,
then close the window. `procs.stop` already skips the caller and its
ancestors, so a pause running in the chat's own pane (`_ended`) is still safe.

Fix, closes left to a cull: just before the close, `procs.hand_off` notes
every process under the panes about to close in
`<brindle home>/closing-procs.json`. Each process is noted by its pid and its
birth: the kernel's start time, with the boot id on Linux, and `ps`'s start
time on macOS. `procs.lineage` reads each process's parent, command line and
birth together (one `ps -axo pid,ppid,lstart,command` row on macOS; one read
of `/proc/<pid>/stat` on Linux, with the command line kept only if a second
read finds the same birth). So the tree a note is taken from and the births
it records are the same processes', even if a pid is reused right after the
read. Claude Code's daemon and everything under it
are never noted: the sessions it hosts may be other agents'. The record is
written under a lock to a temporary file of its own and then moved into
place.

Each cull sweep then calls `procs.handed_orphans`. It reads the process table
and the births as they are at that moment, not the table the sweep began
with. It ends each noted process that is still the one noted (same pid, same
birth) and whose chain of noted parents now leads to launchd, i.e. one the
close left without its CLI. It also ends whatever that process has started
since the note, again leaving out the daemon and what it hosts.

The cull ends them with `procs.terminate(..., owned=births)`. Just before
each pid's own SIGTERM, and again before its SIGKILL, it reads that pid's
birth on its own (`procs.birth_of`). It signals the pid only if it is still
the process the birth names, so a pid that came round again is spared, even
when an earlier signal in the same batch is what freed it. (No pidfd on macOS:
the window between that read and the signal is the few milliseconds the two
calls take.)

What it ends stays noted until a later sweep finds it gone. A process that
exec'd another program keeps its birth, so it is still found. A noted pid
that came round again to another process, even one running the same command,
has a later birth and is dropped. A noted process whose parent still runs is
left alone. Records also go after a day. This costs one `tmux list-panes`, a
process-table read and a start-time read per close, and no waiting.

Test: `tests/test_procs.py::test_a_stop_leaves_nothing_the_agents_cli_started`,
one row per path: session pause, worker stop, session pause left to a cull,
the daemon's batched pause on its way out, a stale pane settled. A fake
process table in which closing the window ends the CLI and reparents its Bash
shell (and the test run under it) to launchd; the last three rows run a real
cull sweep after the close. On the unmodified code nothing is stopped
(`set() >= {5002, 5003}` fails) in every row; with the cull's new step set
aside, the three deferred rows still fail.
`test_what_a_close_left_behind_is_ended_and_nothing_else` covers six
cases:
- a live parent: nothing is ended;
- a pid that came round again for the same command: the new process is spared;
- the same, just after the note's read;
- an exec: still ended;
- a child started after the note: ended too;
- a Claude daemon started from the pane: neither it nor the session it hosts is ended.

`test_a_pid_that_came_round_again_is_never_signalled` signals two pids. It
covers four cases:
- one reused before any signal: it gets no signal;
- one reused between its SIGTERM and SIGKILL: it gets no SIGKILL;
- the second freed by the first's SIGTERM and reused: it gets no SIGTERM;
- the second freed by the first's SIGKILL and reused: it gets no SIGKILL.

What is left: a child started during the few seconds between SIGTERM and
SIGKILL, by a process that then dies, is noted by no one.

The resume path's kill (`agents.resume`, for a failed or settling agent) is
reordered like the other synchronous stops. It has no test row of its own:
it is the same `procs.stop` call, and the resume tests in `test_sessions.py`
and `test_auth_failure_handling.py` pass.

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

| 4 sessions in one repo (3 idle, 1 paused) | warm-up sweep | an unchanged sweep | per minute, over a full 5-minute interval |
|---|---|---|---|
| v0.6.3 | 0 | 0 | 0 |
| main, before | 58 | 56 | about 672 |
| main, after | 46 | 0 | 2 |

Before, that is 168 processes a minute for every open session, on top of what
agents do, and an antivirus inspects each one. After, over a full 5-minute
interval (60 sweeps), brindle started 10 processes: each session's upstream
renewed once (`git remote` and `git symbolic-ref`, 4 sessions × 2), one
`git config` and the repo's one `git fetch`. The fetch starts git's own
helpers besides (a transport such as `git-remote-https`, maybe a credential
helper), which the count doesn't include. In general: 2 processes per session
plus one fetch per repo every 5 minutes, while nothing moves. A session whose
HEAD or `origin/<default>` moves pays the old cost once, on the sweep that
sees it; one that isn't quiet pays `_quiet`'s checks every sweep, as before.

Fix (`session_sync.Seen`, kept by the `ReadinessWatch` between sweeps):

- a session that an idle sync found up to date is left alone while its HEAD
  and `origin/<default>` (read from the checkout's git files with
  `session_review._fingerprint`, no git run) are the very shas the sync
  checked. `drift` now resolves both in one `rev-parse` and measures from
  those shas, and the outcome carries them. It is never a look taken after the
  sync: another fetch or commit may have moved past what was checked. A merge
  the sync made is found up to date, and remembered, on the next sweep;
- a session with no `origin/<default>` has nothing to sync from and is skipped;
- drift measurements (oldest unlanded commit, files that differ) are reused
  while HEAD, `origin/<default>` and the session's recorded squash landings are
  unchanged; whether the oldest commit is past 48 hours is still judged every
  sweep, and the row is written only when it changes;
- `upstream()` (the repo's remote and default branch) is believed for 5
  minutes, as long as the sweep waits between fetches;
- a session that isn't quiet, or is behind, conflicting or busy, is checked
  every sweep as before; a checkout whose refs can't be read from its files
  (reftable, say) is asked of git every time, as before.

Test: `tests/test_session_sync.py::test_a_sweep_starts_no_git_for_a_session_whose_refs_have_not_moved`
checks three things. A commit that reaches `origin/main` just as a sync
finishes is merged on the next sweep (the first version of this fix stored a
look taken after the sync, and skipped it). An unchanged sweep then starts no
process (the unmodified code started 14). A commit pushed later is merged
after the next fetch.

### S2. `claude -p /usage`: one CLI per sidebar every 5 minutes (fixed)

Each sidebar (`brindle watch`) refreshes the Claude plan's usage with `claude -p
/usage` at most every 5 minutes, on a clock of its own process
(`quota._claude_usage_at`): N sidebars started N Claude CLIs every 5 minutes.
Each CLI start also starts every MCP server the person has configured and
Claude Code's login-shell snapshot (`zsh -l`, which sources the person's
`.zprofile` and `.zshrc`).

Fix: the clock is shared across processes. It is the mtime of
`<brindle home>/claude-usage.stamp`, and the answer was already shared, in
`quota.json`. A refresh is claimed under an exclusive lock (`flock` on
`claude-usage.stamp.lock`): the stamp is read and, when a refresh is due, set
to now before the lock is let go, so two sidebars looking at once can't both
find it due. The probe also runs with `--strict-mcp-config` (no MCP servers)
and `reap=True`. Tests in `tests/test_quota.py`:
`test_sidebars_share_one_claude_usage_run_per_refresh` (three fresh sidebar
processes one after another: three runs before, one after) and
`test_two_sidebars_looking_at_once_start_one_claude_usage_run` (the second
looks while the first is reading the stamp: two runs without the lock, one
with it).

### S3. Sidebar git: a safety refresh in every sidebar, and a cull per sidebar (fixed)

Each sidebar (`brindle watch`) re-reads its view every 10 s while idle
(`watch.IDLE_SECONDS`). Two things in it started git with nothing changed:

- **The status cache's safety refresh.** `status_cache.cached_status` keeps
  each worktree's status behind a shallow fingerprint (index, HEAD,
  `logs/HEAD`, the base ref, the root's own entries), and ran `git status`
  and `git rev-list --left-right --count` again every 5 minutes (`TTL`)
  regardless, to catch edits below the root. The cache is per process: every
  sidebar paid it for every worktree it shows or checks (`view.retired` looks
  at each finished worker's worktree too).
- **The cull.** Each sidebar started `cull.sweep` once a minute on a clock of
  its own process (`watch._loop`'s `culled_at`): N sidebars, N sweeps a
  minute. The daemon runs no periodic cull (only `cull_detached`, on events
  such as a launch), so the sidebars' sweeps are the only periodic ones.

Measured with `subprocess.Popen.__init__` wrapped, as for S1: 4 sessions in
one repo (the main checkout and 3 session worktrees, each with an idle
supervisor), 240 idle refreshes on a fake clock (40 minutes at the idle
pace: `view.snapshot`, `view.autopilot_entry`, `watch_costs.local`), and two
sidebars whose own minutes fall 30 s apart, each calling
`_cull_in_background` when its minute is up. The warm-up look before the
count (the sidebar's first) is 18 git before (a status, a rev-list and the
base ref's look per worktree) and 21 after: one `git config --list` per
worktree besides (see Fix).

| 40 idle minutes, from just after the first look | refreshes (one sidebar) | cull sweeps (two sidebars) | cull processes (git) |
|---|---|---|---|
| before | 48 git | 80 | 1229 (589) |
| after | 0 | 40 | 629 (309) |

Before, the refreshes' 48 are `git status` and `rev-list` in each of the 3
worktrees at each of the eight safety refreshes; the main checkout has no
base branch, so no status. A sweep with 4 idle sessions is about 8 git (two
per running agent, see What is left) and 8 other processes (`ps` twice,
`tmux`). In general, before: per sidebar, 2 git per shown or checked
worktree every 5 minutes, plus a sweep a minute; after: the sidebar's first
look (as before), then no git from an idle sidebar's view, and one sweep a
minute between all sidebars, however many there are.

Fix:

- `watch._claim_cull`: the cull's clock is shared, the mtime of
  `<brindle home>/cull.stamp`, claimed under `flock` on `cull.stamp.lock` as
  S2's usage stamp is: the stamp is read and, when a sweep is due, set to now
  before the lock is let go. A sidebar whose minute is up and finds another's
  sweep less than a minute old skips it. An unusable stamp claims nothing
  (that sidebar sweeps, as before).
- The safety look runs without git: `status_cache._deep_fingerprint` stats
  what `git status` looks at below the root: each tracked file (read from
  the index by `status_cache.index_paths`, versions 2 to 4), every directory
  except those git reported ignored (`git status --ignored=matching` names an
  ignored directory as `dir/` without listing it) and nested repositories,
  each `.gitignore`, and every file outside the worktree that decides what is
  ignored (`status_cache._ignore_sources`), both the link and what it points
  at: `info/exclude`, the default global ignore file, the config files git
  may read even before they exist (system, global, the repo's, the
  worktree's), and, from git's own reading of the config
  (`git config --list --show-origin --includes`, so its parsing, escapes and
  includes are git's), every file it read, every `include.path` and
  `includeIf` path whatever its condition, and every `core.excludesFile`.
  That `git config` runs on a sidebar's first look at a worktree and again
  only when one of those files, or the branch (for `onbranch:` includes),
  changed; a list read while one of them changed is asked for again. A
  `git config` that fails (busy, or an error) gives no list: the safety look
  is a git look until one succeeds. A file new anywhere git looks, an empty folder deep down included,
  changes its directory. git runs only when that moved.
- The baseline is stat'ed just before each git look (the first look's
  included), leaving out only what the previous look reported ignored (the
  first look leaves out nothing), then made to leave out what this look
  reports ignored. So it always stands for what git saw: a change made while
  git looks shows at the next safety look, a folder that stops being ignored
  is watched from the next look on (that look itself is a git look, the
  folder being new to the fingerprint), and the first safety look after a
  sidebar opens needs no git. An index this doesn't read (none, a split or sparse index, a
  submodule) gets `git status` at each safety look, as before.
- ahead/behind is kept while HEAD and the base ref (read from the git files,
  `status_cache._refs_key`) are the very commits it was counted for: a busy
  worktree's edits re-run `git status`, no longer `rev-list`. The upstream
  git's status line names (any remote, any branch name, already mapped
  through the remote's refspec, or a local branch) is watched as a ref
  too, so its moving re-runs git at the next refresh. A new upstream (the
  first look's too) is read just after the look that reported it; if its ref
  file or packed-refs changed since that look began, the next safety look
  asks git.
- Freshness is as before or better: a change to the index, HEAD, the base
  ref, the upstream or the root's entries shows on the next refresh; any
  other change git would report, at the next safety look (within 5
  minutes), now found by stat instead of git.

Tests in `tests/test_watch.py`:
`test_an_idle_sidebar_starts_no_git_until_something_changes` (four quiet
sessions, forty minutes of idle refreshes from just after the first look
start no process, where the unmodified code started `git status` and
`rev-list` at each safety look; then an upstream on another remote moving
shows on the next refresh, and at the next safety look: a file new in an old
empty folder three levels down, an edit in a subdirectory of a worktree
with a v4 index, a folder a tracked `src/.gitignore` stops ignoring and a
file new in it afterwards, and a file no longer ignored once the
`core.excludesFile` (a symlink, named by a config included through a path
git writes escaped) is edited through its link) and
`test_background_cull_keeps_one_sweep_in_flight` (a
second sidebar due 30 s after the first's sweep doesn't sweep; the
unmodified code did).

### S4. What S3 left in an idle sidebar's refresh (fixed)

After S3, release.144 still measured about 60 processes a minute per idle
`brindle watch --sidebar`. One idle refresh (every `IDLE_SECONDS`, 10 s),
read with `subprocess.Popen.__init__` wrapped against a copy of this
machine's database (15 workspaces shown), started 41 processes, every time:

- **git, from a cache that never hit**: a worktree whose base branch was
  deleted (6 here) makes `git rev-list <base>...HEAD` fail. A failure was
  never cached, so `status_cache.cached_status` ran `git status` and the
  failing `rev-list` again for each of its three callers in a refresh
  (`warm`, `retired`, `workspace_entry`): 36 git per refresh.
- **tmux, re-read every refresh**: `list-panes`; `capture-pane` for each
  running agent whose status isn't idle (`agents.reconcile`, from
  `view.agent_entry`); `display` for the terminal showing the pane
  (`sidebar_logo.viewer`) and two for `agents.sidebar_come_home`; one
  `display -t <pane> #{pane_dead}` per active worker from the goal line's
  worker count (`autopilot.working_workers` judged liveness without the
  refresh's pane snapshot); and, in a terminal that can't show the braid,
  `display #{client_termfeatures}` every 10 s (`Braid.image`).

Fix:

- `status_cache.cached_status` (a wrapper around the cache, now
  `_cached_status`) remembers a git failure while HEAD reads from the git
  files and the base ref names nothing there, and fails again without git
  while the fingerprint and refs are the very ones git failed under. The
  base branch coming back (a loose ref, `packed-refs`, `origin/<base>`)
  changes them, and git is asked again.
- `watch.TmuxLook` keeps the refresh's pane snapshot while nothing says tmux
  changed: a key, a database commit or a failed pass take it again, and so
  does `TMUX_LOOK_SECONDS` (60 s) passing. Screens are read
  (`view.snapshot(read_screens=...)`), and while quiet the viewer, the
  sidebar's home and the braid are asked, only with a fresh look.
- The goal line's worker count and a failed milestone's work state use the
  refresh's pane snapshot (`autopilot.working_workers(..., panes)`,
  `autopilot.work_state(..., panes)`).

Test: `tests/test_idle_spawns_sidebar.py` drives `watch._loop` itself
(a fake screen, a fake `tmux` on PATH, a real repo with a worktree whose
base branch is gone, a session root with a failed milestone and a worker
waiting on the person) and
counts every process started across two unchanged refreshes after the
first three: 18 on the unmodified code (4 of them git with only the
status_cache part undone), none with the fix.

What is left: an idle sidebar takes one fresh tmux look a minute
(`list-panes`, then `display` for the viewer, two for its home, the braid's
recheck where it can't show, and a `capture-pane` per running non-idle
agent): about 5 processes a minute instead of 6 refreshes' worth. That look
is what notices a CLI that died without writing the database, a missed
follow hook and a status its hooks left stale.

### S5. The daemon's sweep still ran git for sessions S1 didn't settle (fixed)

Live on the owner's machine (release.144, 28 agents, mostly paused or done
coordinators and supervisors) the daemon started about 145 git a minute
while idle (a 45 s `ps` sample, a lower bound): `status --porcelain
--untracked-files=no|all`, `rev-parse HEAD`, `symbolic-ref`, `rev-list
<sha>..HEAD`, `merge-base --is-ancestor`. A harness counting
`subprocess.Popen` over an unchanged round found where:

- **Readiness of a reached goal with no base branch.** `ReadinessWatch`
  caches readiness behind `_state`, which is None for a session on the repo's
  own checkout (no base branch), so `reasons` ran every sweep: a tracked
  `git status`, `rev-parse HEAD`, and `merge-base`/`rev-list` for the audit
  sha, only to say again that there is no branch to open a PR from. Four
  such sessions on this machine. Now: not ready, no git.
- **A session whose merge of origin/main conflicts** was merged and aborted
  every sweep (16 processes). The refs the merge conflicted at are now
  remembered like synced ones: tried again when HEAD or `origin/<default>` moves.
- **A session with unsaved work**: `_quiet`'s `git status` every sweep. The
  sweep's look now uses `status_cache` (git only when the checkout's files
  moved); the sync still asks git, under its locks, before it merges.
- **The repo's remote and default branch** are learned once per repo every
  5 minutes, not once per session.
- **The app's events feed** ran `git.status` (uncached) every 5 s for each
  checkout with no base branch; `status_cache.cached_any_status` caches it
  against its own branch.
- **The cull** (once a minute) now starts no git while nothing moved:
  `_last_edit` (an idle worker's latest uncommitted write, for
  `note_idle_unreported` and `note_unreported_commits`) takes the dirty files
  from `status_cache` and stats them; `note_unreported_commits` reads HEAD
  from the git files and checks its "told" mark, or a count of none remembered
  for the same HEAD, base and reported head, before any git;
  `login_pause` builds the agent's env without the git identity
  (`agent_env(identity=False)`: the login and route don't depend on it); and
  `config._committed_text` reads the branch's sha from the git files instead
  of `git rev-parse` on every `load_repo_config(branch=…)`.

Test: `tests/test_idle_spawns_daemon.py::test_an_unchanged_sweep_starts_no_process`
(a round of the daemon sweep and the feed over sessions in each of those
states, then a whole cull sweep: on the unmodified code the daemon's part
started 32 processes and the cull git for each live agent; now the daemon's
none and the cull no git; each fix reverted alone fails it).

- **The cull's screen reads**: `tmux.list_panes`' one listing now carries
  each pane's `#{window_activity}` (`PaneSnapshot.activity`).
  `decline_environment_setups`, `note_api_errors` and `note_model_refused`
  remember (an agent mark per reader, so the sidebars taking turns share it)
  the last output time a screen was read at with nothing to act on, and don't
  capture it again until the pane prints; `note_api_errors` takes the time
  from the listing instead of a `display-message` per worker. A read is
  remembered only once the output is 2 s old (tmux counts whole seconds).
- **`tmux.session_infos`** (the cull's `reap`) reads each session's
  `BRINDLE_HOME` once per session (name and creation time), not every minute.

Still per cull sweep (once a minute), whatever the number of agents: two `ps`
(the process table), `tmux list-clients` (`login_pause`'s last input) and
`reap`'s `tmux list-panes`.

### S6. Sessions at work: goal and Inbox reads, and the same repo questions for every checkout (fixed)

Live on the owner's machine (release.148, main 0c07614f) the daemon still
started about 500 processes a minute while sessions were at work. A 30 s
sample of git command lines by parent pid (`ps` every 20 ms, a lower bound)
caught 187 from the daemon: `show-ref --verify --quiet
refs/remotes/origin/main` (22), `rev-parse HEAD` (19), `merge-base
--is-ancestor main origin/main` and the reverse (27), `status --porcelain
--untracked-files=no` (14), `symbolic-ref … refs/remotes/origin/HEAD` (10),
`for-each-ref refs/heads` (8), `merge-base --is-ancestor <sha> HEAD` (8),
`rev-list --left-right --count origin/main...HEAD` (7), `git remote` (7),
`rev-list <sha>..HEAD` (7).

Where it came from: code added after v0.6.3 (the session-PR and landing
work). v0.6.3 has none of `new_since_merge`, `current_pr`, `_opened_pr`,
`refresh_idle_sessions`, `refresh_drift_flags` or `land_phase_prs` (checked
with `git grep` at the tag; v0.6.3 itself was not run here). The largest
share is not a sweep at all: the open app reads each session's goal
(`routes_read.goal` → `goal_phase` → `reasons`, `current_pr` →
`new_since_merge`, `has_pr_target`, `pr_block` → `drift`) and its Inbox
(`_opened_pr`: `rev-parse HEAD` per session), and the daemon serves those
reads, so their git shows under the daemon's pid. None of it was cached, so
it never settled, and it grew with every session and every merged PR.

Three classes:

- **Every read of a goal or the Inbox judged the session with git.** About
  15 processes per goal read of a session whose goal is reached (table
  below), the same ones again on the next read.
- **Questions about the repo, asked per checkout.** Whether `origin/<base>`
  exists, which of `<base>` and `origin/<base>` is ahead (`git.base_ref`),
  origin's default branch (`git.default_branch`) and whether there is an
  origin (`git.has_remote`) are the same in every checkout and change only
  when a ref or the config does. S1 and S5 cached their callers per session,
  behind each session's own refs; a session whose row or files change misses
  that cache every sweep, and when main moves every session does at once.
- **A reached goal with workers at work.** `ReadinessWatch` reuses a
  session's readiness while its whole state is unchanged, and that state
  holds its workers' rows. A worker changing status re-ran `reasons` each
  sweep: `git status` twice, `rev-parse HEAD`, the audit commit's
  `merge-base` and `rev-list`, ahead/behind, the merged PR's `rev-list`
  (and `rev-parse <merge>^{tree}`, `merge-tree` per sync merge), and, for a
  session behind origin, `session_sync.drift`'s five.

Fix, in `git.py` so every caller has it (`conflicts`, `gates`,
`rule_checks`, `agents.liveness`, the pipeline, the sidebars):

- `remote_branch_exists`, `default_branch`, `current_branch` and `base_ref`
  read HEAD, the loose refs and `packed-refs` (parsed once per version of
  the file) themselves: no process. `base_ref` asks git only which of two
  different commits is ahead: one `merge-base --is-ancestor`, by sha, once
  per pair of commits.
- `has_remote` asks `git remote` once per repo and per config as git reads
  it in that checkout (`status_cache.remotes`): checkouts whose settings
  list the same share the answer, and it is asked again when any file the
  config depends on changes. Those files are S3's (`_outside_sources`, from
  git's own `config --list --show-origin --includes`): every file git read,
  every include and includeIf target, there or not yet, the system's and
  the person's, and the branch for `onbranch:` conditions.
- `resolve` reads what names point at; `once`, `out_once` and `is_ancestor`
  keep git's answer to a question that names its commits by sha (it never
  changes; kept per repo and per version of its `shallow` and `info/grafts`
  files, and not at all in a repo with replace refs).
  `ahead_behind` and `merge_base` use them, and so do
  `session_sync.drift`, `session_review.new_since_merge` and the audit
  commit's check.
- Whatever the files can't say (a reftable repo, `GIT_DIR` in the
  environment, a path that isn't a checkout's root, a name like `main~2`) is
  asked of git every time, as before.
- `reasons` and `phase_reasons` take a checkout's uncommitted tracked files
  from `status_cache.tracked_dirty`: `git status --untracked-files=no` runs
  only when something it reads moved since it last looked. That is HEAD's
  commit, the index, every tracked file, each one stat'ed wherever it is
  (names from the index, read without git), a `.gitattributes` in any
  folder holding a tracked file (there or not yet, tracked or not), and
  every config and attributes file outside the worktree
  (`_outside_sources`, now with `info/attributes`, `core.attributesFile`
  and the global and system attributes files). The stamps are taken before
  git looks. An index that can't be read that way (a submodule, a split or
  sparse index), or a config git couldn't list, asks git every time.

- The Inbox reads each session's HEAD from the git files (`git.head`).

There is one mechanism: names are read from the git files, and git's answer
about commits is kept per sha. `new_since_merge`, `current_pr`, `reasons` and
`goal_phase` keep no result of their own; with the same HEAD, pushed commit,
base and origin commits and squash rows, every question they ask is already
answered. The repo-level reads are that same mechanism, not an extra one:
knowing that the inputs haven't moved means reading the refs.

Measured with `subprocess.Popen.__init__` wrapped: three sessions in one
repo (two with a reached goal and a worker at work, one of them with a
merged PR and a sync's merge on top; one quiet), `origin/main` moved once.
A round is the sweep, the app's feed, one Inbox read and one goal read per
session.

| processes started | sweep | feed | Inbox read | 3 goal reads | round |
|---|---|---|---|---|---|
| before, the round that sees main move | 65 | 16 | 3 | 45 | 129 |
| before, the next round, workers still at work | 40 | 2 | 3 | 46 | 91 |
| after, the round that sees main move | 29 | 6 | 0 | 1 | 36 |
| after, the next round, workers still at work | 2 | 2 | 0 | 0 | 4 |

Before, the round that saw main move started, by command, `show-ref` 16,
`merge-base --is-ancestor` 20, `symbolic-ref` 13, `remote` 6 and
`for-each-ref` 5 (the `--is-ancestor` and `symbolic-ref` counts include the
few about a checkout's own HEAD), and the next round much the same; after,
one repo question (which of main and origin/main is ahead), then none. The
4 left: the quiet session's sync finding its merge
synced (2, once), and `git status` for each of the two worktrees a worker
edited. The 36 are what main moving really changes: one merge, each
session's drift and status measured once for its new commits.

Test: `tests/test_busy_spawns_daemon.py` (that scenario: each repo question
at most once on the round that sees the move and nothing asked about the
sync's merge again; on the next round no repo question, no git at all in the
checkouts of the sessions whose workers are at work, and `git status` only
in the two worktrees whose files changed), and
`test_a_kept_answer_is_asked_again_when_a_file_git_reads_for_it_changes`
(a remote in a file the config includes goes away; an untracked
`.gitattributes` makes a tracked file read as modified: neither moves a
ref, the index or a tracked file, and both kept answers are asked again).

What is left: a worktree an agent is editing still costs one `git status`
per sweep that sees its files change (the app's feed). Other requests the
app makes (a session's diff, its PR) were not measured here; they go through
the same `git.py` functions.

### Other sources, measured and left as they are

- **gh**: an open session PR costs one `gh pr view` per 90 s
  (`PR_STATE_TTL`) and one merged-check per 60 s (`MERGE_POLL_SECONDS`), the same
  in v0.6.3 and main: under 2 a minute per open PR.
- **Fetches**: idle sync fetches `origin/<default>` once per repo per 5 minutes
  (`IDLE_FETCH_SECONDS`); other fetches happen on events (a merge, a resume, a
  new session), not on a clock.
- **The cull**: once a minute between all sidebars (S3): two `ps` for the
  process table, `tmux`, two git per running agent (see What is left) and git
  for workers it judges.
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

The daemon's share is S1. With this branch an idle session goes from 14 git
processes per 5-second sweep (168 a minute) to 2 per 5 minutes, plus one
fetch per repo every 5 minutes (table above). The live "after" can't be sampled
here: the installed app runs main, not this branch.

The sidebars' git (about 60 a minute that the sample caught, so more in
fact, from 6 `brindle watch` processes) came from each one's own cull and
safety refresh (S3). With S3 an idle sidebar's view starts no git, and the
6 sidebars run one sweep a minute between them instead of 6.

## What is left

- The cull's own git: each sweep, `login_pause._session` builds every running
  agent's full launch environment (`agents.agent_env`: `git config` for the
  identity, `git rev-parse` for the committed repo config) only to ask
  whether it runs on a cloud route: 2 git per running agent a minute, now
  once per machine rather than per sidebar (S3). Next: decide the route
  without building the whole environment.
- A sidebar's first look (opening it) still runs git for every worktree it
  shows (now with a `git config --list` each), and stats each worktree whole
  once, ignored folders included
  (`node_modules`, `.venv`): it doesn't know yet what is ignored. A worktree
  an agent is editing re-runs `git status` on each refresh that sees a
  change, as before, now with a stat of what git is about to look at before
  each.
- Orphans that already exist (from before this fix) are not reaped: a shell
  reparented to launchd names no agent, and its environment can't be trusted
  to say whose it is (Claude Code's daemon passes the environment of whichever
  agent first started it to every session it hosts). Restarting the machine,
  or ending them by hand, clears them once.
- Claude Code's own processes per CLI (its MCP servers, its shell snapshot, a
  `caffeinate`) scale with the number of live agents; reported workers keep
  theirs for `stale_after`.
