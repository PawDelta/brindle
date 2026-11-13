# Harness efficiency: what published research says about cheaper, faster agents

Scope: 2023–2026 papers, technical reports and engineering posts on cutting LLM agent cost or latency at equal quality. Web search only; no code changed.

**Labels.** *Measured* = a brindle number from the benchmark against one Fable 5.2 session (reviewer 34%, supervisor 26%, redoing work 25% of cost; 60–190k start-up cache-write tokens per agent; no parallel work on large tasks). *Reported* = a figure from a paper or post, not replicated here. *Estimate* = my reasoning. Savings are shares of brindle's total cost unless stated.

Several sources are secondary summaries; those are flagged and should be checked before quoting.

## 1. Model cascades and routing

**Findings.**
- FrugalGPT learns a cascade that tries cheap models first and escalates on low confidence. Reported: up to 98% cost reduction at GPT-4-level accuracy, or +4% accuracy at equal cost. The 98% is a best case on its benchmarks; I found no independent replication. [FrugalGPT, arXiv:2305.05176](https://arxiv.org/abs/2305.05176)
- RouteLLM trains a router on preference data. Reported: over 2× cost savings with little quality loss; a matrix-factorization router reaches 95% of GPT-4 quality using 26% of GPT-4 calls (about 48% cheaper than random routing). [RouteLLM, arXiv:2406.18665](https://arxiv.org/abs/2406.18665)

**Coding agents.** Partly. Routing works best when per-request difficulty varies. Agent steps differ in difficulty (exploration and edits are easy, final review is hard), but routers were trained on chat preference data, not agent trajectories.

**Brindle mapping.** Reviewer (34%) and supervisor (26%) are the biggest lines. Those roles could run on a cheaper model with escalation to the strong model only on flagged work.

**Estimated saving: 10–20%.** Reasoning: the reviewer line is 34%; if a cheaper first-pass reviewer handles half the reviews at roughly a third of the price, that saves about 6–10 points. Supervisor routing adds about 3–6 points. Quality risk is the main constraint: section 9 shows cheap verifiers can pass bad work.

## 2. Multi-agent vs single-agent cost

**Findings.**
- Under matched thinking-token budgets (100–10,000), single agents beat multi-agent setups on multi-hop reasoning, arguing information-theoretically that splitting reasoning across agents wastes tokens. Reported, from an April 2026 paper. [Tran & Kiela, arXiv:2604.02460](https://arxiv.org/abs/2604.02460)
- Anthropic's research system used a lead agent with subagents and reported a 90.2% gain over a single Opus 4 agent on internal research evals. Anthropic also reported agents use about 4× chat tokens and multi-agent systems about 15×. The post itself did not load for me; the 4×/15× figures come from secondary summaries. It notes coding is less parallelizable. [Anthropic engineering post](https://www.anthropic.com/engineering/multi-agent-research-system)

**Coding agents.** Yes, and the warning is stronger: shared context and many dependencies between agents are where multi-agent designs fail.

**Brindle mapping.** This is the cost of the supervisor (26%) and start-up overhead per agent. Every new agent pays its own cache write (60–190k tokens) and has to re-read the repo.

**Estimated saving: 5–15%.** Reasoning: fewer, larger agents on tasks that share context would remove repeated start-up cost. This is an estimate; brindle has no measured split of start-up cost per agent beyond the 60–190k figure.

## 3. Planning, decomposition, plan-then-parallel

**Findings.**
- LLMCompiler plans a function-call DAG, then runs independent calls in parallel. Reported vs ReAct: up to 3.7× faster and up to 6.7× cheaper on embarrassingly parallel tasks, and about 9% more accurate on ParallelQA. Gains come from avoiding repeated LLM calls between steps. [LLMCompiler, arXiv:2312.04511, ICML 2024](https://arxiv.org/abs/2312.04511)

**Coding agents.** Partly. The gains depend on independent sub-tasks. Tasks that touch the same files do not parallelize, which matches brindle's overlap guard.

**Brindle mapping.** "No parallel work on a large task" is a latency loss, and it also adds supervisor turns.

**Estimated saving: latency 25–45% on large tasks; cost 0–5%.** Reasoning: parallelism mostly saves wall time. A plan that splits a large task into file-disjoint parts (checked by the existing overlap guard) can run concurrently. Cost gains come only from fewer supervisor turns, so they are small.

## 4. Context compression and memory

**Findings.**
- "The Complexity Trap" compares observation masking (hiding old tool outputs) with LLM summarization in SWE-agent on SWE-bench Verified. Reported: masking roughly halves cost vs a raw agent and matches or slightly beats summarization on solve rate. In OpenHands, masking was 52% cheaper and gained 2.6 points. A hybrid (masking, with summarization as a last resort) cut cost another 7%. Environment observations are about 84% of an average turn. [arXiv:2508.21433](https://arxiv.org/abs/2508.21433)
- CoACT compresses observations while checking that the next action stays the same. Reported: 33% average cut in total tokens on SWE-bench Verified with little accuracy loss. [arXiv:2607.02911](https://arxiv.org/abs/2607.02911)
- Faraday's m1/d1 harnesses (vendor report) wrap an unchanged Opus model with selective retrieval, durable memory, prerequisite checks and outcome verification. Reported: m1 task accuracy 38%→94%, failed tasks per 100 62→6, token reduction 87.8%; d1 token reduction 89.6%. Caveats: an internal benchmark, binary pass/fail scoring, inference cost only (excluding engineering and tools), no per-ablation results, and no latency figures. [Faraday](https://faradaycompute.com/research/faraday-cage-m1-d1)

**Coding agents.** Yes. SWE-bench is the setting where masking was tested. The Faraday numbers are too large to take at face value.

**Brindle mapping.** Supervisor (26%) is the main target: its context grows with every worker report. Start-up cache writes (60–190k) also shrink if the brief and repo context are trimmed.

**Estimated saving: 8–15%.** Reasoning: a masking-style rule for supervisor history could cut supervisor cost by 25–40%, which is about 6–10 points. Trimming start-up context might save a few more. I would not plan on Faraday's 88–90% figures; they are not comparable to brindle's share of cost.

## 5. Prompt caching economics

**Findings.**
- Anthropic's documented multipliers: 5-minute cache writes cost 1.25× base input, 1-hour writes 2×, and reads 0.1×. The 5-minute write breaks even after one read; the 1-hour write after two. [Anthropic prompt caching docs](https://docs.claude.com/en/docs/build-with-claude/prompt-caching)
- Batch discounts stack with caching; some newer-model read rates are lower (0.05× or 0.025× in secondary summaries). Check the [pricing page](https://platform.claude.com/docs/about-claude/pricing) before using those rates.

**Coding agents.** Yes. Agents re-send the same repo context and tool definitions on every turn.

**Brindle mapping.** Start-up cache writes (60–190k per agent) are the direct target. Each new worker writes a new prefix; a shared prefix (system prompt, tool list, repo brief) reused within the 5-minute window costs 0.1× on reads.

**Estimated saving: 5–12%.** Reasoning: if start-up writes are a meaningful share of the 60–190k tokens and workers start within minutes of each other, reusing the prefix turns 1.25× writes into 0.1× reads. Brindle's share of cost for start-up is not measured separately, so this range is an estimate.

## 6. LLM-as-reviewer and verification cost

**Findings.**
- Cheaper judges with ensembling: GPT-5.4 mini with 8 samples reached 79.2% judge accuracy on RewardBench 2 at 1.2× baseline cost; nano with 8 samples reached 71.4% at 0.4×. Small models benefit most from ensembling. Reported, from an April 2026 paper. [Lail & Markham, arXiv:2604.13717](https://arxiv.org/abs/2604.13717)
- LangChain's legal-agent verifiers: Haiku's false-pass rate was 48.4% per criterion (34.7% batched), too permissive for verification. Batching checks cut cost further. [LangChain](https://www.langchain.com/blog/designing-efficient-verifiers-for-legal-agents)

**Coding agents.** Yes, with a clear limit: a cheap reviewer that passes bad code is worse than none.

**Brindle mapping.** The reviewer is 34% of cost, the largest measured line.

**Estimated saving: 10–20%.** Reasoning: the reviewer line could drop by 30–60% if a cheaper or batched first pass handles routine diffs and escalates risky ones. Quality risk is high, so the saving should be paid for by a check on escalation rate and missed bugs, not assumed.

## 7. Speculative and parallel tool execution

**Findings.**
- PASTE (pattern-aware speculative tool execution) learns recurring tool-call sequences and runs likely next calls early. Reported: 48.5% lower task completion time and 1.8× tool throughput. Secondary summary of [arXiv:2603.18897](https://arxiv.org/abs/2603.18897); I did not read the paper.
- Speculative actions (ICLR 2026): a fast model predicts the next action and observation. Reported: up to 55% next-action accuracy and up to 20% latency reduction. Speculation spends tokens on wrong guesses, so its cost is not free.

**Coding agents.** Partly. Test and read commands follow patterns; edits are harder to guess.

**Brindle mapping.** Latency, not cost. Speculation adds tokens, so it does not help the cost lines directly.

**Estimated saving: cost 0% (possibly negative); latency 10–20%.** Reasoning: speculative calls are wasted when wrong, and the cost lines are dominated by reviewer and supervisor tokens, not tool calls.

## 8. Early stopping and budget-aware agents

**Findings.**
- BATS (budget-aware test-time scaling) gives agents a running budget signal, so they decide whether to dig deeper or pivot. Reported on search agents: a budget tracker matched ReAct accuracy with 40.4% fewer search calls and 31.3% lower total cost. Secondary summary; check [arXiv:2511.17006](https://arxiv.org/abs/2511.17006). [Liu et al., 2025](https://arxiv.org/abs/2511.17006)

**Coding agents.** Yes in principle; no coding-agent result was found.

**Brindle mapping.** Redoing work (25%) is the cost of wrong turns. A budget signal to workers and the supervisor could stop low-yield redo loops earlier.

**Estimated saving: 5–10%.** Reasoning: if a fifth of the redo share (25%) is avoidable by stopping early, that is about 5 points. The estimate is weak because the redo share is not split into avoidable and necessary work.

## 9. SWE-bench-style cost studies

**Findings.**
- Agentless (no agent loop: localize, repair, validate) reported $0.34 per issue on SWE-bench Lite with 27.33% resolved, the lowest cost among the systems compared at the time. [Agentless, arXiv:2407.01489](https://arxiv.org/abs/2407.01489)
- HyperAgent's cost table (third-party comparison): SWE-agent with GPT-4o averaged $2.55 per instance (23.2% resolved), Agentless with GPT-4o $0.34 (33.2% resolved). Different versions and benchmarks make these hard to compare. [HyperAgent, arXiv:2409.16299](https://arxiv.org/abs/2409.16299)

**Coding agents.** These are the coding-agent results. The lesson is that fixed-pipeline designs can be much cheaper than open-ended loops, but they fit well-scoped bugs, not large multi-file tasks.

**Brindle mapping.** Brindle's large tasks are open-ended. Routing well-scoped tasks to a fixed pipeline would be cheaper but would not cover them.

**Estimated saving: 5–15% on small tasks only.** Reasoning: this applies only to the share of work that is well-scoped; I have no measure of that share.

## Summary: estimated saving for brindle

| Topic | Brindle cost line | Estimated saving (share of total) | Confidence |
|---|---|---|---|
| Cascades/routing (1) | reviewer, supervisor | 10–20% | low–medium |
| Multi-agent design (2) | supervisor, start-up | 5–15% | low |
| Plan-then-parallel (3) | latency on large tasks | 0–5% cost; 25–45% latency | medium |
| Context masking (4) | supervisor | 8–15% | medium |
| Prompt caching (5) | start-up writes | 5–12% | medium |
| Cheap/batched verification (6) | reviewer | 10–20% | low (quality risk) |
| Speculation (7) | latency | 0% cost | medium |
| Budget-aware stopping (8) | redo | 5–10% | low |
| Fixed pipelines (9) | small tasks | 5–15% (subset) | low |

**Combined.** The lines overlap (a cheaper reviewer and masked supervisor history both reduce the same tokens), so I would not add them. My estimate is **roughly 20–35% total cost reduction** at the same quality, with the largest and least certain part coming from cheaper reviews. The savings are estimates; brindle's own measurement should confirm each one before it is relied on.

**What to measure first.** Escalation rate and missed-bug rate for any cheaper reviewer; supervisor token growth with and without masking; start-up cache hit rate when workers start within five minutes of each other.

## Sources

1. FrugalGPT — https://arxiv.org/abs/2305.05176
2. RouteLLM — https://arxiv.org/abs/2406.18665
3. Single-agent vs multi-agent token budgets (Tran & Kiela) — https://arxiv.org/abs/2604.02460
4. Multi-agent cost framing — https://arxiv.org/pdf/2505.18286
5. LLMCompiler — https://arxiv.org/abs/2312.04511
6. The Complexity Trap (JetBrains/TUM) — https://arxiv.org/abs/2508.21433
7. CoACT — https://arxiv.org/abs/2607.02911
8. Faraday m1/d1 — https://faradaycompute.com/research/faraday-cage-m1-d1
9. Anthropic prompt caching docs — https://docs.claude.com/en/docs/build-with-claude/prompt-caching
10. Anthropic pricing — https://platform.claude.com/docs/about-claude/pricing
11. Budget-aware tool use (BATS) — https://arxiv.org/abs/2511.17006
12. Anthropic multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
13. Lail & Markham, judge ensembling — https://arxiv.org/abs/2604.13717
14. LangChain legal verifiers — https://www.langchain.com/blog/designing-efficient-verifiers-for-legal-agents
15. Agentless — https://arxiv.org/abs/2407.01489
16. HyperAgent — https://arxiv.org/abs/2409.16299
17. PASTE speculative tool execution — https://arxiv.org/abs/2603.18897

## Gaps

- No head-to-head coding-agent study of cascades or routing was found.
- Anthropic's post text did not load; its 4×/15× token figures are secondary.
- Several 2026 IDs and the speculation numbers come from abstracts or summaries; I did not open the full papers.
