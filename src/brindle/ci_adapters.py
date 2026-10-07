"""Provider adapters for brindle CI (:mod:`brindle.ci_client`): one object per
model provider that can run a CI supervisor or answer a review request,
wrapping brindle's existing providers (:mod:`brindle.providers`,
:mod:`brindle.agents`, :mod:`brindle.native`).

An adapter answers four questions and does three things:

* ``installed()``: is the provider's CLI on this machine.
* ``credential(env)``: which credential *names* are present (never values) and
  what kind they are: an API key, a cloud sign-in (Bedrock, Vertex, ...), an
  endpoint without a key, or a personal subscription (``CLAUDE_CODE_OAUTH_TOKEN``;
  a ChatGPT login for Codex). Anthropic workload identity federation reaches
  here as an API key: the workflow exchanges GitHub's OIDC token once and
  gives the job ``ANTHROPIC_AUTH_TOKEN``; during the run that variable holds
  the secret of brindle's own credential proxy (:mod:`brindle.ci_federation`).
* ``available(env)``: installed and holding some credential.
* ``launch``: start a brindle autopilot supervisor with the plan's instructions.
* ``review``: ask the model one question (the plan's instructions) and return
  its raw reply.
* ``usage``: token usage of a session, by model.

The credential rule (:func:`usable`): on a repository owned by a GitHub
organization, a provider whose only credential is a personal subscription is
unavailable for CI. Personal repositories may use it. Whether the owner is
an organization comes from the workflow's event payload or the GitHub API
(:func:`repo_is_org`); when neither says, the stricter answer is used.

Nothing here decides anything about the run itself: no verdicts, no stall
detection, no rendering. The server does that.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, MutableMapping


from brindle import providers

log = logging.getLogger(__name__)

API_KEY = "api_key"
CLOUD = "cloud"
ENDPOINT = "endpoint"
SUBSCRIPTION = "subscription"
RATE_LIMIT = "rate_limit"
AUTH = "auth"

CLAUDE_API_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CLAUDE_CLOUD = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
CLAUDE_SUBSCRIPTION = ("CLAUDE_CODE_OAUTH_TOKEN",)
CODEX_API_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY")
CODEX_LOGIN = "codex login (auth.json)"     # the name shown for a ChatGPT sign-in
NATIVE_KEYS_ENV = "BRINDLE_CI_NATIVE_KEYS"   # the workflow lists NAME@host pairs native profiles may use
LOOPBACK = frozenset({"localhost", "127.0.0.1"})
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
# A base_url written plainly: scheme, host, optional port, optional plain path.
BASE_URL_RE = re.compile(r"^(?P<scheme>https?)://(?P<host>[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                         r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
# Every known provider credential: what a check must not see, and what a
# repo-supplied native profile may not point at its own endpoint.
PROVIDER_KEYS = frozenset((*CLAUDE_API_KEYS, *CLAUDE_SUBSCRIPTION, *CODEX_API_KEYS,
                           "GEMINI_API_KEY", "GOOGLE_API_KEY"))
REVIEW_TIMEOUT = 1200.0
FIRST_RUN_WAIT = 20.0    # how long a new claude supervisor's pane is watched for a first-run screen
# An API error Claude Code retries itself (429, 5xx, overloaded) ends the run
# only once a pane has shown it this long; one it won't retry, at once.
TRANSIENT_API_ERROR_S = 120.0
API_ERROR_MAX = 300
SECRET_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]+|\b(?:Bearer|Basic)\s+\S+|[A-Za-z0-9_+/=-]{40,}", re.I)
DEFAULT_PROFILE = "supervisor"


class AdapterError(Exception):
    """The adapter couldn't do what was asked. Never carries a credential."""


@dataclass(frozen=True)
class Credential:
    kind: str | None            # API_KEY | CLOUD | ENDPOINT | SUBSCRIPTION | None
    names: tuple[str, ...]      # the credential names present (never values)


@dataclass
class Review:
    reply: str
    model: str | None = None
    usage: dict = field(default_factory=dict)   # {model: {input, output, cache_read}}
    exit: int | None = None


def _text(data) -> str:
    return data if isinstance(data, str) else ""


def _usage_entry(input_tokens: int, output_tokens: int, cache_read: int) -> dict:
    return {"input": int(input_tokens), "output": int(output_tokens), "cache_read": int(cache_read)}


def merge_usage(into: dict, more: Mapping) -> dict:
    """Add ``more`` (``{model: {input, output, cache_read}}``) into ``into``."""
    for model, u in more.items():
        if not isinstance(u, Mapping):
            continue
        cur = into.setdefault(model, _usage_entry(0, 0, 0))
        for k in ("input", "output", "cache_read"):
            cur[k] += int(u.get(k) or 0)
    return into


class Adapter:
    """The interface. Subclasses fill in the provider-specific parts."""

    name: str = ""
    cli: str | None = None      # the CLI's name, for doctor

    # -- questions --------------------------------------------------------------------

    def binary(self) -> str | None:
        return None

    def installed(self, env: Mapping[str, str] | None = None) -> bool:
        """Whether the CLI is on ``env``'s PATH (brindle's own by default)."""
        return self.cli_path(env) is not None

    def cli_path(self, env: Mapping[str, str] | None = None) -> str | None:
        exe = self.binary()
        if not exe:
            return None
        path = (env if env is not None else os.environ).get("PATH")
        found = shutil.which(exe, path=path)
        return found or (exe if os.path.isfile(exe) else None)

    def credential(self, env: Mapping[str, str]) -> Credential:
        return Credential(None, ())

    def credential_kind(self, env: Mapping[str, str]) -> str | None:
        return self.credential(env).kind

    def available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        """(usable here, why not)."""
        if not self.installed(env):
            return False, f"{self.cli or self.name} isn't installed"
        if self.credential(env).kind is None:
            return False, "no credential found"
        return True, ""

    # -- actions ----------------------------------------------------------------------

    def launch(self, db, ws, instructions: str, profile: str | None):
        """Start a supervisor in ``ws`` with ``instructions`` as its first
        message; returns the root :class:`brindle.db.Agent`."""
        from brindle import agents

        return agents.spawn(db, ws, profile or DEFAULT_PROFILE, prompt=instructions,
                            provider_name=self.name, autopilot=True)

    def stop(self, db, root_id: str) -> None:
        """Stop the supervisor and everything it started, keeping the worktree."""
        from brindle import agents

        agents.pause(db, root_id)

    def alive(self, db, root) -> bool:
        from brindle import agents

        return agents.is_alive(root)

    def stuck_screen(self, db, root) -> str | None:
        """Why the supervisor can't make progress on its own (a screen only a
        person answers), or None. Default: never."""
        return None

    def review(self, instructions: str, cwd: str, env: Mapping[str, str], *,
               timeout: float = REVIEW_TIMEOUT, profile: str | None = None) -> Review:
        raise AdapterError(f"{self.name} can't review")

    def provider_error(self, db, root_id: str) -> str | None:
        """``"rate_limit"`` when the provider is at its usage limit (brindle's
        quota record, or the session paused or blocked on it), ``"auth"``
        when its CLI reports it is signed out, else None."""
        from brindle import quota

        ap = db.get_autopilot(root_id)
        if ap is not None and (ap.state == "usage_paused" or (
                ap.state == "blocked" and "usage limit" in (ap.note or ""))):
            return RATE_LIMIT
        q = quota.get(self.name)
        if q is not None and q.limited_until and q.limited_until > time.time():
            return RATE_LIMIT
        if providers.signed_out(self.name):
            return AUTH
        return None

    def usage(self, db, root_id: str) -> dict:
        """Token usage of the session rooted at ``root_id``, by model."""
        from brindle import agents, usage

        out: dict = {}
        for a in agents.tree(db, root_id):
            u = usage.agent_usage(db, a)
            if u is None:
                continue
            merge_usage(out, {u.model or self.name: _usage_entry(u.input_tokens, u.output_tokens,
                                                                   u.cache_read_tokens)})
        return out


def _run(argv: list[str], cwd: str, env: Mapping[str, str], stdin: str, timeout: float) -> Review:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=dict(env), input=stdin, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Review("", exit=124)
    except OSError as e:
        raise AdapterError(f"couldn't start {argv[0]}: {e}") from e
    return Review(proc.stdout or "", exit=proc.returncode)


def drop_empty_keys(env: MutableMapping[str, str]) -> list[str]:
    """Remove Claude key variables set to an empty string from ``env`` in
    place (a workflow's ``${{ secrets.ANTHROPIC_API_KEY }}`` when the secret
    isn't set): an empty key still wins Claude Code's precedence, over the
    ``ANTHROPIC_AUTH_TOKEN`` that identity federation gives the job.
    Returns the names removed."""
    gone = [k for k in CLAUDE_API_KEYS if k in env and not env[k]]
    for k in gone:
        del env[k]
    return gone


def claude_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment the claude CLI runs in: ``env`` without empty key
    variables."""
    out = dict(env)
    drop_empty_keys(out)
    return out


def api_error_line(error: str) -> str:
    """An API error as a run's note may carry it: printable ASCII, anything
    key- or token-like redacted, at most API_ERROR_MAX characters."""
    text = SECRET_LIKE.sub("[redacted]", error)
    return re.sub(r"[^\x20-\x7e]", "?", text)[:API_ERROR_MAX]


class ClaudeAdapter(Adapter):
    name = "claude"
    cli = "claude"

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._api_errors: dict[str, tuple[str, float]] = {}   # agent id -> (error, first seen)

    def binary(self) -> str | None:
        return providers.claude_binary()

    def credential(self, env: Mapping[str, str]) -> Credential:
        keys = tuple(k for k in CLAUDE_API_KEYS if env.get(k))
        if keys:
            return Credential(API_KEY, keys)
        cloud = tuple(k for k in CLAUDE_CLOUD if env.get(k))
        if cloud:
            return Credential(CLOUD, cloud)
        sub = tuple(k for k in CLAUDE_SUBSCRIPTION if env.get(k))
        if sub:
            return Credential(SUBSCRIPTION, sub)
        return Credential(None, ())

    @staticmethod
    def prepare(cwd: str, env: Mapping[str, str]) -> None:
        """Answer Claude Code's first-run screens ahead of time (a fresh
        runner has no Claude Code state, and nobody is there to answer them),
        trusting the checkout ``cwd``. brindle's own worktrees are trusted
        when they're made (providers.trust_folder), now that the file exists.
        Only on a CI runner (``GITHUB_ACTIONS`` or ``CI`` is ``"true"`` in
        ``env``): anywhere else Claude Code's state is the person's own, and
        is left alone."""
        if "true" not in (env.get("GITHUB_ACTIONS"), env.get("CI")):
            log.info("brindle ci: not on a CI runner, leaving Claude Code's state alone")
            return
        try:
            providers.seed_ci_config([cwd], env.get("ANTHROPIC_API_KEY") or None)
        except (OSError, ValueError) as e:
            raise AdapterError(f"couldn't set up Claude Code's state: {e}") from None

    def launch(self, db, ws, instructions: str, profile: str | None):
        """Start the supervisor, then fail fast if it sits on a first-run
        screen anyway (it would wait there for good, spending nothing)."""
        self.prepare(ws.path, os.environ)
        root = super().launch(db, ws, instructions, profile)
        deadline = time.time() + FIRST_RUN_WAIT
        while True:
            screen = self._screen(root)
            why = providers.ClaudeCode.first_run_screen(screen or "")
            if why:
                self.stop(db, root.id)
                raise AdapterError(f"Claude Code stopped on {why}, which nobody in CI can answer")
            if screen is None or providers.ClaudeCode.READY.search(screen) or time.time() >= deadline:
                return root
            time.sleep(1)

    def stuck_screen(self, db, root) -> str | None:
        """A first-run screen on the supervisor's pane, or an API error the
        supervisor's or a reviewer's last reply ended on: at once when Claude
        Code won't retry it, else once it has been there TRANSIENT_API_ERROR_S
        (it would otherwise sit at its prompt, spending nothing, until the
        server's stall timer)."""
        from brindle import agents

        screen = self._screen(root) or ""
        why = providers.ClaudeCode.first_run_screen(screen)
        if why:
            return f"Claude Code is on {why}, which nobody in CI can answer"
        panes = [("Claude Code", root, screen)]
        if root is not None:
            panes += [(f"reviewer {a.id}'s Claude Code", a, self._screen(a) or "")
                      for a in agents.tree(db, root.id)
                      if a.id != root.id and a.mode == "review" and a.provider == self.name]
        now = self.clock()
        for who, agent, text in panes:
            if agent is None:
                continue
            error = providers.ClaudeCode.api_error(text)
            if error is None:
                self._api_errors.pop(agent.id, None)
                continue
            seen, since = self._api_errors.get(agent.id, (None, now))
            if seen != error:
                since = now
            self._api_errors[agent.id] = (error, since)
            line = api_error_line(error)
            if providers.ClaudeCode.FATAL_API_ERROR.match(error):
                return f"{who} stopped on an error it won't retry: {line}"
            if now - since >= TRANSIENT_API_ERROR_S:
                return f"{who} has been stopped on an API error for {int(now - since)}s: {line}"
        return None

    @staticmethod
    def _screen(root) -> str | None:
        from brindle import agents, tmux

        if root is None or not getattr(root, "tmux_window", None):
            return None
        try:
            return tmux.capture(root.tmux_window, lines=60, server=agents.server_of(root))
        except tmux.TmuxError:
            return None

    def review(self, instructions: str, cwd: str, env: Mapping[str, str], *,
               timeout: float = REVIEW_TIMEOUT, profile: str | None = None) -> Review:
        self.prepare(cwd, env)
        argv = [self.binary() or "claude", "-p", "--output-format", "json"]
        rev = _run(argv, cwd, claude_env(env), instructions, timeout)
        try:
            data = json.loads(rev.reply)
        except ValueError:
            return rev
        if not isinstance(data, dict):
            return rev
        rev.reply = _text(data.get("result")) or rev.reply
        models = data.get("modelUsage")
        if isinstance(models, dict):
            for model, u in models.items():
                if isinstance(u, dict):
                    merge_usage(rev.usage, {model: _usage_entry(
                        u.get("inputTokens") or 0, u.get("outputTokens") or 0,
                        u.get("cacheReadInputTokens") or 0)})
                    rev.model = rev.model or model
        return rev


class CodexAdapter(Adapter):
    name = "codex"
    cli = "codex"

    def binary(self) -> str | None:
        return providers.codex_binary()

    @staticmethod
    def _auth_file(env: Mapping[str, str]) -> Path:
        return Path(env.get("CODEX_HOME") or os.path.expanduser("~/.codex")) / "auth.json"

    def credential(self, env: Mapping[str, str]) -> Credential:
        keys = tuple(k for k in CODEX_API_KEYS if env.get(k))
        if keys:
            return Credential(API_KEY, keys)
        path = self._auth_file(env)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return Credential(None, ())
        if not isinstance(data, dict):
            return Credential(None, ())
        if data.get("OPENAI_API_KEY"):
            return Credential(API_KEY, (CODEX_LOGIN,))    # `codex login --with-api-key`
        if data.get("tokens"):
            return Credential(SUBSCRIPTION, (CODEX_LOGIN,))
        return Credential(None, ())

    def review(self, instructions: str, cwd: str, env: Mapping[str, str], *,
               timeout: float = REVIEW_TIMEOUT, profile: str | None = None) -> Review:
        with tempfile.TemporaryDirectory(prefix="brindle-ci-") as tmp:
            last = Path(tmp) / "reply.txt"
            argv = [self.binary() or "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                    "--output-last-message", str(last), "-"]
            rev = _run(argv, cwd, env, instructions, timeout)
            try:
                text = last.read_text("utf-8")
            except OSError:
                text = ""
        if text.strip():
            rev.reply = text
        return rev


class NativeAdapter(Adapter):
    """brindle's own loop against an OpenAI- or Anthropic-compatible endpoint.
    The endpoint comes from a native profile; ``profile`` names it, else the
    first native profile of the repo (``repo_root``) whose key is present."""

    name = "native"
    cli = None

    def __init__(self, repo_root: str | None = None) -> None:
        self.repo_root = repo_root

    def installed(self, env: Mapping[str, str] | None = None) -> bool:
        return True

    @staticmethod
    def allowed_keys(env: Mapping[str, str]) -> dict[str, frozenset[str]]:
        """Which key variable a native profile may send to which host, from
        ``BRINDLE_CI_NATIVE_KEYS`` (comma-separated ``NAME@host`` entries,
        set by the workflow, never the repository): ``{name: hosts}``. The
        profile, which comes from the repository, picks the endpoint, so a
        key goes only to the host it was paired with. A bare ``NAME`` reaches
        loopback only (``localhost``, ``127.0.0.1``): not link-local (the
        cloud metadata range) and not a private network, since self-hosted
        runners sit on shared ones; such endpoints need an explicit
        ``NAME@host``. Another provider's credential and the job's secrets
        are never allowed, whatever the list says."""
        out: dict[str, set[str]] = {}
        for entry in (env.get(NATIVE_KEYS_ENV) or "").split(","):
            name, _, host = entry.strip().partition("@")
            name, host = name.strip(), host.strip().lower()
            if not name or (host and not HOST_RE.match(host)) or name in PROVIDER_KEYS \
                    or name.startswith("GH_") or name in ("GITHUB_TOKEN", "BRINDLE_PRO_TOKEN"):
                continue
            out.setdefault(name, set()).update({host} if host else LOOPBACK)
        return {k: frozenset(v) for k, v in out.items()}

    @staticmethod
    def endpoint_host(base_url: str | None) -> str | None:
        """The host of a plainly written base_url, or None when the URL has
        anything a parser could read two ways: userinfo, a backslash,
        whitespace, a query or fragment, an IPv6 literal, a non-ASCII or
        percent-encoded character. Hostnames must be ASCII: an
        internationalized host is given as punycode (``xn--...``), both here
        and in ``BRINDLE_CI_NATIVE_KEYS``. The native client only ever
        connects to a URL this function accepted, so what it checks is what
        is used."""
        m = BASE_URL_RE.match(base_url or "")
        if not m:
            return None
        return m.group("host").lower()

    def _key_refused(self, p, env: Mapping[str, str]) -> str | None:
        """Why profile ``p``'s key may not be sent, or None. A profile comes
        from the repository and names any endpoint, so a key goes only to a
        host the workflow paired it with (see allowed_keys), over https
        (plain http only to the loopback names), and only to a URL written
        plainly enough that no parser can read another host out of it."""
        if not p.api_key_env:
            return None
        hosts = self.allowed_keys(env).get(p.api_key_env, frozenset())
        host = self.endpoint_host(p.base_url)
        scheme = (p.base_url or "").split(":", 1)[0].lower()
        if host and host in hosts and (scheme == "https" or (scheme == "http" and host in LOOPBACK)):
            return None
        shown = host or "an endpoint it can't read plainly"
        return (f"profile {p.name!r} would send {p.api_key_env} to {shown}; "
                f"list {p.api_key_env}@{host or 'host'} in {NATIVE_KEYS_ENV} (https) to allow that")

    def _profiles(self, env: Mapping[str, str] | None = None) -> list:
        """The repo's native profiles with an endpoint whose key (if any) the
        workflow allows."""
        from brindle.profiles import list_profiles

        env = os.environ if env is None else env
        try:
            return [p for p in list_profiles(self.repo_root)
                    if p.provider == "native" and p.base_url and self._key_refused(p, env) is None]
        except Exception:  # noqa: BLE001 - a broken profile is reported elsewhere
            return []

    def _profile(self, name: str | None, env: Mapping[str, str] | None = None):
        from brindle.profiles import load_profile

        env = os.environ if env is None else env
        if name:
            p = load_profile(name, self.repo_root)
            if p.provider != "native":
                raise AdapterError(f"profile {name!r} doesn't use the native provider")
            why = self._key_refused(p, env)
            if why:
                raise AdapterError(why)
            return p
        for p in self._profiles(env):
            if not p.api_key_env or env.get(p.api_key_env):
                return p
        raise AdapterError("no native profile with an endpoint (and an allowed key) is available")

    def credential(self, env: Mapping[str, str]) -> Credential:
        names = []
        keyless = False
        for p in self._profiles(env):
            if p.api_key_env and env.get(p.api_key_env):
                names.append(p.api_key_env)
            elif not p.api_key_env:
                keyless = True
        if names:
            return Credential(API_KEY, tuple(dict.fromkeys(names)))
        if keyless:
            return Credential(ENDPOINT, ())
        return Credential(None, ())

    def available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        if not self._profiles(env):
            return False, "no native profile with a base_url"
        if self.credential(env).kind is None:
            return False, "no native profile has its key"
        return True, ""

    def launch(self, db, ws, instructions: str, profile: str | None):
        from brindle import agents

        return agents.spawn(db, ws, self._profile(profile).name, prompt=instructions,
                            provider_name="native", autopilot=True)

    def review(self, instructions: str, cwd: str, env: Mapping[str, str], *,
               timeout: float = REVIEW_TIMEOUT, profile: str | None = None) -> Review:
        from brindle.native.client import Client, ClientError
        from brindle.native.runner import endpoint_for

        p = self._profile(profile, env)
        endpoint = endpoint_for(p)
        endpoint.timeout = timeout
        endpoint.api_key = env.get(p.api_key_env) if p.api_key_env else None
        try:
            reply = Client(endpoint).complete(p.prompt or None, [{"role": "user", "content": instructions}], [])
        except ClientError as e:
            return Review("", model=p.model, exit=1 if e.status is None else e.status)
        model = reply.model or p.model
        return Review(reply.text, model=model, exit=0, usage={model: _usage_entry(
            reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.cache_read_tokens)})


def default_adapters(repo_root: str | None = None) -> dict[str, Adapter]:
    return {a.name: a for a in (ClaudeAdapter(), CodexAdapter(), NativeAdapter(repo_root))}


# -- the credential rule ------------------------------------------------------------------


def _event_payload(env: Mapping[str, str]) -> dict | None:
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def repo_is_org(env: Mapping[str, str], repo: str | None = None, *, run=subprocess.run) -> bool | None:
    """Whether the repository's owner is a GitHub organization: from the
    workflow's event payload, else ``gh api``. None when neither says."""
    payload = _event_payload(env)
    owner = ((payload or {}).get("repository") or {}).get("owner")
    if isinstance(owner, dict) and isinstance(owner.get("type"), str):
        return owner["type"].lower() == "organization"
    repo = repo or env.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    try:
        proc = run(["gh", "api", f"users/{repo.split('/', 1)[0]}", "--jq", ".type"],
                   capture_output=True, text=True, timeout=30, env=dict(env), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    kind = (proc.stdout or "").strip().lower()
    return kind == "organization" if kind in ("organization", "user") else None


def usable(adapter: Adapter, env: Mapping[str, str], org: bool | None) -> tuple[bool, str]:
    """The credential rule on top of ``available``: (usable for CI, why not)."""
    ok, why = adapter.available(env)
    if not ok:
        return False, why
    if adapter.credential(env).kind == SUBSCRIPTION and org is not False:
        who = "an organization" if org else "unknown (treated as an organization)"
        return False, (f"its only credential is a personal subscription and the repository owner is "
                       f"{who}: use an API key or a cloud sign-in")
    return True, ""


def providers_available(adapters: Mapping[str, Adapter], env: Mapping[str, str],
                        org: bool | None) -> list[str]:
    return sorted(name for name, a in adapters.items() if usable(a, env, org)[0])


def doctor(adapters: Mapping[str, Adapter], env: Mapping[str, str], org: bool | None) -> list[dict]:
    """One row per provider: the CLI, the credential names and kind, and the
    credential rule's answer. Values never appear."""
    rows = []
    for name, a in adapters.items():
        cred = a.credential(env)
        ok, why = usable(a, env, org)
        rows.append({
            "provider": name,
            "cli": a.cli,
            "cli_path": a.cli_path(env) if a.cli else None,
            "installed": a.installed(env),
            "credential_names": list(cred.names),
            "credential_kind": cred.kind,
            "usable": ok,
            "reason": why,
        })
    return rows


def format_doctor(rows: list[dict], repo: str | None, org: bool | None) -> str:
    owner = {True: "an organization", False: "a personal account"}.get(org, "unknown; treated as an organization")
    lines = [f"repository: {repo or '(unknown)'}, owner is {owner}"]
    for r in rows:
        if r["cli"]:
            cli = f"cli {r['cli_path']}" if r["cli_path"] else f"cli {r['cli']} missing"
        else:
            cli = "no cli (brindle's own loop)"
        names = ", ".join(r["credential_names"]) or "none"
        kind = f" ({r['credential_kind']})" if r["credential_kind"] else ""
        verdict = "usable for CI" if r["usable"] else f"not usable: {r['reason']}"
        lines.append(f"{r['provider']}: {cli}; credentials: {names}{kind}; {verdict}")
    return "\n".join(lines)


__all__ = ["Adapter", "AdapterError", "ClaudeAdapter", "CodexAdapter", "Credential", "NativeAdapter",
           "Review", "default_adapters", "doctor", "format_doctor", "merge_usage", "providers_available",
           "repo_is_org", "usable", "claude_env", "drop_empty_keys", "API_KEY", "CLOUD", "ENDPOINT",
           "SUBSCRIPTION"]

