"""The person's MCP servers stay theirs under brindle. brindle writes Claude
Code's state file as seldom as it can and never forces a write: one Claude
Code makes meanwhile (a server added, a sign-in) is kept, and brindle's is
made after it or left for the next launch. And a Claude pane keeps the TLS
client settings its company's remote MCP servers ask for."""

import copy
import json
import os
import time

import pytest

from brindle import agents, providers, tmux
from brindle.db import Agent, Workspace

SERVER = {"type": "http", "url": "https://docs.mcp.example.com/mcp"}
STATE = {"oauthAccount": {"emailAddress": "person@example.com"},
         "mcpServers": {"docs": SERVER},
         "projects": {"/repo": {"enabledMcpjsonServers": ["tracker"], "mcpServers": {"local": SERVER}}}}


@pytest.fixture
def config(tmp_path):
    """Claude Code's state file, in the config home the tests run with."""
    home = tmp_path / "claude-config"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps(STATE))
    (tmp_path / "worktree").mkdir()
    return home / ".claude.json"


class ClaudeCode:
    """Claude Code's side of its state file: what it holds in memory, its
    lock (the directory <file>.lock) and the ways it writes."""

    def __init__(self, config):
        self.config, self.lock = config, config.with_name(".claude.json.lock")
        self.memory, self.waiting = copy.deepcopy(STATE), False

    def _signs_in_and_adds_a_server(self, state):
        state["mcpServers"][f"added-{len(state['mcpServers'])}"] = SERVER
        state["oauthAccount"] = {"emailAddress": "signed-in@example.com"}
        staged = self.config.with_name("claude-code-staged")
        staged.write_text(json.dumps(state))
        os.replace(staged, self.config)
        self.memory = state

    def writes_under_its_lock(self):
        try:
            self.lock.mkdir()
        except FileExistsError:
            self.waiting = True     # held by another writer: it waits its turn
            return
        self._signs_in_and_adds_a_server(json.loads(self.config.read_text()))
        self.lock.rmdir()

    def writes_without_its_lock(self):
        """As it can when it exits: what it holds in memory, over the file."""
        self._signs_in_and_adds_a_server(self.memory)

    def takes_the_lock_over(self):
        """The lease ran out by its clock (brindle was stopped meanwhile): it
        removes the lock, makes its own, writes, and is still at work."""
        self.lock.rmdir()
        self.lock.mkdir()
        self.writes_without_its_lock()


@pytest.mark.parametrize("there, how, when, written", [
    (True, "writes_under_its_lock", "write", True),     # held off by brindle's lock, then works from brindle's file
    (True, "writes_without_its_lock", "write", False),  # the file is no longer the one brindle read: left as it is
    (True, "takes_the_lock_over", "renew", False),      # nor the lock the one brindle made: nothing written, lock left
    (False, "writes_without_its_lock", "write", False),     # no file when brindle looked: none written over the new one
])
def test_a_write_claude_code_makes_while_brindle_is_writing_is_kept(config, tmp_path, monkeypatch, there, how,
                                                                    when, written):
    """Claude Code writes after brindle read the file and changed its copy:
    as brindle goes to write it, or as it renews the lock it holds. brindle's
    write is made after Claude Code's or not at all, and the next launch
    makes it."""
    worktree = tmp_path / "worktree"
    if not there:
        config.unlink()
    claude, real, seen = ClaudeCode(config), {"write": providers._replace_json, "renew": os.utime}, []

    def claude_code_first(point):
        def step(*a, **k):
            if when == point and not seen and (point == "write" or isinstance(a[0], int)):  # an int: the held lock
                seen.append(point)
                getattr(claude, how)()
            return real[point](*a, **k)
        return step

    def brindle_writes():
        try:
            return providers.trust_folder(str(worktree)) if there else bool(providers.seed_ci_config([str(worktree)]))
        except OSError:
            return False

    monkeypatch.setattr(providers, "_replace_json", claude_code_first("write"))
    monkeypatch.setattr(os, "utime", claude_code_first("renew"))
    assert brindle_writes() is written
    if claude.waiting:
        claude.writes_under_its_lock()      # its turn, on the file brindle wrote
    elif how == "takes_the_lock_over":
        claude.lock.rmdir()                 # still there, and Claude Code's to remove

    def state():
        now = json.loads(config.read_text())
        return (now["mcpServers"], now["oauthAccount"], now["projects"].get("/repo"),
                now["projects"].get(os.path.realpath(worktree)) == {"hasTrustDialogAccepted": True})

    kept = (claude.memory["mcpServers"], {"emailAddress": "signed-in@example.com"}, STATE["projects"]["/repo"])
    assert len(kept[0]) == 2 and state() == (*kept, written)
    assert brindle_writes() is True         # the next launch, with nothing in its way
    assert state() == (*kept, True)
    assert [p.name for p in config.parent.iterdir()] == [".claude.json"]


@pytest.mark.parametrize("claude_code, trusted", [
    ("left its lock behind", True),
    ("lets go while brindle waits", True),
    ("keeps its lock", False),
])
def test_brindle_writes_claude_codes_state_only_with_its_lock(config, tmp_path, monkeypatch, claude_code, trusted):
    lock = config.with_name(".claude.json.lock")
    lock.mkdir()
    if claude_code == "left its lock behind":
        os.utime(lock, (0, 0))      # no holder has touched it for longer than one would

    def wait(seconds):
        if claude_code == "lets go while brindle waits":
            lock.rmdir()

    monkeypatch.setattr(providers, "_lock_wait", wait)

    assert providers.trust_folder(str(tmp_path / "worktree")) is trusted

    assert lock.exists() is not trusted
    assert (json.loads(config.read_text()) == STATE) is not trusted


def test_a_worktree_of_a_repo_the_person_trusted_needs_no_write(config, tmp_path):
    """Claude Code reads a linked worktree's trust from its main checkout's
    entry, which it wrote itself when the person trusted the repo."""
    main, worktree = tmp_path / "repo", tmp_path / "worktree"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    config.write_text(json.dumps({**STATE, "projects": {os.path.realpath(main): {"hasTrustDialogAccepted": True}}}))
    before = os.stat(config)

    assert providers.trust_folder(str(worktree)) is True

    assert (os.stat(config).st_ino, os.stat(config).st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("provider, kept", [
    ("claude", True),
    pytest.param("codex", False, marks=pytest.mark.variant(
        "the key's passphrase is Claude Code's: another provider's pane must not get it")),
])
def test_a_claude_pane_keeps_claude_codes_tls_client_settings(db, tmp_path, monkeypatch, provider, kept):
    """The tmux server holds what the person's shell exported for their
    company's gateway and MCP hosts; the pane lost the key and kept the cert."""
    tls = {"CLAUDE_CODE_CLIENT_CERT": "/certs/me.pem", "CLAUDE_CODE_CLIENT_KEY": "/certs/me.key",
           "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "example-passphrase"}
    monkeypatch.setattr(tmux, "inherited_env", lambda *a: {"PATH": os.environ["PATH"], **tls})
    ws = Workspace("w-1", str(tmp_path), "feat", "worktree", "feat", "main", str(tmp_path), None,
                   "brindle-x", time.time())
    db.add_workspace(ws)
    worker = Agent("wk", ws.id, "developer", provider, None, "assign", "starting", "", None, time.time())
    db.add_agent(worker)

    env = agents._effective_window_env(worker, ws)

    assert {name: env.get(name) for name in tls} == (tls if kept else {**dict.fromkeys(tls), "CLAUDE_CODE_CLIENT_CERT": "/certs/me.pem"})
