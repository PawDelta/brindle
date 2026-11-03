import json
import os
import time

from brindle import quota


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
