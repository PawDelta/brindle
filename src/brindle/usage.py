"""Token usage summed from a Claude Code session transcript.

Claude Code writes each session as JSONL under
``~/.claude/projects/<cwd-slug>/<session_id>.jsonl``. ``assistant`` entries
carry ``message.usage`` (``input_tokens``, ``output_tokens``,
``cache_read_input_tokens``, ``cache_creation_input_tokens``) and
``message.model``. Its own built-in subagents (the Agent tool) get their own
transcripts alongside it, under ``<session_id>/subagents/*.jsonl``.

Summing is incremental: the ``usage_cache`` table remembers, per file, how
many bytes have already been parsed and the running totals, so a repeated
call only reads new bytes, and costs nothing but a ``stat`` when a file
hasn't grown. A streamed assistant message can appear on more than one JSONL
line (one per content block), each carrying the full, identical ``usage``
for that message, always written back to back; only the first line for a
given message id is counted, so a single ``last_message_id`` per file is
enough to dedupe even across separate incremental calls. A file that shrank
or whose inode changed (rotated or replaced, even by a larger one) is parsed
again from the start.

The model label is the main transcript's latest model; subagents' models
don't override it, and Claude Code's ``<synthetic>`` placeholder is ignored.

Codex has no ``transcript_path``: it writes each session as a "rollout",
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl``, whose
``token_count`` events carry the session's running ``total_token_usage``
(``input_tokens`` including ``cached_input_tokens``, and ``output_tokens``
including reasoning) and whose ``turn_context`` entries carry the model. A
Codex agent's rollout is the one named for its ``session_ref`` (the
``thread-id`` its notify hook reports), else the first one started in its
worktree after the agent was. Its totals are cached in ``usage_cache`` too,
replaced rather than added to as new events arrive. Other providers
(Antigravity, ...) get None.

``usage_cost`` turns a ``Usage`` into dollars at its model's price (see
``brindle.pricing``), or None when that price isn't known. A transcript's
subagents are priced at the main model's rate: the cache keeps one model per
transcript.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from brindle.db import DB

log = logging.getLogger(__name__)

SYNTHETIC_MODEL = "<synthetic>"
CODEX_LOOKBACK_DAYS = 31    # how far back (by rollout date directory) to look for an agent's rollout
CODEX_START_SLACK = 5.0     # seconds a rollout may claim to start before its agent did


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str | None = None

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_creation_tokens + other.cache_creation_tokens,
            other.model or self.model,
        )

    @property
    def total_in(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def total(self) -> int:
        return self.total_in + self.output_tokens


def _parse_new(path: Path, offset: int, last_message_id: str | None) -> tuple[Usage, int, str | None]:
    """Sum the usage of every complete new line in ``path`` from byte
    ``offset``. Returns the delta, the offset just past the last complete
    line (a trailing partial line is left for next time), and the last
    message id seen."""
    delta = Usage()
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except (OSError, ValueError):
        log.warning("brindle: couldn't read transcript %s", path, exc_info=True)
        return delta, offset, last_message_id
    new_offset = offset
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        new_offset += len(line)
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        msg_id = message.get("id")
        if msg_id and msg_id == last_message_id:
            continue
        u = message.get("usage")
        if not isinstance(u, dict):
            continue
        model = message.get("model")
        try:
            delta = delta + Usage(
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
                model=model if isinstance(model, str) and model != SYNTHETIC_MODEL else None,
            )
        except (TypeError, ValueError):
            continue
        if msg_id:
            last_message_id = msg_id
    return delta, new_offset, last_message_id


def _file_usage(db: DB, path: Path) -> Usage | None:
    """``path``'s usage, using (and updating) its cache row. None if the
    file can't be read at all."""
    try:
        st = path.stat()
    except OSError:
        return None
    size, inode = st.st_size, st.st_ino
    cached = db.get_usage_cache(str(path))
    if cached and cached["size"] <= size and cached["inode"] in (None, inode):
        offset = cached["size"]
        base = Usage(cached["input_tokens"], cached["output_tokens"], cached["cache_read_tokens"],
                     cached["cache_creation_tokens"], cached["model"])
        last_id = cached["last_message_id"]
    else:
        # No cache yet, or the file shrank or was replaced: start over.
        offset, base, last_id = 0, Usage(), None
    if size == offset and cached and cached["inode"] == inode:
        return base
    delta, new_offset, last_id = _parse_new(path, offset, last_id)
    total = base + delta
    db.set_usage_cache(str(path), new_offset, total.input_tokens, total.output_tokens,
                       total.cache_read_tokens, total.cache_creation_tokens, total.model, last_id,
                       inode)
    return total


def _subagent_transcripts(main: Path) -> list[Path]:
    d = main.parent / main.stem / "subagents"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def transcript_usage(db: DB, transcript_path: str) -> Usage | None:
    """Usage summed across ``transcript_path`` and any subagent transcripts
    stored alongside it. None if the transcript doesn't exist."""
    main = Path(transcript_path)
    if not main.is_file():
        return None
    total = _file_usage(db, main) or Usage()
    for p in _subagent_transcripts(main):
        u = _file_usage(db, p)
        if u is not None:
            total = total + Usage(u.input_tokens, u.output_tokens, u.cache_read_tokens,
                                  u.cache_creation_tokens)
    return total


def agent_usage(db: DB, agent) -> Usage | None:
    """Usage for a brindle ``Agent``: its transcript's, or for Codex its
    rollout's. None if it has none (another provider, or nothing recorded
    yet)."""
    if agent.provider == "codex":
        try:
            return codex_agent_usage(db, agent)
        except Exception:  # noqa: BLE001 - usage is an extra: unknown rather than a failure
            log.warning("brindle: couldn't read Codex usage for %s", agent.id, exc_info=True)
            return None
    if not agent.transcript_path:
        return None
    return transcript_usage(db, agent.transcript_path)


# -- Codex rollouts ---------------------------------------------------------------------------------


def _codex_sessions(home: Path | None = None) -> Path:
    from brindle.quota import codex_home

    return (home or codex_home()) / "sessions"


def _iso_ts(value) -> float | None:
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _rollout_meta(path: Path) -> dict | None:
    """The ``session_meta`` payload on a rollout's first line, or None."""
    try:
        with path.open("rb") as f:
            entry = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(entry, dict) or entry.get("type") != "session_meta" or not isinstance(payload, dict):
        return None
    return payload


def find_codex_rollout(session_ref: str | None, cwd: str | None, since: float,
                       home: Path | None = None, now: float | None = None) -> Path | None:
    """The rollout of the Codex session ``session_ref``, else the first one
    started in ``cwd`` at or after ``since``. Only ``sessions/`` is read."""
    sessions = _codex_sessions(home)
    if not sessions.is_dir():
        return None
    if session_ref and all(c.isalnum() or c == "-" for c in session_ref):
        found = sorted(sessions.glob(f"*/*/*/rollout-*-{session_ref}.jsonl"))
        if found:
            return found[0]
    if not cwd:
        return None
    want = os.path.realpath(cwd)
    now = time.time() if now is None else now
    best: tuple[float, Path] | None = None
    day = max(since - 86400, now - CODEX_LOOKBACK_DAYS * 86400)   # a day early: dirs are local dates
    while day <= now + 86400:
        d = sessions / time.strftime("%Y/%m/%d", time.localtime(day))
        for f in sorted(d.glob("rollout-*.jsonl")) if d.is_dir() else ():
            meta = _rollout_meta(f)
            started = _iso_ts(meta.get("timestamp")) if meta else None
            if (started is None or started < since - CODEX_START_SLACK
                    or not isinstance(meta.get("cwd"), str)
                    or os.path.realpath(meta["cwd"]) != want):
                continue
            if best is None or started < best[0]:
                best = (started, f)
        day += 86400
    return best[1] if best else None


def _codex_totals(data: bytes, offset: int, current: Usage) -> tuple[Usage, int]:
    """The latest ``total_token_usage`` (and model) in the complete lines of
    ``data``, read from byte ``offset``; ``current`` if there's none."""
    totals, model, new_offset = current, current.model, offset
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        new_offset += len(line)
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        if entry.get("type") == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
            continue
        info = payload.get("info") if payload.get("type") == "token_count" else None
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(total, dict):
            continue
        try:
            inp = int(total.get("input_tokens") or 0)
            cached = int(total.get("cached_input_tokens") or 0)
            written = int(total.get("cache_write_input_tokens") or 0)
            out = int(total.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        # input_tokens counts the cached (and written) ones too: split them out
        totals = Usage(max(0, inp - cached - written), out, cached, written)
    totals.model = model
    return totals, new_offset


def rollout_usage(db: DB, path: Path) -> Usage | None:
    """A Codex rollout's usage so far, using (and updating) its cache row."""
    try:
        st = path.stat()
    except OSError:
        return None
    cached = db.get_usage_cache(str(path))
    if cached and cached["size"] <= st.st_size and cached["inode"] in (None, st.st_ino):
        offset = cached["size"]
        base = Usage(cached["input_tokens"], cached["output_tokens"], cached["cache_read_tokens"],
                     cached["cache_creation_tokens"], cached["model"])
        if offset == st.st_size and cached["inode"] == st.st_ino:
            return base
    else:
        offset, base = 0, Usage()
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        log.warning("brindle: couldn't read Codex rollout %s", path, exc_info=True)
        return base
    total, new_offset = _codex_totals(data, offset, base)
    db.set_usage_cache(str(path), new_offset, total.input_tokens, total.output_tokens,
                       total.cache_read_tokens, total.cache_creation_tokens, total.model, None,
                       st.st_ino)
    return total


def codex_agent_usage(db: DB, agent, home: Path | None = None) -> Usage | None:
    """A Codex agent's usage from its rollout, or None when it can't be
    found (Codex didn't record it here: "unknown")."""
    ws = db.get_workspace(agent.workspace_id)
    path = find_codex_rollout(agent.session_ref, ws.path if ws else None, agent.created_at, home)
    return rollout_usage(db, path) if path else None


# -- dollars ----------------------------------------------------------------------------------------


def usage_cost(u: Usage | None, extra=None) -> float | None:
    """``u`` in US dollars at its model's price (``extra``: ``pricing``
    overrides), or None when the model or its price is unknown."""
    from brindle import pricing

    if u is None:
        return None
    price = pricing.price_for(u.model, extra)
    if price is None:
        return None
    return price.cost(u.input_tokens, u.output_tokens, u.cache_creation_tokens, u.cache_read_tokens)


def format_tokens(n: int) -> str:
    if n >= 1000:
        return f"{(n + 500) // 1000}k"  # round half up, not to even
    return str(n)


def short_model(model: str | None) -> str:
    if not model:
        return "?"
    m = model.lower()
    for name in ("opus", "sonnet", "haiku"):
        if name in m:
            return name
    # For open-weight models, drop provider prefix and tag suffix
    if "/" in model:
        # Drop everything up to the last "/"
        model = model[model.rindex("/") + 1:]
    if ":" in model:
        # Drop everything from the first ":"
        model = model[:model.index(":")]
    return model


def summary_line(u: Usage, dollars: float | None = None) -> str:
    """The one-line summary appended to a forwarded worker/reviewer result,
    e.g. ``tokens: 182k in (160k cached, 20k written) · 9k out · sonnet``,
    with `` · ~$0.42`` at the end when ``dollars`` is given."""
    line = (f"tokens: {format_tokens(u.total_in)} in ({format_tokens(u.cache_read_tokens)} cached, "
            f"{format_tokens(u.cache_creation_tokens)} written) "
            f"· {format_tokens(u.output_tokens)} out · {short_model(u.model)}")
    if dollars is not None:
        from brindle.pricing import money

        line += f" · ~{money(dollars)}"
    return line


def short_summary(u: Usage) -> str:
    """The compact form for the sidebar and `brindle ls`, e.g. ``191k tok``."""
    return f"{format_tokens(u.total)} tok"
