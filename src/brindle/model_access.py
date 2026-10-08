"""Model-access errors: a cloud account that can't call a model.

Bedrock, Vertex and Foundry refuse a model for reasons that are about the
account, not the request: a quota of 0, a form nobody filled in, a project
without Claude quota. Claude Code prints the provider's error and sits at its
prompt, so every task routed to that model fails the same way. This module

* classifies such an error (matching on stable substrings and status codes)
  into a cause with a one-line fix (``classify``),
* remembers which profiles were refused, for an hour, so routing moves a task
  to the next profile of its weight tier (``config.DEFAULT_ROUTING``) and
  says so in the run notes (``refuse``, ``refused``, ``notes``),
* recognises Claude Code's own alias fallback warning ("Opus 5.5 not
  available — using Opus 4.6 for this session"; ``alias_fallback``), and
* probes a pinned model with one tiny call (``probe``, for ``brindle ci
  doctor --models``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from brindle.config import brindle_home

REFUSAL_TTL = 3600.0       # seconds a refused profile stays out of routing
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_TIMEOUT = 120.0


@dataclass(frozen=True)
class Cause:
    key: str
    provider: str
    cause: str
    fix: str

    def line(self) -> str:
        return f"{self.cause}. Fix: {self.fix}"


BEDROCK_QUOTA = Cause(
    "bedrock_quota", "bedrock",
    "the account's quota for this Bedrock model is 0",
    "open an AWS support case (Service Quotas only accepts values above AWS's default)")
BEDROCK_USE_CASE = Cause(
    "bedrock_use_case", "bedrock",
    "Anthropic's use-case form hasn't been submitted for this AWS account",
    "submit Anthropic's use-case form in the Bedrock console, then wait about 15 minutes")
BEDROCK_ENDPOINT = Cause(
    "bedrock_endpoint", "bedrock",
    "Bedrock Mantle doesn't know this endpoint or model ID",
    "use the `us.` or `global.` inference profile IDs for the model")
VERTEX_QUOTA = Cause(
    "vertex_quota", "vertex",
    "the Google Cloud project has no Claude quota (online_prediction_requests_per_base_model)",
    "request Claude quota for the project; a new project may need 48 hours or billing history, "
    "or Google sales")
VERTEX_DATA_SHARING = Cause(
    "vertex_data_sharing", "vertex",
    "this model needs Anthropic data sharing enabled on the Google Cloud project",
    "enable data sharing for publisher 'anthropic' on the project (Fable requires it)")
FOUNDRY_QUOTA = Cause(
    "foundry_quota", "foundry",
    "the Foundry subscription has no Claude quota (InsufficientQuota)",
    "request Claude quota in the Foundry portal")
FOUNDRY_PROVIDER_DATA = Cause(
    "foundry_provider_data", "foundry",
    "the Foundry deployment lacks organization details (InvalidModelProviderData)",
    "give the deployment organization, industry and country (API version 2026-09-01 or later)")

_DATA_SHARING = re.compile(r"requires data sharing to be enabled for publisher\s*['\"‘’]?anthropic",
                           re.IGNORECASE)


MANTLE_WORDING = "the model 'claude-"   # Mantle's "The model 'claude-…' does not exist"


def classify(text: str | None) -> Cause | None:
    """The cause of a model-access error in ``text`` (an API error line, a
    CLI's stderr), or None when it is some other error."""
    if not text:
        return None
    low = text.lower()
    if "is not available for this account" in low:
        return BEDROCK_QUOTA
    if "model use case details have not been submitted" in low:
        return BEDROCK_USE_CASE
    if "404" in low and "does not exist" in low and "model" in low and (
            "mantle" in low or "bedrock" in low or MANTLE_WORDING in low):
        return BEDROCK_ENDPOINT
    if "online_prediction_requests_per_base_model" in low:
        return VERTEX_QUOTA
    if _DATA_SHARING.search(text):
        return VERTEX_DATA_SHARING
    if "insufficientquota" in low:
        return FOUNDRY_QUOTA
    if "invalidmodelproviderdata" in low:
        return FOUNDRY_PROVIDER_DATA
    return None


ALIAS_FALLBACK = re.compile(
    r"(?P<wanted>[A-Za-z][\w.\-]*(?: [\w.\-]+){0,3}?) (?:is )?not available\s*[—–-]+\s*"
    r"using (?P<got>[A-Za-z][\w.\-]*(?: [\w.\-]+){0,3}?) for this session")


def alias_fallback(text: str | None) -> str | None:
    """Claude Code's own fallback warning in ``text``, as a sentence to show,
    or None."""
    m = ALIAS_FALLBACK.search(text or "")
    if m is None:
        return None
    return (f"Claude Code could not use {m['wanted']} and fell back to {m['got']} for this "
            "session; the account may lack access to the newer model.")


# -- which profiles were refused ------------------------------------------------------


def _path() -> str:
    return str(brindle_home() / "model_refusals.json")


def _load(now: float) -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    refused = {k: v for k, v in (data.get("refused") or {}).items()
               if isinstance(v, dict) and now - float(v.get("at", 0)) < REFUSAL_TTL}
    told = {k: v for k, v in (data.get("told") or {}).items() if now - float(v) < REFUSAL_TTL}
    return {"refused": refused, "told": told}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _path())


# Names and non-secret IDs that say which cloud account a profile calls; never keys.
FINGERPRINT_ENV = (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
    "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL",
)


def fingerprint(profile: str, repo_root: str | None = None) -> str:
    """Where ``profile`` would call a model: its provider, whether a cloud flag
    is on, and the region, project or resource it names. A refusal applies only
    under the fingerprint it was recorded with."""
    env = dict(os.environ)
    provider = ""
    try:
        from brindle.profiles import load_profile

        p = load_profile(profile, repo_root)
        provider = p.provider or ""
        env.update(p.env or {})
    except Exception:  # noqa: BLE001 - an unreadable profile adds nothing
        pass
    parts = [f"provider={provider}"]
    for k in FINGERPRINT_ENV:
        v = env.get(k)
        if v not in (None, ""):
            parts.append(f"{k}={v}")
    return "|".join(parts)


def _key(profile: str, fp: str) -> str:
    return f"{profile}@{fp}"


def refuse(profile: str, cause: Cause, swapped_to: str | None = None, *,
           repo_root: str | None = None, fp: str | None = None) -> None:
    """Record that ``profile``'s model was refused, and where its tasks go."""
    now = time.time()
    fp = fingerprint(profile, repo_root) if fp is None else fp
    data = _load(now)
    data["refused"][_key(profile, fp)] = {"profile": profile, "fp": fp, "cause": cause.key,
                                          "at": now, "swapped_to": swapped_to}
    _save(data)


def refused(profile: str, repo_root: str | None = None, fp: str | None = None) -> Cause | None:
    """The cause ``profile``'s model was refused for, here, within the last hour."""
    fp = fingerprint(profile, repo_root) if fp is None else fp
    entry = _load(time.time())["refused"].get(_key(profile, fp))
    return next((c for c in CAUSES if entry and c.key == entry.get("cause")), None)


def first_time(key: str) -> bool:
    """True once per hour for ``key`` (so a supervisor hears of one thing once)."""
    now = time.time()
    data = _load(now)
    if key in data["told"]:
        return False
    data["told"][key] = now
    _save(data)
    return True


def notes(profiles: Iterable[str] | None = None, repo_root: str | None = None) -> list[str]:
    """One line per profile refused within the last hour, with the cause, the
    fix and the profile its tasks moved to: for a run's notes. With
    ``profiles`` (a session's), only those, and only refusals recorded under
    the fingerprint they have here."""
    wanted = None if profiles is None else set(profiles)
    out = []
    for entry in sorted(_load(time.time())["refused"].values(),
                        key=lambda e: (e.get("profile", ""), e.get("fp", ""))):
        profile = entry.get("profile", "")
        if wanted is not None and (profile not in wanted
                                   or entry.get("fp") != fingerprint(profile, repo_root)):
            continue
        cause = next((c for c in CAUSES if c.key == entry.get("cause")), None)
        if cause is None:
            continue
        to = entry.get("swapped_to")
        moved = f"; tasks moved to {to}" if to else ""
        out.append(f"model refused for {profile}: {cause.line()}{moved}")
    return out


CAUSES = (BEDROCK_QUOTA, BEDROCK_USE_CASE, BEDROCK_ENDPOINT, VERTEX_QUOTA, VERTEX_DATA_SHARING,
          FOUNDRY_QUOTA, FOUNDRY_PROVIDER_DATA)


def next_profile(routing: Mapping[str, list[str]], weight: str | None, profile: str,
                 usable: Callable[[str], bool] = lambda _name: True,
                 repo_root: str | None = None) -> str | None:
    """The profile after ``profile`` in its weight tier that is not refused and
    ``usable``. Without a ``weight``, the first tier that lists ``profile``."""
    tiers = [weight] if weight in routing else list(routing)
    for tier in tiers:
        order = routing.get(tier, [])
        if profile not in order:
            continue
        for name in order[order.index(profile) + 1:]:
            if refused(name, repo_root) is None and usable(name):
                return name
    return None


def swap(routing: Mapping[str, list[str]], weight: str | None, profile: str, error: str,
         usable: Callable[[str], bool] = lambda _name: True,
         repo_root: str | None = None) -> tuple[Cause, str | None] | None:
    """``profile``'s model was refused with ``error``: if that is a model-access
    error, record the refusal and the swap and return (cause, the profile that
    takes over, or None when the tier has none left). None for other errors."""
    cause = classify(error)
    if cause is None:
        return None
    to = next_profile(routing, weight, profile, usable, repo_root)
    refuse(profile, cause, to, repo_root=repo_root)
    return cause, to


# -- probing a pinned model ---------------------------------------------------------------


def probe(binary: str, model: str, cwd: str, env: Mapping[str, str], *,
          run=subprocess.run, timeout: float = PROBE_TIMEOUT) -> tuple[bool, Cause | None, str]:
    """One tiny call to ``model`` through the claude CLI. (answered, the
    model-access cause when it was refused, the error text when it failed)."""
    try:
        proc = run([binary, "-p", "--model", model, "--output-format", "json"], cwd=cwd,
                   env=dict(env), input=PROBE_PROMPT, capture_output=True, text=True,
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, "timed out"
    except OSError as e:
        return False, None, f"couldn't start {binary}: {e}"
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    cause = classify(text)
    if cause is not None:
        return False, cause, text.strip()[-300:]
    if proc.returncode != 0:
        return False, None, text.strip()[-300:] or f"exit {proc.returncode}"
    return True, None, ""
