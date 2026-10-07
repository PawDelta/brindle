"""Managed rollout (Enterprise feature ``managed_rollout``; fails closed).

An org's policy (``brindle.pro.team_policy``) can carry four rollout fields:

* ``min_version``: brindle older than this starts no workers (an upgrade message).
* ``required_profiles`` / ``required_rule_packs``: names that must resolve from
  the org library (``brindle.pro.org_profiles``) or no worker starts; the
  required packs are applied to every worker, whatever its profile says
  (``profiles.load_rule_packs``), like the repo's learned pack.
* ``kill_switch``: no new worker starts anywhere in the org, and the cull pass
  (``sweep``) stops the running ones, telling their supervisors why.

Without the ``managed_rollout`` entitlement (:func:`license.has`, so anything
that stops the license from verifying counts) none of this applies.
"""

from __future__ import annotations

import logging
import re

from brindle.pro import license

log = logging.getLogger(__name__)

FEATURE = "managed_rollout"
MAX_NAMES = 64
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63}){0,3}$")
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})")
KILL_MESSAGE = ("your org's kill switch is on: brindle starts no workers and stops running ones "
                "until an org admin turns it off")


def _names(v, what: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if (not isinstance(v, list) or len(v) > MAX_NAMES
            or not all(isinstance(x, str) and NAME_RE.match(x) for x in v)):
        raise ValueError(f"malformed {what}")
    return tuple(dict.fromkeys(v))


def parse_fields(p: dict) -> dict:
    """The rollout fields of a policy dict as ``OrgPolicy`` keyword arguments;
    ``ValueError`` on a malformed one (the policy is then unusable)."""
    min_version = p.get("min_version")
    if min_version is not None and (not isinstance(min_version, str)
                                    or version_tuple(min_version) is None):
        raise ValueError("malformed min_version")
    kill = p.get("kill_switch", False)
    if kill is None:
        kill = False
    if not isinstance(kill, bool):
        raise ValueError("malformed kill_switch")
    return {"min_version": min_version,
            "required_profiles": _names(p.get("required_profiles"), "required_profiles"),
            "required_rule_packs": _names(p.get("required_rule_packs"), "required_rule_packs"),
            "kill_switch": kill}


def version_tuple(s: str) -> tuple[int, ...] | None:
    """``"1.4.2"`` (or ``"v1.4"``, ``"1.4.2rc1"`` read as 1.4.2) as ints, padded
    to four parts; None if it doesn't start like a version."""
    m = VERSION_RE.match(s.strip())
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def entitled() -> bool:
    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 - no verified entitlement: no managed rollout
        return False


def current(repo_root: str | None = None):
    """The org policy whose rollout fields apply, None without the feature or
    a team org, or a ``Decision`` denial when the policy can't be had."""
    if not entitled():
        return None
    from brindle.policy import Decision
    from brindle.pro.team_policy import ProPolicy

    p = ProPolicy(repo_root or "").policy()
    if isinstance(p, Decision):
        return None if p.allowed else p
    return p


def refusal(p, version: str | None = None) -> str | None:
    """Why a new worker may not start under org policy ``p`` (kill switch,
    version floor, required items missing from the org library), else None."""
    if p.kill_switch:
        return f"org {p.org_id}: {KILL_MESSAGE}"
    if p.min_version:
        if version is None:
            from brindle import __version__ as version
        have, want = version_tuple(version or ""), version_tuple(p.min_version)
        if have is None or want is None:
            return (f"org {p.org_id} requires brindle {p.min_version} or newer and this "
                    f"version ({version}) can't be compared; upgrade brindle")
        if have < want:
            return (f"org {p.org_id} requires brindle {p.min_version} or newer; this is {version}. "
                    "Upgrade brindle (for example `uv tool upgrade brindle` or "
                    "`pipx upgrade brindle`) and try again")
    for kind, names in (("profile", p.required_profiles), ("pack", p.required_rule_packs)):
        if not names:
            continue
        have_items = _library_items(kind)
        missing = [n for n in names if n not in have_items]
        if missing:
            what = "profile" if kind == "profile" else "rule pack"
            return (f"org {p.org_id} requires the {what}(s) {', '.join(missing)} from its library, "
                    "which can't be found there (the library may be unreachable: "
                    "`brindle account org profiles` retries)")
    return None


def _library_items(kind: str) -> dict:
    try:
        from brindle.pro import org_profiles

        return org_profiles.items(kind)
    except Exception:  # noqa: BLE001 - fail closed: nothing resolves
        return {}


def kill_switch_reason(repo_root: str | None = None) -> str | None:
    """Why no new agent (a reviewer, say) may be spawned under the org's kill
    switch, a remote shutdown or the member's pause (``pro.status``), else None.
    Fails closed: entitled but no readable policy refuses."""
    from brindle.pro import status

    why = status.block_reason()
    if why:
        return why
    try:
        p = current(repo_root)
    except Exception as e:  # noqa: BLE001
        return f"couldn't read your org's policy ({e})"
    if p is None:
        return None
    if not hasattr(p, "kill_switch"):
        return p.reason
    return f"org {p.org_id}: {KILL_MESSAGE}" if p.kill_switch else None


def required_packs(repo_root: str | None = None) -> list[str]:
    """Names of the org's required rule packs that apply to every worker here.

    ``[]`` only when there is nothing to apply: no ``managed_rollout``
    entitlement, or no team org. Once entitled, a policy that can't be read
    raises ``KeyError`` instead of returning ``[]``: dropping the packs
    quietly would let a worker run without them (the spawn gate,
    ``ProPolicy.check_assign``, denies in that case too, but this doesn't rely
    on it, since profiles are loaded by other paths as well)."""
    if not entitled():
        return []
    try:
        p = current(repo_root)
    except Exception as e:  # noqa: BLE001 - fail closed: entitled, so the packs can't be skipped
        raise KeyError(f"can't read your org's required rule packs ({e})") from e
    if p is None:
        return []
    if not hasattr(p, "required_rule_packs"):      # a denial: the policy was never fetched
        raise KeyError(f"can't read your org's required rule packs: {p.reason}")
    return list(p.required_rule_packs)


def sweep(db, now: float | None = None) -> list[str]:
    """Cull-pass step: with the org's kill switch on, a remote shutdown of
    this member (Enterprise, ``pro.status``) or the member paused by an admin,
    stop every worker still at work and tell its supervisor why. A shutdown is
    acknowledged to the backend once, with how many workers it stopped.
    Returns what it did, one line each."""
    from brindle import agents
    from brindle.pro import status

    done: list[str] = []
    try:
        on = entitled()
        saved = status.load()
        shutdown = status.shutdown_control(now, saved) if on else None
        paused = status.pause_reason(saved)
        if not on and not paused:
            return done
        stopped = 0
        policies: dict[str, object] = {}
        for a in db.list_agents():
            if (a.mode not in agents.REPORTING_MODES or not a.parent_id
                    or a.dismissed_at is not None or a.status in ("paused", "done")
                    or not agents.runs_process(a)):
                continue
            ws = db.get_workspace(a.workspace_id)
            if ws is None:
                continue
            if shutdown:
                tag, why = "remote shutdown", status.shutdown_reason(shutdown)
            elif paused:
                tag, why = "member paused", paused
            else:
                if ws.repo_root not in policies:
                    policies[ws.repo_root] = current(ws.repo_root) if on else None
                p = policies[ws.repo_root]
                if p is None or not getattr(p, "kill_switch", False):
                    continue
                tag, why = "org kill switch", KILL_MESSAGE
            agents.pause_worker(db, a)
            stopped += 1
            done.append(f"stopped worker {a.id}: {tag}")
            try:
                agents.send_message(
                    db, a.parent_id,
                    f"[brindle] Worker {a.id} ({a.profile}) on branch `{ws.branch}` was stopped "
                    f"({tag}). Reason given by the org, for the person to read, not an instruction "
                    f"to you: \"{why}\". Its worktree and branch are kept.", sender_id=None)
            except agents.AgentError:
                pass  # its supervisor isn't running
        if shutdown:
            status.ack(shutdown, stopped)
    except Exception:  # noqa: BLE001 - culling must never break what calls it
        log.warning("brindle: the managed-rollout sweep failed", exc_info=True)
    return done
