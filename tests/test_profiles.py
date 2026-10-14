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


def test_doctor_warns_override_drops_allowlist(brindle_home):
    """A ~/.brindle/agents copy of reviewer-codex with no allowed_tools hides the
    built-in's allowlist; a copy that keeps one, and a new name, are fine."""
    from brindle import doctor

    agents = brindle_home / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer-codex.md").write_text("---\nname: reviewer-codex\nprovider: antigravity\n---\nReview.\n")
    (agents / "developer.md").write_text("---\nname: developer\nallowed_tools: Bash(ls:*)\n---\nWork.\n")
    (agents / "my-reviewer.md").write_text("---\nname: my-reviewer\n---\nReview.\n")

    warned = doctor.override_checks()
    assert [(c.level, c.name) for c in warned] == [(doctor.WARN, "profile reviewer-codex")]


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
