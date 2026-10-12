import json
import os
import time

import pytest

from brindle import quota
from brindle.config import RepoConfig


def limits(**windows):
    """A rollout event line; windows maps slot -> (used, minutes, resets_at)."""
    rl = {slot: {"used_percent": u, "window_minutes": m, "resets_at": r}
          for slot, (u, m, r) in windows.items()}
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rl}})


def rollout(tmp_path, *lines, name="rollout-2026-09-30T10-00-00-abc.jsonl", day="30"):
    d = tmp_path / "codex-home" / "sessions" / "2026" / "09" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_codex_maps_windows_by_length_not_slot(tmp_path):
    future = time.time() + 3600
    # The weekly window sits in "primary" here and the 5-hour one in "secondary".
    rollout(tmp_path, limits(primary=(82.0, 10080, future), secondary=(10.0, 300, future)))
    q = quota.get("codex")
    by = {w.minutes: w.used for w in q.windows}
    assert by == {10080: 82.0, 300: 10.0}
    assert quota.headroom("codex", RepoConfig()) == pytest.approx(18)
    assert "Codex at 82% of its weekly limit, resets" in quota.note("codex")


def test_codex_reads_the_last_event_with_rate_limits(tmp_path):
    future = time.time() + 3600
    rollout(tmp_path,
            limits(primary=(10.0, 300, future)),
            limits(primary=(55.0, 300, future)),
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}}))
    assert quota.get("codex").windows[0].used == 55.0


def test_stale_windows_are_dropped(tmp_path):
    rollout(tmp_path, limits(primary=(95.0, 300, time.time() - 60)))
    assert quota.get("codex") is None
    assert quota.note("codex") is None
    assert quota.headroom("codex", RepoConfig()) == 100


def test_claude_status_line_goes_through_quota():
    future = time.time() + 3600
    assert quota.record_claude({"rate_limits": {
        "five_hour": {"used_percentage": 93, "resets_at": future},
        "seven_day": {"used_percentage": 20, "resets_at": future}}})
    assert quota.headroom("claude", RepoConfig()) == pytest.approx(7)
    assert "Claude at 93% of its 5-hour limit" in quota.note("claude")
    assert not quota.record_claude({"nothing": 1})


def test_claude_data_goes_stale():
    quota.record_claude({"rate_limits": {"five_hour": {"used_percentage": 93, "resets_at": time.time() + 3600}}})
    data = json.loads(quota.path().read_text())
    data["claude"]["updated_at"] -= quota.CLAUDE_FRESH_SECONDS + 5
    quota.path().write_text(json.dumps(data))
    assert quota.get("claude") is None


def test_limited_until_zeroes_headroom_then_expires():
    until = quota.record_limit("antigravity", RepoConfig())
    assert until == pytest.approx(time.time() + 300 * 60, abs=5)
    assert quota.headroom("antigravity", RepoConfig()) == 0
    assert "limit reached" in quota.note("antigravity")
    data = json.loads(quota.path().read_text())
    data["antigravity"]["limited_until"] = time.time() - 1
    quota.path().write_text(json.dumps(data))
    assert quota.get("antigravity") is None
    assert quota.headroom("antigravity", RepoConfig()) == 100


def test_auth_and_token_files_are_never_read(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text('{"tokens": {"access_token": "secret"}}')
    rollout(tmp_path, limits(primary=(50.0, 300, time.time() + 600)))
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
