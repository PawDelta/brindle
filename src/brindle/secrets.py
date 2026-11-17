"""Which environment variables no agent may see, and which a provider needs.

Shared by brindle CI (:mod:`brindle.ci_client` scrubs a job's environment
before any agent starts) and local sessions (:func:`brindle.tmux.new_window`
starts every agent's pane without them, although the pane would otherwise
inherit them from the tmux server). A provider's own sign-in is never
scrubbed, see :func:`provider_credentials`.

An agent's pane gets an allowlisted environment (env_allowed, plus a repo's
``agent_env``): a secret-looking name (secret_looking) never reaches it
unless it is its own provider's credential, and with ``agent_env_mode:
enforce`` nothing else off the allowlist does either (report mode, the
default, only logs those names: record_env). The person's own tmux server is
never changed.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterable, Mapping, MutableMapping

# Secrets a brindle process holds that no agent may see: the Pro token and
# GitHub's tokens (a worker commits on its branch; brindle itself pushes and
# opens pull requests).
SECRET_ENV = ("BRINDLE_PRO_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
              "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
              # GitLab CI jobs: the job token, its ID tokens, the credentials GitLab puts in a job,
              # and the project token brindle opens merge requests with.
              "CI_JOB_TOKEN", "CI_JOB_JWT", "CI_JOB_JWT_V1", "CI_JOB_JWT_V2", "CI_REGISTRY_PASSWORD",
              "CI_DEPLOY_PASSWORD", "CI_DEPENDENCY_PROXY_PASSWORD", "CI_BUILD_TOKEN", "CI_REPOSITORY_URL", "BRINDLE_ID_TOKEN", "BRINDLE_GITLAB_TOKEN",
              # the job's cloud ID tokens (brindle writes each to a token file for the cloud's SDK)
              "BRINDLE_AWS_ID_TOKEN", "BRINDLE_GCP_ID_TOKEN", "BRINDLE_AZURE_ID_TOKEN",
              "GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN", "GITLAB_ACCESS_TOKEN",
              # gh also reads this alias of GH_ENTERPRISE_TOKEN
              "GITHUB_ENTERPRISE_TOKEN")
SECRET_PREFIXES = ("GH_",)

# How each CLI signs in from the environment, and what the cloud backends
# Claude Code can run on read (their SDKs' standard variables).
CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                      # where identity federation's credential proxy (ci_federation) listens: the
                      # ANTHROPIC_AUTH_TOKEN beside it is only the proxy's own secret
                      "ANTHROPIC_BASE_URL",
                      "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                      "CLAUDE_CODE_USE_MANTLE",
                      # model pins and regions Claude Code reads on the cloud backends
                      "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                      "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_BEDROCK_REGION_PREFIX",
                      "VERTEX_REGION_CLAUDE_3_5_HAIKU", "VERTEX_REGION_CLAUDE_3_7_SONNET",
                      "VERTEX_REGION_CLAUDE_4_0_OPUS", "VERTEX_REGION_CLAUDE_4_0_SONNET",
                      "VERTEX_REGION_CLAUDE_4_1_OPUS", "VERTEX_REGION_CLAUDE_4_5_SONNET",
                      "VERTEX_REGION_CLAUDE_4_5_HAIKU")
CLAUDE_CLOUD_CREDENTIALS = (
    # Bedrock (the token file and role are brindle CI's keyless sign-in, see ci_federation)
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION",
    "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
    # Vertex
    "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_BASE_URL",
    # Foundry
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL", "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET",
    "AZURE_FEDERATED_TOKEN_FILE",
    # where the cloud SDKs find (or are handed) an identity: secret-looking names, but
    # dropping one changes which identity a pane signs in as (see cloud_login.IDENTITY_ENV)
    "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_DEFAULT_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "AWS_CA_BUNDLE",
    "CLOUDSDK_CONFIG", "CLOUDSDK_CORE_PROJECT", "CLOUDSDK_CORE_ACCOUNT",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
    "AZURE_CONFIG_DIR", "AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_AUTHORITY_HOST",
)
# Claude Code's own TLS client identity (its CLAUDE_CODE_CLIENT_* settings, beside
# CLAUDE_CODE_CLIENT_CERT): what a company's gateway and its remote MCP servers ask of
# every connection. Secret-looking names, so a Claude pane used to start with the
# certificate and without its key, and those servers refused it there and nowhere else.
# Settings, not sign-ins: kept in a Claude pane (pane_keep), never stored or looked up.
CLAUDE_TLS_CLIENT = ("CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE")
CODEX_CREDENTIALS = ("OPENAI_API_KEY", "CODEX_API_KEY")
ANTIGRAVITY_CREDENTIALS = ("GEMINI_API_KEY",)
# OpenCode signs in to whichever provider its model names (see cloud_login.CLOUD_KEYS).
OPENCODE_CREDENTIALS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
                        "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY",
                        "AZURE_OPENAI_API_KEY", *CLAUDE_CLOUD_CREDENTIALS)
_CREDENTIALS = {
    "claude": (*CLAUDE_CREDENTIALS, *CLAUDE_CLOUD_CREDENTIALS),
    "codex": CODEX_CREDENTIALS,
    "antigravity": ANTIGRAVITY_CREDENTIALS,
    "opencode": OPENCODE_CREDENTIALS,
}


def is_job_secret(name: str) -> bool:
    return name in SECRET_ENV or name.startswith(SECRET_PREFIXES)


def scrub_secrets(env: MutableMapping[str, str]) -> list[str]:
    """Remove the job's secrets from ``env`` in place (before any agent
    starts), and Claude key variables set to an empty string, which Claude
    Code would take over the federation token. Returns the names removed."""
    from brindle import ci_adapters   # here: ci_adapters imports providers, which imports tmux

    gone = sorted(k for k in env if is_job_secret(k))
    for k in gone:
        del env[k]
    return sorted(gone + ci_adapters.drop_empty_keys(env))


# Providers that reach a model through a cloud (Bedrock, Vertex, Foundry): their
# panes keep every setting that picks the cloud identity (cloud_login.IDENTITY_ENV).
_CLOUD_PROVIDERS = ("claude", "opencode")


def _cloud_identity() -> frozenset[str]:
    from brindle.cloud_login import IDENTITY_ENV   # here: cloud_login imports more of brindle

    return frozenset(n for n in IDENTITY_ENV if not is_job_secret(n))


def provider_credentials(provider: str, api_key_env: str | None = None) -> frozenset[str]:
    """The variables ``provider``'s agents sign in with, which a scrub must
    let through: Claude Code's keys, login token and cloud backends (and
    their SDKs' credentials), Codex's keys, agy's key, and a native profile's
    ``api_key_env`` (whatever it is named, unless it is a job secret, which
    no profile can claim: see pane_unset). A CLI's own login (a file under
    its home) needs nothing from the environment."""
    names = set(_CREDENTIALS.get(provider, ()))
    if provider in _CLOUD_PROVIDERS:
        names |= _cloud_identity()
    if api_key_env and not is_job_secret(api_key_env):
        names.add(api_key_env)
    return frozenset(names)


def pane_keep(provider: str, credentials: Iterable[str]) -> frozenset[str]:
    """What a pane of ``provider``'s keeps of the secret-looking names it
    inherits: its ``credentials`` and, for Claude Code, its TLS client
    settings (CLAUDE_TLS_CLIENT), which no other provider's pane gets."""
    return frozenset(credentials) | frozenset(CLAUDE_TLS_CLIENT if provider == "claude" else ())


def agent_credentials(provider: str, profile: str, repo_root: str | None = None) -> frozenset[str]:
    """provider_credentials for an agent on ``profile`` (its ``api_key_env``,
    when the profile can be loaded)."""
    from brindle.profiles import load_profile

    try:
        api_key_env = load_profile(profile, repo_root).api_key_env
    except (KeyError, ValueError):
        api_key_env = None
    return provider_credentials(provider, api_key_env)


# What an agent's pane gets without asking: what any process needs to run
# (the shell, locale, terminal, a corporate proxy and its CA bundle, the
# ssh agent), the usual toolchains' homes, brindle's own BRINDLE_* routing,
# and every provider's sign-in variables (see _PROVIDER_PREFIXES). A repo
# or the person adds names with ``agent_env`` in config; a profile's
# ``env_allow`` (brindle Pro guardrails) narrows it instead. A secret-looking
# name (secret_looking) never passes this way, whatever lists it.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "SHLVL", "TERM", "COLORTERM", "TERM_PROGRAM",
            "TERM_PROGRAM_VERSION", "LANG", "LANGUAGE", "TZ", "TMPDIR", "PWD", "EDITOR", "VISUAL", "PAGER",
            "TMUX", "TMUX_PANE", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "all_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "JAVA_HOME", "GOPATH", "GOROOT", "VOLTA_HOME", "CARGO_HOME", "RUSTUP_HOME", "DOCKER_HOST")
BASE_PREFIXES = ("LC_", "BRINDLE_", "PYENV_", "NVM_")
# Every provider's sign-in and backend settings, for any agent: the secret
# ones among them still reach only the provider that signs in with them (see
# pane_unset's ``keep``).
_PROVIDER_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "CLOUDSDK_", "GOOGLE_CLOUD_", "VERTEX_",
                      "OPENAI_", "CODEX_", "GEMINI_", "AZURE_")


def _provider_names() -> frozenset[str]:
    return frozenset(n for names in _CREDENTIALS.values() for n in names) | _cloud_identity()


def env_allowed(name: str, allow: Iterable[str]) -> bool:
    """Whether ``name`` is on an agent's allowlist: one a process needs
    (BASE_ENV, ``LC_*``), brindle's own (``BRINDLE_*``), a provider's
    sign-in (any provider's), or listed in ``allow`` (names or globs: a
    repo's ``agent_env``, a profile's ``env_allow``). Being allowed is not
    enough for a secret-looking name: see pane_unset."""
    import fnmatch

    if (name in BASE_ENV or name.startswith(BASE_PREFIXES) or name.startswith(_PROVIDER_PREFIXES)
            or name in _provider_names()):
        return True
    return any(name == a or fnmatch.fnmatchcase(name, a) for a in allow)


# A name segment (split on "_") that says the value is a secret: one ending
# in a long word (SLACKTOKEN, CLIENTSECRET) or equal to a short one (KEY, not
# MONKEY; PAT, not PATH).
_SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL", "CREDENTIALS",
                 "APIKEY", "PRIVATE", "AUTHORIZATION")
_SECRET_SEGMENTS = frozenset({"KEY", "KEYS", "PAT", "AUTH", "PASS", "PW", "CREDS", "COOKIE"})
# Secret-looking names that hold no secret: the ssh agent's socket (git's
# commit signing needs it) and brindle's own settings.
NOT_SECRET = ("SSH_AUTH_SOCK", "BRINDLE_PRO_CREDENTIAL_STORE", "BRINDLE_CODEX_AUTH")


def secret_looking(name: str) -> bool:
    """Whether ``name`` looks like it holds a secret (SLACK_BOT_TOKEN,
    JIRA_API_TOKEN, GITHUB_PAT, SONAR_AUTH, DB_PASSWORD, ...), or is a job
    secret. No agent's pane gets one unless it is its own provider's
    credential, whatever ``agent_env`` or ``env_allow`` lists."""
    if name in NOT_SECRET:
        return False
    if is_job_secret(name):
        return True
    return any(seg in _SECRET_SEGMENTS or seg.endswith(_SECRET_WORDS) for seg in name.upper().split("_"))


AGENT_ENV_MODES = ("report", "enforce")


# A person's own model credentials: what an org with ``deny_personal_keys``
# (Enterprise managed models) keeps out of every agent pane, whatever the
# provider, ``keep`` or ``env_allow`` say. Cloud sign-ins (AWS, Google, Azure)
# stay: those are how the managed provider is reached.
PERSONAL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY")

# The credential proxy brindle CI's identity federation started (see
# :meth:`brindle.ci_federation.Federation.apply`): the ANTHROPIC_BASE_URL and
# ANTHROPIC_AUTH_TOKEN it gave the run. That token is only the proxy's own
# secret, not a person's key, so ``deny_personal_keys`` lets it through (see
# proxy_exempt). None when there is no proxy.
_proxy: tuple[str, str] | None = None


def register_proxy(base_url: str, token: str) -> None:
    global _proxy
    _proxy = (base_url, token)


def clear_proxy() -> None:
    global _proxy
    _proxy = None


def proxy_exempt(env: Mapping[str, str] = {}) -> dict[str, str]:
    """The proxy's variables an agent pane may keep under ``deny``, or {}.
    Only when the pane would really get the registered proxy's URL and
    token: ``env`` (what the launch sets) over this process's environment, so
    a profile pointing ANTHROPIC_BASE_URL elsewhere (a listener that would
    read a person's key) or setting its own token is not exempt."""
    if _proxy is None:
        return {}
    url, token = _proxy
    effective = {**os.environ, **env}
    if effective.get("ANTHROPIC_BASE_URL") == url and effective.get("ANTHROPIC_AUTH_TOKEN") == token:
        return {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": token}
    return {}


# The org-owned key (Enterprise managed models: ``key_env`` / ``key_helper``)
# mapped into a pane being started, as (variable, value) pairs. In memory only,
# for the length of that one launch (see org_key): never stored, logged or kept.
_org_keys: list[tuple[str, str]] = []
_org_keys_lock = threading.Lock()


@contextlib.contextmanager
def org_key(env: Mapping[str, str]):
    """Register the org key variables in ``env`` for the duration of one pane
    launch, so :func:`org_key_exempt` lets them past ``deny_personal_keys``."""
    pairs = [(k, v) for k, v in env.items() if v]
    with _org_keys_lock:
        _org_keys.extend(pairs)
    try:
        yield
    finally:
        with _org_keys_lock:
            for p in pairs:
                _org_keys.remove(p)


def org_key_exempt(env: Mapping[str, str] = {}) -> dict[str, str]:
    """The org key variables an agent pane may keep under ``deny``, or {}. Only
    a variable the launch sets to exactly the registered org key: a personal
    key under the same name (a different value) is not exempt."""
    with _org_keys_lock:
        registered = list(_org_keys)
    return {k: env[k] for k, v in registered if env.get(k) == v}


def pane_deny(deny: Iterable[str], env: Mapping[str, str] = {}) -> list[str]:
    """``deny`` less the federation proxy's token and the org's key when the
    pane gets them (proxy_exempt, org_key_exempt)."""
    exempt = {**proxy_exempt(env), **org_key_exempt(env)}
    return [n for n in deny if n not in exempt]


def pane_unset(names: Iterable[str], keep: Iterable[str] = (),
               allow: Iterable[str] | None = None, deny: Iterable[str] = (),
               agent_env: Iterable[str] = (), enforce: bool = False) -> list[str]:
    """Which of the variables ``names`` (everything an agent's pane would
    inherit) it must start without: every secret-looking name (secret_looking:
    the job secrets, and a SLACK_BOT_TOKEN or JIRA_API_TOKEN the person's
    shell exported), less ``keep`` (the provider's credentials, see
    provider_credentials, and what the launch sets itself). Every name in
    SECRET_ENV is listed whether or not it is set, so a pane never depends on
    the launcher's view of the server's environment being complete.

    A job secret (is_job_secret) is always unset: ``keep`` (which includes a
    profile's ``api_key_env`` and its ``env`` names), ``allow`` and the
    provider never let one through.

    With ``enforce`` (a repo's ``agent_env_mode: enforce``), every other name
    not on the allowlist (env_allowed with ``agent_env``) goes too; in report
    mode those stay, and allowlist_drops names them. With ``allow`` (a
    profile's ``env_allow``, brindle Pro guardrails) the allowlist is that
    instead of ``agent_env``, enforced in either mode.

    ``deny`` (see PERSONAL_KEYS) is always listed, kept or not."""
    denied = set(deny)
    kept = {n for n in set(keep) - denied if not is_job_secret(n)}
    names = set(names)
    found = {n for n in names if secret_looking(n)} | set(SECRET_ENV)
    if allow is not None:
        allow = list(allow)
        found |= {n for n in names if not env_allowed(n, allow)}
    elif enforce:
        agent_env = list(agent_env)
        found |= {n for n in names if not env_allowed(n, agent_env)}
    return sorted({n for n in found if n not in kept} | denied)


# What each launch's allowlist took out of the pane (enforce) or would have
# (report): names only, never values. Per repo, the latest launch (brindle
# doctor shows it); per agent, its own launch (a worker that exits early is
# told what it went without). Under brindle's home.
ENV_REPORT_FILE = "agent-env.json"
ENV_REPORT_AGENTS = 200


def _env_report_path():
    from brindle.config import brindle_home

    return brindle_home() / ENV_REPORT_FILE


def env_report() -> dict:
    """The recorded launches: {"repos": {root: {"mode", "names", "agent",
    "at"}}, "agents": {id: {"mode", "names"}}}."""
    import json

    try:
        found = json.loads(_env_report_path().read_text())
    except (OSError, ValueError):
        return {"repos": {}, "agents": {}}
    if not isinstance(found, dict):
        return {"repos": {}, "agents": {}}
    return {"repos": dict(found.get("repos") or {}), "agents": dict(found.get("agents") or {})}


def record_env(repo_root: str, agent_id: str, mode: str, names: Iterable[str]) -> None:
    """Record the names one launch's allowlist dropped (``mode`` "enforce")
    or would have ("report"). Serialized with a lock file, written to a
    unique temporary file and moved into place, so concurrent launches
    neither lose each other's entry nor leave a half-written file."""
    import fcntl
    import json
    import tempfile
    import time

    path = _env_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    names = sorted(set(names))
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = env_report()
        report["repos"][repo_root] = {"mode": mode, "names": names, "agent": agent_id, "at": time.time()}
        agents = report["agents"]
        agents.pop(agent_id, None)
        agents[agent_id] = {"mode": mode, "names": names}
        for old in list(agents)[:-ENV_REPORT_AGENTS]:
            del agents[old]
        fd, tmp = tempfile.mkstemp(prefix=".agent-env-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(report, f)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def dropped_for(agent_id: str) -> list[str]:
    """The variables enforce mode took out of ``agent_id``'s pane, or []."""
    found = env_report()["agents"].get(agent_id) or {}
    return list(found.get("names") or []) if found.get("mode") == "enforce" else []


def launch_keep(keep: Iterable[str], env: Mapping[str, str]) -> list[str]:
    """pane_unset's ``keep`` for a launch that sets ``env`` itself: the
    provider's credentials (``keep``) and the ordinary settings ``env`` names,
    but not a secret-looking one that isn't a credential (a profile's
    ``env.SLACK_BOT_TOKEN`` is dropped like an inherited one)."""
    keep = set(keep)
    return sorted(keep | {n for n in env if not secret_looking(n)})


def allowlist_drops(names: Iterable[str], keep: Iterable[str] = (), allow: Iterable[str] | None = None,
                    deny: Iterable[str] = (), agent_env: Iterable[str] = ()) -> list[str]:
    """The names of ``names`` that enforce mode takes out of a pane and
    report mode leaves in: what a launch in report mode logs."""
    names, keep, deny, agent_env = list(names), list(keep), list(deny), list(agent_env)
    enforced = pane_unset(names, keep, allow, deny, agent_env, enforce=True)
    return sorted(set(enforced) - set(pane_unset(names, keep, allow, deny, agent_env)))


__all__ = ["SECRET_ENV", "SECRET_PREFIXES", "is_job_secret", "scrub_secrets",
           "provider_credentials", "agent_credentials", "pane_unset", "BASE_ENV", "env_allowed",
           "PERSONAL_KEYS", "register_proxy", "clear_proxy", "proxy_exempt", "pane_deny",
           "org_key", "org_key_exempt", "secret_looking", "NOT_SECRET", "AGENT_ENV_MODES",
           "allowlist_drops", "record_env", "env_report", "dropped_for", "launch_keep", "pane_keep"]
