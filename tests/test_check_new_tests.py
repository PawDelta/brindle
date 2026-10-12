import importlib.util
from pathlib import Path

from coverage import CoverageData

spec = importlib.util.spec_from_file_location(
    "check_new_tests", Path(__file__).resolve().parent.parent / "scripts/check_new_tests.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_test_functions_find_methods_and_variant_reasons():
    found = gate.test_functions('''
import pytest
def helper(): pass
def test_plain(): pass
@pytest.mark.variant("symlink bypass")
def test_marked(): pass
@pytest.mark.variant("")
def test_empty_reason(): pass
class TestGroup:
    async def test_method(self): pass
class Helper:
    def test_ignored(self): pass
''')
    assert found == {"test_plain": None, "test_marked": "symlink bypass",
                     "test_empty_reason": None, "TestGroup::test_method": None}


def test_a_test_must_cover_something_no_other_test_covers(tmp_path):
    src = str(tmp_path / "src/brindle/m.py")
    data = CoverageData(str(tmp_path / ".coverage"))
    for context, lines in [("", [1]),                                    # import time
                           ("tests/test_a.py::test_old|run", [2, 3]),
                           ("tests/test_a.py::test_repeat|run", [1, 2]),
                           ("tests/test_a.py::test_repeat_two|run", [9]),  # a name it prefixes
                           ("tests/test_a.py::test_new[x]|setup", [4]),
                           ("tests/test_a.py::test_new[y]|run", [3])]:
        data.set_context(context)
        data.add_lines({src: lines})
    data.write()
    redundant, unmeasured = gate.uncovered_alone(
        tmp_path / ".coverage",
        ["tests/test_a.py::test_repeat", "tests/test_a.py::test_new", "tests/test_a.py::test_skipped"])
    assert redundant == ["tests/test_a.py::test_repeat"]
    assert unmeasured == ["tests/test_a.py::test_skipped"]


def test_growth_limits_and_who_can_waive_them(monkeypatch, capsys):
    added = {f"tests/test_a.py::test_{i}": None for i in range(gate.MAX_NEW_TESTS)}
    added["tests/test_a.py::test_variant"] = "race"
    monkeypatch.setattr(gate, "new_tests", lambda base: added)
    monkeypatch.setattr(gate, "line_counts", lambda base, path: {"tests": (500, 100), "src": (10, 90)}[path])
    monkeypatch.setattr(gate, "uncovered_alone", lambda cov, ids: (ids[:1], []))
    assert gate.main(["--base", "x"]) == 1
    out = capsys.readouterr().out
    assert f"adds {gate.MAX_NEW_TESTS + 1} test functions" in out
    assert "adds 400 test lines for 10 source lines" in out
    assert "tests/test_a.py::test_0 covers no line" in out
    assert "test_variant: race" in out
    assert gate.main(["--base", "x", "--approved-by", "someone-else"]) == 1
    assert gate.main(["--base", "x", "--approved-by", next(iter(gate.APPROVERS))]) == 0
