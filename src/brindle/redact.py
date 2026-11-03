"""One redaction pass for agent and tool text brindle stores or sends.

:func:`mask` replaces, with :data:`REDACTED`:

- exact token formats: GitHub (``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``,
  ``github_pat_``), GitLab ``glpat-``, Slack ``xox[abprs]-``, AWS access key
  ids (``AKIA``/``ASIA``), Google ``AIza``, Anthropic/OpenAI ``sk-``, JWTs,
  SonarQube ``sqp_``/``squ_``/``sqa_``, Atlassian ``ATATT``, Stripe
  ``sk_live_``/``rk_live_``, npm ``npm_``, Hugging Face ``hf_``, PyPI
  ``pypi-``, Slack webhook URLs and private key blocks;
- the password in a URL (``postgres://user:pw@host``);
- the credential of any ``Authorization:`` header, and a ``Bearer``/``Basic``
  credential elsewhere when it has a digit in it (so prose stays);
- a high-entropy value only when a secret-style label names it
  (``API_KEY=``, ``token:``, ``SECRET=``, ``password=``): bare hex, base64,
  commit SHAs and UUIDs pass through;
- the actual values of secret-named environment variables in brindle's own
  environment (:func:`env_values`), wherever they appear as a whole word,
  unless the value can't be one (``false``, ``8080``, ``1.2.3``).

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
import functools
import re
import sys
import threading
from typing import Any

from brindle import secrets

REDACTED = "[redacted]"

_KEY_HOLD = 16 * 1024   # a private key block is held (and masked to its END) this far at most
# A line end in text, or written as an escape (a key in JSON).
_SEP = r"(?:\r?\n|\\r\\n|\\n)"
# What may come before each line of a key shown as output: a line number
# (cat -n, grep -n, the Read tool, "[12] ", "  2  ") or a log timestamp, date optional.
_GUTTER = (r"(?:[ \t]*(?:\d+[\t:|\u2192]|\[\d+\]|\d+(?=[ \t]))[ \t]*"
           r"|[ \t]*\[?(?:\d{4}-\d\d-\d\d[T ])?\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\]?[ \t]*)")
# The lines of a PEM body: base64, its Proc-Type/DEK-Info headers, blank lines
# (not the line end after the last one).
_PEM_LINES = (rf"(?:[ \t]*{_SEP}{_GUTTER}?[ \t]*(?:Proc-Type:[^\n\\]*|DEK-Info:[^\n\\]*|[A-Za-z0-9+/=]+|(?={_SEP}))"
              rf"[ \t]*(?=\r?\n|\\[rn]|[\"']|\Z))*")
# A key block runs to its END within _KEY_HOLD, or, with no END, over the
# PEM body that follows it: never over the prose after it.
_TOKEN = re.compile(
    rf"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----(?:.{{0,{_KEY_HOLD}}}?-----END (?:[A-Z]+ )*PRIVATE KEY-----|{_PEM_LINES})"
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
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|npm_[A-Za-z0-9]{36}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}"
    r"|https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]{20,}"
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
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]*:)([^\s/@]+)(@)")
# What a stream must hold until more text comes: a header, label or scheme whose
# credential is still to come (mask finds it on the next line), or a private key
# block not yet ended.
_OPEN = re.compile(rf"(?i)(?:{_AUTH_PREFIX}|\b(?:bearer|basic)|{_LABEL_PREFIX})\s*\Z")
_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
# What may follow a BEGIN line while its key block is still to end: PEM body,
# then the line still being written (a stream passes on whole lines only).
_KEY_OPEN = re.compile(rf"{_PEM_LINES}(?:[ \t]*\r?\n[^\n]*)?")
# Where a stream that gave up on a key block picks up again: past its END, or
# at the first whole line that is not PEM body.
_KEY_RESUME = re.compile(rf"(?m)-----END (?:[A-Z]+ )*PRIVATE KEY-----"
                         rf"|^(?!{_GUTTER}?[ \t]*(?:Proc-Type:|DEK-Info:|-----END |[A-Za-z0-9+/=]*[ \t]*(?:\r?\n|\Z)))")
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
# An env value that can't be a secret: a switch, a short number (a port, a
# count) or a version. Anything else in a secret-named variable is masked.
_PLAIN = re.compile(r"(?i)true|false|yes|no|on|off|none|null|[-+]?\d{1,5}|v?\d+(?:\.\d+)+")
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


def _env_secret(value: str) -> bool:
    """An env value worth masking wherever it appears: anything but a switch,
    a short number or a version."""
    value = value.strip()
    return bool(value) and not _PLAIN.fullmatch(value)


@functools.lru_cache(maxsize=8)
def _env_pattern(values: tuple[str, ...]) -> re.Pattern[str] | None:
    """The env values, longest first, each matched wherever it appears
    (``-phunter2``, ``x<token>``) unless it is a plain word, letters in one
    case: that only as a whole word, so "postgres" is not masked inside
    "postgresql"."""
    if not values:
        return None
    return re.compile("|".join(
        rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])" if v.isalpha() and (v.islower() or v.isupper()) else re.escape(v)
        for v in values))


def env_values(environ=None) -> list[str]:
    """The values of secret-named variables in ``environ`` (default: brindle's
    own environment), longest first. A variable named for a path (``*_FILE``,
    ``*_SOCK`` ...) or a switch (``*_ENABLED``) is left out, and so is a value
    that can't be a secret (see _env_secret)."""
    env = os.environ if environ is None else environ
    named = {*secrets.SECRET_ENV, *secrets.PERSONAL_KEYS}
    found = set()
    for name, value in env.items():
        if not _env_secret(value):
            continue
        if name in named or (_SECRET_NAME.search(name) and not _PATH_NAME.search(name)
                             and not _FLAG_NAME.search(name)):
            found.add(value)
    return sorted(found, key=len, reverse=True)


def mask(text: str) -> str:
    """``text`` with every secret it holds replaced by REDACTED."""
    if not isinstance(text, str) or not text:
        return text
    env = _env_pattern(tuple(env_values()))
    if env:
        text = env.sub(REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
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
    found = [m.span() for p in (_TOKEN, _URL_PASSWORD, _AUTHORIZATION, _AUTH_SCHEME, _LABELLED) for m in p.finditer(text)]
    env = _env_pattern(tuple(env_values()))
    if env:
        found += [m.span() for m in env.finditer(text)]
    return found


def _open_key(text: str) -> int:
    """Where in ``text`` a private key block that has not ended begins, or -1:
    a BEGIN line followed by nothing but PEM body. Prose after it means the
    block won't end (mask covers the body), so nothing is held for it."""
    begin = None
    for begin in _KEY_BEGIN.finditer(text):
        pass
    return begin.start() if begin and _KEY_OPEN.fullmatch(text, begin.end()) else -1


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
    of the line, or of the credential it was waiting on. A key block is held
    only while PEM body follows it, and ``_KEY_HOLD`` at most: then it is
    passed on masked and the rest of its body dropped. :meth:`finish` is the
    end of the stream (at exit) and passes on everything, masked. Writes from
    several threads are taken one at a time."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.RLock()
        self._pending = ""
        self._dropping: re.Pattern[str] | None = None   # what ends what is being dropped
        self._dropped = ""                               # the tail of it, for an end split across writes

    def write(self, text: str) -> int:
        with self._lock:
            return self._write(text)

    def _write(self, text: str) -> int:
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
        key = _open_key(self._pending)
        if key >= 0 and len(self._pending) - key > _KEY_HOLD:   # a key block that won't end
            self._stream.write(mask(self._pending))
            self._dropping, self._pending = _KEY_RESUME, ""
        elif len(self._pending) > 4 * _HOLD_MAX:     # can't be split safely: fail closed
            self._stream.write(REDACTED)
            self._dropping = _CREDENTIAL_END if _open_credential(self._pending) >= 0 else _LINE_END
            self._pending = ""
        return n

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()     # what is held stays held

    def finish(self) -> None:
        with self._lock:
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
