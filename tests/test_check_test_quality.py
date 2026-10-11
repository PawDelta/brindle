import importlib.util
import itertools
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "check_test_quality", Path(__file__).resolve().parent.parent / "scripts/check_test_quality.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def test_stacked_marks_use_product_and_allow_twelve():
    collection = quality.Collection()

    def items(name, sizes, parent="tests/test_example.py"):
        names = [f"arg{i}" for i in range(len(sizes))]
        marks = [SimpleNamespace(args=(arg, list(range(n))), kwargs={}) for arg, n in zip(names, sizes)]
        return [SimpleNamespace(path="tests/test_example.py", originalname=name, name=name,
                                parent=SimpleNamespace(nodeid=parent),
                                location=("", 8, ""), iter_markers=lambda _: iter(marks),
                                callspec=SimpleNamespace(indices=dict(zip(names, indices))))
                for indices in itertools.product(*(range(n) for n in sizes))]

    bad = items("test_large", [2, 2, 4])
    collection.pytest_collection_finish(SimpleNamespace(items=[
        *items("test_boundary", [3, 4]), *items("test_single", [20]), *bad, *bad,
        *items("test_same_name", [3, 4], "TestOne"),
        *items("test_same_name", [4, 3], "TestTwo")]))
    assert collection.count == 88
    assert len(collection.issues) == 1
    assert "test_large" in collection.issues[0] and "16 permutations" in collection.issues[0]


def test_sleep_aliases_comments_and_mock_scopes(tmp_path):
    path = tmp_path / "test_sleep.py"
    path.write_text('''
import time as clock
from time import sleep as pause
from unittest.mock import patch
# time.sleep(3)
TEXT = "time.sleep(3)"
def test_real():
    clock.sleep(1)
    pause(1)
@patch("time.sleep")
def test_decorator(mock):
    clock.sleep(1)
def test_context():
    with patch.object(clock, "sleep"):
        clock.sleep(1)
    clock.sleep(1)
def test_monkeypatch(monkeypatch):
    clock.sleep(1)
    monkeypatch.setattr(clock, "sleep", lambda _: None)
    clock.sleep(1)
''')
    issues = quality.sleep_issues(path)
    assert len(issues) == 4
    assert all("unmocked time.sleep" in issue for issue in issues)


@pytest.mark.parametrize("count,budget,exit_code", [(12, 12, 0), (13, 12, 1)])
def test_budget_boundary(tmp_path, monkeypatch, capsys, count, budget, exit_code):
    (tmp_path / "tests").mkdir()

    def collect(args, plugins):
        assert "--collect-only" in args
        plugins[0].count = count
        return 0

    monkeypatch.setattr(quality.pytest, "main", collect)
    assert quality.main(["--root", str(tmp_path), "--budget", str(budget)]) == exit_code
    assert f"{count}/{budget}" in capsys.readouterr().out


def test_collection_failure_cannot_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(quality.pytest, "main", lambda *a, **kw: 2)
    assert quality.main(["--root", str(tmp_path)]) == 1
    assert "collection failed" in capsys.readouterr().out


def test_main_reports_sleep_and_parametrize(tmp_path, monkeypatch, capsys):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_bad.py").write_text("import time\ndef test_bad(): time.sleep(1)\n")

    def collect(args, plugins):
        plugins[0].count = 16
        plugins[0].issues = ["stacked parametrize generates 16 permutations"]
        return 0

    monkeypatch.setattr(quality.pytest, "main", collect)
    assert quality.main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "unmocked time.sleep" in output and "16 permutations" in output


def test_ci_checks_quality_before_tests_in_both_legs():
    script = (quality.ROOT / "scripts" / "ci_local.sh").read_text()
    host, linux = script.split('  else\n    # The export is copied inside', 1)
    assert host.index("python scripts/check_test_quality.py") < host.index("$PYTEST) >")
    assert linux.index("python scripts/check_test_quality.py") < linux.index("$PYTEST")


def test_cli_collects_generated_cases_without_running_tests(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_generated.py").write_text('''
import pytest
@pytest.mark.parametrize(argnames="a", argvalues=(n for n in range(4)))
@pytest.mark.parametrize("b", [1, 2, 3, 4])
def test_generated(a, b):
    raise AssertionError("collection must never execute tests")
''')
    result = subprocess.run([sys.executable, str(quality.ROOT / "scripts/check_test_quality.py"),
                             "--root", str(tmp_path), "--budget", "15"],
                            capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "16 permutations" in result.stdout
    assert "total test count 16 exceeds budget 15" in result.stdout
    assert "collection failed" not in result.stdout
