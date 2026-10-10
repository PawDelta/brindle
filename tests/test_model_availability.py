"""All provider protocols are mocked; these tests never contact a model provider."""

import io
import json
import subprocess
import sys
import time
import urllib.error
from dataclasses import replace
from unittest.mock import Mock

import pytest

from brindle import model_availability as ma
from brindle.profiles import Profile


def profile(provider="native", model="wanted", **kw):
    return Profile("test", "", provider, "", model=model, **kw)


def checker(catalog, **kw):
    discover = Mock(return_value=catalog)
    return ma.ModelAvailabilityChecker(discover=discover, **kw), discover


@pytest.mark.parametrize("model", [None, "", " ", "default"])
@pytest.mark.parametrize("provider", ["native", "codex", "antigravity", "claude"])
def test_default_never_discovers_or_rejects(provider, model):
    check, discover = checker(None)
    result = check.check(profile(provider, model), effective_env={})
    assert result.status == "unverified"
    assert "default" in result.explanation
    discover.assert_not_called()


@pytest.mark.parametrize("provider", ["claude", "shell", "subagent", "unknown"])
@pytest.mark.parametrize("env", [{}, {"CLAUDE_CODE_USE_BEDROCK": "1"},
                                 {"CLAUDE_CODE_USE_VERTEX": "1"},
                                 {"CLAUDE_CODE_USE_FOUNDRY": "1"}])
def test_unsupported_providers_never_call_claude_or_discovery(provider, env):
    check, discover = checker(None)
    assert check.check(profile(provider), effective_env=env).status == "unverified"
    discover.assert_not_called()


@pytest.mark.parametrize("complete,authoritative,status", [
    (True, True, "unavailable"), (True, False, "unverified"),
    (False, True, "unverified"), (False, False, "unverified"),
])
def test_absence_requires_complete_authoritative_catalog(complete, authoritative, status):
    check, _ = checker(ma.Catalog(frozenset(), "openai", complete, authoritative))
    result = check.check(profile(), effective_env={})
    assert result.status == status
    assert "model" in result.explanation


def test_present_model_is_catalog_available_even_on_partial_page():
    check, _ = checker(ma.Catalog(frozenset({"wanted"}), "openai", complete=False))
    result = check.check(profile(), effective_env={})
    assert result.status == "available"
    assert "not verified" in result.explanation


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("models,notices,explanation", [
    (set(), set(), "not found"),
    ({"wanted"}, set(), "bundled or cached"),
    ({"wanted"}, {"wanted"}, "availability notice"),
])
def test_codex_membership_and_notices_do_not_prove_access(complete, models, notices, explanation):
    catalog = ma.Catalog(frozenset(models), "codex", complete=complete,
                         availability_notices=frozenset(notices))
    check, _ = checker(catalog)
    result = check.check(profile("codex"), effective_env={})
    assert result.status == "unverified"
    assert explanation in result.explanation


@pytest.mark.parametrize("selected,listed,status", [
    ("qwen", "qwen:latest", "available"),
    ("qwen", "qwen:7b", "unavailable"),
    ("qwen:7b", "qwen:latest", "unavailable"),
    ("org/qwen", "org/qwen:latest", "available"),
    ("qwen", "other-qwen:latest", "unavailable"),
])
def test_ollama_only_normalizes_omitted_latest_tag(selected, listed, status):
    check, _ = checker(ma.Catalog(frozenset({listed}), "ollama", authoritative=True))
    result = check.check(profile(model=selected), effective_env={})
    assert result.status == status
    if status == "unavailable":
        assert "Pull" in result.explanation


def test_effective_environment_is_complete_and_not_remerged(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret")
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), "openai"), timeout=2)
    env = {"HOME": str(tmp_path), "CUSTOM_KEY": "resolved-secret"}
    p = profile(env={"OPENAI_API_KEY": "profile-secret"}, api_key_env="CUSTOM_KEY")
    check.check(p, effective_env=env, cwd=str(tmp_path))
    request, timeout = discover.call_args.args
    assert request.env == env
    assert request.env is not env
    assert request.cwd == str(tmp_path)
    assert timeout == 2
    assert "secret" not in repr(request)


@pytest.mark.parametrize("kw", [{"api_key_env": "CLAUDE_CODE_OAUTH_TOKEN"},
                              {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "secret"}}])
def test_native_subscription_credentials_follow_existing_auth_guard(kw):
    check, discover = checker(None)
    result = check.check(profile(**kw), effective_env={"CLAUDE_CODE_OAUTH_TOKEN": "secret"})
    assert result.status == "unverified"
    assert "API key" in result.explanation
    discover.assert_not_called()


def test_cache_expires_is_bounded_and_can_be_cleared():
    now = [10.0]
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), "openai"), clock=lambda: now[0])
    for _ in range(2):
        check.check(profile(), effective_env={"KEY": "secret"})
    assert discover.call_count == 1
    now[0] += ma.CACHE_TTL
    check.check(profile(), effective_env={"KEY": "secret"})
    assert discover.call_count == 2
    check.clear()
    check.check(profile(), effective_env={"KEY": "secret"})
    assert discover.call_count == 3
    for i in range(140):
        check.check(profile(), effective_env={"KEY": str(i)})
    assert len(check._cache) == 128
    assert all(isinstance(k, bytes) and len(k) == 32 for k in check._cache)
    assert "secret" not in repr(check._cache)


@pytest.mark.parametrize("change", [
    {"env": {"KEY": "other"}}, {"env": {"KEY": "secret", "AWS_REGION": "elsewhere"}},
    {"env": {"KEY": "secret", "HOME": "/other"}}, {"account": "other-account"},
    {"cwd": "/other"}, {"profile": {"model": "other"}},
    {"profile": {"provider": "codex"}}, {"profile": {"base_url": "https://other/v1"}},
    {"profile": {"api": "anthropic"}}, {"profile": {"api_key_env": "OTHER_KEY"}},
])
def test_cache_isolated_by_effective_identity_and_selection(change):
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), "openai"))
    p = profile()
    check.check(p, effective_env={"KEY": "secret"}, cwd="/one", account_identity="account")
    check.check(replace(p, **change.get("profile", {})),
                effective_env=change.get("env", {"KEY": "secret"}),
                cwd=change.get("cwd", "/one"), account_identity=change.get("account", "account"))
    assert discover.call_count == 2


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_cli_cache_requires_explicit_login_revision(provider):
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), provider))
    for _ in range(2):
        check.check(profile(provider), effective_env={})
    assert discover.call_count == 2
    for _ in range(2):
        check.check(profile(provider), effective_env={}, account_identity="login-v1")
    assert discover.call_count == 3
    check.check(profile(provider), effective_env={}, account_identity="login-v2")
    assert discover.call_count == 4


@pytest.mark.parametrize("error", [ValueError("secret-token"), OSError("secret-token"),
                                 ma._DiscoveryError("secret-token"),
                                 TimeoutError("secret-token"),
                                 subprocess.TimeoutExpired(["secret-token"], 1)])
def test_errors_are_unverified_and_do_not_disclose_details(error):
    check = ma.ModelAvailabilityChecker(discover=Mock(side_effect=error))
    result = check.check(profile(model="secret-model", base_url="https://secret-url"), effective_env={})
    assert result.status == "unverified"
    assert "secret" not in repr(result)
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        assert "timed out" in result.explanation


@pytest.mark.parametrize("kw", [{"timeout": 0}, {"timeout": 31}, {"timeout": float("nan")},
                              {"ttl": -1}, {"ttl": 61}, {"ttl": float("inf")}])
def test_budget_validation(kw):
    with pytest.raises(ValueError):
        ma.ModelAvailabilityChecker(**kw)


class FakeProcess:
    def __init__(self, output, returncode=0):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.returncode = returncode
        self.killed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def kill(self):
        self.killed = True

    def wait(self, **kw):
        return self.returncode

    def poll(self):
        return self.returncode


def rpc_process(monkeypatch, messages):
    from brindle import providers

    monkeypatch.setattr(providers, "codex_binary", lambda: "/mock/codex")
    proc = FakeProcess(b"\n".join(json.dumps(m).encode() for m in messages) + b"\n")
    popen = Mock(return_value=proc)
    monkeypatch.setattr(ma.subprocess, "Popen", popen)
    return proc, popen


def test_codex_handshake_hidden_models_notifications_and_pagination(monkeypatch):
    proc, popen = rpc_process(monkeypatch, [
        {"id": 1, "result": {}}, {"method": "notice", "params": {}},
        {"id": 2, "result": {"data": [{"id": "display-id", "model": "one",
                                        "availabilityNux": {"message": "secret provider prose"}}],
                              "nextCursor": "next"}},
        {"id": 3, "result": {"data": [{"model": "wanted", "hidden": True}], "nextCursor": None}},
    ])
    catalog = ma._codex_catalog()
    assert catalog.models == {"one", "wanted"}
    assert catalog.availability_notices == {"one"}
    assert catalog.complete and not catalog.authoritative
    assert ma._selected("missing", catalog).status == "unverified"
    assert ma._selected("wanted", catalog).status == "unverified"
    assert "availability notice" in ma._selected("one", catalog).explanation
    assert "secret" not in repr(catalog)
    assert "secret" not in repr(ma._selected("one", catalog))
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert [m["method"] for m in sent] == ["initialize", "initialized", "model/list", "model/list"]
    assert sent[0]["params"]["clientInfo"]["name"] == "brindle_model_discovery"
    assert sent[2]["params"]["includeHidden"] is True
    assert sent[3]["params"]["cursor"] == "next"
    assert popen.call_args.args[0] == ["/mock/codex", "app-server"]
    assert proc.killed


@pytest.mark.parametrize("reply", [
    {"id": 1, "error": {"message": "secret"}},
    {"id": 1, "result": {}},  # EOF waiting for model/list
])
def test_codex_errors_and_eof_clean_up(monkeypatch, reply):
    proc, _ = rpc_process(monkeypatch, [reply])
    with pytest.raises(ma._DiscoveryError):
        ma._codex_catalog()
    assert proc.killed


def test_codex_page_limit_preserves_only_positive_evidence(monkeypatch):
    monkeypatch.setattr(ma, "MAX_PAGES", 2)
    proc, _ = rpc_process(monkeypatch, [{"id": 1, "result": {}}] + [
        {"id": i + 2, "result": {"data": [{"model": "wanted"}], "nextCursor": str(i)}}
        for i in range(2)
    ])
    catalog = ma._codex_catalog()
    assert catalog.models == {"wanted"}
    assert not catalog.complete
    assert proc.killed


def test_codex_repeated_cursor_and_oversized_output_are_not_trusted(monkeypatch):
    messages = [{"id": 1, "result": {}}] + [
        {"id": i + 2, "result": {"data": [], "nextCursor": "same"}}
        for i in range(2)
    ]
    proc, _ = rpc_process(monkeypatch, messages)
    with pytest.raises(ma._DiscoveryError):
        ma._codex_catalog()
    assert proc.killed
    monkeypatch.setattr(ma, "MAX_BYTES", 4)
    proc, _ = rpc_process(monkeypatch, messages)
    with pytest.raises(ma._DiscoveryError):
        ma._codex_catalog()
    assert proc.killed


@pytest.mark.parametrize("output,models", [
    ('{"models": [{"id": "gemini-test"}]}', {"gemini-test"}),
    ('[{"id": "gemini-test"}]', {"gemini-test"}),
    ("Available models:\n  gemini-test  Gemini Test\n  claude-test  Claude Test\n", {"gemini-test", "claude-test"}),
    ("\x1b[32m│ gemini-test │ Gemini Test\x1b[0m\n", {"gemini-test"}),
    ("Model ID: gemini-test\n", {"gemini-test"}),
])
def test_antigravity_formats_are_advisory(output, models):
    catalog = ma._parse_antigravity(output)
    assert catalog.models == models
    assert not catalog.authoritative


@pytest.mark.parametrize("output", ["Please sign in", "", "Usage: agy models", '{"error":"secret"}',
                                   '{"models":[{}]}'])
def test_antigravity_unrecognized_output_is_not_empty_catalog(output):
    with pytest.raises(ma._DiscoveryError):
        ma._parse_antigravity(output)


def test_antigravity_invokes_only_models(monkeypatch):
    from brindle import antigravity

    monkeypatch.setattr(antigravity, "binary", lambda: "/mock/agy")
    popen = Mock(return_value=FakeProcess(b"gemini-test  Gemini\n"))
    monkeypatch.setattr(ma.subprocess, "Popen", popen)
    assert ma._antigravity_catalog().models == {"gemini-test"}
    assert popen.call_args.args[0] == ["/mock/agy", "models"]
    assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("code,limit", [(1, 100), (0, 4)])
def test_antigravity_nonzero_exit_or_large_output_is_not_trusted(monkeypatch, code, limit):
    from brindle import antigravity

    monkeypatch.setattr(antigravity, "binary", lambda: "/mock/agy")
    monkeypatch.setattr(ma, "MAX_BYTES", limit)
    monkeypatch.setattr(ma.subprocess, "Popen", Mock(return_value=FakeProcess(b"gemini-test\n", code)))
    with pytest.raises(ma._DiscoveryError):
        ma._antigravity_catalog()


def test_ollama_native_tags_uses_configured_server_and_never_pulls():
    get = Mock(return_value={"models": [{"name": "wanted:latest"}]})
    request = ma.Request("native", "http://localhost:11434/v1", env={"OLLAMA_HOST": "ignored"})
    catalog = ma._native_catalog(request, 2, get=get)
    assert get.call_args.args == ("http://localhost:11434/api/tags", {"Accept": "application/json"}, 2)
    assert catalog.source == "ollama" and catalog.authoritative
    assert ma._selected("wanted", catalog).status == "available"


@pytest.mark.parametrize("api,base,url,source", [
    ("openai", "https://api.openai.com/v1", "https://api.openai.com/v1/models", "openai"),
    ("openai", "https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/models", "compatible"),
    ("openai", "http://localhost:1234/v1/", "http://localhost:1234/v1/models", "compatible"),
    ("anthropic", "https://api.anthropic.com", "https://api.anthropic.com/v1/models", "anthropic"),
    ("anthropic", "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/models", "anthropic"),
])
def test_native_api_catalogs_and_credentials_are_mocked(api, base, url, source):
    get = Mock(return_value={"data": [{"id": "wanted"}]})
    catalog = ma._native_catalog(ma.Request("native", base, api, "KEY", {"KEY": "secret"}), 1, get=get)
    assert catalog.models == {"wanted"}
    assert catalog.source == source
    assert catalog.authoritative == (source != "compatible")
    called_url, headers, timeout = get.call_args.args
    assert called_url == url and timeout == 1
    if api == "anthropic":
        assert headers["x-api-key"] == "secret"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers
    else:
        assert headers["Authorization"] == "Bearer secret"
        assert "x-api-key" not in headers


@pytest.mark.parametrize("api", ["openai", "anthropic"])
@pytest.mark.parametrize("port", [1234, 11434])
@pytest.mark.parametrize("host", [
    "api.openai.com", "api.anthropic.com", "gateway.example.com", "localhost.example.com",
    "192.168.1.2", "0.0.0.0", "[2001:db8::1]", "[::]",
])
def test_external_plain_http_catalog_never_sends_credentials(api, port, host):
    get = Mock(return_value={"data": []})
    request = ma.Request("native", f"http://{host}:{port}/v1", api, "KEY", {"KEY": "secret"})
    with pytest.raises(ma._DiscoveryError, match="^unsupported$"):
        ma._native_catalog(request, 1, get=get)
    get.assert_not_called()


@pytest.mark.parametrize("api", ["openai", "anthropic"])
@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "127.0.0.1", "127.42.0.2", "[::1]", "[0:0:0:0:0:0:0:1]",
])
def test_loopback_plain_http_catalog_accepts_credentials(api, host):
    get = Mock(return_value={"data": [{"id": "wanted"}]})
    base = f"http://{host}:1234/v1"
    request = ma.Request("native", base, api, "KEY", {"KEY": "secret"})
    assert ma._native_catalog(request, 1, get=get).models == {"wanted"}
    url, headers, timeout = get.call_args.args
    assert url == base + "/models" and timeout == 1
    if api == "anthropic":
        assert headers["x-api-key"] == "secret"
    else:
        assert headers["Authorization"] == "Bearer secret"


def test_mocked_anthropic_pagination_is_bounded_and_cursor_is_encoded(monkeypatch):
    get = Mock(side_effect=[
        {"data": [{"id": "one"}], "has_more": True, "last_id": "one&secret"},
        {"data": [{"id": "two"}], "has_more": False},
    ])
    request = ma.Request("native", "https://api.anthropic.com", "anthropic")
    catalog = ma._native_catalog(request, 1, get=get)
    assert catalog.models == {"one", "two"} and catalog.complete
    assert get.call_args.args[0].endswith("?after_id=one%26secret")
    monkeypatch.setattr(ma, "MAX_PAGES", 1)
    get = Mock(return_value={"data": [{"id": "one"}], "has_more": True, "last_id": "one"})
    assert not ma._native_catalog(request, 1, get=get).complete
    assert get.call_count == 1


@pytest.mark.parametrize("data", [{}, {"data": None}, {"data": [None]}, {"data": [{"id": 123}]},
                                 {"data": [], "has_more": "yes"}])
def test_malformed_catalog_does_not_imply_absence(data):
    get = Mock(return_value=data)
    with pytest.raises(ma._DiscoveryError):
        ma._native_catalog(ma.Request("native", "https://api.openai.com/v1"), 1, get=get)


@pytest.mark.parametrize("pagination", [{"has_more": True}, {"nextCursor": "more"},
                                      {"next_page_token": "more"}, {"next": "more"}])
def test_unknown_pagination_does_not_imply_absence(pagination):
    get = Mock(return_value={"data": [], **pagination})
    catalog = ma._native_catalog(ma.Request("native", "https://api.openai.com/v1"), 1, get=get)
    assert ma._selected("wanted", catalog).status == "unverified"


@pytest.mark.parametrize("base,api,key_env", [
    (None, None, None), ("file:///secret", None, None), ("https://u:p@example.com", None, None),
    ("https://example.com?key=secret", None, None), ("https://example.com", "bedrock", None),
    ("https://example.com", None, "MISSING_KEY"),
    ("http://example.com/v1", None, None),
])
def test_unsupported_or_missing_native_configuration_never_sends(base, api, key_env):
    get = Mock()
    with pytest.raises(ma._DiscoveryError):
        ma._native_catalog(ma.Request("native", base, api, key_env), 1, get=get)
    get.assert_not_called()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", ["https://elsewhere/models", "http://elsewhere/models"])
def test_http_is_get_only_and_refuses_redirects(monkeypatch, code, target):
    opener = Mock()
    response = io.BytesIO(b'{"data": []}')
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(ma.urllib.request, "build_opener", build)
    assert ma._get_json("https://example.com/models", {"Authorization": "secret"}, 2) == {"data": []}
    request = opener.open.call_args.args[0]
    assert request.get_method() == "GET" and request.data is None
    assert opener.open.call_args.kwargs["timeout"] == 2
    redirects = build.call_args.args[0]
    assert redirects.redirect_request(request, None, code, "", {}, target) is None


@pytest.mark.parametrize("code,reason", [(401, "auth"), (403, "auth"), (404, "failed"), (429, "failed"), (500, "failed")])
def test_http_failure_does_not_prove_model_absence(monkeypatch, code, reason):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError("https://secret", code, "secret", {}, None)
    monkeypatch.setattr(ma.urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(ma._DiscoveryError, match=f"^{reason}$"):
        ma._get_json("https://example.com", {}, 1)


def test_http_output_limit(monkeypatch):
    monkeypatch.setattr(ma, "MAX_BYTES", 4)
    opener = Mock()
    opener.open.return_value = io.BytesIO(b"12345")
    monkeypatch.setattr(ma.urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(ma._DiscoveryError):
        ma._get_json("https://example.com", {}, 1)


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_isolation_wall_timeout_kills_mock_child_group(monkeypatch, tmp_path):
    real_popen = subprocess.Popen
    children = []
    kill_group = Mock(wraps=ma.os.killpg)
    monkeypatch.setattr(ma.os, "killpg", kill_group)

    def fake_worker(argv, **kw):
        # A local Python fixture, never a provider. Its descendant deliberately
        # keeps stdout open after the parent exits to exercise group cleanup.
        script = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
        proc = real_popen([sys.executable, "-c", script], **kw)
        children.append(proc)
        return proc

    monkeypatch.setattr(ma.subprocess, "Popen", fake_worker)
    started = time.monotonic()
    check = ma.ModelAvailabilityChecker(timeout=0.2)
    result = check.check(profile(), effective_env={}, cwd=str(tmp_path))
    assert result.status == "unverified" and "timed out" in result.explanation
    assert time.monotonic() - started < 2
    assert children[0].poll() is not None
    kill_group.assert_called_once_with(children[0].pid, ma.signal.SIGKILL)


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_isolated_helper_passes_exact_env_and_safe_payload(monkeypatch, tmp_path):
    real_popen = subprocess.Popen
    seen = []

    def fake_worker(argv, **kw):
        seen.append((argv, kw))
        script = ('import json,os,sys; p=json.load(sys.stdin); '
                  'assert os.environ["KEY"] == "secret"; '
                  'assert "PARENT_KEY" not in os.environ; '
                  'assert p["provider"] == "native"; '
                  'print(json.dumps({"models":["wanted"],"source":"openai",'
                  '"complete":True,"authoritative":True}))')
        return real_popen([sys.executable, "-c", script], **kw)

    monkeypatch.setenv("PARENT_KEY", "secret")
    monkeypatch.setattr(ma.subprocess, "Popen", fake_worker)
    check = ma.ModelAvailabilityChecker()
    assert check.check(profile(), effective_env={"KEY": "secret"}, cwd=str(tmp_path)).status == "available"
    argv, kw = seen[0]
    assert "secret" not in repr(argv)
    assert kw["env"] == {"KEY": "secret"}
    assert kw["start_new_session"] is True
    assert kw["stderr"] == subprocess.DEVNULL


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_isolation_caps_output_from_mock_child(monkeypatch, tmp_path):
    real_popen = subprocess.Popen
    children = []
    monkeypatch.setattr(ma, "MAX_BYTES", 100)

    def fake_worker(argv, **kw):
        proc = real_popen([sys.executable, "-c", "print('x' * 1000)"], **kw)
        children.append(proc)
        return proc

    monkeypatch.setattr(ma.subprocess, "Popen", fake_worker)
    result = ma.ModelAvailabilityChecker().check(profile(), effective_env={}, cwd=str(tmp_path))
    assert result.status == "unverified"
    assert children[0].poll() is not None


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_real_helper_entry_point_with_no_endpoint_cannot_contact_providers(tmp_path):
    result = ma.ModelAvailabilityChecker().check(profile(), effective_env={}, cwd=str(tmp_path))
    assert result.status == "unverified"
    assert "No supported read-only catalog" in result.explanation


def test_unsupported_platform_never_launches(monkeypatch):
    monkeypatch.setattr(ma.sys, "platform", "win32")
    popen = Mock()
    monkeypatch.setattr(ma.subprocess, "Popen", popen)
    result = ma.ModelAvailabilityChecker().check(profile(), effective_env={})
    assert result.status == "unverified" and "macOS or Linux" in result.explanation
    popen.assert_not_called()


def test_worker_serializes_only_safe_errors(monkeypatch):
    args = {"provider": "native", "base_url": "https://secret", "api": None,
            "api_key_env": None, "timeout": 1}
    monkeypatch.setattr(ma.sys, "stdin", Mock(buffer=io.BytesIO(json.dumps(args).encode())))
    out = io.StringIO()
    monkeypatch.setattr(ma.sys, "stdout", out)
    monkeypatch.setattr(ma, "_native_catalog", Mock(side_effect=RuntimeError("secret")))
    ma._worker()
    assert json.loads(out.getvalue()) == {"error": "failed"}


def test_worker_preserves_codex_notice_metadata_without_provider_prose(monkeypatch):
    args = {"provider": "codex", "base_url": None, "api": None,
            "api_key_env": None, "timeout": 1}
    monkeypatch.setattr(ma.sys, "stdin", Mock(buffer=io.BytesIO(json.dumps(args).encode())))
    out = io.StringIO()
    monkeypatch.setattr(ma.sys, "stdout", out)
    catalog = ma.Catalog(frozenset({"wanted"}), "codex", availability_notices=frozenset({"wanted"}))
    monkeypatch.setattr(ma, "_codex_catalog", Mock(return_value=catalog))
    ma._worker()
    assert json.loads(out.getvalue())["availability_notices"] == ["wanted"]


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_isolation_preserves_codex_notice_metadata(monkeypatch, tmp_path):
    real_popen = subprocess.Popen

    def fake_worker(argv, **kw):
        payload = {"models": ["wanted"], "source": "codex", "complete": True,
                   "authoritative": False, "availability_notices": ["wanted"]}
        return real_popen([sys.executable, "-c", f"print({json.dumps(payload)!r})"], **kw)

    monkeypatch.setattr(ma.subprocess, "Popen", fake_worker)
    result = ma.ModelAvailabilityChecker().check(profile("codex"), effective_env={}, cwd=str(tmp_path))
    assert result.status == "unverified"
    assert "availability notice" in result.explanation


def test_provisioning_checks_managed_model_and_exact_pane_environment(db, repo, monkeypatch):
    from brindle import agents, guardrails, keystore, secrets, tmux, workspaces
    from brindle.pro import managed_models

    ws = workspaces.adopt_root(db, str(repo))
    p = profile("codex", env={"OPENAI_API_KEY": "profile-key", "CUSTOM": "profile-value"},
                auth="subscription", env_allow=["CUSTOM"])
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    monkeypatch.setattr(guardrails, "effective", lambda p: p)
    monkeypatch.setattr(secrets, "agent_credentials", lambda *a: frozenset({"OPENAI_API_KEY"}))
    monkeypatch.setattr(keystore, "pane_keys", lambda *a: {"STORED_KEY": "stored"})
    monkeypatch.setattr(tmux, "inherited_env", lambda *a: {
        "HOME": "/effective-home", "CODEX_HOME": "/effective-account",
        "OPENAI_API_KEY": "server-key", "CUSTOM": "server-value", "DENIED": "no"})
    monkeypatch.setattr(managed_models, "refusal", lambda *a: None)
    monkeypatch.setattr(managed_models, "apply_profile", lambda m, p: replace(p, model="managed-model"))
    monkeypatch.setattr(managed_models, "agent_env", lambda *a: {"CUSTOM": "managed-value"})
    monkeypatch.setattr(managed_models, "org_key_env", lambda *a: {"OPENAI_API_KEY": "org-key"})
    m = managed_models.Managed("org", None, True)
    seen = []

    def discover(request, timeout):
        seen.append(request)
        return ma.Catalog(frozenset({"managed-model"}), "codex")

    check = Mock(wraps=ma.ModelAvailabilityChecker(discover=discover))
    monkeypatch.setattr(agents, "_model_checker", check)
    assert agents.model_problem("test", ws, m=m) is None
    request = seen[0]
    assert check.check.call_args.args[0].model == "managed-model"
    assert request.cwd == ws.path
    assert request.env["CUSTOM"] == "managed-value"
    assert request.env["OPENAI_API_KEY"] == "org-key"  # org exemptions survive subscription/deny
    assert request.env["CODEX_HOME"] == "/effective-account"
    assert "DENIED" not in request.env
    assert request.env["STORED_KEY"] == "stored"


def test_subscription_excludes_personal_keys_from_catalog(db, repo, monkeypatch):
    from brindle import agents, keystore, tmux, workspaces
    p = profile("codex", auth="subscription", env={"OPENAI_API_KEY": "profile-secret"})
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    monkeypatch.setattr(keystore, "pane_keys", lambda *a: {})
    monkeypatch.setattr(tmux, "inherited_env", lambda *a: {"OPENAI_API_KEY": "parent-secret"})
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), "codex"))
    monkeypatch.setattr(agents, "_model_checker", check)
    assert agents.model_problem("test", workspaces.adopt_root(db, str(repo))) is None
    assert "OPENAI_API_KEY" not in discover.call_args.args[0].env


def test_effective_refusal_cooldown_changes_with_model_account_and_expires(db, repo, monkeypatch):
    from brindle import agents, launch_health, model_access, tmux, workspaces
    p = profile("codex")
    ws = workspaces.adopt_root(db, str(repo))
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    env = {"CODEX_HOME": "/account-a"}
    monkeypatch.setattr(agents, "_effective_window_env", lambda *a: env)
    check, discover = checker(ma.Catalog(frozenset({"wanted", "other"}), "codex"))
    monkeypatch.setattr(agents, "_model_checker", check)
    now = [100.0]
    monkeypatch.setattr(model_access.time, "time", lambda: now[0])
    model_access.refuse("test", model_access.LAUNCH_CAPACITY, fp=launch_health.fingerprint(p, env))
    assert "capacity" in agents.model_problem("test", ws)
    discover.assert_not_called()
    env["CODEX_HOME"] = "/account-b"
    assert agents.model_problem("test", ws) is None
    env["CODEX_HOME"] = "/account-a"
    p = replace(p, model="other")
    assert agents.model_problem("test", ws) is None
    p = replace(p, model="wanted")
    now[0] += model_access.REFUSAL_TTL + 1
    assert agents.model_problem("test", ws) is None


def test_login_revision_invalidates_identity_without_reading_auth(tmp_path):
    from brindle import launch_health
    auth = tmp_path / "auth.json"
    env = {"CODEX_HOME": str(tmp_path)}
    before = launch_health.fingerprint(profile("codex"), env)
    auth.write_text("secret")
    assert launch_health.fingerprint(profile("codex"), env) != before
    assert "secret" not in launch_health.account_revision("codex", env)
    assert launch_health.fingerprint(profile("native"), {"BRINDLE_CUSTOM_KEY": "a"}) != \
        launch_health.fingerprint(profile("native"), {"BRINDLE_CUSTOM_KEY": "b"})


def test_pane_key_helper_resolved_once_for_catalog_health_and_launch(db, repo, monkeypatch):
    from brindle import agents, workspaces
    from brindle.db import Agent
    from brindle.pro import managed_models
    ws = workspaces.adopt_root(db, str(repo))
    a = Agent("worker", ws.id, "developer", "claude", "boss", "assign", "starting", "", None, 0)
    key = Mock(return_value={"ANTHROPIC_API_KEY": "org-key"})
    monkeypatch.setattr(managed_models, "org_key_env", key)
    first = agents._window_environment(a, ws, None)
    agents._effective_window_env(a, ws, None)
    assert agents._window_environment(a, ws, None) is first
    key.assert_called_once()


def test_existing_tmux_environment_excludes_launcher_only_credentials_and_account(db, repo, monkeypatch):
    from brindle import agents, tmux, workspaces
    monkeypatch.setenv("OPENAI_API_KEY", "launcher-key")
    monkeypatch.setenv("CODEX_HOME", "/launcher-account")
    snapshots = {("-g",): "HOME=/server-home\nCODEX_HOME=/server-account\n",
                 ("-t", "=brindle_proj_root"): "CUSTOM=session\n"}
    monkeypatch.setattr(tmux, "_tmux", lambda command, *scope, **k:
                        subprocess.CompletedProcess([], 0, snapshots.get(scope, ""), ""))
    env = tmux.inherited_env("brindle_proj_root")
    assert "OPENAI_API_KEY" not in env and env["CODEX_HOME"] == "/server-account"
    p = profile("codex", env={"CUSTOM": "explicit"})
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), "codex"))
    monkeypatch.setattr(agents, "_model_checker", check)
    assert agents.model_problem("test", workspaces.adopt_root(db, str(repo))) is None
    request = discover.call_args.args[0]
    assert "OPENAI_API_KEY" not in request.env and request.env["CODEX_HOME"] == "/server-account"
    assert request.env["CUSTOM"] == "explicit"
    from brindle import launch_health, model_access
    model_access.refuse("test", model_access.LAUNCH_AUTH, repo_root=str(repo),
                        fp=launch_health.fingerprint(p, request.env))
    monkeypatch.setenv("OPENAI_API_KEY", "different-launcher-key")
    monkeypatch.setenv("CODEX_HOME", "/different-launcher-account")
    assert "authentication" in agents.model_problem("test", workspaces.adopt_root(db, str(repo))).lower()


def test_new_tmux_server_environment_uses_launcher_snapshot(monkeypatch):
    from brindle import tmux
    monkeypatch.setenv("OPENAI_API_KEY", "new-server-key")
    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "no server"))
    assert tmux.inherited_env("new-session")["OPENAI_API_KEY"] == "new-server-key"


@pytest.mark.parametrize("change", ["credential", "account", "project", "selector", "explicit_bookkeeping_key"])
def test_provisioning_cache_preserves_catalog_identity(tmp_path, change):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    env = {"HOME": str(tmp_path), "OPENAI_API_KEY": "first", "CUSTOM_ACCOUNT": "one", "BRINDLE_AGENT_ID": "preview"}
    p = profile("codex", api_key_env="BRINDLE_AGENT_ID" if change == "explicit_bookkeeping_key" else "OPENAI_API_KEY")
    check, discover = checker(ma.Catalog(frozenset({"wanted"}), "codex"))
    check.check(p, effective_env=env, cwd=str(first), account_identity="revision-one",
                catalog_context=ma.provisioning_context(p.provider, str(first), env))
    other = {**env, "BRINDLE_AGENT_ID": "worker"}
    revision = "revision-one"
    if change == "credential":
        other["OPENAI_API_KEY"] = "second"
    elif change == "selector":
        other["CUSTOM_ACCOUNT"] = "two"
    elif change == "account":
        revision = "revision-two"
    elif change == "project":
        (second / ".codex").mkdir()
        (second / ".codex" / "config.toml").write_text('model_provider = "other"')
    check.check(p, effective_env=other, cwd=str(second), account_identity=revision,
                catalog_context=ma.provisioning_context(p.provider, str(second), other))
    assert discover.call_count == 2
    assert discover.call_args.args[0].env == other  # normalized cache identity never alters discovery env


def test_custom_cli_catalog_retains_cwd_identity(tmp_path):
    env = {"BRINDLE_CODEX_BIN": "/custom/codex"}
    assert ma.provisioning_context("codex", str(tmp_path / "a"), env) != ma.provisioning_context("codex", str(tmp_path / "b"), env)
