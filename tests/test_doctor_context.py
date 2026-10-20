"""brindle doctor warns when an Ollama server's context is smaller than a
native profile's context_tokens."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brindle import doctor
from brindle.native import runner


class FakeOllama:
    """Answers /v1/models always; /api/version and /api/show only when
    ``ollama`` is set. ``parameters`` is the string /api/show reports."""

    def __init__(self, ollama=True, parameters=""):
        self.ollama = ollama
        self.parameters = parameters
        self.show_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": "tiny"}]})
                elif self.path == "/api/version" and outer.ollama:
                    self._send(200, {"version": "0.9.0"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.show_requests.append(json.loads(self.rfile.read(n)))
                if self.path == "/api/show" and outer.ollama:
                    self._send(200, {"parameters": outer.parameters})
                else:
                    self._send(404, {"error": "not found"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve():
    servers = []

    def make(**kw):
        s = FakeOllama(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def add_profile(repo, fake, context="8k"):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "local.md").write_text(
        f"---\nname: local\ndescription: a local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: {context}\n"
        "permission_mode: acceptEdits\n---\nYou are a local worker.\n"
    )


def context_checks(repo):
    return [c for c in doctor.native_checks(str(repo)) if c.name == "context local"]


def test_small_num_ctx_warns_with_the_fix(repo, serve):
    fake = serve(parameters="num_ctx                        4096\nstop  <|im_end|>")
    add_profile(repo, fake)
    [c] = context_checks(repo)
    assert c.level == doctor.WARN
    assert "4096" in c.detail and "8000" in c.detail
    assert "OLLAMA_CONTEXT_LENGTH=16192 ollama serve" in c.detail
    assert fake.show_requests == [{"model": "tiny"}]


def test_non_ollama_endpoint_has_no_context_check(repo, serve):
    fake = serve(ollama=False)
    add_profile(repo, fake)
    assert context_checks(repo) == []
    assert runner.server_context(runner.Endpoint(fake.base_url + "/v1", "tiny")) is None
    assert fake.show_requests == []


def test_doctor_daemon_reports_live_and_stale_pid(tmp_path, monkeypatch):
    from brindle import procs

    (tmp_path / "daemon.json").write_text(json.dumps({"pid": 4242, "port": 51234}))
    monkeypatch.setattr(procs, "alive", lambda pid: True)
    [live] = doctor.daemon_checks(tmp_path)
    monkeypatch.setattr(procs, "alive", lambda pid: False)
    [stale] = doctor.daemon_checks(tmp_path)
    assert (live.level, stale.level) == (doctor.OK, doctor.WARN)
    assert live.detail == "running (pid 4242, tcp 127.0.0.1:51234)"
