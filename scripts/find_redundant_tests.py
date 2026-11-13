#!/usr/bin/env python3
"""Audit removable tests and module coverage from one per-test coverage run."""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_new_tests import ROOT, _Collect, _plugin_env


class _Variants(_Collect):
    def pytest_collection_modifyitems(self, items):
        super().pytest_collection_modifyitems(items)
        for item in items:
            if item.get_closest_marker("variant") is not None:
                self.found[item.nodeid] = self.found[item.nodeid] or "variant"


def collect_variants(files: list[str]) -> dict:
    """Collect real inherited and parameter-row marks in an isolated process."""
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--collect-variants", *files],
                          cwd=ROOT, capture_output=True, text=True)
    try:
        listed = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise ValueError(f"test collection failed: {proc.stderr[-2000:]}") from None
    if proc.returncode or listed["status"] != 0:
        raise ValueError(f"test collection failed (exit {listed['status']}): "
                         f"{listed.get('output', '')}{proc.stderr[-2000:]}")
    return listed["tests"]


def greedy_redundant(tests: dict[str, set], protected=(), background=()) -> list[str]:
    """Remove smallest tests first, updating ownership after every removal.

    Ties use node id, so identical tests retain a deterministic representative.
    Background points belong to collection/import code that is never removed.
    """
    counts = Counter(point for points in tests.values() for point in points)
    counts.update(set(background))
    protected = set(protected)
    removed = []
    for name in sorted(tests, key=lambda n: (len(tests[n]), n)):
        if name not in protected and all(counts[p] > 1 for p in tests[name]):
            removed.append(name)
            counts.subtract(tests[name])
    return removed


def local_path(filename: str) -> Path:
    """Resolve coverage captured in another checkout against this checkout."""
    path = Path(filename)
    for prefix in ("src/brindle/", "scripts/"):
        if prefix in path.as_posix():
            return ROOT / prefix / path.as_posix().split(prefix, 1)[1]
    raise ValueError(f"coverage contains an unexpected source path: {filename}")


def read_points(path: Path):
    """Read coverage's SQLite format once; avoid a query per test per module."""
    from coverage import CoverageData

    data = CoverageData(str(path))
    data.read()
    if not data.has_arcs():
        raise ValueError("branch coverage is required (--cov-branch)")
    tests, background = defaultdict(set), set()
    filenames, point_ids = {}, {}

    def point_id(point):
        if point not in point_ids:
            point_ids[point] = len(point_ids)
        return point_ids[point]

    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT file.path, context.context, arc.fromno, arc.tono "
                          "FROM arc JOIN file ON file.id = arc.file_id "
                          "JOIN context ON context.id = arc.context_id")
        for filename, context, start, end in rows:
            if filename not in filenames:
                filenames[filename] = local_path(filename).relative_to(ROOT).as_posix()
            filename = filenames[filename]
            node, sep, phase = context.rpartition("|")
            points = tests[node] if sep and phase in {"setup", "run", "teardown"} else background
            points.add(point_id((filename, "arc", start, end)))
            points.update(point_id((filename, "line", n)) for n in (start, end) if n > 0)
    if not tests:
        raise ValueError("no per-test contexts found (--cov-context=test is required)")
    return dict(tests), background


def core_module(path: Path) -> bool:
    parts = path.relative_to(ROOT / "src/brindle").parts
    return (path.name in {"db.py", "agents.py", "spawn.py", "tmux.py", "cli.py"}
            or "daemon" in parts
            or any(word in part for part in parts
                   for word in ("pipeline", "merge", "review", "autopilot", "tasks")))


def uncovered_functions(path: Path, missing: set[int], branches: dict) -> list[dict]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = []

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    functions.append((name, child.lineno, child.end_lineno))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree)
    counts = defaultdict(Counter)
    for line in missing | branches.keys():
        holders = [(name, start, end) for name, start, end in functions if start <= line <= end]
        if holders:
            name, start, _ = min(holders, key=lambda f: f[2] - f[1])
            counts[(name, start)]["lines"] += int(line in missing)
            counts[(name, start)]["branches"] += len(branches.get(line, []))
    ranked = sorted(counts, key=lambda k: (-sum(counts[k].values()), k))[:5]
    return [dict(name=name, line=line, **counts[(name, line)]) for name, line in ranked]


def module_coverage(path: Path) -> list[dict]:
    from coverage import Coverage, CoverageData

    data = CoverageData(str(path))
    data.read()
    # Map old checkout paths before coverage analyzes local source files.
    mapped = CoverageData(no_disk=True)
    mapped.add_arcs({str(local_path(f)): data.arcs(f) or [] for f in data.measured_files()})
    cov = Coverage(data_file=None, config_file=False, branch=True)
    cov.get_data().update(mapped)
    modules = []
    for source in sorted((ROOT / "src/brindle").rglob("*.py")):
        analysis = cov._analyze(str(source))
        stats = analysis.branch_stats()
        lines = len(analysis.statements)
        branches = sum(total for total, _ in stats.values())
        covered_lines = lines - len(analysis.missing)
        covered_branches = sum(taken for _, taken in stats.values())
        percent = 100 * (covered_lines + covered_branches) / (lines + branches) if lines + branches else 100
        modules.append(dict(path=source.relative_to(ROOT).as_posix(), lines=lines,
                            covered_lines=covered_lines, branches=branches,
                            covered_branches=covered_branches, percent=round(percent, 2),
                            core_under_80=core_module(source) and percent < 80,
                            uncovered_functions=uncovered_functions(source, analysis.missing,
                                                                   analysis.missing_branch_arcs())))
    return sorted(modules, key=lambda m: (m["percent"], m["path"]))


def test_spans(files: set[str]) -> dict[str, dict[str, int]]:
    spans = {}
    for filename in sorted(files):
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        found = {}

        def visit(body, prefix=""):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + "::")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    found[prefix + node.name] = node.end_lineno - start + 1

        visit(tree.body)
        spans[filename] = found
    return spans


def audit(path: Path, failed_tests=()) -> dict:
    tests, background = read_points(path)
    files = {n.split("::", 1)[0] for n in tests}
    collected = collect_variants(sorted(files))
    unknown = tests.keys() - collected.keys()
    if unknown:
        raise ValueError(f"coverage test ids absent from current collection: {sorted(unknown)[:5]}")
    protected = {n for n, reason in collected.items() if reason is not None}
    failed_tests = set(failed_tests)
    protected_failures = {n for n in tests if any(n == f or n.startswith(f + "::") for f in failed_tests)}
    successful = {n: points for n, points in tests.items() if n not in protected_failures}
    removed = greedy_redundant(successful, protected, background)
    spans = test_spans(files)
    groups = defaultdict(list)
    for name, points in tests.items():
        if name not in protected_failures:
            groups[frozenset(points)].append(name)
    by_file = defaultdict(list)
    for name in removed:
        by_file[name.split("::", 1)[0]].append(name)
    redundant = []
    for filename, names in sorted(by_file.items()):
        functions = {n.split("::", 1)[1].split("[", 1)[0] for n in names}
        redundant.append(dict(path=filename, count=len(names),
                              lines=sum(spans[filename].get(f, 0) for f in functions),
                              tests=sorted(names)))
    return dict(measured_tests=len(tests), protected_tests=len(protected & tests.keys()),
                redundant_count=len(removed), redundant=redundant,
                identical=[sorted(names) for _, names in sorted(groups.items(), key=lambda g: min(g[1]))
                           if len(names) > 1], modules=module_coverage(path), failed_tests=sorted(failed_tests))


def markdown(report: dict) -> str:
    lines = ["# Test audit", "", f"- Measured tests: {report['measured_tests']}",
             f"- Protected variants: {report['protected_tests']}",
             f"- Redundant tests: {report['redundant_count']}",
             f"- Failed / error tests: {len(report['failed_tests'])}", "", "## 1. Redundant tests", "",
             "| File | Tests / rows | Test source lines (functions counted once) |",
             "| --- | ---: | ---: |"]
    for group in report["redundant"]:
        lines.append(f"| {group['path']} | {group['count']} | {group['lines']} |")
    for group in report["redundant"]:
        lines.extend(["", f"### {group['path']}", ""])
        lines.extend(f"- `{name}`" for name in group["tests"])
    lines.extend(["", "## 2. Identical coverage groups", ""])
    for i, names in enumerate(report["identical"], 1):
        lines.extend([f"### {i}. {len(names)} tests", ""])
        lines.extend(f"- `{name}`" for name in names)
        lines.append("")
    lines.extend(["## 3. Module line + branch coverage", "",
                  "| Module | Lines covered / total | Branches covered / total | Combined % | Core <80% |",
                  "| --- | ---: | ---: | ---: | --- |"])
    for module in report["modules"]:
        lines.append(f"| {module['path']} | {module['covered_lines']}/{module['lines']} | "
                     f"{module['covered_branches']}/{module['branches']} | {module['percent']:.2f} | "
                     f"{'!' if module['core_under_80'] else ''} |")
    for module in report["modules"]:
        if module["core_under_80"]:
            lines.extend(["", f"### {module['path']}", ""])
            for function in module["uncovered_functions"]:
                lines.append(f"- `{function['name']}:{function['line']}`: "
                             f"{function.get('lines', 0)} lines, {function.get('branches', 0)} branches")
    if report["failed_tests"]:
        lines.extend(["", f"## 4. Failed tests: {len(report['failed_tests'])}", ""])
        lines.extend(f"- `{name}`" for name in report["failed_tests"])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, help="reuse an existing per-test branch coverage database")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--check", action="store_true", help="fail if non-allowlisted removals exceed --max")
    parser.add_argument("--max", type=int, default=0, metavar="N")
    parser.add_argument("--output", type=Path, help="write the report to this path instead of stdout")
    parser.add_argument("--failed-tests", type=Path,
                        help="pytest lastfailed JSON from the coverage run; protect failed tests")
    parser.add_argument("--collect-variants", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.collect_variants:
        import pytest

        plugin = _Variants()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = pytest.main([*args.collect_variants, "--collect-only", "-q", "-o", "addopts=",
                                  "-m", "", "-k", "", "-p", "no:cacheprovider"], plugins=[plugin])
        print(json.dumps(dict(status=int(status), tests=plugin.found, output=output.getvalue()[-2000:])))
        return 0
    if args.max < 0:
        parser.error("--max must be nonnegative")
    path = args.coverage or ROOT / ".coverage"
    suite_status = None
    failed_tests = set()
    if args.coverage is None:
        with tempfile.TemporaryDirectory(prefix="test-audit-", dir=ROOT / ".brindle") as work:
            suite_status = subprocess.run(
                [sys.executable, "-m", "pytest", "-n", "auto", "-m", "not serial",
                 "--cov=brindle", "--cov=scripts", "--cov-branch", "--cov-context=test",
                 "-o", f"cache_dir={work}"], cwd=ROOT, stdout=sys.stderr,
                env=_plugin_env(COVERAGE_FILE=str(path))).returncode
            failures = Path(work) / "v/cache/lastfailed"
            if failures.exists():
                failed_tests.update(json.loads(failures.read_text()))
        if suite_status not in (0, 1):
            parser.exit(suite_status, "coverage run incomplete; audit not generated\n")
    if not path.is_file():
        parser.error(f"coverage file does not exist: {path}")
    try:
        if args.failed_tests:
            failed_tests.update(json.loads(args.failed_tests.read_text()))
        report = audit(path, failed_tests)
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    report["suite_exit"] = suite_status
    allow_path = ROOT / ".brindle/test-audit/allow.txt"
    allowed = {line.strip() for line in allow_path.read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")} if allow_path.exists() else set()
    report["outside_allowlist"] = sum(n not in allowed for g in report["redundant"] for n in g["tests"])
    output = json.dumps(report, indent=2) + "\n" if args.json else markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return int(bool(suite_status) or (args.check and report["outside_allowlist"] > args.max))


if __name__ == "__main__":
    raise SystemExit(main())
