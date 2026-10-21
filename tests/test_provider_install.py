"""Provider CLIs as the person has them (brindle.providers_install) and
brindle's own config home for each (brindle.provider_homes), through the
daemon's routes: fake binaries and installers on a temporary PATH, a fake
home; nothing real is installed or run."""

import asyncio
import json
import os
import threading
import tomllib

import pytest

from brindle import bus, doctor, picker, provider_homes, providers
from brindle import providers_install as pi
from brindle.daemon import routes_actions
from brindle.daemon.app import build_app

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


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_setup_is_copied_once_and_never_a_credential(machine, tmp_path, monkeypatch, provider):
    home, _ = machine
    src = home / (".claude" if provider == "claude" else ".codex")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.delenv("CODEX_HOME")
    brindle_home = tmp_path / f"brindle-{provider}"
    monkeypatch.setenv(f"BRINDLE_{provider.upper()}_CONFIG_HOME", str(brindle_home))
    md = src / ("agents" if provider == "claude" else "prompts")
    md.mkdir(parents=True)
    (md / "review.md").write_text("review the diff")
    (md / ".env").write_text("SECRET=fixture-env")
    secret_file = src / (".credentials.json" if provider == "claude" else "auth.json")
    secret_file.write_text('{"token": "fixture-file"}')
    (md / "notes.md").symlink_to(secret_file)                     # a benign name, a credential behind it
    (md / "linked").symlink_to(src, target_is_directory=True)
    if provider == "claude":
        (src / "settings.json").write_text(json.dumps({
            "model": "opus", "env": {"ANTHROPIC_API_KEY": "fixture-key", "FOO": "1"},
            "statusLine": {"command": "curl -H 'Authorization: Bearer fixture-hdr' x"}}))
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@mail"}, "mcpServers": {
            "ok": {"command": "srv", "args": ["--port", "1"]},
            "flag": {"command": "gh", "args": ["--token", "fixture-arg"]},
            "url": {"url": "https://user:fixture-pass@mcp.example"}}}))
    else:
        (src / "config.toml").write_text(
            'model = "o4"\n'
            'notify = { SAFE = "#", API_KEY = "fixture-inline" }\n'
            '[mcp_servers.ok]\ncommand = "srv"\n'
            '[mcp_servers.gh]\ncommand = "gh"\n[mcp_servers.gh.env]\nTOKEN = """\nfixture-multi\n"""\n')
    env = provider_homes.launch_env(provider)
    assert env == {provider_homes.VARIABLE[provider]: str(brindle_home)}
    copied = {p.relative_to(brindle_home).as_posix(): p.read_text() for p in brindle_home.rglob("*") if p.is_file()}
    assert "review the diff" in copied[f"{md.name}/review.md"]
    assert not any("fixture" in text for text in copied.values())
    if provider == "claude":
        assert json.loads(copied["settings.json"]) == {"model": "opus", "statusLine": {}}
        assert json.loads(copied[".claude.json"]) == {"mcpServers": {"ok": {"command": "srv", "args": ["--port", "1"]}}}
    else:
        assert tomllib.loads(copied["config.toml"]) == {   # parses: valid TOML out
            "model": "o4", "notify": {"SAFE": "#"},
            "mcp_servers": {"ok": {"command": "srv"}, "gh": {"command": "gh"}}}   # env tables gone
    (brindle_home / md.name / "review.md").write_text("brindle's own")
    (md / "later.md").write_text("added later")
    provider_homes.launch_env(provider)                             # a second launch: no copy again
    assert (brindle_home / md.name / "review.md").read_text() == "brindle's own"
    assert not (brindle_home / md.name / "later.md").exists()


def test_sign_in_and_its_check_use_brindles_config_home(machine, tmp_path, monkeypatch):
    home, bin_dir = machine
    brindle_home = tmp_path / "brindle-claude"
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(brindle_home))
    monkeypatch.setattr(providers, "_auth_probe", REAL_PROBE)
    monkeypatch.setattr(doctor, "signed_in_providers", REAL_SIGNED_IN)
    _exe(bin_dir / "claude", 'if [ -f "$CLAUDE_CONFIG_DIR/signed" ]; then echo \'{"loggedIn": true}\'; '
                             'else echo \'{"loggedIn": false}\'; fi')
    assert providers.signed_out("claude") is not None

    def login(argv, env=None, **kw):        # the CLI's own login, writing where it is pointed
        open(os.path.join(env["CLAUDE_CONFIG_DIR"], "signed"), "w").close()
        return 0, ""

    assert picker.sign_in("claude", run=login, echo=lambda *a, **k: None) is True
    assert (brindle_home / "signed").exists()
