"""One redaction pass for agent and tool text brindle stores or sends.

:func:`mask` replaces, with :data:`REDACTED`:

- exact token formats: GitHub (``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``,
  ``github_pat_``), GitLab ``glpat-``, Slack ``xox[abprs]-``, AWS access key
  ids (``AKIA``/``ASIA``), Google ``AIza``, Anthropic/OpenAI ``sk-``, JWTs,
  SonarQube ``sqp_``/``squ_``/``sqa_``, Atlassian ``ATATT`` and private key
  blocks;
- the credential of any ``Authorization:`` header, and a ``Bearer``/``Basic``
  credential elsewhere when it has a digit in it (so prose stays);
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

import atexit
import logging
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
_AUTHORIZATION = re.compile(
    r"(?i)(\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token|digest)\s+)?)"
    r"([^\s'\"`<>,;]+)")
# Outside an Authorization header a credential has a digit in it: "basic functionality" is prose.
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)(\s+)(?=[A-Za-z._~+/-]*\d)([A-Za-z0-9._~+/-]{8,}=*)")
# A secret-style label, then its value. The value is masked only when it looks
# like a secret (see _secret_like), so "token: 1200" or "password: hunter" stay.
_LABELLED = re.compile(
    r"(?i)(\b[\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|secret|token|passw(?:or)?d|passphrase"
    r"|credential|(?<![a-z])key)[\w.-]*[\"']?\s*[:=]\s*[\"']?)([^\s'\"`<>,;]{8,})")
_SECRET_NAME = re.compile(r"(?i)token|secret|passw|passphrase|api_?key|access_?key|private_?key"
                          r"|credential|(?:^|_)pat$|(?:^|_)key$")
# A variable named for where a secret is kept (a key file, a socket) holds a
# path, and one named for how (BRINDLE_PRO_CREDENTIAL_STORE=file) a setting:
# neither holds the secret.
_PATH_NAME = re.compile(r"(?i)_(?:path|file|dir|home|sock|socket|config|url"
                        r"|store|backend|mode|type|kind|provider|helper)$")
# Flag values, not secrets: masking every "1" or "true" would wreck the text.
_FLAG_VALUES = frozenset({"0", "1", "true", "false", "yes", "no", "on", "off"})
_SHORT_ENV = 8          # a shorter env value is masked only as a whole word
_HOLD_MAX = 64 * 1024


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _secret_like(value: str) -> bool:
    """A labelled value worth masking: no URL or path, and random-looking.
    With a digit, enough variety that it isn't a word or a number; without
    one, long, varied and in mixed case the way random text is (a camelCase
    identifier has few capitals)."""
    if "://" in value or value.startswith(("/", "~", ".")):
        return False
    letters = [c for c in value if c.isalpha()]
    if not letters:
        return False
    if any(c.isdigit() for c in value):
        return _entropy(value) >= 3.0
    upper = sum(c.isupper() for c in letters) / len(letters)
    return len(value) >= 16 and _entropy(value) >= 3.5 and 0.2 <= upper <= 0.8


def env_values(environ=None) -> list[str]:
    """The values of secret-named variables in ``environ`` (default: brindle's
    own environment), longest first. A variable named for a path (``*_FILE``,
    ``*_SOCK`` ...) and flag values are left out."""
    env = os.environ if environ is None else environ
    named = {*secrets.SECRET_ENV, *secrets.PERSONAL_KEYS}
    found = set()
    for name, value in env.items():
        if not value or value.strip().lower() in _FLAG_VALUES:
            continue
        if name in named or (_SECRET_NAME.search(name) and not _PATH_NAME.search(name)):
            found.add(value)
    return sorted(found, key=len, reverse=True)


def mask(text: str) -> str:
    """``text`` with every secret it holds replaced by REDACTED."""
    if not isinstance(text, str) or not text:
        return text
    for value in env_values():
        if value not in text:
            continue
        if len(value) >= _SHORT_ENV:
            text = text.replace(value, REDACTED)
        else:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _AUTHORIZATION.sub(lambda m: m.group(1) + REDACTED, text)
    text = _AUTH_SCHEME.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
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
    """A text stream that masks what is written to it before passing it on.

    Text is passed on only as whole lines: an unfinished line is held, across
    ``flush`` too, since the next write may complete a token in it (or the
    credential after ``Authorization: Bearer``). :meth:`finish` is the end of
    the stream (at exit, or when a line grows past ``_HOLD_MAX``) and passes
    on everything, masked."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        if "\n" in self._pending:
            done, _, self._pending = self._pending.rpartition("\n")
            self._stream.write(mask(done + "\n"))
        if len(self._pending) > _HOLD_MAX:   # no line end in sight: don't hold it all
            self.finish()
        return len(text)

    def flush(self) -> None:
        self._stream.flush()         # the unfinished line stays held

    def finish(self) -> None:
        if self._pending:
            self._stream.write(mask(self._pending))
            self._pending = ""
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


class MaskingFormatter(logging.Formatter):
    """A log formatter whose records (traceback included) are masked."""

    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


def guard_std(names: tuple[str, ...] = ("stdout", "stderr")) -> None:
    """Mask everything this process writes to ``names`` (stdout and stderr)
    from now on: the daemon's stderr is app-daemon.log, ``_ended``'s is
    hooks.log, the sidebar's stderr is sidebar.log (its stdout is the curses
    screen, left alone). What is still held back at exit is passed on then."""
    for name in names:
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, MaskingStream):
            guarded = MaskingStream(stream)
            setattr(sys, name, guarded)
            atexit.register(guarded.finish)
