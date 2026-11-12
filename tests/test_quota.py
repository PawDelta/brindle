import json
import os
import subprocess
import time

from brindle import providers, quota, spawn


def test_sidebars_share_one_claude_usage_run_per_refresh(monkeypatch):
    """Each sidebar is a process of its own with its own refresh clock; the
    claude CLI that answers /usage starts once per refresh for all of them."""
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
