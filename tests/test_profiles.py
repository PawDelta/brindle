import pytest

from brindle.profiles import _frontmatter, _list, load_profile


@pytest.mark.parametrize("raw, expected", [
    (r'''['it\'s', "say \"q\" # x", "\\", "\s", a#b] # note''',
     ["it's", 'say "q" # x', "\\", r"\s", "a#b"]),
    ("'a,b' # note", ["a,b"]),
    ('\n  - "a,b" # note\n', ["a,b"]),
    ("[] # note", None),
])
@pytest.mark.variant("the reader's inline-list grammar read directly, from the learned.md comment-stripping incident; the accept test reaches these lines only through the writer")
def test_frontmatter_list_grammar(raw, expected):
    meta, _ = _frontmatter(f"---\nrules: {raw}\n---\n")
    assert _list(meta["rules"]) == expected


@pytest.mark.variant("path regression: repository add_dirs must expand home paths before resolving relative to the repository")
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
