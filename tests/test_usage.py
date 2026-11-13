import json
import shlex
import subprocess

from brindle import provider_homes, providers, quota, spawn
from brindle.daemon import routes_read
from brindle.profiles import Profile


def test_served_claude_usage_follows_subscription_switch_and_back(tmp_path, monkeypatch):
    config = tmp_path / ".claude.json"
    monkeypatch.setattr(providers, "claude_global_config", lambda: str(config))
    monkeypatch.setattr(providers, "claude_binary", lambda: "claude")
    monkeypatch.setattr(provider_homes, "launch_env", lambda provider: {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    monkeypatch.setattr(providers, "_own_status_line", lambda project: None)
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    monkeypatch.setattr(quota, "_refresh_codex_lazily", lambda: None)
    monkeypatch.setattr(quota, "_claude_usage_at", 0.0)
    used = 21
    switch_during_fetch = False

    def login(account, org="org"):
        config.write_text(json.dumps({"oauthAccount": {
            "accountUuid": account, "organizationUuid": org}}))

    def fetch(argv, **kwargs):
        reading = used if kwargs.get("env", {}).get("CLAUDE_CONFIG_DIR") == str(tmp_path) else 99
        if switch_during_fetch:
            login("B")
        return subprocess.CompletedProcess(argv, 0, f"Current session: {reading}% used\n", "")

    monkeypatch.setattr(spawn, "read", fetch)

    def served():
        result = routes_read.get_usage.__wrapped__(None, None)
        return next(p for p in result["providers"] if p["provider"] == "claude")

    login("A")
    # A pre-upgrade provider-only cache has no safe subscription attribution.
    quota._save({"claude": {"windows": [{"used": 99}], "updated_at": quota.time.time()}})
    assert not served()["known"]
    assert quota.refresh_claude_usage(force=True)
    assert served()["windows"][0]["used"] == 21
    login("B")  # Same organization, different subscription owner.
    assert not served()["known"]
    used = 78
    assert quota.refresh_claude_usage()  # A's refresh clock must not suppress B's.
    assert served()["windows"][0]["used"] == 78
    # Capture the real launch command's binding, rather than assigning the
    # current config identity to a status line after its parent has switched.
    argv = providers.ClaudeCode().command(providers.LaunchContext(
        "agent", Profile("test", "", "claude", ""), None, chrome_chosen=True))
    settings = json.loads(argv[argv.index("--settings") + 1])
    binding = shlex.split(settings["statusLine"]["command"])[0]
    name, identity = binding.split("=", 1)
    monkeypatch.setenv(name, identity)
    providers.status_line(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 79}}}))
    assert served()["windows"][0]["used"] == 79
    login("A")
    assert served()["windows"][0]["used"] == 21
    used = 25
    assert quota.refresh_claude_usage(force=True)

    # An older CLI still carries B's status-line reading after switching back.
    providers.status_line(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 78}}}))
    assert served()["windows"][0]["used"] == 25
    monkeypatch.delenv("BRINDLE_CLAUDE_USAGE_IDENTITY")
    providers.status_line(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 78}}}))
    assert served()["windows"][0]["used"] == 25

    login("A", "other-org")
    assert not served()["known"]
    login("A")
    switch_during_fetch = True
    assert not quota.refresh_claude_usage(force=True)
    assert served()["windows"][0]["used"] == 79
    login("A")
    assert served()["windows"][0]["used"] == 25

    # A status line captures A, then login changes just before preserving
    # model caps. Both the prior-window read and the write must stay on A.
    quota.record("claude", [quota.Window(25, None, 300), quota.Window(31, None, 10080, "Opus"),
                           quota.Window(99, quota.time.time() - 1, 10080, "Expired")])
    login("B")
    quota.record("claude", [quota.Window(79, None, 300), quota.Window(88, None, 10080, "Opus")])
    login("A")
    get = quota.get

    def switch_before_prior_windows(provider, **kwargs):
        login("B")
        return get(provider, **kwargs)

    with monkeypatch.context() as race:
        race.setattr(quota, "get", switch_before_prior_windows)
        assert quota.record_claude({"rate_limits": {"five_hour": {"used_percentage": 26}}})
    assert [(w["used"], w["minutes"]) for w in served()["windows"]] == [(79, 300), (88, 10080)]
    login("A")
    assert [(w["used"], w["minutes"]) for w in served()["windows"]] == [(26, 300), (31, 10080)]
    config.write_text("{}")
    assert not served()["known"]
