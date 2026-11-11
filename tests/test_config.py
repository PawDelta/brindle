from __future__ import annotations

import pytest

from conftest import sh

from brindle import config, spawn


def _count_shows(monkeypatch) -> list[list[str]]:
    shows: list[list[str]] = []
    real = spawn.read

    def counting(argv, **kw):
        if argv[:2] == ["git", "show"]:
            shows.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(spawn, "read", counting)
    return shows


def test_forget_user_never_shows_a_reader_half_a_config(monkeypatch):
    """Every MCP server reads ~/.brindle/config.json every few seconds. A
    truncate-then-write in forget_user let one read an empty file, which
    raised in the server's visuals watch. A reader looking while the file is
    being written must see the old file whole."""
    import io
    import json

    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keep": 1, "stale": 2}))
    real_open, seen = io.open, []

    def open_watched(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "w" in mode:     # a write has begun: what does a reader see now?
            seen.append(path.read_text())
        return f

    monkeypatch.setattr(io, "open", open_watched)
    assert config.forget_user("stale")
    monkeypatch.undo()
    assert [json.loads(text) for text in seen] == [{"keep": 1, "stale": 2}]
    assert json.loads(path.read_text()) == {"keep": 1}


@pytest.mark.variant("the only assertion that repeated reads of one commit run `git show` once; other tests run these lines without counting git calls")
def test_branch_config_read_is_cached(tmp_path, monkeypatch):
    sh("git init -q -b main && git config user.email a@b.c && git config user.name n", tmp_path)
    (tmp_path / ".brindle").mkdir()
    cfg = tmp_path / ".brindle" / "config.json"
    cfg.write_text('{"checks": ["one"]}')
    sh("git add -f .brindle/config.json && git commit -qm one", tmp_path)
    shows = _count_shows(monkeypatch)

    assert config._read_shared(tmp_path, "main") == {"checks": ["one"]}
    assert config._read_shared(tmp_path, "main") == {"checks": ["one"]}
    assert len(shows) == 1

    cfg.write_text('{"checks": ["two"]}')
    sh("git commit -qam two", tmp_path)
    assert config._read_shared(tmp_path, "main") == {"checks": ["two"]}
    assert len(shows) == 2
