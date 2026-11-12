"""Once brindle's Claude home is in use, the person's own MCP servers come with it."""

import json

from brindle import provider_homes


def test_signed_in_home_gets_the_persons_mcp_servers_without_secrets(tmp_path, monkeypatch):
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
                     "headers": {"X-API-Key": "s3cr3t", "X-Team": "core"}},
            "local": {"type": "stdio", "command": "npx", "args": ["-y", "server"]},
        },
    }))
    mine = {"type": "stdio", "command": "brindle-own"}
    (ours / ".claude.json").write_text(json.dumps({"mcpServers": {"local": mine}}))
    (ours / provider_homes.SIGNED_IN).write_text("1\n")

    provider_homes.launch_env("claude")

    data = json.loads((ours / ".claude.json").read_text())
    assert data["mcpServers"] == {
        "docs": {"type": "http", "url": "https://mcp.example.com", "headers": {"X-Team": "core"}},
        "local": mine,
    }
    assert "oauthAccount" not in data
