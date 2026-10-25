"""Permission grants: the person allows a worker more than brindle's rules
do, for one call or for a while, by answering its held request in the app.

The lifecycle is the same for every kind of request (read, write, edit,
bash, fetch, mcp, other):

1. Offer. A worker's tool call reaches brindle's hook (Claude Code's or
   Codex's PermissionRequest). If the rules leave it to the person (ask),
   the hold the app answers (agents._hold_for_app) also offers ``allow_for``:
   this agent, this repo, this exact request (``key``), for at most
   ``max_minutes`` and ``max_uses``. The offer is recorded with the request
   (Request.grant), so the digest the app answers covers it. If one of the
   built-in denies an adapter knows (``OVERRIDABLE``) refused it, and
   nothing else denies it, the deny is answered at once as always, and the
   hook asks the person for a one-time grant of exactly that request and
   the scope the adapter worked out (``lift``): it is never held, since a
   held request shows the CLI's own prompt, where an answer would run it.
2. Answer. Only the person, on the daemon's person-only inbox route
   (service.inbox.answer_permission): deny, allow once, a bounded duration,
   or a remembered adapter permission when policy permits. No answer, the
   hold running out, or no daemon: the rules' answer stands.
3. Check again. After an ``allow_for`` answer the hook reads the policy and
   every rule again (``recheck``); the answer to a one-time grant is checked
   against the policy then (``approve``), and its use reads everything,
   the adapter's scope included, once more. Anything changed refuses it.
4. Use. The daemon keeps the grants (brindle.daemon.grants). The hook
   spends a use for a later identical request that the rules, read again
   then, still leave to the person (``use_grant``), or that the same
   built-in deny refuses with the very scope the person allowed (``lift``).
   Grants end when they expire, when their uses are spent, when the person
   revokes them, when the agent is closed, or when the daemon restarts.
   A use counts when the call is allowed, not when the tool succeeds: a
   hook can't see the result.
   Saved adapter permissions require the policy's persistence gate for
   every lifetime, at creation and on every use. Timed permissions survive a daemon restart and
   agent closure. They retain their original expiry and use budget, bounded
   by current policy on every call. Forever permissions last until revoked
   and require current policy to allow persistent permissions on every call.
   A saved push may cover one destination ref, all branches at its verified
   remote, all permitted remotes in that repository, or across repositories.
   Current policy and the adapter's safety checks still govern every use.

Never lifted: an org's, the repo's, a profile's or the person's own deny,
a force flag, and the credential and daemon-token reads. Nothing an agent
or a repo writes makes a grant.

Who decides what may be granted (``limits``): for a member of a Team org,
the org policy's ``permission_grants`` block (brindle.pro.team_policy) and
nothing else, read again on every request. Its ``deny`` rules are enforced
for every provider. No block, a policy that can't be read, or a member
paused or shut down by the org: no grants. Without a Team org, the local
defaults below. The org backend that serves the block isn't part of this
repo: until it serves one, a Team org offers no grants, and the block is
enforced from the cached copy or, in air-gap mode, ``.brindle/policy.json``.

Adapters: ``git-push`` lifts d-git-push for exactly ``git push <remote>
<full sha>:refs/heads/<branch>`` whose remote resolves to one https, ssh or
user@host:path URL with nothing configured that could send the push
elsewhere or push more, and only while no protected paths are set (a push
skips the merge check that enforces them). ``env`` lifts d-env for one
``.env`` file inside the worktree. What is checked is the state when the
hook answers: the command runs right after, outside brindle."""

from __future__ import annotations

import json
import hashlib
import time
import os
import re
import subprocess
from dataclasses import dataclass

from brindle import permissions
from brindle.permissions import Decision, Request, Rule

# Built-in denies a person may lift for one call, by the action org policy
# names them with (team_policy.GRANT_ACTIONS). Every other deny is hard.
OVERRIDABLE = {"d-git-push": "git-push", "d-env": "env", "d-env-rel": "env"}
LOCAL_MINUTES = 60          # without a Team org: what allow_for may cover at most
LOCAL_USES = 20
GIT_SECONDS = 5

SHA = re.compile(r"[0-9a-f]{40}")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BRANCH_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]+")
PUSH_URL = re.compile(r"(?:https|ssh)://[^\s:]+(?::[0-9]+)?/\S+|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s:]\S*")
# Set, any of these could send an approved push somewhere else or push more than one ref.
PUSH_CONFIG = ("core.sshCommand", "core.hooksPath", "remote.{r}.receivepack", "remote.{r}.vcs",
               "remote.{r}.mirror", "push.followTags", "push.recurseSubmodules")
PUSH_HOOKS = ("pre-push", "reference-transaction")
PUSH_FORM = "only `git push <remote> <full commit sha>:refs/heads/<branch>` can be allowed once"


@dataclass(frozen=True)
class Limits:
    """What may be granted in a repo now, and the org's own denies."""
    allow: bool
    kinds: tuple[str, ...] = ()
    overridable: tuple[str, ...] = ()
    max_minutes: int = 0
    max_uses: int = 0
    deny: tuple[Rule, ...] = ()
    protected: bool = False       # the org or repo protects paths
    why: str = ""                 # why nothing may be granted
    allow_persistent: bool = False
    owner: str = "local"
    lifetimes: tuple[str, ...] = ("session", "time")
    scopes: tuple[str, ...] = ("branch",)


def _plugin(repo_root: str):
    from brindle.pro.team_policy import ProPolicy

    return ProPolicy(repo_root)


def org_rule(spec: str) -> Rule:
    kind, match_type, match = spec.split(None, 2)
    return Rule(kind, match, match_type, "deny", "org")


def limits(repo_root: str) -> Limits:
    """The grants a person may give in ``repo_root`` now (see the module).
    Any failure to read the org's policy offers none."""
    from brindle.config import load_repo_config
    from brindle.policy import Decision as PolicyDecision
    from brindle.pro import status
    from brindle.pro.team_policy import GRANT_ACTIONS

    try:
        protected = bool(load_repo_config(repo_root).protected_paths)
        p = _plugin(repo_root).policy()
    except Exception as e:  # noqa: BLE001 - fail closed
        return Limits(False, why=f"the org policy can't be read ({e})")
    if isinstance(p, PolicyDecision):
        if not p.allowed:
            return Limits(False, why=p.reason)
        return Limits(True, permissions.KINDS, GRANT_ACTIONS, LOCAL_MINUTES, LOCAL_USES,
                      protected=protected, allow_persistent=True,
                      lifetimes=("session", "time", "forever"), scopes=("branch", "remote", "repo"))
    g = p.permission_grants
    deny = tuple(org_rule(s) for s in g.deny) if g else ()
    blocked = status.block_reason(p)
    if blocked:
        return Limits(False, deny=deny, why=f"org {p.org_id}: {blocked}")
    if g is None:
        return Limits(False, why=f"org {p.org_id}'s policy sets no permission_grants")
    return Limits(g.allow, g.kinds, g.overridable, g.max_minutes, g.max_uses, deny,
                  protected or bool(p.protected_paths), "" if g.allow else f"org {p.org_id} allows no grants",
                  allow_persistent=g.allow_persistent, owner=f"org:{p.org_id}",
                  lifetimes=g.lifetimes, scopes=g.scopes)


def key(req: Request) -> tuple[str, str] | None:
    """The exact (kind, match) a grant for ``req`` covers: the whole simple
    command, the resolved path, the https URL or the tool; None when no
    grant could cover it (a compound or quoted command, say)."""
    if req.kind == "bash":
        argv = permissions.simple_argv(req.command or "")
        return ("bash", " ".join(argv)) if argv and req.command.strip() == " ".join(argv) else None
    if req.kind in permissions.PATH_KINDS:
        return (req.kind, os.path.realpath(req.path)) if req.path else None
    if req.kind == "fetch":
        return ("fetch", req.url) if req.url and req.url.startswith("https://") else None
    return (req.kind, req.tool) if req.tool else None


def repo_key(req: Request) -> str:
    return os.path.realpath(req.repo_root or req.worktree or req.cwd)


def _hard_deny(req: Request, rules: list[Rule], checks: list[str], action: str) -> Rule | None:
    """A deny the action can't lift that matches ``req``, each rule on its own."""
    lifted = {rid for rid, a in OVERRIDABLE.items() if a == action}
    return next((r for r in rules if r.decision == "deny" and not (r.source == "default" and r.id in lifted)
                 and permissions.rule_matches(r, req, checks)), None)


def offer(req: Request, decision: Decision, lim: Limits) -> dict | None:
    """The ``allow_for`` grant the person may give for a request the rules
    leave to them, held for their answer."""
    k = key(req)
    if (decision.decision != "ask" or not lim.allow or k is None or req.kind not in lim.kinds
            or "time" not in lim.lifetimes or "branch" not in lim.scopes):
        return None
    return {"type": "allow_for", "kind": k[0], "match": k[1], "minutes": lim.max_minutes, "uses": lim.max_uses}


def override(req: Request, decision: Decision, rules: list[Rule], checks: list[str],
             lim: Limits) -> tuple[dict | None, str]:
    """(the one-time lift of the built-in deny that refused ``req``; else
    None and why not, "" when ``decision`` isn't such a deny at all)."""
    rule = decision.rule
    action = OVERRIDABLE.get(rule.id) if decision.decision == "deny" and rule and rule.source == "default" else None
    if action is None:
        return None, ""
    if not lim.allow:
        return None, f"no one-time grant: {lim.why}"
    if not lim.lifetimes or not any(_scope_allowed(action, s, lim) for s in PUSH_SCOPES):
        return None, "no one-time grant: the policy permits no supported lifetime or scope"
    if req.kind not in lim.kinds:
        return None, f"no one-time grant: the policy does not allow {req.kind} grants"
    if action not in lim.overridable:
        return None, f"no one-time grant: the policy doesn't let a person lift {rule.id}"
    hard = _hard_deny(req, rules, checks, action)
    if hard is not None:
        return None, f"no one-time grant: {hard.source} rule {hard.id} ({hard.describe()}) denies it too"
    scope, why = ADAPTERS[action](req, lim)
    if scope is None:
        return None, f"no one-time grant: {why}"
    return {"type": "override", "rule": rule.id, "action": action, "scope": scope}, ""


def lift(agent_id: str, req: Request, decision: Decision, rules: list[Rule], checks: list[str],
         lim: Limits) -> Decision:
    """For a deny an adapter knows: an allow by spending the person's
    one-time grant for exactly this request and the scope it has now, else
    the deny, having asked the person for that grant in the app. The deny
    is answered at once, never held: a held request shows the CLI's own
    prompt meanwhile (Claude Code's does), and an answer typed there would
    run it without the person's grant."""
    from dataclasses import replace

    from brindle.daemon import grants

    lifted, why = override(req, decision, rules, checks, lim)
    k = key(req)
    if lifted is None or k is None:
        return replace(decision, reason=f"{decision.reason}; {why}") if why else decision
    scope = json.dumps(lifted["scope"], sort_keys=True)
    remembered = remembered_match(req, lifted, lim, agent_id=agent_id)
    if remembered is not None:
        return Decision("allow", f"allowed by the person's saved permission preference {remembered}")
    g = grants.use(agent_id, repo_key(req), *k, scope=scope, max_minutes=lim.max_minutes, max_uses=lim.max_uses)
    if g is not None:
        return Decision("allow", f"allowed once by the person's grant {g.get('id')} ({describe(lifted)}); "
                                 "whether the command succeeds isn't known")
    # Only the request: the daemon works out what the person is shown and allows (ask_for).
    asked = grants.ask(agent_id, {**req.to_dict(), "grant": None})
    if asked is None:
        return replace(decision, reason=f"{decision.reason}; no one-time grant could be asked for "
                                        "(brindle's app isn't running, or too many are waiting)")
    return replace(decision, reason=f"{decision.reason}; asked the person to allow it once in brindle's "
                                    f"Inbox ({asked}): once they have, run exactly the same command again")


def decision_now(req: Request, checks: list[str], rules: list[Rule]) -> Decision:
    """The rules' answer for one request, a deny in any part of it first."""
    return permissions.deny_in_parts(req, checks=checks, rules=rules) or permissions.decide(
        req, checks=checks, rules=rules)


def recheck(req: Request, offered: dict, checks: list[str], extra_rules: list[Rule]) -> str | None:
    """Why the ``allow_for`` grant the person just offered or gave no longer
    holds, read again now (the policy, every rule), else None."""
    bare = Request.from_dict({**req.to_dict(), "grant": None})
    lim = limits(bare.repo_root)
    rules = [*permissions.all_rules(bare.repo_root or None), *extra_rules, *lim.deny]
    decision = decision_now(bare, checks, rules)
    if offer(bare, decision, lim) == offered:
        return None
    return lim.why or decision.reason or "the request or the policy changed while it was held"


def use_grant(agent_id: str, req: Request, decision: Decision, lim: Limits) -> Decision:
    """``decision``, or an allow by spending a use of the agent's grant for
    exactly this request when the rules, read just now, leave it to the
    person and the policy still lets this kind be granted."""
    from brindle.daemon import grants

    k = key(req)
    if (decision.decision != "ask" or not lim.allow or k is None or req.kind not in lim.kinds
            or "time" not in lim.lifetimes or "branch" not in lim.scopes):
        return decision
    g = grants.use(agent_id, repo_key(req), *k, max_minutes=lim.max_minutes, max_uses=lim.max_uses)
    if g is None:
        return decision
    left = max(0, round((float(g.get("expires_at", 0)) - time.time()) / 60))
    return Decision("allow", f"allowed by the person's grant {g.get('id')} ({g.get('uses_left')} uses and "
                             f"{left} min left); whether the call succeeds isn't known")


def make(agent_id: str, req: Request, minutes: int | None = None) -> dict | None:
    """The person answered ``allow_for``: the grant it leaves for later
    calls (this one is allowed already), within the policy as it is now.
    This held-request grant also ends with the agent or daemon."""
    from brindle.daemon.grants import GRANTS

    offered = parse(req)
    if not offered or offered.get("type") != "allow_for":
        return None
    lim = limits(req.repo_root)
    if (not lim.allow or offered["kind"] not in lim.kinds
            or "time" not in lim.lifetimes or "branch" not in lim.scopes):
        return None
    g = GRANTS.add(agent_id, repo_key(req), offered["kind"], offered["match"],
                   min(minutes if minutes is not None else int(offered["minutes"]), lim.max_minutes),
                   min(int(offered["uses"]), lim.max_uses) - 1,
                   uses_spent=1)
    return g.to_json() if g else None


def approve(asked) -> tuple[str | None, int]:
    """(why the person can't give the one-time grant ``asked``
    (daemon.grants.Asked) under the policy as it is now, else None; the
    longest the policy lets it last, in minutes)."""
    lim = limits(asked.repo_root)
    if not lim.allow:
        return lim.why, 0
    if not lim.lifetimes or not any(_scope_allowed(asked.action, s, lim) for s in PUSH_SCOPES):
        return "the policy permits no supported lifetime or scope", 0
    if asked.kind not in lim.kinds:
        return f"the policy does not allow {asked.kind} grants", 0
    if asked.action not in lim.overridable:
        return f"the policy doesn't let a person lift a {asked.action} deny", 0
    return None, lim.max_minutes


def validate_asked(asked, db) -> bool:
    """Rebuild the adapter offer from current workspace, rules and destination."""
    agent = db.get_agent(asked.agent_id)
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if ws is None:
        return False
    scope = json.loads(asked.scope)
    raw = {"kind": asked.kind, "tool": "Bash" if asked.kind == "bash" else "Read",
           "cwd": ws.path, "command": asked.match if asked.kind == "bash" else None,
           "path": scope.get("path")}
    return ask_for(asked.agent_id, raw) == (asked.repo_root, asked.kind, asked.match, asked.scope,
                                           asked.action, asked.describe)


PUSH_SCOPES = ("branch", "remote", "repo", "all")


def _scope_allowed(action: str, level: str, lim: Limits) -> bool:
    level = "repo" if level == "repository" else level
    if action != "git-push":
        return level == "branch" and "branch" in lim.scopes
    return level in PUSH_SCOPES and level in lim.scopes


def _preference_scope(action: str, scope: dict, level: str = "branch") -> dict:
    level = "repo" if level == "repository" else level
    names = ("path",)
    if action == "git-push":
        names = {"branch": ("repo", "git_dir", "remote", "url", "ref"),
                 "remote": ("repo", "git_dir", "remote", "url"),
                 "repo": ("repo", "git_dir"), "all": ()}[level]
    return {name: scope[name] for name in names}


def _preference_owner(lim: Limits) -> str | None:
    from brindle.pro import license

    try:
        subject = license.current(refresh=False).sub
    except license.LicenseError:
        return "local" if lim.owner == "local" else None
    return hashlib.sha256(f"{lim.owner}:{subject}".encode()).hexdigest()


def persistent_allowed(asked) -> bool:
    return _saved_allowed(asked, "forever")


def timed_allowed(asked) -> bool:
    return _saved_allowed(asked, "time")


def session_allowed(asked) -> bool:
    return _saved_allowed(asked, "session") and _session_binding(asked.agent_id) is not None


def _saved_allowed(asked, lifetime: str) -> bool:
    lim = limits(asked.repo_root)
    return (lim.allow and lim.allow_persistent and asked.kind in lim.kinds and asked.action in lim.overridable
            and lifetime in lim.lifetimes
            and _preference_owner(lim) is not None
            and any(_scope_allowed(asked.action, s, lim) for s in PUSH_SCOPES))


def _session_binding(agent_id: str) -> dict | None:
    from brindle.autopilot import root_of
    from brindle.db import DB

    db = DB()
    try:
        root = db.get_agent(root_of(db, agent_id))
        if (root is None or root.parent_id is not None or root.mode != "interactive"
                or root.status in ("done", "paused") or root.dismissed_at is not None):
            return None
        return {"id": root.id, "created": root.created_at}
    finally:
        db.conn.close()


def approval_scopes(asked) -> dict[str, str]:
    lim = limits(asked.repo_root)
    if asked.action != "git-push" or not (timed_allowed(asked) or persistent_allowed(asked) or session_allowed(asked)):
        return {}
    return {level: remember_description(asked, level).removesuffix(", until revoked")
            for level in PUSH_SCOPES if _scope_allowed(asked.action, level, lim)}


def remember_description(asked, level: str = "branch") -> str:
    scope = json.loads(asked.scope)
    if asked.action == "git-push":
        if level == "all":
            return "Allow future non-force pushes to all policy-permitted remotes and branches across repositories, until revoked"
        if level in ("repo", "repository"):
            return f"Allow future non-force pushes to all policy-permitted remotes and branches in {asked.repo_root}, until revoked"
        if level == "remote":
            return f"Allow future non-force pushes to all branches on {scope['remote']} at {scope['url']} in this repository, until revoked"
        return f"Allow future non-force pushes to {scope['ref']} at {scope['url']} in this repository, until revoked"
    return f"Allow reading {scope['path']} in this repository, until revoked"


def remember(asked, minutes: int | None = None, scope_level: str = "branch", *,
             lifetime: str | None = None) -> str:
    """Persist the person's scoped decision; callers hold the ask's lock."""
    lifetime = lifetime or ("time" if minutes is not None else "forever")
    if not _saved_allowed(asked, lifetime):
        raise ValueError("current policy does not allow this approval lifetime")
    session = _session_binding(asked.agent_id) if lifetime == "session" else None
    if lifetime == "session" and session is None:
        raise ValueError("the supervisor session is no longer open")
    lim = limits(asked.repo_root)
    if not _scope_allowed(asked.action, scope_level, lim):
        raise ValueError("approval scope exceeds current policy")
    if minutes is not None and (not lim.allow or asked.kind not in lim.kinds
                                or asked.action not in lim.overridable or not 0 < minutes <= lim.max_minutes):
        raise ValueError("duration exceeds current policy")
    owner = _preference_owner(lim)
    if owner is None:
        raise ValueError("the approving identity cannot be verified")
    scope = _preference_scope(asked.action, json.loads(asked.scope), scope_level)
    binding = [owner, None if scope_level == "all" else asked.repo_root, asked.kind, asked.action, scope]
    if lifetime != "forever":
        binding.extend([lifetime, *([session] if session else [])])
    id_ = "remembered-" + hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:24]
    record = {"id": id_, "owner": owner, "repo_root": asked.repo_root, "kind": asked.kind,
              "action": asked.action, "scope": scope, "scope_level": scope_level, "lifetime": lifetime,
              "match": remember_description(asked, scope_level), "created": time.time()}
    if minutes is not None:
        record.update(expires_at=record["created"] + minutes * 60, uses_spent=0, max_uses=lim.max_uses)
        record["match"] = record["match"].replace("until revoked", f"for {minutes} minutes")
    if session is not None:
        record.update(session=session, uses_spent=0, max_uses=lim.max_uses)
        record["match"] = record["match"].replace("until revoked", f"until supervisor session {session['id']} ends")
    with permissions.editing() as store:
        store.remembered = [r for r in store.remembered if r.get("id") != id_] + [record]
    return id_


def remembered_match(req: Request, lifted: dict, lim: Limits, *, agent_id: str = "") -> str | None:
    if not lim.allow or not lim.allow_persistent or req.kind not in lim.kinds or lifted["action"] not in lim.overridable:
        return None
    owner = _preference_owner(lim)
    if owner is None:
        return None
    with permissions.editing() as store:
        for record in store.remembered:
            level = record.get("scope_level", "branch")
            lifetime = record.get("lifetime", "time" if "expires_at" in record else "forever")
            if (record.get("owner") != owner or (level != "all" and record.get("repo_root") != repo_key(req))
                    or record.get("kind") != req.kind or record.get("action") != lifted["action"]
                    or not _scope_allowed(lifted["action"], level, lim)
                    or lifetime not in lim.lifetimes
                    or record.get("scope") != _preference_scope(lifted["action"], lifted["scope"], level)
                    or not isinstance(record.get("id"), str)):
                continue
            if lifetime in ("session", "time"):
                if lifetime == "session":
                    session = _session_binding(agent_id) if agent_id else None
                    if session is None or record.get("session") != session:
                        continue
                if lifetime == "time" and time.time() >= min(record["expires_at"], record["created"] + lim.max_minutes * 60):
                    continue
                if record.get("uses_spent", 0) >= min(record.get("max_uses", lim.max_uses), lim.max_uses):
                    continue
                record["uses_spent"] = record.get("uses_spent", 0) + 1
            return record["id"]
    return None


def remembered_grants() -> list[dict]:
    fields = ("id", "repo_root", "kind", "match", "created")
    out = []
    for r in permissions.load_store().remembered:
        if not all(k in r for k in fields):
            continue
        session = r.get("session") if r.get("lifetime") == "session" else None
        if r.get("lifetime") == "session" and (
                not isinstance(session, dict) or _session_binding(session.get("id", "")) != session):
            continue
        if ("expires_at" in r and r["expires_at"] <= time.time()
                or "max_uses" in r and r.get("uses_spent", 0) >= r["max_uses"]):
            continue
        out.append({**{k: r[k] for k in fields}, "agent_id": session["id"] if session else "",
                    "expires_at": r.get("expires_at"),
                    "uses_left": max(0, r["max_uses"] - r.get("uses_spent", 0)) if "max_uses" in r else None,
                    "persistent": "expires_at" not in r and session is None})
    return out


def forget_remembered(id_: str) -> dict | None:
    with permissions.editing() as store:
        found = next((r for r in store.remembered if r.get("id") == id_), None)
        store.remembered = [r for r in store.remembered if r.get("id") != id_]
        return found


def ask_for(agent_id: str, raw: dict) -> tuple[str, str, str, str, str, str] | None:
    """What a one-time grant of ``raw`` (the request the agent's hook was
    refused) would cover and what the person is shown: (repo, kind, match,
    scope, action, description), worked out in the daemon from the agent's
    own workspace, its rules and the repository as it is now; nothing the
    hook says about it is taken. None when no one-time grant applies."""
    from brindle.config import load_repo_config
    from brindle.db import DB
    from brindle.profiles import profile_rules_for

    fields = ("provider", "kind", "tool", "cwd", "command", "path", "url")
    if not all(raw.get(f) is None or isinstance(raw.get(f), str) for f in fields):
        return None
    db = DB()
    try:
        agent = db.get_agent(agent_id)
        ws = db.get_workspace(agent.workspace_id) if agent else None
    finally:
        db.conn.close()
    if ws is None:
        return None
    try:
        req = Request(**{f: raw.get(f) or "" for f in ("provider", "kind", "tool", "cwd")},
                      worktree=ws.path, repo_root=ws.repo_root,
                      command=raw.get("command"), path=raw.get("path"), url=raw.get("url"))
        lim = limits(ws.repo_root)
        rules = [*permissions.all_rules(ws.repo_root), *profile_rules_for(agent.profile, ws.repo_root), *lim.deny]
        checks = load_repo_config(ws.repo_root).checks
        lifted, _ = override(req, decision_now(req, checks, rules), rules, checks, lim)
    except Exception:  # noqa: BLE001 - anything unexpected grants nothing
        return None
    k = key(req)
    if lifted is None or k is None:
        return None
    return repo_key(req), k[0], k[1], json.dumps(lifted["scope"], sort_keys=True), lifted["action"], describe(lifted)


def parse(req: Request | None) -> dict | None:
    try:
        got = json.loads(req.grant) if req is not None and req.grant else None
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def describe(offered: dict) -> str:
    """The offer in a line, for the person."""
    if offered.get("type") == "allow_for":
        return (f"allow {offered['kind']} {offered['match']!r} for this agent, for {offered['minutes']} min "
                f"or {offered['uses']} uses")
    scope = offered.get("scope") or {}
    if offered.get("action") == "git-push":
        return (f"lift {offered.get('rule')} once: push {scope.get('commit')} from {scope.get('git_dir')} "
                f"to {scope.get('ref')} at {scope.get('url')} ({scope.get('remote')})")
    return f"lift {offered.get('rule')} once: read {scope.get('path')}"


# -- adapters -----------------------------------------------------------------------------------


def _git(cwd: str, *args: str) -> tuple[int, list[str]]:
    try:
        res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return -1, []
    return res.returncode, [ln for ln in res.stdout.splitlines() if ln.strip()]


def push_scope(req: Request, lim: Limits) -> tuple[dict | None, str]:
    """The exact push ``req`` makes, when it is one a person may allow once."""
    if lim.protected:
        return None, "protected paths are set, and a push skips the merge check that enforces them"
    command = (req.command or "").strip()
    argv = permissions.simple_argv(command)
    if not argv or command != " ".join(argv) or len(argv) != 4 or argv[:2] != ["git", "push"]:
        return None, PUSH_FORM
    remote, (commit, _, ref) = argv[2], argv[3].partition(":")
    # The repository the push runs in must be the agent's own worktree, not
    # another clone or a repository nested inside it: that is what is bound.
    root = os.path.realpath(req.worktree) if req.worktree else ""
    code, top = _git(req.cwd or req.worktree, "rev-parse", "--show-toplevel")
    if not root or code != 0 or not top or os.path.realpath(top[0]) != root:
        return None, "the push doesn't run in the agent's own worktree"
    code, common = _git(root, "rev-parse", "--git-common-dir")
    if code != 0 or not common:
        return None, "the worktree's repository can't be read"
    git_dir = os.path.realpath(os.path.join(root, common[0]))
    cwd = root
    if (not REMOTE.fullmatch(remote) or not SHA.fullmatch(commit) or not BRANCH_REF.fullmatch(ref)
            or _git(cwd, "check-ref-format", ref)[0] != 0):
        return None, PUSH_FORM
    code, urls = _git(cwd, "remote", "get-url", "--push", "--all", remote)
    if code != 0 or len(urls) != 1:
        return None, f"remote {remote} has no single push URL"
    url = urls[0].strip()
    if not PUSH_URL.fullmatch(url) or "::" in url:
        return None, f"{remote}'s push URL {url!r} isn't https, ssh or user@host:path"
    for name in PUSH_CONFIG:
        name = name.format(r=remote)
        code, _ = _git(cwd, "config", "--get-all", name)
        if code != 1:
            return None, f"{name} is set (or can't be read), and could change where or what it pushes"
    for hook in PUSH_HOOKS:
        code, out = _git(cwd, "rev-parse", "--git-path", f"hooks/{hook}")
        if code != 0 or not out or os.path.exists(os.path.join(cwd, out[0])):
            return None, f"a {hook} hook runs on push"
    return {"repo": repo_key(req), "git_dir": git_dir, "remote": remote, "url": url, "ref": ref,
            "commit": commit}, ""


def env_scope(req: Request, lim: Limits) -> tuple[dict | None, str]:
    """The one ``.env`` file inside the worktree ``req`` reads."""
    root = os.path.realpath(req.worktree) if req.worktree else ""
    real = os.path.realpath(req.path) if req.path else ""
    if (req.kind != "read" or not root or not real.startswith(root + os.sep)
            or not os.path.basename(real).startswith(".env") or not os.path.isfile(real)):
        return None, "only one .env file inside the worktree can be read once"
    return {"path": real}, ""


ADAPTERS = {"git-push": push_scope, "env": env_scope}
