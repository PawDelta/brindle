"""Cloud login status readers and the expired-login classifier."""

import hashlib
import json
import os
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from brindle import cloud_login, model_access, spawn

NOW = 1_800_000_000.0


def test_google_token_cache_runs_gcloud_once_until_near_expiry(tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ya29.SECRET\n", stderr="")

    env = {"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "adc.json")}

    def check(at):
        return cloud_login.status("vertex", env=env, home=tmp_path, now=at, run=fake_run,
                                  expiry=lambda token: 3600.0)

    first, second = check(NOW), check(NOW + 60)
    assert first.ok and second.ok and second.expires_at == first.expires_at
    assert "SECRET" not in repr(second) and len(calls) == 1
    check(NOW + 3600 - 100)    # inside the margin before expiry: asks gcloud again
    assert len(calls) == 2

VERTEX_ERROR = ('API Error: Could not load Google Cloud credentials · '
                '{"error":"invalid_grant","error_description":"reauth related error (invalid_rapt)",'
                '"error_subtype":"invalid_rapt"}')


@pytest.mark.parametrize("text", [
    VERTEX_ERROR,
    "An error occurred (ExpiredToken) when calling InvokeModel: The security token included "
    "in the request is expired",
    "AADSTS700082: The refresh token has expired due to inactivity.",
])
def test_expired_logins_are_their_own_cause_and_never_a_refusal(text, monkeypatch, tmp_path):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path))
    assert model_access.expired_login(text) is model_access.EXPIRED_LOGIN
    assert model_access.classify(text) is None
    model_access.refuse("p", model_access.EXPIRED_LOGIN, fp="fp")
    assert not (tmp_path / "model_refusals.json").exists()


def _sso_token(cache, key, expires):
    (cache / (hashlib.sha1(key.encode()).hexdigest() + ".json")).write_text(
        json.dumps({"accessToken": "SECRET", "expiresAt": expires}))


def test_aws_reads_only_the_selected_profiles_sso_session(tmp_path):
    cache = tmp_path / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True)
    (tmp_path / ".aws" / "config").write_text(
        "[profile dev]\nsso_session = dev-sess\n\n[profile old]\nsso_start_url = https://old.example\n")
    _sso_token(cache, "dev-sess", "2027-01-15T08:00:00Z")        # = 1_800_000_000
    _sso_token(cache, "other-sess", "2099-01-01T00:00:00Z")      # unrelated, live
    env = {"AWS_PROFILE": "dev", "AWS_CONFIG_FILE": str(tmp_path / ".aws" / "config")}
    live = cloud_login.status("bedrock", env=env, home=tmp_path, now=NOW - 10)
    assert live.ok and live.expires_at == NOW and "SECRET" not in repr(live)
    assert live.refresh_cmd == "aws sso login --profile dev"
    assert not cloud_login.status("bedrock", env=env, home=tmp_path, now=NOW + 1).ok   # other is live
    assert not cloud_login.status("bedrock", env={**env, "AWS_PROFILE": "old"}, home=tmp_path, now=NOW).ok
    # AWS_DEFAULT_PROFILE selects dev (expired) beside a live default session
    (tmp_path / ".aws" / "config").write_text(
        "[default]\nsso_session = def-sess\n\n[profile dev]\nsso_session = dev-sess\n")
    _sso_token(cache, "def-sess", "2099-01-01T00:00:00Z")
    alt = {"AWS_DEFAULT_PROFILE": "dev", "AWS_CONFIG_FILE": env["AWS_CONFIG_FILE"]}
    s = cloud_login.status("bedrock", env=alt, home=tmp_path, now=NOW + 1)
    assert not s.ok and s.expired and s.refresh_cmd == "aws sso login --profile dev"
    # refresh: any AWS CLI call renews the SSO token from the sso-session's refresh token

    def aws(stderr):
        return lambda cmd, **kw: SimpleNamespace(returncode=255, stdout="", stderr=stderr)

    # offline: the token is past its time but the session never said it lapsed
    offline = aws("Could not connect to the endpoint URL: \"https://portal.sso.us-east-1.amazonaws.com\"")
    assert not cloud_login.refresh("bedrock", env=alt, home=tmp_path, now=NOW + 1, run=offline).expired
    lapsed = aws("Error when retrieving token from sso: Token has expired and refresh failed")
    assert cloud_login.refresh("bedrock", env=alt, home=tmp_path, now=NOW + 1, run=lapsed).expired

    def renew(cmd, **kw):
        assert cmd[:4] == ["aws", "sts", "get-caller-identity", "--profile"] and cmd[4] == "dev"
        _sso_token(cache, "dev-sess", "2099-01-01T00:00:00Z")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    assert cloud_login.refresh("bedrock", env=alt, home=tmp_path, now=NOW + 1, run=renew).ok


AZ_TOKEN = json.dumps({"accessToken": "SECRET", "expiresOn": "2027-01-15 08:00:00.000000",
                       "expires_on": int(NOW), "tokenType": "Bearer"})


@pytest.mark.parametrize("proc, now, ok", [
    (SimpleNamespace(returncode=0, stdout=AZ_TOKEN, stderr=""), NOW - 5, True),
    (SimpleNamespace(returncode=0, stdout=AZ_TOKEN, stderr=""), NOW + 5, False),
    (SimpleNamespace(returncode=1, stdout="", stderr="AADSTS70043: refresh token expired"), NOW, False),
])
def test_azure_expiry_comes_from_the_cli_token(proc, now, ok):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["AZURE_CONFIG_DIR"]))
        return proc

    s = cloud_login.status("foundry", env={"AZURE_CONFIG_DIR": "/prof"}, now=now, run=fake_run)
    assert s.ok is ok and s.refresh_cmd == "az login" and "SECRET" not in repr(s)
    assert calls == [(["az", "account", "get-access-token", "--output", "json"], "/prof")]


@pytest.mark.parametrize("proc, ok, left", [
    (SimpleNamespace(returncode=0, stdout="ya29.SECRET\n", stderr=""), True, 1800.0),
    (SimpleNamespace(returncode=1, stdout="", stderr="invalid_grant: Bad Request invalid_rapt"), False, None),
    (SimpleNamespace(returncode=0, stdout="ya29.SECRET\n", stderr=""), False, 0.0),
])
def test_google_status_refreshes_through_gcloud_in_the_given_env(proc, ok, left, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["GOOGLE_APPLICATION_CREDENTIALS"]))
        return proc

    seen = []

    def fake_expiry(token):
        seen.append(token)
        return left       # an impersonated token: not the usual hour

    env = {"GOOGLE_APPLICATION_CREDENTIALS": "/profile/adc.json"}
    s = cloud_login.status("vertex", env=env, home=tmp_path, now=NOW, run=fake_run, expiry=fake_expiry)
    assert s.ok is ok and calls == [(["gcloud", "auth", "application-default", "print-access-token"],
                                     "/profile/adc.json")]
    assert "SECRET" not in repr(s)
    assert s.expires_at == (NOW + left if left is not None else None)
    assert bool(seen) is (proc.returncode == 0)


@pytest.mark.parametrize("provider, stderr", [
    # after `gcloud auth application-default revoke`
    ("vertex", "ERROR: (gcloud.auth.application-default.print-access-token) "
               "Your default credentials were not found."),
    # after `aws sso logout`: no cached token
    ("bedrock", "Error loading SSO Token: Token for dev-sess does not exist"),
    # after `az logout`
    ("foundry", "ERROR: Please run 'az login' to setup account."),
])
def test_signed_out_never_prompts(provider, stderr, tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[profile dev]\nsso_session = dev-sess\n")
    env = {"AWS_PROFILE": "dev", "AWS_CONFIG_FILE": str(tmp_path / ".aws" / "config"),
           "CLOUDSDK_CONFIG": str(tmp_path / "gcloud"), "AZURE_CONFIG_DIR": str(tmp_path / "azure")}
    failed = SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    def browser(cmd, **kw):
        raise AssertionError(f"opened a sign-in: {cmd}")

    how = cloud_login.sign_in(provider, env=env, home=tmp_path, now=NOW, last_input=NOW,
                              run=lambda cmd, **kw: failed, popen=browser)   # the person is right there
    assert how == "no lapse"


def test_custom_adc_lapse_does_not_open_a_different_login(tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home"))
    env = {"CLAUDE_CODE_USE_VERTEX": "1", "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "adc.json")}
    opened = []
    how = cloud_login.sign_in(env=env, home=tmp_path, now=NOW, last_input=NOW,
        run=lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr=VERTEX_ERROR),
        popen=lambda *a, **kw: opened.append(a) or SimpleNamespace(pid=1))
    assert how == "manual" and opened == []
    assert cloud_login.login_command(env) is None
    assert cloud_login.interactive_sign_in(env=env, run=lambda *a, **kw: opened.append(a)) == 1
    assert opened == []
    for extra in ({"AWS_ACCESS_KEY_ID": "FAKE"}, {"AWS_WEB_IDENTITY_TOKEN_FILE": "/token"}):
        assert cloud_login.configured_status({"CLAUDE_CODE_USE_BEDROCK": "1", **extra}) is None
    assert cloud_login.configured_status({**env, "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES": "1"}) is None


@pytest.mark.parametrize("have, opener", [
    ({"wslview"}, "wslview"),                           # wslu installed: it picks the Windows browser
    ({"explorer.exe", "cmd.exe"}, "explorer.exe"),      # else the Windows PATH's explorer
    ({"cmd.exe"}, "cmd.exe /c start"),
    (set(), None),                                      # nothing to open it with: print the URL
])
def test_wsl_browser_signin_opens_the_windows_browser(have, opener, tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("brindle.providers.is_wsl", lambda: True)
    env = {"CLOUDSDK_CONFIG": str(tmp_path / "gcloud")}     # no DISPLAY / WAYLAND_DISPLAY, as on WSL2
    opened = []
    how = cloud_login.sign_in(
        "vertex", env=env, home=tmp_path, now=NOW, last_input=NOW, platform="linux",
        run=lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr=VERTEX_ERROR),
        which=lambda name: f"/bin/{name}" if name in have else None,
        popen=lambda cmd, **kw: opened.append(kw["env"].get("BROWSER")) or SimpleNamespace(pid=1))
    assert (how, opened) == (("opened", [opener]) if opener else ("manual", []))


def test_google_timeout_is_a_failed_login_not_an_exception(tmp_path):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert not cloud_login.status("vertex", home=tmp_path, run=slow).ok


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_issue189_a_timed_out_gcloud_leaves_no_process_behind(tmp_path, monkeypatch):
    # gcloud is a wrapper script around python: killing only the wrapper on a
    # timeout leaves the python process (and any local auth server) running.
    pidfile = tmp_path / "child.pid"
    gcloud = tmp_path / "bin" / "gcloud"
    gcloud.parent.mkdir()
    gcloud.write_text(f"#!/bin/sh\nsleep 30 >/dev/null 2>&1 &\necho $! > {pidfile}.tmp\n"
                      f"mv {pidfile}.tmp {pidfile}\nwait\n")
    gcloud.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gcloud.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cloud_login, "CLI_TIMEOUT", 2.0)   # room for a loaded machine to start the script
    try:
        s = cloud_login.status("vertex", home=tmp_path)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while _alive(pid) and time.monotonic() < deadline:
            pass    # the group was killed before status returned; init reaps the orphan
        assert not s.ok and not _alive(pid)
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def test_a_cli_probe_that_exits_leaves_nothing_of_its_own_running():
    # A cloud CLI can leave a local auth server behind in its group when it exits.
    out = cloud_login.run_bounded(["sh", "-c", "tail -f /dev/null >/dev/null 2>&1 & echo $!"]).stdout
    pid = int(out)
    try:
        end = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < end:
            pass
        assert not _alive(pid)
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("provider, stopped", [("vertex", False), ("foundry", True), ("bedrock", False)])
def test_under_an_expired_or_stopped_deadline_no_cloud_cli_starts(provider, stopped, tmp_path, monkeypatch):
    # GET provider_connection reaches gcloud, az and aws through cloud_login.status;
    # a request the app gave up on must not start them.
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    env = {"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "adc.json"), "AWS_PROFILE": "dev"}
    with spawn.deadline(30 if stopped else -1) as dl:
        if stopped:
            dl.stop()
        with pytest.raises(spawn.Busy):
            cloud_login.refresh(provider, env=env, home=tmp_path)
    assert started == []


def test_google_token_cache_concurrent_misses_share_one_gcloud_call(tmp_path, monkeypatch):
    import threading
    calls, entered, release, progressed = [], threading.Event(), threading.Event(), threading.Event()
    wait = 10   # a bound so a broken run fails instead of hanging

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            entered.set()
            release.wait(wait)
        else:
            progressed.set()   # without coordination the second caller mints its own token
        return SimpleNamespace(returncode=0, stdout="ya29.SECRET\n", stderr="")

    class WatchedLock:
        """The per-identity lock, reporting when a second caller reaches for it."""
        def __init__(self):
            self.lock, self.entries = threading.Lock(), 0

        def __enter__(self):
            self.entries += 1
            if self.entries == 2:
                progressed.set()   # the second caller missed the cache and is about to wait
            self.lock.acquire()

        def __exit__(self, *exc):
            self.lock.release()

    class WatchedLocks(dict):
        def setdefault(self, key, default=None):
            return super().setdefault(key, WatchedLock())

    monkeypatch.setattr(cloud_login, "_google_locks", WatchedLocks())
    env = {"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "adc.json")}
    results = []

    def check():
        results.append(cloud_login.status("vertex", env=env, home=tmp_path, now=NOW, run=fake_run,
                                          expiry=lambda token: 3600.0))

    first, second = threading.Thread(target=check), threading.Thread(target=check)
    try:
        first.start()
        assert entered.wait(wait)
        second.start()
        assert progressed.wait(wait)   # the second caller is now blocked on the lock (or past it)
    finally:
        release.set()
        first.join()
        second.join()
    assert len(calls) == 1 and [r.ok for r in results] == [True, True]
