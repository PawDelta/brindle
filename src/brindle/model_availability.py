"""Bounded, non-generating discovery of a profile's selected model.

Integration API: keep a ``ModelAvailabilityChecker`` and call ``check(profile,
effective_env=..., cwd=...)`` after provisioning has applied managed models,
stored keys, env_allow and pane_auth exclusions. The environment is a complete
snapshot, NOT overrides: this module never restores denied keys or reads the
keystore. Only ``unavailable`` is suitable for rejecting a selected model.

``available`` means listed by the configured endpoint/CLI, not verified quota,
billing, tool support or permission to generate. Codex may return a bundled
catalog, so membership alone is unverified, as are missing IDs and unknown
aliases. Its availabilityNux metadata is a notice, not an entitlement flag;
the result surfaces its presence without interpreting or echoing provider prose.
No thread, turn, completion, model pull or server startup is performed.

Built-in discovery runs in a disposable process group on macOS/Linux, with a
wall timeout (including DNS, login helpers and pagination) and output limits.
Other platforms return unverified. Claude Code, Bedrock, Vertex and Foundry
identity/entitlement discovery is deliberately unsupported: no Claude CLI is
called. Native OpenAI/Anthropic-compatible HTTP catalogs are supported; generic
gateway lists are advisory. Ollama uses /api/tags on its conventional port.

Cache entries live in memory for 30 seconds, with salted identity digests only.
CLI callers must supply ``account_identity`` (an opaque login/config revision)
to opt into caching, and change it on login/config/keyring changes. Without it
CLI discovery is uncached, since env alone cannot identify a subscription.
``clear()`` also invalidates entries. Injected discovery functions accept a
Request and timeout in seconds and must honor that timeout; production callers
should use the default process-isolated discovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from brindle.profiles import Profile

Status = Literal["available", "unavailable", "unverified"]
Source = Literal["codex", "antigravity", "ollama", "openai", "anthropic", "compatible"]
TIMEOUT = 5.0
CACHE_TTL = 30.0
MAX_BYTES = 1024 * 1024
MAX_PAGES = 10


@dataclass(frozen=True)
class Availability:
    status: Status
    explanation: str


@dataclass(frozen=True)
class Request:
    provider: str
    base_url: str | None = field(default=None, repr=False)
    api: str | None = None
    api_key_env: str | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None


@dataclass(frozen=True)
class Catalog:
    models: frozenset[str]
    source: Source
    # A recognized complete list is not necessarily exhaustive for aliases or
    # custom deployments. Only authoritative catalogs justify a negative.
    complete: bool = True
    authoritative: bool = False
    # Codex's message-only availabilityNux is not a boolean access decision.
    availability_notices: frozenset[str] = frozenset()


class _DiscoveryError(Exception):
    pass


_FAILURES = {
    "timeout": "Model discovery timed out; retry or check the provider's connection and login.",
    "auth": "The catalog rejected authentication; check the worker's effective credentials and account.",
    "missing_key": "The configured API key is absent from the worker environment; configure it and retry.",
    "unsupported": "No supported read-only catalog for this provider; verify the model in its account console.",
    "platform": "Bounded discovery requires macOS or Linux; verify the model in the provider's console.",
    "failed": "Could not read a complete model catalog; check the CLI installation, login or endpoint and retry.",
}


class ModelAvailabilityChecker:
    def __init__(self, *, discover: Callable[[Request, float], Catalog] | None = None,
                 timeout: float = TIMEOUT, ttl: float = CACHE_TTL,
                 clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("timeout must be positive and at most 30 seconds")
        if not math.isfinite(ttl) or not 0 <= ttl <= 60:
            raise ValueError("ttl must be between 0 and 60 seconds")
        self._discover = discover or _isolated_discovery
        self._timeout, self._ttl, self._clock = timeout, ttl, clock
        self._salt = os.urandom(32)
        self._cache: dict[bytes, tuple[float, Availability]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def check(self, profile: Profile, *, effective_env: Mapping[str, str],
              cwd: str | None = None, account_identity: str | None = None) -> Availability:
        """Check an explicit model using exactly the environment of its worker.

        Profile.env is intentionally not re-applied; effective_env has already
        resolved its precedence and may have removed credentials from it.
        Account identities, model names, URLs and error bodies are never echoed.
        """
        model = profile.model
        if not model or not model.strip() or model == "default":
            return Availability("unverified", "No explicit model to check; keep the provider's default selection.")
        if profile.provider not in ("codex", "antigravity", "native"):
            return Availability("unverified", _FAILURES["unsupported"])
        if profile.provider == "native":
            from brindle.profiles import subscription_token_problem

            if subscription_token_problem(profile):
                return Availability("unverified", "Native discovery cannot use Claude subscription credentials; "
                                    "configure an API key for this profile.")
        request = Request(profile.provider, profile.base_url, profile.api,
                          profile.api_key_env, dict(effective_env), os.path.abspath(cwd or os.getcwd()))
        # Hash the WHOLE effective environment: provider plugins may choose
        # their account via arbitrary env_key names. Never retain raw keys.
        payload = [request.provider, model, request.base_url, request.api,
                   request.api_key_env, sorted(request.env.items()), request.cwd, account_identity]
        key = hmac.digest(self._salt, json.dumps(payload).encode(), hashlib.sha256)
        cacheable = bool(self._ttl and (request.provider == "native" or account_identity is not None))
        now = self._clock()
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if cacheable and key in self._cache:
                return self._cache[key][1]
        try:
            catalog = self._discover(request, self._timeout)
            result = _selected(model, catalog)
        except (TimeoutError, subprocess.TimeoutExpired):
            result = Availability("unverified", _FAILURES["timeout"])
        except _DiscoveryError as exc:
            result = Availability("unverified", _FAILURES.get(str(exc), _FAILURES["failed"]))
        except Exception:  # Discovery must not break provisioning, nor echo secrets from errors.
            result = Availability("unverified", _FAILURES["failed"])
        if cacheable:
            with self._lock:
                if len(self._cache) >= 128:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = (self._clock() + self._ttl, result)
        return result


def _selected(model: str, catalog: Catalog) -> Availability:
    names = {model}
    if catalog.source == "ollama" and ":" not in model.rsplit("/", 1)[-1]:
        names.add(model + ":latest")
    if catalog.source == "codex" and names & catalog.models:
        if names & catalog.availability_notices:
            return Availability("unverified", "Codex lists the selected model with an availability notice; "
                                "check its model picker and account guidance before provisioning. "
                                "The notice does not verify generation access or quota.")
        return Availability("unverified", "Codex lists the selected model, but may be using a bundled or cached "
                            "catalog. Check its model picker and configured account before provisioning; "
                            "generation access and quota are not verified.")
    if names & catalog.models:
        return Availability("available", "The selected model is listed by the configured provider. "
                            "Generation access, quota and tool support are not verified.")
    if catalog.complete and catalog.authoritative:
        action = ("Pull the selected model on that Ollama server or choose an installed model."
                  if catalog.source == "ollama" else
                  "Choose a listed model or check the configured account and exact model ID.")
        return Availability("unavailable", "The selected model is absent from the provider's complete catalog. " + action)
    return Availability("unverified", "The selected model was not found in an advisory or partial catalog; "
                        "verify its exact ID or alias with the configured provider.")


def _kill_group(proc: subprocess.Popen) -> None:
    # Kill the group even if its leader exited: a CLI's login helper can keep
    # pipes open. Only processes in our newly created session are targeted.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=1)


def _isolated_discovery(request: Request, timeout: float) -> Catalog:
    if sys.platform not in ("darwin", "linux"):
        raise _DiscoveryError("platform")
    payload = json.dumps({"provider": request.provider, "base_url": request.base_url,
                          "api": request.api, "api_key_env": request.api_key_env,
                          "timeout": timeout}).encode()
    # A small stdin payload, never secrets in argv. The helper inherits exactly
    # the worker env, including HOME, PATH and credential exclusions.
    if len(payload) > 4096:
        raise _DiscoveryError("failed")
    deadline = time.monotonic() + timeout
    with subprocess.Popen([sys.executable, os.path.abspath(__file__), "--discover"],
                          env=dict(request.env), cwd=request.cwd, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True) as proc:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
            chunks = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BYTES:
                        raise _DiscoveryError("failed")
            proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            data = json.loads(chunks)
            if "error" in data:
                raise _DiscoveryError(data["error"])
            if proc.returncode != 0:
                raise _DiscoveryError("failed")
            return Catalog(frozenset(data["models"]), data["source"],
                           data["complete"], data["authoritative"],
                           frozenset(data.get("availability_notices", [])))
        finally:
            _kill_group(proc)


def _ids(data: object, field_name: str, id_name: str) -> set[str]:
    if not isinstance(data, dict) or not isinstance(data.get(field_name), list):
        raise _DiscoveryError("failed")
    names = set()
    for item in data[field_name]:
        if not isinstance(item, dict) or not isinstance(item.get(id_name), str) or not item[id_name]:
            raise _DiscoveryError("failed")
        names.add(item[id_name])
    return names


def _codex_catalog() -> Catalog:
    from brindle.providers import codex_binary

    # Protocol: https://developers.openai.com/codex/app-server#models
    # No thread/start, turn/start, login or auth commands. The outer helper
    # process owns the wall timeout and this child's process group.
    with subprocess.Popen([codex_binary(), "app-server"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        total = 0

        def send(message):
            proc.stdin.write(json.dumps(message).encode() + b"\n")
            proc.stdin.flush()

        def rpc(number, method, params):
            nonlocal total
            send({"id": number, "method": method, "params": params})
            while True:
                line = proc.stdout.readline(MAX_BYTES + 1)
                total += len(line)
                if not line or total > MAX_BYTES:
                    raise _DiscoveryError("failed")
                message = json.loads(line)
                if message.get("id") == number:
                    if "error" in message:
                        raise _DiscoveryError("failed")
                    return message["result"]

        try:
            rpc(1, "initialize", {"clientInfo": {"name": "brindle_model_discovery",
                                                "title": "brindle model discovery", "version": "1"}})
            send({"method": "initialized", "params": {}})
            models: set[str] = set()
            notices: set[str] = set()
            cursor = None
            seen = set()
            for page in range(MAX_PAGES):
                data = rpc(page + 2, "model/list", {"limit": 100, "includeHidden": True, "cursor": cursor})
                models.update(_ids(data, "data", "model"))
                # Current model/list exposes a message-only availabilityNux,
                # not a per-model entitlement decision. hidden/isDefault and
                # service tiers are picker/capability metadata, not access.
                # Preserve notices but never classify their free-form prose.
                notices.update(item["model"] for item in data["data"]
                               if item.get("availabilityNux") is not None)
                cursor = data.get("nextCursor")
                if cursor is None:
                    return Catalog(frozenset(models), "codex", availability_notices=frozenset(notices))
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise _DiscoveryError("failed")
                seen.add(cursor)
            return Catalog(frozenset(models), "codex", complete=False,
                           availability_notices=frozenset(notices))
        finally:
            proc.kill()
            proc.wait(timeout=1)


def _antigravity_catalog() -> Catalog:
    from brindle.antigravity import binary

    with subprocess.Popen([binary(), "models"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            output = proc.stdout.read(MAX_BYTES + 1)
            if len(output) > MAX_BYTES or proc.wait() != 0:
                raise _DiscoveryError("failed")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=1)
    return _parse_antigravity(output.decode("utf-8"))


def _parse_antigravity(output: str) -> Catalog:
    # Versions differ in presentation. JSON and explicit ID columns/lines are
    # accepted; unrecognized output is never treated as an empty catalog.
    try:
        data = json.loads(output)
    except ValueError:
        text = re.sub(r"\x1b\[[0-9;]*m", "", output)
        models = set(re.findall(
            r"(?m)^\s*(?:[│|*•-]\s*|(?:Model )?ID:\s*)?"
            r"([a-z][a-z0-9]*[-/][a-z0-9./:_-]+)(?=\s|$)", text))
        if not models:
            raise _DiscoveryError("failed")
        return Catalog(frozenset(models), "antigravity", complete=False)
    if isinstance(data, list):
        data = {"models": data}
    models = _ids(data, "models", "id")
    return Catalog(frozenset(models), "antigravity")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a worker's credentials to a catalog-supplied location.
        return None


def _get_json(url: str, headers: Mapping[str, str], timeout: float) -> dict:
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        with opener.open(urllib.request.Request(url, headers=dict(headers), method="GET"),
                         timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _DiscoveryError("auth" if exc.code in (401, 403) else "failed") from None
    if len(body) > MAX_BYTES:
        raise _DiscoveryError("failed")
    return json.loads(body)


def _native_catalog(request: Request, timeout: float, *, get=_get_json) -> Catalog:
    base = (request.base_url or "").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise _DiscoveryError("unsupported")
    if parsed.username or parsed.password:
        raise _DiscoveryError("unsupported")
    api = (request.api or "openai").lower()
    if api not in ("openai", "anthropic"):
        raise _DiscoveryError("unsupported")
    key = request.env.get(request.api_key_env) if request.api_key_env else None
    if request.api_key_env and not key:
        raise _DiscoveryError("missing_key")
    headers = {"Accept": "application/json"}
    if key:
        header = "x-api-key" if api == "anthropic" else "Authorization"
        headers[header] = key if api == "anthropic" else f"Bearer {key}"
    if api == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    if parsed.port == 11434 and parsed.path in ("", "/v1") and api == "openai":
        url = urllib.parse.urlunsplit(parsed._replace(path="/api/tags"))
        data = get(url, headers, timeout)
        return Catalog(frozenset(_ids(data, "models", "name")), "ollama", authoritative=True)
    # Match native.client.Endpoint's URL convention: OpenAI base includes its
    # API prefix; Anthropic bases may omit /v1.
    url = base + ("/v1/models" if api == "anthropic" and not base.endswith("/v1") else "/models")
    source: Source = "compatible"
    if api == "openai" and parsed.hostname == "api.openai.com" and parsed.path == "/v1":
        source = "openai"
    elif api == "anthropic" and parsed.hostname == "api.anthropic.com" and parsed.path in ("", "/v1"):
        source = "anthropic"
    models: set[str] = set()
    cursor = None
    seen = set()
    for _ in range(MAX_PAGES):
        page_url = url + ("?" + urllib.parse.urlencode({"after_id": cursor}) if cursor else "")
        data = get(page_url, headers, timeout)
        models.update(_ids(data, "data", "id"))
        if "has_more" in data and not isinstance(data["has_more"], bool):
            raise _DiscoveryError("failed")
        if data.get("has_more") is not True:
            # Unknown pagination schemes cannot justify a negative result.
            complete = not any(data.get(k) for k in ("next", "nextCursor", "next_page_token"))
            return Catalog(frozenset(models), source, complete, source in ("openai", "anthropic"))
        cursor = data.get("last_id")
        if api != "anthropic" or not isinstance(cursor, str) or not cursor or cursor in seen:
            return Catalog(frozenset(models), source, complete=False)
        seen.add(cursor)
    return Catalog(frozenset(models), source, complete=False)


def _worker() -> None:
    try:
        args = json.loads(sys.stdin.buffer.read(4097))
        request = Request(args["provider"], args["base_url"], args["api"], args["api_key_env"], dict(os.environ))
        if request.provider == "codex":
            catalog = _codex_catalog()
        elif request.provider == "antigravity":
            catalog = _antigravity_catalog()
        elif request.provider == "native":
            catalog = _native_catalog(request, args["timeout"])
        else:
            raise _DiscoveryError("unsupported")
        output = {"models": sorted(catalog.models), "source": catalog.source,
                  "complete": catalog.complete, "authoritative": catalog.authoritative,
                  "availability_notices": sorted(catalog.availability_notices)}
    except _DiscoveryError as exc:
        output = {"error": str(exc) if str(exc) in _FAILURES else "failed"}
    except Exception:
        output = {"error": "failed"}
    print(json.dumps(output))


if __name__ == "__main__" and sys.argv[1:] == ["--discover"]:
    _worker()
