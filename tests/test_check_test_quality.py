import importlib.util
import itertools
import json
from collections import Counter
from pathlib import Path
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


# Each source is checked as one test module; the numbers are the sleep calls
# the checker must flag, in order.
PATCHERS = '''
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
def test_assignment():
    patcher: object = patch.object(time, "sleep")
    mock_sleep: object = patcher.start()
    time.sleep(6)
def test_stopall():
    mock_sleep = patch("time.sleep").start()
    patch.stopall()
    time.sleep(7)
@patch("time.sleep")
def test_decorator_survives_stopall(mock):
    patch("time.sleep").start()
    patch.stopall()
    time.sleep(8)
def test_monkeypatch_survives_stopall(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    patch("time.sleep").start()
    patch.stopall()
    time.sleep(9)
'''
BINDINGS = '''
import time as clock
from time import sleep
from time import sleep as pause
from unittest.mock import patch
# time.sleep(99)
TEXT = "time.sleep(99)"
def test_real():
    clock.sleep(1)
    pause(2)
@patch("time.sleep")
def test_decorator(mock):
    sleep(3)
    pause(4)
    clock.sleep(5)
def test_context(monkeypatch):
    with patch.object(clock, "sleep"):
        pause(6)
        clock.sleep(7)
    clock.sleep(8)
    monkeypatch.setattr(clock, "sleep", lambda _: None)
    sleep(9)
    clock.sleep(10)
'''
CONTROL_FLOW = '''
import time
import pytest
from unittest.mock import patch
def test_undo(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    time.sleep(0)
    monkeypatch.undo()
    time.sleep(1)
@patch("time.sleep")
def test_undo_keeps_decorators(mock, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.undo()
    time.sleep(2)
def test_string_target(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    time.sleep(3)
def test_nested(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(Exception):
        time.sleep(4)
        monkeypatch.undo()
        time.sleep(5)
    time.sleep(6)
def test_finally_stop():
    patcher = patch("time.sleep")
    patcher.start()
    try:
        time.sleep(7)
    finally:
        patcher.stop()
    time.sleep(8)
def test_if(monkeypatch, use_mock):
    if use_mock:
        monkeypatch.setattr(time, "sleep", lambda _: None)
    else:
        time.sleep(9)
    time.sleep(10)
def test_loop(monkeypatch, items):
    for item in items:
        monkeypatch.setattr(time, "sleep", lambda _: None)
    time.sleep(11)
def test_try_except(monkeypatch):
    try:
        monkeypatch.setattr(time, "sleep", lambda _: None)
    except Exception:
        time.sleep(12)
    time.sleep(13)
'''
CONTEXTS = '''
import time
def test_context(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(time, "sleep", lambda _: None)
        time.sleep(1)
    time.sleep(2)
def test_context_keeps_outer_mock(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with monkeypatch.context() as m:
        m.setattr(time, "sleep", lambda _: None)
        time.sleep(3)
    time.sleep(4)
def test_deferred_mocks_install_nothing(monkeypatch):
    install = lambda: monkeypatch.setattr(time, "sleep", lambda _: None)
    False and monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(time, "sleep", lambda _: None) if install else None
    time.sleep(5)
'''


@pytest.mark.parametrize("source,flagged", [
    (PATCHERS, [1, 2, 4, 7]),
    (BINDINGS, [1, 2, 3, 4, 6, 8, 9]),
    (CONTROL_FLOW, [1, 5, 6, 8, 9, 10, 11, 12, 13]),
    (CONTEXTS, [2, 5]),
], ids=["patchers", "bindings", "control-flow", "contexts"])
def test_unmocked_sleeps_are_flagged(tmp_path, source, flagged):
    path = tmp_path / "test_sleep.py"
    path.write_text(source)
    keys = [issue.key.rsplit("::", 1)[-1] for issue in quality.sleep_issues(path)]
    assert [int(key[key.index("(") + 1:-1]) for key in keys] == flagged


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


def test_main_budget_baseline_and_strict_mode(tmp_path, monkeypatch, capsys):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sleep.py").write_text("import time\ndef test_old(): time.sleep(1)\n")
    count = {"n": 2}

    def collect(args, plugins):
        assert "--collect-only" in args
        plugins[0].count = count["n"]
        return 0

    monkeypatch.setattr(quality.pytest, "main", collect)
    root = ["--root", str(tmp_path)]
    assert quality.main([*root, "--budget", "2"]) == 1
    assert "unmocked time.sleep" in capsys.readouterr().out

    # The default baseline is read from the selected root.
    assert quality.main([*root, "--dump-baseline"]) == 0
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "test_quality_baseline.json").write_text(capsys.readouterr().out)
    assert quality.main([*root, "--budget", "2"]) == 0
    assert "2/2 tests; 0 new issue(s); 1 grandfathered" in capsys.readouterr().out
    count["n"] = 3
    assert quality.main([*root, "--budget", "2"]) == 1
    assert "total test count 3 exceeds budget 2" in capsys.readouterr().out
    assert quality.main([*root, "--budget", "3", "--strict"]) == 1

    monkeypatch.setattr(quality.pytest, "main", lambda *a, **kw: 2)
    assert quality.main(root) == 1
    assert "collection failed" in capsys.readouterr().out


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
