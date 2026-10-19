#!/usr/bin/env python3
"""Score a search function on the retrieval eval in tests/kb_eval/.

Each question names the (file, section) pairs that answer it. A question
scores the share of those pairs found in the search's top K results (recall@K),
and the report gives the mean per subset: ``single-fact`` and ``multi-hop``.
Questions tagged ``private`` must NOT surface the private file they name; they
are counted as leaks, not as recall.

The default search is a plain keyword baseline over heading chunks, so the
eval runs with no index and no new dependency. To score another search, pass
``--search module:factory``: ``factory(chunks)`` returns a callable
``search(question, k) -> [(file, section), ...]`` (best first). A search must
leave out chunks with ``chunk.private`` set.

    uv run python scripts/kb_eval.py [--search module:factory] [--report PATH]
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "tests" / "kb_eval"
K = 5
SOURCES = (
    "README.md",
    "deploy/**/*.md",
    "src/brindle/builtin_rules/**/*.md",
    "src/brindle/builtin_agents/*.md",
    "tests/kb_eval/corpus/**/*.md",
)
SUBSETS = ("single-fact", "multi-hop")
TOP = "(top)"
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i if in is it of on or that the "
    "this to what when where which who why with you your".split())
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


class Chunk(NamedTuple):
    file: str
    section: str
    text: str
    private: bool = False


def chunk_markdown(file: str, text: str) -> list[Chunk]:
    """Split a markdown file at its headings (ignoring ``#`` lines in code
    fences). Text before the first heading, front matter included, is the
    ``(top)`` section."""
    private = bool(re.match(r"---\n(?:.*\n)*?visibility:\s*private\s*\n", text))
    sections: list[tuple[str, list[str]]] = [(TOP, [])]
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        heading = None if fenced else re.match(r"#{1,6}\s+(.*\S)\s*$", line)
        if heading:
            sections.append((heading.group(1), [line]))
        else:
            sections[-1][1].append(line)
    return [Chunk(file, name, "\n".join(lines), private)
            for name, lines in sections if "".join(lines).strip()]


def load_chunks(root: Path, sources=SOURCES) -> list[Chunk]:
    chunks: list[Chunk] = []
    for pattern in sources:
        for path in sorted(root.glob(pattern)):
            chunks += chunk_markdown(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
    return chunks


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+", text.translate(_ZERO_WIDTH).lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def keyword_search(chunks: list[Chunk]):
    """The baseline: tf-idf over each chunk's words plus its file name."""
    docs = [(c, Counter(tokens(f"{c.section} {c.section} {c.file} {c.text}"))) for c in chunks if not c.private]
    df = Counter(w for _, tf in docs for w in tf)

    def search(question: str, k: int = K) -> list[tuple[str, str]]:
        terms = set(tokens(question))
        scored = []
        for chunk, tf in docs:
            score = sum((1 + math.log(tf[w])) * math.log(1 + len(docs) / df[w]) for w in terms if w in tf)
            if score:
                scored.append((-score, chunk.file, chunk.section))
        return [(f, s) for _, f, s in sorted(scored)[:k]]

    return search


def _found(want: dict, got: list[tuple[str, str]]) -> bool:
    return any(f == want["file"] and (not want.get("section") or s.casefold() == want["section"].casefold())
               for f, s in got)


def run(questions: list[dict], search, k: int = K) -> dict:
    """Recall@k per subset, the questions missed, and the private-file leaks."""
    recalls: dict[str, list[float]] = {name: [] for name in SUBSETS}
    missed: list[str] = []
    leaks: list[str] = []
    private = 0
    for q in questions:
        got = search(q["question"], k)[:k]
        if q["tag"] == "private":
            private += 1
            if any(f == q["private_file"] for f, _ in got):
                leaks.append(q["id"])
            continue
        hits = sum(_found(want, got) for want in q["expected"])
        recalls[q["tag"]].append(hits / len(q["expected"]))
        if hits < len(q["expected"]):
            missed.append(q["id"])
    return {"k": k,
            "recall": {name: (sum(r) / len(r) if r else None, len(r)) for name, r in recalls.items()},
            "missed": missed, "private": private, "leaks": leaks}


def report_markdown(result: dict, search_name: str) -> str:
    k = result["k"]
    rows = [f"| {name} | {n} | {'n/a' if r is None else f'{r:.2f}'} |"
            for name, (r, n) in result["recall"].items()]
    lines = [f"# Retrieval eval: {search_name}", "",
             f"recall@{k}: the share of each question's expected (file, section) pairs found in the top {k} results.",
             "", f"| subset | questions | recall@{k} |", "| --- | --- | --- |", *rows, "",
             f"Private-file leaks: {len(result['leaks'])} of {result['private']} private questions.", ""]
    if result["missed"]:
        lines += ["Questions missing an expected source: " + ", ".join(result["missed"]), ""]
    if result["leaks"]:
        lines += ["Leaked a private file: " + ", ".join(result["leaks"]), ""]
    lines += ["Regenerate with `uv run python scripts/kb_eval.py`.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="the directory the question files are relative to")
    parser.add_argument("--questions", type=Path, default=EVAL_DIR / "questions.json")
    parser.add_argument("--report", type=Path, default=EVAL_DIR / "report.md")
    parser.add_argument("--search", help="module:factory of the search to score (default: the keyword baseline)")
    args = parser.parse_args(argv)

    chunks = load_chunks(args.root)
    if args.search:
        module, _, attr = args.search.partition(":")
        sys.path.insert(0, str(ROOT / "scripts"))
        search, name = getattr(importlib.import_module(module), attr)(chunks), args.search
    else:
        search, name = keyword_search(chunks), "keyword baseline over heading chunks"
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    result = run(questions, search)
    args.report.write_text(report_markdown(result, name), encoding="utf-8")
    print(report_markdown(result, name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
