"""Can this provider and model take work now: one answer for routing, the
reviewer check and the supervisor switch.

``check`` reads what brindle already knows (``providers.unusable`` for
install and sign-in, ``model_access`` for a model refused within the hour,
``quota`` for subscription windows and limits) and adds the short-lived
states only this module records:

- ``rate_limited``: the provider answered 429; out for a few minutes.
- ``erroring``: ERROR_COUNT turn-ending provider errors within
  ERROR_SPAN_SECONDS with no success between; out for ERROR_OUT_SECONDS.
- ``closing``: the cutoff is imminent. The window resets more than
  CLOSING_MIN_RESET_SECONDS away, and the CLI showed its approaching-limit
  notice, or a live window reads CLOSING_PERCENT or more, or the readings
  of the last PROJECTION_SPAN_SECONDS project 100% within
  PROJECTION_AHEAD_SECONDS.

Those live in ``provider_health.json`` beside ``quota.json``, keyed by
subscription identity (``subscription_usage.cache_key``) and written
atomically under a lock, so every brindle process of one home sees the same
answer. A one-model cap (a quota window with a ``name``) limits that model
only. Every function takes ``now`` so tests run on a fake clock.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from brindle import quota, subscription_usage
from brindle.config import brindle_home

OK = "ok"
NOT_INSTALLED = "not_installed"
SIGNED_OUT = "signed_out"
REFUSED = "refused"
LIMITED = "limited"
RATE_LIMITED = "rate_limited"
ERRORING = "erroring"
CLOSING = "closing"

RATE_LIMIT_SECONDS = 5 * 60
ERROR_COUNT = 3
ERROR_SPAN_SECONDS = 10 * 60
ERROR_OUT_SECONDS = 10 * 60
CLOSING_PERCENT = 97.0
CLOSING_MIN_RESET_SECONDS = 20 * 60
PROJECTION_SPAN_SECONDS = 10 * 60
PROJECTION_AHEAD_SECONDS = 3 * 60
NOTICE_SECONDS = 10 * 60      # how long a CLI's approaching-limit notice stands
FULL_PERCENT = 100.0


@dataclass(frozen=True)
class Health:
    state: str
    until: float | None = None   # when it lifts, where known
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.state == OK


_OK = Health(OK)


def _clock() -> float:
    return time.time()


# -- the store ---------------------------------------------------------------------


def path():
    return brindle_home() / "provider_health.json"


def _key(provider: str) -> str:
    # No Claude identity (an explicit token, no login yet): the provider's own name.
    return subscription_usage.cache_key(provider) or provider


def _load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    p = path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _locked():
    """The whole store, read under an exclusive lock and written back on exit."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(p.name + ".lock"), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = _load()
            yield data
            _save(data)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _entry(data: dict, key: str) -> dict:
    e = data.get(key)
    if not isinstance(e, dict):
        e = data[key] = {}
    return e


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# -- recording ---------------------------------------------------------------------


def record_rate_limited(provider: str, *, now: float | None = None,
                        seconds: float | None = None) -> float:
    """``provider`` answered 429: out until the returned time."""
    now = _clock() if now is None else now
    until = now + (seconds if seconds and seconds > 0 else RATE_LIMIT_SECONDS)
    with _locked() as data:
        e = _entry(data, _key(provider))
        e["rate_limited_until"] = max(until, _num(e.get("rate_limited_until")) or 0)
    return until


def record_error(provider: str, *, now: float | None = None) -> bool:
    """A turn ended on a provider error. True when that makes it ``erroring``."""
    now = _clock() if now is None else now
    with _locked() as data:
        e = _entry(data, _key(provider))
        errors = [t for t in e.get("errors", []) if (_num(t) or 0) > now - ERROR_SPAN_SECONDS]
        errors.append(now)
        if len(errors) >= ERROR_COUNT:
            e["erroring_until"] = now + ERROR_OUT_SECONDS
            errors = []
        e["errors"] = errors
        return not errors


def record_success(provider: str, *, now: float | None = None) -> None:
    """A turn finished: the error run is broken (an ``erroring`` spell stands)."""
    with _locked() as data:
        e = data.get(_key(provider))
        if isinstance(e, dict) and e.get("errors"):
            e["errors"] = []


def record_notice(provider: str, *, now: float | None = None) -> None:
    """The CLI showed its approaching-limit notice."""
    now = _clock() if now is None else now
    with _locked() as data:
        _entry(data, _key(provider))["notice_at"] = now


def record_reading(provider: str, used: float, resets_at: float | None, *,
                   now: float | None = None, key: str | None = None) -> None:
    """A reading of ``provider``'s fullest plan window, kept for the
    projection. A reading below the last one (the window reset) starts over."""
    now = _clock() if now is None else now
    with _locked() as data:
        e = _entry(data, key or _key(provider))
        kept = [r for r in e.get("readings", [])
                if isinstance(r, list) and len(r) == 3 and (_num(r[0]) or 0) > now - PROJECTION_SPAN_SECONDS]
        if kept and (used < kept[-1][1] or (resets_at or 0) != (kept[-1][2] or 0)):
            kept = []
        if kept and kept[-1][1] == used and now - kept[-1][0] < 60:
            return
        kept.append([now, used, resets_at])
        e["readings"] = kept


# -- reading -----------------------------------------------------------------------


_NOTICE = re.compile(r"approaching (?:your |the )?(?:usage |rate )?limit"
                     r"|close to (?:your |the )?(?:usage |rate |session |weekly )?limit"
                     r"|\b(?:9\d)% of your (?:\w+ )?(?:usage )?limit", re.I)


def is_notice(text: str | None) -> bool:
    """Whether ``text`` is a CLI's approaching-limit notice."""
    return bool(text and _NOTICE.search(text))


def _cap_applies(name: str, model: str | None) -> bool:
    """Whether the one-model cap ``name`` ("Fable", "Gemini Pro 5h") is ``model``'s."""
    if not model:
        return False
    base = re.sub(r"\s+(5h|wk|mo|use|\d+m)$", "", name.strip().lower())
    words = [w for w in re.split(r"[\s_-]+", base) if w]
    flat = model.lower()
    return bool(words) and all(w in flat for w in words)


def _projected_full(readings: list, now: float) -> bool:
    live = [r for r in readings if isinstance(r, list) and len(r) == 3
            and (_num(r[0]) or 0) > now - PROJECTION_SPAN_SECONDS]
    if len(live) < 2:
        return False
    (t0, u0, _), (t1, u1, _) = live[0], live[-1]
    if t1 <= t0 or u1 <= u0:
        return False
    rate = (u1 - u0) / (t1 - t0)
    return u1 + rate * (now - t1 + PROJECTION_AHEAD_SECONDS) >= FULL_PERCENT


def _quota_state(provider: str, model: str | None, now: float, entry: dict) -> Health | None:
    """``limited`` or ``closing`` from the subscription windows, or None."""
    if quota._key_billed(provider):
        return None
    q = quota.get(provider, now=now)
    if q is None:
        return None
    label = quota.NAMES.get(provider, provider)
    if q.limited_until is not None and q.limited_until > now:
        return Health(LIMITED, q.limited_until, f"{label} limit reached")
    windows = [w for w in q.windows if (w.resets_at is None or w.resets_at > now)
               and (w.name is None or _cap_applies(w.name, model))]
    full = [w for w in windows if w.used >= FULL_PERCENT and w.resets_at is not None]
    if full:
        w = max(full, key=lambda w: w.resets_at)
        return Health(LIMITED, w.resets_at, f"{label} {w.name} limit reached" if w.name
                      else f"{label} limit reached")
    notice = (_num(entry.get("notice_at")) or 0) > now - NOTICE_SECONDS
    readings = [r for r in entry.get("readings", []) if isinstance(r, list) and len(r) == 3]
    projected = _projected_full(readings, now)
    # Each window on its own: one that lifts within CLOSING_MIN_RESET_SECONDS
    # (or whose reset is unknown) is waited for, and never hides another.
    for w in sorted(windows, key=lambda w: -w.used):
        if w.resets_at is None or w.resets_at - now <= CLOSING_MIN_RESET_SECONDS:
            continue
        tracked = w.name is None and projected and readings[-1][2] == w.resets_at
        if notice or w.used >= CLOSING_PERCENT or tracked:
            return Health(CLOSING, w.resets_at, f"{label} close to its limit ({w.used:.0f}%)")
    return None


def limit(provider: str, model: str | None = None, *, now: float | None = None) -> Health:
    """The quota side of ``check``: ``limited``, ``rate_limited``,
    ``erroring`` or ``closing``, else ``ok``. Nothing about install, sign-in
    or refusals, so it is cheap."""
    now = _clock() if now is None else now
    entry = _load().get(_key(provider))
    entry = entry if isinstance(entry, dict) else {}
    q = _quota_state(provider, model, now, entry)
    if q is not None and q.state == LIMITED:
        return q
    label = quota.NAMES.get(provider, provider)
    until = _num(entry.get("rate_limited_until"))
    if until and until > now:
        return Health(RATE_LIMITED, until, f"{label} is rate limited")
    until = _num(entry.get("erroring_until"))
    if until and until > now:
        return Health(ERRORING, until, f"{label} keeps failing turns")
    return q or _OK


def limited_until(provider: str, model: str | None = None, *, now: float | None = None) -> float | None:
    """When ``provider``'s (or ``model``'s) limit lifts, or None while in quota."""
    h = limit(provider, model, now=now)
    return h.until if h.state == LIMITED else None


def check(provider: str, *, model: str | None = None, profile: str | None = None,
          repo_root: str | None = None, env: dict[str, str] | None = None,
          installed: bool = True, now: float | None = None) -> Health:
    """Whether ``provider`` (running ``model``, for ``profile``) can take work
    now. ``installed=False`` skips the install and sign-in probe for callers
    that check it themselves."""
    if profile is not None:
        from brindle import model_access

        denied = model_access.refused(profile, repo_root)
        if denied:
            return Health(REFUSED, None, f"model refused for this account ({denied.cause}; {denied.fix})")
    if installed:
        from brindle import providers

        why = providers.unusable(provider, env)
        if why:
            return Health(SIGNED_OUT, None, why) if providers.signed_out(provider, env) \
                else Health(NOT_INSTALLED, None, why)
    return limit(provider, model, now=now)
