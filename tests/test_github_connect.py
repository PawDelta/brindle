from __future__ import annotations

import base64
import functools
import http.server
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import sh

from brindle import clone, git, github_connect, providers
from brindle.daemon import routes_actions, routes_read
from brindle.service.errors import ServiceError

TOKEN = "not-a-real-token-just-a-fixture"


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
