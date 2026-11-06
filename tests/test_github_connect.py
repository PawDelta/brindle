from __future__ import annotations

import asyncio
import base64
import ctypes
import functools
import http.server
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import sh

from brindle import clone, git, github_connect, mac_keychain, providers
from brindle.daemon import auth, holds, peer, routes_actions, routes_read
from brindle.service.errors import ServiceError

TOKEN = "not-a-real-token-just-a-fixture"
ASK = "protocol=https\nhost=github.com\n"
ANSWER = f"username=octo\npassword={TOKEN}\n"


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def github_api(monkeypatch):
    """GitHub's answer to "who is this token": octo for TOKEN, refused for any other."""
    answers = {"expires": None}

    def http(url, token):
        if token != TOKEN:
            return 401, {}, {}
        headers = {"github-authentication-token-expiration": answers["expires"]} if answers["expires"] else {}
        return 200, headers, {"login": "octo"}

    monkeypatch.setattr(github_connect, "_http", http)
    return answers


class _Origin(http.server.SimpleHTTPRequestHandler):
    """A bare repo over git's dumb protocol that wants ``accepts`` as its password, as github.com would."""
    accepts = TOKEN

    def do_GET(self):
        if self.headers.get("Authorization") == "Basic " + base64.b64encode(f"octo:{self.accepts}".encode()).decode():
            return super().do_GET()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="origin"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_connected_github_answers_git_without_the_keychain(brindle_home, tmp_path, monkeypatch, github_api):
    # The person's own helper and askpass stand for the keychain's: a fake that notes being asked.
    asked = tmp_path / "asked"
    asker = _script(tmp_path / "asker", f'echo "$*" >> "{asked}"')
    home_config = tmp_path / "gitconfig"
    home_config.write_text(f"[credential]\n\thelper = !{asker}\n[core]\n\taskPass = {asker}\n"
                           "[user]\n\tname = n\n\temail = a@b.c\n")
    for name, value in (("GIT_CONFIG_GLOBAL", str(home_config)), ("GIT_CONFIG_NOSYSTEM", "1"),
                        ("GIT_ASKPASS", str(asker)), ("GIT_TERMINAL_PROMPT", "0"), ("NO_PROXY", "127.0.0.1")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    private, repo = tmp_path / "private", tmp_path / "repo"
    sh(f"git init -q --bare -b main {private} && git init -q -b main {repo}", tmp_path)
    sh(f"git commit -q --allow-empty -m init && git push -q {private} main", repo)
    sh("git update-server-info", private)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Origin, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{server.server_port}"
    sh(f"git remote add origin http://{host}/private", repo)

    # The local origin plays github.com: the real helper code, in the process git starts, with only its host changed.
    monkeypatch.setattr(github_connect, "ORIGIN", ("http", host))
    monkeypatch.setattr(git, "_GITHUB_HELPER", f"credential.http://{host}.helper")
    helper = _script(tmp_path / "brindle-helper", f"exec {sys.executable} -c 'import sys\n"
                     f'from brindle import github_connect as g\ng.ORIGIN = ("http", "{host}")\n'
                     "sys.exit(g.main(sys.argv[1:]))' \"$@\"")
    monkeypatch.setattr(github_connect, "helper_command", lambda: f"!{helper}")

    assert not git.background_fetch(repo, ["--quiet", "origin", "main"])     # not connected: nobody answers
    github_connect.connect_token(TOKEN)
    try:
        assert git.background_fetch(repo, ["--quiet", "origin", "main"]) and not git.fetch_refused(repo)
        # What the person asks for signs in the same way: a branch they named, and a clone.
        assert git.fetch_remote_branch(repo, "main") == "origin/main"
        clone.clone(f"http://{host}/private", tmp_path / "cloned", lambda line: None)
        # An agent's pane: github.com through brindle's helper alone; another host keeps the person's helpers.
        pane = {**os.environ, "GIT_CONFIG_PARAMETERS": git.pane_git_config(gh=False)}
        filled = git.run_env(["credential", "fill"], repo, pane, input=f"protocol=http\nhost={host}\n\n").stdout
        assert f"password={TOKEN}" in filled
        assert not asked.exists()
        git.run_env(["credential", "fill"], repo, pane, input="protocol=https\nhost=example.com\n\n", check=False)
        assert "get" in asked.read_text()
        assert github_connect.answer("get", "protocol=https\nhost=example.com\n") == ""    # never another host's
        asked.unlink()

        # GitHub stops taking it: git says so once, and no later poll offers it again.
        monkeypatch.setattr(_Origin, "accepts", "replaced-on-github")
        assert not git.background_fetch(repo, ["--quiet", "origin", "main"]) and git.fetch_refused(repo)
        assert github_connect.status()["needs_reconnect"] and git.pane_git_config(gh=False) is None
        assert not git.background_fetch(repo, ["--quiet", "origin", "main"])
        assert not asked.exists()
    finally:
        server.shutdown()


def test_connection_routes_keep_the_token_and_refuse_what_github_wont_take(db, tmp_path, monkeypatch, github_api):
    read = routes_read.github_connection.__wrapped__
    monkeypatch.setattr(git, "gh_credential_helper", lambda: None)      # no gh signed in
    assert read(db, None) == {"connected": False, "login": None, "route": None, "storage": None,
                              "expires_at": None, "needs_reconnect": False, "gh_available": False}
    with pytest.raises(ServiceError) as refused:
        routes_actions.connect_github(db, {}, {"route": "gh"})
    assert refused.value.code == "github_gh_unavailable"

    got = routes_actions.connect_github(db, {}, {"route": "token", "token": f" {TOKEN}\n"})
    assert got == {**read(db, None), "connected": True, "login": "octo", "route": "token", "storage": "file"}
    assert TOKEN not in str(got) and github_connect.credential() == ("octo", TOKEN)
    assert str(github_connect._dir()) in providers._codex_denied_reads()     # Codex's sandbox can't read the store
    # A token GitHub refuses changes nothing, and the error carries no token.
    with pytest.raises(ServiceError) as refused:
        routes_actions.connect_github(db, {}, {"route": "token", "token": "another-fixture-github-refuses"})
    assert refused.value.code == "github_token_invalid" and "another-fixture" not in str(refused.value.to_json())
    assert github_connect.credential() == ("octo", TOKEN)

    # A copy of gh's sign-in, taken from gh when asked; one already past its date isn't offered to git.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "gh", f'[ "$1 $2" = "auth token" ] && echo {TOKEN}')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git, "gh_credential_helper", lambda: "!gh auth git-credential")
    github_api["expires"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() - 60))
    got = routes_actions.connect_github(db, {}, {"route": "gh"})
    assert (got["route"], got["gh_available"], got["needs_reconnect"]) == ("gh", True, True)
    assert got["expires_at"] < time.time() and github_connect.git_helper() is None

    assert routes_actions.disconnect_github(db, {}, {})["connected"] is False
    assert github_connect.credential() is None


class _HelpersItem:
    """The helper app's keychain item, as only that program sees it."""
    secret: bytes | None = None
    refuses: mac_keychain.Refused | None = None

    def load(self):
        if self.refuses:
            raise self.refuses
        return self.secret

    def save(self, secret, ask):
        type(self).secret = secret

    def delete(self):
        type(self).secret = None


def test_under_the_app_only_the_helper_daemon_holds_the_token_and_the_rest_ask_it(db, monkeypatch, github_api):
    is_helper = [True]
    monkeypatch.setattr(github_connect, "is_helper", lambda: is_helper[0])
    monkeypatch.setattr(github_connect, "_item", _HelpersItem)
    monkeypatch.setattr(_HelpersItem, "secret", None)
    got = routes_actions.connect_github(db, {}, {"route": "token", "token": TOKEN})
    assert got["storage"] == "keychain" and _HelpersItem.secret == TOKEN.encode()
    assert "token" not in github_connect._store().load()          # nothing secret on disk
    assert github_connect.answer("get", ASK) == ANSWER            # the daemon answers git itself

    # Any other brindle process (git's helper in a pane, the CLI) holds no token: it passes git's question on.
    def daemon(path, body, seconds):
        assert path == github_connect.CREDENTIAL_PATH
        is_helper[0] = True
        try:
            return routes_actions.github_credential(db, {}, body)
        finally:
            is_helper[0] = False

    monkeypatch.setattr(holds, "call", daemon)
    is_helper[0] = False
    assert github_connect.credential() is None and github_connect.git_helper() is not None
    assert github_connect.answer("get", ASK) == ANSWER
    # A daemon that isn't the helper can't read the item, and doesn't go asking itself.
    assert routes_actions.github_credential(db, {}, {"action": "get", "request": ASK}) == {"answer": ""}

    # That route takes git under an agent, with no token, on the Unix socket; never over TCP.
    reached, sent = [], []

    async def served(scope, receive, send):
        reached.append(scope["path"])

    async def send(message):
        sent.append(message)

    guard = auth.Guard(served, "the-daemons-token")
    scope = {"type": "http", "path": github_connect.CREDENTIAL_PATH, "headers": []}
    asyncio.run(guard({**scope, peer.PEER_PID: 4242}, None, send))
    asyncio.run(guard(scope, None, send))
    assert reached == [github_connect.CREDENTIAL_PATH] and sent[0]["status"] == 403

    # A locked keychain is no answer for now, with nothing to redo; an item no longer the helper's needs a new connect.
    monkeypatch.setattr(_HelpersItem, "refuses", mac_keychain.Refused(-25293, locked=True))
    assert github_connect.answer("get", ASK) == "" and not github_connect.status()["needs_reconnect"]
    monkeypatch.setattr(_HelpersItem, "refuses", mac_keychain.Refused(-25293, locked=False))
    assert github_connect.answer("get", ASK) == "" and github_connect.status()["needs_reconnect"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Security.framework is macOS's")
def test_keychain_item_reads_back_for_its_maker_and_a_locked_keychain_refuses_without_asking(tmp_path):
    # A keychain file of the test's own, never the person's: made here, deleted here.
    sec, password, keychain = mac_keychain.security(), b"a-test-keychains-password", ctypes.c_void_p()
    made = sec.SecKeychainCreate(str(tmp_path / "t.keychain-db").encode(), len(password), password, False, None,
                                 ctypes.byref(keychain))
    assert made == 0
    try:
        item = mac_keychain.Item("brindle-github-test", "connection", keychain)
        assert item.load() is None
        item.save(b"an-earlier-fixture", ask=False)
        item.save(TOKEN.encode(), ask=False)        # replaced, not a second item
        assert item.load() == TOKEN.encode()
        assert sec.SecKeychainLock(keychain) == 0
        for blocked in (item.load, lambda: item.save(b"while-locked", ask=False)):
            with pytest.raises(mac_keychain.Refused) as refused:
                blocked()                           # returns at once: with asking off, no dialog can be up
            assert refused.value.locked
        assert sec.SecKeychainUnlock(keychain, len(password), password, True) == 0
        item.delete()
        assert item.load() is None
    finally:
        sec.SecKeychainDelete(keychain)
