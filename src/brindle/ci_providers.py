"""The agent CLIs a brindle CI run can route to, and ``brindle ci doctor``.

``brindle ci init`` writes a workflow whose ``run`` job installs one CLI per
provider brindle routes to, so routing and learning can pick among them:

- Claude Code, always (the baseline: ``ANTHROPIC_API_KEY``).
- Codex, when ``OPENAI_API_KEY`` or ``CODEX_API_KEY`` is set as a secret. The
  key is handed to ``codex login --with-api-key`` on stdin, so the CLI brindle
  starts in tmux finds a stored login instead of its sign-in screen. The
  install step gets no key; only the separate sign-in step does. ``codex
  login`` stores the key in ``~/.codex/auth.json`` on the run machine, where
  agents can read it just as they can read ``ANTHROPIC_API_KEY``: use a key
  scoped to CI.

Antigravity is left out: it takes only ``GEMINI_API_KEY``, and only with
``modelProvider: "gemini"`` in its settings, which its CLI can't be given
headlessly yet.

Nothing here forces a model: a profile whose CLI isn't installed or signed in
isn't offered (``providers.unusable``), so the run's one baseline profile
stays the default and learning picks among the providers that are installed.

Each key goes only to the step that needs it, in the ``run`` job, and a step
whose secret is missing is skipped (``if:`` on a detection step's output; a
GitHub ``if:`` can't read secrets).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CIProvider:
    name: str
    label: str              # how a person names it
    binary: str             # the CLI on PATH
    package: str            # its npm package
    keys: tuple[str, ...]   # secrets it takes; any one is enough
    optional: bool          # installed only when one of ``keys`` is set


CLAUDE = CIProvider("claude", "Claude Code", "claude", "@anthropic-ai/claude-code",
                    ("ANTHROPIC_API_KEY",), optional=False)
CODEX = CIProvider("codex", "Codex", "codex", "@openai/codex",
                   ("OPENAI_API_KEY", "CODEX_API_KEY"), optional=True)
PROVIDERS = (CLAUDE, CODEX)

# Reported by `brindle ci doctor` too, though not installed by `ci init`.
ANTIGRAVITY = CIProvider("antigravity", "Antigravity", "agy", "", ("GEMINI_API_KEY",),
                         optional=True)

# Credentials a CLI takes from the environment besides the API key (see
# providers._ENV_AUTH); doctor names which are set, never their values.
EXTRA_AUTH = {"claude": ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")}

ENTITLEMENT_REL = Path("brindle-entitlement") / "entitlement.jwt"
TOKEN_ENV = "BRINDLE_PRO_TOKEN"


def _secret(key: str) -> str:
    return "${{ secrets.%s }}" % key


def _env_block(keys, indent: str) -> str:
    return "".join(f"\n{indent}  {k}: {_secret(k)}" for k in keys)


def workflow_steps() -> str:
    """The ``run`` job's install steps, as workflow YAML (6-space step indent)
    with no trailing newline, for ``ci.WORKFLOW``'s ``{providers}`` slot."""
    optional = [p for p in PROVIDERS if p.optional]
    lines = []
    for p in PROVIDERS:
        if not p.optional:
            lines.append(f"      - name: Install {p.label}\n"
                         f"        run: npm install -g {p.package}")
    if optional:
        # Says only whether each provider has a key; the values stay in env.
        checks = "\n".join(
            f'          if [ -n "{"".join("$" + k for k in p.keys)}" ]; then '
            f'echo "{p.name}=true" >> "$GITHUB_OUTPUT"; '
            f'else echo "{p.name}: no {" or ".join(p.keys)} secret, skipping {p.label}"; fi'
            for p in optional)
        keys = [k for p in optional for k in p.keys]
        lines.append("      # Optional providers: installed only when their secret is set.\n"
                     "      - name: Which agent CLIs to install\n"
                     "        id: providers\n"
                     f"        env:{_env_block(keys, '        ')}\n"
                     "        run: |\n"
                     f"{checks}")
    for p in optional:
        lines.append(_install_optional(p))
    return "\n".join(lines)


def _install_optional(p: CIProvider) -> str:
    when = f"        if: steps.providers.outputs.{p.name} == 'true'\n"
    # No key env here: npm's install scripts never see the keys.
    install = (f"      - name: Install {p.label}\n" + when
               + f"        run: npm install -g {p.package}")
    if p is not CODEX:
        return install
    # On stdin, never argv: the stored login is what the CLI brindle starts reads.
    return install + (f"\n      - name: Sign in to {p.label}\n" + when
                      + f"        env:{_env_block(p.keys, '        ')}\n"
                      + "        run: printf '%s' \"${CODEX_API_KEY:-$OPENAI_API_KEY}\" "
                        "| codex login --with-api-key")


# -- brindle ci doctor --------------------------------------------------------------


def _is_set(environ, key: str) -> bool:
    return bool(environ.get(key))


def default_entitlement(environ=None) -> Path | None:
    """Where the workflow puts the entitlement: ``$RUNNER_TEMP/brindle-entitlement/``."""
    environ = os.environ if environ is None else environ
    temp = environ.get("RUNNER_TEMP")
    return Path(temp) / ENTITLEMENT_REL if temp else None


def doctor(entitlement: str | Path | None = None, environ=None, which=None,
           echo=print) -> int:
    """``brindle ci doctor``: which agent CLIs and keys this CI job has, and
    whether the entitlement file or the CI token is set. Prints names and
    "set"/"not set", never a value. Exits 0 when at least one provider can
    run (its CLI installed and a credential present), else 1."""
    environ = os.environ if environ is None else environ
    which = which or shutil.which
    usable = []
    echo("agent CLIs:")
    for p in (*PROVIDERS, ANTIGRAVITY):
        path = which(p.binary)
        keys = [*p.keys, *EXTRA_AUTH.get(p.name, ())]
        present = [k for k in keys if _is_set(environ, k)]
        login = p is CODEX and (Path(environ.get("HOME", "~")).expanduser()
                                / ".codex" / "auth.json").is_file()
        cred = ", ".join(f"{k} set" for k in present) or ("stored login" if login else
                                                         f"no {' or '.join(p.keys)}")
        installed = f"installed ({path})" if path else "not installed"
        echo(f"  {p.label:<12} {installed}; {cred}")
        if path and (present or login):
            usable.append(p.label)
    echo("brindle Pro:")
    ent = Path(entitlement) if entitlement is not None else default_entitlement(environ)
    if ent is None:
        echo("  entitlement  no file given (pass --entitlement, or run in GitHub Actions)")
    else:
        echo(f"  entitlement  {'present' if ent.is_file() else 'missing'} ({ent})")
    echo(f"  {TOKEN_ENV}  {'set' if _is_set(environ, TOKEN_ENV) else 'not set'}")
    if usable:
        echo(f"routing can pick: {', '.join(usable)}")
        return 0
    echo("no agent CLI can run here: install one and set its key")
    return 1
