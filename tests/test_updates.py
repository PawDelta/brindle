import io
import json
from types import SimpleNamespace

import pytest

from brindle import cli, updates


@pytest.mark.parametrize("cached, age, reply, local, disabled, expected, requests", [
    ("0.4.0", 10, None, False, False, "0.4.0", 0),
    ("0.3.8", 86400, "0.4.0", False, False, "0.4.0", 1),
    (None, 0, "0.3.8", False, False, None, 1),
    (None, 0, OSError("offline"), False, False, None, 1),
    ("0.4.0", 10, None, True, False, None, 0),
    ("0.4.0", 10, None, False, True, None, 0),
    ("0.4.0", 86400, "locked", False, False, "0.4.0", 0),
    pytest.param(None, 0, "bad#(shell)", False, False, None, 1,
                 marks=pytest.mark.variant("malformed PyPI data must not reach tmux")),
])
def test_daily_check(brindle_home, monkeypatch, cached, age, reply, local, disabled,
                     expected, requests):
    monkeypatch.setattr(updates, "__version__", "0.3.8")
    monkeypatch.setattr(updates, "distribution", lambda name: SimpleNamespace(
        read_text=lambda name: '{"url": "file:///checkout"}' if local else None))
    monkeypatch.setattr(updates.time, "time", lambda: 100000)
    monkeypatch.delenv("BRINDLE_NO_UPDATE_CHECK", raising=False)
    if disabled:
        monkeypatch.setenv("BRINDLE_NO_UPDATE_CHECK", "")
    path = brindle_home / "update-check.json"
    if cached:
        brindle_home.mkdir()
        path.write_text(json.dumps({"checked_at": 100000 - age, "latest": cached}))
    calls = []

    def fetch(url, timeout):
        assert timeout <= 2
        calls.append(url)
        if isinstance(reply, Exception):
            raise reply
        return io.StringIO(json.dumps({"info": {"version": reply}}))

    monkeypatch.setattr(updates, "urlopen", fetch)
    if reply == "locked":
        def locked(*args):
            raise BlockingIOError("another session is checking")
        monkeypatch.setattr(updates.fcntl, "flock", locked)
    assert updates.newer_version() == expected
    assert updates.newer_version() == expected
    assert bool(updates.notice()) == bool(expected)
    assert len(calls) == requests
    if requests:
        assert json.loads(path.read_text())["checked_at"] == 100000


@pytest.mark.parametrize("opt_in, available, fails, launched", [
    (False, True, False, False),
    (True, False, False, False),
    (True, True, False, True),
    (True, True, True, False),
])
def test_auto_update_config(tmp_path, brindle_home, monkeypatch, opt_in, available, fails, launched):
    brindle_home.mkdir()
    (brindle_home / "config.json").write_text(json.dumps({"auto_update": opt_in}))
    (tmp_path / ".brindle").mkdir()
    monkeypatch.setattr(updates, "newer_version", lambda: "0.4.0" if available else None)
    commands = []

    def spawn(argv, **kwargs):
        if fails:
            raise FileNotFoundError("uv")
        commands.append(argv)
        assert kwargs["start_new_session"]

    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    cli._check_updates(str(tmp_path))
    assert commands == ([["uv", "tool", "upgrade", "brindle"]] if launched else [])
