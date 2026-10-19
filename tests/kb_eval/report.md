# Retrieval eval

recall@5: the share of each question's expected (file, section) pairs found in the top 5 results.

| subset | questions | keyword baseline | FTS5 | FTS5 + one-hop |
| --- | --- | --- | --- | --- |
| single-fact | 38 | 0.84 | 0.89 | 0.87 |
| multi-hop | 10 | 0.60 | 0.65 | 0.60 |

- keyword baseline: private-file leaks 0 of 3; questions missing an expected source: q05, q06, q09, q16, q26, q27, q39, q43, q44, q45, q47, q48
- FTS5: private-file leaks 0 of 3; questions missing an expected source: q03, q09, q32, q33, q40, q42, q43, q44, q45, q46
- FTS5 + one-hop: private-file leaks 0 of 3; questions missing an expected source: q03, q09, q26, q32, q33, q40, q42, q43, q44, q45, q46

Regenerate with `uv run python scripts/kb_eval.py --all` (build the code graph first with `graphify update .`; without it the one-hop walk only follows shared `kb_links`).

## Notes (task 3.7)

- **FTS5** is `kb.search` over the corpus synced into a scratch database with brindle's own chunker (`kb.sync`), so its chunks differ from the baseline's per-heading chunks. A chunk answers for every heading on its path and in its text, so a chunk holding subsections counts for each of them; this favours FTS5 a little against the baseline.
- **One-hop** keeps the top 3 hits and fills the last 2 slots with chunks one hop out (`kb.hop`): chunks sharing a `kb_links` node with those hits, or linked to a node a graphify edge joins to theirs. The graph is the repo's own, built with `graphify update .`. Every multi-hop question had neighbours to add.
- **Verdict on one-hop:** it lowers single-fact recall (0.89 to 0.87) and multi-hop recall (0.65 to 0.60), so by the gating rule it does not ship. The two slots it takes from hits 4 and 5 are worth more than the neighbours it adds; graph edges join code nodes, so a neighbour is a document that names nearby code, not necessarily the document the question hops to.
- **Hybrid (vectors):** not run, and `sqlite-vec` and a model are not added. FTS5 already beats the baseline on both subsets; the 10 questions it still misses include multi-hop ones whose wording differs from the documents' (q40, q42 to q46), which is where embeddings help most. With only 10 multi-hop questions a gain of one question is 0.1 of recall, so a trial is worth running behind the `brindle[kb]` extra on a larger set, but this eval cannot show vectors are needed yet.
