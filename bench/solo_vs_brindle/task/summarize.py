"""Turn a pytest JUnit report into grade.sh's JSON summary.

usage: summarize.py <hidden_tests_dir> <junit.xml>

The expected tests are read from the test files themselves (every top-level
``test_*`` function in ``test_partN_<name>.py``), so a module that fails to
import still counts all of its tests as failed instead of vanishing from the
total. A test passes only if the report shows it ran with no failure, error or
skip.
"""

import ast
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def expected_tests(tests_dir: Path) -> dict:
    parts = {}
    for path in sorted(tests_dir.glob("test_part*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        ]
        parts[path.stem] = names
    return parts


def passed_tests(report: Path) -> set:
    """(module, function) pairs whose every reported case passed.

    Parametrized cases ("test_x[a]") are folded into their function, which
    passes only if all of its cases do.
    """
    passed, failed = set(), set()
    if not report.exists():
        return passed
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError:
        return passed
    for case in root.iter("testcase"):
        module = case.get("classname", "").rsplit(".", 1)[-1]
        key = (module, case.get("name", "").split("[", 1)[0])
        if any(child.tag in ("failure", "error", "skipped") for child in case):
            failed.add(key)
        else:
            passed.add(key)
    return passed - failed


def main(argv) -> int:
    tests_dir, report = Path(argv[1]), Path(argv[2])
    ok = passed_tests(report)
    per_part = {}
    for module, names in expected_tests(tests_dir).items():
        n_passed = sum((module, name) in ok for name in names)
        part = module[len("test_"):]
        per_part[part] = {
            "passed": n_passed,
            "failed": len(names) - n_passed,
            "total": len(names),
        }
    summary = {
        "passed": sum(p["passed"] for p in per_part.values()),
        "failed": sum(p["failed"] for p in per_part.values()),
        "total": sum(p["total"] for p in per_part.values()),
        "per_part": per_part,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
