"""One-click provider setup (brindle.providers_install) through the daemon's
routes, against a fake download server: fixture archives and manifests on a
local HTTP server, never a real download."""

import asyncio
import hashlib
import io
import json
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brindle import antigravity, bus, opencode, providers
from brindle import providers_install as pi
from brindle.daemon import routes_actions
from brindle.daemon.app import build_app

TOKEN = "t" * 43


def _send(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    raw = json.dumps(body or {}).encode()
    path, _, query = path.partition("?")
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json")]}
    asyncio.run(build_app(TOKEN)(scope, receive, send))
    return sent[0]["status"], json.loads(b"".join(m.get("body", b"") for m in sent
                                                  if m["type"] == "http.response.body"))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        gate = self.server.gates.get(self.path)
        if gate is not None:
            gate.wait(10)
        data = self.server.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    """The makers' sources, served from 127.0.0.1."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.files, srv.gates = {}, {}
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(pi, "CLAUDE_BASE", srv.url + "/claude")
    monkeypatch.setattr(pi, "GITHUB_API", srv.url + "/api")
    monkeypatch.setattr(pi, "GITHUB", srv.url)
    monkeypatch.setattr(pi, "AGY_BASE", srv.url + "/agy")
    monkeypatch.setattr(pi, "AGY_DOWNLOADS", (srv.url + "/agy-dl/",))
    # gpg isn't on every machine: the manifest check is gpg's, so it is faked here.
    monkeypatch.setattr(pi, "claude_signature", lambda manifest, sig, staging: True)
    # Don't run the person's real CLIs for their version.
    monkeypatch.setattr(pi, "probe_version", lambda path: None)
    yield srv
    srv.shutdown()


def _archive(kind: str, name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w") as z:
            info = zipfile.ZipInfo(f"bin/{name}")
            info.external_attr = 0o100755 << 16
            z.writestr(info, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def publish(srv, provider: str, version: str, *, corrupt: bool = False, sums: bool = False) -> bytes:
    """Put ``provider``'s ``version`` on the fake source as its latest. Returns
    the executable's content. ``corrupt``: the published checksum is wrong.
    ``sums``: Codex's checksum only in the release's SHA256SUMS."""
    system, arch, musl = pi._platform()
    exe = f"#!/bin/sh\necho {provider} {version}\n".encode()
    f = srv.files
    if provider == "claude":
        plat = f"{system}-{arch}" + ("-musl" if musl else "")
        sha = hashlib.sha256(b"x" if corrupt else exe).hexdigest()
        f["/claude/latest"] = version.encode()
        f[f"/claude/{version}/manifest.json"] = json.dumps({"platforms": {plat: {"checksum": sha}}}).encode()
        f[f"/claude/{version}/manifest.json.sig"] = b"signature"
        f[f"/claude/{version}/{plat}/claude"] = exe
        return exe
    if provider == "antigravity":
        plat = f"{system}_{'arm64' if arch == 'arm64' else 'amd64'}" + ("_musl" if musl else "")
        blob = _archive("tar.gz", "agy", exe)
        f[f"/agy-dl/{version}/agy.tar.gz"] = blob
        f[f"/agy/manifests/{plat}.json"] = json.dumps({
            "version": version, "url": f"{srv.url}/agy-dl/{version}/agy.tar.gz",
            "sha512": hashlib.sha512(b"x" if corrupt else blob).hexdigest()}).encode()
        return exe
    if provider == "codex":
        repo, tag, name = pi.CODEX_REPO, f"rust-v{version}", f"codex-{pi._CODEX_TARGETS[(system, arch)]}.tar.gz"
        blob = _archive("tar.gz", name.removesuffix(".tar.gz"), exe)
    else:
        repo, tag = pi.OPENCODE_REPO, f"v{version}"
        kind = "zip" if system == "darwin" else "tar.gz"
        name = f"opencode-{system}-{arch}" + ("-musl" if musl else "") + f".{kind}"
        blob = _archive(kind, "opencode", exe)
    sha = hashlib.sha256(b"x" if corrupt else blob).hexdigest()
    download = f"/{repo}/releases/download/{tag}"
    assets = [{"name": name, "browser_download_url": srv.url + f"{download}/{name}",
               **({} if sums else {"digest": f"sha256:{sha}"})}]
    f[f"{download}/{name}"] = blob
    if sums:
        assets.append({"name": "codex-package_SHA256SUMS",
                       "browser_download_url": srv.url + f"{download}/codex-package_SHA256SUMS"})
        f[f"{download}/codex-package_SHA256SUMS"] = f"{'0' * 64}  other.tar.gz\n{sha}  {name}\n".encode()
    rel = {"tag_name": tag, "draft": False, "prerelease": False, "assets": assets}
    f[f"/api/repos/{repo}/releases?per_page=30"] = json.dumps([rel]).encode()
    f[f"/api/repos/{repo}/releases/tags/{tag}"] = json.dumps(rel).encode()
    return exe


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr(routes_actions, "_spawn", lambda target: target())


def _installed(provider: str) -> dict:
    code, rows = _send("GET", "/v1/providers/installed")
    assert code == 200
    return next(r for r in rows if r["provider"] == provider)


BINARY = {"claude": providers.claude_binary, "codex": providers.codex_binary, "opencode": opencode.binary,
          "antigravity": antigravity.binary}


@pytest.mark.parametrize("provider,sums,home_env", [
    ("claude", False, "CLAUDE_CONFIG_DIR"),
    ("codex", False, "CODEX_HOME"),
    pytest.param("codex", True, "CODEX_HOME", marks=pytest.mark.variant("checksum only in SHA256SUMS")),
    ("opencode", False, "OPENCODE_CONFIG_DIR"),
    ("antigravity", False, None),     # no relocation: it shares ~/.gemini
])
def test_install_verifies_unpacks_and_launches_brindles_copy(server, inline, db, provider, sums, home_env):
    exe = publish(server, provider, "1.2.3", sums=sums)
    code, reply = _send("POST", f"/v1/providers/{provider}/install")
    assert code == 202
    events = bus.tail(db, kinds=("job.progress", "job.done", "job.failed"))
    assert events[-1]["kind"] == "job.done", events[-1]
    stages = {e["payload"]["stage"] for e in events if e["kind"] == "job.progress"}
    assert stages == {"check_version", "download", "verify", "unpack"}
    row = _installed(provider)
    assert (row["state"], row["version"]) == ("brindle", "1.2.3")
    launched = BINARY[provider]()
    assert launched.startswith(str(pi.root() / provider / "1.2.3"))
    with open(launched, "rb") as f:
        assert f.read() == exe
    env = pi.launch_env(provider)
    assert env.get(home_env) == (str(pi.root() / provider / "home") if home_env else None)


def test_checksum_mismatch_is_refused_and_leaves_nothing(server, inline, db):
    publish(server, "opencode", "1.2.3", corrupt=True)
    code, _ = _send("POST", "/v1/providers/opencode/install")
    assert code == 202
    [ended] = bus.tail(db, kinds=("job.done", "job.failed"))
    assert ended["kind"] == "job.failed"
    assert ended["payload"]["error"]["code"] == "install_failed"
    assert not (pi.root() / "opencode").exists()
    assert _installed("opencode")["state"] != "brindle"


def test_update_replaces_the_version_and_keeps_brindles_settings(server, inline, db, tmp_path):
    publish(server, "claude", "1.0.0")
    _send("POST", "/v1/providers/claude/install")
    settings = pi.root() / "claude" / "home" / "settings.json"
    settings.write_text('{"model": "changed in brindle"}')
    publish(server, "claude", "1.1.0")
    assert _send("GET", "/v1/providers/installed?check=1")[1][0]["update"]["available"] is True
    code, _ = _send("POST", "/v1/providers/claude/update")
    assert code == 202
    done = [e for e in bus.tail(db, kinds=("job.done", "job.failed"))]
    assert done[-1]["payload"]["result"]["updated"] is True
    row = _installed("claude")
    assert (row["version"], row["update"]["available"]) == ("1.1.0", False)
    assert not (pi.root() / "claude" / "1.0.0").exists()
    assert settings.read_text() == '{"model": "changed in brindle"}'


def test_use_mine_runs_the_persons_copy_and_delete_removes_brindles(server, inline, tmp_path):
    publish(server, "codex", "1.2.3")
    _send("POST", "/v1/providers/codex/install")
    mine = tmp_path / "mine" / "codex"
    mine.parent.mkdir()
    mine.write_text("#!/bin/sh\n")
    mine.chmod(0o755)
    code, reply = _send("POST", "/v1/providers/codex/use-mine", {"path": str(mine)})
    assert code == 200
    assert (_installed("codex")["state"], providers.codex_binary()) == ("mine", str(mine))
    assert pi.launch_env("codex") == {}     # the person's copy keeps its own config home
    code, reply = _send("DELETE", "/v1/providers/codex")
    assert code == 200
    assert not (pi.root() / "codex" / "1.2.3").exists()
    assert (pi.root() / "codex" / "home").is_dir()      # its sign-in, untouched


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_settings_copy_takes_setup_and_never_credentials(provider, tmp_path, monkeypatch):
    src = tmp_path / ("claude-config" if provider == "claude" else "codex-home")   # conftest's config dirs
    (src / ("agents" if provider == "claude" else "prompts")).mkdir(parents=True)
    (src / ("agents" if provider == "claude" else "prompts") / "review.md").write_text("review")
    if provider == "claude":
        (src / "settings.json").write_text(json.dumps({"model": "opus", "env": {"ANTHROPIC_API_KEY": "sk-1",
                                                                                 "FOO": "1"}}))
        (src / ".credentials.json").write_text('{"token": "secret"}')
        (src / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@b"},
            "mcpServers": {"gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp-1"}}}}))
    else:
        (src / "config.toml").write_text('model = "o4"\n[mcp_servers.gh]\ncommand = "gh-mcp"\n'
                                         'env = { "GITHUB_TOKEN" = "ghp-1" }\n')
        (src / "auth.json").write_text('{"tokens": "secret"}')
    assert pi.prepare_home(provider) is True
    home = pi.home(provider)
    copied = "".join(p.read_text() for p in home.rglob("*") if p.is_file())
    assert "review" in copied
    for secret in ("sk-1", "secret", "ghp-1", "a@b"):
        assert secret not in copied
    if provider == "claude":
        assert json.loads((home / "settings.json").read_text()) == {"model": "opus", "env": {"FOO": "1"}}
        assert json.loads((home / ".claude.json").read_text()) == {"mcpServers": {"gh": {"command": "gh-mcp",
                                                                                         "env": {}}}}
    else:
        assert (home / "config.toml").read_text() == 'model = "o4"\n[mcp_servers.gh]\ncommand = "gh-mcp"\n'
    assert pi.prepare_home(provider) is False        # once: brindle's copy is its own after that


def test_a_second_press_gets_the_running_install(server, db, monkeypatch):
    exe_path = f"/claude/1.2.3/{'-'.join(pi._platform()[:2])}{'-musl' if pi._platform()[2] else ''}/claude"
    server.gates[exe_path] = gate = threading.Event()
    publish(server, "claude", "1.2.3")
    threads = []     # the jobs run on their own threads, as in the daemon

    def spawn(target):
        threads.append(threading.Thread(target=target, daemon=True))
        threads[-1].start()

    monkeypatch.setattr(routes_actions, "_spawn", spawn)
    first = _send("POST", "/v1/providers/claude/install")
    second = _send("POST", "/v1/providers/claude/update")
    assert first[0] == second[0] == 202
    assert first[1]["job"] == second[1]["job"]
    assert _send("DELETE", "/v1/providers/claude")[0] == 409
    assert len(threads) == 1        # one download, not two
    gate.set()
    threads[0].join(10)
    [ended] = bus.tail(db, kinds=("job.done", "job.failed"))
    assert ended["kind"] == "job.done"
    assert pi.running("claude") is None
