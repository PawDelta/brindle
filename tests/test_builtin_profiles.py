"""The built-in developer profiles share one prompt and one tool list, and
every worker's footer asks it to check its work against the finish line."""

from brindle.agents import SUBAGENT_FOOTER, WORKER_FOOTER
from brindle.profiles import load_profile


def test_developer_variants_extend_developer():
    base = load_profile("developer")
    for name in ("developer-heavy", "developer-codex"):
        p = load_profile(name)
        assert p.extends == "developer"
        assert p.prompt == base.prompt
        assert p.allowed_tools == base.allowed_tools
    assert load_profile("developer-codex").provider == "codex"
    heavy = load_profile("developer-heavy")
    assert (heavy.provider, heavy.model, heavy.effort) == ("claude", "claude-fable-5-1", "high")


def test_developer_preapproves_lint_and_type_checks():
    tools = load_profile("developer").allowed_tools
    for rule in ("Bash(ruff check:*)", "Bash(mypy:*)", "Bash(tsc:*)", "Bash(eslint:*)",
                 "Bash(diff:*)", "Bash(git switch:*)"):
        assert rule in tools


def test_worker_footers_ask_for_a_test_per_requirement():
    for footer in (WORKER_FOOTER, SUBAGENT_FOOTER):
        assert "finish line" in footer
        assert "test that would fail without your" in footer
        assert "what's left" in footer
