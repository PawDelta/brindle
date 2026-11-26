"""How close each provider is to its subscription limit.

Everything here comes from what the CLIs themselves report: the status line
data Claude Code hands brindle and its ``/usage`` command, Codex's session
rollouts, and Antigravity's ``/usage`` command and limit errors. brindle never
reads a CLI's OAuth token or auth files and never calls a provider's backend.

State lives in ``~/.brindle/quota.json``, scoped to Claude's subscription
identity and otherwise one entry per provider.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brindle import subscription_usage
from brindle.config import RepoConfig, brindle_home

CLAUDE_FRESH_SECONDS = 15 * 60   # the status line only reports while a session runs
CODEX_REFRESH_SECONDS = 30       # how often get() re-reads the newest rollout
ROLLOUT_TAIL_BYTES = 1024 * 1024
DEFAULT_COOLDOWN_MINUTES = {"antigravity": 300}
FALLBACK_COOLDOWN_MINUTES = 300
NAMES = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "native": "Local model"}
PROVIDERS = tuple(NAMES)
# Claude Code's status line names its windows; the rest are told by length.
CLAUDE_WINDOWS = {"five_hour": 300, "seven_day": 10080}
CLAUDE_PLAN_WINDOWS = {
    "five_hour": 300, "session": 300,
    "twenty_four_hour": 1440, "one_day": 1440, "daily": 1440,
    "seven_day": 10080, "weekly": 10080, "seven_day_org": 10080,
    "organization": 10080, "org": 10080, "team": 10080, "workspace": 10080, "seat": 10080,
    "thirty_day": 43200, "monthly": 43200, "month": 43200,
    "spend": 43200, "spend_limit": 43200, "monthly_spend": 43200, "budget": 43200,
}
_CLAUDE_PLAN_SCOPES = frozenset({
    "all models", "all", "organization", "org", "team", "workspace", "seat",
    "enterprise", "company", "extra usage", "spend", "monthly", "included",
})
_CLAUDE_MODEL_WINDOW_PREFIXES = (("seven_day_", 10080), ("five_hour_", 300), ("thirty_day_", 43200))
_CLAUDE_NON_SUBSCRIPTION_VARS = (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BASE_URL",
)
_SPEND_RATIO_PAIRS = (
    ("used_credits", "monthly_limit"), ("used_credits", "credit_limit"),
    ("used_credits", "limit_credits"), ("used_credits", "limit"),
    ("used_usd", "limit_usd"), ("spent_usd", "limit_usd"),
    ("spent", "limit"), ("spent", "budget"),
    ("current_spend", "spend_limit"), ("spend", "spend_limit"),
    ("used_cents", "limit_cents"), ("used", "limit"),
)
_CODEX_PLAN_NAMES = frozenset({
    "codex", "default", "all", "workspace", "organization", "org", "team", "enterprise", "seat",
})


@dataclass
class Window:
    used: float                # percent of the window's allowance
    resets_at: float | None    # epoch seconds
    minutes: int | None = None
    name: str | None = None    # a cap on one model ("Fable"); None for the plan's own window
    overage: bool = False      # optional paid overage (extra_usage / credits): serves work when included is full


@dataclass
class Quota:
    provider: str
    windows: list[Window] = field(default_factory=list)
    limited_until: float | None = None
    updated_at: float = 0.0
    source: str = ""
    overage_unlimited: bool = False
    overage_reset: bool = False


def path() -> Path:
    return brindle_home() / "quota.json"


def _load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _number(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return None


def _num_or_str(v) -> float | None:
    n = _number(v)
    if n is not None:
        return n
    if isinstance(v, str):
        cleaned = v.strip().lstrip("$").replace(",", "")
        if cleaned:
            try:
                parsed = float(cleaned)
                return parsed if math.isfinite(parsed) else None
            except ValueError:
                return None
    return None


def _timestamp(v) -> float | None:
    n = _number(v)
    if n is not None:
        return n if n > 0 else None
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            ts = dt.timestamp()
        except (OverflowError, OSError):
            return None
        return ts if math.isfinite(ts) and ts > 0 else None
    return None


def _reset_from_dict(w: dict, now: float) -> float | None:
    for key in ("resets_at", "reset_at", "reset_time"):
        ts = _timestamp(w.get(key))
        if ts is not None:
            return ts
    for key in ("resets_in_seconds", "reset_after_seconds"):
        delta = _number(w.get(key))
        if delta is not None and delta > 0:
            return now + delta
    return None


def _percent_from_dict(w: dict) -> float | None:
    for key in ("used_percentage", "used_percent", "utilization"):
        n = _number(w.get(key))
        if n is not None and n >= 0:
            return n
    frac = _number(w.get("used_fraction"))
    if frac is not None and 0 <= frac <= 1:
        return frac * 100.0
    rem = _number(w.get("remaining_fraction"))
    if rem is not None and 0 <= rem <= 1:
        return (1.0 - rem) * 100.0
    for used_k, limit_k in _SPEND_RATIO_PAIRS:
        used = _num_or_str(w.get(used_k))
        lim = _num_or_str(w.get(limit_k))
        if used is not None and lim is not None and used >= 0 and lim > 0:
            return max(0.0, (used / lim) * 100.0)
    return None


def record(provider: str, windows: list[Window] | None = None, *,
           limited_until: float | None = None, source: str = "", keep_limit: bool = False,
           identity_key: str | None = None, overage_unlimited: bool | None = None,
           event_seen: dict[str, float] | None = None) -> None:
    """Replace what's known about ``provider``. ``windows=None`` keeps the
    windows it has (for recording only a limit); ``keep_limit`` keeps a
    ``limited_until`` still in the future. ``identity_key`` binds an answer
    to the subscription that was queried, even if the login changes."""
    key = identity_key or subscription_usage.cache_key(provider)
    if key is None:
        return
    data = _load()
    old = data.get(key) if isinstance(data.get(key), dict) else {}
    now = time.time()
    if windows is None:
        ser_windows = old.get("windows", [])
        unlimited = bool(old.get("overage_unlimited")) if overage_unlimited is None else overage_unlimited
    else:
        ser_windows = [
            {
                "used": w.used, "resets_at": w.resets_at, "minutes": w.minutes, "name": w.name,
                **({"overage": True} if w.overage else {}),
            }
            for w in windows
        ]
        unlimited = bool(overage_unlimited)
    entry = {
        "windows": ser_windows,
        "limited_until": limited_until,
        "updated_at": now,
        "source": source or old.get("source", ""),
    }
    if unlimited:
        entry["overage_unlimited"] = True
    if event_seen is not None:
        if event_seen:
            entry["event_seen"] = {k: float(v) for k, v in event_seen.items()}
    elif isinstance(old.get("event_seen"), dict):
        # Preserve first-observed timestamps for untimestamped rollout events
        # even across independent CLI limit errors so re-reading an unchanged
        # rollout never renews relative window resets or inferred cooldowns.
        entry["event_seen"] = old["event_seen"]
    if limited_until is None and keep_limit and (_number(old.get("limited_until")) or 0) > now:
        entry["limited_until"] = old["limited_until"]
    data[key] = entry
    _save(data)
    if windows is not None:
        w = fullest(Quota(provider, windows, overage_unlimited=unlimited), now=now)
        if w is not None:
            # The readings the provider-health projection of a closing window runs on.
            from brindle import provider_health

            try:
                provider_health.record_reading(provider, w.used, w.resets_at, key=key)
            except OSError:
                pass


def record_limit(provider: str, cfg: RepoConfig | None = None, *, now: float | None = None,
                 until: float | None = None, repo_root: str | None = None,
                 env: dict[str, str] | None = None) -> float:
    """The provider just refused work on its limit: it's unavailable until
    ``until`` (when the error gave a future reset time) or for the repo's
    ``limit_cooldown_minutes`` (default 300 for Antigravity)."""
    cur = time.time() if now is None else now
    minutes = getattr(cfg, "limit_cooldown_minutes", None) or DEFAULT_COOLDOWN_MINUTES.get(
        provider, FALLBACK_COOLDOWN_MINUTES)
    parsed_until = _number(until)
    target = parsed_until if parsed_until is not None and parsed_until > cur else cur + minutes * 60
    scope = _route_scope(provider, repo_root=repo_root, env=env)
    key = (subscription_usage.route_limit_key(provider, scope)
           if scope is not None else subscription_usage.limit_key(provider))
    record(provider, None, limited_until=target, source="limit error", identity_key=key)
    return target


def _from_entry(provider: str, e: dict) -> Quota:
    windows = [Window(float(w["used"]), _number(w.get("resets_at")), w.get("minutes"),
                      w.get("name") if isinstance(w.get("name"), str) else None,
                      bool(w.get("overage")))
               for w in e.get("windows", []) if isinstance(w, dict) and _number(w.get("used")) is not None]
    return Quota(provider, windows, _number(e.get("limited_until")),
                 _number(e.get("updated_at")) or 0.0, str(e.get("source") or ""),
                 bool(e.get("overage_unlimited")))


def _route_limit_only(provider: str, data: dict, now: float, *, route: str | None = None) -> Quota | None:
    entry = data.get(subscription_usage.route_limit_key(provider, route))
    if not isinstance(entry, dict):
        return None
    until = _number(entry.get("limited_until"))
    if until is None or until <= now:
        return None
    return Quota(provider, [], until, _number(entry.get("updated_at")) or 0.0,
                 str(entry.get("source") or "limit error"))


def get(provider: str, *, identity_key: str | None = None, now: float | None = None,
        repo_root: str | None = None, env: dict[str, str] | None = None) -> Quota | None:
    """What's known about ``provider`` at ``now`` (default the clock):
    windows past their reset dropped, an expired limit cleared. None when
    nothing current is known. ``identity_key`` keeps a read/modify/write on
    its captured subscription."""
    now = time.time() if now is None else now
    if identity_key is None:
        scope = _route_scope(provider, repo_root=repo_root, env=env)
        if scope is not None:
            return _route_limit_only(provider, _load(), now, route=scope)
    if provider == "codex":
        _refresh_codex_lazily()
    key = identity_key if identity_key is not None else subscription_usage.cache_key(provider)
    data = _load()
    if not key:
        return _route_limit_only(provider, data, now)
    entry = data.get(key)
    if not isinstance(entry, dict):
        return None
    q = _from_entry(provider, entry)
    live = []
    stale_claude = provider == "claude" and now - q.updated_at > CLAUDE_FRESH_SECONDS
    for w in q.windows:
        if w.resets_at:  # 0 or missing: unknown, so only its age counts
            if w.resets_at <= now:
                if w.overage and w.name is None and not stale_claude:
                    q.overage_reset = True
                continue
        elif now - q.updated_at > CLAUDE_FRESH_SECONDS:
            continue
        if stale_claude:
            continue
        live.append(w)
    q.windows = live
    if q.limited_until is not None and q.limited_until <= now:
        q.limited_until = None
    return q if q.windows or q.limited_until else None


def _native_down(repo_root: str | None) -> bool:
    from brindle.native import serve

    return any(not serve.reachable(s) for s in serve.local_servers(repo_root))


def _key_billed(provider: str) -> bool:
    """Claude runs on an API key (per token), so a subscription window the
    status line once recorded doesn't apply."""
    if provider != "claude":
        return False
    from brindle import providers

    return providers.claude_credential().keyed


def _managed_for(repo_root: str | None):
    if not repo_root:
        return None
    from brindle.pro import managed_models

    try:
        return managed_models.current(repo_root)
    except Exception:  # noqa: BLE001 - unreadable policy never crashes quota reads
        return None


def _route_var_active(var: str, value: str | None) -> bool:
    val = (value or "").strip()
    if not val:
        return False
    if var.startswith("CLAUDE_CODE_USE_") and val.lower() in ("0", "false"):
        return False
    return True


def _effective_route_env(provider: str, repo_root: str | None = None,
                         env: dict[str, str] | None = None) -> dict[str, str]:
    """Non-secret route variables a launch of ``provider`` receives, following
    ``agents.agent_env`` precedence and honoring explicit empty values (such as
    ``connections._selectors`` blanks that clear an inherited/stored cloud route)."""
    out: dict[str, str] = {}
    if provider == "claude":
        from brindle import company_identity, company_login, providers

        if providers.claude_org_managed():
            return out
        try:
            enforced = company_identity.enforced_route_env()
        except Exception:  # noqa: BLE001
            enforced = None
        managed_env: dict[str, str] = {}
        if (m := _managed_for(repo_root)) is not None:
            from brindle.pro import managed_models

            managed_env = managed_models.agent_env(m, "claude")
        keys = (*_CLAUDE_NON_SUBSCRIPTION_VARS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        if enforced is not None:
            # Under an enforced Enterprise route, inherited pickers and profile
            # overrides that pick another account are stripped; managed_env and
            # enforced decide the route.
            if env:
                for var in keys:
                    if var in env and company_identity.profile_may_set(var):
                        out[var] = env[var]
            for var in keys:
                if var in managed_env:
                    out[var] = managed_env[var]
            for var in keys:
                if var in enforced:
                    out[var] = enforced[var]
        else:
            # Lowest to highest, matching agents.agent_env:
            # inherited process env < company stored_env < profile/connection env < managed_env.
            for var in _CLAUDE_NON_SUBSCRIPTION_VARS:
                if var in os.environ:
                    out[var] = os.environ[var]
            try:
                company = company_login.stored_env()
            except Exception:  # noqa: BLE001
                company = {}
            for var in keys:
                if var in company:
                    out[var] = company[var]
            if env:
                for var in keys:
                    if var in env:
                        out[var] = env[var]
            for var in keys:
                if var in managed_env:
                    out[var] = managed_env[var]
    elif provider == "codex":
        # Inherited process env < profile/connection env < org managed_env.
        if "OPENAI_BASE_URL" in os.environ:
            out["OPENAI_BASE_URL"] = os.environ["OPENAI_BASE_URL"]
        if env and "OPENAI_BASE_URL" in env:
            out["OPENAI_BASE_URL"] = env["OPENAI_BASE_URL"]
        if (m := _managed_for(repo_root)) is not None:
            from brindle.pro import managed_models

            managed_env = managed_models.agent_env(m, "codex")
            if "OPENAI_BASE_URL" in managed_env:
                out["OPENAI_BASE_URL"] = managed_env["OPENAI_BASE_URL"]
    return out


def _route_scope(provider: str, *, repo_root: str | None = None,
                 env: dict[str, str] | None = None) -> str | None:
    """Secret-free fingerprint for a non-subscription corporate route (API key,
    cloud, org-managed, gateway), or None when ``provider`` uses a subscription
    route. Never reads Codex auth/token files."""
    parts: list[str] = []
    if _key_billed(provider):
        parts.append("key")
    route = _effective_route_env(provider, repo_root, env)
    if provider == "claude":
        from brindle import providers

        org_managed = providers.claude_org_managed()
        if org_managed:
            parts.append(f"org-managed:{org_managed}")
        for var in _CLAUDE_NON_SUBSCRIPTION_VARS:
            if _route_var_active(var, route.get(var)):
                parts.append(f"{var}={route[var].strip()}")
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if _route_var_active(var, route.get(var)):
                parts.append(f"env:{var}")
    elif provider == "codex":
        if _route_var_active("OPENAI_BASE_URL", route.get("OPENAI_BASE_URL")):
            parts.append(f"OPENAI_BASE_URL={route['OPENAI_BASE_URL'].strip()}")
        if (m := _managed_for(repo_root)) is not None:
            from brindle.pro import managed_models

            if managed_models.has_org_key(m, "codex"):
                parts.append(f"org-key:{m.org_id}")
    if not parts:
        return None
    return hashlib.sha256("\0".join(sorted(parts)).encode()).hexdigest()[:16]


def _non_subscription_route(provider: str, *, repo_root: str | None = None,
                            env: dict[str, str] | None = None) -> bool:
    """Whether ``provider`` runs on a route without personal subscription
    windows (API key, token, Bedrock/Vertex/Foundry, corporate gateway, or
    org-managed settings). Never reads Codex auth/token files."""
    return _route_scope(provider, repo_root=repo_root, env=env) is not None


def claude_usage_scope() -> str | None:
    """The opaque cache identity for the active Claude subscription."""
    key = subscription_usage.cache_key("claude")
    if key is None or _non_subscription_route("claude"):
        return None
    return key


def effective_windows(q: Quota, *, now: float | None = None) -> tuple[list[Window], float | None, bool]:
    """Evaluate plan availability keeping included allowance and optional paid
    overage separate. Returns ``(serving_windows, limited_until, has_overage_buffer)``.

    Work can run while either the included allowance has room OR optional paid
    overage (``overage=True`` / ``overage_unlimited=True`` / reset overage) has
    room. When both are exhausted, the cutoff lifts at the earliest reset that
    restores either allowance. Once either allowance expires (resets), the other
    exhausted allowance does not hold work back on its own."""
    all_plan = [w for w in q.windows if w.name is None]
    plan = [w for w in all_plan if now is None or w.resets_at is None or w.resets_at > now]
    included = [w for w in plan if not w.overage]
    overage = [w for w in plan if w.overage]
    overage_reset = q.overage_reset or (
        now is not None and any(w.overage and w.resets_at is not None and w.resets_at <= now for w in all_plan)
    )
    if not included:
        # Optional overage windows alone only remain after included windows reset.
        return [], None, q.overage_unlimited or overage_reset
    included_full = [w for w in included if w.used >= 100.0]
    if not included_full:
        has_buffer = (q.overage_unlimited or overage_reset
                      or (bool(overage) and all(w.used < 100.0 for w in overage)))
        return included, None, has_buffer
    if q.overage_unlimited:
        return [], None, True
    if overage:
        overage_full = [w for w in overage if w.used >= 100.0]
        if not overage_full:
            return overage, None, False
        if overage_reset:
            return [], None, True
        inc_resets = [w.resets_at for w in included_full if w.resets_at is not None]
        ov_resets = [w.resets_at for w in overage_full if w.resets_at is not None]
        candidates = [r for r in (max(inc_resets) if inc_resets else None,
                                  max(ov_resets) if ov_resets else None) if r is not None]
        return overage_full, (min(candidates) if candidates else None), False
    if overage_reset:
        return [], None, True
    inc_resets = [w.resets_at for w in included_full if w.resets_at is not None]
    return included_full, (max(inc_resets) if inc_resets else None), False


def known_headroom(provider: str, cfg: RepoConfig | None = None, repo_root: str | None = None,
                   *, now: float | None = None, env: dict[str, str] | None = None) -> float | None:
    """Percent of ``provider``'s allowance left when known: 100 minus its
    fullest serving plan window, 0 while limited, or None when usage is unknown."""
    if provider == "native":
        return 0.0 if _native_down(repo_root) else 100.0
    cur = time.time() if now is None else now
    q = get(provider, now=cur, repo_root=repo_root, env=env)
    if q is None:
        return None
    if q.limited_until and q.limited_until > cur:
        return 0.0
    if _non_subscription_route(provider, repo_root=repo_root, env=env):
        return None
    serving, plan_limited, _ = effective_windows(q, now=cur)
    if plan_limited is not None:
        return 0.0
    w = max(serving, key=lambda w: w.used, default=None)
    return None if w is None else max(0.0, 100.0 - w.used)


def headroom(provider: str, cfg: RepoConfig | None = None, repo_root: str | None = None,
             *, env: dict[str, str] | None = None) -> float:
    """Percent of the provider's allowance left: 100 minus its fullest window,
    0 while it's limited. 100 when nothing is known."""
    room = known_headroom(provider, cfg, repo_root, env=env)
    return 100.0 if room is None else room


_DEFAULT_HEADROOM = headroom


def status(provider: str, repo_root: str | None = None, *, now: float | None = None,
           env: dict[str, str] | None = None) -> str:
    """``"limited"``, ``"ok"``, or ``"unknown"`` for ``provider``'s usage."""
    if provider == "native":
        return "limited" if _native_down(repo_root) else "ok"
    cur = time.time() if now is None else now
    q = get(provider, now=cur, repo_root=repo_root, env=env)
    if q is None:
        return "unknown"
    if q.limited_until and q.limited_until > cur:
        return "limited"
    if _non_subscription_route(provider, repo_root=repo_root, env=env):
        return "unknown"
    serving, plan_limited, _ = effective_windows(q, now=cur)
    if plan_limited is not None or any(w.used >= 100.0 for w in serving):
        return "limited"
    return "ok" if serving else "unknown"


def window_name(minutes: int | None) -> str:
    return {300: "5-hour", 1440: "daily", 10080: "weekly", 43200: "monthly"}.get(
        minutes or 0, f"{minutes}-minute" if minutes else "usage")


def window_short(minutes: int | None) -> str:
    return {300: "5h", 1440: "1d", 10080: "wk", 43200: "mo"}.get(
        minutes or 0, f"{minutes}m" if minutes else "use")


def left_by_window(provider: str, *, repo_root: str | None = None,
                   env: dict[str, str] | None = None) -> list[tuple[str, int]]:
    """``(window, percent left)`` for each live window, shortest first and
    one-model caps last: ``[("5h", 64), ("wk", 91), ("Fable", 89)]``. Empty
    while limited, billed per token, or when nothing current is known."""
    if provider == "native" or _non_subscription_route(provider, repo_root=repo_root, env=env):
        return []
    q = get(provider, repo_root=repo_root, env=env)
    if q is None or q.limited_until:
        return []
    ordered = sorted(q.windows, key=lambda w: (w.name is not None, w.minutes or 0, w.name or ""))
    return [(w.name or window_short(w.minutes), int(round(max(0.0, 100.0 - w.used)))) for w in ordered]


def fullest(q: Quota, *, now: float | None = None) -> Window | None:
    """The serving plan window nearest its limit; a one-model cap (``name``)
    doesn't hold up the plan's other models, and optional overage is evaluated
    against the included allowance."""
    serving, _, _ = effective_windows(q, now=now)
    return max(serving, key=lambda w: w.used, default=None)


def _when(ts: float) -> str:
    t = time.localtime(ts)
    clock = time.strftime("%-I:%M%p", t).lower()
    return clock if time.strftime("%F", t) == time.strftime("%F") else f"{time.strftime('%a', t)} {clock}"


def note(provider: str, repo_root: str | None = None) -> str | None:
    """A one-line status such as "Codex at 82% of its weekly limit, resets
    Thu 9:00am", or None when there's nothing to say."""
    name = NAMES.get(provider, provider)
    if provider == "native":
        return "local model server not answering" if _native_down(repo_root) else None
    q = get(provider, repo_root=repo_root)
    if q is not None and q.limited_until:
        return f"{name} limit reached, available again {_when(q.limited_until)}"
    if _key_billed(provider):
        return f"{name} is pay per token (API key), no subscription window"
    if _non_subscription_route(provider, repo_root=repo_root) or q is None:
        return None
    w = fullest(q)
    if w is None:
        return None
    resets = f", resets {_when(w.resets_at)}" if w.resets_at else ""
    return f"{name} at {w.used:.0f}% of its {window_name(w.minutes)} limit{resets}"


def notes(repo_root: str | None = None, *, native: bool = True) -> list[str]:
    """``note`` for every provider with data. ``native=False`` skips the
    local model server probe (for callers that must not block)."""
    return [n for p in PROVIDERS if native or p != "native" if (n := note(p, repo_root))]


# -- Claude: the status line data -------------------------------------------------------


def _claude_status_window(name: str, data: dict, now: float) -> Window | None:
    used = _percent_from_dict(data)
    if used is None:
        return None
    minutes_raw = _number(data.get("window_minutes"))
    key = name.strip().lower()
    if key in CLAUDE_PLAN_WINDOWS:
        minutes = int(minutes_raw) if minutes_raw and minutes_raw > 0 else CLAUDE_PLAN_WINDOWS[key]
        return Window(used, _reset_from_dict(data, now), minutes, None)
    for prefix, default_min in _CLAUDE_MODEL_WINDOW_PREFIXES:
        if key.startswith(prefix) and len(key) > len(prefix):
            scope = key[len(prefix):].replace("_", " ").strip()
            cap_name = None if scope in _CLAUDE_PLAN_SCOPES else scope.title()
            minutes = int(minutes_raw) if minutes_raw and minutes_raw > 0 else default_min
            return Window(used, _reset_from_dict(data, now), minutes, cap_name)
    return None


def _parse_claude_extra_usage(extra: dict, now: float) -> tuple[bool, Window | None, bool]:
    """Return ``(enabled, spend_window, unlimited)`` for a Claude ``extra_usage`` block."""
    enabled = bool(extra.get("is_enabled") if "is_enabled" in extra else extra.get("enabled", True))
    if not enabled:
        return False, None, False
    used = _percent_from_dict(extra)
    if used is not None:
        minutes_raw = _number(extra.get("window_minutes"))
        minutes = int(minutes_raw) if minutes_raw and minutes_raw > 0 else 43200
        return True, Window(used, _reset_from_dict(extra, now), minutes, None), False
    limit_keys = ("monthly_limit", "credit_limit", "limit_credits", "limit_usd", "spend_limit", "limit")
    unlimited = bool(extra.get("unlimited")) or (
        any(k in extra for k in limit_keys) and all(extra.get(k) is None for k in limit_keys if k in extra)
    )
    return True, None, unlimited


def record_claude(status: dict) -> bool:
    """Keep the plan usage Claude Code gives its status line (Claude.ai
    subscriptions and Enterprise/Team accounts). True when it carried any."""
    if _non_subscription_route("claude"):
        return False
    who = subscription_usage.identity()
    bound = os.environ.get(subscription_usage.STATUS_IDENTITY)
    if who is None or (bound and bound != who):
        return False
    key = subscription_usage.cache_key("claude", who=who)
    limits = status.get("rate_limits")
    extra_raw = (limits.get("extra_usage") if isinstance(limits, dict) else None) or status.get("extra_usage")
    if not isinstance(limits, dict) and not isinstance(extra_raw, dict):
        return False
    now = time.time()
    extra_enabled, extra_win, extra_unlimited = (
        _parse_claude_extra_usage(extra_raw, now) if isinstance(extra_raw, dict) else (False, None, False)
    )
    plan_windows: list[Window] = []
    cap_windows: list[Window] = []
    if isinstance(limits, dict):
        for name, w in limits.items():
            if not isinstance(name, str) or name == "extra_usage" or not isinstance(w, dict):
                continue
            parsed = _claude_status_window(name, w, now)
            if parsed is None:
                continue
            (cap_windows if parsed.name else plan_windows).append(parsed)
    overage_unlimited = bool(extra_enabled and extra_unlimited)
    if extra_win is not None:
        # Optional paid overage alongside included plan windows; when no included
        # windows were reported, extra_usage is the sole spend allowance.
        extra_win.overage = bool(plan_windows)
        plan_windows.append(extra_win)
    windows = plan_windows + cap_windows
    if not windows and not overage_unlimited:
        return False
    # Preserve one-model caps from ``claude /usage`` that the status line didn't supply.
    old = get("claude", identity_key=key)
    seen_caps = {w.name for w in cap_windows}
    windows += [w for w in (old.windows if old else []) if w.name and w.name not in seen_caps]
    record("claude", windows, source="status line", keep_limit=True, identity_key=key,
           overage_unlimited=overage_unlimited)
    return True


# -- Claude: ``claude -p /usage`` -------------------------------------------------------

CLAUDE_USAGE_REFRESH_SECONDS = 30 * 60
CLAUDE_USAGE_TIMEOUT = 30
_USAGE_HEADER = re.compile(
    r"^\s*(?:"
    r"Current\s+(session|day|week|month|spend)"
    r"|(Extra\s+usage|Monthly\s+spend|Current\s+spend|Spend\s+limit"
    r"|Organization\s+(?:limit|usage|spend)|Org\s+(?:limit|usage|spend)"
    r"|Team\s+(?:limit|usage|spend)|Workspace\s+(?:limit|usage|spend)|Seat\s+(?:limit|usage|spend)"
    r"|Daily|Weekly|Monthly)"
    r")(?:\s*\(([^)]+)\))?\s*(?::\s*(.*))?$",
    re.I,
)
_PCT_USED = re.compile(r"(\d+(?:\.\d+)?)%\s*used", re.I)
_PCT_LEFT = re.compile(r"(\d+(?:\.\d+)?)%\s*(?:left|remaining)", re.I)
_SPEND_RATIO = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:/|of)\s*\$\s*([\d,]+(?:\.\d+)?)", re.I)
_RESETS_CLAUSE = re.compile(r"(?:[·,\-]\s*|\b)resets?\s+(?:at\s+)?(.+?)(?:\s*\(([^)]+)\))?\s*$", re.I)
_claude_usage_at = 0.0
_claude_usage_identity: str | None = None
# Every sidebar asks; this file's mtime is when any process of this brindle
# home last did, so N sidebars start one claude CLI per refresh, not N.
CLAUDE_USAGE_STAMP = "claude-usage.stamp"


def _reset_time(text: str, zone: str | None, now: float) -> float | None:
    """"Oct 12 at 6:10pm" / "Dec 1" / "6:10pm" / ISO-8601 in ``zone`` (else local time) as epoch seconds."""
    ts = _timestamp(text)
    if ts is not None:
        return ts
    try:
        tz = ZoneInfo(zone.strip()) if zone and zone.strip() else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    today = datetime.fromtimestamp(now, tz)
    cleaned = re.sub(r"\s+", " ", text.strip().replace(",", "")).replace(" at ", " ").upper()
    for fmt in ("%b %d %Y %I:%M%p", "%b %d %Y %I%p", "%b %d %Y",
                "%b %d %I:%M%p", "%b %d %I%p", "%b %d", "%I:%M%p", "%I%p"):
        try:
            t = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if "%Y" in fmt:
            t = t.replace(tzinfo=tz)
        elif "%b" in fmt:
            t = t.replace(year=today.year, tzinfo=tz)
            if t.timestamp() < now - 86400:      # "Jan 2" read in late December
                t = t.replace(year=today.year + 1)
        else:
            t = today.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if t.timestamp() < now:
                t += timedelta(days=1)
        return t.timestamp()
    return None


def _claude_span_minutes(current_span: str | None, label: str | None) -> int | None:
    if current_span:
        return {"session": 300, "day": 1440, "week": 10080, "month": 43200, "spend": 43200}.get(current_span.lower())
    low = (label or "").lower()
    if "daily" in low:
        return 1440
    if "weekly" in low:
        return 10080
    if any(w in low for w in ("month", "spend", "extra")):
        return 43200
    return 10080


def _parse_usage_percent(body: str) -> float | None:
    m = _PCT_USED.search(body)
    if m:
        return float(m.group(1))
    m = _PCT_LEFT.search(body)
    if m:
        return max(0.0, 100.0 - float(m.group(1)))
    m = _SPEND_RATIO.search(body)
    if m:
        used = _num_or_str(m.group(1))
        lim = _num_or_str(m.group(2))
        if used is not None and lim is not None and used >= 0 and lim > 0:
            return max(0.0, (used / lim) * 100.0)
    return None


def _parse_reset_clause(body: str, now: float) -> float | None:
    m = _RESETS_CLAUSE.search(body)
    if not m:
        return None
    return _reset_time(m.group(1), m.group(2), now)


_UNLIMITED_USAGE_RE = re.compile(r"(?:/|of|:|\b)\s*unlimited\b|\bno\s+(?:spend\s+)?limit\b", re.I)
_USAGE_UNAVAILABLE_RE = re.compile(
    r"\b403\b|\bforbidden\b|not\s+(?:available|supported|enabled)"
    r"|usage\s+(?:endpoint|data|info|tracking|limits?)\s+(?:is\s+|are\s+)?(?:unavailable|disabled|not\s+available)"
    r"|permission\s+denied|access\s+denied|unauthorized",
    re.I,
)
_UNBOUNDED_USAGE_RE = re.compile(
    r"(?:extra\s+usage|monthly\s+spend|current\s+spend|spend\s+limit|organization\s+(?:usage|spend|limit))"
    r"|(?:\$\s*[\d,]+(?:\.\d+)?\s*spent\b)",
    re.I,
)


def _parse_claude_usage_full(text: str, now: float | None = None) -> tuple[list[Window], bool]:
    """Parse ``claude /usage`` into ``(windows, overage_unlimited)``.

    ``Extra usage`` is tagged ``overage=True`` when included plan windows are
    also present (so it shares the same included-vs-overage availability model
    as status-line ingestion), and ``overage=False`` when it is the sole
    allowance.
    """
    now = time.time() if now is None else now
    lines = text.splitlines()
    parsed: list[tuple[Window, bool]] = []
    overage_unlimited = False
    i = 0
    while i < len(lines):
        m = _USAGE_HEADER.match(lines[i])
        if not m:
            i += 1
            continue
        cur_span, named_label, scope, rest = m.groups()
        is_extra = bool(named_label and named_label.strip().lower() == "extra usage")
        minutes = _claude_span_minutes(cur_span, named_label)
        cap_name = None if (scope is None or scope.strip().lower() in _CLAUDE_PLAN_SCOPES) else scope.strip()
        body = (rest or "").strip()
        j = i + 1
        if not body:
            # Multi-line block layout: header line, then progress/value line, optional Resets line.
            block_parts: list[str] = []
            while j < len(lines) and len(block_parts) < 2:
                nxt = lines[j].strip()
                if not nxt or _USAGE_HEADER.match(lines[j]):
                    break
                block_parts.append(nxt)
                j += 1
            body = " · ".join(block_parts)
        used = _parse_usage_percent(body)
        if used is not None:
            resets_at = _parse_reset_clause(body, now)
            if resets_at is None and j < len(lines) and re.match(r"^\s*resets?\b", lines[j], re.I):
                resets_at = _parse_reset_clause(lines[j], now)
                j += 1
            parsed.append((Window(used, resets_at, minutes, cap_name), is_extra))
        elif is_extra and _UNLIMITED_USAGE_RE.search(body):
            overage_unlimited = True
        i = j
    has_included = any(w.name is None and not is_extra for w, is_extra in parsed)
    windows: list[Window] = []
    for w, is_extra in parsed:
        if is_extra and w.name is None and has_included:
            w.overage = True
        windows.append(w)
    return windows, overage_unlimited


def parse_claude_usage(text: str, now: float | None = None) -> list[Window]:
    """The windows in ``claude /usage``'s answer, including corporate org,
    monthly, and spend-based lines::

        Current session: 36% used · resets Oct 12 at 6:10pm (America/New_York)
        Current week (all models): 9% used · resets Oct 19 at 1pm (America/New_York)
        Current week (organization): 85% used · resets Nov 23 at 9am (UTC)
        Extra usage: $150.00 / $200.00 spent · resets Dec 1 (UTC)
        Current week (Fable): 11% used · resets Oct 19 at 1pm (America/New_York)
    """
    return _parse_claude_usage_full(text, now)[0]


def _usage_unavailable_response(returncode: int, stdout: str, stderr: str) -> bool:
    """Whether ``claude -p /usage`` returned a confirmed unavailable or
    unusable usage response (403/forbidden/unsupported, or an unbounded/empty
    usage block on exit 0) rather than a transient CLI or network failure."""
    combined = f"{stdout}\n{stderr}".strip()
    if not combined:
        return False
    if _USAGE_UNAVAILABLE_RE.search(combined):
        return True
    if returncode != 0:
        return False
    return bool(_UNBOUNDED_USAGE_RE.search(stdout)
                or any(_USAGE_HEADER.match(ln) for ln in stdout.splitlines()))


# -- real CLI limit-error detection -----------------------------------------------------

_LIMIT_ERROR_RE = re.compile(
    r"you'?ve hit your (?:[\w\s-]{0,30} )?limit"
    r"|hit (?:your |the )?(?:[\w\s-]{0,20} )?(?:usage|spend(?:ing)?|quota|monthly|weekly|daily|session|organization|org|workspace|team) limit"
    r"|(?:usage|quota|spend(?:ing)?|extra usage|organization|org|workspace|team|monthly|weekly|daily|session) (?:limit|cap|control) (?:reached|exceeded|hit|exhausted)"
    r"|quota (?:exceeded|exhausted)"
    r"|\b(?:resource_exhausted|spend_control_reached|spend_limit_reached|usage_limit_reached|quota_exceeded)\b"
    r"|out of (?:usage|quota|credits)|credits? exhausted",
    re.I,
)
_LIMIT_RESET_RE = re.compile(r"resets?\s+(?:at\s+)?([^\n·()]+?)(?:\s*\(([^)]+)\))?(?:\s*[·.,]|$)", re.I)


def is_limit_error(text: str | None) -> bool:
    """Whether ``text`` reports an actual usage, quota, or spend limit cutoff
    (not a plain transient 429 or an auth/billing credential failure)."""
    return bool(text and _LIMIT_ERROR_RE.search(text))


def parse_limit_reset(text: str | None, now: float | None = None) -> float | None:
    """Extract a reset timestamp from a CLI limit error message, or None."""
    if not text:
        return None
    m = _LIMIT_RESET_RE.search(text)
    if not m:
        return None
    return _reset_time(m.group(1), m.group(2), time.time() if now is None else now)


def _claim_usage_refresh(force: bool, key: str, *, stamp_name: str = CLAUDE_USAGE_STAMP) -> float | None:
    """Claim this refresh for this process: under a lock that every process
    of this brindle home takes, the shared stamp is read and, when a refresh
    is due (or ``force``), set to now before the lock is let go, so two
    sidebars looking at once can't both find it due. None when this process
    asks; otherwise when the other one did. A stamp that can't be used at
    all (an unwritable home) claims nothing: this process asks."""
    stamp = brindle_home() / (stamp_name + "." + key.split(":", 1)[1])
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        with open(stamp.with_name(stamp.name + ".lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                asked = stamp.stat().st_mtime
            except FileNotFoundError:
                asked = 0.0
            if not force and time.time() - asked < CLAUDE_USAGE_REFRESH_SECONDS:
                return asked
            stamp.touch()
            os.utime(stamp)
    except OSError:
        pass
    return None


def refresh_claude_usage(*, force: bool = False, expected_identity: str | None = None) -> bool:
    """Ask ``claude -p /usage`` (Claude Code reads the plan's usage itself;
    brindle never touches its credentials) at most every
    ``CLAUDE_USAGE_REFRESH_SECONDS``. True when it recorded windows."""
    global _claude_usage_at, _claude_usage_identity
    import subprocess

    from brindle import provider_homes, spawn
    from brindle.providers import claude_binary

    key = claude_usage_scope()
    if key is None or (expected_identity is not None and key != expected_identity):
        return False
    if not force:
        current = get("claude", identity_key=key)
        if current and current.source == "status line" and time.time() - current.updated_at <= CLAUDE_FRESH_SECONDS:
            return False
    if not force and key == _claude_usage_identity and time.time() - _claude_usage_at < CLAUDE_USAGE_REFRESH_SECONDS:
        return False
    asked = _claim_usage_refresh(force, key)
    _claude_usage_identity = key
    if asked is not None:
        _claude_usage_at = asked     # another process asked: its answer is in the shared record
        return False
    _claude_usage_at = time.time()
    if _non_subscription_route("claude"):
        return False
    try:
        # No MCP servers: the answer needs none, and each would be one more
        # process started (and inspected) every few minutes. ``reap``: nothing
        # the CLI leaves in its process group outlives the answer.
        proc = spawn.read([claude_binary(), "-p", "--strict-mcp-config", "/usage"],
                          env={**os.environ, **provider_homes.launch_env("claude")},
                          timeout=CLAUDE_USAGE_TIMEOUT, cwd=str(brindle_home()), reap=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if subscription_usage.cache_key("claude") != key:
        return False
    windows, overage_unlimited = _parse_claude_usage_full(proc.stdout) if proc.returncode == 0 else ([], False)
    if not windows and not overage_unlimited:
        if _usage_unavailable_response(proc.returncode, proc.stdout, proc.stderr):
            # Confirmed 403 or unusable usage response: clear superseded cached
            # windows so stale percentages cannot drive failover, while keeping
            # any independently recorded real CLI limit error.
            record("claude", [], source="claude /usage unavailable", keep_limit=True, identity_key=key)
        return False
    record("claude", windows, source="claude /usage", keep_limit=True, identity_key=key,
           overage_unlimited=overage_unlimited)
    return True


# -- Antigravity: ``agy -p /usage --output-format json`` ----------------------------------

ANTIGRAVITY_USAGE_STAMP = "antigravity-usage.stamp"
_antigravity_usage_at = 0.0


def parse_antigravity_usage(text: str) -> list[Window]:
    """Print-mode /usage returns command.data.groups[].buckets[].

    Each model group has its own caps, so retain the group and window in
    the name rather than presenting any one group's limit as plan-wide.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return []
    if not isinstance(body, dict) or body.get("status") != "SUCCESS":
        return []
    command = body.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return []
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return []
    windows = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            remaining = _number(bucket.get("remaining_fraction"))
            span = bucket.get("window")
            minutes = {"5h": 300, "weekly": 10080}.get(span) if isinstance(span, str) else None
            if remaining is None or not 0 <= remaining <= 1 or minutes is None:
                continue
            resets = None
            if isinstance(bucket.get("reset_time"), str):
                try:
                    reset = datetime.fromisoformat(bucket["reset_time"].replace("Z", "+00:00"))
                    if reset.tzinfo is not None:
                        resets = reset.timestamp()
                except (ValueError, OverflowError, OSError):
                    pass
            windows.append(Window((1 - remaining) * 100, resets, minutes,
                                  f"{group['name']} {window_short(minutes)}"))
    return windows


def refresh_antigravity_usage(*, force: bool = False) -> bool:
    """Ask agy's read-only slash command on the same cadence as Claude's.

    agy handles its own login; no auth files or backend calls belong here.
    spawn.read bounds the run and reaps its language server process group.
    """
    global _antigravity_usage_at
    import subprocess

    from brindle import antigravity, spawn

    if not force and time.time() - _antigravity_usage_at < CLAUDE_USAGE_REFRESH_SECONDS:
        return False
    asked = _claim_usage_refresh(force, "antigravity:default", stamp_name=ANTIGRAVITY_USAGE_STAMP)
    _antigravity_usage_at = asked if asked is not None else time.time()
    if asked is not None:
        return False
    try:
        proc = spawn.read([antigravity.binary(), "-p", "/usage", "--output-format", "json"],
                          timeout=CLAUDE_USAGE_TIMEOUT, cwd=str(brindle_home()), reap=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    windows = parse_antigravity_usage(proc.stdout) if proc.returncode == 0 else []
    if not windows:
        return False
    record("antigravity", windows, source="agy /usage", keep_limit=True)
    return True


# -- Codex: rate limits in the session rollouts ------------------------------------------


def codex_home() -> Path:
    from brindle import providers

    return Path(providers.codex_home())


@dataclass(frozen=True)
class CodexUsageScope:
    identity: str
    since: float


def codex_usage_scope() -> CodexUsageScope | None:
    """A local login revision from file metadata, without reading credentials."""
    from brindle import providers

    if _non_subscription_route("codex") or os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
        return None
    path = Path(providers.codex_auth_path())
    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    parts = (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return CodexUsageScope(hashlib.sha256(repr(parts).encode()).hexdigest(), info.st_mtime)


def _codex_session_started(file) -> float | None:
    meta = json.loads(file.readline(ROLLOUT_TAIL_BYTES))
    payload = meta.get("payload") if isinstance(meta, dict) and meta.get("type") == "session_meta" else None
    return _timestamp(payload.get("timestamp")) if isinstance(payload, dict) else None


def newest_rollout(home: Path | None = None, *, login_since: float | None = None) -> Path | None:
    """The most recently written ``sessions/**/rollout-*.jsonl``. Only
    that directory: nothing else under ~/.codex (auth.json lives there) is opened."""
    sessions = (home or codex_home()) / "sessions"
    best, best_time = None, -1.0
    for f in sessions.glob("**/rollout-*.jsonl"):
        try:
            mtime = f.stat().st_mtime
            if mtime <= best_time:
                continue
            if login_since is not None:
                if mtime < login_since:
                    continue
                with f.open("rb") as file:
                    started = _codex_session_started(file)
                if started is None or started < login_since:
                    continue
        except (OSError, ValueError):
            continue
        else:
            best, best_time = f, mtime
    return best


def _codex_window(w: dict, now: float, *, name: str | None = None) -> Window | None:
    used = _percent_from_dict(w)
    if used is None:
        return None
    minutes = _number(w.get("window_minutes"))
    return Window(used, _reset_from_dict(w, now), int(minutes) if minutes and minutes > 0 else None, name)


def _codex_cap_name(limits: dict) -> str | None:
    limit_name = limits.get("limit_name")
    if isinstance(limit_name, str) and limit_name.strip():
        if limit_name.strip().lower() not in _CODEX_PLAN_NAMES:
            return limit_name.strip()
    return None


def _parse_codex_rate_limits(
    limits: dict, now: float, *, event_base: float | None = None,
) -> tuple[list[Window], float | None, bool, bool]:
    """Parse one Codex ``rate_limits`` dict into
    ``(windows, limited_until, is_model_cap, overage_unlimited)``.

    All relative resets and inferred fallback cooldowns resolve against
    ``event_base`` (the event's own timestamp when present, or the persisted
    first-observed time for untimestamped rollout events) so re-reading an
    unchanged rollout never renews either windows or cooldowns.
    """
    base = now if event_base is None else event_base
    cap_name = _codex_cap_name(limits)
    windows: list[Window] = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if isinstance(w, dict):
            parsed = _codex_window(w, base, name=cap_name)
            if parsed is not None:
                windows.append(parsed)
    # Corporate per-seat individual_limit: flat window dict or nested primary/secondary.
    ind = limits.get("individual_limit")
    if isinstance(ind, dict):
        nested = [p for slot in ("primary", "secondary")
                  if isinstance(ind.get(slot), dict) and (p := _codex_window(ind[slot], base)) is not None]
        if nested:
            windows.extend(nested)
        elif (flat := _codex_window(ind, base)) is not None:
            windows.append(flat)
    extra_list = limits.get("additional_rate_limits")
    if isinstance(extra_list, list):
        for item in extra_list:
            if not isinstance(item, dict):
                continue
            item_name = item.get("limit_name")
            sub = item.get("rate_limit")
            if isinstance(item_name, str) and item_name.strip() and isinstance(sub, dict):
                for slot in ("primary", "secondary"):
                    w = sub.get(slot)
                    if isinstance(w, dict) and (p := _codex_window(w, base, name=item_name.strip())) is not None:
                        windows.append(p)
    credits = limits.get("credits")
    has_overage_credits = False
    credit_win: Window | None = None
    if isinstance(credits, dict):
        bal = _num_or_str(credits.get("balance"))
        has_overage_credits = bool(credits.get("unlimited")) or (
            bool(credits.get("has_credits")) and bal is not None and bal > 0
        )
        credit_win = _codex_window(credits, base)
        if credit_win is not None:
            if any(w.name is None for w in windows):
                credit_win.overage = True
            windows.append(credit_win)
    overage_unlimited = bool(cap_name is None and has_overage_credits and credit_win is None)
    spend_ctrl = limits.get("spend_control_reached")
    spend_reached = (spend_ctrl is True) or (
        isinstance(spend_ctrl, dict) and bool(spend_ctrl.get("reached") or spend_ctrl.get("is_reached"))
    )
    rl_type = limits.get("rate_limit_reached_type")
    type_reached = isinstance(rl_type, str) and bool(rl_type.strip()) and rl_type.strip().lower() != "none"
    limited_until: float | None = None
    if spend_reached or type_reached:
        reset = _reset_from_dict(spend_ctrl, base) if isinstance(spend_ctrl, dict) else None
        if reset is None:
            future = [w.resets_at for w in windows if w.name is None and w.resets_at and w.resets_at > base]
            reset = max(future) if future else base + FALLBACK_COOLDOWN_MINUTES * 60
        limited_until = reset
    return windows, limited_until, cap_name is not None, overage_unlimited


def _windows_from_limits(limits: dict, *, now: float | None = None) -> list[Window]:
    return _parse_codex_rate_limits(limits, time.time() if now is None else now)[0]


def _codex_rollout_state(
    rollout: Path, now: float, *, seen_at: dict[str, float] | None = None,
    login_since: float | None = None,
) -> tuple[bool, list[Window], float | None, bool, dict[str, float]]:
    """Read the newest plan-level ``rate_limits`` (plus any newer per-model caps)
    from ``rollout``. Returns
    ``(saw_event, windows, limited_until, overage_unlimited, observed_events)``."""
    try:
        size = rollout.stat().st_size
        tail_start = max(0, size - ROLLOUT_TAIL_BYTES)
        with rollout.open("rb") as f:
            if login_since is not None:
                started = _codex_session_started(f)
                if started is None or started < login_since:
                    return False, [], None, False, {}
            f.seek(tail_start)
            raw_tail = f.read()
    except (OSError, ValueError):
        return False, [], None, False, {}
    raw_lines: list[tuple[int, bytes]] = []
    pos = 0
    for chunk in raw_tail.splitlines(keepends=True):
        raw_lines.append((tail_start + pos, chunk.rstrip(b"\r\n")))
        pos += len(chunk)
    prior = seen_at or {}
    observed: dict[str, float] = {}
    saw_event = False
    plan_windows: list[Window] | None = None
    model_caps: dict[str, list[Window]] = {}
    limited_until: float | None = None
    overage_unlimited = False
    for abs_offset, raw_line in reversed(raw_lines):
        if b'"rate_limits"' not in raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace")
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        event_ts = (
            (_timestamp(event.get("timestamp")) if isinstance(event, dict) else None)
            or (_timestamp(payload.get("timestamp")) if isinstance(payload, dict) else None)
        )
        if login_since is not None and (event_ts is None or event_ts < login_since):
            continue
        for holder in (payload, event):
            if not isinstance(holder, dict) or "rate_limits" not in holder:
                continue
            saw_event = True
            limits = holder.get("rate_limits")
            if limits is None:
                if plan_windows is None:
                    plan_windows = []
                break
            if not isinstance(limits, dict):
                continue
            event_id = hashlib.sha256(f"{rollout}:{abs_offset}:".encode() + raw_line).hexdigest()[:16]
            if event_ts is not None:
                event_base = event_ts
            else:
                prev = _number(prior.get(event_id))
                event_base = prev if prev is not None else now
                observed[event_id] = event_base
            w_list, lim, is_cap, ov_unlim = _parse_codex_rate_limits(limits, now, event_base=event_base)
            if is_cap:
                for w in w_list:
                    if w.name and w.name not in model_caps:
                        model_caps[w.name] = [x for x in w_list if x.name == w.name]
            else:
                if plan_windows is None:
                    plan_windows = [w for w in w_list if w.name is None]
                    for w in w_list:
                        if w.name and w.name not in model_caps:
                            model_caps[w.name] = [x for x in w_list if x.name == w.name]
                    limited_until = lim
                    overage_unlimited = ov_unlim
            break
        if plan_windows is not None:
            break
    windows = (plan_windows or []) + [w for ws in model_caps.values() for w in ws]
    return saw_event, windows, limited_until, overage_unlimited, observed


def last_codex_limits(rollout: Path) -> list[Window]:
    """The windows of the last event in ``rollout`` carrying ``rate_limits``,
    reading from the end."""
    return _codex_rollout_state(rollout, time.time())[1]


def scoped_codex_usage(scope: CodexUsageScope, now: float) -> Quota | None:
    """Read desktop usage only from a session opened under this login revision."""
    rollout = newest_rollout(login_since=scope.since)
    if rollout is None:
        return None
    _, windows, limited_until, overage_unlimited, _ = _codex_rollout_state(rollout, now, login_since=scope.since)
    windows = [w for w in windows if w.resets_at is None or w.resets_at > now]
    if limited_until is not None and limited_until <= now:
        limited_until = None
    if windows or limited_until:
        return Quota("codex", windows, limited_until, now, "codex rollout", overage_unlimited)
    return None


def refresh_codex(home: Path | None = None) -> bool:
    """Record Codex's limits from its newest rollout. True when it recorded
    current limits or confirmed credit-covered usage; clears stale windows
    when the newest rollout explicitly reports ``rate_limits: null``."""
    if _non_subscription_route("codex"):
        return False
    rollout = newest_rollout(home)
    if not rollout:
        return False
    now = time.time()
    old = _load().get("codex")
    prev_seen = old.get("event_seen") if isinstance(old, dict) and isinstance(old.get("event_seen"), dict) else {}
    saw_event, windows, limited_until, overage_unlimited, observed = (
        _codex_rollout_state(rollout, now, seen_at=prev_seen)
    )
    if not saw_event:
        return False
    has_live = any(w.resets_at is None or w.resets_at > now for w in windows)
    if limited_until is not None and limited_until > now:
        record("codex", windows, limited_until=limited_until, source="codex rollout",
               overage_unlimited=overage_unlimited, event_seen=observed)
        return True
    if has_live or overage_unlimited:
        record("codex", windows, source="codex rollout", keep_limit=True,
               overage_unlimited=overage_unlimited, event_seen=observed)
        return True
    # Explicit null/empty or fully expired rollout state: clear stale windows
    # while keeping first-observed event timestamps and any independent CLI limit.
    record("codex", [], source="codex rollout", keep_limit=True, event_seen=observed)
    return False


def _refresh_codex_lazily() -> None:
    entry = _load().get("codex")
    if isinstance(entry, dict) and time.time() - (_number(entry.get("updated_at")) or 0) < CODEX_REFRESH_SECONDS:
        return
    try:
        refresh_codex()
    except OSError:
        pass
