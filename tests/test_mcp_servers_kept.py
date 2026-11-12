"""The person's MCP servers stay theirs under brindle: brindle's own writes to
Claude Code's state file never replace one Claude Code just made (a server
added, a sign-in), and a Claude pane keeps the TLS client settings its
company's remote MCP servers ask for."""

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
        later = time.time() + 60
        os.utime(self.lock, (later, later))
        self.writes_without_its_lock()


@pytest.mark.parametrize("there, how, renames, written", [
    (True, "writes_under_its_lock", 1, True),       # held off by brindle's lock, then works from brindle's file
    (True, "writes_without_its_lock", 1, True),     # traded back at once; brindle starts over from it
    (True, "writes_without_its_lock", 99, False),   # ... at every rename: the file stays Claude Code's
    (True, "takes_the_lock_over", 1, False),        # brindle lost its lease: it writes nothing more
    (False, "writes_without_its_lock", 1, True),    # no file when brindle looked: created, never written over
])
def test_what_claude_code_writes_at_brindles_rename_is_kept(config, tmp_path, monkeypatch, there, how, renames,
                                                            written):
    """Claude Code writes at the last instant: after brindle's last look at
    the file, right before each of its renames."""
    worktree = tmp_path / "worktree"
    if not there:
        config.unlink()
    claude, rename, seen = ClaudeCode(config), providers._renamex, []

    def claude_code_first(src, dst, *, swap):
        if len(seen) < renames:
            seen.append(dst)
            getattr(claude, how)()
        rename(src, dst, swap=swap)

    monkeypatch.setattr(providers, "_renamex", claude_code_first)
    try:
        done = providers.trust_folder(str(worktree)) if there else bool(providers.seed_ci_config([str(worktree)]))
    except OSError:
        done = False
    assert done is written
    if claude.waiting:
        claude.writes_under_its_lock()      # its turn, on the file brindle wrote

    after = json.loads(config.read_text())
    assert len(after["mcpServers"]) > 1 and after["mcpServers"] == claude.memory["mcpServers"]
    assert after["oauthAccount"] == {"emailAddress": "signed-in@example.com"}
    assert after["projects"]["/repo"] == STATE["projects"]["/repo"]
    assert (after["projects"].get(os.path.realpath(worktree)) == {"hasTrustDialogAccepted": True}) is written
    taken = how == "takes_the_lock_over"    # and that lock is Claude Code's: left alone
    assert sorted(p.name for p in config.parent.iterdir()) == [".claude.json", *([".claude.json.lock"] * taken)]


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
