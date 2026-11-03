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
_AUTH_PREFIX = r"\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token|digest)\s+)?"
_AUTHORIZATION = re.compile(rf"(?i)({_AUTH_PREFIX})([^\s'\"`<>,;]+)")
# Outside an Authorization header a credential has a digit in it: "basic functionality" is prose.
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)(\s+)(?=[A-Za-z._~+/-]*\d)([A-Za-z0-9._~+/-]{8,}=*)")
# A secret-style label, then its value. The value is masked only when it looks
# like a secret (see _secret_like), so "token: 1200" or "password: hunter" stay.
_LABEL_PREFIX = (r"\b[\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|secret|token|passw(?:or)?d|passphrase"
                 r"|credential|(?<![a-z])key)[\w.-]*[\"']?\s*[:=]\s*[\"']?")
_LABELLED = re.compile(rf"(?i)({_LABEL_PREFIX})([^\s'\"`<>,;]{{8,}})")
# What a stream must hold until more text comes: a header, label or scheme whose
# credential is still to come (mask finds it on the next line), or a private key
# block not yet ended.
_OPEN = re.compile(rf"(?i)(?:{_AUTH_PREFIX}|\b(?:bearer|basic)|{_LABEL_PREFIX})\s*\Z")
_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
_KEY_END = re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY-----")
# Where a stream that gave up on what it held (see MaskingStream) picks up again.
_CREDENTIAL_END = re.compile(r"[^\s'\"`<>,;]+(?=[\s'\"`<>,;])")
_LINE_END = re.compile(r"\n")
_SECRET_NAME = re.compile(r"(?i)token|secret|passw|passphrase|api_?key|access_?key|private_?key"
                          r"|credential|(?:^|_)pat$|(?:^|_)key$")
# A variable named for where a secret is kept (a key file, a socket) holds a
# path, and one named for how (BRINDLE_PRO_CREDENTIAL_STORE=file) a setting:
# neither holds the secret.
_PATH_NAME = re.compile(r"(?i)_(?:path|file|dir|home|sock|socket|config|url"
                        r"|store|backend|mode|type|kind|provider|helper)$")
# A variable named as a switch or a count (MY_TOKEN_ENABLED=1, MAX_OUTPUT_TOKENS)
# holds a setting, not a secret: masking every "1" or "true" would wreck the
# text. Decided by name only; a secret variable's value is masked whatever it is.
_FLAG_NAME = re.compile(r"(?i)(?:^|_)(?:enable|enabled|disable|disabled|use|no|skip|allow|debug"
                        r"|verbose|max|min|limit|count)(?:_|$)")
_SHORT_ENV = 8          # a shorter env value is masked only as a whole word
_HOLD_MAX = 64 * 1024   # an unfinished line longer than this is passed on in part
_KEEP = 4096            # ... all but its last _KEEP characters (or longest secret)


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
    ``*_SOCK`` ...) or a switch (``*_ENABLED``) is left out."""
    env = os.environ if environ is None else environ
    named = {*secrets.SECRET_ENV, *secrets.PERSONAL_KEYS}
    found = set()
    for name, value in env.items():
        if not value:
            continue
        if name in named or (_SECRET_NAME.search(name) and not _PATH_NAME.search(name)
                             and not _FLAG_NAME.search(name)):
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


def _mask_field(key: Any, value: str) -> str:
    """A dict's string value masked as ``key: value`` would be, so its key
    labels it (``{"API_KEY": ...}``, ``{"Authorization": "Bearer ..."}``)."""
    if not isinstance(key, str) or not key or not value:
        return mask(value)
    label = f"{key}: "
    masked = mask(label + value)
    return masked[len(label):] if masked.startswith(label) else mask(value)


def mask_data(value: Any) -> Any:
    """``value`` (JSON-shaped data) with :func:`mask` applied to every string
    in it, a dict value labelled by its key; keys and non-string values are
    kept."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: _mask_field(k, v) if isinstance(v, str) else mask_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_data(v) for v in value]
    return value


def _spans(text: str) -> list[tuple[int, int]]:
    """Where in ``text`` something :func:`mask` would replace lies."""
    found = [m.span() for p in (_TOKEN, _AUTHORIZATION, _AUTH_SCHEME, _LABELLED) for m in p.finditer(text)]
    for value in env_values():
        at = text.find(value)
        while at >= 0:
            found.append((at, at + len(value)))
            at = text.find(value, at + 1)
    return found


def _open_key(text: str) -> int:
    """Where in ``text`` a private key block that has not ended begins, or -1."""
    begins = list(_KEY_BEGIN.finditer(text))
    if begins and not _KEY_END.search(text, begins[-1].end()):
        return begins[-1].start()
    return -1


def _open_credential(text: str) -> int:
    """Where in ``text`` a header or label whose credential is still to come
    begins, or -1."""
    m = _OPEN.search(text, max(0, len(text.rstrip()) - 1024))
    return m.start() if m else -1


def _safe_cut(text: str, cut: int) -> int:
    """``cut`` moved back until masking ``text[:cut]`` on its own masks all
    it would in ``text``: no match lies across it, and what follows it is not
    the start of a multiline secret, key block or credential still to come."""
    spans = _spans(text)
    starts = [at for value in env_values() if "\n" in value
              for at in range(max(0, len(text) - len(value) + 1), len(text)) if value.startswith(text[at:])]
    starts += [at for at in (_open_key(text), _open_credential(text)) if at >= 0]
    while True:
        moved = min([cut, *starts, *(s for s, e in spans if s < cut < e)])
        if moved == cut:
            return cut
        cut = moved


class MaskingStream:
    """A text stream that masks what is written to it before passing it on.

    Text is passed on only as whole lines, and only up to where nothing that
    could be a secret runs on into what is still held: across ``flush`` too,
    since the next write may complete a token (or a multiline secret, or the
    credential after ``Authorization: Bearer``, even on the next line, or a
    private key block until its END line). A line longer than ``_HOLD_MAX`` is
    passed on in part, keeping back its last ``_KEEP`` characters the same
    way; what can't be split safely by four times that is dropped to the end
    of the line, or of the key block or credential it was waiting on. :meth:`finish` is the end of the stream (at
    exit) and passes on everything, masked."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._pending = ""
        self._dropping: re.Pattern[str] | None = None   # what ends what is being dropped
        self._dropped = ""                               # the tail of it, for an end split across writes

    def write(self, text: str) -> int:
        n = len(text)
        if self._dropping:
            seen = self._dropped + text
            end = self._dropping.search(seen)
            if end is None:
                self._dropped = seen[-256:]
                return n
            self._dropping, self._dropped, text = None, "", seen[end.end():]
        self._pending += text
        cut = self._pending.rfind("\n") + 1
        if len(self._pending) - cut > _HOLD_MAX:   # no line end in sight: don't hold it all
            cut = len(self._pending) - max([_KEEP, *map(len, env_values())])
        cut = _safe_cut(self._pending, cut) if cut > 0 else 0
        if cut > 0:
            done, self._pending = self._pending[:cut], self._pending[cut:]
            self._stream.write(mask(done))
        if len(self._pending) > 4 * _HOLD_MAX:     # can't be split safely: fail closed
            self._stream.write(REDACTED)
            self._dropping = (_KEY_END if _open_key(self._pending) >= 0
                              else _CREDENTIAL_END if _open_credential(self._pending) >= 0 else _LINE_END)
            self._pending = ""
        return n

    def flush(self) -> None:
        self._stream.flush()         # what is held stays held

    def finish(self) -> None:
        if self._pending:
            self._stream.write(mask(self._pending))
            self._pending = ""
        self._dropping, self._dropped = None, ""
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
