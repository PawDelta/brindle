# Retrieval eval: keyword baseline over heading chunks

recall@5: the share of each question's expected (file, section) pairs found in the top 5 results.

| subset | questions | recall@5 |
| --- | --- | --- |
| single-fact | 38 | 0.84 |
| multi-hop | 10 | 0.60 |

Private-file leaks: 0 of 3 private questions.

Questions missing an expected source: q05, q06, q09, q16, q26, q27, q39, q43, q44, q45, q47, q48

Regenerate with `uv run python scripts/kb_eval.py`.
