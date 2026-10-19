"""Lazy, worktree-local language servers using JSON-RPC over stdio.

No servers are installed or downloaded. A scoped profile cannot safely run
a server that indexes the whole worktree, so it gets a note instead.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

# language id, candidate commands (in preference order)
SERVERS = {
    ".py": ("python", (("pyright-langserver", "--stdio"), ("pylsp",))),
    ".pyi": ("python", (("pyright-langserver", "--stdio"), ("pylsp",))),
    ".js": ("javascript", (("typescript-language-server", "--stdio"),)),
    ".jsx": ("javascriptreact", (("typescript-language-server", "--stdio"),)),
    ".ts": ("typescript", (("typescript-language-server", "--stdio"),)),
    ".tsx": ("typescriptreact", (("typescript-language-server", "--stdio"),)),
    ".go": ("go", (("gopls",),)),
    ".rs": ("rust", (("rust-analyzer",),)),
    ".c": ("c", (("clangd",),)),
    ".h": ("c", (("clangd",),)),
    ".cpp": ("cpp", (("clangd",),)),
    ".hpp": ("cpp", (("clangd",),)),
    ".rb": ("ruby", (("ruby-lsp",),)),
    ".lua": ("lua", (("lua-language-server",),)),
}


class Client:
    """One server. Calls are serialized by the native loop; a reader thread
    lets even a partial frame or a silent server obey the request timeout."""

    def __init__(self, command: tuple[str, ...], root: Path, timeout: float):
        self.timeout = timeout
        self.root = root
        self.messages: queue.Queue = queue.Queue()
        self.serial = 0
        self.documents: dict[str, tuple[int, str]] = {}
        self.diagnostics: dict[str, list[dict]] = {}
        self.proc = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            result = self.request("initialize", {
                "processId": os.getpid(), "rootUri": root.as_uri(),
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                "capabilities": {"textDocument": {
                    "publishDiagnostics": {"versionSupport": True},
                    "diagnostic": {},
                }},
            })
            self.capabilities = result.get("capabilities", {})
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        try:
            while True:
                headers = {}
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        raise EOFError("language server closed stdout")
                    if line in (b"\r\n", b"\n"):
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                size = int(headers["content-length"])
                if not 0 < size <= 16 * 1024 * 1024:
                    raise ValueError("invalid LSP frame size")
                body = self.proc.stdout.read(size)
                self.messages.put(json.loads(body))
        except (OSError, ValueError, KeyError, EOFError) as e:
            self.messages.put(e)

    def _send(self, message: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def _receive(self, deadline: float) -> dict:
        if time.monotonic() >= deadline:
            raise TimeoutError("language server timed out")
        try:
            message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("language server timed out") from None
        if isinstance(message, Exception):
            raise OSError(str(message))
        method = message.get("method")
        if method and "id" in message:
            # Configuration and workspace queries are common during startup.
            # Never execute commands or apply server-requested workspace edits.
            if method == "workspace/configuration":
                result = [{} for _ in message.get("params", {}).get("items", [])]
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            elif method == "window/workDoneProgress/create":
                result = None
            else:
                self._send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
                return message
            self._send({"id": message["id"], "result": result})
        elif method == "textDocument/publishDiagnostics":
            params = message["params"]
            uri = params["uri"]
            current = self.documents.get(uri)
            if current and params.get("version", current[0]) == current[0]:
                self.diagnostics[uri] = params.get("diagnostics", [])
        return message

    def request(self, method: str, params: dict | None, timeout: float | None = None):
        self.serial += 1
        serial = self.serial
        self._send({"id": serial, "method": method, "params": params})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            message = self._receive(deadline)
            if "method" not in message and message.get("id") == serial:
                if "error" in message:
                    raise OSError(f"LSP request failed ({message['error'].get('code')})")
                return message.get("result")

    def sync(self, path: Path, language: str) -> str:
        uri = path.as_uri()
        text = path.read_text(encoding="utf-8")
        previous = self.documents.get(uri)
        if previous and previous[1] == text:
            return uri
        version = previous[0] + 1 if previous else 1
        self.documents[uri] = (version, text)
        self.diagnostics.pop(uri, None)
        if previous is None:
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language, "version": version, "text": text}})
        else:
            change = {"text": text}
            sync = self.capabilities.get("textDocumentSync", 1)
            kind = sync.get("change", 1) if isinstance(sync, dict) else sync
            if kind == 2:
                lines = previous[1].split("\n")
                change["range"] = {"start": {"line": 0, "character": 0}, "end": {
                    "line": len(lines) - 1, "character": len(lines[-1].encode("utf-16-le")) // 2}}
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version}, "contentChanges": [change]})
            self.notify("textDocument/didSave", {"textDocument": {"uri": uri}, "text": text})
        return uri

    def diagnose(self, uri: str) -> list[dict]:
        if self.capabilities.get("diagnosticProvider") is not None:
            return self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}).get("items", [])
        deadline = time.monotonic() + self.timeout
        while uri not in self.diagnostics:
            self._receive(deadline)
        return self.diagnostics[uri]

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                try:
                    self.request("shutdown", None, timeout=1)
                    self.notify("exit", {})
                    self.proc.wait(timeout=1)
                except (OSError, TimeoutError, subprocess.TimeoutExpired):
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait()
        finally:
            self.reader.join(timeout=1)
            self.proc.stdin.close()
            self.proc.stdout.close()


class LSP:
    """A worker's server pool. Own it for the runner's lifetime, across turns."""

    def __init__(self, cwd: str, read_scope: list[str] | None = None, timeout: float = 3):
        self.root = Path(cwd).resolve()
        self.read_scope = read_scope
        self.timeout = timeout
        self.clients: dict[tuple[str, ...], Client] = {}
        self.signals: dict = {}

    def __enter__(self):
        # tmux closes a pane with SIGHUP; stopping a worker can use SIGTERM.
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGHUP):
                self.signals[sig] = signal.signal(sig, self._stop)
        return self

    @staticmethod
    def _stop(signum, frame):
        raise SystemExit(128 + signum)

    def __exit__(self, *exc):
        try:
            self.close()
        finally:
            for sig, handler in self.signals.items():
                signal.signal(sig, handler)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def _path(self, raw: str) -> Path:
        path = (self.root / raw).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError("LSP path is outside the working directory")
        if self.read_scope:
            from brindle.guardrails import in_scope
            if not in_scope(path.relative_to(self.root).as_posix(), self.read_scope):
                raise PermissionError("LSP path is outside your read scope")
        return path

    def run(self, operation: str, args: dict) -> str:
        path = self._path(str(args.get("path") or ""))
        if self.read_scope:
            return "LSP unavailable: workspace indexing cannot enforce this profile's read scope."
        spec = SERVERS.get(path.suffix.lower())
        if spec is None:
            return f"LSP unavailable: no server configured for {path.suffix or 'this file type'}."
        language, candidates = spec
        command = next((cmd for cmd in candidates if shutil.which(cmd[0])), None)
        if command is None:
            return f"LSP unavailable: install {' or '.join(cmd[0] for cmd in candidates)} on PATH."
        try:
            if command not in self.clients:
                self.clients[command] = Client(command, self.root, self.timeout)
            client = self.clients[command]
            uri = client.sync(path, language)
            if operation == "open":
                return ""
            if operation == "diagnostics":
                diagnostics = client.diagnose(uri)
                return "\n".join(
                    f"{path.relative_to(self.root)}:{d['range']['start']['line'] + 1}:"
                    f"{d['range']['start']['character'] + 1}: "
                    f"{ {1: 'error', 2: 'warning', 3: 'info', 4: 'hint'}.get(d.get('severity'), 'diagnostic')}: "
                    f"{d['message']}" for d in diagnostics) or "LSP: no diagnostics."
            line, column = int(args["line"]) - 1, int(args["column"]) - 1
            lines = client.documents[uri][1].split("\n")
            if not 0 <= line < len(lines) or not 0 <= column <= len(lines[line]):
                raise ValueError("line or column is outside the file")
            params = {"textDocument": {"uri": uri}, "position": {
                "line": line, "character": len(lines[line][:column].encode("utf-16-le")) // 2}}
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
            result = client.request(f"textDocument/{operation}", params)
            locations = [result] if isinstance(result, dict) else result or []
            out = []
            for loc in locations:
                parsed = urlparse(loc.get("uri") or loc.get("targetUri", ""))
                if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                    continue
                try:
                    target = self._path(unquote(parsed.path))
                except PermissionError:
                    continue
                pos = (loc.get("targetSelectionRange") or loc["range"])["start"]
                out.append(f"{target.relative_to(self.root)}:{pos['line'] + 1}:{pos['character'] + 1}")
            return "\n".join(out) or "LSP: no locations in read scope."
        except (OSError, TimeoutError, ValueError) as e:
            return f"LSP unavailable: {str(e).splitlines()[0]}"
