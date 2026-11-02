"""One redaction pass for agent and tool text brindle stores or sends.

:func:`mask` replaces, with :data:`REDACTED`:

- exact token formats: GitHub (``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``,
  ``github_pat_``), GitLab ``glpat-``, Slack ``xox[abprs]-``, AWS access key
  ids (``AKIA``/``ASIA``), Google ``AIza``, Anthropic/OpenAI ``sk-``, JWTs,
  SonarQube ``sqp_``/``squ_``/``sqa_``, Atlassian ``ATATT``, private key
  blocks, and the credential of an ``Authorization: Bearer``/``Basic`` header;
- a high-entropy value only when a secret-style label names it
  (``API_KEY=``, ``token:``, ``SECRET=``, ``password=``): bare hex, base64,
  commit SHAs and UUIDs pass through;
- the actual values of secret-named environment variables in brindle's own
  environment (:func:`env_values`), wherever they appear.

It is applied where brindle writes or uploads text (the list, with each call
site, is docs/investigations/secret-exposure.md), never to code, diffs,
worktree files or commits. Nothing here logs, and nothing reports what was
masked.
"""

from __future__ import annotations

import math
import os
import re
import sys
from typing import Any

from brindle import secrets

REDACTED = "[redacted]"

_TOKEN = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)"
    r"|(?<![\w-])(?:"
    r"gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|sq[pua]_[A-Za-z0-9]{30,}"
    r"|ATATT[A-Za-z0-9_=-]{20,}"
    r")(?![\w-])", re.DOTALL)
# A credential has a digit in it: "basic functionality" is prose.
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)(\s+)(?=[A-Za-z._~+/-]*\d)([A-Za-z0-9._~+/-]{8,}=*)")
# A secret-style label, then its value. The value is masked only when it looks
# like a secret (see _secret_like), so "token: 1200" or "password: hunter" stay.
_LABELLED = re.compile(
    r"(?i)(\b[\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|secret|token|passw(?:or)?d|passphrase"
    r"|credential|(?<![a-z])key)[\w.-]*[\"']?\s*[:=]\s*[\"']?)([^\s'\"`<>,;]{8,})")
_SECRET_NAME = re.compile(r"(?i)token|secret|passw|passphrase|api_?key|access_?key|private_?key"
                          r"|credential|(?:^|_)pat$|(?:^|_)key$")
_MIN_ENV_VALUE = 8


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _secret_like(value: str) -> bool:
    """A labelled value worth masking: a letter and a digit, no URL or path,
    and enough variety that it isn't a word, a number or a repeated filler."""
    if "://" in value or value.startswith(("/", "~", ".")):
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
        return False
    return _entropy(value) >= 3.0


def env_values(environ=None) -> list[str]:
    """The values of secret-named variables in ``environ`` (default: brindle's
    own environment), longest first. Short values and paths are left out: they
    would mask ordinary text."""
    env = os.environ if environ is None else environ
    named = {*secrets.SECRET_ENV, *secrets.PERSONAL_KEYS}
    found = set()
    for name, value in env.items():
        if not value or len(value) < _MIN_ENV_VALUE or value.startswith(("/", "~")):
            continue
        if name in named or _SECRET_NAME.search(name):
            found.add(value)
    return sorted(found, key=len, reverse=True)


def mask(text: str) -> str:
    """``text`` with every secret it holds replaced by REDACTED."""
    if not isinstance(text, str) or not text:
        return text
    for value in env_values():
        if value in text:
            text = text.replace(value, REDACTED)
    text = _TOKEN.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    return _LABELLED.sub(lambda m: m.group(1) + REDACTED if _secret_like(m.group(2)) else m.group(0), text)


def mask_data(value: Any) -> Any:
    """``value`` (JSON-shaped data) with :func:`mask` applied to every string
    in it; keys and non-string values are kept."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: mask_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_data(v) for v in value]
    return value


class MaskingStream:
    """A text stream that masks what is written to it, a line at a time (so a
    token split across two writes is still caught), before passing it on."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        if "\n" in self._pending:
            done, _, self._pending = self._pending.rpartition("\n")
            self._stream.write(mask(done + "\n"))
        return len(text)

    def flush(self) -> None:
        if self._pending:
            self._stream.write(mask(self._pending))
            self._pending = ""
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def guard_std() -> None:
    """Mask everything this process writes to stdout and stderr from now on:
    the daemon's stderr is app-daemon.log, ``_ended``'s is hooks.log."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, MaskingStream):
            setattr(sys, name, MaskingStream(stream))
