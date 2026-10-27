import os
import subprocess
import sys

from brindle import awake


class FakeProc:
    def __init__(self, argv, **_kw):
        self.argv, self.alive = argv, True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self):
        pass


def test_awake_while_workers(monkeypatch):
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(FakeProc(argv, **kw)) or started[-1])
    monkeypatch.setattr(sys, "platform", "linux")
    other = awake.Awake()
    other.update(True)
    assert not started                        # no-op off macOS

    monkeypatch.setattr(sys, "platform", "darwin")
    a = awake.Awake()
    a.update(True)
    a.update(True)
    assert [p.argv for p in started] == [["caffeinate", "-i", "-w", str(os.getpid())]]   # once, not per tick
    a.update(False)
    assert not started[0].alive and not a.held()
    a.update(False)                           # releasing twice is harmless
    a.update(True)
    assert len(started) == 2 and a.held()     # a later worker holds a fresh assertion
    started[1].alive = False                  # caffeinate died on its own
    a.update(True)
    assert len(started) == 3
