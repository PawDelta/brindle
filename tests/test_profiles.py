from brindle.profiles import load_profile


def test_repo_add_dirs_expand_a_leading_tilde(tmp_path, monkeypatch):
    """``~/cache`` means the home directory's cache, not ``<repo>/~/cache``."""

    home = tmp_path / "home"
    (home / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    (repo / ".brindle" / "config.json").write_text('{"add_dirs": ["~/cache"]}')

    assert load_profile("developer", str(repo)).add_dirs == [str(home / "cache")]


def test_an_unknown_user_in_add_dirs_is_reported_not_raised(tmp_path):
    """``~typo/cache`` makes expanduser raise. Kept as written instead, so
    loading the profile (and every launch with it) still works and launch
    reports the entry as missing."""
    from brindle.profiles import missing_add_dirs

    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    (repo / ".brindle" / "config.json").write_text('{"add_dirs": ["~no-such-user-brindle/cache"]}')

    p = load_profile("developer", str(repo))
    assert p.add_dirs == ["~no-such-user-brindle/cache"]
    assert missing_add_dirs(p) == ["~no-such-user-brindle/cache"]
