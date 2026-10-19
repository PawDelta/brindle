"""Real stdio framing and process lifecycle, using a tiny language server."""

import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle.native import lsp, runner
from brindle.native.client import ClientError, Reply, ToolCall
from brindle.native.loop import NativeAgent
from brindle.native.permissions import Permissions
from brindle.native.tools import Toolbox, core_tools


SERVER = r'''
import json, os, sys
from pathlib import Path

root = Path.cwd()
log = root / "lsp-events.jsonl"
documents = {}
pull = (root / "pull").exists()

def send(message):
    body = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

def diagnostics(text):
    return [{"range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 0, "character": 1}},
             "severity": 1, "message": "undefined café"}] if "broken" in text else []

while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    msg = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    with log.open("a") as f:
        f.write(json.dumps(msg) + "\n")
    method, params = msg.get("method"), msg.get("params")
    if method == "initialize":
        assert params["rootUri"] == root.as_uri()
        caps = {"textDocumentSync": 2}
        if pull:
            caps["diagnosticProvider"] = {"interFileDependencies": False, "workspaceDiagnostics": False}
        send({"id": "config", "method": "workspace/configuration", "params": {"items": [{}]}})
        send({"id": msg["id"], "result": {"capabilities": caps}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        if (root / "exit-early").exists():
            sys.exit(4)
        if (root / "silent").exists():
            continue
        doc = params["textDocument"]
        uri = doc["uri"]
        if method.endswith("didOpen"):
            text = doc["text"]
        else:
            change = params["contentChanges"][0]
            previous = documents[uri].split("\n")
            assert change["range"]["end"] == {"line": len(previous) - 1,
                "character": len(previous[-1].encode("utf-16-le")) // 2}
            text = change["text"]
            send({"method": "textDocument/publishDiagnostics", "params": {
                "uri": uri, "version": doc["version"] - 1, "diagnostics": diagnostics("broken")}})
        documents[uri] = text
        if not pull:
            send({"method": "textDocument/publishDiagnostics", "params": {
                "uri": uri, "version": doc["version"], "diagnostics": diagnostics(text)}})
    elif method == "textDocument/diagnostic":
        send({"id": msg["id"], "result": {"kind": "full", "items": diagnostics(documents[params["textDocument"]["uri"]])}})
    elif method in ("textDocument/definition", "textDocument/references"):
        assert params["position"] == {"line": 0, "character": 3}
        pos = {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 5}}
        location = {"uri": (root / "target.py").as_uri(), "range": pos}
        result = ([{"targetUri": location["uri"], "targetSelectionRange": pos}]
                  if method.endswith("definition") else [location,
                  {"uri": (root.parent / "secret.py").as_uri(), "range": pos},
                  {"uri": (root / "escape.py").as_uri(), "range": pos}])
        send({"id": msg["id"], "result": result})
    elif method == "shutdown":
        if (root / "silent").exists():
            continue
        send({"id": msg["id"], "result": None})
    elif method == "exit":
        break
'''


@pytest.fixture
def server(tmp_path, monkeypatch):
    script = tmp_path / "fake_server.py"
    script.write_text(SERVER)
    # Exercise discovery's fallback branch: the first executable is absent.
    monkeypatch.setitem(lsp.SERVERS, ".py", ("python", (
        ("brindle-test-no-such-server",), (sys.executable, str(script)))))
    return script


@pytest.mark.parametrize("pull", [False, True])
def test_lazy_sync_navigation_and_shutdown(tmp_path, server, pull):
    if pull:
        (tmp_path / "pull").touch()
    (tmp_path / "escape.py").symlink_to(tmp_path.parent / "secret.py")
    with lsp.LSP(str(tmp_path)) as pool:
        box = Toolbox().add(*core_tools(str(tmp_path), lsp=pool))
        agent = NativeAgent(None, box, Permissions("acceptEdits", []), "")
        assert not pool.clients and not (tmp_path / "lsp-events.jsonl").exists()
        # Permission denial must not start a server or create a file.
        agent.permissions = Permissions("plan", [])
        assert agent._execute(ToolCall("0", "Write", {"path": "a.py", "content": "broken"})).is_error
        assert not pool.clients and not (tmp_path / "a.py").exists()
        agent.permissions = Permissions("acceptEdits", [])
        written = agent._execute(ToolCall("1", "Write", {"path": "a.py", "content": "😀 broken"}))
        assert not written.is_error and "a.py:1:1: error: undefined café" in written.content
        client = next(iter(pool.clients.values()))
        edited = agent._execute(ToolCall("2", "Edit", {
            "path": "a.py", "old_string": "broken", "new_string": "fixed"}))
        assert not edited.is_error and "undefined café" not in edited.content
        assert client.diagnose((tmp_path / "a.py").as_uri()) == []
        for name in ("Definition", "References"):
            result = agent._execute(ToolCall(name, name, {"path": "a.py", "line": 1, "column": 3}))
            assert not result.is_error and result.content == "target.py:3:5"
        assert not agent._execute(ToolCall("3", "Diagnostics", {"path": "a.py"})).is_error
        assert next(iter(pool.clients.values())) is client and len(pool.clients) == 1
    assert client.proc.poll() == 0 and not client.reader.is_alive()
    events = [json.loads(line) for line in (tmp_path / "lsp-events.jsonl").read_text().splitlines()]
    assert [e.get("method") for e in events].count("initialize") == 1
    assert [e.get("method") for e in events][-2:] == ["shutdown", "exit"]


def test_missing_server_and_read_scope(tmp_path, server, monkeypatch):
    (tmp_path / "a.py").write_text("broken")
    with monkeypatch.context() as missing, lsp.LSP(str(tmp_path)) as pool:
        missing.setattr(lsp.shutil, "which", lambda name: None)
        box = Toolbox().add(*core_tools(str(tmp_path), lsp=pool))
        plain = Toolbox().add(*core_tools(str(tmp_path)))
        for path in ("a.md", "a.py"):
            (tmp_path / path).write_text("broken")
            # Compare with ordinary tools: LSP unavailability adds no output.
            assert box.call("Read", {"path": path}) == plain.call("Read", {"path": path})
            args = {"path": path, "content": "broken"}
            assert box.call("Write", args) == plain.call("Write", args)
            args = {"path": path, "old_string": "broken", "new_string": "fixed"}
            result = box.call("Edit", args)
            (tmp_path / path).write_text("broken")
            assert result == plain.call("Edit", args)
        for name, args in (("Diagnostics", {"path": "a.py"}),
                           ("Write", {"path": "new.py", "content": "x"}),
                           ("Diagnostics", {"path": "unknown.xyz"})):
            result = box.call(name, args)
            assert not result.is_error and len(result.content.splitlines()) == 1
        assert not pool.clients
        assert box.call("Diagnostics", {"path": "../secret.py"}).is_error
    # The server would index the whole workspace, even for an in-scope query.
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed" / "a.py").write_text("broken")
    (tmp_path / "allowed" / "link.py").symlink_to(tmp_path / "a.py")
    with lsp.LSP(str(tmp_path), read_scope=["allowed/*"]) as pool:
        box = Toolbox().add(*core_tools(str(tmp_path), read_scope=["allowed/*"], lsp=pool))
        plain = Toolbox().add(*core_tools(str(tmp_path), read_scope=["allowed/*"]))
        assert box.call("Read", {"path": "allowed/a.py"}) == plain.call("Read", {"path": "allowed/a.py"})
        args = {"path": "allowed/a.py", "content": "broken"}
        assert box.call("Write", args) == plain.call("Write", args)
        assert box.call("Diagnostics", {"path": "a.py"}).is_error
        assert box.call("Diagnostics", {"path": "allowed/link.py"}).is_error
        assert not box.call("Diagnostics", {"path": "allowed/a.py"}).is_error
        assert not box.call("Write", {"path": "hidden.py", "content": "x"}).is_error
        assert not pool.clients


@pytest.mark.parametrize("failure", ["silent", "exit-early"])
def test_server_failure_preserves_write_and_reaps_process(tmp_path, server, failure):
    (tmp_path / failure).touch()
    with lsp.LSP(str(tmp_path), timeout=1) as pool:
        box = Toolbox().add(*core_tools(str(tmp_path), lsp=pool))
        result = box.call("Write", {"path": "a.py", "content": "broken"})
        assert not result.is_error and (tmp_path / "a.py").read_text() == "broken"
        client = next(iter(pool.clients.values()))
        if failure == "exit-early":
            client.proc.wait(timeout=3)
            (tmp_path / failure).unlink()
            # Real dispatch must replace a dead connection and receive fresh diagnostics.
            recovered = box.call("Diagnostics", {"path": "a.py"})
            assert not recovered.is_error and "a.py:1:1: error: undefined café" in recovered.content
            assert next(iter(pool.clients.values())) is not client
    assert client.proc.poll() is not None and not client.reader.is_alive()


@pytest.mark.parametrize("stop", ["idle", "error", "signal"])
def test_runner_owns_servers_across_turns(tmp_path, server, monkeypatch, stop):
    (tmp_path / "a.py").write_text("good")
    worker = SimpleNamespace(workspace_id="w", mode="chat")
    workspace = SimpleNamespace(path=str(tmp_path))
    profile = SimpleNamespace(write_scope=None, read_scope=None, permission_mode="plan",
                              allowed_tools=[], prompt="", context_tokens=None)
    pending = [SimpleNamespace(body="read"), SimpleNamespace(body="finish")]
    db = SimpleNamespace(get_agent=lambda _: worker, get_workspace=lambda _: workspace,
                         update_agent=lambda *a, **k: None, set_status=lambda *a: None,
                         pop_pending=lambda _: pending.pop(0) if pending else None)
    monkeypatch.setattr(runner.agents, "_profile_for", lambda *a: profile)
    monkeypatch.setattr(runner, "brindle_home", lambda: tmp_path)
    monkeypatch.setattr(runner, "conversation_path", lambda _: tmp_path / "saved.json")
    monkeypatch.setattr(runner, "endpoint_for", lambda _: SimpleNamespace(model="test", base_url="local"))
    monkeypatch.setattr(runner, "brindle_tools", lambda *a: [])
    monkeypatch.setattr(runner, "_drain", lambda *a: [])
    from brindle import launch_health
    monkeypatch.setattr(launch_health, "before_delivery", lambda *a: None)
    monkeypatch.setattr(launch_health, "delivered", lambda *a: None)
    calls = []

    def complete(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return Reply("", [ToolCall("r", "Read", {"path": "a.py"})], "tool_calls")
        if len(calls) == 3:
            if stop == "error":
                raise ClientError("endpoint failed")
            if stop == "signal":
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return Reply("", [ToolCall("d", "Diagnostics", {"path": "a.py"})], "tool_calls")
        return Reply("done", [], "stop")

    monkeypatch.setattr(runner, "Client", lambda *a, **k: SimpleNamespace(complete=complete))
    old_handler = signal.getsignal(signal.SIGTERM)
    if stop == "signal":
        with pytest.raises(SystemExit):
            runner.run_native(db, "agent", exit_when_idle=True)
    else:
        assert runner.run_native(db, "agent", exit_when_idle=True) == (1 if stop == "error" else 0)
    assert signal.getsignal(signal.SIGTERM) == old_handler
    events = [json.loads(line) for line in (tmp_path / "lsp-events.jsonl").read_text().splitlines()]
    assert [e.get("method") for e in events].count("initialize") == 1
    assert [e.get("method") for e in events][-2:] == ["shutdown", "exit"]
