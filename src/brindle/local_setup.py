"""Local Ollama model setup for the CLI worker picker.

When a user picks a local Ollama model in ``brindle -s`` (the workers screen),
the picker calls :func:`setup_model` to make that model ready on this machine:

1. Check that the machine has enough physical memory for the model (for example,
   ``qwen3-coder:30b`` needs at least 24 GB of RAM) and return a fallback
   :class:`SetupResult` if memory is too small.
2. Check whether ``ollama`` is on ``PATH`` (via :func:`brindle.native.serve.ollama_available`).
   If not, never prompt without a TTY (return a fallback result immediately when
   non-interactive); otherwise ask for confirmation before running the install
   command, returning a fallback result if declined.
3. Ensure the local Ollama server is running using :mod:`brindle.native.serve`
   (:class:`~brindle.native.serve.Server`, :func:`~brindle.native.serve.reachable`,
   :func:`~brindle.native.serve.start_ollama`, :func:`~brindle.native.serve.wait_reachable`).
4. Pull the model with ``ollama pull <model>``, streaming progress output lines
   to ``say``.
5. Verify that the model is present in Ollama's ``GET /api/tags`` catalog and
   return a ready :class:`SetupResult` (or a fallback result with the reason).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from brindle.native import serve

DEFAULT_MODEL = "qwen3-coder:30b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_CONTEXT_TOKENS = 32_000
MIN_MEMORY_GB = 24.0
TAGS_TIMEOUT = 5.0
INSTALL_CMD = ("brew", "install", "ollama")

MODEL_MEMORY_GB: dict[str, float] = {
    "qwen3-coder:30b": 24.0,
    "qwen2.5-coder:32b": 24.0,
    "qwen2.5-coder:14b": 12.0,
    "qwen2.5-coder:7b": 8.0,
}


@dataclass(frozen=True)
class SetupResult:
    """Outcome of preparing a local Ollama model for worker routing."""

    ready: bool
    model: str = DEFAULT_MODEL
    reason: str = ""
    fallback: bool = False
    status: str = "ready"

    def __bool__(self) -> bool:
        return self.ready


def _ready(model: str) -> SetupResult:
    return SetupResult(ready=True, model=model, reason="", fallback=False, status="ready")


def _fallback(model: str, reason: str) -> SetupResult:
    return SetupResult(ready=False, model=model, reason=reason, fallback=True, status="fallback")


def required_memory_gb(model: str = DEFAULT_MODEL) -> float:
    """Minimum physical memory (in GiB) required to run ``model`` locally."""
    return MODEL_MEMORY_GB.get(model, MIN_MEMORY_GB)


def machine_memory_gb() -> float:
    """Total physical memory on this machine in GiB."""
    return (os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 ** 3)


def is_tty() -> bool:
    """Whether standard input is an interactive terminal."""
    return sys.stdin.isatty()


def _tty_confirm(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def _confirmed(answer: bool | str) -> bool:
    return answer if isinstance(answer, bool) else answer.strip().lower() in ("y", "yes")


def _run_streaming(
    argv: list[str],
    say: Callable[[str], None],
    env: dict[str, str] | None = None,
) -> bool:
    """Run ``argv`` with combined output, forwarding each non-empty progress line to ``say``."""
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError:
        return False
    if proc.stdout is not None:
        for raw in proc.stdout:
            line = raw.strip()
            if line:
                say(line)
    return proc.wait() == 0


def install_ollama(say: Callable[[str], None] = print) -> bool:
    """Install Ollama via ``INSTALL_CMD`` and verify that ``ollama`` is then on ``PATH``."""
    say("Installing Ollama (`brew install ollama`)...")
    if not _run_streaming(list(INSTALL_CMD), say):
        return False
    return serve.ollama_available()


def pull_model(
    server: serve.Server,
    model: str,
    say: Callable[[str], None] = print,
) -> bool:
    """Pull ``model`` onto ``server`` with ``ollama pull``, streaming progress output to ``say``."""
    say(f"Pulling {model}...")
    return _run_streaming(["ollama", "pull", model], say, env=serve.ollama_env(server))


def _get_tags_json(url: str, timeout: float = TAGS_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def verify_tags(
    host: str,
    model: str,
    *,
    timeout: float = TAGS_TIMEOUT,
    get_json: Callable[[str, float], dict] | None = None,
) -> bool:
    """Return True when ``model`` is listed by Ollama's ``GET <host>/api/tags`` endpoint."""
    url = serve._host(host) + "/api/tags"
    fetch = get_json or _get_tags_json
    try:
        data = fetch(url, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False
    names = {
        str(m.get("name") or m.get("model") or "")
        for m in items
        if isinstance(m, dict)
    }
    candidates = {model, f"{model}:latest"} if ":" not in model else {model}
    return bool(candidates & names)


def setup_model(
    model: str = DEFAULT_MODEL,
    *,
    host: str = DEFAULT_HOST,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    min_memory_gb: float | None = None,
    memory_gb: float | None = None,
    tty: bool | None = None,
    ask: Callable[[str], bool | str] | None = None,
    say: Callable[[str], None] = print,
    log: Path | None = None,
    get_json: Callable[[str, float], dict] | None = None,
) -> SetupResult:
    """Make ``model`` ready on local Ollama, or return a fallback :class:`SetupResult`."""
    avail_gb = machine_memory_gb() if memory_gb is None else float(memory_gb)
    need_gb = required_memory_gb(model) if min_memory_gb is None else float(min_memory_gb)
    if avail_gb < need_gb:
        return _fallback(
            model,
            f"{model} needs at least {need_gb:g} GB of memory (this machine has {avail_gb:.0f} GB); "
            "falling back to a non-local worker model",
        )

    if not serve.ollama_available():
        has_tty = is_tty() if tty is None else tty
        if not has_tty:
            return _fallback(
                model,
                "Ollama is not installed and cannot prompt for installation without a TTY",
            )
        prompt_fn = ask or _tty_confirm
        if not _confirmed(prompt_fn("Ollama is not installed. Install Ollama now? [y/N] ")):
            return _fallback(
                model,
                "Ollama installation was declined; falling back to another worker model",
            )
        if not install_ollama(say=say):
            return _fallback(
                model,
                "Failed to install Ollama (`brew install ollama`)",
            )

    server = serve.Server(
        host=serve._host(host),
        models=[model],
        profiles=["developer-local"],
        context_tokens=context_tokens,
    )
    if not serve.reachable(server):
        serve.start_ollama(server, log=log)
        if not serve.wait_reachable(server):
            return _fallback(
                model,
                f"Ollama server at {server.host} did not become reachable",
            )

    if not pull_model(server, model, say=say):
        return _fallback(
            model,
            f"Failed to pull {model} with `ollama pull {model}`",
        )

    if not verify_tags(server.host, model, get_json=get_json):
        return _fallback(
            model,
            f"{model} was not found in Ollama's /api/tags after pull",
        )
    return _ready(model)


ensure_model = setup = ensure = setup_model

__all__ = [
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_HOST",
    "DEFAULT_MODEL",
    "INSTALL_CMD",
    "MIN_MEMORY_GB",
    "MODEL_MEMORY_GB",
    "SetupResult",
    "ensure",
    "ensure_model",
    "install_ollama",
    "is_tty",
    "machine_memory_gb",
    "pull_model",
    "required_memory_gb",
    "setup",
    "setup_model",
    "verify_tags",
]
