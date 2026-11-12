"""Once brindle's Claude home is in use, the person's own MCP servers come with it."""

import contextlib
import json

from brindle import provider_homes, providers

MINE = {"type": "stdio", "command": "brindle-own"}


def _homes(tmp_path, monkeypatch):
    """The person's ~/.claude.json with two servers (one with secrets), and
    brindle's signed-in home with its own server of one of those names."""
    person = tmp_path / "person"
    person.mkdir()
    ours = tmp_path / "brindle-claude"
    ours.mkdir()
    monkeypatch.setenv("HOME", str(person))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(ours))
    (person / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "someone@example.com"},
        "mcpServers": {
            "docs": {"type": "http", "url": "https://mcp.example.com",
                     "headers": {"X-API-Key": "s3cr3t", "X-Team": "core"},
                     "config": {"accessToken": "synthetic", "region": "eu"}},
            "local": {"type": "stdio", "command": "npx", "args": ["-y", "server"]},
            "agent": {"type": "stdio", "command": "ssh-mcp", "env": {"SSH_AUTH_SOCK": "agent.sock"}},
        },
    }))
    (ours / provider_homes.SIGNED_IN).write_text("1\n")
    return ours


def test_signed_in_home_gets_the_persons_mcp_servers_without_secrets(tmp_path, monkeypatch):
    ours = _homes(tmp_path, monkeypatch)
    (ours / ".claude.json").write_text(json.dumps({"mcpServers": {"local": MINE}}))

    provider_homes.launch_env("claude")

    data = json.loads((ours / ".claude.json").read_text())
    assert data["mcpServers"] == {
        "docs": {"type": "http", "url": "https://mcp.example.com", "headers": {"X-Team": "core"},
                 "config": {"region": "eu"}},
        "agent": {"type": "stdio", "command": "ssh-mcp", "env": {"SSH_AUTH_SOCK": "agent.sock"}},
        "local": MINE,
    }
    assert "oauthAccount" not in data


def test_a_linked_home_config_is_never_written_through(tmp_path, monkeypatch):
    ours = _homes(tmp_path, monkeypatch)
    elsewhere = tmp_path / "unrelated.json"
    elsewhere.write_text('{"mcpServers": {}}')
    (ours / ".claude.json").symlink_to(elsewhere)

    provider_homes.launch_env("claude")

    assert elsewhere.read_text() == '{"mcpServers": {}}'


def test_a_copy_finished_while_waiting_for_the_lock_is_not_repeated(tmp_path, monkeypatch):
    ours = _homes(tmp_path, monkeypatch)
    before = json.dumps({"mcpServers": {"local": MINE}})
    (ours / ".claude.json").write_text(before)

    @contextlib.contextmanager
    def other_launch_finished_first():
        (ours / provider_homes.MCP_MARKER).write_text("1\n")
        yield

    monkeypatch.setattr(providers, "_config_lock", other_launch_finished_first)

    assert provider_homes.copy_mcp_servers(ours) is False
    assert (ours / ".claude.json").read_text() == before
