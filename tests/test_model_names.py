import pytest

from brindle import learning, model_names, profiles
from brindle.pro import learning as pro_learning


def test_learner_history_under_an_old_id_is_recorded_under_the_renamed_profile():
    key = pro_learning.OrgKey("org_1", "k1", b"0" * 32)
    task = learning.TaskInfo(repo_root="/repo", task="fix it", profile="developer-codex")
    body = pro_learning.record_payload(key, "id", task, learning.Outcome("merged"))
    assert body["profile"] == "dev.gpt"


def test_every_builtin_profile_has_a_display_name():
    for name in profiles._builtin_names("brindle.builtin_agents"):
        p = profiles.load_profile(name)
        shown = model_names.describe(p)
        assert set(shown) == {"display", "maker", "tool", "effort"}, name
        assert shown["display"] and shown["maker"] and shown["tool"], name
        assert "default" not in shown["display"].lower(), name
        if p.model:
            assert p.model not in shown["display"], name   # a raw id like gpt-6.1-sol or qwen3-coder:30b
        assert shown["effort"] == p.effort, name


def test_full_claude_ids_resolve_to_their_model():
    # An env override or managed route gives a full id, not the alias.
    assert model_names.model_name("eu.anthropic.claude-opus-x-v1:0", "claude") == model_names.MODELS["opus"]
    assert model_names.model_name("claude-sonnet-5-5[1m]", "claude") == model_names.MODELS["sonnet"]


@pytest.mark.parametrize("name, reviewer", [
    ("review.gpt", True),
    ("reviewer-codex", True),
    ("dev.gpt", False),
])
def test_reviewer_ids_are_recognised_in_both_spellings(name, reviewer):
    assert model_names.is_reviewer_id(name) is reviewer


def test_an_org_allowlist_written_with_old_ids_still_allows_the_renamed_profiles():
    from brindle.pro.team_policy import Access

    assert Access(allowed_profiles=("developer-codex",)).allows_profile("dev.gpt", "codex", None)
    assert not Access(allowed_profiles=("dev.claude-sonnet",)).allows_profile("dev.gpt", "codex", None)


def test_handover_from_an_old_predecessor_id_is_found():
    from brindle import cascade
    from test_cascade import _table

    raw = _table(50)
    raw["handover"] = {"developer-heavy>developer-codex": {"p": 0.99, "runs": 10}}
    assert cascade.parse(raw).handover_p("developer-heavy", "dev.gpt") == 0.99


def test_a_custom_profile_under_an_old_id_is_not_allowed_by_the_renamed_entry(tmp_path):
    from brindle.pro.team_policy import Access

    repo = tmp_path / "proj"
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "developer-codex.md").write_text(
        "---\nname: developer-codex\nprovider: codex\n---\nDo it.\n")
    acc = Access(allowed_profiles=("developer-codex",), repo_root=str(repo))
    assert acc.allows_profile("developer-codex", "codex", None)
    assert not acc.allows_profile("dev.gpt", "codex", None)

