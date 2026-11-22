from brindle import model_names, profiles


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
