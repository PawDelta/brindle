import json
import os
import shlex
import subprocess
from urllib.parse import urlencode

from starlette.requests import Request

from brindle import codex_hook, provider_homes, providers, quota, spawn
from brindle.daemon import routes_read
from brindle.profiles import Profile


def test_desktop_codex_usage_only_serves_rollouts_from_current_login(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "codex_home", lambda: str(tmp_path))
    monkeypatch.setattr(quota, "claude_usage_scope", lambda: None)
    now = quota.time.time()
    auth = tmp_path / "auth.json"
    auth.touch()
    os.utime(auth, (now - 600, now - 600))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    workspace = "workspace-a"

    class AppServer:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, params):
            return {"activeSessionId": "saved-login", "sessions": [{
                "sessionId": "saved-login", "isActive": True, "userId": "user", "selectedWorkspaceAccountId": workspace,
            }]}

        def close(self):
            pass

    monkeypatch.setattr(codex_hook, "_AppServer", AppServer)
    monkeypatch.setattr(providers, "codex_binary", lambda: "codex")

    def rate_event(used):
        return {"timestamp": now - 1, "payload": {"rate_limits": {"secondary": {
            "used_percent": used, "window_minutes": 10080, "resets_at": now + 600,
        }}}}

    def rollout(name, started, used, owner):
        path = sessions / f"rollout-{name}.jsonl"
        lines = [
            {"type": "session_meta", "payload": {
                "timestamp": started, "creator_user_id": "user", "creator_account_id": owner,
            }},
            rate_event(used),
        ]
        path.write_text("\n".join(map(json.dumps, lines)) + "\n")
        os.utime(path, (started, started))
        return path

    def request(query):
        return routes_read.get_usage.__wrapped__(None, Request({"type": "http", "query_string": query}))

    def usage():
        data = request(b"fresh=1&claude_scope=none")
        return next(p for p in data["providers"] if p["provider"] == "codex")

    active_rollout = rollout("old", now - 500, 42, workspace)
    first_scope = request(b"scope_only=1")["codex_usage_scope"]
    assert first_scope and usage()["windows"][0]["used"] == 42

    auth.write_text("{}")
    os.utime(auth, (now - 200, now - 200))
    with active_rollout.open("a") as out:
        out.write(json.dumps(rate_event(47)) + "\n")
    assert request(b"scope_only=1")["codex_usage_scope"] == first_scope
    assert usage()["windows"][0]["used"] == 47

    workspace = "workspace-b"
    os.utime(auth, (now - 100, now - 100))
    scope = request(b"scope_only=1")
    assert scope["codex_usage_scope"] not in (None, first_scope)
    assert not scope["providers"]
    assert not usage()["known"]  # Even a new quota event in the old session is excluded.

    rollout("new", now - 50, 12, workspace)
    os.utime(active_rollout, (now, now))
    assert usage()["windows"][0]["used"] == 12


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

    def snapshot(*, fresh=False, scope=None):
        query = {"fresh": "1"} if fresh else {}
        if scope is not None:
            query["claude_scope"] = scope
        request = Request({"type": "http", "query_string": urlencode(query).encode()})
        return routes_read.get_usage.__wrapped__(None, request)

    def served(*, fresh=False, scope=None):
        return next(p for p in snapshot(fresh=fresh, scope=scope)["providers"] if p["provider"] == "claude")

    login("A")
    # A pre-upgrade provider-only cache has no safe subscription attribution.
    quota._save({"claude": {"windows": [{"used": 99}], "updated_at": quota.time.time()}})
    assert not served()["known"]
    assert served(fresh=True)["windows"][0]["used"] == 21
    scope_a = snapshot()["claude_usage_scope"]
    assert scope_a
    login("B")  # Same organization, different subscription owner.
    assert not served()["known"]
    scope_b = snapshot()["claude_usage_scope"]
    assert scope_b not in (None, scope_a)
    used = 78
    assert not served(fresh=True, scope=scope_a)["known"]
    assert served(fresh=True, scope=scope_b)["windows"][0]["used"] == 78
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
    assert snapshot()["claude_usage_scope"] == scope_a
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
