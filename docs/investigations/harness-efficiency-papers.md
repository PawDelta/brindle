# Harness efficiency: published research on cheaper, faster agents

Scope: 2023–2026 papers, reports and engineering posts on lowering LLM agent cost or latency at equal quality. Web search only; no code changed.

**Labels.** *Measured* = brindle's own benchmark against one Fable 5.2 session (reviewer 34%, supervisor 26%, redo 25% of cost; 60–190k start-up cache-write tokens per agent; no parallel work on large tasks). *Reported* = a paper or post figure, not replicated here. *Estimate* = my reasoning. Savings are shares of brindle's total cost unless stated.

## 1. Model cascades and routing

- FrugalGPT learns a cascade that tries cheap models first and escalates on low confidence. Reported: up to 98% cost reduction at GPT-4-level accuracy (best case, no independent replication). [arXiv:2305.05176](https://arxiv.org/abs/2305.05176)
- RouteLLM trains routers on preference data. Reported: a matrix-factorization router reaches 95% of GPT-4 quality with 26% of GPT-4 calls, about 48% cheaper than random routing. [arXiv:2406.18665](https://arxiv.org/abs/2406.18665)

*Coding agents:* partly. Routers were trained on chat preferences, not agent trajectories.

*Brindle mapping:* reviewer (34%) and supervisor (26%) could run on a cheaper model, escalating flagged work.

**Estimated saving: 10–20%.** If a cheaper first-pass reviewer covers half the reviews at about a third of the price, that saves 6–10 points; supervisor routing adds 3–6. Quality risk limits this (section 6).

## 2. Multi-agent vs single-agent cost

- Under matched thinking-token budgets, single agents beat multi-agent setups on multi-hop reasoning. Reported, April 2026. [arXiv:2604.02460](https://arxiv.org/abs/2604.02460)
- Anthropic's research system (lead agent plus subagents) reported a 90.2% gain over a single agent, at roughly 15× chat tokens for the full system. The post body did not load; the token figure is from secondary summaries. The post says coding is less parallelizable. [Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system)

*Coding agents:* yes, and the warning is stronger: shared context and many inter-agent dependencies are where multi-agent designs fail.

*Brindle mapping:* supervisor cost and per-agent start-up (each new agent pays its own cache write).

**Estimated saving: 5–15%.** Fewer, larger agents on shared-context tasks would remove repeated start-up cost. brindle has no measured split of start-up cost beyond the 60–190k range.

## 3. Planning and plan-then-parallel

- LLMCompiler plans a function-call DAG and runs independent calls in parallel. Reported vs ReAct: up to 3.7× faster and 6.7× cheaper on embarrassingly parallel tasks; about 9% more accurate on ParallelQA. [arXiv:2312.04511](https://arxiv.org/abs/2312.04511)

*Coding agents:* partly. Gains need independent sub-tasks; tasks touching the same files do not parallelize, which matches brindle's overlap guard.

*Brindle mapping:* the blocked parallel work on large tasks is a latency loss, and it adds supervisor turns.

**Estimated saving: latency 25–45% on large tasks; cost 0–5%.** Parallel plans mostly save wall time. File-disjoint splits, checked by the overlap guard, could run concurrently. Cost gains come only from fewer supervisor turns.

## 4. Context compression and memory

- "The Complexity Trap" compares observation masking with LLM summarization in SWE-agent on SWE-bench Verified. Reported: masking roughly halves cost versus a raw agent and matches or slightly beats summarization on solve rate; in OpenHands, 52% cheaper with +2.6 points. Environment output is about 84% of an average turn. [arXiv:2508.21433](https://arxiv.org/abs/2508.21433)
- CoACT compresses observations while checking the next action is unchanged. Reported: 33% average token cut on SWE-bench Verified. [arXiv:2607.02911](https://arxiv.org/abs/2607.02911)
- Faraday m1/d1 (vendor report): reported token reduction 87.8% (m1) and 89.6% (d1), with accuracy 38%→94% on m1. Internal benchmark, inference cost only, no per-ablation results, no latency. [Faraday](https://faradaycompute.com/research/faraday-cage-m1-d1)

*Coding agents:* yes, masking was tested on SWE-bench. Faraday's figures are too large to take at face value.

*Brindle mapping:* the supervisor (26%) is the main target, since its context grows with every worker report.

**Estimated saving: 8–15%.** Masking old supervisor history could cut supervisor cost 25–40%, about 6–10 points, with a few more from trimming start-up context.

## 5. Prompt caching economics

- Anthropic's documented multipliers: 5-minute cache writes 1.25× base input, 1-hour writes 2×, reads 0.1×. A 5-minute write breaks even after one read. [Anthropic docs](https://docs.claude.com/en/docs/build-with-claude/prompt-caching)
- Newer-model read rates are lower in secondary summaries; check the [pricing page](https://platform.claude.com/docs/about-claude/pricing) before using them.

*Coding agents:* yes; agents re-send the same repo context and tool definitions each turn.

*Brindle mapping:* the 60–190k start-up writes per agent are the direct target. A shared prefix (system prompt, tools, repo brief) reused within five minutes is read at 0.1× instead of re-written.

**Estimated saving: 5–12%.** Depends on how many workers start within minutes of each other, which is not measured.

## 6. LLM-as-reviewer and verification cost

- Ensembling cheap judges: GPT-5.4 mini with 8 samples reached 79.2% on RewardBench 2 at 1.2× baseline cost; nano reached 71.4% at 0.4×. Reported, April 2026. [arXiv:2604.13717](https://arxiv.org/abs/2604.13717)
- LangChain's legal verifiers: Haiku's false-pass rate was 48.4% per criterion (34.7% batched), too permissive to verify. [LangChain](https://www.langchain.com/blog/designing-efficient-verifiers-for-legal-agents)

*Coding agents:* yes, with a limit: a cheap reviewer that passes bad code is worse than none.

*Brindle mapping:* the reviewer is 34%, the largest measured line.

**Estimated saving: 10–20%.** A cheaper or batched first pass on routine diffs, escalating risky ones, could cut the reviewer line 30–60%. Quality risk is high, so this needs an escalation-rate and missed-bug check before it is relied on.

## 7. Speculative and parallel tool execution

- PASTE learns recurring tool-call sequences and runs likely next calls early. Reported (v1): 48.5% lower task completion time, 1.8× tool throughput. [arXiv:2603.18897v1](https://arxiv.org/abs/2603.18897v1)
- Speculative actions (ICLR 2026): a fast model predicts the next action. Reported: up to 55% next-action accuracy, up to 20% latency reduction. [OpenReview](https://openreview.net/pdf?id=P0GOk5wslg)

*Coding agents:* partly; test and read commands follow patterns, edits are harder to guess.

*Brindle mapping:* latency. Wrong guesses spend tokens, so this does not help the cost lines.

**Estimated saving: cost 0% (possibly negative); latency 10–20%.**

## 8. Early stopping and budget-aware agents

- BATS gives agents a running budget signal to decide whether to dig deeper or pivot. Reported on search agents: matched ReAct accuracy with 40.4% fewer search calls and 31.3% lower total cost. [arXiv:2511.17006](https://arxiv.org/abs/2511.17006)

*Coding agents:* yes in principle; no coding-agent result found.

*Brindle mapping:* redo (25%) is the cost of wrong turns; a budget signal to workers and supervisor could stop low-yield loops earlier.

**Estimated saving: 5–10%.** Weak, since the redo share is not split into avoidable and necessary work.

## 9. SWE-bench-style cost studies

- Agentless, a fixed pipeline with no agent loop (v1 results): $0.34 per issue on SWE-bench Lite, 27.33% resolved, the lowest cost among the systems compared at the time. [arXiv:2407.01489v1](https://arxiv.org/abs/2407.01489v1)
- HyperAgent's third-party table: SWE-agent with GPT-4o averaged $2.55 per instance (23.2% resolved); Agentless with GPT-4o $0.34 (33.2%). Versions and benchmarks differ. [arXiv:2409.16299](https://arxiv.org/abs/2409.16299)

*Coding agents:* yes. Fixed pipelines can be far cheaper than open loops, but fit well-scoped bugs, not large multi-file tasks.

*Brindle mapping:* large tasks are open-ended; routing well-scoped tasks to a fixed pipeline would help only that subset.

**Estimated saving: 5–15% on small tasks only.** The share of well-scoped work is unmeasured.

## Summary

| Topic | Brindle cost line | Estimated saving | Confidence |
|---|---|---|---|
| 1 Routing | reviewer, supervisor | 10–20% | low–medium |
| 2 Multi-agent design | supervisor, start-up | 5–15% | low |
| 3 Plan-then-parallel | large-task latency | 0–5% cost; 25–45% latency | medium |
| 4 Context masking | supervisor | 8–15% | medium |
| 5 Prompt caching | start-up writes | 5–12% | medium |
| 6 Cheap verification | reviewer | 10–20% | low (quality risk) |
| 7 Speculation | latency | 0% cost | medium |
| 8 Budget stopping | redo | 5–10% | low |
| 9 Fixed pipelines | small tasks | 5–15% (subset) | low |

**Combined: roughly 20–35% total cost reduction at equal quality.** The levers overlap (a cheaper reviewer and masked supervisor history reduce some of the same tokens), so the ranges do not add. The largest and least certain part is cheaper review. brindle should measure escalation rate, missed-bug rate, supervisor token growth and start-up cache hit rate before relying on any of these.

## Sources

1. FrugalGPT — https://arxiv.org/abs/2305.05176
2. RouteLLM — https://arxiv.org/abs/2406.18665
3. Tran & Kiela, single vs multi-agent — https://arxiv.org/abs/2604.02460
4. Anthropic multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
5. LLMCompiler — https://arxiv.org/abs/2312.04511
6. The Complexity Trap — https://arxiv.org/abs/2508.21433
7. CoACT — https://arxiv.org/abs/2607.02911
8. Faraday m1/d1 — https://faradaycompute.com/research/faraday-cage-m1-d1
9. Anthropic prompt caching docs — https://docs.claude.com/en/docs/build-with-claude/prompt-caching
10. Anthropic pricing — https://platform.claude.com/docs/about-claude/pricing
11. BATS — https://arxiv.org/abs/2511.17006
12. Lail & Markham, judge ensembling — https://arxiv.org/abs/2604.13717
13. LangChain legal verifiers — https://www.langchain.com/blog/designing-efficient-verifiers-for-legal-agents
14. PASTE (v1) — https://arxiv.org/abs/2603.18897v1
15. Speculative actions (ICLR 2026) — https://openreview.net/pdf?id=P0GOk5wslg
16. Agentless (v1) — https://arxiv.org/abs/2407.01489v1
17. HyperAgent — https://arxiv.org/abs/2409.16299

## Gaps

- No head-to-head coding-agent study of routing or cascades was found.
- Anthropic's post body did not load; its token figures are secondary.
- Several 2026 papers and the speculation numbers come from abstracts or summaries; full papers not read.
