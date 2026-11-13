import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from brindle import providers, quota, spawn, subscription_usage


@pytest.mark.variant("the spawn source measured live: several sidebars each started their own usage run")
def test_sidebars_share_one_claude_usage_run_per_refresh(monkeypatch):
    """Each sidebar is a process of its own with its own refresh clock; the
    claude CLI that answers /usage starts once per refresh for all of them."""
    monkeypatch.setattr(subscription_usage, "identity", lambda: "test-subscription")
    runs = []
    answer = "Current session: 36% used · resets Oct 12 at 6:10pm (America/New_York)\n"
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: runs.append(argv) or
                        subprocess.CompletedProcess(argv, 0, answer, ""))
    monkeypatch.setattr(providers, "claude_binary", lambda *a, **k: "claude")
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    for _sidebar in range(3):
        monkeypatch.setattr(quota, "_claude_usage_at", 0.0)    # a new process: it never asked
        quota.refresh_claude_usage()
    assert len(runs) == 1


@pytest.mark.variant("a race: two sidebars find the refresh due at the same moment")
def test_two_sidebars_looking_at_once_start_one_claude_usage_run(monkeypatch):
    """The second sidebar looks while the first is reading the shared stamp,
    before it has set it: the first goes on only once the second has finished
    or is waiting its turn at the lock."""
    monkeypatch.setattr(subscription_usage, "identity", lambda: "test-subscription")
    runs = []
    answer = "Current session: 36% used · resets Oct 12 at 6:10pm (America/New_York)\n"
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: runs.append(argv) or
                        subprocess.CompletedProcess(argv, 0, answer, ""))
    monkeypatch.setattr(providers, "claude_binary", lambda *a, **k: "claude")
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    monkeypatch.setattr(quota, "_claude_usage_at", 0.0)
    second_looked = threading.Event()
    flock = fcntl.flock

    def lock(fd, how):
        if threading.current_thread().name == "second sidebar":
            second_looked.set()      # about to wait for the first one's lock
        return flock(fd, how)

    monkeypatch.setattr(fcntl, "flock", lock)
    second = threading.Thread(target=lambda: (quota.refresh_claude_usage(), second_looked.set()),
                              name="second sidebar")
    stat, looked = Path.stat, []

    def first_looks_then_waits(self, *a, **kw):
        if not self.name.startswith(quota.CLAUDE_USAGE_STAMP + ".") or looked:
            return stat(self, *a, **kw)
        looked.append(self)
        try:
            seen = stat(self, *a, **kw)     # what the first one finds, before the second looks
        except FileNotFoundError:
            seen = None
        second.start()
        second_looked.wait()
        if seen is None:
            raise FileNotFoundError(self)
        return seen

    monkeypatch.setattr(Path, "stat", first_looks_then_waits)
    quota.refresh_claude_usage()
    second.join()
    assert len(runs) == 1


def test_auth_and_token_files_are_never_read(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text('{"tokens": {"access_token": "secret"}}')
    rl = {"primary": {"used_percent": 50.0, "window_minutes": 300, "resets_at": time.time() + 600}}
    d = home / "sessions" / "2026" / "09" / "30"
    d.mkdir(parents=True)
    (d / "rollout-2026-09-30T10-00-00-abc.jsonl").write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rl}}) + "\n")
    opened = []
    path_cls = type(home)
    real_open, real_read_text = path_cls.open, path_cls.read_text

    def spy_open(self, *a, **kw):
        opened.append(str(self))
        return real_open(self, *a, **kw)

    def spy_read_text(self, *a, **kw):
        opened.append(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(path_cls, "open", spy_open)
    monkeypatch.setattr(path_cls, "read_text", spy_read_text)
    quota.get("codex")
    quota.refresh_codex()
    assert opened
    assert not [p for p in opened if "auth" in os.path.basename(p) or "token" in os.path.basename(p)]
    assert all(p.endswith(".jsonl") or os.path.basename(p).startswith("quota.json") for p in opened)
