from __future__ import annotations

import asyncio
import base64
import ctypes
import fcntl
import functools
import http.server
import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import sh

from brindle import clone, git, github_connect, keystore, mac_keychain, providers
from brindle.daemon import auth, holds, peer, routes_actions, routes_read
from brindle.service.errors import ServiceError

TOKEN = "not-a-real-token-just-a-fixture"
OTHER = "a-second-fixture-for-another-login"
ASK = "protocol=https\nhost=github.com\n"
ANSWER = f"username=octo\npassword={TOKEN}\n"
WAIT = 10.0     # seconds a test's thread is given to reach a point: a failure's limit, never a pause


def _connect(token):
    """A connect as the daemon's route does it; ``db`` is not its to use."""
    return routes_actions.connect_github(None, {}, {"route": "token", "token": token})


def _disk_full(*args, **kwargs):
    raise OSError("disk full")


def _fails_to_store(monkeypatch, patched, name, breaking):
    """A connect whose write breaks, with ``patched.name`` as ``breaking``: refused as not stored."""
    with monkeypatch.context() as failing:
        failing.setattr(patched, name, breaking)
        with pytest.raises(ServiceError) as failed:
            _connect(OTHER)
    assert failed.value.code == "not_stored" and OTHER not in str(failed.value.to_json())


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def github_api(monkeypatch):
    """GitHub's answer to "who is this token": octo for TOKEN, hubot for OTHER, refused for any other."""
    answers = {"expires": None}

    def http(url, token):
        if token not in (TOKEN, OTHER):
            return 401, {}, {}
        headers = {"github-authentication-token-expiration": answers["expires"]} if answers["expires"] else {}
        return 200, headers, {"login": "octo" if token == TOKEN else "hubot"}

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
    # The person's own helper and askpass stand for the keychain's and a dialog: a fake that notes being asked.
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

        # GitHub stops taking it: git says so once, and it is on record.
        monkeypatch.setattr(_Origin, "accepts", "replaced-on-github")
        assert not git.background_fetch(repo, ["--quiet", "origin", "main"]) and git.fetch_refused(repo)
        assert github_connect.status()["needs_reconnect"]
        assert not git.background_fetch(repo, ["--quiet", "origin", "main"])

        def nobody_answers():
            """brindle's helper is still the only one for the host, and ends git's asking: what the
            person asks for fails plainly and a new pane gets no password. Neither the keychain's
            helper (or gh's) nor an askpass dialog comes back just when they have to connect again."""
            with pytest.raises(git.GitError):
                git.fetch_remote_branch(repo, "main")
            new_pane = {**os.environ, "GIT_CONFIG_PARAMETERS": git.pane_git_config(gh=False)}
            filled = git.run_env(["credential", "fill"], repo, new_pane, input=f"protocol=http\nhost={host}\n\n",
                                 check=False)
            assert filled.returncode != 0 and "password=" not in filled.stdout and not asked.exists()

        nobody_answers()
        # The same for one that ran out, though GitHub would still take it; a poll puts that on record as a refusal.
        monkeypatch.setattr(_Origin, "accepts", TOKEN)
        github_api["expires"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() - 60))
        github_connect.connect_token(TOKEN)
        nobody_answers()
        git._refused_marker(repo).unlink()
        assert not git.background_fetch(repo, ["--quiet", "origin", "main"]) and git.fetch_refused(repo)
        # Only disconnecting gives github.com back to the person's own helpers.
        github_connect.disconnect()
        with pytest.raises(git.GitError):
            git.fetch_remote_branch(repo, "main")
        assert "get" in asked.read_text()
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
    # So git's helper under that sandbox, to which the folder looks absent, asks the daemon, which reads the file.
    denied, readable = [False], keystore.FileStore.load

    def load(store):
        if denied[0]:
            raise PermissionError("denied by the sandbox")
        return readable(store)

    def daemon(path, body, seconds):
        denied[0] = False
        try:
            return routes_actions.github_credential(db, {}, body)
        finally:
            denied[0] = True

    monkeypatch.setattr(keystore.FileStore, "load", load)
    monkeypatch.setattr(holds, "call", daemon)
    denied[0] = True
    assert github_connect.answer("get", ASK) == ANSWER
    denied[0] = False

    # A connect that can't be written leaves the one before it whole, and nothing half-written behind.
    _fails_to_store(monkeypatch, os, "replace", _disk_full)
    assert github_connect.credential() == ("octo", TOKEN)
    assert not list(github_connect._dir().glob(".*.tmp"))
    # Another connect is mid-write, holding the lock every change takes: git still gets the old one, whole,
    # and then the new one, whole.
    mid_write, go_on, writer, sync = threading.Event(), threading.Event(), [], os.fsync

    def fsync(fd):
        if threading.current_thread() in writer:
            mid_write.set()
            go_on.wait(WAIT)
        return sync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    writer.append(threading.Thread(target=_connect, args=(OTHER,)))
    writer[0].start()
    assert mid_write.wait(WAIT)
    with open(github_connect._dir() / github_connect.LOCK_FILE) as lock, pytest.raises(BlockingIOError):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)        # as another process would find it
    assert github_connect.credential() == ("octo", TOKEN)
    go_on.set()
    writer[0].join(WAIT)
    assert github_connect.credential() == ("hubot", OTHER)
    # Two of the daemon's threads writing one file store at once each keep their own file: neither's write is lost.
    shared, writer[:] = keystore.FileStore(tmp_path / "shared", "record"), []
    mid_write.clear()
    go_on.clear()
    writer.append(threading.Thread(target=shared.save, args=({"from": "the first"},)))
    writer[0].start()
    assert mid_write.wait(WAIT)
    shared.save({"from": "the second"})
    go_on.set()
    writer[0].join(WAIT)
    assert shared.load() == {"from": "the first"} and [p.name for p in shared.dir.iterdir()] == ["record.json"]
    _connect(TOKEN)
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
    assert got["expires_at"] < time.time() and github_connect.answer("get", ASK) == github_connect.QUIT

    assert routes_actions.disconnect_github(db, {}, {})["connected"] is False
    assert github_connect.credential() is None


class _HelpersItem:
    """The helper app's keychain, as only that program sees it: one item per generation of the connection."""
    items: dict[str, bytes] = {}
    refuses: mac_keychain.Refused | None = None
    keeps = False                   # a keychain that won't remove an item now
    staged = staticmethod(lambda secret: None)

    def __init__(self, record):
        self.name = record["generation"]

    def load(self):
        if self.refuses:
            raise self.refuses
        return self.items.get(self.name)

    def save(self, secret, ask):
        if self.refuses:
            raise self.refuses
        self.items[self.name] = secret
        self.staged(secret)

    def delete(self):
        if not self.keeps:
            self.items.pop(self.name, None)
        return self.name not in self.items


def test_under_the_app_only_the_helper_daemon_holds_the_token_and_the_rest_ask_it(db, monkeypatch, github_api,
                                                                                  capsys):
    is_helper, items = [True], {}
    monkeypatch.setattr(github_connect, "is_helper", lambda: is_helper[0])
    monkeypatch.setattr(github_connect, "_item", _HelpersItem)
    monkeypatch.setattr(_HelpersItem, "items", items)
    got = routes_actions.connect_github(db, {}, {"route": "token", "token": TOKEN})
    assert got["storage"] == "keychain" and list(items.values()) == [TOKEN.encode()]
    assert "token" not in github_connect._store().load()          # nothing secret on disk
    assert github_connect.answer("get", ASK) == ANSWER            # the daemon answers git itself

    # Two connects overlap: one has its token in the keychain and isn't committed yet when another goes
    # through whole. Each is a generation of its own, so git gets a login with its own token throughout.
    staged, go_on = threading.Event(), threading.Event()

    def held_up(secret):
        if secret == OTHER.encode():
            staged.set()
            go_on.wait(WAIT)

    monkeypatch.setattr(_HelpersItem, "staged", staticmethod(held_up))
    slow = threading.Thread(target=_connect, args=(OTHER,))
    slow.start()
    assert staged.wait(WAIT)
    _connect(TOKEN)
    assert github_connect.credential() == ("octo", TOKEN)
    go_on.set()
    slow.join(WAIT)
    assert github_connect.credential() == ("hubot", OTHER) and list(items.values()) == [OTHER.encode()]
    _connect(TOKEN)

    # A connect that fails, at the keychain, at the list of its items or at the record, leaves the one before
    # it whole and takes back what it staged: also when it would have moved the token to or from the file.
    locked = mac_keychain.Refused(-25293, locked=True)
    _fails_to_store(monkeypatch, _HelpersItem, "refuses", locked)
    _fails_to_store(monkeypatch, os, "replace", _disk_full)                     # the list: so nothing is staged
    _fails_to_store(monkeypatch, keystore.FileStore, "save", _disk_full)        # the record, with the token staged
    monkeypatch.setenv(github_connect.STORE_ENV, "file")
    _fails_to_store(monkeypatch, os, "replace", _disk_full)
    monkeypatch.delenv(github_connect.STORE_ENV)
    assert github_connect.credential() == ("octo", TOKEN) and list(items.values()) == [TOKEN.encode()]

    # The worst of it: a staged connect can't be published, and then nothing about clearing it away works
    # either. The keychain won't remove the item and the list can't be written. The connection is untouched,
    # and the item is still named on the list, written before the item was made.
    disk_works, write = [True], github_connect._write

    def list_write(name, text):
        if not disk_works[0]:
            raise OSError("disk full")
        write(name, text)

    def disk_fills(secret):
        disk_works[0] = False       # from the moment the item is in the keychain

    monkeypatch.setattr(github_connect, "_write", list_write)
    monkeypatch.setattr(_HelpersItem, "staged", staticmethod(disk_fills))
    monkeypatch.setattr(_HelpersItem, "keeps", True)
    with pytest.raises(ServiceError):
        _connect(OTHER)
    assert github_connect.credential() == ("octo", TOKEN)
    assert sorted(items.values()) == sorted([TOKEN.encode(), OTHER.encode()]) and len(github_connect._items()) == 2
    # Things work again. The next change leaves it be, as it could be a connect still at the keychain's dialog;
    # one after it is plainly abandoned removes it.
    disk_works[0] = True
    monkeypatch.setattr(_HelpersItem, "staged", staticmethod(lambda secret: None))
    monkeypatch.setattr(_HelpersItem, "keeps", False)
    _connect(TOKEN)
    assert sorted(items.values()) == sorted([TOKEN.encode(), OTHER.encode()])
    with monkeypatch.context() as later:
        later.setattr(github_connect, "ABANDONED_SECONDS", 0)
        _connect(TOKEN)
    assert list(items.values()) == [TOKEN.encode()] and list(github_connect._items()) == list(items)

    # A keychain that won't remove the old item when a connect replaces it: listed to go, and the next change does.
    monkeypatch.setenv(github_connect.STORE_ENV, "file")
    monkeypatch.setattr(_HelpersItem, "keeps", True)
    assert _connect(OTHER)["storage"] == "file" and len(items) == 1 and list(github_connect._items().values()) == [0]
    assert github_connect.credential() == ("hubot", OTHER)
    monkeypatch.delenv(github_connect.STORE_ENV)
    monkeypatch.setattr(_HelpersItem, "keeps", False)
    assert _connect(TOKEN)["storage"] == "keychain" and list(items.values()) == [TOKEN.encode()]
    assert list(github_connect._items()) == list(items) and "token" not in github_connect._store().load()

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
    # A daemon that isn't the helper can't read the item, and doesn't go asking itself: git is told to stop
    # asking, as it is when no daemon is there, so no askpass or other helper stands in.
    stop = github_connect.QUIT
    assert routes_actions.github_credential(db, {}, {"action": "get", "request": ASK}) == {"answer": stop}
    with monkeypatch.context() as no_daemon:
        no_daemon.setattr(holds, "call", lambda path, body, seconds: None)
        assert github_connect.answer("get", ASK) == stop and github_connect.answer("store", ASK) == ""

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
    assert github_connect.answer("get", ASK) == stop and not github_connect.status()["needs_reconnect"]
    monkeypatch.setattr(_HelpersItem, "refuses", mac_keychain.Refused(-25293, locked=False))
    assert github_connect.answer("get", ASK) == stop and github_connect.status()["needs_reconnect"]
    # Whatever else goes wrong in the helper git started, git is still told to stop.
    def broken(action, request):
        raise RuntimeError("anything")

    monkeypatch.setattr(github_connect, "answer", broken)
    monkeypatch.setattr(sys, "stdin", io.StringIO(ASK))
    assert github_connect.main(["get"]) == 0 and capsys.readouterr().out == stop


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
        assert item.delete()                        # removing needs no unlocking: a disconnect while locked leaves nothing
        assert sec.SecKeychainUnlock(keychain, len(password), password, True) == 0
        assert item.load() is None and item.delete()
    finally:
        sec.SecKeychainDelete(keychain)
