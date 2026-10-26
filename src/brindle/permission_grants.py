"""Saved actions and generalized asks, over brindle.permissions.

An **ask** is a request the policy left to a person. Each one carries its
exact form and a ladder of scopes a decision may cover (``generalize``): a
push's destination ("into main from any branch"), a file's top directory, a
command's family (its first two words), an MCP tool (or its whole server).
The second rung, the general form, is what brindle proposes as the default
grant; pending asks of the same kind and general form make one Inbox item
(``groups``), one decision resolves the item, and the person may narrow to
the exact request.

A **saved action** (permissions.SavedAction) is such a decision kept: a deny
stops every ask its pattern covers; an allow resolves an ask only within the
limits of the ask rule that asked (its scope level or pattern, its lifetime),
never past a deny rule (permissions.decide checks those first), and never
once expired. ``settle`` is what decide() calls; an ask a saved action covers
never reaches the Inbox.

Deciding from the Inbox (``decide_group``, ``allow_all``, ``unblock_denied``)
checks every candidate ask against its own policy, saves the grant, and
answers each hook through the daemon's hold (service.inbox.answer_permission),
so a broad grant never resolves a request its policy forbids."""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from brindle import permissions
from brindle.permissions import (
    LEVELS,
    PUSH_ARROW,
    PUSH_AT,
    UNKNOWN_REF,
    Decision,
    Request,
    Rule,
    SavedAction,
    simple_argv,
)
MODES = ("once", "session", "timed", "forever", "deny", "always-deny")


def _error(code: str, message: str, status: int = 400) -> Exception:
    from brindle.service.errors import ServiceError    # lazily: the service layer imports widely

    return ServiceError(code, message, status)
GENERAL = "general"
EXACT = "exact"


# -- generalizing one request ------------------------------------------------------------------


def grant_kind(req: Request) -> str:
    """The kind a saved action for ``req`` has: ``push`` for a git push, else the request's."""
    if req.kind == "bash" and req.command and permissions.push_calls(req.command):
        return "push"
    return req.kind


def ask_keys(req: Request) -> list[str]:
    """Every key a saved action must cover for ``req``: one per push the
    command makes (``"source → destination @ remote"``), else the one ask_key."""
    if grant_kind(req) == "push":
        return [p.key() for p in permissions.push_calls(req.command or "")]
    return [ask_key(req)]


def ask_key(req: Request) -> str:
    """What a saved action's pattern is matched against: a push's "source →
    destination @ remote" (the first, when a command pushes several), a
    file's path relative to the worktree, the command, the URL or the tool name."""
    kind = grant_kind(req)
    if kind == "push":
        return permissions.push_calls(req.command or "")[0].key()
    if req.kind in permissions.PATH_KINDS:
        pattern = permissions.learnable_pattern(req)
        return pattern[1] if pattern else (req.path or "")
    if req.kind == "bash":
        return " ".join((req.command or "").split())
    if req.kind == "fetch":
        return req.url or ""
    return req.tool


def _scope(value: str, pattern: str, label: str, level: str | None = None) -> dict:
    return {"value": value, "pattern": pattern, "label": label, "level": level}


def scopes_for(kind: str, key: str, name: str = "") -> list[dict]:
    """The scope ladder for a request of ``kind`` with ask key ``key``,
    narrowest first; the general form is the one valued GENERAL."""
    if kind == "push":
        refs, _, remote = key.partition(PUSH_AT)
        src, _, dst = refs.partition(PUSH_ARROW)
        remote = remote or "*"
        return [_scope(EXACT, key, f"This push only · {src} into {dst} on {remote}", "branch"),
                _scope(GENERAL, f"*{PUSH_ARROW}{dst}{PUSH_AT}{remote}", f"Into {dst} from any branch on {remote}",
                       "destination"),
                _scope("remote", f"*{PUSH_ARROW}*{PUSH_AT}{remote}", f"All branches on {remote}", "remote"),
                _scope("repository", f"*{PUSH_ARROW}*{PUSH_AT}*", "All pushes in this repository", "repository"),
                _scope("all", f"*{PUSH_ARROW}*{PUSH_AT}*", "All pushes in every repository", "all")]
    if kind in permissions.PATH_KINDS:
        verb = {"read": "reads", "write": "writes", "edit": "edits"}[kind]
        top = key.split("/")[0] if "/" in key else ""
        out = [_scope(EXACT, key, f"This file only · {key}")]
        if top and not any(c in top for c in "*?["):
            out.append(_scope(GENERAL, f"{top}/**", f"Everything under {top}/**"))
        else:
            out.append(_scope(GENERAL, key, f"This file only · {key}"))
        out.append(_scope("repository", "**", f"Any file ({verb} anywhere in the repository)"))
        return out
    if kind == "bash":
        out = [_scope(EXACT, key, f"This command only · {key}")]
        if simple_argv(key) is not None:
            family = " ".join(key.split()[:2])
            out.append(_scope(GENERAL, f"{family} *", f"Commands starting with {family}"))
        else:
            out.append(_scope(GENERAL, key, f"This command only · {key}"))
        out.append(_scope("repository", "*", "Any command in the repository"))
        return out
    if kind == "mcp":
        parts = key.split("__")
        server = parts[1] if key.startswith("mcp__") and len(parts) >= 3 and parts[1] else None
        out = [_scope(GENERAL, key, f"This tool · {name or key}")]
        if server:
            out.append(_scope("server", f"mcp__{server}__*", f"Every {server} tool"))
        out.append(_scope("all", "mcp__*", "Every MCP tool"))
        return out
    if kind == "fetch":
        parts = urlsplit(key)
        host = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
        out = [_scope(EXACT, key, f"This URL only · {key}")]
        out.append(_scope(GENERAL, f"{host}/*", f"Everything on {host}") if host
                   else _scope(GENERAL, key, f"This URL only · {key}"))
        out.append(_scope("all", "*", "Any URL"))
        return out
    return [_scope(EXACT, key, f"This only · {key}"), _scope(GENERAL, key, f"This only · {key}"),
            _scope("all", "*", f"Every {kind} request")]


@dataclass
class Ask:
    """One request, generalized."""
    kind: str
    exact: str
    scopes: list[dict]

    def scope(self, value: str) -> dict | None:
        return next((s for s in self.scopes if s["value"] == value), None)

    @property
    def general(self) -> dict:
        return self.scope(GENERAL) or self.scopes[0]


def generalize(req: Request) -> Ask:
    """``req`` generalized. A command that pushes to several destinations has
    no single exact or destination form: its ladder starts at the remote
    (every branch on it), and its exact form lists every push."""
    kind = grant_kind(req)
    keys = ask_keys(req)
    if kind == "push" and len(set(keys)) > 1:
        scopes = [s for s in scopes_for(kind, keys[0]) if s["value"] not in (EXACT, GENERAL)]
        return Ask(kind, ", ".join(dict.fromkeys(keys)), scopes)
    return Ask(kind, ask_key(req), scopes_for(kind, ask_key(req), req.tool))


# -- matching saved actions ----------------------------------------------------------------------


def scope_matches(kind: str, pattern: str, key: str) -> bool:
    """Whether a saved action's ``pattern`` covers ask key ``key``."""
    if pattern in ("*", "**"):
        return True
    if kind == "push":
        if PUSH_ARROW not in pattern or PUSH_ARROW not in key:
            return False
        pats, refs = permissions.push_pattern(pattern), permissions.push_pattern(key)
        for pat, ref in zip(pats, refs):
            if ref == UNKNOWN_REF and pat != "*" or not fnmatch.fnmatchcase(ref, pat):
                return False
        return True
    if kind == "bash":
        if simple_argv(key) is None:
            return pattern == key   # a compound or quoted command: only the exact grant
        if pattern.endswith(" *"):
            head = pattern[:-2].split()
            return key.split()[:len(head)] == head
        return pattern == key or fnmatch.fnmatchcase(key, pattern)
    return key == pattern or fnmatch.fnmatchcase(key, pattern)


def _other_parts_allowed(req: Request, rules: list[Rule] | None) -> bool:
    """A push grant covers a compound command only when every part that isn't
    the push is itself allowed by a rule (``cd x && git push`` is fine, ``git
    push && curl ...`` is not)."""
    if simple_argv(req.command) is not None:
        return True
    from dataclasses import replace

    from brindle.native.permissions import split_commands

    rules = rules if rules is not None else permissions.all_rules(req.repo_root or None)
    for part in split_commands(req.command or ""):
        words = part.split()
        if words and words[0] == "git" and "push" in words:
            continue
        if words and words[0] == "cd":
            continue
        got = permissions._rule_decision(replace(req, command=part), rules, None)
        if got is None or got.decision != "allow":
            return False
    return True


def covers(action: SavedAction, req: Request, rules: list[Rule] | None = None) -> bool:
    """Whether ``action``'s pattern covers ``req``. A deny covers it as soon as
    any push the command makes matches; an allow only when every push does
    and nothing else runs alongside them."""
    if action.kind != grant_kind(req):
        return False
    if action.repo is not None and req.repo_root and action.repo != req.repo_root:
        return False
    hits = [scope_matches(action.kind, action.pattern, key) for key in ask_keys(req)]
    if action.decision == "deny":
        return any(hits)
    if not all(hits):
        return False
    return action.kind != "push" or _other_parts_allowed(req, rules)


def limits_allow(action_level: str | None, action_pattern: str, action_life: str, action_minutes: float | None,
                 rule: Rule | None) -> str | None:
    """Why a saved allow with this scope and lifetime falls outside ``rule``'s
    limits (the ask rule that asked; None for an unmatched ask, which has no
    limits), or None when it is within them."""
    limits = (rule.limits if rule is not None else None) or {}
    scope = limits.get("max_scope")
    if scope:
        if action_level is not None:
            if scope in LEVELS and LEVELS.index(action_level) > LEVELS.index(scope):
                return "outside its approval scope"
        elif scope not in ("*", "**") and not action_pattern.startswith(scope.rstrip("*")):
            return "outside its approval scope"
    life = limits.get("life")
    if life in ("session", "time") and action_life == "forever":
        return "not permitted until revoked"
    if life == "time":
        cap = limits.get("minutes")
        if action_life != "time" or action_minutes is None or (cap is not None and action_minutes > cap):
            return f"over its {cap:g} minute limit" if cap is not None else "over its time limit"
    return None


def settle(req: Request, rule: Rule | None, saved: list[SavedAction], *, session: str | None = None,
           rules: list[Rule] | None = None, now: float | None = None, denies_only: bool = False) -> Decision | None:
    """The saved action that settles an ask for ``req`` (``rule``: the ask
    rule that asked, None for an unmatched ask), as a Decision, or None. A
    saved deny always stops it; a saved allow only within the rule's limits
    (``denies_only``: look for a deny alone, before the policy's own allows).
    ``session``: the asking agent's session, for saved actions that last one."""
    now = time.time() if now is None else now
    hits = [s for s in saved if not s.expired(now) and (s.life != "session" or (session and s.session == session))
            and (s.decision == "deny" or not denies_only) and covers(s, req, rules)]
    for s in hits:
        if s.decision == "deny":
            return Decision("deny", f"stopped by your saved deny {s.id} ({s.kind} {s.pattern})")
    if denies_only:
        return None
    for s in hits:
        if limits_allow(s.level, s.pattern, s.life, s.minutes, rule) is None:
            return Decision("allow", f"covered by your saved action {s.id} ({s.kind} {s.pattern})", rule)
    return None


# -- pending asks, grouped -----------------------------------------------------------------------


@dataclass
class PendingAsk:
    agent_id: str
    root_id: str
    request_id: str
    digest: str
    repo_root: str
    request: Request
    ask: Ask
    created_at: float
    policy: Decision              # what the rules say, saved actions aside: deny (blocked) or ask
    worker: str = ""

    @property
    def blocked(self) -> bool:
        return self.policy.decision == "deny"

    @property
    def rule(self) -> Rule | None:
        return self.policy.rule


@dataclass
class Group:
    key: str
    repo_root: str
    kind: str
    items: list[PendingAsk]       # oldest first
    policy: Decision = field(default_factory=lambda: Decision("ask", ""))

    @property
    def rep(self) -> PendingAsk:
        return self.items[0]


def _policy(req: Request, repo_root: str) -> Decision:
    """The rules' answer for ``req`` with saved actions set aside (an ask's rule, for its limits)."""
    from brindle.config import load_repo_config

    checks = list(load_repo_config(repo_root).checks or []) if repo_root else None
    return permissions.decide(req, checks=checks, rules=permissions.all_rules(repo_root or None), saved=[])


def pending_asks(db, repo_root: str | None = None) -> list[PendingAsk]:
    """Every request a hook holds for a person now (across ``repo_root``'s
    sessions, or every repo's), oldest first."""
    from brindle import autopilot
    from brindle.daemon.holds import HOLDS
    from brindle.service import inbox

    out = []
    for agent_id in HOLDS.agents():
        agent = db.get_agent(agent_id)
        if agent is None:
            continue
        ws = db.get_workspace(agent.workspace_id)
        if ws is None or (repo_root and ws.repo_root != repo_root):
            continue
        root = autopilot.root_of(db, agent.id)
        for p in inbox.pending_permissions(db, agent):
            if p.via != "hook" or p.request is None or p.row is None:
                continue
            out.append(PendingAsk(agent.id, root, p.dto.request_id, p.dto.digest, ws.repo_root, p.request,
                                  generalize(p.request), float(p.row["created_at"]), _policy(p.request, ws.repo_root),
                                  worker=agent.profile))
    return sorted(out, key=lambda a: a.created_at)


def group_key(repo_root: str, kind: str, pattern: str) -> str:
    return f"{repo_root}|{kind}|{pattern}"


def _restrictive(policies: list[Decision]) -> Decision:
    """The policy a group is shown under: a deny, else the tightest lifetime."""
    for p in policies:
        if p.decision == "deny":
            return p

    def rank(p: Decision) -> tuple:
        limits = (p.rule.limits if p.rule else None) or {}
        life = limits.get("life")
        return ({"time": 0, "session": 1}.get(life, 2), limits.get("minutes") or 0)

    return sorted(policies, key=rank)[0]


def groups(asks: list[PendingAsk]) -> list[Group]:
    """One group per repo, kind and general form, each generalized from its oldest ask."""
    by_key: dict[str, Group] = {}
    for a in asks:
        key = group_key(a.repo_root, a.ask.kind, a.ask.general["pattern"])
        g = by_key.get(key)
        if g is None:
            g = by_key[key] = Group(key, a.repo_root, a.ask.kind, [])
        g.items.append(a)
    for g in by_key.values():
        g.policy = _restrictive([a.policy for a in g.items])
    return list(by_key.values())


def eligible(ask: PendingAsk, scope: dict, mode: str, minutes: float | None) -> str | None:
    """Why ``ask`` can't be resolved by a grant of ``scope`` in ``mode``, or None."""
    if ask.blocked:
        return "blocked by a deny rule"
    if mode in ("deny", "always-deny"):
        return None
    if not all(scope_matches(ask.ask.kind, scope["pattern"], key) for key in ask_keys(ask.request)):
        return "pushes outside this scope"     # a command pushing to more than the scope covers
    if mode == "once":
        return None
    life = {"session": "session", "timed": "time", "forever": "forever"}[mode]
    return limits_allow(scope.get("level"), scope["pattern"], life, minutes, ask.rule)


def candidates(asks: list[PendingAsk], g: Group, scope: dict) -> list[PendingAsk]:
    """Every pending ask of the group's kind and repo the scope covers (every
    push of a command that makes several)."""
    return [a for a in asks if a.repo_root == g.repo_root and a.ask.kind == g.kind
            and all(scope_matches(g.kind, scope["pattern"], key) for key in ask_keys(a.request))]


def _result(a: PendingAsk, why: str | None = None) -> dict:
    row = {"agent_id": a.agent_id, "request_id": a.request_id, "exact": a.ask.exact}
    return {**row, "why": why} if why else row


def _answer(db, ask: PendingAsk, choice: str) -> str | None:
    """Answer one held ask; why it couldn't be, if it couldn't."""
    from brindle.service import inbox
    from brindle.service.errors import ServiceError

    try:
        inbox.answer_permission(db, ask.agent_id, ask.request_id, ask.digest, choice)
    except ServiceError as e:
        return e.message
    return None


def _grant(g: Group, scope: dict, decision: str, life: str, minutes: float | None, rep: PendingAsk) -> SavedAction:
    return permissions.save_action(
        g.kind, scope["pattern"], decision, scope=scope["value"], level=scope.get("level"),
        request=rep.request.to_dict(), life=life, minutes=minutes if life == "time" else None,
        session=rep.root_id if life == "session" else None,
        repo=None if scope["value"] == "all" else g.repo_root, note=scope["label"])


def _resolve(db, hits: list[PendingAsk], choice: str) -> tuple[list[dict], list[dict]]:
    done, failed = [], []
    for a in hits:
        why = _answer(db, a, choice)
        (failed if why else done).append(_result(a, why))
    return done, failed


def decide_group(db, key: str, scope_value: str, mode: str, minutes: float | None = None,
                 asks: list[PendingAsk] | None = None) -> dict:
    """The person's decision on Inbox item ``key``: ``mode`` once (allow
    these, save nothing), session, timed (``minutes``) or forever (save an
    allow of ``scope_value``), deny (stop these, save nothing) or always-deny
    (save a deny). Every candidate the scope covers is checked against its
    own policy; the grant is saved first, then each eligible ask is answered
    through its hold. What was resumed, stopped, skipped (and why) or failed."""
    if mode not in MODES:
        raise _error("invalid", f"mode must be one of {', '.join(MODES)}")
    asks = pending_asks(db) if asks is None else asks
    g = next((g for g in groups(asks) if g.key == key), None)
    if g is None:
        raise _error("not_found", "no such Inbox item any more: fetch the asks again", 404)
    scope = g.rep.ask.scope(scope_value)
    if scope is None:
        raise _error("invalid", f"scope must be one of {', '.join(s['value'] for s in g.rep.ask.scopes)}")
    if mode == "timed" and (minutes is None or minutes <= 0):
        raise _error("invalid", "a timed grant needs positive minutes")
    # Narrowed to the exact request, "once" and "deny" resolve the oldest ask
    # alone; at any wider scope the whole item (a grant: every ask it covers).
    exact = scope["value"] == EXACT
    wanted = ([g.rep] if exact else list(g.items)) if mode in ("once", "deny") else candidates(asks, g, scope)
    reasons: dict[str, str] = {}
    hits = []
    for a in wanted:
        # A deny needs no coverage: it stops what the person points at.
        why = eligible(a, scope, mode, minutes) if mode not in ("deny", "always-deny") else None
        if why:
            reasons[a.request_id] = why
        else:
            hits.append(a)
    if not hits:
        raise _error("conflict", "nothing in this scope can be approved: "
                     + "; ".join(sorted(set(reasons.values()))), 409)
    grant = None
    if mode in ("session", "timed", "forever"):
        grant = _grant(g, scope, "allow", {"session": "session", "timed": "time", "forever": "forever"}[mode],
                       minutes, g.rep)
    elif mode == "always-deny":
        grant = _grant(g, scope, "deny", "forever", None, g.rep)
    done, failed = _resolve(db, hits, "deny" if mode in ("deny", "always-deny") else "allow_once")
    return {"mode": mode, "scope": scope, "grant": grant.to_dict() if grant else None,
            "resumed": done if mode not in ("deny", "always-deny") else [],
            "stopped": done if mode in ("deny", "always-deny") else [],
            "skipped": [_result(a, reasons[a.request_id]) for a in wanted if a.request_id in reasons],
            "failed": failed}


# -- the stall notice's automated fixes ----------------------------------------------------------


def _mode_for(policy: Decision, forever: bool) -> tuple[str, float | None]:
    """The widest grant ``policy`` permits: forever when asked for, else its
    lifetime (a timed one at its cap), else for the session."""
    if forever:
        return "forever", None
    limits = (policy.rule.limits if policy.rule else None) or {}
    if limits.get("life") == "time":
        return "timed", limits.get("minutes")
    if limits.get("life") == "forever":
        return "forever", None
    return "session", None


def plan_allow_all(asks: list[PendingAsk], forever: bool = False, only: list[PendingAsk] | None = None) -> dict:
    """What "allow everything waiting" would grant: one grant per Inbox item
    at the general scope where every ask's policy permits it, an exact grant
    for each ask whose policy permits only that, and what stays waiting."""
    wanted = asks if only is None else only
    plan, done, skipped = [], [], []
    for g in groups(asks):
        mine = [a for a in g.items if a in wanted]
        if not mine:
            continue
        mode, minutes = _mode_for(g.policy, forever)
        general = g.rep.ask.general
        ok = [a for a in mine if eligible(a, general, mode, minutes) is None]
        if ok:
            plan.append({"group": g, "scope": general, "hits": ok, "mode": mode, "minutes": minutes})
            done += ok
        for a in mine:
            if a in ok:
                continue
            own_mode, own_minutes = _mode_for(a.policy, forever)
            exact = a.ask.scopes[0]
            if eligible(a, exact, own_mode, own_minutes) is None:
                plan.append({"group": g, "scope": exact, "hits": [a], "mode": own_mode, "minutes": own_minutes})
                done.append(a)
            else:
                skipped.append(a)
    return {"plan": plan, "done": done, "skipped": skipped}


def plan_to_json(planned: dict) -> dict:
    return {"grants": [{"group": e["group"].key, "kind": e["group"].kind, "scope": e["scope"],
                        "mode": e["mode"], "minutes": e["minutes"],
                        "requests": [a.request_id for a in e["hits"]]} for e in planned["plan"]],
            "resumes": len(planned["done"]),
            "skipped": [_result(a, "blocked by a deny rule" if a.blocked else "outside its own limits")
                        for a in planned["skipped"]]}


def allow_all(db, repo_root: str | None = None, forever: bool = False, asks: list[PendingAsk] | None = None,
              only: list[PendingAsk] | None = None) -> dict:
    """Carry out plan_allow_all: save each grant, answer each ask through its hold."""
    asks = pending_asks(db, repo_root) if asks is None else asks
    planned = plan_allow_all(asks, forever, only)
    grants, resumed, failed = [], [], []
    for e in planned["plan"]:
        life = {"session": "session", "timed": "time", "forever": "forever"}[e["mode"]]
        grants.append(_grant(e["group"], e["scope"], "allow", life, e["minutes"], e["hits"][0]).to_dict())
        done, lost = _resolve(db, e["hits"], "allow_once")
        resumed += done
        failed += lost
    return {**plan_to_json(planned), "saved": grants, "resumed": resumed, "failed": failed}


def deny_rules_for(ask: PendingAsk, rules: list[Rule]) -> list[Rule]:
    return [r for r in rules if r.decision == "deny" and permissions.rule_matches(r, ask.request)]


def unblock_denied(db, repo_root: str | None = None, forever: bool = False) -> dict:
    """Narrow the deny rules that block waiting asks with an exception for
    exactly each blocked request (a repo's own deny can't be changed here:
    the ask stays blocked), add an ask rule where unmatched actions are
    denied, then allow what was blocked (allow_all over those asks)."""
    asks = pending_asks(db, repo_root)
    denied = [a for a in asks if a.blocked]
    if not denied:
        return {"excepted": 0, "added": 0, "locked": [], **plan_to_json(plan_allow_all([], forever)),
                "saved": [], "resumed": [], "failed": []}
    excepted, added, locked = 0, 0, []
    store_ids = {r.id for r in permissions.load_store().rules}
    for a in denied:
        req = a.request
        key = ask_key(req) if req.kind != "bash" else " ".join((req.command or "").split())
        kind = req.kind
        for r in deny_rules_for(a, permissions.all_rules(a.repo_root)):
            if r.id not in store_ids:
                locked.append(a)     # the repo's own deny, or a profile's: not the person's to narrow
                continue
            if permissions.add_exclusion(r.id, kind, key, "exact") is not None:
                excepted += 1
        if a in locked:
            continue
        a.policy = _policy(req, a.repo_root)
        if a.blocked:    # unmatched actions are denied: a rule for its general form, that asks
            general = a.ask.general
            match_type = "push" if a.ask.kind == "push" else ("exact" if general["pattern"] == key else "glob")
            permissions.add_rule(kind, general["pattern"], "ask", match_type)
            added += 1
            a.policy = _policy(req, a.repo_root)
    free = [a for a in denied if a not in locked and not a.blocked]
    out = allow_all(db, repo_root, forever, asks=asks, only=free)
    return {**out, "excepted": excepted, "added": added, "locked": [_result(a) for a in locked]}
