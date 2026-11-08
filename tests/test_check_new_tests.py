import importlib.util
from pathlib import Path

import pytest
from coverage import CoverageData

spec = importlib.util.spec_from_file_location(
    "check_new_tests", Path(__file__).resolve().parent.parent / "scripts/check_new_tests.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


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
    ids = ["tests/test_a.py::test_repeat", "tests/test_a.py::test_new", "tests/test_a.py::test_skipped"]
    redundant, unmeasured = gate.uncovered_alone(tmp_path / ".coverage", ids)
    assert redundant == ["tests/test_a.py::test_repeat"]
    assert unmeasured == ["tests/test_a.py::test_skipped"]
    assert gate.analyse(tmp_path / ".coverage", ids[1:2])[ids[1]] == {(src, 4)}


def _branch(monkeypatch, tmp_path, base_src, head_src, base_ids, head_ids):
    """A branch that changes tests/test_a.py from base_src to head_src."""
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests/test_a.py").write_text(head_src)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "_git", lambda *a, **kw: base_src)
    monkeypatch.setattr(gate, "changed_test_files", lambda b: ["tests/test_a.py"])
    monkeypatch.setattr(gate, "collect_in", lambda root, files: head_ids)
    monkeypatch.setattr(gate, "tests_at", lambda b, files: base_ids)


def test_a_row_inserted_before_an_old_one_is_the_new_row_worth_committing(monkeypatch, tmp_path):
    # pytest numbers dict parameters by position: the inserted row is case0 and the old one becomes case1
    ids = lambda *n: {f"tests/test_a.py::test_t[case{i}]": None for i in n}   # noqa: E731
    table = "import pytest\n@pytest.mark.parametrize('d', %s)\ndef test_t(d): pass\n"
    _branch(monkeypatch, tmp_path, table % "[{'k': 2}]", table % "[{'k': 1}, {'k': 2}]", ids(0), ids(0, 1))
    assert list(gate.new_tests("x")) == ["tests/test_a.py::test_t[case0]"]
    # a table no source comparison can match to the ids: every row of a changed function is judged
    _branch(monkeypatch, tmp_path, table % "build()", table % "build()", ids(0), ids(0, 1))
    assert list(gate.new_tests("x")) == list(ids(0, 1))


def test_listing_failures_stop_the_check_worth_committing(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="could not list the tests"):
        gate.collect_in(tmp_path, ["missing_test.py"])   # pytest's usage error, not "no tests"
    crashed = type("P", (), {"returncode": 2, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **kw: crashed)
    with pytest.raises(SystemExit, match="pytest exited 2"):
        gate.run_with_coverage(["tests/test_a.py"], tmp_path / ".coverage", append=False)


def test_related_tests_fall_back_to_the_module_worth_committing(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_other.py").write_text("from brindle import agents\ndef test_x(): agents.go()\n")
    (tests / "test_unrelated.py").write_text("def test_y(): pass\n")
    # the lines only the new test covers are in main(): no test names it, but a file tests the module
    assert gate.related_test_files({"main"}, {"agents"}, set(), tests) == ["tests/test_other.py"]
    assert gate.modules_of({("/r/src/brindle/agents.py", 3), ("/r/tests/test_x.py", 1)}) == {"agents"}


def test_new_rows_are_judged_worth_committing(monkeypatch, tmp_path, capsys):
    base = {"tests/test_a.py::test_t[x]": None}
    head = {**base, **{f"tests/test_a.py::test_t[r{i}]": None for i in range(80)},
            "tests/test_a.py::test_t[bypass]": "bypass attempt"}
    rows = ["'x'", *(f"'r{i}'" for i in range(80)), "'bypass'"]
    head_src = "import pytest\n@pytest.mark.parametrize('v', [%s])\ndef test_t(v): pass\n" % ", ".join(rows)
    _branch(monkeypatch, tmp_path, head_src.replace(", " + ", ".join(rows[1:]), ""), head_src, base, head)
    judged = []

    def only_r0_is_new(coverage, ids):
        judged.extend(ids)
        return [n for n in ids if not n.endswith("[r0]")], []

    monkeypatch.setattr(gate, "uncovered_alone", only_r0_is_new)
    assert gate.main(["--base", "x"]) == 1      # 81 new rows, no count limit: only repeats fail
    out = capsys.readouterr().out
    assert "test_t[r1] covers no line or branch" in out and "test_t[r0] covers" not in out
    assert "tests/test_a.py::test_t[x]" not in judged and "bypass" not in " ".join(judged)
    assert "variant: tests/test_a.py::test_t[bypass]: bypass attempt" in out
    assert gate.main(["--base", "x", "--approved-by", next(iter(gate.APPROVERS))]) == 0
    monkeypatch.setattr(gate, "uncovered_alone", lambda c, ids: ([], []))
    assert gate.main(["--base", "x"]) == 0      # lots of new rows, each worth committing


def test_collected_rows_carry_their_variant_marks_worth_committing(tmp_path):
    (tmp_path / "test_t.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('n', [1, pytest.param(2, marks=pytest.mark.variant('race'))])\n"
        "def test_t(n): pass\n")
    assert gate.collect_in(tmp_path, ["test_t.py"]) == {"test_t.py::test_t[1]": None,
                                                        "test_t.py::test_t[2]": "race"}
