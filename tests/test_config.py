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
