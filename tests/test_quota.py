import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from brindle import providers, quota, spawn, subscription_usage


def test_antigravity_usage_reaches_settings(monkeypatch):
    from brindle import antigravity, watch_costs
    from brindle.daemon import routes_read

    # Print-mode /usage envelope; no agent turn or real CLI is involved.
    answer = json.dumps({"status": "SUCCESS", "num_turns": 0, "command": {
        "name": "usage", "data": {"groups": [
            {"name": "Gemini Models", "buckets": [
                {"window": "5h", "remaining_fraction": 0.75,
                 "reset_time": "2026-11-16T22:00:00Z"},
                {"window": "weekly", "remaining_fraction": 0.5,
                 "reset_time": "2026-11-22T22:00:00Z"}]},
            {"name": "Claude and GPT models", "buckets": [
                {"window": "weekly", "remaining_fraction": 0.125,
                 "reset_time": "2026-11-22T23:00:00Z"}]}]}}})
    monkeypatch.setattr(quota.time, "time", lambda: 1794859200.0)
    monkeypatch.setattr(quota, "refresh_claude_usage", lambda: False)
    monkeypatch.setattr(quota, "_antigravity_usage_at", 0.0, raising=False)
    monkeypatch.setattr(quota, "_refresh_codex_lazily", lambda: None)
    monkeypatch.setattr(watch_costs, "_org", lambda *args: None)
    monkeypatch.setattr(antigravity, "binary", lambda: "agy")
    runs = []
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: runs.append(argv) or
                        subprocess.CompletedProcess(argv, 0, answer, ""))
    watch_costs.collect(None)
    result = routes_read.get_usage.__wrapped__(None, None)
    agy = next(p for p in result["providers"] if p["provider"] == "antigravity")
    assert agy["known"]
    assert [(w["used"], w["minutes"], w["resets_at"]) for w in agy["windows"]] == [
        (25, 300, 1794866400.0), (50, 10080, 1795384800.0),
        (87.5, 10080, 1795388400.0)]
    assert len({w["name"] for w in agy["windows"]}) == 3
    watch_costs.collect(None)
    assert len(runs) == 1
    # A second process also uses the shared stamp, not just its local clock.
    monkeypatch.setattr(quota, "_antigravity_usage_at", 0.0)
    watch_costs.collect(None)
    assert len(runs) == 1
    # A failed or unrecognizable answer must not erase the last good reading.
    for bad_answer in ('not json', '{}', '{"status":"ERROR"}'):
        answer = bad_answer
        assert not quota.refresh_antigravity_usage(force=True)
        assert len(quota.get("antigravity").windows) == 3


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


def test_claude_usage_refresh_uses_status_line_and_shared_thirty_minute_cadence(tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path))
    monkeypatch.setattr(subscription_usage, "identity", lambda: "test-subscription")
    monkeypatch.setattr(quota, "_claude_usage_identity", None)
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    monkeypatch.setattr(providers, "claude_binary", lambda *a, **k: "claude")
    now = [1_800_000_000.0]
    monkeypatch.setattr(quota.time, "time", lambda: now[0])
    runs = []
    answer = "Current session: 36% used\n"
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: runs.append(argv) or
                        subprocess.CompletedProcess(argv, 0, answer, ""))

    assert quota.record_claude({"rate_limits": {"five_hour": {"used_percentage": 22}}})
    assert not quota.refresh_claude_usage()
    assert runs == []

    # Expire the status-line reading so the shared CLI cadence applies.
    quota.record("claude", [quota.Window(22, now[0] - quota.CLAUDE_FRESH_SECONDS - 1, 300)],
                 source="claude /usage")
    monkeypatch.setattr(quota, "get", lambda *args, **kwargs: None)
    assert quota.refresh_claude_usage()
    assert len(runs) == 1
    now[0] += 10 * 60
    assert not quota.refresh_claude_usage()
    assert len(runs) == 1
    now[0] += 20 * 60
    assert quota.refresh_claude_usage()
    assert len(runs) == 2


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
    assert all(p.endswith(".jsonl") or os.path.basename(p).startswith(("quota.json", "provider_health.json"))
               for p in opened)
