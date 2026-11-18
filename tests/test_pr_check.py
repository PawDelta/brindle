from brindle import autopilot, pr_check

from test_autopilot import root  # noqa: F401  (fixture)


def test_run_shows_checks_stage_only_while_running_and_returns_result(db, root):  # noqa: F811
    seen = []

    def runner(argv, cwd):
        seen.append(autopilot.goal_activity(db, "boss")["stage"])
        return (1, "boom\nFAILED x") if "check_new_tests.py" in argv[-3] else (0, "ok\n3 passed")

    result = pr_check.run(db, "boss", "/x", runner=runner)
    assert seen and set(seen) == {"checks"}
    assert autopilot.goal_activity(db, "boss")["stage"] != "checks"
    assert not result.passed
    assert [s.name for s in result.steps] == ["suite", "serial", "new tests"]
    assert "FAILED x" in result.render()


def test_checks_mark_cleared_when_a_step_crashes(db, root):  # noqa: F811
    def runner(argv, cwd):
        raise RuntimeError("no uv")

    try:
        pr_check.run(db, "boss", "/x", runner=runner)
    except RuntimeError:
        pass
    assert db.get_autopilot("boss").checking_since is None


def test_overlapping_run_is_refused_and_leaves_the_running_one_marked(db, root):  # noqa: F811
    inner = []

    def runner(argv, cwd):
        try:
            pr_check.run(db, "boss", "/x", runner=lambda a, c: (0, ""))
        except pr_check.Busy:
            inner.append(autopilot.goal_activity(db, "boss")["stage"])
        return 0, ""

    pr_check.run(db, "boss", "/x", runner=runner)
    assert inner and set(inner) == {"checks"}
    assert db.get_autopilot("boss").checking_since is None


def test_mark_stays_live_past_the_checking_timeout(db, root, monkeypatch):  # noqa: F811
    import threading
    import time

    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(pr_check, "BEAT", 0.01)
    beat = threading.Event()
    real = pr_check._Mark.refresh

    def refresh(self, conn):
        real(self, conn)
        beat.set()

    monkeypatch.setattr(pr_check._Mark, "refresh", refresh)
    seen = []

    def runner(argv, cwd):
        clock[0] += autopilot.CHECKING_TIMEOUT + 100   # a run longer than the timeout
        beat.clear()
        beat.wait()
        seen.append(autopilot.checking(db.get_autopilot("boss")))
        return 0, ""

    pr_check.run(db, "boss", "/x", runner=runner)
    assert seen and all(seen)
