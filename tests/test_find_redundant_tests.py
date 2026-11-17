import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "find_redundant_tests", Path(__file__).resolve().parents[1] / "scripts/find_redundant_tests.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_greedy_removals_preserve_joint_line_and_arc_coverage():
    line = ("m.py", "line", 1)
    arc = ("m.py", "arc", 1, 2)
    other_arc = ("m.py", "arc", 1, 3)
    tests = {"small": {line}, "duplicate_a": {line, arc},
             "duplicate_b": {line, arc}, "branch": {line, other_arc},
             "variant": {line}, "empty": set(), "background": {("import",)}}
    removed = audit.greedy_redundant(tests, protected={"variant"}, background={("import",)})
    assert removed == ["empty", "background", "small", "duplicate_a"]
    kept = set().union(*(points for name, points in tests.items() if name not in removed), {("import",)})
    assert kept == set().union(*tests.values())
