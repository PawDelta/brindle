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


@pytest.mark.parametrize("claude_writes, trusted", [
    (2, True),      # Claude Code's write lands between brindle's read and its replace
    (99, False),    # ... and at every attempt: brindle leaves the file to it
])
def test_a_write_claude_code_makes_while_brindle_updates_its_config_is_kept(
        tmp_path, monkeypatch, claude_writes, trusted):
    home = tmp_path / "claude-config"
    home.mkdir()
    config = home / ".claude.json"
    state = {"oauthAccount": {"emailAddress": "person@example.com"},
             "mcpServers": {"docs": SERVER},
             "projects": {"/repo": {"enabledMcpjsonServers": ["tracker"], "mcpServers": {"local": SERVER}}}}
    config.write_text(json.dumps(state))
    added = []
    read = providers._read_json

    def read_then_claude_code_adds_a_server(path):
        data = read(path)
        if len(added) < claude_writes:
            now = read(path)
            now["mcpServers"][f"added-{len(added)}"] = SERVER
            staged = home / "claude-code-staged"
            staged.write_text(json.dumps(now))
            os.replace(staged, config)      # as Claude Code writes it: a new file, renamed over
            added.append(f"added-{len(added)}")
        return data

    monkeypatch.setattr(providers, "_read_json", read_then_claude_code_adds_a_server)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert providers.trust_folder(str(worktree)) is trusted

    after = json.loads(config.read_text())
    assert after["mcpServers"] == {"docs": SERVER, **{name: SERVER for name in added}}
    assert after["oauthAccount"] == state["oauthAccount"] and after["projects"]["/repo"] == state["projects"]["/repo"]
    assert (after["projects"].get(os.path.realpath(worktree)) == {"hasTrustDialogAccepted": True}) is trusted
    assert [p.name for p in home.iterdir()] == [".claude.json"]


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
