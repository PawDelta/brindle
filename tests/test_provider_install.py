"""Provider CLIs as the person has them (brindle.providers_install) and
brindle's own config home for each (brindle.provider_homes), through the
daemon's routes: fake binaries and installers on a temporary PATH, a fake
home; nothing real is installed or run."""

import asyncio
import json
import os
import shutil
import threading
import time

import pytest

from brindle import agents, bus, company_identity, company_login, doctor, picker, provider_homes, providers, workspaces
from brindle import providers_install as pi
from brindle.daemon import routes_actions
from brindle.daemon.app import build_app
from brindle.db import Agent

TOKEN = "t" * 43
# conftest stubs these for every test; the sign-in test asks its fake claude for real.
REAL_PROBE, REAL_SIGNED_IN = providers._auth_probe, doctor.signed_in_providers


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


def test_detect_finds_path_and_standard_installs_with_version_and_signin(machine, tmp_path):
    home, bin_dir = machine
    on_path = _exe(bin_dir / "codex", "echo codex-cli 0.5.0")
    standard = _exe(home / "std" / "codex", "echo codex-cli 0.4.0")
    (tmp_path / "codex-home").mkdir()
    (tmp_path / "codex-home" / "auth.json").write_text("{}")    # there: signed in, never read
    row = _row("codex")
    assert (row["state"], row["path"], row["version"], row["signed_in"]) == ("installed", str(on_path), "0.5.0", True)
    assert [i["path"] for i in row["installs"]] == [str(on_path), str(standard)]
    assert providers.codex_binary() == str(on_path)
    assert _row("claude")["state"] == "missing"


def test_use_mine_picks_an_install_and_delete_only_turns_it_off(machine):
    home, bin_dir = machine
    _exe(bin_dir / "codex")
    standard = _exe(home / "std" / "codex")
    elsewhere = _exe(home / "unlisted" / "codex")
    assert _send("POST", "/v1/providers/codex/use-mine", {"path": str(elsewhere)})[0] == 404
    assert _send("POST", "/v1/providers/codex/use-mine", {"path": str(standard)})[0] == 200
    assert providers.codex_binary() == str(standard)
    code, row = _send("DELETE", "/v1/providers/codex")
    assert (code, row["state"]) == (200, "off")
    assert "turned off" in providers.unusable("codex")
    assert standard.exists()            # never uninstalled


@pytest.mark.parametrize("script,ends", [
    ("echo fetching; mkdir -p {bin}; printf '#!/bin/sh\\necho 1.2.3\\n' > {bin}/opencode; "
     "chmod +x {bin}/opencode; echo installed", "job.done"),
    ("echo fetching; exit 3", "job.failed"),
])
def test_install_runs_the_makers_command_streams_its_lines_and_finds_it_again(machine, db, monkeypatch,
                                                                              script, ends):
    home, bin_dir = machine
    monkeypatch.setitem(pi.OFFICIAL, "opencode", script.format(bin=bin_dir))
    code, reply = _send("POST", "/v1/providers/opencode/install")
    assert code == 202
    events = bus.tail(db, kinds=("job.progress", "job.done", "job.failed"))
    assert "fetching" in [e["payload"].get("text") for e in events if e["kind"] == "job.progress"]
    assert events[-1]["kind"] == ends
    if ends == "job.done":
        assert (events[-1]["payload"]["result"]["state"], events[-1]["payload"]["result"]["version"]) == \
            ("installed", "1.2.3")
    else:
        assert events[-1]["payload"]["error"]["code"] == "install_failed"


def test_a_second_install_press_gets_the_running_job(machine, db, monkeypatch, tmp_path):
    home, bin_dir = machine
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    monkeypatch.setitem(pi.OFFICIAL, "codex", f"cat {gate} > /dev/null; "
                        f"printf '#!/bin/sh\\n' > {bin_dir}/codex; chmod +x {bin_dir}/codex")
    threads = []     # the job runs on its own thread, as in the daemon

    def spawn(target):
        threads.append(threading.Thread(target=target, daemon=True))
        threads[-1].start()

    monkeypatch.setattr(routes_actions, "_spawn", spawn)
    first = _send("POST", "/v1/providers/codex/install")
    second = _send("POST", "/v1/providers/codex/install")
    assert first[1]["job"] == second[1]["job"]
    assert len(threads) == 1            # one installer, not two
    with open(gate, "w") as f:
        f.write("go\n")
    threads[0].join(10)
    [ended] = bus.tail(db, kinds=("job.done", "job.failed"))
    assert ended["kind"] == "job.done"


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


def test_the_persons_home_runs_until_a_sign_in_inside_brindle_then_brindles_does(machine, tmp_path, monkeypatch):
    home, bin_dir = machine
    brindle_home = tmp_path / "brindle-claude"
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(brindle_home))
    monkeypatch.setattr(providers, "_auth_probe", REAL_PROBE)
    monkeypatch.setattr(doctor, "signed_in_providers", REAL_SIGNED_IN)
    monkeypatch.setattr(company_login, "which", shutil.which)     # conftest hides claude from identity probes
    company_identity.reset()
    # One identity per home: the person's own (an existing setup), and brindle's once signed in there.
    _exe(bin_dir / "claude",
         f'if [ "$CLAUDE_CONFIG_DIR" != "{brindle_home}" ]; then echo \'{{"loggedIn": true, "orgId": "person-org"}}\'; '
         f'elif [ -f "{brindle_home}/signed" ]; then echo \'{{"loggedIn": true, "orgId": "brindle-org"}}\'; '
         "else echo '{\"loggedIn\": false}'; fi")
    # Nobody signed in inside brindle yet: launches keep the person's working setup.
    assert provider_homes.launch_env("claude") == {}
    assert providers.signed_out("claude") is None
    assert "person-org" in company_identity.claude_identity("org", None).line

    def login(argv, env=None, **kw):        # the CLI's own login, writing where it is pointed
        open(os.path.join(env["CLAUDE_CONFIG_DIR"], "signed"), "w").close()
        return 0, ""

    assert picker.sign_in("claude", run=login, echo=lambda *a, **k: None) is True
    # Signed in inside brindle: from now on its home, and the identity check asks that one.
    assert provider_homes.launch_env("claude") == {"CLAUDE_CONFIG_DIR": str(brindle_home)}
    assert "brindle-org" in company_identity.claude_identity("org", None).line


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


def test_a_turned_off_provider_never_launches(machine, db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    agent = Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "", None, time.time())
    db.add_agent(agent)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: pytest.fail("ran the CLI's model check"))
    assert _send("DELETE", "/v1/providers/claude")[0] == 200
    with pytest.raises(agents.PreflightBlocked) as blocked:
        agents._launch(db, agent, ws, prompt="go", resume=None, watch_pane=False)
    assert [i.id for i in blocked.value.items] == ["claude.off"]
