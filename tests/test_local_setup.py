"""Tests for brindle.local_setup using fakes for memory, TTY, subprocess, and HTTP."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from brindle import local_setup
from brindle.native import serve


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home"))
    state = {
        "ram_gb": 32,
        "tty": True,
        "installed": False,
        "reachable": False,
        "pull_rc": 0,
        "tags": [],
        "commands": [],
        "urls": [],
    }

    def fake_sysconf(name: str) -> int:
        return 4096 if name == "SC_PAGE_SIZE" else int(state["ram_gb"] * (1024 ** 3) // 4096)

    class FakePopen:
        pid = 5150

        def __init__(self, argv, env=None, **kw):
            cmd = list(argv)
            state["commands"].append((cmd, env))
            if cmd == list(local_setup.INSTALL_CMD):
                state["installed"] = True
                self.stdout = io.StringIO("downloading ollama\ninstalled ollama\n")
                self.returncode = 0
            elif cmd == ["ollama", "serve"]:
                state["reachable"] = True
                self.stdout = None
                self.returncode = 0
            elif cmd[:2] == ["ollama", "pull"]:
                self.stdout = io.StringIO("pulling manifest\npulling layers 50%\nsuccess\n")
                self.returncode = state["pull_rc"]
            else:
                raise AssertionError(f"unexpected command: {cmd}")

        def wait(self) -> int:
            return self.returncode

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        state["urls"].append(url)
        if url.endswith("/api/tags"):
            body = json.dumps({"models": [{"name": m} for m in state["tags"]]}).encode()
            return FakeResponse(body)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(local_setup.os, "sysconf", fake_sysconf)
    monkeypatch.setattr(local_setup.sys.stdin, "isatty", lambda: state["tty"])
    monkeypatch.setattr(serve.shutil, "which", lambda name: "/usr/local/bin/ollama" if state["installed"] else None)
    monkeypatch.setattr(serve, "reachable", lambda server, timeout=1.0: state["reachable"])
    monkeypatch.setattr(serve.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(local_setup.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(local_setup.urllib.request, "urlopen", fake_urlopen)
    return state


def test_insufficient_memory_returns_fallback(fake_env):
    fake_env["ram_gb"] = 16
    res = local_setup.setup_model("qwen3-coder:30b", say=lambda _: None)
    assert not res.ready and not res and res.fallback and res.status == "fallback"
    assert "24" in res.reason and "16" in res.reason
    assert fake_env["commands"] == [] and fake_env["urls"] == []


@pytest.mark.parametrize("tty, answer, asked", [(False, True, 0), (True, "n", 1)], ids=["no-tty", "declined"])
def test_no_ollama_install_without_consent_returns_fallback(fake_env, tty, answer, asked):
    # Without a terminal nothing is asked; a "no" is asked once. Neither installs.
    fake_env["tty"] = tty
    prompts: list[str] = []
    res = local_setup.setup_model(ask=lambda p: (prompts.append(p), answer)[1], say=lambda _: None)
    assert not res.ready and res.fallback
    assert len(prompts) == asked and fake_env["commands"] == []


def test_installs_starts_server_pulls_with_progress_and_verifies_tags(fake_env, monkeypatch, tmp_path):
    fake_env["tags"] = ["qwen3-coder:30b"]
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    progress: list[str] = []
    res = local_setup.setup_model("qwen3-coder:30b", say=progress.append, log=tmp_path / "ollama.log")
    assert res.ready and bool(res) and not res.fallback and res.status == "ready"
    assert [cmd for cmd, _ in fake_env["commands"]] == [
        ["brew", "install", "ollama"],
        ["ollama", "serve"],
        ["ollama", "pull", "qwen3-coder:30b"],
    ]
    assert "pulling layers 50%" in progress
    assert fake_env["urls"] == ["http://localhost:11434/api/tags"]


def test_unverified_or_failed_pull_returns_fallback(fake_env):
    fake_env["installed"] = True
    fake_env["reachable"] = True
    fake_env["tags"] = ["other-model:latest"]
    missing = local_setup.setup_model("qwen3-coder:30b", say=lambda _: None)
    assert not missing.ready and missing.fallback and "/api/tags" in missing.reason

    fake_env["pull_rc"] = 1
    failed = local_setup.setup_model("qwen3-coder:30b", say=lambda _: None)
    assert not failed.ready and failed.fallback and "ollama pull" in failed.reason
