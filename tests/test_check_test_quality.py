import importlib.util
import itertools
import json
from collections import Counter
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
    assert "test_large" in collection.issues[0].message and "16 permutations" in collection.issues[0].message


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
    assert all("unmocked time.sleep" in issue.message for issue in issues)


def test_bare_patchers_do_not_mock_sleep(tmp_path):
    path = tmp_path / "test_patchers.py"
    path.write_text('''
import time
from unittest.mock import patch
def test_patchers(monkeypatch):
    patch("time.sleep")
    time.sleep(1)
    patch.object(time, "sleep")
    time.sleep(2)
    patcher = patch("time.sleep")
    patcher.start()
    time.sleep(3)
    patcher.stop()
    time.sleep(4)
    patch.object(time, "sleep").start()
    time.sleep(5)
def test_immediate(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    time.sleep(6)
''')
    issues = quality.sleep_issues(path)
    assert [issue.key.rsplit("::", 1)[-1] for issue in issues] == [
        "time.sleep(1)", "time.sleep(2)", "time.sleep(4)"]


def test_module_patch_does_not_replace_direct_sleep_imports(tmp_path):
    path = tmp_path / "test_bindings.py"
    path.write_text('''
import time as clock
from time import sleep
from time import sleep as pause
from unittest.mock import patch
@patch("time.sleep")
def test_decorator(mock):
    sleep(1)
    pause(2)
    clock.sleep(3)
def test_context(monkeypatch):
    with patch.object(clock, "sleep"):
        pause(4)
        clock.sleep(5)
    monkeypatch.setattr(clock, "sleep", lambda _: None)
    sleep(6)
    clock.sleep(7)
''')
    issues = quality.sleep_issues(path)
    assert [issue.key.rsplit("::", 1)[-1] for issue in issues] == [
        "sleep(1)", "pause(2)", "pause(4)", "sleep(6)"]


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
        plugins[0].issues = [quality.Finding("parametrize:tests/test_bad.py::test_bad::16",
                                           "stacked parametrize generates 16 permutations")]
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
    command = [sys.executable, str(quality.ROOT / "scripts/check_test_quality.py"),
               "--root", str(tmp_path), "--budget", "16"]
    dumped = subprocess.run([*command, "--dump-baseline"], capture_output=True, text=True)
    assert dumped.returncode == 0, dumped.stdout + dumped.stderr
    baseline = tmp_path / "baseline.json"
    baseline.write_text(dumped.stdout)
    passed = subprocess.run([*command, "--baseline", str(baseline)], capture_output=True, text=True)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "1 grandfathered" in passed.stdout
    # Enlarging the same legacy matrix must fail even with a generous budget.
    source = tests / "test_generated.py"
    source.write_text(source.read_text().replace("range(4)", "range(5)"))
    failed = subprocess.run([*command, "--budget", "20", "--baseline", str(baseline)],
                            capture_output=True, text=True)
    assert failed.returncode == 1
    assert "20 permutations" in failed.stdout


def test_sleep_baseline_tracks_scope_expression_and_occurrences(tmp_path):
    path = tmp_path / "test_sleep.py"
    path.write_text("import time\ndef test_old():\n    time.sleep(1)\n")
    old = quality.sleep_issues(path, tmp_path)
    baseline = Counter(f.key for f in old)
    path.write_text("# unrelated line edit\n" + path.read_text())
    assert quality.new_findings(quality.sleep_issues(path, tmp_path), baseline) == []
    path.write_text(path.read_text() + "    time.sleep(1)\n    time.sleep(2)\ndef test_new():\n    time.sleep(1)\n")
    assert len(quality.new_findings(quality.sleep_issues(path, tmp_path), baseline)) == 3
    # Repository location is not part of the key.
    assert old[0].key.startswith("sleep:test_sleep.py::test_old::")


def test_baseline_does_not_exempt_budget_or_strict_mode(tmp_path, monkeypatch, capsys):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sleep.py").write_text("import time\ndef test_old(): time.sleep(1)\n")
    baseline = tmp_path / "baseline.json"
    finding = quality.sleep_issues(tests / "test_sleep.py", tmp_path)[0]
    baseline.write_text(json.dumps({"version": 1, "violations": {finding.key: 1}}))

    def collect(args, plugins):
        plugins[0].count = 2
        return 0

    monkeypatch.setattr(quality.pytest, "main", collect)
    args = ["--root", str(tmp_path), "--baseline", str(baseline)]
    assert quality.main([*args, "--budget", "2"]) == 0
    assert "1 grandfathered" in capsys.readouterr().out
    assert quality.main([*args, "--budget", "1"]) == 1
    assert "total test count 2 exceeds budget 1" in capsys.readouterr().out
    assert quality.main([*args, "--strict"]) == 1
    assert "unmocked time.sleep" in capsys.readouterr().out


def test_invalid_baselines_fail_closed(tmp_path, capsys):
    path = tmp_path / "baseline.json"
    for data in [{"version": 2, "violations": {}}, {"version": 1, "violations": {"sleep:x": -1}},
                 {"version": 1, "violations": {"sleep:x": True}}, {"version": 1, "violations": []}]:
        path.write_text(json.dumps(data))
        assert quality.main(["--root", str(tmp_path), "--baseline", str(path)]) == 1
        assert "invalid baseline" in capsys.readouterr().out
    path.write_text("invalid json")
    assert quality.main(["--baseline", str(path)]) == 1
    assert quality.main(["--baseline", str(tmp_path / "missing.json")]) == 1


def test_default_baseline_is_loaded_from_selected_root(tmp_path, monkeypatch, capsys):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sleep.py").write_text("import time\ndef test_old(): time.sleep(1)\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    findings = quality.sleep_issues(tests / "test_sleep.py", tmp_path)
    (scripts / "test_quality_baseline.json").write_text(json.dumps(
        {"version": 1, "violations": dict(Counter(f.key for f in findings))}))
    monkeypatch.setattr(quality.pytest, "main", lambda *a, **kw: 0)
    assert quality.main(["--root", str(tmp_path)]) == 0
    assert "1 grandfathered" in capsys.readouterr().out
