# Why analysis-doc briefs coincide with more review rounds

Investigation date: 2026-11-02. Read-only: `~/.brindle/brindle.db` (opened with
`mode=ro`), `~/.brindle/docs`, Claude and Codex transcripts under brindle's
config dir, this checkout, and the server composer in `pawdelta-web`.
"Pre" means before 2026-10-30 12:00 UTC (analysis docs merged in #319 and 0.5.6
tagged that day); "post" means after. Every claim is marked VERIFIED (read from
data or code) or INFERRED.

## Summary

- VERIFIED: within the one worker profile that has briefed tasks
  (developer-heavy, heavy weight), briefed tasks average 3.33 review rounds
  when merged (n=9), against 2.19 for the same profile and weight before
  0.5.6 (n=100, permutation p=0.02) and 2.44 for the same profile and weight
  in the same days without a brief (n=9, p=0.22). Two branches
  (feat/spawn-gate 7 rounds, fix/app-autoupdate-on-launch 6) carry it; without
  them the briefed mean is 2.43. The post period is worse for unbriefed
  tasks of every weight too.
- VERIFIED by search, not by experiment: no reviewer verdict, reviewer
  reasoning or worker message cites a briefed doc, and no agent called
  `read_analysis_doc`. Because the brief's findings sit inline in the prompt,
  this rules out explicit use only; implicit influence and context cost
  cannot be excluded with this data, and section 2 compares brief content
  against what each high-round branch actually did.
- VERIFIED: the briefs are almost all irrelevant. The server cites any doc
  that shares one non-stopword with the task text, so a Google Docs export
  test doc was the whole brief for three heavy workers, and two pty-view docs
  reached reviewers of a caps-lock fix, a lock-file fix and a token-cache fix.
- VERIFIED: every review gets a brief (73 in three days), while only heavy
  workers do (12), and reviewers are told to "Flag as a blocker any change
  that contradicts these decisions or docs". Workers' prompts are 30–36%
  brief text, reviewers' about 19%.
- INFERRED root cause: the extra rounds are explained by task mix, a general
  post-period rise, a strict Codex reviewer loop that finds a new real bug
  each round, and one amendment that contradicted itself (the autoupdate
  branch). The brief content is not implicated in any blocker; the brief
  feature adds prompt cost and a spec-enforcer framing without observed
  benefit. Whether that cost itself adds rounds is unresolved.

## 1. Numbers

`review_rounds` counts review verdicts, approvals included
(`src/brindle/db.py:2386-2393`), so a branch approved first time has 1 round.
A task is a `routing_decisions` row with an outcome; "briefed" means the
worker's agent id has an `analysis_brief` row in `context_receipts`. The
first-review verdict comes from the `history` table, because the `agents`,
`reviews` and reviewer receipts are deleted with the workspace. Cohort query
(read-only; `CUT` is the epoch of 2026-10-30 12:00 UTC):

```sql
SELECT d.agent_id, d.weight, d.profile, d.outcome, d.review_rounds, d.ts, t.branch, d.repo_root,
       EXISTS (SELECT 1 FROM context_receipts r
               WHERE r.task_id = d.agent_id AND r.item_kind = 'analysis_brief') AS briefed
FROM routing_decisions d LEFT JOIN tasks t ON t.id = d.task_id
WHERE d.outcome IS NOT NULL AND d.agent_id IS NOT NULL;
-- period: d.ts >= CUT is "post"; group by (period, briefed, weight, profile)
-- first review: the earliest history row with kind = 'review', the same branch and
-- repo_root, ts between d.ts - 60 and d.ts + 3 days; "APPROVED" or "CHANGES
-- REQUESTED" in its first 260 characters
```

Weight only (all profiles):

| group | n | avg rounds | 0 / 1 / 2+ rounds | first review approved | first review changes | no review found |
|---|---|---|---|---|---|---|
| post, briefed, heavy | 12 | 2.83 | 1 / 2 / 9 | 3 | 8 | 1 |
| post, unbriefed, heavy | 9 | 2.44 | 0 / 4 / 5 | 4 | 5 | 0 |
| pre, heavy | 183 | 2.22 | 5 / 62 / 116 | 47 | 77 | 59 |
| post, unbriefed, medium | 37 | 2.70 | 1 / 13 / 23 | 14 | 22 | 1 |
| pre, medium | 317 | 1.77 | 16 / 146 / 155 | 91 | 87 | 139 |
| post, unbriefed, light | 11 | 2.09 | 0 / 6 / 5 | 6 | 5 | 0 |
| pre, light | 81 | 1.41 | 5 / 50 / 26 | 35 | 19 | 27 |

Weight and worker profile (strata that contain a briefed task, plus their
matches; VERIFIED):

| period, briefed, weight, profile | n | merged | merged rounds | merged avg | first approved | first changes | no review found |
|---|---|---|---|---|---|---|---|
| post, briefed, heavy, developer-heavy | 10 | 9 | 1,1,2,2,3,4,4,6,7 | 3.33 | 2 | 7 | 1 |
| post, unbriefed, heavy, developer-heavy | 9 | 9 | 1,1,1,1,2,3,3,3,7 | 2.44 | 4 | 5 | 0 |
| pre, heavy, developer-heavy | 124 | 100 | median 2 | 2.19 | 27 | 56 | 41 |
| pre, no weight, developer-heavy | 81 | 74 | median 2 | 2.47 | 16 | 17 | 48 |
| post, briefed, heavy, developer-codex-heavy | 2 | 2 | 2,2 | 2.00 | 1 | 1 | 0 |
| pre, heavy, developer-codex-heavy | 6 | 5 | 1,2,2,4,6 | 3.00 | 1 | 4 | 1 |
| pre, heavy, developer-sonnet (no post match) | 43 | 39 | median 2 | 2.18 | 13 | 14 | 16 |

Small or unmatched strata: the codex-heavy pair is 2 vs 6 tasks and says
nothing; pre heavy developer-sonnet, developer-fable (7) and developer (2)
have no post counterpart; 41 of the 124 pre developer-heavy tasks have no
surviving review row.

Permutation tests on merged developer-heavy rounds (one-sided, 20,000
shuffles): briefed vs pre heavy p=0.02 (difference 1.14 rounds); briefed vs
pre heavy-or-unweighted p=0.09; briefed vs post unbriefed p=0.22. Without
spawn-gate and autoupdate the briefed mean is 2.43. VERIFIED.

First-review approval, developer-heavy heavy: briefed 2 of 9 with a review
row, post unbriefed 4 of 9, pre 27 of 83.

The post period is worse for unbriefed work at every weight (medium 1.77 to
2.70, light 1.41 to 2.09; developer-sonnet medium 1.97 to 3.82). The Codex
reviewer's rejection share is the same in both periods: 497 of 798 verdicts
pre (62%), 96 of 154 post (62%); on briefed branches 18 of 33 (55%), on
unbriefed post branches 78 of 121 (64%). VERIFIED. Per review, briefed
branches are not rejected more often; they go through more reviews per task.

The 12 briefed tasks: all heavy, 10 in the daemon-overload and Windows
pty-view plans of sessions 20 and 21, reviewed by reviewer-codex, 5 with
escalations. The 9 unbriefed heavy contemporaries were the analysis-docs
feature branches themselves on Oct 30 (one needed 7 rounds). VERIFIED.

## 2. Causes, tested

Evidence: 10 Claude and 2 Codex worker transcripts, the 73 Codex reviewer
rollouts that contain a brief, and the `history` verdicts. What the searches
establish is explicit use; the brief's summaries, findings and open questions
are inline in the prompt, so an agent can follow them without naming them.

**(a) Stale guidance workers follow: no explicit trace; one possible echo,
unresolved.** VERIFIED: no worker transcript mentions a briefed doc title or
id in assistant text (the only title hits are each worker naming its own doc
while writing it), and no worker called `read_analysis_doc`. Brief content
against what each high-round worker did:

- feat/spawn-gate (7 rounds): brief = two pty-view docs, the stale-mcp doc and
  the export test doc. The blockers were a ContextVar lost in a thread pool,
  a direct `subprocess.run` in `hash_blob`, POST requests without deadlines,
  a commit rollback broken by a stopped deadline, unrouted auth and keychain
  probes, tmux mutations without a write boundary, and a weak test. None of
  these topics appears in the brief. The pty doc's finding "Client is spawned
  with Popen(start_new_session=True) and no preexec_fn" matches the worker's
  own design of `spawn.read` running in its own session, and the worker's text
  mentions `preexec_fn` once; whether that came from the brief, the task or
  the existing code cannot be told apart. INFERRED, unresolved.
- feat/starts-wait-for-room (4 rounds): brief = overload harness and the two
  pty docs; the spawn-gate doc was not in it. The rejected time-based
  `START_WAIT` bypass mirrors `ACTION_WAIT` in the spawn-gate code the task
  told the worker to build on, not a brief finding. VERIFIED.
- perf/session-diff-cache (4 rounds): blockers were a fingerprint that missed
  qualified base refs and prewarming that ignored renames; the brief's four
  docs say nothing about diffs or caches. VERIFIED.
- fix/app-autoupdate-on-launch (6 rounds): the brief was the two pty docs and
  the stale-mcp doc; see (c). VERIFIED.

**(b) Unrelated docs pulled in: the dominant property of every brief.**
VERIFIED. Worker briefs on Oct 31 03:17–03:21 (fix/stale-mcp-after-update,
feat/engine-pty-view, feat/app-pty-bridge) consisted of one doc:
`"Export test: Analysis docs launch check" ... A short test document created
to check whether Google Docs export from pawdelta.com works`. Reviewer briefs:
"Windows pty view" appears in 58 of 73, "Engine pty view" in 53, "Stale
brindle mcp" in 44, the export test doc in 26, including reviews of
fix/capslock-chat-2, fix/lock-file-leak and fix/google-token-cache.

**(c) Reviewers treating docs or old designs as requirements: docs no;
one round lost to a contradictory amendment.** VERIFIED that no verdict or
reviewer reasoning cites a doc, and that reviewers are already told
"later spec updates supersede earlier conflicting instructions"
(`src/brindle/agents.py:3228-3232`). The autoupdate timeline, reconstructed
from the worker transcript, the six reviewer prompts and `history`:

1. Supervisor message 13308 replaced automatic relaunch with an offer banner
   and set two requirements side by side: "(1) Check the feed at launch right
   away, without waiting on the daemon and with a short time limit" and
   "(3) Keep ... the air-gap and staff gates ... as they are". The air-gap
   gate is answered by the daemon, so (1) and (3) conflict. All six reviewer
   prompts carried this message. VERIFIED.
2. Reviews 12419 (14:26), 12423 (14:35) and 12430 (14:45) enforced (1) and
   found real policy bypasses in each attempt to satisfy it: "a successful
   check caches []; the engine subsequently loads an air-gapped repo ... a
   later check ... now permits the feed", then a host-filesystem fallback
   that is wrong on Windows. Substantive findings. VERIFIED.
3. Review 12442 (14:52) at eee93faf: the fix was now fail-closed but "restores
   the dependency the task explicitly requires removing ... No supplied
   accepted decision relaxes this requirement." Decision 6a5a5476 was recorded
   at 14:54:36 accepting engine consent and a skipped look when the engine is
   silent, and review 12448 (14:57) approved the same commit eee93faf
   "against the amended banner design and accepted engine-only air-gap
   decision". One round was spent on a decision, not code. VERIFIED.

So the lost round came from a contradiction inside the supervisor's own
amendment, resolved by a recorded decision; neither the original task text
nor any doc caused it. Such decision-only rounds (a rejection and an approval
at the same commit) are rarer post than pre: 1 of 192 verdicts vs 22 of
1,224. VERIFIED. INFERRED: the analysis brief's "Flag as a blocker any change
that contradicts these decisions or docs" (`src/brindle/pro/analysis_brief.py:176-177`)
reinforces the stance that a deviation needs a recorded decision, but this
data cannot show it changed any verdict.

**(d) Context bloat: real in size, effect on rounds unresolved.** VERIFIED
sizes: worker prompts of 16.4K–19.8K chars carry a 5.9K brief (spawn-gate
5,961 of 16,908; session-diff-cache 5,961 of 16,432; autoupdate 5,884 of
19,812); the 73 reviewer first prompts average 29.5K chars with a 5.5K brief,
and the six autoupdate reviewer prompts were 37K–41K chars. INFERRED: the
blockers on the long branches were real bugs the reviewer found by probing
(spawn-gate: 5, then 3, 2 and 1 distinct cancellation bugs), so the brief did
not mislead the reviewer; whether 6K chars of unrelated docs made workers
likelier to miss those bugs cannot be decided from transcripts.

**(e) Briefs contradicting recorded decisions: not observed.** VERIFIED. One
decision was recorded in the post period (6a5a5476), after the brief in
question, and no brief mentions a decided topic.

**(f) "Still open" gaps treated as must-fix: not observed.** VERIFIED. No
verdict or reviewer reasoning contains "Still open" or any listed open
question, although every brief carries up to four per doc.

**(g) Other.**
- VERIFIED: the pipeline re-reviews after every fix (`src/brindle/pipeline.py:979-1000`) and reviewer-codex rejects 62% of reviews regardless of period; heavy daemon-wide tasks give it a new finding per round.
- VERIFIED: a worker reopened for a fix round without a resumable CLI session gets a fresh server brief with `paths=None` (`src/brindle/agents.py:1619-1629`, `4579-4582`), replacing its receipt instead of replaying it as decisions and chunks are.
- VERIFIED: the product's own metrics read this noise as signal. `results()` (`src/brindle/pro/analysis_brief.py:200-229`) compares briefed tasks to contemporaries by repo, kind and weight with no profile control and no interval. `brief_learning` is a task-brief heuristic, not an analysis-doc metric: a task "went well" when merged within one round (`src/brindle/pro/brief_learning.py:122`, `review_rounds <= 1`), which 2 of the 11 merged briefed tasks meet and 9 fail.
- INFERRED risk, not seen in production: a reviewer's brief citations are pinned (`src/brindle/agents.py:3397-3399`) and `stale_before_result` refuses its verdict if a cited doc gains a version (`src/brindle/pro/analysis_docs.py:594-612`); the only "Not recorded yet" hits are in the Oct 30 feature-development sessions, but every doc in every brief is a tripwire.

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
reviewer prompt already carries the decisions block, the supervisor messages
and "Do not raise a blocker ... that contradicts an accepted decision" from
`_review_context` (`src/brindle/agents.py:3191-3240`, attached at 3337), so
decisions are listed twice. Supervisors get a brief as an inbox message when a
goal has two or more milestones (`src/brindle/autopilot.py:698-701`), and the
doc-writing rule on audit approval (`autopilot.py:704-715`, `1632-1636`).
VERIFIED.

## 4. Fixes, ranked

1. **Gate the reviewer brief and drop its blocker sentence.** Send reviewers a
   doc block only when a cited doc shares a path with the branch, keep the
   decisions block once, and remove "Flag as a blocker ... docs". Measure:
   reviewer prompt chars and the share of reviews carrying a doc block (today
   73 of 73 Codex reviews); rounds per task by weight and profile with the
   query in section 1.
2. **Relevance threshold on the server.** Require a path match or a minimum
   score of about 5, skip docs of kind `other`, and let the client send a repo
   key so another repo's docs never qualify. Measure: fraction of cited docs
   that mention one of the task's files (today 0 of 4 for spawn-gate and 0 of
   1 for the three Oct 31 workers), and briefs returned empty.
3. **Route spec conflicts to the supervisor, not the worker.** Tell reviewers
   that a conflict between two supervisor requirements, or between a
   requirement and an accepted decision, is a `needs supervisor action` item
   so the pipeline escalates it instead of starting a fix round
   (`src/brindle/pipeline.py:978-985`). Measure: decision-only rounds, a
   rejection followed by an approval at the same commit (today 1 of 192 post
   verdicts, 22 of 1,224 pre), and verdicts containing "no supplied accepted
   decision".
4. **Replay, don't refetch, on fresh restarts.** Rebuild the brief from the
   receipt as decisions and chunks are. Measure: `POST /analysis-docs/brief`
   calls per task (today more than one per reopened worker).
5. **Make the product's metrics honest.** `results()` should control for
   profile, report counts with an interval and refuse below a real minimum.
   Measure: the dashboard no longer shows a 3.09 vs 2.44 verdict on 11 vs 9
   tasks as a result.
6. **Run a real comparison.** Toggle `assist` per session for 30 days and
   compare rounds and first-review approval within weight and profile using
   the query in section 1. This is the only way to settle the implicit
   influence and context-cost questions left open in section 2.

## Limits

n=12 briefed tasks (10 developer-heavy), all in two sessions, so any effect
under about one round is not detectable and the profile-matched comparison
with contemporaries rests on 9 vs 9 tasks. Searching for citations shows
explicit use only; implicit use of inline brief text is unresolved. Reviewer
receipts, agent rows and the `reviews` table are deleted with the workspace,
which is why Codex rollout files, not the database, establish which
reviewers got briefs. The server index is encrypted; doc relevance was judged
from the brief text delivered to agents.
