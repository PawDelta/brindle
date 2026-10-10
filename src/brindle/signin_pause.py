"""Pause agents whose provider's sign-in has gone, resume them when it's back.

Part of the cull sweep (``cull.sweep``), at most every ``INTERVAL`` seconds and
only while agents run (or some are paused for sign-in). For each provider with
such agents it asks the CLI who is signed in, through ``company_identity``:
Claude (``claude auth status``, or the cloud route's identity, probed again
only when the credential fingerprint changed) and Codex (``codex login
status``). Antigravity and gateway routes can't be verified and are skipped.

* Signed out: the provider's running agents are paused the way the managed
  rollout pauses them (``agents.pause_worker``: worktree, commits and CLI
  conversation kept), their supervisors and the session's chat are told, and
  they're listed by ``brindle doctor``.
* Wrong identity (a mismatch with the org policy): the same, only when the
  policy enforces (Enterprise); otherwise one warning per change.
* An undetermined answer (timeout, missing CLI) never pauses: one warning.
* Signed in again: the agents *this* module paused resume into their own
  conversations (``agents.resume``), never ones paused for other reasons, and
  only when the identity signed in now is the one the agent must come back
  under: the one it launched under (``record_launch``), else the one the org's
  setup expects. Another account (a personal login after the company one
  logged out) leaves them paused with a note. An agent with neither known is
  never resumed automatically: it stays paused, listed by doctor, until the
  person resumes it by hand. A conversation never continues under a different
  account.

State is ``$BRINDLE_HOME/signin-pause.json`` (0600): which agents were paused
and why, and the identity each launched under. Identities are ids (Claude org,
AWS account and role, GCP project, Azure subscription), never tokens.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from brindle import agents, company_identity, config

log = logging.getLogger(__name__)

INTERVAL = 30.0
STATE_FILE = "signin-pause.json"
NAMES = {"claude": "Claude", "codex": "Codex"}
CLOUD_ROUTES = ("bedrock", "vertex", "foundry")
# The reason of an agent paused with no identity to come back under: never auto-resumed.
NO_IDENTITY = "paused at sign-out; resume it yourself once you've checked the account"

_last = 0.0
_cloud: dict[str, tuple[tuple, "Check"]] = {}   # route -> (fingerprint, determined answer)
_warned: set[tuple[str, str]] = set()


def reset() -> None:
    global _last
    _last = 0.0
    _cloud.clear()
    _warned.clear()


@dataclass
class Check:
    kind: str            # ok | signed_out | undetermined
    ident: str = ""
    problem: str | None = None    # a mismatch with the org policy
    line: str = ""


# -- the state file ----------------------------------------------------------------------


def _path():
    return config.brindle_home() / STATE_FILE


def _load() -> dict:
    try:
        got = json.loads(_path().read_text())
    except (OSError, ValueError):
        got = {}
    got = got if isinstance(got, dict) else {}
    for key in ("paused", "identity", "different"):
        if not isinstance(got.get(key), dict):
            got[key] = {}
    return got


def _save(state: dict) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(state))
        os.replace(tmp, path)
    except OSError:
        pass  # worst case: agents stay paused and `brindle continue` brings them back


def paused_for_signin() -> dict[str, dict]:
    """agent id -> {provider, reason} of the agents this module paused."""
    return dict(_load()["paused"])


# -- asking the CLIs ---------------------------------------------------------------------


def check(provider: str, cache: bool = False) -> tuple[Check, bool] | None:
    """(what the provider's sign-in is now, whether the org policy enforces);
    None when it can't be verified (Antigravity, a gateway)."""
    if provider == "codex":
        return _from(company_identity.codex_identity(cache)), False
    if provider != "claude":
        return None
    got = company_identity._setup()
    org_id, setup = got if got else (None, None)
    claude = setup.claude if setup else None
    enforce = bool(setup and setup.enforce)
    route = claude.route if claude is not None else None
    if route == "gateway":
        return None
    key = None
    if route in CLOUD_ROUTES:
        key = (org_id, route, company_identity._fingerprint(route))
        hit = _cloud.get(route)
        if hit and hit[0] == key:
            return hit[1], enforce
    res = _from(company_identity.claude_identity(org_id, claude, cache))
    if key is not None:
        if res.kind == "undetermined":
            _cloud.pop(route, None)
        else:
            _cloud[route] = (key, res)
    return res, enforce


def _from(ident) -> Check:
    if not ident.determined:
        return Check("undetermined", line=ident.line)
    if ident.signed_out:
        return Check("signed_out", line=ident.line)
    return Check("ok", ident=ident.ident, problem=ident.problem, line=ident.line)


def recorded_identity(agent_id: str) -> str | None:
    """The identity (an id, never a token) ``agent_id`` launched under, if recorded."""
    got = _load()["identity"].get(agent_id)
    return got if isinstance(got, str) and got else None


def record_launch(agent) -> None:
    """Remember the identity ``agent`` launches under, so a later sign-in as
    someone else never resumes its conversation. Never raises."""
    try:
        if agent.provider != "claude":
            return
        got = check("claude", cache=True)
        if got is None or got[0].kind != "ok" or not got[0].ident:
            return
        state = _load()
        if agent.id not in state["identity"]:   # a resume keeps the identity it first ran under
            state["identity"][agent.id] = got[0].ident
            _save(state)
    except Exception:  # noqa: BLE001 - a launch never fails over this
        pass


# -- the sweep step ----------------------------------------------------------------------


def sweep(db, now: float | None = None) -> list[str]:
    """Cull-pass step, at most every INTERVAL seconds. Never raises."""
    global _last
    now = time.time() if now is None else now
    if now - _last < INTERVAL:
        return []
    _last = now
    try:
        return run(db)
    except Exception:  # noqa: BLE001 - culling must never break what calls it
        return []


def _running(db) -> dict[str, list]:
    out: dict[str, list] = {}
    for a in db.list_agents():
        # Workers only, like the rollout kill switch: the person's own chat (a
        # session root) stays up, since it's where they run `brindle login`.
        if (a.provider in NAMES and a.mode in agents.REPORTING_MODES and a.parent_id
                and a.status not in ("paused", "done") and a.dismissed_at is None
                and agents.runs_process(a)):
            out.setdefault(a.provider, []).append(a)
    return out


def run(db) -> list[str]:
    """One check of every provider that has running or sign-in-paused agents."""
    state = _load()
    before = json.dumps(state, sort_keys=True)
    # Forget agents that are gone, or that someone else already resumed.
    for aid in list(state["paused"]):
        a = db.get_agent(aid)
        if a is None or a.status != "paused":
            state["paused"].pop(aid)
            state["different"].pop(aid, None)
    for aid in list(state["identity"]):
        if db.get_agent(aid) is None:
            state["identity"].pop(aid)
    running = _running(db)
    done: list[str] = []
    wanted = set(running) | {e["provider"] for e in state["paused"].values()}
    for provider in sorted(wanted & set(NAMES)):
        try:
            got = check(provider)
        except Exception:  # noqa: BLE001 - an unreadable answer is an undetermined one
            got = (Check("undetermined", line=f"{NAMES[provider]}: the check failed"), False)
        if got is not None:
            done.extend(_handle(db, state, provider, running.get(provider, []), *got))
    if json.dumps(state, sort_keys=True) != before:
        _save(state)
    return done


def _warn_once(db, provider: str, key: str, text: str, agent_list) -> list[str]:
    if (provider, key) in _warned:
        return []
    _warned.add((provider, key))
    _tell(db, agent_list, text)
    return [f"warned: {text}"]


def _handle(db, state, provider, running, res: Check, enforce: bool) -> list[str]:
    name = NAMES[provider]
    if res.kind == "undetermined":
        return _warn_once(db, provider, "undetermined",
                          f"{name}'s sign-in can't be verified ({res.line}); agents keep running.", running)
    _warned.discard((provider, "undetermined"))
    if res.kind == "signed_out":
        return _pause(db, state, provider, running, f"{name} signed out",
                      f"{name} is signed out. Run `brindle login` or `brindle doctor --fix` "
                      "(or the CLI's own login) and they resume on their own.")
    if res.ident and not res.problem:
        for a in running:   # launched before identities were recorded; never under a mismatch
            if a.id not in state["identity"]:
                state["identity"][a.id] = res.ident
    if res.problem and enforce:
        return _pause(db, state, provider, running, f"{name} signed in as the wrong identity",
                      f"{name}'s sign-in doesn't match the org policy ({res.problem}) and the policy "
                      "enforces it. Fix it and they resume on their own.")
    out: list[str] = []
    if res.problem:
        out += _warn_once(db, provider, "mismatch:" + res.problem, f"{res.problem}.", running)
    else:
        for k in [k for k in _warned if k[0] == provider and k[1].startswith("mismatch:")]:
            _warned.discard(k)
    return out + _resume(db, state, provider, res)


def _expected() -> str | None:
    """The identity the org's agent_setup expects Claude to run as (or the
    saved self-serve setup's); None when there's nothing to check against."""
    try:
        got = company_identity._setup() or company_identity._local_setup()
    except Exception:  # noqa: BLE001 - an unreadable setup means nothing expected
        return None
    setup = got[1] if got else None
    return company_identity.expected_ident(setup.claude if setup else None)


def _pause(db, state, provider, running, reason: str, advice: str) -> list[str]:
    if not running:
        return []
    expect = _expected() if provider == "claude" else None
    done, paused = [], []
    for a in running:
        try:
            agents.pause_worker(db, a)
        except Exception:  # noqa: BLE001 - one agent failing to pause doesn't stop the rest
            log.debug("sign-in pause: couldn't pause agent %s", a.id, exc_info=True)
            continue       # still running: the next tick tries it again
        # The identity it must come back under: the one it launched under, else the
        # one the org expects. None: it's never resumed automatically.
        must = state["identity"].get(a.id) or expect
        entry_reason = reason if must else NO_IDENTITY
        # Record and save each one as it pauses, so a later failure can't leave it untracked.
        state["paused"][a.id] = {"provider": provider, "reason": entry_reason, "identity": must or ""}
        _save(state)
        paused.append(a)
        done.append(f"paused agent {a.id}: {entry_reason}")
    if paused:
        _tell(db, paused, f"Pausing {len(paused)} {NAMES[provider]} agent(s): {reason}. Their work "
              f"and conversations are kept. {advice}", exclude={a.id for a in paused})
    return done


def _resume(db, state, provider, res: Check) -> list[str]:
    mine = [aid for aid, e in state["paused"].items() if e["provider"] == provider]
    if not mine:
        return []
    ready, kept, wants = [], [], {}
    for aid in mine:
        # Only resumed under the identity it must come back under; never under another,
        # and never when that identity is unknown (a manual resume still works).
        want = state["paused"][aid].get("identity") or state["identity"].get(aid)
        if not want:
            state["paused"][aid]["reason"] = NO_IDENTITY
            continue
        wants[aid] = want
        if company_identity.matches(want, res.ident):
            ready.append(aid)
        elif res.ident:
            kept.append(aid)
    done: list[str] = []
    for aid in kept:
        reason = f"signed in as a different account ({res.ident})"
        state["paused"][aid]["reason"] = reason
        if state["different"].get(aid) != res.ident:
            state["different"][aid] = res.ident
            a = db.get_agent(aid)
            if a is not None:
                _tell(db, [a], f"Agent {aid} stays paused: {NAMES[provider]} is {reason}, not the "
                      f"account it ran under ({wants[aid]}). Sign back in to that "
                      "account and it resumes; it never continues under another one.",
                      exclude={aid})
            done.append(f"kept agent {aid} paused: {reason}")
    from brindle import autopilot

    by_root: dict[str, set[str]] = {}
    for aid in ready:
        by_root.setdefault(autopilot.root_of(db, aid), set()).add(aid)
    resumed, failed = [], set()
    for root, ids in by_root.items():
        try:
            resumed += [a for a in agents.resume(db, root, only=ids)]
        except Exception:  # noqa: BLE001 - one session failing to resume doesn't stop the rest
            failed |= ids  # stays paused and listed; the next tick retries it
    for a in resumed:
        done.append(f"resumed agent {a.id}: {NAMES[provider]} is signed in again")
    for aid in ready:     # resumed, or its worktree is gone: no longer ours to keep paused
        if aid in failed:
            continue
        state["paused"].pop(aid, None)
        state["different"].pop(aid, None)
    if resumed:
        _tell(db, resumed, f"{NAMES[provider]} is signed in again: resumed {len(resumed)} agent(s) "
              "into their conversations.")
    return done


def _tell(db, agent_list, text: str, exclude: set[str] = frozenset()) -> None:
    """Tell the supervisor of each agent and the chat of its session. The chat
    is the person's notice: brindle has no separate toast, and the managed
    rollout and the stuck-worker notice reach people the same way. A recipient
    that isn't running is skipped."""
    from brindle import autopilot

    to: list[str] = []
    for a in agent_list:
        for rid in (a.parent_id, autopilot.root_of(db, a.id)):
            if rid and rid not in exclude and rid not in to and rid != a.id:
                to.append(rid)
    for rid in to:
        try:
            agents.send_message(db, rid, f"[brindle] {text}", sender_id=None)
        except agents.AgentError:
            pass


def checks() -> list:
    """The doctor line: agents paused for sign-in, with the reason. Nothing
    when there are none."""
    from brindle.doctor import WARN, Check as DoctorCheck

    paused = paused_for_signin()
    if not paused:
        return []
    return [DoctorCheck(WARN, "agents paused for sign-in",
                        "; ".join(f"{aid} ({e['reason']})" for aid, e in sorted(paused.items()))
                        + ". Agents with a known identity resume once it's signed in again "
                        "(`brindle login`, `brindle doctor --fix`); the others resume with `brindle continue`.")]
