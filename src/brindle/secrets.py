"""Which environment variables no agent may see, and which a provider needs.

Shared by brindle CI (:mod:`brindle.ci_client` scrubs a job's environment
before any agent starts) and local sessions (:func:`brindle.tmux.new_window`
starts every agent's pane without them, although the pane would otherwise
inherit them from the tmux server). A provider's own sign-in is never
scrubbed, see :func:`provider_credentials`.
"""

from __future__ import annotations

import os
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
                      "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
CLAUDE_CLOUD_CREDENTIALS = (
    # Bedrock
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION",
    "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL",
    # Vertex
    "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_BASE_URL",
    # Foundry
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL",
    "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET",
)
CODEX_CREDENTIALS = ("OPENAI_API_KEY", "CODEX_API_KEY")
ANTIGRAVITY_CREDENTIALS = ("GEMINI_API_KEY",)
_CREDENTIALS = {
    "claude": (*CLAUDE_CREDENTIALS, *CLAUDE_CLOUD_CREDENTIALS),
    "codex": CODEX_CREDENTIALS,
    "antigravity": ANTIGRAVITY_CREDENTIALS,
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


def provider_credentials(provider: str, api_key_env: str | None = None) -> frozenset[str]:
    """The variables ``provider``'s agents sign in with, which a scrub must
    let through: Claude Code's keys, login token and cloud backends (and
    their SDKs' credentials), Codex's keys, agy's key, and a native profile's
    ``api_key_env`` (whatever it is named, unless it is a job secret, which
    no profile can claim: see pane_unset). A CLI's own login (a file under
    its home) needs nothing from the environment."""
    names = set(_CREDENTIALS.get(provider, ()))
    if api_key_env and not is_job_secret(api_key_env):
        names.add(api_key_env)
    return frozenset(names)


def agent_credentials(provider: str, profile: str, repo_root: str | None = None) -> frozenset[str]:
    """provider_credentials for an agent on ``profile`` (its ``api_key_env``,
    when the profile can be loaded)."""
    from brindle.profiles import load_profile

    try:
        api_key_env = load_profile(profile, repo_root).api_key_env
    except (KeyError, ValueError):
        api_key_env = None
    return provider_credentials(provider, api_key_env)


# What any process needs to run, which a profile's ``env_allow`` never takes
# away (see env_allowed).
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM", "TERM_PROGRAM", "LANG",
            "LANGUAGE", "TZ", "TMPDIR", "PWD", "TMUX", "TMUX_PANE", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
BASE_PREFIXES = ("LC_", "BRINDLE_")


def env_allowed(name: str, allow: Iterable[str]) -> bool:
    """Whether ``name`` may reach an agent under a profile's ``env_allow``
    (names or globs): listed, one a process needs (BASE_ENV, ``LC_*``) or
    brindle's own (``BRINDLE_*``). brindle's secrets never pass this way:
    pane_unset scrubs them whatever ``allow`` says."""
    import fnmatch

    if name in BASE_ENV or name.startswith(BASE_PREFIXES):
        return True
    return any(name == a or fnmatch.fnmatchcase(name, a) for a in allow)


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


def pane_deny(deny: Iterable[str], env: Mapping[str, str] = {}) -> list[str]:
    """``deny`` less the federation proxy's token when the pane gets it
    (proxy_exempt)."""
    exempt = proxy_exempt(env)
    return [n for n in deny if n not in exempt]


def pane_unset(names: Iterable[str], keep: Iterable[str] = (),
               allow: Iterable[str] | None = None, deny: Iterable[str] = ()) -> list[str]:
    """Which of the variables ``names`` (everything an agent's pane would
    inherit) it must start without: the job secrets, less ``keep`` (the
    provider's credentials, see provider_credentials). Every name in
    SECRET_ENV is listed whether or not it is set, so a pane never depends on
    the launcher's view of the server's environment being complete.

    A job secret (is_job_secret) is always unset: ``keep`` (which includes a
    profile's ``api_key_env`` and its ``env`` names), ``allow`` and the
    provider never let one through.

    With ``allow`` (a profile's ``env_allow``, brindle Pro guardrails), every
    other inherited name goes too unless env_allowed or kept.

    ``deny`` (see PERSONAL_KEYS) is always listed, kept or not."""
    denied = set(deny)
    kept = {n for n in set(keep) - denied if not is_job_secret(n)}
    found = {n for n in names if is_job_secret(n)} | set(SECRET_ENV)
    if allow is not None:
        allow = list(allow)
        found |= {n for n in names if not env_allowed(n, allow)}
    return sorted({n for n in found if n not in kept} | denied)


__all__ = ["SECRET_ENV", "SECRET_PREFIXES", "is_job_secret", "scrub_secrets",
           "provider_credentials", "agent_credentials", "pane_unset", "BASE_ENV", "env_allowed",
           "PERSONAL_KEYS", "register_proxy", "clear_proxy", "proxy_exempt", "pane_deny"]
