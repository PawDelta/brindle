# How a session gets its work onto main: options

2026-11-11. Design options only; no code changed. Written from the code on
`brindle/session-10` (which has phase landing, the next-PR change and auto-merge
merged in) and `docs/investigations/merge-conflicts.txt`. I did not open PR #416
on GitHub or run anything; claims about behaviour come from reading the code
and are marked where they should be checked.

## The problem in one paragraph

The merge-conflicts investigation's correction (section 7) says the cause was
not the branch model but how rarely sessions landed: two sessions merged 12 PRs
on 11-03 and 11-04, then one session PR in the following week, while
`brindle/session-8` held 251 commits. Any option below has to make landing
frequent. The owner's doubt is about the form of what was built: several PRs
from one branch, and a history table to remember the earlier ones.

## What the doubt is really about

Three separate things come with "several PRs from one branch". They cost very
different amounts, and the options differ in which they remove.

| | What it is | Where it lives | Size |
|---|---|---|---|
| a | The history table | `session_pr_history` (`db.py:657`), `keep_merged_pr` (`session_review.py:225`), one upsert in `_record_pr` (`routes_actions.py:1020`) | About 15 lines. Exists because `session_prs` is keyed (session, branch) and is a released table. |
| b | Telling "merged, nothing new" from "merged, next PR due" | `new_since_merge` and `_merge_adds` (`session_review.py:171-210`), used by `current_pr`, `reasons`, `goal_phase` and the Inbox | About 40 lines of merge-tree reasoning. Two fixes on the day it shipped (`18f92fba`, `6449badc`). |
| c | Living on after a squash | `session_pr_landings`, `session_squashes`, `_land_merged_prs`, `_verified_squashes`, `_replay_squashes` (`session_sync.py:337-490`) | The dear part. The branch keeps the commits main now holds as one squash, so each sync must find the squash, prove it carries the same changes, and merge it with `-s ours`. |

The person never sees (a). They can see (b) and (c) only when they go wrong:
a PR button that doesn't come back, or a session that conflicts with its own
landed work.

## Summary

| Option | Lands per | PR on GitHub | Removes a | Removes b | Removes c | Build |
|---|---|---|---|---|---|---|
| 1. Keep as built | phase | one per phase, same branch | no | no | no | none |
| 2. A branch per landing | phase | one per phase, own branch | yes | no | no | medium |
| 3. Merge commits, not squashes | phase | one per phase, same branch | no | mostly | only where the repo allows it | small |
| 4. Fresh session branch after each landing | phase, at a quiet point | one per branch | yes | yes | yes | large |
| 5. Land each worker's branch | worker task | one per task | yes | yes | yes | largest |
| 6. Push to main, no PR | phase | none | yes | yes | yes | medium, opt-in only |

Build sizes are estimates from reading the code, not measured.

---

## 1. Keep as built: one branch, a PR per phase

**How it works.** When every milestone in a goal phase passes,
`autopilot.phase_landing` (`autopilot.py:1776`) sends the supervisor one
message: "Open or push the session PR now" (once a day for a goal without
phases). The PR's head is `brindle/session-N`. Each daemon sweep,
`session_review.refresh_pr_states` reads its checks, and
`_auto_merge_session_pr` squash-merges it once it is green and conflict-free
(`gh pr merge --squash --match-head-commit`, `workspaces.py:915`;
`auto_merge_session_prs` is on by default, `config.py:144`). `poll_merged`
records the merge, session sync learns which commit it landed as and merges
main back in. When the branch has new commits, `current_pr` returns nothing,
the next PR is opened, and the merged one moves to `session_pr_history`.

**What the person sees.** Sidebar: a line under the session, "PR #416 · In CI,
14 of 18", and "· drifting" when the oldest unlanded commit is over 48 hours
old or more than 60 files are unlanded (`session_sync._record_drift`). Inbox:
a "PR opened" card they can dismiss; "PR needs attention" if CI fails, the PR
conflicts, or the automatic merge is refused. GitHub: one PR per phase, all
from the same branch name, one squash commit each on main.

**Cost to build.** None.

**Risks.**
- Everything in (b) and (c) above stays, and it is the newest code in the path.
- `merge_pull_request` always squashes. A repo that forbids squash merges gets
  "GitHub refused the automatic PR merge" every phase.
- If the repo deletes head branches on merge (brindle's own tip suggests
  turning that on, `workspaces.keeps_merged_branches`), GitHub deletes the
  remote session branch after every phase and the next push recreates it.
- See "Gaps" below: two of them apply here most directly.

**Live workers mid-phase.** Workers are not interrupted. The PR carries the
session branch as pushed, which includes any later-phase worker branches that
had already merged. After the PR merges, the sync takes the same lock a worker
merge takes (`pipeline.base_lock`); if a merge is under way it reports `busy`
and runs at the next merge. If the sync conflicts, worker merges into the
session are refused (`session_sync.merge_block`) until a worker resolves it.

---

## 2. A branch per landing

**How it works.** The session branch is never pushed. When a phase passes,
brindle pushes the exact commit the phase's checks passed on to a new remote
branch (for example `brindle/landing/session-10-2`; it cannot be
`brindle/session-10/...`, git refuses a branch nested under an existing branch
name) and opens the PR from that. `session_prs` then gets one row per PR, so
its frozen key fits and the history table goes. Only one landing is open at a
time: if a second phase passes meanwhile, the open landing branch is moved
forward to the new commit. The squash still lands on main, so the session
branch still needs (b) and (c).

**What the person sees.** Sidebar and Inbox as today. GitHub: each PR has its
own short-lived branch that can be deleted on merge, and the session branch
never appears on the remote.

**Cost to build.** Medium. Every query that finds the PR by the session's own
branch name has to find the session's newest landing instead:
`session_review.current_pr`, `open_pr`, `adopt_head_pr`, the claim in
`_auto_merge_session_pr`, `_sync_after_merge`; the `_SESSION_PR` queries in
`routes_actions.py` (`open_session_pr`, `push_session_pr`, `_record_pr`,
`_preparation_pending`); `service/inbox.py` `_opened_pr` and `_pr_items`;
`disk.py:587` and `delete_pushed_remote`. `workspaces.open_pull_request` takes
its head from `ws.branch` and needs a head ref. `session_pr_heads` already
means "where the PR really lives" and can hold the landing ref. Remove
`session_pr_history` and `keep_merged_pr`. Tests: `test_daemon_actions.py`,
`test_service_inbox.py`, `test_session_sync.py`, `test_daemon_events.py`,
`test_autopilot.py`. The app reads only the goal's `pr_url` and `pr_state`
(`Sidebar.tsx` `PrLine`), which don't change.

**Risks.**
- It removes the 15-line part of the doubt and keeps the dear part. More code
  changes than it deletes.
- A second branch-naming scheme, and stray remote landing branches if the
  daemon stops between the push and the record.
- Sessions that already have a row keyed by the session branch must keep
  working.

**Live workers mid-phase.** The best of the PR options, but only slightly: the
PR is pinned to the commit that passed, so later worker merges cannot change
what is in CI. Option 1 can get the same by pushing a named commit
(`git.push(..., sha=...)` exists already).

---

## 3. Same branch, land with merge commits instead of squashes

**How it works.** As option 1, but the PR merges with a merge commit. Main then
contains the session's own commits, so the session branch never holds anything
main lacks in another shape. The sync after a merge is a plain merge, and
"anything new since the merge?" is `git rev-list origin/main..HEAD`.

**What the person sees.** The app is unchanged. GitHub: main's commit list
shows a merge commit per PR with every worker commit and "Merge branch ... into
brindle/session-N" beneath it. The first-parent view stays one line per PR.

**Cost to build.** Small to switch on: a repo setting for how session PRs
merge (`config.py`), read by `workspaces.merge_pull_request`. But none of (c)
can be deleted. The person can still press Squash on GitHub, and other
people's repos may allow only squash merges, so brindle must go on handling a
squashed session PR. This makes the fragile code run less often here; it does
not make it smaller.

**Risks.**
- Main's history gets noisy, and anything that reads it (release notes,
  bisect) meets worker commits that never passed the checks on their own.
- Still several PRs from one branch, and still the history table.
- Repos that forbid merge commits can't use it.

**Live workers mid-phase.** As option 1, and a little better after the merge:
the sync doesn't have to wait to learn how the PR landed (the `pending` state
in `session_sync.sync`).

---

## 4. A fresh session branch after every landing

**How it works.** When a session PR merges, brindle cuts a new branch from
origin/main (which now holds the squash) and moves the session onto it. Each
branch has exactly one PR and lives for one phase. No history table, no
"anything new" question, and no squash replay, because the new branch starts
after the squash. `sessions_mod.handover_to` (behind `service/sessions.py:507`)
already moves a goal, its workers and queued tasks to another branch.

The catch: it is only clean when nothing exists beyond what the PR carried.
Any commit made after the push, and every live worker branch (cut from the old
branch), still carries the pre-squash commits. Bringing them onto the new
branch is the same conflict `-s ours` exists to avoid, or a rebase, which
`session_sync` rules out by design.

**What the person sees.** The session's branch name changes each phase in the
sidebar, the terminal and `git branch`. GitHub is the tidiest of all: one
branch, one PR.

**Cost to build.** Large. A live session's `Workspace.branch` changes, and so
does everything keyed by (repo, branch): `session_sync`, `session_drift`,
`session_squashes`, `session_pr_prep`, `session_pr_declined`, and
`pipeline.base_lock`. Every worker's `base_branch` has to be retargeted
(`pipeline.py` reads it in about 30 places). Session branch naming
(`service/sessions.py:155-163`) needs a second level. Tests across the suite.

**Risks.**
- Work stranded on the old branch is a data-loss class of bug.
- To stay clean it must wait for a quiet point. With workers started in
  parallel there rarely is one, so landings get held back, which is the
  original cause of the conflicts.

**Live workers mid-phase.** The worst of the set: wait for them to finish, or
retarget them and replay the squash anyway.

---

## 5. Land each worker's branch (no session PR)

**How it works.** Worker branches are cut from origin/main, not from the
session branch. Each approved branch becomes its own small PR to main and is
merged on green. The session checkout only follows main (session sync already
does that) and is where milestone checks run. Nothing stays unlanded longer
than one task. The pipeline already knows this shape: a worker whose base is
the default branch is held unless `auto_merge_default_branch` is on
(`pipeline.py:1332`).

**What the person sees.** Sidebar: no session PR line; at most a count of
branches in CI. Inbox: only failures. GitHub: one PR per worker task.
`brindle/session-23` took 93 worker merges in nine days, so about ten PRs a
day from one busy session.

**Cost to build.** The largest. `pipeline._merge` (`pipeline.py:1980`) opens a
PR instead of merging locally, and `depends_on` has to wait for CI and the
merge each time. The Review screen is built from the session branch's diff
against its base (`session_review.py` from line 735: `diff`, `draft`, blame per
worker) and would have nothing to show. `goal_phase`'s `ready_for_pr` and
`pr_open` states, the Inbox PR cards and the sidebar `PrLine` all assume one
session PR.

**Risks.**
- Half-built features reach main. In this repo every push to main also ships
  an app build (per the session notes; not re-checked here), so each one would
  need a ship gate.
- CI time and queueing per task; one red main blocks every session.
- The person loses the one moment per phase where they review the whole
  change.

**Live workers mid-phase.** There is no "mid-phase" problem: each worker lands
alone. A worker that needs another's unmerged work has to wait for it or
stack on it.

---

## 6. Push to main without a PR

**How it works.** When a phase passes, brindle runs the PR gate locally
(`gates.pr_checked`: merges origin/main in, then runs every configured check
on a clean checkout of that commit) and pushes the verified commit straight to
origin/main. Because the session branch already contains origin/main, main
simply moves forward to it. No PR, no squash, no replay, no `session_prs` row.

**What the person sees.** Sidebar: "Landed 14:02" with the phase name. Inbox:
only when the gate fails or the push is refused. GitHub: no PRs; main's commit
list shows all worker commits and merges.

**Cost to build.** Medium. A new job beside `open_session_pr` in
`routes_actions.py` (it can reuse `_checked`), a push of a named commit to the
default branch that re-syncs when main has moved, a repo setting, and a
"landed" state in `session_review.goal_phase` that isn't tied to a PR. All the
PR code stays, because most repos protect main.

**Risks.**
- It only works where main is unprotected or brindle pushes as an admin, so it
  can only ever be opt-in: two landing paths to maintain.
- GitHub CI never gates the change. Local checks pass on one machine.
- Nothing that hangs off PRs runs: required reviews, the CLA check. On a
  public repo, contributors see unreviewed pushes to main.

**Live workers mid-phase.** As option 1, with the shortest window of all,
since there is no CI wait: the verified commit lands and workers carry on.

---

## Gaps that apply whichever option is chosen

Read from the code, not run. Worth a check before deciding.

1. **brindle's own Open PR cannot land a phase.** `session_review.reasons`
   (`session_review.py:95`) lists every milestone that hasn't passed, any
   running worker and any queued task, and `open_session_pr` refuses while
   there are reasons. So during a goal with later phases, the only way a phase
   PR opens is the supervisor running `gh pr create` after the message, which
   brindle then adopts (`adopt_head_pr`). Adopted PRs refuse the Push button
   (`push_session_pr`, `routes_actions.py:981`). Landing by phase therefore
   depends on the supervisor acting on a message.
2. **A phase PR is not pinned to the commit the phase passed on.** It carries
   the session branch as pushed, which can include part of the next phase.
3. **The merge method is fixed to squash** (`workspaces.py:918`).

## Recommendation

Keep what was built (option 1), and close gaps 1 and 2: let brindle open the
phase PR itself, pinned to the commit the phase's checks passed on. Then watch
it for a week: count sync conflicts on session branches and any PR button or
landing that got stuck.

Why: the investigation says the cause was landing too rarely, and option 1
already fixes that. For the person, options 1, 2 and 3 look the same in the
sidebar and Inbox and differ on GitHub only by a branch name or the shape of
main's history. The history table is the cheapest part of the design. Option 2
spends a medium-sized change to remove it and keeps the expensive part. The
options that do remove the expensive part cost more than it does: option 4
needs quiet points that parallel workers don't give, option 5 gives up the
Review screen and puts half-built work on main, and option 6 only works where
main is unprotected. Option 3 is the cheap one to try next if the squash
handling keeps producing bugs, as a setting for this repo.

## The strongest argument against it

Option 1 bets on the squash handling, (b) and (c), being right, and that code
needed two fixes on the day it shipped. When it is wrong, the failure is the
worst kind for the person: a session that conflicts with its own landed work,
or a PR button that never returns, with nothing on screen that explains why.
It runs in every user's repo, on merge settings and histories this repo never
exercises. "Keep it and watch" also leaves both things the owner named exactly
as they are, and a week of quiet in one repo does not show the design is
sound. If that risk weighs more than the build cost, the honest alternative is
not option 2 (which keeps the same risk) but a design where a branch never
outlives its squash: option 4, accepting that landings wait for a quiet point,
or option 3 made the default wherever the repo allows merge commits.
