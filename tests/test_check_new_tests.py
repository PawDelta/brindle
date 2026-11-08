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


def test_listing_failures_stop_the_check_worth_committing(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="could not list the tests"):
        gate.collect_in(tmp_path, ["missing_test.py"])   # pytest's usage error, not "no tests"
    crashed = type("P", (), {"returncode": 2, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **kw: crashed)
    with pytest.raises(SystemExit, match="pytest exited 2"):
        gate.run_with_coverage(["tests/test_a.py"], tmp_path / ".coverage", append=False)


def test_a_test_that_passes_only_sometimes_fails_the_check_worth_committing(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_f.py").write_text(
        "import os\ndef test_sometimes(): assert os.environ['BRINDLE_SHUFFLE_SEED'] != '2'\n"
        "def test_always(): pass\n")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FLAKY_RUNS", 2)
    # one run in two fails, under -n 2 and a different order each time
    assert gate.flaky_python(["tests/test_f.py"]) == {"tests.test_f.test_sometimes": 1}


def test_a_vitest_run_that_fails_without_naming_a_test_still_counts_worth_committing(tmp_path, monkeypatch):
    (tmp_path / "app/node_modules").mkdir(parents=True)
    (tmp_path / "app/a.test.ts").write_text("")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FLAKY_RUNS", 2)
    monkeypatch.setattr(gate, "_git", lambda *a, **kw: "app/a.test.ts")
    runs = iter([
        {"success": False, "testResults": [{"name": "/x/a.test.ts", "status": "failed", "message": "boom",
                                            "assertionResults": []}]},      # a suite that failed to load
        {"success": False, "testResults": []}])                              # a crash with nothing named

    def fake_vitest(cmd, **kw):
        out = next(a for a in cmd if a.startswith("--outputFile=")).split("=", 1)[1]
        Path(out).write_text(__import__("json").dumps(next(runs)))
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gate.subprocess, "run", fake_vitest)
    assert gate.flaky_vitest("x") == {"a.test.ts: the suite failed (boom)": 1,
                                      "vitest (exit 1): the run failed without naming a test": 1}


def test_changed_inputs_and_stable_ids_are_judged_by_real_collection_worth_committing(tmp_path):
    def new_rows(base_src, head_src):
        here = tmp_path / str(len(list(tmp_path.iterdir())))   # a fresh folder: no stale bytecode
        here.mkdir()
        (here / "test_base.py").write_text(base_src)
        (here / "test_head.py").write_text(head_src)
        ids = list(gate.collect_in(here, ["test_base.py", "test_head.py"]))
        pick = lambda f: [n.split("::", 1)[1] for n in ids if n.startswith(f)]   # noqa: E731
        scanned = lambda src: gate.inventory.scan(src)["test_t"]                  # noqa: E731
        return gate._new_ids(pick("test_head"), pick("test_base"), scanned(base_src), scanned(head_src))

    # a table that reaches its rows through assignments, one of them changed upstream by name
    computed = "import pytest\nN = %d\nCASES = list(range(N))\n@pytest.mark.parametrize('n', CASES)\ndef test_t(n): pass\n"
    assert new_rows(computed % 2, computed % 3) == ["test_t[0]", "test_t[1]", "test_t[2]"]
    assert new_rows(computed % 2, computed % 2) == []
    # a replaced value in one of two stacked tables keeps the id `0-x0`
    stacked = ("import pytest\n@pytest.mark.parametrize('a', ['x'])\n"
               "@pytest.mark.parametrize('d', [{'k': %d}])\ndef test_t(a, d): pass\n")
    assert len(new_rows(stacked % 1, stacked % 2)) == 1
    assert new_rows(stacked % 1, stacked % 1) == []
    # a chained assignment feeding a table by name: the row text is unchanged, the id stays `d0`
    chained = ("import pytest\nVALUE = ALIAS = %d\nCASES = [{'k': VALUE}]\n"
               "@pytest.mark.parametrize('d', CASES)\ndef test_t(d): pass\n")
    assert new_rows(chained % 1, chained % 2) == ["test_t[d0]"]
    # swapped unpacking targets: the row changes from {'k': 1} to {'k': 2} and the id stays `d0`
    unpack = ("import pytest\n%s = (1, 2)\nCASES = [{'k': A}]\n"
              "@pytest.mark.parametrize('d', CASES)\ndef test_t(d): pass\n")
    assert new_rows(unpack % "A, B", unpack % "B, A") == ["test_t[d0]"]
    # pytest numbers dict parameters by position: the inserted row is case0 and the old one becomes case1
    table = "import pytest\n@pytest.mark.parametrize('d', %s)\ndef test_t(d): pass\n"
    assert new_rows(table % "[{'k': 2}]", table % "[{'k': 1}, {'k': 2}]") == ["test_t[d0]"]
    # a row's variant mark travels with its collected id
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / "test_m.py").write_text(
        "import pytest\n@pytest.mark.parametrize('n', [1, pytest.param(2, marks=pytest.mark.variant('race'))])\n"
        "def test_m(n): pass\n")
    assert gate.collect_in(marked, ["test_m.py"]) == {"test_m.py::test_m[1]": None, "test_m.py::test_m[2]": "race"}


def test_a_new_test_that_never_ran_is_not_a_pass_worth_committing(tmp_path, monkeypatch):
    new = "tests/test_a.py::TestMissing::test_new"
    skipped_twin = "tests/test_a.py::TestSkipped::test_new"    # same name, another class

    def partial(files, coverage_file, append):
        data = CoverageData(str(coverage_file))
        data.set_context("tests/test_a.py::test_old|run")
        data.add_lines({str(tmp_path / "m.py"): [1]})
        data.write()
        return skips

    monkeypatch.setattr(gate, "changed_test_files", lambda b: ["tests/test_a.py"])
    monkeypatch.setattr(gate, "run_with_coverage", partial)
    skips = set()    # coverage exists, but this test left none and wasn't skipped
    assert gate.scoped_redundant("x", [new]) == ([], [], [new])
    skips = {skipped_twin}   # a skip is matched by its full node id: the twin's doesn't excuse this one
    assert gate.scoped_redundant("x", [new, skipped_twin]) == ([], [skipped_twin], [new])


def test_a_duplicate_in_another_file_is_found_by_the_scoped_run_worth_committing(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts/mod.py").write_text("def compute(x):\n    if x:\n        return 1\n    return 2\n")
    (tmp_path / "tests/test_new.py").write_text(
        "import pytest\nfrom scripts.mod import compute\ndef test_dup(): assert compute(1) == 1\n"
        "@pytest.mark.skip('recorded by the plugin')\ndef test_skipped(): pass\n")
    (tmp_path / "tests/test_old.py").write_text(
        "from scripts.mod import compute\ndef test_orig(): assert compute(1) == 1\n"
        "def test_other(): assert compute(0) == 2\n")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "changed_test_files", lambda b: ["tests/test_new.py"])
    ids = ["tests/test_new.py::test_dup", "tests/test_new.py::test_skipped"]
    assert gate.scoped_redundant("x", ids) == ([ids[0]], [ids[1]], [])


@pytest.mark.variant("the module fallback: the covered function is a main() no test names")
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
