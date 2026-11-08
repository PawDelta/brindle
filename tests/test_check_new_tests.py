import importlib.util
from pathlib import Path

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


def test_new_rows_are_judged_worth_committing(monkeypatch, capsys):
    base = {"tests/test_a.py::test_t[x]": None}
    head = {**base, **{f"tests/test_a.py::test_t[r{i}]": None for i in range(80)},
            "tests/test_a.py::test_t[bypass]": "bypass attempt"}
    monkeypatch.setattr(gate, "changed_test_files", lambda b: ["tests/test_a.py"])
    monkeypatch.setattr(gate, "collect_in", lambda root, files: head)
    monkeypatch.setattr(gate, "tests_at", lambda b, files: base)
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
