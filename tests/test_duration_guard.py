from types import SimpleNamespace

import pytest

import conftest


def _report(duration, when="call", passed=True, keywords=()):
    return SimpleNamespace(duration=duration, when=when, passed=passed,
                           keywords={k: 1 for k in keywords})


@pytest.mark.parametrize("report,limit,over", [
    (_report(6.0), 5.0, True),
    (_report(4.0), 5.0, False),
    (_report(6.0, keywords=["slow"]), 5.0, False),
    (_report(6.0, when="setup"), 5.0, False),
    (_report(6.0, passed=False), 5.0, False),
    (_report(60.0), 0.0, False),
])
@pytest.mark.variant("tests the duration guard in tests/conftest.py, which coverage does not measure")
def test_over_limit(report, limit, over):
    assert conftest._over_limit(report, limit) is over


@pytest.mark.variant("tests the duration guard in tests/conftest.py, which coverage does not measure")
def test_session_fails_when_a_test_ran_too_long(monkeypatch):
    monkeypatch.setattr(conftest, "_SLOW_TESTS", [("tests/test_x.py::test_y", 9.0)])
    lines = []
    config = SimpleNamespace(get_terminal_writer=lambda: SimpleNamespace(
        line=lambda text, **_: lines.append(text)))
    session = SimpleNamespace(config=config, exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    assert "tests/test_x.py::test_y" in lines[0]
