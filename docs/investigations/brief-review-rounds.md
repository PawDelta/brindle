# Why analysis-doc briefs coincide with more review rounds

Investigation date: 2026-11-02. Read-only: `~/.brindle/brindle.db` (opened with
`mode=ro`), `~/.brindle/docs`, Claude and Codex transcripts under brindle's
config dir, this checkout, and the server composer in `pawdelta-web`.
"Pre" means before 2026-10-30 12:00 UTC (analysis docs merged in #319 and 0.5.6
tagged that day); "post" means after. Every claim is marked VERIFIED (read from
data or code) or INFERRED.

## Summary

- VERIFIED: briefed heavy tasks needed more review rounds on average than heavy
  tasks before 0.5.6 (3.09 vs 2.30 merged, n=11 vs 153) and than unbriefed
  heavy tasks in the same days (2.44, n=9). The gap is borderline against the
  baseline (permutation p=0.049) and not significant against contemporaries
  (p=0.27). Two branches (feat/spawn-gate 7 rounds, fix/app-autoupdate-on-launch
  6) carry it; without them the briefed mean is 2.33.
- VERIFIED: nobody uses the briefs. 0 of 73 Codex reviewer sessions that got a
  brief mention any briefed doc in their own reasoning or verdict, 0 of 187
  post-period verdicts cite a doc id, 0 reviewers or workers called
  `read_analysis_doc`, and the 10 Claude worker transcripts mention no briefed
  doc except their own when writing it.
- VERIFIED: the briefs are almost all irrelevant. The server cites any doc that
  shares one non-stopword with the task text, so a Google Docs export test doc
  was the whole brief for three heavy workers, and two pty-view docs reached
  reviewers of a caps-lock fix, a lock-file fix and a token-cache fix.
- VERIFIED: every review gets a brief (73 in three days), while only heavy
  workers do (12), and reviewers are told to "Flag as a blocker any change that
  contradicts these decisions or docs". Workers' prompts are 30–36% brief text,
  reviewers' 19%.
- INFERRED root cause: the extra rounds come from task mix, a general
  post-period rise that hits unbriefed tasks too, a strict Codex reviewer
  loop that finds a new real bug each round, and one mid-task spec change
  where the reviewer enforced the worker's original task text until a decision
  was recorded. The brief feature adds cost and a spec-enforcer framing without
  any observed benefit; it is not the mechanism behind the rounds.

## 1. Numbers

`review_rounds` counts review verdicts, approvals included
(`src/brindle/db.py:2386-2393`), so a branch approved first time has 1 round.
Tasks: `routing_decisions` rows with an outcome. "Briefed" means the worker has
an `analysis_brief` context receipt. First-review verdicts come from the
`history` table (the `agents`, `reviews` and reviewer receipts are deleted
with the workspace, so `history` is the durable record).

| group | n | avg rounds | 0 / 1 / 2+ rounds | first review approved | first review changes | no review found |
|---|---|---|---|---|---|---|
| post, briefed, heavy | 12 | 2.83 | 1 / 2 / 9 | 3 | 8 | 1 |
| post, unbriefed, heavy | 9 | 2.44 | 0 / 4 / 5 | 4 | 5 | 0 |
| pre, heavy | 183 | 2.22 | 5 / 62 / 116 | 47 | 77 | 59 |
| post, unbriefed, medium | 37 | 2.70 | 1 / 13 / 23 | 14 | 22 | 1 |
| pre, medium | 317 | 1.77 | 16 / 146 / 155 | 91 | 87 | 139 |
| post, unbriefed, light | 11 | 2.09 | 0 / 6 / 5 | 6 | 5 | 0 |
| pre, light | 81 | 1.41 | 5 / 50 / 26 | 35 | 19 | 27 |

VERIFIED. First-review approval: briefed 3/11 (27%), post unbriefed heavy 4/9
(44%), pre heavy 47/124 (38%).

Merged heavy tasks only: briefed rounds [1,1,2,2,2,2,3,4,4,6,7] (mean 3.09,
median 2); post unbriefed [1,1,1,1,2,3,3,3,7] (mean 2.44); pre mean 2.30
(n=153). One-sided permutation test: briefed vs pre p=0.049, briefed vs post
unbriefed p=0.27. VERIFIED.

The post period is worse for every weight, briefed or not (medium 1.77 to
2.70, light 1.41 to 2.09). The Codex reviewer's rejection share is the same in
both periods: 497 of 798 verdicts pre (62%), 96 of 154 post (62%). On briefed
branches it rejected 18 of 33 (55%), on unbriefed post branches 78 of 121
(64%). VERIFIED. So per review, briefed branches are not rejected more; they
accumulate more reviews per task.

The 12 briefed tasks: all heavy, 10 of them in the daemon-overload and Windows
pty-view plans of sessions 20 and 21, reviewed by reviewer-codex, 5 with
escalations. The 9 unbriefed heavy tasks were the analysis-docs feature
branches themselves on Oct 30 (one needed 7 rounds). VERIFIED.

## 2. Causes, tested

Evidence is from the worker transcripts (10 Claude, 2 Codex), the 73 Codex
reviewer rollouts that contain a brief, and the `history` verdicts.

**(a) Stale guidance workers follow: not observed.** VERIFIED. No worker
transcript mentions a briefed doc title or id in assistant text (the only
title hits are each worker naming its own doc while writing it), and no
worker called `read_analysis_doc`. The spawn-gate worker never mentions
`pty_view` although two pty-view docs were its brief.

**(b) Unrelated docs pulled in: the dominant property of every brief.**
VERIFIED. Worker briefs on Oct 31 03:17–03:21 (fix/stale-mcp-after-update,
feat/engine-pty-view, feat/app-pty-bridge) consisted of one doc:
`"Export test: Analysis docs launch check" ... A short test document created
to check whether Google Docs export from pawdelta.com works`. The spawn-gate
worker's brief (5,961 chars) was the two pty-view docs, the stale-mcp doc and
the overload harness; none shares a file with its declared files. Reviewer
briefs: "Windows pty view" appears in 58 of 73, "Engine pty view" in 53,
"Stale brindle mcp" in 44, the export test doc in 26, including reviews of
fix/capslock-chat-2, fix/lock-file-leak, fix/google-token-cache and
fix/app-autoupdate-on-launch.

**(c) Reviewers treating docs or old designs as requirements: docs no, old
task text yes, once.** VERIFIED that no verdict or reviewer reasoning cites a
doc. VERIFIED the live case: fix/app-autoupdate-on-launch, where the
supervisor replaced "install and relaunch automatically" with an offer
banner. Review 12442 (14:52): "this restores the dependency the task
explicitly requires removing ... No supplied accepted decision relaxes this
requirement." Decision 6a5a5476 ("desktop app update check and air-gap") was
recorded at 14:54:36, review 12448 (14:57) approved "against the amended
banner design and accepted engine-only air-gap decision", and review 12458
notes "production behavior is unchanged". Three of the six rounds were spent
on that one point. The same framing appears on feat/starts-wait-for-room
(12280): "no supplied accepted decision authorizes that exception." INFERRED:
the source is "The worker's original task" in the reviewer prompt
(`src/brindle/agents.py:3301-3302`) plus the decisions-first framing, which
the analysis brief sharpens with "Flag as a blocker any change that contradicts
these decisions or docs, unless the branch says why"
(`src/brindle/pro/analysis_brief.py:176-177`).

**(d) Context bloat: real in size, no measurable effect on rounds.**
VERIFIED sizes: worker prompts of 16.4K–19.8K chars carry a 5.9K brief
(spawn-gate 5,961 of 16,908; session-diff-cache 5,961 of 16,432; autoupdate
5,884 of 19,812); reviewer first prompts average 29.5K chars with a 5.5K brief.
INFERRED: the blockers on the long branches were real bugs found by the
reviewer's own probes (spawn-gate rounds found 5, then 3, 2 and 1 distinct
cancellation bugs, for example "The request ContextVar is lost in the
snapshot's nested ThreadPoolExecutor"), not misses caused by the brief.

**(e) Briefs contradicting recorded decisions: not observed.** VERIFIED. One
decision was recorded in the post period (6a5a5476), after the brief in
question.

**(f) "Still open" gaps treated as must-fix: not observed.** VERIFIED. No
verdict or reviewer reasoning contains "Still open" or any listed open
question, although every brief carries up to four per doc.

**(g) Other.**
- VERIFIED: the pipeline re-reviews after every fix (`src/brindle/pipeline.py:979-1000`) and reviewer-codex rejects 62% of reviews regardless of period; heavy daemon-wide tasks give it a new finding per round.
- VERIFIED: a worker reopened for a fix round without a resumable CLI session gets a fresh server brief with `paths=None` (`src/brindle/agents.py:1619-1629`, `4579-4582`), replacing its receipt instead of replaying it as decisions and chunks are.
- VERIFIED: the product's own metrics read this noise as signal. `results()` (`src/brindle/pro/analysis_brief.py:200-229`) compares briefed heavy tasks to the 9 contemporaries with no confidence interval, and `brief_learning` counts a task as "went well" only at one round (`src/brindle/pro/brief_learning.py:122`), which every briefed heavy task fails.
- INFERRED risk, not seen in production: a reviewer's brief citations are pinned (`src/brindle/agents.py:3397-3399`) and `stale_before_result` refuses its verdict if a cited doc gains a version (`src/brindle/pro/analysis_docs.py:594-612`); no production agent was held this way (the only "Not recorded yet" hits are in the Oct 30 feature-development sessions), but every doc in every brief is a tripwire.

## 3. Selection, scoring and what reviewers are told

Server (`pawdelta-web/backend/pro_api/analysis_docs.py`): `_score` at
1240-1254 gives +3 per task word in the title or topics, +2 in the summary,
+1 in any indexed term, +4 per task path the doc mentions; `_hits` at
1277-1319 keeps any doc with score > 0 (line 1311), ranks a superseded doc one
point lower (1313), takes the top `ANALYSIS_BRIEF_MAX_DOCS = 5`
(`config.py:237`); `_brief_block` at 1368-1389 prints summary, up to 6
findings and 4 open questions per doc and cuts at 6,000 chars
(`config.py:238`). Words are task tokens minus a stopword list (`_tokens`,
877-884), so "daemon", "app", "test" or "check" qualify a doc. There is no
minimum score, no requirement of a path match, and no repo scope: the scope is
the user (`src/brindle/pro/analysis_brief.py:105-109`). VERIFIED.

Client: workers get the brief only for large work
(`src/brindle/agents.py:429-436`, `src/brindle/pro/analysis_brief.py:42-47`,
`239-256`), fenced under the header at `analysis_brief.py:37-39`. Reviewers
get it for every review with the worker's task text plus declared and changed
paths (`src/brindle/agents.py:3338-3346`, `analysis_brief.py:149-177`), and
the sentence "Flag as a blocker any change that contradicts these decisions or
docs, unless the branch says why the decision or doc no longer holds". The same
reviewer prompt already carries the decisions block and "Do not raise a
blocker ... that contradicts an accepted decision" from `_review_context`
(`src/brindle/agents.py:3191-3240`, attached at 3337), so decisions are listed
twice. Supervisors get a brief as an inbox message when a goal has two or more
milestones (`src/brindle/autopilot.py:698-701`), and the doc-writing rule on
audit approval (`autopilot.py:704-715`, `1632-1636`). VERIFIED.

## 4. Fixes, ranked

1. **Gate the reviewer brief and drop its blocker sentence.** Send reviewers a
   doc block only when a cited doc shares a path with the branch, keep the
   decisions block once, and remove "Flag as a blocker ... docs". Measure:
   reviewer prompt chars and the share of reviews carrying a doc block (today
   73 of 73 Codex reviews); verdicts containing "no supplied accepted
   decision" or "explicitly requires" that are later approved with no code
   change.
2. **Relevance threshold on the server.** Require a path match or a minimum
   score of about 5, skip docs of kind `other`, and let the client send a repo
   key so another repo's docs never qualify. Measure: fraction of cited docs
   that mention one of the task's files (today 0 of 4 for spawn-gate and 0 of
   1 for the three Oct 31 workers), and briefs returned empty.
3. **Put amendments above the original task for reviewers.** State that
   supervisor messages supersede the original task text and that a deviation
   justified by one needs no recorded decision. Measure: rounds on tasks whose
   supervisor changed the spec mid-task, and the count of decisions recorded
   only to unblock a review (one so far).
4. **Replay, don't refetch, on fresh restarts.** Rebuild the brief from the
   receipt as decisions and chunks are. Measure: `POST /analysis-docs/brief`
   calls per task (today more than one per reopened worker).
5. **Make the product's metrics honest.** `results()` should report counts
   and an interval and refuse below a real minimum; `brief_learning` should
   compare within weight. Measure: the dashboard no longer shows a 3.09 vs
   2.44 verdict on 11 vs 9 tasks as a result.
6. **Run a real comparison.** Toggle `assist` per session for 30 days and
   compare rounds and first-review approval within weight using the query in
   section 1. Expected if briefs are inert: no difference; the current gap
   should close once fixes 1–2 remove the cost.

## Limits

n=12 briefed tasks, all in two sessions, so any effect under about one round
is not detectable. Reviewer receipts, agent rows and the `reviews` table are
deleted with the workspace, which is why Codex rollout files, not the database,
establish which reviewers got briefs. The server index is encrypted; doc
relevance was judged from the brief text delivered to agents.
