"""The person's MCP servers stay theirs under brindle: brindle's own writes to
Claude Code's state file never replace one Claude Code just made (a server
added, a sign-in), and a Claude pane keeps the TLS client settings its
company's remote MCP servers ask for."""

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


def _claude_code_writes(config, change, replace=os.replace):
    """A write as Claude Code makes it: under its lock (mkdir of <file>.lock,
    FileExistsError while another writer holds it), a new file renamed over."""
    lock = config.with_name(".claude.json.lock")
    lock.mkdir()
    try:
        state = json.loads(config.read_text())
        change(state)
        staged = config.with_name("claude-code-staged")
        staged.write_text(json.dumps(state))
        replace(staged, config)
    finally:
        lock.rmdir()


def test_claude_code_waits_for_brindles_write_instead_of_losing_its_own(config, tmp_path, monkeypatch):
    """Claude Code goes to add a server and sign in at the last instant, after
    brindle's last look at the file and before its rename."""
    replace, held_off = os.replace, []

    def sign_in_and_add(state):
        state["mcpServers"]["added"] = SERVER
        state["oauthAccount"] = {"emailAddress": "signed-in@example.com"}

    def brindle_renames(src, dst):
        try:
            _claude_code_writes(config, sign_in_and_add, replace)
        except FileExistsError:
            held_off.append(dst)
        replace(src, dst)

    monkeypatch.setattr(os, "replace", brindle_renames)
    assert providers.trust_folder(str(tmp_path / "worktree")) is True
    monkeypatch.setattr(os, "replace", replace)
    if held_off:
        _claude_code_writes(config, sign_in_and_add)     # its turn, on the file brindle wrote

    after = json.loads(config.read_text())
    assert after["mcpServers"] == {"docs": SERVER, "added": SERVER}
    assert after["oauthAccount"] == {"emailAddress": "signed-in@example.com"}
    assert after["projects"] == {**STATE["projects"],
                                 os.path.realpath(tmp_path / "worktree"): {"hasTrustDialogAccepted": True}}


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


@pytest.mark.parametrize("claude_writes, trusted", [
    (2, True),      # a write made without the lock lands between brindle's read and its replace
    (99, False),    # ... and at every attempt: brindle leaves the file to it
])
def test_a_write_made_without_the_lock_while_brindle_updates_the_file_is_kept(
        config, tmp_path, monkeypatch, claude_writes, trusted):
    added = []
    read = providers._read_json

    def read_then_claude_code_adds_a_server(path):
        data = read(path)
        if len(added) < claude_writes:
            now = read(path)
            now["mcpServers"][f"added-{len(added)}"] = SERVER
            staged = config.with_name("claude-code-staged")
            staged.write_text(json.dumps(now))
            os.replace(staged, config)
            added.append(f"added-{len(added)}")
        return data

    monkeypatch.setattr(providers, "_read_json", read_then_claude_code_adds_a_server)

    assert providers.trust_folder(str(tmp_path / "worktree")) is trusted

    after = json.loads(config.read_text())
    assert after["mcpServers"] == {"docs": SERVER, **{name: SERVER for name in added}}
    assert after["oauthAccount"] == STATE["oauthAccount"] and after["projects"]["/repo"] == STATE["projects"]["/repo"]
    assert (after["projects"].get(os.path.realpath(tmp_path / "worktree")) == {"hasTrustDialogAccepted": True}) is trusted
    assert [p.name for p in config.parent.iterdir()] == [".claude.json"]


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
