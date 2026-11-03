"""Provider CLIs as the person has them and brindle's own config home for each
(brindle.provider_homes), through the daemon's routes: fake binaries on a
temporary PATH, a fake home; nothing real is installed or run."""

import asyncio
import json
import os
import time

import pytest

from brindle import agents, company_identity, company_login, provider_homes, providers, workspaces
from brindle import providers_install as pi
from brindle.daemon import routes_actions
from brindle.daemon.app import build_app
from brindle.db import Agent

TOKEN = "t" * 43


def _send(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    raw = json.dumps(body or {}).encode()
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json")]}
    asyncio.run(build_app(TOKEN)(scope, receive, send))
    return sent[0]["status"], json.loads(b"".join(m.get("body", b"") for m in sent
                                                  if m["type"] == "http.response.body"))


def _exe(path, body: str = "echo 1.0.0"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake home, a PATH of one temporary bin folder, and each maker's
    standard place moved under the fake home."""
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setattr(pi, "STANDARD", {p: [f"~/std/{pi.CLI[p]}"] for p in pi.PROVIDERS})
    monkeypatch.setattr(routes_actions, "_spawn", lambda target: target())
    return home, bin_dir


def _row(provider: str) -> dict:
    code, rows = _send("GET", "/v1/providers/installed")
    assert code == 200
    return next(r for r in rows if r["provider"] == provider)


@pytest.mark.parametrize("provider,argv", [("claude", ["auth", "logout"]), ("codex", ["logout"])])
def test_provider_logout_runs_the_cli_with_brindles_config_home(machine, tmp_path, monkeypatch, provider, argv):
    _, bin_dir = machine
    exe = _exe(bin_dir / pi.CLI[provider])
    variable = provider_homes.VARIABLE[provider]
    personal, brindles = tmp_path / "personal", tmp_path / "brindles"
    personal.mkdir()
    (personal / "auth.json").write_text("{}")
    monkeypatch.setenv(variable, str(personal))
    monkeypatch.setenv(f"BRINDLE_{provider.upper()}_CONFIG_HOME", str(brindles))
    monkeypatch.setenv(*(("ANTHROPIC_API_KEY", "k") if provider == "claude" else ("OPENAI_API_KEY", "k")))
    runs = []
    monkeypatch.setattr(company_login, "run_cmd", lambda a, *_, env=None, **__: runs.append((a, env)) or (0, ""))
    code, row = _send("POST", f"/v1/providers/{provider}/logout")
    assert (code, row["signed_in"]) == (200, False)
    [(cmd, env)] = runs
    assert cmd == [str(exe), *argv]
    assert env[variable] == str(brindles)
    # brindle keeps launching with its own home; the person's login stays.
    assert _row(provider)["signed_in"] is False
    assert provider_homes.launch_env(provider) == {variable: str(brindles)}
    assert (personal / "auth.json").exists()
    # A re-login that fails or is cancelled leaves brindle on its own home, signed out.
    provider_homes.login_env(provider)
    monkeypatch.setattr(providers, "_auth_probe", lambda *a, **k: None)
    provider_homes._confirmed_at.clear()
    assert _row(provider)["signed_in"] is False
    assert provider_homes.launch_env(provider) == {variable: str(brindles)}


def test_only_instructions_and_markdown_are_copied_once_and_never_through_a_link(machine, tmp_path, monkeypatch):
    home, _ = machine
    src, brindle_home = tmp_path / "dotfiles" / "claude", tmp_path / "brindle-claude"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(brindle_home))
    (src / "commands").mkdir(parents=True)
    (home / ".claude").symlink_to(src, target_is_directory=True)           # a dotfile manager's link: fine
    (src / "CLAUDE.md").write_text("house rules")
    (src / "commands" / "review.md").write_text("review the diff")
    (src / "commands" / "api-token.md").write_text("fixture-named-like-a-credential")
    (src / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "fixture-key"}}')
    (src / ".credentials.json").write_text('{"token": "fixture-file"}')
    (src / "commands" / "notes.md").symlink_to(src / ".credentials.json")    # a linked file
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent.md").write_text("fixture-through-a-link")
    (src / "agents").symlink_to(elsewhere, target_is_directory=True)        # a linked parent folder
    provider_homes.login_env("claude")                                      # brindle's home first used: its sign-in
    copied = sorted(p.relative_to(brindle_home).as_posix() for p in brindle_home.rglob("*") if p.is_file())
    assert copied == [provider_homes.LOGIN_STARTED, provider_homes.MARKER, "CLAUDE.md", "commands/review.md"]
    (brindle_home / "CLAUDE.md").write_text("brindle's own")
    (src / "commands" / "later.md").write_text("added later")
    provider_homes.login_env("claude")                                      # used again: no copy again
    assert (brindle_home / "CLAUDE.md").read_text() == "brindle's own"
    assert not (brindle_home / "commands" / "later.md").exists()


@pytest.mark.variant("a sign-in finishing after spawn must not move a running agent to another config home")
def test_an_agent_keeps_the_home_decided_at_spawn_when_a_sign_in_completes_after(machine, db, repo, tmp_path,
                                                                                 monkeypatch):
    brindle_home = tmp_path / "brindle-claude"
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(brindle_home))
    ws = workspaces.adopt_root(db, str(repo))
    seen = []

    def sign_in_completes(*a, **k):         # spawn has started: the home is decided already
        brindle_home.mkdir(parents=True, exist_ok=True)
        (brindle_home / provider_homes.SIGNED_IN).write_text("now")

    def identity_check(provider, home=None):
        seen.append(("identity", home))
        return None, None, None

    def preflight(*a, **k):
        seen.append(("preflight", provider_homes.launch_env("claude")))
        return []

    class Opened(Exception):
        pass

    def open_window(db, agent, ws, *a, **k):
        seen.append(("pane", agents.agent_env(ws, agent.id, agent).get("CLAUDE_CONFIG_DIR")))
        raise Opened

    monkeypatch.setattr(agents, "model_problem", sign_in_completes)
    monkeypatch.setattr(company_identity, "launch_check", identity_check)
    monkeypatch.setattr(agents, "launch_blockers", preflight)
    monkeypatch.setattr(agents, "_open_window", open_window)
    with pytest.raises(Opened):
        agents.spawn(db, ws, "developer", provider_name="claude")
    assert seen == [("identity", {}), ("preflight", {}), ("pane", None)]     # the person's home, end to end
    # Its stored decision outlives the sign-in: a resume of an agent spawned before it keeps it.
    seen.clear()
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "paused", "", None, time.time(),
                       config_home=""))
    with pytest.raises(Opened):
        agents._launch(db, db.get_agent("w1"), ws, prompt=None, resume=None, watch_pane=False)
    assert seen == [("identity", {}), ("preflight", {}), ("pane", None)]
    assert provider_homes.decide("claude") == str(brindle_home)        # a new agent gets brindle's
